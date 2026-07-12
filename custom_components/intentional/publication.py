"""Change-driven publication of Intentional entity state."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN
from .room_controls import area_for_target, room_controls_for_engine
from .rule_store import StorageRuleStore


def publication_key(entry_id: str) -> str:
    """Return the hass.data key for an entry's publisher."""
    return f"{entry_id}:publication"


def publication_signal(entry_id: str) -> str:
    """Return the private dispatcher signal for an entry."""
    return f"{DOMAIN}_{entry_id}_publication"


class EntityPublication:
    """Publish entity updates only when their canonical projection changes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        engine: Any,
        rule_store: StorageRuleStore,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._engine = engine
        self._rule_store = rule_store
        self._projection: dict[str, object] | None = None
        self._paused = False

    def pause(self) -> None:
        """Suppress publication while platforms are being unloaded."""
        self._paused = True

    def resume(self) -> None:
        """Resume publication after a failed platform unload."""
        self._paused = False

    def publish_if_changed(self) -> bool:
        """Notify platforms when the public entity projection changed."""
        if self._paused:
            return False
        projection = self._build_projection()
        previous = self._projection
        if projection == previous:
            return False
        self._projection = projection
        changed = frozenset(projection) if previous is None else frozenset(
            key
            for key in projection.keys() | previous.keys()
            if projection.get(key) != previous.get(key)
        )
        async_dispatcher_send(
            self._hass, publication_signal(self._entry_id), changed
        )
        return True

    def _build_projection(self) -> dict[str, object]:
        active_targets = tuple(self._engine.list_active_targets())
        statuses = (
            self._engine.list_authored_rule_statuses()
            if hasattr(self._engine, "list_authored_rule_statuses")
            else self._engine.list_rule_statuses()
        )
        rooms = room_controls_for_engine(
            self._engine,
            lambda target: area_for_target(self._hass, target),
        )
        projection: dict[str, object] = {
            "summary": (
                self._engine.rule_count(),
                self._engine.active_intent_count(),
                active_targets,
            ),
            "global": self._engine.is_enabled(),
        }
        infos = {str(info["id"]): info for info in self._rule_store.list_rules()}
        for rule_id, status in statuses.items():
            projection[f"rule:{rule_id}"] = (
                str(infos.get(rule_id, {}).get("filename", "")),
                bool(infos.get(rule_id, {}).get("enabled", True)),
                tuple(
                    sorted(
                        (key, _freeze(value))
                        for key, value in status.items()
                        if key
                        not in {
                            "active_for_ms",
                            "condition_active_for_ms",
                            "held_for_ms",
                            "for_remaining_ms",
                        }
                    )
                ),
            )
        for rule_id, info in infos.items():
            projection.setdefault(
                f"rule:{rule_id}",
                (str(info.get("filename", "")), bool(info.get("enabled", True)), ()),
            )
        for area_id, control in rooms.items():
            projection[f"room:{area_id}"] = (
                control.name,
                control.paused,
                tuple(sorted(control.rule_ids)),
                tuple(sorted(control.active_rule_ids)),
                tuple(sorted(control.paused_rule_ids)),
                tuple(sorted(control.targets)),
                tuple(sorted(control.manual_override_targets)),
                control.active_intent_count,
            )
        return projection


def _freeze(value: Any) -> Any:
    """Convert status payload values to deterministic comparable values."""
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(_freeze(item) for item in value))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value
