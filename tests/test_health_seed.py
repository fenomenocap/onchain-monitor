"""Backfilling pipeline_health from evidence that already exists in the DB.

The health table shipped after the incident it exists to catch, so on the day
it lands every source has no history and would report "no recorded success" —
false, and it buries the one number that matters ("BSC last synced 39d ago").
Two sources of real evidence are already in the DB: sync_state (a sync ran) and
the snapshots table (a snapshot row IS proof fetch_positions succeeded).

The bug this pins: an earlier version of the backfill left the snapshot
timestamp NULL, and the renderer — correctly treating unknown as "not evidence
of health" — raised a 🚨 "every value shown is stale" against chains whose marks
were written that same morning. A false alarm on healthy data discredits the
warning exactly as much as a missed alarm on broken data. Worse, INSERT OR
IGNORE meant those wrong rows were permanent: once written, never revisited.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from defi_tracker.core.storage import Storage

WALLET = "0x1111111111111111111111111111111111111111"
SYNCED_AT = 1_783_296_866  # 2026-07-06
SNAPPED_AT = 1_786_666_719  # 2026-08-14


def _existing_install(db: Path, *, with_snapshot: bool = True) -> Storage:
    """A DB that has synced and (optionally) snapshotted but has no health rows
    — exactly what an install looks like the day the table ships. The seed runs
    inside init_schema, so callers trigger it themselves once the evidence is
    in place."""
    s = Storage(db)
    s.init_schema()
    with s.connect() as conn:
        conn.execute("DELETE FROM pipeline_health")
        conn.execute(
            """INSERT INTO sync_state (wallet, chain, protocol_id, last_event_ts,
                                       last_synced_at)
               VALUES (?, 'bsc', 'pancake_infinity', ?, ?)""",
            (WALLET, SYNCED_AT, SYNCED_AT),
        )
        if with_snapshot:
            conn.execute(
                """INSERT INTO snapshots (snapshot_date, wallet, chain, protocol_id,
                       position_key, protocol_kind, pair_label, snapshot_ts,
                       current_value_usd, current_balances_json)
                   VALUES ('2026-08-14', ?, 'bsc', 'pancake_infinity', 'p1',
                           'cl_amm', 'USDC/PEAQ', ?, 100.0, '[]')""",
                (WALLET, SNAPPED_AT),
            )
    return s


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "seed.db"


def test_seeds_both_timestamps_from_existing_evidence(db: Path):
    s = _existing_install(db)
    s.init_schema()
    (row,) = s.pipeline_health()
    assert row["last_sync_ok_at"] == SYNCED_AT
    assert row["last_snapshot_ok_at"] == SNAPPED_AT


def test_no_snapshots_leaves_the_snapshot_stage_unknown(db: Path):
    """An adapter that has synced but never snapshotted (the disabled Base
    adapter) has no evidence to seed — do not invent one."""
    s = _existing_install(db, with_snapshot=False)
    s.init_schema()
    (row,) = s.pipeline_health()
    assert row["last_sync_ok_at"] == SYNCED_AT
    assert row["last_snapshot_ok_at"] is None


def test_repairs_a_row_left_null_by_an_earlier_backfill(db: Path):
    """The regression: rows written by the first version of this backfill kept
    a NULL snapshot timestamp forever under INSERT OR IGNORE, so the false
    'every value is stale' alarm was permanent."""
    s = _existing_install(db)
    s.init_schema()
    with s.connect() as conn:  # simulate the old, wrong seed
        conn.execute("UPDATE pipeline_health SET last_snapshot_ok_at = NULL")
    s.init_schema()  # re-running must heal it
    (row,) = s.pipeline_health()
    assert row["last_snapshot_ok_at"] == SNAPPED_AT


def test_never_regresses_a_recorded_success(db: Path):
    """The seed fills gaps only. A live pass records the truth; a later
    init_schema must not drag a timestamp backwards to the seeded evidence."""
    s = _existing_install(db)
    s.init_schema()
    later = SNAPPED_AT + 10 * 86400
    with s.connect() as conn:
        conn.execute(
            "UPDATE pipeline_health SET last_sync_ok_at = ?, last_snapshot_ok_at = ?",
            (later, later),
        )
    s.init_schema()
    (row,) = s.pipeline_health()
    assert row["last_sync_ok_at"] == later
    assert row["last_snapshot_ok_at"] == later


def test_is_idempotent(db: Path):
    s = _existing_install(db)
    for _ in range(3):
        s.init_schema()
    rows = s.pipeline_health()
    assert len(rows) == 1
    assert rows[0]["last_snapshot_ok_at"] == SNAPPED_AT
