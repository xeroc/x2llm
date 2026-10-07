"""Stdio MCP server exposing the same cores as the CLI (reads + like_tweet)."""
from __future__ import annotations

import asyncio
import inspect

from . import cores
from .client import XApiError, XClient
from .config import Config, ConfigError

MCP_INSTRUCTIONS = (
    "Access to a logged-in x.com (Twitter) account via the private web API. "
    "Credentials come from X_AUTH_TOKEN/X_CT0 environment variables - if they "
    "expire every tool returns an auth error. All tools are read-only except "
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
        ("One post by id plus its reply thread; cursor paginates deeper replies."),
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

_MCP_CLIENT: XClient | None = None
_MCP_LOCK = asyncio.Lock()


async def mcp_client() -> XClient:
    global _MCP_CLIENT
    async with _MCP_LOCK:
        if _MCP_CLIENT is None:
            try:
                _MCP_CLIENT = XClient(Config.from_env())
            except ConfigError as e:
                raise RuntimeError(str(e)) from e
        return _MCP_CLIENT


def bind_tool(fn):
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())[1:]

    async def bound(**kwargs):
        from mcp.server.mcpserver.exceptions import ToolError

        try:
            return await fn(await mcp_client(), **kwargs)
        except XApiError as e:
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
            description=description,
            annotations=ToolAnnotations(read_only_hint=read_only),
        )
    return server


async def serve() -> None:
    server = build_mcp_server()
    try:
        await server.run_stdio_async()
    finally:
        if _MCP_CLIENT is not None:
            await _MCP_CLIENT.aclose()
