"""Home Assistant timezone integration tests."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tests.dependencies import require_test_dependency

require_test_dependency("homeassistant", reason="homeassistant not installed")

from custom_components.intentional._engine.engine import Engine  # noqa: E402
from custom_components.intentional._engine.ha_adapter import (  # noqa: E402
    sync_time_context_into_engine,
)


@pytest.mark.parametrize(
    ("instant", "clock"),
    [
        (datetime(2026, 1, 15, 6, 30, tzinfo=UTC), "07:30"),
        (datetime(2026, 7, 15, 6, 30, tzinfo=UTC), "08:30"),
    ],
)
def test_time_context_uses_ha_local_time_across_dst(instant, clock) -> None:
    from homeassistant.util import dt as dt_util

    engine = Engine()
    original_timezone = dt_util.DEFAULT_TIME_ZONE
    try:
        dt_util.set_default_time_zone(ZoneInfo("Europe/Berlin"))
        sync_time_context_into_engine(engine, dt_util.as_local(instant))
    finally:
        dt_util.set_default_time_zone(original_timezone)

    assert engine._time_of_day.clock == clock
    assert engine._time_of_day.bucket == "morning"
