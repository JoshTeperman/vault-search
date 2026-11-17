"""Command-line interface for indexing and querying a local vault checkout."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vault_search.client import Client
from vault_search.config import ConfigError


def _tags(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    return values


def _add_filter_flags(parser: argparse.ArgumentParser, *, with_after: bool, default_limit: int) -> None:
    parser.add_argument("--project", help="Narrow to a project (membership, not access control).")
    parser.add_argument("--tag", action="append", dest="tags", help="Require this tag (repeatable).")
    parser.add_argument("--type", dest="note_type", help="Narrow to a note type.")
    parser.add_argument("--limit", type=int, default=default_limit)
    if with_after:
        parser.add_argument("--after", help="Path cursor: results after this path.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vault_search",
        description="Index and query a local knowledge vault (read-only).",
    )
    parser.add_argument("--vault", type=Path, default=None, help="Vault checkout (else VAULT_ROOT / VAULT_PATH).")
    parser.add_argument("--index", type=Path, default=None, help="SQLite index path (else INDEX_PATH).")
    parser.add_argument("--include-raw", action="store_true", help="Also index raw/ (off by default).")
    sub = parser.add_subparsers(dest="command", required=True)

    index_p = sub.add_parser("index", help="Build or incrementally update the FTS5 index.")
    index_p.add_argument("--force", action="store_true", help="Re-parse every file, even if unchanged.")

    search_p = sub.add_parser("search", help="Full-text search (compact markdown).")
    search_p.add_argument("query")
    search_p.add_argument(
        "--mode",
        default="fulltext",
        choices=("fulltext", "hybrid", "semantic"),
        help="hybrid and semantic are reserved for milestone 5; they currently use fulltext.",
    )
    _add_filter_flags(search_p, with_after=True, default_limit=8)

    read_p = sub.add_parser("read", help="Read a note, or a section of one.")
    read_p.add_argument("path")
    read_p.add_argument("--section", default=None)

    list_p = sub.add_parser("list-notes", help="List notes, optionally filtered.")
    _add_filter_flags(list_p, with_after=True, default_limit=20)

    sub.add_parser("list-projects", help="List projects (manifests and project tags).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        client = Client.from_env(
            vault_root=args.vault,
            index_path=args.index,
            include_raw=True if args.include_raw else None,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "index":
            stats = client.reindex(force=args.force)
            sha = stats.vault_sha or "(no git sha)"
            skip = " (unchanged, skipped)" if stats.skipped else ""
            print(
                f"indexed {stats.pages} pages, {stats.chunks} chunks "
                f"({stats.upserted} updated, {stats.unchanged} unchanged, "
                f"{stats.deleted} deleted) in {stats.elapsed_ms} ms; sha {sha}{skip}"
            )
            return 0
        if args.command == "search":
            text = client.search(
                args.query,
                mode=args.mode,
                project=args.project,
                tags=_tags(args.tags),
                type=args.note_type,
                limit=args.limit,
                after=args.after,
            )
        elif args.command == "read":
            text = client.read_note(args.path, args.section)
        elif args.command == "list-notes":
            text = client.list_notes(
                project=args.project,
                tags=_tags(args.tags),
                type=args.note_type,
                limit=args.limit,
                after=args.after,
            )
        else:
            text = client.list_projects()
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return 0 if text.strip() != "not_found" else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
