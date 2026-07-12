"""Pure, read-only conversion of a strict HA automation subset to Rules."""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from typing import Any

import yaml

MAX_SOURCE_BYTES = 256_000
MAX_TRIGGERS = 64
MAX_ACTIONS = 64
MAX_PROPOSAL_BYTES = 256_000
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_REJECTED_ACTION_KEYS = {"delay", "choose", "wait_for_trigger", "wait_template", "scene", "event"}
_LIGHT_DATA_FIELDS = {
    "brightness", "brightness_pct", "color_name", "color_temp", "color_temp_kelvin",
    "color_temp_k", "effect", "hs_color", "kelvin", "rgb_color", "rgbw_color",
    "rgbww_color", "white", "xy_color",
}


def source_fingerprint(config: dict[str, Any]) -> str:
    """Return a stable fingerprint without retaining or exposing source data."""
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def convert_automation(config: dict[str, Any], *, source_entity_id: str) -> dict[str, Any]:
    """Propose Intentional YAML without mutating the supplied automation."""
    source = deepcopy(config)
    diagnostics: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    try:
        _check_source(source)
        fingerprint = source_fingerprint(source)
        triggers = _as_list(source.get("trigger", source.get("triggers")))
        if len(triggers) > MAX_TRIGGERS:
            raise ValueError(f"Automation exceeds {MAX_TRIGGERS} triggers")
        actions = _convert_actions(source.get("action", source.get("actions")))
        if not triggers:
            raise ValueError("Automation has no triggers")
        base_id = _slug(source.get("id") or source.get("alias") or source_entity_id)
        for index, trigger in enumerate(triggers, 1):
            condition, after, initial, active = _convert_trigger(trigger)
            rule: dict[str, Any] = {
                "id": base_id if len(triggers) == 1 else f"{base_id}-trigger-{index}",
                "reason": f"Migrated proposal from {source_entity_id}; source remains unchanged",
                "while": condition,
            }
            if after is not None:
                rule["after"] = after
            rule["intent"] = actions
            rules.append(rule)
            timeline.extend(({"states": initial}, {"states": active}))
        diagnostics.extend((
            _diag("edge_to_level", "warning", "HA triggers are edges; the proposed Rule uses a durable level situation."),
            _diag("intent_withdrawal", "warning", "When the situation stops matching, the Intent withdraws; the source automation does not undo its action."),
        ))
    except (TypeError, ValueError) as err:
        diagnostics.append(_diag("unsupported", "error", str(err)))

    fingerprint = locals().get("fingerprint", "sha256:invalid")

    supported = bool(rules) and not any(item["severity"] == "error" for item in diagnostics)
    yaml_text = yaml.safe_dump(rules, sort_keys=False, allow_unicode=False) if supported else ""
    if len(yaml_text.encode()) > MAX_PROPOSAL_BYTES:
        diagnostics.append(_diag("unsupported", "error", f"Proposal exceeds {MAX_PROPOSAL_BYTES} bytes"))
        supported, yaml_text, rules, timeline = False, "", [], []
    return {
        "source_entity_id": source_entity_id,
        "source_fingerprint": fingerprint,
        "source_mutated": False,
        "supported": supported,
        "diagnostics": diagnostics,
        "yaml": yaml_text,
        "rule_count": len(rules) if supported else 0,
        "starter_timeline": timeline if supported else [],
    }


def _check_source(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise TypeError("Automation config must be a mapping")
    if _contains_non_finite(config):
        raise ValueError("Non-finite numeric values are not supported")
    if len(json.dumps(config, default=str, allow_nan=False).encode()) > MAX_SOURCE_BYTES:
        raise ValueError(f"Automation config exceeds {MAX_SOURCE_BYTES} bytes")
    if config.get("condition") or config.get("conditions"):
        raise ValueError("Conditions are not supported")
    if "use_blueprint" in config:
        raise ValueError("Blueprint automations are not supported")
    if _contains_template(config):
        raise ValueError("Templates are not supported")


def _convert_trigger(trigger: Any) -> tuple[dict[str, Any], str | None, dict[str, Any], dict[str, Any]]:
    if not isinstance(trigger, dict):
        raise ValueError("Each trigger must be a mapping")
    platform = trigger.get("platform", trigger.get("trigger"))
    if "attribute" in trigger:
        raise ValueError("Attribute state and numeric_state triggers are not supported")
    entities = _entity_ids(trigger.get("entity_id"))
    if len(entities) != 1:
        raise ValueError("Triggers require exactly one explicit entity_id")
    entity = entities[0]
    after = _duration(trigger.get("for"))
    if platform == "state":
        if "from" in trigger:
            raise ValueError("State triggers with `from` are not supported")
        if "to" not in trigger or trigger["to"] is None or isinstance(trigger["to"], (dict, list)):
            raise ValueError("State triggers require an explicit literal `to`")
        value = trigger["to"]
        initial = trigger.get("from", "__not_" + str(value))
        return {entity: value}, after, {f"{entity}.state": initial}, {f"{entity}.state": value}
    if platform == "numeric_state":
        above, below = trigger.get("above"), trigger.get("below")
        if above is None and below is None:
            raise ValueError("numeric_state requires literal above and/or below")
        if any(not _literal_number(value) for value in (above, below) if value is not None):
            raise ValueError("numeric_state thresholds must be literal numbers")
        if above is not None and below is not None and float(above) >= float(below):
            raise ValueError("numeric_state above must be lower than below")
        comparison = {}
        if above is not None:
            comparison["gt"] = above
        if below is not None:
            comparison["lt"] = below
        active = (float(above) + float(below)) / 2 if above is not None and below is not None else (float(above) + 1 if above is not None else float(below) - 1)
        inactive = float(above) if above is not None else float(below)
        return {entity: comparison}, after, {f"{entity}.state": inactive}, {f"{entity}.state": active}
    raise ValueError(f"Trigger platform {platform!r} is not supported")


def _convert_actions(value: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    actions = _as_list(value)
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"Automation exceeds {MAX_ACTIONS} actions")
    for action in actions:
        if not isinstance(action, dict) or set(action) & _REJECTED_ACTION_KEYS:
            raise ValueError("Only flat light/switch turn_on/turn_off service actions are supported")
        service = action.get("service", action.get("action"))
        if not isinstance(service, str) or service not in {"light.turn_on", "light.turn_off", "switch.turn_on", "switch.turn_off"}:
            raise ValueError(f"Service action {service!r} is not supported")
        domain, service_name = service.split(".", 1)
        target = action.get("target", {})
        entity_value = target.get("entity_id") if isinstance(target, dict) else None
        if entity_value is None:
            entity_value = action.get("entity_id")
        entities = _entity_ids(entity_value)
        if not entities:
            raise ValueError("Actions require explicit entity_id targets")
        data = action.get("data", {}) or {}
        if not isinstance(data, dict) or any(key in data for key in ("entity_id", "device_id", "area_id")):
            raise ValueError("Dynamic or indirect action targets are not supported")
        allowed = _LIGHT_DATA_FIELDS if domain == "light" and service_name == "turn_on" else set()
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"Service {service!r} has non-durable or unknown data fields: {sorted(unknown)}")
        if _contains_secret(data) or any(not _durable_literal(item) for item in data.values()):
            raise ValueError("Action data must contain only non-secret literal values")
        desired = {"state": "on" if service_name == "turn_on" else "off", **data}
        for entity in entities:
            if not entity.startswith(domain + "."):
                raise ValueError(f"Target {entity!r} does not match service domain {domain!r}")
            if entity in result and result[entity] != desired:
                raise ValueError(f"Conflicting actions for target {entity}")
            result[entity] = desired
    if not result:
        raise ValueError("Automation has no supported actions")
    return {entity: result[entity] for entity in sorted(result)}


def _entity_ids(value: Any) -> list[str]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    if not values or not all(isinstance(item, str) and _ENTITY_ID.fullmatch(item) for item in values):
        return []
    return sorted(set(values))


def _duration(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return f"{value:g}s"
    if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?(?:ms|s|m|h)", value.strip()):
        return value.strip()
    if (
        isinstance(value, dict)
        and set(value) <= {"hours", "minutes", "seconds", "milliseconds"}
        and value
        and all(_literal_number(item) and item >= 0 for item in value.values())
    ):
        milliseconds = sum(float(value.get(key, 0)) * factor for key, factor in (("hours", 3_600_000), ("minutes", 60_000), ("seconds", 1_000), ("milliseconds", 1)))
        return f"{milliseconds:g}ms"
    raise ValueError("`for` must be a fixed literal duration")


def _contains_template(value: Any) -> bool:
    if isinstance(value, str):
        return "{{" in value or "{%" in value
    if isinstance(value, dict):
        return any(_contains_template(key) or _contains_template(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_template(item) for item in value)
    return False


def _contains_secret(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return "!secret" in lowered or "secret" in lowered or "token" in lowered or "password" in lowered
    if isinstance(value, dict):
        return any(_contains_secret(key) or _contains_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def _durable_literal(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int, float)):
        return True
    return isinstance(value, list) and 1 <= len(value) <= 5 and all(_literal_number(item) for item in value)


def _literal_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) or (
        isinstance(value, float) and math.isfinite(value)
    )


def _contains_non_finite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_non_finite(key) or _contains_non_finite(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite(item) for item in value)
    return False


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else ([] if value is None else [value])


def _slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return ("migrate-" + slug if slug else "migrate-ha-automation")[:120]


def _diag(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}
