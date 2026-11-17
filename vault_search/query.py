"""Read-only query helpers. Every formatted string is masked before return."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from vault_search.parse import chunk_body, find_section
DEFAULT_SEARCH_LIMIT = 8
SEARCH_LIMIT_CAP = 20
DEFAULT_LIST_LIMIT = 20
LIST_LIMIT_CAP = 100
LONG_PAGE_CHARS = 8000

_TOKEN = re.compile(r"\S+")
_PHRASE = re.compile(r'"([^"]+)"')
_WORD = re.compile(r"[A-Za-z0-9]+")
_OPERATORS = {"AND", "OR", "NOT"}


def _safe(text: str) -> str:
    # Imported lazily so `python -m vault_search.secrets` does not warn.
    from vault_search import secrets as _secrets
    return _secrets.mask(text)


def clamp_limit(limit: int | None, default: int, cap: int) -> int:
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return 1
    return min(value, cap)


def load_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def query_terms(query: str) -> list[str]:
    """Split a user query into searchable terms (phrases kept, hyphens split)."""
    terms: list[str] = []
    rest = _PHRASE.sub(" ", query or "")
    for match in _PHRASE.finditer(query or ""):
        phrase = " ".join(_WORD.findall(match.group(1)))
        if phrase:
            terms.append(phrase)
    for token in _TOKEN.findall(rest):
        if token in {"(", ")"} or token.upper() in _OPERATORS:
            continue
        # Split hyphenated / underscored compounds so "front-matter" matches
        # both the phrase and the individual stems under unicode61.
        for part in re.split(r"[-_./]+", token):
            words = _WORD.findall(part)
            if not words:
                continue
            if len(words) == 1:
                terms.append(words[0])
            else:
                terms.extend(words)
    # Preserve order, drop short noise except when it is the only term.
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(term)
    if len(unique) > 1:
        unique = [t for t in unique if len(t) >= 2]
    return unique


def fts_query(query: str, *, combine: str = "AND", prefix: bool = True) -> str:
    """Turn a user query into a safe FTS5 MATCH string.

    Tokens are quoted. Prefix matching (`"token"*`) is on by default so a
    query like `front matter` can still hit `front-matter` or `fronts`.
    """
    terms = query_terms(query)
    parts: list[str] = []
    for term in terms:
        escaped = term.replace('"', '""')
        if " " in term:
            # Phrase: prefix only the last word so "front matter" still
            # prefers the phrase, while "front matters" can match.
            clause = f'"{escaped}"'
            if prefix:
                clause += "*"
            parts.append(clause)
        elif prefix:
            parts.append(f'"{escaped}"*')
        else:
            parts.append(f'"{escaped}"')
    joiner = " OR " if combine.upper() == "OR" else " AND "
    return joiner.join(parts)


def _membership_tags(project: str, conn: sqlite3.Connection) -> set[str]:
    include = {project}
    path = f"wiki/projects/{project}.md"
    row = conn.execute(
        "SELECT include_tags_json FROM notes WHERE path = ?", (path,)
    ).fetchone()
    if row is not None:
        include.update(load_json_list(row["include_tags_json"]))
        include.add(project)
    return include


def note_matches_filters(
    row: sqlite3.Row,
    *,
    project: str | None,
    tags: list[str] | None,
    type: str | None,
    membership: set[str] | None,
) -> bool:
    if type and (row["type"] or "") != type:
        return False
    projects = load_json_list(row["projects_json"])
    page_tags = load_json_list(row["tags_json"])
    if project:
        names = set(projects) | set(page_tags)
        if membership is None:
            if project not in names and project not in projects:
                return False
        elif names.isdisjoint(membership):
            return False
    if tags:
        combined = set(page_tags) | set(projects)
        if any(tag not in combined for tag in tags):
            return False
    return True


def format_tag_line(projects: list[str], tags: list[str], note_type: str | None) -> str:
    bits = []
    if note_type:
        bits.append(f"type: {note_type}")
    if projects:
        bits.append("projects: " + ", ".join(projects))
    if tags:
        bits.append("tags: " + ", ".join(tags))
    return " · ".join(bits)


def format_search_hits(hits: list[dict]) -> str:
    if not hits:
        return _safe("No results.")
    lines: list[str] = []
    for hit in hits:
        title = hit["title"] or Path(hit["path"]).stem
        lines.append(f"- **{title}** — `{hit['path']}`")
        if hit.get("breadcrumb"):
            lines.append(f"  {hit['breadcrumb']}")
        meta = format_tag_line(hit.get("projects") or [], hit.get("tags") or [], hit.get("type"))
        if meta:
            lines.append(f"  {meta}")
        snippet = (hit.get("snippet") or "").strip()
        if snippet:
            for snippet_line in snippet.splitlines():
                lines.append(f"  {snippet_line}")
    return _safe("\n".join(lines))


def format_note_list(rows: list[dict]) -> str:
    if not rows:
        return _safe("No notes.")
    lines: list[str] = []
    for row in rows:
        title = row["title"] or Path(row["path"]).stem
        lines.append(f"- **{title}** — `{row['path']}`")
        meta = format_tag_line(row.get("projects") or [], row.get("tags") or [], row.get("type"))
        if meta:
            lines.append(f"  {meta}")
        if row.get("description"):
            lines.append(f"  {row['description']}")
    return _safe("\n".join(lines))


def format_project_list(rows: list[dict]) -> str:
    if not rows:
        return _safe("No projects.")
    lines: list[str] = []
    for row in rows:
        name = row["name"]
        description = (row.get("description") or "").strip()
        if description:
            lines.append(f"- **{name}** — {description}")
        else:
            lines.append(f"- **{name}**")
        extras = []
        if row.get("status"):
            extras.append(f"status: {row['status']}")
        if row.get("path"):
            extras.append(f"`{row['path']}`")
        if extras:
            lines.append("  " + " · ".join(extras))
    return _safe("\n".join(lines))


def format_read_note(
    *,
    path: str,
    title: str,
    note_type: str | None,
    projects: list[str],
    tags: list[str],
    description: str,
    links: list[tuple[str, str]],
    backlinks: list[tuple[str, str]],
    body: str,
    section: str | None,
) -> str:
    chunks = chunk_body(body)
    header = [
        f"# {title}",
        format_tag_line(projects, tags, note_type),
    ]
    if description:
        header.append(description)
    header.append("")
    header.append("## Links")
    if links:
        for dest_path, dest_title in links:
            if dest_path:
                header.append(f"- `{dest_path}` — {dest_title}")
            else:
                header.append(f"- [[{dest_title}]]")
    else:
        header.append("- (none)")
    header.append("")
    header.append("## Backlinks")
    if backlinks:
        for src_path, src_title in backlinks:
            header.append(f"- `{src_path}` — {src_title}")
    else:
        header.append("- (none)")
    header.append("")

    if section:
        selected = find_section(chunks, section)
        if selected is None:
            headings = [c.breadcrumb or c.heading for c in chunks if c.heading]
            outline = ", ".join(headings) if headings else "(no headings)"
            header.append(f"Section not found: {section}")
            header.append(f"Headings: {outline}")
            return _safe("\n".join(header).rstrip() + "\n")
        header.append(f"## {selected[0].breadcrumb or selected[0].heading or section}")
        header.append("")
        header.append("\n\n".join(chunk.text for chunk in selected).strip())
        return _safe("\n".join(header).rstrip() + "\n")

    if len(body) > LONG_PAGE_CHARS:
        header.append("## Outline")
        header.append(
            f"This page is long ({len(body)} characters). Pass `section` to read a heading."
        )
        if not any(chunk.heading for chunk in chunks):
            header.append("- (no headings)")
        else:
            for chunk in chunks:
                if not chunk.heading:
                    continue
                indent = max(0, (chunk.level or 1) - 1)
                header.append(f"{'  ' * indent}- {chunk.heading}")
        return _safe("\n".join(header).rstrip() + "\n")

    header.append("## Body")
    header.append("")
    header.append(body.strip())
    return _safe("\n".join(header).rstrip() + "\n")


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    mode: str = "hybrid",
    project: str | None = None,
    tags: list[str] | None = None,
    type: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    after: str | None = None,
    embedder: object | None = None,
) -> str:
    query = (query or "").strip()
    if not query:
        return _safe("No results.")
    limit = clamp_limit(limit, DEFAULT_SEARCH_LIMIT, SEARCH_LIMIT_CAP)
    membership = _membership_tags(project, conn) if project else None
    mode = (mode or "hybrid").strip().lower()
    if mode not in {"hybrid", "semantic", "fulltext"}:
        mode = "hybrid"
    has_vectors = embedder is not None and _embedding_count(conn, embedder) > 0
    if mode in {"hybrid", "semantic"} and not has_vectors:
        mode = "fulltext"

    fulltext_hits: list[dict] = []
    semantic_hits: list[dict] = []
    if mode in {"fulltext", "hybrid"}:
        fulltext_hits = _fulltext_hits(
            conn, query, project=project, tags=tags, type=type, membership=membership, limit=limit
        )
    if mode in {"semantic", "hybrid"} and embedder is not None:
        semantic_hits = _semantic_hits(
            conn,
            query,
            embedder,
            project=project,
            tags=tags,
            type=type,
            membership=membership,
        )

    if mode == "semantic":
        ordered = semantic_hits
    elif mode == "fulltext":
        ordered = fulltext_hits
    else:
        ordered = _rrf_merge(fulltext_hits, semantic_hits)

    paths = [hit["path"] for hit in ordered]
    if after:
        try:
            index = paths.index(after)
            ordered = ordered[index + 1 :]
        except ValueError:
            pass
    return format_search_hits(ordered[:limit])


def _embedding_count(conn: sqlite3.Connection, embedder: object) -> int:
    model = getattr(embedder, "model_name", None)
    if not model:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM chunk_embeddings WHERE model = ?",
        (model,),
    ).fetchone()
    return int(row["n"] if row else 0)


def _hit_from_row(row: sqlite3.Row, snippet: str) -> dict:
    return {
        "path": row["path"],
        "title": row["title"],
        "type": row["type"],
        "projects": load_json_list(row["projects_json"]),
        "tags": load_json_list(row["tags_json"]),
        "breadcrumb": row["breadcrumb"],
        "snippet": snippet,
    }


def _collect_fts_rows(
    conn: sqlite3.Connection,
    match: str,
    *,
    project: str | None,
    tags: list[str] | None,
    type: str | None,
    membership: set[str] | None,
) -> list[dict] | None:
    if not match:
        return []
    sql = """
        SELECT
            chunks.path AS path,
            notes.title AS title,
            notes.type AS type,
            notes.projects_json AS projects_json,
            notes.tags_json AS tags_json,
            chunks.breadcrumb AS breadcrumb,
            snippet(chunks_fts, 0, '**', '**', '…', 10) AS snippet,
            rank AS rank
        FROM chunks_fts
        JOIN chunks ON chunks.id = chunks_fts.rowid
        JOIN notes ON notes.path = chunks.path
        WHERE chunks_fts MATCH ?
        ORDER BY rank, chunks.path, chunks.ordinal
    """
    try:
        rows = conn.execute(sql, (match,)).fetchall()
    except sqlite3.OperationalError:
        return None
    hits: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not note_matches_filters(
            row, project=project, tags=tags, type=type, membership=membership
        ):
            continue
        path = row["path"]
        if path in seen:
            continue
        seen.add(path)
        hits.append(_hit_from_row(row, row["snippet"] or ""))
    return hits


def _fulltext_hits(
    conn: sqlite3.Connection,
    query: str,
    *,
    project: str | None,
    tags: list[str] | None,
    type: str | None,
    membership: set[str] | None,
    limit: int,
) -> list[dict]:
    """Prefix-AND first; fall back to prefix-OR when AND is too sparse."""
    and_match = fts_query(query, combine="AND", prefix=True)
    hits = _collect_fts_rows(
        conn, and_match, project=project, tags=tags, type=type, membership=membership
    )
    if hits is None:
        hits = []
    if len(hits) >= limit:
        return hits
    or_match = fts_query(query, combine="OR", prefix=True)
    if or_match == and_match:
        return hits
    extra = _collect_fts_rows(
        conn, or_match, project=project, tags=tags, type=type, membership=membership
    )
    if not extra:
        return hits
    seen = {hit["path"] for hit in hits}
    for hit in extra:
        if hit["path"] in seen:
            continue
        hits.append(hit)
        seen.add(hit["path"])
    return hits


def _plain_snippet(text: str, query: str, width: int = 160) -> str:
    body = re.sub(r"\s+", " ", (text or "").strip())
    if not body:
        return ""
    terms = [t.casefold() for t in query_terms(query) if " " not in t]
    lower = body.casefold()
    pos = -1
    for term in terms:
        pos = lower.find(term)
        if pos >= 0:
            break
    if pos < 0:
        snippet = body[:width]
        return snippet + ("…" if len(body) > width else "")
    start = max(0, pos - width // 4)
    end = min(len(body), start + width)
    snippet = body[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(body):
        snippet = snippet + "…"
    return snippet


def _semantic_hits(
    conn: sqlite3.Connection,
    query: str,
    embedder: object,
    *,
    project: str | None,
    tags: list[str] | None,
    type: str | None,
    membership: set[str] | None,
) -> list[dict]:
    from vault_search.embed import dot, l2_normalize, unpack_vector

    embed_queries = getattr(embedder, "embed_queries")
    vectors = embed_queries([query])
    if not vectors:
        return []
    query_vec = l2_normalize(vectors[0])
    model = embedder.model_name  # type: ignore[attr-defined]
    rows = conn.execute(
        """
        SELECT
            e.dim AS dim,
            e.embedding AS embedding,
            chunks.path AS path,
            notes.title AS title,
            notes.type AS type,
            notes.projects_json AS projects_json,
            notes.tags_json AS tags_json,
            chunks.breadcrumb AS breadcrumb,
            chunks.text AS text
        FROM chunk_embeddings e
        JOIN chunks ON chunks.id = e.chunk_id
        JOIN notes ON notes.path = chunks.path
        WHERE e.model = ?
        """,
        (model,),
    ).fetchall()
    best: dict[str, tuple[float, dict]] = {}
    for row in rows:
        if not note_matches_filters(
            row, project=project, tags=tags, type=type, membership=membership
        ):
            continue
        vector = unpack_vector(row["embedding"], row["dim"])
        score = dot(query_vec, vector)
        path = row["path"]
        hit = _hit_from_row(row, _plain_snippet(row["text"] or "", query))
        prior = best.get(path)
        if prior is None or score > prior[0]:
            best[path] = (score, hit)
    ranked = sorted(best.values(), key=lambda item: (-item[0], item[1]["path"]))
    return [item[1] for item in ranked if item[0] > 0]


def _rrf_merge(*lists: list[dict], k: int = 60) -> list[dict]:
    from vault_search.embed import rrf_fuse

    by_path: dict[str, dict] = {}
    ranked: list[list[str]] = []
    for hits in lists:
        paths: list[str] = []
        for hit in hits:
            path = hit["path"]
            # Prefer a full-text snippet when both lists share a path.
            if path not in by_path or (hit.get("snippet") and not by_path[path].get("snippet")):
                by_path[path] = hit
            elif path not in by_path:
                by_path[path] = hit
            paths.append(path)
        ranked.append(paths)
    return [by_path[path] for path in rrf_fuse(*ranked, k=k) if path in by_path]



def list_notes(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    tags: list[str] | None = None,
    type: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    after: str | None = None,
) -> str:
    limit = clamp_limit(limit, DEFAULT_LIST_LIMIT, LIST_LIMIT_CAP)
    membership = _membership_tags(project, conn) if project else None
    rows = conn.execute(
        """
        SELECT path, title, type, description, projects_json, tags_json
        FROM notes
        ORDER BY path
        """
    ).fetchall()
    selected: list[dict] = []
    for row in rows:
        if after and row["path"] <= after:
            continue
        if not note_matches_filters(
            row, project=project, tags=tags, type=type, membership=membership
        ):
            continue
        selected.append(
            {
                "path": row["path"],
                "title": row["title"],
                "type": row["type"],
                "description": row["description"],
                "projects": load_json_list(row["projects_json"]),
                "tags": load_json_list(row["tags_json"]),
            }
        )
        if len(selected) >= limit:
            break
    return format_note_list(selected)


def list_projects(conn: sqlite3.Connection) -> str:
    seen: dict[str, dict] = {}
    for row in conn.execute(
        """
        SELECT path, title, description, status, projects_json, include_tags_json, type
        FROM notes
        ORDER BY path
        """
    ):
        path = row["path"]
        if row["type"] == "project" or path.startswith("wiki/projects/"):
            name = Path(path).stem
            seen[name] = {
                "name": name,
                "description": row["description"] or "",
                "status": row["status"] or "",
                "path": path,
            }
        for project in load_json_list(row["projects_json"]):
            seen.setdefault(
                project,
                {"name": project, "description": "", "status": "", "path": ""},
            )
        # include_tags are membership filters, not project names; skip as names
    rows = [seen[name] for name in sorted(seen)]
    return format_project_list(rows)


def read_note(conn: sqlite3.Connection, path: str, section: str | None = None) -> str:
    resolved = _resolve_path(conn, path)
    if resolved is None:
        return _safe("not_found")
    row = conn.execute(
        """
        SELECT path, title, type, description, projects_json, tags_json, body
        FROM notes WHERE path = ?
        """,
        (resolved,),
    ).fetchone()
    if row is None:
        return _safe("not_found")
    projects = load_json_list(row["projects_json"])
    tags = load_json_list(row["tags_json"])
    links = []
    for link in conn.execute(
        """
        SELECT dest_stem, dest_path,
               (SELECT title FROM notes WHERE notes.path = links.dest_path) AS dest_title
        FROM links
        WHERE src_path = ?
        ORDER BY dest_stem
        """,
        (resolved,),
    ):
        dest_path = link["dest_path"] or ""
        dest_title = link["dest_title"] or link["dest_stem"]
        links.append((dest_path, dest_title))
    backlinks = []
    for link in conn.execute(
        """
        SELECT src_path, notes.title AS src_title
        FROM links
        JOIN notes ON notes.path = links.src_path
        WHERE dest_path = ?
           OR (dest_path IS NULL AND dest_stem = ?)
        ORDER BY src_path
        """,
        (resolved, Path(resolved).stem),
    ):
        if link["src_path"] == resolved:
            continue
        backlinks.append((link["src_path"], link["src_title"] or Path(link["src_path"]).stem))
    return format_read_note(
        path=row["path"],
        title=row["title"],
        note_type=row["type"],
        projects=projects,
        tags=tags,
        description=row["description"] or "",
        links=links,
        backlinks=backlinks,
        body=row["body"] or "",
        section=section,
    )


def _resolve_path(conn: sqlite3.Connection, path: str) -> str | None:
    candidate = (path or "").strip().lstrip("./")
    if not candidate:
        return None
    options = [candidate]
    if not candidate.endswith(".md"):
        options.append(candidate + ".md")
    for option in options:
        row = conn.execute("SELECT path FROM notes WHERE path = ?", (option,)).fetchone()
        if row:
            return row["path"]
    stem = Path(candidate.removesuffix(".md")).name
    rows = conn.execute(
        "SELECT path FROM notes WHERE path LIKE ?", (f"%/{stem}.md",)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["path"]
    return None
