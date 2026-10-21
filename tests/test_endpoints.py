"""
Endpoint resolution: one order for every chain — explicit URL, then the
Alchemy key, then public — and the source is always reported, never the URL.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from defi_tracker.adapters._rpc import RpcClient
from defi_tracker.core.endpoints import fallback_urls, resolve
from defi_tracker.core.types import Chain


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("ALCHEMY_API_KEY", "BSC_RPC_URL", "BASE_RPC_URL", "PEAQ_RPC_URL",
                "PEAQ_RPC_URL_FALLBACK"):  # fmt: skip
        monkeypatch.delenv(var, raising=False)


def test_the_alchemy_key_alone_gives_bsc_and_base_private_endpoints(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "k3y")
    bsc, base = resolve(Chain.BSC), resolve(Chain.BASE)
    assert bsc.url == "https://bnb-mainnet.g.alchemy.com/v2/k3y"  # not bsc-mainnet
    assert base.url == "https://base-mainnet.g.alchemy.com/v2/k3y"
    assert bsc.source == base.source == "alchemy" and bsc.private


def test_an_explicit_url_beats_the_key(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "k3y")
    monkeypatch.setenv("BSC_RPC_URL", "https://node.example/other")
    assert resolve(Chain.BSC).source == "env:BSC_RPC_URL"


def test_alchemy_does_not_serve_peaq():
    import os

    os.environ["ALCHEMY_API_KEY"] = "k3y"
    try:
        assert resolve(Chain.PEAQ, allow_public=False).source == "none"
        assert resolve(Chain.PEAQ).source == "public"
    finally:
        del os.environ["ALCHEMY_API_KEY"]


def test_nothing_set_falls_back_to_public_and_says_so():
    ep = resolve(Chain.BSC)
    assert ep.source == "public" and not ep.private


def test_the_host_never_carries_the_key(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "s3cret")
    assert "s3cret" not in resolve(Chain.BASE).host


def test_fallbacks_are_optional_and_comma_separated(monkeypatch):
    assert fallback_urls(Chain.PEAQ) == ()
    monkeypatch.setenv("PEAQ_RPC_URL_FALLBACK", "https://a.example, https://b.example")
    assert fallback_urls(Chain.PEAQ) == ("https://a.example", "https://b.example")


def _response(body):
    r = MagicMock()
    r.json.return_value = body
    r.raise_for_status.return_value = None
    return r


def test_the_client_fails_over_when_the_primary_is_down():
    calls = []

    def post(url, **_):
        calls.append(url)
        if url == "https://primary":
            raise ConnectionError("down")
        return _response({"result": "0x10"})

    with patch("defi_tracker.adapters._rpc.requests.post", side_effect=post):
        assert (
            RpcClient("https://primary", fallback_urls=("https://backup",)).get_block_number() == 16
        )
    assert calls == ["https://primary", "https://backup"]


def test_a_log_range_the_node_refuses_is_never_read_as_empty():
    """Regression: an error body has no "result", and reading it as [] told a
    cursor scanner the range held no events."""
    err = _response({"error": {"code": -32602, "message": "block range too large"}})
    with (
        patch("defi_tracker.adapters._rpc.requests.post", return_value=err),
        pytest.raises(RuntimeError, match="range too large"),
    ):
        RpcClient("https://x", max_retries=1).get_logs("0xa", [], 0, 10, raise_on_failure=True)


def test_chunked_scans_halve_a_refused_range_and_skip_nothing():
    seen = []

    def post(url, json, **_):
        f = json["params"][0]
        lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        if hi - lo + 1 > 25:
            return _response({"error": {"message": "range too large"}})
        seen.append((lo, hi))
        return _response({"result": [{"blockNumber": hex(lo)}]})

    with patch("defi_tracker.adapters._rpc.requests.post", side_effect=post):
        logs = RpcClient("https://x", max_retries=1).get_logs_chunked(
            "0xa", [], 0, 99, chunk_size=100
        )
    covered = sorted(b for lo, hi in seen for b in range(lo, hi + 1))
    assert covered == list(range(100))  # every block exactly once
    assert len(logs) == len(seen)


def test_a_refused_range_grows_back_after_the_burst_passes():
    """A burst of logs forces tiny ranges; past it, the scan returns to
    full-size ranges instead of crawling on at the burst's size."""
    seen = []

    def post(url, json, **_):
        f = json["params"][0]
        lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        dense = lo < 20  # blocks 0–19 are a burst: only 2 blocks per call fit
        if hi - lo + 1 > (2 if dense else 100):
            return _response({"error": {"message": "query returned more than 10000 results"}})
        seen.append((lo, hi))
        return _response({"result": []})

    with patch("defi_tracker.adapters._rpc.requests.post", side_effect=post):
        RpcClient("https://x", max_retries=1).get_logs_chunked(
            "0xa", [], 0, 999, chunk_size=100, min_chunk=1
        )
    covered = sorted(b for lo, hi in seen for b in range(lo, hi + 1))
    assert covered == list(range(1000))  # every block exactly once
    assert max(hi - lo + 1 for lo, hi in seen if lo >= 200) == 100  # back to full size
    assert len(seen) < 40
