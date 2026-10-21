"""IL compares a position's value with holding its tokens; both sides must be
priced the same way, or the "IL" is partly the gap between two price feeds."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from defi_tracker.adapters.machinex import MachineXAdapter
from defi_tracker.core.types import Chain, Token


def test_hodl_reads_the_price_the_position_was_valued_at(tmp_path):
    ad = MachineXAdapter(graph_api_key="k", db_path=str(tmp_path / "t.db"))
    tok = Token(Chain.PEAQ, "0x3cd66d2e1fac1751b0a20bebf6ca4c9699bb12d7", "WPEAQ", 18)
    ad._current_price_cache[tok.key] = Decimal("0.0284")  # noqa: SLF001 — as the subgraph path writes it
    with patch("defi_tracker.adapters._base.requests.get") as get:
        pp = ad.get_current_price(tok)
    assert pp is not None and pp.price_usd == Decimal("0.0284")
    get.assert_not_called()  # no CoinGecko spot slipped in on the HODL side
