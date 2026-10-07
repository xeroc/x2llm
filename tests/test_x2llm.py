"""Offline tests: config, RPC encapsulation (httpx.MockTransport), parsers,
cores (FakeClient), CLI (CliRunner) and one stdio MCP roundtrip.
No test touches the network."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from x2llm import cli, cores, parse
from x2llm.client import XApiError, XClient
from x2llm.config import Config, ConfigError

ENV = {"X_AUTH_TOKEN": "at", "X_CT0": "ct"}
TX = {"X_TXIDS": '{"search": "STX", "like": "LTX"}'}


# --- config -------------------------------------------------------------------


def test_config_missing_vars_lists_both():
    with pytest.raises(ConfigError, match="X_AUTH_TOKEN, X_CT0"):
        Config.from_env({})


def test_config_cookie_composition_and_override():
    cfg = Config.from_env(ENV)
    assert cfg.cookie_header == "auth_token=at; ct0=ct"
    cfg = Config.from_env({**ENV, "X_COOKIE": "auth_token=at; ct0=ct; cf_clearance=x"})
    assert cfg.cookie_header.startswith("auth_token=at; ct0=ct; cf_clearance=")


# --- client / RPC layer --------------------------------------------------------


def test_client_graphql_builds_request_and_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": {}})

    client = XClient(Config.from_env({**ENV, **TX}), transport=httpx.MockTransport(handler))
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


def test_client_empty_404_hints_at_txid_expiry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"")

    client = XClient(Config.from_env(ENV), transport=httpx.MockTransport(handler))
    with pytest.raises(XApiError, match="transaction-id expired"):
        _run(client.graphql("search", {}))


def test_client_403_hints_at_credentials():
    client = XClient(
        Config.from_env(ENV), transport=httpx.MockTransport(lambda r: httpx.Response(403, json={}))
    )
    with pytest.raises(XApiError, match="credentials rejected"):
        _run(client.rest("/1.1/search/typeahead.json", {"q": "x"}))

def test_client_mutation_posts_json_body_with_txid():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": {"favorite_tweet": "Done"}})

    client = XClient(Config.from_env({**ENV, **TX}), transport=httpx.MockTransport(handler))
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


def test_tweet_row_prefers_note_tweet_text():
    payload = tweet_payload()
    payload["tweet_results"]["result"]["note_tweet"] = {
        "note_tweet_results": {"result": {"text": "the full long text"}}}
    assert parse.collect_tweets(payload)[0]["text"] == "the full long text"


def test_collect_tweets_unwraps_visibility_and_dedups():
    inner = tweet_payload(tid="7")["tweet_results"]["result"]
    wrapped = {"tweet_results": {"result": {"__typename": "TweetWithVisibilityResults", "tweet": inner}}}
    doc = {"a": wrapped, "b": tweet_payload(tid="7"), "c": tweet_payload(tid="8")}
    rows = parse.collect_tweets(doc)
    assert [r["id"] for r in rows] == ["7", "8"]
    assert rows[0]["views"] == 42


def test_collect_cursors():
    doc = {
        "entries": [
            {"content": {"entryType": "TimelineTimelineCursor", "cursorType": "Bottom", "value": "B"}},
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
        self.calls.append(("mutation" if key in ("like", "create_tweet") else "post", key, variables))
        return self.response

    async def rest(self, path, params):
        self.calls.append(("rest", path, params))
        return self.response


def test_search_core_truncates_and_returns_cursor():
    response = {
        "data": {
            "list": [tweet_payload(tid=str(i)) for i in range(5)],
            "cursor": {"content": {
                "entryType": "TimelineTimelineCursor", "cursorType": "Bottom", "value": "C"}},
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
    fake = FakeClient({"users": [{"id_str": "2", "screen_name": "b", "name": "B"}], "next_cursor": 0})
    result = _run(cores.following(fake, "123", limit=1))
    assert fake.calls[0][2]["user_id"] == "123"
    assert result["cursor"] is None
    _run(cores.following(fake, "jack"))
    assert fake.calls[1][2]["screen_name"] == "jack"


def test_suggest_core_shape():
    fake = FakeClient({"topics": [{"topic": "bitcoin"}], "users": [{"id_str": "1", "screen_name": "a", "name": "A"}]})
    assert _run(cores.suggest(fake, "bit")) == {
        "topics": ["bitcoin"], "users": [{"id": "1", "screen_name": "a", "name": "A"}]
    }

def test_user_core_maps_profile_and_strips_at():
    response = {"data": {"user": {"result": {
        "rest_id": "7", "is_blue_verified": True,
        "core": {"screen_name": "a", "name": "A", "created_at": "Fri Sep 25 12:59:34 +0000 2026"},
        "profile_bio": {"description": "builder", "entities": {"url": {"urls": [
            {"expanded_url": "https://example.com"}]}}},
        "location": {"location": "Berlin"},
        "relationship_counts": {"followers": 10, "following": 5},
        "tweet_counts": {"tweets": 100, "media_tweets": 4},
    }}}}
    fake = FakeClient(response)
    row = _run(cores.user(fake, "@a"))
    kind, key, variables = fake.calls[0]
    assert (kind, key) == ("graphql", "user")
    assert variables == {"screen_name": "a", "withGrokTranslatedBio": True}
    assert row["screen_name"] == "a" and row["followers"] == 10 and row["url"] == "https://example.com"


def test_user_core_not_found_raises():
    fake = FakeClient({"data": {"user": None}})
    with pytest.raises(XApiError, match="user not found: nobody"):
        _run(cores.user(fake, "nobody"))


def test_rate_limiter_spaces_requests():
    import time as time_mod
    cfg = Config.from_env({**ENV, "X_RATE_LIMIT": "600"})
    client = XClient(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    start = time_mod.monotonic()
    _run(client.rest("/1.1/x", {}))
    _run(client.rest("/1.1/x", {}))
    assert time_mod.monotonic() - start >= 0.09


def test_rate_limiter_disabled_passes_through():
    import time as time_mod
    cfg = Config.from_env({**ENV, "X_RATE_LIMIT": "0"})
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
    response = {"data": {"home": {"list": [tweet_payload(tid=str(i)) for i in range(5)],
                                  "c": {"entryType": "TimelineTimelineCursor",
                                        "cursorType": "Bottom", "value": "NX"}}}}
    fake = FakeClient(response)
    result = _run(cores.home(fake, limit=2))
    kind, key, variables = fake.calls[0]
    assert (kind, key) == ("post", "home")
    assert variables == {"count": 2, "includePromotedContent": True,
                         "withCommunity": True, "requestContext": "ptr"}
    assert [t["id"] for t in result["tweets"]] == ["0", "1"]
    assert result["cursor"] == "NX"
    _run(cores.home(fake, limit=2, cursor="NX"))
    assert fake.calls[1][2]["cursor"] == "NX"
    _run(cores.home(fake, limit=2, tab="following"))
    assert fake.calls[2][1] == "home_following"
    assert fake.calls[2][2] == {"count": 2, "enableRanking": True,
                                "includePromotedContent": True}

def test_post_tweet_core_shapes():
    response = {"data": {"create_tweet": {"tweet_results": {"result": {
        "rest_id": "99", "legacy": {"full_text": "hi"}}}}}}
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


def test_cli_search_json_out(monkeypatch):
    monkeypatch.setattr(cli, "XClient", lambda cfg: FakeGraphClient())
    result = CliRunner().invoke(cli.cli, ["search", "python"], env={**ENV, "PATH": os.environ["PATH"]})
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


def test_cli_missing_env_is_error():
    result = CliRunner().invoke(cli.cli, ["badges"], env={"X_AUTH_TOKEN": "", "X_CT0": ""})
    assert result.exit_code == 1
    assert "X_AUTH_TOKEN" in json.loads(result.output)["error"]


# --- MCP stdio roundtrip ------------------------------------------------------------


def test_mcp_server_stdio_roundtrip():
    env = {k: v for k, v in os.environ.items() if not k.startswith("X_")}
    env.update(ENV)
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
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    try:
        for r in reqs:
            proc.stdin.write(json.dumps(r) + "\n")
        proc.stdin.flush()
        replies = [json.loads(proc.stdout.readline()) for _ in range(2)]
        tools = [t["name"] for t in replies[1]["result"]["tools"]]
        assert replies[0]["result"]["serverInfo"]["name"] == "x2llm"
        assert "search" in tools and "get_tweet" in tools and len(tools) == 12
    finally:
        proc.stdin.close()
        proc.terminate()


# --- helper ---------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# --- generator (incremental merge) ------------------------------------------------


def _load_generator():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "har_to_endpoints", Path(__file__).resolve().parents[1] / "scripts" / "har_to_endpoints.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mini_har(op, qid, txid, method="GET"):
    url = f"https://x.com/i/api/graphql/{qid}/{op}?variables=%7B%7D&features=%7B%7D"
    entry = {"startedDateTime": "2026-10-07T13:00:00",
             "request": {"method": method, "url": url, "headers": [
        {"name": "x-client-transaction-id", "value": txid},
        {"name": "authorization", "value": "Bearer x"},
        {"name": "cookie", "value": "auth_token=a; ct0=c"},
    ]}}
    if method == "POST":
        entry["request"]["postData"] = {
            "text": json.dumps({"variables": {}, "queryId": qid, "features": {}})}
    return {"log": {"entries": [entry]}}


def test_generator_merges_incrementally_without_writing_source(tmp_path):
    gen = _load_generator()
    registry = tmp_path / "registry.py"
    seed = {key: {"query_id": f"OLD{key}", "name": name, "method": "get"}
            for key, name in [("search", "SearchTimeline"), ("tweet", "TweetDetail"),
                              ("notifications", "NotificationsTimeline"), ("badges", "ViewerBadgeCounts"),
                              ("analytics", "accountOverviewDailyQuery"), ("user", "UserByScreenName"),
                              ("home", "HomeTimeline"), ("home_following", "HomeLatestTimeline"),
                              ("like", "FavoriteTweet"), ("create_tweet", "CreateTweet")]}
    seed["tweet"] = {"query_id": "K", "name": "TweetDetail", "method": "get"}
    registry.write_text(gen.render(seed, "Bearer x"))
    har = tmp_path / "mini.har"
    har.write_text(json.dumps(_mini_har("SearchTimeline", "NEWQID", "NEWTX")))
    graphql, bearer, _txids, _material, fresh, previous = gen.build([str(har)], registry)
    content = gen.render(graphql, bearer)
    assert graphql["search"]["query_id"] == "NEWQID"
    assert "txid" not in content  # session data never ships in source
    assert graphql["tweet"]["query_id"] == "K"
    assert fresh == {"search"} and "tweet" in previous
    # and the registry file itself was NOT rewritten
    assert "NEWQID" not in registry.read_text()


def test_generator_fails_when_still_incomplete(tmp_path):
    gen = _load_generator()
    har = tmp_path / "mini.har"
    har.write_text(json.dumps(_mini_har("SearchTimeline", "NEWQID", "NEWTX")))
    with pytest.raises(SystemExit, match="missing operations"):
        gen.build([str(har)], tmp_path / "registry.py")
