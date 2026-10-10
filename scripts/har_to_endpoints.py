#!/usr/bin/env python3
"""Harvest wire constants (and session credentials) from browser HAR captures.

Usage: uv run python scripts/har_to_endpoints.py [--user NAME] a.har [b.har ...]

Two outputs, one per nature of the data:

- registry.new.py (repo root, gitignored): the updated wire-constant
  registry (query ids, features blobs, fieldToggles - public, global data
  from x.com's JS bundles), plus a unified diff against
  src/x2llm/registry.py. Review the diff, then move the file over:
  `mv registry.new.py src/x2llm/registry.py`. Merge is incremental: entries
  in the given HARs win; operations they don't contain keep the registry's
  current values.

- the local account db (platformdirs user data dir): the harvested
  account's live session material, same as `x2llm import` (identity comes
  from the capture itself via the twid cookie; --user NAME overrides;
  one account per run - the first session found in the entries wins).
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from x2llm.harvest import ALL_OPS, backfill_txids, harvest, store_capture
from x2llm.store import StoreError

ROOT = Path(__file__).resolve().parents[1]


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


def build(paths: list[str], registry: Path) -> tuple[dict, str, dict[str, str], dict, set, set]:
    """Merge HARs over the registry; fail if the result is still incomplete."""
    graphql, bearer = load_registry(registry)
    previous = set(graphql)
    harvested, txids, material = harvest(paths)
    graphql.update(harvested)
    if not bearer:
        bearer = material.get("bearer", "")
    missing = sorted(ALL_OPS - set(graphql))
    if missing:
        sys.exit(f"HARs are missing operations: {missing} - capture those pages, re-export")
    txids = backfill_txids(txids, material.get("newest_tx", ""))
    return graphql, bearer, txids, material, set(harvested), previous


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
        try:
            screen_name = store_capture(material, txids, args.user)
        except StoreError as e:
            sys.exit(str(e))
        print(f"db: stored account '{screen_name}' ({len(txids)} txids)")
    else:
        print("no session material in these HARs - registry proposal only")
    return 0


if __name__ == "__main__":
    main()
