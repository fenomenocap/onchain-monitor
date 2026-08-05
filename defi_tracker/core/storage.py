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
  - Schema versioning via PRAGMA user_version so we can migrate cleanly.

To use:
    from defi_tracker.core.storage import Storage
    s = Storage("tracker.db")
    s.init_schema()                                 # idempotent
    s.upsert_events(events)
    s.write_snapshot(positions, snapshot_date=...)
    rows = s.query_mtd(wallet, chain, year=2026, month=5)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from defi_tracker.core.types import (
    Alert,
    Chain,
    Event,
    Position,
    Token,
)

SCHEMA_VERSION = 2


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
            conn.execute(
                "ALTER TABLE snapshots ADD COLUMN is_closed INTEGER NOT NULL DEFAULT 0"
            )
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

    # ── Key-value store ───────────────────────────────────────────────────

    def kv_get(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key = ?", (key,)
            ).fetchone()
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
    ) -> list[dict]:
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    """SELECT * FROM events
                   WHERE wallet=? AND chain=? AND protocol_id=? AND position_key=?
                   ORDER BY ts ASC, log_index ASC""",
                    (wallet.lower(), chain.value, protocol_id, position_key),
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
                    current_value_usd, unclaimed_usd,
                    cost_basis_usd, hold_value_usd, il_usd, il_pct, pnl_usd,
                    is_closed,
                    in_range, tick_lower, tick_upper, tick_current,
                    pool_tvl_usd, seven_day_fees_usd, position_share_pct, fee_tier_bps,
                    current_balances_json, unclaimed_balances_json, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    float(position.unclaimed_usd),
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
                    float(position.pool_tvl_usd) if position.pool_tvl_usd else None,
                    float(position.seven_day_fees_usd) if position.seven_day_fees_usd else None,
                    float(position.position_share_pct) if position.position_share_pct else None,
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

    def open_position_identities(
        self, wallet: str, chain: Chain, protocol_id: str
    ) -> list[dict]:
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
        MTD PnL per position: current snapshot vs last snapshot of previous month.
        Returns one row per active position with delta_value, fees_collected_mtd, etc.

        If year/month omitted, uses current UTC month.
        """
        today = datetime.now(UTC).date()
        y = year or today.year
        m = month or today.month
        # First day of month, last day of previous month
        month_start = date(y, m, 1).isoformat()
        prev_month_end = (date(y, m, 1).fromordinal(date(y, m, 1).toordinal() - 1)).isoformat()

        sql = """
        WITH curr AS (
            SELECT s.* FROM snapshots s
            INNER JOIN (
                SELECT wallet, chain, protocol_id, position_key,
                       MAX(snapshot_date) AS d
                FROM snapshots
                WHERE snapshot_date >= :month_start
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
              AND ts >= strftime('%s', :month_start)
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

        for r in rows:
            r["fees_collected_mtd"] = fees.get(
                (r["wallet"], r["chain"], r["protocol_id"], r["position_key"]),
                0.0,
            )
            r["pnl_mtd_usd"] = (
                r["delta_value_usd"]
                + (r["unclaimed_now"] - r["unclaimed_month_start"])
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

    def day_over_day_value(self) -> tuple[float, float] | None:
        """(current, previous) total value over positions that have BOTH a
        latest and a prior snapshot — matched per position, so one chain's
        snapshot pass lagging a day doesn't fake a portfolio-wide swing.
        None when no position has two snapshots yet."""
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
                       MAX(CASE WHEN rn = 2 THEN current_value_usd END) AS prev
                FROM ranked WHERE rn <= 2
                GROUP BY wallet, chain, protocol_id, position_key
            )
            SELECT SUM(curr) AS curr, SUM(prev) AS prev
            FROM matched
            WHERE prev IS NOT NULL
              -- only positions fresh as of the latest run: a chain whose
              -- snapshot pass has been failing must not contribute its
              -- week-old move as if it happened today
              AND curr_date = (SELECT MAX(snapshot_date) FROM snapshots WHERE is_closed = 0)
        """
        with self.connect() as conn:
            row = conn.execute(sql).fetchone()
        if row is None or row["curr"] is None:
            return None
        return float(row["curr"]), float(row["prev"])

    def last_in_range_dates(self) -> dict[str, str]:
        """{position_key: latest snapshot_date where the position was in range}
        — lets reports say how long a position has been sitting out of range."""
        # Derive in-range from ticks for legacy rows whose in_range was never
        # set (null) but whose ticks are known — otherwise a position that has
        # only ever had null in_range shows no last-in-range date at all.
        sql = """
            SELECT position_key, MAX(snapshot_date) AS d
            FROM snapshots
            WHERE in_range = 1
               OR (in_range IS NULL
                   AND tick_lower IS NOT NULL AND tick_upper IS NOT NULL
                   AND tick_current IS NOT NULL
                   AND tick_current BETWEEN tick_lower AND tick_upper)
            GROUP BY wallet, chain, protocol_id, position_key
        """
        with self.connect() as conn:
            return {r["position_key"]: r["d"] for r in conn.execute(sql).fetchall()}

    def position_flows(self) -> list[dict]:
        """Per-position lifetime flows from events, tagged open/closed.

        net_invested_usd = Σ signed deposit/withdraw usd (deposits +, withdrawals −,
        each at event-time prices); fees_usd = Σ collects. A position is 'open'
        when its latest snapshot row is live (not a tombstone); positions with
        events but no live snapshot are 'closed' — their realized result
        (fees + withdrawals − deposits) is locked in and belongs in book PnL."""
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
                   EXISTS (
                       SELECT 1 FROM open_keys o
                       WHERE o.wallet=e.wallet AND o.chain=e.chain
                         AND o.protocol_id=e.protocol_id AND o.position_key=e.position_key
                   ) AS is_open
            FROM events e
            GROUP BY e.wallet, e.chain, e.protocol_id, e.position_key
        """
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def position_window_stats(self, days: int = 30) -> list[dict]:
        """Per open position, snapshot-derived stats over the trailing window:
        average value, unclaimed at window start/end, and days of coverage.
        Feeds the realized-yield calculation (accrual-clean, harvest-lump-proof)."""
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
                   l.d_min AS window_start
            FROM windowed w
            JOIN latest l ON w.wallet=l.wallet AND w.chain=l.chain
                 AND w.protocol_id=l.protocol_id AND w.position_key=l.position_key
            GROUP BY w.wallet, w.chain, w.protocol_id, w.position_key
        """
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(sql, {"cutoff": f"-{days} days"}).fetchall()
            ]

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
                for r in conn.execute(
                    sql, {"w": wallet.lower() if wallet else None}
                ).fetchall()
            ]

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

    def list_alerts(
        self, limit: int = 50, undelivered_only: bool = False
    ) -> list[dict]:
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
        """Sync the queue to the current actionable signals (tiers now/soon).

        `actionable` is a list of rebalance_signals dicts, each with a
        ``uid`` key. Each run: upsert a live 'open' episode per still-flagged
        position (a lapsed snooze flips back to open; a re-flagged resolved
        position opens a fresh episode), refresh tier/context, and auto-resolve
        any live item no longer flagged — so a rebalance you completed drops
        off on the next run. Snoozed items stay snoozed until their timer
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
                ctx = json.dumps({
                    k: sig.get(k)
                    for k in ("idle_usd", "depth", "days_out", "forgone_usd",
                              "current_half_pct", "suggest")
                })
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
