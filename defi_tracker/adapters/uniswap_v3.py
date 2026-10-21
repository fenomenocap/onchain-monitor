"""
Uniswap V3 adapter — multi-chain CL (ETH, ARB, OP, BASE, POLYGON).

Events are sourced from The Graph subgraph (Mint/Burn entities).
Position state and unclaimed fees are computed via RPC using the standard
V3 NonfungiblePositionManager ABI, shared with MachineX via _cl_math.py.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from decimal import Decimal

from defi_tracker.adapters._base import BaseAdapter
from defi_tracker.adapters._cl_math import (
    _decode_uint256,
    amounts_from_liquidity,
    compute_unclaimed_fees_v3,
    rpc_enumerate_positions_v3,
    rpc_get_pool_address_v3,
    rpc_get_position_v3,
    rpc_get_slot0_v3,
    tick_to_sqrt_price,
)
from defi_tracker.adapters._rpc import RpcClient
from defi_tracker.core.adapter import register_adapter
from defi_tracker.core.endpoints import fallback_urls, rpc_url
from defi_tracker.core.types import (
    AdapterInfo,
    Chain,
    Event,
    EventKind,
    Position,
    ProtocolKind,
    Token,
    TokenAmount,
)

_log = logging.getLogger(__name__)

GRAPH_GATEWAY = "https://gateway.thegraph.com/api"

# ── Per-chain configuration ────────────────────────────────────────────────
# nft_manager: NonfungiblePositionManager address
# factory: UniswapV3Factory address
# subgraph_id: The Graph subgraph deployment ID

V3_CHAINS: dict[Chain, dict] = {
    Chain.ETHEREUM: {
        "nft_manager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "subgraph_id": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
    },
    Chain.ARBITRUM: {
        "nft_manager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "subgraph_id": "FbCGRftH4a3yZugY7TnbYgPJVEv2LvMT6oF1fxPe9aJM",
    },
    Chain.OPTIMISM: {
        "nft_manager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "subgraph_id": "Cghf4LfVqPiFw6fp6Y5X5Ubc8UpmUhSfJL82zwiBFLaj",
    },
    Chain.BASE: {
        "nft_manager": "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
        "factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        # Verified 2026-08-14 against the live gateway (both deployments at the
        # same block, 49955655). The previous id — HMuAwufqZ1YCRmzL2SfHTVkzZovC9VL2UAKhjvRqKiR1
        # — indexes `position.owner` as the NonfungiblePositionManager
        # (0x03a520b3…) rather than the EOA, so `positions(where: {owner: <wallet>})`
        # returned an empty list with no GraphQL error: every Base position looked
        # closed and got tombstoned. This deployment indexes owner as the EOA and
        # serves identical mints/burns plus every field fetch_positions needs.
        "subgraph_id": "43Hwfi3dJSoGpyas9VwNoDAv55yjgGrPpNSmbQZArzMG",
    },
    Chain.POLYGON: {
        "nft_manager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "subgraph_id": "3hCPRGf4z88VC5rsBKU5AA9FBBq5nF3jbKJG7VZCbhjm",
    },
}


class UniswapV3Adapter(BaseAdapter):
    """Uniswap V3 on ETH, ARB, OP, BASE, POLYGON."""

    def __init__(self, graph_api_key: str, db_path: str = "", chains: list[Chain] | None = None):
        super().__init__(db_path=db_path)
        self.graph_api_key = graph_api_key
        # Which of V3_CHAINS this instance actually serves. Probing a chain the
        # wallets have never touched costs a full subgraph round-trip (with
        # retries) on every run, so the deployment scopes this to the chains
        # positions are actually held on. None = all configured chains.
        self.chains = [c for c in (chains or V3_CHAINS) if c in V3_CHAINS]
        # Lazy per-chain RPC clients (instantiated on first use)
        self._chain_rpc: dict[Chain, RpcClient] = {}

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            protocol_id="uniswap_v3",
            display_name="Uniswap V3",
            chains=list(self.chains),
            protocol_kind=ProtocolKind.CL_AMM,
            supports_exact_fees=True,
            supports_historical=True,
        )

    # ── RPC per chain ─────────────────────────────────────────────────────

    def _rpc_for(self, chain: Chain) -> RpcClient:
        if chain not in self._chain_rpc:
            self._chain_rpc[chain] = RpcClient(rpc_url(chain), fallback_urls=fallback_urls(chain))
        return self._chain_rpc[chain]

    def block_at(self, chain: Chain, ts: int, near: int | None = None) -> int:
        return self._rpc_for(chain).block_at(ts, near)

    def _subgraph_url(self, chain: Chain) -> str:
        sid = V3_CHAINS[chain]["subgraph_id"]
        return f"{GRAPH_GATEWAY}/{self.graph_api_key}/subgraphs/id/{sid}"

    # ── Subgraph paging ───────────────────────────────────────────────────

    def _fetch_entity(self, query: str, chain: Chain, entity: str) -> list[dict]:
        """Run one page of a subgraph query and return the rows for `entity`.

        Raises RuntimeError when the *query* failed; returns [] only when the
        query succeeded and the entity list is genuinely empty. The two cases
        are otherwise indistinguishable to the caller, and an empty position
        list makes the runner tombstone every open position — permanently
        dropping them from reports (same lesson as pancake_infinity).

        `_run_subgraph_query` collapses every transport/GraphQL failure to
        None, so `data is None` is an outage. A payload that succeeded but
        lacks the requested key is also a failure (wrong/renamed entity), not
        an empty wallet. `_run_subgraph_query` already raises on 401/403.
        """
        data = self._run_subgraph_query(query, self._subgraph_url(chain))
        if data is None:
            raise RuntimeError(
                f"Uniswap V3 {entity} query returned no data on {chain.value} "
                "(subgraph outage, bad indexers, or GraphQL error)"
            )
        rows = data.get(entity)
        if rows is None:
            raise RuntimeError(
                f"Uniswap V3 {entity} query on {chain.value} returned a payload "
                f"without a '{entity}' field — schema mismatch, not an empty wallet"
            )
        return list(rows)

    # ── Events ────────────────────────────────────────────────────────────

    def fetch_events(self, wallet: str, chain: Chain, since_ts: int = 0) -> list[Event]:
        if chain not in V3_CHAINS:
            return []

        wallet_lower = wallet.lower()
        all_events: list[Event] = []

        # Fetch Mint events (deposits)
        mints = self._fetch_mints(wallet_lower, chain, since_ts)
        for m in mints:
            ev = self._mint_to_event(m, wallet_lower, chain)
            if ev:
                all_events.append(ev)

        # Fetch Burn events (withdrawals)
        burns = self._fetch_burns(wallet_lower, chain, since_ts)
        for b in burns:
            ev = self._burn_to_event(b, wallet_lower, chain)
            if ev:
                all_events.append(ev)

        all_events.sort(key=lambda e: (e.ts, e.log_index))
        return all_events

    def _fetch_mints(self, wallet: str, chain: Chain, since_ts: int) -> list[dict]:
        all_mints: list[dict] = []
        skip = 0
        while True:
            q = f"""
            {{
              mints(
                where: {{ origin: "{wallet}", timestamp_gte: {since_ts} }}
                first: 1000
                skip: {skip}
                orderBy: timestamp
                orderDirection: asc
              ) {{
                id timestamp logIndex
                amount0 amount1 amountUSD
                tickLower tickUpper
                token0 {{ id symbol decimals }}
                token1 {{ id symbol decimals }}
                pool {{ id tick sqrtPrice feeTier totalValueLockedUSD }}
                transaction {{ id }}
              }}
            }}
            """
            batch = self._fetch_entity(q, chain, "mints")
            all_mints.extend(batch)
            if len(batch) < 1000:
                break
            skip += 1000
        return all_mints

    def _fetch_burns(self, wallet: str, chain: Chain, since_ts: int) -> list[dict]:
        all_burns: list[dict] = []
        skip = 0
        while True:
            q = f"""
            {{
              burns(
                where: {{ origin: "{wallet}", timestamp_gte: {since_ts} }}
                first: 1000
                skip: {skip}
                orderBy: timestamp
                orderDirection: asc
              ) {{
                id timestamp logIndex
                amount0 amount1 amountUSD
                tickLower tickUpper
                token0 {{ id symbol decimals }}
                token1 {{ id symbol decimals }}
                pool {{ id tick sqrtPrice feeTier totalValueLockedUSD }}
                transaction {{ id }}
              }}
            }}
            """
            batch = self._fetch_entity(q, chain, "burns")
            all_burns.extend(batch)
            if len(batch) < 1000:
                break
            skip += 1000
        return all_burns

    def _mint_to_event(self, m: dict, wallet: str, chain: Chain) -> Event | None:
        return self._raw_to_event(m, wallet, chain, EventKind.DEPOSIT)

    def _burn_to_event(self, b: dict, wallet: str, chain: Chain) -> Event | None:
        return self._raw_to_event(b, wallet, chain, EventKind.WITHDRAW)

    def _raw_to_event(self, raw: dict, wallet: str, chain: Chain, kind: EventKind) -> Event | None:
        try:
            t0 = raw["token0"]
            t1 = raw["token1"]
            pool = raw["pool"]
            token0 = Token(chain, t0["id"].lower(), t0["symbol"], int(t0.get("decimals", 18) or 18))
            token1 = Token(chain, t1["id"].lower(), t1["symbol"], int(t1.get("decimals", 18) or 18))
            self._storage.upsert_token(token0)
            self._storage.upsert_token(token1)

            amt0 = Decimal(str(raw.get("amount0", "0") or "0"))
            amt1 = Decimal(str(raw.get("amount1", "0") or "0"))
            if kind == EventKind.WITHDRAW:
                amt0, amt1 = -amt0, -amt1

            if amt0 == 0 and amt1 == 0:
                # Not an unpriced event — an economically empty one. The
                # NonfungiblePositionManager pokes the pool with a
                # zero-liquidity burn(0) before every collect() so the pool
                # settles fee accounting; the subgraph records that as a Burn
                # with amount/amount0/amount1/amountUSD all 0 and no principal
                # leaving the position (position.withdrawnToken0/1 stay 0).
                # No price fallback can value it, and booking it as a $0
                # WITHDRAW would put a real-looking but valueless exit in the
                # ledger. The fee actually paid out lives in the Collect
                # entity, which this adapter does not ingest.
                _log.info(
                    "Skipping zero-amount %s %s (NFPM fee-collect poke, no principal moved)",
                    kind.value,
                    raw.get("transaction", {}).get("id", "?"),
                )
                return None

            ts = int(raw["timestamp"])
            pp0 = self.get_historical_price(token0, ts)
            pp1 = self.get_historical_price(token1, ts)
            p0: Decimal | None = pp0.price_usd if pp0 else None
            p1: Decimal | None = pp1.price_usd if pp1 else None

            # Amount-ratio fallback
            abs0, abs1 = abs(amt0), abs(amt1)
            if p0 is None and p1 is not None and abs1 > 0 and abs0 > 0:
                p0 = abs1 * p1 / abs0
            elif p1 is None and p0 is not None and abs0 > 0 and abs1 > 0:
                p1 = abs0 * p0 / abs1

            if p0 is None or p1 is None:
                self._note_dropped_event(
                    wallet,
                    chain,
                    f"no price for {token0.symbol}/{token1.symbol} at ts={ts}",
                    str(raw.get("transaction", {}).get("id", "?")),
                )
                return None

            usd_at_ts = amt0 * p0 + amt1 * p1
            tick_lower = int(raw.get("tickLower", 0) or 0)
            tick_upper = int(raw.get("tickUpper", 0) or 0)
            position_key = f"{pool['id']}:{tick_lower}:{tick_upper}"

            return Event(
                wallet=wallet,
                chain=chain,
                protocol_id=self.info.protocol_id,
                position_key=position_key,
                tx_hash=raw.get("transaction", {}).get("id", ""),
                log_index=int(raw.get("logIndex", 0) or 0),
                ts=ts,
                block_number=0,  # not available in subgraph mints/burns
                kind=kind,
                amounts=[TokenAmount(token0, amt0), TokenAmount(token1, amt1)],
                prices_at_ts={token0.key: p0, token1.key: p1},
                usd_at_ts=usd_at_ts,
                meta={
                    "pool_id": pool["id"],
                    "tick_lower": tick_lower,
                    "tick_upper": tick_upper,
                },
            )
        except Exception as e:
            self._note_dropped_event(
                wallet, chain, f"failed to parse event: {e}", str(raw.get("id"))
            )
            return None

    # ── Positions ─────────────────────────────────────────────────────────

    def fetch_positions(self, wallet: str, chain: Chain) -> list[Position]:
        if chain not in V3_CHAINS:
            return []

        # Open tokenIds come from the subgraph; everything else is RPC.
        token_ids = self._fetch_active_token_ids(wallet.lower(), chain)
        if not token_ids:
            return []
        return self._positions(wallet, chain, self._rpc_for(chain), token_ids)

    def fetch_positions_at(self, wallet: str, chain: Chain, block: int, ts: int) -> list[Position]:
        """Positions as they stood at `block`, from archive state: the
        position manager enumerates the wallet's tokenIds at that block (the
        subgraph only knows today's), then the live path's reads run pinned
        to it, valued at that day's prices."""
        if chain not in V3_CHAINS:
            return []
        pinned = self._rpc_for(chain).pinned(block)
        token_ids = rpc_enumerate_positions_v3(pinned, V3_CHAINS[chain]["nft_manager"], wallet)
        self._as_of_ts = ts
        try:
            positions = self._positions(wallet, chain, pinned, token_ids)
        finally:
            self._as_of_ts = None
        for p in positions:
            p.snapshot_at = ts
            p.meta = {**p.meta, "source": "archive", "block": block}
        return positions

    _SEL_FEE_GROWTH0 = bytes.fromhex("f3058399")  # feeGrowthGlobal0X128()
    _SEL_FEE_GROWTH1 = bytes.fromhex("46141319")  # feeGrowthGlobal1X128()
    _SEL_LIQUIDITY = bytes.fromhex("1a686502")  # liquidity() — active, in range

    def _pool_uint(self, rpc: RpcClient, pool: str, sel: bytes) -> int | None:
        raw = rpc.eth_call(pool, sel)
        return _decode_uint256(raw, 0) if raw and len(raw) >= 32 else None

    def _pool_fees_7d(
        self, rpc: RpcClient, chain: Chain, pool: str, token0: Token, token1: Token,
        p0: Decimal, p1: Decimal,
    ) -> Decimal | None:  # fmt: skip
        """The pool's 7-day LP fees from its own fee counter (feeGrowthGlobal ×
        active liquidity, sampled by archive eth_call) — a week of Swap logs
        is out of reach where the provider caps log ranges. Unknown (None)
        when a read fails, and on a reconstructed day: a week read today says
        nothing about a past one."""
        from defi_tracker.adapters._pool_activity import fee_growth_activity

        if self._as_of_ts is not None:
            return None

        def growth(r: RpcClient) -> tuple[int, int] | None:
            g0, g1 = (
                self._pool_uint(r, pool, s) for s in (self._SEL_FEE_GROWTH0, self._SEL_FEE_GROWTH1)
            )
            return None if g0 is None or g1 is None else (g0, g1)

        try:
            act = fee_growth_activity(
                rpc, cache=self._storage, cache_key=f"pool_feegrowth:{chain.value}:{pool}",
                read_fee_growth=growth,
                read_liquidity=lambda r: self._pool_uint(r, pool, self._SEL_LIQUIDITY),
                price0=p0, price1=p1, dec0=token0.decimals, dec1=token1.decimals,
            )  # fmt: skip
        except Exception as e:
            _log.warning("Uniswap V3 pool fees unreadable for %s on %s (%s)", pool, chain.value, e)
            return None
        return act.fees_usd

    def _positions(
        self, wallet: str, chain: Chain, rpc: RpcClient, token_ids: list[int]
    ) -> list[Position]:
        cfg = V3_CHAINS[chain]
        nft_manager = cfg["nft_manager"]
        factory = cfg["factory"]
        positions: list[Position] = []
        for token_id in token_ids:
            # Every id here is one the wallet holds, so a read that comes back
            # empty is a failed read, not a closed position — skipping it would
            # have the runner tombstone a live position.
            raw = rpc_get_position_v3(rpc, nft_manager, token_id)
            if not raw:
                raise RuntimeError(f"positions({token_id}) unreadable on {chain.value}")
            token0_addr, token1_addr, fee, tick_lower, tick_upper, liquidity, _, _ = raw
            if liquidity <= 0:
                continue

            token0 = self._token(chain, rpc, token0_addr)
            token1 = self._token(chain, rpc, token1_addr)

            pool = rpc_get_pool_address_v3(rpc, factory, token0_addr, token1_addr, fee)
            if not pool:
                raise RuntimeError(f"getPool unreadable for token {token_id} on {chain.value}")

            slot0 = rpc_get_slot0_v3(rpc, pool)
            if not slot0:
                raise RuntimeError(f"slot0 unreadable for pool {pool} on {chain.value}")
            sqrt_price_x96, tick_current = slot0

            sqrt_lower = tick_to_sqrt_price(tick_lower)
            sqrt_upper = tick_to_sqrt_price(tick_upper)
            raw0, raw1 = amounts_from_liquidity(sqrt_price_x96, sqrt_lower, sqrt_upper, liquidity)
            current0 = Decimal(raw0) / Decimal(10**token0.decimals)
            current1 = Decimal(raw1) / Decimal(10**token1.decimals)

            pp0 = self.price_now_or_then(token0)
            pp1 = self.price_now_or_then(token1)
            p0 = pp0.price_usd if pp0 else Decimal("0")
            p1 = pp1.price_usd if pp1 else Decimal("0")

            # Pool-ratio fallback
            if pp0 is None and pp1 is not None and p1 > 0:
                derived = self.price_from_sqrt(sqrt_price_x96, token0.decimals, token1.decimals, p1)
                if derived:
                    p0 = derived

            lp_value_usd = current0 * p0 + current1 * p1

            # Unreadable fee growth (a throttled public RPC will do it) must
            # not become a real $0 — that is what once made a live Base position
            # report zero unclaimed while its value was correct.
            u0 = u1 = Decimal("0")
            u_usd: Decimal | None = Decimal("0")
            try:
                u0, u1, u_usd = compute_unclaimed_fees_v3(
                    rpc=rpc,
                    factory=factory,
                    nft_manager=nft_manager,
                    token_id=token_id,
                    tick_current=tick_current,
                    decimals0=token0.decimals,
                    decimals1=token1.decimals,
                    price0=p0,
                    price1=p1,
                )
            except RuntimeError as e:
                _log.warning(
                    "Uniswap V3 unclaimed fees unreadable for tokenId %s on %s (%s)",
                    token_id,
                    chain.value,
                    e,
                )
                u_usd = None

            in_range = tick_lower <= tick_current <= tick_upper
            fee_bps = fee // 100
            # The pool's depth and fee rate: what NEGATIVE_CARRY and the range
            # optimiser need. Unknown stays None, never 0.
            active = self._pool_uint(rpc, pool, self._SEL_LIQUIDITY)
            share = None
            if active:
                share = Decimal(liquidity) / Decimal(active) * 100 if in_range else Decimal("0")
            fees7 = self._pool_fees_7d(rpc, chain, pool, token0, token1, p0, p1)
            meta: dict = {"token_id": token_id, "source": "subgraph"}
            if active is not None:
                meta["pool_liquidity"] = str(active)

            positions.append(
                Position(
                    wallet=wallet.lower(),
                    chain=chain,
                    protocol_id=self.info.protocol_id,
                    position_key=f"{pool}:{tick_lower}:{tick_upper}",
                    protocol_kind=ProtocolKind.CL_AMM,
                    pair_label=f"{token0.symbol}/{token1.symbol} {fee / 10000:.2f}%",
                    tokens=[token0, token1],
                    current_value_usd=lp_value_usd,
                    current_balances=[
                        TokenAmount(token0, current0),
                        TokenAmount(token1, current1),
                    ],
                    unclaimed_usd=u_usd,
                    unclaimed_balances=[TokenAmount(token0, u0), TokenAmount(token1, u1)],
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                    tick_current=tick_current,
                    in_range=in_range,
                    liquidity=liquidity,
                    pool_address=pool,
                    fee_tier_bps=fee_bps,
                    snapshot_at=int(time.time()),
                    # source: the discovery path. Open tokenIds come from the
                    # subgraph (a subgraph outage raises in
                    # _fetch_active_token_ids rather than falling back), while
                    # balances and fee math are read over RPC — normal
                    # operation for this adapter, not degradation. So the only
                    # honest label here is 'subgraph': it means "the subgraph
                    # told us which positions exist", which is exactly the
                    # signal health warnings care about.
                    seven_day_fees_usd=fees7,
                    position_share_pct=share,
                    meta=meta,
                )
            )

        return positions

    def _fetch_active_token_ids(self, wallet: str, chain: Chain) -> list[int]:
        """Query subgraph for tokenIds with liquidity > 0 owned by wallet.

        Raises (via _fetch_entity) if the query fails — a silent [] here would
        make the runner tombstone every open position on this chain.
        """
        all_ids: list[int] = []
        skip = 0
        while True:
            q = f"""
            {{
              positions(
                where: {{ owner: "{wallet}", liquidity_gt: "0" }}
                first: 1000
                skip: {skip}
                orderBy: id
                orderDirection: asc
              ) {{ id }}
            }}
            """
            batch = self._fetch_entity(q, chain, "positions")
            for p in batch:
                with contextlib.suppress(KeyError, ValueError):
                    all_ids.append(int(p["id"]))
            if len(batch) < 1000:
                break
            skip += 1000
        return all_ids

    def _token(self, chain: Chain, rpc: RpcClient, address: str) -> Token:
        """Token metadata from storage, else read from the token contract and
        stored. Decimals are never guessed: 18 for a 6-decimal USDC would value
        it 1e12 too low, and a stored guess would outlive the run."""
        addr = address.lower()
        with self._storage.connect() as conn:
            row = conn.execute(
                "SELECT symbol, decimals FROM tokens WHERE chain=? AND address=?",
                (chain.value, addr),
            ).fetchone()
        if row:
            return Token(chain, addr, row["symbol"], row["decimals"])
        symbol, decimals = rpc.erc20_metadata(addr)
        if decimals is None:
            raise RuntimeError(f"decimals() unreadable for {addr} on {chain.value}")
        tok = Token(chain, addr, symbol or addr[:6] + "…" + addr[-4:], decimals)
        self._storage.upsert_token(tok)
        return tok


# ── Auto-register if GRAPH_API_KEY is set ─────────────────────────────────


def _autoregister():
    """Register scoped to UNISWAP_V3_CHAINS (comma-separated chain names).

    Defaults to base — the only chain these wallets hold V3 positions on. It
    was previously disabled outright, which left a live Base position
    invisible and misreported as a closed loss; probing all five chains every
    run is the cost that motivated that, so scope rather than disable. Set the
    var to a wider list to add chains, or to an empty string to register none.
    """
    key = os.getenv("GRAPH_API_KEY")
    if not key:
        return
    raw = os.getenv("UNISWAP_V3_CHAINS", "base")
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    chains = []
    for n in names:
        try:
            chains.append(Chain(n))
        except ValueError:
            _log.warning("UNISWAP_V3_CHAINS: unknown chain %r — ignored", n)
    if not chains:
        return
    register_adapter(
        UniswapV3Adapter(
            graph_api_key=key,
            db_path=os.getenv("TRACKER_DB", "tracker.db"),
            chains=chains,
        )
    )


_autoregister()
