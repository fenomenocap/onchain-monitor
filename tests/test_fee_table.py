"""
The shared fee table (analytics.fee_table).

Two facts the old per-surface tables got wrong, pinned here:
- a sweep that carried two months is SPLIT by month-end unclaimed quantities,
  priced at the claim so the months still add up to the recorded claim;
- a chain whose event feed stopped shows unknown / at-least, never "—" (zero).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from decimal import Decimal

from defi_tracker.analytics import attribute_fee_months, fee_table, split_rolled_sweeps

W = "0xw"
TK = "peaq:0xtoken"


def _ts(d: date, hour: int = 10) -> int:
    return int(datetime.combine(d, time(hour), tzinfo=UTC).timestamp())


def _collect(d: date, pk: str, qty: str, price: str = "2", chain: str = "peaq") -> dict:
    q, p = Decimal(qty), Decimal(price)
    return {
        "month": d.strftime("%Y-%m"),
        "ts": _ts(d),
        "wallet": W,
        "chain": chain,
        "protocol_id": "machinex",
        "position_key": pk,
        "amounts_json": json.dumps([{"token_key": TK, "amount": qty, "price_at_ts": price}]),
        "usd_at_ts": float(q * p),
        "meta_json": None,
    }


def _unc(d: date, pk: str, qty: str, usd: str, known: int = 1, hour: int = 12) -> dict:
    return {
        "wallet": W,
        "chain": "peaq",
        "protocol_id": "machinex",
        "position_key": pk,
        "snapshot_date": d.isoformat(),
        "snapshot_ts": _ts(d, hour),
        "unclaimed_usd": float(usd),
        "unclaimed_is_known": known,
        "unclaimed_balances_json": json.dumps([{"token_key": TK, "amount": qty}]),
        "is_closed": 0,
    }


PKS = ("a", "b", "c")  # three positions, so each batch reads as a whole-book sweep


def _book(july_qty: str = "40", aug_extra: str = "60"):
    """Sweeps on Jul 1 (June) and Sep 1 (Jul + Aug), July skipped."""
    rows, series = [], []
    for pk in PKS:
        rows.append(_collect(date(2026, 7, 1), pk, "10"))
        total = Decimal(july_qty) + Decimal(aug_extra)
        rows.append(_collect(date(2026, 9, 1), pk, str(total), price="3"))
        series.append(_unc(date(2026, 7, 1), pk, "0", "0"))  # just after the Jul 1 sweep
        series.append(_unc(date(2026, 7, 31), pk, july_qty, str(Decimal(july_qty) * 1)))
    return rows, series


def test_a_skipped_month_is_split_by_month_end_quantities_at_claim_price():
    rows, series = _book()
    out, info = split_rolled_sweeps(attribute_fee_months(rows), series, open_positions=3)
    by_month: dict[str, Decimal] = {}
    for r in out:
        by_month[r["month"]] = by_month.get(r["month"], Decimal("0")) + Decimal(str(r["usd_at_ts"]))
    assert by_month["2026-07"] == Decimal("360")  # 3 × 40 × $3 claim price
    assert by_month["2026-08"] == Decimal("540")  # 3 × 60 × $3
    assert sum(by_month.values()) == sum(Decimal(str(r["usd_at_ts"])) for r in rows)
    assert info["2026-07"]["month_end_usd"] == Decimal("120")  # at July's own price
    assert info["2026-07"]["positions_split"] == 3


def test_a_missing_month_end_leaves_the_position_unsplit():
    rows, series = _book()
    series = [
        s for s in series if not (s["position_key"] == "a" and s["snapshot_date"] == "2026-07-31")
    ]
    _, info = split_rolled_sweeps(attribute_fee_months(rows), series, open_positions=3)
    assert (info["2026-07"]["positions_split"], info["2026-07"]["positions_unsplit"]) == (2, 1)


def test_an_unreadable_month_end_is_not_split_against():
    rows, series = _book()
    for s in series:
        if s["snapshot_date"] == "2026-07-31":
            s["unclaimed_is_known"] = 0
    out, info = split_rolled_sweeps(attribute_fee_months(rows), series, open_positions=3)
    assert info["2026-07"]["positions_split"] == 0
    assert not [r for r in out if r["month"] == "2026-07"]


def test_a_collect_between_the_sweeps_blocks_the_split():
    rows, series = _book()
    rows.append(_collect(date(2026, 7, 20), "a", "5"))
    _, info = split_rolled_sweeps(attribute_fee_months(rows), series, open_positions=3)
    assert info["2026-07"]["positions_unsplit"] == 1


def test_more_unclaimed_than_was_claimed_is_inconsistent_and_not_split():
    rows, series = _book(july_qty="200", aug_extra="-150")
    _, info = split_rolled_sweeps(attribute_fee_months(rows), series, open_positions=3)
    assert info["2026-07"]["positions_split"] == 0


def _table(rows, series, health, now=date(2026, 9, 25)):
    active = {(W, "peaq"), (W, "bsc")}
    return fee_table(rows, snaps=[], unclaimed_series=series, health_rows=health, active=active,
                     now=_ts(now))  # fmt: skip


def _health(chain: str, ok: date | None) -> dict:
    return {"wallet": W, "chain": chain, "protocol_id": "x",
            "last_sync_ok_at": _ts(ok) if ok else None}  # fmt: skip


def test_the_table_carries_the_split_month_and_says_how():
    rows, series = _book()
    t = _table(rows, series, [_health("peaq", date(2026, 9, 25))])
    july = next(r for r in t.rows if r.label == "2026-07")
    assert july.status == "split" and july.claimed == Decimal("360")
    assert "month-end unclaimed quantities" in july.note


def test_a_stopped_feed_reads_unknown_or_at_least_never_zero():
    rows, series = _book()
    rows.append(_collect(date(2026, 7, 1), "z", "7", chain="bsc"))  # June, on bsc
    t = _table(rows, series, [_health("peaq", date(2026, 9, 25)), _health("bsc", date(2026, 7, 6))])
    by = {r.label: r for r in t.rows}
    assert "bsc" in by["2026-06"].partial  # claims seen, window not fully covered
    assert "bsc" in by["2026-08"].unknown  # nothing seen, window not covered at all
    assert "bsc" in t.lifetime.partial
    assert any("bsc: collects known only through 2026-07-06" in n for n in t.notes)
    assert any("(event sync stopped)" in n for n in t.notes if n.startswith("bsc"))


def test_a_healthy_feed_in_a_window_still_open_is_a_floor_but_not_called_stopped():
    """Fees claimed on 09-30 count for September, and a sweep up to 10-07
    would too — so September is a floor until then. That is an open window,
    not a broken sync, and the note must say which."""
    rows, series = _book()
    rows.append(_collect(date(2026, 9, 30), "z", "5"))
    now = date(2026, 10, 1)
    t = _table(rows, series, [_health("peaq", now)], now=now)
    (note,) = [n for n in t.notes if n.startswith("peaq")]
    assert "window is open until 2026-10-07" in note and "stopped" not in note


def test_a_wallet_that_never_touched_a_chain_does_not_make_it_stale():
    rows, series = _book()
    health = [_health("peaq", date(2026, 9, 25)),
              {"wallet": "0xother", "chain": "peaq", "protocol_id": "x", "last_sync_ok_at": None}]  # fmt: skip
    t = _table(rows, series, health)
    assert not any(r.unknown or r.partial for r in t.rows)


def test_the_page_renders_the_split_july_row_it_used_to_drop():
    from defi_tracker.report import ReportData
    from defi_tracker.web import render_dashboard

    rows, series = _book()
    t = _table(rows, series, [_health("peaq", date(2026, 9, 25)), _health("bsc", date(2026, 7, 6))])
    snap = {"snapshot_date": "2026-09-25", "wallet": W, "chain": "peaq", "protocol_id": "machinex",
            "position_key": "a", "pair_label": "WPEAQ/USDT", "current_value_usd": 1.0,
            "unclaimed_usd": 0.0, "unclaimed_is_known": 1, "cost_basis_usd": 1.0,
            "hold_value_usd": 1.0, "il_usd": 0.0, "il_pct": 0.0, "pnl_usd": 0.0, "in_range": 1,
            "tick_lower": -1, "tick_upper": 1, "tick_current": 0}  # fmt: skip
    html = render_dashboard(ReportData(snaps=[snap], fee_table=t))
    assert "2026-07 §" in html
    assert "event feed not synced" in html
