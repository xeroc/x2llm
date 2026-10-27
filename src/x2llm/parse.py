"""Parsers: x.com timeline/negotiation blobs -> flat LLM-friendly dicts."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
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


_HEADERS = {"header-one": "# ", "header-two": "## ", "header-three": "### "}


def _iso(secs: Any) -> str | None:
    if isinstance(secs, (int, float)):
        return datetime.fromtimestamp(secs, tz=UTC).isoformat()
    return None


def _entity_values(entity_map: Any) -> list[dict]:
    """x.com ships the Draft.js entityMap as an array of {key, value} pairs;
    a block's entityRanges.key indexes that array positionally."""
    if isinstance(entity_map, list):
        return [e.get("value") or {} for e in entity_map]
    if isinstance(entity_map, dict):
        return list(entity_map.values())
    return []


def _render_blocks(blocks: list[dict], entities: list[dict], media: dict) -> str:
    lines: list[str] = []
    ordered = 0
    for block in blocks:
        btype = block.get("type") or "unstyled"
        text = (block.get("text") or "").strip()
        if btype == "atomic":
            key = ((block.get("entityRanges") or [{}])[0] or {}).get("key")
            entity = entities[key] if isinstance(key, int) and 0 <= key < len(entities) else {}
            data = entity.get("data") or {}
            if entity.get("type") == "DIVIDER":
                lines.append("---")
            else:
                media_id = str(((data.get("mediaItems") or [{}])[0] or {}).get("mediaId") or "")
                url = media.get(media_id) or data.get("url")
                if url:
                    lines.append(f"![image]({url})")
            ordered = 0
            continue
        ordered = ordered + 1 if btype == "ordered-list-item" and text else 0
        if btype in _HEADERS:
            lines.append(_HEADERS[btype] + text)
        elif btype == "unordered-list-item" and text:
            lines.append("- " + text)
        elif btype == "ordered-list-item" and text:
            lines.append(f"{ordered}. " + text)
        elif btype == "blockquote":
            lines.append("> " + text if text else ">")
        elif btype == "code-block":
            lines.append(f"```\n{text}\n```")
        else:
            lines.append(text)
    return "\n\n".join(lines)


def article_row(article: dict | None) -> dict | None:
    """Long-form X article attached to a tweet -> flat dict with the body
    rendered to markdown (headers, images as links, dividers)."""
    result = ((article or {}).get("article_results") or {}).get("result") or {}
    if not result:
        return None
    state = result.get("content_state") or {}
    media = {
        str(m.get("media_id")): (m.get("media_info") or {}).get("original_img_url")
        for m in result.get("media_entities") or []
    }
    row = {
        "id": result.get("rest_id"),
        "title": result.get("title"),
        "preview_text": result.get("preview_text"),
        "summary_text": result.get("summary_text"),
        "published_at": _iso((result.get("metadata") or {}).get("first_published_at_secs")),
        "modified_at": _iso((result.get("lifecycle_state") or {}).get("modified_at_secs")),
        "cover_url": ((result.get("cover_media") or {}).get("media_info") or {}).get("original_img_url"),
        "text": _render_blocks(state.get("blocks") or [], _entity_values(state.get("entityMap")), media),
    }
    return {k: v for k, v in row.items() if v}


def tweet_row(result: dict) -> dict:
    legacy = result.get("legacy") or {}
    user = ((result.get("core") or {}).get("user_results") or {}).get("result") or {}
    note = ((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    views = (result.get("views") or {}).get("count")
    row = {
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
    article = article_row(result.get("article"))
    if article:
        row["article"] = article
    return row


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
