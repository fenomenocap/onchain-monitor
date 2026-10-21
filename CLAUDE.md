# CLAUDE.md

**Read [AGENTS.md](./AGENTS.md) first.** It is the source of truth for architecture, conventions, env vars, and how to extend this repo. [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) explains layers, data flow and the failure model; [docs/METHODOLOGY.md](./docs/METHODOLOGY.md) explains the liquidity-management method the code enforces.

## What this is

On-chain Monitor is a local liquidity desk for a token treasury. It tracks protocol-owned liquidity across wallets, chains and DEXes, prices every liquidity event in USD at event time for accurate cost basis, and computes impermanent loss vs holding, period P&L with flows netted, risk, and propose-only rebalance tickets. It holds the book against a written mandate and tracks market-maker contracts and delivery beside it. Everything runs locally — no service, no custody, read-only RPC access.

Currently supports MachineX (peaq), PancakeSwap Infinity (BSC) and Uniswap V3 (Base); new protocols are added via thin adapters on shared math.

**This repo is public:** never commit wallet addresses, transaction hashes, counterparty names, deal terms, book figures or private links (see AGENTS.md → Confidentiality).
