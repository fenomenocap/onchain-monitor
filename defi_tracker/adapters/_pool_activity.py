"""Pool volume and fee income from Swap logs — the numerator the subgraphs used to supply.

With both subgraphs dead, `seven_day_fees_usd` and `position_share_pct` went
unknown on bsc and peaq, which suspended NEGATIVE_CARRY there and left the
Phase 5 optimiser with no fee numerator. Both come straight off the chain:
every swap emits its amounts (and, on Infinity, the fee it paid), so 7-day
fee income is a sum over the pool's Swap logs.

Two deliberate simplifications, stated so they are not mistaken for more:
- Swaps are valued at CURRENT prices, not each swap's own. For a 7-day
  window that is the difference between a fee-rate estimate and an audit,
  and this is only ever used as the former.
- The input leg is the LARGER of the two USD legs (input = output + fee), so
  the result does not depend on which sign convention a fork uses for swap
  amounts (V3 reports pool deltas, V4-style managers the swapper's).

Results are cached per pool in kv_store for a few hours: `mark` runs hourly
and must not re-scan a week of swaps each time.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from decimal import Decimal

from eth_hash.auto import keccak

from defi_tracker.adapters._rpc import RpcClient

_log = logging.getLogger(__name__)

V3_SWAP_TOPIC = "0x" + keccak(b"Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
WINDOW_S = 7 * 86400
CACHE_S = 6 * 3600


@dataclass(frozen=True)
class PoolActivity:
    swaps: int
    volume_usd: Decimal
    fees_usd: Decimal
    from_block: int
    to_block: int
    fetched_at: int


def _signed(v: int) -> int:
    return v - (1 << 256) if v >= 1 << 255 else v


def block_at(rpc: RpcClient, ts: int, head: int, probe_back: int = 200_000) -> int:
    """A block at (or just before) unix `ts`, by interpolating block times —
    two timestamp reads, then one correction. Never after `ts`."""
    head_ts = rpc.get_block_timestamp(head)
    back = max(1, head - probe_back)
    back_ts = rpc.get_block_timestamp(back)
    if head_ts is None or back_ts is None or head_ts <= back_ts:
        raise RuntimeError("block timestamps unreadable — cannot place the window")
    per_block = (head_ts - back_ts) / (head - back)
    guess = max(1, int(head - (head_ts - ts) / per_block))
    guess_ts = rpc.get_block_timestamp(guess)
    if guess_ts is not None and guess_ts > ts:
        guess = max(1, guess - int((guess_ts - ts) / per_block) - 1)
    return guess


def _sum_swaps(
    logs: list[dict],
    price0: Decimal,
    price1: Decimal,
    dec0: int,
    dec1: int,
    fee_ppm: int | None,
    fee_word: int | None,
) -> tuple[int, Decimal, Decimal]:
    volume = fees = Decimal("0")
    for lg in logs:
        raw = lg["data"][2:]
        w = [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]
        leg0 = abs(Decimal(_signed(w[0]))) / Decimal(10**dec0) * price0
        leg1 = abs(Decimal(_signed(w[1]))) / Decimal(10**dec1) * price1
        vin = max(leg0, leg1)
        ppm = w[fee_word] if fee_word is not None else fee_ppm
        volume += vin
        fees += vin * Decimal(ppm or 0) / Decimal(1_000_000)
    return len(logs), volume, fees


def pool_activity(
    rpc: RpcClient,
    *,
    cache,
    cache_key: str,
    address: str,
    topics: list,
    price0: Decimal,
    price1: Decimal,
    dec0: int,
    dec1: int,
    fee_ppm: int | None = None,
    fee_word: int | None = None,
    chunk: int = 10_000,
    now: int | None = None,
) -> PoolActivity:
    """7-day swap volume and fee income for one pool. `cache` is anything
    with kv_get/kv_set (Storage). Pass `fee_ppm` for a fixed-fee pool (V3,
    the pool's fee()), or `fee_word` for a Swap event that carries its own
    fee (Infinity: word 5). Raises when the logs cannot be read — the caller
    reports the context unknown, never zero."""
    now = now or int(time.time())
    hit = cache.kv_get(cache_key)
    if hit:
        d = json.loads(hit)
        if now - int(d["fetched_at"]) < CACHE_S:
            return PoolActivity(
                d["swaps"], Decimal(d["volume_usd"]), Decimal(d["fees_usd"]),
                d["from_block"], d["to_block"], d["fetched_at"],
            )  # fmt: skip
    head = rpc.get_block_number()
    if head is None:
        raise RuntimeError("head unreadable")
    start = block_at(rpc, now - WINDOW_S, head)
    logs = rpc.get_logs_chunked(address, topics, start, head, chunk_size=chunk, min_chunk=500)
    n, volume, fees = _sum_swaps(logs, price0, price1, dec0, dec1, fee_ppm, fee_word)
    out = PoolActivity(n, volume, fees, start, head, now)
    cache.kv_set(
        cache_key,
        json.dumps({**asdict(out), "volume_usd": str(volume), "fees_usd": str(fees)}),
    )
    return out


def fee_growth_activity(
    rpc: RpcClient,
    *,
    cache,
    cache_key: str,
    read_fee_growth,
    read_liquidity,
    price0: Decimal,
    price1: Decimal,
    dec0: int,
    dec1: int,
    samples: int = 8,
    now: int | None = None,
) -> PoolActivity:
    """7-day LP fee income from the pool's own fee counter — no logs at all.

    feeGrowthGlobal accumulates fees per unit of in-range liquidity (Q128), so
    fees earned over a step are Δ(feeGrowthGlobal) × active liquidity. With
    `samples` daily reads (archive eth_call, which every tier serves) and the
    step's average liquidity, that is the week's LP fees. Needs only ~2·samples
    reads, which is why it works where eth_getLogs over a week does not (the
    Alchemy free tier caps BNB logs at 10 blocks). It slightly misstates a week
    in which active liquidity swung hard between samples; stated, not hidden.

    `read_fee_growth(rpc)` → (g0, g1) and `read_liquidity(rpc)` → L are called
    with block-pinned clients. Raises when a read fails."""
    now = now or int(time.time())
    hit = cache.kv_get(cache_key)
    if hit:
        d = json.loads(hit)
        if now - int(d["fetched_at"]) < CACHE_S:
            return PoolActivity(
                d["swaps"], Decimal(d["volume_usd"]), Decimal(d["fees_usd"]),
                d["from_block"], d["to_block"], d["fetched_at"],
            )  # fmt: skip
    head = rpc.get_block_number()
    if head is None:
        raise RuntimeError("head unreadable")
    start = rpc.block_at(now - WINDOW_S)
    blocks = [start + (head - start) * i // samples for i in range(samples + 1)]
    points = []
    for b in blocks:
        pinned = rpc.pinned(b)
        g, liq = read_fee_growth(pinned), read_liquidity(pinned)
        if g is None or liq is None:
            raise RuntimeError(f"fee growth or liquidity unreadable at block {b}")
        points.append((g[0], g[1], liq))
    q128 = Decimal(2**128)
    f0 = f1 = Decimal("0")
    for (a0, a1, la), (b0, b1, lb) in zip(points, points[1:], strict=False):
        avg_l = Decimal(la + lb) / 2
        # Growth wraps modulo 2^256 like the contract's own arithmetic.
        f0 += Decimal((b0 - a0) % (1 << 256)) * avg_l / q128
        f1 += Decimal((b1 - a1) % (1 << 256)) * avg_l / q128
    fees = f0 / Decimal(10**dec0) * price0 + f1 / Decimal(10**dec1) * price1
    out = PoolActivity(-1, Decimal("0"), fees, start, head, now)  # swaps/volume unknown here
    cache.kv_set(
        cache_key,
        json.dumps({**asdict(out), "volume_usd": "0", "fees_usd": str(fees)}),
    )
    return out
