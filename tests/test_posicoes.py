"""Relatório de posições e ordens pendentes (MT5 simulado)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import fake_mt5 as fm
from fake_mt5 import FakeMT5, make_order, make_position, make_symbol
from test_mt5_client import make_client
from trading_mcp import posicoes
from trading_mcp.mt5_client import MT5Error

# EURUSD do fake: tick de 0,00001 vale 1 USD por lote -> 1 ponto x 0,1 lote = 0,10 USD.
EUR = "EURUSD"
AAPL = make_symbol(
    "AAPL", path="Stocks\\US", digits=2, point=0.01, bid=230.0, ask=230.1, trade_tick_size=0.01, trade_tick_value=0.01,
    trade_contract_size=1.0, volume_min=1.0, volume_step=1.0, trade_calc_mode=fm.SYMBOL_CALC_MODE_CFD,
)


def _client(positions=(), orders=(), symbols=None, **fake_kwargs):
    fake = FakeMT5(symbols or [make_symbol(EUR), AAPL], positions=list(positions), orders=list(orders), **fake_kwargs)
    client, fake = make_client(fake)
    return client, fake


def _opened(minutes_ago: float) -> int:
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)  # relógio do FakeClock
    return int((now - timedelta(minutes=minutes_ago)).timestamp())


BUY = make_position(1, EUR, "buy", 0.1, 1.10000, 1.10100, sl=1.09500, tp=1.11000, profit=10.0, swap=-0.35,
                    time=_opened(185))
# Venda com stop abaixo da entrada (acima do preço atual): lucro protegido.
SELL = make_position(2, EUR, "sell", 0.2, 1.10000, 1.09500, sl=1.09800, profit=100.0, time=_opened(30))
NAKED = make_position(3, "AAPL", "buy", 10.0, 225.00, 230.00, profit=50.0, time=_opened(2))


def test_buy_position_distances_and_money():
    client, _ = _client([BUY])
    p = posicoes.build(client)["posicoes"][0]
    assert p["direcao"] == "compra" and p["preco_abertura"] == 1.1 and p["preco_atual"] == 1.101
    assert p["cotacao"]["estado"] == "atual"
    assert p["lucro_aberto"] == 10.0 and p["lucro_aberto_pct_saldo"] == 0.1 and p["swap"] == -0.35
    assert p["aberta_ha"] == "3 h 05 min"
    stop = p["stop"]
    assert stop["situacao"] == "com_risco" and stop["preco"] == 1.095
    assert stop["distancia_do_preco_atual"] == {"preco": 0.006, "pontos": 600, "pips": 60.0}
    assert stop["distancia_da_entrada"] == {"preco": 0.005, "pontos": 500, "pips": 50.0}
    assert stop["resultado_se_atingido"] == pytest.approx(-50.0)
    assert stop["resultado_se_atingido_pct_saldo"] == pytest.approx(-0.5)
    assert stop["variacao_desde_agora"] == pytest.approx(-60.0)
    alvo = p["alvo"]
    assert alvo["distancia_do_preco_atual"]["pontos"] == 900
    assert alvo["resultado_se_atingido"] == pytest.approx(100.0)
    assert alvo["variacao_desde_agora"] == pytest.approx(90.0)
    assert p["relacao_alvo_stop"] == 2.0


def test_sell_with_stop_past_entry_is_protected_profit():
    client, _ = _client([SELL])
    p = posicoes.build(client)["posicoes"][0]
    stop = p["stop"]
    assert p["direcao"] == "venda" and stop["situacao"] == "lucro_protegido"
    assert stop["distancia_do_preco_atual"]["pontos"] == 300  # stop da venda fica acima do preço
    assert stop["resultado_se_atingido"] == pytest.approx(40.0)
    assert stop["variacao_desde_agora"] == pytest.approx(-60.0)
    assert p["alvo"] == {"preco": None}
    assert p["relacao_alvo_stop"] is None


def test_stop_at_entry_is_breakeven():
    pos = make_position(4, EUR, "buy", 0.1, 1.10000, 1.10200, sl=1.10000)
    client, _ = _client([pos])
    stop = posicoes.build(client)["posicoes"][0]["stop"]
    assert stop["situacao"] == "no_preco_de_entrada" and stop["resultado_se_atingido"] == 0.0


def test_position_without_stop_is_flagged_and_left_out_of_loss_total():
    client, _ = _client([BUY, NAKED])
    data = posicoes.build(client)
    naked = data["posicoes"][1]
    assert naked["stop"] == {"preco": None, "situacao": "sem_stop"}
    assert data["totais"]["posicoes_sem_stop"] == 1
    assert data["totais"]["perda_nos_stops"] == pytest.approx(50.0)
    assert any("1 posição(ões) sem stop" in w for w in data["avisos"])


def test_stock_distances_have_no_pips():
    pos = make_position(9, "AAPL", "buy", 10.0, 225.00, 230.00, sl=220.00)
    client, _ = _client([pos])
    stop = posicoes.build(client)["posicoes"][0]["stop"]
    assert stop["distancia_do_preco_atual"] == {"preco": 10.0, "pontos": 1000}
    assert stop["resultado_se_atingido"] == pytest.approx(-50.0)  # 5 USD x 10 ações


def test_sell_target_and_ratio():
    pos = make_position(8, EUR, "sell", 0.1, 1.10000, 1.09900, sl=1.10300, tp=1.09400)
    client, _ = _client([pos])
    p = posicoes.build(client)["posicoes"][0]
    assert p["stop"]["situacao"] == "com_risco" and p["stop"]["distancia_do_preco_atual"]["pontos"] == 400
    assert p["alvo"]["distancia_do_preco_atual"]["pontos"] == 500  # alvo da venda fica abaixo
    assert p["alvo"]["resultado_se_atingido"] == pytest.approx(60.0)
    assert p["alvo"]["variacao_desde_agora"] == pytest.approx(50.0)
    assert p["relacao_alvo_stop"] == 2.0


def test_stop_already_passed_is_flagged():
    # Compra com stop em 1,0950 e preço em 1,0900 (gap ou cotação parada).
    pos = make_position(20, EUR, "buy", 0.1, 1.10000, 1.09000, sl=1.09500, tp=1.11000)
    client, _ = _client([pos])
    data = posicoes.build(client)
    stop = data["posicoes"][0]["stop"]
    assert stop["ultrapassado"] is True and stop["distancia_do_preco_atual"]["pontos"] == -500
    assert stop["variacao_desde_agora"] is None  # não vira "ganho se o stop for atingido"
    assert "ultrapassado" not in data["posicoes"][0]["alvo"]
    assert data["totais"]["variacao_desde_agora_se_todos_os_stops"] is None
    assert data["totais"]["perda_nos_stops"] == pytest.approx(50.0)
    assert any("já passou do stop" in w for w in data["avisos"])


def test_target_already_passed_is_flagged():
    pos = make_position(21, EUR, "buy", 0.1, 1.10000, 1.10600, sl=1.09500, tp=1.10500)
    client, _ = _client([pos])
    alvo = posicoes.build(client)["posicoes"][0]["alvo"]
    assert alvo["ultrapassado"] is True and alvo["variacao_desde_agora"] is None


def test_totals_classify_by_stop_side_and_flag_sign_mismatch(monkeypatch):
    client, _ = _client([BUY])
    monkeypatch.setattr(client, "profit", lambda *a, **k: 50.0)  # sinal invertido
    data = posicoes.build(client)
    assert data["totais"]["perda_nos_stops"] == pytest.approx(50.0)
    assert data["totais"]["lucro_protegido_nos_stops"] == 0
    assert any("contradiz a posição do stop em 1" in w for w in data["avisos"])


def test_one_bad_symbol_does_not_break_report():
    ghost = make_position(30, "GHOST", "buy", 1.0, 10.0, 11.0, sl=9.0)
    client, _ = _client([BUY, ghost])
    data = posicoes.build(client)
    bad = data["posicoes"][1]
    assert bad["ticket"] == 30 and "GHOST" in bad["erro"]
    assert data["posicoes"][0]["stop"]["resultado_se_atingido"] == pytest.approx(-50.0)
    assert data["totais"]["perda_nos_stops"] is None  # total sem a posição com erro seria enganoso
    assert [g["simbolo"] for g in data["por_simbolo"]] == [EUR]
    assert any("Item 30 (GHOST)" in w for w in data["avisos"])


def test_orders_failure_keeps_positions():
    client, fake = _client([BUY])
    client.ensure_connected()
    fake.orders_get = lambda *a, **k: None
    data = posicoes.build(client)
    assert data["posicoes"][0]["ticket"] == 1 and data["pendentes"] is None and "totais_pendentes" not in data
    assert any("Ordens pendentes não lidas" in w for w in data["avisos"])


def test_orders_read_before_positions_and_filled_order_deduplicated():
    calls = []
    order = make_order(1, EUR, fm.ORDER_TYPE_BUY_LIMIT, 0.1, 1.1, 1.10012, sl=1.095)  # virou a posição 1
    client, fake = _client([BUY], [order])
    client.ensure_connected()
    positions_get, orders_get = fake.positions_get, fake.orders_get
    fake.positions_get = lambda *a, **k: calls.append("positions") or positions_get(*a, **k)
    fake.orders_get = lambda *a, **k: calls.append("orders") or orders_get(*a, **k)
    data = posicoes.build(client)
    assert calls == ["orders", "positions"]
    assert data["pendentes"] == [] and len(data["posicoes"]) == 1


def test_closing_market_order_and_missing_price():
    closing = make_order(40, EUR, fm.ORDER_TYPE_SELL, 0.1, 0.0, 1.1, position_id=1)
    market = make_order(41, EUR, fm.ORDER_TYPE_BUY, 0.1, 0.0, 1.1)
    no_tick = make_order(42, EUR, fm.ORDER_TYPE_BUY_STOP, 0.1, 1.2, 0.0, sl=1.19)
    client, _ = _client(orders=[closing, market, no_tick])
    c, m, n = posicoes.build(client)["pendentes"]
    assert c["tipo"] == "fechamento da posição 1 (em processamento)" and "stop" not in c
    assert "em processamento" in m["tipo"] and "stop" not in m  # sem preço: sem valores "se executada"
    assert n["preco_atual"] is None and n["distancia_ate_ativacao"] is None
    assert n["stop"]["resultado_se_executada_e_atingido"] == pytest.approx(-100.0)


def test_symbol_filter_ignores_case():
    client, _ = _client([make_position(5, "EURUSDm", "buy", 0.1, 1.1, 1.101)], symbols=[make_symbol("EURUSDm")])
    client.resolve_symbol = lambda name: name.upper()  # terminal que aceita o nome em qualquer caixa
    assert [p["ticket"] for p in posicoes.build(client, "EURUSDm")["posicoes"]] == [5]


def test_totals_and_exposure_by_symbol():
    client, _ = _client([BUY, SELL, NAKED])
    data = posicoes.build(client)
    t = data["totais"]
    assert t["posicoes"] == 3 and t["lucro_aberto"] == 160.0 and t["swap"] == -0.35
    assert t["perda_nos_stops"] == pytest.approx(50.0)  # o lucro protegido da venda não compensa
    assert t["perda_nos_stops_pct_saldo"] == pytest.approx(0.5)
    assert t["lucro_protegido_nos_stops"] == pytest.approx(40.0)
    assert t["variacao_desde_agora_se_todos_os_stops"] == pytest.approx(-120.0)
    by = {g["simbolo"]: g for g in data["por_simbolo"]}
    assert by[EUR] == {
        "simbolo": EUR, "posicoes": 2, "lotes_compra": 0.1, "lotes_venda": 0.2, "lotes_liquidos": -0.1,
        "lucro_aberto": 110.0,
    }
    assert by["AAPL"]["lotes_liquidos"] == 10.0


def test_pending_orders_risk_if_executed():
    orders = [
        make_order(10, EUR, fm.ORDER_TYPE_BUY_LIMIT, 0.5, 1.09500, 1.10012, sl=1.09000, tp=1.10500),
        # Stop limitada: ativa em 1,0900 e coloca a limitada em 1,0895 (a entrada).
        make_order(11, EUR, fm.ORDER_TYPE_SELL_STOP_LIMIT, 0.3, 1.09000, 1.10000, sl=1.09450,
                   price_stoplimit=1.08950, type_time=fm.ORDER_TIME_SPECIFIED, time_expiration=_opened(-60)),
    ]
    client, _ = _client(orders=orders)
    data = posicoes.build(client)
    limit, stop_limit = data["pendentes"]
    assert limit["tipo"] == "compra limitada" and limit["volume"] == 0.5
    assert limit["distancia_ate_ativacao"]["pontos"] == 512
    assert limit["stop"]["resultado_se_executada_e_atingido"] == pytest.approx(-250.0)
    assert limit["alvo"]["resultado_se_executada_e_atingido"] == pytest.approx(500.0)
    assert limit["relacao_alvo_stop"] == 2.0 and limit["validade"] == "até cancelar" and limit["expira"] is None
    assert stop_limit["tipo"] == "venda stop limitada"
    assert stop_limit["preco_ativacao"] == 1.09 and stop_limit["preco_limite"] == 1.0895
    assert stop_limit["stop"]["distancia_da_entrada"]["pontos"] == 500
    assert stop_limit["stop"]["resultado_se_executada_e_atingido"] == pytest.approx(-150.0)
    assert stop_limit["validade"] == "até a data de expiração"
    assert stop_limit["expira"]["utc"] == "2026-09-30T13:00:00Z"
    assert data["totais_pendentes"] == {
        "ordens": 2, "perda_nos_stops_se_executadas": 400.0, "perda_nos_stops_se_executadas_pct_saldo": 4.0,
        "ordens_sem_stop": 0,
    }


def test_partially_filled_order_shows_initial_volume():
    order = make_order(12, EUR, fm.ORDER_TYPE_BUY_LIMIT, 0.2, 1.095, 1.10012, volume_initial=0.5)
    client, _ = _client(orders=[order])
    o = posicoes.build(client)["pendentes"][0]
    assert o["volume"] == 0.2 and o["volume_inicial"] == 0.5
    assert o["stop"] == {"preco": None, "situacao": "sem_stop"}


def test_market_order_in_processing_has_no_risk_fields():
    order = make_order(13, EUR, fm.ORDER_TYPE_CLOSE_BY, 0.1, 0.0, 1.1)
    client, _ = _client(orders=[order])
    o = posicoes.build(client)["pendentes"][0]
    assert "em processamento" in o["tipo"] and "stop" not in o


def test_without_pending_orders_section():
    client, _ = _client([BUY], orders=[make_order(10, EUR, fm.ORDER_TYPE_BUY_LIMIT, 0.5, 1.095, 1.1)])
    data = posicoes.build(client, include_pending=False)
    assert "pendentes" not in data and "totais_pendentes" not in data


def test_empty_account():
    client, _ = _client()
    data = posicoes.build(client)
    assert data["posicoes"] == [] and data["pendentes"] == [] and data["por_simbolo"] == []
    assert data["totais"]["perda_nos_stops"] == 0
    assert data["totais"]["variacao_desde_agora_se_todos_os_stops"] is None  # nenhum stop para somar
    assert "avisos" not in data
    assert data["conta"]["moeda"] == "USD" and data["coletado"]["utc"] == "2026-09-30T12:00:00Z"


def test_symbol_filter_resolves_suffix():
    other = make_position(5, "EURUSDm", "buy", 0.1, 1.1, 1.101)
    client, _ = _client([other, make_position(6, "GBPUSDm", "buy", 0.1, 1.3, 1.301)],
                        symbols=[make_symbol("EURUSDm"), make_symbol("GBPUSDm")])
    data = posicoes.build(client, "eurusd")
    assert data["filtro_simbolo"] == "EURUSDm" and [p["ticket"] for p in data["posicoes"]] == [5]


def test_stale_quote_is_flagged():
    client, _ = _client([BUY], tick_age_s={EUR: 3600})
    data = posicoes.build(client)
    assert data["posicoes"][0]["cotacao"]["estado"] == "mercado_fechado_provavel"
    assert any("Cotação não atual em EURUSD" in w for w in data["avisos"])


def test_missing_quote_does_not_break_report():
    client, _ = _client([BUY], zero_tick_symbols={EUR})
    data = posicoes.build(client)
    assert data["posicoes"][0]["cotacao"]["estado"] == "sem_cotacao"
    assert data["posicoes"][0]["stop"]["resultado_se_atingido"] == pytest.approx(-50.0)


def test_real_account_and_disconnection_warn():
    client, fake = _client([BUY], trade_mode=fm.ACCOUNT_TRADE_MODE_REAL)
    client.ensure_connected()
    fake.connected = False
    avisos = posicoes.build(client)["avisos"]
    assert "NÃO é demo" in avisos[0]
    assert any("sem conexão" in w for w in avisos)


def test_money_failure_marks_totals_incomplete(monkeypatch):
    client, _ = _client([BUY])

    def fail(*args, **kwargs):
        raise MT5Error("order_calc_profit falhou")

    monkeypatch.setattr(client, "profit", fail)
    data = posicoes.build(client)
    assert data["posicoes"][0]["stop"]["resultado_se_atingido"] is None
    t = data["totais"]
    # Valor faltando não pode virar "perda zero".
    assert t["perda_nos_stops"] is None and t["perda_nos_stops_pct_saldo"] is None
    assert t["lucro_protegido_nos_stops"] is None and t["variacao_desde_agora_se_todos_os_stops"] is None
    assert sum("order_calc_profit falhou" in w for w in data["avisos"]) == 1  # um aviso por símbolo
    assert any("ficam null" in w for w in data["avisos"])


def test_profit_fallback_uses_profit_or_loss_tick_value():
    sym = make_symbol(EUR)
    sym.trade_tick_value_profit, sym.trade_tick_value_loss = 0.9, 1.1
    client, fake = _client(symbols=[sym])
    fake.calc_profit_returns_none = True
    assert client.profit(EUR, "buy", 1.0, 1.1, 1.1001) == pytest.approx(9.0)
    assert client.profit(EUR, "buy", 1.0, 1.1, 1.0999) == pytest.approx(-11.0)
    assert client.profit(EUR, "sell", 1.0, 1.1, 1.0999) == pytest.approx(9.0)


def test_profit_fallback_without_tick_value_is_mt5_error():
    sym = make_symbol(EUR, trade_tick_value=0.0)
    client, fake = _client(symbols=[sym])
    fake.calc_profit_returns_none = True
    with pytest.raises(MT5Error, match="valor do tick"):
        client.profit(EUR, "buy", 1.0, 1.1, 1.0999)


def test_profit_is_signed_and_falls_back_to_tick_value():
    client, fake = _client()
    assert client.profit(EUR, "buy", 0.1, 1.1, 1.095) == pytest.approx(-50.0)
    assert client.profit(EUR, "sell", 0.1, 1.1, 1.095) == pytest.approx(50.0)
    assert client.profit(EUR, "buy", 0.1, 1.1, 1.1) == 0.0
    fake.calc_profit_returns_none = True
    assert client.profit(EUR, "buy", 0.1, 1.1, 1.095) == pytest.approx(-50.0)
    assert client.profit(EUR, "sell", 0.1, 1.1, 1.095) == pytest.approx(50.0)
    with pytest.raises(ValueError):
        client.profit(EUR, "compra", 0.1, 1.1, 1.095)


def test_open_positions_and_orders_raw_values():
    client, _ = _client([NAKED], [make_order(10, EUR, fm.ORDER_TYPE_SELL_LIMIT, 0.5, 1.105, 1.1)])
    pos = client.open_positions()[0]
    assert pos["stop_loss"] is None and pos["take_profit"] is None and pos["lado"] == "buy"
    assert pos["abertura"] == datetime(2026, 9, 30, 11, 58, tzinfo=timezone.utc)
    order = client.pending_orders()[0]
    assert order["lado"] == "sell" and order["tipo"] == "venda limitada" and order["preco_limite"] is None


def test_orders_get_error_raises():
    client, fake = _client()
    client.ensure_connected()
    fake.orders_get = lambda *a, **k: None
    with pytest.raises(MT5Error, match="ordens pendentes"):
        client.pending_orders()
