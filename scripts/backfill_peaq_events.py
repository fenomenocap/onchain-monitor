"""
One-off catch-up: machinex/peaq event scanning advances at most
MAX_BLOCKS_PER_RUN (150k) blocks per `run` invocation, so the stored block
cursor for each wallet can sit millions of blocks behind chain head — meaning
MTD/monthly fee numbers look empty even though the chain has the data.

This loops TrackerRunner.sync_adapter() (the same code path `run` uses) until
each wallet's cursor catches up to within one run's reach of the current
block, so it re-triggers naturally on the next scheduled `run`.

Usage: python scripts/backfill_peaq_events.py
"""

from __future__ import annotations

import logging
import time

from dotenv import load_dotenv

load_dotenv()

import defi_tracker.adapters.machinex as machinex_mod  # noqa: E402
from defi_tracker.core.adapter import get_adapter  # noqa: E402
from defi_tracker.core.runner import TrackerRunner  # noqa: E402
from defi_tracker.core.storage import Storage  # noqa: E402
from defi_tracker.core.types import Chain  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("backfill_peaq")


def main() -> None:
    storage = Storage("tracker.db")
    runner = TrackerRunner(storage)
    adapter = get_adapter("machinex")

    if adapter._rpc is None:  # noqa: SLF001
        _log.error("No PEAQ_RPC_URL configured — aborting")
        return

    latest = adapter._rpc.get_block_number()  # noqa: SLF001
    if latest is None:
        _log.error("Could not fetch current peaq block number — aborting")
        return
    _log.info("Current peaq block: %d", latest)

    for wallet in storage.list_wallets():
        addr = wallet["address"]
        iteration = 0
        consecutive_failures = 0
        while True:
            cursor = adapter._get_scan_cursor(addr, Chain.PEAQ)  # noqa: SLF001
            remaining = latest - cursor
            if remaining <= machinex_mod.MAX_BLOCKS_PER_RUN:
                _log.info("%s caught up (cursor=%d, remaining=%d blocks)", addr, cursor, remaining)
                break
            iteration += 1
            try:
                inserted = runner.sync_adapter(adapter, addr, Chain.PEAQ)
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    _log.error("%s: %d consecutive failures — moving on. Last: %s",
                               addr, consecutive_failures, e)
                    break
                _log.warning("%s iter %d failed (%s) — retrying in 15s", addr, iteration, e)
                time.sleep(15)
                continue
            consecutive_failures = 0
            new_cursor = adapter._get_scan_cursor(addr, Chain.PEAQ)  # noqa: SLF001
            _log.info(
                "%s iter %d: cursor %d -> %d, events inserted=%d, %d blocks remaining",
                addr, iteration, cursor, new_cursor, inserted, latest - new_cursor,
            )
            if new_cursor <= cursor:
                _log.warning("%s cursor did not advance — stopping to avoid an infinite loop", addr)
                break
            time.sleep(0.5)


if __name__ == "__main__":
    main()
