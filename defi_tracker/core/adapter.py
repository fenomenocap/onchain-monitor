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

        deadline is a time.monotonic() value: incremental adapters should
        check it after advancing their cursor and return early, so a slow
        pass still banks the windows it completed. (The runner also stops
        consuming past the deadline as a fallback.)

        Default: a single batch delegating to fetch_events(). Override for
        incremental scanning.
        """
        yield self.fetch_events(wallet, chain, since_ts=since_ts)

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

    def supports_exact_unclaimed_fees(self) -> bool:
        """True if unclaimed fees are computed from contract storage (feeGrowthInside math)."""
        return self.info.supports_exact_fees


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
