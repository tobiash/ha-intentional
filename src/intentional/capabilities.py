"""Home Assistant capability policy for durable intents and effects."""

from __future__ import annotations

ACTION_LIKE_INTENT_FIELDS = frozenset({
    "command",
    "media_action",
    "camera_action",
    "todo_action",
    "update_action",
})

EFFECT_ONLY_DOMAINS = frozenset({"button", "input_button"})


def vnext_intent_policy_error(target: str, field: str, value: object) -> str | None:
    """Return a validation message when a VNext intent field is not durable."""
    if field == "state" and value == "toggle":
        return "VNext `intent` cannot use state: toggle; use `effect` instead"
    if field in ACTION_LIKE_INTENT_FIELDS:
        return f"VNext `intent` field {field!r} is action-like; use `effect` instead"
    domain, _sep, _object_id = target.partition(".")
    if domain in EFFECT_ONLY_DOMAINS:
        return f"VNext `{domain}` targets are effects only"
    return None
