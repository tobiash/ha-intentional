"""Service-plan signature freezing for deduplication and matching.

A signature is a recursively hashable representation of a service plan,
used to detect duplicate calls and to match applied plans against actual
state. This module is self-contained: it depends only on the shared type
aliases from the adapter package.
"""

from __future__ import annotations

from typing import Any

from . import FrozenValue, ServiceCall, ServiceSignature


def service_signature(
    domain: str,
    service: str,
    service_data: dict[str, Any],
) -> ServiceSignature:
    """Return a deterministic signature for suppressing duplicate service calls."""
    return (
        domain,
        service,
        tuple(
            (key, _freeze_signature_value(value))
            for key, value in sorted(service_data.items())
        ),
    )


def service_plan_signature(calls: tuple[ServiceCall, ...]) -> tuple[ServiceSignature, ...]:
    """Return a deterministic signature for a multi-call service plan."""
    return tuple(service_signature(domain, service, data) for domain, service, data in calls)


def _freeze_signature_value(value: Any) -> FrozenValue:
    """Return a recursively hashable representation of service data."""
    if isinstance(value, dict):
        return tuple(
            (
                _freeze_signature_value(key),
                _freeze_signature_value(item_value),
            )
            for key, item_value in sorted(value.items(), key=lambda item: repr(item[0]))
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_signature_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(
            sorted(
                (_freeze_signature_value(item) for item in value),
                key=repr,
            )
        )
    return value
