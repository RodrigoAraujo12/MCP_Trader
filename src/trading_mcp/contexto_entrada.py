"""Contexto SMC no momento da entrada de uma operação (journal, etapa D2).

Só entram candles que já tinham fechado quando a operação abriu: nada do que veio depois (nem o candle que estava
aberto na hora). As regras são as de ``smc`` (pivôs, BOS/CHoCH, order blocks, FVG, liquidez, sessões), aplicadas no
horário da primeira entrada e comparadas com a direção da operação. É medição para as estatísticas do journal, não
sinal; e não mede o que o preço fez depois da entrada.

* Estrutura: tendência micro e macro por fechamento (e por pavio, ao lado) em cada timeframe.
* CHoCH: rompimentos contra a tendência anterior que fecharam até 30 min antes da entrada (M1, M3, M5).
* Varredura: liquidez tomada antes da entrada com o preço de volta do outro lado na hora de entrar. Nas 2 h
  antes: máximas/mínimas do dia e da semana de mercado anteriores e das sessões já encerradas (pavio além do nível
  no M1; sem M1, no M5) e, pela regra do ``smc`` (pavio além e fechamento de volta no mesmo candle), topos/fundos
  micro e topos/fundos iguais do M5 e do M15. Nos 30 min antes, os mesmos topos/fundos do M1 e do M3 (o gatilho de
  entrada do usuário), contados à parte nas estatísticas. Nível que depois fechou além, ou com o preço ainda além
  dele na entrada, foi rompimento, não varredura.
* Zonas: order blocks ainda não mitigados e FVGs ainda abertos que contêm o preço de entrada.
* Premium/discount: posição do preço na faixa macro do M15 e do H1.

Os candles do MT5 são de bid e a compra executa no ask: para comparar com os candles, o preço de uma compra é
reduzido pelo spread do último candle M1 (na Exness ele é o spread típico, fixo por símbolo).

Rótulo que dependeria de um timeframe sem histórico (o M1 do terminal cobre ~3 meses) sai ``sem_dados`` em vez de
"nenhum": operações antigas não se misturam às medidas por inteiro.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from trading_mcp import smc, tempo
from trading_mcp.mt5_client import MT5Client, MT5Error

VERSION = 1
FRAMES = ("M1", "M3", "M5", "M15", "H1", "H4")
MIN_BARS = smc.MACRO * 4  # menos que isto: a estrutura macro fica pobre e o timeframe não é avaliado
TREND_FRAMES = ("M5", "M15", "H1", "H4")
CHOCH_FRAMES = ("M1", "M3", "M5")
CHOCH_WINDOW = timedelta(minutes=30)
SWEEP_WINDOW = timedelta(hours=2)
# Janela de cada timeframe para topos/fundos e liquidez igual varridos (M1/M3: gatilho, só os 30 min antes).
SWEEP_FRAMES = {"M1": CHOCH_WINDOW, "M3": CHOCH_WINDOW, "M5": SWEEP_WINDOW, "M15": SWEEP_WINDOW}
_TRIGGER_FRAMES = ("M1", "M3")
PD_FRAMES = ("M15", "H1")
# Rótulos das estatísticas do journal (``dimensions``), nesta ordem.
DIMENSIONS = (
    "sessao", *(f"estrutura_{tf}" for tf in TREND_FRAMES), "estrutura_macro_H1", "varredura_a_favor",
    "choch_a_favor", "choch_contra", "zona_a_favor", "zona_contra", *(f"premium_discount_{tf}" for tf in PD_FRAMES),
)
_SESSION_LEVELS = (("sessoes_dia_anterior", " (dia anterior)"), ("sessoes_hoje", ""))
_PERIOD_LEVELS = (("dia_mercado_anterior", "dia anterior"), ("semana_mercado_anterior", "semana anterior"))
_SESSION_NAMES = {"asia": "Ásia", "londres": "Londres", "nova_york": "Nova York"}
# Ordem de importância da varredura nas estatísticas.
_SWEEP_KINDS = ("nivel_chave", "liquidez_igual", "topo_fundo", "topo_fundo_m1_m3")
# Timeframes que precisam ter sido avaliados para procurar cada tipo (o nível chave usa o M1 e, sem ele, o M5).
_SWEEP_NEEDS = {"nivel_chave": ("M5",), "liquidez_igual": ("M1", "M3", "M5", "M15"),
                "topo_fundo": ("M5", "M15"), "topo_fundo_m1_m3": ("M1", "M3")}

NOTES = [
    "Contexto na hora da primeira entrada, só com candles já fechados nela: nada do que o preço fez depois.",
    "Estrutura = tendência micro (pivôs de 5 candles) e macro (50) por fechamento; a favor/contra compara com a "
    "direção da operação.",
    "CHoCH = rompimento por fechamento contra a tendência anterior, fechado até 30 min antes da entrada (M1, M3, M5).",
    "Varredura = liquidez tomada antes com o preço de volta do outro lado na entrada: nível chave (dia/semana "
    "anteriores, sessões encerradas; 2 h), liquidez igual (topos/fundos iguais), topo/fundo micro do M5/M15 (2 h) "
    "ou do M1/M3 (30 min, topo_fundo_m1_m3). A favor = abaixo numa compra, acima numa venda.",
    "Zona = OB não mitigado ou FVG aberto que contém o preço de entrada (compra comparada pelo bid estimado).",
    "Premium/discount: comprar em discount ou vender em premium = a favor.",
]


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _minutes(delta: timedelta) -> float:
    return round(delta.total_seconds() / 60, 1)


def _session(when: datetime) -> str:
    for name, start, end in smc.session_windows(smc.market_day_start(when)):
        if start <= when < end:
            return name
    return "fora"


def _key_level_sweeps(
    m5: pd.DataFrame, m1: pd.DataFrame | None, digits: int, when: datetime, ref: float
) -> list[dict[str, Any]]:
    """Máximas/mínimas de dia, semana e sessões encerradas tomadas na janela, com o preço de volta na entrada."""
    if m5.empty:
        return []
    empty_d1 = pd.DataFrame(columns=["time", "open", "high", "low", "close"])
    levels = smc.key_levels(m5, empty_d1, digits, when, ref)
    window_start = when - SWEEP_WINDOW
    candidates: list[tuple[str, float, str, datetime, list[str]]] = []
    for key, label in _PERIOD_LEVELS:
        item = levels.get(key) or {}
        for side, word, where in (("maxima", "Máx.", "acima"), ("minima", "Mín.", "abaixo")):
            if item.get(side) is not None:
                taken = [item[k] for k in (f"{side}_varrida_em", f"{side}_rompida_em") if item.get(k)]
                candidates.append((f"{word} {label}", float(item[side]), where, _parse(item["ate"]), taken))
    for key, suffix in _SESSION_LEVELS:
        for name, item in (levels.get(key) or {}).items():
            if item.get("situacao") != "concluida":
                continue
            for side, word, where in (("maxima", "Máx.", "acima"), ("minima", "Mín.", "abaixo")):
                taken = [item[k] for k in (f"{side}_varrida_em", f"{side}_rompida_em") if item.get(k)]
                candidates.append((f"{word} {_SESSION_NAMES.get(name, name)}{suffix}", float(item[side]), where,
                                   _parse(item["ate"]), taken))
    m5_bar, m1_bar = timedelta(minutes=5), timedelta(minutes=1)
    out = []
    for label, level, side, ended, taken in candidates:
        first_m5 = min((_parse(t) for t in taken), default=None)
        if first_m5 is not None and first_m5 + m5_bar <= window_start:
            continue  # já tinha sido tomado antes da janela (o candle M5 inteiro antes dela)
        taken_at, closed = None, None
        if m1 is not None and not m1.empty:
            # Começa no candle M5 que pode atravessar o início da janela: a primeira tomada decide.
            start = max(window_start - m5_bar, ended)
            bars = m1[(m1["time"] >= pd.Timestamp(start)) & (m1["time"] < pd.Timestamp(when))]
            beyond = bars[bars["high"] > level] if side == "acima" else bars[bars["low"] < level]
            if len(beyond):
                taken_at = beyond["time"].iloc[0].to_pydatetime()
                closed = taken_at + m1_bar
        elif first_m5 is not None and first_m5 >= max(window_start, ended):
            taken_at, closed = first_m5, first_m5 + m5_bar
        if taken_at is None or taken_at < window_start:
            continue
        back = ref < level if side == "acima" else ref > level
        if not back:
            continue  # tomado e o preço ficou além: rompimento, não varredura
        out.append({"tipo": "nivel_chave", "nivel": label, "preco": round(level, digits), "lado": side,
                    "tomado_em": tempo.iso_utc(taken_at), "minutos_antes": _minutes(when - closed)})
    return out


def _complete(mt5: MT5Client, symbol: str, tf: str, df: pd.DataFrame, when: datetime) -> bool:
    """Histórico curto antes da entrada: True se é o limite do terminal (falta definitiva). Se o terminal tem
    candles mais antigos que não vieram, o histórico ainda está sendo carregado: MT5Error para tentar de novo."""
    oldest = mt5.oldest_bar(symbol, tf)
    first = df["time"].iloc[0].to_pydatetime() if len(df) else when
    if oldest < first:
        raise MT5Error(f"Histórico {tf} de {symbol} ainda não carregado no terminal: contexto da entrada adiado.")
    return True


def compute(mt5: MT5Client, symbol: str, direction: str, when: datetime, price: float) -> dict[str, Any]:
    """Contexto da entrada (``direction``: 'compra' ou 'venda'; ``when`` em UTC; ``price``: preço executado).

    Timeframe sem histórico suficiente antes da entrada vai para ``faltando``. Terminal sem conexão vira MT5Error
    (candles que faltam pareceriam mercado parado): o journal tenta de novo na próxima sincronização.
    """
    if direction not in ("compra", "venda"):
        raise ValueError("direção deve ser 'compra' ou 'venda'.")
    if when.tzinfo is None:
        raise ValueError("Informe o horário da entrada com fuso horário (UTC).")
    when = when.astimezone(timezone.utc)
    spec = mt5.symbol_spec(symbol)
    resolved, digits, point = spec["simbolo"], spec["digitos"], spec["ponto"]
    missing: list[str] = []
    frames: dict[str, pd.DataFrame] = {}
    for tf in FRAMES:
        df = mt5.rates_until(resolved, tf, when, smc.BARS)
        if not df.attrs.get("conectado", True):
            raise MT5Error("Terminal sem conexão com a corretora: contexto da entrada adiado.")
        if len(df) < MIN_BARS:
            _complete(mt5, resolved, tf, df, when)
        frames[tf] = df
    m1 = frames["M1"]
    spread_source = m1 if len(m1) else frames["M5"]
    ref = float(price)
    entry: dict[str, Any] = {"horario": tempo.iso_utc(when), "preco": price, "direcao": direction}
    if direction == "compra" and len(spread_source):
        ref = round(float(price) - float(spread_source["spread"].iloc[-1]) * float(point), digits)
        entry["preco_bid_estimado"] = ref

    want = "alta" if direction == "compra" else "baixa"
    structure: dict[str, Any] = {}
    chochs: list[dict[str, Any]] = []
    sweeps: list[dict[str, Any]] = []
    zones: list[dict[str, Any]] = []
    premium: dict[str, Any] = {}
    for tf in FRAMES:
        df = frames[tf]
        if len(df) < MIN_BARS:
            missing.append(f"{tf}: só {len(df)} candles antes da entrada (histórico do terminal)")
            continue
        report = smc.analyze(df, digits, ref)
        micro, macro = report["estrutura"]["micro"], report["estrutura"]["macro"]
        structure[tf] = {
            "micro": micro["tendencia_por_fechamento"], "micro_pavio": micro["tendencia_por_pavio"],
            "macro": macro["tendencia_por_fechamento"], "macro_pavio": macro["tendencia_por_pavio"],
        }
        bar = tempo.bar_end(when, tf) - when  # duração do candle
        if tf in CHOCH_FRAMES:
            for scale, item in (("micro", micro), ("macro", macro)):
                for brk in item["ultimos_rompimentos"]:
                    for mode in ("por_fechamento", "por_pavio"):
                        event = brk[mode]
                        if not event or event["tipo"] != "CHoCH":
                            continue
                        closed = _parse(event["em"]) + bar
                        if when - closed <= CHOCH_WINDOW:
                            chochs.append({"tf": tf, "estrutura": scale, "direcao": brk["direcao"],
                                           "por": mode.removeprefix("por_"), "nivel": brk["nivel"],
                                           "fechou_em": tempo.iso_utc(closed),
                                           "minutos_antes": _minutes(when - closed)})
        if tf in SWEEP_FRAMES:
            for s in report["varreduras_recentes"]:
                closed = _parse(s["varrido_em"]) + bar
                if when - closed > SWEEP_FRAMES[tf] or s.get("rompido_depois_em"):
                    continue  # fora da janela, ou fechou além depois: rompimento
                above = s["de"] in ("topo", "topos_iguais")
                if (ref >= s["nivel"]) if above else (ref <= s["nivel"]):
                    continue  # o preço da entrada ainda está além do nível
                equal = s["de"] in ("topos_iguais", "fundos_iguais")
                kind = "liquidez_igual" if equal else "topo_fundo_m1_m3" if tf in _TRIGGER_FRAMES else "topo_fundo"
                sweeps.append({
                    "tipo": kind,
                    "nivel": f"{s['de'].replace('_', ' ')} {tf}", "preco": s["nivel"],
                    "lado": "acima" if above else "abaixo",
                    "tomado_em": s["varrido_em"], "minutos_antes": _minutes(when - closed),
                })
        for kind, key in (("OB", "order_blocks"), ("FVG", "fvg_abertos")):
            for zone in report[key]:
                if zone["de"] <= ref <= zone["ate"]:
                    item = {"tipo": kind, "tf": tf, "direcao": zone["direcao"], "de": zone["de"], "ate": zone["ate"]}
                    if kind == "OB":
                        item["estrutura"] = zone["estrutura"]
                    zones.append(item)
        if tf in PD_FRAMES and report.get("premium_discount"):
            pd_zone = report["premium_discount"]
            premium[tf] = {"zona": pd_zone["zona"], "posicao_pct": pd_zone["posicao_pct"]}

    if len(frames["M5"]):
        level_m5 = mt5.rates_until(resolved, "M5", when, smc.LEVEL_BARS)
        sweeps = _key_level_sweeps(level_m5, m1 if len(m1) else None, digits, when, ref) + sweeps
    for s in sweeps:
        s["a_favor"] = s["lado"] == ("abaixo" if want == "alta" else "acima")
    return {
        "versao": VERSION,
        "entrada": entry,
        "sessao": _session(when),
        "hora_nova_york": when.astimezone(tempo.NOVA_YORK).strftime("%H:%M"),
        "estrutura": structure,
        "choch": sorted(chochs, key=lambda c: c["fechou_em"]),
        "varreduras": sorted(sweeps, key=lambda s: s["tomado_em"]),
        "zonas": zones,
        "premium_discount": premium,
        "faltando": missing,
    }


def dimensions(context: dict[str, Any], direction: str) -> dict[str, str]:
    """Rótulos do contexto para agrupar as estatísticas (a favor/contra a direção da operação)."""
    want = "alta" if direction == "compra" else "baixa"
    structure = context.get("estrutura", {})

    def relative(trend: str | None) -> str:
        if trend is None:
            return "sem_dados"
        if trend == "indefinida":
            return "indefinida"
        return "a_favor" if trend == want else "contra"

    out = {"sessao": context.get("sessao", "sem_dados")}
    for tf in TREND_FRAMES:
        out[f"estrutura_{tf}"] = relative((structure.get(tf) or {}).get("micro"))
    out["estrutura_macro_H1"] = relative((structure.get("H1") or {}).get("macro"))

    def evaluated(frames: tuple[str, ...]) -> bool:
        return all(tf in structure for tf in frames)

    # A varredura de maior peso encontrada vale; "nenhuma" (ou uma de peso menor) só se as de peso maior puderam
    # ser procuradas.
    favor = {s["tipo"] for s in context.get("varreduras", []) if s.get("a_favor")}
    out["varredura_a_favor"] = "nenhuma"
    for kind in _SWEEP_KINDS:
        if kind in favor:
            out["varredura_a_favor"] = kind
            break
        if not evaluated(_SWEEP_NEEDS[kind]):
            out["varredura_a_favor"] = "sem_dados"
            break

    closes = [c for c in context.get("choch", []) if c["por"] == "fechamento"]
    if not evaluated(CHOCH_FRAMES):
        out["choch_a_favor"] = out["choch_contra"] = "sem_dados"
    else:
        favor_tfs = {c["tf"] for c in closes if c["direcao"] == want}
        out["choch_a_favor"] = next((tf for tf in CHOCH_FRAMES if tf in favor_tfs), "nenhum")
        out["choch_contra"] = "sim" if any(c["direcao"] != want for c in closes) else "nao"

    zones = context.get("zonas", [])
    kinds = sorted({z["tipo"] for z in zones if z["direcao"] == want}, reverse=True)  # OB antes de FVG
    complete = evaluated(FRAMES)
    out["zona_a_favor"] = " e ".join(kinds) if kinds else "nenhuma" if complete else "sem_dados"
    against = any(z["direcao"] != want for z in zones)
    out["zona_contra"] = "sim" if against else "nao" if complete else "sem_dados"

    for tf in PD_FRAMES:
        zone = (context.get("premium_discount", {}).get(tf) or {}).get("zona")
        if zone is None:
            label = "sem_dados" if tf not in structure else "sem_faixa"
        elif zone in ("premium", "discount"):
            label = "a_favor" if (zone == "discount") == (direction == "compra") else "contra"
        elif zone == "equilibrio":
            label = "equilibrio"
        else:
            label = "fora_da_faixa"
        out[f"premium_discount_{tf}"] = label
    return out
