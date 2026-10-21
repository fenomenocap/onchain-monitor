"""Pancake Infinity events decoded straight from BSC receipts — no subgraph.

The Pancake Infinity subgraph has returned a deterministic indexing error
since 2026-07-06 and nothing replaces it, so everything after that date —
deposits, withdrawals, the monthly fee sweeps — was simply missing. The chain
has all of it, and more exactly than the subgraph did:

- CLPositionManager emits `ModifyLiquidity(uint256 indexed tokenId,
  int256 liquidityChange, BalanceDelta feesAccrued)` for every change to a
  position NFT, with the fees it paid out EXACT in raw token units.
- CLPoolManager emits `ModifyLiquidity(PoolId indexed id, address indexed
  sender, int24 tickLower, int24 tickUpper, int256 liquidityDelta,
  bytes32 salt)` in the same transaction, salt = the tokenId. Its log index
  is the one the subgraph used, so a chain-read event and a subgraph-read
  event share an event_uid and deduplicate.
- The wallet's ERC20 Transfers in the receipt are what it actually paid or
  received. Settlement nets fees against principal (an increase pays
  principal − fees; a decrease receives principal + fees), so
  principal = net paid by the wallet + fees accrued — exact, with no price
  read at a historical block.

Verified against live transactions: a fee sweep (feesAccrued equals the two
Transfers to the wallet to the wei) and a liquidity increase.

Pure functions only; the adapter does the I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eth_hash.auto import keccak


def _topic(signature: str) -> str:
    return "0x" + keccak(signature.encode()).hex()


PM_MODIFY_TOPIC = _topic("ModifyLiquidity(uint256,int256,int256)")
POOL_MODIFY_TOPIC = _topic("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")
TRANSFER_TOPIC = _topic("Transfer(address,address,uint256)")


def _words(data: str) -> list[int]:
    raw = data[2:] if data.startswith("0x") else data
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def _signed(v: int, bits: int = 256) -> int:
    return v - (1 << bits) if v >= 1 << (bits - 1) else v


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


def uint_topic(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


@dataclass
class DecodedChange:
    """One position NFT's change inside one transaction, all in raw units."""

    tx_hash: str
    block_number: int
    log_index: int  # the CLPoolManager log — the subgraph's logIndex
    token_id: int
    pool_id: str
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    fees: tuple[int, int]  # (token0, token1) paid out to the position
    principal: tuple[int, int] | None  # signed: + deposited, − withdrawn; None = unknowable
    tokens: tuple[str, str] | None  # (currency0, currency1), lowercased, when resolvable
    notes: list[str] = field(default_factory=list)


def decode_receipt(
    receipt: dict,
    wallet: str,
    position_manager: str,
    pool_manager: str,
    known_tokens: dict[str, tuple[str, str]] | None = None,
) -> list[DecodedChange]:
    """Every position change in `receipt` that belongs to `wallet`.

    `known_tokens` maps pool id → (currency0, currency1) for pools already
    seen; an unseen pool's currencies are read from the ERC20 Transfers the
    wallet made or received (sorted, as Infinity orders them).

    Principal needs the wallet's net token flow, which is per TRANSACTION.
    When one tx changes the liquidity of two positions, the flow cannot be
    attributed between them, so their principal is None (the caller drops
    them, counted) — fees are still exact per position.
    """
    wallet = wallet.lower()
    pm, pool_mgr = position_manager.lower(), pool_manager.lower()
    known_tokens = known_tokens or {}
    logs = receipt.get("logs", [])
    tx_hash = receipt["transactionHash"].lower()
    block = int(receipt["blockNumber"], 16)
    if receipt.get("from", "").lower() != wallet:
        return []  # only the wallet's own transactions move its positions

    pool_logs: dict[int, dict] = {}
    for lg in logs:
        t = lg.get("topics", [])
        if lg.get("address", "").lower() == pool_mgr and t and t[0].lower() == POOL_MODIFY_TOPIC:
            w = _words(lg["data"])
            salt = w[3]
            pool_logs[salt] = {
                "log_index": int(lg["logIndex"], 16),
                "pool_id": t[1].lower(),
                "tick_lower": _signed(w[0]),
                "tick_upper": _signed(w[1]),
                "liquidity_delta": _signed(w[2]),
            }

    # The wallet's net ERC20 flow per token: + paid out, − received.
    net_out: dict[str, int] = {}
    wallet_topic = address_topic(wallet)
    for lg in logs:
        t = lg.get("topics", [])
        if len(t) < 3 or t[0].lower() != TRANSFER_TOPIC or len(lg.get("data", "")) <= 2:
            continue
        token = lg["address"].lower()
        amount = int(lg["data"], 16)
        if t[1].lower() == wallet_topic:
            net_out[token] = net_out.get(token, 0) + amount
        if t[2].lower() == wallet_topic:
            net_out[token] = net_out.get(token, 0) - amount

    changes: list[DecodedChange] = []
    for lg in logs:
        t = lg.get("topics", [])
        if lg.get("address", "").lower() != pm or not t or t[0].lower() != PM_MODIFY_TOPIC:
            continue
        token_id = int(t[1], 16)
        w = _words(lg["data"])
        packed = w[1]  # BalanceDelta: int128 amount0 << 128 | int128 amount1
        fees = (_signed(packed >> 128, 128), _signed(packed & ((1 << 128) - 1), 128))
        pool = pool_logs.get(token_id)
        if pool is None:
            continue  # PM event with no pool change in this tx — nothing to record
        tokens = known_tokens.get(pool["pool_id"])
        if tokens is None:
            moved = sorted(tok for tok in net_out if tok != pm)
            tokens = (moved[0], moved[1]) if len(moved) == 2 else None
        changes.append(
            DecodedChange(
                tx_hash=tx_hash,
                block_number=block,
                log_index=pool["log_index"],
                token_id=token_id,
                pool_id=pool["pool_id"],
                tick_lower=pool["tick_lower"],
                tick_upper=pool["tick_upper"],
                liquidity_delta=pool["liquidity_delta"],
                fees=fees,
                principal=None,
                tokens=tokens,
            )
        )

    moving = [c for c in changes if c.liquidity_delta != 0]
    for c in changes:
        if c.liquidity_delta == 0:
            c.principal = (0, 0)
        elif len(moving) == 1 and c.tokens is not None:
            # Net paid by the wallet + fees accrued = principal into the pool
            # (negative on a withdrawal).
            c.principal = (
                net_out.get(c.tokens[0], 0) + c.fees[0],
                net_out.get(c.tokens[1], 0) + c.fees[1],
            )
        elif c.tokens is None:
            c.notes.append("pool currencies unresolvable from this receipt")
        else:
            c.notes.append(f"{len(moving)} positions changed liquidity in one tx")
    return changes


def minted_token_ids(logs: list[dict], position_manager: str, wallet: str) -> set[int]:
    """Position NFTs transferred TO the wallet in these logs (mints included)."""
    pm, wt = position_manager.lower(), address_topic(wallet)
    out: set[int] = set()
    for lg in logs:
        t = lg.get("topics", [])
        if (
            lg.get("address", "").lower() == pm
            and len(t) == 4
            and t[0].lower() == TRANSFER_TOPIC
            and t[2].lower() == wt
        ):
            out.add(int(t[3], 16))
    return out
