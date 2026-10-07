"""Service cores - shared by CLI and MCP server. Reads + the like mutation."""
from __future__ import annotations

import base64
from datetime import UTC, datetime

from .client import XApiError
from .parse import collect_cursors, collect_tweets, notification_rows, user_row

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
        key, variables = "home_following", {
            "count": limit,
            "enableRanking": True,
            "includePromotedContent": True,
        }
    else:
        key, variables = "home", {
            "count": limit,
            "includePromotedContent": True,
            "withCommunity": True,
            "requestContext": "ptr",
        }
    if cursor:
        variables["cursor"] = cursor
    data = await c.graphql_post(key, variables)
    return {
        "tab": tab,
        "tweets": collect_tweets(data)[:limit],
        "cursor": collect_cursors(data).get("bottom"),
    }
