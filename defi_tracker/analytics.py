"""
Analytics layer: derives cost basis, IL, and PnL from event history.

Protocol-agnostic — adapters know how to get data; analytics knows what to
do with it. ProtocolKind drives which IL formula runs, not protocol_id.

IL math by AMM type:
  - CL_AMM / XYK_AMM / STABLE_AMM: LP_value - hold_value (adapter computes LP_value)
  - LENDING / STAKING:              no IL; returns (0, cost_basis)
  - VAULT:                          no IL at the user level (single share token)
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Position, ProtocolKind

_log = logging.getLogger(__name__)

# ── Cost basis ────────────────────────────────────────────────────────────


@dataclass
class CostBasis:
    """
    Derived from the event log for a position.

    cost_basis_usd — net USD deposited (deposits − withdrawals at deposit-time prices).
    net_tokens     — same in token units; used for HODL benchmark at today's prices.
    event_count    — rows the roll-up was computed from. Zero means "no history",
                     which is unknowable cost basis — NOT a zero-cost position.
    """

    cost_basis_usd: Decimal
    fees_collected_usd: Decimal  # lifetime COLLECT events
    net_tokens: dict[str, Decimal]  # token_key → net amount
    event_count: int = 0

    @property
    def has_events(self) -> bool:
        return self.event_count > 0


def compute_cost_basis(
    storage: Storage, wallet: str, chain, protocol_id: str, position_key: str
) -> CostBasis:
    """
    Walk the event log for one position and roll up cost basis + net tokens.

    Event.usd_at_ts is signed: +deposit, -withdraw.
    COLLECT events are tracked separately — they're realized PnL, not cost basis.
    """
    events = storage.events_for_position(wallet, chain, protocol_id, position_key)

    cost_basis_usd = Decimal("0")
    fees_collected_usd = Decimal("0")
    net_tokens: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    for ev in events:
        kind = ev["kind"]
        usd = Decimal(str(ev["usd_at_ts"]))
        amts = json.loads(ev["amounts_json"])

        if kind in ("deposit", "withdraw"):
            cost_basis_usd += usd
            for a in amts:
                net_tokens[a["token_key"]] += Decimal(a["amount"])
        elif kind == "collect":
            fees_collected_usd += abs(usd)

    return CostBasis(
        cost_basis_usd=cost_basis_usd,
        fees_collected_usd=fees_collected_usd,
        net_tokens=dict(net_tokens),
        event_count=len(events),
    )


# ── HODL benchmark ────────────────────────────────────────────────────────


def hodl_value_now(
    cost_basis: CostBasis,
    current_prices: dict[str, Decimal],
) -> Decimal:
    """What the user would have if they'd held net-deposited tokens instead of LPing."""
    total = Decimal("0")
    for token_key, qty in cost_basis.net_tokens.items():
        price = current_prices.get(token_key)
        if price is None:
            _log.warning(
                "Missing current price for %s — HODL benchmark and IL will be understated",
                token_key,
            )
            continue
        total += qty * price
    return total


# ── IL math ───────────────────────────────────────────────────────────────


def impermanent_loss(
    position: Position,
    cost_basis: CostBasis,
    current_prices: dict[str, Decimal],
) -> tuple[Decimal, Decimal]:
    """
    Returns (il_usd, hold_value_usd).
    il_usd = current LP value − HODL value. Negative means LP underperformed holding.
    """
    hold = hodl_value_now(cost_basis, current_prices)

    if position.protocol_kind in (ProtocolKind.LENDING, ProtocolKind.STAKING):
        return Decimal("0"), cost_basis.cost_basis_usd

    il = position.current_value_usd - hold
    return il, hold


# ── Fee trend + portfolio roll-ups (pure functions over storage rows) ────


def pair_label_from_amounts(
    amounts_json_str: str,
    position_key: str,
    symbols_by_key: dict[str, str] | None = None,
) -> str:
    """Human pair label from an event's amounts payload (symbols recorded at
    event time) — works for closed positions that never got a snapshot.
    Legacy rows without a symbol field resolve via symbols_by_key
    ({token_key: symbol}, from the tokens registry)."""
    lookup = symbols_by_key or {}
    try:
        symbols = [
            a.get("symbol") or lookup.get(a.get("token_key", ""), "?")
            for a in json.loads(amounts_json_str)
        ]
        if symbols:
            return "/".join(symbols)
    except Exception:  # noqa: S110 - malformed legacy rows fall through to key
        pass
    return position_key[:24]


# Fees are harvested as a monthly sweep in the first few days of each month,
# and that sweep is the PRIOR month's accrued fees. So a claim on/before this
# many days into month M is attributed to month M-1. Mid-month collects (dust,
# fees released on withdrawal) accrued in their own calendar month and stay put.
_FEE_ACCRUAL_CUTOFF_DAY = 7


def attribution_month(ts: int, cutoff_day: int = _FEE_ACCRUAL_CUTOFF_DAY) -> str:
    """The fee-accrual month a collect belongs to (YYYY-MM, UTC). A start-of-
    month harvest sweeps the previous month's fees, so claims on day <= cutoff
    roll back to M-1; everything later stays in its own month."""
    from datetime import UTC, datetime, timedelta

    d = datetime.fromtimestamp(ts, UTC)
    if d.day <= cutoff_day:
        return (d.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return d.strftime("%Y-%m")


def attribute_fee_months(
    collect_rows: list[dict], cutoff_day: int = _FEE_ACCRUAL_CUTOFF_DAY
) -> list[dict]:
    """Re-key collect rows so ``month`` is the fee-accrual month (see
    attribution_month); the raw claim month is preserved as ``claim_month``.
    Every downstream fee rollup buckets on ``month``, so applying this once at
    the entry point shifts the whole trend to an accrual basis. Requires ``ts``
    on each row (from Storage.monthly_collects)."""
    out: list[dict] = []
    for r in collect_rows:
        r2 = dict(r)
        r2["claim_month"] = r["month"]
        r2["month"] = attribution_month(int(r["ts"]), cutoff_day)
        out.append(r2)
    return out


def monthly_fee_matrix(
    collect_rows: list[dict],
) -> tuple[list[str], list[str], dict[tuple[str, str], Decimal]]:
    """(sorted months, sorted chains, {(month, chain): fees_usd}) from
    Storage.monthly_collects() rows."""
    cells: defaultdict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for r in collect_rows:
        cells[(r["month"], r["chain"])] += Decimal(str(r["usd_at_ts"]))
    months = sorted({m for m, _ in cells})
    chains = sorted({c for _, c in cells})
    return months, chains, dict(cells)


def is_withdrawal_fee(row: dict) -> bool:
    """True when a COLLECT was derived by netting fees out of a liquidity
    decrease (fees released on withdrawal) rather than a deliberate harvest
    sweep. The runner tags these with meta ``netted_against_decrease``."""
    meta = row.get("meta_json")
    if not meta:
        return False
    try:
        return bool(json.loads(meta).get("netted_against_decrease"))
    except (ValueError, TypeError):
        return "netted_against_decrease" in meta


def fee_netting_split(collect_rows: list[dict]) -> dict:
    """Per-month split of harvest fees into deliberate sweeps vs
    fee-on-withdrawal (netting-derived), so the mix is never folded in
    silently. Returns {months, withdrawal_by_month, sweep_by_month,
    withdrawal_total, sweep_total} — all keyed off the stored usd_at_ts so
    totals reconcile with every other fee figure."""
    withdrawal: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    sweep: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in collect_rows:
        usd = Decimal(str(r["usd_at_ts"]))
        (withdrawal if is_withdrawal_fee(r) else sweep)[r["month"]] += usd
    return {
        "months": sorted(set(withdrawal) | set(sweep)),
        "withdrawal_by_month": dict(withdrawal),
        "sweep_by_month": dict(sweep),
        "withdrawal_total": sum(withdrawal.values(), Decimal("0")),
        "sweep_total": sum(sweep.values(), Decimal("0")),
    }


def fees_by_pair(
    collect_rows: list[dict],
    recent_months: int = 3,
    symbols_by_key: dict[str, str] | None = None,
) -> list[dict]:
    """Lifetime + trailing-N-month fees per pair, sorted by recent then lifetime.
    Pairs are labeled from event token symbols (snapshot-independent)."""
    months = sorted({r["month"] for r in collect_rows})
    recent = set(months[-recent_months:]) if months else set()
    agg: dict[str, dict] = {}
    for r in collect_rows:
        label = pair_label_from_amounts(r["amounts_json"], r["position_key"], symbols_by_key)
        row = agg.setdefault(
            label,
            {
                "pair": label,
                "chain": r["chain"],
                "lifetime": Decimal("0"),
                "recent": Decimal("0"),
                "collects": 0,
            },
        )
        usd = Decimal(str(r["usd_at_ts"]))
        row["lifetime"] += usd
        row["collects"] += 1
        if r["month"] in recent:
            row["recent"] += usd
    return sorted(agg.values(), key=lambda x: (x["recent"], x["lifetime"]), reverse=True)


def monthly_pair_fee_matrix(
    collect_rows: list[dict],
    symbols_by_key: dict[str, str] | None = None,
    months: int = 6,
    top_n: int = 10,
) -> tuple[list[str], list[str], dict[tuple[str, str], Decimal]]:
    """(trailing months, top-N pairs by window fees, {(month, pair): usd})."""
    all_months = sorted({r["month"] for r in collect_rows})
    window = all_months[-months:]
    cells: defaultdict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    totals: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in collect_rows:
        if r["month"] not in window:
            continue
        label = pair_label_from_amounts(r["amounts_json"], r["position_key"], symbols_by_key)
        usd = Decimal(str(r["usd_at_ts"]))
        cells[(r["month"], label)] += usd
        totals[label] += usd
    pairs = [p for p, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top_n]]
    return window, pairs, dict(cells)


def recent_fees_by_position(
    collect_rows: list[dict], recent_months: int = 3
) -> dict[tuple[str, str], Decimal]:
    """{(wallet, position_key): fees collected in the trailing N months} —
    keys match snapshot rows, so callers can annotate live positions with
    fee yield."""
    months = sorted({r["month"] for r in collect_rows})
    window = set(months[-recent_months:]) if months else set()
    out: defaultdict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for r in collect_rows:
        if r["month"] in window:
            out[(r["wallet"], r["position_key"])] += Decimal(str(r["usd_at_ts"]))
    return dict(out)


def fees_earned_mtd(mtd_rows: list[dict]) -> Decimal:
    """Accrual-basis fees earned this month: collected during the month plus
    the change in unclaimed. Distinct from cash collected — a harvest sweep on
    the 1st collects the PRIOR month's accrual and must not read as
    this-month earnings."""
    total = Decimal("0")
    for r in mtd_rows:
        total += Decimal(str(r["fees_collected_mtd"] or 0))
        total += Decimal(str(r["unclaimed_now"] or 0)) - Decimal(
            str(r["unclaimed_month_start"] or 0)
        )
    return total


def realized_closed_pnl(flow_rows: list[dict]) -> dict:
    """Realized result of CLOSED positions from Storage.position_flows():
    fees + withdrawals − deposits, all at event-time prices. Nothing is left
    on-chain for these, so this is locked-in PnL that the open-positions view
    misses entirely."""
    realized = Decimal("0")
    fees = Decimal("0")
    count = 0
    for r in flow_rows:
        if r["is_open"]:
            continue
        realized += -Decimal(str(r["net_invested_usd"])) + Decimal(str(r["fees_usd"]))
        fees += Decimal(str(r["fees_usd"]))
        count += 1
    return {"realized_usd": realized, "fees_usd": fees, "positions": count}


def window_yields(
    window_stats: list[dict],
    collect_rows_since: list[dict],
    window_days: int = 30,
    min_coverage_days: int = 7,
) -> dict[tuple[str, str], dict]:
    """Per open position: realized yield over the trailing window, accrual
    basis — earned = collects in window + (unclaimed_end − unclaimed_start),
    annualized against the AVERAGE value over the window (today's value would
    overstate yield whenever the token has fallen).

    Returns {(wallet, position_key): {earned, avg_value, apy_pct, days}};
    positions with under min_coverage_days of snapshot span get apy_pct=None
    (too little history to annualize honestly)."""
    collected: defaultdict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for c in collect_rows_since:
        collected[(c["wallet"], c["position_key"])] += Decimal(str(c["usd_at_ts"]))

    out: dict[tuple[str, str], dict] = {}
    for w in window_stats:
        key = (w["wallet"], w["position_key"])
        span = int(w["span_days"] or 0)
        earned = (
            collected.get(key, Decimal("0"))
            + Decimal(str(w["unclaimed_end"] or 0))
            - Decimal(str(w["unclaimed_start"] or 0))
        )
        avg_value = Decimal(str(w["avg_value_usd"] or 0))
        apy = None
        if span >= min_coverage_days and avg_value > 0:
            apy = float(earned) / float(avg_value) * 365 / span * 100
        out[key] = {
            "earned": earned,
            "avg_value": avg_value,
            "apy_pct": apy,
            "days": span,
            "pair_label": w["pair_label"],
        }
    return out


def fee_valuation(collect_rows: list[dict], price_today: dict[str, Decimal] | None = None) -> dict:
    """Fees valued two ways: at harvest (each collect at its claim-day price —
    the standard "fee generation" measure) and marked to today's token prices
    (what those fee tokens are worth now, if held rather than sold at claim).

    A large gap means fee income is concentrated in a token that has since
    moved — a risk signal the single harvest number hides. price_today maps
    token_key → current USD; tokens absent from it fall back to claim value
    (i.e. assumed flat) so a missing price never silently zeroes a month.
    """
    price_today = price_today or {}
    claim_by_month: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    today_by_month: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    claim_by_token: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    today_by_token: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    sym_by_token: dict[str, str] = {}
    priced = bool(price_today)

    for r in collect_rows:
        m = r["month"]
        # Month claim total uses the authoritative stored usd_at_ts so it
        # matches every other fee figure in the app.
        claim_by_month[m] += Decimal(str(r["usd_at_ts"]))
        for a in json.loads(r["amounts_json"]):
            tk = a["token_key"]
            amt = Decimal(str(a["amount"]))
            claim = amt * Decimal(str(a.get("price_at_ts", "0") or "0"))
            tp = price_today.get(tk)
            today_by_month[m] += amt * tp if tp is not None else claim
            claim_by_token[tk] += claim
            today_by_token[tk] += amt * tp if tp is not None else claim
            sym_by_token[tk] = a.get("symbol") or tk

    drops = sorted(
        ((sym_by_token[tk], claim_by_token[tk], today_by_token[tk]) for tk in claim_by_token),
        key=lambda t: t[1] - t[2],
        reverse=True,
    )
    return {
        "months": sorted(claim_by_month),
        "claim_by_month": dict(claim_by_month),
        "today_by_month": dict(today_by_month),
        "claim_total": sum(claim_by_month.values(), Decimal("0")),
        "today_total": sum(today_by_month.values(), Decimal("0")),
        "by_token_drop": drops,  # [(symbol, claim, today)] biggest $ drop first
        "priced": priced,
    }


def pnl_decomposition(snap_rows: list[dict]) -> dict:
    """Split net PnL into its drivers, over latest-snapshot rows.

    net = market_move + il + fees_lifetime + unclaimed, where
      market_move = hold − cost basis  (what holding the deposits did)
      il          = value − hold       (what LPing did vs holding)
    Rows with NULL analytics (no event history) are excluded and counted.
    """
    market = il = fees = unclaimed = net = Decimal("0")
    excluded = 0
    for r in snap_rows:
        if r["pnl_usd"] is None or r["cost_basis_usd"] is None:
            excluded += 1
            continue
        cost = Decimal(str(r["cost_basis_usd"]))
        hold = Decimal(str(r["hold_value_usd"] or 0))
        value = Decimal(str(r["current_value_usd"]))
        unc = Decimal(str(r["unclaimed_usd"] or 0))
        pnl = Decimal(str(r["pnl_usd"]))
        market += hold - cost
        il += Decimal(str(r["il_usd"] or 0))
        unclaimed += unc
        # pnl = value + unclaimed + fees_collected − cost  →  solve for fees
        fees += pnl - value - unc + cost
        net += pnl
    return {
        "market_move": market,
        "il": il,
        "fees_lifetime": fees,
        "unclaimed": unclaimed,
        "net": net,
        "excluded": excluded,
    }


def risk_summary(snap_rows: list[dict]) -> dict:
    """Portfolio risk snapshot: idle (out-of-range) capital and concentration."""
    total = sum(Decimal(str(r["current_value_usd"])) for r in snap_rows) or Decimal("1")
    oor = [r for r in snap_rows if r.get("in_range") == 0]
    oor_value = sum(Decimal(str(r["current_value_usd"])) for r in oor)
    by_pair: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for r in snap_rows:
        by_pair[r["pair_label"]] += Decimal(str(r["current_value_usd"]))
    top_pair, top_pair_value = max(
        by_pair.items(), key=lambda kv: kv[1], default=("—", Decimal("0"))
    )
    largest = max(snap_rows, key=lambda r: r["current_value_usd"], default=None)
    return {
        "total_value": total,
        "out_of_range_value": oor_value,
        "out_of_range_count": len(oor),
        "out_of_range_pct": oor_value / total * 100,
        "top_pair": top_pair,
        "top_pair_value": top_pair_value,
        "top_pair_pct": top_pair_value / total * 100,
        "largest_position_label": largest["pair_label"] if largest else "—",
        "largest_position_value": Decimal(str(largest["current_value_usd"]))
        if largest
        else Decimal("0"),
    }


@dataclass
class RebalanceConfig:
    """Thresholds for the economic rebalance signal. depth is how far price has
    drifted beyond the range bound, in units of the position's own range width —
    so it is width-aware for free: a deliberately-wide range needs a far bigger
    absolute move to reach the same depth."""

    idle_min_usd: Decimal = Decimal("5000")  # ignore dust below this
    dead_yield_pct: float = 5.0  # trailing yield under this = not earning
    depth_soon: float = 0.15  # ≥ this (and dead) → 🟠 rebalance soon
    depth_now: float = 0.50  # ≥ this (and dead) → 🔴 rebalance now
    long_dead_days: int = 30  # days out of range that escalates one tier
    # Range-target sizing: half-width = target_z · σ_ticks · √horizon. z=2.5
    # over 30d leans wide (fat-tailed alts + thin history); tune per posture.
    target_z: float = 2.5
    target_horizon_days: int = 30
    vol_min_points: int = 5  # need ≥ this many daily returns to suggest


DEFAULT_REBALANCE_CONFIG = RebalanceConfig()


def pool_tick_volatility(
    history_rows: list[dict], min_points: int = 5
) -> dict[tuple[str, str], float]:
    """{(chain, pair_label): σ_ticks_per_day} from the daily snapshot
    tick_current series. price = 1.0001^tick, so a day's Δtick IS that day's
    log-return — its stdev is realized volatility in tick units, no price
    conversion needed. Pools with < min_points daily returns are omitted (too
    little history to size a range honestly)."""
    import statistics as _st

    series: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for r in history_rows:
        tc = r.get("tick_current")
        if tc is None or r.get("is_closed"):
            continue
        # First (or any) position's tick that day — pool-wide, so identical.
        series[(r["chain"], r["pair_label"])].setdefault(r["snapshot_date"], int(tc))
    out: dict[tuple[str, str], float] = {}
    for key, by_date in series.items():
        ticks = [by_date[d] for d in sorted(by_date)]
        deltas = [ticks[i] - ticks[i - 1] for i in range(1, len(ticks))]
        if len(deltas) >= min_points:
            out[key] = _st.pstdev(deltas)
    return out


def suggest_range(
    tick_current: int, sigma_ticks: float | None, z: float = 2.5, horizon_days: int = 30
) -> dict | None:
    """Symmetric range around the current tick sized to z·σ·√horizon in tick
    (log-price) space. Returns {half_ticks, lower, upper, half_pct} — half_pct
    is the ± width in price terms — or None when inputs are unusable."""
    import math

    if not sigma_ticks or sigma_ticks <= 0 or tick_current is None:
        return None
    half = int(round(z * sigma_ticks * math.sqrt(horizon_days)))
    return {
        "half_ticks": half,
        "lower": tick_current - half,
        "upper": tick_current + half,
        "half_pct": (math.pow(1.0001, half) - 1) * 100,
    }


def rebalance_signals(
    snap_rows: list[dict],
    yields: dict[tuple[str, str], dict],
    last_in_range: dict[str, str] | None = None,
    config: RebalanceConfig | None = None,
    today=None,
    tick_vol: dict[tuple[str, str], float] | None = None,
) -> list[dict]:
    """Economic rebalance signals for out-of-range positions, tiered by urgency.

    Gate: out of range (from ticks, not the stored flag) · idle ≥ idle_min ·
    known ticks. Tier from depth (width-normalized distance beyond bound) when
    the capital is 'dead' (trailing yield < dead_yield_pct); positions still
    catching fees stay 'watch'. days-out escalates one tier once it is both
    known and ≥ long_dead_days (it refines, never gates — snapshot history is
    shallow and null for perpetually-out positions).

    Returns dicts {wallet, position_key, pair, tier ('now'|'soon'|'watch'),
    idle_usd, depth, days_out, yield_pct, forgone_usd}, sorted now→soon→watch
    then by idle desc. forgone_usd = idle × the book's value-weighted in-range
    yield (opportunity cost of leaving it idle)."""
    from datetime import UTC, datetime

    config = config or DEFAULT_REBALANCE_CONFIG
    last_in_range = last_in_range or {}
    tick_vol = tick_vol or {}
    if today is None:
        today = datetime.now(UTC).date()

    # Benchmark = value-weighted trailing yield of in-range positions.
    num = den = 0.0
    for r in snap_rows:
        if r.get("in_range") == 1:
            y = yields.get((r["wallet"], r["position_key"]))
            if y and y["apy_pct"] is not None:
                v = float(r["current_value_usd"])
                num += v * y["apy_pct"]
                den += v
    benchmark = num / den if den else 0.0

    signals: list[dict] = []
    for r in snap_rows:
        lo, hi, cur = r.get("tick_lower"), r.get("tick_upper"), r.get("tick_current")
        if lo is None or hi is None or cur is None:
            continue  # non-CL or ticks unknown
        width = hi - lo
        if width <= 0 or lo <= cur <= hi:
            continue  # degenerate or in range
        idle = float(r["current_value_usd"])
        if idle < float(config.idle_min_usd):
            continue
        beyond = (lo - cur) if cur < lo else (cur - hi)
        depth = beyond / width
        y = yields.get((r["wallet"], r["position_key"]))
        yld = y["apy_pct"] if y else None
        dead = yld is None or yld < config.dead_yield_pct
        seen = last_in_range.get(r["position_key"])
        days_out = (today - datetime.strptime(seen, "%Y-%m-%d").date()).days if seen else None

        if not dead:
            tier = "watch"
        elif depth >= config.depth_now:
            tier = "now"
        elif depth >= config.depth_soon:
            tier = "soon"
        else:
            tier = "watch"
        if dead and days_out is not None and days_out >= config.long_dead_days:
            tier = {"watch": "soon", "soon": "now", "now": "now"}[tier]

        # Suggested retarget range from the pool's realized tick volatility.
        current_half_pct = (pow(1.0001, width / 2) - 1) * 100
        suggest = suggest_range(
            cur,
            tick_vol.get((r["chain"], r["pair_label"])),
            config.target_z,
            config.target_horizon_days,
        )

        signals.append(
            {
                "uid": f"{r['wallet']}:{r['chain']}:{r['protocol_id']}:{r['position_key']}",
                "wallet": r["wallet"],
                "position_key": r["position_key"],
                "pair": r["pair_label"],
                "tier": tier,
                "idle_usd": idle,
                "depth": depth,
                "days_out": days_out,
                "yield_pct": yld,
                "forgone_usd": idle * benchmark / 100.0,
                "current_half_pct": current_half_pct,
                "suggest": suggest,  # {half_ticks, lower, upper, half_pct} or None
            }
        )

    order = {"now": 0, "soon": 1, "watch": 2}
    signals.sort(key=lambda s: (order[s["tier"]], -s["idle_usd"]))
    return signals


# ── Full PnL roll-up ──────────────────────────────────────────────────────


@dataclass
class PnLBreakdown:
    cost_basis_usd: Decimal
    current_value_usd: Decimal
    unclaimed_usd: Decimal
    fees_collected_usd: Decimal
    hold_value_usd: Decimal
    il_usd: Decimal  # current − hold (signed)
    il_pct: Decimal  # vs hold value
    pnl_usd: Decimal  # current + unclaimed + fees_collected − cost_basis
    pnl_pct: Decimal  # vs cost basis


def compute_pnl(
    position: Position,
    cost_basis: CostBasis,
    current_prices: dict[str, Decimal],
) -> PnLBreakdown:
    """The full picture for one position at one moment."""
    il_usd, hold = impermanent_loss(position, cost_basis, current_prices)
    il_pct = (il_usd / hold * 100) if hold > 0 else Decimal("0")

    pnl = (
        position.current_value_usd
        + position.unclaimed_usd
        + cost_basis.fees_collected_usd
        - cost_basis.cost_basis_usd
    )
    pnl_pct = (
        (pnl / cost_basis.cost_basis_usd * 100) if cost_basis.cost_basis_usd > 0 else Decimal("0")
    )

    return PnLBreakdown(
        cost_basis_usd=cost_basis.cost_basis_usd,
        current_value_usd=position.current_value_usd,
        unclaimed_usd=position.unclaimed_usd,
        fees_collected_usd=cost_basis.fees_collected_usd,
        hold_value_usd=hold,
        il_usd=il_usd,
        il_pct=il_pct,
        pnl_usd=pnl,
        pnl_pct=pnl_pct,
    )
