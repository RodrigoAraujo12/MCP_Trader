"""Cálculo de tamanho de posição e risco (puro, sem MT5)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

_MAX_RISK_PERCENT_WARN = 2.0


@dataclass(frozen=True)
class PositionSize:
    """Resultado do dimensionamento de posição."""

    lots: float
    risk_target: float
    risk_actual: float
    risk_actual_percent: float
    loss_per_lot: float
    warnings: tuple[str, ...]


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


def round_to_step(value: float, step: float) -> float:
    """Arredonda PARA BAIXO ao múltiplo de ``step`` sem erro de ponto flutuante."""
    if step <= 0:
        raise ValueError("O passo de volume (volume_step) deve ser maior que zero.")
    # round(..., 10) absorve ruído de float (ex.: 0.06999999999999999 -> 0.07).
    d_value = _dec(round(float(value), 10))
    d_step = _dec(step)
    multiples = (d_value / d_step).to_integral_value(rounding=ROUND_FLOOR)
    result = multiples * d_step
    return float(result)


def pip_size(point: float, digits: int) -> float:
    """Tamanho do pip: ponto*10 para 3 ou 5 dígitos; senão o próprio ponto."""
    if point <= 0:
        raise ValueError("O valor de 'point' deve ser maior que zero.")
    if digits in (3, 5):
        return float(_dec(point) * 10)
    return float(point)


def loss_per_lot_from_ticks(entry: float, stop: float, tick_size: float, tick_value: float) -> float:
    """Perda (moeda da conta) de 1 lote entre entrada e stop, via tick_size/tick_value."""
    if tick_size <= 0 or tick_value <= 0:
        raise ValueError("tick_size e tick_value devem ser maiores que zero.")
    distance = abs(_dec(entry) - _dec(stop))
    if distance == 0:
        raise ValueError("A entrada e o stop não podem ser iguais.")
    return float(distance / _dec(tick_size) * _dec(tick_value))


def profit_from_ticks(
    side: str, volume: float, price_open: float, price_close: float, tick_size: float, tick_value: float
) -> float:
    """Resultado (moeda da conta) de ``volume`` lotes entre dois preços, via tick_size/tick_value.

    Negativo = perda. ``side`` é "buy" ou "sell".
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side deve ser 'buy' ou 'sell', recebido: {side!r}.")
    if tick_size <= 0 or tick_value <= 0:
        raise ValueError("tick_size e tick_value devem ser maiores que zero.")
    move = _dec(price_close) - _dec(price_open)
    if side == "sell":
        move = -move
    return float(move / _dec(tick_size) * _dec(tick_value) * _dec(volume))


def position_size(
    balance: float,
    risk_percent: float,
    loss_per_lot: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
) -> PositionSize:
    """Calcula o lote para arriscar ``risk_percent`` do saldo, respeitando limites do símbolo."""
    if balance <= 0:
        raise ValueError("O saldo deve ser maior que zero.")
    if risk_percent <= 0 or risk_percent > 100:
        raise ValueError("O risco (%) deve estar entre 0 (exclusivo) e 100.")
    if loss_per_lot <= 0:
        raise ValueError("A perda por lote deve ser maior que zero.")
    if volume_step <= 0:
        raise ValueError("O passo de volume (volume_step) deve ser maior que zero.")
    if volume_min <= 0:
        raise ValueError("O volume mínimo deve ser maior que zero.")
    if volume_max < volume_min:
        raise ValueError("O volume máximo não pode ser menor que o volume mínimo.")

    warnings: list[str] = []
    risk_target = balance * risk_percent / 100
    lots = round_to_step(risk_target / loss_per_lot, volume_step)

    if lots < volume_min:
        min_risk = volume_min * loss_per_lot
        min_pct = min_risk / balance * 100
        warnings.append(
            f"O risco alvo ({risk_target:.2f}) não comporta nem o lote mínimo ({volume_min:g}). "
            f"Operar o lote mínimo arriscaria {min_risk:.2f} ({min_pct:.2f}% do saldo)."
        )
        lots = 0.0
    elif lots > volume_max:
        capped = round_to_step(volume_max, volume_step)
        warnings.append(
            f"O lote calculado ({lots:g}) excede o máximo permitido ({volume_max:g}); "
            f"limitado a {capped:g}."
        )
        lots = capped

    if risk_percent > _MAX_RISK_PERCENT_WARN:
        warnings.append(
            f"Risco de {risk_percent:g}% está acima de 2% por operação (limite prudente comum)."
        )

    risk_actual = lots * loss_per_lot
    return PositionSize(
        lots=lots,
        risk_target=risk_target,
        risk_actual=risk_actual,
        risk_actual_percent=risk_actual / balance * 100,
        loss_per_lot=loss_per_lot,
        warnings=tuple(warnings),
    )
