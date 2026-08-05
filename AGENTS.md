# AGENTS.md

## What this project does
Multi-wallet, multi-chain DeFi position tracker. Persists every liquidity event with USD-at-event-time pricing for accurate cost basis; computes IL vs HODL benchmark, MTD/MoM PnL, and rebalance alerts. Personal CLI tool — not a service.

## Stack
Python 3.11+ · SQLite (stdlib) · `requests` · `python-dotenv` · `pycryptodome` · `eth-hash`
Dev: `pytest` · `ruff` · `mypy`

## File structure
```
core/types.py              — normalized dataclasses; every layer speaks only these
core/adapter.py            — ProtocolAdapter ABC + adapter registry
core/storage.py            — SQLite schema + MTD/MoM queries
core/runner.py             — sync → snapshot → alerts orchestrator
adapters/_cl_math.py       — shared CL math: tick→price, amounts_from_liquidity, fee growth
adapters/_rpc.py           — shared JSON-RPC client
adapters/pancake_infinity.py — working BSC adapter; use as implementation template
analytics.py               — cost basis, IL, PnL (protocol-agnostic)
alerts.py                  — 5 alert kinds (protocol-agnostic)
cli.py                     — argparse entry; imports adapters to trigger auto-registration
tests/conftest.py          — storage / make_event / make_position fixtures
tests/test_pipeline.py     — full smoke tests; no external calls
```

## Key patterns
- Primary key everywhere: `(wallet, chain, protocol_id, position_key)` — never key by `position_key` alone
- Events are immutable; idempotency via `event_uid = f"{tx_hash}:{log_index}"`
- `Event.usd_at_ts` must be USD at block time — use `get_historical_price()` when backfilling, never current price
- `ProtocolKind` drives analytics dispatch, not `protocol_id`; IL branches in `analytics/pnl.py`
- Adapters are stateless (construct/discard freely); all state lives in SQLite
- Auto-registration: `_autoregister()` at module bottom gated on env vars; importing in `cli/main.py` triggers it
- `from __future__ import annotations` at top of every module
- `Decimal` for money · `int` for ticks/blocks/timestamps · `str` for addresses (lowercased on input)
- Protocol-specific extras that don't fit normalized types → `Position.meta` / `Event.meta`

## Env vars
| Var | Default | Controls |
|-----|---------|----------|
| `GRAPH_API_KEY` | required | The Graph gateway; Pancake adapter won't register without it |
| `TRACKER_DB` | `./tracker.db` | SQLite path |
| `BSC_RPC_URL` | public BNB node | BSC JSON-RPC |
| `{ETH,ARB,OP,BASE,POLYGON,AVAX}_RPC_URL` | public nodes | Per-chain RPC (set for reliability) |
| `SLACK_WEBHOOK_URL` | unset | Alert delivery (DB write works; push not yet wired) |

## What NOT to do
- `if protocol_id == "..."` outside `adapters/` → add a method to `ProtocolAdapter` instead
- Import adapter classes above the adapter layer → use the registry
- `float` for money → `Decimal`; the only acceptable float is SQLite `REAL` at the storage boundary
- ORM (SQLAlchemy etc.) → raw SQL in `storage.py` is intentional; queries are too complex to hide
- `async/await` without a concurrency story → sync is fine for a daily cron; use `ThreadPoolExecutor` if needed
- Fetch prices inside `analytics/` or `alerts/` → those layers receive price maps as arguments
- Include protocol name in `position_key` → it's already encoded in the 4-tuple PK

## Adding new modules
**New adapter** — `adapters/<name>.py`:
1. Subclass `ProtocolAdapter`; implement `fetch_events()`, `fetch_positions()`, `get_current_price()`
2. Reuse `_cl_math.py` for any CL fork (tick math is identical across V3/V4/Infinity)
3. `_autoregister()` at module bottom gated on required env vars; `import defi_tracker.adapters.<name>  # noqa: F401` in `cli.py`
4. Tests in `tests/adapters/test_<name>.py` with mocked HTTP — no real RPC/subgraph calls

**New analytics or alerts logic** — add to `analytics.py` or `alerts.py`; accepts only `core/types.py` types; no adapter imports; receives price maps as arguments.

**Module header**:
```python
"""Why this module exists — the constraint or invariant it enforces."""
from __future__ import annotations
# imports: stdlib → third-party → local (ruff enforces order)
```
