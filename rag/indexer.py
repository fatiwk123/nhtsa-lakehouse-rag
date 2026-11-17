"""Incremental index sync: lakehouse documents -> chunks -> embeddings -> pgvector.

A document is (re)embedded only when its content hash or the index version
(embedding model / chunking parameters) changed. Documents that left the
lakehouse are removed from the index. Each document is committed atomically.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ingestion.catalog import get_catalog, get_object_store
from ingestion.config import Settings
from rag import store
from rag.chunking import strategies
from rag.config import RagSettings
from rag.documents import Document, build_documents
from rag.embeddings import get_embedder

log = logging.getLogger(__name__)


def plan_sync(
    documents: list[Document], indexed: dict[str, tuple[str, str]], index_version: str
) -> tuple[list[Document], list[str], int]:
    """Return (documents to (re)index, doc_ids to delete, number unchanged)."""
    current = {d.doc_id for d in documents}
    to_index = [d for d in documents if indexed.get(d.doc_id) != (d.content_hash, index_version)]
    to_delete = sorted(set(indexed) - current)
    return to_index, to_delete, len(documents) - len(to_index)


def sync_index(settings: Settings | None = None, rag: RagSettings | None = None) -> dict[str, Any]:
    settings = settings or Settings.from_env()
    rag = rag or RagSettings.from_env()
    started = time.monotonic()

    documents, build_stats = build_documents(get_catalog(settings), get_object_store(settings))
    conn = store.connect(rag.db_dsn)
    try:
        store.ensure_schema(conn, rag.embedding_dim)
        to_index, to_delete, unchanged = plan_sync(documents, store.indexed_state(conn), rag.index_version)

        store.delete_documents(conn, to_delete)
        conn.commit()

        chunkers = strategies(rag.fixed_window_words, rag.fixed_overlap_words, rag.structure_max_words)
        embedder = get_embedder(rag.embedding_model, rag.batch_size) if to_index else None
        n_chunks = 0
        for doc in to_index:
            chunks = []
            for name, chunker in chunkers.items():
                for c in chunker(doc):
                    chunks.append(
                        {
                            "chunk_id": c.chunk_id,
                            "strategy": name,
                            "index": c.index,
                            "text": c.text,
                            "page_start": c.page_start,
                            "page_end": c.page_end,
                            "metadata": {**doc.metadata, "heading": c.heading},
                        }
                    )
            # Contextual header: the document title is embedded with every chunk.
            embeddings = embedder.embed_passages([f"{doc.title}\n{c['text']}" for c in chunks])
            store.replace_document(
                conn,
                {
                    "doc_id": doc.doc_id,
                    "source_type": doc.source_type,
                    "title": doc.title,
                    "content_hash": doc.content_hash,
                    "index_version": rag.index_version,
                    "metadata": doc.metadata,
                },
                chunks,
                embeddings,
            )
            conn.commit()
            n_chunks += len(chunks)

        stats = {
            **build_stats,
            "index_version": rag.index_version,
            "embedding_model": rag.embedding_model,
            "documents_indexed": len(to_index),
            "documents_unchanged": unchanged,
            "documents_deleted": len(to_delete),
            "chunks_embedded": n_chunks,
            "index_totals": store.counts(conn),
            "duration_s": round(time.monotonic() - started, 1),
        }
    finally:
        conn.close()
    log.info("[rag] %s", stats)
    return stats
