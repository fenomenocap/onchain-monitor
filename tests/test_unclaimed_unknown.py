"""An unreadable unclaimed balance is UNKNOWN, not zero.

The live case: `BASE_RPC_URL` was unset, the public Base endpoint throttled the
fee-growth calls, and a $27k position reported `unclaimed_usd = $0.00` — a real
number, indistinguishable from a position that has genuinely accrued nothing.

The dangerous consumer is not the display, it is the yield. `window_yields`
differences unclaimed between the two ends of a window, so pairing a placeholder
zero with a real balance fabricates a swing the size of the whole balance — a
made-up yield, which is worse than no yield at all.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from defi_tracker.analytics import window_yields
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, Position, ProtocolKind, Token, TokenAmount

WALLET = "0x1111111111111111111111111111111111111111"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "unk.db")
    s.init_schema()
    return s


def _position(unclaimed: Decimal | None, key: str = "pool:1:2:99") -> Position:
    tok = Token(Chain.BASE, "0x" + "aa" * 20, "WETH", 18)
    return Position(
        wallet=WALLET,
        chain=Chain.BASE,
        protocol_id="uniswap_v3",
        position_key=key,
        protocol_kind=ProtocolKind.CL_AMM,
        pair_label="WETH/PEAQ",
        tokens=[tok],
        current_value_usd=Decimal("27000"),
        current_balances=[TokenAmount(tok, Decimal("1"))],
        unclaimed_usd=unclaimed,
    )


class TestPositionType:
    def test_none_means_unreadable_not_zero(self):
        assert _position(None).unclaimed_known is False
        assert _position(Decimal("0")).unclaimed_known is True


class TestPersistence:
    def test_unknown_is_flagged_and_stores_a_placeholder_zero(self, storage: Storage):
        storage.write_snapshot(position=_position(None))
        (row,) = storage.latest_snapshot()
        assert row["unclaimed_is_known"] == 0
        # The stored 0 is a placeholder the column type forces, not a reading.
        assert row["unclaimed_usd"] == 0

    def test_a_genuine_zero_stays_known(self, storage: Storage):
        storage.write_snapshot(position=_position(Decimal("0")))
        (row,) = storage.latest_snapshot()
        assert row["unclaimed_is_known"] == 1

    def test_a_real_balance_round_trips(self, storage: Storage):
        storage.write_snapshot(position=_position(Decimal("123.45")))
        (row,) = storage.latest_snapshot()
        assert row["unclaimed_is_known"] == 1
        assert row["unclaimed_usd"] == pytest.approx(123.45)


# ── The one that actually protects a number ───────────────────────────────


def _wstat(*, start: float, end: float, start_known: int = 1, end_known: int = 1) -> dict:
    return {
        "wallet": WALLET,
        "chain": "base",
        "protocol_id": "uniswap_v3",
        "position_key": "pool:1:2:99",
        "pair_label": "WETH/PEAQ",
        "avg_value_usd": 27000.0,
        "snapshot_days": 30,
        "span_days": 30,
        "unclaimed_start": start,
        "unclaimed_end": end,
        "unclaimed_start_known": start_known,
        "unclaimed_end_known": end_known,
        "window_start": "2026-07-16",
    }


class TestWindowYields:
    KEY = (WALLET, "base", "uniswap_v3", "pool:1:2:99")

    def test_known_endpoints_annualize_normally(self):
        out = window_yields([_wstat(start=100.0, end=400.0)], [], window_days=30)
        assert out[self.KEY]["apy_pct"] == pytest.approx(300 / 27000 * 365 / 30 * 100)

    def test_an_unknown_END_does_not_invent_a_loss(self):
        """start $500 real, end unreadable → stored 0 → a fabricated −$500."""
        out = window_yields([_wstat(start=500.0, end=0.0, end_known=0)], [], window_days=30)
        assert out[self.KEY]["apy_pct"] is None
        assert out[self.KEY]["unclaimed_known"] is False

    def test_an_unknown_START_does_not_invent_a_gain(self):
        """The mirror: a placeholder 0 at the start makes the whole end balance
        look like fees earned inside the window."""
        out = window_yields([_wstat(start=0.0, end=500.0, start_known=0)], [], window_days=30)
        assert out[self.KEY]["apy_pct"] is None

    def test_the_flip_mid_window_case(self):
        """A position that goes unknown → known (an RPC recovering) is exactly
        when the delta is most wrong and most plausible-looking."""
        out = window_yields([_wstat(start=0.0, end=812.0, start_known=0)], [], window_days=30)
        assert out[self.KEY]["apy_pct"] is None

    def test_rows_without_the_columns_behave_as_before(self):
        """Pre-migration rows were all genuinely read; they must not go silent."""
        stat = _wstat(start=100.0, end=400.0)
        del stat["unclaimed_start_known"]
        del stat["unclaimed_end_known"]
        out = window_yields([stat], [], window_days=30)
        assert out[self.KEY]["apy_pct"] is not None


class TestEndToEndThroughStorage:
    def test_window_stats_report_endpoint_knownness(self, storage: Storage):
        """Wire-level: the flag written by write_snapshot has to survive the
        window query, or the guard above never fires in production."""
        import datetime as _dt

        today = _dt.date.today()
        for i, known in enumerate([False, True]):
            day = (today - _dt.timedelta(days=10 - i * 10)).isoformat()
            storage.write_snapshot(
                position=_position(None if not known else Decimal("500")),
                snapshot_date=_dt.date.fromisoformat(day),
            )
        (stat,) = storage.position_window_stats(30)
        assert stat["unclaimed_start_known"] == 0
        assert stat["unclaimed_end_known"] == 1
        assert (
            window_yields([stat], [], window_days=30)[
                (WALLET, "base", "uniswap_v3", "pool:1:2:99")
            ]["apy_pct"]
            is None
        )
