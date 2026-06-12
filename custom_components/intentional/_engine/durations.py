"""Duration parsing for authored rules and runtime policies."""

from __future__ import annotations

import re

_DURATION_RE = re.compile(
    r"^\s*"
    r"(?:(?P<hours>\d+(?:\.\d+)?)h)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)m(?!s))?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)s)?"
    r"(?:(?P<millis>\d+)ms)?"
    r"\s*$"
)


def parse_duration(s: str) -> int:
    """Parse a duration string like '1h30m' or '500ms' into milliseconds."""
    if not isinstance(s, str):
        raise ValueError(f"Duration must be a string, got {type(s).__name__}")
    match = _DURATION_RE.match(s)
    if not match or not any(match.groupdict().values()):
        raise ValueError(
            f"Invalid duration {s!r}. Use forms like '500ms', '2s', '5m', '1h', '1h30m15s'."
        )
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    millis = int(match.group("millis") or 0)
    total = int(hours * 3_600_000 + minutes * 60_000 + seconds * 1000 + millis)
    if total < 0:
        raise ValueError(f"Duration must be non-negative, got {s!r}")
    return total
