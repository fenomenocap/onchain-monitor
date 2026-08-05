# Protocol reference

Contract addresses and data sources for planned adapters.
Use these when implementing the stubs in `adapters/`.

## Uniswap V3

CL_AMM · factory + NonfungiblePositionManager + The Graph subgraph ID per chain.

| Chain    | Factory | Position Manager | Subgraph ID |
|----------|---------|-----------------|-------------|
| Ethereum | `0x1F98431c8aD98523631AE4a59f267346ea31F984` | `0xC36442b4a4522E871399CD717aBDD847Ab11FE88` | `5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV` |
| Arbitrum | `0x1F98431c8aD98523631AE4a59f267346ea31F984` | `0xC36442b4a4522E871399CD717aBDD847Ab11FE88` | `FbCGRftH4a3yZugY7TnbYgPJVEv2LvMT6oF1fxPe9aJM` |
| Optimism | `0x1F98431c8aD98523631AE4a59f267346ea31F984` | `0xC36442b4a4522E871399CD717aBDD847Ab11FE88` | `Cghf4LfVqPiFw6fp6Y5X5Ubc8UpmUhSfJL82zwiBFLaj` |
| Base     | `0x33128a8fC17869897dcE68Ed026d694621f6FDfD` | `0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1` | `HMuAwufqZ1YCRmzL2SfHTVkzZovC9VL2UAKhjvRqKiR1` |
| Polygon  | `0x1F98431c8aD98523631AE4a59f267346ea31F984` | `0xC36442b4a4522E871399CD717aBDD847Ab11FE88` | `3hCPRGf4z88VC5rsBKU5AA9FBBq5nF3jbKJG7VZCbhjm` |

Implementation notes:
- Use `SEL_V3_POSITIONS` ABI (`positions(uint256 tokenId)` on NonfungiblePositionManager) — differs from V4's `getPosition`
- `feeGrowthGlobal` lives on each pool contract, not a singleton manager
- Tick math and `amounts_from_liquidity` from `_cl_math.py` are reusable unchanged

## Aave V3

LENDING · Pool contract per chain. No ticks, no fees, no IL.

| Chain     | Pool |
|-----------|------|
| Ethereum  | `0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2` |
| Arbitrum  | `0x794a61358D6845594F94dc1DB02A252b5b4814aD` |
| Optimism  | `0x794a61358D6845594F94dc1DB02A252b5b4814aD` |
| Base      | `0xA238Dd80C259a72e81d7e4664a9801593F98d1c5` |
| Polygon   | `0x794a61358D6845594F94dc1DB02A252b5b4814aD` |
| Avalanche | `0x794a61358D6845594F94dc1DB02A252b5b4814aD` |

Implementation notes:
- `fetch_events()` emits `DEPOSIT`/`WITHDRAW`/`BORROW`/`REPAY`; no `COLLECT` (interest accrues into aToken balance)
- `fetch_positions()` → one `Position` per asset-market; `protocol_kind = LENDING`
- IL math returns `(0, cost_basis)` — already handled by the `LENDING` branch in `analytics.py`
- Data sources: Aave `UiPoolDataProvider` for live state; Aave subgraph for event history
