"""
End-to-end smoke test exercising the full pipeline with synthetic data.

This is the floor of test coverage — if this passes, the core abstractions
are intact. It does NOT hit any external services (no RPC, no subgraph,
no CoinGecko). Per-adapter tests with mocked HTTP go in
`tests/adapters/test_<name>.py` as adapters are built out.

Reusable fixtures live in `tests/conftest.py`:
  - `storage`, `storage_with_wallet`  → fresh SQLite per test
  - `make_event`, `make_position`     → factory fixtures with sensible defaults
  - `usdt_bsc`, `weth_bsc`            → reusable Token instances
  - `current_prices_usdt_weth`        → a 'now' price map
  - `TEST_WALLET`                     → deterministic test address
"""

from __future__ import annotations

from decimal import Decimal

from defi_tracker.alerts import AlertConfig, evaluate_alerts
from defi_tracker.analytics import PnLBreakdown, compute_cost_basis, compute_pnl
from defi_tracker.core.adapter import ProtocolAdapter
from defi_tracker.core.types import (
    AdapterInfo,
    Alert,
    AlertSeverity,
    Chain,
    Event,
    EventKind,
    ProtocolKind,
)
from tests.conftest import TEST_WALLET

# ─────────────────────────────────────────────────────────────────────────
# Storage layer
# ─────────────────────────────────────────────────────────────────────────


class TestStorage:
    def test_init_idempotent(self, storage):
        from defi_tracker.core.storage import SCHEMA_VERSION

        # Second call must not raise; schema_version stays current
        storage.init_schema()
        assert storage.schema_version() == SCHEMA_VERSION

    def test_wallet_registry_normalizes_and_dedupes(self, storage):
        storage.add_wallet("0xAAA", label="hot")
        storage.add_wallet("0xBBB", label="cold")
        storage.add_wallet("0xAAA", label="dup")  # should be ignored
        wallets = storage.list_wallets()
        assert len(wallets) == 2
        # Addresses are normalized to lowercase
        assert {w["address"] for w in wallets} == {"0xaaa", "0xbbb"}

    def test_event_idempotency(self, storage_with_wallet, make_event):
        evs = [
            make_event(tx_hash="0xhash1", ts=1714521600),
            make_event(
                tx_hash="0xhash2",
                ts=1717113600,
                usdt_amount=Decimal("500"),
                weth_amount=Decimal("0.2"),
                weth_price=Decimal("3500"),
            ),
        ]
        assert storage_with_wallet.upsert_events(evs) == 2
        # Re-inserting must be a no-op
        assert storage_with_wallet.upsert_events(evs) == 0


# ─────────────────────────────────────────────────────────────────────────
# Cost basis derivation from event history
# ─────────────────────────────────────────────────────────────────────────


class TestCostBasis:
    def test_sum_of_deposits(self, storage_with_wallet, make_event, usdt_bsc, weth_bsc):
        # Two deposits + one collect (collect must NOT affect cost basis)
        storage_with_wallet.upsert_events(
            [
                make_event(
                    tx_hash="0xd1",
                    ts=1714521600,
                    usdt_amount=Decimal("1000"),
                    weth_amount=Decimal("0.5"),
                    weth_price=Decimal("3000"),
                ),
                make_event(
                    tx_hash="0xd2",
                    ts=1717113600,
                    usdt_amount=Decimal("500"),
                    weth_amount=Decimal("0.2"),
                    weth_price=Decimal("3500"),
                ),
                make_event(
                    tx_hash="0xc1",
                    ts=1719792000,
                    kind=EventKind.COLLECT,
                    usdt_amount=Decimal("15"),
                    weth_amount=Decimal("0.005"),
                    weth_price=Decimal("3200"),
                ),
            ]
        )
        cb = compute_cost_basis(
            storage_with_wallet,
            TEST_WALLET,
            Chain.BSC,
            "pancake_infinity",
            "pool_X:tick_lo:tick_hi",
        )
        # 2500 (dep1) + 1200 (dep2), COLLECT is excluded from cost basis
        assert cb.cost_basis_usd == Decimal("3700")
        # Net tokens
        assert cb.net_tokens[usdt_bsc.key] == Decimal("1500")
        assert cb.net_tokens[weth_bsc.key] == Decimal("0.7")
        # Fees tracked separately
        assert cb.fees_collected_usd == Decimal("31")


# ─────────────────────────────────────────────────────────────────────────
# PnL and IL math (protocol-agnostic, dispatches on ProtocolKind)
# ─────────────────────────────────────────────────────────────────────────


class TestPnL:
    def test_il_negative_when_lp_underperforms_hodl(
        self,
        storage_with_wallet,
        make_event,
        make_position,
        current_prices_usdt_weth,
    ):
        # Two deposits totaling $3700, 1500 USDT and 0.7 ETH net
        storage_with_wallet.upsert_events(
            [
                make_event(
                    tx_hash="0xd1",
                    ts=1714521600,
                    usdt_amount=Decimal("1000"),
                    weth_amount=Decimal("0.5"),
                    weth_price=Decimal("3000"),
                ),
                make_event(
                    tx_hash="0xd2",
                    ts=1717113600,
                    usdt_amount=Decimal("500"),
                    weth_amount=Decimal("0.2"),
                    weth_price=Decimal("3500"),
                ),
            ]
        )
        pos = make_position(current_value_usd=Decimal("3400"))
        cb = compute_cost_basis(
            storage_with_wallet, TEST_WALLET, Chain.BSC, pos.protocol_id, pos.position_key
        )
        pnl = compute_pnl(pos, cb, current_prices_usdt_weth)

        # HODL value = 1500*$1 + 0.7*$3200 = $3740
        assert pnl.hold_value_usd == Decimal("3740")
        # IL = current ($3400) - hodl ($3740) = -$340
        assert pnl.il_usd == Decimal("-340")
        assert pnl.il_pct < 0

    def test_lending_position_has_zero_il(
        self,
        storage_with_wallet,
        make_event,
        make_position,
        usdt_bsc,
    ):
        # Lending positions: no IL concept regardless of token price moves
        storage_with_wallet.upsert_events(
            [
                make_event(
                    kind=EventKind.DEPOSIT,
                    tx_hash="0xlend1",
                    protocol_id="aave_v3",
                    position_key="aave:USDT",
                    usdt_amount=Decimal("1000"),
                    weth_amount=Decimal("0"),
                    weth_price=Decimal("0"),
                ),
            ]
        )
        pos = make_position(
            protocol_id="aave_v3",
            position_key="aave:USDT",
            protocol_kind=ProtocolKind.LENDING,
            pair_label="USDT Supply",
            tokens=[usdt_bsc],
            current_value_usd=Decimal("1050"),
        )
        cb = compute_cost_basis(storage_with_wallet, TEST_WALLET, Chain.BSC, "aave_v3", "aave:USDT")
        pnl = compute_pnl(pos, cb, {usdt_bsc.key: Decimal("1.0")})

        # No IL for lending kinds — pnl is pure interest accrual
        assert pnl.il_usd == Decimal("0")
        # PnL = current + unclaimed (45) + fees (0) - cost_basis (1000) = 95
        # Wait — unclaimed is 45 from the default fixture; current is 1050
        # 1050 + 45 + 0 - 1000 = 95
        assert pnl.pnl_usd == Decimal("95")


# ─────────────────────────────────────────────────────────────────────────
# Alert engine
# ─────────────────────────────────────────────────────────────────────────


class TestAlerts:
    def _setup_position_with_basis(
        self,
        storage,
        make_event,
        make_position,
        *,
        current_value,
        tick_current=205000,
        in_range=True,
    ):
        storage.upsert_events(
            [
                make_event(
                    tx_hash="0xd1",
                    ts=1714521600,
                    usdt_amount=Decimal("1000"),
                    weth_amount=Decimal("0.5"),
                    weth_price=Decimal("3000"),
                ),
                make_event(
                    tx_hash="0xd2",
                    ts=1717113600,
                    usdt_amount=Decimal("500"),
                    weth_amount=Decimal("0.2"),
                    weth_price=Decimal("3500"),
                ),
            ]
        )
        return make_position(
            current_value_usd=current_value,
            tick_current=tick_current,
            in_range=in_range,
        )

    def test_out_of_range_triggers_high(
        self,
        storage_with_wallet,
        make_event,
        make_position,
        current_prices_usdt_weth,
    ):
        pos = self._setup_position_with_basis(
            storage_with_wallet,
            make_event,
            make_position,
            current_value=Decimal("3400"),
            tick_current=215000,  # above upper bound
            in_range=False,
        )
        cb = compute_cost_basis(
            storage_with_wallet, TEST_WALLET, Chain.BSC, pos.protocol_id, pos.position_key
        )
        pnl = compute_pnl(pos, cb, current_prices_usdt_weth)
        kinds = {a.kind for a in evaluate_alerts(pos, pnl, AlertConfig())}
        assert "OUT_OF_RANGE" in kinds

    def test_near_edge_triggers_within_threshold(
        self,
        storage_with_wallet,
        make_event,
        make_position,
        current_prices_usdt_weth,
    ):
        # tick 209500 in range [200000, 210000] → 5% from upper edge
        pos = self._setup_position_with_basis(
            storage_with_wallet,
            make_event,
            make_position,
            current_value=Decimal("3400"),
            tick_current=209500,
        )
        cb = compute_cost_basis(
            storage_with_wallet, TEST_WALLET, Chain.BSC, pos.protocol_id, pos.position_key
        )
        pnl = compute_pnl(pos, cb, current_prices_usdt_weth)
        kinds = {
            a.kind
            for a in evaluate_alerts(
                pos,
                pnl,
                AlertConfig(edge_proximity_pct=Decimal("15")),
            )
        }
        assert "NEAR_EDGE" in kinds

    def test_il_threshold_respects_config(
        self,
        storage_with_wallet,
        make_event,
        make_position,
        current_prices_usdt_weth,
    ):
        # -9.09% IL (the case from TestPnL above)
        pos = self._setup_position_with_basis(
            storage_with_wallet,
            make_event,
            make_position,
            current_value=Decimal("3400"),
        )
        cb = compute_cost_basis(
            storage_with_wallet, TEST_WALLET, Chain.BSC, pos.protocol_id, pos.position_key
        )
        pnl = compute_pnl(pos, cb, current_prices_usdt_weth)

        # Threshold -10%: -9.09% does NOT trigger
        strict = list(
            evaluate_alerts(
                pos,
                pnl,
                AlertConfig(il_threshold_pct=Decimal("10")),
            )
        )
        assert not any(a.kind == "IL_THRESHOLD" for a in strict)

        # Threshold -5%: -9.09% DOES trigger
        loose = list(
            evaluate_alerts(
                pos,
                pnl,
                AlertConfig(il_threshold_pct=Decimal("5")),
            )
        )
        assert any(a.kind == "IL_THRESHOLD" for a in loose)

    def test_dedup_window(self, storage):
        import time

        now = int(time.time())
        a = Alert(
            position_uid="test:bsc:pancake_infinity:p1",
            kind="IL_THRESHOLD",
            severity=AlertSeverity.HIGH,
            message="test",
            triggered_at=now,
        )
        storage.write_alert(a)
        # Just-written alert is "recent"
        assert storage.recently_alerted(a.position_uid, a.kind, within_hours=24) is True
        # A different kind on same position is not deduped
        assert storage.recently_alerted(a.position_uid, "OUT_OF_RANGE", within_hours=24) is False


# ─────────────────────────────────────────────────────────────────────────
# Snapshot persistence + reporting queries
# ─────────────────────────────────────────────────────────────────────────


class TestSnapshots:
    def test_write_and_read_latest(
        self,
        storage_with_wallet,
        make_event,
        make_position,
        current_prices_usdt_weth,
    ):
        storage_with_wallet.upsert_events(
            [
                make_event(
                    tx_hash="0xd1",
                    usdt_amount=Decimal("1000"),
                    weth_amount=Decimal("0.5"),
                    weth_price=Decimal("3000"),
                ),
            ]
        )
        pos = make_position(current_value_usd=Decimal("3400"))
        cb = compute_cost_basis(
            storage_with_wallet, TEST_WALLET, Chain.BSC, pos.protocol_id, pos.position_key
        )
        pnl = compute_pnl(pos, cb, current_prices_usdt_weth)

        storage_with_wallet.write_snapshot(
            pos,
            cost_basis_usd=pnl.cost_basis_usd,
            hold_value_usd=pnl.hold_value_usd,
            il_usd=pnl.il_usd,
            pnl_usd=pnl.pnl_usd,
        )
        latest = storage_with_wallet.latest_snapshot(wallet=TEST_WALLET)
        assert len(latest) == 1
        row = latest[0]
        assert row["pair_label"] == "USDT/ETH 0.05%"
        assert row["current_value_usd"] == 3400.0

    def test_mtd_and_mom_queries_execute(
        self,
        storage_with_wallet,
        make_event,
        make_position,
        current_prices_usdt_weth,
    ):
        storage_with_wallet.upsert_events(
            [
                make_event(
                    tx_hash="0xd1",
                    usdt_amount=Decimal("1000"),
                    weth_amount=Decimal("0.5"),
                    weth_price=Decimal("3000"),
                ),
            ]
        )
        pos = make_position(current_value_usd=Decimal("3400"))
        cb = compute_cost_basis(
            storage_with_wallet, TEST_WALLET, Chain.BSC, pos.protocol_id, pos.position_key
        )
        pnl = compute_pnl(pos, cb, current_prices_usdt_weth)
        storage_with_wallet.write_snapshot(
            pos,
            cost_basis_usd=pnl.cost_basis_usd,
            hold_value_usd=pnl.hold_value_usd,
            il_usd=pnl.il_usd,
            pnl_usd=pnl.pnl_usd,
        )
        # Should execute cleanly even with one snapshot
        assert isinstance(
            storage_with_wallet.month_to_date_pnl(wallet=TEST_WALLET),
            list,
        )
        assert isinstance(
            storage_with_wallet.month_over_month(wallet=TEST_WALLET, months=6),
            list,
        )


# ─────────────────────────────────────────────────────────────────────────
# Alert timestamp fields — STALE_POSITION and NEGATIVE_CARRY
# ─────────────────────────────────────────────────────────────────────────


class TestAlertTimestampFields:
    """STALE_POSITION and NEGATIVE_CARRY depend on position.last_event_at /
    opened_at being populated. These were previously dead code paths because
    runner.snapshot_adapter() never set those fields."""

    def _make_pnl(self, il_usd: Decimal) -> PnLBreakdown:
        basis = Decimal("2500")
        hold = basis - il_usd  # ensure hold > 0
        return PnLBreakdown(
            cost_basis_usd=basis,
            current_value_usd=Decimal("3400"),
            unclaimed_usd=Decimal("45"),
            fees_collected_usd=Decimal("0"),
            hold_value_usd=hold,
            il_usd=il_usd,
            il_pct=il_usd / hold * 100 if hold else Decimal("0"),
            pnl_usd=Decimal("3400") + Decimal("45") - basis,
            pnl_pct=Decimal("0"),
        )

    def test_stale_position_fires_when_last_event_old(self, make_position):
        import time

        old_ts = int(time.time()) - 40 * 86400  # 40 days ago, past stale_days=30
        pos = make_position(in_range=False)
        pos.last_event_at = old_ts

        pnl = self._make_pnl(il_usd=Decimal("0"))
        kinds = {a.kind for a in evaluate_alerts(pos, pnl, AlertConfig())}
        assert "STALE_POSITION" in kinds

    def test_stale_position_does_not_fire_when_last_event_recent(self, make_position):
        import time

        recent_ts = int(time.time()) - 5 * 86400  # 5 days ago, within stale_days=30
        pos = make_position(in_range=False)
        pos.last_event_at = recent_ts

        pnl = self._make_pnl(il_usd=Decimal("0"))
        kinds = {a.kind for a in evaluate_alerts(pos, pnl, AlertConfig())}
        assert "STALE_POSITION" not in kinds

    def test_stale_position_does_not_fire_when_last_event_at_is_none(self, make_position):
        pos = make_position(in_range=False)
        pos.last_event_at = None

        pnl = self._make_pnl(il_usd=Decimal("0"))
        kinds = {a.kind for a in evaluate_alerts(pos, pnl, AlertConfig())}
        assert "STALE_POSITION" not in kinds

    def test_negative_carry_fires_when_il_exceeds_fees(self, make_position):
        import time

        # 5 days open; daily_il = 200/5 = $40. daily_fees ≈ $3.57 → triggers.
        pos = make_position()
        pos.opened_at = int(time.time()) - 5 * 86400

        pnl = self._make_pnl(il_usd=Decimal("-200"))
        kinds = {a.kind for a in evaluate_alerts(pos, pnl, AlertConfig())}
        assert "NEGATIVE_CARRY" in kinds

    def test_negative_carry_does_not_fire_when_opened_at_none(self, make_position):
        pos = make_position()
        pos.opened_at = None

        pnl = self._make_pnl(il_usd=Decimal("-200"))
        kinds = {a.kind for a in evaluate_alerts(pos, pnl, AlertConfig())}
        assert "NEGATIVE_CARRY" not in kinds


# ─────────────────────────────────────────────────────────────────────────
# Runner batch consumption
# ─────────────────────────────────────────────────────────────────────────


class _BatchStubAdapter(ProtocolAdapter):
    """Yields pre-baked event batches; records how many were requested."""

    def __init__(self, batches: list[list[Event]], positions: list | None = None):
        self._batches = batches
        self._positions = positions or []
        self.batches_requested = 0

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            protocol_id="pancake_infinity",  # matches make_event default
            display_name="Stub",
            chains=[Chain.BSC],
            protocol_kind=ProtocolKind.CL_AMM,
            supports_exact_fees=True,
            supports_historical=True,
        )

    def iter_event_batches(self, wallet, chain, since_ts=0, deadline=None):
        # Deliberately ignores `deadline` — exercises the runner's fallback.
        for batch in self._batches:
            self.batches_requested += 1
            yield batch

    def fetch_events(self, wallet, chain, since_ts=0):
        return [ev for b in self._batches for ev in b]

    def fetch_positions(self, wallet, chain):
        return self._positions


class _FetchOnlyStubAdapter(_BatchStubAdapter):
    """Implements only fetch_events — exercises the ABC's default generator."""

    def __init__(self, batches):
        super().__init__(batches)
        # Remove the override so the ProtocolAdapter default is used
        self.iter_event_batches = lambda wallet, chain, since_ts=0, deadline=None: (
            ProtocolAdapter.iter_event_batches(
                self, wallet, chain, since_ts=since_ts, deadline=deadline
            )
        )


class TestRunnerBatchSync:
    def test_sync_adapter_persists_each_batch_and_watermarks(self, storage, make_event):
        from defi_tracker.core.runner import TrackerRunner

        b1 = [make_event(ts=1_714_521_600, tx_hash="0x01"), make_event(ts=1_714_525_200, tx_hash="0x02")]
        b2 = [make_event(ts=1_714_608_000, tx_hash="0x03")]
        adapter = _BatchStubAdapter([b1, b2])
        runner = TrackerRunner(storage)

        inserted = runner.sync_adapter(adapter, TEST_WALLET, Chain.BSC)

        assert inserted == 3
        assert adapter.batches_requested == 2
        assert (
            storage.get_last_synced_ts(TEST_WALLET, Chain.BSC, "pancake_infinity")
            == 1_714_608_000
        )

    def test_sync_adapter_force_stops_deadline_ignoring_adapter(
        self, storage, make_event
    ):
        import time

        from defi_tracker.core.runner import _DEADLINE_GRACE, TrackerRunner

        b1 = [make_event(ts=1_714_521_600, tx_hash="0x01")]
        b2 = [make_event(ts=1_714_608_000, tx_hash="0x02")]
        adapter = _BatchStubAdapter([b1, b2])  # ignores deadline by design
        runner = TrackerRunner(storage)

        # Deadline + grace already exhausted → runner fallback stops after
        # the first batch is persisted
        inserted = runner.sync_adapter(
            adapter, TEST_WALLET, Chain.BSC,
            deadline=time.monotonic() - _DEADLINE_GRACE - 1,
        )

        assert inserted == 1
        assert adapter.batches_requested == 1
        # First batch's watermark is kept
        assert (
            storage.get_last_synced_ts(TEST_WALLET, Chain.BSC, "pancake_infinity")
            == 1_714_521_600
        )

    def test_sync_adapter_empty_batches_never_advance_watermark(self, storage, make_event):
        from defi_tracker.core.runner import TrackerRunner

        adapter = _BatchStubAdapter([[], []])
        runner = TrackerRunner(storage)

        inserted = runner.sync_adapter(adapter, TEST_WALLET, Chain.BSC)

        assert inserted == 0
        assert storage.get_last_synced_ts(TEST_WALLET, Chain.BSC, "pancake_infinity") == 0

    def test_default_iter_event_batches_wraps_fetch_events(self, storage, make_event):
        from defi_tracker.core.runner import TrackerRunner

        b1 = [make_event(ts=1_714_521_600, tx_hash="0x01")]
        adapter = _FetchOnlyStubAdapter([b1])
        runner = TrackerRunner(storage)

        inserted = runner.sync_adapter(adapter, TEST_WALLET, Chain.BSC)

        assert inserted == 1
        assert (
            storage.get_last_synced_ts(TEST_WALLET, Chain.BSC, "pancake_infinity")
            == 1_714_521_600
        )


# ─────────────────────────────────────────────────────────────────────────
# Honest unknowns: NULL analytics when a position has no event history
# ─────────────────────────────────────────────────────────────────────────


class TestHonestUnknowns:
    POSITION_KEY = "pool_X:tick_lo:tick_hi"  # matches make_event/make_position default

    def test_cost_basis_counts_events(self, storage, make_event):
        cb = compute_cost_basis(
            storage, TEST_WALLET, Chain.BSC, "pancake_infinity", self.POSITION_KEY
        )
        assert cb.event_count == 0
        assert cb.has_events is False

        storage.upsert_events([make_event()])
        cb = compute_cost_basis(
            storage, TEST_WALLET, Chain.BSC, "pancake_infinity", self.POSITION_KEY
        )
        assert cb.event_count == 1
        assert cb.has_events is True

    def test_snapshot_writes_null_analytics_when_no_events(self, storage, make_position):
        from defi_tracker.core.runner import TrackerRunner

        adapter = _BatchStubAdapter([], positions=[make_position()])
        runner = TrackerRunner(storage)

        runner.snapshot_adapter(adapter, TEST_WALLET, Chain.BSC)

        rows = storage.latest_snapshot()
        assert len(rows) == 1
        row = rows[0]
        assert row["cost_basis_usd"] is None
        assert row["hold_value_usd"] is None
        assert row["il_usd"] is None
        assert row["pnl_usd"] is None
        # Value itself is still real and reported
        assert row["current_value_usd"] == 3400.0

    def test_snapshot_computes_analytics_when_events_exist(
        self, storage, make_event, make_position
    ):
        from defi_tracker.core.runner import TrackerRunner

        storage.upsert_events([make_event()])  # $2500 deposit
        adapter = _BatchStubAdapter([], positions=[make_position()])
        runner = TrackerRunner(storage)

        runner.snapshot_adapter(adapter, TEST_WALLET, Chain.BSC)

        row = storage.latest_snapshot()[0]
        assert row["cost_basis_usd"] == 2500.0
        assert row["pnl_usd"] is not None

    def test_evaluate_alerts_with_none_pnl_yields_range_alerts_only(self, make_position):
        pos = make_position(in_range=False, tick_current=250000)
        kinds = {a.kind for a in evaluate_alerts(pos, None, AlertConfig())}
        assert "OUT_OF_RANGE" in kinds
        assert "IL_THRESHOLD" not in kinds
        assert "NEGATIVE_CARRY" not in kinds


# ─────────────────────────────────────────────────────────────────────────
# Closed-position tombstones + schema v2 migration
# ─────────────────────────────────────────────────────────────────────────


class _RaisingPositionsAdapter(_BatchStubAdapter):
    def fetch_positions(self, wallet, chain):
        raise RuntimeError("subgraph returned no data")


class TestTombstones:
    def test_schema_migrates_v1_to_v2(self, storage):
        from defi_tracker.core.storage import SCHEMA_VERSION

        # Rewind to a v1-shaped DB: drop the v2 column and stored version
        with storage.connect() as conn:
            conn.execute("ALTER TABLE snapshots DROP COLUMN is_closed")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', '1')"
            )

        storage.init_schema()  # applies the migration

        with storage.connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
        assert "is_closed" in cols
        assert storage.schema_version() == SCHEMA_VERSION
        storage.init_schema()  # idempotent second run

    def test_tombstone_written_when_position_disappears(self, storage, make_position):
        from defi_tracker.core.runner import TrackerRunner

        storage.write_snapshot(position=make_position())
        assert len(storage.latest_snapshot()) == 1

        # Successful pass with the position gone → tombstoned
        adapter = _BatchStubAdapter([], positions=[])
        TrackerRunner(storage).snapshot_adapter(adapter, TEST_WALLET, Chain.BSC)

        assert storage.latest_snapshot() == []
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT is_closed, current_value_usd FROM snapshots ORDER BY snapshot_ts DESC"
            ).fetchone()
        assert row["is_closed"] == 1
        assert row["current_value_usd"] == 0

    def test_still_open_position_not_tombstoned(self, storage, make_position):
        from defi_tracker.core.runner import TrackerRunner

        pos = make_position()
        storage.write_snapshot(position=pos)

        adapter = _BatchStubAdapter([], positions=[make_position()])
        TrackerRunner(storage).snapshot_adapter(adapter, TEST_WALLET, Chain.BSC)

        rows = storage.latest_snapshot()
        assert len(rows) == 1
        assert rows[0]["is_closed"] == 0

    def test_no_tombstone_when_fetch_positions_raises(self, storage, make_position):
        import pytest

        from defi_tracker.core.runner import TrackerRunner

        storage.write_snapshot(position=make_position())

        adapter = _RaisingPositionsAdapter([])
        with pytest.raises(RuntimeError):
            TrackerRunner(storage).snapshot_adapter(adapter, TEST_WALLET, Chain.BSC)

        # Outage must not close anything
        rows = storage.latest_snapshot()
        assert len(rows) == 1
        assert rows[0]["is_closed"] == 0

    def test_sync_failure_does_not_skip_snapshot(self, storage, make_position):
        """An event-sync outage must not freeze marks: the snapshot still runs
        (the pancake adapter falls back to RPC there) and the sync error is
        surfaced in the run summary."""
        from defi_tracker.core.runner import TrackerRunner

        class _RaisingSyncAdapter(_BatchStubAdapter):
            def iter_event_batches(self, wallet, chain, since_ts=0, deadline=None):
                raise RuntimeError("subgraph returned no data")
                yield  # pragma: no cover — makes this a generator

        storage.add_wallet(TEST_WALLET)
        adapter = _RaisingSyncAdapter([], positions=[make_position()])
        new_ev, positions, alerts, missing, sync_error = TrackerRunner(
            storage
        )._sync_and_snapshot(adapter, TEST_WALLET, Chain.BSC)

        assert new_ev == 0
        assert len(positions) == 1
        assert sync_error is not None and "no data" in sync_error
        # The snapshot was persisted despite the sync failure
        assert len(storage.latest_snapshot()) == 1

    def test_reopened_position_replaces_same_day_tombstone(self, storage, make_position):
        from defi_tracker.core.runner import TrackerRunner

        storage.write_snapshot(position=make_position())
        runner = TrackerRunner(storage)

        # Morning: closed
        runner.snapshot_adapter(_BatchStubAdapter([], positions=[]), TEST_WALLET, Chain.BSC)
        assert storage.latest_snapshot() == []

        # Evening: reopened (same position_key, same day) — live row wins
        runner.snapshot_adapter(
            _BatchStubAdapter([], positions=[make_position()]), TEST_WALLET, Chain.BSC
        )
        rows = storage.latest_snapshot()
        assert len(rows) == 1
        assert rows[0]["is_closed"] == 0


# ─────────────────────────────────────────────────────────────────────────
# Transition-based alerts
# ─────────────────────────────────────────────────────────────────────────


class TestTransitionAlerts:
    @staticmethod
    def _pnl(il_pct=Decimal("0"), il_usd=Decimal("0")) -> PnLBreakdown:
        return PnLBreakdown(
            cost_basis_usd=Decimal("1000"),
            current_value_usd=Decimal("1000"),
            unclaimed_usd=Decimal("0"),
            fees_collected_usd=Decimal("0"),
            hold_value_usd=Decimal("1000"),
            il_usd=il_usd,
            il_pct=il_pct,
            pnl_usd=Decimal("0"),
            pnl_pct=Decimal("0"),
        )

    def _get(self, alerts, kind):
        matches = [a for a in alerts if a.kind == kind]
        assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
        return matches[0]

    def test_out_of_range_transition_vs_steady_state(self, make_position):
        from defi_tracker.alerts import PrevPositionState

        pos = make_position(in_range=False, tick_current=250000)

        was_in = PrevPositionState(in_range=True, near_edge=False, il_pct=None)
        alerts = list(evaluate_alerts(pos, self._pnl(), AlertConfig(), prev=was_in))
        assert self._get(alerts, "OUT_OF_RANGE").context["transition"] is True

        was_out = PrevPositionState(in_range=False, near_edge=False, il_pct=None)
        alerts = list(evaluate_alerts(pos, self._pnl(), AlertConfig(), prev=was_out))
        assert self._get(alerts, "OUT_OF_RANGE").context["transition"] is False

    def test_out_of_range_first_observation_is_transition(self, make_position):
        pos = make_position(in_range=False, tick_current=250000)
        alerts = list(evaluate_alerts(pos, self._pnl(), AlertConfig(), prev=None))
        assert self._get(alerts, "OUT_OF_RANGE").context["transition"] is True

    def test_near_edge_transition_vs_steady_state(self, make_position):
        from defi_tracker.alerts import PrevPositionState

        # tick 200500 in [200000, 210000] → 5% from lower bound → near edge
        pos = make_position(in_range=True, tick_current=200500)

        was_centered = PrevPositionState(in_range=True, near_edge=False, il_pct=None)
        alerts = list(evaluate_alerts(pos, self._pnl(), AlertConfig(), prev=was_centered))
        assert self._get(alerts, "NEAR_EDGE").context["transition"] is True

        was_near = PrevPositionState(in_range=True, near_edge=True, il_pct=None)
        alerts = list(evaluate_alerts(pos, self._pnl(), AlertConfig(), prev=was_near))
        assert self._get(alerts, "NEAR_EDGE").context["transition"] is False

    def test_il_threshold_transition_on_crossing(self, make_position):
        from defi_tracker.alerts import PrevPositionState

        pos = make_position()
        pnl = self._pnl(il_pct=Decimal("-12"), il_usd=Decimal("-120"))

        was_fine = PrevPositionState(in_range=True, near_edge=False, il_pct=Decimal("-8"))
        alerts = list(evaluate_alerts(pos, pnl, AlertConfig(), prev=was_fine))
        assert self._get(alerts, "IL_THRESHOLD").context["transition"] is True

        was_bad = PrevPositionState(in_range=True, near_edge=False, il_pct=Decimal("-11"))
        alerts = list(evaluate_alerts(pos, pnl, AlertConfig(), prev=was_bad))
        assert self._get(alerts, "IL_THRESHOLD").context["transition"] is False

    def test_runner_dedup_uses_reminder_window_for_steady_state(
        self, storage, make_position
    ):
        import time as _time

        from defi_tracker.core.runner import TrackerRunner

        # Yesterday's snapshot: already out of range → today is steady-state
        pos = make_position(in_range=False, tick_current=250000)
        from datetime import UTC, datetime, timedelta

        storage.write_snapshot(
            position=pos,
            snapshot_date=(datetime.now(UTC) - timedelta(days=1)).date(),
        )

        # An OUT_OF_RANGE alert 3 days ago — inside the 7-day reminder window
        storage.write_alert(
            Alert(
                position_uid=pos.uid,
                kind="OUT_OF_RANGE",
                severity=AlertSeverity.HIGH,
                message="prior",
                triggered_at=int(_time.time()) - 3 * 86400,
            )
        )

        adapter = _BatchStubAdapter([], positions=[make_position(in_range=False, tick_current=250000)])
        # Exercise the reminder-window dedup on OUT_OF_RANGE specifically.
        runner = TrackerRunner(storage, AlertConfig(push_kinds=frozenset({"OUT_OF_RANGE"})))
        _, fresh, _ = runner.snapshot_adapter(adapter, TEST_WALLET, Chain.BSC)

        assert "OUT_OF_RANGE" not in {a.kind for a in fresh}  # reminder suppressed

    def test_runner_reminds_after_reminder_window(self, storage, make_position):
        import time as _time
        from datetime import UTC, datetime, timedelta

        from defi_tracker.core.runner import TrackerRunner

        pos = make_position(in_range=False, tick_current=250000)
        storage.write_snapshot(
            position=pos,
            snapshot_date=(datetime.now(UTC) - timedelta(days=1)).date(),
        )
        storage.write_alert(
            Alert(
                position_uid=pos.uid,
                kind="OUT_OF_RANGE",
                severity=AlertSeverity.HIGH,
                message="prior",
                triggered_at=int(_time.time()) - 8 * 86400,  # outside 7-day window
            )
        )

        adapter = _BatchStubAdapter([], positions=[make_position(in_range=False, tick_current=250000)])
        runner = TrackerRunner(storage, AlertConfig(push_kinds=frozenset({"OUT_OF_RANGE"})))
        _, fresh, _ = runner.snapshot_adapter(adapter, TEST_WALLET, Chain.BSC)

        assert "OUT_OF_RANGE" in {a.kind for a in fresh}  # weekly reminder fires

    def test_runner_transition_alert_fires_when_state_flips(self, storage, make_position):
        from datetime import UTC, datetime, timedelta

        from defi_tracker.core.runner import TrackerRunner

        # Yesterday in range → today out of range: transition, alerts immediately
        storage.write_snapshot(
            position=make_position(in_range=True),
            snapshot_date=(datetime.now(UTC) - timedelta(days=1)).date(),
        )
        adapter = _BatchStubAdapter([], positions=[make_position(in_range=False, tick_current=250000)])
        runner = TrackerRunner(storage, AlertConfig(push_kinds=frozenset({"OUT_OF_RANGE"})))
        _, fresh, _ = runner.snapshot_adapter(adapter, TEST_WALLET, Chain.BSC)

        assert "OUT_OF_RANGE" in {a.kind for a in fresh}


# ─────────────────────────────────────────────────────────────────────────
# Fee trend + PnL decomposition + risk summary
# ─────────────────────────────────────────────────────────────────────────


class TestPortfolioRollups:
    def _collect_row(
        self, month_ts, usd, symbols=("WPEAQ", "USDT"), chain="peaq", meta=None
    ):
        import json as _json

        return {
            "month": month_ts,
            "wallet": TEST_WALLET,
            "chain": chain,
            "protocol_id": "machinex",
            "position_key": "pool:1:2:3",
            "amounts_json": _json.dumps([{"symbol": s, "amount": "1"} for s in symbols]),
            "usd_at_ts": usd,
            "meta_json": _json.dumps(meta) if meta is not None else None,
        }

    def test_attribution_month_rolls_start_of_month_to_prior(self):
        from datetime import UTC, datetime

        from defi_tracker.analytics import attribution_month

        def ts(y, mo, d):
            return int(datetime(y, mo, d, 12, 0, tzinfo=UTC).timestamp())

        # Start-of-month sweep → prior month (incl. year boundary).
        assert attribution_month(ts(2026, 7, 1)) == "2026-06"
        assert attribution_month(ts(2026, 5, 7)) == "2026-04"
        assert attribution_month(ts(2026, 1, 5)) == "2025-12"
        # Mid/late-month collects stay in their own calendar month.
        assert attribution_month(ts(2026, 6, 8)) == "2026-06"
        assert attribution_month(ts(2025, 10, 17)) == "2025-10"

    def test_attribute_fee_months_preserves_total_and_claim_month(self):
        from defi_tracker.analytics import attribute_fee_months, monthly_fee_matrix

        def _c(y, mo, d, usd):
            from datetime import UTC, datetime

            return {
                "month": f"{y:04d}-{mo:02d}",
                "ts": int(datetime(y, mo, d, 12, tzinfo=UTC).timestamp()),
                "wallet": TEST_WALLET, "chain": "peaq", "protocol_id": "machinex",
                "position_key": "p:1:2:3", "amounts_json": "[]", "usd_at_ts": usd,
            }

        raw = [_c(2026, 7, 1, 10_000.0), _c(2026, 6, 15, 500.0)]
        attributed = attribute_fee_months(raw)
        # July-1 sweep → June accrual, tagged with its real claim month.
        july_sweep = next(r for r in attributed if r["usd_at_ts"] == 10_000.0)
        assert july_sweep["month"] == "2026-06"
        assert july_sweep["claim_month"] == "2026-07"
        # Mid-month June collect stays in June.
        midmonth = next(r for r in attributed if r["usd_at_ts"] == 500.0)
        assert midmonth["month"] == "2026-06" and midmonth["claim_month"] == "2026-06"
        # Original rows untouched; total conserved (both land in June).
        assert raw[0]["month"] == "2026-07"
        _, _, cells = monthly_fee_matrix(attributed)
        assert sum(cells.values()) == Decimal("10500")
        assert cells[("2026-06", "peaq")] == Decimal("10500")

    def test_fee_netting_split_separates_withdrawal_fees(self):
        from defi_tracker.analytics import fee_netting_split, is_withdrawal_fee

        rows = [
            self._collect_row("2026-05", 100.0),  # deliberate sweep
            self._collect_row("2026-05", 40.0, meta={"netted_against_decrease": True}),
            self._collect_row("2026-06", 25.0, meta={"liquidity_delta": None}),  # sweep
        ]
        assert is_withdrawal_fee(rows[1]) is True
        assert is_withdrawal_fee(rows[0]) is False
        assert is_withdrawal_fee(rows[2]) is False

        split = fee_netting_split(rows)
        assert split["months"] == ["2026-05", "2026-06"]
        assert split["withdrawal_by_month"] == {"2026-05": Decimal("40")}
        assert split["sweep_by_month"] == {"2026-05": Decimal("100"), "2026-06": Decimal("25")}
        assert split["withdrawal_total"] == Decimal("40")
        assert split["sweep_total"] == Decimal("125")
        # Sweeps + withdrawal must reconcile to the full harvest total.
        total = sum(Decimal(str(r["usd_at_ts"])) for r in rows)
        assert split["sweep_total"] + split["withdrawal_total"] == total

    def test_rebalance_signals_tiers_and_gates(self):
        from defi_tracker.analytics import RebalanceConfig, rebalance_signals

        def snap(pk, value, lo, hi, cur, in_range):
            return {
                "wallet": TEST_WALLET, "position_key": pk, "pair_label": pk,
                "chain": "peaq", "protocol_id": "machinex",
                "current_value_usd": value, "tick_lower": lo, "tick_upper": hi,
                "tick_current": cur, "in_range": in_range,
            }

        # Range width 1000 ticks. depth = beyond ÷ width.
        snaps = [
            snap("deep", 8000, 0, 1000, 1600, 0),      # depth 0.60 → now
            snap("moderate", 7000, 0, 1000, 1250, 0),  # depth 0.25 → soon
            snap("shallow", 6000, 0, 1000, 1100, 0),   # depth 0.10 → watch
            snap("small", 1000, 0, 1000, 2000, 0),     # deep but $ below floor
            snap("inrange", 50000, 0, 1000, 500, 1),   # in range → not flagged
            snap("earning", 9000, 0, 1000, 1600, 0),   # deep but still earning
        ]
        yields = {
            (TEST_WALLET, "inrange"): {"apy_pct": 20.0},
            (TEST_WALLET, "earning"): {"apy_pct": 40.0},
        }  # deep/moderate/shallow absent → yield None → dead
        sigs = rebalance_signals(
            snaps, yields, {}, RebalanceConfig(idle_min_usd=Decimal("6500"))
        )
        by = {s["position_key"]: s for s in sigs}
        assert by["deep"]["tier"] == "now"
        assert by["moderate"]["tier"] == "soon"
        assert "shallow" not in by      # below the 6500 floor set here
        assert "small" not in by        # below floor
        assert "inrange" not in by      # in range
        assert by["earning"]["tier"] == "watch"  # deep but yield ≥ 5% → not dead
        assert sigs[0]["tier"] == "now"          # sorted now→soon→watch
        # forgone = idle × value-weighted in-range yield (20%)
        assert abs(by["deep"]["forgone_usd"] - 8000 * 0.20) < 1

    def test_rebalance_queue_lifecycle(self, storage):
        import time as _time

        sig = [{"uid": "w:peaq:machinex:P", "pair": "X/Y", "tier": "now",
                "idle_usd": 6000, "depth": 0.8, "days_out": None, "forgone_usd": 100,
                "current_half_pct": 50, "suggest": {"half_pct": 120}}]

        # First sight → one open item.
        storage.reconcile_rebalance_queue(sig)
        q = storage.list_rebalance_queue()
        assert len(q) == 1 and q[0]["status"] == "open"
        item_id = q[0]["id"]

        # Snooze → suppressed from alert, still tracked.
        storage.snooze_rebalance_item(item_id, int(_time.time()) + 86400)
        assert storage.is_rebalance_snoozed("w:peaq:machinex:P") is True
        assert "w:peaq:machinex:P" in storage.snoozed_rebalance_uids()
        storage.reconcile_rebalance_queue(sig)  # still flagged → stays snoozed
        assert storage.list_rebalance_queue()[0]["status"] == "snoozed"

        # Mark done → resolved, and the grace window prevents instant reopen
        # even though the (stale) signal still flags it.
        assert storage.resolve_rebalance_item(item_id) is True
        storage.reconcile_rebalance_queue(sig)
        assert storage.list_rebalance_queue() == []          # nothing live
        assert len(storage.list_rebalance_queue(include_resolved=True)) == 1

        # No longer flagged → auto-resolve is a no-op (already resolved).
        storage.reconcile_rebalance_queue([])
        assert storage.list_rebalance_queue() == []

    def test_rebalance_queue_auto_resolves_when_unflagged(self, storage):
        sig = [{"uid": "w:peaq:machinex:Q", "pair": "A/B", "tier": "soon",
                "idle_usd": 7000, "depth": 0.3, "days_out": 5, "forgone_usd": 50,
                "current_half_pct": 40, "suggest": None}]
        storage.reconcile_rebalance_queue(sig)
        assert len(storage.list_rebalance_queue()) == 1
        # Position rebalanced → no longer flagged → drops off automatically.
        storage.reconcile_rebalance_queue([])
        assert storage.list_rebalance_queue() == []
        hist = storage.list_rebalance_queue(include_resolved=True)
        assert hist[0]["status"] == "resolved" and hist[0]["resolved_reason"] == "auto"

    def test_pool_tick_volatility_and_suggest_range(self):
        import math

        from defi_tracker.analytics import pool_tick_volatility, suggest_range

        # Daily tick series for one pool: deltas [+100,-100,+100,-100], mean 0.
        hist = [
            {"snapshot_date": f"2026-07-0{d}", "chain": "peaq", "pair_label": "A/B",
             "tick_current": t, "is_closed": 0}
            for d, t in [(1, 0), (2, 100), (3, 0), (4, 100), (5, 0)]
        ]
        vol = pool_tick_volatility(hist, min_points=3)
        assert abs(vol[("peaq", "A/B")] - 100.0) < 1e-9  # pstdev of ±100 = 100
        # Too few daily returns → pool omitted (can't size honestly).
        assert pool_tick_volatility(hist[:2], min_points=3) == {}

        sug = suggest_range(5000, vol[("peaq", "A/B")], z=2.5, horizon_days=30)
        half = round(2.5 * 100 * math.sqrt(30))
        assert sug["half_ticks"] == half
        assert sug["lower"] == 5000 - half and sug["upper"] == 5000 + half
        assert sug["half_pct"] > 0
        assert suggest_range(5000, None) is None  # no vol → no suggestion

    def test_rebalance_signals_attaches_suggested_range(self):
        from defi_tracker.analytics import RebalanceConfig, rebalance_signals

        snaps = [{
            "wallet": TEST_WALLET, "position_key": "p", "pair_label": "A/B",
            "chain": "peaq", "protocol_id": "machinex", "current_value_usd": 9000,
            "tick_lower": 0, "tick_upper": 1000, "tick_current": 1600, "in_range": 0,
        }]
        sigs = rebalance_signals(
            snaps, {}, {}, RebalanceConfig(), tick_vol={("peaq", "A/B"): 300.0}
        )
        assert len(sigs) == 1
        sug = sigs[0]["suggest"]
        assert sug is not None and sug["half_pct"] > 0
        assert sigs[0]["current_half_pct"] > 0  # (1000-0)/2 ticks in %

    def test_monthly_fee_matrix(self):
        from defi_tracker.analytics import monthly_fee_matrix

        rows = [
            self._collect_row("2026-05", 100.0),
            self._collect_row("2026-05", 50.0, chain="bsc"),
            self._collect_row("2026-06", 25.0),
        ]
        months, chains, cells = monthly_fee_matrix(rows)
        assert months == ["2026-05", "2026-06"]
        assert chains == ["bsc", "peaq"]
        assert cells[("2026-05", "peaq")] == Decimal("100")
        assert cells[("2026-05", "bsc")] == Decimal("50")
        assert ("2026-06", "bsc") not in cells

    def test_fees_by_pair_labels_and_recent_window(self):
        from defi_tracker.analytics import fees_by_pair

        rows = [
            self._collect_row("2026-01", 500.0),
            self._collect_row("2026-06", 100.0),
            self._collect_row("2026-06", 10.0, symbols=("ACU", "WPEAQ")),
        ]
        result = fees_by_pair(rows, recent_months=1)
        top = result[0]
        assert top["pair"] == "WPEAQ/USDT"
        assert top["lifetime"] == Decimal("600")
        assert top["recent"] == Decimal("100")  # only 2026-06 in window
        assert result[1]["pair"] == "ACU/WPEAQ"

    def test_pnl_decomposition_sums_to_net(self):
        from defi_tracker.analytics import pnl_decomposition

        rows = [
            {
                # cost 1000, hold 1100, value 1050, unclaimed 10, fees 40
                # pnl = 1050 + 10 + 40 − 1000 = 100
                "cost_basis_usd": 1000.0,
                "hold_value_usd": 1100.0,
                "current_value_usd": 1050.0,
                "unclaimed_usd": 10.0,
                "il_usd": -50.0,
                "pnl_usd": 100.0,
            },
            {  # NULL analytics — excluded
                "cost_basis_usd": None,
                "hold_value_usd": None,
                "current_value_usd": 500.0,
                "unclaimed_usd": 0.0,
                "il_usd": None,
                "pnl_usd": None,
            },
        ]
        d = pnl_decomposition(rows)
        assert d["market_move"] == Decimal("100")  # 1100 − 1000
        assert d["il"] == Decimal("-50")
        assert d["fees_lifetime"] == Decimal("40")
        assert d["unclaimed"] == Decimal("10")
        assert d["net"] == Decimal("100")
        assert d["market_move"] + d["il"] + d["fees_lifetime"] + d["unclaimed"] == d["net"]
        assert d["excluded"] == 1

    def test_risk_summary(self):
        from defi_tracker.analytics import risk_summary

        rows = [
            {"pair_label": "A/B", "current_value_usd": 700.0, "in_range": 1},
            {"pair_label": "A/B", "current_value_usd": 100.0, "in_range": 0},
            {"pair_label": "C/D", "current_value_usd": 200.0, "in_range": 0},
        ]
        r = risk_summary(rows)
        assert r["out_of_range_value"] == Decimal("300")
        assert r["out_of_range_count"] == 2
        assert r["out_of_range_pct"] == Decimal("30")
        assert r["top_pair"] == "A/B"
        assert r["top_pair_value"] == Decimal("800")
        assert r["largest_position_label"] == "A/B"
        assert r["largest_position_value"] == Decimal("700")


class TestFeesEarnedMtd:
    def test_earned_is_collected_plus_unclaimed_delta(self):
        from defi_tracker.analytics import fees_earned_mtd

        rows = [
            # Harvest-sweep case: $100 collected but $90 of it was last
            # month's accrual (unclaimed fell 90); earned = 100 − 90 + 0
            {"fees_collected_mtd": 100.0, "unclaimed_now": 5.0, "unclaimed_month_start": 95.0},
            # Pure accrual case: nothing collected, unclaimed grew 20
            {"fees_collected_mtd": 0.0, "unclaimed_now": 30.0, "unclaimed_month_start": 10.0},
        ]
        assert fees_earned_mtd(rows) == Decimal("30")  # 10 + 20

    def test_handles_none_fields(self):
        from defi_tracker.analytics import fees_earned_mtd

        rows = [{"fees_collected_mtd": None, "unclaimed_now": None, "unclaimed_month_start": None}]
        assert fees_earned_mtd(rows) == Decimal("0")


class TestFeeValuation:
    def _row(self, month, symbol, tk, amt, claim_px, usd):
        import json as _json
        return {
            "month": month, "wallet": TEST_WALLET, "chain": "peaq",
            "protocol_id": "machinex", "position_key": "p:1",
            "amounts_json": _json.dumps([{"token_key": tk, "symbol": symbol,
                                          "amount": str(amt), "price_at_ts": str(claim_px)}]),
            "usd_at_ts": usd,
        }

    def test_harvest_vs_today_split(self):
        from defi_tracker.analytics import fee_valuation
        # 100 WPEAQ harvested at $0.13 = $13; today WPEAQ $0.02 → $2
        rows = [self._row("2025-10", "WPEAQ", "peaq:0xw", 100, "0.13", 13.0)]
        v = fee_valuation(rows, {"peaq:0xw": Decimal("0.02")})
        assert v["claim_total"] == Decimal("13.0")   # matches stored usd_at_ts
        assert v["today_by_month"]["2025-10"] == Decimal("2.00")
        sym, claim, today = v["by_token_drop"][0]
        assert sym == "WPEAQ" and claim == Decimal("13") and today == Decimal("2")
        assert v["priced"] is True

    def test_missing_price_falls_back_to_claim(self):
        from defi_tracker.analytics import fee_valuation
        rows = [self._row("2026-01", "OBSCURE", "peaq:0xz", 50, "0.4", 20.0)]
        v = fee_valuation(rows, {})  # no price → assume flat, not zero
        assert v["today_by_month"]["2026-01"] == Decimal("20.0")
        assert v["priced"] is False

    def test_stablecoin_fees_hold_value(self):
        from defi_tracker.analytics import fee_valuation
        rows = [self._row("2026-06", "USDT", "peaq:0xusdt", 500, "1.0", 500.0)]
        v = fee_valuation(rows, {"peaq:0xusdt": Decimal("1.0")})
        assert v["claim_total"] == v["today_total"] == Decimal("500")


class TestRealizedAndWindowYield:
    def test_realized_closed_pnl_only_counts_closed(self):
        from defi_tracker.analytics import realized_closed_pnl

        flows = [
            # closed: deposited 100, withdrew 130 (net −30), fees 20 → +50
            {"is_open": 0, "net_invested_usd": -30.0, "fees_usd": 20.0},
            # open: must be excluded
            {"is_open": 1, "net_invested_usd": 500.0, "fees_usd": 40.0},
            # closed at a loss: net invested 80 still in when closed... value 0 → −80, fees 10 → −70
            {"is_open": 0, "net_invested_usd": 80.0, "fees_usd": 10.0},
        ]
        r = realized_closed_pnl(flows)
        assert r["realized_usd"] == Decimal("-20")  # +50 − 70
        assert r["fees_usd"] == Decimal("30")
        assert r["positions"] == 2

    def _wstat(self, pkey, avg, unc_start, unc_end, span):
        return {
            "wallet": TEST_WALLET, "chain": "peaq", "protocol_id": "machinex",
            "position_key": pkey, "pair_label": "A/B",
            "avg_value_usd": avg, "snapshot_days": span + 1, "span_days": span,
            "unclaimed_start": unc_start, "unclaimed_end": unc_end,
            "window_start": "2026-06-09",
        }

    def test_window_yield_accrual_and_harvest_lump_proof(self):
        from defi_tracker.analytics import window_yields

        # Position earned 30 over 30 days on avg value 3650:
        # 25 collected mid-window + unclaimed went 10 → 15 (Δ +5)
        stats = [self._wstat("p1", 3650.0, 10.0, 15.0, 30)]
        collects = [{
            "wallet": TEST_WALLET, "chain": "peaq", "protocol_id": "machinex",
            "position_key": "p1", "ts": 0, "usd_at_ts": 25.0,
        }]
        y = window_yields(stats, collects, window_days=30)
        info = y[(TEST_WALLET, "p1")]
        assert info["earned"] == Decimal("30")
        # 30 / 3650 * 365/30 * 100 = 10%
        assert abs(info["apy_pct"] - 10.0) < 0.01

    def test_window_yield_insufficient_coverage_gives_none(self):
        from defi_tracker.analytics import window_yields

        stats = [self._wstat("p2", 1000.0, 0.0, 5.0, 3)]  # only 3 days of span
        y = window_yields(stats, [], window_days=30)
        assert y[(TEST_WALLET, "p2")]["apy_pct"] is None
        assert y[(TEST_WALLET, "p2")]["earned"] == Decimal("5")
