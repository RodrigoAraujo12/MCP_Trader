"""Journal: contexto SMC da entrada medido na sincronização e usado nas estatísticas (contexto simulado)."""

from __future__ import annotations

from datetime import datetime, timezone

import fake_mt5 as fm
from fake_mt5 import FakeMT5, make_deal, make_order, make_symbol
from test_journal import DEALS, EUR, HISTORY_ORDERS, IN, OPEN, OUT, at
from test_mt5_client import make_client
from trading_mcp.journal import Journal, JournalError
from trading_mcp.mt5_client import MT5Error


class StubContext:
    """Contexto fixo por direção; ``fail`` = símbolos que falham; ``cost`` = tempo gasto por chamada."""

    def __init__(self, fail: dict[str, Exception] | None = None, cost: float = 0.0, clock: list[float] | None = None):
        self.calls: list[tuple[str, str, datetime, float]] = []
        self.fail = fail or {}
        self.cost = cost
        self.clock = clock if clock is not None else [0.0]

    def __call__(self, symbol: str, direction: str, when: datetime, price: float) -> dict:
        self.calls.append((symbol, direction, when, price))
        self.clock[0] += self.cost
        if symbol in self.fail:
            raise self.fail[symbol]
        return {
            "versao": 1, "sessao": "londres",
            "estrutura": {"M1": {"micro": "alta"}, "M3": {"micro": "alta"}, "M5": {"micro": "alta", "macro": "alta"},
                          "H1": {"micro": "baixa", "macro": "alta"}, "M15": {"micro": None}},
            "choch": [], "varreduras": [{"tipo": "nivel_chave", "lado": "abaixo", "a_favor": direction == "compra"}],
            "zonas": [], "premium_discount": {}, "faltando": [],
        }


def _journal(tmp_path, stub=None, *, deals=DEALS, orders=HISTORY_ORDERS, positions=OPEN, version=1, budget=20.0):
    stub = stub or StubContext()
    fake = FakeMT5([make_symbol(EUR)], positions=list(positions), deals=list(deals), history_orders=list(orders))
    client, fake = make_client(fake)
    journal = Journal(tmp_path / "j" / "journal.sqlite3", tmp_path / "exp", client, None, entry_context=stub,
                      context_version=version, monotonic=lambda: stub.clock[0], context_budget_s=budget)
    return journal, stub


def _ops(journal: Journal, **kwargs) -> dict[int, dict]:
    return {o["ticket"]: o for o in journal.list_operations(days=None, limit=200, **kwargs)["operacoes"]}


def test_sync_measures_context_once_newest_first(tmp_path):
    journal, stub = _journal(tmp_path)
    out = journal.sync(7)
    assert out["contexto_entrada"] == {"medidos_agora": 3, "faltando": 0}
    assert [c[1] for c in stub.calls] == ["compra", "compra", "venda"]  # 300 (mais nova), 100, 200
    assert [c[2] for c in stub.calls] == [datetime.fromtimestamp(at(h), timezone.utc) for h in (0.5, 2, 3)]
    assert [c[3] for c in stub.calls] == [1.09900, 1.10000, 1.10000]
    ops = _ops(journal)
    assert ops[100]["contexto"]["estrutura_M5"] == "a_favor" and ops[200]["contexto"]["estrutura_M5"] == "contra"
    assert ops[100]["contexto"]["varredura_a_favor"] == "nivel_chave"
    assert ops[200]["contexto"]["varredura_a_favor"] == "nenhuma"  # varredura abaixo não favorece a venda
    assert "contexto_detalhado" not in ops[100]
    assert _ops(journal, detailed_context=True)[100]["contexto_detalhado"]["sessao"] == "londres"
    assert journal.sync(7)["contexto_entrada"] == {"medidos_agora": 0, "faltando": 0}
    assert len(stub.calls) == 3  # já medido: não mede de novo


def test_new_context_version_measures_again(tmp_path):
    journal, stub = _journal(tmp_path)
    journal.sync(7)
    newer, newer_stub = _journal(tmp_path, version=2)
    assert newer.sync(7)["contexto_entrada"]["medidos_agora"] == 3
    assert len(newer_stub.calls) == 3


def test_failure_is_warned_and_retried(tmp_path):
    stub = StubContext(fail={EUR: MT5Error("Terminal sem conexão com a corretora: contexto da entrada adiado.")})
    journal, _ = _journal(tmp_path, stub)
    out = journal.sync(7)
    assert out["contexto_entrada"] == {"medidos_agora": 0, "faltando": 3}
    assert any("Contexto da entrada não medido em 3" in w and "sem conexão" in w for w in out["avisos"])
    assert _ops(journal)[100]["contexto"] is None
    stub.fail.clear()
    assert journal.sync(7)["contexto_entrada"] == {"medidos_agora": 3, "faltando": 0}


def test_unexpected_error_does_not_break_the_sync(tmp_path):
    journal, _ = _journal(tmp_path, StubContext(fail={EUR: KeyError("estrutura")}))
    out = journal.sync(7)
    assert out["operacoes_novas"] == 3  # os fatos foram gravados
    assert any("erro inesperado (KeyError" in w for w in out["avisos"])


def test_time_budget_leaves_the_rest_for_the_next_sync(tmp_path):
    clock = [0.0]
    journal, stub = _journal(tmp_path, StubContext(cost=15.0, clock=clock), budget=20.0)
    out = journal.sync(7)
    assert out["contexto_entrada"] == {"medidos_agora": 2, "faltando": 1}
    assert any("Faltou tempo para medir o contexto da entrada de 1" in w for w in out["avisos"])
    assert journal.sync(7)["contexto_entrada"] == {"medidos_agora": 1, "faltando": 0}
    assert [c[1] for c in stub.calls] == ["compra", "compra", "venda"]


def test_budget_counts_from_the_start_of_the_sync(tmp_path):
    clock = [0.0]
    journal, stub = _journal(tmp_path, StubContext(clock=clock), budget=20.0)
    original = journal._mt5.account

    def slow_account():
        clock[0] += 25.0  # a importação dos fatos já gastou o tempo
        return original()

    journal._mt5.account = slow_account
    out = journal.sync(7)
    assert out["contexto_entrada"] == {"medidos_agora": 0, "faltando": 3} and stub.calls == []


def test_context_write_failure_keeps_the_sync(tmp_path, monkeypatch):
    journal, _ = _journal(tmp_path, StubContext(fail={EUR: MT5Error("x")}))
    journal.sync(7)  # fatos gravados; contexto falhou
    original = Journal._db
    writes = []

    def flaky(self, *, write):
        if write:
            writes.append(1)
            if len(writes) == 2:  # a segunda gravação é a do contexto
                raise JournalError("database is locked")
        return original(self, write=write)

    monkeypatch.setattr(Journal, "_db", flaky)
    journal._entry_context.fail.clear()
    out = journal.sync(7)
    assert out["contexto_entrada"] == {"medidos_agora": 0, "faltando": 3}
    assert any("medido mas não gravado (database is locked)" in w for w in out["avisos"])
    monkeypatch.setattr(Journal, "_db", original)
    assert journal.sync(7)["contexto_entrada"] == {"medidos_agora": 3, "faltando": 0}


def test_context_uses_the_first_entry_price(tmp_path):
    deals = [
        make_deal(41, 400, EUR, "buy", IN, 0.1, 1.10000, at(3), order=400),
        make_deal(42, 400, EUR, "buy", IN, 0.1, 1.09800, at(2.5), order=402),
        make_deal(43, 400, EUR, "sell", OUT, 0.2, 1.10100, at(2), order=403, reason=fm.DEAL_REASON_TP, profit=30.0),
    ]
    orders = [make_order(400, EUR, fm.ORDER_TYPE_BUY, 0.1, 1.1, 1.1, sl=1.095, position_id=400)]
    journal, stub = _journal(tmp_path, deals=deals, orders=orders, positions=[])
    journal.sync(7)
    assert stub.calls == [(EUR, "compra", datetime.fromtimestamp(at(3), timezone.utc), 1.10000)]
    assert _ops(journal)[400]["preco_entrada"] == 1.099  # média das entradas continua nos fatos


def test_stats_by_context_and_by_tag(tmp_path):
    journal, _ = _journal(tmp_path)
    journal.sync(7)
    journal.annotate(ticket=100, tags="sweep, ob m5")
    journal.annotate(ticket=200, tags="OB M5")
    stats = journal.stats()
    assert stats["operacoes_sem_contexto"] == 0
    by_trend = {g["grupo"]: g for g in stats["por_contexto"]["estrutura_M5"]}
    assert by_trend["a_favor"]["operacoes"] == 1 and by_trend["contra"]["operacoes"] == 1
    assert {g["grupo"] for g in stats["por_contexto"]["estrutura_M15"]} == {"sem_dados"}
    by_tag = {g["grupo"]: g["operacoes"] for g in stats["por_tag"]}
    assert by_tag == {"ob m5": 2, "sweep": 1}
    assert any("por_contexto" in n for n in stats["observacoes"])


def test_without_context_function_nothing_changes(tmp_path):
    fake = FakeMT5([make_symbol(EUR)], positions=list(OPEN), deals=list(DEALS), history_orders=list(HISTORY_ORDERS))
    client, _ = make_client(fake)
    journal = Journal(tmp_path / "journal.sqlite3", tmp_path / "exp", client)
    out = journal.sync(7)
    assert "contexto_entrada" not in out
    assert all(o["contexto"] is None for o in journal.list_operations(days=None)["operacoes"])
    assert journal.stats()["operacoes_sem_contexto"] == 2
