#!/usr/bin/env python3
"""Compare thinkScript's on-chart score against the Python engine.

Two implementations of the same model will drift. This is the tool that proves
they have not. Run it whenever you change the spec, and treat a failure as a
bug in whichever side you touched last.

Exporting the reference data from thinkorswim:

  1. Add ``investassist_score`` to a daily chart of the symbol you want.
  2. Set the chart to show enough history — at least 260 bars past the point
     you care about, since the score is suppressed before then.
  3. Right-click the chart -> Export -> Export Data, save as CSV.
  4. The export contains the OHLCV columns plus one column per study plot.
     The score lands in the column named after the study's ``Score`` plot.

Then:

  python tools/parity.py exports/AAPL_daily.csv --score-column "Score"

Expect small differences. thinkorswim computes on its own adjusted data and
carries a different amount of warm-up history than your provider, so the first
bars after MIN_BARS will disagree more than later ones. Sustained divergence
late in the series is the real signal that something is wrong.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from investassist.score import score_series  # noqa: E402
from investassist.spec import Spec  # noqa: E402

OHLCV_ALIASES = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
}


def load_export(path: Path, score_column: str) -> tuple[pd.DataFrame, pd.Series]:
    """Parse a thinkorswim chart export into bars plus the reference score."""
    frame = pd.read_csv(path)
    frame.columns = [str(c).strip() for c in frame.columns]

    lowered = {c.lower(): c for c in frame.columns}
    missing = [name for name in OHLCV_ALIASES if name not in lowered]
    if missing:
        raise SystemExit(
            f"export is missing column(s): {', '.join(missing)}\n"
            f"found: {', '.join(frame.columns)}"
        )
    if score_column not in frame.columns:
        raise SystemExit(
            f"no '{score_column}' column in the export.\n"
            f"found: {', '.join(frame.columns)}\n"
            "Pass the right one with --score-column."
        )

    # thinkorswim exports Date, and Time as a separate column on intraday.
    date_col = lowered.get("date")
    if date_col is None:
        raise SystemExit("export has no Date column")
    stamps = frame[date_col].astype(str)
    if "time" in lowered:
        stamps = stamps + " " + frame[lowered["time"]].astype(str)

    index = pd.DatetimeIndex(pd.to_datetime(stamps, errors="coerce"), name="date")
    bars = pd.DataFrame(
        {target: pd.to_numeric(frame[lowered[src]], errors="coerce")
         for src, target in OHLCV_ALIASES.items()},
        index=index,
    )
    reference = pd.to_numeric(frame[score_column], errors="coerce")
    reference.index = index

    keep = bars.notna().all(axis=1) & index.notna()
    return bars[keep].sort_index(), reference[keep].sort_index()


def compare(
    bars: pd.DataFrame,
    reference: pd.Series,
    spec: Spec,
    benchmark: pd.DataFrame | None,
    tolerance: float,
) -> int:
    computed = score_series(bars, spec, benchmark)

    both = pd.DataFrame({"thinkscript": reference, "python": computed}).dropna()
    # Below min_bars the study plots NaN by design; nothing to compare.
    both = both.iloc[spec.features.min_bars :] if len(both) > spec.features.min_bars else both
    if both.empty:
        raise SystemExit(
            "no overlapping bars to compare — the export probably has fewer "
            f"than {spec.features.min_bars} bars of history"
        )

    both["diff"] = (both["thinkscript"] - both["python"]).abs()
    worst = both["diff"].max()
    mean = both["diff"].mean()
    breaches = both[both["diff"] > tolerance]

    print(f"bars compared     {len(both)}")
    print(f"mean |difference| {mean:.4f}")
    print(f"max  |difference| {worst:.4f}")
    print(f"tolerance         {tolerance:.4f}")
    print(f"breaches          {len(breaches)}")

    if not breaches.empty:
        print("\nworst offenders:")
        print(breaches.nlargest(10, "diff").to_string())
        print(
            "\nA handful of early breaches is usually warm-up history. A steady "
            "run of them late in the series means the implementations diverged."
        )
        return 1

    print("\nparity holds.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("export", type=Path, help="CSV exported from a thinkorswim chart")
    parser.add_argument("--score-column", default="Score", help="study plot column name")
    parser.add_argument("--spec", help="spec YAML (defaults to the bundled one)")
    parser.add_argument("--benchmark", type=Path, help="CSV export of the benchmark, for the regime gate")
    parser.add_argument("--tolerance", type=float, default=1.0, help="allowed absolute score difference")
    args = parser.parse_args(argv)

    spec = Spec.load(args.spec)
    bars, reference = load_export(args.export, args.score_column)

    benchmark = None
    if args.benchmark:
        benchmark, _ = load_export(args.benchmark, args.score_column)
    else:
        print(
            "note: no --benchmark given, so the regime gate falls back to "
            "NEUTRAL. thinkorswim always has SPY available, so expect a "
            "constant-factor difference unless you export it too.\n"
        )

    return compare(bars, reference, spec, benchmark, args.tolerance)


if __name__ == "__main__":
    raise SystemExit(main())
