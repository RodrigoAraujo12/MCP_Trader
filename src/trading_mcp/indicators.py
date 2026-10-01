"""Indicadores técnicos (puro pandas/numpy), compatíveis com TradingView/TA-Lib."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

_KINDS = ("RSI", "SMA", "EMA", "MACD", "ATR", "BB")
_ALIASES = {"BOLLINGER": "BB"}
_SPEC_RE = re.compile(r"^([A-Z]+)(?:\(([^()]*)\))?$")


# --------------------------------------------------------------------------- séries


def _as_array(series: pd.Series) -> np.ndarray:
    return np.array(series, dtype=float)


def _check_period(period: int, minimum: int = 1) -> None:
    if not isinstance(period, (int, np.integer)) or isinstance(period, bool) or period < minimum:
        raise ValueError(f"O período deve ser um inteiro >= {minimum} (recebido: {period!r}).")


def _smoothed(series: pd.Series, period: int, alpha: float) -> pd.Series:
    """Média exponencial semeada com a SMA dos primeiros ``period`` valores não-NaN."""
    values = _as_array(series)
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) >= period:
        start = int(valid[period - 1])
        prev = float(np.mean(values[valid[:period]]))
        out[start] = prev
        for i in range(start + 1, len(values)):
            x = values[i]
            if math.isnan(x):
                break  # dado faltante no meio: não propaga valores inventados
            prev = alpha * x + (1.0 - alpha) * prev
            out[i] = prev
    return pd.Series(out, index=series.index)


def sma(close: pd.Series, period: int) -> pd.Series:
    """Média móvel simples."""
    _check_period(period)
    values = _as_array(close)
    out = np.full(len(values), np.nan)
    if len(values) >= period:
        windows = np.lib.stride_tricks.sliding_window_view(values, period)
        out[period - 1:] = windows.mean(axis=1)
    return pd.Series(out, index=close.index)


def ema(close: pd.Series, period: int) -> pd.Series:
    """Média móvel exponencial (alpha=2/(period+1), semente SMA)."""
    _check_period(period)
    return _smoothed(close, period, 2.0 / (period + 1))


def rma(series: pd.Series, period: int) -> pd.Series:
    """Média móvel de Wilder (alpha=1/period, semente SMA)."""
    _check_period(period)
    return _smoothed(series, period, 1.0 / period)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder, com as regras de borda do TradingView.

    Exceção: série sem nenhuma variação (ganho e perda médios zero) dá 50, como no MT5,
    em vez dos 100 do TradingView, que seriam lidos como "sobrecomprado".
    """
    _check_period(period, 2)
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _as_array(rma(gain, period))
    avg_loss = _as_array(rma(loss, period))
    out = np.full(len(avg_gain), np.nan)
    for i in range(len(out)):
        g, lo = avg_gain[i], avg_loss[i]
        if math.isnan(g) or math.isnan(lo):
            continue
        if lo == 0 and g == 0:
            out[i] = 50.0
        elif lo == 0:
            out[i] = 100.0
        elif g == 0:
            out[i] = 0.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + g / lo)
    return pd.Series(out, index=close.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD: colunas macd, signal, hist."""
    _check_period(fast)
    _check_period(slow)
    _check_period(signal)
    if fast >= slow:
        raise ValueError("No MACD, o período rápido deve ser menor que o lento.")
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig}, index=close.index)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True Range; o primeiro valor é high-low."""
    h, lo, c = _as_array(high), _as_array(low), _as_array(close)
    out = h - lo
    if len(out) > 1:
        out[1:] = np.maximum.reduce(
            [h[1:] - lo[1:], np.abs(h[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])]
        )
    return pd.Series(out, index=close.index)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range (RMA do true range)."""
    _check_period(period, 2)
    return rma(true_range(high, low, close), period)


def bollinger(close: pd.Series, period: int = 20, std_mult: float = 2.0) -> pd.DataFrame:
    """Bandas de Bollinger (desvio padrão populacional): middle, upper, lower."""
    _check_period(period)
    if not std_mult > 0:
        raise ValueError("O multiplicador do desvio padrão deve ser maior que zero.")
    values = _as_array(close)
    std = np.full(len(values), np.nan)
    if len(values) >= period:
        windows = np.lib.stride_tricks.sliding_window_view(values, period)
        std[period - 1:] = windows.std(axis=1, ddof=0)
    middle = sma(close, period)
    std_s = pd.Series(std, index=close.index)
    return pd.DataFrame(
        {"middle": middle, "upper": middle + std_mult * std_s, "lower": middle - std_mult * std_s},
        index=close.index,
    )


# --------------------------------------------------------------------------- specs


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


@dataclass(frozen=True)
class IndicatorSpec:
    """Especificação já validada de um indicador."""

    kind: str
    params: tuple[float, ...]

    @property
    def label(self) -> str:
        """Rótulo canônico, ex.: ``MACD(12,26,9)``."""
        return f"{self.kind}({','.join(_fmt(p) for p in self.params)})"


def _to_period(text: str, name: str, minimum: int = 1) -> int:
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"Parâmetro inválido em {name}: '{text}' não é um número.") from None
    if not math.isfinite(number) or not number.is_integer() or number < minimum:
        raise ValueError(f"Em {name}, o período deve ser um inteiro >= {minimum} (recebido: {text}).")
    return int(number)


def parse_spec(spec: str) -> IndicatorSpec:
    """Interpreta texto como ``rsi(14)``, ``MACD``, ``BB(20,2)`` (case/espaços ignorados)."""
    cleaned = re.sub(r"\s+", "", str(spec)).upper()
    match = _SPEC_RE.match(cleaned)
    if not match:
        raise ValueError(f"Especificação de indicador inválida: '{spec}'.")
    kind = _ALIASES.get(match.group(1), match.group(1))
    if kind not in _KINDS:
        raise ValueError(
            f"Indicador desconhecido: '{match.group(1)}'. Suportados: RSI, SMA, EMA, MACD, ATR, BB."
        )
    raw = match.group(2)
    parts = [] if not raw else raw.split(",")
    if any(p == "" for p in parts):
        raise ValueError(f"Parâmetros inválidos em '{spec}'.")

    def arity(allowed: tuple[int, ...]) -> None:
        if len(parts) not in allowed:
            raise ValueError(
                f"{kind} aceita {' ou '.join(str(a) for a in allowed)} parâmetro(s); "
                f"recebido: {len(parts)}."
            )

    params: tuple[float, ...]
    if kind in ("SMA", "EMA"):
        if not parts:
            raise ValueError(f"{kind} exige um período, ex.: {kind}(20).")
        arity((1,))
        params = (_to_period(parts[0], kind),)
    elif kind in ("RSI", "ATR"):
        arity((0, 1))
        params = (_to_period(parts[0], kind, 2) if parts else 14,)
    elif kind == "MACD":
        arity((0, 3))
        if parts:
            fast, slow, sig = (_to_period(p, kind) for p in parts)
        else:
            fast, slow, sig = 12, 26, 9
        if fast >= slow:
            raise ValueError("No MACD, o período rápido deve ser menor que o lento.")
        params = (fast, slow, sig)
    else:  # BB
        arity((0, 1, 2))
        period = _to_period(parts[0], kind) if parts else 20
        mult = 2.0
        if len(parts) == 2:
            try:
                mult = float(parts[1])
            except ValueError:
                raise ValueError(f"Em BB, o desvio padrão '{parts[1]}' não é um número.") from None
            if not math.isfinite(mult) or mult <= 0:
                raise ValueError("Em BB, o multiplicador do desvio padrão deve ser maior que zero.")
        params = (period, mult)
    return IndicatorSpec(kind, tuple(float(p) for p in params))


def required_bars(specs: list[str]) -> int:
    """Candles recomendados para valores estáveis (máximo entre specs, mínimo 50).

    EMA/MACD usam 10x o período: com 4x a semente (SMA) ainda deixava erro visível
    no último dígito reportado em relação a uma série longa já convergida.
    """
    best = 50
    for text in specs:
        s = parse_spec(text)
        p = s.params
        if s.kind in ("SMA", "BB"):
            need = int(p[0])
        elif s.kind in ("EMA", "RSI", "ATR"):
            need = 10 * int(p[0])
        else:
            need = 10 * int(p[1]) + int(p[2])
        best = max(best, need)
    return best


def _min_bars(s: IndicatorSpec) -> int:
    """Mínimo absoluto de candles para o primeiro valor válido."""
    p = s.params
    if s.kind in ("SMA", "EMA", "BB", "ATR"):
        return int(p[0])
    if s.kind == "RSI":
        return int(p[0]) + 1
    return int(p[1]) + int(p[2]) - 1


# --------------------------------------------------------------------------- compute


def _last_two(series: pd.Series) -> tuple[float, float | None]:
    cur = float(series.iloc[-1]) if len(series) else math.nan
    prev = float(series.iloc[-2]) if len(series) > 1 else math.nan
    return cur, (None if math.isnan(prev) else prev)


def _r(value: float | None, ndigits: int | None) -> float | None:
    if value is None or ndigits is None:
        return value
    return round(value, ndigits)


def compute(df: pd.DataFrame, specs: list[str], digits: int | None = None) -> dict[str, dict]:
    """Calcula os indicadores pedidos e devolve os últimos valores por rótulo.

    Se o valor mais recente for NaN (dados insuficientes) levanta ``ValueError``.
    Se apenas o valor anterior não existir, ele vem como ``None``.
    """
    parsed: dict[str, IndicatorSpec] = {}
    for text in specs:
        s = parse_spec(text)
        parsed.setdefault(s.label, s)

    price = None if digits is None else digits + 1
    macd_d = None if digits is None else digits + 2
    result: dict[str, dict] = {}
    for label, s in parsed.items():
        p = s.params
        if s.kind == "RSI":
            cur, prev = _last_two(rsi(df["close"], int(p[0])))
            vals = {"valor": _r(cur, 2), "anterior": _r(prev, 2)}
        elif s.kind in ("SMA", "EMA", "ATR"):
            if s.kind == "SMA":
                series = sma(df["close"], int(p[0]))
            elif s.kind == "EMA":
                series = ema(df["close"], int(p[0]))
            else:
                series = atr(df["high"], df["low"], df["close"], int(p[0]))
            cur, prev = _last_two(series)
            vals = {"valor": _r(cur, price), "anterior": _r(prev, price)}
        elif s.kind == "MACD":
            m = macd(df["close"], int(p[0]), int(p[1]), int(p[2]))
            line, _ = _last_two(m["macd"])
            sig, _ = _last_two(m["signal"])
            cur, hist_prev = _last_two(m["hist"])  # histograma NaN => sinal NaN
            vals = {
                "macd": _r(line, macd_d),
                "sinal": _r(sig, macd_d),
                "histograma": _r(cur, macd_d),
                "histograma_anterior": _r(hist_prev, macd_d),
            }
        else:  # BB
            b = bollinger(df["close"], int(p[0]), p[1])
            cur, _ = _last_two(b["middle"])
            up, _ = _last_two(b["upper"])
            lo, _ = _last_two(b["lower"])
            width = (up - lo) / cur * 100 if cur else math.nan
            vals = {
                "media": _r(cur, price),
                "superior": _r(up, price),
                "inferior": _r(lo, price),
                # 4 casas: em M1 a largura fica perto de 0,03% e 2 casas apagariam a informação.
                "largura_pct": _r(width, 4),
            }
        if math.isnan(cur):
            raise ValueError(
                f"dados insuficientes para {label}: são necessários pelo menos {_min_bars(s)} "
                f"candles (recebidos: {len(df)}); para valores estáveis use ~{required_bars([label])}."
            )
        result[label] = vals
    return result
