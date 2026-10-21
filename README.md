# On-chain Monitor

**A liquidity desk for a token treasury.** It tracks protocol-owned liquidity
across chains and DEXes, books it like an accountant would, holds it against a
written mandate, and keeps the market-maker relationships beside it honest.
Everything runs locally against one SQLite file: no service, no custody, and
read-only access to the chain.

[![CI](https://github.com/fenomenocap/onchain-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/fenomenocap/onchain-monitor/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Type-checked: mypy](https://img.shields.io/badge/type--checked-mypy-blue)](https://mypy-lang.org/)

> **This repository is a working tool and a portfolio.** It is a token
> treasury's on-chain liquidity management, written down as code: the accounting, the
> range and rebalancing method, the risk and governance controls, and the
> oversight of market makers. The method is set out in
> **[Liquidity management methodology](docs/METHODOLOGY.md)**, and the system that
> enforces it in **[Architecture](docs/ARCHITECTURE.md)**. Everything published
> here uses synthetic data; no real book figures, wallets or counterparties appear.

## What this project demonstrates

| Area | In practice |
|---|---|
| **Liquidity strategy** | Concentrated-liquidity range selection from first principles: occupancy under a random walk, fee share that saturates, the IL rate, and book-level sizing. Volatility comes with its confidence band, and the fee response to depth is measured from each pool's own history. |
| **Execution discipline** | Every action (re-range, resize, close, harvest) is priced in dollars against doing nothing, robust across σ's band before it is called "act now", backtested against the book as it actually stood, and executed by a human signer on a multisig. |
| **Treasury accounting** | Event-time cost basis, a true hold benchmark, period P&L with flows netted, accrual-basis fees with harvests booked to the month that earned them, and gas as a cost line. |
| **Risk and governance** | Shock grid and greeks per position. A mandate that is data: limits that read NOT SET until set, breach episodes with review sign-off, an incident lifecycle, and a monthly report written once. |
| **Counterparty oversight** | Market-maker loans and options marked at the current price, delivery checked against exchanges' own order books, and quotes compared per unit of depth. |
| **Engineering** | Protocol adapters on shared math across three chains, 700+ tests with no network, strict typing, an explicit failure model (unknown is never zero), and secrets redacted from every output. |

## Why it exists

A token issuer's treasury usually runs two kinds of liquidity at once:

- **On-chain:** liquidity the treasury provides itself, in concentrated-liquidity
  pools on several chains and wallets.
- **Off-chain:** market makers lent inventory or granted options to quote the
  token on exchanges.

Each answers to someone, and neither is easy to see. Block explorers show
balances, not results. Spreadsheets drift from the chain. Market-maker reports
are self-reported. Nobody can say, on a given day:

- Is our liquidity earning more than it costs us, versus simply holding the tokens?
- What did we deposit, at what prices, and what is it worth now, net of flows and gas?
- Are we inside the limits we agreed to, and if not, since when, and who reviewed it?
- Is each market maker delivering the depth and spread they are paid for, and
  what would a new quote actually cost per unit of depth?
- If the token moves 20%, what happens to the book, and which positions fall out of range?

This project answers those questions from the chain itself, with every number
traceable to the events and reads behind it.

## Who it serves

| Stakeholder | What they get |
|---|---|
| **Treasury / liquidity operator** | One view of every position on every chain: range state, unclaimed fees, alerts. A propose-only action queue of rebalance tickets ranked by expected value, and a range optimiser that shows its reasoning. |
| **Finance** | Cost basis priced at event time, so it never drifts. Period P&L (day, week, month, quarter, year, inception) with deposits and withdrawals netted out, split into market move, impermanent loss, fees and gas. |
| **Leadership / governance** | A mandate made concrete: limits that read NOT SET until someone sets them (never "passing"), breach episodes that stay open until a reading clears them, review sign-off, an incident log, and a monthly report written once and never restated. |
| **Market-making relationships** | A contractual ledger of loans and options, marked at the current token price. Delivery measured against commitments, cross-checked against the exchanges' own order books. RFQ comparison that prices each bid for its term and per unit of depth. |
| **Risk** | A price-shock grid, delta and gamma per position, odds of exiting the range, and the fee yield each position needs to break even. |
| **Engineering / ops** | Pipeline health per wallet, chain and protocol, so a silent data outage shows up instead of freezing the numbers. Honest unknowns everywhere: a value that could not be read is shown as unknown, never as zero. |

## Design principles

- **Event-time pricing.** Every deposit, withdrawal and fee claim is stored with
  its USD value at the block it happened. Cost basis never depends on today's price.
- **A real benchmark.** Impermanent loss is measured against holding the tokens
  actually deposited, not against the pool's current balances.
- **Unknown is not zero.** A failed read is reported as unknown and excluded with
  the value it leaves out. It is never counted as $0 or as a closed position.
- **Unset is not passing.** A mandate limit nobody has set reports as NOT SET.
- **Propose, never execute.** The tool reads the chain and recommends; it holds no
  keys and sends no transactions.
- **Local first.** One SQLite file, a CLI, a static HTML page or a localhost
  server. API keys are redacted from every error before it is logged, printed or stored.

## Architecture

```
            ┌──────────────────────────────────────────────────────────────┐
 surfaces   │ cli.py · web.py (HTML desk) · serve.py (localhost) · slack.py │
            │ mandate.py (monthly report)                                  │
            ├──────────────────────────────────────────────────────────────┤
 assembly   │ report.py — gathers one ReportData for every surface          │
            ├──────────────────────────────────────────────────────────────┤
 orchestr.  │ core/runner.py — sync → snapshot → marks → alerts             │
            ├──────────────────────────────────────────────────────────────┤
 analytics  │ analytics.py — cost basis, IL, period P&L, risk, optimiser,   │
            │   elasticity, backtest, mandate checks, MM book, RFQ pricing  │
            │ alerts.py · policy.py · costs.py · poolflow.py                │
            ├──────────────────────────────────────────────────────────────┤
 adapters   │ machinex (peaq) · pancake_infinity (BSC) · uniswap_v3 (Base)  │
            │ shared: _cl_math · _rpc · _pricing · _pool_activity           │
            ├──────────────────────────────────────────────────────────────┤
 core       │ core/types.py (normalized types) · core/storage.py (SQLite)   │
            │ core/adapter.py (contract + registry) · core/endpoints.py     │
            └──────────────────────────────────────────────────────────────┘
 market reads at page build: venues.py (CoinGecko) · orderbooks.py (exchanges)
                             · coinwatch.py (MM delivery history, read-only)
```

Two boundaries carry the design:

1. **Adapters speak only the normalized types** in `core/types.py`. Protocol
   shapes (subgraph payloads, log layouts, NFT position managers) never leak
   upward, so a new protocol is one file and every analytic works on it unchanged.
2. **Analytics never fetches.** It receives rows and price maps as arguments,
   which keeps it deterministic and testable without a network.

The full walkthrough covers data flow, the storage model, the failure model and
the extension points: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## The desk

`web` renders the whole book as one self-contained HTML page; `serve` serves the
same page on localhost, re-read from the database on every refresh. Tabs:

| Tab | Shows |
|---|---|
| **Brief** | The two-minute read: headline numbers, what falls due in the next 60 days, market-maker exposure |
| **Actions** | Rebalance tickets ranked by expected value, with the backtest's verdict |
| **Performance** | Period P&L with flows netted; fees by accrual month; gas |
| **Risk** | Shock grid, greeks, range-exit odds, breakeven fee yield |
| **MM book** | Loans and options marked at the current price; expiries and renewal terms |
| **RFQ** | Incoming market-maker quotes priced for their term and per unit of depth, against measured delivery |
| **Venues** | Where the token trades: volume, spread and ±2% depth per exchange |
| **Mandate** | Limits vs the book, open exceptions, incidents, scorecard |
| **Positions** | Every position with range, value, unclaimed fees and yield |

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env            # at minimum: GRAPH_API_KEY, and an RPC URL per chain you track
python -m defi_tracker.cli init
python -m defi_tracker.cli add-wallet 0xYourWallet --label "main"
python -m defi_tracker.cli run  # sync events, snapshot, alert
python -m defi_tracker.cli web --out dashboard.html --open
```

## Commands

**Daily operation**

```bash
defi-tracker run                 # sync + snapshot + alerts (+ gas for new transactions)
defi-tracker mark                # hourly valuation only, no sync
defi-tracker health              # is every data source current?
defi-tracker rpc-check           # endpoints reachable, right chain, archive?
```

**The book**

```bash
defi-tracker report              # current snapshot, P&L decomposition
defi-tracker periods             # D/WTD/MTD/QTD/YTD/ITD P&L, flows netted
defi-tracker periods --by pair --period MTD
defi-tracker fees                # fee income by month
defi-tracker mtd | mom           # per-position month change; month-over-month roll-up
```

**Decisions (propose-only)**

```bash
defi-tracker rebalance           # EV-ranked tickets; mark done or snooze
defi-tracker optimise            # per pool: the band that maximises fees − IL
defi-tracker elasticity          # how pool fees respond to depth, measured from history
defi-tracker backtest            # the optimiser replayed against the book as it stood
defi-tracker risk                # shocks, greeks, exit odds, breakeven yield
```

**Mandate and counterparties**

```bash
defi-tracker limits              # policy.toml limits vs the book
defi-tracker exceptions          # breach episodes; --review ID --by NAME
defi-tracker incident add --title … --severity high
defi-tracker ledger add --ref mm-a-loan --counterparty "MM A" --kind mm_loan \
    --usd 100000 --expires 2027-01-31 --source "MM A agreement §4"
defi-tracker mandate-report --month 2026-09   # → reports/mandate-2026-09.html, written once
```

**Surfaces**

```bash
defi-tracker web --out dashboard.html --open  # self-contained page
defi-tracker serve --port 8787 --open         # localhost, re-read per request
```

`serve` binds `127.0.0.1` and answers only loopback `Host` headers. The page has
no authentication, so binding it elsewhere publishes the book.

**Recovery (idempotent, resumable):** `backfill-snapshots` (missing days from
archive state), `backfill-xyk`, `backfill-gas`, `reset-cursor`, `refresh-tokens`.

## Supported protocols

| Protocol | Chains | Events from | Adapter |
|---|---|---|---|
| MachineX (Uniswap V3 fork + legacy XYK) | peaq | RPC log scan | `adapters/machinex.py` |
| PancakeSwap Infinity (V4-style) | BSC | BSC receipts (subgraph fallback) | `adapters/pancake_infinity.py` |
| Uniswap V3 | Base (more via `UNISWAP_V3_CHAINS`) | subgraph + RPC | `adapters/uniswap_v3.py` |

When a subgraph fails, positions are rebuilt from chain reads instead of being
reported as closed. Such a pass is tagged `rpc_fallback` and flagged by `health`.
Adding a protocol means one file in `adapters/`; see [AGENTS.md](AGENTS.md) and
[PROTOCOLS.md](PROTOCOLS.md).

## Configuration

Settings are environment variables loaded from `.env`; `.env.example` documents
each one. The essentials:

- `GRAPH_API_KEY` — subgraph access.
- `{CHAIN}_RPC_URL`, or one `ALCHEMY_API_KEY` for the chains Alchemy serves —
  JSON-RPC endpoints, with `{CHAIN}_RPC_URL_FALLBACK` as optional backups.
- `TRACKER_DB` — the database path.
- `POLICY_FILE` — mandate limits.
- `SLACK_WEBHOOK_URL` — delivery.
- `COINWATCH_DB`, `MANDATE_FILE`, `RFQ_FILE` — desk inputs. These are confidential
  and kept out of git (`content/`, `RFQ/`).

## Scheduling

```cron
5 0 * * *  cd /path/to/onchain-monitor && .venv/bin/defi-tracker run  >> logs/run.log 2>&1
20 * * * * cd /path/to/onchain-monitor && .venv/bin/defi-tracker mark >> logs/mark.log 2>&1
```

On macOS, `scripts/launchd/` has equivalent LaunchAgents; edit the paths before
installing them. Scheduled passes wait for the network after a laptop wakes.

## Development

```bash
pytest                     # 700+ tests, no network: every external call is mocked
ruff check defi_tracker tests && ruff format --check defi_tracker tests
mypy defi_tracker
```

CI runs all four on every push. Contribution rules and module conventions (money
as `Decimal`, the 4-tuple primary key, no protocol branching outside adapters)
are in [AGENTS.md](AGENTS.md).

## Roadmap

- Custody reconciliation: the vault-balance and transfer tables exist, and the
  integration that fills them is next.
- More adapters (Uniswap V4, Aave V3) on the shared math.
- Execution stays propose-only: tickets are prepared for a human signer, never sent.

## Documentation

| Document | For |
|---|---|
| [docs/METHODOLOGY.md](docs/METHODOLOGY.md) | The liquidity-management method behind the code |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, data flow, storage, failure and security model, extension points |
| [AGENTS.md](AGENTS.md) | Contributor conventions, module map, environment variables |
| [PROTOCOLS.md](PROTOCOLS.md) | Contract addresses and data sources per protocol |

## License

MIT
