"""Alertas de preço nos níveis SMC (etapa G4): o painel avisa quando o preço toca um nível escolhido.

Um alerta guarda o preço de um alvo de liquidez da barra lateral do painel (máximas/mínimas de sessão, dia e semana,
liquidez igual, topos/fundos diários, início de OB/FVG) e o lado em que o preço estava na criação. Dispara uma vez,
quando o bid (o mesmo preço dos candles do gráfico) chega ao nível, e avisa numa notificação do Windows. O nível fica
fixo: se o OB for mitigado ou a máxima da sessão mudar depois, o alerta continua no preço da criação. Nada aqui
envia, altera ou cancela ordens.

Quem vigia é o painel, enquanto a janela dele estiver aberta (o navegador pode estar fechado): a cada segundo, os
ticks desde a última leitura, com o horário do último tick (do servidor) como "agora". Depois de um tempo sem vigiar
(painel fechado, PC suspenso), os candles M1 do intervalo e os ticks das pontas: um toque com o painel fechado
dispara quando ele abre, com o horário do candle. O terminal primeiro devolve o histórico guardado e só depois
sincroniza, então o cursor só passa do intervalo quando os candles chegam até o fim dele (ou depois de SYNC_WAIT_S).
O bid é comparado com meio ponto de folga: no forex, um bid igual ao nível chega com erro de float.

Banco: mesmo padrão dos outros (uma conexão por chamada; gravação com BEGIN IMMEDIATE). O disparo só grava se o
alerta ainda estiver ativo, então dois painéis abertos não avisam duas vezes.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np
import pandas as pd

from trading_mcp import tempo
from trading_mcp.mt5_client import MT5Client, MT5Error

log = logging.getLogger(__name__)

UTC = timezone.utc
SCHEMA_VERSION = 1
MAX_ACTIVE = 50
MAX_TITLE = 120
CHECK_INTERVAL_S = 1.0
# Intervalo lido só com ticks; acima disso (painel fechado), candles M1 no meio e ticks nas pontas.
TICKS_ONLY = timedelta(minutes=10)
# Rodadas mais espaçadas que isto (PC suspenso, MT5 travado) também recuperam o intervalo pelos candles M1.
WAKE_GAP_S = 30.0
# Ao recuperar um intervalo, quanto esperar o terminal sincronizar os candles M1 até o corte; depois, o que veio vale
# (o mercado fechou no fim do intervalo, ou o ativo não negociou no último minuto).
SYNC_WAIT_S = 20.0
# Até onde cada alerta já foi conferido vai para o banco a cada tanto (ao reabrir, o painel continua dali).
SAVE_PROGRESS_S = 30.0
_ERROR_LOG_S = 60.0  # o mesmo erro de um símbolo vai ao log no máximo uma vez por minuto
_EPOCH = pd.Timestamp(0, tz="UTC")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alertas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    simbolo TEXT NOT NULL,
    preco REAL NOT NULL,
    lado TEXT NOT NULL CHECK (lado IN ('acima', 'abaixo')),
    titulo TEXT NOT NULL,
    digitos INTEGER NOT NULL,
    criado_utc TEXT NOT NULL,
    preco_criacao REAL NOT NULL,
    vigiado_ate_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ativo', 'disparado')),
    disparado_utc TEXT,
    preco_disparo REAL,
    disparo_por TEXT
);
CREATE INDEX IF NOT EXISTS alertas_status ON alertas (status, simbolo);
"""


class AlertasError(Exception):
    """Banco dos alertas ilegível ou não gravável."""


def _ms(moment: datetime) -> int:
    return int(round(moment.timestamp() * 1000))


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


# ---------------------------------------------------------------------- banco
class AlertStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _db(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        try:
            if write:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise AlertasError(f"Não foi possível abrir o banco dos alertas ({self._path}): {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
            if has_meta:
                row = conn.execute("SELECT valor FROM meta WHERE chave = 'schema'").fetchone()
                if row and int(row["valor"]) > SCHEMA_VERSION:
                    raise AlertasError(f"O banco dos alertas ({self._path}) é de uma versão mais nova do painel.")
            if write or not has_meta:
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
            raise AlertasError(f"Erro no banco dos alertas ({self._path}): {exc}") from exc
        finally:
            conn.close()

    def _read(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        if not self._path.is_file():  # sem alertas ainda: ler não cria o arquivo
            return []
        with self._db(write=False) as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def all(self) -> list[dict[str, Any]]:
        """Ativos (do mais novo ao mais antigo) e depois os disparados (do último disparo ao primeiro)."""
        return self._read(
            "SELECT * FROM alertas ORDER BY status = 'disparado', "
            "CASE status WHEN 'ativo' THEN criado_utc ELSE disparado_utc END DESC, id DESC"
        )

    def active(self) -> list[dict[str, Any]]:
        return self._read("SELECT * FROM alertas WHERE status = 'ativo' ORDER BY id")

    def create(self, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Grava o alerta; se já houver um ativo no mesmo símbolo e preço, devolve esse. (alerta, criado agora?)"""
        columns = ("simbolo", "preco", "lado", "titulo", "digitos", "criado_utc", "preco_criacao", "vigiado_ate_ms")
        with self._db(write=True) as conn:
            same = conn.execute("SELECT * FROM alertas WHERE status = 'ativo' AND simbolo = ? AND preco = ?",
                                (item["simbolo"], item["preco"])).fetchone()
            if same is not None:
                return dict(same), False
            active = conn.execute("SELECT COUNT(*) FROM alertas WHERE status = 'ativo'").fetchone()[0]
            if active >= MAX_ACTIVE:
                raise ValueError(f"Já há {active} alertas ativos (máximo {MAX_ACTIVE}). Remova algum antes.")
            cursor = conn.execute(
                f"INSERT INTO alertas ({', '.join(columns)}, status) VALUES ({', '.join('?' * len(columns))}, 'ativo')",
                tuple(item[c] for c in columns),
            )
            row = conn.execute("SELECT * FROM alertas WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row), True

    def fire(self, alert_id: int, when: datetime, price: float | None, by: str) -> bool:
        """Marca como disparado; False se o alerta já não estava ativo (removido ou disparado por outro painel)."""
        with self._db(write=True) as conn:
            done = conn.execute(
                "UPDATE alertas SET status = 'disparado', disparado_utc = ?, preco_disparo = ?, disparo_por = ? "
                "WHERE id = ? AND status = 'ativo'",
                (tempo.iso_utc(when), price, by, alert_id),
            )
            return done.rowcount == 1

    def save_progress(self, cursors: dict[int, int]) -> None:
        if not cursors:
            return
        with self._db(write=True) as conn:
            conn.executemany(
                "UPDATE alertas SET vigiado_ate_ms = MAX(vigiado_ate_ms, ?) WHERE id = ? AND status = 'ativo'",
                [(ms, alert_id) for alert_id, ms in cursors.items()],
            )

    def delete(self, alert_id: int) -> bool:
        if not self._path.is_file():
            return False
        with self._db(write=True) as conn:
            return conn.execute("DELETE FROM alertas WHERE id = ?", (alert_id,)).rowcount == 1

    def clear_fired(self) -> int:
        if not self._path.is_file():
            return 0
        with self._db(write=True) as conn:
            return conn.execute("DELETE FROM alertas WHERE status = 'disparado'").rowcount


def public(alert: dict[str, Any]) -> dict[str, Any]:
    """O alerta como o painel mostra: horários em UTC, São Paulo e Nova York."""
    return {
        "id": alert["id"],
        "simbolo": alert["simbolo"],
        "preco": alert["preco"],
        "lado": alert["lado"],
        "titulo": alert["titulo"],
        "digitos": alert["digitos"],
        "status": alert["status"],
        "criado": tempo.exibicao(_parse_utc(alert["criado_utc"])),
        "preco_criacao": alert["preco_criacao"],
        "disparado": tempo.exibicao(_parse_utc(alert["disparado_utc"])) if alert["disparado_utc"] else None,
        "preco_disparo": alert["preco_disparo"],
        "disparo_por": alert["disparo_por"],
    }


def create_alert(store: AlertStore, mt5: MT5Client, symbol: str, price: Any, title: Any) -> tuple[dict[str, Any], bool]:
    """Cria o alerta no preço informado; o lado (acima/abaixo) sai do bid de agora."""
    if isinstance(price, bool) or not isinstance(price, (int, float, str)):
        raise ValueError("`preco` precisa ser um número.")
    try:
        level = float(price)
    except ValueError:
        raise ValueError("`preco` precisa ser um número.") from None
    if not math.isfinite(level) or level <= 0:
        raise ValueError("`preco` precisa ser um número maior que zero.")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("Informe o símbolo (ex.: USTEC).")
    title = " ".join(str(title).split())[:MAX_TITLE] if isinstance(title, (str, int, float)) else ""
    spec = mt5.symbol_spec(symbol)
    resolved, digits = spec["simbolo"], int(spec["digitos"])
    tick = mt5.last_tick(resolved)
    if tick is None:
        raise MT5Error(f"Sem cotação de {resolved}: não dá para saber de que lado do nível o preço está.")
    when, bid = tick
    level = round(level, digits)
    if level == round(bid, digits):
        raise ValueError("O preço já está no nível: escolha outro.")
    return store.create({
        "simbolo": resolved, "preco": level, "lado": "acima" if level > bid else "abaixo", "titulo": title or "Nível",
        "digitos": digits, "criado_utc": tempo.iso_utc(mt5.now_utc()), "preco_criacao": round(bid, digits),
        "vigiado_ate_ms": _ms(when),
    })


# ---------------------------------------------------------------------- caminho do preço
@dataclass
class _Path:
    """Preços (bid) de um intervalo, em ordem: ticks e candles M1 não se sobrepõem no tempo."""

    tick_ms: np.ndarray
    tick_bid: np.ndarray
    bar_ms: np.ndarray  # abertura dos candles M1
    bar_high: np.ndarray
    bar_low: np.ndarray
    until_ms: int | None  # até onde o intervalo está coberto sem buraco (o próximo começa dali)
    complete: bool  # False: o MT5 ainda não devolveu os candles M1 até o corte (histórico sincronizando)

    def first_touch(self, level: float, above: bool, since_ms: int, eps: float) -> tuple[int, float | None, str] | None:
        """(horário em ms, bid do tick ou None no candle, 'tick' ou 'candle_m1') do primeiro toque desde ``since_ms``.

        ``eps`` (meio ponto) absorve o erro do float: no forex, um bid igual ao nível chega como 1.3208199999999999.
        """
        hits = []
        ticks = (self.tick_ms >= since_ms) & ((self.tick_bid >= level - eps) if above else (self.tick_bid <= level + eps))
        if ticks.any():
            i = int(np.argmax(ticks))
            hits.append((int(self.tick_ms[i]), float(self.tick_bid[i]), "tick"))
        bars = (self.bar_ms >= since_ms) & ((self.bar_high >= level - eps) if above else (self.bar_low <= level + eps))
        if bars.any():
            i = int(np.argmax(bars))
            hits.append((int(self.bar_ms[i]), None, "candle_m1"))
        return min(hits, key=lambda h: h[0]) if hits else None


def _times_ms(column: pd.Series) -> np.ndarray:
    return ((column - _EPOCH) // pd.Timedelta(milliseconds=1)).to_numpy(dtype=np.int64)


def _price_path(mt5: MT5Client, symbol: str, since_ms: int, now: datetime, *, catch_up: bool,
                accept_partial: bool) -> _Path:
    """Bid desde ``since_ms`` até ``now`` (horário do servidor, do último tick).

    Normal: só ticks. Recuperando um intervalo sem vigia (``catch_up``: painel fechado, PC suspenso): candles M1 no
    meio e ticks nas pontas. O terminal primeiro devolve o histórico guardado e só depois sincroniza, então candles
    que não chegam até o corte deixam o intervalo incompleto: o cursor para no último candle e a próxima rodada relê;
    com ``accept_partial`` (passou o tempo de espera), o que veio vale (mercado fechado no fim do intervalo).
    O copy_ticks_range trata as pontas em segundos inteiros, com o fim exclusivo: as janelas de ticks terminam num
    segundo cheio depois do que precisam e o que sobra é filtrado pelo horário.
    """
    since = _from_ms(since_ms)
    end = now + timedelta(seconds=2)
    first_full = since.replace(second=0, microsecond=0)
    if first_full < since:
        first_full += timedelta(minutes=1)
    cut = now.replace(second=0, microsecond=0) - timedelta(minutes=1)  # candles antes daqui já fecharam
    pieces: list[pd.DataFrame] = []
    bars = pd.DataFrame(columns=["time", "high", "low"])
    ends: list[int] = []
    complete = True
    if not catch_up or cut <= first_full:
        pieces.append(mt5.ticks_between(symbol, since, end))
        tail = pieces[-1]
    else:
        if first_full > since:
            head = mt5.ticks_between(symbol, since, first_full)
            head = head[head["time"] < pd.Timestamp(first_full)]
            pieces.append(head)
            if len(head):
                ends.append(int(_times_ms(head["time"]).max()))
        bars = mt5.rates_between(symbol, "M1", first_full, cut, attempts=1)
        bars = bars[bars["time"] < cut]
        if len(bars):
            ends.append(int(_times_ms(bars["time"]).max()) + 59_999)
        complete = accept_partial or (len(bars) > 0 and bars["time"].iloc[-1] >= cut - timedelta(minutes=1))
        tail = mt5.ticks_between(symbol, cut, end)
        pieces.append(tail)  # um toque aqui dispara já; o cursor só passa do corte com os candles completos
        if complete:
            ends.append(_ms(cut) - 1)
    if complete and len(tail):
        ends.append(int(_times_ms(tail["time"]).max()))
    ticks = pd.concat([p for p in pieces if len(p)], ignore_index=True) if any(len(p) for p in pieces) else None
    tick_ms = _times_ms(ticks["time"]) if ticks is not None else np.array([], dtype=np.int64)
    tick_bid = ticks["bid"].to_numpy(dtype=float) if ticks is not None else np.array([], dtype=float)
    bar_ms = _times_ms(bars["time"]) if len(bars) else np.array([], dtype=np.int64)
    return _Path(tick_ms, tick_bid, bar_ms, bars["high"].to_numpy(dtype=float), bars["low"].to_numpy(dtype=float),
                 max(ends) if ends else None, complete)


# ---------------------------------------------------------------------- notificação
_POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
_SHOWN_EXIT, _DRY_EXIT = 7, 5
# Notificação "lembrete": fica na tela até ser fechada, com som. O texto entra como nó de texto, nunca como XML.
_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$decode = { param($v) [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($v)) }
$title = & $decode $env:TRADING_MCP_ALERTA_TITULO
$text = & $decode $env:TRADING_MCP_ALERTA_TEXTO
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast scenario="reminder"><visual><binding template="ToastGeneric"><text/><text/></binding></visual><actions><action activationType="system" arguments="dismiss" content=""/></actions><audio src="ms-winsoundevent:Notification.Reminder"/></toast>')
$nodes = $xml.GetElementsByTagName('text')
[void]$nodes.Item(0).AppendChild($xml.CreateTextNode($title))
[void]$nodes.Item(1).AppendChild($xml.CreateTextNode($text))
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
if ($env:TRADING_MCP_ALERTA_SECO -eq '1') { exit 5 }
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
exit 7
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _run_toast(title: str, text: str, dry: bool = False, timeout_s: float = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TRADING_MCP_ALERTA_TITULO"] = _b64(title)
    env["TRADING_MCP_ALERTA_TEXTO"] = _b64(text)
    env["TRADING_MCP_ALERTA_SECO"] = "1" if dry else "0"
    encoded = base64.b64encode(_TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")
    command = [str(_POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(command, env=env, timeout=timeout_s, capture_output=True, creationflags=flags, check=False,
                          stdin=subprocess.DEVNULL)


def show_toast(title: str, text: str) -> bool:
    """Mostra a notificação do Windows; False (e o motivo no log) se não deu."""
    try:
        done = _run_toast(title, text)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("Notificação do Windows não saiu: %s", exc)
        return False
    if done.returncode != _SHOWN_EXIT:
        detail = (done.stderr or b"").decode("utf-8", "replace").strip()[-500:]
        log.warning("Notificação do Windows falhou (código %s): %s", done.returncode, detail)
        return False
    return True


def toast_self_test() -> bool:
    """Monta a notificação sem mostrá-la (confere o PowerShell e o WinRT); True se deu certo."""
    try:
        return _run_toast("teste", "teste", dry=True).returncode == _DRY_EXIT
    except (subprocess.TimeoutExpired, OSError):
        return False


def _price_text(value: float, digits: int) -> str:
    return f"{value:,.{digits}f}".replace(",", "\0").replace(".", ",").replace("\0", ".")


def message(alert: dict[str, Any]) -> tuple[str, str]:
    """Título e texto da notificação de um alerta disparado (horário de São Paulo)."""
    digits = int(alert["digitos"])
    when = tempo.exibicao(_parse_utc(alert["disparado_utc"]))["sao_paulo"][11:16]
    title = f"{alert['simbolo']} tocou {_price_text(alert['preco'], digits)}"
    if alert["disparo_por"] == "candle_m1":
        detail = f"No candle M1 das {when} (São Paulo), enquanto o painel estava fechado."
    else:
        detail = f"Bid {_price_text(alert['preco_disparo'], digits)} às {when} (São Paulo)."
    return title, f"{alert['titulo']}. {detail}"


def notify_windows(alert: dict[str, Any]) -> None:
    """Notificação numa thread à parte: o PowerShell leva ~1 s para abrir e o vigia não pode esperar."""
    title, text = message(alert)
    threading.Thread(target=show_toast, args=(title, text), name="notificacao-alerta", daemon=True).start()


# ---------------------------------------------------------------------- vigia
class Vigia:
    """Confere os alertas ativos a cada segundo numa thread do painel."""

    def __init__(self, store: AlertStore, mt5: MT5Client, notify: Callable[[dict[str, Any]], None] = notify_windows,
                 *, interval_s: float = CHECK_INTERVAL_S, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._store = store
        self._mt5 = mt5
        self._notify = notify
        self._interval = interval_s
        self._monotonic = monotonic
        self._cursors: dict[int, int] = {}  # id -> até onde (ms) os preços já foram conferidos
        self._catch_up: dict[int, float] = {}  # id -> quando (monotonic) começou a recuperar um intervalo sem vigia
        self._last_round: float | None = None
        self._saved_at = monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cycle: datetime | None = None
        self._errors: dict[str, str] = {}
        self._logged: dict[str, tuple[str, float]] = {}

    # -------------------------------------------------------------- ciclo
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="vigia-alertas", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        self._save_progress()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - o vigia não pode morrer por um erro inesperado
                log.exception("Erro inesperado no vigia dos alertas")
            self._stop.wait(self._interval)

    def status(self) -> dict[str, Any]:
        return {
            "ativo": self._thread is not None and self._thread.is_alive(),
            "ultimo_ciclo": tempo.iso_utc(self._last_cycle) if self._last_cycle else None,
            "erros": dict(self._errors),
        }

    def _error(self, key: str, text: str) -> None:
        self._errors[key] = text
        previous = self._logged.get(key)
        now = self._monotonic()
        if previous is None or previous[0] != text or now - previous[1] >= _ERROR_LOG_S:
            log.warning("Alertas (%s): %s", key, text)
            self._logged[key] = (text, now)

    def _save_progress(self) -> None:
        try:
            # Cópia: no stop() a thread pode ainda estar rodando (MT5 travado além do tempo do join).
            self._store.save_progress(dict(self._cursors))
        except AlertasError as exc:
            self._error("banco", str(exc))
        self._saved_at = self._monotonic()

    def check_once(self) -> list[dict[str, Any]]:
        """Uma rodada: devolve os alertas disparados nela (já gravados e notificados)."""
        try:
            alerts = self._store.active()
        except AlertasError as exc:
            self._error("banco", str(exc))
            return []
        self._errors.pop("banco", None)
        ids = {a["id"] for a in alerts}
        self._cursors = {k: v for k, v in self._cursors.items() if k in ids}
        self._catch_up = {k: v for k, v in self._catch_up.items() if k in ids}
        # Primeira rodada (o painel acabou de abrir) ou rodada depois de um buraco (PC suspenso, MT5 travado): os
        # ticks novos podem chegar antes dos do buraco, então o intervalo é recuperado pelos candles M1.
        mono = self._monotonic()
        woke = self._last_round is None or mono - self._last_round > WAKE_GAP_S
        self._last_round = mono
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for alert in alerts:
            by_symbol.setdefault(alert["simbolo"], []).append(alert)
        fired: list[dict[str, Any]] = []
        for symbol, group in by_symbol.items():
            try:
                fired += self._check_symbol(symbol, group, woke)
            except (MT5Error, ValueError, AlertasError) as exc:
                self._error(symbol, str(exc))
            else:
                self._errors.pop(symbol, None)
        for key in [k for k in self._errors if k != "banco" and k not in by_symbol]:
            del self._errors[key]  # símbolo sem alerta ativo: o erro antigo não vale mais
        self._last_cycle = self._mt5.now_utc()
        if self._monotonic() - self._saved_at >= SAVE_PROGRESS_S:
            self._save_progress()
        return fired

    def _cursor(self, alert: dict[str, Any]) -> int:
        return max(self._cursors.get(alert["id"], 0), int(alert["vigiado_ate_ms"]))

    def _check_symbol(self, symbol: str, group: list[dict[str, Any]], woke: bool) -> list[dict[str, Any]]:
        last = self._mt5.last_tick(symbol)
        if last is None:
            return []
        # O horário do último tick é o "agora" do servidor: o relógio do PC pode estar adiantado ou atrasado.
        now, last_bid = last
        last_ms = _ms(now)
        pending = [a for a in group if last_ms > self._cursor(a)]  # sem tick novo, nada a conferir
        if not pending:
            return []
        mono = self._monotonic()
        paths: dict[tuple[int, bool, bool], _Path] = {}
        fired = []
        for alert in pending:
            alert_id, since = alert["id"], self._cursor(alert)
            started = self._catch_up.get(alert_id)
            catch_up = woke or started is not None or now - _from_ms(since) > TICKS_ONLY
            accept = started is not None and mono - started >= SYNC_WAIT_S
            key = (since, catch_up, accept)
            if key not in paths:
                paths[key] = _price_path(self._mt5, symbol, since, now, catch_up=catch_up, accept_partial=accept)
            path = paths[key]
            if path.complete:
                self._catch_up.pop(alert_id, None)
            else:
                self._catch_up.setdefault(alert_id, mono)
            level, above = float(alert["preco"]), alert["lado"] == "acima"
            eps = 0.5 * 10 ** -int(alert["digitos"])
            hit = path.first_touch(level, above, since, eps)
            # O último tick também conta (o histórico de ticks pode chegar um pouco depois), mas não avança o cursor.
            if hit is None and (last_bid >= level - eps if above else last_bid <= level + eps):
                hit = (last_ms, last_bid, "tick")
            if hit is not None:
                when_ms, price, by = hit
                if self._store.fire(alert_id, _from_ms(when_ms), price, by):
                    done = {**alert, "status": "disparado", "disparado_utc": tempo.iso_utc(_from_ms(when_ms)),
                            "preco_disparo": price, "disparo_por": by}
                    fired.append(done)
                    self._notify(done)
                self._cursors.pop(alert_id, None)
                self._catch_up.pop(alert_id, None)
            elif path.until_ms is not None and path.until_ms > since:
                self._cursors[alert_id] = path.until_ms
        return fired
