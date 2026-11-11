"""Offline tests: store/config, RPC encapsulation (httpx.MockTransport), parsers,
cores (FakeClient), CLI (CliRunner), HAR generator and one stdio MCP
roundtrip. No test touches the network or the real account db.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import subprocess
import sys
import types
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from x2llm import cli, cores, parse
from x2llm.client import XApiError, XClient
from x2llm.config import Config, ConfigError

# --- store (sqlite account db) ------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from x2llm import store

    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "accounts.db")
    return store


def _seed(db, name="fabian", **kw):
    db.upsert_account(
        screen_name=name,
        auth_token=kw.get("auth_token", "at"),
        ct0=kw.get("ct0", "ct"),
        bearer=kw.get("bearer", "Bearer BB"),
        cookie=kw.get("cookie", "auth_token=at; ct0=ct"),
        user_agent=kw.get("user_agent", "UA"),
        txids=kw.get("txids", {"search": "STX"}),
        txids_updated=kw.get("txids_updated", datetime.now(UTC).isoformat(timespec="seconds")),
    )


def test_store_roundtrip_and_case_folding(db):
    _seed(db, "Fabian")
    row = db.get_account("FABIAN")
    assert row["screen_name"] == "fabian"
    assert row["auth_token"] == "at" and row["ct0"] == "ct"
    assert row["bearer"] == "Bearer BB" and row["cookie"] == "auth_token=at; ct0=ct"
    assert row["user_agent"] == "UA" and row["txids"] == {"search": "STX"}
    assert row["updated_at"]


def test_store_upsert_updates_in_place(db):
    _seed(db, auth_token="old")
    _seed(db, auth_token="new")
    assert db.get_account("fabian")["auth_token"] == "new"
    assert len(db.list_accounts()) == 1


def test_store_perms(db):
    import stat

    _seed(db)
    mode = db.db_path().stat().st_mode
    assert stat.S_IMODE(mode) == 0o600
    assert stat.S_IMODE(db.db_path().parent.stat().st_mode) == 0o700


def test_store_rejects_bad_txids(db):
    with pytest.raises(db.StoreError, match="txids"):
        db.upsert_account(screen_name="x", auth_token="a", ct0="c", txids={"k": 1})


def test_store_list_and_remove(db):
    _seed(db, "a")
    _seed(db, "b")
    assert [a["screen_name"] for a in db.list_accounts()] == ["a", "b"]
    assert db.list_accounts()[0]["updated_at"]
    assert db.remove_account("A") is True
    assert db.remove_account("a") is False
    assert [a["screen_name"] for a in db.list_accounts()] == ["b"]


def test_store_resolve_none(db):
    with pytest.raises(db.StoreError, match="x2llm import"):
        db.resolve(None)
    _seed(db)
    assert db.resolve(None) == "fabian"  # first import is the default
    _seed(db, "other")
    assert db.resolve(None) == "fabian"  # ...and keeps it


def test_store_resolve_named(db):
    _seed(db, "Fabian")
    assert db.resolve("@fabian") == "fabian"


def test_first_import_becomes_default(db):
    assert db.get_default() is None
    _seed(db, "fabian")
    assert db.get_default() == "fabian"
    _seed(db, "other")
    assert db.get_default() == "fabian"  # second import does not steal it


def test_set_default_validates_and_case_folds(db):
    _seed(db, "Fabian")
    _seed(db, "other")
    db.set_default("FABIAN")
    assert db.get_default() == "fabian"
    assert db.resolve(None) == "fabian"
    with pytest.raises(db.StoreError, match="no such account: ghost"):
        db.set_default("ghost")


def test_resolve_none_without_default_points_at_cli(db):
    _seed(db, "a")
    _seed(db, "b")
    db.set_meta("default_account", "")  # simulate a dangling/removed default
    with pytest.raises(db.StoreError, match="accounts default"):
        db.resolve(None)


def test_remove_default_clears_it(db):
    _seed(db, "a")
    _seed(db, "b")
    db.set_default("a")
    assert db.remove_account("a") is True
    assert db.get_default() is None
    with pytest.raises(db.StoreError, match="accounts default"):
        db.resolve(None)
    db.set_default("b")
    assert db.resolve(None) == "b"


def test_list_accounts_marks_default(db):
    _seed(db, "a")
    _seed(db, "b")
    rows = {r["screen_name"]: r for r in db.list_accounts()}
    assert rows["a"]["default"] is True and rows["b"]["default"] is False


def test_wire_roundtrip_and_override(db):
    db.upsert_wire({"search": {"query_id": "NEW", "name": "SearchTimeline", "method": "get"}})
    wire = db.get_wire()
    assert wire["search"]["query_id"] == "NEW"
    db.upsert_wire({"search": {"query_id": "NEWER", "name": "SearchTimeline", "method": "get"}})
    assert db.get_wire()["search"]["query_id"] == "NEWER"


def test_wire_rejects_garbage(db):
    with pytest.raises(db.StoreError, match="wire"):
        db.upsert_wire({"search": {"no_query_id": True}})


def mkconfig(**over) -> Config:
    base: dict = {
        "screen_name": "t",
        "auth_token": "at",
        "ct0": "ct",
        "cookie": None,
        "bearer": "Bearer A",
        "user_agent": "UA",
        "api_base": "https://x.com/i/api",
        "timeout": 20.0,
        "rate_per_min": 0,
        "txids": {"search": "STX", "like": "LTX"},
    }
    base.update(over)
    return Config(**base)


def test_config_load_reads_db_and_env_knobs(db):
    _seed(db, txids={"search": "STX"})
    cfg = Config.load(
        "fabian", env={"X_API_BASE": "http://x", "X_TIMEOUT": "5", "X_RATE_LIMIT": "0"}
    )
    assert cfg.screen_name == "fabian"
    assert cfg.auth_token == "at" and cfg.ct0 == "ct" and cfg.txids == {"search": "STX"}
    assert cfg.api_base == "http://x" and cfg.timeout == 5.0 and cfg.rate_per_min == 0
    assert cfg.cookie_header == "auth_token=at; ct0=ct"


def test_config_load_defaults_and_bearer_fallback(db):
    from x2llm.registry import BEARER

    _seed(db, bearer="", cookie="", user_agent="")
    cfg = Config.load(None)
    assert cfg.bearer == BEARER and cfg.user_agent
    assert cfg.api_base == "https://x.com/i/api"
    assert cfg.timeout == 20 and cfg.rate_per_min == 30


def test_config_load_cookie_override(db):
    _seed(db, cookie="auth_token=at; ct0=ct; cf_clearance=x")
    assert Config.load("fabian").cookie_header.startswith("auth_token=at; ct0=ct; cf_clearance=")


def test_config_unknown_account_lists_stored(db):
    _seed(db, "fabian")
    with pytest.raises(ConfigError, match="no such account: nobody.*fabian"):
        Config.load("nobody")


def test_config_drops_stale_txid_pool(db):
    import sqlite3

    now = datetime.now(UTC).isoformat(timespec="seconds")
    _seed(db, txids={"search": "STX"}, txids_updated=now)
    assert Config.load("fabian").txids == {"search": "STX"}  # fresh capture keeps them
    with sqlite3.connect(db.db_path()) as conn:
        conn.execute("UPDATE accounts SET txids_updated = '2020-01-01T00:00:00+00:00'")
    assert Config.load("fabian").txids == {}  # stale pool -> txgen takes over


def test_config_empty_store_points_at_refresh(db):
    with pytest.raises(ConfigError, match="x2llm import"):
        Config.load(None)


# --- config: env-injected credentials (storeless MCP deployments) --------------------

_ENV_CREDS = {"X_AUTH_TOKEN": "at", "X_CT0": "ct", "X_ACCOUNT": "Fabian"}


def test_config_env_injection_skips_store(monkeypatch, tmp_path):
    from x2llm import store
    from x2llm.registry import BEARER

    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "accounts.db")
    cfg = Config.load(None, env=_ENV_CREDS)
    assert cfg.screen_name == "fabian"  # X_ACCOUNT gets canonicalized
    assert cfg.auth_token == "at" and cfg.ct0 == "ct"
    assert cfg.bearer == BEARER and cfg.user_agent  # registry defaults
    assert cfg.txids == {}  # no capture pool - txgen mints on demand
    assert not (tmp_path / "accounts.db").exists()  # the store is never touched


def test_config_env_requires_both_cookies(monkeypatch, tmp_path):
    from x2llm import store

    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "accounts.db")
    with pytest.raises(ConfigError, match="X_AUTH_TOKEN and X_CT0 must be set together"):
        Config.load(None, env={"X_AUTH_TOKEN": "at"})
    with pytest.raises(ConfigError, match="must be set together"):
        Config.load(None, env={"X_CT0": "ct"})
    assert not (tmp_path / "accounts.db").exists()


def test_config_env_username_gate(monkeypatch, tmp_path):
    from x2llm import store

    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "accounts.db")
    with pytest.raises(ConfigError, match="env credentials configure"):
        Config.load("other", env=_ENV_CREDS)
    assert Config.load("@Fabian", env=_ENV_CREDS).screen_name == "fabian"


def test_config_env_overrides_cookie_bearer_ua():
    cfg = Config.load(
        None,
        env={
            "X_AUTH_TOKEN": "at",
            "X_CT0": "ct",
            "X_COOKIE": "auth_token=at; ct0=ct; extra=1",
            "X_BEARER": "Bearer BB",
            "X_USER_AGENT": "UA/1",
            "X_TIMEOUT": "5",
        },
    )
    assert cfg.cookie == "auth_token=at; ct0=ct; extra=1"
    assert cfg.bearer == "Bearer BB" and cfg.user_agent == "UA/1"
    assert cfg.screen_name == "env" and cfg.timeout == 5.0  # X_ACCOUNT omitted


def test_visible_accounts_env_beats_store(monkeypatch, db):
    from x2llm.config import visible_accounts

    _seed(db, "stored")
    assert [a["screen_name"] for a in visible_accounts()] == ["stored"]
    monkeypatch.setenv("X_AUTH_TOKEN", "at")
    monkeypatch.setenv("X_CT0", "ct")
    monkeypatch.setenv("X_ACCOUNT", "fabian")
    assert visible_accounts() == [
        {"screen_name": "fabian", "updated_at": None, "default": True, "source": "env"}
    ]


def test_mcp_accounts_tool_lists_env_account(monkeypatch):
    from x2llm import mcp

    monkeypatch.setenv("X_AUTH_TOKEN", "at")
    monkeypatch.setenv("X_CT0", "ct")
    assert asyncio.run(mcp._list_accounts()) == {
        "accounts": [{"screen_name": "env", "updated_at": None, "default": True, "source": "env"}]
    }


def test_cli_accounts_list_shows_env_account(monkeypatch):
    monkeypatch.setenv("X_AUTH_TOKEN", "at")
    monkeypatch.setenv("X_CT0", "ct")
    result = CliRunner().invoke(cli.cli, ["accounts", "list"])
    assert result.exit_code == 0
    assert json.loads(result.output)["accounts"][0]["screen_name"] == "env"


def test_store_get_wire_does_not_create_db(db):
    assert db.get_wire() == {}
    assert not db.db_path().exists()


def test_store_pg_missing_driver_is_store_error(db, monkeypatch):
    """DATABASE_URL set but psycopg not importable -> actionable StoreError."""
    import sys

    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/x")
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(db.StoreError, match=r"x2llm\[postgres\]"):
        db._connect()


# --- client / RPC layer --------------------------------------------------------


def test_client_graphql_builds_request_and_headers(db):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": {}})

    client = XClient(mkconfig(), transport=httpx.MockTransport(handler))
    from x2llm.registry import GRAPHQL

    ep = GRAPHQL["search"]
    result = _run(client.graphql("search", {"rawQuery": "q", "count": 5}))
    assert result == {"data": {}}
    assert ep["query_id"] in seen["url"] and ep["name"] in seen["url"]
    query = urllib.parse.urlparse(seen["url"]).query
    params = dict(p.split("=", 1) for p in query.split("&"))
    variables = json.loads(urllib.parse.unquote(params["variables"]))
    assert variables == {"rawQuery": "q", "count": 5}
    assert json.loads(urllib.parse.unquote(params["features"]))
    h = {k.lower(): v for k, v in seen["headers"].items()}
    assert h["authorization"].startswith("Bearer A")
    assert h["cookie"] == "auth_token=at; ct0=ct"
    assert h["x-csrf-token"] == "ct"
    assert h["x-twitter-auth-type"] == "OAuth2Session"
    assert h["x-client-transaction-id"] == "STX"


def test_client_empty_404_hints_at_txid_expiry(db):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"")

    client = XClient(mkconfig(txids={}), transport=httpx.MockTransport(handler))
    with pytest.raises(XApiError, match="transaction-id expired"):
        _run(client.graphql("search", {}))


def test_client_403_hints_at_credentials():
    client = XClient(
        mkconfig(txids={}), transport=httpx.MockTransport(lambda r: httpx.Response(403, json={}))
    )
    with pytest.raises(XApiError, match="credentials rejected"):
        _run(client.rest("/1.1/search/typeahead.json", {"q": "x"}))


def test_client_mutation_posts_json_body_with_txid(db):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": {"favorite_tweet": "Done"}})

    client = XClient(mkconfig(), transport=httpx.MockTransport(handler))
    from x2llm.registry import GRAPHQL

    ep = GRAPHQL["like"]
    result = _run(client.graphql_post("like", {"tweet_id": "42"}))
    assert result == {"data": {"favorite_tweet": "Done"}}
    assert seen["method"] == "POST"
    assert seen["url"].endswith(f"/graphql/{ep['query_id']}/{ep['name']}")
    assert seen["body"] == {"variables": {"tweet_id": "42"}, "queryId": ep["query_id"]}
    h = {k.lower(): v for k, v in seen["headers"].items()}
    assert h["content-type"] == "application/json"
    assert h["x-csrf-token"] == "ct"
    assert h["x-client-transaction-id"] == "LTX"


def test_client_wire_overrides_registry(db):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": {}})

    db.upsert_wire({"search": {"query_id": "DBQID", "name": "SearchTimeline", "method": "get"}})
    client = XClient(mkconfig(), transport=httpx.MockTransport(handler))
    _run(client.graphql("search", {}))
    assert "DBQID" in seen["url"]
    from x2llm.registry import GRAPHQL

    assert client._op("tweet") == GRAPHQL["tweet"]


# --- parsers --------------------------------------------------------------------


def tweet_payload(tid="1", text="hi", screen_name="a", **legacy):
    return {
        "tweet_results": {
            "result": {
                "__typename": "Tweet",
                "rest_id": tid,
                "core": {
                    "user_results": {
                        "result": {
                            "rest_id": "9",
                            "core": {"screen_name": screen_name, "name": "A Name"},
                        }
                    }
                },
                "views": {"count": "42"},
            }
        }
    }


def test_tweet_row_renders_article_body():
    wrapper = tweet_payload(tid="6")
    wrapper["tweet_results"]["result"]["article"] = {
        "article_results": {
            "result": {
                "rest_id": "2105746476039127040",
                "title": "It's ORE's Time",
                "summary_text": "- one\n- two",
                "metadata": {"first_published_at_secs": 0},
                "lifecycle_state": {"modified_at_secs": 60},
                "cover_media": {"media_info": {"original_img_url": "https://pbs/cover.jpg"}},
                "media_entities": [
                    {"media_id": "14", "media_info": {"original_img_url": "https://pbs/a.jpg"}}
                ],
                "content_state": {
                    "blocks": [
                        {"type": "header-one", "text": "H1"},
                        {"type": "unstyled", "text": "Body."},
                        {
                            "type": "atomic",
                            "text": " ",
                            "entityRanges": [{"key": 0, "offset": 0, "length": 1}],
                        },
                        {
                            "type": "atomic",
                            "text": " ",
                            "entityRanges": [{"key": 1, "offset": 0, "length": 1}],
                        },
                    ],
                    "entityMap": [
                        {
                            "key": "8",
                            "value": {
                                "type": "MEDIA",
                                "data": {"mediaItems": [{"mediaId": "14"}]},
                            },
                        },
                        {"key": "9", "value": {"type": "DIVIDER", "data": {}}},
                    ],
                },
            }
        }
    }
    art = parse.collect_tweets(wrapper)[0]["article"]
    assert art["id"] == "2105746476039127040"
    assert art["title"] == "It's ORE's Time"
    assert art["published_at"] == "1970-01-01T00:00:00+00:00"
    assert art["cover_url"] == "https://pbs/cover.jpg"
    assert art["text"] == ("# H1\n\nBody.\n\n![image](https://pbs/a.jpg)\n\n---")


def test_tweet_row_has_no_article_key_for_plain_tweets():
    assert "article" not in parse.collect_tweets(tweet_payload())[0]


def test_tweet_row_prefers_note_tweet_text():
    payload = tweet_payload()
    payload["tweet_results"]["result"]["note_tweet"] = {
        "note_tweet_results": {"result": {"text": "the full long text"}}
    }
    assert parse.collect_tweets(payload)[0]["text"] == "the full long text"


def test_collect_tweets_unwraps_visibility_and_dedups():
    inner = tweet_payload(tid="7")["tweet_results"]["result"]
    wrapped = {
        "tweet_results": {"result": {"__typename": "TweetWithVisibilityResults", "tweet": inner}}
    }
    doc = {"a": wrapped, "b": tweet_payload(tid="7"), "c": tweet_payload(tid="8")}
    rows = parse.collect_tweets(doc)
    assert [r["id"] for r in rows] == ["7", "8"]
    assert rows[0]["views"] == 42


def test_collect_cursors():
    doc = {
        "entries": [
            {
                "content": {
                    "entryType": "TimelineTimelineCursor",
                    "cursorType": "Bottom",
                    "value": "B",
                }
            },
            {"content": {"entryType": "TimelineTimelineCursor", "cursorType": "Top", "value": "T"}},
        ]
    }
    assert parse.collect_cursors(doc) == {"top": "T", "bottom": "B"}


def test_notification_rows_dedup_and_shape():
    item = {
        "itemContent": {
            "__typename": "TimelineNotification",
            "id": "n1",
            "notification_icon": "heart_icon",
            "rich_message": {"text": "x liked your post"},
            "notification_url": {"url": "/status/1"},
        }
    }
    rows = parse.notification_rows({"a": item, "b": item})
    assert rows == [
        {"id": "n1", "icon": "heart_icon", "text": "x liked your post", "url": "/status/1"}
    ]


# --- cores (FakeClient) ----------------------------------------------------------


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def graphql(self, key, variables):
        self.calls.append(("graphql", key, variables))
        return self.response

    async def graphql_post(self, key, variables):
        self.calls.append(
            ("mutation" if key in ("like", "create_tweet") else "post", key, variables)
        )
        return self.response

    async def rest(self, path, params):
        self.calls.append(("rest", path, params))
        return self.response


def test_search_core_truncates_and_returns_cursor():
    response = {
        "data": {
            "list": [tweet_payload(tid=str(i)) for i in range(5)],
            "cursor": {
                "content": {
                    "entryType": "TimelineTimelineCursor",
                    "cursorType": "Bottom",
                    "value": "C",
                }
            },
        }
    }
    fake = FakeClient(response)
    result = _run(cores.search(fake, "python", limit=3))
    assert [t["id"] for t in result["tweets"]] == ["0", "1", "2"]
    assert result["cursor"] == "C"
    kind, key, variables = fake.calls[0]
    assert (kind, key) == ("graphql", "search")
    assert variables["rawQuery"] == "python" and variables["count"] == 3


def test_analytics_core_aggregates_series_and_decodes_viewer():
    response = {
        "data": {
            "viewer_v2": {
                "user_results": {
                    "id": "VXNlclJlc3VsdHM6MTU5MDQ0NTM=",
                    "result": {
                        "current_time_series": [
                            {"engagement_type": "Displayed", "count": 10, "timestamp": 1},
                            {"engagement_type": "Displayed", "count": 5, "timestamp": 2},
                            {"engagement_type": "Fav", "count": 2, "timestamp": 2},
                        ],
                        "relationship_counts": {"followers": 817},
                        "verified_follower_count": "347",
                        "realtime_active_followers": {"active_count": 567},
                    },
                }
            }
        }
    }
    result = _run(cores.analytics(FakeClient(response), days=7))
    assert result["series"] == {"Displayed": 15, "Fav": 2}
    assert result["viewer"]["id"] == "15904453"
    assert result["viewer"]["followers"] == 817
    assert result["window_days"] == 7


def test_following_core_numeric_vs_name_and_cursor_end():
    fake = FakeClient(
        {"users": [{"id_str": "2", "screen_name": "b", "name": "B"}], "next_cursor": 0}
    )
    result = _run(cores.following(fake, "123", limit=1))
    assert fake.calls[0][2]["user_id"] == "123"
    assert result["cursor"] is None
    _run(cores.following(fake, "jack"))
    assert fake.calls[1][2]["screen_name"] == "jack"


def test_suggest_core_shape():
    fake = FakeClient(
        {
            "topics": [{"topic": "bitcoin"}],
            "users": [{"id_str": "1", "screen_name": "a", "name": "A"}],
        }
    )
    assert _run(cores.suggest(fake, "bit")) == {
        "topics": ["bitcoin"],
        "users": [{"id": "1", "screen_name": "a", "name": "A"}],
    }


def test_user_core_maps_profile_and_strips_at():
    response = {
        "data": {
            "user": {
                "result": {
                    "rest_id": "7",
                    "is_blue_verified": True,
                    "core": {
                        "screen_name": "a",
                        "name": "A",
                        "created_at": "Fri Sep 25 12:59:34 +0000 2026",
                    },
                    "profile_bio": {
                        "description": "builder",
                        "entities": {"url": {"urls": [{"expanded_url": "https://example.com"}]}},
                    },
                    "location": {"location": "Berlin"},
                    "relationship_counts": {"followers": 10, "following": 5},
                    "tweet_counts": {"tweets": 100, "media_tweets": 4},
                }
            }
        }
    }
    fake = FakeClient(response)
    row = _run(cores.user(fake, "@a"))
    kind, key, variables = fake.calls[0]
    assert (kind, key) == ("graphql", "user")
    assert variables == {"screen_name": "a", "withGrokTranslatedBio": True}
    assert (
        row["screen_name"] == "a" and row["followers"] == 10 and row["url"] == "https://example.com"
    )


def test_user_core_not_found_raises():
    fake = FakeClient({"data": {"user": None}})
    with pytest.raises(XApiError, match="user not found: nobody"):
        _run(cores.user(fake, "nobody"))


def test_rate_limiter_spaces_requests():
    import time as time_mod

    cfg = mkconfig(rate_per_min=600)
    client = XClient(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    start = time_mod.monotonic()
    _run(client.rest("/1.1/x", {}))
    _run(client.rest("/1.1/x", {}))
    assert time_mod.monotonic() - start >= 0.09


def test_rate_limiter_disabled_passes_through():
    import time as time_mod

    cfg = mkconfig(rate_per_min=0)
    client = XClient(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    start = time_mod.monotonic()
    _run(client.rest("/1.1/x", {}))
    _run(client.rest("/1.1/x", {}))
    assert time_mod.monotonic() - start < 0.05


def test_like_core_uses_mutation_primitive():
    fake = FakeClient({"data": {"favorite_tweet": "Done"}})
    result = _run(cores.like(fake, "42"))
    assert result == {"liked": "42", "result": "Done"}
    kind, key, variables = fake.calls[0]
    assert (kind, key) == ("mutation", "like")
    assert variables == {"tweet_id": "42"}


def test_home_core_builds_variables_and_pages():
    response = {
        "data": {
            "home": {
                "list": [tweet_payload(tid=str(i)) for i in range(5)],
                "c": {"entryType": "TimelineTimelineCursor", "cursorType": "Bottom", "value": "NX"},
            }
        }
    }
    fake = FakeClient(response)
    result = _run(cores.home(fake, limit=2))
    kind, key, variables = fake.calls[0]
    assert (kind, key) == ("post", "home")
    assert variables == {
        "count": 2,
        "includePromotedContent": True,
        "withCommunity": True,
        "requestContext": "ptr",
    }
    assert [t["id"] for t in result["tweets"]] == ["0", "1"]
    assert result["cursor"] == "NX"
    _run(cores.home(fake, limit=2, cursor="NX"))
    assert fake.calls[1][2]["cursor"] == "NX"
    _run(cores.home(fake, limit=2, tab="following"))
    assert fake.calls[2][1] == "home_following"
    assert fake.calls[2][2] == {"count": 2, "enableRanking": True, "includePromotedContent": True}


def test_post_tweet_core_shapes():
    response = {
        "data": {
            "create_tweet": {
                "tweet_results": {"result": {"rest_id": "99", "legacy": {"full_text": "hi"}}}
            }
        }
    }
    fake = FakeClient(response)
    result = _run(cores.post_tweet(fake, "hi"))
    kind, key, variables = fake.calls[0]
    assert (kind, key) == ("mutation", "create_tweet")
    assert variables["tweet_text"] == "hi" and "reply" not in variables
    assert result == {"posted": True, "id": "99", "text": "hi"}
    _run(cores.post_tweet(fake, "yo", reply_to="42"))
    assert fake.calls[1][2]["reply"] == {"in_reply_to_tweet_id": "42", "exclude_reply_user_ids": []}


def test_like_core_surfaces_graphql_error():
    fake = FakeClient({"data": {}, "errors": [{"message": "already favorited"}]})
    assert _run(cores.like(fake, "42")) == {
        "liked": "42",
        "result": "error",
        "detail": "already favorited",
    }


# --- CLI ---------------------------------------------------------------------------


def test_cli_search_json_out(monkeypatch, db):
    _seed(db)
    monkeypatch.setattr(cli, "XClient", lambda cfg: FakeGraphClient())
    result = CliRunner().invoke(cli.cli, ["search", "python"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tweets"][0]["id"] == "1"


class FakeGraphClient:
    def __init__(self):
        pass

    async def graphql(self, key, variables):
        return {"data": {"list": [tweet_payload(tid="1")]}}

    async def graphql_post(self, key, variables):
        return {"data": {"favorite_tweet": "Done"}}

    async def rest(self, path, params):
        return {}

    async def aclose(self):
        pass


def test_cli_no_account_is_error(db):
    result = CliRunner().invoke(cli.cli, ["badges"])
    assert result.exit_code == 1
    assert "x2llm import" in json.loads(result.output)["error"]


def test_cli_user_flag_selects_account(monkeypatch, db):
    seen_configs = []
    monkeypatch.setattr(
        cli, "XClient", lambda cfg: (seen_configs.append(cfg), FakeGraphClient())[1]
    )
    _seed(db, "fabian")
    _seed(db, "other")
    result = CliRunner().invoke(cli.cli, ["-u", "OTHER", "search", "x"])
    assert result.exit_code == 0, result.output
    assert seen_configs[0].screen_name == "other"


def test_cli_multiple_accounts_use_default(monkeypatch, db):
    seen_configs = []
    monkeypatch.setattr(
        cli, "XClient", lambda cfg: (seen_configs.append(cfg), FakeGraphClient())[1]
    )
    _seed(db, "fabian")
    _seed(db, "other")
    result = CliRunner().invoke(cli.cli, ["search", "x"])
    assert result.exit_code == 0, result.output
    assert seen_configs[0].screen_name == "fabian"  # first import = default


def test_cli_prime_dumps_agent_briefing():
    result = CliRunner().invoke(cli.cli, ["prime"])
    assert result.exit_code == 0
    doc = result.output
    for section in (
        "agent briefing",
        "## Commands",
        "## Examples",
        "## REAL actions",
        "Error dictionary",
        "MCP server",
    ):
        assert section in doc
    # doc-rot guard: every `x2llm <cmd>` referenced in the doc must exist
    import re

    referenced = set(re.findall(r"x2llm (-u NAME )?([a-z_]+)", doc))
    cmds = set(cli.cli.commands)
    for _, cmd in referenced:
        assert cmd in cmds, f"prime references unknown command: {cmd}"


def _mini_har(op, qid, txid, method="GET"):
    url = f"https://x.com/i/api/graphql/{qid}/{op}?variables=%7B%7D&features=%7B%7D"
    entry = {
        "startedDateTime": "2026-10-07T13:00:00",
        "request": {
            "method": method,
            "url": url,
            "headers": [
                {"name": "x-client-transaction-id", "value": txid},
                {"name": "authorization", "value": "Bearer x"},
                {"name": "cookie", "value": "auth_token=a; ct0=c"},
            ],
        },
    }
    if method == "POST":
        entry["request"]["postData"] = {
            "text": json.dumps({"variables": {}, "queryId": qid, "features": {}})
        }
    return {"log": {"entries": [entry]}}


def test_cli_import_identifies_and_stores(db, tmp_path, monkeypatch):
    from x2llm import harvest

    har = tmp_path / "cap.har"
    har.write_text(json.dumps(_mini_har("SearchTimeline", "Q", "TX")))
    identified = {**MATERIAL, "screen_name": "Xeroc"}
    wire = {"search": {"query_id": "Q", "name": "SearchTimeline", "method": "get"}}
    monkeypatch.setattr(harvest, "harvest", lambda paths: (wire, {"search": "T"}, identified))
    result = CliRunner().invoke(cli.cli, ["import", str(har)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["imported"] == "xeroc" and payload["txids"] >= 1
    assert payload["wire_updated"] == ["search"]
    assert db.get_wire()["search"]["query_id"] == "Q"


def test_cli_import_stores_only_own_op_txids(db, tmp_path, monkeypatch):
    """txids are path-bound server-side (search rejects a home-path id with an
    empty 404): a capture that only exercised one op must not seed foreign
    ids into the other ops - those mint fresh path-correct ones instead."""
    from x2llm import harvest

    har = tmp_path / "cap.har"
    har.write_text(json.dumps(_mini_har("ViewerBadgeCounts", "Q", "BTX")))
    identified = {**MATERIAL, "screen_name": "Xeroc", "newest_tx": "FOREIGN"}
    wire = {"badges": {"query_id": "Q", "name": "ViewerBadgeCounts", "method": "get"}}
    monkeypatch.setattr(harvest, "harvest", lambda paths: (wire, {"badges": "BTX"}, identified))
    result = CliRunner().invoke(cli.cli, ["import", str(har)])
    assert result.exit_code == 0, result.output
    row = db.get_account("xeroc")
    assert row["txids"] == {"badges": "BTX"}


def test_cli_accounts_default_show_and_set(db):
    _seed(db, "fabian")
    _seed(db, "other")
    result = CliRunner().invoke(cli.cli, ["accounts", "default"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"default": "fabian"}

    result = CliRunner().invoke(cli.cli, ["accounts", "default", "OTHER"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"default": "other"}

    result = CliRunner().invoke(cli.cli, ["accounts", "default", "ghost"])
    assert result.exit_code == 1
    assert "no such account" in json.loads(result.output)["error"]


def test_cli_accounts_list_and_remove(db):
    _seed(db, "fabian")
    result = CliRunner().invoke(cli.cli, ["accounts", "list"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["accounts"][0]["screen_name"] == "fabian"
    assert "updated_at" in payload["accounts"][0]

    result = CliRunner().invoke(cli.cli, ["accounts", "remove", "FABIAN"], input="y\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output[result.output.index("{") :]) == {"removed": "fabian"}

    result = CliRunner().invoke(cli.cli, ["accounts", "remove", "ghost"], input="y\n")
    assert result.exit_code == 1
    assert "no such account" in json.loads(result.output[result.output.index("{") :])["error"]


# --- MCP stdio roundtrip ------------------------------------------------------------


def test_cli_import_user_flag_overrides(db, tmp_path):
    har = tmp_path / "cap.har"
    har.write_text(json.dumps(_mini_har("SearchTimeline", "Q", "TX")))
    result = CliRunner().invoke(cli.cli, ["import", str(har), "--user", "xer0c"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["imported"] == "xer0c"


def test_cli_import_without_cookies_is_error(db, tmp_path):
    har = tmp_path / "dry.har"
    entry = {
        "log": {
            "entries": [
                {
                    "startedDateTime": "t",
                    "request": {"method": "GET", "url": "https://x.com/home", "headers": []},
                }
            ]
        }
    }
    har.write_text(json.dumps(entry))
    result = CliRunner().invoke(cli.cli, ["import", str(har)])
    assert result.exit_code == 1
    assert "no session" in json.loads(result.output)["error"]


def test_cli_import_unidentifiable_is_error(db, tmp_path, monkeypatch):
    from x2llm import harvest

    monkeypatch.setattr(harvest, "resolve_session_name", lambda *a, **k: None)
    har = tmp_path / "cap.har"
    har.write_text(json.dumps(_mini_har("SearchTimeline", "Q", "TX")))  # no twid/user object
    result = CliRunner().invoke(cli.cli, ["import", str(har)])
    assert result.exit_code == 1
    assert "--user" in json.loads(result.output)["error"]


def test_mcp_server_stdio_roundtrip():
    env = {k: v for k, v in os.environ.items() if not k.startswith("X_")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "x2llm", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[1],
    )
    reqs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    try:
        for r in reqs:
            proc.stdin.write(json.dumps(r) + "\n")
        proc.stdin.flush()
        replies = [json.loads(proc.stdout.readline()) for _ in range(2)]
        tools = {t["name"]: t for t in replies[1]["result"]["tools"]}
        assert replies[0]["result"]["serverInfo"]["name"] == "x2llm"
        assert len(tools) == 16
        assert "accounts" in tools and "search" in tools and "get_tweet" in tools
        assert "username" in tools["search"]["inputSchema"]["properties"]
        assert tools["accounts"]["inputSchema"].get("properties") in (None, {})
    finally:
        proc.stdin.close()
        proc.terminate()


async def test_mcp_http_endpoint_answers_initialize_and_tools(db, monkeypatch):
    """--http mode: streamable HTTP at /mcp, exercised through ASGI transport."""
    import httpx

    from x2llm import mcp

    _seed(db, "stored")
    app = mcp.build_http_app()
    headers = {"Accept": "application/json, text/event-stream", "Host": "127.0.0.1:8000"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            r = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                },
            )
            assert r.status_code == 200
            assert r.json()["result"]["serverInfo"]["name"] == "x2llm"

            r = await client.post(  # stateless: tools work without initialize
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "accounts", "arguments": {}},
                },
            )
            assert r.status_code == 200
            payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert [a["screen_name"] for a in payload["accounts"]] == ["stored"]


def test_bind_tool_routes_username_to_client(monkeypatch, db):
    from x2llm import mcp as mcp_mod

    _seed(db, "fabian")
    seen = {}

    class BadgesClient:
        async def graphql(self, key, variables):
            return {
                "data": {"viewer_v2": {"user_results": {"result": {"badge_counts": {"dm": 1}}}}}
            }

    async def fake_client(username=None):
        seen["username"] = username
        return BadgesClient()

    monkeypatch.setattr(mcp_mod, "mcp_client", fake_client)
    bound = mcp_mod.bind_tool(cores.badges)
    assert "username" in inspect.signature(bound).parameters
    result = _run(bound(username="fabian"))
    assert seen["username"] == "fabian"
    assert result == {"badges": {"dm": 1}}


# --- helper ---------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# --- generator (incremental merge) ------------------------------------------------
# --- generator (incremental merge) - retired with scripts/har_to_endpoints.py ----
# wire entries now go straight into the db via `x2llm import`; see
# test_cli_import_identifies_and_stores and test_wire_roundtrip_and_override.


MATERIAL = {
    "auth_token": "at",
    "ct0": "ct",
    "bearer": "Bearer BB",
    "cookie": "auth_token=at; ct0=ct",
    "user_agent": "UA",
}


def test_harvest_identifies_viewer_via_twid(tmp_path):
    from x2llm import harvest as h

    body = json.dumps(
        {"data": {"users": [{"rest_id": "15904453", "core": {"screen_name": "Xeroc"}}]}}
    )
    har = {
        "log": {
            "entries": [
                {
                    "startedDateTime": "2026-10-10T09:00:00",
                    "request": {
                        "method": "GET",
                        "url": "https://x.com/i/api/graphql/X/ViewerBadgeCounts?variables=%7B%7D",
                        "headers": [
                            {"name": "cookie", "value": "auth_token=a; ct0=c; twid=u%3D15904453"}
                        ],
                    },
                    "response": {"content": {"text": body}},
                }
            ]
        }
    }
    p = tmp_path / "id.har"
    p.write_text(json.dumps(har))
    _, _, material = h.harvest([str(p)])
    assert material["viewer_id"] == "15904453"
    assert material["screen_name"] == "Xeroc"


def test_store_capture_uses_capture_identity(db):
    from x2llm import harvest

    identified = {**MATERIAL, "screen_name": "Fabian"}
    assert harvest.store_capture(identified, {"search": "T"}, None) == "fabian"
    row = db.get_account("fabian")
    assert row["auth_token"] == "at" and row["bearer"] == "Bearer BB"
    assert row["txids"] == {"search": "T"}


def test_store_capture_user_flag_wins(db):
    from x2llm import harvest

    identified = {**MATERIAL, "screen_name": "someoneelse"}
    assert harvest.store_capture(identified, {}, "offline") == "offline"
    assert db.get_account("offline")["auth_token"] == "at"


def test_store_capture_unidentifiable_raises(db, monkeypatch):
    from x2llm import harvest
    from x2llm.store import StoreError

    monkeypatch.setattr(harvest, "resolve_session_name", lambda m: None)
    with pytest.raises(StoreError, match="--user"):
        harvest.store_capture(MATERIAL, {}, None)


def _profile_transport(rest_id: str) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda r: httpx.Response(200, json={"data": {"user": {"result": {"rest_id": rest_id}}}})
    )


def test_verify_name_matches_and_mismatches():
    from x2llm.harvest import verify_name

    material = {**MATERIAL, "viewer_id": "15904453"}
    assert verify_name(material, "xer0c", transport=_profile_transport("15904453")) is True
    assert verify_name(material, "xer0c", transport=_profile_transport("42")) is False


def test_verify_name_without_twid_is_none():
    from x2llm.harvest import verify_name

    assert verify_name(MATERIAL, "xer0c", transport=_profile_transport("15904453")) is None


def test_store_capture_refuses_name_mismatch(db, monkeypatch):
    from x2llm import harvest
    from x2llm.store import StoreError

    material = {**MATERIAL, "viewer_id": "15904453"}
    monkeypatch.setattr(harvest, "verify_name", lambda m, n: False)
    with pytest.raises(StoreError, match="mismatch"):
        harvest.store_capture(material, {}, "xer0c")


def _intent_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "intent/user" in str(request.url):
            return httpx.Response(302, headers={"location": "https://x.com/xer0c?_intent=%7B%7D"})
        return httpx.Response(200, text="<html></html>")

    return httpx.MockTransport(handler)


def test_resolve_session_name_via_intent_page():
    from x2llm.harvest import resolve_session_name

    material = {**MATERIAL, "viewer_id": "15904453"}
    http = httpx.Client(transport=_intent_transport(), follow_redirects=True)
    name = resolve_session_name(material, http=http, transport=_profile_transport("15904453"))
    assert name == "xer0c"


def test_resolve_session_name_without_twid_is_none():
    from x2llm.harvest import resolve_session_name

    http = httpx.Client(transport=_intent_transport(), follow_redirects=True)
    assert resolve_session_name(MATERIAL, http=http) is None


def test_resolve_session_name_unverified_is_none():
    from x2llm.harvest import resolve_session_name

    material = {**MATERIAL, "viewer_id": "15904453"}
    http = httpx.Client(transport=_intent_transport(), follow_redirects=True)
    assert resolve_session_name(material, http=http, transport=_profile_transport("42")) is None


def test_store_capture_resolves_live_when_capture_is_silent(db, monkeypatch):
    from x2llm import harvest

    monkeypatch.setattr(harvest, "resolve_session_name", lambda m: "xer0c")
    assert harvest.store_capture(MATERIAL, {}, None) == "xer0c"
    assert db.get_account("xer0c")["auth_token"] == "at"


# --- dms -------------------------------------------------------------------------


class FakeRoutingClient:
    """Routes rest/rest_post/graphql calls to canned responses by substring."""

    def __init__(self, routes, screen_name="fabian"):
        self.routes = routes
        self.screen_name = screen_name
        self.calls = []
        self.config = types.SimpleNamespace(screen_name=screen_name)

    async def _route(self, kind, target, payload):
        self.calls.append((kind, target, payload))
        for frag, resp in self.routes:
            if frag in target:
                return resp
        raise AssertionError(f"unexpected {kind} call: {target}")

    async def rest(self, path, params):
        return await self._route("rest", path, params)

    async def aclose(self):
        pass

    async def rest_post(self, path, body):
        return await self._route("rest_post", path, body)

    async def graphql(self, key, variables):
        return await self._route("graphql", key, variables)

    async def graphql_post(self, key, variables):
        return await self._route("post", key, variables)


def _dm_user(uid, screen_name):
    return {"id_str": uid, "screen_name": screen_name, "name": screen_name.upper()}


def _dm_entry(mid, time, conv, sender, recipient, text):
    return {
        "message": {
            "id": mid,
            "time": time,
            "conversation_id": conv,
            "message_data": {
                "id": mid,
                "time": time,
                "recipient_id": recipient,
                "sender_id": sender,
                "text": text,
            },
        }
    }


def dm_inbox_payload():
    return {
        "inbox_initial_state": {
            "conversations": {
                "111-222": {
                    "conversation_id": "111-222",
                    "type": "ONE_TO_ONE",
                    "sort_event_id": "2120",
                    "sort_timestamp": "1794000000000",
                    "participants": [
                        {"user_id": "111", "last_read_event_id": "2100"},
                        {"user_id": "222"},
                    ],
                    "muted": False,
                    "trusted": False,
                    "status": "AT_END",
                },
                "111-333": {
                    "conversation_id": "111-333",
                    "type": "ONE_TO_ONE",
                    "sort_event_id": "2000",
                    "sort_timestamp": "1793000000000",
                    "participants": [
                        {"user_id": "111", "last_read_event_id": "2000"},
                        {"user_id": "333"},
                    ],
                    "muted": True,
                    "trusted": False,
                    "status": "AT_END",
                },
            },
            "entries": [
                _dm_entry("2120", "1794000000000", "111-222", "222", "111", "latest from alice"),
                _dm_entry("2000", "1793000000000", "111-333", "111", "333", "latest from viewer"),
            ],
            "users": {
                "111": _dm_user("111", "fabian"),
                "222": _dm_user("222", "alice"),
                "333": _dm_user("333", "bob"),
            },
        }
    }


def test_dm_inbox_core_sorts_flags_and_resolves_names():
    fake = FakeRoutingClient([("inbox_initial_state", dm_inbox_payload())])
    result = _run(cores.dm_inbox(fake))
    rows = result["conversations"]
    assert [r["id"] for r in rows] == ["111-222", "111-333"]  # newest first
    first, second = rows
    assert first["with"] == "alice" and first["unread"] is True
    assert first["last_message"]["text"] == "latest from alice"
    assert first["last_message"]["sender"] == "alice"
    assert first["participants"] == ["fabian", "alice"]
    assert second["with"] == "bob" and second["unread"] is False and second["muted"] is True
    assert second["last_message"]["sender"] == "fabian"
    assert result["cursor"] == "0|"  # first page done; next walks XChat pages


def dm_thread_payload(status="HAS_MORE"):
    return {
        "conversation_timeline": {
            "status": status,
            "min_entry_id": "10",
            "max_entry_id": "30",
            "entries": [
                _dm_entry("30", "1794000003000", "111-222", "111", "222", "c"),
                _dm_entry("20", "1794000002000", "111-222", "222", "111", "b"),
                _dm_entry("10", "1794000001000", "111-222", "222", "111", "a"),
            ],
            "users": {"111": _dm_user("111", "fabian"), "222": _dm_user("222", "alice")},
        }
    }


def test_dm_conversation_core_orders_chronologically_and_pages():
    fake = FakeRoutingClient([("conversation/111-222", dm_thread_payload())])
    result = _run(cores.dm_conversation(fake, "111-222"))
    assert [m["id"] for m in result["messages"]] == ["10", "20", "30"]
    assert [m["sender"] for m in result["messages"]] == ["alice", "alice", "fabian"]
    assert result["messages"][0]["time"] == "2026-11-06T21:20:01+00:00"
    assert result["cursor"] == "10"  # min_entry_id while more history exists
    _run(cores.dm_conversation(fake, "111-222", cursor="10"))
    assert fake.calls[1][2] == {"max_id": "10"}


def test_dm_conversation_cursor_null_at_end():
    fake = FakeRoutingClient([("conversation/111-222", dm_thread_payload(status="AT_END"))])
    result = _run(cores.dm_conversation(fake, "111-222"))
    assert result["cursor"] is None


def dm_send_event():
    """new2.json answers with an inbox-style entries/users blob."""
    return {
        "entries": [_dm_entry("2121", "1794100000000", "111-222", "111", "222", "hi there")],
        "users": {"111": _dm_user("111", "fabian"), "222": _dm_user("222", "alice")},
    }


def test_dm_send_to_screen_name_uses_existing_conversation():
    fake = FakeRoutingClient(
        [("inbox_initial_state", dm_inbox_payload()), ("new2.json", dm_send_event())]
    )
    result = _run(cores.dm_send(fake, "alice", "hi there"))
    post = [c for c in fake.calls if c[0] == "rest_post"]
    assert post == [
        ("rest_post", "/1.1/dm/new2.json", {"conversation_id": "111-222", "text": "hi there"})
    ]
    assert result["sent"] is True
    assert result["conversation_id"] == "111-222"
    assert result["message"] == {
        "id": "2121",
        "time": "2026-11-08T01:06:40+00:00",
        "sender": "fabian",
        "text": "hi there",
    }


def test_dm_send_new_chat_resolves_profile_and_builds_conversation_id():
    fake = FakeRoutingClient(
        [
            ("inbox_initial_state", dm_inbox_payload()),
            (
                "user",
                {
                    "data": {
                        "user": {"result": {"rest_id": "444", "core": {"screen_name": "carol"}}}
                    }
                },
            ),
            ("new2.json", dm_send_event()),
        ]
    )
    result = _run(cores.dm_send(fake, "carol", "first contact"))
    post = [c for c in fake.calls if c[0] == "rest_post"]
    assert post[0][2]["conversation_id"] == "111-444"  # numerically sorted pair
    assert result["sent"] is True


def test_dm_send_explicit_conversation_id_skips_inbox():
    fake = FakeRoutingClient([("new2.json", dm_send_event())])
    _run(cores.dm_send(fake, "111-222", "direct"))
    kinds = [c[0] for c in fake.calls]
    assert kinds == ["rest_post"]
    assert fake.calls[0][2]["conversation_id"] == "111-222"


def test_cli_dms_json_out(monkeypatch, db):
    _seed(db)
    monkeypatch.setattr(
        cli, "XClient", lambda cfg: FakeRoutingClient([("inbox_initial_state", dm_inbox_payload())])
    )
    result = CliRunner().invoke(cli.cli, ["dms"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["conversations"][0]["with"] == "alice"


# --- dms pagination (XChat inbox pages on api.x.com) -----------------------------


def _tlv(field: int, value: bytes) -> bytes:
    return b"\x0b" + field.to_bytes(2, "big") + len(value).to_bytes(4, "big") + value


def _xchat_event_b64(seq: str, uid: str, time_ms: str) -> str:
    blob = _tlv(1, seq.encode()) + _tlv(3, uid.encode()) + _tlv(6, time_ms.encode())
    return base64.b64encode(blob).decode()


def _xchat_item(
    cid: str, screen_name: str, uid: str, muted=False, seq="300", time_ms="1794000000000"
):
    return {
        "__typename": "XChatGetInboxPageConversationData",
        "conversation_detail": {
            "__typename": "XChatDirectConversationDetail",
            "conversation_id": cid,
            "is_archived": False,
            "is_muted": muted,
            "participants_results": [
                {
                    "__typename": "UserResults",
                    "rest_id": uid,
                    "result": {
                        "__typename": "User",
                        "core": {"__typename": "UserCore", "screen_name": screen_name},
                    },
                }
            ],
        },
        "latest_message_events": [_xchat_event_b64(seq, uid, time_ms)],
    }


def test_xchat_conversation_rows_maps_items():
    items = [
        _xchat_item("111:222", "alice", "222", muted=True),
        _xchat_item("111:333", "bob", "333", seq="100", time_ms="1793000000000"),
    ]
    rows = parse.xchat_conversation_rows(items)
    first, second = rows
    assert first["id"] == "111-222"  # colon -> legacy dash
    assert first["with"] == "alice" and first["participants"] == ["alice"]
    assert first["type"] == "ONE_TO_ONE" and first["muted"] is True
    assert first["last_message"]["sender"] == "alice"
    assert first["last_message"]["id"] == "300"
    assert "text" not in first["last_message"]  # E2EE: no plaintext on these pages
    assert second["last_message"]["time"] == "2026-10-26T07:33:20+00:00"


def xchat_page_response(cursor_id="50", snapshot="SNAP", exhausted=False, items=None):
    cursor = {
        "__typename": "XChatGetInboxPageEndCursor",
        "inbox_exhausted": True,
        "graph_snapshot_id": snapshot,
    }
    if not exhausted:
        cursor = {
            "__typename": "XChatGetInboxPageContinueCursor",
            "cursor_id": cursor_id,
            "graph_snapshot_id": snapshot,
        }
    return {"data": {"get_inbox_page": {"items": items or [], "inboxCursor": cursor}}}


def xchat_initial_response(items):
    return {
        "data": {
            "get_initial_chat_page": {
                "items": items,
                "inboxCursor": {"cursor_id": "50", "graph_snapshot_id": "SNAP"},
            }
        }
    }


def test_dm_inbox_first_page_hands_out_xchat_cursor():
    fake = FakeRoutingClient([("inbox_initial_state", dm_inbox_payload())])
    result = _run(cores.dm_inbox(fake))
    assert result["cursor"] == "0|"  # next: first XChat page
    assert result["conversations"][0]["with"] == "alice"


def test_dm_inbox_xchat_initial_and_continue_pages():
    fake = FakeRoutingClient(
        [
            (
                "xchat_inbox_page",
                xchat_page_response(cursor_id="100", items=[_xchat_item("111:333", "bob", "333")]),
            ),
            ("xchat_inbox", xchat_initial_response([_xchat_item("111:222", "alice", "222")])),
        ]
    )
    page1 = _run(cores.dm_inbox(fake, cursor="0|"))
    kind, key, variables = fake.calls[0]
    assert (kind, key) == ("graphql", "xchat_inbox")
    assert variables["max_local_sequence_id"] == "1"
    assert variables["query_settings"]["inbox_conversation_limit"] == 50
    assert page1["cursor"] == "50|SNAP"
    assert page1["conversations"][0]["id"] == "111-222"

    page2 = _run(cores.dm_inbox(fake, cursor="50|SNAP"))
    kind, key, variables = fake.calls[1]
    assert (kind, key) == ("graphql", "xchat_inbox_page")
    assert variables["continue_cursor"] == {
        "cursor_id": "50",
        "graph_snapshot_id": "SNAP",
        "graph_snapshot_restarted": False,
    }
    assert page2["cursor"] == "100|SNAP"
    assert page2["conversations"][0]["with"] == "bob"


def test_dm_inbox_xchat_exhausted_ends_pagination():
    fake = FakeRoutingClient([("xchat_inbox_page", xchat_page_response(exhausted=True))])
    result = _run(cores.dm_inbox(fake, cursor="100|SNAP"))
    assert result["cursor"] is None
    assert result["conversations"] == []


def test_dm_conversation_normalizes_colon_ids():
    fake = FakeRoutingClient([("conversation/111-222", dm_thread_payload(status="AT_END"))])
    _run(cores.dm_conversation(fake, "111:222"))
    assert fake.calls[0][1] == "/1.1/dm/conversation/111-222.json"


def test_client_graphql_honors_registry_base(db):
    from x2llm import store

    store.upsert_wire(
        {
            "xchat_inbox": {
                "query_id": "Q",
                "name": "GetInitialXChatPageQuery",
                "method": "get",
                "base": "https://api.x.com",
            }
        }
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("x-client-transaction-id")))
        return httpx.Response(200, json={"data": {}})

    async def go():
        client = XClient(mkconfig(), transport=httpx.MockTransport(handler))
        try:
            await client.graphql("xchat_inbox", {"max_local_sequence_id": "1"})
        finally:
            await client.aclose()

    _run(go())
    url, txid = seen[0]
    assert url.startswith("https://api.x.com/graphql/Q/GetInitialXChatPageQuery")
    # api.x.com txids look single-use; requests without one are accepted
    assert txid is None


def test_harvest_maps_apix_xchat_operations(tmp_path):
    from x2llm import harvest as h

    har = {
        "log": {
            "entries": [
                {
                    "startedDateTime": "2026-11-11T11:17:44",
                    "request": {
                        "method": "GET",
                        "url": "https://api.x.com/graphql/Hv18nE2Cg6YZJNcRVLGT-g/GetInboxPageRequestQuery?variables=%7B%7D",
                        "headers": [
                            {"name": "x-client-transaction-id", "value": "T"},
                            {"name": "authorization", "value": "Bearer x"},
                            {"name": "cookie", "value": "auth_token=a; ct0=c"},
                        ],
                    },
                }
            ]
        }
    }
    p = tmp_path / "x.har"
    p.write_text(json.dumps(har))
    graphql, txids, _ = h.harvest([str(p)])
    assert graphql["xchat_inbox_page"] == {
        "query_id": "Hv18nE2Cg6YZJNcRVLGT-g",
        "name": "GetInboxPageRequestQuery",
        "method": "get",
        "base": "https://api.x.com",
    }
    assert txids == {}  # api.x.com txids are single-use - never pooled


def test_cli_dms_cursor_pages(monkeypatch, db):
    _seed(db)
    monkeypatch.setattr(
        cli,
        "XClient",
        lambda cfg: FakeRoutingClient(
            [("xchat_inbox", xchat_initial_response([_xchat_item("111:222", "alice", "222")]))]
        ),
    )
    result = CliRunner().invoke(cli.cli, ["dms", "--cursor", "0|"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["conversations"][0]["with"] == "alice"
    assert payload["cursor"] == "50|SNAP"
