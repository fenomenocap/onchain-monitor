"""
MachineX adapter — peaq chain DEX (Ramses/Uni V3 fork).

MachineX is deployed on the peaq blockchain (chain ID 3338).

Position tracking:
  - fetch_positions(): queries the MachineX subgraph for CL and legacy (XYK)
    positions; unclaimed fees are read over RPC (feeGrowthInside math), and
    a subgraph outage or an unusable answer rebuilds positions from the chain
    (_fetch_positions_rpc). Token prices come from subgraph priceUSD.
  - iter_event_batches()/fetch_events(): scans IncreaseLiquidity /
    DecreaseLiquidity / Collect logs via RPC block cursor, one 25k-block
    window per batch. Requires PEAQ_RPC_URL; gracefully skips if not set.
    Standalone Collects are recorded as pure fee harvests; a Collect bundled
    with a same-tx DecreaseLiquidity pays principal + fees, so only the
    difference vs. the decrease amounts is recorded as a COLLECT event.

Auto-registers when GRAPH_API_KEY is present (same env var as other adapters).
PEAQ_RPC_URL is optional — only needed for event scanning / cost basis.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

from eth_hash.auto import keccak

from defi_tracker.adapters._base import BaseAdapter
from defi_tracker.adapters._cl_math import (
    TOPIC_ERC20_TRANSFER,
    TOPIC_V3_COLLECT,
    TOPIC_V3_DECREASE_LIQUIDITY,
    TOPIC_V3_INCREASE_LIQUIDITY,
    _abi_address,
    _decode_address,
    _decode_uint256,
    amounts_from_liquidity,
    compute_unclaimed_fees_pure,
    compute_unclaimed_fees_v3,
    rpc_enumerate_positions_v3,
    rpc_get_pool_address_v3,
    rpc_get_position_v3,
    rpc_get_slot0_v3,
    tick_to_sqrt_price,
)
from defi_tracker.adapters._rpc import RpcClient
from defi_tracker.core.adapter import register_adapter
from defi_tracker.core.endpoints import fallback_urls
from defi_tracker.core.endpoints import rpc_url as endpoint_url
from defi_tracker.core.types import (
    AdapterInfo,
    Chain,
    Event,
    EventKind,
    PoolInfo,
    PoolSwap,
    Position,
    ProtocolKind,
    Token,
    TokenAmount,
)

_log = logging.getLogger(__name__)


def _with_pool_liquidity(meta: dict, pool_liquidity: int | None) -> dict:
    """Carry the pool's active liquidity (raw units, as a string: it can pass
    2^63) in Position.meta when it was read; absent, not zero, when not."""
    if pool_liquidity is not None:
        meta["pool_liquidity"] = str(pool_liquidity)
    return meta


# ── Constants — peaq mainnet ──────────────────────────────────────────────

NFT_MANAGER = "0x4DbC7DD463Bb4a8C6C4381cCEeB58b9327C7Cfa4"
V3_FACTORY = "0x2646DCbE025D21A2925fdaCeB639e998E17d6060"
DEPLOY_BLOCK = 5_317_777

MACHINEX_SUBGRAPH_ID = "EAVSLJ9r1mc18RmDXFHUxzH1QiQ83hDx1MY8LcPQ3nBB"
GRAPH_GATEWAY = "https://gateway.thegraph.com/api"

# peaq stablecoins — subgraph returns priceUSD="0" for these (reference pricing tokens)
_PEAQ_STABLECOINS: dict[str, Decimal] = {
    "0xbba60da06c2c5424f03f7434542280fcad453d10": Decimal("1.0"),  # USDC
    "0xf4d9235269a96aadafc9adae454a0618ebe37949": Decimal("1.0"),  # USDT
}

# peaq tokens with no subgraph priceUSD but a known CoinGecko coin ID
_PEAQ_CG_COIN_IDS: dict[str, str] = {
    "0x165bcb970836f83c15b22c3c1622279d97a20446": "acurast",  # ACU
}

# Scan limits for fetch_events block cursor
MAX_BLOCKS_PER_RUN = 150_000
CHUNK_SIZE = 5_000
CHUNK_SLEEP = 0.3
# Persistence granularity: iter_event_batches yields (and the cursor advances)
# once per window, so an interrupted run loses at most this many blocks.
WINDOW_BLOCKS = 25_000


# ── Subgraph query ────────────────────────────────────────────────────────

# Closed NFTs stay in the result (no liquidity filter), so a full page means
# the wallet's history outgrew it — open positions could be past the cut.
QUERY_PAGE = 1000


def _build_query(wallet: str) -> str:
    w = wallet.lower()
    return f"""
    {{
      clPositions(where: {{ owner: "{w}" }} first: {QUERY_PAGE}) {{
        id
        liquidity
        pool {{
          id
          sqrtPrice
          tick
          feeTier
          feeGrowthGlobal0X128
          feeGrowthGlobal1X128
          gauge {{ id isAlive }}
        }}
        token0 {{ id symbol decimals priceUSD }}
        token1 {{ id symbol decimals priceUSD }}
        tickLower {{
          tickIdx
          feeGrowthOutside0X128
          feeGrowthOutside1X128
        }}
        tickUpper {{
          tickIdx
          feeGrowthOutside0X128
          feeGrowthOutside1X128
        }}
        feeGrowthInside0LastX128
        feeGrowthInside1LastX128
      }}
      legacyPositions(where: {{ owner: "{w}" }} first: {QUERY_PAGE}) {{
        id
        liquidity
        pool {{
          id
          feeTier
          totalSupply
          totalValueLockedToken0
          totalValueLockedToken1
          gauge {{ id isAlive }}
        }}
        token0 {{ id symbol decimals priceUSD }}
        token1 {{ id symbol decimals priceUSD }}
      }}
    }}
    """


class MachineXAdapter(BaseAdapter):
    """MachineX (Ramses V3 fork) on peaq chain. Subgraph positions, RPC events."""

    def __init__(
        self,
        graph_api_key: str,
        rpc_url: str = "",
        db_path: str = "",
        rpc_fallbacks: tuple[str, ...] = (),
    ):
        super().__init__(
            db_path=db_path,
            rpc_url=rpc_url or endpoint_url(Chain.PEAQ, allow_public=False),
            rpc_fallbacks=rpc_fallbacks,
            stablecoins=_PEAQ_STABLECOINS,
            cg_coin_ids=_PEAQ_CG_COIN_IDS,
        )
        self._subgraph_url = f"{GRAPH_GATEWAY}/{graph_api_key}/subgraphs/id/{MACHINEX_SUBGRAPH_ID}"
        # (address, 'YYYY-MM-DD') pairs the subgraph couldn't price this run —
        # indexers that pruned a block won't un-prune it, so don't re-ask.
        self._subgraph_price_failed: set[tuple[str, str]] = set()
        # Per (wallet, chain): did the last event scan reach the chain head?
        # Written by iter_event_batches, read by last_scan_reached_head().
        self._scan_reached_head: dict[tuple[str, str], bool] = {}

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            protocol_id="machinex",
            display_name="MachineX (peaq)",
            chains=[Chain.PEAQ],
            protocol_kind=ProtocolKind.CL_AMM,
            supports_exact_fees=True,
            supports_historical=True,
        )

    # ── Positions ─────────────────────────────────────────────────────────

    def fetch_positions(self, wallet: str, chain: Chain) -> list[Position]:
        if chain != Chain.PEAQ:
            return []

        data = self._run_subgraph_query(_build_query(wallet), self._subgraph_url)
        if data is None:
            # An outage must never be silent: a bare [] is indistinguishable
            # from "wallet closed everything" and would tombstone the whole
            # portfolio. Rebuild from the chain instead, which raises rather
            # than guessing if it cannot establish what is held.
            _log.warning(
                "MachineX subgraph returned no data for wallet %s — rebuilding from RPC",
                wallet[:10],
            )
            return self._fetch_positions_rpc(wallet, chain)

        # A row that cannot be built, or a page that may have cut positions
        # off, must not drop a live position: the runner would tombstone it.
        # The chain read raises rather than guess, so it is the fallback.
        cl_rows = data.get("clPositions") or []
        legacy_rows = data.get("legacyPositions") or []
        positions: list[Position] = []
        try:
            if QUERY_PAGE in (len(cl_rows), len(legacy_rows)):
                raise RuntimeError(f"a full page of {QUERY_PAGE} positions — the list may be cut")
            for pos in cl_rows:
                p = self._build_cl_position(wallet, chain, pos)
                if p is not None:
                    positions.append(p)
            for pos in legacy_rows:
                p = self._build_legacy_position(wallet, chain, pos)
                if p is not None:
                    positions.append(p)
        except RuntimeError as e:
            _log.warning("MachineX subgraph positions unusable (%s) — rebuilding from RPC", e)
            return self._fetch_positions_rpc(wallet, chain)
        return positions

    def _build_cl_position(self, wallet: str, chain: Chain, pos: dict) -> Position | None:
        try:
            pool = pos.get("pool") or {}
            t0 = pos.get("token0") or {}
            t1 = pos.get("token1") or {}
            tick_lower_data = pos.get("tickLower") or {}
            tick_upper_data = pos.get("tickUpper") or {}

            liquidity = int(pos.get("liquidity") or 0)
            if liquidity <= 0:
                return None

            token0 = self._token_from_subgraph(chain, t0)
            token1 = self._token_from_subgraph(chain, t1)
            self._storage.upsert_token(token0)
            self._storage.upsert_token(token1)

            tick_current = int(pool.get("tick") or 0)
            tick_lower = int(tick_lower_data.get("tickIdx") or 0)
            tick_upper = int(tick_upper_data.get("tickIdx") or 0)
            sqrt_price_x96 = int(pool.get("sqrtPrice") or 0)

            sqrt_lower = tick_to_sqrt_price(tick_lower)
            sqrt_upper = tick_to_sqrt_price(tick_upper)
            raw0, raw1 = amounts_from_liquidity(sqrt_price_x96, sqrt_lower, sqrt_upper, liquidity)
            current0 = Decimal(raw0) / Decimal(10**token0.decimals)
            current1 = Decimal(raw1) / Decimal(10**token1.decimals)

            p0 = _subgraph_price(t0)
            p1 = _subgraph_price(t1)
            if p0 == 0:
                pp = self.get_current_price(token0)
                if pp:
                    p0 = pp.price_usd
            if p1 == 0:
                pp = self.get_current_price(token1)
                if pp:
                    p1 = pp.price_usd

            # Cache for runner analytics (get_current_price checks this before CoinGecko)
            if p0 > 0:
                self._current_price_cache[token0.key] = p0
            if p1 > 0:
                self._current_price_cache[token1.key] = p1

            lp_value_usd = current0 * p0 + current1 * p1
            token_id = int(pos.get("id") or 0)

            # Unclaimed fees: prefer on-chain reads — the subgraph's tick
            # feeGrowthOutside values are not reliably flipped when the price
            # crosses a tick, which makes the pure-subgraph math produce
            # garbage right after a boundary crossing (observed: a small
            # position reporting unclaimed fees orders of magnitude above its
            # monthly run-rate the day it re-entered range).
            u0 = u1 = u_usd = Decimal("0")
            rpc_unclaimed_failed = False
            if self._rpc is not None and token_id:
                try:
                    u0, u1, u_usd = compute_unclaimed_fees_v3(
                        self._rpc,
                        V3_FACTORY,
                        NFT_MANAGER,
                        token_id,
                        tick_current,
                        token0.decimals,
                        token1.decimals,
                        p0,
                        p1,
                        nft_header_words=0,
                        int24_third_arg=True,
                    )
                except RuntimeError as e:
                    # The subgraph fallback below is known-unreliable here (it
                    # does not flip feeGrowthOutside on tick crossings), so a
                    # failed RPC read leaves unclaimed UNKNOWN rather than
                    # substituting a number we already distrust.
                    _log.warning(
                        "MachineX unclaimed fees unreadable for tokenId %s (%s)", token_id, e
                    )
                    rpc_unclaimed_failed = True
            else:
                u0, u1, u_usd = compute_unclaimed_fees_pure(
                    liquidity=liquidity,
                    fg_global0=int(pool.get("feeGrowthGlobal0X128") or 0),
                    fg_global1=int(pool.get("feeGrowthGlobal1X128") or 0),
                    fg_out0_lower=int(tick_lower_data.get("feeGrowthOutside0X128") or 0),
                    fg_out1_lower=int(tick_lower_data.get("feeGrowthOutside1X128") or 0),
                    fg_out0_upper=int(tick_upper_data.get("feeGrowthOutside0X128") or 0),
                    fg_out1_upper=int(tick_upper_data.get("feeGrowthOutside1X128") or 0),
                    fg_inside0_last=int(pos.get("feeGrowthInside0LastX128") or 0),
                    fg_inside1_last=int(pos.get("feeGrowthInside1LastX128") or 0),
                    tick_current=tick_current,
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                    decimals0=token0.decimals,
                    decimals1=token1.decimals,
                    price0=p0,
                    price1=p1,
                )

            in_range = tick_lower <= tick_current <= tick_upper
            fee_tier = int(pool.get("feeTier") or 0)
            pool_addr = (pool.get("id") or "unknown").lower()
            # Pool fees from the pool's own Swap logs on either path, so
            # negative carry does not depend on which source enumerated it.
            seven_day_fees, share, pool_liquidity = (
                self._pool_context(
                    self._rpc,
                    pool_addr,
                    fee_tier,
                    liquidity,
                    in_range,
                    p0,
                    p1,
                    token0.decimals,
                    token1.decimals,
                )  # fmt: skip
                if self._rpc is not None and pool_addr != "unknown"
                else (None, None, None)
            )

            return Position(
                seven_day_fees_usd=seven_day_fees,
                position_share_pct=share,
                wallet=wallet.lower(),
                chain=chain,
                protocol_id=self.info.protocol_id,
                position_key=f"{pool_addr}:{tick_lower}:{tick_upper}:{token_id}",
                protocol_kind=ProtocolKind.CL_AMM,
                pair_label=f"{token0.symbol}/{token1.symbol} {fee_tier / 10000:.2f}%",
                tokens=[token0, token1],
                current_value_usd=lp_value_usd,
                current_balances=[TokenAmount(token0, current0), TokenAmount(token1, current1)],
                unclaimed_usd=None if rpc_unclaimed_failed else u_usd,
                unclaimed_balances=[TokenAmount(token0, u0), TokenAmount(token1, u1)],
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                tick_current=tick_current,
                in_range=in_range,
                liquidity=liquidity,
                pool_address=pool_addr,
                fee_tier_bps=fee_tier // 100,
                snapshot_at=int(time.time()),
                # source: which discovery path produced this position. The
                # subgraph is what enumerates positions and prices them here
                # (unclaimed fees are still read over RPC, which is normal
                # operation, not degradation). The degraded counterpart is
                # 'rpc_fallback', set in _fetch_positions_rpc when the
                # subgraph is unavailable.
                meta=_with_pool_liquidity(
                    {"token_id": token_id, "source": "subgraph"}, pool_liquidity
                ),
            )
        except Exception as e:
            raise RuntimeError(f"CL position {pos.get('id')} unbuildable: {e}") from e

    def _build_legacy_position(self, wallet: str, chain: Chain, pos: dict) -> Position | None:
        try:
            pool = pos.get("pool") or {}
            t0 = pos.get("token0") or {}
            t1 = pos.get("token1") or {}

            liquidity = Decimal(str(pos.get("liquidity") or "0"))
            total_supply = Decimal(str(pool.get("totalSupply") or "0"))
            if liquidity <= 0 or total_supply <= 0:
                return None

            token0 = self._token_from_subgraph(chain, t0)
            token1 = self._token_from_subgraph(chain, t1)
            self._storage.upsert_token(token0)
            self._storage.upsert_token(token1)

            share = liquidity / total_supply
            current0 = share * Decimal(str(pool.get("totalValueLockedToken0") or "0"))
            current1 = share * Decimal(str(pool.get("totalValueLockedToken1") or "0"))

            p0 = _subgraph_price(t0)
            p1 = _subgraph_price(t1)
            if p0 == 0:
                pp = self.get_current_price(token0)
                if pp:
                    p0 = pp.price_usd
            if p1 == 0:
                pp = self.get_current_price(token1)
                if pp:
                    p1 = pp.price_usd

            if p0 > 0:
                self._current_price_cache[token0.key] = p0
            if p1 > 0:
                self._current_price_cache[token1.key] = p1

            lp_value_usd = current0 * p0 + current1 * p1
            fee_tier = int(pool.get("feeTier") or 0)
            pool_addr = (pool.get("id") or "unknown").lower()

            return Position(
                wallet=wallet.lower(),
                chain=chain,
                protocol_id=self.info.protocol_id,
                position_key=f"legacy:{pool_addr}:{pos.get('id', '')}",
                protocol_kind=ProtocolKind.XYK_AMM,
                pair_label=f"{token0.symbol}/{token1.symbol} XYK",
                tokens=[token0, token1],
                current_value_usd=lp_value_usd,
                current_balances=[TokenAmount(token0, current0), TokenAmount(token1, current1)],
                unclaimed_usd=Decimal("0"),
                unclaimed_balances=[
                    TokenAmount(token0, Decimal("0")),
                    TokenAmount(token1, Decimal("0")),
                ],
                pool_address=pool_addr,
                fee_tier_bps=fee_tier // 100,
                in_range=True,
                snapshot_at=int(time.time()),
                meta={"pool_share": float(share), "source": "subgraph"},
            )
        except Exception as e:
            raise RuntimeError(f"legacy position {pos.get('id')} unbuildable: {e}") from e

    # ── RPC fallback (subgraph outage) ────────────────────────────────────
    #
    # UniV2-style pair reads, for the legacy XYK positions. They live here
    # rather than in _cl_math because that module is concentrated-liquidity
    # math and a constant-product pair is not.
    _SEL_TOTAL_SUPPLY = bytes.fromhex("18160ddd")  # totalSupply()
    _SEL_BALANCE_OF = bytes.fromhex("70a08231")  # balanceOf(address)
    _SEL_GET_RESERVES = bytes.fromhex("0902f1ac")  # getReserves()
    _SEL_TOKEN0 = bytes.fromhex("0dfe1681")  # token0()
    _SEL_TOKEN1 = bytes.fromhex("d21220a7")  # token1()
    _SEL_POOL_FEE = bytes.fromhex("ddca3f43")  # fee() on the CL pool

    def fetch_positions_at(self, wallet: str, chain: Chain, block: int, ts: int) -> list[Position]:
        """This wallet's positions as they stood at `block`, from archive state
        (the QuikNode peaq endpoint is an archive node) valued at that day's
        prices. The same chain reads as the live RPC path, pinned to a block."""
        if self._rpc is None or chain != Chain.PEAQ:
            return []
        self._as_of_ts = ts
        try:
            positions = self._fetch_positions_rpc(wallet, chain, rpc=self._rpc.pinned(block))
        finally:
            self._as_of_ts = None
        for p in positions:
            p.snapshot_at = ts
            p.meta = {**p.meta, "source": "archive", "block": block}
        return positions

    def _fetch_positions_rpc(
        self, wallet: str, chain: Chain, rpc: RpcClient | None = None
    ) -> list[Position]:
        """Rebuild this wallet's open positions from chain reads alone.

        Reached only when the subgraph is unavailable. This is a stronger
        fallback than the pancake_infinity one it is modelled on: MachineX
        uses a standard V3 NonfungiblePositionManager, so the CL side is
        ENUMERATED live (balanceOf + tokenOfOwnerByIndex) rather than
        reconstructed from the last snapshot. A tokenId the manager does not
        return really is not held, so an empty enumeration is trustworthy and
        the resulting tombstone is correct.

        The legacy XYK positions have no such registry, so their pool
        addresses come from the last live snapshot and the BALANCE is then
        read on-chain — the stored row supplies identity only, never a value.

        Anything that cannot be established raises. Returning a partial list
        would tombstone the positions left out, which is exactly the silent
        loss this whole path exists to avoid. What is genuinely absent from a
        chain read — pool TVL and 7d pool fees — is left None rather than
        carried forward from the last good subgraph pass; a frozen number that
        renders as current is worse than a missing one.
        """
        if self._rpc is None:
            raise RuntimeError(
                "MachineX subgraph is down and PEAQ_RPC_URL is not set — cannot "
                "establish what this wallet holds; refusing to report an empty book"
            )

        # Raises when the RPC will not answer, which is what keeps "holds
        # nothing" and "could not ask" apart (see rpc_enumerate_positions_v3).
        rpc = rpc or self._rpc
        token_ids = rpc_enumerate_positions_v3(rpc, NFT_MANAGER, wallet)
        positions = [
            p
            for tid in token_ids
            if (p := self._cl_position_rpc(rpc, wallet, chain, tid)) is not None
        ]
        positions.extend(self._legacy_positions_rpc(rpc, wallet, chain))
        _log.info(
            "MachineX RPC fallback rebuilt %d position(s) for %s", len(positions), wallet[:10]
        )
        return positions

    def _cl_position_rpc(
        self, rpc: RpcClient, wallet: str, chain: Chain, token_id: int
    ) -> Position | None:
        """One CL position from chain reads, or None when it holds no liquidity.

        None is a genuine close (the NFT survives an exit with liquidity 0 and
        stays enumerable), so omitting it produces the correct tombstone. Every
        other failure raises.
        """
        raw = rpc_get_position_v3(rpc, NFT_MANAGER, token_id, nft_header_words=0)
        if raw is None:
            raise RuntimeError(f"MachineX: positions({token_id}) unreadable during RPC fallback")
        # Third field is TICK SPACING, not a fee: this Ramses-lite manager
        # keys pools that way, which is why getPool takes it as an int24. The
        # fee is a property of the pool and has to be read from it.
        token0_addr, token1_addr, tick_spacing, tick_lower, tick_upper, liquidity, _, _ = raw
        if liquidity <= 0:
            return None

        token0 = self._token_rpc(rpc, chain, token0_addr)
        token1 = self._token_rpc(rpc, chain, token1_addr)
        pool_addr = rpc_get_pool_address_v3(
            rpc, V3_FACTORY, token0_addr, token1_addr, tick_spacing, int24_third_arg=True
        )
        if not pool_addr:
            raise RuntimeError(f"MachineX: getPool unreadable for tokenId {token_id}")
        # Same units as the subgraph's feeTier (hundredths of a bip), so the
        # label this builds is identical to the one the subgraph path builds —
        # a position must not change its name depending on which path ran.
        fee_tier = self._pool_uint(rpc, pool_addr, self._SEL_POOL_FEE)
        slot0 = rpc_get_slot0_v3(rpc, pool_addr)
        if slot0 is None:
            raise RuntimeError(f"MachineX: slot0 unreadable on pool {pool_addr}")
        sqrt_price_x96, tick_current = slot0

        raw0, raw1 = amounts_from_liquidity(
            sqrt_price_x96,
            tick_to_sqrt_price(tick_lower),
            tick_to_sqrt_price(tick_upper),
            liquidity,
        )
        current0 = Decimal(raw0) / Decimal(10**token0.decimals)
        current1 = Decimal(raw1) / Decimal(10**token1.decimals)

        p0, p1 = self._prices_rpc(token0, token1, sqrt_price_x96)

        u0 = u1 = Decimal("0")
        unclaimed: Decimal | None = Decimal("0")
        try:
            u0, u1, unclaimed = compute_unclaimed_fees_v3(
                rpc,
                V3_FACTORY,
                NFT_MANAGER,
                token_id,
                tick_current,
                token0.decimals,
                token1.decimals,
                p0,
                p1,
                nft_header_words=0,
                int24_third_arg=True,
            )
        except RuntimeError as e:
            # Same rule as the subgraph path: unreadable is unknown, not zero.
            # It does NOT invalidate the position — the principal above came
            # from reads that did succeed.
            _log.warning("MachineX unclaimed fees unreadable for tokenId %s (%s)", token_id, e)
            unclaimed = None

        seven_day_fees, share, pool_liquidity = self._pool_context(
            rpc, pool_addr.lower(), fee_tier, liquidity, tick_lower <= tick_current <= tick_upper,
            p0, p1, token0.decimals, token1.decimals,
        )  # fmt: skip
        return Position(
            wallet=wallet.lower(),
            chain=chain,
            protocol_id=self.info.protocol_id,
            seven_day_fees_usd=seven_day_fees,
            position_share_pct=share,
            # Must match the subgraph path byte for byte or the position
            # forks into two identities and its event history is orphaned.
            position_key=f"{pool_addr.lower()}:{tick_lower}:{tick_upper}:{token_id}",
            protocol_kind=ProtocolKind.CL_AMM,
            pair_label=(
                f"{token0.symbol}/{token1.symbol} {fee_tier / 10000:.2f}%"
                if fee_tier
                else f"{token0.symbol}/{token1.symbol}"
            ),
            tokens=[token0, token1],
            current_value_usd=current0 * p0 + current1 * p1,
            current_balances=[TokenAmount(token0, current0), TokenAmount(token1, current1)],
            unclaimed_usd=unclaimed,
            unclaimed_balances=[TokenAmount(token0, u0), TokenAmount(token1, u1)],
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            tick_current=tick_current,
            in_range=tick_lower <= tick_current <= tick_upper,
            liquidity=liquidity,
            pool_address=pool_addr.lower(),
            # None rather than a guess: an unreadable fee is unknown, and a
            # wrong tier would misstate the pool this position is even in.
            fee_tier_bps=fee_tier // 100 if fee_tier else None,
            snapshot_at=int(time.time()),
            meta=_with_pool_liquidity(
                {"token_id": token_id, "source": "rpc_fallback"}, pool_liquidity
            ),
        )

    def _legacy_positions_rpc(self, rpc: RpcClient, wallet: str, chain: Chain) -> list[Position]:
        """XYK positions, identified from stored snapshots and valued on-chain.

        The stored row contributes the pool address and nothing else: share,
        reserves and totals are all read live, so a wallet that has since
        exited reads balanceOf 0 and is correctly left out.
        """
        out: list[Position] = []
        seen: set[str] = set()
        for row in self._storage.all_position_identities(wallet, chain, self.info.protocol_id):
            key = str(row["position_key"])
            if not key.startswith("legacy:"):
                continue
            parts = key.split(":")
            if len(parts) != 3:
                continue
            pool_addr = parts[1].lower()
            if pool_addr in seen:
                continue
            seen.add(pool_addr)
            pos = self._legacy_position_rpc(rpc, wallet, chain, pool_addr, key)
            if pos is not None:
                out.append(pos)
        return out

    def _legacy_position_rpc(
        self, rpc: RpcClient, wallet: str, chain: Chain, pool_addr: str, position_key: str
    ) -> Position | None:
        balance = self._pool_uint(rpc, pool_addr, self._SEL_BALANCE_OF + _abi_address(wallet))
        if balance is None:
            raise RuntimeError(f"MachineX: balanceOf unreadable on XYK pool {pool_addr}")
        if balance == 0:
            return None  # genuinely exited — the tombstone is correct
        total_supply = self._pool_uint(rpc, pool_addr, self._SEL_TOTAL_SUPPLY)
        if not total_supply:
            raise RuntimeError(f"MachineX: totalSupply unreadable on XYK pool {pool_addr}")

        reserves = rpc.eth_call(pool_addr, self._SEL_GET_RESERVES)
        if not reserves or len(reserves) < 64:
            raise RuntimeError(f"MachineX: getReserves unreadable on XYK pool {pool_addr}")
        t0_raw = rpc.eth_call(pool_addr, self._SEL_TOKEN0)
        t1_raw = rpc.eth_call(pool_addr, self._SEL_TOKEN1)
        if not t0_raw or not t1_raw:
            raise RuntimeError(f"MachineX: token0/token1 unreadable on XYK pool {pool_addr}")
        token0 = self._token_rpc(rpc, chain, _decode_address(t0_raw, 0))
        token1 = self._token_rpc(rpc, chain, _decode_address(t1_raw, 0))

        share = Decimal(balance) / Decimal(total_supply)
        current0 = share * Decimal(_decode_uint256(reserves, 0)) / Decimal(10**token0.decimals)
        current1 = share * Decimal(_decode_uint256(reserves, 32)) / Decimal(10**token1.decimals)
        # No sqrtPrice on a constant-product pair, so the pool-ratio fallback
        # is not available here: both legs must price on their own.
        p0, p1 = self._prices_rpc(token0, token1, sqrt_price_x96=0)

        return Position(
            wallet=wallet.lower(),
            chain=chain,
            protocol_id=self.info.protocol_id,
            position_key=position_key,
            protocol_kind=ProtocolKind.XYK_AMM,
            pair_label=f"{token0.symbol}/{token1.symbol} XYK",
            tokens=[token0, token1],
            current_value_usd=current0 * p0 + current1 * p1,
            current_balances=[TokenAmount(token0, current0), TokenAmount(token1, current1)],
            unclaimed_usd=Decimal("0"),
            unclaimed_balances=[
                TokenAmount(token0, Decimal("0")),
                TokenAmount(token1, Decimal("0")),
            ],
            pool_address=pool_addr,
            in_range=True,
            snapshot_at=int(time.time()),
            meta={"pool_share": float(share), "source": "rpc_fallback"},
        )

    _SEL_POOL_LIQUIDITY = bytes.fromhex("1a686502")  # liquidity() — active, in-range

    def _pool_context(
        self,
        rpc: RpcClient,
        pool_addr: str,
        fee_ppm: int | None,
        liquidity: int,
        in_range: bool,
        p0: Decimal,
        p1: Decimal,
        dec0: int,
        dec1: int,
    ) -> tuple[Decimal | None, Decimal | None, int | None]:
        """(pool 7-day fees USD, this position's % of active liquidity, the
        pool's active liquidity in raw units) from the pool's own Swap logs
        and liquidity() — each unknown (None), never zero, when it cannot be
        read. Out of range, the position's share is 0: it earns nothing from
        the swaps that happen there. The pool's liquidity is read either way:
        the range optimiser needs it to price a re-entry."""
        from defi_tracker.adapters._pool_activity import V3_SWAP_TOPIC, pool_activity

        if not fee_ppm or self._as_of_ts is not None:
            # A trailing-week fee window read today says nothing about a past day.
            return None, None, None
        try:
            act = pool_activity(
                rpc, cache=self._storage, cache_key=f"pool_activity:peaq:{pool_addr}",
                address=pool_addr, topics=[V3_SWAP_TOPIC], price0=p0, price1=p1,
                dec0=dec0, dec1=dec1, fee_ppm=fee_ppm, chunk=self.LEGACY_CHUNK,
            )  # fmt: skip
        except Exception as e:
            _log.warning("MachineX pool activity unreadable for %s (%s)", pool_addr, e)
            return None, None, None
        active = self._pool_uint(rpc, pool_addr, self._SEL_POOL_LIQUIDITY)
        if not in_range:
            return act.fees_usd, Decimal("0"), active
        share = Decimal(liquidity) / Decimal(active) * 100 if active else None
        return act.fees_usd, share, active

    def _pool_uint(self, rpc: RpcClient, pool_addr: str, data: bytes) -> int | None:
        result = rpc.eth_call(pool_addr, data)
        if not result or len(result) < 32:
            return None
        return _decode_uint256(result, 0)

    # ── Pool flow history (Phase 5.3) ─────────────────────────────────────

    def position_pool(self, position_key: str) -> tuple[str, int, int] | None:
        # CL keys are 'pool:lower:upper:tokenId'; XYK keys start 'legacy:'.
        parts = position_key.split(":")
        if len(parts) != 4 or not parts[0].startswith("0x"):
            return None
        try:
            return parts[0].lower(), int(parts[1]), int(parts[2])
        except ValueError:
            return None

    def pool_info(self, chain: Chain, pool: str) -> PoolInfo:
        if self._rpc is None:
            raise RuntimeError("no peaq RPC")
        rpc = self._rpc
        t0_raw, t1_raw = rpc.eth_call(pool, self._SEL_TOKEN0), rpc.eth_call(pool, self._SEL_TOKEN1)
        fee = self._pool_uint(rpc, pool, self._SEL_POOL_FEE)
        if not t0_raw or not t1_raw or fee is None:
            raise RuntimeError(f"pool {pool} unreadable")
        tok0 = self._token_rpc(rpc, chain, "0x" + t0_raw[12:32].hex())
        tok1 = self._token_rpc(rpc, chain, "0x" + t1_raw[12:32].hex())
        return PoolInfo(
            chain=chain.value, pool=pool.lower(), token0=tok0.address, token1=tok1.address,
            symbol0=tok0.symbol, symbol1=tok1.symbol, dec0=tok0.decimals, dec1=tok1.decimals,
            fee_ppm=int(fee),  # Uniswap units are parts per million: 10000 = 1.00%
        )  # fmt: skip

    def swap_history_start(self, chain: Chain) -> int:
        return DEPLOY_BLOCK

    def scan_swaps(
        self, chain: Chain, pools: list[str], from_block: int, to_block: int
    ) -> list[PoolSwap]:
        """peaq's eth_getLogs carries no blockTimestamp, so a swap's time is
        interpolated between the window's two end blocks — minutes of error
        on a window of a few hours, which only matters at a day boundary."""
        from defi_tracker.adapters._pool_activity import V3_SWAP_TOPIC

        if self._rpc is None:
            raise RuntimeError("no peaq RPC")
        rpc = self._rpc
        logs = rpc.get_logs_chunked(
            [p.lower() for p in pools], [V3_SWAP_TOPIC], from_block, to_block,
            # Bot bursts put dozens of swaps in a block, and the provider caps a
            # response at 10,000 results — so a refusal may halve to one block.
            chunk_size=self.LEGACY_CHUNK, min_chunk=1,
        )  # fmt: skip
        if not logs:
            return []
        ts_a, ts_b = rpc.get_block_timestamp(from_block), rpc.get_block_timestamp(to_block)
        if ts_a is None or ts_b is None:
            raise RuntimeError(f"block timestamps unreadable for {from_block}–{to_block}")
        span = max(1, to_block - from_block)

        def signed(v: int) -> int:
            return v - (1 << 256) if v >= 1 << 255 else v

        out = []
        for lg in logs:
            block = int(lg["blockNumber"], 16)
            raw = lg["data"][2:]
            w = [int(raw[i : i + 64], 16) for i in range(0, 320, 64)]
            ts = lg.get("blockTimestamp")
            out.append(
                PoolSwap(
                    pool=str(lg["address"]).lower(),
                    block=block,
                    log_index=int(lg["logIndex"], 16),
                    ts=int(ts, 16) if ts else ts_a + (ts_b - ts_a) * (block - from_block) // span,
                    tick=signed(w[4]),
                    liquidity=w[3],
                    amount0=signed(w[0]),
                    amount1=signed(w[1]),
                )  # fmt: skip
            )
        out.sort(key=lambda x: (x.block, x.log_index))
        return out

    def _token_rpc(self, rpc: RpcClient, chain: Chain, address: str) -> Token:
        """A Token for an address, preferring the registry and falling back to
        an ERC20 read. Raises when neither yields real decimals: scaling a raw
        balance by a guessed 18 silently misprices the position by orders of
        magnitude, and this path exists precisely because nothing else is
        double-checking the number."""
        addr = address.lower()
        with self._storage.connect() as conn:
            row = conn.execute(
                "SELECT symbol, decimals FROM tokens WHERE chain=? AND address=?",
                (chain.value, addr),
            ).fetchone()
        if row and not (row["symbol"].startswith("0x") and "…" in row["symbol"]):
            return Token(chain, addr, row["symbol"], row["decimals"])
        symbol, decimals = rpc.erc20_metadata(addr)
        if not symbol or decimals is None:
            raise RuntimeError(f"MachineX: cannot read ERC20 metadata for {addr}")
        token = Token(chain, addr, symbol, decimals)
        self._storage.upsert_token(token)
        return token

    def _prices_rpc(
        self, token0: Token, token1: Token, sqrt_price_x96: int
    ) -> tuple[Decimal, Decimal]:
        """USD prices for both legs, with the pool ratio as a second source.

        The subgraph normally supplies priceUSD for tokens CoinGecko has never
        heard of, so losing it is the real pricing risk in this path — not
        losing the position list. When exactly one leg prices externally, the
        pool's own sqrtPrice gives the other, which covers the shape this book
        actually has (a long tail of peaq tokens quoted against WPEAQ or a
        stablecoin).

        Raises when a leg is still unpriced. Valuing it at zero would quietly
        understate the book, and understating it during an outage is the
        failure this whole path is meant to prevent.
        """
        p0 = self._price_or_zero(token0)
        p1 = self._price_or_zero(token1)
        if p0 > 0 and p1 > 0:
            return p0, p1
        if sqrt_price_x96 > 0:
            # price1=1 turns price_from_sqrt into the bare token1-per-token0
            # exchange rate, which is what both derivations below need.
            ratio = self.price_from_sqrt(
                sqrt_price_x96, token0.decimals, token1.decimals, Decimal("1")
            )
            if ratio and ratio > 0:
                # ratio is token1 per token0, so either leg derives the other.
                if p0 <= 0 < p1:
                    p0 = ratio * p1
                elif p1 <= 0 < p0:
                    p1 = p0 / ratio
        if p0 <= 0 or p1 <= 0:
            unpriced = [t.symbol for t, p in ((token0, p0), (token1, p1)) if p <= 0]
            raise RuntimeError(
                f"MachineX RPC fallback cannot price {', '.join(unpriced)} — refusing to "
                "report the position below its real value"
            )
        return p0, p1

    def _price_or_zero(self, token: Token) -> Decimal:
        point = self.price_now_or_then(token)
        price = point.price_usd if point else Decimal("0")
        if price > 0 and self._as_of_ts is None:
            # The runner's analytics read this cache instead of re-fetching.
            self._current_price_cache[token.key] = price
        return price

    # ── Events ────────────────────────────────────────────────────────────

    def _iter_event_batches(
        self,
        wallet: str,
        chain: Chain,
        since_ts: int = 0,
        deadline: float | None = None,
    ) -> Iterator[list[Event]]:
        """
        Scan IncreaseLiquidity / DecreaseLiquidity / Collect logs for all
        current tokenIds, yielding one batch per WINDOW_BLOCKS block window.

        The block cursor (persisted in kv_store) advances only after the
        consumer resumes the generator — i.e. after the runner has persisted
        the batch — so an interrupted run resumes from the last completed
        window. Requires PEAQ_RPC_URL; skips gracefully if absent.

        Every exit path records whether the cursor ended up at the chain head
        (last_scan_reached_head): this scan is capped at MAX_BLOCKS_PER_RUN
        blocks per run and also stops on the deadline, so returning normally
        routinely means "banked progress", not "caught up".
        """
        # Pessimistic until an exit path proves otherwise — an abandoned
        # generator (consumer force-stop) must read as incomplete.
        self._scan_reached_head[self._scan_key(wallet, chain)] = False
        if chain != Chain.PEAQ:
            # Not our chain: nothing to scan, so nothing is outstanding.
            self._scan_reached_head[self._scan_key(wallet, chain)] = True
            return
        if self._rpc is None:
            # No RPC → events are never ingested at all. That is a permanently
            # behind sync, not a healthy one.
            _log.debug("No PEAQ_RPC_URL — skipping MachineX event scan")
            return

        try:
            token_ids = rpc_enumerate_positions_v3(self._rpc, NFT_MANAGER, wallet)
        except RuntimeError as e:
            # Could not establish what this wallet holds. Skipping the cursor
            # forward now would step past a range nobody scanned, and the cursor
            # never goes back — so do nothing and stay marked incomplete.
            _log.warning(
                "MachineX position enumeration failed for %s (%s) — cursor held, "
                "scan retries next run",
                wallet[:10],
                e,
            )
            return

        # Every held tokenId must be readable, pool included. One skipped
        # token's events would be stepped past by the cursor — a permanent
        # cost-basis gap that no dropped-event count reports — and a guessed
        # pool would write a second identity for it into immutable events.
        live_meta: dict[int, tuple[str, Token, Token]] = {}
        for token_id in token_ids:
            raw = rpc_get_position_v3(self._rpc, NFT_MANAGER, token_id, nft_header_words=0)
            if not raw:
                _log.warning(
                    "MachineX positions(%d) unreadable for %s — cursor held",
                    token_id,
                    wallet[:10],
                )
                return
            token0_addr, token1_addr, fee, tick_lower, tick_upper, _, _, _ = raw
            pool = rpc_get_pool_address_v3(
                self._rpc, V3_FACTORY, token0_addr, token1_addr, fee, int24_third_arg=True
            )
            if not pool:
                _log.warning(
                    "MachineX getPool unreadable for token %d (%s) — cursor held",
                    token_id,
                    wallet[:10],
                )
                return
            token0 = self._token_from_address(chain, token0_addr)
            token1 = self._token_from_address(chain, token1_addr)
            self._storage.upsert_token(token0)
            self._storage.upsert_token(token1)
            position_key = f"{pool}:{tick_lower}:{tick_upper}:{token_id}"
            live_meta[token_id] = (position_key, token0, token1)

        # A position NFT that was burned or transferred stops being enumerated,
        # so its final Decrease/Collect would never be requested and the cursor
        # would walk straight past them. Fold in every tokenId this wallet has
        # held, metadata and filter together (see _historical_tid_meta).
        tid_meta = dict(live_meta)
        for tid, meta in self._historical_tid_meta(wallet, chain, exclude=set(live_meta)).items():
            tid_meta.setdefault(tid, meta)

        if not tid_meta:
            # Nothing held now and nothing ever recorded — genuinely empty.
            self._advance_cursor(wallet, chain)
            return

        tid_topics = ["0x" + tid.to_bytes(32, "big").hex() for tid in tid_meta]

        from_block = self._get_scan_cursor(wallet, chain)
        head = self._rpc.get_block_number()
        # Head unknown (RPC hiccup) → we cannot claim to have caught up.
        latest = head if head else from_block
        to_block = min(from_block + MAX_BLOCKS_PER_RUN, latest)
        # True only if this run's window ends at the head we observed; the
        # 150k cap silently truncates a wallet that has fallen days behind.
        covers_head = head is not None and to_block >= head

        for win_start in range(from_block, to_block + 1, WINDOW_BLOCKS):
            win_end = min(win_start + WINDOW_BLOCKS - 1, to_block)
            events = self._scan_window(wallet, chain, tid_meta, tid_topics, win_start, win_end)
            events += self.scan_legacy_events(wallet, chain, win_start, win_end)
            yield events
            # The runner has persisted this batch — safe to advance. No
            # try/finally: if the consumer's upsert raised, the cursor must
            # NOT move past an unpersisted window.
            self._set_scan_cursor(wallet, chain, win_end + 1)
            # Stop AFTER banking the cursor, not before: if the consumer
            # abandoned the generator on its own deadline instead, this
            # window's progress would be rescanned next run.
            if deadline is not None and time.monotonic() > deadline:
                _log.info(
                    "MachineX scan pausing at block %d for %s (time budget) — resumes next run",
                    win_end + 1,
                    wallet[:10],
                )
                # Everything past win_end is un-scanned — unless win_end IS the
                # target, i.e. the deadline expired on the very last window. In
                # that case the scan genuinely finished and reporting it as
                # truncated would freeze last_sync_ok_at on a healthy pass.
                self._scan_reached_head[self._scan_key(wallet, chain)] = (
                    covers_head and win_end >= to_block
                )
                return

        self._scan_reached_head[self._scan_key(wallet, chain)] = covers_head

    # ── Legacy XYK positions: history from LP-token transfers ─────────────
    #
    # An XYK position has no NFT, so the CL scan above never sees it — which is
    # why the two USDC/USDT XYK positions carried no event history at all, and
    # every period and cost-basis figure had to exclude them. Their history is
    # on-chain all the same: a deposit mints LP to the wallet in the same tx as
    # the pair's Mint(sender, amount0, amount1); a withdrawal sends LP back to
    # the pair alongside Burn(sender, amount0, amount1, to). An LP transfer
    # with neither (between the operator's own wallets, say) moves a share of
    # the reserves, valued from the pair's state at that block (archive read).

    _TOPIC_TRANSFER = "0x" + TOPIC_ERC20_TRANSFER
    _TOPIC_XYK_MINT = "0x" + keccak(b"Mint(address,uint256,uint256)").hex()
    _TOPIC_XYK_BURN = "0x" + keccak(b"Burn(address,uint256,uint256,address)").hex()
    # QuikNode caps eth_getLogs at 10k blocks and 10s per query on peaq; old
    # ranges are slow to index, so start smaller and let refusals halve it.
    LEGACY_CHUNK = 5_000

    def legacy_pools(self, wallet: str, chain: Chain) -> dict[str, str]:
        """{pool address: position_key} for every XYK position on record."""
        out: dict[str, str] = {}
        for row in self._storage.all_position_identities(wallet, chain, self.info.protocol_id):
            key = str(row["position_key"])
            parts = key.split(":")
            if key.startswith("legacy:") and len(parts) == 3:
                out.setdefault(parts[1].lower(), key)
        return out

    def scan_legacy_events(
        self, wallet: str, chain: Chain, from_block: int, to_block: int
    ) -> list[Event]:
        """Every deposit/withdrawal of the wallet's XYK positions in the range."""
        if self._rpc is None or chain != Chain.PEAQ:
            return []
        wtopic = "0x" + "0" * 24 + wallet.lower().removeprefix("0x")
        events: list[Event] = []
        for pool, key in self.legacy_pools(wallet, chain).items():
            txs: set[str] = set()
            directions: list[list[str | list[str] | None]] = [
                [self._TOPIC_TRANSFER, wtopic],
                [self._TOPIC_TRANSFER, None, wtopic],
            ]
            for topics in directions:
                for lg in self._rpc.get_logs_chunked(
                    pool, topics, from_block, to_block, chunk_size=self.LEGACY_CHUNK
                ):
                    txs.add(lg["transactionHash"].lower())
            for tx in sorted(txs):
                events.extend(self._legacy_events_from_tx(wallet, chain, pool, key, tx))
        return events

    def _legacy_events_from_tx(
        self, wallet: str, chain: Chain, pool: str, key: str, tx: str
    ) -> list[Event]:
        assert self._rpc is not None
        receipt = self._rpc.get_transaction_receipt(tx)
        if not receipt:
            raise RuntimeError(f"no receipt for {tx}")  # never skip past an unread tx
        block = int(receipt["blockNumber"], 16)
        wtopic = "0x" + "0" * 24 + wallet.lower().removeprefix("0x")
        zero = "0x" + "0" * 64
        lp_minted = lp_in = lp_out = 0
        first_idx = None
        mint = burn = None
        for lg in receipt.get("logs", []):
            if lg.get("address", "").lower() != pool:
                continue
            t = [x.lower() for x in lg.get("topics", [])]
            idx = int(lg["logIndex"], 16)
            if t and t[0] == self._TOPIC_TRANSFER and len(t) == 3:
                amount = int(lg["data"], 16)
                if t[2] == wtopic:
                    lp_in += amount
                    lp_minted += amount if t[1] == zero else 0
                    first_idx = idx if first_idx is None else first_idx
                if t[1] == wtopic:
                    lp_out += amount
                    first_idx = idx if first_idx is None else first_idx
            elif t and t[0] == self._TOPIC_XYK_MINT:
                w = lg["data"][2:]
                mint = (idx, int(w[:64], 16), int(w[64:128], 16))
            elif t and t[0] == self._TOPIC_XYK_BURN:
                w = lg["data"][2:]
                burn = (idx, int(w[:64], 16), int(w[64:128], 16))
        if first_idx is None:
            return []
        token0, token1 = self._legacy_tokens(pool, chain)
        if mint and lp_minted:
            kind, idx, raw0, raw1 = EventKind.DEPOSIT, mint[0], mint[1], mint[2]
        elif burn and lp_out:
            kind, idx, raw0, raw1 = EventKind.WITHDRAW, burn[0], -burn[1], -burn[2]
        else:
            # An LP transfer with no Mint/Burn: a share of the reserves moved.
            net = lp_in - lp_out
            if net == 0:
                return []
            reserves = self._rpc.eth_call(pool, self._SEL_GET_RESERVES, block=hex(block - 1))
            supply_raw = self._rpc.eth_call(pool, self._SEL_TOTAL_SUPPLY, block=hex(block - 1))
            if not reserves or not supply_raw:
                raise RuntimeError(f"XYK state unreadable at block {block} (archive node needed)")
            supply = _decode_uint256(supply_raw, 0)
            r0, r1 = _decode_uint256(reserves, 0), _decode_uint256(reserves, 32)
            raw0, raw1 = net * r0 // supply, net * r1 // supply
            kind = EventKind.DEPOSIT if net > 0 else EventKind.WITHDRAW
            idx = first_idx
        ts = self._rpc.get_block_timestamp(block)
        if ts is None:
            raise RuntimeError(f"no block timestamp for {tx}")
        a0 = Decimal(raw0) / Decimal(10**token0.decimals)
        a1 = Decimal(raw1) / Decimal(10**token1.decimals)
        pp0, pp1 = self.get_historical_price(token0, ts), self.get_historical_price(token1, ts)
        if pp0 is None or pp1 is None:
            self._note_dropped_event(
                wallet, chain, f"no price for {token0.symbol}/{token1.symbol} at ts={ts}", tx
            )
            return []
        return [
            Event(
                wallet=wallet.lower(),
                chain=chain,
                protocol_id=self.info.protocol_id,
                position_key=key,
                tx_hash=tx,
                log_index=idx,
                ts=ts,
                block_number=block,
                kind=kind,
                amounts=[TokenAmount(token0, a0), TokenAmount(token1, a1)],
                prices_at_ts={token0.key: pp0.price_usd, token1.key: pp1.price_usd},
                usd_at_ts=a0 * pp0.price_usd + a1 * pp1.price_usd,
                meta={"pool": pool, "lp_delta": lp_in - lp_out, "source": "chain_xyk"},
            )
        ]

    def _legacy_tokens(self, pool: str, chain: Chain) -> tuple[Token, Token]:
        assert self._rpc is not None
        t0_raw = self._rpc.eth_call(pool, self._SEL_TOKEN0)
        t1_raw = self._rpc.eth_call(pool, self._SEL_TOKEN1)
        if not t0_raw or not t1_raw:
            raise RuntimeError(f"MachineX: token0/token1 unreadable on XYK pool {pool}")
        return (
            self._token_rpc(self._rpc, chain, _decode_address(t0_raw, 0)),
            self._token_rpc(self._rpc, chain, _decode_address(t1_raw, 0)),
        )

    def fetch_events(self, wallet: str, chain: Chain, since_ts: int = 0) -> list[Event]:
        """Flatten iter_event_batches — kept for the ABC contract and scripts."""
        return [
            ev
            for batch in self.iter_event_batches(wallet, chain, since_ts=since_ts)
            for ev in batch
        ]

    def _scan_window(
        self,
        wallet: str,
        chain: Chain,
        tid_meta: dict[int, tuple[str, Token, Token]],
        tid_topics: list[str],
        from_block: int,
        to_block: int,
    ) -> list[Event]:
        """Fetch and decode all three event topics for one block window.

        Deliberately no since_ts filter: the block cursor already prevents
        rescanning, upserts are idempotent, and dropping by timestamp would
        silently discard historical events when the cursor is rewound (e.g.
        to re-cover a range after fixing a pricing gap)."""
        assert self._rpc is not None  # guarded by iter_event_batches
        events: list[Event] = []

        # One chunked pass with an OR-list of all three topics — the node
        # scans each block range once instead of three times, which is the
        # dominant cost of a window. Logs are classified by topic0 below.
        topic_kind = {
            "0x" + TOPIC_V3_INCREASE_LIQUIDITY: EventKind.DEPOSIT,
            "0x" + TOPIC_V3_DECREASE_LIQUIDITY: EventKind.WITHDRAW,
            "0x" + TOPIC_V3_COLLECT: EventKind.COLLECT,
        }
        logs = self._rpc.get_logs_chunked(
            NFT_MANAGER,
            topics=[list(topic_kind.keys()), tid_topics],
            from_block=from_block,
            to_block=to_block,
            chunk_size=CHUNK_SIZE,
            sleep_between_chunks=CHUNK_SLEEP,
        )

        # Summed raw principal per (tx, tokenId) from DecreaseLiquidity logs —
        # used to net the fee portion out of a bundled Collect below. Collects
        # are deferred to a second pass so every same-tx decrease is summed
        # first regardless of log order.
        decrease_raw: dict[tuple[str, int], tuple[int, int]] = {}
        collect_logs: list[dict] = []
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 2:
                self._note_dropped_event(
                    wallet, chain, "log has no tokenId topic", str(log.get("transactionHash"))
                )
                continue
            kind = topic_kind.get(str(topics[0]).lower())
            if kind is None:
                continue
            raw_tid = int(topics[1], 16)
            if raw_tid not in tid_meta:
                # Fetched, paid for, and unusable: the topic filter asked for a
                # tokenId we cannot describe. Counting it is what catches the
                # filter and tid_meta drifting apart.
                self._note_dropped_event(
                    wallet, chain, "no position metadata for tokenId", str(raw_tid)
                )
                continue
            if kind == EventKind.COLLECT:
                collect_logs.append(log)
                continue
            if kind == EventKind.WITHDRAW:
                decoded = self._decode_log_amounts(log)
                if decoded is not None:
                    dkey = (log.get("transactionHash", ""), raw_tid)
                    prev0, prev1 = decrease_raw.get(dkey, (0, 0))
                    decrease_raw[dkey] = (prev0 + decoded[1], prev1 + decoded[2])
            position_key, token0, token1 = tid_meta[raw_tid]
            ev = self._log_to_event(log, wallet, chain, position_key, token0, token1, kind)
            if ev:
                events.append(ev)

        # Collect: fires on every fee harvest. Standalone Collects are pure
        # fee payouts and recorded in full. A Collect bundled in the same tx
        # as a DecreaseLiquidity pays out principal + fees; the principal is
        # already captured by the WITHDRAW event above, so record only the
        # difference (collect − decrease, clamped at 0) as collected fees.
        for log in collect_logs:
            raw_tid = int(log["topics"][1], 16)
            dkey = (log.get("transactionHash", ""), raw_tid)
            fee_override: tuple[int, int] | None = None
            netted = False
            if dkey in decrease_raw:
                decoded = self._decode_log_amounts(log)
                if decoded is None:
                    self._note_dropped_event(
                        wallet, chain, "could not decode bundled Collect", str(dkey[0])
                    )
                    continue
                # pop: if a tx somehow carries a second Collect for the same
                # tokenId, treat it as a standalone harvest rather than
                # netting against the same principal twice.
                dec0, dec1 = decrease_raw.pop(dkey)
                fee0 = max(decoded[1] - dec0, 0)
                fee1 = max(decoded[2] - dec1, 0)
                if fee0 == 0 and fee1 == 0:
                    continue  # pure principal payout, nothing collected
                fee_override = (fee0, fee1)
                netted = True
            position_key, token0, token1 = tid_meta[raw_tid]
            ev = self._log_to_event(
                log,
                wallet,
                chain,
                position_key,
                token0,
                token1,
                EventKind.COLLECT,
                raw_amounts=fee_override,
            )
            if ev:
                if netted:
                    ev.meta["netted_against_decrease"] = True
                events.append(ev)

        events.sort(key=lambda e: (e.ts, e.log_index))
        return events

    @staticmethod
    def _decode_log_amounts(log: dict) -> tuple[int, int, int] | None:
        """Return (word0, amount0_raw, amount1_raw) from a log's data field.

        Works for IncreaseLiquidity / DecreaseLiquidity (word0 = liquidity)
        and Collect (word0 = padded recipient address) — amount0/amount1 sit
        at the same byte offsets in all three. None if the data is malformed.
        """
        raw = log.get("data", "")
        try:
            data = bytes.fromhex(raw[2:] if raw.startswith(("0x", "0X")) else raw)
        except ValueError:
            return None
        if len(data) < 96:
            return None
        return (
            int.from_bytes(data[0:32], "big"),
            int.from_bytes(data[32:64], "big"),
            int.from_bytes(data[64:96], "big"),
        )

    def _log_to_event(
        self,
        log: dict,
        wallet: str,
        chain: Chain,
        position_key: str,
        token0: Token,
        token1: Token,
        kind: EventKind,
        raw_amounts: tuple[int, int] | None = None,
    ) -> Event | None:
        """Decode an IncreaseLiquidity / DecreaseLiquidity / Collect log into an Event.

        Collect's data layout differs from the other two: it's
        (address recipient, uint256 amount0, uint256 amount1) — no liquidity
        word — so amount0/amount1 sit at the same byte offsets but the first
        word is a padded address, not a liquidity value.

        raw_amounts overrides the decoded token amounts (raw integer units) —
        used by _scan_window to record the netted fee portion of a Collect
        that's bundled with a DecreaseLiquidity.
        """
        try:
            decoded = self._decode_log_amounts(log)
            if decoded is None:
                self._note_dropped_event(
                    wallet, chain, "could not decode log data", str(log.get("transactionHash"))
                )
                return None
            word0, amt0_raw, amt1_raw = decoded
            liquidity = None if kind == EventKind.COLLECT else (word0 & ((1 << 128) - 1))
            if raw_amounts is not None:
                amt0_raw, amt1_raw = raw_amounts

            amt0 = Decimal(amt0_raw) / Decimal(10**token0.decimals)
            amt1 = Decimal(amt1_raw) / Decimal(10**token1.decimals)

            if kind == EventKind.WITHDRAW:
                amt0 = -amt0
                amt1 = -amt1

            block_number = int(log.get("blockNumber") or "0x0", 16)
            tx_hash = log.get("transactionHash", "")
            log_index = int(log.get("logIndex") or "0x0", 16)

            # Standard eth_getLogs has no timestamp field; some RPCs add one.
            ts = int(log.get("blockTimestamp") or log.get("timeStamp") or "0x0", 16)
            if not ts and block_number and self._rpc is not None:
                ts = self._rpc.get_block_timestamp(block_number) or 0
            if not ts:
                self._note_dropped_event(
                    wallet,
                    chain,
                    f"could not resolve block timestamp for block {block_number}",
                    tx_hash,
                )
                return None

            pp0 = self.get_historical_price(token0, ts)
            pp1 = self.get_historical_price(token1, ts)
            p0: Decimal | None = pp0.price_usd if pp0 else None
            p1: Decimal | None = pp1.price_usd if pp1 else None

            def _ratio_fill(
                p0: Decimal | None, p1: Decimal | None
            ) -> tuple[Decimal | None, Decimal | None]:
                abs0, abs1 = abs(amt0), abs(amt1)
                if p0 is None and p1 is not None and abs1 > 0 and abs0 > 0:
                    return abs1 * p1 / abs0, p1
                if p1 is None and p0 is not None and abs0 > 0 and abs1 > 0:
                    return p0, abs0 * p0 / abs1
                return p0, p1

            p0, p1 = _ratio_fill(p0, p1)

            # Last resort: subgraph day price, then block-level time travel
            # (single attempts, negatively cached — see _subgraph_historical_price).
            if p0 is None:
                p0 = self._subgraph_historical_price(token0.address, block_number, ts)
            if p1 is None:
                p1 = self._subgraph_historical_price(token1.address, block_number, ts)
            p0, p1 = _ratio_fill(p0, p1)

            if p0 is None or p1 is None:
                self._note_dropped_event(
                    wallet,
                    chain,
                    f"no price for {token0.symbol}/{token1.symbol} at ts={ts}",
                    tx_hash,
                )
                return None

            usd_at_ts = amt0 * p0 + amt1 * p1

            return Event(
                wallet=wallet.lower(),
                chain=chain,
                protocol_id=self.info.protocol_id,
                position_key=position_key,
                tx_hash=tx_hash,
                log_index=log_index,
                ts=ts,
                block_number=block_number,
                kind=kind,
                amounts=[TokenAmount(token0, amt0), TokenAmount(token1, amt1)],
                prices_at_ts={token0.key: p0, token1.key: p1},
                usd_at_ts=usd_at_ts,
                meta={"liquidity_delta": liquidity},
            )
        except Exception as e:
            self._note_dropped_event(
                wallet, chain, f"failed to decode log: {e}", str(log.get("transactionHash"))
            )
            return None

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _token_from_subgraph(chain: Chain, t: dict) -> Token:
        return Token(
            chain=chain,
            address=(t.get("id") or "").lower(),
            symbol=t.get("symbol") or "?",
            decimals=int(t.get("decimals") or 18),
        )

    _MAX_HISTORICAL_TOKEN_IDS = 50

    def _token_from_key_strict(self, chain: Chain, token_key: str) -> Token | None:
        """A Token for a stored token_key, or None when its decimals cannot be trusted.

        Deliberately NOT _token_from_address: that one falls back to an
        18-decimal address stub, which is fine for a display label but
        catastrophic here. These tokens exist only to scale raw log amounts, so
        a wrong `decimals` turns a recovered event into a silently wrong number
        in the ledger — worse than the missing event this is trying to recover.
        Returning None just leaves that position where it is today.
        """
        addr = token_key.split(":", 1)[-1].lower()
        with self._storage.connect() as conn:
            row = conn.execute(
                "SELECT symbol, decimals FROM tokens WHERE chain=? AND address=?",
                (chain.value, addr),
            ).fetchone()
        if row:
            sym = row["symbol"]
            if not ("…" in sym and sym.startswith("0x")):
                return Token(chain, addr, sym, row["decimals"])
        # Registry only has a stub. The ERC20 still exists even when the
        # position NFT was burned, so one RPC read is worth trying.
        if self._rpc is not None:
            symbol, decimals = self._rpc.erc20_metadata(addr)
            if symbol and decimals is not None:
                tok = Token(chain, addr, symbol, decimals)
                self._storage.upsert_token(tok)
                return tok
        return None

    def _historical_tid_meta(
        self, wallet: str, chain: Chain, exclude: set[int]
    ) -> dict[int, tuple[str, Token, Token]]:
        """tid_meta entries for tokenIds this wallet held once and no longer does.

        Widening the topic filter alone would be strictly worse than doing
        nothing: _scan_window gates every log on `raw_tid in tid_meta`, so the
        extra logs would be fetched, paid for, and discarded while the cursor
        advanced past them. Filter and metadata are therefore built from the
        same source and returned together.

        The stored position_key is reused verbatim rather than rebuilt from the
        pool/tick fields: an event has to join to the same position the
        snapshots already use, and for a burned NFT rpc_get_position_v3 returns
        None so there is nothing to rebuild it from anyway.
        """
        out: dict[int, tuple[str, Token, Token]] = {}
        rows = self._storage.all_position_identities(wallet, chain, self.info.protocol_id)
        for row in rows:
            key = str(row["position_key"])
            parts = key.split(":")
            # CL keys are pool:tickLower:tickUpper:tokenId. Legacy XYK keys are
            # 'legacy:pool:id' and carry no tokenId — the field-count and digit
            # tests drop them structurally.
            if len(parts) != 4 or not parts[3].isdigit():
                continue
            tid = int(parts[3])
            if tid in exclude or tid in out:
                continue
            meta_tid = None
            try:
                meta_tid = (json.loads(row["meta_json"] or "{}") or {}).get("token_id")
            except (TypeError, ValueError):
                meta_tid = None
            if meta_tid is not None and int(meta_tid) != tid:
                # The key and the metadata disagree; one of them is wrong and
                # guessing is how an event gets attributed to the wrong position.
                _log.warning(
                    "MachineX: position_key tokenId %d disagrees with meta token_id %s — skipping",
                    tid,
                    meta_tid,
                )
                continue
            try:
                balances = json.loads(row["current_balances_json"] or "[]")
            except (TypeError, ValueError):
                continue
            if len(balances) < 2:
                continue
            token0 = self._token_from_key_strict(chain, str(balances[0].get("token_key", "")))
            token1 = self._token_from_key_strict(chain, str(balances[1].get("token_key", "")))
            if token0 is None or token1 is None:
                _log.warning(
                    "MachineX: cannot resolve trustworthy token decimals for historical "
                    "tokenId %d (%s) — leaving it out of the scan filter",
                    tid,
                    key,
                )
                continue
            out[tid] = (key, token0, token1)

        if len(out) > self._MAX_HISTORICAL_TOKEN_IDS:
            # The topic list is not chunked and a provider that caps result
            # counts raises, aborting the whole window. Rows are newest-first.
            keep = dict(list(out.items())[: self._MAX_HISTORICAL_TOKEN_IDS])
            _log.warning(
                "MachineX: %d historical tokenIds exceeds the %d cap — scanning the most "
                "recent %d; older positions are not in this run's filter",
                len(out),
                self._MAX_HISTORICAL_TOKEN_IDS,
                self._MAX_HISTORICAL_TOKEN_IDS,
            )
            return keep
        return out

    def _token_from_address(self, chain: Chain, address: str) -> Token:
        """
        Build a Token from address for use in fetch_events().

        Resolution order:
          1. Storage hit with a real symbol → return it
          2. RPC eth_call symbol() + decimals() → upsert + return
          3. Storage hit with a stub → return cached stub
          4. Hard fallback: address-truncated stub
        """
        addr = address.lower()
        with self._storage.connect() as conn:
            row = conn.execute(
                "SELECT symbol, decimals FROM tokens WHERE chain=? AND address=?",
                (chain.value, addr),
            ).fetchone()

        if row:
            sym = row["symbol"]
            if not ("…" in sym and sym.startswith("0x")):
                return Token(chain, addr, sym, row["decimals"])

        if self._rpc is not None:
            symbol, decimals = self._rpc.erc20_metadata(addr)
            if symbol and decimals is not None:
                # Only a full read is stored: a real symbol over a guessed 18
                # would pass every "is this a stub?" check downstream.
                tok = Token(chain, addr, symbol, decimals)
                self._storage.upsert_token(tok)
                return tok

        if row:
            return Token(chain, addr, row["symbol"], row["decimals"])

        short = addr[:6] + "…" + addr[-4:]
        return Token(chain, addr, short, 18)

    def _subgraph_historical_price(self, token_address: str, block: int, ts: int) -> Decimal | None:
        """Historical token price from the subgraph, best-effort.

        Tries tokenDayDatas for the event's UTC day first — served from
        current subgraph state, so it works for any age of event — then falls
        back to a block-level time-travel query, which is more precise but
        fails on indexers that pruned the block. Single attempt each; failures
        cached per (token, day) for this run; successes written to the shared
        price_cache so PriceFetcher serves them from SQLite from then on.
        """
        addr = token_address.lower()
        day = datetime.fromtimestamp(ts, tz=UTC).date()
        fail_key = (addr, day.isoformat())
        if fail_key in self._subgraph_price_failed:
            return None
        price = self._subgraph_day_price(addr, ts)
        source = "subgraph_day"
        if price is None and block:
            price = self._subgraph_price_at_block(addr, block)
            source = "subgraph_block"
        if price is not None:
            self._storage.cache_price(Chain.PEAQ, addr, day, price, source)
            return price
        self._subgraph_price_failed.add(fail_key)
        return None

    def _subgraph_day_price(self, addr: str, ts: int) -> Decimal | None:
        """tokenDayDatas priceUSD for the UTC day containing ts, or None."""
        start_of_day = ts // 86400 * 86400
        query = f"""
        {{
          tokenDayDatas(where: {{ token: "{addr}", startOfDay: {start_of_day} }}) {{
            priceUSD
          }}
        }}
        """
        try:
            data = self._run_subgraph_query(query, self._subgraph_url, max_retries=1)
            rows = (data or {}).get("tokenDayDatas") or []
            price = Decimal(str(rows[0].get("priceUSD") or "0")) if rows else Decimal("0")
            return price if price > 0 else None
        except Exception as e:
            _log.debug("Subgraph day price failed for %s@%d: %s", addr, ts, e)
            return None

    def _subgraph_price_at_block(self, addr: str, block: int) -> Decimal | None:
        """Token priceUSD via block-level time travel, or None (pruned block etc.)."""
        query = f"""
        {{
          token(id: "{addr}", block: {{number: {block}}}) {{
            priceUSD
          }}
        }}
        """
        try:
            data = self._run_subgraph_query(query, self._subgraph_url, max_retries=1)
            token_data = (data or {}).get("token") or {}
            price = Decimal(str(token_data.get("priceUSD") or "0"))
            return price if price > 0 else None
        except Exception as e:
            _log.debug("Subgraph block price failed for %s@%d: %s", addr, block, e)
            return None

    # ── Block cursor ──────────────────────────────────────────────────────

    def _get_scan_cursor(self, wallet: str, chain: Chain) -> int:
        val = self._storage.kv_get(f"block_cursor:{wallet.lower()}:{chain.value}")
        return int(val) if val else DEPLOY_BLOCK

    def _set_scan_cursor(self, wallet: str, chain: Chain, block: int) -> None:
        self._storage.kv_set(f"block_cursor:{wallet.lower()}:{chain.value}", str(block))

    def _advance_cursor(self, wallet: str, chain: Chain) -> None:
        """Advance the cursor by MAX_BLOCKS_PER_RUN without scanning.

        Also records whether that skip landed on the chain head: a wallet with
        no positions still has to walk the cursor forward 150k blocks at a
        time, and it is not caught up until it gets there.
        """
        if self._rpc is None:
            return
        from_block = self._get_scan_cursor(wallet, chain)
        head = self._rpc.get_block_number()
        latest = head if head else from_block
        to_block = min(from_block + MAX_BLOCKS_PER_RUN, latest)
        self._set_scan_cursor(wallet, chain, to_block + 1)
        self._scan_reached_head[self._scan_key(wallet, chain)] = head is not None and (
            to_block >= head
        )

    @staticmethod
    def _scan_key(wallet: str, chain: Chain) -> tuple[str, str]:
        return (wallet.lower(), chain.value)

    def last_scan_reached_head(self, wallet: str, chain: Chain) -> bool:
        """See ProtocolAdapter.last_scan_reached_head. False when no scan has
        run in this process — nothing has proven the cursor is at head."""
        return self._scan_reached_head.get(self._scan_key(wallet, chain), False)


# ── Helpers ───────────────────────────────────────────────────────────────


def _subgraph_price(token_data: dict) -> Decimal:
    """Extract priceUSD from a subgraph token dict, returning Decimal('0') on missing/zero."""
    try:
        return Decimal(str(token_data.get("priceUSD") or "0"))
    except Exception:
        return Decimal("0")


# ── Auto-register ──────────────────────────────────────────────────────────


def _autoregister():
    graph_api_key = os.getenv("GRAPH_API_KEY")
    if not graph_api_key:
        return
    register_adapter(
        MachineXAdapter(
            graph_api_key=graph_api_key,
            rpc_url=endpoint_url(Chain.PEAQ, allow_public=False),
            rpc_fallbacks=fallback_urls(Chain.PEAQ),
            db_path=os.getenv("TRACKER_DB", "tracker.db"),
        )
    )


_autoregister()
