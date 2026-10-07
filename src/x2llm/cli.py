"""Click CLI. Every command prints one JSON document; errors print {"error": ...} and exit 1."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import click

from . import cores
from .client import XApiError, XClient
from .config import Config, ConfigError


def out(obj) -> None:
    print(json.dumps(obj, default=str, ensure_ascii=False, indent=1))


async def _run(make_coro: Callable[[XClient], Awaitable]) -> object:
    config = Config.from_env()
    client = XClient(config)
    try:
        return await make_coro(client)
    finally:
        await client.aclose()


def invoke(make_coro) -> None:
    try:
        result = asyncio.run(_run(make_coro))
    except (ConfigError, XApiError) as e:
        out({"error": str(e)})
        raise SystemExit(1) from None
    out(result)


@click.group()
@click.version_option()
def cli() -> None:
    """x2llm - read-only x.com (Twitter) API client for agents. JSON out."""


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


if __name__ == "__main__":
    raise SystemExit(main())
