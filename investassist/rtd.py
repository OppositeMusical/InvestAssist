"""The thinkorswim RTD bridge.

thinkorswim ships an RTD (Real-Time Data) COM server on Windows — the same one
that backs ``=RTD("tos.rtd", , "LAST", "AAPL")`` in Excel. It is officially
documented by Schwab and needs no OAuth, no developer app, no approval wait and
no market-data subscription: a logged-in thinkorswim on the same machine simply
serves quotes to any local process that asks.

The field that makes this more than a quote feed is ``CUSTOM1``..``CUSTOM19``,
which expose the values computed by the custom-quote studies installed in
MarketWatch. Install ``thinkscript/investassist_column.ts`` into Custom 1 and
the score thinkorswim computes on-chart becomes readable from Python — the two
implementations meeting in the middle.

Operational notes, all of which come from Schwab's own guidance:

* Windows only, and thinkorswim must be running and logged in.
* Run one thinkorswim instance and one RTD consumer. Multiple consumers are
  documented as unreliable.
* This is a quote stream, not a history API. Bars come from ``data.py``; use
  ``recorder.py`` to build your own intraday history from this feed.

.. warning::
   ``WindowsRTDClient`` talks to COM and therefore cannot be exercised on the
   CI machine this was written on. It is written to the documented IRtdServer
   contract but needs a verification pass on a real Windows box with
   thinkorswim running before you trust it. ``MockRTDClient`` covers everything
   else, and the test suite runs against that.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

log = logging.getLogger(__name__)

# The subset of thinkorswim RTD fields this project uses. The server exposes
# many more; these are the ones the engine and recorder need.
FIELD_LAST = "LAST"
FIELD_BID = "BID"
FIELD_ASK = "ASK"
FIELD_VOLUME = "VOLUME"
FIELD_OPEN = "OPEN"
FIELD_HIGH = "HIGH"
FIELD_LOW = "LOW"
FIELD_CLOSE = "CLOSE"
FIELD_NET_CHANGE = "NET_CHANGE"

QUOTE_FIELDS = (FIELD_LAST, FIELD_BID, FIELD_ASK, FIELD_VOLUME, FIELD_HIGH, FIELD_LOW)

#: Custom-quote slot holding the thinkScript score. Match this to the slot you
#: pasted investassist_column.ts into.
FIELD_SCORE = "CUSTOM1"


class RTDError(RuntimeError):
    """The RTD server is unavailable or rejected a request."""


@dataclass
class Quote:
    symbol: str
    fields: dict[str, float | str | None] = field(default_factory=dict)
    updated: float = 0.0

    @property
    def last(self) -> float | None:
        value = self.fields.get(FIELD_LAST)
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def score(self) -> float | None:
        """The thinkScript score, if the custom-quote slot is wired up."""
        value = self.fields.get(FIELD_SCORE)
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def age(self) -> float:
        return time.time() - self.updated


class RTDClient(ABC):
    """A source of live quotes keyed by (field, symbol)."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def subscribe(self, symbols: Iterable[str], fields: Iterable[str]) -> None: ...

    @abstractmethod
    def refresh(self) -> dict[str, Quote]:
        """Pull the latest values. Returns the full quote map, keyed by symbol."""

    def __enter__(self) -> "RTDClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()


class _UpdateEvent:
    """Minimal ``IRTDUpdateEvent`` implementation for the COM server.

    The RTD protocol is push-notify/pull-read: the server calls ``UpdateNotify``
    when something changed, and the client then calls ``RefreshData`` to
    collect it. We only need to record that a refresh is due.
    """

    _public_methods_ = ["UpdateNotify", "Disconnect", "HeartbeatInterval"]
    _com_interfaces_ = ["IRTDUpdateEvent"]

    def __init__(self, on_update: Callable[[], None]):
        self._on_update = on_update
        self.HeartbeatInterval = -1

    def UpdateNotify(self) -> None:  # noqa: N802 - COM naming
        self._on_update()

    def Disconnect(self) -> None:  # noqa: N802 - COM naming
        log.info("RTD server requested disconnect")


class WindowsRTDClient(RTDClient):
    """RTD over COM against a running thinkorswim on Windows.

    .. warning:: Not exercised in CI — see the module docstring.
    """

    PROGID = "tos.rtd"

    def __init__(self, prog_id: str | None = None):
        self.prog_id = prog_id or self.PROGID
        self._server = None
        self._topics: dict[int, tuple[str, str]] = {}
        self._next_topic_id = 1
        self._quotes: dict[str, Quote] = {}
        self._dirty = threading.Event()
        self._dirty.set()

    def connect(self) -> None:
        try:
            import pythoncom  # noqa: F401
            import win32com.client
            from win32com.server.util import wrap
        except ImportError as exc:
            raise RTDError(
                "the RTD bridge needs pywin32 on Windows: pip install 'investassist[windows]'"
            ) from exc

        try:
            self._server = win32com.client.Dispatch(self.prog_id)
        except Exception as exc:
            raise RTDError(
                f"could not reach the '{self.prog_id}' COM server. "
                "Is thinkorswim running and logged in?"
            ) from exc

        callback = wrap(_UpdateEvent(self._dirty.set))
        result = self._server.ServerStart(callback)
        # The documented contract is that ServerStart returns a positive value
        # on success; anything else means the server refused us.
        if not result or int(result) < 1:
            self._server = None
            raise RTDError(f"RTD ServerStart refused the connection (returned {result!r})")
        log.info("connected to thinkorswim RTD server")

    def disconnect(self) -> None:
        if self._server is None:
            return
        for topic_id in list(self._topics):
            try:
                self._server.DisconnectData(topic_id)
            except Exception:
                # Best-effort teardown; a failure here must not mask whatever
                # the caller was actually shutting down for.
                log.debug("DisconnectData failed for topic %s", topic_id, exc_info=True)
        try:
            self._server.ServerTerminate()
        except Exception:
            log.debug("ServerTerminate failed", exc_info=True)
        self._server = None
        self._topics.clear()

    def subscribe(self, symbols: Iterable[str], fields: Iterable[str]) -> None:
        if self._server is None:
            raise RTDError("subscribe() called before connect()")
        existing = set(self._topics.values())
        for symbol in symbols:
            symbol = symbol.upper()
            self._quotes.setdefault(symbol, Quote(symbol))
            for field_name in fields:
                if (field_name, symbol) in existing:
                    continue
                topic_id = self._next_topic_id
                self._next_topic_id += 1
                try:
                    value = self._server.ConnectData(topic_id, (field_name, symbol), True)
                except Exception as exc:
                    raise RTDError(f"ConnectData failed for {field_name}/{symbol}: {exc}") from exc
                self._topics[topic_id] = (field_name, symbol)
                self._store(topic_id, value)
        self._dirty.set()

    def _store(self, topic_id: int, value) -> None:
        entry = self._topics.get(topic_id)
        if entry is None:
            return
        field_name, symbol = entry
        quote = self._quotes.setdefault(symbol, Quote(symbol))
        # thinkorswim reports pending or unavailable values as strings such as
        # "Loading" or "N/A"; those are not numbers and must not be coerced.
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                value = None if value.strip().upper() in ("N/A", "LOADING", "") else value
        quote.fields[field_name] = value
        quote.updated = time.time()

    def refresh(self) -> dict[str, Quote]:
        if self._server is None:
            raise RTDError("refresh() called before connect()")
        if not self._dirty.is_set():
            return dict(self._quotes)
        self._dirty.clear()

        try:
            count, data = self._server.RefreshData(len(self._topics))
        except Exception as exc:
            raise RTDError(f"RefreshData failed: {exc}") from exc

        # RefreshData returns a 2 x N array: row 0 topic ids, row 1 values.
        if data is not None and len(data) >= 2:
            topic_ids, values = data[0], data[1]
            for index in range(min(int(count), len(topic_ids))):
                topic_id = topic_ids[index]
                if topic_id is not None:
                    self._store(int(topic_id), values[index])
        return dict(self._quotes)


class MockRTDClient(RTDClient):
    """Deterministic stand-in for development off-Windows and for tests."""

    def __init__(self, prices: Mapping[str, float] | None = None, drift: float = 0.0):
        self._prices = {k.upper(): float(v) for k, v in (prices or {}).items()}
        self._drift = drift
        self._quotes: dict[str, Quote] = {}
        self._fields: tuple[str, ...] = QUOTE_FIELDS
        self._connected = False
        self.refresh_count = 0

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def subscribe(self, symbols: Iterable[str], fields: Iterable[str]) -> None:
        if not self._connected:
            raise RTDError("subscribe() called before connect()")
        self._fields = tuple(fields)
        for symbol in symbols:
            symbol = symbol.upper()
            self._prices.setdefault(symbol, 100.0)
            self._quotes.setdefault(symbol, Quote(symbol))

    def refresh(self) -> dict[str, Quote]:
        if not self._connected:
            raise RTDError("refresh() called before connect()")
        self.refresh_count += 1
        for symbol, quote in self._quotes.items():
            price = self._prices[symbol] + self._drift * self.refresh_count
            for name in self._fields:
                if name == FIELD_VOLUME:
                    quote.fields[name] = 1_000_000.0 * self.refresh_count
                elif name == FIELD_BID:
                    quote.fields[name] = round(price - 0.01, 4)
                elif name == FIELD_ASK:
                    quote.fields[name] = round(price + 0.01, 4)
                else:
                    quote.fields[name] = round(price, 4)
            quote.updated = time.time()
        return dict(self._quotes)


def build_client(prefer_mock: bool = False, **kwargs) -> RTDClient:
    """Return a Windows RTD client where possible, else the mock.

    Falling back rather than raising keeps the panel usable on a Mac or in CI;
    the caller is expected to surface which one it got.
    """
    import sys

    if prefer_mock or not sys.platform.startswith("win"):
        if not prefer_mock:
            log.warning("not on Windows — falling back to MockRTDClient (no live quotes)")
        return MockRTDClient(**kwargs)
    return WindowsRTDClient()
