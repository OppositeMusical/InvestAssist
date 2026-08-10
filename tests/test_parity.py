"""Parity between the Python engine and the generated thinkScript.

Full numerical parity needs a chart export from thinkorswim, which cannot be
produced in CI — that is what ``tools/parity.py`` is for, and it should be run
by hand after any spec change.

What *can* be enforced automatically is everything upstream of that: the
generated block is in sync with the spec, all three studies carry an identical
core, and the constants thinkScript will use are exactly the numbers Python
uses. Those are the failure modes that actually happen.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from investassist.spec import Spec
from investassist.thinkscript_gen import (
    BEGIN,
    END,
    TARGETS,
    GeneratorError,
    build_core,
    generate,
    render_into,
)

THINKSCRIPT_DIR = Path(__file__).resolve().parent.parent / "thinkscript"


def core_of(path: Path) -> str:
    text = path.read_text()
    start = text.index(BEGIN) + len(BEGIN)
    return text[start : text.index(END)].strip()


def test_every_target_exists():
    for name in TARGETS:
        assert (THINKSCRIPT_DIR / name).exists(), f"missing {name}"


def test_committed_thinkscript_matches_the_spec():
    """Fails when someone edits the spec and forgets to regenerate."""
    expected = build_core(Spec.load()).strip()
    for name in TARGETS:
        assert core_of(THINKSCRIPT_DIR / name) == expected, (
            f"{name} is stale — run: python -m investassist.cli gen-thinkscript"
        )


def test_all_three_studies_share_one_core():
    cores = {name: core_of(THINKSCRIPT_DIR / name) for name in TARGETS}
    assert len(set(cores.values())) == 1


def test_generation_is_idempotent(tmp_path):
    spec = Spec.load()
    for name in TARGETS:
        (tmp_path / name).write_text(f"{BEGIN}\nstale\n{END}\ntrailing content\n")

    assert set(generate(spec, tmp_path)) == set(TARGETS)
    # Second run has nothing left to change.
    assert generate(spec, tmp_path) == []
    # And the hand-written region below the markers is untouched.
    assert (tmp_path / TARGETS[0]).read_text().endswith("trailing content\n")


def test_constants_carry_the_spec_values():
    core = build_core(Spec.load())
    spec = Spec.load()

    def constant(name: str) -> float:
        match = re.search(rf"^def {name} = ([-\d.eE+]+);", core, re.MULTILINE)
        assert match, f"{name} missing from generated core"
        return float(match.group(1))

    assert constant("EMA_FAST") == spec.features.ema_fast
    assert constant("EMA_SLOW") == spec.features.ema_slow
    assert constant("ATR_LENGTH") == spec.features.atr_length
    assert constant("MIN_BARS") == spec.features.min_bars
    assert constant("T_STRONG_BUY") == spec.verdicts.strong_buy
    assert constant("T_SELL") == spec.verdicts.sell
    assert constant("INITIAL_STOP_ATR") == spec.exits.initial_stop_atr


def test_generated_weights_are_normalised():
    core = build_core(Spec.load())
    weights = [
        float(m) for m in re.findall(r"^def W_[A-Z_]+ = ([\d.]+);", core, re.MULTILINE)
    ]
    assert len(weights) == 7
    assert sum(weights) == pytest.approx(1.0, abs=1e-6)


def test_integer_division_hazard_is_avoided():
    """thinkScript truncates integer division, so ``stack / 3`` would be 0.

    This is the single easiest way to silently break the port, and it does not
    show up as an error — just a component stuck at zero.
    """
    core = build_core(Spec.load())
    assert "stack / 3.0" in core
    assert "/ 50.0" in core


def test_spec_change_propagates(tmp_path):
    """Changing a number in the spec must change the generated thinkScript."""
    from dataclasses import replace

    spec = Spec.load()
    louder = replace(spec, weights={**spec.weights, "momentum": spec.weights["momentum"] * 3})
    assert build_core(spec) != build_core(louder)


def test_missing_markers_are_an_error(tmp_path):
    broken = tmp_path / "broken.ts"
    broken.write_text("no markers here")
    with pytest.raises(GeneratorError, match="markers"):
        render_into(broken, "core")


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(GeneratorError, match="missing"):
        render_into(tmp_path / "absent.ts", "core")
