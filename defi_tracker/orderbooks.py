"""PEAQ's order books straight from each exchange's public API, read at build.

Coinwatch tells us which share of a book is each MM's; it cannot be checked
against itself. These snapshots are the independent check: the whole book's
±0.5/1/2% depth and spread as the exchange publishes it, no key, no vendor.
A venue whose market does not exist (an invalid symbol) is reported as not
listed; an empty book as no live book; a failed read as None — never zero.
"""

from __future__ import annotations

import requests

# (venue key, kind, url, parser key). Venue keys match analytics._venue_key.
BOOKS: tuple[tuple[str, str, str, str], ...] = (
    ("gate", "spot", "https://api.gateio.ws/api/v4/spot/order_book?currency_pair=PEAQ_USDT&limit=1000", "gate"),
    ("kucoin", "spot", "https://api.kucoin.com/api/v1/market/orderbook/level2_100?symbol=PEAQ-USDT", "kucoin"),
    ("kraken", "spot", "https://api.kraken.com/0/public/Depth?pair=PEAQUSD&count=500", "kraken"),
    ("mexc", "spot", "https://api.mexc.com/api/v3/depth?symbol=PEAQUSDT&limit=1000", "plain"),
    ("bitget", "spot", "https://api.bitget.com/api/v2/spot/market/orderbook?symbol=PEAQUSDT&type=step0&limit=150", "bitget"),
    ("crypto.com", "spot", "https://api.crypto.com/exchange/v1/public/get-book?instrument_name=PEAQ_USD&depth=150", "cdc"),
    ("binance", "spot", "https://api.binance.com/api/v3/depth?symbol=PEAQUSDT&limit=1000", "plain"),
    ("binance perp", "perp", "https://fapi.binance.com/fapi/v1/depth?symbol=PEAQUSDT&limit=1000", "plain"),
    ("bybit perp", "perp", "https://api.bybit.com/v5/market/orderbook?category=linear&symbol=PEAQUSDT&limit=500", "bybit"),
    ("gate perp", "perp", "https://api.gateio.ws/api/v4/futures/usdt/order_book?contract=PEAQ_USDT&limit=300", "gate_perp"),
)  # fmt: skip

_GATE_PERP_CONTRACT = "https://api.gateio.ws/api/v4/futures/usdt/contracts/PEAQ_USDT"


def _levels(raw: list) -> list[tuple[float, float]]:
    out = []
    for lv in raw:
        if isinstance(lv, dict):
            out.append((float(lv["p"]), float(lv["s"])))
        else:
            out.append((float(lv[0]), float(lv[1])))
    return out


def _parse(kind: str, j: dict) -> tuple[list, list]:
    if kind == "kucoin" or kind == "bitget":
        d = j["data"]
    elif kind == "kraken":
        d = next(iter(j["result"].values()))
    elif kind == "cdc":
        d = j["result"]["data"][0]
    elif kind == "bybit":
        d = {"bids": j["result"]["b"], "asks": j["result"]["a"]}
    else:
        d = j
    return _levels(d.get("bids") or []), _levels(d.get("asks") or [])


def book_stats(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> dict:
    """Mid, spread % and USD depth within ±0.5/1/2% of mid, both sides
    summed (the convention the MM contracts and Coinwatch use). A band the
    returned book does not reach is flagged `truncated` — a floor, not a total."""
    if not bids or not asks:
        return {"status": "empty"}
    bid, ask = max(p for p, _ in bids), min(p for p, _ in asks)
    mid = (bid + ask) / 2
    out: dict = {"status": "ok", "mid": mid, "spread_pct": (ask - bid) / mid * 100, "truncated": []}
    for band, key in ((0.005, "d05"), (0.01, "d1"), (0.02, "d2")):
        lo, hi = mid * (1 - band), mid * (1 + band)
        out[key] = sum(p * q for p, q in bids if p >= lo) + sum(p * q for p, q in asks if p <= hi)
        if min(p for p, _ in bids) > lo or max(p for p, _ in asks) < hi:
            out["truncated"].append(key)
    return out


def fetch_orderbooks(timeout: float = 15.0) -> dict[str, dict]:
    """{venue: {kind, status, mid, spread_pct, d05, d1, d2, truncated}}.
    status: ok · empty (market exists, nothing quoted) · not listed · error."""
    out: dict[str, dict] = {}
    mult = 1.0
    for venue, kind, url, parser in BOOKS:
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 400 and "symbol" in r.text.lower():
                out[venue] = {"kind": kind, "status": "not listed"}
                continue
            r.raise_for_status()
            bids, asks = _parse(parser, r.json())
            if parser == "gate_perp" and (bids or asks):
                c = requests.get(_GATE_PERP_CONTRACT, timeout=timeout).json()
                mult = float(c.get("quanto_multiplier") or 1)
                bids = [(p, q * mult) for p, q in bids]
                asks = [(p, q * mult) for p, q in asks]
            out[venue] = {"kind": kind, **book_stats(bids, asks)}
        except (requests.RequestException, ValueError, KeyError, IndexError, StopIteration):
            out[venue] = {"kind": kind, "status": "error"}
    return out
