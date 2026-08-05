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
from dataclasses import dataclass, field
from datetime import UTC, datetime

import requests

from defi_tracker.core.storage import Storage

_log = logging.getLogger(__name__)

_SEVERITY_EMOJI = {"high": "🚨", "medium": "⚠️", "low": "ℹ️"}

# Trailing window for realized yield (accrual basis) in the report
_YIELD_WINDOW_DAYS = 30


@dataclass
class ReportData:
    """Everything the daily report renders — gathered once in push_report."""

    snaps: list[dict]
    mtd_rows: list[dict]
    collect_rows: list[dict] = field(default_factory=list)
    day_delta: tuple[float, float] | None = None
    last_in_range: dict[str, str] = field(default_factory=dict)
    price_today: dict = field(default_factory=dict)
    flow_rows: list[dict] = field(default_factory=list)
    window_stats: list[dict] = field(default_factory=list)
    window_collects: list[dict] = field(default_factory=list)
    tick_vol: dict = field(default_factory=dict)
    snoozed_uids: set = field(default_factory=set)


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
        snaps = storage.latest_snapshot()
        if not snaps:
            return False

        from defi_tracker.analytics import pool_tick_volatility

        today = datetime.now(UTC).date()
        window_start = today.fromordinal(today.toordinal() - _YIELD_WINDOW_DAYS).isoformat()
        vol_start = today.fromordinal(today.toordinal() - 90)
        data = ReportData(
            snaps=snaps,
            mtd_rows=storage.month_to_date_pnl(),
            collect_rows=storage.monthly_collects(),
            day_delta=storage.day_over_day_value(),
            last_in_range=storage.last_in_range_dates(),
            price_today=price_today or {},
            flow_rows=storage.position_flows(),
            window_stats=storage.position_window_stats(_YIELD_WINDOW_DAYS),
            window_collects=storage.collects_since(window_start),
            tick_vol=pool_tick_volatility(storage.snapshots_in_range(start_date=vol_start)),
            snoozed_uids=storage.snoozed_rebalance_uids(),
        )

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
            fees_earned_mtd,
            monthly_fee_matrix,
            pnl_decomposition,
            realized_closed_pnl,
            rebalance_signals,
            risk_summary,
            window_yields,
        )

        snaps = data.snaps
        mtd_rows = data.mtd_rows
        # Fees by month is bucketed on fee-accrual month: a start-of-month sweep
        # is the prior month's fees, so the trend reads as fee generation.
        collect_rows = attribute_fee_months(data.collect_rows) if data.collect_rows else []
        day_delta = data.day_delta
        last_in_range = data.last_in_range
        price_today = data.price_today
        total_value = sum(r["current_value_usd"] for r in snaps)
        total_mtd = sum(r["pnl_mtd_usd"] or 0 for r in mtd_rows) if mtd_rows else None
        earned_mtd = float(fees_earned_mtd(mtd_rows)) if mtd_rows else None
        total_fees_mtd = sum(r["fees_collected_mtd"] or 0 for r in mtd_rows) if mtd_rows else None
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
        if day_delta and day_delta[1]:
            curr, prev = day_delta
            dv = curr - prev
            pulse.append(f"*Δ day* {self._fmt_k(dv)} ({dv / prev * 100:+.2f}%)")
        if total_mtd is not None:
            pulse.append(f"*MTD PnL* {self._fmt_k(total_mtd)}")
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
        d = pnl_decomposition(snaps)
        r = risk_summary(snaps)
        closed = realized_closed_pnl(data.flow_rows)
        book_net = float(d["net"]) + float(closed["realized_usd"])
        drivers_field = (
            "*PnL drivers (lifetime)*\n"
            f"open · market  {self._fmt_k(float(d['market_move']))}\n"
            f"open · IL  {self._fmt_k(float(d['il']))}\n"
            f"open · fees  {self._fmt_k(float(d['fees_lifetime']))}"
            f"  ·  uncl {self._fmt_k(float(d['unclaimed']))}\n"
            f"closed · realized  {self._fmt_k(float(closed['realized_usd']))}"
            f" ({closed['positions']} pos)\n"
            f"*= book net  {self._fmt_k(book_net)}*"
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

        # ── Fee trend: MoM at harvest vs held-to-today ────────────────────
        if collect_rows:
            val = fee_valuation(collect_rows, price_today)
            window = val["months"][-6:]
            claim_series = [float(val["claim_by_month"].get(m, 0)) for m in window]
            if claim_series:
                spark = self._sparkline(
                    [float(val["claim_by_month"].get(m, 0)) for m in val["months"][-12:]]
                )
                priced = val["priced"]
                # Per-chain split: MachineX = peaq, PancakeSwap (Pancake) = bsc.
                _, chains, cells = monthly_fee_matrix(collect_rows)
                order = [c for c in ("peaq", "bsc") if c in chains] + [
                    c for c in chains if c not in ("peaq", "bsc")
                ]
                clabel = {"peaq": "MachineX", "bsc": "Pancake"}

                def _fee_row(label, mvals, claimed, mtm):
                    body = "".join(f"{(self._fmt_k(v) if v else '—'):>10}" for v in mvals)
                    mtm_s = self._fmt_k(mtm) if priced else "—"
                    return f"{label:<9}{body}{self._fmt_k(claimed):>10}{mtm_s:>9}"

                hdr = (
                    f"{'month':<9}"
                    + "".join(f"{clabel.get(c, c):>10}" for c in order)
                    + f"{'claimed':>10}{'mtm':>9}"
                )
                rows_txt = [hdr]
                for m in window:
                    rows_txt.append(
                        _fee_row(
                            m,
                            [float(cells.get((m, c), 0)) for c in order],
                            float(val["claim_by_month"].get(m, 0)),
                            float(val["today_by_month"].get(m, 0)),
                        )
                    )
                # Current accrual month is still pre-sweep: show live unclaimed
                # (fees earned since the last harvest) as MTD-so-far, marked *.
                cur_month = datetime.now(UTC).strftime("%Y-%m")
                pending_note = ""
                if cur_month not in val["months"]:
                    unc_by_chain: dict[str, float] = {}
                    for sn in snaps:
                        u = float(sn.get("unclaimed_usd") or 0)
                        if u:
                            unc_by_chain[sn["chain"]] = unc_by_chain.get(sn["chain"], 0.0) + u
                    unc = sum(unc_by_chain.values())
                    if unc > 0:
                        rows_txt.append(
                            _fee_row(
                                cur_month + "*", [unc_by_chain.get(c, 0.0) for c in order], unc, unc
                            )
                        )
                        pending_note = f"\n_* {cur_month} = unclaimed so far (accruing, pre-sweep)_"
                # YTD + lifetime summary rows.
                rows_txt.append("─" * len(hdr))
                year = datetime.now(UTC).strftime("%Y")
                ytd_m = [m for m in val["months"] if m.startswith(year)]
                rows_txt.append(
                    _fee_row(
                        f"YTD {year}",
                        [sum(float(cells.get((m, c), 0)) for m in ytd_m) for c in order],
                        sum(float(val["claim_by_month"].get(m, 0)) for m in ytd_m),
                        sum(float(val["today_by_month"].get(m, 0)) for m in ytd_m),
                    )
                )
                rows_txt.append(
                    _fee_row(
                        "Lifetime",
                        [sum(float(cells.get((m, c), 0)) for m in val["months"]) for c in order],
                        float(val["claim_total"]),
                        float(val["today_total"]),
                    )
                )
                ct, td = float(val["claim_total"]), float(val["today_total"])
                # One reconciled fee story: lifetime = closed + open portions,
                # then the held-to-today haircut.
                open_fees = float(d["fees_lifetime"])
                closed_fees = float(closed["fees_usd"])
                headline = f"💰 *Fees by month* _(accrual)_  `{spark}`\n"
                if priced:
                    headline += (
                        f"lifetime *{self._fmt_k(ct)}* claimed "
                        f"(open {self._fmt_k(open_fees)} + closed {self._fmt_k(closed_fees)})"
                        f" · *{self._fmt_k(td)}* marked-to-market"
                    )
                    if val["by_token_drop"]:
                        sym, tc, tt = val["by_token_drop"][0]
                        if float(tc) > 0 and float(tc) - float(tt) > 1000:
                            drop = (1 - float(tt) / float(tc)) * 100
                            headline += (
                                f"  ·  {sym} fees {self._fmt_k(float(tc))}→"
                                f"{self._fmt_k(float(tt))} (−{drop:.0f}%)"
                            )
                else:
                    headline += (
                        f"lifetime *{self._fmt_k(ct)}* claimed "
                        f"(open {self._fmt_k(open_fees)} + closed {self._fmt_k(closed_fees)})"
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
            data.window_stats, data.window_collects, window_days=_YIELD_WINDOW_DAYS
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
                y = yields.get((row["wallet"], row["position_key"]))
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
                            f"_(30d yld = fees earned last {_YIELD_WINDOW_DAYS}d ÷ avg value, annualized)_\n"
                            "```" + "\n".join(lines) + "```"
                        ),
                    },
                }
            )

        # ── What needs action — economic rebalance signals ────────────────
        signals = rebalance_signals(snaps, yields, last_in_range, tick_vol=data.tick_vol)
        # Hide deferred (snoozed) items so they stop nagging until their timer lapses.
        shown = [s for s in signals if s["uid"] not in data.snoozed_uids]
        n_snoozed = sum(
            1 for s in signals if s["uid"] in data.snoozed_uids and s["tier"] in ("now", "soon")
        )
        attention = self._attention_lines(shown)
        if attention:
            n_act = sum(1 for s in shown if s["tier"] in ("now", "soon"))
            head = f"🚨 *Needs action ({n_act})*"
            if n_snoozed:
                head += f"  ·  _{n_snoozed} snoozed_"
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": head + "\n" + "\n".join(attention)},
                }
            )

        # ── Footnotes ─────────────────────────────────────────────────────
        notes: list[str] = []
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

    _TIER_ICON = {"now": "🔴", "soon": "🟠", "watch": "🟡"}

    def _attention_lines(self, signals: list[dict]) -> list[str]:
        """Render rebalance signals (already tiered/sorted now→soon→watch) into
        Slack lines: idle $, how far beyond the range, days out when known, and
        the forgone-fee estimate — a decision, not a nag."""
        lines: list[str] = []
        for s in signals:
            icon = self._TIER_ICON.get(s["tier"], "🟡")
            bits = [f"{self._fmt_k(s['idle_usd'])} idle", f"{s['depth'] * 100:.0f}% past edge"]
            if s.get("days_out"):
                bits.append(f"out {s['days_out']}d")
            if s.get("forgone_usd") and s["forgone_usd"] >= 1:
                bits.append(f"~{self._fmt_k(s['forgone_usd'])}/yr forgone")
            line = f"{icon} *{s['pair']}* — " + " · ".join(bits)
            sug = s.get("suggest")
            if sug:
                line += (
                    f"\n     ↳ retarget ±{sug['half_pct']:.0f}% (now ±{s['current_half_pct']:.0f}%)"
                )
            lines.append(line)
        return lines

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
    def _fmt_k(v) -> str:
        """Format a USD value as compact $k notation: $136.6k, -$7.1k, $0.0k."""
        if v is None:
            return "    —"
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
