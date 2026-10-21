"""
SQLite schema for the DeFi tracker.

Design principles:
  - Multi-wallet, multi-chain native. Every table keys by (wallet, chain,
    protocol, position_key). You can track 10 wallets across 5 chains and
    20 protocols in one database.
  - All USD values stored as REAL (sufficient for tracking; not accounting-grade).
    For accounting-grade precision, store as TEXT-encoded Decimal — left as
    a comment for future migration.
  - Events are immutable, append-only. Snapshots are daily-grained.
  - Heavy use of generated/derived columns and indexes for time-range queries
    (MTD, MoM, custom windows).
  - Schema versioning via the `schema_meta` table (SCHEMA_VERSION), with
    idempotent migrations in init_schema().

To use:
    from defi_tracker.core.storage import Storage
    s = Storage("tracker.db")
    s.init_schema()                                 # idempotent
    s.upsert_events(events)
    s.write_snapshot(position, snapshot_date=...)   # one Position per call
    rows = s.month_to_date_pnl(wallet, year=2026, month=5)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from defi_tracker.core.endpoints import redact
from defi_tracker.core.types import (
    Alert,
    Chain,
    Event,
    Position,
    Token,
    Transfer,
    VaultBalance,
)

SCHEMA_VERSION = 16


# ── DDL ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- ─── Wallets being tracked ─────────────────────────────────────────────
-- A simple registry. Lets us name wallets ('Cold Wallet', 'Hot Wallet', etc.)
-- and toggle which ones are actively scanned.
CREATE TABLE IF NOT EXISTS wallets (
    address       TEXT NOT NULL,           -- 0x... lowercased
    label         TEXT,                    -- user-friendly name
    enabled       INTEGER NOT NULL DEFAULT 1,
    added_at      INTEGER NOT NULL,        -- unix seconds
    fireblocks_vault_id TEXT,              -- custody vault holding this address (v7)
    PRIMARY KEY (address)
);

-- ─── Per (wallet, chain) sync state ─────────────────────────────────────
-- Records the last event timestamp we've successfully ingested, per adapter.
-- Lets fetch_events() do incremental sync efficiently.
CREATE TABLE IF NOT EXISTS sync_state (
    wallet         TEXT NOT NULL,
    chain          TEXT NOT NULL,
    protocol_id    TEXT NOT NULL,
    last_event_ts  INTEGER NOT NULL DEFAULT 0,
    last_synced_at INTEGER NOT NULL,
    PRIMARY KEY (wallet, chain, protocol_id)
);

-- ─── Token registry ─────────────────────────────────────────────────────
-- Stable lookup table. Adapters upsert tokens as they encounter them.
-- The (chain, address) tuple is the canonical key.
CREATE TABLE IF NOT EXISTS tokens (
    chain        TEXT NOT NULL,
    address      TEXT NOT NULL,            -- 0x... lowercased
    symbol       TEXT NOT NULL,
    decimals     INTEGER NOT NULL,
    coingecko_id TEXT,                     -- optional, for historical prices
    PRIMARY KEY (chain, address)
);

-- ─── Events: every position-affecting on-chain action ───────────────────
-- IMMUTABLE. Append-only. event_uid (tx_hash:log_index) is globally unique.
-- usd_at_ts is THE critical column for cost basis — it's the USD value at
-- the moment the event happened, not at the current price.
CREATE TABLE IF NOT EXISTS events (
    event_uid       TEXT PRIMARY KEY,       -- '{tx_hash}:{log_index}'
    wallet          TEXT NOT NULL,
    chain           TEXT NOT NULL,
    protocol_id     TEXT NOT NULL,
    position_key    TEXT NOT NULL,
    tx_hash         TEXT NOT NULL,
    log_index       INTEGER NOT NULL,
    ts              INTEGER NOT NULL,       -- block timestamp, unix seconds
    block_number    INTEGER NOT NULL,
    kind            TEXT NOT NULL,          -- EventKind
    usd_at_ts       REAL NOT NULL,          -- signed: +deposit, -withdraw
    amounts_json    TEXT NOT NULL,          -- JSON: [{token_key, amount, price_at_ts}]
    meta_json       TEXT                    -- adapter-specific extras
);

CREATE INDEX IF NOT EXISTS idx_events_position
    ON events(wallet, chain, protocol_id, position_key, ts);
CREATE INDEX IF NOT EXISTS idx_events_wallet_chain_ts
    ON events(wallet, chain, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts
    ON events(ts);

-- ─── Marks: sub-daily point-in-time valuations ──────────────────────────
-- Deliberately NOT the snapshots table. `snapshots` is keyed by DATE and
-- carries derived analytics (cost basis, IL, PnL); writing it hourly would
-- both collide on its own primary key — the last write of the day silently
-- wins, so intraday history would be lost as fast as it was gathered — and
-- recompute cost basis 24 times a day to reach the same answer.
--
-- This table is narrow, append-only and keyed by TIMESTAMP: only what a
-- volatility, VaR or range estimator actually reads off a price series.
-- Everything derived stays in `snapshots`, daily, where it belongs.
--
-- `liquidity` is TEXT because a CL liquidity value can exceed 2^63 and
-- SQLite INTEGER cannot hold it. `unclaimed_usd` stays NULL when the fee
-- read failed, and `unclaimed_is_known` says which of NULL-because-zero and
-- NULL-because-unreadable it was.
CREATE TABLE IF NOT EXISTS marks (
    ts                INTEGER NOT NULL,     -- unix seconds, the moment marked
    wallet            TEXT NOT NULL,
    chain             TEXT NOT NULL,
    protocol_id       TEXT NOT NULL,
    position_key      TEXT NOT NULL,
    current_value_usd REAL NOT NULL,
    unclaimed_usd     REAL,
    unclaimed_is_known INTEGER NOT NULL DEFAULT 1,
    tick_lower        INTEGER,
    tick_upper        INTEGER,
    tick_current      INTEGER,
    in_range          INTEGER,
    liquidity         TEXT,
    source            TEXT,                 -- which path produced it
    PRIMARY KEY (wallet, chain, protocol_id, position_key, ts)
);

CREATE INDEX IF NOT EXISTS idx_marks_ts ON marks(ts);
CREATE INDEX IF NOT EXISTS idx_marks_position_ts
    ON marks(wallet, chain, protocol_id, position_key, ts);

-- ─── Transaction costs: gas (and slippage, where measurable) ────────────
-- Keyed by TRANSACTION, not by event. One transaction can emit several
-- events — a rebalance burns and mints in the same tx — and gas is paid
-- once for the tx. Storing it per event and summing would double-count
-- exactly the transactions that cost the most.
--
-- Every USD column is nullable and stays NULL when it could not be
-- established: gas_usd is NULL when the native token had no price at that
-- block, and slippage_usd is NULL unless an adapter could actually measure
-- it. Unknown renders as unknown, never as zero.
CREATE TABLE IF NOT EXISTS tx_costs (
    chain           TEXT NOT NULL,
    tx_hash         TEXT NOT NULL,
    ts              INTEGER NOT NULL,       -- block timestamp, unix seconds
    block_number    INTEGER,
    gas_native      REAL,                   -- gas actually burned, native units
    gas_usd         REAL,                   -- priced at block time; NULL if unpriceable
    native_symbol   TEXT,                   -- what was burned: BNB, ETH, PEAQ
    native_price_usd REAL,                  -- the price used, kept for audit
    slippage_usd    REAL,                   -- NULL unless measured; never inferred
    source          TEXT NOT NULL,          -- how it was established, e.g. 'receipt'
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (chain, tx_hash)
);

CREATE INDEX IF NOT EXISTS idx_tx_costs_ts ON tx_costs(ts);

-- ─── Transfers: custody-side movements (v7) ─────────────────────────────
-- The `events` discipline, for custody: IMMUTABLE, append-only, USD at
-- transfer time, idempotent by transfer_uid ('{source}:{source_id}').
--
-- (chain, tx_hash) is the join key to `events`. A vault→LP deposit appears
-- in both tables — once as capital leaving the vault, once as the position's
-- deposit — so a balance sheet must net on it or count the capital twice.
--
-- Deliberately no classification column. Internal vs perimeter-crossing is a
-- config-driven reading of these rows (Phase 3.4) with real rework risk; kept
-- out of an immutable table, a wrong rule is a code fix, not a data repair.
--
-- `amount` is TEXT so a Decimal round-trips exactly; `usd_at_ts` is NULL when
-- the asset had no price at that moment — unknown, never zero.
CREATE TABLE IF NOT EXISTS transfers (
    transfer_uid  TEXT PRIMARY KEY,
    source        TEXT NOT NULL,           -- 'fireblocks' | 'manual'
    source_id     TEXT NOT NULL,
    ts            INTEGER NOT NULL,        -- unix seconds
    chain         TEXT,                    -- NULL for off-chain movements
    tx_hash       TEXT,                    -- lowercased; NULL when off-chain
    asset         TEXT NOT NULL,
    token_key     TEXT,
    amount        TEXT NOT NULL,           -- Decimal, > 0; direction is from → to
    usd_at_ts     REAL,
    from_ref      TEXT NOT NULL,           -- vault id, address, or external ref
    to_ref        TEXT NOT NULL,
    meta_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_transfers_ts ON transfers(ts);
CREATE INDEX IF NOT EXISTS idx_transfers_tx ON transfers(chain, tx_hash);

-- ─── Vault balances: observed custody balances (v7) ─────────────────────
-- Append-only observations, like `marks`: one row per (vault, asset, moment).
-- A reconciliation reads the balance as of a date, so history is kept rather
-- than overwritten.
CREATE TABLE IF NOT EXISTS vault_balances (
    vault_id      TEXT NOT NULL,
    asset         TEXT NOT NULL,
    ts            INTEGER NOT NULL,
    amount        TEXT NOT NULL,           -- Decimal
    price_usd     REAL,                    -- NULL = unpriced
    source        TEXT NOT NULL,
    PRIMARY KEY (vault_id, asset, ts)
);

CREATE INDEX IF NOT EXISTS idx_vault_balances_ts ON vault_balances(ts);

-- ─── Snapshots: daily point-in-time state of every position ─────────────
-- The runner writes one row per (position, date). Backfill possible by
-- replaying events with historical prices.
--
-- (date, wallet, chain, protocol_id, position_key) is the natural key.
-- 'date' is stored as TEXT 'YYYY-MM-DD' for easy GROUP BY date_trunc.
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date     TEXT NOT NULL,        -- 'YYYY-MM-DD' UTC
    wallet            TEXT NOT NULL,
    chain             TEXT NOT NULL,
    protocol_id       TEXT NOT NULL,
    position_key      TEXT NOT NULL,
    protocol_kind     TEXT NOT NULL,        -- ProtocolKind
    pair_label        TEXT NOT NULL,
    snapshot_ts       INTEGER NOT NULL,     -- precise unix seconds

    -- valuation (everything in USD)
    current_value_usd       REAL NOT NULL,
    unclaimed_usd           REAL NOT NULL DEFAULT 0,
    -- 0 when the fee-growth read FAILED: unclaimed_usd above is then a
    -- placeholder, not a measurement. A companion flag rather than a nullable
    -- column because SQLite cannot drop NOT NULL without a table rebuild.
    unclaimed_is_known      INTEGER NOT NULL DEFAULT 1,
    cost_basis_usd          REAL,           -- derived from events
    hold_value_usd          REAL,           -- if user had just held the deposit tokens
    il_usd                  REAL,           -- current - hold (negative = loss vs holding)
    il_pct                  REAL,
    pnl_usd                 REAL,           -- current + unclaimed + lifetime_collected - cost_basis

    -- tombstone flag: 1 when the position disappeared from a successful
    -- fetch (closed on-chain). Reporting queries exclude closed rows.
    is_closed               INTEGER NOT NULL DEFAULT 0,

    -- pool / range context
    in_range                INTEGER,        -- 0/1/NULL
    tick_lower              INTEGER,
    tick_upper              INTEGER,
    tick_current            INTEGER,
    pool_tvl_usd            REAL,
    seven_day_fees_usd      REAL,           -- pool-wide
    position_share_pct      REAL,
    fee_tier_bps            INTEGER,

    -- balances (JSON for flexibility across position kinds)
    current_balances_json   TEXT NOT NULL,  -- [{token_key, amount}]
    unclaimed_balances_json TEXT,
    meta_json               TEXT,

    PRIMARY KEY (snapshot_date, wallet, chain, protocol_id, position_key)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_wallet_date
    ON snapshots(wallet, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_snapshots_position
    ON snapshots(wallet, chain, protocol_id, position_key, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_snapshots_date
    ON snapshots(snapshot_date);

-- ─── Alerts: deduped signal log ─────────────────────────────────────────
-- Dedup window is enforced in app code (don't re-alert same dedup_key
-- within N hours).
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_uid    TEXT NOT NULL,
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    message         TEXT NOT NULL,
    triggered_at    INTEGER NOT NULL,
    delivered_at    INTEGER,                -- NULL until pushed to Slack/etc
    context_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_dedup
    ON alerts(position_uid, kind, triggered_at DESC);

-- ─── Price cache ────────────────────────────────────────────────────────
-- Avoid hammering CoinGecko for the same (token, day). Daily granularity.
CREATE TABLE IF NOT EXISTS price_cache (
    chain         TEXT NOT NULL,
    address       TEXT NOT NULL,
    date          TEXT NOT NULL,            -- 'YYYY-MM-DD' UTC
    price_usd     REAL NOT NULL,
    source        TEXT NOT NULL,
    cached_at     INTEGER NOT NULL,
    PRIMARY KEY (chain, address, date)
);

-- ─── Rebalance action queue ─────────────────────────────────────────────
-- One row per position that has entered an actionable rebalance tier
-- (🔴 now / 🟠 soon). Survives across runs so a rebalance you've done drops
-- off (auto-resolved when no longer flagged) and one you're deferring stays
-- tracked (snoozed). Re-flagging after resolution opens a fresh episode.
CREATE TABLE IF NOT EXISTS rebalance_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    position_uid  TEXT NOT NULL,
    pair_label    TEXT NOT NULL,
    tier          TEXT NOT NULL,              -- 'now' | 'soon' (latest seen)
    status        TEXT NOT NULL,              -- 'open' | 'snoozed' | 'resolved'
    first_seen    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL,
    snooze_until  INTEGER,                    -- unix ts; while > now → suppressed
    resolved_at   INTEGER,
    resolved_reason TEXT,                     -- 'auto' (no longer flagged) | 'manual'
    context_json  TEXT                        -- latest signal snapshot for display
);

-- One live (non-resolved) episode per position at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_rebalance_queue_live
    ON rebalance_queue(position_uid) WHERE status != 'resolved';

-- ─── Policy exceptions (v8) ─────────────────────────────────────────────
-- Mandate §4: "every exception logged and reviewed; trending to zero". One
-- row per EPISODE of a (limit, subject) breach: opened when a check first
-- breaches, updated while it persists, cleared when the check next reads OK.
-- A check that reads UNKNOWN neither clears nor extends an episode — not
-- being able to measure a breach is not evidence it ended.
--
-- Two independent axes, deliberately: whether the condition still holds
-- (cleared_at) and whether a human has reviewed it (reviewed_at). A breach
-- that cleared on its own was still an exception, and still needs review.
-- Observed values are TEXT so the Decimal a check produced round-trips.
CREATE TABLE IF NOT EXISTS policy_exceptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    check_uid       TEXT NOT NULL,          -- 'policy:{limit_id}:{subject}'
    limit_id        TEXT NOT NULL,
    subject         TEXT NOT NULL,
    unit            TEXT NOT NULL,
    threshold       TEXT,                   -- the limit when it opened
    first_observed  TEXT,
    last_observed   TEXT,
    worst_observed  TEXT,
    opened_at       INTEGER NOT NULL,
    last_seen       INTEGER NOT NULL,
    cleared_at      INTEGER,                -- NULL while the breach holds
    reviewed_at     INTEGER,
    reviewed_by     TEXT,
    disposition     TEXT,                   -- 'accepted' | 'remediated' | 'false_positive'
    review_note     TEXT
);

-- One live episode per (limit, subject).
CREATE UNIQUE INDEX IF NOT EXISTS idx_policy_exceptions_live
    ON policy_exceptions(check_uid) WHERE cleared_at IS NULL;

-- ─── Incidents (v8) ─────────────────────────────────────────────────────
-- Hand-logged: a venue, protocol or counterparty failure, or anything the
-- mandate's incident-response section covers. The lifecycle the policy asks
-- for — detection, containment, escalation, post-mortem — is one column each,
-- so a report can say which step an incident is stuck at.
CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    severity        TEXT NOT NULL,          -- 'low' | 'medium' | 'high' | 'critical'
    description     TEXT,
    detected_at     INTEGER NOT NULL,
    logged_at       INTEGER NOT NULL,
    logged_by       TEXT,
    escalated_to    TEXT,
    contained_at    INTEGER,
    resolved_at     INTEGER,
    postmortem      TEXT                    -- link or summary; required to close
);

-- ─── Contractual ledger (v9) ────────────────────────────────────────────
-- MM agreements, loan/option structures, OTC placements. No API returns
-- these, so they are hand-keyed — and a hand-keyed number is only as good as
-- where it came from, so `source_note` is required and every entry carries
-- the date it was true `as_of`.
--
-- Versioned, never edited: a change to a contract is a new row with a later
-- as_of under the same `ref`, so the ledger can say what was believed on any
-- date. The current state of a contract is its latest version.
--
-- `usd_value` is the exposure in USD as keyed (a loan's inventory marked, an
-- option's notional); NULL when not known, which makes that counterparty's
-- exposure unknown rather than smaller.
CREATE TABLE IF NOT EXISTS contracts (
    ref           TEXT NOT NULL,            -- operator's stable id, e.g. 'mm-a-loan-2024'
    as_of         TEXT NOT NULL,            -- 'YYYY-MM-DD' this version was true
    counterparty  TEXT NOT NULL,
    kind          TEXT NOT NULL,            -- mm_loan | mm_option | mm_retainer | otc | other
    status        TEXT NOT NULL,            -- active | expired | terminated
    asset         TEXT,
    amount        TEXT,                     -- Decimal, in `asset` units
    usd_value     REAL,                     -- exposure in USD; NULL = unknown
    start_date    TEXT,
    end_date      TEXT,                     -- expiry; a renewal decision keys on it
    terms         TEXT,                     -- strike, fees, KPIs, free text
    annual_cost_usd REAL,                   -- what the contract costs peaq a year (v10); NULL = not keyed
    notice_by     TEXT,                     -- last day to give notice that stops a renewal (v13)
    interest_rate_pct REAL,                 -- loan interest, %/yr as the contract states it (v14)
    key_terms_json TEXT,                    -- {term: short value}, the desk's term-sheet grid (v14)
    returned      TEXT,                     -- Decimal: loaned units handed back, no longer owed (v16)
    source_note   TEXT NOT NULL,            -- where the figures came from
    recorded_at   INTEGER NOT NULL,
    PRIMARY KEY (ref, as_of)
);

-- One row per option tranche of a contract (v13), versioned like contracts:
-- a new as_of is a new version, the same as_of a same-day correction. The
-- MM book marks each open tranche against PEAQ now. A strike set by formula
-- before it has been measured (e.g. 150% of an ATM average) has strike_usd
-- NULL and the formula in strike_rule — unknown, never a guessed price.
CREATE TABLE IF NOT EXISTS contract_tranches (
    ref           TEXT NOT NULL,            -- the contract (contracts.ref)
    tranche       TEXT NOT NULL,            -- as the contract numbers it: '1', '2', …
    as_of         TEXT NOT NULL,            -- 'YYYY-MM-DD' this version was true
    asset         TEXT NOT NULL,
    amount        TEXT NOT NULL,            -- Decimal, in `asset` units
    strike_usd    TEXT,                     -- Decimal; NULL = not fixed
    strike_rule   TEXT,                     -- how the strike is set, in the contract's words
    strike_low_usd  TEXT,                   -- an unfixed strike's bounds, when the contract
    strike_high_usd TEXT,                   --   pins its formula and dates (Decimal)
    exercisable_from TEXT,                  -- 'YYYY-MM-DD' window, NULL = not stated
    exercisable_to   TEXT,
    status        TEXT NOT NULL,            -- open | exercised | lapsed
    exercised_on  TEXT,                     -- 'YYYY-MM-DD' when status = exercised
    exercise_price_usd TEXT,                -- PEAQ's market price when exercised (v15)
    source_note   TEXT NOT NULL,            -- the document and page the figures came from
    recorded_at   INTEGER NOT NULL,
    PRIMARY KEY (ref, tranche, as_of)
);

-- ─── Pipeline liveness ──────────────────────────────────────────────────
-- One row per (wallet, chain, protocol_id), rewritten every pass. The
-- *_ok_at columns only ever advance on a clean pass, so a stuck timestamp
-- IS the signal: sync_state can't do this job (its watermark only moves
-- when a batch actually contains events, so a dead source looks idle, not
-- broken). `source` records how the positions were obtained, which is how
-- a silent subgraph → rpc_fallback degradation becomes visible.
CREATE TABLE IF NOT EXISTS pipeline_health (
    wallet              TEXT NOT NULL,
    chain               TEXT NOT NULL,
    protocol_id         TEXT NOT NULL,
    last_run_at         INTEGER NOT NULL,  -- every pass, success or failure
    last_sync_ok_at     INTEGER,           -- last pass where event sync raised no error
    last_snapshot_ok_at INTEGER,           -- last pass where fetch_positions succeeded
    source              TEXT,              -- e.g. 'subgraph' | 'rpc_fallback' | NULL
    last_error          TEXT,              -- most recent error string, NULL when clean
    dropped_events_last_run INTEGER NOT NULL DEFAULT 0,  -- fetched but unstorable
    PRIMARY KEY (wallet, chain, protocol_id)
);

-- ─── Pool flow history (v11, Phase 5.3; v12 fee-counter days) ──────────
-- Per pool per UTC day: the LP fees paid (in token1 at the pool's own
-- price), the pool's active liquidity summed over `samples` observations
-- (÷ samples = its mean), and the book's own active liquidity at the same
-- observations. Two sources (`source`):
--   'swaps'       — summed over the day's Swap logs; samples = swaps, each
--                   fee at its swap's own price.
--   'fee_counter' — two archive reads of the pool's state, at the previous
--                   day's close and this day's: fees = Δ feeGrowthGlobal ×
--                   the mean of the two depths, in token1 at the close;
--                   samples = 2 and `swaps` is NULL (not counted — a swap
--                   count is not something a fee counter can say).
-- Sums, not means, so a day that straddles two scan windows adds up exactly;
-- each window lands with its cursor in one transaction, so nothing is ever
-- counted twice. Market data derived from the chain — rebuildable, never
-- booked money.
CREATE TABLE IF NOT EXISTS pool_days (
    chain         TEXT NOT NULL,
    protocol_id   TEXT NOT NULL,
    pool          TEXT NOT NULL,
    day           TEXT NOT NULL,            -- 'YYYY-MM-DD' UTC
    swaps         INTEGER,                  -- NULL on fee-counter days
    samples       INTEGER NOT NULL,         -- observations behind l_sum / book_l_sum
    source        TEXT NOT NULL DEFAULT 'swaps',  -- 'swaps' | 'fee_counter'
    fees_token1   REAL NOT NULL,            -- human units of token1
    l_sum         REAL NOT NULL,            -- Σ pool active liquidity (raw) over samples
    book_l_sum    REAL NOT NULL,            -- Σ book active liquidity (raw) over samples
    book_l_open   REAL NOT NULL,            -- book active liquidity at the day's first sample
    tick_open     INTEGER NOT NULL,
    tick_close    INTEGER NOT NULL,
    first_block   INTEGER NOT NULL,
    last_block    INTEGER NOT NULL,
    PRIMARY KEY (chain, protocol_id, pool, day)
);

-- ─── Schema metadata ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ─── Generic key-value store ─────────────────────────────────────────────
-- Used for adapter-internal cursors and flags (e.g. block scan cursor
-- for RPC-only adapters that need to track progress independently of
-- the event-timestamp watermark in sync_state).
CREATE TABLE IF NOT EXISTS kv_store (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ── Storage class ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ValueDelta:
    """A change in book value between two marks, carrying the window it spans.

    The dates are the point. A surface handed only (curr, prev) has no way to
    know whether it is looking at one day's move or four, so it defaults to
    calling everything a day — which is exactly the lie this type exists to
    stop. `excluded_*` report the positions left out of the comparison because
    their own prior mark predates the baseline; they are stated, never
    silently folded into the number.
    """

    curr: float
    prev: float
    curr_date: str
    prev_date: str
    excluded_positions: int = 0
    excluded_value: float = 0.0

    @property
    def delta(self) -> float:
        return self.curr - self.prev

    @property
    def pct(self) -> float | None:
        """None when the baseline is zero — an undefined percentage, not 0%."""
        return self.delta / self.prev * 100 if self.prev else None

    @property
    def span_days(self) -> int:
        return (date.fromisoformat(self.curr_date) - date.fromisoformat(self.prev_date)).days

    def window_label(self) -> str:
        """How a surface should name this window. 'day' is a claim to be earned."""
        n = self.span_days
        if n == 1:
            return "since yesterday"
        return f"since {self.prev_date} ({n}d)"


class Storage:
    """SQLite wrapper. One instance per database file. Thread-safe enough
    for single-process CLI use; not concurrent-writer-safe."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection with sensible pragmas."""
        conn = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    # ── Schema lifecycle ──────────────────────────────────────────────────

    def init_schema(self) -> None:
        """Idempotent — safe to call every startup. Applies migrations for
        databases created under an older SCHEMA_VERSION."""
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            self._migrate(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("version", str(SCHEMA_VERSION)),
            )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Column-presence-guarded migrations (idempotent; fresh DBs already
        have these via SCHEMA_SQL, so each guard is a no-op there)."""
        # v1 → v2: snapshots.is_closed tombstone flag
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
        if "is_closed" not in cols:
            conn.execute("ALTER TABLE snapshots ADD COLUMN is_closed INTEGER NOT NULL DEFAULT 0")
        # Backfill in_range from ticks for rows an adapter left null (RPC
        # fallbacks). Idempotent — only touches rows still null with known
        # ticks; new snapshots derive in_range at Position construction.
        conn.execute(
            """UPDATE snapshots
               SET in_range = CASE
                       WHEN tick_current BETWEEN tick_lower AND tick_upper THEN 1
                       ELSE 0 END
               WHERE in_range IS NULL
                 AND tick_lower IS NOT NULL
                 AND tick_upper IS NOT NULL
                 AND tick_current IS NOT NULL"""
        )
        # v3 → v4: was the unclaimed balance actually readable this pass?
        snap_cols = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
        if "unclaimed_is_known" not in snap_cols:
            conn.execute(
                "ALTER TABLE snapshots ADD COLUMN unclaimed_is_known INTEGER NOT NULL DEFAULT 1"
            )
        # v9 → v10: a contract's annual cost — the "cost of liquidity" metric.
        contract_cols = {r["name"] for r in conn.execute("PRAGMA table_info(contracts)")}
        if contract_cols and "annual_cost_usd" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN annual_cost_usd REAL")
        if contract_cols and "notice_by" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN notice_by TEXT")
        if contract_cols and "interest_rate_pct" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN interest_rate_pct REAL")
        if contract_cols and "key_terms_json" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN key_terms_json TEXT")
        # v15 → v16: loaned units the MM handed back early (not bought, not owed).
        if contract_cols and "returned" not in contract_cols:
            conn.execute("ALTER TABLE contracts ADD COLUMN returned TEXT")
        # v11 → v12: pool_days carries its source and sample count, and a
        # fee-counter day has no swap count — so `swaps` loses NOT NULL,
        # which SQLite can only do by rebuilding the table.
        pd_cols = {r["name"] for r in conn.execute("PRAGMA table_info(pool_days)")}
        if pd_cols and "samples" not in pd_cols:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("ALTER TABLE pool_days RENAME TO pool_days_v11")
                ddl = SCHEMA_SQL[SCHEMA_SQL.index("CREATE TABLE IF NOT EXISTS pool_days") :]
                conn.execute(ddl[: ddl.index(");") + 2])
                conn.execute(
                    """INSERT INTO pool_days (chain, protocol_id, pool, day, swaps, fees_token1,
                           l_sum, book_l_sum, book_l_open, tick_open, tick_close, first_block,
                           last_block, samples, source)
                       SELECT chain, protocol_id, pool, day, swaps, fees_token1, l_sum,
                           book_l_sum, book_l_open, tick_open, tick_close, first_block,
                           last_block, swaps, 'swaps'
                       FROM pool_days_v11"""
                )
                conn.execute("DROP TABLE pool_days_v11")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        # v13: an unfixed strike's bounds (added before v13 was released).
        tr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(contract_tranches)")}
        if tr_cols and "strike_low_usd" not in tr_cols:
            conn.execute("ALTER TABLE contract_tranches ADD COLUMN strike_low_usd TEXT")
            conn.execute("ALTER TABLE contract_tranches ADD COLUMN strike_high_usd TEXT")
        if tr_cols and "exercise_price_usd" not in tr_cols:
            conn.execute("ALTER TABLE contract_tranches ADD COLUMN exercise_price_usd TEXT")
        # v6 → v7: which custody vault holds each tracked address.
        wallet_cols = {r["name"] for r in conn.execute("PRAGMA table_info(wallets)")}
        if "fireblocks_vault_id" not in wallet_cols:
            conn.execute("ALTER TABLE wallets ADD COLUMN fireblocks_vault_id TEXT")
        # v2 → v3: how many events the last pass fetched but could not store.
        ph_cols = {r["name"] for r in conn.execute("PRAGMA table_info(pipeline_health)")}
        if "dropped_events_last_run" not in ph_cols:
            conn.execute(
                "ALTER TABLE pipeline_health ADD COLUMN "
                "dropped_events_last_run INTEGER NOT NULL DEFAULT 0"
            )
        # Seed pipeline_health from sync_state for (wallet, chain, protocol)
        # tuples that have synced before but have no liveness row yet. Without
        # this, the day the table ships every source reads "event sync has
        # never succeeded" — false, and it hides the one number that matters
        # ("BSC last synced 39 days ago").
        #
        # Why last_synced_at is the right seed: it only advances when a batch
        # actually contained events, so it lags the true last successful sync
        # whenever a wallet was simply quiet. That makes it a CONSERVATIVE
        # lower bound — seeding it can make a warning appear earlier than
        # strictly true, never later, so nothing broken gets hidden.
        #
        # last_snapshot_ok_at is seeded from the snapshots table, not left NULL:
        # a snapshot row IS direct evidence that fetch_positions succeeded at
        # that moment. Leaving it NULL made the renderer — which correctly
        # treats "unknown" as "not evidence of health" — raise a 🚨 "every value
        # shown is stale" against chains whose marks were written the same day.
        # A false alarm on healthy data is the same disease as a missed alarm on
        # broken data, so seed from the evidence that actually exists.
        #
        # INSERT OR IGNORE keeps this a one-time backfill — live rows written by
        # the runner always win.
        #
        # Done in Python rather than as one INSERT…SELECT: the joined form
        # silently produced NULL snapshot timestamps inside the INSERT while the
        # identical SELECT returned correct values standalone. A seed that runs
        # once against real data is the worst place to carry a subtlety like
        # that, and explicit binding is trivially testable.
        snap_ts: dict[tuple[str, str, str], int] = {
            (r["wallet"], r["chain"], r["protocol_id"]): int(r["ts"])
            for r in conn.execute(
                """SELECT wallet, chain, protocol_id, MAX(snapshot_ts) AS ts
                   FROM snapshots GROUP BY wallet, chain, protocol_id"""
            )
            if r["ts"] is not None
        }
        for row in conn.execute(
            "SELECT wallet, chain, protocol_id, last_synced_at FROM sync_state"
            " WHERE last_synced_at > 0"
        ).fetchall():
            key = (row["wallet"], row["chain"], row["protocol_id"])
            synced = int(row["last_synced_at"])
            snapped = snap_ts.get(key)
            # Fills gaps only — COALESCE keeps any value already recorded, so a
            # real success is never regressed and re-running is a no-op. Plain
            # INSERT OR IGNORE would be wrong: a row seeded by an earlier
            # version of this backfill (which left the snapshot timestamp NULL)
            # would keep that NULL forever and go on raising a false alarm.
            conn.execute(
                """INSERT INTO pipeline_health
                       (wallet, chain, protocol_id, last_run_at, last_sync_ok_at,
                        last_snapshot_ok_at, source, last_error)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                   ON CONFLICT(wallet, chain, protocol_id) DO UPDATE SET
                       last_run_at = MAX(pipeline_health.last_run_at,
                                         excluded.last_run_at),
                       last_sync_ok_at = COALESCE(pipeline_health.last_sync_ok_at,
                                                  excluded.last_sync_ok_at),
                       last_snapshot_ok_at = COALESCE(
                           pipeline_health.last_snapshot_ok_at,
                           excluded.last_snapshot_ok_at)""",
                (*key, max(synced, snapped or 0), synced, snapped),
            )

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
            return int(row["value"]) if row else 0

    # ── Wallet registry ───────────────────────────────────────────────────

    def add_wallet(self, address: str, label: str | None = None) -> None:
        addr = address.lower()
        now = int(datetime.now(UTC).timestamp())
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO wallets (address, label, enabled, added_at)
                   VALUES (?, ?, 1, ?)""",
                (addr, label, now),
            )

    def list_wallets(self, enabled_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM wallets"
        if enabled_only:
            sql += " WHERE enabled = 1"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def set_wallet_vault(self, address: str, vault_id: str | None) -> bool:
        """Record which custody vault holds a tracked address. None clears it.
        Returns False when the address is not a registered wallet."""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE wallets SET fireblocks_vault_id = ? WHERE address = ?",
                (vault_id, address.lower()),
            )
            return cur.rowcount > 0

    # ── Transfers and vault balances (custody) ────────────────────────────

    def upsert_transfers(self, transfers: list[Transfer]) -> int:
        """Insert transfers; ignore duplicates by transfer_uid. Returns #
        inserted. Never updates: a transfer, once recorded, is history."""
        if not transfers:
            return 0
        rows = [
            (
                t.transfer_uid,
                t.source,
                t.source_id,
                t.ts,
                t.chain.lower() if t.chain else None,
                t.tx_hash.lower() if t.tx_hash else None,
                t.asset,
                t.token_key,
                str(t.amount),
                float(t.usd_at_ts) if t.usd_at_ts is not None else None,
                t.from_ref,
                t.to_ref,
                json.dumps(t.meta) if t.meta else None,
            )
            for t in transfers
        ]
        with self.connect() as conn:
            cur = conn.executemany(
                """INSERT OR IGNORE INTO transfers
                   (transfer_uid, source, source_id, ts, chain, tx_hash, asset,
                    token_key, amount, usd_at_ts, from_ref, to_ref, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            return cur.rowcount

    def transfers_between(self, since_ts: int, until_ts: int) -> list[dict]:
        """Transfers with since_ts < ts <= until_ts, oldest first. `amount`
        comes back as Decimal."""
        with self.connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM transfers WHERE ts > ? AND ts <= ? ORDER BY ts",
                    (since_ts, until_ts),
                ).fetchall()
            ]
        for r in rows:
            r["amount"] = Decimal(r["amount"])
        return rows

    def write_vault_balances(self, balances: list[VaultBalance]) -> int:
        """Record observed custody balances. Idempotent on (vault, asset, ts)."""
        if not balances:
            return 0
        rows = [
            (
                b.vault_id,
                b.asset,
                b.ts,
                str(b.amount),
                float(b.price_usd) if b.price_usd is not None else None,
                b.source,
            )
            for b in balances
        ]
        with self.connect() as conn:
            cur = conn.executemany(
                """INSERT OR IGNORE INTO vault_balances
                   (vault_id, asset, ts, amount, price_usd, source)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
            return cur.rowcount

    def vault_balances_as_of(self, ts: int) -> list[dict]:
        """Latest observed balance per (vault, asset) at or before `ts`."""
        sql = """
            SELECT b.* FROM vault_balances b
            INNER JOIN (
                SELECT vault_id, asset, MAX(ts) AS t FROM vault_balances
                WHERE ts <= :ts GROUP BY vault_id, asset
            ) l ON b.vault_id = l.vault_id AND b.asset = l.asset AND b.ts = l.t
            ORDER BY b.vault_id, b.asset
        """
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, {"ts": ts}).fetchall()]
        for r in rows:
            r["amount"] = Decimal(r["amount"])
        return rows

    # ── Token registry ────────────────────────────────────────────────────

    def upsert_token(self, token: Token, coingecko_id: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO tokens (chain, address, symbol, decimals, coingecko_id)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(chain, address) DO UPDATE SET
                       symbol = excluded.symbol,
                       decimals = excluded.decimals,
                       coingecko_id = COALESCE(excluded.coingecko_id, tokens.coingecko_id)""",
                (
                    token.chain.value,
                    token.address.lower(),
                    token.symbol,
                    token.decimals,
                    coingecko_id,
                ),
            )

    def set_coingecko_id(self, chain: str, address: str, coingecko_id: str) -> bool:
        """Manually pin a CoinGecko coin ID for a token. Returns True if row was found."""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE tokens SET coingecko_id = ? WHERE chain = ? AND address = ?",
                (coingecko_id, chain, address.lower()),
            )
            return cur.rowcount > 0

    # ── Sync state ────────────────────────────────────────────────────────

    def get_last_synced_ts(self, wallet: str, chain: Chain, protocol_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT last_event_ts FROM sync_state
                   WHERE wallet = ? AND chain = ? AND protocol_id = ?""",
                (wallet.lower(), chain.value, protocol_id),
            ).fetchone()
            return int(row["last_event_ts"]) if row else 0

    def set_last_synced_ts(
        self, wallet: str, chain: Chain, protocol_id: str, last_event_ts: int
    ) -> None:
        now = int(datetime.now(UTC).timestamp())
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO sync_state (wallet, chain, protocol_id,
                                           last_event_ts, last_synced_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(wallet, chain, protocol_id) DO UPDATE SET
                       last_event_ts = MAX(sync_state.last_event_ts, excluded.last_event_ts),
                       last_synced_at = excluded.last_synced_at""",
                (wallet.lower(), chain.value, protocol_id, last_event_ts, now),
            )

    # ── Pipeline health ───────────────────────────────────────────────────

    def record_pipeline_health(
        self,
        wallet: str,
        chain: Chain,
        protocol_id: str,
        *,
        now: int,
        sync_ok: bool,
        snapshot_ok: bool,
        source: str | None,
        error: str | None,
        dropped_events: int = 0,
    ) -> None:
        """Upsert the liveness row for one (wallet, chain, adapter) pass.

        `last_run_at` always advances. `last_sync_ok_at` / `last_snapshot_ok_at`
        advance to `now` ONLY when their flag is True and otherwise KEEP the
        stored value — a stuck timestamp is precisely the signal that a stage
        has been silently failing.

        `source` behaves like the timestamps: a pass that reports no source at
        all (a hard failure, or one that returned no positions) KEEPS the last
        known value. Clobbering it with NULL would erase a known 'rpc_fallback'
        degradation on the first hard failure and silently drop the warning
        until the next successful fallback pass. A pass that does report a
        source overwrites, so recovery ('rpc_fallback' → 'subgraph') still
        shows up immediately.

        `last_error` deliberately does NOT work that way: a clean pass must
        clear it back to NULL, otherwise a fixed adapter reports its old error
        forever. `dropped_events` follows `last_error`, NOT the timestamps, for
        the same reason: it answers "did the most recent run lose anything", so
        a clean pass must be able to zero it. COALESCE-keeps-old would let one
        bad run nag forever with no way to clear it."""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO pipeline_health (wallet, chain, protocol_id, last_run_at,
                                                last_sync_ok_at, last_snapshot_ok_at,
                                                source, last_error,
                                                dropped_events_last_run)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(wallet, chain, protocol_id) DO UPDATE SET
                       last_run_at = excluded.last_run_at,
                       last_sync_ok_at = COALESCE(
                           excluded.last_sync_ok_at, pipeline_health.last_sync_ok_at),
                       last_snapshot_ok_at = COALESCE(
                           excluded.last_snapshot_ok_at, pipeline_health.last_snapshot_ok_at),
                       source = COALESCE(excluded.source, pipeline_health.source),
                       last_error = excluded.last_error,
                       dropped_events_last_run = excluded.dropped_events_last_run""",
                (
                    wallet.lower(),
                    chain.value,
                    protocol_id,
                    now,
                    now if sync_ok else None,
                    now if snapshot_ok else None,
                    source,
                    redact(error) if error else None,
                    int(dropped_events),
                ),
            )

    def prune_pipeline_health(self, wallets: list[str], protocol_ids: list[str]) -> int:
        """Drop liveness rows for wallets that are no longer registered or
        adapters that are no longer loaded, returning the number removed.

        Without this, removing a wallet or de-registering an adapter leaves a
        row whose *_ok_at timestamps never advance again — a permanent 🚨 for
        something nobody is tracking any more.

        Only safe to call with the FULL registered sets (an unfiltered run):
        passing a subset would delete rows for perfectly healthy sources that
        just weren't in scope. Empty inputs are treated as "unknown" and
        prune nothing, so a misconfigured pass can't wipe the table."""
        if not wallets or not protocol_ids:
            return 0
        w_marks = ",".join("?" * len(wallets))
        p_marks = ",".join("?" * len(protocol_ids))
        with self.connect() as conn:
            cur = conn.execute(
                # noqa justified: the only interpolated text is a run of '?'
                # placeholders sized by len() — every value is still bound.
                f"""DELETE FROM pipeline_health
                    WHERE wallet NOT IN ({w_marks})
                       OR protocol_id NOT IN ({p_marks})""",  # noqa: S608
                [w.lower() for w in wallets] + list(protocol_ids),
            )
            return int(cur.rowcount)

    def pipeline_health(self) -> list[dict]:
        """Every liveness row, ordered by (chain, protocol_id, wallet)."""
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM pipeline_health ORDER BY chain, protocol_id, wallet"
                ).fetchall()
            ]

    # ── Pool flow history (Phase 5.3) ─────────────────────────────────────

    def apply_pool_window(
        self,
        chain: str,
        protocol_id: str,
        days: list[dict],
        cursors: dict[str, int | str],
        expected: dict[str, int | str | None] | None = None,
        cursor_kind: str = "swap_cursor",
    ) -> bool:
        """Add one scan window's per-day sums and advance each scanned pool's
        cursor, atomically: a crash between the two would count a window
        twice on the rerun, or skip it. With `expected` (each pool's cursor
        when the scan began), nothing is written and False is returned if
        any cursor has moved since — another pass banked those blocks.
        `cursor_kind` names the cursor: 'swap_cursor' (the last block
        scanned) or 'fee_counter' (the last day-close sample, as JSON)."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for pool, want in (expected or {}).items():
                    row = conn.execute(
                        "SELECT value FROM kv_store WHERE key = ?",
                        (f"{cursor_kind}:{chain}:{protocol_id}:{pool}",),
                    ).fetchone()
                    if (row["value"] if row else None) != (None if want is None else str(want)):
                        conn.execute("ROLLBACK")
                        return False
                for d in days:
                    conn.execute(
                        """INSERT INTO pool_days (chain, protocol_id, pool, day, swaps, samples,
                               source, fees_token1, l_sum, book_l_sum, book_l_open, tick_open,
                               tick_close, first_block, last_block)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(chain, protocol_id, pool, day) DO UPDATE SET
                               swaps = swaps + excluded.swaps,
                               samples = samples + excluded.samples,
                               fees_token1 = fees_token1 + excluded.fees_token1,
                               l_sum = l_sum + excluded.l_sum,
                               book_l_sum = book_l_sum + excluded.book_l_sum,
                               book_l_open = CASE WHEN excluded.first_block < first_block
                                   THEN excluded.book_l_open ELSE book_l_open END,
                               tick_open = CASE WHEN excluded.first_block < first_block
                                   THEN excluded.tick_open ELSE tick_open END,
                               tick_close = CASE WHEN excluded.last_block > last_block
                                   THEN excluded.tick_close ELSE tick_close END,
                               first_block = MIN(first_block, excluded.first_block),
                               last_block = MAX(last_block, excluded.last_block)""",
                        (
                            chain, protocol_id, d["pool"], d["day"], d["swaps"],
                            d.get("samples", d["swaps"]), d.get("source", "swaps"),
                            d["fees_token1"], d["l_sum"], d["book_l_sum"], d["book_l_open"],
                            d["tick_open"], d["tick_close"], d["first_block"], d["last_block"],
                        ),
                    )  # fmt: skip
                for pool, value in cursors.items():
                    conn.execute(
                        "INSERT INTO kv_store (key, value) VALUES (?, ?)"
                        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (f"{cursor_kind}:{chain}:{protocol_id}:{pool}", str(value)),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return True

    def swap_cursor(self, chain: str, protocol_id: str, pool: str) -> int | None:
        """The last block whose swaps are in pool_days for this pool."""
        v = self.kv_get(f"swap_cursor:{chain}:{protocol_id}:{pool}")
        return int(v) if v is not None else None

    def pool_days(self, chain: str | None = None) -> list[dict]:
        sql = "SELECT * FROM pool_days"
        args: tuple = ()
        if chain:
            sql += " WHERE chain = ?"
            args = (chain,)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY chain, pool, day", args)]

    def liquidity_events(self, chain: str, protocol_id: str) -> list[dict]:
        """Every deposit/withdraw with its liquidity change, in chain order —
        the book's own depth in each pool over time."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT position_key, block_number, log_index, ts, kind, meta_json
                   FROM events WHERE chain = ? AND protocol_id = ?
                     AND kind IN ('deposit', 'withdraw')
                   ORDER BY block_number, log_index""",
                (chain, protocol_id),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Key-value store ───────────────────────────────────────────────────

    def kv_get(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO kv_store (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ── Events ────────────────────────────────────────────────────────────

    def upsert_events(self, events: list[Event]) -> int:
        """Insert events; ignore duplicates by event_uid. Returns # inserted."""
        if not events:
            return 0
        rows = [
            (
                ev.event_uid,
                ev.wallet.lower(),
                ev.chain.value,
                ev.protocol_id,
                ev.position_key,
                ev.tx_hash,
                ev.log_index,
                ev.ts,
                ev.block_number,
                ev.kind.value,
                float(ev.usd_at_ts),
                json.dumps(
                    [
                        {
                            "token_key": ta.token.key,
                            "symbol": ta.token.symbol,
                            "amount": str(ta.amount),
                            "price_at_ts": str(ev.prices_at_ts.get(ta.token.key, Decimal("0"))),
                        }
                        for ta in ev.amounts
                    ]
                ),
                json.dumps(ev.meta) if ev.meta else None,
            )
            for ev in events
        ]

        with self.connect() as conn:
            cur = conn.executemany(
                """INSERT OR IGNORE INTO events
                   (event_uid, wallet, chain, protocol_id, position_key,
                    tx_hash, log_index, ts, block_number, kind,
                    usd_at_ts, amounts_json, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            return cur.rowcount

    def events_for_position(
        self,
        wallet: str,
        chain: Chain,
        protocol_id: str,
        position_key: str,
        until_ts: int | None = None,
    ) -> list[dict]:
        """Events for one position, oldest first; `until_ts` keeps only those
        at or before it (a snapshot reconstructed as of a past moment)."""
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    """SELECT * FROM events
                   WHERE wallet=? AND chain=? AND protocol_id=? AND position_key=?
                     AND (? IS NULL OR ts <= ?)
                   ORDER BY ts ASC, log_index ASC""",
                    (wallet.lower(), chain.value, protocol_id, position_key, until_ts, until_ts),
                ).fetchall()
            ]

    def event_ts_bounds(
        self,
        wallet: str,
        chain: Chain,
        protocol_id: str,
        position_key: str,
    ) -> tuple[int | None, int | None]:
        """Return (first_ts, last_ts) from events for this position, or (None, None)."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT MIN(ts), MAX(ts) FROM events
                   WHERE wallet=? AND chain=? AND protocol_id=? AND position_key=?""",
                (wallet.lower(), chain.value, protocol_id, position_key),
            ).fetchone()
        if row is None or row[0] is None:
            return None, None
        return int(row[0]), int(row[1])

    # ── Snapshots ─────────────────────────────────────────────────────────

    def write_snapshot(
        self,
        position: Position,
        snapshot_date: date | None = None,
        cost_basis_usd: Decimal | None = None,
        hold_value_usd: Decimal | None = None,
        il_usd: Decimal | None = None,
        pnl_usd: Decimal | None = None,
        is_closed: bool = False,
    ) -> None:
        """Write one position snapshot. Cost basis/IL/PnL come from analytics layer."""
        snap_date = (snapshot_date or datetime.now(UTC).date()).isoformat()
        il_pct = None
        if il_usd is not None and hold_value_usd and hold_value_usd > 0:
            il_pct = float(il_usd / hold_value_usd * 100)

        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO snapshots (
                    snapshot_date, wallet, chain, protocol_id, position_key,
                    protocol_kind, pair_label, snapshot_ts,
                    current_value_usd, unclaimed_usd, unclaimed_is_known,
                    cost_basis_usd, hold_value_usd, il_usd, il_pct, pnl_usd,
                    is_closed,
                    in_range, tick_lower, tick_upper, tick_current,
                    pool_tvl_usd, seven_day_fees_usd, position_share_pct, fee_tier_bps,
                    current_balances_json, unclaimed_balances_json, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snap_date,
                    position.wallet.lower(),
                    position.chain.value,
                    position.protocol_id,
                    position.position_key,
                    position.protocol_kind.value,
                    position.pair_label,
                    position.snapshot_at or int(datetime.now(UTC).timestamp()),
                    float(position.current_value_usd),
                    # The column is NOT NULL (SQLite cannot drop that without a
                    # table rebuild), so an unreadable balance stores 0 and is
                    # marked unknown alongside. Every reader must consult the
                    # flag — the 0 here is a placeholder, not a measurement.
                    float(position.unclaimed_usd or 0),
                    1 if position.unclaimed_usd is not None else 0,
                    float(cost_basis_usd) if cost_basis_usd is not None else None,
                    float(hold_value_usd) if hold_value_usd is not None else None,
                    float(il_usd) if il_usd is not None else None,
                    il_pct,
                    float(pnl_usd) if pnl_usd is not None else None,
                    int(is_closed),
                    int(position.in_range) if position.in_range is not None else None,
                    position.tick_lower,
                    position.tick_upper,
                    position.tick_current,
                    # `is not None`, not truthiness: a genuine 0 (an out-of-range
                    # position's share of active liquidity, a quiet week's fees)
                    # is a measurement, and must not be stored as unknown.
                    float(position.pool_tvl_usd) if position.pool_tvl_usd is not None else None,
                    float(position.seven_day_fees_usd)
                    if position.seven_day_fees_usd is not None
                    else None,
                    float(position.position_share_pct)
                    if position.position_share_pct is not None
                    else None,
                    position.fee_tier_bps,
                    json.dumps(
                        [
                            {
                                "token_key": ta.token.key,
                                "symbol": ta.token.symbol,
                                "amount": str(ta.amount),
                            }
                            for ta in position.current_balances
                        ]
                    ),
                    json.dumps(
                        [
                            {
                                "token_key": ta.token.key,
                                "symbol": ta.token.symbol,
                                "amount": str(ta.amount),
                            }
                            for ta in position.unclaimed_balances
                        ]
                    )
                    if position.unclaimed_balances
                    else None,
                    json.dumps(position.meta) if position.meta else None,
                ),
            )

    def open_position_identities(self, wallet: str, chain: Chain, protocol_id: str) -> list[dict]:
        """Positions whose most recent snapshot is not a tombstone — i.e. the
        set the runner believed was open as of the last successful pass.
        Returns dicts with position_key, pair_label, protocol_kind."""
        sql = """
            SELECT s.position_key, s.pair_label, s.protocol_kind
            FROM snapshots s
            INNER JOIN (
                SELECT wallet, chain, protocol_id, position_key,
                       MAX(snapshot_date) AS d
                FROM snapshots
                WHERE wallet = ? AND chain = ? AND protocol_id = ?
                GROUP BY wallet, chain, protocol_id, position_key
            ) latest
              ON s.wallet=latest.wallet AND s.chain=latest.chain
             AND s.protocol_id=latest.protocol_id
             AND s.position_key=latest.position_key
             AND s.snapshot_date=latest.d
            WHERE s.is_closed = 0
        """
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(sql, (wallet.lower(), chain.value, protocol_id)).fetchall()
            ]

    def all_position_identities(self, wallet: str, chain: Chain, protocol_id: str) -> list[dict]:
        """Every position ever snapshotted for this source, closed ones
        included, each represented by its most recent row that still CARRIES
        DATA.

        Deliberately NOT "the latest row": write_tombstone_snapshot inserts a
        stub with meta_json NULL and current_balances_json '[]', so for a closed
        position the latest row is precisely the one with nothing to reconstruct
        from. Filtering to is_closed = 0 on BOTH sides of the join picks the last
        live snapshot — the last moment the tracker actually knew what this
        position was.

        Ordered newest-first so a caller that has to cap the set drops the least
        recently active positions, which are the least likely to still be
        producing events ahead of the scan cursor.

        Returns position_key, pair_label, protocol_kind, snapshot_date,
        meta_json, current_balances_json.
        """
        sql = """
            SELECT s.position_key, s.pair_label, s.protocol_kind, s.snapshot_date,
                   s.meta_json, s.current_balances_json
            FROM snapshots s
            INNER JOIN (
                SELECT wallet, chain, protocol_id, position_key,
                       MAX(snapshot_date) AS d
                FROM snapshots
                WHERE wallet = ? AND chain = ? AND protocol_id = ? AND is_closed = 0
                GROUP BY wallet, chain, protocol_id, position_key
            ) latest
              ON s.wallet=latest.wallet AND s.chain=latest.chain
             AND s.protocol_id=latest.protocol_id
             AND s.position_key=latest.position_key
             AND s.snapshot_date=latest.d
            WHERE s.is_closed = 0
            ORDER BY s.snapshot_date DESC
        """
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(sql, (wallet.lower(), chain.value, protocol_id)).fetchall()
            ]

    def latest_snapshot_row(
        self, wallet: str, chain: Chain, protocol_id: str, position_key: str
    ) -> dict | None:
        """Most recent non-tombstone snapshot for one position, or None.
        Used by the runner (before writing today's row) to detect state
        transitions for alerting."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM snapshots
                   WHERE wallet=? AND chain=? AND protocol_id=? AND position_key=?
                     AND is_closed = 0
                   ORDER BY snapshot_date DESC LIMIT 1""",
                (wallet.lower(), chain.value, protocol_id, position_key),
            ).fetchone()
        return dict(row) if row else None

    def write_tombstone_snapshot(
        self,
        wallet: str,
        chain: Chain,
        protocol_id: str,
        position_key: str,
        pair_label: str,
        protocol_kind: str,
        snapshot_date: date | None = None,
    ) -> None:
        """Mark a position closed: a zero-value snapshot row with is_closed=1.
        Written by the runner when a previously-open position is absent from a
        successful fetch_positions pass."""
        snap_date = (snapshot_date or datetime.now(UTC).date()).isoformat()
        now = int(datetime.now(UTC).timestamp())
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO snapshots (
                    snapshot_date, wallet, chain, protocol_id, position_key,
                    protocol_kind, pair_label, snapshot_ts,
                    current_value_usd, unclaimed_usd, is_closed,
                    current_balances_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1, '[]')""",
                (
                    snap_date,
                    wallet.lower(),
                    chain.value,
                    protocol_id,
                    position_key,
                    protocol_kind,
                    pair_label,
                    now,
                ),
            )

    # ── Reporting queries ─────────────────────────────────────────────────
    # These are the queries that power MTD / MoM / portfolio views.
    #
    # All filters use NULL-tolerant parameter binding (`:w IS NULL OR
    # wallet = :w`) so we can pass a single static SQL string with
    # parameterized values — no string interpolation, no S608 false
    # positives, and SQL injection is structurally impossible.

    def latest_snapshot(self, wallet: str | None = None) -> list[dict]:
        """Most recent snapshot per position, optionally filtered by wallet.
        Positions whose latest row is a tombstone (closed) are excluded."""
        sql = """
            SELECT s.* FROM snapshots s
            INNER JOIN (
                SELECT wallet, chain, protocol_id, position_key,
                       MAX(snapshot_date) AS d
                FROM snapshots
                WHERE (:w IS NULL OR wallet = :w)
                GROUP BY wallet, chain, protocol_id, position_key
            ) latest
              ON s.wallet=latest.wallet AND s.chain=latest.chain
             AND s.protocol_id=latest.protocol_id
             AND s.position_key=latest.position_key
             AND s.snapshot_date=latest.d
            WHERE s.is_closed = 0
        """
        with self.connect() as conn:
            params = {"w": wallet.lower() if wallet else None}
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def snapshots_in_range(
        self,
        wallet: str | None = None,
        chain: Chain | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict]:
        """All snapshots within a date range, for time-series analysis."""
        sql = """
            SELECT * FROM snapshots
            WHERE (:w IS NULL OR wallet = :w)
              AND (:c IS NULL OR chain = :c)
              AND (:s IS NULL OR snapshot_date >= :s)
              AND (:e IS NULL OR snapshot_date <= :e)
            ORDER BY snapshot_date ASC, wallet, chain, protocol_id, position_key
        """
        params = {
            "w": wallet.lower() if wallet else None,
            "c": chain.value if chain else None,
            "s": start_date.isoformat() if start_date else None,
            "e": end_date.isoformat() if end_date else None,
        }
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def month_to_date_pnl(
        self,
        wallet: str | None = None,
        year: int | None = None,
        month: int | None = None,
    ) -> list[dict]:
        """
        Per-position change over a calendar month: the month's last snapshot vs
        the last snapshot before it. Returns one row per position open at the
        month's last snapshot with delta_value, fees_collected_mtd, etc.

        `pnl_mtd_usd` is Δvalue + Δunclaimed + collects — flows are NOT netted,
        so a deposit reads as a gain; `period_pnl` is the flow-netted figure.
        Unclaimed that could not be read at either end makes Δunclaimed and
        `pnl_mtd_usd` None rather than a made-up move to or from $0.

        If year/month omitted, uses current UTC month.
        """
        today = datetime.now(UTC).date()
        y = year or today.year
        m = month or today.month
        first = date(y, m, 1)
        month_start = first.isoformat()
        month_end = date(y + (m == 12), m % 12 + 1, 1).isoformat()  # exclusive
        prev_month_end = first.fromordinal(first.toordinal() - 1).isoformat()

        sql = """
        WITH curr AS (
            SELECT s.* FROM snapshots s
            INNER JOIN (
                SELECT wallet, chain, protocol_id, position_key,
                       MAX(snapshot_date) AS d
                FROM snapshots
                WHERE snapshot_date >= :month_start
                  AND snapshot_date < :month_end
                  AND (:w IS NULL OR wallet = :w)
                GROUP BY wallet, chain, protocol_id, position_key
            ) lc
              ON s.wallet=lc.wallet AND s.chain=lc.chain
             AND s.protocol_id=lc.protocol_id AND s.position_key=lc.position_key
             AND s.snapshot_date=lc.d
            WHERE s.is_closed = 0
        ),
        baseline AS (
            SELECT s.* FROM snapshots s
            INNER JOIN (
                SELECT wallet, chain, protocol_id, position_key,
                       MAX(snapshot_date) AS d
                FROM snapshots
                WHERE snapshot_date <= :prev_end
                  AND (:w IS NULL OR wallet = :w)
                GROUP BY wallet, chain, protocol_id, position_key
            ) lb
              ON s.wallet=lb.wallet AND s.chain=lb.chain
             AND s.protocol_id=lb.protocol_id AND s.position_key=lb.position_key
             AND s.snapshot_date=lb.d
        )
        SELECT
            c.wallet, c.chain, c.protocol_id, c.position_key, c.pair_label,
            c.in_range,
            c.current_value_usd                                   AS value_now,
            COALESCE(b.current_value_usd, 0)                      AS value_month_start,
            c.current_value_usd - COALESCE(b.current_value_usd,0) AS delta_value_usd,
            c.unclaimed_usd                                       AS unclaimed_now,
            COALESCE(b.unclaimed_usd, 0)                          AS unclaimed_month_start,
            c.unclaimed_is_known                                  AS unclaimed_known_now,
            COALESCE(b.unclaimed_is_known, 1)                     AS unclaimed_known_start,
            c.il_usd                                              AS il_now,
            COALESCE(b.il_usd, 0)                                 AS il_month_start,
            c.il_usd - COALESCE(b.il_usd, 0)                      AS il_change_mtd,
            c.cost_basis_usd
        FROM curr c
        LEFT JOIN baseline b USING (wallet, chain, protocol_id, position_key)
        ORDER BY c.current_value_usd DESC
        """

        params: dict = {
            "month_start": month_start,
            "month_end": month_end,
            "prev_end": prev_month_end,
            "w": wallet.lower() if wallet else None,
        }

        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        # Fee component computed from events table — sum COLLECT events
        # within the month per position.
        fee_sql = """
            SELECT wallet, chain, protocol_id, position_key,
                   SUM(usd_at_ts) AS fees_collected_mtd
            FROM events
            WHERE kind = 'collect'
              AND ts >= CAST(strftime('%s', :month_start) AS INTEGER)
              AND ts < CAST(strftime('%s', :month_end) AS INTEGER)
              AND (:w IS NULL OR wallet = :w)
            GROUP BY wallet, chain, protocol_id, position_key
        """
        with self.connect() as conn:
            fees = {
                (r["wallet"], r["chain"], r["protocol_id"], r["position_key"]): r[
                    "fees_collected_mtd"
                ]
                for r in conn.execute(fee_sql, params).fetchall()
            }

        def dec(v) -> Decimal:
            return Decimal(str(v or 0))

        for r in rows:
            r["fees_collected_mtd"] = dec(
                fees.get((r["wallet"], r["chain"], r["protocol_id"], r["position_key"]))
            )
            known = r.pop("unclaimed_known_now"), r.pop("unclaimed_known_start")
            if not all(known):
                r["unclaimed_now"] = r["unclaimed_month_start"] = None
                r["pnl_mtd_usd"] = None
                continue
            r["pnl_mtd_usd"] = (
                dec(r["delta_value_usd"])
                + (dec(r["unclaimed_now"]) - dec(r["unclaimed_month_start"]))
                + r["fees_collected_mtd"]
            )
        return rows

    def month_over_month(
        self,
        wallet: str | None = None,
        months: int = 6,
    ) -> list[dict]:
        """
        Per-month aggregates for the trailing `months` months.
        Returns one row per (month, wallet, chain, protocol_id) with:
          - end_value_usd:    portfolio value on last day of month
          - il_eom_usd:       IL on last day of month
          - fees_in_month:    sum of COLLECT events that month
          - delta_vs_prev:    value diff vs previous month
        """
        sql = """
        WITH month_ends AS (
            SELECT
                substr(snapshot_date, 1, 7) AS ym,
                wallet, chain, protocol_id, position_key,
                MAX(snapshot_date)          AS eom_date
            FROM snapshots
            WHERE snapshot_date >= date('now', :months_ago)
              AND (:w IS NULL OR wallet = :w)
            GROUP BY ym, wallet, chain, protocol_id, position_key
        ),
        month_end_snapshots AS (
            SELECT me.ym, s.*
            FROM month_ends me
            JOIN snapshots s
              ON s.wallet=me.wallet AND s.chain=me.chain
             AND s.protocol_id=me.protocol_id
             AND s.position_key=me.position_key
             AND s.snapshot_date=me.eom_date
            WHERE s.is_closed = 0
        )
        SELECT
            ym                                  AS month,
            wallet, chain, protocol_id,
            SUM(current_value_usd)              AS end_value_usd,
            SUM(unclaimed_usd)                  AS end_unclaimed_usd,
            SUM(COALESCE(il_usd, 0))            AS il_eom_usd,
            SUM(COALESCE(cost_basis_usd, 0))    AS cost_basis_eom_usd
        FROM month_end_snapshots
        GROUP BY ym, wallet, chain, protocol_id
        ORDER BY ym ASC
        """
        params: dict = {
            "months_ago": f"-{months} months",
            "w": wallet.lower() if wallet else None,
        }

        with self.connect() as conn:
            value_rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        # Add fees-in-month
        fee_sql = """
            SELECT strftime('%Y-%m', datetime(ts, 'unixepoch')) AS month,
                   wallet, chain, protocol_id,
                   SUM(usd_at_ts) AS fees_in_month
            FROM events
            WHERE kind = 'collect'
              AND ts >= strftime('%s', date('now', :months_ago))
              AND (:w IS NULL OR wallet = :w)
            GROUP BY month, wallet, chain, protocol_id
        """

        with self.connect() as conn:
            fee_map = {
                (r["month"], r["wallet"], r["chain"], r["protocol_id"]): r["fees_in_month"]
                for r in conn.execute(fee_sql, params).fetchall()
            }

        for r in value_rows:
            r["fees_in_month"] = fee_map.get(
                (r["month"], r["wallet"], r["chain"], r["protocol_id"]),
                0.0,
            )

        # Compute month-over-month delta (group by adapter/wallet/chain)
        def _group_key(row: dict) -> tuple:
            return (row["wallet"], row["chain"], row["protocol_id"])

        value_rows.sort(key=lambda r: (_group_key(r), r["month"]))
        prev_value: dict = {}
        for r in value_rows:
            k = _group_key(r)
            r["delta_vs_prev_month"] = (
                r["end_value_usd"] - prev_value[k] if k in prev_value else None
            )
            prev_value[k] = r["end_value_usd"]
        return value_rows

    # ── Period inputs (D / WTD / MTD / QTD / YTD / ITD) ───────────────────

    def snapshots_as_of(self, as_of: date) -> list[dict]:
        """Latest snapshot per position dated on or before `as_of`.

        Tombstones are INCLUDED, unlike `latest_snapshot`: a position closed
        inside a period ends it at zero, and a period that dropped it would
        lose the withdrawal that closed it."""
        sql = """
            SELECT s.* FROM snapshots s
            INNER JOIN (
                SELECT wallet, chain, protocol_id, position_key,
                       MAX(snapshot_date) AS d
                FROM snapshots
                WHERE snapshot_date <= :as_of
                GROUP BY wallet, chain, protocol_id, position_key
            ) l
              ON s.wallet=l.wallet AND s.chain=l.chain
             AND s.protocol_id=l.protocol_id AND s.position_key=l.position_key
             AND s.snapshot_date=l.d
        """
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, {"as_of": as_of.isoformat()}).fetchall()]

    def position_event_summary(self) -> list[dict]:
        """Per position with any event: first and last event time, how many withdrawals
        (evidence of an exit), and a label for positions never snapshotted.

        A period needs the first to tell "opened inside the window" (baseline
        is genuinely zero) from "existed but has no mark at the start" (the
        baseline is unknown), and the second to tell a real exit from a
        position whose adapter merely stopped reporting it."""
        sql = """
            SELECT wallet, chain, protocol_id, position_key,
                   MIN(ts) AS first_ts,
                   MAX(ts) AS last_ts,
                   SUM(CASE WHEN kind = 'withdraw' THEN 1 ELSE 0 END) AS withdraw_count,
                   MIN(amounts_json) AS amounts_json
            FROM events
            GROUP BY wallet, chain, protocol_id, position_key
        """
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql).fetchall()]
        for row in rows:
            row["pair_label"] = (
                self._label_from_amounts(row.pop("amounts_json", None)) or row["position_key"]
            )
        return rows

    def events_between(self, since_ts: int, until_ts: int) -> list[dict]:
        """Every event with since_ts < ts <= until_ts — the flows and collects
        a period nets out. Half-open so adjacent periods never count an event
        twice."""
        sql = """
            SELECT wallet, chain, protocol_id, position_key, tx_hash, ts, kind, usd_at_ts
            FROM events
            WHERE ts > :since AND ts <= :until
            ORDER BY ts
        """
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(sql, {"since": since_ts, "until": until_ts}).fetchall()
            ]

    # ── Marks (sub-daily) ─────────────────────────────────────────────────

    def write_marks(
        self,
        positions: list[Position],
        ts: int | None = None,
        source: str | None = None,
    ) -> int:
        """Record what a set of positions was worth at one instant.

        One `ts` for the whole batch, taken once by the caller rather than
        per row: positions marked in the same pass are one observation of the
        book, and letting each row carry its own write time would make a
        cross-position series that never lines up.

        Idempotent by (position, ts) — re-running a pass within the same
        second overwrites rather than duplicating. Returns the row count.
        """
        if not positions:
            return 0
        at = ts if ts is not None else int(datetime.now(UTC).timestamp())
        rows = [
            (
                at,
                p.wallet,
                p.chain.value,
                p.protocol_id,
                p.position_key,
                float(p.current_value_usd),
                float(p.unclaimed_usd) if p.unclaimed_usd is not None else None,
                1 if p.unclaimed_known else 0,
                p.tick_lower,
                p.tick_upper,
                p.tick_current,
                None if p.in_range is None else int(p.in_range),
                str(p.liquidity) if p.liquidity is not None else None,
                source,
            )
            for p in positions
        ]
        with self.connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO marks (
                       ts, wallet, chain, protocol_id, position_key,
                       current_value_usd, unclaimed_usd, unclaimed_is_known,
                       tick_lower, tick_upper, tick_current, in_range,
                       liquidity, source
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    def marks_in_range(
        self,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[dict]:
        """Marks in [start_ts, end_ts], oldest first — the series every
        volatility and VaR estimator reads."""
        sql = """
            SELECT * FROM marks
            WHERE (:start IS NULL OR ts >= :start)
              AND (:end IS NULL OR ts <= :end)
            ORDER BY ts ASC
        """
        with self.connect() as conn:
            rows = conn.execute(sql, {"start": start_ts, "end": end_ts}).fetchall()
        return [dict(r) for r in rows]

    def mark_coverage(self) -> dict:
        """How much sub-daily history exists yet, and at what cadence.

        Every Phase 4 metric is a function of history not yet collected, so
        this is the number that says how far off they are. `median_gap_s` is
        the observed cadence rather than the configured one — a cron that
        silently stopped shows up here and nowhere else.
        """
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS marks,
                          COUNT(DISTINCT ts) AS passes,
                          (SELECT COUNT(*) FROM (
                               SELECT DISTINCT wallet, chain, protocol_id, position_key
                               FROM marks)) AS positions,
                          MIN(ts) AS first_ts,
                          MAX(ts) AS last_ts
                   FROM marks"""
            ).fetchone()
            stamps = [r["ts"] for r in conn.execute("SELECT DISTINCT ts FROM marks ORDER BY ts")]
        gaps = sorted(b - a for a, b in zip(stamps, stamps[1:], strict=False))
        median_gap = gaps[len(gaps) // 2] if gaps else None
        return {
            "marks": row["marks"],
            "passes": row["passes"],
            "positions": row["positions"],
            "first_ts": row["first_ts"],
            "last_ts": row["last_ts"],
            "median_gap_s": median_gap,
        }

    # ── Transaction costs ─────────────────────────────────────────────────

    def upsert_tx_cost(
        self,
        chain: str,
        tx_hash: str,
        ts: int,
        *,
        block_number: int | None = None,
        gas_native: Decimal | None = None,
        gas_usd: Decimal | None = None,
        native_symbol: str | None = None,
        native_price_usd: Decimal | None = None,
        slippage_usd: Decimal | None = None,
        source: str = "receipt",
    ) -> None:
        """Record what one transaction cost. Idempotent by (chain, tx_hash)."""
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO tx_costs (
                       chain, tx_hash, ts, block_number, gas_native, gas_usd,
                       native_symbol, native_price_usd, slippage_usd, source, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chain,
                    tx_hash.lower(),
                    ts,
                    block_number,
                    float(gas_native) if gas_native is not None else None,
                    float(gas_usd) if gas_usd is not None else None,
                    native_symbol,
                    float(native_price_usd) if native_price_usd is not None else None,
                    float(slippage_usd) if slippage_usd is not None else None,
                    source,
                    int(datetime.now(UTC).timestamp()),
                ),
            )

    def transactions_missing_cost(
        self, chain: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """Distinct (chain, tx_hash, ts, block_number) from `events` that have
        no priced `tx_costs` row yet — the work list for a gas backfill.

        A row whose gas_usd is NULL is still missing: the receipt was read but
        the native token had no price, and a later pass may find one."""
        sql = """
            SELECT e.chain, e.tx_hash, MIN(e.ts) AS ts, MIN(e.block_number) AS block_number
            FROM events e
            LEFT JOIN tx_costs c ON c.chain = e.chain AND c.tx_hash = e.tx_hash
            WHERE c.gas_usd IS NULL
              AND (:chain IS NULL OR e.chain = :chain)
            GROUP BY e.chain, e.tx_hash
            ORDER BY ts DESC
        """
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, {"chain": chain}).fetchall()]

    def tx_cost_rows(self, chain: str | None = None) -> list[dict]:
        """Every transaction that touched a tracked position, left-joined to
        what it cost. Rows with no cost row yet come back with NULL gas — the
        caller needs those to state coverage rather than quietly averaging
        over the transactions it happens to know about."""
        sql = """
            SELECT e.chain, e.tx_hash, MIN(e.ts) AS ts,
                   c.gas_usd, c.gas_native, c.native_symbol, c.slippage_usd
            FROM events e
            LEFT JOIN tx_costs c ON c.chain = e.chain AND c.tx_hash = e.tx_hash
            WHERE (:chain IS NULL OR e.chain = :chain)
            GROUP BY e.chain, e.tx_hash
        """
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, {"chain": chain}).fetchall()]

    def value_delta(self) -> ValueDelta | None:
        """Change in book value between the latest marks and the most recent
        prior marks, as a `ValueDelta` that carries both dates.

        Matched per position, so one chain's snapshot pass lagging does not
        fake a portfolio-wide swing. Two exclusions keep the window honest:

          - positions whose *latest* mark is not the latest run's (a source
            that stopped reporting must not contribute a week-old move), and
          - positions whose *prior* mark predates the baseline — the most
            recent date any fresh position has a prior mark for. Those span a
            longer window than the label claims, so they are counted out and
            reported rather than averaged in.

        None when no position has two comparable snapshots yet.
        """
        sql = """
            WITH ranked AS (
                SELECT wallet, chain, protocol_id, position_key, current_value_usd,
                       snapshot_date,
                       ROW_NUMBER() OVER (
                           PARTITION BY wallet, chain, protocol_id, position_key
                           ORDER BY snapshot_date DESC
                       ) AS rn
                FROM snapshots WHERE is_closed = 0
            ),
            matched AS (
                SELECT wallet, chain, protocol_id, position_key,
                       MAX(CASE WHEN rn = 1 THEN current_value_usd END) AS curr,
                       MAX(CASE WHEN rn = 1 THEN snapshot_date END) AS curr_date,
                       MAX(CASE WHEN rn = 2 THEN current_value_usd END) AS prev,
                       MAX(CASE WHEN rn = 2 THEN snapshot_date END) AS prev_date
                FROM ranked WHERE rn <= 2
                GROUP BY wallet, chain, protocol_id, position_key
            ),
            fresh AS (
                SELECT * FROM matched
                WHERE prev IS NOT NULL
                  AND curr_date = (
                      SELECT MAX(snapshot_date) FROM snapshots WHERE is_closed = 0
                  )
            ),
            baseline AS (SELECT MAX(prev_date) AS d FROM fresh)
            SELECT
                (SELECT MAX(snapshot_date) FROM snapshots WHERE is_closed = 0) AS curr_date,
                (SELECT d FROM baseline) AS prev_date,
                SUM(CASE WHEN prev_date = (SELECT d FROM baseline) THEN curr END) AS curr,
                SUM(CASE WHEN prev_date = (SELECT d FROM baseline) THEN prev END) AS prev,
                COALESCE(SUM(
                    CASE WHEN prev_date < (SELECT d FROM baseline) THEN 1 ELSE 0 END
                ), 0) AS excluded_positions,
                COALESCE(SUM(
                    CASE WHEN prev_date < (SELECT d FROM baseline) THEN curr ELSE 0 END
                ), 0.0) AS excluded_value
            FROM fresh
        """
        with self.connect() as conn:
            row = conn.execute(sql).fetchone()
        if row is None or row["curr"] is None or row["prev"] is None:
            return None
        return ValueDelta(
            curr=float(row["curr"]),
            prev=float(row["prev"]),
            curr_date=row["curr_date"],
            prev_date=row["prev_date"],
            excluded_positions=int(row["excluded_positions"]),
            excluded_value=float(row["excluded_value"]),
        )

    def position_flows(self) -> list[dict]:
        """Per-position lifetime flows from events, tagged open/closed.

        net_invested_usd = Σ signed deposit/withdraw usd (deposits +, withdrawals −,
        each at event-time prices); fees_usd = Σ collects. A position is 'open'
        when its latest snapshot row is live (not a tombstone); positions with
        events but no live snapshot are 'closed' — their realized result
        (fees + withdrawals − deposits) is locked in and belongs in book PnL.

        Also returns two evidence columns the caller needs to tell a real
        realized loss from missing data: `withdraw_count` (a 'closed' position
        with zero withdraws was never actually withdrawn — its adapter just
        stopped reporting it) and `pair_label`, which falls back to the event
        token symbols and then the position_key so closed positions that never
        got a snapshot still render with a usable name."""
        sql = """
            WITH open_keys AS (
                SELECT s.wallet, s.chain, s.protocol_id, s.position_key
                FROM snapshots s
                INNER JOIN (
                    SELECT wallet, chain, protocol_id, position_key,
                           MAX(snapshot_date) AS d
                    FROM snapshots GROUP BY wallet, chain, protocol_id, position_key
                ) l ON s.wallet=l.wallet AND s.chain=l.chain
                   AND s.protocol_id=l.protocol_id AND s.position_key=l.position_key
                   AND s.snapshot_date=l.d
                WHERE s.is_closed = 0
            )
            SELECT e.wallet, e.chain, e.protocol_id, e.position_key,
                   SUM(CASE WHEN e.kind IN ('deposit','withdraw') THEN e.usd_at_ts ELSE 0 END)
                       AS net_invested_usd,
                   SUM(CASE WHEN e.kind = 'collect' THEN e.usd_at_ts ELSE 0 END) AS fees_usd,
                   SUM(CASE WHEN e.kind = 'withdraw' THEN 1 ELSE 0 END) AS withdraw_count,
                   EXISTS (
                       SELECT 1 FROM open_keys o
                       WHERE o.wallet=e.wallet AND o.chain=e.chain
                         AND o.protocol_id=e.protocol_id AND o.position_key=e.position_key
                   ) AS is_open,
                   (SELECT s.pair_label FROM snapshots s
                     WHERE s.wallet=e.wallet AND s.chain=e.chain
                       AND s.protocol_id=e.protocol_id AND s.position_key=e.position_key
                     ORDER BY s.snapshot_date DESC LIMIT 1) AS pair_label,
                   MIN(e.amounts_json) AS amounts_json
            FROM events e
            GROUP BY e.wallet, e.chain, e.protocol_id, e.position_key
        """
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql).fetchall()]
        for row in rows:
            amounts_json = row.pop("amounts_json", None)
            if not row.get("pair_label"):
                row["pair_label"] = self._label_from_amounts(amounts_json) or row["position_key"]
        return rows

    @staticmethod
    def _label_from_amounts(amounts_json: str | None) -> str | None:
        """'USDT/ETH' from an event's amounts_json, or None when unusable.

        Legacy rows predate the `symbol` field, hence the tolerant parse."""
        if not amounts_json:
            return None
        try:
            entries = json.loads(amounts_json)
        except (TypeError, ValueError):
            return None
        symbols = [s for e in entries if isinstance(e, dict) and (s := e.get("symbol"))]
        return "/".join(symbols) if symbols else None

    def position_window_stats(self, days: int = 30) -> list[dict]:
        """Per open position, snapshot-derived stats over the trailing window:
        average value, unclaimed at window start/end, and days of coverage.
        Feeds the realized-yield calculation (accrual-clean, harvest-lump-proof).

        unclaimed_start_known / unclaimed_end_known report whether each endpoint
        was actually readable; window_yields refuses to annualize when either is
        not."""
        sql = """
            WITH windowed AS (
                SELECT * FROM snapshots
                WHERE is_closed = 0 AND snapshot_date >= date('now', :cutoff)
            ),
            latest AS (
                SELECT wallet, chain, protocol_id, position_key,
                       MAX(snapshot_date) AS d_max, MIN(snapshot_date) AS d_min
                FROM windowed GROUP BY wallet, chain, protocol_id, position_key
            )
            SELECT w.wallet, w.chain, w.protocol_id, w.position_key,
                   MAX(w.pair_label) AS pair_label,
                   AVG(w.current_value_usd) AS avg_value_usd,
                   COUNT(*) AS snapshot_days,
                   CAST(julianday(l.d_max) - julianday(l.d_min) AS INTEGER) AS span_days,
                   (SELECT unclaimed_usd FROM windowed f
                     WHERE f.wallet=w.wallet AND f.chain=w.chain
                       AND f.protocol_id=w.protocol_id AND f.position_key=w.position_key
                       AND f.snapshot_date=l.d_min) AS unclaimed_start,
                   (SELECT unclaimed_usd FROM windowed f
                     WHERE f.wallet=w.wallet AND f.chain=w.chain
                       AND f.protocol_id=w.protocol_id AND f.position_key=w.position_key
                       AND f.snapshot_date=l.d_max) AS unclaimed_end,
                   -- The yield is a DELTA between these two endpoints, so an
                   -- unreadable one does not merely lose precision: paired with
                   -- a real one it fabricates a swing the size of the whole
                   -- balance, in whichever direction the unknown sits.
                   (SELECT unclaimed_is_known FROM windowed f
                     WHERE f.wallet=w.wallet AND f.chain=w.chain
                       AND f.protocol_id=w.protocol_id AND f.position_key=w.position_key
                       AND f.snapshot_date=l.d_min) AS unclaimed_start_known,
                   (SELECT unclaimed_is_known FROM windowed f
                     WHERE f.wallet=w.wallet AND f.chain=w.chain
                       AND f.protocol_id=w.protocol_id AND f.position_key=w.position_key
                       AND f.snapshot_date=l.d_max) AS unclaimed_end_known,
                   l.d_min AS window_start
            FROM windowed w
            JOIN latest l ON w.wallet=l.wallet AND w.chain=l.chain
                 AND w.protocol_id=l.protocol_id AND w.position_key=l.position_key
            GROUP BY w.wallet, w.chain, w.protocol_id, w.position_key
        """
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, {"cutoff": f"-{days} days"}).fetchall()]

    def collects_since(self, since_date: str) -> list[dict]:
        """COLLECT events on/after an ISO date (per position), for window yields."""
        sql = """
            SELECT wallet, chain, protocol_id, position_key, ts, usd_at_ts
            FROM events
            WHERE kind='collect' AND ts >= strftime('%s', :d)
        """
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, {"d": since_date}).fetchall()]

    def token_symbol_map(self) -> dict[str, str]:
        """{token_key: symbol} for every registered token — label fallback for
        legacy event rows whose amounts_json predates the symbol field."""
        with self.connect() as conn:
            return {
                f"{r['chain']}:{r['address']}": r["symbol"]
                for r in conn.execute("SELECT chain, address, symbol FROM tokens").fetchall()
            }

    def monthly_collects(self, wallet: str | None = None) -> list[dict]:
        """Every COLLECT event with its month, for fee-trend reporting.

        Sourced purely from the events table — independent of snapshots, so
        fee history is complete even for months before snapshotting existed
        and for positions that have since been closed."""
        sql = """
            SELECT strftime('%Y-%m', datetime(ts, 'unixepoch')) AS month,
                   ts, wallet, chain, protocol_id, position_key,
                   amounts_json, usd_at_ts, meta_json
            FROM events
            WHERE kind = 'collect'
              AND (:w IS NULL OR wallet = :w)
            ORDER BY ts ASC
        """
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(sql, {"w": wallet.lower() if wallet else None}).fetchall()
            ]

    def snapshot_holes(self) -> list[tuple[str, str, str, str]]:
        """(wallet, chain, protocol_id, 'YYYY-MM-DD') for every day with no
        snapshot for that feed, between its first and its latest snapshot.
        Days are UTC dates, as snapshot_date is."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT wallet, chain, protocol_id, snapshot_date FROM snapshots"
                " GROUP BY wallet, chain, protocol_id, snapshot_date"
            ).fetchall()
        have: dict[tuple[str, str, str], set[str]] = {}
        for r in rows:
            have.setdefault((r["wallet"], r["chain"], r["protocol_id"]), set()).add(
                r["snapshot_date"]
            )
        out: list[tuple[str, str, str, str]] = []
        for feed, days in have.items():
            d = date.fromisoformat(min(days))
            end = date.fromisoformat(max(days))
            while d < end:
                if d.isoformat() not in days:
                    out.append((*feed, d.isoformat()))
                d = d.fromordinal(d.toordinal() + 1)
        return sorted(out, key=lambda h: (h[3], h[0], h[1], h[2]))

    def unclaimed_series(self) -> list[dict]:
        """Every snapshot's unclaimed balance per position, as token
        quantities — what splits a multi-month sweep by month-end."""
        sql = """
            SELECT wallet, chain, protocol_id, position_key, snapshot_date, snapshot_ts,
                   unclaimed_usd, unclaimed_is_known, unclaimed_balances_json, is_closed
            FROM snapshots ORDER BY snapshot_ts
        """
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def active_wallet_chains(self) -> set[tuple[str, str]]:
        """{(wallet, chain)} with any event or snapshot — the feeds whose
        freshness a chain-level figure depends on."""
        sql = """
            SELECT DISTINCT wallet, chain FROM events
            UNION SELECT DISTINCT wallet, chain FROM snapshots
        """
        with self.connect() as conn:
            return {(r["wallet"], r["chain"]) for r in conn.execute(sql).fetchall()}

    # ── Alerts ────────────────────────────────────────────────────────────

    def write_alert(self, alert: Alert) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO alerts (position_uid, kind, severity, message,
                                       triggered_at, context_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    alert.position_uid,
                    alert.kind,
                    alert.severity.value,
                    alert.message,
                    alert.triggered_at,
                    json.dumps(alert.context) if alert.context else None,
                ),
            )
            # cur.lastrowid is Optional in typeshed but always int after INSERT
            return cur.lastrowid or 0

    def recently_alerted(self, position_uid: str, kind: str, within_hours: int = 24) -> bool:
        cutoff = int(datetime.now(UTC).timestamp()) - within_hours * 3600
        with self.connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM alerts
                   WHERE position_uid=? AND kind=? AND triggered_at >= ?
                   LIMIT 1""",
                (position_uid, kind, cutoff),
            ).fetchone()
            return row is not None

    def mark_alert_delivered(self, alert_id: int) -> None:
        now = int(datetime.now(UTC).timestamp())
        with self.connect() as conn:
            conn.execute("UPDATE alerts SET delivered_at=? WHERE id=?", (now, alert_id))

    def list_alerts(self, limit: int = 50, undelivered_only: bool = False) -> list[dict]:
        """Return recent alerts ordered by triggered_at DESC."""
        sql = "SELECT * FROM alerts"
        if undelivered_only:
            sql += " WHERE delivered_at IS NULL"
        sql += " ORDER BY triggered_at DESC LIMIT ?"
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]

    def undelivered_alerts(self) -> list[dict]:
        """Return all alerts that have not yet been delivered (delivered_at IS NULL)."""
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM alerts WHERE delivered_at IS NULL ORDER BY triggered_at ASC"
                ).fetchall()
            ]

    # ── Rebalance action queue ──────────────────────────────────────────────

    # A manually-resolved item won't reopen for this long, giving the on-chain
    # rebalance + next snapshot time to clear the signal on its own.
    _RESOLVE_GRACE_SECONDS = 2 * 86400

    def reconcile_rebalance_queue(self, actionable: list[dict]) -> None:
        """Sync the queue to the current trade tickets (tiers now/soon).

        `actionable` is a list of Ticket.queue_item() dicts: ``uid``,
        ``pair``, ``tier`` and a ``context`` kept for display. Each run:
        upsert a live 'open' episode per still-proposed position (a lapsed
        snooze flips back to open; a re-proposed resolved position opens a
        fresh episode), refresh tier/context, and auto-resolve any live item
        no longer proposed — so a ticket you executed drops off on the next
        run. Snoozed items stay snoozed until their timer
        lapses. Idempotent."""
        now = int(datetime.now(UTC).timestamp())
        grace_cut = now - self._RESOLVE_GRACE_SECONDS
        flagged = {s["uid"]: s for s in actionable}
        with self.connect() as conn:
            live = {
                r["position_uid"]: dict(r)
                for r in conn.execute(
                    "SELECT * FROM rebalance_queue WHERE status != 'resolved'"
                ).fetchall()
            }
            # Positions resolved within the grace window — don't reopen them yet,
            # so a just-marked-done item isn't reinstated by the same stale
            # snapshot before the next sync can confirm the rebalance.
            recently_resolved = {
                r["position_uid"]
                for r in conn.execute(
                    "SELECT position_uid, MAX(resolved_at) AS ra FROM rebalance_queue "
                    "WHERE status='resolved' GROUP BY position_uid HAVING ra > ?",
                    (grace_cut,),
                ).fetchall()
            }
            for uid, sig in flagged.items():
                ctx = json.dumps(sig.get("context") or {})
                row = live.get(uid)
                if row is None:
                    if uid in recently_resolved:
                        continue  # within grace — stay resolved
                    conn.execute(
                        """INSERT INTO rebalance_queue
                           (position_uid, pair_label, tier, status, first_seen,
                            last_seen, context_json)
                           VALUES (?, ?, ?, 'open', ?, ?, ?)""",
                        (uid, sig["pair"], sig["tier"], now, now, ctx),
                    )
                else:
                    # A snooze that has lapsed reverts to open.
                    status = row["status"]
                    if status == "snoozed" and (row["snooze_until"] or 0) <= now:
                        status = "open"
                    conn.execute(
                        """UPDATE rebalance_queue
                           SET tier=?, status=?, last_seen=?, context_json=?
                           WHERE id=?""",
                        (sig["tier"], status, now, ctx, row["id"]),
                    )
            # Auto-resolve live items that are no longer flagged.
            for uid, row in live.items():
                if uid not in flagged:
                    conn.execute(
                        """UPDATE rebalance_queue
                           SET status='resolved', resolved_at=?, resolved_reason='auto'
                           WHERE id=?""",
                        (now, row["id"]),
                    )

    def is_rebalance_snoozed(self, position_uid: str) -> bool:
        """True when the position has a live snoozed episode still within its
        snooze window — used to suppress the 🔴 push alert."""
        now = int(datetime.now(UTC).timestamp())
        with self.connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM rebalance_queue
                   WHERE position_uid=? AND status='snoozed' AND snooze_until > ?
                   LIMIT 1""",
                (position_uid, now),
            ).fetchone()
            return row is not None

    def snoozed_rebalance_uids(self) -> set[str]:
        """position_uids with an active snooze — the daily report hides these
        so a deferred item stops nagging until its timer lapses."""
        now = int(datetime.now(UTC).timestamp())
        with self.connect() as conn:
            return {
                r["position_uid"]
                for r in conn.execute(
                    "SELECT position_uid FROM rebalance_queue "
                    "WHERE status='snoozed' AND snooze_until > ?",
                    (now,),
                ).fetchall()
            }

    def list_rebalance_queue(self, include_resolved: bool = False) -> list[dict]:
        """Queue rows for display. Live (open/snoozed) first — 🔴 before 🟠,
        then longest-waiting — optionally followed by resolved history."""
        sql = "SELECT * FROM rebalance_queue"
        if not include_resolved:
            sql += " WHERE status != 'resolved'"
        sql += (
            " ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'snoozed' THEN 1 ELSE 2 END,"
            " CASE tier WHEN 'now' THEN 0 ELSE 1 END, first_seen ASC"
        )
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def resolve_rebalance_item(self, item_id: int, reason: str = "manual") -> bool:
        """Mark a live queue item resolved (a rebalance you've done). Returns
        False if the id isn't a live item."""
        now = int(datetime.now(UTC).timestamp())
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE rebalance_queue
                   SET status='resolved', resolved_at=?, resolved_reason=?
                   WHERE id=? AND status != 'resolved'""",
                (now, reason, item_id),
            )
            return cur.rowcount > 0

    def snooze_rebalance_item(self, item_id: int, until_ts: int) -> bool:
        """Defer a live queue item until until_ts. Returns False if the id
        isn't a live item."""
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE rebalance_queue
                   SET status='snoozed', snooze_until=?
                   WHERE id=? AND status != 'resolved'""",
                (until_ts, item_id),
            )
            return cur.rowcount > 0

    # ── Policy exceptions ─────────────────────────────────────────────────

    def open_exception_uids(self) -> set[str]:
        """check_uids with a live (uncleared) exception episode."""
        with self.connect() as conn:
            return {
                r["check_uid"]
                for r in conn.execute(
                    "SELECT check_uid FROM policy_exceptions WHERE cleared_at IS NULL"
                )
            }

    def reconcile_policy_exceptions(self, checks: list[dict], now: int) -> dict:
        """Bring the exception log in line with this pass's limit checks.

        Each check is a dict with uid, limit_id, subject, unit, observed,
        threshold and status. breach → open an episode or extend the live one
        (tracking the worst reading); ok → clear the live episode; unknown or
        unset → touch nothing. Returns {opened, updated, cleared}."""
        opened = updated = cleared = 0
        with self.connect() as conn:
            for c in checks:
                observed = str(c["observed"]) if c.get("observed") is not None else None
                live = conn.execute(
                    "SELECT id, worst_observed FROM policy_exceptions"
                    " WHERE check_uid = ? AND cleared_at IS NULL",
                    (c["uid"],),
                ).fetchone()
                if c["status"] == "breach":
                    if live is None:
                        conn.execute(
                            """INSERT INTO policy_exceptions
                               (check_uid, limit_id, subject, unit, threshold,
                                first_observed, last_observed, worst_observed,
                                opened_at, last_seen)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                c["uid"],
                                c["limit_id"],
                                c["subject"],
                                c["unit"],
                                str(c["threshold"]) if c.get("threshold") is not None else None,
                                observed,
                                observed,
                                observed,
                                now,
                                now,
                            ),
                        )
                        opened += 1
                    else:
                        worst = live["worst_observed"]
                        if observed is not None and (
                            worst is None or Decimal(observed) > Decimal(worst)
                        ):
                            worst = observed
                        conn.execute(
                            """UPDATE policy_exceptions
                               SET last_seen = ?, last_observed = ?, worst_observed = ?
                               WHERE id = ?""",
                            (now, observed, worst, live["id"]),
                        )
                        updated += 1
                elif c["status"] == "ok" and live is not None:
                    conn.execute(
                        "UPDATE policy_exceptions SET cleared_at = ?, last_observed = ?"
                        " WHERE id = ?",
                        (now, observed, live["id"]),
                    )
                    cleared += 1
        return {"opened": opened, "updated": updated, "cleared": cleared}

    def list_exceptions(
        self, since_ts: int | None = None, until_ts: int | None = None
    ) -> list[dict]:
        """Exception episodes live at any point in [since_ts, until_ts] (both
        optional), newest first."""
        sql = """
            SELECT * FROM policy_exceptions
            WHERE (:until IS NULL OR opened_at <= :until)
              AND (:since IS NULL OR cleared_at IS NULL OR cleared_at >= :since)
            ORDER BY opened_at DESC, id DESC
        """
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(sql, {"since": since_ts, "until": until_ts}).fetchall()
            ]

    def review_exception(
        self, exception_id: int, *, by: str, disposition: str, note: str | None, now: int
    ) -> bool:
        """Record a human review. Returns False when no such exception."""
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE policy_exceptions
                   SET reviewed_at = ?, reviewed_by = ?, disposition = ?, review_note = ?
                   WHERE id = ?""",
                (now, by, disposition, note, exception_id),
            )
            return cur.rowcount > 0

    # ── Incidents ─────────────────────────────────────────────────────────

    def add_incident(
        self,
        *,
        title: str,
        severity: str,
        detected_at: int,
        now: int,
        description: str | None = None,
        logged_by: str | None = None,
        escalated_to: str | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO incidents
                   (title, severity, description, detected_at, logged_at, logged_by,
                    escalated_to)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (title, severity, description, detected_at, now, logged_by, escalated_to),
            )
            return int(cur.lastrowid or 0)

    def update_incident(self, incident_id: int, **fields) -> bool:
        """Set lifecycle fields (escalated_to, contained_at, resolved_at,
        postmortem, description). Unknown fields raise."""
        allowed = {"escalated_to", "contained_at", "resolved_at", "postmortem", "description"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not an incident field: {sorted(unknown)}")
        if not fields:
            return False
        cols = ", ".join(f"{k} = ?" for k in fields)  # keys are from the allow-list
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE incidents SET {cols} WHERE id = ?",  # noqa: S608
                (*fields.values(), incident_id),
            )
            return cur.rowcount > 0

    def get_incident(self, incident_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return dict(row) if row else None

    def list_incidents(
        self, since_ts: int | None = None, until_ts: int | None = None
    ) -> list[dict]:
        """Incidents open at any point in [since_ts, until_ts], newest first."""
        sql = """
            SELECT * FROM incidents
            WHERE (:until IS NULL OR detected_at <= :until)
              AND (:since IS NULL OR resolved_at IS NULL OR resolved_at >= :since)
            ORDER BY detected_at DESC, id DESC
        """
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(sql, {"since": since_ts, "until": until_ts}).fetchall()
            ]

    # ── Contractual ledger ────────────────────────────────────────────────

    CONTRACT_FIELDS = (
        "counterparty",
        "kind",
        "status",
        "asset",
        "amount",
        "usd_value",
        "start_date",
        "end_date",
        "terms",
        "annual_cost_usd",
        "notice_by",
        "interest_rate_pct",
        "key_terms_json",
        "returned",
        "source_note",
    )

    def record_contract(self, ref: str, as_of: date, now: int, **fields) -> None:
        """Record one version of a contract. A second version on the same
        as_of date replaces the first (a same-day correction); every other
        change is a new version, and history is kept."""
        unknown = set(fields) - set(self.CONTRACT_FIELDS)
        if unknown:
            raise ValueError(f"not a contract field: {sorted(unknown)}")
        for required in ("counterparty", "kind", "status", "source_note"):
            if not fields.get(required):
                raise ValueError(f"a contract needs {required}")
        amount = fields.get("amount")
        usd = fields.get("usd_value")
        cost = fields.get("annual_cost_usd")
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO contracts
                   (ref, as_of, counterparty, kind, status, asset, amount, usd_value,
                    start_date, end_date, terms, annual_cost_usd, notice_by, interest_rate_pct,
                    key_terms_json, returned, source_note, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ref,
                    as_of.isoformat(),
                    fields["counterparty"],
                    fields["kind"],
                    fields["status"],
                    fields.get("asset"),
                    str(amount) if amount is not None else None,
                    float(usd) if usd is not None else None,
                    fields.get("start_date"),
                    fields.get("end_date"),
                    fields.get("terms"),
                    float(cost) if cost is not None else None,
                    fields.get("notice_by"),
                    fields.get("interest_rate_pct"),
                    fields.get("key_terms_json"),
                    str(fields["returned"]) if fields.get("returned") is not None else None,
                    fields["source_note"],
                    now,
                ),
            )

    def contracts_as_of(self, as_of: date | None = None) -> list[dict]:
        """The latest version of every contract known on `as_of` (default:
        every version ever recorded counts), ordered by expiry."""
        sql = """
            SELECT c.* FROM contracts c
            INNER JOIN (
                SELECT ref, MAX(as_of) AS a FROM contracts
                WHERE (:d IS NULL OR as_of <= :d) GROUP BY ref
            ) l ON c.ref = l.ref AND c.as_of = l.a
            ORDER BY c.end_date IS NULL, c.end_date, c.counterparty, c.ref
        """
        params = {"d": as_of.isoformat() if as_of else None}
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            r["amount"] = Decimal(r["amount"]) if r["amount"] is not None else None
            r["returned"] = Decimal(r["returned"]) if r.get("returned") is not None else None
        return rows

    def contract_history(self, ref: str) -> list[dict]:
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM contracts WHERE ref = ? ORDER BY as_of", (ref,)
                ).fetchall()
            ]

    TRANCHE_FIELDS = (
        "asset",
        "amount",
        "strike_usd",
        "strike_rule",
        "strike_low_usd",
        "strike_high_usd",
        "exercisable_from",
        "exercisable_to",
        "status",
        "exercised_on",
        "exercise_price_usd",
        "source_note",
    )

    def record_tranche(self, ref: str, tranche: str, as_of: date, now: int, **fields) -> None:
        """Record one version of a contract's option tranche. Same rules as
        record_contract: a same-day version replaces, any other is history."""
        unknown = set(fields) - set(self.TRANCHE_FIELDS)
        if unknown:
            raise ValueError(f"not a tranche field: {sorted(unknown)}")
        for required in ("asset", "amount", "status", "source_note"):
            if not fields.get(required):
                raise ValueError(f"a tranche needs {required}")
        if fields["status"] not in ("open", "exercised", "lapsed"):
            raise ValueError("tranche status is open | exercised | lapsed")
        if not self.contract_history(ref):
            raise ValueError(f"no contract {ref!r} — key the contract first")

        def dec(k: str) -> str | None:
            v = fields.get(k)
            return str(v) if v is not None else None

        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO contract_tranches
                   (ref, tranche, as_of, asset, amount, strike_usd, strike_rule,
                    strike_low_usd, strike_high_usd, exercisable_from, exercisable_to,
                    status, exercised_on, exercise_price_usd, source_note, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ref,
                    str(tranche),
                    as_of.isoformat(),
                    fields["asset"],
                    str(fields["amount"]),
                    dec("strike_usd"),
                    fields.get("strike_rule"),
                    dec("strike_low_usd"),
                    dec("strike_high_usd"),
                    fields.get("exercisable_from"),
                    fields.get("exercisable_to"),
                    fields["status"],
                    fields.get("exercised_on"),
                    dec("exercise_price_usd"),
                    fields["source_note"],
                    now,
                ),
            )

    def tranches_as_of(self, as_of: date | None = None) -> list[dict]:
        """The latest version of every tranche known on `as_of` (default: all
        versions count), ordered by contract and tranche number."""
        sql = """
            SELECT t.* FROM contract_tranches t
            INNER JOIN (
                SELECT ref, tranche, MAX(as_of) AS a FROM contract_tranches
                WHERE (:d IS NULL OR as_of <= :d) GROUP BY ref, tranche
            ) l ON t.ref = l.ref AND t.tranche = l.tranche AND t.as_of = l.a
            ORDER BY t.ref, CAST(t.tranche AS INTEGER), t.tranche
        """
        params = {"d": as_of.isoformat() if as_of else None}
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            r["amount"] = Decimal(r["amount"])
            for k in ("strike_usd", "strike_low_usd", "strike_high_usd", "exercise_price_usd"):
                r[k] = Decimal(r[k]) if r[k] is not None else None
        return rows

    # ── Price cache ───────────────────────────────────────────────────────

    def cache_price(
        self, chain: Chain, address: str, d: date, price_usd: Decimal, source: str
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO price_cache
                   (chain, address, date, price_usd, source, cached_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    chain.value,
                    address.lower(),
                    d.isoformat(),
                    float(price_usd),
                    source,
                    int(datetime.now(UTC).timestamp()),
                ),
            )

    def cached_price(self, chain: Chain, address: str, d: date) -> Decimal | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT price_usd FROM price_cache
                   WHERE chain=? AND address=? AND date=?""",
                (chain.value, address.lower(), d.isoformat()),
            ).fetchone()
            return Decimal(str(row["price_usd"])) if row else None
