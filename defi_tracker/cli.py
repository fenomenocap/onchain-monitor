"""
CLI entry point.

    defi-tracker init
    defi-tracker add-wallet 0xABC --label hot
    defi-tracker run [--report] [--verbose]
    defi-tracker mtd [--wallet 0x...] [--year 2026] [--month 5]
    defi-tracker periods [--by chain|pair|venue|position|wallet] [--period MTD] [--month 2026-08]
    defi-tracker mom [--wallet 0x...] [--months 6]
    defi-tracker report [--wallet 0x...]
    defi-tracker web [--out dashboard.html] [--open]
    defi-tracker serve [--port 8787] [--open]
    defi-tracker mandate-report [--month 2026-09] [--force]

Env vars (.env):
    GRAPH_API_KEY      The Graph gateway (required for Pancake adapter)
    TRACKER_DB         SQLite path (default: ./tracker.db)
    BSC_RPC_URL        BSC RPC endpoint
    SLACK_WEBHOOK_URL  Alert delivery (optional)
    LOG_LEVEL          Logging verbosity (default: WARNING; use --verbose for INFO)
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import UTC, datetime

_log = logging.getLogger(__name__)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()  # must run before adapter imports — they read env vars at import time

# Importing adapters triggers auto-registration for any that find their env vars.
import defi_tracker.adapters.machinex  # noqa: E402, F401
import defi_tracker.adapters.pancake_infinity  # noqa: E402, F401

# uniswap_v3 registers only for the chains in UNISWAP_V3_CHAINS (default: base),
# because probing all 5 configured chains adds minutes of subgraph retry latency
# to every run. It used to be disabled outright, which left a live Base position
# untracked and misreported as a closed loss. Set UNISWAP_V3_CHAINS="" to disable.
import defi_tracker.adapters.uniswap_v3  # noqa: E402, F401
from defi_tracker.core.endpoints import redact  # noqa: E402
from defi_tracker.core.runner import TrackerRunner  # noqa: E402
from defi_tracker.core.storage import Storage  # noqa: E402
from defi_tracker.report import unclaimed_known  # noqa: E402
from defi_tracker.serve import DEFAULT_PRICE_TTL  # noqa: E402
from defi_tracker.slack import SlackDelivery  # noqa: E402


class _RedactUrls(logging.Filter):
    """Masks URL paths in every log record (see core.endpoints.redact)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


# ── Formatting helpers ────────────────────────────────────────────────────


def _usd(v) -> str:
    if v is None:
        return "          —"
    return f"${v:>10,.2f}"


def _pct(v) -> str:
    if v is None:
        return "     —"
    return f"{v:>5.1f}%"


def _w(addr: str) -> str:
    return addr[:6] + "…" + addr[-4:]


def _sep(label: str = "", width: int = 72) -> None:
    dashes = "─" * max(0, width - len(label) - (5 if label else 2))
    if label:
        print(f"\n   ── {label} {dashes}")
    else:
        print(f"   {'─' * width}")


def _range_col(row: dict) -> str:
    tl = row.get("tick_lower")
    tu = row.get("tick_upper")
    tc = row.get("tick_current")
    if tl is None or tu is None or tc is None or tu == tl:
        return "     —"
    if not row.get("in_range"):
        return "   OUT"
    pct = min(tu - tc, tc - tl) / (tu - tl) * 100
    return f"IN{pct:3.0f}%"


def _age(ts: int) -> str:
    """Human-readable age: '2h ago', '3d ago', etc."""
    secs = int(datetime.now(UTC).timestamp()) - ts
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _duration(secs: float) -> str:
    """'45s' / '58m' / '1.0h' / '2.1d' — a gap, not an age. Scaled so an
    hourly cadence and a stalled one do not both read '0.0h'."""
    if secs < 90:
        return f"{secs:.0f}s"
    if secs < 5400:
        return f"{secs / 60:.0f}m"
    if secs < 172800:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def _trunc_mid(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    half = (max_len - 1) // 2
    return s[:half] + "…" + s[-(max_len - half - 1) :]


# ── Storage ───────────────────────────────────────────────────────────────


def _current_fee_prices(s: Storage) -> dict:
    """{token_key: current USD} for every token that appears in a COLLECT
    event, priced via the registered adapters (stablecoins → 1.0, others via
    CoinGecko + cache). Used to mark accumulated fees to today's prices."""
    import json as _json

    from defi_tracker.core.adapter import all_adapters
    from defi_tracker.core.types import Chain, Token

    meta: dict = {}
    with s.connect() as conn:
        for r in conn.execute("SELECT chain, address, symbol, decimals FROM tokens"):
            try:
                meta[f"{r['chain']}:{r['address']}"] = Token(
                    Chain(r["chain"]), r["address"], r["symbol"], r["decimals"]
                )
            except ValueError:
                continue  # unknown chain enum

    keys: set[str] = set()
    for r in s.monthly_collects():
        for a in _json.loads(r["amounts_json"]):
            keys.add(a["token_key"])

    adapters = all_adapters()
    prices: dict = {}
    for tk in keys:
        tok = meta.get(tk)
        if tok is None:
            continue
        for ad in adapters:
            if ad.handles(tok.chain):
                pp = ad.get_current_price(tok)
                if pp:
                    prices[tk] = pp.price_usd
                    break
    return prices


def _db() -> Storage:
    path = os.getenv("TRACKER_DB", "tracker.db")
    s = Storage(path)
    s.init_schema()
    return s


# ── Commands ───────────────────────────────────────────────────────────────


def cmd_init(args):
    s = _db()
    print(f"✅ DB initialized at {s.db_path} (schema v{s.schema_version()})")


def cmd_set_vault(args):
    """Record which Fireblocks vault holds a tracked address (schema v7)."""
    s = _db()
    vault = None if args.clear else args.vault_id
    if not args.clear and not vault:
        print("Give a vault id, or --clear.")
        return
    if s.set_wallet_vault(args.address, vault):
        print(f"✅ {args.address.lower()} → vault {vault}" if vault else "✅ Vault cleared.")
    else:
        print(f"No registered wallet {args.address.lower()} — add it with `add-wallet` first.")


def cmd_add_wallet(args):
    s = _db()
    s.add_wallet(args.address, args.label)
    print(f"✅ Wallet added: {args.address} ({args.label or 'unlabeled'})")
    print("   Configured wallets:")
    for w in s.list_wallets():
        print(f"     {w['address']}  {w['label'] or ''}")


def cmd_run(args):
    if getattr(args, "verbose", False):
        logging.getLogger().setLevel(logging.INFO)

    from defi_tracker.netwait import wait_for_network

    if not wait_for_network():
        print("⚠️  No network — pass skipped (the Mac likely woke before Wi-Fi did).")
        raise SystemExit(75)  # EX_TEMPFAIL
    s = _db()
    runner = TrackerRunner(s)
    print(f"⚙️  Running tracker at {datetime.now(UTC).isoformat()}")
    summary = runner.run(
        wallets=getattr(args, "wallets", None),
        protocols=getattr(args, "protocols", None),
    )
    print("\n📊 Summary")
    print(f"   wallets scanned       : {summary['wallets_scanned']}")
    print(f"   events inserted       : {summary['events_inserted']}")
    print(f"   events dropped        : {summary.get('events_dropped', 0)}")
    print(f"   positions snapshotted : {summary['positions_snapshotted']}")
    print(f"   alerts raised         : {summary['alerts_raised']}")
    for adapter, stats in summary["per_adapter"].items():
        print(f"   {adapter:<30} → {redact(str(stats))}")

    from defi_tracker.analytics import health_warnings

    # Loud, right after the run: a silent source outage is the failure mode
    # that lets every downstream number drift while looking fine.
    degraded = health_warnings(summary.get("health", []))
    if degraded:
        print("\n🩺 Source health")
        for line in degraded:
            print(f"   {line}")

    # Gas for the transactions this pass (or an earlier one) stored: without
    # it here the cost line only moves when someone remembers `backfill-gas`.
    # Idempotent and bounded; a failure leaves the rows unpriced for next time.
    from defi_tracker.costs import backfill_gas

    try:
        gas = backfill_gas(s, limit=50)
        if gas.considered:
            print(f"   gas                   : {gas}")
    except Exception as e:
        logging.warning("Gas backfill skipped: %s", redact(str(e)))

    gap_tokens: list[str] = summary.get("price_gap_tokens", [])
    if gap_tokens:
        print("\n⚠️  Prices missing — IL/PnL understated for:")
        for t in gap_tokens:
            print(f"     • {t}")

    webhook = os.getenv("SLACK_WEBHOOK_URL")
    if webhook:
        delivery = SlackDelivery(webhook)
        ok = delivery.push_report(s, price_today=_current_fee_prices(s))
        print(f"\n{'✅' if ok else '⚠️ '} Slack report {'delivered' if ok else 'delivery failed'}")
        delivered = delivery.push_undelivered(s)
        if delivered:
            logging.info("alerts delivered: %d", delivered)
    elif summary["alerts_raised"]:
        print(
            f"\nℹ️  SLACK_WEBHOOK_URL not set — {summary['alerts_raised']} alert(s) stored in DB but not delivered."
        )

    if getattr(args, "report", False):
        args.wallet = getattr(args, "wallet", None)
        args.year = None
        args.month = None
        args.months = 3
        args.limit = 20
        args.undelivered_only = False
        cmd_report(args)
        cmd_mtd(args)
        cmd_mom(args)
        cmd_alerts(args)


def cmd_health(args):
    """Pipeline liveness — when each (wallet, chain, protocol) last actually
    succeeded. A source that fails silently keeps every other view looking
    healthy, so this is the view that tells you whether to trust them."""
    from defi_tracker.analytics import health_warnings

    s = _db()
    rows = s.pipeline_health()
    if not rows:
        print("No pipeline health on record yet — run `defi-tracker run` first.")
        return

    def _ago(ts) -> str:
        return _age(ts) if ts else "never"

    _sep("Pipeline health")
    print(
        f"   {'Chain':<7}{'Protocol':<19}{'Wallet':<15}"
        f"{'Last run':>10}{'Sync ok':>10}{'Snapshot':>10}  {'Source':<14}"
    )
    _sep(width=90)
    for r in rows:
        print(
            f"   {r['chain']:<7}{r['protocol_id']:<19}{_w(r['wallet']):<15}"
            f"{_ago(r['last_run_at']):>10}{_ago(r['last_sync_ok_at']):>10}"
            f"{_ago(r['last_snapshot_ok_at']):>10}  {(r['source'] or '—'):<14}"
        )
        if r["last_error"]:
            print(f"        ↳ {_trunc_mid(r['last_error'], 84)}")
        dropped = int(r["dropped_events_last_run"] or 0) if "dropped_events_last_run" in r else 0
        if dropped:
            print(f"        ↳ {dropped} event(s) dropped on the last run — see run log")

    cov = s.mark_coverage()
    _sep("Marks")
    if not cov["marks"]:
        print("   No marks recorded yet — run `defi-tracker mark` (hourly) to start the clock.")
    else:
        span = cov["last_ts"] - cov["first_ts"]
        cadence = _duration(cov["median_gap_s"]) if cov["median_gap_s"] else "single pass"
        print(
            f"   {cov['marks']:,} marks over {cov['passes']:,} passes, "
            f"{cov['positions']} position(s), spanning {_duration(span)}"
        )
        print(f"   Observed cadence: {cadence} (median gap between passes)")
        print(f"   Last mark: {_age(cov['last_ts'])}")

    warnings = health_warnings(rows)
    if warnings:
        _sep("Degraded")
        for line in warnings:
            print(f"   {line}")
    else:
        print("\n   ✅ All sources current.")


def _period_line(name: str, t: dict) -> str:
    if not t["positions"]:
        # Nothing established is not "nothing earned": a window whose every
        # position was excluded has no PnL to show, not a PnL of zero.
        return f"   {name:<22} {0:>4}   — every position excluded (see below)"
    return (
        f"   {name:<22} {t['positions']:>4} {_usd(t['value_start'])} {_usd(t['net_flows'])} "
        f"{_usd(t['market'])} {_usd(t['il'])} {_usd(t['fees_earned'])} "
        f"{_usd(t['gas'])} {_usd(t['pnl'])}"
    )


def _print_exclusions(period) -> None:
    """Group a period's exclusions by reason — what the numbers above leave out."""
    if not period.excluded:
        return
    by_reason: dict[str, list] = {}
    for e in period.excluded:
        by_reason.setdefault(e.reason, []).append(e)
    total = float(period.excluded_value())
    print(
        f"\n   Excluded from {period.kind}: {len(period.excluded)} position(s), "
        f"{_usd(total).strip()} of value — not estimated:"
    )
    for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        value = sum(float(e.value_usd or 0) for e in items)
        chains = ", ".join(sorted({e.chain for e in items}))
        print(f"     {len(items):>3} × {reason}  [{chains}; {_usd(value).strip()}]")


def cmd_periods(args):
    """Period PnL with flows netted out: D / WTD / MTD / QTD / YTD / ITD, or a
    calendar month; optionally sliced by position, pair, chain, venue, wallet."""
    from datetime import date

    from defi_tracker.analytics import period_start
    from defi_tracker.report import gather_period, gather_periods, latest_mark_date

    st = _db()
    if args.month:
        y, m = (int(x) for x in args.month.split("-"))
        start = date(y, m, 1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        last = min(nxt.fromordinal(nxt.toordinal() - 1), latest_mark_date(st) or nxt)
        periods = [gather_period(st, start, last, args.month)]
    elif args.by:
        end = latest_mark_date(st)
        if end is None:
            print("No snapshots yet.")
            return
        kind = args.period or "MTD"
        periods = [gather_period(st, period_start(kind, end), end, kind)]
    else:
        periods = gather_periods(st)
    if not periods:
        print("No snapshots yet.")
        return

    head = (
        f"   {'':<22} {'Pos':>4} {'Start value':>11} {'Net flows':>11} {'Market':>11} "
        f"{'IL':>11} {'Fees earned':>11} {'Gas':>11} {'PnL':>11}"
    )
    if len(periods) > 1:
        _sep("Period PnL — flows netted out, by window")
        print(head)
        _sep(width=118)
        for p in periods:
            print(_period_line(p.kind, p.totals()))
        _sep(width=118)
        for p in periods:
            print(f"   {p.label}")
        for p in periods:
            _print_exclusions(p)
    else:
        p = periods[0]
        dim = args.by or "chain"
        _sep(f"{p.label} — by {dim}")
        print(head)
        _sep(width=118)
        for g in p.by(dim):
            print(_period_line(str(g["label"])[:22], g))
        _sep(width=118)
        t = p.totals()
        print(_period_line("Total", t))
        if t["undecomposed"]:
            print(
                f"\n   {_usd(t['undecomposed']).strip()} of price effect is not split into "
                "market vs IL: those positions' marks carry no hold value."
            )
        if t["net_after_gas"] is None and t["gas_txs"]:
            print(
                f"   Net after gas withheld: {t['gas_unpriced_txs']}/{t['gas_txs']} "
                "transactions have no gas price."
            )
        _print_exclusions(p)
    print(
        "\n   PnL = Δ(value + unclaimed) − net flows + fees collected = market + IL + fees earned."
    )


def cmd_limits(args):
    """Mandate policy limits and where the book stands against each."""
    from datetime import date

    from defi_tracker.analytics import counterparty_exposure, policy_checks
    from defi_tracker.policy import LIMITS, PolicyError, fetch_depeg_prices, load_policy

    try:
        policy = load_policy(args.policy)
    except PolicyError as e:
        print(f"🚨 Policy file invalid — no limit evaluated: {e}")
        return
    st = _db()
    today = date.today()
    exposure = counterparty_exposure(st.contracts_as_of(today), today)
    checks = policy_checks(st.latest_snapshot(), policy, fetch_depeg_prices(policy), exposure)
    _sep(f"Policy limits ({policy.source})")
    desc = {d.id: d.description for d in LIMITS}
    icon = {"ok": "✅", "breach": "🚨", "unknown": "❔", "unset": "·"}

    def fmt(v, unit) -> str:
        if v is None:
            return "—"
        return f"${v:,.0f}" if unit == "USD" else f"{v:,.1f}{'' if unit == '%' else ' '}{unit}"

    shown = checks if args.all else [c for c in checks if c.status != "ok"]
    for c in shown:
        what = desc.get(c.limit_id, c.limit_id)
        if c.status == "unset":
            print(f"   {icon[c.status]} {c.limit_id:<34} NOT SET   {what}")
            continue
        print(
            f"   {icon[c.status]} {c.limit_id:<34} {c.subject[:34]:<34} "
            f"{fmt(c.observed, c.unit):>12} / {fmt(c.threshold, c.unit):<10} {c.note}"
        )
    counts = {k: sum(1 for c in checks if c.status == k) for k in icon}
    print(
        f"\n   {counts['breach']} breach · {counts['unknown']} unknown · "
        f"{counts['ok']} ok · {counts['unset']} limit(s) not set"
        + ("" if args.all else "   (--all to list passing checks)")
    )


def _when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d") if ts else "—"


def _parse_day(value: str | None) -> int:
    """'YYYY-MM-DD' → unix seconds at 00:00 UTC; None → now."""
    if not value:
        return int(datetime.now(UTC).timestamp())
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def cmd_exceptions(args):
    """The policy exception log: list episodes, or record a review."""
    st = _db()
    now = int(datetime.now(UTC).timestamp())
    if args.review is not None:
        if not args.by:
            print("A review needs --by: whoever is accountable for it.")
            return
        ok = st.review_exception(
            args.review, by=args.by, disposition=args.disposition, note=args.note, now=now
        )
        print(f"Exception #{args.review} reviewed." if ok else f"No exception #{args.review}.")
        return
    rows = st.list_exceptions()
    if not args.all:
        rows = [r for r in rows if r["cleared_at"] is None or r["reviewed_at"] is None]
    _sep("Policy exceptions" + ("" if args.all else " — open or unreviewed"))
    if not rows:
        print("   None." + ("" if args.all else "  (--all for the full history)"))
        return
    for r in rows:
        state = "OPEN   " if r["cleared_at"] is None else f"cleared {_when(r['cleared_at'])}"
        review = (
            f"reviewed by {r['reviewed_by']} ({r['disposition']})"
            if r["reviewed_at"]
            else "⚠ UNREVIEWED"
        )
        print(
            f"   #{r['id']:<4} {_when(r['opened_at'])}  {state:<18} {r['limit_id']:<32} "
            f"{r['subject'][:30]:<30} worst {r['worst_observed']} {r['unit']} "
            f"(limit {r['threshold']})  {review}"
        )


def cmd_incident(args):
    """The incident log: add, update the lifecycle, or list."""
    st = _db()
    now = int(datetime.now(UTC).timestamp())
    if args.action == "add":
        if not args.title:
            print("An incident needs --title.")
            return
        iid = st.add_incident(
            title=args.title,
            severity=args.severity,
            detected_at=_parse_day(args.at),
            now=now,
            description=args.description,
            logged_by=args.by,
            escalated_to=args.escalated_to,
        )
        print(f"Incident #{iid} logged.")
        return
    if args.action == "update":
        if args.id is None:
            print("Which incident? Give --id.")
            return
        fields: dict = {}
        if args.escalated_to:
            fields["escalated_to"] = args.escalated_to
        if args.description:
            fields["description"] = args.description
        if args.contained:
            fields["contained_at"] = _parse_day(args.at)
        if args.postmortem:
            fields["postmortem"] = args.postmortem
        if args.resolved:
            current = st.get_incident(args.id)
            if current is None:
                print(f"No incident #{args.id}.")
                return
            # The policy requires a post-mortem; closing without one would let
            # an incident leave the report with its lesson unrecorded.
            if not (args.postmortem or current["postmortem"]):
                print("Resolving needs a --postmortem (a link or a summary).")
                return
            fields["resolved_at"] = _parse_day(args.at)
        ok = st.update_incident(args.id, **fields)
        print(
            f"Incident #{args.id} updated." if ok else f"No incident #{args.id}, or nothing to set."
        )
        return
    rows = st.list_incidents()
    _sep("Incidents")
    if not rows:
        print("   None logged.")
        return
    for r in rows:
        if r["resolved_at"]:
            stage = f"resolved {_when(r['resolved_at'])}"
        elif r["contained_at"]:
            stage = f"contained {_when(r['contained_at'])}"
        else:
            stage = "OPEN"
        print(
            f"   #{r['id']:<4} {_when(r['detected_at'])}  {r['severity']:<8} {stage:<20} "
            f"{r['title']}" + (f"  → {r['escalated_to']}" if r["escalated_to"] else "")
        )


def cmd_ledger(args):
    """The contractual ledger: MM agreements, loans/options, OTC placements."""
    from datetime import date

    from defi_tracker.core.storage import Storage as _S

    st = _db()
    now = int(datetime.now(UTC).timestamp())
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    if args.action in ("add", "update"):
        if not args.ref:
            print("Give the contract a --ref (a stable id you choose, e.g. mm-a-loan-2024).")
            return
        given = {
            "counterparty": args.counterparty,
            "kind": args.kind,
            "status": args.status,
            "asset": args.asset,
            "amount": args.amount,
            "usd_value": args.usd,
            "start_date": args.start,
            "end_date": args.expires,
            "terms": args.terms,
            "annual_cost_usd": args.annual_cost,
            "notice_by": args.notice_by,
            "interest_rate_pct": args.interest,
            "returned": args.returned,
            "source_note": args.source,
        }
        fields = {k: v for k, v in given.items() if v is not None}
        if args.action == "update":
            history = st.contract_history(args.ref)
            if not history:
                print(f"No contract {args.ref!r} — use `ledger add`.")
                return
            if not args.source:
                print("An update needs its own --source: where the new figures came from.")
                return
            latest = {k: history[-1][k] for k in _S.CONTRACT_FIELDS}
            fields = {**latest, **fields}
        elif st.contract_history(args.ref):
            print(f"{args.ref!r} exists — use `ledger update` to record a new version.")
            return
        else:
            fields.setdefault("status", "active")
        try:
            st.record_contract(args.ref, as_of, now, **fields)
        except ValueError as e:
            print(f"Not recorded: {e}")
            return
        print(f"Recorded {args.ref} as of {as_of.isoformat()}.")
        return
    if args.action == "tranche":
        if not (args.ref and args.tranche and args.source):
            print("A tranche needs --ref, --tranche and --source (the document and page).")
            return
        prior = next(
            (
                t
                for t in st.tranches_as_of()
                if t["ref"] == args.ref and t["tranche"] == str(args.tranche)
            ),
            None,
        )
        given = {
            "asset": args.asset,
            "amount": args.amount,
            "strike_usd": args.strike,
            "strike_rule": args.strike_rule,
            "strike_low_usd": args.strike_low,
            "strike_high_usd": args.strike_high,
            "exercisable_from": args.window_from,
            "exercisable_to": args.window_to,
            "status": args.tranche_status,
            "exercised_on": args.exercised_on,
            "exercise_price_usd": args.exercise_price,
            "source_note": args.source,
        }
        fields = {k: v for k, v in given.items() if v is not None}
        if prior:
            fields = {**{k: prior[k] for k in _S.TRANCHE_FIELDS}, **fields}
        fields.setdefault("status", "open")
        try:
            st.record_tranche(args.ref, args.tranche, as_of, now, **fields)
        except ValueError as e:
            print(f"Not recorded: {e}")
            return
        print(f"Recorded {args.ref} tranche {args.tranche} as of {as_of.isoformat()}.")
        return
    if args.action == "tranches":
        for t in st.tranches_as_of(as_of):
            if t["strike_usd"] is not None:
                strike = f"${t['strike_usd']}"
            elif t["strike_low_usd"] is not None and t["strike_high_usd"] is not None:
                strike = f"~${t['strike_low_usd']}-{t['strike_high_usd']}"
            else:
                strike = "strike unfixed"
            window = f"{t['exercisable_from'] or '?'} → {t['exercisable_to'] or '?'}"
            print(
                f"   {t['ref']:<22} #{t['tranche']:<3} {t['amount']:>14,} {t['asset']:<6} "
                f"{strike:<16} {t['status']:<10} {window}"
            )
            if t["strike_rule"]:
                print(f"   {'':<27} rule: {t['strike_rule']}")
            print(f"   {'':<27} as of {t['as_of']} · {t['source_note']}")
        return
    if args.action == "history":
        for r in st.contract_history(args.ref or ""):
            print(
                f"   {r['as_of']}  {r['status']:<10} usd={r['usd_value']}  "
                f"expires {r['end_date'] or '—'}  source: {r['source_note']}"
            )
        return
    rows = st.contracts_as_of(as_of)
    _sep(f"Contractual ledger as of {as_of.isoformat()}")
    if not rows:
        print("   Nothing keyed yet. `defi-tracker ledger add --ref … --counterparty … "
              "--kind … --source …`")  # fmt: skip
        return
    for r in rows:
        if r["end_date"]:
            days = (date.fromisoformat(r["end_date"]) - as_of).days
            expiry = f"{r['end_date']} ({days:+d}d)"
        else:
            expiry = "no expiry"
        usd = f"${r['usd_value']:,.0f}" if r["usd_value"] is not None else "USD unknown"
        amount = f"{r['amount']:,} {r['asset'] or ''}".strip() if r["amount"] is not None else ""
        print(
            f"   {r['ref']:<22} {r['counterparty']:<12} {r['kind']:<11} {r['status']:<10} "
            f"{expiry:<20} {usd:>14}  {amount}"
        )
        print(f"   {'':<22} as of {r['as_of']} · {r['source_note']}")


def cmd_mandate_report(args):
    """Write the monthly mandate report (mandate §2.3) as a durable HTML file."""
    from defi_tracker.mandate import (
        ReportExists,
        gather_mandate_report,
        report_path,
        write_mandate_report,
    )
    from defi_tracker.policy import PolicyError, fetch_depeg_prices, load_policy

    today = datetime.now(UTC).date()
    if args.month:
        y, m = (int(x) for x in args.month.split("-"))
    else:  # the last complete month
        y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    try:
        policy = load_policy(args.policy)
    except PolicyError as e:
        print(f"🚨 Policy file invalid — not writing a report against it: {e}")
        return
    rep = gather_mandate_report(_db(), y, m, policy, depeg_prices=fetch_depeg_prices(policy))
    if rep is None:
        print(f"No marks inside {y:04d}-{m:02d}; nothing to report.")
        return
    try:
        path = write_mandate_report(rep, args.out, force=args.force)
    except ReportExists:
        print(
            f"{report_path(rep, args.out)} already exists. It is the report as delivered; "
            "--force rewrites it (and restates the month)."
        )
        return
    kind = "Final" if rep.complete else f"PROVISIONAL (marks through {rep.end.isoformat()})"
    print(f"✅ {kind} mandate report for {rep.month} → {path}")
    t = rep.period.totals()
    print(
        f"   {len(rep.period.rows)} positions attributed, {len(rep.period.excluded)} excluded · "
        f"{sum(1 for c in rep.checks if c.status == 'breach')} breach(es) · "
        f"{sum(1 for c in rep.checks if c.status == 'unset')} limit(s) not set"
    )
    if rep.period.rows:
        print(f"   PnL {_usd(t['pnl']).strip()} · fees earned {_usd(t['fees_earned']).strip()}")


def cmd_risk(args):
    """Price-shock grid, delta/gamma, range-exit probability and breakeven fee
    APR per position, and the book under a PEAQ move (Phase 1.3–1.5)."""
    from defi_tracker.analytics import SHOCKS
    from defi_tracker.report import gather_risk

    st = _db()
    snaps = st.latest_snapshot()
    if not snaps:
        print("No snapshots yet.")
        return
    risk = gather_risk(st, snaps, horizon_days=args.horizon)
    peaq = risk["peaq"]
    _sep("Book under a PEAQ move (other tokens' USD prices held)")
    print("   " + "".join(f"{s:>+10.0%}" for s in SHOCKS))
    print("   " + "".join(f"{peaq['changes'][s] / 1000:>+9,.1f}k" for s in SHOCKS))
    print(f"   {peaq['positions']} PEAQ-exposed position(s) modelled"
          + (f", {peaq['unmodelled']} not modellable" if peaq["unmodelled"] else ""))  # fmt: skip

    _sep(f"Per position — horizon {args.horizon:.0f}d")
    print(
        f"   {'Pair':<20}{'Value':>10}{'Δ/1%':>8}{'Γ/1%²':>8}{'−20%':>10}{'IL@−20%':>9}"
        f"{'σ/day':>7}{'exit':>6}{'re-in':>6}{'breakeven APR':>18}"
    )
    rows = sorted(risk["positions"], key=lambda p: -p["row"]["current_value_usd"])
    for p in rows:
        row, r = p["row"], p["risk"]
        if r is None:
            print(f"   {row['pair_label'][:20]:<20}{_usd(row['current_value_usd']).strip():>10}"
                  "   — not modellable (ticks or decimals unknown)")  # fmt: skip
            continue

        def pct(v):
            return "—" if v is None else f"{v:.0%}"

        be = "—"
        if r["breakeven_apr"] is not None:
            band = r["breakeven_band"]
            be = f"{r['breakeven_apr']:.0f}%" + (
                f" ({band[0]:.0f}–{band[1]:.0f})" if band and r["breakeven_apr"] else ""
            )
        print(
            f"   {row['pair_label'][:20]:<20}{r['value']:>10,.0f}{r['delta_1pct']:>8,.0f}"
            f"{r['gamma_1pct']:>8,.1f}{r['shocks'][-0.2]['change']:>10,.0f}"
            f"{r['shocks'][-0.2]['vs_hold']:>9,.0f}"
            f"{(str(round(r['sigma_daily_pct'], 1)) + '%') if r['sigma_daily_pct'] else '—':>7}"
            f"{pct(r['exit_prob']):>6}{pct(r['reentry_prob']):>6}{be:>18}"
        )
    print(
        "\n   Δ/1%: $ change for a +1% move of the pool price; Γ: change in Δ per 1% — "
        "negative is short gamma.\n   exit: P(leaving the range within the horizon); re-in: "
        "P(an out-of-range position's price reaching its range).\n   breakeven APR: the fee "
        "rate that pays for the IL expected at σ (band: 95% on σ from its sample)."
    )


def cmd_elasticity(args):
    """Phase 5.3: ε, the response of pool fees to pool depth, measured from
    swap history with the book's own depth changes as the instrument."""
    from defi_tracker.core.adapter import all_adapters
    from defi_tracker.poolflow import history_source, sync_pool_history

    st = _db()
    if args.sync:
        for adapter in all_adapters():
            for chain in adapter.info.chains:
                kind = history_source(adapter, chain)
                if kind is None:
                    continue
                what = "scanning swaps" if kind == "swaps" else "reading the pools' fee counters"
                print(f"   {chain.value}/{adapter.info.protocol_id}: {what}", flush=True)
                res = sync_pool_history(
                    st, adapter, chain, progress=lambda m: print(f"      {m}", flush=True)
                )
                state = "at head" if res.reached_head else "stopped short of head"
                unit = "swaps" if kind == "swaps" else "days"
                print(
                    f"   {res.pools} pool(s), {res.swaps:,} {unit} in {res.windows} window(s), {state}"
                )
    _print_elasticity(st)


def _print_elasticity(st) -> None:
    from defi_tracker.analytics import estimate_volume_elasticity
    from defi_tracker.poolflow import pool_infos
    from defi_tracker.report import ELASTICITY_KEY

    rows = st.pool_days()
    if not rows:
        print("   No pool history yet — run with --sync.")
        return
    infos: dict = {}
    for chain in {r["chain"] for r in rows}:
        infos.update(pool_infos(st, chain))
    est = estimate_volume_elasticity(rows, infos)
    st.kv_set(ELASTICITY_KEY, est.to_json())
    _sep("Volume elasticity — pool fees ∝ depth^ε, from the pools' own history")
    print(
        f"   {est.n_obs} pool-weeks across {est.n_pools} pool(s), {est.from_day} → {est.to_day};"
        f" the book's own depth moved ≥20% in {est.book_moves} of them"
    )

    def show(label: str, v: float | None, ci: tuple[float, float] | None) -> None:
        if v is None:
            print(f"   {label:<34} unmeasured")
            return
        band = f"  (95% {ci[0]:.2f} … {ci[1]:.2f})" if ci else ""
        print(f"   {label:<34} {v:.2f}{band}")

    show("ε, OLS (depth chases flow: biased)", est.eps_ols, est.ci_ols)
    show("ε, book depth as instrument", est.eps_iv, est.ci_iv)
    if est.first_stage_f is not None:
        print(f"   first-stage F                      {est.first_stage_f:.1f}")
    chains = sorted({r["chain"] for r in rows})
    if len(chains) > 1:
        # Robustness: does any one chain carry the answer? A chain where the
        # book is the whole pool identifies ε only through the book's own
        # moves there — the fit says how much it adds.
        for chain in chains:
            rest = [r for r in rows if r["chain"] != chain]
            e = estimate_volume_elasticity(rest, infos)
            pool_weeks = est.n_obs - e.n_obs
            v = f"{e.eps_iv:.2f}" if e.eps_iv is not None else "unmeasured"
            band = f" (95% {e.ci_iv[0]:.2f} … {e.ci_iv[1]:.2f})" if e.ci_iv else ""
            f = f", F {e.first_stage_f:.1f}" if e.first_stage_f is not None else ""
            print(
                f"   without {chain:<5} ({pool_weeks} pool-weeks)     ε {v}{band}{f}"
                + ("" if e.usable else f" — {e.reason}")
            )
    if est.usable:
        from defi_tracker.analytics import DEFAULT_REBALANCE_CONFIG, measured_rebalance_config

        cfg = measured_rebalance_config(DEFAULT_REBALANCE_CONFIG, est)
        print(
            f"\n   The optimiser now scores at ε {cfg.volume_elasticity:.2f}, bracket "
            f"{cfg.elasticity_band[0]:.2f} … {cfg.elasticity_band[1]:.2f} (clipped to 0 … 1)."
        )
    else:
        print(f"\n   Not used: {est.reason}. The optimiser keeps its assumed ε.")


def cmd_backtest(args):
    """Phase 5.3: what the optimiser's bands would have realised, window by
    window, beside what the book realised and what the model predicted."""
    from collections import defaultdict
    from dataclasses import replace

    from defi_tracker.analytics import strategy_from_spec
    from defi_tracker.report import effective_rebalance_config, gather_backtest

    st = _db()
    config = replace(effective_rebalance_config(st), target_horizon_days=args.horizon)
    try:
        strategies = [strategy_from_spec(x) for x in (args.strategy or [config.strategy])]
    except ValueError as e:
        raise SystemExit(f"   {e}") from None
    if args.grid:
        if not args.split:
            raise SystemExit("   --grid chooses on history before a split: pass --split YYYY-MM-DD")
        _print_grid(st, config, args)
        return
    if args.split:
        _print_split(st, config, strategies, args)
        return
    if len(strategies) > 1:
        _sep(f"Strategies compared — {args.horizon}d windows every {args.step}d")
        print(
            f"   {'Strategy':<14}{'windows':>8}{'predicted':>12}{'realised':>12}{'hit':>6}"
            f"{'fees−IL':>12}"
        )
        for strat in strategies:
            b = gather_backtest(st, config, step_days=args.step, strategy=strat)
            hr = b.hit_rate
            print(
                f"   {strat.spec:<14}{len(b.windows):>8}{b.predicted_usd:>+12,.0f}"
                f"{b.realised_usd:>+12,.0f}{f'{hr:.0%}' if hr is not None else '—':>6}"
                f"{sum(w.policy_usd for w in b.windows):>+12,.0f}"
            )
        print("   (the same windows for every strategy; 'hold' is the book as it stood)")
        return
    bt = gather_backtest(st, config, step_days=args.step, strategy=strategies[0])
    _sep(
        f"Backtest — {bt.strategy} vs the book as it stood, {bt.horizon_days}d windows every "
        f"{args.step}d, ε {bt.elasticity:.2f}"
    )
    if not bt.windows:
        print("   No window could be replayed — run `defi-tracker elasticity --sync` first.")
        for pair, day, why in bt.skipped[:10]:
            print(f"   · {pair} {day}: {why}")
        return
    per: dict[str, list] = defaultdict(list)
    for w in bt.windows:
        per[w.pair].append(w)
    print(
        f"   {'Pool':<22}{'windows':>8}{'predicted':>12}{'realised':>12}{'hit':>6}"
        f"{'stay fees−IL':>15}{'policy fees−IL':>16}"
    )
    for pair, ws in sorted(per.items(), key=lambda kv: -sum(w.capital_usd for w in kv[1])):
        acted = [w for w in ws if w.predicted_gain_usd > 0]
        hit = (
            f"{sum(1 for w in acted if w.realised_gain_usd > 0) / len(acted):.0%}" if acted else "—"
        )
        print(
            f"   {pair:<22}{len(ws):>8}{sum(w.predicted_gain_usd for w in ws):>+12,.0f}"
            f"{sum(w.realised_gain_usd for w in ws):>+12,.0f}{hit:>6}"
            f"{sum(w.stay_usd for w in ws):>+15,.0f}{sum(w.policy_usd for w in ws):>+16,.0f}"
        )
    hr = bt.hit_rate
    print(
        f"\n   All: {len(bt.windows)} windows · predicted {bt.predicted_usd:+,.0f} · realised "
        f"{bt.realised_usd:+,.0f} · hit rate {f'{hr:.0%}' if hr is not None else '—'}"
    )
    by_chain: dict[str, list] = defaultdict(list)
    for w in bt.windows:
        by_chain[w.chain].append(w)
    if len(by_chain) > 1:
        sources = {r["chain"]: r["source"] for r in st.pool_days()}
        for chain, ws in sorted(by_chain.items()):
            acted = [w for w in ws if w.predicted_gain_usd > 0]
            chain_hit = (
                sum(1 for w in acted if w.realised_gain_usd > 0) / len(acted) if acted else None
            )
            src = "fee counter, 2 reads a day" if sources.get(chain) == "fee_counter" else "swaps"
            print(
                f"   {chain:<5} ({src}): {len(ws)} windows · predicted "
                f"{sum(w.predicted_gain_usd for w in ws):+,.0f} · realised "
                f"{sum(w.realised_gain_usd for w in ws):+,.0f} · hit rate "
                f"{f'{chain_hit:.0%}' if chain_hit is not None else '—'}"
            )
    print(
        "   Windows overlap when the step is shorter than the horizon, so sums count a day more"
        " than once —\n   compare predicted with realised, not either with the book's P&L. A day"
        " is in a band on its close;\n   both branches hold their liquidity fixed; no swap cost"
        " is charged."
    )
    if bt.skipped:
        skip_counts: dict[str, int] = defaultdict(int)
        for _, _, reason in bt.skipped:
            if "held" in reason:
                reason = "the book held too little there"
            elif "daily returns" in reason:
                reason = "too little σ history before it"
            skip_counts[reason] += 1
        print(
            f"   {len(bt.skipped)} window(s) skipped: "
            + "; ".join(f"{n}× {r}" for r, n in sorted(skip_counts.items(), key=lambda kv: -kv[1]))
        )


def _print_split(st, config, strategies, args) -> None:
    """Each strategy in sample (windows ending before the split) and out of
    sample (starting on or after it), against `hold` — whose realised gain
    is zero by construction, so 'beats hold' is a positive realised gain."""
    from defi_tracker.report import gather_backtest

    _sep(
        f"Out of sample — {args.horizon}d windows every {args.step}d, split {args.split} "
        "(windows straddling it are in neither)"
    )
    print(
        f"   {'Strategy':<34}{'in: n':>6}{'realised':>11}{'hit':>6}"
        f"{'out: n':>8}{'realised':>11}{'hit':>6}{'bsc':>10}{'peaq':>10}"
    )

    def hit(b) -> str:
        return f"{b.hit_rate:.0%}" if b.hit_rate is not None else "—"

    for strat in strategies:
        ins, outs = gather_backtest(st, config, step_days=args.step, strategy=strat).split(
            args.split
        )
        per = {
            c: sum(w.realised_gain_usd for w in outs.windows if w.chain == c)
            for c in ("bsc", "peaq")
        }
        print(
            f"   {strat.spec:<34}{len(ins.windows):>6}{ins.realised_usd:>+11,.0f}{hit(ins):>6}"
            f"{len(outs.windows):>8}{outs.realised_usd:>+11,.0f}{hit(outs):>6}"
            f"{per['bsc']:>+10,.0f}{per['peaq']:>+10,.0f}"
        )
    print(
        "   hold realises 0 by construction. Adopt a strategy only if it beats hold out of "
        "sample\n   with a hit rate ≥ 50% — and only on knobs chosen in sample."
    )


def _print_grid(st, config, args) -> None:
    """Choose the optimiser's knobs on history before the split, then score
    that one choice after it — the only honest way to read a grid."""
    from defi_tracker.analytics import adoptable, strategy_from_spec, variant_grid
    from defi_tracker.report import gather_backtest

    results = []
    for spec in variant_grid():
        ins, outs = gather_backtest(
            st, config, step_days=args.step, strategy=strategy_from_spec(spec)
        ).split(args.split)
        results.append((spec, ins, outs))
    # Ranked on in-sample realised gain alone. Variants within $500 of the
    # best are the same answer with knobs that do nothing: the fewest wins.
    results.sort(key=lambda r: -r[1].realised_usd)
    top = results[0][1].realised_usd
    results.sort(key=lambda r: (r[1].realised_usd < top - 500, r[0].count(",") + (":" in r[0]),
                                -r[1].realised_usd))  # fmt: skip
    _sep(
        f"Knobs chosen in sample — {len(results)} variants, split {args.split}, "
        f"{args.horizon}d windows every {args.step}d"
    )
    print(
        f"   {'Variant (ranked in sample)':<44}{'in':>10}{'hit':>6}{'out':>11}{'hit':>6}"
        f"{'bsc':>10}{'peaq':>10}"
    )

    def hit(b) -> str:
        return f"{b.hit_rate:.0%}" if b.hit_rate is not None else "—"

    for spec, ins, outs in results[:8]:
        per = {
            c: sum(w.realised_gain_usd for w in outs.windows if w.chain == c)
            for c in ("bsc", "peaq")
        }
        print(
            f"   {spec:<44}{ins.realised_usd:>+10,.0f}{hit(ins):>6}{outs.realised_usd:>+11,.0f}"
            f"{hit(outs):>6}{per['bsc']:>+10,.0f}{per['peaq']:>+10,.0f}"
        )
    spec, ins, outs = results[0]
    plain = next(r for r in results if r[0] == "optimiser")
    print(
        f"\n   Chosen in sample: {spec}. Plain optimiser: in {plain[1].realised_usd:+,.0f}, "
        f"out {plain[2].realised_usd:+,.0f}."
    )
    if ins.realised_usd <= 0:
        print("   No variant beats hold in sample — nothing to adopt.")
    elif adoptable(outs):
        print(
            f"   Out of sample it beats hold ({outs.realised_usd:+,.0f}, hit {hit(outs)}): "
            f"adoptable — set RebalanceConfig.strategy = {spec!r}."
        )
    else:
        print(
            f"   Out of sample it does not clear the bar ({outs.realised_usd:+,.0f}, hit "
            f"{hit(outs)}; needs > 0 and ≥ 50%): keep the queue advisory."
        )


def cmd_optimise(args):
    """Per pool, the band that maximises expected fees − expected IL over the
    horizon for the capital the book has there (Phase 5.1). Propose-only."""
    from dataclasses import replace

    from defi_tracker.analytics import band_pct_label
    from defi_tracker.report import effective_rebalance_config, gather_pool_plans

    st = _db()
    snaps = st.latest_snapshot()
    if not snaps:
        print("No snapshots yet.")
        return
    config = replace(effective_rebalance_config(st), target_horizon_days=args.horizon)
    plans = gather_pool_plans(st, snaps, config)
    e_lo, e_hi = config.elasticity_band[0], config.elasticity_band[-1]
    _sep(f"Range optimiser — horizon {args.horizon}d, volume held at the last 7 days'")
    for plan in sorted(plans, key=lambda p: -p.capital_usd):
        ps = plan.state
        chain, _, pair = ps.key
        head = f"   {pair} ({chain})"
        if plan.reason:
            print(f"{head} — {plan.reason}")
            continue
        assert plan.best is not None and plan.ev_band is not None
        sig = (ps.sigma or 0) * 100
        print(
            f"{head} — ${plan.capital_usd:,.0f} · fees ${ps.fees_per_day or 0:,.0f}/day · "
            f"σ {sig:.1f}%/day (n={ps.n_obs}) · book {ps.book_share or 0:.0%} of depth"
        )
        lo_pct, hi_pct = plan.half_pct or (0.0, 0.0)
        ev = plan.best.ev
        print(
            f"      best band {band_pct_label(lo_pct, hi_pct, plan.best.ticks)} "
            f"(ticks {plan.best.ticks[0]}…"
            f"{plan.best.ticks[1]}): fees ${ev.fees_usd:,.0f} − IL ${ev.il_usd:,.0f} = "
            f"${ev.ev_usd:+,.0f}  (σ band ${plan.ev_band[0]:+,.0f}…${plan.ev_band[1]:+,.0f}"
            + (
                f"; ε {e_lo:.2g} / {e_hi:.2g} ${plan.volume_band[0]:+,.0f} / "
                f"${plan.volume_band[1]:+,.0f})"
                if plan.volume_band
                else ")"
            )
        )
        widths = [
            f"{c.ticks[0]}…{c.ticks[1]}" for c in (plan.best_lo_sigma, plan.best_hi_sigma) if c
        ]
        if widths:
            print(f"      at σ's low / high end the best band is {' / '.join(widths)}")
        print(f"      as it sits today: ${plan.today_ev or 0:+,.0f}")
        if plan.optimal_capital_usd is not None:
            print(f"      capital that maximises EV in that band: ${plan.optimal_capital_usd:,.0f}")
        for n in plan.notes:
            print(f"      · {n}")
    lo_b, hi_b = config.depth_bounds
    print(
        "\n   EV = expected fees − expected IL vs holding, over the horizon, pool fees ∝ "
        f"depth^{config.volume_elasticity:.2g}.\n   Volume band: the same band at ε {e_lo:.2g} / "
        f"{e_hi:.2g}. Depth outside {lo_b:g}–{hi_b:g}× today's is not scored, except a close."
    )


def cmd_mtd(args):
    s = _db()
    rows = s.month_to_date_pnl(wallet=args.wallet, year=args.year, month=args.month)
    if not rows:
        print("No data for this period.")
        return
    from defi_tracker.analytics import fees_earned_mtd

    # il_change_mtd is NULL for positions with no event history (unknown
    # cost basis) — excluded from the total, rendered as "—" per row.
    # pnl_mtd_usd is None where unclaimed was unreadable at either end.
    unread = sum(1 for r in rows if r["pnl_mtd_usd"] is None)
    total_pnl = sum(r["pnl_mtd_usd"] or 0 for r in rows)
    total_fee = sum(r["fees_collected_mtd"] or 0 for r in rows)
    total_il = sum(r["il_change_mtd"] or 0 for r in rows)
    earned = float(fees_earned_mtd(rows))
    _sep("Month-to-Date change (flows not netted — `periods` nets them)")
    print(
        f"   Δ book + fees      : {_usd(total_pnl)}"
        + (f"   ({unread} position(s) with unread unclaimed left out)" if unread else "")
    )
    print(f"   Fees earned MTD    : {_usd(earned)}   (accrual: collected + Δunclaimed)")
    print(f"   Fees collected MTD : {_usd(total_fee)}   (cash: harvests dated this month)")
    print(f"   IL change MTD      : {_usd(total_il)}")
    print(
        f"\n   {'Wallet':<14} {'Chain':<10} {'Pair':<24} {'ΔValue':>11} {'Fees':>11} {'ΔIL':>11} {'Δ+fees':>11} {'In?'}"
    )
    _sep(width=100)
    for r in rows:
        w_short = _w(r["wallet"])
        in_r = "✅" if r.get("in_range") else ("🚨" if r.get("in_range") is not None else " —")
        print(
            f"   {w_short:<14} {r['chain']:<10} {r['pair_label']:<24} "
            f"{_usd(r['delta_value_usd'])} {_usd(r['fees_collected_mtd'])} "
            f"{_usd(r['il_change_mtd'])} {_usd(r['pnl_mtd_usd'])} {in_r}"
        )
    _sep(width=100)
    print(
        f"   {'Total':<50} {_usd(sum(r['delta_value_usd'] for r in rows))} "
        f"{_usd(total_fee)} {_usd(total_il)} {_usd(total_pnl)}"
    )


def cmd_mom(args):
    s = _db()
    rows = s.month_over_month(wallet=args.wallet, months=args.months)
    if not rows:
        print("No data for this period.")
        return
    _sep(f"Month-over-Month ({args.months} months)")
    print(
        f"   {'Month':<8} {'Wallet':<14} {'Chain':<10} {'Protocol':<22} "
        f"{'EOM Value':>11} {'Fees':>11} {'ΔvsPrev':>11}"
    )
    _sep(width=95)
    for r in rows:
        w_short = _w(r["wallet"])
        delta = (
            _usd(r["delta_vs_prev_month"])
            if r["delta_vs_prev_month"] is not None
            else "          —"
        )
        print(
            f"   {r['month']:<8} {w_short:<14} {r['chain']:<10} "
            f"{r['protocol_id']:<22} {_usd(r['end_value_usd'])} "
            f"{_usd(r['fees_in_month'])} {delta}"
        )


def cmd_alerts(args):
    s = _db()
    alerts = s.list_alerts(limit=args.limit, undelivered_only=args.undelivered_only)
    if not alerts:
        print("No alerts on record.")
        return
    _sev_emoji = {"high": "🚨", "medium": "⚠️ ", "low": "ℹ️ "}
    _sep(f"Alerts (showing up to {args.limit})")
    print(f"   {'When':<10} {'Sev':<4} {'Kind':<18} {'Position':<36} {'Message':<42} {'Del'}")
    _sep(width=120)
    undelivered = [a for a in alerts if not a["delivered_at"]]
    delivered = [a for a in alerts if a["delivered_at"]]
    sections = [("Undelivered", undelivered), ("Delivered", delivered)]
    for section_label, section_rows in sections:
        if not section_rows:
            continue
        if delivered and undelivered:
            print(f"\n   {section_label}:")
        for a in section_rows:
            age = _age(a["triggered_at"])
            emoji = _sev_emoji.get(a["severity"], "•  ")
            pos = a["position_uid"]
            pos_short = _trunc_mid(pos, 36)
            msg_short = _trunc_mid(a["message"], 42)
            del_mark = "✅" if a["delivered_at"] else "—"
            print(
                f"   {age:<10} {emoji:<4} {a['kind']:<18} {pos_short:<36}"
                f" {msg_short:<42} {del_mark}"
            )


def cmd_rebalance(args):
    """Rebalance action queue: list live items (🔴/🟠) with suggested targets,
    or mark one done / snooze it. Reconciles against current signals first, so
    completed rebalances auto-drop and the view is always fresh."""
    import json as _json

    s = _db()

    if args.done is not None:
        ok = s.resolve_rebalance_item(args.done, reason="manual")
        print(f"Item {args.done} marked done." if ok else f"No live queue item #{args.done}.")
        return
    if args.snooze is not None:
        until = int(datetime.now(UTC).timestamp()) + args.days * 86400
        ok = s.snooze_rebalance_item(args.snooze, until)
        if ok:
            when = datetime.fromtimestamp(until, UTC).date().isoformat()
            print(f"Item {args.snooze} snoozed for {args.days}d (until {when}).")
        else:
            print(f"No live queue item #{args.snooze}.")
        return

    # List: refresh the queue against today's tickets, then show it.
    from defi_tracker.analytics import describe_ticket

    queue = TrackerRunner(s).compute_action_queue()
    s.reconcile_rebalance_queue([t.queue_item() for t in queue.tickets])
    rows = s.list_rebalance_queue(include_resolved=args.show_all)
    if not rows:
        print("Action queue is empty — no action pays over the horizon.")
        if queue.unscored:
            print(f"   {len(queue.unscored)} position(s) could not be scored — see `optimise`.")
        return

    now = int(datetime.now(UTC).timestamp())
    icon = {"now": "🔴", "soon": "🟠"}
    _sep(f"Action queue — EV over {queue.horizon_days:.0f}d, propose-only")
    from defi_tracker.analytics import backtest_verdict
    from defi_tracker.report import gather_backtest

    try:
        bt = gather_backtest(s)
    except Exception:
        bt = None
    print(f"   {backtest_verdict(bt, {t.chain for t in queue.tickets})}.\n")
    print(
        f"   {'ID':>3}  {'':<2}{'Pair':<22}{'Action':<22}{'EV':>9}{'σ band':>18}{'Age':>5}  Status"
    )
    _sep(width=104)
    for r in rows:
        ctx = _json.loads(r["context_json"] or "{}")
        age = f"{max(1, (now - r['first_seen']) // 86400)}d"
        if r["status"] == "resolved":
            status = f"✓ {r['resolved_reason']}"
        elif r["status"] == "snoozed":
            wk = datetime.fromtimestamp(r["snooze_until"], UTC).date().isoformat()
            status = f"💤 until {wk}"
        else:
            status = "open"
        # Episodes from before 5.2 carry the old tier-heuristic context.
        score = ctx.get("score_usd")
        band = ctx.get("gain_band") or []
        ev = _fmt_short(score) if score is not None else "—"
        bands = f"{_fmt_short(band[0])}…{_fmt_short(band[1])}" if len(band) == 2 else "—"
        print(
            f"   {r['id']:>3}  {icon.get(r['tier'], '•'):<2}{r['pair_label'][:22]:<22}"
            f"{str(ctx.get('action') or 'rebalance (pre-5.2)')[:22]:<22}{ev:>9}{bands:>18}"
            f"{age:>5}  {status}"
        )
    live = {r["position_uid"]: r["id"] for r in rows if r["status"] != "resolved"}
    for t in queue.tickets:
        if t.uid not in live:
            continue
        head, steps, case = describe_ticket(t)
        print(f"\n   #{live[t.uid]} {icon.get(t.tier, '•')} {head}")
        for line in steps:
            print(f"      {line}")
        for line in case:
            print(f"      · {line}")
    print(
        "\n   🔴 positive across σ's band with every cost known · 🟠 pays at the central σ, "
        "or waits on a swap quote\n   `rebalance --done <ID>` once executed from the multisig · "
        "`--snooze <ID> --days N` to defer"
    )


def _fmt_short(v: float) -> str:
    v = float(v or 0)
    return f"${v / 1000:.1f}k" if abs(v) >= 1000 else f"${v:.0f}"


def cmd_report(args):
    s = _db()
    snaps = s.latest_snapshot(wallet=args.wallet)
    if not snaps:
        print("No snapshots yet. Run `defi-tracker run` first.")
        return
    total_value = sum(r["current_value_usd"] for r in snaps)
    unc_known = [r for r in snaps if unclaimed_known(r)]
    total_unc = sum(r["unclaimed_usd"] for r in unc_known)
    unc_unknown = len(snaps) - len(unc_known)
    total_il = sum(r["il_usd"] or 0 for r in snaps)
    total_pnl = sum(r["pnl_usd"] or 0 for r in snaps)

    _sep("Portfolio Snapshot")
    print(f"   LP Value     : {_usd(total_value)}")
    unc_note = f"   (excludes {unc_unknown} unreadable)" if unc_unknown else ""
    print(f"   Unclaimed    : {_usd(total_unc)}{unc_note}")
    print(f"   IL vs HODL   : {_usd(total_il)}")
    print(f"   Total PnL    : {_usd(total_pnl)}")
    print(f"   Positions    : {len(snaps)}")

    # Group by chain
    from itertools import groupby

    snaps_sorted = sorted(snaps, key=lambda r: (r["chain"], r["pair_label"]))
    col_h = f"   {'Pair':<26} {'Wallet':<14} {'Value':>11} {'Uncl':>11} {'IL':>11} {'PnL':>11} {'Range':>6}  In?"
    for chain, group in groupby(snaps_sorted, key=lambda r: r["chain"]):
        group_rows = list(group)
        _sep(chain)
        print(col_h)
        chain_value = chain_unc = chain_il = chain_pnl = 0.0
        for r in group_rows:
            in_r = "✅" if r.get("in_range") else "🚨"
            rng = _range_col(r)
            chain_value += r["current_value_usd"]
            chain_unc += (r["unclaimed_usd"] or 0) if unclaimed_known(r) else 0
            chain_il += r["il_usd"] or 0
            chain_pnl += r["pnl_usd"] or 0
            print(
                f"   {r['pair_label']:<26} {_w(r['wallet']):<14} "
                f"{_usd(r['current_value_usd'])} "
                f"{_usd(r['unclaimed_usd'] if unclaimed_known(r) else None)} "
                f"{_usd(r['il_usd'])} {_usd(r['pnl_usd'])} {rng}  {in_r}"
            )
        _sep(width=95)
        print(
            f"   {'Subtotal':<40} "
            f"{_usd(chain_value)} {_usd(chain_unc)} {_usd(chain_il)} {_usd(chain_pnl)}"
        )

    _sep("Total", width=95)
    print(
        f"   {'All chains':<40} "
        f"{_usd(total_value)} {_usd(total_unc)} {_usd(total_il)} {_usd(total_pnl)}"
    )
    unknown = sum(1 for r in snaps if r["il_usd"] is None and r["pnl_usd"] is None)
    if unknown:
        print(f"   ⚠️  IL/PnL totals exclude {unknown} position(s) with no event history")

    from defi_tracker.analytics import health_warnings
    from defi_tracker.slack import SlackDelivery

    # Source health first: a fallback can keep snapshot dates current while the
    # underlying event feed is dead, so this is the check that actually catches
    # a silent outage. _stale_chains still covers a chain that stopped
    # snapshotting outright.
    for line in health_warnings(s.pipeline_health()):
        print(f"   {line}")
    for chain_name, as_of, age_days in SlackDelivery._stale_chains(snaps):
        print(
            f"   ⚠️  {chain_name} marks are {age_days}d old (as of {as_of} — "
            f"data source outage; {chain_name} values are frozen there)"
        )

    # ── Where the PnL comes from ──────────────────────────────────────────
    from defi_tracker.analytics import pnl_decomposition, realized_closed_pnl, risk_summary

    d = pnl_decomposition(snaps, s.tx_cost_rows())
    closed = realized_closed_pnl(s.position_flows())
    book_net = float(d["net"]) + float(closed["realized_usd"])
    _sep("PnL decomposition")
    print(f"   Open · market move (HODL vs deposit) : {_usd(d['market_move'])}")
    print(f"   Open · LP vs HODL (IL)               : {_usd(d['il'])}")
    print(f"   Open · fees collected                : {_usd(d['fees_lifetime'])}")
    print(f"   Open · unclaimed fees                : {_usd(d['unclaimed'])}")
    print(f"   Open subtotal                        : {_usd(d['net'])}")
    print(
        f"   Closed · realized ({closed['positions']} positions)     : "
        f"{_usd(closed['realized_usd'])}"
    )
    print(f"   = Book net PnL                       : {_usd(book_net)}")
    costs = d["costs"]
    if costs and costs["txs"]:
        cover = f"{costs['gas_known_txs']}/{costs['txs']} txs priced"
        print(f"   Gas paid (every tx on record)        : {_usd(d['gas'])}   ({cover})")
        if d["net_after_costs"] is not None:
            print(f"   Open subtotal after costs            : {_usd(d['net_after_costs'])}")
    # The verdict on the LPing itself. Market move is token exposure you would
    # have had holding the same tokens, so it is deliberately NOT a component
    # of this — the two are alternatives, not parent and child.
    lp_vs_hodl = float(d["il"]) + float(d["fees_lifetime"]) + float(d["unclaimed"])
    excluded_note = f"{closed['positions']} closed"
    if d["excluded"]:
        excluded_note += f", {d['excluded']} with no event history"
    print(f"\n   LP vs just holding (open positions)  : {_usd(lp_vs_hodl)}")
    print("     = IL + fees + unclaimed. Separately, holding those tokens")
    print(f"     would itself have returned {_usd(d['market_move']).strip()}.")
    print(f"     Open positions only — excludes {excluded_note}.")
    if closed["unverified_positions"]:
        print(
            f"\n   ⚠️  {closed['unverified_positions']} closed position(s) have no withdrawal "
            f"on record ({_usd(float(closed['unverified_usd']))} deposited) — "
            "excluded from realized PnL, they are missing data, not losses:"
        )
        for u in closed["unverified"]:
            print(
                f"        {u['pair_label'][:28]:<28} {_usd(float(u['net_invested_usd']))}  "
                f"{_w(u['wallet'])}"
            )

    r = risk_summary(snaps)
    _sep("Risk")
    print(
        f"   Idle (out-of-range) capital   : {_usd(r['out_of_range_value'])} "
        f"({r['out_of_range_pct']:.1f}% of LP value, {r['out_of_range_count']} positions)"
    )
    print(
        f"   Largest single position       : {r['largest_position_label']} "
        f"{_usd(r['largest_position_value'])}"
    )
    print(
        f"   Top pair concentration        : {r['top_pair']} "
        f"{_usd(r['top_pair_value'])} ({r['top_pair_pct']:.1f}%)"
    )


def cmd_fees(args):
    """Monthly fee income trend, straight from COLLECT events (snapshot-independent)."""
    from defi_tracker.analytics import (
        attribute_fee_months,
        fee_netting_split,
        fee_table,
        fee_valuation,
        fees_by_pair,
    )

    s = _db()
    rows = s.monthly_collects(wallet=args.wallet)
    if not rows:
        print("No collected fees on record.")
        return

    # Default to the fee-accrual month: a start-of-month harvest is the PRIOR
    # month's fees. --by-claim-date keeps the raw claim-tx month.
    by_accrual = not getattr(args, "by_claim_date", False)
    if by_accrual:
        rows = attribute_fee_months(rows)

    # Two valuation bases: at-harvest (claim-day price) and marked to today.
    price_today = _current_fee_prices(s)
    val = fee_valuation(rows, price_today)
    split = fee_netting_split(rows)
    # Snapshots power the live "accruing" row and the open-capital column below,
    # and the open-position count tells sweep detection what "the whole book"
    # means — without it a two-position book can never look like a harvest.
    snaps = s.latest_snapshot(wallet=args.wallet)

    table = fee_table(
        s.monthly_collects(wallet=args.wallet),
        snaps=snaps,
        unclaimed_series=s.unclaimed_series(),
        health_rows=s.pipeline_health(),
        active=s.active_wallet_chains(),
        price_today=price_today,
        by_accrual=by_accrual,
    )
    chain_label = {"peaq": "MachineX", "bsc": "PancakeSwap", "base": "Uniswap V3"}
    ordered = table.chains
    labels = [chain_label.get(c, c) for c in ordered]
    wm, wc, wt = 9, 13, 15  # month / chain / total column widths

    def _cell(row, c) -> str:
        if c in row.unknown:
            return "?"
        v = row.cells.get(c, 0)
        if not v:
            return "—"
        return f"${v:,.0f}" + ("+" if c in row.partial else "")

    def _print_row(row, label=None) -> None:
        body = "".join(f"{_cell(row, c):>{wc}}" for c in ordered)
        if row.status == "unswept":
            tot, mtm = "unswept†", "—"
        elif row.status == "rolled":
            tot, mtm = "in later ‡", "—"
        else:
            tot = f"${row.claimed:,.0f}" + ("+" if row.partial or row.unknown else "")
            mtm = f"${row.mtm:,.0f}" if row.mtm is not None else "—"
        mark = {"split": "§", "accruing": "*"}.get(row.status, "")
        print(f"   {(label or row.label) + mark:<{wm}}{body}{tot:>{wt}}{mtm:>{wt}}")

    basis = "fee-accrual month" if by_accrual else "claim-tx month"
    _sep(f"Fees claimed by month ({basis}) — at claim price vs marked-to-market")
    header = (
        f"   {'Month':<{wm}}"
        + "".join(f"{lab:>{wc}}" for lab in labels)
        + f"{'Total claimed':>{wt}}{'Claimed (MtM)':>{wt}}"
    )
    print(header)
    _sep(width=len(header) - 3)
    shown = table.rows[-args.months :] if args.months else table.rows
    for row in shown:
        _print_row(row)
    if table.accruing:
        _print_row(table.accruing)
    _sep(width=len(header) - 3)
    _print_row(table.ytd)
    _print_row(table.lifetime)
    print()
    if table.accruing:
        print(f"   * accruing — {table.accruing.note}; marked-to-market, not yet claimed.")
    for row in shown:
        if row.status == "split":
            print(f"   § {row.label}: {row.note}.")
        elif row.status == "rolled":
            print(f"   ‡ {row.label}: {row.note}.")
    if any(r.status == "unswept" for r in shown):
        print("   † unswept — accrued but not yet harvested; the fees are in the accruing row.")
    for note in table.notes:
        print(f"   ? {note}.")
    if split["withdrawal_total"] > 0:
        print(
            f"   'Total claimed' incl. ${float(split['withdrawal_total']):,.0f} lifetime "
            "fee-on-withdrawal (fees released on liquidity removal)."
        )
    if val["by_token_drop"]:
        sym, tc, tt = val["by_token_drop"][0]
        drop = (1 - float(tt) / float(tc)) * 100 if tc else 0
        print(
            f"   Biggest MtM erosion: {sym}  "
            f"${float(tc):,.0f} at claim → ${float(tt):,.0f} MtM  (−{drop:.0f}%)"
        )
    if by_accrual:
        print(
            "   Fee-accrual month: a start-of-month sweep is the prior month's "
            "fees (--by-claim-date for raw claim dates)."
        )

    # Open capital per pair — matches event-symbol labels against snapshot
    # pair labels (which carry a fee-tier suffix) by prefix.
    symbols = s.token_symbol_map()
    value_by_label: dict[str, float] = {}
    for snap in snaps:
        value_by_label[snap["pair_label"]] = (
            value_by_label.get(snap["pair_label"], 0.0) + snap["current_value_usd"]
        )

    def _open_value(pair: str) -> float | None:
        vals = [v for lbl, v in value_by_label.items() if lbl.startswith(pair)]
        return sum(vals) if vals else None

    _sep(f"By pair (lifetime · last {args.recent} months · open capital · eff. APR)")
    print(
        f"   {'Pair':<24} {'Chain':<6} {'Collects':>8} {'Lifetime':>11} "
        f"{'Recent':>11} {'Open Val':>11} {'~APR':>6}"
    )
    _sep(width=85)
    for pair_row in fees_by_pair(rows, recent_months=args.recent, symbols_by_key=symbols):
        open_val = _open_value(pair_row["pair"])
        if open_val and open_val > 0:
            apr = float(pair_row["recent"]) / args.recent * 12 / open_val * 100
            val_s, apr_s = f"${open_val:,.0f}", f"{apr:.0f}%"
        else:
            val_s, apr_s = "closed", "—"
        print(
            f"   {pair_row['pair'][:24]:<24} {pair_row['chain']:<6} {pair_row['collects']:>8} "
            f"{'$' + format(float(pair_row['lifetime']), ',.0f'):>11} "
            f"{'$' + format(float(pair_row['recent']), ',.0f'):>11} "
            f"{val_s:>11} {apr_s:>6}"
        )

    # Month × pair detail matrix
    from defi_tracker.analytics import monthly_pair_fee_matrix

    d_months, d_pairs, d_cells = monthly_pair_fee_matrix(
        rows, symbols_by_key=symbols, months=min(args.months or 6, 6)
    )
    if d_months and d_pairs:
        _sep("Fees by month × pair")
        header = f"   {'Pair':<24}" + "".join(f"{m[2:]:>10}" for m in d_months)
        print(header)
        _sep(width=len(header) - 3)
        for p in d_pairs:
            cells = [float(d_cells.get((m, p), 0)) for m in d_months]
            print(
                f"   {p[:24]:<24}"
                + "".join(f"{('$' + format(v, ',.0f')) if v else '—':>10}" for v in cells)
            )


def cmd_dashboard(args):
    """Full portfolio view — report + MTD + MoM + alerts. Does NOT re-sync."""
    args.wallet = getattr(args, "wallet", None)
    args.year = None
    args.month = None
    args.months = 3
    args.limit = 20
    args.undelivered_only = False
    cmd_report(args)
    cmd_mtd(args)
    cmd_mom(args)
    cmd_alerts(args)


def _market_reads() -> tuple[list[dict], dict]:
    """CoinGecko venues and the exchanges' PEAQ order books — the network
    reads `web` and `serve` both skip under --no-prices."""
    from defi_tracker.orderbooks import fetch_orderbooks
    from defi_tracker.venues import fetch_venues

    return fetch_venues(), fetch_orderbooks()


def cmd_web(args):
    """Render the desk view as a self-contained HTML file."""
    import webbrowser
    from pathlib import Path

    from defi_tracker.coinwatch import fetch_delivery
    from defi_tracker.report import gather_report_data
    from defi_tracker.web import render_dashboard

    st = _db()
    price_today = {} if args.no_prices else _current_fee_prices(st)
    venues, books = ([], {}) if args.no_prices else _market_reads()
    data = gather_report_data(st, price_today, venues, fetch_delivery(), books)
    if data is None:
        print("No snapshots yet. Run `defi-tracker run` first.")
        return

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_dashboard(data, title=args.title), encoding="utf-8")
    print(f"Wrote {out}  ({out.stat().st_size / 1024:.0f} KB, {len(data.snaps)} positions)")
    if args.open:
        webbrowser.open(out.as_uri())


def cmd_rpc_check(args):
    """Probe each configured RPC endpoint: reachable, right chain, archive?"""
    from defi_tracker.core.types import Chain
    from defi_tracker.rpccheck import CHAIN_ENV, check_all

    chains = [Chain(c) for c in args.chains] if args.chains else None
    reports = check_all(chains)

    _sep("RPC endpoints")
    for r in reports:
        if not r.configured:
            print(f"   {r.chain.value:<9} —  {r.env_var} not set (public endpoint in use)")
        else:
            state = "✅" if r.ok else "🚨"
            archive = (
                "archive" if r.archive else ("pruned" if r.archive is False else "archive unknown")
            )
            via = "via ALCHEMY_API_KEY" if r.source == "alchemy" else f"via {r.source[4:]}"
            print(
                f"   {r.chain.value:<9} {state}  {r.url_host}  "
                f"chain id {r.chain_id}  block {r.block_number:,}  {archive}  ({via})"
                if r.reachable and r.block_number
                else f"   {r.chain.value:<9} {state}  {r.url_host}  ({via})"
            )
        for note in r.notes:
            print(f"        ↳ {note}")

    unset = [r for r in reports if not r.configured]
    if unset:
        print(
            "\n   Public endpoints are rate-limited and unaccountable. Set ALCHEMY_API_KEY "
            "in .env (covers bsc and base), or "
            + ", ".join(CHAIN_ENV[r.chain] for r in unset)
            + " to a private node."
        )


def _xyk_changed_windows(adapter, wallet: str, pools: dict, start: int, stop: int, step=20_000):
    """Intervals of `step` blocks across which the wallet's LP balance in any
    of `pools` changed, found by sampling balanceOf at archive blocks — a few
    hundred cheap reads instead of scanning millions of blocks of logs.

    Blind spot, stated: a deposit and a withdrawal of exactly equal LP inside
    one interval leave both ends equal and are not seen. That nets to ~zero
    flow, and the regular scan covers everything after its cursor in full;
    --exhaustive scans every block when that is not good enough."""
    from concurrent.futures import ThreadPoolExecutor

    rpc = adapter._rpc  # noqa: SLF001
    sel = adapter._SEL_BALANCE_OF + bytes(12) + bytes.fromhex(wallet[2:])  # noqa: SLF001
    blocks = [*range(start, stop, step), stop]

    def balances(b: int) -> tuple:
        out = []
        for pool in pools:
            # The raw call keeps "no contract yet" ("0x" — before the pair was
            # created, a balance of zero) apart from a failed read (raises).
            res = rpc.call("eth_call", [{"to": pool, "data": "0x" + sel.hex()}, hex(b)])
            out.append(int(res, 16) if res and res != "0x" else 0)
        return tuple(out)

    with ThreadPoolExecutor(max_workers=8) as ex:
        seen = list(ex.map(balances, blocks))
    return [
        (a + 1 if i else a, b)
        for i, (a, b, x, y) in enumerate(zip(blocks, blocks[1:], seen, seen[1:], strict=False))
        if x != y
    ]


def cmd_backfill_xyk(args):
    """Recover the XYK positions' history from LP-token transfers on peaq.

    The regular scan covers these going forward; this walks the range before
    its cursor, once. Resumable: progress is banked per window in kv_store,
    and events are idempotent by uid, so an interrupted run just re-runs."""
    from defi_tracker.adapters.machinex import DEPLOY_BLOCK, MachineXAdapter
    from defi_tracker.core.adapter import all_adapters
    from defi_tracker.core.types import Chain

    st = _db()
    adapter = next((a for a in all_adapters() if isinstance(a, MachineXAdapter)), None)
    if adapter is None or adapter._rpc is None:  # noqa: SLF001
        print("MachineX needs PEAQ_RPC_URL (an archive node) for this backfill.")
        return
    from concurrent.futures import ThreadPoolExecutor

    window = 50_000
    for w in st.list_wallets():
        wallet = w["address"]
        pools = adapter.legacy_pools(wallet, Chain.PEAQ)
        if not pools:
            continue
        stop = adapter._get_scan_cursor(wallet, Chain.PEAQ)  # noqa: SLF001
        key = f"xyk_backfill_cursor:{wallet}"
        start = int(st.kv_get(key) or args.from_block or DEPLOY_BLOCK)
        print(f"{wallet[:10]}… {len(pools)} XYK pool(s), blocks {start:,} → {stop:,}", flush=True)
        windows = [(a, min(a + window - 1, stop - 1)) for a in range(start, stop, window)]
        if not args.exhaustive:
            windows = _xyk_changed_windows(adapter, wallet, pools, start, stop)
            print(f"   LP balance changed in {len(windows)} sampled interval(s)", flush=True)
        found = 0
        done: dict[int, list] = {}
        banked = 0  # windows[:banked] are persisted and the cursor is past them
        # Windows run concurrently (old peaq ranges are slow to query), but the
        # cursor only ever advances over a CONTIGUOUS completed prefix, so an
        # interrupted run can never skip a range.
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(adapter.scan_legacy_events, wallet, Chain.PEAQ, a, b): i
                for i, (a, b) in enumerate(windows)
            }
            for fut, i in futures.items():
                done[i] = fut.result()
                while banked in done:
                    found += st.upsert_events(done.pop(banked))
                    st.kv_set(key, str(windows[banked][1] + 1))
                    banked += 1
                print(f"   through {windows[banked - 1][1]:,}: {found} new event(s)", flush=True)
        print(f"   done — {found} event(s) recorded", flush=True)


def cmd_backfill_snapshots(args):
    """Fill snapshot holes from archive state — reconstructed, and labelled so."""
    from datetime import date

    from defi_tracker.core.adapter import all_adapters
    from defi_tracker.core.types import Chain

    st = _db()
    runner = TrackerRunner(st)
    by_pid = {a.info.protocol_id: a for a in all_adapters()}
    holes = st.snapshot_holes()
    if args.chains:
        holes = [h for h in holes if h[1] in args.chains]
    if not holes:
        print("No snapshot holes.")
        return
    print(f"{len(holes)} missing feed-day(s) to reconstruct from archive state")
    for wallet, chain, pid, day in holes:
        adapter = by_pid.get(pid)
        label = f"{day}  {chain}/{pid}  {wallet[:10]}…"
        if adapter is None:
            print(f"   {label}  — no adapter registered")
            continue
        try:
            n = runner.reconstruct_day(adapter, wallet, Chain(chain), date.fromisoformat(day))
            print(f"   {label}  ✅ {n} position(s)", flush=True)
        except NotImplementedError:
            print(f"   {label}  — this adapter cannot read archive state", flush=True)
        except Exception as e:
            print(f"   {label}  ✗ {redact(str(e))}", flush=True)


def cmd_mark(args):
    """Record one mark per position. No event sync, no snapshot, no alerts."""
    from defi_tracker.core.types import Chain
    from defi_tracker.netwait import wait_for_network

    if not wait_for_network():
        print("⚠️  No network — mark skipped.")
        raise SystemExit(75)  # EX_TEMPFAIL

    st = _db()
    runner = TrackerRunner(st)
    chains = [Chain(c) for c in args.chains] if args.chains else None
    result = runner.mark_all(wallets=args.wallets, chains=chains, protocols=args.protocols)
    stamp = datetime.fromtimestamp(result["ts"], tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    print(f"{result['marks_written']} mark(s) across {result['positions']} position(s) at {stamp}")
    for failure in result["failures"]:
        print(f"   ⚠ {redact(failure)}")


def cmd_backfill_gas(args):
    """Read receipts for transactions with no priced cost row and record what
    each one cost in gas."""
    from defi_tracker.core.types import Chain
    from defi_tracker.costs import backfill_gas

    st = _db()
    chain = Chain(args.chain) if args.chain else None
    result = backfill_gas(st, chain=chain, limit=args.limit)
    print(result)
    if result.no_rpc:
        print("Set the chain's *_RPC_URL to price the rest — gas stays unknown without one.")


def cmd_serve(args):
    """Serve the desk view on localhost, re-reading the DB on every request."""
    import webbrowser

    from defi_tracker.coinwatch import fetch_delivery
    from defi_tracker.serve import serve

    st = _db()
    provider = None if args.no_prices else _current_fee_prices

    def _announce(url: str) -> None:
        print(f"Serving the desk at {url}  (Ctrl-C to stop)")
        if args.open:
            webbrowser.open(url)

    serve(
        st,
        host=args.host,
        port=args.port,
        title=args.title,
        price_provider=provider,
        price_ttl=args.price_ttl,
        on_start=_announce,
        market_provider=None if args.no_prices else _market_reads,
        delivery_provider=fetch_delivery,
    )


def cmd_set_cg_id(args):
    """
    Manually pin a CoinGecko coin ID to a token address.

    Useful when CoinGecko doesn't index the token on its native chain
    (e.g. a PEAQ token that also trades on Ethereum/Base under the same ID).

    Example:
        defi-tracker set-cg-id --chain peaq --address 0x08e6...1484 --cg-id ovr
    """
    s = _db()
    found = s.set_coingecko_id(args.chain, args.address, args.cg_id)
    if found:
        print(f"✅ Set coingecko_id='{args.cg_id}' for {args.address} on {args.chain}")
        print("   Run `defi-tracker run` to reprice affected positions.")
    else:
        print(f"⚠️  Token not found: {args.address} on {args.chain}")
        print("   It may not have been seen yet — run `defi-tracker run` first to register it.")


def cmd_reset_cursor(args):
    """
    Reset the block-scan cursor for a chain back to its deploy block.

    This enables historical backfill of COLLECT (and other) events that occurred
    before the tracker was first set up. After resetting, run `defi-tracker run`
    several times — each run will advance the cursor by 150k blocks until it
    reaches the chain tip, picking up all historical events.

    Example:
        defi-tracker reset-cursor --chain peaq
    """
    from defi_tracker.adapters.machinex import DEPLOY_BLOCK

    chain = args.chain.lower()

    _DEPLOY_BLOCKS = {
        "peaq": DEPLOY_BLOCK,
    }
    deploy_block = _DEPLOY_BLOCKS.get(chain)
    if deploy_block is None:
        print(f"⚠️  No deploy block defined for chain '{chain}'.")
        print(f"   Supported: {', '.join(_DEPLOY_BLOCKS)}")
        return

    s = _db()
    wallets = s.list_wallets()
    if not wallets:
        print("No wallets registered. Run `defi-tracker add-wallet <address>` first.")
        return

    print(f"Resetting {chain} block cursor to deploy block {deploy_block:,} for all wallets…")
    for w in wallets:
        addr = w["address"].lower()
        key = f"block_cursor:{addr}:{chain}"
        s.kv_set(key, str(deploy_block))
        print(f"   {addr}  →  block {deploy_block:,}")

    print(f"\n✅ Reset {len(wallets)} cursor(s).")
    print("   Run `defi-tracker run` (up to ~20 times) to backfill historical events.")


def cmd_refresh_tokens(args):
    """
    Resolve ERC20 symbol/decimals for all stub entries in the token registry.

    Stubs are rows whose symbol looks like '0xabcd…1234' — written when a token
    was first encountered on a chain with no subgraph (e.g. peaq / MachineX).
    Iterates every stub and calls symbol()+decimals() via the chain's RPC.
    """
    import re

    from defi_tracker.adapters._rpc import RpcClient
    from defi_tracker.core.endpoints import rpc_url as endpoint_url
    from defi_tracker.core.types import Chain

    rpc_by_chain = {
        "peaq": endpoint_url(Chain.PEAQ),
    }
    if args.rpc:
        chain, url = args.rpc.split("=", 1)
        rpc_by_chain[chain.strip()] = url.strip()

    stub_re = re.compile(r"^0x[0-9a-fA-F]{4}…[0-9a-fA-F]{4}$")

    s = _db()
    with s.connect() as conn:
        stubs = [
            dict(r)
            for r in conn.execute("SELECT chain, address, symbol FROM tokens").fetchall()
            if stub_re.match(r["symbol"])
        ]

    if not stubs:
        print("No stub tokens found — all symbols already resolved.")
        return

    print(f"Found {len(stubs)} stub token(s). Resolving via RPC…")
    resolved = failed = 0

    for row in stubs:
        chain = row["chain"]
        addr = row["address"]
        rpc_url = rpc_by_chain.get(chain)
        if not rpc_url:
            _log.debug("No RPC URL for chain %s — skipping %s", chain, addr)
            failed += 1
            continue

        rpc = RpcClient(rpc_url)
        symbol, decimals = rpc.erc20_metadata(addr)
        if symbol:
            from defi_tracker.core.types import Chain as ChainEnum
            from defi_tracker.core.types import Token

            try:
                chain_enum = ChainEnum(chain)
            except ValueError:
                failed += 1
                continue
            tok = Token(chain_enum, addr, symbol, decimals if decimals is not None else 18)
            s.upsert_token(tok)
            print(f"   {addr}  →  {symbol} ({decimals} decimals)")
            resolved += 1
        else:
            _log.debug("No symbol returned for %s on %s", addr, chain)
            failed += 1

    print(f"\n✅ Resolved {resolved}, skipped/failed {failed}.")
    if resolved:
        print("   Run `defi-tracker run` to snapshot positions with updated labels.")


# ── argparse wiring ───────────────────────────────────────────────────────


def main():
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Backstop for the RPC client's own redaction: no log line from any
    # library may carry a URL path, because an RPC URL's path is its API key.
    for handler in logging.getLogger().handlers:
        handler.addFilter(_RedactUrls())
    p = argparse.ArgumentParser(prog="defi-tracker")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialize SQLite schema").set_defaults(func=cmd_init)

    p_add = sub.add_parser("add-wallet", help="Register a wallet to track")
    p_add.add_argument("address")
    p_add.add_argument("--label")
    p_add.set_defaults(func=cmd_add_wallet)

    p_vault = sub.add_parser("set-vault", help="Record which Fireblocks vault holds a wallet")
    p_vault.add_argument("address")
    p_vault.add_argument("vault_id", nargs="?")
    p_vault.add_argument("--clear", action="store_true", help="Remove the vault link")
    p_vault.set_defaults(func=cmd_set_vault)

    p_run = sub.add_parser("run", help="Sync + snapshot + evaluate alerts")
    p_run.add_argument("--wallets", nargs="*", help="Filter to specific wallets")
    p_run.add_argument("--protocols", nargs="*", help="Filter to specific protocol_ids")
    p_run.add_argument(
        "--report", action="store_true", help="Also print portfolio + MTD + alerts after sync"
    )
    p_run.add_argument(
        "--verbose", "-v", action="store_true", help="Set log level to INFO for this run"
    )
    p_run.set_defaults(func=cmd_run)

    p_mtd = sub.add_parser(
        "mtd", help="Month-to-date change per position (flows not netted; see `periods`)"
    )
    p_mtd.add_argument("--wallet")
    p_mtd.add_argument("--year", type=int)
    p_mtd.add_argument("--month", type=int)
    p_mtd.set_defaults(func=cmd_mtd)

    p_per = sub.add_parser(
        "periods",
        help="Period PnL (D/WTD/MTD/QTD/YTD/ITD) with flows netted out, sliceable",
    )
    p_per.add_argument(
        "--by", choices=["position", "pair", "chain", "venue", "wallet"], help="Slice one period"
    )
    p_per.add_argument(
        "--period", choices=["D", "WTD", "MTD", "QTD", "YTD", "ITD"], help="With --by (default MTD)"
    )
    p_per.add_argument(
        "--month", help="A calendar month, YYYY-MM (sliced with --by, default chain)"
    )
    p_per.set_defaults(func=cmd_periods)

    p_lim = sub.add_parser("limits", help="Mandate policy limits and breaches")
    p_lim.add_argument("--policy", help="Policy file (default: $POLICY_FILE or ./policy.toml)")
    p_lim.add_argument("--all", action="store_true", help="Also list passing checks")
    p_lim.set_defaults(func=cmd_limits)

    p_exc = sub.add_parser("exceptions", help="Policy exception log — list, or record a review")
    p_exc.add_argument("--all", action="store_true", help="Include cleared, reviewed episodes")
    p_exc.add_argument("--review", type=int, metavar="ID", help="Record a review of exception ID")
    p_exc.add_argument("--by", help="Who reviewed it (required with --review)")
    p_exc.add_argument(
        "--disposition",
        choices=["accepted", "remediated", "false_positive"],
        default="accepted",
    )
    p_exc.add_argument("--note")
    p_exc.set_defaults(func=cmd_exceptions)

    p_inc = sub.add_parser("incident", help="Incident log — add, update, or list")
    p_inc.add_argument("action", choices=["list", "add", "update"], nargs="?", default="list")
    p_inc.add_argument("--id", type=int)
    p_inc.add_argument("--title")
    p_inc.add_argument(
        "--severity", choices=["low", "medium", "high", "critical"], default="medium"
    )
    p_inc.add_argument("--description")
    p_inc.add_argument("--at", help="YYYY-MM-DD the step happened (default: now)")
    p_inc.add_argument("--by", help="Who is logging it")
    p_inc.add_argument("--escalated-to", dest="escalated_to")
    p_inc.add_argument("--contained", action="store_true")
    p_inc.add_argument("--resolved", action="store_true")
    p_inc.add_argument("--postmortem", help="Link or summary; required to resolve")
    p_inc.set_defaults(func=cmd_incident)

    p_led = sub.add_parser(
        "ledger", help="Contractual ledger — MM agreements, loans/options, OTC (hand-keyed)"
    )
    p_led.add_argument(
        "action",
        choices=["list", "add", "update", "history", "tranche", "tranches"],
        nargs="?",
        default="list",
    )
    p_led.add_argument("--ref", help="Stable id for the contract, e.g. mm-a-loan-2024")
    p_led.add_argument("--counterparty")
    p_led.add_argument("--kind", choices=["mm_loan", "mm_option", "mm_retainer", "otc", "other"])
    p_led.add_argument("--status", choices=["active", "expired", "terminated"])
    p_led.add_argument("--asset")
    p_led.add_argument("--amount", help="Amount in asset units")
    p_led.add_argument("--usd", type=float, help="Exposure in USD (leave out if unknown)")
    p_led.add_argument("--start", help="YYYY-MM-DD")
    p_led.add_argument("--expires", help="YYYY-MM-DD")
    p_led.add_argument("--terms", help="Strike, fees, KPIs — free text")
    p_led.add_argument(
        "--annual-cost",
        dest="annual_cost",
        type=float,
        help="What the contract costs peaq a year, USD (fees, premia) — the cost-of-liquidity metric",
    )
    p_led.add_argument(
        "--tranche", help="`tranche`: the option tranche, as the contract numbers it"
    )
    p_led.add_argument("--strike", help="`tranche`: strike in USD; leave out while unfixed")
    p_led.add_argument("--strike-rule", dest="strike_rule", help="`tranche`: how the strike is set")
    p_led.add_argument(
        "--strike-low", dest="strike_low", help="`tranche`: unfixed strike, low bound"
    )
    p_led.add_argument(
        "--strike-high", dest="strike_high", help="`tranche`: unfixed strike, high bound"
    )
    p_led.add_argument("--window-from", dest="window_from", help="`tranche`: YYYY-MM-DD")
    p_led.add_argument("--window-to", dest="window_to", help="`tranche`: YYYY-MM-DD")
    p_led.add_argument(
        "--tranche-status", dest="tranche_status", choices=["open", "exercised", "lapsed"]
    )
    p_led.add_argument("--exercised-on", dest="exercised_on", help="`tranche`: YYYY-MM-DD")
    p_led.add_argument(
        "--exercise-price", dest="exercise_price", help="`tranche`: PEAQ market price at exercise"
    )
    p_led.add_argument(
        "--notice-by",
        dest="notice_by",
        help="YYYY-MM-DD: last day to give notice to stop a renewal",
    )
    p_led.add_argument("--interest", type=float, help="Loan interest, %%/yr as the contract states")
    p_led.add_argument("--returned", help="Loaned units the MM handed back early (no longer owed)")
    p_led.add_argument("--source", help="Where the figures came from (required)")
    p_led.add_argument(
        "--as-of", dest="as_of", help="YYYY-MM-DD this is true as of (default today)"
    )
    p_led.set_defaults(func=cmd_ledger)

    p_man = sub.add_parser("mandate-report", help="Write the monthly mandate report (durable HTML)")
    p_man.add_argument("--month", help="YYYY-MM (default: the last complete month)")
    p_man.add_argument("--out", default="reports", help="Directory (default: reports/)")
    p_man.add_argument("--policy", help="Policy file (default: $POLICY_FILE or ./policy.toml)")
    p_man.add_argument(
        "--force", action="store_true", help="Rewrite a final report that already exists"
    )
    p_man.set_defaults(func=cmd_mandate_report)

    p_risk = sub.add_parser(
        "risk", help="Price-shock grid, greeks, exit probability, breakeven APR"
    )
    p_risk.add_argument("--horizon", type=float, default=30.0, help="Days (default 30)")
    p_risk.set_defaults(func=cmd_risk)

    p_opt = sub.add_parser(
        "optimise", help="Per pool: the band that maximises expected fees − IL (propose-only)"
    )
    p_opt.add_argument("--horizon", type=int, default=30, help="Days (default 30)")
    p_opt.set_defaults(func=cmd_optimise)

    p_el = sub.add_parser(
        "elasticity",
        help="Measure how pool fees respond to depth (ε) from the pools' own history (swaps, or the fee counter)",
    )
    p_el.add_argument(
        "--sync", action="store_true", help="Scan new swaps first (slow the first time)"
    )
    p_el.set_defaults(func=cmd_elasticity)

    p_bt = sub.add_parser(
        "backtest", help="Replay the range optimiser over pool history vs the book as it stood"
    )
    p_bt.add_argument("--horizon", type=int, default=30, help="Days (default 30)")
    p_bt.add_argument("--step", type=int, default=7, help="Days between rebalance dates")
    p_bt.add_argument(
        "--strategy",
        action="append",
        help="optimiser[:knobs] | hold | sigma:<z> | fixed:<pct>; repeat to compare "
        "(default: configured)",
    )
    p_bt.add_argument(
        "--split",
        metavar="YYYY-MM-DD",
        help="Score each strategy in sample (windows ending before) and out of sample (after)",
    )
    p_bt.add_argument(
        "--grid",
        action="store_true",
        help="With --split: choose the optimiser's knobs in sample, score them out of sample",
    )
    p_bt.set_defaults(func=cmd_backtest)

    p_mom = sub.add_parser("mom", help="Month-over-month roll-up")
    p_mom.add_argument("--wallet")
    p_mom.add_argument("--months", type=int, default=6)
    p_mom.set_defaults(func=cmd_mom)

    sub.add_parser(
        "health", help="Pipeline liveness — when each source last actually succeeded"
    ).set_defaults(func=cmd_health)

    p_rep = sub.add_parser("report", help="Current portfolio snapshot")
    p_rep.add_argument("--wallet")
    p_rep.set_defaults(func=cmd_report)

    p_fees = sub.add_parser("fees", help="Monthly fee income trend from collect events")
    p_fees.add_argument("--wallet")
    p_fees.add_argument("--months", type=int, help="Limit trend to trailing N months")
    p_fees.add_argument(
        "--recent", type=int, default=3, help="Trailing months for the 'Recent' pair column"
    )
    p_fees.add_argument(
        "--by-claim-date",
        action="store_true",
        help="Bucket by the calendar month of the claim tx (raw), instead of the "
        "fee-accrual month (start-of-month sweeps attributed to the prior month)",
    )
    p_fees.set_defaults(func=cmd_fees)

    p_alerts = sub.add_parser("alerts", help="Show recent alerts")
    p_alerts.add_argument("--limit", type=int, default=50)
    p_alerts.add_argument("--undelivered-only", action="store_true", dest="undelivered_only")
    p_alerts.set_defaults(func=cmd_alerts)

    p_reb = sub.add_parser("rebalance", help="Rebalance action queue — list, mark done, or snooze")
    p_reb.add_argument(
        "--done", type=int, metavar="ID", help="Mark queue item ID rebalanced (drops off)"
    )
    p_reb.add_argument("--snooze", type=int, metavar="ID", help="Defer queue item ID")
    p_reb.add_argument(
        "--days", type=int, default=7, help="Snooze duration in days (with --snooze)"
    )
    p_reb.add_argument(
        "--all", action="store_true", dest="show_all", help="Include resolved history"
    )
    p_reb.set_defaults(func=cmd_rebalance)

    p_dash = sub.add_parser("dashboard", help="Sync + full portfolio view in one command")
    p_dash.add_argument("--wallet")
    p_dash.set_defaults(func=cmd_dashboard)

    p_web = sub.add_parser("web", help="Render the desk view as a self-contained HTML page")
    p_web.add_argument(
        "--out", default="dashboard.html", help="Output path (default: dashboard.html)"
    )
    p_web.add_argument("--title", default="Liquidity Desk", help="Page title")
    p_web.add_argument("--open", action="store_true", help="Open it in the default browser")
    p_web.add_argument(
        "--no-prices",
        action="store_true",
        help="Offline: skip today's prices, CoinGecko venues and exchange order books "
        "(fees show at-claim value; market tables read 'not read')",
    )
    p_web.set_defaults(func=cmd_web)

    p_rpc = sub.add_parser(
        "rpc-check",
        help="Probe each configured RPC: reachable, right chain, archive or pruned",
    )
    p_rpc.add_argument("--chains", nargs="*", help="Chains to probe (default: peaq, bsc, base)")
    p_rpc.set_defaults(func=cmd_rpc_check)

    p_xyk = sub.add_parser(
        "backfill-xyk", help="Recover the XYK positions' deposit/withdraw history (peaq, once)"
    )
    p_xyk.add_argument("--from-block", dest="from_block", type=int)
    p_xyk.add_argument("--workers", type=int, default=8, help="Concurrent windows (default 8)")
    p_xyk.add_argument(
        "--exhaustive", action="store_true", help="Scan every block, not only changed intervals"
    )
    p_xyk.set_defaults(func=cmd_backfill_xyk)

    p_bs = sub.add_parser(
        "backfill-snapshots",
        help="Fill missing snapshot days from archive state (labelled source=archive)",
    )
    p_bs.add_argument("--chains", nargs="*", help="Limit to these chains")
    p_bs.set_defaults(func=cmd_backfill_snapshots)

    p_mark = sub.add_parser(
        "mark",
        help="Record one mark per position — no event sync. Run this hourly; `run` stays daily",
    )
    p_mark.add_argument("--wallets", nargs="*", help="Limit to these wallets")
    p_mark.add_argument("--chains", nargs="*", help="Limit to these chains")
    p_mark.add_argument("--protocols", nargs="*", help="Limit to these protocol ids")
    p_mark.set_defaults(func=cmd_mark)

    p_gas = sub.add_parser(
        "backfill-gas",
        help="Read transaction receipts and record what each one cost in gas",
    )
    p_gas.add_argument("--chain", help="Limit to one chain, e.g. peaq")
    p_gas.add_argument("--limit", type=int, help="Stop after this many transactions")
    p_gas.set_defaults(func=cmd_backfill_gas)

    p_serve = sub.add_parser(
        "serve",
        help="Serve the desk view on localhost, re-read from the DB on every request",
    )
    p_serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1 — the page has no authentication)",
    )
    p_serve.add_argument("--port", type=int, default=8787, help="Port (default: 8787)")
    p_serve.add_argument("--title", default="Liquidity Desk", help="Page title")
    p_serve.add_argument("--open", action="store_true", help="Open it in the default browser")
    p_serve.add_argument(
        "--price-ttl",
        type=float,
        default=DEFAULT_PRICE_TTL,
        dest="price_ttl",
        help=f"Seconds to cache today's prices (default: {DEFAULT_PRICE_TTL:.0f})",
    )
    p_serve.add_argument(
        "--no-prices",
        action="store_true",
        help="Offline: skip today's prices, CoinGecko venues and exchange order books "
        "(fees show at-claim value; market tables read 'not read')",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_cg = sub.add_parser(
        "set-cg-id",
        help="Pin a CoinGecko coin ID to a token address (for tokens CoinGecko doesn't index on their native chain)",
    )
    p_cg.add_argument("--chain", required=True, help="Chain name, e.g. peaq, ethereum, bsc")
    p_cg.add_argument("--address", required=True, help="Token contract address")
    p_cg.add_argument("--cg-id", required=True, dest="cg_id", help="CoinGecko coin ID, e.g. ovr")
    p_cg.set_defaults(func=cmd_set_cg_id)

    p_rt = sub.add_parser(
        "refresh-tokens",
        help="Resolve ERC20 symbol/decimals for address-stub entries in token registry",
    )
    p_rt.add_argument(
        "--rpc",
        metavar="CHAIN=URL",
        help="Override RPC URL for a chain, e.g. --rpc peaq=https://...",
    )
    p_rt.set_defaults(func=cmd_refresh_tokens)

    p_rc = sub.add_parser(
        "reset-cursor",
        help="Reset block-scan cursor to deploy block for historical event backfill",
    )
    p_rc.add_argument(
        "--chain",
        default="peaq",
        help="Chain to reset (default: peaq)",
    )
    p_rc.set_defaults(func=cmd_reset_cursor)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
