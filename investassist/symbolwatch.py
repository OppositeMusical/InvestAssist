"""Symbol-context detection: learning which stock you are looking at.

thinkorswim has no outbound API and will not report its UI state, so the active
symbol has to be bridged. This module implements the two most robust options
from the plan — clipboard watching and a Windows global hotkey. Both are
strictly read-only. Neither automates order entry, and nothing here clicks
anything in thinkorswim.

Ranked alternatives, if you outgrow these: reading the symbol out of a detached
chart window's title (cheap, worth a one-hour spike on your layout), OCR of the
symbol field, and the Java Access Bridge. The first two are covered in the
README; all of them are fallbacks for the same one-line interface below.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

log = logging.getLogger(__name__)

# US listed tickers: 1-5 letters, optionally a class suffix (BRK.B, RDS-A).
# thinkorswim prefixes futures with '/' and options are longer strings; both
# are rejected because this build is stocks and ETFs only.
_TICKER = re.compile(r"^[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?$")

# Words that pass the shape test but are almost certainly not what you copied.
# Without this, copying a sentence fragment silently rescores the panel.
_COMMON_FALSE_POSITIVES = frozenset(
    {"A", "I", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "IF", "IN", "IS", "IT",
     "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US", "WE", "AND", "ARE",
     "BUT", "CAN", "FOR", "HAD", "HAS", "HER", "HIM", "HIS", "HOW", "ITS",
     "NOT", "NOW", "ONE", "OUR", "OUT", "SEE", "SHE", "THE", "TOO", "TWO",
     "USE", "WAS", "WHO", "WHY", "YOU", "THAT", "THIS", "WITH", "FROM",
     "HAVE", "WILL", "YOUR", "THEY", "BEEN", "WERE", "WHEN", "SOME"}
)


def normalise_ticker(text: str | None, allowlist: frozenset[str] | None = None) -> str | None:
    """Extract a plausible ticker from copied text, or ``None``.

    ``allowlist`` — typically your watchlist universe — turns this from a
    heuristic into an exact check, and is strongly preferred once you have one.
    """
    if not text:
        return None
    candidate = text.strip().upper()
    # thinkorswim copies some symbols with a leading $ or trailing exchange tag.
    candidate = candidate.lstrip("$").split()[0] if candidate.split() else ""
    if not candidate or not _TICKER.match(candidate):
        return None
    if allowlist is not None:
        return candidate if candidate in allowlist else None
    if candidate in _COMMON_FALSE_POSITIVES:
        return None
    return candidate


class ClipboardReader(Protocol):
    def __call__(self) -> str | None: ...


def tk_clipboard_reader(root) -> ClipboardReader:
    """Clipboard access via an existing Tk root — no extra dependency."""

    def read() -> str | None:
        try:
            return root.clipboard_get()
        except Exception:
            # Empty clipboard, or it holds something that is not text.
            return None

    return read


@dataclass
class ClipboardWatcher:
    """Polls the clipboard and reports newly copied tickers.

    Copy a symbol in thinkorswim and the panel follows along. Deliberately
    dependency-free and stateless beyond the last value seen, which makes it
    the one detection mechanism that cannot break when Schwab reskins the
    platform.
    """

    read: ClipboardReader
    on_symbol: Callable[[str], None]
    allowlist: frozenset[str] | None = None
    _last_raw: str | None = None
    _last_symbol: str | None = None

    def poll(self) -> str | None:
        """Check once. Returns the symbol if it changed, else ``None``."""
        raw = self.read()
        if raw is None or raw == self._last_raw:
            return None
        self._last_raw = raw

        symbol = normalise_ticker(raw, self.allowlist)
        if symbol is None or symbol == self._last_symbol:
            return None

        self._last_symbol = symbol
        try:
            self.on_symbol(symbol)
        except Exception:
            # A failure inside the callback must not kill the polling loop —
            # the panel stays up and shows the error instead.
            log.exception("symbol callback failed for %s", symbol)
        return symbol

    def reset(self) -> None:
        self._last_raw = None
        self._last_symbol = None


class WindowsHotkey:
    """Global hotkey via ``RegisterHotKey``, so detection is explicit.

    Preferred over continuous clipboard polling when you would rather press a
    key than have the panel react to everything you copy. Windows only; on
    other platforms ``start()`` raises and the caller should fall back to
    :class:`ClipboardWatcher`.

    .. warning:: Like the RTD bridge this needs pywin32 and cannot be
       exercised off Windows.
    """

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    WM_HOTKEY = 0x0312

    def __init__(self, on_press: Callable[[], None], modifiers: int | None = None, key: str = "I"):
        self.on_press = on_press
        self.modifiers = modifiers if modifiers is not None else (self.MOD_CONTROL | self.MOD_SHIFT)
        self.key = key
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        try:
            import win32con  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "global hotkeys need pywin32 on Windows: pip install 'investassist[windows]'"
            ) from exc
        self._thread = threading.Thread(target=self._run, name="investassist-hotkey", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import win32api
        import win32con
        import win32gui

        hotkey_id = 1
        if not win32gui.RegisterHotKey(None, hotkey_id, self.modifiers, ord(self.key)):
            log.error("could not register hotkey — another app probably owns it")
            return
        log.info("hotkey registered: ctrl+shift+%s", self.key)
        try:
            while not self._stop.is_set():
                rc, message = win32gui.PeekMessage(None, 0, 0, win32con.PM_REMOVE)
                if rc and message[1] == self.WM_HOTKEY:
                    try:
                        self.on_press()
                    except Exception:
                        log.exception("hotkey callback failed")
                win32api.Sleep(50)
        finally:
            win32gui.UnregisterHotKey(None, hotkey_id)
