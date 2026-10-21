"""
Volume elasticity from swap history (Phase 5.3).

Two things must hold. The scan is an accountant: every swap is summed into
its pool-day once, the book's own depth at each swap is what its events say,
and a failed window leaves every earlier one banked and nothing after it.
The estimator is honest: on a synthetic history where other LPs chase flow
(so depth follows fees as much as fees follow depth), plain OLS is biased
and the book-depth instrument recovers the ε the history was built with —
and an instrument that never moves is reported as unidentified, not used.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from defi_tracker.analytics import (
    DEFAULT_REBALANCE_CONFIG,
    ElasticityEstimate,
    estimate_volume_elasticity,
    measured_rebalance_config,
)
from defi_tracker.core.types import AdapterInfo, Chain, PoolInfo, PoolSwap, ProtocolKind
from defi_tracker.poolflow import _Book, _BookPool, _BookPosition, day_sums, sync_pool_flow

POOL = "0xpool"
INFO = PoolInfo("peaq", POOL, "0xa", "0xb", "A", "USDC", 18, 18, 10_000)


def _ts(day: str, hour: int = 12) -> int:
    return int(datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC).timestamp())


def _swap(block, tick, liq, a0, a1, day="2026-01-05", hour=12, li=0):
    return PoolSwap(POOL, block, li, _ts(day, hour), tick, liq, a0, a1)


def _book(lo=-100, hi=100, changes=((50, 0, 1000),)):
    bp = _BookPool(positions={"k": _BookPosition(lo, hi)})
    bp.changes = [(b, i, "k", d) for b, i, d in changes]
    return bp


# ── the scan ──────────────────────────────────────────────────────────────


def test_fees_are_the_input_leg_times_the_fee_in_token1_at_the_swaps_price():
    # tick 0 with equal decimals: 1 A = 1 USDC. 1% fee.
    swaps = [
        _swap(10, 0, 5000, 100 * 10**18, -99 * 10**18),
        _swap(11, 0, 5000, -50 * 10**18, 51 * 10**18),
    ]
    (d,) = day_sums(swaps, INFO, _Book(_book(changes=()), (10, 0)))
    assert d["swaps"] == 2
    assert d["fees_token1"] == pytest.approx(1.0 + 0.51)
    assert d["l_sum"] == 10_000 and d["tick_open"] == 0 and d["tick_close"] == 0


def test_the_books_depth_is_counted_only_after_its_deposit_and_while_in_range():
    swaps = [
        _swap(40, 0, 9000, 10**18, -(10**18)),  # before the deposit at block 50
        _swap(60, 0, 9000, 10**18, -(10**18)),  # after it, in range
        _swap(70, 500, 9000, 10**18, -(10**18)),  # after it, out of range
    ]
    (d,) = day_sums(swaps, INFO, _Book(_book(), (40, 0)))
    assert d["book_l_sum"] == 1000  # only the middle swap saw the book
    assert d["book_l_open"] == 0  # the day's first swap came before the deposit


def test_a_day_is_split_by_utc_date():
    swaps = [
        _swap(1, 0, 1, 10**18, 0, "2026-01-05", 23),
        _swap(2, 0, 1, 10**18, 0, "2026-01-06", 1),
    ]
    days = day_sums(swaps, INFO, _Book(_book(changes=()), (1, 0)))
    assert sorted(d["day"] for d in days) == ["2026-01-05", "2026-01-06"]


class _Fake:
    """An adapter whose swap scan fails from a given block on."""

    def __init__(self, swaps, fail_from=None):
        self.swaps, self.fail_from, self.calls = swaps, fail_from, []

    info = AdapterInfo("fakex", "Fake", [Chain.PEAQ], ProtocolKind.CL_AMM, True, True)

    def position_pool(self, key):
        return (POOL, -100, 100) if key.startswith(POOL) else None

    def pool_info(self, chain, pool):
        return INFO

    def swap_history_start(self, chain):
        return 1

    def block_at(self, chain, ts):
        return 30_000

    def scan_swaps(self, chain, pools, a, b):
        self.calls.append((a, b))
        if self.fail_from is not None and b >= self.fail_from:
            raise RuntimeError("provider refused")
        return [s for s in self.swaps if a <= s.block <= b]


def _deposit(storage, block=5, delta=1000):
    with storage.connect() as conn:
        conn.execute(
            """INSERT INTO events (event_uid, wallet, chain, protocol_id, position_key, tx_hash,
                   log_index, ts, block_number, kind, usd_at_ts, amounts_json, meta_json)
               VALUES (?, '0xw', 'peaq', 'fakex', ?, '0xt', 0, 0, ?, 'deposit', 1, '[]', ?)""",
            (f"0xt:{block}", f"{POOL}:-100:100:1", block, f'{{"liquidity_delta": {delta}}}'),
        )


def test_a_rerun_never_counts_a_swap_twice(storage):
    _deposit(storage)
    swaps = [_swap(100, 0, 5000, 10**18, 0), _swap(15_000, 0, 5000, 10**18, 0)]
    fake = _Fake(swaps)
    r1 = sync_pool_flow(storage, fake, Chain.PEAQ)
    r2 = sync_pool_flow(storage, fake, Chain.PEAQ)
    assert r1.reached_head and r1.swaps == 2 and r2.windows == 0
    (row,) = storage.pool_days("peaq")
    assert row["swaps"] == 2 and row["book_l_sum"] == 2000  # same UTC day, both after the deposit


def test_a_failed_window_keeps_every_earlier_one_and_nothing_after(storage):
    _deposit(storage)
    swaps = [_swap(100, 0, 5000, 10**18, 0), _swap(25_000, 0, 5000, 10**18, 0)]
    with pytest.raises(RuntimeError):
        sync_pool_flow(storage, _Fake(swaps, fail_from=20_000), Chain.PEAQ)
    assert storage.swap_cursor("peaq", "fakex", POOL) == 10_000
    (row,) = storage.pool_days("peaq")
    assert row["swaps"] == 1
    # The rerun picks up from the cursor and finishes the job.
    sync_pool_flow(storage, _Fake(swaps), Chain.PEAQ)
    assert storage.pool_days("peaq")[0]["swaps"] == 2


def test_a_pool_whose_book_depth_cannot_be_rebuilt_is_not_scanned(storage):
    with storage.connect() as conn:
        conn.execute(
            """INSERT INTO events (event_uid, wallet, chain, protocol_id, position_key, tx_hash,
                   log_index, ts, block_number, kind, usd_at_ts, amounts_json, meta_json)
               VALUES ('0xt:1', '0xw', 'peaq', 'fakex', ?, '0xt', 0, 0, 1, 'deposit', 1, '[]', '{}')""",
            (f"{POOL}:-100:100:1",),
        )
    fake = _Fake([_swap(100, 0, 5000, 10**18, 0)])
    assert sync_pool_flow(storage, fake, Chain.PEAQ).pools == 0 and fake.calls == []


# ── the estimator ─────────────────────────────────────────────────────────


def _history(eps=0.5, n_pools=6, n_weeks=40, chase=0.8, book_moves=True, seed=1):
    """pool_days rows for a history built with fees ∝ depth^eps. Other LPs'
    depth chases each week's flow shock u (so OLS is biased up); the book's
    depth moves on its own schedule (so it identifies eps)."""
    rng = random.Random(seed)
    rows, infos = [], {}
    start = date(2026, 1, 5)  # a Monday
    for p in range(n_pools):
        pool = f"0xp{p}"
        infos[pool] = PoolInfo("peaq", pool, f"0xa{p}", "0xusd", f"T{p}", "USDC", 18, 18, 10_000)
        alpha = rng.uniform(-1, 1)
        book = rng.uniform(0.5, 2.0)
        tick = 0
        for w in range(n_weeks):
            if book_moves and rng.random() < 0.3:
                book *= math.exp(rng.gauss(0, 0.8))
            u = rng.gauss(0, 0.3)  # this week's flow shock
            ext = math.exp(rng.gauss(0, 0.3) + chase * u)
            depth = book + ext  # in units of 1e18 raw liquidity
            fees_w = math.exp(alpha + eps * math.log(depth) + u)
            for d in range(5):
                tick += rng.randint(-30, 30)
                day = start + timedelta(days=7 * w + d)
                rows.append({
                    "chain": "peaq", "protocol_id": "machinex", "pool": pool, "day": day.isoformat(),
                    "swaps": 10, "fees_token1": fees_w / 5, "l_sum": 10 * depth * 1e18,
                    "book_l_sum": 10 * book * 1e18, "book_l_open": book * 1e18,
                    "tick_open": tick, "tick_close": tick, "first_block": 1, "last_block": 2,
                })  # fmt: skip
    return rows, infos


def test_the_instrument_recovers_the_elasticity_the_history_was_built_with():
    rows, infos = _history(eps=0.5)
    est = estimate_volume_elasticity(rows, infos, n_boot=99)
    assert est.usable, est.reason
    assert est.eps_iv == pytest.approx(0.5, abs=0.15)
    assert est.ci_iv[0] < 0.5 < est.ci_iv[1]  # type: ignore[index]
    # Depth chasing flow biases plain OLS up — the reason for the instrument.
    assert est.eps_ols > est.eps_iv  # type: ignore[operator]
    assert est.first_stage_f > 10 and est.book_moves > 0  # type: ignore[operator]


def test_a_book_that_never_moves_its_depth_identifies_nothing():
    rows, infos = _history(book_moves=False)
    est = estimate_volume_elasticity(rows, infos, n_boot=19)
    assert not est.usable
    assert est.reason is not None and "first-stage F" in est.reason
    assert measured_rebalance_config(DEFAULT_REBALANCE_CONFIG, est) is DEFAULT_REBALANCE_CONFIG


def test_too_little_history_says_so():
    rows, infos = _history(n_pools=2, n_weeks=5)
    est = estimate_volume_elasticity(rows, infos, n_boot=9)
    assert not est.usable and "too few" in (est.reason or "")


def test_a_measured_elasticity_replaces_the_assumed_one_clipped_to_the_valid_range():
    est = ElasticityEstimate(0.9, (0.7, 1.1), 0.62, (0.31, 1.24), 25.0, 200, 8, 50, 30, "a", "b")
    cfg = measured_rebalance_config(DEFAULT_REBALANCE_CONFIG, est)
    assert cfg.volume_elasticity == pytest.approx(0.62)
    assert cfg.elasticity_band == (pytest.approx(0.31), 1.0)
    assert ElasticityEstimate.from_json(est.to_json()) == est


def test_a_stored_estimate_is_what_the_queue_and_the_page_score_with(storage):
    from defi_tracker.report import ELASTICITY_KEY, ReportData, effective_rebalance_config
    from defi_tracker.web import render_dashboard
    from tests.test_action_queue import _dead_but_paid, _queue
    from tests.test_web import _snap

    assert effective_rebalance_config(storage).volume_elasticity == 0.5  # assumed until measured
    est = ElasticityEstimate(0.9, (0.7, 1.1), 0.3, (0.1, 0.6), 25.0, 200, 8, 50, 30, "a", "b")
    storage.kv_set(ELASTICITY_KEY, est.to_json())
    cfg = effective_rebalance_config(storage)
    assert (cfg.volume_elasticity, cfg.elasticity_band) == (0.3, (0.1, 0.6))
    q = _queue([_dead_but_paid()], cfg)
    assert q.plans[0].elasticity == 0.3
    assert all(t.elasticity == 0.3 and t.elasticity_band == (0.1, 0.6) for t in q.tickets)
    page = render_dashboard(
        ReportData(snaps=[_snap()], actions=q, elasticity=est, rebalance_config=cfg)
    )
    assert "ε = 0.3 (measured from 200 pool-weeks of pool history" in page


def test_two_passes_scanning_at_once_never_both_bank_a_window(storage):
    """The scheduled run and a manual backfill can overlap. A window is
    banked only if the pool's cursor is where the scan found it."""
    day = {
        "pool": POOL, "day": "2026-01-05", "swaps": 1, "fees_token1": 1.0, "l_sum": 1.0,
        "book_l_sum": 0.0, "book_l_open": 0.0, "tick_open": 0, "tick_close": 0,
        "first_block": 5, "last_block": 5,
    }  # fmt: skip
    assert storage.apply_pool_window("peaq", "fakex", [day], {POOL: 10}, expected={POOL: None})
    # A second pass that also started from 'no cursor' is refused, and writes nothing.
    assert not storage.apply_pool_window("peaq", "fakex", [day], {POOL: 10}, expected={POOL: None})
    (row,) = storage.pool_days("peaq")
    assert row["swaps"] == 1 and storage.swap_cursor("peaq", "fakex", POOL) == 10


def test_a_run_banks_new_swaps_and_re_measures_before_scoring(storage, monkeypatch):
    from defi_tracker.core.runner import TrackerRunner
    from defi_tracker.report import stored_elasticity

    _deposit(storage)
    fake = _Fake([_swap(100, 0, 5000, 10**18, 0)])
    monkeypatch.setattr("defi_tracker.core.runner.all_adapters", lambda: [fake])
    TrackerRunner(storage)._refresh_elasticity()  # noqa: SLF001
    assert storage.pool_days("peaq")[0]["swaps"] == 1
    est = stored_elasticity(storage)
    assert est is not None and not est.usable  # one swap is no history — stated, not used


def test_a_failing_swap_scan_never_fails_the_run(storage, monkeypatch):
    from defi_tracker.core.runner import TrackerRunner

    _deposit(storage)
    monkeypatch.setattr("defi_tracker.core.runner.all_adapters", lambda: [_Fake([], fail_from=0)])
    TrackerRunner(storage)._refresh_elasticity()  # noqa: SLF001 — must not raise
