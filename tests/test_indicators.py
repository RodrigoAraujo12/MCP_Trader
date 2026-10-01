"""Testes dos indicadores contra implementações de referência ingênuas (listas puras)."""
from __future__ import annotations

import math
import random

import numpy as np
import pandas as pd
import pytest

from trading_mcp import indicators as ind

NAN = float("nan")


# ------------------------------------------------------------------ referências ingênuas


def ref_sma(x: list[float], n: int) -> list[float]:
    out = [NAN] * len(x)
    for t in range(n - 1, len(x)):
        out[t] = sum(x[t - n + 1 : t + 1]) / n
    return out


def _ref_smoothed(x: list[float], n: int, alpha: float) -> list[float]:
    out = [NAN] * len(x)
    seen: list[float] = []
    prev = None
    for t, v in enumerate(x):
        if math.isnan(v):
            continue
        if prev is None:
            seen.append(v)
            if len(seen) == n:
                prev = sum(seen) / n
                out[t] = prev
        else:
            prev = alpha * v + (1 - alpha) * prev
            out[t] = prev
    return out


def ref_ema(x: list[float], n: int) -> list[float]:
    return _ref_smoothed(x, n, 2 / (n + 1))


def ref_rma(x: list[float], n: int) -> list[float]:
    return _ref_smoothed(x, n, 1 / n)


def ref_rsi(x: list[float], n: int) -> list[float]:
    out = [NAN] * len(x)
    # Wilder clássico: média simples das n primeiras variações, depois suavização recursiva.
    gains = [max(x[t] - x[t - 1], 0.0) for t in range(1, len(x))]
    losses = [max(x[t - 1] - x[t], 0.0) for t in range(1, len(x))]
    if len(gains) < n:
        return out
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n

    def val(g: float, lo: float) -> float:
        if lo == 0:
            return 100.0
        if g == 0:
            return 0.0
        return 100 - 100 / (1 + g / lo)

    out[n] = val(ag, al)
    for k in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[k]) / n
        al = (al * (n - 1) + losses[k]) / n
        out[k + 1] = val(ag, al)
    return out


def ref_tr(h: list[float], lo: list[float], c: list[float]) -> list[float]:
    out = [h[0] - lo[0]]
    for t in range(1, len(h)):
        out.append(max(h[t] - lo[t], abs(h[t] - c[t - 1]), abs(lo[t] - c[t - 1])))
    return out


def ref_atr(h, lo, c, n):
    return ref_rma(ref_tr(h, lo, c), n)


def ref_bb(x: list[float], n: int, k: float):
    mid, up, low = [NAN] * len(x), [NAN] * len(x), [NAN] * len(x)
    for t in range(n - 1, len(x)):
        w = x[t - n + 1 : t + 1]
        m = sum(w) / n
        sd = math.sqrt(sum((v - m) ** 2 for v in w) / n)
        mid[t], up[t], low[t] = m, m + k * sd, m - k * sd
    return mid, up, low


def ref_macd(x, fast, slow, sig):
    f, s = ref_ema(x, fast), ref_ema(x, slow)
    line = [a - b for a, b in zip(f, s)]
    signal = ref_ema(line, sig)
    hist = [a - b for a, b in zip(line, signal)]
    return line, signal, hist


# ------------------------------------------------------------------ dados


def random_walk(n: int = 400, seed: int = 42):
    rng = random.Random(seed)
    close, high, low, opn = [], [], [], []
    price = 100.0
    for _ in range(n):
        o = price
        price = max(1.0, price + rng.gauss(0, 1.0))
        high.append(max(o, price) + abs(rng.gauss(0, 0.5)))
        low.append(min(o, price) - abs(rng.gauss(0, 0.5)))
        opn.append(o)
        close.append(price)
    return opn, high, low, close


@pytest.fixture(scope="module")
def data():
    o, h, lo, c = random_walk()
    df = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c})
    return df, o, h, lo, c


def assert_close(actual: pd.Series, expected: list[float], tol: float = 1e-9) -> None:
    a = np.asarray(actual, dtype=float)
    e = np.asarray(expected, dtype=float)
    assert len(a) == len(e)
    assert np.array_equal(np.isnan(a), np.isnan(e)), "posições de NaN diferem"
    mask = ~np.isnan(e)
    assert np.allclose(a[mask], e[mask], rtol=0, atol=tol)


# ------------------------------------------------------------------ comparação com referências


@pytest.mark.parametrize("n", [1, 5, 20, 50])
def test_sma_matches_reference(data, n):
    df, _, _, _, c = data
    assert_close(ind.sma(df["close"], n), ref_sma(c, n))


@pytest.mark.parametrize("n", [1, 3, 12, 26, 200])
def test_ema_matches_reference(data, n):
    df, _, _, _, c = data
    assert_close(ind.ema(df["close"], n), ref_ema(c, n))


@pytest.mark.parametrize("n", [2, 14, 30])
def test_rma_matches_reference(data, n):
    df, _, _, _, c = data
    assert_close(ind.rma(df["close"], n), ref_rma(c, n))


def test_ema_seed_is_sma_of_first_values():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ind.ema(s, 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(2.0)
    assert out.iloc[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)  # alpha = 0.5


def test_ema_with_leading_nans():
    x = [NAN, NAN, NAN] + [float(v) for v in range(1, 11)]
    out = ind.ema(pd.Series(x), 4)
    assert_close(out, ref_ema(x, 4))
    assert out.iloc[:6].isna().all()  # semente na 4a posição válida (índice 6)
    assert out.iloc[6] == pytest.approx(2.5)


@pytest.mark.parametrize("n", [2, 5, 14, 21])
def test_rsi_matches_reference(data, n):
    df, _, _, _, c = data
    out = ind.rsi(df["close"], n)
    assert_close(out, ref_rsi(c, n))
    assert out.iloc[:n].isna().all() and not math.isnan(out.iloc[n])


def test_rsi_all_up_is_100_and_all_down_is_0():
    up = pd.Series([float(i) for i in range(1, 40)])
    assert ind.rsi(up, 14).dropna().eq(100.0).all()
    down = pd.Series([float(i) for i in range(40, 1, -1)])
    assert ind.rsi(down, 14).dropna().eq(0.0).all()


def test_rsi_flat_series_is_neutral():
    # Sem nenhuma variação: 50 (convenção do MT5), não 100.
    out = ind.rsi(pd.Series([5.0] * 30), 14)
    assert out.iloc[:14].isna().all()
    assert out.iloc[14:].eq(50.0).all()


def test_rsi_flat_after_rise_keeps_tradingview_rule():
    # Perda média zero com ganho positivo continua 100.
    out = ind.rsi(pd.Series([float(i) for i in range(15)] + [14.0] * 5), 14)
    assert out.iloc[-1] == 100.0


def test_rsi_bounds(data):
    out = ind.rsi(data[0]["close"], 14).dropna()
    assert ((out >= 0) & (out <= 100)).all()


def test_true_range_matches_reference_and_first_is_hl(data):
    df, _, h, lo, c = data
    tr = ind.true_range(df["high"], df["low"], df["close"])
    assert tr.iloc[0] == pytest.approx(h[0] - lo[0])
    assert_close(tr, ref_tr(h, lo, c))


def test_true_range_gap():
    tr = ind.true_range(pd.Series([10.0, 15.0]), pd.Series([9.0, 14.0]), pd.Series([9.5, 14.5]))
    assert tr.iloc[1] == pytest.approx(5.5)  # |15 - 9.5|


@pytest.mark.parametrize("n", [2, 14, 20])
def test_atr_matches_reference(data, n):
    df, _, h, lo, c = data
    assert_close(ind.atr(df["high"], df["low"], df["close"], n), ref_atr(h, lo, c, n))


@pytest.mark.parametrize("n,k", [(20, 2.0), (5, 1.5), (1, 2.0)])
def test_bollinger_matches_reference(data, n, k):
    df, _, _, _, c = data
    bb = ind.bollinger(df["close"], n, k)
    m, u, lo = ref_bb(c, n, k)
    assert_close(bb["middle"], m)
    assert_close(bb["upper"], u)
    assert_close(bb["lower"], lo)


def test_bollinger_uses_population_std():
    bb = ind.bollinger(pd.Series([1.0, 2.0, 3.0, 4.0]), 4, 1.0)
    pop = math.sqrt(1.25)  # variância populacional = 1.25 (amostral seria 1.6667)
    assert bb["upper"].iloc[-1] == pytest.approx(2.5 + pop)


def test_bollinger_flat_series_has_zero_width():
    bb = ind.bollinger(pd.Series([3.0] * 25), 20, 2.0)
    assert (bb["upper"].dropna() == 3.0).all() and (bb["lower"].dropna() == 3.0).all()


@pytest.mark.parametrize("f,s,g", [(12, 26, 9), (3, 8, 4)])
def test_macd_matches_reference(data, f, s, g):
    df, _, _, _, c = data
    out = ind.macd(df["close"], f, s, g)
    line, sig, hist = ref_macd(c, f, s, g)
    assert_close(out["macd"], line)
    assert_close(out["signal"], sig)
    assert_close(out["hist"], hist)
    assert list(out.columns) == ["macd", "signal", "hist"]
    assert out["signal"].first_valid_index() == s + g - 2


def test_macd_invalid():
    with pytest.raises(ValueError):
        ind.macd(pd.Series([1.0] * 50), 26, 12, 9)


def test_index_preserved():
    s = pd.Series([1.0, 2.0, 3.0, 4.0], index=[10, 20, 30, 40])
    assert list(ind.sma(s, 2).index) == [10, 20, 30, 40]
    assert list(ind.macd(s, 1, 2, 1).index) == [10, 20, 30, 40]


# ------------------------------------------------------------------ parse_spec


@pytest.mark.parametrize(
    "text,label",
    [
        ("RSI", "RSI(14)"), ("rsi(14)", "RSI(14)"), (" Rsi ( 7 ) ", "RSI(7)"),
        ("SMA(50)", "SMA(50)"), ("ema( 200 )", "EMA(200)"),
        ("MACD", "MACD(12,26,9)"), ("macd(12, 26, 9)", "MACD(12,26,9)"),
        ("ATR", "ATR(14)"), ("atr(10)", "ATR(10)"),
        ("BB", "BB(20,2)"), ("BB(20,2)", "BB(20,2)"), ("BOLLINGER(20,2)", "BB(20,2)"),
        ("bb(10, 2.5)", "BB(10,2.5)"), ("BB(20)", "BB(20,2)"),
    ],
)
def test_parse_spec_labels(text, label):
    assert ind.parse_spec(text).label == label


def test_parse_spec_fields():
    s = ind.parse_spec("MACD(5,10,3)")
    assert s.kind == "MACD" and s.params == (5, 10, 3)
    with pytest.raises(Exception):
        s.kind = "RSI"  # type: ignore[misc]  # frozen


@pytest.mark.parametrize(
    "text",
    ["MACD(26,12,9)", "MACD(12,12,9)", "SMA", "EMA", "SMA()", "RSI(1)", "ATR(1)", "RSI(0)",
     "FOO(3)", "BB(20,0)", "BB(20,-1)", "BB(0,2)", "SMA(0)", "SMA(-5)", "SMA(2.5)",
     "SMA(abc)", "RSI(14", "", "MACD(12,26)", "SMA(1,2)", "BB(20,x)"],
)
def test_parse_spec_invalid(text):
    with pytest.raises(ValueError):
        ind.parse_spec(text)


def test_parse_spec_unknown_name_message():
    with pytest.raises(ValueError, match="desconhecido"):
        ind.parse_spec("STOCH(14)")


def test_required_bars():
    assert ind.required_bars([]) == 50
    assert ind.required_bars(["SMA(10)"]) == 50
    assert ind.required_bars(["SMA(200)"]) == 200
    assert ind.required_bars(["EMA(200)"]) == 2000
    assert ind.required_bars(["RSI"]) == 140
    assert ind.required_bars(["ATR(20)"]) == 200
    assert ind.required_bars(["MACD"]) == 10 * 26 + 9
    assert ind.required_bars(["BB(100,2)", "RSI(14)"]) == 140


# ------------------------------------------------------------------ compute


def test_compute_values_match_series(data):
    df, _, h, lo, c = data
    res = ind.compute(df, ["RSI(14)", "SMA(50)", "EMA(20)", "ATR(14)", "MACD", "BB(20,2)"])
    assert set(res) == {"RSI(14)", "SMA(50)", "EMA(20)", "ATR(14)", "MACD(12,26,9)", "BB(20,2)"}
    r = ref_rsi(c, 14)
    assert res["RSI(14)"]["valor"] == pytest.approx(r[-1], abs=0.005)
    assert res["RSI(14)"]["anterior"] == pytest.approx(r[-2], abs=0.005)
    s = ref_sma(c, 50)
    assert res["SMA(50)"]["valor"] == pytest.approx(s[-1], abs=1e-9)
    assert res["SMA(50)"]["anterior"] == pytest.approx(s[-2], abs=1e-9)
    e = ref_ema(c, 20)
    assert res["EMA(20)"]["valor"] == pytest.approx(e[-1], abs=1e-9)
    a = ref_atr(h, lo, c, 14)
    assert res["ATR(14)"]["valor"] == pytest.approx(a[-1], abs=1e-9)
    line, sig, hist = ref_macd(c, 12, 26, 9)
    m = res["MACD(12,26,9)"]
    assert set(m) == {"macd", "sinal", "histograma", "histograma_anterior"}
    assert m["macd"] == pytest.approx(line[-1], abs=1e-9)
    assert m["sinal"] == pytest.approx(sig[-1], abs=1e-9)
    assert m["histograma"] == pytest.approx(hist[-1], abs=1e-9)
    assert m["histograma_anterior"] == pytest.approx(hist[-2], abs=1e-9)
    mid, up, low = ref_bb(c, 20, 2.0)
    b = res["BB(20,2)"]
    assert set(b) == {"media", "superior", "inferior", "largura_pct"}
    assert b["media"] == pytest.approx(mid[-1], abs=1e-9)
    assert b["superior"] == pytest.approx(up[-1], abs=1e-9)
    assert b["inferior"] == pytest.approx(low[-1], abs=1e-9)
    assert b["largura_pct"] == pytest.approx((up[-1] - low[-1]) / mid[-1] * 100, abs=0.005)


def test_compute_values_are_plain_floats(data):
    res = ind.compute(data[0], ["RSI", "MACD"])
    assert type(res["RSI(14)"]["valor"]) is float
    assert type(res["MACD(12,26,9)"]["macd"]) is float


def test_compute_duplicates_and_case_collapse(data):
    res = ind.compute(data[0], ["rsi", "RSI(14)", " RSI ( 14 ) ", "bollinger(20,2)", "BB"])
    assert list(res) == ["RSI(14)", "BB(20,2)"]


def test_compute_rounding_with_digits(data):
    res = ind.compute(data[0], ["RSI", "SMA(20)", "ATR", "MACD", "BB"], digits=2)
    assert res["RSI(14)"]["valor"] == round(res["RSI(14)"]["valor"], 2)
    for key in ("valor", "anterior"):
        assert res["SMA(20)"][key] == round(res["SMA(20)"][key], 3)
        assert res["ATR(14)"][key] == round(res["ATR(14)"][key], 3)
    for key in ("macd", "sinal", "histograma", "histograma_anterior"):
        assert res["MACD(12,26,9)"][key] == round(res["MACD(12,26,9)"][key], 4)
    b = res["BB(20,2)"]
    assert b["media"] == round(b["media"], 3)
    assert b["superior"] == round(b["superior"], 3)
    assert b["largura_pct"] == round(b["largura_pct"], 4)


def test_compute_digits_none_does_not_round(data):
    res = ind.compute(data[0], ["SMA(20)"], digits=None)
    assert res["SMA(20)"]["valor"] != round(res["SMA(20)"]["valor"], 3)


def test_compute_rsi_always_two_decimals_even_with_digits_5(data):
    res = ind.compute(data[0], ["RSI"], digits=5)
    assert res["RSI(14)"]["valor"] == round(res["RSI(14)"]["valor"], 2)


@pytest.mark.parametrize(
    "spec,min_needed",
    [("RSI(14)", 15), ("SMA(50)", 50), ("EMA(20)", 20), ("ATR(14)", 14), ("MACD", 34), ("BB(20,2)", 20)],
)
def test_compute_insufficient_data(data, spec, min_needed):
    df = data[0].iloc[: min_needed - 1]
    with pytest.raises(ValueError, match="dados insuficientes") as exc:
        ind.compute(df, [spec])
    assert str(min_needed) in str(exc.value)
    assert ind.parse_spec(spec).label in str(exc.value)


def test_compute_minimum_data_works(data):
    res = ind.compute(data[0].iloc[:15], ["RSI(14)"])
    assert res["RSI(14)"]["anterior"] is None
    assert 0 <= res["RSI(14)"]["valor"] <= 100


def test_compute_flat_prices():
    df = pd.DataFrame({"open": [1.1] * 60, "high": [1.1] * 60, "low": [1.1] * 60, "close": [1.1] * 60})
    res = ind.compute(df, ["RSI", "BB", "ATR", "MACD"])
    assert res["RSI(14)"]["valor"] == 50.0
    assert res["BB(20,2)"]["largura_pct"] == 0.0
    assert res["ATR(14)"]["valor"] == 0.0
    assert res["MACD(12,26,9)"]["histograma"] == pytest.approx(0.0, abs=1e-12)


def test_compute_invalid_spec_raises(data):
    with pytest.raises(ValueError):
        ind.compute(data[0], ["MACD(26,12,9)"])
