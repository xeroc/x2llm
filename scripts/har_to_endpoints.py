#!/usr/bin/env python3
"""Harvest wire constants and session credentials from browser HAR captures.

Usage: uv run python scripts/har_to_endpoints.py [--user NAME] a.har [b.har ...]

Two outputs, one per nature of the data:

- registry.new.py (repo root, gitignored): the updated wire-constant
  registry (query ids, features blobs, fieldToggles - public, global data
  from x.com's JS bundles), plus a unified diff against
  src/x2llm/registry.py. Review the diff, then move the file over:
  `mv registry.new.py src/x2llm/registry.py`. Merge is incremental: entries
  in the given HARs win; operations they don't contain keep the registry's
  current values.

- the local account db (platformdirs user data dir): the harvested account's
  live session material - auth_token, ct0, bearer, cookie, user_agent and
  the txid pool (shared across endpoints, expires within ~1h). The account
  is identified by a live call to x.com (whoami) unless --user NAME skips
  the check (offline fallback); one account per run - HARs from the first
  session found win.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import sys
import urllib.parse
from pathlib import Path

from x2llm import store
from x2llm.client import XClient
from x2llm.config import DEFAULT_USER_AGENT, Config
from x2llm.registry import BEARER as REGISTRY_BEARER

ROOT = Path(__file__).resolve().parents[1]
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


def load_registry(path: Path) -> tuple[dict, str]:
    if not path.exists():
        return {}, ""
    namespace: dict = {}
    exec(path.read_text(), namespace)  # noqa: S102 - our own tracked source file
    return dict(namespace.get("GRAPHQL") or {}), namespace.get("BEARER") or ""

def render(graphql: dict, bearer: str) -> str:
    return (
        "# Wire constants for x.com GraphQL operations. Plain source code: review\n"
        "# changes like any other file. `make refresh HAR=...` proposes updates to\n"
        "# this file (registry.new.py + diff) from a browser capture - public,\n"
        "# account-independent constants only; session-scoped values live in the\n"
        "# local account db (store.py). The bearer here is the public web-client\n"
        "# token: a boot default, overridden by each stored account's harvested one.\n"
        f"BEARER = {bearer!r}\n"
        f"GRAPHQL = {graphql!r}\n"
    )


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


def build(paths: list[str], registry: Path) -> tuple[dict, str, dict[str, str], dict, set, set]:
    """Merge HARs over the registry; fail if the result is still incomplete."""
    graphql, bearer = load_registry(registry)
    previous = set(graphql)
    harvested, txids, material = harvest(paths)
    graphql.update(harvested)
    if not bearer:
        bearer = material.get("bearer", "")
    wanted = set(READS.values()) | set(POST_READS.values()) | set(MUTATIONS.values())
    missing = sorted(wanted - set(graphql))
    if missing:
        sys.exit(f"HARs are missing operations: {missing} - capture those pages, re-export")
    newest_tx = material.get("newest_tx", "")
    for key in wanted:  # pool is shared across endpoints: backfill missing ops
        if key not in txids and newest_tx:
            txids[key] = newest_tx
    return graphql, bearer, txids, material, set(harvested), previous

def identify(material: dict[str, str], txids: dict[str, str], bearer: str) -> str | None:
    """Ask x.com who these cookies belong to. None = unusable session."""

    async def whoami() -> dict:
        client = XClient(
            Config(
                screen_name="?",
                auth_token=material["auth_token"],
                ct0=material["ct0"],
                cookie=material.get("cookie") or None,
                bearer=bearer,
                user_agent=material.get("user_agent") or DEFAULT_USER_AGENT,
                api_base="https://x.com/i/api",
                timeout=20.0,
                rate_per_min=0,
                txids=txids,
            )
        )
        try:
            return await client.rest("/1.1/account/settings.json", {})
        finally:
            await client.aclose()

    try:
        return asyncio.run(whoami()).get("screen_name") or None
    except Exception:  # noqa: BLE001 - any failure means "do not trust the session"
        return None


def store_capture(material: dict[str, str], txids: dict[str, str], user: str | None) -> str:
    """Identify the account and upsert its session material. Returns screen name."""
    screen_name = user
    if not screen_name:
        screen_name = identify(material, txids, REGISTRY_BEARER)
        if not screen_name:
            sys.exit(
                "could not verify the captured session against x.com "
                "(dead cookies?) - rerun with --user NAME to store anyway"
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
    return screen_name


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Harvest wire constants and session credentials from HAR captures."
    )
    ap.add_argument("har", nargs="+", help="HAR file(s) - one logged-in account per run")
    ap.add_argument(
        "--user",
        metavar="NAME",
        default=None,
        help="store under NAME without the live identity check (offline fallback)",
    )
    args = ap.parse_args()

    registry = ROOT / "src" / "x2llm" / "registry.py"
    graphql, bearer, txids, material, fresh, previous = build([*args.har], registry)

    new = ROOT / "registry.new.py"
    content = render(graphql, bearer)
    new.write_text(content)
    diff = list(difflib.unified_diff(
        registry.read_text().splitlines(), content.splitlines(),
        fromfile="src/x2llm/registry.py", tofile="registry.new.py", lineterm=""))
    print("\n".join(diff) if diff else "registry unchanged")
    print(f"\nwrote {new} - review, then: mv registry.new.py src/x2llm/registry.py")
    print(f"(refreshed: {sorted(fresh) or 'none'}; kept from registry: "
          f"{sorted(previous - fresh) or 'none'})")

    if material.get("auth_token"):
        screen_name = store_capture(material, txids, args.user)
        print(f"db: stored account '{screen_name}' ({len(txids)} txids) -> {store.db_path()}")
    else:
        print("no session material in these HARs - registry proposal only")
    return 0


if __name__ == "__main__":
    main()
