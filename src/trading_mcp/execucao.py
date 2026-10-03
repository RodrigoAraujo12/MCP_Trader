"""Execução de propostas na conta DEMO (etapa F), só com a aprovação do usuário numa janela do Windows.

Ordem das travas, todas conferidas de novo a cada chamada:

1. uma execução por vez; execução habilitada no .env (``EXECUCAO_HABILITADA=sim``) e sem o arquivo
   ``PARAR_EXECUCOES`` na pasta do banco das propostas;
2. proposta íntegra (assinatura HMAC confere), válida, a pelo menos 15 s de expirar e sem tentativa anterior;
3. a mesma conta da proposta, demo, conectada, com o Algo Trading ligado e a negociação pela API Python liberada;
4. cotação atual, o preço de agora ainda dá o mesmo tipo de ordem e o risco com o lote assinado (a mercado, com o
   desvio máximo de preço) cabe nos limites de agora;
5. janela com os dados exatos: só o clique em "Enviar ordem", dentro do prazo, libera (o servidor espera o clique);
6. depois do clique, os passos 1 a 4 de novo (o preço andou durante a espera) e a validação do servidor (order_check);
   imediatamente antes do envio, o arquivo de parada e a validade mais uma vez, e o próprio cliente do MT5 confere de
   novo a execução habilitada, o arquivo de parada, a conta da proposta e a conta demo. O resultado é gravado (antes
   do envio fica "enviando", com a requisição) e conferido no terminal.

Cada proposta tem uma única tentativa: recusada, sem resposta ou com falha, faça uma nova proposta.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trading_mcp import limites, tempo
from trading_mcp.aprovacao import APPROVED
from trading_mcp.config import KILL_FILE_NAME
from trading_mcp.limites import LimitesError, ProposalStore, RiskRules
from trading_mcp.mt5_client import MT5Client, MT5Error

logger = logging.getLogger(__name__)

APPROVAL_TIMEOUT_S = 45.0  # o Claude Desktop desiste de uma tool em ~60 s
MIN_TIME_LEFT = timedelta(seconds=15)
MAGIC = 20261002
KILL_FILE = KILL_FILE_NAME
RETCODE_PLACED, RETCODE_DONE, RETCODE_DONE_PARTIAL, RETCODE_INVALID_FILL = 10008, 10009, 10010, 10030
_PENDING = {"compra_limitada": "ORDER_TYPE_BUY_LIMIT", "compra_stop": "ORDER_TYPE_BUY_STOP",
            "venda_limitada": "ORDER_TYPE_SELL_LIMIT", "venda_stop": "ORDER_TYPE_SELL_STOP"}
_HUMAN = {"a_mercado": "a mercado", "compra_limitada": "limitada", "compra_stop": "stop", "venda_limitada": "limitada",
          "venda_stop": "stop"}
SYMBOL_EXPIRATION_SPECIFIED = 4
# Uma execução por vez: duas janelas ao mesmo tempo poderiam, juntas, passar dos limites.
_RUNNING = threading.Lock()

Approver = Callable[[str, str, float], str]


class ExecucaoError(Exception):
    """Execução recusada antes de qualquer envio (nada foi enviado)."""


def kill_file(store: ProposalStore) -> Path:
    return store._path.parent / KILL_FILE


def stop_all(store: ProposalStore, reason: str = "", now: datetime | None = None) -> dict[str, Any]:
    """Para novas execuções criando o arquivo PARAR_EXECUCOES (ordens e posições existentes não são mexidas)."""
    path = kill_file(store)
    moment = now or datetime.now(timezone.utc)
    path.parent.mkdir(parents=True, exist_ok=True)
    already = path.exists()
    if not already:
        path.write_text(f"Parado em {tempo.iso_utc(moment)}. {reason}".strip() + "\n", encoding="utf-8")
    return {
        "parado": True,
        "ja_estava_parado": already,
        "arquivo": str(path),
        "como_retomar": f"Apague o arquivo {path} (só você, fora do chat).",
        "observacao": "Ordens pendentes e posições abertas continuam no MT5: feche ou cancele lá se quiser.",
    }


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _to_tick(price: float, tick: float, digits: int) -> float:
    return round(round(price / tick) * tick, digits) if tick > 0 else round(price, digits)


def _plan(mt5: MT5Client, rules: RiskRules, row: dict[str, Any], moment: datetime) -> dict[str, Any]:
    """Requisição da ordem com o preço de agora; ExecucaoError se o cenário mudou desde a proposta."""
    symbol, direction, kind = row["simbolo"], row["direcao"], row["tipo_ordem"]
    entry, stop, target, volume = row["entrada"], row["stop"], row["alvo"], row["volume"]
    spec = mt5.symbol_spec(symbol)
    digits, tick, point = spec["digitos"], spec["tick_size"], spec["ponto"]
    quote = mt5.quote(symbol)
    if quote["estado"] != "atual":
        raise ExecucaoError(f"Cotação {quote['estado']}: só executo com o preço de agora.")
    now_kind = limites._order_type(direction, entry, quote)
    if now_kind != kind:
        raise ExecucaoError(f"O preço mudou: a proposta era {kind}, agora seria {now_kind}. Faça uma nova proposta.")
    market = kind == "a_mercado"
    buy = direction == "compra"
    fill = (quote["ask"] if buy else quote["bid"]) if market else entry
    if (fill <= stop) if buy else (fill >= stop):
        raise ExecucaoError(f"O preço de execução ({fill}) já passou do stop ({stop}).")
    if target is not None and ((fill >= target) if buy else (fill <= target)):
        raise ExecucaoError(f"O preço de execução ({fill}) já passou do alvo ({target}).")
    loss_per_lot, _ = mt5.loss_per_lot(symbol, fill, stop)
    risk = round(volume * loss_per_lot, 2)
    deviation = 0
    worst_risk = risk
    if market:
        deviation = limites.market_deviation(quote, point)
        worst_loss, _ = mt5.loss_per_lot(symbol, limites.worst_market_fill(direction, fill, quote, point), stop)
        worst_risk = round(volume * worst_loss, 2)
    limits = limites.account_limits(mt5, rules, moment)
    nxt = limits["proxima_operacao"]
    if not nxt["pode_operar"]:
        raise ExecucaoError(f"Limites não permitem nova operação agora: {nxt['motivo']}")
    if worst_risk > nxt["risco_maximo"] + limites.MIN_ROOM:
        extra = f" (com o desvio máximo de {deviation} pontos a mercado)" if market else ""
        raise ExecucaoError(f"Com o preço de agora o risco seria {worst_risk:.2f}{extra}, acima do permitido "
                            f"({nxt['risco_maximo']:.2f}). Faça uma nova proposta.")

    c = mt5.constant
    request: dict[str, Any] = {
        "symbol": symbol, "volume": float(volume), "sl": _to_tick(float(stop), tick, digits),
        "tp": _to_tick(float(target), tick, digits) if target is not None else 0.0,
        "magic": MAGIC, "comment": f"mcp {row['id']}",
    }
    warnings: list[str] = []
    expiry_note = ""
    if market:
        request.update(action=c("TRADE_ACTION_DEAL"), price=_to_tick(float(fill), tick, digits),
                       type=c("ORDER_TYPE_BUY") if buy else c("ORDER_TYPE_SELL"), deviation=deviation,
                       type_time=c("ORDER_TIME_GTC"))
    else:
        request.update(action=c("TRADE_ACTION_PENDING"), price=_to_tick(float(entry), tick, digits),
                       type=c(_PENDING[kind]))
        if spec["modos_expiracao"] & SYMBOL_EXPIRATION_SPECIFIED:
            # Epoch do horário da corretora: na Exness o servidor está em UTC (docs/diagnostico-mt5-2026-10-01.md).
            request.update(type_time=c("ORDER_TIME_SPECIFIED"), expiration=int(_parse(row["expira_utc"]).timestamp()))
            expires = _parse(row["expira_utc"]).astimezone(tempo.SAO_PAULO).strftime("%H:%M:%S")
            expiry_note = f"A ordem pendente expira às {expires} (Brasília), junto com a proposta."
        else:
            request["type_time"] = c("ORDER_TIME_GTC")
            expiry_note = "O símbolo não aceita expiração com data: a ordem pendente fica até ser cancelada no MT5."
            warnings.append(expiry_note)
    fillings = [c("ORDER_FILLING_FOK"), c("ORDER_FILLING_IOC"), c("ORDER_FILLING_RETURN")]
    bits = spec["modo_preenchimento"]
    allowed = [f for f, bit in zip(fillings, (1, 2, 0)) if bit == 0 or bits & bit]
    candidates = allowed if market else [fillings[2], *[f for f in allowed if f != fillings[2]]]
    return {"request": request, "fillings": candidates, "risk": risk, "worst_risk": worst_risk, "deviation": deviation,
            "fill": fill, "limits": nxt, "base": limits["dia"]["base_dos_limites"], "warnings": warnings,
            "expiry_note": expiry_note, "market": market}


def _summary(row: dict[str, Any], plan: dict[str, Any], account: dict[str, Any]) -> tuple[str, str]:
    expires = _parse(row["expira_utc"]).astimezone(tempo.SAO_PAULO).strftime("%H:%M:%S")
    currency = account["moeda"]
    pct = plan["risk"] / plan["base"] * 100 if plan["base"] else 0.0
    lines = [
        f"ORDEM NA CONTA DEMO {account['login']} ({account['servidor']})",
        "",
        f"Proposta {row['id']}",
        f"{row['direcao'].capitalize()} {_HUMAN[row['tipo_ordem']]} - {row['simbolo']}",
        f"Lote: {row['volume']}",
        f"Entrada: {row['entrada']}" + (f" (agora executaria em {plan['fill']})" if plan["market"] else ""),
        f"Stop: {row['stop']}",
        f"Alvo: {row['alvo'] if row['alvo'] is not None else 'sem alvo'}",
        f"Risco: {plan['risk']:.2f} {currency} ({pct:.2f}% da base do dia)",
    ]
    if plan["market"]:
        lines.append(f"A mercado o preço pode variar: risco de até {plan['worst_risk']:.2f} {currency} com "
                     f"{plan['deviation']} pontos de desvio; a corretora pode executar com escorregamento maior.")
    if plan["expiry_note"]:
        lines.append(plan["expiry_note"])
    try:
        news = json.loads(row.get("detalhes") or "{}").get("noticias") or []
    except ValueError:
        news = []
    if news:
        lines.append("Notícia de importância alta perto: "
                     + "; ".join(f"{n.get('evento')} às {str(n.get('utc', ''))[11:16]} UTC" for n in news[:3]))
    lines += [f"Proposta válida até {expires} (Brasília)", "", "Enviar esta ordem?"]
    return "Aprovar ordem na conta DEMO?", "\n".join(lines)


def _check_proposal(store: ProposalStore, proposal_id: str, moment: datetime, margin: timedelta) -> dict[str, Any]:
    if kill_file(store).exists():
        raise ExecucaoError(f"Execuções paradas (arquivo {kill_file(store)}). Para retomar, apague o arquivo.")
    row = store.get(proposal_id)
    if row is None:
        raise ExecucaoError(f"Proposta {proposal_id} não encontrada.")
    if not store.verify(proposal_id):
        raise ExecucaoError(f"A proposta {proposal_id} foi alterada depois de assinada: não executo.")
    if row["status"] != "valida":
        raise ExecucaoError(f"A proposta {proposal_id} foi recusada ao ser criada: não pode ser executada.")
    if moment > _parse(row["expira_utc"]) - margin:
        raise ExecucaoError(f"A proposta {proposal_id} expirou (ou está a segundos de expirar): faça uma nova.")
    return row


def _check_account(mt5: MT5Client, row: dict[str, Any]) -> dict[str, Any]:
    account = mt5.account()
    if (int(account["login"]), str(account["servidor"])) != (row["conta_login"], row["conta_servidor"]):
        raise ExecucaoError("O terminal está em outra conta, diferente da conta da proposta.")
    if not account["is_demo"] or not row["conta_demo"]:
        raise ExecucaoError("Execução só na conta demo.")
    if not account["conectado"]:
        raise ExecucaoError("Terminal sem conexão com a corretora.")
    terminal = mt5.terminal()
    if not terminal["algo_trading_ativo"] or terminal["negociacao_via_python_desativada"]:
        raise ExecucaoError(
            "O MT5 está bloqueando ordens automáticas: ligue o botão Algo Trading e, em Ferramentas > Opções > Expert "
            "Advisors, desmarque \"Desativar negociação algorítmica via API Python externa\"."
        )
    return account


def _as_dict(record: Any) -> dict[str, Any]:
    try:
        data = record._asdict()
    except AttributeError:
        return {"valor": str(record)}
    return {k: (v if isinstance(v, (int, float, str, type(None))) else str(v)) for k, v in data.items()}


def execute(
    mt5: MT5Client,
    store: ProposalStore,
    rules: RiskRules,
    proposal_id: str,
    *,
    enabled: bool,
    approve: Approver,
    timeout_s: float = APPROVAL_TIMEOUT_S,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Executa uma proposta na conta demo depois da aprovação do usuário na janela. Ver as travas no módulo."""
    if not enabled:
        raise ExecucaoError("Execução desativada: para habilitar, ponha EXECUCAO_HABILITADA=sim no .env e reinicie o "
                            "Claude Desktop. Nada foi enviado.")
    if not _RUNNING.acquire(blocking=False):
        raise ExecucaoError("Outra execução está em andamento (esperando aprovação ou enviando): tente depois.")
    try:
        return _execute(mt5, store, rules, proposal_id.strip().lower(), approve, timeout_s, now or mt5.now_utc)
    finally:
        _RUNNING.release()


def _execute(mt5: MT5Client, store: ProposalStore, rules: RiskRules, proposal_id: str, approve: Approver,
             timeout_s: float, clock: Callable[[], datetime]) -> dict[str, Any]:
    moment = clock()
    row = _check_proposal(store, proposal_id, moment, MIN_TIME_LEFT)
    done = store.execution(proposal_id)
    if done is not None:
        raise ExecucaoError(f"A proposta {proposal_id} já teve uma tentativa de execução ({done['status']}).")
    account = _check_account(mt5, row)
    plan = _plan(mt5, rules, row, moment)
    left = (_parse(row["expira_utc"]) - moment).total_seconds()
    wait = max(1.0, min(timeout_s, left - MIN_TIME_LEFT.total_seconds()))
    if not store.start_execution(proposal_id, moment, plan["request"]):
        raise ExecucaoError(f"A proposta {proposal_id} já teve uma tentativa de execução.")

    result: dict[str, Any] = {"id": proposal_id, "simbolo": row["simbolo"], "direcao": row["direcao"],
                              "tipo_ordem": row["tipo_ordem"], "volume": row["volume"]}
    try:
        title, text = _summary(row, plan, account)
        answer = approve(title, text, wait)
    except Exception as exc:  # a janela não abriu: nada é enviado
        logger.exception("Janela de aprovação")
        store.update_execution(proposal_id, status="erro_na_janela", mensagem=str(exc))
        result.update(status="erro_na_janela", envio=f"nenhum: a janela de aprovação falhou ({exc})")
        return result
    result["aprovacao"] = answer
    if answer != APPROVED:
        status = {"recusada": "recusada_pelo_usuario", "sem_resposta": "sem_resposta"}.get(answer, "erro_na_janela")
        store.update_execution(proposal_id, aprovacao=answer, status=status)
        result.update(status=status, envio="nenhum: a ordem não foi aprovada na janela; nada foi enviado",
                      proxima_acao="Para tentar de novo, faça uma nova proposta (só se você pedir).")
        return result

    approved_at = clock()
    store.update_execution(proposal_id, aprovacao=answer, aprovada_utc=tempo.iso_utc(approved_at), status="aprovada")
    expected = (int(row["conta_login"]), str(row["conta_servidor"]))
    try:
        # O preço andou durante a espera: tudo de novo antes de enviar.
        row = _check_proposal(store, proposal_id, approved_at, timedelta(0))
        account = _check_account(mt5, row)
        plan = _plan(mt5, rules, row, approved_at)
        chosen, check_message = _validate(mt5, plan, expected)
    except Exception as exc:  # qualquer falha antes do envio aborta (nada foi enviado)
        if not isinstance(exc, (ExecucaoError, MT5Error, LimitesError)):
            logger.exception("Execução %s", proposal_id)
        store.update_execution(proposal_id, status="abortada", mensagem=str(exc))
        result.update(status="abortada", envio=f"nenhum: {exc}", proxima_acao="Faça uma nova proposta.")
        return result
    if chosen is None:
        store.update_execution(proposal_id, status="recusada_pelo_servidor", mensagem=check_message)
        result.update(status="recusada_pelo_servidor", envio=f"nenhum: o servidor não validou a ordem ({check_message})")
        return result

    # Imediatamente antes do envio: parada e validade de novo; o cliente confere habilitação, parada e conta.
    sent_at = clock()
    try:
        if kill_file(store).exists():
            raise ExecucaoError("As execuções foram paradas (arquivo PARAR_EXECUCOES).")
        if sent_at >= _parse(row["expira_utc"]):
            raise ExecucaoError("A proposta expirou durante a validação.")
        store.update_execution(proposal_id, status="enviando", enviada_utc=tempo.iso_utc(sent_at), requisicao=chosen)
    except Exception as exc:
        store.update_execution(proposal_id, status="abortada", mensagem=str(exc))
        result.update(status="abortada", envio=f"nenhum: {exc}", proxima_acao="Faça uma nova proposta.")
        return result
    try:
        answer_mt5 = mt5.send_order(chosen, expected)
    except MT5Error as exc:
        _record(store, proposal_id, result, status="falhou", mensagem=str(exc))
        result.update(status="falhou", envio=f"erro ao enviar: {exc}. Confira no MT5 se algo foi aberto.")
        return result
    if answer_mt5 is None:
        _record(store, proposal_id, result, status="falhou", mensagem="order_send não respondeu")
        result.update(status="falhou", envio="sem resposta do MT5 ao enviar. Confira no MT5 se algo foi aberto.")
        return result
    retcode = answer_mt5.retcode
    if retcode in (RETCODE_DONE, RETCODE_PLACED, RETCODE_DONE_PARTIAL):
        # A situação vem da ação: muitos servidores respondem 10009 (DONE) também para ordens pendentes.
        status = "colocada" if not plan["market"] else ("executada_parcial" if retcode == RETCODE_DONE_PARTIAL
                                                         else "executada")
    else:
        status = "rejeitada"
    _record(store, proposal_id, result, status=status, retcode=retcode, ordem=answer_mt5.order or None,
            negocio=answer_mt5.deal or None, preco=answer_mt5.price or None, volume=answer_mt5.volume or None,
            mensagem=answer_mt5.comment, resposta=_as_dict(answer_mt5))
    result.update(status=status, retcode=retcode, mensagem_mt5=answer_mt5.comment, ordem=answer_mt5.order or None,
                  preco=answer_mt5.price or None, risco=plan["risk"], enviada=tempo.exibicao(sent_at))
    if status == "rejeitada":
        result["envio"] = f"enviada e rejeitada pela corretora ({retcode}: {answer_mt5.comment})"
        return result
    result["envio"] = "ordem enviada à conta DEMO" + (" (pendente, aguardando o preço)" if status == "colocada" else "")
    result["conferido_no_terminal"] = _reconcile(mt5, status, answer_mt5.order)
    if plan["warnings"]:
        result.setdefault("avisos", []).extend(plan["warnings"])
    return result


def _validate(mt5: MT5Client, plan: dict[str, Any], account: tuple[int, str]) -> tuple[dict | None, str]:
    """order_check com os modos de preenchimento aceitos; só troca de modo quando o servidor recusa o modo (10030)."""
    message = ""
    for filling in plan["fillings"]:
        request = {**plan["request"], "type_filling": filling}
        check = mt5.check_order(request, account)
        if check is None:
            return None, "order_check não respondeu"
        if check.retcode == 0:
            return request, ""
        message = f"{check.retcode}: {check.comment}"
        if check.retcode != RETCODE_INVALID_FILL:
            break
    return None, message


def _record(store: ProposalStore, proposal_id: str, result: dict[str, Any], **fields: Any) -> None:
    """Grava o resultado do envio; se o banco falhar, o resultado do MT5 ainda volta para o usuário."""
    try:
        store.update_execution(proposal_id, **fields)
    except Exception as exc:  # a ordem pode estar no MT5: não esconder o resultado por causa do banco
        logger.exception("Registro da execução %s", proposal_id)
        result.setdefault("avisos", []).append(
            f"Resultado não gravado no banco das propostas ({exc}): a execução fica como 'enviando'. Confira no MT5."
        )


def _reconcile(mt5: MT5Client, status: str, ticket: int) -> bool | None:
    """A ordem (pendente) ou a posição (a mercado) aparece no terminal? None se a leitura falhou."""
    try:
        if status == "colocada":
            return any(o["ticket"] == ticket for o in mt5.pending_orders())
        return any(p["identificador"] == ticket for p in mt5.open_positions())
    except MT5Error:
        return None
