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
    # None means UNREADABLE, not zero — the fee-growth read failed. Reporting
    # that as $0 understates unclaimed fees and every yield derived from them,
    # so the two are kept apart all the way to the report, which shows "—" and
    # excludes the position from the total rather than counting it as nothing.
    unclaimed_usd: Decimal | None = Decimal("0")
    unclaimed_balances: list[TokenAmount] = field(default_factory=list)

    @property
    def unclaimed_known(self) -> bool:
        return self.unclaimed_usd is not None

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


@dataclass(frozen=True)
class Transfer:
    """Custody-side movement of an asset between two references — a vault, an
    address, or something outside the perimeter. Follows the `Event`
    discipline: immutable, USD at transfer time, idempotent by a stable uid.

    `chain` is a plain string, not `Chain`: custody spans networks and
    off-chain venues the position tracker does not model. When it names a
    tracked chain and `tx_hash` is set, the pair is the join key to `events` —
    a vault→LP deposit appears in both, and must be netted on it, not summed.

    Direction lives in from_ref → to_ref; `amount` is always positive. Whether
    a transfer is internal or capital crossing the perimeter is NOT stored: it
    is a classification over these rows (Phase 3.4), config-driven and
    recomputable, so a wrong rule never corrupts an immutable record.
    """

    source: str  # 'fireblocks' | 'manual'
    source_id: str  # the source's own stable id for this transfer
    ts: int  # unix seconds
    asset: str  # symbol or the source's asset id
    amount: Decimal  # > 0
    from_ref: str
    to_ref: str
    chain: str | None = None
    tx_hash: str | None = None  # lowercased
    token_key: str | None = None  # Token.key when the asset maps to one
    usd_at_ts: Decimal | None = None  # None = unpriced, never zero
    meta: dict = field(default_factory=dict)

    @property
    def transfer_uid(self) -> str:
        return f"{self.source}:{self.source_id}"


@dataclass(frozen=True)
class VaultBalance:
    """One observed custody balance: this much of `asset` in `vault_id` at
    `ts`. Append-only, like marks — a balance is an observation at a moment,
    and the history of them is what a reconciliation reads."""

    vault_id: str
    asset: str
    amount: Decimal
    ts: int
    source: str = "fireblocks"
    price_usd: Decimal | None = None  # None = unpriced

    @property
    def usd_value(self) -> Decimal | None:
        return self.amount * self.price_usd if self.price_usd is not None else None


@dataclass
class Alert:
    """A signal worth surfacing to the user (Slack, terminal, etc.)."""

    position_uid: str
    kind: str  # 'OUT_OF_RANGE', 'IL_THRESHOLD', etc.
    severity: AlertSeverity
    message: str
    triggered_at: int  # unix seconds
    context: dict = field(default_factory=dict)


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


@dataclass(frozen=True)
class PoolInfo:
    """A CL pool's identity for reading its swaps: tokens, decimals, fee."""

    chain: str
    pool: str  # lowercased address
    token0: str
    token1: str
    symbol0: str
    symbol1: str
    dec0: int
    dec1: int
    fee_ppm: int  # 10000 = 1.00%


@dataclass(frozen=True)
class PoolSwap:
    """One swap as the pool saw it, in pool-native units — the raw material
    for measuring how a pool's flow responds to its depth (Phase 5.3)."""

    pool: str
    block: int
    log_index: int
    ts: int  # unix seconds; interpolated within its scan window where logs omit it
    tick: int  # the pool's tick after the swap
    liquidity: int  # the pool's active liquidity at the swap, raw
    amount0: int  # signed pool delta, raw: positive = paid into the pool
    amount1: int


@dataclass(frozen=True)
class PoolState:
    """A CL pool's fee counter, depth and price at one block, raw units —
    where Swap logs cannot be read, two of these a day apart give the day's
    LP fees (Δ feeGrowthGlobal × active liquidity)."""

    block: int
    fee_growth0: int  # feeGrowthGlobal0X128
    fee_growth1: int
    liquidity: int  # active liquidity
    sqrt_price_x96: int  # 0 = the pool was not yet initialised
    tick: int
