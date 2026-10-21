"""Wait for the network before a scheduled pass, instead of failing into a hole.

The tracker runs from LaunchAgents on a laptop. When the Mac wakes, launchd
fires the missed job at once — often before Wi-Fi has reassociated — and every
source fails together: The Graph, CoinGecko, Slack and the RPC nodes. That has
emptied whole scheduled passes before, and a day with no snapshot is a hole no
later pass can fill.

So a scheduled pass first waits, briefly, for DNS to resolve the hosts it is
about to call. Nothing here judges whether a *source* is healthy — only
whether this machine can reach the internet at all.
"""

from __future__ import annotations

import logging
import socket
import time

from defi_tracker.core.endpoints import resolve
from defi_tracker.core.types import Chain

_log = logging.getLogger(__name__)

# Hosts every pass needs. The RPC hosts come from the resolver so a private
# endpoint is probed rather than a public one the pass will not use.
_ALWAYS = ("api.coingecko.com", "gateway.thegraph.com")


def probe_hosts() -> list[str]:
    hosts = [resolve(c).host for c in (Chain.PEAQ, Chain.BSC, Chain.BASE)]
    return [h for h in (*hosts, *_ALWAYS) if h]


def network_up(hosts: list[str] | None = None) -> bool:
    """True once any probe host resolves. One is enough: the question is
    'is there a network', not 'is every source up'."""
    for host in hosts or probe_hosts():
        try:
            socket.getaddrinfo(host, 443)
            return True
        except OSError:
            continue
    return False


def wait_for_network(
    timeout_s: float = 300.0, interval_s: float = 15.0, *, sleep=time.sleep, clock=time.monotonic
) -> bool:
    """Block until the network is up or `timeout_s` passes. Returns whether
    it came up. Logs once when it had to wait, so a slow wake is visible."""
    deadline = clock() + timeout_s
    waited = False
    while True:
        if network_up():
            if waited:
                _log.warning("Network came up after waiting; starting the pass")
            return True
        if clock() >= deadline:
            _log.error("No network after %.0fs — skipping this pass", timeout_s)
            return False
        waited = True
        sleep(interval_s)
