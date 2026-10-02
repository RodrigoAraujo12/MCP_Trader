"""Calendário econômico a partir do arquivo exportado pelo serviço MQL5 ``TradingMcpCalendar``.

A biblioteca Python do MetaTrader 5 não acessa o calendário. O serviço
(``mql5/Services/TradingMcpCalendar.mq5``) roda dentro do terminal e grava
``MQL5\\Files\\trading_mcp\\calendar_US.json``; este módulo só lê esse arquivo.

Regras dos dados (conferidas com o arquivo real em 2026-10-01):

* Valores do MT5 vêm multiplicados por 10^6; ausência de valor vira ``null``, nunca zero.
  Valor / 10^6 é o número exibido, na unidade do multiplicador (NFP 162 + THOUSANDS = 162 mil).
* ``forecast`` é a previsão do calendário do MT5; nem sempre é o consenso de mercado
  (há previsões com 3 casas decimais, típicas de modelo, não de pesquisa com analistas).
* ``revision``: 0 = divulgação única; 1 = primeira estimativa de um dado que será revisado
  (``prev`` é o período anterior); 2 ou mais = estimativa revisada (``prev`` é a estimativa
  anterior do MESMO período). ``revised_prev`` só existe quando a fonte revisou o anterior.
* Horários estão no horário do servidor; o arquivo traz a diferença para o GMT
  (``server_gmt_offset_s``), que na Exness é 0.
* ``actual_seen_gmt`` é quando o serviço viu o realizado aparecer num valor que antes estava
  sem realizado; só então mede a latência da fonte.
* Os nomes vêm traduzidos pelo terminal e podem estar errados; o ``event_code`` é confiável.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trading_mcp import tempo

SCHEMA = 1
SOURCE = "Calendário econômico do MetaTrader 5 (MetaQuotes)"
_SCALE = 1_000_000

IMPORTANCE_LEVELS = {"baixa": 1, "moderada": 2, "alta": 3}
_IMPORTANCE = {"NONE": 0, "LOW": 1, "MODERATE": 2, "HIGH": 3}
_IMPORTANCE_PT = {0: "nenhuma", 1: "baixa", 2: "moderada", 3: "alta"}
_TIME_MODE = {"DATETIME": "exato", "DATE": "dia_inteiro", "NOTIME": "sem_horario", "TENTATIVE": "provisorio"}
_WHOLE_DAY = ("dia_inteiro", "sem_horario")
_MULTIPLIER = {"NONE": "", "THOUSANDS": "mil", "MILLIONS": "milhões", "BILLIONS": "bilhões", "TRILLIONS": "trilhões"}
_UNIT = {
    "NONE": "",
    "PERCENT": "%",
    "CURRENCY": "moeda local",
    "USD": "USD",
    "JOB": "empregos",
    "PEOPLE": "pessoas",
    "HOUR": "horas",
    "RIG": "sondas",
    "MORTGAGE": "hipotecas",
    "VOTE": "votos",
    "BARREL": "barris",
    "CUBICFEET": "pés cúbicos",
    "POSITION": "contratos",
    "BUILDING": "imóveis",
}
_COUNTRY_CURRENCY = {"US": "USD"}
_FREQUENCY = {"NONE": None, "WEEK": "semanal", "MONTH": "mensal", "QUARTER": "trimestral", "YEAR": "anual", "DAY": "diária"}
_IMPACT = {"NA": None, "POSITIVE": "positivo", "NEGATIVE": "negativo"}
# Diferença servidor-GMT é medida com duas leituras de relógio: arredonda para 15 min.
_OFFSET_STEP_S = 900

# O terminal traduz os nomes dos eventos e às vezes erra: em 2026-10-01 o CPI cheio mensal
# (consumer-price-index-mm) aparecia como "Núcleo do Índice de Preços ao Consumidor (IPC) (Mensal)",
# o mesmo nome do núcleo. O código (em inglês) é o identificador confiável.
KEY_EVENTS = {
    "nonfarm-payrolls": "Payroll (NFP): variação de empregos não agrícolas",
    "unemployment-rate": "Taxa de desemprego",
    "average-hourly-earnings-mm": "Ganho médio por hora, variação mensal",
    "initial-jobless-claims": "Pedidos iniciais de seguro-desemprego",
    "continuing-jobless-claims": "Pedidos contínuos de seguro-desemprego",
    "consumer-price-index": "CPI, nível do índice",
    "consumer-price-index-mm": "CPI cheio, variação mensal",
    "consumer-price-index-yy": "CPI cheio, variação anual",
    "consumer-price-index-ex-food-energy-mm": "CPI núcleo (sem alimentos e energia), variação mensal",
    "consumer-price-index-ex-food-energy-yy": "CPI núcleo (sem alimentos e energia), variação anual",
    "consumer-price-index-ex-food-energy-nsa-mm": "CPI núcleo, sem ajuste sazonal, variação mensal",
    "producer-price-index-mm": "PPI cheio, variação mensal",
    "producer-price-index-yy": "PPI cheio, variação anual",
    "retail-sales-mm": "Vendas no varejo, variação mensal",
    "retail-sales-ex-autos-mm": "Vendas no varejo exceto automóveis, variação mensal",
    "ism-manufacturing-pmi": "ISM industrial (PMI)",
    "ism-non-manufacturing-pmi": "ISM serviços (PMI)",
    "adp-nonfarm-employment-change": "ADP: variação de empregos privados",
    "core-pce-price-index-mm": "PCE núcleo, variação mensal",
    "core-pce-price-index-yy": "PCE núcleo, variação anual",
    "fomc-minutes": "Ata do FOMC",
}
# Termos de busca -> expressão sobre o código do evento. Um termo com apelido só casa pelo
# apelido (evita "fed" casar com "federal" ou com os Fed regionais).
SEARCH_ALIASES = {
    "cpi": r"consumer-price-index",
    "ipc": r"consumer-price-index",
    "ppi": r"producer-price-index",
    "pce": r"pce-price-index",
    "inflacao": r"consumer-price-index|producer-price-index|pce-price-index",
    "core": r"ex-food-energy|^core-",
    "nucleo": r"ex-food-energy|^core-",
    "nfp": r"^nonfarm-payrolls$",
    "payroll": r"payrolls|adp-nonfarm",
    "fomc": r"fomc|^fed-interest-rate|^fed-funds",
    "fed": r"^fed-|fomc",
    "juros": r"interest-rate",
    "claims": r"jobless-claims",
    "desemprego": r"unemployment|jobless-claims",
    "pib": r"gross-domestic-product|^gdp-",
    "gdp": r"gross-domestic-product|^gdp-",
    "varejo": r"retail-sales",
    "retail": r"retail-sales",
    "ism": r"^ism-",
    "pmi": r"pmi",
}
_PRICE_FAMILIES = ("consumer-price-index", "producer-price-index", "pce-price-index")

NOTES = [
    "Fonte: calendário econômico do MetaTrader 5. 'previsao' é a previsão desse calendário e pode não ser "
    "o consenso de mercado (outras fontes divergem); ausente = null, nunca zero. Não chame de consenso "
    "sem conferir em outra fonte.",
    "Os nomes ('evento') vêm traduzidos pelo terminal e podem estar errados (ex.: o CPI cheio aparece como "
    "'Núcleo'). Identifique a medida por 'codigo', 'descricao' e 'medida'.",
    "Confira o realizado na fonte oficial (fonte_oficial) antes de qualquer decisão.",
    "'surpresa' é realizado − previsão na unidade do indicador; não diz se o dado é bom ou ruim para o ativo.",
]


class CalendarError(Exception):
    """Calendário indisponível (mensagem em português, pronta para o modelo)."""


def _suffix(enum_name: Any) -> str:
    """'CALENDAR_IMPORTANCE_HIGH' -> 'HIGH'."""
    return str(enum_name or "").rsplit("_", 1)[-1].upper()


def _num(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    return _clean(float(raw) / _SCALE)


def _clean(value: float) -> float:
    """Arredonda o ruído de ponto flutuante sem perder casas reais e evita -0.0."""
    rounded = round(value, 6)
    return 0.0 if rounded == 0 else rounded


def _normalize(text: Any) -> str:
    """Minúsculas, sem acentos e com hífens/sublinhados como espaço (para busca)."""
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[-_/]+", " ", plain.lower())


def measure(code: str) -> dict[str, Any] | None:
    """Medida derivada do código do evento: variação mensal/anual/trimestral, núcleo, ajuste sazonal."""
    code = code.lower()
    parts = code.split("-")
    out: dict[str, Any] = {}
    if parts[-1] in ("mm", "yy", "qq"):
        out["variacao"] = {"mm": "mensal", "yy": "anual", "qq": "trimestral"}[parts[-1]]
    if "nsa" in parts:
        out["ajuste_sazonal"] = False
    if any(family in code for family in _PRICE_FAMILIES):
        out["nucleo"] = code.startswith("core-") or "ex-food-energy" in code
    return out or None


def _matches(tokens: list[str], ev: dict[str, Any]) -> bool:
    """Todos os termos precisam casar (pelo apelido no código ou pelo texto do evento)."""
    code = str(ev.get("event_code") or "").lower()
    text = f"{_normalize(ev.get('name'))} {_normalize(code)} {_normalize(KEY_EVENTS.get(code, ''))}"
    for token in tokens:
        alias = SEARCH_ALIASES.get(token)
        if alias is not None:
            if not re.search(alias, code):
                return False
        elif token not in text:
            return False
    return True


def matches_search(search: str, code: str, name: str | None = None) -> bool:
    """A busca da tool ``calendario`` aplicada a um evento já conhecido (código e nome)."""
    return _matches(_normalize(search).split(), {"event_code": code, "name": name})


def _span(data: dict[str, Any]) -> tuple[datetime, datetime]:
    """Intervalo que o arquivo cobre: da exportação - days_back até a exportação + days_ahead."""
    generated = tempo.from_epoch(int(data["generated_gmt"]))
    return (
        generated - timedelta(days=int(data.get("days_back") or 0)),
        generated + timedelta(days=int(data.get("days_ahead") or 0)),
    )


def _period_label(period_epoch: int, frequency: str | None) -> str:
    """Período de referência pela frequência (o MT5 guarda o primeiro dia do período)."""
    first_day = datetime.fromtimestamp(period_epoch, tz=timezone.utc)
    if frequency == "mensal":
        return first_day.strftime("%Y-%m")
    if frequency == "trimestral":
        return f"{first_day.year}-T{(first_day.month - 1) // 3 + 1}"
    if frequency == "anual":
        return str(first_day.year)
    if frequency == "semanal":
        return f"semana de {first_day:%Y-%m-%d}"
    return first_day.strftime("%Y-%m-%d")


class EconomicCalendar:
    """Lê o arquivo do serviço e responde consultas por janela de tempo."""

    def __init__(self, locate: Callable[[], Path], now_utc: Callable[[], datetime] | None = None) -> None:
        self._locate = locate
        self._now_utc = now_utc or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ leitura
    def _load(self) -> dict[str, Any]:
        path = self._locate()
        if not path.is_file():
            raise CalendarError(
                f"Arquivo do calendário não encontrado ({path}). O serviço TradingMcpCalendar precisa estar "
                "rodando no MetaTrader 5: veja a seção 'Calendário econômico' do README."
            )
        try:
            # utf-8-sig: a documentação do MQL5 não garante se o UTF-8 sai com ou sem BOM.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise CalendarError(f"Não foi possível ler o arquivo do calendário ({path}): {exc}") from exc
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            found = data.get("schema") if isinstance(data, dict) else "?"
            raise CalendarError(
                f"Formato do arquivo do calendário não reconhecido (schema {found}, esperado {SCHEMA}): "
                "atualize o serviço TradingMcpCalendar."
            )
        if not data.get("ok", False):
            raise CalendarError(f"O serviço do calendário informou erro no MetaTrader 5: {data.get('error')}")
        return data

    # ------------------------------------------------------------------ consulta
    def query(
        self,
        *,
        hours_back: float = 2.0,
        hours_ahead: float = 24.0,
        min_importance: str = "moderada",
        search: str = "",
        limit: int = 40,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        """Eventos entre agora - hours_back e agora + hours_ahead, ou entre ``start`` e ``end`` se informados."""
        level = IMPORTANCE_LEVELS.get((min_importance or "").strip().lower())
        if level is None:
            raise ValueError(f"importancia_minima deve ser baixa, moderada ou alta (recebido: {min_importance!r}).")
        data = self._load()
        now = self._now_utc()
        raw_offset = int(data.get("server_gmt_offset_s") or 0)
        offset = round(raw_offset / _OFFSET_STEP_S) * _OFFSET_STEP_S
        generated = tempo.from_epoch(int(data["generated_gmt"]))
        age = (now - generated).total_seconds()
        poll = int(data.get("poll_seconds") or 15)
        refresh = int(data.get("refresh_seconds") or 300)
        stale = age > refresh + 3 * poll + 30
        started = data.get("service_started_gmt")
        currency = _COUNTRY_CURRENCY.get(str(data.get("country") or ""))

        events = {ev["id"]: ev for ev in data.get("events", []) if isinstance(ev, dict) and "id" in ev}
        values = [v for v in data.get("values", []) if isinstance(v, dict)]
        start = start if start is not None else now - timedelta(hours=hours_back)
        end = end if end is not None else now + timedelta(hours=hours_ahead)
        tokens = _normalize(search).split()

        rows: list[tuple[datetime, int, dict[str, Any]]] = []
        # Eventos com horário exato, por instante: (id do valor, rótulo com o código). Nomes traduzidos
        # podem repetir para medidas diferentes, então a comparação é pelo id.
        by_time: dict[int, list[tuple[Any, str]]] = {}
        orphans = 0
        for value in values:
            ev = events.get(value.get("event_id"))
            if ev is None:
                orphans += 1
                continue
            when = tempo.from_epoch(int(value["time"]) - offset)
            time_mode = _TIME_MODE.get(_suffix(ev.get("time_mode")), "desconhecido")
            importance = _IMPORTANCE.get(_suffix(ev.get("importance")), 0)
            holiday = _suffix(ev.get("type")) == "HOLIDAY"
            code = str(ev.get("event_code") or "")
            if time_mode == "exato":
                label = f"{KEY_EVENTS.get(code, ev.get('name'))} ({code})"
                by_time.setdefault(int(when.timestamp()), []).append((value.get("id"), label))
            # Evento de dia inteiro conta se o dia cruza a janela.
            last = when + timedelta(days=1) if time_mode in _WHOLE_DAY else when
            if last < start or when > end:
                continue
            if not holiday and importance < level:  # feriados sempre aparecem: afetam as sessões
                continue
            if tokens and not _matches(tokens, ev):
                continue
            row = self._event_row(ev, value, when, time_mode, importance, now, offset, started, poll, currency)
            rows.append((when, 3 if holiday else importance, row))

        # Com limite, ficam os mais importantes e os mais próximos de agora; a saída volta à ordem do horário.
        chosen = sorted(rows, key=lambda r: (-r[1], abs((r[0] - now).total_seconds())))[: max(1, limit)]
        chosen.sort(key=lambda r: (r[0], -r[1], str(r[2]["evento"])))
        out_events = []
        same_time: dict[str, list[str]] = {}
        for when, _, row in chosen:
            group = by_time.get(int(when.timestamp()), []) if row["horario_tipo"] == "exato" else []
            if len(group) > 1:
                # Lista completa uma vez por horário (inclui eventos fora do filtro); no evento, só a contagem.
                row["outros_no_mesmo_horario"] = len(group) - 1
                same_time[row["horario"]["utc"]] = sorted({label for _, label in group})
            out_events.append(row)

        notes = list(NOTES)
        if offset:
            notes.append(
                f"O servidor do calendário não está em UTC (diferença de {offset} s): horários convertidos com a "
                "diferença atual; eventos de antes de uma mudança de horário de verão podem estar 1 h errados."
            )
        if len(rows) > len(out_events):
            notes.append(
                f"Mostrando {len(out_events)} de {len(rows)} eventos (os mais importantes e mais próximos de "
                "agora); reduza a janela ou use 'busca' para ver os demais."
            )
        estado = "desatualizado" if stale else "atual"
        if stale:
            notes.append(
                f"O arquivo do calendário não é atualizado há {tempo.describe_age(age)}: o serviço "
                "TradingMcpCalendar ou o terminal parecem parados; realizados recentes podem faltar."
            )
        if not data.get("terminal_connected", True):
            notes.append("O terminal estava sem conexão com a corretora na última exportação.")
        if not values:
            notes.append(
                "O arquivo não tem nenhum valor: o calendário do terminal pode ainda não ter sincronizado."
            )
        missing = orphans or int(data.get("missing_events") or 0)
        if missing:
            notes.append(f"{missing} valor(es) sem descrição do evento no arquivo foram ignorados.")
        return {
            "fonte": f"{SOURCE}, via {data.get('server') or 'terminal MT5'}",
            "pais": data.get("country"),
            "estado": estado,
            "arquivo_atualizado": tempo.exibicao(generated),
            "idade_arquivo_s": round(max(age, 0.0), 1),
            "agora": tempo.exibicao(now),
            "janela": {"de": tempo.exibicao(start), "ate": tempo.exibicao(end)},
            "filtro": {"importancia_minima": min_importance, "busca": search or None},
            "total": len(rows),
            "eventos": out_events,
            "mesmo_horario": same_time,
            "observacoes": notes,
        }

    def events_between(
        self, start: datetime, end: datetime, min_importance: str = "alta"
    ) -> tuple[list[dict[str, Any]], bool]:
        """Eventos com horário exato entre ``start`` e ``end`` e se o arquivo cobre esse intervalo.

        Feriados e eventos de dia inteiro ficam de fora: não marcam um instante de divulgação.
        """
        level = IMPORTANCE_LEVELS.get((min_importance or "").strip().lower())
        if level is None:
            raise ValueError(f"importancia_minima deve ser baixa, moderada ou alta (recebido: {min_importance!r}).")
        data = self._load()
        offset = round(int(data.get("server_gmt_offset_s") or 0) / _OFFSET_STEP_S) * _OFFSET_STEP_S
        first, last = _span(data)
        covered = first <= start and end <= last
        events = {ev["id"]: ev for ev in data.get("events", []) if isinstance(ev, dict) and "id" in ev}
        out: list[dict[str, Any]] = []
        for value in data.get("values", []):
            ev = events.get(value.get("event_id")) if isinstance(value, dict) else None
            if ev is None or _TIME_MODE.get(_suffix(ev.get("time_mode"))) != "exato":
                continue
            importance = _IMPORTANCE.get(_suffix(ev.get("importance")), 0)
            if _suffix(ev.get("type")) == "HOLIDAY" or importance < level:
                continue
            when = tempo.from_epoch(int(value["time"]) - offset)
            if start <= when <= end:
                code = str(ev.get("event_code") or "")
                out.append(
                    {
                        "utc": tempo.iso_utc(when),
                        "codigo": code,
                        "evento": KEY_EVENTS.get(code, ev.get("name")),
                        "importancia": _IMPORTANCE_PT[importance],
                    }
                )
        return sorted(out, key=lambda e: (e["utc"], e["codigo"])), covered

    def coverage(self) -> tuple[datetime, datetime]:
        """De quando até quando o arquivo traz eventos (dias para trás e para frente do serviço)."""
        return _span(self._load())

    # ------------------------------------------------------------------ evento
    @staticmethod
    def _event_row(
        ev: dict[str, Any],
        value: dict[str, Any],
        when: datetime,
        time_mode: str,
        importance: int,
        now: datetime,
        offset: int,
        started: Any,
        poll: int,
        currency: str | None,
    ) -> dict[str, Any]:
        unit = _UNIT.get(_suffix(ev.get("unit")), "")
        if unit == "moeda local" and currency:
            unit = currency
        mult = _MULTIPLIER.get(_suffix(ev.get("multiplier")), "")
        unit_label = " ".join(p for p in (mult, unit) if p) or None
        actual, forecast = _num(value.get("actual")), _num(value.get("forecast"))
        prev, revised = _num(value.get("prev")), _num(value.get("revised_prev"))
        kind = _suffix(ev.get("type"))
        code = str(ev.get("event_code") or "")
        whole_day = time_mode in _WHOLE_DAY

        row: dict[str, Any] = {"id": f"mt5:{ev.get('id')}:{value.get('id')}", "evento": ev.get("name"), "codigo": code}
        if code in KEY_EVENTS:
            row["descricao"] = KEY_EVENTS[code]
        if (m := measure(code)) is not None:
            row["medida"] = m
        row["importancia"] = _IMPORTANCE_PT[importance]
        if whole_day:
            # Só a data: converter 00:00 do servidor para Nova York mostraria o dia anterior.
            row["data"] = datetime.fromtimestamp(int(value["time"]), tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            row["horario"] = tempo.exibicao(when)
        row["horario_tipo"] = time_mode
        frequency = _FREQUENCY.get(_suffix(ev.get("frequency")))
        period = value.get("period")
        if period:
            # O período é uma data do servidor (primeiro dia do período): não aplica a diferença de fuso.
            row["periodo_referencia"] = _period_label(int(period), frequency)
        if frequency:
            row["frequencia"] = frequency

        day_end = when + timedelta(days=1)
        if kind == "HOLIDAY":
            row["situacao"] = "feriado"
        elif actual is not None:
            row["situacao"] = "divulgado"
        elif when > now:
            row["situacao"] = "agendado"
            if not whole_day:
                row["faltam_min"] = round((when - now).total_seconds() / 60, 1)
        elif kind == "EVENT":
            row["situacao"] = "evento_sem_valores"
        elif whole_day and now < day_end:
            row["situacao"] = "aguardando_realizado"  # horário exato desconhecido: sem contar atraso
        else:
            row["situacao"] = "aguardando_realizado"
            row["atraso_min"] = round((now - when).total_seconds() / 60, 1)

        if kind == "INDICATOR":
            revision = int(value.get("revision") or 0)
            row.update(
                {
                    "unidade": unit_label,
                    "realizado": actual,
                    "previsao": forecast,
                    "anterior": prev,
                    "anterior_revisado": revised,
                    "anterior_foi_revisado": revised is not None and revised != prev,
                    "surpresa": EconomicCalendar._surprise(actual, forecast, unit, unit_label),
                }
            )
            if revision == 1:
                row["estimativa"] = "primeira estimativa (será revisada); 'anterior' é do período anterior"
            elif revision >= 2:
                row["estimativa"] = (
                    "estimativa revisada; 'anterior' é a estimativa anterior do MESMO período de referência, "
                    "não o período anterior"
                )
            impact = _IMPACT.get(_suffix(value.get("impact")))
            if impact:
                row["impacto_no_usd_segundo_a_fonte"] = impact
            seen = value.get("actual_seen_gmt")
            # Latência só quando o serviço já rodava antes do evento e viu o realizado aparecer.
            if (
                actual is not None
                and seen
                and started
                and int(seen) > int(started) + poll
                and when.timestamp() >= int(started)
            ):
                seen_at = tempo.from_epoch(int(seen))
                row["realizado_visto"] = tempo.exibicao(seen_at)
                row["latencia_fonte_s"] = round((seen_at - when).total_seconds(), 1)
        if ev.get("source_url"):
            row["fonte_oficial"] = ev["source_url"]
        return row

    @staticmethod
    def _surprise(
        actual: float | None, forecast: float | None, unit: str, unit_label: str | None
    ) -> dict[str, Any] | None:
        if actual is None:
            return None
        if forecast is None:
            return {"valor": None, "motivo": "a fonte não informou previsão"}
        diff = _clean(actual - forecast)
        sentido = "acima da previsão" if diff > 0 else "abaixo da previsão" if diff < 0 else "igual à previsão"
        return {"valor": diff, "unidade": "p.p." if unit == "%" else unit_label, "sentido": sentido}
