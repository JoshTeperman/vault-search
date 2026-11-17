"""FTS recall, hybrid/semantic search, incremental embeddings, filter application."""

from __future__ import annotations

import re
from pathlib import Path

from vault_search.client import Client
from vault_search.config import Settings
from vault_search.embed import rrf_fuse
from vault_search.query import fts_query, query_terms

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# Deterministic topic axes so semantic tests do not depend on hash() or collisions.
_TOPIC_DIM = {
    "apple": 0,
    "orchard": 0,
    "orchards": 0,
    "pear": 0,
    "harvest": 0,
    "cider": 0,
    "apiary": 0,
    "river": 1,
    "rivers": 1,
    "delta": 1,
    "silt": 1,
    "boats": 1,
    "tidal": 1,
    "marshes": 1,
    "front": 2,
    "matter": 2,
    "yaml": 2,
    "fields": 2,
}


class FakeEmbedder:
    model_name = "fake-test"
    dim = 8

    def _vec(self, text: str) -> list[float]:
        values = [0.0] * self.dim
        for token in re.findall(r"[a-z0-9]+", (text or "").lower()):
            if token in _TOPIC_DIM:
                values[_TOPIC_DIM[token]] += 1.0
        return values

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_queries(self, texts):
        return [self._vec(t) for t in texts]


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _recall_vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    _write(
        root,
        "wiki/sources/phrase.md",
        "---\ntitle: Phrase page\ntype: source\n---\n\nThe exact front matter phrase lives here.\n",
    )
    _write(
        root,
        "wiki/concepts/front-matter-guide.md",
        "---\ntitle: Front-matter guide\ntype: concept\nprojects: [alpha]\n---\n\n"
        "YAML front-matter fields sit at the top of a note.\n",
    )
    _write(
        root,
        "wiki/concepts/front-door.md",
        "---\ntitle: Front door\ntype: concept\n---\n\nThe front door is painted blue.\n",
    )
    _write(
        root,
        "wiki/concepts/unrelated.md",
        "---\ntitle: Unrelated\ntype: concept\n---\n\nA note about orchards and rivers.\n",
    )
    return root


def test_query_terms_split_hyphens_and_phrases() -> None:
    assert query_terms("front matter") == ["front", "matter"]
    assert query_terms("front-matter") == ["front", "matter"]
    assert "front matter" in query_terms('"front matter" guide')
    assert "AND" not in query_terms("front AND matter")


def test_fts_query_uses_prefix_and_or() -> None:
    assert fts_query("front matter") == '"front"* AND "matter"*'
    assert fts_query("front matter", combine="OR") == '"front"* OR "matter"*'


def test_front_matter_recall_prefix_and_or_fallback(tmp_path: Path) -> None:
    root = _recall_vault(tmp_path)
    settings = Settings(vault_root=root, index_path=tmp_path / "index.db")
    with Client(settings, autoload=False) as client:
        client.reindex()
        text = client.search("front matter", mode="fulltext", limit=8)
        assert "wiki/sources/phrase.md" in text
        assert "wiki/concepts/front-matter-guide.md" in text
        # OR fallback fills in the page that only has "front"
        assert "wiki/concepts/front-door.md" in text
        assert "wiki/concepts/unrelated.md" not in text
        # Phrase / AND hits stay ahead of the OR-only door page.
        assert text.index("phrase.md") < text.index("front-door.md")
        assert text.index("front-matter-guide.md") < text.index("front-door.md")


def test_hybrid_and_semantic_with_fake_embedder(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(
        root,
        "wiki/concepts/orchards.md",
        "---\ntitle: Orchards\ntype: concept\n---\n\n"
        "Apple orchards, pear trees, and autumn harvest cider.\n",
    )
    _write(
        root,
        "wiki/concepts/rivers.md",
        "---\ntitle: Rivers\ntype: concept\n---\n\n"
        "A river delta with silt, boats, and tidal marshes.\n",
    )
    _write(
        root,
        "wiki/concepts/front-matter-guide.md",
        "---\ntitle: Front-matter guide\ntype: concept\nprojects: [alpha]\n---\n\n"
        "YAML front-matter fields sit at the top of a note.\n",
    )
    settings = Settings(vault_root=root, index_path=tmp_path / "index.db")
    embedder = FakeEmbedder()
    with Client(settings, embedder=embedder, autoload=False) as client:
        stats = client.reindex()
        assert stats.embeddings_written == stats.chunks
        assert stats.embeddings_written > 0
        semantic = client.search("apple orchard harvest cider", mode="semantic")
        assert "orchards.md" in semantic
        assert "rivers.md" not in semantic
        hybrid = client.search("front matter", mode="hybrid")
        assert "front-matter-guide.md" in hybrid
        # Filters apply to semantic too.
        filtered = client.search("front matter", mode="semantic", project="alpha")
        assert "front-matter-guide.md" in filtered
        # A second reindex of an unchanged tree writes no new vectors.
        again = client.reindex()
        assert again.embeddings_written == 0
        # Editing one page re-embeds only its new chunks.
        _write(
            root,
            "wiki/concepts/orchards.md",
            "---\ntitle: Orchards\ntype: concept\n---\n\n"
            "Apple orchards, pear trees, autumn harvest cider, and a new apiary.\n",
        )
        updated = client.reindex()
        assert updated.upserted == 1
        assert 0 < updated.embeddings_written <= updated.chunks
        # Fallback: no embedder means hybrid still returns full-text hits.
    with Client(settings, autoload=False) as plain:
        # Reuse the existing db; do not reindex (would drop embeddings? no, we just don't sync)
        text = plain.search("front matter", mode="hybrid")
        assert "front-matter-guide.md" in text


def test_rrf_prefers_shared_high_ranks() -> None:
    fused = rrf_fuse(["a", "b", "c"], ["c", "a", "d"], k=60)
    assert fused[0] == "a"
    assert "c" in fused and "d" in fused


