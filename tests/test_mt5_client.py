from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import fake_mt5 as fm
from fake_mt5 import FakeMT5, Record, make_rates, make_symbol
from trading_mcp.config import Settings
from trading_mcp.mt5_client import TIMEFRAMES, MT5Client, MT5Error, SymbolNotFoundError


class FakeClock:
    """Relógio falso: sleep avança o tempo sem dormir."""

    def __init__(self, now: datetime | None = None) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []
        self.now = now or datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)  # quarta-feira
        self.on_sleep = None

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        if self.on_sleep:
            self.on_sleep()

    def monotonic(self) -> float:
        return self.t

    def now_utc(self) -> datetime:
        return self.now


def make_client(fake: FakeMT5 | None = None, **settings) -> tuple[MT5Client, FakeMT5]:
    fake = fake or FakeMT5([make_symbol("EURUSD")])
    clock = FakeClock()
    client = MT5Client(
        Settings(**settings), mt5_module=fake, sleep=clock.sleep, monotonic=clock.monotonic, now_utc=clock.now_utc
    )
    client.clock = clock  # type: ignore[attr-defined]
    return client, fake


# ---------------------------------------------------------------- conexão
def test_initialize_passes_only_set_values():
    client, fake = make_client()
    client.quote("EURUSD")
    assert fake.initialize_calls == [{"timeout": 60_000}]


def test_initialize_passes_all_settings_including_password():
    client, fake = make_client(
        FakeMT5(login=123), mt5_path="C:/mt5/terminal64.exe", mt5_login=123, mt5_password="s3cret", mt5_server="Exness-MT5Trial"
    )
    client.ensure_connected()
    assert fake.initialize_calls == [
        {"path": "C:/mt5/terminal64.exe", "login": 123, "password": "s3cret", "server": "Exness-MT5Trial", "timeout": 60_000}
    ]


def test_password_never_logged(caplog):
    client, _ = make_client(FakeMT5(login=1), mt5_password="s3cret", mt5_login=1, mt5_path="C:/mt5/terminal64.exe")
    with caplog.at_level("DEBUG"):
        client.ensure_connected()
    assert "s3cret" not in caplog.text


def test_initialize_failure_message():
    client, _ = make_client(FakeMT5(initialize_ok=False))
    with pytest.raises(MT5Error) as exc:
        client.ensure_connected()
    msg = str(exc.value)
    assert "IPC initialize failed" in msg and "MT5_PATH" in msg and "aberto" in msg


def test_terminal_disconnected():
    client, _ = make_client(FakeMT5(connected=False))
    with pytest.raises(MT5Error, match="sem conexão com o servidor da corretora"):
        client.ensure_connected()


def test_reconnects_when_terminal_info_none():
    client, fake = make_client()
    client.ensure_connected()
    client.ensure_connected()
    assert len(fake.initialize_calls) == 1
    fake.initialized = False  # terminal fechado/reiniciado
    client.quote("EURUSD")
    assert len(fake.initialize_calls) == 2


def test_missing_module_raises_mt5error(monkeypatch):
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)  # import -> ImportError
    client = MT5Client(Settings())
    with pytest.raises(MT5Error, match="pip install MetaTrader5"):
        client.ensure_connected()


# ---------------------------------------------------------------- símbolos
def test_exact_symbol_and_symbol_select_called():
    fake = FakeMT5([make_symbol("EURUSD", visible=False)])
    client, _ = make_client(fake)
    assert client.resolve_symbol(" eurusd ") == "EURUSD"
    assert "EURUSD" in fake.selected


def test_suffix_from_settings():
    fake = FakeMT5([make_symbol("EURUSDm"), make_symbol("EURUSDc")])
    client, _ = make_client(fake, symbol_suffix="m")
    assert client.resolve_symbol("EURUSD") == "EURUSDm"


def test_suffix_auto_detection():
    client, _ = make_client(FakeMT5([make_symbol("EURUSDm"), make_symbol("GBPUSD.")]))
    assert client.resolve_symbol("eurusd") == "EURUSDm"
    assert client.resolve_symbol("GBPUSD") == "GBPUSD."


def test_case_insensitive_full_name():
    client, _ = make_client(FakeMT5([make_symbol("EURUSDm")]))
    assert client.resolve_symbol("eurusdM") == "EURUSDm"


def test_eur_does_not_resolve_to_eurusd():
    client, _ = make_client(FakeMT5([make_symbol("EURUSD"), make_symbol("EURGBP")]))
    with pytest.raises(SymbolNotFoundError) as exc:
        client.resolve_symbol("EUR")
    assert "EURUSD" in str(exc.value) and "EURGBP" in str(exc.value)


def test_ambiguity_lists_candidates():
    client, _ = make_client(FakeMT5([make_symbol("EURUSDm", visible=False), make_symbol("EURUSDc", visible=False)]))
    with pytest.raises(MT5Error, match="ambíguo") as exc:
        client.resolve_symbol("EURUSD")
    assert "EURUSDm" in str(exc.value) and "EURUSDc" in str(exc.value)
    assert not isinstance(exc.value, SymbolNotFoundError)


def test_ambiguity_resolved_by_visible():
    client, _ = make_client(FakeMT5([make_symbol("EURUSDm", visible=True), make_symbol("EURUSDc", visible=False)]))
    assert client.resolve_symbol("EURUSD") == "EURUSDm"


def test_not_found_suggestions_capped_at_10():
    syms = [make_symbol(f"XAU{i:02d}") for i in range(15)]
    client, _ = make_client(FakeMT5(syms))
    with pytest.raises(SymbolNotFoundError) as exc:
        client.resolve_symbol("XAU")
    assert str(exc.value).count("XAU") == 10 + 1  # 10 sugestões + eco do termo


def test_not_found_without_suggestions():
    client, _ = make_client()
    with pytest.raises(SymbolNotFoundError, match="não encontrado"):
        client.resolve_symbol("ZZZZ")


def test_resolution_cached():
    fake = FakeMT5([make_symbol("EURUSDm")])
    client, _ = make_client(fake)
    client.resolve_symbol("EURUSD")
    del fake.symbols["EURUSDm"]  # se não houvesse cache, falharia
    assert client.resolve_symbol("EURUSD") == "EURUSDm"


def test_search_symbols():
    fake = FakeMT5(
        [make_symbol("EURUSD", description="Euro vs US Dollar"), make_symbol("AAPL", description="Apple Inc", path="Stocks\\US")]
    )
    client, _ = make_client(fake)
    assert [r["nome"] for r in client.search_symbols("apple")] == ["AAPL"]
    assert [r["nome"] for r in client.search_symbols("eur")] == ["EURUSD"]
    r = client.search_symbols("")[0]
    assert set(r) == {"nome", "descricao", "categoria", "digitos"}
    assert len(client.search_symbols("", limit=1)) == 1


def test_symbol_spec():
    cfd = make_symbol("AAPL", trade_calc_mode=fm.SYMBOL_CALC_MODE_CFD, digits=2, point=0.01)
    client, _ = make_client(FakeMT5([make_symbol("EURUSD"), cfd]))
    s = client.symbol_spec("EURUSD")
    assert s["is_forex"] is True and s["simbolo"] == "EURUSD" and s["volume_step"] == 0.01
    assert {"tick_value_loss", "moeda_lucro", "contrato", "ponto"} <= set(s)
    assert client.symbol_spec("aapl")["is_forex"] is False


# ---------------------------------------------------------------- cotação
def test_quote_forex_5_digits():
    client, _ = make_client()
    q = client.quote("EURUSD")
    assert q["bid"] == 1.1 and q["ask"] == 1.10012
    assert q["spread_pontos"] == 12 and isinstance(q["spread_pontos"], int)
    assert q["spread_pips"] == pytest.approx(1.2)
    assert q["spread_preco"] == pytest.approx(0.00012)
    assert len(q["horario_servidor"]) == 19 and q["horario_servidor"][4] == "-"


def test_quote_forex_3_digits():
    jpy = make_symbol("USDJPY", digits=3, point=0.001, bid=150.000, ask=150.015)
    client, _ = make_client(FakeMT5([jpy]))
    q = client.quote("USDJPY")
    assert q["spread_pontos"] == 15
    assert q["spread_pips"] == pytest.approx(1.5)


def test_quote_cfd_has_no_pips():
    aapl = make_symbol("AAPL", digits=2, point=0.01, bid=200.00, ask=200.10, trade_calc_mode=fm.SYMBOL_CALC_MODE_CFD)
    client, _ = make_client(FakeMT5([aapl]))
    q = client.quote("AAPL")
    assert q["spread_pips"] is None and q["spread_pontos"] == 10


def test_quote_no_tick_raises(monkeypatch):
    client, fake = make_client()
    client.ensure_connected()
    monkeypatch.setattr(fake, "symbol_info_tick", lambda name: None)
    with pytest.raises(MT5Error, match="Sem cotação"):
        client.quote("EURUSD")


# ---------------------------------------------------------------- candles
def rates_client(n: int = 10, **settings):
    closes = [1.10 + i * 0.001 for i in range(n)]
    fake = FakeMT5([make_symbol("EURUSD")], rates={"EURUSD": make_rates(closes)})
    return make_client(fake, **settings)[0], closes


def test_rates_dataframe_shape_and_dtypes():
    client, closes = rates_client()
    df = client.rates("EURUSD", "h1", 10)
    assert list(df.columns) == ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
    assert str(df["time"].dtype).startswith("datetime64") and df["time"].dt.tz is None
    assert df["time"].iloc[0] == pd.Timestamp(1_700_000_000, unit="s")
    assert df["time"].is_monotonic_increasing
    assert list(df.index) == list(range(10))
    assert df["close"].iloc[-1] == pytest.approx(closes[-1])
    assert df["close"].dtype == np.float64


def test_rates_count_clamped_to_max_bars_and_min_one():
    client, _ = rates_client(10, max_bars=4)
    assert len(client.rates("EURUSD", "H1", 100)) == 4
    assert len(client.rates("EURUSD", "H1", 0)) == 1


def test_rates_exclude_current():
    client, closes = rates_client()
    df = client.rates("EURUSD", "H1", 3, include_current=False)
    assert df["close"].iloc[-1] == pytest.approx(closes[-2])
    assert len(df) == 3


def test_rates_invalid_timeframe():
    client, _ = rates_client()
    with pytest.raises(ValueError) as exc:
        client.rates("EURUSD", "H2", 5)
    assert all(tf in str(exc.value) for tf in TIMEFRAMES)


def test_rates_empty_raises():
    client, _ = make_client()  # sem rates
    with pytest.raises(MT5Error, match="Sem candles"):
        client.rates("EURUSD", "H1", 5)


# ---------------------------------------------------------------- risco
def test_loss_per_lot_buy_and_sell():
    client, _ = make_client()
    loss, side = client.loss_per_lot("EURUSD", 1.1000, 1.0990)
    assert side == "buy" and loss == pytest.approx(100.0)
    loss, side = client.loss_per_lot("EURUSD", 1.1000, 1.1010)
    assert side == "sell" and loss == pytest.approx(100.0)


def test_loss_per_lot_fallback_when_calc_profit_none():
    client, fake = make_client()
    fake.calc_profit_returns_none = True
    loss, side = client.loss_per_lot("EURUSD", 1.1000, 1.0990)
    assert side == "buy" and loss == pytest.approx(100.0)


def test_loss_per_lot_equal_entry_stop():
    client, _ = make_client()
    with pytest.raises(ValueError):
        client.loss_per_lot("EURUSD", 1.1, 1.1)


def test_margin_uses_real_volume():
    client, _ = make_client()
    assert client.margin("EURUSD", "buy", 1.1, 1.0) == pytest.approx(100_000 * 1.1 / 200)
    assert client.margin("EURUSD", "SELL", 1.1, 2.5) == pytest.approx(100_000 * 2.5 * 1.1 / 200)


def test_margin_invalid_side():
    client, _ = make_client()
    for bad in ("compra", "", "hold"):
        with pytest.raises(ValueError):
            client.margin("EURUSD", bad, 1.1, 1.0)


# ---------------------------------------------------------------- conta
@pytest.mark.parametrize(
    "mode,expected,is_demo",
    [
        (fm.ACCOUNT_TRADE_MODE_DEMO, "demo", True),
        (fm.ACCOUNT_TRADE_MODE_REAL, "real", False),
        (fm.ACCOUNT_TRADE_MODE_CONTEST, "concurso", False),
    ],
)
def test_account_trade_mode(mode, expected, is_demo):
    client, _ = make_client(FakeMT5(trade_mode=mode, balance=5000.0))
    a = client.account()
    assert a["tipo_conta"] == expected and a["is_demo"] is is_demo
    assert a["saldo"] == 5000.0 and a["moeda"] == "USD" and a["alavancagem"] == 200
    assert a["servidor"] == "Exness-MT5Trial" and a["corretora"] == "Exness"


def test_account_has_no_password_or_holder_name():
    client, _ = make_client(FakeMT5(login=1), mt5_password="s3cret", mt5_login=1, mt5_path="C:/x/terminal64.exe")
    a = client.account()
    assert "password" not in a and "senha" not in a
    assert "Conta Teste" not in repr(a) and "s3cret" not in repr(a)


def test_positions_mapping():
    pos = [
        Record(ticket=1, symbol="EURUSD", type=fm.POSITION_TYPE_BUY, volume=0.1, price_open=1.1, price_current=1.101,
               sl=1.09, tp=1.12, profit=10.0, swap=-0.5, time=1_700_000_000),
        Record(ticket=2, symbol="EURUSD", type=fm.POSITION_TYPE_SELL, volume=0.2, price_open=1.1, price_current=1.101,
               sl=0.0, tp=0.0, profit=-20.0, swap=0.0, time=1_700_000_060),
    ]
    client, _ = make_client(FakeMT5([make_symbol("EURUSD")], positions=pos))
    out = client.positions()
    assert [p["tipo"] for p in out] == ["compra", "venda"]
    assert out[0] == {
        "ticket": 1, "simbolo": "EURUSD", "tipo": "compra", "volume": 0.1, "preco_abertura": 1.1,
        "preco_atual": 1.101, "stop_loss": 1.09, "take_profit": 1.12, "lucro": 10.0, "swap": -0.5,
        "abertura": "2023-11-14 22:13:20",
    }


def test_positions_empty():
    client, _ = make_client()
    assert client.positions() == []


# ---------------------------------------------------------------- concorrência
def test_concurrent_quotes():
    client, fake = make_client(FakeMT5([make_symbol("EURUSDm"), make_symbol("GBPUSDm")]))
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(20):
                assert client.quote("EURUSD")["simbolo"] == "EURUSDm"
                assert client.quote("gbpusd")["simbolo"] == "GBPUSDm"
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(fake.initialize_calls) == 1


# ---------------------------------------------------------------- auditoria
DEDICATED = "C:/mt5-demo/terminal64.exe"


def test_config_errors_block_connection():
    client, fake = make_client(errors=("MT5_LOGIN deve ser um número inteiro",), env_file="C:/p/.env")
    with pytest.raises(MT5Error) as exc:
        client.ensure_connected()
    assert str(exc.value) == "Configuração inválida no .env (C:/p/.env): MT5_LOGIN deve ser um número inteiro"
    assert fake.initialize_calls == []


def test_config_errors_without_env_file_and_joined():
    client, _ = make_client(errors=("a", "b"))
    with pytest.raises(MT5Error, match=r"\(variáveis de ambiente\): a; b"):
        client.quote("EURUSD")


def test_login_without_path_refused_without_initialize():
    client, fake = make_client(mt5_login=123, mt5_password="s3cret")
    with pytest.raises(MT5Error) as exc:
        client.ensure_connected()
    msg = str(exc.value)
    assert "MT5_PATH" in msg and "terminal64.exe" in msg and "MT5_LOGIN" in msg and "s3cret" not in msg
    assert fake.initialize_calls == []


def test_login_mismatch_shuts_down_and_raises():
    client, fake = make_client(FakeMT5(login=999), mt5_login=123, mt5_path=DEDICATED, mt5_password="s3cret")
    with pytest.raises(MT5Error) as exc:
        client.ensure_connected()
    assert "999" in str(exc.value) and "123" in str(exc.value) and "s3cret" not in str(exc.value)
    assert fake.shutdown_calls == 1
    assert fake.initialized is False


def test_warmup_succeeds_after_polls():
    client, fake = make_client(FakeMT5(connect_after_polls=3))
    client.ensure_connected()
    assert client.clock.sleeps == [0.5, 0.5, 0.5]


def test_warmup_times_out():
    client, fake = make_client(FakeMT5(connected=False))
    with pytest.raises(MT5Error, match="sem conexão com o servidor da corretora"):
        client.ensure_connected()
    assert sum(client.clock.sleeps) == pytest.approx(10.0)


def test_reconnect_calls_shutdown_and_clears_cache():
    fake = FakeMT5([make_symbol("EURUSDm")])
    client, _ = make_client(fake)
    assert client.resolve_symbol("EURUSD") == "EURUSDm"
    fake.initialized = False
    fake.symbols["EURUSD"] = fake.symbols.pop("EURUSDm")
    fake.symbols["EURUSD"].name = "EURUSD"
    fake.selected.add("EURUSD")
    assert client.resolve_symbol("EURUSD") == "EURUSD"  # cache limpo
    assert fake.shutdown_calls == 1
    assert len(fake.initialize_calls) == 2


def test_reconnect_ignores_shutdown_errors(monkeypatch):
    client, fake = make_client()
    client.ensure_connected()
    fake.initialized = False

    def boom():
        raise SystemError("x")

    monkeypatch.setattr(fake, "shutdown", boom)
    client.ensure_connected()
    assert len(fake.initialize_calls) == 2


def test_select_failure_raises_and_is_not_cached(monkeypatch):
    client, fake = make_client()
    monkeypatch.setattr(fake, "symbol_select", lambda name, enable=True: False)
    with pytest.raises(MT5Error, match="Market Watch"):
        client.resolve_symbol("EURUSD")
    assert client._symbol_cache == {}
    monkeypatch.undo()
    assert client.resolve_symbol("EURUSD") == "EURUSD"


def test_zero_tick_retried_then_success():
    fake = FakeMT5([make_symbol("EURUSD")], zero_tick_symbols={"EURUSD"})
    client, _ = make_client(fake)
    client.clock.on_sleep = lambda: fake.zero_tick_symbols.clear()
    q = client.quote("EURUSD")
    assert q["bid"] == 1.1
    assert client.clock.sleeps == [0.3]


def test_zero_tick_gives_error_after_three_attempts():
    fake = FakeMT5([make_symbol("EURUSD")], zero_tick_symbols={"EURUSD"})
    client, _ = make_client(fake)
    with pytest.raises(MT5Error, match="Sem cotação para EURUSD: mercado fechado ou símbolo ainda sem dados"):
        client.quote("EURUSD")
    assert client.clock.sleeps == [0.3, 0.3]


def test_weekend_warning_forex_saturday():
    client, _ = make_client()
    client.clock.now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    assert "fim de semana" in client.quote("EURUSD")["aviso"]


@pytest.mark.parametrize(
    "when,expected",
    [
        (datetime(2026, 10, 4, 20, 59, tzinfo=timezone.utc), True),  # domingo antes das 21h
        (datetime(2026, 10, 4, 21, 0, tzinfo=timezone.utc), False),
        (datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc), True),  # sexta às 21h
        (datetime(2026, 10, 2, 20, 59, tzinfo=timezone.utc), False),
        (datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc), False),  # quarta
    ],
)
def test_weekend_window(when, expected):
    client, _ = make_client()
    client.clock.now = when
    assert ("aviso" in client.quote("EURUSD")) is expected


def test_no_warning_for_crypto_on_weekend():
    btc = make_symbol("BTCUSD", path="Crypto\\Majors", digits=2, point=0.01, bid=60000.0, ask=60010.0)
    client, _ = make_client(FakeMT5([btc]))
    client.clock.now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    assert "aviso" not in client.quote("BTCUSD")


def test_rates_retry_then_success():
    closes = [1.1 + i * 0.001 for i in range(5)]
    fake = FakeMT5([make_symbol("EURUSD")], rates={"EURUSD": make_rates(closes)}, rates_fail_times=2)
    client, _ = make_client(fake)
    assert len(client.rates("EURUSD", "H1", 5)) == 5
    assert client.clock.sleeps == [0.5, 0.5]


def test_rates_retry_exhausted():
    fake = FakeMT5([make_symbol("EURUSD")], rates={"EURUSD": make_rates([1.1, 1.2])}, rates_fail_times=3)
    client, _ = make_client(fake)
    with pytest.raises(MT5Error, match="Sem candles.*history not synchronized"):
        client.rates("EURUSD", "H1", 2)
    assert fake.rates_calls == 3


@pytest.mark.parametrize("exc_type", [SystemError, OverflowError, TypeError])
def test_library_exception_becomes_mt5error(monkeypatch, exc_type):
    client, fake = make_client()
    client.ensure_connected()

    def boom(*a, **k):
        raise exc_type("falha interna")

    monkeypatch.setattr(fake, "symbol_info_tick", boom)
    with pytest.raises(MT5Error) as exc:
        client.quote("EURUSD")
    assert exc_type.__name__ in str(exc.value)


def test_unexpected_return_shape_becomes_mt5error(monkeypatch):
    client, fake = make_client()
    client.ensure_connected()
    monkeypatch.setattr(fake, "symbol_info_tick", lambda name: object())  # sem .time
    with pytest.raises(MT5Error) as exc:
        client.quote("EURUSD")
    assert "AttributeError" in str(exc.value)


def test_proxy_blocks_order_send_and_other_writes():
    client, fake = make_client()
    mod = client._module()
    for name in ("order_send", "order_check", "Buy", "positions_close"):
        with pytest.raises(MT5Error, match="Operação não permitida"):
            getattr(mod, name)
    with pytest.raises(MT5Error, match="order_send"):
        mod.order_send({})
    assert mod.TIMEFRAME_H1 == fm.TIMEFRAME_H1  # constantes liberadas
