# x2llm

x.com (Twitter) client for LLM agents. Multiple accounts in a local sqlite
store, three faces: a Python library, a `click` CLI, and a stdio MCP
server. Built for machines first: every command prints exactly one JSON
document (`x2llm prime` is the one markdown exception - an agent briefing),
every error prints `{"error": "..."}` and exits non-zero. Everything
is read-only **except one deliberate write: `like`** — no posting, replying,
following, or DMing.

## Key Features

- Search (full operator support: `from:user`, `since:`, `lang:`, `min_faves:` ...)
- Multiple accounts in a local store: `--user` on the CLI, `username` on MCP tools
- Tweet + reply-thread retrieval
- Notification tab reading
- Account analytics (impressions, likes, follows — your own statistics page)
- Follower/following graphs, typeahead suggestions, unread badge counts
- Like a tweet (the single write action, explicit everywhere)
- Same core functions power both CLI and MCP server

## Tech Stack

- **Language**: Python 3.12+
- **HTTP**: httpx (async)
- **CLI**: click
- **MCP**: official `mcp` SDK v2 (stdio transport)
- **Packaging**: uv + hatchling (src layout)
- **Tests**: pytest + pytest-asyncio, ruff for linting

## How x.com authentication works (the 30-second version)

The web app authenticates every API call with four pieces:

| Piece | What it is | Secret? |
|---|---|---|
| `authorization: Bearer AAAA…` | Public web-client key, baked into x.com's JS bundle. Same for every user. | No |
| Cookie `auth_token` | The session itself (httpOnly). **Whoever holds it IS the account** — read and write. | **Yes** |
| Cookie `ct0` | CSRF token. Mirrored verbatim into the `x-csrf-token` header. | Semi |
| `x-client-transaction-id` | Anti-automation header derived from page state. Only *some* endpoints validate it (search does, most others don't); validated copies are captured per endpoint and expire after a while. | No |

There is no request signing. Bearer + cookies + mirrored CSRF header is the
whole scheme. `x2llm` encapsulates all of this in `XClient` — nothing above
that layer ever touches headers.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (or pip + venv, but commands below assume uv)
- A browser logged in to x.com (for credentials — 2 minutes, see below)

## Getting Started

### 1. Clone and install

```bash
cd x2llm
make setup        # uv sync — creates .venv with all deps
```

### 2. Harvest your logged-in browser session into the account store

Credentials never enter the environment or the repo — they live in a local
sqlite db (`~/.local/share/x2llm/accounts.db` on Linux; 0600, created on
first write). Accounts get in via a HAR capture:

1. Open `https://x.com` in your logged-in browser
2. `F12` → **Network** tab, filter for `i/api`, then skim your timeline
   (a fuller tour also refreshes wire constants — see
   [Refreshing captured tokens](#refreshing-captured-tokens))
3. Export HAR: Network ⚙️ gear → *Save all as HAR*
4. Import:

```bash
uv run x2llm import '../x.com_Archive [26-10-10 09-19-00].har'
```

The account is identified from the capture itself: its `twid` session
cookie carries your numeric user id, matched against user objects in the
responses — no live call needed. Captures that never name the viewer are
filed explicitly instead, and that name is then **verified** live against
the capture's session id before anything is stored:

```bash
uv run x2llm import capture.har --user xer0c   # verified: name ↔ twid id
```

Imported material per account: auth_token, ct0, bearer, full cookie
header, user-agent and the transaction-id pool. One account per run.

Verify:

```bash
uv run x2llm badges
```

Expected output:

```json
{
 "badges": {
  "dm_unread_count": 0,
  "ntab_unread_count": 1,
  "total_unread_count": 1
 }
}
```

See what is stored with `x2llm accounts list` (screen names, last refresh
time, which one is the default). The first imported account becomes the
default - the one used when `--user` is omitted; change it with
`x2llm accounts default NAME`, remove an account with
`x2llm accounts remove NAME`.

### 3. Install the CLI system-wide (optional)

```bash
make install       # uv tool install — puts `x2llm` on your PATH
```

## Architecture

### Directory structure

```
├── src/x2llm/
│   ├── config.py        # Config.load() — account row from db + infra env knobs
│   ├── store.py         # sqlite account store (screen_name → credentials)
│   ├── registry.py      # wire constants (query ids, features, toggles) - plain tracked source
│   ├── client.py        # XClient — the ONLY module that knows the RPC protocol
│   ├── parse.py         # response-blob walkers → flat dicts (tweet/user/cursor rows)
│   ├── cores.py         # 8 async service cores, shared by CLI and MCP
│   ├── cli.py           # click commands, one JSON doc per command
│   └── mcp.py           # stdio MCP server wrapping the same cores
├── scripts/
│   └── har_to_endpoints.py   # harvest a HAR: proposes registry.new.py + upserts the account db
├── tests/test_x2llm.py       # offline: MockTransport, FakeClient, CliRunner, stdio roundtrip
└── Makefile
```

### Request lifecycle

```
click command / MCP tool
        │
        ▼
cores.py  (search, tweet, notifications, … like — dicts in, dicts out)
        │
        ▼
XClient.graphql(key, variables)          — reads
XClient.graphql_post(key, variables)    — POST GraphQL (home feed, like write)
XClient.rest(path, params)               — legacy REST reads
        │   builds URL: GET https://x.com/i/api/graphql/<queryId>/<Operation>
        │             ?variables=<json>&features=<json>
        │   attaches: bearer, cookie session, x-csrf-token (== ct0),
        │             x-twitter-auth-type, per-endpoint x-client-transaction-id
        ▼
httpx.AsyncClient
        │
        ▼
x.com → 200 JSON (or XApiError with a human hint)
        │
        ▼
parse.py walkers (walk / collect_tweets / collect_cursors / notification_rows)
        │
        ▼
flat JSON row: {id, author, text, likes, views, …}
```

### Rate limiting

XClient spaces requests so at most `X_RATE_LIMIT` per minute leave the process
(default 30 — deliberately below what the session allows; x.com's own
per-endpoint budgets seen in responses are far higher, e.g. 12 000 / 15 min
for following-list). The limiter is a simple monotonic-clock slot allocator
under an asyncio lock; 0 disables. Server-side 429s surface as errors with a
hint instead of being retried — the right answer is to slow down.

### Design invariants

- **RPC is encapsulated.** Only `client.py` imports httpx or knows what a
  header is. Only `registry.py` holds wire constants - plain tracked source.
- **Cores are transport-agnostic.** They call `graphql(key, variables)` /
  `graphql_mutation(key, variables)` / `rest(path, params)` — anything with
  those three async methods works (see `FakeClient` in the tests). This is
  what keeps the suite offline.
- **One code path.** CLI handlers and MCP tools bind the same core functions;
  behavior can never drift between the two surfaces.
- **Writes are isolated.** Read feeds are GET, except POST-fed timelines
  (home — the browser itself scrolls via POST). The single write,
  `like`, shares the POST primitive but nothing else does. The MCP server
  registers it with `read_only_hint=false` and a loud description.
- **One pagination model.** Every list core (`home`, `search`, `tweet`,
  `notifications`, `following`) returns a `cursor`; feed it back for the next
  page, `null` means end. The home timeline has two tabs served by different
  operations: `for_you` (HomeTimeline, algorithmic) and `following`
  (HomeLatestTimeline, chronological) — cursors are tab-specific, pass back
  what the same tab returned.

## Environment Variables

Credentials are **not** environment variables — they live in the account db
(see [Getting Started](#getting-started)). The environment only carries
infrastructure knobs:

| Variable | Description | Default |
|---|---|---|
| `X_API_BASE` | API root | `https://x.com/i/api` |
| `X_TIMEOUT` | Per-request timeout, seconds | `20` |
| `X_RATE_LIMIT` | Max requests per minute, client-enforced (0 = unlimited) | `30` |

## CLI Reference

Every command accepts `-u/--user NAME` before the subcommand to pick the
account; omitted, the default account is used.


| Command | Description |
|---|---|
| `x2llm import HAR [HAR…] [--user NAME]` | Import an account from a capture; identity from the capture (twid), `--user` verified live |
| `x2llm accounts list` | Stored screen names, last refresh, default flag |
| `x2llm accounts default [NAME]` | Show or set the default account |
| `x2llm accounts remove NAME` | Delete NAME's credentials (asks for confirmation) |
| `x2llm prime` | Agent briefing (markdown - the one non-JSON command): commands, examples, error fixes |
| `x2llm home [--tab for_you\|following] [--limit N] [--cursor C]` | Home timeline; scroll with the returned cursor |
| `x2llm badges` | Unread notification/DM counts — cheapest auth check |
| `x2llm user SCREEN_NAME` | Profile lookup: bio, url, follower counts, verification |
| `x2llm me` | Viewer id + follower counts |
| `x2llm suggest QUERY` | Typeahead topics/users for a prefix |
| `x2llm search QUERY [--limit N] [--product Top\|Latest\|Media] [--cursor C]` | Full search with X operators |
| `x2llm tweet ID [--cursor C]` | Tweet + reply thread |
| `x2llm notifications [--limit N] [--cursor C]` | Notification tab |
| `x2llm following USER [--limit N] [--cursor C]` | Accounts USER follows (id or screen name) |
| `x2llm like TWEET_ID` | **Like a tweet — real, public, notifies the author** |
| `x2llm mcp` | Run as stdio MCP server |

All pagination works the same way: the response carries a `cursor`; feed it
back via `--cursor` for the next page (`null` means end).

Example — search, take the cursor, get page two:

```bash
uv run x2llm search "solana lang:en min_faves:100" --limit 10
uv run x2llm search "solana lang:en min_faves:100" --limit 10 --cursor 'DAACCgACHUB…'
```

## MCP Server

`x2llm mcp` speaks MCP over stdio with 13 tools — the same cores as the CLI,
plus `accounts`. Account selection is per tool call: every tool takes an
optional `username` parameter (omitted = the default account, marked by the
`accounts` tool; an unset default is an error that says how to pick one —
Eleven tools are
annotated `read_only_hint` (`accounts`, `get_home_timeline`, `get_user`,
`get_badges`, `me`, `suggest`, `search`, `get_tweet`, `get_notifications`,
`get_analytics`, `get_following`); `like_tweet` and `post_tweet` are the
writes, registered with `read_only_hint=false` and descriptions that
instruct agents to call them only on explicit user request. Liking an
already-liked tweet returns the server's idempotent rejection
(`{"result": "error", "detail": "... has already favorited ..."}`) — no
state changes.

No account is pinned at startup and no credentials go into the MCP config:

```json
{
  "mcpServers": {
    "x": {
      "command": "/absolute/path/to/x2llm/.venv/bin/x2llm",
      "args": ["mcp"]
    }
  }
}
```

Quick smoke test by hand:

```bash
printf '%s\n%s\n%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 | uv run x2llm mcp
```

## Refreshing captured tokens

The package ships only **public, account-independent constants**
(`src/x2llm/registry.py`: query ids, features blobs, fieldToggles — identical
for every user, taken from x.com's JS bundles; they rotate on a months
scale). Everything account-scoped — auth_token, ct0, bearer, cookie,
user-agent and the transaction-id pool — lives in the local account db.
**Transaction ids expire fast (observed: tens of minutes to hours)** and
only some endpoints enforce them — `search` is the one that hurts. Nothing
user-specific ever enters the package, so it is safe to publish. No file
inside the repo is machine-written: refresh produces `registry.new.py`
(gitignored) plus a diff for you to review and move over by hand.

Refreshes are **incremental**: entries in the HARs you pass win; operations
they don't contain keep their previous query ids from the existing
registry. One small HAR that only captured a search is enough — the run
fails only if the merged result is still incomplete. The generator prints
which endpoints were refreshed vs kept, then updates the account db.

When `search` or `like` starts failing with an empty 404 (the error text
says so), or credentials go stale:

1. Open x.com logged in as the account to refresh → `F12` → Network →
   filter `i/api`
2. Visit: the **home timeline** (badges), run one **search**, open one
   **tweet**, open **notifications**, open **x.com/analytics**, open one
   **user profile**, **like** one tweet, **scroll both home tabs** (FOR YOU
   and FOLLOWING) two screens (that touches all operations)
3. Export HAR: Network ⚙️ gear → *Save all as HAR*
4. Harvest:

```bash
make refresh HAR='../x.com_Archive [26-10-07 13-27-31].har'
# prints a unified diff; then, if it looks right:
mv registry.new.py src/x2llm/registry.py && make check
# the account db was already updated in place - nothing to copy
```

The account half behaves exactly like `x2llm import`: identity comes from
the capture's `twid` cookie (user objects in the responses), `--user NAME`
files explicitly and is verified against the capture's session id — no way
to file credentials under the wrong user. One account per run: capture
each browser profile separately.

While `search`/`like` are down, everything else keeps working — tweet,
notifications, analytics, following, suggest, both home tabs and user lookups
work without any transaction id at all.
Features blobs and fieldToggles are captured per endpoint (x.com ships
several variants). The generator accepts several HAR files and merges them,
so captures can be split across sessions:

```bash
uv run python scripts/har_to_endpoints.py 'reads.har' 'like.har'
```

## Testing

```bash
make check        # ruff + pytest — the gate every change must pass
uv run pytest -q tests/test_x2llm.py::test_search_core_truncates_and_returns_cursor   # single test
```

The suite is offline by design: `httpx.MockTransport` exercises the real
`XClient` request construction, `FakeClient` exercises cores, `CliRunner`
exercises commands, and one subprocess test does a full MCP stdio handshake
(`initialize` + `tools/list`) with dummy credentials. No test needs your real
account or network.

## Troubleshooting

### `{"error": "…: HTTP 404 … transaction-id expired"}`

Search hit its transaction-id expiry. Refresh per the section above. Other
commands are unaffected.

### `{"error": "…: HTTP 401/403 …"}`

The account's `auth_token`/`ct0` are stale (logged out elsewhere? password
change? long-lived capture?). Re-capture and `make refresh` per the section
above — the harvest stores the full `Cookie` header (including
`cf_clearance`) and the capture browser's user-agent, which covers the
IP/UA-drift cases automatically.

### `{"error": "no accounts stored yet …"}`

The account db is empty — run `make refresh HAR=...` first (see
[Getting Started](#getting-started)). With several accounts stored, pick one
with `-u/--user` (CLI) or the `username` parameter (MCP tools).

### Empty body with a `cf-ray` header

Cloudflare edge rejection, not x.com. Re-capture in the same browser
profile and refresh — the stored cookie header and user-agent are replaced
wholesale by each harvest.

### `author: null` in results

x.com is A/B-testing a new user-object shape. `parse.py` reads both the old
(`legacy`) and new (`core`) paths; if both are null you are looking at a third
shape — file it with a response sample.

## Security Notes

> [!CAUTION]
> `auth_token` is a full-account bearer credential: read **and write** access
> until it expires. The account db (`~/.local/share/x2llm/accounts.db`,
> 0600), a shell history line, or a HAR file containing it must be treated
> like a password file. HAR exports additionally contain every response
> body the browser saw. `*.har` is gitignored for a reason — keep it that
> way.

This project is API-usage of your own logged-in session for personal tooling
(reads + likes you explicitly request). Respect x.com's terms of service and rate limits; the
client sends plain GETs at human-scale speeds and has no retry hammering.

## License

Internal tool, no license granted. All rights reserved.
