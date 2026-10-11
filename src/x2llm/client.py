"""XClient: the only module that knows how x.com RPC works.

Everything above this layer deals in plain dicts. Three primitives:

- graphql(key, variables) -> dict          (GET /i/api/graphql/<queryId>/<Op>)
- graphql_post(key, variables) -> dict    (POST /i/api/graphql/... — feeds + writes)
- rest(path, params) -> dict               (GET /i/api/1.1/... legacy REST)

Auth = public web bearer + cookie session (auth_token + ct0) + x-csrf-token
mirroring ct0. Some endpoints additionally validate x-client-transaction-id
(per operation, from the account store; path-bound, expire server-side).

A client-side rate limiter spaces requests at most X_RATE_LIMIT per minute
(default 30, 0 disables) so the tool stays inside power-user territory.

Transaction ids (x-client-transaction-id) are minted on demand by TxidFactory
(txgen.py) - two anonymous fetches of x.com's public page, then pure CPU.
Manual entries in the account store's txids column override the generator
per operation.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from . import store
from .config import Config
from .registry import GRAPHQL as DEFAULT_GRAPHQL
from .txgen import TxidFactory


class XApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

class XClient:
    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._http = httpx.AsyncClient(timeout=config.timeout, transport=transport)
        self._min_interval = 60.0 / config.rate_per_min if config.rate_per_min > 0 else 0.0
        self._rl_lock = asyncio.Lock()
        self._next_slot = 0.0
        self._txgen = None
        self._graphql: dict | None = None  # defaults merged with db wire, built lazily

    def _op(self, key: str) -> dict:
        """Registry defaults overridden per operation by harvested wire data."""
        if self._graphql is None:
            self._graphql = {**DEFAULT_GRAPHQL, **store.get_wire()}
        return self._graphql[key]

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._txgen is not None:
            await self._txgen.aclose()


    async def graphql(self, key: str, variables: dict) -> dict:
        ep = self._op(key)
        params = {"variables": json.dumps(variables, separators=(",", ":"))}
        if ep.get("features"):
            params["features"] = json.dumps(ep["features"], separators=(",", ":"))
        if ep.get("toggles"):
            params["fieldToggles"] = json.dumps(ep["toggles"], separators=(",", ":"))
        path = f"/graphql/{ep['query_id']}/{ep['name']}"
        await self._throttle()
        response = await self._http.get(
            f"{self.config.api_base}{path}",
            params=params,
            headers=self._headers(await self._txid(key, "GET", f"/i/api{path}")),
        )
        return self._json(response, ep["name"])

    async def graphql_post(self, key: str, variables: dict) -> dict:
        """POST GraphQL: body carries variables+queryId (+features/fieldToggles
        from the registry). Used for POST-fed timelines (home) and the like write."""
        ep = self._op(key)
        body = {"variables": variables, "queryId": ep["query_id"]}
        if ep.get("features"):
            body["features"] = ep["features"]
        if ep.get("toggles"):
            body["fieldToggles"] = ep["toggles"]
        path = f"/graphql/{ep['query_id']}/{ep['name']}"
        await self._throttle()
        response = await self._http.post(
            f"{self.config.api_base}{path}",
            json=body,
            headers=self._headers(await self._txid(key, "POST", f"/i/api{path}")),
        )
        return self._json(response, ep["name"])

    async def rest(self, path: str, params: dict) -> dict:
        await self._throttle()
        response = await self._http.get(
            f"{self.config.api_base}{path}", params=params, headers=self._headers(None)
        )
        return self._json(response, path)

    async def _txid(self, key, method, path):
        """A manual txids entry wins; otherwise mint fresh (only some
        endpoints validate txids - search, like)."""
        manual = self.config.txids.get(key)
        if manual:
            return manual
        if self._txgen is None:
            self._txgen = TxidFactory()
        try:
            await self._txgen.init()
        except Exception:  # noqa: BLE001 - a txid-less request is still worth trying
            self._txgen = None
            return None
        return self._txgen.generate(method, path)

    async def _throttle(self) -> None:
        if not self._min_interval:
            return
        async with self._rl_lock:
            now = time.monotonic()
            wait = self._next_slot - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_slot = now + self._min_interval

    def _headers(self, txid: str | None) -> dict[str, str]:
        headers = {
            "authorization": self.config.bearer,
            "cookie": self.config.cookie_header,
            "x-csrf-token": self.config.ct0,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "user-agent": self.config.user_agent,
            "accept": "*/*",
        }
        if txid:
            headers["x-client-transaction-id"] = txid
        return headers

    @staticmethod
    def _json(response: httpx.Response, what: str) -> dict:
        if response.status_code != 200:
            hint = ""
            if response.status_code == 404 and not response.content:
                hint = (
                    " | hint: empty 404 usually means this endpoint's "
                    "x-client-transaction-id expired - re-capture and "
                    "`x2llm import` (README - Refreshing captured tokens)"
                )
            elif response.status_code in (401, 403):
                hint = (
                    " | hint: credentials rejected - re-capture the browser "
                    "session and `x2llm import` (the harvest stores the full "
                    "Cookie header incl. cf_clearance)"
                )
            elif response.status_code == 429:
                hint = " | hint: rate limited by x.com - slow down or raise the wait"
            raise XApiError(
                f"{what}: HTTP {response.status_code} {response.text[:200]}{hint}",
                status=response.status_code,
            )
        return response.json()
