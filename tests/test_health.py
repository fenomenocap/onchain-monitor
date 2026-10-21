"""
Pipeline liveness: the record that says when the pipeline last actually
worked, and the evidence columns that separate a real realized loss from an
adapter that quietly stopped reporting.

The bug these guard against: BSC event ingestion died on 2026-07-06 while
snapshots kept looking fresh (RPC fallback), and nothing anywhere recorded
that the sync stage had been failing for 39 days.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from defi_tracker.core.adapter import ProtocolAdapter
from defi_tracker.core.runner import TrackerRunner
from defi_tracker.core.types import (
    AdapterInfo,
    Chain,
    EventKind,
    ProtocolKind,
)
from tests.conftest import TEST_WALLET

PID = "pancake_infinity"  # matches make_event / make_position defaults
POSITION_KEY = "pool_X:tick_lo:tick_hi"


# ─────────────────────────────────────────────────────────────────────────
# record_pipeline_health / pipeline_health
# ─────────────────────────────────────────────────────────────────────────


class TestRecordPipelineHealth:
    def _row(self, storage) -> dict:
        rows = storage.pipeline_health()
        assert len(rows) == 1
        return rows[0]

    def test_failing_pass_advances_last_run_but_not_sync_ok(self, storage):
        storage.record_pipeline_health(
            TEST_WALLET,
            Chain.BSC,
            PID,
            now=1_000,
            sync_ok=True,
            snapshot_ok=True,
            source="subgraph",
            error=None,
        )
        # A later pass where the sync failed but the RPC fallback kept
        # snapshots fresh — exactly the silent-degradation case.
        storage.record_pipeline_health(
            TEST_WALLET,
            Chain.BSC,
            PID,
            now=2_000,
            sync_ok=False,
            snapshot_ok=True,
            source="rpc_fallback",
            error="event sync failed: subgraph returned no data",
        )

        row = self._row(storage)
        assert row["last_run_at"] == 2_000
        assert row["last_sync_ok_at"] == 1_000  # stuck: the signal
        assert row["last_snapshot_ok_at"] == 2_000

    def test_recovery_advances_both_timestamps(self, storage):
        storage.record_pipeline_health(
            TEST_WALLET,
            Chain.BSC,
            PID,
            now=1_000,
            sync_ok=False,
            snapshot_ok=False,
            source=None,
            error="boom",
        )
        row = self._row(storage)
        assert row["last_run_at"] == 1_000
        assert row["last_sync_ok_at"] is None
        assert row["last_snapshot_ok_at"] is None

        storage.record_pipeline_health(
            TEST_WALLET,
            Chain.BSC,
            PID,
            now=3_000,
            sync_ok=True,
            snapshot_ok=True,
            source="subgraph",
            error=None,
        )
        row = self._row(storage)
        assert row["last_run_at"] == 3_000
        assert row["last_sync_ok_at"] == 3_000
        assert row["last_snapshot_ok_at"] == 3_000

    def test_source_and_error_overwrite_every_pass(self, storage):
        storage.record_pipeline_health(
            TEST_WALLET,
            Chain.BSC,
            PID,
            now=1_000,
            sync_ok=False,
            snapshot_ok=True,
            source="rpc_fallback",
            error="subgraph down",
        )
        assert self._row(storage)["source"] == "rpc_fallback"
        assert self._row(storage)["last_error"] == "subgraph down"

        storage.record_pipeline_health(
            TEST_WALLET,
            Chain.BSC,
            PID,
            now=2_000,
            sync_ok=True,
            snapshot_ok=True,
            source="subgraph",
            error=None,
        )
        row = self._row(storage)
        assert row["source"] == "subgraph"
        assert row["last_error"] is None  # cleared on recovery

    def test_rows_are_per_adapter_and_sorted(self, storage):
        for chain, pid in (
            (Chain.BSC, "pancake_infinity"),
            (Chain.BASE, "uniswap_v3"),
            (Chain.BSC, "aerodrome"),
        ):
            storage.record_pipeline_health(
                TEST_WALLET,
                chain,
                pid,
                now=1_000,
                sync_ok=True,
                snapshot_ok=True,
                source=None,
                error=None,
            )

        rows = storage.pipeline_health()
        assert [(r["chain"], r["protocol_id"]) for r in rows] == [
            ("base", "uniswap_v3"),
            ("bsc", "aerodrome"),
            ("bsc", "pancake_infinity"),
        ]

    def test_wallet_is_lowercased(self, storage):
        storage.record_pipeline_health(
            TEST_WALLET.upper(),
            Chain.BSC,
            PID,
            now=1_000,
            sync_ok=True,
            snapshot_ok=True,
            source=None,
            error=None,
        )
        assert self._row(storage)["wallet"] == TEST_WALLET.lower()


# ─────────────────────────────────────────────────────────────────────────
# Runner writes health rows every pass
# ─────────────────────────────────────────────────────────────────────────


class _HealthStubAdapter(ProtocolAdapter):
    """Minimal adapter: optional positions, optionally raising sync/fetch."""

    def __init__(self, positions=None, sync_raises=False, fetch_raises=False):
        self._positions = positions or []
        self._sync_raises = sync_raises
        self._fetch_raises = fetch_raises

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            protocol_id=PID,
            display_name="Stub",
            chains=[Chain.BSC],
            protocol_kind=ProtocolKind.CL_AMM,
            supports_exact_fees=True,
            supports_historical=True,
        )

    def iter_event_batches(self, wallet, chain, since_ts=0, deadline=None):
        if self._sync_raises:
            raise RuntimeError("subgraph returned no data")
        yield []

    def fetch_events(self, wallet, chain, since_ts=0):
        return []

    def fetch_positions(self, wallet, chain):
        if self._fetch_raises:
            raise RuntimeError("rpc unreachable")
        return self._positions


@pytest.fixture
def only_stub_adapter(monkeypatch):
    """Point the runner's adapter registry at a single stub."""

    def _install(adapter):
        monkeypatch.setattr("defi_tracker.core.runner.all_adapters", lambda: [adapter])
        monkeypatch.setattr(
            "defi_tracker.core.runner.adapters_for_chain",
            lambda chain: [adapter] if chain is Chain.BSC else [],
        )
        return adapter

    return _install


class TestRunnerRecordsHealth:
    def test_success_path_records_clean_row(
        self, storage_with_wallet, make_position, only_stub_adapter
    ):
        pos = make_position()
        pos.meta = {"source": "subgraph"}
        only_stub_adapter(_HealthStubAdapter(positions=[pos]))

        summary = TrackerRunner(storage_with_wallet).run()

        rows = storage_with_wallet.pipeline_health()
        assert len(rows) == 1
        row = rows[0]
        assert row["chain"] == "bsc" and row["protocol_id"] == PID
        assert row["last_run_at"] > 0
        assert row["last_sync_ok_at"] == row["last_run_at"]
        assert row["last_snapshot_ok_at"] == row["last_run_at"]
        assert row["source"] == "subgraph"
        assert row["last_error"] is None
        # The CLI renders this key — it is a contract
        assert summary["health"] == rows

    def test_sync_failure_path_records_error_and_fallback_source(
        self, storage_with_wallet, make_position, only_stub_adapter
    ):
        pos = make_position()
        pos.meta = {"source": "rpc_fallback"}
        only_stub_adapter(_HealthStubAdapter(positions=[pos], sync_raises=True))

        TrackerRunner(storage_with_wallet).run()

        row = storage_with_wallet.pipeline_health()[0]
        assert row["last_sync_ok_at"] is None  # never synced cleanly
        assert row["last_snapshot_ok_at"] == row["last_run_at"]  # marks still fresh
        assert row["source"] == "rpc_fallback"
        assert row["last_error"] is not None and "no data" in row["last_error"]

    def test_snapshot_failure_path_records_both_stages_failed(
        self, storage_with_wallet, only_stub_adapter
    ):
        only_stub_adapter(_HealthStubAdapter(fetch_raises=True))

        TrackerRunner(storage_with_wallet).run()

        row = storage_with_wallet.pipeline_health()[0]
        assert row["last_run_at"] > 0
        assert row["last_sync_ok_at"] is None
        assert row["last_snapshot_ok_at"] is None
        assert row["source"] is None
        assert "rpc unreachable" in row["last_error"]

    def test_sync_ok_timestamp_persists_across_a_later_failure(
        self, storage_with_wallet, make_position, only_stub_adapter, monkeypatch
    ):
        """The 39-day case: one good pass, then failures forever. The stored
        last_sync_ok_at must stay pinned to the good pass."""
        pos = make_position()
        only_stub_adapter(_HealthStubAdapter(positions=[pos]))
        TrackerRunner(storage_with_wallet).run()
        good_ts = storage_with_wallet.pipeline_health()[0]["last_sync_ok_at"]
        assert good_ts is not None

        only_stub_adapter(_HealthStubAdapter(positions=[pos], sync_raises=True))
        TrackerRunner(storage_with_wallet).run()

        row = storage_with_wallet.pipeline_health()[0]
        assert row["last_sync_ok_at"] == good_ts
        assert row["source"] is None  # this pass's positions declared none


# ─────────────────────────────────────────────────────────────────────────
# position_flows evidence columns
# ─────────────────────────────────────────────────────────────────────────


class TestPositionFlowsEvidence:
    def test_deposit_only_position_has_zero_withdraws_and_a_label(self, storage, make_event):
        """The $26k Base/uniswap_v3 case: a deposit, no snapshot (adapter
        disabled), no withdraw. It must not look like a clean realized loss."""
        storage.upsert_events([make_event(kind=EventKind.DEPOSIT, tx_hash="0x01")])

        rows = storage.position_flows()
        assert len(rows) == 1
        row = rows[0]
        assert row["withdraw_count"] == 0
        assert row["is_open"] == 0  # no live snapshot → booked as 'closed'
        assert row["pair_label"] is not None
        # Falls back to the event's token symbols, never the bare key here
        assert row["pair_label"] == "USDT/ETH"
        assert row["net_invested_usd"] == pytest.approx(2500.0)

    def test_label_falls_back_to_position_key_without_symbols(self, storage, make_event):
        storage.upsert_events([make_event(tx_hash="0x01")])
        # Simulate a legacy row whose amounts_json predates the symbol field
        with storage.connect() as conn:
            conn.execute("UPDATE events SET amounts_json = '[]'")

        assert storage.position_flows()[0]["pair_label"] == POSITION_KEY

    def test_open_position_prefers_snapshot_label(self, storage, make_event, make_position):
        storage.upsert_events([make_event(tx_hash="0x01")])
        storage.write_snapshot(position=make_position())

        row = storage.position_flows()[0]
        assert row["is_open"] == 1
        assert row["pair_label"] == "USDT/ETH 0.05%"  # snapshot wins over symbols
        assert row["withdraw_count"] == 0

    def test_closed_position_counts_withdraws(self, storage, make_event, make_position):
        storage.upsert_events(
            [
                make_event(kind=EventKind.DEPOSIT, tx_hash="0x01", ts=1_714_521_600),
                make_event(
                    kind=EventKind.WITHDRAW,
                    tx_hash="0x02",
                    ts=1_714_608_000,
                    usdt_amount=Decimal("-600"),
                    weth_amount=Decimal("-0.3"),
                ),
                make_event(
                    kind=EventKind.WITHDRAW,
                    tx_hash="0x03",
                    ts=1_714_694_400,
                    usdt_amount=Decimal("-100"),
                    weth_amount=Decimal("-0.05"),
                ),
                make_event(
                    kind=EventKind.COLLECT,
                    tx_hash="0x04",
                    ts=1_714_694_400,
                    usdt_amount=Decimal("50"),
                    weth_amount=Decimal("0"),
                ),
            ]
        )
        # Snapshot then tombstone → genuinely closed
        storage.write_snapshot(position=make_position())
        storage.write_tombstone_snapshot(
            wallet=TEST_WALLET,
            chain=Chain.BSC,
            protocol_id=PID,
            position_key=POSITION_KEY,
            pair_label="USDT/ETH 0.05%",
            protocol_kind=ProtocolKind.CL_AMM.value,
        )

        row = storage.position_flows()[0]
        assert row["withdraw_count"] == 2
        assert row["is_open"] == 0
        assert row["pair_label"] == "USDT/ETH 0.05%"
        assert row["fees_usd"] == pytest.approx(50.0)
        # 2500 deposited − (1500 + 250) withdrawn
        assert row["net_invested_usd"] == pytest.approx(750.0)
