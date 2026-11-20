# vault-search

Read-only search for a private knowledge vault, with a bundled MCP server. It is meant for coding agents: find a page or section, read only what is needed, and keep context small.

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

## Connect

In production, `tailscale serve` terminates TLS on the tailnet and forwards to nginx on loopback, which proxies to this app on loopback. Agents connect over HTTPS to `https://<vps>.<tailnet>.ts.net/vault/mcp` (placeholders only; never commit real names). The service is not on the public internet. Funnel stays off.

Vault Keeper owns the code in this repository. Deploy by pulling a release and following [DEPLOY.md](DEPLOY.md). Architecture, access, masking, and the tool contract are in [SPEC.md](SPEC.md).

This project is licensed under the MIT License.
