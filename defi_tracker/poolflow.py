"""Pool flow history — every pool's fees and depth per day, with the book's own depth beside them.

The optimiser prices a band with pool fees ∝ depth^ε, and ε — how much a
pool's flow responds to its depth — is the one input no snapshot shows. It
is measured from history (Phase 5.3). Every swap log reports the pool's
active liquidity at that moment, and the book's own liquidity at the same
block follows from its deposit/withdraw events, so one pass over the pools'
Swap logs yields fees, total depth and the book's share of it, per day.

Invariants:
- A window's day sums land with the pools' cursors in one transaction
  (Storage.apply_pool_window), so a rerun never counts a swap twice and an
  interrupted scan resumes where it stopped.
- A cursor only advances over blocks the adapter actually read; scan_swaps
  raises rather than return a partial range.
- A pool whose book liquidity cannot be reconstructed (an event without its
  liquidity change) is not scanned: the book's depth there would be a guess.

Where Swap logs are out of reach (bsc on the Alchemy free tier: 10 blocks
per eth_getLogs), a day comes from the pool's fee counter instead — its
state read at each UTC day's last block (sync_fee_counter). The stated
approximation: a day's fees are Δ feeGrowthGlobal × the MEAN of the pool's
depth at the day's two ends, so a day whose depth swung hard between them
is misstated; its depth, and the book's, are the two ends' mean, not a
swap-weighted one. Those rows say so (source 'fee_counter', samples 2, no
swap count).
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from defi_tracker.core.adapter import ProtocolAdapter
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, PoolInfo, PoolState, PoolSwap

_log = logging.getLogger(__name__)

WINDOW_BLOCKS = 10_000  # banked per window; the adapter chunks inside it
PARALLEL = 6  # windows in flight at once
BANK_DAYS = 7  # fee-counter days banked per window
LEAD_DAYS = 30  # fee-counter days sampled before the book's first deposit (σ needs them)


@dataclass
class _BookPosition:
    lo: int
    hi: int
    liquidity: int = 0


@dataclass
class _BookPool:
    """The book's positions in one pool and the liquidity changes that built
    them, in chain order: (block, log_index, position_key, signed delta)."""

    positions: dict[str, _BookPosition] = field(default_factory=dict)
    changes: list[tuple[int, int, str, int]] = field(default_factory=list)


def book_changes(
    storage: Storage, adapter: ProtocolAdapter, chain: Chain, resolve_blocks: bool = False
) -> dict[str, list[dict]]:
    """{pool: the book's liquidity changes there, in chain order} from its
    events. A pool with any event missing its liquidity change is left out
    entirely — the book's depth there would be a guess.

    Events recorded from a subgraph that never returned a block number carry
    block 0, which would apply them before every sample. `resolve_blocks`
    (the sync paths, which are on the network anyway) places each such event
    at the chain's block for its timestamp, cached in kv_store so a block is
    searched for once."""
    pools: dict[str, list[dict]] = {}
    broken: set[str] = set()
    for ev in storage.liquidity_events(chain.value, adapter.info.protocol_id):
        where = adapter.position_pool(str(ev["position_key"]))
        if where is None:
            continue
        pool, lo, hi = where
        raw = json.loads(ev["meta_json"] or "{}").get("liquidity_delta")
        if raw is None:
            broken.add(pool)
            continue
        pools.setdefault(pool, []).append(
            {
                "block": int(ev["block_number"]),
                "log_index": int(ev["log_index"]),
                "ts": int(ev["ts"]),
                "key": str(ev["position_key"]),
                "lo": lo,
                "hi": hi,
                # Adapters disagree on the stored sign (MachineX unsigned,
                # Pancake already negative on a withdraw): the kind decides.
                "delta": abs(int(raw)) * (-1 if ev["kind"] == "withdraw" else 1),
            }  # fmt: skip
        )
    for pool in broken:
        _log.warning(
            "pool %s: an event has no liquidity change — book depth unknown, skipped", pool
        )
        pools.pop(pool, None)
    if resolve_blocks:
        for changes in pools.values():
            _place_blockless(storage, adapter, chain, changes)
    return pools


def _place_blockless(
    storage: Storage, adapter: ProtocolAdapter, chain: Chain, changes: list[dict]
) -> None:
    for c in changes:
        if c["block"] > 0:
            continue
        key = f"event_block:{chain.value}:{c['ts']}"
        cached = storage.kv_get(key)
        if cached is None:
            try:
                cached = str(adapter.block_at(chain, c["ts"]))
            except Exception as e:  # noqa: BLE001 — stays at block 0, as before
                _log.warning("no block for an event at ts %d on %s: %s", c["ts"], chain.value, e)
                continue
            storage.kv_set(key, cached)
        c["block"] = int(cached)
    changes.sort(key=lambda c: (c["block"], c["log_index"]))


def book_pools(
    storage: Storage, adapter: ProtocolAdapter, chain: Chain, resolve_blocks: bool = False
) -> dict[str, _BookPool]:
    """{pool: the book's liquidity history there}, from its events."""
    return {
        pool: _book_pool(ch)
        for pool, ch in book_changes(storage, adapter, chain, resolve_blocks).items()
    }


def _book_pool(changes: list[dict]) -> _BookPool:
    bp = _BookPool()
    for c in changes:
        bp.positions.setdefault(c["key"], _BookPosition(c["lo"], c["hi"]))
        bp.changes.append((c["block"], c["log_index"], c["key"], c["delta"]))
    return bp


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


class _Book:
    """The book's active liquidity in a pool as swaps stream past in order."""

    def __init__(self, bp: _BookPool, after: tuple[int, int]):
        self.bp = bp
        self.i = 0
        for p in bp.positions.values():
            p.liquidity = 0
        self.advance(after)

    def advance(self, upto: tuple[int, int]) -> None:
        """Apply every change strictly before (block, log_index) `upto`."""
        ch = self.bp.changes
        while self.i < len(ch) and (ch[self.i][0], ch[self.i][1]) < upto:
            _, _, key, delta = ch[self.i]
            self.bp.positions[key].liquidity += delta
            self.i += 1

    def active(self, tick: int) -> int:
        return sum(p.liquidity for p in self.bp.positions.values() if p.lo <= tick < p.hi)


def day_sums(swaps: list[PoolSwap], info: PoolInfo, book: _Book) -> list[dict]:
    """Per-day sums for one pool's swaps (already in chain order). The fee is
    the input leg × the pool's fee, in token1 at the swap's own price."""
    out: dict[str, dict] = {}
    scale = 10 ** (info.dec0 - info.dec1)
    fee = info.fee_ppm / 1_000_000
    for s in swaps:
        book.advance((s.block, s.log_index))
        mine = book.active(s.tick)
        price = math.pow(1.0001, s.tick) * scale  # token1 per token0, human
        if s.amount0 > 0:
            f1 = s.amount0 / 10**info.dec0 * fee * price
        else:
            f1 = max(s.amount1, 0) / 10**info.dec1 * fee
        d = out.get(_day(s.ts))
        if d is None:
            d = out[_day(s.ts)] = {
                "pool": info.pool, "day": _day(s.ts), "swaps": 0, "fees_token1": 0.0,
                "l_sum": 0.0, "book_l_sum": 0.0, "book_l_open": float(mine),
                "tick_open": s.tick, "first_block": s.block,
            }  # fmt: skip
        d["swaps"] += 1
        d["fees_token1"] += f1
        d["l_sum"] += float(s.liquidity)
        d["book_l_sum"] += float(mine)
        d["tick_close"] = s.tick
        d["last_block"] = s.block
    return list(out.values())


@dataclass
class SyncResult:
    pools: int
    windows: int
    swaps: int
    reached_head: bool
    head: int


def sync_pool_flow(
    storage: Storage,
    adapter: ProtocolAdapter,
    chain: Chain,
    *,
    deadline: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> SyncResult:
    """Scan the Swap logs of every pool the book has ever held, from each
    pool's cursor to the head, one banked window at a time. `deadline`
    (time.monotonic()) stops between windows."""
    pid = adapter.info.protocol_id
    books = book_pools(storage, adapter, chain, resolve_blocks=True)
    if not books:
        return SyncResult(0, 0, 0, True, 0)
    infos = {pool: _pool_info(storage, adapter, chain, pool) for pool in books}
    start = adapter.swap_history_start(chain)
    stored = {p: storage.swap_cursor(chain.value, pid, p) for p in books}
    cursor = {p: c if c is not None else start - 1 for p, c in stored.items()}
    head = adapter.block_at(chain, int(time.time()))
    windows = n_swaps = 0
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool_exec:
        while True:
            low = min(cursor.values())
            if low >= head:
                break
            if deadline is not None and time.monotonic() > deadline:
                break
            # Pools at the lowest cursor scan together, up to the next pool's
            # cursor, so no pool is ever scanned over blocks it already has.
            group = sorted(p for p, c in cursor.items() if c == low)
            above = [c for c in cursor.values() if c > low]
            limit = min(head, *(above or [head]))
            # The provider's cost is per block scanned, not per call, so
            # windows are fetched side by side — and banked strictly in
            # order: a failed window stops the pass with every earlier one
            # kept and nothing after it.
            spans: list[tuple[int, int]] = []
            a = low + 1
            while a <= limit and len(spans) < PARALLEL:
                b = min(a + WINDOW_BLOCKS - 1, limit)
                spans.append((a, b))
                a = b + 1
            futures = [pool_exec.submit(adapter.scan_swaps, chain, group, a, b) for a, b in spans]
            for (a, b), fut in zip(spans, futures, strict=True):
                swaps = fut.result()  # raises: the windows before it are already banked
                by_pool: dict[str, list[PoolSwap]] = {}
                for s in swaps:
                    by_pool.setdefault(s.pool, []).append(s)
                days: list[dict] = []
                for pool, ss in by_pool.items():
                    if pool in books:
                        start_at = (ss[0].block, ss[0].log_index)
                        days += day_sums(ss, infos[pool], _Book(books[pool], start_at))
                was: dict[str, int | str | None] = {p: stored[p] for p in group}
                if not storage.apply_pool_window(
                    chain.value, pid, days, dict.fromkeys(group, b), expected=was
                ):
                    # Another pass banked these blocks first: stop, count nothing.
                    _log.warning("swap cursor moved under this scan — another pass is running")
                    return SyncResult(len(books), windows, n_swaps, False, head)
                for p in group:
                    cursor[p] = stored[p] = b
                windows += 1
                n_swaps += len(swaps)
                if progress:
                    progress(f"blocks {a:,}–{b:,}: {len(swaps)} swaps across {len(group)} pool(s)")
    return SyncResult(len(books), windows, n_swaps, min(cursor.values()) >= head, head)


def _pool_info(storage: Storage, adapter: ProtocolAdapter, chain: Chain, pool: str) -> PoolInfo:
    cached = storage.kv_get(f"pool_info:{chain.value}:{pool}")
    if cached:
        return PoolInfo(**json.loads(cached))
    info = adapter.pool_info(chain, pool)
    storage.kv_set(f"pool_info:{chain.value}:{pool}", json.dumps(info.__dict__))
    return info


# ── Fee-counter history (where Swap logs are out of reach) ────────────────


def history_source(adapter: ProtocolAdapter, chain: Chain) -> str | None:
    """How this adapter's pools get a daily history on `chain`: 'swaps'
    (Swap logs), 'fee_counter' (archive reads of pool state), or None."""
    try:
        adapter.swap_history_start(chain)
        return "swaps"
    except NotImplementedError:
        pass
    return "fee_counter" if adapter.reads_fee_counter(chain) else None


def sync_pool_history(
    storage: Storage,
    adapter: ProtocolAdapter,
    chain: Chain,
    *,
    deadline: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> SyncResult:
    """Advance a chain's pool history by whichever source it has."""
    if history_source(adapter, chain) == "fee_counter":
        return sync_fee_counter(storage, adapter, chain, deadline=deadline, progress=progress)
    return sync_pool_flow(storage, adapter, chain, deadline=deadline, progress=progress)


def _day_close_ts(day: date) -> int:
    """The last second of a UTC day."""
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) + 86_399


def _sample_json(s: PoolState, day: date, per_day: int | None) -> str:
    return json.dumps(
        {
            "day": day.isoformat(),
            "block": s.block,
            "g0": str(s.fee_growth0),
            "g1": str(s.fee_growth1),
            "liq": str(s.liquidity),
            "sqrt": str(s.sqrt_price_x96),
            "tick": s.tick,
            "per_day": per_day,
        }  # fmt: skip
    )


def _sample_state(d: dict) -> PoolState:
    return PoolState(
        int(d["block"]), int(d["g0"]), int(d["g1"]), int(d["liq"]), int(d["sqrt"]), int(d["tick"])
    )


def fee_counter_day(
    day: str, prev: PoolState, cur: PoolState, info: PoolInfo, book_open: int, book_close: int
) -> dict:
    """A pool_days row from two day-close samples. Fees = Δ feeGrowthGlobal
    (Q128, wrapping like the contract) × the mean of the two ends' depth,
    each token's fees in token1 at the close."""
    q = 1 << 128
    two_l = prev.liquidity + cur.liquidity  # 2 × the mean depth
    f0 = (cur.fee_growth0 - prev.fee_growth0) % (1 << 256) * two_l / (2 * q) / 10**info.dec0
    f1 = (cur.fee_growth1 - prev.fee_growth1) % (1 << 256) * two_l / (2 * q) / 10**info.dec1
    price = math.pow(1.0001, cur.tick) * 10 ** (info.dec0 - info.dec1)  # token1 per token0
    return {
        "pool": info.pool, "day": day, "swaps": None, "samples": 2, "source": "fee_counter",
        "fees_token1": f0 * price + f1, "l_sum": float(two_l),
        "book_l_sum": float(book_open + book_close), "book_l_open": float(book_open),
        "tick_open": prev.tick, "tick_close": cur.tick,
        "first_block": prev.block, "last_block": cur.block,
    }  # fmt: skip


def sync_fee_counter(
    storage: Storage,
    adapter: ProtocolAdapter,
    chain: Chain,
    *,
    deadline: float | None = None,
    progress: Callable[[str], None] | None = None,
    today: date | None = None,
) -> SyncResult:
    """Sample every pool the book has held at each closed UTC day's last
    block, from each pool's cursor (its last sample) to yesterday, banking
    BANK_DAYS days at a time with the cursor compare-and-set. A day's block
    is searched from the previous day's plus its block count — a few reads
    instead of a search over the whole chain. `deadline` (time.monotonic())
    stops between days; `result.swaps` counts days banked."""
    pid = adapter.info.protocol_id
    changes = book_changes(storage, adapter, chain, resolve_blocks=True)
    if not changes:
        return SyncResult(0, 0, 0, True, 0)
    last_day = (today or datetime.now(UTC).date()) - timedelta(days=1)
    windows = n_days = head = 0
    for pool in sorted(changes):
        info = _pool_info(storage, adapter, chain, pool)
        stored = storage.kv_get(f"fee_counter:{chain.value}:{pid}:{pool}")
        prev = json.loads(stored) if stored else None
        if prev is None:
            first = min(c["ts"] for c in changes[pool])  # the book's first deposit here
            day = datetime.fromtimestamp(first, UTC).date() - timedelta(days=LEAD_DAYS)
        else:
            day = date.fromisoformat(prev["day"]) + timedelta(days=1)
        book = _Book(_book_pool(changes[pool]), (int(prev["block"]) + 1, 0) if prev else (0, 0))
        book_prev = book.active(int(prev["tick"])) if prev else 0
        rows: list[dict] = []
        cur_json = stored
        while day <= last_day:
            near = int(prev["block"]) + int(prev["per_day"]) if prev and prev["per_day"] else None
            block = adapter.block_at(chain, _day_close_ts(day), near=near)
            state = adapter.pool_state_at(chain, pool, block)
            book.advance((block + 1, 0))
            book_now = book.active(state.tick)
            if prev is not None and int(prev["sqrt"]) > 0 and state.sqrt_price_x96 > 0:
                rows.append(
                    fee_counter_day(
                        day.isoformat(), _sample_state(prev), state, info, book_prev, book_now
                    )
                )
            cur_json = _sample_json(state, day, block - int(prev["block"]) if prev else None)
            prev, book_prev, head = json.loads(cur_json), book_now, block
            day += timedelta(days=1)
            stop = deadline is not None and time.monotonic() > deadline
            if len(rows) >= BANK_DAYS or day > last_day or stop:
                if not storage.apply_pool_window(
                    chain.value, pid, rows, {pool: cur_json}, expected={pool: stored},
                    cursor_kind="fee_counter",
                ):  # fmt: skip
                    _log.warning("fee-counter cursor moved under this pass — another is running")
                    return SyncResult(len(changes), windows, n_days, False, head)
                stored = cur_json
                windows += 1
                n_days += len(rows)
                if progress:
                    progress(
                        f"{info.symbol0}/{info.symbol1} through {prev['day']}: {len(rows)} day(s)"
                    )
                rows = []
            if stop:
                return SyncResult(len(changes), windows, n_days, False, head)
    return SyncResult(len(changes), windows, n_days, True, head)


def pool_infos(storage: Storage, chain: str) -> dict[str, PoolInfo]:
    """The PoolInfo recorded for each scanned pool on a chain."""
    out = {}
    for pool in {r["pool"] for r in storage.pool_days(chain)}:
        raw = storage.kv_get(f"pool_info:{chain}:{pool}")
        if raw:
            out[pool] = PoolInfo(**json.loads(raw))
    return out
