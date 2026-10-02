"""Substituto do módulo MetaTrader5 para testes, sem terminal real.

Reproduz a superfície da API usada pelo projeto: os objetos retornados expõem
atributos (como os namedtuples do MT5) e `_asdict()`. `order_send` falha de
propósito: a fase 1 é só leitura e nenhum código pode enviar ordens.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import numpy as np

# Constantes com os mesmos valores do pacote MetaTrader5 5.0.6231.
TIMEFRAME_M1, TIMEFRAME_M2, TIMEFRAME_M3, TIMEFRAME_M4, TIMEFRAME_M5 = 1, 2, 3, 4, 5
TIMEFRAME_M6, TIMEFRAME_M10, TIMEFRAME_M12, TIMEFRAME_M15 = 6, 10, 12, 15
TIMEFRAME_M20, TIMEFRAME_M30 = 20, 30
TIMEFRAME_H1, TIMEFRAME_H2, TIMEFRAME_H3, TIMEFRAME_H4 = 16385, 16386, 16387, 16388
TIMEFRAME_H6, TIMEFRAME_H8, TIMEFRAME_H12 = 16390, 16392, 16396
TIMEFRAME_D1, TIMEFRAME_W1, TIMEFRAME_MN1 = 16408, 32769, 49153

ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
COPY_TICKS_ALL, COPY_TICKS_INFO, COPY_TICKS_TRADE = -1, 1, 2
TICK_DTYPE = np.dtype(
    [("time", "<i8"), ("bid", "<f8"), ("ask", "<f8"), ("last", "<f8"), ("volume", "<u8"), ("time_msc", "<i8"),
     ("flags", "<u4"), ("volume_real", "<f8")]
)
ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 2, 3, 4, 5
ORDER_TYPE_BUY_STOP_LIMIT, ORDER_TYPE_SELL_STOP_LIMIT, ORDER_TYPE_CLOSE_BY = 6, 7, 8
ORDER_TIME_GTC, ORDER_TIME_DAY, ORDER_TIME_SPECIFIED, ORDER_TIME_SPECIFIED_DAY = 0, 1, 2, 3
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
DEAL_TYPE_BUY, DEAL_TYPE_SELL, DEAL_TYPE_BALANCE, DEAL_TYPE_CREDIT, DEAL_TYPE_CHARGE = 0, 1, 2, 3, 4
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
DEAL_REASON_CLIENT, DEAL_REASON_MOBILE, DEAL_REASON_WEB, DEAL_REASON_EXPERT = 0, 1, 2, 3
DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO = 4, 5, 6
DEAL_REASON_ROLLOVER, DEAL_REASON_VMARGIN, DEAL_REASON_SPLIT = 7, 8, 9

ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2

SYMBOL_CALC_MODE_FOREX = 0
SYMBOL_CALC_MODE_FUTURES = 1
SYMBOL_CALC_MODE_CFD = 2
SYMBOL_CALC_MODE_CFDINDEX = 3
SYMBOL_CALC_MODE_CFDLEVERAGE = 4
SYMBOL_CALC_MODE_FOREX_NO_LEVERAGE = 5

RATES_DTYPE = np.dtype(
    [
        ("time", "<i8"),
        ("open", "<f8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("close", "<f8"),
        ("tick_volume", "<u8"),
        ("spread", "<i4"),
        ("real_volume", "<u8"),
    ]
)


class Record(SimpleNamespace):
    """Imita os namedtuples do MT5 (acesso por atributo + `_asdict`)."""

    def _asdict(self) -> dict[str, Any]:
        return dict(vars(self))


def make_symbol(
    name: str,
    *,
    description: str = "",
    path: str = "Forex\\Majors",
    visible: bool = True,
    digits: int = 5,
    point: float = 0.00001,
    bid: float = 1.10000,
    ask: float = 1.10012,
    trade_tick_size: float = 0.00001,
    trade_tick_value: float = 1.0,
    trade_contract_size: float = 100_000.0,
    volume_min: float = 0.01,
    volume_max: float = 200.0,
    volume_step: float = 0.01,
    currency_base: str = "EUR",
    currency_profit: str = "USD",
    currency_margin: str = "EUR",
    trade_calc_mode: int = SYMBOL_CALC_MODE_FOREX,
) -> Record:
    return Record(
        name=name,
        description=description,
        path=path,
        visible=visible,
        digits=digits,
        point=point,
        bid=bid,
        ask=ask,
        trade_tick_size=trade_tick_size,
        trade_tick_value=trade_tick_value,
        trade_tick_value_profit=trade_tick_value,
        trade_tick_value_loss=trade_tick_value,
        trade_contract_size=trade_contract_size,
        volume_min=volume_min,
        volume_max=volume_max,
        volume_step=volume_step,
        currency_base=currency_base,
        currency_profit=currency_profit,
        currency_margin=currency_margin,
        trade_calc_mode=trade_calc_mode,
    )


def make_position(
    ticket: int,
    symbol: str,
    side: str,
    volume: float,
    price_open: float,
    price_current: float,
    *,
    sl: float = 0.0,
    tp: float = 0.0,
    profit: float = 0.0,
    swap: float = 0.0,
    time: int = 1_700_000_000,
) -> Record:
    """Posição como a de positions_get (o MT5 usa 0 para stop/alvo ausentes)."""
    return Record(
        ticket=ticket, time=time, time_msc=time * 1000, time_update=time, time_update_msc=time * 1000,
        type=POSITION_TYPE_BUY if side == "buy" else POSITION_TYPE_SELL, magic=0, identifier=ticket, reason=0,
        volume=volume, price_open=price_open, sl=sl, tp=tp, price_current=price_current, swap=swap,
        profit=profit, symbol=symbol, comment="", external_id="",
    )


def make_order(
    ticket: int,
    symbol: str,
    order_type: int,
    volume: float,
    price_open: float,
    price_current: float,
    *,
    sl: float = 0.0,
    tp: float = 0.0,
    price_stoplimit: float = 0.0,
    volume_initial: float | None = None,
    type_time: int = ORDER_TIME_GTC,
    time_expiration: int = 0,
    time_setup: int = 1_700_000_000,
    position_id: int = 0,
) -> Record:
    """Ordem como a de orders_get."""
    return Record(
        ticket=ticket, time_setup=time_setup, time_setup_msc=time_setup * 1000, time_done=0, time_done_msc=0,
        time_expiration=time_expiration, type=order_type, type_time=type_time, type_filling=2, state=1, magic=0,
        position_id=position_id, position_by_id=0, reason=0,
        volume_initial=volume if volume_initial is None else volume_initial, volume_current=volume,
        price_open=price_open, sl=sl, tp=tp, price_current=price_current, price_stoplimit=price_stoplimit,
        symbol=symbol, comment="", external_id="",
    )


def make_deal(
    ticket: int,
    position_id: int,
    symbol: str,
    side: str,
    entry: int,
    volume: float,
    price: float,
    time: int,
    *,
    order: int | None = None,
    reason: int = DEAL_REASON_CLIENT,
    profit: float = 0.0,
    commission: float = 0.0,
    swap: float = 0.0,
    fee: float = 0.0,
) -> Record:
    """Negócio como o de history_deals_get."""
    return Record(
        ticket=ticket, order=order if order is not None else ticket, time=time, time_msc=time * 1000,
        type=DEAL_TYPE_BUY if side == "buy" else DEAL_TYPE_SELL, entry=entry, magic=0, position_id=position_id,
        reason=reason, volume=volume, price=price, commission=commission, swap=swap, profit=profit, fee=fee,
        symbol=symbol, comment="", external_id="",
    )


def make_balance_deal(ticket: int, amount: float, time: int, deal_type: int = DEAL_TYPE_BALANCE) -> Record:
    """Lançamento fora das operações: depósito/saque (padrão), crédito, tarifa etc."""
    return Record(
        ticket=ticket, order=0, time=time, time_msc=time * 1000, type=deal_type, entry=DEAL_ENTRY_IN, magic=0,
        position_id=0, reason=0, volume=0.0, price=0.0, commission=0.0, swap=0.0, profit=amount, fee=0.0, symbol="",
        comment="deposit", external_id="",
    )


def make_ticks(rows: list[tuple[float, float, float]]) -> np.ndarray:
    """Ticks a partir de (epoch em segundos, bid, ask)."""
    return np.array(
        [(int(t), bid, ask, 0.0, 0, int(t * 1000), 6, 0.0) for t, bid, ask in rows], dtype=TICK_DTYPE
    )


def make_rates(closes: list[float], *, start_time: int = 1_700_000_000, step_seconds: int = 3600, spread: int = 12):
    """Gera candles a partir de uma lista de fechamentos (open = fechamento anterior)."""
    rows = []
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) + 0.0005
        low = min(prev, close) - 0.0005
        rows.append((start_time + i * step_seconds, prev, high, low, close, 100 + i, spread, 0))
        prev = close
    return np.array(rows, dtype=RATES_DTYPE)


class FakeMT5:
    """Instância usada no lugar do módulo: `MT5Client(settings, mt5_module=FakeMT5(...))`."""

    def __init__(
        self,
        symbols: list[Record] | None = None,
        rates: dict[str, np.ndarray] | None = None,
        *,
        trade_mode: int = ACCOUNT_TRADE_MODE_DEMO,
        balance: float = 10_000.0,
        currency: str = "USD",
        initialize_ok: bool = True,
        connected: bool = True,
        positions: list[Record] | None = None,
        orders: list[Record] | None = None,
        deals: list[Record] | None = None,
        history_orders: list[Record] | None = None,
        ticks: dict[str, np.ndarray] | None = None,
        login: int = 12345678,
        zero_tick_symbols: set[str] | None = None,
        rates_fail_times: int = 0,
        connect_after_polls: int = 0,
        server: str = "Exness-MT5Trial",
        maxbars: int = 100_000,
        tradeapi_disabled: bool = False,
        now: Callable[[], float] | None = None,
        tick_age_s: dict[str, float] | None = None,
    ) -> None:
        # Copia as constantes do módulo para a instância (para `mt5.TIMEFRAME_H1` etc.).
        for key, value in globals().items():
            if key.isupper():
                setattr(self, key, value)
        self.symbols = {s.name: s for s in (symbols or [])}
        self.selected: set[str] = {s.name for s in (symbols or []) if s.visible}
        self.rates = rates or {}
        self.trade_mode = trade_mode
        self.balance = balance
        self.currency = currency
        self.initialize_ok = initialize_ok
        self.connected = connected
        self.positions = positions or []
        self.orders = orders or []
        self.deals = deals or []
        self.history_orders = history_orders or []
        self.ticks = ticks or {}
        # Candles por (símbolo, timeframe) para todas as leituras de candles; sem entrada, vale `rates` do símbolo.
        self.rates_tf: dict[tuple[str, int], np.ndarray] = {}
        self.login = login
        self.zero_tick_symbols: set[str] = set(zero_tick_symbols or ())
        self.rates_fail_times = rates_fail_times
        self.connect_after_polls = connect_after_polls
        self.server = server
        self.maxbars = maxbars
        self.tradeapi_disabled = tradeapi_disabled
        self.data_path = "C:/fake/MetaQuotes/Terminal/ABC"
        # Relógio dos ticks (epoch UTC); os testes o alinham ao relógio do cliente.
        self.now: Callable[[], float] = now or _time.time
        # Idade (s) do último tick por símbolo; ausente = tick de agora.
        self.tick_age_s: dict[str, float] = dict(tick_age_s or {})
        self.rates_calls = 0
        self.range_calls: list[tuple[Any, ...]] = []
        self.from_calls: list[tuple[Any, ...]] = []
        self.terminal_polls = 0
        self.shutdown_calls = 0
        self.initialized = False
        self.initialize_calls: list[dict[str, Any]] = []
        self.calc_profit_returns_none = False
        self._last_error: tuple[int, str] = (1, "Success")

    # --- conexão ---------------------------------------------------------
    def initialize(self, path: str | None = None, **kwargs: Any) -> bool:
        call = dict(kwargs)
        if path is not None:
            call["path"] = path
        self.initialize_calls.append(call)
        if not self.initialize_ok:
            self._last_error = (-10003, "IPC initialize failed, MetaTrader 5 x64 not found")
            return False
        self.initialized = True
        self._last_error = (1, "Success")
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.initialized = False

    def last_error(self) -> tuple[int, str]:
        return self._last_error

    def terminal_info(self) -> Record | None:
        if not self.initialized:
            return None
        self.terminal_polls += 1
        connected = self.connected and self.terminal_polls > self.connect_after_polls
        return Record(
            connected=connected,
            trade_allowed=False,
            tradeapi_disabled=self.tradeapi_disabled,
            maxbars=self.maxbars,
            data_path=self.data_path,
            name="MetaTrader 5 (fake)",
            build=5000,
        )

    def account_info(self) -> Record | None:
        if not self.initialized:
            return None
        return Record(
            login=self.login,
            trade_mode=self.trade_mode,
            leverage=200,
            balance=self.balance,
            equity=self.balance,
            margin=0.0,
            margin_free=self.balance,
            margin_level=0.0,
            currency=self.currency,
            server=self.server,
            company="Exness",
            name="Conta Teste",
        )

    # --- símbolos --------------------------------------------------------
    def symbols_get(self, group: str | None = None) -> tuple[Record, ...] | None:
        if not self.initialized:
            return None
        return tuple(self.symbols.values())

    def symbol_info(self, name: str) -> Record | None:
        if not self.initialized:
            return None
        return self.symbols.get(name)

    def symbol_select(self, name: str, enable: bool = True) -> bool:
        if name not in self.symbols:
            return False
        if enable:
            self.selected.add(name)
        else:
            self.selected.discard(name)
        return True

    def symbol_info_tick(self, name: str) -> Record | None:
        if not self.initialized or name not in self.symbols or name not in self.selected:
            return None
        s = self.symbols[name]
        if name in self.zero_tick_symbols:
            return Record(time=0, bid=0.0, ask=0.0, last=0.0, volume=0, time_msc=0, flags=0, volume_real=0.0)
        msc = int((self.now() - self.tick_age_s.get(name, 0.0)) * 1000)
        return Record(time=msc // 1000, bid=s.bid, ask=s.ask, last=0.0, volume=0, time_msc=msc, flags=6, volume_real=0.0)

    # --- dados -----------------------------------------------------------
    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int):
        self.rates_calls += 1
        if self.rates_calls <= self.rates_fail_times:
            self._last_error = (-2, "Terminal: history not synchronized yet")
            return None
        if not self.initialized or (symbol not in self.rates and (symbol, timeframe) not in self.rates_tf):
            self._last_error = (-4, "Terminal: Not found")
            return None
        if count >= self.maxbars:  # como no terminal real: pedir maxbars ou mais falha
            self._last_error = (-2, "Terminal: Invalid params")
            return None
        data = self.rates_tf.get((symbol, timeframe), self.rates.get(symbol))
        end = len(data) - start_pos
        if end <= 0:
            return np.array([], dtype=RATES_DTYPE)
        begin = max(0, end - count)
        return data[begin:end].copy()

    def copy_rates_from(self, symbol: str, timeframe: int, date_from: datetime, count: int):
        """Como no terminal real: os ``count`` candles que abriram até ``date_from``, inclusive o que estava aberto."""
        self.from_calls.append((symbol, timeframe, date_from, count))
        if not self.initialized:
            return None
        if date_from.tzinfo is None:
            raise AssertionError("copy_rates_from recebeu datetime sem fuso")
        data = self.rates_tf.get((symbol, timeframe), self.rates.get(symbol, np.array([], dtype=RATES_DTYPE)))
        return data[data["time"] <= int(date_from.timestamp())][-count:].copy()

    def copy_rates_range(self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime):
        self.range_calls.append((symbol, timeframe, date_from, date_to))
        if not self.initialized:
            return None
        # O MT5 real lê datetime sem fuso como horário local do Windows.
        if date_from.tzinfo is None or date_to.tzinfo is None:
            raise AssertionError("copy_rates_range recebeu datetime sem fuso")
        data = self.rates_tf.get((symbol, timeframe), self.rates.get(symbol, np.array([], dtype=RATES_DTYPE)))
        lo, hi = int(date_from.timestamp()), int(date_to.timestamp())
        return data[(data["time"] >= lo) & (data["time"] <= hi)].copy()

    # --- cálculos --------------------------------------------------------
    def order_calc_profit(self, action: int, symbol: str, volume: float, price_open: float, price_close: float):
        if self.calc_profit_returns_none or symbol not in self.symbols:
            return None
        s = self.symbols[symbol]
        direction = 1 if action == ORDER_TYPE_BUY else -1
        return direction * (price_close - price_open) / s.trade_tick_size * s.trade_tick_value * volume

    def order_calc_margin(self, action: int, symbol: str, volume: float, price: float):
        if symbol not in self.symbols:
            return None
        s = self.symbols[symbol]
        return s.trade_contract_size * volume * price / 200

    def positions_get(self, symbol: str | None = None, **kwargs: Any):
        if not self.initialized:
            return None
        items = self.positions if symbol is None else [p for p in self.positions if p.symbol == symbol]
        return tuple(items)

    def orders_get(self, symbol: str | None = None, **kwargs: Any):
        if not self.initialized:
            return None
        items = self.orders if symbol is None else [o for o in self.orders if o.symbol == symbol]
        return tuple(items)

    def copy_ticks_range(self, symbol: str, date_from: datetime, date_to: datetime, flags: int):
        if not self.initialized:
            return None
        if date_from.tzinfo is None or date_to.tzinfo is None:
            raise AssertionError("copy_ticks_range recebeu datetime sem fuso")
        data = self.ticks.get(symbol, np.array([], dtype=TICK_DTYPE))
        lo, hi = int(date_from.timestamp() * 1000), int(date_to.timestamp() * 1000)
        return data[(data["time_msc"] >= lo) & (data["time_msc"] <= hi)].copy()

    def history_deals_get(self, date_from: Any = None, date_to: Any = None, *, position: int | None = None, **kwargs: Any):
        if not self.initialized:
            return None
        if position is not None:
            return tuple(d for d in self.deals if d.position_id == position)
        # O MT5 real lê datetime sem fuso como horário local do Windows: o cliente nunca pode mandar assim.
        for moment in (date_from, date_to):
            if isinstance(moment, datetime) and moment.tzinfo is None:
                raise AssertionError("history_deals_get recebeu datetime sem fuso")
        lo, hi = int(date_from.timestamp()), int(date_to.timestamp())
        return tuple(d for d in self.deals if lo <= d.time <= hi)

    def history_orders_get(self, date_from: Any = None, date_to: Any = None, *, position: int | None = None, **kwargs: Any):
        if not self.initialized:
            return None
        if position is not None:
            return tuple(o for o in self.history_orders if o.position_id == position)
        lo, hi = int(date_from.timestamp()), int(date_to.timestamp())
        return tuple(o for o in self.history_orders if lo <= o.time_setup <= hi)

    # --- proibido na fase 1 ---------------------------------------------
    def order_send(self, *args: Any, **kwargs: Any):
        raise AssertionError("order_send nunca pode ser chamado: a fase 1 é somente leitura")

    def order_check(self, *args: Any, **kwargs: Any):
        raise AssertionError("order_check não é usado na fase 1")
