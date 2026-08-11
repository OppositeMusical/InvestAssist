"""Loading and validation for the scoring specification.

The YAML in ``investassist/spec/`` is the single source of truth for both the
Python engine and the generated thinkScript studies. Everything here is plain
dataclasses so the spec can be constructed in tests without touching disk.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

import yaml

SPEC_DIR = Path(__file__).parent / "spec"
DEFAULT_SPEC = SPEC_DIR / "default.yaml"


class SpecError(ValueError):
    """Raised when a spec file is malformed or internally inconsistent."""


def _subset(cls: type, data: Mapping[str, Any], section: str):
    """Build a dataclass from ``data``, rejecting unknown keys.

    Silently dropping unknown keys is how a typo in a spec file turns into a
    strategy that quietly runs with default parameters, so we refuse instead.
    """
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise SpecError(
            f"unknown key(s) in '{section}': {', '.join(sorted(unknown))}"
        )
    return cls(**data)


@dataclass(frozen=True)
class RegimeSpec:
    benchmark: str = "SPY"
    trend_lookback: int = 200
    roc_lookback: int = 20
    risk_on_multiplier: float = 1.0
    neutral_multiplier: float = 0.85
    risk_off_multiplier: float = 0.55


@dataclass(frozen=True)
class FeatureSpec:
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    adx_length: int = 14
    adx_trend_floor: float = 20.0
    rsi_length: int = 14
    rsi_fast_length: int = 2
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_length: int = 20
    bb_stdev: float = 2.0
    atr_length: int = 14
    donchian_length: int = 252
    volume_avg_length: int = 20
    min_bars: int = 260


@dataclass(frozen=True)
class ConfidenceSpec:
    agreement_weight: float = 0.7
    volatility_weight: float = 0.3
    high_atr_pct: float = 6.0


@dataclass(frozen=True)
class VerdictSpec:
    strong_buy: float = 60.0
    buy: float = 25.0
    trim: float = -25.0
    sell: float = -60.0

    def __post_init__(self) -> None:
        if not (self.strong_buy > self.buy > self.trim > self.sell):
            raise SpecError(
                "verdict thresholds must be strictly descending: "
                f"strong_buy({self.strong_buy}) > buy({self.buy}) > "
                f"trim({self.trim}) > sell({self.sell})"
            )


@dataclass(frozen=True)
class ExitSpec:
    initial_stop_atr: float = 2.0
    trail_activate_r: float = 1.0
    chandelier_lookback: int = 22
    chandelier_atr: float = 3.0
    targets_r: tuple[float, ...] = (1.0, 2.0)
    time_stop_bars: int = 40
    invalidate_below: float = 0.0

    def __post_init__(self) -> None:
        if self.initial_stop_atr <= 0:
            raise SpecError("exits.initial_stop_atr must be positive")
        object.__setattr__(self, "targets_r", tuple(self.targets_r))


@dataclass(frozen=True)
class RiskSpec:
    risk_pct: float = 0.005
    max_position_pct: float = 0.20
    max_open_positions: int = 8
    max_portfolio_heat_r: float = 4.0
    daily_loss_limit_r: float = 2.0

    def __post_init__(self) -> None:
        if not 0 < self.risk_pct < 1:
            raise SpecError("risk.risk_pct must be a fraction between 0 and 1")


@dataclass(frozen=True)
class CostSpec:
    commission_per_share: float = 0.0
    commission_min: float = 0.0
    slippage_bps: float = 5.0
    slippage_sweep_bps: tuple[float, ...] = (0.0, 2.5, 5.0, 10.0, 20.0, 40.0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "slippage_sweep_bps", tuple(self.slippage_sweep_bps))


# Component names, in the order they are displayed. Adding a component means
# adding it here, in `weights` in the YAML, and in score.py.
COMPONENTS = (
    "trend_stack",
    "trend_strength",
    "momentum",
    "rsi_position",
    "mean_reversion",
    "location",
    "volume",
)


@dataclass(frozen=True)
class Spec:
    name: str = "default"
    version: int = 1
    regime: RegimeSpec = field(default_factory=RegimeSpec)
    features: FeatureSpec = field(default_factory=FeatureSpec)
    weights: Mapping[str, float] = field(default_factory=dict)
    confidence: ConfidenceSpec = field(default_factory=ConfidenceSpec)
    verdicts: VerdictSpec = field(default_factory=VerdictSpec)
    exits: ExitSpec = field(default_factory=ExitSpec)
    risk: RiskSpec = field(default_factory=RiskSpec)
    costs: CostSpec = field(default_factory=CostSpec)

    @property
    def normalised_weights(self) -> dict[str, float]:
        """Weights rescaled to sum to 1, so the score stays in -100..100."""
        total = sum(self.weights.values())
        return {k: v / total for k, v in self.weights.items()}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Spec":
        path = Path(path) if path else DEFAULT_SPEC
        if not path.exists():
            raise SpecError(f"spec file not found: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Spec":
        raw = copy.deepcopy(dict(raw))

        weights = raw.get("weights") or {}
        missing = set(COMPONENTS) - set(weights)
        extra = set(weights) - set(COMPONENTS)
        if missing:
            raise SpecError(f"weights missing component(s): {', '.join(sorted(missing))}")
        if extra:
            raise SpecError(f"weights has unknown component(s): {', '.join(sorted(extra))}")
        if any(w < 0 for w in weights.values()):
            raise SpecError("weights must be non-negative")
        if sum(weights.values()) <= 0:
            raise SpecError("weights must sum to a positive number")

        return cls(
            name=raw.get("name", "default"),
            version=int(raw.get("version", 1)),
            regime=_subset(RegimeSpec, raw.get("regime", {}), "regime"),
            features=_subset(FeatureSpec, raw.get("features", {}), "features"),
            weights=dict(weights),
            confidence=_subset(ConfidenceSpec, raw.get("confidence", {}), "confidence"),
            verdicts=_subset(VerdictSpec, raw.get("verdicts", {}), "verdicts"),
            exits=_subset(ExitSpec, raw.get("exits", {}), "exits"),
            risk=_subset(RiskSpec, raw.get("risk", {}), "risk"),
            costs=_subset(CostSpec, raw.get("costs", {}), "costs"),
        )
