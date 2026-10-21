"""What each MM actually delivered: Coinwatch's daily history, read at build.

A separate project (market-makers) pulls Coinwatch daily into its own
SQLite file; this module only reads it, read-only, and hands the rows to the
report — the analytics layer never opens a database it does not own. The day
in progress is excluded (its figures are still moving). A missing or
unreadable file is an empty list, which the page shows as "not read".
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

DEFAULT_COINWATCH_DB = "~/market-makers/data/history.sqlite3"

# Coinwatch's exchange slugs → the desk's venue keys (analytics._venue_key).
_VENUE = {"gateio": "gate", "mexc-global": "mexc"}


def fetch_delivery(path: str | None = None, today: date | None = None) -> list[dict]:
    """[{metric, mm, venue, day, own, remaining}] for every complete day.

    `own` is the MM's figure, `remaining` the rest of the book on that venue
    (None where Coinwatch has no such series, e.g. spread)."""
    p = Path(os.path.expanduser(path or os.getenv("COINWATCH_DB") or DEFAULT_COINWATCH_DB))
    if not p.exists():
        return []
    cutoff = (today or datetime.now(UTC).date()).isoformat()
    try:
        # as_uri() percent-escapes the path; a raw "?" or "#" in it would
        # otherwise be parsed as the URI's query and the read silently fail.
        con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT o.metric, o.mm, o.exchange, o.day, o.value, r.value"
                " FROM observation o LEFT JOIN observation r"
                "   ON r.metric = o.metric AND r.mm = o.mm AND r.exchange = o.exchange"
                "  AND r.day = o.day AND r.series = 'remaining'"
                " WHERE o.series = 'own' AND o.day < ?"
                " ORDER BY o.day",
                (cutoff,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [
        {"metric": m, "mm": mm, "venue": _VENUE.get(ex, ex), "day": d, "own": own, "remaining": rem}
        for m, mm, ex, d, own, rem in rows
    ]
