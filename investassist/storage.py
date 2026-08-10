"""Frame persistence with a graceful format fallback.

Parquet is the right format for bar data — typed, compressed, fast — but it
needs ``pyarrow``, and a recorder that silently drops a session because an
optional dependency is missing is worse than one that writes a slightly larger
CSV. So: parquet when it is available, CSV when it is not, and the reader
figures out which it is looking at.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)


def parquet_available() -> bool:
    try:
        import pyarrow  # noqa: F401

        return True
    except ImportError:
        try:
            import fastparquet  # noqa: F401

            return True
        except ImportError:
            return False


def _paths(stem: Path) -> tuple[Path, Path]:
    return stem.with_suffix(".parquet"), stem.with_suffix(".csv")


def write_frame(frame: pd.DataFrame, stem: Path) -> Path:
    """Write ``frame`` next to ``stem``, returning the path actually used."""
    parquet_path, csv_path = _paths(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)

    if parquet_available():
        frame.to_parquet(parquet_path)
        # Drop a stale CSV so a later read cannot pick up the older copy.
        csv_path.unlink(missing_ok=True)
        return parquet_path

    frame.to_csv(csv_path)
    return csv_path


def read_frame(stem: Path) -> pd.DataFrame | None:
    """Read whichever format is present, or ``None`` if neither is readable."""
    parquet_path, csv_path = _paths(stem)

    if parquet_path.exists():
        try:
            return pd.read_parquet(parquet_path)
        except Exception:
            log.warning("unreadable parquet at %s, ignoring", parquet_path, exc_info=True)

    if csv_path.exists():
        try:
            return pd.read_csv(csv_path, index_col=0, parse_dates=True)
        except Exception:
            log.warning("unreadable csv at %s, ignoring", csv_path, exc_info=True)

    return None
