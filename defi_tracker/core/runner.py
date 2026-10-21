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
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal

from defi_tracker.alerts import AlertConfig, PrevPositionState, evaluate_alerts, near_edge_pct
from defi_tracker.analytics import (
    DEFAULT_REBALANCE_CONFIG,
    RebalanceConfig,
    compute_cost_basis,
    compute_pnl,
)
from defi_tracker.core.adapter import ProtocolAdapter, adapters_for_chain, all_adapters
from defi_tracker.core.endpoints import redact
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Alert, AlertSeverity, Chain, Position

_log = logging.getLogger(__name__)
# Soft per-(wallet, chain, adapter) time budget, checked between event batches.
# Cooperative by design: every network call inside a batch is itself bounded
# by requests timeouts, so a pass can run long but never hang — and progress
# made before the budget expires is already persisted.
_ADAPTER_TIME_BUDGET = 540  # seconds
_POOL_FLOW_BUDGET = 180  # seconds a run may spend banking new swaps (Phase 5.3)
# Extra slack before the runner force-stops an adapter that ignores its
# deadline — long enough that adapters honoring the deadline always stop
# themselves first (and bank their cursor).
_DEADLINE_GRACE = 120  # seconds


@dataclass(frozen=True)
class AdapterPass:
    """Everything one (wallet, chain, adapter) pass produced.

    A dataclass rather than a widening tuple: this already carried five
    positional values and the sixth (dropped) is the one a reader is most
    likely to mis-order against events_inserted.
    """

    events_inserted: int
    events_dropped: int
    positions: list[Position]
    alerts: list[Alert]
    missing_prices: list[str]
    sync_error: str | None


def _new_adapter_bucket() -> dict[str, list[str] | int]:
    """The per-adapter summary bucket. One constructor because the shape is
    built on both the success and the failure path, and a key added to only
    one of them is a silent KeyError waiting for the next outage."""
    return {"events": 0, "dropped": 0, "positions": 0, "alerts": 0, "errors": []}


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
        mark_ts: int | None = None,
    ) -> tuple[list[Position], list[Alert], list[str]]:
        """
        Take a current snapshot, derive PnL from events, write to storage,
        evaluate alerts. Returns (positions, fresh_alerts, price_gap_tokens).

        `mark_ts` stamps this pass's row in `marks`. Passing one timestamp for
        the whole run makes the book's positions a single cross-sectional
        observation rather than a smear across however long the pass took.
        """
        if not adapter.handles(chain):
            return [], [], []

        positions = adapter.fetch_positions(wallet, chain)
        fresh_alerts: list[Alert] = []
        missing_prices: list[str] = []

        # Mark the book before anything derived is computed. A mark is just
        # "what was this worth, when" — it costs one write, it cannot fail in
        # a way that should abort the pass, and it is the only record of the
        # day that survives a second pass overwriting today's snapshot row.
        self.storage.write_marks(positions, ts=mark_ts, source=self._positions_source(positions))

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

    def compute_action_queue(self):
        """The EV-ranked action queue from the latest snapshots (Phase 5.2).
        Pure read — shared by the alert pass and the CLI queue view, so both
        see the same tickets."""
        from defi_tracker.report import gather_action_queue

        return gather_action_queue(
            self.storage, self.storage.latest_snapshot(), self._effective_rebalance_config()
        )

    def _effective_rebalance_config(self) -> RebalanceConfig:
        """The config given, with ε measured from swap history when it has
        been (report.effective_rebalance_config)."""
        from defi_tracker.report import effective_rebalance_config

        return effective_rebalance_config(self.storage, self.rebalance_config)

    def reconstruct_day(self, adapter: ProtocolAdapter, wallet: str, chain: Chain, day) -> int:
        """Fill one missing snapshot day from archive state.

        Positions are read at the chain's last block of `day` (UTC) and valued
        at that day's prices; cost basis counts only the events up to that
        moment. Written with source 'archive' so a reconstructed row is always
        distinguishable from one observed live. Never overwrites: a day that
        already has a snapshot is not a hole. Returns rows written."""
        from datetime import time as dtime

        from defi_tracker.adapters._base import end_of_day_price_ts

        existing = self.storage.snapshots_as_of(day)
        pid = adapter.info.protocol_id
        if any(
            r["snapshot_date"] == day.isoformat()
            and r["wallet"] == wallet.lower()
            and r["chain"] == chain.value
            and r["protocol_id"] == pid
            for r in existing
        ):
            return 0
        ts = int(datetime.combine(day, dtime(23, 59, 59), tzinfo=UTC).timestamp())
        block = adapter.block_at(chain, ts)
        positions = adapter.fetch_positions_at(wallet, chain, block, ts)
        written = 0
        for pos in positions:
            cb = compute_cost_basis(
                self.storage, wallet, chain, pos.protocol_id, pos.position_key, until_ts=ts
            )
            prices: dict[str, Decimal] = {}
            for tok in pos.tokens:
                pp = adapter.get_historical_price(tok, end_of_day_price_ts(ts))
                if pp is not None:
                    prices[tok.key] = pp.price_usd
            pnl = compute_pnl(pos, cb, prices) if cb.has_events else None
            self.storage.write_snapshot(
                position=pos,
                snapshot_date=day,
                cost_basis_usd=cb.cost_basis_usd if pnl else None,
                hold_value_usd=pnl.hold_value_usd if pnl else None,
                il_usd=pnl.il_usd if pnl else None,
                pnl_usd=pnl.pnl_usd if pnl else None,
            )
            written += 1
        return written

    def _refresh_elasticity(self) -> None:
        """Bank the swaps since the last pass and re-measure ε, before the
        queue is scored (Phase 5.3). Never fails the run: a scan or fit that
        breaks leaves the last estimate standing, and says so in the log."""
        from defi_tracker.analytics import estimate_volume_elasticity
        from defi_tracker.poolflow import history_source, pool_infos, sync_pool_history
        from defi_tracker.report import ELASTICITY_KEY

        deadline = time.monotonic() + _POOL_FLOW_BUDGET
        banked = False
        chains: list[str] = []
        for adapter in all_adapters():
            for chain in adapter.info.chains:
                if history_source(adapter, chain) is None:
                    continue
                chains.append(chain.value)
                try:
                    res = sync_pool_history(self.storage, adapter, chain, deadline=deadline)
                    banked = banked or res.windows > 0
                except Exception as e:
                    _log.warning("Swap history for %s not advanced: %s", chain.value, e)
        if not banked:
            return
        try:
            infos: dict = {}
            for c in chains:
                infos.update(pool_infos(self.storage, c))
            est = estimate_volume_elasticity(self.storage.pool_days(), infos)
            self.storage.kv_set(ELASTICITY_KEY, est.to_json())
        except Exception as e:
            _log.warning("ε not re-measured — the last estimate stands: %s", e)

    def _emit_policy_alerts(self) -> int:
        """Evaluate the book against the mandate policy and alert on breaches.

        A malformed policy file is itself alerted, not raised: a typo must not
        stop the daily snapshot, and must not quietly read as "no limits"."""
        from defi_tracker.alerts import policy_breach_alerts
        from defi_tracker.analytics import counterparty_exposure, policy_checks
        from defi_tracker.policy import PolicyError, fetch_depeg_prices, load_policy

        now = int(datetime.now(UTC).timestamp())
        try:
            policy = load_policy()
        except PolicyError as e:
            _log.error("Policy file invalid: %s", e)
            if self.storage.recently_alerted("policy:file", "POLICY_BREACH", 24):
                return 0
            self.storage.write_alert(
                Alert(
                    position_uid="policy:file",
                    kind="POLICY_BREACH",
                    severity=AlertSeverity.HIGH,
                    message=f"Policy file invalid — limits NOT evaluated: {e}",
                    triggered_at=now,
                    context={"transition": True},
                )
            )
            return 1
        if not policy.values and not policy.counterparty_caps:
            return 0
        snaps = self.storage.latest_snapshot()
        today = datetime.now(UTC).date()
        exposure = counterparty_exposure(self.storage.contracts_as_of(today), today)
        checks = policy_checks(snaps, policy, fetch_depeg_prices(policy), exposure)
        written = 0
        reminder_h = (
            self.alert_config.reminder_days * 24
            if self.alert_config.reminder_days > 0
            else 10 * 365 * 24
        )
        # Open episodes BEFORE reconciling: a breach absent from them is new,
        # so it alerts now rather than waiting out the reminder window.
        open_uids = self.storage.open_exception_uids()
        for alert in policy_breach_alerts(checks, open_uids, now=now):
            window_h = self.dedupe_hours if alert.context.get("transition") else reminder_h
            if self.storage.recently_alerted(alert.position_uid, alert.kind, window_h):
                continue
            self.storage.write_alert(alert)
            written += 1
        self.storage.reconcile_policy_exceptions([{**asdict(c), "uid": c.uid} for c in checks], now)
        return written

    def _emit_rebalance_alerts(self) -> int:
        """Reconcile the action queue to today's tickets, then emit a
        REBALANCE alert for every 'now' ticket (positive across σ's band,
        costs known) that is not snoozed. Persistent condition, so deduped
        like NEGATIVE_CARRY: first occurrence fires, then reminds every
        reminder_days. Returns the count of alerts written."""
        from defi_tracker.analytics import describe_ticket

        queue = self.compute_action_queue()
        # Every ticket is queued; one no longer proposed auto-resolves, so a
        # ticket you executed drops off on the next pass.
        self.storage.reconcile_rebalance_queue([t.queue_item() for t in queue.tickets])

        now = int(datetime.now(UTC).timestamp())
        written = 0
        window_h = (
            self.alert_config.reminder_days * 24
            if self.alert_config.reminder_days > 0
            else 10 * 365 * 24
        )
        for t in queue.tickets:
            if t.tier != "now" or self.storage.is_rebalance_snoozed(t.uid):
                continue
            if self.storage.recently_alerted(t.uid, "REBALANCE", window_h):
                continue
            head, steps, case = describe_ticket(t)
            self.storage.write_alert(
                Alert(
                    position_uid=t.uid,
                    kind="REBALANCE",
                    severity=AlertSeverity.HIGH,
                    message=f"{head}. " + " · ".join(steps[1:] + case[-1:]),
                    triggered_at=now,
                    context={**t.context(), "transition": False},
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
        self, adapter: ProtocolAdapter, wallet: str, chain: Chain, mark_ts: int | None = None
    ) -> AdapterPass:
        """Sync events, then snapshot. A sync failure is reported (last tuple
        element) but does NOT skip the snapshot: the event watermark hasn't
        advanced, so events backfill once the source recovers, while
        fetch_positions may still succeed (e.g. via an RPC fallback) and keep
        marks fresh. snapshot_adapter failures still propagate — a missing
        snapshot is the loud signal that marks are stale.

        The reported error also covers a *truncated* sync: an incremental
        adapter that stopped short of the chain head returned cleanly, but the
        un-scanned range still hides events, so that pass must not count as a
        successful sync (see ProtocolAdapter.last_scan_reached_head)."""
        deadline = time.monotonic() + _ADAPTER_TIME_BUDGET
        new_ev = 0
        sync_error: str | None = None
        try:
            new_ev = self.sync_adapter(adapter, wallet, chain, deadline=deadline)
            if not adapter.last_scan_reached_head(wallet, chain):
                sync_error = (
                    "event sync incomplete: scan stopped before the chain head "
                    "(time budget or per-run block cap) — still catching up"
                )
                _log.info(
                    "Event sync truncated for %s@%s — not marking sync healthy",
                    adapter.info.protocol_id,
                    chain.value,
                )
        except Exception as e:
            sync_error = redact(f"event sync failed: {e}")
            _log.warning(
                "Event sync failed for %s@%s (%s) — snapshotting anyway",
                adapter.info.protocol_id,
                chain.value,
                e,
            )
        # Read after the generator is exhausted, same as last_scan_reached_head.
        dropped = adapter.last_scan_dropped_events(wallet, chain)
        positions, alerts, missing_prices = self.snapshot_adapter(
            adapter, wallet, chain, mark_ts=mark_ts
        )
        return AdapterPass(
            events_inserted=new_ev,
            events_dropped=dropped,
            positions=positions,
            alerts=alerts,
            missing_prices=missing_prices,
            sync_error=sync_error,
        )

    @staticmethod
    def _positions_source(positions: list[Position]) -> str | None:
        """How this pass obtained its positions ('subgraph', 'rpc_fallback', …).

        Adapters tag it on Position.meta and are homogeneous within a pass, so
        the first non-null wins. None when nothing was returned or nothing
        declared a source."""
        for pos in positions:
            source = (pos.meta or {}).get("source")
            if source:
                return str(source)
        return None

    # ── Marks-only pass ───────────────────────────────────────────────────

    def mark_all(
        self,
        wallets: list[str] | None = None,
        chains: list[Chain] | None = None,
        protocols: list[str] | None = None,
    ) -> dict:
        """Fetch positions and record one mark each. Nothing else.

        This exists so marks can be collected hourly while `run` stays daily.
        A full pass syncs events, recomputes cost basis, writes the daily
        snapshot and evaluates alerts — none of which produces a better mark,
        and all of which costs rate-limited calls that an hourly cadence
        cannot afford. What a volatility estimator needs is value and tick
        over time, and that is exactly what this writes.

        Every position in the pass shares one timestamp, so the book is one
        observation rather than a smear across however long the pass took.

        A failing adapter is logged and skipped: a partial mark is a real
        observation of the positions it covers, and losing the whole pass
        because one chain is down loses history that cannot be reconstructed.
        """
        wallet_list = (
            [w.lower() for w in wallets]
            if wallets
            else [w["address"] for w in self.storage.list_wallets()]
        )
        chain_list = chains or list(Chain)
        adapter_list = all_adapters()
        if protocols:
            adapter_list = [a for a in adapter_list if a.info.protocol_id in protocols]

        at = int(datetime.now(UTC).timestamp())
        marks = 0
        positions_seen = 0
        failures: list[str] = []

        for wallet in wallet_list:
            for chain in chain_list:
                for adapter in adapter_list:
                    if not adapter.handles(chain):
                        continue
                    pid = adapter.info.protocol_id
                    try:
                        positions = adapter.fetch_positions(wallet, chain)
                    except Exception as e:
                        _log.warning("Marking %s@%s failed: %s", pid, chain.value, e)
                        failures.append(redact(f"{pid}@{chain.value}: {e}"))
                        continue
                    positions_seen += len(positions)
                    marks += self.storage.write_marks(
                        positions, ts=at, source=self._positions_source(positions)
                    )
        return {
            "ts": at,
            "marks_written": marks,
            "positions": positions_seen,
            "failures": failures,
        }

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
        events_dropped = 0
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
                        result = self._sync_and_snapshot(adapter, wallet, chain, mark_ts=started_at)
                    except Exception as e:
                        _log.warning("Adapter %s failed: %s", key, e)
                        err_bucket = per_adapter.setdefault(key, _new_adapter_bucket())
                        errors_list = err_bucket["errors"]
                        assert isinstance(errors_list, list)
                        errors_list.append(redact(str(e)))
                        # Nothing about this pass succeeded — record it anyway so
                        # the stuck *_ok_at timestamps expose how long it's been
                        # broken (an adapter that never runs cleanly is otherwise
                        # invisible: it just stops contributing rows).
                        self.storage.record_pipeline_health(
                            wallet,
                            chain,
                            pid,
                            now=int(datetime.now(UTC).timestamp()),
                            sync_ok=False,
                            snapshot_ok=False,
                            source=None,
                            error=redact(str(e)),
                        )
                        continue

                    self.storage.record_pipeline_health(
                        wallet,
                        chain,
                        pid,
                        now=int(datetime.now(UTC).timestamp()),
                        sync_ok=result.sync_error is None,
                        # _sync_and_snapshot lets snapshot failures propagate to
                        # the except branch above, so reaching here means
                        # fetch_positions returned.
                        snapshot_ok=True,
                        source=self._positions_source(result.positions),
                        error=result.sync_error,
                        dropped_events=result.events_dropped,
                    )

                    bucket = per_adapter.setdefault(key, _new_adapter_bucket())
                    if result.sync_error:
                        sync_errors_list = bucket["errors"]
                        assert isinstance(sync_errors_list, list)
                        sync_errors_list.append(result.sync_error)
                    # All numeric fields are int by construction
                    bucket["events"] = int(bucket["events"]) + result.events_inserted  # type: ignore[arg-type]
                    bucket["dropped"] = int(bucket["dropped"]) + result.events_dropped  # type: ignore[arg-type]
                    bucket["positions"] = int(bucket["positions"]) + len(result.positions)  # type: ignore[arg-type]
                    bucket["alerts"] = int(bucket["alerts"]) + len(result.alerts)  # type: ignore[arg-type]

                    events_inserted += result.events_inserted
                    events_dropped += result.events_dropped
                    positions_snapshotted += len(result.positions)
                    alerts_raised += len(result.alerts)
                    price_gap_tokens.extend(result.missing_prices)

        # Retire liveness rows for wallets/adapters that no longer exist — a
        # stale row warns forever otherwise. Only on an unfiltered pass, where
        # the registered sets below really are "everything" (see
        # Storage.prune_pipeline_health). Registered wallets include disabled
        # ones: a temporarily disabled wallet should keep its history.
        if not wallets and not protocols:
            self.storage.prune_pipeline_health(
                [w["address"] for w in self.storage.list_wallets(enabled_only=False)],
                [a.info.protocol_id for a in all_adapters()],
            )

        # Portfolio-wide rebalance alerts — fired on the 🔴 tier only, once all
        # snapshots for today are written (the signal needs cross-position
        # yields + last-in-range, so it can't live in the per-adapter loop).
        if not wallets and not protocols:
            self._refresh_elasticity()
        # Independent passes: an error in the rebalance analytics must not
        # also skip the mandate's breach checks (or abort the run summary).
        for emit in (self._emit_rebalance_alerts, self._emit_policy_alerts):
            try:
                alerts_raised += emit()
            except Exception as e:
                _log.error("%s failed: %s", emit.__name__, redact(str(e)), exc_info=True)

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
            "events_dropped": events_dropped,
            "positions_snapshotted": positions_snapshotted,
            "alerts_raised": alerts_raised,
            "price_gap_tokens": deduped,
            "per_adapter": per_adapter,
            "health": self.storage.pipeline_health(),
        }
