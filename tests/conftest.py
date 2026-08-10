"""Shared fixtures. All synthetic and seeded, so tests never touch the network."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from investassist.spec import Spec


def make_bars(
    n: int = 400,
    start_price: float = 100.0,
    drift: float = 0.0,
    volatility: float = 0.01,
    seed: int = 7,
    start: str = "2021-01-04",
) -> pd.DataFrame:
    """Deterministic OHLCV bars on a business-day calendar.

    ``drift`` is the per-bar log return, so a positive value gives a clean
    uptrend and a negative one a downtrend — enough to assert that the score
    points the right way without pretending to be a market simulator.
    """
    rng = np.random.default_rng(seed)
    shocks = rng.normal(drift, volatility, n)
    close = start_price * np.exp(np.cumsum(shocks))

    # Build a plausible bar around each close.
    spread = np.abs(rng.normal(0, volatility * 0.6, n)) * close
    open_ = np.concatenate([[start_price], close[:-1]])
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.integers(500_000, 2_000_000, n).astype(float)

    index = pd.bdate_range(start=start, periods=n, name="date")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


@pytest.fixture
def spec() -> Spec:
    return Spec.load()


@pytest.fixture
def uptrend() -> pd.DataFrame:
    return make_bars(drift=0.0015, seed=11)


@pytest.fixture
def downtrend() -> pd.DataFrame:
    return make_bars(drift=-0.0015, seed=13)


@pytest.fixture
def flat() -> pd.DataFrame:
    return make_bars(drift=0.0, volatility=0.006, seed=17)


# Benchmark fixtures use low volatility deliberately. The regime gate reads the
# most recent bar, so a noisy series can end on a 20-day pullback inside a real
# uptrend — correct behaviour, but it would make these tests measure the noise
# rather than the classification logic.
@pytest.fixture
def benchmark_up() -> pd.DataFrame:
    return make_bars(drift=0.0012, volatility=0.003, seed=23)


@pytest.fixture
def benchmark_down() -> pd.DataFrame:
    return make_bars(drift=-0.0012, volatility=0.003, seed=29)
