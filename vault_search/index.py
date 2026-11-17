"""SQLite FTS5 index over a vault checkout. Incremental, no vault content in git.

Every text value is secret-masked at a single write choke point before it is
stored. A schema-version mismatch deletes the database file and sidecars and
rebuilds them, so older unmasked rows cannot remain in free pages.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from vault_search.config import Settings
from vault_search.parse import ParsedNote, parse_note
from vault_search.secrets import mask as mask_secrets

SCHEMA_VERSION = "6"

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
  path TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  type TEXT,
  description TEXT,
  created TEXT,
  updated TEXT,
  status TEXT,
  source TEXT NOT NULL,
  projects_json TEXT NOT NULL DEFAULT '[]',
  tags_json TEXT NOT NULL DEFAULT '[]',
  include_tags_json TEXT NOT NULL DEFAULT '[]',
  mtime_ns INTEGER NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  heading TEXT NOT NULL DEFAULT '',
  breadcrumb TEXT NOT NULL DEFAULT '',
  level INTEGER NOT NULL DEFAULT 0,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  text TEXT NOT NULL,
  UNIQUE(path, ordinal)
);

CREATE INDEX IF NOT EXISTS chunks_path_idx ON chunks(path);

CREATE TABLE IF NOT EXISTS links (
  src_path TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
  dest_stem TEXT NOT NULL,
  dest_path TEXT,
  PRIMARY KEY (src_path, dest_stem)
);

CREATE INDEX IF NOT EXISTS links_dest_path_idx ON links(dest_path);
CREATE INDEX IF NOT EXISTS links_dest_stem_idx ON links(dest_stem);

-- Reserved for milestone 5: vectors attach 1:1 to chunks by chunk_id.
CREATE TABLE IF NOT EXISTS chunk_embeddings (
  chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  embedding BLOB NOT NULL
);
"""

FTS_CREATE = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text,
  title,
  breadcrumb,
  path UNINDEXED,
  tokenize = 'porter unicode61 remove_diacritics 2'
);
"""


@dataclass(frozen=True)
class ReindexStats:
    pages: int
    chunks: int
    upserted: int
    unchanged: int
    deleted: int
    vault_sha: str | None
    elapsed_ms: int
    skipped: bool = False
    embeddings_written: int = 0


def git_head(root: Path) -> str | None:
    """Return HEAD only when *root* itself is a git checkout, not a subfolder of one."""
    try:
        toplevel = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if toplevel.returncode != 0:
        return None
    top = Path(toplevel.stdout.strip())
    try:
        if top.resolve() != root.resolve():
            return None
    except OSError:
        return None
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_note_paths(root: Path, *, include_raw: bool, include_examples: bool) -> list[Path]:
    bases = [root / "wiki"]
    if include_raw:
        bases.append(root / "raw")
    paths: list[Path] = []
    for base in bases:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if not path.is_file() or path.is_symlink():
                continue
            if not include_examples and "_examples" in path.parts:
                continue
            paths.append(path)
    return paths


def store_text(value: str) -> str:
    """Mask secret-like spans before text is written to the index.

    This is the only function that may prepare a string for a SQLite write.
    Callers must bind text through ``execute_write`` or ``store_text`` itself.
    Output masking in the query layer is a second line of defence, not a
    substitute for this step.
    """
    return mask_secrets(value)


_WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|REPLACE|DELETE)\b", re.IGNORECASE)


def execute_write(
    conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()
) -> sqlite3.Cursor:
    """Run a write statement after passing every string parameter through store_text."""
    if not _WRITE_SQL.match(sql):
        raise ValueError("execute_write accepts only INSERT, UPDATE, REPLACE, or DELETE")
    bound = tuple(store_text(param) if isinstance(param, str) else param for param in params)
    return conn.execute(sql, bound)


def index_sidecar_paths(index_path: Path) -> tuple[Path, ...]:
    """Return the main database path plus WAL, SHM, and journal sidecars."""
    name = str(index_path)
    return (index_path, Path(name + "-wal"), Path(name + "-shm"), Path(name + "-journal"))


def remove_index_files(index_path: Path) -> None:
    """Delete the index database and any sidecar files if they exist."""
    for path in index_sidecar_paths(index_path):
        path.unlink(missing_ok=True)


def connect(index_path: Path) -> sqlite3.Connection:
    index_path = index_path.expanduser().resolve()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the MCP server may call the shared Client from
    # worker threads. Writes still go through reindex; readers are concurrent
    # under WAL.
    conn = sqlite3.connect(index_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    execute_write(
        conn,
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _create_fresh_index(index_path: Path) -> sqlite3.Connection:
    """Write a new empty database file with the current schema."""
    remove_index_files(index_path)
    conn = connect(index_path)
    conn.executescript(SCHEMA)
    conn.executescript(FTS_CREATE)
    _set_meta(conn, "schema_version", SCHEMA_VERSION)
    conn.commit()
    return conn


def ensure_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Rebuild the index from a new file if the schema version does not match.

    Returns a (possibly new) connection. When a rebuild is required, the
    input connection is closed and the database file plus sidecars are
    deleted so no unmasked text can remain in free pages or WAL leftovers.
    """
    current = None
    try:
        current = _meta(conn, "schema_version")
    except sqlite3.OperationalError:
        current = None
    if current == SCHEMA_VERSION:
        return conn
    row = conn.execute("PRAGMA database_list").fetchone()
    index_path = Path(row["file"] if isinstance(row, sqlite3.Row) else row[2])
    conn.close()
    return _create_fresh_index(index_path)


def open_index(index_path: Path) -> sqlite3.Connection:
    """Connect and ensure the current schema, rebuilding the file if needed."""
    return ensure_schema(connect(index_path))


def _source_for(rel: str) -> str:
    if rel.startswith("raw/"):
        return "raw"
    return "wiki"


def _delete_note(conn: sqlite3.Connection, path: str) -> None:
    stored = store_text(path)
    ids = [row["id"] for row in conn.execute("SELECT id FROM chunks WHERE path = ?", (stored,))]
    for chunk_id in ids:
        execute_write(conn, "DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
    execute_write(conn, "DELETE FROM links WHERE src_path = ?", (stored,))
    execute_write(conn, "DELETE FROM chunks WHERE path = ?", (stored,))
    execute_write(conn, "DELETE FROM notes WHERE path = ?", (stored,))


def _upsert_note(
    conn: sqlite3.Connection,
    note: ParsedNote,
    *,
    mtime_ns: int,
    size: int,
    sha256: str,
) -> int:
    _delete_note(conn, note.path)
    execute_write(
        conn,
        """
        INSERT INTO notes(
            path, title, type, description, created, updated, status, source,
            projects_json, tags_json, include_tags_json, mtime_ns, size, sha256, body
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note.path,
            note.title,
            note.note_type,
            note.description,
            note.created,
            note.updated,
            note.status,
            note.source,
            json.dumps(note.projects),
            json.dumps(note.tags),
            json.dumps(note.include_tags),
            mtime_ns,
            size,
            sha256,
            note.body,
        ),
    )
    for link in note.links:
        execute_write(
            conn,
            "INSERT OR IGNORE INTO links(src_path, dest_stem, dest_path) VALUES (?, ?, NULL)",
            (note.path, link),
        )
    count = 0
    for chunk in note.chunks:
        cursor = execute_write(
            conn,
            """
            INSERT INTO chunks(path, ordinal, heading, breadcrumb, level, start_line, end_line, text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                note.path,
                chunk.ordinal,
                chunk.heading,
                chunk.breadcrumb,
                chunk.level,
                chunk.start_line,
                chunk.end_line,
                chunk.text,
            ),
        )
        chunk_id = cursor.lastrowid
        execute_write(
            conn,
            "INSERT INTO chunks_fts(rowid, text, title, breadcrumb, path) VALUES (?, ?, ?, ?, ?)",
            (chunk_id, chunk.text, note.title, chunk.breadcrumb, note.path),
        )
        count += 1
    return count


def _resolve_links(conn: sqlite3.Connection) -> None:
    stems: dict[str, str] = {}
    for row in conn.execute("SELECT path FROM notes"):
        stem = Path(row["path"]).stem
        # First path wins; unique stems are the expected case.
        stems.setdefault(stem, row["path"])
    for row in conn.execute("SELECT src_path, dest_stem FROM links"):
        dest = stems.get(row["dest_stem"])
        execute_write(
            conn,
            "UPDATE links SET dest_path = ? WHERE src_path = ? AND dest_stem = ?",
            (dest, row["src_path"], row["dest_stem"]),
        )


class Index:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.conn = open_index(settings.index_path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def reindex(self, *, force: bool = False, embedder: object | None = None) -> ReindexStats:
        started = time.perf_counter()
        root = self.settings.vault_root
        if not root.is_dir():
            raise FileNotFoundError(f"vault checkout not found: {root}")
        sha = git_head(root)
        paths = iter_note_paths(
            root,
            include_raw=self.settings.include_raw,
            include_examples=self.settings.include_examples,
        )
        existing = {
            row["path"]: row
            for row in self.conn.execute(
                "SELECT path, mtime_ns, size, sha256 FROM notes"
            )
        }
        seen: set[str] = set()
        upserted = 0
        unchanged = 0
        skipped_all = False

        if (
            not force
            and sha
            and _meta(self.conn, "vault_sha") == sha
            and _meta(self.conn, "include_raw") == str(self.settings.include_raw)
            and _meta(self.conn, "include_examples") == str(self.settings.include_examples)
        ):
            # Fast path: same commit, same file set and mtimes.
            current_rel = {path.relative_to(root).as_posix() for path in paths}
            if current_rel == set(existing) and all(
                existing[rel]["mtime_ns"] == path.stat().st_mtime_ns
                and existing[rel]["size"] == path.stat().st_size
                for rel, path in (
                    (p.relative_to(root).as_posix(), p) for p in paths
                )
            ):
                skipped_all = True

        if not skipped_all:
            for path in paths:
                rel = path.relative_to(root).as_posix()
                seen.add(rel)
                stat = path.stat()
                prior = existing.get(rel)
                if (
                    not force
                    and prior is not None
                    and prior["mtime_ns"] == stat.st_mtime_ns
                    and prior["size"] == stat.st_size
                ):
                    unchanged += 1
                    continue
                digest = file_sha256(path)
                if not force and prior is not None and prior["sha256"] == digest:
                    execute_write(
                        self.conn,
                        "UPDATE notes SET mtime_ns = ?, size = ? WHERE path = ?",
                        (stat.st_mtime_ns, stat.st_size, rel),
                    )
                    unchanged += 1
                    continue
                text = path.read_text(encoding="utf-8")
                note = parse_note(rel, text, source=_source_for(rel))
                if note.example and not self.settings.include_examples:
                    if rel in existing:
                        _delete_note(self.conn, rel)
                    continue
                _upsert_note(
                    self.conn,
                    note,
                    mtime_ns=stat.st_mtime_ns,
                    size=stat.st_size,
                    sha256=digest,
                )
                upserted += 1

            deleted = 0
            for rel in set(existing) - seen:
                _delete_note(self.conn, rel)
                deleted += 1
            _resolve_links(self.conn)
        else:
            deleted = 0

        _set_meta(self.conn, "vault_sha", sha or "")
        _set_meta(self.conn, "include_raw", str(self.settings.include_raw))
        _set_meta(self.conn, "include_examples", str(self.settings.include_examples))
        written = _sync_embeddings(self.conn, embedder) if embedder is not None else 0
        self.conn.commit()

        pages = self.conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
        chunks = self.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ReindexStats(
            pages=pages,
            chunks=chunks,
            upserted=upserted,
            unchanged=unchanged,
            deleted=deleted,
            vault_sha=sha,
            elapsed_ms=elapsed_ms,
            skipped=skipped_all,
            embeddings_written=written,
        )


# ---------------------------------------------------------------------------
# Incremental embeddings
# ---------------------------------------------------------------------------

_EMBED_BATCH = 32


def _chunk_embed_text(title: str, breadcrumb: str, text: str) -> str:
    parts = [p.strip() for p in (title, breadcrumb, text) if p and p.strip()]
    return "\n".join(parts)


def _sync_embeddings(conn: sqlite3.Connection, embedder: object) -> int:
    """Embed chunks that are missing a vector for the embedder's current model."""
    from vault_search.embed import l2_normalize, pack_vector

    model = embedder.model_name  # type: ignore[attr-defined]
    dim = int(embedder.dim)  # type: ignore[attr-defined]
    execute_write(conn, "DELETE FROM chunk_embeddings WHERE model != ?", (model,))
    rows = conn.execute(
        """
        SELECT c.id, n.title, c.breadcrumb, c.text
        FROM chunks c
        JOIN notes n ON n.path = c.path
        WHERE NOT EXISTS (
            SELECT 1 FROM chunk_embeddings e
            WHERE e.chunk_id = c.id AND e.model = ?
        )
        ORDER BY c.id
        """,
        (model,),
    ).fetchall()
    if not rows:
        return 0
    written = 0
    embed_documents = embedder.embed_documents  # type: ignore[attr-defined]
    for i in range(0, len(rows), _EMBED_BATCH):
        batch = rows[i : i + _EMBED_BATCH]
        texts = [_chunk_embed_text(r["title"], r["breadcrumb"], r["text"]) for r in batch]
        vectors = embed_documents(texts)
        if len(vectors) != len(batch):
            raise RuntimeError("embedder returned a different number of vectors than inputs")
        for row, vector in zip(batch, vectors, strict=True):
            packed = pack_vector(l2_normalize(vector))
            execute_write(
                conn,
                """
                INSERT OR REPLACE INTO chunk_embeddings (chunk_id, model, dim, embedding)
                VALUES (?, ?, ?, ?)
                """,
                (row["id"], model, dim, packed),
            )
            written += 1
    _set_meta(conn, "embedding_model", model)
    _set_meta(conn, "embedding_dim", str(dim))
    return written
