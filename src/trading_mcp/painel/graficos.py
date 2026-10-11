"""O que o gráfico do painel desenha: candles e a estrutura SMC com as mesmas regras do ``smc.py``.

O ``smc.report`` resume para o Claude ler (as zonas mais perto do preço, os três últimos rompimentos). Aqui sai o que se
desenha, com o índice de cada candle no array enviado ao navegador: rompimentos por fechamento ligados ao topo/fundo de
origem, topos e fundos macro (HH/HL/LH/LL), order blocks e FVGs ainda ativos, liquidez igual, varreduras recentes e as
caixas das sessões. É medição, não sinal, como no resto do projeto.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from trading_mcp import indicators, smc, tempo
from trading_mcp.mt5_client import MT5Client, MT5Error, candle_warnings

DEFAULT_BARS = 1000
MIN_BARS, MAX_BARS = 200, 3000
MAX_BREAKS = {"micro": 30, "macro": 12}
MAX_SWINGS = 16
MAX_ZONES = 3  # order blocks e FVGs por lado (acima e abaixo do preço)
MAX_POOLS = 4
SWEEP_BARS = 150  # varreduras só nos últimos N candles
MAX_SWEEPS = 6
# Dias de mercado com caixas de sessão, por timeframe (no H1 seis dias de caixas viram ruído).
SESSION_DAYS = {"M1": 6, "M3": 6, "M5": 6, "M15": 6, "M30": 3, "H1": 3}
_SESSION_LABELS = {"asia": "Ásia", "londres": "Londres", "nova_york": "Nova York"}


def epoch(moment: Any) -> int:
    return int(pd.Timestamp(moment).timestamp())


def candles(df: pd.DataFrame, digits: int) -> list[dict[str, Any]]:
    """Candles no formato do Lightweight Charts (``time`` em segundos UTC)."""
    return [
        {"time": epoch(row.time), "open": round(float(row.open), digits), "high": round(float(row.high), digits),
         "low": round(float(row.low), digits), "close": round(float(row.close), digits)}
        for row in df.itertuples(index=False)
    ]


def _closest(zones: list[dict[str, Any]], price: float | None, per_side: int) -> list[dict[str, Any]]:
    """As zonas que contêm o preço e as ``per_side`` mais perto acima e abaixo (sem preço: as mais recentes)."""
    if price is None:
        return sorted(zones, key=lambda z: z["i"])[-2 * per_side:]
    inside = [z for z in zones if z["de"] <= price <= z["ate"]]
    above = sorted((z for z in zones if z["de"] > price), key=lambda z: z["de"])[:per_side]
    below = sorted((z for z in zones if z["ate"] < price), key=lambda z: -z["ate"])[:per_side]
    return inside + above + below


def _swing_labels(high: np.ndarray, low: np.ndarray, size: int) -> list[dict[str, Any]]:
    """Topos e fundos confirmados, rotulados contra o anterior do mesmo tipo (HH/LH, HL/LL)."""
    out: list[dict[str, Any]] = []
    last: dict[str, float] = {}
    for _, p, kind in smc.pivots(high, low, size):
        value = float(high[p] if kind == "topo" else low[p])
        label = None
        if kind in last:
            if kind == "topo":
                label = "HH" if value > last[kind] else "LH"
            else:
                label = "HL" if value > last[kind] else "LL"
        last[kind] = value
        if label:
            out.append({"i": p, "tipo": kind, "rotulo": label, "preco": value})
    return out


def structure(df: pd.DataFrame, digits: int, price: float | None) -> dict[str, Any]:
    """Desenhos SMC de um timeframe a partir de candles fechados (do mais antigo ao mais novo)."""
    df = df.reset_index(drop=True)
    p = lambda value: round(float(value), digits)  # noqa: E731
    high, low = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    atr = indicators.atr(df["high"], df["low"], df["close"], smc.ATR_PERIOD).to_numpy(dtype=float)

    breaks: list[dict[str, Any]] = []
    blocks: dict[tuple[str, int], dict[str, Any]] = {}
    trend: dict[str, str] = {}
    for name, size in (("micro", smc.MICRO), ("macro", smc.MACRO)):
        found = smc.structure(df, smc.pivots(high, low, size), "fechamento", atr)
        trend[name] = {1: "alta", -1: "baixa"}.get(found["tendencia"], "indefinida")
        for e in found["eventos"][-MAX_BREAKS[name]:]:
            breaks.append({"estrutura": name, "tipo": e["tipo"], "direcao": e["direcao"], "nivel": p(e["nivel"]),
                           "de_i": e["pivo_i"], "ate_i": e["i"]})
        for b in found["order_blocks"]:
            status = smc.block_status(df, b)
            if status["mitigado_i"] is not None:
                continue
            key = (b["direcao"], b["i"])
            if key in blocks:  # o mesmo candle no micro e no macro aparece uma vez
                blocks[key]["estrutura"] = "micro e macro"
                continue
            blocks[key] = {"direcao": b["direcao"], "estrutura": name, "de": p(b["fundo"]), "ate": p(b["topo"]),
                           "i": b["i"], "tocado": status["tocado_i"] is not None}

    gaps = [
        {"direcao": g["direcao"], "de": p(g["fundo"]), "ate": p(g["topo"]), "i": g["i"],
         "preenchido_pct": g["preenchido_pct"]}
        for g in smc.fair_value_gaps(df) if g["preenchido_i"] is None
    ]

    pools: list[dict[str, Any]] = []
    for pool in smc.equal_levels(df, atr):
        side = "acima" if pool["tipo"] == "topos_iguais" else "abaixo"
        crossed = smc.crossings(df, pool["nivel"], side, pool["confirmado_i"] + 1)
        item = {"tipo": "EQH" if side == "acima" else "EQL", "nivel": p(pool["nivel"]),
                "pontos_i": pool["pontos_i"], "varrido_i": crossed["varredura_i"],
                "rompido_i": crossed["rompimento_i"]}
        pools.append(item)
    active = [x for x in pools if x["varrido_i"] is None and x["rompido_i"] is None]
    if price is not None:
        active.sort(key=lambda x: abs(x["nivel"] - price))
    taken = [x for x in pools if x not in active and x["varrido_i"] is not None]

    sweeps: dict[tuple[int, str], dict[str, Any]] = {}
    recent = len(df) - SWEEP_BARS
    for t, piv, kind in smc.pivots(high, low, smc.MICRO):
        level = high[piv] if kind == "topo" else low[piv]
        side = "acima" if kind == "topo" else "abaixo"
        crossed = smc.crossings(df, level, side, t + 1)
        i = crossed["varredura_i"]
        if i is None or i < recent:
            continue
        sweeps.setdefault((i, side), {"i": i, "lado": side, "nivel": p(level), "origem_i": piv,
                                      "rompido_depois_i": crossed["rompimento_i"]})

    swings = _swing_labels(high, low, smc.MACRO)[-MAX_SWINGS:]
    return {
        "tendencia": trend,
        "rompimentos": sorted(breaks, key=lambda b: b["ate_i"]),
        "topos_fundos": [{**s, "preco": p(s["preco"])} for s in swings],
        "order_blocks": _closest(list(blocks.values()), price, MAX_ZONES),
        "fvg": _closest(gaps, price, MAX_ZONES),
        "liquidez": active[:MAX_POOLS] + taken[-2:],
        "varreduras": sorted(sweeps.values(), key=lambda s: s["i"])[-MAX_SWEEPS:],
    }


def sessions(df: pd.DataFrame, digits: int, now: datetime, days: int = 6) -> list[dict[str, Any]]:
    """Caixas das sessões (máxima e mínima de cada uma) nos últimos ``days`` dias de mercado do gráfico."""
    if df.empty:
        return []
    df = df.reset_index(drop=True)
    first = df["time"].iloc[0].to_pydatetime()
    out: list[dict[str, Any]] = []
    start = smc.market_day_start(now)
    starts = []
    for _ in range(days + 3):  # fins de semana e feriados não têm candles e são pulados abaixo
        starts.append(start)
        start = smc.market_day_start(start - timedelta(minutes=1))
    for day_start in reversed(starts):
        if day_start < first - timedelta(days=1):
            continue
        for name, s_at, e_at in smc.session_windows(day_start):
            if s_at > now:
                continue
            window = df[(df["time"] >= pd.Timestamp(s_at)) & (df["time"] < pd.Timestamp(e_at))]
            if window.empty:
                continue
            out.append({"sessao": name, "rotulo": _SESSION_LABELS[name], "de_i": int(window.index[0]),
                        "ate_i": int(window.index[-1]), "maxima": round(float(window["high"].max()), digits),
                        "minima": round(float(window["low"].min()), digits), "em_andamento": e_at > now})
    # Cada dia tem 3 sessões; fica só o último ``days`` dias com candles.
    return out[-3 * days:]


def chart(mt5: MT5Client, symbol: str, timeframe: str, bars: int = DEFAULT_BARS) -> dict[str, Any]:
    """Candles (com o atual, se aberto) e os desenhos SMC calculados só com os candles fechados."""
    tf = (timeframe or "").strip().upper()
    if tf not in smc.TIMEFRAMES:
        raise ValueError(f"Timeframes aceitos no gráfico: {', '.join(smc.TIMEFRAMES)}.")
    bars = max(MIN_BARS, min(int(bars), MAX_BARS))
    spec = mt5.symbol_spec(symbol)
    resolved, digits = spec["simbolo"], spec["digitos"]
    df = mt5.rates(resolved, tf, bars, include_current=True)
    forming = bool(df.attrs.get("ultimo_em_formacao"))
    closed = df.iloc[:-1] if forming else df
    try:
        price = mt5.quote(resolved)["bid"] or None
    except MT5Error:  # sem cotação, as zonas saem pelas mais recentes em vez das mais próximas
        price = None
    now = mt5.now_utc()
    last_open = df["time"].iloc[-1].to_pydatetime()
    return {
        "simbolo": resolved,
        "descricao": spec.get("descricao"),
        "timeframe": tf,
        "digitos": digits,
        "segundos_por_candle": int((tempo.bar_end(last_open, tf) - last_open).total_seconds()),
        "candles": candles(df, digits),
        "ultimo_em_formacao": forming,
        "smc": structure(closed, digits, price),
        "sessoes": sessions(closed, digits, now, SESSION_DAYS[tf]) if tf in SESSION_DAYS else [],
        "coletado_utc": tempo.iso_utc(now),
        "avisos": candle_warnings(df),
    }
