"""Service cores - shared by CLI and MCP server. Reads + the write actions."""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime

from .client import XApiError
from .parse import (
    collect_cursors,
    collect_tweets,
    dm_conversation_rows,
    dm_message_rows,
    notification_rows,
    user_row,
    xchat_conversation_rows,
)

DAY_MS = 86_400_000


async def badges(c) -> dict:
    data = await c.graphql("badges", {})
    result = data["data"]["viewer_v2"]["user_results"]["result"]
    return {"badges": result.get("badge_counts") or {}}


async def suggest(c, query: str, lang: str = "en") -> dict:
    data = await c.rest(
        "/1.1/search/typeahead.json", {"q": query, "src": "search_box", "lang": lang}
    )
    return {
        "topics": [t.get("topic") for t in data.get("topics") or []],
        "users": [
            {
                "id": u.get("id_str"),
                "screen_name": u.get("screen_name"),
                "name": u.get("name"),
            }
            for u in data.get("users") or []
        ],
    }


async def search(
    c, query: str, limit: int = 20, product: str = "Top", cursor: str | None = None
) -> dict:
    variables = {
        "rawQuery": query,
        "count": limit,
        "querySource": "typed_query",
        "product": product,
        "withGrokTranslatedBio": False,
        "withQuickPromoteEligibilityTweetFields": False,
    }
    if cursor:
        variables["cursor"] = cursor
    data = await c.graphql("search", variables)
    return {
        "tweets": collect_tweets(data)[:limit],
        "cursor": collect_cursors(data).get("bottom"),
    }


async def tweet(c, tweet_id: str, cursor: str | None = None) -> dict:
    variables = {
        "focalTweetId": str(tweet_id),
        "referrer": "search",
        "with_rux_injections": False,
        "rankingMode": "Relevance",
        "includePromotedContent": True,
        "withCommunity": True,
        "withQuickPromoteEligibilityTweetFields": True,
        "withBirdwatchNotes": True,
        "withVoice": True,
    }
    if cursor:
        variables["cursor"] = cursor
    data = await c.graphql("tweet", variables)
    tweets = collect_tweets(data)
    return {
        "tweet": tweets[0] if tweets else None,
        "replies": tweets[1:],
        "cursor": collect_cursors(data).get("bottom"),
    }


async def notifications(c, limit: int = 20, cursor: str | None = None) -> dict:
    variables = {"timeline_type": "All", "count": limit}
    if cursor:
        variables["cursor"] = cursor
    data = await c.graphql("notifications", variables)
    return {
        "notifications": notification_rows(data)[:limit],
        "cursor": collect_cursors(data).get("bottom"),
    }


def _analytics_variables(days: int) -> dict:
    now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    to_ms, span = int(now.timestamp() * 1000), days * DAY_MS
    prev_to_ms = to_ms - span

    def iso(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")

    return {
        "current_from": to_ms - span,
        "current_from_iso": iso(to_ms - span),
        "current_to": to_ms,
        "current_to_iso": now.isoformat().replace("+00:00", "Z"),
        "prev_from": prev_to_ms - span,
        "prev_from_iso": iso(prev_to_ms - span),
        "prev_to": prev_to_ms,
        "prev_to_iso": iso(prev_to_ms),
        "backfill_from": to_ms - 2 * DAY_MS,
        "backfill_to": to_ms,
        "show_realtime_active_followers": True,
        "show_verified_followers": True,
    }


def _viewer_id(encoded: str) -> str | None:
    try:
        decoded = base64.b64decode(encoded).decode()
        return decoded.split(":")[-1]
    except Exception:  # noqa: BLE001 - malformed id must not kill analytics
        return None


async def analytics(c, days: int = 7) -> dict:
    days = max(1, min(days, 28))
    data = await c.graphql("analytics", _analytics_variables(days))
    user_results = data["data"]["viewer_v2"]["user_results"]
    result = user_results.get("result") or {}
    series: dict[str, int] = {}
    for point in result.get("current_time_series") or []:
        kind = point.get("engagement_type")
        if kind:
            series[kind] = series.get(kind, 0) + (point.get("count") or 0)
    relationship = result.get("relationship_counts") or {}
    return {
        "viewer": {
            "id": _viewer_id(user_results.get("id") or ""),
            "followers": relationship.get("followers"),
            "verified_followers": result.get("verified_follower_count"),
            "realtime_active_followers": (result.get("realtime_active_followers") or {}).get(
                "active_count"
            ),
        },
        "series": dict(sorted(series.items())),
        "window_days": days,
    }


async def me(c) -> dict:
    return (await analytics(c, days=1))["viewer"]


async def following(
    c,
    user: str,
    limit: int = 20,
    cursor: str | None = None,
) -> dict:
    params: dict[str, str | int] = {"count": limit, "skip_status": "1", "cursor": cursor or "-1"}
    if str(user).isdigit():
        params["user_id"] = user
    else:
        params["screen_name"] = user
    data = await c.rest("/1.1/friends/following/list.json", params)
    users = [
        {
            "id": u.get("id_str"),
            "screen_name": u.get("screen_name"),
            "name": u.get("name"),
            "followers": u.get("followers_count"),
        }
        for u in data.get("users") or []
    ]
    next_cursor = data.get("next_cursor")
    return {"users": users, "cursor": str(next_cursor) if next_cursor else None}


async def like(c, tweet_id: str) -> dict:
    data = await c.graphql_post("like", {"tweet_id": str(tweet_id)})
    errors = data.get("errors") or []
    if errors:
        return {
            "liked": str(tweet_id),
            "result": "error",
            "detail": errors[0].get("message"),
        }
    return {"liked": str(tweet_id), "result": (data.get("data") or {}).get("favorite_tweet")}


async def user(c, screen_name: str) -> dict:
    name = str(screen_name).lstrip("@")
    data = await c.graphql("user", {"screen_name": name, "withGrokTranslatedBio": True})
    result = ((data.get("data") or {}).get("user") or {}).get("result")
    if not result:
        raise XApiError(f"user not found: {name}")
    return user_row(result)


async def home(c, limit: int = 20, cursor: str | None = None, tab: str = "for_you") -> dict:
    if tab == "following":
        key, variables = (
            "home_following",
            {
                "count": limit,
                "enableRanking": True,
                "includePromotedContent": True,
            },
        )
    else:
        key, variables = (
            "home",
            {
                "count": limit,
                "includePromotedContent": True,
                "withCommunity": True,
                "requestContext": "ptr",
            },
        )
    if cursor:
        variables["cursor"] = cursor
    data = await c.graphql_post(key, variables)
    return {
        "tab": tab,
        "tweets": collect_tweets(data)[:limit],
        "cursor": collect_cursors(data).get("bottom"),
    }


async def post_tweet(c, text: str, reply_to: str | None = None) -> dict:
    variables = {
        "tweet_text": text,
        "media": {"media_entities": [], "possibly_sensitive": False},
        "semantic_annotation_ids": [],
        "disallowed_reply_options": None,
        "semantic_annotation_options": {"source": "Unknown"},
    }
    if reply_to:
        variables["reply"] = {"in_reply_to_tweet_id": str(reply_to), "exclude_reply_user_ids": []}
    data = await c.graphql_post("create_tweet", variables)
    result = (data.get("data") or {}).get("create_tweet", {})
    tweet = (result.get("tweet_results") or {}).get("result") or {}
    errors = data.get("errors") or []
    if errors:
        return {"posted": False, "detail": errors[0].get("message")}
    return {
        "posted": True,
        "id": tweet.get("rest_id"),
        "text": (tweet.get("legacy") or {}).get("full_text"),
    }


DM_INBOX = "/1.1/dm/inbox_initial_state.json"
_CONVERSATION_ID = re.compile(r"^\d+-\d+$")
_XCHAT_SETTINGS = {
    "conversation_event_limit": 200,
    "inbox_conversation_event_limit": 5,
    "inbox_conversation_limit": 50,
    "user_event_limit": 500,
}
# client-storage version stamp echoed by the server; from the web client
_XCHAT_PULL_VERSION = 1788882958


async def dm_inbox(c, cursor: str | None = None) -> dict:
    """The DM inbox, paginated. First page: the 1.1 inbox - ~30 recent
    conversations WITH plaintext previews and unread flags. Deeper pages
    walk the XChat inbox (50/page, api.x.com GraphQL): every conversation
    ever, but previews are E2E-encrypted so rows carry metadata only.
    Cursor format: "<cursor_id>|<graph_snapshot_id>"; "0|" = first XChat page.
    """
    if not cursor:
        state = (await c.rest(DM_INBOX, {"supports_reactions": "true"})).get(
            "inbox_initial_state"
        ) or {}
        return {"conversations": dm_conversation_rows(state, c.config.screen_name), "cursor": "0|"}
    offset, _, snapshot = cursor.partition("|")
    if offset == "0":
        data = await c.graphql(
            "xchat_inbox",
            {
                "max_local_sequence_id": "1",
                "query_settings": _XCHAT_SETTINGS,
                "message_pull_version": _XCHAT_PULL_VERSION,
            },
        )
        page = (data.get("data") or {}).get("get_initial_chat_page") or {}
    else:
        data = await c.graphql(
            "xchat_inbox_page",
            {
                "continue_cursor": {
                    "cursor_id": offset,
                    "graph_snapshot_id": snapshot,
                    "graph_snapshot_restarted": False,
                },
                "query_settings": _XCHAT_SETTINGS,
            },
        )
        page = (data.get("data") or {}).get("get_inbox_page") or {}
    cur = page.get("inboxCursor") or {}
    nxt = None
    if cur.get("cursor_id") is not None and not cur.get("inbox_exhausted"):
        nxt = f"{cur['cursor_id']}|{cur.get('graph_snapshot_id') or ''}"
    return {"conversations": xchat_conversation_rows(page.get("items") or []), "cursor": nxt}


async def dm_conversation(c, conversation_id: str, cursor: str | None = None) -> dict:
    """One DM thread, oldest to newest. Accepts legacy "a-b" and XChat
    "a:b" conversation ids. cursor (a previous min_entry_id) pages into
    older history while the endpoint reports more."""
    cid = conversation_id.replace(":", "-")
    tl = (
        await c.rest(f"/1.1/dm/conversation/{cid}.json", {"max_id": cursor} if cursor else {})
    ).get("conversation_timeline") or {}
    return {
        "conversation": cid,
        "messages": dm_message_rows(tl),
        "cursor": tl.get("min_entry_id") if tl.get("status") not in ("AT_END", None) else None,
    }


async def dm_send(c, to: str, text: str) -> dict:
    """Send a DM. `to` is @screen_name or an explicit a-b conversation id.
    1:1 conversation ids are the two numeric user ids joined low-high."""
    name = to.lstrip("@").replace(":", "-")
    if _CONVERSATION_ID.match(name):
        cid = name
    else:
        cid = await _dm_conversation_id(c, name)
    data = await c.rest_post("/1.1/dm/new2.json", {"conversation_id": cid, "text": text})
    rows = dm_message_rows({"entries": data.get("entries") or [], "users": data.get("users") or {}})
    message = rows[-1] if rows else {"id": None, "time": None, "sender": None, "text": text}
    return {"sent": True, "conversation_id": cid, "to": name, "message": message}


async def _dm_conversation_id(c, name: str) -> str:
    """Resolve @name to a 1:1 conversation id: both sides' numeric ids come
    from the inbox users map; someone never chatted with resolves through
    the profile api."""
    state = (await c.rest(DM_INBOX, {"supports_reactions": "true"})).get(
        "inbox_initial_state"
    ) or {}
    users = state.get("users") or {}

    def find(screen_name: str):
        return next(
            (
                u
                for u in users.values()
                if str(u.get("screen_name", "")).lower() == screen_name.lower()
            ),
            None,
        )

    viewer = find(c.config.screen_name)
    if viewer is None:
        raise XApiError(
            f"dm-send: could not resolve your own user id from the inbox as @{c.config.screen_name}"
        )
    other = find(name)
    other_id = str(other.get("id_str")) if other else (await user(c, name)).get("id")
    if not other_id:
        raise XApiError(f"dm-send: no such user @{name}")
    low, high = sorted((str(viewer.get("id_str")), other_id), key=int)
    return f"{low}-{high}"
