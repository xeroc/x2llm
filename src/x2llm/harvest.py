"""Harvest session material from browser HAR captures.

Shared by the CLI (`x2llm import`) and scripts/har_to_endpoints.py (which
additionally proposes registry updates). One account per run: material from
the first session found in the entries wins.

The account is identified offline from the capture itself: the `twid`
session cookie carries the viewer's numeric user id, and any user object in
x.com's response bodies with that id names the account. No network needed.
"""
from __future__ import annotations

import asyncio
import json
import urllib.parse
from pathlib import Path

import httpx

from . import store
from .client import XClient
from .config import DEFAULT_USER_AGENT, Config
from .registry import BEARER as REGISTRY_BEARER
from .store import StoreError

READS = {
    "SearchTimeline": "search",
    "TweetDetail": "tweet",
    "NotificationsTimeline": "notifications",
    "ViewerBadgeCounts": "badges",
    "accountOverviewDailyQuery": "analytics",
    "UserByScreenName": "user",
}
POST_READS = {"HomeTimeline": "home",  # FOR YOU tab
              "HomeLatestTimeline": "home_following"}  # FOLLOWING tab
MUTATIONS = {"FavoriteTweet": "like", "CreateTweet": "create_tweet"}
ALL_OPS = frozenset(READS.values()) | frozenset(POST_READS.values()) | frozenset(MUTATIONS.values())


def _find_screen_name(node, viewer_id: str) -> str | None:
    """First user object in an x.com response whose id matches the viewer."""
    if isinstance(node, dict):
        rid = node.get("rest_id") or node.get("id_str")
        if rid == viewer_id:
            core = node.get("core") or {}
            name = core.get("screen_name") or (node.get("legacy") or {}).get("screen_name")
            if name:
                return name
        for v in node.values():
            if found := _find_screen_name(v, viewer_id):
                return found
    elif isinstance(node, list):
        for v in node:
            if found := _find_screen_name(v, viewer_id):
                return found
    return None


def harvest(paths: list[str]) -> tuple[dict, dict[str, str], dict]:
    """Returns (graphql entries, txid pool, cookie/env material)."""
    graphql: dict[str, dict] = {}
    txids: dict[str, str] = {}
    material: dict[str, str] = {}
    newest_seen = newest_tx = ""
    for arg in paths:
        har = json.loads(Path(arg).read_text())
        for e in har["log"]["entries"]:
            req = e["request"]
            headers = {h["name"].lower(): h["value"] for h in req["headers"]}
            tx = headers.get("x-client-transaction-id", "")
            if tx and e["startedDateTime"] > newest_seen:
                newest_seen, newest_tx = e["startedDateTime"], tx
            if not material.get("auth_token"):
                cookies = {}
                for part in headers.get("cookie", "").split("; "):
                    name, _, value = part.partition("=")
                    cookies[name] = value
                if cookies.get("auth_token") and cookies.get("ct0"):
                    material = {
                        "auth_token": cookies["auth_token"],
                        "ct0": cookies["ct0"],
                        "cookie": headers.get("cookie", ""),
                        "user_agent": headers.get("user-agent", ""),
                    }
                    twid = urllib.parse.unquote(cookies.get("twid", ""))
                    if twid.startswith("u="):
                        material["viewer_id"] = twid[2:]
            if material.get("viewer_id") and not material.get("screen_name"):
                body = (e.get("response") or {}).get("content", {}).get("text") or ""
                if material["viewer_id"] in body:
                    try:
                        material["screen_name"] = _find_screen_name(
                            json.loads(body), material["viewer_id"]
                        )
                    except ValueError:
                        pass  # non-JSON body - keep scanning
            url = req["url"]
            if "/i/api/graphql/" not in url:
                continue
            op = url.split("/graphql/", 1)[1].split("/", 1)[1].split("?", 1)[0]
            key = READS.get(op) or POST_READS.get(op) or MUTATIONS.get(op)
            if key and key not in txids and tx:
                txids[key] = tx
            if key is None or key in graphql:
                continue
            if op in POST_READS and req["method"] != "POST":
                continue  # first page may arrive as GET; capture the scroll POST
            entry = {
                "query_id": url.split("/graphql/", 1)[1].split("/", 1)[0],
                "name": op,
                "method": "post" if op in MUTATIONS or op in POST_READS else "get",
            }
            if req["method"] == "POST" and req.get("postData", {}).get("text"):
                body = json.loads(req["postData"]["text"])
                if "features" in body:
                    entry["features"] = body["features"]
                if "fieldToggles" in body:
                    entry["toggles"] = body["fieldToggles"]
            else:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                if "features" in q:
                    entry["features"] = json.loads(q["features"][0])
                if "fieldToggles" in q:
                    entry["toggles"] = json.loads(q["fieldToggles"][0])
            graphql[key] = entry
            if not material.get("bearer"):
                material["bearer"] = headers.get("authorization", "")
    material["newest_tx"] = newest_tx
    return graphql, txids, material


def backfill_txids(txids: dict[str, str], newest_tx: str) -> dict[str, str]:
    """The txid pool is shared across endpoints: fill ops without their own
    captured id with the newest seen one (all expire together, ~1h)."""
    for key in ALL_OPS:
        if key not in txids and newest_tx:
            txids[key] = newest_tx
    return txids


def verify_name(
    material: dict[str, str], name: str, transport: httpx.AsyncBaseTransport | None = None
) -> bool | None:
    """Check that NAME is the account these cookies belong to.

    Live profile lookup: the profile's rest_id must match the capture's
    twid cookie. True/False on a decisive answer; None when no check was
    possible (no twid in the capture, or the lookup itself failed).
    """
    viewer_id = material.get("viewer_id")
    if not viewer_id:
        return None

    async def lookup() -> str | None:
        client = XClient(
            Config(
                screen_name="?",
                auth_token=material["auth_token"],
                ct0=material["ct0"],
                cookie=material.get("cookie") or None,
                bearer=material.get("bearer") or REGISTRY_BEARER,
                user_agent=material.get("user_agent") or DEFAULT_USER_AGENT,
                api_base="https://x.com/i/api",
                timeout=20.0,
                rate_per_min=0,
                txids={},
            ),
            transport=transport,
        )
        try:
            data = await client.graphql(
                "user", {"screen_name": name.lstrip("@"), "withGrokTranslatedBio": True}
            )
        finally:
            await client.aclose()
        result = ((data.get("data") or {}).get("user") or {}).get("result") or {}
        return result.get("rest_id")

    try:
        return asyncio.run(lookup()) == viewer_id
    except Exception:  # noqa: BLE001 - lookup failure means "no verdict", not "mismatch"
        return None


def store_capture(material: dict[str, str], txids: dict[str, str], user: str | None) -> str:
    """Store the capture's session material under its account. Returns screen name."""
    screen_name = user or material.get("screen_name")
    if not screen_name:
        raise StoreError(
            "could not identify the account from this capture (no user object "
            "matching the session's twid cookie) - rerun with --user NAME"
        )
    if user and verify_name(material, user) is False:
        raise StoreError(
            f"this capture does not belong to '{user}' (session id mismatch) - refusing to store"
        )
    store.upsert_account(
        screen_name=screen_name,
        auth_token=material["auth_token"],
        ct0=material["ct0"],
        bearer=material.get("bearer", ""),
        cookie=material.get("cookie", ""),
        user_agent=material.get("user_agent", ""),
        txids=txids,
    )
    return store.canonical(screen_name)
