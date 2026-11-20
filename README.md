# vault-search

Read-only search for a private knowledge vault, with a bundled, stateless MCP server. It is meant for coding agents: find a page or section, read only what is needed, and keep context small.

This repository holds the search interface, the shared secret detector, a local FTS5 indexer with optional hybrid search, and a Streamable HTTP MCP server. The vault itself is a separate private checkout and is never committed here.

The v1 deploy is keyword-only (full-text) search. Install without the `[semantic]` extra. Meaning-based search can be turned on later by installing `.[semantic]` and reindexing. See [DEPLOY.md](DEPLOY.md).

The design, tool contract, and implementation notes are in [SPEC.md](SPEC.md). Production deploy is in [DEPLOY.md](DEPLOY.md).

## Install and run

Requires Python 3.11 or later. The core indexer is the Python standard library plus the official `mcp` SDK, pinned to an exact version (also listed in the `dev` extra). Local embeddings are optional: `pip install '.[semantic]'` (fastembed/ONNX, not torch). The v1 deploy does not install that extra.

```bash
python -m pip install -e ".[dev]"
python -m pip install -e ".[semantic]"   # optional; not part of the v1 deploy
python -m pytest -q
python -m vault_search.secrets scan .
python -m vault_search index-audit index/vault.db   # after building a local index
```

Copy `.env.example` to `.env` and set at least `VAULT_ROOT` and `INDEX_PATH`. This repository contains placeholders only. Never commit `.env` or any filled-in values. The full configuration list is in [SPEC.md](SPEC.md#configuration).

Index a local vault checkout and search it:

```bash
python -m vault_search index
python -m vault_search search "evals" --project alpha --limit 8
python -m vault_search read wiki/concepts/evals.md --section Scoring
python -m vault_search.mcp           # Streamable HTTP on 127.0.0.1
```

The Python `Client` API and the rest of the CLI are in [SPEC.md](SPEC.md#local-index-and-cli). Tests use only synthetic fixtures under `tests/fixtures`. Never point the test suite at a real vault.

A GitHub Actions workflow runs the tests and [gitleaks](https://github.com/gitleaks/gitleaks) on push and pull request. To run the same hook locally:

```bash
pre-commit install
pre-commit run --all-files
```

## MCP

`python -m vault_search.mcp` runs one MCP server named `vault-search`. It listens on `LISTEN_HOST:LISTEN_PORT` (default `127.0.0.1:8787`) at `MCP_PATH` (default `/vault/mcp`). It refuses a wildcard bind unless `LISTEN_ALLOW_NONLOCAL=1` is set.

```text
POST {MCP_PATH}            JSON-RPC 2.0 in, one JSON body out
├── search                 query, mode, project, tags, type, limit (8, max 20), after
├── read_note              path, section
├── list_notes             project, tags, type, limit (20, max 100), after
└── list_projects          no arguments
```

All four tools are read-only and return compact markdown with secrets masked. `after` is the last path from the previous page. `tags` is a comma-separated string. The full tool contract is in [SPEC.md](SPEC.md).

### Stateless

The server keeps no session and does not stream. Each POST is independent and gets one `application/json` reply:

- There is no `initialize` call and no `Mcp-Session-Id`. A script sends `tools/call` directly.
- A restart or redeploy does not break connected clients.
- There are no server-to-client messages (progress, sampling, elicitation). No tool needs them.

The transport is still Streamable HTTP, the current MCP HTTP transport, so standard MCP clients connect as usual. They still send `initialize`, and the server answers it.

### Access and audit

Access control is the network: the app and nginx listen on loopback only, and only the tailnet reaches nginx. There is no token. The server checks the `Host` header against `PUBLIC_BASE_URL` (other hosts get `421`) and denies requests marked as Funnel traffic. Each tool call is appended to `AUDIT_LOG_PATH` with the client IP and the optional, unverified `X-Vault-Agent-Name` header.

### Usage

Set the URL in your shell. Never commit a real hostname.

```bash
export VAULT_MCP_URL='https://<vps>.<tailnet>.ts.net/vault/mcp'   # or http://127.0.0.1:8787/vault/mcp locally

mcp() {
  curl -fsS "$VAULT_MCP_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json' \
    -H 'X-Vault-Agent-Name: my-script' \
    -d "$1"
}
```

List the tools:

```bash
mcp '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Search, then read one section of a hit:

```bash
mcp '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search","arguments":{"query":"evals","project":"alpha","limit":5}}}'
mcp '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"read_note","arguments":{"path":"wiki/concepts/evals.md","section":"Scoring"}}}'
```

Page through notes and list projects:

```bash
mcp '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_notes","arguments":{"tags":"evals,howto","limit":20}}}'
mcp '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_notes","arguments":{"tags":"evals,howto","after":"<last path from page 1>"}}}'
mcp '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_projects","arguments":{}}}'
```

The markdown is in `result.content[0].text`. To print only that text:

```bash
mcp '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_projects","arguments":{}}}' \
  | jq -r '.result.content[0].text'
```

Add the server to Claude Code:

```bash
claude mcp add --transport http vault-search "$VAULT_MCP_URL"
```

`scripts/verify-deploy` runs the post-deploy checks against the same URL. See [DEPLOY.md](DEPLOY.md).

## Connect

In production, `tailscale serve` terminates TLS on the tailnet and forwards to nginx on loopback, which proxies to this app on loopback. Agents connect over HTTPS to `https://<vps>.<tailnet>.ts.net/vault/mcp` (placeholders only; never commit real names). The service is not on the public internet. Funnel stays off.

Vault Keeper owns the code in this repository. Deploy by pulling a release and following [DEPLOY.md](DEPLOY.md). Architecture, access, masking, and the tool contract are in [SPEC.md](SPEC.md).

This project is licensed under the MIT License.
