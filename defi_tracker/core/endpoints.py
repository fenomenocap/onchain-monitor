"""One place that decides which JSON-RPC endpoint each chain uses, and says why.

Before this, every adapter and every tool read its own `*_RPC_URL` with its own
public default, so "which node is bsc actually on?" had as many answers as
there were call sites. Now there is one resolution order, per chain:

  1. `{CHAIN}_RPC_URL` — an explicit URL always wins;
  2. `ALCHEMY_API_KEY` — for every chain Alchemy serves, the URL is built from
     the key (`https://{network}.g.alchemy.com/v2/{key}`), so the operator
     sets one key instead of pasting one URL per chain;
  3. the chain's public endpoint, if it has one.

The `source` travels with the URL so `rpc-check` and `health` can say which
of the three a chain is on. A URL is never logged or printed: its path is the
API key.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from defi_tracker.core.types import Chain

# An absolute URL's path, or the bare path requests prints after "url:" in a
# connection error ("Max retries exceeded with url: /v2/<key>").
_URL_PATH = re.compile(r"(https?://[^/\s'\"]+)/[^\s'\")]*")
_BARE_URL_PATH = re.compile(r"(\burl: )/[^\s'\")]*")


def redact(text: str) -> str:
    """Strip the path from every URL in `text`. An RPC URL's path IS the API
    key (Alchemy: /v2/<key>; QuikNode: /<token>/; The Graph: /api/<key>/), and
    requests puts the URL — whole, or just its path — in every exception it
    raises, so an error message is a credential leak unless it passes through
    here before it is logged, printed or stored."""
    return _BARE_URL_PATH.sub(r"\1/…", _URL_PATH.sub(r"\1/…", text))


ENV_VAR: dict[Chain, str] = {
    Chain.BSC: "BSC_RPC_URL",
    Chain.BASE: "BASE_RPC_URL",
    Chain.PEAQ: "PEAQ_RPC_URL",
    Chain.ETHEREUM: "ETH_RPC_URL",
    Chain.ARBITRUM: "ARB_RPC_URL",
    Chain.OPTIMISM: "OP_RPC_URL",
    Chain.POLYGON: "POLYGON_RPC_URL",
    Chain.AVALANCHE: "AVAX_RPC_URL",
}

# Alchemy's network slugs. BSC is `bnb-mainnet` — `bsc-mainnet` does not
# resolve. Alchemy does not serve peaq (it is not bring-your-own-chain), so
# peaq has no entry and stays on its explicit URL.
ALCHEMY_NETWORK: dict[Chain, str] = {
    Chain.ETHEREUM: "eth-mainnet",
    Chain.ARBITRUM: "arb-mainnet",
    Chain.OPTIMISM: "opt-mainnet",
    Chain.BASE: "base-mainnet",
    Chain.POLYGON: "polygon-mainnet",
    Chain.AVALANCHE: "avax-mainnet",
    Chain.BSC: "bnb-mainnet",
}

PUBLIC_RPC: dict[Chain, str] = {
    Chain.BSC: "https://bsc-dataseed.bnbchain.org",
    Chain.BASE: "https://mainnet.base.org",
    Chain.PEAQ: "https://peaq.api.onfinality.io/public",
    Chain.ETHEREUM: "https://eth.llamarpc.com",
    Chain.ARBITRUM: "https://arb1.arbitrum.io/rpc",
    Chain.OPTIMISM: "https://mainnet.optimism.io",
    Chain.POLYGON: "https://polygon-rpc.com",
    Chain.AVALANCHE: "https://api.avax.network/ext/bc/C/rpc",
}


@dataclass(frozen=True)
class Endpoint:
    chain: Chain
    url: str  # "" when nothing is available
    source: str  # 'env:BSC_RPC_URL' | 'alchemy' | 'public' | 'none'

    @property
    def private(self) -> bool:
        return self.source.startswith("env:") or self.source == "alchemy"

    @property
    def host(self) -> str:
        """The endpoint's host, never its path — an RPC URL's path is the key."""
        return self.url.split("://", 1)[-1].split("/", 1)[0] if self.url else ""


def resolve(chain: Chain, *, allow_public: bool = True) -> Endpoint:
    """The endpoint `chain` should use right now, and where it came from."""
    var = ENV_VAR.get(chain)
    explicit = os.getenv(var, "").strip() if var else ""
    if explicit:
        return Endpoint(chain, explicit, f"env:{var}")
    key = os.getenv("ALCHEMY_API_KEY", "").strip()
    network = ALCHEMY_NETWORK.get(chain)
    if key and network:
        return Endpoint(chain, f"https://{network}.g.alchemy.com/v2/{key}", "alchemy")
    public = PUBLIC_RPC.get(chain, "") if allow_public else ""
    return Endpoint(chain, public, "public" if public else "none")


def rpc_url(chain: Chain, *, allow_public: bool = True) -> str:
    return resolve(chain, allow_public=allow_public).url


def fallback_urls(chain: Chain) -> tuple[str, ...]:
    """Second-provider endpoints from `{CHAIN}_RPC_URL_FALLBACK` (comma-
    separated), tried in order when the primary does not answer. Optional:
    a failover for a provider's bad hour, not a replacement for the primary."""
    var = ENV_VAR.get(chain)
    raw = os.getenv(f"{var}_FALLBACK", "") if var else ""
    return tuple(u.strip() for u in raw.split(",") if u.strip())
