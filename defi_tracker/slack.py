"""
Slack delivery — alerts and portfolio report.

Alert delivery:
  push_undelivered(storage) — POSTs each undelivered alert as a separate message
  and marks it delivered on success. Failures leave delivered_at NULL so the
  next run retries automatically.

Portfolio report:
  push_report(storage) — POSTs the full position snapshot as a single Block Kit
  message: header totals, per-chain tables (monospace code blocks), MTD summary,
  and a "Needs attention" section for out-of-range / near-edge positions.
  Sent on every run when SLACK_WEBHOOK_URL is set.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import requests

from defi_tracker.core.storage import Storage
from defi_tracker.report import (
    YIELD_WINDOW_DAYS,
    ReportData,
    gather_report_data,
    mtd_headline,
)

_log = logging.getLogger(__name__)

_SEVERITY_EMOJI = {"high": "🚨", "medium": "⚠️", "low": "ℹ️"}


class SlackDelivery:
    def __init__(self, webhook_url: str):
        self._webhook_url = webhook_url

    # ── Alert delivery (existing) ─────────────────────────────────────────

    # Alerts older than this are silently marked delivered without posting to Slack
    _MAX_ALERT_AGE_SECS = 48 * 3600

    def push_undelivered(self, storage: Storage) -> int:
        """
        Deliver all undelivered alerts to the Slack webhook.
        Alerts older than 48 hours are silently marked delivered (avoids spam after gaps).
        Returns the number of alerts successfully posted.
        """
        alerts = storage.undelivered_alerts()
        if not alerts:
            return 0

        now = datetime.now(UTC).timestamp()
        delivered = 0
        for alert in alerts:
            age = now - alert["triggered_at"]
            if age > self._MAX_ALERT_AGE_SECS:
                storage.mark_alert_delivered(alert["id"])
                _log.debug("Silently expiring stale alert %s (%.0fh old)", alert["id"], age / 3600)
                continue
            payload = self._format_alert(alert)
            try:
                r = requests.post(self._webhook_url, json=payload, timeout=10)
                r.raise_for_status()
                storage.mark_alert_delivered(alert["id"])
                delivered += 1
            except Exception as e:
                _log.warning("Slack delivery failed for alert %s: %s", alert["id"], e)
        return delivered

    def _format_alert(self, alert: dict) -> dict:
        emoji = _SEVERITY_EMOJI.get(alert["severity"], "•")
        ts_str = datetime.fromtimestamp(alert["triggered_at"], tz=UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        text = (
            f"{emoji} *{alert['kind']}* | `{alert['position_uid']}`\n{alert['message']}\n_{ts_str}_"
        )
        return {"text": text}

    # ── Portfolio report ──────────────────────────────────────────────────

    def push_report(self, storage: Storage, price_today: dict | None = None) -> bool:
        """
        POST the full portfolio snapshot as a Block Kit message.
        price_today ({token_key: current USD}) enables marking accumulated
        fees to today's prices alongside their at-harvest value.
        Returns True on success.
        """
        data = gather_report_data(storage, price_today)
        if data is None:
            return False

        try:
            payload = self._build_report_payload(data)
            r = requests.post(self._webhook_url, json=payload, timeout=15)
            r.raise_for_status()
            return True
        except Exception as e:
            _log.warning("Slack report delivery failed: %s", e)
            return False

    # ── Block Kit builders ────────────────────────────────────────────────

    def _build_report_payload(self, data: ReportData) -> dict:
        """Daily digest, structured around three questions: what changed
        (pulse line), what's earning (position table with realized yield),
        and what needs action (attention bullets with idle time)."""
        from defi_tracker.analytics import (
            attribute_fee_months,
            fee_valuation,
            health_warnings,
            pnl_decomposition,
            position_id,
            realized_closed_pnl,
            risk_summary,
            window_yields,
        )

        snaps = data.snaps
        # Fees by month is bucketed on fee-accrual month: a start-of-month sweep
        # is the prior month's fees, so the trend reads as fee generation.
        collect_rows = attribute_fee_months(data.collect_rows) if data.collect_rows else []
        value_delta = data.value_delta
        price_today = data.price_today
        total_value = sum(r["current_value_usd"] for r in snaps)
        # MTD is period PnL with flows netted out, exactly as the page shows it.
        mtd_head = mtd_headline(data.periods)
        total_mtd = float(mtd_head["pnl"]) if mtd_head else None
        earned_mtd = float(mtd_head["fees_earned"]) if mtd_head else None
        total_fees_mtd = float(mtd_head["fees_collected"]) if mtd_head else None
        today = datetime.now(UTC).strftime("%b %-d")

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"💼 Portfolio  {self._fmt_k(total_value)}  ·  {today}",
                },
            }
        ]

        # ── Pulse: what changed ───────────────────────────────────────────
        pulse: list[str] = []
        if value_delta is not None and value_delta.pct is not None:
            # Never "Δ day" unless the window really is a day — a chain that
            # missed a pass makes the baseline older than yesterday, and the
            # label has to move with it.
            span = value_delta.span_days
            head = "Δ day" if span == 1 else f"Δ {span}d"
            pulse.append(f"*{head}* {self._fmt_k(value_delta.delta)} ({value_delta.pct:+.2f}%)")
            if value_delta.excluded_positions:
                pulse.append(f"_excl. {value_delta.excluded_positions} stale_")
        if total_mtd is not None:
            cover = (
                f" _(flows netted · {mtd_head['covered_pct']:.0f}% of book)_"
                if mtd_head and mtd_head["covered_pct"] is not None
                else ""
            )
            pulse.append(f"*MTD PnL* {self._fmt_k(total_mtd)}{cover}")
        if earned_mtd is not None and total_fees_mtd is not None:
            # earned = accrual (collected + Δunclaimed); collected = cash.
            # A harvest sweep makes these very different — show both.
            pulse.append(
                f"*Fees MTD* {self._fmt_k(earned_mtd)} earned · "
                f"{self._fmt_k(total_fees_mtd)} collected"
            )
        pulse.append(f"{len(snaps)} positions")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "  ·  ".join(pulse)},
            }
        )

        # ── Drivers | Risk, side by side ──────────────────────────────────
        d = pnl_decomposition(snaps, data.tx_cost_rows)
        r = risk_summary(snaps)
        closed = realized_closed_pnl(data.flow_rows)
        book_net = float(d["net"]) + float(closed["realized_usd"])
        lp_vs_hodl = float(d["il"]) + float(d["fees_lifetime"]) + float(d["unclaimed"])
        drivers_field = (
            "*PnL drivers (lifetime)*\n"
            f"open · market  {self._fmt_k(float(d['market_move']))}\n"
            f"open · IL  {self._fmt_k(float(d['il']))}\n"
            f"open · fees  {self._fmt_k(float(d['fees_lifetime']))}"
            f"  ·  uncl {self._fmt_k(float(d['unclaimed']))}\n"
            f"closed · realized  {self._fmt_k(float(closed['realized_usd']))}"
            f" ({closed['positions']} pos)\n"
            f"{self._cost_lines(d)}"
            f"*= book net  {self._fmt_k(book_net)}*\n"
            # The verdict on the strategy: market move is exposure you'd have
            # had holding the tokens anyway, so it is an ALTERNATIVE to this
            # number, not a component of it — never render it as "of which".
            f"*LP vs holding  {self._fmt_k(lp_vs_hodl)}* _(open only)_"
        )
        risk_field = (
            "*Risk*\n"
            f"idle out-of-range  {self._fmt_k(float(r['out_of_range_value']))} "
            f"({r['out_of_range_pct']:.1f}% · {r['out_of_range_count']} pos)\n"
            f"top pair  {r['top_pair']}  {r['top_pair_pct']:.0f}% of book\n"
            f"largest position  {self._fmt_k(float(r['largest_position_value']))}"
        )
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": drivers_field},
                    {"type": "mrkdwn", "text": risk_field},
                ],
            }
        )

        # ── Fee trend: the shared fee table (analytics.fee_table) ─────────
        table = data.fee_table
        if collect_rows and table is not None:
            val = fee_valuation(collect_rows, price_today)
            priced = val["priced"]
            # The last six months that have a row, so trailing empty months
            # never crowd real ones off the front, and a Slack section stays
            # well under its 3000-char limit.
            window = table.rows[-6:]
            spark = self._sparkline([float(r.claimed) for r in table.rows[-12:]])
            order = table.chains
            clabel = {"peaq": "MachineX", "bsc": "Pancake", "base": "UniV3"}

            def _cell(row, c) -> str:
                if c in row.unknown:
                    return "?"
                v = float(row.cells.get(c, 0))
                if not v:
                    return "—"
                return self._fmt_k(v) + ("+" if c in row.partial else "")

            def _line(row) -> str:
                body = "".join(f"{_cell(row, c):>10}" for c in order)
                if row.status == "unswept":
                    tot, mtm = "unswept", "—"
                elif row.status == "rolled":
                    tot, mtm = "in later", "—"
                else:
                    tot = self._fmt_k(float(row.claimed)) + (
                        "+" if row.partial or row.unknown else ""
                    )
                    mtm = self._fmt_k(float(row.mtm)) if priced and row.mtm is not None else "—"
                mark = {"split": "§", "accruing": "*"}.get(row.status, "")
                return f"{row.label[:8] + mark:<9}{body}{tot:>10}{mtm:>9}"

            hdr = (
                f"{'month':<9}"
                + "".join(f"{clabel.get(c, c):>10}" for c in order)
                + f"{'claimed':>10}{'mtm':>9}"
            )
            rows_txt = [hdr, *(_line(r) for r in window)]
            if table.accruing:
                rows_txt.append(_line(table.accruing))
            rows_txt.append("─" * len(hdr))
            rows_txt += [_line(table.ytd), _line(table.lifetime)]
            pending_note = ""
            if table.accruing:
                pending_note += f"\n_* {table.accruing.note} — not yet claimed_"
            for frow in window:
                if frow.status in ("split", "rolled"):
                    pending_note += f"\n_{frow.label}: {frow.note}_"
            for n in table.notes:
                pending_note += f"\n_? {n}_"

            ct, td = float(val["claim_total"]), float(val["today_total"])
            # One reconciled fee story: lifetime = closed + open portions.
            # The closed share is the remainder; if it goes negative the two
            # totals have diverged, so print lifetime alone rather than an
            # equation that does not add up.
            open_fees = float(d["fees_lifetime"])
            closed_fees = ct - open_fees
            reconciles = closed_fees >= 0
            split_txt = (
                f"(open {self._fmt_k(open_fees)} + closed {self._fmt_k(closed_fees)})"
                if reconciles
                else ""
            )
            floor = "+" if table.lifetime.partial else ""
            headline = f"💰 *Fees by month* _(accrual)_  `{spark}`\n"
            headline += f"lifetime *{self._fmt_k(ct)}{floor}* claimed " + split_txt
            if priced:
                headline += f" · *{self._fmt_k(td)}* marked-to-market"
                if val["by_token_drop"]:
                    sym, tc, tt = val["by_token_drop"][0]
                    if float(tc) > 0 and float(tc) - float(tt) > 1000:
                        drop = (1 - float(tt) / float(tc)) * 100
                        headline += (
                            f"  ·  {sym} fees {self._fmt_k(float(tc))}→"
                            f"{self._fmt_k(float(tt))} (−{drop:.0f}%)"
                        )
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": headline + "\n```" + "\n".join(rows_txt) + "```" + pending_note,
                    },
                }
            )

        # ── What's earning: in-range positions with realized 30d yield ───
        yields = window_yields(
            data.window_stats, data.window_collects, window_days=YIELD_WINDOW_DAYS
        )
        earning = sorted(
            (row for row in snaps if row.get("in_range")),
            key=lambda row: row["current_value_usd"],
            reverse=True,
        )
        if earning:
            shown = earning[:8]
            lines = [f"{'Pair':<22} {'Value':>8} {'Range':>6} {'30d yld':>8}"]
            for row in shown:
                y = yields.get(position_id(row))
                yld = f"{y['apy_pct']:.0f}%" if y and y["apy_pct"] is not None else "—"
                lines.append(
                    f"{row['pair_label'][:22]:<22} "
                    f"{self._fmt_k(row['current_value_usd']):>8} "
                    f"{self._range_str(row):>6} {yld:>8}"
                )
            rest = earning[8:]
            if rest:
                rest_val = sum(row["current_value_usd"] for row in rest)
                lines.append(f"… +{len(rest)} more in range · {self._fmt_k(rest_val)}")
            in_range_val = sum(row["current_value_usd"] for row in earning)
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"🟢 *Earning — {self._fmt_k(in_range_val)} in range* "
                            f"_(30d yld = fees earned last {YIELD_WINDOW_DAYS}d ÷ avg value, annualized)_\n"
                            "```" + "\n".join(lines) + "```"
                        ),
                    },
                }
            )

        # ── Action queue — EV-ranked trade tickets (Phase 5.2) ───────────
        blocks.extend(self._ticket_blocks(data))

        # ── Mandate: one line, the same state the page's Mandate section shows ──
        m = data.mandate
        if m:
            checks = m.get("checks", [])
            n_breach = sum(1 for c in checks if c.status == "breach")
            n_unset = sum(1 for c in checks if c.status == "unset")
            unreviewed = sum(1 for e in m.get("exceptions", []) if not e["reviewed_at"])
            parts = [f"{n_breach} breach{'es' if n_breach != 1 else ''}"]
            if unreviewed:
                parts.append(f"{unreviewed} exception(s) unreviewed")
            if n_unset:
                parts.append(f"{n_unset} limit(s) not set")
            if m.get("policy_error"):
                parts = ["policy file INVALID — no limit evaluated"]
            ends = [
                (c["counterparty"], c["end_date"])
                for c in m.get("contracts", [])
                if c["status"] == "active" and c["end_date"]
            ]
            if ends:
                name, end = min(ends, key=lambda e: e[1])
                days = (datetime.fromisoformat(end).date() - datetime.now(UTC).date()).days
                parts.append(f"next expiry {name} in {days}d")
            elif not m.get("contracts"):
                parts.append("ledger not keyed")
            icon = "🚨" if n_breach or m.get("policy_error") else "📜"
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"{icon} *Mandate* — " + " · ".join(parts)},
                }
            )

        # ── Footnotes ─────────────────────────────────────────────────────
        notes: list[str] = health_warnings(data.health_rows)
        if closed["unverified_positions"]:
            notes.append(
                f"{closed['unverified_positions']} closed position(s) with no withdrawal on "
                f"record ({self._fmt_k(float(closed['unverified_usd']))} deposited) are excluded "
                "from realized PnL — missing data, not losses"
            )
        stale = self._stale_chains(snaps)
        for chain, as_of, age_days in stale:
            notes.append(
                f"⚠️ *{chain} marks are {age_days}d old* (as of {as_of} — "
                f"data source outage; values, Δ and alerts for {chain} are frozen there)"
            )
        unknown = sum(1 for row in snaps if row["il_usd"] is None and row["pnl_usd"] is None)
        if unknown:
            notes.append(f"IL/PnL figures exclude {unknown} position(s) with no event history")
        if notes:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "\n".join(notes)}],
                }
            )

        return {"blocks": blocks}

    @staticmethod
    def _stale_chains(snaps: list[dict]) -> list[tuple[str, str, int]]:
        """[(chain, as_of_date, age_days)] for chains whose latest snapshot
        lags the overall latest — i.e. their data-source pass has been failing
        and every number shown for them is frozen at as_of_date."""
        if not snaps:
            return []
        by_chain: dict[str, str] = {}
        for r in snaps:
            d = r["snapshot_date"]
            if r["chain"] not in by_chain or d > by_chain[r["chain"]]:
                by_chain[r["chain"]] = d
        newest = max(by_chain.values())
        out = []
        for chain, d in sorted(by_chain.items()):
            if d < newest:
                age = (
                    datetime.strptime(newest, "%Y-%m-%d").date()
                    - datetime.strptime(d, "%Y-%m-%d").date()
                ).days
                out.append((chain, d, age))
        return out

    def _ticket_blocks(self, data: ReportData) -> list[dict]:
        """The action queue at a glance: one line per pool (per leg where a
        pool has several) — action, NFT, deploy band vs spot, pool depth
        change, EV, conviction. The full ticket (ticks, amounts, drivers,
        the calculation) is on the desk, one link away."""
        from defi_tracker.analytics import DESK_URL, GROUP_ORDER, GROUP_TITLES, money, plan_cards

        # An empty queue must never read as "nothing to do": say whether it
        # was not computed, or which positions could not be scored.
        q = data.actions
        if q is None:
            return [
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*Action queue* · not computed this run"},
                },
            ]
        cards = plan_cards(q, data.snoozed_uids)
        shown = sum(len(c.steps) for c in cards)
        n_snoozed = len(q.tickets) - shown
        total = sum(c.total_usd for c in cards)
        # No desk link configured (DESK_URL unset): the pointer stays, unlinked.
        desk = f"<{DESK_URL}|full tickets on the desk>" if DESK_URL else "full tickets on the desk"
        head = (
            f"*Action queue* · {shown} tickets · EV {money(total, True)} / "
            f"{q.horizon_days:.0f}d · propose-only · {desk}"
        )
        if n_snoozed:
            head += f" · {n_snoozed} snoozed"
        if q.unscored:
            head += f" · {len(q.unscored)} position(s) not scored (unknown, not zero)"
        from defi_tracker.analytics import backtest_verdict, ticket_why

        verdict = backtest_verdict(data.backtest, {t.chain for t in q.tickets})
        blocks: list[dict] = [
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": head}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_{verdict}._"}]},
        ]
        if not cards:
            return blocks

        def leg(v) -> str:
            parts = [f"{v.action} {v.ref}" + (f" → {v.width}" if v.width else "")]
            dch = v.ticket.depth_change
            if abs(dch) >= 0.05:
                parts.append(f"depth {'+' if dch > 0 else '−'}{abs(dch):.0%}")
            parts.append(f"{v.ev}")
            parts.append(ticket_why(v.ticket, short=True))
            parts.append(v.conviction.upper())
            return " · ".join(parts)

        for group in GROUP_ORDER:
            group_cards = [c for c in cards if c.group == group]
            if not group_cards:
                continue
            title, sub = GROUP_TITLES[group]
            lines = [f"*{title}* _({sub})_"]
            for c in group_cards:
                if len(c.steps) == 1:
                    lines.append(f"• *{c.pair}* · {c.venue} — {leg(c.steps[0])}")
                else:
                    lines.append(
                        f"• *{c.pair}* · {c.venue} — {len(c.steps)} legs, EV "
                        f"{money(c.total_usd, True)}"
                    )
                    lines += [f"      {k}. {leg(v)}" for k, v in enumerate(c.steps, 1)]
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2990]}}
            )
        return blocks

    _SPARK_CHARS = "▁▂▃▄▅▆▇█"

    @classmethod
    def _sparkline(cls, values: list[float]) -> str:
        """Unicode sparkline scaled to the series max."""
        peak = max(values) if values else 0
        if peak <= 0:
            return "▁" * len(values)
        idx = [
            min(int(v / peak * (len(cls._SPARK_CHARS) - 1) + 0.5), len(cls._SPARK_CHARS) - 1)
            for v in values
        ]
        return "".join(cls._SPARK_CHARS[i] for i in idx)

    # ── Formatting helpers ────────────────────────────────────────────────

    @staticmethod
    def _cost_lines(d: dict) -> str:
        """Gas and slippage as their own lines above book net. Gas is already
        paid; leaving it out of the drivers made the book look better than the
        desk did. Partial coverage is stated inline — a gas total over 6 of 239
        transactions is a floor, and reading it as a total is the error this
        line exists to prevent."""
        costs = d.get("costs")
        if not costs or not costs["txs"]:
            return ""
        gas = "—" if costs["gas_usd"] is None else f"-${float(costs['gas_usd']):,.2f}"
        cover = (
            ""
            if costs["gas_is_complete"] or not costs["txs"]
            else f" _({costs['gas_known_txs']}/{costs['txs']} tx)_"
        )
        slip = "—" if costs["slippage_usd"] is None else f"-${float(costs['slippage_usd']):,.2f}"
        return f"costs · gas  {gas}{cover}  ·  slippage  {slip}\n"

    @staticmethod
    def _fmt_k(v) -> str:
        """Format a USD value as compact $k notation: $12.3k, -$4.5k, $0."""
        if v is None:
            return "    —"
        if not v:
            return "$0"  # an exact zero is a fact, not a rounded $0.0k
        sign = "-" if v < 0 else ""
        return f"{sign}${abs(v) / 1000:.1f}k"

    @staticmethod
    def _range_str(row: dict) -> str:
        tl = row.get("tick_lower")
        tu = row.get("tick_upper")
        tc = row.get("tick_current")
        if tl is None or tu is None or tc is None or tu == tl:
            return "—"
        if not row.get("in_range"):
            return "OUT"
        pct = min(tu - tc, tc - tl) / (tu - tl) * 100
        return f"IN{pct:.0f}%"
