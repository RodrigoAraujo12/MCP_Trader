"""Setup A do Tiago (varredura + engolfo, entrada nos 50% do engolfo) com regras fixas, para o robô.

O mesmo código roda no backtest e na demo: recebe candles fechados, um por vez (``step``), e devolve os sinais
(ordem limite com stop e alvo), os níveis que fecharam além (para cancelar a ordem ligada a eles) e as recusas
com o motivo. Nada aqui olha um candle que ainda não fechou: os níveis só valem depois de a janela deles acabar,
e os topos/fundos do H1 só depois de confirmados.

Regras da versão 1 (combinadas com o usuário em 2026-10-06; ver ``Params``):

1. Tempos: contexto no H1; varredura e engolfo no tempo de entrada (M15 no ouro).
2. Liquidez: máxima e mínima da Ásia (18:00-02:00 de Nova York), de Londres (02:00-09:30), do dia de mercado
   anterior (vira às 17:00 de Nova York) e da semana de mercado anterior (domingo 17:00); só enquanto nenhum
   candle passou do nível.
3. Varredura: o pavio passa do nível e o candle fecha do lado de dentro. Fechou além: o nível morre.
4. Engolfo: candle contrário cujo corpo cobre o corpo do anterior, no candle da varredura ou até 2 candles depois.
5. Entrada: ordem limite nos 50% do engolfo (pavio a pavio), válida por 1 h e no máximo até 16:00 de Nova York.
6. Stop: extremo da varredura mais 1 spread; sem operação se o stop ficar menor que 3 spreads.
7. Alvo: a primeira liquidez ainda pendente do outro lado; só com risco/retorno de pelo menos 2.
8. Área: venda só em prêmio e compra só em desconto, na última perna do H1 (topos/fundos de 5 candles).
9. Sinais só de 02:00 a 16:00 de Nova York; nenhum de 30 min antes a 30 min depois de notícia forte dos EUA nem no
   último dia útil do mês (forex e ouro). Reentrada no mesmo nível enquanto ele não fechar além, até 2 execuções
   por nível.
10. Regra operacional (auditoria de 2026-10-06): sem sinais em feriado dos EUA nem nos dias de fechamento mais cedo
    (véspera de Natal, 3 de julho, dia seguinte ao Dia de Ação de Graças): o mercado fecha antes das 16:45 e a
    posição atravessaria a pausa.

Spread: coluna ``spread`` dos candles (em pontos), já multiplicada pelo fator de estresse quando houver; a coluna
opcional ``spread_bar`` (sem o fator) é a tolerância do engolfo, para o estresse mudar só o custo. No MT5 a coluna é
o menor spread do candle; em 2026 ela ficou igual à média dos ticks do minuto no M1 (mediana 1,00; média 1,08 no
ouro e 1,01 no Nasdaq, medido em 2026-10-06).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from trading_mcp import smc, tempo

LEVEL_NAMES = ("asia", "londres", "dia_anterior", "semana_anterior")
_ROLL = 17 * 60  # o dia de mercado vira às 17:00 de Nova York
# Minutos desde a virada (17:00 de Nova York): Ásia 18:00-02:00, Londres 02:00-09:30.
_ASIA = (60, 540)
_LONDON = (540, 990)


def epoch_s(times: Any) -> np.ndarray:
    """Horários (coluna ou índice com fuso) -> segundos desde 1970, qualquer que seja a resolução do pandas."""
    return (pd.DatetimeIndex(pd.to_datetime(times, utc=True)).as_unit("ns").asi8 // 10**9).astype(np.int64)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-ésimo ``weekday`` (0 = segunda) do mês; n = -1 é o último."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Domingo de Páscoa (algoritmo gregoriano anônimo)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date) -> date:
    """Feriado no sábado vale na sexta; no domingo, na segunda."""
    return day - timedelta(days=1) if day.weekday() == 5 else day + timedelta(days=1) if day.weekday() == 6 else day


def us_market_holidays(year: int) -> set[date]:
    """Feriados da bolsa dos EUA (regras da NYSE) e os dias de fechamento mais cedo (3 de julho, dia seguinte ao Dia
    de Ação de Graças, véspera de Natal). Nesses dias os CFDs de índices e metais fecham antes do horário normal."""
    days = {
        _observed(date(year, 1, 1)), _observed(date(year, 7, 4)), _observed(date(year, 12, 25)),
        _nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3), _nth_weekday(year, 5, 0, -1),
        _nth_weekday(year, 9, 0, 1), _easter(year) - timedelta(days=2),
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    days |= {thanksgiving, thanksgiving + timedelta(days=1)}
    for early in (date(year, 7, 3), date(year, 12, 24)):
        if early.weekday() < 5:
            days.add(early)
    return days


def market_minute(clock: time) -> int:
    """Horário de Nova York -> minutos desde a virada do dia de mercado (17:00)."""
    return (clock.hour * 60 + clock.minute - _ROLL) % 1440


@dataclass(frozen=True)
class Params:
    levels: tuple[str, ...] = LEVEL_NAMES
    engulf: str = "corpo"  # "corpo": corpo cobre o do anterior; "extremo": fecha além da mínima/máxima do anterior
    engulf_max_bars: int = 2
    entry_fraction: float = 0.5  # 0,5 = meio do engolfo, de pavio a pavio
    order_valid_min: int = 60
    stop_spreads: float = 1.0
    min_stop_spreads: float = 3.0
    min_rr: float = 2.0
    breakeven_r: float | None = None  # stop no zero depois de andar tantos R a favor (None = não move)
    premium_discount: bool = True
    pd_pivot: int = 5
    pd_band_pct: float = 2.5  # 47,5-52,5% = equilíbrio, sem operação
    entry_start: time = time(2, 0)
    entry_end: time = time(16, 0)
    flat_at: time = time(16, 45)
    news_min: int = 30
    skip_month_end: bool = True
    skip_us_holidays: bool = True
    # POI do tempo maior onde a varredura tem de acontecer: "nenhum" (v1) ou "fvg_h1" (FVG do H1 ainda não tocado).
    poi: str = "nenhum"
    max_attempts: int = 2

    def __post_init__(self) -> None:
        unknown = set(self.levels) - set(LEVEL_NAMES)
        if unknown:
            raise ValueError(f"Níveis desconhecidos: {sorted(unknown)}. Válidos: {', '.join(LEVEL_NAMES)}.")
        if self.poi not in ("nenhum", "fvg_h1"):
            raise ValueError("poi deve ser 'nenhum' ou 'fvg_h1'.")
        if self.engulf not in ("corpo", "extremo"):
            raise ValueError("engulf deve ser 'corpo' ou 'extremo'.")
        if not 0 < self.entry_fraction < 1:
            raise ValueError("entry_fraction deve ficar entre 0 e 1.")
        if not market_minute(self.entry_start) < market_minute(self.entry_end) <= market_minute(self.flat_at):
            raise ValueError("Horários fora de ordem: precisa ser entry_start < entry_end <= flat_at no mesmo dia.")


@dataclass
class Level:
    id: str
    name: str
    side: str  # "alta" (máxima; a varredura dela arma venda) ou "baixa" (mínima; arma compra)
    price: float
    state: str = "pendente"  # pendente -> tomado (pavio passou) ou rompido (fechou além)
    outside: bool = False  # tomado fora do horário de entrada: não arma setup
    attempts: int = 0  # ordens executadas a partir deste nível
    armed: bool = False
    first_sweep: int = -1  # primeiro candle da varredura em andamento
    last_sweep: int = -1
    extreme: float = math.nan


@dataclass(frozen=True)
class Signal:
    i: int  # candle do engolfo
    time: datetime  # fechamento do engolfo (a ordem vale a partir daqui)
    direction: str  # "compra" ou "venda"
    entry: float
    stop: float
    target: float
    rr: float
    spread: float
    valid_until: datetime
    levels: tuple[str, ...]
    level_names: tuple[str, ...]
    target_name: str
    zone_pct: float | None
    poi: str | None = None


@dataclass
class Step:
    signals: list[Signal] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)


class SetupA:
    """Máquina de estados do setup A sobre os candles do tempo de entrada (do mais antigo ao mais novo).

    ``bars``: time (abertura, UTC), open, high, low, close, spread (pontos). ``context``: o mesmo no H1.
    ``news``: horários (UTC) das notícias fortes, para o filtro de 30 min.
    """

    def __init__(self, bars: pd.DataFrame, context: pd.DataFrame, point: float, digits: int, params: Params,
                 tf_seconds: int, news: list[datetime] | None = None) -> None:
        self.p = params
        self.point, self.digits = point, digits
        self.tf_min = tf_seconds // 60
        times = pd.DatetimeIndex(pd.to_datetime(bars["time"], utc=True)).as_unit("ns")
        self.times = times
        self.open, self.high, self.low, self.close = (bars[c].to_numpy(dtype=float) for c in ("open", "high", "low",
                                                                                              "close"))
        self.spread = bars["spread"].to_numpy(dtype=float) * point
        tol = bars["spread_bar"] if "spread_bar" in bars else bars["spread"]
        self.tolerance = tol.to_numpy(dtype=float) * point
        # Relógio de parede de Nova York + 7 h: a virada das 17:00 cai à meia-noite (com o horário de verão).
        shifted = times.tz_convert(tempo.NOVA_YORK).tz_localize(None) + pd.Timedelta(hours=7)
        self.msd = (shifted.hour * 60 + shifted.minute).to_numpy()
        days = shifted.normalize()
        # Dia de mercado = data de Nova York da parte principal do dia (o de segunda começa domingo 17:00), em dias
        # desde 1970-01-01; semana = ano*100 + semana ISO dessa data.
        self.day = (days.asi8 // 86_400_000_000_000).astype(np.int64)
        iso = days.isocalendar()
        self.week = (iso["year"].to_numpy() * 100 + iso["week"].to_numpy()).astype(np.int64)
        self.bar_open_s = (times.asi8 // 10**9).astype(np.int64)
        start, end = market_minute(params.entry_start), market_minute(params.entry_end)
        self.start_msd, self.end_msd, self.flat_msd = start, end, market_minute(params.flat_at)
        self.in_window = (self.msd >= start) & (self.msd + self.tf_min <= end)
        last = {d: self._last_weekday(d) for d in np.unique(self.day)}
        self.month_end = np.array([last[d] for d in self.day], dtype=bool)
        holidays = {d for y in {self.day_date(d).year for d in last} for d in us_market_holidays(y)}
        self.holiday = np.array([self.day_date(d).date() in holidays for d in self.day], dtype=bool)
        self._stats(bars)
        self._context(context)
        self.news = np.array(sorted(int(n.timestamp()) for n in (news or [])), dtype=np.int64)
        self.levels: dict[str, Level] = {}
        self.active: list[Level] = []
        self._cur_day: Any = None
        self._cur_week: int | None = None
        self._activated: set[str] = set()

    # ------------------------------------------------------------------ preparo
    @staticmethod
    def day_date(day: int) -> datetime:
        return datetime(1970, 1, 1) + timedelta(days=int(day))

    @classmethod
    def _last_weekday(cls, day: int) -> bool:
        """Último dia útil (segunda a sexta) do mês? Feriados não entram na conta."""
        date = cls.day_date(day)
        nxt = date + timedelta(days=3 if date.weekday() == 4 else 1)
        return nxt.month != date.month

    def _stats(self, bars: pd.DataFrame) -> None:
        """Máxima/mínima por dia, semana e janela de sessão (cada uma só é usada depois de acabar)."""
        df = pd.DataFrame({"day": self.day, "week": self.week, "msd": self.msd, "high": self.high, "low": self.low})
        self.day_hl = {k: (g["high"].max(), g["low"].min()) for k, g in df.groupby("day")}
        self.week_hl = {k: (g["high"].max(), g["low"].min()) for k, g in df.groupby("week")}
        self.session_hl: dict[tuple[Any, str], tuple[float, float]] = {}
        for name, (a, b) in (("asia", _ASIA), ("londres", _LONDON)):
            part = df[(df["msd"] >= a) & (df["msd"] < b)]
            for k, g in part.groupby("day"):
                self.session_hl[(k, name)] = (g["high"].max(), g["low"].min())
        day_keys, week_keys = sorted(self.day_hl), sorted(self.week_hl)
        self.prev_day = dict(zip(day_keys[1:], day_keys[:-1]))
        self.prev_week = dict(zip(week_keys[1:], week_keys[:-1]))

    def _context(self, context: pd.DataFrame) -> None:
        """Topos/fundos confirmados do H1, para prêmio/desconto."""
        ctx_times = pd.DatetimeIndex(pd.to_datetime(context["time"], utc=True)).as_unit("ns")
        self.ctx_close = (ctx_times.asi8 // 10**9 + 3600).astype(np.int64)
        self.ctx_high = context["high"].to_numpy(dtype=float)
        self.ctx_low = context["low"].to_numpy(dtype=float)
        n = len(context)
        self.last_top = np.full(n, -1)
        self.last_bottom = np.full(n, -1)
        top = bottom = -1
        confirmed = {}
        for t, p, kind in smc.pivots(self.ctx_high, self.ctx_low, self.p.pd_pivot):
            confirmed.setdefault(t, []).append((p, kind))
        for k in range(n):
            for p, kind in confirmed.get(k, ()):
                if kind == "topo":
                    top = p
                else:
                    bottom = p
            self.last_top[k], self.last_bottom[k] = top, bottom
        self._fvgs()

    def _fvgs(self) -> None:
        """FVGs do H1 pela regra mais simples (3 candles, espaço entre o pavio do 1º e o do 3º, sem filtro de
        tamanho), o candle de entrada a partir do qual existem (fechamento do 3º) e o primeiro que entra na zona.

        O primeiro toque é calculado de uma vez sobre todos os candles, mas só é usado como "o primeiro toque caiu
        entre o início da varredura e o candle atual", que depende só de candles já fechados."""
        h, l = self.ctx_high, self.ctx_low
        bull = np.flatnonzero(l[2:] > h[:-2]) + 2
        bear = np.flatnonzero(h[2:] < l[:-2]) + 2
        k = np.concatenate([bull, bear])
        self.fvg_dir = np.concatenate([np.ones(len(bull), dtype=int), -np.ones(len(bear), dtype=int)])
        self.fvg_bottom = np.concatenate([h[bull - 2], h[bear]])
        self.fvg_top = np.concatenate([l[bull], l[bear - 2]])
        self.fvg_from = np.searchsorted(self.bar_open_s, self.ctx_close[k]).astype(np.int64)
        n = len(self.high)
        touch = np.full(len(k), n + 1, dtype=np.int64)
        for m in range(len(k)):
            j = int(self.fvg_from[m])
            step = 256
            while j < n:
                seg = (self.low[j:j + step] <= self.fvg_top[m]) if self.fvg_dir[m] > 0 else \
                    (self.high[j:j + step] >= self.fvg_bottom[m])
                hit = np.flatnonzero(seg)
                if len(hit):
                    touch[m] = j + int(hit[0])
                    break
                j += step
                step *= 2
        self.fvg_touch = touch

    # ------------------------------------------------------------------ ajudas
    def bar_end(self, i: int) -> datetime:
        return self.times[i].to_pydatetime() + timedelta(minutes=self.tf_min)

    def day_time(self, i: int, msd: int) -> datetime:
        """Instante (UTC) do minuto ``msd`` do dia de mercado do candle ``i``."""
        return self.times[i].to_pydatetime() + timedelta(minutes=int(msd) - int(self.msd[i]))

    def flat_time(self, i: int) -> datetime:
        return self.day_time(i, self.flat_msd)

    def in_news(self, moment: datetime) -> bool:
        """``moment`` está a menos de ``news_min`` minutos de uma notícia forte?"""
        if not len(self.news):
            return False
        t = int(moment.timestamp())
        k = int(np.searchsorted(self.news, t - self.p.news_min * 60))
        return k < len(self.news) and self.news[k] <= t + self.p.news_min * 60

    def _round(self, price: float) -> float:
        return round(round(price / self.point) * self.point, self.digits)

    def zone(self, i: int, price: float) -> float | None:
        """Posição de ``price`` (0-100%) na última perna do H1 fechado até o fim do candle ``i``, esticada pelos
        extremos posteriores (inclusive os do H1 ainda aberto, vistos nos candles de entrada já fechados)."""
        end = int(self.bar_open_s[i]) + self.tf_min * 60
        k = int(np.searchsorted(self.ctx_close, end, side="right")) - 1
        if k < 0 or self.last_top[k] < 0 or self.last_bottom[k] < 0:
            return None
        hi = float(self.ctx_high[self.last_top[k]:k + 1].max())
        lo = float(self.ctx_low[self.last_bottom[k]:k + 1].min())
        j = int(np.searchsorted(self.bar_open_s, self.ctx_close[k]))
        if j <= i:
            hi, lo = max(hi, float(self.high[j:i + 1].max())), min(lo, float(self.low[j:i + 1].min()))
        if hi <= lo:
            return None
        return (price - lo) / (hi - lo) * 100

    # ------------------------------------------------------------------ níveis
    def _new_level(self, key: str, name: str, side: str, price: float) -> None:
        lid = f"{key}:{name}:{side}"
        if lid in self.levels:
            return
        lv = Level(lid, name, side, float(price))
        self.levels[lid] = lv
        self.active.append(lv)

    def _roll(self, i: int) -> None:
        day, week = self.day[i], int(self.week[i])
        if day != self._cur_day:
            keep_week = week == self._cur_week
            self.active = [lv for lv in self.active if lv.name == "semana_anterior" and keep_week]
            self._cur_day, self._activated = day, set()
            label = self.day_date(day).date().isoformat()
            prev = self.prev_day.get(day)
            if prev is not None and "dia_anterior" in self.p.levels:
                hi, lo = self.day_hl[prev]
                self._new_level(label, "dia_anterior", "alta", hi)
                self._new_level(label, "dia_anterior", "baixa", lo)
            if not keep_week:
                self._cur_week = week
                prev_w = self.prev_week.get(week)
                if prev_w is not None and "semana_anterior" in self.p.levels:
                    hi, lo = self.week_hl[prev_w]
                    self._new_level(str(week), "semana_anterior", "alta", hi)
                    self._new_level(str(week), "semana_anterior", "baixa", lo)
        for name, (_, ends) in (("asia", _ASIA), ("londres", _LONDON)):
            if name in self._activated or name not in self.p.levels or self.msd[i] < ends:
                continue
            self._activated.add(name)
            hl = self.session_hl.get((day, name))
            if hl is not None:
                label = self.day_date(day).date().isoformat()
                self._new_level(label, name, "alta", hl[0])
                self._new_level(label, name, "baixa", hl[1])

    def register_fill(self, signal: Signal) -> None:
        """Uma ordem do sinal foi executada: conta a tentativa nos níveis dele."""
        for lid in signal.levels:
            if lid in self.levels:
                self.levels[lid].attempts += 1

    # ------------------------------------------------------------------ candle a candle
    def step(self, i: int) -> Step:
        """Processa o candle ``i`` (já fechado)."""
        self._roll(i)
        out = Step()
        h, l, c, window = self.high[i], self.low[i], self.close[i], bool(self.in_window[i])
        for lv in self.active:
            if lv.state == "rompido":
                continue
            up = lv.side == "alta"
            wick = h > lv.price if up else l < lv.price
            beyond = c > lv.price if up else c < lv.price
            if beyond:
                lv.state, lv.armed = "rompido", False
                out.broken.append(lv.id)
                continue
            if wick:
                if lv.state == "pendente":
                    lv.outside = not window
                lv.state = "tomado"
                if window and not lv.outside and lv.attempts < self.p.max_attempts:
                    extreme = h if up else l
                    if not lv.armed:
                        lv.armed, lv.extreme, lv.first_sweep = True, extreme, i
                    else:
                        lv.extreme = max(lv.extreme, extreme) if up else min(lv.extreme, extreme)
                    lv.last_sweep = i
            if lv.armed and i - lv.last_sweep > self.p.engulf_max_bars:
                lv.armed = False

        for direction, side in (("venda", "alta"), ("compra", "baixa")):
            armed = [lv for lv in self.active if lv.armed and lv.side == side]
            if not armed or not self._engulf(i, direction):
                continue
            for lv in armed:
                lv.armed = False  # o engolfo consome a varredura; reentrada pede varredura nova
            armed.sort(key=lambda lv: -lv.price if side == "alta" else lv.price)
            extreme = max(lv.extreme for lv in armed) if side == "alta" else min(lv.extreme for lv in armed)
            result = self._build(i, direction, armed, extreme)
            if isinstance(result, Signal):
                out.signals.append(result)
            else:
                out.refusals.append({"i": i, "time": self.bar_end(i), "direction": direction, "reason": result,
                                     "levels": [lv.name for lv in armed]})
        return out

    def _engulf(self, i: int, direction: str) -> bool:
        if i == 0:
            return False
        o, c, po, pc = self.open[i], self.close[i], self.open[i - 1], self.close[i - 1]
        tol = self.tolerance[i]  # a abertura de um candle quase nunca repete o fechamento do anterior no centavo
        if direction == "venda":
            if c >= o:
                return False
            if self.p.engulf == "extremo":
                return c < self.low[i - 1]
            return o >= max(po, pc) - tol and c < min(po, pc)
        if c <= o:
            return False
        if self.p.engulf == "extremo":
            return c > self.high[i - 1]
        return o <= min(po, pc) + tol and c > max(po, pc)

    def _build(self, i: int, direction: str, armed: list[Level], extreme: float) -> Signal | str:
        p = self.p
        when = self.bar_end(i)
        if not self.in_window[i]:
            return "fora_do_horario"
        valid = min(when + timedelta(minutes=p.order_valid_min), self.day_time(i, self.end_msd))
        if valid <= when:
            return "sem_tempo"  # engolfo no último candle antes das 16:00: a ordem nasceria vencida
        if p.skip_month_end and self.month_end[i]:
            return "fim_do_mes"
        if p.skip_us_holidays and self.holiday[i]:
            return "feriado_eua"
        if self.in_news(when):
            return "noticia"
        s, h, l, c = self.spread[i], self.high[i], self.low[i], self.close[i]
        sell = direction == "venda"
        if sell:
            entry = self._round(l + p.entry_fraction * (h - l))
            stop = self._round(extreme + p.stop_spreads * s)
            if not entry > c:  # a venda limite precisa ficar acima do bid
                return "entrada_ja_passou"
            dist = stop - entry
        else:
            entry = self._round(h - p.entry_fraction * (h - l))
            stop = self._round(extreme - p.stop_spreads * s)
            if not entry < c + s:  # a compra limite precisa ficar abaixo do ask
                return "entrada_ja_passou"
            dist = entry - stop
        if dist < p.min_stop_spreads * s or dist <= 0:
            return "stop_curto"
        opposite = "baixa" if sell else "alta"
        pool = [lv for lv in self.active if lv.side == opposite and lv.state == "pendente"
                and (lv.price < entry if sell else lv.price > entry)]
        if not pool:
            return "sem_alvo"
        goal = max(pool, key=lambda lv: lv.price) if sell else min(pool, key=lambda lv: lv.price)
        rr = (entry - goal.price) / dist if sell else (goal.price - entry) / dist
        if rr < p.min_rr:
            return "rr_baixo"
        poi = self._poi(i, sell, min(lv.first_sweep for lv in armed)) if p.poi != "nenhum" else None
        if p.poi != "nenhum" and poi is None:
            return "sem_poi"
        zone = self.zone(i, entry)
        if p.premium_discount:
            if zone is None:
                return "sem_contexto"
            if abs(zone - 50) <= p.pd_band_pct:
                return "equilibrio"
            if (sell and zone < 50) or (not sell and zone > 50):
                return "area_errada"
        return Signal(
            i=i, time=when, direction=direction, entry=entry, stop=stop, target=goal.price, rr=round(rr, 2),
            spread=float(s), valid_until=valid, levels=tuple(lv.id for lv in armed),
            level_names=tuple(lv.name for lv in armed), target_name=goal.name,
            zone_pct=None if zone is None else round(zone, 1), poi=poi,
        )

    def _poi(self, i: int, sell: bool, first: int) -> str | None:
        """FVG do H1 a favor que a varredura (do candle ``first`` ao ``i``) tocou pela primeira vez: formado antes de
        a varredura começar e intocado até ali. Devolve a zona ou None."""
        want = -1 if sell else 1
        ok = (self.fvg_dir == want) & (self.fvg_from <= first) & (self.fvg_touch >= first) & (self.fvg_touch <= i)
        hits = np.flatnonzero(ok)
        if not len(hits):
            return None
        k = hits[-1]  # o mais recente
        return f"fvg_h1 {self.fvg_bottom[k]:.{self.digits}f}-{self.fvg_top[k]:.{self.digits}f}"
