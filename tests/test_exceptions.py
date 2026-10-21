"""
Exception and incident log (Phase 2.3).

Mandate §4 weights these as heavily as yield: "every exception logged and
reviewed; trending to zero". So an exception is an EPISODE — opened by a
breach, cleared only by a reading that says the breach is over — and
clearing it is not the same as reviewing it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from defi_tracker.core.storage import SCHEMA_VERSION, Storage

UID = "policy:concentration.max_pair_pct:WPEAQ/USDT"


def _check(status: str, observed: str | None = "60") -> dict:
    return {
        "uid": UID,
        "limit_id": "concentration.max_pair_pct",
        "subject": "WPEAQ/USDT",
        "unit": "%",
        "observed": Decimal(observed) if observed is not None else None,
        "threshold": Decimal("50"),
        "status": status,
    }


def test_schema_has_the_v8_log_tables(storage: Storage):
    assert SCHEMA_VERSION >= 8
    with storage.connect() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"policy_exceptions", "incidents"} <= tables


def test_a_breach_opens_one_episode_and_tracks_its_worst_reading(storage: Storage):
    storage.reconcile_policy_exceptions([_check("breach", "60")], now=1)
    storage.reconcile_policy_exceptions([_check("breach", "75")], now=2)
    storage.reconcile_policy_exceptions([_check("breach", "55")], now=3)
    (row,) = storage.list_exceptions()
    assert (row["first_observed"], row["worst_observed"], row["last_observed"]) == (
        "60",
        "75",
        "55",
    )
    assert (row["opened_at"], row["last_seen"], row["cleared_at"]) == (1, 3, None)
    assert storage.open_exception_uids() == {UID}


def test_an_ok_reading_clears_and_a_later_breach_is_a_new_episode(storage: Storage):
    storage.reconcile_policy_exceptions([_check("breach")], now=1)
    storage.reconcile_policy_exceptions([_check("ok", "40")], now=2)
    assert storage.open_exception_uids() == set()
    storage.reconcile_policy_exceptions([_check("breach")], now=3)
    assert [r["opened_at"] for r in storage.list_exceptions()] == [3, 1]


@pytest.mark.parametrize("status", ["unknown", "unset"])
def test_not_being_able_to_measure_a_breach_does_not_end_it(storage: Storage, status):
    storage.reconcile_policy_exceptions([_check("breach")], now=1)
    storage.reconcile_policy_exceptions([_check(status, None)], now=2)
    assert storage.open_exception_uids() == {UID}


def test_clearing_is_not_reviewing(storage: Storage):
    storage.reconcile_policy_exceptions([_check("breach")], now=1)
    storage.reconcile_policy_exceptions([_check("ok", "40")], now=2)
    (row,) = storage.list_exceptions()
    assert row["cleared_at"] == 2 and row["reviewed_at"] is None
    assert storage.review_exception(
        row["id"], by="xavi", disposition="accepted", note="rebalanced", now=3
    )
    (row,) = storage.list_exceptions()
    assert (row["reviewed_by"], row["disposition"]) == ("xavi", "accepted")


def test_exceptions_in_a_window_include_those_that_straddle_it(storage: Storage):
    storage.reconcile_policy_exceptions([_check("breach")], now=100)
    storage.reconcile_policy_exceptions([_check("ok", "1")], now=200)
    assert storage.list_exceptions(since_ts=150, until_ts=300)
    assert not storage.list_exceptions(since_ts=250, until_ts=300)
    assert not storage.list_exceptions(since_ts=0, until_ts=50)


def test_runner_opens_exceptions_and_alerts_a_new_breach_at_once(
    storage: Storage, make_position, tmp_path, monkeypatch
):
    from defi_tracker.core.runner import TrackerRunner

    policy = tmp_path / "policy.toml"
    policy.write_text("[concentration]\nmax_pair_pct = 50\n")
    monkeypatch.setenv("POLICY_FILE", str(policy))
    storage.write_snapshot(make_position(current_value_usd=Decimal("900")))
    runner = TrackerRunner(storage)
    assert runner._emit_policy_alerts() == 1  # noqa: SLF001
    (alert,) = storage.list_alerts()
    assert alert["kind"] == "POLICY_BREACH"
    assert storage.open_exception_uids() == {"policy:concentration.max_pair_pct:USDT/ETH"}
    # Still breaching next pass: an open episode is a reminder, deduped.
    assert runner._emit_policy_alerts() == 0  # noqa: SLF001
    assert len(storage.list_exceptions()) == 1


def test_a_policy_of_only_counterparty_caps_is_still_enforced(
    storage: Storage, make_position, tmp_path, monkeypatch
):
    """`[counterparty.caps]` lives outside Policy.values; a caps-only file must
    not read as "nothing to check"."""
    from datetime import date

    from defi_tracker.core.runner import TrackerRunner

    policy = tmp_path / "policy.toml"
    policy.write_text('[counterparty.caps]\n"Alpha MM" = 1000\n')
    monkeypatch.setenv("POLICY_FILE", str(policy))
    storage.write_snapshot(make_position(current_value_usd=Decimal("900")))
    storage.record_contract(
        "alpha-loan", date(2024, 11, 1), now=1, counterparty="Alpha MM", kind="mm_loan",
        status="active", asset="PEAQ", amount=Decimal("5000000"),
        usd_value=Decimal("250000"), start_date="2024-11-01", end_date=None,
        source_note="test",
    )  # fmt: skip
    assert TrackerRunner(storage)._emit_policy_alerts() == 1  # noqa: SLF001
    assert storage.open_exception_uids() == {"policy:counterparty.caps:Alpha MM"}


# ── incidents ─────────────────────────────────────────────────────────────


def test_an_incident_walks_its_lifecycle(storage: Storage):
    iid = storage.add_incident(
        title="MachineX subgraph indexers down", severity="high", detected_at=10, now=11
    )
    assert storage.update_incident(iid, escalated_to="ops", contained_at=12)
    assert storage.update_incident(iid, resolved_at=20, postmortem="RPC fallback shipped")
    row = storage.get_incident(iid)
    assert row is not None
    assert (row["contained_at"], row["resolved_at"]) == (12, 20)


def test_incident_updates_reject_unknown_fields(storage: Storage):
    iid = storage.add_incident(title="x", severity="low", detected_at=1, now=1)
    with pytest.raises(ValueError):
        storage.update_incident(iid, severity="critical")


def test_resolving_an_incident_requires_a_postmortem(storage: Storage, monkeypatch, capsys):
    from defi_tracker import cli

    iid = storage.add_incident(title="x", severity="low", detected_at=1, now=1)
    monkeypatch.setattr(cli, "_db", lambda: storage)
    args = type(
        "A",
        (),
        {
            "action": "update",
            "id": iid,
            "resolved": True,
            "postmortem": None,
            "contained": False,
            "escalated_to": None,
            "description": None,
            "at": None,
        },
    )()
    cli.cmd_incident(args)
    assert "needs a --postmortem" in capsys.readouterr().out
    row = storage.get_incident(iid)
    assert row is not None and row["resolved_at"] is None
