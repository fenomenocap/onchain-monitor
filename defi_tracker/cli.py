"""
CLI entry point.

    defi-tracker init
    defi-tracker add-wallet 0xABC --label hot
    defi-tracker run [--report] [--verbose]
    defi-tracker mtd [--wallet 0x...] [--year 2026] [--month 5]
    defi-tracker mom [--wallet 0x...] [--months 6]
    defi-tracker report [--wallet 0x...]

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

# uniswap_v3 (ETH/ARB/OP/BASE/POLYGON) is disabled by default — these wallets hold
# no positions there, and probing all 5 chains adds minutes of subgraph retry latency
# to every run. Set ENABLE_UNISWAP_V3=1 to re-enable if positions are opened there.
if os.getenv("ENABLE_UNISWAP_V3"):
    import defi_tracker.adapters.uniswap_v3  # noqa: F401
from defi_tracker.core.runner import TrackerRunner  # noqa: E402
from defi_tracker.core.storage import Storage  # noqa: E402
from defi_tracker.slack import SlackDelivery  # noqa: E402

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


def _trunc_mid(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    half = (max_len - 1) // 2
    return s[:half] + "…" + s[-(max_len - half - 1):]


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
    print(f"   positions snapshotted : {summary['positions_snapshotted']}")
    print(f"   alerts raised         : {summary['alerts_raised']}")
    for adapter, stats in summary["per_adapter"].items():
        print(f"   {adapter:<30} → {stats}")

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
        print(f"\nℹ️  SLACK_WEBHOOK_URL not set — {summary['alerts_raised']} alert(s) stored in DB but not delivered.")

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


def cmd_mtd(args):
    s = _db()
    rows = s.month_to_date_pnl(wallet=args.wallet, year=args.year, month=args.month)
    if not rows:
        print("No data for this period.")
        return
    from defi_tracker.analytics import fees_earned_mtd

    # il_change_mtd is NULL for positions with no event history (unknown
    # cost basis) — excluded from the total, rendered as "—" per row.
    total_pnl = sum(r["pnl_mtd_usd"] or 0 for r in rows)
    total_fee = sum(r["fees_collected_mtd"] or 0 for r in rows)
    total_il = sum(r["il_change_mtd"] or 0 for r in rows)
    earned = float(fees_earned_mtd(rows))
    _sep("Month-to-Date PnL")
    print(f"   Total PnL          : {_usd(total_pnl)}")
    print(f"   Fees earned MTD    : {_usd(earned)}   (accrual: collected + Δunclaimed)")
    print(f"   Fees collected MTD : {_usd(total_fee)}   (cash: harvests dated this month)")
    print(f"   IL change MTD      : {_usd(total_il)}")
    print(
        f"\n   {'Wallet':<14} {'Chain':<10} {'Pair':<24} {'ΔValue':>11} {'Fees':>11} {'ΔIL':>11} {'PnL':>11} {'In?'}"
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
        delta = _usd(r["delta_vs_prev_month"]) if r["delta_vs_prev_month"] is not None else "          —"
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
    print(
        f"   {'When':<10} {'Sev':<4} {'Kind':<18} {'Position':<36} {'Message':<42} {'Del'}"
    )
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

    # List: refresh the queue against current signals, then show it.
    signals = TrackerRunner(s).compute_rebalance_signals()
    s.reconcile_rebalance_queue([x for x in signals if x["tier"] in ("now", "soon")])
    rows = s.list_rebalance_queue(include_resolved=args.show_all)
    if not rows:
        print("Rebalance queue is empty — nothing needs action. 🎉")
        return

    now = int(datetime.now(UTC).timestamp())
    icon = {"now": "🔴", "soon": "🟠"}
    _sep("Rebalance queue")
    print(f"   {'ID':>3}  {'':<2}{'Pair':<22}{'Idle':>9}{'Past':>6}{'Age':>5}  {'Status':<18}Target")
    _sep(width=104)
    for r in rows:
        ctx = _json.loads(r["context_json"] or "{}")
        sug = ctx.get("suggest") or {}
        age = f"{max(1, (now - r['first_seen']) // 86400)}d"
        if r["status"] == "resolved":
            status = f"✓ {r['resolved_reason']}"
        elif r["status"] == "snoozed":
            wk = datetime.fromtimestamp(r["snooze_until"], UTC).date().isoformat()
            status = f"💤 until {wk}"
        else:
            status = "open"
        tgt = (
            f"±{sug['half_pct']:.0f}% (now ±{ctx.get('current_half_pct', 0):.0f}%)"
            if sug else "—"
        )
        depth = f"{ctx.get('depth', 0) * 100:.0f}%"
        print(
            f"   {r['id']:>3}  {icon.get(r['tier'], '•'):<2}{r['pair_label'][:22]:<22}"
            f"{_fmt_short(ctx.get('idle_usd', 0)):>9}{depth:>6}{age:>5}  {status:<18}{tgt}"
        )
    print("\n   `rebalance --done <ID>` when rebalanced · `--snooze <ID> --days N` to defer")


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
    total_unc = sum(r["unclaimed_usd"] for r in snaps)
    total_il = sum(r["il_usd"] or 0 for r in snaps)
    total_pnl = sum(r["pnl_usd"] or 0 for r in snaps)

    _sep("Portfolio Snapshot")
    print(f"   LP Value     : {_usd(total_value)}")
    print(f"   Unclaimed    : {_usd(total_unc)}")
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
            chain_unc += r["unclaimed_usd"] or 0
            chain_il += r["il_usd"] or 0
            chain_pnl += r["pnl_usd"] or 0
            print(
                f"   {r['pair_label']:<26} {_w(r['wallet']):<14} "
                f"{_usd(r['current_value_usd'])} {_usd(r['unclaimed_usd'])} "
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

    from defi_tracker.slack import SlackDelivery

    for chain_name, as_of, age_days in SlackDelivery._stale_chains(snaps):
        print(
            f"   ⚠️  {chain_name} marks are {age_days}d old (as of {as_of} — "
            f"data source outage; {chain_name} values are frozen there)"
        )

    # ── Where the PnL comes from ──────────────────────────────────────────
    from defi_tracker.analytics import pnl_decomposition, realized_closed_pnl, risk_summary

    d = pnl_decomposition(snaps)
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
        fee_valuation,
        fees_by_pair,
        monthly_fee_matrix,
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
    months = val["months"]
    if args.months:
        months = months[-args.months:]

    from datetime import UTC, datetime

    # Snapshots power the live "accruing" row (current month, pre-sweep) and
    # the open-capital column further down.
    snaps = s.latest_snapshot(wallet=args.wallet)

    # Chain → display label. MachineX = peaq CL; PancakeSwap = bsc Infinity.
    chain_label = {"peaq": "MachineX", "bsc": "PancakeSwap"}
    _, chains, cells = monthly_fee_matrix(rows)
    # Stable, named column order; any other chain keeps its raw name.
    ordered = [c for c in ("peaq", "bsc") if c in chains] + [
        c for c in chains if c not in ("peaq", "bsc")
    ]
    labels = [chain_label.get(c, c) for c in ordered]

    # Current accrual month is still pre-sweep: show live unclaimed (fees since
    # the last harvest, marked to market) as an '*' row, outside the totals.
    pending_month = None
    pending_by_chain: dict[str, float] = {}
    if by_accrual:
        cur_month = datetime.now(UTC).strftime("%Y-%m")
        if cur_month not in val["months"]:
            for snap in snaps:
                u = float(snap.get("unclaimed_usd") or 0)
                if u:
                    pending_by_chain[snap["chain"]] = pending_by_chain.get(snap["chain"], 0.0) + u
            if sum(pending_by_chain.values()) > 0:
                pending_month = cur_month

    wm, wc, wt = 9, 13, 15  # month / chain / total column widths

    def _fee_row(label, chain_vals, total, mtm):
        body = "".join(
            f"{('$' + format(v, ',.0f')) if v else '—':>{wc}}" for v in chain_vals
        )
        return (
            f"   {label:<{wm}}{body}"
            f"{'$' + format(total, ',.0f'):>{wt}}{'$' + format(mtm, ',.0f'):>{wt}}"
        )

    basis = "fee-accrual month" if by_accrual else "claim-tx month"
    _sep(f"Fees claimed by month ({basis}) — at claim price vs marked-to-market")
    header = (
        f"   {'Month':<{wm}}"
        + "".join(f"{lab:>{wc}}" for lab in labels)
        + f"{'Total claimed':>{wt}}{'Claimed (MtM)':>{wt}}"
    )
    print(header)
    _sep(width=len(header) - 3)
    for m in months:
        cvals = [float(cells.get((m, c), 0)) for c in ordered]
        print(
            _fee_row(
                m, cvals, float(val["claim_by_month"].get(m, 0)),
                float(val["today_by_month"].get(m, 0)),
            )
        )
    if pending_month:
        cvals = [pending_by_chain.get(c, 0.0) for c in ordered]
        ptot = sum(cvals)
        print(_fee_row(pending_month + "*", cvals, ptot, ptot))
    _sep(width=len(header) - 3)

    # YTD (current calendar year, claimed only) + lifetime.
    year = datetime.now(UTC).strftime("%Y")
    ytd_months = [m for m in val["months"] if m.startswith(year)]
    print(
        _fee_row(
            f"YTD {year}",
            [sum(float(cells.get((m, c), 0)) for m in ytd_months) for c in ordered],
            sum(float(val["claim_by_month"].get(m, 0)) for m in ytd_months),
            sum(float(val["today_by_month"].get(m, 0)) for m in ytd_months),
        )
    )
    print(
        _fee_row(
            "Lifetime",
            [sum(float(cells.get((m, c), 0)) for m in val["months"]) for c in ordered],
            float(val["claim_total"]), float(val["today_total"]),
        )
    )
    if pending_month:
        print("\n   * accruing — unclaimed so far, marked-to-market; not yet claimed.")
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
    for row in fees_by_pair(rows, recent_months=args.recent, symbols_by_key=symbols):
        open_val = _open_value(row["pair"])
        if open_val and open_val > 0:
            apr = float(row["recent"]) / args.recent * 12 / open_val * 100
            val_s, apr_s = f"${open_val:,.0f}", f"{apr:.0f}%"
        else:
            val_s, apr_s = "closed", "—"
        print(
            f"   {row['pair'][:24]:<24} {row['chain']:<6} {row['collects']:>8} "
            f"{'$' + format(float(row['lifetime']), ',.0f'):>11} "
            f"{'$' + format(float(row['recent']), ',.0f'):>11} "
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

    rpc_by_chain = {
        "peaq": os.getenv("PEAQ_RPC_URL", "https://peaq.api.onfinality.io/public"),
    }
    if args.rpc:
        chain, url = args.rpc.split("=", 1)
        rpc_by_chain[chain.strip()] = url.strip()

    stub_re = re.compile(r"^0x[0-9a-fA-F]{4}…[0-9a-fA-F]{4}$")

    s = _db()
    with s.connect() as conn:
        stubs = [
            dict(r)
            for r in conn.execute(
                "SELECT chain, address, symbol FROM tokens"
            ).fetchall()
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
    p = argparse.ArgumentParser(prog="defi-tracker")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialize SQLite schema").set_defaults(func=cmd_init)

    p_add = sub.add_parser("add-wallet", help="Register a wallet to track")
    p_add.add_argument("address")
    p_add.add_argument("--label")
    p_add.set_defaults(func=cmd_add_wallet)

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

    p_mtd = sub.add_parser("mtd", help="Month-to-date PnL")
    p_mtd.add_argument("--wallet")
    p_mtd.add_argument("--year", type=int)
    p_mtd.add_argument("--month", type=int)
    p_mtd.set_defaults(func=cmd_mtd)

    p_mom = sub.add_parser("mom", help="Month-over-month roll-up")
    p_mom.add_argument("--wallet")
    p_mom.add_argument("--months", type=int, default=6)
    p_mom.set_defaults(func=cmd_mom)

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
    p_alerts.add_argument(
        "--undelivered-only", action="store_true", dest="undelivered_only"
    )
    p_alerts.set_defaults(func=cmd_alerts)

    p_reb = sub.add_parser(
        "rebalance", help="Rebalance action queue — list, mark done, or snooze"
    )
    p_reb.add_argument("--done", type=int, metavar="ID", help="Mark queue item ID rebalanced (drops off)")
    p_reb.add_argument("--snooze", type=int, metavar="ID", help="Defer queue item ID")
    p_reb.add_argument("--days", type=int, default=7, help="Snooze duration in days (with --snooze)")
    p_reb.add_argument("--all", action="store_true", dest="show_all", help="Include resolved history")
    p_reb.set_defaults(func=cmd_rebalance)

    p_dash = sub.add_parser(
        "dashboard", help="Sync + full portfolio view in one command"
    )
    p_dash.add_argument("--wallet")
    p_dash.set_defaults(func=cmd_dashboard)

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
