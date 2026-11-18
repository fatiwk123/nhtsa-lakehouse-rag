"""Retrieval pipeline: vector | hybrid (RRF) | hybrid + cross-encoder rerank.

The three modes are exposed so phase 5 can compare them on the same eval set.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache

from rag.embeddings import Embedder
from rag.search import Hit, keyword_search, vector_search

MODES = ("vector", "hybrid", "hybrid_rerank")
RRF_K = 60  # standard constant from Cormack et al. (2009)


def rrf_fuse(rankings: list[list[Hit]], k: int = RRF_K) -> list[Hit]:
    """Reciprocal Rank Fusion: score(d) = sum over rankings of 1 / (k + rank).
    Rank-based, so the incomparable scales of cosine and ts_rank do not matter."""
    scores: dict[str, float] = {}
    hits: dict[str, Hit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
            hits.setdefault(hit.chunk_id, hit)
    ordered = sorted(scores, key=scores.get, reverse=True)
    return [replace(hits[c], score=scores[c]) for c in ordered]


class Reranker:
    def __init__(self, model_name: str):
        from sentence_transformers import CrossEncoder  # heavy import, kept lazy

        self.model_name = model_name
        self.model = CrossEncoder(model_name, device="cpu", max_length=512)

    def rerank(self, query: str, hits: list[Hit]) -> list[Hit]:
        if not hits:
            return []
        scores = self.model.predict([(query, f"{h.title}\n{h.content}") for h in hits], show_progress_bar=False)
        ranked = sorted(zip(hits, scores), key=lambda p: float(p[1]), reverse=True)
        return [replace(h, score=float(s)) for h, s in ranked]


@lru_cache(maxsize=2)
def get_reranker(model_name: str) -> Reranker:
    return Reranker(model_name)


@dataclass
class RetrievalResult:
    hits: list[Hit]
    mode: str
    relevant: bool  # False -> the assistant must answer "je ne sais pas"


def retrieve(
    conn,
    embedder: Embedder,
    query: str,
    mode: str = "hybrid_rerank",
    k: int = 5,
    candidates: int = 30,
    reranker: Reranker | None = None,
    min_rerank_score: float | None = None,
    strategy: str = "structure",
) -> RetrievalResult:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    dense = vector_search(conn, embedder, query, k=candidates if mode != "vector" else k, strategy=strategy)
    if mode == "vector":
        return RetrievalResult(dense, mode, relevant=bool(dense))
    fused = rrf_fuse([dense, keyword_search(conn, query, k=candidates, strategy=strategy)])
    if mode == "hybrid":
        return RetrievalResult(fused[:k], mode, relevant=bool(fused))
    if reranker is None:
        raise ValueError("hybrid_rerank needs a reranker")
    ranked = reranker.rerank(query, fused[:candidates])
    relevant = bool(ranked) and (min_rerank_score is None or ranked[0].score >= min_rerank_score)
    return RetrievalResult(ranked[:k], mode, relevant)
