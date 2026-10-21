"""Monthly mandate report — the durable artifact mandate §2.3 asks leadership to receive.

"Monthly written report to leadership: deployed capital, yield, attribution,
exposure by venue and counterparty, policy exceptions, and incidents." This
module gathers exactly that for one calendar month, renders it as one
self-contained HTML document, and writes it once.

Written once, because a report is a record of what was reported. The book
underneath keeps changing — a subgraph heals and backfills a month of events,
a price gets corrected — so regenerating September in December would quietly
restate it. A final report therefore refuses to overwrite itself without
`force`; a month still in progress is written as a separate, explicitly
provisional file that may be replaced.

Benchmark: HODL of the deposited tokens (the desk's chosen benchmark).
Against it, excess return = IL + fees earned − gas, exactly, per position.

Like the desk view, this renders; the numbers come from `analytics`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

from defi_tracker.analytics import (
    LimitCheck,
    PeriodPnL,
    counterparty_exposure,
    health_warnings,
    mandate_scorecard,
    policy_checks,
)
from defi_tracker.core.storage import Storage
from defi_tracker.policy import LIMITS, Policy
from defi_tracker.report import gather_period, latest_mark_date, reports_written
from defi_tracker.web import _CSS, _e, _usd

BENCHMARK = "HODL of the deposited tokens"
EXPIRY_WATCH_DAYS = 90  # contracts expiring within this many days are flagged


class ReportExists(FileExistsError):
    """A final report for this month has already been written."""


@dataclass
class MandateReport:
    month: str  # 'YYYY-MM'
    start: date
    end: date  # the last day the report's marks reach (≤ month end)
    month_end: date
    complete: bool  # the month is over and the marks reach its last day
    generated_at: int
    period: PeriodPnL
    book_rows: list[dict]  # live positions as of `end`, for exposure
    checks: list[LimitCheck]
    policy_source: str
    exceptions: list[dict]
    incidents: list[dict]
    contracts: list[dict]
    exposure: dict[str, Decimal | None]
    source_warnings: list[str] = field(default_factory=list)
    depeg_note: str = ""
    scorecard: list[dict] = field(default_factory=list)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return start, nxt.fromordinal(nxt.toordinal() - 1)


def _ts(d: date, end_of_day: bool = False) -> int:
    t = time.max if end_of_day else time.min
    return int(datetime.combine(d, t, tzinfo=UTC).timestamp())


def gather_mandate_report(
    storage: Storage,
    year: int,
    month: int,
    policy: Policy,
    *,
    today: date | None = None,
    depeg_prices: dict[str, Decimal] | None = None,
) -> MandateReport | None:
    """Everything the month's report says, read once. None when there are
    no marks inside the month at all."""
    today = today or datetime.now(UTC).date()
    start, month_end = _month_bounds(year, month)
    latest = latest_mark_date(storage)
    if latest is None or latest < start:
        return None
    end = min(month_end, latest)
    complete = today > month_end and end == month_end

    book_rows = [r for r in storage.snapshots_as_of(end) if not r["is_closed"]]
    contracts = storage.contracts_as_of(end)
    exposure = counterparty_exposure(contracts, end)
    # A market price read today says nothing about a past month's peg.
    live = end >= today.fromordinal(today.toordinal() - 1)
    prices = depeg_prices if live else None
    depeg_note = "" if live else "Depeg is not assessed for a past month: no historical read."
    period = gather_period(storage, start, end, f"{year:04d}-{month:02d}")
    incidents = storage.list_incidents(_ts(start), _ts(end, end_of_day=True))
    exceptions = storage.list_exceptions(_ts(start), _ts(end, end_of_day=True))
    return MandateReport(
        scorecard=mandate_scorecard(
            [period], contracts, incidents, exceptions, reports_written(), end
        ),
        month=f"{year:04d}-{month:02d}",
        start=start,
        end=end,
        month_end=month_end,
        complete=complete,
        generated_at=int(datetime.now(UTC).timestamp()),
        period=period,
        book_rows=book_rows,
        checks=policy_checks(book_rows, policy, prices, exposure),
        policy_source=policy.source,
        exceptions=exceptions,
        incidents=incidents,
        contracts=contracts,
        exposure=exposure,
        source_warnings=health_warnings(storage.pipeline_health()),
        depeg_note=depeg_note,
    )


# ── rendering ─────────────────────────────────────────────────────────────


def _f(v: Decimal | None, dp: int = 0) -> str:
    return _usd(float(v), dp) if v is not None else "—"


def _pct(num: Decimal | None, den: Decimal | None, dp: int = 1) -> str:
    if num is None or den is None or den <= 0:
        return "—"
    return f"{num / den * 100:.{dp}f}%"


def _day(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d") if ts else "—"


def _returns(rep: MandateReport) -> dict:
    """The headline numbers, against the benchmark."""
    t = rep.period.totals()
    decomposed = [r for r in rep.period.rows if r.il is not None]
    excess = (
        sum((r.il + r.fees_earned for r in decomposed if r.il is not None), Decimal("0"))
        if decomposed
        else None
    )
    gas = t["gas"]
    gas_complete = t["gas_unpriced_txs"] == 0
    # Simple Dietz: flows are assumed to arrive mid-period. Stated on the page.
    capital = t["value_start"] + t["net_flows"] / 2
    days = (rep.end - rep.start).days + 1
    return {
        **t,
        "excess": excess,
        "excess_after_gas": (excess - gas) if excess is not None and gas_complete else None,
        "capital": capital if capital > 0 else None,
        "days": days,
        "undecomposed_positions": t["positions"] - t["decomposed_positions"],
    }


def _annualized(x: Decimal | None, capital: Decimal | None, days: int) -> str:
    if x is None or capital is None or days <= 0:
        return "—"
    return f"{x / capital * 100 * 365 / days:.1f}%"


def _table(head: list[str], rows: list[list[str]], foot: list[str] | None = None) -> str:
    out = ["<table><thead><tr>"]
    out += [f"<th>{_e(h)}</th>" for h in head]
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    out.append("</tbody>")
    if foot:
        out.append("<tfoot><tr>" + "".join(f"<td>{c}</td>" for c in foot) + "</tr></tfoot>")
    out.append("</table>")
    return "".join(out)


_REPORT_CSS = """
.status{font-weight:650}.status.breach{color:var(--critical)}.status.unknown{color:var(--serious)}
.status.unset{color:var(--muted)}.status.ok{color:var(--good)}
.method p{margin:4px 0;font-size:12.5px;color:var(--ink-2)}
td.wrap{white-space:normal;text-align:left}
@media print{#theme{display:none}.card{break-inside:avoid}body{background:#fff}}
"""


def render_mandate_report(rep: MandateReport) -> str:
    """The month's report as one self-contained HTML document."""
    r = _returns(rep)
    p = rep.period
    out: list[str] = []
    a = out.append
    title = f"Liquidity & Treasury Mandate — {rep.month}"
    state = "Final" if rep.complete else f"PROVISIONAL — marks through {rep.end.isoformat()}"
    a('<!doctype html><html lang="en"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width,initial-scale=1">')
    a(f"<title>{_e(title)}</title><style>{_CSS}{_REPORT_CSS}</style></head><body>")
    a('<div class="wrap">')
    a(
        f'<header><h1>{_e(title)}</h1><span class="asof">{_e(state)} · '
        f"window {rep.start.isoformat()} → {rep.end.isoformat()} · generated "
        f"{_e(datetime.fromtimestamp(rep.generated_at, tz=UTC).strftime('%Y-%m-%d %H:%M UTC'))}"
        "</span></header>"
    )
    if not rep.complete:
        a(
            '<div class="banner"><div class="t">Provisional</div><p>The month is not '
            "complete, or its marks do not yet reach its last day. Figures will change; "
            "the final report is written once the month closes.</p></div>"
        )

    # ── 0. success metrics ───────────────────────────────────────────
    if rep.scorecard:
        a(
            '<h2>Success metrics <span class="asof">from the proposal — target by end 2026</span></h2>'
        )
        rows = [
            [
                f'<span class="wrap">{_e(r["metric"])}</span>',
                f'<span class="wrap">{_e(r["target"])}</span>',
                f'<span class="wrap">{_e(r["now"])}</span><div class="asof wrap">{_e(r["note"])}</div>',
                _e({"measured": "Measured", "needs_input": "Needs input",
                    "not_here": "Measured elsewhere", "not_yet": "Not yet"}.get(r["status"], r["status"])),
            ]
            for r in rep.scorecard
        ]  # fmt: skip
        a(f'<div class="card">{_table(["Metric", "Target", "This month", "Status"], rows)}</div>')

    # ── 1. deployed capital ──────────────────────────────────────────
    book = sum((Decimal(str(pos["current_value_usd"])) for pos in rep.book_rows), Decimal("0"))
    # Position value only, like the book and the exclusions, so the three
    # tiles add up; unclaimed fees are in PnL, not in deployed capital.
    covered = sum((row.value_end for row in p.rows), Decimal("0"))
    a('<h2>1 · Deployed capital</h2><div class="tiles">')
    tiles = [
        ("Tracked DEX book", _f(book), f"{len(rep.book_rows)} live positions at {rep.end}"),
        (
            "Covered by attribution",
            _f(covered) if p.rows else "—",
            f"{_pct(covered, book)} of book · {len(p.rows)} positions",
        ),
        (
            "Excluded",
            _f(p.excluded_value()) if p.excluded else "$0",
            f"{len(p.excluded)} positions — see §9",
        ),
        (
            "Contract exposure",
            _f(sum((v for v in rep.exposure.values() if v is not None), Decimal("0")))
            if rep.exposure
            else "—",
            f"{len(rep.exposure)} counterparties keyed" if rep.exposure else "ledger not keyed",
        ),
    ]
    for lbl, val, note in tiles:
        a(
            f'<div class="card tile"><div class="lbl">{_e(lbl)}</div>'
            f'<div class="val">{_e(val)}</div><div class="note">{_e(note)}</div></div>'
        )
    a("</div>")

    # ── 2. yield vs benchmark ────────────────────────────────────────
    a(f"<h2>2 · Yield versus benchmark ({_e(BENCHMARK)})</h2>")
    if not p.rows:
        a('<div class="card"><p class="empty">No position could be attributed for this '
          "month — every one is excluded (§9). No yield is stated rather than a zero.</p></div>")  # fmt: skip
    else:
        kv = [
            ("Book PnL (covered positions)", _f(r["pnl"]), ""),
            ("Benchmark: HODL return (market move)", _f(r["market"]), ""),
            ("Excess vs benchmark: IL + fees earned", _f(r["excess"]), ""),
            ("↳ of which fees earned", _f(r["fees_earned"]), ""),
            ("↳ of which IL", _f(r["il"]), ""),
            ("Gas", _f(r["gas"], 2), ""),
            (
                "Excess after gas",
                _f(r["excess_after_gas"]),
                ""
                if r["excess_after_gas"] is not None
                else f"withheld: {r['gas_unpriced_txs']} tx unpriced",
            ),
            (
                "Annualized, on average capital",
                f"fees {_annualized(r['fees_earned'], r['capital'], r['days'])} · "
                f"excess {_annualized(r['excess_after_gas'], r['capital'], r['days'])}",
                f"capital {_f(r['capital'])}, simple Dietz",
            ),
        ]
        a('<div class="card">')
        for lbl, val, note in kv:
            a(
                f'<div class="kv"><span>{_e(lbl)}</span><span>{_e(val)}'
                + (f' <span class="asof">({_e(note)})</span>' if note else "")
                + "</span></div>"
            )
        if r["undecomposed_positions"]:
            a(
                f'<p class="asof">{r["undecomposed_positions"]} position(s) have no hold value to '
                f"split against: their {_f(r['undecomposed'])} price effect is in PnL but not in "
                "the benchmark or excess lines.</p>"
            )
        a("</div>")

    # ── 3. attribution ───────────────────────────────────────────────
    a('<h2>3 · Attribution by position</h2><div class="card">')
    if p.rows:
        rows = []
        for x in sorted(p.rows, key=lambda x: -abs(x.pnl)):
            excess = x.il + x.fees_earned if x.il is not None else None
            rows.append(
                [
                    f'<span class="pair">{_e(x.pair_label)}</span>'
                    + (" (closed)" if x.closed else ""),
                    _e(x.chain),
                    _e(_f(x.value_start + x.unclaimed_start)),
                    _e(_f(x.net_flows)),
                    _e(_f(x.market)),
                    _e(_f(x.il)),
                    _e(_f(x.fees_earned)),
                    _e(_f(x.pnl)),
                    _e(_f(excess)),
                ]
            )
        foot = [
            "Total",
            "",
            _e(_f(r["value_start"])),
            _e(_f(r["net_flows"])),
            _e(_f(r["market"])),
            _e(_f(r["il"])),
            _e(_f(r["fees_earned"])),
            _e(_f(r["pnl"])),
            _e(_f(r["excess"])),
        ]
        head = ["Position", "Chain", "Start", "Net flows", "Market", "IL", "Fees", "PnL", "Excess"]
        a(_table(head, rows, foot))
    else:
        a('<p class="empty">Nothing attributable this month.</p>')
    a("</div>")

    # ── 4. exposure ──────────────────────────────────────────────────
    a('<h2>4 · Exposure by venue, chain and counterparty</h2><div class="cols">')
    for title_, key in (
        ("Venue", lambda pos: f"{pos['chain']}:{pos['protocol_id']}"),
        ("Chain", lambda pos: pos["chain"]),
    ):
        grouped: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for pos in rep.book_rows:
            grouped[key(pos)] += Decimal(str(pos["current_value_usd"]))
        rows = [
            [_e(k), _e(_f(v)), _e(_pct(v, book))]
            for k, v in sorted(grouped.items(), key=lambda kv: -kv[1])
        ]
        a(f'<div class="card">{_table([title_, "Value", "Share"], rows)}</div>')
    if rep.exposure:
        rows = [
            [_e(k), _e(_f(v)), _e(_pct(v, book) if v is not None else "—")]
            for k, v in sorted(rep.exposure.items(), key=lambda kv: -(kv[1] or 0))
        ]
        a(f'<div class="card">{_table(["Counterparty", "Exposure", "vs DEX book"], rows)}</div>')
    else:
        a('<div class="card"><p class="empty">No counterparty contracts keyed in the '
          "ledger — counterparty exposure is unknown, not zero.</p></div>")  # fmt: skip
    a("</div>")

    # ── 5. policy limits ─────────────────────────────────────────────
    a(f'<h2>5 · Policy limits <span class="asof">({_e(rep.policy_source)})</span></h2>')
    a('<div class="card">')
    desc = {d.id: d.description for d in LIMITS}
    counts = {
        k: sum(1 for c in rep.checks if c.status == k) for k in ("breach", "unknown", "ok", "unset")
    }
    a(
        f"<p>{counts['breach']} breach · {counts['unknown']} unknown · {counts['ok']} within "
        f"limit · {counts['unset']} limit(s) <strong>not set</strong>. A limit that is not set "
        "is not a limit the book is inside.</p>"
    )
    shown = [c for c in rep.checks if c.status != "ok"]
    if shown:
        rows = []
        for c in shown:

            def fmt(v: Decimal | None, unit: str = c.unit) -> str:
                if v is None:
                    return "—"
                return _f(v) if unit == "USD" else f"{v:.1f}{'%' if unit == '%' else ' ' + unit}"

            rows.append(
                [
                    _e(desc.get(c.limit_id, c.limit_id)),
                    _e(c.subject),
                    _e(fmt(c.observed)),
                    _e(fmt(c.threshold)),
                    f'<span class="status {c.status}">{_e(c.status.upper())}</span>',
                    f'<span class="asof">{_e(c.note)}</span>',
                ]
            )
        a(_table(["Limit", "Subject", "Observed", "Limit", "Status", ""], rows))
    if rep.depeg_note:
        a(f'<p class="asof">{_e(rep.depeg_note)}</p>')
    a("</div>")

    # ── 6. exceptions ────────────────────────────────────────────────
    a('<h2>6 · Policy exceptions</h2><div class="card">')
    if rep.exceptions:
        unreviewed = sum(1 for exc in rep.exceptions if not exc["reviewed_at"])
        a(
            f"<p>{len(rep.exceptions)} exception(s) live during the month · "
            f"<strong>{unreviewed} unreviewed</strong>.</p>"
        )
        rows = [
            [
                f"#{exc['id']}",
                _e(exc["limit_id"]),
                _e(exc["subject"]),
                _e(_day(exc["opened_at"])),
                _e(_day(exc["cleared_at"]) if exc["cleared_at"] else "open"),
                _e(f"{exc['worst_observed']} {exc['unit']} (limit {exc['threshold']})"),
                _e(
                    f"{exc['disposition']} — {exc['reviewed_by']}"
                    if exc["reviewed_at"]
                    else "UNREVIEWED"
                ),
            ]
            for exc in rep.exceptions
        ]
        a(_table(["", "Limit", "Subject", "Opened", "Cleared", "Worst", "Review"], rows))
    else:
        a('<p class="empty">No policy exceptions during the month.'
          + (" (No limit is set yet, so none could arise.)" if counts["unset"] == len(LIMITS) else "")
          + "</p>")  # fmt: skip
    a("</div>")

    # ── 7. incidents ─────────────────────────────────────────────────
    a('<h2>7 · Incidents</h2><div class="card">')
    if rep.incidents:
        rows = [
            [
                f"#{inc['id']}",
                _e(inc["severity"]),
                f'<span class="wrap">{_e(inc["title"])}</span>',
                _e(_day(inc["detected_at"])),
                _e(_day(inc["contained_at"])),
                _e(_day(inc["resolved_at"])),
                _e(inc["escalated_to"] or "—"),
                f'<span class="wrap">{_e(inc["postmortem"] or "—")}</span>',
            ]
            for inc in rep.incidents
        ]
        head = [
            "",
            "Severity",
            "Incident",
            "Detected",
            "Contained",
            "Resolved",
            "Escalated",
            "Post-mortem",
        ]
        a(_table(head, rows))
    else:
        a('<p class="empty">No incidents logged for the month.</p>')
    a("</div>")

    # ── 8. contracts ─────────────────────────────────────────────────
    a('<h2>8 · Contractual ledger</h2><div class="card">')
    if rep.contracts:
        rows = []
        for con in rep.contracts:
            if con["end_date"]:
                days = (date.fromisoformat(con["end_date"]) - rep.end).days
                expiry = f"{con['end_date']} ({days:+d}d)"
                if 0 <= days <= EXPIRY_WATCH_DAYS:
                    expiry = f"<strong>{_e(expiry)}</strong>"
                else:
                    expiry = _e(expiry)
            else:
                expiry = "—"
            rows.append(
                [
                    _e(con["counterparty"]),
                    _e(con["kind"]),
                    _e(con["status"]),
                    expiry,
                    _e(
                        _f(Decimal(str(con["usd_value"])))
                        if con["usd_value"] is not None
                        else "unknown"
                    ),
                    f'<span class="wrap">{_e(con["terms"] or "")}</span>',
                    f'<span class="asof">as of {_e(con["as_of"])} · {_e(con["source_note"])}</span>',
                ]
            )
        a(_table(["Counterparty", "Kind", "Status", "Expires", "USD", "Terms", "Source"], rows))
    else:
        a('<p class="empty">Nothing keyed. MM agreements, loan/option structures and OTC '
          "placements are not visible to any API; key them with <code>defi-tracker ledger "
          "add</code>.</p>")  # fmt: skip
    a("</div>")

    # ── 9. data coverage ─────────────────────────────────────────────
    a('<h2>9 · Data coverage — what the figures above leave out</h2><div class="card">')
    if p.excluded:
        by_reason: defaultdict[str, list] = defaultdict(list)
        for ex in p.excluded:
            by_reason[ex.reason].append(ex)
        rows = [
            [
                _e(reason),
                str(len(items)),
                _e(", ".join(sorted({i.chain for i in items}))),
                _e(_f(sum((i.value_usd or Decimal("0") for i in items), Decimal("0")))),
            ]
            for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1]))
        ]
        a(_table(["Excluded because", "Positions", "Chains", "Value"], rows))
    else:
        a("<p>Every position is covered.</p>")
    if rep.source_warnings:
        a("<p><strong>Source state at generation:</strong></p>")
        for w in rep.source_warnings:
            a(f'<p class="asof">{_e(w)}</p>')
    a("</div>")

    # ── method ───────────────────────────────────────────────────────
    a('<h2>Method</h2><div class="card method">')
    for line in (
        "PnL = Δ(value + unclaimed fees) − net deposits/withdrawals + fees collected, per "
        "position between its last mark before the month and its last mark in it. Capital "
        "moving in or out is not performance.",
        f"Benchmark: {BENCHMARK}. Market = what those tokens did; IL = what providing "
        "liquidity did relative to holding them; excess vs benchmark = IL + fees earned − gas.",
        "Fees earned = fees collected + change in unclaimed fees (accrual), so a sweep on "
        "the 1st does not count the prior month's fees twice.",
        "Annualized yields divide by average capital, simple Dietz (start value + half the "
        "month's net flows), × 365 / days.",
        "Shares are of the tracked DEX book: custody balances (Phase 3) are not yet visible "
        "to this report, so it is not yet the whole balance sheet.",
        "A position is excluded — never estimated — when a term cannot be established; §9 "
        "lists each reason and the value it leaves out.",
    ):
        a(f"<p>{_e(line)}</p>")
    a("</div></div></body></html>")
    return "".join(out)


def report_path(rep: MandateReport, out_dir: str | Path = "reports") -> Path:
    suffix = "" if rep.complete else ".provisional"
    return Path(out_dir) / f"mandate-{rep.month}{suffix}.html"


def write_mandate_report(
    rep: MandateReport, out_dir: str | Path = "reports", force: bool = False
) -> Path:
    """Write the report. A final report is written once: rewriting it would
    restate a month already reported, so that takes `force`."""
    path = report_path(rep, out_dir)
    if rep.complete and path.exists() and not force:
        raise ReportExists(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_mandate_report(rep))
    return path
