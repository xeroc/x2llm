# x2llm

x.com (Twitter) client for LLM agents. Multiple accounts in a local sqlite
store, three faces: a Python library, a `click` CLI, and a stdio MCP
server. Built for machines first: every command prints exactly one JSON
document (`x2llm prime` is the one markdown exception - an agent briefing),
every error prints `{"error": "..."}` and exits non-zero. Reads plus
three deliberate writes: `post`, `like`, and `dm-send` — each marked
REAL everywhere and gated on explicit user intent.

## Key Features

- Search (full operator support: `from:user`, `since:`, `lang:`, `min_faves:` ...)
- Multiple accounts in a local store: `--user` on the CLI, `username` on MCP tools
- Tweet + reply-thread retrieval
- Full article reading: `x2llm tweet <id>` on an article returns title, summary, cover and the whole body rendered as markdown (headers, images, dividers) under `article`
- Notification tab reading
- Account analytics (impressions, likes, follows — your own statistics page)
- Follower/following graphs, typeahead suggestions, unread badge counts
- Post a tweet, like a tweet (write actions, explicit everywhere)
- Direct messages: inbox (`dms`), thread history (`dm`), sending (`dm-send`)
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

The account is identified from the capture itself: its `twid` session cookie carries your numeric user id, matched against user objects in the responses — no live call needed. Captures that never name the viewer fall back to a live lookup: x.com's public intent page resolves the `twid` id to a screen name, and that name is **verified** live (profile rest_id ↔ twid id) before anything is stored. `--user NAME` skips straight to explicit filing under that name, with the same live verification — no way to file credentials under the wrong user.

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
│   ├── cores.py         # 15 async service cores, shared by CLI and MCP
│   ├── cli.py           # click commands, one JSON doc per command
│   └── mcp.py           # stdio MCP server wrapping the same cores
├── tests/test_x2llm.py  # offline: MockTransport, FakeClient, CliRunner, stdio roundtrip
└── Makefile
```

### Request lifecycle

```
click command / MCP tool
        │
        ▼
cores.py  (search, tweet, notifications, … like, dm-send — dicts in, dicts out)
        │
        ▼
XClient.graphql(key, variables)          — reads
XClient.graphql_post(key, variables)    — POST GraphQL (home feed, like/post writes)
XClient.rest(path, params)               — legacy REST reads (dms, dm threads)
XClient.rest_post(path, body)            — legacy REST write (dm-send)
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
parse.py walkers (walk / collect_tweets / collect_cursors / dm_conversation_rows)
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
  header is. Wire data comes from `registry.py` defaults, overridden per
  operation by the account db's wire table (written by `x2llm import`).
- **Cores are transport-agnostic.** They call `graphql(key, variables)` /
  `graphql_post(key, variables)` / `rest(path, params)` /
  `rest_post(path, body)` — anything with those four async methods works
  (see `FakeClient` in the tests). This is what keeps the suite offline.
- **One code path.** CLI handlers and MCP tools bind the same core functions;
  behavior can never drift between the two surfaces.
- **Writes are isolated.** Read feeds are GET, except POST-fed timelines
  (home — the browser itself scrolls via POST). Writes are `like`/`post`
  (GraphQL POST) and `dm-send` (REST POST). The MCP server registers all
  three with `read_only_hint=false` and loud descriptions.
- **One pagination model.** Every list core (`home`, `search`, `tweet`,
  `notifications`, `following`, `dm`, `dms`) returns a `cursor`; feed it
  back for the next page, `null` means end. The home timeline has two tabs
  served by different operations: `for_you` (HomeTimeline, algorithmic)
  and `following` (HomeLatestTimeline, chronological) — cursors are
  tab-specific, pass back what the same tab returned. `dms` pages across
  two stacks: page 1 is the legacy 1.1 inbox (~30 conversations, plaintext
  previews, unread flags); deeper pages walk the XChat inbox on
  api.x.com GraphQL (50/page, every conversation ever, E2E-encrypted
  bodies so previews are metadata-only).

## Environment Variables

### Env-Injected Credentials (storeless deployments)

The default account db is a machine-local sqlite file. To deploy the MCP
server (or CLI) somewhere you don't want to ship that file, inject the
session cookies via the environment instead:

| Variable | Required | Description |
|---|---|---|
| `X_AUTH_TOKEN` | yes (with `X_CT0`) | The `auth_token` session cookie — this IS the account |
| `X_CT0` | yes (with `X_AUTH_TOKEN`) | The `ct0` csrf cookie |
| `X_ACCOUNT` | no | Screen-name label for the account (default `env`); tool `username` params must match it |
| `X_BEARER` | no | Override the public web-client bearer |
| `X_COOKIE` | no | Full cookie header override (e.g. to add `cf_clearance`) |
| `X_USER_AGENT` | no | Override the default Firefox user agent |

Env credentials win over any store, and `search`/`like` still work —
transaction ids are minted on demand by txgen (there is simply no capture
pool). Get the two cookie values from a logged-in browser (same place the
HAR capture gets them: DevTools → Storage → Cookies for x.com); they expire
on the same timescale as stored ones, so rotate by updating the env, not by
re-importing.

### Postgres Store

Set `DATABASE_URL` (any libpq conninfo or URI) and the account store moves
to postgres — same schema, same SQL, same commands (`import`, `accounts`,
`--user`, MCP `username` all work unchanged). sqlite stays the default.

```console
$ pip install 'x2llm[postgres]'   # pulls psycopg
$ export DATABASE_URL='postgresql://user:pass@db.example.com:5432/x2llm'
$ x2llm import capture.har
```

The store is credential-shaped, not multi-writer-shaped: it opens one
connection per operation (`# ponytail:` in store.py — pool it if store
throughput ever matters; the X API rate limiter dominates anyway). Give the
role exactly these privileges and nothing more:

```sql
CREATE ROLE x2llm LOGIN PASSWORD '...';
CREATE DATABASE x2llm OWNER x2llm;
-- optional hardening: read/write on the three tables, no DDL after first run
-- REVOKE ALL ON DATABASE x2llm FROM PUBLIC; etc.
```

Precedence: `X_AUTH_TOKEN`/`X_CT0` (if set) beat `DATABASE_URL`, which beats
the local sqlite file. Tables are created idempotently on first connect; the
db holds live session secrets, so treat the postgres database like the
sqlite file: restricted access, no public schemas, TLS in transit.

To run the postgres dialect-conformance tests against a throwaway db:

```console
$ uv sync --extra postgres --inexact
$ DATABASE_URL='postgresql://.../x2llm_test' uv run --extra postgres pytest tests/test_store_pg.py
```

### Infrastructure knobs

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
| `x2llm dms [--cursor C]` | DM inbox; `--cursor` walks deeper pages (E2EE: metadata-only previews beyond page 1) |
| `x2llm dm CONVERSATION_ID [--cursor C]` | One DM thread, oldest to newest |
| `x2llm like TWEET_ID` | **Like a tweet — real, public, notifies the author** |
| `x2llm post TEXT [--reply-to ID]` | **Post a tweet — real, public, permanent** |
| `x2llm dm-send USER_or_ID TEXT` | **Send a DM — real, private, notifies the recipient, cannot be unsent** |
| `x2llm mcp` | Run as stdio MCP server |

All pagination works the same way: the response carries a `cursor`; feed it
back via `--cursor` for the next page (`null` means end). For `dms`, page 1
carries plaintext previews; deeper pages (XChat inbox) list every
conversation ever but with metadata-only previews — message bodies there
are end-to-end encrypted.

Example — search, take the cursor, get page two:

```bash
uv run x2llm search "solana lang:en min_faves:100" --limit 10
uv run x2llm search "solana lang:en min_faves:100" --limit 10 --cursor 'DAACCgACHUB…'
```

## MCP Server

`x2llm mcp` speaks MCP over stdio with 16 tools — the same cores as the CLI,
plus `accounts`. Account selection is per tool call: every tool takes an
optional `username` parameter (omitted = the default account, marked by the
`accounts` tool; an unset default is an error that says how to pick one —
see the CLI Reference above). Fourteen tools are
annotated `read_only_hint` (`accounts`, `get_home_timeline`, `get_user`,
`get_badges`, `me`, `suggest`, `search`, `get_tweet`, `get_notifications`,
`get_analytics`, `get_following`, `list_dm_conversations`,
`get_dm_conversation`); `post_tweet`, `like_tweet` and `send_dm` are the
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

Everything that ages lives in the local account db — nothing in the repo
is machine-written, and no maintenance release is ever needed for wire
changes:

- **account rows** — auth_token, ct0, bearer, cookie, user-agent and the
  transaction-id pool. Transaction ids are **path-bound** (a search request
  rejects a home-path id with an empty 404) and expire after tens of
  minutes; imports store only ids captured on their own operation, and the
  `txgen` module mints fresh path-correct ones for everything else.
- **wire rows** — per-operation query ids, features blobs and
  fieldToggles harvested from the capture. They override the shipped
  defaults in `registry.py` per operation, so x.com bundle rotations are
  absorbed by the next import.

When `search` or `like` starts failing with an empty 404, or credentials
go stale:

1. Open x.com logged in as the account to refresh → `F12` → Network →
   filter `i/api`
2. Visit: the **home timeline** (badges), run one **search**, open one
   **tweet**, open **notifications**, open **x.com/analytics**, open one
   **user profile**, **like** one tweet, **scroll both home tabs** (FOR YOU
   and FOLLOWING) two screens (that touches all operations)
3. Export HAR: Network ⚙️ gear → *Save all as HAR*
4. Import:

```bash
x2llm import '../x.com_Archive [26-10-10 09-19-00].har'
# -> {"imported": "xer0c", "txids": 10, "wire_updated": ["home", "search", ...]}
```

Identity comes from the capture's `twid` cookie (user objects in the responses); captures that never name the viewer are resolved live via x.com's intent page (twid id → screen name, verified against the session), and `--user NAME` files explicitly with the same verification.
One account per run: capture each browser profile separately. Several HAR
files merge in one run: `x2llm import reads.har like.har`.

Ops without a captured id mint one via `txgen` (path-correct, always fresh),
so a capture that never ran a search still searches. `search`/`like` only
degrade if `txgen` itself breaks (x.com bundle change) — then a capture
covering all operations (below) restores them.

## Docker & Releases

The image (`ghcr.io/xeroc/x2llm`, multi-stage: wheel built with uv, installed
into `python:3.12-slim` with the postgres extra, runs as non-root `x2llm`)
ships the CLI and the stdio MCP server — `docker run ghcr.io/xeroc/x2llm`
defaults to `x2llm mcp`. No store is baked in; configure the same two ways
as everywhere else: env credentials or `DATABASE_URL`.

Point an MCP client at the container:

```json
{
  "mcpServers": {
    "x2llm": {
      "command": "docker",
      "args": ["run", "-i", "--rm",
               "-e", "X_AUTH_TOKEN", "-e", "X_CT0",
               "ghcr.io/xeroc/x2llm:latest"]
    }
  }
}
```

Releasing (requires a clean tree, `docker login ghcr.io`, and push rights):

```console
$ make release              # bump patch, uv lock, commit, tag vX.Y.Z,
                            # build+push image (:X.Y.Z and :latest), push git
$ make release BUMP=minor   # or major
$ make docker               # build-only, tagged :dev
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

The account's credentials are stale (logged out elsewhere? password
change? long-lived capture?). Re-capture and `x2llm import` per the section
above — the harvest stores the full `Cookie` header (including
`cf_clearance`) and the capture browser's user-agent, which covers the
IP/UA-drift cases automatically.

### `{"error": "no accounts stored yet …"}`

The account db is empty — run `x2llm import capture.har` first (see
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
