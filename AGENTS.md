# AGENTS.md

## What this project does
A liquidity desk for a token treasury: tracks protocol-owned liquidity across wallets, chains and DEXes; persists every liquidity event with USD-at-event-time pricing for accurate cost basis; computes IL vs a HODL benchmark, period P&L with flows netted, risk, and propose-only rebalance tickets; holds the book against a mandate (policy limits, exceptions, incidents, monthly report) and keeps the market-maker book (contracts, delivery, RFQ pricing) beside it. Local CLI + HTML desk — not a service, read-only on chain, never transacts.

Read [README.md](README.md) for purpose and stakeholders, [docs/METHODOLOGY.md](docs/METHODOLOGY.md) for the liquidity-management method the code enforces, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for layers, data flow, storage, failure and security model. The repo doubles as the operator's portfolio, so keep docs accurate to the code and free of figures.

## Confidentiality (public repo)
This repo is public. Never commit real wallet addresses, transaction hashes or position IDs, counterparty names, deal terms, book figures, private links, or personal paths. Confidential inputs live in gitignored places (`content/`, `RFQ/`, the DB, local docs); tests use synthetic data (fictional "Alpha/Beta/Gamma MM", fake wallets `0x1111…`).

## Stack
Python 3.11+ · SQLite (stdlib) · `requests` · `python-dotenv` · `pycryptodome` · `eth-hash`
Dev: `pytest` · `ruff` · `mypy`

## File structure
```
core/types.py              — normalized dataclasses; every layer speaks only these
core/adapter.py            — ProtocolAdapter ABC + adapter registry
core/storage.py            — SQLite schema + migrations + every query (raw SQL)
core/runner.py             — sync → snapshot → alerts orchestrator
core/endpoints.py          — the one place a chain's RPC URL is chosen (explicit → Alchemy key → public); `redact()`
adapters/_base.py          — BaseAdapter: shared subgraph/RPC/pricing scaffolding
adapters/_cl_math.py       — shared CL math: tick→price, amounts_from_liquidity, fee growth
adapters/_rpc.py           — shared JSON-RPC client (failover, range-halving, rate-limit backoff, block-pinned reads)
adapters/_pricing.py       — PriceFetcher: historical prices, one call per token per day
adapters/_pancake_chain.py — Pancake Infinity events decoded from BSC receipts (no subgraph)
adapters/_pool_activity.py — 7-day pool fees/volume from Swap logs or the fee counter
adapters/machinex.py       — peaq (Ramses-fork CL + legacy XYK)
adapters/pancake_infinity.py — BSC; use as implementation template
adapters/uniswap_v3.py     — Base (others via UNISWAP_V3_CHAINS)
poolflow.py                — pool history per day (Swap logs, or the fee counter where logs are out of reach) with the book's own depth (fee elasticity ε, backtest)
analytics.py               — cost basis, IL, PnL, period PnL, policy checks, optimiser, MM book (protocol-agnostic)
alerts.py                  — per-position alert kinds + POLICY_BREACH (protocol-agnostic)
costs.py                   — gas from receipts, priced at block time (`backfill_gas`)
policy.py                  — mandate limits from policy.toml; unset ≠ passing; desk content (mandate/RFQ TOML)
mandate.py                 — monthly mandate report (durable HTML, written once)
report.py                  — gathers ReportData / period PnL once for every surface
web.py                     — the desk view: ReportData → one self-contained HTML page
serve.py                   — the desk on localhost, re-read per request (loopback Host only)
slack.py                   — alert + report delivery
venues.py                  — where PEAQ trades (CoinGecko tickers), read at build
orderbooks.py              — PEAQ order books from each exchange's public API at build: the independent check on Coinwatch
coinwatch.py               — MM delivery history (market-makers' Coinwatch SQLite), read-only at desk build
rpccheck.py                — `rpc-check`: reachable, right chain id, archive?
netwait.py                 — scheduled passes wait for the network after a laptop wake
cli.py                     — argparse entry; imports adapters to trigger auto-registration
tests/conftest.py          — storage / make_event / make_position fixtures
tests/test_pipeline.py     — full smoke tests; no external calls
docs/ARCHITECTURE.md       — layers, data flow, storage, failure + security model, extension points
docs/METHODOLOGY.md        — how the liquidity is managed: accounting, ranges, rebalancing, risk, governance, MMs
```

## Key patterns
- Primary key everywhere: `(wallet, chain, protocol_id, position_key)` — never key by `position_key` alone, or by wallet + key (`analytics.position_id`); uniswap_v3 keys are `pool:lo:hi`, shared by any wallet in that range
- Events are immutable; idempotency via `event_uid = f"{tx_hash}:{log_index}"`
- `Event.usd_at_ts` must be USD at block time — use `get_historical_price()` when backfilling, never current price
- `ProtocolKind` drives analytics dispatch, not `protocol_id`; IL branches in `analytics.impermanent_loss`
- Adapters are stateless (construct/discard freely); all state lives in SQLite
- Auto-registration: `_autoregister()` at module bottom gated on env vars; importing in `cli.py` triggers it
- `from __future__ import annotations` at top of every module
- `Decimal` for money · `int` for ticks/blocks/timestamps · `str` for addresses (lowercased on input)
- Protocol-specific extras that don't fit normalized types → `Position.meta` / `Event.meta`
- A failed read is unknown, never zero or absent: raise (or `None`) rather than skip a position (the runner tombstones what is missing) or return $0 fees
- Any error string that can carry a URL goes through `core.endpoints.redact()` before it is logged, printed or stored — an RPC URL's path is its API key

## Env vars
| Var | Default | Controls |
|-----|---------|----------|
| `GRAPH_API_KEY` | required | The Graph gateway; Pancake adapter won't register without it |
| `TRACKER_DB` | `./tracker.db` | SQLite path |
| `ALCHEMY_API_KEY` | unset | Builds bsc (`bnb-mainnet`) and base endpoints; needs Pay As You Go for the bsc log scan |
| `BSC_RPC_URL` | public BNB node | BSC JSON-RPC (wins over the Alchemy key) |
| `{CHAIN}_RPC_URL_FALLBACK` | unset | Optional second provider(s), comma-separated |
| `{ETH,ARB,OP,BASE,POLYGON,AVAX}_RPC_URL` | public nodes | Per-chain RPC (set for reliability) |
| `SLACK_WEBHOOK_URL` | unset | Alert + report delivery after each `run`; without it alerts stay in the DB |
| `COINWATCH_DB` | `~/market-makers/data/history.sqlite3` | MM delivery history for the RFQ tab (read-only) |
| `POLICY_FILE` | `./policy.toml` | Mandate policy limits; a malformed, unreadable or (when named) missing file alerts, it does not load as "no limits" |
| `PEAQ_RPC_URL` | unset | peaq JSON-RPC (Alchemy does not serve peaq); needed for MachineX event scanning |
| `UNISWAP_V3_CHAINS` | `base` | Chains the Uniswap V3 adapter registers for; `""` disables it |
| `COINGECKO_API_KEY` | unset | CoinGecko demo key for prices and venues |
| `MANDATE_FILE` | `content/mandate.toml` | Desk mandate charter content |
| `RFQ_FILE` | `content/rfq.toml` | Incoming MM quotes for the RFQ tab |
| `DESK_PROPOSAL_URL` | `""` | Link to the mandate proposal the desk and scorecard cite; empty = no link |
| `DESK_URL` | `""` | Link to the hosted desk page, used in the Slack ticket digest; empty = no link |
| `LOG_LEVEL` | `WARNING` | Logging verbosity (`--verbose` for INFO on `run`) |

## What NOT to do
- `if protocol_id == "..."` outside `adapters/` → add a method to `ProtocolAdapter` instead
- Import adapter classes above the adapter layer → use the registry
- `float` for booked money → `Decimal`. Booked money = events, cost basis, fees, PnL, anything summed into the book; the only acceptable float there is SQLite `REAL` at the storage boundary. Model estimates (risk grid, greeks, optimiser, ticket EVs — expectations, not balances) are float by design; they are displayed and ranked, never summed back into the book
- ORM (SQLAlchemy etc.) → raw SQL in `storage.py` is intentional; queries are too complex to hide
- `async/await` without a concurrency story → sync is fine for a daily cron; use `ThreadPoolExecutor` if needed
- Fetch prices inside `analytics.py` or `alerts.py` → those layers receive price maps as arguments
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
