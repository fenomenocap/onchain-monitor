"""MachineX scans events for tokenIds the wallet held ONCE, not just now.

The hole: the topic filter is built from `rpc_enumerate_positions_v3`, which
reports current ownership. Burn or transfer a position NFT and its final
Decrease/Collect are never requested, while the block cursor walks past them
permanently.

The load-bearing constraint these tests exist to protect: `_scan_window` gates
every returned log on `raw_tid in tid_meta`. Widening the topic filter WITHOUT a
matching tid_meta entry is strictly worse than doing nothing — the logs get
fetched, paid for, and silently discarded while the cursor advances. Filter and
metadata must always widen together.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from defi_tracker.adapters.machinex import MachineXAdapter
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, Token

WALLET = "0xabc123def456abc123def456abc123def456abc1"
POOL = "0x" + "cc" * 20
T0 = "0x" + "aa" * 20
T1 = "0x" + "bb" * 20
LIVE_TID = 1
GONE_TID = 466


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "hist.db")
    s.init_schema()
    return s


@pytest.fixture
def adapter(storage: Storage, tmp_path: Path) -> MachineXAdapter:
    a = MachineXAdapter(
        graph_api_key="k", rpc_url="http://fake-rpc", db_path=str(tmp_path / "hist.db")
    )
    a._storage = storage
    return a


def _seed_position(
    storage: Storage,
    tid: int,
    *,
    key: str | None = None,
    is_closed: int = 0,
    resolve_tokens: bool = True,
    date: str = "2026-08-01",
) -> str:
    """A live snapshot row for a position, plus the token registry rows that
    make its decimals trustworthy."""
    position_key = key or f"{POOL}:-100:100:{tid}"
    if resolve_tokens:
        storage.upsert_token(Token(Chain.PEAQ, T0, "AAA", 18))
        storage.upsert_token(Token(Chain.PEAQ, T1, "BBB", 6))
    with storage.connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO snapshots (
                 snapshot_date, wallet, chain, protocol_id, position_key,
                 protocol_kind, pair_label, snapshot_ts, current_value_usd,
                 unclaimed_usd, is_closed, current_balances_json, meta_json
               ) VALUES (?, ?, 'peaq', 'machinex', ?, 'cl_amm', 'AAA/BBB', 1, 100, 0, ?, ?, ?)""",
            (
                date,
                WALLET.lower(),
                position_key,
                is_closed,
                json.dumps(
                    [
                        {"token_key": f"peaq:{T0}", "amount": "1"},
                        {"token_key": f"peaq:{T1}", "amount": "1"},
                    ]
                ),
                json.dumps({"token_id": tid, "source": "subgraph"}),
            ),
        )
    return position_key


# ── Storage: the tombstone trap ───────────────────────────────────────────


class TestAllPositionIdentities:
    def test_returns_the_last_LIVE_row_not_the_tombstone(self, storage: Storage):
        """The error this test exists for: write_tombstone_snapshot stores
        meta_json NULL and current_balances_json '[]', so "latest row per
        position" hands back exactly the row with nothing to rebuild from."""
        _seed_position(storage, GONE_TID, date="2026-08-01")
        _seed_position(storage, GONE_TID, date="2026-08-02", is_closed=1)

        (row,) = storage.all_position_identities(WALLET, Chain.PEAQ, "machinex")
        assert row["snapshot_date"] == "2026-08-01"
        assert row["meta_json"] is not None
        assert json.loads(row["current_balances_json"]) != []

    def test_newest_first(self, storage: Storage):
        _seed_position(storage, 1, date="2026-07-01")
        _seed_position(storage, 2, date="2026-08-01")
        rows = storage.all_position_identities(WALLET, Chain.PEAQ, "machinex")
        assert [r["snapshot_date"] for r in rows] == ["2026-08-01", "2026-07-01"]


# ── Reconstruction ────────────────────────────────────────────────────────


class TestHistoricalTidMeta:
    def test_recovers_a_position_no_longer_held(self, adapter, storage):
        _seed_position(storage, GONE_TID)
        meta = adapter._historical_tid_meta(WALLET, Chain.PEAQ, exclude=set())
        assert GONE_TID in meta
        key, token0, token1 = meta[GONE_TID]
        # The STORED key, verbatim — an event must join to the same position
        # the snapshots already use.
        assert key == f"{POOL}:-100:100:{GONE_TID}"
        assert (token0.symbol, token0.decimals) == ("AAA", 18)
        assert (token1.symbol, token1.decimals) == ("BBB", 6)

    def test_currently_held_positions_are_excluded(self, adapter, storage):
        _seed_position(storage, LIVE_TID)
        assert adapter._historical_tid_meta(WALLET, Chain.PEAQ, exclude={LIVE_TID}) == {}

    def test_legacy_xyk_keys_carry_no_tokenid(self, adapter, storage):
        _seed_position(storage, 0, key=f"legacy:{POOL}:0xdeadbeef")
        assert adapter._historical_tid_meta(WALLET, Chain.PEAQ, exclude=set()) == {}

    def test_untrustworthy_decimals_skip_the_position(self, adapter, storage):
        """A stub token row means decimals are a guess. Scaling a raw log amount
        by a guessed decimals writes a silently wrong number into the ledger —
        worse than the missing event we are trying to recover."""
        _seed_position(storage, GONE_TID, resolve_tokens=False)
        storage.upsert_token(Token(Chain.PEAQ, T0, "0xaaaa…aaaa", 18))
        storage.upsert_token(Token(Chain.PEAQ, T1, "0xbbbb…bbbb", 18))
        with patch.object(adapter._rpc, "erc20_metadata", return_value=(None, None)):
            assert adapter._historical_tid_meta(WALLET, Chain.PEAQ, exclude=set()) == {}

    def test_key_and_metadata_disagreement_is_skipped(self, adapter, storage):
        """One of the two is wrong, and guessing mis-attributes the event."""
        key = _seed_position(storage, GONE_TID)
        with storage.connect() as conn:
            conn.execute(
                "UPDATE snapshots SET meta_json = ? WHERE position_key = ?",
                (json.dumps({"token_id": 999}), key),
            )
        assert adapter._historical_tid_meta(WALLET, Chain.PEAQ, exclude=set()) == {}

    def test_capped_newest_first(self, adapter, storage):
        for i in range(60):
            _seed_position(storage, 1000 + i, date=f"2026-06-{(i % 28) + 1:02d}")
        meta = adapter._historical_tid_meta(WALLET, Chain.PEAQ, exclude=set())
        assert len(meta) == adapter._MAX_HISTORICAL_TOKEN_IDS


# ── End to end through the scan ───────────────────────────────────────────


class TestScanFilter:
    def _drain(self, adapter, captured):
        def fake_chunked(address, topics, from_block, to_block, **kwargs):
            captured.append(topics)
            return []

        with (
            patch("time.sleep"),
            patch(
                "defi_tracker.adapters.machinex.rpc_enumerate_positions_v3",
                return_value=[LIVE_TID],
            ),
            patch(
                "defi_tracker.adapters.machinex.rpc_get_position_v3",
                side_effect=lambda rpc, mgr, tid, **kw: (
                    (T0, T1, 100, -100, 100, 5, 0, 0) if tid == LIVE_TID else None
                ),
            ),
            patch("defi_tracker.adapters.machinex.rpc_get_pool_address_v3", return_value=POOL),
            patch.object(adapter._rpc, "get_block_number", return_value=5_400_000),
            patch.object(adapter._rpc, "get_logs_chunked", side_effect=fake_chunked),
        ):
            for _ in adapter.iter_event_batches(WALLET, Chain.PEAQ):
                pass

    def test_burned_tokenid_is_in_the_filter_AND_the_metadata(self, adapter, storage):
        _seed_position(storage, GONE_TID)
        captured: list = []
        self._drain(adapter, captured)

        assert captured, "expected at least one getLogs call"
        tid_topics = captured[0][1]
        assert "0x" + LIVE_TID.to_bytes(32, "big").hex() in tid_topics
        assert "0x" + GONE_TID.to_bytes(32, "big").hex() in tid_topics

    def test_a_skipped_position_is_absent_from_the_filter(self, adapter, storage):
        """The anti-regression for the load-bearing constraint: a tokenId we
        cannot describe must not reach the topic list, or its logs would be
        fetched and silently discarded while the cursor moved past them."""
        _seed_position(storage, GONE_TID, resolve_tokens=False)
        storage.upsert_token(Token(Chain.PEAQ, T0, "0xaaaa…aaaa", 18))
        storage.upsert_token(Token(Chain.PEAQ, T1, "0xbbbb…bbbb", 18))
        captured: list = []
        with patch.object(adapter._rpc, "erc20_metadata", return_value=(None, None)):
            self._drain(adapter, captured)
        assert "0x" + GONE_TID.to_bytes(32, "big").hex() not in captured[0][1]


class TestCursorInteraction:
    def test_history_makes_an_empty_wallet_scan_instead_of_skipping(self, adapter, storage):
        """A wallet holding nothing now but with machinex history has events
        worth looking for — skipping 150k blocks would step over them."""
        _seed_position(storage, GONE_TID)
        called: list = []

        with (
            patch("time.sleep"),
            patch("defi_tracker.adapters.machinex.rpc_enumerate_positions_v3", return_value=[]),
            patch.object(adapter._rpc, "get_block_number", return_value=5_400_000),
            patch.object(
                adapter._rpc,
                "get_logs_chunked",
                side_effect=lambda *a, **kw: called.append(kw or a) or [],
            ),
        ):
            for _ in adapter.iter_event_batches(WALLET, Chain.PEAQ):
                pass

        assert called, "a wallet with history must be scanned, not skipped"

    def test_no_history_and_nothing_held_still_skips(self, adapter, storage):
        """The unchanged half: genuinely empty means walk the cursor forward."""
        called: list = []
        with (
            patch("time.sleep"),
            patch("defi_tracker.adapters.machinex.rpc_enumerate_positions_v3", return_value=[]),
            patch.object(adapter._rpc, "get_block_number", return_value=5_400_000),
            patch.object(
                adapter._rpc,
                "get_logs_chunked",
                side_effect=lambda *a, **kw: called.append(1) or [],
            ),
        ):
            for _ in adapter.iter_event_batches(WALLET, Chain.PEAQ):
                pass

        assert not called
        assert storage.kv_get(f"block_cursor:{WALLET.lower()}:peaq") is not None
