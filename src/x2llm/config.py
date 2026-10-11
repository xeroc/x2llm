"""Configuration: credentials from the local account store, infra knobs from the env.

12-factor for everything that is NOT a secret (api base, timeout, rate limit);
the account db (see store.py) for everything that is. Bearer falls back to the
public web-client token shipped in registry.py when a row has none.
Manual transaction ids expire within roughly an hour; older pools are
dropped here so the on-demand generator (txgen) takes over - search and
like self-heal between captures.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from . import store
from .registry import BEARER

DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0"
TXID_TTL_SECONDS = 45 * 60  # observed life of captured transaction ids


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    screen_name: str
    auth_token: str
    ct0: str
    cookie: str | None
    bearer: str
    user_agent: str
    api_base: str
    timeout: float
    rate_per_min: int
    txids: dict[str, str]

    @classmethod
    def load(cls, username: str | None = None, env: Mapping[str, str] | None = None) -> Config:
        """Resolve the account (None = the only stored one) and read its row."""
        env = os.environ if env is None else env
        try:
            name = store.resolve(username)
            row = store.get_account(name)
            if row is None:  # resolve() just guaranteed it exists
                raise store.StoreError(f"account disappeared: {name}")
        except store.StoreError as e:
            raise ConfigError(str(e)) from e
        return cls(
            screen_name=name,
            auth_token=row["auth_token"],
            ct0=row["ct0"],
            cookie=row["cookie"] or None,
            bearer=row["bearer"] or BEARER,
            user_agent=row["user_agent"] or DEFAULT_USER_AGENT,
            api_base=env.get("X_API_BASE") or "https://x.com/i/api",
            timeout=float(env.get("X_TIMEOUT") or "20"),
            rate_per_min=int(env.get("X_RATE_LIMIT") or "30"),
            txids=_live_txids(row),
        )

    @property
    def cookie_header(self) -> str:
        if self.cookie:
            return self.cookie
        return f"auth_token={self.auth_token}; ct0={self.ct0}"


def _live_txids(row: dict) -> dict[str, str]:
    """Manual txid pool only while younger than the observed TTL.

    The anchor is the CAPTURE time of the pool (txids_updated), not the
    import time - re-importing an old HAR must not resurrect dead ids.
    Rows from before the column existed have '' and count as stale.
    """
    stamp = row.get("txids_updated") or ""
    if not stamp:
        return {}
    try:
        age = datetime.now(UTC) - datetime.fromisoformat(stamp)
    except ValueError:
        return {}
    return row["txids"] if age.total_seconds() < TXID_TTL_SECONDS else {}
