"""
Pool history from the fee counter (bsc, where Swap logs are out of reach).

A day is two archive reads of the pool apart: fees = Δ feeGrowthGlobal ×
the mean of the two ends' depth. What must hold is the same accounting as
the swap scan — each day banked once, with its cursor, a failed read
leaving every earlier day and nothing after it — plus the fee-counter's own
honesty: a row says it was sampled (source, samples 2), never a swap count
it did not see; a day before the pool existed is no row; and each day's
block search starts from yesterday's instead of the whole chain.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

import pytest

from defi_tracker.analytics import elasticity_panel, pool_day_prices, pool_history
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import AdapterInfo, Chain, PoolInfo, PoolState, ProtocolKind
from defi_tracker.poolflow import (
    fee_counter_day,
    history_source,
    sync_fee_counter,
    sync_pool_history,
)

POOL = "0x" + "ab" * 32
INFO = PoolInfo("bsc", POOL, "0xusdc", "0xpeaq", "USDC", "PEAQ", 18, 18, 3000)
Q128 = 1 << 128
BLOCKS_PER_DAY = 1000
DAY0 = date(2026, 3, 1)


def _ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def _block_for(ts: int) -> int:
    """This fake chain's last block at or before ts."""
    return (ts - _ts(DAY0)) * BLOCKS_PER_DAY // 86_400 + 10_000


# ── the day ───────────────────────────────────────────────────────────────


def test_a_days_fees_are_fee_growth_times_the_mean_depth_in_token1_at_the_close():
    prev = PoolState(1, 0, 0, 1000, 1, 0)
    # tick 0, equal decimals: 1 USDC = 1 PEAQ. 2 USDC + 3 PEAQ per unit of
    # depth, over a mean depth of 1500.
    cur = PoolState(2, 2 * Q128 // 10**18, 3 * Q128 // 10**18, 2000, 1, 0)
    row = fee_counter_day("2026-03-02", prev, cur, INFO, book_open=10, book_close=30)
    assert row["fees_token1"] == pytest.approx(1500 * 5 / 10**18 * 10**0, rel=1e-9)
    assert row["source"] == "fee_counter" and row["samples"] == 2 and row["swaps"] is None
    assert row["l_sum"] == 3000 and row["book_l_sum"] == 40 and row["book_l_open"] == 10


def test_fee_growth_wraps_like_the_contracts_own_arithmetic():
    prev = PoolState(1, (1 << 256) - Q128, 0, 1, 1, 0)
    cur = PoolState(2, Q128, 0, 1, 1, 0)  # grew by 2·Q128 across the wrap
    assert fee_counter_day("d", prev, cur, INFO, 0, 0)["fees_token1"] == pytest.approx(2 / 10**18)


def test_token0_fees_are_priced_at_the_close():
    prev = PoolState(1, 0, 0, 10**18, 1, 0)
    cur = PoolState(2, Q128, 0, 10**18, 1, 6932)  # ≈ 2 PEAQ per USDC at the close
    assert fee_counter_day("d", prev, cur, INFO, 0, 0)["fees_token1"] == pytest.approx(2, rel=1e-3)


# ── the sync ──────────────────────────────────────────────────────────────


class _Fake:
    """An archive-reading adapter: growth accrues 1 unit per block, depth is
    1000 until the book deposits, the pool is uninitialised before DAY0."""

    info = AdapterInfo("pancakex", "Fake", [Chain.BSC], ProtocolKind.CL_AMM, True, True)

    def __init__(self, fail_at_block=None):
        self.fail_at_block = fail_at_block
        self.nears: list[int | None] = []
        self.reads: list[int] = []

    def position_pool(self, key):
        pool, lo, hi = key.split(":")
        return pool, int(lo), int(hi)

    def pool_info(self, chain, pool):
        return INFO

    def swap_history_start(self, chain):
        raise NotImplementedError

    def reads_fee_counter(self, chain):
        return True

    def block_at(self, chain, ts, near=None):
        self.nears.append(near)
        return _block_for(ts)

    def pool_state_at(self, chain, pool, block):
        if self.fail_at_block is not None and block >= self.fail_at_block:
            raise RuntimeError("archive read refused")
        self.reads.append(block)
        if block < 10_000:
            return PoolState(block, 0, 0, 0, 0, 0)  # not yet initialised
        return PoolState(block, block * Q128, 0, 1000, 1 << 96, 0)


def _deposit(storage, day: date, block: int, delta=500, kind="deposit"):
    with storage.connect() as conn:
        conn.execute(
            """INSERT INTO events (event_uid, wallet, chain, protocol_id, position_key, tx_hash,
                   log_index, ts, block_number, kind, usd_at_ts, amounts_json, meta_json)
               VALUES (?, '0xw', 'bsc', 'pancakex', ?, '0xt', 0, ?, ?, ?, 1, '[]', ?)""",
            (f"0xt:{block}", f"{POOL}:-100:100", _ts(day) + 3600, block, kind,
             json.dumps({"liquidity_delta": delta})),
        )  # fmt: skip


@pytest.mark.parametrize("stored", [500, -500])
def test_a_withdraw_lowers_the_books_depth_whatever_sign_the_adapter_stored(storage, stored):
    """MachineX stores a withdraw's liquidity change unsigned, Pancake signed;
    either way a full withdraw must leave the book at zero, not double it."""
    from defi_tracker.poolflow import book_changes

    _deposit(storage, date(2026, 3, 5), 100)
    _deposit(storage, date(2026, 3, 6), 200, delta=stored, kind="withdraw")
    (changes,) = book_changes(storage, _Fake(), Chain.BSC).values()
    assert sum(c["delta"] for c in changes) == 0


def test_a_deposit_stored_without_a_block_is_placed_by_its_timestamp(storage):
    """Subgraph-era Pancake deposits have block 0; left there, the book would
    hold them from before the pool's first sample."""
    from defi_tracker.poolflow import book_changes

    day = date(2026, 3, 5)
    _deposit(storage, day, 0)
    fake = _Fake()
    (changes,) = book_changes(storage, fake, Chain.BSC, resolve_blocks=True).values()
    assert changes[0]["block"] == _block_for(_ts(day) + 3600)
    # Cached: the second read searches for nothing.
    fake.nears.clear()
    book_changes(storage, fake, Chain.BSC, resolve_blocks=True)
    assert fake.nears == []


def test_a_bsc_adapter_without_swap_logs_gets_its_history_from_the_fee_counter():
    assert history_source(_Fake(), Chain.BSC) == "fee_counter"


def test_each_closed_day_is_banked_once_and_a_rerun_reads_nothing(storage):
    _deposit(storage, date(2026, 3, 5), _block_for(_ts(date(2026, 3, 5)) + 3600))
    fake = _Fake()
    today = date(2026, 3, 15)
    r1 = sync_fee_counter(storage, fake, Chain.BSC, today=today)
    rows = storage.pool_days("bsc")
    # Sampling starts 30 days before the deposit, but the pool only exists
    # from DAY0: the first day with two initialised ends is 03-02.
    assert [r["day"] for r in rows][0] == "2026-03-02"
    assert rows[-1]["day"] == "2026-03-14"  # today's day has not closed
    assert r1.swaps == len(rows) == 13 and r1.reached_head
    assert all(r["source"] == "fee_counter" and r["samples"] == 2 for r in rows)
    assert all(r["swaps"] is None for r in rows)
    # Each day earned BLOCKS_PER_DAY units of growth over a depth of 1000.
    assert rows[3]["fees_token1"] == pytest.approx(BLOCKS_PER_DAY * 1000 / 10**18)
    n = len(fake.reads)
    r2 = sync_fee_counter(storage, fake, Chain.BSC, today=today)
    assert r2.swaps == 0 and len(fake.reads) == n


def test_the_books_depth_is_counted_from_the_day_its_deposit_closes(storage):
    _deposit(storage, date(2026, 3, 5), _block_for(_ts(date(2026, 3, 5)) + 3600))
    sync_fee_counter(storage, _Fake(), Chain.BSC, today=date(2026, 3, 8))
    by_day = {r["day"]: r for r in storage.pool_days("bsc")}
    assert by_day["2026-03-04"]["book_l_sum"] == 0
    assert by_day["2026-03-05"]["book_l_open"] == 0 and by_day["2026-03-05"]["book_l_sum"] == 500
    assert by_day["2026-03-06"]["book_l_sum"] == 1000  # both ends


def test_each_days_block_search_starts_from_yesterdays_block(storage):
    _deposit(storage, date(2026, 3, 5), _block_for(_ts(date(2026, 3, 5)) + 3600))
    fake = _Fake()
    sync_fee_counter(storage, fake, Chain.BSC, today=date(2026, 3, 10))
    assert fake.nears[0] is None and fake.nears[1] is None  # no block count yet
    # From the third day on: yesterday's block plus yesterday's block count.
    last = _block_for(_ts(date(2026, 3, 8)) + 86_399)
    assert fake.nears[-1] == last + BLOCKS_PER_DAY


def test_a_failed_read_keeps_every_earlier_day_and_nothing_after(storage):
    _deposit(storage, date(2026, 3, 5), _block_for(_ts(date(2026, 3, 5)) + 3600))
    fail = _block_for(_ts(date(2026, 3, 11)))
    with pytest.raises(RuntimeError):
        sync_fee_counter(storage, _Fake(fail_at_block=fail), Chain.BSC, today=date(2026, 3, 20))
    kept = [r["day"] for r in storage.pool_days("bsc")]
    assert kept and kept[-1] < "2026-03-11"
    # The rerun resumes from the cursor and finishes, counting nothing twice.
    sync_fee_counter(storage, _Fake(), Chain.BSC, today=date(2026, 3, 20))
    days = [r["day"] for r in storage.pool_days("bsc")]
    assert days == sorted(set(days)) and days[-1] == "2026-03-19"
    assert len(days) == 18


def test_two_passes_at_once_never_both_bank_a_day(storage):
    _deposit(storage, date(2026, 3, 5), _block_for(_ts(date(2026, 3, 5)) + 3600))
    cursor = storage.kv_get(f"fee_counter:bsc:pancakex:{POOL}")
    row = fee_counter_day(
        "2026-03-02", PoolState(1, 0, 0, 1, 1, 0), PoolState(2, 0, 0, 1, 1, 0), INFO, 0, 0
    )
    kw = {"cursor_kind": "fee_counter", "expected": {POOL: cursor}}
    assert storage.apply_pool_window("bsc", "pancakex", [row], {POOL: '{"a":1}'}, **kw)
    assert not storage.apply_pool_window("bsc", "pancakex", [row], {POOL: '{"a":2}'}, **kw)
    assert len(storage.pool_days("bsc")) == 1


def test_the_dispatcher_sends_a_fee_counter_chain_to_the_fee_counter(storage):
    _deposit(storage, date(2026, 3, 5), _block_for(_ts(date(2026, 3, 5)) + 3600))
    res = sync_pool_history(storage, _Fake(), Chain.BSC)
    assert res.swaps > 0 and storage.pool_days("bsc")[0]["source"] == "fee_counter"


# ── the consumers ─────────────────────────────────────────────────────────


def test_fee_counter_days_feed_the_history_with_their_two_sample_means(storage):
    _deposit(storage, date(2026, 3, 5), _block_for(_ts(date(2026, 3, 5)) + 3600))
    sync_fee_counter(storage, _Fake(), Chain.BSC, today=date(2026, 5, 1))
    rows = storage.pool_days("bsc")
    prices = pool_day_prices(rows, {POOL: INFO})
    days = pool_history(rows, INFO, prices)
    assert len(days) == len(rows)
    assert days[10].l_pool == pytest.approx(1000 / 10**18)  # (1000 + 1000) / 2 samples
    assert days[10].l_book == pytest.approx(500 / 10**18)
    assert elasticity_panel(rows, {POOL: INFO})  # weeks form from sampled days too


# ── the schema ────────────────────────────────────────────────────────────


def test_a_v11_database_keeps_its_swap_days_and_gains_source_and_samples(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE pool_days (chain TEXT NOT NULL, protocol_id TEXT NOT NULL,
               pool TEXT NOT NULL, day TEXT NOT NULL, swaps INTEGER NOT NULL,
               fees_token1 REAL NOT NULL, l_sum REAL NOT NULL, book_l_sum REAL NOT NULL,
               book_l_open REAL NOT NULL, tick_open INTEGER NOT NULL,
               tick_close INTEGER NOT NULL, first_block INTEGER NOT NULL,
               last_block INTEGER NOT NULL, PRIMARY KEY (chain, protocol_id, pool, day))"""
    )
    conn.execute(
        "INSERT INTO pool_days VALUES ('peaq','machinex','0xp','2026-01-05',7,1.5,70,7,1,0,3,10,20)"
    )
    conn.commit()
    conn.close()
    st = Storage(path)
    st.init_schema()
    st.init_schema()  # idempotent
    (row,) = st.pool_days("peaq")
    assert row["swaps"] == 7 and row["samples"] == 7 and row["source"] == "swaps"
    assert row["fees_token1"] == 1.5 and row["last_block"] == 20


# ── the block search ──────────────────────────────────────────────────────


class _Chain:
    """Block timestamps for a chain whose block time fell from 3s to 0.75s,
    several blocks to a second after the change (as on BSC)."""

    def __init__(self):
        self.reads = 0

    def ts(self, b: int) -> int | None:
        if b > 3_000_000:
            return None  # beyond the head
        return 3 * b if b <= 1_000_000 else 3_000_000 + (b - 1_000_000) * 3 // 4


def _rpc(chain: _Chain):
    from defi_tracker.adapters._rpc import RpcClient

    rpc = RpcClient("http://x")

    def ts(b):
        chain.reads += 1
        return chain.ts(b)

    rpc.get_block_timestamp = ts  # type: ignore[method-assign]
    rpc.get_block_number = lambda: 3_000_000  # type: ignore[method-assign]
    return rpc


@pytest.mark.parametrize("target", [3_500_000, 3_600_001, 4_000_123])
def test_a_search_seeded_near_the_answer_agrees_with_the_full_search_in_fewer_reads(target):
    full_chain, near_chain = _Chain(), _Chain()
    want = _rpc(full_chain).block_at(target)
    assert full_chain.ts(want) <= target < full_chain.ts(want + 1)
    for guess in (want - 700, want + 700, want):
        near_chain.reads = 0
        assert _rpc(near_chain).block_at(target, near=guess) == want
        assert near_chain.reads < full_chain.reads / 2


def test_a_seed_past_the_head_falls_back_to_the_full_search():
    c = _Chain()
    assert _rpc(c).block_at(3_600_000, near=5_000_000) == _rpc(_Chain()).block_at(3_600_000)


# ── the Pancake adapter ───────────────────────────────────────────────────


def _pancake(tmp_path, rpc_url="https://bnb-mainnet.example/v2/k"):
    from defi_tracker.adapters.pancake_infinity import PancakeInfinityAdapter

    Storage(tmp_path / "t.db").init_schema()
    return PancakeInfinityAdapter("k", rpc_url=rpc_url, db_path=str(tmp_path / "t.db"))


def test_pancake_reads_its_pools_from_keys_without_a_token_id(tmp_path):
    a = _pancake(tmp_path)
    assert a.position_pool(f"{POOL.upper().replace('0X', '0x')}:-10:20") == (POOL, -10, 20)
    assert a.position_pool("legacy:0xabc") is None


def test_pancake_reads_the_fee_counter_only_from_an_archive_node(tmp_path):
    from defi_tracker.core.endpoints import PUBLIC_RPC

    assert _pancake(tmp_path).reads_fee_counter(Chain.BSC)
    assert not _pancake(tmp_path, PUBLIC_RPC[Chain.BSC]).reads_fee_counter(Chain.BSC)


def test_pancake_pool_state_is_read_at_the_pinned_block(tmp_path, monkeypatch):
    from defi_tracker.adapters import _cl_math
    from defi_tracker.adapters._rpc import RpcClient

    calls = []

    def eth_call(self, to, data, block="latest"):
        calls.append((data[:4], block))
        if data[:4] == _cl_math.SEL_V4_GET_SLOT0:
            return (123).to_bytes(32, "big") + (-5 % (1 << 256)).to_bytes(32, "big") + bytes(64)
        if data[:4] == _cl_math.SEL_V4_FEE_GROWTH_GLOBALS:
            return (7).to_bytes(32, "big") + (9).to_bytes(32, "big")
        return (4242).to_bytes(32, "big")  # getLiquidity

    monkeypatch.setattr(RpcClient, "eth_call", eth_call)
    st = _pancake(tmp_path).pool_state_at(Chain.BSC, POOL, 1000)
    assert st == PoolState(1000, 7, 9, 4242, 123, -5)
    assert {b for _, b in calls} == {hex(1000)}


def test_pancake_pool_state_refuses_a_partial_read(tmp_path, monkeypatch):
    from defi_tracker.adapters._rpc import RpcClient

    monkeypatch.setattr(RpcClient, "eth_call", lambda self, to, data, block="latest": None)
    with pytest.raises(RuntimeError, match="unreadable"):
        _pancake(tmp_path).pool_state_at(Chain.BSC, POOL, 1000)
