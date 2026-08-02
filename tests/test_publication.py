"""Change-driven Home Assistant entity publication tests."""

from tests.dependencies import require_test_dependency

require_test_dependency("homeassistant", reason="homeassistant not installed")

from custom_components.intentional import publication  # noqa: E402
from custom_components.intentional.switch import _without_volatile_remaining  # noqa: E402


class _Engine:
    remaining_ms = 5_000

    def list_active_targets(self): return []
    def list_authored_rule_statuses(self):
        return {"adaptive": {"active": True, "hold_after": {"frozen": True, "duration_ms": 10_000, "remaining_ms": self.remaining_ms}}}
    def rule_count(self): return 1
    def active_intent_count(self): return 1
    def is_enabled(self): return True


class _ChangingEngine(_Engine):
    active = True
    targets = ()

    def list_active_targets(self): return self.targets
    def list_authored_rule_statuses(self):
        return {"adaptive": {"active": self.active, "hold_after": {"remaining_ms": self.remaining_ms}}}


class _Store:
    def list_rules(self): return [{"id": "adaptive", "filename": "rules.yaml", "enabled": True}]


def test_remaining_countdown_does_not_publish_each_tick(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(publication, "room_controls_for_engine", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(publication, "async_dispatcher_send", lambda *_args: sent.append(_args))
    engine = _Engine()
    publisher = publication.EntityPublication(object(), "entry", engine, _Store())

    assert publisher.publish_if_changed()
    engine.remaining_ms = 4_900
    assert not publisher.publish_if_changed()
    assert len(sent) == 1


def test_rule_switch_attributes_omit_nested_remaining_countdown() -> None:
    status = {"hold_after": {"duration_ms": 10_000, "remaining_ms": 4_900}}

    assert _without_volatile_remaining(status) == {
        "hold_after": {"duration_ms": 10_000}
    }


def test_shadow_transition_and_churn_publish_once_per_projection(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(publication, "room_controls_for_engine", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(publication, "async_dispatcher_send", lambda *_args: sent.append(_args))
    engine = _ChangingEngine()
    publisher = publication.EntityPublication(object(), "entry", engine, _Store())

    assert publisher.publish_if_changed()
    engine.active = False
    assert publisher.publish_if_changed()
    for remaining in (4_900, 4_800, 4_700):
        engine.remaining_ms = remaining
        assert not publisher.publish_if_changed()
    assert len(sent) == 2


def test_reload_and_restored_projection_do_not_republish(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(publication, "room_controls_for_engine", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(publication, "async_dispatcher_send", lambda *_args: sent.append(_args))
    engine = _ChangingEngine()
    engine.targets = ("light.restored",)
    publisher = publication.EntityPublication(object(), "entry", engine, _Store())

    assert publisher.publish_if_changed()
    # An unchanged reload and lifecycle restore reconstruct equivalent public state.
    assert not publisher.publish_if_changed()
    assert not publisher.publish_if_changed()
    assert len(sent) == 1


def test_projection_reuses_authored_rule_statuses_for_rooms(monkeypatch) -> None:
    engine = _Engine()
    calls = 0
    original = engine.list_authored_rule_statuses

    def counted_statuses():
        nonlocal calls
        calls += 1
        return original()

    engine.list_authored_rule_statuses = counted_statuses
    monkeypatch.setattr(publication, "room_controls_for_engine", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(publication, "async_dispatcher_send", lambda *_args: None)

    publication.EntityPublication(object(), "entry", engine, _Store()).publish_if_changed()

    assert calls == 1
