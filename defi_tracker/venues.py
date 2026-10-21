"""Where PEAQ trades: CoinGecko's tickers, aggregated per exchange.

Read at page build (like today's prices) and handed to the report — the
analytics layer never fetches. Each row carries 24h volume and the order
book's ±2% depth as CoinGecko reports it; a failed read is an empty list,
which the page shows as "not read", never as "no venues".
"""

from __future__ import annotations

import os

import requests

PEAQ_COINGECKO_ID = "peaq-2"


def fetch_venues(coin_id: str = PEAQ_COINGECKO_ID, timeout: float = 20.0) -> list[dict]:
    """[{venue, pairs, volume_usd, depth_up_usd, depth_down_usd, spread_pct}], one per exchange."""
    headers = {"x-cg-demo-api-key": k} if (k := os.getenv("COINGECKO_API_KEY")) else {}
    agg: dict[str, dict] = {}
    try:
        for page in (1, 2, 3):
            r = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/tickers",
                params={"page": page, "depth": "true"},
                headers=headers,
                timeout=timeout,
            )
            r.raise_for_status()
            tickers = r.json().get("tickers", [])
            if not tickers:
                break
            for t in tickers:
                name = t["market"]["name"]
                row = agg.setdefault(
                    name,
                    {
                        "venue": name,
                        "pairs": [],
                        "volume_usd": 0.0,
                        "depth_up_usd": 0.0,
                        "depth_down_usd": 0.0,
                        "spread_pct": None,
                    },  # fmt: skip
                )
                row["pairs"].append(f"{t.get('base', '')}/{t.get('target', '')}")
                row["volume_usd"] += float((t.get("converted_volume") or {}).get("usd") or 0)
                row["depth_up_usd"] += float(t.get("cost_to_move_up_usd") or 0)
                row["depth_down_usd"] += float(t.get("cost_to_move_down_usd") or 0)
                if row["spread_pct"] is None and t.get("bid_ask_spread_percentage") is not None:
                    row["spread_pct"] = float(t["bid_ask_spread_percentage"])
    except (requests.RequestException, ValueError, KeyError):
        return []
    return sorted(agg.values(), key=lambda r: -r["volume_usd"])
