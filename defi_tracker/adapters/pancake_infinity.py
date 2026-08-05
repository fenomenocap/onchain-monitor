"""
PancakeSwap Infinity (V4) adapter for BSC.

Ports the existing pancake_defi.py logic into the ProtocolAdapter interface.
All the hard-won RPC + feeGrowthInside math is preserved; only the SHAPE
of the inputs/outputs changes (now normalized Event/Position types).

Concretely, what was a single 894-line script becomes:
  - fetch_events()    → ModifyLiquidity subgraph query → normalized Events
  - fetch_positions() → reconstruct from events + RPC fee math → Positions
  - get_*_price()     → CoinGecko + stablecoin map + pool-ratio fallback

The CL math (amounts_from_liquidity, feeGrowthInside) is identical and lives
in defi_tracker.adapters._cl_math so the Uni V3 / Uni V4 adapters reuse it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from decimal import Decimal

import requests

from defi_tracker.adapters._base import BaseAdapter
from defi_tracker.adapters._cl_math import (
    amounts_from_liquidity,
    compute_unclaimed_fees_rpc,
    rpc_get_pool_liquidity_v4,
    rpc_get_position,
    rpc_get_slot0_v4,
    sum_transfers_to,
    tick_to_sqrt_price,
)
from defi_tracker.core.adapter import register_adapter
from defi_tracker.core.types import (
    AdapterInfo,
    Chain,
    Event,
    EventKind,
    Position,
    PricePoint,
    ProtocolKind,
    Token,
    TokenAmount,
)

_log = logging.getLogger(__name__)

# ── Constants (extracted from your script, made configurable) ────────────

BSC_STABLECOINS = {
    "0x55d398326f99059ff775485246999027b3197955": ("USDT", Decimal("1.0")),
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": ("USDC", Decimal("1.0")),
    "0xe9e7cea3dedca5984780bafc599bd69add087d56": ("BUSD", Decimal("1.0")),
    "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3": ("DAI", Decimal("1.0")),
}

# Price-only view used by BaseAdapter stablecoin logic
_BSC_STABLECOIN_PRICES: dict[str, Decimal] = {
    addr: price for addr, (_sym, price) in BSC_STABLECOINS.items()
}

GRAPH_GATEWAY = "https://gateway.thegraph.com/api"
SUBGRAPH_ID = "8jFYxwKP8tNGSDisucpHRK1ojUchZd7ELd8zh2ugHGDN"
CL_POOL_MANAGER = "0xa0FfB9c1CE1Fe56963B0321B32E7A0302114058b"
CL_POS_MANAGER = "0x55f4c8aba71a1e923edc303eb4feff14608cc226"


class PancakeInfinityAdapter(BaseAdapter):
    """PancakeSwap Infinity (V4-style CL) on BSC."""

    def __init__(
        self,
        graph_api_key: str,
        rpc_url: str = "https://bsc-dataseed.bnbchain.org",
        db_path: str = "",
    ):
        super().__init__(
            db_path=db_path,
            rpc_url=rpc_url,
            stablecoins=_BSC_STABLECOIN_PRICES,
        )
        self.graph_api_key = graph_api_key
        self._subgraph_url = f"{GRAPH_GATEWAY}/{graph_api_key}/subgraphs/id/{SUBGRAPH_ID}"

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(
            protocol_id="pancake_infinity",
            display_name="PancakeSwap Infinity (V4)",
            chains=[Chain.BSC],
            protocol_kind=ProtocolKind.CL_AMM,
            supports_exact_fees=True,
            supports_historical=True,
        )

    # ── Pricing (batch override for BSC — faster than one-at-a-time) ─────

    def get_current_price(self, token: Token) -> PricePoint | None:
        addr = token.address.lower()
        if addr in self._stablecoins:
            return PricePoint(token.key, self._stablecoins[addr], int(time.time()), "stable")
        if addr in self._current_price_cache:
            return PricePoint(
                token.key, self._current_price_cache[addr], int(time.time()), "cached"
            )
        prices = self._coingecko_prices_bsc([addr])
        if addr in prices:
            return PricePoint(token.key, prices[addr], int(time.time()), "coingecko")
        return None

    def _coingecko_prices_bsc(self, addresses: list[str]) -> dict[str, Decimal]:
        addrs = [a for a in addresses if a not in self._stablecoins]
        if not addrs:
            return {}
        headers = {"x-cg-demo-api-key": self._cg_key} if self._cg_key else {}
        out: dict[str, Decimal] = {}
        for i in range(0, len(addrs), 30):
            chunk = addrs[i : i + 30]
            url = (
                "https://api.coingecko.com/api/v3/simple/token_price/"
                "binance-smart-chain"
                f"?contract_addresses={','.join(chunk)}&vs_currencies=usd"
            )
            try:
                r = requests.get(url, timeout=15, headers=headers)
                r.raise_for_status()
                for addr, v in r.json().items():
                    if "usd" in v:
                        price = Decimal(str(v["usd"]))
                        out[addr.lower()] = price
                        self._current_price_cache[addr.lower()] = price
                time.sleep(0.3)
            except Exception as e:
                _log.warning("CoinGecko price fetch failed: %s", e)
        return out

    # ── Events ────────────────────────────────────────────────────────────

    def fetch_events(self, wallet: str, chain: Chain, since_ts: int = 0) -> list[Event]:
        """
        Pull ModifyLiquidity events since `since_ts` from the V4 subgraph,
        convert to normalized Events with USD priced at event time.
        """
        if chain != Chain.BSC:
            return []

        raw = self._fetch_modify_liquidities(wallet, since_ts)
        events: list[Event] = []

        for ev in raw:
            pool = ev["pool"]
            pool_id = pool["id"]
            t0 = pool["token0"]
            t1 = pool["token1"]
            decimals0 = int(t0.get("decimals", 18) or 18)
            decimals1 = int(t1.get("decimals", 18) or 18)

            token0 = Token(chain, t0["id"].lower(), t0["symbol"], decimals0)
            token1 = Token(chain, t1["id"].lower(), t1["symbol"], decimals1)
            self._storage.upsert_token(token0)
            self._storage.upsert_token(token1)

            amt0 = Decimal(str(ev.get("amount0", "0") or "0"))
            amt1 = Decimal(str(ev.get("amount1", "0") or "0"))
            liquidity_delta = int(ev["amount"])
            tx_hash = ev.get("transaction", {}).get("id", "")

            if liquidity_delta == 0:
                # Pure fee harvest — the subgraph reports amount0/amount1 as 0
                # for these (it only tracks principal deltas), so pull the
                # actual payout from the ERC20 Transfer logs in the tx receipt.
                kind = EventKind.COLLECT
                amt0, amt1 = self._collected_fee_amounts(
                    tx_hash, wallet, t0["id"], t1["id"], decimals0, decimals1
                )
            else:
                kind = EventKind.DEPOSIT if liquidity_delta > 0 else EventKind.WITHDRAW

            # Sign convention: storage expects amounts signed +deposit, -withdraw.
            # The subgraph already gives amounts in this convention for V4
            # (negative for withdraw); keep as-is. Collected fees are positive.
            ta0 = TokenAmount(token0, amt0)
            ta1 = TokenAmount(token1, amt1)

            ts = int(ev["timestamp"])
            # Price each token at the event's timestamp via PriceFetcher
            pp0 = self.get_historical_price(token0, ts)
            pp1 = self.get_historical_price(token1, ts)
            p0: Decimal | None = pp0.price_usd if pp0 else None
            p1: Decimal | None = pp1.price_usd if pp1 else None

            # Amount-ratio fallback: if one price is known and amounts are non-zero,
            # derive the missing price from the ratio of deposited amounts.
            abs_amt0, abs_amt1 = abs(amt0), abs(amt1)
            if p0 is None and p1 is not None and abs_amt1 > 0 and abs_amt0 > 0:
                p0 = abs_amt1 * p1 / abs_amt0
                _log.warning(
                    "Used amount-ratio fallback for %s price at ts=%d (tx %s)",
                    token0.symbol,
                    ts,
                    ev.get("transaction", {}).get("id", "?"),
                )
            elif p1 is None and p0 is not None and abs_amt0 > 0 and abs_amt1 > 0:
                p1 = abs_amt0 * p0 / abs_amt1
                _log.warning(
                    "Used amount-ratio fallback for %s price at ts=%d (tx %s)",
                    token1.symbol,
                    ts,
                    ev.get("transaction", {}).get("id", "?"),
                )

            if p0 is None or p1 is None:
                _log.warning(
                    "Skipping event %s: no price for %s/%s at ts=%d — "
                    "will retry on next sync",
                    ev.get("transaction", {}).get("id", "?"),
                    token0.symbol,
                    token1.symbol,
                    ts,
                )
                continue

            usd_at_ts = amt0 * p0 + amt1 * p1

            position_key = f"{pool_id}:{ev['tickLower']}:{ev['tickUpper']}"

            log_index = int(ev.get("logIndex", 0) or 0)

            events.append(
                Event(
                    wallet=wallet.lower(),
                    chain=chain,
                    protocol_id=self.info.protocol_id,
                    position_key=position_key,
                    tx_hash=tx_hash,
                    log_index=log_index,
                    ts=ts,
                    block_number=int(ev.get("blockNumber", 0) or 0),
                    kind=kind,
                    amounts=[ta0, ta1],
                    prices_at_ts={token0.key: p0, token1.key: p1},
                    usd_at_ts=usd_at_ts,
                    meta={
                        "pool_id": pool_id,
                        "tick_lower": int(ev["tickLower"]),
                        "tick_upper": int(ev["tickUpper"]),
                        "liquidity_delta": liquidity_delta,
                    },
                )
            )

            # A withdraw tx pays out principal + accrued fees, but the
            # subgraph amounts above are principal only — recover the fee
            # portion from the tx receipt and record it as its own COLLECT
            # event (synthetic log_index keeps event_uid unique).
            if kind == EventKind.WITHDRAW and self._rpc is not None and tx_hash:
                fee_ev = self._withdraw_fee_event(
                    events[-1], wallet, token0, token1, amt0, amt1, p0, p1
                )
                if fee_ev is not None:
                    events.append(fee_ev)
        return events

    _SYNTHETIC_COLLECT_LOG_OFFSET = 500_000

    def _withdraw_fee_event(
        self,
        withdraw: Event,
        wallet: str,
        token0: Token,
        token1: Token,
        amt0: Decimal,
        amt1: Decimal,
        p0: Decimal,
        p1: Decimal,
    ) -> Event | None:
        """Derive the fee portion of a withdraw tx: receipt payout − |principal|.

        Returns a COLLECT Event for the surplus, or None when the receipt is
        unavailable or the payout is principal-only.
        """
        paid0, paid1 = self._collected_fee_amounts(
            withdraw.tx_hash,
            wallet,
            token0.address,
            token1.address,
            token0.decimals,
            token1.decimals,
        )
        fee0 = max(paid0 - abs(amt0), Decimal("0"))
        fee1 = max(paid1 - abs(amt1), Decimal("0"))
        if fee0 == 0 and fee1 == 0:
            return None
        return Event(
            wallet=wallet.lower(),
            chain=withdraw.chain,
            protocol_id=self.info.protocol_id,
            position_key=withdraw.position_key,
            tx_hash=withdraw.tx_hash,
            log_index=withdraw.log_index + self._SYNTHETIC_COLLECT_LOG_OFFSET,
            ts=withdraw.ts,
            block_number=withdraw.block_number,
            kind=EventKind.COLLECT,
            amounts=[TokenAmount(token0, fee0), TokenAmount(token1, fee1)],
            prices_at_ts={token0.key: p0, token1.key: p1},
            usd_at_ts=fee0 * p0 + fee1 * p1,
            meta={**withdraw.meta, "derived_from_withdraw": True},
        )

    def _collected_fee_amounts(
        self,
        tx_hash: str,
        wallet: str,
        token0_addr: str,
        token1_addr: str,
        decimals0: int,
        decimals1: int,
    ) -> tuple[Decimal, Decimal]:
        """
        Read the actual token payout of a pure fee-harvest tx (liquidityDelta
        == 0) from its ERC20 Transfer logs. Falls back to (0, 0) if no RPC is
        configured or the receipt can't be fetched — same as historical
        behavior, just now scoped to the case we can't do better.
        """
        if self._rpc is None or not tx_hash:
            _log.warning(
                "No BSC RPC configured — can't read collected-fee amounts for tx %s", tx_hash
            )
            return Decimal("0"), Decimal("0")
        receipt = self._rpc.get_transaction_receipt(tx_hash)
        if not receipt:
            _log.warning("Could not fetch receipt for collect tx %s — fees recorded as 0", tx_hash)
            return Decimal("0"), Decimal("0")
        raw0 = sum_transfers_to(receipt, token0_addr, wallet)
        raw1 = sum_transfers_to(receipt, token1_addr, wallet)
        return Decimal(raw0) / Decimal(10**decimals0), Decimal(raw1) / Decimal(10**decimals1)

    def _fetch_modify_liquidities(self, wallet: str, since_ts: int) -> list[dict]:
        all_events, skip = [], 0
        seven_days_ago = int(time.time()) - 7 * 86400
        wallet_lower = wallet.lower()

        while True:
            q = f"""
            {{
              modifyLiquidities(
                where: {{ origin: "{wallet_lower}", timestamp_gte: {since_ts} }}
                first: 1000
                skip: {skip}
                orderBy: timestamp
                orderDirection: asc
              ) {{
                id timestamp amount amount0 amount1
                tickLower tickUpper logIndex
                pool {{
                  id sqrtPrice tick feeTier liquidity
                  token0Price token1Price totalValueLockedUSD feesUSD
                  createdAtTimestamp
                  token0 {{ id symbol decimals }}
                  token1 {{ id symbol decimals }}
                  poolDayData(
                    where: {{ date_gte: {seven_days_ago} }}
                    orderBy: date orderDirection: desc first: 7
                  ) {{ date feesUSD tvlUSD liquidity }}
                }}
                transaction {{ id timestamp }}
              }}
            }}
            """
            data = self._run_subgraph_query(q, self._subgraph_url)
            if data is None:
                # Outage must be loud: a silent [] here is indistinguishable
                # from "wallet has no positions" and would tombstone the
                # whole portfolio in the snapshot pass.
                raise RuntimeError("Pancake subgraph returned no data (outage or bad indexers)")
            batch = data.get("modifyLiquidities", [])
            all_events.extend(batch)
            if len(batch) < 1000:
                break
            skip += 1000
        return all_events

    # ── Positions ─────────────────────────────────────────────────────────

    def fetch_positions(self, wallet: str, chain: Chain) -> list[Position]:
        """
        Reconstruct active positions from event history + current chain state.
        Matches your existing reconstruct_positions() logic but emits Position
        objects instead of dicts.
        """
        if chain != Chain.BSC:
            return []

        # Pull all events (we need the full history to reconstruct net liquidity)
        try:
            raw_events = self._fetch_modify_liquidities(wallet, since_ts=0)
        except RuntimeError as e:
            # Subgraph outage (e.g. the deterministic indexing error that froze
            # BSC marks from 2026-07-07): rebuild positions from stored state +
            # live chain reads instead of leaving snapshots stale.
            _log.warning(
                "Pancake subgraph unavailable (%s) — reconstructing positions via RPC", e
            )
            return self._fetch_positions_rpc(wallet, chain)
        if not raw_events:
            return []

        # Group by (pool_id, tickLower, tickUpper)
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for ev in raw_events:
            key = (ev["pool"]["id"], int(ev["tickLower"]), int(ev["tickUpper"]))
            groups[key].append(ev)

        # Get position NFT tokenIds (needed for RPC fee lookup)
        token_ids = self._fetch_position_token_ids(wallet)
        sorted_tids = sorted([int(p["tokenId"]) for p in token_ids], reverse=True)

        # Pre-fetch current prices for all tokens in one batch call
        all_addrs = set()
        for evs in groups.values():
            all_addrs.add(evs[0]["pool"]["token0"]["id"].lower())
            all_addrs.add(evs[0]["pool"]["token1"]["id"].lower())
        self._coingecko_prices_bsc(list(all_addrs))

        positions: list[Position] = []

        for (pool_id, tick_lower, tick_upper), evs in groups.items():
            pool = evs[-1]["pool"]
            t0_raw = pool["token0"]
            t1_raw = pool["token1"]

            decimals0 = int(t0_raw.get("decimals", 18) or 18)
            decimals1 = int(t1_raw.get("decimals", 18) or 18)
            token0 = Token(chain, t0_raw["id"].lower(), t0_raw["symbol"], decimals0)
            token1 = Token(chain, t1_raw["id"].lower(), t1_raw["symbol"], decimals1)
            self._storage.upsert_token(token0)
            self._storage.upsert_token(token1)

            # Net liquidity from event sum
            net_liquidity = sum(int(e["amount"]) for e in evs)
            if net_liquidity <= 0:
                continue

            tick_current = int(pool.get("tick", 0) or 0)
            sqrt_price = int(pool.get("sqrtPrice", 0) or 0)

            # Current token amounts via CL math
            sqrt_lower = tick_to_sqrt_price(tick_lower)
            sqrt_upper = tick_to_sqrt_price(tick_upper)
            raw0, raw1 = amounts_from_liquidity(sqrt_price, sqrt_lower, sqrt_upper, net_liquidity)
            current0 = Decimal(raw0) / Decimal(10**decimals0)
            current1 = Decimal(raw1) / Decimal(10**decimals1)

            # Current prices
            pp0 = self.get_current_price(token0)
            pp1 = self.get_current_price(token1)
            p0 = pp0.price_usd if pp0 else Decimal("0")
            p1 = pp1.price_usd if pp1 else Decimal("0")
            lp_value_usd = current0 * p0 + current1 * p1

            # Match to tokenId for RPC fees
            matched_tid = None
            unclaimed0 = unclaimed1 = Decimal("0")
            unclaimed_usd = Decimal("0")
            try:
                if self._rpc is None:
                    raise RuntimeError("No BSC RPC configured — set BSC_RPC_URL")
                for tid in sorted_tids:
                    pos_result = rpc_get_position(
                        self._rpc,
                        CL_POOL_MANAGER,
                        CL_POS_MANAGER,
                        pool_id,
                        tick_lower,
                        tick_upper,
                        tid,
                    )
                    if pos_result and pos_result[0] > 0:
                        matched_tid = tid
                        u0, u1, u_usd = compute_unclaimed_fees_rpc(
                            rpc=self._rpc,
                            pool_manager=CL_POOL_MANAGER,
                            pos_manager=CL_POS_MANAGER,
                            pool_id=pool_id,
                            tick_lower=tick_lower,
                            tick_upper=tick_upper,
                            tick_current=tick_current,
                            token_id=tid,
                            net_liquidity=net_liquidity,
                            decimals0=decimals0,
                            decimals1=decimals1,
                            price0=p0,
                            price1=p1,
                        )
                        unclaimed0, unclaimed1, unclaimed_usd = u0, u1, u_usd
                        break
            except Exception as e:
                _log.warning("BSC RPC fee computation failed for pool %s: %s — unclaimed set to 0", pool_id, e)

            # Pool context for APR
            pool_day = pool.get("poolDayData", [])
            seven_day_fees = Decimal(str(sum(float(d.get("feesUSD", 0) or 0) for d in pool_day)))

            pool_liquidity = int(pool.get("liquidity", 1) or 1)
            share_pct = (
                Decimal(net_liquidity) / Decimal(pool_liquidity) * 100
                if pool_liquidity > 0
                else Decimal("0")
            )

            position_key = f"{pool_id}:{tick_lower}:{tick_upper}"
            in_range = tick_lower <= tick_current <= tick_upper

            positions.append(
                Position(
                    wallet=wallet.lower(),
                    chain=chain,
                    protocol_id=self.info.protocol_id,
                    position_key=position_key,
                    protocol_kind=ProtocolKind.CL_AMM,
                    pair_label=f"{token0.symbol}/{token1.symbol}",
                    tokens=[token0, token1],
                    current_value_usd=lp_value_usd,
                    current_balances=[TokenAmount(token0, current0), TokenAmount(token1, current1)],
                    unclaimed_usd=unclaimed_usd,
                    unclaimed_balances=[
                        TokenAmount(token0, unclaimed0),
                        TokenAmount(token1, unclaimed1),
                    ],
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                    tick_current=tick_current,
                    in_range=in_range,
                    liquidity=net_liquidity,
                    pool_address=pool_id,
                    fee_tier_bps=int(pool.get("feeTier", 0) or 0),
                    pool_tvl_usd=Decimal(str(pool.get("totalValueLockedUSD", 0) or 0)),
                    seven_day_fees_usd=seven_day_fees,
                    position_share_pct=share_pct,
                    opened_at=int(evs[0]["timestamp"]),
                    last_event_at=int(evs[-1]["timestamp"]),
                    snapshot_at=int(time.time()),
                    meta={"token_id": matched_tid, "event_count": len(evs)},
                )
            )

        return positions

    # ── RPC-only fallback (subgraph outage) ───────────────────────────────

    def _fetch_positions_rpc(self, wallet: str, chain: Chain) -> list[Position]:
        """
        Rebuild open positions without the subgraph: identity + tokenId come
        from the last successful snapshots, while liquidity, pool price, and
        unclaimed fees are read fresh from the chain. A position closed during
        the outage reads liquidity=0 on-chain and is omitted, so the runner's
        tombstoning stays correct.

        Raises RuntimeError whenever a position can't be verified on-chain —
        an unverifiable position must keep the pass stale (loud, like the
        subgraph path) rather than risk a wrong tombstone or fabricated value.

        Limits vs the subgraph path: positions opened during the outage are
        invisible (no events, no snapshot to seed from), and pool TVL / 7-day
        fee context is carried forward from the last snapshot.
        """
        if self._rpc is None:
            raise RuntimeError(
                "Pancake subgraph down and no BSC RPC configured — cannot snapshot"
            )

        pid = self.info.protocol_id
        identities = self._storage.open_position_identities(wallet, chain, pid)
        if not identities:
            return []

        slot0_cache: dict[str, tuple[int, int]] = {}
        pool_liquidity_cache: dict[str, int | None] = {}
        positions: list[Position] = []

        for ident in identities:
            position_key = ident["position_key"]
            row = self._storage.latest_snapshot_row(wallet, chain, pid, position_key)
            if row is None:
                raise RuntimeError(f"No snapshot row for open position {position_key}")
            snap_meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
            token_id = snap_meta.get("token_id")
            if not token_id:
                raise RuntimeError(
                    f"No stored tokenId for {position_key} — cannot verify on-chain"
                )

            pool_id, tick_lower_s, tick_upper_s = position_key.rsplit(":", 2)
            tick_lower, tick_upper = int(tick_lower_s), int(tick_upper_s)

            pos_state = rpc_get_position(
                self._rpc, CL_POOL_MANAGER, CL_POS_MANAGER,
                pool_id, tick_lower, tick_upper, int(token_id),
            )
            if pos_state is None:
                raise RuntimeError(
                    f"RPC getPosition failed for {position_key} (tokenId {token_id})"
                )
            net_liquidity = pos_state[0]
            if net_liquidity <= 0:
                _log.info(
                    "RPC fallback: %s (tokenId %s) has zero on-chain liquidity — closed",
                    position_key, token_id,
                )
                continue

            if pool_id not in slot0_cache:
                slot0 = rpc_get_slot0_v4(self._rpc, CL_POOL_MANAGER, pool_id)
                if slot0 is None:
                    raise RuntimeError(f"RPC getSlot0 failed for pool {pool_id}")
                slot0_cache[pool_id] = slot0
                pool_liquidity_cache[pool_id] = rpc_get_pool_liquidity_v4(
                    self._rpc, CL_POOL_MANAGER, pool_id
                )
            sqrt_price, tick_current = slot0_cache[pool_id]

            token0, token1 = self._tokens_from_snapshot_row(chain, row)

            raw0, raw1 = amounts_from_liquidity(
                sqrt_price,
                tick_to_sqrt_price(tick_lower),
                tick_to_sqrt_price(tick_upper),
                net_liquidity,
            )
            current0 = Decimal(raw0) / Decimal(10**token0.decimals)
            current1 = Decimal(raw1) / Decimal(10**token1.decimals)

            p0, p1 = self._prices_with_pool_ratio(token0, token1, sqrt_price)
            lp_value_usd = current0 * p0 + current1 * p1

            unclaimed0, unclaimed1, unclaimed_usd = compute_unclaimed_fees_rpc(
                rpc=self._rpc,
                pool_manager=CL_POOL_MANAGER,
                pos_manager=CL_POS_MANAGER,
                pool_id=pool_id,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                tick_current=tick_current,
                token_id=int(token_id),
                net_liquidity=net_liquidity,
                decimals0=token0.decimals,
                decimals1=token1.decimals,
                price0=p0,
                price1=p1,
            )

            pool_liquidity = pool_liquidity_cache.get(pool_id)
            share_pct = (
                Decimal(net_liquidity) / Decimal(pool_liquidity) * 100
                if pool_liquidity
                else None
            )
            first_ts, last_ts = self._storage.event_ts_bounds(wallet, chain, pid, position_key)

            positions.append(
                Position(
                    wallet=wallet.lower(),
                    chain=chain,
                    protocol_id=pid,
                    position_key=position_key,
                    protocol_kind=ProtocolKind.CL_AMM,
                    pair_label=row["pair_label"],
                    tokens=[token0, token1],
                    current_value_usd=lp_value_usd,
                    current_balances=[
                        TokenAmount(token0, current0),
                        TokenAmount(token1, current1),
                    ],
                    unclaimed_usd=unclaimed_usd,
                    unclaimed_balances=[
                        TokenAmount(token0, unclaimed0),
                        TokenAmount(token1, unclaimed1),
                    ],
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                    tick_current=tick_current,
                    in_range=tick_lower <= tick_current <= tick_upper,
                    liquidity=net_liquidity,
                    pool_address=pool_id,
                    fee_tier_bps=row["fee_tier_bps"],
                    # Pool-wide context isn't readable via RPC — carry the last
                    # subgraph-sourced values forward rather than zeroing them.
                    pool_tvl_usd=(
                        Decimal(str(row["pool_tvl_usd"]))
                        if row["pool_tvl_usd"] is not None
                        else None
                    ),
                    seven_day_fees_usd=(
                        Decimal(str(row["seven_day_fees_usd"]))
                        if row["seven_day_fees_usd"] is not None
                        else None
                    ),
                    position_share_pct=share_pct,
                    opened_at=first_ts,
                    last_event_at=last_ts,
                    snapshot_at=int(time.time()),
                    meta={"token_id": int(token_id), "source": "rpc_fallback"},
                )
            )

        return positions

    def _tokens_from_snapshot_row(self, chain: Chain, row: dict) -> tuple[Token, Token]:
        """Rebuild ordered (token0, token1) from a snapshot's balances JSON +
        the tokens table (decimals). Raises RuntimeError on unknown tokens —
        wrong decimals would silently corrupt every downstream amount."""
        balances = json.loads(row["current_balances_json"])
        if len(balances) != 2:
            raise RuntimeError(
                f"Expected 2 balance entries for {row['position_key']}, got {len(balances)}"
            )
        tokens: list[Token] = []
        with self._storage.connect() as conn:
            for entry in balances:
                address = entry["token_key"].split(":", 1)[1]
                t_row = conn.execute(
                    "SELECT symbol, decimals FROM tokens WHERE chain=? AND address=?",
                    (chain.value, address),
                ).fetchone()
                if t_row is None:
                    raise RuntimeError(f"Token {entry['token_key']} not in tokens table")
                tokens.append(Token(chain, address, t_row["symbol"], int(t_row["decimals"])))
        return tokens[0], tokens[1]

    def _prices_with_pool_ratio(
        self, token0: Token, token1: Token, sqrt_price: int
    ) -> tuple[Decimal, Decimal]:
        """Current USD prices with a pool-ratio fallback: when only one side
        has a market price, derive the other from the pool's own exchange rate.
        Raises RuntimeError if neither side is priceable — a $0-valued snapshot
        would poison day-over-day and PnL math."""
        pp0 = self.get_current_price(token0)
        pp1 = self.get_current_price(token1)
        p0 = pp0.price_usd if pp0 else None
        p1 = pp1.price_usd if pp1 else None
        if p0 is None and p1 is not None:
            p0 = self.price_from_sqrt(sqrt_price, token0.decimals, token1.decimals, p1)
        elif p1 is None and p0 is not None:
            # price_from_sqrt with price1=1 returns the pool's token1-per-token0
            # ratio itself; inverting it prices token1 from token0.
            ratio = self.price_from_sqrt(
                sqrt_price, token0.decimals, token1.decimals, Decimal("1")
            )
            p1 = p0 / ratio if ratio else None
        if p0 is None or p1 is None:
            raise RuntimeError(
                f"No USD price for {token0.symbol}/{token1.symbol} — cannot value position"
            )
        return p0, p1

    def _fetch_position_token_ids(self, wallet: str) -> list[dict]:
        all_positions, skip = [], 0
        while True:
            q = f"""
            {{
              positions(
                where: {{ owner: "{wallet.lower()}" }}
                first: 1000
                skip: {skip}
                orderBy: tokenId
                orderDirection: asc
              ) {{ id tokenId owner createdAtTimestamp }}
            }}
            """
            data = self._run_subgraph_query(q, self._subgraph_url)
            if not data:
                break
            batch = data.get("positions", [])
            all_positions.extend(batch)
            if len(batch) < 1000:
                break
            skip += 1000
        return all_positions


# ── Auto-register if env vars are present ──────────────────────────────────


def _autoregister():
    key = os.getenv("GRAPH_API_KEY")
    if key:
        register_adapter(
            PancakeInfinityAdapter(
                graph_api_key=key,
                rpc_url=os.getenv("BSC_RPC_URL", "https://bsc-dataseed.bnbchain.org"),
                db_path=os.getenv("TRACKER_DB", "tracker.db"),
            )
        )


_autoregister()
