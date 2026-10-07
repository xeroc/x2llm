# x2llm

x.com (Twitter) client for LLM agents. One credential set from the
environment, three faces: a Python library, a `click` CLI, and a stdio MCP
server. Built for machines first: every command prints exactly one JSON
document, every error prints `{"error": "..."}` and exits non-zero. Everything
is read-only **except one deliberate write: `like`** — no posting, replying,
following, or DMing.

## Key Features

- Search (full operator support: `from:user`, `since:`, `lang:`, `min_faves:` ...)
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

### 2. Extract credentials from your logged-in browser

You need two cookie values from a browser session that is logged in to x.com:
`auth_token` and `ct0`.

> [!IMPORTANT]
> `document.cookie` in the devtools console will **not** show `auth_token` —
> it is httpOnly. Use one of the methods below.

#### Method A — DevTools Application tab (fastest, cookies only)

1. Open `https://x.com` in your logged-in browser
2. Press `F12` → **Storage** (Firefox) or **Application** (Chrome) → **Cookies** → `https://x.com`
3. Find the rows `auth_token` and `ct0`, copy their **Value** columns
4. Put them into `.env`:

```bash
cp .env.example .env
```

```
X_AUTH_TOKEN=paste auth_token value here
X_CT0=paste ct0 value here
```

#### Method B — Network tab (gets you everything, incl. transaction ids)

1. `F12` → **Network** tab, filter for `i/api`
2. Scroll your x.com home timeline once — badge-count calls appear
3. Click any request, then **Headers** → **Request Headers** → find the `Cookie:` header
4. Copy the values of `auth_token=…` and `ct0=…` from it into `.env` as above

If you also want `search` to work, export a HAR (see
[Refreshing captured tokens](#refreshing-captured-tokens)) — search validates
`x-client-transaction-id`, which the HAR provides.

> [!TIP]
> Cookies from the Network tab can be pasted wholesale into `X_COOKIE` —
> including `cf_clearance` — which helps if Cloudflare challenges your
> machine: `X_COOKIE=auth_token=…; ct0=…; cf_clearance=…`

#### Verifying credentials work

```bash
source .env && export X_AUTH_TOKEN X_CT0
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

### 3. Install the CLI system-wide (optional)

```bash
make install       # uv tool install — puts `x2llm` on your PATH
```

## Architecture

### Directory structure

```
├── src/x2llm/
│   ├── config.py        # Config.from_env() — every knob is an env var
│   ├── registry.py      # wire constants (query ids, features, toggles) - plain tracked source
│   ├── client.py        # XClient — the ONLY module that knows the RPC protocol
│   ├── parse.py         # response-blob walkers → flat dicts (tweet/user/cursor rows)
│   ├── cores.py         # 8 async service cores, shared by CLI and MCP
│   ├── cli.py           # click commands, one JSON doc per command
│   └── mcp.py           # stdio MCP server wrapping the same cores
├── scripts/
│   └── har_to_endpoints.py   # harvest a HAR: proposes registry.new.py + writes .env.fresh
├── tests/test_x2llm.py       # offline: MockTransport, FakeClient, CliRunner, stdio roundtrip
├── Makefile
└── .env.example
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

### Required

| Variable | Description | How to get it |
|---|---|---|
| `X_AUTH_TOKEN` | Session secret (`auth_token` cookie) | DevTools → Cookies (see Getting Started) |
| `X_CT0` | CSRF token (`ct0` cookie) | same |

### Optional

| Variable | Description | Default |
|---|---|---|
| `X_COOKIE` | Full `Cookie` header verbatim (overrides the two vars; add `cf_clearance` here if Cloudflare challenges) | composed from the two above |
| `X_BEARER` | Web-client bearer token | current public one (in `registry.py`) |
| `X_USER_AGENT` | Should match the browser the cookies came from | Firefox/Linux UA |
| `X_API_BASE` | API root | `https://x.com/i/api` |
| `X_TIMEOUT` | Per-request timeout, seconds | `20` |
| `X_RATE_LIMIT` | Max requests per minute, client-enforced (0 = unlimited) | `30` |
| `X_TXIDS` | JSON `{"search": "<x-client-transaction-id>", ...}` — per-operation transaction ids; only some endpoints need them and they expire within hours. Written to `.env.fresh` by `make refresh`. | `{}` |

The `Makefile` auto-exports `.env`; for other entry points source it yourself
or use something like `direnv`.

## CLI Reference

| Command | Description |
|---|---|
| `x2llm home [--tab for_you\|following] [--limit N] [--cursor C]` | Home timeline; scroll with the returned cursor |
| `x2llm badges` | Unread notification/DM counts — cheapest auth check |
| `x2llm user SCREEN_NAME` | Profile lookup: bio, url, follower counts, verification |
| `x2llm me` | Viewer id + follower counts |
| `x2llm suggest QUERY` | Typeahead topics/users for a prefix |
| `x2llm search QUERY [--limit N] [--product Top\|Latest\|Media] [--cursor C]` | Full search with X operators |
| `x2llm tweet ID [--cursor C]` | Tweet + reply thread |
| `x2llm notifications [--limit N] [--cursor C]` | Notification tab |
| `x2llm analytics [--days N]` | Engagement statistics (1–28 days) |
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

`x2llm mcp` speaks MCP over stdio with 11 tools — the same cores as the CLI.
Ten are annotated `read_only_hint` (`get_home_timeline`, `get_user`,
`get_badges`, `me`, `suggest`, `search`, `get_tweet`, `get_notifications`,
`get_analytics`, `get_following`); the eleventh, `like_tweet`, is the one write and is registered with `read_only_hint=false`
and a description that instructs agents to call it only on explicit user
request. Liking an already-liked tweet returns the server's idempotent
rejection (`{"result": "error", "detail": "... has already favorited ..."}`)
— no state changes.

Wire it into an MCP client config (example for Claude-style configs):

```json
{
  "mcpServers": {
    "x": {
      "command": "/absolute/path/to/x2llm/.venv/bin/x2llm",
      "args": ["mcp"],
      "env": {
        "X_AUTH_TOKEN": "…",
        "X_CT0": "…"
      }
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

The package ships only **public, session-independent constants**
(`src/x2llm/registry.py`: query ids, features blobs, fieldToggles — identical
for every user, taken from x.com's JS bundles; they rotate on a months scale).
Everything session-scoped comes from the environment: `X_AUTH_TOKEN`,
`X_CT0`, `X_TXIDS`. **Transaction ids expire fast (observed: tens of minutes
to hours)** and only some endpoints enforce them — `search` is the one that
hurts. Nothing user-specific ever enters the package, so it is safe to publish.
No file inside the repo is machine-written: refresh produces `registry.new.py`
(gitignored) plus a diff for you to review and move over by hand.

Refreshes are **incremental**: entries in the HARs you pass win; operations
they don't contain keep their previous query ids from the existing
`endpoints.py`. One small HAR that only captured a search is enough — the run
fails only if the merged result is still incomplete. The generator prints
which endpoints were refreshed vs kept, and writes `.env.fresh` with the
session's `X_AUTH_TOKEN`, `X_CT0` and `X_TXIDS` (quoted so both
`source .env` and the Makefile include parse it).

When `search` or `like` starts failing with an empty 404 (the error text says
so):

1. Open x.com logged in → `F12` → Network → filter `i/api`
2. Visit: the **home timeline** (badges), run one **search**, open one
   **tweet**, open **notifications**, open **x.com/analytics**, open one
   **user profile**, **like** one tweet, **scroll both home tabs** (FOR YOU
   and FOLLOWING) two screens (that touches all nine operations)
3. Export HAR: Network ⚙️ gear → *Save all as HAR*
4. Harvest:

```bash
make refresh HAR='../x.com_Archive [26-10-07 13-27-31].har'
# prints a unified diff; then, if it looks right:
mv registry.new.py src/x2llm/registry.py && make check
cp .env.fresh .env  # your fresh session credentials + X_TXIDS
```

While `search`/`like` are down, everything else keeps working — tweet,
notifications, analytics, following, suggest, both home tabs and user lookups
work without any transaction id at all.
Features blobs and fieldToggles are captured per endpoint (x.com ships
several variants). The generator also extracts `X_AUTH_TOKEN`/`X_CT0` from
the capture's cookies and writes them to `.env.fresh` (it never touches an
existing `.env`; review and `cp .env.fresh .env`). Both files hold live
session secrets and are gitignored. The generator accepts several HAR files
and merges them, so captures can be split across sessions:

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

Your `auth_token`/`ct0` are stale (logged out elsewhere? password change?
long-lived capture?). Re-extract both from the browser. If your IP/UA changed
since capture, also set `X_COOKIE` with the full header including
`cf_clearance` and a matching `X_USER_AGENT`.

### `{"error": "missing environment variables: X_AUTH_TOKEN, X_CT0"}`

Self-explanatory — `.env` not loaded or empty. The Makefile auto-exports it;
elsewhere `source .env && export X_AUTH_TOKEN X_CT0`.

### Empty body with a `cf-ray` header

Cloudflare edge rejection, not x.com. Keep the same User-Agent as the browser
you captured from; if it persists, copy the full `Cookie` header into
`X_COOKIE`.

### `author: null` in results

x.com is A/B-testing a new user-object shape. `parse.py` reads both the old
(`legacy`) and new (`core`) paths; if both are null you are looking at a third
shape — file it with a response sample.

## Security Notes

> [!CAUTION]
> `auth_token` is a full-account bearer credential: read **and write** access
> until it expires. A `.env`, a shell history line, or a HAR file containing
> it must be treated like a password file. HAR exports additionally contain
> every response body the browser saw. `*.har` is gitignored for a reason —
> keep it that way.

This project is API-usage of your own logged-in session for personal tooling
(reads + likes you explicitly request). Respect x.com's terms of service and rate limits; the
client sends plain GETs at human-scale speeds and has no retry hammering.

## License

Internal tool, no license granted. All rights reserved.
