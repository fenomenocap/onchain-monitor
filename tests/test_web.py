"""
HTML desk view.

The page renders numbers a human will act on, so these tests are mostly about
what it must NEVER say: a stale mark presented as today's, an unreadable
balance shown as zero, a full-range position rendered as a 10^40% band, or a
pair label that escapes into markup.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from defi_tracker.core.storage import ValueDelta
from defi_tracker.report import ReportData, gather_report_data
from defi_tracker.web import _range_pct, render_dashboard


def _snap(**over) -> dict:
    """A snapshot row shaped like Storage.latest_snapshot() returns."""
    row = {
        "snapshot_date": "2026-09-21",
        "wallet": "0xabc123def456abc123def456abc123def456abc1",
        "chain": "peaq",
        "protocol_id": "machinex",
        "position_key": "1001",
        "pair_label": "WPEAQ/USDT 1.00%",
        "current_value_usd": 10_000.0,
        "unclaimed_usd": 250.0,
        "unclaimed_is_known": 1,
        "cost_basis_usd": 12_000.0,
        "hold_value_usd": 11_000.0,
        "il_usd": -1_000.0,
        "il_pct": -9.1,
        "pnl_usd": -1_750.0,
        "in_range": 1,
        "tick_lower": -10_000,
        "tick_upper": 10_000,
        "tick_current": 0,
    }
    row.update(over)
    return row


def _data(snaps=None, **over) -> ReportData:
    kw = {"snaps": snaps or [_snap()]}
    kw.update(over)
    return ReportData(**kw)


# ── the page renders at all ───────────────────────────────────────────────


def test_renders_a_complete_html_document():
    out = render_dashboard(_data())
    assert out.startswith("<!doctype html>")
    assert out.rstrip().endswith("</html>")
    assert "WPEAQ/USDT 1.00%" in out
    assert "$10,000" in out


def test_survives_a_book_with_no_events_behind_it():
    """NULL analytics are the honest answer for a position with no event
    history — the page must render them, not crash on the None."""
    out = render_dashboard(_data([_snap(il_usd=None, pnl_usd=None, cost_basis_usd=None)]))
    assert "no event history" in out


# ── what it must never say ────────────────────────────────────────────────


def test_unreadable_unclaimed_is_a_dash_never_zero():
    out = render_dashboard(_data([_snap(unclaimed_is_known=0, unclaimed_usd=0.0)]))
    assert "unreadable" in out
    # The positions table must not claim a zero balance we never read.
    rows = out.split("All positions", 1)[1].split("</table>", 1)[0]
    assert "<td>—</td>" in rows, rows
    assert "<td>$0</td>" not in rows, rows


def test_full_range_position_does_not_render_an_astronomical_percentage():
    assert _range_pct({"tick_lower": -887_272, "tick_upper": 887_272}) == "full"
    out = render_dashboard(_data([_snap(tick_lower=-887_272, tick_upper=887_272)]))
    assert "e+" not in out
    assert "full" in out


@pytest.mark.parametrize(
    ("lo", "hi", "expect"),
    [
        (-10_000, 10_000, "±172%"),
        (-100, 100, "±1%"),
        (None, None, "—"),
        (-200_000, 200_000, ">±999%"),
    ],
)
def test_range_width_reads_as_a_percentage_band(lo, hi, expect):
    assert _range_pct({"tick_lower": lo, "tick_upper": hi}) == expect


def test_pair_labels_are_escaped():
    out = render_dashboard(_data([_snap(pair_label='<img src=x onerror="alert(1)">')]))
    assert "<img src=x" not in out
    assert "&lt;img" in out


# ── staleness is stated, never implied away ───────────────────────────────


def test_marks_carry_their_own_as_of_date():
    out = render_dashboard(_data([_snap(snapshot_date="2026-09-18")]))
    assert "marks 2026-09-18" in out


def test_a_day_behind_says_so():
    stale = "2000-01-01"
    out = render_dashboard(_data([_snap(snapshot_date=stale)]))
    assert f"as of {stale}" in out
    assert "No snapshot pass has completed today" in out


def test_todays_marks_do_not_raise_a_staleness_note():
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    out = render_dashboard(_data([_snap(snapshot_date=today)]))
    assert "No snapshot pass has completed today" not in out


def test_a_multi_day_gap_is_labelled_with_its_real_span():
    """The delta carries its own window, and the page must print that window
    rather than defaulting to 'today'."""
    out = render_dashboard(
        _data(value_delta=ValueDelta(110_000.0, 100_000.0, "2026-09-21", "2026-09-17"))
    )
    assert "since 2026-09-17 (4d)" in out
    assert "today</span>" not in out
    assert "4 day(s), not necessarily one" in out


def test_a_one_day_span_says_yesterday():
    out = render_dashboard(
        _data(value_delta=ValueDelta(110_000.0, 100_000.0, "2026-09-21", "2026-09-20"))
    )
    assert "since yesterday" in out


def test_positions_left_out_of_the_delta_are_named_on_the_page():
    out = render_dashboard(
        _data(
            value_delta=ValueDelta(
                110_000.0,
                100_000.0,
                "2026-09-21",
                "2026-09-20",
                excluded_positions=3,
                excluded_value=529_000.0,
            )
        )
    )
    assert "excludes 3 position(s)" in out
    assert "$529,000" in out


def test_a_zero_baseline_renders_no_change_line_at_all():
    """A percentage off zero is undefined; the page withholds it rather than
    printing a confident +0.00%."""
    out = render_dashboard(_data(value_delta=ValueDelta(50.0, 0.0, "2026-09-21", "2026-09-20")))
    assert "since yesterday" not in out


def test_degraded_sources_surface_as_a_banner():
    out = render_dashboard(
        _data(
            health_rows=[
                {
                    "wallet": "0xabc123def456abc123def456abc123def456abc1",
                    "chain": "bsc",
                    "protocol_id": "pancake_infinity",
                    "last_run_at": 1,
                    "last_sync_ok_at": 1,
                    "last_snapshot_ok_at": 1,
                    "source": "rpc_fallback",
                    "last_error": "subgraph outage",
                    "dropped_events_last_run": 0,
                }
            ]
        )
    )
    assert "Data status" in out


# ── the gatherer ──────────────────────────────────────────────────────────


def test_gather_returns_none_when_nothing_has_been_snapshotted(storage):
    assert gather_report_data(storage) is None


# ── cost lines ────────────────────────────────────────────────────────────


def test_gas_renders_as_a_cost_line():
    out = render_dashboard(_data(tx_cost_rows=[{"gas_usd": 412.0, "slippage_usd": None}]))
    assert "Gas paid" in out
    assert "-$412.00" in out


def test_partial_gas_coverage_is_stated_where_the_number_is_read():
    """A gas total covering one transaction in three is a floor. Saying so
    only in a footnote lets the number be read as a total."""
    out = render_dashboard(
        _data(
            tx_cost_rows=[
                {"gas_usd": 412.0, "slippage_usd": None},
                {"gas_usd": None, "slippage_usd": None},
                {"gas_usd": None, "slippage_usd": None},
            ]
        )
    )
    assert "(1 of 3 tx)" in out
    assert "Book net after costs" not in out
    assert "Gas is priced for 1 of 3" in out


def test_book_net_after_costs_appears_once_the_cost_base_is_complete():
    out = render_dashboard(_data(tx_cost_rows=[{"gas_usd": 412.0, "slippage_usd": None}]))
    assert "Book net after costs" in out
    assert "(1 of 1 tx)" not in out


def test_slippage_renders_as_unknown_never_as_zero():
    out = render_dashboard(_data(tx_cost_rows=[{"gas_usd": 412.0, "slippage_usd": None}]))
    assert "Slippage" in out
    assert "$0" not in out.split("Slippage")[1].split("</div>")[0]


def test_no_cost_history_means_no_cost_lines():
    out = render_dashboard(_data())
    assert "Gas paid" not in out


# ── 1.6: periods, risk and the mandate on the page ────────────────────────


def _book_with_history(storage, make_position, make_event):
    from datetime import date as _date

    from defi_tracker.core.types import Chain, EventKind

    storage.upsert_events([make_event(kind=EventKind.DEPOSIT, ts=1_767_000_000)])
    for d, v in ((_date(2026, 8, 31), "1000"), (_date(2026, 9, 25), "1100")):
        pos = make_position(current_value_usd=Decimal(v))
        pos.snapshot_at = int(datetime(d.year, d.month, d.day, 12, tzinfo=UTC).timestamp())
        storage.write_snapshot(pos, snapshot_date=d, il_usd=Decimal("0"), hold_value_usd=Decimal(v))
    storage.record_pipeline_health(
        "0xabc123def456abc123def456abc123def456abc1", Chain.BSC, "pancake_infinity",
        now=int(datetime(2026, 9, 25, 23, tzinfo=UTC).timestamp()), sync_ok=True,
        snapshot_ok=True, source="subgraph", error=None,
    )  # fmt: skip


def test_the_mtd_tile_is_flow_netted_and_says_its_coverage(storage, make_position, make_event):
    _book_with_history(storage, make_position, make_event)
    out = render_dashboard(gather_report_data(storage))
    tile = out.split(">PnL MTD<", 1)[1][:200]
    assert "flows netted" in tile and "of book" in tile


def test_the_page_shows_every_window_and_the_benchmark(storage, make_position, make_event):
    _book_with_history(storage, make_position, make_event)
    out = render_dashboard(gather_report_data(storage))
    assert "Performance by period" in out and "vs holding the deposited tokens" in out
    for kind in ("D (", "MTD (", "ITD ("):
        assert kind in out


def test_a_window_with_nothing_established_says_so_on_the_page():
    from defi_tracker.analytics import PeriodExclusion, PeriodPnL

    empty = PeriodPnL("WTD", date(2026, 9, 21), date(2026, 9, 25), [],
                      [PeriodExclusion("w", "peaq", "m", "k", "X", Decimal("5"), "no mark")])  # fmt: skip
    out = render_dashboard(_data(periods=[empty]))
    assert "every position excluded — no figure, not zero" in out


def test_the_mandate_section_says_unset_limits_are_not_limits(storage, make_position, make_event):
    _book_with_history(storage, make_position, make_event)
    out = render_dashboard(gather_report_data(storage))
    assert "Not set" in out
    assert "Counterparty exposure is unknown, not zero" in out


def test_the_risk_section_renders_the_peaq_shock_row(storage, make_position, make_event):
    _book_with_history(storage, make_position, make_event)
    out = render_dashboard(gather_report_data(storage))
    assert "PEAQ move" in out and "Breakeven fee APR" in out


# ── desk tabs and the mirrored proposal doc ───────────────────────────────


def test_every_tab_has_its_panel_and_the_overview_comes_first():
    from defi_tracker.web import _TABS

    out = render_dashboard(_data())
    for tab_id, label in _TABS:
        assert f'<a href="#{tab_id}">{label}</a>' in out
        assert f'<section class="panel" id="{tab_id}">' in out
    assert out.index('id="overview"') < out.index('id="actions"')
    assert out.count("<section") == out.count("</section>")


def test_the_desk_links_the_proposal_doc_rather_than_copying_it(monkeypatch):
    """One place for each thing: the proposal and MM Comps live in the doc."""
    import defi_tracker.analytics as an

    monkeypatch.setattr(an, "PROPOSAL_URL", "https://example.com/proposal")
    out = render_dashboard(_data())
    assert '<a href="https://example.com/proposal">Proposal ↗</a>' in out
    assert 'id="proposal"' not in out and 'id="mm-comps"' not in out


def test_no_configured_proposal_link_renders_no_dead_link(monkeypatch):
    import defi_tracker.analytics as an

    monkeypatch.setattr(an, "PROPOSAL_URL", "")
    out = render_dashboard(_data())
    assert 'href=""' not in out and "Proposal ↗" not in out


def test_the_mm_book_marks_each_contract_at_peaq_now_and_keeps_the_keyed_value_dated():
    from defi_tracker.analytics import PeaqMark

    mark = PeaqMark(
        price=Decimal("0.04"), ts=1_791_850_000, source="market",
        pool_price=Decimal("0.0396"), pool_ts=1_791_840_000,
        market_price=Decimal("0.04"), market_ts=1_791_850_000,
    )  # fmt: skip
    row = {
        "ref": "alpha-loan-option", "as_of": "2026-09-30", "counterparty": "Alpha MM",
        "kind": "mm_loan", "status": "active", "asset": "PEAQ",
        "amount": Decimal("20250000"), "usd_value": 850000.0, "end_date": "2026-11-01",
        "terms": "Loan + call option.", "annual_cost_usd": None, "source_note": "deck",
        "usd_now": Decimal("810000"), "days_left": 19, "tranches": [],
        "exercised": Decimal(0), "outstanding": Decimal("20250000"),
    }  # fmt: skip
    out = render_dashboard(_data(peaq=mark, mm_book=[row]))
    assert "$810,000" in out  # at PEAQ now
    assert "20.25m" in out  # the loan, in PEAQ, as the operator's sheet shows it
    assert "<strong>$0.0400</strong>" in out and "(+1.0%)" in out
    assert "2026-11-01 (19d)" in out
    assert "not all keyed" in out  # one unknown cost makes the total unknown
    assert "Not in the documents on file" in out


def test_the_mm_book_shows_each_tranche_against_spot_and_flags_an_open_window():
    from defi_tracker.analytics import PeaqMark, tranche_marks

    mark = PeaqMark(Decimal("0.04"), 1, "market", None, None, Decimal("0.04"), 1)
    today = datetime.now(UTC).date().isoformat()
    tranches = tranche_marks(
        [
            {
                "ref": "alpha",
                "tranche": "1",
                "asset": "PEAQ",
                "amount": Decimal("4000000"),
                "strike_usd": Decimal("0.30"),
                "strike_rule": None,
                "status": "open",
                "exercisable_from": "2020-01-01",
                "exercisable_to": today,
            },
            {
                "ref": "alpha",
                "tranche": "2",
                "asset": "PEAQ",
                "amount": Decimal("1000000"),
                "strike_usd": None,
                "strike_rule": "150% of ATM, set by the MM",
                "status": "open",
                "exercisable_from": None,
                "exercisable_to": None,
            },
        ],  # fmt: skip
        mark.price,
    )
    row = {
        "ref": "alpha", "as_of": "2026-10-13", "counterparty": "Alpha MM", "kind": "mm_loan",
        "status": "active", "asset": "PEAQ", "amount": Decimal("20000000"), "usd_value": None,
        "end_date": None, "terms": "", "annual_cost_usd": 0.0, "source_note": "MLA",
        "usd_now": Decimal("800000"), "days_left": None, "tranches": tranches,
        "exercised": Decimal(0), "outstanding": Decimal("20000000"),
    }  # fmt: skip
    out = render_dashboard(_data(peaq=mark, mm_book=[row]))
    assert "7.5× spot" in out  # 0.30 / 0.04
    assert "WINDOW OPEN" in out and "Option ladder" in out
    assert "<td>NO</td>" in out  # not exercised
    assert "not fixed" in out


def test_the_mm_book_shows_the_incumbents_own_proposal_under_its_signed_ladder():
    """An incumbent's renewal proposal: 26m for 9 months, half at +10% and
    half at +25% of $0.04 → $0.0440 / $0.0500, flagged as not signed. A
    counter of ours is not shown as their proposal."""
    from defi_tracker.analytics import PeaqMark, tranche_marks

    mark = PeaqMark(Decimal("0.04"), 1, "market", None, None, Decimal("0.04"), 1)
    tranches = tranche_marks(
        [{"ref": "beta", "tranche": "6", "asset": "PEAQ", "amount": Decimal("8000000"),
          "strike_usd": Decimal("0.02"), "strike_rule": None, "status": "open",
          "exercisable_from": "2026-12-01", "exercisable_to": "2026-12-01"}],
        mark.price,
    )  # fmt: skip
    row = {
        "ref": "beta", "as_of": "2026-10-19", "counterparty": "Beta MM", "kind": "mm_loan",
        "status": "active", "asset": "PEAQ", "amount": Decimal("30000000"), "usd_value": None,
        "end_date": None, "terms": "", "annual_cost_usd": 0.0, "source_note": "MLA",
        "usd_now": Decimal("880000"), "days_left": None, "tranches": tranches,
        "exercised": Decimal("3000000"), "returned": Decimal("5000000"),
        "outstanding": Decimal("22000000"),
    }  # fmt: skip
    rfq = {"bid": [
        {"name": "Beta MM · renewal", "kind": "renewal proposal · term sheet",
         "incumbent": "Beta MM", "loan_peaq": 26000000, "ref_price": 0.04, "tenor_months": 9,
         "options": [{"share": 0.5, "strike_pct": 110}, {"share": 0.5, "strike_pct": 125}]},
        {"name": "Alpha MM · counter", "kind": "our counter", "incumbent": "Alpha MM", "loan_peaq": 1,
         "options": [{"share": 1, "strike_pct": 100}]},
    ]}  # fmt: skip
    out = render_dashboard(_data(peaq=mark, mm_book=[row], rfq=rfq, peaq_sigma=(0.06, 90, "pool")))
    assert "Proposed · not signed · renewal proposal" in out
    assert "+4.00m on the 22.00m held" in out
    assert "$0.0440" in out and "$0.0500" in out and "13.00m" in out
    assert out.count("Proposed · not signed") == 1  # Alpha MM's counter is ours, not theirs


def test_an_unknown_peaq_price_withholds_every_value():
    out = render_dashboard(_data(mm_book=[]))
    assert "PEAQ price unknown" in out


def test_the_brief_lists_what_falls_due_and_each_mm_on_one_line():
    """The two-minute read: a loan ending and an option window closing inside
    60 days are dated rows; each MM is one line with its nearest strike."""
    from datetime import timedelta

    from defi_tracker.analytics import PeaqMark, tranche_marks

    today = datetime.now(UTC).date()
    soon, later = (today + timedelta(10)).isoformat(), (today + timedelta(400)).isoformat()
    mark = PeaqMark(Decimal("0.04"), 1, "market", None, None, Decimal("0.04"), 1)
    tranches = tranche_marks(
        [
            {
                "ref": "g",
                "tranche": "1",
                "asset": "PEAQ",
                "amount": Decimal("4000000"),
                "strike_usd": Decimal("0.30"),
                "strike_rule": None,
                "status": "open",
                "exercisable_from": today.isoformat(),
                "exercisable_to": soon,
            },
        ],  # fmt: skip
        mark.price,
    )
    base = {"kind": "mm_loan", "status": "active", "asset": "PEAQ", "usd_value": None,
            "terms": "", "annual_cost_usd": 0.0, "source_note": "x", "as_of": "2026-10-13",
            "exercised": Decimal(0)}  # fmt: skip
    book = [
        {**base, "ref": "k", "counterparty": "Beta MM", "amount": Decimal("30500000"),
         "outstanding": Decimal("30500000"), "usd_now": Decimal("1220000"), "end_date": soon,
         "days_left": 10, "tranches": []},
        {**base, "ref": "g", "counterparty": "Alpha MM", "amount": Decimal("20000000"),
         "outstanding": Decimal("20000000"), "usd_now": Decimal("800000"), "end_date": later,
         "days_left": 400, "tranches": tranches},
    ]  # fmt: skip
    out = render_dashboard(_data(peaq=mark, mm_book=book))
    brief = out.split('id="overview"', 1)[1].split("</section>", 1)[0]
    assert "Beta MM loan ends" in brief and "30.50m PEAQ due back" in brief
    assert (
        "Alpha MM option #1 window closes" in brief
        and "strike 7.5× spot, out of the money" in brief
    )
    assert "not on file" in brief  # Beta MM's options are not keyed
    assert "$2.02M" in brief  # MM loans at PEAQ now


def test_key_terms_are_a_grid_term_by_mm_never_prose():
    import json

    base = {"ref": "x", "as_of": "2026-10-13", "kind": "mm_loan", "status": "active",
            "asset": "PEAQ", "amount": None, "usd_value": None, "end_date": None, "terms": "long prose",
            "annual_cost_usd": None, "source_note": "MLA p.1", "usd_now": None, "days_left": None,
            "tranches": [], "exercised": Decimal(0), "outstanding": None}  # fmt: skip
    book = [
        {**base, "counterparty": "Alpha MM", "key_terms_json": json.dumps({"Loan ends": "1 Mar 2028"})},
        {**base, "counterparty": "Beta MM",
         "key_terms_json": json.dumps({"Loan ends": "1–15 Dec 2026", "Options": "Not on file"})},
    ]  # fmt: skip
    out = render_dashboard(_data(mm_book=book))
    assert "<h2>Key terms</h2>" in out and "long prose" not in out
    assert '<td class="pair">Loan ends</td><td class="">1 Mar 2028</td>' in out
    assert '<td class="down">Not on file</td>' in out  # a gap stands out


def test_early_termination_sits_in_each_mm_card_and_the_brief_not_the_grid():
    import json

    terms = {"Loan ends": "1 Mar 2028", "Exit, short": "Not possible",
             "peaq's right to terminate": "None while the loan is out",
             "Cost if peaq terminates today": "No break fee"}  # fmt: skip
    row = {"ref": "g", "as_of": "2026-10-13", "counterparty": "Alpha MM", "kind": "mm_loan",
           "status": "active", "asset": "PEAQ", "amount": Decimal("20000000"), "usd_value": None,
           "end_date": None, "terms": "", "annual_cost_usd": 0.0, "source_note": "MLA",
           "usd_now": Decimal("800000"), "days_left": None, "tranches": [],
           "exercised": Decimal(0), "outstanding": Decimal("20000000"),
           "key_terms_json": json.dumps(terms)}  # fmt: skip
    out = render_dashboard(_data(mm_book=[row]))
    card = out.split('class="mm card"', 1)[1]
    assert "If peaq terminates early" in card and "No break fee" in card
    grid = out.split("<h2>Key terms</h2>", 1)[1]
    assert "Cost if peaq" not in grid and "Loan ends" in grid
    brief = out.split('id="overview"', 1)[1].split("</section>", 1)[0]
    assert "<th>peaq early exit</th>" in brief and "<td>Not possible</td>" in brief


def test_venues_show_where_peaq_trades_against_each_mms_contract():
    import json

    from defi_tracker.analytics import venue_matrix

    venues = [
        {"venue": "Gate", "pairs": ["PEAQ/USDT"], "volume_usd": 300.0, "depth_up_usd": 60.0,
         "depth_down_usd": 60.0, "spread_pct": 0.06},
        {"venue": "MEXC", "pairs": ["PEAQ/USDT"], "volume_usd": 200.0, "depth_up_usd": 10.0,
         "depth_down_usd": 3.0, "spread_pct": 0.1},
        {"venue": "Crypto.com Exchange", "pairs": ["PEAQ/USD"], "volume_usd": 20.0,
         "depth_up_usd": 38.0, "depth_down_usd": 48.0, "spread_pct": 0.09},
        {"venue": "Pancakeswap Infinity CLMM (BSC)", "pairs": ["x/y"], "volume_usd": 100.0,
         "depth_up_usd": 10.0, "depth_down_usd": 10.0, "spread_pct": 0.6},
    ]  # fmt: skip
    book = [
        {"counterparty": "Gamma MM", "key_terms_json": json.dumps({"Venues": "Bybit, Gate, Crypto.com"})},
        {"counterparty": "Alpha MM", "key_terms_json": json.dumps({"Venues": "Gate, Kraken"})},
    ]  # fmt: skip
    m = venue_matrix(venues, book)
    gate, mexc, cdc, pcs = m["rows"]
    assert gate["mm"] == {"Gamma MM": True, "Alpha MM": True} and cdc["mm"]["Gamma MM"] is True
    assert not any(mexc["mm"].values()) and pcs["dex"]
    assert round(m["uncovered_pct"], 1) == round(200 / 620 * 100, 1)  # MEXC only; DEX excluded
    assert m["unlisted"] == {"Gamma MM": ["bybit"], "Alpha MM": ["kraken"]}

    out = render_dashboard(_data(venues=venues, mm_book=[]))
    assert 'id="venues"' in out and "MEXC" in out


def test_venues_unread_say_so():
    out = render_dashboard(_data())
    assert "Markets not read for this build" in out


def test_the_rfq_tab_sets_bids_beside_live_market_depth_and_scores_only_complete_rows():
    from defi_tracker.analytics import rfq_scores

    crit = [{"criterion": "Depth", "weight": 60, "scores": {"A": 4, "B": 2}},
            {"criterion": "Cost", "weight": 40, "scores": {"A": 3}}]  # fmt: skip
    assert rfq_scores(crit, ["A", "B"]) == {"A": 3.6, "B": None}  # B is unscored on cost

    rfq = {"as_of": "2026-10-14", "bidders": ["A", "B"],
           "scope": [{"term": "Ask", "cells": {"A": "+6m PEAQ", "B": "Retainer"}}],
           "depth": [{"venue": "Gate", "cells": {"A": "$60k", "B": "TBD"}},
                     {"venue": "Bybit perp", "cells": {"A": "$22k"}}],
           "score": crit}  # fmt: skip
    venues = [{"venue": "Gate", "pairs": [], "volume_usd": 100.0, "depth_up_usd": 61000.0,
               "depth_down_usd": 68000.0, "spread_pct": 0.1}]  # fmt: skip
    out = render_dashboard(_data(rfq=rfq, venues=venues))
    tab = out.split('id="rfq"', 1)[1].split("</section>", 1)[0]
    assert "+6m PEAQ" in tab
    assert "<td>3.60</td>" in tab and "<td>—</td>" in tab


def test_an_unkeyed_rfq_says_so():
    out = render_dashboard(_data())
    assert "No RFQ keyed" in out
