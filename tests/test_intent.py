"""Tests for the Intent data model.

An Intent is the core unit of ha-intentional: a claim about how a target
entity should be, with priority metadata. This module tests the Intent
class itself — its creation, comparison, expiration, and immutability.
"""

from __future__ import annotations

import time
from dataclasses import FrozenInstanceError

import pytest

from intentional.intent import Authority, Intent

# ── Authority enum ────────────────────────────────────────────────────


class TestAuthority:
    def test_three_tiers_in_order(self) -> None:
        assert Authority.SENSOR < Authority.AUTOMATION < Authority.USER

    def test_string_values(self) -> None:
        assert Authority.SENSOR.value == "sensor"
        assert Authority.AUTOMATION.value == "automation"
        assert Authority.USER.value == "user"

    def test_from_string_round_trip(self) -> None:
        assert Authority("sensor") is Authority.SENSOR
        assert Authority("automation") is Authority.AUTOMATION
        assert Authority("user") is Authority.USER

    def test_unknown_authority_raises(self) -> None:
        with pytest.raises(ValueError):
            Authority("admin")


# ── Intent construction ───────────────────────────────────────────────


class TestIntentConstruction:
    def test_minimal_intent_uses_defaults(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 50})
        assert intent.target == "light.x"
        assert intent.set == {"brightness_pct": 50}
        assert intent.merge is False
        assert intent.cap == {}
        assert intent.floor == {}
        assert intent.offset == {}
        assert intent.multiply == {}
        assert intent.transition_ms == 0
        assert intent.easing == "linear"
        assert intent.authority is Authority.AUTOMATION
        assert intent.confidence == 1.0
        assert intent.ttl_ms is None
        assert intent.reason == ""
        assert intent.rule_id == ""
        assert intent.created_at_ms > 0

    def test_all_fields_populated(self) -> None:
        now = int(time.time() * 1000)
        intent = Intent(
            target="light.living_room",
            set={"brightness_pct": 80, "color_temp_k": 2700},
            merge=True,
            cap={"brightness_pct": 100},
            floor={"brightness_pct": 0},
            offset={"brightness_pct": -10},
            multiply={"brightness_pct": 0.5},
            transition_ms=1500,
            easing="ease-in-out",
            authority=Authority.USER,
            confidence=0.9,
            ttl_ms=3_600_000,
            reason="Manual override",
            rule_id="movie-scene",
        )
        assert intent.target == "light.living_room"
        assert intent.set == {"brightness_pct": 80, "color_temp_k": 2700}
        assert intent.merge is True
        assert intent.cap == {"brightness_pct": 100}
        assert intent.floor == {"brightness_pct": 0}
        assert intent.offset == {"brightness_pct": -10}
        assert intent.multiply == {"brightness_pct": 0.5}
        assert intent.transition_ms == 1500
        assert intent.easing == "ease-in-out"
        assert intent.authority is Authority.USER
        assert intent.confidence == 0.9
        assert intent.ttl_ms == 3_600_000
        assert intent.reason == "Manual override"
        assert intent.rule_id == "movie-scene"
        assert intent.created_at_ms >= now

    def test_target_required(self) -> None:
        with pytest.raises(TypeError):
            Intent(set={"brightness_pct": 50})  # type: ignore[call-arg]

    def test_field_dicts_are_copied(self) -> None:
        """Mutable defaults protection: passing in a dict must not share state."""
        original = {"brightness_pct": 50}
        intent = Intent(target="light.x", set=original)
        original["brightness_pct"] = 99
        assert intent.set == {"brightness_pct": 50}

    def test_intent_is_immutable(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 50})
        with pytest.raises(FrozenInstanceError):
            intent.target = "light.y"  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            intent.confidence = 0.5  # type: ignore[misc]


# ── Intent comparison and priority ────────────────────────────────────


class TestIntentPriority:
    def test_higher_authority_ranks_higher(self) -> None:
        sensor = Intent(
            target="light.x", set={"brightness_pct": 50}, authority=Authority.SENSOR
        )
        auto = Intent(
            target="light.x", set={"brightness_pct": 50}, authority=Authority.AUTOMATION
        )
        user = Intent(
            target="light.x", set={"brightness_pct": 50}, authority=Authority.USER
        )
        assert user > auto > sensor

    def test_within_authority_higher_confidence_wins(self) -> None:
        weak = Intent(
            target="light.x",
            set={"brightness_pct": 50},
            authority=Authority.AUTOMATION,
            confidence=0.3,
        )
        strong = Intent(
            target="light.x",
            set={"brightness_pct": 50},
            authority=Authority.AUTOMATION,
            confidence=0.9,
        )
        assert strong > weak

    def test_within_authority_and_confidence_newer_wins(self) -> None:
        older = Intent(
            target="light.x",
            set={"brightness_pct": 50},
            authority=Authority.AUTOMATION,
            confidence=0.5,
        )
        # Force a clearly newer created_at_ms
        time.sleep(0.002)
        newer = Intent(
            target="light.x",
            set={"brightness_pct": 50},
            authority=Authority.AUTOMATION,
            confidence=0.5,
        )
        assert newer > older

    def test_priority_tuple_for_sorting(self) -> None:
        """Compositor uses this for max() selection. Order: authority, confidence, created_at, id."""
        intent = Intent(
            target="light.x",
            set={"brightness_pct": 50},
            authority=Authority.USER,
            confidence=0.7,
        )
        priority = intent.priority
        assert len(priority) == 4
        assert priority[0] == 100            # authority index
        assert priority[1] == 0.7            # confidence
        assert priority[2] == intent.created_at_ms
        assert priority[3] == id(intent)     # object identity tiebreaker

    def test_priority_keys_stable_across_intents(self) -> None:
        """Two intents with same metadata should be comparable."""
        a = Intent(
            target="light.x",
            set={"brightness_pct": 50},
            authority=Authority.AUTOMATION,
            confidence=0.5,
        )
        b = Intent(
            target="light.x",
            set={"brightness_pct": 50},
            authority=Authority.AUTOMATION,
            confidence=0.5,
        )
        # Whichever was created later wins
        assert (a.priority < b.priority) or (b.priority < a.priority)


# ── Intent expiration ─────────────────────────────────────────────────


class TestIntentExpiration:
    def test_intent_without_ttl_never_expires(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 50}, ttl_ms=None)
        assert intent.is_expired() is False
        assert intent.is_expired(into_the_future_ms=10**10) is False

    def test_intent_with_ttl_expires_after_duration(self) -> None:
        intent = Intent(
            target="light.x", set={"brightness_pct": 50}, ttl_ms=1000
        )
        # "now" must be a real timestamp, in ms since epoch
        before = intent.created_at_ms + 999
        exactly = intent.created_at_ms + 1000
        after = intent.created_at_ms + 1001
        assert intent.is_expired(into_the_future_ms=before) is False
        assert intent.is_expired(into_the_future_ms=exactly) is True   # boundary
        assert intent.is_expired(into_the_future_ms=after) is True

    def test_intent_with_zero_ttl_is_already_expired(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 50}, ttl_ms=0)
        assert intent.is_expired() is True

    def test_expires_at_returns_future_timestamp(self) -> None:
        intent = Intent(
            target="light.x", set={"brightness_pct": 50}, ttl_ms=2000
        )
        expected = intent.created_at_ms + 2000
        assert intent.expires_at_ms() == expected

    def test_intent_without_ttl_has_no_expiry(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 50})
        assert intent.expires_at_ms() is None


# ── Intent string representation ──────────────────────────────────────


class TestIntentStringRepresentation:
    def test_repr_contains_target_and_authority(self) -> None:
        intent = Intent(
            target="light.living_room",
            set={"brightness_pct": 80},
            authority=Authority.USER,
            rule_id="movie-scene",
        )
        r = repr(intent)
        assert "light.living_room" in r
        assert "user" in r
        assert "movie-scene" in r

    def test_repr_works_without_rule_id(self) -> None:
        intent = Intent(target="light.x", set={"brightness_pct": 50})
        r = repr(intent)
        assert "light.x" in r
