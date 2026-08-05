"""
Tests for PancakeInfinityAdapter.

All subgraph, RPC, and CoinGecko calls are mocked.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from defi_tracker.adapters.pancake_infinity import PancakeInfinityAdapter
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, EventKind

TEST_WALLET = "0xabc123def456abc123def456abc123def456abc1"
FAKE_API_KEY = "test-api-key"


@pytest.fixture
def adapter(tmp_path: Path) -> PancakeInfinityAdapter:
    s = Storage(tmp_path / "test.db")
    s.init_schema()
    return PancakeInfinityAdapter(
        graph_api_key=FAKE_API_KEY,
        db_path=str(tmp_path / "test.db"),
    )


def _make_subgraph_event(
    pool_id: str = "0xpool",
    tick_lower: int = -100,
    tick_upper: int = 100,
    amount: str = "1000000",
    amount0: str = "500",
    amount1: str = "0.1",
    timestamp: str = "1714521600",
    tx_id: str = "0xdeadbeef",
    log_index: str = "0",
) -> dict:
    return {
        "id": f"{tx_id}:{log_index}",
        "timestamp": timestamp,
        "amount": amount,
        "amount0": amount0,
        "amount1": amount1,
        "tickLower": str(tick_lower),
        "tickUpper": str(tick_upper),
        "logIndex": log_index,
        "blockNumber": "12345",
        "pool": {
            "id": pool_id,
            "sqrtPrice": "79228162514264337593543950336",  # 1:1 price
            "tick": "0",
            "feeTier": "500",
            "liquidity": "10000000",
            "token0Price": "1.0",
            "token1Price": "1.0",
            "totalValueLockedUSD": "1000000",
            "feesUSD": "100",
            "createdAtTimestamp": "1700000000",
            "token0": {
                "id": "0x55d398326f99059ff775485246999027b3197955",
                "symbol": "USDT",
                "decimals": "18",
            },
            "token1": {
                "id": "0x2170ed0880ac9a755fd29b2688956bd959f933f8",
                "symbol": "ETH",
                "decimals": "18",
            },
            "poolDayData": [],
        },
        "transaction": {"id": tx_id, "timestamp": timestamp},
    }


# ── fetch_events tests ─────────────────────────────────────────────────────


def test_fetch_events_returns_normalized_events(adapter, mock_subgraph, mock_coingecko):
    """fetch_events() returns a list of normalized Event objects."""
    event_data = _make_subgraph_event()
    mock_subgraph.return_value.json.return_value = {"data": {"modifyLiquidities": [event_data]}}
    mock_coingecko.return_value.json.return_value = {}  # no prices needed (stablecoins)

    events = adapter.fetch_events(TEST_WALLET, Chain.BSC, since_ts=0)

    assert len(events) == 1
    ev = events[0]
    assert ev.wallet == TEST_WALLET.lower()
    assert ev.chain == Chain.BSC
    assert ev.protocol_id == "pancake_infinity"
    assert ev.kind == EventKind.DEPOSIT
    assert ev.tx_hash == "0xdeadbeef"


def test_fetch_events_empty_subgraph_returns_empty_list(adapter, mock_subgraph, mock_coingecko):
    """Empty subgraph result → empty list (not None, not error)."""
    mock_subgraph.return_value.json.return_value = {"data": {"modifyLiquidities": []}}
    events = adapter.fetch_events(TEST_WALLET, Chain.BSC, since_ts=0)
    assert events == []


def test_fetch_events_wrong_chain_returns_empty(adapter):
    """Non-BSC chain → empty list without any network calls."""
    with patch("requests.post") as mock_post:
        events = adapter.fetch_events(TEST_WALLET, Chain.ETHEREUM, since_ts=0)
    assert events == []
    mock_post.assert_not_called()


def test_fetch_events_withdraw_event_kind(adapter, mock_subgraph, mock_coingecko):
    """Negative liquidity delta → EventKind.WITHDRAW."""
    event_data = _make_subgraph_event(amount="-500000")
    mock_subgraph.return_value.json.return_value = {"data": {"modifyLiquidities": [event_data]}}
    mock_coingecko.return_value.json.return_value = {}

    events = adapter.fetch_events(TEST_WALLET, Chain.BSC, since_ts=0)
    assert events[0].kind == EventKind.WITHDRAW


# ── withdraw fee recovery tests ─────────────────────────────────────────────


def test_withdraw_with_fee_surplus_emits_collect_pair(adapter, mock_subgraph, mock_coingecko):
    """Receipt payout above principal → WITHDRAW plus a derived COLLECT event."""
    from decimal import Decimal

    event_data = _make_subgraph_event(amount="-500000", amount0="-500", amount1="-0.1")
    mock_subgraph.return_value.json.return_value = {"data": {"modifyLiquidities": [event_data]}}
    mock_coingecko.return_value.json.return_value = {}

    with patch.object(
        adapter, "_collected_fee_amounts", return_value=(Decimal("510"), Decimal("0.1"))
    ):
        events = adapter.fetch_events(TEST_WALLET, Chain.BSC, since_ts=0)

    assert [e.kind for e in events] == [EventKind.WITHDRAW, EventKind.COLLECT]
    withdraw, collect = events
    assert collect.amounts[0].amount == Decimal("10")  # 510 paid − 500 principal
    assert collect.amounts[1].amount == Decimal("0")
    assert collect.tx_hash == withdraw.tx_hash
    assert collect.log_index != withdraw.log_index  # unique event_uid
    assert collect.meta["derived_from_withdraw"] is True
    assert collect.usd_at_ts == Decimal("10")  # USDT at $1


def test_withdraw_exact_principal_emits_no_collect(adapter, mock_subgraph, mock_coingecko):
    from decimal import Decimal

    event_data = _make_subgraph_event(amount="-500000", amount0="-500", amount1="-0.1")
    mock_subgraph.return_value.json.return_value = {"data": {"modifyLiquidities": [event_data]}}
    mock_coingecko.return_value.json.return_value = {}

    with patch.object(
        adapter, "_collected_fee_amounts", return_value=(Decimal("500"), Decimal("0.1"))
    ):
        events = adapter.fetch_events(TEST_WALLET, Chain.BSC, since_ts=0)

    assert [e.kind for e in events] == [EventKind.WITHDRAW]


def test_withdraw_without_rpc_skips_fee_recovery(adapter, mock_subgraph, mock_coingecko):
    """No RPC configured → withdraw recorded as before, receipt never fetched."""
    event_data = _make_subgraph_event(amount="-500000", amount0="-500", amount1="-0.1")
    mock_subgraph.return_value.json.return_value = {"data": {"modifyLiquidities": [event_data]}}
    mock_coingecko.return_value.json.return_value = {}

    adapter._rpc = None
    with patch.object(adapter, "_collected_fee_amounts") as mock_fees:
        events = adapter.fetch_events(TEST_WALLET, Chain.BSC, since_ts=0)

    assert [e.kind for e in events] == [EventKind.WITHDRAW]
    mock_fees.assert_not_called()


# ── auth failure tests ─────────────────────────────────────────────────────


def test_auth_failure_raises_runtime_error(adapter):
    """HTTP 401 from subgraph raises RuntimeError (not silent empty result)."""
    auth_fail = MagicMock()
    auth_fail.status_code = 401

    with (
        patch("requests.post", return_value=auth_fail),
        pytest.raises(RuntimeError, match="auth failed"),
    ):
        adapter.fetch_events(TEST_WALLET, Chain.BSC, since_ts=0)


def test_403_raises_runtime_error(adapter):
    """HTTP 403 from subgraph also raises RuntimeError."""
    auth_fail = MagicMock()
    auth_fail.status_code = 403

    with (
        patch("requests.post", return_value=auth_fail),
        pytest.raises(RuntimeError, match="auth failed"),
    ):
        adapter.fetch_events(TEST_WALLET, Chain.BSC, since_ts=0)


# ── subgraph outage tests ───────────────────────────────────────────────────


def test_fetch_events_raises_on_subgraph_outage(adapter):
    with (
        patch.object(adapter, "_run_subgraph_query", return_value=None),
        pytest.raises(RuntimeError, match="no data"),
    ):
        adapter.fetch_events(TEST_WALLET, Chain.BSC, since_ts=0)


# ── RPC fallback tests (fetch_positions during a subgraph outage) ──────────
#
# Since the 2026-07 deterministic indexing error on the Infinity CL subgraph,
# fetch_positions falls back to reconstructing positions from the last
# successful snapshots (identity + tokenId) plus live RPC reads (liquidity,
# slot0, fee growth). These tests fake the chain by routing eth_call on the
# function selector.

USDT_ADDR = "0x55d398326f99059ff775485246999027b3197955"
USDC_ADDR = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
POOL_ID = "0x" + "ab" * 32
FALLBACK_POSITION_KEY = f"{POOL_ID}:-100:100"
TOKEN_ID = 737759
LIVE_LIQUIDITY = 5 * 10**18
SQRT_1_TO_1 = 79228162514264337593543950336  # 2^96 → tick 0, price 1:1


def _fake_eth_call(liquidity: int = LIVE_LIQUIDITY):
    """eth_call stub routing on selector; all feeGrowth values 0 → no fees."""
    from defi_tracker.adapters._cl_math import (
        SEL_V4_FEE_GROWTH_GLOBALS,
        SEL_V4_GET_LIQUIDITY,
        SEL_V4_GET_POSITION,
        SEL_V4_GET_SLOT0,
        SEL_V4_POOL_TICK_INFO,
    )

    def _call(to: str, data: bytes, block: str = "latest") -> bytes | None:
        sel = data[:4]
        if sel == SEL_V4_GET_POSITION:
            return liquidity.to_bytes(32, "big") + b"\x00" * 64
        if sel == SEL_V4_GET_SLOT0:
            return SQRT_1_TO_1.to_bytes(32, "big") + b"\x00" * 96  # tick 0
        if sel == SEL_V4_GET_LIQUIDITY:
            return (liquidity * 4).to_bytes(32, "big")
        if sel == SEL_V4_FEE_GROWTH_GLOBALS:
            return b"\x00" * 64
        if sel == SEL_V4_POOL_TICK_INFO:
            return b"\x00" * 128
        return None

    return _call


@pytest.fixture
def adapter_with_history(tmp_path: Path, make_position, usdt_bsc) -> PancakeInfinityAdapter:
    """Adapter whose DB holds a prior snapshot (with tokenId) for one open
    stablecoin/stablecoin position — the state the fallback reconstructs from."""
    from defi_tracker.core.types import Token, TokenAmount

    db = tmp_path / "test.db"
    s = Storage(db)
    s.init_schema()
    usdc = Token(Chain.BSC, USDC_ADDR, "USDC", 18)
    s.upsert_token(usdt_bsc)
    s.upsert_token(usdc)
    pos = make_position(
        position_key=FALLBACK_POSITION_KEY,
        tick_lower=-100,
        tick_upper=100,
        tick_current=0,
        tokens=[usdt_bsc, usdc],
    )
    pos.current_balances = [
        TokenAmount(usdt_bsc, Decimal("1000")),
        TokenAmount(usdc, Decimal("1000")),
    ]
    pos.meta = {"token_id": TOKEN_ID, "event_count": 3}
    s.write_snapshot(position=pos)
    return PancakeInfinityAdapter(graph_api_key=FAKE_API_KEY, db_path=str(db))


def test_fetch_positions_outage_falls_back_to_rpc(adapter_with_history):
    """Subgraph down → position rebuilt from snapshot identity + live RPC."""
    with (
        patch.object(adapter_with_history, "_run_subgraph_query", return_value=None),
        patch(
            "defi_tracker.adapters._rpc.RpcClient.eth_call",
            side_effect=_fake_eth_call(),
        ),
    ):
        positions = adapter_with_history.fetch_positions(TEST_WALLET, Chain.BSC)

    assert len(positions) == 1
    pos = positions[0]
    assert pos.position_key == FALLBACK_POSITION_KEY
    assert pos.liquidity == LIVE_LIQUIDITY  # from chain, not from stored events
    assert pos.tick_current == 0
    assert pos.in_range is True
    assert pos.meta["source"] == "rpc_fallback"
    assert pos.meta["token_id"] == TOKEN_ID
    # 1:1 pool, both stablecoins at $1 → value > 0 without any network pricing
    assert pos.current_value_usd > 0
    # pool context carried forward from the last snapshot
    assert pos.pool_tvl_usd == Decimal("10000000")
    assert pos.seven_day_fees_usd == Decimal("5000")


def test_fetch_positions_outage_closed_position_is_omitted(adapter_with_history):
    """Zero on-chain liquidity during outage → omitted (runner tombstones it)."""
    with (
        patch.object(adapter_with_history, "_run_subgraph_query", return_value=None),
        patch(
            "defi_tracker.adapters._rpc.RpcClient.eth_call",
            side_effect=_fake_eth_call(liquidity=0),
        ),
    ):
        positions = adapter_with_history.fetch_positions(TEST_WALLET, Chain.BSC)
    assert positions == []


def test_fetch_positions_outage_rpc_failure_raises(adapter_with_history):
    """If the chain read fails too, raise — never guess, never tombstone."""
    with (
        patch.object(adapter_with_history, "_run_subgraph_query", return_value=None),
        patch("defi_tracker.adapters._rpc.RpcClient.eth_call", return_value=None),
        pytest.raises(RuntimeError, match="getPosition failed"),
    ):
        adapter_with_history.fetch_positions(TEST_WALLET, Chain.BSC)


def test_fetch_positions_outage_missing_token_id_raises(
    adapter_with_history, make_position, usdt_bsc
):
    """A stored position without a tokenId can't be verified on-chain → raise."""
    from defi_tracker.core.types import Token

    pos = make_position(
        position_key=f"{POOL_ID}:-200:200",
        tokens=[usdt_bsc, Token(Chain.BSC, USDC_ADDR, "USDC", 18)],
    )
    pos.meta = {}  # no token_id
    adapter_with_history._storage.write_snapshot(position=pos)
    with (
        patch.object(adapter_with_history, "_run_subgraph_query", return_value=None),
        patch(
            "defi_tracker.adapters._rpc.RpcClient.eth_call",
            side_effect=_fake_eth_call(),
        ),
        pytest.raises(RuntimeError, match="tokenId"),
    ):
        adapter_with_history.fetch_positions(TEST_WALLET, Chain.BSC)


def test_fetch_positions_outage_without_rpc_raises(tmp_path: Path):
    """Subgraph down and no RPC configured → loud failure, marks stay stale."""
    db = tmp_path / "test.db"
    Storage(db).init_schema()
    adapter = PancakeInfinityAdapter(
        graph_api_key=FAKE_API_KEY, rpc_url="", db_path=str(db)
    )
    with (
        patch.object(adapter, "_run_subgraph_query", return_value=None),
        pytest.raises(RuntimeError, match="no BSC RPC"),
    ):
        adapter.fetch_positions(TEST_WALLET, Chain.BSC)


def test_fetch_positions_outage_no_history_returns_empty(adapter):
    """Outage + nothing ever snapshotted → [] (nothing to reconstruct or
    tombstone; a raise would just hide 'this wallet has no BSC positions')."""
    with (
        patch.object(adapter, "_run_subgraph_query", return_value=None),
        patch(
            "defi_tracker.adapters._rpc.RpcClient.eth_call",
            side_effect=_fake_eth_call(),
        ),
    ):
        assert adapter.fetch_positions(TEST_WALLET, Chain.BSC) == []


# ── adapter identity tests ─────────────────────────────────────────────────


def test_adapter_info(adapter):
    """Adapter info matches expected values."""
    info = adapter.info
    assert info.protocol_id == "pancake_infinity"
    assert Chain.BSC in info.chains
    assert info.supports_exact_fees is True
