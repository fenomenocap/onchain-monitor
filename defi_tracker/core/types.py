"""
Normalized types every protocol adapter speaks.

Design principle: these dataclasses are the *lingua franca* of the system.
Adapters convert protocol-specific data INTO these. Analytics, storage,
and alerts only ever see these. New protocols = new adapters, no changes
elsewhere.

Wallet + Chain are first-class identifiers. A single Position is always
scoped to (wallet, chain, protocol, position_key) — that 4-tuple is the
primary key everywhere in the system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

# ── Enums ──────────────────────────────────────────────────────────────────


class Chain(StrEnum):
    """Chains we support. Add new ones here; adapters declare which they handle."""

    ETHEREUM = "ethereum"
    BSC = "bsc"
    ARBITRUM = "arbitrum"
    OPTIMISM = "optimism"
    BASE = "base"
    POLYGON = "polygon"
    AVALANCHE = "avalanche"
    PEAQ = "peaq"


class ProtocolKind(StrEnum):
    """
    What *kind* of math/PnL model applies. Drives which analytics module
    runs. This is the key abstraction: 'Aave V3 on Arbitrum' and 'Compound
    V3 on Base' are different adapters but both ProtocolKind.LENDING.
    """

    CL_AMM = "cl_amm"  # Uni V3, Uni V4, Pancake Infinity — concentrated liquidity
    XYK_AMM = "xyk_amm"  # Uni V2, Pancake V2 — constant product
    STABLE_AMM = "stable_amm"  # Curve, Velodrome stable pools
    LENDING = "lending"  # Aave, Compound, Morpho
    STAKING = "staking"  # Lido, Rocket Pool, native staking
    VAULT = "vault"  # Yearn, Beefy, ERC-4626 vaults


class EventKind(StrEnum):
    """All position-affecting events normalize to one of these."""

    DEPOSIT = "deposit"  # Added liquidity / supplied / staked
    WITHDRAW = "withdraw"  # Removed liquidity / withdrew / unstaked
    COLLECT = "collect"  # Claimed fees/rewards (separate from withdraw)
    BORROW = "borrow"  # Lending-only
    REPAY = "repay"  # Lending-only
    REBALANCE = "rebalance"  # Closed old range + opened new (CL only)


class AlertSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ── Core dataclasses ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Token:
    """A token on a specific chain. Address is checksummed lowercase."""

    chain: Chain
    address: str  # 0x... lowercased
    symbol: str
    decimals: int

    @property
    def key(self) -> str:
        return f"{self.chain.value}:{self.address.lower()}"


@dataclass
class TokenAmount:
    """An amount of a token, kept as Decimal for precision."""

    token: Token
    amount: Decimal  # human-readable (already / 10**decimals)

    def usd_value(self, price_usd: Decimal) -> Decimal:
        return self.amount * price_usd


@dataclass
class PricePoint:
    """A USD price for a token at a specific moment."""

    token_key: str  # Token.key
    price_usd: Decimal
    ts: int  # unix seconds
    source: str  # 'coingecko', 'chainlink', 'pool_ratio', 'stable', etc.


@dataclass
class Position:
    """
    A single position in a protocol. The shape is *normalized* — concentrated
    liquidity, lending, staking all fit. Adapter-specific extras go in `meta`.

    The 4-tuple (wallet, chain, protocol_id, position_key) uniquely identifies
    a position across the entire system.
    """

    # ── Identity (the primary key) ──
    wallet: str  # 0x... lowercased
    chain: Chain
    protocol_id: str  # 'pancake_infinity', 'uniswap_v3', 'aave_v3', ...
    position_key: str  # adapter-defined unique-within-protocol id
    # e.g. tokenId for NFT positions, pool addr for Aave

    # ── Classification ──
    protocol_kind: ProtocolKind
    pair_label: str  # display name: 'USDC/WETH 0.05%' or 'USDC Supply'

    # ── Tokens involved ──
    # For AMMs: [token0, token1]. For lending: [supplied_token, borrowed_token?].
    # For single-asset: [token]. Adapter decides; analytics layer handles each kind.
    tokens: list[Token]

    # ── Current state (USD-denominated) ──
    current_value_usd: Decimal  # what it's worth right now
    current_balances: list[TokenAmount]  # parallel to `tokens`

    # ── Original cost basis (the user's "original LP amount in USD") ──
    # Computed by analytics layer from event history, NOT by the adapter.
    # The adapter just provides the events; the storage layer accumulates them.
    cost_basis_usd: Decimal | None = None
    net_deposited: list[TokenAmount] | None = None  # sum(deposits) - sum(withdrawals)

    # ── CL-specific (None for non-CL protocols) ──
    tick_lower: int | None = None
    tick_upper: int | None = None
    tick_current: int | None = None
    in_range: bool | None = None
    liquidity: int | None = None
    pool_address: str | None = None
    fee_tier_bps: int | None = None  # 500 = 0.05%, etc.

    # ── Unclaimed value (fees for AMMs, accrued interest for lending) ──
    unclaimed_usd: Decimal = Decimal("0")
    unclaimed_balances: list[TokenAmount] = field(default_factory=list)

    # ── Pool/market context (for APR math, alerts) ──
    pool_tvl_usd: Decimal | None = None
    seven_day_fees_usd: Decimal | None = None  # pool-wide
    position_share_pct: Decimal | None = None  # this position's share of pool liquidity

    # ── Timestamps ──
    opened_at: int | None = None  # first event ts
    last_event_at: int | None = None  # most recent event ts
    snapshot_at: int = 0  # when this snapshot was taken (set by runner)

    # ── Escape hatch for protocol-specific stuff ──
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Derive in_range from ticks whenever an adapter left it unset but the
        # ticks are known (e.g. RPC-fallback paths). A null in_range otherwise
        # makes out-of-range capital invisible to alerts and the report.
        if (
            self.in_range is None
            and self.tick_lower is not None
            and self.tick_upper is not None
            and self.tick_current is not None
        ):
            self.in_range = self.tick_lower <= self.tick_current <= self.tick_upper

    @property
    def uid(self) -> str:
        """Globally unique position identifier."""
        return f"{self.wallet}:{self.chain.value}:{self.protocol_id}:{self.position_key}"


@dataclass
class Event:
    """
    A single on-chain event that changed a position. Includes USD price AT
    THE EVENT'S BLOCK TIME — this is critical for accurate cost basis.

    The adapter is responsible for fetching historical prices when backfilling.
    """

    # ── Identity ──
    wallet: str
    chain: Chain
    protocol_id: str
    position_key: str
    tx_hash: str
    log_index: int  # uniqueness within a tx
    ts: int  # unix seconds (block timestamp)
    block_number: int

    # ── What happened ──
    kind: EventKind
    amounts: list[TokenAmount]  # signed by convention: +deposit, -withdraw
    prices_at_ts: dict[str, Decimal]  # {token_key: price_usd_at_ts}
    usd_at_ts: Decimal  # signed: + for deposit, - for withdraw

    # ── Adapter extras ──
    meta: dict = field(default_factory=dict)

    @property
    def event_uid(self) -> str:
        return f"{self.tx_hash}:{self.log_index}"


@dataclass
class Alert:
    """A signal worth surfacing to the user (Slack, terminal, etc.)."""

    position_uid: str
    kind: str  # 'OUT_OF_RANGE', 'IL_THRESHOLD', etc.
    severity: AlertSeverity
    message: str
    triggered_at: int  # unix seconds
    context: dict = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """For deduping alerts within a window."""
        return f"{self.position_uid}:{self.kind}"


# ── Adapter capability descriptor ──────────────────────────────────────────


@dataclass(frozen=True)
class AdapterInfo:
    """
    What an adapter can do. The runner uses this to decide which adapters
    to invoke for a given (wallet, chain) and which analytics modules apply.
    """

    protocol_id: str  # 'pancake_infinity'
    display_name: str  # 'PancakeSwap Infinity (V4)'
    chains: list[Chain]  # chains this adapter handles
    protocol_kind: ProtocolKind
    supports_exact_fees: bool  # True for V3/V4-style, False for estimate-only
    supports_historical: bool  # True if it can backfill events
