"""Template rendering for rule-emitted intent and effect values."""

from __future__ import annotations

from typing import Any

from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment

from .records import Effect


class TemplateRenderer:
    """Render Jinja scalar values against the engine state snapshot."""

    def __init__(self) -> None:
        self._env = NativeEnvironment(undefined=StrictUndefined)

    def render_value(self, value: Any, state: dict[str, Any]) -> Any:
        """Render templated scalars recursively, preserving native Python types."""
        if isinstance(value, str):
            if "{{" not in value and "{%" not in value:
                return value
            return self._env.from_string(value).render(
                states=lambda entity_id: state.get(f"{entity_id}.state", "unknown"),
                state_attr=lambda entity_id, attr: _state_attr(state, entity_id, attr),
            )
        if isinstance(value, dict):
            return {key: self.render_value(item, state) for key, item in value.items()}
        if isinstance(value, list):
            return [self.render_value(item, state) for item in value]
        if isinstance(value, tuple):
            return tuple(self.render_value(item, state) for item in value)
        return value

    def render_effect(self, effect: Effect, state: dict[str, Any]) -> Effect:
        """Render templated effect target/data values."""
        return Effect(
            domain=effect.domain,
            service=effect.service,
            target=self.render_value(effect.target, state),
            data=self.render_value(effect.data, state),
        )


def _state_attr(state: dict[str, Any], entity_id: str, attr: str) -> Any:
    attrs = state.get(f"{entity_id}.attributes")
    if isinstance(attrs, dict):
        return attrs.get(attr)
    return None
