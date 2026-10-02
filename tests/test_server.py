"""Testes ponta a ponta das tools via protocolo MCP (cliente em memória)."""

from __future__ import annotations

import json
import random
import tempfile
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from fake_mt5 import (
    ACCOUNT_TRADE_MODE_REAL,
    ORDER_TYPE_BUY_STOP,
    SYMBOL_CALC_MODE_CFD,
    FakeMT5,
    make_order,
    make_position,
    make_rates,
    make_symbol,
)
from trading_mcp.config import Settings
from trading_mcp.mt5_client import MT5Client
from trading_mcp.sec_edgar import SecEdgarError
from trading_mcp.server import create_server

EXPECTED_TOOLS = {
    "cotacao", "historico", "indicadores", "tamanho_posicao", "info_conta", "posicoes", "simbolos", "calendario",
    "fundamentos", "reacao_evento", "contexto_mercado", "journal_sincronizar", "journal_anotar", "journal_listar", "journal_estatisticas", "journal_exportar",
    "reacoes_registrar", "reacoes_estatisticas", "estrutura_smc", "risco_conta", "proposta_operacao",
    "propostas_listar",
}
# Gravam só nos bancos locais (journal, reações e propostas); todas as demais são somente leitura.
JOURNAL_WRITERS = {"journal_sincronizar", "journal_anotar", "journal_exportar", "reacoes_registrar", "proposta_operacao"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _random_walk(n: int, start: float = 1.1, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    values = [start]
    for _ in range(n - 1):
        values.append(round(values[-1] + rng.uniform(-0.0015, 0.0015), 5))
    return values


def _fake_mt5(**kwargs: Any) -> FakeMT5:
    symbols = [
        make_symbol("EURUSDm", description="Euro vs US Dollar"),
        make_symbol(
            "USDJPYm",
            description="US Dollar vs Japanese Yen",
            digits=3,
            point=0.001,
            bid=150.000,
            ask=150.012,
            trade_tick_size=0.001,
            trade_tick_value=0.6667,
            currency_base="USD",
            currency_profit="JPY",
            currency_margin="USD",
        ),
        make_symbol(
            "AAPLm",
            description="Apple Inc.",
            path="Stocks\\US",
            digits=2,
            point=0.01,
            bid=230.00,
            ask=230.10,
            trade_tick_size=0.01,
            trade_tick_value=0.01,
            trade_contract_size=1.0,
            volume_min=1.0,
            volume_max=1000.0,
            volume_step=1.0,
            currency_base="AAPL",
            currency_profit="USD",
            currency_margin="USD",
            trade_calc_mode=SYMBOL_CALC_MODE_CFD,
        ),
    ]
    rates = {"EURUSDm": make_rates(_random_walk(1500))}
    return FakeMT5(symbols, rates, **kwargs)


class FakeSec:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def fundamentals(self, ticker: str) -> dict[str, Any]:
        if self.error:
            raise self.error
        return {"ticker": ticker.upper(), "empresa": "Apple Inc.", "cik": 320193}


def _server(fake: FakeMT5 | None = None, sec: Any | None = None, calendar: Any | None = None, **settings_kwargs: Any):
    # Nunca o journal real do usuário (~/trading-mcp).
    scratch = Path(tempfile.mkdtemp(prefix="journal-test-"))
    settings_kwargs.setdefault("journal_path", scratch / "journal.sqlite3")
    settings_kwargs.setdefault("journal_export_dir", scratch / "export")
    settings_kwargs.setdefault("reacoes_path", scratch / "reacoes.sqlite3")
    settings_kwargs.setdefault("propostas_path", scratch / "propostas.sqlite3")
    settings = Settings(max_bars=5_000, **settings_kwargs)
    mt5 = MT5Client(settings, mt5_module=fake or _fake_mt5())
    return create_server(settings, mt5, sec or FakeSec(), calendar)


def _payload(result: Any) -> Any:
    assert not result.is_error, result.content
    if result.structured_content is not None:
        content = result.structured_content
        # Retornos que não são objeto (listas) vêm embrulhados em {"result": ...}.
        return content.get("result", content) if set(content) == {"result"} else content
    return json.loads(result.content[0].text)


def _error_text(result: Any) -> str:
    assert result.is_error
    return " ".join(block.text for block in result.content)


@pytest.mark.anyio
async def test_only_journal_tools_write_and_none_is_destructive() -> None:
    async with Client(_server()) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.annotations is not None and tool.description
        if tool.name in JOURNAL_WRITERS:
            assert tool.annotations.read_only_hint is False and tool.annotations.destructive_hint is False
        else:
            assert tool.annotations.read_only_hint is True


@pytest.mark.anyio
async def test_reacao_and_contexto_use_configured_instruments() -> None:
    import time

    import numpy as np

    from fake_mt5 import RATES_DTYPE

    minute = int(time.time()) // 60 * 60
    # EURUSDm: 1,10000 até 30 min atrás, 1,10050 depois.
    rows = [(minute - 60 * i, 1.1, 1.1, 1.1, 1.1 if i > 30 else 1.1005, 10, 12, 0) for i in range(120, 0, -1)]
    fake = _fake_mt5()
    fake.rates["EURUSDm"] = np.array(rows, dtype=RATES_DTYPE)
    event = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute - 30 * 60))
    async with Client(_server(fake, instruments=("EURUSD", "USDJPY"))) as client:
        reaction = _payload(await client.call_tool("reacao_evento", {"horario_utc": event}))
        context = _payload(await client.call_tool("contexto_mercado", {}))
        nothing = await client.call_tool("reacao_evento", {})
    eur = reaction["movimento_medido"][0]
    assert [m["simbolo"] for m in reaction["movimento_medido"]] == ["EURUSDm", "USDJPYm"]
    assert eur["janelas"][0]["pips"] == 5.0
    assert [i["simbolo"] for i in context["instrumentos"]] == ["EURUSDm", "USDJPYm"]
    assert "Informe o evento" in _error_text(nothing)


@pytest.mark.anyio
async def test_reacoes_tools_report_errors_to_the_model(tmp_path: Any) -> None:
    db = tmp_path / "reacoes.sqlite3"
    async with Client(_server(reacoes_path=db)) as client:
        empty = await client.call_tool("reacoes_estatisticas", {"evento": "claims"})
        windows = await client.call_tool("reacoes_estatisticas", {"evento": "claims", "janelas_min": [30]})
        # Sem o arquivo do calendário no terminal falso: o erro aponta o serviço do MT5.
        no_calendar = await client.call_tool("reacoes_registrar", {})
    assert "reacoes_registrar" in _error_text(empty) and not db.exists()
    assert "Janelas guardadas" in _error_text(windows)
    assert "TradingMcpCalendar" in _error_text(no_calendar)


@pytest.mark.anyio
async def test_journal_tools_end_to_end(tmp_path: Any) -> None:
    import time

    from fake_mt5 import DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_REASON_SL, make_deal

    now = int(time.time())
    fake = _fake_mt5(
        deals=[
            make_deal(1, 70, "EURUSDm", "buy", DEAL_ENTRY_IN, 0.1, 1.1, now - 7200, order=70),
            make_deal(2, 70, "EURUSDm", "sell", DEAL_ENTRY_OUT, 0.1, 1.098, now - 3600, order=71,
                      reason=DEAL_REASON_SL, profit=-20.0),
        ],
    )
    db = tmp_path / "journal.sqlite3"
    async with Client(_server(fake, journal_path=db, journal_export_dir=tmp_path / "exp")) as client:
        empty = await client.call_tool("journal_listar", {})
        synced = _payload(await client.call_tool("journal_sincronizar", {}))
        noted = _payload(await client.call_tool("journal_anotar", {"ticket": 70, "stop_inicial": 1.098, "setup": "OB"}))
        listed = _payload(await client.call_tool("journal_listar", {"dias": None}))
        stats = _payload(await client.call_tool("journal_estatisticas", {}))
        exported = _payload(await client.call_tool("journal_exportar", {}))
        both = await client.call_tool("journal_anotar", {"ticket": 70, "operacao_id": 1, "setup": "x"})
    assert "journal_sincronizar" in _error_text(empty)
    assert synced["operacoes_novas"] == 1 and synced["sem_stop_inicial"][0]["ticket"] == 70
    assert noted["risco_inicial"] == pytest.approx(20.0) and noted["r"] == -1.0 and noted["setup"] == "OB"
    assert listed["operacoes"][0]["fechamento_motivo"] == "stop"
    assert stats["geral"]["operacoes"] == 1 and stats["geral"]["r"]["r_medio"] == -1.0
    assert Path(exported["csv"]).is_file() and Path(exported["copia_do_banco"]).is_file()
    assert "exatamente um" in _error_text(both)


@pytest.mark.anyio
async def test_cotacao_resolves_suffix() -> None:
    async with Client(_server()) as client:
        data = _payload(await client.call_tool("cotacao", {"simbolo": "eurusd"}))
    assert data["simbolo"] == "EURUSDm"
    assert data["bid"] == pytest.approx(1.1)
    assert data["spread_pontos"] == 12
    assert data["spread_pips"] == pytest.approx(1.2)


@pytest.mark.anyio
async def test_historico_shape_and_order() -> None:
    async with Client(_server()) as client:
        data = _payload(await client.call_tool("historico", {"simbolo": "EURUSD", "quantidade": 50}))
    assert data["simbolo"] == "EURUSDm"
    header, *rows = data["candles_csv"].splitlines()
    assert header == "horario_utc,abertura,maxima,minima,fechamento,volume_ticks"
    assert data["fuso_horario"] == "UTC"
    assert data["ultimo_candle"]["em_formacao"] is False  # candles do fake são de 2023
    assert set(data["ultimo_candle"]) == {"utc", "sao_paulo", "nova_york", "em_formacao"}
    assert set(data["diferenca_utc_no_ultimo_candle"]) == {"sao_paulo", "nova_york"}
    assert len(rows) == data["quantidade"] == 50
    cells = [r.split(",") for r in rows]
    assert all(len(c) == 6 for c in cells)
    assert all(len(c[1].split(".")[1]) == 5 for c in cells)  # preço com os 5 dígitos do símbolo
    times = [c[0] for c in cells]
    assert times == sorted(times)
    assert data["resumo"]["maxima"] >= data["resumo"]["minima"]


@pytest.mark.anyio
async def test_historico_max_output_stays_compact() -> None:
    async with Client(_server()) as client:
        result = await client.call_tool("historico", {"simbolo": "EURUSD", "quantidade": 500})
    text = result.content[0].text
    assert len(json.loads(text)["candles_csv"].splitlines()) == 501
    assert len(text) < 40_000  # ~10k tokens; antes 1000 candles passavam de 170 mil caracteres


@pytest.mark.anyio
async def test_historico_rejects_too_many_candles() -> None:
    async with Client(_server()) as client:
        result = await client.call_tool("historico", {"simbolo": "EURUSD", "quantidade": 5000})
    assert "quantidade" in _error_text(result)


@pytest.mark.anyio
async def test_indicadores_default_list() -> None:
    async with Client(_server()) as client:
        data = _payload(await client.call_tool("indicadores", {"simbolo": "EURUSD"}))
    assert set(data["indicadores"]) == {"RSI(14)", "MACD(12,26,9)", "EMA(20)", "EMA(50)", "ATR(14)"}
    assert 0 <= data["indicadores"]["RSI(14)"]["valor"] <= 100
    assert data["candles_usados"] > 0
    assert data["candle_referencia"]["em_formacao"] is False
    assert not any("formação" in n for n in data["observacoes"])


@pytest.mark.anyio
async def test_indicadores_invalid_spec_is_tool_error() -> None:
    async with Client(_server()) as client:
        result = await client.call_tool("indicadores", {"simbolo": "EURUSD", "lista": ["MACD(26,12,9)"]})
    assert "MACD" in _error_text(result)


@pytest.mark.anyio
async def test_indicadores_invalid_spec_fails_before_mt5() -> None:
    # Spec inválida deve ser recusada mesmo sem terminal MT5 disponível.
    async with Client(_server(_fake_mt5(initialize_ok=False))) as client:
        result = await client.call_tool("indicadores", {"simbolo": "EURUSD", "lista": ["XYZ(3)"]})
    text = _error_text(result)
    assert "XYZ" in text and "MetaTrader" not in text


@pytest.mark.anyio
async def test_tamanho_posicao_forex_buy() -> None:
    # 1% de 10.000 = 100 USD; stop de 20 pips no EURUSD = 200 USD por lote -> 0,50 lote.
    async with Client(_server()) as client:
        data = _payload(
            await client.call_tool(
                "tamanho_posicao", {"simbolo": "EURUSD", "entrada": 1.1, "stop": 1.098, "risco_percentual": 1}
            )
        )
    assert data["direcao"] == "compra"
    assert data["lotes"] == pytest.approx(0.5)
    assert data["risco_alvo"] == pytest.approx(100.0)
    assert data["risco_real"] == pytest.approx(100.0)
    assert data["perda_por_lote"] == pytest.approx(200.0)
    assert data["distancia_stop"]["pips"] == pytest.approx(20.0)
    assert data["distancia_stop"]["pontos"] == 200
    assert data["moeda_conta"] == "USD"
    # Spread de 1,2 pip sobre stop de 20 pips = +6% -> 0,5 lote x (200 + 12) = 106.
    assert data["risco_com_spread"] == pytest.approx(106.0)
    # Margem do fake: contrato x volume x preço / 200.
    assert data["margem_estimada"] == pytest.approx(100_000 * 0.5 * 1.1 / 200)
    assert "comissão" in data["custos_nao_incluidos"]


@pytest.mark.anyio
async def test_tamanho_posicao_sell_with_custom_balance() -> None:
    # Venda: stop acima. 2% de 5.000 = 100 USD; 25 pips = 250 USD/lote -> 0,40 lote.
    async with Client(_server()) as client:
        data = _payload(
            await client.call_tool(
                "tamanho_posicao",
                {"simbolo": "EURUSD", "entrada": 1.1, "stop": 1.1025, "risco_percentual": 2, "saldo": 5000},
            )
        )
    assert data["direcao"] == "venda"
    assert data["lotes"] == pytest.approx(0.4)
    assert data["saldo_base"] == pytest.approx(5000)
    # Venda: entra no bid e o stop é acionado pelo ask -> perda é a própria distância.
    assert data["risco_com_spread"] == pytest.approx(data["risco_real"])
    assert data["spread_atual"]["pontos"] == 12


@pytest.mark.anyio
async def test_tamanho_posicao_stop_below_tick_is_rejected() -> None:
    async with Client(_server()) as client:
        result = await client.call_tool(
            "tamanho_posicao", {"simbolo": "EURUSD", "entrada": 1.1, "stop": 1.0999999999}
        )
    assert "tick mínimo" in _error_text(result)


@pytest.mark.anyio
async def test_tamanho_posicao_warns_when_spread_dominates_stop() -> None:
    # Stop de 1 pip (10 pontos) com spread de 12 pontos.
    async with Client(_server()) as client:
        data = _payload(
            await client.call_tool("tamanho_posicao", {"simbolo": "EURUSD", "entrada": 1.1, "stop": 1.0999})
        )
    assert any("spread atual" in w for w in data["avisos"])


@pytest.mark.anyio
async def test_tamanho_posicao_warns_when_margin_exceeds_free_margin() -> None:
    # Stop de 5 pontos: 1% de 10.000 / 5 USD = 20 lotes -> margem 11.000 > margem livre 10.000.
    async with Client(_server()) as client:
        data = _payload(
            await client.call_tool("tamanho_posicao", {"simbolo": "EURUSD", "entrada": 1.1, "stop": 1.09995, "risco_percentual": 1})
        )
    assert data["lotes"] == pytest.approx(20)
    assert any("margem livre" in w for w in data["avisos"])


@pytest.mark.anyio
async def test_tamanho_posicao_stock_cfd_has_no_pips() -> None:
    # AAPL: 1 ação por lote, stop de 5 USD -> perda de 5 USD por lote; 1% de 10.000 = 100 -> 20 lotes.
    async with Client(_server()) as client:
        data = _payload(
            await client.call_tool("tamanho_posicao", {"simbolo": "AAPL", "entrada": 230.0, "stop": 225.0, "risco_percentual": 1})
        )
    assert data["lotes"] == pytest.approx(20)
    assert data["distancia_stop"]["pips"] is None


@pytest.mark.anyio
async def test_tamanho_posicao_warns_on_far_entry() -> None:
    async with Client(_server()) as client:
        data = _payload(
            await client.call_tool("tamanho_posicao", {"simbolo": "EURUSD", "entrada": 1.3, "stop": 1.29})
        )
    assert any("preço atual" in w for w in data["avisos"])


@pytest.mark.anyio
async def test_tamanho_posicao_entry_equals_stop_is_tool_error() -> None:
    async with Client(_server()) as client:
        result = await client.call_tool("tamanho_posicao", {"simbolo": "EURUSD", "entrada": 1.1, "stop": 1.1})
    assert "stop" in _error_text(result).lower()


@pytest.mark.anyio
async def test_info_conta_pinned_demo_has_no_warning() -> None:
    pinned = {"mt5_login": 12345678, "mt5_server": "Exness-MT5Trial", "mt5_path": "C:/MT5-Demo/terminal64.exe"}
    async with Client(_server(**pinned)) as client:
        data = _payload(await client.call_tool("info_conta", {}))
    assert data["conta"]["is_demo"] is True and data["conta"]["conectado"] is True
    assert "avisos" not in data
    assert "senha" not in json.dumps(data).lower()


@pytest.mark.anyio
async def test_info_conta_unpinned_account_warns() -> None:
    async with Client(_server()) as client:
        data = _payload(await client.call_tool("info_conta", {}))
    assert any("não fixada" in w for w in data["avisos"])


@pytest.mark.anyio
async def test_info_conta_real_account_warns() -> None:
    async with Client(_server(_fake_mt5(trade_mode=ACCOUNT_TRADE_MODE_REAL))) as client:
        data = _payload(await client.call_tool("info_conta", {}))
    assert data["conta"]["is_demo"] is False
    assert any("NÃO é demo" in w for w in data["avisos"])


@pytest.mark.anyio
async def test_info_conta_shows_terminal_trading_locks() -> None:
    async with Client(_server(_fake_mt5(tradeapi_disabled=True))) as client:
        data = _payload(await client.call_tool("info_conta", {}))
    assert data["terminal"]["negociacao_via_python_desativada"] is True
    assert data["terminal"]["algo_trading_ativo"] is False


@pytest.mark.anyio
async def test_posicoes_reports_positions_and_pending_orders() -> None:
    fake = _fake_mt5(
        positions=[make_position(7, "EURUSDm", "buy", 0.1, 1.098, 1.1, sl=1.096, tp=1.104, profit=20.0)],
        orders=[make_order(8, "EURUSDm", ORDER_TYPE_BUY_STOP, 0.2, 1.102, 1.10012, sl=1.1)],
    )
    async with Client(_server(fake)) as client:
        data = _payload(await client.call_tool("posicoes", {}))
        only_usdjpy = _payload(await client.call_tool("posicoes", {"simbolo": "USDJPY", "incluir_pendentes": False}))
    pos = data["posicoes"][0]
    assert pos["stop"]["resultado_se_atingido"] == pytest.approx(-20.0)
    assert pos["alvo"]["resultado_se_atingido"] == pytest.approx(60.0)
    assert pos["cotacao"]["estado"] == "atual"
    assert data["pendentes"][0]["tipo"] == "compra stop"
    assert data["totais_pendentes"]["perda_nos_stops_se_executadas"] == pytest.approx(40.0)
    assert only_usdjpy["posicoes"] == [] and "pendentes" not in only_usdjpy


@pytest.mark.anyio
async def test_historico_and_indicadores_warn_when_disconnected() -> None:
    fake = _fake_mt5()
    async with Client(_server(fake)) as client:
        await client.call_tool("cotacao", {"simbolo": "EURUSD"})
        fake.connected = False
        hist = _payload(await client.call_tool("historico", {"simbolo": "EURUSD", "quantidade": 5}))
        ind = _payload(await client.call_tool("indicadores", {"simbolo": "EURUSD"}))
    assert any("sem conexão" in w for w in hist["avisos"])
    assert any("sem conexão" in n for n in ind["observacoes"])


@pytest.mark.anyio
async def test_indicadores_flags_forming_candle() -> None:
    import time

    n = 1500
    start = int(time.time()) - (n - 1) * 3600 - 1800  # último H1 abriu há 30 min
    fake = _fake_mt5()
    fake.rates["EURUSDm"] = make_rates(_random_walk(n), start_time=start)
    async with Client(_server(fake)) as client:
        data = _payload(await client.call_tool("indicadores", {"simbolo": "EURUSD"}))
        closed = _payload(
            await client.call_tool("indicadores", {"simbolo": "EURUSD", "incluir_candle_atual": False})
        )
    assert data["candle_referencia"]["em_formacao"] is True
    assert any("em formação" in n for n in data["observacoes"])
    assert closed["candle_referencia"]["em_formacao"] is False


@pytest.mark.anyio
async def test_cotacao_reports_time_age_and_state() -> None:
    async with Client(_server()) as client:
        data = _payload(await client.call_tool("cotacao", {"simbolo": "EURUSD"}))
    assert data["estado"] == "atual" and data["idade_s"] < 5
    assert data["horario"]["utc"].endswith("Z") and "aviso" not in data


@pytest.mark.anyio
async def test_tamanho_posicao_warns_on_real_account_balance() -> None:
    async with Client(_server(_fake_mt5(trade_mode=ACCOUNT_TRADE_MODE_REAL))) as client:
        data = _payload(
            await client.call_tool("tamanho_posicao", {"simbolo": "EURUSD", "entrada": 1.1, "stop": 1.098})
        )
    assert any("NÃO é demo" in w for w in data["avisos"])


@pytest.mark.anyio
async def test_tamanho_posicao_warns_on_stale_quote() -> None:
    # Tick de 1 h atrás e nenhum candle na semana passada: estado "mercado_fechado_provavel".
    async with Client(_server(_fake_mt5(tick_age_s={"EURUSDm": 3600}))) as client:
        data = _payload(
            await client.call_tool("tamanho_posicao", {"simbolo": "EURUSD", "entrada": 1.1, "stop": 1.098})
        )
    assert any("cotação não atual (mercado_fechado_provavel" in w for w in data["avisos"])


@pytest.mark.anyio
async def test_simbolos_search() -> None:
    async with Client(_server()) as client:
        data = _payload(await client.call_tool("simbolos", {"busca": "apple"}))
    assert [s["nome"] for s in data] == ["AAPLm"]


@pytest.mark.anyio
async def test_unknown_symbol_is_tool_error_with_message() -> None:
    async with Client(_server()) as client:
        result = await client.call_tool("cotacao", {"simbolo": "XYZABC"})
    assert "XYZABC" in _error_text(result)


@pytest.mark.anyio
async def test_mt5_unavailable_is_tool_error() -> None:
    async with Client(_server(_fake_mt5(initialize_ok=False))) as client:
        result = await client.call_tool("cotacao", {"simbolo": "EURUSD"})
    assert "IPC initialize failed" in _error_text(result)


@pytest.mark.anyio
async def test_fundamentos_ok_and_error() -> None:
    async with Client(_server()) as client:
        data = _payload(await client.call_tool("fundamentos", {"ticker": "aapl"}))
    assert data["ticker"] == "AAPL"

    failing = FakeSec(SecEdgarError("Ticker não encontrado na SEC: ZZZZ"))
    async with Client(_server(sec=failing)) as client:
        result = await client.call_tool("fundamentos", {"ticker": "ZZZZ"})
    assert "ZZZZ" in _error_text(result)


@pytest.mark.anyio
async def test_unexpected_exception_is_readable_tool_error() -> None:
    crashing = FakeSec(AttributeError("'NoneType' object has no attribute 'get'"))
    async with Client(_server(sec=crashing)) as client:
        result = await client.call_tool("fundamentos", {"ticker": "AAPL"})
    text = _error_text(result)
    assert "AttributeError" in text and "NoneType" in text


@pytest.mark.anyio
async def test_stdio_server_starts_from_other_cwd(tmp_path: Any) -> None:
    """Sobe o servidor de verdade (subprocesso stdio), como o Claude Code faz."""
    import os
    import sys

    from mcp import StdioServerParameters

    env = {
        **os.environ,
        "TRADING_MCP_ENV_FILE": str(tmp_path / "nenhum.env"),
        "JOURNAL_PATH": str(tmp_path / "journal.sqlite3"),
        "JOURNAL_EXPORT_DIR": str(tmp_path / "exp"),
        "REACOES_PATH": str(tmp_path / "reacoes.sqlite3"),
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "trading_mcp"], env=env, cwd=str(tmp_path))
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        result = await client.call_tool("indicadores", {"simbolo": "EURUSD", "lista": ["MACD(26,12,9)"]})
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert result.is_error and "MACD" in _error_text(result)


@pytest.mark.anyio
async def test_calendario_without_service_file_is_tool_error() -> None:
    async with Client(_server()) as client:
        result = await client.call_tool("calendario", {})
    text = _error_text(result)
    assert "TradingMcpCalendar" in text and "calendar_US.json" in text


@pytest.mark.anyio
async def test_calendario_returns_events(tmp_path: Any) -> None:
    from datetime import datetime, timedelta, timezone

    from test_calendario import ev, val, write
    from trading_mcp.calendario import EconomicCalendar

    now = datetime.now(timezone.utc)
    path = write(
        tmp_path,
        [ev(1, "Nonfarm Payrolls", "nonfarm-payrolls", unit="JOB", mult="THOUSANDS", digits=0)],
        [val(10, 1, now + timedelta(minutes=20), forecast=89, prev=162)],
        generated_gmt=int(now.timestamp()),
    )
    async with Client(_server(calendar=EconomicCalendar(lambda: path))) as client:
        data = _payload(await client.call_tool("calendario", {"horas_a_frente": 0.5, "horas_atras": 0}))
        bad = await client.call_tool("calendario", {"importancia_minima": "altissima"})
    event = data["eventos"][0]
    assert event["evento"] == "Nonfarm Payrolls" and event["situacao"] == "agendado"
    assert event["previsao"] == 89.0 and event["realizado"] is None
    assert data["estado"] == "atual"
    assert bad.is_error


@pytest.mark.anyio
async def test_risk_panel_and_proposal_tools_never_send_orders() -> None:
    fake = _fake_mt5()
    async with Client(_server(fake)) as client:
        panel = _payload(await client.call_tool("risco_conta", {}))
        proposal = _payload(
            await client.call_tool("proposta_operacao", {"simbolo": "EURUSD", "entrada": 1.099, "stop": 1.097,
                                                         "alvo": 1.103, "validade_min": 15})
        )
        listed = _payload(await client.call_tool("propostas_listar", {}))
        bad = await client.call_tool("proposta_operacao", {"simbolo": "EURUSD", "entrada": 1.099, "stop": 1.097,
                                                           "alvo": 1.098})
    assert panel["regras"] == {**panel["regras"], "por_operacao_pct": 1.25, "dia_pct": 5.0, "semana_pct": 25.0}
    assert panel["proxima_operacao"]["pode_operar"] is True
    assert proposal["status"] == "valida" and "nada foi enviado" in proposal["envio"]
    assert proposal["volume"] > 0 and proposal["risco_pct_base_dia"] <= 1.25
    assert [p["id"] for p in listed["propostas"]] == [proposal["id"]]
    assert bad.is_error and "lado do lucro" in _error_text(bad)
    # O FakeMT5 levanta AssertionError em order_send/order_check: chegar aqui prova que nada foi enviado.


@pytest.mark.anyio
async def test_tamanho_posicao_defaults_to_the_user_limit_and_warns_above_it() -> None:
    async with Client(_server()) as client:
        default = _payload(await client.call_tool("tamanho_posicao", {"simbolo": "EURUSD", "entrada": 1.1, "stop": 1.098}))
        above = _payload(await client.call_tool(
            "tamanho_posicao", {"simbolo": "EURUSD", "entrada": 1.1, "stop": 1.098, "risco_percentual": 2}))
    assert default["risco_percentual"] == 1.25 and default["risco_alvo"] == pytest.approx(125.0)
    assert not any("acima do seu limite" in w for w in default["avisos"])
    assert any("acima do seu limite de 1.25%" in w for w in above["avisos"])

