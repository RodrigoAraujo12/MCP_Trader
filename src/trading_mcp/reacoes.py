"""Reações a eventos guardadas (SQLite) e estatísticas por tipo de surpresa.

O arquivo do calendário cobre poucos dias e os candles M1 do terminal cerca de 3 meses: o que não for
guardado se perde. ``register`` grava, para cada divulgação dos EUA de importância moderada ou alta com
horário exato, o fato publicado (calendário do MT5) e, separado, o movimento medido em cada instrumento
operado (a mesma medição de ``reacao_evento``). ``stats`` agrupa as divulgações de um evento pelo sentido
da surpresa. Medição, não causa: nada aqui vira compra ou venda.

Medição com falha (sem referência, janela sem negociação ou com preço antigo, candles faltando) pode ser
dado que o terminal ainda não tinha baixado: fica "a confirmar" e é refeita nos registros seguintes; só entra
nas estatísticas quando duas medições com pelo menos 10 min de distância dão o mesmo resultado. Se o horário
sair do histórico M1 antes disso, fica "não confirmável" (fora das estatísticas).

Banco: mesmo padrão do journal (uma conexão por chamada; esquema preparado antes da transação; gravação
com BEGIN IMMEDIATE; o trabalho no MT5 e no calendário é feito fora dela).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from trading_mcp import reacao, tempo
from trading_mcp.calendario import KEY_EVENTS, CalendarError, EconomicCalendar, matches_search
from trading_mcp.mt5_client import MT5Client, MT5Error

SCHEMA_VERSION = 1
# Janelas guardadas (minutos depois do evento); as estatísticas escolhem entre elas.
WINDOWS = (1, 5, 15, 60)
DEFAULT_STATS_WINDOWS = (5, 15)
# Divulgações guardadas e também o limiar de "outro evento dentro da janela" (como em reacao_evento).
MIN_IMPORTANCE = "moderada"
SMALL_SAMPLE = 20
# A partir disto o movimento é uma reação clara (README, seção de reação a eventos).
CLEAR_REACTION = 3.0
MAX_CODES = 3
RECENT_RELEASES = 8
MAX_INVENTORY = 60
MAX_STATS_DAYS = 3660
# O Claude Desktop desiste de uma tool depois de cerca de 60 s: o resto fica para a próxima chamada.
TIME_BUDGET_S = 40.0
BACKUP_NAME = "reacoes-copia.sqlite3"
# Margem para o último candle da janela mais longa fechar antes de medir.
_SETTLE = timedelta(minutes=1)
_LOOKBACK = timedelta(minutes=reacao.LOOKBACK_MIN)
_STALE = timedelta(minutes=reacao.STALE_MIN)
_LATE = timedelta(days=1)
# Confirmar uma medição com falha pela repetição só depois disso (o terminal pode estar baixando o histórico).
_CONFIRM_AFTER = timedelta(minutes=10)
# Situação de confirmação em ``medicoes.confirmada``.
_UNCONFIRMED, _CONFIRMED, _UNCONFIRMABLE = 0, 1, 2
_RANK = {"alta": 3, "moderada": 2, "baixa": 1, "nenhuma": 0}
_DIRECTIONS = (("acima", "surpresa_acima"), ("abaixo", "surpresa_abaixo"), ("igual", "igual_a_previsao"))
_EMPTY = "Ainda não há reações guardadas: rode `reacoes_registrar` (o banco é criado na primeira vez)."

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS eventos (
    id TEXT PRIMARY KEY,
    codigo TEXT NOT NULL,
    nome TEXT,
    importancia TEXT NOT NULL,
    horario_utc TEXT NOT NULL,
    periodo TEXT,
    frequencia TEXT,
    unidade TEXT,
    realizado REAL,
    previsao REAL,
    anterior REAL,
    anterior_revisado REAL,
    estimativa TEXT,
    surpresa REAL,
    sentido TEXT NOT NULL,
    leitura_da_fonte TEXT,
    registrado_utc TEXT NOT NULL,
    atualizado_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS eventos_horario ON eventos (horario_utc);
CREATE INDEX IF NOT EXISTS eventos_codigo ON eventos (codigo);
CREATE TABLE IF NOT EXISTS medicoes (
    horario_utc TEXT NOT NULL,
    simbolo TEXT NOT NULL,
    instrumento TEXT NOT NULL,
    situacao TEXT NOT NULL,
    referencia REAL,
    referencia_ate_utc TEXT,
    referencia_antiga INTEGER NOT NULL DEFAULT 0,
    maxima_pct REAL,
    minima_pct REAL,
    spread_antes_pontos INTEGER,
    spread_depois_max_pontos INTEGER,
    candles_recebidos INTEGER,
    candles_esperados INTEGER,
    avisos TEXT,
    medido_utc TEXT NOT NULL,
    confirmada INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (horario_utc, simbolo)
);
CREATE TABLE IF NOT EXISTS janelas (
    horario_utc TEXT NOT NULL,
    simbolo TEXT NOT NULL,
    minutos INTEGER NOT NULL,
    situacao TEXT NOT NULL,
    preco REAL,
    variacao REAL,
    variacao_pct REAL,
    pontos INTEGER,
    pips REAL,
    ate_utc TEXT,
    tipico REAL,
    vezes_o_tipico REAL,
    PRIMARY KEY (horario_utc, simbolo, minutos)
);
"""
# Campos do fato que vêm do calendário (comparados a cada registro).
_FACT_FIELDS = (
    "codigo", "nome", "importancia", "horario_utc", "periodo", "frequencia", "unidade", "realizado", "previsao",
    "anterior", "anterior_revisado", "estimativa", "surpresa", "sentido", "leitura_da_fonte",
)
# Calculados a partir de realizado e previsão: mudam junto com eles.
_DERIVED = ("surpresa", "sentido")

NOTES_REGISTER = [
    "Guarda todas as divulgações dos EUA de importância moderada ou alta com horário exato que o arquivo do "
    "calendário cobre (fato do calendário do MT5) e, separado, o movimento de cada instrumento em +1, +5, +15 e "
    "+60 min, medido como em `reacao_evento`.",
    "Rode pelo menos uma vez por semana: o arquivo do calendário cobre só os dias de InpDaysBack do serviço "
    "TradingMcpCalendar e o M1 do terminal, cerca de 3 meses (menos no BTCUSD). O que sair desses limites antes "
    "de ser guardado não pode mais ser medido.",
    "Pode rodar sempre: não duplica. Divulgações sem realizado ganham o valor quando ele aparece no calendário. "
    "Medições com falha ficam `a_confirmar` e são refeitas nos registros seguintes (a confirmação exige 10 min "
    "entre as duas medições).",
]
NOTES_STATS = [
    "Estatística descritiva das divulgações guardadas: fato do calendário do MT5 e movimento em candles M1 de bid "
    "(referência = último preço antes do horário; +N min = último preço antes de horário + N). Medição, não causa. "
    "Não é previsão nem regra de compra ou venda; a reação passada não garante a próxima.",
    "Surpresa = realizado − previsão do calendário do MT5, que pode não ser o consenso de mercado. 'acima' não diz se "
    "o dado é bom ou ruim para o ativo (nos pedidos de seguro-desemprego, acima = mais pedidos). `todas` inclui as "
    "divulgações sem surpresa calculada (sem previsão ou sem realizado no calendário).",
    "Eventos no mesmo horário se misturam na mesma reação (ex.: payroll, desemprego e salários juntos): veja "
    "`no_mesmo_horario`. `com_outro_evento_na_janela` conta divulgações com outro evento dos EUA de importância "
    "moderada ou alta dentro da janela. O calendário é só dos EUA: BCE, BoE, BoJ e OPEP não aparecem.",
    "`fora_das_contas` diz por que uma divulgação ficou de fora em cada janela: sem_medicao (não medida: fora do "
    "histórico M1 ou ainda não registrada), sem_referencia (sem negociação nas 2 h antes), referencia_antiga "
    "(mercado parado antes do evento), sem_negociacao (nada depois do evento), preco_antigo (o último preço da "
    "janela é de mais de 5 min antes do fim dela), a_confirmar (medição com falha esperando outro registro) e "
    "nao_confirmavel (saiu do histórico M1 antes de ser confirmada).",
    f"vezes_o_tipico = movimento / mediana dos movimentos do mesmo tamanho nas 2 h antes (só janelas até 30 min); "
    f"perto de 1 é ruído normal, {CLEAR_REACTION:g} ou mais (`reacao_clara`) é reação clara. Compare instrumentos "
    "por ele e pela variação em %, não por pontos.",
    f"Com menos de {SMALL_SAMPLE} divulgações a amostra é pequena (`amostra_pequena`): não generalize. Eventos mensais "
    "acumulam uma divulgação por mês.",
    "Os símbolos são CFDs da corretora (DXYm não é o índice ICE; JP225m é cotado em ienes). Não há instrumento de "
    "Treasury na conta: rendimentos (yields) não estão disponíveis.",
]


class ReacoesError(Exception):
    """Falha ao ler ou gravar o banco das reações."""


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _direction(row: dict[str, Any]) -> tuple[float | None, str]:
    """Surpresa e sentido de uma linha do calendário."""
    surprise = row.get("surpresa")
    if surprise is None:
        return None, "sem_realizado" if "unidade" in row else "sem_valores"
    if surprise.get("valor") is None:
        return None, "sem_previsao"
    value = surprise["valor"]
    return value, "acima" if value > 0 else "abaixo" if value < 0 else "igual"


def _fact(row: dict[str, Any]) -> dict[str, Any]:
    surprise, direction = _direction(row)
    return {
        "id": row["id"],
        "codigo": row["codigo"],
        "nome": row.get("evento"),
        "importancia": row["importancia"],
        "horario_utc": row["horario"]["utc"],
        "periodo": row.get("periodo_referencia"),
        "frequencia": row.get("frequencia"),
        "unidade": row.get("unidade"),
        "realizado": row.get("realizado"),
        "previsao": row.get("previsao"),
        "anterior": row.get("anterior"),
        "anterior_revisado": row.get("anterior_revisado"),
        "estimativa": row.get("estimativa"),
        "surpresa": surprise,
        "sentido": direction,
        "leitura_da_fonte": row.get("impacto_no_usd_segundo_a_fonte"),
    }


def _window_status(target: datetime, w: dict[str, Any]) -> str:
    if "preco" not in w:
        return w.get("situacao", "sem_negociacao")
    # Faltaram candles no fim da janela: o preço é de antes dela; muito antes, não é o movimento da janela.
    if w.get("ate") and target - _parse(w["ate"]) > _STALE:
        return "preco_antigo"
    return "ok"


def _measure_rows(
    moment: datetime, instrument: str, m: dict[str, Any], measured_at: str
) -> tuple[tuple, list[tuple], bool]:
    """Linhas de ``medicoes`` (sem ``confirmada``) e ``janelas`` e se a medição veio sem falhas."""
    moment_iso = tempo.iso_utc(moment)
    symbol = m["simbolo"]
    ref = m.get("referencia")
    extremes = m.get("extremos") or {}
    spread = m.get("spread") or {}
    candles = m.get("candles_m1") or {}
    measurement = (
        moment_iso, symbol, instrument, "medido" if ref else "sem_referencia",
        ref["preco"] if ref else None, ref["ate"] if ref else None, int(bool(ref and ref.get("antiga"))),
        (extremes.get("maxima") or {}).get("variacao_pct"), (extremes.get("minima") or {}).get("variacao_pct"),
        spread.get("antes_mediana_pontos"), spread.get("depois_max_pontos"),
        candles.get("recebidos"), candles.get("esperados"),
        json.dumps(m["avisos"], ensure_ascii=False) if m.get("avisos") else None, measured_at,
    )
    windows = []
    for w in m.get("janelas", []):
        status = _window_status(moment + timedelta(minutes=w["minutos"]), w)
        windows.append((
            moment_iso, symbol, w["minutos"], status, w.get("preco"), w.get("variacao"), w.get("variacao_pct"),
            w.get("pontos"), w.get("pips"), w.get("ate"), w.get("tipico_antes"), w.get("vezes_o_tipico"),
        ))
    clean = bool(ref) and not ref.get("antiga") and not candles and all(w[3] == "ok" for w in windows)
    return measurement, windows, clean


def _signature(measurement: Sequence[Any], windows: Sequence[Sequence[Any]]) -> tuple:
    """O que precisa se repetir entre dois registros para confirmar uma medição com falhas: o que as estatísticas
    usam (situação, referência, candles e janelas), sem spread e extremos."""
    return (
        tuple(measurement[3:7]), tuple(measurement[11:13]), tuple(sorted(tuple(w[2:]) for w in windows))
    )


def _summary(values: list[float]) -> float | None:
    return round(median(values), 3) if values else None


class ReactionStore:
    """Banco das reações em SQLite; cada chamada abre e fecha a própria conexão."""

    def __init__(
        self,
        path: Path,
        mt5: MT5Client,
        calendar: EconomicCalendar | None,
        instruments: Sequence[str],
        *,
        backup_dir: Path | None = None,
        now_utc: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        time_budget_s: float = TIME_BUDGET_S,
    ) -> None:
        self._path = Path(path)
        self._mt5 = mt5
        self._calendar = calendar
        self._instruments = tuple(instruments)
        self._backup_dir = Path(backup_dir) if backup_dir is not None else None
        self._now = now_utc or mt5.now_utc
        self._monotonic = monotonic
        self._budget = time_budget_s

    # ------------------------------------------------------------------ banco
    def _prepare(self, conn: sqlite3.Connection, write: bool) -> None:
        """Confere a versão antes de tudo; só quem grava cria o esquema (em autocommit)."""
        has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
        stored = None
        if has_meta:
            row = conn.execute("SELECT valor FROM meta WHERE chave = 'schema'").fetchone()
            stored = int(row["valor"]) if row else None
            if stored is not None and stored > SCHEMA_VERSION:
                raise ReacoesError(
                    f"O banco das reações ({self._path}) foi criado por uma versão mais nova do servidor "
                    f"(schema {stored}); atualize o trading-mcp."
                )
        if not write:
            if not has_meta:
                raise ReacoesError(_EMPTY)
            return
        conn.executescript(_SCHEMA)
        if stored != SCHEMA_VERSION:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))

    @contextmanager
    def _db(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        """Conexão com uma transação: BEGIN IMMEDIATE para gravar, BEGIN para ler. Leitura não cria o arquivo."""
        if not write and not self._path.is_file():
            raise ReacoesError(_EMPTY)
        try:
            if write:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise ReacoesError(f"Não foi possível abrir o banco das reações ({self._path}): {exc}") from exc
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
            raise ReacoesError(f"Erro no banco das reações ({self._path}): {exc}") from exc
        finally:
            conn.close()

    def _calendar_until(self) -> datetime | None:
        """Até quando o calendário já foi guardado (registro anterior)."""
        if not self._path.is_file():
            return None
        with self._db(write=False) as conn:
            row = conn.execute("SELECT valor FROM meta WHERE chave = 'calendario_ate'").fetchone()
        return _parse(json.loads(row["valor"])) if row else None

    # ------------------------------------------------------------------ registro
    def _save_facts(self, facts: list[dict[str, Any]], now_iso: str, until: datetime) -> dict[str, Any]:
        new = filled = changed = touched = 0
        changes: list[str] = []
        with self._db(write=True) as conn:
            for fact in facts:
                old = conn.execute("SELECT * FROM eventos WHERE id = ?", (fact["id"],)).fetchone()
                if old is None:
                    conn.execute(
                        f"INSERT INTO eventos ({', '.join(('id',) + _FACT_FIELDS)}, registrado_utc, atualizado_utc) "
                        f"VALUES ({', '.join('?' * (len(_FACT_FIELDS) + 3))})",
                        (fact["id"], *(fact[k] for k in _FACT_FIELDS), now_iso, now_iso),
                    )
                    new += 1
                    continue
                # Valor que some do calendário não apaga o guardado (avisa).
                vanished = [k for k in _FACT_FIELDS if old[k] is not None and fact[k] is None and k not in _DERIVED]
                if vanished:
                    changes.append(
                        f"{fact['codigo']} de {fact['horario_utc']}: {', '.join(vanished)} sumiu do calendário "
                        "(mantido o valor guardado)"
                    )
                    restored = vanished + (list(_DERIVED) if {"realizado", "previsao"} & set(vanished) else [])
                    for k in restored:
                        fact[k] = old[k]
                diff = [k for k in _FACT_FIELDS if old[k] != fact[k]]
                if not diff:
                    changed += bool(vanished)
                    continue
                touched += 1
                conn.execute(
                    f"UPDATE eventos SET {', '.join(f'{k} = ?' for k in diff)}, atualizado_utc = ? WHERE id = ?",
                    (*(fact[k] for k in diff), now_iso, fact["id"]),
                )
                if old["realizado"] is None and fact["realizado"] is not None:
                    filled += 1
                # Valor que aparece (nulo → valor: realizado, leitura da fonte, anterior revisado) é o esperado;
                # avisa quando um valor já guardado muda.
                moved = [k for k in diff if old[k] is not None and k not in _DERIVED]
                if moved or vanished:
                    changed += 1
                if moved:
                    changes.append(
                        f"{fact['codigo']} de {fact['horario_utc']}: "
                        + ", ".join(f"{k} {old[k]!r} → {fact[k]!r}" for k in moved)
                    )
            row = conn.execute("SELECT valor FROM meta WHERE chave = 'calendario_ate'").fetchone()
            previous = _parse(json.loads(row["valor"])) if row else None
            if previous is None or until > previous:
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('calendario_ate', ?)", (json.dumps(tempo.iso_utc(until)),))
        return {
            "novas": new, "realizado_preenchido": filled, "alteradas": changed, "mudancas": changes, "tocadas": touched,
        }

    def _pending(self, symbols: dict[str, str], until: datetime) -> list[tuple[str, list[str]]]:
        """Horários já terminados e os símbolos sem medição confirmada em cada um (do mais antigo ao mais novo)."""
        if not self._path.is_file():
            return []
        with self._db(write=False) as conn:
            moments = [r["horario_utc"] for r in conn.execute(
                "SELECT DISTINCT horario_utc FROM eventos WHERE horario_utc <= ? ORDER BY horario_utc",
                (tempo.iso_utc(until),),
            )]
            done: dict[str, set[str]] = {}
            for r in conn.execute("SELECT horario_utc, simbolo FROM medicoes WHERE confirmada != 0"):
                done.setdefault(r["horario_utc"], set()).add(r["simbolo"])
        out = []
        for moment in moments:
            missing = [s for s in symbols if s not in done.get(moment, set())]
            if missing:
                out.append((moment, missing))
        return out

    def _save_measurements(self, batch: list[tuple[tuple, list[tuple], bool]], now: datetime) -> int:
        """Grava um horário; medição com falha só fica confirmada se repetir uma de pelo menos 10 min antes."""
        provisional = 0
        with self._db(write=True) as conn:
            for measurement, windows, clean in batch:
                key = (measurement[0], measurement[1])
                confirmed = clean
                if not clean:
                    old = conn.execute("SELECT * FROM medicoes WHERE horario_utc = ? AND simbolo = ?", key).fetchone()
                    if old is not None and now - _parse(old["medido_utc"]) < _CONFIRM_AFTER:
                        measurement = (*measurement[:14], old["medido_utc"])  # conta a partir da primeira
                        old = None
                    if old is not None:
                        old_windows = conn.execute(
                            "SELECT * FROM janelas WHERE horario_utc = ? AND simbolo = ?", key
                        ).fetchall()
                        confirmed = _signature(tuple(old), [tuple(w) for w in old_windows]) == _signature(
                            measurement, windows
                        )
                conn.execute("DELETE FROM janelas WHERE horario_utc = ? AND simbolo = ?", key)
                conn.execute(f"INSERT OR REPLACE INTO medicoes VALUES ({', '.join('?' * 16)})", (*measurement, int(confirmed)))
                conn.executemany(f"INSERT INTO janelas VALUES ({', '.join('?' * 12)})", windows)
                provisional += not confirmed
        return provisional

    def _give_up(self, pairs: list[tuple[str, str]]) -> int:
        """Medições a confirmar cujo horário saiu do histórico M1: não confirmáveis (fora das estatísticas)."""
        if not pairs:
            return 0
        with self._db(write=True) as conn:
            return sum(
                conn.execute(
                    "UPDATE medicoes SET confirmada = ? WHERE horario_utc = ? AND simbolo = ? AND confirmada = ?",
                    (_UNCONFIRMABLE, moment, symbol, _UNCONFIRMED),
                ).rowcount
                for moment, symbol in pairs
            )

    def _backup(self) -> tuple[str | None, str | None]:
        """Cópia do banco na pasta de exportação (fica no OneDrive): as medições antigas não podem ser refeitas."""
        if self._backup_dir is None:
            return None, None
        target = self._backup_dir / BACKUP_NAME
        partial = target.with_name(target.name + ".tmp")
        try:
            self._backup_dir.mkdir(parents=True, exist_ok=True)
            partial.unlink(missing_ok=True)
            # VACUUM INTO: cópia consistente que respeita o tempo de espera do banco (sem transação aberta).
            with closing(sqlite3.connect(self._path, timeout=10, isolation_level=None)) as source:
                source.execute("VACUUM INTO ?", (str(partial),))
            os.replace(partial, target)
        except (OSError, sqlite3.Error) as exc:
            return None, f"Cópia de segurança não gravada ({target}): {exc}"
        return str(target), None

    def register(self) -> dict[str, Any]:
        """Guarda as divulgações que o calendário cobre e mede as reações que faltam (dentro do tempo)."""
        if self._calendar is None:
            raise ValueError("Calendário não configurado: não há divulgações para guardar.")
        started = self._monotonic()
        now = self._now()
        now_iso = tempo.iso_utc(now)
        warnings: list[str] = []
        errors: list[str] = []
        previous_until = self._calendar_until()

        # 1) Fatos: tudo o que o arquivo do calendário cobre, gravado de uma vez. Sem calendário, as medições
        # pendentes continuam.
        facts: dict[str, Any] = {"no_calendario": 0, "novas": 0, "realizado_preenchido": 0, "alteradas": 0}
        try:
            first_covered, _ = self._calendar.coverage()
            data = self._calendar.query(start=first_covered, end=now, min_importance=MIN_IMPORTANCE, limit=100_000)
        except CalendarError as exc:
            if not self._path.is_file():
                raise  # nada guardado ainda: sem calendário não há o que medir
            warnings.append(f"Calendário indisponível ({exc}): nenhuma divulgação nova guardada agora.")
        else:
            rows = [
                r for r in data["eventos"]
                if r.get("horario_tipo") == "exato" and r.get("situacao") != "feriado" and "horario" in r
            ]
            generated = _parse(data["arquivo_atualizado"]["utc"])
            saved = self._save_facts([_fact(r) for r in rows], now_iso, min(now, generated))
            facts = {"calendario_cobre_desde": tempo.iso_utc(first_covered), "no_calendario": len(rows), **saved}
            if previous_until is not None and first_covered > previous_until:
                warnings.append(
                    f"Lacuna: divulgações entre {tempo.iso_utc(previous_until)} e {tempo.iso_utc(first_covered)} não "
                    "foram guardadas e o calendário já não as cobre. Rode `reacoes_registrar` com mais frequência ou "
                    "aumente InpDaysBack no serviço TradingMcpCalendar."
                )
            if data.get("estado") != "atual":
                warnings.append(
                    f"Calendário {data.get('estado')} (arquivo de {data['arquivo_atualizado']['utc']}): realizados "
                    "recentes podem faltar; rode de novo com o serviço funcionando."
                )
            for change in facts.pop("mudancas")[:5]:
                warnings.append(f"Valor mudou no calendário depois de guardado: {change}.")
        touched = facts.pop("tocadas", 0)
        # Fora do histórico é avisado para as divulgações que o calendário ainda cobre (o que se perde agora).
        report_from = _parse(facts["calendario_cobre_desde"]) if "calendario_cobre_desde" in facts else previous_until

        # 2) Medições que faltam, do horário mais antigo (o primeiro a sair do histórico M1) ao mais novo.
        symbols: dict[str, str] = {}
        for name in self._instruments:
            try:
                symbols[self._mt5.symbol_spec(name)["simbolo"]] = name
            except (MT5Error, ValueError) as exc:
                errors.append(f"{name}: {exc}")
        pending = self._pending(symbols, now - timedelta(minutes=max(WINDOWS)) - _SETTLE)
        measured = {"horarios_medidos": 0, "gravadas": 0, "sem_referencia": 0, "a_confirmar": 0}
        stuck: list[tuple[str, str]] = []
        out_of_history: Counter[str] = Counter()
        history_start: dict[str, datetime] = {}
        left = 0
        connected = True
        if pending:
            try:
                connected = self._mt5.terminal()["conectado"]
            except MT5Error as exc:
                connected = False
                errors.append(f"Terminal: {exc}")
        if pending and not connected:
            warnings.append(
                "Terminal sem conexão com a corretora: nenhuma reação foi medida agora (candles que faltam no "
                "terminal pareceriam mercado fechado). Rode de novo com o terminal conectado."
            )
            left = len(pending)
        elif pending:
            for symbol in symbols:
                try:
                    history_start[symbol] = self._mt5.oldest_bar(symbol, "M1")
                except MT5Error as exc:
                    errors.append(f"{symbol}: início do histórico M1 não lido ({exc}); medições adiadas")
            for i, (moment_iso, missing) in enumerate(pending):
                moment = _parse(moment_iso)
                report = report_from is None or moment >= report_from
                batch = []
                stopped = False
                for symbol in missing:
                    if self._monotonic() - started > self._budget:
                        stopped = True
                        break
                    if symbol not in history_start:
                        continue  # erro já registrado
                    if history_start[symbol] > moment - _LOOKBACK:
                        out_of_history[symbol] += report
                        stuck.append((moment_iso, symbol))
                        continue
                    try:
                        m = reacao.measure(self._mt5, symbol, moment, WINDOWS, self._now())
                    except (MT5Error, ValueError) as exc:
                        errors.append(f"{symbol} em {moment_iso}: {exc}")
                        continue
                    if m.get("fora_do_historico"):
                        out_of_history[symbol] += report
                        stuck.append((moment_iso, symbol))
                        continue
                    if m.get("sem_conexao"):
                        errors.append(f"{symbol} em {moment_iso}: terminal sem conexão durante a medição")
                        continue
                    if any(w.get("situacao") == "pendente" for w in m.get("janelas", [])):
                        continue  # relógio do terminal atrás do servidor: mede na próxima
                    batch.append(_measure_rows(moment, symbols[symbol], m, now_iso))
                    if m.get("referencia") is None:
                        measured["sem_referencia"] += 1
                if batch:
                    measured["a_confirmar"] += self._save_measurements(batch, now)
                    measured["horarios_medidos"] += 1
                    measured["gravadas"] += len(batch)
                if stopped:
                    left = len(pending) - i
                    break
        given_up = self._give_up(stuck)
        if given_up:
            measured["nao_confirmaveis"] = given_up
            warnings.append(
                f"{given_up} medição(ões) a confirmar saíram do histórico M1 do terminal: ficam como não confirmáveis "
                "(fora das estatísticas)."
            )
        if left and connected:
            warnings.append(
                f"Faltaram {left} horário(s) de divulgação para medir: rode `reacoes_registrar` de novo para continuar "
                "(não duplica)."
            )
        if errors:
            warnings.append(f"{len(errors)} erro(s); as medições afetadas serão tentadas de novo na próxima vez.")

        with self._db(write=False) as conn:
            totals = self._totals(conn)
        result: dict[str, Any] = {
            "divulgacoes": facts,
            "medicoes": {**measured, "horarios_faltando": left},
            "banco": totals,
        }
        if any(out_of_history.values()):
            result["medicoes"]["fora_do_historico_m1"] = {
                s: {"medicoes": n, "historico_m1_desde": tempo.iso_utc(history_start[s])}
                for s, n in sorted(out_of_history.items()) if n
            }
        if facts["novas"] or touched or measured["gravadas"] or given_up:
            copy, problem = self._backup()
            if copy:
                result["copia_de_seguranca"] = copy
            if problem:
                warnings.append(problem)
        result["avisos"] = warnings
        if errors:
            result["erros"] = errors[:10]
        result["observacoes"] = NOTES_REGISTER
        return result

    def _totals(self, conn: sqlite3.Connection) -> dict[str, Any]:
        facts = conn.execute("SELECT COUNT(*) AS n, MIN(horario_utc) AS de, MAX(horario_utc) AS ate FROM eventos").fetchone()
        measurements = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(confirmada = 0), 0) AS a_confirmar, "
            "COALESCE(SUM(confirmada = 2), 0) AS nao_confirmaveis FROM medicoes"
        ).fetchone()
        return {
            "arquivo": str(self._path),
            "divulgacoes": facts["n"],
            "de": facts["de"],
            "ate": facts["ate"],
            "medicoes": measurements["n"],
            "medicoes_a_confirmar": measurements["a_confirmar"],
            "medicoes_nao_confirmaveis": measurements["nao_confirmaveis"],
        }

    @staticmethod
    def _codes(conn: sqlite3.Connection, cutoff: str) -> list[dict[str, Any]]:
        """Eventos guardados (por código) com o nome e a importância da divulgação mais recente."""
        codes: dict[str, dict[str, Any]] = {}
        for r in conn.execute(
            "SELECT codigo, nome, importancia, horario_utc FROM eventos WHERE horario_utc >= ? ORDER BY horario_utc",
            (cutoff,),
        ):
            item = codes.setdefault(r["codigo"], {"codigo": r["codigo"], "n": 0, "de": r["horario_utc"]})
            item.update(nome=r["nome"], importancia=r["importancia"], ate=r["horario_utc"], n=item["n"] + 1)
        return sorted(codes.values(), key=lambda c: (-_RANK.get(c["importancia"], 0), -c["n"], c["codigo"]))

    # ------------------------------------------------------------------ estatísticas
    def stats(
        self,
        search: str = "",
        symbols: Sequence[str] | None = None,
        windows: Sequence[int] = DEFAULT_STATS_WINDOWS,
        days: float | None = None,
    ) -> dict[str, Any]:
        """Por evento: divulgações, surpresas e o movimento de cada instrumento, agrupado pelo sentido da surpresa."""
        windows = sorted({int(w) for w in windows})
        invalid = [w for w in windows if w not in WINDOWS]
        if not windows or invalid:
            raise ValueError(f"Janelas guardadas: {', '.join(map(str, WINDOWS))} minutos.")
        if days is not None and not 0 < days <= MAX_STATS_DAYS:
            raise ValueError(f"dias deve estar entre 0 e {MAX_STATS_DAYS} (vazio = todas as divulgações guardadas).")
        cutoff = tempo.iso_utc(self._now() - timedelta(days=days)) if days is not None else ""
        wanted = [s.strip().upper() for s in (symbols or self._instruments) if s.strip()]
        with self._db(write=False) as conn:
            if not search.strip():
                return self._inventory(conn, cutoff)
            return self._event_stats(conn, search.strip(), wanted, windows, cutoff, days)

    def _inventory(self, conn: sqlite3.Connection, cutoff: str) -> dict[str, Any]:
        measured: Counter[str] = Counter(
            r["codigo"] for r in conn.execute(
                "SELECT codigo FROM eventos e WHERE horario_utc >= ? AND EXISTS (SELECT 1 FROM medicoes m WHERE "
                "m.horario_utc = e.horario_utc AND m.situacao = 'medido' AND m.confirmada = 1)",
                (cutoff,),
            )
        )
        events = [
            {
                "codigo": c["codigo"],
                "descricao": KEY_EVENTS.get(c["codigo"], c["nome"]),
                "importancia": c["importancia"],
                "divulgacoes": c["n"],
                "com_reacao_medida": measured[c["codigo"]],
                "de": c["de"][:10],
                "ate": c["ate"][:10],
            }
            for c in self._codes(conn, cutoff)
        ]
        coverage = [
            {"simbolo": r["simbolo"], "instrumento": r["instrumento"], "horarios_medidos": r["n"],
             "de": r["de"], "ate": r["ate"]}
            for r in conn.execute(
                "SELECT simbolo, MAX(instrumento) AS instrumento, COUNT(*) AS n, MIN(horario_utc) AS de, "
                "MAX(horario_utc) AS ate FROM medicoes WHERE horario_utc >= ? AND situacao = 'medido' AND "
                "confirmada = 1 AND horario_utc IN (SELECT horario_utc FROM eventos) GROUP BY simbolo ORDER BY simbolo",
                (cutoff,),
            )
        ]
        notes = ["Informe `evento` (ex.: claims, cpi, nfp ou o código) para ver as reações agrupadas pela surpresa."]
        if len(events) > MAX_INVENTORY:
            notes.append(f"Mostrando {MAX_INVENTORY} de {len(events)} eventos (os mais importantes e frequentes).")
        return {
            "banco": self._totals(conn),
            "medicoes_por_simbolo": coverage,
            "eventos": events[:MAX_INVENTORY],
            "observacoes": notes,
        }

    def _event_stats(
        self, conn: sqlite3.Connection, search: str, wanted: list[str], windows: list[int], cutoff: str,
        days: float | None,
    ) -> dict[str, Any]:
        codes = self._codes(conn, cutoff)
        exact = [c for c in codes if c["codigo"].lower() == search.lower()]
        found = exact or [c for c in codes if matches_search(search, c["codigo"], c["nome"])]
        if not found:
            raise ValueError(
                f"Nenhuma divulgação '{search}' guardada"
                + (f" nos últimos {days:g} dias" if days else "")
                + ". Rode `reacoes_registrar` ou chame sem `evento` para ver o que está guardado."
            )
        chosen, others = found[:MAX_CODES], found[MAX_CODES:]

        # Na ordem pedida (padrão: a dos instrumentos configurados), pelo nome configurado ou pelo da conta.
        by_name: dict[str, str] = {}
        for r in conn.execute("SELECT DISTINCT simbolo, instrumento FROM medicoes"):
            by_name.setdefault(r["instrumento"].upper(), r["simbolo"])
            by_name.setdefault(r["simbolo"].upper(), r["simbolo"])
        selected = list(dict.fromkeys(by_name[s] for s in wanted if s in by_name))
        missing_symbols = [s for s in wanted if s not in by_name]

        events = [self._code_stats(conn, c, selected, windows, cutoff) for c in chosen]
        notes = list(NOTES_STATS)
        if any("estimativas" in e for e in events):
            notes.append(
                "`estimativas`: o evento tem primeira estimativa e revisões do mesmo período (ex.: PIB); as estatísticas "
                "juntam as duas. Veja `ultimas_divulgacoes` para separar."
            )
        if others:
            notes.append(
                "Outros eventos que também casam com a busca (informe o código para vê-los): "
                + ", ".join(f"{c['codigo']} ({c['n']})" for c in others)
                + "."
            )
        if missing_symbols:
            notes.append(f"Sem medições guardadas para: {', '.join(missing_symbols)}.")
        return {
            "filtro": {"evento": search, "simbolos": wanted, "janelas_min": windows, "dias": days},
            "banco": self._totals(conn),
            "eventos": events,
            "observacoes": notes,
        }

    def _code_stats(
        self, conn: sqlite3.Connection, code_row: dict[str, Any], symbols: list[str], windows: list[int], cutoff: str
    ) -> dict[str, Any]:
        code = code_row["codigo"]
        facts = [dict(r) for r in conn.execute(
            "SELECT * FROM eventos WHERE codigo = ? AND horario_utc >= ? ORDER BY horario_utc", (code, cutoff)
        )]
        moments = sorted({f["horario_utc"] for f in facts})
        marks = ", ".join("?" * len(moments))
        # Outros eventos guardados no mesmo horário e logo depois (até a maior janela guardada).
        last = tempo.iso_utc(_parse(moments[-1]) + timedelta(minutes=max(WINDOWS)))
        around = conn.execute(
            "SELECT horario_utc, codigo FROM eventos WHERE horario_utc >= ? AND horario_utc <= ?", (moments[0], last)
        ).fetchall()
        same: dict[str, set[str]] = {}
        later: dict[str, list[float]] = {}
        moment_dt = {m: _parse(m) for m in moments}
        for r in around:
            if r["horario_utc"] in moment_dt and r["codigo"] != code:
                same.setdefault(r["horario_utc"], set()).add(r["codigo"])
            when = _parse(r["horario_utc"])
            for m, dt in moment_dt.items():
                gap = (when - dt).total_seconds() / 60
                if 0 < gap < max(WINDOWS):
                    later.setdefault(m, []).append(gap)

        measurements = {
            (r["horario_utc"], r["simbolo"]): dict(r)
            for r in conn.execute(f"SELECT * FROM medicoes WHERE horario_utc IN ({marks})", moments)
        }
        moves: dict[tuple[str, str, int], dict] = {
            (r["horario_utc"], r["simbolo"], r["minutos"]): dict(r)
            for r in conn.execute(f"SELECT * FROM janelas WHERE horario_utc IN ({marks})", moments)
        }

        def usable(moment: str, symbol: str, minutes: int) -> tuple[dict | None, str]:
            """A janela e, se não servir para as contas, o motivo."""
            m = measurements.get((moment, symbol))
            if m is None:
                return None, "sem_medicao"
            if m["confirmada"] == _UNCONFIRMED:
                return None, "a_confirmar"
            if m["confirmada"] == _UNCONFIRMABLE:
                return None, "nao_confirmavel"
            if m["situacao"] != "medido":
                return None, m["situacao"]
            if m["referencia_antiga"]:
                return None, "referencia_antiga"
            w = moves.get((moment, symbol, minutes))
            if w is None:
                return None, "sem_medicao"
            return (w, "ok") if w["situacao"] == "ok" else (None, w["situacao"])

        reactions = []
        for symbol in symbols:
            per_window: dict[str, Any] = {}
            for minutes in windows:
                pairs: list[tuple[dict, dict]] = []
                left_out: Counter[str] = Counter()
                for f in facts:
                    w, reason = usable(f["horario_utc"], symbol, minutes)
                    if w is None:
                        left_out[reason] += 1
                    else:
                        pairs.append((f, w))
                if not pairs:
                    continue
                pcts = [w["variacao_pct"] for _, w in pairs if w["variacao_pct"] is not None]
                typical = [w["vezes_o_tipico"] for _, w in pairs if w["vezes_o_tipico"] is not None]
                entry: dict[str, Any] = {
                    "todas": {
                        "n": len(pairs),
                        "mediana_abs_pct": _summary([abs(p) for p in pcts]),
                        **(
                            {"mediana_vezes_o_tipico": round(median(typical), 1),
                             "reacao_clara": sum(t >= CLEAR_REACTION for t in typical)}
                            if typical else {}
                        ),
                    },
                }
                for direction, label in _DIRECTIONS:
                    group = [w["variacao_pct"] for f, w in pairs if f["sentido"] == direction and w["variacao_pct"] is not None]
                    if group:
                        entry[label] = {
                            "n": len(group),
                            "mediana_pct": _summary(group),
                            "subiu": sum(p > 0 for p in group),
                            "caiu": sum(p < 0 for p in group),
                        }
                mixed = sum(any(g < minutes for g in later.get(f["horario_utc"], [])) for f, _ in pairs)
                if mixed:
                    entry["com_outro_evento_na_janela"] = mixed
                if left_out:
                    entry["fora_das_contas"] = dict(sorted(left_out.items()))
                per_window[str(minutes)] = entry
            if per_window:
                reactions.append({"simbolo": symbol, "janelas": per_window})

        directions = Counter(f["sentido"] for f in facts)
        same_counter = Counter(c for m in moments for c in same.get(m, ()))
        late = sum(_parse(f["registrado_utc"]) - _parse(f["horario_utc"]) > _LATE for f in facts)
        estimates = Counter(
            "primeira" if (f["estimativa"] or "").startswith("primeira")
            else "revisada" if f["estimativa"] else "unica"
            for f in facts
        )
        recent = []
        for f in facts[-RECENT_RELEASES:][::-1]:
            item: dict[str, Any] = {
                "horario_utc": f["horario_utc"],
                "periodo": f["periodo"],
                "realizado": f["realizado"],
                "previsao": f["previsao"],
                "surpresa": f["surpresa"],
                "sentido": f["sentido"],
            }
            if f["estimativa"]:
                item["estimativa"] = f["estimativa"]
            moved = {
                s: [w["variacao_pct"] if (w := usable(f["horario_utc"], s, m)[0]) is not None else None for m in windows]
                for s in symbols
            }
            item["variacao_pct"] = {s: v for s, v in moved.items() if any(x is not None for x in v)}
            recent.append(item)

        out: dict[str, Any] = {
            "codigo": code,
            "descricao": KEY_EVENTS.get(code, code_row["nome"]),
            "importancia": code_row["importancia"],
            "unidade": next((f["unidade"] for f in reversed(facts) if f["unidade"]), None),
            "divulgacoes": len(facts),
            "de": facts[0]["horario_utc"][:10],
            "ate": facts[-1]["horario_utc"][:10],
            "amostra_pequena": len(facts) < SMALL_SAMPLE,
            "surpresas": {k: directions[k] for k in
                          ("acima", "abaixo", "igual", "sem_previsao", "sem_realizado", "sem_valores") if directions[k]},
        }
        if estimates["primeira"] or estimates["revisada"]:
            out["estimativas"] = {k: v for k, v in estimates.items() if v}
        if same_counter:
            out["no_mesmo_horario"] = {
                "divulgacoes_com_outros_eventos": sum(1 for m in moments if same.get(m)),
                "eventos": dict(same_counter.most_common(6)),
            }
        if late:
            out["registradas_depois_do_dia"] = late  # previsão como o calendário mostrava no registro
        out["reacoes"] = reactions
        out["ultimas_divulgacoes"] = {"janelas_min": windows, "itens": recent}
        return out
