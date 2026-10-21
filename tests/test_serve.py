"""
The desk view as a served page.

The reason to serve rather than write a file is freshness: a file is stale
the moment a sync runs and nothing on it says so. So these tests are mostly
about that — the DB is re-read per request, nothing is cached, and the
failure modes (no book yet, a dead price feed) degrade rather than 500.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from datetime import date
from decimal import Decimal

import pytest

from defi_tracker.serve import _TtlCache, make_server


@pytest.fixture
def server(storage):
    """A real server on a free port, torn down after the test."""
    servers = []

    def _start(**kw):
        srv = make_server(storage, host="127.0.0.1", port=0, **kw)
        servers.append(srv)
        threading.Thread(
            target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        host, port = srv.socket.getsockname()[:2]
        return f"http://{host}:{port}"

    yield _start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


# An opener with proxies explicitly disabled: the default one asks macOS for
# the system proxy configuration, which costs tens of seconds on a first call
# and has nothing to do with reaching our own loopback socket.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str):
    with _OPENER.open(url, timeout=10) as r:
        return r.status, r.headers, r.read().decode()


def _snapshot(storage, make_position, day: str, value: str):
    storage.write_snapshot(
        make_position(current_value_usd=Decimal(value)),
        snapshot_date=date.fromisoformat(day),
    )


def test_serves_the_desk_page(server, storage, make_position):
    _snapshot(storage, make_position, "2026-09-21", "10000")
    status, headers, body = _get(server() + "/")

    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert body.startswith("<!doctype html>")
    assert "$10,000" in body


def test_the_db_is_re_read_on_every_request(server, storage, make_position):
    """The whole justification for serving. A second request after a sync
    must show the new marks, with no restart and no regeneration step."""
    _snapshot(storage, make_position, "2026-09-21", "10000")
    base = server()
    assert "$10,000" in _get(base + "/")[2]

    _snapshot(storage, make_position, "2026-09-22", "12500")
    body = _get(base + "/")[2]
    assert "$12,500" in body
    assert "marks 2026-09-22" in body


def test_the_page_is_never_cached(server, storage, make_position):
    """A cached page would reintroduce exactly the staleness serving fixes."""
    _snapshot(storage, make_position, "2026-09-21", "10000")
    _, headers, _ = _get(server() + "/")
    assert headers["Cache-Control"] == "no-store"


def test_an_empty_database_says_so_rather_than_rendering_an_empty_book(server, storage):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server() + "/")
    assert exc.value.code == 503
    assert "No snapshot has ever been written" in exc.value.read().decode()


def test_unknown_paths_404(server, storage, make_position):
    _snapshot(storage, make_position, "2026-09-21", "10000")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server() + "/admin")
    assert exc.value.code == 404


def test_healthz_is_cheap_and_does_not_render_the_book(server, storage):
    """No snapshots at all, and the health check still answers — it must not
    depend on there being a book to render."""
    status, _, body = _get(server() + "/healthz")
    assert status == 200
    assert body == "ok\n"


def test_a_query_string_still_reaches_the_page(server, storage, make_position):
    _snapshot(storage, make_position, "2026-09-21", "10000")
    assert _get(server() + "/?refresh=1")[0] == 200


# ── prices degrade, they do not fail the page ─────────────────────────────


def test_prices_are_fetched_once_per_ttl_not_once_per_request(server, storage, make_position):
    """A refresh must not be able to spend a rate-limited price call."""
    calls = []

    def provider(_st):
        calls.append(1)
        return {}

    _snapshot(storage, make_position, "2026-09-21", "10000")
    base = server(price_provider=provider, price_ttl=300.0)
    _get(base + "/")
    _get(base + "/")
    _get(base + "/")
    assert len(calls) == 1


def test_a_dead_price_feed_still_serves_the_book(server, storage, make_position):
    def provider(_st):
        raise RuntimeError("CoinGecko is down")

    _snapshot(storage, make_position, "2026-09-21", "10000")
    status, _, body = _get(server(price_provider=provider) + "/")
    assert status == 200
    assert "$10,000" in body


def test_the_price_cache_keeps_the_last_good_value_through_a_failure(storage):
    state = {"fail": False}

    def provider(_st):
        if state["fail"]:
            raise RuntimeError("down")
        return {"bsc:0xabc": Decimal("1")}

    cache = _TtlCache(provider, 0.0, {}, "Price")
    assert cache.get(storage) == {"bsc:0xabc": Decimal("1")}
    state["fail"] = True
    assert cache.get(storage) == {"bsc:0xabc": Decimal("1")}


def test_no_provider_means_no_prices_and_no_calls(storage):
    assert _TtlCache(None, 300.0, {}, "Price").get(storage) == {}


def test_a_rebound_hostname_is_refused_on_loopback(server):
    """DNS rebinding: evil.example resolving to 127.0.0.1 must not read the book."""
    base = server()
    req = urllib.request.Request(base + "/healthz", headers={"Host": "evil.example:8787"})  # noqa: S310
    with pytest.raises(urllib.error.HTTPError) as exc:
        _OPENER.open(req, timeout=10)
    assert exc.value.code == 421
    status, _, _ = _get(base.replace("127.0.0.1", "localhost") + "/healthz")
    assert status == 200


def test_the_served_page_reads_delivery_and_market_like_web(storage, make_position, server):
    """`serve` renders what `web` writes: Coinwatch delivery every request,
    venues and books through the same cache as prices."""
    _snapshot(storage, make_position, "2026-09-01", "1000")
    calls = {"delivery": 0, "market": 0}

    def delivery():
        calls["delivery"] += 1
        return []

    def market():
        calls["market"] += 1
        return [], {}

    base = server(market_provider=market, delivery_provider=delivery, price_ttl=300.0)
    _get(base + "/")
    _get(base + "/")
    assert calls == {"delivery": 2, "market": 1}
