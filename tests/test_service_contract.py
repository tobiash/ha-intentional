"""Contract tests for the public manual intent service surface."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import yaml

from intentional.ha_adapter import MANUAL_SET_FIELDS
from intentional.yaml_loader import load_rules_from_string

REPO_ROOT = Path(__file__).parent.parent
SRC_ADAPTER = REPO_ROOT / "src" / "intentional" / "ha_adapter.py"
BUNDLED_ADAPTER = (
    REPO_ROOT
    / "custom_components"
    / "intentional"
    / "_engine"
    / "ha_adapter.py"
)
INTEGRATION_INIT = REPO_ROOT / "custom_components" / "intentional" / "__init__.py"
SERVICES_YAML = REPO_ROOT / "custom_components" / "intentional" / "services.yaml"
TRANSLATIONS_JSON = (
    REPO_ROOT
    / "custom_components"
    / "intentional"
    / "translations"
    / "en.json"
)
RULES_DOC = REPO_ROOT / "docs" / "rules.md"


def test_manual_set_fields_match_bundled_adapter() -> None:
    """The bundled HA copy must expose the same manual intent fields."""
    assert _manual_set_fields_from_adapter(BUNDLED_ADAPTER) == set(MANUAL_SET_FIELDS)
    assert _manual_set_fields_from_adapter(SRC_ADAPTER) == set(MANUAL_SET_FIELDS)


def test_manual_set_fields_are_accepted_by_fire_service_schema() -> None:
    """Every adapter-supported manual field must be accepted by intentional.fire."""
    schema_fields = _fire_service_schema_optional_fields(INTEGRATION_INIT)

    assert set(MANUAL_SET_FIELDS) <= schema_fields
    assert "ttl" in schema_fields


def test_manual_set_fields_are_visible_in_service_metadata() -> None:
    """The HA Services UI metadata must not lag behind adapter support."""
    services = yaml.safe_load(SERVICES_YAML.read_text())
    fire_fields = set(services["fire"]["fields"])

    assert set(MANUAL_SET_FIELDS) <= fire_fields
    assert {"target", "ttl"} <= fire_fields


def test_manual_set_fields_are_visible_in_translations() -> None:
    """Translated service metadata must expose every manual field."""
    translations = json.loads(TRANSLATIONS_JSON.read_text())
    fire_fields = set(translations["services"]["fire"]["fields"])

    assert set(MANUAL_SET_FIELDS) <= fire_fields
    assert {"target", "ttl"} <= fire_fields


def test_manual_set_fields_are_documented() -> None:
    """The documented intentional.fire field list must stay complete."""
    text = RULES_DOC.read_text()
    start = text.index("`intentional.fire` accepts")
    end = text.index("For example", start)
    fire_field_section = text[start:end]

    missing = [
        field
        for field in MANUAL_SET_FIELDS
        if f"`{field}`" not in fire_field_section
    ]
    assert missing == []


def test_manual_set_fields_can_be_loaded_from_yaml_rules() -> None:
    """Rule YAML must accept the same target fields as intentional.fire."""
    set_payload = _representative_manual_set_payload()
    yaml_text = yaml.safe_dump([
        {
            "id": "all-manual-fields",
            "when": "input_boolean.test == 'on'",
            "emit": {
                "target": "media_player.kitchen",
                "set": set_payload,
            },
        }
    ])

    rules = load_rules_from_string(yaml_text)

    assert len(rules) == 1
    assert rules[0].set == set_payload
    assert set(rules[0].set) == set(MANUAL_SET_FIELDS)


def _manual_set_fields_from_adapter(path: Path) -> set[str]:
    module = ast.parse(path.read_text())
    for node in module.body:
        if isinstance(node, ast.Assign):
            names = {
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            if "MANUAL_SET_FIELDS" in names:
                return {
                    item.value
                    for item in node.value.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
    raise AssertionError(f"MANUAL_SET_FIELDS not found in {path}")


def _fire_service_schema_optional_fields(path: Path) -> set[str]:
    module = ast.parse(path.read_text())
    for node in module.body:
        if isinstance(node, ast.Assign):
            names = {
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            if "FIRE_SERVICE_SCHEMA" in names:
                return _vol_optional_fields(node.value)
    raise AssertionError(f"FIRE_SERVICE_SCHEMA not found in {path}")


def _vol_optional_fields(node: ast.AST) -> set[str]:
    fields: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if not isinstance(child.func, ast.Attribute):
            continue
        if child.func.attr != "Optional":
            continue
        if not child.args:
            continue
        first_arg = child.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            fields.add(first_arg.value)
    return fields


def _representative_manual_set_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "state": "on",
        "brightness_pct": 70,
        "brightness": 180,
        "color_temp_k": 2700,
        "color_temp_mired": 370,
        "rgb_color": [255, 80, 40],
        "rgbw_color": [255, 80, 40, 10],
        "rgbww_color": [255, 80, 40, 10, 5],
        "hs_color": [24.0, 90.0],
        "xy_color": [0.45, 0.36],
        "effect": "colorloop",
        "flash": "short",
        "volume_level": 0.35,
        "is_volume_muted": False,
        "tone": "alarm",
        "source": "HDMI 2",
        "sound_mode": "Movie",
        "media_action": "play_media",
        "media_content_id": "media-source://album/1",
        "media_content_type": "music",
        "enqueue": "play",
        "announce": True,
        "extra": {"metadata": {"title": "Dinner"}},
        "shuffle": True,
        "repeat": "all",
        "seek_position": 42.5,
        "group_members": ["media_player.dining_room"],
        "position": 25,
        "tilt_position": 75,
        "percentage": 40,
        "hvac_mode": "heat",
        "temperature": 21.5,
        "target_temp_low": 18,
        "target_temp_high": 23,
        "preset_mode": "eco",
        "fan_mode": "auto",
        "direction": "forward",
        "oscillating": True,
        "humidity": 55,
        "swing_mode": "vertical",
        "swing_horizontal_mode": "wide",
        "aux_heat": True,
        "mode": "sleep",
        "operation_mode": "eco",
        "away_mode": True,
        "fan_speed": "turbo",
        "camera_action": "snapshot",
        "filename": "/tmp/doorbell.jpg",
        "media_player": "media_player.office",
        "format": "hls",
        "lookback": 5,
        "command": "clean_segments",
        "params": {"segments": [1, 2]},
        "cleaning_area_id": ["kitchen"],
        "activity": "Watch TV",
        "device": "Android TV",
        "num_repeats": 2,
        "delay_secs": 0.4,
        "hold_secs": 0.1,
        "value": 42,
        "option": "Guest",
        "cycle": False,
        "code": "1234",
        "message": "Front door opened",
        "title": "Security",
        "data": {"tag": "front-door"},
        "service": "notification",
        "service_data": {"browser_id": ["office-dashboard"]},
        "media_player_entity_id": "media_player.office",
        "cache": True,
        "language": "de",
        "options": {"voice": "default"},
        "browser_id": ["office-dashboard"],
        "user_id": ["person.tobias"],
        "path": "/lovelace/office",
        "action_text": "Open camera",
        "action": {"action": "navigate", "navigation_path": "/lovelace/cameras"},
        "parse_mode": "html",
        "disable_notification": False,
        "disable_web_page_preview": True,
        "keyboard": ["/ack"],
        "inline_keyboard": [["Acknowledge:/ack"]],
        "message_tag": "front-door",
        "chat_id": "12345",
        "todo_action": "add_item",
        "item": "Buy filters",
        "rename": "Buy HVAC filters",
        "status": "completed",
        "due_date": "2026-06-06",
        "due_datetime": "2026-06-06 10:00:00",
        "description": "For the office purifier",
        "variables": {"mode": "movie"},
        "skip_condition": False,
        "datetime": "2026-06-05 22:30:00",
        "date": "2026-06-05",
        "time": "22:30:00",
        "timestamp": 1780691400,
        "duration": "00:10:00",
        "update_action": "install",
        "version": "1.0.0",
        "backup": True,
        "update_entity": True,
    }
    missing = set(MANUAL_SET_FIELDS) - set(payload)
    extra = set(payload) - set(MANUAL_SET_FIELDS)
    if missing or extra:
        raise AssertionError(f"payload drift: missing={sorted(missing)}, extra={sorted(extra)}")
    return payload
