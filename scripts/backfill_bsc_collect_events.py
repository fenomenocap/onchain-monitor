"""
One-off fix: every historical BSC withdraw event recorded with amount0=amount1=0
was actually a pure-liquidity fee harvest (liquidityDelta == 0), mis-recorded
before pancake_infinity.py learned to read collected amounts from the tx
receipt's ERC20 Transfer logs. `events` uses INSERT OR IGNORE, so a normal
re-run never overwrites these rows — this script re-derives and replaces them
in place so past-month MTD/monthly fee numbers become correct.

Usage: python scripts/backfill_bsc_collect_events.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

from defi_tracker.adapters._cl_math import sum_transfers_to  # noqa: E402
from defi_tracker.core.adapter import get_adapter  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("backfill_bsc")

DB_PATH = "tracker.db"


def main() -> None:
    adapter = get_adapter("pancake_infinity")
    if adapter._rpc is None:  # noqa: SLF001
        _log.error("No BSC RPC configured — aborting")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM events WHERE chain='bsc' AND kind='withdraw' AND usd_at_ts=0"
    ).fetchall()
    _log.info("Found %d zero-value withdraw rows to check", len(rows))

    fixed = 0
    for row in rows:
        amts = json.loads(row["amounts_json"])
        if len(amts) != 2:
            continue
        (tk0, tk1) = (amts[0]["token_key"], amts[1]["token_key"])
        addr0, addr1 = tk0.split(":", 1)[1], tk1.split(":", 1)[1]

        receipt = adapter._rpc.get_transaction_receipt(row["tx_hash"])  # noqa: SLF001
        if not receipt:
            _log.warning("No receipt for tx %s — skipping", row["tx_hash"])
            continue

        raw0 = sum_transfers_to(receipt, addr0, row["wallet"])
        raw1 = sum_transfers_to(receipt, addr1, row["wallet"])
        if raw0 == 0 and raw1 == 0:
            _log.info("tx %s: no transfers found to wallet — leaving as-is", row["tx_hash"])
            continue

        dec0 = conn.execute(
            "SELECT decimals FROM tokens WHERE chain='bsc' AND address=?", (addr0,)
        ).fetchone()
        dec1 = conn.execute(
            "SELECT decimals FROM tokens WHERE chain='bsc' AND address=?", (addr1,)
        ).fetchone()
        decimals0 = dec0[0] if dec0 else 18
        decimals1 = dec1[0] if dec1 else 18

        amt0 = Decimal(raw0) / Decimal(10**decimals0)
        amt1 = Decimal(raw1) / Decimal(10**decimals1)
        p0 = Decimal(str(amts[0]["price_at_ts"]))
        p1 = Decimal(str(amts[1]["price_at_ts"]))
        usd = amt0 * p0 + amt1 * p1

        new_amounts = [
            {"token_key": tk0, "amount": str(amt0), "price_at_ts": str(p0)},
            {"token_key": tk1, "amount": str(amt1), "price_at_ts": str(p1)},
        ]

        conn.execute(
            "UPDATE events SET kind='collect', usd_at_ts=?, amounts_json=? WHERE event_uid=?",
            (float(usd), json.dumps(new_amounts), row["event_uid"]),
        )
        fixed += 1
        _log.info(
            "Fixed %s: %s %s + %s %s ($%.2f)",
            row["tx_hash"][:12], amt0, amts[0]["symbol"], amt1, amts[1]["symbol"], usd,
        )

    conn.commit()
    conn.close()
    _log.info("Done — %d/%d rows reclassified as collect", fixed, len(rows))


if __name__ == "__main__":
    main()
