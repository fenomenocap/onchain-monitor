"""An RPC URL's path is its API key; no error message may carry it."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
import requests

from defi_tracker.adapters._rpc import RpcClient
from defi_tracker.core.endpoints import redact

KEY_URL = "https://bnb-mainnet.g.alchemy.com/v2/alch_SECRETKEY123"


def test_redact_keeps_the_host_and_drops_the_path():
    msg = f"400 Client Error: Bad Request for url: {KEY_URL}"
    out = redact(msg)
    assert "SECRETKEY" not in out and "bnb-mainnet.g.alchemy.com/…" in out


def test_a_failed_request_raises_without_the_key():
    err = requests.HTTPError(f"400 Client Error: Bad Request for url: {KEY_URL}")
    with (
        patch("defi_tracker.adapters._rpc.requests.post", side_effect=err),
        pytest.raises(RuntimeError) as exc,
    ):
        RpcClient(KEY_URL, max_retries=1).get_logs("0xa", [], 0, 10, raise_on_failure=True)
    assert "SECRETKEY" not in str(exc.value)


def test_nothing_the_client_logs_carries_the_key(caplog):
    err = requests.ConnectionError(f"Max retries exceeded with url: {KEY_URL}")
    with (
        patch("defi_tracker.adapters._rpc.requests.post", side_effect=err),
        caplog.at_level(logging.WARNING),
    ):
        client = RpcClient(KEY_URL, max_retries=1)
        client.eth_call("0xa", b"\x00")
        client.get_logs("0xa", [], 0, 10)
        client.get_transaction_receipt("0xtx")
    assert caplog.records and "SECRETKEY" not in caplog.text


def test_a_connection_error_names_only_the_path_and_that_is_dropped_too():
    """urllib3's own wording puts a bare path after "url:", with no scheme or
    host — the form a laptop-wake DNS failure actually produces."""
    msg = (
        "HTTPSConnectionPool(host='bnb-mainnet.g.alchemy.com', port=443): Max retries "
        "exceeded with url: /v2/alch_SECRETKEY123 (Caused by NameResolutionError(...))"
    )
    out = redact(msg)
    assert "SECRETKEY" not in out and "url: /…" in out


def test_a_real_connection_failure_is_redacted():
    """Not a hand-built message: whatever requests raises for a refused
    connection (a closed local port, so no network is touched)."""
    url = "http://127.0.0.1:1/v2/alch_SECRETKEY123"
    with pytest.raises(requests.RequestException) as exc:
        requests.post(url, json={}, timeout=2)
    assert "SECRETKEY" not in redact(str(exc.value))


def test_a_stored_health_error_is_redacted(storage):
    from defi_tracker.core.types import Chain

    storage.record_pipeline_health(
        "0xabc", Chain.BSC, "pancake_infinity", sync_ok=False, snapshot_ok=False, source=None,
        error="Max retries exceeded with url: /v2/alch_SECRETKEY123", now=1,
    )  # fmt: skip
    (row,) = storage.pipeline_health()
    assert "SECRETKEY" not in row["last_error"]
