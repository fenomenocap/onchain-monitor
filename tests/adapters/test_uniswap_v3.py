"""
Tests for UniswapV3Adapter.

All subgraph, RPC and CoinGecko traffic is mocked — no live network.
Shapes follow the live BASE subgraph payload for a WETH/PEAQ position (pool
0x99188f…4ad1); wallet, tokenId, tx ids and amounts are synthetic.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from defi_tracker.adapters.uniswap_v3 import V3_CHAINS, UniswapV3Adapter
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, EventKind

TEST_WALLET = "0x1111111111111111111111111111111111111111"
FAKE_API_KEY = "test-api-key"

POOL_ID = "0x99188f1755b91b95e1f4d732536a833fb82b4ad1"
WETH = "0x4200000000000000000000000000000000000006"
PEAQ = "0x9b56b112bbd6343a8961a093315a9a60b8cb1f36"
TOKEN_ID = 4000001
TICK_LOWER, TICK_UPPER = -887220, 887220
LIQUIDITY = 2400000000000000000000
# sqrtPriceX96 at tick 116024 (live pool value)
SQRT_PRICE = 26192873518947295348558153299534
TICK_CURRENT = 116024
FEE = 3000


@pytest.fixture
def adapter(tmp_path: Path) -> UniswapV3Adapter:
    db = tmp_path / "test.db"
    Storage(db).init_schema()
    return UniswapV3Adapter(graph_api_key=FAKE_API_KEY, db_path=str(db))


def _mint(
    amount0: str = "5.123456789012345678",
    amount1: str = "870000.123456789012345678",
    amount_usd: str = "13500.000000000000000000",
    timestamp: str = "1776376000",
    tx_id: str = "0xaaaa0001",
    log_index: str = "671",
) -> dict:
    """A mints/burns row shaped exactly like the live BASE subgraph response."""
    return {
        "id": f"{tx_id}#{log_index}",
        "timestamp": timestamp,
        "logIndex": log_index,
        "amount0": amount0,
        "amount1": amount1,
        "amountUSD": amount_usd,
        "tickLower": str(TICK_LOWER),
        "tickUpper": str(TICK_UPPER),
        "token0": {"id": WETH, "symbol": "WETH", "decimals": "18"},
        "token1": {"id": PEAQ, "symbol": "PEAQ", "decimals": "18"},
        "pool": {
            "id": POOL_ID,
            "tick": str(TICK_CURRENT),
            "sqrtPrice": str(SQRT_PRICE),
            "feeTier": str(FEE),
            "totalValueLockedUSD": "30000.000000000000000000",
        },
        "transaction": {"id": tx_id},
    }


def _poke_burn(tx_id: str = "0xaaaa0002", log_index: str = "2233") -> dict:
    """The real BASE burns: a zero-liquidity NFPM poke ahead of collect()."""
    return _mint(
        amount0="0",
        amount1="0",
        amount_usd="0",
        timestamp="1783681200",
        tx_id=tx_id,
        log_index=log_index,
    )


def _subgraph_responses(adapter: UniswapV3Adapter, **entities: list[dict]) -> MagicMock:
    """Patch _run_subgraph_query, routing on which entity the query asks for."""

    def _run(query: str, url: str, max_retries: int | None = None) -> dict:
        for name, rows in entities.items():
            if f"{name}(" in query:
                return {name: rows}
        return {}

    return patch.object(adapter, "_run_subgraph_query", side_effect=_run)


def _prices(adapter: UniswapV3Adapter, weth: str = "3000", peaq: str = "0.06"):
    """Stub historical/current pricing so no CoinGecko traffic happens."""

    def _price(token, *args):
        from defi_tracker.core.types import PricePoint

        value = Decimal(weth) if token.address.lower() == WETH else Decimal(peaq)
        return PricePoint(token.key, value, 0, "test")

    return (
        patch.object(adapter, "get_historical_price", side_effect=_price),
        patch.object(adapter, "get_current_price", side_effect=_price),
    )


# ── TASK 1: Base subgraph deployment ───────────────────────────────────────


def test_base_uses_eoa_owner_subgraph():
    """Base must use the deployment that indexes position.owner as the EOA.

    The old id (HMuAwufq…) indexes owner as the NonfungiblePositionManager, so
    owner-filtered position queries came back empty and every Base position
    was tombstoned.
    """
    assert V3_CHAINS[Chain.BASE]["subgraph_id"] == "43Hwfi3dJSoGpyas9VwNoDAv55yjgGrPpNSmbQZArzMG"


def test_other_chain_subgraph_ids_unchanged():
    """Only Base was re-verified; the other deployments must stay put."""
    assert (
        V3_CHAINS[Chain.ETHEREUM]["subgraph_id"] == "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"
    )
    assert (
        V3_CHAINS[Chain.ARBITRUM]["subgraph_id"] == "FbCGRftH4a3yZugY7TnbYgPJVEv2LvMT6oF1fxPe9aJM"
    )
    assert (
        V3_CHAINS[Chain.OPTIMISM]["subgraph_id"] == "Cghf4LfVqPiFw6fp6Y5X5Ubc8UpmUhSfJL82zwiBFLaj"
    )
    assert V3_CHAINS[Chain.POLYGON]["subgraph_id"] == "3hCPRGf4z88VC5rsBKU5AA9FBBq5nF3jbKJG7VZCbhjm"


# ── TASK 2: query failure must never read as "no positions" ────────────────


def test_fetch_positions_raises_on_subgraph_outage(adapter):
    """Transport/GraphQL failure → RuntimeError, so the runner never tombstones."""
    with (
        patch.object(adapter, "_run_subgraph_query", return_value=None),
        pytest.raises(RuntimeError, match="positions query returned no data"),
    ):
        adapter.fetch_positions(TEST_WALLET, Chain.BASE)


def test_fetch_positions_raises_when_entity_key_missing(adapter):
    """A 200 response without a 'positions' field is a schema mismatch, not empty."""
    with (
        patch.object(adapter, "_run_subgraph_query", return_value={"somethingElse": []}),
        pytest.raises(RuntimeError, match="without a 'positions' field"),
    ):
        adapter.fetch_positions(TEST_WALLET, Chain.BASE)


def test_fetch_positions_auth_failure_raises(adapter):
    """HTTP 401 propagates as RuntimeError rather than an empty position list."""
    auth_fail = MagicMock()
    auth_fail.status_code = 401
    with (
        patch("requests.post", return_value=auth_fail),
        pytest.raises(RuntimeError, match="auth failed"),
    ):
        adapter.fetch_positions(TEST_WALLET, Chain.BASE)


def test_fetch_positions_genuine_empty_returns_empty_list(adapter):
    """Query succeeded and the wallet truly holds nothing → [] (no raise)."""
    with _subgraph_responses(adapter, positions=[]):
        assert adapter.fetch_positions(TEST_WALLET, Chain.BASE) == []


def test_fetch_events_raises_on_subgraph_outage(adapter):
    """Event history must fail loudly too — a partial history skews cost basis."""
    with (
        patch.object(adapter, "_run_subgraph_query", return_value=None),
        pytest.raises(RuntimeError, match="mints query returned no data"),
    ):
        adapter.fetch_events(TEST_WALLET, Chain.BASE, since_ts=0)


def test_fetch_events_genuine_empty_returns_empty_list(adapter):
    with _subgraph_responses(adapter, mints=[], burns=[]):
        assert adapter.fetch_events(TEST_WALLET, Chain.BASE, since_ts=0) == []


def test_unsupported_chain_returns_empty_without_network(adapter):
    with patch("requests.post") as mock_post:
        assert adapter.fetch_positions(TEST_WALLET, Chain.BSC) == []
        assert adapter.fetch_events(TEST_WALLET, Chain.BSC, since_ts=0) == []
    mock_post.assert_not_called()


# ── Positions payload parsing ──────────────────────────────────────────────


def _fake_eth_call(liquidity: int = LIQUIDITY):
    """eth_call stub routing on selector, mirroring the live BASE position."""
    from defi_tracker.adapters import _cl_math as clm

    def _call(to: str, data: bytes, block: str = "latest") -> bytes | None:
        sel = data[:4]
        if sel == clm.SEL_V3_POSITIONS:
            # (nonce, operator, token0, token1, fee, tickLower, tickUpper,
            #  liquidity, feeGrowthInside0, feeGrowthInside1, owed0, owed1)
            words = [
                (0).to_bytes(32, "big"),
                bytes(12) + bytes.fromhex("00" * 20),
                bytes(12) + bytes.fromhex(WETH[2:]),
                bytes(12) + bytes.fromhex(PEAQ[2:]),
                FEE.to_bytes(32, "big"),
                (TICK_LOWER & (2**256 - 1)).to_bytes(32, "big"),
                TICK_UPPER.to_bytes(32, "big"),
                liquidity.to_bytes(32, "big"),
                bytes(32),
                bytes(32),
                bytes(32),
                bytes(32),
            ]
            return b"".join(words)
        if sel == clm.SEL_V3_GET_POOL:
            return bytes(12) + bytes.fromhex(POOL_ID[2:])
        if sel == clm.SEL_V3_SLOT0:
            return SQRT_PRICE.to_bytes(32, "big") + TICK_CURRENT.to_bytes(32, "big") + bytes(32 * 5)
        return bytes(32 * 8)

    return _call


def test_fetch_positions_parses_live_shaped_payload(adapter):
    """A positions payload shaped like the real BASE one yields one Position."""
    from defi_tracker.core.types import Token

    # Token metadata comes from the tokens table (populated by fetch_events).
    adapter._storage.upsert_token(Token(Chain.BASE, WETH, "WETH", 18))
    adapter._storage.upsert_token(Token(Chain.BASE, PEAQ, "PEAQ", 18))
    hist, curr = _prices(adapter)
    with (
        _subgraph_responses(adapter, positions=[{"id": str(TOKEN_ID)}]),
        patch("defi_tracker.adapters._rpc.RpcClient.eth_call", side_effect=_fake_eth_call()),
        hist,
        curr,
    ):
        positions = adapter.fetch_positions(TEST_WALLET, Chain.BASE)

    assert len(positions) == 1
    pos = positions[0]
    assert pos.wallet == TEST_WALLET.lower()
    assert pos.chain == Chain.BASE
    assert pos.protocol_id == "uniswap_v3"
    assert pos.meta["token_id"] == TOKEN_ID
    assert pos.pool_address.lower() == POOL_ID
    assert pos.position_key.endswith(f":{TICK_LOWER}:{TICK_UPPER}")
    assert pos.liquidity == LIQUIDITY
    assert pos.fee_tier_bps == 30
    assert pos.tick_current == TICK_CURRENT
    assert pos.in_range is True
    assert pos.pair_label == "WETH/PEAQ 0.30%"
    # Full-range position priced at WETH $3000 / PEAQ $0.06 → tens of thousands
    assert Decimal("10000") < pos.current_value_usd < Decimal("100000")


def test_fetch_positions_skips_zero_liquidity_token(adapter):
    """A tokenId the subgraph still lists but with no on-chain liquidity is skipped."""
    hist, curr = _prices(adapter)
    with (
        _subgraph_responses(adapter, positions=[{"id": str(TOKEN_ID)}]),
        patch(
            "defi_tracker.adapters._rpc.RpcClient.eth_call",
            side_effect=_fake_eth_call(liquidity=0),
        ),
        hist,
        curr,
    ):
        assert adapter.fetch_positions(TEST_WALLET, Chain.BASE) == []


def test_an_unreadable_slot0_raises_rather_than_dropping_a_live_position(adapter):
    """A dropped position would be tombstoned by the runner as closed."""
    from defi_tracker.adapters import _cl_math as clm

    _tokens(adapter)
    live = _fake_eth_call()

    def _call(to, data, block="latest"):
        return None if data[:4] == clm.SEL_V3_SLOT0 else live(to, data, block)

    hist, curr = _prices(adapter)
    with (
        _subgraph_responses(adapter, positions=[{"id": str(TOKEN_ID)}]),
        patch("defi_tracker.adapters._rpc.RpcClient.eth_call", side_effect=_call),
        hist,
        curr,
        pytest.raises(RuntimeError, match="slot0"),
    ):
        adapter.fetch_positions(TEST_WALLET, Chain.BASE)


def test_unread_decimals_are_never_guessed_or_stored(adapter):
    """No tokens row and decimals() failing: raise, store nothing."""
    live = _fake_eth_call()

    def _call(to, data, block="latest"):
        return None if data[:4] == bytes.fromhex("313ce567") else live(to, data, block)

    hist, curr = _prices(adapter)
    with (
        _subgraph_responses(adapter, positions=[{"id": str(TOKEN_ID)}]),
        patch("defi_tracker.adapters._rpc.RpcClient.eth_call", side_effect=_call),
        hist,
        curr,
        pytest.raises(RuntimeError, match="decimals"),
    ):
        adapter.fetch_positions(TEST_WALLET, Chain.BASE)
    with adapter._storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0


# ── TASK 3: zero-amount burns ──────────────────────────────────────────────


def test_zero_amount_burn_is_not_recorded_as_zero_dollar_withdraw(adapter):
    """The NFPM's burn(0) poke moves no principal — it must not enter the ledger.

    The live BASE burns come back with amount0/amount1/amountUSD all 0 while
    position.withdrawnToken0/1 stay 0: nothing was withdrawn. Recording them
    would put a $0 WITHDRAW in the ledger that looks like a real unpriced exit.
    """
    hist, curr = _prices(adapter)
    with _subgraph_responses(adapter, mints=[], burns=[_poke_burn()]), hist, curr:
        events = adapter.fetch_events(TEST_WALLET, Chain.BASE, since_ts=0)
    assert events == []


def test_nonzero_burn_still_recorded_as_priced_withdraw(adapter):
    """A real withdraw (non-zero amounts) is unaffected and priced from amounts."""
    burn = _mint(amount0="1", amount1="1000", tx_id="0xrealburn", log_index="7")
    hist, curr = _prices(adapter)
    with _subgraph_responses(adapter, mints=[], burns=[burn]), hist, curr:
        events = adapter.fetch_events(TEST_WALLET, Chain.BASE, since_ts=0)

    assert len(events) == 1
    ev = events[0]
    assert ev.kind == EventKind.WITHDRAW
    assert ev.amounts[0].amount == Decimal("-1")
    assert ev.amounts[1].amount == Decimal("-1000")
    # −1 WETH @ $3000 + −1000 PEAQ @ $0.06 = −$3060, never $0
    assert ev.usd_at_ts == Decimal("-3060")


def test_mint_and_poke_burn_together(adapter):
    """Deposits parse normally while the zero-amount burns drop out."""
    hist, curr = _prices(adapter)
    with (
        _subgraph_responses(adapter, mints=[_mint()], burns=[_poke_burn()]),
        hist,
        curr,
    ):
        events = adapter.fetch_events(TEST_WALLET, Chain.BASE, since_ts=0)

    assert [e.kind for e in events] == [EventKind.DEPOSIT]
    ev = events[0]
    assert ev.position_key == f"{POOL_ID}:{TICK_LOWER}:{TICK_UPPER}"
    assert ev.usd_at_ts > 0
    assert ev.meta["pool_id"] == POOL_ID


# ── Identity ───────────────────────────────────────────────────────────────


def test_adapter_info(adapter):
    info = adapter.info
    assert info.protocol_id == "uniswap_v3"
    assert Chain.BASE in info.chains
    assert info.supports_exact_fees is True


# ── Pool depth, 7-day fees and archive reads ───────────────────────────────


def _with_pool_liquidity(active: int):
    base = _fake_eth_call()

    def _call(to: str, data: bytes, block: str = "latest") -> bytes | None:
        if data[:4] == bytes.fromhex("1a686502"):  # liquidity()
            return active.to_bytes(32, "big")
        return base(to, data, block)

    return _call


def _tokens(adapter):
    from defi_tracker.core.types import Token

    adapter._storage.upsert_token(Token(Chain.BASE, WETH, "WETH", 18))
    adapter._storage.upsert_token(Token(Chain.BASE, PEAQ, "PEAQ", 18))


def test_a_position_carries_its_pools_depth_share_and_7d_fees(adapter):
    from defi_tracker.adapters._pool_activity import PoolActivity

    _tokens(adapter)
    hist, curr = _prices(adapter)
    act = PoolActivity(-1, Decimal("0"), Decimal("700"), 1, 2, 0)
    with (
        _subgraph_responses(adapter, positions=[{"id": str(TOKEN_ID)}]),
        patch(
            "defi_tracker.adapters._rpc.RpcClient.eth_call",
            side_effect=_with_pool_liquidity(2 * LIQUIDITY),
        ),
        patch("defi_tracker.adapters._pool_activity.fee_growth_activity", return_value=act),
        hist,
        curr,
    ):
        (pos,) = adapter.fetch_positions(TEST_WALLET, Chain.BASE)
    assert pos.seven_day_fees_usd == Decimal("700")
    assert pos.position_share_pct == Decimal("50")
    assert pos.meta["pool_liquidity"] == str(2 * LIQUIDITY)


def test_unreadable_pool_fees_are_unknown_not_zero(adapter):
    _tokens(adapter)
    hist, curr = _prices(adapter)
    with (
        _subgraph_responses(adapter, positions=[{"id": str(TOKEN_ID)}]),
        patch(
            "defi_tracker.adapters._rpc.RpcClient.eth_call",
            side_effect=_with_pool_liquidity(2 * LIQUIDITY),
        ),
        patch(
            "defi_tracker.adapters._pool_activity.fee_growth_activity",
            side_effect=RuntimeError("throttled"),
        ),
        hist,
        curr,
    ):
        (pos,) = adapter.fetch_positions(TEST_WALLET, Chain.BASE)
    assert pos.seven_day_fees_usd is None


def test_an_archive_day_enumerates_tokenids_at_that_block_and_reads_no_fee_week(adapter):
    _tokens(adapter)
    hist, curr = _prices(adapter)
    with (
        patch(
            "defi_tracker.adapters.uniswap_v3.rpc_enumerate_positions_v3", return_value=[TOKEN_ID]
        ) as enum,
        patch(
            "defi_tracker.adapters._rpc.RpcClient.eth_call",
            side_effect=_with_pool_liquidity(2 * LIQUIDITY),
        ),
        patch("defi_tracker.adapters._pool_activity.fee_growth_activity") as fees,
        hist,
        curr,
    ):
        (pos,) = adapter.fetch_positions_at(TEST_WALLET, Chain.BASE, 123, 1_755_600_000)
    assert enum.call_args.args[0].block_number == 123  # enumerated at the pinned block
    fees.assert_not_called()
    assert pos.seven_day_fees_usd is None
    assert pos.meta["source"] == "archive" and pos.meta["block"] == 123
    assert pos.snapshot_at == 1_755_600_000
