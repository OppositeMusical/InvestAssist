"""Record the RTD quote stream into intraday bars.

The RTD bridge is a quote feed, not a history API, and thinkorswim will not
hand you intraday history. So if you want intraday bars to backtest on, the
only free way to get them is to start recording now — every session you capture
becomes data you can test against later. Start this on day one even if the
engine is still running on daily bars.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .rtd import FIELD_LAST, FIELD_VOLUME, QUOTE_FIELDS, RTDClient, RTDError
from .storage import read_frame, write_frame

log = logging.getLogger(__name__)


@dataclass
class _Building:
    """A bar under construction."""

    start: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    first_volume: float | None = None
    last_volume: float | None = None

    def update(self, price: float, cumulative_volume: float | None) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        if cumulative_volume is not None:
            if self.first_volume is None:
                self.first_volume = cumulative_volume
            self.last_volume = cumulative_volume

    def finish(self) -> dict:
        # RTD reports session-cumulative volume, so a bar's volume is the
        # difference across it. A negative delta means the session rolled over.
        volume = 0.0
        if self.first_volume is not None and self.last_volume is not None:
            volume = max(0.0, self.last_volume - self.first_volume)
        return {
            "date": self.start,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": volume,
        }


@dataclass
class BarRecorder:
    """Aggregates RTD ticks into fixed-interval bars and appends to Parquet."""

    client: RTDClient
    symbols: list[str]
    directory: Path
    interval_seconds: int = 60
    poll_seconds: float = 1.0
    _building: dict[str, _Building] = field(default_factory=dict)
    _completed: dict[str, list[dict]] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.symbols = [s.upper() for s in self.symbols]

    def _bucket(self, when: datetime) -> pd.Timestamp:
        epoch = int(when.timestamp())
        return pd.Timestamp(epoch - (epoch % self.interval_seconds), unit="s", tz=timezone.utc)

    def ingest(self, symbol: str, price: float, volume: float | None, when: datetime) -> None:
        """Fold one tick into the current bar, rolling over when it expires."""
        bucket = self._bucket(when)
        current = self._building.get(symbol)

        if current is None:
            self._building[symbol] = _Building(bucket, price, price, price, price)
            self._building[symbol].update(price, volume)
            return

        if bucket > current.start:
            self._completed.setdefault(symbol, []).append(current.finish())
            fresh = _Building(bucket, price, price, price, price)
            fresh.update(price, volume)
            self._building[symbol] = fresh
        else:
            current.update(price, volume)

    def _stem(self, symbol: str) -> Path:
        return self.directory / f"{symbol}_{self.interval_seconds}s"

    def flush(self) -> int:
        """Append completed bars to disk. Returns how many were written."""
        written = 0
        for symbol, rows in list(self._completed.items()):
            if not rows:
                continue
            frame = pd.DataFrame(rows).set_index("date").sort_index()

            existing = read_frame(self._stem(symbol))
            if existing is not None and not existing.empty:
                frame = pd.concat([existing, frame])
                frame = frame[~frame.index.duplicated(keep="last")].sort_index()
            try:
                write_frame(frame, self._stem(symbol))
                written += len(rows)
                self._completed[symbol] = []
            except Exception:
                # Keep the rows buffered and retry on the next flush rather
                # than losing a session's recording to a transient disk error.
                log.exception("failed writing bars for %s", symbol)
        return written

    def stop(self) -> None:
        self._stop.set()

    def run(self, duration_seconds: float | None = None) -> None:
        """Poll the RTD client until stopped. Blocks."""
        self.client.connect()
        try:
            self.client.subscribe(self.symbols, QUOTE_FIELDS)
        except RTDError:
            self.client.disconnect()
            raise

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.stop())
            except ValueError:
                # Not on the main thread — the caller owns shutdown instead.
                pass

        started = time.time()
        last_flush = started
        log.info("recording %s at %ds bars", ", ".join(self.symbols), self.interval_seconds)
        try:
            while not self._stop.is_set():
                now = datetime.now(timezone.utc)
                for symbol, quote in self.client.refresh().items():
                    price = quote.fields.get(FIELD_LAST)
                    if isinstance(price, (int, float)) and price > 0:
                        volume = quote.fields.get(FIELD_VOLUME)
                        self.ingest(
                            symbol,
                            float(price),
                            float(volume) if isinstance(volume, (int, float)) else None,
                            now,
                        )

                if time.time() - last_flush >= self.interval_seconds:
                    count = self.flush()
                    if count:
                        log.info("wrote %d bars", count)
                    last_flush = time.time()

                if duration_seconds and time.time() - started >= duration_seconds:
                    break
                self._stop.wait(self.poll_seconds)
        finally:
            # Bank the in-flight bars so a clean shutdown never loses the last
            # minute of the session.
            for symbol, current in self._building.items():
                self._completed.setdefault(symbol, []).append(current.finish())
            self._building.clear()
            self.flush()
            self.client.disconnect()
            log.info("recorder stopped")
