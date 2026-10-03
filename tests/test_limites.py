"""Limites de risco do usuário e propostas de operação (MT5 simulado; nada é enviado)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import fake_mt5 as fm
from fake_mt5 import FakeMT5, make_balance_deal, make_deal, make_order, make_position, make_symbol
from test_mt5_client import make_client
from trading_mcp import limites
from trading_mcp.calendario import CalendarError

UTC = timezone.utc
# Relógio do FakeClock: quarta 2026-09-30 12:00 UTC. Dia de mercado desde terça 21:00 UTC (17:00 de Nova York);
# semana desde domingo 27/09 21:00 UTC.
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
EUR = "EURUSD"  # tick de 0,00001 vale 1 USD por lote; bid 1,10000, ask 1,10012
IN, OUT = fm.DEAL_ENTRY_IN, fm.DEAL_ENTRY_OUT
RULES = limites.RiskRules(1.25, 5.0, 25.0)


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())


def trade(pid: int, at: str, profit: float, commission: float = 0.0) -> list:
    """Uma operação fechada (entrada 1 h antes da saída em ``at``)."""
    exit_at = ts(at)
    return [make_deal(pid * 10, pid, EUR, "buy", IN, 0.5, 1.1, exit_at - 3600, order=pid * 10, commission=commission),
            make_deal(pid * 10 + 1, pid, EUR, "sell", OUT, 0.5, 1.1, exit_at, order=pid * 10 + 1, profit=profit,
                      commission=commission)]


# Depósito antes da semana; operação 1 na semana (antes do dia): -100 e comissão -1 por lado; operação 2 hoje: -150.
DEALS = [make_balance_deal(1, 10_000.0, ts("2026-09-20T10:00"))] + trade(1, "2026-09-28T11:00", -100, -1) + trade(
    2, "2026-09-30T10:00", -150)
BALANCE = 10_000 - 102 - 150
# Compra limitada pendente de 0,1 lote, stop a 20 pips: perde 20 USD se executada e estopada.
PENDING = [make_order(500, EUR, fm.ORDER_TYPE_BUY_LIMIT, 0.1, 1.095, 1.1, sl=1.093)]
MAX_RISK = 9_898 * 0.0125  # 1,25% da base do dia (123,725; arredondado no servidor)


def _client(deals=DEALS, balance=BALANCE, positions=(), orders=PENDING, **fake_kwargs):
    fake = FakeMT5([make_symbol(EUR)], deals=list(deals), balance=balance, positions=list(positions),
                   orders=list(orders), **fake_kwargs)
    return make_client(fake)


class StubCalendar:
    def __init__(self, events=(), error=None):
        self.events, self.error = list(events), error

    def events_between(self, start, end, min_importance="alta"):
        if self.error:
            raise self.error
        return [e for e in self.events if start <= datetime.fromisoformat(e["utc"].replace("Z", "+00:00")) <= end], True


def _news(at: str) -> dict:
    return {"utc": at, "codigo": "nonfarm-payrolls", "evento": "Payroll", "importancia": "alta"}


# ---------------------------------------------------------------- painel
def test_day_and_week_from_the_deal_history():
    client, _ = _client()
    out = limites.account_limits(client, RULES)
    day, week = out["dia"], out["semana"]
    assert day["inicio"]["utc"] == "2026-09-29T21:00:00Z" and week["inicio"]["utc"] == "2026-09-27T21:00:00Z"
    assert day["saldo_inicio"] == pytest.approx(9_898) and day["resultado"] == pytest.approx(-150)
    assert week["saldo_inicio"] == pytest.approx(10_000) and week["resultado"] == pytest.approx(-252)
    assert day["posicoes_com_saida"] == 1 and week["posicoes_com_saida"] == 2
    assert day["limite"] == pytest.approx(494.9) and week["limite"] == pytest.approx(2_500)
    assert out["exposicao_aberta"]["perda_nos_stops_pendentes"] == pytest.approx(20)
    assert day["disponivel"] == pytest.approx(494.9 - 150 - 20) and week["disponivel"] == pytest.approx(2_500 - 252 - 20)
    nxt = out["proxima_operacao"]
    assert nxt["pode_operar"] and nxt["limitado_por"] == "por_operacao" and nxt["risco_maximo"] == pytest.approx(MAX_RISK, abs=0.006)


def test_period_starts_exactly_at_17h_new_york():
    deals = [make_balance_deal(1, 10_000.0, ts("2026-09-20T10:00")),
             make_balance_deal(2, -10.0, ts("2026-09-29T20:59:59"), fm.DEAL_TYPE_CHARGE),  # véspera
             make_balance_deal(3, -20.0, ts("2026-09-29T21:00:00"), fm.DEAL_TYPE_CHARGE)]  # já o dia de hoje
    client, _ = _client(deals=deals, balance=9_970, orders=())
    out = limites.account_limits(client, RULES)
    assert out["dia"]["resultado"] == pytest.approx(-20) and out["semana"]["resultado"] == pytest.approx(-30)


def test_deal_after_the_local_clock_still_counts():
    # O relógio do Windows atrasado em relação ao servidor: a perda já está no saldo e precisa estar no resultado.
    deals = DEALS + trade(3, "2026-09-30T12:02", -300)
    client, _ = _client(deals=deals, balance=BALANCE - 300, orders=())
    day = limites.account_limits(client, RULES)["dia"]
    assert day["resultado"] == pytest.approx(-450) and day["saldo_inicio"] == pytest.approx(9_898)


def test_charges_count_as_result_and_credit_is_ignored():
    deals = DEALS + [make_balance_deal(7, -200.0, ts("2026-09-30T11:00"), fm.DEAL_TYPE_CHARGE),
                     make_balance_deal(8, 1_000.0, ts("2026-09-30T11:00"), fm.DEAL_TYPE_CREDIT)]
    client, _ = _client(deals=deals, balance=BALANCE - 200, orders=())
    day = limites.account_limits(client, RULES)["dia"]
    assert day["resultado"] == pytest.approx(-350) and day["outros_lancamentos"] == pytest.approx(-200)
    assert day["base_dos_limites"] == pytest.approx(9_898) and "depositos_e_saques" not in day
    assert day["disponivel"] == pytest.approx(494.9 - 350)


def test_deposit_inside_the_week_becomes_the_base():
    deals = [make_balance_deal(1, 400.0, ts("2026-09-29T10:00"))]
    client, _ = _client(deals=deals, balance=400.0, orders=())
    week = limites.account_limits(client, RULES)["semana"]
    assert week["saldo_inicio"] == pytest.approx(0) and week["base_dos_limites"] == pytest.approx(400)
    assert week["depositos_e_saques"] == pytest.approx(400) and week["limite"] == pytest.approx(100)
    assert week["situacao"] == "ok"


def test_withdrawing_everything_leaves_no_base():
    # Ganhou 500 hoje e sacou tudo: a base (saldo do início + saques) fica negativa.
    deals = [make_balance_deal(1, 10_000.0, ts("2026-09-20T10:00"))] + trade(3, "2026-09-30T10:00", 500) + [
        make_balance_deal(9, -10_500.0, ts("2026-09-30T11:00"))]
    client, _ = _client(deals=deals, balance=0.0, orders=())
    out = limites.account_limits(client, RULES)
    assert out["dia"]["situacao"] == "indeterminado" and not out["proxima_operacao"]["pode_operar"]


def test_position_without_stop_blocks_new_trades():
    position = make_position(300, EUR, "buy", 0.1, 1.099, 1.1, profit=10.0)  # sem stop
    client, _ = _client(positions=[position], orders=())
    out = limites.account_limits(client, RULES)
    assert out["dia"]["situacao"] == "indeterminado" and out["dia"]["disponivel"] is None
    assert not out["proxima_operacao"]["pode_operar"]
    assert any("sem stop" in w for w in out["avisos"])


def test_pending_order_without_stop_blocks_new_trades():
    client, _ = _client(orders=[make_order(501, EUR, fm.ORDER_TYPE_BUY_LIMIT, 0.1, 1.095, 1.1)])
    out = limites.account_limits(client, RULES)
    assert out["semana"]["situacao"] == "indeterminado" and not out["proxima_operacao"]["pode_operar"]


def test_accrued_swap_is_part_of_the_open_loss():
    position = make_position(300, EUR, "buy", 1.0, 1.1, 1.1, sl=1.098, swap=-80.0)  # -200 no stop, -80 de swap
    client, _ = _client(positions=[position], orders=())
    out = limites.account_limits(client, RULES)
    assert out["exposicao_aberta"]["swap_acumulado"] == pytest.approx(-80)
    assert out["dia"]["disponivel"] == pytest.approx(494.9 - 150 - 200 - 80)


def test_daily_loss_limit_reached():
    deals = DEALS + trade(3, "2026-09-30T11:00", -400)
    client, _ = _client(deals=deals, balance=BALANCE - 400, orders=())
    out = limites.account_limits(client, RULES)
    assert out["dia"]["situacao"] == "limite_atingido"  # -550 com limite de 494,90
    assert out["dia"]["disponivel"] == pytest.approx(494.9 - 550)  # sem stops abertos
    assert out["semana"]["situacao"] == "ok"
    assert not out["proxima_operacao"]["pode_operar"] and "chegou ao limite" in out["proxima_operacao"]["motivo"]


def test_weekly_limit_reached_while_the_day_is_fine():
    deals = DEALS + trade(3, "2026-09-28T15:00", -2_400)
    client, _ = _client(deals=deals, balance=BALANCE - 2_400, orders=())
    out = limites.account_limits(client, RULES)
    assert out["dia"]["situacao"] == "ok" and out["semana"]["situacao"] == "limite_atingido"
    assert not out["proxima_operacao"]["pode_operar"]


def test_week_can_be_the_binding_limit():
    deals = DEALS + trade(3, "2026-09-28T15:00", -2_200)  # sobram 2.500 - 2.452 = 48 na semana
    client, _ = _client(deals=deals, balance=BALANCE - 2_200, orders=())
    nxt = limites.account_limits(client, RULES)["proxima_operacao"]
    assert nxt["limitado_por"] == "semana" and nxt["risco_maximo"] == pytest.approx(48)


def test_open_stops_can_use_up_the_day():
    big = [make_order(501, EUR, fm.ORDER_TYPE_BUY_LIMIT, 2.0, 1.095, 1.1, sl=1.093)]  # perde 400 se estopada
    client, _ = _client(orders=big)
    out = limites.account_limits(client, RULES)
    assert out["dia"]["situacao"] == "comprometido_pelos_stops" and out["dia"]["disponivel"] < 0
    assert not out["proxima_operacao"]["pode_operar"]


def test_little_room_left_in_the_day_limits_the_next_trade():
    medium = [make_order(502, EUR, fm.ORDER_TYPE_BUY_LIMIT, 1.5, 1.095, 1.1, sl=1.093)]  # perde 300
    client, _ = _client(orders=medium)
    nxt = limites.account_limits(client, RULES)["proxima_operacao"]
    assert nxt["limitado_por"] == "dia" and nxt["risco_maximo"] == pytest.approx(494.9 - 150 - 300)


def test_a_cent_of_room_is_no_room():
    start = datetime(2026, 9, 29, 21, tzinfo=UTC)
    out = limites._period([], start, 100.0, 5.0, 4.995, "dia")  # sobra meio centavo
    assert out["situacao"] == "comprometido_pelos_stops"


# ---------------------------------------------------------------- propostas
def _store(tmp_path, name="p"):
    return limites.ProposalStore(tmp_path / name / "propostas.sqlite3")


def test_valid_pending_proposal_is_sized_stored_and_expires(tmp_path):
    client, _ = _client(orders=())
    store = _store(tmp_path)
    out = limites.propose(client, store, RULES, "EURUSD", 1.0990, 1.0970, 1.1030, 30)
    assert out["status"] == "valida" and out["tipo_ordem"] == "compra_limitada"
    assert "nada foi enviado" in out["envio"]
    # Pendente executa no próprio preço: 20 pips = 200 USD/lote; 123,73 / 200 -> 0,61 lote.
    assert out["preco_execucao_estimado"] == 1.0990
    assert out["volume"] == pytest.approx(0.61) and out["risco"] == pytest.approx(122.0)
    assert out["risco_retorno"] == pytest.approx(2.0) and out["lucro_no_alvo"] == pytest.approx(0.61 * 400)
    assert out["limitado_por"] == "por_operacao" and len(out["assinatura"]) == 64
    assert store.verify(out["id"])
    listed = store.list(NOW)["propostas"]
    assert [(p["id"], p["situacao"]) for p in listed] == [(out["id"], "valida")]
    assert store.list(NOW + timedelta(minutes=31))["propostas"][0]["situacao"] == "expirada"


def test_market_orders_are_sized_from_the_price_they_would_get(tmp_path):
    client, _ = _client(orders=())
    store = _store(tmp_path)
    # Venda com a entrada no ask: executa no bid (1,10000); o lote sai do pior preço dentro do desvio (24 pontos =
    # 2 spreads): 1,09976 até o stop 1,10212 = 23,6 pips = 236/lote -> 0,52 lote, 122,72.
    sell = limites.propose(client, store, RULES, "EURUSD", 1.10012, 1.10212)
    assert sell["tipo_ordem"] == "a_mercado" and sell["preco_execucao_estimado"] == pytest.approx(1.1)
    assert sell["volume"] == pytest.approx(0.52) and sell["risco"] == pytest.approx(0.52 * 236)
    assert sell["risco"] <= MAX_RISK
    # Compra com a entrada no bid: executa no ask (1,10012); pior preço 1,10036.
    buy = limites.propose(client, store, RULES, "EURUSD", 1.1, 1.098)
    assert buy["tipo_ordem"] == "a_mercado" and buy["preco_execucao_estimado"] == pytest.approx(1.10012)
    assert buy["volume"] == pytest.approx(0.52)


def test_market_price_beyond_the_stop_is_refused(tmp_path):
    client, _ = _client(orders=())
    store = _store(tmp_path)
    # Compra a mercado executaria no ask 1,10012, já abaixo do stop 1,10013.
    buy = limites.propose(client, store, RULES, "EURUSD", 1.10015, 1.10013)
    assert buy["tipo_ordem"] == "a_mercado" and buy["status"] == "recusada"
    assert any("já passou do stop" in r for r in buy["motivos_da_recusa"])
    # Venda a mercado executaria no bid 1,10000, já acima do stop 1,09998.
    sell = limites.propose(client, store, RULES, "EURUSD", 1.09995, 1.09998)
    assert sell["tipo_ordem"] == "a_mercado" and sell["status"] == "recusada"


def test_proposal_is_refused_when_the_day_is_blocked(tmp_path):
    position = make_position(300, EUR, "buy", 0.1, 1.099, 1.1, profit=10.0)  # sem stop
    client, _ = _client(positions=[position], orders=())
    store = _store(tmp_path)
    out = limites.propose(client, store, RULES, "EURUSD", 1.0990, 1.0970)
    assert out["status"] == "recusada" and out["volume"] == 0
    assert any("sem stop" in r for r in out["motivos_da_recusa"])
    assert store.list(NOW)["propostas"][0]["situacao"] == "recusada"


def test_proposal_is_refused_when_the_minimum_lot_is_too_risky(tmp_path):
    client, _ = _client(orders=())
    out = limites.propose(client, _store(tmp_path), RULES, "EURUSD", 1.0990, 0.9000)  # 1.990 pips
    assert out["status"] == "recusada" and any("lote mínimo" in r for r in out["motivos_da_recusa"])


def test_proposal_is_refused_without_margin(tmp_path):
    client, _ = _client(orders=())
    client.margin = lambda *args: 1e9  # type: ignore[method-assign]
    out = limites.propose(client, _store(tmp_path), RULES, "EURUSD", 1.0990, 1.0970)
    assert out["status"] == "recusada" and out["volume"] == 0 and any("Margem" in r for r in out["motivos_da_recusa"])


def test_proposal_is_refused_without_a_quote(tmp_path):
    client, _ = _client(orders=(), zero_tick_symbols={EUR})
    out = limites.propose(client, _store(tmp_path), RULES, "EURUSD", 1.0990, 1.0970)
    assert out["status"] == "recusada" and out["tipo_ordem"] == "desconhecido"
    assert any("Sem cotação" in r for r in out["motivos_da_recusa"])


def test_proposal_is_refused_on_a_real_account(tmp_path):
    client, _ = _client(orders=(), trade_mode=fm.ACCOUNT_TRADE_MODE_REAL)
    store = _store(tmp_path)
    out = limites.propose(client, store, RULES, "EURUSD", 1.0990, 1.0970)
    assert out["status"] == "recusada" and any("só na conta demo" in r for r in out["motivos_da_recusa"])
    with sqlite3.connect(store._path) as conn:
        assert conn.execute("SELECT conta_demo FROM propostas").fetchone()[0] == 0


def test_proposal_is_refused_when_the_terminal_is_disconnected(tmp_path):
    client, fake = _client(orders=())
    client.account()  # conecta
    fake.connected = False
    out = limites.propose(client, _store(tmp_path), RULES, "EURUSD", 1.0990, 1.0970)
    assert out["status"] == "recusada" and any("sem conexão" in r.lower() for r in out["motivos_da_recusa"])


def test_order_type_follows_the_current_price(tmp_path):
    client, _ = _client(orders=())
    store = _store(tmp_path)
    assert limites.propose(client, store, RULES, "EURUSD", 1.1001, 1.0980)["tipo_ordem"] == "a_mercado"
    assert limites.propose(client, store, RULES, "EURUSD", 1.1050, 1.1030)["tipo_ordem"] == "compra_stop"
    assert limites.propose(client, store, RULES, "EURUSD", 1.1050, 1.1070)["tipo_ordem"] == "venda_limitada"
    assert limites.propose(client, store, RULES, "EURUSD", 1.0950, 1.0970)["tipo_ordem"] == "venda_stop"
    # Venda a um pouco mais de um spread acima do bid já é limitada, não a mercado.
    assert limites.propose(client, store, RULES, "EURUSD", 1.10013, 1.10213)["tipo_ordem"] == "venda_limitada"


def test_pending_sell_risk_is_the_distance(tmp_path):
    client, _ = _client(orders=())
    out = limites.propose(client, _store(tmp_path), RULES, "EURUSD", 1.1050, 1.1070)
    assert out["volume"] == pytest.approx(0.61) and out["risco"] == pytest.approx(122.0)


def test_news_window_runs_until_after_expiry(tmp_path):
    client, _ = _client(orders=())
    calendar = StubCalendar([_news("2026-09-30T12:40:00Z"), _news("2026-09-30T12:50:00Z")])
    out = limites.propose(client, _store(tmp_path), RULES, "EURUSD", 1.0990, 1.0970, None, 30, calendar,
                          lambda s, d, w, p: {"sessao": "nova_york", "direcao": d})
    assert [n["utc"] for n in out["noticias_na_validade"]] == ["2026-09-30T12:40:00Z"]  # validade 12:30 + 15 min
    assert any("Payroll às 12:40 UTC" in w for w in out["avisos"])
    assert out["contexto_smc"] == {"sessao": "nova_york", "direcao": "compra"}


def test_calendar_or_context_failure_is_only_a_warning(tmp_path):
    client, _ = _client(orders=())

    def broken(*args):
        raise KeyError("estrutura")

    out = limites.propose(client, _store(tmp_path), RULES, "EURUSD", 1.0990, 1.0970, None, 30,
                          StubCalendar(error=CalendarError("arquivo ausente")), broken)
    assert out["status"] == "valida"
    assert any("Calendário indisponível" in w for w in out["avisos"])
    assert any("Contexto SMC não medido (KeyError" in w for w in out["avisos"])


def test_proposals_do_not_reserve_risk_but_warn(tmp_path):
    client, _ = _client()  # 324,90 de espaço no dia
    store = _store(tmp_path)
    first = limites.propose(client, store, RULES, "EURUSD", 1.0990, 1.0970)
    second = limites.propose(client, store, RULES, "EURUSD", 1.0990, 1.0970)
    third = limites.propose(client, store, RULES, "EURUSD", 1.0990, 1.0970)
    assert not any("não reservam risco" in w for w in first.get("avisos", []) + second.get("avisos", []))
    assert any("2 outra(s) proposta(s) válida(s)" in w for w in third["avisos"])


@pytest.mark.parametrize(
    ("entry", "stop", "target", "validity", "message"),
    [
        (1.0990, 1.0970, 1.0980, 30, "lado do lucro"),
        (1.0990, 1.0990, None, 30, "diferente"),
        (1.0990, 1.0970, None, 3, "validade_min"),
        (1.0990, 1.0970, None, 300, "validade_min"),
        (1.0990, 1.098999, None, 30, "tick mínimo"),
    ],
)
def test_invalid_proposals(tmp_path, entry, stop, target, validity, message):
    client, _ = _client(orders=())
    with pytest.raises(ValueError, match=message):
        limites.propose(client, _store(tmp_path), RULES, "EURUSD", entry, stop, target, validity)


# ---------------------------------------------------------------- assinatura
ITEM = {"id": "abcd1234", "criada_utc": "2026-09-30T12:00:00Z", "expira_utc": "2026-09-30T12:30:00Z",
        "conta_login": 1, "conta_servidor": "s", "conta_demo": 1, "simbolo": "EURUSD", "direcao": "compra",
        "tipo_ordem": "compra_limitada", "entrada": 1.099, "stop": 1.097, "alvo": None, "volume": 0.58,
        "risco_valor": 116.0, "status": "valida"}


def test_signature_binds_every_field_and_ignores_int_vs_float(tmp_path):
    store = _store(tmp_path)
    base = store.sign(ITEM)
    assert base == store.sign({**ITEM, "risco_valor": 116, "volume": 0.58})
    changes = {"id": "x", "criada_utc": "2026-09-30T12:01:00Z", "expira_utc": "2026-09-30T12:31:00Z", "conta_login": 2,
               "conta_servidor": "t", "conta_demo": 0, "simbolo": "GBPUSD", "direcao": "venda", "tipo_ordem": "a_mercado",
               "entrada": 1.0991, "stop": 1.0969, "alvo": 1.103, "volume": 0.59, "risco_valor": 117.0,
               "status": "recusada"}
    assert set(changes) == set(limites._SIGNED)
    for key, value in changes.items():
        assert store.sign({**ITEM, key: value}) != base, key
    assert _store(tmp_path, "outra").sign(ITEM) != base  # outra chave


def test_tampered_proposal_fails_verification(tmp_path):
    client, _ = _client(orders=())
    store = _store(tmp_path)
    out = limites.propose(client, store, RULES, "EURUSD", 1.0990, 1.0970)
    assert store.verify(out["id"])
    with sqlite3.connect(store._path) as conn:
        conn.execute("UPDATE propostas SET volume = 5 WHERE id = ?", (out["id"],))
    assert not store.verify(out["id"])
    assert store._key_path.is_file() and store._key_path.parent == store._path.parent


def test_listing_without_proposals_says_how_to_start(tmp_path):
    with pytest.raises(limites.LimitesError, match="proposta_operacao"):
        _store(tmp_path).list(NOW)


def test_details_are_kept_for_the_next_stage(tmp_path):
    client, _ = _client(orders=())
    store = _store(tmp_path)
    out = limites.propose(client, store, RULES, "EURUSD", 1.0990, 1.0970, 1.1030, 30)
    with sqlite3.connect(store._path) as conn:
        row = conn.execute("SELECT detalhes, conta_demo FROM propostas WHERE id = ?", (out["id"],)).fetchone()
    details = json.loads(row[0])
    assert row[1] == 1 and details["preco_execucao_estimado"] == 1.099
    assert details["limites"]["proxima_operacao"]["risco_maximo"] == pytest.approx(MAX_RISK, abs=0.006)


# ---------------------------------------------------------------- casos que protegem a execução futura
def test_stale_quote_refuses_a_market_order(tmp_path):
    client, _ = _client(orders=(), tick_age_s={EUR: 900})  # último tick há 15 min
    store = _store(tmp_path)
    market = limites.propose(client, store, RULES, "EURUSD", 1.1, 1.098)
    assert market["tipo_ordem"] == "a_mercado" and market["status"] == "recusada"
    assert any("precisa do preço de agora" in r for r in market["motivos_da_recusa"])
    pending = limites.propose(client, store, RULES, "EURUSD", 1.0990, 1.0970)  # pendente: só avisa
    assert pending["status"] == "valida" and any("não é de agora" in w for w in pending["avisos"])


def test_market_fill_beyond_the_target_is_refused_and_rr_uses_the_fill(tmp_path):
    client, _ = _client(orders=())
    store = _store(tmp_path)
    past = limites.propose(client, store, RULES, "EURUSD", 1.1, 1.098, 1.10005)  # executaria em 1,10012
    assert past["status"] == "recusada" and any("já passou do alvo" in r for r in past["motivos_da_recusa"])
    ok = limites.propose(client, store, RULES, "EURUSD", 1.1, 1.098, 1.104)
    assert ok["risco_retorno"] == pytest.approx(round(0.00388 / 0.00212, 2))  # do ask, não da entrada (2,0)


def test_open_risk_counts_only_valid_unexpired_proposals_of_the_account(tmp_path):
    client, _ = _client(orders=())
    store = _store(tmp_path)
    out = limites.propose(client, store, RULES, "EURUSD", 1.0990, 1.0970, None, 30)
    limites.propose(client, store, RULES, "EURUSD", 1.0990, 0.9000)  # recusada: não conta
    login, server = client.account()["login"], client.account()["servidor"]
    assert store.open_risk(NOW, login, server) == (1, pytest.approx(out["risco"]))
    assert store.open_risk(NOW + timedelta(minutes=31), login, server) == (0, 0.0)
    assert store.open_risk(NOW, login + 1, server) == (0, 0.0)
    assert store.open_risk(NOW, login, server + "x") == (0, 0.0)


def _deal(at: datetime, profit: float) -> dict:
    return {"horario": at, "tipo": "sell", "entrada": "out", "posicao_id": 1, "lucro": profit, "comissao": 0.0,
            "swap": 0.0, "taxa": 0.0}


def test_a_cent_left_in_the_realized_limit_is_the_limit():
    start = datetime(2026, 9, 29, 21, tzinfo=UTC)
    out = limites._period([_deal(start + timedelta(hours=1), -4.995)], start, 95.005, 5.0, 0.0, "dia")
    assert out["base_dos_limites"] == pytest.approx(100) and out["situacao"] == "limite_atingido"


def test_per_trade_limit_below_a_cent_blocks():
    client, _ = _client(deals=[], balance=0.5, orders=())  # 1,25% de 0,50 = 0,006
    nxt = limites.account_limits(client, RULES)["proxima_operacao"]
    assert not nxt["pode_operar"]


def test_signature_depends_on_the_signature_version(tmp_path, monkeypatch):
    store = _store(tmp_path)
    before = store.sign(ITEM)
    monkeypatch.setattr(limites, "SIGNATURE_VERSION", limites.SIGNATURE_VERSION + 1)
    assert store.sign(ITEM) != before


@pytest.mark.parametrize("content", ["", "ab", "00" * 16])
def test_empty_or_short_key_is_rejected(tmp_path, content):
    store = _store(tmp_path)
    store._key_path.parent.mkdir(parents=True)
    store._key_path.write_text(content, encoding="ascii")
    with pytest.raises(limites.LimitesError, match="inválida"):
        store.sign(ITEM)


def test_key_is_created_once_and_reused(tmp_path):
    store = _store(tmp_path)
    first = store.sign(ITEM)
    key = store._key_path.read_text(encoding="ascii")
    assert len(bytes.fromhex(key)) == limites.KEY_BYTES
    assert limites.ProposalStore(store._path).sign(ITEM) == first  # outra instância, mesma chave
    assert not list(store._key_path.parent.glob("*.tmp"))  # o temporário não fica para trás
