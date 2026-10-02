"""Estrutura SMC: regras objetivas sobre candles sintéticos e o relatório com o MT5 simulado."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import fake_mt5 as fm
from fake_mt5 import FakeMT5, make_symbol
from test_mt5_client import make_client
from trading_mcp import smc

UTC = timezone.utc
T0 = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)


def frame(rows: list[tuple[float, float, float, float]], start: datetime = T0, minutes: int = 15) -> pd.DataFrame:
    """Candles (abertura, máxima, mínima, fechamento) a cada ``minutes``."""
    times = [start + timedelta(minutes=minutes * i) for i in range(len(rows))]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df.insert(0, "time", pd.to_datetime(times, utc=True))
    return df


def candle(close: float, prev: float | None = None, wick: float = 0.5) -> tuple[float, float, float, float]:
    """Pavio só do lado do movimento: o candle seguinte não repete a máxima/mínima (sem topos/fundos duplos)."""
    o = close if prev is None else prev
    up = close >= o
    return (o, max(o, close) + (wick if up else 0.0), min(o, close) - (0.0 if up else wick), close)


def path(closes: list[float], wick: float = 0.5) -> list[tuple[float, float, float, float]]:
    rows, prev = [], None
    for c in closes:
        rows.append(candle(c, prev, wick))
        prev = c
    return rows


# ---------------------------------------------------------------- pivôs e estrutura
def test_pivots_confirmed_size_bars_later_by_wick():
    # Sobe até o candle 4 (máxima 14,5), cai até o 9, sobe de novo.
    df = frame(path([10, 11, 12, 13, 14, 13, 12, 11, 10, 9, 10, 11, 12, 13, 14, 15]))
    piv = smc.pivots(df["high"].to_numpy(), df["low"].to_numpy(), 2)
    tops = [(t, p) for t, p, k in piv if k == "topo"]
    bottoms = [(t, p) for t, p, k in piv if k == "fundo"]
    assert (6, 4) in tops  # topo no candle 4, confirmado 2 candles depois
    assert (11, 9) in bottoms


def _zigzag() -> pd.DataFrame:
    # topo A (candle 4, 14,5) → fundo B (candle 9, 8,5) → rompe A por pavio (candle 13, máx 15) sem fechar acima →
    # fecha acima de A no candle 15 (BOS) → cai e rompe B (CHoCH).
    closes = [10, 11, 12, 13, 14, 13, 12, 11, 10, 9, 10, 11, 12, 13.8, 13, 15, 16, 15, 14, 12, 10, 8]
    rows = path(closes)
    rows[13] = (12.0, 15.0, 11.5, 13.8)  # pavio acima de 14,5, fechamento abaixo
    return frame(rows)


def test_break_by_wick_and_by_close_are_separate():
    df = _zigzag()
    piv = smc.pivots(df["high"].to_numpy(), df["low"].to_numpy(), 2)
    wick = smc.structure(df, piv, "pavio")
    close = smc.structure(df, piv, "fechamento")
    summary = lambda s: [(e["tipo"], e["direcao"], e["nivel"], e["pivo_i"], e["i"]) for e in s["eventos"]]  # noqa: E731
    # Fundo 1 (10) rompido para baixo (BOS: sem tendência antes); topo 4 (14,5) rompido para cima contra a
    # tendência (CHoCH): pelo pavio no candle 13, pelo fechamento só no 15. O candle 13 não forma fundo novo (a
    # perna ainda é de alta), então o fundo vigente segue o 9 (8,5), rompido no 21 (CHoCH).
    assert summary(wick)[:2] == [("BOS", "baixa", 10.0, 1, 8), ("CHoCH", "alta", 14.5, 4, 13)]
    assert summary(close) == [("BOS", "baixa", 10.0, 1, 9), ("CHoCH", "alta", 14.5, 4, 15),
                              ("CHoCH", "baixa", 8.5, 9, 21)]
    assert close["tendencia"] == -1


def test_order_block_is_the_extreme_candle_between_pivot_and_break():
    df = _zigzag()
    piv = smc.pivots(df["high"].to_numpy(), df["low"].to_numpy(), 2)
    close = smc.structure(df, piv, "fechamento")
    bull = next(b for b in close["order_blocks"] if b["direcao"] == "alta")
    # Entre o topo (4) e o rompimento (15), a menor mínima é a do candle 9 (8,5); zona de pavio a pavio.
    assert bull["i"] == 9 and (bull["fundo"], bull["topo"]) == (8.5, 10.0)


def test_wide_candle_is_not_chosen_as_order_block():
    df = _zigzag()
    df.loc[9, "low"] = 5.0  # candle 9 vira um candle enorme (amplitude 5,5)
    piv = smc.pivots(df["high"].to_numpy(), df["low"].to_numpy(), 2)
    atr = np.full(len(df), 2.0)  # 2 ATR = 4: o candle 9 fica de fora
    bull = next(b for b in smc.structure(df, piv, "fechamento", atr)["order_blocks"] if b["direcao"] == "alta")
    assert bull["i"] != 9


def test_order_block_mitigated_by_close_and_touched_by_wick():
    block = {"direcao": "alta", "criado_i": 1, "topo": 10.0, "fundo": 9.0}
    df = frame([(11, 11, 11, 11), (11, 11, 11, 11), (11, 11.2, 9.8, 10.5), (10.5, 10.6, 8.8, 9.5), (9.5, 9.6, 8.5, 8.9)])
    status = smc.block_status(df, block)
    assert status == {"tocado_i": 2, "mitigado_i": 4}


# ---------------------------------------------------------------- FVG, liquidez, varredura
def test_fair_value_gap_bullish_and_its_fill():
    # Corpos pequenos e depois um candle forte: espaço entre a máxima do candle 3 (10,1) e a mínima do 5 (11).
    rows = [(10, 10.1, 9.9, 10.0)] * 4 + [(10.0, 11.5, 9.95, 11.4), (11.4, 12.0, 11.0, 11.8), (11.8, 11.9, 10.5, 10.6),
                                         (10.6, 10.7, 9.9, 10.0)]
    gap, down = smc.fair_value_gaps(frame(rows))
    assert down["preenchido_i"] is None and down["preenchido_pct"] == 0.0  # nada voltou acima de 10,7
    assert (gap["direcao"], gap["i"], gap["fundo"], gap["topo"]) == ("alta", 4, 10.1, 11.0)
    assert gap["preenchido_i"] == 7 and gap["preenchido_pct"] == 100.0
    # A queda forte do candle 6 deixa um FVG de baixa entre a mínima do 5 (11) e a máxima do 7 (10,7).
    assert (down["direcao"], down["i"], down["fundo"], down["topo"]) == ("baixa", 6, 10.7, 11.0)


def test_fair_value_gap_needs_a_strong_middle_candle():
    rows = [(10, 11, 9, 10.5)] * 4 + [(10.0, 10.6, 9.95, 10.4), (10.4, 12.0, 10.7, 11.8)]
    assert smc.fair_value_gaps(frame(rows)) == []  # corpo do meio menor que o dobro do corpo médio


def test_equal_highs_within_a_tenth_of_atr():
    # A perna começa como de baixa: um fundo antes para o primeiro topo contar.
    closes = [12, 11, 10, 11, 12, 11, 10, 9, 10, 11, 12.02, 11, 10, 9, 8]
    df = frame(path(closes, wick=0.1))
    pools = smc.equal_levels(df, np.full(len(df), 1.0))
    assert [(p["tipo"], p["pontos_i"]) for p in pools] == [("topos_iguais", [4, 10])]
    assert pools[0]["nivel"] == pytest.approx(12.12)
    assert smc.equal_levels(df, np.full(len(df), 0.1)) == []  # 0,02 não é menor que 0,1 × 0,1


def test_three_equal_highs_make_one_group_and_equal_lows_work():
    closes = [12, 11, 10, 11, 12, 11, 10, 11, 12.01, 11, 10, 11, 12.02, 11, 10, 11, 10.02, 11, 12, 13]
    df = frame(path(closes, wick=0.1))
    pools = smc.equal_levels(df, np.full(len(df), 1.0))
    highs = [p for p in pools if p["tipo"] == "topos_iguais"]
    assert len(highs) == 1 and highs[0]["pontos_i"] == [4, 8, 12] and highs[0]["nivel"] == pytest.approx(12.12)
    lows = [p for p in pools if p["tipo"] == "fundos_iguais"]
    assert lows and lows[0]["nivel"] == pytest.approx(9.9)


def test_crossings_report_sweep_and_the_later_break():
    df = frame([(10, 10.5, 9.5, 10), (10, 11.2, 9.8, 10.6), (10.6, 11.6, 10.4, 11.4)])
    # 11,0: pavio além no candle 1 e fechamento de volta (varredura); fecha além no 2 (rompimento depois).
    assert smc.crossings(df, 11.0, "acima", 0) == {"varredura_i": 1, "rompimento_i": 2}
    assert smc.crossings(df, 11.3, "acima", 0) == {"varredura_i": None, "rompimento_i": 2}  # rompeu direto
    assert smc.crossings(df, 12.0, "acima", 0) == {"varredura_i": None, "rompimento_i": None}
    assert smc.crossings(df, 9.7, "abaixo", 0) == {"varredura_i": 0, "rompimento_i": None}


def test_outside_bar_by_wick_orders_breaks_by_candle_colour():
    # Topo em 12,5 e fundo em 8,5 vigentes; o último candle passa os dois pelo pavio.
    closes = [10, 9, 8.9, 10, 11, 12, 11, 10, 9.5, 10, 10.5]
    rows = path(closes)
    rows.append((10.5, 13.0, 8.0, 12.0))  # candle de alta: desceu antes de subir → tendência final de alta
    df = frame(rows)
    piv = smc.pivots(df["high"].to_numpy(), df["low"].to_numpy(), 2)
    wick = smc.structure(df, piv, "pavio")
    last_two = [e["direcao"] for e in wick["eventos"] if e["i"] == len(df) - 1]
    assert last_two == ["baixa", "alta"] and wick["tendencia"] == 1


# ---------------------------------------------------------------- dia, semana e sessões
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 2, 12, 0, tzinfo=UTC), datetime(2026, 10, 1, 21, 0, tzinfo=UTC)),  # EDT
        (datetime(2026, 10, 2, 21, 0, tzinfo=UTC), datetime(2026, 10, 2, 21, 0, tzinfo=UTC)),  # na virada
        (datetime(2026, 11, 3, 12, 0, tzinfo=UTC), datetime(2026, 11, 2, 22, 0, tzinfo=UTC)),  # depois do fim do EDT
        (datetime(2026, 11, 2, 21, 30, tzinfo=UTC), datetime(2026, 11, 1, 22, 0, tzinfo=UTC)),  # 16:30 em NY (EST)
    ],
)
def test_market_day_starts_at_17_new_york(moment, expected):
    assert smc.market_day_start(moment) == expected


def test_market_week_starts_sunday_17_new_york():
    sunday_open = datetime(2026, 9, 27, 21, 0, tzinfo=UTC)
    assert smc.market_week_start(datetime(2026, 9, 30, 12, 0, tzinfo=UTC)) == sunday_open  # quarta
    assert smc.market_week_start(datetime(2026, 10, 3, 12, 0, tzinfo=UTC)) == sunday_open  # sábado
    assert smc.market_week_start(datetime(2026, 10, 4, 21, 30, tzinfo=UTC)) == datetime(2026, 10, 4, 21, 0, tzinfo=UTC)


def test_sessions_follow_each_city_daylight_saving():
    summer = {n: (s, e) for n, s, e in smc.session_windows(datetime(2026, 10, 1, 21, 0, tzinfo=UTC))}
    assert summer["asia"] == (datetime(2026, 10, 2, 0, tzinfo=UTC), datetime(2026, 10, 2, 7, tzinfo=UTC))
    assert summer["londres"] == (datetime(2026, 10, 2, 7, tzinfo=UTC), datetime(2026, 10, 2, 12, tzinfo=UTC))
    assert summer["nova_york"] == (datetime(2026, 10, 2, 12, tzinfo=UTC), datetime(2026, 10, 2, 21, tzinfo=UTC))
    winter = {n: (s, e) for n, s, e in smc.session_windows(datetime(2026, 11, 2, 22, 0, tzinfo=UTC))}
    assert winter["londres"] == (datetime(2026, 11, 3, 8, tzinfo=UTC), datetime(2026, 11, 3, 13, tzinfo=UTC))
    assert winter["nova_york"][1] == datetime(2026, 11, 3, 22, tzinfo=UTC)
    # 20/03/2026: Nova York já em horário de verão (desde 08/03), Londres ainda não (só em 29/03).
    march = {n: (s, e) for n, s, e in smc.session_windows(datetime(2026, 3, 19, 21, 0, tzinfo=UTC))}
    assert march["londres"][0] == datetime(2026, 3, 20, 8, tzinfo=UTC)
    assert march["nova_york"][0] == datetime(2026, 3, 20, 12, tzinfo=UTC)


def _m5(start: datetime, end: datetime, price) -> pd.DataFrame:
    rows, times, t = [], [], start
    while t < end:
        if t.weekday() != 5 and not (t.weekday() == 6 and t.hour < 21) and not (t.weekday() == 4 and t.hour >= 21):
            o, h, l, c = price(t)
            rows.append((o, h, l, c))
            times.append(t)
        t += timedelta(minutes=5)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df.insert(0, "time", pd.to_datetime(times, utc=True))
    return df


def test_key_levels_skip_the_weekend_and_mark_session_sweeps():
    now = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)  # segunda 04:00 em NY

    def price(t):
        if t < datetime(2026, 10, 2, 21, tzinfo=UTC):
            return (100, 101, 99, 100)  # semana anterior: 99–101
        if t < datetime(2026, 10, 5, 7, tzinfo=UTC):
            return (100, 100.5, 99.5, 100)  # Ásia de segunda: 99,5–100,5
        if t == datetime(2026, 10, 5, 7, 30, tzinfo=UTC):
            return (100, 101.5, 99.8, 100.2)  # Londres varre a máxima da Ásia e a de sexta
        return (100, 100.4, 99.8, 100)

    m5 = _m5(datetime(2026, 9, 27, 21, tzinfo=UTC), now, price)
    d1 = frame(path([90, 95, 105, 100, 98, 99]), start=datetime(2026, 9, 27, tzinfo=UTC), minutes=1440)
    levels = smc.key_levels(m5, d1, 2, now, 100.0)
    prev = levels["dia_mercado_anterior"]
    assert (prev["de"], prev["ate"]) == ("2026-10-01T21:00:00Z", "2026-10-02T21:00:00Z")  # sexta, pulando o fim de semana
    assert prev["maxima_varrida_em"] == "2026-10-05T07:30:00Z" and "maxima_rompida_em" not in prev
    assert levels["sessoes_dia_anterior"]["londres"]["situacao"] == "concluida"
    today = levels["sessoes_hoje"]
    assert today["asia"]["maxima"] == 100.5 and today["asia"]["maxima_varrida_em"] == "2026-10-05T07:30:00Z"
    assert today["londres"]["situacao"] == "em_andamento" and "maxima_varrida_em" not in today["londres"]
    assert today["nova_york"]["situacao"] == "ainda_nao_comecou"
    assert levels["semana_mercado_anterior"]["de"] == "2026-09-27T21:00:00Z"
    assert levels["semana_mercado_atual"]["maxima"] == 101.5 and levels["dia_mercado_atual"]["maxima"] == 101.5
    assert not any(v.get("incompleto") for v in levels.values() if isinstance(v, dict))


def test_on_saturday_the_current_week_is_the_one_that_just_closed():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # sábado

    def price(t):
        return (100, 102, 98, 100) if t >= datetime(2026, 9, 27, 21, tzinfo=UTC) else (100, 101, 99, 100)

    m5 = _m5(datetime(2026, 9, 20, 21, tzinfo=UTC), now, price)
    levels = smc.key_levels(m5, m5.iloc[:0], 2, now, 100.0)
    assert levels["semana_mercado_atual"]["de"] == "2026-09-27T21:00:00Z"
    assert (levels["semana_mercado_atual"]["maxima"], levels["semana_mercado_atual"]["minima"]) == (102, 98)
    assert levels["semana_mercado_anterior"]["de"] == "2026-09-20T21:00:00Z"
    assert levels["dia_mercado_anterior"]["de"] == "2026-10-01T21:00:00Z"  # quinta 17h → sexta 17h


def test_period_older_than_the_m5_history_is_flagged():
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    m5 = _m5(datetime(2026, 9, 24, 0, tzinfo=UTC), now, lambda t: (100, 101, 99, 100))  # começa no meio da semana
    levels = smc.key_levels(m5, m5.iloc[:0], 2, now, 100.0)
    assert levels["semana_mercado_anterior"]["incompleto"] is True
    assert "incompleto" not in levels["dia_mercado_anterior"]


def test_daily_swings_keep_only_intact_levels():
    # Fundo em 90 (dia 1), topo em 105,5 (dia 2) e fundo em 97,5 (dia 4), todos intactos; mais um dia abaixo de
    # 97,5 o varre e ele sai da lista.
    closes = [90, 95, 105, 100, 98, 99, 101, 100]
    d1 = frame(path(closes), start=datetime(2026, 9, 1, tzinfo=UTC), minutes=1440)
    swings = smc.daily_swings(d1, 2, 100.0)
    assert [(s["tipo"], s["preco"]) for s in swings] == [("topo", 105.5), ("fundo", 97.5), ("fundo", 90.0)]
    assert swings[0]["dia"] == "2026-09-03" and swings[0]["distancia_pct"] == 5.5
    swept = frame(path(closes + [97]), start=datetime(2026, 9, 1, tzinfo=UTC), minutes=1440)
    assert ("fundo", 97.5) not in [(s["tipo"], s["preco"]) for s in smc.daily_swings(swept, 2, 97.0)]


def test_daily_swing_passed_by_todays_candle_or_by_price_is_not_intact():
    closes = [90, 95, 105, 100, 98, 99, 101, 100]
    today = [(100.0, 106.0, 99.8, 104.0)]  # candle de hoje (aberto) passa o topo de 105,5
    d1 = frame(path(closes) + today, start=datetime(2026, 9, 1, tzinfo=UTC), minutes=1440)
    kinds = [(s["tipo"], s["preco"]) for s in smc.daily_swings(d1, 2, 104.0, has_forming=True)]
    assert ("topo", 105.5) not in kinds and ("fundo", 97.5) in kinds
    plain = frame(path(closes), start=datetime(2026, 9, 1, tzinfo=UTC), minutes=1440)
    assert ("topo", 105.5) not in [(s["tipo"], s["preco"]) for s in smc.daily_swings(plain, 2, 106.0)]


def test_sunday_stub_joins_monday_for_symbols_closed_on_saturday():
    start = datetime(2026, 9, 6, tzinfo=UTC)  # domingo
    rows = [(100, 101, 99.5, 100.5), (100.5, 103, 98, 102), (102, 104, 101, 103)]
    merged = smc._merge_sundays(frame(rows, start=start, minutes=1440))
    assert len(merged) == 2 and merged["time"].iloc[0].weekday() == 0
    assert (merged["open"].iloc[0], merged["high"].iloc[0], merged["low"].iloc[0]) == (100, 103, 98)
    btc = frame(rows * 3, start=datetime(2026, 9, 1, tzinfo=UTC), minutes=1440)  # tem sábado: fica igual
    assert len(smc._merge_sundays(btc)) == 9


# ---------------------------------------------------------------- relatório
def _random_walk(n: int, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    closes = 30000 + np.cumsum(rng.normal(0, 8, n))
    start = int((datetime(2026, 9, 30, 12, tzinfo=UTC) - timedelta(minutes=5 * n)).timestamp())
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        rows.append((start + 300 * i, prev, max(prev, c) + 3, min(prev, c) - 3, c, 10, 112, 0))
        prev = c
    return np.array(rows, dtype=fm.RATES_DTYPE)


def test_report_with_fake_terminal():
    index = make_symbol("USTECm", digits=2, point=0.01, bid=30000.0, ask=30001.12, trade_calc_mode=fm.SYMBOL_CALC_MODE_CFD,
                        currency_base="USD", currency_profit="USD")
    client, _ = make_client(FakeMT5([index], rates={"USTECm": _random_walk(4100)}))
    out = smc.report(client, "USTEC", ["m15", "H1"])
    assert out["simbolo"] == "USTECm" and set(out["timeframes"]) == {"M15", "H1"}
    frame_out = out["timeframes"]["M15"]
    assert set(frame_out["estrutura"]) == {"micro", "macro"}
    assert {"tendencia_por_fechamento", "tendencia_por_pavio", "topo_atual", "fundo_atual",
            "ultimos_rompimentos"} <= set(frame_out["estrutura"]["micro"])
    assert {"fvg_abertos", "order_blocks", "liquidez_igual", "varreduras_recentes"} <= set(frame_out)
    assert {"sessoes_hoje", "dia_mercado_atual", "semana_mercado_atual"} <= set(out["niveis"])
    assert any("não sinal" in n for n in out["observacoes"])
    for item in frame_out["estrutura"]["micro"]["ultimos_rompimentos"]:
        assert item["por_pavio"] or item["por_fechamento"]


def test_report_rejects_unknown_timeframe():
    client, _ = make_client(FakeMT5([make_symbol("EURUSD")], rates={"EURUSD": _random_walk(300)}))
    with pytest.raises(ValueError, match="Timeframes aceitos"):
        smc.report(client, "EURUSD", ["M2"])


def test_premium_discount_range_follows_a_breakout():
    df = frame(path([10, 11, 12, 11, 10, 11, 13, 14]))
    macro = {"topo": {"nivel": 12.5, "i": 2}, "fundo": {"nivel": 9.5, "i": 4}}
    fmt = smc._Fmt(df, 2, 13.0)
    zone = smc._premium_discount(df, macro, 13.0, fmt)
    # Depois do topo, a máxima chegou a 14,5: a faixa vai de 9,5 a 14,5 e 13 fica em premium.
    assert zone == {"faixa": [9.5, 14.5], "posicao_pct": 70.0, "zona": "premium"}
    assert smc._premium_discount(df, macro, 15.0, fmt)["zona"] == "acima_da_faixa"
