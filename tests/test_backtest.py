"""
Backtest harness (Phase 5.3).

The replay is an accountant before it is a judge: a policy that keeps the
book gains exactly nothing, one that holds the book's own band realises
what the book did, a position in range takes its share of each day's fees,
and a band the price leaves is charged its IL against holding.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from defi_tracker.analytics import BandChoice, HoldStrategy, Strategy, band_ev
from defi_tracker.core.types import PoolInfo

POOL = "0xpool"


# ── the backtest ──────────────────────────────────────────────────────────


def _flat_history(n_days=80, tick_path=None, fees=100.0, l_pool=5e6, l_book=1e6):
    """One pool, token1 a stablecoin, equal decimals: price = 1.0001^tick.
    The book deposited 1e6 (human) liquidity in [-2000, 2000] on day 0."""
    info = PoolInfo("peaq", POOL, "0xa", "0xusd", "A", "USDC", 18, 18, 10_000)
    rng = random.Random(3)
    rows, tick = [], 0
    for k in range(n_days):
        tick = tick_path[k] if tick_path else tick + rng.randint(-40, 40)
        day = (date(2026, 1, 1) + timedelta(days=k)).isoformat()
        rows.append({
            "pool": POOL, "day": day, "swaps": 10, "fees_token1": fees,
            "l_sum": 10 * l_pool * 1e18, "book_l_sum": 10 * l_book * 1e18, "book_l_open": 0.0,
            "tick_open": tick, "tick_close": tick, "first_block": 1, "last_block": 2,
        })  # fmt: skip
    t0 = int(datetime(2025, 12, 31, tzinfo=UTC).timestamp())
    book = {POOL: [(t0, "k", -2000, 2000, int(l_book * 1e18))]}
    return {POOL: rows}, {POOL: info}, book


def _bt(strategy=None, **kw):
    from defi_tracker.analytics import RebalanceConfig, backtest_optimiser

    rows, infos, book = _flat_history(**kw)
    cfg = RebalanceConfig(min_sigma_obs=20, volume_elasticity=0.0)
    return backtest_optimiser(rows, infos, book, cfg, strategy=strategy, min_capital_usd=1.0)


class _SameBand(Strategy):
    """Re-places the book's own band with its own liquidity."""

    spec = "same"

    def band(self, v, config):
        liq, ticks = v.current[0]
        return BandChoice(
            ticks, liq, band_ev(liq, v.sqrt_p, v.p1, 0, 0, 0, 0, 0, v.sigma, 1), False, 0
        )


def test_a_policy_that_keeps_the_book_gains_exactly_nothing():
    bt = _bt(HoldStrategy())
    assert bt.windows
    for w in bt.windows:
        assert w.band is None and w.predicted_gain_usd == 0.0 and w.realised_gain_usd == 0.0


def test_a_policy_holding_the_same_band_realises_what_the_book_did():
    bt = _bt(_SameBand())
    for w in bt.windows:
        assert w.realised_gain_usd == pytest.approx(0.0, abs=1e-6)


def test_the_book_in_range_takes_its_share_of_each_days_fees_and_no_il_on_a_flat_price():
    bt = _bt(HoldStrategy(), tick_path=[0] * 80)
    w = bt.windows[0]
    days = 31  # the start day and 30 more
    assert w.stay_fees_usd == pytest.approx(days * 100.0 * 1e6 / 5e6)
    assert w.stay_usd == pytest.approx(w.stay_fees_usd)  # flat price: no IL


def test_a_band_the_price_leaves_realises_il_against_holding():
    path = [0] * 40 + [3000] * 40  # the price jumps out of [-2000, 2000] and stays
    bt = _bt(HoldStrategy(), tick_path=path)
    w = next(w for w in bt.windows if w.start < "2026-02-10" <= w.end)
    assert w.stay_usd < w.stay_fees_usd  # IL was charged
    assert bt.hit_rate is None  # no window predicted a gain


def test_the_optimiser_policy_is_scored_against_its_own_prediction():
    bt = _bt()
    assert bt.windows and all(w.band is not None for w in bt.windows)
    assert bt.predicted_usd == pytest.approx(sum(w.predicted_gain_usd for w in bt.windows))
    assert bt.realised_usd == pytest.approx(sum(w.policy_usd - w.stay_usd for w in bt.windows))


# ── strategies ─────────────────────────────────────────────────────────────


def test_strategies_are_configuration_and_a_misspelt_one_is_refused():
    from defi_tracker.analytics import strategy_from_spec

    assert [strategy_from_spec(x).spec for x in ("optimiser", "hold", "sigma:2.5", "fixed:20")] == [
        "optimiser", "hold", "sigma:2.5", "fixed:20",
    ]  # fmt: skip
    for bad in ("optimizer", "sigma", "sigma:-1", "fixed:abc", "hold:1"):
        with pytest.raises(ValueError):
            strategy_from_spec(bad)


def test_a_fixed_width_strategy_places_its_band_at_that_width_about_spot():
    from defi_tracker.analytics import strategy_from_spec

    bt = _bt(strategy_from_spec("fixed:20"))
    half = math.log(1.2) / math.log(1.0001)
    spacing = 2000  # the book's own ticks set the grid bands snap outward to
    assert bt.windows and bt.strategy == "fixed:20"
    for w in bt.windows:
        lo, hi = w.band  # type: ignore[misc]
        assert lo % spacing == 0 and hi % spacing == 0
        assert hi - lo >= 2 * half and hi - lo <= 2 * half + 2 * spacing


def test_the_live_queue_asks_the_configured_strategy_for_its_band():
    from decimal import Decimal

    from defi_tracker.analytics import RebalanceConfig
    from tests.test_action_queue import _dead_but_paid, _queue

    hold = _queue([_dead_but_paid()], RebalanceConfig(idle_min_usd=Decimal("0"), strategy="hold"))
    assert all(t.action != "rerange" for t in hold.tickets)
    assert all(t.new_ticks in (None, (t.ticks_now[0], t.ticks_now[2])) for t in hold.tickets)
    fixed = _queue(
        [_dead_but_paid()], RebalanceConfig(idle_min_usd=Decimal("0"), strategy="fixed:10")
    )
    (t,) = fixed.tickets
    lo, hi = t.new_ticks  # type: ignore[misc]
    cur = t.ticks_now[1]
    assert pow(1.0001, hi - cur) == pytest.approx(1.10, rel=0.02)
    assert pow(1.0001, cur - lo) == pytest.approx(1.10, rel=0.02)


def test_a_fee_counter_history_replays_and_its_chain_counts_as_backtested():
    """bsc's pool history comes from two day-end reads (no swap count). It
    must replay like a swap history, tag its windows with its chain, and
    stop the queue verdict from calling that chain untested."""
    from defi_tracker.analytics import RebalanceConfig, backtest_optimiser, backtest_verdict

    rows, infos, book = _flat_history()
    info = infos[POOL]
    bsc = PoolInfo("bsc", POOL, info.token0, info.token1, "A", "USDC", 18, 18, 335)
    sampled = [
        {**r, "swaps": None, "samples": 2, "source": "fee_counter",
         "l_sum": r["l_sum"] / 5, "book_l_sum": r["book_l_sum"] / 5}
        for r in rows[POOL]
    ]  # fmt: skip
    cfg = RebalanceConfig(min_sigma_obs=20, volume_elasticity=0.0)
    swaps = backtest_optimiser(rows, infos, book, cfg, min_capital_usd=1.0)
    counter = backtest_optimiser({POOL: sampled}, {POOL: bsc}, book, cfg, min_capital_usd=1.0)
    assert len(counter.windows) == len(swaps.windows) > 0
    assert {w.chain for w in counter.windows} == {"bsc"} and counter.chains == ("bsc",)
    # Same means (depth ÷ samples), same history: the same record.
    assert counter.realised_usd == pytest.approx(swaps.realised_usd)
    assert "not backtested" not in backtest_verdict(counter, {"bsc"})
    assert "peaq not backtested" in backtest_verdict(counter, {"bsc", "peaq"})


def test_a_window_not_expected_to_pay_is_not_acted_on_and_realises_nothing():
    """As in the live queue: a re-range is proposed only when it is expected
    to gain more than min_ev_usd — otherwise the book stays."""
    from defi_tracker.analytics import strategy_from_spec

    bt = _bt(strategy_from_spec("fixed:500"))  # far too wide to pay on a calm pool
    idle = [w for w in bt.windows if not w.acted]
    assert idle and all(w.predicted_gain_usd <= 0 for w in idle)
    assert all(w.realised_gain_usd == 0 for w in idle)
    assert all(w.acted == (w.predicted_gain_usd > 0) for w in bt.windows)


def test_the_split_keeps_windows_on_one_side_and_drops_the_ones_straddling_it():
    bt = _bt()
    ins, outs = bt.split("2026-02-15")
    assert all(w.end < "2026-02-15" for w in ins.windows)
    assert all(w.start >= "2026-02-15" for w in outs.windows)
    straddle = [w for w in bt.windows if w.start < "2026-02-15" <= w.end]
    assert len(ins.windows) + len(outs.windows) + len(straddle) == len(bt.windows)
    assert ins.strategy == outs.strategy == bt.strategy
