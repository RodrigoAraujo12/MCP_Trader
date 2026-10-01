"""Base de tempo: UTC internamente, exibição em São Paulo e Nova York.

Os epochs do MetaTrader 5 são tratados como UTC. A documentação da biblioteca Python
diz UTC; no fórum da MQL5 há relatos de corretoras cujo epoch é o horário do servidor
gravado como se fosse UTC. No servidor da Exness os dois coincidem: em 2026-10-01 a
diferença entre o horário dos ticks e o relógio UTC foi de 0 a 1 s
(docs/diagnostico-mt5-2026-10-01.md). Uma cotação "do futuro" indica que essa premissa
falhou; ver ``MT5Client.quote``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
NOVA_YORK = ZoneInfo("America/New_York")

_TF_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 7 * 86400,
}


def from_epoch(epoch: float) -> datetime:
    """Epoch do MT5 (segundos) -> datetime UTC com fuso."""
    return datetime.fromtimestamp(float(epoch), tz=UTC)


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def _offset(moment: datetime, tz: ZoneInfo) -> str:
    minutes = int(moment.astimezone(tz).utcoffset().total_seconds()) // 60  # type: ignore[union-attr]
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


def exibicao(moment: datetime) -> dict[str, str]:
    """O mesmo instante em UTC, São Paulo e Nova York (com horário de verão dos EUA)."""
    return {"utc": iso_utc(moment), "sao_paulo": _local(moment, SAO_PAULO), "nova_york": _local(moment, NOVA_YORK)}


def offsets(moment: datetime) -> dict[str, str]:
    """Diferença para o UTC em São Paulo e Nova York, válida no instante informado."""
    return {"sao_paulo": _offset(moment, SAO_PAULO), "nova_york": _offset(moment, NOVA_YORK)}


def bar_end(open_time: datetime, timeframe: str) -> datetime:
    """Fim (exclusivo) de um candle a partir da abertura."""
    if timeframe == "MN1":
        o = open_time.astimezone(UTC)
        year, month = (o.year + 1, 1) if o.month == 12 else (o.year, o.month + 1)
        return o.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    return open_time + timedelta(seconds=_TF_SECONDS[timeframe])


def bar_in_progress(open_time: datetime, timeframe: str, now: datetime) -> bool:
    """True se o candle ainda não fechou no instante ``now``."""
    return bar_end(open_time, timeframe) > now


def describe_age(seconds: float) -> str:
    """Duração legível: '45 s', '12 min', '3 h 05 min', '2 dias'."""
    s = abs(float(seconds))
    if s < 90:
        return f"{s:.0f} s"
    if s < 3600:
        return f"{s / 60:.0f} min"
    if s < 2 * 86400:
        hours, rest = divmod(int(s) // 60, 60)
        return f"{hours} h {rest:02d} min"
    return f"{s / 86400:.0f} dias"
