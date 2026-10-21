"""
Optimiser variants (Phase 5.4, item 3 of 2026-09-30's plan).

The optimiser lost its backtest choosing narrow bands on a calm trailing σ.
Each variant changes only the σ the band is searched at (or the narrowest
band allowed); the band is then scored at the queue's own σ, as every live
candidate is — so a variant is a different choice, never a rosier forecast.
Variants are configuration: spec strings that round-trip, and refuse typos.
"""

from __future__ import annotations

import math

import pytest

from defi_tracker.analytics import (
    PoolView,
    RebalanceConfig,
    band_ev,
    strategy_from_spec,
)

CFG = RebalanceConfig(volume_elasticity=0.0)


def _view(sigma=0.02, returns=(), n_obs=90) -> PoolView:
    # tick 0, equal decimals: √P = 1, token1 at $1. A pool paying $500/day
    # with 1e6 of other liquidity; $100k to place.
    return PoolView(
        capital_usd=100_000.0, tick_current=0, sqrt_p=1.0, scale=1.0, p1=1.0,
        fees_per_day=500.0, l_ext=1e6, l_own=0.0, l_active=1e6, sigma=sigma, horizon=30.0,
        spacing=10, returns=tuple(returns), n_obs=n_obs,
    )  # fmt: skip


def _half_pct(ticks: tuple[int, int]) -> float:
    return (math.pow(1.0001, (ticks[1] - ticks[0]) / 2) - 1) * 100


def test_knobs_round_trip_and_a_misspelt_one_is_refused():
    for spec in (
        "optimiser",
        "optimiser:floor=5",
        "optimiser:window=180,ewma=0.94,upper,floor=8,minw=20",
    ):
        assert strategy_from_spec(spec).spec == spec
    for bad in (
        "optimiser:flor=5", "optimiser:floor=-1", "optimiser:floor=x", "optimiser:ewma=1",
        "optimiser:window=1", "optimiser:upper=1",
        "optimiser:minw=0",
    ):  # fmt: skip
        with pytest.raises(ValueError):
            strategy_from_spec(bad)


def test_a_sigma_floor_widens_the_band_on_a_calm_pool_and_is_inert_above_it():
    base = strategy_from_spec("optimiser").band(_view(0.02), CFG)
    floored = strategy_from_spec("optimiser:floor=6").band(_view(0.02), CFG)
    assert base and floored and _half_pct(floored.ticks) > 1.5 * _half_pct(base.ticks)
    # σ already above the floor: the same band.
    hot = _view(0.08)
    assert strategy_from_spec("optimiser:floor=6").band(hot, CFG).ticks == (
        strategy_from_spec("optimiser").band(hot, CFG).ticks
    )


def test_a_variants_band_is_scored_at_the_queues_sigma_not_its_own():
    v = _view(0.02)
    got = strategy_from_spec("optimiser:floor=6").band(v, CFG)
    assert got is not None
    lo, hi = got.ticks
    want = band_ev(
        got.liquidity, 1.0, 1.0, lo * math.log(1.0001), hi * math.log(1.0001), 500.0, 1e6, 0.0,
        0.02, 30.0, elasticity=0.0, depth_ref=1e6,
    )  # fmt: skip
    assert got.ev.ev_usd == pytest.approx(want.ev_usd)
    # …so it can never look better than the band the optimiser found at that σ.
    best = strategy_from_spec("optimiser").band(v, CFG)
    assert best is not None and got.ev.ev_usd <= best.ev.ev_usd + 1e-9


def test_a_window_reads_only_its_last_days_and_falls_back_with_too_few():
    calm, wild = [0.01, -0.01] * 50, [0.05, -0.05] * 50
    s = strategy_from_spec("optimiser:window=60")
    assert s.search_sigma(_view(0.02, calm + wild)) == pytest.approx(0.05)  # type: ignore[attr-defined]
    assert s.search_sigma(_view(0.02, wild + calm)) == pytest.approx(0.01)  # type: ignore[attr-defined]
    assert s.search_sigma(_view(0.02, wild[:20])) == 0.02  # type: ignore[attr-defined]


def test_an_ewma_weighs_the_recent_days_most():
    calm, wild = [0.01, -0.01] * 50, [0.05, -0.05] * 10
    s = strategy_from_spec("optimiser:ewma=0.94")
    after_storm = s.search_sigma(_view(0.02, calm + wild))  # type: ignore[attr-defined]
    after_calm = s.search_sigma(_view(0.02, wild + calm))  # type: ignore[attr-defined]
    assert after_storm > 0.04 and after_calm < 0.015


def test_upper_searches_at_the_top_of_sigmas_95pct_band_from_its_own_sample():
    from defi_tracker.analytics import sigma_band

    s = strategy_from_spec("optimiser:upper")
    assert s.search_sigma(_view(0.03, n_obs=90)) == pytest.approx(sigma_band(0.03, 90)[1])  # type: ignore[attr-defined]
    # Thinner history, wider band, higher search σ.
    assert s.search_sigma(_view(0.03, n_obs=30)) > s.search_sigma(_view(0.03, n_obs=90))  # type: ignore[attr-defined]


def test_a_minimum_width_widens_a_narrow_answer_and_leaves_a_wide_one():
    v = _view(0.01)
    narrow = strategy_from_spec("optimiser").band(v, CFG)
    assert narrow is not None and _half_pct(narrow.ticks) < 20
    wide = strategy_from_spec("optimiser:minw=20").band(v, CFG)
    assert wide is not None and 20 <= _half_pct(wide.ticks) < 21
    lo, hi = wide.ticks
    assert lo % 10 == 0 and hi % 10 == 0 and -lo == hi  # snapped outward, about spot
    hot = _view(0.10)  # already wider than ±20%: untouched
    assert strategy_from_spec("optimiser:minw=20").band(hot, CFG).ticks == (
        strategy_from_spec("optimiser").band(hot, CFG).ticks
    )


def test_every_grid_variant_parses_and_the_plain_optimiser_is_among_them():
    from defi_tracker.analytics import DEFAULT_REBALANCE_CONFIG, variant_grid

    grid = variant_grid()
    assert "optimiser" in grid and len(set(grid)) == len(grid)
    assert all(strategy_from_spec(s).spec == s for s in grid)
    assert DEFAULT_REBALANCE_CONFIG.strategy in grid  # the default came from the grid


def test_the_adoption_bar_is_a_gain_over_hold_and_a_hit_rate_of_at_least_half():
    from defi_tracker.analytics import Backtest, BacktestWindow, adoptable

    def bt(*pairs):  # (predicted, realised) per window
        ws = [
            BacktestWindow("p", "A/B", "2026-04-01", "2026-05-01", 1.0, [], (0, 1), pr, 0.0, re)
            for pr, re in pairs
        ]
        return Backtest(ws, [], 30, 1.0)

    assert adoptable(bt((1, 5), (1, -1)))  # +4, right 1 of 2
    assert not adoptable(bt((1, 5), (1, -1), (1, -1)))  # +3 but right 1 of 3
    assert not adoptable(bt((1, -5), (1, 1)))  # right half the time, but a loss
    assert not adoptable(bt())
