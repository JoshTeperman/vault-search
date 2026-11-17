"""Scan a built SQLite index for unmasked secrets.

Dumps every text-like cell (including FTS shadow tables) and also runs the
detector over the raw database bytes and any WAL/journal sidecars. Matched
secret values are never printed.

CLI::

    python -m vault_search index-audit [index.db]

Exits 1 if any finding is reported, 0 when the index is clean, and 2 on
usage or missing-path errors.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

from vault_search.index import index_sidecar_paths
from vault_search.secrets import Finding, scan

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_INDEX_BYTES = 512 * 1024 * 1024


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        if not value:
            return None
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).decode("latin-1")
    return None


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    info = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    return [str(row[1]) for row in info]


def iter_index_text(conn: sqlite3.Connection) -> Iterator[tuple[str, str]]:
    """Yield (location, text) for every text-like cell, including FTS shadows."""
    for table in _table_names(conn):
        if not _IDENT.fullmatch(table):
            continue
        try:
            columns = _column_names(conn, table)
        except sqlite3.Error:
            continue
        if not columns:
            continue
        quoted_cols = ", ".join(_quote_ident(col) for col in columns)
        has_rowid = True
        try:
            rows = conn.execute(
                f"SELECT rowid AS _audit_rowid, {quoted_cols} FROM {_quote_ident(table)}"
            ).fetchall()
        except sqlite3.Error:
            has_rowid = False
            try:
                rows = conn.execute(
                    f"SELECT {quoted_cols} FROM {_quote_ident(table)}"
                ).fetchall()
            except sqlite3.Error:
                continue
        for row in rows:
            mapping = dict(row)
            row_id = mapping.pop("_audit_rowid", None) if has_rowid else None
            for column, value in mapping.items():
                text = _as_text(value)
                if not text:
                    continue
                if row_id is None:
                    location = f"{table}.{column}"
                else:
                    location = f"{table}.{column}:rowid={row_id}"
                yield location, text


def _scan_file_bytes(path: Path) -> list[Finding]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0 or size > _MAX_INDEX_BYTES:
        return []
    try:
        data = path.read_bytes()
    except OSError:
        return []
    text = data.decode("latin-1")
    return scan(text, path=f"{path.as_posix()}:bytes")


def audit_index(index_path: Path) -> list[Finding]:
    """Return detector findings from SQL text cells and raw index files."""
    index_path = index_path.expanduser()
    findings: list[Finding] = []
    uri = "file:" + index_path.resolve().as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        for location, text in iter_index_text(conn):
            display = f"{index_path.as_posix()}:{location}"
            findings.extend(scan(text, path=display))
    finally:
        conn.close()

    for side in index_sidecar_paths(index_path.resolve()):
        if side.is_file():
            findings.extend(_scan_file_bytes(side))

    findings.sort(key=lambda f: (f.path or "", f.line, f.column, f.rule))
    return findings


def run_audit(index_path: Path) -> int:
    """Print findings and return the process exit code."""
    if not index_path.expanduser().is_file():
        print(f"error: index not found: {index_path}", file=sys.stderr)
        return 2
    findings = audit_index(index_path)
    for finding in findings:
        location = finding.path or "<input>"
        print(f"{location}:{finding.line}:{finding.column}: {finding.rule}")
    return 1 if findings else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vault_search index-audit",
        description=(
            "Dump all text from a vault-search index and scan it for unmasked "
            "secrets. Matched values are never printed."
        ),
    )
    parser.add_argument(
        "index_file",
        nargs="?",
        type=Path,
        default=None,
        help="SQLite index path (else --index / INDEX_PATH / index/vault.db).",
    )
    args = parser.parse_args(argv)
    path = args.index_file
    if path is None:
        raw = os.environ.get("INDEX_PATH")
        path = Path(raw) if raw else Path.cwd() / "index" / "vault.db"
    return run_audit(path)


if __name__ == "__main__":
    sys.exit(main())
