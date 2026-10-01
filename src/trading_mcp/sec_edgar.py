"""Fundamentos de empresas dos EUA via SEC EDGAR (XBRL companyfacts).

Regras de seleção de dados (validadas contra dados reais da SEC):

* Um 10-K traz também o ano anterior (comparativos) e os trimestres; por isso
  os fatos são escolhidos pelo ``end`` mais recente (nunca pelo ``filed``) e,
  em caso de empate de ``end`` (reapresentações), pelo ``filed`` mais recente.
* **Receita**: se várias tags empatam no ``end`` mais recente, vale o MAIOR valor
  (a tag ASC 606 é só um subconjunto de ``Revenues``).
* Balanço (instantâneos) só vale a até 7 dias do fim do ano fiscal; senão None.
* Âncora de cada bloco = lucro líquido; demais métricas precisam casar (±3 dias).
* 4º trimestre = anual - acumulado de 9 meses quando não há 10-Q do Q4.
* **Tag mais fresca vence**: quando uma métrica tem várias tags candidatas
  (ex.: ``Revenues`` antiga e ``RevenueFromContractWithCustomer...`` pós-ASC 606),
  escolhe-se a tag cujo fato selecionado tem o ``end`` mais recente; empate
  -> ordem do contrato. Isso evita usar uma tag abandonada há anos.
* Num bloco (anual/trimestral) só entram métricas do mesmo período de
  referência; métricas defasadas viram ``None`` com observação.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
TIMEOUT = 20.0
SOURCE = "SEC EDGAR (XBRL companyfacts)"

ANNUAL_FORMS = frozenset({"10-K", "10-K/A"})
QUARTER_FORMS = frozenset({"10-Q", "10-Q/A", "10-K", "10-K/A"})
INSTANT_FORMS = QUARTER_FORMS
FOREIGN_FORMS = frozenset({"20-F", "20-F/A", "40-F", "40-F/A"})

_USD = "USD"
# métrica -> (taxonomia, tags candidatas em ordem de contrato, unidade)
METRICS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "receita": ("us-gaap", (
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense"), _USD),
    "lucro_liquido": ("us-gaap", ("NetIncomeLoss",), _USD),
    "lpa_diluido": ("us-gaap", ("EarningsPerShareDiluted",), "USD/shares"),
    "fluxo_caixa_operacional": ("us-gaap", ("NetCashProvidedByUsedInOperatingActivities",), _USD),
    "ativos": ("us-gaap", ("Assets",), _USD),
    "passivos": ("us-gaap", ("Liabilities",), _USD),
    "patrimonio_liquido": ("us-gaap", (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), _USD),
    "caixa": ("us-gaap", ("CashAndCashEquivalentsAtCarryingValue",), _USD),
    "divida_longo_prazo": ("us-gaap", ("LongTermDebtNoncurrent", "LongTermDebt"), _USD),
}
DURATION_ANNUAL = ("receita", "lucro_liquido", "lpa_diluido", "fluxo_caixa_operacional")
DURATION_QUARTER = ("receita", "lucro_liquido", "lpa_diluido")
INSTANT_ANNUAL = ("ativos", "passivos", "patrimonio_liquido", "caixa", "divida_longo_prazo")

Fact = dict[str, Any]


class SecEdgarError(Exception):
    """Erro esperado ao consultar a SEC EDGAR (mensagem em português)."""


def _days(fact: Fact) -> int | None:
    try:
        return (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days
    except (KeyError, ValueError, TypeError):
        return None


def _gap(a: str | None, b: str | None) -> int | None:
    """Distância absoluta em dias entre duas datas ISO (None se inválidas)."""
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _near(a: str | None, b: str | None, tol: int) -> bool:
    g = _gap(a, b)
    return g is not None and g <= tol


def _key(f: Fact) -> tuple[str, str]:
    return (str(f.get("end") or ""), str(f.get("filed") or ""))


def _latest(facts: list[Fact]) -> Fact | None:
    """Fato com ``end`` mais recente; empate -> ``filed`` mais recente."""
    if not facts:
        return None
    return max(facts, key=_key)


def _valid(f: Any) -> bool:
    return (isinstance(f, dict) and isinstance(f.get("end"), str) and isinstance(f.get("form"), str)
            and isinstance(f.get("val"), (int, float)) and not isinstance(f.get("val"), bool)
            and f["val"] == f["val"])


def _duration_facts(facts: list[Fact], forms: frozenset[str], lo: int, hi: int) -> list[Fact]:
    out = []
    for f in facts:
        d = _days(f)
        if f.get("form") in forms and d is not None and lo <= d <= hi:
            out.append(f)
    return out


def _instant_facts(facts: list[Fact]) -> list[Fact]:
    return [f for f in facts if f.get("form") in INSTANT_FORMS and "start" not in f]


def _unit_facts(node: Any, unit: str | None = None) -> list[Fact]:
    """Fatos válidos (dict com ``end`` e ``val`` numérico) de um nó de tag; formas inesperadas são ignoradas."""
    if not isinstance(node, dict):
        return []
    units = node.get("units")
    if not isinstance(units, dict):
        return []
    lists = [units.get(unit)] if unit is not None else list(units.values())
    out: list[Fact] = []
    for fs in lists:
        if isinstance(fs, list):
            out.extend(f for f in fs if _valid(f))
    return out


def _all_facts(facts: dict[str, Any]) -> list[Fact]:
    out: list[Fact] = []
    for tax in facts.values():
        if not isinstance(tax, dict):
            continue
        for item in tax.values():
            out.extend(_unit_facts(item))
    return out


class SecEdgarClient:
    """Cliente somente leitura da SEC EDGAR com cache do mapa ticker -> CIK."""

    def __init__(self, user_agent: str | None, http_client: httpx.Client | None = None) -> None:
        self._user_agent = (user_agent or "").strip()
        self._http = http_client
        self._tickers: dict[str, tuple[int, str]] | None = None

    # ------------------------------------------------------------------ HTTP
    def _get_json(self, url: str) -> Any:
        if not self._user_agent:
            raise SecEdgarError(
                "User-Agent da SEC não configurado. Defina SEC_USER_AGENT=\"Seu Nome seu@email.com\" "
                "no arquivo .env (a SEC exige identificação nas requisições)."
            )
        headers = {"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"}
        if self._http is None:
            self._http = httpx.Client(timeout=TIMEOUT, follow_redirects=True)
        try:
            resp = self._http.get(url, headers=headers, timeout=TIMEOUT)
        except httpx.TimeoutException as exc:
            raise SecEdgarError("Tempo esgotado ao consultar a SEC EDGAR. Tente novamente em instantes.") from exc
        except httpx.HTTPError as exc:
            raise SecEdgarError(f"Falha de rede ao consultar a SEC EDGAR: {exc}") from exc
        if resp.status_code == 403:
            raise SecEdgarError(
                "A SEC recusou a requisição (HTTP 403): provavelmente o SEC_USER_AGENT é inválido. "
                "Use o formato \"Seu Nome seu@email.com\"."
            )
        if resp.status_code == 404:
            raise SecEdgarError("A SEC não tem dados XBRL para esta empresa (HTTP 404).")
        if resp.status_code != 200:
            raise SecEdgarError(f"A SEC EDGAR respondeu com erro HTTP {resp.status_code}.")
        try:
            return resp.json()
        except ValueError as exc:
            raise SecEdgarError("Resposta inválida da SEC EDGAR (JSON malformado).") from exc

    # --------------------------------------------------------------- tickers
    def _load_tickers(self) -> dict[str, tuple[int, str]]:
        if self._tickers is None:
            raw = self._get_json(TICKERS_URL)
            if isinstance(raw, dict):
                rows = raw.values()
            elif isinstance(raw, list):
                rows = raw
            else:
                raise SecEdgarError("Resposta inválida da SEC EDGAR (mapa de tickers em formato inesperado).")
            mapping: dict[str, tuple[int, str]] = {}
            for row in rows:
                try:
                    mapping.setdefault(str(row["ticker"]).upper(), (int(row["cik_str"]), str(row["title"])))
                except (KeyError, TypeError, ValueError):
                    continue
            self._tickers = mapping
            logger.info("Mapa ticker->CIK carregado: %d tickers", len(mapping))
        return self._tickers

    @staticmethod
    def normalize_ticker(ticker: str) -> str:
        """Maiúsculas, sem espaços, "." -> "-" (BRK.B -> BRK-B)."""
        return ticker.strip().upper().replace(".", "-")

    def ticker_to_cik(self, ticker: str) -> tuple[int, str]:
        """Retorna ``(cik, nome da empresa)``; levanta SecEdgarError se não achar."""
        tickers = self._load_tickers()
        norm = self.normalize_ticker(ticker)
        if norm not in tickers:
            raise SecEdgarError(
                f"Ticker '{norm}' não encontrado na SEC. Pode ser ETF, ADR/empresa estrangeira "
                "ou ticker incorreto (use o ticker da bolsa dos EUA, ex.: AAPL, BRK-B)."
            )
        return tickers[norm]

    # ------------------------------------------------------------ fundamentos
    @staticmethod
    def _candidates(facts: dict[str, Any], metric: str) -> list[tuple[str, list[Fact]]]:
        taxonomy, tags, unit = METRICS[metric]
        node = facts.get(taxonomy)
        node = node if isinstance(node, dict) else {}
        return [(tag, _unit_facts(node.get(tag), unit)) for tag in tags]

    @staticmethod
    def _select(cands: list[tuple[str, list[Fact]]], metric: str, align: str | None = None,
                tol: int = 3) -> Fact | None:
        """Tag mais fresca vence (empate -> ordem do contrato). Para ``receita`` o empate de
        ``end`` entre tags resolve-se pelo MAIOR valor (ASC 606 é só um subconjunto de Revenues).
        Com ``align``, só valem fatos cujo ``end`` esteja a ``tol`` dias dele."""
        picked: list[Fact] = []
        for tag, fs in cands:
            if align is not None:
                fs = [f for f in fs if _near(f["end"], align, tol)]
            c = _latest(fs)
            if c is not None:
                picked.append({**c, "_tag": tag})
        if not picked:
            return None
        top = max(f["end"] for f in picked)
        tied = [f for f in picked if f["end"] == top]
        if metric == "receita":
            return max(tied, key=lambda f: f["val"])
        return tied[0]

    @staticmethod
    def _pick_instant(facts: dict[str, Any], metric: str, aend: str | None) -> tuple[Fact | None, Fact | None]:
        """Retorna (alinhado, mais recente). Alinhado = ``end`` a até 7 dias do fim do ano fiscal
        (primeira tag em ordem de contrato)."""
        cands = [_instant_facts(fs) for _, fs in SecEdgarClient._candidates(facts, metric)]
        newest = _latest([f for inst in cands for f in inst])
        if aend:
            for inst in cands:
                hit = _latest([f for f in inst if _near(f["end"], aend, 7)])
                if hit is not None:
                    return hit, newest
        return None, newest

    def _duration_block(self, facts: dict[str, Any], metrics: tuple[str, ...], forms: frozenset[str],
                        lo: int, hi: int, label: str, obs: list[str]
                        ) -> tuple[dict[str, Any], Fact | None, dict[str, Fact | None]]:
        """Âncora = lucro líquido mais recente (senão o ``end`` mais recente entre as métricas);
        as demais métricas precisam casar com o ``end`` da âncora (±3 dias) ou viram None."""
        cands = {m: [(t, _duration_facts(fs, forms, lo, hi)) for t, fs in self._candidates(facts, m)]
                 for m in metrics}
        latest = {m: self._select(cands[m], m) for m in metrics}
        found = {m: f for m, f in latest.items() if f}
        if latest.get("lucro_liquido"):
            anchor = latest["lucro_liquido"]
        elif found:
            anchor = max(found.values(), key=_key)
        else:
            anchor = None
        block: dict[str, Any] = {}
        picks: dict[str, Fact | None] = {}
        for m in metrics:
            f = self._select(cands[m], m, align=anchor["end"]) if anchor else None
            picks[m] = f
            if f is not None:
                block[m] = f["val"]
                continue
            block[m] = None
            if latest[m] is None:
                obs.append(f"{label}: métrica '{m}' não encontrada.")
            else:
                obs.append(f"{label}: '{m}' desatualizada (último dado em {latest[m]['end']}, "
                           f"período de referência {anchor['end'] if anchor else None}).")
        return block, anchor, picks

    def _ytd(self, facts: dict[str, Any], metric: str, tag: str, astart: str, qend: str) -> Fact | None:
        taxonomy, _, unit = METRICS[metric]
        node = facts.get(taxonomy)
        node = node if isinstance(node, dict) else {}
        fs = [f for f in _duration_facts(_unit_facts(node.get(tag), unit), QUARTER_FORMS, 250, 300)
              if _near(f.get("start"), astart, 7) and _near(f["end"], qend, 3)]
        return _latest(fs)

    def fundamentals(self, ticker: str) -> dict:
        """Fundamentos anuais/trimestrais mais recentes (ver docstring do módulo)."""
        cik, name = self.ticker_to_cik(ticker)
        data = self._get_json(FACTS_URL.format(cik=cik))
        if not isinstance(data, dict) or not isinstance(data.get("facts"), dict):
            raise SecEdgarError("Resposta inválida da SEC EDGAR: campo 'facts' ausente ou em formato inesperado.")
        facts = data["facts"]
        obs: list[str] = []

        # --- anual
        dur, anchor, apicks = self._duration_block(facts, DURATION_ANNUAL, ANNUAL_FORMS, 350, 380, "Anual", obs)
        aend = anchor["end"] if anchor else None
        annual: dict[str, Any] = {
            "fim_periodo": aend,
            "ano_fiscal": anchor.get("fy") if anchor else None,
            "formulario": anchor.get("form") if anchor else None,
            "arquivado_em": anchor.get("filed") if anchor else None,
            **dur,
        }
        for m in INSTANT_ANNUAL:
            aligned, newest = self._pick_instant(facts, m, aend)
            if aligned is not None:
                annual[m] = aligned["val"]
                continue
            annual[m] = None
            if newest is None:
                obs.append(f"Anual: métrica '{m}' não encontrada.")
            else:
                obs.append(f"Anual: '{m}' sem dado no fim do ano fiscal ({aend}); "
                           f"último dado em {newest['end']} (descartado).")
        rev, ni, eq = annual["receita"], annual["lucro_liquido"], annual["patrimonio_liquido"]
        annual["margem_liquida_pct"] = round(ni / rev * 100, 2) if rev is not None and ni is not None and rev > 0 else None
        annual["roe_pct"] = round(ni / eq * 100, 2) if ni is not None and eq is not None and eq > 0 else None

        # --- trimestral
        qdur, qanchor, _ = self._duration_block(facts, DURATION_QUARTER, QUARTER_FORMS, 80, 120, "Trimestral", obs)
        quarter: dict[str, Any] = {
            "fim_periodo": qanchor["end"] if qanchor else None,
            "formulario": qanchor.get("form") if qanchor else None,
            "arquivado_em": qanchor.get("filed") if qanchor else None,
            **qdur,
            "derivado": False,
        }
        qend = quarter["fim_periodo"]
        ani = apicks.get("lucro_liquido")
        if aend and qend and ani is not None and aend > qend and (_gap(aend, qend) or 0) > 3:
            derived: dict[str, float | None] = {}
            for m in ("receita", "lucro_liquido"):
                af = apicks.get(m)
                y = self._ytd(facts, m, af["_tag"], af["start"], qend) if af else None
                derived[m] = af["val"] - y["val"] if af and y else None
            # Receita do Q4 negativa ou maior que a anual indica reapresentação (ex.: operações descontinuadas).
            arev = apicks.get("receita")
            if derived["receita"] is not None and arev and not 0 <= derived["receita"] <= arev["val"]:
                obs.append(f"Trimestral: receita do 4º trimestre derivada inconsistente ({derived['receita']:.0f}); "
                           "provável reapresentação entre o 10-Q e o 10-K, valor descartado.")
                derived["receita"] = None
            if derived["lucro_liquido"] is not None:
                quarter.update({
                    "fim_periodo": aend,
                    "formulario": "10-K (derivado: anual - 9 meses)",
                    "arquivado_em": anchor.get("filed"),
                    "receita": derived["receita"],
                    "lucro_liquido": derived["lucro_liquido"],
                    "lpa_diluido": None,
                    "derivado": True,
                })
                obs.append("Trimestral: LPA do 4º trimestre não é derivável (número de ações varia).")
                if derived["receita"] is None:
                    obs.append("Trimestral: 'receita' do 4º trimestre não derivável (falta acumulado de 9 meses).")
            else:
                obs.append(f"Trimestral: 4º trimestre não derivável (falta acumulado de 9 meses); "
                           f"mantido o último trimestre real ({qend}).")

        # --- ações em circulação (dei)
        dei = facts.get("dei")
        shares_node = dei.get("EntityCommonStockSharesOutstanding") if isinstance(dei, dict) else None
        sh = _latest(_unit_facts(shares_node, "shares"))
        shares = {"valor": sh["val"], "data": sh["end"]} if sh else {"valor": None, "data": None}
        if sh is None:
            obs.append("Ações em circulação não encontradas.")
        else:
            try:
                if aend:
                    age, limit, ref = (date.fromisoformat(aend) - date.fromisoformat(sh["end"])).days, 400, aend
                else:
                    age, limit, ref = (date.today() - date.fromisoformat(sh["end"])).days, 548, "hoje"
            except ValueError:
                age, limit, ref = 10**6, 0, "?"
            if age > limit:
                shares = {"valor": None, "data": None}
                obs.append(f"Ações em circulação desatualizadas (último dado em {sh['end']}, "
                           f"referência {ref}); valor descartado.")

        # --- empresa estrangeira
        if anchor is None and not any(f.get("form") in ANNUAL_FORMS for f in _all_facts(facts)):
            if "ifrs-full" in facts or any(f.get("form") in FOREIGN_FORMS for f in _all_facts(facts)):
                obs.append(
                    "Empresa estrangeira (ADR): usa 20-F/40-F e IFRS, sem 10-K. "
                    "Não suportada nesta versão."
                )
            else:
                obs.append("Nenhum formulário 10-K encontrado para esta empresa.")

        return {
            "ticker": self.normalize_ticker(ticker), "empresa": name, "cik": cik,
            "anual": annual, "trimestral": quarter, "acoes_em_circulacao": shares,
            "fonte": SOURCE, "observacoes": obs,
        }
