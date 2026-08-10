"""The service layer that the panel, the CLI and the backtester all share."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd

from .data import BarStore, ProviderError
from .risk import Position, SizeDecision, size_position
from .score import Assessment, InsufficientData, assess
from .spec import Spec

log = logging.getLogger(__name__)


@dataclass
class Recommendation:
    """An assessment plus the sizing that follows from it."""

    assessment: Assessment
    size: SizeDecision | None

    @property
    def headline(self) -> str:
        a = self.assessment
        return f"{a.symbol} — {a.verdict.value} ({a.score:+.0f}, {a.confidence:.0f}% confidence)"

    def as_text(self) -> str:
        a = self.assessment
        lines = [
            self.headline,
            f"  price {a.price:,.2f}   ATR {a.atr:,.2f} ({a.atr_pct:.1f}%)   regime {a.regime.value}",
            f"  stop {a.exits.initial_stop:,.2f}   risk/share {a.exits.risk_per_share:,.2f}"
            f"   targets {', '.join(f'{t:,.2f}' for t in a.exits.targets)}",
        ]
        if self.size is not None:
            if self.size.approved:
                lines.append(
                    f"  size {self.size.shares} shares (${self.size.dollars:,.0f}, "
                    f"risking ${self.size.risk_dollars:,.0f})"
                )
                if self.size.limited_by:
                    lines.append(f"  capped by {self.size.limited_by}")
            else:
                lines.append(f"  no position: {self.size.limited_by}")
        lines.append("  why:")
        lines.extend(f"    - {reason}" for reason in a.rationale)
        return "\n".join(lines)


class Engine:
    """Scores symbols on demand, caching bars and the benchmark."""

    def __init__(self, store: BarStore, spec: Spec, equity: float | None = None):
        self.store = store
        self.spec = spec
        self.equity = equity
        self._benchmark: pd.DataFrame | None = None
        self._benchmark_day: date | None = None

    def benchmark(self, asof: date | None = None) -> pd.DataFrame | None:
        """Benchmark bars for the regime gate, fetched at most once a day.

        A missing benchmark is not fatal — ``detect_regime`` treats it as
        NEUTRAL, which costs conviction rather than inventing it.
        """
        today = asof or date.today()
        if self._benchmark is not None and self._benchmark_day == today:
            return self._benchmark
        try:
            self._benchmark = self.store.get(self.spec.regime.benchmark, end=today)
            self._benchmark_day = today
        except (ProviderError, Exception) as exc:
            log.warning("benchmark %s unavailable: %s", self.spec.regime.benchmark, exc)
            self._benchmark = None
        return self._benchmark

    def recommend(
        self,
        symbol: str,
        open_positions: list[Position] | None = None,
        realised_r_today: float = 0.0,
        entry: float | None = None,
        refresh: bool = False,
    ) -> Recommendation:
        """Score ``symbol`` and size it. Raises on missing or thin data."""
        bars = self.store.get(symbol, refresh=refresh)
        assessment = assess(
            bars,
            self.spec,
            symbol=symbol.upper(),
            benchmark=self.benchmark(),
            entry=entry,
        )

        size = None
        # Sizing only means something for a position we would actually open.
        if self.equity and assessment.verdict.is_bullish:
            size = size_position(
                self.equity,
                assessment.exits,
                self.spec,
                open_positions=open_positions,
                realised_r_today=realised_r_today,
            )
        return Recommendation(assessment=assessment, size=size)

    def scan(self, symbols: list[str]) -> list[Recommendation]:
        """Score a universe, best first. Symbols that cannot be scored are
        skipped with a warning rather than aborting the whole run."""
        out: list[Recommendation] = []
        for symbol in symbols:
            try:
                out.append(self.recommend(symbol))
            except (InsufficientData, ProviderError) as exc:
                log.warning("skipping %s: %s", symbol, exc)
            except Exception:
                log.exception("unexpected failure scoring %s", symbol)
        return sorted(out, key=lambda r: r.assessment.score, reverse=True)
