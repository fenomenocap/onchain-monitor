"""
Shared pytest fixtures.

Fixtures here are available to every test file in `tests/` without import.
Keep them protocol-agnostic — adapter-specific fixtures belong in
`tests/adapters/conftest.py` (create as needed).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from defi_tracker.core.storage import Storage
from defi_tracker.core.types import (
    Chain,
    Event,
    EventKind,
    Position,
    ProtocolKind,
    Token,
    TokenAmount,
)

# ── Test wallet address (deterministic, never used on any real chain) ─────
# A reserved-looking address that's obviously fake. Don't change this — it
# keeps grep-ability across the test suite.
TEST_WALLET = "0xabc123def456abc123def456abc123def456abc1"


# ── Reusable token instances ──────────────────────────────────────────────
# Real BSC addresses (public stablecoins / WETH) used solely so token_key
# values are realistic. No private data here.


@pytest.fixture(scope="session")
def usdt_bsc() -> Token:
    return Token(
        chain=Chain.BSC,
        address="0x55d398326f99059ff775485246999027b3197955",
        symbol="USDT",
        decimals=18,
    )


@pytest.fixture(scope="session")
def weth_bsc() -> Token:
    return Token(
        chain=Chain.BSC,
        address="0x2170ed0880ac9a755fd29b2688956bd959f933f8",
        symbol="ETH",
        decimals=18,
    )


# ── Database fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    """A fresh SQLite DB per test, schema initialized."""
    s = Storage(tmp_path / "test.db")
    s.init_schema()
    return s


@pytest.fixture
def storage_with_wallet(storage: Storage) -> Storage:
    """Storage with TEST_WALLET pre-registered."""
    storage.add_wallet(TEST_WALLET, label="test")
    return storage


# ── Event / position factories ────────────────────────────────────────────
# These are functions, not values, because tests usually want to tweak a
# field or two. Pattern: `evt = make_event(kind=EventKind.WITHDRAW, ...)`.


@pytest.fixture
def make_event(usdt_bsc: Token, weth_bsc: Token) -> Callable[..., Event]:
    """Factory for synthetic Events with sensible defaults."""

    def _make(
        *,
        kind: EventKind = EventKind.DEPOSIT,
        ts: int = 1714521600,
        log_index: int = 0,
        tx_hash: str = "0xdeadbeef",
        usdt_amount: Decimal = Decimal("1000"),
        weth_amount: Decimal = Decimal("0.5"),
        usdt_price: Decimal = Decimal("1.0"),
        weth_price: Decimal = Decimal("3000"),
        wallet: str = TEST_WALLET,
        position_key: str = "pool_X:tick_lo:tick_hi",
        protocol_id: str = "pancake_infinity",
        chain: Chain = Chain.BSC,
    ) -> Event:
        usd_at_ts = usdt_amount * usdt_price + weth_amount * weth_price
        # Sign convention: COLLECT events have positive amounts (fees received)
        # but don't change cost basis (handled in analytics layer)
        return Event(
            wallet=wallet,
            chain=chain,
            protocol_id=protocol_id,
            position_key=position_key,
            tx_hash=tx_hash,
            log_index=log_index,
            ts=ts,
            block_number=12345,
            kind=kind,
            amounts=[
                TokenAmount(usdt_bsc, usdt_amount),
                TokenAmount(weth_bsc, weth_amount),
            ],
            prices_at_ts={usdt_bsc.key: usdt_price, weth_bsc.key: weth_price},
            usd_at_ts=usd_at_ts,
        )

    return _make


@pytest.fixture
def make_position(usdt_bsc: Token, weth_bsc: Token) -> Callable[..., Position]:
    """Factory for synthetic Positions with sensible defaults."""

    def _make(
        *,
        current_value_usd: Decimal = Decimal("3400"),
        in_range: bool = True,
        tick_lower: int = 200000,
        tick_upper: int = 210000,
        tick_current: int = 205000,
        unclaimed_usd: Decimal = Decimal("45"),
        wallet: str = TEST_WALLET,
        position_key: str = "pool_X:tick_lo:tick_hi",
        protocol_id: str = "pancake_infinity",
        protocol_kind: ProtocolKind = ProtocolKind.CL_AMM,
        pair_label: str = "USDT/ETH 0.05%",
        chain: Chain = Chain.BSC,
        tokens: list[Token] | None = None,
    ) -> Position:
        if tokens is None:
            tokens = [usdt_bsc, weth_bsc]
        return Position(
            wallet=wallet,
            chain=chain,
            protocol_id=protocol_id,
            position_key=position_key,
            protocol_kind=protocol_kind,
            pair_label=pair_label,
            tokens=tokens,
            current_value_usd=current_value_usd,
            current_balances=[
                TokenAmount(usdt_bsc, Decimal("1300")),
                TokenAmount(weth_bsc, Decimal("0.65")),
            ],
            unclaimed_usd=unclaimed_usd,
            unclaimed_balances=[
                TokenAmount(usdt_bsc, Decimal("20")),
                TokenAmount(weth_bsc, Decimal("0.008")),
            ],
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            tick_current=tick_current,
            in_range=in_range,
            liquidity=1_000_000_000,
            pool_tvl_usd=Decimal("10000000"),
            seven_day_fees_usd=Decimal("5000"),
            position_share_pct=Decimal("0.5"),
            fee_tier_bps=500,
            opened_at=1714521600,
            last_event_at=1719792000,
        )

    return _make


# ── Common price maps ─────────────────────────────────────────────────────


@pytest.fixture
def current_prices_usdt_weth(usdt_bsc: Token, weth_bsc: Token) -> dict[str, Decimal]:
    """A 'now' price map: USDT $1, WETH $3200."""
    return {
        usdt_bsc.key: Decimal("1.0"),
        weth_bsc.key: Decimal("3200"),
    }
