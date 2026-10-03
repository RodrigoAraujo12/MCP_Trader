"""Limites de risco do usuário e propostas de operação (etapa E). Nada aqui envia, altera ou cancela ordens.

Regras do usuário, em porcentagem do saldo do INÍCIO do período (fixo durante ele, como nas mesas proprietárias):

* por operação: 1,25% da base do dia;
* por dia: perda líquida de até 5% da base do dia de mercado (vira às 17:00 de Nova York);
* por semana: até 25% da base da semana de mercado (domingo 17:00 de Nova York).

Saldo do início = saldo atual menos tudo o que mexeu no saldo desde então, pelo histórico de negócios do MT5.
Depósitos e saques (negócio de saldo) não são resultado: entram na base (saldo do início + depósitos − saques do
período), e assim uma conta aberta no meio da semana tem o depósito como base, não zero. Crédito da corretora não
mexe no saldo e fica de fora. Resultado do período = operações (lucro, comissão, swap, taxa) e os demais lançamentos
(tarifas, juros, dividendos, correções). Disponível = limite + resultado do período − perda nos stops das posições
abertas (com o swap já acumulado nelas) − perda nos stops das ordens pendentes, se executadas: o pior caso se tudo
bater no stop agora. Lucro já protegido por stop não conta como folga. Posição ou ordem sem stop deixa o disponível
indeterminado, e nenhuma proposta é feita.

Proposta = o lote para arriscar o máximo permitido (o limite por operação, ou menos se o dia ou a semana tiverem
menos espaço), arredondado para baixo. Ordem pendente executa no próprio preço e o stop sai no preço dele: o risco é
a distância entrada-stop. A mercado, a compra executa no ask e a venda no bid de agora: o risco parte desse preço.
Fica guardada com validade e uma assinatura (HMAC, chave num arquivo fora do banco) dos parâmetros exatos: a etapa F
só poderá executar uma proposta válida, íntegra, dentro do prazo e aprovada pelo usuário fora do chat, conferindo os
limites de novo na hora (propostas não reservam risco entre si).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trading_mcp import posicoes, risk, smc, tempo
from trading_mcp.calendario import CalendarError, EconomicCalendar
from trading_mcp.mt5_client import MT5Client, MT5Error

SCHEMA_VERSION = 2  # 2: tabela das execuções (etapa F)
# Versão do formato assinado; independente do banco, para uma mudança de tabela não invalidar assinaturas antigas.
SIGNATURE_VERSION = 1
MIN_VALIDITY_MIN, MAX_VALIDITY_MIN, DEFAULT_VALIDITY_MIN = 5, 240, 30
# Notícias de importância alta procuradas da criação até este tempo depois do fim da validade.
NEWS_AFTER = timedelta(minutes=15)
ENTRY_DISTANCE_WARNING_PCT = 5.0
# Espaço menor que isto (na moeda da conta) é tratado como esgotado.
MIN_ROOM = 0.01
KEY_BYTES = 32
# Negócios até este tempo à frente de "agora" entram: o relógio do Windows pode estar atrás do servidor.
_DEALS_AHEAD = timedelta(days=1)
_EMPTY = "Ainda não há propostas: use `proposta_operacao` (o banco é criado na primeira vez)."
_SIGNED = ("id", "criada_utc", "expira_utc", "conta_login", "conta_servidor", "conta_demo", "simbolo", "direcao",
           "tipo_ordem", "entrada", "stop", "alvo", "volume", "risco_valor", "status")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS propostas (
    id TEXT PRIMARY KEY,
    criada_utc TEXT NOT NULL,
    expira_utc TEXT NOT NULL,
    conta_login INTEGER NOT NULL,
    conta_servidor TEXT NOT NULL,
    conta_demo INTEGER NOT NULL,
    simbolo TEXT NOT NULL,
    direcao TEXT NOT NULL,
    tipo_ordem TEXT NOT NULL,
    entrada REAL NOT NULL,
    stop REAL NOT NULL,
    alvo REAL,
    volume REAL NOT NULL,
    risco_valor REAL,
    risco_pct REAL,
    risco_retorno REAL,
    status TEXT NOT NULL,
    motivos TEXT NOT NULL,
    detalhes TEXT NOT NULL,
    assinatura TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS propostas_criada ON propostas (criada_utc);
CREATE TABLE IF NOT EXISTS execucoes (
    proposta_id TEXT PRIMARY KEY REFERENCES propostas (id),
    iniciada_utc TEXT NOT NULL,
    aprovacao TEXT,
    aprovada_utc TEXT,
    enviada_utc TEXT,
    status TEXT NOT NULL,
    retcode INTEGER,
    ordem INTEGER,
    negocio INTEGER,
    preco REAL,
    volume REAL,
    mensagem TEXT,
    requisicao TEXT,
    resposta TEXT
);
"""

NOTES = [
    "Proposta não é ordem: nada foi enviado ao MT5. Quem decide e executa é o usuário.",
    "Limites em % da base do dia de mercado (17:00 de Nova York) e da semana (domingo 17:00): saldo do início mais "
    "depósitos e menos saques do período. Disponível = limite + resultado do período − perdas nos stops abertos "
    "(com o swap acumulado) e pendentes.",
    "Lote arredondado para baixo. Ordem pendente: risco da entrada ao stop. A mercado: do pior preço dentro do desvio "
    "máximo (2 spreads, mínimo 10 pontos, a partir do ask na compra e do bid na venda) ao stop. Comissão, gap e "
    "escorregamento além do desvio não entram.",
    "Risco/retorno é a geometria da operação (distância ao alvo ÷ distância ao stop), não a chance de acerto.",
    "Propostas não reservam risco entre si: a execução (etapa F) confere os limites de novo na hora.",
]


class LimitesError(Exception):
    """Falha ao ler ou gravar as propostas."""


@dataclass(frozen=True)
class RiskRules:
    per_trade_pct: float = 1.25
    daily_pct: float = 5.0
    weekly_pct: float = 25.0


def _money(deal: dict[str, Any]) -> float:
    return deal["lucro"] + deal["comissao"] + deal["swap"] + deal["taxa"]


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _period(deals: list[dict[str, Any]], start: datetime, balance: float, pct: float, open_loss: float | None,
            name: str) -> dict[str, Any]:
    inside = [d for d in deals if d["horario"] >= start]
    trading = [d for d in inside if d["tipo"] in ("buy", "sell")]
    trades = sum(_money(d) for d in trading)
    flows = sum(_money(d) for d in inside if d["tipo"] == "saldo")
    other = sum(_money(d) for d in inside if d["tipo"] not in ("buy", "sell", "saldo", "credito"))
    result = trades + other
    start_balance = balance - result - flows
    base = start_balance + flows
    limit = base * pct / 100
    out: dict[str, Any] = {
        "inicio": tempo.exibicao(start),
        "saldo_inicio": round(start_balance, 2),
        "base_dos_limites": round(base, 2),
        "resultado": round(result, 2),
        "resultado_operacoes": round(trades, 2),
        "posicoes_com_saida": len({d["posicao_id"] for d in trading if d["entrada"] in ("out", "out_by", "inout")}),
        "limite_pct": pct,
        "limite": round(limit, 2),
        "resultado_pct_da_base": round(result / base * 100, 2) if base > 0 else None,
    }
    if flows:
        out["depositos_e_saques"] = round(flows, 2)
    if other:
        out["outros_lancamentos"] = round(other, 2)
    if base <= 0:
        out.update(disponivel=None, situacao="indeterminado")
        out["motivo"] = f"Base dos limites da {name} não é positiva: limites não calculáveis."
        return out
    if limit + result <= MIN_ROOM:
        out.update(disponivel=_round(None if open_loss is None else limit + result - open_loss),
                   situacao="limite_atingido")
        out["motivo"] = f"A perda realizada na {name} já chegou ao limite de {pct:g}%: sem novas operações."
        return out
    if open_loss is None:
        out.update(disponivel=None, situacao="indeterminado")
        out["motivo"] = "Há posição ou ordem sem stop (ou sem valor no stop): a perda possível não tem limite."
        return out
    available = limit + result - open_loss
    out["disponivel"] = round(available, 2)
    out["situacao"] = "ok" if available > MIN_ROOM else "comprometido_pelos_stops"
    if available <= MIN_ROOM:
        out["motivo"] = (f"Se os stops abertos e pendentes forem atingidos, a {name} chega ao limite de {pct:g}%: "
                         "sem espaço para outra operação.")
    return out


def account_limits(mt5: MT5Client, rules: RiskRules, now: datetime | None = None) -> dict[str, Any]:
    """Painel dos limites: saldo do início do dia e da semana de mercado, resultado, exposição nos stops e quanto
    ainda cabe na próxima operação."""
    moment = now or mt5.now_utc()
    day_start, week_start = smc.market_day_start(moment), smc.market_week_start(moment)
    # Posições primeiro, depois o histórico e por fim o saldo: um stop atingido no meio das leituras aparece no
    # histórico e no saldo (perda contada), nunca some dos dois.
    report = posicoes.build(mt5, include_pending=True, now=lambda: moment)
    deals = mt5.deals(min(day_start, week_start), moment + _DEALS_AHEAD)
    account = mt5.account()
    balance = float(account["saldo"])
    totals = report["totais"]
    pending = report.get("totais_pendentes") or {}
    pos_loss = totals.get("perda_nos_stops")
    pend_loss = pending.get("perda_nos_stops_se_executadas") if report.get("pendentes") is not None else None
    swap_cost = max(0.0, -(totals.get("swap") or 0.0))  # o swap acumulado sai do saldo quando a posição fecha
    no_stop = (totals.get("posicoes_sem_stop") or 0) + (pending.get("ordens_sem_stop") or 0)
    open_loss = None if (pos_loss is None or pend_loss is None or no_stop) else pos_loss + pend_loss + swap_cost

    day = _period(deals, day_start, balance, rules.daily_pct, open_loss, "dia")
    week = _period(deals, week_start, balance, rules.weekly_pct, open_loss, "semana")
    per_trade = day["base_dos_limites"] * rules.per_trade_pct / 100 if day["base_dos_limites"] > 0 else None
    candidates = {"por_operacao": per_trade, "dia": day["disponivel"], "semana": week["disponivel"]}
    blocked = [p for p in (day, week) if p["situacao"] != "ok"]
    next_trade: dict[str, Any] = {"limite_por_operacao": _round(per_trade), "limite_por_operacao_pct": rules.per_trade_pct}
    if blocked or per_trade is None or per_trade <= MIN_ROOM:
        next_trade.update(pode_operar=False, risco_maximo=None,
                          motivo=" ".join(p.get("motivo", "") for p in blocked).strip() or "Base do dia inválida.")
    else:
        binding = min(candidates, key=lambda k: candidates[k])  # type: ignore[arg-type,return-value]
        next_trade.update(pode_operar=True, risco_maximo=round(candidates[binding], 2), limitado_por=binding)  # type: ignore[arg-type]
    warnings: list[str] = []
    if not account["is_demo"]:
        warnings.append(f"ATENÇÃO: a conta conectada NÃO é demo (tipo: {account['tipo_conta']}).")
    if not account["conectado"]:
        warnings.append("Terminal sem conexão: histórico, posições e saldo podem estar desatualizados.")
    if no_stop:
        warnings.append(f"{no_stop} posição(ões)/ordem(ns) sem stop: coloque o stop para os limites voltarem a valer.")
    warnings += [w for w in report.get("avisos", []) if "NÃO é demo" not in w and "sem conexão" not in w]
    return {
        "conta": {"login": account["login"], "servidor": account["servidor"], "tipo_conta": account["tipo_conta"],
                  "is_demo": account["is_demo"], "conectado": account["conectado"], "moeda": account["moeda"],
                  "saldo": account["saldo"], "equity": account["equity"], "margem_livre": account["margem_livre"]},
        "coletado": tempo.exibicao(moment),
        "regras": {"por_operacao_pct": rules.per_trade_pct, "dia_pct": rules.daily_pct, "semana_pct": rules.weekly_pct,
                   "base": "saldo do início do dia/semana de mercado, mais depósitos e menos saques do período",
                   "dia_vira": "17:00 de Nova York", "semana_vira": "domingo 17:00 de Nova York"},
        "dia": day,
        "semana": week,
        "exposicao_aberta": {
            "posicoes": totals.get("posicoes"),
            "lucro_aberto": totals.get("lucro_aberto"),
            "perda_nos_stops_posicoes": pos_loss,
            "perda_nos_stops_pendentes": pend_loss,
            "swap_acumulado": _round(totals.get("swap")),
            "lucro_protegido_nos_stops": totals.get("lucro_protegido_nos_stops"),
            "sem_stop": no_stop,
        },
        "proxima_operacao": next_trade,
        "avisos": warnings,
        "observacoes": NOTES[1:3],
    }


# --------------------------------------------------------------------------- propostas
def _order_type(direction: str, entry: float, quote: dict[str, Any]) -> str:
    """A mercado quando a entrada está a menos de um spread do preço que executaria (ask na compra, bid na venda)."""
    spread = max(quote["ask"] - quote["bid"], 0.0)
    if direction == "compra":
        ref = quote["ask"]
        if abs(entry - ref) <= spread:
            return "a_mercado"
        return "compra_limitada" if entry < ref else "compra_stop"
    ref = quote["bid"]
    if abs(entry - ref) <= spread:
        return "a_mercado"
    return "venda_limitada" if entry > ref else "venda_stop"


def market_deviation(quote: dict[str, Any], point: float) -> int:
    """Desvio máximo de preço (pontos) aceito numa ordem a mercado: 2 spreads, no mínimo 10 pontos."""
    spread_points = round(max(quote["ask"] - quote["bid"], 0.0) / point) if point > 0 else 0
    return max(10, 2 * spread_points)


def worst_market_fill(direction: str, fill: float, quote: dict[str, Any], point: float) -> float:
    """Pior preço de uma ordem a mercado dentro do desvio máximo (acima na compra, abaixo na venda)."""
    deviation = market_deviation(quote, point) * point
    return fill + deviation if direction == "compra" else fill - deviation


def _canonical(item: dict[str, Any]) -> bytes:
    """Campos assinados em JSON estável; números como float (39000 e 39000.0, lido do SQLite, assinam igual)."""
    values: dict[str, Any] = {"schema": SIGNATURE_VERSION}
    for key in _SIGNED:
        value = item[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and key not in ("conta_login", "conta_demo"):
            value = float(value)
        values[key] = value
    return json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class ProposalStore:
    """Banco das propostas em SQLite (cada chamada abre e fecha a própria conexão) e a chave das assinaturas, num
    arquivo ao lado do banco (``.chave``), criado na primeira proposta."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._key_path = self._path.with_suffix(".chave")

    # ------------------------------------------------------------------ assinatura
    def _create_key(self) -> None:
        """Grava a chave num arquivo temporário e só então a põe no lugar, sem nunca trocar uma existente: quem
        perde a corrida (ou cai no meio) nunca deixa um arquivo vazio ou pela metade como chave."""
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._key_path.with_name(f"{self._key_path.name}.{secrets.token_hex(6)}.tmp")
        tmp.write_text(secrets.token_bytes(KEY_BYTES).hex(), encoding="ascii")
        try:
            if os.name == "nt":
                os.rename(tmp, self._key_path)  # no Windows falha se o destino existe
            else:
                os.link(tmp, self._key_path)  # idem no POSIX
        except FileExistsError:
            pass  # outra chamada criou primeiro: vale a dela
        finally:
            tmp.unlink(missing_ok=True)

    def _key(self) -> bytes:
        try:
            if not self._key_path.is_file():
                self._create_key()
            for attempt in range(3):
                key = bytes.fromhex(self._key_path.read_text(encoding="ascii").strip())
                if len(key) == KEY_BYTES:
                    return key
                time.sleep(0.05 * (attempt + 1))
        except (OSError, ValueError) as exc:
            raise LimitesError(f"Chave das assinaturas ilegível ou não gravável ({self._key_path}): {exc}") from exc
        raise LimitesError(
            f"Chave das assinaturas inválida ({self._key_path}: {len(key)} bytes em vez de {KEY_BYTES}). Não apague "
            "nem troque a chave à toa: propostas assinadas com a anterior deixam de conferir."
        )

    def sign(self, item: dict[str, Any]) -> str:
        return hmac.new(self._key(), _canonical(item), hashlib.sha256).hexdigest()

    def verify(self, proposal_id: str) -> bool:
        """True se a proposta guardada não foi alterada depois de assinada."""
        with self._db(write=False) as conn:
            row = conn.execute("SELECT * FROM propostas WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise LimitesError(f"Proposta {proposal_id} não encontrada.")
        return hmac.compare_digest(self.sign(dict(row)), row["assinatura"])

    # ------------------------------------------------------------------ banco
    @contextmanager
    def _db(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        if not write and not self._path.is_file():
            raise LimitesError(_EMPTY)
        try:
            if write:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise LimitesError(f"Não foi possível abrir o banco das propostas ({self._path}): {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
            if has_meta:
                row = conn.execute("SELECT valor FROM meta WHERE chave = 'schema'").fetchone()
                if row and int(row["valor"]) > SCHEMA_VERSION:
                    raise LimitesError(f"O banco das propostas ({self._path}) é de uma versão mais nova do servidor.")
            elif not write:
                raise LimitesError(_EMPTY)
            if write:
                conn.executescript(_SCHEMA)
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise LimitesError(f"Erro no banco das propostas ({self._path}): {exc}") from exc
        finally:
            conn.close()

    def save(self, item: dict[str, Any]) -> None:
        columns = ("id", "criada_utc", "expira_utc", "conta_login", "conta_servidor", "conta_demo", "simbolo",
                   "direcao", "tipo_ordem", "entrada", "stop", "alvo", "volume", "risco_valor", "risco_pct",
                   "risco_retorno", "status", "motivos", "detalhes", "assinatura")
        with self._db(write=True) as conn:
            conn.execute(f"INSERT INTO propostas ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                         tuple(item[c] for c in columns))

    def exists(self, proposal_id: str) -> bool:
        if not self._path.is_file():
            return False
        try:
            with self._db(write=False) as conn:
                return conn.execute("SELECT 1 FROM propostas WHERE id = ?", (proposal_id,)).fetchone() is not None
        except LimitesError:
            return False

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        with self._db(write=False) as conn:
            row = conn.execute("SELECT * FROM propostas WHERE id = ?", (proposal_id,)).fetchone()
        return None if row is None else dict(row)

    # ------------------------------------------------------------------ execuções (etapa F)
    def execution(self, proposal_id: str) -> dict[str, Any] | None:
        with self._db(write=False) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'execucoes'").fetchone():
                return None
            row = conn.execute("SELECT * FROM execucoes WHERE proposta_id = ?", (proposal_id,)).fetchone()
        return None if row is None else dict(row)

    def start_execution(self, proposal_id: str, now: datetime, request: dict[str, Any]) -> bool:
        """Reserva a proposta para uma única execução; False se ela já teve uma tentativa."""
        with self._db(write=True) as conn:
            try:
                conn.execute(
                    "INSERT INTO execucoes (proposta_id, iniciada_utc, status, requisicao) VALUES (?, ?, ?, ?)",
                    (proposal_id, tempo.iso_utc(now), "aguardando_aprovacao", json.dumps(request, ensure_ascii=False)),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def update_execution(self, proposal_id: str, **fields: Any) -> None:
        allowed = {"aprovacao", "aprovada_utc", "enviada_utc", "status", "retcode", "ordem", "negocio", "preco", "volume",
                   "mensagem", "requisicao", "resposta"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Campos desconhecidos da execução: {sorted(unknown)}")
        values = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in fields.items()}
        with self._db(write=True) as conn:
            conn.execute(f"UPDATE execucoes SET {', '.join(f'{k} = ?' for k in values)} WHERE proposta_id = ?",
                         (*values.values(), proposal_id))

    def open_risk(self, now: datetime, login: int, server: str) -> tuple[int, float]:
        """Propostas válidas e dentro do prazo da conta e o risco somado delas."""
        if not self._path.is_file():
            return 0, 0.0
        try:
            with self._db(write=False) as conn:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(risco_valor), 0) FROM propostas WHERE status = 'valida' AND "
                    "expira_utc > ? AND conta_login = ? AND conta_servidor = ?",
                    (tempo.iso_utc(now), login, server),
                ).fetchone()
        except LimitesError:
            return 0, 0.0
        return int(row[0]), float(row[1])

    def list(self, now: datetime, days: float | None = 7, symbol: str = "", limit: int = 30) -> dict[str, Any]:
        where, params = [], []
        if days:
            where.append("criada_utc >= ?")
            params.append(tempo.iso_utc(now - timedelta(days=days)))
        if symbol.strip():
            where.append("UPPER(simbolo) LIKE ?")
            params.append(f"{symbol.strip().upper()}%")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._db(write=False) as conn:
            rows = conn.execute(f"SELECT * FROM propostas {clause} ORDER BY criada_utc DESC", params).fetchall()
            has_exec = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'execucoes'").fetchone()
            executions = {r["proposta_id"]: dict(r) for r in conn.execute("SELECT * FROM execucoes")} if has_exec else {}
        now_iso = tempo.iso_utc(now)
        out = []
        for r in rows[: max(1, limit)]:
            item = {k: r[k] for k in ("id", "simbolo", "direcao", "tipo_ordem", "entrada", "stop", "alvo", "volume",
                                      "risco_valor", "risco_pct", "risco_retorno")}
            situation = r["status"]
            if situation == "valida" and r["expira_utc"] <= now_iso:
                situation = "expirada"
            item.update(situacao=situation, criada=tempo.exibicao(_parse(r["criada_utc"])),
                        expira=tempo.exibicao(_parse(r["expira_utc"])), conta_demo=bool(r["conta_demo"]))
            if r["status"] == "recusada":
                item["motivos"] = json.loads(r["motivos"])
            done = executions.get(r["id"])
            if done is not None:
                item["execucao"] = {k: done[k] for k in ("status", "retcode", "ordem", "preco", "volume", "mensagem")}
                item["situacao"] = f"execucao_{done['status']}"
            out.append(item)
        return {"total": len(rows), "propostas": out, "banco": str(self._path)}


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def propose(
    mt5: MT5Client,
    store: ProposalStore,
    rules: RiskRules,
    symbol: str,
    entry: float,
    stop: float,
    target: float | None = None,
    validity_min: int = DEFAULT_VALIDITY_MIN,
    calendar: EconomicCalendar | None = None,
    context: Callable[[str, str, datetime, float], dict[str, str]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Monta e guarda uma proposta (válida ou recusada, com os motivos). Não envia nada ao MT5."""
    if not MIN_VALIDITY_MIN <= validity_min <= MAX_VALIDITY_MIN:
        raise ValueError(f"validade_min deve estar entre {MIN_VALIDITY_MIN} e {MAX_VALIDITY_MIN}.")
    if entry <= 0 or stop <= 0 or (target is not None and target <= 0):
        raise ValueError("Entrada, stop e alvo devem ser preços positivos.")
    if entry == stop:
        raise ValueError("O stop precisa ser diferente da entrada.")
    direction = "compra" if stop < entry else "venda"
    side = "buy" if direction == "compra" else "sell"
    if target is not None and ((target <= entry) if direction == "compra" else (target >= entry)):
        raise ValueError(f"O alvo ({target}) precisa ficar do lado do lucro de uma {direction} com entrada {entry}.")
    moment = (now or mt5.now_utc()).astimezone(timezone.utc).replace(microsecond=0)
    spec = mt5.symbol_spec(symbol)
    resolved, digits = spec["simbolo"], spec["digitos"]
    if abs(entry - stop) < spec["tick_size"]:
        raise ValueError(f"Stop a {abs(entry - stop):.10g} da entrada: menor que o tick mínimo do símbolo "
                         f"({spec['tick_size']}).")

    reasons: list[str] = []
    warnings: list[str] = []
    limits = account_limits(mt5, rules, moment)
    account = limits["conta"]
    warnings += limits["avisos"]
    nxt = limits["proxima_operacao"]
    if not account["is_demo"]:
        reasons.append(f"Propostas só na conta demo (a conectada é {account['tipo_conta']}).")
    if not account["conectado"]:
        reasons.append("Terminal sem conexão com a corretora: saldo, histórico e preço podem estar desatualizados.")
    if not nxt["pode_operar"]:
        reasons.append(nxt["motivo"])

    quote = None
    try:
        quote = mt5.quote(resolved)
    except MT5Error as exc:
        reasons.append(f"Sem cotação ({exc}): tipo de ordem e preço de execução desconhecidos.")
    order_type = "desconhecido"
    fill = entry
    if quote is not None:
        order_type = _order_type(direction, entry, quote)
        if order_type == "a_mercado":
            if quote["estado"] != "atual":
                reasons.append(f"Cotação {quote['estado']}: uma ordem a mercado precisa do preço de agora.")
            fill = quote["ask"] if direction == "compra" else quote["bid"]
        elif quote["estado"] != "atual":
            warnings.append(f"Cotação {quote['estado']}: o tipo de ordem usa um preço que não é de agora.")
        mid = (quote["bid"] + quote["ask"]) / 2
        if abs(entry - mid) / mid * 100 > ENTRY_DISTANCE_WARNING_PCT:
            warnings.append(f"A entrada ({entry}) está a mais de {ENTRY_DISTANCE_WARNING_PCT:.0f}% do preço atual "
                            f"({round(mid, digits)}): confira o preço.")
    if (fill <= stop) if direction == "compra" else (fill >= stop):
        reasons.append(f"O preço de execução ({fill}) já passou do stop ({stop}).")
    if target is not None and ((fill >= target) if direction == "compra" else (fill <= target)):
        reasons.append(f"O preço de execução ({fill}) já passou do alvo ({target}).")
    distance = abs(fill - stop)
    spread = max(quote["ask"] - quote["bid"], 0.0) if quote is not None else 0.0
    if quote is not None and distance > 0 and spread / distance > 0.10:
        warnings.append(f"O spread atual equivale a {spread / distance:.0%} da distância do stop: o stop pode ser "
                        "acionado pelo próprio spread.")

    base = limits["dia"]["base_dos_limites"]
    volume = 0.0
    risk_value = loss_per_lot = None
    if not reasons:
        # A mercado, o lote é calculado pelo pior preço dentro do desvio máximo (a execução confere do mesmo jeito).
        sizing_fill = worst_market_fill(direction, fill, quote, spec["ponto"]) if order_type == "a_mercado" else fill
        loss_per_lot, _ = mt5.loss_per_lot(resolved, sizing_fill, stop)
        size = risk.position_size(balance=base, risk_percent=nxt["risco_maximo"] / base * 100, loss_per_lot=loss_per_lot,
                                  volume_min=spec["volume_min"], volume_max=spec["volume_max"],
                                  volume_step=spec["volume_step"])
        volume = size.lots
        if volume <= 0:
            min_risk = spec["volume_min"] * loss_per_lot
            reasons.append(f"O lote mínimo ({spec['volume_min']:g}) arriscaria {min_risk:.2f} ({min_risk / base * 100:.2f}% "
                           f"da base do dia), acima do permitido agora ({nxt['risco_maximo']:.2f}).")
        else:
            risk_value = round(volume * loss_per_lot, 2)
            warnings += list(size.warnings)
            margin = mt5.margin(resolved, side, fill, volume)
            if margin is not None and margin > float(account["margem_livre"]):
                reasons.append(f"Margem estimada ({margin:.2f}) maior que a margem livre ({account['margem_livre']}).")
                volume, risk_value = 0.0, None

    reward = rr = None
    if target is not None and distance > 0:
        rr = round(abs(target - fill) / distance, 2)
        if volume > 0:
            reward = round(mt5.profit(resolved, side, volume, fill, target), 2)

    expires = moment + timedelta(minutes=validity_min)
    news: list[dict[str, Any]] = []
    if calendar is not None:
        try:
            news, covered = calendar.events_between(moment, expires + NEWS_AFTER, "alta")
            if not covered:
                warnings.append("O arquivo do calendário não cobre toda a validade: notícias podem faltar.")
        except CalendarError as exc:
            warnings.append(f"Calendário indisponível ({exc}): notícias não conferidas.")
    if news:
        warnings.append("Notícia de importância alta durante a validade (ou logo depois): "
                        + "; ".join(f"{n['evento']} às {n['utc'][11:16]} UTC" for n in news[:3])
                        + ". O spread abre e o primeiro movimento é instável.")
    labels = None
    if context is not None:
        try:
            labels = context(resolved, direction, moment, entry)
        except Exception as exc:  # o contexto é informação extra: a proposta não depende dele
            warnings.append(f"Contexto SMC não medido ({type(exc).__name__}: {exc}).")
    if risk_value and not reasons and nxt.get("risco_maximo") is not None:
        count, committed = store.open_risk(moment, int(account["login"]), str(account["servidor"]))
        room = min(v for v in (limits["dia"]["disponivel"], limits["semana"]["disponivel"]) if v is not None)
        if count and committed + risk_value > room:
            warnings.append(f"Há {count} outra(s) proposta(s) válida(s) arriscando {committed:.2f}: executar todas "
                            f"passaria do espaço disponível ({room:.2f}). Propostas não reservam risco entre si.")

    proposal_id = secrets.token_hex(4)
    while store.exists(proposal_id):
        proposal_id = secrets.token_hex(4)
    status = "recusada" if reasons else "valida"
    item: dict[str, Any] = {
        "id": proposal_id, "criada_utc": tempo.iso_utc(moment), "expira_utc": tempo.iso_utc(expires),
        "conta_login": int(account["login"]), "conta_servidor": str(account["servidor"]),
        "conta_demo": int(bool(account["is_demo"])), "simbolo": resolved, "direcao": direction,
        "tipo_ordem": order_type, "entrada": float(entry), "stop": float(stop),
        "alvo": None if target is None else float(target), "volume": float(volume),
        "risco_valor": risk_value, "risco_pct": round(risk_value / base * 100, 2) if risk_value and base > 0 else None,
        "risco_retorno": rr, "status": status, "motivos": json.dumps(reasons, ensure_ascii=False),
    }
    details = {"limites": {k: limits[k] for k in ("dia", "semana", "exposicao_aberta", "proxima_operacao")},
               "preco_execucao_estimado": fill, "spread": round(spread, digits),
               "perda_por_lote": _round(loss_per_lot), "lucro_no_alvo": reward, "noticias": news,
               "contexto_smc": labels, "avisos": warnings}
    item["detalhes"] = json.dumps(details, ensure_ascii=False)
    item["assinatura"] = store.sign(item)
    store.save(item)

    result: dict[str, Any] = {
        "id": proposal_id,
        "status": status,
        "envio": "nenhum: proposta não é ordem; nada foi enviado ao MT5",
        "conta": {"login": account["login"], "tipo_conta": account["tipo_conta"], "moeda": account["moeda"]},
        "simbolo": resolved,
        "direcao": direction,
        "tipo_ordem": order_type,
        "entrada": entry,
        "preco_execucao_estimado": fill,
        "stop": stop,
        "alvo": target,
        "volume": volume,
        "risco": risk_value,
        "risco_pct_base_dia": item["risco_pct"],
        "risco_maximo_permitido": nxt.get("risco_maximo"),
        "limitado_por": nxt.get("limitado_por"),
        "lucro_no_alvo": reward,
        "risco_retorno": rr,
        "criada": tempo.exibicao(moment),
        "expira": tempo.exibicao(expires),
        "noticias_na_validade": news,
        "contexto_smc": labels,
        "limites": {name: {k: limits[name].get(k) for k in ("base_dos_limites", "resultado", "limite", "disponivel",
                                                             "situacao")} for name in ("dia", "semana")},
        "assinatura": item["assinatura"],
    }
    if reasons:
        result["motivos_da_recusa"] = reasons
    if warnings:
        result["avisos"] = warnings
    result["observacoes"] = NOTES
    return result
