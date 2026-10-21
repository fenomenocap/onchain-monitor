"""
The book's headline change, and the window it actually spans.

`value_delta` used to hand surfaces a bare (curr, prev) pair, which every
surface then labelled "Δ day". A chain whose snapshot pass fails for three
days has a prior mark three days old, so that label was a lie exactly when
the number was most surprising. These tests pin the window down.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from defi_tracker.core.types import Chain


def _write(storage, make_position, day: str, value: str, key: str = "p1", chain=Chain.BSC):
    storage.write_snapshot(
        make_position(
            position_key=key,
            chain=chain,
            current_value_usd=Decimal(value),
        ),
        snapshot_date=date.fromisoformat(day),
    )


def test_none_until_a_position_has_two_marks(storage, make_position):
    _write(storage, make_position, "2026-09-21", "100")
    assert storage.value_delta() is None


def test_consecutive_marks_span_one_day(storage, make_position):
    _write(storage, make_position, "2026-09-20", "100")
    _write(storage, make_position, "2026-09-21", "110")

    vd = storage.value_delta()
    assert vd is not None
    assert (vd.curr, vd.prev) == (110.0, 100.0)
    assert vd.curr_date == "2026-09-21"
    assert vd.prev_date == "2026-09-20"
    assert vd.span_days == 1
    assert vd.delta == 10.0
    assert vd.pct == 10.0
    assert vd.window_label() == "since yesterday"


def test_a_gap_in_the_history_is_reported_as_its_real_span(storage, make_position):
    """Every position missed the same days. The move is real, but it is a
    four-day move and must not be called a day's."""
    _write(storage, make_position, "2026-09-17", "100")
    _write(storage, make_position, "2026-09-21", "130")

    vd = storage.value_delta()
    assert vd is not None
    assert vd.span_days == 4
    assert vd.delta == 30.0
    assert vd.window_label() == "since 2026-09-17 (4d)"
    assert vd.excluded_positions == 0


def test_a_position_with_a_stale_baseline_is_excluded_and_counted(storage, make_position):
    """The bug this module exists for: peaq misses two passes, so its prior
    mark is three days older than everyone else's. Folding its three-day move
    into a one-day delta is the wrong number under the right label."""
    # Fresh position: marks on both days.
    _write(storage, make_position, "2026-09-20", "100", key="fresh")
    _write(storage, make_position, "2026-09-21", "105", key="fresh")
    # Stale position: last marked 09-18, then again today — nothing between.
    _write(storage, make_position, "2026-09-18", "400", key="stale", chain=Chain.PEAQ)
    _write(storage, make_position, "2026-09-21", "500", key="stale", chain=Chain.PEAQ)

    vd = storage.value_delta()
    assert vd is not None
    # The headline is the one-day slice only.
    assert (vd.curr, vd.prev) == (105.0, 100.0)
    assert vd.span_days == 1
    # …and the excluded book is stated, not dropped in silence.
    assert vd.excluded_positions == 1
    assert vd.excluded_value == 500.0


def test_a_source_that_stopped_reporting_contributes_nothing(storage, make_position):
    """A position whose LATEST mark is not the latest run's has no current
    value to compare — its old move must not surface as today's."""
    _write(storage, make_position, "2026-09-20", "100", key="live")
    _write(storage, make_position, "2026-09-21", "110", key="live")
    _write(storage, make_position, "2026-09-10", "900", key="dead", chain=Chain.PEAQ)
    _write(storage, make_position, "2026-09-11", "300", key="dead", chain=Chain.PEAQ)

    vd = storage.value_delta()
    assert vd is not None
    assert (vd.curr, vd.prev) == (110.0, 100.0)
    assert vd.excluded_positions == 0


def test_a_zero_baseline_yields_no_percentage(storage, make_position):
    _write(storage, make_position, "2026-09-20", "0")
    _write(storage, make_position, "2026-09-21", "50")

    vd = storage.value_delta()
    assert vd is not None
    assert vd.delta == 50.0
    assert vd.pct is None


def test_closed_positions_are_not_marked(storage, make_position):
    _write(storage, make_position, "2026-09-20", "100")
    storage.write_snapshot(
        make_position(position_key="p1", current_value_usd=Decimal("0")),
        snapshot_date=date.fromisoformat("2026-09-21"),
        is_closed=True,
    )
    # Only the 09-20 row is live, so there is no pair to compare.
    assert storage.value_delta() is None


def test_the_wallet_is_not_part_of_the_comparison_key_by_accident(storage, make_position):
    """Two wallets holding the same position_key are distinct positions and
    each is matched against its own prior mark."""
    _write(storage, make_position, "2026-09-20", "100")
    _write(storage, make_position, "2026-09-21", "110")
    other = "0xdef456abc123def456abc123def456abc123def4"
    storage.write_snapshot(
        make_position(position_key="p1", wallet=other, current_value_usd=Decimal("200")),
        snapshot_date=date.fromisoformat("2026-09-20"),
    )
    storage.write_snapshot(
        make_position(position_key="p1", wallet=other, current_value_usd=Decimal("220")),
        snapshot_date=date.fromisoformat("2026-09-21"),
    )

    vd = storage.value_delta()
    assert vd is not None
    assert vd.prev == 300.0
    assert vd.curr == 330.0
