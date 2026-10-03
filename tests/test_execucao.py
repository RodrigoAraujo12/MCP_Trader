"""Execução de propostas na conta demo (etapa F) com o MT5 simulado e a janela de aprovação simulada."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import fake_mt5 as fm
from fake_mt5 import FakeMT5, make_order, make_symbol
from test_mt5_client import make_client
from trading_mcp import execucao, limites
from trading_mcp.execucao import ExecucaoError
from trading_mcp.mt5_client import MT5Error

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # relógio do FakeClock
EUR = "EURUSD"  # bid 1,10000, ask 1,10012
RULES = limites.RiskRules(1.25, 5.0, 25.0)


class Approver:
    """Janela simulada: devolve a resposta configurada e guarda o que foi mostrado."""

    def __init__(self, answer="aprovada", during=None):
        self.answer, self.during, self.calls = answer, during, []

    def __call__(self, title, text, timeout_s):
        self.calls.append((title, text, timeout_s))
        if self.during:
            self.during()
        return self.answer


def _setup(tmp_path, *, enabled=True, **fake_kwargs):
    fake = FakeMT5([make_symbol(EUR)], trading=True, **fake_kwargs)
    path = tmp_path / "p" / "propostas.sqlite3"  # nunca a pasta real (~/trading-mcp) nem o PARAR_EXECUCOES de lá
    client, fake = make_client(fake, execution_enabled=enabled, propostas_path=path)
    store = limites.ProposalStore(path)
    return client, fake, store


def _propose(client, store, entry=1.0990, stop=1.0970, target=1.1030, validity=30):
    out = limites.propose(client, store, RULES, "EURUSD", entry, stop, target, validity)
    assert out["status"] == "valida", out.get("motivos_da_recusa")
    return out


def _execute(client, store, proposal_id, approver, enabled=True, **kwargs):
    return execucao.execute(client, store, RULES, proposal_id, enabled=enabled, approve=approver, **kwargs)


# ---------------------------------------------------------------- caminho feliz
def test_pending_order_is_placed_after_approval(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    approver = Approver()
    out = _execute(client, store, proposal["id"], approver)
    assert out["status"] == "colocada" and out["envio"].startswith("ordem enviada à conta DEMO")
    assert out["conferido_no_terminal"] is True
    [request] = fake.sent
    assert request["action"] == fm.TRADE_ACTION_PENDING and request["type"] == fm.ORDER_TYPE_BUY_LIMIT
    assert (request["price"], request["sl"], request["tp"], request["volume"]) == (1.099, 1.097, 1.103, 0.62)
    assert request["type_time"] == fm.ORDER_TIME_SPECIFIED
    assert request["expiration"] == int((NOW + timedelta(minutes=30)).timestamp())  # expira junto com a proposta
    assert request["comment"] == f"mcp {proposal['id']}" and request["magic"] == execucao.MAGIC
    assert request["type_filling"] == fm.ORDER_FILLING_RETURN  # pendente tenta RETURN primeiro
    title, text, wait = approver.calls[0]
    assert "DEMO" in title and "Lote: 0.62" in text and "Stop: 1.097" in text and "Alvo: 1.103" in text
    assert "124.00 USD" in text and wait == pytest.approx(execucao.APPROVAL_TIMEOUT_S)
    listed = store.list(NOW)["propostas"][0]
    assert listed["situacao"] == "execucao_colocada" and listed["execucao"]["ordem"] == out["ordem"]


def test_market_order_uses_the_current_price(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store, entry=1.1, stop=1.098, target=1.104)
    out = _execute(client, store, proposal["id"], Approver())
    assert out["status"] == "executada" and out["conferido_no_terminal"] is True
    [request] = fake.sent
    assert request["action"] == fm.TRADE_ACTION_DEAL and request["type"] == fm.ORDER_TYPE_BUY
    assert request["price"] == pytest.approx(1.10012) and request["deviation"] >= 10
    assert request["type_filling"] == fm.ORDER_FILLING_FOK


# ---------------------------------------------------------------- sem aprovação, nada sai
@pytest.mark.parametrize(("answer", "status"), [("recusada", "recusada_pelo_usuario"), ("sem_resposta", "sem_resposta"),
                                                ("erro_na_janela", "erro_na_janela")])
def test_nothing_is_sent_without_approval_and_there_is_no_second_try(tmp_path, answer, status):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    out = _execute(client, store, proposal["id"], Approver(answer))
    assert out["status"] == status and "nada foi enviado" in out["envio"]
    assert fake.sent == [] and fake.checked == []
    with pytest.raises(ExecucaoError, match="já teve uma tentativa"):
        _execute(client, store, proposal["id"], Approver())
    assert fake.sent == []


def test_disabled_execution_refuses_before_anything(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    approver = Approver()
    with pytest.raises(ExecucaoError, match="EXECUCAO_HABILITADA=sim"):
        _execute(client, store, proposal["id"], approver, enabled=False)
    assert approver.calls == [] and fake.sent == [] and store.execution(proposal["id"]) is None


def test_kill_switch_blocks_and_only_the_user_resumes(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    stopped = execucao.stop_all(store, "teste")
    assert stopped["parado"] and "Apague o arquivo" in stopped["como_retomar"]
    assert execucao.stop_all(store)["ja_estava_parado"] is True
    with pytest.raises(ExecucaoError, match="Execuções paradas"):
        _execute(client, store, proposal["id"], Approver())
    execucao.kill_file(store).unlink()
    assert _execute(client, store, proposal["id"], Approver())["status"] == "colocada"


def test_kill_switch_during_approval_aborts(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    out = _execute(client, store, proposal["id"], Approver(during=lambda: execucao.stop_all(store)))
    assert out["status"] == "abortada" and fake.sent == []


# ---------------------------------------------------------------- travas da proposta
def test_tampered_proposal_is_not_executed(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    with sqlite3.connect(store._path) as conn:
        conn.execute("UPDATE propostas SET volume = 5 WHERE id = ?", (proposal["id"],))
    with pytest.raises(ExecucaoError, match="alterada depois de assinada"):
        _execute(client, store, proposal["id"], Approver())
    assert fake.sent == []


def test_refused_or_missing_proposal_is_not_executed(tmp_path):
    client, fake, store = _setup(tmp_path)
    refused = limites.propose(client, store, RULES, "EURUSD", 1.0990, 0.9000)  # lote mínimo arriscado demais
    assert refused["status"] == "recusada"
    with pytest.raises(ExecucaoError, match="recusada ao ser criada"):
        _execute(client, store, refused["id"], Approver())
    with pytest.raises(ExecucaoError, match="não encontrada"):
        _execute(client, store, "ffffffff", Approver())


def test_expired_proposal_is_not_executed(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store, validity=5)
    client.clock.now = NOW + timedelta(minutes=5)
    with pytest.raises(ExecucaoError, match="expirou"):
        _execute(client, store, proposal["id"], Approver())


def test_wait_never_runs_past_the_proposal(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store, validity=5)
    client.clock.now = NOW + timedelta(minutes=4, seconds=30)  # faltam 30 s
    approver = Approver("sem_resposta")
    _execute(client, store, proposal["id"], approver)
    assert approver.calls[0][2] == pytest.approx(15)  # 30 s - 15 s de margem


# ---------------------------------------------------------------- travas da conta e do terminal
def test_account_must_still_be_demo(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    fake.trade_mode = fm.ACCOUNT_TRADE_MODE_REAL
    with pytest.raises(ExecucaoError, match="só na conta demo"):
        _execute(client, store, proposal["id"], Approver())
    assert fake.sent == []


def test_terminal_must_allow_algo_trading(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    fake.tradeapi_disabled = True
    with pytest.raises(ExecucaoError, match="API Python externa"):
        _execute(client, store, proposal["id"], Approver())
    fake.tradeapi_disabled, fake.algo_trading = False, False
    with pytest.raises(ExecucaoError, match="Algo Trading"):
        _execute(client, store, proposal["id"], Approver())


def test_client_refuses_orders_unless_enabled_and_demo(tmp_path):
    client, fake, store = _setup(tmp_path, enabled=False)
    account = (client.account()["login"], client.account()["servidor"])
    with pytest.raises(MT5Error, match="Execução desativada"):
        client.send_order({"symbol": "EURUSD"}, account)
    client2, fake2, _ = _setup(tmp_path / "real", trade_mode=fm.ACCOUNT_TRADE_MODE_REAL)
    account2 = (client2.account()["login"], client2.account()["servidor"])
    with pytest.raises(MT5Error, match="só na conta demo"):
        client2.check_order({"symbol": "EURUSD"}, account2)
    with pytest.raises(MT5Error, match="outra conta"):
        client2.check_order({"symbol": "EURUSD"}, (account2[0] + 1, account2[1]))
    assert fake.sent == [] and fake2.checked == []


# ---------------------------------------------------------------- mercado mudou
def test_price_change_before_approval_refuses(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)  # compra limitada em 1,0990
    fake.symbols[EUR] = make_symbol(EUR, bid=1.0980, ask=1.09812)  # o ask caiu abaixo da entrada
    approver = Approver()
    with pytest.raises(ExecucaoError, match="O preço mudou"):
        _execute(client, store, proposal["id"], approver)
    assert approver.calls == []


def test_price_change_during_approval_aborts(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)

    def move():
        fake.symbols[EUR] = make_symbol(EUR, bid=1.0980, ask=1.09812)

    out = _execute(client, store, proposal["id"], Approver(during=move))
    assert out["status"] == "abortada" and "O preço mudou" in out["envio"] and fake.sent == []
    assert store.execution(proposal["id"])["status"] == "abortada"


def test_limits_used_up_since_the_proposal_refuse(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    fake.orders.append(make_order(700, EUR, fm.ORDER_TYPE_BUY_LIMIT, 2.5, 1.095, 1.1, sl=1.093))  # 500 no stop
    with pytest.raises(ExecucaoError, match="Limites não permitem"):
        _execute(client, store, proposal["id"], Approver())


def test_stale_quote_refuses_execution(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    fake.tick_age_s[EUR] = 900
    with pytest.raises(ExecucaoError, match="só executo com o preço de agora"):
        _execute(client, store, proposal["id"], Approver())


# ---------------------------------------------------------------- servidor da corretora
def test_filling_falls_back_when_the_server_refuses_one(tmp_path):
    client, fake, store = _setup(tmp_path, unsupported_fillings={fm.ORDER_FILLING_RETURN})
    proposal = _propose(client, store)
    out = _execute(client, store, proposal["id"], Approver())
    assert out["status"] == "colocada"
    assert [c["type_filling"] for c in fake.checked] == [fm.ORDER_FILLING_RETURN, fm.ORDER_FILLING_FOK]
    assert fake.sent[0]["type_filling"] == fm.ORDER_FILLING_FOK


def test_server_validation_failure_sends_nothing(tmp_path):
    every = {fm.ORDER_FILLING_RETURN, fm.ORDER_FILLING_FOK, fm.ORDER_FILLING_IOC}
    client, fake, store = _setup(tmp_path, unsupported_fillings=every)
    proposal = _propose(client, store)
    out = _execute(client, store, proposal["id"], Approver())
    assert out["status"] == "recusada_pelo_servidor" and fake.sent == []


def test_broker_rejection_is_reported(tmp_path):
    client, fake, store = _setup(tmp_path, send_retcode=10006)
    proposal = _propose(client, store)
    out = _execute(client, store, proposal["id"], Approver())
    assert out["status"] == "rejeitada" and "rejeitada pela corretora" in out["envio"]
    assert store.execution(proposal["id"])["retcode"] == 10006


# ---------------------------------------------------------------- tudo de novo depois do clique
def _during(tmp_path, change, **fake_kwargs):
    client, fake, store = _setup(tmp_path, **fake_kwargs)
    proposal = _propose(client, store)
    out = _execute(client, store, proposal["id"], Approver(during=lambda: change(client, fake, store, proposal)))
    return out, fake, store, proposal


def test_tampering_during_approval_aborts(tmp_path):
    def tamper(client, fake, store, proposal):
        with sqlite3.connect(store._path) as conn:
            conn.execute("UPDATE propostas SET stop = 1.0 WHERE id = ?", (proposal["id"],))

    out, fake, *_ = _during(tmp_path, tamper)
    assert out["status"] == "abortada" and "alterada" in out["envio"] and fake.sent == []


def test_account_switch_during_approval_aborts(tmp_path):
    out, fake, *_ = _during(tmp_path, lambda c, f, s, p: setattr(f, "login", f.login + 1))
    assert out["status"] == "abortada" and "outra conta" in out["envio"] and fake.sent == []


def test_real_account_during_approval_aborts(tmp_path):
    out, fake, *_ = _during(tmp_path, lambda c, f, s, p: setattr(f, "trade_mode", fm.ACCOUNT_TRADE_MODE_REAL))
    assert out["status"] == "abortada" and fake.sent == []


def test_expiry_during_approval_aborts(tmp_path):
    out, fake, *_ = _during(tmp_path, lambda c, f, s, p: setattr(c.clock, "now", NOW + timedelta(minutes=31)))
    assert out["status"] == "abortada" and "expirou" in out["envio"] and fake.sent == []


def test_limits_used_up_during_approval_abort(tmp_path):
    def big_order(client, fake, store, proposal):
        fake.orders.append(make_order(701, EUR, fm.ORDER_TYPE_BUY_LIMIT, 2.5, 1.095, 1.1, sl=1.093))

    out, fake, *_ = _during(tmp_path, big_order)
    assert out["status"] == "abortada" and "Limites" in out["envio"] and fake.sent == []


def test_kill_switch_right_before_sending(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    original = client.check_order

    def check_then_stop(request, account):
        answer = original(request, account)
        execucao.stop_all(store)  # parado entre a validação e o envio
        return answer

    client.check_order = check_then_stop  # type: ignore[method-assign]
    out = _execute(client, store, proposal["id"], Approver())
    assert out["status"] == "abortada" and fake.sent == []


def test_account_switch_at_send_time_is_caught_by_the_client(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    original = client.check_order

    def check_then_switch(request, account):
        answer = original(request, account)
        fake.login += 1  # outra conta demo logada no terminal
        return answer

    client.check_order = check_then_switch  # type: ignore[method-assign]
    out = _execute(client, store, proposal["id"], Approver())
    assert out["status"] == "falhou" and "outra conta" in out["envio"] and fake.sent == []


# ---------------------------------------------------------------- preço, desvio e resposta da corretora
def test_market_price_past_the_stop_refuses(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store, entry=1.1, stop=1.098, target=1.104)
    # Spread de 200 pontos: ainda a mercado (a entrada está a um spread do ask), mas o ask já está no stop.
    fake.symbols[EUR] = make_symbol(EUR, bid=1.096, ask=1.098)
    with pytest.raises(ExecucaoError, match="já passou do stop"):
        _execute(client, store, proposal["id"], Approver())
    assert fake.sent == []


def test_market_deviation_counts_in_the_risk(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store, entry=1.1, stop=1.098)  # 0,52 lote
    fake.symbols[EUR] = make_symbol(EUR, bid=1.1, ask=1.1005)  # spread de 50 pontos: desvio de 100
    with pytest.raises(ExecucaoError, match="desvio máximo de 100 pontos"):
        _execute(client, store, proposal["id"], Approver())


def test_dialog_tells_market_variation_expiry_and_news(tmp_path):
    client, fake, store = _setup(tmp_path)
    market = _propose(client, store, entry=1.1, stop=1.098)
    approver = Approver("recusada")
    _execute(client, store, market["id"], approver)
    assert "o preço pode variar" in approver.calls[0][1] and "24 pontos de desvio" in approver.calls[0][1]
    pending = _propose(client, store)
    with sqlite3.connect(store._path) as conn:  # notícia gravada nos detalhes (fora da assinatura)
        conn.execute("UPDATE propostas SET detalhes = ? WHERE id = ?",
                     ('{"noticias": [{"evento": "Payroll", "utc": "2026-09-30T12:30:00Z"}]}', pending["id"]))
    _execute(client, store, pending["id"], approver)
    text = approver.calls[1][1]
    assert "expira às" in text and "Payroll às 12:30 UTC" in text


def test_pending_order_answered_with_done_is_still_placed(tmp_path):
    client, fake, store = _setup(tmp_path, send_retcode=fm.TRADE_RETCODE_DONE)
    proposal = _propose(client, store)
    out = _execute(client, store, proposal["id"], Approver())
    assert out["status"] == "colocada" and out["conferido_no_terminal"] is True


def test_server_refusal_other_than_filling_stops_the_checks(tmp_path):
    client, fake, store = _setup(tmp_path, check_retcode=10019)  # sem dinheiro
    proposal = _propose(client, store)
    out = _execute(client, store, proposal["id"], Approver())
    assert out["status"] == "recusada_pelo_servidor" and "10019" in out["envio"]
    assert len(fake.checked) == 1 and fake.sent == []


def test_result_survives_a_database_failure_after_sending(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    original = store.update_execution

    def flaky(proposal_id, **fields):
        if fields.get("status") == "colocada":
            raise limites.LimitesError("database is locked")
        return original(proposal_id, **fields)

    store.update_execution = flaky  # type: ignore[method-assign]
    out = _execute(client, store, proposal["id"], Approver())
    assert out["status"] == "colocada" and len(fake.sent) == 1
    assert any("não gravado" in w for w in out["avisos"])
    assert store.execution(proposal["id"])["status"] == "enviando"  # com a requisição gravada antes do envio


def test_only_one_execution_at_a_time(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    assert execucao._RUNNING.acquire(blocking=False)
    try:
        with pytest.raises(ExecucaoError, match="Outra execução"):
            _execute(client, store, proposal["id"], Approver())
    finally:
        execucao._RUNNING.release()
    assert store.execution(proposal["id"]) is None


def test_signed_non_demo_proposal_is_refused(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    row = store.get(proposal["id"])
    row.update(id="abcdef01", conta_demo=0)
    row["assinatura"] = store.sign(row)
    store.save(row)
    with pytest.raises(ExecucaoError, match="só na conta demo"):
        _execute(client, store, "abcdef01", Approver())


def test_window_failure_sends_nothing(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)

    def broken(*args):
        raise OSError("sem área de trabalho")

    out = _execute(client, store, proposal["id"], broken)
    assert out["status"] == "erro_na_janela" and fake.sent == []


def test_algo_trading_turned_off_during_approval_aborts(tmp_path):
    out, fake, *_ = _during(tmp_path, lambda c, f, s, p: setattr(f, "algo_trading", False))
    assert out["status"] == "abortada" and "Algo Trading" in out["envio"] and fake.sent == []


def test_client_itself_honours_the_kill_switch(tmp_path):
    fake = FakeMT5([make_symbol(EUR)], trading=True)
    client, fake = make_client(fake, execution_enabled=True, propostas_path=tmp_path / "p" / "propostas.sqlite3")
    account = (client.account()["login"], client.account()["servidor"])
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / execucao.KILL_FILE).write_text("parado", encoding="utf-8")
    with pytest.raises(MT5Error, match="Execuções paradas"):
        client.send_order({"symbol": "EURUSD"}, account)
    assert fake.sent == []


def test_deviation_alone_can_exceed_the_limit(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store, entry=1.1, stop=1.098)  # 0,52 lote
    # Spread de 20 pontos: no preço de agora 0,52 x 220 = 114,40 cabe; com 40 pontos de desvio, 135,20 não.
    fake.symbols[EUR] = make_symbol(EUR, bid=1.1, ask=1.1002)
    with pytest.raises(ExecucaoError, match="desvio máximo de 40 pontos"):
        _execute(client, store, proposal["id"], Approver())


def test_expiry_right_before_sending_aborts(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    original = client.check_order

    def slow_check(request, account):
        answer = original(request, account)
        client.clock.now = NOW + timedelta(minutes=30)  # a validação demorou até o fim da validade
        return answer

    client.check_order = slow_check  # type: ignore[method-assign]
    out = _execute(client, store, proposal["id"], Approver())
    assert out["status"] == "abortada" and "expirou" in out["envio"] and fake.sent == []


def test_disconnected_terminal_refuses(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    fake.connected = False
    with pytest.raises(ExecucaoError, match="sem conexão"):
        _execute(client, store, proposal["id"], Approver())
    assert fake.sent == []


def test_unexpected_error_after_approval_aborts(tmp_path, monkeypatch):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)

    def explode(*args, **kwargs):
        raise RuntimeError("falha inesperada")

    out = _execute(client, store, proposal["id"],
                   Approver(during=lambda: monkeypatch.setattr(limites, "account_limits", explode)))
    assert out["status"] == "abortada" and "falha inesperada" in out["envio"] and fake.sent == []


def test_prices_are_rounded_to_the_tick_size():
    assert execucao._to_tick(1.23456, 0.0005, 5) == pytest.approx(1.2345)
    assert execucao._to_tick(30000.13, 0.25, 2) == pytest.approx(30000.25)
    assert execucao._to_tick(30000.10, 0.25, 2) == pytest.approx(30000.0)


def test_single_attempt_holds_even_if_the_first_check_misses_it(tmp_path):
    client, fake, store = _setup(tmp_path)
    proposal = _propose(client, store)
    _execute(client, store, proposal["id"], Approver("recusada"))
    store.execution = lambda proposal_id: None  # type: ignore[method-assign]  # como numa corrida
    with pytest.raises(ExecucaoError, match="já teve uma tentativa"):
        _execute(client, store, proposal["id"], Approver())
    assert fake.sent == []
