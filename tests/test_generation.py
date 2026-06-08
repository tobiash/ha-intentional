from __future__ import annotations

from intentional.generation import parse_generator_spec, sample_generated_field
from intentional.yaml_loader import parse_duration


def _spec(raw):
    return parse_generator_spec(raw, parse_duration=parse_duration)


def test_walk_generator_moves_to_palette_neighbor() -> None:
    spec = _spec({
        "kind": "walk",
        "from": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        "every": "1s",
    })

    state = sample_generated_field(
        spec,
        now_ms=0,
        seed="walk",
        previous_value=[0, 1, 0],
    )

    assert state.value in ([1, 0, 0], [0, 0, 1])


def test_weighted_sample_generator_accepts_inline_weights() -> None:
    spec = _spec({
        "kind": "weighted_sample",
        "from": [
            {"value": [255, 0, 0], "weight": 0},
            {"value": [0, 0, 255], "weight": 1},
        ],
        "every": "1s",
    })

    state = sample_generated_field(spec, now_ms=0, seed="weighted")

    assert state.value == [0, 0, 255]


def test_gradient_generator_moves_halfway_to_nearest_palette_color() -> None:
    spec = _spec({
        "kind": "gradient",
        "mode": "nearest",
        "from": [[100, 0, 0], [0, 100, 0]],
        "every": "1s",
    })

    state = sample_generated_field(
        spec,
        now_ms=0,
        seed="gradient",
        previous_value=[80, 0, 0],
    )

    assert state.value == [90, 0, 0]


def test_noise_generator_can_generate_rgb_from_hsv_ranges() -> None:
    spec = _spec({
        "kind": "noise",
        "hue": {"min": 0, "max": 0},
        "saturation": {"min": 100, "max": 100},
        "value": {"min": 100, "max": 100},
        "every": "1s",
    })

    state = sample_generated_field(spec, now_ms=0, seed="noise")

    assert state.value == [255, 0, 0]


def test_noise_generator_can_generate_numeric_values() -> None:
    spec = _spec({
        "kind": "noise",
        "min": 10,
        "max": 20,
        "step": 5,
        "every": "1s",
    })

    state = sample_generated_field(spec, now_ms=0, seed="numeric-noise")

    assert state.value in {10, 15, 20}
