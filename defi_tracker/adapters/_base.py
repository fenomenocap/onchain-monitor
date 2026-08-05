"""
BaseAdapter — shared scaffolding for all protocol adapters.

Concrete adapters subclass this and implement only:
  - info (property)
  - fetch_events()
  - fetch_positions()

Everything else (pricing, RPC, subgraph queries) is provided here so each
new adapter file is a one-file job with no boilerplate.
"""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal

import requests

from defi_tracker.adapters._pricing import _CG_PLATFORM, PriceFetcher
from defi_tracker.adapters._rpc import RpcClient
from defi_tracker.core.adapter import ProtocolAdapter
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import PricePoint, Token

_log = logging.getLogger(__name__)


class BaseAdapter(ProtocolAdapter):
    """
    Shared implementation base for all protocol adapters.

    Subclasses must call super().__init__(db_path, rpc_url, stablecoins)
    in their own __init__. Concrete adapters only implement info, fetch_events(),
    and fetch_positions().
    """

    def __init__(
        self,
        db_path: str = "",
        rpc_url: str = "",
        stablecoins: dict[str, Decimal] | None = None,
        cg_coin_ids: dict[str, str] | None = None,
    ):
        resolved_db = db_path or os.getenv("TRACKER_DB", "tracker.db")
        self._stablecoins: dict[str, Decimal] = stablecoins or {}
        self._cg_coin_ids: dict[str, str] = {k.lower(): v for k, v in (cg_coin_ids or {}).items()}
        self._pricer = PriceFetcher(
            resolved_db, stablecoins=self._stablecoins, coin_ids=self._cg_coin_ids
        )
        self._storage = Storage(resolved_db)
        self._rpc = RpcClient(rpc_url) if rpc_url else None
        self._cg_key = os.getenv("COINGECKO_API_KEY")
        self._current_price_cache: dict[str, Decimal] = {}

    # ── Pricing ───────────────────────────────────────────────────────────

    def get_current_price(self, token: Token) -> PricePoint | None:
        """Single-token current price via CoinGecko simple/token_price endpoint."""
        addr = token.address.lower()
        if addr in self._stablecoins:
            return PricePoint(token.key, self._stablecoins[addr], int(time.time()), "stable")
        if addr in self._current_price_cache:
            return PricePoint(
                token.key, self._current_price_cache[addr], int(time.time()), "cached"
            )
        platform = _CG_PLATFORM.get(token.chain)
        if not platform:
            return None
        url = (
            f"https://api.coingecko.com/api/v3/simple/token_price/{platform}"
            f"?contract_addresses={addr}&vs_currencies=usd"
        )
        try:
            headers = {"x-cg-demo-api-key": self._cg_key} if self._cg_key else {}
            r = requests.get(url, timeout=15, headers=headers)
            r.raise_for_status()
            data = r.json()
            if addr in data and "usd" in data[addr]:
                price = Decimal(str(data[addr]["usd"]))
                self._current_price_cache[addr] = price
                return PricePoint(token.key, price, int(time.time()), "coingecko")
        except Exception as e:
            _log.warning("CoinGecko price fetch failed for %s: %s", token.address, e)
        # Fallback: coin-ID lookup for tokens not indexed by contract address on this chain.
        # Check hardcoded map first, then the DB (populated via `set-cg-id` CLI command).
        coin_id = self._cg_coin_ids.get(addr)
        if not coin_id:
            with self._storage.connect() as conn:
                row = conn.execute(
                    "SELECT coingecko_id FROM tokens WHERE chain=? AND address=?",
                    (token.chain.value, addr),
                ).fetchone()
            if row and row["coingecko_id"]:
                coin_id = str(row["coingecko_id"])
        if coin_id:
            try:
                headers = {"x-cg-demo-api-key": self._cg_key} if self._cg_key else {}
                r = requests.get(
                    f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd",
                    timeout=15,
                    headers=headers,
                )
                r.raise_for_status()
                data = r.json()
                if coin_id in data and "usd" in data[coin_id]:
                    price = Decimal(str(data[coin_id]["usd"]))
                    self._current_price_cache[addr] = price
                    return PricePoint(token.key, price, int(time.time()), "coingecko_id")
            except Exception as e:
                _log.warning(
                    "CoinGecko coin-ID price fetch failed for %s (%s): %s",
                    coin_id,
                    token.address,
                    e,
                )
        return None

    def get_historical_price(self, token: Token, ts: int) -> PricePoint | None:
        """Historical price via PriceFetcher (CoinGecko history endpoint + SQLite cache)."""
        price = self._pricer.get(token, ts)
        if price is None:
            return None
        return PricePoint(token.key, price, ts, "coingecko_history")

    # ── Pool-ratio price fallback ─────────────────────────────────────────

    @staticmethod
    def price_from_sqrt(
        sqrt_price_x96: int,
        decimals0: int,
        decimals1: int,
        price1: Decimal,
    ) -> Decimal | None:
        """
        Derive token0's USD price from the pool's sqrtPriceX96 and token1's known price.

        Used as a last-resort fallback when CoinGecko doesn't cover a token but the pool
        pairs it with a token whose price is known (e.g. a stablecoin or WETH).

        sqrtPriceX96 encodes raw token1-per-token0; adjusting for decimals gives
        the human exchange rate, so:

            ratio  = (sqrtP / 2^96)^2 * 10^(decimals0 - decimals1)   # token1 per token0
            price0 = ratio * price1
        """
        if sqrt_price_x96 <= 0:
            return None
        try:
            ratio = (Decimal(sqrt_price_x96) / Decimal(2**96)) ** 2 * Decimal(
                10 ** (decimals0 - decimals1)
            )
            return ratio * price1 if ratio > 0 else None
        except Exception:
            return None

    # ── Subgraph ──────────────────────────────────────────────────────────

    _INDEXER_MAX_RETRIES = 3
    _INDEXER_RETRY_SLEEP = 2.0

    def _run_subgraph_query(
        self, query: str, url: str, max_retries: int | None = None
    ) -> dict | None:
        """POST a GraphQL query to url. Raises RuntimeError on 401/403.

        Retries up to max_retries times (default _INDEXER_MAX_RETRIES) when
        The Graph returns a 'bad indexers' error (transient routing to
        unhealthy indexers). Pass max_retries=1 for a single attempt — used
        for best-effort queries like historical block prices where the error
        is usually permanent (pruned block), not transient.
        Schema errors ('has no field', 'unknown type') are not retried.
        """
        retries = max_retries if max_retries is not None else self._INDEXER_MAX_RETRIES
        for attempt in range(retries):
            try:
                r = requests.post(url, json={"query": query}, timeout=30)
                if r.status_code in (401, 403):
                    raise RuntimeError(
                        f"Subgraph auth failed (HTTP {r.status_code}) — check API key"
                    )
                r.raise_for_status()
                j = r.json()
                if "errors" in j:
                    msgs = [e.get("message", "") for e in j["errors"]]
                    if any("bad indexers" in m for m in msgs) and attempt < retries - 1:
                        _log.warning(
                            "Subgraph bad indexers (attempt %d/%d), retrying in %.0fs…",
                            attempt + 1,
                            retries,
                            self._INDEXER_RETRY_SLEEP,
                        )
                        time.sleep(self._INDEXER_RETRY_SLEEP)
                        continue
                    _log.warning("Subgraph errors: %s", j["errors"])
                    return None
                data = j.get("data")
                return data if isinstance(data, dict) else None
            except RuntimeError:
                raise
            except Exception as e:
                _log.warning("Subgraph query failed: %s", e)
                return None
        return None
