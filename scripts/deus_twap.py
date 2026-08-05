"""
Fetch daily DEUS prices from the MachineX subgraph and compute TWAP over an exact date range.

Usage:
    python scripts/deus_twap.py                                        # 14D rolling window
    python scripts/deus_twap.py --start 2026-05-27 --end 2026-06-10   # exact campaign window

Requires GRAPH_API_KEY in .env (same key used by the main tracker).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import requests
from dotenv import load_dotenv

load_dotenv()

DEUS_ADDRESS = "0x940a319b75861014a220d9c6c144d108552b089b"
MACHINEX_SUBGRAPH_ID = "EAVSLJ9r1mc18RmDXFHUxzH1QiQ83hDx1MY8LcPQ3nBB"
GRAPH_GATEWAY = "https://gateway.thegraph.com/api"


def _subgraph_url(api_key: str) -> str:
    return f"{GRAPH_GATEWAY}/{api_key}/subgraphs/id/{MACHINEX_SUBGRAPH_ID}"


def _query(token_address: str, start_ts: int, end_ts: int) -> str:
    return f"""
    {{
      tokenDayDatas(
        orderBy: startOfDay
        orderDirection: asc
        first: 1000
        where: {{
          token: "{token_address.lower()}"
          startOfDay_gte: {start_ts}
          startOfDay_lte: {end_ts}
        }}
      ) {{
        startOfDay
        priceUSD
        token {{ symbol }}
      }}
    }}
    """


def fetch_daily_prices(api_key: str, token_address: str, start_ts: int, end_ts: int) -> list[dict]:
    url = _subgraph_url(api_key)
    query = _query(token_address, start_ts, end_ts)

    for attempt in range(3):
        try:
            r = requests.post(url, json={"query": query}, timeout=30)
            if r.status_code in (401, 403):
                print(f"ERROR: Subgraph auth failed (HTTP {r.status_code}) — check GRAPH_API_KEY", file=sys.stderr)
                sys.exit(1)
            r.raise_for_status()
            j = r.json()
            if "errors" in j:
                msgs = [e.get("message", "") for e in j["errors"]]
                if any("bad indexers" in m for m in msgs) and attempt < 2:
                    time.sleep(4 * (attempt + 1))
                    continue
                print(f"ERROR: Subgraph returned errors: {msgs}", file=sys.stderr)
                sys.exit(1)
            return j.get("data", {}).get("tokenDayDatas") or []
        except requests.RequestException as e:
            if attempt == 2:
                print(f"ERROR: Subgraph request failed: {e}", file=sys.stderr)
                sys.exit(1)
            time.sleep(4)

    return []


def _decimal(value: str | None) -> Decimal | None:
    try:
        d = Decimal(str(value or "0"))
        return d if d > 0 else None
    except InvalidOperation:
        return None


def _date_to_ts(d: date) -> int:
    """Unix timestamp for midnight UTC on the given date."""
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def compute_twap(rows: list[dict]) -> Decimal:
    prices = [_decimal(r.get("priceUSD")) for r in rows]
    prices = [p for p in prices if p is not None]
    if not prices:
        raise ValueError("No valid price data to compute TWAP")
    return sum(prices, Decimal("0")) / Decimal(len(prices))


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute DEUS TWAP from MachineX subgraph")
    parser.add_argument("--start", default="2026-05-27", help="Start date inclusive, YYYY-MM-DD")
    parser.add_argument("--end", default="2026-06-10", help="End date inclusive, YYYY-MM-DD")
    parser.add_argument("--address", default=DEUS_ADDRESS, help="Token address (default: DEUS on peaq)")
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)

    if start_date > end_date:
        print("ERROR: --start must be before --end", file=sys.stderr)
        sys.exit(1)

    api_key = os.getenv("GRAPH_API_KEY")
    if not api_key:
        print("ERROR: GRAPH_API_KEY not set in environment / .env", file=sys.stderr)
        sys.exit(1)

    start_ts = _date_to_ts(start_date)
    end_ts = _date_to_ts(end_date)
    n_days = (end_date - start_date).days + 1

    print(f"Fetching {n_days}-day price history ({start_date} → {end_date}) from MachineX subgraph…")
    rows = fetch_daily_prices(api_key, args.address, start_ts, end_ts)

    if not rows:
        print("No tokenDayData returned — token may not exist in subgraph or has no trading history.")
        sys.exit(1)

    symbol = (rows[0].get("token") or {}).get("symbol") or "DEUS"

    # Check for any missing days
    returned_dates = {datetime.fromtimestamp(int(r["startOfDay"]), tz=UTC).date() for r in rows}
    expected_dates = {start_date + timedelta(days=i) for i in range(n_days)}
    missing = sorted(expected_dates - returned_dates)

    print(f"\n{'Date':<12}  {'Price (USD)':>16}")
    print("-" * 30)

    for row in rows:
        d = datetime.fromtimestamp(int(row["startOfDay"]), tz=UTC).date()
        price = _decimal(row.get("priceUSD"))
        price_s = f"${price:,.6f}" if price else "       n/a"
        print(f"{str(d):<12}  {price_s:>16}")

    if missing:
        for d in missing:
            print(f"{str(d):<12}  {'  missing':>16}")

    print("-" * 30)

    if missing:
        print(f"\n⚠  {len(missing)} day(s) missing from subgraph — TWAP computed over {len(rows)} available days.")

    try:
        twap = compute_twap(rows)
        n_valid = len([r for r in rows if _decimal(r.get("priceUSD"))])
        print(f"\n{symbol} TWAP ({start_date} → {end_date}): ${twap:,.6f}  [{n_valid} days]")
    except ValueError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
