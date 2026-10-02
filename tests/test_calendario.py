from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from trading_mcp.calendario import CalendarError, EconomicCalendar, matches_search, measure

UTC = timezone.utc
NOW = datetime(2026, 10, 1, 12, 45, tzinfo=UTC)
RELEASE = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)  # 8:30 em Nova York (EDT)
_S = 1_000_000


def ts(moment: datetime) -> int:
    return int(moment.timestamp())


def ev(
    event_id: int,
    name: str,
    code: str,
    *,
    importance: str = "HIGH",
    unit: str = "PERCENT",
    mult: str = "NONE",
    digits: int = 1,
    kind: str = "INDICATOR",
    time_mode: str = "DATETIME",
    frequency: str = "MONTH",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "name": name,
        "event_code": code,
        "type": f"CALENDAR_TYPE_{kind}",
        "sector": "CALENDAR_SECTOR_PRICES",
        "frequency": f"CALENDAR_FREQUENCY_{frequency}",
        "time_mode": f"CALENDAR_TIMEMODE_{time_mode}",
        "unit": f"CALENDAR_UNIT_{unit}",
        "multiplier": f"CALENDAR_MULTIPLIER_{mult}",
        "importance": f"CALENDAR_IMPORTANCE_{importance}",
        "digits": digits,
        "source_url": "https://www.bls.gov/cpi/",
    }


def val(
    value_id: int,
    event_id: int,
    when: datetime,
    *,
    actual: float | None = None,
    forecast: float | None = None,
    prev: float | None = None,
    revised: float | None = None,
    seen: datetime | None = None,
    impact: str = "NA",
    revision: int = 0,
    period: datetime | None = datetime(2026, 9, 1, tzinfo=UTC),
) -> dict[str, Any]:
    def conv(x: float | None) -> int | None:
        return None if x is None else int(round(x * _S))

    return {
        "id": value_id,
        "event_id": event_id,
        "time": ts(when),
        "period": ts(period) if period else 0,
        "revision": revision,
        "actual": conv(actual),
        "forecast": conv(forecast),
        "prev": conv(prev),
        "revised_prev": conv(revised),
        "impact": f"CALENDAR_IMPACT_{impact}",
        "actual_seen_gmt": ts(seen) if seen else None,
    }


def write(tmp_path: Path, events: list, values: list, **header: Any) -> Path:
    data: dict[str, Any] = {
        "schema": 1,
        "ok": True,
        "error": None,
        "country": "US",
        "server": "Exness-MT5Trial11",
        "terminal_connected": True,
        "generated_gmt": ts(NOW) - 10,
        "generated_server": ts(NOW) - 10,
        "server_gmt_offset_s": 0,
        "service_started_gmt": ts(NOW - timedelta(days=1)),
        "poll_seconds": 15,
        "refresh_seconds": 300,
        "days_back": 7,
        "days_ahead": 14,
        "change_id": "1",
        "values_count": len(values),
        "missing_events": 0,
        "events": events,
        "values": values,
    }
    data.update(header)
    path = tmp_path / "calendar_US.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def cal(path: Path, now: datetime = NOW) -> EconomicCalendar:
    return EconomicCalendar(lambda: path, now_utc=lambda: now)


def only(result: dict[str, Any]) -> dict[str, Any]:
    assert len(result["eventos"]) == 1, result["eventos"]
    return result["eventos"][0]


# ---------------------------------------------------------------- valores
def test_released_cpi_surprise_in_percentage_points_and_revised_previous(tmp_path):
    path = write(
        tmp_path,
        [ev(1, "CPI m/m", "cpi-mm")],
        [val(10, 1, RELEASE, actual=0.4, forecast=0.3, prev=0.2, revised=0.3, impact="POSITIVE")],
    )
    e = only(cal(path).query())
    assert e["situacao"] == "divulgado"
    assert (e["realizado"], e["previsao"], e["anterior"], e["anterior_revisado"]) == (0.4, 0.3, 0.2, 0.3)
    assert "consenso" not in e  # a previsão do MT5 não é necessariamente o consenso de mercado
    assert e["anterior_foi_revisado"] is True
    assert e["surpresa"] == {"valor": 0.1, "unidade": "p.p.", "sentido": "acima da previsão"}
    assert e["unidade"] == "%"
    assert e["horario"] == {"utc": "2026-10-01T12:30:00Z", "sao_paulo": "2026-10-01 09:30:00",
                            "nova_york": "2026-10-01 08:30:00"}
    assert e["impacto_no_usd_segundo_a_fonte"] == "positivo"
    assert e["periodo_referencia"] == "2026-09" and e["frequencia"] == "mensal"
    assert e["id"] == "mt5:1:10" and e["fonte_oficial"].startswith("https://")
    assert "estimativa" not in e  # revisão 0: divulgação única


def test_missing_forecast_is_null_never_zero(tmp_path):
    path = write(tmp_path, [ev(1, "PPI m/m", "ppi-mm")], [val(10, 1, RELEASE, actual=0.2, prev=0.1)])
    e = only(cal(path).query())
    assert e["previsao"] is None
    assert e["surpresa"] == {"valor": None, "motivo": "a fonte não informou previsão"}
    assert e["anterior_revisado"] is None and e["anterior_foi_revisado"] is False


def test_nfp_surprise_in_thousands(tmp_path):
    path = write(
        tmp_path,
        [ev(2, "Nonfarm Payrolls", "nonfarm-payrolls", unit="JOB", mult="THOUSANDS", digits=0)],
        [val(20, 2, RELEASE, actual=254, forecast=150, prev=159, revised=142)],
    )
    e = only(cal(path).query())
    assert e["surpresa"] == {"valor": 104.0, "unidade": "mil empregos", "sentido": "acima da previsão"}
    assert e["unidade"] == "mil empregos"


def test_negative_surprise_and_weekly_period(tmp_path):
    path = write(
        tmp_path,
        [ev(3, "Initial Jobless Claims", "initial-jobless-claims", unit="NONE", mult="THOUSANDS", digits=0,
            frequency="WEEK")],
        [val(30, 3, RELEASE, actual=218, forecast=225, prev=226, period=datetime(2026, 9, 26, tzinfo=UTC))],
    )
    e = only(cal(path).query())
    assert e["surpresa"]["valor"] == -7.0 and e["surpresa"]["sentido"] == "abaixo da previsão"
    assert e["surpresa"]["unidade"] == "mil" and e["frequencia"] == "semanal"
    assert e["periodo_referencia"] == "semana de 2026-09-26"


def test_precision_is_not_lost_to_digits_and_no_negative_zero(tmp_path):
    # Caso real: Case-Shiller com digits=1 e anterior 0.43; arredondar para 1 casa daria "0.0, igual".
    path = write(
        tmp_path,
        [ev(4, "Case-Shiller", "s-p-cs-hpi-composite-20-mm", digits=1)],
        [val(40, 4, RELEASE, actual=0.43, forecast=0.4, prev=0.43)],
    )
    e = only(cal(path).query())
    assert e["realizado"] == 0.43 and e["anterior"] == 0.43
    assert e["surpresa"]["valor"] == 0.03 and e["surpresa"]["sentido"] == "acima da previsão"
    neg = write(tmp_path, [ev(5, "X", "x-mm")], [val(50, 5, RELEASE, actual=-0.0000001, forecast=0.0)])
    assert only(cal(neg).query())["realizado"] == 0.0


def test_currency_unit_is_usd_for_us(tmp_path):
    path = write(
        tmp_path,
        [ev(6, "Balança Comercial", "goods-trade-balance", unit="CURRENCY", mult="BILLIONS", digits=3)],
        [val(60, 6, RELEASE, actual=-81.515, forecast=-80.0)],
    )
    e = only(cal(path).query())
    assert e["unidade"] == "bilhões USD" and e["surpresa"]["unidade"] == "bilhões USD"


# ---------------------------------------------------------------- revisões (estimativas)
def test_revised_estimate_previous_refers_to_same_period(tmp_path):
    # Caso real: GDP q/q de 30/09 (revisão 3) traz como 'anterior' a estimativa anterior do mesmo trimestre.
    path = write(
        tmp_path,
        [ev(7, "PIB (Trimestral)", "gross-domestic-product-qq", frequency="QUARTER")],
        [val(70, 7, RELEASE, actual=2.2, prev=1.5, revision=3, period=datetime(2026, 4, 1, tzinfo=UTC))],
    )
    e = only(cal(path).query())
    assert "MESMO período" in e["estimativa"]
    assert e["periodo_referencia"] == "2026-T2" and e["frequencia"] == "trimestral"


def test_first_estimate_previous_is_prior_period(tmp_path):
    path = write(
        tmp_path,
        [ev(8, "Estoques no atacado", "wholesale-inventories")],
        [val(80, 8, RELEASE, actual=0.7, prev=1.3, revision=1, period=datetime(2026, 8, 1, tzinfo=UTC))],
    )
    e = only(cal(path).query())
    assert e["estimativa"].startswith("primeira estimativa") and e["periodo_referencia"] == "2026-08"


# ---------------------------------------------------------------- nomes traduzidos e medida
def test_mislabeled_headline_cpi_is_identified_by_code(tmp_path):
    # Caso real (2026-10-01): o terminal em português chamava o CPI cheio de "Núcleo".
    name = "Núcleo do Índice de Preços ao Consumidor (IPC) (Mensal)"
    path = write(
        tmp_path,
        [ev(21, name, "consumer-price-index-mm"), ev(22, name, "consumer-price-index-ex-food-energy-mm")],
        [val(210, 21, RELEASE, actual=0.4, forecast=0.3), val(220, 22, RELEASE, actual=0.3, forecast=0.3)],
    )
    result = cal(path).query()
    rows = {r["codigo"]: r for r in result["eventos"]}
    assert rows["consumer-price-index-mm"]["descricao"] == "CPI cheio, variação mensal"
    assert rows["consumer-price-index-mm"]["medida"] == {"variacao": "mensal", "nucleo": False}
    assert rows["consumer-price-index-ex-food-energy-mm"]["medida"] == {"variacao": "mensal", "nucleo": True}
    # Mesmo nome traduzido, medida diferente: os dois contam como simultâneos, identificados pelo código.
    assert rows["consumer-price-index-mm"]["outros_no_mesmo_horario"] == 1
    assert result["mesmo_horario"] == {"2026-10-01T12:30:00Z": [
        "CPI cheio, variação mensal (consumer-price-index-mm)",
        "CPI núcleo (sem alimentos e energia), variação mensal (consumer-price-index-ex-food-energy-mm)"]}


@pytest.mark.parametrize(
    "code,expected",
    [
        ("consumer-price-index-yy", {"variacao": "anual", "nucleo": False}),
        ("core-pce-price-index-mm", {"variacao": "mensal", "nucleo": True}),
        ("consumer-price-index-ex-food-energy-nsa-mm", {"variacao": "mensal", "ajuste_sazonal": False,
                                                        "nucleo": True}),
        ("gross-domestic-product-qq", {"variacao": "trimestral"}),
        ("real-pce-qq", {"variacao": "trimestral"}),  # consumo, não preço: sem 'nucleo'
        ("nonfarm-payrolls", None),
        ("ism-prices-paid", None),
    ],
)
def test_measure_from_code(code, expected):
    assert measure(code) == expected


SEARCH_EVENTS = [
    ("Núcleo do Índice de Preços ao Consumidor (IPC) (Mensal)", "consumer-price-index-mm"),
    ("Núcleo do Índice de Preços ao Consumidor (IPC) (Mensal)", "consumer-price-index-ex-food-energy-mm"),
    ("Relatório de Emprego (Payroll) não-agrícola", "nonfarm-payrolls"),
    ("Pedidos Iniciais de Subsídio de Desemprego", "initial-jobless-claims"),
    ("Discurso de Waller, Governador do Fed", "fed-governor-waller-speech"),
    ("Atas da Reunião do FOMC", "fomc-minutes"),
    ("Índice de Atividade Industrial do Fed de Filadélfia", "philadelphia-fed-manufacturing-index"),
    ("Saldo do Orçamento Federal", "federal-budget-balance"),
    ("PCE real (Trimestral)", "real-pce-qq"),
    ("Núcleo do PCE (Mensal)", "core-pce-price-index-mm"),
]


@pytest.mark.parametrize(
    "search,expected",
    [
        ("CPI", {"consumer-price-index-mm", "consumer-price-index-ex-food-energy-mm"}),
        ("core CPI", {"consumer-price-index-ex-food-energy-mm"}),
        ("CPI núcleo", {"consumer-price-index-ex-food-energy-mm"}),
        ("inflação", {"consumer-price-index-mm", "consumer-price-index-ex-food-energy-mm",
                      "core-pce-price-index-mm"}),
        ("PCE", {"core-pce-price-index-mm"}),
        ("NFP", {"nonfarm-payrolls"}),
        ("Jobless Claims", {"initial-jobless-claims"}),
        ("subsídio", {"initial-jobless-claims"}),
        ("Fed", {"fed-governor-waller-speech", "fomc-minutes"}),
        ("FOMC", {"fomc-minutes"}),
        ("filadélfia", {"philadelphia-fed-manufacturing-index"}),
        ("varejo", set()),
    ],
)
def test_search(tmp_path, search, expected):
    events = [ev(300 + i, name, code) for i, (name, code) in enumerate(SEARCH_EVENTS)]
    values = [val(400 + i, 300 + i, RELEASE) for i in range(len(SEARCH_EVENTS))]
    found = {r["codigo"] for r in cal(write(tmp_path, events, values)).query(search=search)["eventos"]}
    assert found == expected


# ---------------------------------------------------------------- janela, filtros e limite
def test_next_30_minutes(tmp_path):
    soon = NOW + timedelta(minutes=25)
    path = write(tmp_path, [ev(4, "ISM Manufacturing PMI", "ism-manufacturing-pmi", unit="NONE")],
                 [val(40, 4, soon, forecast=49.5, prev=48.7)])
    e = only(cal(path).query(hours_back=0, hours_ahead=0.5))
    assert e["situacao"] == "agendado" and e["faltam_min"] == 25.0
    assert e["realizado"] is None and e["surpresa"] is None
    assert cal(path).query(hours_back=0, hours_ahead=0.25)["eventos"] == []


def test_importance_filter(tmp_path):
    path = write(
        tmp_path,
        [ev(5, "Initial Jobless Claims", "initial-jobless-claims", importance="MODERATE"),
         ev(6, "Redbook Index y/y", "redbook-index", importance="LOW")],
        [val(50, 5, RELEASE, actual=218), val(60, 6, RELEASE, actual=5.1)],
    )
    c = cal(path)
    assert [e["codigo"] for e in c.query()["eventos"]] == ["initial-jobless-claims"]
    assert len(c.query(min_importance="baixa")["eventos"]) == 2
    assert c.query(min_importance="alta")["eventos"] == []
    with pytest.raises(ValueError):
        c.query(min_importance="altíssima")


def test_limit_keeps_most_important_and_closest_then_sorts_by_time(tmp_path):
    # Muitos eventos baixos de ontem não podem esconder o NFP de amanhã.
    events = [ev(100 + i, f"Baixo {i}", f"low-{i}", importance="LOW") for i in range(5)]
    events.append(ev(200, "NFP", "nonfarm-payrolls", unit="JOB", mult="THOUSANDS"))
    values = [val(500 + i, 100 + i, NOW - timedelta(hours=20 - i)) for i in range(5)]
    values.append(val(600, 200, NOW + timedelta(hours=20), forecast=52))
    result = cal(write(tmp_path, events, values)).query(hours_back=24, hours_ahead=24, min_importance="baixa",
                                                       limit=3)
    codes = [e["codigo"] for e in result["eventos"]]
    assert "nonfarm-payrolls" in codes and len(codes) == 3 and result["total"] == 6
    assert codes == ["low-3", "low-4", "nonfarm-payrolls"]  # os baixos mais próximos de agora, em ordem
    assert any("Mostrando 3 de 6" in n for n in result["observacoes"])


def test_simultaneous_events_listed_even_if_filtered_out(tmp_path):
    path = write(
        tmp_path,
        [ev(7, "Core CPI m/m", "core-cpi-mm"), ev(8, "Initial Jobless Claims", "initial-jobless-claims",
                                                  importance="LOW")],
        [val(70, 7, RELEASE, actual=0.3, forecast=0.3), val(80, 8, RELEASE, actual=218)],
    )
    result = cal(path).query(min_importance="alta")
    e = only(result)
    assert e["outros_no_mesmo_horario"] == 1
    assert "Pedidos iniciais de seguro-desemprego (initial-jobless-claims)" in result["mesmo_horario"][
        "2026-10-01T12:30:00Z"]
    assert e["surpresa"]["sentido"] == "igual à previsão"


def test_no_same_time_group_for_lone_event(tmp_path):
    path = write(tmp_path, [ev(1, "CPI m/m", "cpi-mm")], [val(10, 1, RELEASE, actual=0.4)])
    result = cal(path).query()
    assert "outros_no_mesmo_horario" not in only(result) and result["mesmo_horario"] == {}


# ---------------------------------------------------------------- situação do evento
def test_waiting_for_actual_after_release_time(tmp_path):
    path = write(tmp_path, [ev(9, "Retail Sales m/m", "retail-sales-mm")], [val(90, 9, RELEASE, forecast=0.4)])
    e = only(cal(path).query())
    assert e["situacao"] == "aguardando_realizado" and e["atraso_min"] == 15.0


def test_speech_has_no_values(tmp_path):
    path = write(tmp_path, [ev(11, "Fed Chair Speech", "fed-chair-speech", kind="EVENT", unit="NONE")],
                 [val(110, 11, RELEASE)])
    e = only(cal(path).query())
    assert e["situacao"] == "evento_sem_valores" and "realizado" not in e


def test_holiday_always_listed_with_date_only(tmp_path):
    # Caso real: Columbus Day tem importância NONE e horário 00:00 do dia (modo DATE).
    day = datetime(2026, 10, 12, tzinfo=UTC)
    path = write(
        tmp_path,
        [ev(12, "Dia de Colombo", "columbus-day", importance="NONE", kind="HOLIDAY", unit="NONE",
            time_mode="DATE")],
        [val(120, 12, day, period=None)],
    )
    during = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)  # consulta durante o feriado, olhando 2 h para trás
    e = only(cal(path, now=during).query(min_importance="alta"))
    assert e["situacao"] == "feriado" and e["data"] == "2026-10-12" and "horario" not in e
    assert e["horario_tipo"] == "dia_inteiro"
    assert only(cal(path, now=NOW).query(hours_ahead=24 * 14))["situacao"] == "feriado"


def test_tentative_time(tmp_path):
    path = write(
        tmp_path,
        [ev(13, "FOMC Member Speaks", "fomc-member", kind="EVENT", unit="NONE", time_mode="TENTATIVE")],
        [val(130, 13, NOW + timedelta(hours=3))],
    )
    e = only(cal(path).query())
    assert (e["situacao"], e["horario_tipo"]) == ("agendado", "provisorio") and "horario" in e


# ---------------------------------------------------------------- latência e base de tempo
def test_latency_only_when_service_was_running_before_release(tmp_path):
    seen = RELEASE + timedelta(seconds=40)
    path = write(tmp_path, [ev(14, "CPI y/y", "cpi-yy")], [val(140, 14, RELEASE, actual=3.1, forecast=3.0, seen=seen)])
    e = only(cal(path).query())
    assert e["latencia_fonte_s"] == 40.0 and e["realizado_visto"]["utc"] == "2026-10-01T12:30:40Z"

    # Serviço iniciado depois da divulgação: nada de latência, mesmo que 'seen' venha preenchido.
    started_late = write(tmp_path, [ev(14, "CPI y/y", "cpi-yy")],
                         [val(140, 14, RELEASE, actual=3.1, seen=RELEASE + timedelta(minutes=10, seconds=40))],
                         service_started_gmt=ts(RELEASE + timedelta(minutes=10)))
    assert "latencia_fonte_s" not in only(cal(started_late).query())


def test_server_offset_is_converted_and_noted(tmp_path):
    # Servidor em UTC+3: o MT5 grava 15:30 para um evento das 12:30 UTC; o período fica como data do servidor.
    value = val(150, 15, RELEASE + timedelta(hours=3), actual=0.2)
    path = write(tmp_path, [ev(15, "PPI m/m", "ppi-mm")], [value], server_gmt_offset_s=10_800)
    result = cal(path).query()
    assert only(result)["horario"]["utc"] == "2026-10-01T12:30:00Z"
    assert only(result)["periodo_referencia"] == "2026-09"
    assert any("não está em UTC" in n for n in result["observacoes"])


def test_offset_jitter_of_one_second_is_ignored(tmp_path):
    path = write(tmp_path, [ev(16, "PPI m/m", "ppi-mm")], [val(160, 16, RELEASE, actual=0.2)], server_gmt_offset_s=1)
    result = cal(path).query()
    assert only(result)["horario"]["utc"] == "2026-10-01T12:30:00Z"
    assert not any("não está em UTC" in n for n in result["observacoes"])


# ---------------------------------------------------------------- estado do arquivo
def test_fresh_file_is_current(tmp_path):
    result = cal(write(tmp_path, [], [])).query()
    assert result["estado"] == "atual" and result["idade_arquivo_s"] == 10.0
    assert result["fonte"].endswith("Exness-MT5Trial11") and result["eventos"] == []
    assert any("não tem nenhum valor" in n for n in result["observacoes"])


def test_stale_file_is_flagged(tmp_path):
    result = cal(write(tmp_path, [], [], generated_gmt=ts(NOW - timedelta(hours=1)))).query()
    assert result["estado"] == "desatualizado"
    assert any("parecem parados" in n for n in result["observacoes"])


def test_disconnected_terminal_noted(tmp_path):
    result = cal(write(tmp_path, [], [], terminal_connected=False)).query()
    assert any("sem conexão" in n for n in result["observacoes"])


def test_values_without_event_description_are_reported(tmp_path):
    path = write(tmp_path, [ev(1, "CPI m/m", "cpi-mm")], [val(10, 1, RELEASE), val(11, 999, RELEASE)])
    result = cal(path).query()
    assert len(result["eventos"]) == 1
    assert any("sem descrição do evento" in n for n in result["observacoes"])


def test_missing_file_explains_how_to_install(tmp_path):
    with pytest.raises(CalendarError, match="TradingMcpCalendar"):
        cal(tmp_path / "nao_existe.json").query()


def test_service_error_is_reported(tmp_path):
    path = write(tmp_path, [], [], ok=False, error="CalendarValueHistory falhou: 5401")
    with pytest.raises(CalendarError, match="5401"):
        cal(path).query()


def test_unknown_schema_is_rejected(tmp_path):
    with pytest.raises(CalendarError, match="schema"):
        cal(write(tmp_path, [], [], schema=99)).query()


def test_corrupt_file_is_reported(tmp_path):
    path = tmp_path / "calendar_US.json"
    path.write_text("{ quebrado", encoding="utf-8")
    with pytest.raises(CalendarError, match="Não foi possível ler"):
        cal(path).query()


def test_events_between_for_journal(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        [
            ev(1, "Nonfarm Payrolls", "nonfarm-payrolls", unit="JOB", mult="THOUSANDS", digits=0),
            ev(2, "Fala de dirigente", "fed-speech", importance="MODERATE", kind="EVENT"),
            ev(3, "Columbus Day", "columbus-day", importance="NONE", kind="HOLIDAY", time_mode="DATE"),
        ],
        [val(10, 1, RELEASE), val(11, 2, RELEASE), val(12, 3, RELEASE.replace(hour=0, minute=0))],
    )
    cal = EconomicCalendar(lambda: path, now_utc=lambda: NOW)
    events, covered = cal.events_between(RELEASE - timedelta(minutes=30), RELEASE + timedelta(hours=1))
    assert covered is True
    assert [e["codigo"] for e in events] == ["nonfarm-payrolls"]  # só alta, sem feriado
    assert events[0]["utc"] == "2026-10-01T12:30:00Z" and events[0]["importancia"] == "alta"
    moderate, _ = cal.events_between(RELEASE - timedelta(minutes=1), RELEASE + timedelta(minutes=1), "moderada")
    assert {e["codigo"] for e in moderate} == {"nonfarm-payrolls", "fed-speech"}
    _, old = cal.events_between(NOW - timedelta(days=8), NOW - timedelta(days=7, hours=23))
    assert old is False  # antes dos 7 dias exportados
    with pytest.raises(ValueError):
        cal.events_between(NOW, NOW, "altissima")


def test_coverage_follows_the_service_days(tmp_path: Path) -> None:
    path = write(tmp_path, [], [], days_back=100, days_ahead=14)
    first, last = cal(path).coverage()
    generated = NOW - timedelta(seconds=10)
    assert first == generated - timedelta(days=100) and last == generated + timedelta(days=14)


def test_matches_search_uses_the_calendar_aliases() -> None:
    assert matches_search("claims", "initial-jobless-claims", "Pedidos")
    assert matches_search("nfp", "nonfarm-payrolls") and not matches_search("nfp", "adp-nonfarm-employment-change")
    assert matches_search("cpi nucleo", "consumer-price-index-ex-food-energy-mm")
    assert not matches_search("cpi nucleo", "consumer-price-index-mm")
    assert matches_search("pedidos", "initial-jobless-claims", "Pedidos iniciais")
