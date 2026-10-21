"""
Scenario grid, greeks and range probabilities (Phase 1.3–1.5).

Checked against closed forms, not against themselves: a CL position's
expected IL rate is L·√P·σ²/4 (in token1 units), a constant-product value
scales with √P, and a driftless walk's exit probability rises with σ and
horizon and falls with width.
"""

from __future__ import annotations

import json
import math

import pytest

from defi_tracker.analytics import (
    _exit_probability,
    _hit_probability,
    peaq_scenarios,
    position_risk,
    risk_model,
)

DEC = {"peaq:0xpeaq": 18, "peaq:0xusd": 18, "peaq:0xa": 18, "peaq:0xb": 18}
LN = math.log(1.0001)


def _cl_row(
    lo: int, cur: int, hi: int, L: float = 1000.0, syms=("WPEAQ", "USDT"), keys=("0xpeaq", "0xusd")
):
    s_lo, s_p, s_hi = (math.exp(t * LN / 2) for t in (lo, min(max(cur, lo), hi), hi))
    x = L * (1 / s_p - 1 / s_hi) if cur < hi else 0.0
    y = L * (s_p - s_lo) if cur > lo else 0.0
    if cur < lo:
        x, y = L * (1 / s_lo - 1 / s_hi), 0.0
    if cur > hi:
        x, y = 0.0, L * (s_hi - s_lo)
    price = math.exp(cur * LN)
    value = x * price + y  # token1 = $1
    return {
        "protocol_kind": "cl_amm", "current_value_usd": value, "tick_lower": lo,
        "tick_current": cur, "tick_upper": hi,
        "current_balances_json": json.dumps([
            {"token_key": f"peaq:{keys[0]}", "symbol": syms[0], "amount": str(x)},
            {"token_key": f"peaq:{keys[1]}", "symbol": syms[1], "amount": str(y)},
        ]),
    }  # fmt: skip


def test_the_model_reproduces_the_snapshot_value():
    row = _cl_row(-2000, 0, 2000)
    m = risk_model(row, DEC)
    assert m is not None
    assert m.value_at(0) == pytest.approx(row["current_value_usd"], rel=1e-9)


def test_breakeven_apr_matches_the_closed_form_expected_il():
    row = _cl_row(-2000, 0, 2000, L=1000.0)
    sigma_ticks = 500.0
    r = position_risk(row, DEC, sigma_ticks, n_obs=90)
    sig = sigma_ticks * LN
    expected_daily = 1000.0 * 1.0 * sig**2 / 4  # L·√P·σ²/4, √P = 1, p1 = $1
    assert r["breakeven_apr"] == pytest.approx(expected_daily * 365 / r["value"] * 100, rel=1e-3)
    lo, hi = r["breakeven_band"]
    assert lo < r["breakeven_apr"] < hi  # σ from 90 days carries an error bar


def test_in_range_is_short_gamma_and_out_of_range_is_linear():
    assert position_risk(_cl_row(-2000, 0, 2000), DEC)["gamma_1pct"] < 0
    below = position_risk(_cl_row(1000, -3000, 2000), DEC)  # all token0, price below range
    assert below["gamma_1pct"] == pytest.approx(0, abs=1e-9)
    assert below["shocks"][-0.2]["vs_hold"] == pytest.approx(0, abs=1e-9)


def test_a_price_drop_loses_more_than_holding_would():
    r = position_risk(_cl_row(-2000, 0, 2000), DEC)
    assert r["shocks"][-0.2]["vs_hold"] < 0 and r["shocks"][0.2]["vs_hold"] < 0


def test_constant_product_scales_with_the_root_of_price():
    row = {"protocol_kind": "xyk_amm", "current_value_usd": 200.0,
           "current_balances_json": json.dumps([{"token_key": "peaq:0xa", "amount": "100"},
                                                {"token_key": "peaq:0xb", "amount": "100"}])}  # fmt: skip
    r = position_risk(row, DEC)
    assert r["shocks"][0.2]["value"] == pytest.approx(200 * math.sqrt(1.2))


def test_exit_probability_behaves():
    wide, narrow = _exit_probability(5, 10, 0.1, 30), _exit_probability(0.1, 0.2, 0.1, 30)
    assert wide < 0.01 and narrow > 0.99
    assert _exit_probability(5, 10, 0.1, 30) < _exit_probability(5, 10, 0.2, 30)
    assert _exit_probability(1, 10, 0.3, 30) > _exit_probability(5, 10, 0.3, 30)  # nearer an edge


def test_reentry_probability_is_the_reflection_principle():
    assert _hit_probability(0.0, 0.1, 30) == pytest.approx(1.0)
    assert _hit_probability(0.1 * math.sqrt(30) * 1.96, 0.1, 30) == pytest.approx(0.05, abs=1e-3)


def test_without_sigma_the_sigma_figures_are_unknown_not_zero():
    r = position_risk(_cl_row(-2000, 0, 2000), DEC)
    assert r["breakeven_apr"] is None and r["exit_prob"] is None


def test_unmodellable_rows_are_none():
    assert position_risk({"protocol_kind": "cl_amm", "current_value_usd": 5.0,
                          "current_balances_json": "[]"}, DEC) is None  # fmt: skip


def test_a_peaq_shock_maps_onto_whichever_side_peaq_is_on():
    as_token0 = _cl_row(-2000, 0, 2000, syms=("WPEAQ", "USDT"))
    as_token1 = _cl_row(-2000, 0, 2000, syms=("USDT", "WPEAQ"), keys=("0xusd", "0xpeaq"))
    unrelated = _cl_row(-2000, 0, 2000, syms=("SLC", "USDC"), keys=("0xa", "0xb"))
    for row in (as_token0, as_token1):
        out = peaq_scenarios([row, unrelated], DEC)
        assert out["positions"] == 1
        assert out["changes"][-0.2] < 0 < out["changes"][0.2]
    # Symmetric pools: PEAQ-as-token1 down 20% ≈ token0 up 25% in pool terms.
    a = peaq_scenarios([as_token0], DEC)["changes"][-0.2]
    b = peaq_scenarios([as_token1], DEC)["changes"][-0.2]
    assert a < 0 and b < 0
