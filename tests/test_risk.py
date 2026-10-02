"""Testes do dimensionamento de posição."""
from __future__ import annotations

import pytest

from trading_mcp.risk import (
    PositionSize,
    loss_per_lot_from_ticks,
    pip_size,
    position_size,
    profit_from_ticks,
    round_to_step,
)


@pytest.mark.parametrize(
    "value,step,expected",
    [
        (0.07, 0.01, 0.07),
        (0.06999999999999999, 0.01, 0.07),
        (0.0799999, 0.01, 0.07),
        (0.29, 0.01, 0.29),
        (1.15, 0.01, 1.15),
        (0.57, 0.01, 0.57),
        (0.19, 0.1, 0.1),
        (0.3, 0.1, 0.3),
        (0.7, 0.1, 0.7),
        (2.9, 1, 2.0),
        (3.0, 1, 3.0),
        (0.5, 1, 0.0),
        (0.004, 0.01, 0.0),
        (1.237, 0.05, 1.2),
        (0.0, 0.01, 0.0),
    ],
)
def test_round_to_step(value, step, expected):
    assert round_to_step(value, step) == expected


def test_round_to_step_invalid_step():
    with pytest.raises(ValueError):
        round_to_step(1.0, 0)


def test_round_to_step_no_float_garbage():
    assert str(round_to_step(0.07, 0.01)) == "0.07"
    assert str(round_to_step(0.29999, 0.1)) == "0.2"


def test_position_size_basic():
    # saldo 10000, 1% = 100; perda/lote 200 -> 0.5 lote
    r = position_size(10_000, 1, 200, 0.01, 100, 0.01)
    assert isinstance(r, PositionSize)
    assert r.lots == 0.5
    assert r.risk_target == pytest.approx(100)
    assert r.risk_actual == pytest.approx(100)
    assert r.risk_actual_percent == pytest.approx(1.0)
    assert r.loss_per_lot == 200
    assert r.warnings == ()


def test_position_size_rounds_down_and_reduces_risk():
    # 100 / 30 = 3.333 -> 3.33 lotes
    r = position_size(10_000, 1, 30, 0.01, 100, 0.01)
    assert r.lots == 3.33
    assert r.risk_actual == pytest.approx(99.9)
    assert r.risk_actual < r.risk_target


def test_position_size_decimal_safe_007():
    r = position_size(700, 1, 100, 0.01, 10, 0.01)  # 7 / 100
    assert r.lots == 0.07
    # 1000 * 0.7 / 100 = 7.000000000000001 em float; ainda deve dar 0.07
    r2 = position_size(1000, 0.7, 100, 0.01, 10, 0.01)
    assert r2.lots == 0.07


def test_position_size_step_variants():
    assert position_size(10_000, 1, 33, 0.1, 50, 0.1).lots == 3.0  # 3.03 -> 3.0
    assert position_size(10_000, 1, 33, 1, 50, 1).lots == 3.0
    assert position_size(10_000, 1, 7, 1, 50, 1).lots == 14.0


def test_position_size_below_min_returns_zero_with_warning():
    r = position_size(1_000, 1, 500, 0.01, 100, 0.01)  # alvo 10 / 500 = 0.02
    assert r.lots == 0.02
    r = position_size(100, 0.5, 1000, 0.01, 100, 0.01)  # alvo 0.5 / 1000 = 0.0005
    assert r.lots == 0.0
    assert r.risk_actual == 0.0 and r.risk_actual_percent == 0.0
    assert len(r.warnings) == 1
    w = r.warnings[0]
    assert "mínimo" in w and "0.01" in w and "10.00" in w and "10.00%" in w


def test_position_size_cap_at_max():
    r = position_size(1_000_000, 1, 10, 0.01, 50, 0.01)  # 1000 lotes -> 50
    assert r.lots == 50
    assert r.risk_actual == pytest.approx(500)
    assert any("máximo" in w for w in r.warnings)


def test_position_size_warns_above_2_percent():
    r = position_size(10_000, 3, 100, 0.01, 100, 0.01)
    assert r.lots == 3.0
    assert any("2%" in w for w in r.warnings)
    assert position_size(10_000, 2, 100, 0.01, 100, 0.01).warnings == ()


def test_position_size_min_lot_above_two_percent_both_warnings():
    r = position_size(100, 3, 1000, 0.01, 100, 0.01)  # alvo 3 / 1000 -> 0.003
    assert r.lots == 0.0
    assert len(r.warnings) == 2


@pytest.mark.parametrize(
    "args",
    [
        (0, 1, 100, 0.01, 100, 0.01),
        (-5, 1, 100, 0.01, 100, 0.01),
        (1000, 0, 100, 0.01, 100, 0.01),
        (1000, -1, 100, 0.01, 100, 0.01),
        (1000, 100.5, 100, 0.01, 100, 0.01),
        (1000, 1, 0, 0.01, 100, 0.01),
        (1000, 1, -3, 0.01, 100, 0.01),
        (1000, 1, 100, 0.01, 100, 0),
        (1000, 1, 100, 0, 100, 0.01),
        (1000, 1, 100, 1.0, 0.5, 0.01),
    ],
)
def test_position_size_invalid(args):
    with pytest.raises(ValueError):
        position_size(*args)


def test_position_size_risk_100_percent_allowed():
    assert position_size(1000, 100, 1000, 0.01, 100, 0.01).lots == 1.0


def test_position_size_is_frozen():
    r = position_size(10_000, 1, 200, 0.01, 100, 0.01)
    with pytest.raises(Exception):
        r.lots = 9  # type: ignore[misc]


def test_loss_per_lot_eurusd_20_pips():
    # 5 dígitos, tick 0.00001, valor do tick 1 USD: 20 pips = 200 ticks -> 200 USD
    assert loss_per_lot_from_ticks(1.10000, 1.09800, 0.00001, 1.0) == pytest.approx(200.0, abs=1e-9)
    assert loss_per_lot_from_ticks(1.09800, 1.10000, 0.00001, 1.0) == pytest.approx(200.0, abs=1e-9)


def test_loss_per_lot_eurusd_no_float_noise():
    assert loss_per_lot_from_ticks(1.1, 1.098, 0.00001, 1.0) == 200.0


def test_loss_per_lot_usdjpy_like():
    # 3 dígitos, tick 0.001, valor do tick ~0.6667 USD; 20 pips = 0.20 = 200 ticks
    assert loss_per_lot_from_ticks(150.000, 149.800, 0.001, 0.6667) == pytest.approx(133.34, abs=1e-9)


def test_loss_per_lot_invalid():
    with pytest.raises(ValueError):
        loss_per_lot_from_ticks(1.1, 1.1, 0.00001, 1.0)
    with pytest.raises(ValueError):
        loss_per_lot_from_ticks(1.1, 1.0, 0, 1.0)
    with pytest.raises(ValueError):
        loss_per_lot_from_ticks(1.1, 1.0, 0.00001, 0)


@pytest.mark.parametrize(
    "point,digits,expected",
    [
        (0.00001, 5, 0.0001),
        (0.001, 3, 0.01),
        (0.01, 2, 0.01),
        (0.0001, 4, 0.0001),
        (0.1, 1, 0.1),
    ],
)
def test_pip_size(point, digits, expected):
    assert pip_size(point, digits) == expected


def test_end_to_end_eurusd_20_pips():
    loss = loss_per_lot_from_ticks(1.10000, 1.09800, 0.00001, 1.0)
    r = position_size(5_000, 1, loss, 0.01, 100, 0.01)  # alvo 50 / 200 = 0.25
    assert r.lots == 0.25
    assert r.risk_actual == pytest.approx(50)


@pytest.mark.parametrize(
    "side,close,expected",
    [("buy", 1.09800, -20.0), ("buy", 1.10200, 20.0), ("sell", 1.09800, 20.0), ("sell", 1.10200, -20.0)],
)
def test_profit_from_ticks_is_signed(side, close, expected):
    # 200 pontos x 1 USD x 0,1 lote.
    assert profit_from_ticks(side, 0.1, 1.10000, close, 0.00001, 1.0) == pytest.approx(expected)


def test_profit_from_ticks_rejects_bad_input():
    with pytest.raises(ValueError):
        profit_from_ticks("compra", 0.1, 1.1, 1.2, 0.00001, 1.0)
    with pytest.raises(ValueError):
        profit_from_ticks("buy", 0.1, 1.1, 1.2, 0.0, 1.0)
