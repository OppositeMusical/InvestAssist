"""Symbol-context detection tests."""

from __future__ import annotations

import pytest

from investassist.symbolwatch import ClipboardWatcher, normalise_ticker


@pytest.mark.parametrize(
    "text,expected",
    [
        ("AAPL", "AAPL"),
        ("  msft  ", "MSFT"),
        ("$TSLA", "TSLA"),
        ("BRK.B", "BRK.B"),
        ("RDS-A", "RDS-A"),
        ("AAPL extra words", "AAPL"),
    ],
)
def test_accepts_plausible_tickers(text, expected):
    assert normalise_ticker(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "   ",
        "TOOLONGSYMBOL",
        "/ES",            # futures are out of scope for this build
        ".SPX240119C500", # option symbols are not tickers
        "123",
        "the",            # a copied word that happens to look like a ticker
        "AND",
    ],
)
def test_rejects_non_tickers(text):
    assert normalise_ticker(text) is None


def test_allowlist_turns_the_heuristic_into_an_exact_check():
    universe = frozenset({"AAPL", "MSFT"})
    assert normalise_ticker("AAPL", universe) == "AAPL"
    # ON is a real ticker but not in this universe, and IT would otherwise be
    # rejected as a common word — the allowlist decides both cases.
    assert normalise_ticker("ON", universe) is None
    assert normalise_ticker("IT", frozenset({"IT"})) == "IT"


def test_watcher_fires_once_per_new_symbol():
    seen = []
    clipboard = {"value": "AAPL"}
    watcher = ClipboardWatcher(
        read=lambda: clipboard["value"], on_symbol=seen.append
    )

    assert watcher.poll() == "AAPL"
    assert watcher.poll() is None       # unchanged clipboard, no repeat
    clipboard["value"] = "MSFT"
    assert watcher.poll() == "MSFT"
    assert seen == ["AAPL", "MSFT"]


def test_watcher_ignores_junk_between_tickers():
    clipboard = {"value": "AAPL"}
    seen = []
    watcher = ClipboardWatcher(read=lambda: clipboard["value"], on_symbol=seen.append)

    watcher.poll()
    clipboard["value"] = "some copied sentence that is not a ticker"
    assert watcher.poll() is None
    assert seen == ["AAPL"]


def test_watcher_survives_a_failing_callback():
    """A broken callback must not kill the polling loop and take the panel down."""

    def explode(symbol):
        raise RuntimeError("scoring blew up")

    watcher = ClipboardWatcher(read=lambda: "AAPL", on_symbol=explode)
    assert watcher.poll() == "AAPL"


def test_watcher_handles_an_unreadable_clipboard():
    watcher = ClipboardWatcher(read=lambda: None, on_symbol=lambda s: None)
    assert watcher.poll() is None
