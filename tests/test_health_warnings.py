"""analytics.health_warnings — the copy that tells the reader a number is stale.

The failure this guards against: BSC ran 39 days on an RPC fallback while every
report looked healthy, because the old check compared snapshot DATES and the
fallback keeps those current. These assertions are about what the reader is
told, so they check the substance of the line, not its exact wording.
"""

from __future__ import annotations

from defi_tracker.analytics import health_warnings

NOW = 1_786_000_000  # fixed clock so ages are deterministic
HOUR = 3600
DAY = 86400


def _row(
    chain="bsc",
    protocol_id="pancake_infinity",
    wallet="0xabc",
    sync_ok=NOW,
    snapshot_ok=NOW,
    source="subgraph",
    error=None,
    dropped=0,
):
    return {
        "wallet": wallet,
        "chain": chain,
        "protocol_id": protocol_id,
        "last_run_at": NOW,
        "last_sync_ok_at": sync_ok,
        "last_snapshot_ok_at": snapshot_ok,
        "source": source,
        "last_error": error,
        "dropped_events_last_run": dropped,
    }


class TestHealthy:
    def test_everything_current_says_nothing(self):
        assert health_warnings([_row()], now=NOW) == []

    def test_empty_input(self):
        assert health_warnings([], now=NOW) == []

    def test_recent_blip_within_grace_is_not_reported(self):
        # A source can miss a run or two without the reader needing to care.
        rows = [_row(sync_ok=NOW - 12 * HOUR, snapshot_ok=NOW - 12 * HOUR)]
        assert health_warnings(rows, now=NOW) == []


class TestDegraded:
    def test_stale_event_sync_is_reported_with_age_and_consequence(self):
        rows = [_row(sync_ok=NOW - 39 * DAY)]
        (line,) = health_warnings(rows, now=NOW)
        assert "bsc/pancake_infinity" in line
        assert "39d ago" in line
        # the reader must be told what the staleness costs them
        assert "cost basis" in line

    def test_rpc_fallback_is_reported_even_when_timestamps_are_fresh(self):
        # The exact case the old snapshot-date check could never catch.
        rows = [_row(source="rpc_fallback")]
        (line,) = health_warnings(rows, now=NOW)
        assert "RPC fallback" in line
        # Pool context is unavailable rather than frozen — the warning must not
        # imply a stale number is still being shown.
        assert "unavailable" in line
        assert "frozen" not in line

    def test_stale_snapshot_is_the_loudest_signal(self):
        rows = [_row(snapshot_ok=NOW - 5 * DAY, sync_ok=NOW)]
        (line,) = health_warnings(rows, now=NOW)
        assert line.startswith("🚨")
        assert "stale" in line

    def test_missing_timestamp_reads_as_unknown_not_as_never(self):
        """A NULL timestamp means no success was RECORDED — which is also what a
        freshly-seeded health table looks like. Claiming the pipeline "has never
        succeeded" is an assertion the absent row cannot support."""
        rows = [_row(sync_ok=None, snapshot_ok=None)]
        lines = health_warnings(rows, now=NOW)
        assert len(lines) == 2
        assert all("no recorded success" in x for x in lines)
        assert not any("never" in x for x in lines)
        # An unknown snapshot timestamp must not claim the marks are stale —
        # we do not know that, and a false 🚨 on healthy data discredits the
        # warning as much as a missed one on broken data.
        snap_line = next(x for x in lines if "snapshot" in x)
        assert snap_line.startswith("ℹ️")
        assert "stale" not in snap_line

    def test_missing_timestamp_still_warns(self):
        # Unknown is not evidence of health — it must still be surfaced.
        assert len(health_warnings([_row(sync_ok=None)], now=NOW)) == 1

    def test_outage_and_fallback_produce_separate_lines(self):
        rows = [_row(sync_ok=NOW - 39 * DAY, source="rpc_fallback")]
        lines = health_warnings(rows, now=NOW)
        assert len(lines) == 2


WALLET_A = "0x2222d1f4b2a7c8e90123456789abcdef01234567"
WALLET_B = "0x1111d1f4b2a7c8e90123456789abcdef0189ab99"


class TestGrouping:
    def test_a_stale_wallet_warns_even_when_a_sibling_is_current(self):
        """The live machinex case: the adapter keeps a PER-WALLET block cursor
        and its per-run block cap applies per wallet, so one wallet drifts weeks
        behind while its sibling is current. Reporting the most recent success
        silenced a 39-day-stale wallet holding ~$160k."""
        rows = [
            _row(wallet=WALLET_A, sync_ok=NOW - 39 * DAY),
            _row(wallet=WALLET_B, sync_ok=NOW),
        ]
        (line,) = health_warnings(rows, now=NOW)
        assert "39d ago" in line  # the OLDEST success, not the newest

    def test_the_stale_wallet_is_named_when_wallets_disagree(self):
        rows = [
            _row(wallet=WALLET_A, sync_ok=NOW - 39 * DAY),
            _row(wallet=WALLET_B, sync_ok=NOW),
        ]
        (line,) = health_warnings(rows, now=NOW)
        assert "0x2222" in line  # the reader must know WHICH wallet
        assert "0x1111" not in line  # the healthy sibling is not implicated

    def test_uniform_outage_stays_one_wallet_free_line(self):
        """When every wallet is behind it is a source outage, not a wallet
        problem — one line, no per-wallet spam."""
        rows = [
            _row(wallet=WALLET_A, sync_ok=NOW - 39 * DAY),
            _row(wallet=WALLET_B, sync_ok=NOW - 40 * DAY),
        ]
        (line,) = health_warnings(rows, now=NOW)
        assert "0x2222" not in line and "0x1111" not in line
        assert "40d ago" in line  # still the oldest of the two

    def test_healthy_wallets_stay_silent(self):
        rows = [_row(wallet=WALLET_A), _row(wallet=WALLET_B)]
        assert health_warnings(rows, now=NOW) == []

    def test_a_wallet_with_no_recorded_success_is_not_covered_by_a_healthy_sibling(self):
        rows = [
            _row(wallet=WALLET_A, sync_ok=None),
            _row(wallet=WALLET_B, sync_ok=NOW),
        ]
        (line,) = health_warnings(rows, now=NOW)
        assert "no recorded success" in line
        assert "0x2222" in line

    def test_fallback_on_any_wallet_flags_the_whole_source(self):
        rows = [
            _row(wallet="0xaaa", source="subgraph"),
            _row(wallet="0xbbb", source="rpc_fallback"),
        ]
        (line,) = health_warnings(rows, now=NOW)
        assert "RPC fallback" in line

    def test_each_chain_protocol_reported_separately_and_sorted(self):
        rows = [
            _row(chain="peaq", protocol_id="machinex", sync_ok=NOW - 10 * DAY),
            _row(chain="bsc", protocol_id="pancake_infinity", sync_ok=NOW - 39 * DAY),
        ]
        lines = health_warnings(rows, now=NOW)
        assert len(lines) == 2
        assert "bsc/pancake_infinity" in lines[0]  # sorted by chain
        assert "peaq/machinex" in lines[1]

    def test_custom_stale_threshold(self):
        rows = [_row(sync_ok=NOW - 3 * HOUR)]
        assert health_warnings(rows, now=NOW) == []
        assert len(health_warnings(rows, now=NOW, stale_hours=1)) == 1


class TestDroppedEvents:
    """A dropped event is not a stale sync — the scan ran and reached head, it
    just could not convert some of what it found. The cursor has already moved
    past them, so the line has to say the loss is permanent and name the fix."""

    def test_reports_count_and_the_recovery_command(self):
        rows = [_row(chain="peaq", protocol_id="machinex", dropped=3)]
        (line,) = health_warnings(rows, now=NOW)
        assert "3 on-chain event(s)" in line
        assert "will not be re-requested" in line
        assert "reset-cursor --chain peaq" in line

    def test_zero_or_absent_says_nothing(self):
        assert health_warnings([_row(dropped=0)], now=NOW) == []
        bare = _row()
        bare.pop("dropped_events_last_run", None)
        assert health_warnings([bare], now=NOW) == []

    def test_sums_across_wallets_in_a_group(self):
        rows = [
            _row(wallet=WALLET_A, chain="peaq", protocol_id="machinex", dropped=2),
            _row(wallet=WALLET_B, chain="peaq", protocol_id="machinex", dropped=1),
        ]
        (line,) = health_warnings(rows, now=NOW)
        assert "3 on-chain event(s)" in line

    def test_names_the_wallet_when_only_one_is_affected(self):
        rows = [
            _row(wallet=WALLET_A, chain="peaq", protocol_id="machinex", dropped=2),
            _row(wallet=WALLET_B, chain="peaq", protocol_id="machinex", dropped=0),
        ]
        (line,) = health_warnings(rows, now=NOW)
        assert "0x2222" in line  # the affected wallet
        assert "0x1111" not in line  # not the clean sibling

    def test_non_peaq_chains_get_a_reachable_remedy(self):
        """reset-cursor only knows peaq, so pointing bsc at it would be a lie."""
        rows = [_row(chain="bsc", protocol_id="pancake_infinity", dropped=1)]
        (line,) = health_warnings(rows, now=NOW)
        assert "reset-cursor" not in line
        assert "backfill" in line
