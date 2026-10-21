"""
The proposal's success metrics, measured (the mandate scorecard).

Every metric gets a status, and the ones this tool cannot see say so —
a blank would read as "fine".
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from defi_tracker.analytics import PeriodPnL, PeriodRow, mandate_scorecard
from defi_tracker.core.storage import SCHEMA_VERSION, Storage

TODAY = date(2026, 9, 26)


def _by(rows):
    return {r["metric"].split(" (")[0]: r for r in rows}


def _period(kind="MTD", il="-30", fees="17"):
    row = PeriodRow("w", "peaq", "m", "k", "X", None, "2026-09-25", Decimal(0), Decimal(0),
                    Decimal(100), Decimal(0), Decimal(0), Decimal(fees), Decimal(il),
                    Decimal(0), frozenset(), frozenset(), False)  # fmt: skip
    return PeriodPnL(kind, date(2026, 9, 1), TODAY, [row], [])


def test_every_metric_has_a_status_and_nothing_is_blank():
    rows = mandate_scorecard([], [], [], [], [], TODAY)
    assert len(rows) == 6
    assert all(r["status"] in {"measured", "needs_input", "not_here", "not_yet"} for r in rows)
    assert all(r["note"] for r in rows)


def test_cost_of_liquidity_needs_every_active_contract_costed():
    c = {"status": "active", "annual_cost_usd": 120_000.0}
    uncosted = {"status": "active", "annual_cost_usd": None}
    assert (
        _by(mandate_scorecard([], [c, uncosted], [], [], [], TODAY))["Annual cost of liquidity"][
            "status"
        ]
        == "needs_input"
    )
    row = _by(mandate_scorecard([], [c, c], [], [], [], TODAY))["Annual cost of liquidity"]
    assert row["status"] == "measured" and "$240,000/yr" in row["now"]


def test_the_treasury_book_is_not_here_and_the_liquidity_book_is_labelled_as_such():
    """The proposal's return metric is the TREASURY book; the desk sees the
    liquidity book. Its figure is carried, labelled, never as the other."""
    row = _by(mandate_scorecard([_period()], [], [], [], [], TODAY))["Treasury book net return"]
    assert row["status"] == "not_here" and row["now"] == "—"
    assert "Liquidity book, excess vs HODL: MTD -$13" in row["note"]  # IL −30 + fees 17


def test_spread_and_depth_are_measured_elsewhere_not_here():
    row = _by(mandate_scorecard([], [], [], [], [], TODAY))["Spread and ±2% depth, tier-1 venues"]
    assert row["status"] == "not_here"


def test_the_report_metric_waits_for_its_first_due_month():
    """The first report is due for October 2026 (the proposal's Live reporting)."""
    rows = _by(mandate_scorecard([], [], [], [], [], TODAY))
    assert rows["Monthly desk P&L report"]["status"] == "not_yet"
    assert "first due for 2026-10" in rows["Monthly desk P&L report"]["note"]
    rows = _by(mandate_scorecard([], [], [], [], [], date(2026, 11, 5)))
    assert rows["Monthly desk P&L report"]["status"] == "measured"  # due, and none written
    rows = _by(
        mandate_scorecard([], [], [], [], [{"month": "2026-10", "final": True}], date(2026, 11, 5))
    )
    assert rows["Monthly desk P&L report"]["now"] == "1 final report(s), latest 2026-10"


def test_the_ledger_keeps_a_contracts_annual_cost(storage: Storage):
    assert SCHEMA_VERSION >= 10
    storage.record_contract("alpha", date(2026, 9, 1), now=1, counterparty="Alpha MM", kind="mm_loan",
                            status="active", annual_cost_usd=Decimal("180000"),
                            source_note="agreement §6")  # fmt: skip
    assert storage.contracts_as_of(TODAY)[0]["annual_cost_usd"] == 180000


def test_a_v9_ledger_gains_the_cost_column(storage: Storage):
    with storage.connect() as conn:
        conn.execute("ALTER TABLE contracts DROP COLUMN annual_cost_usd")
    storage.init_schema()
    with storage.connect() as conn:
        assert "annual_cost_usd" in {
            r["name"] for r in conn.execute("PRAGMA table_info(contracts)")
        }
