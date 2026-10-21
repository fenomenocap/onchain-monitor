"""
Contractual ledger (Phase 2.6).

The layer carrying the November decision, and the only one no API can fill.
So a figure is only accepted with a source, a change is a new version rather
than an edit, and a contract whose USD value was never keyed makes its
counterparty's exposure unknown — not smaller.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from defi_tracker.analytics import counterparty_exposure, policy_checks
from defi_tracker.core.storage import SCHEMA_VERSION, Storage
from defi_tracker.policy import parse_policy

ON = date(2026, 9, 25)


def _record(st: Storage, ref="alpha-loan", as_of=date(2026, 9, 1), **over) -> None:
    fields = {
        "counterparty": "Alpha MM",
        "kind": "mm_loan",
        "status": "active",
        "asset": "PEAQ",
        "amount": Decimal("1000000"),
        "usd_value": Decimal("250000"),
        "start_date": "2024-11-01",
        "end_date": "2026-11-15",
        "source_note": "Alpha MM agreement §1",
    }
    fields.update(over)
    st.record_contract(ref, as_of, now=1, **fields)


def test_schema_has_the_v9_ledger(storage: Storage):
    assert SCHEMA_VERSION >= 9
    _record(storage)
    assert storage.contracts_as_of(ON)[0]["amount"] == Decimal("1000000")


@pytest.mark.parametrize("missing", ["source_note", "counterparty", "kind", "status"])
def test_a_contract_needs_its_source_and_identity(storage: Storage, missing):
    with pytest.raises(ValueError, match=missing):
        _record(storage, **{missing: None})


def test_a_change_is_a_new_version_and_history_is_kept(storage: Storage):
    _record(storage, as_of=date(2026, 9, 1), usd_value=Decimal("250000"))
    _record(storage, as_of=date(2026, 9, 20), usd_value=Decimal("310000"))
    assert [r["usd_value"] for r in storage.contract_history("alpha-loan")] == [250000, 310000]
    assert storage.contracts_as_of(date(2026, 9, 10))[0]["usd_value"] == 250000
    assert storage.contracts_as_of(ON)[0]["usd_value"] == 310000


def test_contracts_list_by_expiry(storage: Storage):
    _record(storage, ref="a", end_date="2027-01-01")
    _record(storage, ref="b", end_date="2026-11-15")
    _record(storage, ref="c", end_date=None)
    assert [r["ref"] for r in storage.contracts_as_of(ON)] == ["b", "a", "c"]


def test_exposure_sums_active_contracts_in_force(storage: Storage):
    _record(storage, ref="loan", usd_value=Decimal("250000"))
    _record(storage, ref="option", kind="mm_option", usd_value=Decimal("50000"))
    _record(storage, ref="old", end_date="2026-01-01", usd_value=Decimal("999"))
    _record(storage, ref="gone", status="terminated", usd_value=Decimal("999"))
    _record(storage, ref="future", start_date="2027-01-01", usd_value=Decimal("999"))
    assert counterparty_exposure(storage.contracts_as_of(ON), ON) == {"Alpha MM": Decimal("300000")}


def test_one_unvalued_contract_makes_the_counterparty_unknown(storage: Storage):
    _record(storage, ref="loan", usd_value=Decimal("250000"))
    _record(storage, ref="option", kind="mm_option", usd_value=None)
    _record(storage, ref="beta", counterparty="Beta MM", usd_value=Decimal("10"))
    exposure = counterparty_exposure(storage.contracts_as_of(ON), ON)
    assert exposure == {"Alpha MM": None, "Beta MM": Decimal("10")}


def test_ledger_feeds_counterparty_limits():
    policy = parse_policy({"counterparty": {"max_exposure_usd": 200000, "caps": {"Gamma MM": 1}}})
    row = {"current_value_usd": 1_000_000, "pair_label": "x", "chain": "peaq", "wallet": "0xw",
           "protocol_id": "machinex", "position_key": "1"}  # fmt: skip
    checks = policy_checks(
        [row], policy, counterparty_exposure={"Alpha MM": Decimal("300000"), "Beta MM": None}
    )
    got = {(c.limit_id, c.subject): c.status for c in checks if c.limit_id.startswith("counter")}
    assert got[("counterparty.max_exposure_usd", "Alpha MM")] == "breach"
    assert got[("counterparty.max_exposure_usd", "Beta MM")] == "unknown"
    assert got[("counterparty.caps", "Gamma MM")] == "unknown"  # a cap with nothing keyed


def test_an_empty_ledger_is_unkeyed_not_zero_exposure():
    policy = parse_policy({"counterparty": {"max_exposure_usd": 1}})
    (check,) = [c for c in policy_checks([], policy, counterparty_exposure={})
                if c.limit_id == "counterparty.max_exposure_usd"]  # fmt: skip
    assert check.status == "unknown"


# ── PEAQ now, and the ledger marked at it ────────────────────────────────


def _pool(value, stable, peaq, ts=1_791_850_000, symbol="PEAQ", key="bsc:0xpeaq"):
    import json as _json

    return {
        "current_value_usd": value,
        "snapshot_ts": ts,
        "is_closed": 0,
        "current_balances_json": _json.dumps(
            [
                {"token_key": "bsc:0xusdc", "symbol": "USDC", "amount": str(stable)},
                {"token_key": key, "symbol": symbol, "amount": str(peaq)},
            ]
        ),
    }


def test_peaq_mark_reads_the_book_pools_weighted_by_peaq_held():
    from decimal import Decimal

    from defi_tracker.analytics import peaq_mark

    m = peaq_mark([_pool(140, 100, 1000), _pool(1300, 1000, 3000, ts=1_791_860_000)])
    # (40 + 300) / 4000 PEAQ
    assert m.pool_price == Decimal("0.085") and m.pool_ts == 1_791_860_000
    assert m.source == "book pools" and m.price == m.pool_price


def test_peaq_mark_prefers_the_market_read_and_keeps_the_pools_as_a_cross_check():
    from decimal import Decimal

    from defi_tracker.analytics import peaq_mark

    m = peaq_mark([_pool(140, 100, 1000)], {"bsc:0xpeaq": Decimal("0.044")}, 1_791_870_000)
    assert m.source == "market" and m.price == Decimal("0.044") and m.ts == 1_791_870_000
    assert m.pool_price == Decimal("0.04")
    assert round(m.gap_pct, 1) == 10.0


def test_peaq_mark_takes_the_true_median_of_two_market_reads():
    from decimal import Decimal

    from defi_tracker.analytics import peaq_mark

    pools = [_pool(140, 100, 1000), _pool(140, 100, 1000, symbol="WPEAQ", key="peaq:0xw")]
    m = peaq_mark(pools, {"bsc:0xpeaq": Decimal("0.09"), "peaq:0xw": Decimal("0.05")}, 1)
    assert m.price == Decimal("0.07")


def test_wstpeaq_never_sets_the_peaq_mark():
    """Liquid-staked PEAQ trades at its own rate; it is exposure, not price."""
    from defi_tracker.analytics import peaq_mark

    assert peaq_mark([_pool(140, 100, 1000, symbol="wstPEAQ")]).price is None


def test_peaq_mark_is_unknown_without_a_pool_or_a_market_price():
    from defi_tracker.analytics import peaq_mark

    m = peaq_mark([], {})
    assert m.price is None and m.source == "none"


def test_contract_marks_values_peaq_at_the_mark_and_leaves_other_assets_unknown():
    from datetime import date
    from decimal import Decimal

    from defi_tracker.analytics import PeaqMark, contract_marks

    mark = PeaqMark(Decimal("0.04"), 1, "market", None, None, Decimal("0.04"), 1)
    rows = contract_marks(
        [
            {
                "ref": "c",
                "status": "active",
                "asset": "PEAQ",
                "amount": Decimal("1000000"),
                "end_date": "2026-11-01",
                "start_date": None,
            },
            {
                "ref": "c",
                "status": "active",
                "asset": "BTC",
                "amount": Decimal("1"),
                "end_date": None,
                "start_date": None,
            },
            {
                "ref": "c",
                "status": "expired",
                "asset": "PEAQ",
                "amount": Decimal("5"),
                "end_date": None,
                "start_date": None,
            },
            {
                "ref": "c",
                "status": "active",
                "asset": "PEAQ",
                "amount": Decimal("5"),
                "end_date": "2026-10-01",
                "start_date": None,
            },
        ],  # fmt: skip
        mark,
        date(2026, 10, 13),
    )
    assert len(rows) == 2  # the expired and the lapsed contract are out
    assert rows[0]["usd_now"] == Decimal("40000") and rows[0]["days_left"] == 19
    assert rows[1]["usd_now"] is None and rows[1]["days_left"] is None


def test_an_exercised_tranche_comes_off_what_the_mm_owes_back(storage):
    """Tranches round-trip through storage, and PEAQ sold to the MM by an
    exercise is no longer owed back — the loan is marked on the rest."""
    from datetime import date
    from decimal import Decimal

    from defi_tracker.analytics import PeaqMark, contract_marks

    d = date(2026, 10, 13)
    storage.record_contract(
        "beta", d, 1, counterparty="Beta MM", kind="mm_loan", status="active", asset="PEAQ",
        amount=Decimal("30000000"), source_note="MSA p.1",
    )  # fmt: skip
    storage.record_tranche(
        "beta", "2", d, 1, asset="PEAQ", amount=Decimal("3000000"), strike_usd=Decimal("0.30"),
        status="exercised", exercised_on="2025-01-01", source_note="confirmation",
    )  # fmt: skip
    storage.record_tranche(
        "beta", "4", d, 1, asset="PEAQ", amount=Decimal("6000000"), strike_usd=None,
        strike_rule="150% of TWAP", status="open", source_note="confirmation",
    )  # fmt: skip
    tranches = storage.tranches_as_of(d)
    assert [t["tranche"] for t in tranches] == ["2", "4"]
    assert tranches[0]["strike_usd"] == Decimal("0.30") and tranches[1]["strike_usd"] is None
    mark = PeaqMark(Decimal("0.04"), 1, "market", None, None, Decimal("0.04"), 1)
    (row,) = contract_marks(storage.contracts_as_of(d), mark, d, tranches)
    assert row["outstanding"] == Decimal("27000000")
    assert row["usd_now"] == Decimal("1080000")
    exercised = row["tranches"][0]
    assert exercised["in_money"] is None and exercised["strike_usd_total"] == Decimal("900000")


def test_returned_peaq_is_not_owed_and_only_open_tranches_mark_today(storage):
    """A loan where part was bought and part handed back: 30m lent, 3m bought,
    5m returned → 22m owed. Today's P&L marks only the open calls; the past
    exercise and the lapsed tranches stay out of it."""
    from datetime import date
    from decimal import Decimal

    from defi_tracker.analytics import PeaqMark, contract_marks

    d = date(2026, 10, 19)
    storage.record_contract(
        "beta", d, 1, counterparty="Beta MM", kind="mm_loan", status="active", asset="PEAQ",
        amount=Decimal("30000000"), returned=Decimal("5000000"), source_note="MLA + email",
    )  # fmt: skip
    for n, amt, k, status in (("1", "3000000", "0.50", "lapsed"), ("2", "3000000", "0.30", "exercised"),
                              ("3", "6000000", "0.45", "lapsed"), ("6", "8000000", "0.02", "open")):  # fmt: skip
        storage.record_tranche(
            "beta", n, d, 1, asset="PEAQ", amount=Decimal(amt), strike_usd=Decimal(k),
            status=status, source_note="confirmation",
        )  # fmt: skip
    mark = PeaqMark(Decimal("0.04"), 1, "market", None, None, Decimal("0.04"), 1)
    (row,) = contract_marks(storage.contracts_as_of(d), mark, d, storage.tranches_as_of(d))
    assert row["returned"] == Decimal("5000000")
    assert row["outstanding"] == Decimal("22000000")
    assert row["mm_pnl_usd"] == Decimal("160000")  # (0.04 − 0.02) × 8m, open call only


def test_a_tranche_needs_its_contract_and_a_known_status(storage):
    from datetime import date
    from decimal import Decimal

    import pytest

    d = date(2026, 10, 13)
    with pytest.raises(ValueError, match="key the contract first"):
        storage.record_tranche(
            "nope", "1", d, 1, asset="PEAQ", amount=Decimal(1), status="open", source_note="x"
        )
    storage.record_contract(
        "c", d, 1, counterparty="X", kind="mm_loan", status="active", source_note="x"
    )
    with pytest.raises(ValueError, match="open \\| exercised \\| lapsed"):
        storage.record_tranche(
            "c", "1", d, 1, asset="PEAQ", amount=Decimal(1), status="pending", source_note="x"
        )


def test_an_unfixed_strike_with_bounds_is_a_range_against_spot_never_a_point(storage):
    """A strike that is a formula the contract leaves unpriced; its bounds
    give a moneyness range, and only the far bound can make it in the money."""
    from datetime import date
    from decimal import Decimal

    from defi_tracker.analytics import tranche_marks

    d = date(2026, 10, 13)
    storage.record_contract(
        "a", d, 1, counterparty="Gamma MM", kind="mm_loan", status="active", source_note="x"
    )
    storage.record_tranche(
        "a", "1", d, 1, asset="PEAQ", amount=Decimal("10000000"), status="open",
        strike_low_usd=Decimal("0.06"), strike_high_usd=Decimal("0.08"), source_note="x",
    )  # fmt: skip
    (t,) = tranche_marks(storage.tranches_as_of(d), Decimal("0.04"))
    assert t["strike_x_spot"] is None
    assert [round(x, 2) for x in t["strike_x_range"]] == [1.5, 2.0]
    assert t["in_money"] is False and t["mm_gain_usd"] == 0  # below every strike allowed
    (t,) = tranche_marks(storage.tranches_as_of(d), Decimal("0.07"))
    assert t["in_money"] is None and t["mm_gain_usd"] is None  # inside the bounds: unknown
    (t,) = tranche_marks(storage.tranches_as_of(d), Decimal("0.09"))
    assert t["in_money"] is True  # above even the high bound


def test_a_renewal_notice_deadline_is_on_the_calendar_even_once_passed():
    """A renewable contract's notice date is the decision; once it has passed
    while the term is still running, the brief says to act now."""
    from datetime import date

    from defi_tracker.analytics import mm_calendar

    c = {"counterparty": "Beta MM", "notice_by": "2026-10-01", "end_date": "2026-11-01",
         "days_left": 26, "outstanding": None, "tranches": []}  # fmt: skip
    rows = mm_calendar([c], date(2026, 10, 6))
    notice = rows[0]
    assert notice["what"] == "Beta MM notice to stop renewal" and notice["days"] == -5
    assert "send written notice now" in notice["detail"]
    assert rows[1]["what"] == "Beta MM loan ends"


def test_an_exercised_tranche_reports_pnl_at_exercise_and_today_as_the_operators_sheet():
    """A tranche of 3m exercised at $0.30 when PEAQ traded at $0.50 → peaq
    −$600,000 at exercise; at $0.02, +$840,000 today."""
    from decimal import Decimal

    from defi_tracker.analytics import tranche_marks

    t = {"ref": "k", "tranche": "2", "asset": "PEAQ", "amount": Decimal("3000000"),
         "strike_usd": Decimal("0.30"), "status": "exercised",
         "exercise_price_usd": Decimal("0.50")}  # fmt: skip
    (row,) = tranche_marks([t], Decimal("0.02"))
    assert -row["mm_gain_at_exercise_usd"] == Decimal("-600000")
    assert -row["mm_gain_usd"] == Decimal("840000")
