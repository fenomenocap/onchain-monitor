# CLAUDE.md

**Read [AGENTS.md](./AGENTS.md) first.** It is the source of truth for architecture, conventions, env vars, and how to extend this repo.

## What this is

On-chain Monitor is a personal CLI tool for multi-wallet, multi-chain DeFi position tracking. It syncs liquidity events into a local SQLite database, prices them in USD at event time for accurate cost basis, and computes impermanent loss, MTD/MoM PnL, and rebalancing alerts. Everything runs locally — no service, no custody, read-only RPC access.

Currently supports PancakeSwap Infinity on BSC; new protocols are added via thin adapters on shared math.
