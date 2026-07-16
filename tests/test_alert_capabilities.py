from __future__ import annotations

import pytest

from intentional.alerting.capabilities import CapabilityRuntime


def test_capability_is_bound_single_use_and_exports_no_raw_token() -> None:
    runtime = CapabilityRuntime(b"s" * 32, id_factory=lambda: "record-1")
    issued = runtime.issue(
        entry_id="entry-1",
        instance_id="instance-1",
        operation="acknowledge",
        destination_id="destination-1",
        expires_at_ms=10_000,
    )

    exported = runtime.export_state()
    assert issued["token"] not in json_text(exported)
    with pytest.raises(ValueError, match="binding mismatch"):
        runtime.consume(
            issued["record_id"],
            issued["token"],
            actor="user-1",
            now_ms=1_000,
            entry_id="entry-1",
            instance_id="other",
            operation="acknowledge",
        )
    consumed = runtime.consume(
        issued["record_id"],
        issued["token"],
        actor="user-1",
        now_ms=1_000,
        entry_id="entry-1",
        instance_id="instance-1",
        operation="acknowledge",
    )
    assert consumed["consumed"] is True
    with pytest.raises(ValueError, match="unavailable"):
        runtime.consume(
            issued["record_id"],
            issued["token"],
            actor="user-1",
            now_ms=1_001,
            entry_id="entry-1",
            instance_id="instance-1",
            operation="acknowledge",
        )


def json_text(value) -> str:
    import json

    return json.dumps(value, sort_keys=True)
