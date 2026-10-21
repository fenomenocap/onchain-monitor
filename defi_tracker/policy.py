"""Mandate policy limits, loaded from config — the thresholds the book is reported against.

The mandate (§2.2) asks for concentration and counterparty caps "expressed
both in absolute terms and as a share of the balance sheet", an IL tolerance
and stablecoin depeg triggers, but leaves every number blank until leadership
agrees them. So every limit here is optional, and an unset limit is reported
as NOT SET — never as passing. A limit nobody has chosen is not a limit the
book is inside.

Config is TOML (stdlib `tomllib`), read from `$POLICY_FILE` or `./policy.toml`.
Unknown keys are an error rather than ignored: a typo in a limit name would
otherwise silently disable the limit it was meant to set.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

_log = logging.getLogger(__name__)

DEFAULT_POLICY_PATH = "policy.toml"


@dataclass(frozen=True)
class LimitDef:
    """One configurable limit: where it lives in the TOML and what it caps."""

    id: str  # '{section}.{key}', stable — exception log entries key on it
    unit: str  # '%', 'USD', 'bps'
    description: str

    @property
    def section(self) -> str:
        return self.id.split(".", 1)[0]

    @property
    def key(self) -> str:
        return self.id.split(".", 1)[1]


# Every limit the checks know how to evaluate. Denominators for '%' limits are
# the tracked book until custody (Phase 3) makes the whole balance sheet
# visible; the report states which one it used.
LIMITS: tuple[LimitDef, ...] = (
    LimitDef("concentration.max_position_pct", "%", "Largest single position, share of book"),
    LimitDef("concentration.max_position_usd", "USD", "Largest single position, absolute"),
    LimitDef("concentration.max_pair_pct", "%", "Any one pair (all fee tiers), share of book"),
    LimitDef("concentration.max_pair_usd", "USD", "Any one pair (all fee tiers), absolute"),
    LimitDef("concentration.max_venue_pct", "%", "Any one venue (protocol on a chain), share"),
    LimitDef("concentration.max_venue_usd", "USD", "Any one venue (protocol on a chain), absolute"),
    LimitDef("concentration.max_chain_pct", "%", "Any one chain, share of book"),
    LimitDef("concentration.max_chain_usd", "USD", "Any one chain, absolute"),
    LimitDef("counterparty.max_exposure_pct", "%", "Any one counterparty, share of book"),
    LimitDef("counterparty.max_exposure_usd", "USD", "Any one counterparty, absolute"),
    LimitDef("risk.max_il_pct", "%", "Impermanent loss vs HODL, any one position"),
    LimitDef("risk.max_out_of_range_pct", "%", "Capital sitting out of range, share of book"),
    LimitDef("depeg.max_deviation_bps", "bps", "Stablecoin distance from $1.00"),
)
_BY_ID = {d.id: d for d in LIMITS}

# Non-limit keys each section may also carry.
_EXTRA_KEYS = {
    "depeg": {"coins"},  # {SYMBOL = "coingecko-id"} — where the market price comes from
    "counterparty": {"caps"},  # {NAME = usd} — per-counterparty absolute overrides
}


class PolicyError(ValueError):
    """The policy file exists but cannot be trusted as written."""


@dataclass(frozen=True)
class Policy:
    """Loaded limits. `values` holds only limits that were actually set."""

    values: dict[str, Decimal] = field(default_factory=dict)
    depeg_coins: dict[str, str] = field(default_factory=dict)  # SYMBOL → coingecko id
    counterparty_caps: dict[str, Decimal] = field(default_factory=dict)
    source: str = "none"  # the file it came from, or 'none'

    def get(self, limit_id: str) -> Decimal | None:
        if limit_id not in _BY_ID:
            raise KeyError(limit_id)
        return self.values.get(limit_id)

    @property
    def unset(self) -> list[LimitDef]:
        return [d for d in LIMITS if d.id not in self.values]


def _decimal(where: str, raw: object) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise PolicyError(f"{where}: expected a number, got {raw!r}")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as e:
        raise PolicyError(f"{where}: {raw!r} is not a number") from e
    if not value.is_finite() or value < 0:
        raise PolicyError(f"{where}: must be a non-negative number, got {raw!r}")
    return value


def parse_policy(doc: dict, source: str = "inline") -> Policy:
    """Validate a parsed TOML document into a Policy. Raises PolicyError."""
    sections = {d.section for d in LIMITS}
    values: dict[str, Decimal] = {}
    depeg_coins: dict[str, str] = {}
    caps: dict[str, Decimal] = {}
    for section, body in doc.items():
        if section not in sections:
            raise PolicyError(f"unknown section [{section}] (known: {', '.join(sorted(sections))})")
        if not isinstance(body, dict):
            raise PolicyError(f"[{section}] must be a table")
        for key, raw in body.items():
            limit_id = f"{section}.{key}"
            if limit_id in _BY_ID:
                values[limit_id] = _decimal(limit_id, raw)
            elif key in _EXTRA_KEYS.get(section, set()):
                if not isinstance(raw, dict):
                    raise PolicyError(f"{limit_id} must be a table")
                if section == "depeg":
                    depeg_coins = {str(k).upper(): str(v) for k, v in raw.items()}
                else:
                    caps = {str(k): _decimal(f"{limit_id}.{k}", v) for k, v in raw.items()}
            else:
                raise PolicyError(f"unknown key {limit_id!r} — a typo here would disable a limit")
    if "depeg.max_deviation_bps" in values and not depeg_coins:
        raise PolicyError("depeg.max_deviation_bps is set but depeg.coins names no stablecoin")
    return Policy(values=values, depeg_coins=depeg_coins, counterparty_caps=caps, source=source)


def load_policy(path: str | Path | None = None) -> Policy:
    """Read the policy file. No file at the default path is an empty policy
    (every limit NOT SET); a file that was named (argument or $POLICY_FILE)
    but is missing, unreadable or malformed raises, because a broken policy
    that loads as "no limits" would report a book in breach as merely
    unconstrained."""
    named = path or os.getenv("POLICY_FILE")
    p = Path(named or DEFAULT_POLICY_PATH)
    if not p.exists():
        if named:
            raise PolicyError(f"{p}: no such file — a mistyped path would disable every limit")
        return Policy()
    try:
        doc = tomllib.loads(p.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise PolicyError(f"{p}: {e}") from e
    return parse_policy(doc, source=str(p))


def fetch_depeg_prices(policy: Policy, timeout: float = 15.0) -> dict[str, Decimal]:
    """Market USD price per configured stablecoin symbol, from CoinGecko.

    The pricing layer deliberately values stablecoins at exactly $1.00, so the
    book itself can never show a depeg; this is the independent read the
    trigger needs. A symbol that could not be priced is simply absent — the
    check then reports it UNKNOWN rather than on peg.
    """
    if not policy.depeg_coins:
        return {}
    import requests

    ids = ",".join(sorted(set(policy.depeg_coins.values())))
    headers = {"x-cg-demo-api-key": k} if (k := os.getenv("COINGECKO_API_KEY")) else {}
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd"},
            headers=headers,
            timeout=timeout,
        )
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        _log.warning("Stablecoin market prices unavailable: %s", e)
        return {}
    out: dict[str, Decimal] = {}
    for symbol, cg_id in policy.depeg_coins.items():
        usd = (body.get(cg_id) or {}).get("usd")
        if usd is not None:
            out[symbol] = Decimal(str(usd))
    return out


DEFAULT_CHARTER_PATH = "content/mandate.toml"
DEFAULT_RFQ_PATH = "content/rfq.toml"


def _load_content(path: Path) -> dict:
    """A desk content file: missing → {}, malformed → {"error": …}, never a crash."""
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        return {"error": f"{path}: {e}"}


def load_charter(path: str | Path | None = None) -> dict:
    """The mandate as proposed — books, approval authority, plan — for the
    desk's Mandate tab. Kept beside the desk content (gitignored: it names
    approvers), not in policy.toml, which holds only enforceable limits."""
    return _load_content(Path(path or os.getenv("MANDATE_FILE") or DEFAULT_CHARTER_PATH))


def load_rfq(path: str | Path | None = None) -> dict:
    """The MM RFQ comparison — bidders' scope, committed depth by venue and
    scores — for the desk's RFQ tab. Gitignored: counterparty terms."""
    return _load_content(Path(path or os.getenv("RFQ_FILE") or DEFAULT_RFQ_PATH))
