"""HA adapter package: focused modules for HA domain translation.

Per ADR-0002, the former ha_adapter.py monolith splits into sibling modules
behind small interfaces. This package groups them and owns the shared type
aliases that cross module boundaries.
"""

from __future__ import annotations

from typing import Any

ServiceCall = tuple[str, str, dict[str, Any]]
FrozenValue = Any
ServiceSignature = tuple[str, str, tuple[tuple[str, FrozenValue], ...]]
ServicePlanSignature = tuple[ServiceSignature, ...]
SceneActivationPlan = tuple[tuple[ServiceCall, ...], set[str], set[str]]
