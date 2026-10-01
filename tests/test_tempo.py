from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_mcp import tempo

UTC = timezone.utc


def test_exibicao_us_daylight_time():
    # 8:30 em Nova York (EDT) = 12:30 UTC = 9:30 em São Paulo
    assert tempo.exibicao(datetime(2026, 10, 1, 12, 30, tzinfo=UTC)) == {
        "utc": "2026-10-01T12:30:00Z",
        "sao_paulo": "2026-10-01 09:30:00",
        "nova_york": "2026-10-01 08:30:00",
    }


def test_exibicao_us_standard_time_after_november_change():
    # Depois de 1/11/2026 (fim do horário de verão dos EUA), 8:30 em Nova York = 13:30 UTC = 10:30 em SP
    assert tempo.exibicao(datetime(2026, 11, 5, 13, 30, tzinfo=UTC)) == {
        "utc": "2026-11-05T13:30:00Z",
        "sao_paulo": "2026-11-05 10:30:00",
        "nova_york": "2026-11-05 08:30:00",
    }


def test_offsets():
    assert tempo.offsets(datetime(2026, 10, 1, tzinfo=UTC)) == {"sao_paulo": "-03:00", "nova_york": "-04:00"}
    assert tempo.offsets(datetime(2026, 12, 1, tzinfo=UTC)) == {"sao_paulo": "-03:00", "nova_york": "-05:00"}


def test_from_epoch_is_utc():
    assert tempo.iso_utc(tempo.from_epoch(1_700_000_000)) == "2023-11-14T22:13:20Z"


@pytest.mark.parametrize(
    "opened,tf,end",
    [
        (datetime(2026, 9, 30, 12, 0, tzinfo=UTC), "M1", datetime(2026, 9, 30, 12, 1, tzinfo=UTC)),
        (datetime(2026, 9, 30, 20, 0, tzinfo=UTC), "H4", datetime(2026, 10, 1, 0, 0, tzinfo=UTC)),
        (datetime(2026, 9, 27, 0, 0, tzinfo=UTC), "W1", datetime(2026, 10, 4, 0, 0, tzinfo=UTC)),
        (datetime(2026, 12, 1, 0, 0, tzinfo=UTC), "MN1", datetime(2027, 1, 1, 0, 0, tzinfo=UTC)),
        (datetime(2026, 1, 1, 0, 0, tzinfo=UTC), "MN1", datetime(2026, 2, 1, 0, 0, tzinfo=UTC)),
    ],
)
def test_bar_end(opened, tf, end):
    assert tempo.bar_end(opened, tf) == end


def test_bar_in_progress_boundary():
    opened = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    assert tempo.bar_in_progress(opened, "H1", datetime(2026, 9, 30, 12, 59, 59, tzinfo=UTC)) is True
    assert tempo.bar_in_progress(opened, "H1", datetime(2026, 9, 30, 13, 0, tzinfo=UTC)) is False


@pytest.mark.parametrize(
    "seconds,text", [(45, "45 s"), (-45, "45 s"), (720, "12 min"), (4020, "1 h 07 min"), (3 * 86400, "3 dias")]
)
def test_describe_age(seconds, text):
    assert tempo.describe_age(seconds) == text
