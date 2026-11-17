"""Ingest-time masking: secrets must never be written to the SQLite index.

Synthetic tokens are assembled at runtime so the committed source does not
contain a complete secret that gitleaks or the detector would flag.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from vault_search.client import Client
from vault_search.config import Settings
from vault_search.index import (
    SCHEMA,
    SCHEMA_VERSION,
    FTS_CREATE,
    connect,
    execute_write,
    index_sidecar_paths,
    store_text,
)
from vault_search.secrets import RULE_GITHUB, mask


def _join(*parts: str) -> str:
    return "".join(parts)


def _github() -> str:
    return _join("gh", "p_", "A" * 36)


def _openai() -> str:
    return _join("sk", "-", "D" * 48)


def _generic() -> str:
    return _join("n7Q", "w2p", "Lx9", "vR4", "tZ8", "bK1", "sH6", "cY3")


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


def _dump_all_sql_text(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Independently dump every cell of every table, including FTS shadows."""
    dumped: list[tuple[str, str]] = []
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
    ).fetchall()
    for (name,) in tables:
        try:
            rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            mapping = dict(row) if isinstance(row, sqlite3.Row) else {}
            values = mapping.items() if mapping else enumerate(tuple(row))
            for column, value in values:
                text = _as_text(value)
                if text:
                    dumped.append((f"{name}.{column}", text))
    return dumped


def _assert_absent_from_index(index_path: Path, *secrets: str) -> None:
    assert secrets, "test must pass at least one secret"
    if index_path.is_file():
        uri = "file:" + index_path.resolve().as_posix() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for location, text in _dump_all_sql_text(conn):
                for secret in secrets:
                    assert secret not in text, f"secret leaked in SQL {location}"
        finally:
            conn.close()
    for path in index_sidecar_paths(index_path):
        if not path.is_file():
            continue
        data = path.read_bytes()
        for secret in secrets:
            assert secret.encode("utf-8") not in data, f"secret leaked in {path.name} bytes"
            assert secret.encode("latin-1") not in data, f"secret leaked in {path.name} bytes"


def _write_secret_note(root: Path, github: str, openai: str, generic: str) -> Path:
    wiki = root / "wiki" / "concepts"
    wiki.mkdir(parents=True, exist_ok=True)
    note = wiki / "ingest-mask.md"
    note.write_text(
        "---\n"
        f"title: Fixture with {github} in the title\n"
        "type: concept\n"
        f"description: Front matter description holds {openai}\n"
        f"tags: [ingest-mask, \"{github}\"]\n"
        "projects: [alpha]\n"
        "scope: project\n"
        "---\n\n"
        "Preamble has no credentials.\n\n"
        f"# Heading carries {openai}\n\n"
        f"Body paragraph includes the github token {github}.\n\n"
        f"api_token = {generic}\n",
        encoding="utf-8",
    )
    return note


def _reindex(root: Path, index_path: Path, embedder: object | None = None) -> Client:
    settings = Settings(vault_root=root, index_path=index_path)
    client = Client(settings, embedder=embedder, autoload=False)
    client.reindex()
    return client


def test_store_text_and_execute_write_mask_bindings(tmp_path: Path) -> None:
    token = _github()
    assert token not in store_text(f"key={token}")
    assert "REDACTED" in store_text(token)
    conn = sqlite3.connect(tmp_path / "scratch.db")
    conn.execute("CREATE TABLE t (v TEXT)")
    execute_write(conn, "INSERT INTO t(v) VALUES (?)", (f"token={token}",))
    stored = conn.execute("SELECT v FROM t").fetchone()[0]
    assert token not in stored
    try:
        execute_write(conn, "SELECT v FROM t WHERE v = ?", (token,))
        raise AssertionError("SELECT should be rejected")
    except ValueError:
        pass
    conn.close()


def test_indexed_note_never_stores_synthetic_secrets(tmp_path: Path) -> None:
    github, openai, generic = _github(), _openai(), _generic()
    root = tmp_path / "vault"
    index_path = tmp_path / "index.db"
    _write_secret_note(root, github, openai, generic)
    client = _reindex(root, index_path)
    try:
        meta = client._index.conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("schema_version",)
        ).fetchone()
        assert meta["value"] == SCHEMA_VERSION
        dumped = _dump_all_sql_text(client._index.conn)
        tables = {location.split(".", 1)[0] for location, _ in dumped}
        # Ordinary tables plus the FTS virtual table and its shadows must be scanned.
        assert "notes" in tables
        assert "chunks" in tables
        assert any(name.startswith("chunks_fts") for name in tables)
        for location, text in dumped:
            for secret in (github, openai, generic):
                assert secret not in text, location
        title = client._index.conn.execute("SELECT title FROM notes").fetchone()["title"]
        tags = client._index.conn.execute("SELECT tags_json FROM notes").fetchone()["tags_json"]
        heading = client._index.conn.execute("SELECT heading FROM chunks WHERE heading != ''").fetchone()[
            "heading"
        ]
        assert "[REDACTED:" in title
        assert "[REDACTED:" in tags
        assert "[REDACTED:" in heading
        assert RULE_GITHUB in title or "REDACTED" in title
        read = client.read_note("wiki/concepts/ingest-mask.md")
        search = client.search("credential")
        for output in (read, search, title, tags, heading):
            assert github not in output
            assert openai not in output
            assert generic not in output
    finally:
        client.close()
    _assert_absent_from_index(index_path, github, openai, generic)


def test_embeddings_are_computed_from_masked_text(tmp_path: Path) -> None:
    github, openai, generic = _github(), _openai(), _generic()
    recorded: list[str] = []

    class RecordingEmbedder:
        model_name = "fake-test"
        dim = 8

        def embed_documents(self, texts):
            recorded.extend(texts)
            return [[0.0] * self.dim for _ in texts]

        def embed_queries(self, texts):
            return [[0.0] * self.dim for _ in texts]

    root = tmp_path / "vault"
    index_path = tmp_path / "emb.db"
    _write_secret_note(root, github, openai, generic)
    client = _reindex(root, index_path, embedder=RecordingEmbedder())
    try:
        assert recorded, "embedder should have received chunk texts"
        for text in recorded:
            assert github not in text
            assert openai not in text
            assert generic not in text
        blobs = client._index.conn.execute("SELECT embedding FROM chunk_embeddings").fetchall()
        assert blobs
        for (blob,) in blobs:
            data = bytes(blob)
            assert github.encode("utf-8") not in data
            assert openai.encode("utf-8") not in data
            assert generic.encode("utf-8") not in data
    finally:
        client.close()
    _assert_absent_from_index(index_path, github, openai, generic)


def test_schema_upgrade_wipes_unmasked_legacy_rows(tmp_path: Path) -> None:
    assert SCHEMA_VERSION != "5"
    github = _github()
    index_path = tmp_path / "legacy.db"
    conn = connect(index_path)
    conn.executescript(SCHEMA)
    conn.executescript(FTS_CREATE)
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("schema_version", "5"))
    body = f"legacy unmasked body {github}"
    conn.execute(
        """
        INSERT INTO notes(
            path, title, type, description, created, updated, status, source,
            projects_json, tags_json, include_tags_json, mtime_ns, size, sha256, body
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "wiki/legacy.md",
            f"Legacy {github}",
            "concept",
            "old",
            "",
            "",
            "",
            "wiki",
            "[]",
            "[]",
            "[]",
            1,
            1,
            "x" * 64,
            body,
        ),
    )
    cursor = conn.execute(
        """
        INSERT INTO chunks(path, ordinal, heading, breadcrumb, level, start_line, end_line, text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("wiki/legacy.md", 0, f"Head {github}", f"Head {github}", 1, 1, 2, body),
    )
    conn.execute(
        "INSERT INTO chunks_fts(rowid, text, title, breadcrumb, path) VALUES (?, ?, ?, ?, ?)",
        (cursor.lastrowid, body, f"Legacy {github}", f"Head {github}", "wiki/legacy.md"),
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    raw = index_path.read_bytes()
    assert github.encode("utf-8") in raw

    root = tmp_path / "vault"
    _write_secret_note(root, github, _openai(), _generic())
    client = _reindex(root, index_path)
    try:
        version = client._index.conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("schema_version",)
        ).fetchone()["value"]
        assert version == SCHEMA_VERSION
        paths = [
            row["path"]
            for row in client._index.conn.execute("SELECT path FROM notes")
        ]
        assert "wiki/legacy.md" not in paths
        assert "wiki/concepts/ingest-mask.md" in paths
    finally:
        client.close()
    _assert_absent_from_index(index_path, github, _openai(), _generic())


def test_index_audit_cli_clean_and_dirty(tmp_path: Path) -> None:
    github, openai, generic = _github(), _openai(), _generic()
    root = tmp_path / "vault"
    index_path = tmp_path / "audit.db"
    _write_secret_note(root, github, openai, generic)
    client = _reindex(root, index_path)
    client.close()

    clean = subprocess.run(
        [sys.executable, "-m", "vault_search", "index-audit", str(index_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert clean.stdout == ""
    for secret in (github, openai, generic):
        assert secret not in clean.stdout
        assert secret not in clean.stderr

    # Bypass the choke point to prove the auditor catches an unmasked row.
    conn = sqlite3.connect(index_path)
    conn.execute(
        "UPDATE notes SET description = ? WHERE path = ?",
        (f"leaked {github}", "wiki/concepts/ingest-mask.md"),
    )
    conn.commit()
    conn.close()
    dirty = subprocess.run(
        [sys.executable, "-m", "vault_search", "index-audit", str(index_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert dirty.returncode == 1, dirty.stdout + dirty.stderr
    assert "github_token" in dirty.stdout
    assert github not in dirty.stdout
    assert github not in dirty.stderr
    assert RULE_GITHUB in dirty.stdout or "github_token" in dirty.stdout

    missing = subprocess.run(
        [sys.executable, "-m", "vault_search", "index-audit", str(tmp_path / "nope.db")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 2
    assert "not found" in missing.stderr


def test_output_masking_remains_as_second_layer() -> None:
    token = _github()
    already = mask(f"kept {token}")
    assert token not in already
    assert token not in mask(already)
    assert already == mask(already)
