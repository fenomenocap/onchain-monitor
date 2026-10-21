"""
EV-ranked action queue (Phase 5.2).

A ticket's dollar case must be arithmetic a reader can redo: gain = after −
stay, each "fees − IL", at the central σ, with the band from σ's 95%
interval. The queue takes a pool's tickets in order, bounding depth against
the pool as it is today, so two tickets can never jointly empty a pool the
book owns. Costs nobody has given are not assumed — a ticket states the swap
cost it breaks even at.
"""

from __future__ import annotations

import html
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from defi_tracker.analytics import (
    RebalanceConfig,
    _price,
    _signed_pct,
    action_queue,
    describe_ticket,
    optimise_pool,
    plan_cards,
    pool_states,
    ticket_view,
)
from defi_tracker.report import ReportData
from defi_tracker.slack import SlackDelivery
from defi_tracker.web import render_dashboard
from tests.test_optimiser import DEC, VOL, _row
from tests.test_web import _snap

NO_FLOOR = RebalanceConfig(idle_min_usd=Decimal("0"))


def _queue(rows, config=NO_FLOOR, vol=VOL):
    plans = [optimise_pool(ps, config) for ps in pool_states(rows, DEC, vol)]
    return action_queue(plans, config)


def _dead_but_paid():
    """Out of range (price below it: all token0), ~$4k, in a pool with deep,
    paying outside liquidity — 400k units of other LPs' liquidity and
    $10k/day of fees, so the fee per unit of liquidity (0.025) beats the IL
    rate √P·p1·σ²/4 (6.25e-4) and re-entering adds under 50% to the depth."""
    return _row(3000, 0, 5000, 50_000.0, share=0.0, fees7=70_000.0,
                meta={"pool_liquidity": str(400_000 * 10**18)})  # fmt: skip


def test_a_dead_position_in_a_paying_pool_is_re_ranged_about_todays_price():
    (t,) = _queue([_dead_but_paid()]).tickets
    assert t.action in ("rerange", "resize")
    assert t.new_ticks is not None and t.new_ticks[0] <= 0 <= t.new_ticks[1]
    assert t.gain_usd == pytest.approx(t.after.ev_usd - t.stay.ev_usd)  # type: ignore[union-attr]
    assert t.gain_band[0] <= t.gain_usd <= t.gain_band[1]
    # The deposit is worth the capital it says (token1 = $1, price 1 at tick 0).
    assert sum(a for _, a in t.deposit) == pytest.approx(t.capital_after_usd, rel=1e-9)


def test_an_unknown_swap_cost_is_not_assumed_and_the_ticket_says_what_it_can_bear():
    (t,) = _queue([_dead_but_paid()], replace(NO_FLOOR, swap_cost_pct=None)).tickets
    assert t.swap_usd > 0 and t.swap_leg == ("A", "B")  # all token0 → half into token1
    assert t.swap_cost_usd is None
    assert t.tier == "soon"  # it waits on a quote
    assert t.breakeven_swap_pct == pytest.approx(t.gain_usd / t.swap_usd * 100)
    _, _, case = describe_ticket(t)
    assert any("swap cost not given — pays while the swap costs under" in c for c in case)


def test_a_given_swap_cost_is_charged_and_can_make_a_ticket_robust():
    (t,) = _queue([_dead_but_paid()], RebalanceConfig(idle_min_usd=Decimal("0"),
                                                       swap_cost_pct=0.3)).tickets  # fmt: skip
    assert t.swap_cost_usd == pytest.approx(t.swap_usd * 0.003)
    assert t.score_usd == pytest.approx(t.gain_usd - t.swap_cost_usd)
    robust = t.gain_band[0] - t.swap_cost_usd > 0
    assert t.tier == ("now" if robust and not t.at_edge else "soon")


def test_a_pool_whose_fees_cannot_pay_il_gets_a_close_worth_exactly_the_il_avoided():
    row = {**_row(-2000, 0, 2000, 1000.0, share=25.0, fees7=0.0001), "il_usd": -300.0}
    (t,) = _queue([row]).tickets
    assert t.action == "close"
    assert t.after is None and t.deposit == []
    assert t.gain_usd == pytest.approx(-t.stay.ev_usd)
    assert t.realised_il_usd == pytest.approx(-300.0)
    assert t.il_charged_usd == 0.0  # shown, not charged, by default
    assert t.tier == "now"  # positive across σ's band, no swap
    head, steps, case = describe_ticket(t)
    assert head.startswith("Close · A/B 1.00%")
    assert "Proceeds to treasury — nothing redeposited" in steps
    assert "After: nothing in the pool = $0" in case
    assert any(c.startswith("Makes −$300 of IL vs holding permanent") for c in case)


def test_charging_realised_il_is_the_operators_switch():
    row = {**_row(-2000, 0, 2000, 1000.0, share=25.0, fees7=0.0001), "il_usd": -1.0}
    cfg = RebalanceConfig(idle_min_usd=Decimal("0"), charge_realised_il=True)
    (t,) = _queue([row], cfg).tickets
    assert t.il_charged_usd == pytest.approx(1.0)
    assert t.score_usd == pytest.approx(t.gain_usd - 1.0)


def test_two_positions_that_own_a_pool_are_closed_in_sequence_never_twice_counted():
    """Each alone would pay to close — its fees go to the other. Closing
    both is not twice that: the second is scored after the first, with no
    sibling left to inherit its fees, and emptying the pool takes depth past
    the modelled bound — so it is flagged and never 'now'."""
    rows = [
        _row(-2000, 0, 2000, 1500.0, share=60.0, fees7=0.0001),
        {**_row(-2000, 0, 2000, 1000.0, share=40.0, fees7=0.0001), "position_key": "0xpool:b"},
    ]
    q = _queue(rows)
    assert q.tickets  # something pays: the book's depth costs more IL than the pool earns
    assert [t.order for t in q.tickets] == list(range(1, len(q.tickets) + 1))
    lo_b = NO_FLOOR.depth_bounds[0]
    inside = [t for t in q.tickets if 1 + t.depth_change >= lo_b - 1e-9]
    assert sum(1 for t in inside if t.action == "close") <= 1
    for t in q.tickets:
        if 1 + t.depth_change < lo_b - 1e-9:
            assert t.action == "close" and t.depth_extrapolated and t.tier == "soon"
            assert ticket_view(t).conviction != "high"


def test_a_close_past_the_depth_bound_is_scored_and_flagged():
    """The book is 100% of the pool and the pool pays nothing: closing is
    the answer, though it takes depth to zero — scored, not refused."""
    row = {**_row(-2000, 0, 2000, 1000.0, share=100.0, fees7=1.0), "il_usd": -50.0}
    (t,) = _queue([row]).tickets
    assert t.action == "close" and t.depth_extrapolated
    assert t.tier == "soon" and t.gain_usd > 0
    assert any("extrapolation" in n for n in t.notes)
    assert "depth past the modelled bound" in dict(ticket_view(t).rows)["Assumes"]


def test_the_ticket_arithmetic_reconciles_line_by_line():
    (t,) = _queue([_dead_but_paid()]).tickets
    _, _, case = describe_ticket(t)
    stay = next(c for c in case if c.startswith("Stay:"))
    assert f"= {'+' if t.stay.ev_usd >= 0 else '−'}${abs(t.stay.ev_usd):,.0f}" in stay
    assert f"E[IL] ${t.stay.il_usd:,.0f}" in stay


def test_a_pool_without_its_inputs_is_unscored_with_the_reason():
    q = _queue([_row(-2000, 0, 2000, 1000.0, share=25.0, fees7=None)])
    assert q.tickets == []
    (row, why) = q.unscored[0]
    assert "pool fees unknown" in why


def test_the_dust_floor_is_stated_not_silent():
    q = _queue([_row(-2000, 0, 2000, 1000.0, share=25.0, fees7=0.0001)], RebalanceConfig())
    assert q.tickets == []
    assert "below the $5,000 floor" in q.unscored[0][1]


def test_a_queue_item_carries_what_the_cli_shows(storage):
    (t,) = _queue([_dead_but_paid()]).tickets
    storage.reconcile_rebalance_queue([t.queue_item()])
    (live,) = storage.list_rebalance_queue()
    ctx = json.loads(live["context_json"])
    assert live["position_uid"] == t.uid and live["tier"] == t.tier
    assert ctx["action"] == t.action and ctx["score_usd"] == pytest.approx(t.score_usd)


# ── surfaces render the same ticket in the same words ─────────────────────


def _report(queue, snoozed=frozenset()):
    return ReportData(snaps=[_snap()], actions=queue,
                      snoozed_uids=set(snoozed))  # fmt: skip


def test_page_and_slack_carry_the_same_rows():
    q = _queue([_dead_but_paid()])
    v = ticket_view(q.tickets[0])
    rows = dict(v.rows)
    page = render_dashboard(_report(q))
    assert "Action queue · 1 ticket" in page
    for words in (v.action, v.ev, rows["Deploy"], rows["Current"], rows["Drivers"]):
        assert html.escape(words) in page
    assert "Ticket and calculation" in page
    # Slack is the overview: action, NFT, deploy band, EV, conviction —
    # the rows stay on the desk.
    blocks = SlackDelivery("https://hooks.slack.test/x")._ticket_blocks(_report(q))  # noqa: SLF001
    text = "\n".join(b["text"]["text"] for b in blocks if "text" in b)
    assert v.width is not None
    for words in (v.action, v.ref, v.width, v.ev, v.conviction.upper()):
        assert words in text
    assert rows["Drivers"] not in text and "full tickets on the desk" in text


def test_a_snoozed_ticket_is_counted_not_shown():
    q = _queue([_dead_but_paid()])
    page = render_dashboard(_report(q, {q.tickets[0].uid}))
    assert "Action queue · 0 tickets" in page and "1 snoozed" in page


def test_the_optimiser_table_says_why_a_pool_has_no_answer():
    q = _queue([_row(-2000, 0, 2000, 1000.0, share=25.0, fees7=None)])
    page = render_dashboard(_report(q))
    assert "Range optimiser" in page and "pool fees unknown" in page
    assert "Not scored" in page and "unknown, not zero" in page


def test_a_pool_too_thin_to_re_enter_can_still_be_closed():
    """Other LPs' liquidity is so thin that re-entering at any width more
    than doubles the pool's depth — no re-range is scored — but closing an
    out-of-range position moves no active depth, so it still is."""
    row = {**_row(3000, 0, 5000, 50_000.0, share=0.0, fees7=0.0001,
                  meta={"pool_liquidity": str(10**18)}), "il_usd": -50.0}  # fmt: skip
    q = _queue([row])
    assert q.plans[0].reason is not None and "depth" in q.plans[0].reason
    (t,) = q.tickets
    assert t.action == "close" and t.depth_change == pytest.approx(0.0)


# ── the plain-words view ──────────────────────────────────────────────────


def test_the_deploy_row_gives_the_range_in_dollars_and_ticks():
    (t,) = _queue([_dead_but_paid()], replace(NO_FLOOR, swap_cost_pct=None)).tickets
    v = ticket_view(t)
    assert v.action.startswith(("WIDEN", "NARROW", "RE-CENTER"))
    rows = dict(v.rows)
    # A trades at 1 B today (tick 0) and B is $1: the band's edges in
    # dollars are 1.0001^tick at the new ticks.
    lo, hi = t.new_ticks  # type: ignore[misc]
    assert rows["Deploy"] == (
        f"{_price(pow(1.0001, lo))} – {_price(pow(1.0001, hi))}  ·  {v.width}  ·  ticks {lo} / {hi}"
    )
    # Spot is $1 here, so the band's ± from spot is 1.0001^tick − 1.
    assert v.width == f"{_signed_pct((pow(1.0001, lo) - 1) * 100)} / " + _signed_pct(
        (pow(1.0001, hi) - 1) * 100
    )
    # Pool depth within ±2%, today → after, moves by the ticket's depth change.
    before, after = (
        float(x.replace("$", "").replace(",", "").replace("k", "e3"))
        for x in rows["Depth ±2%"].split("  ·  ")[0].split(" (")[0].split(" → ")
    )
    assert after / before - 1 == pytest.approx(t.depth_change, abs=0.02)
    assert rows["Current"].endswith("out of range")
    assert rows["Swap"].startswith("$") and "break-even" in rows["Swap"]
    assert "Deposit" in rows and "Withdraw" in rows
    assert v.group == "quote"  # a swap is needed and no swap cost is set


def test_a_ticket_whose_outcomes_include_a_loss_is_low_conviction_and_marginal():
    (t,) = _queue([_dead_but_paid()]).tickets
    t.gain_band = (-1.0, t.gain_band[1])
    v = ticket_view(t)
    assert v.conviction == "low" and v.group == "borderline"
    assert "σ band includes a loss" in dict(v.rows)["Assumes"]


def test_a_close_that_leans_on_volume_holding_says_so():
    row = {**_row(-2000, 0, 2000, 1000.0, share=25.0, fees7=0.0001), "il_usd": -300.0}
    (t,) = _queue([row]).tickets
    v = ticket_view(t)
    rows = dict(v.rows)
    assert v.action == "CLOSE"
    assert rows["Deploy"] == "none — proceeds to treasury"
    assert v.conviction == "high"  # pays at every ε in the bracket
    assert "fees ∝ depth^0.5; pool depth −25%" in rows["Assumes"]
    assert "ε 0 / 1" in rows["Volume band"]
    assert rows["Crystallises"].startswith("−$300")
    assert v.group == "do"


def test_a_pool_card_takes_its_first_legs_group():
    rows = [
        _row(-2000, 0, 2000, 1500.0, share=60.0, fees7=0.0001),
        {**_row(-2000, 0, 2000, 1000.0, share=40.0, fees7=0.0001), "position_key": "0xpool:b"},
    ]
    q = _queue(rows)
    (card,) = plan_cards(q)
    assert card.group == card.steps[0].group
    assert [v.ticket.order for v in card.steps] == sorted(v.ticket.order for v in card.steps)


def test_closing_a_position_that_brings_depth_costs_its_flow_once_volume_follows_depth():
    """Two positions own a pool. At ε = 0 closing one is nearly free — its
    fees move to the other. At ε = 1 its depth carried its own flow, so
    the close gives that up: the volume band says so, and a close that
    only pays at ε = 0 is not robust on the model."""
    rows = [
        _row(-2000, 0, 2000, 1500.0, share=60.0, fees7=10.0),
        {**_row(-2000, 0, 2000, 1000.0, share=40.0, fees7=10.0), "position_key": "0xpool:b"},
    ]
    (close,) = [t for t in _queue(rows).tickets if t.action == "close"]
    at_zero, at_one = close.volume_band
    # ε = 0: its fees move to the sibling, so the close saves its IL for free.
    assert at_zero == pytest.approx(close.stay.il_usd, rel=0.05)
    # ε = 1: its depth carried its own flow, which leaves with it.
    assert at_one < 0.2 * at_zero


@pytest.mark.parametrize("fees7, bound", [(3.0, 0), (100.0, 1)])
def test_a_resize_sized_to_the_depth_limit_says_it_is_a_direction(fees7, bound):
    """The book is the whole pool. With fees this thin (or this rich) EV
    keeps rising as the position shrinks (or grows), so the size stops at
    the depth limit — where the search ended, not where EV peaked."""
    row = {**_row(-2000, 0, 2000, 1000.0, share=100.0, fees7=fees7), "il_usd": -50.0}
    (t,) = _queue([row]).tickets
    assert t.action == "resize"
    assert 1 + t.depth_change == pytest.approx(NO_FLOOR.depth_bounds[bound], rel=1e-3)
    assert t.at_edge
    assert any("sized to the depth limit" in n for n in t.notes)
    assert ticket_view(t).conviction != "high"


def test_the_volume_band_is_labelled_with_the_bracket_it_was_read_at():
    cfg = RebalanceConfig(idle_min_usd=Decimal("0"), elasticity_band=(0.2, 0.8))
    (t,) = _queue([_dead_but_paid()], cfg).tickets
    assert t.elasticity_band == (0.2, 0.8)
    assert dict(ticket_view(t).rows)["Volume band"].endswith("ε 0.2 / 0.8")
    _, _, case = describe_ticket(t)
    assert any("; ε 0.2 / 0.8: " in c for c in case) and not any("ε 0 / 1" in c for c in case)


def test_slack_never_shows_an_empty_queue_as_nothing_to_do():
    slack = SlackDelivery("https://hooks.slack.test/x")

    def text(report):
        return "\n".join(b["text"]["text"] for b in slack._ticket_blocks(report) if "text" in b)  # noqa: SLF001

    assert "not computed" in text(ReportData(snaps=[_snap()], actions=None))
    q = _queue([_row(-2000, 0, 2000, 1000.0, share=25.0, fees7=None)])
    assert q.tickets == [] and q.unscored
    said = text(_report(q))
    assert "0 tickets" in said and "1 position(s) not scored (unknown, not zero)" in said


def test_every_ticket_says_why_it_exists_and_what_to_settle_first():
    from defi_tracker.analytics import ticket_checks, ticket_why

    row = {**_row(-2000, 0, 2000, 1000.0, share=25.0, fees7=0.0001), "il_usd": -300.0}
    (t,) = _queue([row]).tickets
    assert t.action == "close"
    why = ticket_why(t)
    assert why.startswith("In range") and "IL outweighs the fees" in why
    assert ticket_why(t, short=True).startswith("in range, fees")
    rows = dict(ticket_view(t).rows)
    assert rows["Why"] == why
    checks = ticket_checks(t)
    assert any(c.startswith("accept locking in −$300 of IL") for c in checks)
    assert rows["Before acting"] == "  ·  ".join(checks)
    _, _, case = describe_ticket(t)
    assert case[0] == f"Why: {why}" and case[-1].startswith("Before acting: ")


def test_the_queue_carries_the_backtests_verdict_on_every_surface():
    from defi_tracker.analytics import Backtest, BacktestWindow, backtest_verdict

    assert backtest_verdict(None).startswith("Advisory")
    w = BacktestWindow(
        "0xp", "A/B", "2026-01-01", "2026-01-31", 1e5, [(0, 1)], (0, 1), 500.0, 0.0, -900.0
    )
    bt = Backtest([w], [], 30, 1.0, "optimiser", ("peaq",))
    v = backtest_verdict(bt, {"peaq", "bsc"})
    assert v.startswith("Advisory only") and "bsc not backtested" in v
    q = _queue([_dead_but_paid()])
    report = ReportData(snaps=[_snap()], actions=q, backtest=bt)
    assert html.escape(backtest_verdict(bt, {"peaq"})) in render_dashboard(report)
    blocks = SlackDelivery("https://hooks.slack.test/x")._ticket_blocks(report)  # noqa: SLF001
    assert any("Advisory only" in json.dumps(b) for b in blocks)


def test_a_ticket_at_the_models_limit_is_never_now_even_with_every_cost_known():
    """A resize sized to the depth bound (or a band at the search edge) is a
    direction: it may be robust on the model, but it must not push a
    REBALANCE alert as if the destination were known."""
    q = _queue([_dead_but_paid()], replace(NO_FLOOR, swap_cost_pct=0.0))
    edge = [t for t in q.tickets if t.at_edge]
    assert edge and all(t.tier != "now" for t in edge)
