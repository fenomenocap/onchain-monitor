"""
Legacy XYK history from LP-token transfers (MachineX, peaq).

These positions have no NFT, so the CL scan never saw them and they carried
no event history. A deposit is LP minted to the wallet next to the pair's
Mint; a withdrawal is LP sent back next to Burn; a bare LP transfer moves a
share of the reserves at that block.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from defi_tracker.adapters.machinex import MachineXAdapter
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, EventKind, PricePoint, Token

W = "0x1111111111111111111111111111111111111111"
POOL = "0x42f526f24c5cf894efb4f887d911605d908b9855"
USDC = "0xbba60da06c2c5424f03f7434542280fcad453d10"
USDT = "0xf4d9235269a96aadafc9adae454a0618ebe37949"
KEY = f"legacy:{POOL}:{POOL}{W[2:]}"
ZERO = "0x" + "0" * 64


def _t(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:]


def _word(v: int) -> str:
    return v.to_bytes(32, "big").hex()


def _log(idx: int, topics: list[str], data: str) -> dict:
    return {"address": POOL, "logIndex": hex(idx), "topics": topics, "data": "0x" + data}


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch) -> MachineXAdapter:
    db = tmp_path / "t.db"
    st = Storage(db)
    st.init_schema()
    st.upsert_token(Token(Chain.PEAQ, USDC, "USDC", 6))
    st.upsert_token(Token(Chain.PEAQ, USDT, "USDT", 6))
    ad = MachineXAdapter(graph_api_key="k", rpc_url="https://peaq.example", db_path=str(db))
    rpc = MagicMock()
    rpc.get_block_timestamp.return_value = 1_750_000_000
    ad._rpc = rpc  # noqa: SLF001
    monkeypatch.setattr(ad, "_legacy_tokens", lambda pool, chain: (
        Token(Chain.PEAQ, USDC, "USDC", 6), Token(Chain.PEAQ, USDT, "USDT", 6)))  # fmt: skip
    monkeypatch.setattr(ad, "get_historical_price",
                        lambda tok, ts: PricePoint(tok.key, Decimal("1"), ts, "stable"))  # fmt: skip
    return ad


def _receipt(logs: list[dict], block: int = 100) -> dict:
    return {"blockNumber": hex(block), "logs": logs}


def test_a_mint_is_a_deposit_of_the_mint_amounts(adapter):
    T, M = adapter._TOPIC_TRANSFER, adapter._TOPIC_XYK_MINT  # noqa: SLF001
    adapter._rpc.get_transaction_receipt.return_value = _receipt([  # noqa: SLF001
        _log(3, [T, ZERO, _t("0x" + "f" * 40)], _word(5)),  # protocol fee mint, not ours
        _log(4, [T, ZERO, _t(W)], _word(1000)),
        _log(5, [M, _t("0x" + "a" * 40)], _word(10_000_000) + _word(11_000_000)),
    ])  # fmt: skip
    (ev,) = adapter._legacy_events_from_tx(W, Chain.PEAQ, POOL, KEY, "0xtx")  # noqa: SLF001
    assert ev.kind == EventKind.DEPOSIT and ev.log_index == 5 and ev.position_key == KEY
    assert [a.amount for a in ev.amounts] == [Decimal(10), Decimal(11)]
    assert ev.usd_at_ts == Decimal(21)


def test_a_burn_is_a_withdrawal_signed_negative(adapter):
    T, B = adapter._TOPIC_TRANSFER, adapter._TOPIC_XYK_BURN  # noqa: SLF001
    adapter._rpc.get_transaction_receipt.return_value = _receipt([  # noqa: SLF001
        _log(7, [T, _t(W), _t(POOL)], _word(1000)),
        _log(9, [B, _t("0x" + "a" * 40), _t(W)], _word(4_000_000) + _word(6_000_000)),
    ])  # fmt: skip
    (ev,) = adapter._legacy_events_from_tx(W, Chain.PEAQ, POOL, KEY, "0xtx")  # noqa: SLF001
    assert ev.kind == EventKind.WITHDRAW and ev.usd_at_ts == Decimal(-10)


def test_a_bare_lp_transfer_moves_a_share_of_reserves_at_that_block(adapter):
    T = adapter._TOPIC_TRANSFER  # noqa: SLF001
    adapter._rpc.get_transaction_receipt.return_value = _receipt(  # noqa: SLF001
        [_log(2, [T, _t("0x" + "b" * 40), _t(W)], _word(250))], block=500
    )

    def call(to, data, block="latest"):
        assert block == hex(499)  # state BEFORE the transfer's block
        if data == adapter._SEL_GET_RESERVES:  # noqa: SLF001
            return bytes.fromhex(_word(40_000_000) + _word(60_000_000))
        return bytes.fromhex(_word(1000))  # totalSupply

    adapter._rpc.eth_call.side_effect = call  # noqa: SLF001
    (ev,) = adapter._legacy_events_from_tx(W, Chain.PEAQ, POOL, KEY, "0xtx")  # noqa: SLF001
    assert ev.kind == EventKind.DEPOSIT
    assert [a.amount for a in ev.amounts] == [Decimal(10), Decimal(15)]  # a quarter of each


def test_a_missing_receipt_raises_rather_than_skipping(adapter):
    adapter._rpc.get_transaction_receipt.return_value = None  # noqa: SLF001
    with pytest.raises(RuntimeError, match="no receipt"):
        adapter._legacy_events_from_tx(W, Chain.PEAQ, POOL, KEY, "0xtx")  # noqa: SLF001
