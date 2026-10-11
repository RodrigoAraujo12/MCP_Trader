"""Painel: desenhos do gráfico (mesmas regras do smc.py) e a API local só de leitura, com o MT5 simulado."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from starlette.testclient import TestClient

import fake_mt5 as fm
from fake_mt5 import FakeMT5, make_position, make_symbol
from test_mt5_client import make_client
from test_smc import frame, path
from trading_mcp import indicators, journal as journal_module, smc
from trading_mcp.config import Settings
from trading_mcp.painel import app as painel_app, graficos

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # o mesmo do FakeClock


def _walk(n: int, step_s: int = 300, seed: int = 7, end: datetime = NOW) -> np.ndarray:
    """Candles com o último abrindo ``step_s`` antes de ``end`` (fechado no relógio falso)."""
    rng = np.random.default_rng(seed)
    closes = 30000 + np.cumsum(rng.normal(0, 8, n))
    start = int((end - timedelta(seconds=step_s * n)).timestamp())
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        rows.append((start + step_s * i, prev, max(prev, c) + 3, min(prev, c) - 3, c, 10, 112, 0))
        prev = c
    return np.array(rows, dtype=fm.RATES_DTYPE)


def _index_client(n: int = 4100, **fake_kwargs):
    index = make_symbol("USTECm", digits=2, point=0.01, bid=30000.0, ask=30001.12,
                        trade_calc_mode=fm.SYMBOL_CALC_MODE_CFD, currency_base="USD", currency_profit="USD")
    client, fake = make_client(FakeMT5([index], rates={"USTECm": _walk(n)}, **fake_kwargs))
    return client, fake


# ---------------------------------------------------------------- desenhos
def test_chart_indices_point_into_the_candles_sent():
    client, _ = _index_client()
    out = graficos.chart(client, "USTEC", "m5", 600)
    assert out["simbolo"] == "USTECm" and out["timeframe"] == "M5" and out["segundos_por_candle"] == 300
    candles = out["candles"]
    assert len(candles) == 600
    times = [c["time"] for c in candles]
    assert times == sorted(times) and len(set(times)) == len(times)
    n = len(candles)
    s = out["smc"]
    assert {"tendencia", "rompimentos", "topos_fundos", "order_blocks", "fvg", "liquidez", "varreduras"} == set(s)
    for r in s["rompimentos"]:
        assert 0 <= r["de_i"] < r["ate_i"] < n and r["tipo"] in ("BOS", "CHoCH")
    for z in s["order_blocks"] + s["fvg"]:
        assert 0 <= z["i"] < n and z["de"] <= z["ate"]
    for l in s["liquidez"]:
        assert all(0 <= i < n for i in l["pontos_i"]) and l["tipo"] in ("EQH", "EQL")
    for v in s["varreduras"]:
        assert 0 <= v["origem_i"] < v["i"] < n
    for box in out["sessoes"]:
        assert 0 <= box["de_i"] <= box["ate_i"] < n and box["minima"] <= box["maxima"]


def test_breaks_and_blocks_follow_the_smc_rules():
    """O gráfico desenha os mesmos rompimentos por fechamento e order blocks ativos que o smc.py mede."""
    client, _ = _index_client()
    df = client.rates("USTEC", "M5", 1000, include_current=False)
    out = graficos.structure(df, 2, price=None)
    high, low = df["high"].to_numpy(float), df["low"].to_numpy(float)
    atr = indicators.atr(df["high"], df["low"], df["close"], smc.ATR_PERIOD).to_numpy(float)
    micro = smc.structure(df, smc.pivots(high, low, smc.MICRO), "fechamento", atr)
    expected = [(e["pivo_i"], e["i"], e["tipo"]) for e in micro["eventos"][-graficos.MAX_BREAKS["micro"]:]]
    got = [(r["de_i"], r["ate_i"], r["tipo"]) for r in out["rompimentos"] if r["estrutura"] == "micro"]
    assert got == expected
    active = {b["i"] for b in micro["order_blocks"] if smc.block_status(df, b)["mitigado_i"] is None}
    assert {z["i"] for z in out["order_blocks"] if z["estrutura"] == "micro"} <= active
    assert out["tendencia"]["micro"] == {1: "alta", -1: "baixa"}.get(micro["tendencia"], "indefinida")


def test_swing_labels_compare_with_the_previous_swing_of_the_same_kind():
    # Zigue-zague com pivôs de 1 candle: fundos 4, 5, 6, 3 e topos 10, 12, 11 (o primeiro de cada tipo não tem rótulo).
    highs = np.array([5, 10, 6, 12, 7, 11, 4, 9.0])
    lows = highs - 1
    labels = [(s["tipo"], s["rotulo"]) for s in graficos._swing_labels(highs, lows, 1)]
    assert labels == [("fundo", "HL"), ("topo", "HH"), ("fundo", "HL"), ("topo", "LH"), ("fundo", "LL")]


def test_closest_zones_keep_the_one_containing_price_and_the_nearest_each_side():
    zones = [{"de": lo, "ate": lo + 1, "i": i} for i, lo in enumerate([90, 95, 99.5, 103, 110])]
    picked = graficos._closest(zones, 100.0, per_side=1)
    assert [z["de"] for z in picked] == [99.5, 103, 95]
    assert [z["i"] for z in graficos._closest(zones, None, per_side=1)] == [3, 4]


def test_session_boxes_cover_each_city_window():
    # Um dia de mercado em M15 (terça 29/09/2026, horário de verão em Londres e Nova York).
    start = smc.market_day_start(datetime(2026, 9, 29, 12, tzinfo=UTC))
    rows = path([100 + (i % 7) for i in range(96)])
    df = frame(rows, start=start, minutes=15)
    boxes = graficos.sessions(df, 2, now=start + timedelta(hours=24), days=1)
    assert [b["sessao"] for b in boxes] == ["asia", "londres", "nova_york"]
    windows = smc.session_windows(start)
    for box, (_, s_at, e_at) in zip(boxes, windows):
        assert df["time"].iloc[box["de_i"]] == s_at
        assert df["time"].iloc[box["ate_i"]] < e_at and not box["em_andamento"]
        window = df.iloc[box["de_i"]:box["ate_i"] + 1]
        assert box["maxima"] == round(window["high"].max(), 2) and box["minima"] == round(window["low"].min(), 2)


def test_chart_rejects_unknown_timeframe():
    client, _ = _index_client(500)
    with pytest.raises(ValueError, match="Timeframes aceitos"):
        graficos.chart(client, "USTEC", "W1")


# ---------------------------------------------------------------- API
@pytest.fixture
def api():
    client, fake = _index_client()
    app = painel_app.create_app(Settings(instruments=("USTEC", "XAUUSD")), client)
    return TestClient(app, base_url="http://127.0.0.1"), fake


def test_api_config_and_page(api):
    http, _ = api
    assert http.get("/api/config").json() == {"instrumentos": ["USTEC", "XAUUSD"], "timeframes": list(smc.TIMEFRAMES)}
    page = http.get("/")
    assert page.status_code == 200 and "Painel MCP Trader" in page.text
    assert http.get("/static/painel.js").status_code == 200


def test_api_chart_structure_and_live(api):
    http, _ = api
    chart = http.get("/api/grafico", params={"simbolo": "USTEC", "tf": "M15", "candles": 300})
    assert chart.status_code == 200 and chart.headers["cache-control"] == "no-store"
    assert len(chart.json()["candles"]) == 300
    structure = http.get("/api/estrutura", params={"simbolo": "USTEC", "tf": "M3"}).json()
    assert set(structure["timeframes"]) == {"H4", "H1", "M15", "M5", "M3"}
    assert "observacoes" not in structure and isinstance(structure["avisos"], list)
    assert not any(n in smc.NOTES for n in structure["avisos"])
    live = http.get("/api/ao_vivo", params={"simbolo": "USTEC", "tf": "M5"}).json()
    assert live["cotacao"]["simbolo"] == "USTECm" and len(live["candles"]) == 2
    assert live["ultimo_em_formacao"] is False and live["fecha_em"] is None


def test_api_risk_returns_limits_and_positions_together():
    position = make_position(9, "USTECm", "buy", 1.0, 29990.0, 30000.0, sl=29950.0, tp=30100.0, profit=10.0)
    client, _ = _index_client(500, positions=[position])
    http = TestClient(painel_app.create_app(Settings(), client), base_url="http://127.0.0.1")
    out = http.get("/api/risco").json()
    assert set(out) == {"limites", "posicoes"}
    assert "observacoes" not in out["limites"] and "observacoes" not in out["posicoes"]
    assert [p["ticket"] for p in out["posicoes"]["posicoes"]] == [9]
    assert out["posicoes"]["posicoes"][0]["stop"]["preco"] == 29950.0
    assert out["limites"]["exposicao_aberta"]["posicoes"] == 1
    assert out["limites"]["regras"]["dia_pct"] == Settings().daily_loss_pct


def _journal_api(tmp_path, sync: bool = True):
    from test_journal import _journal

    journal, client, _ = _journal(tmp_path)
    if sync:
        journal.sync(7)
        journal.annotate(ticket=100, setup="OB")
    app = painel_app.create_app(Settings(), client, journal=journal)
    return TestClient(app, base_url="http://127.0.0.1"), journal


def test_api_journal_matches_the_stats_and_lists_closed_trades_in_order(tmp_path):
    http, journal = _journal_api(tmp_path)
    out = http.get("/api/journal").json()
    assert out["estatisticas"]["geral"] == journal.stats()["geral"]
    # Fechadas, da mais antiga para a mais nova pelo fechamento: a 200 fechou há 2,5 h e a 100 há 1 h.
    assert [o["ticket"] for o in out["operacoes"]] == [200, 100]
    assert out["operacoes"][1]["setup"] == "OB" and out["operacoes"][1]["r"] == 0.19
    assert set(out["operacoes"][0]) == set(painel_app._OPERATION_FIELDS)
    assert out["simbolos_existentes"] == ["EURUSD"] and out["setups_existentes"] == ["OB"]
    assert out["abertas_ou_parciais"] == 1
    assert "observacoes" not in out["estatisticas"]
    assert out["estatisticas"]["avisos"] and not set(out["estatisticas"]["avisos"]) & set(journal_module.STATS_NOTES)


def test_api_journal_filters_agree_with_the_stats(tmp_path):
    http, _ = _journal_api(tmp_path)
    for params in ({"dias": "0.1"}, {"setup": "ob"}, {"simbolo": "eur"}, {"simbolo": "GBP"}):
        out = http.get("/api/journal", params=params).json()
        assert len(out["operacoes"]) == out["estatisticas"]["geral"]["operacoes"], params
    assert [o["ticket"] for o in http.get("/api/journal", params={"dias": "0.1"}).json()["operacoes"]] == [100]
    assert http.get("/api/journal", params={"dias": "abc"}).status_code == 400
    assert http.get("/api/journal", params={"dias": "0"}).status_code == 400


def test_api_journal_empty_explains_and_creates_nothing(tmp_path):
    http, journal = _journal_api(tmp_path, sync=False)
    out = http.get("/api/journal").json()
    assert out["vazio"] is True and "journal_sincronizar" in out["mensagem"]
    assert not journal._path.exists()


def test_api_errors_become_messages(api):
    http, _ = api
    missing = http.get("/api/grafico", params={"simbolo": "NOPE", "tf": "M5"})
    assert missing.status_code == 404 and "não encontrado" in missing.json()["erro"]
    assert http.get("/api/grafico", params={"simbolo": "USTEC", "tf": "W1"}).status_code == 400
    assert http.get("/api/grafico").status_code == 400


def test_api_only_answers_on_localhost(api):
    _, fake = api
    other = TestClient(painel_app.create_app(Settings(), make_client(fake)[0]), base_url="http://evil.example")
    assert other.get("/api/config").status_code == 400


def test_api_has_no_write_routes():
    """O painel só lê: enviar ordem continua só pelo chat, com a janela de aprovação do Windows."""
    client, _ = _index_client(500)
    app = painel_app.create_app(Settings(), client)
    for route in app.routes:
        methods = getattr(route, "methods", None)
        assert methods is None or methods <= {"GET", "HEAD"}, route.path


def test_json_is_valid_for_the_browser():
    cleaned = painel_app._clean({"a": float("nan"), "b": [np.float64(1.5), np.int64(2), float("inf")]})
    assert cleaned == {"a": None, "b": [1.5, 2, None]}
    assert type(cleaned["b"][0]) is float and type(cleaned["b"][1]) is int
