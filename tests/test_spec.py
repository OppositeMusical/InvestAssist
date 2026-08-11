"""Spec loading and validation tests.

A typo in a spec file must be an error, not a silent fallback to defaults —
that is how a strategy ends up running parameters nobody chose.
"""

from __future__ import annotations

import pytest
import yaml

from investassist.spec import COMPONENTS, DEFAULT_SPEC, Spec, SpecError


def base_dict() -> dict:
    return yaml.safe_load(DEFAULT_SPEC.read_text(encoding="utf-8"))


def test_default_spec_loads():
    spec = Spec.load()
    assert spec.name == "default"
    assert set(spec.weights) == set(COMPONENTS)


def test_weights_normalise_to_one():
    spec = Spec.load()
    assert sum(spec.normalised_weights.values()) == pytest.approx(1.0)


def test_missing_spec_file_is_an_error():
    with pytest.raises(SpecError, match="not found"):
        Spec.load("/nonexistent/spec.yaml")


def test_unknown_key_is_rejected():
    raw = base_dict()
    raw["features"]["ema_fastt"] = 20
    with pytest.raises(SpecError, match="unknown key"):
        Spec.from_dict(raw)


def test_missing_component_weight_is_rejected():
    raw = base_dict()
    del raw["weights"]["momentum"]
    with pytest.raises(SpecError, match="missing component"):
        Spec.from_dict(raw)


def test_unknown_component_weight_is_rejected():
    raw = base_dict()
    raw["weights"]["vibes"] = 0.5
    with pytest.raises(SpecError, match="unknown component"):
        Spec.from_dict(raw)


def test_negative_weight_is_rejected():
    raw = base_dict()
    raw["weights"]["momentum"] = -0.1
    with pytest.raises(SpecError, match="non-negative"):
        Spec.from_dict(raw)


def test_zero_total_weight_is_rejected():
    raw = base_dict()
    raw["weights"] = {name: 0.0 for name in COMPONENTS}
    with pytest.raises(SpecError, match="positive"):
        Spec.from_dict(raw)


def test_out_of_order_verdict_thresholds_are_rejected():
    raw = base_dict()
    raw["verdicts"]["buy"] = 90.0  # above strong_buy
    with pytest.raises(SpecError, match="descending"):
        Spec.from_dict(raw)


def test_absurd_risk_fraction_is_rejected():
    raw = base_dict()
    raw["risk"]["risk_pct"] = 1.5
    with pytest.raises(SpecError, match="fraction"):
        Spec.from_dict(raw)


def test_non_positive_stop_distance_is_rejected():
    raw = base_dict()
    raw["exits"]["initial_stop_atr"] = 0
    with pytest.raises(SpecError, match="positive"):
        Spec.from_dict(raw)
