"""
Minimal JSON-RPC client. One instance per (chain, endpoint).

Multi-chain support means each adapter holds its own RpcClient pointed at
the right endpoint. No global state.
"""

from __future__ import annotations

import logging
import time

import requests

_log = logging.getLogger(__name__)


class RpcClient:
    def __init__(self, url: str, timeout: int = 15, max_retries: int = 3):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._block_ts_cache: dict[int, int] = {}

    def eth_call(self, to: str, data: bytes, block: str = "latest") -> bytes | None:
        """Return raw bytes from eth_call, or None on failure."""
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": to, "data": "0x" + data.hex()}, block],
            "id": 1,
        }
        for attempt in range(self.max_retries):
            try:
                r = requests.post(self.url, json=payload, timeout=self.timeout)
                r.raise_for_status()
                result = r.json().get("result", "0x")
                if not result or result == "0x":
                    return None
                return bytes.fromhex(result[2:])
            except Exception as e:
                if attempt == self.max_retries - 1:
                    _log.warning("RPC eth_call failed: %s", e)
                    return None
                time.sleep(0.5 * (2**attempt))
        return None

    def get_transaction_receipt(self, tx_hash: str) -> dict | None:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash],
            "id": 1,
        }
        try:
            r = requests.post(self.url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            result = r.json().get("result")
            return result if isinstance(result, dict) else None
        except Exception as e:
            _log.warning("RPC eth_getTransactionReceipt failed for %s: %s", tx_hash, e)
            return None

    def get_block_timestamp(self, block_number: int) -> int | None:
        """Return the unix timestamp of a block via eth_getBlockByNumber.

        Cached per instance — event logs cluster in few blocks, so repeat
        lookups are free within one adapter pass.
        """
        cached = self._block_ts_cache.get(block_number)
        if cached is not None:
            return cached
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBlockByNumber",
            "params": [hex(block_number), False],
            "id": 1,
        }
        for attempt in range(self.max_retries):
            try:
                r = requests.post(self.url, json=payload, timeout=self.timeout)
                r.raise_for_status()
                block = r.json().get("result")
                if not block or "timestamp" not in block:
                    return None
                ts = int(block["timestamp"], 16)
                self._block_ts_cache[block_number] = ts
                return ts
            except Exception as e:
                if attempt == self.max_retries - 1:
                    _log.warning("RPC eth_getBlockByNumber failed for %d: %s", block_number, e)
                    return None
                time.sleep(0.5 * (2**attempt))
        return None

    def get_block_number(self) -> int | None:
        payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
        try:
            r = requests.post(self.url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            return int(r.json()["result"], 16)
        except Exception:
            return None

    def get_logs(
        self,
        address: str,
        topics: list[str | None | list[str]],
        from_block: int | str = 0,
        to_block: int | str = "latest",
        raise_on_failure: bool = False,
    ) -> list[dict]:
        """
        Call eth_getLogs. Returns list of log dicts, or [] on error —
        unless raise_on_failure, which raises RuntimeError after retries
        are exhausted. Cursor-based scanners must use raise_on_failure:
        a silent [] would advance their cursor past a range whose events
        were never fetched (a permanent gap in cost basis).

        topics entries may be None (wildcard), a hex string, or a list of
        hex strings (OR match). from_block/to_block may be int or "latest".
        """
        from_hex = hex(from_block) if isinstance(from_block, int) else from_block
        to_hex = hex(to_block) if isinstance(to_block, int) else to_block
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getLogs",
            "params": [
                {
                    "address": address,
                    "topics": topics,
                    "fromBlock": from_hex,
                    "toBlock": to_hex,
                }
            ],
            "id": 1,
        }
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                r = requests.post(self.url, json=payload, timeout=self.timeout)
                r.raise_for_status()
                result = r.json().get("result", [])
                return result if isinstance(result, list) else []
            except Exception as e:
                last_err = e
                if attempt == self.max_retries - 1:
                    break
                wait = 5.0 if "429" in str(e) else 0.5
                time.sleep(wait * (2**attempt))
        _log.warning("eth_getLogs failed after %d attempts: %s", self.max_retries, last_err)
        if raise_on_failure:
            raise RuntimeError(
                f"eth_getLogs failed for blocks {from_hex}-{to_hex}: {last_err}"
            )
        return []

    # ── ERC20 metadata ────────────────────────────────────────────────────

    def erc20_metadata(self, address: str) -> tuple[str | None, int | None]:
        """
        Call symbol() and decimals() on an ERC20 contract.
        Returns (symbol, decimals) — either field may be None on failure.
        Handles both dynamic-string (ERC20 standard) and bytes32 (old MKR-style) symbols.
        """
        sym_raw = self.eth_call(address, bytes.fromhex("95d89b41"))  # symbol()
        symbol = self._decode_abi_string(sym_raw) if sym_raw else None

        dec_raw = self.eth_call(address, bytes.fromhex("313ce567"))  # decimals()
        decimals: int | None = None
        if dec_raw and len(dec_raw) >= 32:
            decimals = int.from_bytes(dec_raw[:32], "big") & 0xFF

        return symbol, decimals

    @staticmethod
    def _decode_abi_string(data: bytes) -> str | None:
        """Decode an ABI-encoded string return value (dynamic or bytes32 fallback)."""
        try:
            if len(data) < 32:
                return None
            first_word = int.from_bytes(data[:32], "big")
            if first_word == 32 and len(data) >= 64:
                str_len = int.from_bytes(data[32:64], "big")
                if len(data) >= 64 + str_len and str_len > 0:
                    return data[64 : 64 + str_len].decode("utf-8", errors="replace").strip("\x00")
            # bytes32 fallback (e.g. old tokens on some chains)
            decoded = data[:32].rstrip(b"\x00").decode("ascii", errors="ignore").strip()
            return decoded or None
        except Exception:
            return None

    def get_logs_chunked(
        self,
        address: str,
        topics: list[str | None | list[str]],
        from_block: int,
        to_block: int,
        chunk_size: int = 10_000,
        sleep_between_chunks: float = 0.0,
    ) -> list[dict]:
        """
        Paginate eth_getLogs over a large block range in chunk_size increments.
        Handles nodes that enforce a max block range per request.
        sleep_between_chunks adds a delay between requests to avoid rate limits.

        Raises RuntimeError if any chunk fails after retries — callers scan by
        cursor, and a silently-empty chunk would leave a permanent event gap.
        """
        all_logs: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(start + chunk_size - 1, to_block)
            batch = self.get_logs(
                address, topics, from_block=start, to_block=end, raise_on_failure=True
            )
            all_logs.extend(batch)
            start = end + 1
            if sleep_between_chunks > 0 and start <= to_block:
                time.sleep(sleep_between_chunks)
        return all_logs
