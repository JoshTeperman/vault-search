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


def fts_query(query: str) -> str:
    """Turn a user query into a safe FTS5 MATCH string (AND of quoted tokens)."""
    tokens = _TOKEN.findall(query.strip())
    parts = []
    for token in tokens:
        upper = token.upper()
        if upper in {"AND", "OR", "NOT"} or token in {"(", ")"}:
            parts.append(upper if upper in {"AND", "OR", "NOT"} else token)
            continue
        escaped = token.replace('"', '""')
        parts.append(f'"{escaped}"')
    return " AND ".join(parts)


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
    mode: str = "fulltext",
    project: str | None = None,
    tags: list[str] | None = None,
    type: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    after: str | None = None,
) -> str:
    del mode  # hybrid/semantic land in milestone 5; fulltext is used now
    query = (query or "").strip()
    if not query:
        return _safe("No results.")
    limit = clamp_limit(limit, DEFAULT_SEARCH_LIMIT, SEARCH_LIMIT_CAP)
    membership = _membership_tags(project, conn) if project else None
    match = fts_query(query)
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
        return _safe("No results.")

    hits: list[dict] = []
    seen_paths: list[str] = []
    seen_set: set[str] = set()
    for row in rows:
        if not note_matches_filters(
            row, project=project, tags=tags, type=type, membership=membership
        ):
            continue
        path = row["path"]
        if path in seen_set:
            continue
        seen_set.add(path)
        seen_paths.append(path)
        hits.append(
            {
                "path": path,
                "title": row["title"],
                "type": row["type"],
                "projects": load_json_list(row["projects_json"]),
                "tags": load_json_list(row["tags_json"]),
                "breadcrumb": row["breadcrumb"],
                "snippet": row["snippet"],
            }
        )

    if after:
        try:
            index = seen_paths.index(after)
            hits = hits[index + 1 :]
        except ValueError:
            pass
    return format_search_hits(hits[:limit])


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
