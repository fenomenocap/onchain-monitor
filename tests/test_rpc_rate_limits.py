"""A rate-limited node is transient: back off and retry — never read it as
an empty range, and never halve a scan into a false "range cap" error."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from defi_tracker.adapters._rpc import RpcClient


def _limited(n_refusals: int):
    """A node that answers `rate limit exceeded` n times, then serves logs."""
    calls = {"n": 0}

    def _post(self, payload):
        calls["n"] += 1
        if calls["n"] <= n_refusals:
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32005, "message": "rate limit exceeded"},
            }  # noqa: E501
        return {"jsonrpc": "2.0", "id": 1, "result": [{"logIndex": "0x0"}]}

    return _post, calls


def test_a_rate_limited_get_logs_backs_off_and_retries():
    post, calls = _limited(2)
    with patch.object(RpcClient, "_post", post), patch("defi_tracker.adapters._rpc.time.sleep"):
        logs = RpcClient("http://x", max_retries=3).get_logs(
            "0xa", [], 0, 10, raise_on_failure=True
        )
    assert logs == [{"logIndex": "0x0"}] and calls["n"] == 3


def test_a_rate_limited_chunked_scan_does_not_halve_into_a_false_range_cap():
    from defi_tracker.adapters._rpc import RpcRateLimited

    post, calls = _limited(10**6)
    with (
        patch.object(RpcClient, "_post", post),
        patch("defi_tracker.adapters._rpc.time.sleep"),
        pytest.raises(RpcRateLimited),
    ):
        RpcClient("http://x", max_retries=2).get_logs_chunked(
            "0xa", [], 0, 9_999, chunk_size=10_000
        )
    assert calls["n"] == 2  # one window, its retries — no halving
