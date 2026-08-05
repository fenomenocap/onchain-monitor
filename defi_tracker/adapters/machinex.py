"""
MachineX adapter — peaq chain DEX (Ramses/Uni V3 fork).

MachineX is deployed on the peaq blockchain (chain ID 3338).

Position tracking:
  - fetch_positions(): queries the MachineX subgraph for CL and legacy (XYK)
    positions. Unclaimed fees are computed from subgraph feeGrowth fields —
    no extra RPC calls needed. Token prices come from subgraph priceUSD.
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

import logging
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

from defi_tracker.adapters._base import BaseAdapter
from defi_tracker.adapters._cl_math import (
    TOPIC_V3_COLLECT,
    TOPIC_V3_DECREASE_LIQUIDITY,
    TOPIC_V3_INCREASE_LIQUIDITY,
    amounts_from_liquidity,
    compute_unclaimed_fees_pure,
    compute_unclaimed_fees_v3,
    rpc_enumerate_positions_v3,
    rpc_get_pool_address_v3,
    rpc_get_position_v3,
    tick_to_sqrt_price,
)
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

# ── Constants — peaq mainnet ──────────────────────────────────────────────

NFT_MANAGER = "0x4DbC7DD463Bb4a8C6C4381cCEeB58b9327C7Cfa4"
V3_FACTORY = "0x2646DCbE025D21A2925fdaCeB639e998E17d6060"
RPC_DEFAULT = "https://peaq.api.onfinality.io/public"  # chain ID 3338
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

def _build_query(wallet: str) -> str:
    w = wallet.lower()
    return f"""
    {{
      clPositions(where: {{ owner: "{w}" }} first: 100) {{
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
      legacyPositions(where: {{ owner: "{w}" }} first: 100) {{
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
    ):
        super().__init__(
            db_path=db_path,
            rpc_url=rpc_url or os.getenv("PEAQ_RPC_URL", ""),
            stablecoins=_PEAQ_STABLECOINS,
            cg_coin_ids=_PEAQ_CG_COIN_IDS,
        )
        self._subgraph_url = f"{GRAPH_GATEWAY}/{graph_api_key}/subgraphs/id/{MACHINEX_SUBGRAPH_ID}"
        # (address, 'YYYY-MM-DD') pairs the subgraph couldn't price this run —
        # indexers that pruned a block won't un-prune it, so don't re-ask.
        self._subgraph_price_failed: set[tuple[str, str]] = set()

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
            # Outage must be loud: a silent [] is indistinguishable from
            # "wallet closed everything" and would tombstone the portfolio.
            raise RuntimeError(f"MachineX subgraph returned no data for wallet {wallet}")

        positions: list[Position] = []
        for pos in data.get("clPositions") or []:
            p = self._build_cl_position(wallet, chain, pos)
            if p is not None:
                positions.append(p)
        for pos in data.get("legacyPositions") or []:
            p = self._build_legacy_position(wallet, chain, pos)
            if p is not None:
                positions.append(p)
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
            # garbage right after a boundary crossing (observed: a $34/month
            # position reporting $15k unclaimed the day it re-entered range).
            u0 = u1 = u_usd = Decimal("0")
            if self._rpc is not None and token_id:
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

            return Position(
                wallet=wallet.lower(),
                chain=chain,
                protocol_id=self.info.protocol_id,
                position_key=f"{pool_addr}:{tick_lower}:{tick_upper}:{token_id}",
                protocol_kind=ProtocolKind.CL_AMM,
                pair_label=f"{token0.symbol}/{token1.symbol} {fee_tier/10000:.2f}%",
                tokens=[token0, token1],
                current_value_usd=lp_value_usd,
                current_balances=[TokenAmount(token0, current0), TokenAmount(token1, current1)],
                unclaimed_usd=u_usd,
                unclaimed_balances=[TokenAmount(token0, u0), TokenAmount(token1, u1)],
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                tick_current=tick_current,
                in_range=in_range,
                liquidity=liquidity,
                pool_address=pool_addr,
                fee_tier_bps=fee_tier // 100,
                snapshot_at=int(time.time()),
                meta={"token_id": token_id},
            )
        except Exception as e:
            _log.warning("Failed to build CL position from subgraph data: %s", e)
            return None

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
                meta={"pool_share": float(share)},
            )
        except Exception as e:
            _log.warning("Failed to build legacy position from subgraph data: %s", e)
            return None

    # ── Events ────────────────────────────────────────────────────────────

    def iter_event_batches(
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
        """
        if chain != Chain.PEAQ:
            return
        if self._rpc is None:
            _log.debug("No PEAQ_RPC_URL — skipping MachineX event scan")
            return

        token_ids = rpc_enumerate_positions_v3(self._rpc, NFT_MANAGER, wallet)
        if not token_ids:
            self._advance_cursor(wallet, chain)
            return

        tid_meta: dict[int, tuple[str, Token, Token]] = {}
        for token_id in token_ids:
            raw = rpc_get_position_v3(self._rpc, NFT_MANAGER, token_id, nft_header_words=0)
            if not raw:
                continue
            token0_addr, token1_addr, fee, tick_lower, tick_upper, _, _, _ = raw
            token0 = self._token_from_address(chain, token0_addr)
            token1 = self._token_from_address(chain, token1_addr)
            self._storage.upsert_token(token0)
            self._storage.upsert_token(token1)
            pool = rpc_get_pool_address_v3(
                self._rpc, V3_FACTORY, token0_addr, token1_addr, fee, int24_third_arg=True
            )
            position_key = f"{pool or 'unknown'}:{tick_lower}:{tick_upper}:{token_id}"
            tid_meta[token_id] = (position_key, token0, token1)

        if not tid_meta:
            self._advance_cursor(wallet, chain)
            return

        tid_topics = ["0x" + tid.to_bytes(32, "big").hex() for tid in tid_meta]

        from_block = self._get_scan_cursor(wallet, chain)
        latest = self._rpc.get_block_number() or from_block
        to_block = min(from_block + MAX_BLOCKS_PER_RUN, latest)

        for win_start in range(from_block, to_block + 1, WINDOW_BLOCKS):
            win_end = min(win_start + WINDOW_BLOCKS - 1, to_block)
            events = self._scan_window(
                wallet, chain, tid_meta, tid_topics, win_start, win_end
            )
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
                    win_end + 1, wallet[:10],
                )
                return

    def fetch_events(
        self, wallet: str, chain: Chain, since_ts: int = 0
    ) -> list[Event]:
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
                continue
            kind = topic_kind.get(str(topics[0]).lower())
            if kind is None:
                continue
            raw_tid = int(topics[1], 16)
            if raw_tid not in tid_meta:
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
            ev = self._log_to_event(
                log, wallet, chain, position_key, token0, token1, kind
            )
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
                log, wallet, chain, position_key, token0, token1, EventKind.COLLECT,
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
                _log.warning(
                    "Skipping event %s: could not resolve block timestamp for block %d",
                    tx_hash, block_number,
                )
                return None

            pp0 = self.get_historical_price(token0, ts)
            pp1 = self.get_historical_price(token1, ts)
            p0: Decimal | None = pp0.price_usd if pp0 else None
            p1: Decimal | None = pp1.price_usd if pp1 else None

            def _ratio_fill(p0: Decimal | None, p1: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
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
                _log.warning(
                    "Skipping event %s: no price for %s/%s at ts=%d",
                    tx_hash, token0.symbol, token1.symbol, ts,
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
            _log.warning("Failed to decode log %s: %s", log.get("transactionHash"), e)
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
            if symbol:
                tok = Token(chain, addr, symbol, decimals if decimals is not None else 18)
                self._storage.upsert_token(tok)
                return tok

        if row:
            return Token(chain, addr, row["symbol"], row["decimals"])

        short = addr[:6] + "…" + addr[-4:]
        return Token(chain, addr, short, 18)

    def _subgraph_historical_price(
        self, token_address: str, block: int, ts: int
    ) -> Decimal | None:
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
        """Advance the cursor by MAX_BLOCKS_PER_RUN without scanning."""
        if self._rpc is None:
            return
        from_block = self._get_scan_cursor(wallet, chain)
        latest = self._rpc.get_block_number() or from_block
        to_block = min(from_block + MAX_BLOCKS_PER_RUN, latest)
        self._set_scan_cursor(wallet, chain, to_block + 1)


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
            rpc_url=os.getenv("PEAQ_RPC_URL", ""),
            db_path=os.getenv("TRACKER_DB", "tracker.db"),
        )
    )


_autoregister()
