"""Dropped-event accounting: silent data loss becomes a visible number.

A dropped event is permanent loss. The block cursor and the sync watermark both
advance past it, so nothing re-requests it, and cost basis, fees and yields are
all quietly wrong afterwards with nothing on any report saying so. It took a
hand audit of the ledger to find the 49 peaq events a blockTimestamp bug ate
over five days in July 2026.

These tests pin the three properties that make the counter trustworthy:
  * only LOSSY drops count — an event carrying nothing to record is not a loss
  * the count resets per scan, so it means "the last run", not "ever"
  * a clean run can zero it in storage (unlike the *_ok_at columns, which
    deliberately keep their old value)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain

WALLET = "0x1111111111111111111111111111111111111111"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "drops.db")
    s.init_schema()
    return s


# ── Storage: the clobber-vs-keep asymmetry ────────────────────────────────


def _record(s: Storage, *, dropped: int, sync_ok: bool = True) -> dict:
    s.record_pipeline_health(
        WALLET,
        Chain.BSC,
        "pancake_infinity",
        now=1_786_000_000,
        sync_ok=sync_ok,
        snapshot_ok=True,
        source="subgraph",
        error=None,
        dropped_events=dropped,
    )
    return s.pipeline_health()[0]


class TestPersistence:
    def test_a_clean_run_zeroes_the_count(self, storage: Storage):
        """The whole point of last-run semantics. If this column followed the
        COALESCE-keeps-old rule the *_ok_at columns use, one bad run in July
        would nag forever with no way to clear it."""
        assert _record(storage, dropped=3)["dropped_events_last_run"] == 3
        assert _record(storage, dropped=0)["dropped_events_last_run"] == 0

    def test_timestamps_still_keep_their_old_value(self, storage: Storage):
        """Guards against 'fixing' the asymmetry by making everything clobber:
        a failing stage must still pin its timestamp."""
        first = _record(storage, dropped=0, sync_ok=True)
        assert first["last_sync_ok_at"] is not None
        after_failure = _record(storage, dropped=1, sync_ok=False)
        assert after_failure["last_sync_ok_at"] == first["last_sync_ok_at"]
        assert after_failure["dropped_events_last_run"] == 1

    def test_defaults_to_zero_for_callers_that_omit_it(self, storage: Storage):
        storage.record_pipeline_health(
            WALLET,
            Chain.BSC,
            "pancake_infinity",
            now=1_786_000_000,
            sync_ok=True,
            snapshot_ok=True,
            source=None,
            error=None,
        )
        assert storage.pipeline_health()[0]["dropped_events_last_run"] == 0


# ── Adapter bookkeeping ───────────────────────────────────────────────────


class _Adapter:
    """Minimal stand-in exercising BaseAdapter's counter without a network."""

    def __init__(self, tmp_path: Path):
        from defi_tracker.adapters._base import BaseAdapter

        class _Stub(BaseAdapter):
            @property
            def info(self):  # pragma: no cover - unused
                raise NotImplementedError

            def fetch_events(self, wallet, chain, since_ts=0):
                return []

            def fetch_positions(self, wallet, chain):
                return []

        self.adapter = _Stub(db_path=str(tmp_path / "a.db"))


class TestAdapterCounter:
    def test_counts_and_is_readable(self, tmp_path: Path):
        a = _Adapter(tmp_path).adapter
        a._begin_event_scan(WALLET, Chain.BSC)
        a._note_dropped_event(WALLET, Chain.BSC, "no price", "0xdead")
        a._note_dropped_event(WALLET, Chain.BSC, "no timestamp", "0xbeef")
        assert a.last_scan_dropped_events(WALLET, Chain.BSC) == 2

    def test_resets_per_scan(self, tmp_path: Path):
        """'Dropped' means the last run. A counter that accumulated across runs
        would keep reporting July's losses every morning."""
        a = _Adapter(tmp_path).adapter
        a._begin_event_scan(WALLET, Chain.BSC)
        a._note_dropped_event(WALLET, Chain.BSC, "no price")
        a._begin_event_scan(WALLET, Chain.BSC)
        assert a.last_scan_dropped_events(WALLET, Chain.BSC) == 0

    def test_isolated_per_wallet_and_chain(self, tmp_path: Path):
        a = _Adapter(tmp_path).adapter
        other = "0x2222222222222222222222222222222222222222"
        a._begin_event_scan(WALLET, Chain.BSC)
        a._begin_event_scan(other, Chain.BSC)
        a._note_dropped_event(WALLET, Chain.BSC, "no price")
        assert a.last_scan_dropped_events(WALLET, Chain.BSC) == 1
        assert a.last_scan_dropped_events(other, Chain.BSC) == 0
        assert a.last_scan_dropped_events(WALLET, Chain.PEAQ) == 0

    def test_unscanned_pair_reports_zero(self, tmp_path: Path):
        a = _Adapter(tmp_path).adapter
        assert a.last_scan_dropped_events(WALLET, Chain.BSC) == 0

    def test_the_template_resets_without_the_adapter_remembering(self, tmp_path: Path):
        """iter_event_batches is the reset point precisely so each adapter does
        not have to invent one — that is where the drift would be."""
        a = _Adapter(tmp_path).adapter
        a._begin_event_scan(WALLET, Chain.BSC)
        a._note_dropped_event(WALLET, Chain.BSC, "stale from a previous scan")
        list(a.iter_event_batches(WALLET, Chain.BSC))
        assert a.last_scan_dropped_events(WALLET, Chain.BSC) == 0
