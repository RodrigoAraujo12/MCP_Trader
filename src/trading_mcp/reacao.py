"""Reação observada a eventos e contexto entre ativos (somente leitura).

Separa o fato publicado (calendário do MT5) do movimento medido (candles M1 de bid e ticks do MT5).
Não atribui causa: outros eventos, a abertura de mercados e o fluxo normal também movem o preço.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from trading_mcp import tempo
from trading_mcp.calendario import CalendarError, EconomicCalendar
from trading_mcp.mt5_client import MT5Client, MT5Error

DEFAULT_WINDOWS = (1, 5, 15)
MAX_WINDOW_MIN = 240
MAX_SYMBOLS = 20
# Candles M1 antes do evento: referência e movimento típico (base de comparação entre instrumentos).
_LOOKBACK_MIN = 120
_TYPICAL_MAX_WINDOW = 30
_TYPICAL_MIN_SAMPLES = 20
# Último preço mais velho que isso = mercado parado (fechado ou sem liquidez) naquele instante.
_STALE_MIN = 5
_SPREAD_BEFORE = timedelta(minutes=5)
_SPREAD_AFTER = timedelta(minutes=2)
# Eventos a partir desta importância liberados dentro da janela de medição são listados.
_INSIDE_IMPORTANCE = "moderada"
CONTEXT_WINDOWS = (("15min", 15), ("1h", 60), ("4h", 240))
_FIAT = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK", "MXN", "ZAR", "SGD", "HKD", "CNH", "TRY"}
_RANK = {"alta": 3, "moderada": 2, "baixa": 1, "nenhuma": 0}
_M1 = pd.Timedelta(minutes=1)

OBSERVACOES_REACAO = [
    "Movimento medido em candles M1 de bid do MT5, em UTC. Referência = último preço antes do horário do "
    "evento; +N min = último preço antes de horário + N min. Resolução de 1 minuto.",
    "Medição, não causa: outros eventos, a abertura de mercados e o fluxo normal também movem o preço. Compare "
    "instrumentos pela variação em % e por `vezes_o_tipico` (o movimento dividido pela mediana dos movimentos do "
    "mesmo tamanho nas 2 h antes); % sozinho não considera a volatilidade de cada um.",
    "O calendário é só dos EUA: decisões e dados de outros países (BCE, BoE, BoJ, OPEP...) não aparecem nem como "
    "eventos no mesmo horário nem dentro da janela.",
    "Os símbolos são CFDs da corretora (DXYm não é o índice ICE; JP225m é cotado em ienes). Não há "
    "instrumento de Treasury na conta: rendimentos (yields) não estão disponíveis.",
]


def parse_utc(text: str) -> datetime:
    """'2026-10-01T12:30:00Z', '2026-10-01 12:30' ou com +00:00; sem fuso = UTC."""
    raw = (text or "").strip().replace(" ", "T")
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Horário inválido: {text!r}. Use UTC no formato 2026-10-01T12:30:00Z.") from exc
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _parse_event_time(text: str) -> datetime:
    if not re.search(r"\d{1,2}:\d{2}", text or ""):
        raise ValueError(f"Informe data e hora do evento em UTC (ex.: 2026-10-01T12:30:00Z), recebido: {text!r}.")
    moment = parse_utc(text)
    if moment.second or moment.microsecond:
        raise ValueError(
            "A medição usa candles de 1 minuto: informe o horário em minuto cheio (ex.: 12:30:00), como os "
            "horários do calendário."
        )
    return moment


def _move(value: float, ref: float, spec: dict) -> dict[str, Any]:
    digits = spec["digitos"]
    change = value - ref
    out: dict[str, Any] = {
        "variacao": round(change, digits),
        "variacao_pct": round(change / ref * 100, 3) if ref else None,
        "pontos": round(change / spec["ponto"]),
    }
    # Pips só em par de moedas de verdade: índices da Exness vêm com base e lucro = USD; ouro e cripto ficam fora.
    base, quote = spec["moeda_base"], spec["moeda_lucro"]
    if spec["is_forex"] and base != quote and base in _FIAT and quote in _FIAT:
        out["pips"] = round(change / (spec["ponto"] * (10 if digits in (3, 5) else 1)), 1)
    return out


def _bar_end(open_time: pd.Timestamp) -> datetime:
    return (open_time + _M1).to_pydatetime()


def _close_by(df: pd.DataFrame, moment: datetime) -> tuple[float, datetime] | None:
    """Último preço conhecido em ``moment``: fechamento do último candle M1 que terminou até ele."""
    done = df[df["time"] + _M1 <= pd.Timestamp(moment)]
    if done.empty:
        return None
    bar = done.iloc[-1]
    return float(bar["close"]), _bar_end(bar["time"])


def _typical(df: pd.DataFrame, moment: datetime, minutes: int) -> float | None:
    """Mediana de |variação em N min| nas 2 h antes do evento (base para comparar instrumentos)."""
    before = df[df["time"] + _M1 <= pd.Timestamp(moment)]
    closes = pd.Series(before["close"].to_numpy(), index=before["time"])
    shifted = closes.copy()
    shifted.index = shifted.index + pd.Timedelta(minutes=minutes)
    diffs = (closes - shifted).dropna().abs()
    if len(diffs) < _TYPICAL_MIN_SAMPLES:
        return None
    value = float(diffs.median())
    return value or None


def _spread(mt5: MT5Client, symbol: str, moment: datetime, now: datetime, point: float) -> dict[str, Any]:
    """Spread (em pontos): mediana nos 5 min antes e máximo nos 2 min depois, pelos ticks de cotação."""
    try:
        before = mt5.ticks_between(symbol, moment - _SPREAD_BEFORE, moment - timedelta(milliseconds=1))
        after = mt5.ticks_between(symbol, moment, min(moment + _SPREAD_AFTER, now))
    except MT5Error as exc:
        return {"indisponivel": f"Ticks não lidos: {exc}"}
    if before.empty and after.empty:
        return {"indisponivel": "Sem ticks nesse período."}

    def points(df: pd.DataFrame) -> pd.Series:
        return ((df["ask"] - df["bid"]) / point).round()

    return {
        "antes_mediana_pontos": int(points(before).median()) if not before.empty else None,
        "depois_max_pontos": int(points(after).max()) if not after.empty else None,
    }


def _measure(mt5: MT5Client, name: str, moment: datetime, windows: Sequence[int], now: datetime) -> dict[str, Any]:
    spec = mt5.symbol_spec(name)
    symbol, digits = spec["simbolo"], spec["digitos"]
    last = max(windows)
    end = min(moment + timedelta(minutes=last), now)
    df = mt5.rates_between(symbol, "M1", moment - timedelta(minutes=_LOOKBACK_MIN), end)
    out: dict[str, Any] = {"simbolo": symbol, "descricao": spec["descricao"]}
    warnings: list[str] = []
    if not df.attrs.get("conectado", True):
        warnings.append("Terminal sem conexão com a corretora: candles recentes podem faltar.")
    ref = _close_by(df, moment)
    if ref is None:
        out["referencia"] = None
        if df.empty:
            warnings.append(
                "Sem candles M1 nesse período: fora do histórico disponível (cerca de 3 meses), mercado fechado ou "
                "sem dados."
            )
        else:
            warnings.append(f"Sem negociação nas 2 h antes do evento ({symbol} fechado ou sem dados).")
        out["avisos"] = warnings
        return out
    ref_price, ref_time = ref
    out["referencia"] = {"preco": round(ref_price, digits), "ate": tempo.iso_utc(ref_time)}
    if moment - ref_time > timedelta(minutes=_STALE_MIN):
        out["referencia"]["antiga"] = True
        warnings.append(
            f"O último preço antes do evento é de {tempo.describe_age((moment - ref_time).total_seconds())} antes: "
            "mercado parado ou fechado naquele horário."
        )
    results = []
    for w in windows:
        target = moment + timedelta(minutes=w)
        item: dict[str, Any] = {"minutos": w}
        if target > now:
            item["situacao"] = "pendente"
            results.append(item)
            continue
        found = _close_by(df, target)
        if found is None or found[1] <= moment:
            item["situacao"] = "sem_negociacao"
            results.append(item)
            continue
        price, price_time = found
        item.update(preco=round(price, digits), **_move(price, ref_price, spec))
        if price_time < target:
            item["ate"] = tempo.iso_utc(price_time)  # faltou candle no fim da janela: último preço antes dela
        if w <= _TYPICAL_MAX_WINDOW and (typical := _typical(df, moment, w)) is not None:
            item["tipico_antes"] = round(typical, digits)
            item["vezes_o_tipico"] = round(abs(price - ref_price) / typical, 1)
        results.append(item)
    out["janelas"] = results
    after = df[(df["time"] >= pd.Timestamp(moment)) & (df["time"] < pd.Timestamp(end))]
    if not after.empty:
        high = after.loc[after["high"].idxmax()]
        low = after.loc[after["low"].idxmin()]
        extremes: dict[str, Any] = {
            "de": tempo.iso_utc(moment),
            "ate": tempo.iso_utc(end),
            "maxima": {**_move(float(high["high"]), ref_price, spec), "no_minuto": tempo.iso_utc(high["time"].to_pydatetime())},
            "minima": {**_move(float(low["low"]), ref_price, spec), "no_minuto": tempo.iso_utc(low["time"].to_pydatetime())},
        }
        if end < moment + timedelta(minutes=last):
            extremes["parcial"] = True  # a janela mais longa ainda não terminou
        out["extremos"] = extremes
        expected = int((end - moment).total_seconds() // 60)
        complete = after[after["time"] + _M1 <= pd.Timestamp(end)]
        if len(complete) < expected:
            out["candles_m1"] = {"recebidos": len(complete), "esperados": expected}
    elif any(r.get("situacao") != "pendente" for r in results):
        warnings.append("Nenhum candle depois do evento: sem negociação nesse período.")
    out["spread"] = _spread(mt5, symbol, moment, now, spec["ponto"])
    if warnings:
        out["avisos"] = warnings
    return out


def _label(row: dict) -> str:
    return f"{row.get('descricao') or row.get('evento')} ({row.get('codigo')})"


def _pick_event(calendar: EconomicCalendar, search: str, now: datetime) -> tuple[datetime, list[dict], dict]:
    """Evento buscado: no dia mais recente com divulgação, o de maior importância e, entre esses, o primeiro.

    (Em dia de FOMC, a decisão das 18:00 vem antes da entrevista das 18:30.)
    """
    data = calendar.query(hours_back=7 * 24, hours_ahead=0, min_importance="baixa", search=search, limit=100)
    released = [
        e for e in data["eventos"]
        if e.get("horario_tipo") == "exato" and "horario" in e and parse_utc(e["horario"]["utc"]) <= now
    ]
    if not released:
        raise ValueError(
            f"Nenhum evento '{search}' com horário exato nos últimos 7 dias do calendário. Informe horario_utc "
            "para medir um evento mais antigo."
        )
    latest_day = max(parse_utc(e["horario"]["utc"]).date() for e in released)
    day = [e for e in released if parse_utc(e["horario"]["utc"]).date() == latest_day]
    chosen = min(day, key=lambda e: (-_RANK.get(e.get("importancia"), 0), e["horario"]["utc"]))
    moment = parse_utc(chosen["horario"]["utc"])
    others = {
        (e["horario"]["utc"], e["codigo"]): {"utc": e["horario"]["utc"], "codigo": e["codigo"], "evento": _label(e)}
        for e in released
        if e["horario"]["utc"] != chosen["horario"]["utc"]
    }
    candidates = sorted(others.values(), key=lambda c: c["utc"], reverse=True)[:6]
    return moment, candidates, data


def _calendar_part(
    calendar: EconomicCalendar | None, moment: datetime | None, search: str, windows: Sequence[int], now: datetime
) -> tuple[datetime, dict[str, Any], list[str]]:
    """Horário do evento e a parte do calendário da resposta (fato publicado e eventos em volta)."""
    notes: list[str] = []
    part: dict[str, Any] = {"fato_publicado": [], "outros_eventos_no_mesmo_horario": []}
    if calendar is None:
        if moment is None:
            raise ValueError("Calendário não configurado: informe horario_utc.")
        return moment, part, ["Calendário não configurado: só o movimento foi medido."]
    try:
        candidates: list[dict] = []
        if moment is None:
            moment, candidates, info = _pick_event(calendar, search, now)
        window = {"min_importance": "baixa", "limit": 100, "start": moment - timedelta(seconds=1),
                  "end": moment + timedelta(seconds=1)}
        info = calendar.query(**window)
        key = tempo.iso_utc(moment)
        rows = [e for e in info["eventos"] if e.get("horario_tipo") == "exato" and e.get("horario", {}).get("utc") == key]
        matched_ids = (
            {e["id"] for e in calendar.query(search=search, **window)["eventos"]} if search.strip() else None
        )
        inside, covered = calendar.events_between(
            moment + timedelta(seconds=1), moment + timedelta(minutes=max(windows)), _INSIDE_IMPORTANCE
        )
    except CalendarError as exc:
        if moment is None:
            raise
        return moment, part, [f"Calendário indisponível ({exc}): só o movimento foi medido."]

    facts = [r for r in rows if matched_ids is None or r["id"] in matched_ids]
    others = [r for r in rows if matched_ids is not None and r["id"] not in matched_ids]
    # A leitura direcional da fonte (MetaQuotes) é interpretação: sai do bloco de fatos.
    reading = []
    for f in facts:
        impact = f.pop("impacto_no_usd_segundo_a_fonte", None)
        if impact:
            reading.append({"codigo": f["codigo"], "impacto_no_usd": impact})
    part["fato_publicado"] = facts
    part["outros_eventos_no_mesmo_horario"] = [_label(r) for r in others]
    if reading:
        part["leitura_da_fonte"] = reading
    if candidates:
        part["outros_candidatos"] = candidates
    if inside:
        listed = []
        for e in inside:
            when = parse_utc(e["utc"])
            listed.append({**e, "minutos_apos": round((when - moment).total_seconds() / 60, 1)})
        part["eventos_dentro_da_janela"] = listed
        part["janelas_afetadas"] = {
            str(w): [e["codigo"] for e in listed if e["minutos_apos"] < w] for w in windows
            if any(e["minutos_apos"] < w for e in listed)
        }
        notes.append(
            "Há eventos dos EUA divulgados dentro da janela de medição (`eventos_dentro_da_janela`): as janelas em "
            "`janelas_afetadas` misturam a reação a mais de um evento."
        )
    if not covered:
        notes.append("O arquivo do calendário não cobre toda a janela: eventos dentro dela podem faltar.")
    if search.strip() and not facts:
        listed_now = ", ".join(_label(r) for r in rows) or "nenhum"
        notes.append(f"Nenhum evento '{search}' nesse horário. Eventos dos EUA nesse horário: {listed_now}.")
    elif not rows:
        notes.append(
            "Nenhum evento do calendário dos EUA nesse horário (o arquivo cobre só os últimos 7 dias): confira o "
            "horário."
        )
    if info.get("estado") != "atual":
        notes.append(
            f"Calendário {info.get('estado')} (arquivo de {info['arquivo_atualizado']['utc']}): realizados recentes "
            "podem faltar e o evento 'mais recente' pode não ser o último divulgado."
        )
    notes += [n for n in info.get("observacoes", []) if "sem conexão" in n or "nenhum valor" in n]
    return moment, part, notes


def reaction(
    mt5: MT5Client,
    calendar: EconomicCalendar | None,
    *,
    when: str = "",
    search: str = "",
    symbols: Sequence[str],
    windows: Sequence[int] = DEFAULT_WINDOWS,
) -> dict[str, Any]:
    """Movimento dos instrumentos depois de um evento, ao lado do fato publicado."""
    windows = sorted({int(w) for w in windows})
    if not windows or windows[0] < 1 or windows[-1] > MAX_WINDOW_MIN:
        raise ValueError(f"Janelas devem estar entre 1 e {MAX_WINDOW_MIN} minutos.")
    if not when.strip() and not search.strip():
        raise ValueError("Informe o evento (busca, ex.: CPI, NFP, claims) ou o horário UTC.")
    if len(symbols) > MAX_SYMBOLS:
        raise ValueError(f"No máximo {MAX_SYMBOLS} instrumentos por consulta.")
    now = mt5.now_utc()
    moment = _parse_event_time(when) if when.strip() else None
    if moment is not None and moment > now:
        raise ValueError(f"O horário {tempo.iso_utc(moment)} ainda não chegou.")
    moment, calendar_part, notes = _calendar_part(calendar, moment, search, windows, now)
    measures = []
    for name in symbols:
        try:
            measures.append(_measure(mt5, name, moment, windows, now))
        except (MT5Error, ValueError) as exc:
            measures.append({"simbolo": name, "erro": str(exc)})
    return {
        "horario_evento": tempo.exibicao(moment),
        **calendar_part,
        "janelas_min": windows,
        "movimento_medido": measures,
        "coletado": tempo.exibicao(now),
        "observacoes": OBSERVACOES_REACAO + notes,
    }


# ---------------------------------------------------------------------- contexto entre ativos
def _day_baseline(mt5: MT5Client, symbol: str, day_start: datetime) -> tuple[float, datetime] | None:
    """Último preço antes de 00:00 UTC (fechamento do último candle H1 que terminou até a virada)."""
    hours = mt5.rates_between(symbol, "H1", day_start - timedelta(days=4), day_start)
    done = hours[hours["time"] + pd.Timedelta(hours=1) <= pd.Timestamp(day_start)]
    if done.empty:
        return None
    bar = done.iloc[-1]
    return float(bar["close"]), (bar["time"] + pd.Timedelta(hours=1)).to_pydatetime()


def context(mt5: MT5Client, symbols: Sequence[str]) -> dict[str, Any]:
    """Variação recente de cada instrumento (15 min, 1 h, 4 h e desde 00:00 UTC), com o estado da cotação."""
    if len(symbols) > MAX_SYMBOLS:
        raise ValueError(f"No máximo {MAX_SYMBOLS} instrumentos por consulta.")
    now = mt5.now_utc()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    longest = max(m for _, m in CONTEXT_WINDOWS)
    items = []
    for name in symbols:
        try:
            spec = mt5.symbol_spec(name)
            symbol, digits = spec["simbolo"], spec["digitos"]
            quote = mt5.quote(symbol)
            start = min(now - timedelta(minutes=longest + _LOOKBACK_MIN), day_start)
            df = mt5.rates_between(symbol, "M1", start, now)
            baseline = _day_baseline(mt5, symbol, day_start)
        except (MT5Error, ValueError) as exc:
            items.append({"simbolo": name, "erro": str(exc)})
            continue
        current = quote["bid"]  # candles são de bid: compara bid com bid
        item: dict[str, Any] = {
            "simbolo": symbol,
            "preco": current,
            "cotacao": {"estado": quote["estado"], "idade_s": quote["idade_s"]},
        }
        changes: dict[str, Any] = {}
        for label, minutes in CONTEXT_WINDOWS:
            window_start = now - timedelta(minutes=minutes)
            past = _close_by(df, window_start)
            if past is None:
                changes[label] = None
                continue
            entry = _move(current, past[0], spec)
            if window_start - past[1] > timedelta(minutes=_STALE_MIN):
                entry["desde"] = tempo.iso_utc(past[1])  # mercado parado no início da janela
            changes[label] = entry
        if baseline is not None:
            entry = _move(current, baseline[0], spec)
            if day_start - baseline[1] > timedelta(minutes=_STALE_MIN):
                entry["desde"] = tempo.iso_utc(baseline[1])  # o mercado fechou antes da virada do dia
            changes["dia_utc"] = entry
        else:
            changes["dia_utc"] = None
        today = df[df["time"] >= pd.Timestamp(day_start)]
        if not today.empty:
            high, low = float(today["high"].max()), float(today["low"].min())
            day_range: dict[str, Any] = {
                "maxima": round(high, digits),
                "minima": round(low, digits),
                "posicao_pct": round((current - low) / (high - low) * 100, 1) if high > low else None,
            }
            first = today["time"].iloc[0].to_pydatetime()
            if first - day_start > timedelta(minutes=_STALE_MIN):
                day_range["desde"] = tempo.iso_utc(first)  # primeiro negócio do dia depois de 00:00
            item["faixa_do_dia"] = day_range
        item["variacao"] = changes
        if quote["estado"] != "atual":
            item["aviso"] = quote.get("aviso") or f"Cotação {quote['estado']}: variações não são de agora."
        items.append(item)
    stale = [i["simbolo"] for i in items if i.get("cotacao", {}).get("estado") not in (None, "atual")]
    notes = [
        "Variação = bid atual contra o último preço antes do início de cada janela (candles M1 de bid). "
        "'dia_utc' parte do último preço antes de 00:00 UTC (21:00 de Brasília), não da abertura de Nova York; "
        "`desde` marca quando esse preço é mais antigo (mercado parado).",
        "posicao_pct: onde o preço está na faixa do dia (0 = mínima, 100 = máxima).",
        "Compare instrumentos pela variação em %. Correlação aparente não indica causa.",
    ]
    if stale:
        notes.append(f"Cotação não atual em {', '.join(stale)}: não misture com os demais como se fosse agora.")
    return {"coletado": tempo.exibicao(now), "instrumentos": items, "observacoes": notes}
