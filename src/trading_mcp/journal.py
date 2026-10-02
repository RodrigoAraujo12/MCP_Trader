"""Journal local das operações da conta demo (SQLite).

O MT5 é a fonte dos fatos de execução (negócios, preços, custos e horários); o journal guarda o
que só o usuário sabe (setup, motivo, observações) e o stop inicial, que define o risco inicial e
o resultado em R. Uma operação é uma posição do MT5; cada execução dela é um negócio. Estatísticas
são calculadas aqui, de forma determinística.

O contexto SMC da entrada (``contexto_entrada``) é medido com os candles fechados antes da primeira
entrada, depois de gravados os fatos, e serve para agrupar as estatísticas; nada do que o preço fez depois
da entrada é medido.

Banco: o esquema é preparado numa transação própria; gravações usam BEGIN IMMEDIATE (a trava de
escrita vale desde a leitura); o trabalho no MT5 e no calendário é feito antes de abrir a transação.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from trading_mcp import contexto_entrada, tempo
from trading_mcp.calendario import CalendarError, EconomicCalendar
from trading_mcp.mt5_client import MT5Client, MT5Error

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3
# Operação "com notícia": evento dos EUA de importância alta de 30 min antes da entrada até o fechamento.
NEWS_BEFORE = timedelta(minutes=30)
NEWS_IMPORTANCE = "alta"
# Grupos com menos operações que isto são marcados como amostra pequena.
SMALL_SAMPLE = 20
_VOLUME_EPS = 1e-6
# Negócios de serviço (rolagem, desdobramento) não são entradas nem saídas da operação.
_SERVICE_REASONS = ("rolagem", "desdobramento")
_STOP_SOURCES = ("ordem_de_abertura", "informado", "observado")
_EMPTY = "O journal ainda está vazio: rode `journal_sincronizar` para importar as operações do MT5."
# Contexto das entradas só começa a ser medido até este tempo desde o início da sincronização (o Claude Desktop
# desiste de uma tool em ~60 s; cada medição leva ~0,6 s); o que faltar é medido nas próximas.
CONTEXT_BUDGET_S = 30.0
# Mede o contexto de uma entrada: (símbolo, direção 'compra'/'venda', horário UTC, preço) -> dict com "versao".
EntryContext = Callable[[str, str, datetime, float], dict[str, Any]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conta_login INTEGER NOT NULL,
    conta_servidor TEXT NOT NULL,
    posicao_id INTEGER NOT NULL,
    simbolo TEXT NOT NULL,
    direcao TEXT NOT NULL,
    origem TEXT NOT NULL,
    status TEXT NOT NULL,
    abertura_utc TEXT NOT NULL,
    fechamento_utc TEXT,
    volume_entrada REAL NOT NULL,
    preco_entrada REAL NOT NULL,
    volume_saida REAL NOT NULL,
    preco_saida REAL,
    entradas INTEGER NOT NULL,
    saidas INTEGER NOT NULL,
    stop_inicial REAL,
    alvo_inicial REAL,
    stop_inicial_fonte TEXT,
    stop_inicial_em_utc TEXT,
    risco_inicial REAL,
    resultado_bruto REAL NOT NULL,
    comissao REAL NOT NULL,
    swap REAL NOT NULL,
    taxas REAL NOT NULL,
    resultado_liquido REAL NOT NULL,
    fechamento_motivo TEXT,
    noticia TEXT NOT NULL DEFAULT 'desconhecido',
    noticia_fonte TEXT,
    noticias TEXT,
    noticia_ate_utc TEXT,
    contexto_entrada TEXT,
    contexto_versao INTEGER,
    setup TEXT,
    tags TEXT,
    motivo TEXT,
    observacoes TEXT,
    criado_utc TEXT NOT NULL,
    atualizado_utc TEXT NOT NULL,
    UNIQUE (conta_login, conta_servidor, posicao_id)
);
CREATE TABLE IF NOT EXISTS negocios (
    conta_login INTEGER NOT NULL,
    conta_servidor TEXT NOT NULL,
    ticket INTEGER NOT NULL,
    posicao_id INTEGER NOT NULL,
    ordem INTEGER,
    horario_utc TEXT NOT NULL,
    tipo TEXT NOT NULL,
    entrada TEXT NOT NULL,
    motivo TEXT NOT NULL,
    volume REAL NOT NULL,
    preco REAL NOT NULL,
    comissao REAL NOT NULL,
    swap REAL NOT NULL,
    lucro REAL NOT NULL,
    taxa REAL NOT NULL,
    simbolo TEXT NOT NULL,
    PRIMARY KEY (conta_login, conta_servidor, ticket)
);
"""
# Colunas acrescentadas depois da versão 1 (migração com ALTER TABLE).
_ADDED_COLUMNS = {"noticia_ate_utc": "TEXT", "contexto_entrada": "TEXT", "contexto_versao": "INTEGER"}

_STATS_NOTES = [
    "Só operações fechadas entram nas estatísticas.",
    "Taxa de acerto = ganhos / operações (empates contam no total). Resultado líquido = preço + comissão + "
    "swap + taxas. fator_lucro nulo = nenhuma perda no grupo.",
    "R = resultado líquido / risco inicial (perda até o stop inicial). Sem stop inicial conhecido, a operação "
    "fica fora das médias em R.",
    "Notícia = evento dos EUA de importância alta entre 30 min antes da entrada e o fechamento, pelo calendário "
    "do MT5; 'desconhecido' quando o calendário não cobria a operação na sincronização.",
    "por_contexto: o contexto SMC na hora da primeira entrada (só candles já fechados), a favor ou contra a direção "
    "da operação; sem_dados = timeframe sem histórico no terminal. Cada grupo é uma leitura isolada: grupos "
    "pequenos não provam nada.",
    "por_tag: uma operação com várias tags conta em cada uma.",
]


class JournalError(Exception):
    """Falha ao ler ou gravar o journal."""


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else tempo.iso_utc(moment)


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _show(text: str | None) -> dict[str, str] | None:
    moment = _parse(text)
    return None if moment is None else tempo.exibicao(moment)


def _price(value: float | None) -> float | None:
    return None if value is None else round(value, 8)  # tira ruído de float (85448.40000000001)


def _weighted(deals: list[dict]) -> tuple[float, float | None]:
    volume = sum(d["volume"] for d in deals)
    if volume <= _VOLUME_EPS:
        return 0.0, None
    return volume, sum(d["volume"] * d["preco"] for d in deals) / volume


def _summarize(deals: list[dict]) -> dict[str, Any]:
    """Fatos de uma posição a partir dos negócios dela (do mais antigo ao mais novo; há ao menos uma entrada)."""
    trading = [d for d in deals if d["motivo"] not in _SERVICE_REASONS]
    ins = [d for d in trading if d["entrada"] in ("in", "inout")]
    outs = [d for d in trading if d["entrada"] in ("out", "out_by", "inout")]
    first = ins[0]
    vol_in, price_in = _weighted(ins)
    vol_out, price_out = _weighted(outs)
    if vol_in - vol_out <= _VOLUME_EPS:
        status = "fechada"
    elif vol_out > _VOLUME_EPS:
        status = "parcial"
    else:
        status = "aberta"
    # Valores em dinheiro de todos os negócios, inclusive os de serviço.
    gross = sum(d["lucro"] for d in deals)
    commission = sum(d["comissao"] for d in deals)
    swap = sum(d["swap"] for d in deals)
    fees = sum(d["taxa"] for d in deals)
    return {
        "simbolo": first["simbolo"],
        "lado": first["tipo"],
        "direcao": "compra" if first["tipo"] == "buy" else "venda",
        "origem": first["motivo"],
        "ordem_abertura": first["ordem"],
        "status": status,
        "abertura": first["horario"],
        "fechamento": outs[-1]["horario"] if status == "fechada" else None,
        "volume_entrada": round(vol_in, 8),
        "preco_entrada": _price(price_in) or 0.0,
        "volume_saida": round(vol_out, 8),
        "preco_saida": _price(price_out),
        "entradas": len(ins),
        "saidas": len(outs),
        "resultado_bruto": round(gross, 2),
        "comissao": round(commission, 2),
        "swap": round(swap, 2),
        "taxas": round(fees, 2),
        "resultado_liquido": round(gross + commission + swap + fees, 2),
        "fechamento_motivo": outs[-1]["motivo"] if outs else None,
        "reversao": any(d["entrada"] == "inout" for d in deals),
    }


def _r_multiple(row: dict) -> float | None:
    if row["status"] != "fechada" or not row["risco_inicial"]:
        return None
    return round(row["resultado_liquido"] / row["risco_inicial"], 2)


def _stats(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    nets = [round(r["resultado_liquido"], 2) for r in rows]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    with_r = [r for r in rows if r["r"] is not None]
    rs = [r["r"] for r in with_r]
    return {
        "operacoes": n,
        "ganhos": len(wins),
        "perdas": len(losses),
        "empates": n - len(wins) - len(losses),
        "taxa_acerto_pct": round(len(wins) / n * 100, 1) if n else None,
        "resultado_liquido": round(sum(nets), 2),
        "resultado_medio": round(sum(nets) / n, 2) if n else None,
        "ganho_medio": round(mean(wins), 2) if wins else None,
        "perda_media": round(mean(losses), 2) if losses else None,
        "maior_ganho": max(wins) if wins else None,
        "maior_perda": min(losses) if losses else None,
        "fator_lucro": round(sum(wins) / -sum(losses), 2) if losses else None,
        "r": {
            "operacoes_com_r": len(rs),
            "sem_risco_inicial": n - len(rs),
            "r_total": round(sum(rs), 2) if rs else None,
            "r_medio": round(mean(rs), 2) if rs else None,
            # De onde veio o stop inicial: "observado" pode ser um stop já movido.
            "por_fonte_do_stop": {
                src: sum(1 for r in with_r if r["stop_inicial_fonte"] == src)
                for src in _STOP_SOURCES
                if any(r["stop_inicial_fonte"] == src for r in with_r)
            },
        },
        "custos": {
            "comissao": round(sum(r["comissao"] for r in rows), 2),
            "swap": round(sum(r["swap"] for r in rows), 2),
            "taxas": round(sum(r["taxas"] for r in rows), 2),
        },
        "amostra_pequena": n < SMALL_SAMPLE,
    }


def _group(rows: list[dict], key: Callable[[dict], str | list[str]]) -> list[dict[str, Any]]:
    """Estatísticas por grupo; ``key`` pode devolver vários grupos (a operação conta em cada um)."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        names = key(r)
        for name in [names] if isinstance(names, str) else names:
            groups.setdefault(name, []).append(r)
    out = []
    for name, items in groups.items():
        s = _stats(items)
        out.append(
            {
                "grupo": name,
                "operacoes": s["operacoes"],
                "taxa_acerto_pct": s["taxa_acerto_pct"],
                "resultado_liquido": s["resultado_liquido"],
                "r_medio": s["r"]["r_medio"],
                "operacoes_com_r": s["r"]["operacoes_com_r"],
                "amostra_pequena": s["amostra_pequena"],
            }
        )
    return sorted(out, key=lambda g: (-g["operacoes"], g["grupo"]))


def _filters(symbol: str, setup: str) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if symbol.strip():
        where.append("UPPER(simbolo) LIKE ?")
        params.append(f"{symbol.strip().upper()}%")
    if setup.strip():
        where.append("LOWER(setup) = ?")
        params.append(setup.strip().lower())
    return where, params


class Journal:
    """Journal em SQLite; cada chamada abre e fecha a própria conexão."""

    def __init__(
        self,
        path: Path,
        export_dir: Path,
        mt5: MT5Client,
        calendar: EconomicCalendar | None = None,
        now_utc: Callable[[], datetime] | None = None,
        *,
        entry_context: EntryContext | None = None,
        context_version: int = 0,
        monotonic: Callable[[], float] = time.monotonic,
        context_budget_s: float = CONTEXT_BUDGET_S,
    ) -> None:
        self._path = Path(path)
        self._export_dir = Path(export_dir)
        self._mt5 = mt5
        self._calendar = calendar
        self._now = now_utc or mt5.now_utc
        self._entry_context = entry_context
        self._context_version = context_version
        self._monotonic = monotonic
        self._context_budget = context_budget_s

    # ------------------------------------------------------------------ banco
    def _prepare(self, conn: sqlite3.Connection, write: bool) -> None:
        """Confere a versão antes de tudo; só quem grava cria ou migra o esquema (em autocommit)."""
        has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
        stored = None
        if has_meta:
            row = conn.execute("SELECT valor FROM meta WHERE chave = 'schema'").fetchone()
            stored = int(row["valor"]) if row else None
            if stored is not None and stored > SCHEMA_VERSION:
                raise JournalError(
                    f"O journal ({self._path}) foi criado por uma versão mais nova do servidor "
                    f"(schema {stored}); atualize o trading-mcp."
                )
        if not write:
            if not has_meta:
                raise JournalError(_EMPTY)
            return
        conn.executescript(_SCHEMA)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(operacoes)")}
        for name, kind in _ADDED_COLUMNS.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE operacoes ADD COLUMN {name} {kind}")
        if stored != SCHEMA_VERSION:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))

    @contextmanager
    def _db(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        """Conexão com uma transação: BEGIN IMMEDIATE para gravar, BEGIN para ler.

        Leitura não cria o arquivo: sem journal, avisa para sincronizar.
        """
        if not write and not self._path.is_file():
            raise JournalError(_EMPTY)
        try:
            if write:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise JournalError(f"Não foi possível abrir o journal ({self._path}): {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            self._prepare(conn, write)
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise JournalError(f"Erro no banco do journal ({self._path}): {exc}") from exc
        finally:
            conn.close()

    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str) -> Any:
        row = conn.execute("SELECT valor FROM meta WHERE chave = ?", (key,)).fetchone()
        return None if row is None else json.loads(row["valor"])

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    def _account(self, conn: sqlite3.Connection) -> tuple[int, str]:
        account = self._meta(conn, "ultima_conta")
        if not account:
            raise JournalError(_EMPTY)
        return int(account["login"]), str(account["servidor"])

    @staticmethod
    def _find(conn: sqlite3.Connection, login: int, server: str, position_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM operacoes WHERE conta_login = ? AND conta_servidor = ? AND posicao_id = ?",
            (login, server, position_id),
        ).fetchone()

    def _snapshot(self, login: int, server: str, position_ids: list[int]) -> dict[int, dict]:
        """Operações já gravadas, lidas sem trava de escrita (o cálculo vem antes da gravação)."""
        if not position_ids or not self._path.is_file():
            return {}
        try:
            with self._db(write=False) as conn:
                return {
                    pid: dict(row) for pid in position_ids if (row := self._find(conn, login, server, pid)) is not None
                }
        except JournalError as exc:
            if str(exc) == _EMPTY:
                return {}
            raise

    # ------------------------------------------------------------------ sincronização
    @staticmethod
    def _initial_stop(
        facts: dict, orders: list[dict], existing: dict | None, open_position: dict | None, now: datetime
    ) -> dict[str, Any]:
        """Stop/alvo iniciais: informado > ordem de abertura > primeira observação da posição aberta."""
        keys = ("stop_inicial", "alvo_inicial", "stop_inicial_fonte", "stop_inicial_em_utc")
        if existing is not None and existing["stop_inicial_fonte"] == "informado":
            return {k: existing[k] for k in keys}
        opening = next((o for o in orders if o["ticket"] == facts["ordem_abertura"]), None)
        if opening is not None and opening["stop_loss"]:
            return {
                "stop_inicial": opening["stop_loss"],
                "alvo_inicial": opening["take_profit"],
                "stop_inicial_fonte": "ordem_de_abertura",
                "stop_inicial_em_utc": _iso(opening["colocada"]),
            }
        if existing is not None and existing["stop_inicial_fonte"] in ("ordem_de_abertura", "observado"):
            return {k: existing[k] for k in keys}
        if open_position is not None and open_position["stop_loss"]:
            return {
                "stop_inicial": open_position["stop_loss"],
                "alvo_inicial": open_position["take_profit"],
                "stop_inicial_fonte": "observado",
                "stop_inicial_em_utc": _iso(now),
            }
        return {
            "stop_inicial": None,
            "alvo_inicial": opening["take_profit"] if opening else None,
            "stop_inicial_fonte": None,
            "stop_inicial_em_utc": None,
        }

    def _risk(self, symbol: str, side: str, volume: float, entry: float, stop: float | None) -> float | None:
        """Perda até o stop inicial (positiva); None se o stop não fica do lado da perda."""
        if stop is None or volume <= 0:
            return None
        loss = self._mt5.profit(symbol, side, volume, entry, stop)
        return round(-loss, 2) if loss < 0 else None

    def _risk_for(self, pid: int, facts: dict, stop: dict, existing: dict | None, warnings: list[str]) -> float | None:
        """Risco inicial; congelado numa operação fechada já calculada e mantido se o MT5 falhar."""
        same = (
            existing is not None
            and existing["stop_inicial"] == stop["stop_inicial"]
            and abs(existing["volume_entrada"] - facts["volume_entrada"]) <= _VOLUME_EPS
            and existing["preco_entrada"] == facts["preco_entrada"]
        )
        kept = existing["risco_inicial"] if same else None  # type: ignore[index]
        if kept is not None and existing["status"] == "fechada":  # type: ignore[index]
            return kept
        try:
            risk = self._risk(facts["simbolo"], facts["lado"], facts["volume_entrada"], facts["preco_entrada"],
                              stop["stop_inicial"])
        except MT5Error as exc:
            suffix = "; mantido o valor anterior" if kept is not None else ""
            warnings.append(f"Risco inicial da posição {pid} não recalculado ({exc}){suffix}.")
            return kept
        if stop["stop_inicial"] is not None and risk is None:
            warnings.append(
                f"Posição {pid}: o stop inicial ({stop['stop_inicial_fonte']}) não fica do lado da perda; risco "
                "inicial e R ficam vazios (informe o stop original com `journal_anotar`)."
            )
        return risk

    def _news(self, facts: dict, existing: dict | None, now: datetime, warnings: list[str]) -> dict[str, Any]:
        keys = ("noticia", "noticia_fonte", "noticias", "noticia_ate_utc")
        if existing is not None and existing["noticia_fonte"] == "informado":
            return {k: existing.get(k) for k in keys}
        unknown = {"noticia": "desconhecido", "noticia_fonte": None, "noticias": None, "noticia_ate_utc": None}
        end = facts["fechamento"] or now
        kept = (
            {k: existing.get(k) for k in keys}
            if existing is not None and existing["noticia_fonte"] == "calendario"
            else dict(unknown)
        )
        # Um "nao" verificado só até antes do fim da operação não vale mais: o resto não foi visto.
        if kept["noticia"] == "nao" and (_parse(kept["noticia_ate_utc"]) or datetime.min.replace(tzinfo=timezone.utc)) < end:
            kept = dict(unknown)
        if self._calendar is None:
            return kept
        try:
            events, covered = self._calendar.events_between(facts["abertura"] - NEWS_BEFORE, end, NEWS_IMPORTANCE)
        except CalendarError as exc:
            if not any(w.startswith("Calendário indisponível") for w in warnings):
                warnings.append(f"Calendário indisponível: notícia não marcada ({exc})")
            return kept
        if events:  # evento encontrado vale mesmo que o arquivo não cubra a operação inteira
            return {
                "noticia": "sim",
                "noticia_fonte": "calendario",
                "noticias": json.dumps(events, ensure_ascii=False),
                "noticia_ate_utc": _iso(end),
            }
        if covered:
            return {"noticia": "nao", "noticia_fonte": "calendario", "noticias": None, "noticia_ate_utc": _iso(end)}
        return kept

    def sync(self, days: float = 7) -> dict[str, Any]:
        """Importa do MT5 as posições com negócios nos últimos ``days`` dias e as posições abertas."""
        if not 0 < days <= 366:
            raise ValueError("dias deve estar entre 0 e 366.")
        started = self._monotonic()
        account = self._mt5.account()
        login, server = account["login"], account["servidor"]
        now = self._now()
        start = now - timedelta(days=days)
        warnings: list[str] = []
        if not account["is_demo"]:
            warnings.append(f"ATENÇÃO: a conta conectada NÃO é demo (tipo: {account['tipo_conta']}).")

        # 1) MT5: posições abertas, negócios da janela e o histórico completo de cada posição.
        open_positions = {p["identificador"]: p for p in self._mt5.open_positions()}
        window = self._mt5.deals(start, now + timedelta(minutes=1))
        trading = [d for d in window if d["tipo"] in ("buy", "sell") and d["posicao_id"]]
        position_ids = sorted({d["posicao_id"] for d in trading} | set(open_positions))
        gathered = []
        for pid in position_ids:
            deals = [d for d in self._mt5.deals(position=pid) if d["tipo"] in ("buy", "sell")]
            if not deals:
                continue
            if not any(d["entrada"] in ("in", "inout") and d["motivo"] not in _SERVICE_REASONS for d in deals):
                warnings.append(f"Posição {pid}: o histórico não tem o negócio de entrada; ignorada.")
                continue
            try:
                orders = self._mt5.position_orders(pid)
            except MT5Error as exc:
                orders = []
                warnings.append(f"Ordens da posição {pid} não lidas (stop inicial pela ordem indisponível): {exc}")
            facts = _summarize(deals)
            facts["incompleta"] = facts["status"] != "fechada" and pid not in open_positions
            if facts["incompleta"]:
                warnings.append(
                    f"Posição {pid}: os negócios indicam posição {facts['status']}, mas ela não está entre as abertas "
                    "(histórico incompleto ou fechada durante a leitura)."
                )
            if facts["reversao"]:
                warnings.append(f"Posição {pid} tem reversão (conta netting, não suportada): entrada e saída aproximadas.")
            gathered.append((pid, deals, orders, facts))

        # 2) Stop, risco e notícia calculados sobre um retrato do banco, sem trava de escrita.
        snapshot = self._snapshot(login, server, [g[0] for g in gathered])
        planned = []
        for pid, deals, orders, facts in gathered:
            existing = snapshot.get(pid)
            stop = self._initial_stop(facts, orders, existing, open_positions.get(pid), now)
            risk = self._risk_for(pid, facts, stop, existing, warnings)
            news = self._news(facts, existing, now, warnings)
            planned.append((pid, deals, facts, stop, risk, news))

        # 3) Gravação numa transação IMMEDIATE, relendo cada operação: o que o usuário informou nesse
        # meio-tempo prevalece.
        created = updated = 0
        with self._db(write=True) as conn:
            for pid, deals, facts, stop, risk, news in planned:
                existing = self._find(conn, login, server, pid)
                if existing is not None:
                    if facts["incompleta"] and existing["status"] == "fechada":
                        continue  # não troca uma operação fechada por um histórico incompleto
                    if existing["stop_inicial_fonte"] == "informado":
                        stop = {k: existing[k] for k in stop}
                        risk = existing["risco_inicial"]
                    if existing["noticia_fonte"] == "informado":
                        news = {k: existing[k] for k in news}
                for d in deals:
                    conn.execute(
                        "INSERT OR REPLACE INTO negocios VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            login, server, d["ticket"], d["posicao_id"], d["ordem"], _iso(d["horario"]), d["tipo"],
                            d["entrada"], d["motivo"], d["volume"], d["preco"], d["comissao"], d["swap"], d["lucro"],
                            d["taxa"], d["simbolo"],
                        ),
                    )
                values = {
                    "simbolo": facts["simbolo"],
                    "direcao": facts["direcao"],
                    "origem": facts["origem"],
                    "status": facts["status"],
                    "abertura_utc": _iso(facts["abertura"]),
                    "fechamento_utc": _iso(facts["fechamento"]),
                    "volume_entrada": facts["volume_entrada"],
                    "preco_entrada": facts["preco_entrada"],
                    "volume_saida": facts["volume_saida"],
                    "preco_saida": facts["preco_saida"],
                    "entradas": facts["entradas"],
                    "saidas": facts["saidas"],
                    **stop,
                    "risco_inicial": risk,
                    "resultado_bruto": facts["resultado_bruto"],
                    "comissao": facts["comissao"],
                    "swap": facts["swap"],
                    "taxas": facts["taxas"],
                    "resultado_liquido": facts["resultado_liquido"],
                    "fechamento_motivo": facts["fechamento_motivo"],
                    **news,
                    "atualizado_utc": _iso(now),
                }
                if existing is None:
                    values.update(conta_login=login, conta_servidor=server, posicao_id=pid, criado_utc=_iso(now))
                    columns = ", ".join(values)
                    conn.execute(
                        f"INSERT INTO operacoes ({columns}) VALUES ({', '.join('?' * len(values))})",
                        tuple(values.values()),
                    )
                    created += 1
                else:
                    assignments = ", ".join(f"{k} = ?" for k in values)
                    conn.execute(f"UPDATE operacoes SET {assignments} WHERE id = ?", (*values.values(), existing["id"]))
                    updated += 1
            self._set_meta(conn, "ultima_conta", {"login": login, "servidor": server})
            self._set_meta(conn, f"sincronizado:{login}:{server}", _iso(now))
            scope = "conta_login = ? AND conta_servidor = ?"
            missing_count = conn.execute(
                f"SELECT COUNT(*) FROM operacoes WHERE {scope} AND stop_inicial IS NULL", (login, server)
            ).fetchone()[0]
            missing = conn.execute(
                f"SELECT id, posicao_id, simbolo, abertura_utc FROM operacoes WHERE {scope} AND stop_inicial IS NULL "
                "ORDER BY abertura_utc DESC LIMIT 20",
                (login, server),
            ).fetchall()
            observed = conn.execute(
                f"SELECT COUNT(*) FROM operacoes WHERE {scope} AND stop_inicial_fonte = 'observado'", (login, server)
            ).fetchone()[0]
            open_count = conn.execute(
                f"SELECT COUNT(*) FROM operacoes WHERE {scope} AND status != 'fechada'", (login, server)
            ).fetchone()[0]

        # 4) Contexto SMC das entradas, depois dos fatos gravados (o MT5 é lido fora da transação).
        contexts = self._fill_contexts(login, server, started, warnings) if self._entry_context is not None else None

        result: dict[str, Any] = {
            "conta": {"login": login, "servidor": server, "tipo_conta": account["tipo_conta"]},
            "periodo": {"de": tempo.exibicao(start), "ate": tempo.exibicao(now)},
            "negocios_no_periodo": len(trading),
            "operacoes_novas": created,
            "operacoes_atualizadas": updated,
            "operacoes_abertas_ou_parciais": open_count,
            "sem_stop_inicial": [
                {"id": m["id"], "ticket": m["posicao_id"], "simbolo": m["simbolo"], "abertura": _show(m["abertura_utc"])}
                for m in missing
            ],
            "banco": str(self._path),
        }
        if contexts is not None:
            result["contexto_entrada"] = contexts
        if missing_count:
            shown = f" (mostrando {len(missing)})" if missing_count > len(missing) else ""
            warnings.append(
                f"{missing_count} operação(ões) sem stop inicial conhecido{shown}: sem risco inicial nem R. Informe com "
                "`journal_anotar` (stop_inicial)."
            )
        if observed:
            warnings.append(
                f"{observed} operação(ões) com stop inicial 'observado' (visto com a posição aberta, pode já ter sido "
                "movido): confirme ou corrija com `journal_anotar` para o R valer."
            )
        if warnings:
            result["avisos"] = warnings
        return result

    def _fill_contexts(self, login: int, server: str, started: float, warnings: list[str]) -> dict[str, int]:
        """Mede o contexto das entradas sem contexto (ou de uma versão anterior), das mais novas para as mais
        antigas (o M1 do terminal some primeiro nas antigas), dentro do tempo da sincronização (``started``).
        Falha fica para a próxima vez."""
        assert self._entry_context is not None
        service = ", ".join("?" * len(_SERVICE_REASONS))
        with self._db(write=False) as conn:
            pending = conn.execute(
                "SELECT o.id, o.posicao_id, o.simbolo, o.direcao, o.abertura_utc, "
                "(SELECT n.preco FROM negocios n WHERE n.conta_login = o.conta_login AND n.conta_servidor = "
                "o.conta_servidor AND n.posicao_id = o.posicao_id AND n.entrada IN ('in', 'inout') AND n.motivo NOT IN "
                f"({service}) ORDER BY n.horario_utc, n.ticket LIMIT 1) AS preco "
                "FROM operacoes o WHERE o.conta_login = ? AND o.conta_servidor = ? AND "
                "(o.contexto_versao IS NULL OR o.contexto_versao < ?) ORDER BY o.abertura_utc DESC, o.id DESC",
                (*_SERVICE_REASONS, login, server, self._context_version),
            ).fetchall()
        measured: list[tuple[int, str]] = []
        failed: list[str] = []
        left = 0
        for i, row in enumerate(pending):
            if self._monotonic() - started > self._context_budget:
                left = len(pending) - i
                break
            if row["preco"] is None:
                failed.append(f"posição {row['posicao_id']}: negócio de entrada não encontrado")
                continue
            try:
                found = self._entry_context(row["simbolo"], row["direcao"], _parse(row["abertura_utc"]), row["preco"])
            except (MT5Error, ValueError) as exc:
                failed.append(f"posição {row['posicao_id']}: {exc}")
                continue
            except Exception as exc:  # um erro de cálculo não derruba a sincronização (os fatos já foram gravados)
                logger.exception("Contexto da entrada da posição %s", row["posicao_id"])
                failed.append(f"posição {row['posicao_id']}: erro inesperado ({type(exc).__name__}: {exc})")
                continue
            measured.append((row["id"], json.dumps(found, ensure_ascii=False)))
        saved = 0
        if measured:
            try:
                with self._db(write=True) as conn:
                    for op_id, text in measured:
                        conn.execute(
                            "UPDATE operacoes SET contexto_entrada = ?, contexto_versao = ? WHERE id = ?",
                            (text, self._context_version, op_id),
                        )
                saved = len(measured)
            except JournalError as exc:  # os fatos já foram gravados: a sincronização não se perde
                warnings.append(f"Contexto da entrada de {len(measured)} operação(ões) medido mas não gravado ({exc}); "
                                "grava na próxima sincronização.")
        if failed:
            shown = "; ".join(failed[:3]) + (f" (e mais {len(failed) - 3})" if len(failed) > 3 else "")
            warnings.append(f"Contexto da entrada não medido em {len(failed)} operação(ões) ({shown}); tenta de novo "
                            "na próxima sincronização.")
        if left:
            warnings.append(f"Faltou tempo para medir o contexto da entrada de {left} operação(ões): rode "
                            "`journal_sincronizar` de novo (não duplica).")
        return {"medidos_agora": saved, "faltando": left + len(failed) + len(measured) - saved}

    # ------------------------------------------------------------------ anotações
    def annotate(
        self,
        *,
        operation_id: int | None = None,
        ticket: int | None = None,
        setup: str | None = None,
        tags: str | None = None,
        reason: str | None = None,
        note: str | None = None,
        initial_stop: float | None = None,
        news: str | None = None,
    ) -> dict[str, Any]:
        """Grava o que o usuário informa sobre uma operação; os fatos do MT5 não mudam aqui."""
        if (operation_id is None) == (ticket is None):
            raise ValueError("Informe exatamente um: operacao_id (do journal) ou ticket (da posição no MT5).")
        if all(v is None for v in (setup, tags, reason, note, initial_stop, news)):
            raise ValueError("Nada para anotar: informe setup, tags, motivo, observacao, stop_inicial ou noticia.")
        if news is not None and news not in ("sim", "nao"):
            raise ValueError("noticia deve ser 'sim' ou 'nao'.")
        if not self._path.is_file():
            raise JournalError(_EMPTY)
        now = self._now()
        warnings: list[str] = []
        with self._db(write=True) as conn:
            if operation_id is not None:
                row = conn.execute("SELECT * FROM operacoes WHERE id = ?", (operation_id,)).fetchone()
            else:
                login, server = self._account(conn)
                row = self._find(conn, login, server, ticket)  # type: ignore[arg-type]
            if row is None:
                which = f"id {operation_id}" if operation_id is not None else f"ticket {ticket}"
                raise ValueError(f"Operação não encontrada ({which}). Rode `journal_sincronizar` e confira em `journal_listar`.")
            changes: dict[str, Any] = {}
            if setup is not None:
                name = setup.strip() or None
                if name:
                    # Reaproveita a grafia já usada ("OB" e "ob" são o mesmo setup).
                    same = conn.execute(
                        "SELECT setup FROM operacoes WHERE conta_login = ? AND conta_servidor = ? AND "
                        "LOWER(setup) = LOWER(?) LIMIT 1",
                        (row["conta_login"], row["conta_servidor"], name),
                    ).fetchone()
                    name = same[0] if same else name
                changes["setup"] = name
            if tags is not None:
                cleaned = sorted({t.strip().lower() for t in tags.split(",") if t.strip()})
                changes["tags"] = ", ".join(cleaned) or None
            if reason is not None:
                changes["motivo"] = reason.strip() or None
            if note is not None and note.strip():
                stamp = tempo.exibicao(now)["sao_paulo"][:16]
                line = f"[{stamp} SP] {note.strip()}"
                changes["observacoes"] = f"{row['observacoes']}\n{line}" if row["observacoes"] else line
            if news is not None:
                changes.update(noticia=news, noticia_fonte="informado")
            if initial_stop is not None:
                entry = row["preco_entrada"]
                wrong_side = initial_stop >= entry if row["direcao"] == "compra" else initial_stop <= entry
                if wrong_side:
                    raise ValueError(
                        f"Stop inicial {initial_stop} não fica do lado da perda de uma {row['direcao']} com entrada "
                        f"{entry}: confira o valor."
                    )
                changes.update(stop_inicial=initial_stop, stop_inicial_fonte="informado", stop_inicial_em_utc=_iso(now))
                side = "buy" if row["direcao"] == "compra" else "sell"
                try:
                    changes["risco_inicial"] = self._risk(row["simbolo"], side, row["volume_entrada"], entry, initial_stop)
                except MT5Error as exc:
                    changes["risco_inicial"] = None
                    warnings.append(f"Risco inicial não calculado agora ({exc}); sai na próxima sincronização da conta.")
            changes["atualizado_utc"] = _iso(now)
            assignments = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE operacoes SET {assignments} WHERE id = ?", (*changes.values(), row["id"]))
            updated = conn.execute("SELECT * FROM operacoes WHERE id = ?", (row["id"],)).fetchone()
        result = self._view(updated)
        if warnings:
            result["avisos"] = warnings
        return result

    # ------------------------------------------------------------------ consultas
    @staticmethod
    def _view(row: sqlite3.Row, detailed_context: bool = False) -> dict[str, Any]:
        r = dict(row)
        opened, closed = _parse(r["abertura_utc"]), _parse(r["fechamento_utc"])
        closed_trade = r["status"] == "fechada"
        view: dict[str, Any] = {
            "id": r["id"],
            "ticket": r["posicao_id"],
            "simbolo": r["simbolo"],
            "direcao": r["direcao"],
            "status": r["status"],
            "origem": r["origem"],
            "abertura": _show(r["abertura_utc"]),
            "fechamento": _show(r["fechamento_utc"]),
            "duracao": tempo.describe_age((closed - opened).total_seconds()) if opened and closed else None,
            "volume": r["volume_entrada"],
            "preco_entrada": r["preco_entrada"],
            "preco_saida": r["preco_saida"],
            "entradas": r["entradas"],
            "saidas": r["saidas"],
            "stop_inicial": r["stop_inicial"],
            "stop_inicial_fonte": r["stop_inicial_fonte"],
            "alvo_inicial": r["alvo_inicial"],
            "risco_inicial": r["risco_inicial"],
            "resultado_liquido" if closed_trade else "resultado_realizado": r["resultado_liquido"],
            "resultado_bruto": r["resultado_bruto"],
            "custos": {"comissao": r["comissao"], "swap": r["swap"], "taxas": r["taxas"]},
            "r": _r_multiple(r),
            "fechamento_motivo": r["fechamento_motivo"],
            "noticia": r["noticia"],
            "noticias": json.loads(r["noticias"]) if r["noticias"] else [],
            "setup": r["setup"],
            "tags": r["tags"],
            "motivo": r["motivo"],
            "observacoes": r["observacoes"],
            "contexto": None,
        }
        if r.get("contexto_entrada"):
            context = json.loads(r["contexto_entrada"])
            view["contexto"] = contexto_entrada.dimensions(context, r["direcao"])
            if detailed_context:
                view["contexto_detalhado"] = context
        if not closed_trade:
            view["nota_resultado"] = (
                "Operação ainda aberta: só a parte realizada (comissões e saídas parciais). O resultado flutuante "
                "está em `posicoes`."
            )
            if r["noticia"] == "nao" and r.get("noticia_ate_utc"):
                view["noticia_verificada_ate"] = _show(r["noticia_ate_utc"])
        if r["status"] == "parcial":
            view["volume_fechado"] = r["volume_saida"]
        if r["entradas"] > 1 and r["risco_inicial"] is not None:
            view["nota_risco"] = "Várias entradas: risco inicial calculado sobre o volume total no preço médio."
        if r["stop_inicial_fonte"] == "observado":
            view["nota_stop"] = (
                f"Stop observado com a posição aberta em {r['stop_inicial_em_utc']}; pode já ter sido movido. "
                "Corrija com `journal_anotar` se o original era outro."
            )
        return view

    def _rows(self, conn: sqlite3.Connection, where: list[str], params: list[Any], order: str) -> list[sqlite3.Row]:
        login, server = self._account(conn)
        clauses = ["conta_login = ?", "conta_servidor = ?", *where]
        query = f"SELECT * FROM operacoes WHERE {' AND '.join(clauses)} ORDER BY {order}"
        return conn.execute(query, (login, server, *params)).fetchall()

    def list_operations(
        self,
        *,
        days: float | None = 30,
        symbol: str = "",
        setup: str = "",
        status: str | None = None,
        limit: int = 30,
        detailed_context: bool = False,
    ) -> dict[str, Any]:
        where, params = _filters(symbol, setup)
        if days:
            where.append("abertura_utc >= ?")
            params.append(_iso(self._now() - timedelta(days=days)))
        if status:
            where.append("status = ?")
            params.append(status)
        with self._db(write=False) as conn:
            rows = self._rows(conn, where, params, "abertura_utc DESC")
            login, server = self._account(conn)
            setups = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT setup FROM operacoes WHERE conta_login = ? AND conta_servidor = ? AND setup IS "
                    "NOT NULL ORDER BY setup",
                    (login, server),
                )
            ]
            synced = self._meta(conn, f"sincronizado:{login}:{server}")
        return {
            "conta": {"login": login, "servidor": server},
            "ultima_sincronizacao": _show(synced),
            "total": len(rows),
            "operacoes": [self._view(r, detailed_context) for r in rows[: max(1, limit)]],
            "setups_existentes": setups,
        }

    def stats(self, *, days: float | None = None, symbol: str = "", setup: str = "") -> dict[str, Any]:
        """Estatísticas das operações fechadas (por data de fechamento)."""
        where, params = _filters(symbol, setup)
        closed_where, closed_params = [*where, "status = 'fechada'"], list(params)
        if days:
            closed_where.append("fechamento_utc >= ?")
            closed_params.append(_iso(self._now() - timedelta(days=days)))
        with self._db(write=False) as conn:
            rows = [dict(r) for r in self._rows(conn, closed_where, closed_params, "fechamento_utc")]
            not_closed = len(self._rows(conn, [*where, "status != 'fechada'"], params, "id"))
            login, server = self._account(conn)
            synced = self._meta(conn, f"sincronizado:{login}:{server}")
        for r in rows:
            r["r"] = _r_multiple(r)
        notes = list(_STATS_NOTES)
        if not_closed:
            notes.append(f"{not_closed} operação(ões) aberta(s) ou parcialmente fechada(s) ficaram de fora.")
        general = _stats(rows)
        dims = {
            r["id"]: contexto_entrada.dimensions(json.loads(r["contexto_entrada"]), r["direcao"])
            for r in rows if r.get("contexto_entrada")
        }
        with_context = [r for r in rows if r["id"] in dims]
        observed = general["r"]["por_fonte_do_stop"].get("observado", 0)
        if observed:
            notes.append(
                f"{observed} R vêm de stop 'observado' (visto com a posição aberta; pode já ter sido movido, o que "
                "infla o R). Confirme o stop original com `journal_anotar`."
            )
        return {
            "conta": {"login": login, "servidor": server},
            "ultima_sincronizacao": _show(synced),
            "filtro": {"dias": days, "simbolo": symbol or None, "setup": setup or None},
            "geral": general,
            "por_setup": _group(rows, lambda r: r["setup"] or "sem setup"),
            "por_simbolo": _group(rows, lambda r: r["simbolo"]),
            "por_noticia": _group(rows, lambda r: r["noticia"]),
            "por_direcao": _group(rows, lambda r: r["direcao"]),
            "por_tag": _group(rows, lambda r: [t.strip() for t in r["tags"].split(",")] if r["tags"] else "sem tag"),
            "por_contexto": {
                name: _group(with_context, lambda r, n=name: dims[r["id"]][n]) for name in contexto_entrada.DIMENSIONS
            },
            "operacoes_sem_contexto": len(rows) - len(with_context),
            "observacoes": notes,
        }

    # ------------------------------------------------------------------ exportação
    def export(self) -> dict[str, Any]:
        """Cópia do banco e CSV das operações (separador ';' e vírgula decimal, como o Excel em português)."""
        with self._db(write=False) as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM operacoes ORDER BY abertura_utc")]
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        try:
            self._export_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise JournalError(f"Não foi possível criar a pasta de exportação ({self._export_dir}): {exc}") from exc
        suffix = ""
        for n in range(1, 100):
            backup_path = self._export_dir / f"journal-{stamp}{suffix}.sqlite3"
            csv_path = self._export_dir / f"operacoes-{stamp}{suffix}.csv"
            if not backup_path.exists() and not csv_path.exists():
                break
            suffix = f"-{n}"
        # VACUUM INTO: cópia consistente que respeita o tempo de espera do banco (sem transação aberta).
        try:
            with closing(sqlite3.connect(self._path, timeout=10, isolation_level=None)) as source:
                source.execute("VACUUM INTO ?", (str(backup_path),))
        except sqlite3.Error as exc:
            raise JournalError(f"Não foi possível gravar a cópia do journal ({backup_path}): {exc}") from exc
        columns = list(rows[0]) if rows else ["id"]

        def cell(value: Any) -> Any:
            return str(value).replace(".", ",") if isinstance(value, float) else value

        try:
            with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh, delimiter=";")
                writer.writerow(columns)
                for r in rows:
                    writer.writerow([cell(r[c]) for c in columns])
        except OSError as exc:
            raise JournalError(f"Não foi possível gravar o CSV ({csv_path}): {exc}") from exc
        return {
            "operacoes": len(rows),
            "copia_do_banco": str(backup_path),
            "csv": str(csv_path),
            "observacao": "CSV com ';' e vírgula decimal (abre direto no Excel em português). A cópia do banco "
            "guarda tudo, inclusive os negócios.",
        }
