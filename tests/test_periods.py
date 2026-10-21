"""
Period PnL (Phase 1.1 / 1.2).

The one property every test here protects: capital moving in or out is not
performance. A deposit inside a window must not read as profit, a withdrawal
must not read as a loss, and whatever cannot be established — a missing start
mark, an unsynced event history, an unreadable balance — is excluded and
stated, never approximated.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from defi_tracker.analytics import PERIOD_KINDS, period_start
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, EventKind
from defi_tracker.report import gather_period, gather_periods
from tests.conftest import TEST_WALLET

END = date(2026, 9, 25)
MONTH = date(2026, 9, 1)


def _ts(d: date, hour: int = 12) -> int:
    return int(datetime.combine(d, time(hour), tzinfo=UTC).timestamp())


@pytest.fixture
def book(storage: Storage, make_position, make_event):
    """Helpers that write snapshots, events and a healthy sync row."""

    class Book:
        st = storage

        def snap(
            self,
            day: date,
            value: str,
            *,
            unclaimed: str | None = "0",
            il: str | None = "0",
            key: str = "p1",
            chain: Chain = Chain.BSC,
            pair: str = "USDT/ETH 0.05%",
        ) -> None:
            pos = make_position(
                current_value_usd=Decimal(value),
                unclaimed_usd=Decimal(unclaimed) if unclaimed is not None else None,
                position_key=key,
                chain=chain,
                pair_label=pair,
            )
            pos.snapshot_at = _ts(day)
            v = Decimal(value)
            storage.write_snapshot(
                pos,
                snapshot_date=day,
                il_usd=Decimal(il) if il is not None else None,
                hold_value_usd=v - Decimal(il) if il is not None else None,
            )

        def event(
            self,
            day: date,
            kind: EventKind,
            usd: str,
            *,
            key: str = "p1",
            tx: str | None = None,
            chain: Chain = Chain.BSC,
            hour: int = 14,
            log_index: int = 0,
        ) -> None:
            amount = Decimal(usd)
            ev = make_event(
                kind=kind,
                ts=_ts(day, hour),
                log_index=log_index,
                tx_hash=tx or f"0x{kind.value}{day.isoformat()}{key}{hour}",
                usdt_amount=amount,
                weth_amount=Decimal("0"),
                position_key=key,
                chain=chain,
            )
            storage.upsert_events([ev])

        def synced(self, at: date = END, chain: Chain = Chain.BSC) -> None:
            storage.record_pipeline_health(
                TEST_WALLET,
                chain,
                "pancake_infinity",
                now=_ts(at, 23),
                sync_ok=True,
                snapshot_ok=True,
                source="subgraph",
                error=None,
            )

        def mtd(self):
            return gather_period(storage, MONTH, END, "MTD")

    return Book()


def _only(period):
    assert not period.excluded, [e.reason for e in period.excluded]
    assert len(period.rows) == 1
    return period.rows[0]


# ── period bounds ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "expect"),
    [
        ("D", date(2026, 9, 25)),
        ("WTD", date(2026, 9, 21)),  # the Monday
        ("MTD", date(2026, 9, 1)),
        ("QTD", date(2026, 7, 1)),
        ("YTD", date(2026, 1, 1)),
        ("ITD", None),
    ],
)
def test_period_starts(kind, expect):
    assert period_start(kind, END) == expect


def test_unknown_period_is_an_error_not_a_default():
    with pytest.raises(ValueError):
        period_start("MTX", END)


# ── flows are not performance ─────────────────────────────────────────────


def test_a_deposit_inside_the_window_is_not_profit(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(date(2026, 8, 31), "1000")
    book.event(date(2026, 9, 10), EventKind.DEPOSIT, "500")
    book.snap(END, "1520")
    book.synced()
    row = _only(book.mtd())
    assert row.net_flows == Decimal("500")
    assert row.pnl == Decimal("20")


def test_a_withdrawal_inside_the_window_is_not_a_loss(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(date(2026, 8, 31), "1000")
    book.event(date(2026, 9, 10), EventKind.WITHDRAW, "-400")
    book.snap(END, "590")
    book.synced()
    assert _only(book.mtd()).pnl == Decimal("-10")


def test_decomposition_adds_up_exactly(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(date(2026, 8, 31), "1000", unclaimed="10", il="-20")
    book.event(date(2026, 9, 3), EventKind.COLLECT, "15")
    book.snap(END, "1100", unclaimed="12", il="-50")
    book.synced()
    row = _only(book.mtd())
    assert row.fees_earned == Decimal("17")  # 15 collected + 2 more unclaimed
    assert row.il == Decimal("-30")
    assert row.market == Decimal("130")
    assert row.market + row.il + row.fees_earned == row.pnl == Decimal("117")


def test_no_hold_value_means_no_split_never_a_guessed_one(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(date(2026, 8, 31), "1000", il=None)
    book.snap(END, "1100")
    book.synced()
    period = book.mtd()
    row = _only(period)
    assert row.il is None and row.market is None
    t = period.totals()
    assert t["undecomposed"] == Decimal("100")
    assert t["market"] + t["il"] + t["undecomposed"] + t["fees_earned"] == t["pnl"]


# ── what cannot be established is excluded and said ───────────────────────


def test_a_position_opened_inside_the_window_starts_from_zero(book):
    book.event(date(2026, 9, 10), EventKind.DEPOSIT, "1000")
    book.snap(END, "1030")
    book.synced()
    row = _only(book.mtd())
    assert row.start_date is None
    assert row.pnl == Decimal("30")


def test_a_position_that_existed_but_has_no_start_mark_is_excluded(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(END, "1030")  # no mark on or before 08-31
    book.synced()
    period = book.mtd()
    assert not period.rows
    assert period.excluded[0].reason == "no mark at period start"
    assert period.excluded_value() == Decimal("1030")


def test_a_start_mark_older_than_the_tolerance_is_excluded(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(date(2026, 8, 20), "1000")
    book.snap(END, "1030")
    book.synced()
    reason = book.mtd().excluded[0].reason
    assert reason == "no mark at period start (latest is 2026-08-20)"


def test_a_one_day_late_start_mark_is_tolerated_over_a_month(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(date(2026, 8, 30), "1000")
    book.snap(END, "1030")
    book.synced()
    assert _only(book.mtd()).start_date == "2026-08-30"


def test_a_day_behind_is_not_tolerated_for_a_day(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(date(2026, 9, 23), "1000")
    book.snap(date(2026, 9, 24), "1010")
    book.synced()
    period = gather_period(book.st, END, END, "D")
    assert period.excluded[0].reason == "marks 1d old (as of 2026-09-24)"


def test_unsynced_events_exclude_the_position(book):
    """The bsc case: marks are current but the event feed stopped, so a
    deposit inside the window could be missing and read as profit."""
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(date(2026, 8, 31), "1000")
    book.snap(END, "1520")
    book.synced(at=date(2026, 7, 6))
    reason = book.mtd().excluded[0].reason
    assert reason == "events not synced through period end (last clean sync 2026-07-06)"


def test_unreadable_unclaimed_excludes_rather_than_counting_zero(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(date(2026, 8, 31), "1000", unclaimed="50")
    book.snap(END, "1000", unclaimed=None)
    book.synced()
    assert book.mtd().excluded[0].reason == "unclaimed unreadable (2026-09-25)"


def test_a_position_with_no_events_at_all_is_excluded(book):
    book.snap(date(2026, 8, 31), "1000")
    book.snap(END, "1100")
    book.synced()
    assert book.mtd().excluded[0].reason == "no event history — flows unknowable"


# ── closures ──────────────────────────────────────────────────────────────


def test_a_position_closed_inside_the_window_books_its_exit(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(date(2026, 8, 31), "1000", il="-20")
    book.event(date(2026, 9, 10), EventKind.WITHDRAW, "-990")
    book.st.write_tombstone_snapshot(
        TEST_WALLET, Chain.BSC, "pancake_infinity", "p1", "USDT/ETH 0.05%", "cl_amm", END
    )
    # A tombstone is stamped with the wall clock (it is written the day the
    # closure is seen), so the clean sync that goes with it is today's too —
    # a fixed date here expired 48h after END.
    book.synced(datetime.now(UTC).date())
    row = _only(book.mtd())
    assert row.closed
    assert row.pnl == Decimal("-10")
    assert row.market + row.il == row.price


def test_a_position_closed_before_the_window_is_not_in_it(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.event(date(2026, 3, 5), EventKind.WITHDRAW, "-1000")
    book.synced()
    period = book.mtd()
    assert not period.rows and not period.excluded


# ── gas ───────────────────────────────────────────────────────────────────


def test_gas_is_split_across_the_positions_one_transaction_touched(book):
    for i, key in enumerate(("p1", "p2")):
        book.event(
            date(2026, 9, 10), EventKind.DEPOSIT, "500", key=key, tx="0xrebalance", log_index=i
        )
        book.snap(END, "500", key=key)
    book.st.upsert_tx_cost(
        "bsc", "0xrebalance", ts=_ts(date(2026, 9, 10)), gas_native=0.001, gas_usd=0.6,
        native_symbol="BNB", native_price_usd=600.0, source="receipt",
    )  # fmt: skip
    book.synced()
    period = book.mtd()
    assert [r.gas for r in period.rows] == [Decimal("0.3"), Decimal("0.3")]
    t = period.totals()
    assert t["gas"] == Decimal("0.6")
    assert t["gas_txs"] == 1  # one transaction, however many positions it touched
    assert t["net_after_gas"] == t["pnl"] - Decimal("0.6")


def test_unpriced_gas_withholds_the_net(book):
    book.event(date(2026, 9, 10), EventKind.DEPOSIT, "500")
    book.snap(END, "500")
    book.synced()
    t = book.mtd().totals()
    assert t["gas_unpriced_txs"] == 1
    assert t["net_after_gas"] is None


# ── inception and slicing ─────────────────────────────────────────────────


def test_inception_to_date_is_lifetime_pnl(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.event(date(2026, 4, 5), EventKind.COLLECT, "80")
    book.snap(END, "950", unclaimed="5")
    book.synced()
    period = gather_period(book.st, None, END, "ITD")
    # value + unclaimed + collected − cost basis
    assert _only(period).pnl == Decimal("950") + 5 + 80 - 1000


def test_slices_sum_to_the_total(book):
    for key, chain, pair in (
        ("p1", Chain.BSC, "USDT/ETH 0.05%"),
        ("p2", Chain.BSC, "USDT/ETH 0.30%"),
        ("p3", Chain.BASE, "USDC/ETH 0.05%"),
    ):
        book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000", key=key, chain=chain)
        book.snap(date(2026, 8, 31), "1000", key=key, chain=chain, pair=pair)
        book.snap(END, "1100", key=key, chain=chain, pair=pair)
        book.synced(chain=chain)
    period = book.mtd()
    total = period.totals()["pnl"]
    for dim in ("position", "pair", "chain", "venue", "wallet"):
        assert sum(g["pnl"] for g in period.by(dim)) == total, dim
    pairs = {g["label"] for g in period.by("pair")}
    assert pairs == {"USDT/ETH", "USDC/ETH"}  # fee tiers aggregate


def test_gather_periods_ends_every_window_on_the_newest_mark(book):
    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(END, "1000")
    book.synced()
    periods = gather_periods(book.st)
    assert [p.kind for p in periods] == list(PERIOD_KINDS)
    assert {p.end for p in periods} == {END}


def test_gather_periods_on_an_empty_book_is_empty(storage):
    assert gather_periods(storage) == []


def test_a_window_with_every_position_excluded_is_not_rendered_as_zero(book, capsys, monkeypatch):
    """WTD on today's book: every start mark falls in the 09-19/20 hole. The
    CLI must say nothing was established, not print a PnL of $0.00."""
    from defi_tracker import cli

    book.event(date(2026, 1, 5), EventKind.DEPOSIT, "1000")
    book.snap(date(2026, 9, 18), "1000")
    book.snap(END, "1100")
    book.synced()
    monkeypatch.setattr(cli, "_db", lambda: book.st)
    cli.cmd_periods(type("A", (), {"month": None, "by": None, "period": None})())
    wtd = next(
        line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("WTD")
    )
    assert "every position excluded" in wtd
    assert "$" not in wtd
