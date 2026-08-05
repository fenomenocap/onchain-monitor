"""
Concentrated-liquidity math, shared across Uni V3, Uni V4, and Pancake Infinity.

This is the canonical V3-style math. All three protocols use the SAME
invariants — sqrt-price ticks, feeGrowthInside, liquidity * delta / Q128 —
they just live in different contracts. By isolating it here, the per-protocol
adapters become thin wrappers over RPC.

If you add a new CL fork (e.g. Algebra, KyberSwap CL), it almost certainly
plugs in here without touching the math.
"""

from __future__ import annotations

import math
from decimal import Decimal

# eth-hash is the Ethereum ecosystem's standard keccak wrapper. We declare
# eth-hash[pycryptodome] in pyproject.toml; this pulls in pycryptodome as the
# backend (actively maintained, recommended by EF tooling). Ethereum uses
# original Keccak-256 — NOT NIST SHA-3 (different padding), so Python's
# stdlib hashlib.sha3_256 is NOT a substitute.
from eth_hash.auto import keccak as _keccak256_fn

from defi_tracker.adapters._rpc import RpcClient

Q96 = 2**96
Q128 = 2**128
MAX_U256 = 2**256


def _keccak256(data: bytes) -> bytes:
    return bytes(_keccak256_fn(data))


# ── Tick math ──────────────────────────────────────────────────────────────


def tick_to_sqrt_price(tick: int) -> int:
    """
    Compute sqrtPriceX96 for a given tick.

    NOTE: Uses float math like your existing script. This is fine for display
    and for amounts where you can tolerate ~1bp drift, but at extreme ticks
    or for accounting-grade precision, port the Uniswap reference
    getSqrtRatioAtTick (integer-only, exact). See:
    https://github.com/Uniswap/v3-core/blob/main/contracts/libraries/TickMath.sol
    """
    return int(math.sqrt(1.0001**tick) * Q96)


def amounts_from_liquidity(
    sqrt_price_x96: int,
    sqrt_lower_x96: int,
    sqrt_upper_x96: int,
    liquidity: int,
) -> tuple[int, int]:
    """
    Given a CL position's [lower, upper] range and the current price, return
    (amount0, amount1) in raw token units. Same formula Uniswap V3 uses.
    """
    liquidity = int(liquidity)
    if liquidity <= 0:
        return 0, 0
    if sqrt_price_x96 <= sqrt_lower_x96:
        a0 = (
            liquidity
            * (sqrt_upper_x96 - sqrt_lower_x96)
            // (sqrt_upper_x96 * sqrt_lower_x96 // Q96)
        )
        a1 = 0
    elif sqrt_price_x96 >= sqrt_upper_x96:
        a0 = 0
        a1 = liquidity * (sqrt_upper_x96 - sqrt_lower_x96) // Q96
    else:
        a0 = (
            liquidity
            * (sqrt_upper_x96 - sqrt_price_x96)
            // (sqrt_upper_x96 * sqrt_price_x96 // Q96)
        )
        a1 = liquidity * (sqrt_price_x96 - sqrt_lower_x96) // Q96
    return a0, a1


# ── ABI encoding for eth_call ──────────────────────────────────────────────


def _abi_bytes32(val: str) -> bytes:
    return bytes.fromhex(val.lstrip("0x").zfill(64))


def _abi_address(val: str) -> bytes:
    return bytes.fromhex(val.lstrip("0x").lower().zfill(64))


def _abi_int24(val: int) -> bytes:
    if val < 0:
        val = val & ((1 << 256) - 1)
    return val.to_bytes(32, "big")


def _decode_uint256(data: bytes, offset: int = 0) -> int:
    return int.from_bytes(data[offset : offset + 32], "big")


def _decode_int128(data: bytes, offset: int = 0) -> int:
    val = int.from_bytes(data[offset : offset + 32], "big")
    if val >= (1 << 127):
        val -= 1 << 256
    return val


# ── Selectors (keccak256 of function sigs, first 4 bytes) ─────────────────


def _selector(sig: str) -> bytes:
    return _keccak256(sig.encode())[:4]


# Pancake Infinity & Uni V4 use these signatures on their PoolManager
SEL_V4_GET_POSITION = _selector("getPosition(bytes32,address,int24,int24,bytes32)")
SEL_V4_FEE_GROWTH_GLOBALS = _selector("getFeeGrowthGlobals(bytes32)")
SEL_V4_POOL_TICK_INFO = _selector("getPoolTickInfo(bytes32,int24)")
SEL_V4_GET_SLOT0 = _selector("getSlot0(bytes32)")
SEL_V4_GET_LIQUIDITY = _selector("getLiquidity(bytes32)")


# ── V4-style RPC reads (Pancake Infinity uses these too) ──────────────────


def rpc_get_position(
    rpc: RpcClient,
    pool_manager: str,
    pos_manager: str,
    pool_id: str,
    tick_lower: int,
    tick_upper: int,
    token_id: int,
) -> tuple[int, int, int] | None:
    """
    V4-style getPosition(poolId, owner=positionManager, tickLower, tickUpper, salt=tokenId)
    Returns (liquidity, feeGrowthInside0LastX128, feeGrowthInside1LastX128) or None.
    """
    salt = token_id.to_bytes(32, "big")
    data = (
        SEL_V4_GET_POSITION
        + _abi_bytes32(pool_id)
        + _abi_address(pos_manager)
        + _abi_int24(tick_lower)
        + _abi_int24(tick_upper)
        + salt
    )
    result = rpc.eth_call(pool_manager, data)
    if not result or len(result) < 96:
        return None
    liquidity = _decode_uint256(result, 0) & ((1 << 128) - 1)
    fg_inside0 = _decode_uint256(result, 32)
    fg_inside1 = _decode_uint256(result, 64)
    return liquidity, fg_inside0, fg_inside1


def rpc_get_slot0_v4(
    rpc: RpcClient,
    pool_manager: str,
    pool_id: str,
) -> tuple[int, int] | None:
    """
    V4-style getSlot0(poolId) → (sqrtPriceX96, tick, protocolFee, lpFee).
    Returns (sqrtPriceX96, tick) or None.
    """
    data = SEL_V4_GET_SLOT0 + _abi_bytes32(pool_id)
    result = rpc.eth_call(pool_manager, data)
    if not result or len(result) < 64:
        return None
    sqrt_price_x96 = _decode_uint256(result, 0)
    tick = _decode_int128(result, 32)
    return sqrt_price_x96, tick


def rpc_get_pool_liquidity_v4(
    rpc: RpcClient,
    pool_manager: str,
    pool_id: str,
) -> int | None:
    """V4-style getLiquidity(poolId) → current in-range pool liquidity, or None."""
    data = SEL_V4_GET_LIQUIDITY + _abi_bytes32(pool_id)
    result = rpc.eth_call(pool_manager, data)
    if not result or len(result) < 32:
        return None
    return _decode_uint256(result, 0) & ((1 << 128) - 1)


def rpc_get_fee_growth_globals(
    rpc: RpcClient,
    pool_manager: str,
    pool_id: str,
) -> tuple[int, int] | None:
    data = SEL_V4_FEE_GROWTH_GLOBALS + _abi_bytes32(pool_id)
    result = rpc.eth_call(pool_manager, data)
    if not result or len(result) < 64:
        return None
    return _decode_uint256(result, 0), _decode_uint256(result, 32)


def rpc_get_tick_info(
    rpc: RpcClient,
    pool_manager: str,
    pool_id: str,
    tick: int,
) -> tuple[int, int, int, int] | None:
    data = SEL_V4_POOL_TICK_INFO + _abi_bytes32(pool_id) + _abi_int24(tick)
    result = rpc.eth_call(pool_manager, data)
    if not result or len(result) < 128:
        return None
    liquidity_gross = _decode_uint256(result, 0)
    liquidity_net = _decode_int128(result, 32)
    fg_outside0 = _decode_uint256(result, 64)
    fg_outside1 = _decode_uint256(result, 96)
    return liquidity_gross, liquidity_net, fg_outside0, fg_outside1


# ── The big one: exact unclaimed fees via feeGrowthInside delta ───────────


def compute_unclaimed_fees_rpc(
    rpc: RpcClient,
    pool_manager: str,
    pos_manager: str,
    pool_id: str,
    tick_lower: int,
    tick_upper: int,
    tick_current: int,
    token_id: int,
    net_liquidity: int,
    decimals0: int,
    decimals1: int,
    price0: Decimal,
    price1: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Standard V3/V4 feeGrowthInside math, returning (unclaimed0, unclaimed1, unclaimed_usd).

    The algorithm (identical for Uni V3, Uni V4, and Pancake Infinity, just
    different contract addresses):

      1. Read feeGrowthInside{0,1}LastX128 from the position's state
      2. Read feeGrowthGlobal{0,1}X128 from the pool
      3. Read feeGrowthOutside{0,1}X128 from both tick boundaries
      4. Compute feeGrowthInside (current) using the standard subtraction
      5. delta = feeGrowthInside_now - feeGrowthInside_last
      6. unclaimed_token = liquidity * delta / 2^128 / 10^decimals
    """
    if net_liquidity <= 0:
        return Decimal("0"), Decimal("0"), Decimal("0")

    pos = rpc_get_position(
        rpc, pool_manager, pos_manager, pool_id, tick_lower, tick_upper, token_id
    )
    if not pos:
        return Decimal("0"), Decimal("0"), Decimal("0")
    _, fg_inside0_last, fg_inside1_last = pos

    globals_ = rpc_get_fee_growth_globals(rpc, pool_manager, pool_id)
    if not globals_:
        return Decimal("0"), Decimal("0"), Decimal("0")
    fg_global0, fg_global1 = globals_

    lower = rpc_get_tick_info(rpc, pool_manager, pool_id, tick_lower)
    upper = rpc_get_tick_info(rpc, pool_manager, pool_id, tick_upper)
    if not lower or not upper:
        return Decimal("0"), Decimal("0"), Decimal("0")
    _, _, fg_out0_lower, fg_out1_lower = lower
    _, _, fg_out0_upper, fg_out1_upper = upper

    # Below / above feeGrowth, per the V3 paper
    if tick_current >= tick_lower:
        fg_below0, fg_below1 = fg_out0_lower, fg_out1_lower
    else:
        fg_below0 = (fg_global0 - fg_out0_lower) % MAX_U256
        fg_below1 = (fg_global1 - fg_out1_lower) % MAX_U256

    if tick_current < tick_upper:
        fg_above0, fg_above1 = fg_out0_upper, fg_out1_upper
    else:
        fg_above0 = (fg_global0 - fg_out0_upper) % MAX_U256
        fg_above1 = (fg_global1 - fg_out1_upper) % MAX_U256

    fg_inside0 = (fg_global0 - fg_below0 - fg_above0) % MAX_U256
    fg_inside1 = (fg_global1 - fg_below1 - fg_above1) % MAX_U256

    delta0 = (fg_inside0 - fg_inside0_last) % MAX_U256
    delta1 = (fg_inside1 - fg_inside1_last) % MAX_U256

    # Sanity: huge underflowed deltas mean fees-accrued-zero; cap them
    if delta0 > MAX_U256 // 2:
        delta0 = 0
    if delta1 > MAX_U256 // 2:
        delta1 = 0

    unc0 = Decimal(net_liquidity * delta0) / Decimal(Q128) / Decimal(10**decimals0)
    unc1 = Decimal(net_liquidity * delta1) / Decimal(Q128) / Decimal(10**decimals1)

    # Catch absurd overflow before it pollutes USD math
    if unc0 > Decimal("1e9") or unc1 > Decimal("1e9"):
        return Decimal("0"), Decimal("0"), Decimal("0")

    unc_usd = unc0 * price0 + unc1 * price1
    return unc0, unc1, unc_usd


# ── V3-style selectors ────────────────────────────────────────────────────
# Uni V3 / PancakeSwap V3 / MachineX (Ramses V3 fork) — NonfungiblePositionManager
# and Pool contracts have different ABIs from V4's PoolManager.

SEL_V3_POSITIONS = _selector("positions(uint256)")
SEL_V3_BALANCE_OF = _selector("balanceOf(address)")
SEL_V3_TOKEN_OF_OWNER_BY_INDEX = _selector("tokenOfOwnerByIndex(address,uint256)")
SEL_V3_GET_POOL = _selector("getPool(address,address,uint24)")
SEL_V3_GET_POOL_INT24 = _selector(
    "getPool(address,address,int24)"
)  # MachineX: third arg is tickSpacing
SEL_V3_SLOT0 = _selector("slot0()")
SEL_V3_FEE_GROWTH_GLOBAL0 = _selector("feeGrowthGlobal0X128()")
SEL_V3_FEE_GROWTH_GLOBAL1 = _selector("feeGrowthGlobal1X128()")
SEL_V3_TICKS = _selector("ticks(int24)")

# Event topic hashes for eth_getLogs
TOPIC_V3_INCREASE_LIQUIDITY = _keccak256(
    b"IncreaseLiquidity(uint256,uint128,uint256,uint256)"
).hex()
TOPIC_V3_DECREASE_LIQUIDITY = _keccak256(
    b"DecreaseLiquidity(uint256,uint128,uint256,uint256)"
).hex()
TOPIC_V3_COLLECT = _keccak256(b"Collect(uint256,address,uint256,uint256)").hex()

# Standard ERC20 Transfer(address,address,uint256) — used to measure actual
# fee payouts for pure-harvest calls (liquidity unchanged), since neither the
# PancakeSwap Infinity subgraph nor the V3-style Collect event cleanly
# isolates "fees only" when principal and fees are swept in the same call.
TOPIC_ERC20_TRANSFER = _keccak256(b"Transfer(address,address,uint256)").hex()


def sum_transfers_to(receipt: dict, token_address: str, recipient: str) -> int:
    """
    Sum raw ERC20 Transfer amounts for `token_address` paid to `recipient`
    in a transaction receipt. Returns 0 if the receipt has no matching logs.
    """
    token_address = token_address.lower()
    recipient_topic = "0" * 24 + recipient.lower().removeprefix("0x")
    total = 0
    for log in receipt.get("logs", []):
        if log.get("address", "").lower() != token_address:
            continue
        topics = log.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != "0x" + TOPIC_ERC20_TRANSFER:
            continue
        if topics[2].lower().removeprefix("0x") != recipient_topic:
            continue
        total += int(log.get("data", "0x0"), 16)
    return total


def _abi_uint256(val: int) -> bytes:
    return val.to_bytes(32, "big")


def _decode_address(data: bytes, offset: int = 0) -> str:
    """Decode an ABI-encoded address (last 20 bytes of a 32-byte slot)."""
    return "0x" + data[offset + 12 : offset + 32].hex().lower()


# ── V3-style RPC reads ─────────────────────────────────────────────────────


def rpc_enumerate_positions_v3(
    rpc: RpcClient,
    nft_manager: str,
    wallet: str,
) -> list[int]:
    """
    Return all tokenIds currently owned by wallet on the NonfungiblePositionManager.

    Calls balanceOf(wallet) → count, then tokenOfOwnerByIndex(wallet, i) for each index.
    Only returns CURRENT positions — exited positions (NFT transferred away) are not included.
    """
    bal_data = SEL_V3_BALANCE_OF + _abi_address(wallet)
    bal_result = rpc.eth_call(nft_manager, bal_data)
    if not bal_result or len(bal_result) < 32:
        return []
    count = _decode_uint256(bal_result, 0)
    if count == 0:
        return []

    token_ids: list[int] = []
    for i in range(count):
        idx_data = SEL_V3_TOKEN_OF_OWNER_BY_INDEX + _abi_address(wallet) + _abi_uint256(i)
        idx_result = rpc.eth_call(nft_manager, idx_data)
        if idx_result and len(idx_result) >= 32:
            token_ids.append(_decode_uint256(idx_result, 0))
    return token_ids


def rpc_get_position_v3(
    rpc: RpcClient,
    nft_manager: str,
    token_id: int,
    nft_header_words: int = 2,
) -> tuple[str, str, int, int, int, int, int, int] | None:
    """
    Call positions(tokenId) on the NonfungiblePositionManager.

    Returns (token0, token1, fee, tickLower, tickUpper, liquidity,
             feeGrowthInside0LastX128, feeGrowthInside1LastX128)
    or None on failure.

    Standard Uni V3 ABI layout (nft_header_words=2, each slot = 32 bytes):
      0: nonce (uint96)
      1: operator (address)
      2: token0 (address)
      3: token1 (address)
      4: fee (uint24)
      5: tickLower (int24) — signed
      6: tickUpper (int24) — signed
      7: liquidity (uint128)
      8: feeGrowthInside0LastX128 (uint256)
      9: feeGrowthInside1LastX128 (uint256)

    MachineX / Ramses-lite ABI (nft_header_words=0) drops nonce+operator:
      0: token0, 1: token1, 2: fee, 3: tickLower, 4: tickUpper,
      5: liquidity, 6: feeGrowthInside0LastX128, 7: feeGrowthInside1LastX128
    """
    data = SEL_V3_POSITIONS + _abi_uint256(token_id)
    result = rpc.eth_call(nft_manager, data)
    base = nft_header_words * 32
    if not result or len(result) < base + 256:
        return None

    token0 = _decode_address(result, base)
    token1 = _decode_address(result, base + 32)
    fee = _decode_uint256(result, base + 64)
    tick_lower = _decode_int128(result, base + 96)
    tick_upper = _decode_int128(result, base + 128)
    liquidity = _decode_uint256(result, base + 160) & ((1 << 128) - 1)
    fg0_last = _decode_uint256(result, base + 192)
    fg1_last = _decode_uint256(result, base + 224)

    return token0, token1, fee, tick_lower, tick_upper, liquidity, fg0_last, fg1_last


def rpc_get_pool_address_v3(
    rpc: RpcClient,
    factory: str,
    token0: str,
    token1: str,
    fee: int,
    int24_third_arg: bool = False,
) -> str | None:
    """
    Call getPool(token0, token1, fee) on the V3 factory. Returns pool address or None.

    Set int24_third_arg=True for MachineX/Ramses factories whose getPool signature uses
    int24 (tickSpacing) as the third argument instead of uint24 (fee).
    """
    sel = SEL_V3_GET_POOL_INT24 if int24_third_arg else SEL_V3_GET_POOL
    data = sel + _abi_address(token0) + _abi_address(token1) + _abi_uint256(fee)
    result = rpc.eth_call(factory, data)
    if not result or len(result) < 32:
        return None
    addr = _decode_address(result, 0)
    # Zero address means pool doesn't exist
    return None if addr == "0x" + "0" * 40 else addr


def rpc_get_slot0_v3(rpc: RpcClient, pool: str) -> tuple[int, int] | None:
    """
    Call slot0() on a V3 pool. Returns (sqrtPriceX96, tick) or None.

    ABI layout:
      slot 0: sqrtPriceX96 (uint160)
      slot 1: tick (int24, signed)
    """
    result = rpc.eth_call(pool, SEL_V3_SLOT0)
    if not result or len(result) < 64:
        return None
    sqrt_price_x96 = _decode_uint256(result, 0)
    tick = _decode_int128(result, 32)
    return sqrt_price_x96, tick


def rpc_get_fee_growth_globals_v3(
    rpc: RpcClient,
    pool: str,
) -> tuple[int, int] | None:
    """
    Read feeGrowthGlobal0X128 and feeGrowthGlobal1X128 from a V3 pool.
    Makes two separate eth_calls (V3 exposes them as individual view functions).
    """
    r0 = rpc.eth_call(pool, SEL_V3_FEE_GROWTH_GLOBAL0)
    r1 = rpc.eth_call(pool, SEL_V3_FEE_GROWTH_GLOBAL1)
    if not r0 or len(r0) < 32 or not r1 or len(r1) < 32:
        return None
    return _decode_uint256(r0, 0), _decode_uint256(r1, 0)


def rpc_get_tick_info_v3(
    rpc: RpcClient,
    pool: str,
    tick: int,
) -> tuple[int, int, int, int] | None:
    """
    Call ticks(tick) on a V3 pool.

    Returns (liquidityGross, liquidityNet, feeGrowthOutside0X128, feeGrowthOutside1X128).

    ABI layout:
      slot 0: liquidityGross (uint128)
      slot 1: liquidityNet (int128, signed)
      slot 2: feeGrowthOutside0X128 (uint256)
      slot 3: feeGrowthOutside1X128 (uint256)
    """
    data = SEL_V3_TICKS + _abi_int24(tick)
    result = rpc.eth_call(pool, data)
    if not result or len(result) < 128:
        return None
    liq_gross = _decode_uint256(result, 0) & ((1 << 128) - 1)
    liq_net = _decode_int128(result, 32)
    fg_outside0 = _decode_uint256(result, 64)
    fg_outside1 = _decode_uint256(result, 96)
    return liq_gross, liq_net, fg_outside0, fg_outside1


def compute_unclaimed_fees_pure(
    liquidity: int,
    fg_global0: int,
    fg_global1: int,
    fg_out0_lower: int,
    fg_out1_lower: int,
    fg_out0_upper: int,
    fg_out1_upper: int,
    fg_inside0_last: int,
    fg_inside1_last: int,
    tick_current: int,
    tick_lower: int,
    tick_upper: int,
    decimals0: int,
    decimals1: int,
    price0: Decimal,
    price1: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Standard V3 feeGrowthInside math from pre-fetched fee growth data (subgraph or RPC).
    Returns (unclaimed_token0, unclaimed_token1, unclaimed_usd).
    """
    if liquidity <= 0:
        return Decimal("0"), Decimal("0"), Decimal("0")

    if tick_current >= tick_lower:
        fg_below0, fg_below1 = fg_out0_lower, fg_out1_lower
    else:
        fg_below0 = (fg_global0 - fg_out0_lower) % MAX_U256
        fg_below1 = (fg_global1 - fg_out1_lower) % MAX_U256

    if tick_current < tick_upper:
        fg_above0, fg_above1 = fg_out0_upper, fg_out1_upper
    else:
        fg_above0 = (fg_global0 - fg_out0_upper) % MAX_U256
        fg_above1 = (fg_global1 - fg_out1_upper) % MAX_U256

    fg_inside0 = (fg_global0 - fg_below0 - fg_above0) % MAX_U256
    fg_inside1 = (fg_global1 - fg_below1 - fg_above1) % MAX_U256

    delta0 = (fg_inside0 - fg_inside0_last) % MAX_U256
    delta1 = (fg_inside1 - fg_inside1_last) % MAX_U256

    if delta0 > MAX_U256 // 2:
        delta0 = 0
    if delta1 > MAX_U256 // 2:
        delta1 = 0

    unc0 = Decimal(liquidity * delta0) / Decimal(Q128) / Decimal(10**decimals0)
    unc1 = Decimal(liquidity * delta1) / Decimal(Q128) / Decimal(10**decimals1)

    if unc0 > Decimal("1e9") or unc1 > Decimal("1e9"):
        return Decimal("0"), Decimal("0"), Decimal("0")

    unc_usd = unc0 * price0 + unc1 * price1
    return unc0, unc1, unc_usd


def compute_unclaimed_fees_v3(
    rpc: RpcClient,
    factory: str,
    nft_manager: str,
    token_id: int,
    tick_current: int,
    decimals0: int,
    decimals1: int,
    price0: Decimal,
    price1: Decimal,
    nft_header_words: int = 2,
    int24_third_arg: bool = False,
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Standard feeGrowthInside math for V3-style contracts (Uni V3, MachineX, PancakeSwap V3).

    Same algorithm as compute_unclaimed_fees_rpc but reads from V3 Pool / NFT Manager
    instead of V4 PoolManager.

    Returns (unclaimed_token0, unclaimed_token1, unclaimed_usd).
    Pass nft_header_words=0 for MachineX/Ramses-lite NFT managers that omit nonce+operator.
    Pass int24_third_arg=True for MachineX factory whose getPool uses int24 (tickSpacing).
    """
    pos = rpc_get_position_v3(rpc, nft_manager, token_id, nft_header_words=nft_header_words)
    if not pos:
        return Decimal("0"), Decimal("0"), Decimal("0")
    token0_addr, token1_addr, fee, tick_lower, tick_upper, liquidity, fg0_last, fg1_last = pos

    if liquidity <= 0:
        return Decimal("0"), Decimal("0"), Decimal("0")

    pool = rpc_get_pool_address_v3(
        rpc, factory, token0_addr, token1_addr, fee, int24_third_arg=int24_third_arg
    )
    if not pool:
        return Decimal("0"), Decimal("0"), Decimal("0")

    globals_ = rpc_get_fee_growth_globals_v3(rpc, pool)
    if not globals_:
        return Decimal("0"), Decimal("0"), Decimal("0")
    fg_global0, fg_global1 = globals_

    lower = rpc_get_tick_info_v3(rpc, pool, tick_lower)
    upper = rpc_get_tick_info_v3(rpc, pool, tick_upper)
    if not lower or not upper:
        return Decimal("0"), Decimal("0"), Decimal("0")
    _, _, fg_out0_lower, fg_out1_lower = lower
    _, _, fg_out0_upper, fg_out1_upper = upper

    # feeGrowthInside calculation — identical V3/V4 math
    if tick_current >= tick_lower:
        fg_below0, fg_below1 = fg_out0_lower, fg_out1_lower
    else:
        fg_below0 = (fg_global0 - fg_out0_lower) % MAX_U256
        fg_below1 = (fg_global1 - fg_out1_lower) % MAX_U256

    if tick_current < tick_upper:
        fg_above0, fg_above1 = fg_out0_upper, fg_out1_upper
    else:
        fg_above0 = (fg_global0 - fg_out0_upper) % MAX_U256
        fg_above1 = (fg_global1 - fg_out1_upper) % MAX_U256

    fg_inside0 = (fg_global0 - fg_below0 - fg_above0) % MAX_U256
    fg_inside1 = (fg_global1 - fg_below1 - fg_above1) % MAX_U256

    delta0 = (fg_inside0 - fg0_last) % MAX_U256
    delta1 = (fg_inside1 - fg1_last) % MAX_U256

    if delta0 > MAX_U256 // 2:
        delta0 = 0
    if delta1 > MAX_U256 // 2:
        delta1 = 0

    unc0 = Decimal(liquidity * delta0) / Decimal(Q128) / Decimal(10**decimals0)
    unc1 = Decimal(liquidity * delta1) / Decimal(Q128) / Decimal(10**decimals1)

    if unc0 > Decimal("1e9") or unc1 > Decimal("1e9"):
        return Decimal("0"), Decimal("0"), Decimal("0")

    unc_usd = unc0 * price0 + unc1 * price1
    return unc0, unc1, unc_usd
