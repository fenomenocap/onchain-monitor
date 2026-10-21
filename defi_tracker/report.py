"""
Report assembly — the one place the daily book is gathered.

Every surface renders the same `ReportData`: Slack (`slack.py`) and the HTML
desk view (`web.py`). Gathering lives here rather than inside a delivery class
so a second surface never has to re-derive what the first already knows — two
surfaces disagreeing about the book is worse than having only one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING

from defi_tracker.core.storage import Storage, ValueDelta

if TYPE_CHECKING:
    from defi_tracker.analytics import (
        ActionQueue,
        Backtest,
        ElasticityEstimate,
        FeeTable,
        PeaqMark,
        PeriodPnL,
        RebalanceConfig,
    )

# Trailing window for realized yield (accrual basis) in every report surface.
YIELD_WINDOW_DAYS = 30


def unclaimed_known(row) -> bool:
    """Was this row's unclaimed balance actually readable? Rows predating the
    column are treated as known, which is what they were."""
    try:
        return bool(row["unclaimed_is_known"])
    except (KeyError, IndexError):
        return True


@dataclass
class ReportData:
    """Everything a report renders — gathered once, rendered many ways."""

    snaps: list[dict]
    collect_rows: list[dict] = field(default_factory=list)
    value_delta: ValueDelta | None = None
    price_today: dict = field(default_factory=dict)
    flow_rows: list[dict] = field(default_factory=list)
    window_stats: list[dict] = field(default_factory=list)
    window_collects: list[dict] = field(default_factory=list)
    snoozed_uids: set = field(default_factory=set)
    health_rows: list[dict] = field(default_factory=list)
    tx_cost_rows: list[dict] = field(default_factory=list)
    periods: list[PeriodPnL] = field(default_factory=list)
    fee_table: FeeTable | None = None
    risk: dict = field(default_factory=dict)  # gather_risk(): per-position + PEAQ scenarios
    mandate: dict = field(default_factory=dict)  # gather_mandate_state()
    actions: ActionQueue | None = None  # gather_action_queue(): tickets + pool plans
    elasticity: ElasticityEstimate | None = (
        None  # ElasticityEstimate the queue was scored with, or None (unmeasured)
    )
    backtest: Backtest | None = None  # gather_backtest(): the queue strategy's record on history
    rebalance_config: RebalanceConfig | None = None  # the RebalanceConfig the queue was scored with
    peaq: PeaqMark | None = None  # PEAQ now: market at build, book pools as cross-check
    mm_book: list[dict] = field(default_factory=list)  # contracts in force, marked at PEAQ now
    rfq: dict = field(default_factory=dict)  # content/rfq.toml: the MM RFQ comparison
    venues: list[dict] = field(
        default_factory=list
    )  # PEAQ's markets (CoinGecko) at build; [] = not read
    delivery: list[dict] = field(
        default_factory=list
    )  # Coinwatch: what each MM delivered, per day (coinwatch.py); [] = not read
    orderbooks: dict = field(
        default_factory=dict
    )  # orderbooks.fetch_orderbooks(): public books at build
    peaq_sigma: tuple[float, int, str] | None = None  # PEAQ's daily σ (log), n returns, its pool


def gather_report_data(
    storage: Storage,
    price_today: dict | None = None,
    venues: list[dict] | None = None,
    delivery: list[dict] | None = None,
    orderbooks: dict | None = None,
) -> ReportData | None:
    """
    Read the whole book out of storage in one pass.

    Returns None when there is nothing to report — no snapshot has ever been
    written — which callers treat as "not an error, just nothing to say".
    """
    from defi_tracker.analytics import contract_marks, fee_table, peaq_mark

    snaps = storage.latest_snapshot()
    if not snaps:
        return None
    built = datetime.now(UTC)
    peaq = peaq_mark(snaps, price_today, int(built.timestamp()))
    collect_rows = storage.monthly_collects()
    health_rows = storage.pipeline_health()

    today = datetime.now(UTC).date()
    window_start = today.fromordinal(today.toordinal() - YIELD_WINDOW_DAYS).isoformat()
    periods = gather_periods(storage)
    return ReportData(
        snaps=snaps,
        collect_rows=collect_rows,
        value_delta=storage.value_delta(),
        price_today=price_today or {},
        flow_rows=storage.position_flows(),
        window_stats=storage.position_window_stats(YIELD_WINDOW_DAYS),
        window_collects=storage.collects_since(window_start),
        snoozed_uids=storage.snoozed_rebalance_uids(),
        health_rows=health_rows,
        tx_cost_rows=storage.tx_cost_rows(),
        periods=periods,
        risk=gather_risk(storage, snaps),
        mandate=gather_mandate_state(storage, snaps, periods),
        actions=gather_action_queue(storage, snaps),
        elasticity=stored_elasticity(storage),
        backtest=_backtest_or_none(storage),
        rebalance_config=effective_rebalance_config(storage),
        peaq=peaq,
        venues=venues or [],
        delivery=delivery or [],
        orderbooks=orderbooks or {},
        peaq_sigma=peaq_sigma(storage),
        rfq=_load_rfq(),
        mm_book=contract_marks(
            storage.contracts_as_of(built.date()),
            peaq,
            built.date(),
            storage.tranches_as_of(built.date()),
        ),
        fee_table=fee_table(
            collect_rows,
            snaps=snaps,
            unclaimed_series=storage.unclaimed_series(),
            health_rows=health_rows,
            active=storage.active_wallet_chains(),
            price_today=price_today or {},
        )
        if collect_rows
        else None,
    )


def _load_rfq() -> dict:
    from defi_tracker.policy import load_rfq

    return load_rfq()


def token_decimals(storage: Storage) -> dict[str, int]:
    """{token_key: decimals} for every token the book has seen."""
    with storage.connect() as conn:
        return {
            f"{r['chain']}:{r['address']}": int(r["decimals"])
            for r in conn.execute("SELECT chain, address, decimals FROM tokens")
        }


def pool_volatility(storage: Storage) -> dict:
    """{(chain, pair_label): (σ ticks/day, n daily returns)} over 90 days —
    the one σ the risk section, the optimiser and the action queue share."""
    from defi_tracker.analytics import tick_volatility_with_counts

    today = datetime.now(UTC).date()
    return tick_volatility_with_counts(
        storage.snapshots_in_range(start_date=today.fromordinal(today.toordinal() - 90))
    )


def peaq_sigma(storage: Storage) -> tuple[float, int, str] | None:
    """PEAQ's own daily σ (log price) for pricing options granted to MMs:
    the PEAQ/stablecoin pool with the most daily returns in the shared
    90-day σ, ties to the larger σ (the conservative read). None = no pool."""
    from defi_tracker.analytics import _LN_TICK, _STABLE_SYMBOLS, PEAQ_EXPOSURE_SYMBOLS

    best: tuple[float, int, str] | None = None
    for (chain, pair), (sig, n) in pool_volatility(storage).items():
        syms = pair.split(" ")[0].upper().split("/")
        if len(syms) != 2 or not (
            {syms[0], syms[1]} & PEAQ_EXPOSURE_SYMBOLS and {syms[0], syms[1]} & _STABLE_SYMBOLS
        ):
            continue
        cand = (sig * _LN_TICK, n, f"{chain} {pair}")
        if best is None or (n, cand[0]) > (best[1], best[0]):
            best = cand
    return best


def pool_returns(storage: Storage) -> dict:
    """{(chain, pair_label): daily log returns over the last year, oldest
    first} — what a strategy variant with its own σ (a longer window, an
    EWMA) reads; the shared 90-day σ above is unchanged by it."""
    from defi_tracker.analytics import daily_log_returns

    today = datetime.now(UTC).date()
    return daily_log_returns(
        storage.snapshots_in_range(start_date=today.fromordinal(today.toordinal() - 365))
    )


ELASTICITY_KEY = "elasticity_estimate"


def stored_elasticity(storage: Storage) -> ElasticityEstimate | None:
    """The last ε measured from swap history (`defi-tracker elasticity`), or
    None when it has never been measured."""
    from defi_tracker.analytics import ElasticityEstimate

    raw = storage.kv_get(ELASTICITY_KEY)
    return ElasticityEstimate.from_json(raw) if raw else None


def effective_rebalance_config(storage: Storage, base=None) -> RebalanceConfig:
    """The optimiser's config with ε measured where it has been: the default
    (or `base`) with volume_elasticity and its bracket from the stored
    estimate when that estimate is usable, else unchanged."""
    from defi_tracker.analytics import DEFAULT_REBALANCE_CONFIG, measured_rebalance_config

    return measured_rebalance_config(base or DEFAULT_REBALANCE_CONFIG, stored_elasticity(storage))


def _backtest_or_none(storage: Storage):
    """The queue strategy's backtest for the report's verdict line. A report
    never fails for it: without one, the verdict says it is untested."""
    try:
        return gather_backtest(storage)
    except Exception:
        return None


def gather_backtest(storage: Storage, config=None, **kw):
    """The optimiser replayed over every scanned pool's swap history (5.3),
    against the book as it stood at each rebalance date."""
    from defi_tracker.analytics import backtest_optimiser
    from defi_tracker.core.adapter import all_adapters
    from defi_tracker.poolflow import book_changes, history_source, pool_infos

    rows_by_pool: dict[str, list[dict]] = {}
    infos: dict = {}
    book: dict = {}
    for adapter in all_adapters():
        for chain in adapter.info.chains:
            if history_source(adapter, chain) is None:
                continue
            for r in storage.pool_days(chain.value):
                rows_by_pool.setdefault(r["pool"], []).append(r)
            infos.update(pool_infos(storage, chain.value))
            for pool, changes in book_changes(storage, adapter, chain).items():
                book[pool] = [(c["ts"], c["key"], c["lo"], c["hi"], c["delta"]) for c in changes]
    return backtest_optimiser(
        rows_by_pool, infos, book, config or effective_rebalance_config(storage), **kw
    )


def gather_pool_plans(storage: Storage, snaps: list[dict], config=None) -> list:
    """The range optimiser's answer for every pool the book is in (5.1)."""
    from defi_tracker.analytics import optimise_pool, pool_states

    config = config or effective_rebalance_config(storage)
    states = pool_states(
        snaps, token_decimals(storage), pool_volatility(storage), pool_returns(storage)
    )
    return [optimise_pool(ps, config) for ps in states]


def gather_action_queue(storage: Storage, snaps: list[dict], config=None) -> ActionQueue:
    """The EV-ranked action queue (5.2): a trade ticket per position whose
    best action pays over the horizon, built on the pool plans of 5.1."""
    from defi_tracker.analytics import action_queue

    config = config or effective_rebalance_config(storage)
    return action_queue(gather_pool_plans(storage, snaps, config), config)


def gather_risk(storage: Storage, snaps: list[dict], horizon_days: float = 30.0) -> dict:
    """Scenario grid, greeks and range probabilities for every live position
    (Phase 1.3–1.5), plus the book under a PEAQ move. σ is each pool's daily
    tick volatility over the last 90 days, with its observation count."""
    from defi_tracker.analytics import peaq_scenarios, position_risk

    decimals = token_decimals(storage)
    vol = pool_volatility(storage)
    positions = []
    for row in snaps:
        sv = vol.get((row["chain"], row["pair_label"]))
        r = position_risk(row, decimals, sv[0] if sv else None, sv[1] if sv else 0, horizon_days)
        positions.append({"row": row, "risk": r})
    return {
        "positions": positions,
        "peaq": peaq_scenarios(snaps, decimals),
        "horizon_days": horizon_days,
    }


def gather_mandate_state(
    storage: Storage, snaps: list[dict], periods: list[PeriodPnL] | None = None
) -> dict:
    """Where the book stands against the mandate right now: limit checks,
    exception and incident logs, and the contracts in force. No network: a
    depeg limit reads UNKNOWN here (the runner's pass reads market prices and
    alerts); a malformed policy file is carried as an error, never as
    'no limits'. `periods` is `gather_periods(storage)`, read here if not given."""
    from defi_tracker.analytics import counterparty_exposure, mandate_scorecard, policy_checks
    from defi_tracker.policy import PolicyError, load_charter, load_policy

    today = datetime.now(UTC).date()
    contracts = storage.contracts_as_of(today)
    exposure = counterparty_exposure(contracts, today)
    try:
        policy = load_policy()
        checks = policy_checks(snaps, policy, None, exposure)
        source, error = policy.source, None
    except PolicyError as e:
        checks, source, error = [], "policy.toml", str(e)
    incidents = storage.list_incidents()
    exceptions = storage.list_exceptions()
    return {
        "scorecard": mandate_scorecard(
            [
                p
                for p in (gather_periods(storage) if periods is None else periods)
                if p.kind in ("MTD", "QTD", "ITD")
            ],
            contracts,
            incidents,
            exceptions,
            reports_written(),
            today,
        ),
        "checks": checks,
        "policy_source": source,
        "policy_error": error,
        "exceptions": exceptions,
        "incidents": incidents,
        "contracts": contracts,
        "exposure": exposure,
        "charter": load_charter(),
        "as_of": today,
    }


def reports_written(out_dir: str = "reports") -> list[dict]:
    """The monthly mandate reports on disk: [{month, final}]. A final report
    is written once (mandate.write_mandate_report), so its presence is the
    record that the month was reported."""
    import re
    from pathlib import Path

    out = []
    for p in sorted(Path(out_dir).glob("mandate-*.html")):
        m = re.fullmatch(r"mandate-(\d{4}-\d{2})(\.provisional)?\.html", p.name)
        if m:
            out.append({"month": m.group(1), "final": m.group(2) is None})
    return out


def mtd_headline(periods: list) -> dict | None:
    """The MTD figure every surface's headline shows: period PnL with flows
    netted out, and how much of the book it covers. None when there is no MTD
    period or nothing in it could be established."""
    mtd = next((p for p in periods if p.kind == "MTD"), None)
    if mtd is None or not mtd.rows:
        return None
    t = mtd.totals()
    covered = sum((r.value_end for r in mtd.rows), start=type(t["pnl"])(0))
    book = covered + mtd.excluded_value()
    return {
        "pnl": t["pnl"],
        "fees_earned": t["fees_earned"],
        "fees_collected": t["fees_collected"],
        "covered_pct": covered / book * 100 if book else None,
        "excluded": len(mtd.excluded),
    }


def latest_mark_date(storage: Storage) -> date | None:
    """The date of the newest live snapshot — where every period ends."""
    dates = [r["snapshot_date"] for r in storage.latest_snapshot()]
    return date.fromisoformat(max(dates)) if dates else None


def gather_period(
    storage: Storage, start: date | None, end: date, kind: str = "custom"
) -> PeriodPnL:
    """One window's PnL, read out of storage. `start` None means inception."""
    from defi_tracker.analytics import period_pnl

    baseline = start.fromordinal(start.toordinal() - 1) if start is not None else None
    until = int(datetime.combine(end, time.max, tzinfo=UTC).timestamp())
    return period_pnl(
        start=start,
        end=end,
        kind=kind,
        baseline_rows=storage.snapshots_as_of(baseline) if baseline is not None else [],
        end_rows=storage.snapshots_as_of(end),
        event_summary=storage.position_event_summary(),
        events=storage.events_between(0, until),
        tx_cost_rows=storage.tx_cost_rows(),
        health_rows=storage.pipeline_health(),
    )


def gather_periods(storage: Storage, end: date | None = None) -> list[PeriodPnL]:
    """D / WTD / MTD / QTD / YTD / ITD, all ending on the newest mark."""
    from defi_tracker.analytics import PERIOD_KINDS, period_start

    end = end or latest_mark_date(storage)
    if end is None:
        return []
    return [gather_period(storage, period_start(k, end), end, k) for k in PERIOD_KINDS]
