"""
Minimal JSON-RPC client. One instance per (chain, endpoint).

Multi-chain support means each adapter holds its own RpcClient pointed at
the right endpoint. No global state.
"""

from __future__ import annotations

import logging
import time

import requests

from defi_tracker.core.endpoints import redact

_log = logging.getLogger(__name__)


class RpcTransportError(RuntimeError):
    """A request that did not get an answer, with the URL's path removed."""


class RpcError(RuntimeError):
    """The node answered, with a deterministic JSON-RPC error (range too large…)."""


class RpcRateLimited(RuntimeError):
    """The node kept refusing for rate or capacity — transient, and not a sign
    that the range is too large, so a chunked scan must not halve on it."""


_RATE_LIMIT_MARKERS = ("rate", "429", "too many", "capacity")


class RpcClient:
    def block_at(self, ts: int, near: int | None = None) -> int:
        """The last block at or before unix `ts`: interpolate on block times,
        then step until the answer is exact to the block's own timestamp.

        `near` — a block believed close to the answer (yesterday's close plus
        yesterday's block count) — brackets the search locally: a few reads
        instead of the ~27 a search over the whole chain costs on BSC."""
        if near is not None:
            return self._block_near(ts, max(1, near))
        head = self.get_block_number()
        if head is None:
            raise RpcError("head unreadable")
        lo, hi = 1, head
        lo_ts, hi_ts = self.get_block_timestamp(lo), self.get_block_timestamp(hi)
        if lo_ts is None or hi_ts is None:
            raise RpcError("block timestamps unreadable")
        if ts >= hi_ts:
            return hi
        return self._search(ts, lo, lo_ts, hi, hi_ts)

    def _block_near(self, ts: int, near: int) -> int:
        """block_at, bracketed by stepping out from `near` in growing steps."""
        n_ts = self.get_block_timestamp(near)
        if n_ts is None:  # past the head, or unreadable: search the whole chain
            return self.block_at(ts)
        step = 64
        if n_ts <= ts:
            lo, lo_ts = near, n_ts
            while True:
                hi = lo + step
                hi_ts = self.get_block_timestamp(hi)
                if hi_ts is None:  # beyond the head
                    return self.block_at(ts)
                if hi_ts > ts:
                    break
                lo, lo_ts, step = hi, hi_ts, step * 4
        else:
            hi, hi_ts = near, n_ts
            while True:
                lo = max(1, hi - step)
                t = self.get_block_timestamp(lo)
                if t is None:
                    raise RpcError(f"timestamp of block {lo} unreadable")
                lo_ts = t
                if lo_ts <= ts or lo == 1:
                    break
                hi, hi_ts, step = lo, lo_ts, step * 4
        return self._search(ts, lo, lo_ts, hi, hi_ts)

    def _search(self, ts: int, lo: int, lo_ts: int, hi: int, hi_ts: int) -> int:
        """The last block ≤ ts between lo (ts(lo) ≤ ts) and hi (ts(hi) > ts)."""
        # Interpolation first (fast where block times are steady), bisection
        # after: block times are NOT constant over a chain's life — BSC went
        # from 3s to 0.75s blocks — and pure interpolation then never
        # converges. It once returned block 1 for 2026-06-09. Bisection always
        # converges, in ~27 reads on BSC.
        steps = 0
        while hi - lo > 1:
            if steps < 6:
                guess = lo + int((ts - lo_ts) * (hi - lo) / max(1, hi_ts - lo_ts))
            else:
                guess = (lo + hi) // 2
            steps += 1
            guess = min(max(guess, lo + 1), hi - 1)
            g_ts = self.get_block_timestamp(guess)
            if g_ts is None:
                raise RpcError(f"timestamp of block {guess} unreadable")
            if g_ts <= ts:
                lo, lo_ts = guess, g_ts
            else:
                hi, hi_ts = guess, g_ts
        return lo

    def __init__(
        self,
        url: str,
        timeout: int = 15,
        max_retries: int = 3,
        fallback_urls: tuple[str, ...] = (),
    ):
        self.url = url
        self.fallback_urls = tuple(u for u in fallback_urls if u and u != url)
        self.timeout = timeout
        self.max_retries = max_retries
        self._block_ts_cache: dict[int, int] = {}

    def _post(self, payload: dict) -> dict:
        """POST to the primary endpoint, then each fallback in turn. Returns the
        JSON body of the first endpoint that answers at the HTTP level; raises
        the last error when none does. A JSON-RPC error body is returned, not
        retried elsewhere — the caller decides what an error means."""
        last: Exception | None = None
        for url in (self.url, *self.fallback_urls):
            try:
                r = requests.post(url, json=payload, timeout=self.timeout)
                r.raise_for_status()
                body = r.json()
                if not isinstance(body, dict):
                    raise RpcError(f"unexpected response: {str(body)[:120]}")
                return body
            except RpcError:
                raise
            except Exception as e:
                # Re-raised without the path: the path is the API key, and this
                # message goes to logs, pipeline_health.last_error and Slack.
                last = RpcTransportError(redact(f"{type(e).__name__}: {e}"))
        raise last if last else RuntimeError("no endpoint configured")

    def call(self, method: str, params: list):
        """One JSON-RPC call with retries; returns `result` or raises RpcError
        (for an error body) / the transport error after retries."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                body = self._post(payload)
                if "error" in body:
                    raise RpcError(str(body["error"].get("message", body["error"]))[:300])
                return body.get("result")
            except RpcError:
                raise
            except Exception as e:
                last = e
                if attempt < self.max_retries - 1:
                    time.sleep(0.5 * (2**attempt))
        raise last if last else RpcError(f"{method} failed")

    def pinned(self, block: int) -> PinnedRpc:
        """A client whose reads default to state at `block` (archive node)."""
        return PinnedRpc(self, block)

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
                result = self._post(payload).get("result", "0x")
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
            result = self._post(payload).get("result")
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
                block = self._post(payload).get("result")
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
            return int(self._post(payload)["result"], 16)
        except Exception:
            return None

    def get_logs(
        self,
        address: str | list[str],
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
                body = self._post(payload)
                # A deterministic refusal (range cap, query timeout) will not
                # succeed on retry — raise now so a chunked caller halves the
                # range immediately. Rate limits are transient: back off and
                # retry those. Either way an error body has no "result", and
                # reading it as [] would tell a cursor-based scanner the range
                # was empty when it was never fetched.
                err = body.get("error")
                if err:
                    msg = str(err.get("message", err) if isinstance(err, dict) else err)[:300]
                    if any(k in msg.lower() for k in _RATE_LIMIT_MARKERS):
                        raise RpcRateLimited(msg)
                    raise RpcError(msg)
                result = body.get("result")
                if not isinstance(result, list):
                    raise RpcError(f"eth_getLogs returned no list: {str(result)[:120]}")
                return result
            except RpcError as e:
                last_err = e
                break
            except Exception as e:
                last_err = e
                if attempt == self.max_retries - 1:
                    break
                limited = isinstance(e, RpcRateLimited) or "429" in str(e)
                time.sleep((5.0 if limited else 0.5) * (2**attempt))
        _log.warning("eth_getLogs failed after %d attempts: %s", self.max_retries, last_err)
        if raise_on_failure:
            failed = RpcRateLimited if isinstance(last_err, RpcRateLimited) else RuntimeError
            raise failed(f"eth_getLogs failed for blocks {from_hex}-{to_hex}: {last_err}")
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
        address: str | list[str],
        topics: list[str | None | list[str]],
        from_block: int,
        to_block: int,
        chunk_size: int = 10_000,
        sleep_between_chunks: float = 0.0,
        min_chunk: int = 1,
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
        size = chunk_size
        while start <= to_block:
            end = min(start + size - 1, to_block)
            try:
                batch = self.get_logs(
                    address, topics, from_block=start, to_block=end, raise_on_failure=True
                )
            except RpcRateLimited:
                raise  # a smaller range would be refused just the same
            except RuntimeError:
                # Providers cap log ranges differently (and by result count,
                # not only blocks). Halve and retry the SAME start, so nothing
                # is skipped; only a single-block range failing is fatal.
                if end == start:
                    raise
                size = (end - start + 1) // 2
                if size < min_chunk:
                    # A provider that only serves a handful of blocks per call
                    # would turn a backfill into a crawl no run can finish.
                    raise RuntimeError(
                        f"eth_getLogs refused ranges down to {size * 2} blocks — this "
                        "endpoint's range cap is too small to scan with"
                    ) from None
                continue
            all_logs.extend(batch)
            start = end + 1
            # Refusals are local — a burst of logs, a slow stretch of index —
            # so grow back toward chunk_size after each success rather than
            # crawl the rest of the range at the burst's size.
            size = min(chunk_size, size * 2)
            if sleep_between_chunks > 0 and start <= to_block:
                time.sleep(sleep_between_chunks)
        return all_logs


class PinnedRpc(RpcClient):
    """RpcClient whose eth_call defaults to one historical block, so every
    existing read (positions, slot0, fee growth, balances) can be replayed
    as of that block without threading a block number through each helper."""

    def __init__(self, base: RpcClient, block: int):
        super().__init__(base.url, base.timeout, base.max_retries, base.fallback_urls)
        self.block_number = block
        self._block_ts_cache = base._block_ts_cache

    def eth_call(self, to: str, data: bytes, block: str = "latest") -> bytes | None:
        return super().eth_call(to, data, hex(self.block_number) if block == "latest" else block)
