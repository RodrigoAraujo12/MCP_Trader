"""Journal local (SQLite) com o MT5 simulado."""

from __future__ import annotations

import csv
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import fake_mt5 as fm
from fake_mt5 import FakeMT5, make_balance_deal, make_deal, make_order, make_position, make_symbol
from test_mt5_client import make_client
from trading_mcp.calendario import CalendarError
from trading_mcp.journal import Journal, JournalError
from trading_mcp.mt5_client import MT5Error

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)  # relógio do FakeClock
EUR = "EURUSD"  # tick de 0,00001 vale 1 USD por lote


def at(hours_ago: float) -> int:
    return int((NOW - timedelta(hours=hours_ago)).timestamp())


IN, OUT = fm.DEAL_ENTRY_IN, fm.DEAL_ENTRY_OUT

# 100: compra 0,1 a 1,10000, saiu no alvo a 1,10100 (+10, comissão 0,35 por lado). Stop na ordem.
# 200: venda 0,2 a 1,10000, saiu no stop a 1,10250 (-50). Ordem sem stop.
# 300: compra 0,1 a 1,09900 ainda aberta, stop 1,09700 só na posição.
DEALS = [
    make_balance_deal(1, 400.0, at(48)),
    make_deal(11, 100, EUR, "buy", IN, 0.1, 1.10000, at(2), order=100, commission=-0.35),
    make_deal(12, 100, EUR, "sell", OUT, 0.1, 1.10100, at(1), order=101, reason=fm.DEAL_REASON_TP, profit=10.0,
              commission=-0.35),
    make_deal(21, 200, EUR, "sell", IN, 0.2, 1.10000, at(3), order=200),
    make_deal(22, 200, EUR, "buy", OUT, 0.2, 1.10250, at(2.5), order=201, reason=fm.DEAL_REASON_SL, profit=-50.0),
    make_deal(31, 300, EUR, "buy", IN, 0.1, 1.09900, at(0.5), order=300),
]
HISTORY_ORDERS = [
    make_order(100, EUR, fm.ORDER_TYPE_BUY, 0.1, 1.09998, 1.1, sl=1.09500, tp=1.10100, position_id=100),
    make_order(200, EUR, fm.ORDER_TYPE_SELL, 0.2, 1.10000, 1.1, position_id=200),
    make_order(300, EUR, fm.ORDER_TYPE_BUY, 0.1, 1.09900, 1.1, position_id=300),
]
OPEN = [make_position(300, EUR, "buy", 0.1, 1.09900, 1.09950, sl=1.09700, tp=1.10500, profit=5.0)]


class StubCalendar:
    """events_between com eventos fixos; ``covered_from`` define até onde o arquivo cobre."""

    def __init__(self, events=(), covered_from: datetime | None = None, error: Exception | None = None):
        self.events = list(events)
        self.covered_from = covered_from or NOW - timedelta(days=7)
        self.error = error
        self.calls = []

    def events_between(self, start, end, min_importance="alta"):
        self.calls.append((start, end, min_importance))
        if self.error:
            raise self.error
        inside = [e for e in self.events if start <= e[0] <= end]
        return [{"utc": e[0].strftime("%Y-%m-%dT%H:%M:%SZ"), "codigo": e[1]} for e in inside], start >= self.covered_from


def _journal(tmp_path: Path, *, deals=DEALS, orders=HISTORY_ORDERS, positions=OPEN, calendar=None, **fake_kwargs):
    fake = FakeMT5([make_symbol(EUR)], positions=list(positions), deals=list(deals), history_orders=list(orders),
                   **fake_kwargs)
    client, fake = make_client(fake)
    journal = Journal(tmp_path / "j" / "journal.sqlite3", tmp_path / "exp", client, calendar)
    return journal, client, fake


def _ops(journal: Journal) -> dict[int, dict]:
    return {o["ticket"]: o for o in journal.list_operations(days=None, limit=200)["operacoes"]}


def test_sync_imports_positions_from_deals(tmp_path):
    journal, _, _ = _journal(tmp_path)
    out = journal.sync(7)
    assert out["operacoes_novas"] == 3 and out["operacoes_atualizadas"] == 0
    assert out["negocios_no_periodo"] == 5  # o depósito não conta
    assert [m["ticket"] for m in out["sem_stop_inicial"]] == [200]
    ops = _ops(journal)
    win = ops[100]
    assert win["status"] == "fechada" and win["direcao"] == "compra" and win["origem"] == "terminal"
    assert win["preco_entrada"] == 1.1 and win["preco_saida"] == 1.101  # preços dos negócios, não da ordem
    assert win["resultado_bruto"] == 10.0 and win["custos"]["comissao"] == -0.7
    assert win["resultado_liquido"] == 9.3 and win["fechamento_motivo"] == "alvo"
    assert win["stop_inicial"] == 1.095 and win["stop_inicial_fonte"] == "ordem_de_abertura"
    assert win["alvo_inicial"] == 1.101
    assert win["risco_inicial"] == 50.0 and win["r"] == 0.19
    assert win["duracao"] == "1 h 00 min" and win["abertura"]["utc"] == "2026-09-30T10:00:00Z"
    loss = ops[200]
    assert loss["direcao"] == "venda" and loss["resultado_liquido"] == -50.0 and loss["fechamento_motivo"] == "stop"
    assert loss["stop_inicial"] is None and loss["risco_inicial"] is None and loss["r"] is None
    live = ops[300]
    assert live["status"] == "aberta" and live["fechamento"] is None and live["r"] is None
    assert live["stop_inicial"] == 1.097 and live["stop_inicial_fonte"] == "observado"
    assert live["risco_inicial"] == 20.0 and "pode já ter sido movido" in live["nota_stop"]


def test_sync_is_idempotent_and_keeps_annotations(tmp_path):
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    journal.annotate(ticket=100, setup="OB + FVG", reason="varredura de liquidez", tags="SMC, Londres")
    again = journal.sync(7)
    assert again["operacoes_novas"] == 0 and again["operacoes_atualizadas"] == 3
    ops = _ops(journal)
    assert len(ops) == 3
    assert ops[100]["setup"] == "OB + FVG" and ops[100]["tags"] == "londres, smc"
    with sqlite3.connect(tmp_path / "j" / "journal.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM negocios").fetchone()[0] == 5


def test_position_opened_before_window_keeps_its_entry(tmp_path):
    deals = [
        make_deal(41, 400, EUR, "buy", IN, 0.1, 1.08000, at(24 * 10), order=400),
        make_deal(42, 400, EUR, "sell", OUT, 0.1, 1.08500, at(1), order=401, profit=50.0),
    ]
    journal, _, _ = _journal(tmp_path, deals=deals, orders=[], positions=[])
    journal.sync(2)
    op = _ops(journal)[400]
    assert op["abertura"]["utc"] == "2026-09-20T12:00:00Z" and op["preco_entrada"] == 1.08


def test_partial_close_and_scale_in(tmp_path):
    deals = [
        make_deal(51, 500, EUR, "buy", IN, 0.1, 1.10000, at(3), order=500),
        make_deal(52, 500, EUR, "buy", IN, 0.1, 1.10200, at(2), order=501),
        make_deal(53, 500, EUR, "sell", OUT, 0.1, 1.10300, at(1), order=502, profit=20.0),
    ]
    orders = [make_order(500, EUR, fm.ORDER_TYPE_BUY, 0.1, 1.1, 1.1, sl=1.09900, position_id=500)]
    journal, _, _ = _journal(tmp_path, deals=deals, orders=orders, positions=[])
    journal.sync(7)
    op = _ops(journal)[500]
    assert op["status"] == "parcial" and op["volume"] == 0.2 and op["volume_fechado"] == 0.1
    assert op["preco_entrada"] == 1.101 and op["entradas"] == 2 and op["saidas"] == 1
    assert op["risco_inicial"] == 40.0 and "Várias entradas" in op["nota_risco"]  # 200 pontos x 0,2
    stats = journal.stats()
    assert stats["geral"]["operacoes"] == 0 and any("1 operação" in n for n in stats["observacoes"])


def test_annotate_initial_stop_gives_r_and_survives_sync(tmp_path):
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    op = journal.annotate(ticket=200, initial_stop=1.10250)
    assert op["stop_inicial_fonte"] == "informado" and op["risco_inicial"] == 50.0 and op["r"] == -1.0
    journal.sync(7)
    assert _ops(journal)[200]["stop_inicial"] == 1.1025 and _ops(journal)[200]["r"] == -1.0


def test_annotate_rejects_stop_on_wrong_side_and_bad_input(tmp_path):
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    with pytest.raises(ValueError, match="lado da perda"):
        journal.annotate(ticket=200, initial_stop=1.09)  # venda com stop abaixo da entrada
    with pytest.raises(ValueError, match="exatamente um"):
        journal.annotate(setup="x")
    with pytest.raises(ValueError, match="Nada para anotar"):
        journal.annotate(ticket=100)
    with pytest.raises(ValueError, match="não encontrada"):
        journal.annotate(ticket=999, setup="x")


def test_annotate_appends_observations_and_overrides_news(tmp_path):
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    ident = _ops(journal)[100]["id"]
    journal.annotate(operation_id=ident, note="entrei cedo")
    op = journal.annotate(operation_id=ident, note="saída no alvo", news="sim")
    lines = op["observacoes"].splitlines()
    assert len(lines) == 2 and lines[0].endswith("SP] entrei cedo") and lines[0].startswith("[2026-09-30 09:00")
    assert op["noticia"] == "sim"
    journal.sync(7)  # a marcação informada não é recalculada
    assert _ops(journal)[100]["noticia"] == "sim"


def test_news_tagging_from_calendar(tmp_path):
    # NFP 15 min antes da entrada da 100; nada perto da 200; a 300 aberta pega evento depois da entrada.
    cal = StubCalendar(events=[(NOW - timedelta(hours=2, minutes=15), "nonfarm-payrolls"),
                               (NOW - timedelta(minutes=10), "initial-jobless-claims")])
    journal, _, _ = _journal(tmp_path, calendar=cal)
    journal.sync(7)
    ops = _ops(journal)
    assert ops[100]["noticia"] == "sim" and ops[100]["noticias"][0]["codigo"] == "nonfarm-payrolls"
    assert ops[200]["noticia"] == "nao" and ops[200]["noticias"] == []
    assert ops[300]["noticia"] == "sim"
    start, end, level = cal.calls[0]
    assert level == "alta" and end - start == timedelta(minutes=90)  # 30 min antes da entrada até o fechamento


def test_news_unknown_when_calendar_does_not_cover_and_kept_after(tmp_path):
    cal = StubCalendar(covered_from=NOW - timedelta(hours=2, minutes=40))  # cobre a 100, não a 200
    journal, _, _ = _journal(tmp_path, calendar=cal)
    journal.sync(7)
    assert _ops(journal)[100]["noticia"] == "nao" and _ops(journal)[200]["noticia"] == "desconhecido"
    cal.covered_from = NOW  # o arquivo andou: a 100 saiu da janela, mas a marcação fica
    journal.sync(7)
    assert _ops(journal)[100]["noticia"] == "nao"


def test_calendar_error_is_a_warning(tmp_path):
    journal, _, _ = _journal(tmp_path, calendar=StubCalendar(error=CalendarError("arquivo não encontrado")))
    out = journal.sync(7)
    assert sum("Calendário indisponível" in w for w in out["avisos"]) == 1
    assert _ops(journal)[100]["noticia"] == "desconhecido"


def test_stats_are_deterministic_with_samples_and_gaps(tmp_path):
    journal, _, _ = _journal(tmp_path, calendar=StubCalendar(events=[(NOW - timedelta(hours=2, minutes=15), "nfp")]))
    journal.sync(7)
    journal.annotate(ticket=100, setup="OB")
    s = journal.stats()
    g = s["geral"]
    assert g["operacoes"] == 2 and g["ganhos"] == 1 and g["perdas"] == 1 and g["empates"] == 0
    assert g["taxa_acerto_pct"] == 50.0 and g["resultado_liquido"] == -40.7 and g["resultado_medio"] == -20.35
    assert g["ganho_medio"] == 9.3 and g["perda_media"] == -50.0 and g["fator_lucro"] == 0.19
    assert g["r"] == {"operacoes_com_r": 1, "sem_risco_inicial": 1, "r_total": 0.19, "r_medio": 0.19,
                      "por_fonte_do_stop": {"ordem_de_abertura": 1}}
    assert g["custos"]["comissao"] == -0.7 and g["amostra_pequena"] is True
    setups = {x["grupo"]: x for x in s["por_setup"]}
    assert setups["OB"]["operacoes"] == 1 and setups["sem setup"]["resultado_liquido"] == -50.0
    news = {x["grupo"]: x["operacoes"] for x in s["por_noticia"]}
    assert news == {"sim": 1, "nao": 1}
    assert any("1 operação(ões) aberta" in n for n in s["observacoes"])
    assert journal.stats(setup="ob")["geral"]["operacoes"] == 1
    assert journal.stats(days=0.1)["geral"]["operacoes"] == 1  # 2,4 h: só a 100 (fechou há 1 h)


def test_list_filters_and_known_setups(tmp_path):
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    journal.annotate(ticket=200, setup="CHoCH")
    out = journal.list_operations(days=30, status="fechada")
    assert [o["ticket"] for o in out["operacoes"]] == [100, 200]  # mais recente primeiro
    assert out["setups_existentes"] == ["CHoCH"] and out["ultima_sincronizacao"]["utc"] == "2026-09-30T12:00:00Z"
    assert journal.list_operations(symbol="eur", status="aberta")["total"] == 1
    assert journal.list_operations(setup="choch")["total"] == 1


def test_empty_journal_asks_for_sync(tmp_path):
    journal, _, _ = _journal(tmp_path)
    with pytest.raises(JournalError, match="journal_sincronizar"):
        journal.stats()


def test_export_writes_backup_and_excel_friendly_csv(tmp_path):
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    out = journal.export()
    assert out["operacoes"] == 3
    with sqlite3.connect(out["copia_do_banco"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM operacoes").fetchone()[0] == 3
    raw = Path(out["csv"]).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM: o Excel reconhece UTF-8
    rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines(), delimiter=";"))
    assert rows[0]["simbolo"] == EUR and "," in rows[0]["preco_entrada"]


def test_sync_warns_on_real_account_and_survives_order_history_failure(tmp_path, monkeypatch):
    journal, client, _ = _journal(tmp_path, trade_mode=fm.ACCOUNT_TRADE_MODE_REAL)

    def fail(position):
        raise MT5Error("history_orders_get falhou")

    monkeypatch.setattr(client, "position_orders", fail)
    out = journal.sync(7)
    assert "NÃO é demo" in out["avisos"][0]
    assert sum("Ordens da posição" in w for w in out["avisos"]) == 3
    assert _ops(journal)[300]["stop_inicial_fonte"] == "observado"  # a posição aberta ainda dá o stop


def test_newer_schema_is_refused(tmp_path):
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    with sqlite3.connect(tmp_path / "j" / "journal.sqlite3") as conn:
        conn.execute("UPDATE meta SET valor = '99' WHERE chave = 'schema'")
    with pytest.raises(JournalError, match="versão mais nova"):
        journal.list_operations()


def test_sync_rejects_bad_days(tmp_path):
    journal, _, _ = _journal(tmp_path)
    with pytest.raises(ValueError):
        journal.sync(0)


def test_client_deals_require_timezone_and_map_fields(tmp_path):
    _, client, _ = _journal(tmp_path)
    with pytest.raises(ValueError, match="fuso"):
        client.deals(datetime(2026, 9, 30), datetime(2026, 10, 1))
    deals = client.deals(NOW - timedelta(days=3), NOW)
    assert [d["ticket"] for d in deals] == [1, 21, 22, 11, 12, 31]  # do mais antigo ao mais novo
    assert deals[0]["tipo"] == "outro"  # depósito
    tp_exit = next(d for d in deals if d["ticket"] == 12)
    assert tp_exit["entrada"] == "out" and tp_exit["motivo"] == "alvo" and tp_exit["tipo"] == "sell"
    assert client.position_orders(100)[0]["stop_loss"] == 1.095


# ---------------------------------------------------------------- correções da auditoria
def _run_with_timeout(func, seconds=15):
    """Roda ``func`` numa thread; falha se travar (o export travava num journal novo)."""
    box = {}

    def target():
        try:
            box["value"] = func()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "travou"
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_export_right_after_first_sync_does_not_hang(tmp_path):
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    out = _run_with_timeout(journal.export)
    assert out["operacoes"] == 3
    assert _run_with_timeout(lambda: journal.list_operations(days=None))["total"] == 3  # banco não ficou travado


def test_read_tools_never_create_the_database(tmp_path):
    journal, _, _ = _journal(tmp_path)
    for call in (journal.list_operations, journal.stats, journal.export, lambda: journal.annotate(ticket=1, setup="x")):
        with pytest.raises(JournalError, match="journal_sincronizar"):
            call()
    assert not (tmp_path / "j").exists()


def test_informed_stop_beats_opening_order(tmp_path):
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    journal.annotate(ticket=100, initial_stop=1.094)
    journal.sync(7)
    op = _ops(journal)[100]
    assert op["stop_inicial"] == 1.094 and op["stop_inicial_fonte"] == "informado" and op["risco_inicial"] == 60.0


def test_annotation_during_sync_is_not_overwritten(tmp_path):
    journal_box = {}

    class AnnotatingCalendar(StubCalendar):
        def events_between(self, start, end, min_importance="alta"):
            if not self.calls:  # o usuário anota enquanto a sincronização calcula
                journal_box["j"].annotate(ticket=100, initial_stop=1.094, news="sim")
            return super().events_between(start, end, min_importance)

    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    journal._calendar = AnnotatingCalendar()
    journal_box["j"] = journal
    journal.sync(7)
    op = _ops(journal)[100]
    assert op["stop_inicial"] == 1.094 and op["stop_inicial_fonte"] == "informado" and op["noticia"] == "sim"


def test_mt5_failure_on_resync_keeps_risk(tmp_path, monkeypatch):
    journal, client, _ = _journal(tmp_path)
    journal.sync(7)

    def fail(*args, **kwargs):
        raise MT5Error("order_calc_profit falhou")

    monkeypatch.setattr(client, "profit", fail)
    out = journal.sync(7)
    ops = _ops(journal)
    assert ops[300]["risco_inicial"] == 20.0  # aberta: recalcularia, mas o MT5 falhou -> mantém
    assert ops[100]["risco_inicial"] == 50.0  # fechada: congelada, nem tenta
    assert any("mantido o valor anterior" in w for w in out["avisos"])


def _close_300(fake, client, minutes_after=30):
    client.clock.now = NOW + timedelta(hours=1)
    fake.positions = []
    fake.deals.append(make_deal(32, 300, EUR, "sell", OUT, 0.1, 1.10100, int((NOW + timedelta(minutes=minutes_after)).timestamp()),
                                order=301, profit=20.0))


def test_no_news_while_open_becomes_unknown_if_rest_was_not_checked(tmp_path):
    cal = StubCalendar()
    journal, client, fake = _journal(tmp_path, calendar=cal)
    journal.sync(7)
    assert _ops(journal)[300]["noticia"] == "nao" and "noticia_verificada_ate" in _ops(journal)[300]
    _close_300(fake, client)
    cal.covered_from = NOW + timedelta(days=1)  # o arquivo não cobre mais a operação
    journal.sync(7)
    assert _ops(journal)[300]["noticia"] == "desconhecido"


def test_event_found_counts_even_when_file_does_not_cover_everything(tmp_path):
    cal = StubCalendar()
    journal, client, fake = _journal(tmp_path, calendar=cal)
    journal.sync(7)
    _close_300(fake, client)
    cal.covered_from = NOW + timedelta(days=1)
    cal.events = [(NOW + timedelta(minutes=15), "fomc")]
    journal.sync(7)
    assert _ops(journal)[300]["noticia"] == "sim"


def test_observed_stop_is_flagged_in_stats_and_open_result_is_partial(tmp_path):
    journal, client, fake = _journal(tmp_path)
    out = journal.sync(7)
    assert any("'observado'" in w for w in out["avisos"])
    live = _ops(journal)[300]
    assert "resultado_liquido" not in live and live["resultado_realizado"] == 0.0 and "flutuante" in live["nota_resultado"]
    _close_300(fake, client)
    journal.sync(7)
    s = journal.stats()
    assert s["geral"]["r"]["por_fonte_do_stop"] == {"ordem_de_abertura": 1, "observado": 1}
    assert any("stop 'observado'" in n for n in s["observacoes"])
    assert _ops(journal)[300]["resultado_liquido"] == 20.0


def test_setup_spelling_is_reused(tmp_path):
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    journal.annotate(ticket=100, setup="OB")
    assert journal.annotate(ticket=200, setup="ob")["setup"] == "OB"
    assert [g["grupo"] for g in journal.stats()["por_setup"]] == ["OB"]


def test_position_without_entry_deal_is_skipped(tmp_path):
    deals = [make_deal(61, 600, EUR, "sell", OUT, 0.1, 1.1, at(1), order=601, profit=5.0)]
    journal, _, _ = _journal(tmp_path, deals=deals, orders=[], positions=[])
    out = journal.sync(7)
    assert out["operacoes_novas"] == 0 and any("não tem o negócio de entrada" in w for w in out["avisos"])


def test_service_deals_do_not_count_as_entries(tmp_path):
    deals = [
        make_deal(71, 700, EUR, "buy", IN, 0.1, 1.1, at(30), order=700),
        make_deal(72, 700, EUR, "sell", OUT, 0.1, 1.1005, at(20), order=0, reason=fm.DEAL_REASON_ROLLOVER, swap=-1.0),
        make_deal(73, 700, EUR, "buy", IN, 0.1, 1.1005, at(20), order=0, reason=fm.DEAL_REASON_ROLLOVER),
        make_deal(74, 700, EUR, "sell", OUT, 0.1, 1.101, at(1), order=701, profit=10.0),
    ]
    journal, _, _ = _journal(tmp_path, deals=deals, orders=[], positions=[])
    journal.sync(7)
    op = _ops(journal)[700]
    assert op["volume"] == 0.1 and op["entradas"] == 1 and op["status"] == "fechada"
    assert op["resultado_liquido"] == 9.0  # o swap da rolagem entra no resultado


def test_stats_note_about_open_trades_respects_filters(tmp_path):
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    assert not any("ficaram de fora" in n for n in journal.stats(symbol="XAU")["observacoes"])


def test_missing_stop_warning_counts_all(tmp_path):
    deals = []
    for i in range(25):
        pid = 1000 + i
        deals += [make_deal(pid * 10, pid, EUR, "buy", IN, 0.1, 1.1, at(5), order=pid),
                  make_deal(pid * 10 + 1, pid, EUR, "sell", OUT, 0.1, 1.1, at(4), order=pid + 5000)]
    journal, _, _ = _journal(tmp_path, deals=deals, orders=[], positions=[])
    out = journal.sync(7)
    assert len(out["sem_stop_inicial"]) == 20
    assert any(w.startswith("25 operação(ões) sem stop inicial conhecido (mostrando 20)") for w in out["avisos"])


def test_version_1_database_is_migrated(tmp_path):
    path = tmp_path / "j" / "journal.sqlite3"
    path.parent.mkdir()
    from trading_mcp import journal as module

    old_schema = module._SCHEMA
    for name, kind in module._ADDED_COLUMNS.items():
        old_schema = old_schema.replace(f"    {name} {kind},\n", "")
        assert name not in old_schema
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        conn.execute("INSERT INTO meta VALUES ('schema', '1')")
    journal, _, _ = _journal(tmp_path)
    journal.sync(7)
    with sqlite3.connect(path) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(operacoes)")}
        assert set(module._ADDED_COLUMNS) <= columns
        assert conn.execute("SELECT valor FROM meta WHERE chave = 'schema'").fetchone()[0] == str(module.SCHEMA_VERSION)


def test_newer_schema_is_not_touched(tmp_path):
    path = tmp_path / "j" / "journal.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema', '9')")
    journal, _, _ = _journal(tmp_path)
    with pytest.raises(JournalError, match="versão mais nova"):
        journal.sync(7)
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"meta"}  # o servidor antigo não criou tabelas no banco novo
