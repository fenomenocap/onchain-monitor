"""
Schema v7: the custody tables Phase 3 fills.

Transfers carry the `events` discipline — immutable, USD at transfer time,
idempotent by a stable uid — and vault balances are append-only observations.
These tests hold that line before any Fireblocks code exists to test against.
"""

from __future__ import annotations

from decimal import Decimal

from defi_tracker.core.storage import SCHEMA_VERSION, Storage
from defi_tracker.core.types import Transfer, VaultBalance
from tests.conftest import TEST_WALLET


def _transfer(**over) -> Transfer:
    kw = {
        "source": "fireblocks",
        "source_id": "fb-tx-1",
        "ts": 1_758_000_000,
        "asset": "PEAQ",
        "amount": Decimal("12345.678901234567890123"),
        "from_ref": "vault:0",
        "to_ref": "0xpool",
        "chain": "PEAQ",
        "tx_hash": "0xABCDEF",
        "usd_at_ts": Decimal("617.28"),
    }
    kw.update(over)
    return Transfer(**kw)


def test_schema_has_the_v7_custody_tables(storage: Storage):
    assert SCHEMA_VERSION >= 7
    with storage.connect() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"transfers", "vault_balances"} <= tables


def test_v6_database_gains_the_vault_column(storage: Storage):
    with storage.connect() as conn:
        conn.execute("ALTER TABLE wallets DROP COLUMN fireblocks_vault_id")
        conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', '6')")
    storage.init_schema()
    with storage.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(wallets)")}
    assert "fireblocks_vault_id" in cols
    assert storage.schema_version() == SCHEMA_VERSION
    storage.init_schema()  # idempotent


def test_a_wallet_can_be_linked_to_its_vault(storage_with_wallet: Storage):
    assert storage_with_wallet.set_wallet_vault(TEST_WALLET.upper(), "12")
    assert storage_with_wallet.list_wallets()[0]["fireblocks_vault_id"] == "12"
    assert storage_with_wallet.set_wallet_vault(TEST_WALLET, None)
    assert storage_with_wallet.list_wallets()[0]["fireblocks_vault_id"] is None


def test_linking_an_unknown_wallet_says_so(storage: Storage):
    assert not storage.set_wallet_vault("0xnotregistered", "12")


def test_transfers_are_idempotent_and_never_rewritten(storage: Storage):
    assert storage.upsert_transfers([_transfer()]) == 1
    # Same uid, different amount: history is not edited by a re-sync.
    assert storage.upsert_transfers([_transfer(amount=Decimal("1"))]) == 0
    (row,) = storage.transfers_between(0, 2_000_000_000)
    assert row["transfer_uid"] == "fireblocks:fb-tx-1"
    assert row["amount"] == Decimal("12345.678901234567890123")  # exact, not a float


def test_transfer_join_key_is_normalised_to_match_events(storage: Storage):
    storage.upsert_transfers([_transfer()])
    (row,) = storage.transfers_between(0, 2_000_000_000)
    assert (row["chain"], row["tx_hash"]) == ("peaq", "0xabcdef")


def test_an_unpriced_transfer_stays_unpriced(storage: Storage):
    storage.upsert_transfers([_transfer(usd_at_ts=None)])
    assert storage.transfers_between(0, 2_000_000_000)[0]["usd_at_ts"] is None


def test_transfers_between_is_half_open(storage: Storage):
    storage.upsert_transfers([_transfer(ts=100)])
    assert storage.transfers_between(99, 100)
    assert not storage.transfers_between(100, 200)


def test_vault_balance_as_of_reads_the_latest_observation(storage: Storage):
    storage.write_vault_balances(
        [
            VaultBalance("0", "USDC", Decimal("100"), ts=10, price_usd=Decimal("1")),
            VaultBalance("0", "USDC", Decimal("250"), ts=20, price_usd=Decimal("1")),
            VaultBalance("0", "PEAQ", Decimal("5"), ts=15),
        ]
    )
    at_15 = {r["asset"]: r for r in storage.vault_balances_as_of(15)}
    assert at_15["USDC"]["amount"] == Decimal("100")
    assert at_15["PEAQ"]["price_usd"] is None
    assert storage.vault_balances_as_of(25)[1]["amount"] == Decimal("250")


def test_vault_balance_usd_is_unknown_without_a_price():
    assert VaultBalance("0", "PEAQ", Decimal("5"), ts=1).usd_value is None
    assert VaultBalance("0", "USDC", Decimal("5"), ts=1, price_usd=Decimal("1")).usd_value == 5
