"""Streamable HTTP MCP server wrapping Client. Binds to loopback by default."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from vault_search.audit import append_event
from vault_search.client import Client
from vault_search.config import ConfigError, Settings, assert_bind_host
from vault_search.identity import Caller, resolve_caller


try:
    from mcp.server import MCPServer
    from mcp.server.mcpserver.context import Context
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError:  # pragma: no cover - exercised via ConfigError in build_server
    MCPServer = None  # type: ignore[assignment, misc]
    Context = object  # type: ignore[assignment, misc]

UNAUTHORIZED = "not authorized"

SEARCH_DESCRIPTION = (
    "Search the private knowledge vault and return a compact markdown hit list. "
    "Each hit is a title, vault-relative path, heading breadcrumb, projects/tags, "
    "and a short snippet. There are no ids, scores, or UUIDs. Secrets are masked. "
    "Results are keyword (full-text) search unless a local embedding index is installed. "
    "mode=hybrid is the API default: it fuses full-text with local semantic search when "
    "embeddings are present, and falls back to fulltext when they are not installed "
    "or the index has no vectors. mode=fulltext and mode=semantic are explicit overrides; "
    "semantic also falls back to fulltext without embeddings. A fallback is still "
    "full-text; it is not a semantic result. "
    "Optional project, tags, and type filters apply to every mode. "
    "Default limit is 8, hard cap 20. after is the last path from a previous page, "
    "used as a cursor, not an opaque token."
)
READ_DESCRIPTION = (
    "Read one vault note by vault-relative path (for example wiki/concepts/example.md). "
    "Returns markdown with title, type, projects/tags, description, outgoing links, "
    "backlinks, and either the named section, the full body, or a heading outline "
    "when the page is long (over about 8,000 characters) and section is omitted. "
    "Pass section to read a specific heading. Returns the exact string not_found "
    "only when the path does not exist. Secrets are masked."
)
LIST_NOTES_DESCRIPTION = (
    "List vault notes as compact markdown (title, path, type, projects/tags, "
    "one-line description). Optional project, tags, and type filters narrow the "
    "list; they are not access control. Default limit is 20, hard cap 100. "
    "after is the last path from a previous page. Secrets are masked."
)
LIST_PROJECTS_DESCRIPTION = (
    "List every project in the vault as compact markdown (name, one-line "
    "description, optional status and path). Includes project manifests and "
    "names referenced by page front matter. No arguments. Secrets are masked."
)


def _headers_and_peer(ctx: Any) -> tuple[dict[str, str], str | None]:
    headers: dict[str, str] = {}
    peer: str | None = None
    request = None
    try:
        request = ctx.request_context.request
    except Exception:
        request = None
    if request is not None:
        raw_headers = getattr(request, "headers", None)
        if raw_headers is not None:
            try:
                headers = {str(key).lower(): str(value) for key, value in raw_headers.items()}
            except Exception:
                headers = {}
        client = getattr(request, "client", None)
        if client is not None:
            if isinstance(client, (tuple, list)) and client:
                peer = str(client[0])
            else:
                host = getattr(client, "host", None)
                if host:
                    peer = str(host)
        if peer is None:
            scope = getattr(request, "scope", None)
            if isinstance(scope, Mapping):
                client_scope = scope.get("client")
                if isinstance(client_scope, (tuple, list)) and client_scope:
                    peer = str(client_scope[0])
    return headers, peer


def _tags(value: str | Sequence[str] | None) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
        return items or None
    items = [str(part).strip() for part in value if str(part).strip()]
    return items or None


def _authorize(ctx: Any) -> Caller:
    headers, peer = _headers_and_peer(ctx)
    return resolve_caller(headers=headers, peer=peer)


def transport_security(settings: Settings) -> TransportSecuritySettings:
    """Keep the SDK's DNS-rebinding check, but also accept the public host.

    The SDK only allows localhost Host headers on a loopback bind, so a request
    proxied with the MagicDNS name would get 421. PUBLIC_BASE_URL names it.
    """
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if settings.public_host:
        hosts += [settings.public_host, f"{settings.public_host}:*"]
        origins.append(f"https://{settings.public_host}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def _finish(
    *,
    settings: Settings,
    caller: Caller,
    tool: str,
    args: dict[str, Any],
    text: str,
    denied: bool = False,
) -> str:
    append_event(
        settings.audit_log_path,
        caller=caller,
        tool=tool,
        args=args,
        result_text=text,
        denied=denied,
    )
    return text


def build_server(
    settings: Settings,
    *,
    client: Client | None = None,
):
    if MCPServer is None or Context is object:
        raise ConfigError(
            "the official MCP SDK is not installed; reinstall the package (pip install -e .)"
        )

    vault = client or Client(settings)
    server = MCPServer(name="vault-search", version="0.1.0")

    def _gate(ctx: Context) -> tuple[Caller, str | None]:
        caller = _authorize(ctx)
        if not caller.allowed:
            return caller, UNAUTHORIZED
        return caller, None

    @server.tool(name="search", description=SEARCH_DESCRIPTION)
    def search(
        query: str,
        mode: str = "hybrid",
        project: str | None = None,
        tags: str | None = None,
        type: str | None = None,
        limit: int | None = None,
        after: str | None = None,
        *,
        ctx: Context,
    ) -> str:
        args = {
            "query": query,
            "mode": mode,
            "project": project,
            "tags": tags,
            "type": type,
            "limit": limit,
            "after": after,
        }
        caller, error = _gate(ctx)
        if error:
            return _finish(settings=settings, caller=caller, tool="search", args=args, text=error, denied=True)
        text = vault.search(
            query,
            mode=mode or "hybrid",
            project=project,
            tags=_tags(tags),
            type=type,
            limit=limit if limit is not None else 8,
            after=after,
        )
        return _finish(settings=settings, caller=caller, tool="search", args=args, text=text)

    @server.tool(name="read_note", description=READ_DESCRIPTION)
    def read_note(path: str, section: str | None = None, *, ctx: Context) -> str:
        args = {"path": path, "section": section}
        caller, error = _gate(ctx)
        if error:
            return _finish(settings=settings, caller=caller, tool="read_note", args=args, text=error, denied=True)
        text = vault.read_note(path, section)
        return _finish(settings=settings, caller=caller, tool="read_note", args=args, text=text)

    @server.tool(name="list_notes", description=LIST_NOTES_DESCRIPTION)
    def list_notes(
        project: str | None = None,
        tags: str | None = None,
        type: str | None = None,
        limit: int | None = None,
        after: str | None = None,
        *,
        ctx: Context,
    ) -> str:
        args = {
            "project": project,
            "tags": tags,
            "type": type,
            "limit": limit,
            "after": after,
        }
        caller, error = _gate(ctx)
        if error:
            return _finish(settings=settings, caller=caller, tool="list_notes", args=args, text=error, denied=True)
        text = vault.list_notes(
            project=project,
            tags=_tags(tags),
            type=type,
            limit=limit if limit is not None else 20,
            after=after,
        )
        return _finish(settings=settings, caller=caller, tool="list_notes", args=args, text=text)

    @server.tool(name="list_projects", description=LIST_PROJECTS_DESCRIPTION)
    def list_projects(*, ctx: Context) -> str:
        args: dict[str, Any] = {}
        caller, error = _gate(ctx)
        if error:
            return _finish(
                settings=settings, caller=caller, tool="list_projects", args=args, text=error, denied=True
            )
        text = vault.list_projects()
        return _finish(settings=settings, caller=caller, tool="list_projects", args=args, text=text)

    return server, vault


def serve(
    settings: Settings,
    *,
    client: Client | None = None,
) -> None:
    host = assert_bind_host(settings.listen_host, settings.listen_allow_nonlocal)
    server, _vault = build_server(settings, client=client)
    server.run(
        transport="streamable-http",
        host=host,
        port=settings.listen_port,
        streamable_http_path=settings.mcp_path,
        transport_security=transport_security(settings),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vault_search.mcp",
        description="Run the vault-search MCP server (Streamable HTTP, loopback by default).",
    )
    parser.add_argument("--vault", type=Path, default=None)
    parser.add_argument("--index", type=Path, default=None)
    parser.add_argument("--host", default=None, help="Bind address (default LISTEN_HOST or 127.0.0.1).")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default LISTEN_PORT or 8787).")
    parser.add_argument("--path", default=None, help="MCP URL path (default MCP_PATH or /vault/mcp).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env(vault_root=args.vault, index_path=args.index)
        if args.host:
            object.__setattr__(settings, "listen_host", args.host)
        if args.port is not None:
            object.__setattr__(settings, "listen_port", args.port)
        if args.path:
            from vault_search.config import normalize_mcp_path

            object.__setattr__(settings, "mcp_path", normalize_mcp_path(args.path))
        serve(settings)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
