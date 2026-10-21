# Architecture

This document explains how On-chain Monitor turns chain state into a book a
treasury can be held to: what each layer does, how data moves, where it is
stored, and how the system behaves when a source fails. For the "why" and the
stakeholders, start with the [README](../README.md); for the method the system
enforces, see [METHODOLOGY.md](METHODOLOGY.md). For coding conventions, see
[AGENTS.md](../AGENTS.md).

## 1. The problem the design answers

A treasury's liquidity book has to be **accurate over time**, **explainable**
and **auditable against a mandate**. Three choices follow from that:

| Requirement | Design choice |
|---|---|
| Cost basis must not drift as prices move | Store every liquidity event once, priced in USD **at its block**, and never rewrite it |
| Every number must be explainable | Compute results from stored events, snapshots and marks, with no hidden state or estimates silently filling gaps |
| Results are judged against limits and reported monthly | Make the mandate itself data (policy limits, exceptions, incidents, contracts) and render reports from the same gathered book as every other surface |

## 2. Layers

```
surfaces     cli.py · web.py · serve.py · slack.py · mandate.py
                 │  render
assembly     report.py ── gather_report_data() → ReportData (one per build)
                 │  reads storage, calls analytics with rows + price maps
orchestration core/runner.py ── run() / mark_all() / reconstruct_day()
                 │  drives adapters, writes storage, emits alerts
analytics    analytics.py · alerts.py · policy.py · costs.py · poolflow.py
                 │  pure functions over normalized rows (no fetching)
adapters     adapters/{machinex, pancake_infinity, uniswap_v3}.py
                 │  chain/subgraph shapes → core/types
             shared: _cl_math (tick math, fee growth) · _rpc (JSON-RPC client)
                     _pricing (historical prices) · _pool_activity (pool fees)
core         types.py · storage.py · adapter.py (contract + registry) · endpoints.py
```

Dependencies point downward only. Two rules keep the layers honest:

- **Adapters emit only `core/types`** (`Position`, `Event`, `Token`, `PoolState`…).
  Nothing above the adapter layer knows what a subgraph or a log topic is.
- **Analytics never does I/O.** It takes storage rows and price maps as
  arguments. Prices, venues and order books are fetched at the edges (the runner
  and page builds) and handed in.

Protocol behaviour is dispatched on `ProtocolKind` (CL, XYK, lending…), never on
`protocol_id`. Protocol-specific decisions live behind methods on
`ProtocolAdapter`.

## 3. Data flow

### The daily pass (`run`)

```
for each wallet × chain × registered adapter:
  1. sync      adapter.iter_event_batches(since=cursor)
               → events (deposit / withdraw / collect), USD at block time
               → storage.upsert_events() — idempotent on tx_hash:log_index
               → cursor banked only after each batch is persisted
  2. snapshot  adapter.fetch_positions()
               → cost basis + IL + P&L from the event log (analytics)
               → storage.write_snapshot() — one row per position per day
               → positions missing from the answer are tombstoned as closed
  3. mark      one timestamped valuation per position (also written hourly by `mark`)
  4. health    pipeline_health: last clean sync / snapshot / source / error
then, once per pass:
  5. pool history and fee elasticity refreshed (poolflow)
  6. alerts    per-position (range, IL, carry, staleness), rebalance tickets,
               and policy breaches — each pass isolated, deduplicated in storage
  7. gas       receipts read for unpriced transactions (bounded, idempotent)
  8. delivery  Slack report + undelivered alerts, if configured
```

Because a missing position is read as **closed**, adapters must raise when they
cannot establish what is held. A failed read becomes a failed snapshot, recorded
in `pipeline_health`, and never an empty answer (see §6).

### The page build (`web`, `serve`, Slack, monthly report)

`report.gather_report_data()` reads everything once — snapshots, events, period
P&L, risk, mandate state, action queue, elasticity, backtest, contracts — plus the
market reads passed in from outside (current prices, CoinGecko venues, exchange
order books, Coinwatch delivery history). Every surface renders the same
`ReportData`, so the HTML page, the Slack post and the CLI cannot disagree.

## 4. Storage model

One SQLite file (`TRACKER_DB`), with raw SQL in `core/storage.py` and
idempotent migrations keyed by a schema version. Money is stored as `REAL` at
this boundary and handled as `Decimal` everywhere above it.

| Group | Tables | Notes |
|---|---|---|
| Identity | `wallets`, `tokens` | Addresses lowercased on input; token decimals are never guessed |
| Ledger of record | `events`, `tx_costs` | Append-only. `event_uid = tx_hash:log_index`; gas keyed per transaction so it is paid once |
| Time series | `snapshots` (daily, with derived analytics), `marks` (timestamped, narrow) | Separate on purpose: hourly marks would otherwise overwrite intraday history |
| Pool context | `pool_days`, `price_cache`, `kv_store` | Daily pool fees and the book's own depth; historical prices; cursors |
| Operations | `sync_state`, `pipeline_health`, `alerts`, `rebalance_queue` | Liveness, alert dedup, ticket lifecycle (open / snoozed / done) |
| Mandate | `policy_exceptions`, `incidents`, `contracts`, `contract_tranches` | Breach episodes with review sign-off; incidents; versioned counterparty contracts and option tranches |
| Custody | `transfers`, `vault_balances` | Schema for vault reconciliation, ready for a custody integration |

The primary key everywhere is **`(wallet, chain, protocol_id, position_key)`**.
A position key alone is not unique: on Uniswap V3 it is `pool:tickLower:tickUpper`,
which every wallet in the same range shares.

## 5. Analytics, in brief

- **Cost basis and IL.** Net deposited tokens and USD from the event log. IL is
  measured against holding those tokens: `value − hold`.
- **Period P&L.** For any window, value at both ends plus flows within it, so
  deposits never read as profit. Split into market move, IL, fees earned
  (collected + Δunclaimed) and gas. Positions that cannot be valued at both ends
  are listed as excluded, with their value.
- **Fees by accrual month.** Harvest sweeps that collect a prior month's accrual
  are split back to the month that earned them.
- **Risk.** A concentrated-liquidity position modelled from its range and
  liquidity: price-shock grid, delta and gamma, probability of exiting the range
  over a horizon, and the fee yield that breaks even.
- **Range optimiser.** Per pool, the symmetric band that maximises expected fees
  minus expected IL. Volume's response to depth (elasticity ε) is measured from
  the pool's own history and carried as a band rather than a point.
- **Rebalance tickets.** Propose-only actions ranked by expected value, each with
  its robustness to σ and ε. A backtest replays the optimiser against the book as
  it actually stood.
- **Mandate checks.** Concentration, IL, out-of-range share, depeg and
  counterparty limits from `policy.toml`. A limit is NOT SET until configured,
  and a breach opens an exception episode that stays open until a reading clears it.
- **Market-maker book.** Loans and option tranches marked at the current token
  price (market read, cross-checked against the book's own pools). Delivery comes
  from Coinwatch history and is checked independently against each exchange's
  public order book. RFQ bids are priced for their term and per $1k of ±2% depth.

## 6. Failure model

The system is built to be **wrong loudly, never quietly**:

| Situation | Behaviour |
|---|---|
| Subgraph down | Positions rebuilt from chain reads (`rpc_fallback`), flagged in `health` |
| Any position/pool read fails | Adapter raises: no snapshot that day, not a tombstone |
| Unclaimed fees unreadable | Stored as unknown; yields and P&L that depend on it are excluded, not zeroed |
| Event scan incomplete or a token unreadable | Cursor held. `last_scan_reached_head` is false, so periods ending after it are excluded |
| RPC rate-limited | Back off and retry; never treated as an empty log range |
| Policy file malformed, unreadable, or named but missing | `POLICY_BREACH` alert, not "no limits" |
| Laptop wakes without network | Scheduled passes wait, then exit with a temporary-failure code |
| One alert pass errors | The others still run |

## 7. Security model

- **No keys, no transactions.** The tool is read-only on every chain.
- **Secrets stay out of output.** RPC and subgraph URLs carry API keys in their
  paths, so `core.endpoints.redact()` strips them from every error before it is
  logged, printed or stored.
- **Local surfaces.** `serve` binds loopback and refuses non-loopback `Host`
  headers (DNS-rebinding guard). The static page needs no server at all.
- **Confidential inputs stay out of git.** Contract documents, desk content and
  incoming quotes live in gitignored folders and the local database.

## 8. Extension points

| To add… | Do this |
|---|---|
| A protocol | One `adapters/<name>.py` subclassing `ProtocolAdapter`; reuse `_cl_math` for any CL fork; `_autoregister()` gated on env vars; import it in `cli.py`; tests with mocked HTTP |
| A chain | An entry in `core/endpoints.ENV_VAR` (and Alchemy network, if served) plus the adapter's chain config |
| An analytic | A pure function in `analytics.py` over `core/types` rows and price maps; render it from `ReportData` |
| A mandate limit | An entry in `policy.LIMITS` and a check in `analytics.policy_checks`; it reads NOT SET until configured |
| A surface | Consume `ReportData`; never query storage directly from a renderer |
