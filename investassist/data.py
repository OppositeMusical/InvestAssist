"""History providers and the local bar cache.

The engine needs daily bars. thinkorswim's RTD bridge streams live quotes but
is not a history API, so backfill comes from here. Providers are deliberately
interchangeable — start on a free source, swap in a paid one later without
touching the scoring code.
"""

from __future__ import annotations

import io
import logging
import os
from abc import ABC, abstractmethod
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .storage import read_frame, write_frame

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]
DEFAULT_CACHE = Path(
    os.environ.get("INVESTASSIST_CACHE", Path.home() / ".investassist" / "bars")
)


class ProviderError(RuntimeError):
    """A provider could not return usable bars."""


def normalise(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Coerce a provider's output into the canonical bar frame.

    Providers disagree about capitalisation, column order, index type and sort
    order. Every one of those has caused a silently wrong backtest at some
    point, so normalisation is centralised here rather than trusted per source.
    """
    frame = frame.rename(columns={c: str(c).strip().lower() for c in frame.columns})
    missing = set(COLUMNS) - set(frame.columns)
    if missing:
        raise ProviderError(
            f"{symbol}: provider returned no {', '.join(sorted(missing))} column"
        )

    frame = frame[COLUMNS].copy()
    for column in COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ProviderError(f"{symbol}: provider returned a non-datetime index")
    frame.index = frame.index.tz_localize(None).normalize()
    frame.index.name = "date"

    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    # A bar with no close is unusable; a bar with no volume is merely a quiet
    # day and gets zero rather than being dropped.
    frame["volume"] = frame["volume"].fillna(0.0)
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    if frame.empty:
        raise ProviderError(f"{symbol}: provider returned no usable rows")
    return frame


class HistoryProvider(ABC):
    """Source of daily OHLCV bars."""

    name = "abstract"

    @abstractmethod
    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Return normalised daily bars for ``symbol`` in ``[start, end]``."""


class CSVProvider(HistoryProvider):
    """Bars from local ``<dir>/<SYMBOL>.csv`` files.

    The zero-dependency path, and the one the tests use. Also how you bring in
    an export from thinkorswim or any vendor without writing a new provider.
    """

    name = "csv"

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        path = self.directory / f"{symbol.upper()}.csv"
        if not path.exists():
            raise ProviderError(f"{symbol}: no CSV at {path}")
        frame = pd.read_csv(path, index_col=0, parse_dates=True)
        frame = normalise(frame, symbol)
        return frame.loc[str(start) : str(end)]


class StooqProvider(HistoryProvider):
    """Free daily bars from stooq.com. No API key, no account, rate-limited.

    Good enough to get the engine running today. Note that stooq adjusts for
    splits but its dividend handling is inconsistent, so treat it as a
    development source rather than the basis for a live allocation decision.
    """

    name = "stooq"
    URL = "https://stooq.com/q/d/l/"

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - import guard
            raise ProviderError("stooq provider needs `requests` installed") from exc

        params = {
            "s": f"{symbol.lower()}.us",
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
            "i": "d",
        }
        response = requests.get(self.URL, params=params, timeout=30)
        response.raise_for_status()
        text = response.text
        # Stooq answers a bad symbol with a 200 and the body "No data".
        if not text.lstrip().lower().startswith("date"):
            raise ProviderError(f"{symbol}: stooq returned no data")
        return normalise(pd.read_csv(io.StringIO(text), index_col=0, parse_dates=True), symbol)


class YFinanceProvider(HistoryProvider):
    """Daily bars via ``yfinance``. Split/dividend adjusted, unofficial API."""

    name = "yfinance"

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        try:
            import yfinance
        except ImportError as exc:  # pragma: no cover - import guard
            raise ProviderError("yfinance provider needs `yfinance` installed") from exc

        frame = yfinance.download(
            symbol,
            start=start.isoformat(),
            # yfinance treats `end` as exclusive.
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=True,
            progress=False,
        )
        if frame is None or frame.empty:
            raise ProviderError(f"{symbol}: yfinance returned no data")
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        return normalise(frame, symbol)


class BarStore:
    """Parquet-backed cache in front of a provider.

    Scoring a symbol should not cost a network round trip every time you press
    the hotkey. Cached bars are reused when they already cover today's session.
    """

    def __init__(
        self,
        provider: HistoryProvider,
        directory: str | Path = DEFAULT_CACHE,
        max_age_days: int = 1,
    ):
        self.provider = provider
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_age_days = max_age_days

    def _stem(self, symbol: str) -> Path:
        return self.directory / symbol.upper()

    def _read_cache(self, symbol: str) -> pd.DataFrame | None:
        # A truncated or unreadable cache file costs a refetch rather than
        # taking the session down; read_frame already logs and returns None.
        frame = read_frame(self._stem(symbol))
        if frame is None or frame.empty:
            return None
        if not isinstance(frame.index, pd.DatetimeIndex):
            return None
        return frame

    def get(
        self,
        symbol: str,
        lookback_days: int = 800,
        end: date | None = None,
        refresh: bool = False,
    ) -> pd.DataFrame:
        symbol = symbol.upper()
        end = end or date.today()
        start = end - timedelta(days=lookback_days)

        cached = None if refresh else self._read_cache(symbol)
        if cached is not None and not cached.empty:
            newest = cached.index[-1].date()
            covers_start = cached.index[0].date() <= start + timedelta(days=7)
            if covers_start and (end - newest).days <= self.max_age_days:
                return cached.loc[str(start) :]

        fresh = self.provider.fetch(symbol, start, end)
        if cached is not None and not cached.empty:
            fresh = normalise(
                pd.concat([cached, fresh])[~pd.concat([cached, fresh]).index.duplicated(keep="last")],
                symbol,
            )
        try:
            write_frame(fresh, self._stem(symbol))
        except Exception:
            # Read-only disk, no space, whatever — caching is an optimisation
            # and must never be load-bearing.
            log.warning("could not cache bars for %s", symbol, exc_info=True)
        return fresh.loc[str(start) :]


def build_provider(name: str, csv_dir: str | Path | None = None) -> HistoryProvider:
    """Resolve a provider by name, for the CLI and config files."""
    name = name.lower()
    if name == "csv":
        if not csv_dir:
            raise ProviderError("csv provider requires a directory")
        return CSVProvider(csv_dir)
    if name == "stooq":
        return StooqProvider()
    if name in ("yfinance", "yahoo"):
        return YFinanceProvider()
    raise ProviderError(f"unknown provider '{name}' (csv, stooq, yfinance)")
