"""Click CLI. Every command prints one JSON document; errors print {"error": ...} and exit 1."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import click

from . import cores, store
from .client import XApiError, XClient
from .config import Config, ConfigError


def out(obj) -> None:
    print(json.dumps(obj, default=str, ensure_ascii=False, indent=1))


async def _run(make_coro: Callable[[XClient], Awaitable], username: str | None) -> object:
    config = Config.load(username)
    client = XClient(config)
    try:
        return await make_coro(client)
    finally:
        await client.aclose()


def invoke(make_coro) -> None:
    username = (click.get_current_context().obj or {}).get("username")
    try:
        result = asyncio.run(_run(make_coro, username))
    except (ConfigError, XApiError) as e:
        out({"error": str(e)})
        raise SystemExit(1) from None
    out(result)


@click.group()
@click.version_option()
@click.option(
    "-u",
    "--user",
    "username",
    help="Account to act as (default: the default account; see `x2llm accounts default`).",
    default=None,
)
def cli(username: str | None) -> None:
    """x2llm - read-only x.com (Twitter) API client for agents. JSON out."""
    click.get_current_context().obj = {"username": username}

@cli.command()
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--cursor", default=None, help="Pagination cursor from a previous page.")
@click.option("--tab", type=click.Choice(["for_you", "following"]), default="for_you", show_default=True)
def home(limit: int, cursor: str | None, tab: str) -> None:
    """Your home timeline (for_you or following); scroll with the returned cursor."""
    invoke(lambda c: cores.home(c, limit=limit, cursor=cursor, tab=tab))


@cli.command()
def badges() -> None:
    """Unread notification/DM counts. Cheapest auth check."""
    invoke(lambda c: cores.badges(c))


@cli.command()
@click.argument("screen_name")
def user(screen_name: str) -> None:
    """Profile of a user by screen name: bio, follower counts, verification."""
    invoke(lambda c: cores.user(c, screen_name))


@cli.command()
def me() -> None:
    """Viewer identity + follower counts."""
    invoke(lambda c: cores.me(c))


@cli.command()
@click.argument("query")
def suggest(query: str) -> None:
    """Search typeahead: topic and user suggestions for a prefix."""
    invoke(lambda c: cores.suggest(c, query))


@cli.command()
@click.argument("query")
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--product", type=click.Choice(["Top", "Latest", "Media"]), default="Top", show_default=True)
@click.option("--cursor", default=None, help="Pagination cursor from a previous page.")
def search(query: str, limit: int, product: str, cursor: str | None) -> None:
    """Full search. Supports operators: from:user, since:, lang:, min_faves: ..."""
    invoke(lambda c: cores.search(c, query, limit=limit, product=product, cursor=cursor))


@cli.command()
@click.argument("tweet_id")
@click.option("--cursor", default=None, help="Pagination cursor for more replies.")
def tweet(tweet_id: str, cursor: str | None) -> None:
    """One tweet plus its reply thread."""
    invoke(lambda c: cores.tweet(c, tweet_id, cursor=cursor))


@cli.command()
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--cursor", default=None, help="Pagination cursor from a previous page.")
def notifications(limit: int, cursor: str | None) -> None:
    """Your notification tab."""
    invoke(lambda c: cores.notifications(c, limit=limit, cursor=cursor))


@cli.command()
@click.option("--days", type=int, default=7, show_default=True, help="Window length (1-28).")
def analytics(days: int) -> None:
    """Your engagement statistics (impressions, likes, follows ...)."""
    invoke(lambda c: cores.analytics(c, days=days))


@cli.command()
@click.argument("user")
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--cursor", default=None, help="Pagination cursor from a previous page.")
def following(user: str, limit: int, cursor: str | None) -> None:
    """Accounts USER follows. USER is a numeric id or a screen name."""
    invoke(lambda c: cores.following(c, user, limit=limit, cursor=cursor))


@cli.command()
def mcp() -> None:
    """Run as a stdio MCP server (same tool surface, read-only)."""
    from .mcp import serve

    asyncio.run(serve())


@cli.command("import")
@click.argument("har_file", nargs=-1, required=True, type=click.Path(exists=True))
@click.option(
    "--user",
    "username",
    metavar="NAME",
    default=None,
    help="Store under NAME instead of the capture-derived identity.",
)
def import_account(har_file: tuple[str, ...], username: str | None) -> None:
    """Import an account from HAR capture(s) into the local db.

    The account is identified from the capture itself (its twid session
    cookie matched against user objects in the responses) and stored under
    that screen name. One account per run.
    """
    from . import harvest

    try:
        graphql_entries, txids, material = harvest.harvest(list(har_file))
        if not material.get("auth_token"):
            raise store.StoreError("no session cookies in these HAR files (auth_token/ct0)")
        txids = harvest.backfill_txids(txids, material.get("newest_tx", ""))
        name = harvest.store_capture(material, txids, username)
        wire_updated = store.upsert_wire(graphql_entries)
    except store.StoreError as e:
        out({"error": str(e)})
        raise SystemExit(1) from None
    out({"imported": name, "txids": len(txids), "wire_updated": wire_updated})



@cli.command()
def prime() -> None:
    """Print an agent briefing (markdown): commands, examples, error fixes.

    The one command that prints plain markdown instead of JSON.
    """
    from .prime import PRIME_MD

    print(PRIME_MD)


def main(argv: list[str] | None = None) -> int:
    try:
        cli(args=argv, standalone_mode=True)
    except click.exceptions.Abort:
        return 130
    return 0



@cli.command()
@click.argument("text")
@click.option("--reply-to", default=None, help="Tweet id to reply to.")
def post(text: str, reply_to: str | None) -> None:
    """Post a tweet. REAL action: public and permanent. Use with care."""
    invoke(lambda c: cores.post_tweet(c, text, reply_to=reply_to))



@cli.command()
@click.argument("tweet_id")
def like(tweet_id: str) -> None:
    """Like a tweet. REAL action: publicly visible, notifies the author."""
    invoke(lambda c: cores.like(c, tweet_id))


@cli.group()
def accounts() -> None:
    """Manage stored x.com accounts (credentials live in a local sqlite db)."""


@accounts.command("list")
def accounts_list() -> None:
    """Show stored screen names and when each was last refreshed."""
    out({"accounts": store.list_accounts()})


@accounts.command("default")
@click.argument("name", required=False)
def accounts_default(name: str | None) -> None:
    """Show or set the default account (used when --user is omitted)."""
    if name is None:
        out({"default": store.get_default()})
        return
    try:
        out({"default": store.set_default(name)})
    except store.StoreError as e:
        out({"error": str(e)})
        raise SystemExit(1) from None


@accounts.command()
@click.argument("name")
@click.confirmation_option(prompt="Remove this account's stored credentials?")
def remove(name: str) -> None:
    """Remove NAME's credentials from the local account db."""
    try:
        if not store.remove_account(name):
            stored = ", ".join(a["screen_name"] for a in store.list_accounts()) or "none stored"
            raise store.StoreError(f"no such account: {name} (stored: {stored})")
    except store.StoreError as e:
        out({"error": str(e)})
        raise SystemExit(1) from None
    out({"removed": name.lstrip("@").lower()})


if __name__ == "__main__":
    raise SystemExit(main())
