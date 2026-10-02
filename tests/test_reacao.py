"""Reação a eventos e contexto entre ativos (MT5 simulado)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import fake_mt5 as fm
from fake_mt5 import FakeMT5, make_symbol, make_ticks
from test_calendario import ev, val, write
from test_mt5_client import make_client
from trading_mcp import reacao
from trading_mcp.calendario import EconomicCalendar

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # relógio do FakeClock
EVENT = datetime(2026, 9, 30, 11, 30, tzinfo=UTC)
INDEX = make_symbol(
    "USTECm", path="Indices\\USTECm", digits=2, point=0.01, bid=30000.0, ask=30001.12, trade_tick_size=0.01,
    trade_contract_size=1.0, currency_base="USD", currency_profit="USD", currency_margin="USD",
    trade_calc_mode=fm.SYMBOL_CALC_MODE_CFD,
)
GOLD = make_symbol("XAUUSDm", digits=3, point=0.001, bid=4180.0, ask=4180.24, trade_tick_size=0.001,
                   currency_base="XAU", currency_profit="USD")


def bars(start: datetime, closes: list[float], *, highs: dict[int, float] | None = None,
         lows: dict[int, float] | None = None, skip: set[int] = frozenset()) -> np.ndarray:
    """Candles M1 a partir de ``start``; ``highs``/``lows`` sobrescrevem por índice; ``skip`` remove minutos."""
    rows, prev = [], closes[0]
    for i, close in enumerate(closes):
        if i not in skip:
            t = int((start + timedelta(minutes=i)).timestamp())
            high = (highs or {}).get(i, max(prev, close))
            low = (lows or {}).get(i, min(prev, close))
            rows.append((t, prev, high, low, close, 10, 12, 0))
        prev = close
    return np.array(rows, dtype=fm.RATES_DTYPE)


def hour_bars(start: datetime, closes: list[float]) -> np.ndarray:
    rows = [(int((start + timedelta(hours=i)).timestamp()), c, c, c, c, 10, 12, 0) for i, c in enumerate(closes)]
    return np.array(rows, dtype=fm.RATES_DTYPE)


def eurusd_bars(highs: dict[int, float] | None = None, **kwargs) -> np.ndarray:
    # 10:30–11:59: 1,10000 até 11:29; 11:30 → 1,10010; 11:31–33 → 1,10020; 11:34 → 1,10050;
    # 11:35–11:44 → 1,10030 (máxima de 1,10090 às 11:40); depois 1,10000.
    closes = [1.1] * 60 + [1.1001] + [1.1002] * 3 + [1.1005] + [1.1003] * 10 + [1.1] * 15
    return bars(EVENT - timedelta(minutes=60), closes, highs={70: 1.1009, **(highs or {})}, **kwargs)


def _client(rates=None, symbols=None, ticks=None, **fake_kwargs):
    fake = FakeMT5(symbols or [make_symbol("EURUSD"), INDEX, GOLD],
                   rates=rates if rates is not None else {"EURUSD": eurusd_bars()},
                   ticks=ticks, **fake_kwargs)
    client, fake = make_client(fake)
    return client, fake


def _measure(out: dict, symbol: str) -> dict:
    return next(m for m in out["movimento_medido"] if m["simbolo"] == symbol)


def test_reaction_windows_extremes_and_pips():
    client, _ = _client()
    out = reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"])
    m = _measure(out, "EURUSD")
    assert m["referencia"] == {"preco": 1.1, "ate": "2026-09-30T11:30:00Z"}
    w = {x["minutos"]: x for x in m["janelas"]}
    assert w[1]["preco"] == 1.1001 and w[1]["pips"] == 1.0 and w[1]["pontos"] == 10 and w[1]["variacao_pct"] == 0.009
    assert w[5]["preco"] == 1.1005 and w[5]["pips"] == 5.0
    assert w[15]["preco"] == 1.1003 and w[15]["pips"] == 3.0 and "ate" not in w[15]
    assert m["extremos"]["maxima"]["pips"] == 9.0 and m["extremos"]["maxima"]["no_minuto"] == "2026-09-30T11:40:00Z"
    assert m["extremos"]["minima"]["variacao"] == 0.0
    assert m["extremos"]["de"] == "2026-09-30T11:30:00Z" and m["extremos"]["ate"] == "2026-09-30T11:45:00Z"
    assert "parcial" not in m["extremos"]
    assert out["horario_evento"]["sao_paulo"] == "2026-09-30 08:30:00"
    assert any("Calendário não configurado" in n for n in out["observacoes"])


def test_index_and_gold_have_no_pips():
    closes = [30000.0] * 60 + [30010.0] * 30
    client, _ = _client(rates={"USTECm": bars(EVENT - timedelta(minutes=60), closes),
                               "XAUUSDm": bars(EVENT - timedelta(minutes=60), [4180.0] * 60 + [4182.5] * 30)})
    out = reacao.reaction(client, None, when="2026-09-30T11:30Z", symbols=["USTEC", "XAUUSD"])
    index, gold = _measure(out, "USTECm"), _measure(out, "XAUUSDm")
    assert index["janelas"][0]["variacao"] == 10.0 and "pips" not in index["janelas"][0]
    assert gold["janelas"][0]["variacao_pct"] == 0.06 and "pips" not in gold["janelas"][0]


def test_windows_after_now_are_pending():
    client, _ = _client()
    out = reacao.reaction(client, None, when="2026-09-30T11:57:00Z", symbols=["EURUSD"])
    w = {x["minutos"]: x for x in _measure(out, "EURUSD")["janelas"]}
    assert "preco" in w[1] and w[5] == {"minutos": 5, "situacao": "pendente"} and w[15]["situacao"] == "pendente"


def test_closed_market_and_stale_reference():
    # Último candle 20 min antes do evento e nada depois.
    rates = {"EURUSD": bars(EVENT - timedelta(minutes=40), [1.1] * 20)}
    client, _ = _client(rates=rates)
    m = _measure(reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"]), "EURUSD")
    assert m["referencia"]["antiga"] is True
    assert all(x["situacao"] == "sem_negociacao" for x in m["janelas"])
    assert any("parado ou fechado" in a for a in m["avisos"]) and any("Nenhum candle" in a for a in m["avisos"])


def test_missing_bar_at_window_end_is_flagged():
    client, _ = _client(rates={"EURUSD": eurusd_bars(skip={64})})  # sem o candle das 11:34
    w = {x["minutos"]: x for x in _measure(
        reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"]), "EURUSD")["janelas"]}
    assert w[5]["preco"] == 1.1002 and w[5]["ate"] == "2026-09-30T11:34:00Z"


def test_no_data_before_event():
    # Histórico do dia anterior: o terminal tem dados, mas não houve negociação nas 2 h antes do evento.
    old = bars(EVENT - timedelta(days=1), [1.1] * 10)
    client, _ = _client(rates={"EURUSD": np.concatenate([old, bars(EVENT, [1.1] * 10)])})
    m = _measure(reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"]), "EURUSD")
    assert m["referencia"] is None and any("fechado ou sem dados" in a for a in m["avisos"])
    assert "fora_do_historico" not in m


def test_event_before_terminal_history_is_not_called_market_closed():
    # O M1 do terminal começa no horário do evento (limite de candles): não é mercado fechado.
    client, _ = _client(rates={"EURUSD": bars(EVENT, [1.1] * 10)})
    m = _measure(reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"]), "EURUSD")
    assert m["referencia"] is None and m["fora_do_historico"] is True
    assert any("Fora do histórico M1" in a and "2026-09-30T11:30:00Z" in a for a in m["avisos"])
    assert not any("fechado ou sem dados" in a for a in m["avisos"])


def test_spread_from_ticks():
    t0 = EVENT.timestamp()
    ticks = make_ticks([(t0 - 120, 1.1, 1.10012), (t0 - 60, 1.1, 1.10012), (t0 + 1, 1.1, 1.1003), (t0 + 30, 1.1, 1.10015)])
    client, _ = _client(ticks={"EURUSD": ticks})
    m = _measure(reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"]), "EURUSD")
    assert m["spread"]["antes_mediana_pontos"] == 12 and m["spread"]["depois_max_pontos"] == 30


def test_unknown_symbol_does_not_break_others():
    client, _ = _client()
    out = reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD", "NAOEXISTE"])
    assert "erro" in _measure(out, "NAOEXISTE") and "janelas" in _measure(out, "EURUSD")


def _calendar(tmp_path):
    path = write(
        tmp_path,
        [ev(1, "Initial Jobless Claims", "initial-jobless-claims", unit="NONE", mult="THOUSANDS", digits=0),
         ev(2, "Retail Sales", "retail-sales-mm")],
        [val(10, 1, EVENT, actual=197, forecast=192, prev=197), val(11, 2, EVENT, actual=0.5, forecast=0.3, prev=0.2)],
        generated_gmt=int(NOW.timestamp()),
    )
    return EconomicCalendar(lambda: path, now_utc=lambda: NOW)


def test_reaction_by_search_finds_latest_event_and_separates_facts(tmp_path):
    client, _ = _client()
    out = reacao.reaction(client, _calendar(tmp_path), search="claims", symbols=["EURUSD"])
    assert out["horario_evento"]["utc"] == "2026-09-30T11:30:00Z"
    assert [f["codigo"] for f in out["fato_publicado"]] == ["initial-jobless-claims"]
    assert out["fato_publicado"][0]["surpresa"]["valor"] == 5.0
    others = out["outros_eventos_no_mesmo_horario"]
    assert len(others) == 1 and others[0].endswith("(retail-sales-mm)")
    with pytest.raises(ValueError, match="Nenhum evento 'cpi'"):
        reacao.reaction(client, _calendar(tmp_path), search="cpi", symbols=["EURUSD"])


def test_reaction_by_time_lists_all_events_at_that_time(tmp_path):
    client, _ = _client()
    out = reacao.reaction(client, _calendar(tmp_path), when="2026-09-30 11:30", symbols=["EURUSD"])
    assert {f["codigo"] for f in out["fato_publicado"]} == {"initial-jobless-claims", "retail-sales-mm"}
    old = reacao.reaction(client, _calendar(tmp_path), when="2026-09-30T10:00:00Z", symbols=["EURUSD"])
    assert old["fato_publicado"] == [] and any("confira o horário" in n for n in old["observacoes"])


def test_reaction_without_calendar_file_still_measures(tmp_path):
    client, _ = _client()
    missing = EconomicCalendar(lambda: tmp_path / "nao_existe.json", now_utc=lambda: NOW)
    out = reacao.reaction(client, missing, when="2026-09-30T11:30:00Z", symbols=["EURUSD"])
    assert any("Calendário indisponível" in n for n in out["observacoes"])
    assert "janelas" in _measure(out, "EURUSD")


def test_reaction_validates_input():
    client, _ = _client()
    with pytest.raises(ValueError, match="ainda não chegou"):
        reacao.reaction(client, None, when="2026-09-30T13:00:00Z", symbols=["EURUSD"])
    with pytest.raises(ValueError, match="Janelas"):
        reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"], windows=[0, 5])
    with pytest.raises(ValueError, match="Informe o evento"):
        reacao.reaction(client, None, symbols=["EURUSD"])
    with pytest.raises(ValueError, match="Informe data e hora"):
        reacao.reaction(client, None, when="ontem", symbols=["EURUSD"])
    with pytest.raises(ValueError, match="Informe data e hora"):
        reacao.reaction(client, None, when="2026-09-30", symbols=["EURUSD"])
    with pytest.raises(ValueError, match="minuto cheio"):
        reacao.reaction(client, None, when="2026-09-30T11:30:30Z", symbols=["EURUSD"])
    with pytest.raises(ValueError, match="No máximo"):
        reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"] * 21)
    with pytest.raises(ValueError, match="horario_utc"):
        reacao.reaction(client, None, search="cpi", symbols=["EURUSD"])


def test_parse_utc_variants():
    expected = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    assert reacao.parse_utc("2026-10-01T12:30:00Z") == expected
    assert reacao.parse_utc("2026-10-01 12:30") == expected
    assert reacao.parse_utc("2026-10-01T09:30:00-03:00") == expected


def test_context_changes_day_range_and_stale_quote():
    # Bid atual do fake = 1,10000. Candles: 1,0980 desde 00:00; 1,0990 a partir de 08:00; 1,0995 a partir de 11:00;
    # 1,1000 a partir de 11:50.
    day = NOW.replace(hour=0)
    closes = [1.098] * 480 + [1.099] * 180 + [1.0995] * 50 + [1.1] * 10
    rates = {"EURUSD": bars(day, closes, highs={600: 1.1010}, lows={10: 1.0970})}
    client, fake = _client(rates=rates, symbols=[make_symbol("EURUSD"), INDEX], tick_age_s={"USTECm": 3600})
    # Dia anterior em H1, último candle às 23:00 fechando em 1,0980.
    fake.rates_tf[("EURUSD", fm.TIMEFRAME_H1)] = hour_bars(day - timedelta(hours=5), [1.097] * 4 + [1.098])
    out = reacao.context(client, ["EURUSD", "USTEC"])
    eur = next(i for i in out["instrumentos"] if i["simbolo"] == "EURUSD")
    v = eur["variacao"]
    assert v["15min"]["pips"] == 5.0  # 11:45 → 1,0995
    assert v["1h"]["pips"] == 10.0 and v["4h"]["pips"] == 20.0 and v["dia_utc"]["pips"] == 20.0
    assert eur["faixa_do_dia"] == {"maxima": 1.101, "minima": 1.097, "posicao_pct": 75.0}
    index = next(i for i in out["instrumentos"] if i["simbolo"] == "USTECm")
    assert index["cotacao"]["estado"] != "atual" and "aviso" in index
    assert index["variacao"]["15min"] is None  # sem candles
    assert any("USTECm" in n for n in out["observacoes"])


def test_context_flags_window_start_without_trading():
    # Sem candles entre 08:00 e 11:00: a janela de 1 h (desde 11:00) começa num preço de antes da parada.
    closes = [1.099] * 60
    rates = {"EURUSD": np.concatenate([bars(NOW.replace(hour=7), closes), bars(NOW.replace(hour=11), [1.0995] * 60)])}
    client, _ = _client(rates=rates)
    eur = reacao.context(client, ["EURUSD"])["instrumentos"][0]
    assert eur["variacao"]["1h"]["desde"] == "2026-09-30T08:00:00Z"
    assert "desde" not in eur["variacao"]["4h"] and "desde" not in eur["variacao"]["15min"]


# ---------------------------------------------------------------- correções da auditoria
def test_extremes_ignore_pre_event_bar_and_bar_after_window():
    # Candle das 11:29 com máxima enorme (antes do evento) e o das 11:45 também (depois de +15).
    client, _ = _client(rates={"EURUSD": eurusd_bars(highs={59: 1.2, 70: 1.1009, 75: 1.3}, lows={59: 1.0})})
    m = _measure(reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"]), "EURUSD")
    assert m["extremos"]["maxima"]["pips"] == 9.0 and m["extremos"]["minima"]["variacao"] == 0.0


def test_three_digit_pair_pips():
    jpy = make_symbol("USDJPY", digits=3, point=0.001, bid=150.0, ask=150.012, trade_tick_size=0.001,
                      currency_base="USD", currency_profit="JPY")
    closes = [150.0] * 60 + [150.05] * 30
    client, _ = _client(rates={"USDJPY": bars(EVENT - timedelta(minutes=60), closes)}, symbols=[jpy])
    w = _measure(reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["USDJPY"]), "USDJPY")["janelas"][0]
    assert w["pips"] == 5.0 and w["pontos"] == 50


def test_typical_move_baseline():
    # Antes do evento o preço alterna 1 pip por minuto: |variação de 1 min| típica = 1 pip.
    pre = [1.1 + (0.0001 if i % 2 else 0.0) for i in range(120)]
    closes = pre + [pre[-1] + 0.0005] * 20
    client, _ = _client(rates={"EURUSD": bars(EVENT - timedelta(minutes=120), closes)})
    w = {x["minutos"]: x for x in _measure(
        reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"]), "EURUSD")["janelas"]}
    assert w[1]["tipico_antes"] == 0.0001 and w[1]["vezes_o_tipico"] == 5.0
    assert "vezes_o_tipico" in w[15]


def test_pending_extremes_are_partial():
    client, _ = _client()
    m = _measure(reacao.reaction(client, None, when="2026-09-30T11:50:00Z", symbols=["EURUSD"]), "EURUSD")
    assert m["extremos"]["parcial"] is True and m["extremos"]["ate"] == "2026-09-30T12:00:00Z"


def test_disconnected_without_reference_keeps_warning():
    client, fake = _client(rates={"EURUSD": bars(EVENT, [1.1] * 10)})
    client.ensure_connected()
    fake.connected = False
    m = _measure(reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"]), "EURUSD")
    assert any("sem conexão" in a for a in m["avisos"])
    empty, _ = _client(rates={})
    gone = _measure(reacao.reaction(empty, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"]), "EURUSD")
    assert any("fora do histórico" in a for a in gone["avisos"])


def test_blank_symbol_does_not_abort():
    client, _ = _client()
    out = reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD", " "])
    assert "erro" in out["movimento_medido"][1] and "janelas" in out["movimento_medido"][0]


def test_spread_before_excludes_tick_at_event():
    t0 = EVENT.timestamp()
    ticks = make_ticks([(t0 - 60, 1.1, 1.10012), (t0, 1.1, 1.1005)])
    client, _ = _client(ticks={"EURUSD": ticks})
    spread = _measure(reacao.reaction(client, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"]), "EURUSD")["spread"]
    assert spread == {"antes_mediana_pontos": 12, "depois_max_pontos": 50}
    none, _ = _client()
    assert "indisponivel" in _measure(
        reacao.reaction(none, None, when="2026-09-30T11:30:00Z", symbols=["EURUSD"]), "EURUSD")["spread"]


def test_rates_between_retries_empty_past_history():
    client, fake = _client()
    calls = {"n": 0}
    real = fake.copy_rates_range

    def flaky(*args):
        calls["n"] += 1
        return real(*args)[:0] if calls["n"] == 1 else real(*args)

    client.ensure_connected()
    fake.copy_rates_range = flaky
    df = client.rates_between("EURUSD", "M1", EVENT - timedelta(minutes=5), EVENT)
    assert len(df) == 6 and calls["n"] == 2  # 11:25 a 11:30, as duas pontas incluídas


def _custom_calendar(tmp_path, events, values, **header):
    path = write(tmp_path, events, values, generated_gmt=int(NOW.timestamp()), **header)
    return EconomicCalendar(lambda: path, now_utc=lambda: NOW)


def test_events_inside_window_are_flagged(tmp_path):
    cal = _custom_calendar(
        tmp_path,
        [ev(1, "Initial Jobless Claims", "initial-jobless-claims", unit="NONE", mult="THOUSANDS", digits=0),
         ev(2, "ISM", "ism-manufacturing-pmi", unit="NONE")],
        [val(10, 1, EVENT, actual=197, forecast=192), val(11, 2, EVENT + timedelta(minutes=10), actual=49, forecast=50)],
    )
    client, _ = _client()
    out = reacao.reaction(client, cal, search="claims", symbols=["EURUSD"])
    inside = out["eventos_dentro_da_janela"]
    assert [(e["codigo"], e["minutos_apos"]) for e in inside] == [("ism-manufacturing-pmi", 10.0)]
    assert out["janelas_afetadas"] == {"15": ["ism-manufacturing-pmi"]}
    assert any("misturam a reação" in n for n in out["observacoes"])


def test_search_prefers_main_release_on_latest_day(tmp_path):
    day = EVENT.replace(hour=10, minute=0)
    cal = _custom_calendar(
        tmp_path,
        [ev(1, "Fed Interest Rate Decision", "fed-interest-rate-decision"),
         ev(2, "FOMC Press Conference", "fomc-press-conference", kind="EVENT")],
        [val(10, 1, day - timedelta(days=1)), val(11, 1, day), val(12, 2, day + timedelta(minutes=30))],
    )
    client, _ = _client(rates={"EURUSD": bars(day - timedelta(minutes=60), [1.1] * 120)})
    out = reacao.reaction(client, cal, search="fomc", symbols=["EURUSD"])
    assert out["horario_evento"]["utc"] == "2026-09-30T10:00:00Z"
    assert [c["utc"] for c in out["outros_candidatos"]] == ["2026-09-30T10:30:00Z", "2026-09-29T10:00:00Z"]


def test_time_with_unmatched_search_lists_events_at_that_time(tmp_path):
    client, _ = _client()
    out = reacao.reaction(client, _calendar(tmp_path), when="2026-09-30T11:30:00Z", search="cpi", symbols=["EURUSD"])
    assert out["fato_publicado"] == [] and len(out["outros_eventos_no_mesmo_horario"]) == 2
    assert any("Nenhum evento 'cpi' nesse horário" in n and "initial-jobless-claims" in n for n in out["observacoes"])


def test_source_reading_is_kept_out_of_facts(tmp_path):
    path = write(
        tmp_path,
        [ev(1, "Initial Jobless Claims", "initial-jobless-claims", unit="NONE", mult="THOUSANDS", digits=0)],
        [{**val(10, 1, EVENT, actual=197, forecast=192), "impact": "CALENDAR_IMPACT_NEGATIVE"}],
        generated_gmt=int(NOW.timestamp()),
    )
    client, _ = _client()
    out = reacao.reaction(client, EconomicCalendar(lambda: path, now_utc=lambda: NOW), search="claims", symbols=["EURUSD"])
    assert "impacto_no_usd_segundo_a_fonte" not in out["fato_publicado"][0]
    assert out["leitura_da_fonte"] == [{"codigo": "initial-jobless-claims", "impacto_no_usd": "negativo"}]


def test_stale_calendar_is_reported(tmp_path):
    cal = _custom_calendar(
        tmp_path,
        [ev(1, "Initial Jobless Claims", "initial-jobless-claims", unit="NONE", mult="THOUSANDS", digits=0)],
        [val(10, 1, EVENT, actual=197, forecast=192)],
    )
    path = cal._locate()
    import json as _json

    data = _json.loads(path.read_text(encoding="utf-8"))
    data["generated_gmt"] = int((NOW - timedelta(days=2)).timestamp())
    path.write_text(_json.dumps(data), encoding="utf-8")
    client, _ = _client()
    out = reacao.reaction(client, cal, search="claims", symbols=["EURUSD"])
    assert any(n.startswith("Calendário desatualizado") for n in out["observacoes"])


def test_context_uses_completed_bars_when_now_is_mid_minute():
    client, fake = _client(rates={"EURUSD": bars(NOW - timedelta(minutes=60), [1.09 + i * 0.0001 for i in range(60)])})
    client.clock.now = NOW + timedelta(seconds=50)
    eur = reacao.context(client, ["EURUSD"])["instrumentos"][0]
    # Janela de 15 min a partir de 11:45:50: o último candle concluído é o das 11:44 (1,0944), não o das 11:45.
    assert eur["variacao"]["15min"]["pips"] == round((1.1 - 1.0944) / 0.0001, 1)


def test_context_day_change_flags_market_closed_before_midnight():
    day = NOW.replace(hour=0)
    client, fake = _client(rates={"EURUSD": bars(day + timedelta(hours=1), [1.099] * 600)})
    fake.rates_tf[("EURUSD", fm.TIMEFRAME_H1)] = hour_bars(day - timedelta(hours=8), [1.098] * 5)  # último às 20:00
    eur = reacao.context(client, ["EURUSD"])["instrumentos"][0]
    assert eur["variacao"]["dia_utc"]["pips"] == 20.0 and eur["variacao"]["dia_utc"]["desde"] == "2026-09-29T21:00:00Z"
    assert eur["faixa_do_dia"]["desde"] == "2026-09-30T01:00:00Z"
