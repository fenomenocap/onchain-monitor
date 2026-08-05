# On-chain Monitor

Multi-chain DeFi position tracking — real cost basis, impermanent loss, and
PnL across protocols and wallets.

[![CI](https://github.com/fenomenocap/onchain-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/fenomenocap/onchain-monitor/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Type-checked: mypy](https://img.shields.io/badge/type--checked-mypy-blue)](https://mypy-lang.org/)

A multi-protocol, multi-chain, multi-wallet DeFi position tracker. Computes
real cost basis, impermanent loss, MTD/MoM PnL, and rebalancing alerts.
Everything runs locally against a single SQLite file — no service, no
custody, read-only RPC access.

Currently supports **PancakeSwap Infinity (BSC)**. Architecture is designed
so adding Uniswap V3/V4, Aave V3, Curve, etc. is a thin adapter on top of
shared math.

## What it actually does

- Persists every position-affecting event with the USD price **at event time**,
  so cost basis stays accurate forever regardless of where prices go later
- Computes IL against a true HODL benchmark (net deposited tokens × current
  price), not against current pool balances
- Snapshots every position daily; MTD and MoM queries roll up from snapshots
  + collected fees within each window
- Surfaces five alert kinds: `OUT_OF_RANGE`, `NEAR_EDGE`, `IL_THRESHOLD`,
  `NEGATIVE_CARRY`, `STALE_POSITION`, with built-in dedup
- Multi-wallet, multi-chain native — primary key everywhere is
  `(wallet, chain, protocol_id, position_key)`

## Quick start

```bash
# 1. Install
pip install -e .

# 2. Configure
cp .env.example .env
# edit .env — at minimum set GRAPH_API_KEY

# 3. Initialize the database
python -m defi_tracker.cli init

# 4. Register wallets you want to track
python -m defi_tracker.cli add-wallet 0xYourWallet --label "main"
python -m defi_tracker.cli add-wallet 0xAnother --label "cold"

# 5. Run a sync + snapshot pass (do this daily via cron)
python -m defi_tracker.cli run

# 6. Look at the data
python -m defi_tracker.cli report           # current snapshot
python -m defi_tracker.cli mtd              # month-to-date PnL
python -m defi_tracker.cli mom --months 6   # 6-month roll-up
```

## Architecture

Four layers, top-down dependency:

```
┌──────────────────────────────────────────────────────────┐
│  cli/             argparse entry point                   │
├──────────────────────────────────────────────────────────┤
│  core/runner.py   orchestrates sync → snapshot → alerts  │
├──────────────────────────────────────────────────────────┤
│  analytics/       cost basis, IL, PnL (protocol-agnostic)│
│  alerts/          rebalance signals (protocol-agnostic)  │
├──────────────────────────────────────────────────────────┤
│  adapters/        per-protocol: pancake_infinity, ...    │
│  adapters/_cl_math.py   shared CL math (V3/V4/Infinity)  │
│  adapters/_rpc.py       shared JSON-RPC client           │
├──────────────────────────────────────────────────────────┤
│  core/types.py    Position, Event, Token, Alert          │
│  core/storage.py  SQLite schema + queries                │
│  core/adapter.py  the ProtocolAdapter abstract base      │
└──────────────────────────────────────────────────────────┘
```

The boundary that matters: **adapters only ever speak the normalized types
in `core/types.py`**. Protocol-specific shapes never leak above the adapter
layer. This is what makes new protocols cheap to add and analytics easy to
test.

## Supported protocols

| Protocol | Chains | Status | Adapter file |
|---|---|---|---|
| PancakeSwap Infinity (V4) | BSC | ✅ Working | `adapters/pancake_infinity.py` |
| Uniswap V3 | ETH, ARB, OP, Base, Polygon | 🚧 Stub | `adapters/uniswap_v3.py` |
| Aave V3 | ETH, ARB, OP, Base, Polygon, AVAX | 🚧 Stub | `adapters/aave_v3.py` |

Adding a new protocol = one new file in `adapters/` implementing
`ProtocolAdapter`. See `AGENTS.md` for the step-by-step.

## Database

SQLite, single file. Default path `./tracker.db`, override via `TRACKER_DB`
env var.

Tables (see `core/storage.py` for the full schema):

- `wallets` — tracked addresses
- `events` — immutable, append-only, USD priced at event time
- `snapshots` — daily per-position state (drives MTD/MoM)
- `alerts` — deduped signal log
- `price_cache` — historical price lookups
- `sync_state` — per (wallet, chain, protocol) last-synced cursor

## Dependencies

- `requests` — HTTP for subgraph + RPC + CoinGecko
- `python-dotenv` — load `.env`
- `pycryptodome` — keccak256 for function selectors

That's it. SQLite is in the stdlib. No web framework, no ORM.

## Daily cron

```cron
# Run every day at 00:05 UTC
5 0 * * * cd /path/to/onchain-monitor && /usr/bin/python -m defi_tracker.cli run >> tracker.log 2>&1
```

## What's not done yet

- Uniswap V3 / V4 adapters (skeleton only)
- Aave V3 adapter (skeleton only)
- Historical price backfill (currently uses current price for old events
  as fallback — see `get_historical_price` in `pancake_infinity.py`)
- Slack delivery for alerts (alerts are written to DB but not yet pushed)
- A web UI (everything is CLI right now)

These are good "next ticket" items. See `AGENTS.md` for guidance on each.
