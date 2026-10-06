"""Backtest: execução da ordem, stop/alvo, regras conservadoras dentro do candle, lote pelos limites e relatório."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from test_setup_a import NO_PD, SPREAD, Bars, buy_setup, ny, to_h1
from trading_mcp import backtest
from trading_mcp.backtest import Data, Simulator, Spec, stats, verdict
from trading_mcp.setup_a import Params

UTC = timezone.utc
SPEC = Spec(symbol="TESTm", point=0.01, digits=2, money_per_unit=100.0, vol_min=0.01, vol_step=0.01, vol_max=100.0)
# Sinal da compra: entrada 99,70, stop 98,70 (1,0 de distância), alvo 103,0; lote = 125 / 100 = 1,25.


def sim(b: Bars, *, m1: pd.DataFrame | None = None, params: Params = NO_PD, **kw) -> Simulator:
    df = b.frame()
    return Simulator(Data(SPEC, "M15", df, to_h1(df), m1), params, **kw)


def only_trade(s: Simulator) -> dict:
    res = s.run()
    assert len(res.trades) == 1, (res.trades, res.cancels, res.refusals)
    return res.trades[0]


def test_fill_then_target():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 99.95, 99.55, 99.8)  # ask mínimo 99,65: executa em 99,70
    b.add("2026-06-02 02:45", 99.8, 103.2, 99.75, 103.1)  # bid chega a 103
    t = only_trade(sim(b))
    assert t["executada"] == pytest.approx(99.70) and t["saida"] == pytest.approx(103.0)
    assert t["motivo"] == "alvo" and t["lotes"] == pytest.approx(1.25)
    assert t["resultado"] == pytest.approx(3.3 * 1.25 * 100)
    assert t["r"] == pytest.approx(3.3)
    assert t["executada_utc"] == "2026-06-02T06:30:00Z" and t["sessao"] == "londres"


def test_buy_limit_needs_the_ask():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 99.95, 99.62, 99.8)  # bid 99,62 -> ask 99,72: não executa
    res = sim(b).run()
    assert res.trades == [] and res.cancels["prazo"] == 1


def test_fill_and_stop_in_the_same_candle_is_a_loss():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 99.95, 98.6, 99.0)
    t = only_trade(sim(b))
    assert t["motivo"] == "stop" and t["saida"] == pytest.approx(98.70) and t["r"] == pytest.approx(-1.0)


def test_gap_through_entry_and_stop_exits_at_the_open():
    b = buy_setup()
    b.add("2026-06-02 02:30", 98.5, 98.6, 98.4, 98.5)
    t = only_trade(sim(b))
    assert t["executada"] == pytest.approx(98.6)  # ask da abertura
    assert t["saida"] == pytest.approx(98.5) and t["resultado"] < 0


def test_fill_and_target_in_the_same_candle_cancels_without_m1():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 103.5, 99.55, 103.2)
    res = sim(b).run()
    assert res.trades == [] and res.cancels["alvo_antes_da_execucao"] == 1


def _m1(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    df = pd.DataFrame([r[1:] for r in rows], columns=["open", "high", "low", "close"])
    df.insert(0, "time", pd.to_datetime([ny(r[0]) for r in rows], utc=True))
    df["spread"] = SPREAD
    return df


def test_m1_decides_the_order_inside_the_candle():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 103.5, 99.55, 103.2)
    m1 = _m1([("2026-06-02 02:30", 99.9, 99.95, 99.55, 99.8)] +
             [(f"2026-06-02 02:{31 + k}", 99.8 + k * 0.3, 100.1 + k * 0.3, 99.75 + k * 0.3, 100.1 + k * 0.3)
              for k in range(12)] +
             [("2026-06-02 02:43", 103.1, 103.5, 103.0, 103.2), ("2026-06-02 02:44", 103.2, 103.3, 103.1, 103.2)])
    s = sim(b, m1=m1)
    t = only_trade(s)
    assert t["motivo"] == "alvo" and t["resolucao"] == "M1" and s.m1_used >= 1


def test_m1_that_does_not_match_the_candle_is_ignored():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 103.5, 99.55, 103.2)
    m1 = _m1([("2026-06-02 02:30", 99.9, 99.95, 99.55, 99.8)])  # máxima não bate com o M15
    res = sim(b, m1=m1).run()
    assert res.trades == [] and res.cancels["alvo_antes_da_execucao"] == 1


def test_target_and_stop_in_the_same_candle_is_a_stop():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 99.95, 99.55, 99.8)
    b.add("2026-06-02 02:45", 99.8, 103.5, 98.5, 100.0)
    t = only_trade(sim(b))
    assert t["motivo"] == "stop" and t["r"] == pytest.approx(-1.0)


def test_order_expires_after_an_hour():
    b = buy_setup()
    b.add("2026-06-02 03:30", 99.9, 99.95, 99.4, 99.8)  # chega à entrada, mas depois de 1 h
    res = sim(b).run()
    assert res.trades == [] and res.cancels["prazo"] == 1


def test_news_cancels_the_pending_order():
    b = buy_setup()
    b.add("2026-06-02 03:00", 99.9, 99.95, 99.4, 99.8)
    res = sim(b, news=[ny("2026-06-02 03:20").to_pydatetime()]).run()
    assert res.trades == [] and res.cancels["noticia"] == 1


def test_position_is_closed_at_16_45_new_york():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 99.95, 99.55, 99.8)
    t = only_trade(sim(b))
    assert t["motivo"] == "horario" and t["saida_utc"] == "2026-06-02T20:45:00Z"
    assert t["saida"] == pytest.approx(100.0)  # fechamento (bid) do candle das 16:30


def test_hole_in_the_data_before_16_45_exits_at_the_last_known_price():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 99.95, 99.55, 99.8)
    for t in list(b.rows):
        if ny("2026-06-02 14:00") <= t < ny("2026-06-02 17:00"):
            del b.rows[t]  # corretora sem candles das 14:00 à pausa
    b.add("2026-06-02 13:45", 100.0, 100.3, 99.9, 100.2)
    b.flat("2026-06-02 18:00", "2026-06-02 20:00", price=95.0)  # reabre bem mais baixo
    t = only_trade(sim(b))
    assert t["motivo"] == "sem_dados_ate_o_horario"
    assert t["saida"] == pytest.approx(100.2) and t["saida_utc"] == "2026-06-02T18:00:00Z"


def test_breakeven_variant():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 99.95, 99.55, 99.8)
    b.add("2026-06-02 02:45", 99.8, 100.8, 99.75, 100.7)  # +1R (100,70)
    b.add("2026-06-02 03:00", 100.7, 100.75, 99.6, 99.7)  # volta à entrada
    t = only_trade(sim(b, params=Params(premium_discount=False, breakeven_r=1.0)))
    assert t["motivo"] == "zero" and t["resultado"] == pytest.approx(0.0)


def test_size_follows_the_user_limits():
    s = sim(buy_setup())
    sig = type("S", (), {"entry": 99.70, "stop": 98.70})()
    assert s._size(sig) == pytest.approx((1.25, 125.0))
    s.day_result = -450.0  # faltam 50 para -5% do dia
    assert s._size(sig) == pytest.approx((0.5, 50.0))
    s.day_result = -500.0
    assert s._size(sig) == "limite_dia"
    s.day_result, s.week_result = 0.0, -2500.0
    assert s._size(sig) == "limite_semana"
    s.week_result = 0.0
    far = type("S", (), {"entry": 200.0, "stop": 0.0})()  # 0,01 lote arriscaria 200, mais que 125
    assert s._size(far) == "lote_minimo"


def test_signals_outside_the_period_are_ignored():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 99.95, 99.55, 99.8)
    res = sim(b, start=datetime(2026, 6, 3, tzinfo=UTC)).run()
    assert res.trades == [] and res.signals == 0


# ---------------------------------------------------------------- relatório
def _trade(r: float, week: int, before: float) -> dict:
    return {"resultado": r * 100, "r": r, "saldo_antes": before, "saldo_depois": before + r * 100,
            "semana": week, "base_semana": 10_000.0}


def test_stats_and_verdict():
    trades, bal = [], 10_000.0
    for r, w in ((-1, 1), (-1, 1), (3, 2), (-1, 2), (2, 3)):
        trades.append(_trade(r, w, bal))
        bal += r * 100
    s = stats(trades)
    assert s["operacoes"] == 5 and s["ganhos"] == 2 and s["taxa_acerto_pct"] == 40.0
    assert s["r_medio"] == pytest.approx(0.4) and s["fator_lucro"] == pytest.approx(1.67)
    assert s["pior_semana_pct"] == pytest.approx(-2.0)
    assert s["drawdown_max_pct"] == pytest.approx(200 / 10_000 * 100)
    assert s["maior_sequencia_de_perdas"] == 2
    v = verdict(s)
    assert not v["aprovado"]  # poucas operações
    assert verdict({"operacoes": 150, "r_medio": 0.2, "pior_semana_pct": -10})["aprovado"]
    assert stats([]) == {"operacoes": 0}


# ---------------------------------------------------------------- dados
class _Client:
    def __init__(self, df: pd.DataFrame) -> None:
        self.df, self.calls = df, []

    def symbol_spec(self, symbol: str) -> dict:
        return {"simbolo": "TESTm", "ponto": 0.01, "digitos": 2, "tick_size": 0.01, "tick_value": 1.0,
                "contrato": 100.0, "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0}

    def oldest_bar(self, symbol: str, tf: str) -> datetime:
        if tf == "M1":  # este cliente só tem M15 (o mesmo quadro para M15 e H1)
            raise RuntimeError("sem M1")
        return self.df["time"].iloc[0].to_pydatetime()

    def rates_between(self, symbol: str, tf: str, start: datetime, end: datetime) -> pd.DataFrame:
        self.calls.append((tf, start, end))
        d = self.df
        return d[(d["time"] >= pd.Timestamp(start)) & (d["time"] <= pd.Timestamp(end))].reset_index(drop=True)


def test_load_data_caches_and_completes_the_tail(tmp_path: Path):
    df = Bars().flat("2026-06-01 00:00", "2026-06-20 00:00").frame()
    client = _Client(df)
    start, end = datetime(2026, 6, 10, tzinfo=UTC), datetime(2026, 6, 15, tzinfo=UTC)
    data = backtest.load_data(client, "TEST", start, end, cache_dir=tmp_path, say=lambda _: None)
    assert data.spec.money_per_unit == pytest.approx(100.0)
    assert data.bars["time"].iloc[-1] < pd.Timestamp(end)
    calls = len(client.calls)
    backtest.load_data(client, "TEST", start, end, cache_dir=tmp_path, say=lambda _: None)
    assert len(client.calls) == calls  # tudo do cache
    later = datetime(2026, 6, 18, tzinfo=UTC)
    again = backtest.load_data(client, "TEST", start, later, cache_dir=tmp_path, say=lambda _: None)
    assert len(client.calls) > calls  # só o fim novo
    first = pd.Timestamp(start - timedelta(days=21))  # aquecimento do tempo de entrada
    expected = df[(df["time"] >= first) & (df["time"] < pd.Timestamp(later))]
    assert len(again.bars) == len(expected)
    assert np.array_equal(backtest.epoch_s(again.bars["time"]), backtest.epoch_s(expected["time"]))


def test_stale_answer_does_not_leave_a_hole_in_the_cache(tmp_path: Path):
    df = Bars().flat("2026-06-01 00:00", "2026-06-25 00:00").frame()
    client = _Client(df[df["time"] < pd.Timestamp("2026-06-12", tz="UTC")])  # terminal ainda sem os candles novos
    start, end = datetime(2026, 6, 10, tzinfo=UTC), datetime(2026, 6, 20, tzinfo=UTC)
    backtest.load_data(client, "TEST", start, end, cache_dir=tmp_path, say=lambda _: None)
    client.df = df  # sincronizou
    again = backtest.load_data(client, "TEST", start, end, cache_dir=tmp_path, say=lambda _: None)
    expected = df[(df["time"] >= pd.Timestamp(start - timedelta(days=21))) & (df["time"] < pd.Timestamp(end))]
    assert len(again.bars) == len(expected)


def test_spread_stress_changes_cost_not_signals():
    b = buy_setup()
    b.add("2026-06-02 02:30", 99.9, 99.95, 99.55, 99.8)
    base = sim(b).run()
    stressed = sim(b, spread_mult=1.5).run()
    assert base.signals == stressed.signals == 1  # a tolerância do engolfo não muda com o estresse


def test_fetch_drops_the_bar_still_open():
    df = Bars().flat("2026-06-01 00:00", "2026-06-01 02:00").frame()
    end = (ny("2026-06-01 01:50")).to_pydatetime()  # candle das 01:45 ainda aberto
    out = backtest.fetch(_Client(df), "TEST", "M15", ny("2026-06-01 00:00").to_pydatetime(), end)
    assert out["time"].iloc[-1] == ny("2026-06-01 01:30")
