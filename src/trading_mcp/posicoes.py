"""Relatório de posições abertas e ordens pendentes (somente leitura).

Só medições sobre o stop e o alvo atuais: distâncias, resultado se forem atingidos, duração e
exposição por símbolo. Nenhuma regra ou limite de risco é aplicado aqui.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from trading_mcp import risk, tempo
from trading_mcp.mt5_client import MT5Client, MT5Error

OBSERVACOES = [
    "Valores em dinheiro na moeda da conta. Resultados consideram só o preço: o swap acumulado aparece à "
    "parte e a comissão não está incluída (fica nos negócios do histórico).",
    "Stop e alvo são medidos como estão agora; se o stop foi movido, o risco inicial da operação (para "
    "resultado em R) não aparece aqui.",
    "O stop vira ordem a mercado: em gaps e notícias a execução pode sair pior que o preço do stop, "
    "inclusive num stop com lucro protegido.",
    "Totais somam posição por posição, sem compensar posições opostas no mesmo símbolo.",
    "`pontos` são pontos do MT5 (a menor variação de preço do símbolo), não pontos do índice; nos índices, "
    "a distância em pontos do índice é o campo `preco`.",
]


def _round_money(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _pct(value: float | None, balance: float) -> float | None:
    return None if value is None or balance <= 0 else round(value / balance * 100, 2)


def _lots(value: float) -> float:
    return round(value, 8)  # tira ruído de float (0.1 + 0.2)


class _Report:
    """Monta o relatório a partir do cliente MT5, com cache de especificação e cotação por símbolo."""

    def __init__(self, mt5: MT5Client, balance: float) -> None:
        self.mt5 = mt5
        self.balance = balance
        self.warnings: list[str] = []
        self._specs: dict[str, dict] = {}
        self._quotes: dict[str, dict] = {}
        self._profit_failed: set[str] = set()

    def spec(self, symbol: str) -> dict:
        if symbol not in self._specs:
            self._specs[symbol] = self.mt5.symbol_spec(symbol)
        return self._specs[symbol]

    def quote_state(self, symbol: str) -> dict:
        """Estado da cotação do símbolo; os preços em si vêm da posição/ordem (mesmo instante do MT5)."""
        if symbol not in self._quotes:
            try:
                q = self.mt5.quote(symbol)
            except MT5Error as exc:
                state = {"estado": "sem_cotacao", "aviso": str(exc)}
            else:
                state = {"estado": q["estado"], "idade_s": q["idade_s"]}
                if q.get("aviso"):
                    state["aviso"] = q["aviso"]
            self._quotes[symbol] = state
        return self._quotes[symbol]

    def stale_symbols(self) -> list[str]:
        return sorted(s for s, q in self._quotes.items() if q["estado"] != "atual")

    def money(self, symbol: str, side: str, volume: float, price_open: float, price_close: float) -> float | None:
        try:
            return self.mt5.profit(symbol, side, volume, price_open, price_close)
        except MT5Error as exc:
            if symbol not in self._profit_failed:
                self._profit_failed.add(symbol)
                self.warnings.append(f"Não foi possível calcular valores em dinheiro para {symbol}: {exc}")
            return None

    def distance(self, symbol: str, value: float) -> dict[str, Any]:
        s = self.spec(symbol)
        digits = s["digitos"]
        out: dict[str, Any] = {"preco": round(value, digits), "pontos": round(value / s["ponto"])}
        if s["is_forex"]:
            out["pips"] = round(value / risk.pip_size(s["ponto"], digits), 1)
        return out

    def price(self, symbol: str, value: float) -> float:
        return round(value, self.spec(symbol)["digitos"])


def _stop_situation(side: str, entry: float, stop: float | None, point: float) -> str:
    """sem_stop, com_risco, no_preco_de_entrada ou lucro_protegido (stop além da entrada)."""
    if stop is None:
        return "sem_stop"
    beyond = (stop - entry) if side == "buy" else (entry - stop)
    if abs(beyond) < point / 2:
        return "no_preco_de_entrada"
    return "lucro_protegido" if beyond > 0 else "com_risco"


def _room(side: str, current: float, level: float, *, is_stop: bool) -> float:
    """Distância com sinal do preço atual até o nível: positiva = ainda não atingido."""
    toward_up = (side == "buy") != is_stop  # alvo da compra e stop da venda ficam acima
    return (level - current) if toward_up else (current - level)


def _target_stop_ratio(side: str, entry: float, stop: float | None, target: float | None, situation: str) -> float | None:
    if situation != "com_risco" or target is None:
        return None
    reward = (target - entry) if side == "buy" else (entry - target)
    if reward <= 0:
        return None
    return round(reward / abs(entry - stop), 2)  # type: ignore[operator]


def _position(r: _Report, p: dict, now: datetime) -> dict[str, Any]:
    sym, side, vol = p["simbolo"], p["lado"], p["volume"]
    entry, current, sl, tp = p["preco_abertura"], p["preco_atual"], p["stop_loss"], p["take_profit"]
    situation = _stop_situation(side, entry, sl, r.spec(sym)["ponto"])
    stop: dict[str, Any] = {"preco": None if sl is None else r.price(sym, sl), "situacao": situation}
    if sl is not None:
        at_stop = r.money(sym, side, vol, entry, sl)
        room = _room(side, current, sl, is_stop=True)
        stop.update(
            {
                "distancia_do_preco_atual": r.distance(sym, room),
                "distancia_da_entrada": r.distance(sym, abs(entry - sl)),
                "resultado_se_atingido": _round_money(at_stop),
                "resultado_se_atingido_pct_saldo": _pct(at_stop, r.balance),
                "variacao_desde_agora": None if room < 0 else _round_money(r.money(sym, side, vol, current, sl)),
            }
        )
        if room < 0:
            stop["ultrapassado"] = True
            r.warnings.append(
                f"Posição {p['ticket']} ({sym}): o preço atual já passou do stop (gap, stop ainda em processamento "
                "ou cotação parada). Executado a mercado, o resultado tende a ser pior que `resultado_se_atingido`."
            )
    target: dict[str, Any] = {"preco": None if tp is None else r.price(sym, tp)}
    if tp is not None:
        room = _room(side, current, tp, is_stop=False)
        target.update(
            {
                "distancia_do_preco_atual": r.distance(sym, room),
                "resultado_se_atingido": _round_money(r.money(sym, side, vol, entry, tp)),
                "variacao_desde_agora": None if room < 0 else _round_money(r.money(sym, side, vol, current, tp)),
            }
        )
        if room < 0:
            target["ultrapassado"] = True
    age = max((now - p["abertura"]).total_seconds(), 0.0)
    return {
        "ticket": p["ticket"],
        "simbolo": sym,
        "direcao": "compra" if side == "buy" else "venda",
        "volume": vol,
        "preco_abertura": r.price(sym, entry),
        "preco_atual": r.price(sym, current),
        "cotacao": r.quote_state(sym),
        "lucro_aberto": _round_money(p["lucro"]),
        "lucro_aberto_pct_saldo": _pct(p["lucro"], r.balance),
        "swap": _round_money(p["swap"]),
        "abertura": tempo.exibicao(p["abertura"]),
        "aberta_ha": tempo.describe_age(age),
        "stop": stop,
        "alvo": target,
        "relacao_alvo_stop": _target_stop_ratio(side, entry, sl, tp, situation),
    }


def _order(r: _Report, o: dict) -> dict[str, Any]:
    sym, side, vol, current = o["simbolo"], o["lado"], o["volume_atual"], o["preco_atual"]
    out: dict[str, Any] = {
        "ticket": o["ticket"],
        "simbolo": sym,
        "tipo": o["tipo"],
        "volume": vol,
    }
    if o["volume_inicial"] != vol:
        out["volume_inicial"] = o["volume_inicial"]
    # Na stop limitada, a entrada é o preço da limitada colocada quando o stop é atingido.
    entry = o["preco_limite"] or o["preco"]
    if side is None or not entry:
        return {**out, "colocada": tempo.exibicao(o["colocada"])}
    out.update(
        {
            "preco_ativacao": r.price(sym, o["preco"]),
            "preco_limite": None if o["preco_limite"] is None else r.price(sym, o["preco_limite"]),
            "preco_atual": None if current is None else r.price(sym, current),
            "distancia_ate_ativacao": None if current is None else r.distance(sym, abs(o["preco"] - current)),
            "cotacao": r.quote_state(sym),
        }
    )
    sl, tp = o["stop_loss"], o["take_profit"]
    situation = _stop_situation(side, entry, sl, r.spec(sym)["ponto"])
    stop: dict[str, Any] = {"preco": None if sl is None else r.price(sym, sl), "situacao": situation}
    if sl is not None:
        at_stop = r.money(sym, side, vol, entry, sl)
        stop.update(
            {
                "distancia_da_entrada": r.distance(sym, abs(entry - sl)),
                "resultado_se_executada_e_atingido": _round_money(at_stop),
                "resultado_se_executada_e_atingido_pct_saldo": _pct(at_stop, r.balance),
            }
        )
    target: dict[str, Any] = {"preco": None if tp is None else r.price(sym, tp)}
    if tp is not None:
        target["resultado_se_executada_e_atingido"] = _round_money(r.money(sym, side, vol, entry, tp))
    out.update(
        {
            "stop": stop,
            "alvo": target,
            "relacao_alvo_stop": _target_stop_ratio(side, entry, sl, tp, situation),
            "colocada": tempo.exibicao(o["colocada"]),
            "validade": o["validade"],
            "expira": None if o["expira"] is None else tempo.exibicao(o["expira"]),
        }
    )
    return out


def _safe(r: _Report, build_item: Callable[..., dict], item: dict, *args: Any) -> dict[str, Any]:
    """Monta um item; uma falha num símbolo vira um item com erro, sem derrubar o relatório."""
    try:
        return build_item(r, item, *args)
    except MT5Error as exc:
        r.warnings.append(f"Item {item['ticket']} ({item['simbolo']}) sem detalhes: {exc}")
        return {"ticket": item["ticket"], "simbolo": item["simbolo"], "erro": str(exc)}


def _risk_sum(items: list[dict], key: str) -> tuple[float | None, float | None, list[Any]]:
    """Perda somada nos stops com risco e lucro somado nos stops protegidos (None se faltou valor).

    Classifica pela posição do stop, não pelo sinal do valor, e devolve os tickets em que o sinal
    contradiz a posição do stop (conferência da convenção de sinal de order_calc_profit).
    """
    loss = protected = 0.0
    missing = False
    mismatched: list[Any] = []
    for item in items:
        if "erro" in item:
            missing = True
            continue
        stop = item.get("stop")
        if not stop or stop["situacao"] in ("sem_stop", "no_preco_de_entrada"):
            continue
        value = stop.get(key)
        if value is None:
            missing = True
            continue
        at_risk = stop["situacao"] == "com_risco"
        if (value > 0) if at_risk else (value < 0):
            mismatched.append(item["ticket"])
        if at_risk:
            loss += abs(value)
        else:
            protected += abs(value)
    if missing:
        return None, None, mismatched
    return round(loss, 2), round(protected, 2), mismatched


def _by_symbol(positions: list[dict]) -> list[dict]:
    groups: dict[str, dict[str, Any]] = {}
    for p in positions:
        if "erro" in p:
            continue
        g = groups.setdefault(
            p["simbolo"],
            {"simbolo": p["simbolo"], "posicoes": 0, "lotes_compra": 0.0, "lotes_venda": 0.0, "lucro_aberto": 0.0},
        )
        g["posicoes"] += 1
        g["lotes_compra" if p["direcao"] == "compra" else "lotes_venda"] += p["volume"]
        g["lucro_aberto"] += p["lucro_aberto"] or 0.0
    out = []
    for g in groups.values():
        g["lotes_compra"], g["lotes_venda"] = _lots(g["lotes_compra"]), _lots(g["lotes_venda"])
        g["lotes_liquidos"] = _lots(g["lotes_compra"] - g["lotes_venda"])
        g["lucro_aberto"] = round(g["lucro_aberto"], 2)
        out.append(g)
    return sorted(out, key=lambda g: g["simbolo"])


def build(
    mt5: MT5Client,
    symbol: str = "",
    include_pending: bool = True,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Relatório de posições (e ordens pendentes) da conta conectada."""
    account = mt5.account()
    wanted = mt5.resolve_symbol(symbol) if symbol.strip() else None

    def keep(item: dict) -> bool:
        return wanted is None or item["simbolo"].upper() == wanted.upper()

    balance = float(account["saldo"])
    r = _Report(mt5, balance)
    # Ordens antes das posições: uma ordem executada entre as duas leituras aparece nas duas (e é
    # descartada abaixo) em vez de sumir de ambas.
    orders: list[dict] | None = None
    if include_pending:
        try:
            orders = [o for o in mt5.pending_orders() if keep(o)]
        except MT5Error as exc:
            r.warnings.append(f"Ordens pendentes não lidas: {exc}")
    positions = [p for p in mt5.open_positions() if keep(p)]
    if orders is not None:
        opened = {p["identificador"] for p in positions}
        orders = [o for o in orders if o["ticket"] not in opened]
    moment = (now or mt5.now_utc)()

    pos_out = [_safe(r, _position, p, moment) for p in positions]
    ord_out = [_safe(r, _order, o) for o in orders or []]

    loss, protected, mismatched = _risk_sum(pos_out, "resultado_se_atingido")
    ok = [p for p in pos_out if "erro" not in p]
    without_stop = sum(1 for p in ok if p["stop"]["situacao"] == "sem_stop")
    changes = [p["stop"]["variacao_desde_agora"] for p in ok if p["stop"]["situacao"] != "sem_stop"]
    complete = changes and None not in changes and len(ok) == len(pos_out)
    totals: dict[str, Any] = {
        "posicoes": len(pos_out),
        "lucro_aberto": round(sum(p["lucro_aberto"] or 0.0 for p in ok), 2),
        "swap": round(sum(p["swap"] or 0.0 for p in ok), 2),
        "perda_nos_stops": loss,
        "perda_nos_stops_pct_saldo": _pct(loss, balance),
        "lucro_protegido_nos_stops": protected,
        "variacao_desde_agora_se_todos_os_stops": round(sum(changes), 2) if complete else None,  # type: ignore[arg-type]
        "posicoes_sem_stop": without_stop,
    }
    result: dict[str, Any] = {
        "conta": {
            "login": account["login"],
            "tipo_conta": account["tipo_conta"],
            "moeda": account["moeda"],
            "saldo": account["saldo"],
            "equity": account["equity"],
            "margem_livre": account["margem_livre"],
        },
        "coletado": tempo.exibicao(moment),
        "posicoes": pos_out,
        "totais": totals,
        "por_simbolo": _by_symbol(pos_out),
    }
    if wanted:
        result["filtro_simbolo"] = wanted

    incomplete = loss is None
    if include_pending:
        result["pendentes"] = ord_out if orders is not None else None
        if orders is not None:
            o_loss, _, o_mismatched = _risk_sum(ord_out, "resultado_se_executada_e_atingido")
            mismatched += o_mismatched
            incomplete = incomplete or o_loss is None
            result["totais_pendentes"] = {
                "ordens": len(ord_out),
                "perda_nos_stops_se_executadas": o_loss,
                "perda_nos_stops_se_executadas_pct_saldo": _pct(o_loss, balance),
                "ordens_sem_stop": sum(1 for o in ord_out if o.get("stop", {}).get("situacao") == "sem_stop"),
            }

    warnings = list(r.warnings)
    if not account["is_demo"]:
        warnings.insert(0, f"ATENÇÃO: a conta conectada NÃO é demo (tipo: {account['tipo_conta']}).")
    if not account["conectado"]:
        warnings.append(
            "Terminal sem conexão com a corretora: as listas de posições e ordens e os preços podem estar "
            "desatualizados (stops atingidos ou ordens executadas no servidor podem não aparecer)."
        )
    stale = r.stale_symbols()
    if stale:
        warnings.append(
            f"Cotação não atual em {', '.join(stale)}: preço atual, lucro aberto e distâncias desses símbolos "
            "não são de agora (veja `cotacao` em cada item)."
        )
    if without_stop:
        warnings.append(
            f"{without_stop} posição(ões) sem stop: a perda delas não é limitada pelo stop e não entra em "
            "`perda_nos_stops`."
        )
    if incomplete:
        warnings.append("Faltou algum valor em dinheiro: os totais de perda e lucro nos stops ficam null.")
    if mismatched:
        warnings.append(
            f"O sinal do resultado no stop contradiz a posição do stop em {', '.join(map(str, mismatched))}: "
            "confira os valores no terminal antes de usá-los."
        )
    if warnings:
        result["avisos"] = warnings
    result["observacoes"] = OBSERVACOES
    return result
