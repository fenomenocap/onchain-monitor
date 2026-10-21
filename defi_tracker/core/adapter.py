"""
ProtocolAdapter ABC and the adapter registry.

Every protocol implements ProtocolAdapter; adapters self-register via
register_adapter() at import time, gated on required env vars.
The runner discovers adapters through the registry — it never imports
adapter classes directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from defi_tracker.core.types import (
    AdapterInfo,
    Chain,
    Event,
    PoolInfo,
    PoolState,
    PoolSwap,
    Position,
    PricePoint,
    Token,
)


class ProtocolAdapter(ABC):
    """
    Implement this to support a new protocol.

    Lifecycle of one tracking run (per wallet, per chain):
      1. Runner asks: do you handle this chain?           → handles()
      2. Runner asks: list events since timestamp X       → fetch_events()
      3. Runner persists events to SQLite.
      4. Runner asks: snapshot positions right now        → fetch_positions()
      5. Runner persists snapshot to SQLite.
      6. Analytics layer joins events + snapshots → PnL, IL, MoM, alerts.
    """

    @property
    @abstractmethod
    def info(self) -> AdapterInfo:
        """Return what this adapter is and what it can do."""

    def handles(self, chain: Chain) -> bool:
        """Does this adapter support the given chain?"""
        return chain in self.info.chains

    @abstractmethod
    def fetch_events(
        self,
        wallet: str,
        chain: Chain,
        since_ts: int = 0,
    ) -> list[Event]:
        """
        Return all position-affecting events for `wallet` on `chain` since
        `since_ts` (unix seconds, inclusive). Pass 0 for full backfill.

        Must include USD prices in event.prices_at_ts for every token in
        event.amounts. Must be idempotent — same args → same event_uid set.
        Storage layer dedupes by event_uid.
        """

    def iter_event_batches(
        self,
        wallet: str,
        chain: Chain,
        since_ts: int = 0,
        deadline: float | None = None,
    ) -> Iterator[list[Event]]:
        """
        Yield events in persistence-sized batches. The runner persists each
        batch before requesting the next, so adapters that scan incrementally
        (e.g. by block window) can safely advance their own progress cursor
        *after* the yield returns — a crash or time-budget stop then loses at
        most one already-idempotent batch.

        DO NOT OVERRIDE THIS — override _iter_event_batches instead. This
        wrapper marks where a scan begins, which is what lets "dropped since
        the last scan" mean the same thing for an adapter that scans by block
        window and one that fetches its whole history in a single call. Left
        to each adapter, that reset point drifts.
        """
        self._begin_event_scan(wallet, chain)
        yield from self._iter_event_batches(wallet, chain, since_ts=since_ts, deadline=deadline)

    def _iter_event_batches(
        self,
        wallet: str,
        chain: Chain,
        since_ts: int = 0,
        deadline: float | None = None,
    ) -> Iterator[list[Event]]:
        """
        The actual scan. Default: a single batch delegating to fetch_events().
        Override for incremental scanning.

        deadline is a time.monotonic() value: incremental adapters should
        check it after advancing their cursor and return early, so a slow
        pass still banks the windows it completed. (The runner also stops
        consuming past the deadline as a fallback.)

        An override that can stop before the chain head (deadline, per-run
        block cap, missing RPC) MUST also override last_scan_reached_head().
        """
        yield self.fetch_events(wallet, chain, since_ts=since_ts)

    def _begin_event_scan(self, wallet: str, chain: Chain) -> None:  # noqa: B027
        """Hook called once at the top of every scan. Default no-op; BaseAdapter
        zeroes its dropped-event counter here.

        Deliberately NOT abstract: an adapter with no per-scan state to reset
        should not be forced to write an empty method."""

    def fetch_positions_at(self, wallet: str, chain: Chain, block: int, ts: int) -> list[Position]:
        """Positions as they stood at `block` (unix `ts`), read from archive
        state and valued at that day's prices — for filling a snapshot hole.
        Adapters that cannot do this raise NotImplementedError."""
        raise NotImplementedError

    def block_at(self, chain: Chain, ts: int, near: int | None = None) -> int:
        """The chain's last block at or before `ts`. `near`, a block believed
        close to the answer, lets the search start there instead of from
        the chain's whole span."""
        raise NotImplementedError

    # ── Pool flow history (Phase 5.3) ─────────────────────────────────────
    # How a pool's volume responds to its depth is measured from its own
    # swaps. Adapters for concentrated-liquidity pools implement these; the
    # rest keep the defaults and are skipped.

    def position_pool(self, position_key: str) -> tuple[str, int, int] | None:
        """(pool, tick_lower, tick_upper) a position sits in, read from its
        key; None when the position is not concentrated liquidity."""
        return None

    def pool_info(self, chain: Chain, pool: str) -> PoolInfo:
        """Tokens, decimals and fee of a pool. Raises when unreadable."""
        raise NotImplementedError

    def swap_history_start(self, chain: Chain) -> int:
        """The first block worth scanning for this protocol's swaps."""
        raise NotImplementedError

    def scan_swaps(
        self, chain: Chain, pools: list[str], from_block: int, to_block: int
    ) -> list[PoolSwap]:
        """Every swap in `pools` over [from_block, to_block], in chain order.
        Raises when any part of the range cannot be read — a caller banking
        a cursor must never pass blocks it did not see."""
        raise NotImplementedError

    # Where a chain's Swap logs are out of reach (the Alchemy free tier
    # serves 10 blocks of BNB logs per call), a pool's daily history comes
    # from its fee counter instead: archive reads of its state at each
    # day's close. Adapters that can read that state implement these.

    def reads_fee_counter(self, chain: Chain) -> bool:
        """True when pool_state_at works on `chain` (an archive node)."""
        return False

    def pool_state_at(self, chain: Chain, pool: str, block: int) -> PoolState:
        """The pool's fee counter, depth and price at `block`. Raises when
        any read fails — a day must never be built from a partial read."""
        raise NotImplementedError

    def last_scan_reached_head(self, wallet: str, chain: Chain) -> bool:
        """
        Did the most recent iter_event_batches() for (wallet, chain) scan all
        the way to the chain head?

        Queried by the runner immediately after it finishes consuming the
        generator, and it is the ONLY thing that makes `last_sync_ok_at`
        advance. "No exception" is not the same as "caught up": an adapter
        that stops on its time budget or a per-run block cap returns cleanly
        while events inside the un-scanned range are still missing, which
        would otherwise be reported as a healthy sync.

        Note what this is NOT: it is not "found events". A wallet with no new
        activity whose scan reached head is COMPLETE — quiet, not behind.

        Default True: adapters that fetch their whole history in one pass
        (subgraph queries returning everything, paginated to exhaustion) are
        always caught up by the time the generator ends. Incremental,
        cursor-based adapters must override.
        """
        return True

    def last_scan_dropped_events(self, wallet: str, chain: Chain) -> int:
        """
        How many on-chain events the most recent scan for (wallet, chain)
        fetched but could NOT convert into a stored Event.

        A drop is silent data loss: the event never reaches the ledger, yet the
        sync watermark advances past it, so it is never requested again. Cost
        basis, fees and yields are all quietly wrong afterwards with nothing on
        any report to say so. Counting them is what makes that visible — the
        remedy is `defi-tracker reset-cursor` and a re-run.

        Counts only LOSSY drops (unpriceable, undecodable, no block timestamp).
        Events skipped because they carry nothing to record — a zero-amount
        NFPM fee-collect poke, a principal-only payout — are not losses and
        must not be counted.

        Default 0: an adapter that never drops anything reports nothing.
        """
        return 0

    @abstractmethod
    def fetch_positions(
        self,
        wallet: str,
        chain: Chain,
    ) -> list[Position]:
        """
        Return all currently-open positions for `wallet` on `chain`, valued
        at current prices.

        Must populate: identity fields, protocol_kind, pair_label, tokens,
        current_value_usd, current_balances, unclaimed_usd, unclaimed_balances,
        in_range + tick_* (CL only), pool_tvl_usd + seven_day_fees_usd (if available).

        Do NOT populate cost_basis_usd or net_deposited — computed by analytics
        from event history.
        """

    def get_historical_price(self, token: Token, ts: int) -> PricePoint | None:
        """
        USD price of token at unix timestamp `ts`. Default: None.
        Override with CoinGecko historical, Chainlink, pool TWAP, etc.
        Used during backfill to set Event.prices_at_ts accurately.
        """
        return None

    def get_current_price(self, token: Token) -> PricePoint | None:
        """Current USD price. Default: None. Override per adapter."""
        return None


# ── Registry ──────────────────────────────────────────────────────────────

_REGISTRY: dict[str, ProtocolAdapter] = {}


def register_adapter(adapter: ProtocolAdapter) -> None:
    """Add an adapter instance to the registry. Call at module import time."""
    pid = adapter.info.protocol_id
    if pid in _REGISTRY:
        raise ValueError(f"Adapter '{pid}' already registered")
    _REGISTRY[pid] = adapter


def get_adapter(protocol_id: str) -> ProtocolAdapter:
    if protocol_id not in _REGISTRY:
        raise KeyError(f"No adapter registered for '{protocol_id}'. Registered: {list(_REGISTRY)}")
    return _REGISTRY[protocol_id]


def adapters_for_chain(chain: Chain) -> list[ProtocolAdapter]:
    """All adapters that handle a given chain."""
    return [a for a in _REGISTRY.values() if a.handles(chain)]


def all_adapters() -> list[ProtocolAdapter]:
    return list(_REGISTRY.values())
