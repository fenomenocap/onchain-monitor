"""
PancakeSwap Infinity (V4) adapter for BSC.

  - events: _iter_event_batches() scans BSC by block cursor — txs found via
    Alchemy's asset-transfers API (or eth_getLogs), each receipt decoded by
    _pancake_chain. The ModifyLiquidity subgraph query (fetch_events) runs
    only when no RPC is configured; the subgraph has been frozen since
    2026-07-07.
  - fetch_positions() → subgraph when it answers, else rebuilt from stored
    events + RPC reads (_fetch_positions_rpc), with RPC fee math
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
from collections.abc import Iterator
from decimal import Decimal

import requests

from defi_tracker.adapters._base import BaseAdapter, end_of_day_price_ts
from defi_tracker.adapters._cl_math import (
    SEL_V4_GET_SLOT0,
    amounts_from_liquidity,
    compute_unclaimed_fees_rpc,
    rpc_get_fee_growth_globals,
    rpc_get_pool_liquidity_v4,
    rpc_get_position,
    rpc_get_slot0_v4,
    sum_transfers_to,
    tick_to_sqrt_price,
)
from defi_tracker.adapters._pancake_chain import (
    PM_MODIFY_TOPIC,
    TRANSFER_TOPIC,
    address_topic,
    decode_receipt,
    minted_token_ids,
    uint_topic,
)
from defi_tracker.core.adapter import register_adapter
from defi_tracker.core.endpoints import PUBLIC_RPC, fallback_urls
from defi_tracker.core.endpoints import rpc_url as endpoint_url
from defi_tracker.core.types import (
    AdapterInfo,
    Chain,
    Event,
    EventKind,
    PoolInfo,
    PoolState,
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
        rpc_fallbacks: tuple[str, ...] = (),
    ):
        super().__init__(
            db_path=db_path,
            rpc_url=rpc_url,
            stablecoins=_BSC_STABLECOIN_PRICES,
            rpc_fallbacks=rpc_fallbacks,
        )
        self.graph_api_key = graph_api_key
        self._subgraph_url = f"{GRAPH_GATEWAY}/{graph_api_key}/subgraphs/id/{SUBGRAPH_ID}"
        # Written by _iter_event_batches, read by last_scan_reached_head().
        self._scan_reached_head: dict[tuple[str, str], bool] = {}

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
        if self._as_of_ts is not None:
            return self.get_historical_price(token, end_of_day_price_ts(self._as_of_ts))
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
                # NB: not "will retry on next sync" — the watermark advances to
                # the newest event KEPT in this batch, so a drop older than a
                # stored sibling is never requested again.
                self._note_dropped_event(
                    wallet,
                    chain,
                    f"no price for {token0.symbol}/{token1.symbol} at ts={ts}",
                    str(ev.get("transaction", {}).get("id", "?")),
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

    # ── Events from the chain (the subgraph has been dead since 2026-07-06) ──

    CHAIN_WINDOW_BLOCKS = 10_000  # Alchemy PAYG's eth_getLogs cap on BNB
    # ~4 days of BSC at 0.75s blocks; the time budget usually binds first.
    CHAIN_MAX_BLOCKS_PER_RUN = 3_000_000
    # Below this, a provider's range cap turns the scan into a crawl.
    CHAIN_MIN_CHUNK = 500

    def _iter_event_batches(
        self,
        wallet: str,
        chain: Chain,
        since_ts: int = 0,
        deadline: float | None = None,
    ) -> Iterator[list[Event]]:
        """Scan BSC for this wallet's position changes, one batch per window.

        Tx discovery prefers Alchemy's asset-transfers API (every tx the wallet
        sent or received tokens in, whole range, a few calls) and falls back to
        eth_getLogs over CLPositionManager, windowed. Either way each tx's
        receipt is decoded by _pancake_chain.decode_receipt. The block cursor
        is banked after the runner persists each batch, exactly like MachineX.

        Without an RPC the old subgraph path runs (it raises on the outage,
        which the runner records as a failed sync).
        """
        key = (wallet.lower(), chain.value)
        self._scan_reached_head[key] = False
        if chain != Chain.BSC:
            self._scan_reached_head[key] = True
            return
        if self._rpc is None:
            yield self.fetch_events(wallet, chain, since_ts=since_ts)
            self._scan_reached_head[key] = True
            return

        if self._rpc.url == PUBLIC_RPC[Chain.BSC]:
            # The public node refuses eth_getLogs over any useful range and
            # serves no alchemy_getAssetTransfers. Say what is needed rather
            # than crawl. Raised, so the runner records a failed sync.
            raise RuntimeError(
                "Pancake events are read from the chain and need a private BSC endpoint "
                "— set ALCHEMY_API_KEY (Pay As You Go: the free tier caps eth_getLogs at "
                "10 blocks on BNB)"
            )
        head = self._rpc.get_block_number()
        if head is None:
            _log.warning("Pancake chain scan: BSC head unknown — cursor held")
            return
        cursor = self._chain_cursor(wallet, head)
        known_tokens = self._known_pool_tokens(wallet)

        windows: list[tuple[int, int, list[str]]]
        try:
            # The transfers API answers a whole range in a few calls, so it
            # covers cursor → head in one pass; the per-run cap below exists
            # only for the windowed log scan.
            txs = self._discover_txs_asset_transfers(wallet, cursor, head)
            to_block = head
            windows = [(cursor, to_block, txs)]
        except Exception as e:  # not Alchemy, or the API is not served on this plan
            _log.info("Asset-transfers discovery unavailable (%s) — using eth_getLogs", e)
            to_block = min(cursor + self.CHAIN_MAX_BLOCKS_PER_RUN, head)
            windows = [
                (a, min(a + self.CHAIN_WINDOW_BLOCKS - 1, to_block), [])
                for a in range(cursor, to_block + 1, self.CHAIN_WINDOW_BLOCKS)
            ]
            txs = None

        token_ids = self._known_token_ids(wallet)
        for win_start, win_end, found in windows:
            if txs is None:
                found = self._discover_txs_logs(wallet, token_ids, win_start, win_end)
            events: list[Event] = []
            for tx in found:
                events.extend(self._events_from_tx(wallet, chain, tx, known_tokens, token_ids))
            yield events
            # Persisted by the runner — safe to bank.
            self._set_chain_cursor(wallet, win_end + 1)
            if deadline is not None and time.monotonic() > deadline:
                _log.info("Pancake chain scan pausing at block %d (time budget)", win_end + 1)
                self._scan_reached_head[key] = win_end >= head
                return
        self._scan_reached_head[key] = to_block >= head

    def last_scan_reached_head(self, wallet: str, chain: Chain) -> bool:
        return self._scan_reached_head.get((wallet.lower(), chain.value), False)

    def _cursor_key(self, wallet: str) -> str:
        return f"pancake_block_cursor:{wallet.lower()}"

    def _set_chain_cursor(self, wallet: str, block: int) -> None:
        self._storage.kv_set(self._cursor_key(wallet), str(block))

    def _chain_cursor(self, wallet: str, head: int) -> int:
        """Where the chain scan resumes. First run: the block of the newest
        event the subgraph ever gave us (its tx's receipt says which block),
        so the scan starts exactly where the subgraph stopped. A wallet with
        no Pancake history at all starts at head — there is nothing behind it
        to recover."""
        stored = self._storage.kv_get(self._cursor_key(wallet))
        if stored:
            return int(stored)
        with self._storage.connect() as conn:
            row = conn.execute(
                "SELECT tx_hash FROM events WHERE wallet = ? AND chain = 'bsc'"
                " AND protocol_id = ? ORDER BY ts DESC LIMIT 1",
                (wallet.lower(), self.info.protocol_id),
            ).fetchone()
        if row is None:
            return head
        assert self._rpc is not None
        receipt = self._rpc.get_transaction_receipt(row["tx_hash"])
        if not receipt:
            raise RuntimeError(f"cannot place the scan start: no receipt for {row['tx_hash']}")
        return int(receipt["blockNumber"], 16)

    def _known_token_ids(self, wallet: str) -> set[int]:
        """Every position NFT this wallet is known to have held on Pancake."""
        ids: set[int] = set()
        with self._storage.connect() as conn:
            for r in conn.execute(
                "SELECT meta_json FROM snapshots WHERE wallet = ? AND chain = 'bsc'"
                " AND protocol_id = ? AND meta_json IS NOT NULL"
                " UNION SELECT meta_json FROM events WHERE wallet = ? AND chain = 'bsc'"
                " AND protocol_id = ? AND meta_json IS NOT NULL",
                (wallet.lower(), self.info.protocol_id) * 2,
            ):
                tid = json.loads(r["meta_json"]).get("token_id")
                if tid:
                    ids.add(int(tid))
        return ids

    def _known_pool_tokens(self, wallet: str) -> dict[str, tuple[str, str]]:
        """pool id → (currency0, currency1) from past events' amounts."""
        out: dict[str, tuple[str, str]] = {}
        with self._storage.connect() as conn:
            for r in conn.execute(
                "SELECT meta_json, amounts_json FROM events WHERE chain = 'bsc'"
                " AND protocol_id = ? AND meta_json IS NOT NULL",
                (self.info.protocol_id,),
            ):
                pool = json.loads(r["meta_json"]).get("pool_id")
                amts = json.loads(r["amounts_json"])
                if pool and len(amts) == 2:
                    a, b = (x["token_key"].split(":", 1)[1] for x in amts)
                    out[pool.lower()] = (a, b)
        return out

    def _discover_txs_asset_transfers(
        self, wallet: str, from_block: int, to_block: int
    ) -> list[str]:
        """Every tx in [from, to] where the wallet sent or received an ERC20 or
        a position NFT, via Alchemy's alchemy_getAssetTransfers. Raises when
        the endpoint does not serve it — the caller then falls back."""
        assert self._rpc is not None
        if "alchemy.com" not in self._rpc.url:
            raise RuntimeError("not an Alchemy endpoint")
        hashes: set[str] = set()
        for direction in ("fromAddress", "toAddress"):
            page: str | None = None
            while True:
                params: dict = {
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    direction: wallet.lower(),
                    "category": ["erc20", "erc721"],
                    "excludeZeroValue": False,
                    "maxCount": "0x3e8",
                }
                if page:
                    params["pageKey"] = page
                res = self._rpc.call("alchemy_getAssetTransfers", [params]) or {}
                hashes.update(t["hash"].lower() for t in res.get("transfers", []))
                page = res.get("pageKey")
                if not page:
                    break
        return sorted(hashes)

    def _discover_txs_logs(
        self, wallet: str, token_ids: set[int], from_block: int, to_block: int
    ) -> list[str]:
        """Txs in [from, to] touching the wallet's position NFTs, via eth_getLogs.
        New NFTs minted to the wallet in the window join `token_ids` first."""
        assert self._rpc is not None
        minted = self._rpc.get_logs_chunked(
            CL_POS_MANAGER,
            [TRANSFER_TOPIC, None, address_topic(wallet)],
            from_block,
            to_block,
            chunk_size=self.CHAIN_WINDOW_BLOCKS,
            min_chunk=self.CHAIN_MIN_CHUNK,
        )
        token_ids |= minted_token_ids(minted, CL_POS_MANAGER, wallet)
        if not token_ids:
            return []
        logs = self._rpc.get_logs_chunked(
            CL_POS_MANAGER,
            [PM_MODIFY_TOPIC, [uint_topic(t) for t in sorted(token_ids)]],
            from_block,
            to_block,
            chunk_size=self.CHAIN_WINDOW_BLOCKS,
            min_chunk=self.CHAIN_MIN_CHUNK,
        )
        return sorted({lg["transactionHash"].lower() for lg in logs})

    def _events_from_tx(
        self,
        wallet: str,
        chain: Chain,
        tx_hash: str,
        known_tokens: dict[str, tuple[str, str]],
        token_ids: set[int],
    ) -> list[Event]:
        assert self._rpc is not None
        receipt = self._rpc.get_transaction_receipt(tx_hash)
        if not receipt:
            # Not a drop we can skip past: raise so the window is not banked.
            raise RuntimeError(f"no receipt for {tx_hash}")
        token_ids |= minted_token_ids(receipt.get("logs", []), CL_POS_MANAGER, wallet)
        changes = decode_receipt(receipt, wallet, CL_POS_MANAGER, CL_POOL_MANAGER, known_tokens)
        if not changes:
            return []
        ts = self._rpc.get_block_timestamp(changes[0].block_number)
        if ts is None:
            raise RuntimeError(f"no block timestamp for {tx_hash}")
        out: list[Event] = []
        for c in changes:
            token_ids.add(c.token_id)
            if c.principal is None or c.tokens is None:
                self._note_dropped_event(
                    wallet, chain, "; ".join(c.notes) or "unresolvable", tx_hash
                )
                continue
            known_tokens.setdefault(c.pool_id, c.tokens)
            token0 = self._token_for(chain, c.tokens[0])
            token1 = self._token_for(chain, c.tokens[1])
            scale0, scale1 = Decimal(10**token0.decimals), Decimal(10**token1.decimals)
            prin = (Decimal(c.principal[0]) / scale0, Decimal(c.principal[1]) / scale1)
            fees = (Decimal(c.fees[0]) / scale0, Decimal(c.fees[1]) / scale1)
            p0, p1 = self._event_prices(token0, token1, ts, prin if any(prin) else fees)
            if p0 is None or p1 is None:
                self._note_dropped_event(
                    wallet,
                    chain,
                    f"no price for {token0.symbol}/{token1.symbol} at ts={ts}",
                    tx_hash,
                )
                continue
            meta = {
                "pool_id": c.pool_id,
                "tick_lower": c.tick_lower,
                "tick_upper": c.tick_upper,
                "liquidity_delta": c.liquidity_delta,
                "token_id": c.token_id,
                "source": "chain",
            }
            position_key = f"{c.pool_id}:{c.tick_lower}:{c.tick_upper}"
            prices = {token0.key: p0, token1.key: p1}

            def make(
                log_index: int,
                kind: EventKind,
                amounts: list[TokenAmount],
                usd: Decimal,
                ev_meta: dict,
                c=c,
                position_key=position_key,
                prices=prices,
            ) -> Event:
                return Event(
                    wallet=wallet.lower(),
                    chain=chain,
                    protocol_id=self.info.protocol_id,
                    position_key=position_key,
                    tx_hash=c.tx_hash,
                    log_index=log_index,
                    ts=ts,
                    block_number=c.block_number,
                    kind=kind,
                    amounts=amounts,
                    prices_at_ts=prices,
                    usd_at_ts=usd,
                    meta=ev_meta,
                )

            if c.liquidity_delta != 0:
                out.append(
                    make(
                        c.log_index,
                        EventKind.DEPOSIT if c.liquidity_delta > 0 else EventKind.WITHDRAW,
                        [TokenAmount(token0, prin[0]), TokenAmount(token1, prin[1])],
                        prin[0] * p0 + prin[1] * p1,
                        meta,
                    )
                )
            if any(fees):
                netted = c.liquidity_delta != 0
                # Fees released by a liquidity change are not a harvest, and
                # sweep detection must not read them as one.
                flag = {}
                if c.liquidity_delta < 0:
                    flag = {"netted_against_decrease": True}
                elif c.liquidity_delta > 0:
                    flag = {"netted_against_increase": True}
                out.append(
                    make(
                        c.log_index + (self._SYNTHETIC_COLLECT_LOG_OFFSET if netted else 0),
                        EventKind.COLLECT,
                        [TokenAmount(token0, fees[0]), TokenAmount(token1, fees[1])],
                        fees[0] * p0 + fees[1] * p1,
                        {**meta, **flag},
                    )
                )
        return out

    def _token_for(self, chain: Chain, address: str) -> Token:
        with self._storage.connect() as conn:
            row = conn.execute(
                "SELECT symbol, decimals FROM tokens WHERE chain = ? AND address = ?",
                (chain.value, address.lower()),
            ).fetchone()
        if row is not None:
            return Token(chain, address.lower(), row["symbol"], int(row["decimals"]))
        assert self._rpc is not None
        symbol, decimals = self._rpc.erc20_metadata(address)
        if decimals is None:
            raise RuntimeError(f"cannot read decimals for {address} — amounts would be wrong")
        token = Token(chain, address.lower(), symbol or address[:10], decimals)
        self._storage.upsert_token(token)
        return token

    def _event_prices(
        self, token0: Token, token1: Token, ts: int, amounts: tuple[Decimal, Decimal]
    ) -> tuple[Decimal | None, Decimal | None]:
        """Prices at the event, with the same amount-ratio fallback the
        subgraph path uses when one side has no market price."""
        pp0 = self.get_historical_price(token0, ts)
        pp1 = self.get_historical_price(token1, ts)
        p0 = pp0.price_usd if pp0 else None
        p1 = pp1.price_usd if pp1 else None
        a0, a1 = abs(amounts[0]), abs(amounts[1])
        if p0 is None and p1 is not None and a0 > 0 and a1 > 0:
            p0 = a1 * p1 / a0
        elif p1 is None and p0 is not None and a0 > 0 and a1 > 0:
            p1 = a0 * p0 / a1
        return p0, p1

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
            _log.warning("Pancake subgraph unavailable (%s) — reconstructing positions via RPC", e)
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
            unclaimed_usd: Decimal | None = Decimal("0")
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
                _log.warning(
                    "BSC RPC fee computation failed for pool %s: %s — unclaimed unknown",
                    pool_id,
                    e,
                )
                unclaimed_usd = None

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
                    # source: positions were reconstructed from subgraph event
                    # history (fee math still reads the chain over RPC, which
                    # is normal here). The degraded counterpart is
                    # 'rpc_fallback', set in _fetch_positions_rpc when the
                    # subgraph is unavailable.
                    meta={"token_id": matched_tid, "event_count": len(evs), "source": "subgraph"},
                )
            )

        return positions

    # ── RPC-only fallback (subgraph outage) ───────────────────────────────

    def fetch_positions_at(self, wallet: str, chain: Chain, block: int, ts: int) -> list[Position]:
        """Positions as they stood at `block`, from BSC archive state at that
        day's prices. Every identity ever recorded is tried — a position open
        then may be closed now — and one with no liquidity at that block is
        simply absent. Needs an archive endpoint (Alchemy)."""
        if self._rpc is None or chain != Chain.BSC:
            return []
        if self._rpc.url == PUBLIC_RPC[Chain.BSC]:
            raise RuntimeError("the public BSC node serves no archive state — set ALCHEMY_API_KEY")
        live = self._rpc
        self._rpc = live.pinned(block)
        self._as_of_ts = ts
        try:
            positions = self._fetch_positions_rpc(wallet, chain, every_identity=True)
        finally:
            self._rpc, self._as_of_ts = live, None
        for p in positions:
            p.snapshot_at = ts
            p.meta = {**p.meta, "source": "archive", "block": block}
        return positions

    def _fetch_positions_rpc(
        self, wallet: str, chain: Chain, every_identity: bool = False
    ) -> list[Position]:
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
            raise RuntimeError("Pancake subgraph down and no BSC RPC configured — cannot snapshot")

        pid = self.info.protocol_id
        identities = (
            self._storage.all_position_identities(wallet, chain, pid)
            if every_identity
            else self._storage.open_position_identities(wallet, chain, pid)
        )
        # Positions the chain scan found that no snapshot has seen yet — opened
        # after the subgraph died, so nothing else knows they exist.
        seeded = {i["position_key"] for i in identities}
        pseudo_rows = self._event_only_rows(wallet, chain, seeded)
        identities = identities + [{"position_key": k} for k in pseudo_rows]
        if not identities:
            return []

        slot0_cache: dict[str, tuple[int, int]] = {}
        pool_liquidity_cache: dict[str, int | None] = {}
        positions: list[Position] = []

        for ident in identities:
            position_key = ident["position_key"]
            row = pseudo_rows.get(position_key) or (
                self._latest_live_row(wallet, chain, position_key)
                if every_identity
                else self._storage.latest_snapshot_row(wallet, chain, pid, position_key)
            )
            if row is None:
                raise RuntimeError(f"No snapshot row for open position {position_key}")
            snap_meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
            token_id = snap_meta.get("token_id")
            if not token_id:
                raise RuntimeError(f"No stored tokenId for {position_key} — cannot verify on-chain")

            pool_id, tick_lower_s, tick_upper_s = position_key.rsplit(":", 2)
            tick_lower, tick_upper = int(tick_lower_s), int(tick_upper_s)

            pos_state = rpc_get_position(
                self._rpc,
                CL_POOL_MANAGER,
                CL_POS_MANAGER,
                pool_id,
                tick_lower,
                tick_upper,
                int(token_id),
            )
            if pos_state is None:
                raise RuntimeError(
                    f"RPC getPosition failed for {position_key} (tokenId {token_id})"
                )
            net_liquidity = pos_state[0]
            if net_liquidity <= 0:
                _log.info(
                    "RPC fallback: %s (tokenId %s) has zero on-chain liquidity — closed",
                    position_key,
                    token_id,
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

            unclaimed_usd: Decimal | None
            try:
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
            except RuntimeError as e:
                # The position itself is verified; only its fees are not.
                _log.warning("Pancake unclaimed fees unreadable for %s: %s", position_key, e)
                unclaimed0 = unclaimed1 = Decimal("0")
                unclaimed_usd = None

            pool_liquidity = pool_liquidity_cache.get(pool_id)
            in_range = tick_lower <= tick_current <= tick_upper
            share_pct = (
                Decimal(net_liquidity) / Decimal(pool_liquidity) * 100
                if pool_liquidity and in_range
                else (Decimal("0") if pool_liquidity else None)
            )
            seven_day_fees = self._pool_fees_7d(pool_id, p0, p1, token0, token1)
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
                    # Pool-wide context is not readable over RPC, and it is
                    # left UNKNOWN rather than carried forward from the last
                    # good subgraph pass. Those values were captured on
                    # 2026-07-06; re-emitting them every day dates them to the
                    # current snapshot, so a figure from a quarter ago reads as
                    # today's. seven_day_fees_usd is not merely displayed
                    # either — it drives the NEGATIVE_CARRY alert, which would
                    # otherwise weigh today's IL against a fee rate from July
                    # and fire (or stay silent) on a comparison that means
                    # nothing. No pool context is the honest state: the alert
                    # skips these positions, and health_warnings says why.
                    position_share_pct=share_pct,
                    seven_day_fees_usd=seven_day_fees,
                    opened_at=first_ts,
                    last_event_at=last_ts,
                    snapshot_at=int(time.time()),
                    meta={
                        "token_id": int(token_id),
                        "source": "rpc_fallback",
                        **(
                            {"pool_liquidity": str(pool_liquidity)}
                            if pool_liquidity is not None
                            else {}
                        ),
                    },
                )
            )

        return positions

    def _event_only_rows(self, wallet: str, chain: Chain, seeded: set[str]) -> dict[str, dict]:
        """Snapshot-shaped rows for positions with chain events but no snapshot:
        net liquidity > 0 from the events' own deltas, identity and tokens from
        the newest event. The live chain read that follows verifies them."""
        with self._storage.connect() as conn:
            rows = conn.execute(
                "SELECT position_key, amounts_json, meta_json FROM events WHERE wallet = ?"
                ' AND chain = ? AND protocol_id = ? AND meta_json LIKE \'%"source": "chain"%\''
                " ORDER BY ts",
                (wallet.lower(), chain.value, self.info.protocol_id),
            ).fetchall()
        net: dict[str, int] = defaultdict(int)
        last: dict[str, dict] = {}
        for r in rows:
            if r["position_key"] in seeded:
                continue
            meta = json.loads(r["meta_json"])
            net[r["position_key"]] += int(meta.get("liquidity_delta") or 0)
            last[r["position_key"]] = {"meta": meta, "amounts": json.loads(r["amounts_json"])}
        out: dict[str, dict] = {}
        for key, liq in net.items():
            if liq <= 0:
                continue
            ev = last[key]
            balances = [{"token_key": a["token_key"], "amount": "0"} for a in ev["amounts"]]
            symbols = [a.get("symbol") or a["token_key"][-6:] for a in ev["amounts"]]
            out[key] = {
                "position_key": key,
                "pair_label": "/".join(symbols),
                "current_balances_json": json.dumps(balances),
                "meta_json": json.dumps({"token_id": ev["meta"].get("token_id")}),
                "fee_tier_bps": None,
            }
        return out

    def _latest_live_row(self, wallet: str, chain: Chain, position_key: str) -> dict | None:
        """The newest NON-tombstone snapshot of a position: a tombstone keeps
        neither its tokenId nor its balances, and a historical read needs both."""
        with self._storage.connect() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE wallet = ? AND chain = ? AND protocol_id = ?"
                " AND position_key = ? AND is_closed = 0 ORDER BY snapshot_date DESC LIMIT 1",
                (wallet.lower(), chain.value, self.info.protocol_id, position_key),
            ).fetchone()
        return dict(row) if row else None

    def _pool_fees_7d(
        self, pool_id: str, p0: Decimal, p1: Decimal, token0: Token, token1: Token
    ) -> Decimal | None:
        """The pool's 7-day LP fee income from its own fee counter
        (getFeeGrowthGlobals × active liquidity, sampled daily by archive
        eth_call) — works on every Alchemy tier, where a week of Swap logs
        does not. Unknown (None) when the reads fail, and on reconstructed
        days: a week read today says nothing about a past one."""
        from defi_tracker.adapters._pool_activity import fee_growth_activity

        assert self._rpc is not None
        if self._rpc.url == PUBLIC_RPC[Chain.BSC] or self._as_of_ts is not None:
            return None
        try:
            act = fee_growth_activity(
                self._rpc,
                cache=self._storage,
                cache_key=f"pool_feegrowth:bsc:{pool_id}",
                read_fee_growth=lambda r: rpc_get_fee_growth_globals(r, CL_POOL_MANAGER, pool_id),
                read_liquidity=lambda r: rpc_get_pool_liquidity_v4(r, CL_POOL_MANAGER, pool_id),
                price0=p0,
                price1=p1,
                dec0=token0.decimals,
                dec1=token1.decimals,
            )
        except Exception as e:
            _log.warning("Pancake pool fees unreadable for %s (%s)", pool_id[:12], e)
            return None
        return act.fees_usd

    # ── Pool history from the fee counter (Phase 5.3, bsc) ────────────────
    # Swap logs are out of reach on the Alchemy free tier (10 blocks per
    # eth_getLogs on BNB), but archive eth_call is not: the pool's own fee
    # counter, read at each day's close, gives the day's LP fees.

    def position_pool(self, position_key: str) -> tuple[str, int, int] | None:
        # Keys are 'poolId:lower:upper' — the tokenId lives in meta.
        parts = position_key.split(":")
        if len(parts) != 3 or not parts[0].startswith("0x"):
            return None
        try:
            return parts[0].lower(), int(parts[1]), int(parts[2])
        except ValueError:
            return None

    def reads_fee_counter(self, chain: Chain) -> bool:
        # The public BNB node is not an archive: its past state reads fail.
        return (
            chain == Chain.BSC and self._rpc is not None and self._rpc.url != PUBLIC_RPC[Chain.BSC]
        )

    def pool_info(self, chain: Chain, pool: str) -> PoolInfo:
        """Tokens from the book's own snapshots of the pool (an Infinity pool
        id is a hash — its currencies are not readable from it), the fee
        from the pool's slot0 (lpFee, parts per million)."""
        if self._rpc is None:
            raise RuntimeError("no BSC RPC")
        with self._storage.connect() as conn:
            row = conn.execute(
                """SELECT position_key, current_balances_json FROM snapshots
                   WHERE chain = ? AND protocol_id = ? AND position_key LIKE ?
                   ORDER BY snapshot_ts DESC LIMIT 1""",
                (chain.value, self.info.protocol_id, f"{pool}:%"),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"no snapshot of pool {pool[:12]} to read its tokens from")
        tok0, tok1 = self._tokens_from_snapshot_row(chain, dict(row))
        raw = self._rpc.eth_call(CL_POOL_MANAGER, SEL_V4_GET_SLOT0 + bytes.fromhex(pool[2:]))
        if not raw or len(raw) < 128:
            raise RuntimeError(f"pool {pool[:12]} slot0 unreadable")
        return PoolInfo(
            chain=chain.value, pool=pool.lower(), token0=tok0.address, token1=tok1.address,
            symbol0=tok0.symbol, symbol1=tok1.symbol, dec0=tok0.decimals, dec1=tok1.decimals,
            fee_ppm=int.from_bytes(raw[96:128], "big"),
        )  # fmt: skip

    def pool_state_at(self, chain: Chain, pool: str, block: int) -> PoolState:
        if self._rpc is None:
            raise RuntimeError("no BSC RPC")
        pinned = self._rpc.pinned(block)
        slot0 = rpc_get_slot0_v4(pinned, CL_POOL_MANAGER, pool)
        growth = rpc_get_fee_growth_globals(pinned, CL_POOL_MANAGER, pool)
        liq = rpc_get_pool_liquidity_v4(pinned, CL_POOL_MANAGER, pool)
        if slot0 is None or growth is None or liq is None:
            raise RuntimeError(f"pool {pool[:12]} state unreadable at block {block}")
        return PoolState(block, growth[0], growth[1], liq, slot0[0], slot0[1])

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
            ratio = self.price_from_sqrt(sqrt_price, token0.decimals, token1.decimals, Decimal("1"))
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
                rpc_url=endpoint_url(Chain.BSC),
                rpc_fallbacks=fallback_urls(Chain.BSC),
                db_path=os.getenv("TRACKER_DB", "tracker.db"),
            )
        )


_autoregister()
