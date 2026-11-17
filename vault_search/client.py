"""Public read-only API the MCP layer wraps."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from vault_search import query as q
from vault_search.config import Settings
from vault_search.index import Index, ReindexStats


def _try_load_embedder(settings: Settings):
    from vault_search.embed import load_embedder

    return load_embedder(
        model_name=settings.embedding_model,
        cache_dir=settings.embedding_cache_dir,
        model_path=settings.embedding_model_path,
    )


class Client:
    """Search and read a local vault index.

    All tool methods return compact markdown with secrets already masked.
    `read_note` returns `not_found` only when the path is missing.
    Search defaults to `hybrid` when embeddings exist, otherwise full-text.
    """

    def __init__(self, settings: Settings, embedder: object | None = None, *, autoload: bool = True):
        self.settings = settings
        self._index = Index(settings)
        if embedder is not None:
            self._embedder = embedder
        elif autoload:
            self._embedder = _try_load_embedder(settings)
        else:
            self._embedder = None

    @classmethod
    def from_env(
        cls,
        *,
        vault_root: Path | None = None,
        index_path: Path | None = None,
        include_raw: bool | None = None,
        include_examples: bool | None = None,
        embedder: object | None = None,
    ) -> Client:
        return cls(
            Settings.from_env(
                vault_root=vault_root,
                index_path=index_path,
                include_raw=include_raw,
                include_examples=include_examples,
            ),
            embedder=embedder,
        )

    @property
    def embedder(self) -> object | None:
        return self._embedder

    def close(self) -> None:
        self._index.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def reindex(self, *, force: bool = False) -> ReindexStats:
        return self._index.reindex(force=force, embedder=self._embedder)

    def search(
        self,
        query: str,
        *,
        mode: str = "hybrid",
        project: str | None = None,
        tags: Sequence[str] | None = None,
        type: str | None = None,
        limit: int = q.DEFAULT_SEARCH_LIMIT,
        after: str | None = None,
    ) -> str:
        return q.search(
            self._index.conn,
            query,
            mode=mode,
            project=project,
            tags=list(tags) if tags else None,
            type=type,
            limit=limit,
            after=after,
            embedder=self._embedder,
        )

    def read_note(self, path: str, section: str | None = None) -> str:
        return q.read_note(self._index.conn, path, section)

    def list_notes(
        self,
        *,
        project: str | None = None,
        tags: Sequence[str] | None = None,
        type: str | None = None,
        limit: int = q.DEFAULT_LIST_LIMIT,
        after: str | None = None,
    ) -> str:
        return q.list_notes(
            self._index.conn,
            project=project,
            tags=list(tags) if tags else None,
            type=type,
            limit=limit,
            after=after,
        )

    def list_projects(self) -> str:
        return q.list_projects(self._index.conn)
