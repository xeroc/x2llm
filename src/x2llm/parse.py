"""Parsers: x.com timeline/negotiation blobs -> flat LLM-friendly dicts."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def walk(obj: Any) -> Iterator[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def _tweet_result(wrapper: dict | None) -> dict | None:
    result = (wrapper or {}).get("result") or {}
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}
    if result.get("__typename") != "Tweet" or not result.get("rest_id"):
        return None
    return result


def _user_fields(user: dict) -> dict:
    core = user.get("core") or {}
    legacy = user.get("legacy") or {}
    return {
        "author": core.get("screen_name") or legacy.get("screen_name"),
        "author_name": core.get("name") or legacy.get("name"),
        "author_id": user.get("rest_id"),
    }


def tweet_row(result: dict) -> dict:
    legacy = result.get("legacy") or {}
    user = ((result.get("core") or {}).get("user_results") or {}).get("result") or {}
    note = ((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    views = (result.get("views") or {}).get("count")
    return {
        "id": result.get("rest_id"),
        "created_at": legacy.get("created_at"),
        **_user_fields(user),
        "text": note.get("text") or legacy.get("full_text"),
        "lang": legacy.get("lang"),
        "replies": legacy.get("reply_count"),
        "reposts": legacy.get("retweet_count"),
        "likes": legacy.get("favorite_count"),
        "views": int(views) if isinstance(views, str) and views.isdigit() else views,
        "bookmarks": legacy.get("bookmark_count"),
    }


def collect_tweets(obj: Any) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for node in walk(obj):
        if "tweet_results" not in node:
            continue
        result = _tweet_result(node["tweet_results"])
        if result and result["rest_id"] not in seen:
            seen.add(result["rest_id"])
            rows.append(tweet_row(result))
    return rows


def collect_cursors(obj: Any) -> dict[str, str]:
    cursors: dict[str, str] = {}
    for node in walk(obj):
        if node.get("entryType") == "TimelineTimelineCursor" and node.get("value"):
            kind = node.get("cursorType")
            if kind in ("Top", "Bottom"):
                cursors[kind.lower()] = node["value"]
    return cursors


def notification_rows(obj: Any) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for node in walk(obj):
        item = node.get("itemContent")
        if not isinstance(item, dict) or item.get("__typename") != "TimelineNotification":
            continue
        if item.get("id") in seen:
            continue
        seen.add(item.get("id"))
        rich = item.get("rich_message") or {}
        rows.append(
            {
                "id": item.get("id"),
                "icon": item.get("notification_icon"),
                "text": rich.get("text"),
                "url": (item.get("notification_url") or {}).get("url"),
            }
        )
    return rows


def user_row(result: dict) -> dict:
    core = result.get("core") or {}
    bio = result.get("profile_bio") or {}
    relationship = result.get("relationship_counts") or {}
    counts = result.get("tweet_counts") or {}
    urls = ((bio.get("entities") or {}).get("url") or {}).get("urls") or []
    return {
        "id": result.get("rest_id"),
        "screen_name": core.get("screen_name"),
        "name": core.get("name"),
        "created_at": core.get("created_at"),
        "bio": bio.get("description"),
        "url": urls[0].get("expanded_url") if urls else None,
        "location": (result.get("location") or {}).get("location"),
        "followers": relationship.get("followers"),
        "following": relationship.get("following"),
        "tweets": counts.get("tweets"),
        "media_tweets": counts.get("media_tweets"),
        "blue_verified": result.get("is_blue_verified"),
    }
