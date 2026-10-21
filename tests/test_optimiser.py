"""
Range optimiser (Phase 5.1), checked against closed forms, not itself.

- occupancy: the closed form for ∫₀ᵀ P(a ≤ u_t ≤ b) dt agrees with a direct
  numerical integral of the normal CDF, and has the right limits.
- IL: L·√P·σ²/4 per day in band; for a symmetric band of capital V that is
  V·σ²/(8·(1 − e^{−h/2})), h the band's half-width in log price.
- sizing: L* = √(F·L_ext/c) − L_other is the argmax of the book's EV.
- share: narrowing raises the share and it saturates — a small LP with fees
  above IL goes narrow; a book that is the whole pool goes wide.
"""

from __future__ import annotations

import json
import math

import pytest

from defi_tracker.analytics import (
    DEFAULT_REBALANCE_CONFIG,
    RebalanceConfig,
    band_ev,
    best_band,
    best_liquidity,
    infer_tick_spacing,
    liquidity_per_usd,
    occupancy_days,
    optimal_liquidity,
    optimise_pool,
    pool_states,
    risk_model,
    sigma_band,
    snap_band,
)

LN = math.log(1.0001)
DEC = {"peaq:0xa": 18, "peaq:0xb": 18}


def _numeric_occupancy(d_lo: float, d_hi: float, sigma: float, horizon: float) -> float:
    """∫₀ᵀ [Φ(d_hi/σ√t) − Φ(d_lo/σ√t)] dt by the midpoint rule."""
    n = 200_000
    dt = horizon / n
    tot = 0.0
    for i in range(n):
        s = sigma * math.sqrt((i + 0.5) * dt)
        tot += 0.5 * (math.erfc(-d_hi / s / math.sqrt(2)) - math.erfc(-d_lo / s / math.sqrt(2)))
    return tot * dt


@pytest.mark.parametrize(
    "d_lo,d_hi",
    [(-0.2, 0.2), (-0.05, 0.3), (0.1, 0.4), (-0.5, -0.15), (-0.01, 0.01)],
)
def test_occupancy_matches_the_integral_of_the_normal_cdf(d_lo, d_hi):
    sigma, horizon = 0.06, 30.0
    assert occupancy_days(d_lo, d_hi, sigma, horizon) == pytest.approx(
        _numeric_occupancy(d_lo, d_hi, sigma, horizon), rel=1e-4, abs=1e-6
    )


def test_occupancy_limits():
    assert occupancy_days(-0.2, 0.2, 0.0, 30) == 30  # no volatility: never leaves
    assert occupancy_days(0.1, 0.2, 0.0, 30) == 0  # …and never arrives
    assert occupancy_days(-50, 50, 0.06, 30) == pytest.approx(30, rel=1e-9)
    assert occupancy_days(0.1, 0.1, 0.06, 30) == 0
    # A walk spends half its time above today's price.
    assert occupancy_days(0, 50, 0.06, 30) == pytest.approx(15, rel=1e-9)


def test_liquidity_per_usd_inverts_the_positions_own_value():
    lo, cur, hi, L = -2000, 300, 2500, 1234.0
    s_lo, s_p, s_hi = (math.exp(t * LN / 2) for t in (lo, cur, hi))
    value = L * (1 / s_p - 1 / s_hi) * s_p**2 + L * (s_p - s_lo)  # token1 = $1
    assert value * liquidity_per_usd(s_p, s_lo, s_hi, 1.0) == pytest.approx(L, rel=1e-12)


def test_expected_il_is_the_closed_form_rate_times_occupancy():
    L, sp, p1, sigma, T = 500.0, 1.3, 2.0, 0.05, 30.0
    ev = band_ev(L, sp, p1, -0.3, 0.3, 100.0, 0.0, 0.0, sigma, T)
    assert ev.il_usd == pytest.approx(ev.occupancy_days * L * sp * p1 * sigma**2 / 4)


def test_symmetric_band_il_rate_in_terms_of_capital():
    """IL/day = V·σ²/(8(1 − e^{−h/2})) for capital V in a band of half-width h."""
    V, sigma, h, sp, p1 = 10_000.0, 0.05, 0.4, 1.0, 1.0
    L = V * liquidity_per_usd(sp, sp * math.exp(-h / 2), sp * math.exp(h / 2), p1)
    ev = band_ev(L, sp, p1, -h, h, 0.0, 0.0, 0.0, sigma, 1.0)
    rate = ev.il_usd / ev.occupancy_days
    assert rate == pytest.approx(V * sigma**2 / (8 * (1 - math.exp(-h / 2))), rel=1e-12)


def test_book_view_nets_out_fees_taken_from_the_books_own_positions():
    ev = band_ev(100.0, 1.0, 1.0, -1, 1, 50.0, 300.0, 100.0, 0.0, 10.0)
    assert ev.share == pytest.approx(100 / 500)
    assert ev.fees_usd == pytest.approx(50 * 10 * 0.2)
    assert ev.own_fees_usd == pytest.approx(ev.fees_usd * 100 / 400)
    assert ev.book_fees_usd == pytest.approx(ev.fees_usd * 300 / 400)


def test_optimal_liquidity_is_the_argmax_of_book_ev():
    F, l_ext, l_own, sp, p1, sigma = 80.0, 5_000.0, 2_000.0, 1.1, 1.0, 0.04
    l_star = optimal_liquidity(F, l_ext, l_ext + l_own, sp, p1, sigma)
    assert l_star is not None and l_star > 0

    def ev(L):
        return band_ev(L, sp, p1, -0.3, 0.3, F, l_ext, l_own, sigma, 30).ev_usd

    grid = [l_star * (0.5 + i / 1000) for i in range(1001)]
    assert max(grid, key=ev) == pytest.approx(l_star, rel=2e-3)
    # Closed form, written out.
    c = sp * p1 * sigma**2 / 4
    assert l_star == pytest.approx(math.sqrt(F * l_ext / c) - (l_ext + l_own))


def test_optimal_liquidity_floors_at_zero_and_refuses_the_sole_lp_case():
    # Fees far below IL: hold nothing.
    assert optimal_liquidity(0.001, 10.0, 10.0, 1.0, 1.0, 0.1) == 0.0
    # Nobody else in the pool: with volume fixed any L takes every fee.
    assert optimal_liquidity(100.0, 0.0, 0.0, 1.0, 1.0, 0.05) is None


def _search(capital, l_ext, l_own, F, sigma=0.05, **kw):
    sp = 1.0
    return best_band(
        capital_usd=capital, tick_current=0, sqrt_p=sp, scale=1.0, p1=1.0, fees_per_day=F,
        l_ext=l_ext, l_own=l_own, l_active_before=l_ext + l_own + kw.pop("l_self", 0.0),
        sigma=sigma, horizon=30.0, spacing=10, depth_bounds=kw.pop("bounds", (0.0, 1e18)),
        z_grid=DEFAULT_REBALANCE_CONFIG.z_grid, **kw,
    )  # fmt: skip


def test_a_small_lp_whose_fees_beat_il_goes_narrow():
    # Tiny capital against deep outside liquidity: share ∝ L, so fees and IL
    # both scale with 1/width and only occupancy separates them — narrowest.
    # F/L_ext = 1e-3 per unit L beats the IL rate √P·p1·σ²/4 = 6.25e-4.
    choice = _search(capital=100.0, l_ext=1e6, l_own=0.0, F=1_000.0)
    assert choice is not None and choice.at_edge
    assert choice.ticks[1] - choice.ticks[0] == min(
        b - a
        for a, b in (
            snap_band(0, z * 0.05 / LN * math.sqrt(30), 10) for z in DEFAULT_REBALANCE_CONFIG.z_grid
        )
    )


def test_a_book_that_is_the_pool_goes_wide():
    # All fees are the book's at any width; narrowing only adds IL.
    choice = _search(capital=100_000.0, l_ext=1e-9, l_own=0.0, F=50.0, l_self=1.0)
    assert choice is not None
    widths = [
        snap_band(0, z * 0.05 / LN * math.sqrt(30), 10) for z in DEFAULT_REBALANCE_CONFIG.z_grid
    ]
    assert choice.ticks[1] - choice.ticks[0] == max(b - a for a, b in widths)


def test_saturating_share_gives_an_interior_optimum():
    """Capital comparable to outside liquidity: narrowing raises the share
    until it saturates, so the best band is neither extreme."""
    base = _search(capital=1.0, l_ext=1.0, l_own=0.0, F=1.0)
    assert base is not None
    l_ext = base.liquidity * 3  # make outside liquidity comparable to ours
    choice = _search(capital=10_000.0, l_ext=l_ext * 10_000, l_own=0.0, F=400.0)
    assert choice is not None and not choice.at_edge


def test_the_depth_bound_keeps_the_answer_local():
    free = _search(capital=100_000.0, l_ext=1e-9, l_own=0.0, F=50.0, l_self=1.0)
    assert free is not None
    bounded = _search(
        capital=100_000.0,
        l_ext=1e-9,
        l_own=0.0,
        F=50.0,
        l_self=free.liquidity * 4,
        bounds=(0.5, 1.5),
    )
    assert bounded is not None and 0.5 <= 1 + bounded.depth_change <= 1.5
    assert bounded.ticks[1] - bounded.ticks[0] < free.ticks[1] - free.ticks[0]


def test_snap_band_rounds_outward_to_the_spacing():
    assert snap_band(1234, 250.0, 100) == (900, 1500)
    assert snap_band(0, 1.0, 60) == (-60, 60)  # at least one spacing either side
    assert infer_tick_spacing([-324700, -305900, -323300]) == 100
    assert infer_tick_spacing([29957, 46051, 48283]) == 1


def test_sigma_band_matches_the_breakeven_apr_band():
    lo, hi = sigma_band(0.05, 81)
    rel = 1.96 / math.sqrt(160)
    assert (lo, hi) == pytest.approx((0.05 * (1 - rel), 0.05 * (1 + rel)))


# ── pool assembly and the plan ─────────────────────────────────────────────


def _row(lo, cur, hi, L, share=None, fees7=700.0, meta=None, pair="A/B 1.00%"):
    s_lo, s_hi = math.exp(lo * LN / 2), math.exp(hi * LN / 2)
    s_p = math.exp(min(max(cur, lo), hi) * LN / 2)
    x = L * (1 / s_p - 1 / s_hi)
    y = L * (s_p - s_lo)
    price = math.exp(cur * LN)
    return {
        "wallet": "0xw", "chain": "peaq", "protocol_id": "machinex", "pair_label": pair,
        "position_key": f"0xpool:{lo}:{hi}", "protocol_kind": "cl_amm", "is_closed": 0,
        "current_value_usd": x * price + y, "tick_lower": lo, "tick_current": cur,
        "tick_upper": hi, "seven_day_fees_usd": fees7, "position_share_pct": share,
        "meta_json": json.dumps(meta or {}),
        "current_balances_json": json.dumps([
            {"token_key": "peaq:0xa", "symbol": "A", "amount": str(x)},
            {"token_key": "peaq:0xb", "symbol": "B", "amount": str(y)},
        ]),
    }  # fmt: skip


VOL = {("peaq", "A/B 1.00%"): (500.0, 90)}


def test_pool_state_reads_active_liquidity_from_an_in_range_share():
    rows = [_row(-2000, 0, 2000, 1000.0, share=25.0), _row(3000, 0, 5000, 500.0, share=0.0)]
    (ps,) = pool_states(rows, DEC, VOL)
    m0 = risk_model(rows[0], DEC)
    assert m0 is not None
    assert ps.l_active == pytest.approx(m0.liquidity / 0.25)
    assert ps.l_ext == pytest.approx(ps.l_active * 0.75)
    assert ps.book_share == pytest.approx(0.25)
    assert ps.fees_per_day == pytest.approx(100.0)
    assert ps.spacing == 1000


def test_pool_state_prefers_the_pools_own_liquidity_read():
    # Raw 4e21 at 18/18 decimals is 4000 human units.
    rows = [_row(3000, 0, 5000, 500.0, share=0.0, meta={"pool_liquidity": str(4 * 10**21)})]
    (ps,) = pool_states(rows, DEC, VOL)
    assert ps.l_active == pytest.approx(4000.0)
    assert ps.l_ext == pytest.approx(4000.0)  # the book is out of range


@pytest.mark.parametrize(
    "rows,vol,reason",
    [
        ([_row(-2000, 0, 2000, 1000.0, share=25.0, fees7=None)], VOL, "pool fees unknown"),
        ([_row(-2000, 0, 2000, 1000.0, share=25.0)], {}, "no σ yet"),
        (
            [_row(-2000, 0, 2000, 1000.0, share=25.0)],
            {("peaq", "A/B 1.00%"): (500.0, 12)},
            "not enough history — σ from 12 daily returns",
        ),
        ([_row(3000, 0, 5000, 500.0, share=0.0)], VOL, "pool active liquidity unknown"),
    ],
)
def test_a_pool_without_its_inputs_says_why_rather_than_guessing(rows, vol, reason):
    (ps,) = pool_states(rows, DEC, vol)
    plan = optimise_pool(ps, DEFAULT_REBALANCE_CONFIG)
    assert plan.reason is not None and reason in plan.reason
    assert plan.best is None and plan.ev_band is None


def test_the_plan_carries_sigmas_band_through_to_width_and_ev():
    rows = [_row(-2000, 0, 2000, 1000.0, share=25.0)]
    (ps,) = pool_states(rows, DEC, VOL)
    plan = optimise_pool(ps, RebalanceConfig(depth_bounds=(0.0, 1e18)))
    assert plan.reason is None and plan.best is not None and plan.ev_band is not None
    assert plan.best_lo_sigma is not None and plan.best_hi_sigma is not None
    lo_ev, hi_ev = plan.ev_band
    assert lo_ev <= plan.best.ev.ev_usd <= hi_ev
    assert plan.capital_usd == pytest.approx(rows[0]["current_value_usd"])
    assert plan.today_ev is not None
    # The optimum for the book's capital beats (or equals) leaving it as is.
    assert plan.best.ev.ev_usd >= plan.today_ev - 1e-6


# ── volume's response to depth: fees ∝ depth^ε ───────────────────────────


def test_at_elasticity_zero_book_fees_are_fees_times_the_outside_share():
    """ε = 0 is the fixed-volume model the closed forms above were built on."""
    ev = band_ev(100.0, 1.0, 1.0, -1, 1, 50.0, 300.0, 100.0, 0.0, 10.0, elasticity=0.0,
                 depth_ref=500.0)  # fmt: skip
    assert ev.book_fees_usd == pytest.approx(ev.fees_usd * 300 / 400)


def test_at_elasticity_one_each_unit_of_liquidity_brings_its_own_flow():
    """ε = 1: pool fees ∝ depth, so the book's fees from L are F·L/D₀ however
    the rest of the pool is split between the book and other LPs."""
    F, D0, occ = 50.0, 500.0, 10.0
    for l_ext, l_own in ((300.0, 100.0), (0.0, 400.0), (400.0, 0.0)):
        ev = band_ev(100.0, 1.0, 1.0, -9, 9, F, l_ext, l_own, 0.0, occ, elasticity=1.0,
                     depth_ref=D0)  # fmt: skip
        assert ev.book_fees_usd == pytest.approx(F * 100.0 / D0 * occ)


def test_the_numeric_sizing_matches_the_fixed_volume_closed_form():
    F, l_ext, l_own, sp, p1, sigma = 80.0, 5_000.0, 2_000.0, 1.1, 1.0, 0.04
    closed = optimal_liquidity(F, l_ext, l_ext + l_own, sp, p1, sigma)
    numeric = best_liquidity(
        lambda L: band_ev(L, sp, p1, -0.3, 0.3, F, l_ext, l_own, sigma, 30).ev_usd, 0.0, 1e6
    )
    assert numeric == pytest.approx(closed, rel=1e-4)


def test_a_sole_lp_at_elasticity_half_has_a_closed_form_size():
    """Sole LP, ε = ½: EV ∝ F·√(L/D₀) − c·L, so L* = F² / (4·c²·D₀),
    c = √P·p1·σ²/4. Fixed volume could not size this at all."""
    F, D0, sp, p1, sigma = 120.0, 2_000.0, 1.2, 1.0, 0.05
    c = sp * p1 * sigma**2 / 4
    numeric = best_liquidity(
        lambda L: (
            band_ev(L, sp, p1, -9, 9, F, 0.0, 0.0, sigma, 30, elasticity=0.5, depth_ref=D0).ev_usd
        ),  # fmt: skip
        0.0,
        1e9,
    )
    assert numeric == pytest.approx(F**2 / (4 * c**2 * D0), rel=1e-4)


def test_elasticity_pulls_a_book_that_is_the_pool_back_from_the_widest_band():
    """At ε = 0 the book that is the pool always goes widest; once flow
    follows depth, thinning the pool costs fees and the band narrows."""
    fixed = _search(capital=100_000.0, l_ext=1e-9, l_own=0.0, F=5_000.0, l_self=1.0)
    elastic = _search(capital=100_000.0, l_ext=1e-9, l_own=0.0, F=5_000.0, l_self=1.0,
                      elasticity=1.0)  # fmt: skip
    assert fixed is not None and elastic is not None
    assert elastic.ticks[1] - elastic.ticks[0] < fixed.ticks[1] - fixed.ticks[0]


def test_a_band_at_the_tick_bounds_reads_full_range_not_an_unreadable_percentage():
    from defi_tracker.analytics import band_pct_label

    assert band_pct_label(-100.0, 5.3e35, (-887220, 887220)) == "full range"
    assert band_pct_label(-11.2, 12.7, (-1200, 1200)) == "-11% / +13%"
    assert band_pct_label(-99.0, 2.5e6, (-50000, 150000)) == "-99% / >+99,999%"
