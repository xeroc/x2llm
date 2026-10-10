"""The `x2llm prime` payload: a skill-style briefing for AI agents.

One flat markdown document - the common surface, examples, and the error
dictionary - so an agent can use x2llm without reading the README. The
prime test asserts every command referenced here actually exists.
"""

PRIME_MD = """\
# x2llm - agent briefing

Read-only x.com (Twitter) client for AI agents. Multi-account; credentials
live in a local sqlite db (`~/.local/share/x2llm/accounts.db`), never in
env vars. Surfaces: this CLI, a stdio MCP server (`x2llm mcp`), a Python
library (`x2llm` package). This is the one command that prints plain
markdown instead of JSON - pipe it into your context and go.

## Ground rules

- Every other command prints exactly ONE JSON document.
- Errors print `{"error": "..."}` and exit non-zero. Never retry blindly -
  the error text says what to do.
- Pagination: every list command returns a `cursor`; feed it back via
  `--cursor` for the next page; `null` means end.
- Two REAL write actions exist (`like`, `post`) - see below.

## Accounts

- `x2llm accounts list` - stored screen names, last refresh, default flag.
- Omit `-u/--user` to act as the default account (marked in `accounts list`).
- `x2llm -u NAME <command>` - act as another stored account.
- `x2llm accounts default NAME` - move the default.
- `x2llm import capture.har [--user NAME]` - import/refresh an account from
  a browser HAR capture (identity is derived from the capture itself;
  `--user` is verified live against the session before storing).

## Commands you will actually use

| Command | What it does |
|---|---|
| `x2llm badges` | Unread notification/DM counts - cheapest auth check |
| `x2llm me` | Viewer identity + follower counts |
| `x2llm user NAME` | Profile: bio, url, follower counts, verification |
| `x2llm search QUERY` | Full search with X operators (see below) |
| `x2llm tweet ID` | One tweet plus its reply thread |
| `x2llm home` / `--tab following` | Home timeline (algorithmic / chronological) |
| `x2llm notifications` | The account's notification tab |
| `x2llm following USER` | Accounts USER follows (id or screen name) |
| `x2llm suggest PREFIX` | Search typeahead: topics + users |
| `x2llm analytics` | Own engagement stats (`--days 1-28`) |
| `x2llm accounts list` / `default NAME` / `remove NAME` | Manage stored accounts |
| `x2llm import HAR [--user NAME]` | Import an account from a capture |
| `x2llm mcp` | Run as stdio MCP server |

Search operators inside QUERY: `from:user`, `lang:en`, `min_faves:100`,
`min_retweets:10`, `since:2026-01-01`. Combine freely.

## Examples

```bash
x2llm badges                                    # auth check, default account
x2llm search "solana lang:en min_faves:100" --limit 10
x2llm search "solana lang:en min_faves:100" --limit 10 --cursor 'DAACCgACHUB...'
x2llm user vitalikbuterin                       # @ is stripped automatically
x2llm tweet 1580243534825066496                 # tweet + replies
x2llm home --tab following --limit 30
x2llm -u work_account notifications             # act as another account
x2llm accounts list                             # discovery: what is stored?
```

## REAL actions - only on explicit user request

`x2llm like TWEET_ID` and `x2llm post TEXT [--reply-to ID]` are public,
permanent, and notify people. Never call them unless the user explicitly
asked for that exact action. Everything else is read-only.

## Error dictionary

| Error contains | Meaning | Do this |
|---|---|---|
| `HTTP 401/403` + "credentials rejected" | session expired | ask user to re-capture a HAR, then `x2llm import` |
| empty `404` + "transaction-id expired" (search/like) | txids stale | same: re-capture + `x2llm import` |
| `no such account: NAME` | unknown account | run `x2llm accounts list` |
| `no default account set` | marker missing | `x2llm accounts default NAME` |
| `no accounts stored yet` | empty db | `x2llm import capture.har` |
| `429` rate limited | too fast | slow down; or raise `X_RATE_LIMIT` |

## MCP server

`x2llm mcp` exposes the same cores as 13 tools. Every tool takes an
optional `username` (omitted = default account); the `accounts` tool lists
what is stored. Wire it into any MCP client config with command `x2llm`
and args `["mcp"]` - no credentials in the config.

## Optional env knobs (not credentials)

`X_API_BASE` (default https://x.com/i/api), `X_TIMEOUT` (20s),
`X_RATE_LIMIT` (30 req/min, 0 = unlimited).
"""
