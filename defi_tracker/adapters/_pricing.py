"""
Shared historical price fetcher.

All adapters use PriceFetcher instead of rolling their own CoinGecko calls.
Cache key: (chain, address, date) so we make at most one HTTP call per token per day.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, date, datetime
from decimal import Decimal

import requests

from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, Token

_log = logging.getLogger(__name__)

_CG_PLATFORM: dict[Chain, str] = {
    Chain.BSC: "binance-smart-chain",
    Chain.ETHEREUM: "ethereum",
    Chain.ARBITRUM: "arbitrum-one",
    Chain.OPTIMISM: "optimistic-ethereum",
    Chain.BASE: "base",
    Chain.POLYGON: "polygon-pos",
    Chain.AVALANCHE: "avalanche",
    Chain.PEAQ: "peaq",  # best-effort; CoinGecko peaq coverage is limited
}


class PriceFetcher:
    """
    Historical USD price lookup backed by CoinGecko + SQLite cache.

    Flow per .get(token, ts) call:
      1. Stablecoin map → Decimal("1.0") immediately
      2. SQLite price_cache hit → return cached value
      3. Resolve CoinGecko coin ID (from tokens table or via contract lookup)
      4. Fetch /coins/{id}/history?date={DD-MM-YYYY}
      5. Cache result → return
      6. On 404 / missing data → return None, log warning, do not raise
    """

    def __init__(
        self,
        db_path: str,
        stablecoins: dict[str, Decimal] | None = None,
        coin_ids: dict[str, str] | None = None,
    ):
        self._storage = Storage(db_path)
        self._stablecoins: dict[str, Decimal] = stablecoins or {}
        self._coin_ids: dict[str, str] = {k.lower(): v for k, v in (coin_ids or {}).items()}
        # Addresses CoinGecko confirmed it has no coin ID for (this run) —
        # the tokens table can't distinguish "unknown" from "known missing".
        self._cg_id_missing: set[str] = set()
        self._cg_key = os.getenv("COINGECKO_API_KEY")

    def get(self, token: Token, ts: int) -> Decimal | None:
        """Return USD price for token at unix timestamp ts, or None if unavailable."""
        addr = token.address.lower()

        # 1. Stablecoin shortcut — no network call needed
        if addr in self._stablecoins:
            return self._stablecoins[addr]

        platform = _CG_PLATFORM.get(token.chain)
        if not platform:
            _log.warning("No CoinGecko platform mapping for chain %s", token.chain)
            return None

        d = datetime.fromtimestamp(ts, tz=UTC).date()

        # 2. SQLite cache hit
        cached = self._storage.cached_price(token.chain, addr, d)
        if cached is not None:
            return cached

        # 3. Resolve CoinGecko coin ID
        cg_id = self._resolve_cg_id(token, platform)
        if not cg_id:
            return None

        # 4. Fetch historical price
        price = self._fetch_history(cg_id, d)
        if price is None:
            return None

        # 5. Cache and return
        self._storage.cache_price(token.chain, addr, d, price, "coingecko_history")
        return price

    # ── Internal helpers ──────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"x-cg-demo-api-key": self._cg_key} if self._cg_key else {}

    def _resolve_cg_id(self, token: Token, platform: str) -> str | None:
        """Return CoinGecko coin ID for this token, fetching and caching if unknown."""
        addr = token.address.lower()
        injected = self._coin_ids.get(addr)
        if injected:
            return injected
        if addr in self._cg_id_missing:
            return None
        with self._storage.connect() as conn:
            row = conn.execute(
                "SELECT coingecko_id FROM tokens WHERE chain=? AND address=?",
                (token.chain.value, addr),
            ).fetchone()
        if row and row["coingecko_id"]:
            return str(row["coingecko_id"])

        url = f"https://api.coingecko.com/api/v3/coins/{platform}/contract/{addr}"
        try:
            r = self._get_with_retry(url)
            if r is None:
                return None
            if r.status_code == 404:
                _log.warning("CoinGecko: no coin ID for %s on %s", token.address, platform)
                self._cg_id_missing.add(addr)
                return None
            r.raise_for_status()
            cg_id: str | None = r.json().get("id")
            if cg_id:
                self._storage.upsert_token(token, coingecko_id=cg_id)
            time.sleep(2)
            return cg_id
        except Exception as e:
            _log.warning("CoinGecko contract lookup failed for %s: %s", token.address, e)
            return None

    def _fetch_history(self, cg_id: str, d: date) -> Decimal | None:
        date_str = d.strftime("%d-%m-%Y")
        url = (
            f"https://api.coingecko.com/api/v3/coins/{cg_id}/history"
            f"?date={date_str}&localization=false"
        )
        try:
            r = self._get_with_retry(url)
            if r is None:
                return None
            if r.status_code == 404:
                _log.warning("CoinGecko history: 404 for %s on %s", cg_id, date_str)
                return None
            r.raise_for_status()
            j = r.json()
            usd = (j.get("market_data") or {}).get("current_price", {}).get("usd")
            if usd is None:
                _log.warning("CoinGecko history: no USD price for %s on %s", cg_id, date_str)
                return None
            time.sleep(2)
            return Decimal(str(usd))
        except Exception as e:
            _log.warning("CoinGecko history fetch failed for %s: %s", cg_id, e)
            return None

    def _get_with_retry(self, url: str, max_attempts: int = 3) -> requests.Response | None:
        """GET with exponential backoff on 429 / 5xx."""
        for attempt in range(max_attempts):
            try:
                r = requests.get(url, timeout=15, headers=self._headers())
                if r.status_code == 429 or r.status_code >= 500:
                    wait = 10 * (2**attempt)
                    _log.warning(
                        "CoinGecko HTTP %s — retrying in %ds (attempt %d/%d)",
                        r.status_code,
                        wait,
                        attempt + 1,
                        max_attempts,
                    )
                    time.sleep(wait)
                    continue
                return r
            except Exception as e:
                if attempt == max_attempts - 1:
                    _log.warning("CoinGecko request failed after %d attempts: %s", max_attempts, e)
                    return None
                time.sleep(5 * (2**attempt))
        _log.warning("CoinGecko request failed after %d attempts (rate limited)", max_attempts)
        return None
