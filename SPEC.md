# vault-search specification

Design and implementation notes for vault-search. For a short visitor introduction, see [README.md](README.md). For the production runbook, including checkable completion criteria, see [DEPLOY.md](DEPLOY.md).

The v1 deploy is keyword-only (full-text) search. Masking happens at ingest and again on output. `tailscale serve` terminates TLS, then nginx on loopback, then the app on loopback. A WhoIs lookup is not used.

## Non-goals

- Writing back into the vault (no ingest or "add this note" tool).
- Exposing the service on the public internet.
- Hiding or filtering pages by access control. Project names and tags are optional search filters, not permissions. Every authorised caller can search the full vault.
- Building a wider context gateway. Tool names and result shapes are kept stable so a gateway can front this service later.
- Shipping vault notes, hostnames, overlay-network details, addresses, or credentials in this repository.

## Architecture

This repository holds the search interface, the shared secret detector, a local FTS5 indexer with optional hybrid search, and a Streamable HTTP MCP server. The vault itself is a separate private checkout and is never committed here.

The production request path is:

```text
tailnet ──HTTPS──▶ tailscaled :443 ──HTTP──▶ nginx 127.0.0.1:8080 ──▶ vault-search 127.0.0.1:8787
                  (tailscale serve,           (location /vault/mcp)
                   TLS + *.ts.net cert)
```

`8080` and `8787` are the documented defaults; the app port is `LISTEN_PORT`. There is no `tailscale cert` step and no nginx listener on the tailnet IP. The nginx config lives in a separate git on the VPS, not in this repo. The systemd unit binds to `127.0.0.1` only. Agents connect over HTTPS, not SSH tunnels. Admin access is Tailscale SSH, governed by ACLs, not SSH keys. Funnel stays off.

The MCP server in this repo binds to loopback and is meant to sit behind the VPS nginx `location /vault/mcp`. Binding `0.0.0.0` or `::` is refused unless `LISTEN_ALLOW_NONLOCAL=1`.

Access control is the tailnet itself. Every tailnet device may call the service, and the app does not authorise callers. It rejects only requests that carry `Tailscale-Funnel-Request`. If per-device restriction is needed later, put the service on its own tailnet port and restrict it with a Tailscale grant. A WhoIs lookup and denylist were removed on 19 Nov 2026 as not needed yet. See the Decisions section in [DEPLOY.md](DEPLOY.md#decisions-19-nov-2026-approved-by-josh).

The public endpoint is `https://<vps>.<tailnet>.ts.net/vault/mcp` (placeholders only). Behind the proxy, `PUBLIC_BASE_URL` must be set to that tailnet-only base URL so the server accepts the proxied `Host` header. The MCP SDK otherwise answers 421 to non-localhost hosts. Use the real MagicDNS name only in the on-box env file.

Service user, pull-only vault clone, systemd unit, and the nginx location are in [DEPLOY.md](DEPLOY.md).

## Tool contract

All tools are read-only. Every text field is secret-masked before it is written to the index, and every response is masked again on the way out. Results are compact markdown (path, title, heading breadcrumb, projects/tags, snippet) rather than raw JSON. There are no ids, raw scores, or UUIDs.

The Python API (`Client`) is what the MCP server wraps. The v1 deploy does not install embeddings, so search is keyword (full-text) only. The API default `mode=hybrid` fuses full-text with local semantic search (RRF, k=60) when embeddings exist. If the `[semantic]` extra is not installed, or the index has no vectors, `hybrid` and `semantic` fall back to full-text. `mode=fulltext` and `mode=semantic` remain explicit overrides. Filters apply to every mode. Do not treat a hybrid fallback as a semantic result.

| Tool | Arguments | Result |
| --- | --- | --- |
| `search` | `query: string`, `mode: "hybrid" \| "fulltext" \| "semantic" = "hybrid"`, `project?: string`, `tags?: string[]`, `type?: string`, `limit: int = 8` | Markdown hit list. Default `limit` is 8, hard cap 20. `hybrid` falls back to full-text when embeddings are not installed. |
| `read_note` | `path: string`, `section?: string` | Markdown header, links, backlinks, and the named section, the body, or a heading outline if the body is long. `not_found` only when the path is genuinely missing. |
| `list_notes` | `project?: string`, `tags?: string[]`, `type?: string`, `limit: int = 20`, `after?: string` | Markdown list. `after` is the last path, not an opaque cursor. |
| `list_projects` | _(none)_ | Markdown list of every project (name and one-line description). |

`path` is a vault-relative wiki path such as `wiki/concepts/example.md`.

On a v1 (keyword-only) install, `search` returns full-text hits even if the caller passes `mode=hybrid`. `mode` is otherwise for debugging once semantic search is enabled. Optional `project` / `tags` / `type` filters narrow results; they do not hide pages from an unfiltered call.

`read_note` of a long page (over about 8,000 characters) without `section` returns a heading outline instead of the body.

## Indexing and search

`wiki/` is indexed first. `raw/` is off unless `INDEX_INCLUDE_RAW=1` or `--include-raw`. Reindex is incremental (git SHA plus per-file mtimes and SHA-256). Tests use only synthetic fixtures under `tests/fixtures`. Never point the test suite at a real vault.

The index is SQLite FTS5 over `wiki/`, with heading chunks, backlinks, and compact masked markdown. Hybrid search is implemented in this tree but optional. Installing `fastembed` (`BAAI/bge-small-en-v1.5`, ONNX, CPU) and reindexing attaches vectors to `chunk_embeddings` and turns on RRF hybrid (k=60). Optional embeddings are computed from already-masked text. Vault text is not sent to a hosted embedding API.

A schema-version mismatch deletes the existing index file and rebuilds it from scratch so older unmasked rows cannot remain in free pages. After a schema rebuild, a long-running server reopens the new file on its next query; no restart is needed for that case.

## Configuration

Runtime configuration comes from the environment (or a gitignored `.env`). This repository contains placeholders only. On the VPS, the filled-in file lives at `/etc/vault-search/env` and is not committed. Production values and file permissions are in [DEPLOY.md](DEPLOY.md#5-environment-configuration).

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
| `LISTEN_PORT` | Bind port on loopback. Default `8787`. |
| `MCP_PATH` | URL path the proxy mounts, default `/vault/mcp`. The public endpoint is `https://<vps>.<tailnet>.ts.net/vault/mcp` (placeholders only). |
| `PUBLIC_BASE_URL` | Tailnet-only base URL, for example `https://<vps>.<tailnet>.ts.net`. Required behind the proxy: the server accepts this `Host` header (the MCP SDK otherwise answers 421 to non-localhost hosts). Use the real MagicDNS name only in the on-box env file. |

Copy `.env.example` to `.env` when running locally. Never commit `.env` or any filled-in values.

## Secrets and masking

No vault content belongs in this repo. Notes, search indexes, model caches, and `.env` files are gitignored. Tests use synthetic fixtures, not vault pages. Documentation uses placeholder names only.

Secrets must never be stored in the vault. This repo is the home of the shared detector. Vault ingest lint should call the same `scan` API (pin a release, or vendor a copy). The indexer runs every text field through `mask` before writing it to SQLite (body, chunks, FTS, titles, breadcrumbs, front matter, and tags). Optional embeddings are computed from that already-masked text. Query output is masked again as a second layer. This is defence in depth, not a licence to store secrets.

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

The same exit codes apply. The command never prints matched values.

Rules cover common token formats (GitHub, OpenAI, Anthropic, AWS, Slack, Google), JWTs, private-key blocks, and high-entropy `key=` / `token=` (and similar) assignments.

## Audit log and callers

Every tool call is appended to the log at `AUDIT_LOG_PATH`: timestamp, caller identity, tool, arguments, `result_count`, and whether a secret-like span was masked (rule, path, span, not the secret value). Search calls that return no hits are logged with `result_count` 0 and `zero_hits` true. Vault Keeper uses those result counts to decide whether to enable semantic search later. Do not log secret values or vault body text. The log stays on the host; it is not committed here.

The audit log records the caller's tailnet IP, taken from `X-Real-IP` / `X-Forwarded-For` only when the TCP peer is loopback (nginx), plus a self-reported `X-Vault-Agent-Name` header marked unverified. The shared Grok Bot box used by all of Josh's agents is one device, so agents on it are told apart only by that unverified header.

On-box audit checks are in [DEPLOY.md](DEPLOY.md#8-caller-details-and-audit-log).

## Ownership and verification

Vault Keeper owns the design and the code in this repository. Deployment on the VPS is done by pulling a release and following [DEPLOY.md](DEPLOY.md): service user, pull-only vault clone, loopback systemd unit, and an nginx `location /vault/mcp` in the on-box nginx git.

Every deploy step in that runbook ends with checkable completion criteria. A report from the VPS deploy executor is not acceptance. Vault Keeper independently verifies a deploy from the shared Grok Bot box (the verification node). For the externally visible checks (MCP `initialize` / `tools/list`, masked fixture query, public IP must fail):

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

The full end-to-end checks, including on-box commands, are in [DEPLOY.md](DEPLOY.md#9-end-to-end-verification).

## Local index and CLI

Requires a vault checkout path and an index path. See [Configuration](#configuration).

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

1. **Public repo and secret scanner.** Done. Detector, tests, CI, pre-commit, deploy runbook.
2. **Access and inventory.** Procedures are in [DEPLOY.md](DEPLOY.md). Inventory (host names, addresses) stays off this repository.
3. **Pull-only vault sync on the VPS.** Procedures are in [DEPLOY.md](DEPLOY.md).
4. **Index and CLI.** Done in this tree. SQLite FTS5 over `wiki/`, heading chunks, backlinks, compact masked markdown, ingest-time secret masking, incremental reindex.
5. **Hybrid search.** Done in this tree, optional. The v1 deploy is keyword-only (no `[semantic]` extra). Installing `fastembed` (`BAAI/bge-small-en-v1.5`, ONNX, CPU) and reindexing attaches vectors to `chunk_embeddings` and turns on RRF hybrid (k=60).
6. **MCP server.** Done in this tree (loopback Streamable HTTP, audit log with caller IP, `scripts/verify-deploy`). In production, `tailscale serve` terminates TLS, nginx listens on loopback, and the app listens on loopback. See [DEPLOY.md](DEPLOY.md).
7. **Adapter-ready.** Tool contract documented for a future gateway; no other data sources.
