"""
Tests for MachineXAdapter.

All RPC calls are mocked; no network traffic.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from defi_tracker.adapters.machinex import (
    DEPLOY_BLOCK,
    MAX_BLOCKS_PER_RUN,
    WINDOW_BLOCKS,
    MachineXAdapter,
)
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, Token

TEST_WALLET = "0xabc123def456abc123def456abc123def456abc1"
FAKE_BLOCK = 7_000_000


def _fake_token(_self, chain: Chain, address: str) -> Token:
    """Bypass the registry/ERC20 read — these tests are about position logic."""
    return Token(chain, address.lower(), address[2:6].upper(), 18)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "test.db")
    s.init_schema()
    return s


@pytest.fixture
def adapter(storage: Storage, tmp_path: Path) -> MachineXAdapter:
    a = MachineXAdapter(
        graph_api_key="test-api-key",
        rpc_url="http://fake-rpc",
        db_path=str(tmp_path / "test.db"),
    )
    a._storage = storage
    return a


# ── Block cursor tests ─────────────────────────────────────────────────────


def test_fetch_events_limited_to_max_blocks_per_run(adapter: MachineXAdapter, storage: Storage):
    """fetch_events must not scan more than MAX_BLOCKS_PER_RUN blocks in one call."""
    with (
        patch(
            "defi_tracker.adapters.machinex.rpc_enumerate_positions_v3",
            return_value=[],
        ),
        patch.object(adapter._rpc, "get_block_number", return_value=FAKE_BLOCK),
    ):
        adapter.fetch_events(TEST_WALLET, Chain.PEAQ, since_ts=0)

    cursor = storage.kv_get(f"block_cursor:{TEST_WALLET.lower()}:peaq")
    assert cursor is not None
    scanned_to = int(cursor) - 1  # cursor points to next-to-scan
    assert scanned_to <= DEPLOY_BLOCK + MAX_BLOCKS_PER_RUN


def test_fetch_events_cursor_advances_on_empty_result(adapter: MachineXAdapter, storage: Storage):
    """Cursor advances even when no positions / events are found, preventing a stuck loop."""
    with (
        patch(
            "defi_tracker.adapters.machinex.rpc_enumerate_positions_v3",
            return_value=[],
        ),
        patch.object(adapter._rpc, "get_block_number", return_value=FAKE_BLOCK),
    ):
        adapter.fetch_events(TEST_WALLET, Chain.PEAQ, since_ts=0)
        first_cursor = storage.kv_get(f"block_cursor:{TEST_WALLET.lower()}:peaq")

        # Second call — cursor should advance again
        adapter.fetch_events(TEST_WALLET, Chain.PEAQ, since_ts=0)
        second_cursor = storage.kv_get(f"block_cursor:{TEST_WALLET.lower()}:peaq")

    assert first_cursor is not None
    assert second_cursor is not None
    assert int(second_cursor) > int(first_cursor)


def test_fetch_events_cursor_resumes_from_saved_position(
    adapter: MachineXAdapter, storage: Storage
):
    """fetch_events starts from the persisted cursor, not from DEPLOY_BLOCK."""
    saved_block = 6_000_000
    storage.kv_set(f"block_cursor:{TEST_WALLET.lower()}:peaq", str(saved_block))

    captured_from = []

    def fake_get_logs_chunked(address, topics, from_block, to_block, **kwargs):
        captured_from.append(from_block)
        return []

    with _one_position_scan(adapter, fake_get_logs_chunked):
        adapter.fetch_events(TEST_WALLET, Chain.PEAQ, since_ts=0)

    # The scan begins where the cursor left off, and banks progress past it.
    assert captured_from and min(captured_from) == saved_block
    cursor = storage.kv_get(f"block_cursor:{TEST_WALLET.lower()}:peaq")
    assert int(cursor) > saved_block


def test_unreadable_positions_hold_the_cursor(adapter: MachineXAdapter, storage: Storage):
    """balanceOf says the wallet owns positions but none can be read: that is an
    RPC failure, not an empty wallet.

    Skipping the cursor forward here would step past a block range nobody
    scanned, and the cursor never goes back — the events in it would be lost
    permanently while the sync reported itself caught up.
    """
    saved_block = 6_000_000
    storage.kv_set(f"block_cursor:{TEST_WALLET.lower()}:peaq", str(saved_block))

    with (
        patch("defi_tracker.adapters.machinex.rpc_enumerate_positions_v3", return_value=[1]),
        patch("defi_tracker.adapters.machinex.rpc_get_position_v3", return_value=None),
        patch.object(adapter._rpc, "get_block_number", return_value=FAKE_BLOCK),
    ):
        adapter.fetch_events(TEST_WALLET, Chain.PEAQ, since_ts=0)

    assert int(storage.kv_get(f"block_cursor:{TEST_WALLET.lower()}:peaq")) == saved_block
    assert adapter.last_scan_reached_head(TEST_WALLET, Chain.PEAQ) is False


def test_one_unreadable_position_among_several_holds_the_cursor(
    adapter: MachineXAdapter, storage: Storage
):
    """Token 2 reads, token 1 does not: scanning on would step past token 1's
    events for good, so the cursor stays put."""
    saved_block = 6_000_000
    storage.kv_set(f"block_cursor:{TEST_WALLET.lower()}:peaq", str(saved_block))
    ok = ("0x" + "aa" * 20, "0x" + "bb" * 20, 3000, -60, 60, 1, 0, 0)

    with (
        patch("defi_tracker.adapters.machinex.rpc_enumerate_positions_v3", return_value=[1, 2]),
        patch(
            "defi_tracker.adapters.machinex.rpc_get_position_v3",
            side_effect=lambda rpc, nft, tid, **kw: ok if tid == 2 else None,
        ),
        patch("defi_tracker.adapters.machinex.rpc_get_pool_address_v3", return_value="0xpool"),
        patch.object(adapter._rpc, "get_block_number", return_value=FAKE_BLOCK),
    ):
        adapter.fetch_events(TEST_WALLET, Chain.PEAQ, since_ts=0)

    assert int(storage.kv_get(f"block_cursor:{TEST_WALLET.lower()}:peaq")) == saved_block


def test_enumeration_failure_holds_the_cursor(adapter: MachineXAdapter, storage: Storage):
    """RpcClient.eth_call swallows transport errors into None, so a flaky RPC
    used to be indistinguishable from 'wallet holds nothing' — and that answer
    walks the cursor forward 150k blocks without scanning."""
    saved_block = 6_000_000
    storage.kv_set(f"block_cursor:{TEST_WALLET.lower()}:peaq", str(saved_block))

    with (
        patch(
            "defi_tracker.adapters.machinex.rpc_enumerate_positions_v3",
            side_effect=RuntimeError("balanceOf failed — RPC unavailable"),
        ),
        patch.object(adapter._rpc, "get_block_number", return_value=FAKE_BLOCK),
    ):
        adapter.fetch_events(TEST_WALLET, Chain.PEAQ, since_ts=0)

    assert int(storage.kv_get(f"block_cursor:{TEST_WALLET.lower()}:peaq")) == saved_block
    assert adapter.last_scan_reached_head(TEST_WALLET, Chain.PEAQ) is False


# ── Windowed batch generator tests ──────────────────────────────────────────


@contextlib.contextmanager
def _one_position_scan(adapter: MachineXAdapter, get_logs_chunked):
    """Patch context: iter_event_batches sees one tokenId with resolvable metadata."""
    with (
        patch("time.sleep"),
        patch(
            "defi_tracker.adapters.machinex.rpc_enumerate_positions_v3",
            return_value=[1],
        ),
        patch(
            "defi_tracker.adapters.machinex.rpc_get_position_v3",
            return_value=("0x" + "aa" * 20, "0x" + "bb" * 20, 100, -10, 10, 5, 0, 0),
        ),
        patch(
            "defi_tracker.adapters.machinex.rpc_get_pool_address_v3",
            return_value="0x" + "cc" * 20,
        ),
        patch.object(adapter._rpc, "get_block_number", return_value=FAKE_BLOCK),
        patch.object(adapter._rpc, "get_logs_chunked", side_effect=get_logs_chunked),
    ):
        yield


def test_iter_event_batches_advances_cursor_only_after_consumer_resumes(
    adapter: MachineXAdapter, storage: Storage, mock_rpc
):
    key = f"block_cursor:{TEST_WALLET.lower()}:peaq"

    with _one_position_scan(adapter, lambda *a, **kw: []):
        gen = adapter.iter_event_batches(TEST_WALLET, Chain.PEAQ, since_ts=0)
        next(gen)
        # First window scanned but not yet persisted by the consumer —
        # cursor must not have moved.
        assert storage.kv_get(key) is None
        next(gen)
        assert int(storage.kv_get(key)) == DEPLOY_BLOCK + WINDOW_BLOCKS


def test_iter_event_batches_windows_cover_range_without_gap_or_overlap(
    adapter: MachineXAdapter, storage: Storage, mock_rpc
):
    windows: list[tuple[int, int]] = []

    def capture(address, topics, from_block, to_block, **kwargs):
        windows.append((from_block, to_block))
        return []

    with _one_position_scan(adapter, capture):
        for _ in adapter.iter_event_batches(TEST_WALLET, Chain.PEAQ, since_ts=0):
            pass

    # Three topics per window → dedupe to the distinct block windows
    distinct = sorted(set(windows))
    expected_end = min(DEPLOY_BLOCK + MAX_BLOCKS_PER_RUN, FAKE_BLOCK)
    assert distinct[0][0] == DEPLOY_BLOCK
    assert distinct[-1][1] == expected_end
    for (_, prev_end), (next_start, _) in zip(distinct, distinct[1:], strict=False):
        assert next_start == prev_end + 1

    cursor = storage.kv_get(f"block_cursor:{TEST_WALLET.lower()}:peaq")
    assert int(cursor) == expected_end + 1


def test_iter_event_batches_honors_deadline_after_banking_cursor(
    adapter: MachineXAdapter, storage: Storage, mock_rpc
):
    """An expired deadline stops the scan after ONE window — with its cursor saved."""
    key = f"block_cursor:{TEST_WALLET.lower()}:peaq"
    scanned: list[tuple[int, int]] = []

    def capture(address, topics, from_block, to_block, **kwargs):
        scanned.append((from_block, to_block))
        return []

    with _one_position_scan(adapter, capture):
        batches = list(
            adapter.iter_event_batches(TEST_WALLET, Chain.PEAQ, since_ts=0, deadline=0.0)
        )

    assert len(batches) == 1  # stopped after the first window
    assert set(scanned) == {(DEPLOY_BLOCK, DEPLOY_BLOCK + WINDOW_BLOCKS - 1)}
    # ...but that window's progress is banked
    assert int(storage.kv_get(key)) == DEPLOY_BLOCK + WINDOW_BLOCKS


def test_default_iter_event_batches_wraps_fetch_events(adapter: MachineXAdapter):
    """fetch_events flattens exactly what iter_event_batches yields."""
    with patch.object(adapter, "iter_event_batches", return_value=iter([["a"], [], ["b", "c"]])):
        assert adapter.fetch_events(TEST_WALLET, Chain.PEAQ) == ["a", "b", "c"]


# ── Bundled Collect fee netting tests ───────────────────────────────────────

from defi_tracker.adapters._cl_math import (  # noqa: E402
    TOPIC_V3_COLLECT,
    TOPIC_V3_DECREASE_LIQUIDITY,
    TOPIC_V3_INCREASE_LIQUIDITY,
)

TOPIC_INC = "0x" + TOPIC_V3_INCREASE_LIQUIDITY
TOPIC_DEC = "0x" + TOPIC_V3_DECREASE_LIQUIDITY
TOPIC_COL = "0x" + TOPIC_V3_COLLECT
E18 = 10**18
FAKE_NET_TS = 1_751_000_000


def _liq_log(topic0: str, tx: str, tid: int, w0: int, a0: int, a1: int, log_index: int):
    """Build a fake Increase/Decrease/Collect log. w0 = liquidity or recipient word."""
    data = w0.to_bytes(32, "big") + a0.to_bytes(32, "big") + a1.to_bytes(32, "big")
    return {
        "data": "0x" + data.hex(),
        "topics": [topic0, "0x" + tid.to_bytes(32, "big").hex()],
        "transactionHash": tx,
        "logIndex": hex(log_index),
        "blockNumber": hex(150),
    }


def _run_scan_window(adapter, storage, decrease_logs, collect_logs, increase_logs=()):
    """Drive _scan_window with fixed logs (single combined pass); prices pre-seeded at $1."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from defi_tracker.core.types import Token

    t0 = Token(Chain.PEAQ, "0x" + "aa" * 20, "AAA", 18)
    t1 = Token(Chain.PEAQ, "0x" + "bb" * 20, "BBB", 18)
    day = datetime.fromtimestamp(FAKE_NET_TS, tz=UTC).date()
    storage.cache_price(Chain.PEAQ, t0.address, day, Decimal("1"), "test")
    storage.cache_price(Chain.PEAQ, t1.address, day, Decimal("1"), "test")

    tid_meta = {1: ("pool:lo:hi:1", t0, t1)}
    tid_topics = ["0x" + (1).to_bytes(32, "big").hex()]
    # The scan makes ONE call with an OR-list of topics; return everything —
    # collects deliberately first to prove order-independence of the netting.
    all_logs = list(collect_logs) + list(increase_logs) + list(decrease_logs)

    def fake_chunked(address, topics, from_block, to_block, **kwargs):
        assert isinstance(topics[0], list) and len(topics[0]) == 3  # OR-list of 3 topics
        return all_logs

    with (
        patch("time.sleep"),
        patch.object(adapter._rpc, "get_logs_chunked", side_effect=fake_chunked),
        patch.object(adapter._rpc, "get_block_timestamp", return_value=FAKE_NET_TS),
    ):
        return adapter._scan_window(TEST_WALLET, Chain.PEAQ, tid_meta, tid_topics, 100, 200)


def test_bundled_collect_netted_against_decrease(adapter, storage):
    from decimal import Decimal

    events = _run_scan_window(
        adapter,
        storage,
        decrease_logs=[_liq_log(TOPIC_DEC, "0xtx1", 1, 999, 100 * E18, 200 * E18, 5)],
        collect_logs=[_liq_log(TOPIC_COL, "0xtx1", 1, 0xABC, 110 * E18, 205 * E18, 6)],
    )

    kinds = [e.kind.value for e in events]
    assert kinds == ["withdraw", "collect"]
    withdraw, collect = events
    assert withdraw.amounts[0].amount == Decimal(-100)
    assert withdraw.amounts[1].amount == Decimal(-200)
    assert collect.amounts[0].amount == Decimal(10)
    assert collect.amounts[1].amount == Decimal(5)
    assert collect.meta.get("netted_against_decrease") is True
    assert collect.usd_at_ts == Decimal(15)  # both tokens at $1


def test_bundled_collect_all_principal_skipped(adapter, storage):
    events = _run_scan_window(
        adapter,
        storage,
        decrease_logs=[_liq_log(TOPIC_DEC, "0xtx1", 1, 999, 100 * E18, 200 * E18, 5)],
        collect_logs=[_liq_log(TOPIC_COL, "0xtx1", 1, 0xABC, 100 * E18, 200 * E18, 6)],
    )

    assert [e.kind.value for e in events] == ["withdraw"]


def test_multiple_decreases_same_tx_summed_before_netting(adapter, storage):
    from decimal import Decimal

    events = _run_scan_window(
        adapter,
        storage,
        decrease_logs=[
            _liq_log(TOPIC_DEC, "0xtx1", 1, 999, 60 * E18, 120 * E18, 4),
            _liq_log(TOPIC_DEC, "0xtx1", 1, 999, 40 * E18, 80 * E18, 5),
        ],
        collect_logs=[_liq_log(TOPIC_COL, "0xtx1", 1, 0xABC, 110 * E18, 205 * E18, 6)],
    )

    collects = [e for e in events if e.kind.value == "collect"]
    assert len(collects) == 1
    assert collects[0].amounts[0].amount == Decimal(10)  # 110 − (60+40)
    assert collects[0].amounts[1].amount == Decimal(5)  # 205 − (120+80)


def test_collect_smaller_than_decrease_clamps_to_zero(adapter, storage):
    from decimal import Decimal

    events = _run_scan_window(
        adapter,
        storage,
        decrease_logs=[_liq_log(TOPIC_DEC, "0xtx1", 1, 999, 100 * E18, 200 * E18, 5)],
        collect_logs=[_liq_log(TOPIC_COL, "0xtx1", 1, 0xABC, 90 * E18, 205 * E18, 6)],
    )

    collects = [e for e in events if e.kind.value == "collect"]
    assert len(collects) == 1
    assert collects[0].amounts[0].amount == Decimal(0)  # clamped, not negative
    assert collects[0].amounts[1].amount == Decimal(5)


def test_standalone_collect_recorded_in_full(adapter, storage):
    from decimal import Decimal

    events = _run_scan_window(
        adapter,
        storage,
        decrease_logs=[],
        collect_logs=[_liq_log(TOPIC_COL, "0xtx9", 1, 0xABC, 3 * E18, 7 * E18, 2)],
    )

    assert [e.kind.value for e in events] == ["collect"]
    assert events[0].amounts[0].amount == Decimal(3)
    assert events[0].amounts[1].amount == Decimal(7)
    assert "netted_against_decrease" not in events[0].meta


# ── RPC get_logs retry tests ────────────────────────────────────────────────


def test_get_logs_retries_on_429():
    """get_logs must retry after a 429 response and return the eventual success."""
    from defi_tracker.adapters._rpc import RpcClient

    rpc = RpcClient("http://fake", timeout=1, max_retries=3)

    response_429 = MagicMock()
    response_429.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "429 Client Error: Too Many Requests"
    )

    response_ok = MagicMock()
    response_ok.raise_for_status.return_value = None
    response_ok.json.return_value = {"result": [{"blockNumber": "0x1"}]}

    with (
        patch("requests.post", side_effect=[response_429, response_429, response_ok]),
        patch("time.sleep"),  # don't actually sleep in tests
    ):
        logs = rpc.get_logs("0xaddr", [], from_block=1, to_block=2)

    assert logs == [{"blockNumber": "0x1"}]


def test_get_logs_returns_empty_after_max_retries():
    """get_logs returns [] after exhausting all retries."""
    from defi_tracker.adapters._rpc import RpcClient

    rpc = RpcClient("http://fake", timeout=1, max_retries=3)

    response_429 = MagicMock()
    response_429.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "429 Client Error: Too Many Requests"
    )

    with patch("requests.post", return_value=response_429), patch("time.sleep"):
        logs = rpc.get_logs("0xaddr", [], from_block=1, to_block=2)

    assert logs == []


def test_get_logs_chunked_raises_on_persistent_failure():
    """A chunk that fails all retries must raise — a silent [] would advance
    the scan cursor past events that were never fetched."""
    from defi_tracker.adapters._rpc import RpcClient

    rpc = RpcClient("http://fake", timeout=1, max_retries=3)

    response_429 = MagicMock()
    response_429.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "429 Client Error: Too Many Requests"
    )

    with (
        patch("requests.post", return_value=response_429),
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="eth_getLogs failed"),
    ):
        rpc.get_logs_chunked("0xaddr", [], from_block=1, to_block=10_000, chunk_size=5_000)


def test_get_logs_chunked_sleeps_between_chunks():
    """get_logs_chunked passes sleep_between_chunks to time.sleep between chunks."""
    from defi_tracker.adapters._rpc import RpcClient

    rpc = RpcClient("http://fake", timeout=1)

    response_ok = MagicMock()
    response_ok.raise_for_status.return_value = None
    response_ok.json.return_value = {"result": []}

    with patch("requests.post", return_value=response_ok), patch("time.sleep") as mock_sleep:
        rpc.get_logs_chunked(
            "0xaddr",
            [],
            from_block=0,
            to_block=20_000,
            chunk_size=5_000,
            sleep_between_chunks=0.3,
        )

    # 4 chunks (0-4999, 5000-9999, 10000-14999, 15000-19999, 20000) — sleep after each except last
    # from_block=0, to_block=20000 with chunk_size=5000 → chunks at 0,5k,10k,15k,20k = 5 chunks, sleep 4 times
    assert mock_sleep.call_count == 4
    mock_sleep.assert_called_with(0.3)


# ── Cache tests ─────────────────────────────────────────────────────────────


def test_fetch_positions_queries_subgraph(adapter: MachineXAdapter):
    """fetch_positions calls the subgraph, not the RPC enumerator."""
    with (
        patch.object(
            adapter, "_run_subgraph_query", return_value={"clPositions": [], "legacyPositions": []}
        ) as mock_sg,
        patch("defi_tracker.adapters.machinex.rpc_enumerate_positions_v3") as mock_rpc,
    ):
        result = adapter.fetch_positions(TEST_WALLET, Chain.PEAQ)

    mock_sg.assert_called_once()
    mock_rpc.assert_not_called()
    assert result == []


def test_fetch_positions_falls_back_to_rpc_on_subgraph_failure(adapter: MachineXAdapter):
    """Subgraph outage rebuilds from the chain rather than raising."""
    with (
        patch.object(adapter, "_run_subgraph_query", return_value=None),
        patch.object(adapter, "_fetch_positions_rpc", return_value=["sentinel"]) as fb,
    ):
        assert adapter.fetch_positions(TEST_WALLET, Chain.PEAQ) == ["sentinel"]
    fb.assert_called_once_with(TEST_WALLET, Chain.PEAQ)


@pytest.mark.parametrize(
    "data",
    [
        # A row the builder chokes on: dropping it would tombstone a live position.
        {"clPositions": [{"id": "7", "liquidity": "5", "tickLower": {"tickIdx": "x"}}]},
        # A full page: open positions may sit past the cut.
        {"clPositions": [{"id": str(i), "liquidity": "0"} for i in range(1000)]},
    ],
)
def test_an_unusable_subgraph_answer_rebuilds_from_rpc(adapter: MachineXAdapter, data):
    with (
        patch.object(adapter, "_run_subgraph_query", return_value=data),
        patch.object(adapter, "_fetch_positions_rpc", return_value=["sentinel"]) as fb,
    ):
        assert adapter.fetch_positions(TEST_WALLET, Chain.PEAQ) == ["sentinel"]
    fb.assert_called_once()


def test_fetch_positions_raises_when_outage_leaves_no_way_to_look(storage: Storage, tmp_path: Path):
    """No subgraph AND no RPC: raise. Reporting an empty book here would
    tombstone every open position on a pure infrastructure failure."""
    a = MachineXAdapter(graph_api_key="test-api-key", rpc_url="", db_path=str(tmp_path / "t.db"))
    a._storage = storage
    with (
        patch.object(a, "_run_subgraph_query", return_value=None),
        pytest.raises(RuntimeError, match="refusing to report an empty book"),
    ):
        a.fetch_positions(TEST_WALLET, Chain.PEAQ)


def test_rpc_fallback_skips_zero_liquidity_but_keeps_live_positions(adapter: MachineXAdapter):
    """An exited NFT stays enumerable with liquidity 0 — it must drop out (a
    correct tombstone) without taking the live siblings with it."""
    live = (
        "0x" + "a1" * 20,
        "0x" + "b2" * 20,
        100,
        -1000,
        1000,
        10**18,
        0,
        0,
    )
    exited = (live[0], live[1], 100, -1000, 1000, 0, 0, 0)

    def fake_position(_rpc, _mgr, token_id, **_kw):
        return exited if token_id == 2 else live

    with (
        patch("defi_tracker.adapters.machinex.rpc_enumerate_positions_v3", return_value=[1, 2]),
        patch("defi_tracker.adapters.machinex.rpc_get_position_v3", side_effect=fake_position),
        patch(
            "defi_tracker.adapters.machinex.rpc_get_pool_address_v3",
            return_value="0x" + "cc" * 20,
        ),
        patch(
            "defi_tracker.adapters.machinex.rpc_get_slot0_v3",
            return_value=(2**96, 0),
        ),
        patch.object(adapter, "_token_rpc", side_effect=_fake_token),
        patch.object(adapter, "_prices_rpc", return_value=(Decimal("1"), Decimal("1"))),
        patch(
            "defi_tracker.adapters.machinex.compute_unclaimed_fees_v3",
            return_value=(Decimal("0"), Decimal("0"), Decimal("0")),
        ),
        patch.object(adapter, "_legacy_positions_rpc", return_value=[]),
    ):
        positions = adapter._fetch_positions_rpc(TEST_WALLET, Chain.PEAQ)

    assert [p.meta["token_id"] for p in positions] == [1]
    assert positions[0].meta["source"] == "rpc_fallback"


def test_rpc_fallback_raises_when_a_held_position_is_unreadable(adapter: MachineXAdapter):
    """A partial list would tombstone whatever it left out — the exact silent
    loss this path exists to avoid."""
    with (
        patch("defi_tracker.adapters.machinex.rpc_enumerate_positions_v3", return_value=[7]),
        patch("defi_tracker.adapters.machinex.rpc_get_position_v3", return_value=None),
        pytest.raises(RuntimeError, match="unreadable"),
    ):
        adapter._fetch_positions_rpc(TEST_WALLET, Chain.PEAQ)


def test_rpc_fallback_refuses_to_undervalue_an_unpriceable_position(adapter: MachineXAdapter):
    """Both legs unpriced and no pool ratio to lean on: raise rather than book
    the position at zero."""
    tok0 = _fake_token(None, Chain.PEAQ, "0x" + "a1" * 20)
    tok1 = _fake_token(None, Chain.PEAQ, "0x" + "b2" * 20)
    with (
        patch.object(adapter, "_price_or_zero", return_value=Decimal("0")),
        pytest.raises(RuntimeError, match="cannot price"),
    ):
        adapter._prices_rpc(tok0, tok1, sqrt_price_x96=0)


def test_rpc_fallback_derives_the_unpriced_leg_from_the_pool(adapter: MachineXAdapter):
    """The subgraph is the usual source of prices for the long tail of peaq
    tokens, so losing it must not blank them: one known leg plus the pool's
    own sqrtPrice prices the other."""
    tok0 = _fake_token(None, Chain.PEAQ, "0x" + "a1" * 20)  # 18 decimals
    tok1 = _fake_token(None, Chain.PEAQ, "0x" + "b2" * 20)

    def price(token):
        return Decimal("2") if token.address == tok1.address else Decimal("0")

    with patch.object(adapter, "_price_or_zero", side_effect=price):
        # sqrtPriceX96 == 2**96 is a 1:1 raw ratio, equal decimals → 1 t1 per t0.
        p0, p1 = adapter._prices_rpc(tok0, tok1, sqrt_price_x96=2**96)
    assert p0 == Decimal("2")
    assert p1 == Decimal("2")


# ── wrong chain ──────────────────────────────────────────────────────────────


def test_fetch_events_wrong_chain_returns_empty(adapter: MachineXAdapter):
    events = adapter.fetch_events(TEST_WALLET, Chain.ETHEREUM, since_ts=0)
    assert events == []


def test_fetch_positions_wrong_chain_returns_empty(adapter: MachineXAdapter):
    positions = adapter.fetch_positions(TEST_WALLET, Chain.ETHEREUM)
    assert positions == []


# ── Block timestamp + event pricing tests ───────────────────────────────────

PEAQ_USDC = "0xbba60da06c2c5424f03f7434542280fcad453d10"
PEAQ_USDT = "0xf4d9235269a96aadafc9adae454a0618ebe37949"
FAKE_TS = 1_751_000_000  # 2025-06-27 UTC


def _fake_log(block: int = 7_100_000, amt0: int = 10**18, amt1: int = 2 * 10**18) -> dict:
    """A minimal IncreaseLiquidity-shaped log with no timestamp field."""
    data = (100).to_bytes(32, "big") + amt0.to_bytes(32, "big") + amt1.to_bytes(32, "big")
    return {
        "data": "0x" + data.hex(),
        "topics": ["0x" + "00" * 32, "0x" + (1).to_bytes(32, "big").hex()],
        "blockNumber": hex(block),
        "transactionHash": "0x" + "ab" * 32,
        "logIndex": "0x1",
    }


def _stable_tokens():
    from defi_tracker.core.types import Token

    return (
        Token(Chain.PEAQ, PEAQ_USDC, "USDC", 6),
        Token(Chain.PEAQ, PEAQ_USDT, "USDT", 6),
    )


def test_get_block_timestamp_parses_and_caches():
    from defi_tracker.adapters._rpc import RpcClient

    rpc = RpcClient("http://fake")
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"result": {"timestamp": hex(FAKE_TS)}}

    with patch("requests.post", return_value=resp) as mock_post:
        first = rpc.get_block_timestamp(7_100_000)
        second = rpc.get_block_timestamp(7_100_000)

    assert first == second == FAKE_TS
    assert mock_post.call_count == 1  # second lookup served from cache


def test_log_to_event_fetches_ts_via_rpc_when_log_lacks_timestamp(adapter: MachineXAdapter):
    from defi_tracker.core.types import EventKind

    t0, t1 = _stable_tokens()
    with patch.object(adapter._rpc, "get_block_timestamp", return_value=FAKE_TS) as mock_ts:
        ev = adapter._log_to_event(
            _fake_log(), TEST_WALLET, Chain.PEAQ, "pool:1:2:3", t0, t1, EventKind.DEPOSIT
        )

    assert ev is not None
    assert ev.ts == FAKE_TS
    mock_ts.assert_called_once_with(7_100_000)


def test_log_to_event_dropped_when_ts_unresolvable(adapter: MachineXAdapter):
    from defi_tracker.core.types import EventKind

    t0, t1 = _stable_tokens()
    with patch.object(adapter._rpc, "get_block_timestamp", return_value=None):
        ev = adapter._log_to_event(
            _fake_log(), TEST_WALLET, Chain.PEAQ, "pool:1:2:3", t0, t1, EventKind.DEPOSIT
        )

    assert ev is None


def test_price_day_cache_hit_skips_subgraph_and_coingecko(
    adapter: MachineXAdapter, storage: Storage, mock_subgraph, mock_coingecko
):
    from datetime import UTC, datetime
    from decimal import Decimal

    from defi_tracker.core.types import EventKind, Token

    t0 = Token(Chain.PEAQ, "0x" + "d1" * 20, "AAA", 18)
    t1 = Token(Chain.PEAQ, "0x" + "d2" * 20, "BBB", 18)
    day = datetime.fromtimestamp(FAKE_TS, tz=UTC).date()
    storage.cache_price(Chain.PEAQ, t0.address, day, Decimal("2.5"), "test")
    storage.cache_price(Chain.PEAQ, t1.address, day, Decimal("0.1"), "test")

    with patch.object(adapter._rpc, "get_block_timestamp", return_value=FAKE_TS):
        ev = adapter._log_to_event(
            _fake_log(), TEST_WALLET, Chain.PEAQ, "pool:1:2:3", t0, t1, EventKind.DEPOSIT
        )

    assert ev is not None
    assert ev.prices_at_ts[t0.key] == Decimal("2.5")
    assert ev.prices_at_ts[t1.key] == Decimal("0.1")
    mock_subgraph.assert_not_called()
    mock_coingecko.assert_not_called()


def test_amount_ratio_used_before_subgraph(adapter: MachineXAdapter, mock_subgraph):
    """One side stablecoin, other unpriceable → ratio fills it; subgraph never asked."""
    from decimal import Decimal

    from defi_tracker.core.types import EventKind, Token

    t0 = Token(Chain.PEAQ, PEAQ_USDC, "USDC", 6)
    t1 = Token(Chain.PEAQ, "0x" + "d3" * 20, "CCC", 18)

    not_found = MagicMock()
    not_found.status_code = 404  # CoinGecko has no id for CCC

    with (
        patch.object(adapter._rpc, "get_block_timestamp", return_value=FAKE_TS),
        patch("requests.get", return_value=not_found),
    ):
        ev = adapter._log_to_event(
            _fake_log(amt0=4 * 10**6, amt1=2 * 10**18),
            TEST_WALLET,
            Chain.PEAQ,
            "pool:1:2:3",
            t0,
            t1,
            EventKind.DEPOSIT,
        )

    assert ev is not None
    # 4 USDC against 2 CCC → CCC = $2
    assert ev.prices_at_ts[t1.key] == Decimal("2")
    mock_subgraph.assert_not_called()


def test_subgraph_price_single_attempts_and_negative_cache(adapter: MachineXAdapter, mock_subgraph):
    mock_subgraph.return_value.json.return_value = {
        "errors": [{"message": "bad indexers: {0xdead: Unavailable(missing block)}"}]
    }
    addr = "0x" + "d4" * 20

    with patch("time.sleep") as mock_sleep:
        first = adapter._subgraph_historical_price(addr, 7_100_000, FAKE_TS)
        # Same token, same day, different block — must not re-query
        second = adapter._subgraph_historical_price(addr, 7_100_500, FAKE_TS + 600)

    assert first is None and second is None
    # One day-price attempt + one block-price attempt, then negative-cached
    assert mock_subgraph.call_count == 2
    mock_sleep.assert_not_called()


def test_subgraph_day_price_preferred_and_written_to_price_cache(
    adapter: MachineXAdapter, storage: Storage, mock_subgraph
):
    from datetime import UTC, datetime
    from decimal import Decimal

    mock_subgraph.return_value.json.return_value = {
        "data": {"tokenDayDatas": [{"priceUSD": "0.5"}]}
    }
    addr = "0x" + "d5" * 20

    price = adapter._subgraph_historical_price(addr, 7_100_000, FAKE_TS)

    assert price == Decimal("0.5")
    assert mock_subgraph.call_count == 1  # day price hit — block query not needed
    day = datetime.fromtimestamp(FAKE_TS, tz=UTC).date()
    assert storage.cached_price(Chain.PEAQ, addr, day) == Decimal("0.5")


def test_subgraph_block_price_used_when_day_price_missing(
    adapter: MachineXAdapter, storage: Storage, mock_subgraph
):
    from decimal import Decimal

    day_resp = MagicMock()
    day_resp.status_code = 200
    day_resp.raise_for_status.return_value = None
    day_resp.json.return_value = {"data": {"tokenDayDatas": []}}
    block_resp = MagicMock()
    block_resp.status_code = 200
    block_resp.raise_for_status.return_value = None
    block_resp.json.return_value = {"data": {"token": {"priceUSD": "0.7"}}}
    mock_subgraph.side_effect = [day_resp, block_resp]
    addr = "0x" + "d6" * 20

    price = adapter._subgraph_historical_price(addr, 7_100_000, FAKE_TS)

    assert price == Decimal("0.7")
    assert mock_subgraph.call_count == 2


# ── ABI + factory selector tests ─────────────────────────────────────────────


def test_machinex_factory_uses_int24_selector():
    """rpc_get_pool_address_v3 with int24_third_arg=True uses the MachineX selector."""
    from defi_tracker.adapters._cl_math import (
        SEL_V3_GET_POOL,
        SEL_V3_GET_POOL_INT24,
        rpc_get_pool_address_v3,
    )

    assert SEL_V3_GET_POOL != SEL_V3_GET_POOL_INT24, "selectors must differ"

    captured_data = []

    def fake_eth_call(to, data, block="latest"):
        captured_data.append(data[:4])
        return b"\x00" * 32  # zero address → returns None

    from unittest.mock import patch

    from defi_tracker.adapters._rpc import RpcClient

    rpc = RpcClient("http://fake")
    with patch.object(rpc, "eth_call", side_effect=fake_eth_call):
        rpc_get_pool_address_v3(rpc, "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40, 100)
        rpc_get_pool_address_v3(
            rpc, "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40, 100, int24_third_arg=True
        )

    assert captured_data[0] == SEL_V3_GET_POOL
    assert captured_data[1] == SEL_V3_GET_POOL_INT24


def test_nft_header_words_0_shifts_read_offsets():
    """rpc_get_position_v3 with nft_header_words=0 reads token0 at offset 0, not 64."""
    from unittest.mock import patch

    from defi_tracker.adapters._cl_math import rpc_get_position_v3
    from defi_tracker.adapters._rpc import RpcClient

    # 10 words: token0 addr at word0, token1 at word1, fee at word2 ...
    token0_addr = bytes.fromhex("165bcb970836f83c15b22c3c1622279d97a20446")
    token1_addr = bytes.fromhex("3cd66d2e1fac1751b0a20bebf6ca4c9699bb12d7")
    fee = 100
    tick_lower = 17900
    tick_upper = 34700
    liquidity = 12345678

    def make_word(val, is_addr=False):
        if is_addr:
            return b"\x00" * 12 + val
        return val.to_bytes(32, "big")

    payload = (
        make_word(token0_addr, is_addr=True)
        + make_word(token1_addr, is_addr=True)
        + make_word(fee)
        + make_word(tick_lower)
        + make_word(tick_upper)
        + make_word(liquidity)
        + make_word(0)  # fg0
        + make_word(0)  # fg1
    )  # 8 words = 256 bytes (nft_header_words=0 needs base+256)

    rpc = RpcClient("http://fake")
    with patch.object(rpc, "eth_call", return_value=payload):
        result = rpc_get_position_v3(rpc, "0xnft", 42, nft_header_words=0)

    assert result is not None
    t0, t1, f, tl, tu, liq, _, _ = result
    assert t0 == "0x" + token0_addr.hex()
    assert f == fee
    assert liq == liquidity


def test_cl_position_prefers_rpc_fee_math(adapter: MachineXAdapter):
    """With an RPC configured, unclaimed fees come from on-chain reads —
    the subgraph's tick feeGrowthOutside is unreliable across tick crossings."""
    from decimal import Decimal

    pos_data = {
        "id": "42",
        "liquidity": "1000000",
        "pool": {
            "id": "0x" + "ee" * 20,
            "sqrtPrice": str(2**96),
            "tick": "100",
            "feeTier": "10000",
            "feeGrowthGlobal0X128": "0",
            "feeGrowthGlobal1X128": "0",
        },
        "token0": {"id": "0x" + "aa" * 20, "symbol": "AAA", "decimals": "18", "priceUSD": "1"},
        "token1": {"id": "0x" + "bb" * 20, "symbol": "BBB", "decimals": "18", "priceUSD": "1"},
        "tickLower": {"tickIdx": "0", "feeGrowthOutside0X128": "0", "feeGrowthOutside1X128": "0"},
        "tickUpper": {"tickIdx": "200", "feeGrowthOutside0X128": "0", "feeGrowthOutside1X128": "0"},
        "feeGrowthInside0LastX128": "0",
        "feeGrowthInside1LastX128": "0",
    }
    sentinel = (Decimal("1"), Decimal("2"), Decimal("3"))
    with patch(
        "defi_tracker.adapters.machinex.compute_unclaimed_fees_v3", return_value=sentinel
    ) as mock_rpc_fees:
        built = adapter._build_cl_position(TEST_WALLET, Chain.PEAQ, pos_data)

    assert built is not None
    mock_rpc_fees.assert_called_once()
    assert mock_rpc_fees.call_args.args[3] == 42  # token_id
    assert built.unclaimed_usd == Decimal("3")


def test_cl_position_falls_back_to_subgraph_fee_math_without_rpc(adapter: MachineXAdapter):
    from decimal import Decimal

    adapter._rpc = None
    pos_data = {
        "id": "42",
        "liquidity": "1000000",
        "pool": {
            "id": "0x" + "ee" * 20,
            "sqrtPrice": str(2**96),
            "tick": "100",
            "feeTier": "10000",
            "feeGrowthGlobal0X128": "0",
            "feeGrowthGlobal1X128": "0",
        },
        "token0": {"id": "0x" + "aa" * 20, "symbol": "AAA", "decimals": "18", "priceUSD": "1"},
        "token1": {"id": "0x" + "bb" * 20, "symbol": "BBB", "decimals": "18", "priceUSD": "1"},
        "tickLower": {"tickIdx": "0", "feeGrowthOutside0X128": "0", "feeGrowthOutside1X128": "0"},
        "tickUpper": {"tickIdx": "200", "feeGrowthOutside0X128": "0", "feeGrowthOutside1X128": "0"},
        "feeGrowthInside0LastX128": "0",
        "feeGrowthInside1LastX128": "0",
    }
    with patch("defi_tracker.adapters.machinex.compute_unclaimed_fees_v3") as mock_rpc_fees:
        built = adapter._build_cl_position(TEST_WALLET, Chain.PEAQ, pos_data)

    assert built is not None
    mock_rpc_fees.assert_not_called()  # subgraph-pure path used
    assert built.unclaimed_usd == Decimal("0")  # zero fee growth → zero unclaimed


# ── Dropped-event accounting through the real scan path ────────────────────


def test_unpriceable_event_is_counted_as_dropped(adapter, storage):
    """No price means the event never reaches the ledger, and the cursor moves
    past it — the exact shape of the 49 peaq events lost in July 2026."""
    adapter._begin_event_scan(TEST_WALLET, Chain.PEAQ)
    # _run_scan_window seeds prices; drive _scan_window directly WITHOUT them.
    from defi_tracker.core.types import Token

    t0 = Token(Chain.PEAQ, "0x" + "aa" * 20, "AAA", 18)
    t1 = Token(Chain.PEAQ, "0x" + "bb" * 20, "BBB", 18)
    tid_meta = {1: ("pool:lo:hi:1", t0, t1)}
    logs = [_liq_log(TOPIC_INC, "0xaa", 1, 10, 5, 5, 0)]

    with (
        patch("time.sleep"),
        patch.object(adapter._rpc, "get_logs_chunked", return_value=logs),
        patch.object(adapter._rpc, "get_block_timestamp", return_value=FAKE_NET_TS),
    ):
        events = adapter._scan_window(
            TEST_WALLET, Chain.PEAQ, tid_meta, ["0x" + (1).to_bytes(32, "big").hex()], 100, 200
        )

    assert events == []
    assert adapter.last_scan_dropped_events(TEST_WALLET, Chain.PEAQ) == 1


def test_log_for_an_unknown_tokenid_is_counted(adapter, storage):
    """The tripwire for the topic filter and tid_meta drifting apart: a log we
    asked for, paid for, and cannot describe. Silently discarding it is how a
    widened filter would look like it was working."""
    adapter._begin_event_scan(TEST_WALLET, Chain.PEAQ)
    from defi_tracker.core.types import Token

    t0 = Token(Chain.PEAQ, "0x" + "aa" * 20, "AAA", 18)
    t1 = Token(Chain.PEAQ, "0x" + "bb" * 20, "BBB", 18)
    tid_meta = {1: ("pool:lo:hi:1", t0, t1)}
    logs = [_liq_log(TOPIC_INC, "0xaa", 999, 10, 5, 5, 0)]  # tid 999

    with (
        patch("time.sleep"),
        patch.object(adapter._rpc, "get_logs_chunked", return_value=logs),
        patch.object(adapter._rpc, "get_block_timestamp", return_value=FAKE_NET_TS),
    ):
        events = adapter._scan_window(
            TEST_WALLET, Chain.PEAQ, tid_meta, ["0x" + (1).to_bytes(32, "big").hex()], 100, 200
        )

    assert events == []
    assert adapter.last_scan_dropped_events(TEST_WALLET, Chain.PEAQ) == 1


def test_a_clean_scan_drops_nothing(adapter, storage):
    """The counter must stay at 0 on the happy path, or every report cries wolf."""
    adapter._begin_event_scan(TEST_WALLET, Chain.PEAQ)
    events = _run_scan_window(
        adapter,
        storage,
        [],
        [],
        increase_logs=[_liq_log(TOPIC_INC, "0xaa", 1, 10, 5, 5, 0)],
    )
    assert len(events) == 1
    assert adapter.last_scan_dropped_events(TEST_WALLET, Chain.PEAQ) == 0
