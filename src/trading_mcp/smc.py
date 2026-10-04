"""Estrutura de mercado no estilo SMC (somente medição; nada aqui é sinal de entrada).

Regras objetivas, com os padrões das implementações mais usadas (indicador "Smart Money Concepts" da LuxAlgo
no TradingView e a biblioteca Python ``smartmoneyconcepts``):

* Topo/fundo (pivô) pelo pavio, confirmado ``tamanho`` candles depois, sem repintar (lógica de "perna" da
  LuxAlgo): micro = 5 candles, macro = 50 (estrutura interna e externa da LuxAlgo).
* Rompimento do último topo/fundo ainda não rompido, por fechamento (padrão da LuxAlgo) e, separado, por pavio
  (como o usuário também lê). Na direção da tendência = BOS; contra = CHoCH, e a tendência vira.
* Order block (escolha do candle da LuxAlgo): no rompimento por fechamento, o candle de mínima mais baixa
  (rompimento para cima) ou de máxima mais alta (para baixo) entre o topo/fundo rompido e o rompimento; candles
  com amplitude de 2 ATR(200) ou mais não contam. Zona de pavio a pavio (como o usuário usa); sai quando um
  candle fecha além dela.
* FVG (LuxAlgo): três candles com espaço entre a máxima do 1º e a mínima do 3º (ou o contrário), o 2º fechando
  além do espaço e com corpo maior que o dobro do corpo médio (em %, como o limiar automático da LuxAlgo).
  Preenchido quando o preço atravessa o espaço inteiro.
* Topos/fundos iguais (LuxAlgo): pivôs de 3 candles a menos de 0,1 ATR(200) do pivô anterior do mesmo tipo;
  três ou mais seguidos viram um grupo só.
* Varredura (convenção SMC): pavio além do nível e fechamento de volta no mesmo candle; fechamento além é
  rompimento (que também é informado quando vem depois de uma varredura).
* Dia de mercado: vira às 17:00 de Nova York (o do usuário); semana de mercado: domingo 17:00. Sessões em
  sequência (Tóquio, Londres, Nova York), cada uma até a abertura da seguinte, no horário local de cada praça.
  Topos/fundos diários pelo candle D1 do MT5 (o toco de domingo entra na segunda nos símbolos que não abrem
  no sábado).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from trading_mcp import indicators, tempo
from trading_mcp.mt5_client import MT5Client, MT5Error

MICRO, MACRO, EQUAL_SIZE = 5, 50, 3
ATR_PERIOD = 200
EQUAL_ATR = 0.1
OB_VOLATILITY_ATR = 2.0
TIMEFRAMES = ("M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1")
DEFAULT_TIMEFRAMES = ("M5", "M15", "H1", "H4")
BARS = 1500
LEVEL_BARS = 4600  # M5 para os níveis: ~16 dias de 24 h, cobre a semana de mercado anterior até no BTC
MAX_EVENTS = 3
MAX_ZONES = 2  # por lado (acima e abaixo do preço)
MAX_POOLS = 3
RECENT_SWEEP_BARS = 60
DAILY_SWING_SIZE = 3
DAILY_BARS = 300
MAX_DAILY = 3  # topos/fundos diários intactos por lado
_MARKET_ROLL_HOUR = 17
# Sessões em sequência, cada uma até a abertura da seguinte (assim dá para ver uma varrer a anterior): abertura
# no horário local de cada praça, com o horário de verão de cada uma. Nova York vai até a virada (17:00).
SESSIONS = (
    ("asia", ZoneInfo("Asia/Tokyo"), 9),
    ("londres", ZoneInfo("Europe/London"), 8),
    ("nova_york", tempo.NOVA_YORK, 8),
)

NOTES = [
    "Medição da estrutura, não sinal de entrada: a decisão continua sua. Só candles fechados; topos e fundos só "
    "aparecem depois de confirmados (5 candles no micro, 50 no macro).",
    "Rompimentos por fechamento (padrão da LuxAlgo) e por pavio aparecem separados. BOS = rompimento a favor da "
    "tendência; CHoCH = contra (a tendência vira).",
    "Order block: candle escolhido pela regra da LuxAlgo (o extremo entre o topo/fundo rompido e o rompimento), zona "
    "de pavio a pavio, sai quando um candle fecha além dela; pode diferir do 'último candle contrário' do ICT.",
    "Dia de mercado vira às 17:00 de Nova York (semana: domingo 17:00). Sessões em sequência, cada uma até a abertura "
    "da seguinte: Ásia = abertura de Tóquio (9 h local) até a de Londres (8 h local); Londres até a de Nova York (8 h "
    "local); Nova York até as 17 h. Das 17 h de Nova York à abertura de Tóquio fica fora das sessões. Varredura = "
    "pavio além da máxima/mínima e fechamento de volta no mesmo candle M5; fechamento além = rompimento.",
    "Topos e fundos diários: pivôs do candle diário do MT5 (vira às 00:00 UTC, 21:00 de Brasília; o toco de domingo "
    "entra na segunda) com 3 dias de cada lado; aparecem só os que nenhum candle, nem o de hoje, passou.",
    "Os símbolos são CFDs da corretora: a estrutura pode diferir da do mercado à vista ou do futuro.",
]


# --------------------------------------------------------------------------- blocos
def pivots(high: np.ndarray, low: np.ndarray, size: int) -> list[tuple[int, int, str]]:
    """Topos e fundos confirmados: (candle da confirmação, candle do pivô, 'topo' ou 'fundo'), em ordem.

    Lógica de perna da LuxAlgo: o candle ``t - size`` é topo quando a máxima dele passa a maior máxima dos
    ``size`` candles seguintes (vira perna de baixa), e fundo no espelho. A perna começa como de baixa.
    """
    n = len(high)
    if n <= size:
        return []
    after_high = pd.Series(high).rolling(size).max().to_numpy()
    after_low = pd.Series(low).rolling(size).min().to_numpy()
    out: list[tuple[int, int, str]] = []
    leg = "baixa"
    for t in range(size, n):
        p = t - size
        new = leg
        if high[p] > after_high[t]:
            new = "baixa"
        elif low[p] < after_low[t]:
            new = "alta"
        if new != leg:
            out.append((t, p, "topo" if new == "baixa" else "fundo"))
            leg = new
    return out


def _parsed(high: np.ndarray, low: np.ndarray, atr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Máxima/mínima para escolher o order block: candles de amplitude >= 2 ATR trocam as duas (não são escolhidos)."""
    with np.errstate(invalid="ignore"):
        wide = (high - low) >= OB_VOLATILITY_ATR * atr
    return np.where(wide, low, high), np.where(wide, high, low)


def structure(
    df: pd.DataFrame, piv: list[tuple[int, int, str]], mode: str, atr: np.ndarray | None = None
) -> dict[str, Any]:
    """Tendência, rompimentos (BOS/CHoCH) e, por fechamento, order blocks.

    ``mode``: 'fechamento' (rompe quando o candle fecha além do nível) ou 'pavio' (quando a máxima/mínima passa).
    Um candle que passa o topo e o fundo pelo pavio: a ordem segue a cor dele (de alta = desceu antes de subir).
    """
    open_, high, low, close = (df[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close"))
    confirmed: dict[int, list[tuple[int, str]]] = {}
    for t, p, kind in piv:
        confirmed.setdefault(t, []).append((p, kind))
    by_close = mode == "fechamento"
    parsed_high, parsed_low = _parsed(high, low, atr) if (by_close and atr is not None) else (high, low)
    levels: dict[str, dict[str, Any] | None] = {"topo": None, "fundo": None}
    state = {"trend": 0}
    events: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []

    def broke(kind: str, t: int) -> bool:
        item = levels[kind]
        if item is None or item["rompido"]:
            return False
        if kind == "topo":
            return (close[t] if by_close else high[t]) > item["nivel"]
        return (close[t] if by_close else low[t]) < item["nivel"]

    def register(kind: str, t: int) -> None:
        item = levels[kind]
        up = kind == "topo"
        against = state["trend"] == (-1 if up else 1)
        events.append({"tipo": "CHoCH" if against else "BOS", "direcao": "alta" if up else "baixa",
                       "nivel": item["nivel"], "pivo_i": item["i"], "i": t})
        state["trend"], item["rompido"] = (1 if up else -1), True
        if by_close:
            segment = slice(item["i"], t)
            k = item["i"] + int(np.argmin(parsed_low[segment]) if up else np.argmax(parsed_high[segment]))
            blocks.append({"direcao": "alta" if up else "baixa", "i": k, "criado_i": t,
                           "topo": max(parsed_high[k], parsed_low[k]), "fundo": min(parsed_high[k], parsed_low[k])})

    for t in range(len(df)):
        for p, kind in confirmed.get(t, ()):
            levels[kind] = {"nivel": high[p] if kind == "topo" else low[p], "i": p, "rompido": False}
        order = ("topo", "fundo")
        if not by_close and close[t] >= open_[t]:
            order = ("fundo", "topo")
        for kind in order:
            if broke(kind, t):
                register(kind, t)
    return {"tendencia": state["trend"], "eventos": events, "topo": levels["topo"], "fundo": levels["fundo"],
            "order_blocks": blocks}


def block_status(df: pd.DataFrame, block: dict[str, Any]) -> dict[str, Any]:
    """Mitigação (fechamento além da zona) e toque (pavio dentro dela) depois da criação do order block."""
    after = df.iloc[block["criado_i"] + 1:]
    if block["direcao"] == "alta":
        mitigated = after.index[after["close"] < block["fundo"]]
        touched = after.index[after["low"] <= block["topo"]]
    else:
        mitigated = after.index[after["close"] > block["topo"]]
        touched = after.index[after["high"] >= block["fundo"]]
    return {
        "mitigado_i": int(mitigated[0]) if len(mitigated) else None,
        "tocado_i": int(touched[0]) if len(touched) else None,
    }


def fair_value_gaps(df: pd.DataFrame) -> list[dict[str, Any]]:
    """FVGs (regra da LuxAlgo) com quanto já foi preenchido e quando foi atravessado."""
    o, h, l, c = (df[col].to_numpy(dtype=float) for col in ("open", "high", "low", "close"))
    body = np.zeros_like(c)
    np.divide(c - o, o, out=body, where=o != 0)
    body *= 100
    # Limiar automático da LuxAlgo: o dobro da média de |corpo %| até o candle do meio, inclusive.
    cumulative = np.cumsum(np.abs(body))
    out = []
    for t in range(2, len(df)):
        mid = t - 1
        threshold = 2 * cumulative[mid] / (mid + 1)
        if l[t] > h[t - 2] and c[mid] > h[t - 2] and body[mid] > threshold:
            gap = {"direcao": "alta", "i": mid, "criado_i": t, "topo": l[t], "fundo": h[t - 2]}
        elif h[t] < l[t - 2] and c[mid] < l[t - 2] and -body[mid] > threshold:
            gap = {"direcao": "baixa", "i": mid, "criado_i": t, "topo": l[t - 2], "fundo": h[t]}
        else:
            continue
        after = df.iloc[t + 1:]
        size = gap["topo"] - gap["fundo"]
        if gap["direcao"] == "alta":
            through = after.index[after["low"] < gap["fundo"]]
            deepest = after["low"].min() if len(after) else np.inf
            filled = (gap["topo"] - deepest) / size
        else:
            through = after.index[after["high"] > gap["topo"]]
            deepest = after["high"].max() if len(after) else -np.inf
            filled = (deepest - gap["fundo"]) / size
        gap["preenchido_i"] = int(through[0]) if len(through) else None
        gap["preenchido_pct"] = round(float(min(max(filled, 0.0), 1.0)) * 100, 1) if size > 0 else 100.0
        out.append(gap)
    return out


def equal_levels(df: pd.DataFrame, atr: np.ndarray) -> list[dict[str, Any]]:
    """Topos/fundos iguais: pivôs de 3 candles a menos de 0,1 ATR(200) do anterior do mesmo tipo (grupos seguidos
    viram um só, no nível mais extremo)."""
    high, low = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    last: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for t, p, kind in pivots(high, low, EQUAL_SIZE):
        prices = high if kind == "topo" else low
        if kind in last and not np.isnan(atr[t]) and abs(prices[p] - prices[last[kind]]) < EQUAL_ATR * atr[t]:
            pick = max if kind == "topo" else min
            name = "topos_iguais" if kind == "topo" else "fundos_iguais"
            previous = next((g for g in reversed(out) if g["tipo"] == name), None)
            if previous is not None and previous["pontos_i"][-1] == last[kind]:
                previous["pontos_i"].append(p)
                previous["nivel"] = pick(previous["nivel"], prices[p])
                previous["confirmado_i"] = t
            else:
                out.append({"tipo": name, "nivel": pick(prices[p], prices[last[kind]]),
                            "pontos_i": [last[kind], p], "confirmado_i": t})
        last[kind] = p
    return out


def crossings(df: pd.DataFrame, level: float, side: str, start: int) -> dict[str, int | None]:
    """A partir de ``start``: o primeiro candle cujo pavio passou o nível e fechou de volta (varredura, se veio antes
    de qualquer fechamento além) e o primeiro que fechou além (rompimento)."""
    after = df.iloc[start:]
    if side == "acima":
        wick, beyond = after.index[after["high"] > level], after.index[after["close"] > level]
    else:
        wick, beyond = after.index[after["low"] < level], after.index[after["close"] < level]
    first_wick = int(wick[0]) if len(wick) else None
    first_close = int(beyond[0]) if len(beyond) else None
    sweep = first_wick if first_wick is not None and first_wick != first_close else None
    return {"varredura_i": sweep, "rompimento_i": first_close}


def _merge_sundays(d1: pd.DataFrame) -> pd.DataFrame:
    """Junta o toco de domingo do D1 ao candle de segunda, se o símbolo não negocia no sábado."""
    days = d1["time"].dt.weekday
    if (days == 5).any() or not (days == 6).any():
        return d1.reset_index(drop=True)
    rows = []
    pending = None
    for row in d1.itertuples(index=False):
        if row.time.weekday() == 6:
            pending = row
            continue
        if pending is not None:
            row = row._replace(open=pending.open, high=max(row.high, pending.high), low=min(row.low, pending.low))
            pending = None
        rows.append(row)
    if pending is not None:
        rows.append(pending)
    return pd.DataFrame(rows, columns=d1.columns)


# --------------------------------------------------------------------------- dia, semana, sessões
def market_day_start(moment: datetime) -> datetime:
    """Início do dia de mercado (17:00 de Nova York) que contém ``moment``."""
    local = moment.astimezone(tempo.NOVA_YORK)
    start = local.replace(hour=_MARKET_ROLL_HOUR, minute=0, second=0, microsecond=0)
    if local < start:
        start = (start - timedelta(days=1)).replace(hour=_MARKET_ROLL_HOUR)
    return start.astimezone(tempo.UTC)


def market_week_start(moment: datetime) -> datetime:
    """Início da semana de mercado (domingo 17:00 de Nova York) que contém ``moment``."""
    day = market_day_start(moment).astimezone(tempo.NOVA_YORK)
    # O dia de mercado que começa domingo 17:00 é o primeiro da semana (weekday 6 = domingo).
    back = (day.weekday() - 6) % 7
    return (day - timedelta(days=back)).replace(hour=_MARKET_ROLL_HOUR).astimezone(tempo.UTC)


def session_windows(day_start: datetime) -> list[tuple[str, datetime, datetime]]:
    """Sessões do dia de mercado que começa em ``day_start`` (17:00 de Nova York da véspera), em UTC."""
    date = (day_start.astimezone(tempo.NOVA_YORK) + timedelta(days=1)).date()
    opens = [
        (name, datetime(date.year, date.month, date.day, hour, tzinfo=zone).astimezone(tempo.UTC))
        for name, zone, hour in SESSIONS
    ]
    close = datetime(date.year, date.month, date.day, _MARKET_ROLL_HOUR, tzinfo=tempo.NOVA_YORK).astimezone(tempo.UTC)
    ends = [at for _, at in opens[1:]] + [close]
    return [(name, start, end) for (name, start), end in zip(opens, ends)]


def _window(df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    return df[(df["time"] >= pd.Timestamp(start)) & (df["time"] < pd.Timestamp(end))]


# --------------------------------------------------------------------------- relatório
class _Fmt:
    def __init__(self, df: pd.DataFrame, digits: int, price: float | None) -> None:
        self.times = df["time"]
        self.digits = digits
        self.price = price

    def t(self, i: int) -> str:
        return tempo.iso_utc(self.times.iat[i].to_pydatetime())

    def p(self, value: float) -> float:
        return round(float(value), self.digits)

    def dist(self, value: float) -> float | None:
        return round((float(value) / self.price - 1) * 100, 3) if self.price else None


def _trend_name(value: int) -> str:
    return {1: "alta", -1: "baixa"}.get(value, "indefinida")


def _structure_report(
    df: pd.DataFrame, size: int, fmt: _Fmt, atr: np.ndarray
) -> tuple[dict[str, Any], list, dict[str, Any]]:
    high, low = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    piv = pivots(high, low, size)
    by_close = structure(df, piv, "fechamento", atr)
    by_wick = structure(df, piv, "pavio")

    def level(item: dict[str, Any] | None, closed: dict[str, Any] | None) -> dict[str, Any] | None:
        if item is None:
            return None
        return {"preco": fmt.p(item["nivel"]), "em": fmt.t(item["i"]),
                "rompido_por_fechamento": closed["rompido"], "rompido_por_pavio": item["rompido"]}

    # Um rompimento por topo/fundo: quando foi passado pelo pavio e quando fechou além (cada leitura tem a sua
    # tendência, então o mesmo rompimento pode ser BOS numa e CHoCH na outra).
    merged: dict[tuple[int, str], dict[str, Any]] = {}
    for mode, found in (("por_pavio", by_wick), ("por_fechamento", by_close)):
        for e in found["eventos"]:
            item = merged.setdefault((e["pivo_i"], e["direcao"]), {"direcao": e["direcao"], "nivel": e["nivel"],
                                                                   "pivo_i": e["pivo_i"], "por_pavio": None,
                                                                   "por_fechamento": None})
            item[mode] = {"tipo": e["tipo"], "i": e["i"]}
    first = lambda m: min(v["i"] for v in (m["por_pavio"], m["por_fechamento"]) if v)  # noqa: E731
    recent = sorted(merged.values(), key=first)[-MAX_EVENTS:]
    report = {
        "tendencia_por_fechamento": _trend_name(by_close["tendencia"]),
        "tendencia_por_pavio": _trend_name(by_wick["tendencia"]),
        "topo_atual": level(by_wick["topo"], by_close["topo"]),
        "fundo_atual": level(by_wick["fundo"], by_close["fundo"]),
        "ultimos_rompimentos": [
            {
                "direcao": m["direcao"], "nivel": fmt.p(m["nivel"]), "topo_fundo_em": fmt.t(m["pivo_i"]),
                **{mode: ({"tipo": m[mode]["tipo"], "em": fmt.t(m[mode]["i"])} if m[mode] else None)
                   for mode in ("por_pavio", "por_fechamento")},
            }
            for m in recent
        ],
    }
    return report, by_close["order_blocks"], {"topo": by_close["topo"], "fundo": by_close["fundo"]}


def _nearest(items: list[dict[str, Any]], price: float | None) -> list[dict[str, Any]]:
    """Até MAX_ZONES acima e MAX_ZONES abaixo do preço, os mais próximos primeiro."""
    if price is None:
        return items[-2 * MAX_ZONES:]
    above = sorted((z for z in items if z["fundo"] > price), key=lambda z: z["fundo"])[:MAX_ZONES]
    below = sorted((z for z in items if z["topo"] < price), key=lambda z: -z["topo"])[:MAX_ZONES]
    inside = [z for z in items if z["fundo"] <= price <= z["topo"]][:MAX_ZONES]
    return inside + above + below


def _premium_discount(df: pd.DataFrame, macro: dict[str, Any], price: float | None, fmt: _Fmt) -> dict[str, Any] | None:
    """Posição do preço na faixa macro: do último fundo ao último topo, esticada pelos extremos depois deles (como os
    extremos móveis da LuxAlgo), para a faixa acompanhar um rompimento."""
    top, bottom = macro["topo"], macro["fundo"]
    if price is None or top is None or bottom is None:
        return None
    high = max(top["nivel"], float(df["high"].iloc[top["i"]:].max()))
    low = min(bottom["nivel"], float(df["low"].iloc[bottom["i"]:].min()))
    if high <= low:
        return None
    position = (price - low) / (high - low) * 100
    zone = ("acima_da_faixa" if position > 100 else "abaixo_da_faixa" if position < 0 else
            "premium" if position > 52.5 else "discount" if position < 47.5 else "equilibrio")
    return {"faixa": [fmt.p(low), fmt.p(high)], "posicao_pct": round(float(position), 1), "zona": zone}


def analyze(df: pd.DataFrame, digits: int, price: float | None) -> dict[str, Any]:
    """Estrutura de um timeframe a partir de candles fechados (do mais antigo ao mais novo)."""
    df = df.reset_index(drop=True)
    fmt = _Fmt(df, digits, price)
    atr = indicators.atr(df["high"], df["low"], df["close"], ATR_PERIOD).to_numpy(dtype=float)
    micro, micro_blocks, _ = _structure_report(df, MICRO, fmt, atr)
    macro, macro_blocks, macro_levels = _structure_report(df, MACRO, fmt, atr)
    out: dict[str, Any] = {
        "candles": len(df),
        "ultimo_candle_fechado": fmt.t(len(df) - 1),
        "estrutura": {"micro": micro, "macro": macro},
    }
    zone = _premium_discount(df, macro_levels, price, fmt)
    if zone:
        out["premium_discount"] = zone

    gaps = [g for g in fair_value_gaps(df) if g["preenchido_i"] is None]
    out["fvg_abertos"] = [
        {"direcao": g["direcao"], "de": fmt.p(g["fundo"]), "ate": fmt.p(g["topo"]), "candle_em": fmt.t(g["i"]),
         "preenchido_pct": g["preenchido_pct"], "distancia_pct": fmt.dist((g["topo"] + g["fundo"]) / 2)}
        for g in _nearest(gaps, price)
    ]

    # O mesmo candle pode ser order block no micro e no macro: aparece uma vez.
    blocks: dict[tuple[str, int], dict[str, Any]] = {}
    for name, items in (("micro", micro_blocks), ("macro", macro_blocks)):
        for b in items:
            status = block_status(df, b)
            if status["mitigado_i"] is not None:
                continue
            key = (b["direcao"], b["i"])
            if key in blocks:
                blocks[key]["estrutura"] = "micro e macro"
                continue
            blocks[key] = {**b, "estrutura": name, "tocado": status["tocado_i"] is not None}
    out["order_blocks"] = [
        {"direcao": b["direcao"], "estrutura": b["estrutura"], "de": fmt.p(b["fundo"]), "ate": fmt.p(b["topo"]),
         "candle_em": fmt.t(b["i"]), "tocado": b["tocado"], "distancia_pct": fmt.dist((b["topo"] + b["fundo"]) / 2)}
        for b in _nearest(list(blocks.values()), price)
    ]

    # Liquidez igual ainda não tomada (a mais perto do preço); as varridas há pouco vão para as varreduras.
    recent = len(df) - RECENT_SWEEP_BARS
    active: list[dict[str, Any]] = []
    sweeps: dict[tuple[str, str], dict[str, Any]] = {}

    def add_sweep(kind: str, level: float, origin: int, crossed: dict[str, int | None]) -> None:
        i = crossed["varredura_i"]
        if i is None or i < recent:
            return
        side = "acima" if kind in ("topo", "topos_iguais") else "abaixo"
        item = {"de": kind, "nivel": fmt.p(level), "topo_fundo_em": fmt.t(origin), "varrido_em": fmt.t(i)}
        if crossed["rompimento_i"] is not None:
            item["rompido_depois_em"] = fmt.t(crossed["rompimento_i"])
        sweeps.setdefault((item["varrido_em"], side), item)  # a mesma varredura conta uma vez

    for pool in equal_levels(df, atr):
        side = "acima" if pool["tipo"] == "topos_iguais" else "abaixo"
        crossed = crossings(df, pool["nivel"], side, pool["confirmado_i"] + 1)
        if crossed["varredura_i"] is None and crossed["rompimento_i"] is None:
            active.append({"tipo": pool["tipo"], "nivel": fmt.p(pool["nivel"]),
                           "pontos_em": [fmt.t(i) for i in pool["pontos_i"]], "distancia_pct": fmt.dist(pool["nivel"])})
        else:
            add_sweep(pool["tipo"], pool["nivel"], pool["pontos_i"][-1], crossed)
    if price is not None:
        active.sort(key=lambda a: abs(a["nivel"] - price))
    out["liquidez_igual"] = active[: 2 * MAX_ZONES] if price is not None else active[-2 * MAX_ZONES:]

    # Varreduras recentes dos topos/fundos micro (pavio além e fechamento de volta).
    high, low = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    for t, p, kind in pivots(high, low, MICRO):
        level = high[p] if kind == "topo" else low[p]
        add_sweep(kind, level, p, crossings(df, level, "acima" if kind == "topo" else "abaixo", t + 1))
    out["varreduras_recentes"] = sorted(sweeps.values(), key=lambda s: s["varrido_em"])[-MAX_POOLS:]
    return out


def key_levels(m5: pd.DataFrame, d1: pd.DataFrame, digits: int, now: datetime, price: float | None,
               d1_has_forming: bool = False) -> dict[str, Any]:
    """Máxima/mínima do dia e da semana de mercado (atuais e anteriores), das sessões (hoje e no dia anterior) e os
    topos e fundos diários ainda intactos. ``m5`` só com candles fechados.

    Para cada máxima/mínima de um período encerrado: se e quando foi varrida e/ou rompida (M5).
    """
    m5 = m5.reset_index(drop=True)
    fmt = _Fmt(m5, digits, price)
    first_bar = m5["time"].iloc[0].to_pydatetime() if len(m5) else None

    def describe(hi: float, lo: float, start: datetime, end: datetime, closed: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {"de": tempo.iso_utc(start), "ate": tempo.iso_utc(end),
                               "maxima": fmt.p(hi), "minima": fmt.p(lo)}
        if first_bar is not None and first_bar > start:
            out["incompleto"] = True  # o histórico M5 do terminal não chega ao início do período
        later = m5.index[m5["time"] >= pd.Timestamp(end)]
        if closed and len(later):
            for name, level, side in (("maxima", hi, "acima"), ("minima", lo, "abaixo")):
                crossed = crossings(m5, level, side, int(later[0]))
                if crossed["varredura_i"] is not None:
                    out[f"{name}_varrida_em"] = fmt.t(crossed["varredura_i"])
                if crossed["rompimento_i"] is not None:
                    out[f"{name}_rompida_em"] = fmt.t(crossed["rompimento_i"])
        return out

    def from_window(start: datetime, end: datetime, closed: bool = True) -> dict[str, Any] | None:
        window = _window(m5, start, end)
        if window.empty:
            return None
        return describe(float(window["high"].max()), float(window["low"].min()), start, end, closed)

    def sessions(day_start: datetime) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, s_at, e_at in session_windows(day_start):
            if s_at > now:
                out[name] = {"situacao": "ainda_nao_comecou", "de": tempo.iso_utc(s_at), "ate": tempo.iso_utc(e_at)}
                continue
            running = e_at > now
            found = from_window(s_at, min(e_at, now), closed=not running)
            if found is None:
                out[name] = {"situacao": "sem_negociacao", "de": tempo.iso_utc(s_at), "ate": tempo.iso_utc(e_at)}
                continue
            found["ate"] = tempo.iso_utc(e_at)
            out[name] = {"situacao": "em_andamento" if running else "concluida", **found}
        return out

    levels: dict[str, Any] = {}
    today = market_day_start(now)
    levels["dia_mercado_atual"] = from_window(today, now, closed=False) or {"de": tempo.iso_utc(today)}
    # Dia de mercado anterior com negociação (pula fim de semana e feriados).
    end = today
    for _ in range(7):
        begin = market_day_start(end - timedelta(minutes=1))
        found = from_window(begin, end)
        if found is not None:
            levels["dia_mercado_anterior"] = found
            levels["sessoes_dia_anterior"] = sessions(begin)
            break
        end = begin
    levels["sessoes_hoje"] = sessions(today)
    week = market_week_start(now)
    # No fim de semana, a "semana atual" é a que acabou de fechar na sexta.
    levels["semana_mercado_atual"] = from_window(week, now, closed=False) or {"de": tempo.iso_utc(week)}
    levels["semana_mercado_anterior"] = from_window(market_week_start(week - timedelta(minutes=1)), week)
    if len(d1):
        levels["topos_fundos_diarios"] = daily_swings(d1.reset_index(drop=True), digits, price, d1_has_forming)
    return levels


def daily_swings(d1: pd.DataFrame, digits: int, price: float | None, has_forming: bool = False) -> list[dict[str, Any]]:
    """Topos e fundos do D1 que nenhum candle passou, nem o de hoje (``has_forming``: o último ainda está aberto) nem
    o preço atual; os mais perto do preço de cada lado. Pivôs só com candles fechados."""
    closed = d1.iloc[:-1] if has_forming and len(d1) else d1
    merged = _merge_sundays(closed)
    full = pd.concat([merged, d1.iloc[[-1]]], ignore_index=True) if has_forming and len(d1) else merged
    fmt = _Fmt(full, digits, price)
    high, low = merged["high"].to_numpy(dtype=float), merged["low"].to_numpy(dtype=float)
    intact = []
    for t, p, kind in pivots(high, low, DAILY_SWING_SIZE):
        level = high[p] if kind == "topo" else low[p]
        crossed = crossings(full, level, "acima" if kind == "topo" else "abaixo", p + 1)
        if crossed["varredura_i"] is not None or crossed["rompimento_i"] is not None:
            continue
        if price is not None and (price > level if kind == "topo" else price < level):
            continue
        intact.append({"tipo": kind, "preco": fmt.p(level), "dia": fmt.t(p)[:10], "distancia_pct": fmt.dist(level)})
    if price is None:
        return intact[-2 * MAX_DAILY:]
    above = sorted((x for x in intact if x["preco"] > price), key=lambda x: x["preco"])[:MAX_DAILY]
    below = sorted((x for x in intact if x["preco"] <= price), key=lambda x: -x["preco"])[:MAX_DAILY]
    return above[::-1] + below


MAX_TARGETS = 5  # por lado
# Níveis a menos disto (fração do preço) um do outro viram um alvo só.
TARGET_MERGE = 0.0003
_SESSION_NAMES = {"asia": "Ásia", "londres": "Londres", "nova_york": "Nova York"}


def targets(levels: dict[str, Any] | None, frames: dict[str, Any], price: float | None, digits: int,
            entry: float | None = None, stop: float | None = None) -> dict[str, Any] | None:
    """Alvos de liquidez acima e abaixo do preço (ou da entrada): máximas/mínimas ainda não tomadas, liquidez igual,
    topos/fundos diários intactos e o início das zonas contrárias (OB/FVG). Com entrada e stop, o risco/retorno."""
    base = entry if entry is not None else price
    if base is None:
        return None
    found: list[tuple[float, str, str]] = []  # (preço, lado, rótulo)

    def add(value: Any, label: str) -> None:
        if value is not None:
            found.append((float(value), "acima" if value > base else "abaixo", label))

    if levels:
        for key, label in (("dia_mercado_anterior", "dia anterior"), ("semana_mercado_anterior", "semana anterior")):
            item = levels.get(key) or {}
            for side, word in (("maxima", "Máx."), ("minima", "Mín.")):
                if item.get(side) is not None and not item.get(f"{side}_varrida_em") and not item.get(f"{side}_rompida_em"):
                    add(item[side], f"{word} {label}")
        for day_key, suffix in (("sessoes_dia_anterior", " (dia anterior)"), ("sessoes_hoje", "")):
            for name, item in (levels.get(day_key) or {}).items():
                if item.get("situacao") != "concluida":
                    continue
                for side, word in (("maxima", "Máx."), ("minima", "Mín.")):
                    if not item.get(f"{side}_varrida_em") and not item.get(f"{side}_rompida_em"):
                        add(item.get(side), f"{word} {_SESSION_NAMES.get(name, name)}{suffix}")
        today = levels.get("dia_mercado_atual") or {}
        add(today.get("maxima"), "Máx. de hoje (até agora)")
        add(today.get("minima"), "Mín. de hoje (até agora)")
        for swing in levels.get("topos_fundos_diarios") or []:
            add(swing["preco"], f"{'Topo' if swing['tipo'] == 'topo' else 'Fundo'} diário de {swing['dia'][8:10]}/{swing['dia'][5:7]}")
    for tf, info in frames.items():
        if "estrutura" not in info:
            continue
        for pool in info.get("liquidez_igual", []):
            add(pool["nivel"], f"{'Topos' if pool['tipo'] == 'topos_iguais' else 'Fundos'} iguais {tf}")
        for kind, key in (("OB", "order_blocks"), ("FVG", "fvg_abertos")):
            for zone in info.get(key, []):
                if zone["direcao"] == "baixa" and zone["de"] > base:
                    add(zone["de"], f"{kind} de baixa {tf} (início)")
                elif zone["direcao"] == "alta" and zone["ate"] < base:
                    add(zone["ate"], f"{kind} de alta {tf} (início)")

    def side_list(side: str) -> list[dict[str, Any]]:
        items = sorted((f for f in found if f[1] == side), key=lambda f: abs(f[0] - base))
        merged: list[dict[str, Any]] = []
        for value, _, label in items:
            if merged and abs(value - merged[-1]["_v"]) <= TARGET_MERGE * base:
                if label not in merged[-1]["tipos"]:
                    merged[-1]["tipos"].append(label)
                continue
            if len(merged) == MAX_TARGETS:
                break
            merged.append({"_v": value, "preco": round(value, digits), "tipos": [label]})
        for item in merged:
            value = item.pop("_v")
            item["distancia"] = round(value - base, digits)
            item["distancia_pct"] = round((value / base - 1) * 100, 3)
            if entry is not None and stop is not None and side == ("acima" if stop < entry else "abaixo"):
                item["risco_retorno"] = round(abs(value - entry) / abs(entry - stop), 2)
        return merged

    out: dict[str, Any] = {"referencia": round(base, digits), "acima": side_list("acima"), "abaixo": side_list("abaixo")}
    if entry is not None and stop is not None:
        buy = stop < entry
        out["operacao"] = {"direcao": "compra" if buy else "venda", "entrada": entry, "stop": stop,
                           "risco": round(abs(entry - stop), digits),
                           "alvos_na_direcao": "acima" if buy else "abaixo"}
    return out


def _data_warnings(frames: Sequence[pd.DataFrame]) -> list[str]:
    notes = []
    if any(not f.attrs.get("conectado", True) for f in frames):
        notes.append("Terminal sem conexão com a corretora: os candles mais recentes podem faltar.")
    if any(f.attrs.get("horario_inconsistente") for f in frames):
        notes.append(
            "Candle com horário à frente do relógio UTC: o servidor pode não estar em UTC e os horários de dia, semana "
            "e sessões ficam errados. Não use sem conferir."
        )
    if any(f.attrs.get("defasado") for f in frames):
        notes.append(
            "Faltam os candles mais recentes (o terminal ainda sincronizava o histórico): máximas, mínimas e estrutura "
            "podem estar desatualizadas. Repita em alguns segundos."
        )
    return notes


def report(mt5: MT5Client, symbol: str, timeframes: Sequence[str] = DEFAULT_TIMEFRAMES,
           entry: float | None = None, stop: float | None = None) -> dict[str, Any]:
    """Estrutura SMC de um símbolo em vários timeframes, com os níveis de liquidez do dia, da semana e das sessões e
    os alvos acima e abaixo (com risco/retorno quando ``entry`` e ``stop`` são informados)."""
    wanted = list(dict.fromkeys(tf.strip().upper() for tf in timeframes if tf.strip()))
    invalid = [tf for tf in wanted if tf not in TIMEFRAMES]
    if not wanted or invalid:
        raise ValueError(f"Timeframes aceitos: {', '.join(TIMEFRAMES)}.")
    if (entry is None) != (stop is None):
        raise ValueError("Para o risco/retorno, informe entrada e stop juntos.")
    if entry is not None and entry == stop:
        raise ValueError("O stop precisa ser diferente da entrada.")
    spec = mt5.symbol_spec(symbol)
    resolved, digits = spec["simbolo"], spec["digitos"]
    now = mt5.now_utc()
    notes = list(NOTES)
    result: dict[str, Any] = {"simbolo": resolved, "coletado": tempo.exibicao(now), "diferenca_utc": tempo.offsets(now)}
    try:
        quote = mt5.quote(resolved)
    except MT5Error as exc:
        price = None
        result["preco"] = None
        notes.append(f"Sem cotação ({exc}): sem distâncias até o preço.")
    else:
        price = quote["bid"] or None
        result["preco"] = {"bid": quote["bid"], "ask": quote["ask"], "estado": quote["estado"]}
        if quote["estado"] != "atual":
            notes.append(f"Cotação {quote['estado']}: as distâncias usam um preço que não é de agora.")
    used: list[pd.DataFrame] = []
    try:
        m5 = mt5.rates(resolved, "M5", LEVEL_BARS, include_current=False)
        d1 = mt5.rates(resolved, "D1", DAILY_BARS, include_current=True)
        used += [m5, d1]
        result["niveis"] = key_levels(m5, d1, digits, now, price, bool(d1.attrs.get("ultimo_em_formacao")))
        if any(v.get("incompleto") for v in result["niveis"].values() if isinstance(v, dict)):
            notes.append("Períodos com `incompleto`: o histórico M5 do terminal não chega ao início deles.")
    except MT5Error as exc:
        result["niveis"] = None
        notes.append(f"Níveis do dia/semana/sessões indisponíveis: {exc}")
    frames = {}
    for tf in wanted:
        try:
            df = mt5.rates(resolved, tf, BARS, include_current=False)
        except MT5Error as exc:
            frames[tf] = {"erro": str(exc)}
            continue
        used.append(df)
        frames[tf] = analyze(df, digits, price)
        if len(df) < MACRO * 4:
            frames[tf]["aviso"] = f"Só {len(df)} candles: a estrutura macro (50 candles por pivô) fica pobre."
    result["timeframes"] = frames
    result["alvos"] = targets(result.get("niveis"), frames, price, digits, entry, stop)
    if result["alvos"]:
        notes.append(
            "Alvos = liquidez ainda não tomada (máximas/mínimas de dia, semana e sessões, topos/fundos iguais e diários "
            "intactos) e o início das zonas contrárias (OB/FVG), onde o preço costuma reagir pela leitura SMC. Não é "
            "previsão e ainda não há taxa de acerto medida para eles."
        )
    result["observacoes"] = notes + _data_warnings(used)
    return result
