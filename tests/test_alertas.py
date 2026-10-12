"""Alertas de preço (etapa G4): criação, vigia (ticks, candles M1 depois de um intervalo), banco e API do painel."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from starlette.testclient import TestClient

import fake_mt5 as fm
from fake_mt5 import FakeMT5, make_symbol, make_ticks
from test_mt5_client import make_client
from trading_mcp import alertas
from trading_mcp.config import Settings
from trading_mcp.mt5_client import MT5Error
from trading_mcp.painel import app as painel_app

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # o mesmo do FakeClock
T0 = NOW.timestamp()


def _setup(tmp_path, ticks=None, m1=None):
    index = make_symbol("USTECm", digits=2, point=0.01, bid=30000.0, ask=30001.12,
                        trade_calc_mode=fm.SYMBOL_CALC_MODE_CFD, currency_base="USD", currency_profit="USD")
    fake = FakeMT5([index], ticks={"USTECm": make_ticks(ticks)} if ticks else None)
    if m1 is not None:
        fake.rates_tf[("USTECm", fm.TIMEFRAME_M1)] = m1
    client, fake = make_client(fake)
    store = alertas.AlertStore(tmp_path / "alertas.sqlite3")
    notified: list[dict] = []
    vigia = alertas.Vigia(store, client, notified.append, monotonic=client.clock.monotonic)
    return client, fake, store, vigia, notified


def _m1(rows):
    """Candles M1 a partir de (abertura, máxima, mínima)."""
    return np.array([(int(t), lo, hi, lo, lo, 1, 100, 0) for t, hi, lo in rows], dtype=fm.RATES_DTYPE)


def _later(client, seconds):
    client.clock.now = NOW + timedelta(seconds=seconds)


# ---------------------------------------------------------------- criação
def test_create_takes_the_side_from_the_bid_and_rounds_to_the_symbol_digits(tmp_path):
    client, _, store, _, _ = _setup(tmp_path)
    above, created = alertas.create_alert(store, client, "USTEC", 30100.004, "  FVG de baixa H1   (início) ")
    assert created and above["simbolo"] == "USTECm" and above["lado"] == "acima" and above["preco"] == 30100.0
    assert above["titulo"] == "FVG de baixa H1 (início)" and above["preco_criacao"] == 30000.0
    assert above["vigiado_ate_ms"] == int(T0 * 1000)  # o tick da criação
    below, _ = alertas.create_alert(store, client, "USTECm", "29900.5", None)
    assert below["lado"] == "abaixo" and below["titulo"] == "Nível"
    again, created = alertas.create_alert(store, client, "USTEC", 30100.0, "outro")
    assert not created and again["id"] == above["id"]
    assert [a["id"] for a in store.all()] == [below["id"], above["id"]]


@pytest.mark.parametrize("price", [30000.0, 30000.001, "abc", True, None, -1, float("nan"), [1]])
def test_create_refuses_bad_prices(tmp_path, price):
    client, _, store, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError):
        alertas.create_alert(store, client, "USTEC", price, "x")
    assert store.all() == []


def test_create_needs_a_quote_and_caps_the_active_alerts(tmp_path, monkeypatch):
    client, fake, store, _, _ = _setup(tmp_path)
    monkeypatch.setattr(alertas, "MAX_ACTIVE", 2)
    alertas.create_alert(store, client, "USTEC", 30100, "a")
    alertas.create_alert(store, client, "USTEC", 30200, "b")
    with pytest.raises(ValueError, match="máximo 2"):
        alertas.create_alert(store, client, "USTEC", 30300, "c")
    fake.zero_tick_symbols.add("USTECm")
    with pytest.raises(MT5Error, match="Sem cotação"):
        alertas.create_alert(store, client, "USTEC", 29000, "d")


def test_reading_without_alerts_does_not_create_the_database(tmp_path):
    _, _, store, vigia, _ = _setup(tmp_path)
    assert store.all() == [] and vigia.check_once() == [] and not store.delete(1) and store.clear_fired() == 0
    assert not store.path.exists()


# ---------------------------------------------------------------- vigia
def test_first_tick_reaching_the_level_fires_once(tmp_path):
    ticks = [(T0 - 5, 30500.0, 30501.0),  # antes da criação: não conta
             (T0 + 1, 30050.0, 30051.0), (T0 + 2, 30100.5, 30101.5), (T0 + 3, 30200.0, 30201.0)]
    client, _, store, vigia, notified = _setup(tmp_path, ticks)
    alert, _ = alertas.create_alert(store, client, "USTEC", 30100, "Máx. Londres")
    _later(client, 5)
    fired = vigia.check_once()
    assert [f["id"] for f in fired] == [alert["id"]] and notified == fired
    stored = store.all()[0]
    assert stored["status"] == "disparado" and stored["disparo_por"] == "tick" and stored["preco_disparo"] == 30100.5
    assert stored["disparado_utc"] == "2026-09-30T12:00:02Z"
    assert vigia.check_once() == [] and len(notified) == 1


def test_ticks_on_the_other_side_do_not_fire_and_move_the_cursor(tmp_path):
    ticks = [(T0 + 1, 29950.0, 29951.0), (T0 + 2, 30090.0, 30091.0)]
    client, _, store, vigia, notified = _setup(tmp_path, ticks)
    alert, _ = alertas.create_alert(store, client, "USTEC", 30100, "x")
    below, _ = alertas.create_alert(store, client, "USTEC", 29900, "y")
    _later(client, 5)
    assert vigia.check_once() == [] and notified == []
    assert vigia._cursors == {alert["id"]: int((T0 + 2) * 1000), below["id"]: int((T0 + 2) * 1000)}
    vigia.stop()  # grava até onde conferiu
    assert {a["vigiado_ate_ms"] for a in store.active()} == {int((T0 + 2) * 1000)}


def test_the_last_tick_counts_even_without_tick_history(tmp_path):
    client, fake, store, vigia, notified = _setup(tmp_path)
    alertas.create_alert(store, client, "USTEC", 29900, "Mín. Ásia")
    _later(client, 3)
    assert vigia.check_once() == []  # tick novo, mas do mesmo lado
    fake.symbols["USTECm"].bid = 29899.0
    _later(client, 4)
    [fired] = vigia.check_once()
    assert fired["preco_disparo"] == 29899.0 and fired["disparado_utc"] == "2026-09-30T12:00:04Z"


def test_nothing_new_since_the_cursor_reads_nothing(tmp_path, monkeypatch):
    client, _, store, vigia, _ = _setup(tmp_path)
    alertas.create_alert(store, client, "USTEC", 30100, "x")
    monkeypatch.setattr(client, "ticks_between", lambda *a: pytest.fail("leu ticks sem tick novo"))
    assert vigia.check_once() == []  # o último tick é o da criação


def test_after_a_long_gap_m1_candles_fill_the_middle(tmp_path):
    # Painel fechado por 2 h: o toque está num candle M1 no meio do intervalo, sem ticks guardados.
    m1 = _m1([(T0 - 60, 30500.0, 29000.0),  # candle da criação (começou antes dela): não conta
              (T0 + 600, 30050.0, 29990.0), (T0 + 1800, 30120.0, 30010.0), (T0 + 3600, 30300.0, 30100.0)])
    client, fake, store, vigia, notified = _setup(tmp_path, m1=m1)
    alertas.create_alert(store, client, "USTEC", 30100, "Topo diário de 29/09")
    _later(client, 7200)
    [fired] = vigia.check_once()
    assert fired["disparo_por"] == "candle_m1" and fired["preco_disparo"] is None
    assert fired["disparado_utc"] == "2026-09-30T12:30:00Z"
    title, text = alertas.message(fired)
    assert title == "USTECm tocou 30.100,00" and "candle M1 das 09:30 (São Paulo)" in text and "painel estava fechado" in text


def test_a_long_gap_without_candles_waits_for_the_sync_then_moves_the_cursor_to_the_cut(tmp_path):
    # Sem candles no intervalo: pode ser histórico ainda sincronizando, então o cursor espera; passado o tempo de
    # espera, vale como mercado fechado e a leitura longa não se repete.
    client, fake, store, vigia, _ = _setup(tmp_path, m1=_m1([]))
    alert, _ = alertas.create_alert(store, client, "USTEC", 30100, "x")
    _later(client, 7200)
    assert vigia.check_once() == [] and alert["id"] not in vigia._cursors
    client.clock.t += alertas.SYNC_WAIT_S - 1
    vigia.check_once()
    assert alert["id"] not in vigia._cursors
    calls = len(fake.range_calls)
    client.clock.t += 2
    vigia.check_once()
    cut = (NOW + timedelta(seconds=7200)).replace(second=0) - timedelta(minutes=1)
    assert vigia._cursors[alert["id"]] == int(cut.timestamp() * 1000) - 1 and len(fake.range_calls) == calls + 1
    _later(client, 7201)
    vigia.check_once()
    assert len(fake.range_calls) == calls + 1  # agora só ticks


def test_stale_m1_history_is_read_again_until_it_reaches_the_cut(tmp_path):
    # O terminal devolve primeiro o histórico guardado (até 12:40); o toque às 13:10 só aparece depois da sincronização.
    client, fake, store, vigia, notified = _setup(tmp_path, m1=_m1([(T0 + 2400, 30050.0, 30000.0)]))
    alert, _ = alertas.create_alert(store, client, "USTEC", 30100, "x")
    _later(client, 7200)
    assert vigia.check_once() == []
    assert vigia._cursors[alert["id"]] == int((T0 + 2400 + 60) * 1000) - 1  # para no último candle, não no corte
    fake.rates_tf[("USTECm", fm.TIMEFRAME_M1)] = _m1(
        [(T0 + 2400, 30050.0, 30000.0), (T0 + 4200, 30150.0, 30050.0), (T0 + 7080, 30060.0, 30040.0)])
    client.clock.t += 1
    [fired] = vigia.check_once()
    assert fired["disparo_por"] == "candle_m1" and fired["disparado_utc"] == "2026-09-30T13:10:00Z"


def test_creation_mid_minute_reads_the_rest_of_that_minute_by_ticks(tmp_path):
    # Criado às 12:00:30. O candle das 12:00 tem a máxima de antes da criação (não conta); o toque é um tick no último
    # segundo desse minuto, que o copy_ticks_range só devolve com o fim no minuto cheio.
    ticks = [(T0 + 10, 30200.0, 30201.0), (T0 + 59.5, 30100.0, 30101.0)]
    m1 = _m1([(T0, 30200.0, 29990.0), (T0 + 60, 30090.0, 30000.0), (T0 + 3540, 30050.0, 30000.0)])
    client, _, store, vigia, _ = _setup(tmp_path, ticks, m1)
    _later(client, 30)
    alertas.create_alert(store, client, "USTEC", 30100, "x")
    _later(client, 3700)
    [fired] = vigia.check_once()
    assert fired["disparo_por"] == "tick" and fired["disparado_utc"] == "2026-09-30T12:00:59Z"
    assert fired["preco_disparo"] == 30100.0


def test_a_pre_creation_extreme_in_the_creation_minute_never_fires(tmp_path):
    m1 = _m1([(T0, 30200.0, 29990.0), (T0 + 60, 30090.0, 30000.0), (T0 + 3540, 30050.0, 30000.0)])
    client, _, store, vigia, notified = _setup(tmp_path, [(T0 + 10, 30200.0, 30201.0)], m1)
    _later(client, 30)
    alert, _ = alertas.create_alert(store, client, "USTEC", 30100, "x")
    _later(client, 3700)
    assert vigia.check_once() == [] and notified == []
    assert vigia._cursors[alert["id"]] > int((T0 + 3540) * 1000)


def test_a_touch_after_the_cut_fires_by_tick(tmp_path):
    later = T0 + 3700
    m1 = _m1([(T0 + 60 * i, 30050.0, 30000.0) for i in range(1, 61)])  # 12:01 a 13:00, completos até o corte
    client, _, store, vigia, _ = _setup(tmp_path, [(later - 30, 30101.0, 30102.0)], m1)
    alertas.create_alert(store, client, "USTEC", 30100, "x")
    _later(client, 3700)
    [fired] = vigia.check_once()
    assert fired["disparo_por"] == "tick" and fired["preco_disparo"] == 30101.0


def test_a_long_pause_between_rounds_recovers_by_m1(tmp_path):
    client, fake, store, vigia, _ = _setup(tmp_path, [(T0 + 1, 30010.0, 30011.0)], _m1([]))
    alertas.create_alert(store, client, "USTEC", 30100, "x")
    _later(client, 2)
    vigia.check_once()
    calls = len(fake.range_calls)
    _later(client, 300)  # 5 min: menos que TICKS_ONLY, mas o vigia ficou parado (PC suspenso)
    client.clock.t += 298
    vigia.check_once()
    assert len(fake.range_calls) == calls + 1


def test_a_forex_bid_one_float_step_below_the_level_still_touches(tmp_path):
    eurusd = make_symbol("EURUSD", bid=1.10000, ask=1.10012)
    fake = FakeMT5([eurusd], ticks={"EURUSD": make_ticks([(T0 + 1, 1.1008199999999999, 1.10094)])})
    client, fake = make_client(fake)
    store = alertas.AlertStore(tmp_path / "alertas.sqlite3")
    vigia = alertas.Vigia(store, client, lambda a: None, monotonic=client.clock.monotonic)
    alertas.create_alert(store, client, "EURUSD", 1.10082, "EQH M5")
    _later(client, 2)
    assert len(vigia.check_once()) == 1


@pytest.mark.parametrize("pc_ahead_s", [120, -120])
def test_server_time_drives_the_reads_when_the_pc_clock_is_off(tmp_path, pc_ahead_s):
    client, fake, store, vigia, _ = _setup(tmp_path)
    fake.tick_age_s["USTECm"] = pc_ahead_s  # último tick = relógio do PC menos isto
    server_now = T0 - pc_ahead_s
    fake.ticks["USTECm"] = make_ticks([(server_now + 1, 30050.0, 30051.0), (server_now + 2, 30120.0, 30121.0)])
    alert, _ = alertas.create_alert(store, client, "USTEC", 30100, "x")
    assert alert["vigiado_ate_ms"] == int(server_now * 1000)
    _later(client, 3)
    [fired] = vigia.check_once()
    assert fired["preco_disparo"] == 30120.0


def test_alerts_with_different_cursors_on_the_same_symbol(tmp_path):
    # A (12:00:00) já foi conferido até 12:00:01; B nasce às 12:00:03, depois de um tick abaixo do nível dele às
    # 12:00:02,5: esse tick está no intervalo lido para A, mas não pode disparar B.
    ticks = [(T0 + 1, 30050.0, 30051.0)]
    client, fake, store, vigia, _ = _setup(tmp_path, ticks)
    first, _ = alertas.create_alert(store, client, "USTEC", 30100, "A")
    _later(client, 2)
    assert vigia.check_once() == [] and vigia._cursors[first["id"]] == int((T0 + 1) * 1000)
    ticks.append((T0 + 2.5, 29950.0, 29951.0))
    fake.ticks["USTECm"] = make_ticks(ticks)
    _later(client, 3)
    second, _ = alertas.create_alert(store, client, "USTEC", 29970, "B")
    _later(client, 4)
    assert vigia.check_once() == []
    fake.ticks["USTECm"] = make_ticks(ticks + [(T0 + 5, 29960.0, 29961.0)])
    _later(client, 6)
    assert [f["id"] for f in vigia.check_once()] == [second["id"]]
    assert [a["id"] for a in store.active()] == [first["id"]]


def test_two_panels_never_notify_twice(tmp_path):
    ticks = [(T0 + 1, 30150.0, 30151.0)]
    client, _, store, vigia, notified = _setup(tmp_path, ticks)
    other_notified: list[dict] = []
    other = alertas.Vigia(store, client, other_notified.append)
    alertas.create_alert(store, client, "USTEC", 30100, "x")
    _later(client, 2)
    first, second = vigia.check_once(), other.check_once()
    assert len(first) + len(second) == 1 and len(notified) + len(other_notified) == 1


def test_a_removed_alert_never_fires(tmp_path):
    ticks = [(T0 + 1, 30150.0, 30151.0)]
    client, _, store, vigia, notified = _setup(tmp_path, ticks)
    alert, _ = alertas.create_alert(store, client, "USTEC", 30100, "x")
    assert store.delete(alert["id"]) and not store.delete(alert["id"])
    _later(client, 2)
    assert vigia.check_once() == [] and notified == []
    assert not store.fire(alert["id"], NOW, 1.0, "tick")


def test_clear_removes_only_fired_alerts(tmp_path):
    ticks = [(T0 + 1, 30150.0, 30151.0)]
    client, _, store, vigia, _ = _setup(tmp_path, ticks)
    alertas.create_alert(store, client, "USTEC", 30100, "dispara")
    keep, _ = alertas.create_alert(store, client, "USTEC", 29000, "fica")
    _later(client, 2)
    assert len(vigia.check_once()) == 1
    assert store.clear_fired() == 1 and [a["id"] for a in store.all()] == [keep["id"]]


def test_mt5_errors_show_in_the_status_and_clear_when_it_works(tmp_path, monkeypatch):
    client, _, store, vigia, _ = _setup(tmp_path)
    alertas.create_alert(store, client, "USTEC", 30100, "x")
    _later(client, 2)

    def broken(*args):
        raise MT5Error("Sem ticks de USTECm: (-1, 'falhou')")

    monkeypatch.setattr(client, "ticks_between", broken)
    assert vigia.check_once() == []
    assert vigia.status()["erros"] == {"USTECm": "Sem ticks de USTECm: (-1, 'falhou')"}
    monkeypatch.undo()
    _later(client, 3)
    vigia.check_once()
    assert vigia.status()["erros"] == {} and vigia.status()["ultimo_ciclo"] == "2026-09-30T12:00:03Z"


def test_the_thread_runs_and_stops(tmp_path):
    _, _, _, vigia, _ = _setup(tmp_path)
    assert vigia.status()["ativo"] is False
    vigia._interval = 0.01
    vigia.start()
    assert vigia.status()["ativo"] is True
    vigia.stop()
    assert vigia.status()["ativo"] is False


def test_newer_database_is_refused(tmp_path):
    client, _, store, _, _ = _setup(tmp_path)
    alertas.create_alert(store, client, "USTEC", 30100, "x")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE meta SET valor = '99' WHERE chave = 'schema'")
    with pytest.raises(alertas.AlertasError, match="versão mais nova"):
        store.all()


# ---------------------------------------------------------------- notificação
def test_message_for_a_tick(tmp_path):
    alert = {"simbolo": "XAUUSDm", "preco": 2650.5, "digitos": 3, "titulo": "EQH M5", "disparo_por": "tick",
             "preco_disparo": 2650.512, "disparado_utc": "2026-09-30T15:42:10Z"}
    assert alertas.message(alert) == ("XAUUSDm tocou 2.650,500", "EQH M5. Bid 2.650,512 às 12:42 (São Paulo).")


def test_toast_text_goes_through_the_environment(monkeypatch):
    seen = {}

    def run(command, **kwargs):
        seen["command"], seen["env"] = command, kwargs["env"]
        return subprocess.CompletedProcess(command, 7, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert alertas.show_toast("Título", "'; exit 7; $(calc)") is True
    assert "$(calc)" not in " ".join(seen["command"])
    assert seen["env"]["TRADING_MCP_ALERTA_TEXTO"] == alertas._b64("'; exit 7; $(calc)")
    assert seen["command"][0].lower().endswith("windowspowershell\\v1.0\\powershell.exe")
    monkeypatch.setattr(subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 1, b"", b"erro"))
    assert alertas.show_toast("t", "x") is False


@pytest.mark.skipif(sys.platform != "win32", reason="notificação do Windows só no Windows")
def test_toast_builds_without_showing():
    assert alertas.toast_self_test() is True


# ---------------------------------------------------------------- API
HEADERS = {"X-Painel": "1", "Content-Type": "application/json"}


@pytest.fixture
def api(tmp_path):
    client, fake, store, vigia, _ = _setup(tmp_path)
    app = painel_app.create_app(Settings(), client, alerts=store, vigia=vigia)
    return TestClient(app, base_url="http://127.0.0.1"), store


def test_api_creates_lists_and_removes_alerts(api):
    http, store = api
    out = http.post("/api/alertas", json={"simbolo": "USTEC", "preco": 30100, "titulo": "FVG"}, headers=HEADERS)
    assert out.status_code == 200 and out.json()["criado"] is True
    alert = out.json()["alerta"]
    assert alert["simbolo"] == "USTECm" and alert["lado"] == "acima" and alert["criado"]["sao_paulo"] == "2026-09-30 09:00:00"
    listed = http.get("/api/alertas").json()
    assert [a["id"] for a in listed["alertas"]] == [alert["id"]]
    assert listed["vigia"] == {"ativo": False, "ultimo_ciclo": None, "erros": {}}
    assert http.delete(f"/api/alertas/{alert['id']}", headers=HEADERS).json() == {"removido": alert["id"]}
    assert http.delete(f"/api/alertas/{alert['id']}", headers=HEADERS).status_code == 404
    assert http.post("/api/alertas/limpar", json={}, headers=HEADERS).json() == {"removidos": 0}


def test_api_reports_bad_requests(api):
    http, _ = api
    assert http.post("/api/alertas", content=b"{nao", headers=HEADERS).status_code == 400
    assert http.post("/api/alertas", json=[1], headers=HEADERS).status_code == 400
    assert http.post("/api/alertas", json={"simbolo": "USTEC", "preco": 30000}, headers=HEADERS).status_code == 400
    assert http.post("/api/alertas", json={"simbolo": "NOPE", "preco": 1}, headers=HEADERS).status_code == 404


@pytest.mark.parametrize("headers", [
    {"Content-Type": "application/json"},  # sem X-Painel
    {**HEADERS, "Origin": "http://evil.example"},
    {**HEADERS, "Origin": "http://127.0.0.1:9999"},
    {**HEADERS, "Sec-Fetch-Site": "cross-site"},
    {"X-Painel": "1", "Content-Type": "text/plain"},
])
def test_api_refuses_writes_from_outside_the_page(api, headers):
    http, store = api
    out = http.post("/api/alertas", content=b'{"simbolo": "USTEC", "preco": 30100}', headers=headers)
    assert out.status_code == 403 and "só a página do painel" in out.json()["erro"]
    assert store.all() == []


def test_api_accepts_the_page_own_origin(api):
    http, _ = api
    headers = {**HEADERS, "Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"}
    assert http.post("/api/alertas", json={"simbolo": "USTEC", "preco": 30100}, headers=headers).status_code == 200


def test_api_without_a_vigia_says_so(tmp_path):
    client, _, store, _, _ = _setup(tmp_path)
    http = TestClient(painel_app.create_app(Settings(), client, alerts=store), base_url="http://127.0.0.1")
    assert http.get("/api/alertas").json() == {"alertas": [], "vigia": None}
