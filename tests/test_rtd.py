"""RTD bridge and recorder tests.

Everything here runs against ``MockRTDClient``. The Windows COM path cannot be
exercised off Windows and is flagged as needing a verification pass on a real
machine — see the warning in investassist/rtd.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from investassist.recorder import BarRecorder
from investassist.storage import read_frame
from investassist.rtd import (
    FIELD_LAST,
    FIELD_SCORE,
    QUOTE_FIELDS,
    MockRTDClient,
    Quote,
    RTDError,
    build_client,
)


def test_mock_client_round_trip():
    client = MockRTDClient({"AAPL": 190.0})
    with client:
        client.subscribe(["AAPL"], QUOTE_FIELDS)
        quotes = client.refresh()

    assert "AAPL" in quotes
    assert quotes["AAPL"].last == pytest.approx(190.0)
    assert quotes["AAPL"].fields["BID"] < quotes["AAPL"].fields["ASK"]


def test_using_the_client_before_connecting_is_an_error():
    client = MockRTDClient()
    with pytest.raises(RTDError):
        client.subscribe(["AAPL"], QUOTE_FIELDS)
    with pytest.raises(RTDError):
        client.refresh()


def test_quote_ignores_non_numeric_fields():
    """thinkorswim reports pending values as strings like 'Loading'."""
    quote = Quote("AAPL", {FIELD_LAST: "Loading", FIELD_SCORE: 42.0})
    assert quote.last is None
    assert quote.score == pytest.approx(42.0)


def test_build_client_falls_back_off_windows():
    # The panel must stay usable on a Mac rather than refusing to start.
    assert isinstance(build_client(prefer_mock=True), MockRTDClient)


def test_recorder_aggregates_ticks_into_bars(tmp_path):
    recorder = BarRecorder(
        client=MockRTDClient(), symbols=["AAPL"], directory=tmp_path, interval_seconds=60
    )
    t0 = datetime(2026, 8, 10, 14, 30, 0, tzinfo=timezone.utc)

    recorder.ingest("AAPL", 100.0, 1000, t0)
    recorder.ingest("AAPL", 102.0, 1500, t0 + timedelta(seconds=10))
    recorder.ingest("AAPL", 99.0, 2000, t0 + timedelta(seconds=20))
    # Crossing the minute boundary closes the first bar.
    recorder.ingest("AAPL", 101.0, 2500, t0 + timedelta(seconds=61))

    assert recorder.flush() == 1

    frame = read_frame(tmp_path / "AAPL_60s")
    assert frame is not None
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["open"] == pytest.approx(100.0)
    assert row["high"] == pytest.approx(102.0)
    assert row["low"] == pytest.approx(99.0)
    assert row["close"] == pytest.approx(99.0)
    # RTD volume is session-cumulative, so a bar's volume is the delta.
    assert row["volume"] == pytest.approx(1000.0)


def test_recorder_flush_is_idempotent(tmp_path):
    recorder = BarRecorder(
        client=MockRTDClient(), symbols=["AAPL"], directory=tmp_path, interval_seconds=60
    )
    t0 = datetime(2026, 8, 10, 14, 30, 0, tzinfo=timezone.utc)
    recorder.ingest("AAPL", 100.0, 1000, t0)
    recorder.ingest("AAPL", 101.0, 1200, t0 + timedelta(seconds=61))
    recorder.flush()
    assert recorder.flush() == 0


def test_recorder_merges_across_sessions(tmp_path):
    t0 = datetime(2026, 8, 10, 14, 30, 0, tzinfo=timezone.utc)

    first = BarRecorder(client=MockRTDClient(), symbols=["AAPL"], directory=tmp_path)
    first.ingest("AAPL", 100.0, 1000, t0)
    first.ingest("AAPL", 101.0, 1200, t0 + timedelta(seconds=61))
    first.flush()

    second = BarRecorder(client=MockRTDClient(), symbols=["AAPL"], directory=tmp_path)
    second.ingest("AAPL", 105.0, 2000, t0 + timedelta(seconds=3600))
    second.ingest("AAPL", 106.0, 2200, t0 + timedelta(seconds=3661))
    second.flush()

    frame = read_frame(tmp_path / "AAPL_60s")
    assert frame is not None
    assert len(frame) == 2
    assert frame.index.is_monotonic_increasing


def test_recorder_run_banks_the_final_partial_bar(tmp_path):
    recorder = BarRecorder(
        client=MockRTDClient({"AAPL": 100.0}),
        symbols=["AAPL"],
        directory=tmp_path,
        interval_seconds=60,
        poll_seconds=0.01,
    )
    recorder.run(duration_seconds=0.05)

    frame = read_frame(tmp_path / "AAPL_60s")
    # A clean shutdown must not lose the in-flight minute.
    assert frame is not None and len(frame) >= 1


def test_recorder_falls_back_to_csv_without_pyarrow(tmp_path, monkeypatch):
    """Recording must never be lost to a missing optional dependency."""
    monkeypatch.setattr("investassist.storage.parquet_available", lambda: False)

    recorder = BarRecorder(
        client=MockRTDClient(), symbols=["AAPL"], directory=tmp_path, interval_seconds=60
    )
    t0 = datetime(2026, 8, 10, 14, 30, 0, tzinfo=timezone.utc)
    recorder.ingest("AAPL", 100.0, 1000, t0)
    recorder.ingest("AAPL", 101.0, 1200, t0 + timedelta(seconds=61))

    assert recorder.flush() == 1
    assert (tmp_path / "AAPL_60s.csv").exists()
    assert not (tmp_path / "AAPL_60s.parquet").exists()

    frame = read_frame(tmp_path / "AAPL_60s")
    assert frame is not None and len(frame) == 1
