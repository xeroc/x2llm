"""Stdio MCP server exposing the same cores as the CLI (reads + like_tweet).

Account selection is per tool call via the `username` parameter (omitted =
the default account); the `accounts` tool lists what is stored.
"""
from __future__ import annotations

import asyncio
import inspect

from . import cores
from .client import XApiError, XClient
from .config import Config, ConfigError, visible_accounts

MCP_INSTRUCTIONS = (
    "Access to logged-in x.com (Twitter) accounts via the private web API. "
    "Accounts come from a local store or from env-injected credentials "
    "(X_AUTH_TOKEN/X_CT0): call the `accounts` tool to list what is "
    "available, then pass `username` to any tool to pick one (omitted "
    "selects the default account - the accounts tool marks it). "
    "Credentials expire - refresh them with a HAR capture or new env "
    "values (see the x2llm README). All tools are read-only except "
    "like_tweet, which performs a REAL, publicly visible action and notifies "
    "the author - never call it unless the user explicitly asked to like that "
    "specific post. search supports X operators (from:user, "
    "since:YYYY-MM-DD, lang:, min_faves:, min_retweets:). get_tweet returns "
    "the tweet plus its reply thread; paginate long threads and searches with "
    "the returned cursor. analytics returns the account's own engagement "
    "statistics. If search or like fails with an empty 404, the captured "
    "x-client-transaction-id expired - see the x2llm README."
)

# (tool name, description, core, read_only)
MCP_TOOLS = [
    (
        "get_home_timeline",
        (
            "The account's home timeline. tab: 'for_you' (algorithmic) or "
            "'following' (chronological). Scroll deeper by passing the "
            "returned cursor."
        ),
        cores.home,
        True,
    ),
    (
        "get_user",
        (
            "Profile of a user by screen name: bio, url, follower/following "
            "counts, tweet counts, verification. Use to qualify authors "
            "before engaging."
        ),
        cores.user,
        True,
    ),
    (
        "get_badges",
        ("Unread notification and DM counts. Cheapest way to verify credentials work."),
        cores.badges,
        True,
    ),
    (
        "me",
        (
            "Viewer identity: numeric user id, follower, verified-follower and "
            "realtime-active-follower counts."
        ),
        cores.me,
        True,
    ),
    (
        "suggest",
        ("Search typeahead for a prefix: matching topics and users (with ids)."),
        cores.suggest,
        True,
    ),
    (
        "search",
        (
            "Search posts. Supports X operators: from:user, since:YYYY-MM-DD, "
            "lang:, min_faves:, min_retweets:. Returns posts and a cursor for the "
            "next page."
        ),
        cores.search,
        True,
    ),
    (
        "get_tweet",
        ("One post by id plus its reply thread; cursor paginates deeper replies. Long-form articles come back under article (title, summary, full body as markdown)."),
        cores.tweet,
        True,
    ),
    (
        "get_notifications",
        ("The account's notification tab (mentions, likes, follows, reposts)."),
        cores.notifications,
        True,
    ),
    (
        "get_analytics",
        (
            "The account's own engagement statistics aggregated over a window "
            "(impressions, likes, reposts, follows, ...). days: 1-28."
        ),
        cores.analytics,
        True,
    ),
    (
        "get_following",
        (
            "Accounts a user follows. user is a numeric id or screen name; cursor paginates."
        ),
        cores.following,
        True,
    ),
    (
        "post_tweet",
        (
            "Post a tweet as the logged-in user. REAL action: public, "
            "permanent, visible to everyone. Never call unless the user "
            "explicitly provided the exact text and asked for it to be posted."
        ),
        cores.post_tweet,
        False,
    ),
    (
        "like_tweet",
        (
            "Like a post as the logged-in user. REAL action: publicly visible, "
            "notifies the author, not undoable via this tool. Only call when "
            "the user explicitly asked to like that specific post."
        ),
        cores.like,
        False,
    ),
]

_MCP_CLIENTS: dict[str, XClient] = {}
_MCP_LOCK = asyncio.Lock()

_USERNAME_PARAM = inspect.Parameter(
    "username", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None, annotation="str | None"
)
_USERNAME_HELP = (
    " username: stored account to act as - omit to use the only stored "
    "account; see the accounts tool."
)


async def mcp_client(username: str | None = None) -> XClient:
    """One cached XClient per account; rebuilt when a refresh changes its row."""
    config = Config.load(username)
    async with _MCP_LOCK:
        client = _MCP_CLIENTS.get(config.screen_name)
        if client is None or client.config != config:
            client = XClient(config)
            _MCP_CLIENTS[config.screen_name] = client
        return client


async def _list_accounts() -> dict:
    return {"accounts": visible_accounts()}


def bind_tool(fn):
    sig = inspect.signature(fn)
    params = [*list(sig.parameters.values())[1:], _USERNAME_PARAM]

    async def bound(username=None, **kwargs):
        from mcp.server.mcpserver.exceptions import ToolError

        try:
            return await fn(await mcp_client(username), **kwargs)
        except (ConfigError, XApiError) as e:
            raise ToolError(str(e)) from e
        except TypeError as e:
            raise ToolError(f"bad arguments: {e}") from e

    bound.__signature__ = inspect.Signature(params, return_annotation=dict)
    return bound


def build_mcp_server():
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer(name="x2llm", instructions=MCP_INSTRUCTIONS)
    for name, description, fn, read_only in MCP_TOOLS:
        server.add_tool(
            bind_tool(fn),
            name=name,
            description=description + _USERNAME_HELP,
            annotations=ToolAnnotations(read_only_hint=read_only),
        )
    server.add_tool(
        _list_accounts,
        name="accounts",
        description=(
            "List available x.com accounts (screen name + freshness; never "
            "credentials). Env-injected accounts are marked source=env. "
            "Pass a tool's username parameter to select one."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    return server


async def serve() -> None:
    server = build_mcp_server()
    try:
        await server.run_stdio_async()
    finally:
        for client in _MCP_CLIENTS.values():
            await client.aclose()
        _MCP_CLIENTS.clear()
