"""
Policy limits and breach detection (Phase 2.2).

The rule under test: a limit is OK, in BREACH, UNKNOWN or UNSET — and the
last two are never reported as passing. A book is not "inside" a limit that
nobody has set or that could not be measured.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from defi_tracker.alerts import policy_breach_alerts
from defi_tracker.analytics import breaches, policy_checks
from defi_tracker.policy import LIMITS, Policy, PolicyError, load_policy, parse_policy


def _row(pair="WPEAQ/USDT 1.00%", value=100.0, chain="peaq", key="1", **over) -> dict:
    row = {
        "wallet": "0xw",
        "chain": chain,
        "protocol_id": "machinex",
        "position_key": key,
        "pair_label": pair,
        "current_value_usd": value,
        "il_pct": -5.0,
        "in_range": 1,
    }
    row.update(over)
    return row


def _policy(**limits) -> Policy:
    doc: dict = {}
    for k, v in limits.items():
        section, key = k.split("__")
        doc.setdefault(section, {})[key] = v
    return parse_policy(doc)


def _by(checks, limit_id):
    return {c.subject: c for c in checks if c.limit_id == limit_id}


# ── loading ───────────────────────────────────────────────────────────────


def test_no_policy_file_means_every_limit_unset_not_passing(tmp_path, monkeypatch):
    monkeypatch.delenv("POLICY_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    policy = load_policy()
    checks = policy_checks([_row()], policy)
    assert {c.status for c in checks} == {"unset"}
    assert len(checks) == len(LIMITS)


def test_a_named_policy_file_that_is_missing_raises(tmp_path, monkeypatch):
    """A typo in --policy or $POLICY_FILE must not load as "no limits"."""
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "absent.toml")
    monkeypatch.setenv("POLICY_FILE", str(tmp_path / "absent.toml"))
    with pytest.raises(PolicyError):
        load_policy()


def test_the_committed_policy_file_sets_nothing_yet():
    """The mandate's numbers are still blank; the repo's file must not invent them."""
    assert load_policy("policy.toml").values == {}


def test_a_typo_in_a_limit_name_is_rejected_not_ignored():
    with pytest.raises(PolicyError, match="max_pari_pct"):
        parse_policy({"concentration": {"max_pari_pct": 20}})


@pytest.mark.parametrize("bad", [-1, "lots", True, float("nan")])
def test_a_limit_must_be_a_non_negative_number(bad):
    with pytest.raises(PolicyError):
        parse_policy({"risk": {"max_il_pct": bad}})


def test_a_depeg_trigger_needs_coins_to_watch():
    with pytest.raises(PolicyError, match="names no stablecoin"):
        parse_policy({"depeg": {"max_deviation_bps": 100}})


def test_malformed_toml_raises(tmp_path):
    p = tmp_path / "policy.toml"
    p.write_text("[risk\nmax_il_pct = 5")
    with pytest.raises(PolicyError):
        load_policy(p)


# ── concentration ─────────────────────────────────────────────────────────


def test_pair_concentration_aggregates_fee_tiers_and_divides_by_the_book():
    rows = [
        _row("WPEAQ/USDT 1.00%", 300, key="1"),
        _row("WPEAQ/USDT 0.30%", 300, key="2"),
        _row("SLC/USDC 1.00%", 400, key="3"),
    ]
    checks = _by(
        policy_checks(rows, _policy(concentration__max_pair_pct=50)), "concentration.max_pair_pct"
    )
    assert checks["WPEAQ/USDT"].observed == Decimal("60")
    assert checks["WPEAQ/USDT"].status == "breach"
    assert checks["SLC/USDC"].status == "ok"


def test_absolute_caps_compare_usd():
    rows = [_row(value=900_001, chain="bsc"), _row(value=10, chain="peaq", key="2")]
    checks = _by(
        policy_checks(rows, _policy(concentration__max_chain_usd=900_000)),
        "concentration.max_chain_usd",
    )
    assert checks["bsc"].status == "breach"
    assert checks["peaq"].status == "ok"


def test_exactly_at_the_limit_is_inside_it():
    rows = [_row(value=50, key="1"), _row("SLC/USDC", 50, key="2")]
    (check,) = [c for c in policy_checks(rows, _policy(concentration__max_pair_pct=50))
                if c.subject == "WPEAQ/USDT"]  # fmt: skip
    assert check.status == "ok"


# ── risk ──────────────────────────────────────────────────────────────────


def test_il_tolerance_caps_the_loss_and_unknown_il_is_unknown():
    rows = [_row(il_pct=-30.0, key="1"), _row(il_pct=None, key="2"), _row(il_pct=4.0, key="3")]
    checks = policy_checks(rows, _policy(risk__max_il_pct=25))
    status = [c.status for c in checks if c.limit_id == "risk.max_il_pct"]
    assert sorted(status) == ["breach", "ok", "unknown"]


def test_out_of_range_share():
    rows = [_row(value=40, in_range=0, key="1"), _row(value=60, key="2")]
    (check,) = _by(
        policy_checks(rows, _policy(risk__max_out_of_range_pct=30)), "risk.max_out_of_range_pct"
    ).values()
    assert check.observed == Decimal("40")
    assert check.status == "breach"


# ── depeg and counterparty need inputs the book does not carry ────────────


def test_depeg_without_a_market_price_is_unknown_never_on_peg():
    policy = parse_policy({"depeg": {"max_deviation_bps": 50, "coins": {"usdc": "usd-coin"}}})
    (check,) = _by(policy_checks([_row()], policy), "depeg.max_deviation_bps").values()
    assert check.subject == "USDC"
    assert check.status == "unknown"


def test_depeg_measures_distance_from_a_dollar_in_bps():
    policy = parse_policy(
        {"depeg": {"max_deviation_bps": 50, "coins": {"USDC": "usd-coin", "USDT": "tether"}}}
    )
    prices = {"USDC": Decimal("0.9930"), "USDT": Decimal("1.0004")}
    checks = _by(policy_checks([_row()], policy, prices), "depeg.max_deviation_bps")
    assert checks["USDC"].observed == Decimal("70") and checks["USDC"].status == "breach"
    assert checks["USDT"].status == "ok"


def test_counterparty_limits_are_unknown_until_exposures_exist():
    checks = policy_checks([_row()], _policy(counterparty__max_exposure_usd=100))
    (check,) = _by(checks, "counterparty.max_exposure_usd").values()
    assert check.status == "unknown"


def test_counterparty_exposure_is_checked_per_name_and_against_named_caps():
    policy = parse_policy({"counterparty": {"max_exposure_usd": 1000, "caps": {"Alpha MM": 400}}})
    exposure = {"Alpha MM": Decimal("500"), "Beta MM": Decimal("900")}
    checks = policy_checks([_row()], policy, counterparty_exposure=exposure)
    assert _by(checks, "counterparty.max_exposure_usd")["Beta MM"].status == "ok"
    assert _by(checks, "counterparty.caps")["Alpha MM"].status == "breach"


# ── alerts ────────────────────────────────────────────────────────────────


def test_breach_alerts_carry_a_stable_identity_per_limit_and_subject():
    rows = [_row(value=900, key="1"), _row("SLC/USDC", 100, key="2")]
    checks = policy_checks(rows, _policy(concentration__max_pair_pct=50))
    (alert,) = policy_breach_alerts(checks, now=1)
    assert alert.kind == "POLICY_BREACH"
    assert alert.position_uid == "policy:concentration.max_pair_pct:WPEAQ/USDT"
    assert "90.0%" in alert.message and "limit 50.0%" in alert.message


def test_a_breach_already_open_is_a_reminder_a_new_one_a_transition():
    rows = [_row(value=900, key="1"), _row("SLC/USDC", 100, key="2")]
    checks = breaches(policy_checks(rows, _policy(concentration__max_pair_pct=50)))
    (new,) = policy_breach_alerts(checks, open_uids=set(), now=1)
    (old,) = policy_breach_alerts(checks, open_uids={checks[0].uid}, now=1)
    assert new.context["transition"] is True
    assert old.context["transition"] is False


def test_an_invalid_policy_file_alerts_instead_of_stopping_the_run(storage, tmp_path, monkeypatch):
    from defi_tracker.core.runner import TrackerRunner

    bad = tmp_path / "policy.toml"
    bad.write_text("[risk]\nmax_il_pc = 5\n")
    monkeypatch.setenv("POLICY_FILE", str(bad))
    runner = TrackerRunner(storage)
    assert runner._emit_policy_alerts() == 1  # noqa: SLF001
    assert runner._emit_policy_alerts() == 0  # noqa: SLF001 — deduped within a day
    (alert,) = storage.list_alerts()
    assert "limits NOT evaluated" in alert["message"]


def test_two_wallets_in_the_same_range_are_two_position_subjects():
    """uniswap_v3 keys a position by pool:lo:hi, so two wallets in the same
    full-range position share a position_key — but not a position."""
    rows = [
        {**_row(), "wallet": w, "position_key": "pool:-887220:887220", "current_value_usd": 30}
        for w in ("0xa", "0xb")
    ] + [{**_row(), "wallet": "0xc", "position_key": "other", "current_value_usd": 40}]
    checks = policy_checks(rows, _policy(concentration__max_position_pct=40))
    by_subject = _by(checks, "concentration.max_position_pct")
    assert len(by_subject) == 3
    assert {c.status for c in by_subject.values()} == {"ok"}
