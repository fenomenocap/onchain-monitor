"""
The runner — orchestrates a full tracking pass.

Lifecycle per invocation:
  1. Load wallets from storage
  2. For each (wallet, chain, adapter):
       a. Fetch incremental events since last sync
       b. Persist events
       c. Snapshot current positions
       d. Compute cost basis, IL, PnL from event history
       e. Write daily snapshot
       f. Evaluate alerts; dedup via storage
  3. Optionally: push alerts to Slack
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

from defi_tracker.alerts import AlertConfig, PrevPositionState, evaluate_alerts, near_edge_pct
from defi_tracker.analytics import (
    DEFAULT_REBALANCE_CONFIG,
    RebalanceConfig,
    compute_cost_basis,
    compute_pnl,
    pool_tick_volatility,
    rebalance_signals,
    window_yields,
)
from defi_tracker.core.adapter import ProtocolAdapter, adapters_for_chain, all_adapters
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Alert, AlertSeverity, Chain, Position

_log = logging.getLogger(__name__)
# Soft per-(wallet, chain, adapter) time budget, checked between event batches.
# Cooperative by design: every network call inside a batch is itself bounded
# by requests timeouts, so a pass can run long but never hang — and progress
# made before the budget expires is already persisted.
_ADAPTER_TIME_BUDGET = 540  # seconds
# Extra slack before the runner force-stops an adapter that ignores its
# deadline — long enough that adapters honoring the deadline always stop
# themselves first (and bank their cursor).
_DEADLINE_GRACE = 120  # seconds


class TrackerRunner:
    def __init__(
        self,
        storage: Storage,
        alert_config: AlertConfig | None = None,
        dedupe_hours: int = 24,
        rebalance_config: RebalanceConfig | None = None,
    ):
        self.storage = storage
        self.alert_config = alert_config if alert_config is not None else AlertConfig()
        self.dedupe_hours = dedupe_hours
        self.rebalance_config = rebalance_config or DEFAULT_REBALANCE_CONFIG

    # ── Single-adapter pass ───────────────────────────────────────────────

    def sync_adapter(
        self,
        adapter: ProtocolAdapter,
        wallet: str,
        chain: Chain,
        deadline: float | None = None,
    ) -> int:
        """Fetch + persist new events, one batch at a time. Returns number inserted.

        Each batch is persisted before the next is requested, so incremental
        adapters can advance their scan cursor per batch. `deadline` (a
        time.monotonic() value) stops consuming between batches; the adapter
        resumes from its cursor on the next run.
        """
        if not adapter.handles(chain):
            return 0
        pid = adapter.info.protocol_id
        since_ts = self.storage.get_last_synced_ts(wallet, chain, pid)
        inserted = 0
        got_any = False
        # The deadline is passed to the adapter so incremental scanners stop
        # cleanly after banking their cursor. The fallback below only fires
        # for adapters that ignore the deadline — with a grace period, so a
        # well-behaved adapter is never abandoned mid-iteration (which would
        # lose its final cursor advance).
        for batch in adapter.iter_event_batches(
            wallet, chain, since_ts=since_ts, deadline=deadline
        ):
            inserted += self.storage.upsert_events(batch)
            if batch:
                got_any = True
                # Only advance the watermark when we have events — ensures an
                # empty response (e.g. subgraph down) never skips a sync window.
                # set_last_synced_ts keeps the MAX, so per-batch calls are safe.
                self.storage.set_last_synced_ts(wallet, chain, pid, max(ev.ts for ev in batch))
            if deadline is not None and time.monotonic() > deadline + _DEADLINE_GRACE:
                _log.warning(
                    "Adapter %s@%s ignored its time budget for %s — force-stopping; "
                    "it resumes from its cursor next run",
                    pid,
                    chain.value,
                    wallet[:10],
                )
                break
        if not got_any:
            _log.debug(
                "No events returned for %s/%s/%s since ts=%d",
                wallet[:10],
                chain,
                pid,
                since_ts,
            )
        return inserted

    def snapshot_adapter(
        self,
        adapter: ProtocolAdapter,
        wallet: str,
        chain: Chain,
    ) -> tuple[list[Position], list[Alert], list[str]]:
        """
        Take a current snapshot, derive PnL from events, write to storage,
        evaluate alerts. Returns (positions, fresh_alerts, price_gap_count).
        """
        if not adapter.handles(chain):
            return [], [], []

        positions = adapter.fetch_positions(wallet, chain)
        fresh_alerts: list[Alert] = []
        missing_prices: list[str] = []

        # fetch_positions succeeded (adapters raise on outage), so anything
        # previously open but absent from this pass was closed on-chain —
        # tombstone it so reports stop carrying it forever.
        current_keys = {p.position_key for p in positions}
        pid = adapter.info.protocol_id
        for prior in self.storage.open_position_identities(wallet, chain, pid):
            if prior["position_key"] not in current_keys:
                _log.info(
                    "Position closed: %s/%s/%s %s",
                    wallet[:10],
                    chain.value,
                    pid,
                    prior["pair_label"],
                )
                self.storage.write_tombstone_snapshot(
                    wallet=wallet,
                    chain=chain,
                    protocol_id=pid,
                    position_key=prior["position_key"],
                    pair_label=prior["pair_label"],
                    protocol_kind=prior["protocol_kind"],
                )

        for pos in positions:
            # 1. Cost basis from event history
            cb = compute_cost_basis(
                self.storage,
                wallet,
                chain,
                pos.protocol_id,
                pos.position_key,
            )

            # 2. Populate opened_at / last_event_at from event history so that
            #    NEGATIVE_CARRY and STALE_POSITION alerts can evaluate correctly.
            first_ts, last_ts = self.storage.event_ts_bounds(
                wallet, chain, pos.protocol_id, pos.position_key
            )
            if first_ts is not None:
                pos.opened_at = first_ts
            if last_ts is not None:
                pos.last_event_at = last_ts

            # 3. Current prices for HODL math
            current_prices, missing = self._current_price_map(adapter, pos)
            missing_prices.extend(missing)

            # 4. PnL roll-up — only when event history exists. Without it,
            #    cost basis is UNKNOWN, not zero: computing IL/PnL anyway
            #    would report the position's whole value as profit and IL.
            pnl = compute_pnl(pos, cb, current_prices) if cb.has_events else None

            # 5. Previous state for transition detection — must be read
            #    BEFORE today's snapshot replaces it as the latest row.
            prev_state = self._prev_position_state(wallet, chain, pid, pos.position_key)

            # 6. Persist snapshot (NULL analytics when unknown)
            self.storage.write_snapshot(
                position=pos,
                cost_basis_usd=cb.cost_basis_usd if pnl else None,
                hold_value_usd=pnl.hold_value_usd if pnl else None,
                il_usd=pnl.il_usd if pnl else None,
                pnl_usd=pnl.pnl_usd if pnl else None,
            )

            # 7. Evaluate alerts. Transitions dedupe on dedupe_hours as
            #    before; steady-state repeats (transition=False) wait
            #    reminder_days — or fire exactly once if reminders are off.
            for alert in evaluate_alerts(pos, pnl, self.alert_config, prev=prev_state):
                if alert.kind not in self.alert_config.push_kinds:
                    continue  # retired from the push path (see AlertConfig.push_kinds)
                transition = bool((alert.context or {}).get("transition", True))
                if transition:
                    window_h = self.dedupe_hours
                elif self.alert_config.reminder_days > 0:
                    window_h = self.alert_config.reminder_days * 24
                else:
                    window_h = 10 * 365 * 24  # reminders off: first occurrence only
                if self.storage.recently_alerted(alert.position_uid, alert.kind, window_h):
                    continue
                self.storage.write_alert(alert)
                fresh_alerts.append(alert)

        return positions, fresh_alerts, missing_prices

    def _prev_position_state(
        self, wallet: str, chain: Chain, protocol_id: str, position_key: str
    ) -> PrevPositionState | None:
        """Reconstruct alert-relevant state from the previous snapshot row."""
        row = self.storage.latest_snapshot_row(wallet, chain, protocol_id, position_key)
        if row is None:
            return None
        in_range = bool(row["in_range"]) if row["in_range"] is not None else None
        edge_pct = near_edge_pct(row["tick_lower"], row["tick_upper"], row["tick_current"])
        near_edge = None
        if in_range is not None:
            near_edge = (
                in_range
                and edge_pct is not None
                and edge_pct < self.alert_config.edge_proximity_pct
            )
        il_pct = Decimal(str(row["il_pct"])) if row["il_pct"] is not None else None
        return PrevPositionState(in_range=in_range, near_edge=near_edge, il_pct=il_pct)

    _REBALANCE_WINDOW_DAYS = 30

    def compute_rebalance_signals(self) -> list[dict]:
        """Current rebalance signals from the latest snapshots (yields +
        realized-vol targets). Pure read — shared by the alert pass and the
        CLI queue view so both see the same tiers."""
        snaps = self.storage.latest_snapshot()
        if not snaps:
            return []
        today = datetime.now(UTC).date()
        since = today.fromordinal(today.toordinal() - self._REBALANCE_WINDOW_DAYS).isoformat()
        yields = window_yields(
            self.storage.position_window_stats(self._REBALANCE_WINDOW_DAYS),
            self.storage.collects_since(since),
            window_days=self._REBALANCE_WINDOW_DAYS,
        )
        tick_vol = pool_tick_volatility(
            self.storage.snapshots_in_range(start_date=today.fromordinal(today.toordinal() - 90)),
            min_points=self.rebalance_config.vol_min_points,
        )
        return rebalance_signals(
            snaps,
            yields,
            self.storage.last_in_range_dates(),
            self.rebalance_config,
            tick_vol=tick_vol,
        )

    def _emit_rebalance_alerts(self) -> int:
        """Reconcile the rebalance action queue, then emit a REBALANCE alert for
        every 🔴 ('now') position that is open (not snoozed). Persistent
        condition, so deduped like NEGATIVE_CARRY: first occurrence fires, then
        reminds every reminder_days. Returns the count of alerts written."""
        signals = self.compute_rebalance_signals()
        if not signals:
            return 0
        # Keep the action queue in sync: tracks now+soon, auto-resolves anything
        # no longer flagged (a completed rebalance drops off next run).
        actionable = [s for s in signals if s["tier"] in ("now", "soon")]
        self.storage.reconcile_rebalance_queue(actionable)

        now = int(datetime.now(UTC).timestamp())
        written = 0
        for sig in signals:
            if sig["tier"] != "now":
                continue
            uid = sig["uid"]
            # A snoozed item is deferred — don't nag until its timer lapses.
            if self.storage.is_rebalance_snoozed(uid):
                continue
            # Persistent condition → reminder cadence (transition=False).
            window_h = (
                self.alert_config.reminder_days * 24
                if self.alert_config.reminder_days > 0
                else 10 * 365 * 24
            )
            if self.storage.recently_alerted(uid, "REBALANCE", window_h):
                continue
            days = f", out {sig['days_out']}d" if sig.get("days_out") else ""
            sug = sig.get("suggest")
            target = (
                f" Suggest retarget to ±{sug['half_pct']:.0f}% "
                f"(ticks {sug['lower']}–{sug['upper']}), now ±{sig['current_half_pct']:.0f}%."
                if sug
                else ""
            )
            self.storage.write_alert(
                Alert(
                    position_uid=uid,
                    kind="REBALANCE",
                    severity=AlertSeverity.HIGH,
                    message=(
                        f"{sig['pair']}: ${sig['idle_usd']:,.0f} idle, "
                        f"{sig['depth'] * 100:.0f}% past range edge{days}, not earning. "
                        f"Rebalance to redeploy (~${sig['forgone_usd']:,.0f}/yr forgone).{target}"
                    ),
                    triggered_at=now,
                    context={
                        "idle_usd": sig["idle_usd"],
                        "depth": sig["depth"],
                        "days_out": sig["days_out"],
                        "forgone_usd": sig["forgone_usd"],
                        "suggest": sug,
                        "transition": False,
                    },
                )
            )
            written += 1
        return written

    def _current_price_map(
        self, adapter: ProtocolAdapter, position: Position
    ) -> tuple[dict[str, Decimal], list[str]]:
        """Build {token_key: current_price_usd} for this position's tokens.
        Returns (prices, missing_tokens) where missing_tokens are 'SYMBOL (key)' strings."""
        prices: dict[str, Decimal] = {}
        missing: list[str] = []
        for token in position.tokens:
            pp = adapter.get_current_price(token)
            if pp:
                prices[token.key] = pp.price_usd
            else:
                label = f"{token.symbol} ({token.key})"
                missing.append(label)
                _log.warning(
                    "No current price for %s (%s) — IL/PnL for position %s will be understated",
                    token.symbol,
                    token.key,
                    position.position_key,
                )
        return prices, missing

    def _sync_and_snapshot(
        self, adapter: ProtocolAdapter, wallet: str, chain: Chain
    ) -> tuple[int, list[Position], list[Alert], list[str], str | None]:
        """Sync events, then snapshot. A sync failure is reported (last tuple
        element) but does NOT skip the snapshot: the event watermark hasn't
        advanced, so events backfill once the source recovers, while
        fetch_positions may still succeed (e.g. via an RPC fallback) and keep
        marks fresh. snapshot_adapter failures still propagate — a missing
        snapshot is the loud signal that marks are stale."""
        deadline = time.monotonic() + _ADAPTER_TIME_BUDGET
        new_ev = 0
        sync_error: str | None = None
        try:
            new_ev = self.sync_adapter(adapter, wallet, chain, deadline=deadline)
        except Exception as e:
            sync_error = f"event sync failed: {e}"
            _log.warning(
                "Event sync failed for %s@%s (%s) — snapshotting anyway",
                adapter.info.protocol_id,
                chain.value,
                e,
            )
        positions, alerts, missing_prices = self.snapshot_adapter(adapter, wallet, chain)
        return new_ev, positions, alerts, missing_prices, sync_error

    # ── Full pass ─────────────────────────────────────────────────────────

    def run(
        self,
        wallets: list[str] | None = None,
        chains: list[Chain] | None = None,
        protocols: list[str] | None = None,
    ) -> dict:
        """
        Run a full sync + snapshot + alert pass.

        All filters are optional — pass None for "everything in scope".
        Returns a summary dict for the CLI to render.
        """
        # Wallets
        if wallets:
            wallet_list = [w.lower() for w in wallets]
        else:
            wallet_list = [w["address"] for w in self.storage.list_wallets()]

        # Chains
        chain_list = chains or list(Chain)

        # Adapters
        adapter_list = all_adapters()
        if protocols:
            adapter_list = [a for a in adapter_list if a.info.protocol_id in protocols]

        # Per-adapter bucket: nested dict, mypy needs the inner type spelled out.
        per_adapter: dict[str, dict[str, list[str] | int]] = {}
        wallets_scanned = 0
        events_inserted = 0
        positions_snapshotted = 0
        alerts_raised = 0
        price_gap_tokens: list[str] = []
        started_at = int(datetime.now(UTC).timestamp())

        for wallet in wallet_list:
            wallets_scanned += 1
            for chain in chain_list:
                for adapter in adapters_for_chain(chain):
                    if adapter not in adapter_list:
                        continue
                    pid = adapter.info.protocol_id
                    key = f"{pid}@{chain.value}"

                    try:
                        new_ev, positions, alerts, missing, sync_error = self._sync_and_snapshot(
                            adapter, wallet, chain
                        )
                    except Exception as e:
                        _log.warning("Adapter %s failed: %s", key, e)
                        err_bucket = per_adapter.setdefault(
                            key, {"events": 0, "positions": 0, "alerts": 0, "errors": []}
                        )
                        errors_list = err_bucket["errors"]
                        assert isinstance(errors_list, list)
                        errors_list.append(str(e))
                        continue

                    bucket = per_adapter.setdefault(
                        key,
                        {"events": 0, "positions": 0, "alerts": 0, "errors": []},
                    )
                    if sync_error:
                        sync_errors_list = bucket["errors"]
                        assert isinstance(sync_errors_list, list)
                        sync_errors_list.append(sync_error)
                    # All numeric fields are int by construction
                    bucket["events"] = int(bucket["events"]) + new_ev  # type: ignore[arg-type]
                    bucket["positions"] = int(bucket["positions"]) + len(positions)  # type: ignore[arg-type]
                    bucket["alerts"] = int(bucket["alerts"]) + len(alerts)  # type: ignore[arg-type]

                    events_inserted += new_ev
                    positions_snapshotted += len(positions)
                    alerts_raised += len(alerts)
                    price_gap_tokens.extend(missing)

        # Portfolio-wide rebalance alerts — fired on the 🔴 tier only, once all
        # snapshots for today are written (the signal needs cross-position
        # yields + last-in-range, so it can't live in the per-adapter loop).
        alerts_raised += self._emit_rebalance_alerts()

        # Deduplicate missing tokens across all positions/adapters
        seen: set[str] = set()
        deduped: list[str] = []
        for t in price_gap_tokens:
            if t not in seen:
                seen.add(t)
                deduped.append(t)

        return {
            "started_at": started_at,
            "finished_at": int(datetime.now(UTC).timestamp()),
            "wallets_scanned": wallets_scanned,
            "events_inserted": events_inserted,
            "positions_snapshotted": positions_snapshotted,
            "alerts_raised": alerts_raised,
            "price_gap_tokens": deduped,
            "per_adapter": per_adapter,
        }
