"""
Regression tests for two reporting defects found in QA:

  A. Closed positions with no withdraw on record were booked as total losses,
     so the headline "book net" was the residual of huge unverified items.
  B. Fee-by-month tables dropped months with no claim, and labelled the
     pending accrual with the current month instead of "since last sweep".

These are pure functions over plain dicts — no DB fixtures needed.
"""

from __future__ import annotations

import json
from decimal import Decimal

from defi_tracker.analytics import (
    last_sweep,
    month_range,
    realized_closed_pnl,
    rolled_up_months,
    unswept_months,
)

# ── helpers ───────────────────────────────────────────────────────────────

WALLET = "0xabc123def456abc123def456abc123def456abc1"


def flow(
    *,
    is_open: int = 0,
    net_invested_usd: float = 100.0,
    fees_usd: float = 0.0,
    withdraw_count: int | None = 1,
    pair_label: str = "USDT/ETH",
    position_key: str = "pos1",
    wallet: str = WALLET,
) -> dict:
    """A Storage.position_flows() row. withdraw_count=None omits the key
    entirely (legacy row), which is the defensive case."""
    row = {
        "wallet": wallet,
        "chain": "base",
        "protocol_id": "uniswap_v3",
        "position_key": position_key,
        "net_invested_usd": net_invested_usd,
        "fees_usd": fees_usd,
        "is_open": is_open,
        "pair_label": pair_label,
    }
    if withdraw_count is not None:
        row["withdraw_count"] = withdraw_count
    return row


def collect(ts: int, usd: float, *, withdrawal: bool = False, position: str = "") -> dict:
    row: dict = {"ts": ts, "usd_at_ts": usd, "wallet": WALLET}
    if position:
        row["position_key"] = position
    if withdrawal:
        row["meta_json"] = json.dumps({"netted_against_decrease": True})
    return row


# 2026-07-01 00:00:00 UTC
JUL_1 = 1782864000
DAY = 86400
HOUR = 3600

# A harvest shaped like a real monthly sweep: 15 collects across 15 positions
# on peaq + bsc within six minutes, $10,429.53 in total, and it contains its
# own dust — a $0.03 claim sits inside the batch.
JUL_HARVEST_USD = [
    1700.0000,
    1650.0000,
    740.0000,
    80.0000,
    35.0000,
    1900.0000,
    1200.0000,
    90.0000,
    450.0000,
    4.5000,
    160.0000,
    355.0000,
    1490.0000,
    575.0000,
    0.0300,
]
JUL_HARVEST_TOTAL = Decimal("10429.5300")


def harvest(start_ts: int, usds: list[float] | None = None, *, tag: str = "jul") -> list[dict]:
    """A realistic harvest batch: one collect per position, ~30s apart."""
    usds = JUL_HARVEST_USD if usds is None else usds
    return [collect(start_ts + i * 30, u, position=f"{tag}-pos{i}") for i, u in enumerate(usds)]


# ── Defect A: realized_closed_pnl ─────────────────────────────────────────


class TestRealizedClosedPnl:
    def test_verified_only_totals(self):
        rows = [
            # exited: deposited 100, withdrew 130 (net −30), fees 20 → +50
            flow(net_invested_usd=-30.0, fees_usd=20.0, withdraw_count=1, position_key="a"),
            # exited at a loss: net invested 80 written off, fees 10 → −70
            flow(net_invested_usd=80.0, fees_usd=10.0, withdraw_count=2, position_key="b"),
        ]
        r = realized_closed_pnl(rows)
        assert r["realized_usd"] == Decimal("-20")
        assert r["fees_usd"] == Decimal("30")
        assert r["positions"] == 2
        assert r["unverified_positions"] == 0
        assert r["unverified_usd"] == Decimal("0")
        assert r["unverified"] == []

    def test_unverified_row_excluded_and_surfaced(self):
        """The live case: a $25,000 Base deposit whose adapter is disabled.
        It must not land in realized_usd, and must be reported as missing."""
        rows = [
            flow(net_invested_usd=-30.0, fees_usd=20.0, withdraw_count=1, position_key="a"),
            flow(
                net_invested_usd=25000.0,
                fees_usd=0.0,
                withdraw_count=0,
                pair_label="WETH/USDC",
                position_key="ghost",
            ),
        ]
        r = realized_closed_pnl(rows)
        assert r["realized_usd"] == Decimal("50")  # unaffected by the ghost
        assert r["fees_usd"] == Decimal("20")
        assert r["positions"] == 1
        assert r["unverified_positions"] == 1
        assert r["unverified_usd"] == Decimal("25000")
        assert r["unverified"] == [
            {
                "pair_label": "WETH/USDC",
                "wallet": WALLET,
                "position_key": "ghost",
                "net_invested_usd": Decimal("25000"),
            }
        ]

    def test_unverified_sorted_by_size_desc(self):
        rows = [
            flow(net_invested_usd=100.0, withdraw_count=0, position_key="small"),
            flow(net_invested_usd=9000.0, withdraw_count=0, position_key="big"),
            flow(net_invested_usd=500.0, withdraw_count=0, position_key="mid"),
        ]
        r = realized_closed_pnl(rows)
        assert [u["position_key"] for u in r["unverified"]] == ["big", "mid", "small"]
        assert r["unverified_usd"] == Decimal("9600")

    def test_open_rows_ignored_entirely(self):
        rows = [
            flow(is_open=1, net_invested_usd=500.0, fees_usd=40.0, withdraw_count=0),
            flow(is_open=1, net_invested_usd=500.0, fees_usd=40.0, withdraw_count=3),
        ]
        r = realized_closed_pnl(rows)
        assert r["realized_usd"] == Decimal("0")
        assert r["positions"] == 0
        assert r["unverified_positions"] == 0  # open != unverified
        assert r["unverified_usd"] == Decimal("0")

    def test_missing_withdraw_count_key_is_unverified(self):
        """Legacy rows predate the field — absence of evidence of an exit is
        precisely the missing-data case, so it must not book as a loss."""
        rows = [flow(net_invested_usd=1000.0, fees_usd=5.0, withdraw_count=None)]
        r = realized_closed_pnl(rows)
        assert r["realized_usd"] == Decimal("0")
        assert r["fees_usd"] == Decimal("0")
        assert r["positions"] == 0
        assert r["unverified_positions"] == 1
        assert r["unverified_usd"] == Decimal("1000")

    def test_null_withdraw_count_is_unverified(self):
        rows = [flow(net_invested_usd=42.0, withdraw_count=None)]
        rows[0]["withdraw_count"] = None  # SQL NULL, not a missing key
        r = realized_closed_pnl(rows)
        assert r["unverified_positions"] == 1
        assert r["positions"] == 0

    def test_empty_input(self):
        r = realized_closed_pnl([])
        assert r == {
            "realized_usd": Decimal("0"),
            "fees_usd": Decimal("0"),
            "positions": 0,
            "unverified_positions": 0,
            "unverified_usd": Decimal("0"),
            "unverified": [],
        }

    def test_money_is_decimal(self):
        r = realized_closed_pnl(
            [
                flow(net_invested_usd=0.1, fees_usd=0.2, withdraw_count=1),
                flow(net_invested_usd=0.3, withdraw_count=0, position_key="u"),
            ]
        )
        assert isinstance(r["realized_usd"], Decimal)
        assert isinstance(r["fees_usd"], Decimal)
        assert isinstance(r["unverified_usd"], Decimal)
        assert isinstance(r["unverified"][0]["net_invested_usd"], Decimal)
        assert r["unverified_usd"] == Decimal("0.3")  # no float drift


# ── Defect B: month_range ─────────────────────────────────────────────────


class TestMonthRange:
    def test_same_month(self):
        assert month_range("2026-06", "2026-06") == ["2026-06"]

    def test_adjacent_months(self):
        assert month_range("2026-06", "2026-07") == ["2026-06", "2026-07"]

    def test_fills_the_gap_the_live_report_dropped(self):
        assert month_range("2026-06", "2026-08") == ["2026-06", "2026-07", "2026-08"]

    def test_year_rollover(self):
        assert month_range("2025-11", "2026-02") == [
            "2025-11",
            "2025-12",
            "2026-01",
            "2026-02",
        ]

    def test_december_to_january(self):
        assert month_range("2025-12", "2026-01") == ["2025-12", "2026-01"]

    def test_multi_year_span(self):
        r = month_range("2024-03", "2026-03")
        assert len(r) == 25
        assert r[0] == "2024-03"
        assert r[-1] == "2026-03"
        assert "2025-01" in r

    def test_reversed_input_is_empty(self):
        assert month_range("2026-08", "2026-06") == []
        assert month_range("2026-01", "2025-12") == []


# ── Defect B: last_sweep ──────────────────────────────────────────────────


class TestLastSweep:
    def test_picks_latest_and_sums_the_whole_batch(self):
        """A harvest is a multi-tx batch — the sweep is all of it, and the
        sweep reported is the most recent one."""
        rows = harvest(JUL_1 - 30 * DAY, tag="jun") + harvest(JUL_1)
        s = last_sweep(rows, now=JUL_1 + 44 * DAY)
        assert s is not None
        assert s["date"] == "2026-07-01"
        assert s["ts"] == JUL_1 + (len(JUL_HARVEST_USD) - 1) * 30
        assert abs(s["usd"] - JUL_HARVEST_TOTAL) < Decimal("0.01")
        assert isinstance(s["usd"], Decimal)

    def test_dust_collect_does_not_become_the_sweep(self):
        """The defect this guards: one $12 dust claim used to reset the sweep
        date, so a $7.2k accruing balance got footnoted '4d ago' and implied a
        fee run-rate an order of magnitude too high."""
        rows = harvest(JUL_1) + [collect(JUL_1 + 40 * DAY, 12.0, position="dust")]
        s = last_sweep(rows, now=JUL_1 + 44 * DAY)
        assert s is not None
        assert s["date"] == "2026-07-01"
        assert s["days_ago"] == 44
        assert abs(s["usd"] - JUL_HARVEST_TOTAL) < Decimal("0.01")

    def test_a_real_later_harvest_does_become_the_sweep(self):
        """The converse — the rule must not be so strict that it misses an
        actual harvest, or the date would freeze forever."""
        rows = harvest(JUL_1) + harvest(JUL_1 + 31 * DAY, tag="aug")
        s = last_sweep(rows, now=JUL_1 + 44 * DAY)
        assert s is not None
        assert s["date"] == "2026-08-01"
        assert s["days_ago"] == 13

    def test_size_alone_does_not_make_a_sweep(self):
        """A single large claim is not a harvest of the book. Judging on amount
        is what made the rule collapse when the book changed size — one big
        position paying out is one position, however much it pays."""
        rows = harvest(JUL_1) + [collect(JUL_1 + 40 * DAY, 9000.0, position="whale")]
        s = last_sweep(rows, now=JUL_1 + 44 * DAY, open_positions=15)
        assert s is not None
        assert s["date"] == "2026-07-01"

    def test_whole_book_harvest_counts_however_small(self):
        """The defect this replaces: a genuine harvest of a shrunken two-position
        book was rejected for being small against a $9.4k historical median, so
        last_sweep froze a month back and a 14-day accrual was reported over 45
        days. Coverage of the book is the signal, not the dollar amount."""
        rows = harvest(JUL_1) + harvest(JUL_1 + 40 * DAY, [200.0, 200.0], tag="aug")
        s = last_sweep(rows, now=JUL_1 + 44 * DAY, open_positions=2)
        assert s is not None
        assert s["date"] == "2026-08-10"
        assert s["usd"] == Decimal("400")

    def test_dust_is_not_a_sweep_even_on_a_small_book(self):
        """Coverage still has to mean coverage: one position out of ten is not
        a harvest, whatever the book size."""
        rows = harvest(JUL_1) + [collect(JUL_1 + 40 * DAY, 12.0, position="dust")]
        s = last_sweep(rows, now=JUL_1 + 44 * DAY, open_positions=10)
        assert s is not None
        assert s["date"] == "2026-07-01"

    def test_midnight_straddling_harvest_stays_one_sweep(self):
        """The real 2026-05-05 harvest ran past midnight into 05-06. Calendar-day
        bucketing reported only its tail; a time window keeps it whole."""
        start = JUL_1 + 22 * HOUR  # batch crosses into the next UTC day
        rows = harvest(start, [500.0, 400.0, 300.0]) + [
            collect(start + 3 * HOUR, 250.0, position="late-tail"),
        ]
        s = last_sweep(rows, now=JUL_1 + 10 * DAY)
        assert s is not None
        assert s["usd"] == Decimal("1450")  # whole batch, both sides of midnight
        assert s["date"] == "2026-07-02"  # dated by its last collect

    def test_ignores_fee_on_withdrawal_rows(self):
        rows = harvest(JUL_1) + [
            collect(JUL_1 + 20 * DAY, 5000.0, withdrawal=True, position="w"),  # not a sweep
        ]
        s = last_sweep(rows, now=JUL_1 + 44 * DAY)
        assert s is not None
        assert s["date"] == "2026-07-01"

    def test_withdrawal_fee_inside_the_sweep_window_is_not_counted(self):
        rows = harvest(JUL_1) + [collect(JUL_1 + 3600, 999.0, withdrawal=True, position="w")]
        s = last_sweep(rows, now=JUL_1)
        assert s is not None
        assert abs(s["usd"] - JUL_HARVEST_TOTAL) < Decimal("0.01")

    def test_days_ago_with_injected_now(self):
        """The live case: last sweep 2026-07-01, report run 2026-08-14."""
        s = last_sweep(harvest(JUL_1), now=JUL_1 + 44 * DAY)
        assert s is not None
        assert s["days_ago"] == 44

    def test_days_ago_same_day_is_zero(self):
        s = last_sweep(harvest(JUL_1), now=JUL_1 + 3600)
        assert s is not None
        assert s["days_ago"] == 0

    def test_defaults_to_now_when_not_injected(self):
        s = last_sweep(harvest(JUL_1))
        assert s is not None
        assert s["days_ago"] > 0

    def test_none_on_empty_input(self):
        assert last_sweep([], now=JUL_1) is None

    def test_none_when_all_rows_are_withdrawal_fees(self):
        rows = [
            collect(JUL_1, 100.0, withdrawal=True),
            collect(JUL_1 + DAY, 200.0, withdrawal=True),
        ]
        assert last_sweep(rows, now=JUL_1 + 10 * DAY) is None


class TestUnsweptMonths:
    """Months whose fees are earned but not yet harvested must render as
    pending, never as $0 — an explicit zero is a stronger false claim than the
    missing row it replaced, and attribution_month only rolls back one month so
    a late sweep would leave the skipped month at $0 permanently."""

    def test_live_case_july_and_august_are_unswept(self):
        # Last real harvest 2026-07-01 (June's accrual); report run 2026-08-14.
        rows = harvest(JUL_1)
        assert unswept_months(rows, now=JUL_1 + 44 * DAY) == ["2026-07", "2026-08"]

    def test_current_month_is_unswept_even_right_after_a_harvest(self):
        """A 2026-08-01 harvest sweeps JULY's accrual (attribution_month rolls a
        day<=7 claim back one month). August's own fees start accruing at once,
        so the current month is outstanding from the moment the sweep lands —
        it must render pending, not $0."""
        aug_1 = JUL_1 + 31 * DAY
        assert unswept_months(harvest(aug_1, tag="aug"), now=aug_1 + 2 * DAY) == ["2026-08"]

    def test_dust_does_not_shrink_the_unswept_range(self):
        """Falls straight out of the last_sweep defect: if dust reset the sweep
        date, July would silently drop out of the unswept set and render $0."""
        rows = harvest(JUL_1) + [collect(JUL_1 + 40 * DAY, 12.0, position="dust")]
        assert unswept_months(rows, now=JUL_1 + 44 * DAY) == ["2026-07", "2026-08"]

    def test_never_swept_spans_the_whole_collect_history(self):
        """Fee-on-withdrawal collects are never harvests, so a book that has
        only ever released fees on withdrawal has swept nothing and every month
        on record is outstanding.

        Note this is the only true 'never swept' case: sweep materiality is
        measured as a share of the book's OWN harvest scale, so a history made
        entirely of dust reads as a book whose harvests happen to be small —
        deliberately, so the rule travels to a smaller book — not as no
        harvests at all."""
        rows = [
            collect(JUL_1, 5.0, position="w1", withdrawal=True),
            collect(JUL_1 + 40 * DAY, 6.0, position="w2", withdrawal=True),
        ]
        assert unswept_months(rows, now=JUL_1 + 44 * DAY) == ["2026-06", "2026-07", "2026-08"]

    def test_no_collect_history_claims_nothing(self):
        assert unswept_months([], now=JUL_1) == []


class TestRolledUpMonths:
    """A skipped monthly sweep leaves a month with no claims of its own, even
    though its fees WERE harvested — inside the catch-up sweep. Rendering that
    month as $0 states the opposite of what happened and never self-corrects,
    because attribution_month only rolls a claim back one month.

    The live shape: sweeps on 2026-07-01 (accrual June) and 2026-09-01 (accrual
    August), 62 days apart. July's fees are inside the September sweep.
    """

    def test_the_live_skipped_month_case(self):
        rows = harvest(JUL_1) + harvest(JUL_1 + 62 * DAY, tag="sep")
        assert rolled_up_months(rows, open_positions=15) == {"2026-07": "2026-08"}

    def test_monthly_cadence_has_no_gaps(self):
        rows = (
            harvest(JUL_1, tag="jul")
            + harvest(JUL_1 + 31 * DAY, tag="aug")
            + harvest(JUL_1 + 62 * DAY, tag="sep")
        )
        assert rolled_up_months(rows, open_positions=15) == {}

    def test_a_longer_gap_maps_every_skipped_month(self):
        # Sweeps 2026-07-01 (June) and 2026-11-01 (October): Jul, Aug, Sep skipped.
        rows = harvest(JUL_1) + harvest(JUL_1 + 123 * DAY, tag="nov")
        assert rolled_up_months(rows, open_positions=15) == {
            "2026-07": "2026-10",
            "2026-08": "2026-10",
            "2026-09": "2026-10",
        }

    def test_months_before_the_first_sweep_are_left_alone(self):
        """No earlier boundary exists to measure a gap against, so claiming a
        later sweep covered them would be a guess."""
        rows = harvest(JUL_1)
        assert rolled_up_months(rows, open_positions=15) == {}

    def test_crosses_a_year_boundary(self):
        # 2026-12-01 (accrual Nov) then 2027-02-01 (accrual Jan) → Dec skipped.
        dec_1 = 1_796_083_200  # 2026-12-01 00:00 UTC
        rows = harvest(dec_1, tag="dec") + harvest(dec_1 + 62 * DAY, tag="feb")
        assert rolled_up_months(rows, open_positions=15) == {"2026-12": "2027-01"}

    def test_dust_does_not_count_as_a_sweep_boundary(self):
        """Falls out of sweep_batches: if a dust claim counted, it would create
        a false boundary and hide the real gap."""
        rows = (
            harvest(JUL_1)
            + [collect(JUL_1 + 40 * DAY, 12.0, position="dust")]
            + harvest(JUL_1 + 62 * DAY, tag="sep")
        )
        assert rolled_up_months(rows, open_positions=15) == {"2026-07": "2026-08"}

    def test_no_sweeps_at_all(self):
        assert rolled_up_months([], open_positions=15) == {}
