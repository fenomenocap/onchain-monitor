"""
Shared fixtures for adapter tests.

These fixtures mock external network calls so adapter tests run in isolation
without any real HTTP, subgraph, or RPC traffic.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_subgraph(request) -> Generator[MagicMock, None, None]:
    """
    Patches requests.post to return a fake subgraph response.

    Usage:
        def test_something(mock_subgraph):
            mock_subgraph.return_value.json.return_value = {"data": {...}}
    """
    with patch("requests.post") as mock_post:
        ok = MagicMock()
        ok.status_code = 200
        ok.raise_for_status.return_value = None
        mock_post.return_value = ok
        yield mock_post


@pytest.fixture
def mock_rpc() -> Generator[MagicMock, None, None]:
    """
    Patches RpcClient.eth_call to return None by default.

    Usage:
        def test_something(mock_rpc):
            mock_rpc.return_value = bytes.fromhex("...")
    """
    with patch("defi_tracker.adapters._rpc.RpcClient.eth_call", return_value=None) as mock:
        yield mock


@pytest.fixture
def mock_coingecko() -> Generator[MagicMock, None, None]:
    """
    Patches requests.get to return a fake CoinGecko response.

    Usage:
        def test_something(mock_coingecko):
            mock_coingecko.return_value.json.return_value = {
                "0xabc...": {"usd": 3000.0}
            }
    """
    with patch("requests.get") as mock_get:
        ok = MagicMock()
        ok.status_code = 200
        ok.raise_for_status.return_value = None
        mock_get.return_value = ok
        yield mock_get
