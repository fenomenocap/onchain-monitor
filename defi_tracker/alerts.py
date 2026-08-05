"""
Rebalance alert engine.

Consumes Positions + PnLBreakdown, emits Alerts. Stateless — caller dedupes
via storage.recently_alerted().

Signals:
  1. OUT_OF_RANGE     CL position no longer earning fees
  2. NEAR_EDGE        Within N% of range bound; rebalance soon
  3. IL_THRESHOLD     IL% exceeded the user's tolerance
  4. NEGATIVE_CARRY   Fees < IL accrual rate
  5. STALE_POSITION   No events in 30+ days and out of range
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from defi_tracker.analytics import PnLBreakdown
from defi_tracker.core.types import Alert, AlertSeverity, Position, ProtocolKind


@dataclass
class AlertConfig:
    """Tunable thresholds. Override per-wallet if needed."""

    il_threshold_pct: Decimal = Decimal("10.0")  # alert if IL worse than -10%
    edge_proximity_pct: Decimal = Decimal("15.0")  # alert when within 15% of bound
    stale_days: int = 30
    negative_carry_check: bool = True
    # Steady-state conditions (still out of range, still bleeding) re-alert at
    # most once per this many days; state *transitions* always alert. 0 = never
    # remind. Day-to-day visibility comes from the daily report, not alerts.
    reminder_days: int = 7
    # Which alert kinds actually get pushed/deduped. The pure evaluate_alerts()
    # still computes every signal (callers may inspect them all), but for a
    # volatility-tolerant book we only push the economic ones: REBALANCE (the
    # 🔴 tier, emitted at portfolio level) and NEGATIVE_CARRY. Raw range
    # mechanics (OUT_OF_RANGE, NEAR_EDGE) and the blunt IL_THRESHOLD are
    # retired from the push path — the daily report's "Needs action" carries
    # the graduated 🟠/🟡 tiers instead. Add kinds here to re-enable them.
    push_kinds: frozenset[str] = frozenset({"REBALANCE", "NEGATIVE_CARRY"})


# Default config singleton — avoids constructing in default argument (ruff B008).
DEFAULT_ALERT_CONFIG = AlertConfig()


@dataclass(frozen=True)
class PrevPositionState:
    """The position's state at the previous snapshot, for transition detection.
    None fields mean 'unknown' — treated as a transition when the condition
    holds now (first observation should alert)."""

    in_range: bool | None
    near_edge: bool | None
    il_pct: Decimal | None


def near_edge_pct(
    tick_lower: int | None, tick_upper: int | None, tick_current: int | None
) -> Decimal | None:
    """Distance to the nearest range bound as % of range width, or None when
    ticks are missing/degenerate. Shared by the evaluator and the runner so
    'near edge' means the same thing when reconstructing previous state."""
    if tick_lower is None or tick_upper is None or tick_current is None:
        return None
    width = tick_upper - tick_lower
    if width <= 0:
        return None
    return Decimal(min(tick_upper - tick_current, tick_current - tick_lower)) / width * 100


def evaluate_alerts(
    position: Position,
    pnl: PnLBreakdown | None,
    config: AlertConfig | None = None,
    prev: PrevPositionState | None = None,
) -> Iterator[Alert]:
    """Yield every alert this position currently warrants.

    pnl is None when the position has no event history (unknown cost basis) —
    range-based signals still run; IL-based signals are skipped.

    prev is the state at the previous snapshot. Each alert carries
    context["transition"]: True when the condition just started holding (or
    state is unknown), False when it was already true — the runner dedupes
    reminders (transition=False) on a much longer window. Persistent-condition
    kinds (NEGATIVE_CARRY, STALE_POSITION) are always reminders after their
    first occurrence. Stateless by design: all state arrives via `prev`.
    """
    if config is None:
        config = DEFAULT_ALERT_CONFIG
    now = int(datetime.now(UTC).timestamp())

    # ── 1. Out of range (CL only) ─────────────────────────────────────────
    if position.protocol_kind == ProtocolKind.CL_AMM and position.in_range is False:
        transition = prev is None or prev.in_range is not False
        yield Alert(
            position_uid=position.uid,
            kind="OUT_OF_RANGE",
            severity=AlertSeverity.HIGH,
            message=(
                f"{position.pair_label} is out of range. "
                f"Earning 0 fees until price re-enters or you rebalance."
            ),
            triggered_at=now,
            context={
                "tick_current": position.tick_current,
                "range": [position.tick_lower, position.tick_upper],
                "transition": transition,
            },
        )

    # ── 2. Near edge of range ─────────────────────────────────────────────
    if position.protocol_kind == ProtocolKind.CL_AMM and position.in_range:
        min_dist_pct = near_edge_pct(
            position.tick_lower, position.tick_upper, position.tick_current
        )
        if min_dist_pct is not None and min_dist_pct < config.edge_proximity_pct:
            transition = prev is None or prev.near_edge is not True
            yield Alert(
                position_uid=position.uid,
                kind="NEAR_EDGE",
                severity=AlertSeverity.MEDIUM,
                message=(
                    f"{position.pair_label} is within {min_dist_pct:.1f}% of its range bound."
                ),
                triggered_at=now,
                context={"min_dist_pct": float(min_dist_pct), "transition": transition},
            )

    # ── 3. IL exceeded threshold ──────────────────────────────────────────
    if pnl is not None and pnl.il_pct < -config.il_threshold_pct:
        transition = prev is None or prev.il_pct is None or prev.il_pct >= -config.il_threshold_pct
        yield Alert(
            position_uid=position.uid,
            kind="IL_THRESHOLD",
            severity=AlertSeverity.HIGH,
            message=(
                f"{position.pair_label}: IL is "
                f"{pnl.il_pct:.2f}% vs HODL "
                f"(threshold: -{config.il_threshold_pct}%). "
                f"Net loss vs holding: ${pnl.il_usd:,.2f}."
            ),
            triggered_at=now,
            context={
                "il_pct": float(pnl.il_pct),
                "il_usd": float(pnl.il_usd),
                "transition": transition,
            },
        )

    # ── 4. Negative carry — fees aren't keeping up with IL ────────────────
    if (
        config.negative_carry_check
        and pnl is not None
        and pnl.il_usd < 0
        and position.seven_day_fees_usd
        and position.position_share_pct
        and position.opened_at
    ):
        days_open = max((now - position.opened_at) / 86400, 1)
        daily_il = abs(pnl.il_usd) / Decimal(str(days_open))
        daily_fees = position.seven_day_fees_usd * (position.position_share_pct / 100) / 7

        if daily_fees < daily_il and daily_il > Decimal("1"):  # >$1/day matters
            yield Alert(
                position_uid=position.uid,
                kind="NEGATIVE_CARRY",
                severity=AlertSeverity.MEDIUM,
                message=(
                    f"{position.pair_label}: IL accruing at "
                    f"${daily_il:.2f}/day, fees only "
                    f"${daily_fees:.2f}/day. Position is bleeding."
                ),
                triggered_at=now,
                context={
                    "daily_il_usd": float(daily_il),
                    "daily_fees_usd": float(daily_fees),
                    # Persistent condition — first occurrence alerts (no prior
                    # row in the dedup window), repeats are reminders.
                    "transition": False,
                },
            )

    # ── 5. Stale position with drifted price ──────────────────────────────
    if (
        position.last_event_at
        and position.protocol_kind == ProtocolKind.CL_AMM
        and position.in_range is False
    ):
        days_since_event = (now - position.last_event_at) / 86400
        if days_since_event > config.stale_days:
            yield Alert(
                position_uid=position.uid,
                kind="STALE_POSITION",
                severity=AlertSeverity.LOW,
                message=(
                    f"{position.pair_label}: no activity in "
                    f"{days_since_event:.0f} days and out of range. "
                    f"Capital is idle."
                ),
                triggered_at=now,
                context={"days_since_event": int(days_since_event), "transition": False},
            )
