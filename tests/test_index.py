"""Indexer, chunking, filters, pagination, backlinks, masking, and outline tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from vault_search.client import Client
from vault_search.config import Settings
from vault_search.parse import chunk_body, parse_front_matter, parse_note, wikilink_stems
from vault_search.query import LONG_PAGE_CHARS
from vault_search.secrets import RULE_GITHUB

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _token() -> str:
    return "gh" + "p_" + ("A" * 36)


def test_chunking_and_breadcrumbs() -> None:
    text = (FIXTURES / "wiki/concepts/evals.md").read_text(encoding="utf-8")
    note = parse_note("wiki/concepts/evals.md", text, source="wiki")
    crumbs = [chunk.breadcrumb for chunk in note.chunks]
    headings = [chunk.heading for chunk in note.chunks]
    assert note.links == ["scoring", "thresholds"]
    assert "Scoring" in headings
    assert "Thresholds" in headings
    assert "Scoring" in crumbs
    assert note.chunks[0].heading == ""  # preamble
    # Nested fixture heading path
    long = (FIXTURES / "wiki/concepts/long-page.md").read_text(encoding="utf-8")
    long_note = parse_note("wiki/concepts/long-page.md", long, source="wiki")
    assert any(chunk.breadcrumb == "Second heading > Nested" for chunk in long_note.chunks)
    assert any(chunk.heading == "Nested" for chunk in long_note.chunks)


def test_front_matter_lists_and_wikilinks() -> None:
    text = "---\ntitle: T\nprojects: [alpha, beta]\ntags:\n  - evals\n---\nSee [[foo#x|Foo]] and [[bar]].\n"
    meta, err, body = parse_front_matter(text)
    assert err is None
    assert meta["projects"] == ["alpha", "beta"]
    assert meta["tags"] == ["evals"]
    assert wikilink_stems(text) == ["foo", "bar"]
    assert "See" in body


def test_index_counts_and_skips_raw_by_default(client: Client) -> None:
    stats = client.reindex()
    assert stats.pages == 8  # 2 projects + evals, scoring, thresholds, side-note, topic, long
    assert stats.chunks >= stats.pages
    assert stats.skipped or stats.unchanged == stats.pages
    listed = client.list_notes()
    assert "optional.md" not in listed
    assert "wiki/concepts/evals.md" in listed


def test_include_raw(vault_root: Path, tmp_path: Path) -> None:
    settings = Settings(vault_root=vault_root, index_path=tmp_path / "raw.db", include_raw=True)
    with Client(settings) as client:
        stats = client.reindex()
        assert stats.pages == 9
        assert "raw/chat/optional.md" in client.list_notes()


def test_filters_project_tags_type(client: Client) -> None:
    alpha = client.list_notes(project="alpha")
    assert "wiki/concepts/evals.md" in alpha
    assert "wiki/ideas/side-note.md" not in alpha
    # include_tags membership: topic-page is global + tag eval-topic
    assert "wiki/syntheses/topic-page.md" in alpha
    beta = client.list_notes(project="beta")
    assert "wiki/ideas/side-note.md" in beta
    assert "wiki/concepts/evals.md" not in beta
    tagged = client.list_notes(tags=["aside"])
    assert "side-note.md" in tagged
    assert "evals.md" not in tagged
    ideas = client.list_notes(type="idea")
    assert "side-note.md" in ideas
    assert "evals.md" not in ideas
    # unfiltered still sees everything
    all_notes = client.list_notes(limit=100)
    assert "side-note.md" in all_notes and "evals.md" in all_notes


def test_list_projects(client: Client) -> None:
    text = client.list_projects()
    assert "**alpha**" in text
    assert "Synthetic alpha project" in text
    assert "**beta**" in text
    assert "**global**" in text  # referenced by topic-page


def test_pagination_list_and_search(client: Client) -> None:
    first = client.list_notes(project="alpha", limit=2)
    paths = [line.split("`")[1] for line in first.splitlines() if "`wiki/" in line]
    assert len(paths) == 2
    second = client.list_notes(project="alpha", limit=2, after=paths[-1])
    second_paths = [line.split("`")[1] for line in second.splitlines() if "`wiki/" in line]
    assert second_paths
    assert second_paths[0] > paths[-1]
    assert set(paths).isdisjoint(second_paths)

    results = client.search("scoring", limit=2)
    assert "wiki/" in results
    first_hit = [line.split("`")[1] for line in results.splitlines() if "`wiki/" in line][0]
    paged = client.search("scoring", limit=2, after=first_hit)
    paged_paths = [line.split("`")[1] for line in paged.splitlines() if "`wiki/" in line]
    assert first_hit not in paged_paths


def test_backlinks_and_read_section(client: Client) -> None:
    scoring = client.read_note("wiki/concepts/scoring.md")
    assert "# Scoring" in scoring
    assert "## Backlinks" in scoring
    assert "`wiki/concepts/evals.md` — Evals" in scoring
    assert "## Links" in scoring
    evals = client.read_note("wiki/concepts/evals.md", section="Scoring")
    assert "numeric scale" in evals
    assert "Pass and fail cutoffs" not in evals
    missing = client.read_note("wiki/concepts/does-not-exist.md")
    assert missing.strip() == "not_found"
    by_stem = client.read_note("scoring")
    assert "# Scoring" in by_stem


def test_long_page_outline(client: Client) -> None:
    text = client.read_note("wiki/concepts/long-page.md")
    assert "## Outline" in text
    assert "First heading" in text
    assert "Second heading" in text
    assert "Nested" in text
    assert "Pass `section`" in text
    # The repeated filler that makes the page long should not all be dumped.
    assert text.lower().count("filler text") < 5
    section = client.read_note("wiki/concepts/long-page.md", section="Nested")
    assert "Second heading > Nested" in section or "## Nested" in section
    assert "## Outline" not in section
    # Body length of the fixture is above the threshold
    raw = (FIXTURES / "wiki/concepts/long-page.md").read_text(encoding="utf-8")
    _, _, body = __import__("vault_search.parse", fromlist=["parse_front_matter"]).parse_front_matter(raw)
    assert len(body) > LONG_PAGE_CHARS


def test_search_is_compact_markdown(client: Client) -> None:
    text = client.search("rubric", project="alpha")
    assert "wiki/concepts/evals.md" in text or "wiki/concepts/scoring.md" in text
    assert "**" in text
    assert "uuid" not in text.lower()
    assert " rowid" not in text.lower()
    # no raw FTS score dump
    assert "rank=" not in text
    assert "-0." not in text


def test_masking_on_every_output(vault_root: Path, tmp_path: Path) -> None:
    token = _token()
    work = tmp_path / "vault"
    # Copy fixtures, then add a note that contains a runtime-assembled token.
    dest_wiki = work / "wiki" / "concepts"
    dest_wiki.mkdir(parents=True)
    src = FIXTURES / "wiki"
    for path in src.rglob("*.md"):
        target = work / "wiki" / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    secret_note = dest_wiki / "secret-note.md"
    secret_note.write_text(
        "---\n"
        "title: Secret fixture\n"
        "type: concept\n"
        "projects: [alpha]\n"
        "scope: project\n"
        "description: Holds a synthetic token assembled at test time.\n"
        "tags: [secret-fixture]\n"
        "---\n\n"
        f"The credential is {token} and should never appear in output.\n",
        encoding="utf-8",
    )
    with Client(Settings(vault_root=work, index_path=tmp_path / "masked.db")) as client:
        client.reindex()
        search = client.search("credential")
        note = client.read_note("wiki/concepts/secret-note.md")
        listed = client.list_notes(tags=["secret-fixture"])
        for output in (search, note, listed):
            assert token not in output
        # Search and read include the body, so the token must be masked there.
        # list_notes only shows the header, which has no token to redact.
        assert "REDACTED" in search
        assert "REDACTED" in note
        assert "secret-note.md" in listed


def test_incremental_reindex_uses_hash_and_sha(vault_root: Path, tmp_path: Path) -> None:
    work = tmp_path / "gitvault"
    wiki = work / "wiki" / "concepts"
    wiki.mkdir(parents=True)
    # Two pages, then a git commit so the index can store a SHA.
    first = wiki / "one.md"
    first.write_text(
        "---\ntitle: One\ntype: concept\nprojects: [alpha]\nscope: project\n"
        "description: First.\ntags: []\n---\n\nHello one.\n",
        encoding="utf-8",
    )
    second = wiki / "two.md"
    second.write_text(
        "---\ntitle: Two\ntype: concept\nprojects: [alpha]\nscope: project\n"
        "description: Second.\ntags: []\n---\n\nHello two.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=work, check=True)
    subprocess.run(["git", "add", "."], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=work, check=True, capture_output=True)

    settings = Settings(vault_root=work, index_path=tmp_path / "inc.db")
    with Client(settings) as client:
        first_stats = client.reindex()
        assert first_stats.pages == 2
        assert first_stats.upserted == 2
        assert first_stats.vault_sha
        sha = first_stats.vault_sha
        again = client.reindex()
        assert again.skipped or again.upserted == 0
        assert again.vault_sha == sha

        first.write_text(first.read_text(encoding="utf-8").replace("Hello one.", "Hello one (edited)."), encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "edit"], cwd=work, check=True, capture_output=True)
        updated = client.reindex()
        assert updated.upserted == 1
        assert updated.unchanged == 1
        assert updated.vault_sha != sha
        assert "Hello one (edited)." in client.read_note("wiki/concepts/one.md")

        first.unlink()
        removed = client.reindex()
        assert removed.deleted == 1
        assert client.read_note("wiki/concepts/one.md").strip() == "not_found"


def test_chunk_body_preamble_and_levels() -> None:
    body = "intro\n\n# A\n\naa\n\n## B\n\nbb\n\n# C\n\ncc\n"
    chunks = chunk_body(body)
    crumbs = [c.breadcrumb for c in chunks]
    assert crumbs[0] == ""
    assert "intro" in chunks[0].text
    assert "A > B" in crumbs
    assert chunks[-1].heading == "C"
    # B is closed before C
    assert all("cc" not in c.text for c in chunks if c.heading == "B")


def test_long_running_client_follows_a_rebuilt_index_file(tmp_path: Path) -> None:
    """A schema rebuild by another process replaces the file; readers must follow it."""
    from vault_search.index import remove_index_files

    settings = Settings(vault_root=FIXTURES, index_path=tmp_path / "index.db")
    with Client(settings, autoload=False) as server:
        server.reindex()
        assert "**alpha**" in server.list_projects()
        # The sync timer deletes the file and recreates it empty, then refills it.
        remove_index_files(settings.index_path)
        with Client(settings, autoload=False):
            pass
        assert server.list_notes() == "No notes."
        with Client(settings, autoload=False) as sync:
            sync.reindex()
        assert "**alpha**" in server.list_projects()
