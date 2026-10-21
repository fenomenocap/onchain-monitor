"""7-day pool fees from Swap logs — independent of any subgraph."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from defi_tracker.adapters._pool_activity import CACHE_S, block_at, pool_activity


def _swap(a0: int, a1: int, fee: int | None = None) -> dict:
    words = [a0 % (1 << 256), a1 % (1 << 256), 0, 0, 0] + ([fee, 0] if fee is not None else [])
    return {"data": "0x" + "".join(w.to_bytes(32, "big").hex() for w in words)}


class Cache(dict):
    def kv_get(self, k):
        return self.get(k)

    def kv_set(self, k, v):
        self[k] = v


def _rpc(logs):
    rpc = MagicMock()
    rpc.get_block_number.return_value = 1_000_000
    rpc.get_block_timestamp.side_effect = lambda b: 1_000_000_000 + b  # 1s blocks
    rpc.get_logs_chunked.return_value = logs
    return rpc


def test_fees_are_the_larger_leg_times_the_pool_fee_whatever_the_sign_convention():
    # 1000 USDT in for 25,000 PEAQ out ($0.04) — V3 signs (pool delta) and the
    # swapper's signs must give the same answer.
    for a0, a1 in ((1000 * 10**6, -25_000 * 10**18), (-1000 * 10**6, 25_000 * 10**18)):
        act = pool_activity(
            _rpc([_swap(a0, a1)]), cache=Cache(), cache_key="k", address="0xp", topics=[],
            price0=Decimal(1), price1=Decimal("0.0399"), dec0=6, dec1=18, fee_ppm=10_000,
            now=1_001_000_000,
        )  # fmt: skip
        assert act.volume_usd == Decimal(1000)
        assert act.fees_usd == Decimal(10)


def test_an_event_that_carries_its_own_fee_uses_it():
    act = pool_activity(
        _rpc([_swap(10**18, -(10**18), fee=2500)]), cache=Cache(), cache_key="k",
        address="0xm", topics=[], price0=Decimal(2), price1=Decimal(2), dec0=18, dec1=18,
        fee_word=5, now=1_001_000_000,
    )  # fmt: skip
    assert act.fees_usd == Decimal("0.005")


def test_results_are_cached_so_hourly_marks_do_not_rescan():
    cache = Cache()
    rpc = _rpc([_swap(10**6, -(10**6))])
    kw = {"cache": cache, "cache_key": "k", "address": "0xp", "topics": [],
          "price0": Decimal(1), "price1": Decimal(1), "dec0": 6, "dec1": 6, "fee_ppm": 100}  # fmt: skip
    pool_activity(rpc, now=1_001_000_000, **kw)
    pool_activity(rpc, now=1_001_000_000 + CACHE_S - 1, **kw)
    assert rpc.get_logs_chunked.call_count == 1
    pool_activity(rpc, now=1_001_000_000 + CACHE_S + 1, **kw)
    assert rpc.get_logs_chunked.call_count == 2
    assert json.loads(cache["k"])["fees_usd"] == "0.0001"


def test_the_window_starts_a_week_back_never_later():
    rpc = _rpc([])
    start = block_at(rpc, 1_000_000_000 + 400_000, head=1_000_000)
    assert 1_000_000_000 + start <= 1_000_000_000 + 400_000


def test_unreadable_logs_raise_so_the_caller_says_unknown():
    rpc = _rpc([])
    rpc.get_logs_chunked.side_effect = RuntimeError("range cap")
    with pytest.raises(RuntimeError):
        pool_activity(rpc, cache=Cache(), cache_key="k", address="0xp", topics=[],
                      price0=Decimal(1), price1=Decimal(1), dec0=6, dec1=6, fee_ppm=1,
                      now=1_001_000_000)  # fmt: skip


def test_fee_growth_gives_week_fees_without_any_logs():
    """Δ feeGrowthGlobal (Q128, per unit liquidity) × active liquidity = fees."""
    from defi_tracker.adapters._pool_activity import fee_growth_activity

    rpc = _rpc([])
    rpc.block_at.return_value = 0
    rpc.pinned.side_effect = lambda b: b  # the reader receives the block number
    q = 2**128
    # Growth of 1e-6 token0 per unit of liquidity per sample step, L = 1e24.
    g = lambda b: (b // 125_000 * q // 10**6, 0)  # noqa: E731 — 8 steps over 1,000,000 blocks
    act = fee_growth_activity(
        rpc, cache=Cache(), cache_key="k", read_fee_growth=g, read_liquidity=lambda b: 10**24,
        price0=Decimal(2), price1=Decimal(1), dec0=18, dec1=18, now=1_001_000_000,
    )  # fmt: skip
    # 8 steps × 1e-6 × 1e24 raw = 8e18 raw = 8 tokens × $2
    assert act.fees_usd == Decimal(16)
    rpc.get_logs_chunked.assert_not_called()
