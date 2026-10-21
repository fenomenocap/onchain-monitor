"""
"Sync ok" must mean *caught up*, not *didn't raise*.

The health table exists because BSC event ingestion died for 39 days behind a
working RPC fallback. A truncated scan is the same class of lie in a different
disguise: machinex caps every run at MAX_BLOCKS_PER_RUN blocks and also stops
on its cooperative deadline, returning cleanly both times while events inside
the un-scanned range are still missing from cost basis, fees and yields.

These tests pin the contract end to end:
  * adapters report completion via ProtocolAdapter.last_scan_reached_head()
  * the runner refuses to advance last_sync_ok_at on a truncated pass
  * a quiet wallet (scan reached head, zero events) still counts as complete
  * Position.meta['source'] is produced by real adapter code, not test setup
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from defi_tracker.adapters.machinex import (
    DEPLOY_BLOCK,
    MAX_BLOCKS_PER_RUN,
    WINDOW_BLOCKS,
    MachineXAdapter,
)
from defi_tracker.adapters.uniswap_v3 import UniswapV3Adapter
from defi_tracker.core.adapter import ProtocolAdapter
from defi_tracker.core.runner import TrackerRunner
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import AdapterInfo, Chain, ProtocolKind
from tests.conftest import TEST_WALLET

PID = "pancake_infinity"  # matches the make_event / make_position fixtures

# Far enough past the cursor that one run's 150k-block cap cannot reach it.
FAR_HEAD = DEPLOY_BLOCK + MAX_BLOCKS_PER_RUN * 4


# ─────────────────────────────────────────────────────────────────────────
# machinex: does the scan know whether it reached the chain head?
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def peaq_storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "peaq.db")
    s.init_schema()
    return s


@pytest.fixture
def machinex(peaq_storage: Storage, tmp_path: Path) -> MachineXAdapter:
    a = MachineXAdapter(
        graph_api_key="test-api-key",
        rpc_url="http://fake-rpc",
        db_path=str(tmp_path / "peaq.db"),
    )
    a._storage = peaq_storage
    return a


def _scan(adapter: MachineXAdapter, head: int, *, deadline: float | None = None) -> None:
    """Drain iter_event_batches against a chain whose head is `head`."""
    with (
        patch("time.sleep"),
        patch("defi_tracker.adapters.machinex.rpc_enumerate_positions_v3", return_value=[1]),
        patch(
            "defi_tracker.adapters.machinex.rpc_get_position_v3",
            return_value=("0x" + "aa" * 20, "0x" + "bb" * 20, 100, -10, 10, 5, 0, 0),
        ),
        patch(
            "defi_tracker.adapters.machinex.rpc_get_pool_address_v3",
            return_value="0x" + "cc" * 20,
        ),
        patch("defi_tracker.adapters._rpc.RpcClient.eth_call", return_value=None),
        patch.object(adapter._rpc, "get_block_number", return_value=head),
        patch.object(adapter._rpc, "get_logs_chunked", return_value=[]),
    ):
        for _ in adapter.iter_event_batches(TEST_WALLET, Chain.PEAQ, deadline=deadline):
            pass


def test_scan_truncated_by_block_cap_is_not_complete(machinex: MachineXAdapter):
    """The 150k cap: a wallet a week behind returns cleanly, still behind."""
    _scan(machinex, FAR_HEAD)
    assert machinex.last_scan_reached_head(TEST_WALLET, Chain.PEAQ) is False


def test_scan_reaching_head_is_complete(machinex: MachineXAdapter):
    _scan(machinex, DEPLOY_BLOCK + WINDOW_BLOCKS)
    assert machinex.last_scan_reached_head(TEST_WALLET, Chain.PEAQ) is True


def test_scan_stopped_on_deadline_is_not_complete(machinex: MachineXAdapter):
    """Even when the head is one window away, an expired deadline stops the
    scan before it gets there."""
    _scan(machinex, DEPLOY_BLOCK + WINDOW_BLOCKS * 3, deadline=0.0)
    assert machinex.last_scan_reached_head(TEST_WALLET, Chain.PEAQ) is False


def test_catching_up_over_several_runs_flips_to_complete(machinex: MachineXAdapter):
    """Run after run the cursor walks forward; only the run that lands on the
    head reports a healthy sync."""
    head = DEPLOY_BLOCK + MAX_BLOCKS_PER_RUN + WINDOW_BLOCKS
    _scan(machinex, head)
    assert machinex.last_scan_reached_head(TEST_WALLET, Chain.PEAQ) is False
    _scan(machinex, head)
    assert machinex.last_scan_reached_head(TEST_WALLET, Chain.PEAQ) is True


def test_wallet_with_no_positions_reports_cursor_progress_honestly(machinex: MachineXAdapter):
    """No positions → the cursor is skipped forward without scanning; that is
    complete only once the skip lands on the head."""
    with (
        patch("defi_tracker.adapters.machinex.rpc_enumerate_positions_v3", return_value=[]),
        patch.object(machinex._rpc, "get_block_number", return_value=FAR_HEAD),
    ):
        machinex.fetch_events(TEST_WALLET, Chain.PEAQ)
    assert machinex.last_scan_reached_head(TEST_WALLET, Chain.PEAQ) is False

    with (
        patch("defi_tracker.adapters.machinex.rpc_enumerate_positions_v3", return_value=[]),
        patch.object(machinex._rpc, "get_block_number", return_value=DEPLOY_BLOCK + WINDOW_BLOCKS),
    ):
        machinex.fetch_events(TEST_WALLET, Chain.PEAQ)
    assert machinex.last_scan_reached_head(TEST_WALLET, Chain.PEAQ) is True


def test_unknown_head_is_not_complete(machinex: MachineXAdapter):
    """An RPC that can't report the block number proves nothing — don't guess."""
    with (
        patch("defi_tracker.adapters.machinex.rpc_enumerate_positions_v3", return_value=[]),
        patch.object(machinex._rpc, "get_block_number", return_value=None),
    ):
        machinex.fetch_events(TEST_WALLET, Chain.PEAQ)
    assert machinex.last_scan_reached_head(TEST_WALLET, Chain.PEAQ) is False


def test_one_pass_adapters_are_complete_by_default():
    """pancake / uniswap_v3 fetch everything in one pass — the ABC default
    keeps them trivially caught up."""
    uni = UniswapV3Adapter(graph_api_key="k", db_path=":memory:")
    assert uni.last_scan_reached_head(TEST_WALLET, Chain.BASE) is True


# ─────────────────────────────────────────────────────────────────────────
# Runner: a truncated-but-clean pass must not advance last_sync_ok_at
# ─────────────────────────────────────────────────────────────────────────


class _TruncatingStubAdapter(ProtocolAdapter):
    """Yields a batch, banks a cursor, then returns early without reaching
    head — the exact shape of machinex hitting its per-run block cap."""

    def __init__(self, positions=None, *, reached_head: bool, batches=None):
        self._positions = positions or []
        self._reached_head = reached_head
        self._batches = batches if batches is not None else [[]]
        self.banked_cursor: int | None = None

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
        for i, batch in enumerate(self._batches):
            yield batch
            self.banked_cursor = i  # cursor advances after the consumer persists
        return

    def fetch_events(self, wallet, chain, since_ts=0):
        return [ev for batch in self._batches for ev in batch]

    def fetch_positions(self, wallet, chain):
        return self._positions

    def last_scan_reached_head(self, wallet, chain) -> bool:
        return self._reached_head


@pytest.fixture
def only_stub_adapter(monkeypatch):
    def _install(adapter):
        monkeypatch.setattr("defi_tracker.core.runner.all_adapters", lambda: [adapter])
        monkeypatch.setattr(
            "defi_tracker.core.runner.adapters_for_chain",
            lambda chain: [adapter] if chain is Chain.BSC else [],
        )
        return adapter

    return _install


class TestTruncatedSyncHealth:
    def test_truncated_clean_sync_does_not_advance_last_sync_ok_at(
        self, storage_with_wallet, make_position, make_event, only_stub_adapter
    ):
        """The peaq catch-up case: events ARE ingested and no exception is
        raised, but the scan stopped short of head — health must stay red."""
        adapter = only_stub_adapter(
            _TruncatingStubAdapter(
                positions=[make_position()],
                reached_head=False,
                batches=[[make_event(tx_hash="0x01")]],
            )
        )
        TrackerRunner(storage_with_wallet).run()

        row = storage_with_wallet.pipeline_health()[0]
        assert adapter.banked_cursor == 0  # it really did bank progress
        assert row["last_run_at"] > 0
        assert row["last_snapshot_ok_at"] == row["last_run_at"]  # marks are fine
        assert row["last_sync_ok_at"] is None  # …but the sync is not
        assert "incomplete" in row["last_error"]

    def test_repeated_truncated_runs_keep_last_sync_ok_at_pinned(
        self, storage_with_wallet, make_position, make_event, only_stub_adapter
    ):
        """One good pass, then days of catching-up passes: the timestamp of
        the last COMPLETE sync must not move."""
        only_stub_adapter(_TruncatingStubAdapter(positions=[make_position()], reached_head=True))
        TrackerRunner(storage_with_wallet).run()
        good_ts = storage_with_wallet.pipeline_health()[0]["last_sync_ok_at"]
        assert good_ts is not None

        for tx in ("0x01", "0x02"):
            only_stub_adapter(
                _TruncatingStubAdapter(
                    positions=[make_position()],
                    reached_head=False,
                    batches=[[make_event(tx_hash=tx)]],
                )
            )
            TrackerRunner(storage_with_wallet).run()

        row = storage_with_wallet.pipeline_health()[0]
        assert row["last_sync_ok_at"] == good_ts
        # >= not >: all three passes land inside the same wall-clock second, so
        # a strict > would only be asserting that the test is slow. That the
        # later passes ran at all is shown by the error they recorded.
        assert row["last_run_at"] >= good_ts
        assert "incomplete" in row["last_error"]

    def test_quiet_wallet_with_complete_scan_is_healthy(
        self, storage_with_wallet, make_position, only_stub_adapter
    ):
        """No new events is NOT the same as not caught up — conflating the two
        is the bug this whole contract exists to avoid."""
        only_stub_adapter(
            _TruncatingStubAdapter(positions=[make_position()], reached_head=True, batches=[[]])
        )
        TrackerRunner(storage_with_wallet).run()

        row = storage_with_wallet.pipeline_health()[0]
        assert row["last_sync_ok_at"] == row["last_run_at"]
        assert row["last_error"] is None

    def test_recovery_after_catching_up_advances_again(
        self, storage_with_wallet, make_position, make_event, only_stub_adapter
    ):
        only_stub_adapter(
            _TruncatingStubAdapter(
                positions=[make_position()],
                reached_head=False,
                batches=[[make_event(tx_hash="0x01")]],
            )
        )
        TrackerRunner(storage_with_wallet).run()
        assert storage_with_wallet.pipeline_health()[0]["last_sync_ok_at"] is None

        only_stub_adapter(_TruncatingStubAdapter(positions=[make_position()], reached_head=True))
        TrackerRunner(storage_with_wallet).run()

        row = storage_with_wallet.pipeline_health()[0]
        assert row["last_sync_ok_at"] == row["last_run_at"]
        assert row["last_error"] is None


# ─────────────────────────────────────────────────────────────────────────
# source flows out of REAL adapter code, not out of test setup
# ─────────────────────────────────────────────────────────────────────────


class TestSourceFromRealAdapter:
    """The previous health tests hand-set pos.meta['source'], which no adapter
    ever did — that is exactly what let the column stay permanently NULL in
    production. These drive it through the real fetch_positions path with only
    the transport mocked."""

    def _uniswap(self, tmp_path: Path) -> UniswapV3Adapter:
        db = tmp_path / "uni.db"
        Storage(db).init_schema()
        return UniswapV3Adapter(graph_api_key="test-api-key", db_path=str(db))

    def test_uniswap_v3_positions_are_tagged_subgraph(self, tmp_path: Path):
        from tests.adapters.test_uniswap_v3 import (
            PEAQ,
            TOKEN_ID,
            WETH,
            _fake_eth_call,
            _prices,
            _subgraph_responses,
        )
        from tests.adapters.test_uniswap_v3 import TEST_WALLET as UNI_WALLET

        adapter = self._uniswap(tmp_path)
        from defi_tracker.core.types import Token

        adapter._storage.upsert_token(Token(Chain.BASE, WETH, "WETH", 18))
        adapter._storage.upsert_token(Token(Chain.BASE, PEAQ, "PEAQ", 18))
        hist, curr = _prices(adapter)
        with (
            _subgraph_responses(adapter, positions=[{"id": str(TOKEN_ID)}]),
            patch("defi_tracker.adapters._rpc.RpcClient.eth_call", side_effect=_fake_eth_call()),
            hist,
            curr,
        ):
            positions = adapter.fetch_positions(UNI_WALLET, Chain.BASE)

        assert positions
        assert all(p.meta.get("source") == "subgraph" for p in positions)

    def test_runner_records_the_source_the_adapter_reported(self, tmp_path: Path, monkeypatch):
        """End to end: real UniswapV3Adapter → runner → pipeline_health.source.
        Live rows showed 'base/uniswap_v3 source: —' next to a $27k position;
        this is the regression that catches that."""
        from defi_tracker.core.types import Token
        from tests.adapters.test_uniswap_v3 import (
            PEAQ,
            TOKEN_ID,
            WETH,
            _fake_eth_call,
            _prices,
            _subgraph_responses,
        )
        from tests.adapters.test_uniswap_v3 import TEST_WALLET as UNI_WALLET

        db = tmp_path / "run.db"
        storage = Storage(db)
        storage.init_schema()
        storage.add_wallet(UNI_WALLET)
        adapter = UniswapV3Adapter(graph_api_key="test-api-key", db_path=str(db))
        adapter._storage.upsert_token(Token(Chain.BASE, WETH, "WETH", 18))
        adapter._storage.upsert_token(Token(Chain.BASE, PEAQ, "PEAQ", 18))

        monkeypatch.setattr("defi_tracker.core.runner.all_adapters", lambda: [adapter])
        monkeypatch.setattr(
            "defi_tracker.core.runner.adapters_for_chain",
            lambda chain: [adapter] if chain is Chain.BASE else [],
        )

        hist, curr = _prices(adapter)
        with (
            _subgraph_responses(adapter, positions=[{"id": str(TOKEN_ID)}], mints=[], burns=[]),
            patch("defi_tracker.adapters._rpc.RpcClient.eth_call", side_effect=_fake_eth_call()),
            hist,
            curr,
        ):
            TrackerRunner(storage).run(chains=[Chain.BASE])

        row = storage.pipeline_health()[0]
        assert (row["chain"], row["protocol_id"]) == ("base", "uniswap_v3")
        assert row["source"] == "subgraph"
        assert row["last_sync_ok_at"] == row["last_run_at"]
        assert row["last_error"] is None
