"""
Analytics layer: derives cost basis, IL, and PnL from event history.

Protocol-agnostic — adapters know how to get data; analytics knows what to
do with it. ProtocolKind drives which IL formula runs, not protocol_id.

IL math by AMM type:
  - CL_AMM / XYK_AMM / STABLE_AMM: LP_value - hold_value (adapter computes LP_value)
  - LENDING / STAKING:              no IL; returns (0, cost_basis)
  - VAULT:                          LP_value - hold_value, like an AMM (a vault
                                    over LP positions carries their IL)
"""

from __future__ import annotations

import json
import logging
import math
import os
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
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
    event_count    — deposits/withdrawals the roll-up was computed from. Zero means
                     "no flow history", which is unknowable cost basis — NOT a
                     zero-cost position (collects alone don't count).
    """

    cost_basis_usd: Decimal
    fees_collected_usd: Decimal  # lifetime COLLECT events
    net_tokens: dict[str, Decimal]  # token_key → net amount
    event_count: int = 0

    @property
    def has_events(self) -> bool:
        return self.event_count > 0


def compute_cost_basis(
    storage: Storage,
    wallet: str,
    chain,
    protocol_id: str,
    position_key: str,
    until_ts: int | None = None,
) -> CostBasis:
    """
    Walk the event log for one position and roll up cost basis + net tokens.

    Event.usd_at_ts is signed: +deposit, -withdraw.
    COLLECT events are tracked separately — they're realized PnL, not cost basis.
    """
    events = storage.events_for_position(wallet, chain, protocol_id, position_key, until_ts)

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
        # Flows only: a collect alone proves nothing about what was put in,
        # and has_events is the guard against booking a position whose
        # deposit was never stored as 100% profit.
        event_count=sum(1 for ev in events if ev["kind"] in ("deposit", "withdraw")),
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


def month_range(first: str, last: str) -> list[str]:
    """Every 'YYYY-MM' from first to last inclusive, with no gaps.

    Fee tables derive their month axis from the collect rows, so a month in
    which nothing was claimed simply vanishes from the table — which reads as
    "no fees earned" when the truth is "fees earned, not yet swept". Driving
    the axis off this contiguous range instead makes an empty month show up as
    an explicit zero. Returns [] when first > last (nothing to render).
    """
    fy, fm = (int(x) for x in first.split("-"))
    ly, lm = (int(x) for x in last.split("-"))
    start, end = fy * 12 + (fm - 1), ly * 12 + (lm - 1)
    return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(start, end + 1)]


# ── Sweep detection ───────────────────────────────────────────────────────
#
# A "sweep" is a DELIBERATE harvest of the book, not any collect. Getting this
# wrong is expensive: taking the latest collect as the sweep lets a single dust
# claim reset the date, and the report then divides a large accruing balance by
# a handful of days and implies a fee run-rate an order of magnitude too high.
#
# The constants below are read off a live collect distribution (several
# months of non-withdrawal collects across peaq and bsc), not invented:
#
# _SWEEP_BATCH_GAP_HOURS — sorted gaps between consecutive collects have a wide
#   empty valley: the largest gap INSIDE a harvest is just under 10h (one that
#   also straddled midnight), and the smallest gap between a harvest and an
#   unrelated later collect (a dust claim the next day) is over 23h. Nothing at
#   all lies in 10h…23h, so 12h splits the valley. A time window rather than a
#   calendar-day bucket is what keeps a midnight-straddling harvest as one sweep.
#
# _SWEEP_MIN_POSITIONS — harvests touch the book broadly: every monthly sweep in
#   the data collects from at least 3 distinct positions, while every incidental
#   collect (dust, a single stray claim) touches 1–2. 3 is the cut between the
#   two observed populations.
#
# _SWEEP_BOOK_SHARE — breadth for a book too small to reach _SWEEP_MIN_POSITIONS.
#   A harvest is defined by COVERAGE, not size: collecting from half the open
#   book is a sweep whatever it yields.
#
# Materiality is deliberately NOT part of this rule. An earlier version gated on
# a share of the median historical harvest, which cannot survive the book
# changing size: when the book shrank to a couple of positions, a genuine small
# harvest fell under a floor set by the much larger historical median, last_sweep
# froze at the previous month, and the report divided a two-week accrual over
# six weeks — the same class
# of wrong number as the dust bug it replaced, in the opposite direction. It
# also made a batch's status depend on data that arrived AFTER it, so past
# harvests were reclassified as the book grew. Coverage is a property of the
# batch itself, so it is stable.
_SWEEP_BATCH_GAP_HOURS = 12
_SWEEP_MIN_POSITIONS = 3
_SWEEP_BOOK_SHARE = 0.5


def _position_identity(row: dict, index: int) -> tuple[str, str]:
    """(wallet, position_key) for breadth counting. Rows without a position key
    count as their own position — the most permissive reading, so a caller that
    does not carry position identity is never penalised on breadth."""
    pk = row.get("position_key")
    if not pk:
        return ("", f"#{index}")
    return (str(row.get("wallet") or ""), str(pk))


def collect_batches(
    collect_rows: list[dict], gap_hours: int = _SWEEP_BATCH_GAP_HOURS
) -> list[dict]:
    """Group collects into contiguous harvest batches: a new batch starts
    wherever more than gap_hours passes between consecutive collects.

    Returns [{start_ts, ts, usd, positions, rows}] in time order. A time window
    (not a UTC calendar day) is deliberate — a harvest that runs past
    midnight would otherwise be reported only by its tail.
    """
    ordered = sorted(enumerate(collect_rows), key=lambda t: int(t[1]["ts"]))
    if not ordered:
        return []

    gap = gap_hours * 3600
    groups: list[list[tuple[int, dict]]] = [[]]
    for idx, r in ordered:
        if groups[-1] and int(r["ts"]) - int(groups[-1][-1][1]["ts"]) > gap:
            groups.append([])
        groups[-1].append((idx, r))

    return [
        {
            "start_ts": int(g[0][1]["ts"]),
            "ts": int(g[-1][1]["ts"]),
            "usd": sum((Decimal(str(r["usd_at_ts"])) for _, r in g), Decimal("0")),
            "positions": len({_position_identity(r, i) for i, r in g}),
            "rows": [r for _, r in g],
        }
        for g in groups
    ]


def sweep_batches(
    collect_rows: list[dict],
    gap_hours: int = _SWEEP_BATCH_GAP_HOURS,
    min_positions: int = _SWEEP_MIN_POSITIONS,
    open_positions: int | None = None,
) -> list[dict]:
    """The batches that are deliberate harvest sweeps, in time order.

    Fee-on-withdrawal collects are never sweeps (see is_withdrawal_fee) and are
    dropped first. A remaining batch qualifies on COVERAGE: it touches at least
    min_positions distinct positions, or — when the caller knows how many
    positions are actually open — at least half of them. Amount is deliberately
    irrelevant: harvesting the whole book is a harvest whatever it yields, and
    any size threshold has to be relative to something that moves
    as the book does (see the note above _SWEEP_BATCH_GAP_HOURS).

    open_positions rescues the small-book case that breadth alone misses: a book
    down to two positions can never reach min_positions, but collecting both of
    them is plainly a sweep. Pass the current open-position count; leave it None
    and only the absolute breadth test applies.
    """
    rows = [r for r in collect_rows if not is_withdrawal_fee(r)]
    batches = collect_batches(rows, gap_hours)
    if not batches:
        return []

    covers_book = None
    if open_positions:
        covers_book = max(1, math.ceil(_SWEEP_BOOK_SHARE * open_positions))

    return [
        b
        for b in batches
        if b["usd"] > 0
        and (
            b["positions"] >= min_positions
            or (covers_book is not None and b["positions"] >= covers_book)
        )
    ]


def last_sweep(
    collect_rows: list[dict], now: int | None = None, open_positions: int | None = None
) -> dict | None:
    """The most recent deliberate harvest sweep: {ts, date, days_ago, usd}.

    The pending "accruing" bucket is everything unclaimed since this sweep, so
    labelling it with the CURRENT month is wrong whenever a sweep was missed —
    it silently relabels the prior month's accrual. Reports should say "since
    <date>, N days" instead, which needs this.

    ``usd`` is the WHOLE batch, and ``ts``/``date`` are its last collect, so
    days_ago never overstates how long the book has gone unharvested. See
    sweep_batches for what separates a harvest from an incidental collect —
    a dust claim is not a sweep and must not reset this date. Returns None when
    there is no deliberate harvest on record.
    """
    from datetime import UTC, datetime

    sweeps = sweep_batches(collect_rows, open_positions=open_positions)
    if not sweeps:
        return None

    batch = sweeps[-1]
    day = datetime.fromtimestamp(batch["ts"], UTC).date()
    now_ts = int(datetime.now(UTC).timestamp()) if now is None else int(now)
    days_ago = (datetime.fromtimestamp(now_ts, UTC).date() - day).days
    return {
        "ts": batch["ts"],
        "date": day.strftime("%Y-%m-%d"),
        "days_ago": days_ago,
        "usd": batch["usd"],
    }


def rolled_up_months(collect_rows: list[dict], open_positions: int | None = None) -> dict[str, str]:
    """{accrual month with no sweep of its own: month of the sweep that covered it}.

    A harvest sweeps everything accrued since the PREVIOUS harvest, but
    attribution_month can only roll a claim back one month. So when a monthly
    sweep is skipped, the catch-up sweep carries two months of fees and the
    skipped month is left with no claims at all — and a contiguous month axis
    then renders it as an explicit $0.

    Example: sweeps on July 1 (accrual June) and September 1 (accrual
    August), two months apart. July's fees were real and WERE harvested, inside
    the September sweep. Reporting July as $0 states the opposite, and it never
    self-corrects. Reporting it as "included in August" is the truth we can
    actually support without inventing a split between the two months.

    Only the months strictly between two consecutive sweeps' accrual months are
    returned; a month before the first sweep on record has no earlier boundary
    to measure the gap against, so it is left alone.
    """
    sweeps = sweep_batches(collect_rows, open_positions=open_positions)
    out: dict[str, str] = {}
    for prev, cur in zip(sweeps, sweeps[1:], strict=False):
        a_prev = attribution_month(int(prev["ts"]))
        a_cur = attribution_month(int(cur["ts"]))
        # month_range is inclusive at both ends; drop the two boundary months,
        # which have sweeps of their own.
        for m in month_range(a_prev, a_cur)[1:-1]:
            out[m] = a_cur
    return out


def unswept_months(
    collect_rows: list[dict], now: int | None = None, open_positions: int | None = None
) -> list[str]:
    """The fee-ACCRUAL months that have not been harvested yet ('YYYY-MM').

    The fee table is bucketed on accrual month and its axis is contiguous
    (month_range), so a month whose fees are still sitting unclaimed renders as
    an explicit $0 — a positive false claim that nothing was earned. Worse,
    attribution_month only rolls back one month, so a July accrual finally swept
    in September lands in August and July stays $0 forever.

    These months must render as unswept/pending (— , pointing at the accruing
    row), never as $0. A month BEFORE the last sweep with no claims genuinely
    earned nothing and correctly renders $0.

    The range runs from the accrual month following the last real sweep through
    the current month; it is empty when the last sweep already covers the
    current accrual month, and spans the whole collect history when nothing has
    ever been swept.
    """
    from datetime import UTC, datetime

    if not collect_rows:
        return []  # no collect history: nothing is known to be unswept

    now_ts = int(datetime.now(UTC).timestamp()) if now is None else int(now)
    current = datetime.fromtimestamp(now_ts, UTC).strftime("%Y-%m")

    sweep = last_sweep(collect_rows, now=now_ts, open_positions=open_positions)
    if sweep is not None:
        first = _next_month(attribution_month(int(sweep["ts"])))
    else:
        # Nothing was ever harvested: everything from the first accrual on record.
        first = min(attribution_month(int(r["ts"])) for r in collect_rows)
    return month_range(first, current)


def _next_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


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


def fees_earned_mtd(mtd_rows: list[dict]) -> Decimal:
    """Accrual-basis fees earned this month: collected during the month plus
    the change in unclaimed. Distinct from cash collected — a harvest sweep on
    the 1st collects the PRIOR month's accrual and must not read as
    this-month earnings. A row whose unclaimed could not be read at either
    end contributes only what it collected — unread is not $0."""
    total = Decimal("0")
    for r in mtd_rows:
        total += Decimal(str(r["fees_collected_mtd"] or 0))
        if r["unclaimed_now"] is not None and r["unclaimed_month_start"] is not None:
            total += Decimal(str(r["unclaimed_now"])) - Decimal(str(r["unclaimed_month_start"]))
    return total


def realized_closed_pnl(flow_rows: list[dict]) -> dict:
    """Realized result of CLOSED positions from Storage.position_flows():
    fees + withdrawals − deposits, all at event-time prices.

    A position counts as closed when no live snapshot remains, but that alone
    cannot tell an actual exit from MISSING DATA: a deposit whose adapter was
    later disabled is never snapshotted again and looks byte-for-byte like a
    total loss, booking its whole cost basis as a realized loss. So closed
    positions are split on ``withdraw_count`` — evidence of an exit in the
    event log:

      VERIFIED   (withdraw_count >= 1) — really exited; feeds book net.
      UNVERIFIED (withdraw_count == 0) — deposits with no exit on record.
                 Capital unaccounted for, NOT a loss; reported separately so
                 the headline figure is never the residual of unverified
                 gross items.

    A row missing ``withdraw_count`` entirely (legacy / pre-contract storage)
    is unverified: absence of evidence is exactly the case this guards.
    ``pair_label`` is required on unverified rows — a missing label means the
    storage contract is broken and must surface, not be papered over.
    """
    realized = Decimal("0")
    fees = Decimal("0")
    count = 0
    unverified: list[dict] = []
    unverified_usd = Decimal("0")

    for r in flow_rows:
        if r["is_open"]:
            continue
        net_invested = Decimal(str(r["net_invested_usd"]))
        if int(r.get("withdraw_count") or 0) < 1:
            unverified_usd += net_invested
            unverified.append(
                {
                    "pair_label": r["pair_label"],
                    "wallet": r["wallet"],
                    "position_key": r["position_key"],
                    "net_invested_usd": net_invested,
                }
            )
            continue
        realized += -net_invested + Decimal(str(r["fees_usd"]))
        fees += Decimal(str(r["fees_usd"]))
        count += 1

    unverified.sort(key=lambda u: u["net_invested_usd"], reverse=True)
    return {
        "realized_usd": realized,
        "fees_usd": fees,
        "positions": count,
        "unverified_positions": len(unverified),
        "unverified_usd": unverified_usd,
        "unverified": unverified,
    }


def position_id(r: dict) -> tuple[str, str, str, str]:
    """A row's primary key — (wallet, chain, protocol_id, position_key). Never
    key by position_key alone, or by wallet + position_key: uniswap_v3 keys
    are pool:lo:hi, shared by any wallet in the same range."""
    return (r["wallet"], r["chain"], r["protocol_id"], r["position_key"])


def window_yields(
    window_stats: list[dict],
    collect_rows_since: list[dict],
    window_days: int = 30,
    min_coverage_days: int = 7,
) -> dict[tuple[str, str, str, str], dict]:
    """Per open position: realized yield over the trailing window, accrual
    basis — earned = collects in window + (unclaimed_end − unclaimed_start),
    annualized against the AVERAGE value over the window (today's value would
    overstate yield whenever the token has fallen).

    Returns {(wallet, chain, protocol_id, position_key): {earned, avg_value,
    apy_pct, days}} — see `position_id`;
    positions with under min_coverage_days of snapshot span get apy_pct=None
    (too little history to annualize honestly)."""
    collected: defaultdict[tuple[str, str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for c in collect_rows_since:
        collected[position_id(c)] += Decimal(str(c["usd_at_ts"]))

    out: dict[tuple[str, str, str, str], dict] = {}
    for w in window_stats:
        key = position_id(w)
        span = int(w["span_days"] or 0)
        earned = (
            collected.get(key, Decimal("0"))
            + Decimal(str(w["unclaimed_end"] or 0))
            - Decimal(str(w["unclaimed_start"] or 0))
        )
        avg_value = Decimal(str(w["avg_value_usd"] or 0))
        # An unreadable endpoint is stored as a placeholder 0. Differencing it
        # against a real one invents a swing the size of the whole unclaimed
        # balance — a fabricated yield, which is worse than no yield. `.get`
        # with a default of 1 keeps pre-column rows behaving as before.
        endpoints_known = bool(w.get("unclaimed_start_known", 1)) and bool(
            w.get("unclaimed_end_known", 1)
        )
        apy = None
        if span >= min_coverage_days and avg_value > 0 and endpoints_known:
            apy = float(earned) / float(avg_value) * 365 / span * 100
        out[key] = {
            "earned": earned,
            "avg_value": avg_value,
            "apy_pct": apy,
            "unclaimed_known": endpoints_known,
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


def pnl_decomposition(snap_rows: list[dict], tx_cost_rows: list[dict] | None = None) -> dict:
    """Split net PnL into its drivers, over latest-snapshot rows.

    net = market_move + il + fees_lifetime + unclaimed, where
      market_move = hold − cost basis  (what holding the deposits did)
      il          = value − hold       (what LPing did vs holding)
    Rows with NULL analytics (no event history) are excluded and counted.

    `net` is a position-level number and stays one: it is what the marks and
    the event history say, and transaction costs are not in either. Pass
    `tx_cost_rows` (from `Storage.tx_cost_rows`) to also get `gas`,
    `slippage` and `net_after_costs` — the number a desk actually took home.

    `net_after_costs` is None whenever gas coverage is incomplete. A partial
    cost base subtracted from a complete PnL is a better-looking number than
    the truth, which is the one thing it must not be.
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
    out = {
        "market_move": market,
        "il": il,
        "fees_lifetime": fees,
        "unclaimed": unclaimed,
        "net": net,
        "excluded": excluded,
        "gas": None,
        "slippage": None,
        "net_after_costs": None,
        "costs": None,
    }
    if tx_cost_rows is not None:
        costs = cost_summary(tx_cost_rows)
        out["costs"] = costs
        out["gas"] = costs["gas_usd"]
        out["slippage"] = costs["slippage_usd"]
        if costs["gas_is_complete"]:
            out["net_after_costs"] = (
                net - (costs["gas_usd"] or Decimal("0")) - (costs["slippage_usd"] or Decimal("0"))
            )
    return out


def cost_summary(tx_cost_rows: list[dict]) -> dict:
    """Realized transaction costs over the rows `Storage.tx_cost_rows` returns.

    Gas is summed per TRANSACTION, because that is what it is paid per: a
    rebalance that burns and mints in one transaction pays once, and summing
    a per-event copy would overstate exactly the busiest transactions.

    Coverage is part of the answer, not a footnote. `gas_known_txs` /
    `txs` says how much of the book's transaction history the total actually
    covers, so a reader can tell "$412 of gas" from "$412 of gas over the 6
    transactions we happen to have receipts for".

    Slippage is only ever summed from measured values. Nothing here infers
    it: a mint or a burn has no slippage, and the swap leg of a zap is not
    in `events`, so `slippage_known_txs` is 0 until something can measure it.
    """
    gas = Decimal("0")
    slippage = Decimal("0")
    gas_known = slippage_known = 0
    for r in tx_cost_rows:
        if r.get("gas_usd") is not None:
            gas += Decimal(str(r["gas_usd"]))
            gas_known += 1
        if r.get("slippage_usd") is not None:
            slippage += Decimal(str(r["slippage_usd"]))
            slippage_known += 1
    txs = len(tx_cost_rows)
    return {
        "gas_usd": gas if gas_known else None,
        "slippage_usd": slippage if slippage_known else None,
        "total_usd": (gas + slippage) if (gas_known or slippage_known) else None,
        "txs": txs,
        "gas_known_txs": gas_known,
        "slippage_known_txs": slippage_known,
        "gas_is_complete": txs > 0 and gas_known == txs,
    }


def risk_summary(snap_rows: list[dict]) -> dict:
    """Portfolio risk snapshot: idle (out-of-range) capital and concentration."""
    total = sum((Decimal(str(r["current_value_usd"])) for r in snap_rows), Decimal("0"))
    denom = total or Decimal("1")  # an empty book is 0% of everything, not $1
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
        "out_of_range_pct": oor_value / denom * 100,
        "top_pair": top_pair,
        "top_pair_value": top_pair_value,
        "top_pair_pct": top_pair_value / denom * 100,
        "largest_position_label": largest["pair_label"] if largest else "—",
        "largest_position_value": Decimal(str(largest["current_value_usd"]))
        if largest
        else Decimal("0"),
    }


# A source can blip for a run or two without meaning anything; past this it is
# an outage the reader needs to know about before trusting a number.
_HEALTH_STALE_HOURS = 48


def _age_phrase(seconds: float) -> str:
    """'39d' / '6h' / '12m' — coarse age, for warning copy."""
    if seconds >= 86400:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    return f"{max(0, int(seconds // 60))}m"


def _short_wallet(wallet: str) -> str:
    """0x1234…abcd — enough to identify the wallet in a one-line warning."""
    return f"{wallet[:6]}…{wallet[-4:]}" if len(wallet) > 12 else wallet


def _wallet_suffix(wallets: list[str], max_named: int = 3) -> str:
    """' (wallet 0x1234…abcd)' — names the affected wallets, or '' for none."""
    if not wallets:
        return ""
    shown = [_short_wallet(w) for w in wallets[:max_named]]
    extra = len(wallets) - len(shown)
    if extra > 0:
        shown.append(f"+{extra} more")
    noun = "wallet" if len(wallets) == 1 else "wallets"
    return f" ({noun} {', '.join(shown)})"


def health_warnings(
    health_rows: list[dict],
    now: int | None = None,
    stale_hours: int = _HEALTH_STALE_HOURS,
) -> list[str]:
    """Human-readable degradation lines from Storage.pipeline_health() rows.
    Empty list means every source is current — say nothing in that case.

    A tracker that keeps rendering fresh-looking numbers while a data source is
    down is worse than one that stops: the BSC subgraph failed for 39 days and
    no surface said so, because the RPC fallback keeps snapshot dates current
    and the old staleness check compared only snapshot dates. These lines are
    driven by when each stage last actually SUCCEEDED, which is the one signal
    a fallback cannot fake.

    One line per (chain, protocol_id), reported on the OLDEST success in the
    group — never the most recent. Staleness is NOT uniform across wallets: the
    machinex adapter keeps a per-wallet block cursor and its per-run block cap
    applies per wallet, so one wallet can fall weeks behind while a sibling is
    current. Collapsing onto the freshest wallet hid exactly that, silencing a
    39-day-stale wallet holding six figures. When wallets disagree the affected
    ones are named; when they are uniform the line stays wallet-free so the
    common case is not spammed one line per wallet.
    """
    from datetime import UTC, datetime

    now_ts = int(datetime.now(UTC).timestamp()) if now is None else int(now)
    cutoff = stale_hours * 3600

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in health_rows:
        grouped[(r["chain"], r["protocol_id"])].append(r)

    def _when(ts: int | None) -> str:
        # A missing timestamp means none was RECORDED — which is also what a
        # freshly-added health table looks like. "has never succeeded" is a
        # claim about the pipeline that the absence of a row cannot support.
        if ts is None:
            return "has no recorded success"
        d = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
        return f"last succeeded {_age_phrase(now_ts - ts)} ago ({d})"

    def _stale(rows: list[dict], field: str) -> tuple[bool, int | None, list[str]]:
        """(is_stale, oldest_ts, wallets to name) for one stage of a group.

        Reported on the OLDEST success, and a None timestamp counts as older
        than any real one — unknown is not evidence of health. Wallets are named
        only when they DISAGREE (some behind, some current); when every wallet
        in the group is behind it is a source-wide outage and the line stays
        wallet-free."""
        seen = [(r.get(field), str(r.get("wallet") or "")) for r in rows]
        bad = [(ts, w) for ts, w in seen if ts is None or now_ts - int(ts) > cutoff]
        if not bad:
            return False, None, []
        # A KNOWN stale timestamp always wins over an unknown one. Letting a
        # single NULL sibling collapse the group to "unknown" threw away the
        # only number the reader needs — live, that turned a real 40-day BSC
        # outage into "has no recorded success" and downgraded the snapshot
        # alarm from 🚨 to ℹ️. Unknown rows are strictly worse than any known
        # one, so they never improve the verdict; they just cannot date it.
        known = [int(ts) for ts, _ in bad if ts is not None]
        oldest = min(known) if known else None
        named = [] if len(bad) == len(seen) else sorted({w for _, w in bad if w})
        return True, oldest, named

    out: list[str] = []
    for (chain, pid), rows in sorted(grouped.items()):
        label = f"{chain}/{pid}"

        snap_stale, snap_ts, snap_wallets = _stale(rows, "last_snapshot_ok_at")
        if snap_stale:
            # "every value is stale" is only supportable against a KNOWN old
            # timestamp. With no record at all we know nothing — including
            # whether there are any values — so say that and stay quiet about
            # the rest. Over-claiming on healthy data discredits the warning
            # exactly as much as missing a real outage.
            out.append(
                f"🚨 {label}{_wallet_suffix(snap_wallets)} — position snapshot "
                f"{_when(snap_ts)}; every value shown for it is stale"
                if snap_ts is not None
                else f"ℹ️ {label}{_wallet_suffix(snap_wallets)} — position snapshot "
                "has no recorded success yet (populates on the next run)"
            )

        sync_stale, sync_ts, sync_wallets = _stale(rows, "last_sync_ok_at")
        if sync_stale:
            out.append(
                f"⚠️ {label}{_wallet_suffix(sync_wallets)} — event sync "
                f"{_when(sync_ts)}; deposits, withdrawals and claims "
                "since then are missing from cost basis, fees and yields"
            )

        # Events fetched on the last pass that could not be stored. Distinct
        # from a stale sync: the scan ran and reached head, it just could not
        # convert some of what it found — and the cursor has already moved past
        # them, so nothing re-requests them. `.get` because callers hand-build
        # these dicts and older rows predate the column.
        dropped = sum(int(r.get("dropped_events_last_run") or 0) for r in rows)
        if dropped:
            drop_wallets = sorted(
                {
                    str(r.get("wallet") or "")
                    for r in rows
                    if int(r.get("dropped_events_last_run") or 0)
                }
            )
            named = [] if len(drop_wallets) == len(rows) else [w for w in drop_wallets if w]
            recover = (
                f"`defi-tracker reset-cursor --chain {chain}` then re-run"
                if chain == "peaq"
                else f"rewinding the {chain} sync watermark (see scripts/backfill_*)"
            )
            out.append(
                f"⚠️ {label}{_wallet_suffix(named)} — {dropped} on-chain event(s) were "
                "fetched but could not be recorded on the last run; the scan cursor has "
                "moved past them, so they will not be re-requested. The run log has the "
                f"reason per event; recover with {recover}"
            )

        # rpc_fallback anywhere degrades the whole source, but name the wallets
        # when only some of them fell back.
        fell_back = sorted(
            {str(r.get("wallet") or "") for r in rows if r.get("source") == "rpc_fallback"}
        )
        if fell_back:
            all_back = all(r.get("source") == "rpc_fallback" for r in rows)
            # Says UNAVAILABLE, not "frozen": the fallback used to re-emit the
            # last subgraph-sourced pool TVL and 7d fees on every pass, which
            # re-dated a July figure to today's snapshot. Those fields are now
            # left unknown, and pool-share and carry math sit out rather than
            # run on a stale denominator.
            out.append(
                f"⚠️ {label}{'' if all_back else _wallet_suffix([w for w in fell_back if w])} — "
                "positions reconstructed via RPC fallback (subgraph unavailable); "
                "balances and fees are read live from the chain. Pool TVL is "
                "unavailable; 7-day pool fees come from the pool's Swap logs where "
                "the endpoint can serve them, and a position without them sits out "
                "negative-carry rather than run on a stale denominator"
            )
    return out


@dataclass
class RebalanceConfig:
    """Knobs for the range optimiser and the action queue (Phase 5).

    The decision thresholds are the operator's: min_ev_usd, swap_cost_pct
    and charge_realised_il ship neutral (score everything, assume no swap
    cost, charge no realised IL) and the tickets say what that leaves out.
    min_sigma_obs and depth_bounds are the model's own validity bounds —
    where its answer stops being worth reading — not views on the book."""

    idle_min_usd: Decimal = Decimal("5000")  # positions below this are dust
    target_horizon_days: int = 30
    min_ev_usd: float = 0.0  # a ticket must score above this over the horizon
    # Swap cost as % of notional, the operator's number. None: unknown, and a
    # ticket states the swap cost it breaks even at instead of assuming one.
    # 1.0 (set by the operator): a standing floor — the LP fee of the 1.00%
    # pools where every swap-needing ticket sits. Price impact is on top and
    # not in it; each ticket shows its swap beside the pool's ±2% depth.
    swap_cost_pct: float | None = 1.0
    # Charge the IL a close makes permanent as a cost. Off: under a driftless
    # price its expected recovery is zero and "stay" already carries the IL
    # staying is expected to add, so charging it counts it twice.
    charge_realised_il: bool = False
    min_sigma_obs: int = 30  # fewer daily returns → "not enough history"
    # Pool volume's response to the pool's depth at spot: fees ∝ depth^ε.
    # ε = 0 holds volume fixed (a thinner pool loses no flow); ε = 1 makes it
    # proportional (aggregator-routed flow follows depth). These defaults hold
    # where history is too thin to measure it (measured_rebalance_config
    # replaces them where it can); bands are chosen at the central value and
    # every ticket is also scored at both ends of the bracket, its volume band.
    volume_elasticity: float = 0.5
    elasticity_band: tuple[float, float] = (0.0, 1.0)
    # Pool depth after an action, as a multiple of today's, outside which the
    # depth^ε response is extrapolation and nothing is scored.
    depth_bounds: tuple[float, float] = (0.25, 4.0)
    z_grid: tuple[float, ...] = tuple(round(0.1 * 1.08**i, 4) for i in range(58))  # ×σ√T
    # Which Strategy picks the band a re-range ticket moves to (strategy_from_spec):
    # 'optimiser[:knobs]' (5.1's EV-maximising band), 'sigma:<z>' (±z·σ·√T),
    # 'fixed:<pct>' (±pct% about spot), 'hold'. `defi-tracker backtest --strategy …`
    # compares them. The default was chosen by `backtest --split <date> --grid`
    # on history before the split and adopted because after it it beat hold
    # (with a hit rate of about one in two); the plain optimiser lost in sample.
    # Re-run the grid as history grows — the hit rate is on the line.
    strategy: str = "optimiser:floor=12,minw=50"


DEFAULT_REBALANCE_CONFIG = RebalanceConfig()


_QUOTE_SYMBOLS = frozenset({"USDC", "USDT", "DAI", "BUSD", "USD1", "FDUSD"})


# ── Full PnL roll-up ──────────────────────────────────────────────────────


@dataclass
class PnLBreakdown:
    cost_basis_usd: Decimal
    current_value_usd: Decimal
    unclaimed_usd: Decimal | None  # None = unreadable, NOT zero
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

    # Unreadable unclaimed contributes nothing to PnL rather than making the
    # whole figure unknown — it is normally a small fraction of a position, and
    # blanking IL/PnL over it would lose far more than it protects. The
    # snapshot records that it was unreadable, so the report excludes the
    # position from the unclaimed total and names it instead of quietly
    # counting it as zero.
    unclaimed = position.unclaimed_usd or Decimal("0")
    pnl = (
        position.current_value_usd
        + unclaimed
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


# ── Period PnL: D / WTD / MTD / QTD / YTD / ITD ───────────────────────────
#
# One definition of "what did this window earn", for any window, per position:
#
#   pnl   = (value_end + unclaimed_end) − (value_start + unclaimed_start)
#           − net_flows + fees_collected
#
# where net_flows are the deposits minus withdrawals inside the window (at
# event-time USD) and fees_collected the collects inside it. Without the flow
# term a deposit reads as profit and a withdrawal as a loss — which is what
# `month_to_date_pnl` still does, and why this exists.
#
# It then splits exactly, with no residual:
#
#   pnl = market + il + fees_earned          (and − gas for net_after_gas)
#     fees_earned = fees_collected + Δunclaimed
#     il          = Δil_usd                  (value − hold, per the snapshots)
#     market      = (Δvalue − net_flows) − il
#
# `market` is what the deposited tokens themselves did; `il` is what LPing
# did relative to holding them. A position whose snapshots carry no hold
# value has a known pnl but an unknown split: it is counted in `pnl` and in
# `undecomposed`, never guessed into market or IL.
#
# A position is EXCLUDED, with a stated reason, rather than approximated,
# whenever a term cannot be established: no mark at the start of a window it
# existed at, marks too old at either end, events not synced through the end
# of the window (a deposit could be missing), an unreadable unclaimed balance,
# or no event history at all. Exclusions are reported with the value they
# leave out, the way `ValueDelta` reports its stale positions.

PERIOD_KINDS = ("D", "WTD", "MTD", "QTD", "YTD", "ITD")

# Event sync may lag a window's end by this much before its flows stop being
# trusted — the same tolerance the health banner uses for "stale".
_PERIOD_SYNC_GRACE_S = _HEALTH_STALE_HOURS * 3600


def period_start(kind: str, end: date) -> date | None:
    """First day inside the period ending on `end`; None for ITD (no start)."""
    if kind == "D":
        return end
    if kind == "WTD":
        return end.fromordinal(end.toordinal() - end.weekday())
    if kind == "MTD":
        return end.replace(day=1)
    if kind == "QTD":
        return end.replace(month=(end.month - 1) // 3 * 3 + 1, day=1)
    if kind == "YTD":
        return end.replace(month=1, day=1)
    if kind == "ITD":
        return None
    raise ValueError(f"unknown period {kind!r}; expected one of {PERIOD_KINDS}")


def _mark_tolerance_days(baseline: date | None, end: date) -> int:
    """How many days a position's own mark may lag the period's boundary.

    Zero for a day or a week — a stale mark there changes what the window is.
    For longer windows a missed pass or two is a small share of the span and
    excluding the position would lose more than it protects, so up to a tenth
    of the span, capped at three days."""
    if baseline is None:
        return 3
    return min(3, (end - baseline).days // 10)


def _dec(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _dec_or_none(v) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


@dataclass(frozen=True)
class PeriodRow:
    """One position's result over one window. Every USD figure is Decimal."""

    wallet: str
    chain: str
    protocol_id: str
    position_key: str
    pair_label: str
    start_date: str | None  # the mark the window starts from; None = from zero
    end_date: str  # the mark it ends on
    value_start: Decimal
    unclaimed_start: Decimal
    value_end: Decimal
    unclaimed_end: Decimal
    net_flows: Decimal  # deposits − withdrawals inside the window
    fees_collected: Decimal
    il: Decimal | None  # None = snapshots carry no hold value to split against
    gas: Decimal  # this position's share of priced gas in the window
    gas_txs: frozenset  # (chain, tx_hash) of every tx inside the window
    gas_unpriced_txs: frozenset  # the subset with no gas price
    closed: bool

    @property
    def uid(self) -> str:
        return f"{self.wallet}:{self.chain}:{self.protocol_id}:{self.position_key}"

    @property
    def fees_earned(self) -> Decimal:
        return self.fees_collected + self.unclaimed_end - self.unclaimed_start

    @property
    def price(self) -> Decimal:
        """Value change not explained by capital moving: market + IL."""
        return self.value_end - self.value_start - self.net_flows

    @property
    def market(self) -> Decimal | None:
        return None if self.il is None else self.price - self.il

    @property
    def pnl(self) -> Decimal:
        return self.price + self.fees_earned


@dataclass(frozen=True)
class PeriodExclusion:
    """A position left out of a window, why, and what it leaves out."""

    wallet: str
    chain: str
    protocol_id: str
    position_key: str
    pair_label: str
    value_usd: Decimal | None  # latest known value; None if never marked
    reason: str


# Slicing dimensions for PeriodPnL.by(). "pair" drops the fee tier so the same
# pair across tiers aggregates; "position" keeps every position apart.
PERIOD_DIMENSIONS = ("position", "pair", "chain", "venue", "wallet")


def _dimension_key(row: PeriodRow, dim: str) -> tuple[str, str]:
    """(group key, display label) of a row along `dim`."""
    if dim == "position":
        return row.uid, row.pair_label
    if dim == "pair":
        pair = row.pair_label.split(" ")[0]
        return pair, pair
    if dim == "chain":
        return row.chain, row.chain
    if dim == "venue":
        return f"{row.chain}:{row.protocol_id}", row.protocol_id
    if dim == "wallet":
        return row.wallet, row.wallet
    raise ValueError(f"unknown dimension {dim!r}; expected one of {PERIOD_DIMENSIONS}")


def summarize_period_rows(rows: list[PeriodRow]) -> dict:
    """Totals over a set of rows.

    `market` and `il` sum only the rows that can be split; the rest of the
    price effect is `undecomposed`, so market + il + undecomposed + fees_earned
    == pnl exactly. Gas counts each transaction once however many positions it
    touched; `net_after_gas` is None unless every transaction is priced — a
    partial cost base makes the net look better than it is."""
    zero = Decimal("0")
    txs: set = set()
    unpriced: set = set()
    market = il = undecomposed = zero
    split = 0
    for r in rows:
        txs |= r.gas_txs
        unpriced |= r.gas_unpriced_txs
        if r.il is None:
            undecomposed += r.price
        else:
            il += r.il
            market += r.price - r.il
            split += 1
    pnl = sum((r.pnl for r in rows), zero)
    gas = sum((r.gas for r in rows), zero)
    return {
        "positions": len(rows),
        "decomposed_positions": split,
        "value_start": sum((r.value_start + r.unclaimed_start for r in rows), zero),
        "value_end": sum((r.value_end + r.unclaimed_end for r in rows), zero),
        "net_flows": sum((r.net_flows for r in rows), zero),
        "fees_collected": sum((r.fees_collected for r in rows), zero),
        "unclaimed_change": sum((r.unclaimed_end - r.unclaimed_start for r in rows), zero),
        "fees_earned": sum((r.fees_earned for r in rows), zero),
        "market": market,
        "il": il,
        "undecomposed": undecomposed,
        "price": sum((r.price for r in rows), zero),
        "pnl": pnl,
        "gas": gas if len(unpriced) < len(txs) else (None if txs else zero),
        "gas_txs": len(txs),
        "gas_unpriced_txs": len(unpriced),
        "net_after_gas": pnl - gas if not unpriced else None,
    }


@dataclass
class PeriodPnL:
    """A window's result: included rows, exclusions, and how to slice them."""

    kind: str  # 'D' … 'ITD', or 'custom'
    start: date | None  # first day inside the window; None = inception
    end: date
    rows: list[PeriodRow]
    excluded: list[PeriodExclusion]

    @property
    def label(self) -> str:
        if self.start is None:
            return f"{self.kind} (inception → {self.end.isoformat()})"
        return f"{self.kind} ({self.start.isoformat()} → {self.end.isoformat()})"

    def totals(self) -> dict:
        return summarize_period_rows(self.rows)

    def excluded_value(self) -> Decimal:
        return sum((e.value_usd or Decimal("0") for e in self.excluded), Decimal("0"))

    def by(self, dim: str) -> list[dict]:
        """One totals dict per group along `dim`, largest |pnl| first. Each
        carries `key` and `label`."""
        groups: dict[str, tuple[str, list[PeriodRow]]] = {}
        for r in self.rows:
            key, label = _dimension_key(r, dim)
            groups.setdefault(key, (label, []))[1].append(r)
        out = []
        for key, (label, rows) in groups.items():
            out.append({"key": key, "label": label, **summarize_period_rows(rows)})
        out.sort(key=lambda g: -abs(g["pnl"]))
        return out


def period_pnl(
    *,
    start: date | None,
    end: date,
    baseline_rows: list[dict],
    end_rows: list[dict],
    event_summary: list[dict],
    events: list[dict],
    tx_cost_rows: list[dict],
    health_rows: list[dict],
    kind: str = "custom",
) -> PeriodPnL:
    """Per-position PnL over the window [start, end] (dates inclusive).

    Inputs are storage rows, gathered by `Storage`:
      baseline_rows  snapshots_as_of(start − 1 day); ignored when start is None
      end_rows       snapshots_as_of(end)
      event_summary  position_event_summary()
      events         events_between(…) covering every position's window
      tx_cost_rows   tx_cost_rows()
      health_rows    pipeline_health()
    """
    from datetime import UTC, datetime, time

    baseline_date = start.fromordinal(start.toordinal() - 1) if start is not None else None
    tol = _mark_tolerance_days(baseline_date, end)

    def pk(r: dict) -> tuple[str, str, str, str]:
        return (r["wallet"], r["chain"], r["protocol_id"], r["position_key"])

    def day_end_ts(d) -> int:
        return int(datetime.combine(d, time.max, tzinfo=UTC).timestamp())

    base = {pk(r): r for r in baseline_rows} if baseline_date is not None else {}
    fin = {pk(r): r for r in end_rows}
    summary = {pk(r): r for r in event_summary}
    sync_ok = {
        (h["wallet"], h["chain"], h["protocol_id"]): h.get("last_sync_ok_at") for h in health_rows
    }
    gas_by_tx = {(t["chain"], t["tx_hash"]): t.get("gas_usd") for t in tx_cost_rows}
    events_by_pos: defaultdict[tuple, list[dict]] = defaultdict(list)
    positions_by_tx: defaultdict[tuple, set] = defaultdict(set)
    for ev in events:
        events_by_pos[pk(ev)].append(ev)
        positions_by_tx[(ev["chain"], ev["tx_hash"])].add(pk(ev))

    rows: list[PeriodRow] = []
    excluded: list[PeriodExclusion] = []
    period_end_ts = day_end_ts(end)
    baseline_end_ts = day_end_ts(baseline_date) if baseline_date is not None else 0

    for key in sorted(set(base) | set(fin) | set(summary)):
        b, e, s = base.get(key), fin.get(key), summary.get(key)
        label = (e or b or s or {}).get("pair_label") or key[3]
        known_value = _dec_or_none((e or b or {}).get("current_value_usd"))

        def exclude(reason: str, key=key, label=label, known_value=known_value) -> None:
            w, c, p, k = key
            excluded.append(PeriodExclusion(w, c, p, k, label, known_value, reason))

        first_ts = s["first_ts"] if s else None
        if first_ts is None or first_ts > period_end_ts:
            if e is not None and not e["is_closed"] and first_ts is None:
                exclude("no event history — flows unknowable")
            continue  # no events yet: nothing to account for in this window
        exited = bool(s and int(s["withdraw_count"] or 0) >= 1)
        if e is None and b is None and exited and s and s["last_ts"] <= baseline_end_ts:
            continue  # exited before the window, and before marks began: nothing in it

        # ── start of the window ──
        il0: Decimal | None
        il1: Decimal | None
        if baseline_date is None:
            v0 = u0 = il0 = Decimal("0")
            t0, start_date = 0, None
        elif b is not None and b["is_closed"]:
            v0 = u0 = il0 = Decimal("0")
            t0, start_date = int(b["snapshot_ts"]), b["snapshot_date"]
        elif b is not None:
            lag = (baseline_date - date.fromisoformat(b["snapshot_date"])).days
            if lag > tol:
                exclude(f"no mark at period start (latest is {b['snapshot_date']})")
                continue
            if not b["unclaimed_is_known"]:
                exclude(f"unclaimed unreadable at period start ({b['snapshot_date']})")
                continue
            v0, u0 = _dec(b["current_value_usd"]), _dec(b["unclaimed_usd"])
            il0 = _dec_or_none(b["il_usd"])
            t0, start_date = int(b["snapshot_ts"]), b["snapshot_date"]
        elif first_ts <= baseline_end_ts:
            exclude("no mark at period start")
            continue
        else:  # opened inside the window: it genuinely starts from zero
            v0 = u0 = il0 = Decimal("0")
            t0, start_date = baseline_end_ts, None

        # ── end of the window ──
        window = [ev for ev in events_by_pos.get(key, []) if t0 < ev["ts"] <= period_end_ts]
        if e is None:
            # Events but no mark at all: an exit only if a withdrawal says so.
            if not exited:
                exclude("never marked, and no withdrawal on record")
                continue
            v1 = u1 = il1 = Decimal("0")
            closed = True
            t1 = max((ev["ts"] for ev in window), default=t0)
            end_date = end.isoformat()
        elif e["is_closed"]:
            if (b is None or b["is_closed"]) and not window and baseline_date is not None:
                continue  # closed before the window opened; nothing happened in it
            v1 = u1 = il1 = Decimal("0")
            closed = True
            t1, end_date = int(e["snapshot_ts"]), e["snapshot_date"]
        else:
            lag = (end - date.fromisoformat(e["snapshot_date"])).days
            if lag > tol:
                exclude(f"marks {lag}d old (as of {e['snapshot_date']})")
                continue
            if not e["unclaimed_is_known"]:
                exclude(f"unclaimed unreadable ({e['snapshot_date']})")
                continue
            v1, u1 = _dec(e["current_value_usd"]), _dec(e["unclaimed_usd"])
            il1 = _dec_or_none(e["il_usd"])
            closed = False
            t1, end_date = int(e["snapshot_ts"]), e["snapshot_date"]

        ok_at = sync_ok.get(key[:3])
        if ok_at is None or int(ok_at) < t1 - _PERIOD_SYNC_GRACE_S:
            when = (
                datetime.fromtimestamp(int(ok_at), tz=UTC).date().isoformat() if ok_at else "never"
            )
            exclude(f"events not synced through period end (last clean sync {when})")
            continue

        window = [ev for ev in window if ev["ts"] <= t1]
        flows = sum(
            (_dec(ev["usd_at_ts"]) for ev in window if ev["kind"] in ("deposit", "withdraw")),
            Decimal("0"),
        )
        collected = sum(
            (_dec(ev["usd_at_ts"]) for ev in window if ev["kind"] == "collect"), Decimal("0")
        )
        txs = frozenset((ev["chain"], ev["tx_hash"]) for ev in window)
        unpriced = frozenset(tx for tx in txs if gas_by_tx.get(tx) is None)
        gas = sum(
            (_dec(gas_by_tx[tx]) / len(positions_by_tx[tx]) for tx in txs - unpriced),
            Decimal("0"),
        )
        il = (il1 - il0) if il0 is not None and il1 is not None else None
        rows.append(
            PeriodRow(
                *key,
                pair_label=label,
                start_date=start_date,
                end_date=end_date,
                value_start=v0,
                unclaimed_start=u0,
                value_end=v1,
                unclaimed_end=u1,
                net_flows=flows,
                fees_collected=collected,
                il=il,
                gas=gas,
                gas_txs=txs,
                gas_unpriced_txs=unpriced,
                closed=closed,
            )
        )
    return PeriodPnL(kind=kind, start=start, end=end, rows=rows, excluded=excluded)


# ── Policy limits: breach detection (Phase 2.2) ───────────────────────────
#
# Evaluates the book against `policy.Policy`. Four outcomes, never three: a
# limit is OK, in BREACH, UNKNOWN (set, but the input it needs is missing),
# or UNSET (nobody has chosen a number). UNSET and UNKNOWN are not passes;
# a report that folded either into "ok" would say the book is inside limits
# nobody has set or could measure.


@dataclass(frozen=True)
class LimitCheck:
    limit_id: str
    subject: str  # the pair / venue / chain / position / coin checked, or 'book'
    observed: Decimal | None
    threshold: Decimal | None
    unit: str
    status: str  # 'ok' | 'breach' | 'unknown' | 'unset'
    note: str = ""

    @property
    def uid(self) -> str:
        """Stable identity of this (limit, subject) — alerts and the exception
        log key on it, so a breach that persists is one episode, not many."""
        return f"policy:{self.limit_id}:{self.subject}"


def _check(limit_id, subject, observed, threshold, unit, note="") -> LimitCheck:
    if observed is None:
        return LimitCheck(limit_id, subject, None, threshold, unit, "unknown", note)
    status = "breach" if observed > threshold else "ok"
    return LimitCheck(limit_id, subject, observed, threshold, unit, status, note)


def _position_subject(r: dict) -> str:
    """A position's policy subject: its label plus the full 4-tuple, so two
    wallets in the same range of the same pool stay two subjects (and two
    exception episodes)."""
    return f"{r['pair_label']} [{r['wallet']}:{r['chain']}:{r['protocol_id']}:{r['position_key']}]"


def policy_checks(
    snap_rows: list[dict],
    policy,
    depeg_prices: dict[str, Decimal] | None = None,
    counterparty_exposure: dict[str, Decimal | None] | None = None,
) -> list[LimitCheck]:
    """Every configured limit, evaluated against latest-snapshot rows.

    '%' limits divide by the tracked book (sum of live position values) —
    until custody is visible that IS the balance sheet this tool can see, and
    each check's note says so. `depeg_prices` maps stablecoin symbol → market
    USD (see `policy.fetch_depeg_prices`); `counterparty_exposure` maps
    counterparty → USD (from the contractual ledger). A missing input makes
    the dependent check UNKNOWN, not OK.
    """
    from defi_tracker.policy import LIMITS

    checks: list[LimitCheck] = []
    for d in policy.unset:
        checks.append(LimitCheck(d.id, "—", None, None, d.unit, "unset"))

    total = sum((Decimal(str(r["current_value_usd"])) for r in snap_rows), Decimal("0"))
    book_note = "share of tracked DEX book"

    def groups(key) -> dict[str, Decimal]:
        out: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for r in snap_rows:
            out[key(r)] += Decimal(str(r["current_value_usd"]))
        return dict(out)

    dims = {
        "position": _position_subject,
        "pair": lambda r: r["pair_label"].split(" ")[0],
        "venue": lambda r: f"{r['chain']}:{r['protocol_id']}",
        "chain": lambda r: r["chain"],
    }
    for dim, key in dims.items():
        grouped = groups(key)
        for unit, limit_id in (("%", f"concentration.max_{dim}_pct"),
                               ("USD", f"concentration.max_{dim}_usd")):  # fmt: skip
            threshold = policy.get(limit_id)
            if threshold is None:
                continue
            for subject, value in sorted(grouped.items(), key=lambda kv: -kv[1]):
                if unit == "%":
                    observed = value / total * 100 if total > 0 else None
                    checks.append(_check(limit_id, subject, observed, threshold, unit, book_note))
                else:
                    checks.append(_check(limit_id, subject, value, threshold, unit))

    threshold = policy.get("risk.max_il_pct")
    if threshold is not None:
        for r in snap_rows:
            subject = _position_subject(r)
            il_pct = r.get("il_pct")
            # il_pct is signed (negative = loss vs HODL); the limit caps the loss.
            observed = -Decimal(str(il_pct)) if il_pct is not None else None
            note = "" if il_pct is not None else "no event history, IL unknowable"
            checks.append(_check("risk.max_il_pct", subject, observed, threshold, "%", note))

    threshold = policy.get("risk.max_out_of_range_pct")
    if threshold is not None:
        if any(r.get("in_range") is None for r in snap_rows):
            note = "some positions' range state is unknown; counted as in range"
        else:
            note = book_note
        oor = sum(
            (Decimal(str(r["current_value_usd"])) for r in snap_rows if r.get("in_range") == 0),
            Decimal("0"),
        )
        observed = oor / total * 100 if total > 0 else None
        checks.append(_check("risk.max_out_of_range_pct", "book", observed, threshold, "%", note))

    threshold = policy.get("depeg.max_deviation_bps")
    if threshold is not None:
        prices = depeg_prices or {}
        for symbol in sorted(policy.depeg_coins):
            price = prices.get(symbol)
            observed = abs(price - 1) * 10_000 if price is not None else None
            note = f"market ${price:.4f}" if price is not None else "no market price read"
            checks.append(
                _check("depeg.max_deviation_bps", symbol, observed, threshold, "bps", note)
            )

    for unit, limit_id in (("%", "counterparty.max_exposure_pct"),
                           ("USD", "counterparty.max_exposure_usd")):  # fmt: skip
        threshold = policy.get(limit_id)
        if threshold is None:
            continue
        if not counterparty_exposure:  # an empty ledger is unkeyed, not zero exposure
            checks.append(
                _check(limit_id, "—", None, threshold, unit, "no counterparty exposures on record")
            )
            continue
        for name, usd in sorted(counterparty_exposure.items(), key=lambda kv: -(kv[1] or 0)):
            if usd is None:
                note = "a contract's USD value was never keyed"
                checks.append(_check(limit_id, name, None, threshold, unit, note))
            elif unit == "%":
                observed = usd / total * 100 if total > 0 else None
                checks.append(_check(limit_id, name, observed, threshold, unit, book_note))
            else:
                checks.append(_check(limit_id, name, usd, threshold, unit))
    for name, cap in sorted(policy.counterparty_caps.items()):
        exposure = (counterparty_exposure or {}).get(name)
        note = "per-counterparty cap" if exposure is not None else "exposure unknown"
        checks.append(_check("counterparty.caps", name, exposure, cap, "USD", note))

    order = {d.id: i for i, d in enumerate(LIMITS)}
    checks.sort(key=lambda c: (order.get(c.limit_id, len(order)), c.subject))
    return checks


def counterparty_exposure(contracts: list[dict], on: date) -> dict[str, Decimal | None]:
    """USD exposure per counterparty from the contractual ledger, on a date.

    Counts contracts that are `active` and in force on `on` (started, not yet
    past their end date). A counterparty with any such contract whose USD value
    was never keyed has exposure None — unknown, not the sum of the rest."""
    out: dict[str, Decimal | None] = {}
    for c in contracts:
        if c["status"] != "active":
            continue
        if c.get("start_date") and date.fromisoformat(c["start_date"]) > on:
            continue
        if c.get("end_date") and date.fromisoformat(c["end_date"]) < on:
            continue
        name = c["counterparty"]
        usd = c.get("usd_value")
        if usd is None:
            out[name] = None
        elif name not in out or out[name] is not None:
            out[name] = (out.get(name) or Decimal("0")) + Decimal(str(usd))
    return out


# ── PEAQ now, and the contractual ledger marked at it ─────────────────────
#
# The ledger's usd_value is what was keyed on its as-of date (the 09-30 mark),
# a static figure. Notional on loan moves with PEAQ several percent a day, so
# the MM book re-marks every PEAQ-denominated contract at the latest price the
# desk has — and says which price, from where, and when.

# Priced one-for-one with PEAQ. wstPEAQ is not: it is PEAQ exposure (below)
# but trades at its own rate, so it never sets the PEAQ mark.
PEAQ_SYMBOLS = frozenset({"PEAQ", "WPEAQ"})
_STABLE_SYMBOLS = frozenset({"USDC", "USDT"})


@dataclass(frozen=True)
class PeaqMark:
    """PEAQ in USD as the desk last saw it. `price` is the freshest of the
    two sources; the other is kept as a cross-check, never silently dropped."""

    price: Decimal | None
    ts: int | None  # unix seconds of `price`
    source: str  # "market" (CoinGecko at page build) | "book pools" | "none"
    pool_price: Decimal | None  # implied by the book's own PEAQ/stable pools
    pool_ts: int | None  # the snapshot those pools were read at
    market_price: Decimal | None  # CoinGecko at page build
    market_ts: int | None

    @property
    def gap_pct(self) -> float | None:
        """Market vs book pools, % — how far the two sources disagree."""
        if self.market_price is None or not self.pool_price:
            return None
        return float((self.market_price - self.pool_price) / self.pool_price * 100)


def peaq_mark(
    snaps: list[dict], market_prices: dict | None = None, market_ts: int | None = None
) -> PeaqMark:
    """PEAQ's USD price from the book's own pools and, when given, the market.

    Pools: each open position holding PEAQ and a stablecoin and nothing else
    implies (value − stable) / PEAQ; the pools are weighted by their PEAQ
    held. `market_prices` is {token_key: USD} read at page build; any PEAQ key
    in it is the market price. With both, the market wins (it is the newer
    read); with neither, the price is None — unknown, never a stale default."""
    num = den = Decimal(0)
    pool_ts: int | None = None
    peaq_keys: set[str] = set()
    for r in snaps:
        if r.get("is_closed"):
            continue
        try:
            bals = json.loads(r.get("current_balances_json") or "[]")
        except (TypeError, ValueError):
            continue
        peaq = [b for b in bals if str(b.get("symbol") or "").upper() in PEAQ_SYMBOLS]
        peaq_keys.update(b["token_key"] for b in peaq if b.get("token_key"))
        stable = [b for b in bals if str(b.get("symbol") or "").upper() in _STABLE_SYMBOLS]
        if len(peaq) != 1 or len(stable) != 1 or len(bals) != 2:
            continue
        qty = Decimal(str(peaq[0]["amount"]))
        if qty <= 0 or r.get("current_value_usd") is None:
            continue
        implied = (Decimal(str(r["current_value_usd"])) - Decimal(str(stable[0]["amount"]))) / qty
        if implied <= 0:
            continue
        num += implied * qty
        den += qty
        ts = r.get("snapshot_ts")
        pool_ts = max(pool_ts or 0, int(ts)) if ts is not None else pool_ts
    pool_price = num / den if den else None
    market = sorted(
        Decimal(str(v)) for k, v in (market_prices or {}).items() if k in peaq_keys and v
    )
    mid = len(market) // 2
    market_price = (
        (market[mid] if len(market) % 2 else (market[mid - 1] + market[mid]) / 2)
        if market
        else None
    )
    if market_price is not None:
        price, ts_, source = market_price, market_ts, "market"
    elif pool_price is not None:
        price, ts_, source = pool_price, pool_ts, "book pools"
    else:
        price, ts_, source = None, None, "none"
    return PeaqMark(
        price=price,
        ts=ts_,
        source=source,
        pool_price=pool_price,
        pool_ts=pool_ts,
        market_price=market_price,
        market_ts=market_ts if market_price is not None else None,
    )


def tranche_marks(tranches: list[dict], price: Decimal | None) -> list[dict]:
    """Each option tranche against PEAQ now.

    - `strike_x_spot`: the strike as a multiple of spot (2.0 = strike twice spot)
    - `in_money`: spot at or above the strike — exercising pays the MM today
    - `mm_gain_usd`: (spot − strike) × amount when in the money, else 0 — what
      an exercise today hands the MM over the market, i.e. what peaq gives up
    - `strike_usd_total`: strike × amount — what peaq is paid on exercise

    An unknown price leaves all four None. An unfixed strike with bounds
    (`strike_low_usd`/`strike_high_usd`) gets `strike_x_range` — the low and
    high multiples of spot — and `in_money` only when every strike in the
    bounds is (spot ≥ the high); otherwise all four stay None."""
    out = []
    for t in tranches:
        k: Decimal | None = t.get("strike_usd")
        lo, hi = t.get("strike_low_usd"), t.get("strike_high_usd")
        amount: Decimal = t["amount"]
        row = {**t, "strike_x_spot": None, "strike_x_range": None, "in_money": None,
               "mm_gain_usd": None, "mm_gain_at_exercise_usd": None}  # fmt: skip
        row["strike_usd_total"] = k * amount if k is not None else None
        if price is None or price <= 0:
            out.append(row)
            continue
        if k is not None:
            row["strike_x_spot"] = float(k / price)
            if t["status"] == "open":
                row["in_money"] = price >= k
                row["mm_gain_usd"] = max(price - k, Decimal(0)) * amount
            elif t["status"] == "exercised":
                # Sold to the MM at the strike: its gain marked at spot is peaq's loss.
                row["mm_gain_usd"] = (price - k) * amount
        x = t.get("exercise_price_usd")
        if t["status"] == "exercised" and k is not None and x is not None:
            # The MM's gain on the day it exercised, at that day's market price.
            row["mm_gain_at_exercise_usd"] = (x - k) * amount
        elif lo is not None and hi is not None:
            row["strike_x_range"] = (float(lo / price), float(hi / price))
            if t["status"] == "open" and price >= hi:
                row["in_money"] = True
            elif t["status"] == "open" and price < lo:
                # Below every strike the bounds allow: worth nothing to exercise today.
                row["in_money"] = False
                row["mm_gain_usd"] = Decimal(0)
        out.append(row)
    return out


def contract_marks(
    contracts: list[dict], mark: PeaqMark, on: date, tranches: list[dict] | None = None
) -> list[dict]:
    """The contracts in force on `on`, each with its value at PEAQ now.

    A PEAQ-denominated contract is worth what is still owed back × mark.price;
    any other asset, or no price, leaves `usd_now` None (unknown) — the keyed
    usd_value is carried beside it with its own date, never relabelled as
    today's. When the contract's tranches are keyed, PEAQ sold to the MM by an
    exercised tranche is no longer owed back and comes off `outstanding`, as
    does any PEAQ the MM already `returned`. `mm_pnl_usd` marks only the
    tranches still open: an exercised one settled on its own day, and a
    lapsed one can no longer be exercised."""
    by_ref: dict[str, list[dict]] = defaultdict(list)
    for t in tranche_marks(tranches or [], mark.price):
        by_ref[t["ref"]].append(t)
    out = []
    for c in contracts:
        if c["status"] != "active":
            continue
        if c.get("start_date") and date.fromisoformat(c["start_date"]) > on:
            continue
        if c.get("end_date") and date.fromisoformat(c["end_date"]) < on:
            continue
        amount = c.get("amount")
        mine = by_ref.get(c["ref"], [])
        exercised = sum(
            (t["amount"] for t in mine if t["status"] == "exercised" and t["asset"] == c["asset"]),
            Decimal(0),
        )
        returned = c.get("returned") or Decimal(0)
        outstanding = Decimal(str(amount)) - exercised - returned if amount is not None else None
        usd_now = (
            outstanding * mark.price
            if outstanding is not None and mark.price is not None and c.get("asset") in PEAQ_SYMBOLS
            else None
        )
        end = c.get("end_date")
        start = c.get("start_date")
        months = (
            round((date.fromisoformat(end) - date.fromisoformat(start)).days / 30.44)
            if start and end
            else None
        )
        rate = c.get("interest_rate_pct")
        interest = (
            Decimal(str(amount)) * Decimal(str(rate)) / 100 * months / 12
            if amount is not None and rate is not None and months is not None
            else None
        )
        out.append(
            {
                **c,
                "tenure_months": months,
                "interest_payable": interest,
                "mm_pnl_usd": sum(
                    (
                        t["mm_gain_usd"]
                        for t in mine
                        if t["status"] == "open" and t["mm_gain_usd"] is not None
                    ),
                    Decimal(0),
                )
                if mine
                else None,  # fmt: skip
                "mm_pnl_at_exercise_usd": sum(
                    (
                        t["mm_gain_at_exercise_usd"]
                        for t in mine
                        if t["mm_gain_at_exercise_usd"] is not None
                    ),
                    Decimal(0),
                )
                if mine
                else None,
                "tranches": mine,
                "exercised": exercised,
                "returned": returned,
                "outstanding": outstanding,
                "usd_now": usd_now,
                "days_left": (date.fromisoformat(end) - on).days if end else None,
            }
        )
    return out


def mm_exposure(book: list[dict], today: date) -> list[dict]:
    """One line per MM for the brief: PEAQ still owed back, its value now,
    when the loan ends, and its nearest live option — the next expiry and
    the strike closest to spot. Options expire on their own dates, apart
    from the loan's end; a contract with no tranches keyed says so."""
    out = []
    for c in book:
        live = [
            t
            for t in c.get("tranches", [])
            if t["status"] == "open"
            and (t["exercisable_to"] or t["exercisable_from"] or "9999") >= today.isoformat()
        ]
        expiries = [t["exercisable_to"] or t["exercisable_from"] for t in live]
        multiples = [t["strike_x_spot"] for t in live if t["strike_x_spot"] is not None]
        ranges = [t["strike_x_range"] for t in live if t.get("strike_x_range")]
        out.append(
            {
                "counterparty": c["counterparty"],
                "outstanding": c.get("outstanding"),
                "usd_now": c.get("usd_now"),
                "loan_end": c.get("end_date"),
                "days_left": c.get("days_left"),
                "options_keyed": bool(c.get("tranches")),
                "next_option_expiry": min((e for e in expiries if e), default=None),
                # The lowest strike is the first to come into the money.
                "lowest_strike_x": min(multiples, default=None),
                "lowest_strike_range": min(ranges, default=None),
                "unfixed_strikes": sum(1 for t in live if t["strike_usd"] is None),
                "in_money": any(t["in_money"] for t in live),
            }
        )
    return sorted(out, key=lambda r: r["loan_end"] or "9999")


def mm_calendar(book: list[dict], today: date, horizon_days: int = 60) -> list[dict]:
    """Dated events in the MM book within `horizon_days`: loans ending and
    option windows opening or closing. Tranches sharing a window are one line."""
    end = date.fromordinal(today.toordinal() + horizon_days).isoformat()
    now = today.isoformat()
    rows: list[dict] = []
    for c in book:
        nb = c.get("notice_by")
        if nb and nb <= end and (c.get("end_date") or "9999") >= now:
            days = (date.fromisoformat(nb) - today).days
            rows.append(
                {
                    "date": nb,
                    "what": f"{c['counterparty']} notice to stop renewal",
                    "detail": "deadline may have passed: send written notice now"
                    if days < 0
                    else "without written notice the agreement renews",
                    "days": days,
                }
            )
        if c.get("end_date") and now <= c["end_date"] <= end:
            rows.append(
                {
                    "date": c["end_date"],
                    "what": f"{c['counterparty']} loan ends",
                    "detail": f"{float(c['outstanding']) / 1e6:,.2f}m PEAQ due back"
                    if c.get("outstanding") is not None
                    else "",
                    "days": c["days_left"],
                }
            )
        windows: dict[tuple, list[dict]] = defaultdict(list)
        for t in c.get("tranches", []):
            if t["status"] == "open":
                windows[(t["exercisable_from"], t["exercisable_to"])].append(t)
        for (frm, to), ts in windows.items():
            is_open = frm is not None and frm <= now and (to is None or now <= to)
            when = to if is_open else frm
            if when is None or not (now <= when <= end):
                continue
            nos = "–".join(dict.fromkeys([ts[0]["tranche"], ts[-1]["tranche"]]))
            xs = [t["strike_x_spot"] for t in ts if t["strike_x_spot"] is not None]
            rng = [t["strike_x_range"] for t in ts if t.get("strike_x_range")]
            if any(t["in_money"] for t in ts):
                state = "in the money"
            elif rng and not xs:
                state = f"strike about {rng[0][0]:.1f}–{rng[0][1]:.1f}× spot (not fixed)"
            elif xs:
                span = f"{min(xs):.1f}" if len(set(xs)) == 1 else f"{min(xs):.1f}–{max(xs):.1f}"
                state = f"strike {span}× spot, out of the money"
            else:
                state = "strike not fixed"
            amount = sum((t["amount"] for t in ts), Decimal(0))
            rows.append(
                {
                    "date": when,
                    "what": f"{c['counterparty']} option {'#' + nos} "
                    + ("window closes" if is_open else "window opens"),
                    "detail": f"{float(amount) / 1e6:,.0f}m PEAQ · {state}",
                    "days": (date.fromisoformat(when) - today).days,
                }
            )
    return sorted(rows, key=lambda r: r["date"])


def _venue_key(name: str) -> str:
    n = name.lower().replace(" exchange", "").replace(".io", "").strip()
    return n.split(" (")[0]


def venue_matrix(venues: list[dict], book: list[dict]) -> dict:
    """Where PEAQ trades against where each MM is contracted to quote.

    `venues` are CoinGecko rows (fetch_venues); each contract's venues come
    from its key terms ("Venues": comma-separated). Returns the traded rows
    with each MM's flag, the contracted venues with no PEAQ market, and the
    share of volume no MM is contracted on. DEX rows are flagged, not matched."""
    import json

    mm_venues: dict[str, set[str]] = {}
    for c in book:
        try:
            terms = json.loads(c.get("key_terms_json") or "{}")
        except ValueError:
            terms = {}
        names = [v.strip() for v in str(terms.get("Venues", "")).split(",") if v.strip()]
        mm_venues[c["counterparty"]] = {_venue_key(v) for v in names}
    total = sum(v["volume_usd"] for v in venues) or 0.0
    rows, listed = [], set()
    uncovered = 0.0
    for v in venues:
        key = _venue_key(v["venue"])
        listed.add(key)
        dex = any(
            w in v["venue"].lower()
            for w in ("swap", "uniswap", "orca", "raydium", "matcha", "zerofi", "clmm")
        )
        flags = {mm: key in vs for mm, vs in mm_venues.items()}
        if not dex and not any(flags.values()):
            uncovered += v["volume_usd"]
        rows.append({**v, "share_pct": v["volume_usd"] / total * 100 if total else None,
                     "dex": dex, "mm": flags})  # fmt: skip
    unlisted = {mm: sorted(k for k in vs if k not in listed) for mm, vs in mm_venues.items()}
    return {
        "rows": rows,
        "mms": list(mm_venues),
        "unlisted": unlisted,
        "uncovered_pct": uncovered / total * 100 if total else None,
        "total_volume_usd": total,
    }


def rfq_scores(criteria: list[dict], bidders: list[str]) -> dict[str, float | None]:
    """Weighted RFQ score per bidder, 1–5 scale: Σ weight × score / Σ weight.
    A bidder missing any score has no total (None) — a partial sum would rank
    an unscored bid as a weak one."""
    out: dict[str, float | None] = {}
    total_w = sum(float(c.get("weight", 0)) for c in criteria)
    for b in bidders:
        got = [(float(c.get("weight", 0)), (c.get("scores") or {}).get(b)) for c in criteria]
        scored = [(w, float(s)) for w, s in got if s is not None]
        if not criteria or not total_w or len(scored) < len(got):
            out[b] = None
        else:
            out[b] = sum(w * s for w, s in scored) / total_w
    return out


# ── MM delivery: what each incumbent delivered against its contract ───────
#
# Coinwatch's daily figures (coinwatch.fetch_delivery) per MM × venue, read
# against each contract's KPIs as keyed in rfq.toml's [[target]] tables. A
# figure is a median of daily values over the window, never a mean: one
# outage day should not move the headline. Taken as Coinwatch reports it.

_DEPTH_METRICS = (("depth_200", "depth_2pct_usd"), ("depth_100", "depth_1pct_usd"),
                  ("depth_50", "depth_05pct_usd"))  # fmt: skip


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _grade(value: float | None, target: float | None, lower_is_better: bool = False) -> str:
    """ok / warn / bad against a contract target; '' where there is none.
    Depth: at target ok, ≥ half warn. Spread: at target ok, ≤ 2× warn."""
    if value is None or target is None:
        return ""
    if lower_is_better:
        return "ok" if value <= target else ("warn" if value <= 2 * target else "bad")
    return "ok" if value >= target else ("warn" if value >= 0.5 * target else "bad")


def _grade_share(pct: float | None) -> str:
    if pct is None:
        return ""
    return "ok" if pct >= 90 else ("warn" if pct >= 50 else "bad")


def _day_depth_target(t: dict, key: str, own: float, rem: float | None) -> float | None:
    """A depth KPI on one day: the fixed figure, or — where the contract says
    "or a share of the book, whichever is lower" — that share of the whole book."""
    base = t.get(key)
    if base is None:
        return None
    cap = t.get("book_share_cap")
    if cap is not None and key == "depth_2pct_usd" and rem is not None:
        return min(float(base), float(cap) * (own + rem))
    return float(base)


def mm_delivery(rows: list[dict], targets: list[dict], window_days: int = 90) -> dict:
    """Delivered vs contracted, MM × venue, over the last `window_days`
    complete days and over the full history.

    `rows` are coinwatch.fetch_delivery rows; `targets` the rfq.toml
    [[target]] tables ({mm, venues, spread_pct, depth_2pct_usd, …}).
    Per MM and venue: the median of own depth (±2/±1/±0.5%) and spread, the
    median of the rest of the book's ±2% depth, the share of days the ±2%
    KPI was met, and the share of days every keyed KPI was met. A venue the
    contract does not name carries figures but no grade."""
    if not rows:
        return {}
    end = max(r["day"] for r in rows)
    start = date.fromordinal(date.fromisoformat(end).toordinal() - window_days + 1).isoformat()
    # "Now": the last 7 complete days — a venue the MM left a fortnight ago
    # must not still count at its 90-day median.
    start7 = date.fromordinal(date.fromisoformat(end).toordinal() - 6).isoformat()
    first = min(r["day"] for r in rows)
    by: dict[tuple[str, str], dict[str, dict[str, tuple[float, float | None]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for r in rows:
        by[(r["mm"].lower(), r["venue"])][r["metric"]][r["day"]] = (r["own"], r["remaining"])
    tmap = {t["mm"].lower(): t for t in targets if t.get("mm")}
    mms = sorted(
        {mm for mm, _ in by},
        key=lambda m: (m not in tmap, list(tmap).index(m) if m in tmap else 0, m),
    )
    venues = sorted({v for _, v in by} | {v for t in tmap.values() for v in t.get("venues", [])})

    def stats(
        series: dict[str, tuple[float, float | None]], pick
    ) -> tuple[float | None, float | None]:
        recent = [pick(v) for d, v in series.items() if d >= start and pick(v) is not None]
        full = [pick(v) for v in series.values() if pick(v) is not None]
        return _median(recent), _median(full)

    def last7(series: dict[str, tuple[float, float | None]], pick) -> float | None:
        return _median([pick(v) for d, v in series.items() if d >= start7 and pick(v) is not None])

    blocks = []
    market: dict[str, dict] = {}
    for mm in mms:
        t = tmap.get(mm, {})
        contracted = set(t.get("venues", []))
        # A contract measured wider than ±2% (e.g. ±4%) is read through
        # Coinwatch's ±2% figure, which is a floor: a pass proves a pass, a
        # miss proves nothing — so a miss is left ungraded, never red.
        floor = float(t.get("depth_measured_at_pct", 2)) > 2

        def soften(status: str, floor: bool = floor) -> str:
            return status if not floor or status == "ok" else ""

        cells: dict[str, dict[str, dict]] = defaultdict(dict)
        for v in venues:
            m = by.get((mm, v))
            if not m:
                continue
            graded = v in contracted
            for metric, key in _DEPTH_METRICS:
                if metric in m:
                    r90, rf = stats(m[metric], lambda x: x[0])
                    tgt = t.get(key) if graded else None
                    st = _grade(r90, tgt)
                    cells[metric][v] = {"v90": r90, "full": rf, "target": tgt,
                                        "v7": last7(m[metric], lambda x: x[0]),
                                        "status": soften(st) if metric == "depth_200" else st}  # fmt: skip
            if "spread" in m:
                r90, rf = stats(m["spread"], lambda x: x[0])
                tgt = t.get("spread_pct") if graded else None
                cells["spread"][v] = {"v90": r90, "full": rf, "target": tgt,
                                      "v7": last7(m["spread"], lambda x: x[0]),
                                      "status": _grade(r90, tgt, lower_is_better=True)}  # fmt: skip
            d2 = m.get("depth_200", {})
            if d2:
                r90, rf = stats(d2, lambda x: x[1])
                cells["rest"][v] = {"v90": r90, "full": rf, "target": None, "status": ""}
                tot90, totf = stats(d2, lambda x: x[0] + x[1] if x[1] is not None else None)
                market.setdefault(v, {"v90": tot90, "full": totf,
                                      "v7": last7(d2, lambda x: x[0] + x[1] if x[1] is not None else None)})  # fmt: skip
            if graded and d2 and t.get("depth_2pct_usd") is not None:
                for kind, test in (("on_target", False), ("all_kpis", True)):
                    hit = {"v90": [0, 0], "full": [0, 0]}
                    for d, (own, rem) in d2.items():
                        ok = own >= (_day_depth_target(t, "depth_2pct_usd", own, rem) or 0)
                        if test:
                            known = True
                            for metric, key in _DEPTH_METRICS[1:]:
                                if t.get(key) is not None:
                                    x = m.get(metric, {}).get(d)
                                    known &= x is not None
                                    ok &= x is not None and x[0] >= float(t[key])
                            if t.get("spread_pct") is not None:
                                x = m.get("spread", {}).get(d)
                                known &= x is not None
                                ok &= x is not None and x[0] <= float(t["spread_pct"])
                            if not known:
                                continue
                        for w in ("v90", "full") if d >= start else ("full",):
                            hit[w][0] += ok
                            hit[w][1] += 1
                    p90 = hit["v90"][0] / hit["v90"][1] * 100 if hit["v90"][1] else None
                    pf = hit["full"][0] / hit["full"][1] * 100 if hit["full"][1] else None
                    cells[kind][v] = {"v90": p90, "full": pf, "target": None,
                                      "status": soften(_grade_share(p90))}  # fmt: skip
        blocks.append({"mm": t.get("mm", mm.title()), "target": t, "cells": dict(cells),
                       "contracted": sorted(contracted), "floor": floor})  # fmt: skip
    return {"window": (start, end), "full": (first, end), "last7": (start7, end), "venues": venues,
            "blocks": blocks, "market": market}  # fmt: skip


def dex_depth(dex: list[dict], plans: list[PoolPlan], venues: list[dict]) -> list[dict]:
    """±2% depth on each DEX venue the RFQ names, and who supplies it.

    `dex` is rfq.toml's [[dex]] list: {label, coingecko?, pool?} where `pool`
    is "chain:protocol_id:pair_label" of a pool the book is in (its depth and
    peaq's share come from the optimiser's pool state, active liquidity held
    flat over ±2%) and `coingecko` the venue's CoinGecko name (both sides of
    its ±2% depth summed). A venue with no pool of the book's has no peaq
    share: the liquidity there is someone else's."""
    states = {":".join(p.state.key): p.state for p in plans}
    cg = {v["venue"]: v for v in venues}
    out = []
    for d in dex:
        st = states.get(d.get("pool", ""))
        v = cg.get(d.get("coingecko", ""))
        book = st.l_active * st.sqrt_p * st.p1 * _DEPTH_2PCT if st and st.l_active else None
        share = (min(1.0, st.l_ours_active / st.l_active) if st and st.l_active else
                 (0.0 if d.get("pool") is None else None))  # fmt: skip
        out.append({
            "label": d.get("label", d.get("coingecko", d.get("pool", "?"))),
            "coingecko_usd": v["depth_up_usd"] + v["depth_down_usd"] if v else None,
            "pool_usd": book,
            "peaq_share": share,
            "volume_usd": v["volume_usd"] if v else None,
            "owner": d.get("owner"),
        })  # fmt: skip
    return out


# ── Bids: what each quote commits, and what it costs ─────────────────────
#
# Each bid is an rfq.toml [[bid]] table. Depth commitments are per venue
# ({venues = ["gate"], d2 = 25000, d1 = …, d05 = …}) or, where a quote
# aggregates across venues, one entry naming several (group = "A"). Venue
# keys ending " perp" are perpetuals; they are shown but never summed with
# spot depth, and a spot venue where PEAQ is not listed is shown but not
# summed either — depth there cannot exist.

_BID_BANDS = (("d2", "depth_200"), ("d1", "depth_100"), ("d05", "depth_50"))


def bid_commitments(bid: dict, listed: set[str] | None) -> dict:
    """{cells: {metric: {venue: {usd, group}}}, groups, listed_d2_usd,
    perp_d2_usd, unlisted}. `listed` = venue keys where PEAQ trades spot
    (CoinGecko); None = not read, every spot venue counts."""
    cells: dict[str, dict[str, dict]] = defaultdict(dict)
    groups: list[dict] = []
    unlisted: set[str] = set()
    listed_d2 = perp_d2 = 0.0
    for e in bid.get("depth", []):
        vs = list(e.get("venues", []))
        group = e.get("group") if len(vs) > 1 else None
        if group:
            groups.append({"group": group, "venues": vs, **{k: e.get(k) for k, _ in _BID_BANDS}})
        for key, metric in _BID_BANDS:
            if e.get(key) is None:
                continue
            for v in vs:
                cells[metric][v] = {"usd": float(e[key]), "group": group}
        perp = all(v.endswith(" perp") for v in vs)
        live = [v for v in vs if not v.endswith(" perp") and (listed is None or v in listed)]
        unlisted.update(v for v in vs if not v.endswith(" perp") and v not in live)
        if e.get("d2") is not None:
            if perp:
                perp_d2 += float(e["d2"])
            elif live:
                listed_d2 += float(e["d2"])
    if bid.get("spread_pct") is not None:
        for v in {v for e in bid.get("depth", []) for v in e.get("venues", [])}:
            cells["spread"][v] = {"usd": float(bid["spread_pct"]), "group": None}
    return {"cells": dict(cells), "groups": groups, "listed_d2_usd": listed_d2 or None,
            "perp_d2_usd": perp_d2 or None, "unlisted": sorted(unlisted)}  # fmt: skip


def bs_call(spot: float, strike: float, sigma_annual: float, years: float) -> float:
    """Black–Scholes call value per token, zero rates (a token loan pays no
    interest either way). Expired or zero-vol: intrinsic."""
    if years <= 0 or sigma_annual <= 0 or spot <= 0:
        return max(spot - strike, 0.0)
    if strike <= 0:
        return spot
    sd = sigma_annual * math.sqrt(years)
    d1 = (math.log(spot / strike) + sd * sd / 2) / sd
    return spot * _norm_cdf(d1) - strike * _norm_cdf(d1 - sd)


def bid_cost(
    bid: dict,
    spot: float | None,
    sigma_daily: float | None,
    n_obs: int,
    carry_rate_pct: float | None,
    depth_usd: float | None,
) -> dict:
    """All-in cost per year of one bid, in USD at PEAQ now:

    - fee: cash per year as quoted (None = not stated, never $0)
    - options: fair value of the calls granted on the loan — Black–Scholes at
      PEAQ's own σ (annualised √365, crypto trades daily), strike as % of the
      reference (TWAP/ATM, taken as spot now), expiry at the tenor's end;
      σ's 95% band gives the range
    - carry: PEAQ (or stables) lent × `carry_rate_pct` — what the loan forgoes
    Annualised over the tenor. `per_1k` = all-in ÷ (±2% depth / $1,000), the
    depth being committed (a bid) or delivered (an incumbent)."""
    years = float(bid.get("tenor_months") or 12) / 12
    loan = bid.get("loan_peaq")
    if loan is None and bid.get("loan_usd") is not None and spot:
        loan = float(bid["loan_usd"]) / spot
    loan = float(loan or 0)
    notional = loan * spot if spot is not None else None
    stables = float(bid.get("stables_lent_usd") or 0)
    fee = float(bid["fee_usd_year"]) if bid.get("fee_usd_year") is not None else None
    sig = sigma_daily * math.sqrt(365) if sigma_daily else None
    opts: float | None = 0.0
    band: tuple[float, float] | None = (0.0, 0.0)
    if bid.get("options") and (loan or any(o.get("peaq") or o.get("usd") for o in bid["options"])):
        if spot is None or sig is None:
            opts = band = None
        else:
            lo_s, hi_s = sigma_band(sig, n_obs)

            def one(o: dict, s: float) -> float:
                """A tranche: `peaq` tokens, `usd` of notional, or `share` of
                the loan; strike as `strike_usd` or `strike_pct` of the
                reference (PEAQ now); expiry `months` from now or the tenor.
                `type = "put"`: the MM may hand PEAQ back at the strike in
                place of the stables lent — `usd` of stables buys usd/strike
                PEAQ — so peaq is short a put (value = call − spot + strike)."""
                k = (
                    float(o["strike_usd"])
                    if o.get("strike_usd") is not None
                    else spot * float(o["strike_pct"]) / 100
                )
                is_put = o.get("type") == "put"
                if o.get("peaq") is not None:
                    qty = float(o["peaq"])
                elif o.get("usd") is not None:
                    qty = float(o["usd"]) / (k if is_put else spot)
                else:
                    qty = float(o.get("share", 1)) * loan
                t = float(o["months"]) / 12 if o.get("months") is not None else years
                c = bs_call(spot, k, s, t)
                return qty * (c - spot + k if is_put else c)

            def value(s: float) -> float:
                # `replaces`: options the bid swaps out — their value is netted
                # off, so a re-strike costs what it adds, not what it holds.
                return sum(one(o, s) for o in bid["options"]) - sum(
                    one(o, s) for o in bid.get("replaces", [])
                )

            opts, band = value(sig) / years, (value(lo_s) / years, value(hi_s) / years)
    carry = None
    if carry_rate_pct is not None and notional is not None:
        carry = (notional + stables) * carry_rate_pct / 100
    known = [x for x in (fee, opts, carry) if x is not None]
    total = sum(known) if opts is not None and carry is not None else None
    total_band = (
        (band[0] + (fee or 0) + carry, band[1] + (fee or 0) + carry)
        if band is not None and carry is not None
        else None
    )
    return {
        "loan_peaq": loan or None,
        "notional_usd": notional,
        "years": years,
        "sigma_annual": sig,
        "fee_usd": fee,
        "options_usd": opts,
        "options_band": band,
        "carry_usd": carry,
        "total_usd": total,  # excludes an unstated fee: `fee_usd` None says so
        "total_band": total_band,
        "depth_usd": depth_usd,
        "per_1k": total / (depth_usd / 1000) if total is not None and depth_usd else None,
    }


def breaches(checks: list[LimitCheck]) -> list[LimitCheck]:
    return [c for c in checks if c.status == "breach"]


# ── The fee table: one construction for every surface ─────────────────────
#
# The CLI, Slack and the desk page each used to build the month-by-month fee
# table themselves, and they had drifted: the page never learned about
# unswept or rolled-up months, so it silently dropped a rolled-up month. This is the one
# construction; surfaces only format it.
#
# Two things it does that none of them did:
#
# 1. SPLITS a sweep that carried more than one month. When a monthly sweep is
#    skipped the next one collects both months, and attribution_month can only
#    roll a claim back one month. The daily snapshots record each position's
#    unclaimed fees as token QUANTITIES, so the split is measured, not guessed:
#    a skipped month's share is the growth in unclaimed quantity from just
#    after the previous sweep to that month's last snapshot. Each share is
#    valued at the sweep's own claim price, so the months still add up to the
#    recorded claim; the month-end valuation travels as a note. A position
#    whose chain of snapshots is broken (a missing or unreadable month-end, a
#    collect in between) is not split and stays in the receiving month.
#
# 2. Says UNKNOWN where the event feed did not reach. A cell is only complete
#    when that chain's event sync covered the month's claim window; claims
#    exist → "at least" (partial), none → unknown. Never a zero it cannot
#    support — the Pancake subgraph stopped on 2026-07-06, and every bsc cell
#    after that used to read "—", which looks like "no fees".

_SPLIT_EDGE_DAYS = 1  # a month-end snapshot may be at most this early


@dataclass
class FeeRow:
    label: str
    cells: dict[str, Decimal]  # chain → claimed USD (known part)
    claimed: Decimal
    mtm: Decimal | None
    status: str = "claimed"  # claimed | split | rolled | unswept | accruing | total
    partial: set[str] = field(default_factory=set)  # chains whose figure is a floor
    unknown: set[str] = field(default_factory=set)  # chains with no coverage at all
    note: str = ""


@dataclass
class FeeTable:
    basis: str  # 'accrual' | 'claim'
    chains: list[str]
    rows: list[FeeRow]
    accruing: FeeRow | None
    ytd: FeeRow
    lifetime: FeeRow
    notes: list[str]
    last_sweep: dict | None


def _month_end(month: str):
    y, m = (int(x) for x in month.split("-"))
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return nxt.fromordinal(nxt.toordinal() - 1)


def fee_event_coverage(health_rows: list[dict], active: set[tuple[str, str]]) -> dict[str, int]:
    """{chain: unix ts through which that chain's collects are known}. The
    minimum over the (wallet, protocol) feeds that hold positions there, so
    one stale feed makes the chain incomplete. `active` is {(wallet, chain)}
    with any event or snapshot — a wallet that never touched a chain does not
    make it incomplete."""
    out: dict[str, int] = {}
    for h in health_rows:
        if (h["wallet"], h["chain"]) not in active:
            continue
        ok = int(h.get("last_sync_ok_at") or 0)
        out[h["chain"]] = min(out.get(h["chain"], ok), ok)
    return out


def _qty(balances_json: str | None) -> dict[str, Decimal]:
    return {a["token_key"]: Decimal(str(a["amount"])) for a in json.loads(balances_json or "[]")}


def split_rolled_sweeps(
    collect_rows: list[dict], unclaimed_series: list[dict], open_positions: int | None = None
) -> tuple[list[dict], dict[str, dict]]:
    """Split multi-month sweeps by month-end unclaimed quantities.

    `collect_rows` must already be on the accrual basis (attribute_fee_months).
    `unclaimed_series` is Storage.unclaimed_series(). Returns (rows, info)
    where rows replaces collect_rows and info maps each split month to
    {sweep_date, receiving, positions_split, positions_unsplit, month_end_usd}.
    """
    series: defaultdict[tuple, list[dict]] = defaultdict(list)
    for s in unclaimed_series:
        series[(s["wallet"], s["chain"], s["protocol_id"], s["position_key"])].append(s)
    for v in series.values():
        v.sort(key=lambda s: s["snapshot_ts"])

    def key(r: dict) -> tuple:
        return (r["wallet"], r["chain"], r.get("protocol_id"), r["position_key"])

    sweeps = sweep_batches(collect_rows, open_positions=open_positions)
    replace: dict[int, list[dict]] = {}
    info: dict[str, dict] = {}
    for prev, cur in zip(sweeps, sweeps[1:], strict=False):
        a_cur = attribution_month(int(cur["ts"]))
        skipped = month_range(attribution_month(int(prev["ts"])), a_cur)[1:-1]
        if not skipped:
            continue
        sweep_date = datetime_date(int(cur["ts"]))
        for m in skipped:
            info.setdefault(
                m,
                {
                    "sweep_date": sweep_date,
                    "receiving": a_cur,
                    "positions_split": 0,
                    "positions_unsplit": 0,
                    "month_end_usd": Decimal("0"),
                },  # fmt: skip
            )
        in_cur = {id(r) for r in cur["rows"]}
        for r in cur["rows"]:
            k = key(r)
            others = [
                o for o in collect_rows
                if key(o) == k and id(o) not in in_cur and prev["ts"] < int(o["ts"]) < cur["start_ts"]
            ]  # fmt: skip
            snaps = series.get(k, [])
            after = [s for s in snaps if s["snapshot_ts"] > prev["ts"]]
            chain_pts = [after[0]] if after else []
            for m in skipped:
                end = _month_end(m)
                pick = [s for s in snaps if date.fromisoformat(s["snapshot_date"]) <= end]
                pt = pick[-1] if pick else None
                if (
                    pt is None
                    or (end - date.fromisoformat(pt["snapshot_date"])).days > _SPLIT_EDGE_DAYS
                ):
                    chain_pts = []
                    break
                chain_pts.append(pt)
            ok = (
                not others
                and len(chain_pts) == len(skipped) + 1
                and all(p["unclaimed_is_known"] and not p["is_closed"] for p in chain_pts)
                and chain_pts[0]["snapshot_ts"] < chain_pts[1]["snapshot_ts"]
            )
            claimed = {
                a["token_key"]: (Decimal(str(a["amount"])), Decimal(str(a.get("price_at_ts") or 0)))
                for a in json.loads(r["amounts_json"])
            }
            shares: list[dict[str, Decimal]] = []
            if ok:
                qs = [_qty(p["unclaimed_balances_json"]) for p in chain_pts]
                for i in range(1, len(qs)):
                    shares.append({t: qs[i].get(t, Decimal("0")) - qs[i - 1].get(t, Decimal("0"))
                                   for t in claimed})  # fmt: skip
                for t, (amt, _) in claimed.items():
                    tot = sum((s[t] for s in shares), Decimal("0"))
                    if any(s[t] < 0 for s in shares) or tot > amt * Decimal("1.000001"):
                        ok = False
            if not ok:
                for m in skipped:
                    info[m]["positions_unsplit"] += 1
                continue
            parts: list[dict] = []
            split_usd = Decimal("0")
            for i, (m, share) in enumerate(zip(skipped, shares, strict=True)):
                usd = sum((share[t] * claimed[t][1] for t in claimed), Decimal("0"))
                split_usd += usd
                info[m]["positions_split"] += 1
                info[m]["month_end_usd"] += Decimal(str(chain_pts[i + 1]["unclaimed_usd"])) - (
                    Decimal(str(chain_pts[i]["unclaimed_usd"]))
                )
                amounts = [
                    {**a, "amount": str(share[a["token_key"]])}
                    for a in json.loads(r["amounts_json"])
                ]
                parts.append({**r, "month": m, "usd_at_ts": usd,
                              "amounts_json": json.dumps(amounts), "split_from": sweep_date})  # fmt: skip
            rest_amounts = [
                {**a, "amount": str(claimed[a["token_key"]][0]
                                    - sum((s[a["token_key"]] for s in shares), Decimal("0")))}
                for a in json.loads(r["amounts_json"])
            ]  # fmt: skip
            parts.append({**r, "amounts_json": json.dumps(rest_amounts),
                          "usd_at_ts": Decimal(str(r["usd_at_ts"])) - split_usd})  # fmt: skip
            replace[id(r)] = parts
    out: list[dict] = []
    for r in collect_rows:
        out.extend(replace.get(id(r), [r]))
    return out, info


def datetime_date(ts: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def fee_table(
    collect_rows: list[dict],
    *,
    snaps: list[dict],
    unclaimed_series: list[dict],
    health_rows: list[dict],
    active: set[tuple[str, str]],
    price_today: dict | None = None,
    by_accrual: bool = True,
    now: int | None = None,
) -> FeeTable:
    """Month-by-month claimed fees, per chain, with every cell's status."""
    from datetime import UTC, datetime

    now_ts = int(datetime.now(UTC).timestamp()) if now is None else int(now)
    cur_month = datetime.fromtimestamp(now_ts, UTC).strftime("%Y-%m")
    open_n = len(snaps)
    rows = collect_rows
    split_info: dict[str, dict] = {}
    if by_accrual:
        rows = attribute_fee_months(rows)
        rows, split_info = split_rolled_sweeps(rows, unclaimed_series, open_positions=open_n)
    val = fee_valuation(rows, price_today or {})
    _, chains, cells = monthly_fee_matrix(rows)
    coverage = fee_event_coverage(health_rows, active)
    chains = sorted(set(chains) | {c for (_, c) in active if c in coverage})
    order = [c for c in ("peaq", "bsc", "base") if c in chains] + [
        c for c in chains if c not in ("peaq", "bsc", "base")
    ]

    sweep = last_sweep(rows, now=now_ts, open_positions=open_n) if by_accrual else None
    unswept = set(unswept_months(rows, now=now_ts, open_positions=open_n)) if by_accrual else set()
    rolled = rolled_up_months(rows, open_positions=open_n) if by_accrual else {}

    def window_end_ts(month: str) -> int:
        """When the last claim that can belong to `month` lands."""
        end = _month_end(month)
        if by_accrual:  # the sweep for M arrives in the first days of M+1
            end = end.fromordinal(end.toordinal() + _FEE_ACCRUAL_CUTOFF_DAY)
        return int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC).timestamp())

    first = val["months"][0] if val["months"] else cur_month
    months = month_range(first, max(val["months"][-1] if val["months"] else cur_month, cur_month))
    notes: list[str] = []
    out_rows: list[FeeRow] = []
    for m in months:
        mcells = {c: cells.get((m, c), Decimal("0")) for c in order}
        claimed = val["claim_by_month"].get(m, Decimal("0"))
        partial = {c for c in order if coverage.get(c, 0) < window_end_ts(m) and mcells[c] > 0}
        unknown = {c for c in order if coverage.get(c, 0) < window_end_ts(m) and mcells[c] == 0}
        row = FeeRow(m, mcells, claimed, val["today_by_month"].get(m) if val["priced"] else None,
                     partial=partial, unknown=unknown)  # fmt: skip
        if m in split_info:
            i = split_info[m]
            row.status = "split"
            row.note = (
                f"split from the {i['sweep_date']} sweep by month-end unclaimed quantities "
                f"(valued at claim price; {_usd_s(i['month_end_usd'])} at {m}'s month-end prices)"
                + (
                    f"; {i['positions_unsplit']} position(s) left in {i['receiving']}"
                    if i["positions_unsplit"]
                    else ""
                )  # fmt: skip
            )
        elif not claimed and m in unswept and m != cur_month:
            row.status = "unswept"
        elif not claimed and m in rolled:
            row.status = "rolled"
            row.note = f"no sweep of its own; its fees are inside {rolled[m]}"
        elif m == cur_month and not claimed:
            continue  # the accruing row carries the current month
        out_rows.append(row)

    accruing = None
    pend: dict[str, Decimal] = {}
    unreadable = 0
    for s in snaps:
        if not s.get("unclaimed_is_known", 1):
            unreadable += 1
            continue
        u = Decimal(str(s.get("unclaimed_usd") or 0))
        if u:
            pend[s["chain"]] = pend.get(s["chain"], Decimal("0")) + u
    if by_accrual and pend:
        tot = sum(pend.values(), Decimal("0"))
        accruing = FeeRow("accruing", {c: pend.get(c, Decimal("0")) for c in order}, tot, tot,
                          status="accruing",
                          note=(f"unclaimed since the last sweep on {sweep['date']}" if sweep
                                else "unclaimed so far")
                          + (f"; {unreadable} position(s) unreadable, not counted"
                             if unreadable else ""))  # fmt: skip

    def total(label: str, keep) -> FeeRow:
        ms = [r for r in out_rows if keep(r.label)]
        cs = {c: sum((r.cells[c] for r in ms), Decimal("0")) for c in order}
        partial = {c for r in ms for c in (r.partial | r.unknown)}
        mtm_vals = [r.mtm for r in ms]
        mtm = sum((v for v in mtm_vals if v is not None), Decimal("0")) if val["priced"] else None
        return FeeRow(label, cs, sum((r.claimed for r in ms), Decimal("0")), mtm,
                      status="total", partial=partial)  # fmt: skip

    year = cur_month[:4]
    ytd = total(f"YTD {year}", lambda m: m.startswith(year))
    lifetime = total("Lifetime", lambda m: True)
    last_shown = out_rows[-1].label if out_rows else months[-1]
    stale = {c: ts for c, ts in coverage.items() if ts < window_end_ts(last_shown) and c in order}
    window_end = window_end_ts(last_shown)
    for c, ts in sorted(stale.items()):
        when = datetime_date(ts) if ts else "never"
        # A window that has not ended yet is open, not broken: a sweep can
        # still land in it. Only a window that HAS ended means the sync lags.
        why = (
            f"{last_shown}'s window is open until {datetime_date(window_end)}"
            if window_end > now_ts
            else "event sync stopped"
        )
        notes.append(
            f"{c}: collects known only through {when} ({why}) — later cells are "
            "a floor (+) or unknown (?), not zero"
        )
    return FeeTable("accrual" if by_accrual else "claim", order, out_rows, accruing, ytd,
                    lifetime, notes, sweep)  # fmt: skip


def _usd_s(v: Decimal) -> str:
    return f"${v:,.0f}" if v >= 0 else f"-${-v:,.0f}"


# ── Scenario grid, greeks, exit-range probability (Phase 1.3–1.5) ─────────
#
# Everything here is closed-form from ONE snapshot row: no history, no price
# feed. A CL position's liquidity follows from its token amounts and ticks,
# and its tokens' USD prices follow from its own value at the pool price —
# so shocks, delta and gamma are self-consistent with the value the book
# reports, rather than mixing the pool price with a CoinGecko spot that can
# run ahead of it (it has, by several percent, on a fast day).
#
# A shock s moves the pool price (token0 in token1) by s with token1's USD
# price held fixed — the pair-relative move a range actually sees. For
# PEAQ-quoted pairs that IS a PEAQ move; `peaq_scenarios` maps a PEAQ shock
# onto whichever side of each pair PEAQ is on.

SHOCKS = (-0.50, -0.20, -0.10, -0.05, 0.05, 0.10, 0.20, 0.50)
# Upper-cased symbols that move with PEAQ under a shock (compare `.upper()`).
PEAQ_EXPOSURE_SYMBOLS = PEAQ_SYMBOLS | {"WSTPEAQ"}
_LN_TICK = math.log(1.0001)
# USD per unit of (L·√P·p1) to move the price +2% and −2% with flat
# liquidity: (√1.02 − 1) + (1/√0.98 − 1) — the "±2% depth" desks quote.
_DEPTH_2PCT = (math.sqrt(1.02) - 1) + (1 / math.sqrt(0.98) - 1)


@dataclass(frozen=True)
class RiskModel:
    """The pieces of a position that price moves act on."""

    kind: str  # 'cl' | 'xyk'
    value_usd: float
    x: float  # token0 amount (human units)
    y: float  # token1 amount
    p0: float  # token0 USD price implied by the snapshot's own value
    p1: float  # token1 USD price
    sqrt_lo: float = 0.0  # √(price) at the range bounds, human units (CL only)
    sqrt_hi: float = 0.0
    sqrt_p: float = 0.0
    liquidity: float = 0.0  # in human units, consistent with the √prices
    ticks: tuple[int, int, int] | None = None  # (lower, current, upper)

    def value_at(self, s: float) -> float:
        """USD value after the pool price moves by s (token1 fixed)."""
        if self.kind == "xyk":
            return self.value_usd * math.sqrt(1 + s)
        sp = self.sqrt_p * math.sqrt(1 + s)
        lo, hi, L = self.sqrt_lo, self.sqrt_hi, self.liquidity
        if sp <= lo:
            x, y = L * (1 / lo - 1 / hi), 0.0
        elif sp >= hi:
            x, y = 0.0, L * (hi - lo)
        else:
            x, y = L * (1 / sp - 1 / hi), L * (sp - lo)
        return x * self.p0 * (1 + s) + y * self.p1

    def hold_at(self, s: float) -> float:
        """Holding today's amounts through the same move."""
        return self.x * self.p0 * (1 + s) + self.y * self.p1


def risk_model(row: dict, decimals: dict[str, int]) -> RiskModel | None:
    """A RiskModel from a snapshot row, or None when the row cannot support
    one (non-AMM, unknown ticks or decimals, zero value). None, not a guess."""
    value = float(row.get("current_value_usd") or 0)
    bal = json.loads(row.get("current_balances_json") or "[]")
    if value <= 0 or len(bal) != 2:
        return None
    x, y = float(bal[0]["amount"]), float(bal[1]["amount"])
    kind = row.get("protocol_kind")
    if kind == "xyk_amm":
        if x <= 0 or y <= 0:
            return None
        # Constant product: equal USD on each leg.
        return RiskModel("xyk", value, x, y, value / 2 / x, value / 2 / y)
    raw_ticks = (row.get("tick_lower"), row.get("tick_current"), row.get("tick_upper"))
    d0, d1 = decimals.get(bal[0]["token_key"]), decimals.get(bal[1]["token_key"])
    if None in raw_ticks or d0 is None or d1 is None:
        return None
    lo, cur, hi = (int(t) for t in raw_ticks)  # type: ignore[arg-type]
    if hi <= lo:
        return None
    scale = 10 ** ((d0 - d1) / 2)  # √(human price) = √(raw price) · 10^((d0−d1)/2)
    s_lo, s_hi, s_p = (math.exp(t * _LN_TICK / 2) * scale for t in (lo, hi, cur))
    # Liquidity from whichever leg is non-zero at the current price.
    if s_p <= s_lo:
        L = x / (1 / s_lo - 1 / s_hi)
    elif s_p >= s_hi:
        L = y / (s_hi - s_lo)
    elif x > 0:
        L = x / (1 / s_p - 1 / s_hi)
    else:
        L = y / (s_p - s_lo)
    price = s_p * s_p  # token0 in token1, human units
    denom = x * price + y  # value in token1 units
    if denom <= 0 or L <= 0:
        return None
    p1 = value / denom
    return RiskModel("cl", value, x, y, price * p1, p1, s_lo, s_hi, s_p, L, (lo, cur, hi))


def _exit_probability(dist_lo: float, width: float, sigma: float, horizon: float) -> float:
    """P(a driftless Brownian motion started `dist_lo` above the lower edge of
    [0, width] leaves it within `horizon`), all in the same log-price units —
    the classical first-exit series."""
    if width <= 0 or sigma <= 0 or horizon <= 0:
        return float("nan")
    stay = 0.0
    k = (math.pi * sigma) ** 2 * horizon / (2 * width**2)
    for n in range(1, 200, 2):
        stay += 4 / (n * math.pi) * math.sin(n * math.pi * dist_lo / width) * math.exp(-(n**2) * k)
    return min(1.0, max(0.0, 1 - stay))


def _hit_probability(distance: float, sigma: float, horizon: float) -> float:
    """P(a driftless Brownian motion reaches a level `distance` away within
    `horizon`) — the reflection principle."""
    if sigma <= 0 or horizon <= 0:
        return float("nan")
    z = distance / (sigma * math.sqrt(horizon))
    return math.erfc(z / math.sqrt(2))


def position_risk(
    row: dict,
    decimals: dict[str, int],
    sigma_ticks_daily: float | None = None,
    n_obs: int = 0,
    horizon_days: float = 30.0,
    shocks: tuple[float, ...] = SHOCKS,
) -> dict | None:
    """Scenario grid, greeks, exit probability and breakeven fee APR for one
    snapshot row. σ is optional: without it the σ-dependent figures are None.

    - shocks: {s: {value, change, vs_hold}} — vs_hold is the IL the move adds.
    - delta_1pct: $ change for a +1% move; gamma_1pct: change in delta per 1%.
      A concentrated LP is short gamma in range and linear outside it.
    - exit_prob: P(leaving the range within the horizon); for a position
      already out, reentry_prob: P(price reaches the range within it).
    - breakeven_apr: the fee APR that pays for the IL expected at σ, from the
      IL gamma: E[IL] per day ≈ ½·(V″ − H″)·σ², V″, H″ in log-price. Its
      band is the ±1.96·SE of σ from n observations, stated so a figure from
      30 noisy days is not read as precise.
    """
    m = risk_model(row, decimals)
    if m is None:
        return None
    # Differences are taken against the MODEL's own value at today's price,
    # not the snapshot's: stored amounts are rounded, so liquidity implied by
    # one leg reproduces the other to within a few dollars, and a second
    # difference divides that residue by h² — a mismatch of a few dollars became
    # a four-digit "breakeven APR" before this. Against its own base the model is
    # exact; the tiny offset cancels.
    base = m.value_at(0.0)
    hold0 = m.hold_at(0.0)
    grid = {
        s: {
            "value": m.value_usd + m.value_at(s) - base,
            "change": m.value_at(s) - base,
            "vs_hold": (m.value_at(s) - base) - (m.hold_at(s) - hold0),
        }
        for s in shocks
    }
    h = 0.01
    up, dn = m.value_at(h), m.value_at(-h)
    delta = (up - dn) / 2
    gamma = up - 2 * base + dn
    out: dict = {
        "kind": m.kind,
        "value": m.value_usd,
        "shocks": grid,
        "delta_1pct": delta,
        "gamma_1pct": gamma,
        "in_range": None,
        "exit_prob": None,
        "reentry_prob": None,
        "breakeven_apr": None,
        "breakeven_band": None,
        "sigma_daily_pct": None,
        "n_obs": n_obs,
        "horizon_days": horizon_days,
    }
    if m.ticks is not None:
        lo, cur, hi = m.ticks
        out["in_range"] = lo <= cur <= hi
    if not sigma_ticks_daily or sigma_ticks_daily <= 0:
        return out
    sig = sigma_ticks_daily * _LN_TICK  # daily σ of log price
    out["sigma_daily_pct"] = (math.exp(sig) - 1) * 100
    if m.ticks is not None:
        u_lo, u_cur, u_hi = (t * _LN_TICK for t in m.ticks)
        if u_lo <= u_cur <= u_hi:
            out["exit_prob"] = _exit_probability(u_cur - u_lo, u_hi - u_lo, sig, horizon_days)
        else:
            gap = min(abs(u_cur - u_lo), abs(u_cur - u_hi))
            out["reentry_prob"] = _hit_probability(gap, sig, horizon_days)
    # IL gamma in log price: second difference of (V − H) over u = ln(1+s).
    e = 0.005
    su, sd = math.exp(e) - 1, math.exp(-e) - 1

    def il(s: float) -> float:
        return (m.value_at(s) - base) - (m.hold_at(s) - hold0)

    il2 = (il(su) - 2 * il(0.0) + il(sd)) / e**2
    daily_il = -0.5 * il2 * sig**2  # positive = expected loss vs holding
    if m.value_usd > 0:
        # Out of range a position is linear in price and expects no IL: 0, not
        # a negative number from rounding.
        apr = daily_il * 365 / m.value_usd * 100
        apr = apr if apr > 1e-9 else 0.0
        out["breakeven_apr"] = apr
        if n_obs > 2:
            rel = 1.96 / math.sqrt(2 * (n_obs - 1))
            lo_f, hi_f = max(0.0, 1 - rel) ** 2, (1 + rel) ** 2  # APR ∝ σ²
            out["breakeven_band"] = (apr * lo_f, apr * hi_f)
    return out


def tick_volatility_with_counts(
    history_rows: list[dict],
) -> dict[tuple[str, str], tuple[float, int]]:
    """{(chain, pair_label): (σ_ticks/day, number of daily returns)} — the
    count is what makes an error bar possible."""
    series: defaultdict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for r in history_rows:
        if r.get("tick_current") is None or r.get("is_closed"):
            continue
        series[(r["chain"], r["pair_label"])][r["snapshot_date"]] = int(r["tick_current"])
    out: dict[tuple[str, str], tuple[float, int]] = {}
    for key, by_day in series.items():
        days = sorted(by_day)
        rets = [
            by_day[b] - by_day[a]
            for a, b in zip(days, days[1:], strict=False)
            if (date.fromisoformat(b) - date.fromisoformat(a)).days == 1
        ]
        if len(rets) >= 5:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / len(rets)
            out[key] = (math.sqrt(var), len(rets))
    return out


def daily_log_returns(history_rows: list[dict]) -> dict[tuple[str, str], tuple[float, ...]]:
    """{(chain, pair_label): daily log returns of the pool price, oldest
    first} from consecutive days' closing ticks — the series a strategy
    variant builds its own σ from (a longer window, an EWMA)."""
    series: defaultdict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for r in history_rows:
        if r.get("tick_current") is None or r.get("is_closed"):
            continue
        series[(r["chain"], r["pair_label"])][r["snapshot_date"]] = int(r["tick_current"])
    out: dict[tuple[str, str], tuple[float, ...]] = {}
    for key, by_day in series.items():
        days = sorted(by_day)
        out[key] = tuple(
            (by_day[b] - by_day[a]) * _LN_TICK
            for a, b in zip(days, days[1:], strict=False)
            if (date.fromisoformat(b) - date.fromisoformat(a)).days == 1
        )
    return out


# ── Range optimiser (Phase 5.1) ───────────────────────────────────────────
#
# Per pool: the band that maximises expected fees − expected IL over a
# horizon. Same driftless log-price model as position_risk, and every piece
# is a closed form (each is checked against one in tests/test_optimiser.py):
#
#   occupancy  E[days in band] = ∫₀ᵀ P(a ≤ u_t ≤ b) dt with u_t ~ N(0, σ²t),
#              from ∫₀ᵀ Φ(−c/√t) dt = (T + c²)·Φ(−c/√T) − c·√T·φ(c/√T), c = d/σ.
#              Occupancy, not first exit: the position is not touched inside
#              the horizon, so a price that leaves and comes back earns again.
#   fees       pool fees/day × share × occupancy, share = L / (L + L_other).
#              A narrower band puts more liquidity L behind the same capital,
#              so the share rises — and saturates as L passes L_other. On a
#              pool the book already dominates, narrowing buys almost nothing.
#   IL         L·√P·σ²/4 per day in band (token1 units) — the identity the
#              breakeven APR is checked against; out of band a position is
#              linear in price and accrues none.
#   book view  fees a position takes from the book's OTHER positions in the
#              same pool are not a gain to the book: book fees = fees ×
#              L_ext / L_other, L_ext the liquidity that is not ours.
#   sizing     d(book fees)/dL = d(IL)/dL  ⇒  L* = √(F·L_ext / c) − L_other,
#              c = √P·p1·σ²/4 — the optimal LIQUIDITY does not depend on the
#              band; the capital it takes does.
#
# What it holds fixed, so it is not mistaken for more:
#   - pool volume's response to depth: fees ∝ depth^ε (RebalanceConfig.
#     volume_elasticity), from the last 7 days' fees at today's depth. ε is
#     measured where history allows (5.3, measured_rebalance_config), with a
#     band: bands are chosen at the central ε and every
#     answer is also scored at the bracket's ends (ε = 0, volume fixed;
#     ε = 1, volume proportional to depth) — its volume band. At ε = 0 a
#     book that IS the pool always prefers wider; ε > 0 prices the flow a
#     thinner pool loses. Depth outside depth_bounds × today's is not
#     scored: the response is extrapolation there.
#   - other LPs' liquidity is flat across the band at today's active level
#     (their tick distribution is not read).
#   - √P at today's value in the IL rate (error grows with band width).
#   - Brownian log price. Fat tails make realised IL worse than this.
#   - σ from daily snapshot ticks; its 95% band (±1.96/√(2(n−1)) on σ) is
#     carried to every answer, and too few returns is "not enough history".


def _norm_pdf(z: float) -> float:
    return math.exp(-z * z / 2) / math.sqrt(2 * math.pi)


def _norm_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _days_beyond(c: float, horizon: float) -> float:
    """∫₀ᵀ Φ(−c/√t) dt, c ≥ 0 (in units of σ·√day): expected days a
    driftless walk from 0 spends above the level c within T."""
    rt = math.sqrt(horizon)
    return (horizon + c * c) * _norm_cdf(-c / rt) - c * rt * _norm_pdf(c / rt)


def occupancy_days(d_lo: float, d_hi: float, sigma: float, horizon: float) -> float:
    """Expected days log price, starting at 0, spends in [d_lo, d_hi] within
    `horizon` days, σ in log units per √day. The band may sit entirely on one
    side of today's price (an out-of-range position)."""
    if d_hi <= d_lo or horizon <= 0:
        return 0.0
    if sigma <= 0:
        return horizon if d_lo <= 0 <= d_hi else 0.0

    def below(d: float) -> float:  # expected days with u_t < d
        beyond = _days_beyond(abs(d) / sigma, horizon)
        return beyond if d <= 0 else horizon - beyond

    return max(0.0, below(d_hi) - below(d_lo))


def liquidity_per_usd(sqrt_p: float, sqrt_lo: float, sqrt_hi: float, p1: float) -> float:
    """Liquidity (human units) one USD buys in [lo, hi] at today's price —
    the inverse of a unit of L's value, x·P·p1 + y·p1, with √P clamped to
    the band (an out-of-band deposit is single-sided)."""
    sp = min(max(sqrt_p, sqrt_lo), sqrt_hi)
    token1_per_l = sqrt_p * sqrt_p * (1 / sp - 1 / sqrt_hi) + (sp - sqrt_lo)
    return 1 / (token1_per_l * p1) if token1_per_l > 0 and p1 > 0 else 0.0


@dataclass(frozen=True)
class BandEV:
    """Expected economics of liquidity L in one band over the horizon, USD."""

    occupancy_days: float
    share: float  # of the pool's active liquidity while in band
    fees_usd: float  # the position's expected fees
    own_fees_usd: float  # …of which come out of the book's other positions
    il_usd: float  # expected IL vs holding (positive = a loss)

    @property
    def book_fees_usd(self) -> float:
        return self.fees_usd - self.own_fees_usd

    @property
    def ev_usd(self) -> float:
        """What the band adds to the BOOK: its fees from outside liquidity,
        less the IL it is expected to take."""
        return self.book_fees_usd - self.il_usd


def band_ev(
    liquidity: float,
    sqrt_p: float,
    p1: float,
    d_lo: float,
    d_hi: float,
    fees_per_day: float,
    l_ext: float,
    l_own: float,
    sigma: float,
    horizon: float,
    *,
    elasticity: float = 0.0,
    depth_ref: float = 0.0,
) -> BandEV:
    """BandEV for `liquidity` (human units) in a band whose edges sit d_lo,
    d_hi away from today's log price. l_ext / l_own: other LPs' / the book's
    other positions' active liquidity, flat across the band.

    Pool fees respond to the pool's depth D = l_ext + l_own + liquidity as
    F(D) = fees_per_day · (D / depth_ref)^elasticity (depth_ref: today's
    depth; elasticity 0 holds volume fixed). The book's fees from this
    position are B(L) − B(0), B(L) = F(D)·(L + l_own)/D — the book's share
    of the pool's fees with it, less without it — so a position that only
    takes fees from its siblings adds nothing, and under ε > 0 one that
    brings depth brings volume. At ε = 0 this is fees · l_ext / l_other."""
    occ = occupancy_days(d_lo, d_hi, sigma, horizon)
    l_other = l_ext + l_own

    def pool_fees(depth: float) -> float:
        if elasticity and depth_ref > 0:
            return fees_per_day * (depth / depth_ref) ** elasticity if depth > 0 else 0.0
        return fees_per_day

    depth = liquidity + l_other
    share = liquidity / depth if depth > 0 else 0.0
    fees = pool_fees(depth) * occ * share
    with_it = pool_fees(depth) * (liquidity + l_own) / depth if depth > 0 else 0.0
    without = pool_fees(l_other) * l_own / l_other if l_other > 0 else 0.0
    own = fees - occ * (with_it - without)
    il = occ * liquidity * sqrt_p * p1 * sigma * sigma / 4
    return BandEV(occ, share, fees, own, il)


def optimal_liquidity(
    fees_per_day: float, l_ext: float, l_other: float, sqrt_p: float, p1: float, sigma: float
) -> float | None:
    """L* where the book's marginal fee equals the marginal expected IL:
    √(F·L_ext/c) − L_other, c = √P·p1·σ²/4, floored at 0 (hold nothing).
    None when every other unit of liquidity in the pool is ours (L_other = 0):
    with volume held fixed any liquidity at all takes every fee, and the
    model cannot size that — pool depth is then what sets the volume."""
    c = sqrt_p * p1 * sigma * sigma / 4
    if l_other <= 0 or c <= 0:
        return None
    return max(0.0, math.sqrt(fees_per_day * l_ext / c) - l_other)


def best_liquidity(ev_of, lo: float, hi: float, iters: int = 90) -> float:
    """argmax of a concave EV(L) on [lo, hi] by golden section. Book EV is
    concave in L for ε ∈ [0, 1]: the fee term is concave, IL linear."""
    if hi <= lo:
        return max(lo, 0.0)
    g = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = ev_of(c), ev_of(d)
    for _ in range(iters):
        if fc < fd:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = ev_of(d)
        else:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = ev_of(c)
    best = (a + b) / 2
    # The ends are admissible too: a concave function can peak on either.
    return max((lo, best, hi), key=ev_of)


def infer_tick_spacing(ticks: list[int]) -> int:
    """The gcd of the pool's position bounds: a multiple of the true spacing,
    so a band rounded to it is always placeable (possibly coarser than the
    finest the pool allows)."""
    g = 0
    for t in ticks:
        g = math.gcd(g, abs(int(t)))
    return g or 1


_MAX_TICK = 887272


def snap_band(tick_current: int, half_ticks: float, spacing: int) -> tuple[int, int]:
    """[lower, upper] around the current tick, rounded OUTWARD to the spacing,
    at least one spacing either side."""
    half = max(float(spacing), half_ticks)
    lo = math.floor((tick_current - half) / spacing) * spacing
    hi = math.ceil((tick_current + half) / spacing) * spacing
    lo = max(lo, -(_MAX_TICK // spacing) * spacing)
    hi = min(hi, (_MAX_TICK // spacing) * spacing)
    return lo, hi


@dataclass(frozen=True)
class BandChoice:
    ticks: tuple[int, int]
    liquidity: float
    ev: BandEV
    at_edge: bool  # the widest or narrowest searched — the optimum may lie beyond
    depth_change: float  # its effect on the pool's active liquidity, as a fraction
    at_bound: bool = False  # the pool's depth after it sits at a depth_bounds edge


def _sqrt_at(tick: int, scale: float) -> float:
    return math.exp(tick * _LN_TICK / 2) * scale


def best_band(
    *,
    capital_usd: float,
    tick_current: int,
    sqrt_p: float,
    scale: float,
    p1: float,
    fees_per_day: float,
    l_ext: float,
    l_own: float,
    l_active_before: float,
    sigma: float,
    horizon: float,
    spacing: int,
    depth_bounds: tuple[float, float],
    z_grid: tuple[float, ...],
    elasticity: float = 0.0,
) -> BandChoice | None:
    """The symmetric band (about today's price, no directional view) that
    maximises book EV for `capital_usd`, over half-widths z·σ·√T, with pool
    fees ∝ depth^elasticity. A band that would leave the pool's active
    liquidity outside depth_bounds × today's is skipped: the depth response
    is extrapolation there. None when nothing searched is admissible."""
    sigma_ticks = sigma / _LN_TICK
    best: BandChoice | None = None
    seen: set[tuple[int, int]] = set()
    lo_z, hi_z = min(z_grid), max(z_grid)
    for z in sorted(z_grid):
        lo, hi = snap_band(tick_current, z * sigma_ticks * math.sqrt(horizon), spacing)
        if (lo, hi) in seen:
            continue
        seen.add((lo, hi))
        s_lo, s_hi = _sqrt_at(lo, scale), _sqrt_at(hi, scale)
        liq = capital_usd * liquidity_per_usd(sqrt_p, s_lo, s_hi, p1)
        after = l_ext + l_own + liq
        ratio = after / l_active_before if l_active_before else 1.0
        if not depth_bounds[0] <= ratio <= depth_bounds[1]:
            continue
        d_lo, d_hi = (lo - tick_current) * _LN_TICK, (hi - tick_current) * _LN_TICK
        ev = band_ev(
            liq, sqrt_p, p1, d_lo, d_hi, fees_per_day, l_ext, l_own, sigma, horizon,
            elasticity=elasticity, depth_ref=l_active_before,
        )  # fmt: skip
        if best is None or ev.ev_usd > best.ev.ev_usd:
            near = ratio < depth_bounds[0] * 1.1 or ratio > depth_bounds[1] * 0.9
            best = BandChoice((lo, hi), liq, ev, z in (lo_z, hi_z), ratio - 1, near)
    return best


def sigma_band(sigma: float, n_obs: int) -> tuple[float, float]:
    """95% interval on σ from n returns: σ·(1 ∓ 1.96/√(2(n−1)))."""
    rel = 1.96 / math.sqrt(2 * (n_obs - 1)) if n_obs > 1 else 1.0
    return sigma * max(0.0, 1 - rel), sigma * (1 + rel)


@dataclass
class PoolMember:
    """One of the book's positions in a pool, as the optimiser sees it."""

    row: dict
    model: RiskModel

    @property
    def in_range(self) -> bool:
        lo, cur, hi = self.model.ticks or (0, 0, -1)
        return lo <= cur <= hi


@dataclass
class PoolState:
    """A pool as the book sits in it today. Every None is unknown."""

    key: tuple[str, str, str]  # (chain, protocol_id, pair_label)
    members: list[PoolMember]
    unmodelled: list[dict]
    fees_per_day: float | None
    sigma: float | None  # daily σ of log price
    n_obs: int
    l_active: float | None  # the pool's active liquidity, human units
    spacing: int
    tick_current: int | None
    sqrt_p: float
    scale: float
    p0: float
    p1: float
    returns: tuple[float, ...] = ()  # daily log returns, oldest first, up to a year

    @property
    def l_ours_active(self) -> float:
        return sum(m.model.liquidity for m in self.members if m.in_range)

    @property
    def l_ext(self) -> float | None:
        """Active liquidity that is not the book's. Floored at 0: the book's
        share is read from rounded amounts, and 100.1% ours means ~all."""
        if self.l_active is None:
            return None
        return max(0.0, self.l_active - self.l_ours_active)

    @property
    def book_share(self) -> float | None:
        if not self.l_active:
            return None
        return min(1.0, self.l_ours_active / self.l_active)

    def unknown_reason(self, min_obs: int) -> str | None:
        """Why this pool cannot be optimised, in words — or None if it can."""
        if not self.members:
            return "no position the risk model can read (ticks or decimals unknown)"
        if self.fees_per_day is None:
            return "pool fees unknown (the 7-day fee read failed)"
        if self.sigma is None:
            return "no σ yet — fewer than 5 consecutive daily ticks"
        if self.n_obs < min_obs:
            return f"not enough history — σ from {self.n_obs} daily returns, needs {min_obs}"
        if self.l_active is None:
            return "pool active liquidity unknown (no in-range share or pool read)"
        return None


def pool_states(
    snaps: list[dict],
    decimals: dict[str, int],
    vol: dict[tuple[str, str], tuple[float, int]],
    returns: dict[tuple[str, str], tuple[float, ...]] | None = None,
) -> list[PoolState]:
    """Group the book's live CL positions by pool — (chain, protocol,
    pair label), the key σ is measured on — and establish what each pool
    needs for the optimiser. The pool's active liquidity comes from the
    adapter's direct read (meta pool_liquidity, raw) or, failing that, from
    any in-range position: its liquidity ÷ its share."""
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in snaps:
        if r.get("protocol_kind") != "cl_amm" or r.get("is_closed"):
            continue
        groups[(r["chain"], r["protocol_id"], r["pair_label"])].append(r)
    out: list[PoolState] = []
    for key, rows in sorted(groups.items()):
        members, unmodelled = [], []
        for r in rows:
            m = risk_model(r, decimals)
            if m is not None and m.kind == "cl":
                members.append(PoolMember(r, m))
            else:
                unmodelled.append(r)
        fee_vals = [r.get("seven_day_fees_usd") for r in rows]
        fees = next((float(f) / 7 for f in fee_vals if f is not None), None)
        sv = vol.get((key[0], key[2]))
        ticks = [
            int(t)
            for r in rows
            for t in (r.get("tick_lower"), r.get("tick_upper"))
            if t is not None
        ]
        ref = members[0].model if members else None
        l_active = _pool_active_liquidity(members, decimals)
        out.append(
            PoolState(
                key=key,
                members=members,
                unmodelled=unmodelled,
                fees_per_day=fees,
                sigma=sv[0] * _LN_TICK if sv else None,
                n_obs=sv[1] if sv else 0,
                l_active=l_active,
                spacing=infer_tick_spacing(ticks),
                tick_current=ref.ticks[1] if ref and ref.ticks else None,
                sqrt_p=ref.sqrt_p if ref else 0.0,
                scale=(ref.sqrt_p / _sqrt_at(ref.ticks[1], 1.0)) if ref and ref.ticks else 1.0,
                p0=ref.p0 if ref else 0.0,
                p1=ref.p1 if ref else 0.0,
                returns=(returns or {}).get((key[0], key[2]), ()),
            )
        )
    return out


def _pool_active_liquidity(members: list[PoolMember], decimals: dict[str, int]) -> float | None:
    for m in members:
        raw = json.loads(m.row.get("meta_json") or "{}").get("pool_liquidity")
        bal = json.loads(m.row.get("current_balances_json") or "[]")
        if raw is None or len(bal) != 2:
            continue
        d0, d1 = decimals.get(bal[0]["token_key"]), decimals.get(bal[1]["token_key"])
        if d0 is not None and d1 is not None:
            # L_human = L_raw · 10^−(d0+d1)/2, the scale risk_model's √P uses.
            return int(raw) * 10 ** (-(d0 + d1) / 2)
    shares = [
        (float(m.row["position_share_pct"]), m.model.liquidity)
        for m in members
        if m.in_range and m.row.get("position_share_pct")
    ]
    if not shares:
        return None
    pct, liq = max(shares)  # the largest share carries the least rounding
    return liq / (pct / 100)


@dataclass
class PoolPlan:
    """The 5.1 answer for one pool: the band that maximises the book's EV
    for the capital it has there, with σ's error bar carried through."""

    state: PoolState
    reason: str | None  # why there is no answer — None when there is one
    capital_usd: float = 0.0
    horizon_days: float = 30.0
    best: BandChoice | None = None
    best_lo_sigma: BandChoice | None = None  # optimum at the low end of σ's band
    best_hi_sigma: BandChoice | None = None
    ev_band: tuple[float, float] | None = None  # the chosen band's EV across σ's band
    volume_band: tuple[float, float] | None = None  # …and across the ε bracket, at central σ
    elasticity: float = 0.0  # the ε the band was chosen at
    today_ev: float | None = None  # the book's positions as they sit
    optimal_capital_usd: float | None = None  # at the chosen band, from L*
    notes: list[str] = field(default_factory=list)

    @property
    def half_pct(self) -> tuple[float, float] | None:
        """The chosen band as (−x%, +y%) around today's price."""
        if self.best is None or self.state.tick_current is None:
            return None
        lo, hi = self.best.ticks
        cur = self.state.tick_current
        return (pow(1.0001, lo - cur) - 1) * 100, (pow(1.0001, hi - cur) - 1) * 100


def _member_stay_ev(
    state: PoolState, m: PoolMember, sigma: float, horizon: float, elasticity: float = 0.0
) -> BandEV:
    """A member's EV left exactly where it is, against everyone else in the pool."""
    lo, cur, hi = m.model.ticks  # type: ignore[misc]
    l_own = state.l_ours_active - (m.model.liquidity if m.in_range else 0.0)
    return band_ev(
        m.model.liquidity, m.model.sqrt_p, m.model.p1,
        (lo - cur) * _LN_TICK, (hi - cur) * _LN_TICK,
        state.fees_per_day or 0.0, state.l_ext or 0.0, l_own, sigma, horizon,
        elasticity=elasticity, depth_ref=state.l_active or 0.0,
    )  # fmt: skip


def optimise_pool(state: PoolState, config: RebalanceConfig) -> PoolPlan:
    """Solve one pool: the book's capital there as ONE symmetric band, other
    LPs' liquidity as the competition, pool fees ∝ depth^ε at the central ε.
    Compared against the book as it sits, each position valued against all
    the others."""
    horizon = float(config.target_horizon_days)
    eps = config.volume_elasticity
    reason = state.unknown_reason(config.min_sigma_obs)
    plan = PoolPlan(state=state, reason=reason, horizon_days=horizon, elasticity=eps)
    if reason is not None:
        return plan
    assert state.sigma is not None and state.fees_per_day is not None
    assert state.tick_current is not None and state.l_active is not None
    capital = sum(m.model.value_usd for m in state.members)
    plan.capital_usd = capital
    l_ext = state.l_ext or 0.0
    F, D0, cur = state.fees_per_day, state.l_active, state.tick_current

    def solve(sigma: float) -> BandChoice | None:
        return best_band(
            capital_usd=capital, tick_current=cur, sqrt_p=state.sqrt_p,
            scale=state.scale, p1=state.p1, fees_per_day=F,
            l_ext=l_ext, l_own=0.0, l_active_before=D0, sigma=sigma,
            horizon=horizon, spacing=state.spacing,
            depth_bounds=config.depth_bounds, z_grid=config.z_grid, elasticity=eps,
        )  # fmt: skip

    s_lo, s_hi = sigma_band(state.sigma, state.n_obs)
    plan.best = solve(state.sigma)
    plan.best_lo_sigma, plan.best_hi_sigma = solve(s_lo), solve(s_hi)
    # The book as it sits: GROSS fees — each position's fees from its own
    # siblings are still the book's, and netting them per position would
    # make a pool the book owns outright look as if it earned nothing.
    stays = [_member_stay_ev(state, m, state.sigma, horizon, eps) for m in state.members]
    plan.today_ev = sum(e.fees_usd - e.il_usd for e in stays)
    if plan.best is None:
        lo_b, hi_b = config.depth_bounds
        plan.reason = (
            f"every band searched would leave the pool's depth outside {lo_b:g}–{hi_b:g}× "
            "today's — the depth response is extrapolation there"
        )
        return plan
    lo, hi = plan.best.ticks
    d_lo, d_hi = (lo - cur) * _LN_TICK, (hi - cur) * _LN_TICK

    def ev(liq: float, sigma: float, e: float) -> float:
        return band_ev(
            liq, state.sqrt_p, state.p1, d_lo, d_hi, F, l_ext, 0.0, sigma, horizon,
            elasticity=e, depth_ref=D0,
        ).ev_usd  # fmt: skip

    L = plan.best.liquidity
    plan.ev_band = (
        min(ev(L, x, eps) for x in (s_lo, state.sigma, s_hi)),
        max(ev(L, x, eps) for x in (s_lo, state.sigma, s_hi)),
    )
    plan.volume_band = tuple(ev(L, state.sigma, e) for e in config.elasticity_band)  # type: ignore[assignment]
    per_usd = liquidity_per_usd(
        state.sqrt_p, _sqrt_at(lo, state.scale), _sqrt_at(hi, state.scale), state.p1
    )
    if per_usd > 0:
        lo_l = max(0.0, config.depth_bounds[0] * D0 - l_ext)
        hi_l = config.depth_bounds[1] * D0 - l_ext
        l_star = best_liquidity(lambda x: ev(x, state.sigma or 0.0, eps), lo_l, hi_l)
        plan.optimal_capital_usd = l_star / per_usd
    if plan.best.at_edge or plan.best.at_bound:
        why = (
            f" (depth {plan.best.depth_change + 1:.2g}× today's, at the "
            f"{config.depth_bounds[0]:g}–{config.depth_bounds[1]:g}× limit)"
            if plan.best.at_bound
            else ""
        )
        plan.notes.append(
            f"the best band sits at the edge of what was searched{why} — a direction, not a "
            "destination"
        )
    return plan


# ── EV-ranked action queue (Phase 5.2) ────────────────────────────────────
#
# Every candidate action on a position is scored in dollars over the horizon
# against leaving it where it is, with the arithmetic kept, and the best one
# becomes a trade ticket for manual execution from the multisig. The tool
# never signs; a ticket is a proposal.
#
# Candidates: re-range to the optimiser's band (same capital); re-range and
# resize to L* (add or cut); close; harvest and compound. Within a pool they
# are taken greedily — best ticket first, the pool updated as if it were
# done, the rest re-scored — because two tickets that each assume the other
# is not executed can both be wrong (closing BOTH of two positions that own
# a pool is not twice the gain of closing one). Each ticket says its order.
#
# Costs. Gas is ~$0 on these chains (cents per transaction). The swap that
# rebalances the token mix is measured as a notional; its cost is the
# operator's number (RebalanceConfig.swap_cost_pct) — unset, a ticket states
# the swap cost it breaks even at rather than assume one. The IL a close
# makes permanent is shown on every ticket; it is not charged by default,
# because under a driftless price its expected recovery is zero and the
# "stay" branch already carries the IL that staying is expected to add —
# charging it too would count it twice and argue for never acting on a loss.
# RebalanceConfig.charge_realised_il turns it on.

_ACTION_TITLES = {
    "rerange": "Re-range",
    "resize": "Re-range and resize",
    "close": "Close",
    "harvest": "Harvest and compound",
}


@dataclass
class Ticket:
    """One proposed action, sized and priced, for manual execution."""

    uid: str  # the position's uid — the queue's key
    wallet: str
    chain: str
    protocol_id: str
    position_key: str
    pair: str
    action: str  # 'rerange' | 'resize' | 'close' | 'harvest'
    value_usd: float
    ticks_now: tuple[int, int, int]  # (lower, current, upper)
    new_ticks: tuple[int, int] | None
    withdraw: list[tuple[str, float]]  # (symbol, amount) taken out
    deposit: list[tuple[str, float]]  # (symbol, amount) put in
    capital_after_usd: float
    stay: BandEV
    after: BandEV | None  # None: the capital leaves the pool
    gain_usd: float  # after − stay, central σ, before any swap cost
    gain_band: tuple[float, float]  # across σ's 95% band
    swap_usd: float  # notional to rebalance the token mix
    swap_leg: tuple[str, str] | None  # (sell, buy)
    swap_cost_usd: float | None  # None: no cost given — see breakeven_swap_pct
    realised_il_usd: float | None  # IL vs holding the action makes permanent (<0 = loss)
    il_charged_usd: float  # the part of it scored as a cost (0 unless configured)
    depth_change: float  # effect on the pool's active liquidity, fraction
    order: int  # 1 = first in its pool; later tickets assume earlier ones done
    horizon_days: float
    fee_rate_per_day: float  # the pool's fees/day the scores are built on
    tier: str = "soon"  # 'now': positive across σ's band, costs known · 'soon': not robust
    notes: list[str] = field(default_factory=list)
    base_is_token1: bool = False  # quote bands as token1's price (token0 is a stablecoin)
    token_id: int | None = None  # the position NFT the multisig acts on, when known
    symbols: tuple[str, str] = ("token0", "token1")
    prices_usd: tuple[float, float] = (0.0, 0.0)  # today's, implied by the snapshot's value
    book_share: float | None = None  # the book's share of the pool's active liquidity
    at_edge: bool = False  # the band sits at the edge of the search: a direction
    depth_extrapolated: bool = False  # a close past depth_bounds: ε > 0 figures extrapolate
    # USD liquidity within ±2% of spot across the whole pool today, with the
    # active liquidity held flat over that ±2% (other LPs' ticks are not read).
    pool_depth_2pct_usd: float | None = None
    volume_band: tuple[float, float] = (0.0, 0.0)  # gain at the ε bracket's ends, central σ
    elasticity: float = 0.0  # the central ε the ticket is scored at
    elasticity_band: tuple[float, float] = (0.0, 1.0)  # the ε bracket volume_band is read at

    @property
    def score_usd(self) -> float:
        """Gain less every cost that is known."""
        return self.gain_usd - (self.swap_cost_usd or 0.0) - self.il_charged_usd

    @property
    def breakeven_swap_pct(self) -> float | None:
        """The swap cost, as % of notional, at which the ticket stops paying."""
        if self.swap_usd < 1:
            return None
        return (self.gain_usd - self.il_charged_usd) / self.swap_usd * 100

    def queue_item(self) -> dict:
        """The shape Storage.reconcile_rebalance_queue takes."""
        return {"uid": self.uid, "pair": self.pair, "tier": self.tier, "context": self.context()}

    def context(self) -> dict:
        """What the queue table keeps for display between runs."""
        return {
            "action": self.action,
            "score_usd": self.score_usd,
            "gain_band": list(self.gain_band),
            "new_ticks": list(self.new_ticks) if self.new_ticks else None,
            "swap_usd": self.swap_usd,
            "value_usd": self.value_usd,
            "horizon_days": self.horizon_days,
        }


@dataclass
class ActionQueue:
    tickets: list[Ticket]
    plans: list[PoolPlan]
    unscored: list[tuple[dict, str]]  # (snapshot row, why it has no score)
    horizon_days: float


def _band_ends_pct(ticks: tuple[int, int], cur: int, invert: bool) -> tuple[float, float]:
    """A band as (−x%, +y%) around today's price — in token1's price when
    `invert` (token0 is a stablecoin, so the pool price reads upside down)."""
    lo, hi = (cur - ticks[1], cur - ticks[0]) if invert else (ticks[0] - cur, ticks[1] - cur)
    return (pow(1.0001, lo) - 1) * 100, (pow(1.0001, hi) - 1) * 100


def _amounts(liq: float, sqrt_p: float, s_lo: float, s_hi: float) -> tuple[float, float]:
    """Token amounts (human) behind `liq` in [lo, hi] at √P."""
    sp = min(max(sqrt_p, s_lo), s_hi)
    return liq * (1 / sp - 1 / s_hi), liq * (sp - s_lo)


@dataclass
class _Slot:
    """A member's position in the pool as the greedy pass sees it."""

    member: PoolMember
    liquidity: float
    ticks: tuple[int, int]
    done: bool = False

    def active(self, cur: int) -> float:
        return self.liquidity if self.ticks[0] <= cur <= self.ticks[1] else 0.0


def _slot_uid(s: _Slot) -> str:
    r = s.member.row
    return f"{r['wallet']}:{r['chain']}:{r['protocol_id']}:{r['position_key']}"


def action_queue(plans: list[PoolPlan], config: RebalanceConfig | None = None) -> ActionQueue:
    """Candidate actions for every position the optimiser could price,
    scored in dollars over the horizon; the best per position that clears
    config.min_ev_usd becomes a ticket. Sorted: robust ('now') first, then
    by score."""
    config = config or DEFAULT_REBALANCE_CONFIG
    horizon = float(config.target_horizon_days)
    tickets: list[Ticket] = []
    unscored: list[tuple[dict, str]] = []
    for plan in plans:
        ps = plan.state
        for r in ps.unmodelled:
            unscored.append((r, "the risk model cannot read it (ticks or decimals unknown)"))
        # Only missing INPUTS keep a pool out. A pool whose every re-range
        # would move its depth too far can still be scored for a close.
        reason = ps.unknown_reason(config.min_sigma_obs)
        if reason is not None:
            unscored.extend((m.row, reason) for m in ps.members)
            continue
        found = _pool_tickets(ps, config, horizon, unscored)
        tickets.extend(found)
        if plan.reason is not None and not found:
            listed = {id(r) for r, _ in unscored}
            unscored.extend((m.row, plan.reason) for m in ps.members if id(m.row) not in listed)
    tickets.sort(key=lambda t: (t.tier != "now", -t.score_usd))
    return ActionQueue(tickets, plans, unscored, horizon)


def _pool_tickets(
    ps: PoolState, config: RebalanceConfig, horizon: float, unscored: list[tuple[dict, str]]
) -> list[Ticket]:
    assert ps.sigma is not None and ps.fees_per_day is not None and ps.tick_current is not None
    l_ext = ps.l_ext or 0.0
    sigmas = (ps.sigma, *sigma_band(ps.sigma, ps.n_obs))
    slots = [_Slot(m, m.model.liquidity, (m.model.ticks[0], m.model.ticks[2])) for m in ps.members]  # type: ignore[index]
    out: list[Ticket] = []
    order = 0
    while True:
        best: Ticket | None = None
        for slot in slots:
            if slot.done:
                continue
            if float(slot.member.model.value_usd) < float(config.idle_min_usd):
                continue
            t = _best_ticket(ps, slot, slots, l_ext, sigmas, config, horizon)
            if t is not None and (best is None or t.score_usd > best.score_usd):
                best = t
        if best is None or best.score_usd <= config.min_ev_usd:
            break
        order += 1
        best.order = order
        out.append(best)
        slot = next(s for s in slots if _slot_uid(s) == best.uid)
        slot.done = True
        if best.after is None:
            slot.liquidity = 0.0
        elif best.new_ticks is not None:
            s_lo, s_hi = (
                _sqrt_at(best.new_ticks[0], ps.scale),
                _sqrt_at(best.new_ticks[1], ps.scale),
            )
            slot.liquidity = best.capital_after_usd * liquidity_per_usd(
                ps.sqrt_p, s_lo, s_hi, ps.p1
            )
            slot.ticks = best.new_ticks
        else:  # harvest: more liquidity in the same band
            m = slot.member.model
            slot.liquidity += (best.capital_after_usd - m.value_usd) * m.liquidity / m.value_usd
    small = [
        s.member.row
        for s in slots
        if not s.done and float(s.member.model.value_usd) < float(config.idle_min_usd)
    ]
    unscored.extend((r, f"below the ${float(config.idle_min_usd):,.0f} floor") for r in small)
    return out


def _best_ticket(
    ps: PoolState,
    slot: _Slot,
    slots: list[_Slot],
    l_ext: float,
    sigmas: tuple[float, float, float],
    config: RebalanceConfig,
    horizon: float,
) -> Ticket | None:
    """The highest-scoring candidate for one slot, against the pool as the
    greedy pass has left it."""
    assert ps.fees_per_day is not None and ps.tick_current is not None
    cur, F = ps.tick_current, ps.fees_per_day
    m = slot.member.model
    row = slot.member.row
    l_own = sum(s.active(cur) for s in slots if s is not slot)
    # Depth is bounded against the pool as it is TODAY, not as earlier
    # tickets leave it — otherwise the ±bound compounds down a pool's queue.
    depth_now = ps.l_active or (l_ext + l_own + slot.active(cur))
    bal = json.loads(row.get("current_balances_json") or "[]")
    syms = (str(bal[0].get("symbol") or "token0"), str(bal[1].get("symbol") or "token1"))
    value = m.value_at(0.0)
    have0 = m.x * m.p0
    sp, p1 = m.sqrt_p, m.p1

    eps = config.volume_elasticity
    # (σ, ε) pairs every candidate is scored at: σ's band at the central ε,
    # then the ε bracket's ends at the central σ.
    grid = [(sg, eps) for sg in sigmas] + [(sigmas[0], e) for e in config.elasticity_band]

    def ev_at(liq: float, ticks: tuple[int, int], sigma: float, e: float) -> BandEV:
        return band_ev(
            liq, sp, p1, (ticks[0] - cur) * _LN_TICK, (ticks[1] - cur) * _LN_TICK,
            F, l_ext, l_own, sigma, horizon, elasticity=e, depth_ref=depth_now,
        )  # fmt: skip

    stays = [ev_at(slot.liquidity, slot.ticks, sg, e) for sg, e in grid]

    def depth_change(liq_after: float, ticks: tuple[int, int] | None) -> float:
        active = liq_after if ticks and ticks[0] <= cur <= ticks[1] else 0.0
        return (l_ext + l_own + active) / depth_now - 1 if depth_now else 0.0

    realised = float(row["il_usd"]) if row.get("il_usd") is not None else None

    def make(
        action: str,
        ticks: tuple[int, int] | None,
        capital: float,
        liq: float,
        swap: tuple[float, tuple[str, str] | None],
        il_fraction: float,
    ) -> Ticket | None:
        dch = depth_change(liq, ticks)
        outside = not config.depth_bounds[0] <= 1 + dch <= config.depth_bounds[1]
        # A close is still scored past the bound: at ε = 0 it is exact (its
        # fees pass to whoever stays). Only the ε > 0 figures lean on depth
        # the response was not read over, so it is flagged and never 'now'.
        if outside and ticks is not None:
            return None
        if ticks is None:
            afters: list[BandEV | None] = [None] * len(grid)
            # Leaving the pool: its book fees go (under ε > 0 some of the
            # siblings' flow goes with its depth) and so does its IL.
            all_gains = [-e.ev_usd for e in stays]
        else:
            afters = [ev_at(liq, ticks, sg, e) for sg, e in grid]
            all_gains = [a.ev_usd - e.ev_usd for a, e in zip(afters, stays, strict=True)]  # type: ignore[union-attr]
        gains, vol_gains = all_gains[:3], all_gains[3:]
        notional, leg = swap
        cost = notional * config.swap_cost_pct / 100 if config.swap_cost_pct is not None else None
        made_permanent = realised * il_fraction if realised is not None and il_fraction else None
        charged = (
            -made_permanent
            if config.charge_realised_il and made_permanent is not None and made_permanent < 0
            else 0.0
        )
        deposit: list[tuple[str, float]] = []
        if ticks is not None and liq > 0:
            s_lo, s_hi = _sqrt_at(ticks[0], ps.scale), _sqrt_at(ticks[1], ps.scale)
            x, y = _amounts(liq, sp, s_lo, s_hi)
            deposit = [(syms[0], x), (syms[1], y)]
        t = Ticket(
            uid=f"{row['wallet']}:{row['chain']}:{row['protocol_id']}:{row['position_key']}",
            wallet=row["wallet"], chain=row["chain"], protocol_id=row["protocol_id"],
            position_key=row["position_key"], pair=row["pair_label"], action=action,
            value_usd=value, ticks_now=(slot.ticks[0], cur, slot.ticks[1]), new_ticks=ticks,
            withdraw=[(syms[0], m.x), (syms[1], m.y)] if action != "harvest" else [],
            deposit=deposit, capital_after_usd=capital, stay=stays[0], after=afters[0],
            gain_usd=gains[0], gain_band=(min(gains), max(gains)), swap_usd=notional,
            volume_band=(vol_gains[0], vol_gains[-1]), elasticity=eps,
            elasticity_band=(config.elasticity_band[0], config.elasticity_band[-1]),
            swap_leg=leg, swap_cost_usd=cost, realised_il_usd=made_permanent,
            il_charged_usd=charged, depth_change=dch, order=0, horizon_days=horizon,
            fee_rate_per_day=F,
            base_is_token1=syms[0].upper() in _QUOTE_SYMBOLS,
            token_id=json.loads(row.get("meta_json") or "{}").get("token_id"),
            symbols=syms, prices_usd=(m.p0, m.p1), book_share=ps.book_share,
            pool_depth_2pct_usd=(
                ps.l_active * sp * p1 * _DEPTH_2PCT if ps.l_active else None
            ),
        )  # fmt: skip
        robust = min(gains + vol_gains) - (cost or 0.0) - charged > 0
        t.tier = "now" if robust and (cost is not None or notional < 1) and not outside else "soon"
        if outside:
            t.depth_extrapolated = True
            t.notes.append(
                f"closing takes the pool's depth to {1 + dch:.2g}× today's, outside the "
                f"{config.depth_bounds[0]:g}–{config.depth_bounds[1]:g}× the depth response is "
                "read over; its ε > 0 figures are extrapolation (at ε "
                f"{config.elasticity_band[0]:g}: {money(vol_gains[0], True)})"
            )
        return t

    def mix_swap(target0: float, target1: float) -> tuple[float, tuple[str, str] | None]:
        """Notional to move from today's token mix to the target's, the
        target scaled to the capital the position keeps; any capital
        added or removed moves in the band's own ratio."""
        keep = min(value, target0 + target1)
        if keep <= 0 or target0 + target1 <= 0:
            return 0.0, None
        want0 = keep * target0 / (target0 + target1)
        short0 = want0 - have0 * keep / value
        if abs(short0) < 1e-9:
            return 0.0, None
        return abs(short0), ((syms[1], syms[0]) if short0 > 0 else (syms[0], syms[1]))

    cands: list[Ticket] = []
    view = PoolView(
        capital_usd=value, tick_current=cur, sqrt_p=sp, scale=ps.scale, p1=p1,
        fees_per_day=F, l_ext=l_ext, l_own=l_own, l_active=depth_now, sigma=sigmas[0],
        horizon=horizon, spacing=ps.spacing,
        current=((slot.liquidity, slot.ticks),), returns=ps.returns, n_obs=ps.n_obs,
    )  # fmt: skip
    choice = strategy_from_spec(config.strategy).band(view, config)
    if choice is not None and choice.ticks != slot.ticks:
        s_lo, s_hi = _sqrt_at(choice.ticks[0], ps.scale), _sqrt_at(choice.ticks[1], ps.scale)
        x, y = _amounts(choice.liquidity, sp, s_lo, s_hi)
        t = make("rerange", choice.ticks, value, choice.liquidity, mix_swap(x * m.p0, y * p1), 1.0)
        if t is not None:
            if choice.at_edge or choice.at_bound:
                t.at_edge = True
                t.notes.append(
                    "the band is at the edge of the search: a direction, not a destination"
                )
            cands.append(t)
    band = choice.ticks if choice is not None else slot.ticks
    s_lo, s_hi = _sqrt_at(band[0], ps.scale), _sqrt_at(band[1], ps.scale)
    per_usd = liquidity_per_usd(sp, s_lo, s_hi, p1)
    if per_usd > 0 and band[0] <= cur <= band[1]:
        # Size inside the depth bounds, where the depth^ε response is read.
        lo_l = max(0.0, config.depth_bounds[0] * depth_now - l_ext - l_own)
        hi_l = config.depth_bounds[1] * depth_now - l_ext - l_own
        liq = best_liquidity(lambda x: ev_at(x, band, sigmas[0], eps).ev_usd, lo_l, hi_l)
        capital = liq / per_usd
        # A size on either depth limit is where the search stopped, not
        # where EV peaked: the optimum may lie beyond it.
        tol = 1e-6 * max(hi_l - lo_l, 1e-12)
        pinned = (lo_l > 0 and liq <= lo_l + tol) or liq >= hi_l - tol
        # A size that keeps under 1% of the position is a close, and the close
        # candidate scores it as one — never a 'resize' that deposits dust.
        if capital >= 0.01 * value and abs(capital - value) > 0.01 * value:
            x, y = _amounts(liq, sp, s_lo, s_hi)
            cut = max(0.0, (value - capital) / value)
            moved = 1.0 if band != slot.ticks else cut  # a new band withdraws it all
            t = make("resize", band, capital, liq, mix_swap(x * m.p0, y * p1), moved)
            if t is not None:
                t.at_edge = pinned or (
                    choice is not None
                    and band == choice.ticks
                    and (choice.at_edge or choice.at_bound)
                )
                if pinned:
                    t.notes.append(
                        f"sized to the depth limit (pool depth {1 + t.depth_change:.2g}× today's, "
                        f"bound {config.depth_bounds[0]:g}–{config.depth_bounds[1]:g}×): a "
                        "direction, not a destination"
                    )
                if capital > value:
                    t.notes.append(
                        f"adds ${capital - value:,.0f} from treasury, which this tool cannot see "
                        "(custody is Phase 3)"
                    )
                cands.append(t)
    close = make("close", None, 0.0, 0.0, (0.0, None), 1.0)
    if close is not None:
        cands.append(close)
    unclaimed = row.get("unclaimed_usd")
    if (
        row.get("unclaimed_is_known", 1)
        and unclaimed
        and float(unclaimed) > 0
        and slot.ticks[0] <= cur <= slot.ticks[1]
        and m.value_usd > 0
    ):
        add = float(unclaimed)
        liq = slot.liquidity + add * m.liquidity / m.value_usd
        t = make("harvest", slot.ticks, value + add, liq, (0.0, None), 0.0)
        if t is not None:
            t.new_ticks = None  # same band
            x, y = _amounts(liq - slot.liquidity, sp, *(_sqrt_at(k, ps.scale) for k in slot.ticks))
            t.deposit = [(syms[0], x), (syms[1], y)]
            t.notes.append("collected fees arrive in the mix they were earned; any swap is small")
            cands.append(t)
    for c in cands:
        # A band or size at the model's limit is a direction, not a
        # destination — like a close past the depth bound, never 'now' (which
        # pushes a REBALANCE alert): the optimum may lie beyond what was read.
        if c.at_edge and c.tier == "now":
            c.tier = "soon"
    return max(cands, key=lambda c: c.score_usd) if cands else None


def _pct_s(v: float) -> str:
    """±x% with enough digits that a narrow band does not read '−0%'."""
    return f"{v:+.1f}%" if abs(v) < 10 else f"{v:+.0f}%"


def ticket_why(t: Ticket, short: bool = False) -> str:
    """Why the ticket exists, in words: where the position stands and what
    the model expects it to earn and lose if left alone."""
    lo, cur, hi = t.ticks_now
    inside = lo <= cur <= hi
    fees, il, H = t.stay.fees_usd, t.stay.il_usd, t.horizon_days
    where = "in range" if inside else "out of range"
    if short:
        if t.action == "close" and inside:
            return f"{where}, fees {money(fees)} < E[IL] {money(il)}"
        return f"{where}, fees {money(fees)} vs E[IL] {money(il)}"
    stand = (
        f"In range, {t.stay.occupancy_days:.0f} of the next {H:.0f} days expected in range"
        if inside
        else f"Out of range — {t.stay.occupancy_days:.0f} of the next {H:.0f} days expected back in it"
    )
    earn = f"left alone it is expected to earn {money(fees)} in fees and lose {money(il)} to IL"
    if t.action == "close":
        verdict = (
            "the IL outweighs the fees, so holding the tokens outright is expected to do better"
            if il > fees
            else "its fees mostly come from the book's own depth, so they are not lost by closing"
        )
    elif t.action == "harvest":
        verdict = "compounding the unclaimed fees earns on them too"
    else:
        a = t.after
        verdict = (
            f"in the new band: {money(a.fees_usd)} fees, {money(a.il_usd)} IL"
            if a is not None
            else ""
        )
    return f"{stand}; {earn} — {verdict}" if verdict else f"{stand}; {earn}"


def ticket_checks(t: Ticket) -> list[str]:
    """What to settle before acting on the ticket — each item from the
    ticket's own figures, in the order worth doing them."""
    out: list[str] = []
    if t.realised_il_usd is not None and t.realised_il_usd < 0 and not t.il_charged_usd:
        pct = -t.realised_il_usd / t.value_usd * 100 if t.value_usd else 0.0
        if pct >= 5:
            out.append(
                f"accept locking in {money(t.realised_il_usd)} of IL vs holding ({pct:.0f}% of the "
                "position) — the score does not charge it"
            )
    if t.swap_leg and t.swap_usd >= 1 and t.swap_cost_usd is None:
        be = t.breakeven_swap_pct
        out.append(
            f"quote the {money(t.swap_usd)} {t.swap_leg[0]}→{t.swap_leg[1]} swap"
            + (f" — it pays while the cost is under {be:.1f}%" if be and be > 0 else "")
        )
    if t.gain_band[0] <= 0 or min(t.volume_band) <= 0:
        out.append("wait for σ history to firm up — the σ or volume band includes a loss")
    if t.at_edge or t.depth_extrapolated:
        out.append("treat the size or band as a direction: it sits at the model's limit")
    if t.capital_after_usd > t.value_usd * 1.01:
        out.append(f"find {money(t.capital_after_usd - t.value_usd)} in treasury to add")
    out.append("re-check the next run's ticket: it is re-scored twice a day")
    return out


def describe_ticket(t: Ticket) -> tuple[str, list[str], list[str]]:
    """(headline, steps, case) for one ticket: what to execute, and the
    dollar case with its arithmetic — the same words on the page, in
    Slack and in the CLI, so no two surfaces phrase one proposal twice."""

    def usd(v: float, sign: bool = False) -> str:
        s = f"{abs(v):,.0f}"
        return (("+" if v >= 0 else "−") if sign else ("−" if v < 0 else "")) + "$" + s

    def amt(v: float) -> str:
        return f"{v:,.4g}" if abs(v) < 1000 else f"{v:,.0f}"

    base = t.pair.split(" ")[0].split("/")
    quote_note = f" ({base[1]} price in {base[0]})" if t.base_is_token1 and len(base) == 2 else ""
    title = _ACTION_TITLES[t.action]
    if t.new_ticks:
        lo_pct, hi_pct = _band_ends_pct(t.new_ticks, t.ticks_now[1], t.base_is_token1)
        title += f" to {_pct_s(lo_pct)} / {_pct_s(hi_pct)}{quote_note}"
    head = f"{title} · {t.pair} · {usd(t.score_usd, True)} over {t.horizon_days:.0f}d"
    steps: list[str] = []
    lo, cur, hi = t.ticks_now
    ident = f"NFT #{t.token_id}" if t.token_id is not None else t.position_key
    now_lo, now_hi = _band_ends_pct((lo, hi), cur, t.base_is_token1)
    where = "in range" if lo <= cur <= hi else "out of range"
    steps.append(
        f"Position {ident} on {t.chain} ({t.protocol_id}), {usd(t.value_usd)}, "
        f"ticks {lo}…{hi} ({_pct_s(now_lo)} / {_pct_s(now_hi)}{quote_note.replace(' (', '; ').rstrip(')')}), {where}"
    )
    if t.withdraw:
        out = [f"{amt(a)} {s}" for s, a in t.withdraw if a > 0]
        steps.append("Withdraw: " + ", ".join(out or ["the position"]) + " and collect fees")
    if t.swap_leg and t.swap_usd >= 1:
        steps.append(f"Swap: ~{usd(t.swap_usd)} of {t.swap_leg[0]} → {t.swap_leg[1]}")
    if t.deposit:
        where_to = f"ticks {t.new_ticks[0]}…{t.new_ticks[1]}" if t.new_ticks else "the same range"
        steps.append(
            f"Deposit into {where_to}: " + ", ".join(f"{amt(a)} {s}" for s, a in t.deposit if a > 0)
            + f" ({usd(t.capital_after_usd)})"
        )  # fmt: skip
    elif t.action == "close":
        steps.append("Proceeds to treasury — nothing redeposited")

    def ev_line(label: str, e: BandEV) -> str:
        own = (
            f", {usd(e.own_fees_usd)} of it from the book's own positions"
            if e.own_fees_usd >= 1
            else ""
        )
        return (
            f"{label}: fees {usd(e.fees_usd)} ({e.share:.0%} of ${t.fee_rate_per_day:,.0f}/day × "
            f"{e.occupancy_days:.1f} of {t.horizon_days:.0f} days in range{own}) − E[IL] "
            f"{usd(e.il_usd)} = {usd(e.ev_usd, True)}"
        )

    case: list[str] = [f"Why: {ticket_why(t)}"]
    case.append(ev_line("Stay", t.stay))
    if t.after is not None:
        case.append(ev_line("After", t.after))
    else:
        case.append("After: nothing in the pool = $0")
    gain = (
        f"Gain {usd(t.gain_usd, True)} at ε {t.elasticity:.2g} (σ band {usd(t.gain_band[0], True)}"
        f" … {usd(t.gain_band[1], True)}; ε {t.elasticity_band[0]:.2g} / {t.elasticity_band[1]:.2g}: "
        f"{usd(t.volume_band[0], True)} / "
        f"{usd(t.volume_band[1], True)})"
    )
    if t.swap_usd >= 1 and t.swap_cost_usd is None:
        be = t.breakeven_swap_pct
        gain += (
            f"; swap cost not given — pays while the swap costs under {be:.1f}% of its notional"
            if be and be > 0
            else "; no swap cost it could pay"
        )
    elif t.swap_cost_usd:
        gain += f" − swap {usd(t.swap_cost_usd)}"
    if t.il_charged_usd:
        gain += f" − realised IL {usd(t.il_charged_usd)}"
    case.append(gain + f" → {usd(t.score_usd, True)}")
    if t.realised_il_usd is not None and t.realised_il_usd < 0 and not t.il_charged_usd:
        case.append(
            f"Makes {usd(t.realised_il_usd)} of IL vs holding permanent — shown, not charged: "
            "with no view on price, a return to the old range is worth nothing in expectation"
        )
    if t.order > 1:
        before = "step 1 is" if t.order == 2 else f"steps 1–{t.order - 1} are"
        case.append(f"Step {t.order} in this pool — scored as if its {before} done")
    case.extend(n[0].upper() + n[1:] for n in t.notes)
    case.append("Before acting: " + "; ".join(ticket_checks(t)))
    return head, steps, case


# ── The ticket as a desk reads it ─────────────────────────────────────────
#
# describe_ticket is the model's full working, for checking. This is the
# same ticket as a blotter line: action and position, EV over the horizon,
# then labelled rows — deploy range (price and ticks), current range, size,
# EV with its σ band, the fee / IL / time-in-range drivers, swap, legs, and
# the assumptions the number leans on. No prose. Every figure is the
# ticket's own; nothing here computes a new number.

GROUP_ORDER = ("do", "quote", "borderline")
GROUP_TITLES = {
    "do": (
        "Robust on the model",
        "positive across the σ and volume bands; all costs known — see the verdict above",
    ),
    "quote": ("Pending swap quote", "EV before swap cost; break-even shown"),
    "borderline": ("Marginal", "σ or volume band includes a loss"),
}
_VENUES = {"peaq": "MachineX", "bsc": "PancakeSwap", "base": "Uniswap V3"}


def money(v: float, sign: bool = False) -> str:
    """$1,234 · $12.3k · $1.23M."""
    a = abs(v)
    s = f"${a / 1e6:,.2f}M" if a >= 1e6 else f"${a / 1e3:,.1f}k" if a >= 1e4 else f"${a:,.0f}"
    if sign:
        return ("+" if v >= 0 else "−") + s
    return ("−" if v < 0 else "") + s


def _price(v: float) -> str:
    """A token price to two significant figures: $0.0046, $0.042, $1.23, $2,450."""
    if v <= 0:
        return "$0"
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    v = float(f"{v:.2g}")  # rounded first, so 0.000999 → 0.0010
    digits = max(2, 1 - math.floor(math.log10(v)))
    return f"${v:.{digits}f}"


def _amount(v: float) -> str:
    return f"{v:,.4g}" if abs(v) < 1000 else f"{v:,.0f}"


def _action(t: Ticket) -> str:
    if t.action == "close":
        return "CLOSE"
    if t.action == "harvest":
        return "HARVEST + COMPOUND"
    lo, _, hi = t.ticks_now
    old, new = hi - lo, (t.new_ticks[1] - t.new_ticks[0]) if t.new_ticks else hi - lo
    verb = "WIDEN" if new > old * 1.05 else "NARROW" if new < old * 0.95 else "RE-CENTER"
    delta = t.capital_after_usd - t.value_usd
    if t.action == "resize" and abs(delta) > 0.01 * t.value_usd:
        verb += f" + {'ADD' if delta > 0 else 'CUT'} {money(abs(delta))}"
    return verb


def _base(t: Ticket) -> tuple[int, str, str | None]:
    """(index, symbol, non-stable quote symbol or None) of the token the
    range is quoted in — the non-stable side."""
    i = 1 if t.symbols[0].upper() in _QUOTE_SYMBOLS else 0
    other = t.symbols[1 - i]
    return i, t.symbols[i], None if other.upper() in _QUOTE_SYMBOLS else other


def _band_prices(t: Ticket, ticks: tuple[int, int]) -> tuple[float, float]:
    """The base token's USD price at the band's edges, the other token's
    USD price held at spot — the move a range actually sees."""
    i, _, _ = _base(t)
    cur = t.ticks_now[1]
    sign = 1 if i == 0 else -1  # the pool price is token0 in token1
    a, b = (t.prices_usd[i] * pow(1.0001, sign * (k - cur)) for k in ticks)
    return (min(a, b), max(a, b))


_FULL_RANGE_TICK = 887_000  # V3-family full range sits at ±887272 snapped to the spacing
_WIDE_PCT = 99_999.0  # past this, a percentage stops being readable


def is_full_range(ticks: tuple[int, int]) -> bool:
    return ticks[0] <= -_FULL_RANGE_TICK and ticks[1] >= _FULL_RANGE_TICK


def band_pct_label(lo_pct: float, hi_pct: float, ticks: tuple[int, int] | None = None) -> str:
    """'−11% / +13%' from spot; 'full range' for a band at the tick bounds —
    1.0001^887000 is not a percentage anyone can read."""
    if ticks is not None and is_full_range(ticks):
        return "full range"
    hi = f">+{_WIDE_PCT:,.0f}%" if hi_pct > _WIDE_PCT else f"{hi_pct:+.0f}%"
    return f"{lo_pct:+.0f}% / {hi}"


def _signed_pct(v: float) -> str:
    """−8.1% · +783% — one decimal under 10%, typographic minus."""
    text = f"{abs(v):.1f}%" if abs(v) < 10 else f"{abs(v):,.0f}%"
    return ("+" if v >= 0 else "−") + text


def _width(t: Ticket, ticks: tuple[int, int]) -> str | None:
    """A band as '−89% / +783%' from spot."""
    i, _, _ = _base(t)
    if is_full_range(ticks):
        return "full range"
    lo, hi = _band_prices(t, ticks)
    spot = t.prices_usd[i]
    if spot <= 0:
        return None
    if (hi / spot - 1) * 100 > _WIDE_PCT:
        return f"{_signed_pct((lo / spot - 1) * 100)} / >+{_WIDE_PCT:,.0f}%"
    return f"{_signed_pct((lo / spot - 1) * 100)} / {_signed_pct((hi / spot - 1) * 100)}"


def _range(t: Ticket, ticks: tuple[int, int]) -> str:
    """'$0.0046 – $0.36  ·  −89% / +783%  ·  ticks −330100 / −286500'."""
    lo, hi = _band_prices(t, ticks)
    w = _width(t, ticks)
    width = f"  ·  {w}" if w else ""
    return f"{_price(lo)} – {_price(hi)}{width}  ·  ticks {ticks[0]} / {ticks[1]}"


def _pct_in(e: BandEV, horizon: float) -> str:
    return f"{e.occupancy_days / horizon:.0%}" if horizon else "—"


def _conviction(t: Ticket) -> tuple[str, list[str]]:
    """('high'|'med'|'low', the assumptions the EV leans on)."""
    risks: list[str] = []
    costs = (t.swap_cost_usd or 0.0) + t.il_charged_usd
    worst_sigma = t.gain_band[0] - costs
    worst_volume = min(t.volume_band) - costs
    if worst_sigma <= 0:
        risks.append(f"σ band includes a loss ({money(worst_sigma, True)})")
    if worst_volume <= 0:
        risks.append(f"volume band includes a loss ({money(worst_volume, True)})")
    if abs(t.depth_change) >= 0.05:
        risks.append(
            f"fees ∝ depth^{t.elasticity:.2g}; pool depth {_signed_pct(t.depth_change * 100)}"
        )
    if t.at_edge:
        risks.append("band at search limit")
    if t.depth_extrapolated:
        risks.append("depth past the modelled bound")
    if t.book_share is not None and t.book_share > 0.9:
        risks.append(f"book {t.book_share:.0%} of pool liquidity")
    edge = t.at_edge or t.depth_extrapolated
    level = "low" if min(worst_sigma, worst_volume) <= 0 else "med" if edge else "high"
    return level, risks


@dataclass(frozen=True)
class TicketView:
    """One ticket as a blotter entry. `rows` are (label, value)."""

    action: str  # 'WIDEN + CUT $1.8k'
    ref: str  # '#12345' or the position key
    pair: str
    venue: str
    ev: str  # e.g. '+$1,234'
    ev_pct: str  # '+2.5%'
    conviction: str  # 'high' | 'med' | 'low'
    width: str | None  # the deploy band vs spot, '−89% / +783%'; None when nothing is deployed
    rows: list[tuple[str, str]]
    group: str
    details: list[str]  # describe_ticket: execution steps
    math: list[str]  # describe_ticket: the arithmetic
    ticket: Ticket

    @property
    def line(self) -> str:
        """'WIDEN + CUT $1.8k · WPEAQ/USDC 1.00% #12345' — the blotter headline."""
        return f"{self.action} · {self.pair} {self.ref}"


def ticket_view(t: Ticket) -> TicketView:
    _, details, math_lines = describe_ticket(t)
    H = t.horizon_days
    i, sym, quote = _base(t)
    spot = f"{sym} {_price(t.prices_usd[i])}" + (f" (vs {quote} at spot)" if quote else "")
    lo, cur, hi = t.ticks_now
    rows: list[tuple[str, str]] = [("Why", ticket_why(t))]
    if t.new_ticks:
        rows.append(("Deploy", _range(t, t.new_ticks)))
    elif t.action == "close":
        rows.append(("Deploy", "none — proceeds to treasury"))
    rows.append(("Current", _range(t, (lo, hi)) + ("" if lo <= cur <= hi else "  ·  out of range")))
    rows.append(("Spot", spot))
    size = money(t.value_usd)
    if t.after is not None and abs(t.capital_after_usd - t.value_usd) > 0.01 * t.value_usd:
        size += f" → {money(t.capital_after_usd)}"
    elif t.after is None:
        size += " → 0"
    rows.append(("Size", size))
    if t.pool_depth_2pct_usd is not None:
        after = t.pool_depth_2pct_usd * (1 + t.depth_change)
        rows.append(
            (
                "Depth ±2%",
                f"{money(t.pool_depth_2pct_usd)} → {money(after)} "
                f"({_signed_pct(t.depth_change * 100)})  ·  pool, at spot",
            )
        )
    rows.append(("σ band", f"{money(t.gain_band[0], True)} / {money(t.gain_band[1], True)}"))
    e0, e1 = t.volume_band
    lo_e, hi_e = t.elasticity_band
    rows.append(
        ("Volume band", f"{money(e0, True)} / {money(e1, True)}  ·  ε {lo_e:.2g} / {hi_e:.2g}")
    )
    s, a = t.stay, t.after
    if a is not None:
        rows.append(
            (
                "Drivers",
                f"fees {money(s.fees_usd)} → {money(a.fees_usd)}  ·  E[IL] {money(s.il_usd)} → "
                f"{money(a.il_usd)}  ·  in range {_pct_in(s, H)} → {_pct_in(a, H)}",
            )
        )
    else:
        own = (
            f"  ·  {s.own_fees_usd / s.fees_usd:.0%} of fees from own positions"
            if s.fees_usd >= 1 and s.own_fees_usd >= 1
            else ""
        )
        rows.append(
            (
                "Drivers",
                f"fees {money(s.fees_usd)} vs E[IL] {money(s.il_usd)}  ·  in range "
                f"{_pct_in(s, H)}{own}",
            )
        )
    if t.withdraw:
        rows.append(("Withdraw", " + ".join(f"{_amount(x)} {k}" for k, x in t.withdraw if x > 0)))
    if t.swap_leg and t.swap_usd >= 1:
        leg = f"{money(t.swap_usd)} {t.swap_leg[0]} → {t.swap_leg[1]}"
        if t.swap_cost_usd is not None:
            rows.append(("Swap", f"{leg}  ·  cost {money(t.swap_cost_usd)}"))
        else:
            be = t.breakeven_swap_pct
            rows.append(
                (
                    "Swap",
                    f"{leg}  ·  break-even {'>100' if be and be >= 100 else f'{be:.1f}' if be else '—'}%",
                )
            )
    if t.deposit:
        rows.append(("Deposit", " + ".join(f"{_amount(x)} {k}" for k, x in t.deposit if x > 0)))
    if t.realised_il_usd is not None and t.realised_il_usd < 0 and t.action != "harvest":
        charged = "charged" if t.il_charged_usd else "not charged"
        rows.append(("Crystallises", f"{money(t.realised_il_usd)} IL vs HODL ({charged})"))
    level, risks = _conviction(t)
    if risks:
        rows.append(("Assumes", "  ·  ".join(risks)))
    if t.order > 1:
        rows.append(
            (
                "Sequence",
                f"leg {t.order}; scored after legs 1–{t.order - 1}"
                if t.order > 2
                else "leg 2; scored after leg 1",
            )
        )
    rows.append(("Before acting", "  ·  ".join(ticket_checks(t))))
    unknown_cost = bool(t.swap_leg and t.swap_usd >= 1 and t.swap_cost_usd is None)
    group = "borderline" if level == "low" else "quote" if unknown_cost else "do"
    pct = t.score_usd / t.value_usd * 100 if t.value_usd else 0.0
    return TicketView(
        action=_action(t),
        ref=f"#{t.token_id}" if t.token_id is not None else t.position_key[:12],
        pair=t.pair,
        venue=_VENUES.get(t.chain, t.chain),
        ev=money(t.score_usd, True),
        ev_pct=f"{pct:+.1f}%",
        conviction=level,
        width=_width(t, t.new_ticks) if t.new_ticks else None,
        rows=rows,
        group=group,
        details=details,
        math=math_lines,
        ticket=t,
    )


@dataclass(frozen=True)
class PlanCard:
    """Every leg for one pool, in execution order. The card sits in its
    FIRST leg's group: leg 1 can be executed on its own, and a later leg
    that waits on a quote must not bury it."""

    pair: str
    venue: str
    steps: list[TicketView]
    group: str
    total_usd: float
    horizon_days: float


def plan_cards(
    queue: ActionQueue, hidden: set[str] | frozenset[str] = frozenset()
) -> list[PlanCard]:
    """One card per pool with tickets, grouped by the first leg, largest
    first. `hidden`: ticket uids to leave out (snoozed)."""
    by_pool: dict[tuple[str, str, str], list[TicketView]] = defaultdict(list)
    for t in queue.tickets:
        if t.uid not in hidden:
            by_pool[(t.chain, t.protocol_id, t.pair)].append(ticket_view(t))
    cards = []
    for (chain, _, pair), views in by_pool.items():
        views.sort(key=lambda v: v.ticket.order)
        cards.append(
            PlanCard(pair, _VENUES.get(chain, chain), views, views[0].group,
                     sum(v.ticket.score_usd for v in views), queue.horizon_days)
        )  # fmt: skip
    cards.sort(key=lambda c: (GROUP_ORDER.index(c.group), -c.total_usd))
    return cards


def peaq_scenarios(
    rows: list[dict], decimals: dict[str, int], shocks: tuple[float, ...] = SHOCKS
) -> dict:
    """Book value under a PEAQ move of s, every other token's USD price held.

    PEAQ as token0 → the pool price moves by s. PEAQ as token1 → token1's USD
    price moves by s and the pool price by 1/(1+s) − 1. A pair without PEAQ
    is untouched. Rows without a risk model are counted, not guessed."""
    changes = dict.fromkeys(shocks, 0.0)
    modelled = unmodelled = 0
    for row in rows:
        bal = json.loads(row.get("current_balances_json") or "[]")
        syms = [str(b.get("symbol") or "").upper() for b in bal]
        if not any(sym in PEAQ_EXPOSURE_SYMBOLS for sym in syms):
            continue
        m = risk_model(row, decimals)
        if m is None:
            unmodelled += 1
            continue
        modelled += 1
        both = all(sym in PEAQ_EXPOSURE_SYMBOLS for sym in syms)
        for s in shocks:
            if both:
                changes[s] += m.value_usd * s  # WPEAQ/wstPEAQ: the whole pair moves together
            elif syms[0] in PEAQ_EXPOSURE_SYMBOLS:
                changes[s] += m.value_at(s) - m.value_usd
            else:
                r = 1 / (1 + s) - 1
                changes[s] += m.value_at(r) * (1 + s) - m.value_usd
    return {"changes": changes, "positions": modelled, "unmodelled": unmodelled}


# ── Mandate scorecard: the proposal's success metrics, measured ───────────
#
# The proposal ("Treasury & DeFi Desk", Success metrics) sets six targets
# for end 2026. This reads each one against what the book can show
# today, and says plainly where it cannot: CEX spread and depth, the treasury
# book's trades and the launch book are outside what this tool sees, and a
# status says so rather than leaving a blank that reads as "fine".
#
# Status vocabulary, shared by every surface:
#   measured      a figure from the book
#   needs_input   measurable here once the operator keys something
#   not_here      measured elsewhere (MM KPI reports, custody), not this tool
#   not_yet       depends on history or a later phase

# Where the proposal and the hosted desk live is the operator's configuration,
# not code: empty (the default) means there is no link to follow.
PROPOSAL_URL = os.getenv("DESK_PROPOSAL_URL", "").strip()
DESK_URL = os.getenv("DESK_URL", "").strip()
FIRST_REPORT_DUE = "2026-10"  # "First due for Oct'26" (Live reporting)


def mandate_scorecard(
    periods: list,
    contracts: list[dict],
    incidents: list[dict],
    exceptions: list[dict],
    reports: list[dict],
    today: date,
) -> list[dict]:
    """[{metric, target, now, status, note}] in the proposal's order.

    `reports` are the monthly reports written, as [{month, final}]; the first
    one is due for October 2026 per the proposal's Live reporting."""
    rows: list[dict] = []

    # 1. Cost of liquidity — from the ledger's keyed annual costs.
    active = [c for c in contracts if c.get("status") == "active"]
    costed = [c for c in active if c.get("annual_cost_usd") is not None]
    if active and len(costed) == len(active):
        total = sum(float(c["annual_cost_usd"]) for c in costed)
        now, status = f"${total:,.0f}/yr across {len(costed)} contracts", "measured"
        note = "October baseline not yet recorded — the target compares against it"
    else:
        now, status = "—", "needs_input"
        note = (
            f"{len(active) - len(costed)} of {len(active)} contracts lack an annual cost"
            if active
            else "no MM contracts keyed — `defi-tracker ledger add … --annual-cost`"
        )
    rows.append({"metric": "Annual cost of liquidity (MM fees, option premia, incentives)",
                 "target": "Down __% vs. October baseline", "now": now, "status": status,
                 "note": note})  # fmt: skip

    # 2. Market quality — CEX order books; not this tool.
    rows.append({"metric": "Spread and ±2% depth, tier-1 venues",
                 "target": "Flat or better vs. current (re-define liquidity depth and spreads)",
                 "now": "—", "status": "not_here",
                 "note": "measured from CEX order books in the MM KPI reports; the only baseline "
                         "so far is an incumbent MM's self-reported ±2% depth"})  # fmt: skip

    # 3. Treasury book net return — the treasury book (stables, hedging, basis)
    #    is not visible here. What the desk does measure is the LIQUIDITY
    #    book against HODL; it is carried in the note, labelled as that book,
    #    so one is never read as the other.
    parts = []
    for p in periods:  # the windows the caller wants shown, in its order
        if not p.rows:
            continue
        t = p.totals()
        excess = t["il"] + t["fees_earned"] - (t["gas"] or 0)
        parts.append(f"{p.kind} {_usd_s(excess)}")
    liquidity = (
        "Liquidity book, excess vs HODL: " + " · ".join(parts)
        if parts
        else "Liquidity book vs HODL: not established yet"
    )
    rows.append({
        "metric": "Treasury book net return",
        "target": "__% vs. [benchmark – price of BTC?], max drawdown __%",
        "now": "—",
        "status": "not_here",
        "note": f"the treasury book's trades are not in this tool (the custody integration is "
                f"pending). {liquidity} — HODL of the deposited tokens",
    })  # fmt: skip

    # 4. Launch book — initiatives are not tagged here.
    rows.append({"metric": "Launch book", "target": "__ initiatives seeded, each with a defined exit",
                 "now": "—", "status": "not_yet",
                 "note": "launch positions are not tagged as a book yet"})  # fmt: skip

    # 5. Losses from counterparty or protocol failure — the incident log.
    open_inc = [i for i in incidents if not i.get("resolved_at")]
    high = [i for i in incidents if i.get("severity") in ("high", "critical")]
    rows.append({
        "metric": "Losses from counterparty or protocol failure",
        "target": "Zero",
        "now": f"{len(incidents)} incident(s) logged, {len(open_inc)} open, {len(high)} high or critical",
        "status": "measured",
        "note": "counts what is logged with `defi-tracker incident add`; a loss not logged is not seen",
    })  # fmt: skip

    # 6. Monthly report on time — from the reports written.
    finals = sorted(r["month"] for r in reports if r.get("final"))
    first_due = FIRST_REPORT_DUE
    now = f"{len(finals)} final report(s), latest {finals[-1]}" if finals else "none yet"
    due_passed = today.strftime("%Y-%m") > first_due
    rows.append({
        "metric": "Monthly desk P&L report",
        "target": "On time, every month",
        "now": now,
        "status": "measured" if finals or due_passed else "not_yet",
        "note": f"first due for {first_due} per the proposal; "
                f"{sum(1 for e in exceptions if not e.get('reviewed_at'))} exception(s) awaiting review",
    })  # fmt: skip
    return rows


# ── Volume elasticity, measured from swap history (Phase 5.3) ─────────────
#
# The optimiser prices a band with pool fees ∝ depth^ε. Here ε is estimated
# from each pool's own history (poolflow.py): per pool-week, log fees on log
# active depth, with pool and week fixed effects (the week absorbs whatever
# moves every peaq pool at once — PEAQ's price, the chain's activity) and
# log realised volatility as a control (a volatile week draws flow AND moves
# the book out of range).
#
# Plain OLS is biased: other LPs chase flow, so depth follows fees as much
# as fees follow depth. The instrument is the book's own depth at the start
# of the week — set by the book's earlier deposits, withdrawals and range
# choices, not by that week's flow. 2SLS on it is the ε the optimiser needs:
# how a pool's flow answers depth the BOOK adds or removes. Its interval is
# a bootstrap over weeks (each draw keeps every pool's row for that week, so
# common shocks stay together).
#
# Everything is priced from the pools themselves — stablecoins at $1, every
# other token through a pool that pairs it with a priced one, day by day —
# because the price cache holds a few dozen points per token, not a series.

_MIN_DAYS_PER_WEEK = 4
_MIN_ELASTICITY_OBS = 40
_MIN_FIRST_STAGE_F = 10.0


@dataclass
class ElasticityEstimate:
    """ε from swap history. Every None is unmeasured; `reason` says why the
    estimate cannot be used (None: it can)."""

    eps_ols: float | None
    ci_ols: tuple[float, float] | None
    eps_iv: float | None
    ci_iv: tuple[float, float] | None
    first_stage_f: float | None
    n_obs: int  # pool-weeks
    n_pools: int
    n_weeks: int
    book_moves: int  # pool-weeks where the book's own depth moved ≥ 20% on the week before
    from_day: str | None
    to_day: str | None
    reason: str | None = None
    pools: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.reason is None and self.eps_iv is not None and self.ci_iv is not None

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, raw: str) -> ElasticityEstimate:
        d = json.loads(raw)
        for k in ("ci_ols", "ci_iv"):
            if d.get(k) is not None:
                d[k] = tuple(d[k])
        return cls(**d)


def _samples(r: dict) -> int:
    """How many observations a pool_days row's depth sums are over: its
    swaps, or the fee counter's two day-end reads (v12). Rows built before
    v12 carry only `swaps`."""
    n = r.get("samples")
    return int(n if n is not None else r.get("swaps") or 0)


def pool_day_prices(rows: list[dict], infos: dict, stables=_QUOTE_SYMBOLS) -> dict:
    """{(day, token address): USD} from each day's closing pool prices:
    stablecoins at $1, then any token paired with a priced one. On a day a
    token is paired with several priced tokens, the busiest pool sets it."""
    prices: dict[tuple[str, str], float] = {}
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if _samples(r) > 0 and r["pool"] in infos:
            by_day[r["day"]].append(r)
    for day, rs in by_day.items():
        rs.sort(key=lambda r: -(r["swaps"] or 0))  # a fee-counter day counts no swaps
        for r in rs:
            info = infos[r["pool"]]
            for addr, sym in ((info.token0, info.symbol0), (info.token1, info.symbol1)):
                if sym.upper() in stables:
                    prices[(day, addr)] = 1.0
        for _ in range(4):  # a token two hops from a stablecoin needs two passes
            grew = False
            for r in rs:
                info = infos[r["pool"]]
                p = math.pow(1.0001, r["tick_close"]) * 10 ** (info.dec0 - info.dec1)
                k0, k1 = (day, info.token0), (day, info.token1)
                if k1 in prices and k0 not in prices:
                    prices[k0] = p * prices[k1]
                    grew = True
                elif k0 in prices and k1 not in prices and p > 0:
                    prices[k1] = prices[k0] / p
                    grew = True
            if not grew:
                break
    return prices


def elasticity_panel(rows: list[dict], infos: dict, stables=_QUOTE_SYMBOLS) -> list[dict]:
    """One row per pool-week with enough swap-days:
    y = log fees, USD;
    x = log mean active liquidity (raw units — depth, with no price in it);
    z = log(1 + the book's own liquidity at the week's first swap ÷ the
        pool's median liquidity) — the instrument;
    c = log realised volatility; v = log mean √P·p1, the USD value of a unit
        of liquidity, so a price move that re-values the week's fees is not
        read as depth moving them.
    Depth and the instrument are kept out of USD on purpose: both would
    otherwise carry the same price factor as the fees, which builds a
    correlation near 1 into the fit whatever depth really does."""
    prices = pool_day_prices(rows, infos, stables)
    by_pool: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["pool"] in infos and _samples(r) > 0:
            by_pool[r["pool"]].append(dict(r))  # annotated below; the caller's rows stay as given
    panel: list[dict] = []
    for pool, rs in by_pool.items():
        info = infos[pool]
        dscale = 10 ** (info.dec0 - info.dec1)
        rs.sort(key=lambda r: r["day"])
        daily_l = sorted(r["l_sum"] / _samples(r) for r in rs)
        typical = daily_l[len(daily_l) // 2]
        if typical <= 0:
            continue
        weeks: dict[str, list[tuple[dict, float]]] = defaultdict(list)
        prev_tick: tuple[date, int] | None = None
        for r in rs:
            day = date.fromisoformat(r["day"])
            p1 = prices.get((r["day"], info.token1))
            ret = None
            if prev_tick is not None and (day - prev_tick[0]).days == 1:
                ret = (r["tick_close"] - prev_tick[1]) * _LN_TICK
            prev_tick = (day, r["tick_close"])
            if p1 is None:
                continue
            r["_ret"] = ret
            weeks[date.fromordinal(day.toordinal() - day.weekday()).isoformat()].append((r, p1))
        for week, items in weeks.items():
            if len(items) < _MIN_DAYS_PER_WEEK:
                continue
            fees = liq = unit = 0.0
            rets = []
            for r, p1 in items:
                fees += r["fees_token1"] * p1
                liq += r["l_sum"] / _samples(r)
                unit += math.sqrt(math.pow(1.0001, r["tick_close"]) * dscale) * p1
                if r.get("_ret") is not None:
                    rets.append(r["_ret"])
            liq /= len(items)
            unit /= len(items)
            if fees <= 0 or liq <= 0 or unit <= 0:
                continue
            rv = math.sqrt(sum(x * x for x in rets)) if rets else 0.0
            panel.append(
                {
                    "pool": pool,
                    "week": week,
                    "y": math.log(fees),
                    "x": math.log(liq),
                    "z": math.log1p(max(items[0][0]["book_l_open"], 0.0) / typical),
                    "c": math.log(rv + 1e-3),
                    "v": math.log(unit),
                    "days": len(items),
                    "first_day": items[0][0]["day"],
                    "last_day": items[-1][0]["day"],
                }  # fmt: skip
            )
    panel.sort(key=lambda p: (p["pool"], p["week"]))
    return panel


def _demean_two_way(cols: list[list[float]], g1: list, g2: list, tol: float = 1e-11) -> None:
    """Sweep out two sets of fixed effects in place (alternating projections;
    exact for a balanced panel in one pass, converges on an unbalanced one)."""
    for _ in range(1000):
        worst = 0.0
        for groups in (g1, g2):
            for col in cols:
                s: dict = defaultdict(float)
                n: dict = defaultdict(int)
                for v, g in zip(col, groups, strict=True):
                    s[g] += v
                    n[g] += 1
                for i, g in enumerate(groups):
                    m = s[g] / n[g]
                    col[i] -= m
                    worst = max(worst, abs(m))
        if worst < tol:
            return


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve a·x = b by Gauss–Jordan with partial pivoting; None if singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _regress(
    y: list[float], cols: list[list[float]]
) -> tuple[list[float], list[list[float]]] | None:
    """OLS coefficients of y on cols, and (X'X)⁻¹ (for standard errors)."""
    k = len(cols)
    xtx = [[_dot(cols[i], cols[j]) for j in range(k)] for i in range(k)]
    beta = _solve(xtx, [_dot(c, y) for c in cols])
    if beta is None:
        return None
    inv = []
    for i in range(k):
        e = _solve(xtx, [1.0 if j == i else 0.0 for j in range(k)])
        if e is None:
            return None
        inv.append(e)
    return beta, inv


_CONTROLS = ("c", "v")


def _fit(
    panel: list[dict], week_key=lambda p: p["week"]
) -> tuple[float, float | None, float] | None:
    """(ε OLS, ε 2SLS, first-stage F) on a panel, after sweeping out pool
    and week effects. An instrument with no variation left gives (ε OLS,
    None, 0). None when OLS itself is singular."""
    y, x, z = ([p[k] for p in panel] for k in ("y", "x", "z"))
    ctrl = [[p[k] for p in panel] for k in _CONTROLS]
    pools = [p["pool"] for p in panel]
    weeks = [week_key(p) for p in panel]
    _demean_two_way([y, x, z, *ctrl], pools, weeks)
    ols = _regress(y, [x, *ctrl])
    if ols is None:
        return None
    first = _regress(x, [z, *ctrl])
    if first is None:
        return ols[0][0], None, 0.0
    b_first, inv_first = first
    xhat = [
        sum(b * col[i] for b, col in zip(b_first, [z, *ctrl], strict=True)) for i in range(len(x))
    ]
    iv = _regress(y, [xhat, *ctrl])
    if iv is None:
        return ols[0][0], None, 0.0
    resid = [xi - xh for xi, xh in zip(x, xhat, strict=True)]
    dof = len(panel) - len(set(pools)) - len(set(weeks)) - 1 - len(_CONTROLS)
    if dof <= 0:
        return None
    s2 = _dot(resid, resid) / dof
    var_bz = s2 * inv_first[0][0]
    f = b_first[0] ** 2 / var_bz if var_bz > 0 else 0.0
    return ols[0][0], iv[0][0], f


def estimate_volume_elasticity(
    rows: list[dict],
    infos: dict,
    stables=_QUOTE_SYMBOLS,
    n_boot: int = 499,
    seed: int = 7,
) -> ElasticityEstimate:
    """ε with its 95% interval, from pool_days rows and their PoolInfo."""
    import random

    panel = elasticity_panel(rows, infos, stables)
    counts: dict[str, int] = defaultdict(int)
    for p in panel:
        counts[p["pool"]] += 1
    panel = [p for p in panel if counts[p["pool"]] >= 4]  # a pool seen once says nothing
    pools = sorted({p["pool"] for p in panel})
    weeks = sorted({p["week"] for p in panel})
    moves = 0
    last: dict[str, tuple[str, float]] = {}
    for p in panel:
        prev = last.get(p["pool"])
        wk = date.fromisoformat(p["week"])
        if prev and (wk - date.fromisoformat(prev[0])).days == 7:
            moves += abs(p["z"] - prev[1]) >= math.log(1.2)
        last[p["pool"]] = (p["week"], p["z"])
    est = ElasticityEstimate(
        None, None, None, None, None, len(panel), len(pools), len(weeks), moves,
        min((p["first_day"] for p in panel), default=None),
        max((p["last_day"] for p in panel), default=None),
        pools=[f"{infos[x].symbol0}/{infos[x].symbol1} {infos[x].fee_ppm / 10_000:.2f}%" for x in pools],
    )  # fmt: skip
    if len(panel) < _MIN_ELASTICITY_OBS or len(pools) < 3:
        est.reason = f"{len(panel)} pool-weeks across {len(pools)} pool(s) — too few to estimate"
        return est
    fit = _fit([dict(p) for p in panel])
    if fit is None:
        est.reason = "the regression is singular on this history"
        return est
    est.eps_ols, est.eps_iv, est.first_stage_f = fit
    by_week: dict[str, list[dict]] = defaultdict(list)
    for p in panel:
        by_week[p["week"]].append(p)
    rng = random.Random(seed)
    ols_draws: list[float] = []
    iv_draws: list[float] = []
    for _ in range(n_boot):
        draw = []
        for k in range(len(weeks)):
            wk = weeks[rng.randrange(len(weeks))]
            draw += [{**p, "draw": k} for p in by_week[wk]]
        f = _fit(draw, week_key=lambda p: p["draw"])
        if f is not None:
            ols_draws.append(f[0])
            if f[1] is not None:
                iv_draws.append(f[1])

    def ci(xs: list[float]) -> tuple[float, float] | None:
        if len(xs) < n_boot // 2:
            return None
        xs = sorted(xs)
        return xs[int(0.025 * (len(xs) - 1))], xs[int(0.975 * (len(xs) - 1))]

    est.ci_ols, est.ci_iv = ci(ols_draws), ci(iv_draws)
    if est.first_stage_f < _MIN_FIRST_STAGE_F:
        est.reason = (
            f"the book's own depth moves too little to identify ε (first-stage F "
            f"{est.first_stage_f:.1f} < {_MIN_FIRST_STAGE_F:g})"
        )
    elif est.ci_iv is None:
        est.reason = "the bootstrap failed on too many draws for an interval"
    return est


def measured_rebalance_config(
    base: RebalanceConfig, est: ElasticityEstimate | None
) -> RebalanceConfig:
    """`base` with ε and its bracket replaced by the measured ones, clipped to
    [0, 1] — the range over which book EV stays concave and the optimiser's
    search is valid. Unchanged when there is no usable estimate."""
    from dataclasses import replace

    if est is None or not est.usable:
        return base
    assert est.eps_iv is not None and est.ci_iv is not None

    def clip(v: float) -> float:
        return min(1.0, max(0.0, v))

    lo, hi = clip(est.ci_iv[0]), clip(est.ci_iv[1])
    return replace(
        base, volume_elasticity=min(hi, max(lo, clip(est.eps_iv))), elasticity_band=(lo, hi)
    )


# ── Backtest: the optimiser replayed over history (Phase 5.3) ─────────────
#
# At each rebalance date the pool is rebuilt as the optimiser would have
# seen it — σ from the trailing daily closes, fees/day from the trailing
# week, the pool's active depth and the book's positions as its events had
# left them — and the policy chooses a band for the book's capital there.
# Then both the policy's band and the book as it stood ("stay") are run
# forward over the horizon on what actually happened: each day's fees,
# its close, and the depth other LPs actually supplied. Each earns the
# day's fees when the close sits in its band, in proportion to its share of
# the depth, with the day's fees scaled by (its depth ÷ the actual)^ε; each
# ends with the IL its band realised against holding what it started with.
#
# So a strategy change is measured, not argued: the realised gain beside
# the gain the model predicted at the time, window by window. Stated
# approximations: a day is in or out of a band on its close (no intraday
# path); both branches hold their liquidity fixed through the horizon (the
# book's own later actions are not replayed); swap costs are not charged.


@dataclass(frozen=True)
class HistoryDay:
    day: str
    tick: int  # close
    fees_usd: float
    l_pool: float  # swap-weighted active liquidity, human units
    l_book: float  # the book's share of it, human units
    p0: float
    p1: float


@dataclass
class BacktestWindow:
    pool: str
    pair: str
    start: str
    end: str
    capital_usd: float
    stay_bands: list[tuple[int, int]]
    band: tuple[int, int] | None  # the policy's choice; None = it chose the book as it was
    predicted_gain_usd: float  # policy − stay, the model's EV at the start
    stay_usd: float  # realised fees − IL, the book as it stood
    policy_usd: float  # realised fees − IL, the policy's band
    stay_fees_usd: float = 0.0
    policy_fees_usd: float = 0.0
    chain: str = ""
    acted: bool = True  # False: not expected to pay (≤ min_ev), so the book stayed

    @property
    def realised_gain_usd(self) -> float:
        return self.policy_usd - self.stay_usd


@dataclass
class Backtest:
    windows: list[BacktestWindow]
    skipped: list[tuple[str, str, str]]  # (pool, date, why)
    horizon_days: int
    elasticity: float
    strategy: str = "optimiser"

    @property
    def predicted_usd(self) -> float:
        return sum(w.predicted_gain_usd for w in self.windows)

    @property
    def realised_usd(self) -> float:
        return sum(w.realised_gain_usd for w in self.windows)

    chains: tuple[str, ...] = ()  # the chains whose pools have swap history to replay

    @property
    def hit_rate(self) -> float | None:
        """Of the windows where the policy was predicted to gain, the share
        where it did."""
        acted = [w for w in self.windows if w.predicted_gain_usd > 0]
        if not acted:
            return None
        return sum(1 for w in acted if w.realised_gain_usd > 0) / len(acted)

    def split(self, day: str) -> tuple[Backtest, Backtest]:
        """(in sample, out of sample) about `day`: windows that ENDED before
        it, and windows that start on or after it. A window straddling the
        split is in neither — its outcome would leak across."""
        from dataclasses import replace

        before = [w for w in self.windows if w.end < day]
        after = [w for w in self.windows if w.start >= day]
        return replace(self, windows=before), replace(self, windows=after)


def pool_history(rows: list[dict], info, prices: dict) -> list[HistoryDay]:
    """A pool's priced daily series from its pool_days rows (days without a
    USD price for both legs are left out, not guessed)."""
    l_scale = 10 ** (-(info.dec0 + info.dec1) / 2)
    out = []
    for r in sorted(rows, key=lambda r: r["day"]):
        if _samples(r) <= 0:
            continue
        p0, p1 = prices.get((r["day"], info.token0)), prices.get((r["day"], info.token1))
        if p0 is None or p1 is None:
            continue
        out.append(
            HistoryDay(
                r["day"],
                int(r["tick_close"]),
                r["fees_token1"] * p1,
                r["l_sum"] / _samples(r) * l_scale,
                r["book_l_sum"] / _samples(r) * l_scale,
                p0,
                p1,
            )  # fmt: skip
        )
    return out


def _band_value(liq: float, tick: int, band: tuple[int, int], scale: float, p0: float, p1: float):
    """(token0, token1, USD) behind `liq` in `band` at `tick`."""
    sp = _sqrt_at(tick, scale)
    x, y = _amounts(liq, sp, _sqrt_at(band[0], scale), _sqrt_at(band[1], scale))
    return x, y, x * p0 + y * p1


def _replay(
    legs: list[tuple[float, tuple[int, int]]],
    days: list[HistoryDay],
    scale: float,
    eps: float,
) -> tuple[float, float]:
    """(fees, fees − IL) realised by `legs` (liquidity, band) held fixed over
    `days`, the first of which is the start."""
    start, end = days[0], days[-1]
    fees = 0.0
    for d in days:
        active = sum(liq for liq, (lo, hi) in legs if lo <= d.tick < hi)
        if active <= 0 or d.l_pool <= 0:
            continue
        ext = max(d.l_pool - d.l_book, 0.0)
        depth = ext + active
        fees += d.fees_usd * (depth / d.l_pool) ** eps * active / depth
    il = 0.0
    for liq, band in legs:
        x0, y0, _ = _band_value(liq, start.tick, band, scale, start.p0, start.p1)
        _, _, v_end = _band_value(liq, end.tick, band, scale, end.p0, end.p1)
        il += x0 * end.p0 + y0 * end.p1 - v_end
    return fees, fees - il


def backtest_optimiser(
    rows_by_pool: dict[str, list[dict]],
    infos: dict,
    book: dict[str, list[tuple[int, str, int, int, int]]],
    config: RebalanceConfig | None = None,
    *,
    step_days: int = 7,
    history_days: int = 90,
    min_capital_usd: float = 1000.0,
    strategy: Strategy | None = None,
) -> Backtest:
    """Replay a strategy over each pool's history. `book[pool]` is the book's
    liquidity changes there — (ts, position key, lower, upper, signed raw
    delta). The strategy defaults to config.strategy; a band of None keeps
    the book as it stood."""
    config = config or DEFAULT_REBALANCE_CONFIG
    strat = strategy or strategy_from_spec(config.strategy)
    H = int(config.target_horizon_days)
    eps = config.volume_elasticity
    windows: list[BacktestWindow] = []
    skipped: list[tuple[str, str, str]] = []
    prices = pool_day_prices([r for rs in rows_by_pool.values() for r in rs], infos)
    for pool, rows in sorted(rows_by_pool.items()):
        info = infos.get(pool)
        if info is None or pool not in book:
            continue
        pair = f"{info.symbol0}/{info.symbol1} {info.fee_ppm / 10_000:.2f}%"
        days = pool_history(rows, info, prices)
        by_day = {d.day: i for i, d in enumerate(days)}
        l_scale = 10 ** (-(info.dec0 + info.dec1) / 2)
        scale = math.sqrt(10 ** (info.dec0 - info.dec1))
        changes = sorted(book[pool])
        spacing = infer_tick_spacing([t for _, _, lo, hi, _ in changes for t in (lo, hi)])
        if not days:
            continue
        first = date.fromisoformat(days[0].day)
        last = date.fromisoformat(days[-1].day)
        t = first.fromordinal(first.toordinal() + 30)
        while t.toordinal() + H <= last.toordinal():
            label = t.isoformat()
            t_next = t.fromordinal(t.toordinal() + step_days)
            window = [
                days[by_day[d]]
                for k in range(H + 1)
                if (d := t.fromordinal(t.toordinal() + k).isoformat()) in by_day
            ]
            prior = [
                days[by_day[d]]
                for k in range(1, history_days + 1)
                if (d := t.fromordinal(t.toordinal() - k).isoformat()) in by_day
            ]
            t, t0 = t_next, t
            if len(window) < H * 0.6 or window[0].day != label:
                skipped.append((pair, label, "too few swap-days in the horizon"))
                continue
            prior.sort(key=lambda d: d.day)
            rets = _returns(prior)
            year = sorted(
                (
                    days[by_day[d]]
                    for k in range(1, 366)
                    if (d := t0.fromordinal(t0.toordinal() - k).isoformat()) in by_day
                ),
                key=lambda d: d.day,
            )
            if len(rets) < config.min_sigma_obs:
                skipped.append((pair, label, f"{len(rets)} daily returns before it"))
                continue
            mean = sum(rets) / len(rets)
            sigma = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
            week_start = t0.fromordinal(t0.toordinal() - 7).isoformat()
            fees_day = sum(d.fees_usd for d in prior if d.day >= week_start) / 7
            ref = prior[-1]
            start_ts = int(_utc_midnight(t0))
            liq_by_key: dict[str, list] = {}
            for ts, key, lo, hi, delta in changes:
                if ts >= start_ts:
                    break
                leg = liq_by_key.setdefault(key, [lo, hi, 0])
                leg[2] += delta
            legs = [(v * l_scale, (lo, hi)) for lo, hi, v in liq_by_key.values() if v > 0]
            cur, s0 = window[0].tick, window[0]
            capital = sum(_band_value(liq, cur, band, scale, s0.p0, s0.p1)[2] for liq, band in legs)
            if capital < min_capital_usd:
                skipped.append((pair, label, f"book held ${capital:,.0f} there"))
                continue
            l_active = ref.l_pool
            book_active = sum(liq for liq, (lo, hi) in legs if lo <= cur < hi)
            l_ext = max(l_active - book_active, 0.0)
            sp = _sqrt_at(cur, scale)
            stay_ev = 0.0
            for liq, (lo, hi) in legs:
                others = book_active - (liq if lo <= cur < hi else 0.0)
                e = band_ev(
                    liq, sp, s0.p1, (lo - cur) * _LN_TICK, (hi - cur) * _LN_TICK, fees_day,
                    l_ext, others, sigma, float(H), elasticity=eps, depth_ref=l_active,
                )  # fmt: skip
                stay_ev += e.fees_usd - e.il_usd
            view = PoolView(
                capital_usd=capital, tick_current=cur, sqrt_p=sp, scale=scale, p1=s0.p1,
                fees_per_day=fees_day, l_ext=l_ext, l_own=0.0, l_active=l_active, sigma=sigma,
                horizon=float(H), spacing=spacing, current=tuple(legs),
                returns=tuple(_returns(year)), n_obs=len(rets),
            )  # fmt: skip
            chosen = strat.band(view, config)
            predicted = 0.0
            if chosen is not None:
                predicted = chosen.ev.fees_usd - chosen.ev.il_usd - stay_ev
            band = chosen.ticks if chosen is not None else None
            # The queue proposes a re-range only when it is expected to pay;
            # below that the book stays as it is.
            acted = chosen is not None and predicted > config.min_ev_usd
            p_legs = [(chosen.liquidity, chosen.ticks)] if acted and chosen else legs
            s_fees, stay = _replay(legs, window, scale, eps)
            p_fees, pol = _replay(p_legs, window, scale, eps)
            windows.append(
                BacktestWindow(
                    pool,
                    pair,
                    label,
                    window[-1].day,
                    capital,
                    [b for _, b in legs],
                    band,
                    predicted,
                    stay,
                    pol,
                    s_fees,
                    p_fees,
                    info.chain,
                    acted,
                )  # fmt: skip
            )
    chains = tuple(sorted({infos[p].chain for p in rows_by_pool if p in infos}))
    return Backtest(windows, skipped, H, eps, strat.spec, chains)


def _returns(days: list[HistoryDay]) -> list[float]:
    """Daily log returns between consecutive calendar days' closes."""
    return [
        (b.tick - a.tick) * _LN_TICK
        for a, b in zip(days, days[1:], strict=False)
        if (date.fromisoformat(b.day) - date.fromisoformat(a.day)).days == 1
    ]


def _utc_midnight(d: date) -> float:
    from datetime import UTC, datetime

    return datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()


# ── Strategies (Phase 5.4) ────────────────────────────────────────────────
#
# A strategy answers one question: given a pool as it stands and the
# capital to place, which band? The live queue asks it for the band a
# re-range ticket moves to; the backtest asks it at every rebalance date of
# history. The queue's other candidates — resize within the band, close,
# harvest — and every cost, ε and σ band stay the queue's, so strategies
# differ only where they claim to. Chosen by a spec string
# (RebalanceConfig.strategy), so a variant is configuration, not code.


@dataclass(frozen=True)
class PoolView:
    """A pool as a strategy sees it, human liquidity units throughout."""

    capital_usd: float
    tick_current: int
    sqrt_p: float
    scale: float
    p1: float
    fees_per_day: float
    l_ext: float  # other LPs' active liquidity
    l_own: float  # the book's other positions' active liquidity
    l_active: float  # the pool's active liquidity today
    sigma: float  # daily σ of log price
    horizon: float
    spacing: int
    current: tuple = ()  # ((liquidity, (lower, upper)), …) — what is there now
    returns: tuple[float, ...] = ()  # daily log returns before today, oldest first
    n_obs: int = 0  # the returns σ was measured from


class Strategy(ABC):
    """Picks the band for a pool's capital. None: leave it where it is."""

    spec: str

    @abstractmethod
    def band(self, v: PoolView, config: RebalanceConfig) -> BandChoice | None: ...


def _choice_for(v: PoolView, ticks: tuple[int, int], config: RebalanceConfig) -> BandChoice:
    """A band placed with the view's capital, scored the way best_band scores."""
    s_lo, s_hi = _sqrt_at(ticks[0], v.scale), _sqrt_at(ticks[1], v.scale)
    liq = v.capital_usd * liquidity_per_usd(v.sqrt_p, s_lo, s_hi, v.p1)
    cur = v.tick_current
    ev = band_ev(
        liq, v.sqrt_p, v.p1, (ticks[0] - cur) * _LN_TICK, (ticks[1] - cur) * _LN_TICK,
        v.fees_per_day, v.l_ext, v.l_own, v.sigma, v.horizon,
        elasticity=config.volume_elasticity, depth_ref=v.l_active,
    )  # fmt: skip
    ratio = (v.l_ext + v.l_own + liq) / v.l_active if v.l_active else 1.0
    return BandChoice(ticks, liq, ev, False, ratio - 1)


@dataclass
class OptimiserStrategy(Strategy):
    """5.1's answer: the symmetric band that maximises the book's EV.

    Knobs (spec 'optimiser:k=v,…') change only the σ the band is SEARCHED
    at; the band it finds is scored at the queue's σ, as the live queue
    scores every candidate — so a variant is a different choice of band,
    never a different view of what it will earn. The optimiser lost its
    backtest choosing narrow bands on a calm trailing σ:
      window=<days> σ from the last <days> daily returns (up to a year)
                    instead of the queue's 90;
      ewma=<λ>      σ as an exponentially weighted average (RiskMetrics:
                    var ← λ·var + (1−λ)·r²) — recent days weigh most;
      upper         the upper end of σ's 95% band (σ·(1 + 1.96/√(2(n−1))))
                    — the σ the queue already stress-tests every ticket at;
      floor=<pct>   σ at least <pct>% a day;
      minw=<pct>    no band narrower than ±<pct>% about spot: a narrower
                    answer is widened to it (EV is concave in width, so
                    the constrained optimum sits on the constraint).
    A window or EWMA with fewer returns than min_sigma_obs falls back to
    the queue's σ.
    """

    floor_pct: float | None = None
    window_days: int | None = None
    ewma: float | None = None
    upper: bool = False
    min_half_pct: float | None = None

    @property
    def spec(self) -> str:  # type: ignore[override]
        knobs = []
        if self.window_days is not None:
            knobs.append(f"window={self.window_days}")
        if self.ewma is not None:
            knobs.append(f"ewma={self.ewma:g}")
        if self.upper:
            knobs.append("upper")
        if self.floor_pct is not None:
            knobs.append(f"floor={self.floor_pct:g}")
        if self.min_half_pct is not None:
            knobs.append(f"minw={self.min_half_pct:g}")
        return "optimiser" + (":" + ",".join(knobs) if knobs else "")

    def search_sigma(self, v: PoolView, min_obs: int = 30) -> float:
        sigma = v.sigma
        rets = v.returns[-self.window_days :] if self.window_days else v.returns
        if (self.window_days or self.ewma) and len(rets) >= min_obs:
            if self.ewma is not None:
                var = sum(r * r for r in rets[:10]) / 10
                for r in rets[10:]:
                    var = self.ewma * var + (1 - self.ewma) * r * r
            else:
                mean = sum(rets) / len(rets)
                var = sum((r - mean) ** 2 for r in rets) / len(rets)
            sigma = math.sqrt(var)
            if self.upper:
                sigma = sigma_band(sigma, len(rets))[1]
        elif self.upper:
            sigma = sigma_band(sigma, v.n_obs)[1]
        if self.floor_pct is not None:
            sigma = max(sigma, self.floor_pct / 100)
        return sigma

    def band(self, v: PoolView, config: RebalanceConfig) -> BandChoice | None:
        sigma = self.search_sigma(v, config.min_sigma_obs)
        found = best_band(
            capital_usd=v.capital_usd, tick_current=v.tick_current, sqrt_p=v.sqrt_p,
            scale=v.scale, p1=v.p1, fees_per_day=v.fees_per_day, l_ext=v.l_ext,
            l_own=v.l_own, l_active_before=v.l_active, sigma=sigma, horizon=v.horizon,
            spacing=v.spacing, depth_bounds=config.depth_bounds, z_grid=config.z_grid,
            elasticity=config.volume_elasticity,
        )  # fmt: skip
        if found is None:
            return None
        if self.min_half_pct is not None:
            half = math.log(1 + self.min_half_pct / 100) / _LN_TICK
            lo, hi = found.ticks
            if min(v.tick_current - lo, hi - v.tick_current) < half:
                return _choice_for(v, snap_band(v.tick_current, half, v.spacing), config)
        if sigma == v.sigma:
            return found
        scored = _choice_for(v, found.ticks, config)
        return replace(scored, at_edge=found.at_edge, at_bound=found.at_bound)


@dataclass
class SigmaWidthStrategy(Strategy):
    """±z·σ·√T about spot: the width a price walk stays inside with a
    chosen confidence, whatever the fees."""

    z: float

    @property
    def spec(self) -> str:  # type: ignore[override]
        return f"sigma:{self.z:g}"

    def band(self, v: PoolView, config: RebalanceConfig) -> BandChoice | None:
        half = self.z * v.sigma * math.sqrt(v.horizon) / _LN_TICK
        return _choice_for(v, snap_band(v.tick_current, half, v.spacing), config)


@dataclass
class FixedWidthStrategy(Strategy):
    """±pct% about spot, the same every time."""

    pct: float

    @property
    def spec(self) -> str:  # type: ignore[override]
        return f"fixed:{self.pct:g}"

    def band(self, v: PoolView, config: RebalanceConfig) -> BandChoice | None:
        half = math.log(1 + self.pct / 100) / _LN_TICK
        return _choice_for(v, snap_band(v.tick_current, half, v.spacing), config)


class HoldStrategy(Strategy):
    """Never moves a band: the baseline every other strategy is measured
    against, and a way to switch re-range tickets off."""

    spec = "hold"

    def band(self, v: PoolView, config: RebalanceConfig) -> BandChoice | None:
        return None


def _optimiser_from_knobs(arg: str) -> OptimiserStrategy:
    """'floor=5' → OptimiserStrategy(floor_pct=5). Raises on an unknown
    knob or a value out of range."""
    kw: dict = {}
    for part in filter(None, (x.strip() for x in arg.split(","))):
        key, _, val = part.partition("=")
        if key == "floor" and float(val) > 0:
            kw["floor_pct"] = float(val)
        elif key == "window" and int(val) >= 2:
            kw["window_days"] = int(val)
        elif key == "ewma" and 0 < float(val) < 1:
            kw["ewma"] = float(val)
        elif key == "upper" and not val:
            kw["upper"] = True
        elif key == "minw" and float(val) > 0:
            kw["min_half_pct"] = float(val)
        else:
            raise ValueError(part)
    return OptimiserStrategy(**kw)


def strategy_from_spec(spec: str) -> Strategy:
    """'optimiser[:knobs]' · 'hold' · 'sigma:<z>' · 'fixed:<pct>'. Raises on
    anything else — a misspelt strategy must not quietly become the default.
    Optimiser knobs: window=<days>, ewma=<λ>, upper, floor=<pct>, minw=<pct>."""
    name, _, arg = spec.strip().partition(":")
    try:
        if name == "optimiser":
            return _optimiser_from_knobs(arg)
        if name == "hold" and not arg:
            return HoldStrategy()
        if name == "sigma" and float(arg) > 0:
            return SigmaWidthStrategy(float(arg))
        if name == "fixed" and float(arg) > 0:
            return FixedWidthStrategy(float(arg))
    except ValueError:
        pass
    raise ValueError(
        f"unknown strategy {spec!r} — use optimiser[:window=<d>,ewma=<λ>,upper,floor=<pct>,minw=<pct>], hold, sigma:<z> or fixed:<pct>"
    )


def variant_grid() -> list[str]:
    """The optimiser variants `backtest --grid` chooses among: every
    combination of σ estimator, σ-band end, σ floor and minimum width."""
    specs = []
    for est in ("", "window=30", "ewma=0.97"):
        for upper in ("", "upper"):
            for floor in ("", "floor=8", "floor=10", "floor=12", "floor=15"):
                for minw in ("", "minw=50", "minw=100", "minw=150"):
                    knobs = ",".join(k for k in (est, upper, floor, minw) if k)
                    specs.append("optimiser" + (f":{knobs}" if knobs else ""))
    return specs


def adoptable(out_of_sample: Backtest) -> bool:
    """The bar for making a strategy the default: out of sample it beats
    hold (a positive realised gain — hold's is zero) and is right at least
    half the time it expects to gain."""
    hr = out_of_sample.hit_rate
    return out_of_sample.realised_usd > 0 and hr is not None and hr >= 0.5


def backtest_verdict(bt: Backtest | None, ticket_chains: set[str] | None = None) -> str:
    """One line for the head of the queue: how far to trust its tickets, on
    the backtest's evidence. Every surface prints this same line."""
    if bt is None or not bt.windows:
        return (
            "Advisory — the strategy has not been backtested (`defi-tracker elasticity --sync`, "
            "then `defi-tracker backtest`)"
        )
    hr = bt.hit_rate
    rate = f", right {hr:.0%} of the time" if hr is not None else ""
    record = (
        f"in backtest '{bt.strategy}' realised {money(bt.realised_usd, True)} against "
        f"{money(bt.predicted_usd, True)} predicted over {len(bt.windows)} windows{rate}"
    )
    untested = sorted((ticket_chains or set()) - set(bt.chains))
    gap = f"; {', '.join(untested)} not backtested (no pool history)" if untested else ""
    trusted = bt.realised_usd > 0 and (hr is None or hr >= 0.5)
    if trusted:
        return f"Backtested — {record}{gap}"
    return f"Advisory only — {record}; do not act on a ticket on the model's word{gap}"
