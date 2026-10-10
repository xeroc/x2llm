"""SQLite account store: per-account session credentials, machine-local.

One row per x.com account, keyed by lowercased screen name, plus a tiny
meta table. The db lives in platformdirs user_data_dir ("x2llm") with
0700/0600 permissions - it holds live session secrets (auth_token IS the
account). Infra knobs that are not credentials (X_API_BASE, X_TIMEOUT,
X_RATE_LIMIT) stay in the environment.

Default account: the first import becomes the default; `accounts default`
changes it; an omitted --user/username resolves to it. Removing the
default clears the marker.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from platformdirs import user_data_dir

DEFAULT_KEY = "default_account"


class StoreError(Exception):
    pass


def db_path() -> Path:
    """Location of the account db. Uncached so tests can monkeypatch it."""
    return Path(user_data_dir("x2llm")) / "accounts.db"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS accounts ("
        "screen_name TEXT PRIMARY KEY,"
        "auth_token  TEXT NOT NULL,"
        "ct0         TEXT NOT NULL,"
        "bearer      TEXT NOT NULL DEFAULT '',"
        "cookie      TEXT NOT NULL DEFAULT '',"
        "user_agent  TEXT NOT NULL DEFAULT '',"
        "txids       TEXT NOT NULL DEFAULT '{}',"
        "updated_at  TEXT NOT NULL)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    os.chmod(path, 0o600)
    return conn


def canonical(name: str) -> str:
    return name.lstrip("@").strip().lower()


def _names() -> list[str]:
    with _connect() as conn:
        return [r[0] for r in conn.execute("SELECT screen_name FROM accounts ORDER BY screen_name")]


def get_meta(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_default() -> str | None:
    default = get_meta(DEFAULT_KEY)
    if default and get_account(default):  # dangling marker counts as unset
        return default
    return None


def set_default(screen_name: str) -> str:
    name = canonical(screen_name)
    if not get_account(name):
        stored = ", ".join(_names()) or "none stored yet"
        raise StoreError(f"no such account: {screen_name} (stored: {stored})")
    set_meta(DEFAULT_KEY, name)
    return name


def upsert_account(
    *,
    screen_name: str,
    auth_token: str,
    ct0: str,
    bearer: str = "",
    cookie: str = "",
    user_agent: str = "",
    txids: dict[str, str] | None = None,
) -> None:
    if not screen_name or not auth_token or not ct0:
        raise StoreError("screen_name, auth_token and ct0 are required")
    txids = txids or {}
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in txids.items()):
        raise StoreError("txids must be a dict of {operation: transaction-id} strings")
    name = canonical(screen_name)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO accounts"
            " (screen_name, auth_token, ct0, bearer, cookie, user_agent, txids, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(screen_name) DO UPDATE SET"
            " auth_token=excluded.auth_token, ct0=excluded.ct0, bearer=excluded.bearer,"
            " cookie=excluded.cookie, user_agent=excluded.user_agent, txids=excluded.txids,"
            " updated_at=excluded.updated_at",
            (
                name,
                auth_token,
                ct0,
                bearer,
                cookie,
                user_agent,
                json.dumps(txids, separators=(",", ":")),
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
    if not get_default():  # the very first import becomes the default
        set_meta(DEFAULT_KEY, name)


def get_account(screen_name: str) -> dict | None:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM accounts WHERE screen_name = ?", (canonical(screen_name),)
        ).fetchone()
    if row is None:
        return None
    account = dict(row)
    account["txids"] = json.loads(account["txids"])
    return account


def list_accounts() -> list[dict]:
    """Screen names + freshness + default flag - never secrets (accounts tool)."""
    default = get_default()
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT screen_name, updated_at FROM accounts ORDER BY screen_name"
        ).fetchall()
    return [
        {
            "screen_name": r["screen_name"],
            "updated_at": r["updated_at"],
            "default": r["screen_name"] == default,
        }
        for r in rows
    ]


def remove_account(screen_name: str) -> bool:
    name = canonical(screen_name)
    with _connect() as conn:
        cur = conn.execute("DELETE FROM accounts WHERE screen_name = ?", (name,))
    if get_meta(DEFAULT_KEY) == name:
        set_meta(DEFAULT_KEY, "")
    return cur.rowcount > 0


def resolve(username: str | None) -> str:
    """Map a user-supplied name (or None) to a canonical stored screen name.

    None resolves to the default account; errors say what to do next.
    """
    if username:
        name = canonical(username)
        if not get_account(name):
            stored = ", ".join(_names()) or "none stored yet"
            raise StoreError(f"no such account: {username} (stored: {stored})")
        return name
    names = _names()
    if not names:
        raise StoreError(
            "no accounts stored yet - capture a HAR from a logged-in browser and "
            "run `x2llm import <capture.har>` (see README)"
        )
    default = get_default()
    if not default:
        raise StoreError(
            f"no default account set - pick one with `x2llm accounts default NAME` "
            f"(stored: {', '.join(names)})"
        )
    return default
