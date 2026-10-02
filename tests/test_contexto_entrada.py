"""Contexto SMC da entrada: só candles fechados antes dela, rótulos a favor/contra e varreduras de níveis chave."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import fake_mt5 as fm
from fake_mt5 import FakeMT5, make_symbol
from test_mt5_client import make_client
from trading_mcp import contexto_entrada as ce
from trading_mcp.mt5_client import MT5Error

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # relógio do FakeClock (quarta-feira)
ENTRY = datetime(2026, 9, 30, 11, 47, 30, tzinfo=UTC)  # sessão de Londres; candles de M1 a H4 abertos na hora
SYMBOL = "USTECm"
TF_CODES = {"M1": fm.TIMEFRAME_M1, "M3": fm.TIMEFRAME_M3, "M5": fm.TIMEFRAME_M5, "M15": fm.TIMEFRAME_M15,
            "H1": fm.TIMEFRAME_H1, "H4": fm.TIMEFRAME_H4}
TF_SECONDS = {"M1": 60, "M3": 180, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400}


def _index():
    return make_symbol(SYMBOL, digits=2, point=0.01, bid=30000.0, ask=30001.12,
                       trade_calc_mode=fm.SYMBOL_CALC_MODE_CFD, currency_base="USD", currency_profit="USD")


def _walk(tf: str, n: int, seed: int) -> np.ndarray:
    """Passeio aleatório de ``n`` candles terminando no candle que contém NOW (há candles depois da entrada)."""
    step = TF_SECONDS[tf]
    last_open = int(NOW.timestamp()) // step * step
    rng = np.random.default_rng(seed)
    closes = 30000 + np.cumsum(rng.normal(0, 6 * np.sqrt(step / 60), n))
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        rows.append((last_open - (n - 1 - i) * step, prev, max(prev, c) + 2, min(prev, c) - 2, c, 10, 112, 0))
        prev = c
    return np.array(rows, dtype=fm.RATES_DTYPE)


def _rates(sizes: dict[str, int] | None = None) -> dict[tuple[str, int], np.ndarray]:
    sizes = sizes or {}
    return {(SYMBOL, TF_CODES[tf]): _walk(tf, sizes.get(tf, 4700 if tf == "M5" else 1700), seed)
            for seed, tf in enumerate(TF_CODES)}


def _client(rates: dict[tuple[str, int], np.ndarray], **fake_kwargs):
    fake = FakeMT5([_index()], **fake_kwargs)
    fake.rates_tf.update(rates)
    client, fake = make_client(fake)
    return client, fake


# ---------------------------------------------------------------- candles até a entrada
def test_rates_until_drops_the_bar_open_at_that_moment():
    client, fake = _client(_rates())
    df = client.rates_until("USTEC", "M15", ENTRY, 10)
    assert len(df) == 10
    last_close = df["time"].iloc[-1].to_pydatetime() + timedelta(minutes=15)
    assert last_close <= ENTRY  # 11:30 fechou às 11:45; o de 11:45 ainda estava aberto às 11:47:30
    assert df["time"].iloc[-1].to_pydatetime() == datetime(2026, 9, 30, 11, 30, tzinfo=UTC)
    assert fake.from_calls[-1][2].tzinfo is not None
    with pytest.raises(ValueError, match="fuso"):
        client.rates_until("USTEC", "M15", ENTRY.replace(tzinfo=None), 10)


def test_rates_until_before_history_is_empty():
    client, _ = _client(_rates())
    assert client.rates_until("USTEC", "M1", datetime(2020, 1, 1, tzinfo=UTC), 10).empty


def test_context_ignores_everything_after_the_entry():
    """O mesmo contexto com e sem os candles de depois (inclusive o que estava aberto), mesmo com picos neles."""
    full = _rates()
    spiked, cut = {}, {}
    for key, data in full.items():
        step = TF_SECONDS[next(tf for tf, code in TF_CODES.items() if code == key[1])]
        after = data["time"] + step > int(ENTRY.timestamp())  # aberto na entrada ou depois
        changed = data.copy()
        changed["high"][after] += 5000
        changed["low"][after] -= 5000
        spiked[key] = changed
        cut[key] = data[~after].copy()
    client_full, _ = _client(spiked)
    client_cut, _ = _client(cut)
    with_future = ce.compute(client_full, "USTEC", "compra", ENTRY, 30010.0)
    without = ce.compute(client_cut, "USTEC", "compra", ENTRY, 30010.0)
    assert with_future == without
    assert set(with_future["estrutura"]) == set(ce.FRAMES) and with_future["faltando"] == []
    assert with_future["sessao"] == "londres" and with_future["hora_nova_york"] == "07:47"


def test_buy_is_compared_by_estimated_bid():
    client, _ = _client(_rates())
    ctx = ce.compute(client, "USTEC", "compra", ENTRY, 30010.0)
    assert ctx["entrada"]["preco_bid_estimado"] == pytest.approx(30010.0 - 112 * 0.01)
    sell = ce.compute(client, "USTEC", "venda", ENTRY, 30010.0)
    assert "preco_bid_estimado" not in sell["entrada"]  # a venda executa no bid


def test_short_history_goes_to_missing_and_labels_say_no_data():
    client, _ = _client(_rates({"M1": 120, "M3": 120}))
    ctx = ce.compute(client, "USTEC", "venda", ENTRY, 30010.0)
    assert {"M1", "M3"} - set(ctx["estrutura"]) == {"M1", "M3"}
    assert any(m.startswith("M1: só") for m in ctx["faltando"])
    dims = ce.dimensions(ctx, "venda")
    assert dims["estrutura_M5"] != "sem_dados"  # o M5 continua avaliado


def test_disconnected_terminal_is_an_error_to_retry():
    client, fake = _client(_rates())
    client.rates_until("USTEC", "M1", ENTRY, 5)  # conecta
    fake.connected = False
    client._connected = False  # o cliente marca a perda de conexão ao ler o terminal
    with pytest.raises(MT5Error, match="sem conexão"):
        ce.compute(client, "USTEC", "compra", ENTRY, 30010.0)


def test_invalid_direction_or_naive_time():
    client, _ = _client(_rates())
    with pytest.raises(ValueError, match="direção"):
        ce.compute(client, "USTEC", "buy", ENTRY, 1.0)
    with pytest.raises(ValueError, match="fuso"):
        ce.compute(client, "USTEC", "compra", ENTRY.replace(tzinfo=None), 1.0)


# ---------------------------------------------------------------- sessão
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 2, 13, 29, tzinfo=UTC), "nova_york"),  # 09:29 em Nova York
        (datetime(2026, 10, 2, 4, 4, tzinfo=UTC), "asia"),  # 13:04 em Tóquio
        (datetime(2026, 10, 2, 9, 0, tzinfo=UTC), "londres"),
        (datetime(2026, 10, 2, 22, 30, tzinfo=UTC), "fora"),  # depois das 17:00 de NY, antes de Tóquio abrir
    ],
)
def test_session_at_entry(moment, expected):
    assert ce._session(moment) == expected


# ---------------------------------------------------------------- varredura de níveis chave
def _flat(start: datetime, end: datetime, minutes: int, price: float = 100.0) -> pd.DataFrame:
    times = pd.date_range(start, end, freq=f"{minutes}min", inclusive="left", tz=UTC)
    return pd.DataFrame({"time": times, "open": price, "high": price + 0.5, "low": price - 0.5, "close": price,
                         "spread": 10})


def _dip(df: pd.DataFrame, at: datetime, low: float) -> pd.DataFrame:
    df = df.copy()
    df.loc[df["time"] == pd.Timestamp(at), "low"] = low
    return df


T = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)  # dia de mercado começou 29/09 21:00 UTC; o anterior, 28/09 21:00
PREV_DAY_LOW = 95.0


def _levels_data(m5_dip_at: datetime | None = None, m1_dip_at: datetime | None = None, dip: float = 94.5):
    m5 = _flat(datetime(2026, 9, 21, 21, 0, tzinfo=UTC), T, 5)
    m5 = _dip(m5, datetime(2026, 9, 29, 3, 0, tzinfo=UTC), PREV_DAY_LOW)  # mínima do dia anterior
    if m5_dip_at:
        m5 = _dip(m5, m5_dip_at, dip)
    m1 = _flat(T - timedelta(hours=3), T, 1)
    if m1_dip_at:
        m1 = _dip(m1, m1_dip_at, dip)
    return m5, m1


def test_previous_day_low_swept_in_the_window_with_price_back():
    at = datetime(2026, 9, 30, 13, 32, tzinfo=UTC)
    m5, m1 = _levels_data(m5_dip_at=datetime(2026, 9, 30, 13, 30, tzinfo=UTC), m1_dip_at=at)
    found = ce._key_level_sweeps(m5, m1, 2, T, 100.0)
    prev = [s for s in found if s["nivel"] == "Mín. dia anterior"]
    assert prev == [{"tipo": "nivel_chave", "nivel": "Mín. dia anterior", "preco": 95.0, "lado": "abaixo",
                     "tomado_em": "2026-09-30T13:32:00Z", "minutos_antes": 27.0}]  # do fechamento do M1
    # O mesmo pavio tomou as mínimas das sessões já encerradas de hoje (Ásia e Londres, em 99,5).
    assert {"Mín. Ásia", "Mín. Londres"} <= {s["nivel"] for s in found}


def test_level_taken_and_price_still_beyond_is_not_a_sweep():
    m5, m1 = _levels_data(m1_dip_at=datetime(2026, 9, 30, 13, 32, tzinfo=UTC))
    assert not [s for s in ce._key_level_sweeps(m5, m1, 2, T, 94.8) if s["nivel"] == "Mín. dia anterior"]


def test_level_already_taken_before_the_window_does_not_count():
    m5, m1 = _levels_data(m5_dip_at=datetime(2026, 9, 30, 10, 0, tzinfo=UTC),
                          m1_dip_at=datetime(2026, 9, 30, 13, 32, tzinfo=UTC))
    assert not [s for s in ce._key_level_sweeps(m5, m1, 2, T, 100.0) if s["nivel"] == "Mín. dia anterior"]


def test_without_m1_the_m5_take_is_used():
    m5, _ = _levels_data(m5_dip_at=datetime(2026, 9, 30, 13, 30, tzinfo=UTC))
    found = [s for s in ce._key_level_sweeps(m5, None, 2, T, 100.0) if s["nivel"] == "Mín. dia anterior"]
    assert found and found[0]["tomado_em"] == "2026-09-30T13:30:00Z"


def test_first_take_just_before_the_window_decides():
    """Janela começa às 12:02:30, dentro do candle M5 das 12:00: tomada às 12:01 (antes) e de novo às 12:10."""
    when = datetime(2026, 9, 30, 14, 2, 30, tzinfo=UTC)
    m5 = _flat(datetime(2026, 9, 21, 21, 0, tzinfo=UTC), datetime(2026, 9, 30, 14, 0, tzinfo=UTC), 5)
    m5 = _dip(_dip(m5, datetime(2026, 9, 29, 3, 0, tzinfo=UTC), PREV_DAY_LOW), datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
              94.5)
    m1 = _flat(when - timedelta(hours=3, seconds=30), when - timedelta(seconds=30), 1)
    early = _dip(_dip(m1, datetime(2026, 9, 30, 12, 1, tzinfo=UTC), 94.5), datetime(2026, 9, 30, 12, 10, tzinfo=UTC),
                 94.5)
    assert not [s for s in ce._key_level_sweeps(m5, early, 2, when, 100.0) if s["nivel"] == "Mín. dia anterior"]
    inside = _dip(m1, datetime(2026, 9, 30, 12, 4, tzinfo=UTC), 94.5)
    found = [s for s in ce._key_level_sweeps(m5, inside, 2, when, 100.0) if s["nivel"] == "Mín. dia anterior"]
    assert [(s["tomado_em"], s["minutos_antes"]) for s in found] == [("2026-09-30T12:04:00Z", 117.5)]


def test_sweep_older_than_the_window_is_ignored():
    m5, m1 = _levels_data(m5_dip_at=datetime(2026, 9, 30, 11, 30, tzinfo=UTC))
    assert not [s for s in ce._key_level_sweeps(m5, m1, 2, T, 100.0) if s["nivel"] == "Mín. dia anterior"]


# ---------------------------------------------------------------- rótulos das estatísticas
def _context(**overrides):
    base = {
        "versao": ce.VERSION, "sessao": "nova_york",
        "estrutura": {tf: {"micro": "alta", "macro": "baixa"} for tf in ce.FRAMES},
        "choch": [], "varreduras": [], "zonas": [], "premium_discount": {}, "faltando": [],
    }
    base.update(overrides)
    return base


def test_dimensions_follow_the_trade_direction():
    ctx = _context(premium_discount={"M15": {"zona": "discount"}, "H1": {"zona": "premium"}})
    buy, sell = ce.dimensions(ctx, "compra"), ce.dimensions(ctx, "venda")
    assert list(buy) == list(ce.DIMENSIONS)
    assert buy["estrutura_M5"] == "a_favor" and sell["estrutura_M5"] == "contra"
    assert buy["estrutura_macro_H1"] == "contra" and sell["estrutura_macro_H1"] == "a_favor"
    assert buy["premium_discount_M15"] == "a_favor" and buy["premium_discount_H1"] == "contra"
    assert sell["premium_discount_M15"] == "contra" and sell["premium_discount_H1"] == "a_favor"
    assert buy["varredura_a_favor"] == "nenhuma" and buy["zona_a_favor"] == "nenhuma"
    assert buy["choch_a_favor"] == "nenhum" and buy["choch_contra"] == "nao"


def test_dimensions_sweeps_zones_and_choch():
    ctx = _context(
        varreduras=[
            {"tipo": "topo_fundo_m1_m3", "lado": "abaixo", "a_favor": True},
            {"tipo": "liquidez_igual", "lado": "abaixo", "a_favor": True},
            {"tipo": "nivel_chave", "lado": "acima", "a_favor": False},
        ],
        zonas=[{"tipo": "FVG", "direcao": "alta"}, {"tipo": "OB", "direcao": "alta"}, {"tipo": "OB", "direcao": "baixa"}],
        choch=[
            {"tf": "M5", "direcao": "alta", "por": "fechamento"},
            {"tf": "M1", "direcao": "alta", "por": "pavio"},  # só pavio: não conta nos rótulos
            {"tf": "M3", "direcao": "baixa", "por": "fechamento"},
        ],
    )
    dims = ce.dimensions(ctx, "compra")
    assert dims["varredura_a_favor"] == "liquidez_igual"  # a mais importante das a favor
    assert dims["zona_a_favor"] == "OB e FVG" and dims["zona_contra"] == "sim"
    assert dims["choch_a_favor"] == "M5" and dims["choch_contra"] == "sim"


def test_dimensions_without_data():
    ctx = _context(estrutura={}, premium_discount={})
    dims = ce.dimensions(ctx, "compra")
    assert dims["estrutura_H4"] == "sem_dados" and dims["choch_a_favor"] == "sem_dados"
    assert dims["premium_discount_M15"] == "sem_dados"
    with_frames = ce.dimensions(_context(premium_discount={"M15": {"zona": "acima_da_faixa"}}), "compra")
    assert with_frames["premium_discount_M15"] == "fora_da_faixa" and with_frames["premium_discount_H1"] == "sem_faixa"
    assert ce.dimensions(_context(estrutura={"M5": {"micro": "indefinida"}}), "venda")["estrutura_M5"] == "indefinida"


# ---------------------------------------------------------------- timeframes coerentes (um M1 reamostrado)
def _resampled(days: int = 36, seed: int = 11) -> dict[tuple[str, int], np.ndarray]:
    """Um passeio aleatório de M1 terminando em NOW e os demais timeframes reamostrados dele (preços coerentes)."""
    n = days * 1440
    last_open = int(NOW.timestamp()) // 60 * 60
    rng = np.random.default_rng(seed)
    closes = 30000 + np.cumsum(rng.normal(0, 6, n))
    opens = np.concatenate([[closes[0]], closes[:-1]])
    wick = np.abs(rng.normal(0, 4, n))
    m1 = pd.DataFrame({"time": last_open - (n - 1 - np.arange(n)) * 60, "open": opens,
                       "high": np.maximum(opens, closes) + wick, "low": np.minimum(opens, closes) - wick,
                       "close": closes})
    out = {}
    for tf, code in TF_CODES.items():
        step = TF_SECONDS[tf]
        g = m1.groupby(m1["time"] // step * step)
        bars = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                             "close": g["close"].last()})
        rows = np.zeros(len(bars), dtype=fm.RATES_DTYPE)
        rows["time"] = bars.index.to_numpy()
        for col in ("open", "high", "low", "close"):
            rows[col] = bars[col].to_numpy()
        rows["tick_volume"], rows["spread"] = 10, 112
        out[(SYMBOL, code)] = rows
    return out


def _split(data: dict[tuple[str, int], np.ndarray], entry: datetime):
    """(com picos em tudo o que abriu na entrada ou depois, só o que já tinha fechado na entrada)."""
    spiked, cut = {}, {}
    code_tf = {code: tf for tf, code in TF_CODES.items()}
    for key, rows in data.items():
        after = rows["time"] + TF_SECONDS[code_tf[key[1]]] > int(entry.timestamp())
        changed = rows.copy()
        changed["high"][after] += 5000
        changed["low"][after] -= 5000
        spiked[key], cut[key] = changed, rows[~after].copy()
    return spiked, cut


def test_coherent_timeframes_without_lookahead_and_with_findings():
    data = _resampled()
    found = {"choch": 0, "varreduras": 0, "zonas": 0}
    for minutes_back in (37, 151, 283, 419):
        entry = NOW - timedelta(minutes=minutes_back, seconds=23)
        spiked, cut = _split(data, entry)
        with_future = ce.compute(_client(spiked)[0], "USTEC", "compra", entry, 30000.0)
        without = ce.compute(_client(cut)[0], "USTEC", "compra", entry, 30000.0)
        assert with_future == without
        for key in found:
            found[key] += len(with_future[key])
    assert all(found.values()), found  # o teste só vale se houver CHoCH, varreduras e zonas para vazar


# ---------------------------------------------------------------- janelas (relatório do smc simulado)
def _fake_analyze(per_tf: dict[str, dict]):
    """smc.analyze simulado: o timeframe sai do espaçamento dos candles; ``per_tf`` dá rompimentos e varreduras."""
    def analyze(df, digits, price):
        step = int((df["time"].iloc[1] - df["time"].iloc[0]).total_seconds())
        tf = next(name for name, seconds in TF_SECONDS.items() if seconds == step)
        extra = per_tf.get(tf, {})
        micro = {"tendencia_por_fechamento": "alta", "tendencia_por_pavio": "alta",
                 "ultimos_rompimentos": extra.get("rompimentos", [])}
        macro = {"tendencia_por_fechamento": "alta", "tendencia_por_pavio": "alta", "ultimos_rompimentos": []}
        return {"estrutura": {"micro": micro, "macro": macro}, "varreduras_recentes": extra.get("varreduras", []),
                "order_blocks": [], "fvg_abertos": []}
    return analyze


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _choch(em: datetime, direction: str = "alta") -> dict:
    return {"direcao": direction, "nivel": 30000.0, "topo_fundo_em": _iso(em - timedelta(hours=1)),
            "por_pavio": None, "por_fechamento": {"tipo": "CHoCH", "em": _iso(em)}}


def test_choch_window_counts_from_the_close_of_the_breaking_candle(monkeypatch):
    # M1: fecha exatamente 30 min antes (entra) e 1 s antes disso (fica fora); M5: o mesmo, com 5 min de candle.
    monkeypatch.setattr(ce.smc, "analyze", _fake_analyze({
        "M1": {"rompimentos": [_choch(ENTRY - timedelta(minutes=31)),
                               _choch(ENTRY - timedelta(minutes=31, seconds=1), "baixa")]},
        "M5": {"rompimentos": [_choch(ENTRY - timedelta(minutes=35)),
                               _choch(ENTRY - timedelta(minutes=35, seconds=1), "baixa")]},
    }))
    client, _ = _client(_rates())
    ctx = ce.compute(client, "USTEC", "compra", ENTRY, 30010.0)
    assert [(c["tf"], c["direcao"], c["minutos_antes"]) for c in ctx["choch"]] == [("M1", "alta", 30.0),
                                                                                ("M5", "alta", 30.0)]
    dims = ce.dimensions(ctx, "compra")
    assert dims["choch_a_favor"] == "M1" and dims["choch_contra"] == "nao"


def _sweep(kind: str, level: float, at: datetime, broken: bool = False) -> dict:
    item = {"de": kind, "nivel": level, "topo_fundo_em": _iso(at - timedelta(hours=1)), "varrido_em": _iso(at)}
    if broken:
        item["rompido_depois_em"] = _iso(at + timedelta(minutes=5))
    return item


def test_smc_sweeps_need_price_back_and_no_later_close_beyond(monkeypatch):
    at = ENTRY - timedelta(minutes=20)
    monkeypatch.setattr(ce.smc, "analyze", _fake_analyze({"M5": {"varreduras": [
        _sweep("fundo", 29990.0, at),  # varredura: entrada (bid 30008,88) acima do fundo
        _sweep("fundos_iguais", 29980.0, at, broken=True),  # fechou abaixo depois: rompimento
        _sweep("fundo", 30050.0, at),  # o preço da entrada continua abaixo: rompimento
        _sweep("topo", 30100.0, at),  # topo varrido, preço de volta abaixo: contra a compra
        _sweep("topo", 30120.0, ENTRY - timedelta(hours=2, minutes=6)),  # fora da janela de 2 h
    ]}}))
    client, _ = _client(_rates())
    ctx = ce.compute(client, "USTEC", "compra", ENTRY, 30010.0)
    smc_sweeps = [(s["nivel"], s["preco"], s["a_favor"], s["minutos_antes"])
                  for s in ctx["varreduras"] if s["tipo"] != "nivel_chave"]
    # minutos_antes contados do fechamento do candle M5 (20 min antes da entrada + 5 min de candle).
    assert smc_sweeps == [("fundo M5", 29990.0, True, 15.0), ("topo M5", 30100.0, False, 15.0)]
    assert ce.dimensions(ctx, "compra")["varredura_a_favor"] in ("nivel_chave", "topo_fundo")


# ---------------------------------------------------------------- bordas do histórico
def test_bars_after_the_requested_time_are_dropped():
    """Antes do início do histórico o terminal devolve o primeiro candle que tem, posterior (conferido em 2026-10-02)."""
    client, fake = _client(_rates())
    m1 = fake.rates_tf[(SYMBOL, fm.TIMEFRAME_M1)]
    fake.copy_rates_from = lambda symbol, timeframe, date_from, count: m1[:3].copy()
    assert client.rates_until("USTEC", "M1", datetime(2020, 1, 1, tzinfo=UTC), 10).empty


def test_env_max_bars_does_not_shorten_the_context():
    fake = FakeMT5([_index()])
    fake.rates_tf.update(_rates())
    client, _ = make_client(fake, max_bars=100)  # MAX_BARS do .env
    assert len(client.rates_until("USTEC", "M5", ENTRY, 1500)) == 1500
    assert ce.compute(client, "USTEC", "compra", ENTRY, 30010.0)["faltando"] == []


def test_history_still_loading_is_retried_not_marked_missing():
    client, fake = _client(_rates())
    m1 = fake.rates_tf[(SYMBOL, fm.TIMEFRAME_M1)]
    # O terminal tem 1.700 candles M1 (copy_rates_from_pos), mas só entregou os 50 últimos antes da entrada.
    fake.copy_rates_from = lambda symbol, timeframe, date_from, count: (
        m1[m1["time"] <= int(date_from.timestamp())][-50:].copy() if timeframe == fm.TIMEFRAME_M1
        else FakeMT5.copy_rates_from(fake, symbol, timeframe, date_from, count)
    )
    with pytest.raises(MT5Error, match="ainda não carregado"):
        ce.compute(client, "USTEC", "compra", ENTRY, 30010.0)


def test_missing_lower_frames_give_no_data_labels():
    ctx = _context(estrutura={tf: {"micro": "alta", "macro": "alta"} for tf in ("M5", "M15", "H1", "H4")},
                   varreduras=[{"tipo": "topo_fundo", "lado": "abaixo", "a_favor": True}])
    dims = ce.dimensions(ctx, "compra")
    assert dims["choch_a_favor"] == "sem_dados" and dims["choch_contra"] == "sem_dados"
    assert dims["zona_a_favor"] == "sem_dados" and dims["zona_contra"] == "sem_dados"
    # Topo/fundo do M5/M15 achado não basta: a liquidez igual (peso maior) não pôde ser procurada no M1/M3.
    assert dims["varredura_a_favor"] == "sem_dados"
    with_key = _context(estrutura=ctx["estrutura"], varreduras=[{"tipo": "nivel_chave", "lado": "abaixo",
                                                                  "a_favor": True}])
    assert ce.dimensions(with_key, "compra")["varredura_a_favor"] == "nivel_chave"  # o nível chave usa o M5
    none_found = _context(estrutura=ctx["estrutura"])
    assert ce.dimensions(none_found, "compra")["varredura_a_favor"] == "sem_dados"
