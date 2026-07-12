"""ha-intentional: Declarative intent-based automation for Home Assistant.

This package contains the pure-Python intent engine, which is the core of
ha-intentional. The Home Assistant integration is in `custom_components/intentional/`
and uses this engine to load YAML rules, listen to state changes, and apply
resolved intents to Home Assistant entities.

The engine has no Home Assistant dependencies. It can be used standalone for
testing, development, or in other contexts.
"""

from __future__ import annotations

__version__ = "0.10.1"

__all__ = [
    "AnimationFrame",
    "AnimationSpec",
    "Authority",
    "Intent",
    "ResolvedIntent",
    "RuleLoadError",
    "__version__",
    "load_rules",
    "resolve_intents",
]


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """Lazily import submodules to avoid forcing all of them to exist at import time.

    This makes test-driven development practical: write tests for one module,
    run them, without the whole package being assembled.
    """
    if name in ("Intent", "Authority"):
        from .intent import Authority, Intent

        return {"Intent": Intent, "Authority": Authority}[name]
    if name in ("ResolvedIntent", "resolve_intents"):
        from .compositor import ResolvedIntent, resolve_intents

        return {
            "ResolvedIntent": ResolvedIntent,
            "resolve_intents": resolve_intents,
        }[name]
    if name in ("AnimationSpec", "AnimationFrame"):
        from .animation import AnimationFrame, AnimationSpec

        return {
            "AnimationSpec": AnimationSpec,
            "AnimationFrame": AnimationFrame,
        }[name]
    if name in ("load_rules", "RuleLoadError"):
        from .yaml_loader import Rule, RuleLoadError, load_rules

        return {
            "load_rules": load_rules,
            "RuleLoadError": RuleLoadError,
            "Rule": Rule,
        }[name]
    if name in ("parse_when", "WhenSyntaxError"):
        from .when_parser import WhenSyntaxError, parse_when

        return {"parse_when": parse_when, "WhenSyntaxError": WhenSyntaxError}[name]
    if name == "Engine":
        from .engine import Engine

        return Engine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
