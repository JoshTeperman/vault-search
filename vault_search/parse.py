"""Front matter, wikilinks, and heading chunks. Stdlib only.

Front matter is the same small YAML subset the vault uses: `key: value`,
inline lists `[a, b]`, block lists (`- a`), quoted strings, and `#` comments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
SCHEMA_TYPES = {"concept", "entity", "idea", "source", "synthesis", "project", "meta", "raw"}


def _strip_comment(value: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out).strip()


def _scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    return value


def _split_inline_list(value: str) -> list:
    inner = value.strip()[1:-1]
    items, buf, quote, depth = [], "", None, 0
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(buf)
            buf = ""
            continue
        buf += ch
    if buf.strip():
        items.append(buf)
    return [_scalar(item) for item in items if item.strip()]


def as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def parse_front_matter(text: str) -> tuple[dict | None, str | None, str]:
    """Return (meta, error, body). Body has no leading/trailing fence."""
    if not text.startswith("---"):
        return None, "no front matter", text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None, "unterminated front matter", text
    data, key = {}, None
    for number, raw in enumerate(lines[1:end], start=2):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = re.match(r"^\s*-\s+(.*)$", raw) or re.match(r"^-\s+(.*)$", raw)
        if match and key is not None:
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(_scalar(_strip_comment(match.group(1))))
            continue
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", raw)
        if not match:
            return data, f"line {number}: cannot parse {raw!r}", "\n".join(lines[end + 1 :])
        key, val = match.group(1), _strip_comment(match.group(2))
        if val == "":
            data[key] = []
        elif val.startswith("[") and val.endswith("]"):
            data[key] = _split_inline_list(val)
        else:
            data[key] = _scalar(val)
    return data, None, "\n".join(lines[end + 1 :])


def link_target(ref: str) -> str:
    """'[[foo]]' / 'wiki/x/foo.md' / 'foo' -> 'foo' (resolved by stem)."""
    ref = str(ref).strip()
    match = WIKILINK_RE.search(ref)
    if match:
        ref = match.group(1).strip()
    if ref.endswith(".md") or "/" in ref:
        return ref.rsplit("/", 1)[-1].removesuffix(".md")
    return ref


def wikilink_stems(text: str) -> list[str]:
    """Unique wikilink stems, in first-seen order."""
    seen: list[str] = []
    for match in WIKILINK_RE.finditer(text):
        stem = link_target(match.group(1))
        if stem and stem not in seen:
            seen.append(stem)
    return seen


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading: str
    breadcrumb: str
    text: str
    start_line: int
    end_line: int
    level: int


def chunk_body(body: str) -> list[Chunk]:
    """Split markdown body into heading-delimited chunks with breadcrumbs.

    Preamble before the first heading is a chunk with an empty heading.
    A heading's chunk runs until the next heading of the same or higher level.
    """
    lines = body.splitlines()
    starts: list[tuple[int, int, str]] = []  # (line_index, level, title)
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match:
            starts.append((index, len(match.group(1)), match.group(2).strip()))

    if not starts:
        text = body.strip()
        return [Chunk(0, "", "", text, 1, max(1, len(lines)), 0)] if text or body == "" else []

    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []
    ordinal = 0

    # Preamble
    first_start = starts[0][0]
    if first_start > 0:
        preamble = "\n".join(lines[:first_start]).strip()
        if preamble:
            chunks.append(Chunk(ordinal, "", "", preamble, 1, first_start, 0))
            ordinal += 1

    for i, (start, level, title) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        breadcrumb = " > ".join(name for _, name in stack)
        text = "\n".join(lines[start:end]).strip()
        chunks.append(
            Chunk(
                ordinal=ordinal,
                heading=title,
                breadcrumb=breadcrumb,
                text=text,
                start_line=start + 1,
                end_line=max(start + 1, end),
                level=level,
            )
        )
        ordinal += 1
    return chunks


def find_section(chunks: list[Chunk], section: str) -> list[Chunk] | None:
    """Return the chunk for *section* plus descendants, or None if missing."""
    wanted = section.strip().lower()
    if not wanted:
        return None
    for i, chunk in enumerate(chunks):
        aliases = {chunk.heading.lower(), chunk.breadcrumb.lower()}
        if chunk.breadcrumb:
            aliases.add(chunk.breadcrumb.split(" > ")[-1].lower())
        if wanted in aliases:
            selected = [chunk]
            for child in chunks[i + 1 :]:
                if chunk.level and child.level and child.level <= chunk.level:
                    break
                if not chunk.level:
                    break
                selected.append(child)
            return selected
    return None


@dataclass
class ParsedNote:
    path: str
    meta: dict
    error: str | None
    body: str
    title: str
    note_type: str | None
    description: str
    created: str
    updated: str
    status: str
    source: str
    projects: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    include_tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    example: bool = False


def parse_note(path: str, text: str, *, source: str) -> ParsedNote:
    meta, error, body = parse_front_matter(text)
    meta = meta or {}
    title = str(meta.get("title") or "").strip()
    if not title:
        title = path.rsplit("/", 1)[-1].removesuffix(".md")
    created = str(meta.get("created") or meta.get("captured") or "")
    updated = str(meta.get("updated") or "")
    note_type = meta.get("type")
    if note_type is not None:
        note_type = str(note_type)
    elif source == "raw":
        note_type = "raw"
    links = wikilink_stems(text)
    return ParsedNote(
        path=path,
        meta=meta,
        error=error,
        body=body,
        title=title,
        note_type=note_type,
        description=str(meta.get("description") or "").strip(),
        projects=as_list(meta.get("projects")),
        tags=as_list(meta.get("tags")),
        include_tags=as_list(meta.get("include_tags")),
        created=str(created),
        updated=str(updated),
        status=str(meta.get("status") or ""),
        source=source,
        links=links,
        chunks=chunk_body(body),
        example=bool(meta.get("example")),
    )
