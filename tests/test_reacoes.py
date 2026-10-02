"""Reações guardadas e estatísticas por surpresa (MT5 simulado, calendário em arquivo)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import fake_mt5 as fm
from fake_mt5 import FakeMT5, make_symbol
from test_calendario import ev, ts, val, write
from test_mt5_client import make_client
from trading_mcp import reacao
from trading_mcp.calendario import EconomicCalendar
from trading_mcp.reacoes import BACKUP_NAME, ReacoesError, ReactionStore

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # relógio do FakeClock
E1 = datetime(2026, 9, 29, 12, 30, tzinfo=UTC)  # claims acima da previsão (+ contínuos no mesmo horário)
E2 = datetime(2026, 9, 30, 10, 30, tzinfo=UTC)  # claims abaixo da previsão
PMI = E2 + timedelta(minutes=10)  # outro evento moderado dentro da janela de 15 min de E2
E3 = datetime(2026, 9, 30, 11, 30, tzinfo=UTC)  # janela de 60 min ainda não terminou às 12:00
M = timedelta(minutes=1)

EVENTS = [
    ev(1, "Pedidos iniciais", "initial-jobless-claims", importance="MODERATE", unit="NONE", mult="THOUSANDS",
       frequency="WEEK"),
    ev(2, "Pedidos contínuos", "continuing-jobless-claims", importance="MODERATE", unit="NONE", mult="THOUSANDS",
       frequency="WEEK"),
    ev(3, "PMI", "sp-global-manufacturing-pmi", importance="MODERATE", unit="NONE"),
    ev(4, "Discurso", "fed-governor-speech", importance="LOW", kind="EVENT"),
]


def values(e3_actual: float | None = None, e1_forecast: float = 220, **e3) -> list[dict]:
    return [
        val(11, 1, E1, actual=230, forecast=e1_forecast, prev=225),
        val(12, 2, E1, actual=1900, forecast=1950),
        val(21, 1, E2, actual=210, forecast=220),
        val(31, 3, PMI, actual=50, forecast=49),
        val(41, 4, E2 + 5 * M),
        val(51, 1, E3, actual=e3_actual, forecast=220, prev=210, **e3),
    ]


def m1(start: datetime, end: datetime, price: Callable[[datetime, int], float],
       skip: Callable[[datetime], bool] = lambda t: False) -> np.ndarray:
    rows, prev, t, i = [], None, start, 0
    while t < end:
        close = price(t, i)
        if not skip(t):
            open_ = close if prev is None else prev
            rows.append((int(t.timestamp()), open_, max(open_, close), min(open_, close), close, 10, 12, 0))
            prev = close
        t, i = t + M, i + 1
    return np.array(rows, dtype=fm.RATES_DTYPE)


def _quiet(t: datetime) -> bool:
    """Ruído de 2 pontos só antes de cada evento (fora do candle de referência): janelas limpas depois."""
    return t < E1 - M or E1 + 61 * M <= t < E2 - M


def eur(t: datetime, i: int) -> float:
    # 1,10000 → 1,10100 em E1 (+0,091%) → 1,09880 em E2 (−0,200%).
    level = 1.1 if t < E1 else 1.101 if t < E2 else 1.0988
    return round(level + (0.00002 * (i % 2) if _quiet(t) else 0.0), 5)


def btc(t: datetime, i: int) -> float:
    return 60_300.0 if t >= E2 else 60_000.0 + (2.0 * (i % 2) if t < E2 - M else 0.0)


def eur_bars(skip: Callable[[datetime], bool] = lambda t: False) -> np.ndarray:
    return m1(E1 - timedelta(hours=3), NOW + timedelta(hours=3), eur, skip)


RATES = {
    "EURUSD": eur_bars(),
    # Ouro: parado desde 10:00 do dia 30 (referência antiga em E2 e depois).
    "XAUUSD": m1(datetime(2026, 9, 29, tzinfo=UTC), datetime(2026, 9, 30, 10, tzinfo=UTC), lambda t, i: 4000.0),
    # BTC: o M1 do terminal só começa em 30/09 (E1 fica fora do histórico).
    "BTCUSD": m1(datetime(2026, 9, 30, tzinfo=UTC), NOW + timedelta(hours=3), btc),
    # Petróleo: nada nas 2 h antes de E2 (sem referência).
    "UKOIL": m1(datetime(2026, 9, 29, tzinfo=UTC), datetime(2026, 9, 29, 20, tzinfo=UTC), lambda t, i: 70.0),
}
SYMBOLS = [
    make_symbol("EURUSD"),
    make_symbol("XAUUSD", digits=3, point=0.001, bid=4000.0, ask=4000.2, currency_base="XAU", currency_profit="USD"),
    make_symbol("BTCUSD", digits=2, point=0.01, bid=60_300.0, ask=60_310.0, currency_base="BTC", currency_profit="USD"),
    make_symbol("UKOIL", digits=3, point=0.001, bid=70.0, ask=70.03, currency_base="USD", currency_profit="USD"),
]
INSTRUMENTS = ("EURUSD", "XAUUSD", "BTCUSD")


def _store(tmp_path: Path, vals: list[dict] | None = None, *, events=EVENTS, instruments=INSTRUMENTS,
           monotonic: Callable[[], float] | None = None, budget: float = 40.0, rates: dict | None = None,
           backup: bool = False, fake_kwargs: dict | None = None, **header):
    client, fake = make_client(FakeMT5(list(SYMBOLS), rates=dict(rates or RATES), **(fake_kwargs or {})))
    header.setdefault("generated_gmt", ts(NOW) - 10)
    write(tmp_path, events, values() if vals is None else vals, **header)
    calendar = EconomicCalendar(lambda: tmp_path / "calendar_US.json", now_utc=client.clock.now_utc)
    store = ReactionStore(tmp_path / "db" / "reacoes.sqlite3", client, calendar, instruments,
                          backup_dir=tmp_path / "export" if backup else None,
                          monotonic=monotonic or (lambda: 0.0), time_budget_s=budget)
    return store, client, fake


def _rows(store: ReactionStore, sql: str) -> list[dict]:
    with sqlite3.connect(store._path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql)]


def _windows(store: ReactionStore) -> dict[tuple, dict]:
    return {(w["horario_utc"], w["simbolo"], w["minutos"]): w for w in _rows(store, "SELECT * FROM janelas")}


def _measurement(store: ReactionStore, moment: datetime, symbol: str) -> dict:
    return _rows(store, f"SELECT * FROM medicoes WHERE horario_utc = '{_iso(moment)}' AND simbolo = '{symbol}'")[0]


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _later(client, tmp_path: Path, minutes: float = 10, vals: list[dict] | None = None) -> None:
    """Avança o relógio (a confirmação exige 10 min entre medições) e regrava o calendário nesse horário."""
    client.clock.now += timedelta(minutes=minutes)
    write(tmp_path, EVENTS, values() if vals is None else vals, generated_gmt=ts(client.clock.now) - 10)


# ---------------------------------------------------------------- registro
def test_register_saves_facts_and_measures_finished_windows(tmp_path):
    store, _, _ = _store(tmp_path)
    out = store.register()
    assert out["divulgacoes"] == {
        "calendario_cobre_desde": _iso(NOW - timedelta(days=7, seconds=10)), "no_calendario": 5, "novas": 5,
        "realizado_preenchido": 0, "alteradas": 0,
    }
    # E1 (EUR, ouro), E2 e PMI (os três); E3 ainda não terminou; BTC em E1 fora do histórico. O ouro parado
    # em E2 e no PMI fica a confirmar.
    assert out["medicoes"] == {
        "horarios_medidos": 3, "gravadas": 8, "sem_referencia": 0, "a_confirmar": 2, "horarios_faltando": 0,
        "fora_do_historico_m1": {"BTCUSD": {"medicoes": 1, "historico_m1_desde": "2026-09-30T00:00:00Z"}},
    }
    assert out["banco"]["divulgacoes"] == 5 and out["banco"]["medicoes_a_confirmar"] == 2 and out["avisos"] == []

    facts = {f["id"]: f for f in _rows(store, "SELECT * FROM eventos")}
    assert "mt5:4:41" not in facts  # importância baixa não é guardada
    claims = facts["mt5:1:11"]
    assert (claims["realizado"], claims["previsao"], claims["surpresa"], claims["sentido"]) == (230, 220, 10, "acima")
    assert claims["horario_utc"] == _iso(E1) and claims["unidade"] == "mil" and claims["periodo"]
    assert facts["mt5:1:51"]["sentido"] == "sem_realizado"

    windows = _windows(store)
    w1 = windows[(_iso(E1), "EURUSD", 5)]
    assert (w1["situacao"], w1["preco"], w1["variacao_pct"], w1["pips"], w1["vezes_o_tipico"]) == ("ok", 1.101, 0.091, 10.0, 50.0)
    assert windows[(_iso(E2), "EURUSD", 15)]["variacao_pct"] == -0.2
    assert windows[(_iso(E2), "EURUSD", 60)]["vezes_o_tipico"] is None  # típico só até 30 min
    assert windows[(_iso(E2), "BTCUSD", 1)]["variacao_pct"] == 0.5
    gold = _measurement(store, E2, "XAUUSD")
    assert gold["referencia_antiga"] == 1 and gold["confirmada"] == 0
    assert windows[(_iso(E2), "XAUUSD", 5)]["situacao"] == "sem_negociacao"
    eur_e1 = _measurement(store, E1, "EURUSD")
    assert eur_e1["instrumento"] == "EURUSD" and eur_e1["referencia"] == 1.1 and eur_e1["maxima_pct"] == 0.091
    assert eur_e1["confirmada"] == 1


def test_stored_rows_match_reacao_evento(tmp_path):
    store, client, _ = _store(tmp_path)
    store.register()
    windows = _windows(store)
    shown = reacao.reaction(client, None, when=_iso(E2), symbols=["EURUSD", "BTCUSD"], windows=[1, 5, 15, 60])
    for m in shown["movimento_medido"]:
        for w in m["janelas"]:
            row = windows[(_iso(E2), m["simbolo"], w["minutos"])]
            assert (row["preco"], row["variacao_pct"], row["pontos"], row["pips"], row["vezes_o_tipico"]) == (
                w["preco"], w["variacao_pct"], w["pontos"], w.get("pips"), w.get("vezes_o_tipico")
            )


def test_no_trading_before_event_is_confirmed_on_the_next_run(tmp_path):
    store, client, _ = _store(tmp_path, instruments=("UKOIL",))
    out = store.register()
    assert out["medicoes"]["gravadas"] == 3 and out["medicoes"]["sem_referencia"] == 2  # E2 e PMI
    assert out["medicoes"]["a_confirmar"] == 2
    oil = _measurement(store, E2, "UKOIL")
    assert oil["situacao"] == "sem_referencia" and oil["referencia"] is None and oil["confirmada"] == 0
    assert "Sem negociação" in oil["avisos"]
    assert _rows(store, f"SELECT * FROM janelas WHERE horario_utc = '{_iso(E2)}'") == []
    five = store.stats("initial-jobless-claims")["eventos"][0]["reacoes"][0]["janelas"]["5"]
    assert five["todas"]["n"] == 1 and five["fora_das_contas"] == {"a_confirmar": 1, "sem_medicao": 1}

    soon = store.register()  # logo depois: mede de novo, mas ainda não confirma
    assert soon["medicoes"]["gravadas"] == 2 and soon["medicoes"]["a_confirmar"] == 2
    _later(client, tmp_path)
    again = store.register()  # mesmo resultado 10 min depois: confirmado
    assert again["medicoes"]["gravadas"] == 2 and again["medicoes"]["a_confirmar"] == 0
    assert store.register()["medicoes"]["gravadas"] == 0  # não mede de novo
    five = store.stats("initial-jobless-claims")["eventos"][0]["reacoes"][0]["janelas"]["5"]
    assert five["fora_das_contas"] == {"sem_medicao": 1, "sem_referencia": 1}


def test_register_does_not_duplicate(tmp_path):
    store, client, _ = _store(tmp_path)
    store.register()
    _later(client, tmp_path)
    again = store.register()
    assert again["divulgacoes"]["novas"] == 0
    assert again["medicoes"]["gravadas"] == 2 and again["medicoes"]["a_confirmar"] == 0  # ouro confirmado
    # BTC em E1 continua no calendário e fora do histórico: o aviso se repete enquanto o calendário o cobrir.
    assert again["medicoes"]["fora_do_historico_m1"]["BTCUSD"]["medicoes"] == 1
    third = store.register()
    assert third["medicoes"]["gravadas"] == 0
    assert len(_rows(store, "SELECT * FROM eventos")) == 5 and len(_rows(store, "SELECT * FROM medicoes")) == 8
    assert len(_rows(store, "SELECT * FROM janelas")) == 8 * 4


def test_pending_window_and_late_actual_are_completed_later(tmp_path):
    store, client, _ = _store(tmp_path)
    store.register()
    client.clock.now = NOW + timedelta(hours=2)
    write(tmp_path, EVENTS, values(e3_actual=215), generated_gmt=ts(client.clock.now) - 10)
    out = store.register()
    assert out["divulgacoes"]["realizado_preenchido"] == 1 and out["divulgacoes"]["alteradas"] == 0
    assert out["avisos"] == []
    # E3 agora (EUR, BTC e o ouro parado, a confirmar) e o ouro de E2 e do PMI confirmado.
    assert out["medicoes"]["gravadas"] == 5 and out["medicoes"]["a_confirmar"] == 1
    e3 = _rows(store, "SELECT * FROM eventos WHERE id = 'mt5:1:51'")[0]
    assert (e3["realizado"], e3["surpresa"], e3["sentido"]) == (215, -5, "abaixo")
    assert e3["registrado_utc"] == _iso(NOW) and e3["atualizado_utc"] == _iso(NOW + timedelta(hours=2))


def test_actual_arriving_with_source_reading_and_revised_prev_is_not_a_change(tmp_path):
    store, _, _ = _store(tmp_path)
    store.register()
    write(tmp_path, EVENTS, values(e3_actual=215, impact="POSITIVE", revised=212), generated_gmt=ts(NOW) - 10)
    out = store.register()
    assert out["divulgacoes"]["realizado_preenchido"] == 1 and out["divulgacoes"]["alteradas"] == 0
    assert out["avisos"] == []
    e3 = _rows(store, "SELECT * FROM eventos WHERE id = 'mt5:1:51'")[0]
    assert e3["leitura_da_fonte"] == "positivo" and e3["anterior_revisado"] == 212


def test_changed_value_in_calendar_is_updated_and_warned(tmp_path):
    store, _, _ = _store(tmp_path)
    store.register()
    write(tmp_path, EVENTS, values(e1_forecast=225), generated_gmt=ts(NOW) - 10)
    out = store.register()
    assert out["divulgacoes"]["alteradas"] == 1
    assert any("initial-jobless-claims" in a and "previsao 220.0 → 225.0" in a and "surpresa" not in a
               for a in out["avisos"])
    assert _rows(store, "SELECT surpresa FROM eventos WHERE id = 'mt5:1:11'")[0]["surpresa"] == 5


def test_stale_price_at_window_end_is_left_out_of_stats(tmp_path):
    # O EURUSD para de negociar 2 min depois de E2 (até 11:40): +15 e +60 seriam o preço de 10:32.
    rates = {**RATES, "EURUSD": eur_bars(skip=lambda t: E2 + 2 * M <= t < E2 + 70 * M)}
    store, client, _ = _store(tmp_path, rates=rates, instruments=("EURUSD",))
    store.register()
    windows = _windows(store)
    assert windows[(_iso(E2), "EURUSD", 5)]["situacao"] == "ok"  # 10:32 está a 3 min do fim da janela
    assert windows[(_iso(E2), "EURUSD", 15)]["situacao"] == "preco_antigo"
    assert windows[(_iso(E2), "EURUSD", 60)]["ate_utc"] == "2026-09-30T10:32:00Z"
    _later(client, tmp_path)
    store.register()  # mesmo resultado: confirmado
    stats = store.stats("initial-jobless-claims", windows=[5, 15])["eventos"][0]["reacoes"][0]["janelas"]
    assert stats["5"]["todas"]["n"] == 2
    assert stats["15"]["todas"]["n"] == 1 and stats["15"]["fora_das_contas"] == {"preco_antigo": 1, "sem_medicao": 1}


def test_missing_candles_are_remeasured_when_the_terminal_has_them(tmp_path):
    # Primeiro registro com um buraco de 10 min na janela de E2 (histórico ainda baixando).
    rates = {**RATES, "EURUSD": eur_bars(skip=lambda t: E2 + 20 * M <= t < E2 + 30 * M)}
    store, _, fake = _store(tmp_path, rates=rates, instruments=("EURUSD",))
    out = store.register()
    assert out["medicoes"]["a_confirmar"] == 2  # o buraco também cai na janela de 60 min do PMI
    first = _measurement(store, E2, "EURUSD")
    assert (first["candles_recebidos"], first["candles_esperados"], first["confirmada"]) == (50, 60, 0)
    fake.rates["EURUSD"] = eur_bars()
    again = store.register()
    assert again["medicoes"]["gravadas"] == 2 and again["medicoes"]["a_confirmar"] == 0
    fixed = _measurement(store, E2, "EURUSD")
    assert fixed["candles_recebidos"] is None and fixed["confirmada"] == 1


def test_time_budget_stops_between_symbols_and_next_call_continues(tmp_path):
    clock = iter([0.0, 0.0, 100.0])  # início, EURUSD em E1, depois estoura
    store, _, _ = _store(tmp_path, monotonic=lambda: next(clock, 100.0))
    out = store.register()
    assert out["medicoes"]["horarios_medidos"] == 1 and out["medicoes"]["gravadas"] == 1
    assert out["medicoes"]["horarios_faltando"] == 3  # E1 ficou pela metade
    assert any("rode `reacoes_registrar` de novo" in a for a in out["avisos"])
    store._monotonic = lambda: 0.0
    rest = store.register()
    assert rest["medicoes"]["horarios_faltando"] == 0 and rest["medicoes"]["gravadas"] == 7
    assert len(_rows(store, "SELECT * FROM medicoes")) == 8


def test_unconfirmed_measurement_that_leaves_m1_history_becomes_unconfirmable(tmp_path):
    store, client, fake = _store(tmp_path)
    store.register()  # ouro parado em E2 e no PMI: a confirmar
    # O M1 do ouro passa a começar às 09:00 do dia 30: E2 e o PMI saem do histórico antes da confirmação.
    fake.rates["XAUUSD"] = m1(datetime(2026, 9, 30, 9, tzinfo=UTC), datetime(2026, 9, 30, 10, tzinfo=UTC),
                              lambda t, i: 4000.0)
    _later(client, tmp_path)
    out = store.register()
    assert out["medicoes"]["nao_confirmaveis"] == 2 and out["banco"]["medicoes_a_confirmar"] == 0
    assert out["banco"]["medicoes_nao_confirmaveis"] == 2
    assert any("não confirmáveis" in a for a in out["avisos"])
    gold = store.stats("initial-jobless-claims", windows=[5])["eventos"][0]["reacoes"][1]
    assert gold["simbolo"] == "XAUUSD" and gold["janelas"]["5"]["fora_das_contas"] == {
        "nao_confirmavel": 1, "sem_medicao": 1
    }
    again = store.register()  # não volta a ficar pendente
    assert again["medicoes"]["gravadas"] == 0 and "nao_confirmaveis" not in again["medicoes"]


def test_value_that_vanishes_from_calendar_is_kept(tmp_path):
    store, _, _ = _store(tmp_path)
    store.register()
    vals = values()
    vals[0] = val(11, 1, E1, actual=None, forecast=220, prev=225)  # realizado sumiu
    write(tmp_path, EVENTS, vals, generated_gmt=ts(NOW) - 10)
    out = store.register()
    assert out["divulgacoes"]["alteradas"] == 1
    assert any("realizado sumiu do calendário" in a for a in out["avisos"])
    kept = _rows(store, "SELECT * FROM eventos WHERE id = 'mt5:1:11'")[0]
    assert (kept["realizado"], kept["surpresa"], kept["sentido"]) == (230, 10, "acima")


def test_disconnected_terminal_saves_facts_but_measures_nothing(tmp_path):
    store, client, fake = _store(tmp_path)
    client.symbol_spec("EURUSD")  # conecta antes de perder a conexão
    fake.connected = False
    out = store.register()
    assert out["divulgacoes"]["novas"] == 5 and out["medicoes"]["gravadas"] == 0
    assert out["medicoes"]["horarios_faltando"] == 3
    assert any("sem conexão" in a for a in out["avisos"])
    assert _rows(store, "SELECT * FROM medicoes") == []


def test_connection_lost_during_measurement_is_not_stored(tmp_path, monkeypatch):
    store, _, fake = _store(tmp_path, instruments=("EURUSD", "BTCUSD"))
    real = reacao.measure

    def measure(*args, **kwargs):
        out = real(*args, **kwargs)
        fake.connected = False  # cai depois da primeira medição
        return out

    monkeypatch.setattr(reacao, "measure", measure)
    out = store.register()
    assert out["medicoes"]["gravadas"] == 1
    assert any("sem conexão durante a medição" in e for e in out["erros"])
    monkeypatch.setattr(reacao, "measure", real)
    fake.connected = True
    assert store.register()["medicoes"]["gravadas"] == 4  # o resto, na volta da conexão


def test_measurement_error_is_retried_next_time(tmp_path, monkeypatch):
    store, _, _ = _store(tmp_path, instruments=("EURUSD", "BTCUSD"))
    real = reacao.measure

    def measure(mt5, symbol, *args, **kwargs):
        if symbol == "BTCUSD":
            raise reacao.MT5Error("ticks indisponíveis")
        return real(mt5, symbol, *args, **kwargs)

    monkeypatch.setattr(reacao, "measure", measure)
    out = store.register()
    assert out["medicoes"]["gravadas"] == 3 and len(out["erros"]) == 2  # BTC em E2 e no PMI
    assert any("serão tentadas de novo" in a for a in out["avisos"])
    monkeypatch.setattr(reacao, "measure", real)
    assert store.register()["medicoes"]["gravadas"] == 2


def test_history_start_read_failure_is_an_error_not_out_of_history(tmp_path):
    # As 3 tentativas do primeiro símbolo (EURUSD) falham; os outros são lidos.
    store, _, _ = _store(tmp_path, fake_kwargs={"rates_fail_times": 3})
    out = store.register()
    assert any("EURUSD: início do histórico M1 não lido" in e for e in out["erros"])
    assert "EURUSD" not in out["medicoes"]["fora_do_historico_m1"]
    assert not _rows(store, "SELECT * FROM medicoes WHERE simbolo = 'EURUSD'")
    assert store.register()["medicoes"]["gravadas"] == 3 + 2  # EURUSD agora, e o ouro medido de novo


def test_missing_calendar_does_not_block_pending_measurements(tmp_path):
    store, client, fake = _store(tmp_path)
    client.symbol_spec("EURUSD")
    fake.connected = False
    store.register()  # guarda os fatos, não mede
    fake.connected = True
    (tmp_path / "calendar_US.json").unlink()
    out = store.register()
    assert any("Calendário indisponível" in a for a in out["avisos"]) and out["medicoes"]["gravadas"] == 8
    assert out["divulgacoes"]["novas"] == 0


def test_missing_calendar_on_first_run_is_an_error(tmp_path):
    store, _, _ = _store(tmp_path)
    (tmp_path / "calendar_US.json").unlink()
    with pytest.raises(Exception, match="TradingMcpCalendar"):
        store.register()
    assert not store._path.exists()


def test_gap_between_runs_is_warned(tmp_path):
    store, client, _ = _store(tmp_path)
    store.register()
    client.clock.now = NOW + timedelta(days=10)
    write(tmp_path, EVENTS, values(), generated_gmt=ts(client.clock.now) - 10)
    out = store.register()
    assert any(a.startswith("Lacuna") and "2026-09-30T11:59:50Z" in a and "InpDaysBack" in a for a in out["avisos"])


def test_stale_calendar_is_warned(tmp_path):
    store, _, _ = _store(tmp_path, generated_gmt=ts(NOW) - 3600)
    assert any("desatualizado" in a for a in store.register()["avisos"])


def test_unknown_instrument_is_reported_and_others_measured(tmp_path):
    store, _, _ = _store(tmp_path, instruments=("EURUSD", "NAOEXISTE"))
    out = store.register()
    assert out["medicoes"]["gravadas"] == 3 and any("NAOEXISTE" in e for e in out["erros"])


def test_backup_copy_is_written_when_something_changes(tmp_path):
    store, client, _ = _store(tmp_path, backup=True)
    out = store.register()
    copy = tmp_path / "export" / BACKUP_NAME
    assert out["copia_de_seguranca"] == str(copy)
    with sqlite3.connect(copy) as conn:
        assert conn.execute("SELECT COUNT(*) FROM eventos").fetchone()[0] == 5
    _later(client, tmp_path)
    store.register()  # confirma o ouro
    assert "copia_de_seguranca" not in store.register()  # nada mudou


def test_register_requires_calendar(tmp_path):
    _, client, _ = _store(tmp_path)
    with pytest.raises(ValueError, match="Calendário não configurado"):
        ReactionStore(tmp_path / "x.sqlite3", client, None, INSTRUMENTS).register()


# ---------------------------------------------------------------- estatísticas
def test_stats_group_by_surprise_with_sample_and_mixed_windows(tmp_path):
    store, client, _ = _store(tmp_path)
    store.register()
    _later(client, tmp_path)
    store.register()  # confirma o ouro parado
    out = store.stats("claims", windows=[5, 15])
    initial, continuing = out["eventos"]
    assert initial["codigo"] == "initial-jobless-claims" and continuing["codigo"] == "continuing-jobless-claims"
    assert initial["divulgacoes"] == 3 and initial["amostra_pequena"] is True
    assert initial["surpresas"] == {"acima": 1, "abaixo": 1, "sem_realizado": 1}
    assert initial["no_mesmo_horario"] == {
        "divulgacoes_com_outros_eventos": 1, "eventos": {"continuing-jobless-claims": 1}
    }
    assert "registradas_depois_do_dia" not in initial and "estimativas" not in initial

    assert [r["simbolo"] for r in initial["reacoes"]] == ["EURUSD", "XAUUSD", "BTCUSD"]  # ordem configurada
    reactions = {r["simbolo"]: r["janelas"] for r in initial["reacoes"]}
    five = reactions["EURUSD"]["5"]
    assert five["todas"]["n"] == 2 and five["todas"]["mediana_abs_pct"] == pytest.approx(0.1455, abs=1e-3)
    assert five["todas"]["mediana_vezes_o_tipico"] == 80.0 and five["todas"]["reacao_clara"] == 2
    assert five["surpresa_acima"] == {"n": 1, "mediana_pct": 0.091, "subiu": 1, "caiu": 0}
    assert five["surpresa_abaixo"] == {"n": 1, "mediana_pct": -0.2, "subiu": 0, "caiu": 1}
    assert five["fora_das_contas"] == {"sem_medicao": 1} and "com_outro_evento_na_janela" not in five  # E3
    assert reactions["EURUSD"]["15"]["com_outro_evento_na_janela"] == 1  # PMI 10 min depois de E2
    assert reactions["XAUUSD"]["5"]["todas"]["n"] == 1
    assert reactions["XAUUSD"]["5"]["fora_das_contas"] == {"referencia_antiga": 1, "sem_medicao": 1}
    btc5 = reactions["BTCUSD"]["5"]
    assert btc5["surpresa_abaixo"]["mediana_pct"] == 0.5 and "surpresa_acima" not in btc5
    assert btc5["fora_das_contas"] == {"sem_medicao": 2}

    recent = initial["ultimas_divulgacoes"]
    assert recent["janelas_min"] == [5, 15]
    assert [i["horario_utc"] for i in recent["itens"]] == [_iso(E3), _iso(E2), _iso(E1)]
    assert recent["itens"][0]["variacao_pct"] == {}  # ainda não medido
    assert recent["itens"][1]["variacao_pct"] == {"EURUSD": [-0.2, -0.2], "BTCUSD": [0.5, 0.5]}
    assert recent["itens"][2]["variacao_pct"] == {"EURUSD": [0.091, 0.091], "XAUUSD": [0.0, 0.0]}
    notes = " ".join(out["observacoes"])
    assert "Medição, não causa" in notes and "Não é previsão" in notes and "DXYm" in notes


def test_event_exactly_at_window_end_does_not_mix_it(tmp_path):
    vals = values()
    vals[3] = val(31, 3, E2 + 5 * M, actual=50, forecast=49)  # PMI exatamente em +5
    store, _, _ = _store(tmp_path, vals, instruments=("EURUSD",))
    store.register()
    windows = store.stats("initial-jobless-claims", windows=[5, 15])["eventos"][0]["reacoes"][0]["janelas"]
    assert "com_outro_evento_na_janela" not in windows["5"] and windows["15"]["com_outro_evento_na_janela"] == 1


def test_stats_filters_symbols_and_days(tmp_path):
    store, _, _ = _store(tmp_path)
    store.register()
    out = store.stats("initial-jobless-claims", symbols=["eurusd", "US30"], days=0.5)
    (event,) = out["eventos"]
    assert event["divulgacoes"] == 2  # E2 e E3 (últimas 12 h)
    assert [r["simbolo"] for r in event["reacoes"]] == ["EURUSD"]
    assert any("US30" in n for n in out["observacoes"])


def test_stats_without_event_lists_inventory(tmp_path):
    store, client, _ = _store(tmp_path)
    store.register()
    _later(client, tmp_path)
    store.register()
    out = store.stats("")
    events = {e["codigo"]: e for e in out["eventos"]}
    assert set(events) == {"initial-jobless-claims", "continuing-jobless-claims", "sp-global-manufacturing-pmi"}
    assert events["initial-jobless-claims"]["divulgacoes"] == 3 and events["initial-jobless-claims"]["com_reacao_medida"] == 2
    assert events["initial-jobless-claims"]["descricao"] == "Pedidos iniciais de seguro-desemprego"
    coverage = {c["simbolo"]: c for c in out["medicoes_por_simbolo"]}
    assert coverage["BTCUSD"]["horarios_medidos"] == 2 and coverage["EURUSD"]["de"] == _iso(E1)
    assert coverage["XAUUSD"]["horarios_medidos"] == 3  # referência antiga ainda é uma medição
    assert out["banco"]["medicoes"] == 8 and out["banco"]["medicoes_a_confirmar"] == 0
    assert out["banco"]["medicoes_nao_confirmaveis"] == 0


def test_exact_code_and_too_many_matches(tmp_path):
    codes = ["consumer-price-index-mm", "consumer-price-index-yy", "consumer-price-index-ex-food-energy-mm",
             "consumer-price-index-ex-food-energy-yy", "consumer-price-index"]
    events = [ev(i + 1, "CPI", code) for i, code in enumerate(codes)]
    vals = [val(10 + i, i + 1, E1, actual=0.3, forecast=0.2) for i in range(len(codes))]
    store, _, _ = _store(tmp_path, vals, events=events)
    store.register()
    exact = store.stats("consumer-price-index-mm")
    assert [e["codigo"] for e in exact["eventos"]] == ["consumer-price-index-mm"]
    assert exact["eventos"][0]["no_mesmo_horario"]["divulgacoes_com_outros_eventos"] == 1
    wide = store.stats("cpi")
    assert len(wide["eventos"]) == 3
    assert any("Outros eventos que também casam" in n for n in wide["observacoes"])


def test_first_and_revised_estimates_are_counted(tmp_path):
    events = [ev(1, "PIB", "gross-domestic-product-qq", frequency="QUARTER")]
    vals = [val(10, 1, E1, actual=2.0, forecast=1.8, revision=1), val(11, 1, E2, actual=2.1, forecast=2.0, revision=2)]
    store, _, _ = _store(tmp_path, vals, events=events)
    store.register()
    out = store.stats("pib")
    assert out["eventos"][0]["estimativas"] == {"primeira": 1, "revisada": 1}
    assert any("`estimativas`" in n for n in out["observacoes"])


def test_stats_errors(tmp_path):
    store, _, _ = _store(tmp_path)
    with pytest.raises(ReacoesError, match="reacoes_registrar"):
        store.stats("claims")
    assert not store._path.exists()  # leitura não cria o banco
    store.register()
    with pytest.raises(ValueError, match="Nenhuma divulgação 'cpi'"):
        store.stats("cpi")
    with pytest.raises(ValueError, match="Janelas guardadas"):
        store.stats("claims", windows=[30])
    for days in (0, 10**9):
        with pytest.raises(ValueError, match="dias"):
            store.stats("claims", days=days)


def test_newer_schema_is_refused(tmp_path):
    store, _, _ = _store(tmp_path)
    store.register()
    with sqlite3.connect(store._path) as conn:
        conn.execute("UPDATE meta SET valor = '99' WHERE chave = 'schema'")
    with pytest.raises(ReacoesError, match="versão mais nova"):
        store.stats("claims")
    with pytest.raises(ReacoesError, match="versão mais nova"):
        store.register()
