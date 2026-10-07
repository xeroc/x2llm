"""Environment-driven configuration (12-factor: all config from the env)."""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass

from .registry import BEARER

DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0"
REQUIRED = ("X_AUTH_TOKEN", "X_CT0")


class ConfigError(Exception):
    pass


def _parse_txids(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    raw = raw.strip()
    if len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1]  # tolerate quoting from `source .env` / make -include
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise ConfigError(f"X_TXIDS is not valid JSON: {e}") from e
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ConfigError("X_TXIDS must be a JSON object of {operation: transaction-id}")
    return parsed


@dataclass(frozen=True)
class Config:
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
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        missing = [k for k in REQUIRED if not env.get(k)]
        if missing:
            raise ConfigError(
                f"missing environment variables: {', '.join(missing)} "
                "(see README - Credentials)"
            )
        return cls(
            auth_token=env["X_AUTH_TOKEN"],
            ct0=env["X_CT0"],
            cookie=env.get("X_COOKIE") or None,
            bearer=env.get("X_BEARER") or BEARER,
            user_agent=env.get("X_USER_AGENT") or DEFAULT_USER_AGENT,
            api_base=env.get("X_API_BASE", "https://x.com/i/api"),
            timeout=float(env.get("X_TIMEOUT", "20")),
            rate_per_min=int(env.get("X_RATE_LIMIT", "30")),
            txids=_parse_txids(env.get("X_TXIDS")),
        )

    @property
    def cookie_header(self) -> str:
        if self.cookie:
            return self.cookie
        return f"auth_token={self.auth_token}; ct0={self.ct0}"
