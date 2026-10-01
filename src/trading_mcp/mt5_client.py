"""Cliente somente leitura do MetaTrader 5 (thread-safe, conexão preguiçosa)."""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from trading_mcp import risk
from trading_mcp.config import Settings

log = logging.getLogger(__name__)

TIMEFRAMES: dict[str, str] = {
    "M1": "TIMEFRAME_M1",
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
        "copy_rates_from_pos",
        "order_calc_profit",
        "order_calc_margin",
        "positions_get",
    }
)

_WARMUP_TIMEOUT_S = 10.0
_WARMUP_STEP_S = 0.5
_TICK_ATTEMPTS = 3
_TICK_RETRY_S = 0.3
_RATES_ATTEMPTS = 3
_RATES_RETRY_S = 0.5
_WEEKEND_WARNING = (
    "Mercado provavelmente fechado (fim de semana): esta é a última cotação disponível; veja horario_servidor."
)


class MT5Error(Exception):
    """Erro ao falar com o MetaTrader 5."""


class SymbolNotFoundError(MT5Error):
    """Símbolo não encontrado na corretora."""


class _ReadOnlyModule:
    """Proxy do módulo MetaTrader5: só expõe funções de leitura e constantes MAIÚSCULAS."""

    __slots__ = ("_raw",)

    def __init__(self, raw: Any) -> None:
        object.__setattr__(self, "_raw", raw)

    def __getattr__(self, name: str) -> Any:
        if name in _ALLOWED_CALLS or (name[:1].isalpha() and name == name.upper()):
            return getattr(object.__getattribute__(self, "_raw"), name)
        raise MT5Error(f"Operação não permitida (servidor somente leitura): {name}")


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


def _fmt_time(epoch: int | float) -> str:
    """Formata um epoch do MT5 (que já representa o horário do servidor)."""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _is_weekend(now: datetime) -> bool:
    wd = now.weekday()  # segunda=0
    return wd == 5 or (wd == 6 and now.hour < 21) or (wd == 4 and now.hour >= 21)


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

    @_guard
    def ensure_connected(self) -> None:
        """Inicializa o MT5 se necessário (ou se o terminal foi fechado/reiniciado)."""
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
                if self._call("terminal_info") is not None:
                    return
                log.warning("Terminal MT5 não responde; reconectando")
                try:
                    self._call("shutdown")
                except MT5Error:
                    pass
            self._initialized = False
            self._symbol_cache.clear()
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
            if s.mt5_login is not None:
                acc = self._call("account_info")
                actual = getattr(acc, "login", None)
                if actual != s.mt5_login:
                    try:
                        self._call("shutdown")
                    except MT5Error:
                        pass
                    raise MT5Error(
                        f"O terminal MT5 está logado na conta {actual}, diferente de MT5_LOGIN ({s.mt5_login}). "
                        "Conexão encerrada; confira MT5_PATH, MT5_LOGIN e MT5_SERVER."
                    )
            self._initialized = True

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
            }

    # ------------------------------------------------------------------ mercado
    @staticmethod
    def _tick_valid(tick: Any) -> bool:
        return tick is not None and tick.time != 0 and tick.bid > 0 and tick.ask > 0

    @_guard
    def quote(self, symbol: str) -> dict:
        """Cotação atual com spread em preço, pontos e (forex) pips."""
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
            result = {
                "simbolo": resolved,
                "bid": round(tick.bid, digits),
                "ask": round(tick.ask, digits),
                "spread_preco": round(spread, digits),
                "spread_pontos": spread_points,
                "spread_pips": spread_pips,
                "horario_servidor": _fmt_time(tick.time),
            }
            if "crypto" not in str(getattr(info, "path", "") or "").lower() and _is_weekend(self._now_utc()):
                result["aviso"] = _WEEKEND_WARNING
            return result

    @_guard
    def rates(self, symbol: str, timeframe: str, count: int, include_current: bool = True) -> pd.DataFrame:
        """Candles do mais antigo ao mais novo; `time` em horário do servidor (naive)."""
        tf_key = (timeframe or "").strip().upper()
        if tf_key not in TIMEFRAMES:
            raise ValueError(f"Timeframe inválido: '{timeframe}'. Válidos: {', '.join(TIMEFRAMES)}.")
        count = max(1, min(int(count), self._settings.max_bars))
        with self._lock:
            resolved = self.resolve_symbol(symbol)
            tf = getattr(self._module(), TIMEFRAMES[tf_key])
            data = None
            for attempt in range(_RATES_ATTEMPTS):
                if attempt:
                    self._sleep(_RATES_RETRY_S)
                data = self._call("copy_rates_from_pos", resolved, tf, 0 if include_current else 1, count)
                if data is not None and len(data) > 0:
                    break
            else:
                raise MT5Error(f"Sem candles para {resolved} em {tf_key}: {self._last_error()}")
        df = pd.DataFrame(data)[_RATE_COLUMNS]
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df.reset_index(drop=True)

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
            mt5 = self._module()
            a = self._call("account_info")
            if a is None:
                raise MT5Error(f"Não foi possível ler a conta: {self._last_error()}")
            modes = {
                mt5.ACCOUNT_TRADE_MODE_DEMO: "demo",
                mt5.ACCOUNT_TRADE_MODE_CONTEST: "concurso",
                mt5.ACCOUNT_TRADE_MODE_REAL: "real",
            }
            kind = modes.get(a.trade_mode, "desconhecido")
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
            }

    @_guard
    def positions(self) -> list[dict]:
        """Posições abertas."""
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
                    "simbolo": p.symbol,
                    "tipo": "compra" if p.type == buy else "venda",
                    "volume": p.volume,
                    "preco_abertura": p.price_open,
                    "preco_atual": p.price_current,
                    "stop_loss": p.sl,
                    "take_profit": p.tp,
                    "lucro": p.profit,
                    "swap": p.swap,
                    "abertura": _fmt_time(p.time),
                }
                for p in items
            ]
