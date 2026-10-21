"""
Monthly mandate report (Phase 2.4).

A report is a record of what was reported, so a final one is written once.
Within it: yield is stated against the HODL benchmark, unset limits read as
NOT SET, an unkeyed ledger reads as unknown exposure, and every excluded
position is listed with the value it leaves out.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, EventKind
from defi_tracker.mandate import (
    ReportExists,
    gather_mandate_report,
    render_mandate_report,
    report_path,
    write_mandate_report,
)
from defi_tracker.policy import Policy, parse_policy
from tests.conftest import TEST_WALLET


def _ts(d: date, hour: int = 12) -> int:
    return int(datetime.combine(d, time(hour), tzinfo=UTC).timestamp())


@pytest.fixture
def august(storage: Storage, make_position, make_event) -> Storage:
    """One position through August: 1000 at the start, 1100 at the end with
    IL −20 → −50 and a 15 collect in between."""
    storage.upsert_events(
        [
            make_event(kind=EventKind.DEPOSIT, ts=_ts(date(2026, 1, 5)), usdt_amount=Decimal("1000"),
                       weth_amount=Decimal("0"), position_key="p1", tx_hash="0xa"),
            make_event(kind=EventKind.COLLECT, ts=_ts(date(2026, 8, 3)), usdt_amount=Decimal("15"),
                       weth_amount=Decimal("0"), position_key="p1", tx_hash="0xb"),
        ]
    )  # fmt: skip
    for day, value, unc, il in (
        (date(2026, 7, 31), "1000", "10", "-20"),
        (date(2026, 8, 31), "1100", "12", "-50"),
    ):
        pos = make_position(
            current_value_usd=Decimal(value), unclaimed_usd=Decimal(unc), position_key="p1"
        )
        pos.snapshot_at = _ts(day)
        v, i = Decimal(value), Decimal(il)
        storage.write_snapshot(pos, snapshot_date=day, il_usd=i, hold_value_usd=v - i)
    storage.record_pipeline_health(
        TEST_WALLET, Chain.BSC, "pancake_infinity", now=_ts(date(2026, 9, 1)),
        sync_ok=True, snapshot_ok=True, source="subgraph", error=None,
    )  # fmt: skip
    return storage


def _gather(st: Storage, policy: Policy | None = None, today=date(2026, 9, 25)):
    rep = gather_mandate_report(st, 2026, 8, policy or Policy(), today=today)
    assert rep is not None
    return rep


def test_a_closed_month_with_marks_to_its_last_day_is_final(august):
    rep = _gather(august)
    assert rep.complete and rep.end == date(2026, 8, 31)


def test_a_month_in_progress_is_provisional(august):
    rep = _gather(august, today=date(2026, 8, 31))
    assert not rep.complete
    assert "PROVISIONAL" in render_mandate_report(rep)
    assert report_path(rep).name == "mandate-2026-08.provisional.html"


def test_no_marks_in_the_month_is_nothing_to_report(storage):
    assert gather_mandate_report(storage, 2026, 8, Policy()) is None


def test_yield_is_stated_against_the_hodl_benchmark(august):
    html = render_mandate_report(_gather(august))
    # market = (1100 − 1000) − (−30) = 130 · IL −30 · fees 15 + 2 = 17 · excess = −13
    assert "HODL of the deposited tokens" in html
    assert "$130" in html and "-$30" in html and "$17" in html and "-$13" in html


def test_unset_limits_and_an_unkeyed_ledger_are_said_not_implied(august):
    html = render_mandate_report(_gather(august))
    assert "13 limit(s) <strong>not set</strong>" in html
    assert "counterparty exposure is unknown, not zero" in html


def test_breaches_and_exceptions_reach_the_report(august):
    august.reconcile_policy_exceptions(
        [
            {"uid": "policy:risk.max_il_pct:x", "limit_id": "risk.max_il_pct", "subject": "x",
             "unit": "%", "observed": Decimal("30"), "threshold": Decimal("25"), "status": "breach"}
        ],
        now=_ts(date(2026, 8, 10)),
    )  # fmt: skip
    policy = parse_policy({"concentration": {"max_position_usd": 500}})
    html = render_mandate_report(_gather(august, policy))
    assert "BREACH" in html
    assert "1 unreviewed" in html


def test_excluded_positions_are_listed_with_their_value(august, make_position):
    pos = make_position(current_value_usd=Decimal("777"), position_key="p2")
    pos.snapshot_at = _ts(date(2026, 8, 31))
    august.write_snapshot(pos, snapshot_date=date(2026, 8, 31))  # no events behind it
    html = render_mandate_report(_gather(august))
    assert "no event history — flows unknowable" in html
    assert "$777" in html


def test_contracts_near_expiry_are_flagged(august):
    august.record_contract(
        "alpha", date(2026, 8, 1), now=1, counterparty="Alpha MM", kind="mm_loan", status="active",
        usd_value=Decimal("250000"), end_date="2026-11-15", source_note="agreement §4",
    )  # fmt: skip
    rep = _gather(august)
    assert rep.exposure == {"Alpha MM": Decimal("250000")}
    assert "<strong>2026-11-15 (+76d)</strong>" in render_mandate_report(rep)


def test_a_final_report_is_written_once(august, tmp_path):
    rep = _gather(august)
    path = write_mandate_report(rep, tmp_path)
    assert path.name == "mandate-2026-08.html"
    with pytest.raises(ReportExists):
        write_mandate_report(rep, tmp_path)
    write_mandate_report(rep, tmp_path, force=True)


def test_a_provisional_report_may_be_replaced(august, tmp_path):
    rep = _gather(august, today=date(2026, 8, 31))
    write_mandate_report(rep, tmp_path)
    write_mandate_report(rep, tmp_path)  # no error


def test_depeg_is_not_assessed_for_a_past_month(august):
    policy = parse_policy({"depeg": {"max_deviation_bps": 50, "coins": {"USDC": "usd-coin"}}})
    rep = gather_mandate_report(
        august, 2026, 8, policy, today=date(2026, 9, 25), depeg_prices={"USDC": Decimal("0.9")}
    )
    assert rep is not None
    (check,) = [c for c in rep.checks if c.limit_id == "depeg.max_deviation_bps"]
    assert check.status == "unknown"  # today's price says nothing about August's peg
