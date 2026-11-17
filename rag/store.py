"""pgvector + Postgres full-text store.

rag_documents: one row per source document, with the content hash used for incremental sync.
rag_chunks:    chunks of every strategy, with an embedding (pgvector, HNSW) and a tsvector
               (GIN) for keyword / BM25-style ranking: hybrid search with a single database.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import psycopg2
from pgvector.psycopg2 import register_vector
from psycopg2.extras import execute_values

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_documents (
    doc_id         text PRIMARY KEY,
    source_type    text NOT NULL,
    title          text NOT NULL,
    content_hash   text NOT NULL,
    index_version  text NOT NULL,
    metadata       jsonb NOT NULL DEFAULT '{{}}',
    indexed_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id    text PRIMARY KEY,
    doc_id      text NOT NULL REFERENCES rag_documents(doc_id) ON DELETE CASCADE,
    strategy    text NOT NULL,
    chunk_index int  NOT NULL,
    title       text NOT NULL,
    content     text NOT NULL,
    page_start  int,
    page_end    int,
    metadata    jsonb NOT NULL DEFAULT '{{}}',
    embedding   vector({dim}) NOT NULL,
    tsv         tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('english', title), 'A') ||
                    setweight(to_tsvector('english', content), 'B')
                ) STORED
);

CREATE INDEX IF NOT EXISTS rag_chunks_embedding_idx ON rag_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS rag_chunks_tsv_idx ON rag_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS rag_chunks_strategy_idx ON rag_chunks (strategy);
CREATE INDEX IF NOT EXISTS rag_chunks_doc_idx ON rag_chunks (doc_id);
"""


def connect(dsn: str):
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.commit()
    register_vector(conn)
    return conn


def ensure_schema(conn, dim: int) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL.format(dim=dim))
    conn.commit()


def indexed_state(conn) -> dict[str, tuple[str, str]]:
    """doc_id -> (content_hash, index_version)"""
    with conn.cursor() as cur:
        cur.execute("SELECT doc_id, content_hash, index_version FROM rag_documents")
        return {doc_id: (h, v) for doc_id, h, v in cur.fetchall()}


def delete_documents(conn, doc_ids: list[str]) -> None:
    if doc_ids:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rag_documents WHERE doc_id = ANY(%s)", (doc_ids,))


def replace_document(
    conn,
    doc: dict[str, Any],
    chunks: list[dict[str, Any]],
    embeddings: np.ndarray,
) -> None:
    """Upsert one document and replace all its chunks (caller commits)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO rag_documents (doc_id, source_type, title, content_hash, index_version, metadata, indexed_at)
            VALUES (%(doc_id)s, %(source_type)s, %(title)s, %(content_hash)s, %(index_version)s, %(metadata)s, now())
            ON CONFLICT (doc_id) DO UPDATE SET
                source_type = EXCLUDED.source_type, title = EXCLUDED.title,
                content_hash = EXCLUDED.content_hash, index_version = EXCLUDED.index_version,
                metadata = EXCLUDED.metadata, indexed_at = now()
            """,
            {**doc, "metadata": json.dumps(doc["metadata"], default=str)},
        )
        cur.execute("DELETE FROM rag_chunks WHERE doc_id = %s", (doc["doc_id"],))
        execute_values(
            cur,
            """
            INSERT INTO rag_chunks (chunk_id, doc_id, strategy, chunk_index, title, content,
                                    page_start, page_end, metadata, embedding)
            VALUES %s
            """,
            [
                (
                    c["chunk_id"], doc["doc_id"], c["strategy"], c["index"], doc["title"], c["text"],
                    c["page_start"], c["page_end"], json.dumps(c["metadata"], default=str), emb,
                )
                for c, emb in zip(chunks, embeddings)
            ],
        )


def counts(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT source_type, count(*) FROM rag_documents GROUP BY 1")
        docs = dict(cur.fetchall())
        cur.execute("SELECT strategy, count(*) FROM rag_chunks GROUP BY 1")
        chunks = dict(cur.fetchall())
    return {"documents": docs, "chunks": chunks}
