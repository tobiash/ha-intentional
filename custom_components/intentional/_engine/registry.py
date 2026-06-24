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
