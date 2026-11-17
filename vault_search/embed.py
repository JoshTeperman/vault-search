"""Optional local embeddings (fastembed/ONNX). Stdlib cosine; no vector DB."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class Embedder(Protocol):
    model_name: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class FastEmbedEmbedder:
    """Thin wrapper around fastembed.TextEmbedding. Import is delayed."""

    model_name: str
    dim: int
    _model: object

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = list(self._model.embed(list(texts)))  # type: ignore[attr-defined]
        return [_as_list(v) for v in vectors]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        query_embed = getattr(self._model, "query_embed", None)
        if query_embed is None:
            return self.embed_documents(texts)
        vectors = list(query_embed(list(texts)))
        return [_as_list(v) for v in vectors]


def _as_list(vector) -> list[float]:
    return [float(x) for x in vector]


def fastembed_available() -> bool:
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return True


def load_embedder(
    *,
    model_name: str = DEFAULT_MODEL,
    cache_dir: Path | None = None,
    model_path: Path | None = None,
) -> Embedder | None:
    """Return a local embedder, or None if the semantic extra is not installed."""
    try:
        from fastembed import TextEmbedding
    except ImportError:
        return None
    kwargs: dict = {"model_name": model_name}
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        kwargs["cache_dir"] = str(cache_dir)
    if model_path is not None:
        kwargs["specific_model_path"] = str(model_path)
    model = TextEmbedding(**kwargs)
    dim = 384
    try:
        listed = TextEmbedding.list_supported_models()
        for item in listed:
            name = item.get("model") or item.get("model_name") if isinstance(item, dict) else None
            if name == model_name and isinstance(item, dict) and item.get("dim"):
                dim = int(item["dim"])
                break
    except Exception:
        pass
    return FastEmbedEmbedder(model_name=model_name, dim=dim, _model=model)


def pack_vector(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *[float(v) for v in values])


def unpack_vector(blob: bytes, dim: int) -> list[float]:
    expected = dim * 4
    if len(blob) != expected:
        # Tolerate a dim stored on the row that doesn't match the blob.
        dim = len(blob) // 4
    return list(struct.unpack(f"<{dim}f", blob[: dim * 4]))


def l2_normalize(values: Sequence[float]) -> list[float]:
    total = sum(v * v for v in values) ** 0.5
    if total <= 0:
        return [0.0] * len(values)
    return [v / total for v in values]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def rrf_fuse(*ranked: list[str], k: int = 60) -> list[str]:
    """Reciprocal Rank Fusion. Each list is a sequence of path ids, best first."""
    scores: dict[str, float] = {}
    for paths in ranked:
        seen: set[str] = set()
        for rank, path in enumerate(paths, start=1):
            if path in seen:
                continue
            seen.add(path)
            scores[path] = scores.get(path, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda p: (-scores[p], p))
