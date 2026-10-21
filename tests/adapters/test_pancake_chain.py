"""
Pancake Infinity events read from the chain (the subgraph died 2026-07-06).

Fixtures are two BSC receipts shaped like the chain returns them, with the
wallet, hashes, blocks and tokenIds made synthetic: a fee sweep (0x55555555…)
and an increase (0x66666666…). The decoder must reproduce
what the subgraph recorded for them to the unit, and share its event_uids, so
a chain backfill and the subgraph history deduplicate instead of doubling.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from defi_tracker.adapters._pancake_chain import PM_MODIFY_TOPIC, decode_receipt
from defi_tracker.adapters.pancake_infinity import (
    CL_POOL_MANAGER,
    CL_POS_MANAGER,
    PancakeInfinityAdapter,
)
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, EventKind, PricePoint, Token

W = "0x1111111111111111111111111111111111111111"
USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
PEAQ = "0x8b9ee39195ea99d6ddd68030f44131116bc218f6"
SWEEP = "0x5555555555555555555555555555555555555555555555555555555555555555"
INCREASE = "0x6666666666666666666666666666666666666666666666666666666666666666"
RECEIPTS = json.loads((Path(__file__).parent / "fixtures" / "pancake_receipts.json").read_text())


def _decode(tx: str, wallet: str = W):
    return decode_receipt(RECEIPTS[tx], wallet, CL_POS_MANAGER, CL_POOL_MANAGER)


# ── decoding ──────────────────────────────────────────────────────────────


def test_a_sweep_decodes_to_the_exact_fees_the_subgraph_recorded():
    (c,) = _decode(SWEEP)
    assert (c.log_index, c.token_id, c.tick_lower, c.tick_upper) == (147, 700001, 37200, 48200)
    assert c.liquidity_delta == 0 and c.principal == (0, 0)
    # The fixture's subgraph-era record: 40.123456789012345678 USDC, 2000.987654321098765432 PEAQ
    assert Decimal(c.fees[0]) / 10**18 == Decimal("40.123456789012345678")
    assert Decimal(c.fees[1]) / 10**18 == Decimal("2000.987654321098765432")
    assert c.tokens == (USDC, PEAQ)


def test_an_increase_recovers_principal_and_the_fees_it_netted():
    (c,) = _decode(INCREASE)
    assert c.log_index == 59 and c.liquidity_delta > 0
    # principal = paid by the wallet + fees accrued; the subgraph said 15000 USDC
    assert Decimal(c.principal[0]) / 10**18 == Decimal("15000")
    assert Decimal(c.principal[1]) / 10**18 == Decimal("1900000.123456789012345678")
    assert c.fees[0] > 0  # netted fees the subgraph never recorded


def test_someone_elses_transaction_is_not_ours():
    assert _decode(SWEEP, wallet="0x" + "9" * 40) == []


def test_two_liquidity_changes_in_one_tx_cannot_be_attributed():
    rc = copy.deepcopy(RECEIPTS[INCREASE])
    pm = next(lg for lg in rc["logs"] if lg["topics"][0] == PM_MODIFY_TOPIC)
    pool = next(lg for lg in rc["logs"] if lg["address"].lower() == CL_POOL_MANAGER.lower())
    twin_pm, twin_pool = copy.deepcopy(pm), copy.deepcopy(pool)
    twin_pm["topics"][1] = "0x" + (999).to_bytes(32, "big").hex()
    twin_pool["data"] = twin_pool["data"][:-64] + (999).to_bytes(32, "big").hex()
    twin_pool["logIndex"] = hex(900)
    rc["logs"] += [twin_pool, twin_pm]
    changes = decode_receipt(rc, W, CL_POS_MANAGER, CL_POOL_MANAGER)
    assert len(changes) == 2
    assert all(c.principal is None for c in changes)
    assert "2 positions changed liquidity in one tx" in changes[0].notes


# ── the adapter's scan ────────────────────────────────────────────────────


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch) -> PancakeInfinityAdapter:
    db = tmp_path / "t.db"
    st = Storage(db)
    st.init_schema()
    st.upsert_token(Token(Chain.BSC, USDC, "USDC", 18))
    st.upsert_token(Token(Chain.BSC, PEAQ, "PEAQ", 18))
    ad = PancakeInfinityAdapter(graph_api_key="k", rpc_url="https://bsc.example", db_path=str(db))
    rpc = MagicMock()
    rpc.url = "https://bsc.example"
    rpc.get_block_number.return_value = 107_430_000
    rpc.get_transaction_receipt.side_effect = lambda tx: RECEIPTS.get(tx)
    rpc.get_block_timestamp.return_value = 1_782_900_000
    ad._rpc = rpc  # noqa: SLF001
    monkeypatch.setattr(
        ad,
        "get_historical_price",
        lambda tok, ts: PricePoint(
            tok.key, Decimal("1") if tok.address == USDC else Decimal("0.02"), ts, "t"
        ),
    )
    return ad


def _pm_logs(*txs: str) -> list[dict]:
    return [{"transactionHash": tx} for tx in txs]


def test_the_scan_emits_events_that_share_the_subgraphs_uids(adapter):
    adapter._rpc.get_logs_chunked.side_effect = lambda addr, topics, *a, **k: (  # noqa: SLF001
        [] if topics[0] != PM_MODIFY_TOPIC else _pm_logs(SWEEP, INCREASE)
    )
    adapter._storage.kv_set(f"pancake_block_cursor:{W}", "107400000")  # noqa: SLF001
    adapter._storage.upsert_events([])  # noqa: SLF001
    # Known NFT, as the subgraph-era snapshots record it
    with adapter._storage.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO snapshots (snapshot_date, wallet, chain, protocol_id, position_key,"
            " protocol_kind, pair_label, snapshot_ts, current_value_usd, current_balances_json,"
            " meta_json) VALUES ('2026-07-05', ?, 'bsc', 'pancake_infinity', 'k', 'cl_amm',"
            " 'USDC/PEAQ', 1, 1, '[]', '{\"token_id\": 700001}')",
            (W,),
        )
    batches = list(adapter.iter_event_batches(W, Chain.BSC))
    events = [e for b in batches for e in b]
    uids = {e.event_uid: e for e in events}
    assert uids[f"{SWEEP}:147"].kind == EventKind.COLLECT
    assert uids[f"{INCREASE}:59"].kind == EventKind.DEPOSIT
    netted = uids[f"{INCREASE}:500059"]
    assert netted.kind == EventKind.COLLECT and netted.meta["netted_against_increase"]
    assert adapter.last_scan_reached_head(W, Chain.BSC)
    assert adapter._storage.kv_get(f"pancake_block_cursor:{W}") == str(107_430_000 + 1)  # noqa: SLF001


def test_the_first_scan_starts_at_the_subgraphs_last_event(adapter, make_event):
    adapter._storage.upsert_events(  # noqa: SLF001
        [make_event(tx_hash=SWEEP, wallet=W, protocol_id="pancake_infinity", chain=Chain.BSC)]
    )
    adapter._rpc.get_logs_chunked.return_value = []  # noqa: SLF001
    list(adapter.iter_event_batches(W, Chain.BSC))
    first_call = adapter._rpc.get_logs_chunked.call_args_list[0]  # noqa: SLF001
    assert first_call.args[2] == 107_410_000  # the sweep's own block


def test_a_wallet_with_no_pancake_history_starts_at_head(adapter):
    adapter._rpc.get_logs_chunked.return_value = []  # noqa: SLF001
    list(adapter.iter_event_batches(W, Chain.BSC))
    assert adapter._storage.kv_get(f"pancake_block_cursor:{W}") == str(107_430_001)  # noqa: SLF001


def test_a_missing_receipt_stops_the_window_instead_of_skipping_it(adapter):
    adapter._storage.kv_set(f"pancake_block_cursor:{W}", "107400000")  # noqa: SLF001
    adapter._rpc.get_logs_chunked.side_effect = lambda addr, topics, *a, **k: (  # noqa: SLF001
        [] if topics[0] != PM_MODIFY_TOPIC else _pm_logs("0xmissing")
    )
    with adapter._storage.connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO snapshots (snapshot_date, wallet, chain, protocol_id, position_key,"
            " protocol_kind, pair_label, snapshot_ts, current_value_usd, current_balances_json,"
            " meta_json) VALUES ('2026-07-05', ?, 'bsc', 'pancake_infinity', 'k', 'cl_amm',"
            " 'USDC/PEAQ', 1, 1, '[]', '{\"token_id\": 1}')",
            (W,),
        )
    with pytest.raises(RuntimeError, match="no receipt"):
        list(adapter.iter_event_batches(W, Chain.BSC))
    assert adapter._storage.kv_get(f"pancake_block_cursor:{W}") == "107400000"  # noqa: SLF001
    assert not adapter.last_scan_reached_head(W, Chain.BSC)


def test_alchemy_asset_transfers_discover_the_txs_in_one_pass(adapter):
    adapter._rpc.url = "https://bnb-mainnet.g.alchemy.com/v2/k"  # noqa: SLF001
    adapter._storage.kv_set(f"pancake_block_cursor:{W}", "107400000")  # noqa: SLF001
    adapter._rpc.call.return_value = {"transfers": [{"hash": SWEEP}]}  # noqa: SLF001
    events = [e for b in adapter.iter_event_batches(W, Chain.BSC) for e in b]
    assert [e.event_uid for e in events] == [f"{SWEEP}:147"]
    adapter._rpc.get_logs_chunked.assert_not_called()  # noqa: SLF001


def test_the_old_subgraph_path_runs_without_an_rpc(adapter, monkeypatch):
    adapter._rpc = None  # noqa: SLF001
    monkeypatch.setattr(adapter, "fetch_events", lambda *a, **k: [])
    assert list(adapter.iter_event_batches(W, Chain.BSC)) == [[]]


def test_the_public_node_is_refused_with_what_is_needed(adapter):
    adapter._rpc.url = "https://bsc-dataseed.bnbchain.org"  # noqa: SLF001
    with pytest.raises(RuntimeError, match="ALCHEMY_API_KEY"):
        list(adapter.iter_event_batches(W, Chain.BSC))


def test_a_ten_block_range_cap_is_refused_rather_than_crawled():
    from unittest.mock import patch

    from defi_tracker.adapters._rpc import RpcClient

    def post(url, json, **_):
        f = json["params"][0]
        wide = int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1 > 10
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.json.return_value = {"error": {"message": "range"}} if wide else {"result": []}
        return r

    with (
        patch("defi_tracker.adapters._rpc.requests.post", side_effect=post),
        pytest.raises(RuntimeError, match="range cap is too small"),
    ):
        RpcClient("https://x", max_retries=1).get_logs_chunked("0xa", [], 0, 10_000, min_chunk=500)


def test_a_position_opened_after_the_subgraph_died_is_seeded_from_its_events(adapter):
    adapter._storage.kv_set(f"pancake_block_cursor:{W}", "107400000")  # noqa: SLF001
    adapter._rpc.get_logs_chunked.side_effect = lambda addr, topics, *a, **k: (  # noqa: SLF001
        [] if topics[0] != PM_MODIFY_TOPIC else _pm_logs(INCREASE)
    )
    adapter._known_token_ids = lambda wallet: {700002}  # noqa: SLF001
    for batch in adapter.iter_event_batches(W, Chain.BSC):
        adapter._storage.upsert_events(batch)  # noqa: SLF001
    rows = adapter._event_only_rows(W, Chain.BSC, seeded=set())  # noqa: SLF001
    (row,) = rows.values()
    assert json.loads(row["meta_json"])["token_id"] == 700002
    assert row["pair_label"] == "USDC/PEAQ"
    # Already snapshotted positions are not duplicated.
    assert adapter._event_only_rows(W, Chain.BSC, seeded=set(rows)) == {}  # noqa: SLF001
