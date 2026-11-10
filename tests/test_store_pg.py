"""Dialect conformance for the postgres backend (DATABASE_URL store).

Not part of the default run: needs the optional driver and a live,
throwaway database, e.g.

    uv sync --extra postgres --inexact
    DATABASE_URL="postgresql://user:pass@host:5432/x2llm_test" \
        uv run --extra postgres pytest tests/test_store_pg.py

Exercises the same invariants as the sqlite tests in test_x2llm.py,
against whichever DATABASE_URL points at. Tables are wiped on entry.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


def _wipe():
    from x2llm import store

    with store._connect() as conn:
        conn.execute("DELETE FROM accounts")
        conn.execute("DELETE FROM meta")
        conn.execute("DELETE FROM wire")


def test_pg_roundtrip_case_folding_and_defaults():
    from x2llm import store

    _wipe()
    store.upsert_account(
        screen_name="Fabian",
        auth_token="at",
        ct0="ct",
        txids={"search": "STX"},
        txids_updated=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    row = store.get_account("@FABIAN")
    assert row is not None
    assert row["screen_name"] == "fabian"
    assert row["auth_token"] == "at" and row["ct0"] == "ct"
    assert row["txids"] == {"search": "STX"}
    assert row["bearer"] == ""  # NOT NULL DEFAULT '' comes back


def test_pg_upsert_updates_in_place():
    from x2llm import store

    _wipe()
    store.upsert_account(screen_name="a", auth_token="old", ct0="c")
    store.upsert_account(screen_name="a", auth_token="new", ct0="c")
    row = store.get_account("a")
    assert row is not None and row["auth_token"] == "new"
    assert len(store.list_accounts()) == 1


def test_pg_default_marker_and_resolve():
    from x2llm import store

    _wipe()
    store.upsert_account(screen_name="fabian", auth_token="at", ct0="ct")
    store.upsert_account(screen_name="other", auth_token="at2", ct0="ct2")
    assert store.get_default() == "fabian"  # first insert becomes default
    assert store.set_default("OTHER") == "other"  # canonicalized
    assert store.resolve(None) == "other"
    assert store.resolve("@Fabian") == "fabian"
    with pytest.raises(store.StoreError, match="no such account"):
        store.resolve("nobody")
    assert store.remove_account("other") is True
    assert store.remove_account("other") is False  # second remove: gone
    assert store.get_default() is None  # marker cleared with the account


def test_pg_wire_upsert_is_change_tracking():
    from x2llm import store

    _wipe()
    entry = {"query_id": "Q", "name": "SearchTimeline"}
    assert store.upsert_wire({"search": entry}) == ["search"]
    assert store.upsert_wire({"search": entry}) == []  # unchanged -> no write
    assert store.get_wire() == {"search": entry}
    with pytest.raises(store.StoreError, match="query_id"):
        store.upsert_wire({"search": {"name": "x"}})


def test_pg_config_load_reads_rows():
    from x2llm.config import Config

    _wipe()
    from x2llm import store

    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    store.upsert_account(
        screen_name="fabian", auth_token="at", ct0="ct", txids={"search": "STX"},
        txids_updated=stamp,
    )
    cfg = Config.load("fabian", env={})
    assert cfg.screen_name == "fabian"
    assert cfg.auth_token == "at" and cfg.txids == {"search": "STX"}


