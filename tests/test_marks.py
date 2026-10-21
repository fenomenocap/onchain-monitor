"""
Sub-daily marks.

The roadmap calls this the item that gets worse the longer it waits: every
risk metric downstream is a function of history not yet being collected, and
history cannot be reconstructed retroactively.

The design constraint these tests defend: marks are NOT snapshots. `snapshots`
is keyed by date, so a second pass on the same day overwrites the first —
running it hourly would lose intraday history as fast as it was gathered.
`marks` is keyed by timestamp and keeps every pass.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from defi_tracker.core.adapter import ProtocolAdapter
from defi_tracker.core.runner import TrackerRunner
from defi_tracker.core.types import AdapterInfo, Chain, ProtocolKind

from .conftest import TEST_WALLET


class _StubAdapter(ProtocolAdapter):
    """Returns canned positions; can be told to fail."""

    def __init__(self, positions=None, fail=False, chains=None):
        self._positions = positions or []
        self._fail = fail
        self._chains = chains or [Chain.BSC]

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            protocol_id="pancake_infinity",
            display_name="Stub",
            chains=self._chains,
            protocol_kind=ProtocolKind.CL_AMM,
            supports_exact_fees=True,
            supports_historical=True,
        )

    def fetch_events(self, wallet, chain, since_ts=0):
        return []

    def fetch_positions(self, wallet, chain):
        if self._fail:
            raise RuntimeError("chain is down")
        return self._positions


# ── the table keeps what snapshots cannot ─────────────────────────────────


def test_two_marks_on_one_day_are_both_kept(storage, make_position):
    """The whole reason for a separate table. `snapshots` is keyed by date and
    the second write of a day replaces the first; `marks` keeps both."""
    pos = make_position(current_value_usd=Decimal("100"))
    storage.write_marks([pos], ts=1_700_000_000)
    pos2 = make_position(current_value_usd=Decimal("110"))
    storage.write_marks([pos2], ts=1_700_003_600)

    marks = storage.marks_in_range()
    assert [m["current_value_usd"] for m in marks] == [100.0, 110.0]

    # Same two writes into snapshots, same day: only one row survives.
    storage.write_snapshot(pos, snapshot_date=date(2023, 11, 14))
    storage.write_snapshot(pos2, snapshot_date=date(2023, 11, 14))
    assert len(storage.latest_snapshot()) == 1


def test_a_mark_is_idempotent_within_one_instant(storage, make_position):
    storage.write_marks([make_position()], ts=1_700_000_000)
    storage.write_marks([make_position()], ts=1_700_000_000)
    assert len(storage.marks_in_range()) == 1


def test_every_position_in_a_pass_shares_one_timestamp(storage, make_position):
    """A cross-position series that does not line up is useless for
    correlation, so the pass stamps the book once."""
    positions = [make_position(position_key=f"p{i}") for i in range(3)]
    storage.write_marks(positions, ts=1_700_000_000)
    assert {m["ts"] for m in storage.marks_in_range()} == {1_700_000_000}


def test_marks_carry_what_a_volatility_estimator_reads(storage, make_position):
    storage.write_marks(
        [make_position(tick_current=205_000, in_range=True, current_value_usd=Decimal("3400"))],
        ts=1_700_000_000,
    )
    m = storage.marks_in_range()[0]
    assert m["tick_current"] == 205_000
    assert m["tick_lower"] == 200_000
    assert m["tick_upper"] == 210_000
    assert m["in_range"] == 1
    assert m["current_value_usd"] == 3400.0
    assert m["liquidity"] == "1000000000"


def test_an_unreadable_unclaimed_balance_stays_unknown(storage, make_position):
    storage.write_marks([make_position(unclaimed_usd=None)], ts=1_700_000_000)
    m = storage.marks_in_range()[0]
    assert m["unclaimed_usd"] is None
    assert m["unclaimed_is_known"] == 0


def test_liquidity_beyond_64_bits_survives_the_round_trip(storage, make_position):
    """A CL liquidity value can exceed 2^63, which SQLite INTEGER cannot hold.
    Stored as TEXT so it comes back intact rather than overflowing."""
    huge = 2**70 + 7
    pos = make_position()
    pos.liquidity = huge
    storage.write_marks([pos], ts=1_700_000_000)
    assert int(storage.marks_in_range()[0]["liquidity"]) == huge


def test_writing_no_positions_writes_nothing(storage):
    assert storage.write_marks([], ts=1_700_000_000) == 0


def test_the_series_can_be_windowed(storage, make_position):
    for i, ts in enumerate([100, 200, 300]):
        storage.write_marks([make_position(current_value_usd=Decimal(str(i)))], ts=ts)
    storage.write_marks([make_position(position_key="other")], ts=200)

    assert len(storage.marks_in_range(start_ts=200)) == 3
    assert len(storage.marks_in_range(start_ts=150, end_ts=250)) == 2


# ── coverage: how far off the risk metrics still are ──────────────────────


def test_coverage_counts_positions_by_their_full_key(storage, make_position):
    """Two wallets in the same pool range share a position_key, not a position."""
    storage.write_marks(
        [make_position(wallet="0x" + "a" * 40), make_position(wallet="0x" + "b" * 40)], ts=100
    )
    assert storage.mark_coverage()["positions"] == 2


def test_coverage_is_empty_before_the_first_pass(storage):
    cov = storage.mark_coverage()
    assert cov["marks"] == 0
    assert cov["median_gap_s"] is None


def test_coverage_reports_the_observed_cadence_not_the_configured_one(storage, make_position):
    """A cron that silently stopped shows up here and nowhere else."""
    hour = 3600
    for i in range(4):
        storage.write_marks([make_position(), make_position(position_key="b")], ts=i * hour)

    cov = storage.mark_coverage()
    assert cov["marks"] == 8
    assert cov["passes"] == 4
    assert cov["positions"] == 2
    assert cov["median_gap_s"] == hour
    assert (cov["first_ts"], cov["last_ts"]) == (0, 3 * hour)


# ── the runner ────────────────────────────────────────────────────────────


def test_a_full_pass_also_writes_a_mark(storage_with_wallet, make_position):
    """A mark costs one write and the run already has the positions in hand;
    not writing one would throw away the only record of an intraday pass."""
    runner = TrackerRunner(storage_with_wallet)
    runner.snapshot_adapter(
        _StubAdapter([make_position()]), TEST_WALLET, Chain.BSC, mark_ts=1_700_000_000
    )
    assert len(storage_with_wallet.marks_in_range()) == 1


def test_mark_all_writes_marks_and_no_snapshot(storage_with_wallet, make_position, monkeypatch):
    """The hourly path must not write a daily snapshot — that is what makes it
    cheap enough to run hourly."""
    import defi_tracker.core.runner as runner_mod

    monkeypatch.setattr(runner_mod, "all_adapters", lambda: [_StubAdapter([make_position()])])
    result = TrackerRunner(storage_with_wallet).mark_all(chains=[Chain.BSC])

    assert result["marks_written"] == 1
    assert result["positions"] == 1
    assert len(storage_with_wallet.marks_in_range()) == 1
    assert storage_with_wallet.latest_snapshot() == []


def test_one_dead_chain_does_not_lose_the_whole_pass(
    storage_with_wallet, make_position, monkeypatch
):
    """A partial mark is a real observation of the positions it covers, and
    the history it represents cannot be gathered again later."""
    import defi_tracker.core.runner as runner_mod

    good = _StubAdapter([make_position()], chains=[Chain.BSC])
    bad = _StubAdapter(fail=True, chains=[Chain.BSC])
    monkeypatch.setattr(runner_mod, "all_adapters", lambda: [bad, good])

    result = TrackerRunner(storage_with_wallet).mark_all(chains=[Chain.BSC])
    assert result["marks_written"] == 1
    assert len(result["failures"]) == 1
    assert "chain is down" in result["failures"][0]


def test_mark_all_with_no_positions_reports_a_clean_empty_pass(storage_with_wallet, monkeypatch):
    import defi_tracker.core.runner as runner_mod

    monkeypatch.setattr(runner_mod, "all_adapters", lambda: [_StubAdapter([])])
    result = TrackerRunner(storage_with_wallet).mark_all(chains=[Chain.BSC])
    assert (result["marks_written"], result["positions"], result["failures"]) == (0, 0, [])


@pytest.mark.parametrize("filter_kw", [{"protocols": ["nope"]}, {"chains": [Chain.BASE]}])
def test_filters_narrow_the_pass(storage_with_wallet, make_position, monkeypatch, filter_kw):
    import defi_tracker.core.runner as runner_mod

    monkeypatch.setattr(runner_mod, "all_adapters", lambda: [_StubAdapter([make_position()])])
    assert TrackerRunner(storage_with_wallet).mark_all(**filter_kw)["marks_written"] == 0


# ── how the coverage reads ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("secs", "expected"),
    [(9, "9s"), (89, "89s"), (3600, "60m"), (5400, "1.5h"), (86400, "24.0h"), (604800, "7.0d")],
)
def test_a_gap_is_formatted_at_a_useful_scale(secs, expected):
    """An hourly cadence and a stalled one must not both read '0.0h'."""
    from defi_tracker.cli import _duration

    assert _duration(secs) == expected
