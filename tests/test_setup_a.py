"""Setup A: regras do robô sobre candles sintéticos (horários de Nova York, ponto 0,01, spread 0,10)."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from trading_mcp.setup_a import Params, SetupA, market_minute

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
POINT, DIGITS, SPREAD = 0.01, 2, 10  # spread em pontos = 0,10
NO_PD = Params(premium_discount=False)


def ny(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz=NY).tz_convert("UTC")


class Bars:
    """Candles de 15 min por horário de Nova York; ``add`` substitui o candle do mesmo horário."""

    def __init__(self) -> None:
        self.rows: dict[pd.Timestamp, tuple[float, float, float, float, float]] = {}

    def flat(self, start: str, end: str, price: float = 100.0, half: float = 0.3) -> Bars:
        t, stop = ny(start), ny(end)
        while t < stop:
            self.rows[t] = (price, price + half, price - half, price, SPREAD)
            t += pd.Timedelta(minutes=15)
        return self

    def add(self, when: str, o: float, h: float, l: float, c: float) -> Bars:
        self.rows[ny(when)] = (o, h, l, c, SPREAD)
        return self

    def frame(self) -> pd.DataFrame:
        times = sorted(self.rows)
        df = pd.DataFrame([self.rows[t] for t in times], columns=["open", "high", "low", "close", "spread"])
        df.insert(0, "time", pd.to_datetime(times, utc=True))
        return df


def to_h1(df: pd.DataFrame) -> pd.DataFrame:
    g = df.set_index("time").resample("1h")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "spread": g["spread"].max()}).dropna().reset_index()
    return out


def base_day() -> Bars:
    """Segunda (dia anterior, faixa 99,5-100,5) e terça até a abertura de Londres.

    Ásia da terça: máxima 103,0 (20:00) e mínima 99,0 (22:00), que também tomam a máxima e a mínima do dia anterior
    fora do horário de entrada."""
    b = Bars().flat("2026-05-31 18:00", "2026-06-01 17:00", half=0.5)
    b.flat("2026-06-01 18:00", "2026-06-02 17:00")
    b.add("2026-06-01 20:00", 100.0, 103.0, 99.9, 100.0)
    b.add("2026-06-01 22:00", 100.0, 100.1, 99.0, 100.0)
    return b


def buy_setup() -> Bars:
    """Varre a mínima da Ásia às 02:00 (pavio até 98,8, fecha 99,4) e engolfa às 02:15 (máx. 100,0, mín. 99,4)."""
    b = base_day()
    b.add("2026-06-02 02:00", 99.6, 99.7, 98.8, 99.4)
    b.add("2026-06-02 02:15", 99.45, 100.0, 99.4, 99.9)
    return b


def run(df: pd.DataFrame, params: Params = NO_PD, news: list[datetime] | None = None):
    s = SetupA(df, to_h1(df), POINT, DIGITS, params, 900, news)
    steps = [s.step(i) for i in range(len(df))]
    return s, steps


def signals(steps):
    return [sig for st in steps for sig in st.signals]


def refusals(steps):
    return [r for st in steps for r in st.refusals]


# ---------------------------------------------------------------- sinal
def test_sweep_and_engulf_give_limit_at_half_with_stop_beyond_sweep():
    s, steps = run(buy_setup().frame())
    (sig,) = signals(steps)
    assert sig.direction == "compra"
    assert sig.time == ny("2026-06-02 02:30").to_pydatetime()
    assert sig.entry == pytest.approx(99.70)  # meio do engolfo (100,0 - 0,3)
    assert sig.stop == pytest.approx(98.70)  # extremo da varredura (98,8) - spread
    assert sig.target == pytest.approx(103.0)  # máxima da Ásia, a primeira liquidez pendente acima
    assert sig.target_name == "asia" and sig.level_names == ("asia",)
    assert sig.rr == pytest.approx(3.3)
    assert sig.valid_until == ny("2026-06-02 03:30").to_pydatetime()


def test_sell_mirror():
    b = base_day()
    # Varre a máxima da Ásia (103,0) e engolfa para baixo; alvo = mínima da Ásia (99,0).
    b.add("2026-06-02 02:00", 102.0, 103.4, 101.9, 102.6)
    b.add("2026-06-02 02:15", 102.65, 102.7, 101.5, 101.8)
    _, steps = run(b.frame())
    (sig,) = signals(steps)
    assert sig.direction == "venda"
    assert sig.entry == pytest.approx(102.10)
    assert sig.stop == pytest.approx(103.50)
    assert sig.target == pytest.approx(99.0)


def test_sweep_candle_can_be_the_engulf():
    b = base_day()
    b.add("2026-06-02 01:45", 99.9, 99.95, 99.5, 99.6)  # de baixa: corpo 99,6-99,9
    b.add("2026-06-02 02:00", 99.55, 100.2, 98.8, 100.0)  # varre 99,0 e fecha acima do corpo anterior
    _, steps = run(b.frame())
    (sig,) = signals(steps)
    assert sig.stop == pytest.approx(98.70) and sig.entry == pytest.approx(99.50)


def test_close_beyond_kills_the_level():
    b = base_day()
    b.add("2026-06-02 02:00", 99.6, 99.7, 98.8, 98.9)  # fecha abaixo da mínima da Ásia
    b.add("2026-06-02 02:15", 98.95, 100.0, 98.9, 99.9)
    s, steps = run(b.frame())
    assert signals(steps) == []
    assert "2026-06-02:asia:baixa" in [lid for st in steps for lid in st.broken]
    assert s.levels["2026-06-02:asia:baixa"].state == "rompido"


def test_engulf_more_than_two_candles_after_the_sweep_does_not_count():
    b = base_day()
    b.add("2026-06-02 02:00", 99.6, 99.7, 98.8, 99.4)
    b.add("2026-06-02 03:00", 99.95, 100.4, 99.4, 100.3)  # 4 candles depois
    _, steps = run(b.frame())
    assert signals(steps) == [] and refusals(steps) == []


def test_level_taken_outside_entry_hours_never_arms():
    b = base_day()
    # A mínima do dia anterior (99,5) foi tomada na Ásia (22:00): em Londres, varrer só ela não arma nada.
    b.add("2026-06-02 02:00", 99.8, 99.85, 99.3, 99.6)
    b.add("2026-06-02 02:15", 99.6, 100.1, 99.55, 100.0)
    s, steps = run(b.frame())
    assert s.levels["2026-06-02:dia_anterior:baixa"].outside
    assert signals(steps) == [] and refusals(steps) == []


def test_levels_appear_only_after_their_window():
    s = SetupA(buy_setup().frame(), to_h1(buy_setup().frame()), POINT, DIGITS, NO_PD, 900)
    df = buy_setup().frame()
    i_0145 = int(np.flatnonzero(df["time"] == ny("2026-06-02 01:45"))[0])
    for i in range(i_0145 + 1):
        s.step(i)
    assert not any(lv.name == "asia" for lv in s.active)  # Ásia ainda aberta às 01:45
    s.step(i_0145 + 1)
    assert any(lv.name == "asia" for lv in s.active)


def test_target_needs_two_to_one():
    b = buy_setup()
    b.add("2026-06-01 20:00", 100.0, 101.0, 99.9, 100.0)  # máxima da Ásia 101: 1,3/1,0 < 2
    _, steps = run(b.frame())
    assert signals(steps) == []
    assert [r["reason"] for r in refusals(steps)] == ["rr_baixo"]


def test_month_end_and_news_refuse():
    _, steps = run(buy_setup().frame(), news=[ny("2026-06-02 02:50").to_pydatetime()])
    assert [r["reason"] for r in refusals(steps)] == ["noticia"]
    _, steps = run(buy_setup().frame(), news=[ny("2026-06-02 03:05").to_pydatetime()])  # 35 min depois do sinal
    assert len(signals(steps)) == 1

    shift = pd.Timedelta(days=28)  # terça 2026-06-30: último dia útil de junho
    df = buy_setup().frame()
    df["time"] = df["time"] + shift
    _, steps = run(df)
    assert [r["reason"] for r in refusals(steps)] == ["fim_do_mes"]
    _, steps = run(df, Params(premium_discount=False, skip_month_end=False))
    assert len(signals(steps)) == 1


def test_reentry_needs_new_sweep_and_stops_after_max_fills():
    b = buy_setup()
    # Segunda varredura + engolfo às 03:00/03:15.
    b.add("2026-06-02 03:00", 99.6, 99.7, 98.9, 99.4)
    b.add("2026-06-02 03:15", 99.45, 100.0, 99.4, 99.9)
    b.add("2026-06-02 04:00", 99.6, 99.7, 98.9, 99.4)
    b.add("2026-06-02 04:15", 99.45, 100.0, 99.4, 99.9)
    df = b.frame()
    s = SetupA(df, to_h1(df), POINT, DIGITS, Params(premium_discount=False, max_attempts=2), 900)
    got = []
    for i in range(len(df)):
        for sig in s.step(i).signals:
            got.append(sig)
            s.register_fill(sig)  # cada sinal executado conta uma tentativa
    assert len(got) == 2  # a terceira varredura não arma mais


def test_engulf_variants():
    b = base_day()
    b.add("2026-06-02 02:00", 99.6, 99.7, 98.8, 99.4)
    b.add("2026-06-02 02:15", 99.45, 99.68, 99.4, 99.65)  # corpo cobre o anterior, mas não fecha acima da máxima 99,7
    _, steps = run(b.frame(), Params(premium_discount=False, engulf="corpo", min_rr=0.1))
    assert len(signals(steps)) == 1
    _, steps = run(b.frame(), Params(premium_discount=False, engulf="extremo", min_rr=0.1))
    assert signals(steps) == []


# ---------------------------------------------------------------- prêmio / desconto
def test_zone_uses_last_confirmed_h1_leg_only():
    # H1: um fundo mais antigo em 80 (fora da perna), topo em ~100, fundo em 90, topo em 110, volta a 105.
    closes = (list(np.linspace(95, 80, 8)) + list(np.linspace(81, 100, 10)) + list(np.linspace(99, 90, 10))
              + list(np.linspace(91, 110, 10)) + list(np.linspace(109, 105, 8)))
    start = ny("2026-06-01 18:00")
    h1 = pd.DataFrame({"time": [start + pd.Timedelta(hours=k) for k in range(len(closes))],
                       "open": closes, "high": [c + 0.2 for c in closes], "low": [c - 0.2 for c in closes],
                       "close": closes, "spread": SPREAD})
    m15 = Bars().flat("2026-06-04 00:00", "2026-06-04 02:00", price=105.0, half=0.1).frame()
    s = SetupA(m15, h1, POINT, DIGITS, Params(), 900)
    zone_hi = s.zone(len(m15) - 1, 108.0)
    zone_lo = s.zone(len(m15) - 1, 92.0)
    assert zone_hi == pytest.approx((108 - 89.8) / (110.2 - 89.8) * 100)
    assert zone_lo < 50 < zone_hi


def _leg_h1(bottom: float, top: float) -> pd.DataFrame:
    """H1 que cai até ``bottom`` (hora 10), sobe até ``top`` (hora 20) e recua um pouco, antes de 02:00 de terça."""
    closes = (list(np.linspace(bottom + 5, bottom, 11)) + list(np.linspace(bottom + 1, top, 10))
              + list(np.linspace(top - 1, top - 3, 8)))
    start = ny("2026-05-31 18:00")
    return pd.DataFrame({"time": [start + pd.Timedelta(hours=k) for k in range(len(closes))], "open": closes,
                         "high": [c + 0.2 for c in closes], "low": [c - 0.2 for c in closes], "close": closes,
                         "spread": SPREAD})


def test_sell_needs_premium_and_buy_needs_discount():
    b = base_day()
    b.add("2026-06-02 02:00", 102.0, 103.4, 101.9, 102.6)
    b.add("2026-06-02 02:15", 102.65, 102.7, 101.5, 101.8)
    df = b.frame()
    # Perna de 100 a 130: a venda em 102,1 fica em desconto.
    s = SetupA(df, _leg_h1(100, 130), POINT, DIGITS, Params(), 900)
    steps = [s.step(i) for i in range(len(df))]
    assert signals(steps) == [] and [r["reason"] for r in refusals(steps)] == ["area_errada"]
    # Perna de 90 a 103,5: 102,1 fica em prêmio e a venda sai.
    s = SetupA(df, _leg_h1(90, 103.5), POINT, DIGITS, Params(), 900)
    steps = [s.step(i) for i in range(len(df))]
    (sig,) = signals(steps)
    assert sig.zone_pct > 52.5
    # Compra em prêmio: recusada.
    df = buy_setup().frame()
    s = SetupA(df, _leg_h1(90, 100.5), POINT, DIGITS, Params(), 900)
    steps = [s.step(i) for i in range(len(df))]
    assert [r["reason"] for r in refusals(steps)] == ["area_errada"]


def test_us_holidays_and_early_closes():
    from trading_mcp.setup_a import us_market_holidays

    h = us_market_holidays(2026)
    for day in ("2026-01-19", "2026-04-03", "2026-07-03", "2026-11-26", "2026-11-27", "2026-12-24"):
        assert pd.Timestamp(day).date() in h  # MLK, Sexta-Feira Santa, 4/7 observado, Ação de Graças e dia seguinte
    assert pd.Timestamp("2026-07-06").date() not in h
    assert pd.Timestamp("2023-07-03").date() in us_market_holidays(2023)  # véspera de 4/7 numa terça
    assert pd.Timestamp("2021-06-18").date() not in us_market_holidays(2021)  # Juneteenth só desde 2022


def test_no_signal_on_us_holiday():
    df = buy_setup().frame()
    df["time"] = df["time"] + pd.Timedelta(days=14)  # terça 2026-06-16, dia normal
    _, steps = run(df)
    assert len(signals(steps)) == 1
    juneteenth = buy_setup().frame()
    juneteenth["time"] = juneteenth["time"] + pd.Timedelta(days=17)  # sexta 2026-06-19 (Juneteenth)
    _, steps = run(juneteenth)
    assert [r["reason"] for r in refusals(steps)] == ["feriado_eua"]
    _, steps = run(juneteenth, Params(premium_discount=False, skip_us_holidays=False))
    assert len(signals(steps)) == 1


def test_engulf_on_the_last_candle_before_16h_is_refused():
    b = base_day()
    b.add("2026-06-02 15:30", 99.6, 99.7, 98.8, 99.4)
    b.add("2026-06-02 15:45", 99.45, 100.0, 99.4, 99.9)  # sinal às 16:00: a ordem nasceria vencida
    _, steps = run(b.frame())
    assert signals(steps) == [] and [r["reason"] for r in refusals(steps)] == ["sem_tempo"]


def _with_fvg(df: pd.DataFrame) -> pd.DataFrame:
    """H1 com um FVG de alta 98,0-98,9 formado segunda às 12:00-13:00 de NY (antes da Ásia de terça)."""
    ctx = to_h1(df).set_index("time")
    for when, row in (("2026-06-01 10:00", (97.5, 98.0, 97.0, 97.9)), ("2026-06-01 11:00", (97.9, 99.5, 97.8, 99.4)),
                      ("2026-06-01 12:00", (99.4, 100.2, 98.9, 100.0))):
        ctx.loc[ny(when), ["open", "high", "low", "close"]] = row
    return ctx.reset_index()


def test_poi_fvg_h1_needs_the_sweep_to_be_the_first_touch():
    poi = Params(premium_discount=False, poi="fvg_h1")
    df = buy_setup().frame()
    s = SetupA(df, _with_fvg(df), POINT, DIGITS, poi, 900)
    steps = [s.step(i) for i in range(len(df))]
    (sig,) = signals(steps)
    assert sig.poi == "fvg_h1 98.00-98.90"
    # Sem FVG no H1: recusa.
    _, steps = run(df, poi)
    assert signals(steps) == [] and [r["reason"] for r in refusals(steps)] == ["sem_poi"]
    # FVG já tocado na Ásia (22:00 desce a 98,85): não é mais POI.
    b = buy_setup()
    b.add("2026-06-01 22:00", 100.0, 100.1, 98.85, 100.0)
    df = b.frame()
    s = SetupA(df, _with_fvg(df), POINT, DIGITS, poi, 900)
    steps = [s.step(i) for i in range(len(df))]
    assert signals(steps) == [] and [r["reason"] for r in refusals(steps)] == ["sem_poi"]


def test_params_validate():
    with pytest.raises(ValueError):
        Params(levels=("asia", "lua"))
    with pytest.raises(ValueError):
        Params(entry_start=time(16, 0), entry_end=time(2, 0))
    assert market_minute(time(17, 0)) == 0 and market_minute(time(2, 0)) == 540
