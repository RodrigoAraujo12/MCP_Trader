"""Journal local das operações da conta demo (SQLite).

O MT5 é a fonte dos fatos de execução (negócios, preços, custos e horários); o journal guarda o
que só o usuário sabe (setup, motivo, observações) e o stop inicial, que define o risco inicial e
o resultado em R. Uma operação é uma posição do MT5; cada execução dela é um negócio. Estatísticas
são calculadas aqui, de forma determinística.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from trading_mcp import tempo
from trading_mcp.calendario import CalendarError, EconomicCalendar
from trading_mcp.mt5_client import MT5Client, MT5Error

SCHEMA_VERSION = 1
# Operação "com notícia": evento dos EUA de importância alta de 30 min antes da entrada até o fechamento.
NEWS_BEFORE = timedelta(minutes=30)
NEWS_IMPORTANCE = "alta"
# Grupos com menos operações que isto são marcados como amostra pequena.
SMALL_SAMPLE = 20
_VOLUME_EPS = 1e-6
_MANUAL = ("terminal", "celular", "web")

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

_STATS_NOTES = [
    "Só operações fechadas entram nas estatísticas.",
    "Taxa de acerto = ganhos / operações (empates contam no total). Resultado líquido = preço + comissão + "
    "swap + taxas.",
    "R = resultado líquido / risco inicial (perda até o stop inicial). Sem stop inicial conhecido, a operação "
    "fica fora das médias em R.",
    "Notícia = evento dos EUA de importância alta entre 30 min antes da entrada e o fechamento, pelo calendário "
    "do MT5; 'desconhecido' quando o calendário não cobria a operação na sincronização.",
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
    """Fatos de uma posição a partir dos negócios dela (do mais antigo ao mais novo)."""
    ins = [d for d in deals if d["entrada"] in ("in", "inout")]
    outs = [d for d in deals if d["entrada"] in ("out", "out_by", "inout")]
    first = ins[0] if ins else deals[0]
    vol_in, price_in = _weighted(ins)
    vol_out, price_out = _weighted(outs)
    remaining = vol_in - vol_out
    if remaining <= _VOLUME_EPS and vol_in > 0:
        status = "fechada"
    elif vol_out > _VOLUME_EPS:
        status = "parcial"
    else:
        status = "aberta"
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
    rs = [r["r"] for r in rows if r["r"] is not None]
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
        },
        "custos": {
            "comissao": round(sum(r["comissao"] for r in rows), 2),
            "swap": round(sum(r["swap"] for r in rows), 2),
            "taxas": round(sum(r["taxas"] for r in rows), 2),
        },
        "amostra_pequena": n < SMALL_SAMPLE,
    }


def _group(rows: list[dict], key: Callable[[dict], str]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
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


class Journal:
    """Journal em SQLite; cada chamada abre e fecha a própria conexão."""

    def __init__(
        self,
        path: Path,
        export_dir: Path,
        mt5: MT5Client,
        calendar: EconomicCalendar | None = None,
        now_utc: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path)
        self._export_dir = Path(export_dir)
        self._mt5 = mt5
        self._calendar = calendar
        self._now = now_utc or mt5.now_utc

    # ------------------------------------------------------------------ banco
    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, timeout=10)
        except (OSError, sqlite3.Error) as exc:
            raise JournalError(f"Não foi possível abrir o journal ({self._path}): {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # uma transação: grava tudo ou nada
                conn.executescript(_SCHEMA)
                row = conn.execute("SELECT valor FROM meta WHERE chave = 'schema'").fetchone()
                if row is None:
                    conn.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
                elif int(row["valor"]) > SCHEMA_VERSION:
                    raise JournalError(
                        f"O journal ({self._path}) foi criado por uma versão mais nova do servidor "
                        f"(schema {row['valor']}); atualize o trading-mcp."
                    )
                yield conn
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
            raise JournalError("O journal ainda está vazio: rode `journal_sincronizar` para importar as operações do MT5.")
        return int(account["login"]), str(account["servidor"])

    # ------------------------------------------------------------------ sincronização
    @staticmethod
    def _initial_stop(
        facts: dict, orders: list[dict], existing: sqlite3.Row | None, open_position: dict | None, now: datetime
    ) -> dict[str, Any]:
        """Stop/alvo iniciais: informado > ordem de abertura > primeira observação da posição aberta."""
        if existing is not None and existing["stop_inicial_fonte"] == "informado":
            return {k: existing[k] for k in ("stop_inicial", "alvo_inicial", "stop_inicial_fonte", "stop_inicial_em_utc")}
        opening = next((o for o in orders if o["ticket"] == facts["ordem_abertura"]), None)
        if opening is not None and opening["stop_loss"]:
            return {
                "stop_inicial": opening["stop_loss"],
                "alvo_inicial": opening["take_profit"],
                "stop_inicial_fonte": "ordem_de_abertura",
                "stop_inicial_em_utc": _iso(opening["colocada"]),
            }
        if existing is not None and existing["stop_inicial_fonte"] in ("ordem_de_abertura", "observado"):
            return {k: existing[k] for k in ("stop_inicial", "alvo_inicial", "stop_inicial_fonte", "stop_inicial_em_utc")}
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

    def _news(self, facts: dict, existing: sqlite3.Row | None, now: datetime, warnings: list[str]) -> dict[str, Any]:
        if existing is not None and existing["noticia_fonte"] == "informado":
            return {k: existing[k] for k in ("noticia", "noticia_fonte", "noticias")}
        kept = (
            {k: existing[k] for k in ("noticia", "noticia_fonte", "noticias")}
            if existing is not None and existing["noticia_fonte"] == "calendario"
            else {"noticia": "desconhecido", "noticia_fonte": None, "noticias": None}
        )
        if self._calendar is None:
            return kept
        start = facts["abertura"] - NEWS_BEFORE
        end = facts["fechamento"] or now
        try:
            events, covered = self._calendar.events_between(start, end, NEWS_IMPORTANCE)
        except CalendarError as exc:
            if not any(w.startswith("Calendário indisponível") for w in warnings):
                warnings.append(f"Calendário indisponível: notícia não marcada ({exc})")
            return kept
        if not covered:
            return kept
        return {
            "noticia": "sim" if events else "nao",
            "noticia_fonte": "calendario",
            "noticias": json.dumps(events, ensure_ascii=False) if events else None,
        }

    def sync(self, days: float = 7) -> dict[str, Any]:
        """Importa do MT5 as posições com negócios nos últimos ``days`` dias e as posições abertas."""
        if not 0 < days <= 366:
            raise ValueError("dias deve estar entre 0 e 366.")
        account = self._mt5.account()
        login, server = account["login"], account["servidor"]
        now = self._now()
        start = now - timedelta(days=days)
        warnings: list[str] = []
        if not account["is_demo"]:
            warnings.append(f"ATENÇÃO: a conta conectada NÃO é demo (tipo: {account['tipo_conta']}).")

        # Tudo do MT5 é lido antes de abrir a transação do banco.
        open_positions = {p["identificador"]: p for p in self._mt5.open_positions()}
        window = self._mt5.deals(start, now + timedelta(minutes=1))
        trading = [d for d in window if d["tipo"] in ("buy", "sell") and d["posicao_id"]]
        position_ids = sorted({d["posicao_id"] for d in trading} | set(open_positions))
        positions = []
        for pid in position_ids:
            deals = [d for d in self._mt5.deals(position=pid) if d["tipo"] in ("buy", "sell")]
            if not deals:
                continue
            try:
                orders = self._mt5.position_orders(pid)
            except MT5Error as exc:
                orders = []
                warnings.append(f"Ordens da posição {pid} não lidas (stop inicial pela ordem indisponível): {exc}")
            positions.append((pid, deals, orders, _summarize(deals)))

        created = updated = 0
        with self._db() as conn:
            for pid, deals, orders, facts in positions:
                existing = conn.execute(
                    "SELECT * FROM operacoes WHERE conta_login = ? AND conta_servidor = ? AND posicao_id = ?",
                    (login, server, pid),
                ).fetchone()
                stop = self._initial_stop(facts, orders, existing, open_positions.get(pid), now)
                risk = None
                risk_failed = False
                try:
                    risk = self._risk(
                        facts["simbolo"], facts["lado"], facts["volume_entrada"], facts["preco_entrada"], stop["stop_inicial"]
                    )
                except MT5Error as exc:
                    risk_failed = True
                    warnings.append(f"Risco inicial da posição {pid} não calculado: {exc}")
                if stop["stop_inicial"] is not None and risk is None and not risk_failed:
                    warnings.append(
                        f"Posição {pid}: o stop inicial ({stop['stop_inicial_fonte']}) não fica do lado da perda; "
                        "risco inicial e R ficam vazios (informe o stop original com `journal_anotar`)."
                    )
                if facts["reversao"]:
                    warnings.append(f"Posição {pid} tem reversão (conta netting): entrada e saída são aproximadas.")
                news = self._news(facts, existing, now, warnings)
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
            missing = conn.execute(
                "SELECT id, posicao_id, simbolo, abertura_utc FROM operacoes WHERE conta_login = ? AND "
                "conta_servidor = ? AND stop_inicial IS NULL ORDER BY abertura_utc DESC LIMIT 20",
                (login, server),
            ).fetchall()
            open_count = conn.execute(
                "SELECT COUNT(*) FROM operacoes WHERE conta_login = ? AND conta_servidor = ? AND status != 'fechada'",
                (login, server),
            ).fetchone()[0]

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
        if missing:
            warnings.append(
                f"{len(missing)} operação(ões) sem stop inicial conhecido: sem risco inicial nem R. Informe com "
                "`journal_anotar` (stop_inicial)."
            )
        if warnings:
            result["avisos"] = warnings
        return result

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
        now = self._now()
        warnings: list[str] = []
        with self._db() as conn:
            if operation_id is not None:
                row = conn.execute("SELECT * FROM operacoes WHERE id = ?", (operation_id,)).fetchone()
            else:
                login, server = self._account(conn)
                row = conn.execute(
                    "SELECT * FROM operacoes WHERE conta_login = ? AND conta_servidor = ? AND posicao_id = ?",
                    (login, server, ticket),
                ).fetchone()
            if row is None:
                which = f"id {operation_id}" if operation_id is not None else f"ticket {ticket}"
                raise ValueError(f"Operação não encontrada ({which}). Rode `journal_sincronizar` e confira em `journal_listar`.")
            changes: dict[str, Any] = {}
            if setup is not None:
                changes["setup"] = setup.strip() or None
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
                    warnings.append(f"Risco inicial não calculado agora ({exc}); sai na próxima sincronização.")
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
    def _view(row: sqlite3.Row) -> dict[str, Any]:
        r = dict(row)
        opened, closed = _parse(r["abertura_utc"]), _parse(r["fechamento_utc"])
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
            "resultado_liquido": r["resultado_liquido"],
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
        }
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
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if days:
            where.append("abertura_utc >= ?")
            params.append(_iso(self._now() - timedelta(days=days)))
        if symbol.strip():
            where.append("UPPER(simbolo) LIKE ?")
            params.append(f"{symbol.strip().upper()}%")
        if setup.strip():
            where.append("LOWER(setup) = ?")
            params.append(setup.strip().lower())
        if status:
            where.append("status = ?")
            params.append(status)
        with self._db() as conn:
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
            "operacoes": [self._view(r) for r in rows[: max(1, limit)]],
            "setups_existentes": setups,
        }

    def stats(self, *, days: float | None = None, symbol: str = "", setup: str = "") -> dict[str, Any]:
        """Estatísticas das operações fechadas (por data de fechamento)."""
        where = ["status = 'fechada'"]
        params: list[Any] = []
        now = self._now()
        if days:
            where.append("fechamento_utc >= ?")
            params.append(_iso(now - timedelta(days=days)))
        if symbol.strip():
            where.append("UPPER(simbolo) LIKE ?")
            params.append(f"{symbol.strip().upper()}%")
        if setup.strip():
            where.append("LOWER(setup) = ?")
            params.append(setup.strip().lower())
        with self._db() as conn:
            rows = [dict(r) for r in self._rows(conn, where, params, "fechamento_utc")]
            login, server = self._account(conn)
            not_closed = conn.execute(
                "SELECT COUNT(*) FROM operacoes WHERE conta_login = ? AND conta_servidor = ? AND status != 'fechada'",
                (login, server),
            ).fetchone()[0]
            synced = self._meta(conn, f"sincronizado:{login}:{server}")
        for r in rows:
            r["r"] = _r_multiple(r)
        notes = list(_STATS_NOTES)
        if not_closed:
            notes.append(f"{not_closed} operação(ões) aberta(s) ou parcialmente fechada(s) ficaram de fora.")
        return {
            "conta": {"login": login, "servidor": server},
            "ultima_sincronizacao": _show(synced),
            "filtro": {"dias": days, "simbolo": symbol or None, "setup": setup or None},
            "geral": _stats(rows),
            "por_setup": _group(rows, lambda r: r["setup"] or "sem setup"),
            "por_simbolo": _group(rows, lambda r: r["simbolo"]),
            "por_noticia": _group(rows, lambda r: r["noticia"]),
            "por_direcao": _group(rows, lambda r: r["direcao"]),
            "observacoes": notes,
        }

    # ------------------------------------------------------------------ exportação
    def export(self) -> dict[str, Any]:
        """Cópia do banco e CSV das operações (separador ';' e vírgula decimal, como o Excel em português)."""
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        try:
            self._export_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise JournalError(f"Não foi possível criar a pasta de exportação ({self._export_dir}): {exc}") from exc
        backup_path = self._export_dir / f"journal-{stamp}.sqlite3"
        csv_path = self._export_dir / f"operacoes-{stamp}.csv"
        with self._db() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM operacoes ORDER BY abertura_utc")]
            try:
                with closing(sqlite3.connect(backup_path)) as target:
                    conn.backup(target)
            except (OSError, sqlite3.Error) as exc:
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
