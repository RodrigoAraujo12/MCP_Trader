"""Estratégias de continuação para comparar com o setup A (só candles fechados, como no setup A).

Pesquisa de 2026-10-06: o que tem evidência pública para índices é continuação/momentum, não reversão depois de
varredura. Três testes, com as regras fixadas antes de ver o resultado:

* ``Rompimento``: os mesmos níveis de liquidez do setup A (Ásia, Londres, dia e semana anteriores), mas a favor do
  rompimento: candle fecha além de um nível ainda vivo -> entra a mercado no sentido do rompimento, stop do outro
  lado do candle do rompimento (mais 1 spread), sem alvo, zera às 16:45 de Nova York. Mesmos filtros do setup A
  (horário 02:00-16:00, notícia, fim do mês, feriados) e stop mínimo de 3 spreads. Cada nível rompe uma vez só.
* ``FaixaRuido``: "Beat the Market" (Zarattini, Aziz e Barbon, Swiss Finance Institute 24-97, 2024; regras
  conferidas no PDF, seções 2-3). Ruído(t, HH:MM) = média, nos 14 dias anteriores, de |preço às HH:MM / abertura
  das 9:30 - 1|; faixa de cima = max(abertura 9:30, fechamento 16:00 da véspera) x (1 + ruído), de baixo = min(...)
  x (1 - ruído). Decide só às HH:00 e HH:30 (10:00 a 15:30): compra acima da faixa, vende abaixo; sai quando o preço
  numa decisão passa do stop móvel max(faixa de cima, VWAP) na compra / min(faixa de baixo, VWAP) na venda (e vira a
  mão se passou da faixa oposta); zera às 16:00. Adaptações: lote pelo risco do usuário até um stop de proteção na
  faixa oposta (o artigo usa 100% do capital, ou alvo de volatilidade até 4x, sem stop fixo); VWAP com o volume de
  ticks do CFD (o CFD não tem volume real). Na prática a virada de mão quase não acontece (0 vezes em 2021-2026 no
  USTECm): o ruído cresce ao longo do dia, então a faixa oposta numa decisão posterior fica além do stop de proteção,
  que dispara antes; a regra vira "stop e nova entrada na decisão seguinte". Auditoria de 2026-10-06: sem olhar o
  futuro, mas o resultado depende de executar no primeiro tick depois da decisão (`--atraso-seg 60` corta a
  vantagem para perto de zero).
* ``ORB``: rompimento da abertura de 5 minutos (Zarattini, Aziz e Barbon, "Can Day Trading Really Be Profitable?",
  SSRN 4416622), como controle: candle das 9:30-9:35 de alta -> compra na abertura das 9:35, de baixa -> venda,
  doji -> nada; stop no outro extremo desse candle; alvo de 10R; zera às 16:00. Uma replicação em CFDs de índice
  achou resultado líquido perto de zero: o esperado aqui é não passar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from trading_mcp.setup_a import Clock, Level, Params, SetupA, Signal, Step, market_minute

_CASH_OPEN, _CASH_CLOSE = market_minute(time(9, 30)), market_minute(time(16, 0))


def _common_refusal(s: Clock, i: int, when: datetime, *, month_end: bool, holidays: bool) -> str | None:
    if not s.in_window[i]:
        return "fora_do_horario"
    if month_end and s.month_end[i]:
        return "fim_do_mes"
    if holidays and s.holiday[i]:
        return "feriado_eua"
    if s.in_news(when):
        return "noticia"
    return None


def _market_signal(s: Clock, i: int, direction: str, stop: float, *, target: float | None = None,
                   names: tuple[str, ...] = (), ids: tuple[str, ...] = ()) -> Signal:
    """Ordem a mercado na abertura do candle seguinte; a referência do lote é o fechamento (ask na compra)."""
    sp = float(s.spread[i])
    entry = s._round(s.close[i] + (sp if direction == "compra" else 0.0))
    dist = abs(entry - stop)
    when = s.bar_end(i)
    return Signal(i=i, time=when, direction=direction, entry=entry, stop=s._round(stop),
                  target=None if target is None else s._round(target),
                  rr=None if target is None or dist <= 0 else round(abs(target - entry) / dist, 2), spread=sp,
                  valid_until=when + timedelta(minutes=s.tf_min), levels=ids, level_names=names, target_name="",
                  zone_pct=None, kind="mercado")


# --------------------------------------------------------------------------- rompimento dos níveis de liquidez
class Rompimento(SetupA):
    """Rompimento por fechamento dos níveis do setup A, a favor (usa de ``Params`` só níveis, horários, filtros,
    ``stop_spreads`` e ``min_stop_spreads``)."""

    NAME = "rompimento de liquidez"

    def step(self, i: int) -> Step:
        self._roll(i)
        out = Step()
        self._update_levels(i, out)
        broken = [self.levels[lid] for lid in out.broken]
        for direction, side in (("compra", "alta"), ("venda", "baixa")):
            group = [lv for lv in broken if lv.side == side]
            if not group:
                continue
            result = self._breakout(i, direction, group)
            if isinstance(result, Signal):
                out.signals.append(result)
            else:
                out.refusals.append({"i": i, "time": self.bar_end(i), "direction": direction, "reason": result,
                                     "levels": [lv.name for lv in group]})
        return out

    def _breakout(self, i: int, direction: str, group: list[Level]) -> Signal | str:
        p = self.p
        why = _common_refusal(self, i, self.bar_end(i), month_end=p.skip_month_end, holidays=p.skip_us_holidays)
        if why:
            return why
        s = float(self.spread[i])
        buy = direction == "compra"
        stop = self.low[i] - p.stop_spreads * s if buy else self.high[i] + p.stop_spreads * s
        sig = _market_signal(self, i, direction, stop, names=tuple(lv.name for lv in group),
                             ids=tuple(lv.id for lv in group))
        if abs(sig.entry - sig.stop) < p.min_stop_spreads * s:
            return "stop_curto"
        return sig


# --------------------------------------------------------------------------- faixa de ruído (Beat the Market)
@dataclass(frozen=True)
class RuidoParams:
    lookback: int = 14
    multiplier: float = 1.0
    first_check: time = time(10, 0)
    last_check: time = time(15, 30)
    check_every_min: int = 30
    flat_at: time = time(16, 0)
    news_min: int = 0  # o artigo não filtra notícia
    skip_us_holidays: bool = True


class FaixaRuido(Clock):
    NAME = "faixa de ruído (intraday momentum)"

    def __init__(self, bars: pd.DataFrame, context: pd.DataFrame, point: float, digits: int, params: RuidoParams,
                 tf_seconds: int, news: list[datetime] | None = None) -> None:
        super().__init__(bars, point, digits, tf_seconds, news, params.news_min, params.flat_at,
                         time(9, 30), time(16, 0))
        if 30 % self.tf_min:
            raise ValueError("A faixa de ruído precisa de candles que dividam 30 min (M1, M5, M15).")
        self.p = params
        first, last = market_minute(params.first_check), market_minute(params.last_check)
        self.checks = set(range(first, last + 1, params.check_every_min))
        end = self.msd + self.tf_min
        self.end_msd_bar = end
        self._daily(end)
        self.side = 0  # posição que o simulador confirmou: +1 comprado, -1 vendido
        self._day: Any = None
        self._pv = self._v = 0.0

    def _daily(self, end: np.ndarray) -> None:
        """Abertura das 9:30, fechamento das 16:00 e ruído por dia e horário de decisão (só dias anteriores)."""
        df = pd.DataFrame({"day": self.day, "msd": self.msd, "end": end, "open": self.open, "close": self.close})
        opens = df[df["msd"] == _CASH_OPEN].groupby("day")["open"].first()
        closes = df[df["end"] == _CASH_CLOSE].groupby("day")["close"].last()
        taus = sorted(self.checks)
        at = df[df["end"].isin(taus)].pivot_table(index="day", columns="end", values="close", aggfunc="last")
        days = sorted(set(opens.index))
        self.open930 = opens.to_dict()
        prev_close: dict[Any, float] = {}
        last_close = None
        for d in sorted(set(opens.index) | set(closes.index)):
            if last_close is not None:
                prev_close[d] = last_close
            if d in closes.index:
                last_close = float(closes[d])
        self.prev_close = prev_close
        moves = (at.reindex(days).div(opens.reindex(days), axis=0) - 1).abs()
        # Média dos ``lookback`` dias anteriores com dado naquele horário (o próprio dia fica de fora; dia sem o
        # candle daquele horário, como num fechamento mais cedo, é pulado em vez de anular a média).
        cols = {}
        for col in moves.columns:
            known = moves[col].dropna()
            cols[col] = known.shift(1).rolling(self.p.lookback, min_periods=self.p.lookback).mean().reindex(days)
        self.sigma = pd.DataFrame(cols, index=days)

    def register_fill(self, signal: Signal) -> None:
        self.side = 1 if signal.direction == "compra" else -1

    def register_exit(self, signal: Signal) -> None:
        self.side = 0

    def step(self, i: int) -> Step:
        out = Step()
        day, msd, end = self.day[i], int(self.msd[i]), int(self.end_msd_bar[i])
        if day != self._day:
            self._day, self._pv, self._v = day, 0.0, 0.0
        if _CASH_OPEN <= msd and end <= _CASH_CLOSE:
            vol = self.volume[i] if self.volume is not None and self.volume[i] > 0 else 1.0
            self._pv += (self.high[i] + self.low[i] + self.close[i]) / 3 * vol
            self._v += vol
        if end not in self.checks or self._v <= 0:
            return out
        o930, pc = self.open930.get(day), self.prev_close.get(day)
        sigma = self.sigma.at[day, end] if day in self.sigma.index and end in self.sigma.columns else np.nan
        if o930 is None or pc is None or not np.isfinite(sigma):
            return out
        m = self.p.multiplier * float(sigma)
        upper, lower = max(o930, pc) * (1 + m), min(o930, pc) * (1 - m)
        price, vwap = self.close[i], self._pv / self._v
        if self.side == 1:
            if price >= max(upper, vwap):
                return out
            out.exit_now = "stop_movel"
            if price < lower:
                self._enter(i, out, "venda", upper)
        elif self.side == -1:
            if price <= min(lower, vwap):
                return out
            out.exit_now = "stop_movel"
            if price > upper:
                self._enter(i, out, "compra", lower)
        elif price > upper:
            self._enter(i, out, "compra", lower)
        elif price < lower:
            self._enter(i, out, "venda", upper)
        return out

    def _enter(self, i: int, out: Step, direction: str, protect: float) -> None:
        when = self.bar_end(i)
        why = "feriado_eua" if self.p.skip_us_holidays and self.holiday[i] else "noticia" if self.in_news(when) else None
        if why:
            out.refusals.append({"i": i, "time": when, "direction": direction, "reason": why, "levels": []})
            return
        out.signals.append(_market_signal(self, i, direction, protect, names=("faixa",)))


# --------------------------------------------------------------------------- ORB de 5 minutos (controle)
@dataclass(frozen=True)
class OrbParams:
    target_r: float = 10.0
    flat_at: time = time(16, 0)
    news_min: int = 0  # o artigo não filtra notícia
    skip_us_holidays: bool = True


class ORB(Clock):
    NAME = "ORB 5 min (controle)"

    def __init__(self, bars: pd.DataFrame, context: pd.DataFrame, point: float, digits: int, params: OrbParams,
                 tf_seconds: int, news: list[datetime] | None = None) -> None:
        super().__init__(bars, point, digits, tf_seconds, news, params.news_min, params.flat_at,
                         time(9, 30), time(16, 0))
        if self.tf_min != 5:
            raise ValueError("O ORB de 5 minutos usa candles M5.")
        self.p = params

    def step(self, i: int) -> Step:
        out = Step()
        if int(self.msd[i]) != _CASH_OPEN:
            return out
        o, c = self.open[i], self.close[i]
        if c == o:
            return out
        direction = "compra" if c > o else "venda"
        when = self.bar_end(i)
        why = "feriado_eua" if self.p.skip_us_holidays and self.holiday[i] else "noticia" if self.in_news(when) else None
        if why:
            out.refusals.append({"i": i, "time": when, "direction": direction, "reason": why, "levels": []})
            return out
        stop = self.low[i] if direction == "compra" else self.high[i]
        ref = self.close[i] + (float(self.spread[i]) if direction == "compra" else 0.0)
        dist = abs(ref - stop)
        if dist <= 0:
            return out
        target = ref + self.p.target_r * dist if direction == "compra" else ref - self.p.target_r * dist
        out.signals.append(_market_signal(self, i, direction, stop, target=target, names=("orb",)))
        return out


STRATEGIES: dict[str, tuple[type[Clock], type]] = {
    "setup_a": (SetupA, Params),
    "rompimento": (Rompimento, Params),
    "faixa_ruido": (FaixaRuido, RuidoParams),
    "orb": (ORB, OrbParams),
}
