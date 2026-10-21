"""
Is a configured RPC endpoint actually good enough for what we ask of it?

Two questions the roadmap keeps running into and neither of which a URL
answers on its own:

  - Is this endpoint reachable, and is it the chain it claims to be? A URL
    pasted into the wrong variable fails silently — the adapter falls back,
    the report stays plausible, and nobody finds out until a number is wrong.
  - Is it an ARCHIVE node? `graph-node` needs one, and so does any backfill
    that reads state at an old block. A pruned node answers `eth_blockNumber`
    perfectly and then refuses the only calls that matter.

Read-only: `eth_chainId`, `eth_blockNumber`, and one historical `eth_call`.
It never sends a transaction and never needs a key of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

from defi_tracker.core.endpoints import ENV_VAR, redact, resolve
from defi_tracker.core.types import Chain

# Chain id per chain, so a URL in the wrong variable is caught rather than
# quietly trusted. Sourced from each chain's own documentation.
EXPECTED_CHAIN_ID: dict[Chain, int] = {
    Chain.ETHEREUM: 1,
    Chain.OPTIMISM: 10,
    Chain.BSC: 56,
    Chain.POLYGON: 137,
    Chain.BASE: 8453,
    Chain.ARBITRUM: 42161,
    Chain.AVALANCHE: 43114,
    Chain.PEAQ: 3338,
}

# How far back to probe for archive state. Deep enough that a pruned node
# (typically 128 blocks of state) certainly refuses, shallow enough to exist
# on every chain here.
ARCHIVE_PROBE_DEPTH = 200_000


@dataclass
class RpcReport:
    chain: Chain
    env_var: str
    configured: bool
    url_host: str = ""
    reachable: bool = False
    chain_id: int | None = None
    chain_id_ok: bool | None = None
    block_number: int | None = None
    archive: bool | None = None
    source: str = ""  # 'env:BSC_RPC_URL' | 'alchemy' | 'public' | 'none'
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.reachable and self.chain_id_ok is not False


def _rpc(url: str, method: str, params: list, timeout: int = 10):
    """One call; any failure is re-raised with the URL's path (the key) removed."""
    try:
        r = requests.post(
            url,
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            timeout=timeout,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(redact(f"{type(e).__name__}: {e}")) from None
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"].get("message", str(body["error"])))
    return body.get("result")


def _host(url: str) -> str:
    """The endpoint's host, never its path — an RPC URL's path is the API key."""
    without_scheme = url.split("://", 1)[-1]
    return without_scheme.split("/", 1)[0]


def check_chain(chain: Chain, env_var: str, url: str | None = None) -> RpcReport:
    """Probe one chain's endpoint. Never raises — an unreachable endpoint is
    a finding, not an error."""
    if url is not None:
        endpoint, source = url, (f"env:{env_var}" if url else "none")
    else:
        ep = resolve(chain, allow_public=False)
        endpoint, source = ep.url, ep.source
    report = RpcReport(chain=chain, env_var=env_var, configured=bool(endpoint), source=source)
    if not endpoint:
        report.notes.append("not set — the adapter falls back to a public endpoint")
        return report

    report.url_host = _host(endpoint)
    try:
        raw_id = _rpc(endpoint, "eth_chainId", [])
        report.chain_id = int(str(raw_id), 16)
        report.reachable = True
    except Exception as e:
        report.notes.append(f"unreachable: {e}")
        return report

    expected = EXPECTED_CHAIN_ID.get(chain)
    if expected is not None:
        report.chain_id_ok = report.chain_id == expected
        if not report.chain_id_ok:
            report.notes.append(
                f"WRONG CHAIN: endpoint reports chain id {report.chain_id}, "
                f"expected {expected} for {chain.value}"
            )

    try:
        report.block_number = int(str(_rpc(endpoint, "eth_blockNumber", [])), 16)
    except Exception as e:
        report.notes.append(f"eth_blockNumber failed: {e}")
        return report

    # Archive probe: read state at a block far enough back that a pruned node
    # has dropped it. A plain balance read is enough — any archive-only call
    # would do, and this one needs no contract to exist.
    probe = max(1, report.block_number - ARCHIVE_PROBE_DEPTH)
    try:
        _rpc(
            endpoint,
            "eth_getBalance",
            ["0x0000000000000000000000000000000000000000", hex(probe)],
        )
        report.archive = True
    except Exception as e:
        report.archive = False
        report.notes.append(
            f"no archive state at block {probe:,} ({e}) — fine for current reads, "
            "not enough for graph-node or a state backfill"
        )
    return report


# Which env var each chain reads — the single table in core.endpoints.
CHAIN_ENV: dict[Chain, str] = ENV_VAR


def check_all(chains: list[Chain] | None = None) -> list[RpcReport]:
    targets = chains or [Chain.PEAQ, Chain.BSC, Chain.BASE]
    return [check_chain(c, CHAIN_ENV[c]) for c in targets if c in CHAIN_ENV]
