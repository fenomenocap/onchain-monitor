"""
The desk view as a local page rather than a file you have to regenerate.

`defi-tracker web` writes a snapshot of the book to disk; the moment a sync
runs, that file is stale and nothing says so. This module serves the same
`render_dashboard` output over HTTP, re-reading the database on every
request, so a browser refresh is always the current book.

Deliberately stdlib-only: `http.server`, no framework, no dependency. This
is a personal tool serving one reader on loopback — it has no auth, no TLS
and no rate limiting, and binding it to a public interface would publish the
whole book. `serve()` binds 127.0.0.1 by default and warns when told not to.

Prices, venues and exchange order books are the things not re-read per
request: they cost rate-limited network calls, and a browser refresh must not
be able to spend one. They are cached for `price_ttl` seconds; the marks and
the (local) Coinwatch delivery history are read on every request, so the
served page carries everything `defi-tracker web` writes.

Loopback is not a boundary on its own: a page on another site can re-point
its own hostname at 127.0.0.1 (DNS rebinding) and then read this one as
same-origin. So on loopback only a loopback Host header is answered.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic

from defi_tracker.core.storage import Storage
from defi_tracker.report import gather_report_data
from defi_tracker.web import render_dashboard

_log = logging.getLogger(__name__)

# A price lookup is a rate-limited network call; a refresh must not spend one.
DEFAULT_PRICE_TTL = 300.0

PriceProvider = Callable[[Storage], dict]
# (venues, order books): the desk's market reads, fetched like prices.
MarketProvider = Callable[[], tuple[list[dict], dict]]
DeliveryProvider = Callable[[], list[dict]]

_LOOPBACK = ("127.0.0.1", "localhost", "::1")

_EMPTY_PAGE = (
    "<!doctype html><html lang=en><meta charset=utf-8><title>No book yet</title>"
    "<body style='font:16px system-ui;padding:3rem;max-width:40rem'>"
    "<h1>No book yet</h1><p>No snapshot has ever been written to this database. "
    "Run <code>defi-tracker run</code>, then refresh.</p>"
)

_NOT_FOUND = (
    "<!doctype html><html lang=en><meta charset=utf-8><title>Not found</title>"
    "<body style='font:16px system-ui;padding:3rem'><h1>404</h1>"
    "<p>This server serves one page: <a href='/'>/</a>.</p>"
)


class _TtlCache:
    """A network read, re-fetched at most once per `ttl` seconds.

    A failed lookup never fails the request: the page falls back to the last
    value it had, or to `empty`, which renders fees at their claim-time value
    rather than marked to market and the market tables as "not read". A
    missing price is a smaller lie than a missing page.
    """

    def __init__(self, provider: Callable | None, ttl: float, empty, what: str):
        self._provider = provider
        self._ttl = ttl
        self._empty = empty
        self._value = empty
        self._what = what
        self._fetched_at: float | None = None

    def get(self, *args):
        if self._provider is None:
            return self._empty
        now = monotonic()
        if self._fetched_at is not None and now - self._fetched_at < self._ttl:
            return self._value
        try:
            self._value = self._provider(*args)
        except Exception as exc:  # noqa: BLE001 — a read must never 500 the page
            _log.warning("%s lookup failed, serving the page without it: %s", self._what, exc)
        self._fetched_at = now
        return self._value


class DeskHandler(BaseHTTPRequestHandler):
    """Serves the desk view at `/` and nothing else."""

    server_version = "defi-tracker"
    sys_version = ""  # don't advertise the Python version
    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        *args,
        storage: Storage,
        prices: _TtlCache,
        market: _TtlCache,
        delivery: DeliveryProvider | None,
        title: str,
        loopback_only: bool,
        **kw,
    ):
        self._storage = storage
        self._prices = prices
        self._market = market
        self._delivery = delivery
        self._title = title
        self._loopback_only = loopback_only
        super().__init__(*args, **kw)

    # ── request handling ──────────────────────────────────────────────

    def _host_allowed(self) -> bool:
        if not self._loopback_only:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):  # [::1]:8787
            host = host[1 : host.find("]")] if "]" in host else host
        else:
            host = host.rsplit(":", 1)[0]
        return host in _LOOPBACK

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
        if not self._host_allowed():
            self._respond(421, b"Misdirected request\n", "text/plain; charset=utf-8")
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._serve_page()
        elif path == "/healthz":
            self._respond(200, b"ok\n", "text/plain; charset=utf-8")
        else:
            self._respond(404, _NOT_FOUND.encode(), "text/html; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        """Same status and headers as GET, no body — so a health check or a
        link checker doesn't have to render the whole book to ask."""
        self.do_GET()

    def _serve_page(self) -> None:
        try:
            venues, books = self._market.get()
            delivery = None
            if self._delivery is not None:
                try:
                    delivery = self._delivery()
                except Exception as exc:  # noqa: BLE001 — renders as "not read"
                    _log.warning("Delivery history unreadable: %s", exc)
            data = gather_report_data(
                self._storage, self._prices.get(self._storage), venues, delivery, books
            )
        except Exception:
            _log.exception("Failed to gather the book")
            self._respond(500, b"<h1>500</h1><p>Failed to read the book.</p>", "text/html")
            return
        if data is None:
            self._respond(503, _EMPTY_PAGE.encode(), "text/html; charset=utf-8")
            return
        body = render_dashboard(data, title=self._title).encode("utf-8")
        self._respond(200, body, "text/html; charset=utf-8")

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The whole point is that a refresh shows the current book. A cached
        # page would reintroduce exactly the staleness serving is meant to fix.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        """Route access logs through logging rather than raw stderr, so
        `--verbose` controls them like everything else."""
        _log.info("%s %s", self.address_string(), fmt % args)


def make_server(
    storage: Storage,
    host: str = "127.0.0.1",
    port: int = 8787,
    title: str = "Liquidity Desk",
    price_provider: PriceProvider | None = None,
    price_ttl: float = DEFAULT_PRICE_TTL,
    market_provider: MarketProvider | None = None,
    delivery_provider: DeliveryProvider | None = None,
) -> ThreadingHTTPServer:
    """Build the server without starting it — `port=0` picks a free port and
    the caller reads the real one back off `server_address`. Separate from
    `serve()` so tests can drive it without a blocking loop."""
    loopback = host in _LOOPBACK
    if not loopback:
        _log.warning(
            "Serving on %s — this page has no authentication and shows the whole book, "
            "including wallet addresses and position values.",
            host,
        )
    handler = partial(
        DeskHandler,
        storage=storage,
        prices=_TtlCache(price_provider, price_ttl, {}, "Price"),
        market=_TtlCache(market_provider, price_ttl, ([], {}), "Market"),
        delivery=delivery_provider,
        title=title,
        loopback_only=loopback,
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(
    storage: Storage,
    host: str = "127.0.0.1",
    port: int = 8787,
    title: str = "Liquidity Desk",
    price_provider: PriceProvider | None = None,
    price_ttl: float = DEFAULT_PRICE_TTL,
    on_start: Callable[[str], None] | None = None,
    market_provider: MarketProvider | None = None,
    delivery_provider: DeliveryProvider | None = None,
) -> None:
    """Serve until interrupted. `on_start` receives the URL once bound."""
    server = make_server(
        storage, host, port, title, price_provider, price_ttl, market_provider, delivery_provider
    )
    bound_host, bound_port = server.socket.getsockname()[:2]
    url = f"http://{bound_host}:{bound_port}/"
    if on_start:
        on_start(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
