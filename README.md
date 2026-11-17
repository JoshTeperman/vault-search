# vault-search

Read-only search interface for a private knowledge vault, with a bundled MCP server. It is intended for coding agents: find a page or section, read only what is needed, and keep context small.

This repository holds the interface and the shared secret detector. The vault itself is a separate private checkout and is **never** committed here. Milestone 1 is the scaffold and secret detector only; indexing, search, and the MCP server come later.

## Non-goals

- Writing back into the vault (no ingest or "add this note" tool).
- Exposing the service on the public internet.
- Hiding or filtering pages by access control. Project names and tags are optional search filters, not permissions. Every authorised caller can search the full vault.
- Building a wider context gateway. Tool names and result shapes are kept stable so a gateway can front this service later.
- Shipping vault notes, hostnames, overlay-network details, addresses, or credentials in this repository.

## Planned tool contract

All tools are read-only. Every response is run through the secret masker. Results are compact markdown (path, title, heading breadcrumb, projects/tags, snippet) rather than raw JSON.

| Tool | Arguments | Result |
| --- | --- | --- |
| `search` | `query: string`, `mode: "hybrid" \| "fulltext" \| "semantic" = "hybrid"`, `project?: string`, `tags?: string[]`, `type?: string`, `limit: int = 8` | Markdown hit list. Default `limit` is 8, hard cap 20. |
| `read_note` | `path: string`, `section?: string` | Markdown header, links, backlinks, and the named section, the body, or a heading outline if the body is long. `not_found` only when the path is genuinely missing. |
| `list_notes` | `project?: string`, `tags?: string[]`, `type?: string`, `limit: int = 20`, `after?: string` | Markdown list. `after` is the last path, not an opaque cursor. |
| `list_projects` | _(none)_ | Markdown list of every project (name and one-line description). |

`path` is a vault-relative wiki path such as `wiki/concepts/example.md`.

Default `search` is hybrid (full-text plus local semantic search, fused). `mode` is for debugging. Optional `project` / `tags` / `type` filters narrow results; they do not hide pages from an unfiltered call.

These tools are not implemented in this milestone.

## Configuration

Runtime configuration comes from the environment (or a gitignored `.env`). This repository contains placeholders only. On the VPS, the filled-in file lives at `/etc/vault-search/env` and is not committed. See [DEPLOY.md](DEPLOY.md).

| Variable | Purpose |
| --- | --- |
| `VAULT_ROOT` | Path to the local vault checkout. That checkout is not part of this repo. |
| `INDEX_PATH` | Path to the local search index database. |
| `EMBEDDING_MODEL_PATH` | Path to the local embedding model files. Vault text is not sent to a hosted embedding API. |
| `AUDIT_LOG_PATH` | Path to the append-only audit log. |
| `DENYLIST_PATH` | Optional path to a caller denylist (defence in depth, on top of overlay-network access rules). |
| `LISTEN_HOST` | Bind address. Must be `127.0.0.1` in production; the reverse proxy listens on the tailnet. Do not bind to `0.0.0.0` or a public IP. |
| `LISTEN_PORT` | Bind port on loopback. |
| `MCP_PATH` | URL path the proxy mounts, default `/vault/mcp`. The public endpoint is `https://<vps>.<tailnet>.ts.net/vault/mcp` (placeholders only). |
| `PUBLIC_BASE_URL` | Tailnet-only base URL, for example `https://<vps>.<tailnet>.ts.net`. Use the real MagicDNS name only in the on-box env file. |
| `IDENTITY_SOURCE` | How the app identifies the caller. Production is `whois` (nginx does not inject Tailscale identity). See [Caller identity](#caller-identity-and-exposure). |
| `TAILSCALED_SOCKET` | Path to the local tailscaled Unix socket used for WhoIs, usually `/var/run/tailscale/tailscaled.sock`. |

Copy `.env.example` to `.env` when running locally. Never commit `.env` or any filled-in values.

## Security

**No vault content in this repo.** Notes, search indexes, model caches, and `.env` files are gitignored. Tests use synthetic fixtures, not vault pages. Documentation uses placeholder names only.

**Secrets must never be stored in the vault.** This repo is the home of the shared detector. Vault ingest lint should call the same `scan` API (pin a release, or vendor a copy). The service also masks every read and search result, which is defence in depth, not a licence to store secrets.

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

Rules cover common token formats (GitHub, OpenAI, Anthropic, AWS, Slack, Google), JWTs, private-key blocks, and high-entropy `key=` / `token=` (and similar) assignments.

**Audit log.** Every tool call is appended to the log at `AUDIT_LOG_PATH`: timestamp, caller identity, tool, arguments, outcome, duration, and whether a secret-like span was masked (rule, path, span — not the secret value). Do not log secret values or vault body text. The log stays on the host; it is not committed here.

**Caller identity and exposure.** The service is reachable only on a private tailnet, not the public internet. nginx on the VPS listens on the node's MagicDNS name only and serves this app at `https://<vps>.<tailnet>.ts.net/vault/mcp` (placeholders; never commit real names). TLS certificates come from `tailscale cert`. The nginx config lives in a separate git on the VPS, not in this repo. The systemd unit binds to `127.0.0.1` only. Agents connect over HTTPS, not SSH tunnels. Admin access is Tailscale SSH, governed by ACLs, not SSH keys. Funnel stays off.

Caller identity is recorded on every call and used for the denylist. **`IDENTITY_SOURCE=whois` is required under nginx:** nginx does not inject Tailscale identity. It sets `X-Real-IP` and `X-Forwarded-For` from `$remote_addr`. The app trusts those headers only when the immediate TCP peer is loopback, then looks up the client address via the local tailscaled socket (`GET /localapi/v0/whois?addr=...` on `TAILSCALED_SOCKET`). That returns node name and tags, and a user profile when the node is not tagged. Reject requests that carry `Tailscale-Funnel-Request`.

A caller that is not on the tailnet, or that is on the denylist, is rejected.

## Deploy

Vault Keeper owns the code in this repository. Deployment on the VPS is done by pulling a release and following [DEPLOY.md](DEPLOY.md): service user, pull-only vault clone, loopback systemd unit, and an nginx `location /vault/mcp` in the on-box nginx git. The HTTP server itself is available from later milestones; the runbook marks those steps and gives checkable completion criteria for each one.

Vault Keeper independently verifies a deploy from a tailnet-connected machine. A report from the VPS agent is not acceptance. For the externally visible checks (MCP `initialize` / `tools/list`, masked fixture query, public IP must fail):

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

## Development

Requires Python 3.11 or later.

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m vault_search.secrets scan .
```

A GitHub Actions workflow runs the tests and [gitleaks](https://github.com/gitleaks/gitleaks) on push and pull request. To run the same hook locally:

```bash
pre-commit install
pre-commit run --all-files
```

This project is licensed under the MIT License.
