# vault-search

Read-only search interface for a private knowledge vault, with a bundled MCP server. It is intended for coding agents: find a page or section, read only what is needed, and keep context small.

This repository holds the search interface, the shared secret detector, a local FTS5 indexer with optional hybrid search, and a Streamable HTTP MCP server. The vault itself is a separate private checkout and is **never** committed here. Milestones 4–6 are implemented in this tree. The v1 deploy is **keyword-only** (full-text) search: install without the `[semantic]` extra. Meaning-based search can be turned on later by installing `.[semantic]` and reindexing; see [DEPLOY.md](DEPLOY.md).

## Non-goals

- Writing back into the vault (no ingest or "add this note" tool).
- Exposing the service on the public internet.
- Hiding or filtering pages by access control. Project names and tags are optional search filters, not permissions. Every authorised caller can search the full vault.
- Building a wider context gateway. Tool names and result shapes are kept stable so a gateway can front this service later.
- Shipping vault notes, hostnames, overlay-network details, addresses, or credentials in this repository.

## Tool contract

All tools are read-only. Every text field is secret-masked before it is written to the index, and every response is masked again on the way out. Results are compact markdown (path, title, heading breadcrumb, projects/tags, snippet) rather than raw JSON. There are no ids, raw scores, or UUIDs.

The Python API (`Client`) is what the MCP server wraps. The v1 deploy does not install embeddings, so search is keyword (full-text) only. The API default `mode=hybrid` fuses full-text with local semantic search (RRF, k=60) **when embeddings exist**; if the `[semantic]` extra is not installed, or the index has no vectors, `hybrid` and `semantic` fall back to full-text. `mode=fulltext` and `mode=semantic` remain explicit overrides. Filters apply to every mode. Do not treat a hybrid fallback as a semantic result.

| Tool | Arguments | Result |
| --- | --- | --- |
| `search` | `query: string`, `mode: "hybrid" \| "fulltext" \| "semantic" = "hybrid"`, `project?: string`, `tags?: string[]`, `type?: string`, `limit: int = 8` | Markdown hit list. Default `limit` is 8, hard cap 20. `hybrid` falls back to full-text when embeddings are not installed. |
| `read_note` | `path: string`, `section?: string` | Markdown header, links, backlinks, and the named section, the body, or a heading outline if the body is long. `not_found` only when the path is genuinely missing. |
| `list_notes` | `project?: string`, `tags?: string[]`, `type?: string`, `limit: int = 20`, `after?: string` | Markdown list. `after` is the last path, not an opaque cursor. |
| `list_projects` | _(none)_ | Markdown list of every project (name and one-line description). |

`path` is a vault-relative wiki path such as `wiki/concepts/example.md`.

On a v1 (keyword-only) install, `search` returns full-text hits even if the caller passes `mode=hybrid`. `mode` is otherwise for debugging once semantic search is enabled. Optional `project` / `tags` / `type` filters narrow results; they do not hide pages from an unfiltered call.

`read_note` of a long page (over about 8,000 characters) without `section` returns a heading outline instead of the body.

## Configuration

Runtime configuration comes from the environment (or a gitignored `.env`). This repository contains placeholders only. On the VPS, the filled-in file lives at `/etc/vault-search/env` and is not committed. See [DEPLOY.md](DEPLOY.md).

| Variable | Purpose |
| --- | --- |
| `VAULT_ROOT` | Path to the local vault checkout. That checkout is not part of this repo. `VAULT_PATH` is accepted as an alias if `VAULT_ROOT` is unset. |
| `INDEX_PATH` | Path to the local SQLite FTS5 index. Defaults to `index/vault.db` under the current directory if unset (gitignored). |
| `INDEX_INCLUDE_RAW` | Set to `1` to also index `raw/`. Off by default. |
| `EMBEDDING_MODEL` | Unused in the v1 keyword-only deploy. Default `BAAI/bge-small-en-v1.5` when semantic search is enabled later. |
| `EMBEDDING_CACHE_DIR` | Unused in v1. Directory for downloaded ONNX model files (gitignored) once semantic search is enabled. |
| `EMBEDDING_MODEL_PATH` | Unused in v1. Optional path to a pre-fetched model directory. Vault text is not sent to a hosted embedding API. |
| `INDEX_INCLUDE_EXAMPLES` | Set to `1` to index `wiki/_examples` and pages marked `example: true`. Off by default. |
| `AUDIT_LOG_PATH` | Path to the append-only audit log. |
| `LISTEN_HOST` | Bind address. Must be `127.0.0.1` in production; the reverse proxy listens on the tailnet. Binding `0.0.0.0` or `::` is refused unless `LISTEN_ALLOW_NONLOCAL=1`. |
| `LISTEN_ALLOW_NONLOCAL` | Set to `1` only if you really mean to bind a non-loopback address. Leave unset in production. |
| `LISTEN_PORT` | Bind port on loopback. |
| `MCP_PATH` | URL path the proxy mounts, default `/vault/mcp`. The public endpoint is `https://<vps>.<tailnet>.ts.net/vault/mcp` (placeholders only). |
| `PUBLIC_BASE_URL` | Tailnet-only base URL, for example `https://<vps>.<tailnet>.ts.net`. Required behind the proxy: the server accepts this `Host` header (the MCP SDK otherwise answers 421 to non-localhost hosts). Use the real MagicDNS name only in the on-box env file. |

Copy `.env.example` to `.env` when running locally. Never commit `.env` or any filled-in values.

## Security

**No vault content in this repo.** Notes, search indexes, model caches, and `.env` files are gitignored. Tests use synthetic fixtures, not vault pages. Documentation uses placeholder names only.

**Secrets must never be stored in the vault.** This repo is the home of the shared detector. Vault ingest lint should call the same `scan` API (pin a release, or vendor a copy). The indexer runs every text field through `mask` before writing it to SQLite (body, chunks, FTS, titles, breadcrumbs, front matter, and tags). Optional embeddings are computed from that already-masked text. Query output is masked again as a second layer. This is defence in depth, not a licence to store secrets.

**Detector API and CLI**

```python
from vault_search.secrets import scan, mask

findings = scan(text)          # rule, start, end, line, column; never the secret value
safe = mask(text)              # replaces spans with [REDACTED:<rule>]
```

```bash
python -m vault_search.secrets scan <paths>
```

The command exits `1` when it reports a finding, `0` when the paths are clean, and `2` on usage or missing-path errors. Output is `path:line:column: rule`. Matched values are never printed. A `mask` subcommand reads stdin and writes redacted text to stdout.

After building an index, scan the database itself (SQL text, FTS shadow tables, and raw file bytes, including WAL/journal sidecars):

```bash
python -m vault_search index-audit /path/to/index.db
```

The same exit codes apply. The command never prints matched values. A schema-version mismatch deletes the existing index file and rebuilds it from scratch so older unmasked rows cannot remain in free pages.

Rules cover common token formats (GitHub, OpenAI, Anthropic, AWS, Slack, Google), JWTs, private-key blocks, and high-entropy `key=` / `token=` (and similar) assignments.

**Audit log.** Every tool call is appended to the log at `AUDIT_LOG_PATH`: timestamp, caller identity, tool, arguments, `result_count`, and whether a secret-like span was masked (rule, path, span — not the secret value). Search calls that return no hits are logged with `result_count` 0 and `zero_hits` true. Vault Keeper uses those result counts to decide whether to enable semantic search later. Do not log secret values or vault body text. The log stays on the host; it is not committed here.

**Exposure and access.** The service is reachable only on a private tailnet, not the public internet. On the VPS, `tailscale serve` terminates TLS on the tailnet and forwards to nginx on loopback, which serves this app at `https://<vps>.<tailnet>.ts.net/vault/mcp` (placeholders; never commit real names). The nginx config lives in a separate git on the VPS, not in this repo. The systemd unit binds to `127.0.0.1` only. Agents connect over HTTPS, not SSH tunnels. Admin access is Tailscale SSH, governed by ACLs, not SSH keys. Funnel stays off.

Access control is the tailnet itself: every tailnet device may call the service, and the app does not authorise callers. It rejects only requests that carry `Tailscale-Funnel-Request`. If per-device restriction is needed later, put the service on its own tailnet port and restrict it with a Tailscale grant. (A WhoIs lookup and denylist were removed on 19 Nov 2026 as not needed yet; see the Decisions section in [DEPLOY.md](DEPLOY.md).)

**Caller details.** The audit log records the caller's tailnet IP, taken from `X-Real-IP` / `X-Forwarded-For` only when the TCP peer is loopback (nginx), plus a self-reported `X-Vault-Agent-Name` header marked **unverified**. The shared Grok Bot box used by all of Josh's agents is one device, so agents on it are told apart only by that unverified header.

This same shared box is Vault Keeper's **verification node** for the [DEPLOY.md](DEPLOY.md) completion criteria. A report from the VPS deploy executor is not acceptance.

## Deploy

Vault Keeper owns the code in this repository. Deployment on the VPS is done by pulling a release and following [DEPLOY.md](DEPLOY.md): service user, pull-only vault clone, loopback systemd unit, and an nginx `location /vault/mcp` in the on-box nginx git. The MCP server in this repo binds to loopback and is meant to sit behind the VPS nginx location. The runbook has checkable completion criteria for the VPS steps, which are still blocked on access.

Vault Keeper independently verifies a deploy from the shared Grok Bot box (the verification node; see [Caller details](#exposure-and-access)). A report from the VPS agent is not acceptance. For the externally visible checks (MCP `initialize` / `tools/list`, masked fixture query, public IP must fail):

```bash
# Real values only in the shell, never committed.
export PUBLIC_BASE_URL='https://<vps>.<tailnet>.ts.net'
export MCP_PATH=/vault/mcp
# optional:
# export PUBLIC_IP='<public-ip>'
# export VERIFY_FIXTURE_PATH='wiki/example.md'
# export VERIFY_SYNTHETIC_SECRET='...'   # assembled at runtime; never commit
python3 scripts/verify-deploy
```

## Local index and CLI

Requires a vault checkout path and an index path (see configuration). `wiki/` is indexed first; `raw/` is off unless `INDEX_INCLUDE_RAW=1` or `--include-raw`. Reindex is incremental (git SHA plus per-file mtimes and SHA-256). Tests use only synthetic fixtures under `tests/fixtures`; never point the test suite at a real vault.

```python
from vault_search import Client

with Client.from_env() as vault:
    vault.reindex()
    print(vault.search("evals", project="alpha"))
    print(vault.read_note("wiki/concepts/evals.md", section="Scoring"))
    print(vault.list_notes(type="concept", after="wiki/concepts/example.md"))
    print(vault.list_projects())
```

```bash
python -m vault_search index
python -m vault_search search "evals" --project alpha --limit 8
python -m vault_search search "evals" --mode fulltext
python -m vault_search read wiki/concepts/evals.md --section Scoring
python -m vault_search list-notes --type concept
python -m vault_search list-projects
python -m vault_search sync          # git pull --ff-only at VAULT_ROOT, then reindex
python -m vault_search.mcp           # Streamable HTTP on 127.0.0.1 (or `vault_search serve`)
python -m vault_search.secrets scan .
python -m vault_search index-audit index/vault.db
```

## Milestones

1. **Public repo and secret scanner** — done. Detector, tests, CI, pre-commit, deploy runbook.
2. **Access and inventory** — blocked on VPS access.
3. **Pull-only vault sync on the VPS** — blocked on VPS access.
4. **Index and CLI** — done in this tree. SQLite FTS5 over `wiki/`, heading chunks, backlinks, compact masked markdown, ingest-time secret masking, incremental reindex.
5. **Hybrid search** — done in this tree, optional. The v1 deploy is keyword-only (no `[semantic]` extra). Installing `fastembed` (`BAAI/bge-small-en-v1.5`, ONNX, CPU) and reindexing attaches vectors to `chunk_embeddings` and turns on RRF hybrid (k=60).
6. **MCP server** — done in this tree (loopback Streamable HTTP, audit log with caller IP, `scripts/verify-deploy`). Binding it to the VPS tailnet is still blocked on VPS access; see [DEPLOY.md](DEPLOY.md).
7. **Adapter-ready** — tool contract documented for a future gateway; no other data sources.

## Development

Requires Python 3.11 or later. The core indexer is stdlib plus the official `mcp` SDK, pinned to an exact version (also listed in the `dev` extra). Local embeddings are optional: `pip install '.[semantic]'` (fastembed/ONNX, not torch). The v1 deploy does not install that extra.

```bash
python -m pip install -e ".[dev]"
python -m pip install -e ".[semantic]"   # optional; not part of the v1 deploy
python -m pytest -q
python -m vault_search.secrets scan .
python -m vault_search index-audit index/vault.db   # after building a local index
```

A GitHub Actions workflow runs the tests and [gitleaks](https://github.com/gitleaks/gitleaks) on push and pull request. To run the same hook locally:

```bash
pre-commit install
pre-commit run --all-files
```

This project is licensed under the MIT License.
