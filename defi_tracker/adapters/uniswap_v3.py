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
    amounts_from_liquidity,
    compute_unclaimed_fees_v3,
    rpc_get_pool_address_v3,
    rpc_get_position_v3,
    rpc_get_slot0_v3,
    tick_to_sqrt_price,
)
from defi_tracker.adapters._rpc import RpcClient
from defi_tracker.core.adapter import register_adapter
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
# rpc_env: env var name for the RPC URL
# rpc_default: public fallback RPC

V3_CHAINS: dict[Chain, dict] = {
    Chain.ETHEREUM: {
        "nft_manager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "subgraph_id": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
        "rpc_env": "ETH_RPC_URL",
        "rpc_default": "https://eth.llamarpc.com",
    },
    Chain.ARBITRUM: {
        "nft_manager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "subgraph_id": "FbCGRftH4a3yZugY7TnbYgPJVEv2LvMT6oF1fxPe9aJM",
        "rpc_env": "ARB_RPC_URL",
        "rpc_default": "https://arb1.arbitrum.io/rpc",
    },
    Chain.OPTIMISM: {
        "nft_manager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "subgraph_id": "Cghf4LfVqPiFw6fp6Y5X5Ubc8UpmUhSfJL82zwiBFLaj",
        "rpc_env": "OP_RPC_URL",
        "rpc_default": "https://mainnet.optimism.io",
    },
    Chain.BASE: {
        "nft_manager": "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
        "factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        "subgraph_id": "HMuAwufqZ1YCRmzL2SfHTVkzZovC9VL2UAKhjvRqKiR1",
        "rpc_env": "BASE_RPC_URL",
        "rpc_default": "https://mainnet.base.org",
    },
    Chain.POLYGON: {
        "nft_manager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "subgraph_id": "3hCPRGf4z88VC5rsBKU5AA9FBBq5nF3jbKJG7VZCbhjm",
        "rpc_env": "POLYGON_RPC_URL",
        "rpc_default": "https://polygon-rpc.com",
    },
}


class UniswapV3Adapter(BaseAdapter):
    """Uniswap V3 on ETH, ARB, OP, BASE, POLYGON."""

    def __init__(self, graph_api_key: str, db_path: str = ""):
        super().__init__(db_path=db_path)
        self.graph_api_key = graph_api_key
        # Lazy per-chain RPC clients (instantiated on first use)
        self._chain_rpc: dict[Chain, RpcClient] = {}

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            protocol_id="uniswap_v3",
            display_name="Uniswap V3",
            chains=list(V3_CHAINS.keys()),
            protocol_kind=ProtocolKind.CL_AMM,
            supports_exact_fees=True,
            supports_historical=True,
        )

    # ── RPC per chain ─────────────────────────────────────────────────────

    def _rpc_for(self, chain: Chain) -> RpcClient:
        if chain not in self._chain_rpc:
            cfg = V3_CHAINS[chain]
            url = os.getenv(cfg["rpc_env"], cfg["rpc_default"])
            self._chain_rpc[chain] = RpcClient(url)
        return self._chain_rpc[chain]

    def _subgraph_url(self, chain: Chain) -> str:
        sid = V3_CHAINS[chain]["subgraph_id"]
        return f"{GRAPH_GATEWAY}/{self.graph_api_key}/subgraphs/id/{sid}"

    # ── Events ────────────────────────────────────────────────────────────

    def fetch_events(
        self, wallet: str, chain: Chain, since_ts: int = 0
    ) -> list[Event]:
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

    def _fetch_mints(
        self, wallet: str, chain: Chain, since_ts: int
    ) -> list[dict]:
        all_mints, skip = [], 0
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
            data = self._run_subgraph_query(q, self._subgraph_url(chain))
            if not data:
                break
            batch = data.get("mints", [])
            all_mints.extend(batch)
            if len(batch) < 1000:
                break
            skip += 1000
        return all_mints

    def _fetch_burns(
        self, wallet: str, chain: Chain, since_ts: int
    ) -> list[dict]:
        all_burns, skip = [], 0
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
            data = self._run_subgraph_query(q, self._subgraph_url(chain))
            if not data:
                break
            batch = data.get("burns", [])
            all_burns.extend(batch)
            if len(batch) < 1000:
                break
            skip += 1000
        return all_burns

    def _mint_to_event(self, m: dict, wallet: str, chain: Chain) -> Event | None:
        return self._raw_to_event(m, wallet, chain, EventKind.DEPOSIT)

    def _burn_to_event(self, b: dict, wallet: str, chain: Chain) -> Event | None:
        return self._raw_to_event(b, wallet, chain, EventKind.WITHDRAW)

    def _raw_to_event(
        self, raw: dict, wallet: str, chain: Chain, kind: EventKind
    ) -> Event | None:
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
                _log.warning(
                    "Skipping event %s: no price for %s/%s at ts=%d",
                    raw.get("transaction", {}).get("id", "?"),
                    token0.symbol,
                    token1.symbol,
                    ts,
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
            _log.warning("Failed to parse event %s: %s", raw.get("id"), e)
            return None

    # ── Positions ─────────────────────────────────────────────────────────

    def fetch_positions(self, wallet: str, chain: Chain) -> list[Position]:
        if chain not in V3_CHAINS:
            return []

        cfg = V3_CHAINS[chain]
        nft_manager = cfg["nft_manager"]
        factory = cfg["factory"]
        rpc = self._rpc_for(chain)

        # Query active positions from subgraph
        token_ids = self._fetch_active_token_ids(wallet.lower(), chain)
        if not token_ids:
            return []

        positions: list[Position] = []
        for token_id in token_ids:
            raw = rpc_get_position_v3(rpc, nft_manager, token_id)
            if not raw:
                continue
            token0_addr, token1_addr, fee, tick_lower, tick_upper, liquidity, _, _ = raw
            if liquidity <= 0:
                continue

            token0 = self._token_from_subgraph_or_default(chain, token0_addr)
            token1 = self._token_from_subgraph_or_default(chain, token1_addr)
            self._storage.upsert_token(token0)
            self._storage.upsert_token(token1)

            pool = rpc_get_pool_address_v3(rpc, factory, token0_addr, token1_addr, fee)
            if not pool:
                continue

            slot0 = rpc_get_slot0_v3(rpc, pool)
            if not slot0:
                continue
            sqrt_price_x96, tick_current = slot0

            sqrt_lower = tick_to_sqrt_price(tick_lower)
            sqrt_upper = tick_to_sqrt_price(tick_upper)
            raw0, raw1 = amounts_from_liquidity(
                sqrt_price_x96, sqrt_lower, sqrt_upper, liquidity
            )
            current0 = Decimal(raw0) / Decimal(10**token0.decimals)
            current1 = Decimal(raw1) / Decimal(10**token1.decimals)

            pp0 = self.get_current_price(token0)
            pp1 = self.get_current_price(token1)
            p0 = pp0.price_usd if pp0 else Decimal("0")
            p1 = pp1.price_usd if pp1 else Decimal("0")

            # Pool-ratio fallback
            if pp0 is None and pp1 is not None and p1 > 0:
                derived = self.price_from_sqrt(
                    sqrt_price_x96, token0.decimals, token1.decimals, p1
                )
                if derived:
                    p0 = derived

            lp_value_usd = current0 * p0 + current1 * p1

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

            in_range = tick_lower <= tick_current <= tick_upper
            fee_bps = fee // 100

            positions.append(
                Position(
                    wallet=wallet.lower(),
                    chain=chain,
                    protocol_id=self.info.protocol_id,
                    position_key=f"{pool}:{tick_lower}:{tick_upper}",
                    protocol_kind=ProtocolKind.CL_AMM,
                    pair_label=f"{token0.symbol}/{token1.symbol} {fee/10000:.2f}%",
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
                    meta={"token_id": token_id},
                )
            )

        return positions

    def _fetch_active_token_ids(self, wallet: str, chain: Chain) -> list[int]:
        """Query subgraph for tokenIds with liquidity > 0 owned by wallet."""
        all_ids, skip = [], 0
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
            data = self._run_subgraph_query(q, self._subgraph_url(chain))
            if not data:
                break
            batch = data.get("positions", [])
            for p in batch:
                with contextlib.suppress(KeyError, ValueError):
                    all_ids.append(int(p["id"]))
            if len(batch) < 1000:
                break
            skip += 1000
        return all_ids

    def _token_from_subgraph_or_default(self, chain: Chain, address: str) -> Token:
        """Look up token metadata from storage; fall back to address-based defaults."""
        addr = address.lower()
        with self._storage.connect() as conn:
            row = conn.execute(
                "SELECT symbol, decimals FROM tokens WHERE chain=? AND address=?",
                (chain.value, addr),
            ).fetchone()
        if row:
            return Token(chain, addr, row["symbol"], row["decimals"])
        return Token(chain, addr, addr[:6] + "…" + addr[-4:], 18)


# ── Auto-register if GRAPH_API_KEY is set ─────────────────────────────────


def _autoregister():
    key = os.getenv("GRAPH_API_KEY")
    if key:
        register_adapter(
            UniswapV3Adapter(
                graph_api_key=key,
                db_path=os.getenv("TRACKER_DB", "tracker.db"),
            )
        )


_autoregister()
