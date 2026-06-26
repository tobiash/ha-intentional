"""Shared domain constants for target field metadata.

Single source of truth for field groups that multiple engine modules must
agree on. Per ADR-0002, this is the seed of the field registry that the
adapter package split will grow into a full metadata table.
"""

from __future__ import annotations

# Light color-mode fields are mutually exclusive: a higher-priority intent
# setting any one suppresses the others from lower-priority intents during
# composition, and the adapter emits only one color descriptor per call.
# Ordered: the adapter picks the first match when a value carries several.
LIGHT_COLOR_FIELDS: tuple[str, ...] = (
    "color_temp_k",
    "color_temp_mired",
    "rgbww_color",
    "rgbw_color",
    "rgb_color",
    "hs_color",
    "xy_color",
)

# HA alarm states mapped to their corresponding HA service calls.
# Shared by the translator (set → calls) and the matcher (calls → expected state).
ALARM_STATE_SERVICES: dict[str, str] = {
    "armed_home": "alarm_arm_home",
    "armed_away": "alarm_arm_away",
    "armed_night": "alarm_arm_night",
    "armed_vacation": "alarm_arm_vacation",
    "armed_custom_bypass": "alarm_arm_custom_bypass",
    "disarmed": "alarm_disarm",
    "disarm": "alarm_disarm",
}
