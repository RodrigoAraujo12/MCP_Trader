"""Backtest do setup A com o histórico do MT5 (só leitura e simulação; nenhuma ordem é enviada).

Uso: python -m trading_mcp.backtest [--simbolo XAUUSDm] [--de AAAA-MM-DD] [--ate AAAA-MM-DD] [--saldo 10000]
         [--spread-mult 1] [--tempo M15|M5] [--engolfo corpo|extremo] [--sem-premium] [--zero-em R]
         [--divisao 2025-01-01] [--calendario ARQUIVO] [--atualizar] [--saida PASTA]

Simulação (conservadora onde a ordem dos preços dentro de um candle é desconhecida):

* Os candles do MT5 são de bid; ask = bid + spread do candle (coluna ``spread``, vezes ``--spread-mult``). A coluna é
  o menor spread do candle; no M1 de 2026 ela ficou igual à média dos ticks do minuto (mediana 1,00; média 1,08 no
  ouro), por isso a execução anda pelo M1. Use ``--spread-mult 1.5`` como teste de estresse.
* A ordem vale a partir do candle seguinte ao engolfo. Venda limite executa quando o bid chega à entrada, compra
  limite quando o ask chega; stop e alvo da venda pelo ask, da compra pelo bid (como no MT5).
* Dentro de cada candle de entrada, o M1 decide a ordem dos acontecimentos quando existe e bate com o candle;
  senão vale o próprio candle. Num mesmo candle (M1 ou o de entrada): stop e alvo -> stop; execução e stop ->
  stop; execução e alvo -> ordem cancelada (o alvo pode ter vindo antes da execução).
* Cancelamento: 1 h sem executar, 16:00 de Nova York, nível varrido fechou além, notícia forte a menos de 30 min,
  alvo antes da execução. Posição aberta é fechada às 16:45 de Nova York (antes da virada e do swap).
* Lote pelos limites do usuário, como em ``limites.py``: risco = menor entre 1,25% do saldo do início do dia, o que
  falta para -5% no dia e para -25% na semana; volume arredondado para baixo no passo; abaixo do mínimo, não opera.
* Só resultados executados: nada de "quanto poderia ter ganhado".
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trading_mcp import tempo
from trading_mcp.calendario import CalendarError, EconomicCalendar
from trading_mcp.limites import MIN_ROOM, RiskRules
from trading_mcp.setup_a import Params, SetupA, Signal, epoch_s

UTC = timezone.utc
CACHE_DIR = Path.home() / "trading-mcp" / "backtest"
# Cópia de uma exportação longa do serviço do calendário (InpDaysBack alto), para o filtro de notícia no passado.
HISTORY_CALENDAR = CACHE_DIR / "calendar_US_hist.json"
_COLUMNS = ("open", "high", "low", "close", "spread")
_TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}
_CHUNK_DAYS = {"M1": 7, "M5": 30, "M15": 90, "H1": 365}
DEFAULT_SPLIT = datetime(2025, 1, 1, tzinfo=UTC)
# Critério combinado antes de ver o resultado (vale para o período de conferência).
CRITERIA = {"operacoes_min": 100, "r_medio_min": 0.1, "pior_semana_pct_min": -25.0}
_LONDON_END_MSD = 990  # 09:30 de Nova York em minutos desde a virada das 17:00

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- dados
@dataclass(frozen=True)
class Spec:
    symbol: str
    point: float
    digits: int
    money_per_unit: float  # resultado de 1 lote por 1,0 de preço, na moeda da conta
    vol_min: float
    vol_step: float
    vol_max: float


@dataclass
class Data:
    spec: Spec
    entry_tf: str
    bars: pd.DataFrame  # tempo de entrada
    context: pd.DataFrame  # H1
    m1: pd.DataFrame | None


def spec_from(info: dict[str, Any]) -> Spec:
    tick_size, tick_value = float(info.get("tick_size") or 0), float(info.get("tick_value") or 0)
    per_unit = tick_value / tick_size if tick_size > 0 and tick_value > 0 else float(info["contrato"])
    return Spec(symbol=info["simbolo"], point=float(info["ponto"]), digits=int(info["digitos"]),
                money_per_unit=per_unit, vol_min=float(info["volume_min"]), vol_step=float(info["volume_step"]),
                vol_max=float(info["volume_max"]))


def _save(df: pd.DataFrame, path: Path, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {c: df[c].to_numpy(dtype=float) for c in _COLUMNS}
    np.savez_compressed(path, time=epoch_s(df["time"]), **arrays)
    path.with_suffix(".json").write_text(json.dumps(meta, indent=1), encoding="utf-8")


def _load(path: Path) -> tuple[pd.DataFrame, dict[str, Any]] | None:
    meta_path = path.with_suffix(".json")
    if not path.is_file() or not meta_path.is_file():
        return None
    with np.load(path) as z:
        df = pd.DataFrame({c: z[c] for c in _COLUMNS})
        df.insert(0, "time", pd.to_datetime(z["time"], unit="s", utc=True))
    return df, json.loads(meta_path.read_text(encoding="utf-8"))


def fetch(client: Any, symbol: str, tf: str, start: datetime, end: datetime,
          say: Callable[[str], None] = print) -> pd.DataFrame:
    """Candles de ``start`` a ``end`` em pedaços (o MT5 responde melhor a janelas curtas)."""
    parts = []
    a = start
    step = timedelta(days=_CHUNK_DAYS[tf])
    while a < end:
        b = min(a + step, end)
        df = client.rates_between(symbol, tf, a, b)
        if len(df):
            parts.append(df[["time", *_COLUMNS]])
        elif (b - a) >= timedelta(days=3):
            say(f"Aviso: {symbol} {tf} sem candles de {a:%Y-%m-%d} a {b:%Y-%m-%d} (histórico ainda não carregado?).")
        a = b
    if not parts:
        return pd.DataFrame(columns=["time", *_COLUMNS])
    out = pd.concat(parts).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    # Só candles já fechados em ``end`` (o último pode estar em formação).
    closed = out["time"] + pd.Timedelta(seconds=_TF_SECONDS[tf]) <= pd.Timestamp(end)
    return out[(out["time"] >= pd.Timestamp(start)) & closed].reset_index(drop=True)


def _covered_until(df: pd.DataFrame, tf: str, first: datetime) -> str:
    """Até onde o cache vale: o fechamento do último candle recebido (não o fim pedido, que pode ter vindo sem os
    candles mais novos); o próximo uso completa a partir daí."""
    if not len(df):
        return tempo.iso_utc(first)
    last = df["time"].iloc[-1].to_pydatetime() + timedelta(seconds=_TF_SECONDS[tf])
    return tempo.iso_utc(last)


def load_data(client: Any, symbol: str, start: datetime, end: datetime, entry_tf: str = "M15",
              cache_dir: Path = CACHE_DIR, refresh: bool = False, say: Callable[[str], None] = print) -> Data:
    """Candles do tempo de entrada, do H1 e do M1 (o que o terminal tiver), guardados em ``cache_dir``."""
    spec = spec_from(client.symbol_spec(symbol))
    frames: dict[str, pd.DataFrame | None] = {}
    wanted = {entry_tf: start - timedelta(days=21), "H1": start - timedelta(days=45)}
    try:
        wanted["M1"] = max(start, client.oldest_bar(spec.symbol, "M1"))
    except Exception as exc:  # noqa: BLE001 - sem M1 a simulação usa o tempo de entrada
        say(f"Sem M1 no terminal ({exc}); a simulação usa só o tempo de entrada.")
    for tf, first in wanted.items():
        path = cache_dir / f"{spec.symbol}_{tf}.npz"
        cached = None if refresh else _load(path)
        if cached is not None and cached[1].get("de", "~") <= tempo.iso_utc(first):
            df, meta = cached
            if meta["ate"] < tempo.iso_utc(end):
                # Completa o fim (com um dia de sobra: o candle mais novo do cache pode ter mudado de pedaço).
                tail_from = datetime.strptime(meta["ate"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) - timedelta(days=1)
                say(f"Completando {spec.symbol} {tf} de {tail_from:%Y-%m-%d} a {end:%Y-%m-%d}...")
                tail = fetch(client, spec.symbol, tf, tail_from, end, say)
                df = pd.concat([df[df["time"] < pd.Timestamp(tail_from)], tail]).reset_index(drop=True)
                meta = {**meta, "ate": _covered_until(df, tf, first), "candles": len(df)}
                _save(df, path, meta)
        else:
            say(f"Baixando {spec.symbol} {tf} de {first:%Y-%m-%d} a {end:%Y-%m-%d}...")
            df = fetch(client, spec.symbol, tf, first, end, say)
            _save(df, path, {"simbolo": spec.symbol, "tf": tf, "de": tempo.iso_utc(first),
                             "ate": _covered_until(df, tf, first),
                             "baixado_utc": tempo.iso_utc(datetime.now(UTC)), "candles": len(df)})
        frames[tf] = df[(df["time"] >= pd.Timestamp(first)) & (df["time"] < pd.Timestamp(end))].reset_index(drop=True)
    m1 = frames.get("M1")
    return Data(spec=spec, entry_tf=entry_tf, bars=frames[entry_tf], context=frames["H1"],
                m1=m1 if m1 is not None and len(m1) else None)


def load_news(path: Path | None, start: datetime, end: datetime) -> tuple[list[datetime], dict[str, Any]]:
    """Notícias fortes dos EUA (calendário do MT5), o período que o arquivo cobre e se cobre o backtest inteiro."""
    if path is None:
        return [], {"arquivo": None, "cobre": None, "cobre_o_periodo": False}
    cal = EconomicCalendar(lambda: path)
    try:
        events, covered = cal.events_between(start, end, "alta")
        first, last = cal.coverage()
    except CalendarError as exc:
        return [], {"arquivo": str(path), "erro": str(exc), "cobre_o_periodo": False}
    times = sorted({datetime.strptime(e["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) for e in events})
    return times, {"arquivo": str(path), "cobre": [tempo.iso_utc(first), tempo.iso_utc(last)], "eventos": len(times),
                   "cobre_o_periodo": covered}


# --------------------------------------------------------------------------- simulação
@dataclass
class _Order:
    signal: Signal
    lots: float
    risk: float


@dataclass
class _Position:
    signal: Signal
    lots: float
    risk: float
    fill: float
    fill_time: datetime
    stop: float
    flat_s: int
    session: str
    resolution: set[str] = field(default_factory=set)
    moved: bool = False
    last: tuple[float, float] | None = None  # (fechamento bid, spread) do último candle percorrido
    last_t: int | None = None  # fim desse candle (s)


@dataclass
class Result:
    trades: list[dict[str, Any]]
    refusals: Counter
    cancels: Counter
    signals: int
    start_balance: float
    final_balance: float
    m1_bars: int = 0
    entry_bars: int = 0


class Simulator:
    def __init__(self, data: Data, params: Params, *, balance: float = 10_000.0, rules: RiskRules = RiskRules(),
                 spread_mult: float = 1.0, news: list[datetime] | None = None, start: datetime | None = None,
                 end: datetime | None = None) -> None:
        self.data, self.p, self.rules = data, params, rules
        self.spec = data.spec
        self.tf_s = _TF_SECONDS[data.entry_tf]
        bars = data.bars.copy()
        bars["spread_bar"] = bars["spread"]  # tolerância do engolfo: o estresse muda o custo, não os sinais
        bars["spread"] = bars["spread"] * spread_mult
        self.strat = SetupA(bars, data.context, self.spec.point, self.spec.digits, params, self.tf_s, news)
        self.open, self.high, self.low, self.close = (bars[c].to_numpy(dtype=float) for c in ("open", "high", "low",
                                                                                              "close"))
        self.spread = self.strat.spread
        self.bo = self.strat.bar_open_s
        m1 = data.m1
        if m1 is not None and len(m1):
            self.m1_t = epoch_s(m1["time"])
            self.m1 = {c: m1[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close")}
            self.m1["spread"] = m1["spread"].to_numpy(dtype=float) * spread_mult * self.spec.point
        else:
            self.m1_t = None
        self.start_s = int(start.timestamp()) if start else -2**62
        self.end_s = int(end.timestamp()) if end else 2**62
        self.balance = self.start_balance = float(balance)
        self.order: _Order | None = None
        self.pos: _Position | None = None
        self.trades: list[dict[str, Any]] = []
        self.refusals: Counter = Counter()
        self.cancels: Counter = Counter()
        self.signals = 0
        self.m1_used = self.entry_used = 0
        self._day = self._week = None
        self.day_base = self.week_base = self.balance
        self.day_result = self.week_result = 0.0

    # ------------------------------------------------------------------ candles dentro do candle de entrada
    def _subbars(self, i: int) -> list[tuple[int, int, float, float, float, float, float, str]]:
        """(abertura s, duração s, o, h, l, c, spread, resolução) para percorrer o candle ``i`` em ordem."""
        if self.m1_t is not None:
            a = int(np.searchsorted(self.m1_t, self.bo[i]))
            b = int(np.searchsorted(self.m1_t, self.bo[i] + self.tf_s))
            if b > a:
                tol = 2 * self.spec.point + 1e-9
                h, l = self.m1["high"][a:b], self.m1["low"][a:b]
                if abs(h.max() - self.high[i]) <= tol and abs(l.min() - self.low[i]) <= tol:
                    self.m1_used += 1
                    return [(int(self.m1_t[k]), 60, self.m1["open"][k], self.m1["high"][k], self.m1["low"][k],
                             self.m1["close"][k], self.m1["spread"][k], "M1") for k in range(a, b)]
        self.entry_used += 1
        return [(int(self.bo[i]), self.tf_s, self.open[i], self.high[i], self.low[i], self.close[i], self.spread[i],
                 self.data.entry_tf)]

    def _cancel(self, reason: str) -> None:
        self.cancels[reason] += 1
        self.order = None

    def _open(self, i: int, t: int, price: float, res: str) -> None:
        od = self.order
        assert od is not None
        msd = int(self.strat.msd[i]) + (t - int(self.bo[i])) // 60
        self.pos = _Position(signal=od.signal, lots=od.lots, risk=od.risk, fill=price,
                             fill_time=datetime.fromtimestamp(t, UTC), stop=od.signal.stop,
                             flat_s=int(self.strat.flat_time(i).timestamp()),
                             session="londres" if msd < _LONDON_END_MSD else "nova_york", resolution={res})
        self.order = None
        self.strat.register_fill(od.signal)

    def _close(self, t: int, price: float, reason: str) -> None:
        pos = self.pos
        assert pos is not None
        sig = pos.signal
        sell = sig.direction == "venda"
        pnl = ((pos.fill - price) if sell else (price - pos.fill)) * pos.lots * self.spec.money_per_unit
        before = self.balance
        self.balance += pnl
        self.day_result += pnl
        self.week_result += pnl
        self.trades.append({
            "n": len(self.trades) + 1,
            "direcao": sig.direction,
            "niveis": "+".join(sig.level_names),
            "alvo_nivel": sig.target_name,
            "sinal_utc": tempo.iso_utc(sig.time),
            "executada_utc": tempo.iso_utc(pos.fill_time),
            "saida_utc": tempo.iso_utc(datetime.fromtimestamp(t, UTC)),
            "sessao": pos.session,
            "entrada_plano": sig.entry,
            "executada": round(pos.fill, self.spec.digits),
            "stop": sig.stop,
            "alvo": sig.target,
            "rr_plano": sig.rr,
            "zona_pct": sig.zone_pct,
            "spread": round(sig.spread, self.spec.digits),
            "lotes": pos.lots,
            "risco": round(pos.risk, 2),
            "saida": round(price, self.spec.digits),
            "motivo": reason,
            "resultado": round(pnl, 2),
            "r": round(pnl / pos.risk, 3) if pos.risk > 0 else None,
            "saldo_antes": round(before, 2),
            "saldo_depois": round(self.balance, 2),
            "base_semana": round(self.week_base, 2),
            "semana": int(self._week) if self._week is not None else None,
            "resolucao": "+".join(sorted(pos.resolution)),
        })
        self.pos = None

    def _sub(self, i: int, sb: tuple[int, int, float, float, float, float, float, str]) -> None:
        t, dur, o, h, l, c, s, res = sb
        if self.order is not None:
            sig = self.order.signal
            sell = sig.direction == "venda"
            if t >= int(sig.valid_until.timestamp()):
                return self._cancel("prazo")
            if self.strat.in_news(datetime.fromtimestamp(t, UTC)):
                return self._cancel("noticia")
            fill = h >= sig.entry if sell else l + s <= sig.entry
            seen = l + s <= sig.target if sell else h >= sig.target
            stop = h + s >= sig.stop if sell else l <= sig.stop
            if fill and stop:
                # Abertura além da entrada e do stop (salto): executa na abertura e o stop dispara na hora.
                self._open(i, t, max(sig.entry, o) if sell else min(sig.entry, o + s), res)
                return self._close(t, max(sig.stop, o + s) if sell else min(sig.stop, o), "stop")
            if seen:
                return self._cancel("alvo_antes_da_execucao")
            if fill:
                self._open(i, t, max(sig.entry, o) if sell else min(sig.entry, o + s), res)
                self.pos.last, self.pos.last_t = (c, s), t + dur  # type: ignore[union-attr]
            return None
        pos = self.pos
        if pos is None:
            return None
        pos.resolution.add(res)
        sig = pos.signal
        sell = sig.direction == "venda"
        if t >= pos.flat_s:
            if t - pos.flat_s < 3600:
                return self._close(t, o + s if sell else o, "horario")
            # O histórico pula a saída das 16:45 (buraco nos dados da corretora ou fechamento mais cedo): o robô teria
            # saído no horário; o último preço conhecido antes do buraco é a melhor estimativa.
            last_c, last_s = pos.last if pos.last is not None else (o, s)
            return self._close(pos.last_t or t, last_c + last_s if sell else last_c, "sem_dados_ate_o_horario")
        if (h + s >= pos.stop) if sell else (l <= pos.stop):
            price = max(pos.stop, o + s) if sell else min(pos.stop, o)
            return self._close(t, price, "zero" if pos.moved else "stop")
        if (l + s <= sig.target) if sell else (h >= sig.target):
            return self._close(t, min(sig.target, o + s) if sell else max(sig.target, o), "alvo")
        be = self.p.breakeven_r
        if be is not None and not pos.moved:
            dist = abs(sig.entry - sig.stop)
            if (l + s <= pos.fill - be * dist) if sell else (h >= pos.fill + be * dist):
                pos.stop, pos.moved = pos.fill, True
        if t + dur >= pos.flat_s:
            return self._close(t + dur, c + s if sell else c, "horario")
        pos.last, pos.last_t = (c, s), t + dur
        return None

    def _size(self, sig: Signal) -> tuple[float, float] | str:
        r = self.rules
        day_room = r.daily_pct / 100 * self.day_base + self.day_result
        week_room = r.weekly_pct / 100 * self.week_base + self.week_result
        if day_room <= MIN_ROOM:
            return "limite_dia"
        if week_room <= MIN_ROOM:
            return "limite_semana"
        allowed = min(r.per_trade_pct / 100 * self.day_base, day_room, week_room)
        per_lot = abs(sig.entry - sig.stop) * self.spec.money_per_unit
        step = self.spec.vol_step
        lots = min(math.floor(allowed / per_lot / step + 1e-9) * step, self.spec.vol_max)
        lots = round(lots, 8)
        if lots < self.spec.vol_min - 1e-12:
            return "lote_minimo"
        return lots, lots * per_lot

    # ------------------------------------------------------------------ laço
    def run(self) -> Result:
        n = len(self.bo)
        for i in range(n):
            day, week = self.strat.day[i], self.strat.week[i]
            if day != self._day:
                self._day, self.day_base, self.day_result = day, self.balance, 0.0
            if week != self._week:
                self._week, self.week_base, self.week_result = week, self.balance, 0.0
            if self.order is not None or self.pos is not None:
                for sb in self._subbars(i):
                    self._sub(i, sb)
                    if self.order is None and self.pos is None:
                        break
            step = self.strat.step(i)
            end_s = int(self.bo[i]) + self.tf_s
            if self.order is not None and set(self.order.signal.levels) & set(step.broken):
                self._cancel("nivel_rompido")
            if self.order is not None and end_s >= int(self.order.signal.valid_until.timestamp()):
                self._cancel("prazo")
            if self.pos is not None and end_s >= self.pos.flat_s:
                sell = self.pos.signal.direction == "venda"
                self._close(end_s, self.close[i] + (self.spread[i] if sell else 0.0), "horario")
            if not self.start_s <= int(self.bo[i]) < self.end_s:
                continue
            for r in step.refusals:
                self.refusals[r["reason"]] += 1
            for sig in step.signals:
                self.signals += 1
                if self.order is not None or self.pos is not None:
                    self.refusals["ocupado"] += 1
                    continue
                sized = self._size(sig)
                if isinstance(sized, str):
                    self.refusals[sized] += 1
                    continue
                self.order = _Order(sig, *sized)
        if self.pos is not None:
            sell = self.pos.signal.direction == "venda"
            self._close(int(self.bo[-1]) + self.tf_s, self.close[-1] + (self.spread[-1] if sell else 0.0),
                        "fim_dos_dados")
        if self.order is not None:
            self._cancel("fim_dos_dados")
        return Result(self.trades, self.refusals, self.cancels, self.signals, self.start_balance, self.balance,
                      self.m1_used, self.entry_used)


# --------------------------------------------------------------------------- relatório
def stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"operacoes": 0}
    pnl = np.array([t["resultado"] for t in trades], dtype=float)
    r = np.array([t["r"] for t in trades], dtype=float)
    win, loss = pnl > 0, pnl < 0
    gross_loss = -pnl[loss].sum()
    equity = np.concatenate([[trades[0]["saldo_antes"]], [t["saldo_depois"] for t in trades]])
    peak = np.maximum.accumulate(equity)
    weeks: dict[Any, list[float]] = {}
    for t in trades:
        item = weeks.setdefault(t["semana"], [0.0, t["base_semana"]])
        item[0] += t["resultado"]
    worst_week = min(v[0] / v[1] * 100 for v in weeks.values() if v[1] > 0)
    streak = longest = 0
    for x in pnl:
        streak = streak + 1 if x < 0 else 0
        longest = max(longest, streak)
    return {
        "operacoes": len(trades),
        "ganhos": int(win.sum()),
        "perdas": int(loss.sum()),
        "zeradas": int((pnl == 0).sum()),
        "taxa_acerto_pct": round(win.mean() * 100, 1),
        "r_medio": round(float(r.mean()), 3),
        "r_total": round(float(r.sum()), 2),
        "fator_lucro": round(float(pnl[win].sum() / gross_loss), 2) if gross_loss > 0 else None,
        "resultado": round(float(pnl.sum()), 2),
        "resultado_pct": round(float(pnl.sum() / trades[0]["saldo_antes"] * 100), 2),
        "drawdown_max_pct": round(float(((peak - equity) / peak).max() * 100), 2),
        "pior_semana_pct": round(float(worst_week), 2),
        "maior_sequencia_de_perdas": longest,
    }


def _short(trades: list[dict[str, Any]]) -> dict[str, Any]:
    s = stats(trades)
    return {k: s.get(k) for k in ("operacoes", "taxa_acerto_pct", "r_medio", "r_total")}


def _by(trades: list[dict[str, Any]], key: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        groups.setdefault(str(key(t)), []).append(t)
    return {k: _short(v) for k, v in sorted(groups.items())}


def verdict(s: dict[str, Any]) -> dict[str, Any]:
    checks = {
        f"pelo menos {CRITERIA['operacoes_min']} operações": s.get("operacoes", 0) >= CRITERIA["operacoes_min"],
        f"R médio acima de {CRITERIA['r_medio_min']}": (s.get("r_medio") or -1) > CRITERIA["r_medio_min"],
        f"nenhuma semana pior que {CRITERIA['pior_semana_pct_min']:g}%":
            s.get("operacoes", 0) > 0 and s.get("pior_semana_pct", -100) > CRITERIA["pior_semana_pct_min"],
    }
    return {"aprovado": all(checks.values()), "itens": checks}


def report(result: Result, *, params: Params, spec: Spec, entry_tf: str, start: datetime, end: datetime,
           split: datetime, spread_mult: float, news_info: dict[str, Any], data_info: dict[str, Any]) -> dict[str, Any]:
    trades = result.trades
    split_iso = tempo.iso_utc(split)
    fit = [t for t in trades if t["sinal_utc"] < split_iso]
    check = [t for t in trades if t["sinal_utc"] >= split_iso]
    total_sub = result.m1_bars + result.entry_bars
    params_out = {k: (str(v) if not isinstance(v, (int, float, str, bool, type(None), tuple)) else v)
                  for k, v in asdict(params).items()}
    return {
        "simbolo": spec.symbol,
        "tempo_entrada": entry_tf,
        "periodo": {"de": tempo.iso_utc(start), "ate": tempo.iso_utc(end), "divisao": split_iso},
        "saldo_inicial": result.start_balance,
        "saldo_final": round(result.final_balance, 2),
        "spread_mult": spread_mult,
        "regras": params_out,
        "sinais": result.signals,
        "recusas": dict(result.refusals.most_common()),
        "ordens_canceladas": dict(result.cancels.most_common()),
        "geral": stats(trades),
        "ajuste": stats(fit),
        "conferencia": stats(check),
        "criterio_conferencia": verdict(stats(check)),
        "por_direcao": _by(trades, lambda t: t["direcao"]),
        "por_sessao": _by(trades, lambda t: t["sessao"]),
        "por_nivel": _by(trades, lambda t: t["niveis"].split("+")[0]),
        "por_ano": _by(trades, lambda t: t["sinal_utc"][:4]),
        "por_saida": dict(Counter(t["motivo"] for t in trades).most_common()),
        "resolucao": {"candles_de_entrada_vistos_no_M1_pct": round(result.m1_bars / total_sub * 100, 1)
                      if total_sub else None, **data_info},
        "noticias": news_info,
    }


def markdown(rep: dict[str, Any]) -> str:
    def line(name: str, s: dict[str, Any]) -> str:
        if not s.get("operacoes"):
            return f"| {name} | 0 | | | | | | |"
        return (f"| {name} | {s['operacoes']} | {s['taxa_acerto_pct']}% | {s['r_medio']} | {s['fator_lucro']} | "
                f"{s['resultado_pct']}% | {s['drawdown_max_pct']}% | {s['pior_semana_pct']}% |")

    v = rep["criterio_conferencia"]
    out = [
        f"# Backtest setup A — {rep['simbolo']} {rep['tempo_entrada']}",
        "",
        f"Período {rep['periodo']['de'][:10]} a {rep['periodo']['ate'][:10]}; ajuste antes de "
        f"{rep['periodo']['divisao'][:10]}, conferência depois. Saldo inicial {rep['saldo_inicial']:.0f}, spread x"
        f"{rep['spread_mult']:g}.",
        "",
        "| Período | Operações | Acerto | R médio | Fator de lucro | Resultado | Drawdown máx. | Pior semana |",
        "|---|---|---|---|---|---|---|---|",
        line("Ajuste", rep["ajuste"]),
        line("Conferência", rep["conferencia"]),
        line("Tudo", rep["geral"]),
        "",
        f"**Critério na conferência: {'APROVADO' if v['aprovado'] else 'REPROVADO'}** — "
        + "; ".join(f"{k}: {'ok' if ok else 'não'}" for k, ok in v["itens"].items()),
        "",
        "Por direção: " + "; ".join(f"{k} {s['operacoes']} ops, R médio {s['r_medio']}"
                                     for k, s in rep["por_direcao"].items()),
        "Por sessão: " + "; ".join(f"{k} {s['operacoes']} ops, R médio {s['r_medio']}"
                                    for k, s in rep["por_sessao"].items()),
        "Por nível varrido: " + "; ".join(f"{k} {s['operacoes']} ops, R médio {s['r_medio']}"
                                          for k, s in rep["por_nivel"].items()),
        "Por ano: " + "; ".join(f"{k} {s['operacoes']} ops, R médio {s['r_medio']}"
                                 for k, s in rep["por_ano"].items()),
        "Saídas: " + ", ".join(f"{k} {n}" for k, n in rep["por_saida"].items()),
        "",
        f"Sinais: {rep['sinais']}. Recusas: " + ", ".join(f"{k} {n}" for k, n in rep["recusas"].items()),
        "Ordens canceladas: " + ", ".join(f"{k} {n}" for k, n in rep["ordens_canceladas"].items()),
        f"Candles de entrada percorridos no M1: {rep['resolucao']['candles_de_entrada_vistos_no_M1_pct']}% "
        f"(o resto pelo próprio candle, regra conservadora).",
        f"Notícias: {rep['noticias']}",
    ]
    if not rep["noticias"].get("cobre_o_periodo"):
        out.insert(14, "**Atenção: o calendário não cobre o período todo; fora dele as operações incluem horários de "
                       "notícia forte que o robô recusaria.**")
    return "\n".join(out)


# --------------------------------------------------------------------------- linha de comando
def _date(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trading_mcp.backtest", description=__doc__.splitlines()[0])
    parser.add_argument("--simbolo", default="XAUUSDm")
    parser.add_argument("--de", type=_date, help="início dos sinais (padrão: 3 semanas depois do 1º candle)")
    parser.add_argument("--ate", type=_date, help="fim (padrão: agora)")
    parser.add_argument("--saldo", type=float, default=10_000.0)
    parser.add_argument("--spread-mult", type=float, default=1.0)
    parser.add_argument("--tempo", choices=("M15", "M5"), default="M15")
    parser.add_argument("--engolfo", choices=("corpo", "extremo"), default="corpo")
    parser.add_argument("--sem-premium", action="store_true", help="sem o filtro de prêmio/desconto do H1")
    parser.add_argument("--zero-em", type=float, help="move o stop para o zero depois de tantos R a favor")
    parser.add_argument("--poi", choices=("nenhum", "fvg_h1"), default="nenhum",
                        help="POI do tempo maior onde a varredura tem de acontecer (fvg_h1: FVG do H1 ainda não tocado)")
    parser.add_argument("--opera-fim-do-mes", action="store_true",
                        help="opera no último dia útil do mês (a metodologia só evita esse dia em forex e ouro)")
    parser.add_argument("--divisao", type=_date, default=DEFAULT_SPLIT)
    parser.add_argument("--calendario", type=Path,
                        help="arquivo calendar_US.json (padrão: a cópia longa em ~/trading-mcp/backtest, senão o do terminal)")
    parser.add_argument("--atualizar", action="store_true", help="baixa os candles de novo, ignorando o cache")
    parser.add_argument("--saida", type=Path, help="pasta do relatório (padrão: ~/trading-mcp/backtest/execucoes)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    from trading_mcp.config import load_settings
    from trading_mcp.mt5_client import MT5Client

    client = MT5Client(load_settings())
    client.ensure_connected()
    end = args.ate or datetime.now(UTC).replace(second=0, microsecond=0)
    first = client.oldest_bar(args.simbolo, args.tempo)
    start = args.de or (first + timedelta(days=21)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = max(start, first + timedelta(days=21))
    data = load_data(client, args.simbolo, start, end, args.tempo, refresh=args.atualizar)
    cal_path = args.calendario
    if cal_path is None:
        cal_path = HISTORY_CALENDAR if HISTORY_CALENDAR.is_file() else (
            Path(client.terminal_data_path()) / "MQL5" / "Files" / "trading_mcp" / "calendar_US.json")
    news, news_info = load_news(cal_path, start - timedelta(days=1), end)
    params = Params(engulf=args.engolfo, premium_discount=not args.sem_premium, breakeven_r=args.zero_em,
                    skip_month_end=not args.opera_fim_do_mes, poi=args.poi)
    result = Simulator(data, params, balance=args.saldo, spread_mult=args.spread_mult, news=news,
                       start=start, end=end).run()
    data_info = {"m1_desde": tempo.iso_utc(data.m1["time"].iloc[0].to_pydatetime()) if data.m1 is not None else None}
    rep = report(result, params=params, spec=data.spec, entry_tf=args.tempo, start=start, end=end,
                 split=args.divisao, spread_mult=args.spread_mult, news_info=news_info, data_info=data_info)
    folder = args.saida or CACHE_DIR / "execucoes" / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "relatorio.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False, default=str),
                                           encoding="utf-8")
    pd.DataFrame(result.trades).to_csv(folder / "operacoes.csv", index=False, encoding="utf-8")
    text = markdown(rep)
    (folder / "resumo.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"\nArquivos em {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
