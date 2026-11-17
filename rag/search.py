"""Retrieval primitives over the index: dense (pgvector) and keyword (tsvector).

Hybrid fusion (RRF) and reranking come in phase 4 on top of these two functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rag.embeddings import Embedder


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    title: str
    content: str
    page_start: int | None
    metadata: dict[str, Any]
    score: float


_COLUMNS = "chunk_id, doc_id, title, content, page_start, metadata"


def _hits(rows) -> list[Hit]:
    return [Hit(*row) for row in rows]


def vector_search(conn, embedder: Embedder, query: str, k: int = 10, strategy: str = "structure") -> list[Hit]:
    q = embedder.embed_query(query)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_COLUMNS}, 1 - (embedding <=> %s) AS score
            FROM rag_chunks WHERE strategy = %s
            ORDER BY embedding <=> %s LIMIT %s
            """,
            (q, strategy, q, k),
        )
        return _hits(cur.fetchall())


def keyword_search(conn, query: str, k: int = 10, strategy: str = "structure") -> list[Hit]:
    """Full-text ranking (ts_rank_cd, length-normalised): the BM25-like leg of hybrid search.
    Uses OR semantics so a long natural-language question still matches."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (
                SELECT to_tsquery('english', string_agg(lexeme, ' | ')) AS query
                FROM unnest(to_tsvector('english', %s))
            )
            SELECT {_COLUMNS}, ts_rank_cd(tsv, q.query, 32) AS score
            FROM rag_chunks, q
            WHERE strategy = %s AND tsv @@ q.query
            ORDER BY score DESC LIMIT %s
            """,
            (query, strategy, k),
        )
        return _hits(cur.fetchall())
