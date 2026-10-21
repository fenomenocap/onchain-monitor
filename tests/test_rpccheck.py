"""
RPC endpoint verification.

Two silent failures this exists to make loud: a URL pasted into the wrong
chain's variable (the adapter falls back, the report stays plausible, and
nobody finds out until a number is wrong), and a pruned node presented as an
archive one (it answers `eth_blockNumber` perfectly and then refuses the only
calls that need history).
"""

from __future__ import annotations

from unittest.mock import patch

from defi_tracker.core.types import Chain
from defi_tracker.rpccheck import _host, check_all, check_chain


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _responder(chain_id="0x2105", block="0x1000000", archive=True, fail_method=None):
    """A fake JSON-RPC endpoint driven by the method being called."""

    def _post(_url, json=None, timeout=None):  # noqa: A002 — mirrors requests.post
        method = json["method"]
        if method == fail_method:
            raise RuntimeError("boom")
        if method == "eth_chainId":
            return _Resp({"result": chain_id})
        if method == "eth_blockNumber":
            return _Resp({"result": block})
        if method == "eth_getBalance":
            if archive:
                return _Resp({"result": "0x0"})
            return _Resp({"error": {"message": "missing trie node"}})
        raise AssertionError(f"unexpected method {method}")

    return _post


def test_an_unset_endpoint_is_a_finding_not_an_error():
    r = check_chain(Chain.BASE, "BASE_RPC_URL", url="")
    assert r.configured is False
    assert r.ok is False
    assert "falls back to a public endpoint" in r.notes[0]


def test_a_healthy_archive_endpoint_reports_clean():
    with patch("requests.post", _responder(chain_id="0x2105")):
        r = check_chain(Chain.BASE, "BASE_RPC_URL", url="https://base.example/key")
    assert r.ok
    assert r.chain_id == 8453
    assert r.chain_id_ok is True
    assert r.archive is True
    assert r.notes == []


def test_the_wrong_chain_is_caught_rather_than_trusted():
    """A Base URL in BSC_RPC_URL fails silently everywhere else in the system."""
    with patch("requests.post", _responder(chain_id="0x2105")):
        r = check_chain(Chain.BSC, "BSC_RPC_URL", url="https://base.example/key")
    assert r.ok is False
    assert r.chain_id_ok is False
    assert "WRONG CHAIN" in r.notes[0]


def test_a_pruned_node_is_distinguished_from_an_archive_one():
    """graph-node (roadmap decision C) needs archive state. A pruned node
    answers every shallow call correctly, so nothing else would notice."""
    with patch("requests.post", _responder(chain_id="0x2105", archive=False)):
        r = check_chain(Chain.BASE, "BASE_RPC_URL", url="https://base.example/key")
    assert r.archive is False
    assert r.reachable is True
    assert "not enough for graph-node" in r.notes[0]


def test_an_unreachable_endpoint_is_reported_not_raised():
    with patch("requests.post", _responder(fail_method="eth_chainId")):
        r = check_chain(Chain.BASE, "BASE_RPC_URL", url="https://base.example/key")
    assert r.reachable is False
    assert r.ok is False
    assert "unreachable" in r.notes[0]


def test_the_api_key_in_the_url_is_never_echoed():
    """An RPC URL's path IS the credential. Only the host is ever printed."""
    secret = "https://base-mainnet.g.alchemy.com/v2/SUPERSECRETKEY"
    with patch("requests.post", _responder(chain_id="0x2105")):
        r = check_chain(Chain.BASE, "BASE_RPC_URL", url=secret)
    assert r.url_host == "base-mainnet.g.alchemy.com"
    assert "SUPERSECRETKEY" not in repr(r)


def test_host_extraction_handles_a_bare_host():
    assert _host("localhost:8545") == "localhost:8545"


def test_check_all_defaults_to_the_three_live_chains(monkeypatch):
    for var in ("PEAQ_RPC_URL", "BSC_RPC_URL", "BASE_RPC_URL"):
        monkeypatch.delenv(var, raising=False)
    assert [r.chain for r in check_all()] == [Chain.PEAQ, Chain.BSC, Chain.BASE]
