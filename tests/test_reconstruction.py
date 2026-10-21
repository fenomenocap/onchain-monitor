"""
Filling snapshot holes from archive state.

A reconstructed day must read the chain AT that day, price at that day's
close, count only the events up to it, say it was reconstructed, and never
overwrite a day that was observed live.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from defi_tracker.adapters._base import end_of_day_price_ts
from defi_tracker.adapters._rpc import RpcClient
from defi_tracker.core.runner import TrackerRunner
from defi_tracker.core.types import Chain, EventKind, PricePoint
from tests.conftest import TEST_WALLET


def test_a_pinned_client_reads_state_at_its_block():
    seen = []

    def post(url, json, **_):
        seen.append(json["params"][1])
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.json.return_value = {"result": "0x" + "00" * 32}
        return r

    with patch("defi_tracker.adapters._rpc.requests.post", side_effect=post):
        RpcClient("https://x").pinned(1234).eth_call("0xa", b"\x00")
    assert seen == [hex(1234)]


def test_block_at_finds_the_last_block_not_after_the_moment():
    rpc = RpcClient("https://x")
    rpc.get_block_number = lambda: 10_000  # type: ignore[method-assign]
    rpc.get_block_timestamp = lambda b: 1_000 + b * 3 + (b // 1000)  # type: ignore[method-assign]
    b = rpc.block_at(20_000)
    assert rpc.get_block_timestamp(b) <= 20_000 < rpc.get_block_timestamp(b + 1)


def test_the_end_of_a_day_is_priced_with_the_next_days_open():
    ts = int(datetime(2026, 9, 19, 23, 59, 59, tzinfo=UTC).timestamp())
    assert datetime.fromtimestamp(end_of_day_price_ts(ts), UTC).date() == date(2026, 9, 20)


def test_holes_are_the_days_a_feed_skipped(storage, make_position):
    for d in (date(2026, 9, 17), date(2026, 9, 18), date(2026, 9, 21)):
        storage.write_snapshot(make_position(), snapshot_date=d)
    assert [h[3] for h in storage.snapshot_holes()] == ["2026-09-19", "2026-09-20"]


def _adapter(make_position, liquidity_value="1500"):
    ad = MagicMock()
    ad.info.protocol_id = "pancake_infinity"
    ad.block_at.return_value = 777
    pos = make_position(current_value_usd=Decimal(liquidity_value))
    pos.meta = {"source": "archive", "block": 777}
    ad.fetch_positions_at.return_value = [pos]
    ad.get_historical_price.side_effect = lambda tok, ts: PricePoint(tok.key, Decimal("1"), ts, "t")
    return ad


def test_a_reconstructed_day_counts_only_the_events_before_it(storage, make_event, make_position):
    storage.upsert_events(
        [
            make_event(ts=int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()), tx_hash="0xa",
                       usdt_amount=Decimal("1000"), weth_amount=Decimal("0")),
            make_event(ts=int(datetime(2026, 9, 25, tzinfo=UTC).timestamp()), tx_hash="0xb",
                       usdt_amount=Decimal("500"), weth_amount=Decimal("0")),
        ]
    )  # fmt: skip
    ad = _adapter(make_position)
    n = TrackerRunner(storage).reconstruct_day(ad, TEST_WALLET, Chain.BSC, date(2026, 9, 19))
    assert n == 1
    (row,) = storage.snapshots_as_of(date(2026, 9, 19))
    assert row["snapshot_date"] == "2026-09-19"
    assert row["cost_basis_usd"] == 1000  # the 09-25 deposit had not happened yet
    ts = ad.fetch_positions_at.call_args.args[3]
    assert datetime.fromtimestamp(ts, UTC) == datetime(2026, 9, 19, 23, 59, 59, tzinfo=UTC)


def test_an_observed_day_is_never_overwritten(storage, make_position):
    storage.write_snapshot(
        make_position(current_value_usd=Decimal("999")), snapshot_date=date(2026, 9, 19)
    )
    ad = _adapter(make_position)
    assert (
        TrackerRunner(storage).reconstruct_day(ad, TEST_WALLET, Chain.BSC, date(2026, 9, 19)) == 0
    )
    ad.fetch_positions_at.assert_not_called()
    (row,) = storage.snapshots_as_of(date(2026, 9, 19))
    assert row["current_value_usd"] == 999


def test_reconstructed_rows_say_so(storage, make_position, make_event):
    storage.upsert_events([make_event(kind=EventKind.DEPOSIT)])
    TrackerRunner(storage).reconstruct_day(_adapter(make_position), TEST_WALLET, Chain.BSC,
                                           date(2026, 9, 19))  # fmt: skip
    (row,) = storage.snapshots_as_of(date(2026, 9, 19))
    assert '"source": "archive"' in row["meta_json"]


def test_block_at_converges_when_block_times_change():
    """BSC went from 3s to 0.75s blocks; pure interpolation never converged on
    that and returned block 1 for 2026-06-09."""
    rpc = RpcClient("https://x")
    switch = 50_000_000

    def ts_of(b: int) -> int:
        return 1_600_000_000 + (b * 3 if b <= switch else switch * 3 + (b - switch) * 3 // 4)

    rpc.get_block_number = lambda: 124_000_000  # type: ignore[method-assign]
    rpc.get_block_timestamp = ts_of  # type: ignore[method-assign]
    target = ts_of(107_000_000) + 1
    b = rpc.block_at(target)
    assert ts_of(b) <= target < ts_of(b + 1)
