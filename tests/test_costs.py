"""
Gas and slippage as cost lines.

`pnl_decomposition` used to answer "what did the positions do" and call it
PnL, with gas — realized, already paid — appearing nowhere. These tests pin
the two ways that fix can go wrong: double-counting gas across the several
events of one transaction, and quietly presenting a partial cost base as a
complete one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from defi_tracker.analytics import cost_summary, pnl_decomposition
from defi_tracker.core.types import Chain
from defi_tracker.costs import BackfillResult, backfill_gas, gas_native_from_receipt


class _FakeRpc:
    """An RPC that returns canned receipts and counts the calls."""

    def __init__(self, receipts: dict):
        self.receipts = receipts
        self.calls: list[str] = []

    def get_transaction_receipt(self, tx_hash: str):
        self.calls.append(tx_hash)
        return self.receipts.get(tx_hash)


class _FakePrices:
    def __init__(self, price=Decimal("600")):
        self.price = price

    def get(self, _token, _ts):
        return self.price


def _receipt(gas_used: int, gas_price: int) -> dict:
    return {"gasUsed": hex(gas_used), "effectiveGasPrice": hex(gas_price)}


# ── reading a receipt ─────────────────────────────────────────────────────


def test_gas_is_used_times_effective_price():
    # 100,000 gas at 3 gwei = 0.0003 native
    assert gas_native_from_receipt(_receipt(100_000, 3_000_000_000)) == Decimal("0.0003")


def test_pre_1559_nodes_fall_back_to_gas_price():
    r = {"gasUsed": hex(100_000), "gasPrice": hex(3_000_000_000)}
    assert gas_native_from_receipt(r) == Decimal("0.0003")


def test_a_receipt_without_a_price_is_unknown_not_zero():
    assert gas_native_from_receipt({"gasUsed": hex(100_000)}) is None
    assert gas_native_from_receipt({}) is None


def test_integer_fields_are_accepted_as_well_as_hex():
    assert gas_native_from_receipt({"gasUsed": 100_000, "effectiveGasPrice": 3_000_000_000}) == (
        Decimal("0.0003")
    )


# ── the backfill ──────────────────────────────────────────────────────────


@pytest.fixture
def seeded(storage_with_wallet, make_event):
    """Two events sharing one transaction, plus a second transaction. Gas is
    paid per transaction, so this is exactly the shape that double-counts."""
    storage_with_wallet.upsert_events(
        [
            make_event(tx_hash="0xaaa", log_index=0, ts=1_700_000_000),
            make_event(tx_hash="0xaaa", log_index=1, ts=1_700_000_000),
            make_event(tx_hash="0xbbb", log_index=0, ts=1_700_000_100),
        ]
    )
    return storage_with_wallet


def test_gas_is_charged_once_per_transaction_not_once_per_event(seeded):
    rpc = _FakeRpc(
        {"0xaaa": _receipt(100_000, 3_000_000_000), "0xbbb": _receipt(50_000, 3_000_000_000)}
    )
    result = backfill_gas(
        seeded, rpc_factory=lambda _c: rpc, price_fetcher=_FakePrices(Decimal("600"))
    )

    assert result.priced == 2
    assert sorted(rpc.calls) == ["0xaaa", "0xbbb"]
    rows = seeded.tx_cost_rows()
    assert len(rows) == 2
    # 0.0003 * 600 + 0.00015 * 600 = 0.18 + 0.09
    assert cost_summary(rows)["gas_usd"] == Decimal("0.27")


def test_a_second_pass_re_reads_nothing(seeded):
    rpc = _FakeRpc(
        {"0xaaa": _receipt(100_000, 3_000_000_000), "0xbbb": _receipt(50_000, 3_000_000_000)}
    )
    backfill_gas(seeded, rpc_factory=lambda _c: rpc, price_fetcher=_FakePrices())
    calls_after_first = len(rpc.calls)
    again = backfill_gas(seeded, rpc_factory=lambda _c: rpc, price_fetcher=_FakePrices())

    assert len(rpc.calls) == calls_after_first
    assert again.considered == 0


def test_an_unpriceable_transaction_keeps_the_burn_and_stays_unpriced(seeded):
    """The gas burned is a fact even when its USD value is not. Recording the
    burn means a later pass can price it without re-reading the chain."""
    rpc = _FakeRpc(
        {"0xaaa": _receipt(100_000, 3_000_000_000), "0xbbb": _receipt(50_000, 3_000_000_000)}
    )

    class _NoPrice:
        def get(self, _t, _ts):
            return None

    result = backfill_gas(seeded, rpc_factory=lambda _c: rpc, price_fetcher=_NoPrice())
    assert result.priced == 0
    assert result.no_price == 2

    rows = {r["tx_hash"]: r for r in seeded.tx_cost_rows()}
    assert rows["0xaaa"]["gas_usd"] is None
    assert rows["0xaaa"]["gas_native"] == pytest.approx(0.0003)
    # Still on the work list — unknown is a retry, not a resolved zero.
    assert len(seeded.transactions_missing_cost()) == 2


def test_a_missing_receipt_is_counted_and_written_nowhere(seeded):
    rpc = _FakeRpc({"0xaaa": _receipt(100_000, 3_000_000_000)})
    result = backfill_gas(seeded, rpc_factory=lambda _c: rpc, price_fetcher=_FakePrices())

    assert result.priced == 1
    assert result.no_receipt == 1
    assert [r["tx_hash"] for r in seeded.tx_cost_rows() if r["gas_usd"] is not None] == ["0xaaa"]


def test_no_rpc_for_a_chain_leaves_gas_unknown_rather_than_failing(seeded):
    result = backfill_gas(seeded, rpc_factory=lambda _c: None, price_fetcher=_FakePrices())
    assert result.no_rpc == 2
    assert result.priced == 0
    assert all(r["gas_usd"] is None for r in seeded.tx_cost_rows())


def test_the_backfill_can_be_limited_and_scoped_to_a_chain(seeded):
    rpc = _FakeRpc(
        {"0xaaa": _receipt(100_000, 3_000_000_000), "0xbbb": _receipt(50_000, 3_000_000_000)}
    )
    result = backfill_gas(
        seeded, chain=Chain.BSC, limit=1, rpc_factory=lambda _c: rpc, price_fetcher=_FakePrices()
    )
    assert result.considered == 1
    assert result.priced == 1

    assert backfill_gas(seeded, chain=Chain.BASE, rpc_factory=lambda _c: rpc).considered == 0


def test_the_result_reads_as_a_sentence():
    assert "2/3 transactions priced" in str(BackfillResult(considered=3, priced=2, no_receipt=1))


# ── the summary ───────────────────────────────────────────────────────────


def test_no_transactions_at_all_is_unknown_not_zero():
    c = cost_summary([])
    assert c["gas_usd"] is None
    assert c["total_usd"] is None
    assert c["gas_is_complete"] is False


def test_coverage_is_reported_alongside_the_total():
    rows = [
        {"gas_usd": 1.5, "slippage_usd": None},
        {"gas_usd": None, "slippage_usd": None},
        {"gas_usd": None, "slippage_usd": None},
    ]
    c = cost_summary(rows)
    assert c["gas_usd"] == Decimal("1.5")
    assert (c["gas_known_txs"], c["txs"]) == (1, 3)
    assert c["gas_is_complete"] is False


def test_slippage_is_never_summed_from_nothing():
    c = cost_summary([{"gas_usd": 1.0, "slippage_usd": None}])
    assert c["slippage_usd"] is None
    assert c["gas_is_complete"] is True


# ── the decomposition ─────────────────────────────────────────────────────


def _snap(**over) -> dict:
    row = {
        "current_value_usd": 10_000.0,
        "unclaimed_usd": 0.0,
        "cost_basis_usd": 9_000.0,
        "hold_value_usd": 9_500.0,
        "il_usd": 500.0,
        "pnl_usd": 1_000.0,
        "pair_label": "A/B",
    }
    row.update(over)
    return row


def test_costs_are_absent_unless_asked_for():
    d = pnl_decomposition([_snap()])
    assert d["gas"] is None
    assert d["net_after_costs"] is None


def test_net_after_costs_subtracts_a_complete_cost_base():
    d = pnl_decomposition([_snap()], [{"gas_usd": 40.0, "slippage_usd": None}])
    assert d["net"] == Decimal("1000")
    assert d["gas"] == Decimal("40")
    assert d["net_after_costs"] == Decimal("960")


def test_net_after_costs_is_withheld_while_the_cost_base_is_partial():
    """A partial cost base subtracted from a complete PnL flatters the book.
    The number is withheld rather than approximated."""
    d = pnl_decomposition(
        [_snap()],
        [{"gas_usd": 40.0, "slippage_usd": None}, {"gas_usd": None, "slippage_usd": None}],
    )
    assert d["gas"] == Decimal("40")
    assert d["net_after_costs"] is None


def test_net_itself_is_untouched_by_costs():
    """`net` is what the marks and the events say. Costs sit beside it, so a
    surface that has always rendered `net` keeps meaning the same thing."""
    plain = pnl_decomposition([_snap()])
    with_costs = pnl_decomposition([_snap()], [{"gas_usd": 40.0, "slippage_usd": None}])
    assert plain["net"] == with_costs["net"]
