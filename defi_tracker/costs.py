"""
What the book's transactions actually cost.

`pnl_decomposition` used to answer "what did the positions do" and call it
PnL. Gas is a real, realized, already-paid cost that appeared nowhere — on a
book that rebalances, it is the difference between a yield number and a
return number.

Gas is a property of a transaction, not of a protocol: every EVM chain
charges `gasUsed × effectiveGasPrice` in its native token, whoever the
counterparty contract was. So this module is chain-level and sits beside the
adapters rather than inside them — there is no adapter-specific branch here
and none is needed.

Slippage is deliberately NOT inferred. A mint or a burn has no slippage, and
the swap leg of a zap or a rebalance is not in `events` at all, so there is
nothing to measure it against. `tx_costs.slippage_usd` stays NULL until
something can actually measure it, and every surface renders that as unknown
rather than as zero. Claiming zero slippage would be worse than claiming
nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from defi_tracker.adapters._pricing import PriceFetcher
from defi_tracker.adapters._rpc import RpcClient
from defi_tracker.core.endpoints import fallback_urls
from defi_tracker.core.endpoints import rpc_url as endpoint_url
from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Chain, Token

_log = logging.getLogger(__name__)

WEI = Decimal(10) ** 18


@dataclass(frozen=True)
class GasToken:
    """The native token a chain charges gas in, and the wrapped ERC-20 that
    stands in for it when pricing — CoinGecko prices contracts, and the
    native asset has no contract."""

    symbol: str
    wrapped_address: str
    decimals: int


# Wrapped-native addresses, used only as price proxies. Gas is always charged
# in the native asset; the wrapped token tracks it 1:1 by construction.
GAS_TOKENS: dict[Chain, GasToken] = {
    Chain.BSC: GasToken(
        "BNB",
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
        18,
    ),
    Chain.BASE: GasToken(
        "ETH",
        "0x4200000000000000000000000000000000000006",
        18,
    ),
    Chain.PEAQ: GasToken(
        "PEAQ",
        "0x3cd66d2e1fac1751b0a20bebf6ca4c9699bb12d7",  # WPEAQ
        18,
    ),
    Chain.ETHEREUM: GasToken(
        "ETH",
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        18,
    ),
    Chain.ARBITRUM: GasToken(
        "ETH",
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        18,
    ),
    Chain.OPTIMISM: GasToken(
        "ETH",
        "0x4200000000000000000000000000000000000006",
        18,
    ),
    Chain.POLYGON: GasToken(
        "POL",
        "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
        18,
    ),
    Chain.AVALANCHE: GasToken(
        "AVAX",
        "0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7",
        18,
    ),
}


def _hex_int(value) -> int | None:
    """Receipt fields arrive as hex strings, occasionally as ints."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 16)
    except ValueError:
        return None


def gas_native_from_receipt(receipt: dict) -> Decimal | None:
    """`gasUsed × effectiveGasPrice`, in native units.

    `effectiveGasPrice` is the post-EIP-1559 field and is what was actually
    charged; `gasPrice` is the pre-1559 fallback some nodes still return.
    Without either, the cost is unknown — and unknown is the answer, not an
    assumed price.
    """
    used = _hex_int(receipt.get("gasUsed"))
    price = _hex_int(receipt.get("effectiveGasPrice"))
    if price is None:
        price = _hex_int(receipt.get("gasPrice"))
    if used is None or price is None:
        return None
    return Decimal(used) * Decimal(price) / WEI


@dataclass
class BackfillResult:
    """What a pass managed to establish. `priced` is the only number that
    moves the report; the rest exist so a failure is legible."""

    considered: int = 0
    priced: int = 0
    no_receipt: int = 0
    no_price: int = 0
    no_rpc: int = 0

    def __str__(self) -> str:
        return (
            f"{self.priced}/{self.considered} transactions priced "
            f"({self.no_receipt} without a receipt, {self.no_price} without a price, "
            f"{self.no_rpc} without an RPC endpoint)"
        )


def _rpc_for(chain: Chain) -> RpcClient | None:
    cfg = GAS_TOKENS.get(chain)
    if cfg is None:
        return None
    url = endpoint_url(chain)
    return RpcClient(url, fallback_urls=fallback_urls(chain)) if url else None


def backfill_gas(
    storage: Storage,
    chain: Chain | None = None,
    limit: int | None = None,
    rpc_factory=_rpc_for,
    price_fetcher: PriceFetcher | None = None,
) -> BackfillResult:
    """Read receipts for transactions with no priced cost row and record what
    they cost. Idempotent: a transaction already priced is never re-read, so
    this is safe to run on every sync and safe to interrupt.

    A transaction whose receipt or price cannot be had is left unpriced and
    counted. It is not written as zero, and it will be retried next pass.
    """
    todo = storage.transactions_missing_cost(
        chain=str(chain) if chain is not None else None, limit=limit
    )
    result = BackfillResult(considered=len(todo))
    if not todo:
        return result

    prices = price_fetcher or PriceFetcher(storage.db_path)
    clients: dict[str, RpcClient | None] = {}

    for row in todo:
        chain_name = row["chain"]
        try:
            chain_enum = Chain(chain_name)
        except ValueError:
            _log.warning("Unknown chain %s on tx %s — skipping", chain_name, row["tx_hash"])
            result.no_rpc += 1
            continue

        cfg = GAS_TOKENS.get(chain_enum)
        if cfg is None:
            result.no_rpc += 1
            continue
        if chain_name not in clients:
            clients[chain_name] = rpc_factory(chain_enum)
        client = clients[chain_name]
        if client is None:
            _log.info("No RPC for %s — gas stays unknown", chain_name)
            result.no_rpc += 1
            continue

        receipt = client.get_transaction_receipt(row["tx_hash"])
        if not receipt:
            result.no_receipt += 1
            continue
        gas_native = gas_native_from_receipt(receipt)
        if gas_native is None:
            result.no_receipt += 1
            continue

        token = Token(chain_enum, cfg.wrapped_address, cfg.symbol, cfg.decimals)
        price = prices.get(token, row["ts"])
        # The burn itself is a fact even when the price is not, so it is
        # recorded either way — a later pass can price it without re-reading
        # the receipt, and the row states what is still missing.
        storage.upsert_tx_cost(
            chain_name,
            row["tx_hash"],
            row["ts"],
            block_number=row.get("block_number"),
            gas_native=gas_native,
            gas_usd=gas_native * price if price is not None else None,
            native_symbol=cfg.symbol,
            native_price_usd=price,
            source="receipt",
        )
        if price is None:
            result.no_price += 1
        else:
            result.priced += 1

    return result
