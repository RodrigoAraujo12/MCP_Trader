from __future__ import annotations

from typing import Any

import httpx
import pytest

from trading_mcp.sec_edgar import SecEdgarClient, SecEdgarError

UA = "tester test@example.com"

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    "2": {"cik_str": 999, "ticker": "FORX", "title": "Foreign Co ADR"},
}


def dur(start: str, end: str, val: float, form: str = "10-K", filed: str = "2025-11-01", fy: int = 2025) -> dict:
    return {"start": start, "end": end, "val": val, "fy": fy, "fp": "FY", "form": form, "filed": filed}


def inst(end: str, val: float, form: str = "10-K", filed: str = "2025-11-01") -> dict:
    return {"end": end, "val": val, "fy": 2025, "fp": "FY", "form": form, "filed": filed}


def tag(units: dict[str, list[dict]]) -> dict:
    return {"units": units}


def usd(*facts: dict) -> dict:
    return tag({"USD": list(facts)})


def apple_like() -> dict:
    return {"facts": {
        "dei": {"EntityCommonStockSharesOutstanding": tag({"shares": [
            {"end": "2025-10-17", "val": 14.8e9, "form": "10-K", "filed": "2025-10-31"},
            {"end": "2026-01-16", "val": 14.7e9, "form": "10-Q", "filed": "2026-01-30"}]})},
        "us-gaap": {
            # tag antiga, abandonada: dado de 2018
            "Revenues": usd(dur("2017-10-01", "2018-09-29", 265e9, filed="2018-11-05", fy=2018)),
            "RevenueFromContractWithCustomerExcludingAssessedTax": usd(
                # ano anterior (comparativo) arquivado DEPOIS do ano corrente em outra reapresentação
                dur("2023-10-01", "2024-09-28", 391e9, filed="2025-10-31"),
                dur("2024-09-29", "2025-09-27", 416e9, filed="2025-10-31"),
                # trimestre do 10-K (Q4) e 10-Q posterior
                dur("2025-06-29", "2025-09-27", 102e9, filed="2025-10-31"),
                dur("2025-09-28", "2025-12-27", 140e9, form="10-Q", filed="2026-01-30", fy=2026),
                # acumulado 6 meses (ignorar)
                dur("2025-09-28", "2026-03-28", 250e9, form="10-Q", filed="2026-05-01", fy=2026),
            ),
            "NetIncomeLoss": usd(
                dur("2023-10-01", "2024-09-28", 93e9, filed="2025-10-31"),
                dur("2024-09-29", "2025-09-27", 112e9, filed="2025-10-31"),
                dur("2025-09-28", "2025-12-27", 40e9, form="10-Q", filed="2026-01-30", fy=2026),
            ),
            "EarningsPerShareDiluted": tag({
                "USD/shares": [
                    dur("2024-09-29", "2025-09-27", 7.46, filed="2025-10-31"),
                    dur("2025-09-28", "2025-12-27", 2.84, form="10-Q", filed="2026-01-30", fy=2026)],
                "USD": [dur("2024-09-29", "2025-09-27", 999, filed="2025-10-31")],  # unidade errada
            }),
            "NetCashProvidedByUsedInOperatingActivities": usd(dur("2024-09-29", "2025-09-27", 111e9)),
            "Assets": usd(inst("2024-09-28", 364e9), inst("2025-09-27", 359e9),
                          inst("2025-12-27", 380e9, form="10-Q", filed="2026-01-30")),
            "Liabilities": usd(inst("2025-09-27", 285e9)),
            "StockholdersEquity": usd(inst("2025-09-27", 74e9), inst("2025-12-27", 80e9, form="10-Q")),
            "CashAndCashEquivalentsAtCarryingValue": usd(inst("2025-09-27", 35e9)),
            "LongTermDebtNoncurrent": usd(inst("2025-09-27", 90e9)),
        },
    }}


def make_client(facts: dict | None = None, *, status: dict[str, int] | None = None,
                user_agent: str | None = UA, tickers: dict | None = None
                ) -> tuple[SecEdgarClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []
    status = status or {}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        key = "tickers" if "company_tickers" in req.url.path else "facts"
        if key in status:
            return httpx.Response(status[key])
        if key == "tickers":
            return httpx.Response(200, json=tickers or TICKERS)
        return httpx.Response(200, json=facts if facts is not None else apple_like())

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return SecEdgarClient(user_agent, http_client=http), calls


# ---------------------------------------------------------------- tickers
def test_ticker_normalization_and_cik():
    c, _ = make_client()
    assert c.ticker_to_cik(" aapl ") == (320193, "Apple Inc.")
    assert c.ticker_to_cik("BRK.B") == (1067983, "BERKSHIRE HATHAWAY INC")
    assert c.ticker_to_cik("brk.b")[0] == 1067983


def test_unknown_ticker():
    c, _ = make_client()
    with pytest.raises(SecEdgarError, match="não encontrado"):
        c.ticker_to_cik("ZZZZ")


def test_missing_user_agent_raises_only_on_call():
    for ua in (None, "", "   "):
        c = SecEdgarClient(ua)  # construir não levanta
        with pytest.raises(SecEdgarError, match="SEC_USER_AGENT"):
            c.ticker_to_cik("AAPL")
        with pytest.raises(SecEdgarError, match="SEC_USER_AGENT"):
            c.fundamentals("AAPL")


def test_ticker_map_fetched_once_and_headers_sent():
    c, calls = make_client()
    c.fundamentals("AAPL")
    c.fundamentals("AAPL")
    ticker_calls = [r for r in calls if "company_tickers" in r.url.path]
    assert len(ticker_calls) == 1
    assert len(calls) == 3  # 1 mapa + 2 fatos
    for r in calls:
        assert r.headers["User-Agent"] == UA
        assert "gzip" in r.headers["Accept-Encoding"]
    assert "CIK0000320193" in str(calls[1].url)


# ---------------------------------------------------------------- seleção
def test_annual_ignores_prior_year_and_picks_latest_end():
    r = make_client()[0].fundamentals("AAPL")
    a = r["anual"]
    assert a["fim_periodo"] == "2025-09-27"
    assert a["ano_fiscal"] == 2025
    assert a["formulario"] == "10-K"
    assert a["receita"] == 416e9  # não o comparativo 391e9, nem a tag antiga
    assert a["lucro_liquido"] == 112e9
    assert a["fluxo_caixa_operacional"] == 111e9
    assert r["ticker"] == "AAPL" and r["empresa"] == "Apple Inc." and r["cik"] == 320193
    assert r["fonte"].startswith("SEC EDGAR")


def test_restatement_tiebreak_by_filed():
    f = apple_like()
    f["facts"]["us-gaap"]["NetIncomeLoss"] = usd(
        dur("2024-09-29", "2025-09-27", 100e9, filed="2025-10-31"),
        dur("2024-09-29", "2025-09-27", 105e9, form="10-K/A", filed="2025-12-15"),
    )
    a = make_client(f)[0].fundamentals("AAPL")["anual"]
    assert a["lucro_liquido"] == 105e9
    assert a["arquivado_em"] in ("2025-10-31", "2025-12-15")


def test_quarterly_selection_80_to_100_days():
    q = make_client()[0].fundamentals("AAPL")["trimestral"]
    assert q["fim_periodo"] == "2025-12-27"
    assert q["formulario"] == "10-Q"
    assert q["receita"] == 140e9  # ignora acumulado de 6 meses mais recente
    assert q["lucro_liquido"] == 40e9
    assert q["lpa_diluido"] == 2.84


def test_eps_uses_usd_per_share_unit():
    a = make_client()[0].fundamentals("AAPL")["anual"]
    assert a["lpa_diluido"] == 7.46


def test_instant_facts_aligned_with_annual_end():
    a = make_client()[0].fundamentals("AAPL")["anual"]
    assert a["ativos"] == 359e9  # não 380e9 do 10-Q posterior
    assert a["patrimonio_liquido"] == 74e9
    assert a["passivos"] == 285e9
    assert a["caixa"] == 35e9
    assert a["divida_longo_prazo"] == 90e9


def test_shares_outstanding_latest_dei():
    s = make_client()[0].fundamentals("AAPL")["acoes_em_circulacao"]
    assert s == {"valor": 14.7e9, "data": "2026-01-16"}


def test_freshest_tag_wins_for_revenue():
    f = apple_like()
    g = f["facts"]["us-gaap"]
    # tag antiga com valor ENORME mas defasada; ASC 606 é a mais nova
    g["Revenues"] = usd(dur("2017-10-01", "2018-09-29", 1e12, filed="2018-11-05"))
    assert make_client(f)[0].fundamentals("AAPL")["anual"]["receita"] == 416e9
    # invertido: só a tag antiga "Revenues" está atual (ex.: banco)
    g["RevenueFromContractWithCustomerExcludingAssessedTax"] = usd(
        dur("2019-01-01", "2019-12-31", 5e9, filed="2020-02-01"))
    g["Revenues"] = usd(dur("2024-09-29", "2025-09-27", 420e9, filed="2025-10-31"))
    assert make_client(f)[0].fundamentals("AAPL")["anual"]["receita"] == 420e9


def test_freshest_tag_tie_uses_contract_order():
    f = apple_like()
    f["facts"]["us-gaap"]["Revenues"] = usd(dur("2024-09-29", "2025-09-27", 1.0, filed="2025-10-31"))
    assert make_client(f)[0].fundamentals("AAPL")["anual"]["receita"] == 416e9


def test_stale_metric_is_none_with_observation():
    f = apple_like()
    f["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"] = usd(
        dur("2019-10-01", "2020-09-26", 80e9, filed="2020-10-30"))
    r = make_client(f)[0].fundamentals("AAPL")
    assert r["anual"]["fluxo_caixa_operacional"] is None
    assert any("fluxo_caixa_operacional" in o for o in r["observacoes"])


def test_missing_metrics_none_and_observation():
    f = apple_like()
    del f["facts"]["us-gaap"]["LongTermDebtNoncurrent"]
    del f["facts"]["us-gaap"]["CashAndCashEquivalentsAtCarryingValue"]
    r = make_client(f)[0].fundamentals("AAPL")
    assert r["anual"]["divida_longo_prazo"] is None
    assert r["anual"]["caixa"] is None
    obs = " ".join(r["observacoes"])
    assert "divida_longo_prazo" in obs and "caixa" in obs


def test_foreign_filer_ifrs_observation():
    f = {"facts": {"ifrs-full": {"Revenue": usd(dur("2024-01-01", "2024-12-31", 1e9, form="20-F"))}}}
    r = make_client(f)[0].fundamentals("FORX")
    assert r["anual"]["receita"] is None
    assert r["anual"]["fim_periodo"] is None
    assert any("20-F" in o and "IFRS" in o for o in r["observacoes"])


def test_foreign_filer_by_form_only():
    f = {"facts": {"us-gaap": {"Assets": usd(inst("2024-12-31", 1e9, form="20-F"))}}}
    r = make_client(f)[0].fundamentals("FORX")
    assert any("20-F" in o for o in r["observacoes"])


# ---------------------------------------------------------------- cálculo
def test_margin_and_roe_math():
    a = make_client()[0].fundamentals("AAPL")["anual"]
    assert a["margem_liquida_pct"] == round(112 / 416 * 100, 2)
    assert a["roe_pct"] == round(112 / 74 * 100, 2)


@pytest.mark.parametrize("equity", [0, -5e9])
def test_roe_none_when_equity_not_positive(equity: float):
    f = apple_like()
    f["facts"]["us-gaap"]["StockholdersEquity"] = usd(inst("2025-09-27", equity))
    a = make_client(f)[0].fundamentals("AAPL")["anual"]
    assert a["roe_pct"] is None
    assert a["margem_liquida_pct"] is not None


def test_margin_none_without_revenue():
    f = apple_like()
    for t in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"):
        del f["facts"]["us-gaap"][t]
    a = make_client(f)[0].fundamentals("AAPL")["anual"]
    assert a["receita"] is None and a["margem_liquida_pct"] is None


# ---------------------------------------------------------------- HTTP
def test_http_403_message():
    c, _ = make_client(status={"tickers": 403})
    with pytest.raises(SecEdgarError, match="SEC_USER_AGENT"):
        c.ticker_to_cik("AAPL")


def test_http_404_facts_message():
    c, _ = make_client(status={"facts": 404})
    with pytest.raises(SecEdgarError, match="XBRL"):
        c.fundamentals("AAPL")


def test_http_500_message():
    c, _ = make_client(status={"facts": 500})
    with pytest.raises(SecEdgarError, match="500"):
        c.fundamentals("AAPL")


def test_timeout_and_network_errors():
    def boom(exc: Exception) -> SecEdgarClient:
        def handler(req: httpx.Request) -> Any:
            raise exc
        return SecEdgarClient(UA, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(SecEdgarError, match="Tempo esgotado"):
        boom(httpx.ReadTimeout("slow")).ticker_to_cik("AAPL")
    with pytest.raises(SecEdgarError, match="rede"):
        boom(httpx.ConnectError("down")).ticker_to_cik("AAPL")


# ------------------------------------------------- auditoria: novas regras
def _gaap(**tags: Any) -> dict:
    return {"facts": {"us-gaap": tags}}


def msft_like(*, ytd: bool = True, q4_direct: bool = False) -> dict:
    """Ano fiscal termina em 30/06/2025; último 10-Q real termina em 31/03/2025."""
    rev = [dur("2024-07-01", "2025-06-30", 300.0, filed="2025-07-30"),
           dur("2025-01-01", "2025-03-31", 70.0, form="10-Q", filed="2025-04-30")]
    ni = [dur("2024-07-01", "2025-06-30", 100.0, filed="2025-07-30"),
          dur("2025-01-01", "2025-03-31", 25.0, form="10-Q", filed="2025-04-30")]
    eps = [dur("2024-07-01", "2025-06-30", 13.0, filed="2025-07-30"),
           dur("2025-01-01", "2025-03-31", 3.3, form="10-Q", filed="2025-04-30")]
    if ytd:
        rev.append(dur("2024-07-01", "2025-03-31", 210.0, form="10-Q", filed="2025-04-30"))
        ni.append(dur("2024-07-01", "2025-03-31", 70.0, form="10-Q", filed="2025-04-30"))
    if q4_direct:
        ni.append(dur("2025-04-01", "2025-06-30", 31.0, filed="2025-07-30"))
        rev.append(dur("2025-04-01", "2025-06-30", 90.0, filed="2025-07-30"))
    return _gaap(
        RevenueFromContractWithCustomerExcludingAssessedTax=usd(*rev),
        NetIncomeLoss=usd(*ni),
        EarningsPerShareDiluted=tag({"USD/shares": eps}),
    )


def test_revenue_tie_takes_largest_value():
    # estilo Capital One: ASC 606 (subconjunto) empata com Revenues (total)
    f = apple_like()
    f["facts"]["us-gaap"]["Revenues"] = usd(dur("2024-09-29", "2025-09-27", 500e9, filed="2025-10-31"))
    assert make_client(f)[0].fundamentals("AAPL")["anual"]["receita"] == 500e9
    # a tag fresca ainda vence quando a maior é defasada (não é "maior global")
    f["facts"]["us-gaap"]["Revenues"] = usd(dur("2022-09-29", "2023-09-27", 900e9, filed="2023-10-31"))
    assert make_client(f)[0].fundamentals("AAPL")["anual"]["receita"] == 416e9


def test_bank_revenues_net_of_interest_expense_quarter():
    f = _gaap(
        Revenues=usd(dur("2024-01-01", "2024-12-31", 170e9)),
        RevenuesNetOfInterestExpense=usd(
            dur("2024-01-01", "2024-12-31", 180e9),
            dur("2025-04-01", "2025-06-30", 45e9, form="10-Q")),
        NetIncomeLoss=usd(dur("2024-01-01", "2024-12-31", 58e9),
                          dur("2025-04-01", "2025-06-30", 15e9, form="10-Q")),
    )
    r = make_client(f)[0].fundamentals("AAPL")
    assert r["trimestral"]["receita"] == 45e9
    assert r["trimestral"]["fim_periodo"] == "2025-06-30"
    assert r["anual"]["receita"] == 180e9


def test_equity_tie_prefers_parent_only():
    f = apple_like()
    f["facts"]["us-gaap"]["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"] = usd(
        inst("2025-09-27", 99e9))
    a = make_client(f)[0].fundamentals("AAPL")["anual"]
    assert a["patrimonio_liquido"] == 74e9
    assert a["roe_pct"] == round(112 / 74 * 100, 2)


@pytest.mark.parametrize("end", ["2018-09-29", "2025-12-27", "2025-10-20"])
def test_unaligned_instants_are_none_with_observation(end: str):
    f = apple_like()
    g = f["facts"]["us-gaap"]
    g["CashAndCashEquivalentsAtCarryingValue"] = usd(inst(end, 7e9))
    g["StockholdersEquity"] = usd(inst(end, 70e9))
    r = make_client(f)[0].fundamentals("AAPL")
    assert r["anual"]["caixa"] is None
    assert r["anual"]["patrimonio_liquido"] is None
    assert r["anual"]["roe_pct"] is None
    obs = " ".join(r["observacoes"])
    assert "caixa" in obs and "patrimonio_liquido" in obs


def test_instant_within_7_days_accepted():
    f = apple_like()
    f["facts"]["us-gaap"]["Liabilities"] = usd(inst("2025-09-30", 286e9))
    assert make_client(f)[0].fundamentals("AAPL")["anual"]["passivos"] == 286e9


def test_stale_shares_discarded_against_annual_end():
    f = apple_like()
    f["facts"]["dei"] = {"EntityCommonStockSharesOutstanding": tag({"shares": [
        {"end": "2011-02-10", "val": 1.5e6, "form": "10-K", "filed": "2011-03-01"}]})}
    r = make_client(f)[0].fundamentals("AAPL")
    assert r["acoes_em_circulacao"] == {"valor": None, "data": None}
    assert any("2011-02-10" in o for o in r["observacoes"])


def test_stale_shares_without_annual_block_uses_today():
    f = _gaap(NetIncomeLoss=usd(dur("2025-04-01", "2025-06-30", 1.0, form="10-Q")))
    f["facts"]["dei"] = {"EntityCommonStockSharesOutstanding": tag({"shares": [
        {"end": "2000-01-01", "val": 5.0, "form": "10-Q", "filed": "2000-02-01"}]})}
    r = make_client(f)[0].fundamentals("AAPL")
    assert r["anual"]["fim_periodo"] is None
    assert r["acoes_em_circulacao"] == {"valor": None, "data": None}
    assert any("2000-01-01" in o for o in r["observacoes"])


def test_16_week_quarter_of_112_days_accepted():
    f = _gaap(NetIncomeLoss=usd(dur("2025-01-01", "2025-04-23", 5.0, form="10-Q")),  # 112 dias
              Revenues=usd(dur("2025-01-01", "2025-04-23", 50.0, form="10-Q")))
    q = make_client(f)[0].fundamentals("AAPL")["trimestral"]
    assert q["lucro_liquido"] == 5.0 and q["receita"] == 50.0


def test_ten_k_tagging_only_q4_revenue_does_not_blank_net_income():
    f = _gaap(
        Revenues=usd(dur("2025-07-01", "2025-09-30", 80.0, filed="2025-11-05")),  # só o Q4 no 10-K
        NetIncomeLoss=usd(dur("2025-04-01", "2025-06-30", 9.0, form="10-Q", filed="2025-08-01")),
        EarningsPerShareDiluted=tag({"USD/shares": [dur("2025-04-01", "2025-06-30", 0.9, form="10-Q")]}),
    )
    r = make_client(f)[0].fundamentals("AAPL")
    q = r["trimestral"]
    assert q["fim_periodo"] == "2025-06-30"
    assert q["lucro_liquido"] == 9.0 and q["lpa_diluido"] == 0.9
    assert q["receita"] is None
    assert any("receita" in o and "2025-09-30" in o for o in r["observacoes"])


def test_q4_derived_from_annual_minus_nine_months():
    r = make_client(msft_like())[0].fundamentals("AAPL")
    q = r["trimestral"]
    assert q["derivado"] is True
    assert q["fim_periodo"] == "2025-06-30"
    assert q["receita"] == 90.0 and q["lucro_liquido"] == 30.0
    assert q["lpa_diluido"] is None
    assert q["formulario"] == "10-K (derivado: anual - 9 meses)"
    assert any("LPA do 4º trimestre não é derivável" in o for o in r["observacoes"])
    assert r["anual"]["receita"] == 300.0


def test_q4_fallback_to_real_quarter_when_ytd_missing():
    r = make_client(msft_like(ytd=False))[0].fundamentals("AAPL")
    q = r["trimestral"]
    assert q["derivado"] is False
    assert q["fim_periodo"] == "2025-03-31"
    assert q["lucro_liquido"] == 25.0 and q["lpa_diluido"] == 3.3
    assert q["formulario"] == "10-Q"


def test_q4_tagged_directly_is_not_derived():
    q = make_client(msft_like(q4_direct=True))[0].fundamentals("AAPL")["trimestral"]
    assert q["derivado"] is False
    assert q["fim_periodo"] == "2025-06-30"
    assert q["lucro_liquido"] == 31.0 and q["receita"] == 90.0


def test_q4_inconsistent_derived_revenue_is_discarded():
    # 9 meses reapresentados (ex.: operações descontinuadas) maiores que o anual -> Q4 negativo.
    f = msft_like()
    rev = f["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
    for fact in rev:
        if fact["start"] == "2024-07-01" and fact["end"] == "2025-03-31":
            fact["val"] = 320.0
    r = make_client(f)[0].fundamentals("AAPL")
    q = r["trimestral"]
    assert q["derivado"] is True and q["lucro_liquido"] == 30.0
    assert q["receita"] is None
    assert any("inconsistente" in o for o in r["observacoes"])


def test_non_string_form_is_skipped_not_crash():
    f = apple_like()
    f["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"].append(
        {"start": "2024-09-29", "end": "2025-09-27", "val": 1.0, "form": ["10-K"], "filed": "2025-10-31"})
    assert make_client(f)[0].fundamentals("AAPL")["anual"]["lucro_liquido"] == 112e9


def test_real_quarter_after_annual_end_is_not_derived():
    q = make_client()[0].fundamentals("AAPL")["trimestral"]
    assert q["derivado"] is False and q["fim_periodo"] == "2025-12-27"


@pytest.mark.parametrize("payload", [
    {"facts": None},
    {"facts": []},
    [],
    {"outra": 1},
])
def test_malformed_top_level_raises_sec_edgar_error(payload: Any):
    with pytest.raises(SecEdgarError, match="inválida"):
        make_client(payload)[0].fundamentals("AAPL")


def test_malformed_inner_shapes_do_not_crash():
    f = apple_like()
    g = f["facts"]["us-gaap"]
    g["Assets"] = {"units": None}
    g["Liabilities"] = {}  # sem "units"
    g["NetCashProvidedByUsedInOperatingActivities"] = usd(
        dur("2024-09-29", "2025-09-27", "abc"), "lixo", None, {"end": "2025-09-27"})
    g["CashAndCashEquivalentsAtCarryingValue"] = "texto"
    g["StockholdersEquity"] = usd(inst("2025-09-27", True))
    f["facts"]["dei"] = None
    f["facts"]["lixo"] = {"X": {"units": {"USD": "nope"}}}
    r = make_client(f)[0].fundamentals("AAPL")
    a = r["anual"]
    assert a["ativos"] is None and a["passivos"] is None and a["caixa"] is None
    assert a["fluxo_caixa_operacional"] is None and a["patrimonio_liquido"] is None
    assert a["receita"] == 416e9
    assert r["acoes_em_circulacao"] == {"valor": None, "data": None}
