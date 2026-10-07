"""Estratégias de continuação: rompimento dos níveis, faixa de ruído e ORB, com o simulador."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from test_backtest import SPEC, _m1
from test_setup_a import SPREAD, Bars, base_day, ny, to_h1
from trading_mcp import backtest
from trading_mcp.backtest import Data, Simulator
from trading_mcp.estrategias import ORB, FaixaRuido, OrbParams, Rompimento, RuidoParams
from trading_mcp.setup_a import Params, us_market_holidays


def _run(strategy, params, df: pd.DataFrame, tf: str = "M15", **kw):
    return Simulator(Data(SPEC, tf, df, to_h1(df), None), params, strategy=strategy, **kw).run()


# ---------------------------------------------------------------- rompimento
def test_breakout_of_a_level_buys_at_the_next_open_and_exits_at_16_45():
    b = base_day()  # máxima da Ásia 103,0
    b.add("2026-06-02 03:00", 102.8, 103.5, 102.7, 103.3)  # fecha acima de 103,0
    b.add("2026-06-02 03:15", 103.3, 103.6, 103.2, 103.5)
    b.flat("2026-06-02 03:30", "2026-06-02 17:00", price=104.0)
    df = b.frame()
    s = Rompimento(df, to_h1(df), 0.01, 2, Params(), 900)
    sigs = [sig for i in range(len(df)) for sig in s.step(i).signals]
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.kind == "mercado" and sig.direction == "compra" and "asia" in sig.level_names
    assert sig.stop == pytest.approx(102.6) and sig.target is None
    res = _run(Rompimento, Params(), df)
    (t,) = res.trades
    assert t["executada"] == pytest.approx(103.4)  # ask da abertura das 03:15
    assert t["motivo"] == "horario" and t["saida_utc"] == "2026-06-02T20:45:00Z"
    assert t["lotes"] == pytest.approx(1.56)
    assert t["resultado"] == pytest.approx((104.0 - 103.4) * 1.56 * 100)


def test_breakout_stop_and_filters():
    b = base_day()
    b.add("2026-06-02 03:00", 102.8, 103.5, 102.7, 103.3)
    b.add("2026-06-02 03:15", 103.3, 103.4, 102.5, 102.6)  # volta e bate o stop (102,6) no candle seguinte
    df = b.frame()
    (t,) = _run(Rompimento, Params(), df).trades
    assert t["motivo"] == "stop" and t["r"] == pytest.approx(-1.0, abs=0.01)
    # Notícia a 10 min do sinal: recusa.
    res = Simulator(Data(SPEC, "M15", df, to_h1(df), None), Params(), strategy=Rompimento,
                    news=[ny("2026-06-02 03:25").to_pydatetime()]).run()
    assert res.trades == [] and res.refusals["noticia"] == 1


# ---------------------------------------------------------------- faixa de ruído
def _ruido_days() -> Bars:
    """19 dias úteis com o mesmo desenho: abre 100, fica em 100,2 nas decisões (ruído = 0,2%), fecha 100."""
    b = Bars()
    for day in pd.bdate_range("2026-05-04", "2026-05-29"):
        if day.date().isoformat() == "2026-05-25":
            continue  # Memorial Day
        d = day.date().isoformat()
        b.flat(f"{d} 09:30", f"{d} 16:00", price=100.2, half=0.05)
        b.add(f"{d} 09:30", 100.0, 100.25, 99.95, 100.2)
        b.add(f"{d} 15:30", 100.2, 100.25, 99.95, 100.0)
        b.add(f"{d} 15:45", 100.0, 100.05, 99.95, 100.0)
    return b


def test_noise_area_enters_above_the_band_and_exits_on_the_trailing_stop():
    b = _ruido_days()
    d = "2026-06-01"
    b.flat(f"{d} 09:30", f"{d} 16:00", price=100.0, half=0.05)
    b.add(f"{d} 09:30", 100.0, 100.15, 99.95, 100.1)
    b.add(f"{d} 09:45", 100.1, 100.55, 100.05, 100.5)  # 10:00: 100,5 > 100 x 1,002
    b.add(f"{d} 10:00", 100.5, 100.7, 100.45, 100.6)
    b.add(f"{d} 10:15", 100.6, 100.6, 99.95, 100.0)  # 10:30: abaixo de max(faixa, VWAP)
    df = b.frame()
    res = _run(FaixaRuido, RuidoParams(), df)
    (t,) = res.trades
    assert t["direcao"] == "compra" and t["executada"] == pytest.approx(100.6)  # ask da abertura das 10:00
    assert t["stop"] == pytest.approx(99.8)  # proteção na faixa de baixo
    assert t["motivo"] == "stop_movel" and t["saida"] == pytest.approx(100.0)
    assert t["saida_utc"] == ny(f"{d} 10:30").strftime("%Y-%m-%dT%H:%M:%SZ")


def test_noise_area_needs_fourteen_previous_days():
    b = Bars()
    d = "2026-06-01"
    b.flat(f"{d} 09:30", f"{d} 16:00", price=100.0, half=0.05)
    b.add(f"{d} 09:45", 100.1, 100.55, 100.05, 100.5)
    assert _run(FaixaRuido, RuidoParams(), b.frame()).trades == []


# ---------------------------------------------------------------- ORB
def _m5(rows: dict[str, tuple[float, float, float, float]], start: str, end: str, price: float) -> pd.DataFrame:
    times = pd.date_range(ny(start), ny(end), freq="5min", inclusive="left")
    data = {t: (price, price + 0.05, price - 0.05, price) for t in times}
    data.update({ny(k): v for k, v in rows.items()})
    df = pd.DataFrame([data[t] for t in sorted(data)], columns=["open", "high", "low", "close"])
    df.insert(0, "time", pd.to_datetime(sorted(data), utc=True))
    df["spread"] = SPREAD
    return df


def test_orb_follows_the_first_five_minutes():
    df = _m5({"2026-06-02 09:30": (100.0, 100.4, 99.9, 100.3)}, "2026-06-02 09:30", "2026-06-02 16:30", 100.5)
    (t,) = _run(ORB, OrbParams(), df, tf="M5").trades
    assert t["direcao"] == "compra" and t["stop"] == pytest.approx(99.9)
    assert t["alvo"] == pytest.approx(100.4 + 10 * 0.5)
    assert t["executada"] == pytest.approx(100.6)  # ask da abertura das 9:35
    assert t["motivo"] == "horario" and t["saida_utc"] == "2026-06-02T20:00:00Z"


def test_orb_needs_m5():
    df = Bars().flat("2026-06-02 09:30", "2026-06-02 10:00").frame()
    with pytest.raises(ValueError):
        ORB(df, to_h1(df), 0.01, 2, OrbParams(), 900)


# ---------------------------------------------------------------- faixa de ruído: regras do artigo
HIST_DAYS = [d.date().isoformat() for d in pd.bdate_range("2026-05-04", "2026-05-29")
             if d.date().isoformat() != "2026-05-25"]  # 19 dias úteis (sem o Memorial Day)


def _bar(b: Bars, when: str, o: float, c: float, low: float | None = None, high: float | None = None) -> None:
    b.add(when, o, max(o, c) + 0.02 if high is None else high, min(o, c) - 0.02 if low is None else low, c)


def _day(b: Bars, d: str, base: float = 0.2, moves: dict[str, float] | None = None, close_1545: float = 100.0) -> None:
    """Dia das 9:30 às 16:00 que abre em 100 e fica em 100 x (1 + base%); ``moves`` troca o fechamento de candles
    (pela hora de abertura do candle: o das 09:45 é o que termina na decisão das 10:00)."""
    price = 100 * (1 + base / 100)
    b.flat(f"{d} 09:30", f"{d} 16:00", price=price, half=0.02)
    _bar(b, f"{d} 09:30", 100.0, price)
    for when, m in (moves or {}).items():
        _bar(b, f"{d} {when}", price, 100 * (1 + m / 100))
    _bar(b, f"{d} 15:45", price, close_1545)


def _iso(when: str) -> str:
    return ny(when).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_noise_is_the_mean_of_the_last_14_days_only():
    b = Bars()
    for k, d in enumerate(HIST_DAYS):  # 10:00: 1% nos 5 mais antigos, 0,2% nos 13 seguintes, 0,6% no último
        _day(b, d, moves={"09:45": 1.0 if k < 5 else 0.6 if k == len(HIST_DAYS) - 1 else 0.2})
    _day(b, "2026-06-01", base=0.25)  # 0,25% > média dos 14 (0,2286%), < último dia (0,6%) e < 19 dias (0,43%)
    trades = _run(FaixaRuido, RuidoParams(), b.frame()).trades
    (t, *_) = [t for t in trades if t["executada_utc"].startswith("2026-06-01")]
    assert t["direcao"] == "compra" and t["executada_utc"] == _iso("2026-06-01 10:00")


def test_overnight_gap_widens_the_band():
    b = Bars()
    for k, d in enumerate(HIST_DAYS):
        _day(b, d, close_1545=101.0 if k == len(HIST_DAYS) - 1 else 100.0)  # véspera fecha em 101
    _day(b, "2026-06-01", base=0.5)  # abre 100 (gap de baixa) e vai a 100,5: abaixo de 101 x 1,002
    assert _run(FaixaRuido, RuidoParams(), b.frame()).trades == []


def test_vwap_trailing_stop_and_daily_reset():
    b = Bars()
    for d in HIST_DAYS:
        _day(b, d)
    d = "2026-06-01"
    _day(b, d, base=0.6)
    _bar(b, f"{d} 09:30", 100.0, 100.1)
    _bar(b, f"{d} 09:45", 100.1, 101.0)  # volume grande aqui: VWAP ~100,7
    _bar(b, f"{d} 10:00", 101.0, 100.6)
    df = b.frame()
    df["tick_volume"] = 100.0  # histórico pesado: um VWAP que não zera por dia ficaria perto de 100,2
    df.loc[df["time"] >= ny(f"{d} 09:30"), "tick_volume"] = 1.0
    df.loc[df["time"] == ny(f"{d} 09:45"), "tick_volume"] = 1000.0
    (t, *_) = _run(FaixaRuido, RuidoParams(), df).trades
    # 10:30: 100,6 ainda está acima da faixa (100,2), mas abaixo do VWAP: sai.
    assert t["motivo"] == "stop_movel" and t["saida_utc"] == _iso(f"{d} 10:30")


def test_noise_area_reversal_exits_then_sells_at_the_same_open():
    b = Bars()
    for d in HIST_DAYS:  # ruído 0,5% às 10:00 e só 0,1% às 10:30
        _day(b, d, base=0.3, moves={"09:45": 0.5, "10:15": 0.1})
    d = "2026-06-01"
    _day(b, d, base=0.3)
    _bar(b, f"{d} 09:45", 100.3, 100.6)  # 10:00 acima de 100,5: compra (proteção em 99,5)
    _bar(b, f"{d} 10:00", 100.6, 100.6)
    _bar(b, f"{d} 10:15", 100.6, 99.8, low=99.75)  # 10:30 abaixo de 99,9 sem tocar 99,5: sai e vende
    b.flat(f"{d} 10:30", f"{d} 16:00", price=99.8, half=0.02)
    long_, short, *_ = _run(FaixaRuido, RuidoParams(), b.frame()).trades
    assert long_["direcao"] == "compra" and long_["motivo"] == "stop_movel" and long_["saida"] == pytest.approx(99.8)
    assert short["direcao"] == "venda" and short["executada"] == pytest.approx(99.8)
    assert short["executada_utc"] == long_["saida_utc"] == _iso(f"{d} 10:30")
    assert short["stop"] == pytest.approx(100.1)  # proteção na faixa de cima das 10:30


def test_after_the_protective_stop_the_next_check_can_enter_again():
    b = Bars()
    for d in HIST_DAYS:
        _day(b, d)
    d = "2026-06-01"
    _day(b, d, base=0.5)
    _bar(b, f"{d} 10:00", 100.5, 100.4, low=99.7)  # bate a proteção (99,8)
    first, second, *_ = _run(FaixaRuido, RuidoParams(), b.frame()).trades
    assert first["motivo"] == "stop" and second["direcao"] == "compra"
    assert second["executada_utc"] == _iso(f"{d} 10:30")


# ---------------------------------------------------------------- atraso de execução
def test_execution_delay_fills_at_the_next_minute():
    b = base_day()
    b.add("2026-06-02 03:00", 102.8, 103.5, 102.7, 103.3)
    b.add("2026-06-02 03:15", 103.3, 103.9, 103.2, 103.5)
    b.flat("2026-06-02 03:30", "2026-06-02 17:00", price=103.5)
    df = b.frame()
    m1 = _m1([("2026-06-02 03:15", 103.3, 103.35, 103.2, 103.3), ("2026-06-02 03:16", 103.8, 103.9, 103.7, 103.8)]
             + [(f"2026-06-02 03:{m}", 103.5, 103.55, 103.45, 103.5) for m in range(17, 30)])
    fast = Simulator(Data(SPEC, "M15", df, to_h1(df), m1), Params(), strategy=Rompimento).run()
    slow = Simulator(Data(SPEC, "M15", df, to_h1(df), m1), Params(), strategy=Rompimento, delay_s=60).run()
    assert fast.trades[0]["executada"] == pytest.approx(103.4)
    assert slow.trades[0]["executada"] == pytest.approx(103.9)  # abertura das 03:16 + spread
    with pytest.raises(ValueError):
        Simulator(Data(SPEC, "M15", df, to_h1(df), m1), Params(), strategy=Rompimento, delay_s=900)


# ---------------------------------------------------------------- rompimento: filtros
def test_breakout_outside_hours_and_short_stop_are_refused():
    late = base_day()
    late.add("2026-06-02 16:15", 102.9, 103.3, 102.85, 103.2)  # rompe a máxima da Ásia fora do horário
    res = _run(Rompimento, Params(), late.frame())
    assert res.trades == [] and res.refusals["fora_do_horario"] == 1
    tiny = base_day()
    tiny.add("2026-06-02 03:00", 102.95, 103.05, 102.98, 103.04)  # candle de 0,07: stop menor que 3 spreads
    res = _run(Rompimento, Params(), tiny.frame())
    assert res.trades == [] and res.refusals["stop_curto"] == 1


# ---------------------------------------------------------------- ORB: venda, doji, inverno
def test_orb_short_doji_and_winter_time():
    df = _m5({"2026-12-01 09:30": (100.0, 100.1, 99.6, 99.7)}, "2026-12-01 09:30", "2026-12-01 16:30", 99.5)
    (t,) = _run(ORB, OrbParams(), df, tf="M5").trades
    assert t["direcao"] == "venda" and t["stop"] == pytest.approx(100.1)
    assert t["alvo"] == pytest.approx(99.7 - 10 * 0.4)
    assert t["executada_utc"] == "2026-12-01T14:35:00Z"  # 9:35 de NY no horário de inverno
    doji = _m5({"2026-12-01 09:30": (100.0, 100.1, 99.6, 100.0)}, "2026-12-01 09:30", "2026-12-01 16:30", 99.5)
    assert _run(ORB, OrbParams(), doji, tf="M5").trades == []


# ---------------------------------------------------------------- miudezas da auditoria
def test_new_year_on_saturday_is_not_a_friday_holiday():
    assert date(2021, 12, 31) not in us_market_holidays(2022) | us_market_holidays(2021)
    assert date(2023, 1, 2) in us_market_holidays(2023)  # 1º de janeiro no domingo: segunda


def test_old_cache_without_tick_volume_is_reused(tmp_path: Path):
    path = tmp_path / "X_M15.npz"
    np.savez_compressed(path, time=np.array([1_700_000_000, 1_700_000_900]), open=np.ones(2), high=np.ones(2),
                        low=np.ones(2), close=np.ones(2), spread=np.ones(2))
    path.with_suffix(".json").write_text(json.dumps({"de": "x", "ate": "y"}), encoding="utf-8")
    df, _ = backtest._load(path)
    assert list(df["tick_volume"]) == [0.0, 0.0]


def test_reversal_sizing_counts_the_position_that_is_leaving():
    from test_setup_a import buy_setup

    df = buy_setup().frame()
    s = Simulator(Data(SPEC, "M15", df, to_h1(df), None), Params(premium_discount=False))
    leaving = backtest._Position(signal=SimpleNamespace(direction="compra"), lots=1.0, risk=100.0, fill=104.5,
                                 fill_time=None, stop=99.0, flat_s=0, session="londres", exit_pending="stop_movel")
    s.pos = leaving
    sig = SimpleNamespace(entry=99.70, stop=98.70)
    # Fechamento do candle 0 = 100,0: a posição que sai perde 450; sobram 50 dos 500 do dia.
    assert s._size(sig, 0) == pytest.approx((0.5, 50.0))
