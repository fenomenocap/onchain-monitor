"""
Tests for PriceFetcher (_pricing.py).

All network calls are mocked — no real HTTP requests made.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from defi_tracker.adapters._pricing import PriceFetcher
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, Token

# ── Test constants ────────────────────────────────────────────────────────

USDT_BSC = Token(
    chain=Chain.BSC,
    address="0x55d398326f99059ff775485246999027b3197955",
    symbol="USDT",
    decimals=18,
)
WETH_BSC = Token(
    chain=Chain.BSC,
    address="0x2170ed0880ac9a755fd29b2688956bd959f933f8",
    symbol="ETH",
    decimals=18,
)
STABLECOINS = {USDT_BSC.address: Decimal("1.0")}
TS_2024 = 1714521600  # 2024-05-01 00:00:00 UTC


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    s = Storage(tmp_path / "test.db")
    s.init_schema()
    return str(tmp_path / "test.db")


@pytest.fixture
def pricer(db_path: str) -> PriceFetcher:
    return PriceFetcher(db_path, stablecoins=STABLECOINS)


# ── Tests ─────────────────────────────────────────────────────────────────


def test_stablecoin_returns_one_immediately(pricer: PriceFetcher):
    """Stablecoin address → Decimal("1.0") with no HTTP calls."""
    with patch("requests.get") as mock_get:
        price = pricer.get(USDT_BSC, TS_2024)
    assert price == Decimal("1.0")
    mock_get.assert_not_called()


def test_cache_hit_skips_http(pricer: PriceFetcher, db_path: str):
    """A cached price in SQLite is returned without any HTTP call."""
    s = Storage(db_path)
    s.cache_price(Chain.BSC, WETH_BSC.address, date(2024, 5, 1), Decimal("3100"), "test")

    with patch("requests.get") as mock_get:
        price = pricer.get(WETH_BSC, TS_2024)

    assert price == Decimal("3100")
    mock_get.assert_not_called()


def test_cache_miss_resolves_id_fetches_history_and_caches(pricer: PriceFetcher, db_path: str):
    """Cache miss: resolves CoinGecko ID, fetches history, writes to cache, returns Decimal."""
    contract_resp = MagicMock()
    contract_resp.status_code = 200
    contract_resp.json.return_value = {"id": "ethereum"}

    history_resp = MagicMock()
    history_resp.status_code = 200
    history_resp.json.return_value = {"market_data": {"current_price": {"usd": 3000.0}}}

    with patch("requests.get", side_effect=[contract_resp, history_resp]) as mock_get:
        price = pricer.get(WETH_BSC, TS_2024)

    assert price == Decimal("3000.0")
    assert mock_get.call_count == 2

    # Verify it was written to the SQLite cache
    s = Storage(db_path)
    cached = s.cached_price(Chain.BSC, WETH_BSC.address, date(2024, 5, 1))
    assert cached == Decimal("3000.0")


def test_cache_miss_uses_stored_coingecko_id(pricer: PriceFetcher, db_path: str):
    """If tokens table already has coingecko_id, skip contract lookup."""
    s = Storage(db_path)
    s.upsert_token(WETH_BSC, coingecko_id="ethereum")

    history_resp = MagicMock()
    history_resp.status_code = 200
    history_resp.json.return_value = {"market_data": {"current_price": {"usd": 3200.0}}}

    with patch("requests.get", return_value=history_resp) as mock_get:
        price = pricer.get(WETH_BSC, TS_2024)

    assert price == Decimal("3200.0")
    # Only one call — history fetch, no contract lookup
    assert mock_get.call_count == 1


def test_404_on_contract_lookup_returns_none(pricer: PriceFetcher):
    """CoinGecko 404 on contract lookup → returns None, does not raise."""
    not_found = MagicMock()
    not_found.status_code = 404

    with patch("requests.get", return_value=not_found):
        price = pricer.get(WETH_BSC, TS_2024)

    assert price is None


def test_404_on_history_returns_none(pricer: PriceFetcher, db_path: str):
    """CoinGecko 404 on history fetch → returns None, does not raise."""
    s = Storage(db_path)
    s.upsert_token(WETH_BSC, coingecko_id="ethereum")

    not_found = MagicMock()
    not_found.status_code = 404

    with patch("requests.get", return_value=not_found):
        price = pricer.get(WETH_BSC, TS_2024)

    assert price is None


def test_missing_market_data_returns_none(pricer: PriceFetcher, db_path: str):
    """CoinGecko history response with no market_data → returns None, does not raise."""
    s = Storage(db_path)
    s.upsert_token(WETH_BSC, coingecko_id="ethereum")

    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.json.return_value = {}  # no market_data key

    with patch("requests.get", return_value=empty_resp):
        price = pricer.get(WETH_BSC, TS_2024)

    assert price is None


def test_injected_coin_id_map_skips_contract_lookup(db_path: str):
    """A coin_ids entry resolves the CoinGecko ID without the contract-lookup HTTP call."""
    pricer = PriceFetcher(db_path, coin_ids={WETH_BSC.address: "ethereum"})

    history_resp = MagicMock()
    history_resp.status_code = 200
    history_resp.json.return_value = {"market_data": {"current_price": {"usd": 3300.0}}}

    with patch("requests.get", return_value=history_resp) as mock_get:
        price = pricer.get(WETH_BSC, TS_2024)

    assert price == Decimal("3300.0")
    assert mock_get.call_count == 1  # history fetch only, no contract lookup


def test_contract_404_memoized_within_run(pricer: PriceFetcher):
    """A confirmed 'no coin ID' 404 is not re-asked for later timestamps in the same run."""
    not_found = MagicMock()
    not_found.status_code = 404

    with patch("requests.get", return_value=not_found) as mock_get:
        assert pricer.get(WETH_BSC, TS_2024) is None
        assert pricer.get(WETH_BSC, TS_2024 + 86400) is None  # next day, same token

    assert mock_get.call_count == 1


def test_integration_two_events_correct_cost_basis(db_path: str):
    """
    Integration: deposit event at $3000/ETH + another at $3200/ETH.
    PriceFetcher returns different prices per date; cost basis adds up correctly.
    """
    # Two events at two different timestamps
    ts1 = 1714521600  # 2024-05-01
    ts2 = 1714608000  # 2024-05-02
    s = Storage(db_path)
    s.upsert_token(WETH_BSC, coingecko_id="ethereum")

    def fake_history(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "01-05-2024" in url:
            resp.json.return_value = {"market_data": {"current_price": {"usd": 3000.0}}}
        else:
            resp.json.return_value = {"market_data": {"current_price": {"usd": 3200.0}}}
        return resp

    pricer = PriceFetcher(db_path)

    with patch("requests.get", side_effect=fake_history):
        p1 = pricer.get(WETH_BSC, ts1)
        p2 = pricer.get(WETH_BSC, ts2)

    assert p1 == Decimal("3000.0")
    assert p2 == Decimal("3200.0")
    # Simulated cost basis for 1 ETH deposited each day
    cost_basis = Decimal("1") * p1 + Decimal("1") * p2
    assert cost_basis == Decimal("6200.0")


# ── price_from_sqrt direction ──────────────────────────────────────────────
# Regression: the ratio was inverted (price1 / ratio), deriving wildly wrong
# fallback prices. sqrtPriceX96 encodes raw token1-per-token0, so
# price0 = ratio * price1. Verified against the live USDC/PEAQ Infinity pool.


def test_price_from_sqrt_direction():
    from defi_tracker.adapters._base import BaseAdapter

    # Live BSC USDC/PEAQ pool reading (2026-07-13): ratio ≈ 56.6 PEAQ per USDC.
    sqrt_price = 596194136248885487518314408226
    peaq_usd = Decimal("0.0177")
    usdc_usd = BaseAdapter.price_from_sqrt(sqrt_price, 18, 18, peaq_usd)
    assert usdc_usd is not None
    assert Decimal("0.9") < usdc_usd < Decimal("1.1")  # a stablecoin, ~$1


def test_price_from_sqrt_equal_price_pool():
    from defi_tracker.adapters._base import BaseAdapter

    # sqrtP = 2^96 → 1:1 raw ratio; equal decimals → price0 == price1
    p = BaseAdapter.price_from_sqrt(2**96, 18, 18, Decimal("42"))
    assert p == Decimal("42")


def test_price_from_sqrt_invalid_inputs():
    from defi_tracker.adapters._base import BaseAdapter

    assert BaseAdapter.price_from_sqrt(0, 18, 18, Decimal("1")) is None
    assert BaseAdapter.price_from_sqrt(-5, 18, 18, Decimal("1")) is None
