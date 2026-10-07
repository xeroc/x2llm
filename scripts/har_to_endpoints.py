#!/usr/bin/env python3
"""Harvest wire constants and session credentials from browser HAR captures.

Usage: uv run python scripts/har_to_endpoints.py archive1.har [archive2.har ...]

Two outputs, neither of which touches tracked source:

- registry.new.py (repo root, gitignored): the updated wire-constant
  registry, plus a unified diff against src/x2llm/registry.py. Review the
  diff, then move the file over: `mv registry.new.py src/x2llm/registry.py`.
  Constants are public, session-independent data (query ids, features blobs,
  fieldToggles - from x.com's JS bundles). Merge is incremental: entries in
  the given HARs win; operations they don't contain keep the registry's
  current values.

- .env.fresh (repo root, gitignored): live session secrets - X_AUTH_TOKEN,
  X_CT0 and X_TXIDS (one x-client-transaction-id per operation; the pool is
  shared across endpoints and expires within ~1h). Never touches an
  existing .env; review and `cp .env.fresh .env`.
"""
from __future__ import annotations

import difflib
import json
import sys
import urllib.parse
from pathlib import Path

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
MUTATIONS = {"FavoriteTweet": "like"}


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
        "# this file (registry.new.py + diff) from a browser capture - public\n"
        "# constants only; session-scoped values live in env vars (X_TXIDS etc.).\n"
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


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    paths = sys.argv[1:]
    registry = ROOT / "src" / "x2llm" / "registry.py"
    graphql, bearer, txids, material, fresh, previous = build(paths, registry)

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
        env_out = ROOT / ".env.fresh"
        env_out.write_text(
            "# Generated by har_to_endpoints.py - live session secrets. Keep private.\n"
            f"X_AUTH_TOKEN={material['auth_token']}\n"
            f"X_CT0={material['ct0']}\n"
            "X_TXIDS='" + json.dumps(txids, separators=(",", ":")) + "'\n"
            f"# X_USER_AGENT={material['user_agent']}\n"
            f"# X_COOKIE={material['cookie']}\n"
        )
        print(f"wrote {env_out} ({len(txids)} txids) - review and: cp .env.fresh .env")
    return 0


if __name__ == "__main__":
    main()
