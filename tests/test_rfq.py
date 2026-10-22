"""The RFQ tab's numbers: what each MM delivered against its contract, what
each bid commits, and what each bid costs. No network: Coinwatch is a temp
SQLite file, market depth is passed in."""

from __future__ import annotations

import sqlite3
from datetime import date

from defi_tracker.analytics import mm_delivery
from defi_tracker.coinwatch import fetch_delivery


def _obs(mm: str, venue: str, day: str, metric: str, own: float, rem: float | None = None):
    return {"metric": metric, "mm": mm, "venue": venue, "day": day, "own": own, "remaining": rem}


def test_coinwatch_reader_maps_venues_joins_the_rest_of_book_and_drops_the_day_in_progress(
    tmp_path,
):
    db = tmp_path / "history.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE observation (metric TEXT, mm TEXT, exchange TEXT, day TEXT, "
                "series TEXT, value REAL, first_seen TEXT, last_seen TEXT)")  # fmt: skip
    rows = [("depth_200", "mm-a", "gateio", "2026-10-15", "own", 50.0),
            ("depth_200", "mm-a", "gateio", "2026-10-15", "remaining", 40.0),
            ("spread", "mm-a", "gateio", "2026-10-15", "own", 0.2),
            ("depth_200", "mm-a", "gateio", "2026-10-16", "own", 99.0)]  # fmt: skip
    con.executemany("INSERT INTO observation VALUES (?,?,?,?,?,?,'','')", rows)
    con.commit()
    con.close()
    got = fetch_delivery(str(db), today=date(2026, 10, 16))
    assert {(r["metric"], r["venue"], r["own"], r["remaining"]) for r in got} == {
        ("depth_200", "gate", 50.0, 40.0),
        ("spread", "gate", 0.2, None),
    }
    assert fetch_delivery(str(tmp_path / "missing.sqlite3")) == []


def test_delivery_is_a_median_graded_against_the_contract_only_where_it_names_the_venue():
    days = [f"2026-10-{d:02d}" for d in range(1, 11)]
    rows = []
    for i, d in enumerate(days):
        # Gate: 8 days at $40k, 2 outage days at $0 → median $40k, on target 80%.
        rows.append(_obs("mm-a", "gate", d, "depth_200", 0.0 if i < 2 else 40_000.0, 60_000.0))
        rows.append(_obs("mm-a", "gate", d, "spread", 0.30 if i == 9 else 0.20))
        rows.append(_obs("mm-a", "bitget", d, "depth_200", 5_000.0, 1_000.0))
    t = [{"mm": "MM-A", "venues": ["gate"], "spread_pct": 0.25, "depth_2pct_usd": 30_000}]
    m = mm_delivery(rows, t, window_days=5)
    assert m["window"] == ("2026-10-06", "2026-10-10") and m["full"][0] == "2026-10-01"
    b = m["blocks"][0]
    g = b["cells"]["depth_200"]["gate"]
    assert (g["v90"], g["full"], g["status"]) == (40_000.0, 40_000.0, "ok")
    assert b["cells"]["on_target"]["gate"]["full"] == 80.0
    assert b["cells"]["on_target"]["gate"]["v90"] == 100.0
    # The last day's spread breaks the spread KPI: all-KPIs is 4 of the last 5.
    assert b["cells"]["all_kpis"]["gate"]["v90"] == 80.0
    # Bitget is not in the contract: a figure, no grade, no days-on-target.
    assert b["cells"]["depth_200"]["bitget"]["status"] == ""
    assert "bitget" not in b["cells"]["on_target"]
    assert m["market"]["gate"]["v90"] == 100_000.0  # own + rest of book


def test_a_book_share_cap_lowers_the_daily_kpi():
    """A KPI of "$12k or 40% of the book, whichever is lower" — a thin book
    lowers the bar for that day."""
    rows = [_obs("mm-b", "kraken", "2026-10-01", "depth_200", 8_000.0, 6_000.0)]
    t = [{"mm": "MM-B", "venues": ["kraken"], "depth_2pct_usd": 12_000, "book_share_cap": 0.4}]
    m = mm_delivery(rows, t)
    assert m["blocks"][0]["cells"]["on_target"]["kraken"]["v90"] == 100.0  # 8k ≥ 40% × 14k


def test_no_history_is_no_matrix():
    assert mm_delivery([], []) == {}


def test_the_rfq_tab_shows_the_matrix_coloured_against_the_contract():
    from defi_tracker.report import ReportData
    from defi_tracker.web import render_dashboard
    from tests.test_web import _snap

    rows = [_obs("mm-a", "kraken", "2026-10-01", "depth_200", 8_000.0, 50_000.0)]
    rfq = {"target": [{"mm": "MM-A", "venues": ["kraken", "kucoin"], "depth_2pct_usd": 30_000}]}
    out = render_dashboard(ReportData(snaps=[_snap()], rfq=rfq, delivery=rows))
    tab = out.split('id="rfq"', 1)[1].split("</section>", 1)[0]
    assert "Delivered vs contracted" in tab
    assert '<td class="bad">$8.0k' in tab  # below half the $30k KPI
    assert '<td class="bad">no data</td>' in tab  # KuCoin is contracted, nothing observed
    unread = render_dashboard(ReportData(snaps=[_snap()], rfq=rfq))
    assert "Coinwatch history not read" in unread


# ── bids ──────────────────────────────────────────────────────────────────


def test_a_commitment_counts_once_per_aggregate_and_never_where_peaq_cannot_trade():
    from defi_tracker.analytics import bid_commitments

    bid = {"spread_pct": 0.3, "depth": [
        {"venues": ["gate"], "d2": 40_000, "d1": 20_000},
        {"venues": ["bybit", "kucoin"], "group": "A", "d2": 45_000},  # one listed → counts once
        {"venues": ["coinbase"], "d2": 10_000},  # PEAQ not listed there
        {"venues": ["binance perp"], "d2": 60_000},
    ]}  # fmt: skip
    c = bid_commitments(bid, listed={"gate", "kucoin"})
    assert c["listed_d2_usd"] == 85_000 and c["perp_d2_usd"] == 60_000
    assert c["unlisted"] == ["bybit", "coinbase"]
    assert c["cells"]["depth_200"]["kucoin"] == {"usd": 45_000, "group": "A"}
    assert c["cells"]["spread"]["gate"]["usd"] == 0.3
    assert bid_commitments(bid, listed=None)["listed_d2_usd"] == 95_000  # markets unread: all spot


def test_black_scholes_call_matches_the_closed_form_and_its_limits():
    from defi_tracker.analytics import bs_call

    assert abs(bs_call(1.0, 1.0, 1.0, 1.0) - 0.38292) < 1e-4  # N(0.5) − N(−0.5)
    assert bs_call(1.0, 0.5, 0.8, 0.0) == 0.5  # expired: intrinsic
    assert bs_call(1.0, 2.0, 0.8, 0.0) == 0.0
    assert bs_call(1.0, 1.5, 1.2, 1.0) < bs_call(1.0, 1.0, 1.2, 1.0)  # dearer strike, cheaper call


def test_a_bid_costs_its_fee_its_options_and_its_carry_and_an_unstated_fee_is_not_zero():
    from defi_tracker.analytics import bid_cost, bs_call

    sd = 0.05  # daily σ
    bid = {"loan_usd": 100_000, "tenor_months": 12, "options": [{"share": 1, "strike_pct": 100}]}
    c = bid_cost(bid, spot=0.04, sigma_daily=sd, n_obs=90, carry_rate_pct=0.5, depth_usd=50_000)
    assert round(c["loan_peaq"]) == 2_500_000 and c["fee_usd"] is None
    expect = 2_500_000 * bs_call(0.04, 0.04, sd * 365**0.5, 1.0)
    assert abs(c["options_usd"] - expect) < 1e-6
    assert c["options_band"][0] < c["options_usd"] < c["options_band"][1]
    assert c["carry_usd"] == 500.0
    assert abs(c["per_1k"] - (expect + 500) / 50) < 1e-6
    retainer = bid_cost({"fee_usd_year": 120_000}, 0.04, sd, 90, 0.5, None)
    assert retainer["total_usd"] == 120_000 and retainer["per_1k"] is None
    assert bid_cost(bid, None, sd, 90, 0.5, 1.0)["total_usd"] is None  # no price, no cost


def test_dex_liquidity_verified_on_chain_shows_against_what_the_contract_requires():
    from defi_tracker.report import ReportData
    from defi_tracker.web import render_dashboard
    from tests.test_web import _snap

    rows = [_obs("mm-a", "kraken", "2026-10-01", "depth_200", 8_000.0, 50_000.0)]
    rfq = {"target": [{"mm": "MM-A", "venues": ["kraken"], "depth_2pct_usd": 30_000,
                       "dex_required": {"Pool": 100_000},
                       "dex_verified": [{"venue": "Pool X", "value_usd": 90_000, "note": "NFT #1"}]}],
           "dex": [{"label": "Pool X"}]}  # fmt: skip
    tab = render_dashboard(ReportData(snaps=[_snap()], rfq=rfq, delivery=rows))
    perf = tab.split("2 · How today", 1)[1].split("</table>", 1)[0]
    assert "Pool X $90.0k" in perf and "needs $100.0k" in perf
    assert '<td class="dex0 warn">$90.0k held<small>NFT #1 · req. $100.0k</small></td>' in tab


def test_a_contract_measured_wider_than_2pct_is_never_failed_on_coinwatchs_2pct_figure():
    """±2% depth is a floor for ±4% depth: a pass at ±2% is a pass, a miss is unknown."""
    rows = [_obs("mm-c", "gate", "2026-10-01", "depth_200", 9_000.0, 90_000.0),
            _obs("mm-c", "kraken", "2026-10-01", "depth_200", 20_000.0, 30_000.0)]  # fmt: skip
    t = [{"mm": "MM-C", "venues": ["gate", "kraken"], "depth_2pct_usd": 15_000,
          "depth_measured_at_pct": 4}]  # fmt: skip
    b = mm_delivery(rows, t)["blocks"][0]
    assert b["floor"] is True
    assert b["cells"]["depth_200"]["gate"]["status"] == ""  # miss at ±2%: unknown at ±4%
    assert b["cells"]["depth_200"]["kraken"]["status"] == "ok"
    assert b["cells"]["on_target"]["gate"]["status"] == ""


def test_a_re_strike_costs_what_it_adds_not_what_it_holds():
    from defi_tracker.analytics import bid_cost, bs_call

    sd, spot = 0.05, 0.04
    sig = sd * 365**0.5
    old = [{"peaq": 1_000_000, "strike_usd": 0.40, "months": 12}]
    new = [{"peaq": 1_000_000, "strike_usd": 0.03, "months": 12}]
    c = bid_cost({"options": new, "replaces": old, "fee_usd_year": 0}, spot, sd, 90, 0.5, None)
    expect = 1_000_000 * (bs_call(spot, 0.03, sig, 1.0) - bs_call(spot, 0.40, sig, 1.0))
    assert abs(c["options_usd"] - expect) < 1e-6 and c["fee_usd"] == 0.0
    assert c["options_usd"] > 1_000_000 * (spot - 0.03)  # at least its day-one intrinsic


# ── public order books, "now", live markets ───────────────────────────────


def test_book_depth_sums_both_sides_within_the_band_and_flags_a_truncated_book():
    from defi_tracker.orderbooks import book_stats

    bids = [(0.99, 100.0), (0.985, 100.0), (0.97, 1000.0)]  # 0.97 is outside ±2% of mid 1.0
    asks = [(1.01, 100.0), (1.015, 100.0)]  # book stops inside +2%: truncated
    s = book_stats(bids, asks)
    assert round(s["spread_pct"], 6) == 2.0 and round(s["mid"], 6) == 1.0
    assert round(s["d1"], 2) == round(0.99 * 100 + 1.01 * 100, 2)
    assert round(s["d2"], 2) == round(0.99 * 100 + 0.985 * 100 + 1.01 * 100 + 1.015 * 100, 2)
    assert "d2" in s["truncated"]
    assert book_stats([], asks) == {"status": "empty"}


def test_now_is_the_last_seven_days_so_a_venue_left_recently_counts_as_left():
    rows = [_obs("mm-a", "bitget", f"2026-10-{d:02d}", "depth_200", 30_000.0 if d < 9 else 0.0, 1.0)
            for d in range(1, 16)]  # fmt: skip
    m = mm_delivery(rows, [], window_days=15)
    c = m["blocks"][0]["cells"]["depth_200"]["bitget"]
    assert c["v7"] == 0.0 and c["v90"] == 30_000.0  # 8 of 15 days at $30k: median still $30k
    assert m["last7"] == ("2026-10-09", "2026-10-15")


def _rfq_page(rfq, rows, books=None):
    from decimal import Decimal

    from defi_tracker.analytics import PeaqMark
    from defi_tracker.report import ReportData
    from defi_tracker.web import render_dashboard
    from tests.test_web import _snap

    mark = PeaqMark(price=Decimal("0.04"), ts=0, source="market", pool_price=None, pool_ts=None,
                    market_price=Decimal("0.04"), market_ts=0)  # fmt: skip
    out = render_dashboard(ReportData(snaps=[_snap()], rfq=rfq, delivery=rows, peaq=mark,
                                      peaq_sigma=(0.05, 90, "x"), orderbooks=books or {}))  # fmt: skip
    return out.split('id="rfq"', 1)[1].split("</section>", 1)[0]


def test_the_rfq_table_reads_today_to_offer_with_no_prose():
    rows = [_obs("mm-a", "gate", "2026-10-15", "depth_200", 20_000.0, 50_000.0)]
    rfq = {"carry_rate_pct": 0.5, "target": [{"mm": "MM-A", "venues": ["gate"], "depth_2pct_usd": 10_000}],
           "bid": [{"name": "Renewal", "incumbent": "MM-A", "replaces_peaq": 1_000_000, "loan_peaq": 1_500_000,
                    "tenor_months": 6,
                    "option_today": "1m @ $0.06", "option_offer": "1.5m @ 150%",
                    "options": [{"share": 1, "strike_pct": 150}],
                    "depth": [{"venues": ["gate"], "d2": 40_000}, {"venues": ["dead perp"], "d2": 50_000},
                              {"venues": ["bybit perp"], "d2": 5_000}]},
                   {"name": "Ask", "incumbent": "MM-A", "keeps_peaq": 2_000_000, "loan_peaq": 500_000,
                    "fee_usd_year": 0, "depth": [],
                    "options": [{"peaq": 1_000_000, "strike_usd": 0.02, "months": 12}]}],
           "pending": [{"name": "Later", "note": "Quote due"}], "circulating_supply": 100_000_000, "fdv_supply": 200_000_000,
           "variant": [{"of": "Renewal", "name": "Half", "loan_peaq": 750_000}]}  # fmt: skip
    books = {"dead perp": {"kind": "perp", "status": "not listed"},
             "bybit perp": {"kind": "perp", "status": "ok", "d2": 9_000.0}}  # fmt: skip
    tab = _rfq_page(rfq, rows, books)
    t = tab.split("1 · The RFQ", 1)[1].split("2 · How today", 1)[0]
    assert "1.00m</span> → <strong>1.50m</strong>" in t  # replaces today's loan
    assert "2.00m</span> → <strong>2.50m</strong>" in t  # keeps today's, adds new
    assert "1m @ $0.06</span> → <strong>1.5m @ 150%</strong>" in t
    assert "$20.0k</span> → <strong>$40.0k</strong>" in t and "+$5.0k on live perp markets" in t
    assert "none in $" in t and "on depth it delivers now" in t and "Quote due" in t
    assert "Watch out" not in t
    assert " for 6 mo<small>≈ " in t
    assert "0.50%</span> → <strong>0.75%</strong>" in t  # same loan against the FDV supply
    assert (
        "1.00%</span> → <strong>1.50%</strong><small>+500,000" in t
        or "1.00%</span> → <strong>1.50%</strong>" in t
    )  # a 6-month bid shows its term cost, annualised beneath
    assert "Other structures and their cost" in tab and "Renewal: other structures" in tab
    assert "<strong>Half</strong>" in tab


def test_a_variant_with_its_own_depth_shows_depth_per_dollar_lent():
    """Loan utilisation as the MMs' sheets compute it: ±2% depth (spot and
    perp) ÷ PEAQ lent in $. 1m PEAQ at $0.04 = $40k lent; $10k + $10k perp
    as quoted = 50%; a variant asking $30k + $10k = 100%."""
    rows = [_obs("mm-a", "gate", "2026-10-15", "depth_200", 20_000.0, 50_000.0)]
    rfq = {"carry_rate_pct": 0.5,
           "bid": [{"name": "Renewal", "incumbent": "MM-A", "loan_peaq": 1_000_000, "tenor_months": 6,
                    "options": [{"share": 1, "strike_pct": 150}],
                    "depth": [{"venues": ["gate"], "d2": 10_000}, {"venues": ["bybit perp"], "d2": 10_000}]}],
           "variant": [{"of": "Renewal", "name": "Same"},
                       {"of": "Renewal", "name": "More depth",
                        "depth": [{"venues": ["gate"], "d2": 30_000}, {"venues": ["bybit perp"], "d2": 10_000}]}]}  # fmt: skip
    tab = _rfq_page(rfq, rows)
    v = tab.split("Renewal: other structures", 1)[1]
    assert "Depth ÷ loan" in v
    assert "<td>50%<small>$20.0k ±2%</small>" in v and "<td>100%<small>$40.0k ±2%</small>" in v


def test_a_put_on_stables_lent_is_priced_as_a_put_the_desk_writes():
    """A strangle bid: $500 of PEAQ and $500 of stables lent, a call on the
    PEAQ and a put letting the MM repay the stables in PEAQ at the strike.
    The put is priced by parity (call − spot + strike) on usd/strike tokens;
    an at-the-money put is worth as much as the at-the-money call."""
    from defi_tracker.analytics import bid_cost, bs_call

    call = {"usd": 500, "strike_pct": 100}
    put = {"type": "put", "usd": 500, "strike_pct": 100}
    base = {"loan_usd": 500, "stables_lent_usd": 500, "tenor_months": 12}
    c = bid_cost({**base, "options": [call]}, 1.0, 0.05, 365, None, None)
    p = bid_cost({**base, "options": [put]}, 1.0, 0.05, 365, None, None)
    assert abs(c["options_usd"] - p["options_usd"]) < 1e-9
    otm = bid_cost({**base, "options": [{"type": "put", "usd": 500, "strike_pct": 80}]},
                   1.0, 0.05, 365, None, None)  # fmt: skip
    sig = 0.05 * 365**0.5
    assert abs(otm["options_usd"] - 500 / 0.8 * (bs_call(1.0, 0.8, sig, 1.0) - 1.0 + 0.8)) < 1e-9


def test_depth_per_dollar_lent_counts_the_stables_too():
    rows = [_obs("mm-a", "gate", "2026-10-15", "depth_200", 20_000.0, 50_000.0)]
    rfq = {"carry_rate_pct": 0.5,
           "bid": [{"name": "Renewal", "incumbent": "MM-A", "loan_peaq": 1_000_000, "tenor_months": 12,
                    "options": [{"share": 1, "strike_pct": 150}], "depth": [{"venues": ["gate"], "d2": 20_000}]}],
           "variant": [{"of": "Renewal", "name": "Strangle", "loan_peaq": 500_000, "stables_lent_usd": 20_000,
                        "options": [{"share": 1, "strike_pct": 150},
                                    {"type": "put", "usd": 20_000, "strike_pct": 90}]}]}  # fmt: skip
    v = _rfq_page(rfq, rows).split("Renewal: other structures", 1)[1]
    assert "<td>50%<small>$20.0k ±2%</small>" in v  # $20k ÷ ($20k PEAQ + $20k stables)


def test_a_decision_card_is_the_call_the_deadline_and_what_to_request_and_check():
    rfq = {"seat": [{"seat": "MM-A", "incumbent": "MM-A", "exit_by": "2099-01-01", "exit_how": "notice",
                     "call": "Renegotiate", "why": "One line.", "request": ["Ask A"], "check": ["Check B"]}]}  # fmt: skip
    tab = _rfq_page(rfq, [_obs("mm-a", "gate", "2026-10-15", "depth_200", 1.0, 1.0)])
    seat = tab.split("3 · Decisions", 1)[1]
    assert '<div class="call">Renegotiate</div>' in seat and "2099-01-01" in seat
    assert "<dd>One line.</dd>" in seat and "<li>Ask A</li>" in seat and "<li>Check B</li>" in seat
