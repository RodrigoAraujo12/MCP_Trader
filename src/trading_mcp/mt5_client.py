"""Cliente do MetaTrader 5 (thread-safe, conexão preguiçosa): leitura; envio de ordens só pelo caminho da etapa F."""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from trading_mcp import risk, tempo
from trading_mcp.config import KILL_FILE_NAME, Settings

log = logging.getLogger(__name__)

TIMEFRAMES: dict[str, str] = {
    "M1": "TIMEFRAME_M1",
    "M3": "TIMEFRAME_M3",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
    "W1": "TIMEFRAME_W1",
    "MN1": "TIMEFRAME_MN1",
}

_SUFFIX_RE = re.compile(r"^[a-z0-9._#+-]{1,3}$")
_RATE_COLUMNS = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]

_ALLOWED_CALLS = frozenset(
    {
        "initialize",
        "shutdown",
        "last_error",
        "terminal_info",
        "account_info",
        "symbols_get",
        "symbol_info",
        "symbol_info_tick",
        "symbol_select",
        "copy_rates_from",
        "copy_rates_from_pos",
        "copy_rates_range",
        "copy_ticks_range",
        "order_calc_profit",
        "order_calc_margin",
        "positions_get",
        "orders_get",
        "history_deals_get",
        "history_orders_get",
    }
)

# Únicas funções que enviam algo ao servidor da corretora; só pelos métodos check_order/send_order, que exigem
# a execução habilitada no .env e a conta demo, conferidas a cada chamada.
_TRADE_CALLS = frozenset({"order_check", "order_send"})

_WARMUP_TIMEOUT_S = 10.0
_WARMUP_STEP_S = 0.5
_TICK_ATTEMPTS = 3
_TICK_RETRY_S = 0.3
_RATES_ATTEMPTS = 3
_RATES_RETRY_S = 0.5
# O terminal entrega o histórico que tem guardado e só depois sincroniza: num ativo que ficou um tempo sem pedidos,
# os candles param onde ele deixou de atualizar (conferido em 2026-10-04: XAUUSDm e EURUSDm vieram até 22:05 com
# ticks às 23:17, e corretos segundos depois). Espera até _SYNC_ATTEMPTS × _SYNC_RETRY_S pelos candles novos.
_SYNC_ATTEMPTS = 10
_SYNC_RETRY_S = 0.5
# Folga entre o fim do último candle e o tick lido antes do pedido (tick só de ask não abre candle).
_SYNC_GRACE = timedelta(seconds=60)
# Atraso maior não é tratado como sincronização pendente: o histórico nem chega perto do tick (como nos candles
# sintéticos dos testes, de 2023, com ticks de agora).
_SYNC_MAX_LAG = timedelta(days=90)
# Cotação com até esta idade é "atual". Índices e forex têm ticks a cada poucos segundos na sessão.
_FRESH_S = 60.0
# Tick mais adiantado que isso em relação ao relógio UTC indica servidor fora de UTC (ou relógio errado).
_FUTURE_TOLERANCE_S = 60.0
# Tick mais antigo que isso não é comparado com a semana anterior.
_MAX_LOOKBACK = timedelta(days=7)

ACCOUNT_MODES = {"ACCOUNT_TRADE_MODE_DEMO": "demo", "ACCOUNT_TRADE_MODE_CONTEST": "concurso", "ACCOUNT_TRADE_MODE_REAL": "real"}
# Tipo de ordem -> (lado, descrição). Ordens a mercado só aparecem em orders_get enquanto são processadas.
ORDER_KINDS: dict[str, tuple[str | None, str]] = {
    "ORDER_TYPE_BUY": ("buy", "compra a mercado (em processamento)"),
    "ORDER_TYPE_SELL": ("sell", "venda a mercado (em processamento)"),
    "ORDER_TYPE_BUY_LIMIT": ("buy", "compra limitada"),
    "ORDER_TYPE_SELL_LIMIT": ("sell", "venda limitada"),
    "ORDER_TYPE_BUY_STOP": ("buy", "compra stop"),
    "ORDER_TYPE_SELL_STOP": ("sell", "venda stop"),
    "ORDER_TYPE_BUY_STOP_LIMIT": ("buy", "compra stop limitada"),
    "ORDER_TYPE_SELL_STOP_LIMIT": ("sell", "venda stop limitada"),
    "ORDER_TYPE_CLOSE_BY": (None, "fechamento por posição oposta (em processamento)"),
}
ORDER_TIMES = {
    "ORDER_TIME_GTC": "até cancelar",
    "ORDER_TIME_DAY": "até o fim do dia",
    "ORDER_TIME_SPECIFIED": "até a data de expiração",
    "ORDER_TIME_SPECIFIED_DAY": "até o fim do dia da expiração",
}
# Depósito/saque = "saldo"; crédito da corretora (não mexe no saldo) = "credito"; tarifas, juros, dividendos,
# correções etc. = "outro".
DEAL_TYPES = {"DEAL_TYPE_BUY": "buy", "DEAL_TYPE_SELL": "sell", "DEAL_TYPE_BALANCE": "saldo", "DEAL_TYPE_CREDIT": "credito"}
DEAL_ENTRIES = {"DEAL_ENTRY_IN": "in", "DEAL_ENTRY_OUT": "out", "DEAL_ENTRY_INOUT": "inout", "DEAL_ENTRY_OUT_BY": "out_by"}
DEAL_REASONS = {
    "DEAL_REASON_CLIENT": "terminal",
    "DEAL_REASON_MOBILE": "celular",
    "DEAL_REASON_WEB": "web",
    "DEAL_REASON_EXPERT": "robo",
    "DEAL_REASON_SL": "stop",
    "DEAL_REASON_TP": "alvo",
    "DEAL_REASON_SO": "stop_out",
    "DEAL_REASON_ROLLOVER": "rolagem",
    "DEAL_REASON_VMARGIN": "margem_variacao",
    "DEAL_REASON_SPLIT": "desdobramento",
}


class MT5Error(Exception):
    """Erro ao falar com o MetaTrader 5."""


class SymbolNotFoundError(MT5Error):
    """Símbolo não encontrado na corretora."""


class _ReadOnlyModule:
    """Proxy do módulo MetaTrader5: só expõe funções de leitura e constantes MAIÚSCULAS.

    Evita chamadas acidentais neste código; não é barreira de segurança (o módulo original
    continua acessível a quem importá-lo diretamente).
    """

    __slots__ = ("_raw",)

    def __init__(self, raw: Any) -> None:
        object.__setattr__(self, "_raw", raw)

    def __getattr__(self, name: str) -> Any:
        if name in _ALLOWED_CALLS or (name[:1].isalpha() and name == name.upper()):
            return getattr(object.__getattribute__(self, "_raw"), name)
        raise MT5Error(f"Operação não permitida por este caminho (só leitura): {name}")


def _guard(func: Callable[..., Any]) -> Callable[..., Any]:
    """Converte falhas inesperadas ao interpretar dados do MT5 em MT5Error."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (MT5Error, ValueError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise MT5Error(
                f"Erro inesperado ao processar dados do MetaTrader 5 em {func.__name__}() "
                f"({type(exc).__name__}): {exc}"
            ) from exc

    return wrapper


class MT5Client:
    """Acesso de leitura ao MT5; toda chamada ao módulo é serializada por um RLock."""

    def __init__(
        self,
        settings: Settings,
        mt5_module: Any | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._settings = settings
        self._mt5: Any | None = _ReadOnlyModule(mt5_module) if mt5_module is not None else None
        self._sleep = sleep
        self._monotonic = monotonic
        self._now_utc = now_utc
        self._lock = threading.RLock()
        self._initialized = False
        self._symbol_cache: dict[str, str] = {}
        # Estado observado na última verificação (ensure_connected).
        self._connected = False
        self._identity: tuple[Any, Any] | None = None  # (login, servidor) da última verificação
        # Com a conta fixada no .env: (login, servidor) da primeira conexão; não é zerado ao reconectar.
        self._pinned_identity: tuple[Any, Any] | None = None
        self._trade_mode: Any = None
        self._account_snapshot: Any = None  # account_info() da última verificação
        self._terminal_maxbars: int | None = None

    # ------------------------------------------------------------------ conexão
    def _module(self) -> Any:
        if self._mt5 is None:
            try:
                import MetaTrader5  # type: ignore[import-not-found]
            except ImportError as exc:
                raise MT5Error(
                    "O pacote MetaTrader5 não está instalado. Instale com `pip install MetaTrader5` "
                    "(funciona somente no Windows)."
                ) from exc
            self._mt5 = _ReadOnlyModule(MetaTrader5)
        return self._mt5

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Chama uma função do MT5 sob o lock; falhas inesperadas viram MT5Error."""
        with self._lock:
            mt5 = self._module()
            try:
                return getattr(mt5, name)(*args, **kwargs)
            except MT5Error:
                raise
            except Exception as exc:  # noqa: BLE001 - a extensão em C pode lançar qualquer coisa
                raise MT5Error(
                    f"Erro inesperado do MetaTrader 5 em {name}() ({type(exc).__name__}): {exc}"
                ) from exc

    def _last_error(self) -> str:
        try:
            return str(self._call("last_error"))
        except Exception:  # noqa: BLE001 - só para compor a mensagem
            return "desconhecido"

    def _shutdown_quietly(self) -> None:
        try:
            self._call("shutdown")
        except MT5Error:
            pass

    def now_utc(self) -> datetime:
        """Relógio UTC usado pelo cliente (injetável nos testes)."""
        return self._now_utc()

    @property
    def account_pinned(self) -> bool:
        """True se o .env fixa a conta esperada (MT5_LOGIN e/ou MT5_SERVER)."""
        return self._settings.mt5_login is not None or bool(self._settings.mt5_server)

    @_guard
    def ensure_connected(self) -> None:
        """Inicializa o MT5 se necessário e, a cada uso, confere conexão e conta logada.

        Troca de conta no terminal: com MT5_LOGIN/MT5_SERVER no .env, as ferramentas são
        bloqueadas até voltar à conta configurada (sem relogar sozinho); sem eles, o cache
        de símbolos é limpo e a nova conta passa a ser usada. Perda de conexão depois de
        conectado não gera erro: fica registrada e aparece no estado das cotações.
        """
        with self._lock:
            s = self._settings
            if s.errors:
                where = s.env_file or "variáveis de ambiente"
                raise MT5Error(f"Configuração inválida no .env ({where}): {'; '.join(s.errors)}")
            if s.mt5_login is not None and not s.mt5_path:
                raise MT5Error(
                    "MT5_LOGIN está definido sem MT5_PATH: o MetaTrader 5 usaria o terminal padrão da máquina "
                    "e trocaria a conta dele (podendo parar seus robôs). Defina MT5_PATH com o terminal64.exe "
                    "da instalação dedicada à conta demo, ou remova MT5_LOGIN/MT5_PASSWORD/MT5_SERVER para "
                    "usar a conta já logada no terminal."
                )
            self._module()
            if self._initialized:
                info = self._call("terminal_info")
                if info is not None:
                    self._update_terminal(info)
                    self._check_account(initializing=False)
                    return
                log.warning("Terminal MT5 não responde; reconectando")
                self._shutdown_quietly()
            self._initialized = False
            self._symbol_cache.clear()
            self._identity = None
            kwargs: dict[str, Any] = {}
            if s.mt5_path:
                kwargs["path"] = s.mt5_path
            if s.mt5_login is not None:
                kwargs["login"] = s.mt5_login
            if s.mt5_password:
                kwargs["password"] = s.mt5_password
            if s.mt5_server:
                kwargs["server"] = s.mt5_server
            if s.mt5_timeout_ms:
                kwargs["timeout"] = s.mt5_timeout_ms
            log.info("Inicializando MT5 (parâmetros: %s)", sorted(k for k in kwargs if k != "password"))
            if not self._call("initialize", **kwargs):
                raise MT5Error(
                    f"Falha ao inicializar o MetaTrader 5: {self._last_error()}. "
                    "Verifique: o MT5 está instalado? O terminal está aberto? "
                    "MT5_PATH aponta para o terminal64.exe certo? Login e servidor estão corretos?"
                )
            deadline = self._monotonic() + _WARMUP_TIMEOUT_S
            while True:
                info = self._call("terminal_info")
                if info is None:
                    raise MT5Error(f"MT5 inicializado, mas terminal_info() falhou: {self._last_error()}.")
                if getattr(info, "connected", False):
                    break
                if self._monotonic() >= deadline:
                    raise MT5Error(
                        "O terminal MT5 está aberto, mas sem conexão com o servidor da corretora. "
                        "Verifique a internet e o login da conta."
                    )
                self._sleep(_WARMUP_STEP_S)
            self._update_terminal(info)
            self._check_account(initializing=True)
            self._initialized = True

    def _update_terminal(self, info: Any) -> None:
        was_connected = self._connected
        self._connected = bool(getattr(info, "connected", False))
        if was_connected and not self._connected:
            log.warning("Terminal MT5 perdeu a conexão com a corretora")
        maxbars = getattr(info, "maxbars", None)
        if isinstance(maxbars, int) and maxbars > 1:
            self._terminal_maxbars = maxbars

    def _check_account(self, *, initializing: bool) -> None:
        """Confere a conta logada contra o .env e detecta troca de conta.

        Com a conta fixada (mesmo que só por MT5_LOGIN ou só por MT5_SERVER), o par
        (login, servidor) da primeira conexão fica congelado, inclusive depois de o terminal
        reiniciar: qualquer troca bloqueia.
        """
        acc = self._call("account_info")
        if acc is None:
            if initializing:
                self._shutdown_quietly()
            raise MT5Error(
                f"O terminal MT5 não informou a conta logada ({self._last_error()}). "
                "Confira se há uma conta conectada no terminal."
            )
        s = self._settings
        login, server = getattr(acc, "login", None), getattr(acc, "server", None)
        problems: list[str] = []
        if s.mt5_login is not None and login != s.mt5_login:
            problems.append(f"na conta {login}, diferente de MT5_LOGIN ({s.mt5_login})")
        if s.mt5_server and server != s.mt5_server:
            problems.append(f"no servidor {server}, diferente de MT5_SERVER ({s.mt5_server})")
        identity = (login, server)
        frozen = self._pinned_identity
        if not problems and frozen is not None and identity != frozen:
            problems.append(
                f"na conta {login} do servidor {server}, diferente da conectada no início "
                f"({frozen[0]} em {frozen[1]})"
            )
        if problems:
            if initializing:
                self._shutdown_quietly()
                action = "Conexão encerrada; confira MT5_PATH, MT5_LOGIN e MT5_SERVER."
            else:
                action = (
                    "A conta foi trocada no terminal: as ferramentas ficam bloqueadas até ele voltar "
                    "para a conta original."
                )
            raise MT5Error(f"O terminal MT5 está logado {' e '.join(problems)}. {action}")
        if self._identity is not None and identity != self._identity:
            log.warning("Conta do terminal mudou de %s para %s; cache de símbolos limpo", self._identity, identity)
            self._symbol_cache.clear()
        self._identity = identity
        if self.account_pinned and self._pinned_identity is None:
            self._pinned_identity = identity
        self._trade_mode = getattr(acc, "trade_mode", None)
        self._account_snapshot = acc

    def _source(self) -> str:
        return f"MetaTrader 5 ({self._identity[1]})" if self._identity else "MetaTrader 5"

    def _account_kind(self) -> str:
        mt5 = self._module()
        modes = {getattr(mt5, const): kind for const, kind in ACCOUNT_MODES.items()}
        return modes.get(self._trade_mode, "desconhecido")

    # ------------------------------------------------------------------ símbolos
    def _all_symbols(self) -> list[Any]:
        return list(self._call("symbols_get") or [])

    @_guard
    def resolve_symbol(self, name: str) -> str:
        """Resolve o nome informado para o nome real do símbolo na corretora."""
        key = (name or "").strip().upper()
        if not key:
            raise ValueError("Informe o nome do símbolo.")
        with self._lock:
            self.ensure_connected()
            if key in self._symbol_cache:
                return self._symbol_cache[key]
            resolved = self._find_symbol(key)
            if not self._call("symbol_select", resolved, True):
                raise MT5Error(
                    f"Não foi possível adicionar {resolved} ao Market Watch: {self._last_error()}"
                )
            self._symbol_cache[key] = resolved
            # O nome real também vira chave: chamadas seguintes com "USTECm" não refazem a busca.
            self._symbol_cache.setdefault(resolved.upper(), resolved)
            return resolved

    def _find_symbol(self, key: str) -> str:
        symbols = self._all_symbols()
        names = [s.name for s in symbols]
        by_upper = {n.upper(): n for n in names}

        # 1) existe exatamente (ou só difere em caixa)
        if self._call("symbol_info", key) is not None:
            return key
        if key in by_upper:
            return by_upper[key]

        # 2) sufixo configurado
        suffix = (self._settings.symbol_suffix or "").strip()
        if suffix:
            cand = (key + suffix).upper()
            if cand in by_upper:
                return by_upper[cand]
            if self._call("symbol_info", key + suffix) is not None:
                return key + suffix

        # 3) detecção automática de sufixo curto (minúsculas/símbolos)
        cands = [
            s
            for s in symbols
            if s.name.upper().startswith(key) and _SUFFIX_RE.match(s.name[len(key):]) and not s.name[len(key):].isdigit()
        ]
        if len(cands) > 1:
            visible = [s for s in cands if getattr(s, "visible", False)]
            if visible:
                cands = visible
        if len(cands) == 1:
            return cands[0].name
        if len(cands) > 1:
            raise MT5Error(
                f"Símbolo '{key}' é ambíguo; candidatos: {', '.join(sorted(s.name for s in cands))}. "
                "Defina SYMBOL_SUFFIX ou informe o nome completo."
            )

        suggestions = sorted(n for n in names if key in n.upper())[:10]
        hint = f" Sugestões: {', '.join(suggestions)}." if suggestions else ""
        raise SymbolNotFoundError(f"Símbolo '{key}' não encontrado na corretora.{hint}")

    @_guard
    def search_symbols(self, query: str, limit: int = 30) -> list[dict]:
        """Busca símbolos por nome ou descrição (case-insensitive)."""
        q = (query or "").strip().lower()
        with self._lock:
            self.ensure_connected()
            symbols = self._all_symbols()
        out: list[dict] = []
        for s in symbols:
            if q and q not in s.name.lower() and q not in (s.description or "").lower():
                continue
            out.append(
                {
                    "nome": s.name,
                    "descricao": s.description,
                    "categoria": s.path,
                    "digitos": s.digits,
                }
            )
            if len(out) >= max(1, limit):
                break
        return out

    def _info(self, resolved: str) -> Any:
        info = self._call("symbol_info", resolved)
        if info is None:
            raise MT5Error(f"Sem informações do símbolo {resolved}: {self._last_error()}")
        return info

    def _is_forex(self, info: Any) -> bool:
        mt5 = self._module()
        return info.trade_calc_mode in (mt5.SYMBOL_CALC_MODE_FOREX, mt5.SYMBOL_CALC_MODE_FOREX_NO_LEVERAGE)

    @_guard
    def symbol_spec(self, symbol: str) -> dict:
        """Especificação do contrato do símbolo."""
        with self._lock:
            resolved = self.resolve_symbol(symbol)
            i = self._info(resolved)
            return {
                "simbolo": resolved,
                "descricao": i.description,
                "categoria": i.path,
                "digitos": i.digits,
                "ponto": i.point,
                "tick_size": i.trade_tick_size,
                "tick_value": i.trade_tick_value,
                "tick_value_loss": i.trade_tick_value_loss,
                "contrato": i.trade_contract_size,
                "volume_min": i.volume_min,
                "volume_max": i.volume_max,
                "volume_step": i.volume_step,
                "moeda_base": i.currency_base,
                "moeda_lucro": i.currency_profit,
                "moeda_margem": i.currency_margin,
                "is_forex": self._is_forex(i),
                "modo_preenchimento": getattr(i, "filling_mode", 0),
                "modos_expiracao": getattr(i, "expiration_mode", 0),
            }

    # ------------------------------------------------------------------ mercado
    @staticmethod
    def _tick_valid(tick: Any) -> bool:
        return tick is not None and tick.time != 0 and tick.bid > 0 and tick.ask > 0

    @staticmethod
    def _tick_time(tick: Any) -> datetime:
        time_msc = getattr(tick, "time_msc", 0) or 0
        return tempo.from_epoch(time_msc / 1000 if time_msc else tick.time)

    def _traded_between(self, resolved: str, start: datetime, end: datetime) -> bool | None:
        """Houve candle M1 entre ``start`` e ``end``? None se o histórico não respondeu."""
        try:
            data = self._call("copy_rates_range", resolved, self._module().TIMEFRAME_M1, start, end)
        except MT5Error as exc:
            log.warning("copy_rates_range falhou para %s: %s", resolved, exc)
            return None
        if data is None:
            return None
        return len(data) > 0

    def _freshness(self, resolved: str, tick_time: datetime, now: datetime) -> tuple[str, str | None]:
        """Estado da cotação a partir da idade do tick, da conexão e da semana anterior.

        Para separar mercado fechado de atraso, confere se o ativo negociou na semana anterior
        durante o mesmo intervalo que hoje está sem ticks (do último tick até agora).
        """
        age = (now - tick_time).total_seconds()
        if not self._connected:
            return (
                "desconectado",
                f"O terminal está sem conexão com a corretora: esta é a última cotação recebida, "
                f"de {tempo.describe_age(age)} atrás.",
            )
        if age < -_FUTURE_TOLERANCE_S:
            return (
                "horario_inconsistente",
                f"A cotação está {tempo.describe_age(age)} à frente do relógio UTC deste computador: o servidor "
                "pode não usar UTC ou o relógio do Windows está errado. Não use os horários sem conferir.",
            )
        if age <= _FRESH_S:
            return "atual", None
        ago = tempo.describe_age(age)
        if now - tick_time > _MAX_LOOKBACK:
            return "antigo", f"Última cotação há {ago}: o ativo não negocia há mais de uma semana."
        week = timedelta(days=7)
        # Candles M1 são marcados pela abertura do minuto: o primeiro candle inteiro sem ticks hoje
        # começa no minuto seguinte ao do último tick.
        gap_start = tick_time.replace(second=0, microsecond=0) + timedelta(minutes=1)
        traded = self._traded_between(resolved, gap_start - week, now - week)
        if traded is False:
            return (
                "mercado_fechado_provavel",
                f"Última cotação há {ago}. No mesmo intervalo da semana passada também não houve negociação: "
                "provável pausa diária, fim de semana ou horário fora da sessão do ativo (ou feriado na "
                "semana passada).",
            )
        if traded is True:
            return (
                "atrasado",
                f"Última cotação há {ago}, mas no mesmo intervalo da semana passada houve negociação: possível "
                "feriado, atraso ou problema de conexão. Não trate como preço atual.",
            )
        return "antigo", f"Última cotação há {ago}; não foi possível verificar o horário de negociação do ativo."

    @_guard
    def quote(self, symbol: str) -> dict:
        """Cotação com spread, horário (UTC, São Paulo, Nova York), idade e estado."""
        with self._lock:
            resolved = self.resolve_symbol(symbol)
            info = self._info(resolved)
            tick = None
            for attempt in range(_TICK_ATTEMPTS):
                if attempt:
                    self._call("symbol_select", resolved, True)
                    self._sleep(_TICK_RETRY_S)
                tick = self._call("symbol_info_tick", resolved)
                if self._tick_valid(tick):
                    break
            else:
                raise MT5Error(
                    f"Sem cotação para {resolved}: mercado fechado ou símbolo ainda sem dados"
                )
            digits = int(info.digits)
            spread = tick.ask - tick.bid
            spread_points = int(round(spread / info.point)) if info.point else 0
            spread_pips: float | None = None
            if self._is_forex(info):
                pip = risk.pip_size(info.point, digits)
                spread_pips = round(spread / pip, 1) if pip else None
            tick_time = self._tick_time(tick)
            now = self._now_utc()
            age = (now - tick_time).total_seconds()
            estado, aviso = self._freshness(resolved, tick_time, now)
            result = {
                "simbolo": resolved,
                "descricao": info.description,
                "categoria": info.path,
                "moeda": info.currency_profit,
                "fonte": self._source(),
                "bid": round(tick.bid, digits),
                "ask": round(tick.ask, digits),
                "spread_preco": round(spread, digits),
                "spread_pontos": spread_points,
                "spread_pips": spread_pips,
                "horario": tempo.exibicao(tick_time),
                # Idade negativa dentro da tolerância = relógio do PC um pouco atrás do servidor.
                "idade_s": round(age if estado == "horario_inconsistente" else max(age, 0.0), 1),
                "estado": estado,
                "coletado_utc": tempo.iso_utc(now),
            }
            if aviso:
                result["aviso"] = aviso
            return result

    def _bars_limit(self) -> int:
        """Máximo de candles por chamada: MAX_BARS e o limite do terminal (pedir maxbars falha)."""
        limit = self._settings.max_bars
        if self._terminal_maxbars:
            limit = min(limit, self._terminal_maxbars - 1)
        return max(1, limit)

    @_guard
    def rates(self, symbol: str, timeframe: str, count: int, include_current: bool = True) -> pd.DataFrame:
        """Candles do mais antigo ao mais novo, com ``time`` em UTC.

        ``df.attrs``: ``ultimo_em_formacao`` (o último candle ainda está aberto, pelo horário e não
        pela posição), ``horario_inconsistente`` (último candle abre depois de agora), ``conectado``
        (terminal conectado à corretora; sem conexão os candles recentes podem faltar) e ``defasado``
        (o último candle termina antes da última cotação mesmo depois de esperar a sincronização do
        terminal: os candles mais novos faltam). Sem o candle atual, só sai o candle que de fato está
        aberto: na pausa diária ou no fim de semana o último candle já fechou e é mantido.
        """
        tf_key = (timeframe or "").strip().upper()
        if tf_key not in TIMEFRAMES:
            raise ValueError(f"Timeframe inválido: '{timeframe}'. Válidos: {', '.join(TIMEFRAMES)}.")
        with self._lock:
            resolved = self.resolve_symbol(symbol)
            count = max(1, min(int(count), self._bars_limit()))
            # O candle extra (para descartar um aberto) só esbarra no limite do terminal, não no MAX_BARS.
            terminal_limit = self._terminal_maxbars - 1 if self._terminal_maxbars else count + 1
            request = min(count + (0 if include_current else 1), terminal_limit)
            tf = getattr(self._module(), TIMEFRAMES[tf_key])
            # Lido antes dos candles: todo tick lido aqui já tem de estar neles.
            tick_time = self._last_tick_time(resolved)
            data = self._copy_rates(resolved, tf, tf_key, request)
            behind = self._behind_tick(data, tf_key, tick_time)
            for _ in range(_SYNC_ATTEMPTS):
                if not behind:
                    break
                self._sleep(_SYNC_RETRY_S)
                data = self._copy_rates(resolved, tf, tf_key, request)
                behind = self._behind_tick(data, tf_key, tick_time)
            connected = self._connected
        df = pd.DataFrame(data)[_RATE_COLUMNS]
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        now = self._now_utc()
        raw_last_open = df["time"].iloc[-1].to_pydatetime()
        # Candle aberto depois de agora: o servidor não está em UTC (ou o relógio do Windows está errado).
        future = (raw_last_open - now).total_seconds() > _FUTURE_TOLERANCE_S
        if not include_current and tempo.bar_in_progress(raw_last_open, tf_key, now):
            df = df.iloc[:-1]
            if df.empty:
                raise MT5Error(f"Sem candles fechados para {resolved} em {tf_key}.")
        df = df.tail(count).reset_index(drop=True)
        last_open = df["time"].iloc[-1].to_pydatetime()
        df.attrs["ultimo_em_formacao"] = tempo.bar_in_progress(last_open, tf_key, now)
        df.attrs["horario_inconsistente"] = future
        df.attrs["conectado"] = connected
        df.attrs["defasado"] = behind
        return df

    def _copy_rates(self, resolved: str, tf: Any, tf_key: str, request: int) -> Any:
        """Os ``request`` candles mais recentes; repete enquanto o terminal devolve vazio (histórico não carregado)."""
        for attempt in range(_RATES_ATTEMPTS):
            if attempt:
                self._sleep(_RATES_RETRY_S)
            data = self._call("copy_rates_from_pos", resolved, tf, 0, request)
            if data is not None and len(data) > 0:
                return data
        raise MT5Error(f"Sem candles para {resolved} em {tf_key}: {self._last_error()}")

    def _last_tick_time(self, resolved: str) -> datetime | None:
        try:
            tick = self._call("symbol_info_tick", resolved)
        except MT5Error as exc:
            log.warning("symbol_info_tick falhou para %s: %s", resolved, exc)
            return None
        return self._tick_time(tick) if self._tick_valid(tick) else None

    @staticmethod
    def _behind_tick(data: Any, tf_key: str, tick_time: datetime | None) -> bool:
        """O último candle termina antes do tick (o terminal devolveu o histórico guardado, sem os candles novos)?"""
        if tick_time is None:
            return False
        lag = tick_time - tempo.bar_end(tempo.from_epoch(int(data[-1]["time"])), tf_key)
        return _SYNC_GRACE < lag <= _SYNC_MAX_LAG

    @staticmethod
    def _require_utc(start: datetime, end: datetime) -> tuple[datetime, datetime]:
        # Sem fuso, o MT5 lê a data como horário local do Windows (conferido em 2026-10-02).
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Informe início e fim com fuso horário (UTC).")
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    @_guard
    def rates_between(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Candles com abertura entre ``start`` e ``end`` (UTC), do mais antigo ao mais novo; pode vir vazio.

        Os candles do MT5 são de bid. ``df.attrs["conectado"]`` informa a conexão com a corretora.
        """
        tf_key = (timeframe or "").strip().upper()
        if tf_key not in TIMEFRAMES:
            raise ValueError(f"Timeframe inválido: '{timeframe}'. Válidos: {', '.join(TIMEFRAMES)}.")
        start, end = self._require_utc(start, end)
        with self._lock:
            resolved = self.resolve_symbol(symbol)
            tf = getattr(self._module(), TIMEFRAMES[tf_key])
            past = end < self._now_utc() - timedelta(minutes=1)
            for attempt in range(_RATES_ATTEMPTS):
                if attempt:
                    self._sleep(_RATES_RETRY_S)
                data = self._call("copy_rates_range", resolved, tf, start, end)
                if data is None:
                    raise MT5Error(f"Sem candles de {resolved} em {tf_key}: {self._last_error()}")
                # Histórico ainda não carregado volta vazio na primeira chamada; período em andamento pode
                # estar vazio de verdade (mercado fechado), então só o passado é repetido.
                if len(data) or not past:
                    break
            connected = self._connected
        df = pd.DataFrame(data, columns=_RATE_COLUMNS) if len(data) else pd.DataFrame(columns=_RATE_COLUMNS)
        df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="s", utc=True)
        df.attrs["conectado"] = connected
        return df.reset_index(drop=True)

    @_guard
    def rates_until(self, symbol: str, timeframe: str, end: datetime, count: int) -> pd.DataFrame:
        """Até ``count`` candles já fechados no instante ``end`` (UTC), do mais antigo ao mais novo; pode vir vazio.

        ``copy_rates_from`` devolve os candles que abriram até ``end``, inclusive o que estava aberto nele (conferido
        em 2026-10-02): esse é descartado, porque a máxima, a mínima e o fechamento dele incluem o que veio depois.
        Pedindo uma data anterior ao início do histórico, o terminal devolve o primeiro candle que tem, que é
        posterior: todo candle que termina depois de ``end`` é descartado. ``df.attrs["conectado"]`` informa a
        conexão com a corretora.
        """
        tf_key = (timeframe or "").strip().upper()
        if tf_key not in TIMEFRAMES:
            raise ValueError(f"Timeframe inválido: '{timeframe}'. Válidos: {', '.join(TIMEFRAMES)}.")
        if end.tzinfo is None:
            raise ValueError("Informe o horário com fuso horário (UTC).")
        end = end.astimezone(timezone.utc)
        with self._lock:
            resolved = self.resolve_symbol(symbol)
            tf = getattr(self._module(), TIMEFRAMES[tf_key])
            # Só o limite do terminal: o MAX_BARS do .env vale para os pedidos do usuário, não para o contexto
            # (que precisa de todos os candles pedidos para não marcar falta onde não há).
            terminal_limit = self._terminal_maxbars - 1 if self._terminal_maxbars else int(count) + 1
            request = max(1, min(int(count) + 1, terminal_limit))
            data = None
            for attempt in range(_RATES_ATTEMPTS):
                if attempt:
                    self._sleep(_RATES_RETRY_S)
                data = self._call("copy_rates_from", resolved, tf, end, request)
                # Histórico ainda não carregado volta vazio (ou None) na primeira chamada.
                if data is not None and len(data):
                    break
            connected = self._connected
        if data is None:
            raise MT5Error(f"Sem candles de {resolved} em {tf_key}: {self._last_error()}")
        df = pd.DataFrame(data, columns=_RATE_COLUMNS) if len(data) else pd.DataFrame(columns=_RATE_COLUMNS)
        df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="s", utc=True)
        if len(df):
            closed = [not tempo.bar_in_progress(t.to_pydatetime(), tf_key, end) for t in df["time"]]
            df = df[closed]
        df = df.tail(int(count)).reset_index(drop=True)
        df.attrs["conectado"] = connected
        return df

    @_guard
    def oldest_bar(self, symbol: str, timeframe: str) -> datetime:
        """Abertura do candle mais antigo que o terminal entrega.

        O terminal guarda um número fixo de candles ("Máx. de barras no gráfico"): no M1, um símbolo que
        negocia 24 h por dia (BTCUSD) tem menos dias de histórico que um índice com pausa diária. Falha de
        leitura vira MT5Error, nunca "sem histórico".
        """
        tf_key = (timeframe or "").strip().upper()
        if tf_key not in TIMEFRAMES:
            raise ValueError(f"Timeframe inválido: '{timeframe}'. Válidos: {', '.join(TIMEFRAMES)}.")
        with self._lock:
            resolved = self.resolve_symbol(symbol)
            count = self._terminal_maxbars - 1 if self._terminal_maxbars else 99_999
            tf = getattr(self._module(), TIMEFRAMES[tf_key])
            for attempt in range(_RATES_ATTEMPTS):
                if attempt:
                    self._sleep(_RATES_RETRY_S)
                data = self._call("copy_rates_from_pos", resolved, tf, 0, count)
                if data is not None and len(data) > 0:
                    break
            else:
                raise MT5Error(f"Sem candles para {resolved} em {tf_key}: {self._last_error()}")
        return tempo.from_epoch(int(data["time"][0]))

    @_guard
    def ticks_between(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Mudanças de bid/ask entre ``start`` e ``end`` (UTC), com ``time`` em milissegundos convertido para UTC."""
        start, end = self._require_utc(start, end)
        with self._lock:
            resolved = self.resolve_symbol(symbol)
            mt5 = self._module()
            data = self._call("copy_ticks_range", resolved, start, end, mt5.COPY_TICKS_INFO)
            if data is None:
                raise MT5Error(f"Sem ticks de {resolved}: {self._last_error()}")
        df = pd.DataFrame(data)
        if df.empty:
            return pd.DataFrame(columns=["time", "bid", "ask"])
        df["time"] = pd.to_datetime(df["time_msc"].astype("int64"), unit="ms", utc=True)
        return df[["time", "bid", "ask"]].reset_index(drop=True)

    @_guard
    def loss_per_lot(self, symbol: str, entry: float, stop: float) -> tuple[float, str]:
        """Perda (moeda da conta) de 1 lote se o stop for atingido, e a direção inferida."""
        if stop == entry:
            raise ValueError("Entrada e stop não podem ser iguais.")
        side = "buy" if stop < entry else "sell"
        with self._lock:
            resolved = self.resolve_symbol(symbol)
            mt5 = self._module()
            action = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
            profit = self._call("order_calc_profit", action, resolved, 1.0, entry, stop)
            if profit:
                return abs(float(profit)), side
            info = self._info(resolved)
            tick_value = getattr(info, "trade_tick_value_loss", 0.0) or info.trade_tick_value
        log.info("order_calc_profit indisponível para %s; usando tick_value", resolved)
        return risk.loss_per_lot_from_ticks(entry, stop, info.trade_tick_size, tick_value), side

    @_guard
    def margin(self, symbol: str, side: str, price: float, volume: float) -> float | None:
        """Margem exigida para `volume` lotes (pode haver faixas por volume), ou None se indisponível."""
        side_key = (side or "").strip().lower() if isinstance(side, str) else ""
        if side_key not in ("buy", "sell"):
            raise ValueError(f"side deve ser 'buy' ou 'sell', recebido: {side!r}.")
        with self._lock:
            resolved = self.resolve_symbol(symbol)
            mt5 = self._module()
            action = mt5.ORDER_TYPE_BUY if side_key == "buy" else mt5.ORDER_TYPE_SELL
            value = self._call("order_calc_margin", action, resolved, float(volume), float(price))
        return float(value) if value else None

    # ------------------------------------------------------------------ conta
    @_guard
    def account(self) -> dict:
        """Resumo da conta (sem senha e sem nome do titular)."""
        with self._lock:
            self.ensure_connected()
            a = self._account_snapshot  # a mesma leitura que acabou de ser conferida
            kind = self._account_kind()
            return {
                "login": a.login,
                "servidor": a.server,
                "corretora": a.company,
                "moeda": a.currency,
                "saldo": a.balance,
                "equity": a.equity,
                "margem": a.margin,
                "margem_livre": a.margin_free,
                "nivel_margem": a.margin_level,
                "alavancagem": a.leverage,
                "tipo_conta": kind,
                "is_demo": kind == "demo",
                "conectado": self._connected,
            }

    @_guard
    def terminal(self) -> dict:
        """Estado do terminal, incluindo as travas de negociação automática."""
        with self._lock:
            self.ensure_connected()
            t = self._call("terminal_info")
            if t is None:
                raise MT5Error(f"Não foi possível ler o terminal: {self._last_error()}")
            return {
                "conectado": bool(getattr(t, "connected", False)),
                "algo_trading_ativo": bool(getattr(t, "trade_allowed", False)),
                "negociacao_via_python_desativada": bool(getattr(t, "tradeapi_disabled", False)),
                "build": getattr(t, "build", None),
                "max_barras_grafico": getattr(t, "maxbars", None),
            }

    # ------------------------------------------------------------------ ordens (etapa F)
    def _trade(self, name: str, request: dict[str, Any], account: tuple[int, str]) -> Any:
        """order_check/order_send no módulo original, conferindo agora: execução habilitada, sem o arquivo de parada,
        a conta ``account`` (login, servidor) da proposta logada no terminal e conta demo."""
        if name not in _TRADE_CALLS:
            raise MT5Error(f"Operação de negociação desconhecida: {name}")
        if not self._settings.execution_enabled:
            raise MT5Error("Execução desativada (EXECUCAO_HABILITADA no .env não é 'sim'): nenhuma ordem é enviada.")
        if (Path(self._settings.propostas_path).parent / KILL_FILE_NAME).exists():
            raise MT5Error(f"Execuções paradas (arquivo {KILL_FILE_NAME}): nada foi enviado.")
        with self._lock:
            self.ensure_connected()  # lê a conta de novo e atualiza o tipo de conta
            snapshot = self._account_snapshot
            if (getattr(snapshot, "login", None), getattr(snapshot, "server", None)) != tuple(account):
                raise MT5Error("O terminal está em outra conta, diferente da conta da proposta. Nada foi enviado.")
            if self._account_kind() != "demo":
                raise MT5Error("Ordens só na conta demo: a conta conectada não é demo. Nada foi enviado.")
            raw = object.__getattribute__(self._module(), "_raw")
            try:
                return getattr(raw, name)(request)
            except Exception as exc:  # noqa: BLE001 - a extensão em C pode lançar qualquer coisa
                raise MT5Error(f"Erro do MetaTrader 5 em {name}() ({type(exc).__name__}): {exc}") from exc

    def constant(self, name: str) -> int:
        """Constante do módulo MetaTrader5 (ex.: TRADE_ACTION_DEAL)."""
        if not (name[:1].isalpha() and name == name.upper()):
            raise ValueError(f"Constante inválida: {name}")
        return getattr(self._module(), name)

    def check_order(self, request: dict[str, Any], account: tuple[int, str]) -> Any:
        """Validação da ordem pelo servidor, sem enviar (MqlTradeCheckResult; retcode 0 = aceita)."""
        return self._trade("order_check", request, account)

    def send_order(self, request: dict[str, Any], account: tuple[int, str]) -> Any:
        """Envia a ordem (MqlTradeResult). Só a etapa F chama, depois da aprovação do usuário."""
        return self._trade("order_send", request, account)

    @_guard
    def terminal_data_path(self) -> str:
        """Pasta de dados do terminal (onde ficam MQL5\\Files e os serviços)."""
        with self._lock:
            self.ensure_connected()
            t = self._call("terminal_info")
            path = getattr(t, "data_path", None) if t is not None else None
            if not path:
                raise MT5Error(f"O terminal não informou a pasta de dados: {self._last_error()}")
            return str(path)

    @staticmethod
    def _epoch_ms(record: Any, field: str) -> datetime:
        """Horário de um registro do MT5, com milissegundos quando houver (``<field>_msc``)."""
        msc = getattr(record, f"{field}_msc", 0) or 0
        return tempo.from_epoch(msc / 1000 if msc else getattr(record, field))

    @_guard
    def open_positions(self) -> list[dict]:
        """Posições abertas com valores brutos: ``lado`` buy/sell, ``abertura`` em datetime UTC.

        ``preco_atual`` é o preço de fechamento da posição (bid na compra, ask na venda); ``lucro``
        não inclui swap nem comissão. Stop/alvo ausentes saem como None (o MT5 usa 0).
        """
        with self._lock:
            self.ensure_connected()
            mt5 = self._module()
            items = self._call("positions_get")
            if items is None:
                raise MT5Error(f"Não foi possível ler as posições: {self._last_error()}")
            buy = mt5.POSITION_TYPE_BUY
            return [
                {
                    "ticket": p.ticket,
                    # Identificador da posição = ticket da ordem que a abriu.
                    "identificador": getattr(p, "identifier", p.ticket),
                    "simbolo": p.symbol,
                    "lado": "buy" if p.type == buy else "sell",
                    "volume": p.volume,
                    "preco_abertura": p.price_open,
                    "preco_atual": p.price_current,
                    "stop_loss": p.sl or None,
                    "take_profit": p.tp or None,
                    "lucro": p.profit,
                    "swap": p.swap,
                    "abertura": self._epoch_ms(p, "time"),
                }
                for p in items
            ]

    @_guard
    def positions(self) -> list[dict]:
        """Posições abertas (resumo para exibição)."""
        return [
            {
                "ticket": p["ticket"],
                "simbolo": p["simbolo"],
                "tipo": "compra" if p["lado"] == "buy" else "venda",
                "volume": p["volume"],
                "preco_abertura": p["preco_abertura"],
                "preco_atual": p["preco_atual"],
                "stop_loss": p["stop_loss"] or 0.0,
                "take_profit": p["take_profit"] or 0.0,
                "lucro": p["lucro"],
                "swap": p["swap"],
                "abertura": tempo.exibicao(p["abertura"]),
            }
            for p in self.open_positions()
        ]

    @_guard
    def pending_orders(self) -> list[dict]:
        """Ordens ativas (pendentes ou em processamento) com valores brutos.

        ``preco`` é o preço de ativação; na stop limitada, ``preco_limite`` é o preço da ordem limitada
        colocada quando o stop é atingido. ``lado`` é None para ordens que fecham posição (fechamento por
        oposta ou ordem a mercado ligada a uma posição). ``preco_atual`` é None sem cotação.
        """
        with self._lock:
            self.ensure_connected()
            mt5 = self._module()
            items = self._call("orders_get")
            if items is None:
                raise MT5Error(f"Não foi possível ler as ordens pendentes: {self._last_error()}")
            kinds = {getattr(mt5, const): kind for const, kind in ORDER_KINDS.items() if hasattr(mt5, const)}
            times = {getattr(mt5, const): text for const, text in ORDER_TIMES.items() if hasattr(mt5, const)}
            market = {mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL}
            out: list[dict] = []
            for o in items:
                side, kind = kinds.get(o.type, (None, f"tipo {o.type} desconhecido"))
                position_id = getattr(o, "position_id", 0) or 0
                if o.type in market and position_id:
                    side, kind = None, f"fechamento da posição {position_id} (em processamento)"
                expiration = getattr(o, "time_expiration", 0) or 0
                out.append(
                    {
                        "ticket": o.ticket,
                        "simbolo": o.symbol,
                        "lado": side,
                        "tipo": kind,
                        "volume_inicial": getattr(o, "volume_initial", o.volume_current),
                        "volume_atual": o.volume_current,
                        "preco": o.price_open,
                        "preco_limite": getattr(o, "price_stoplimit", 0.0) or None,
                        "stop_loss": o.sl or None,
                        "take_profit": o.tp or None,
                        "preco_atual": o.price_current or None,
                        "colocada": self._epoch_ms(o, "time_setup"),
                        "validade": times.get(getattr(o, "type_time", None), "não informada"),
                        "expira": tempo.from_epoch(expiration) if expiration else None,
                    }
                )
            return out

    @_guard
    def profit(self, symbol: str, side: str, volume: float, price_open: float, price_close: float) -> float:
        """Resultado (moeda da conta) de ``volume`` lotes entre dois preços; negativo = perda."""
        side_key = (side or "").strip().lower() if isinstance(side, str) else ""
        if side_key not in ("buy", "sell"):
            raise ValueError(f"side deve ser 'buy' ou 'sell', recebido: {side!r}.")
        if price_open == price_close or volume == 0:
            return 0.0
        with self._lock:
            resolved = self.resolve_symbol(symbol)
            mt5 = self._module()
            action = mt5.ORDER_TYPE_BUY if side_key == "buy" else mt5.ORDER_TYPE_SELL
            value = self._call("order_calc_profit", action, resolved, float(volume), float(price_open), float(price_close))
            if value:
                return float(value)
            info = self._info(resolved)
        log.info("order_calc_profit indisponível para %s; usando tick_value", resolved)
        gain = (price_close > price_open) == (side_key == "buy")
        field = "trade_tick_value_profit" if gain else "trade_tick_value_loss"
        tick_value = getattr(info, field, 0.0) or info.trade_tick_value
        try:
            return risk.profit_from_ticks(side_key, volume, price_open, price_close, info.trade_tick_size, tick_value)
        except ValueError as exc:
            raise MT5Error(f"Sem valor do tick para calcular o resultado em {resolved}: {exc}") from exc

    # ------------------------------------------------------------------ histórico
    def _deal_record(self, d: Any, types: dict, entries: dict, reasons: dict) -> dict:
        return {
            "ticket": d.ticket,
            "ordem": d.order,
            "posicao_id": d.position_id,
            "horario": self._epoch_ms(d, "time"),
            "tipo": types.get(d.type, "outro"),
            "entrada": entries.get(d.entry, "desconhecida"),
            "motivo": reasons.get(d.reason, "desconhecido"),
            "volume": d.volume,
            "preco": d.price,
            "comissao": d.commission,
            "swap": d.swap,
            "lucro": d.profit,
            "taxa": getattr(d, "fee", 0.0),
            "simbolo": d.symbol,
        }

    def _deal_maps(self) -> tuple[dict, dict, dict]:
        mt5 = self._module()

        def build(table: dict[str, str]) -> dict:
            return {getattr(mt5, const): name for const, name in table.items() if hasattr(mt5, const)}

        return build(DEAL_TYPES), build(DEAL_ENTRIES), build(DEAL_REASONS)

    @_guard
    def deals(self, start: datetime | None = None, end: datetime | None = None, *, position: int | None = None) -> list[dict]:
        """Negócios (execuções) do histórico: por intervalo UTC ou de uma posição, do mais antigo ao mais novo.

        As datas vão com fuso: datetime sem fuso é lido pelo MT5 como horário local do Windows
        (conferido em 2026-10-02: a janela 04–05 UTC sem fuso não trouxe nada).
        """
        with self._lock:
            self.ensure_connected()
            if position is not None:
                items = self._call("history_deals_get", position=int(position))
            else:
                if start is None or end is None or start.tzinfo is None or end.tzinfo is None:
                    raise ValueError("Informe início e fim do histórico com fuso horário (UTC).")
                items = self._call("history_deals_get", start.astimezone(timezone.utc), end.astimezone(timezone.utc))
            if items is None:
                raise MT5Error(f"Não foi possível ler o histórico de negócios: {self._last_error()}")
            maps = self._deal_maps()
            out = [self._deal_record(d, *maps) for d in items]
        return sorted(out, key=lambda d: (d["horario"], d["ticket"]))

    @_guard
    def position_orders(self, position: int) -> list[dict]:
        """Ordens do histórico ligadas a uma posição (preço, stop e alvo como foram enviados)."""
        with self._lock:
            self.ensure_connected()
            items = self._call("history_orders_get", position=int(position))
            if items is None:
                raise MT5Error(f"Não foi possível ler as ordens da posição {position}: {self._last_error()}")
            return [
                {
                    "ticket": o.ticket,
                    "tipo": o.type,
                    "colocada": self._epoch_ms(o, "time_setup"),
                    "preco": o.price_open,
                    "stop_loss": o.sl or None,
                    "take_profit": o.tp or None,
                }
                for o in items
            ]
