"""RAG settings (env driven)."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RagSettings:
    db_dsn: str
    embedding_model: str
    embedding_dim: int
    batch_size: int
    # Strategy "fixed": sliding window over words.
    fixed_window_words: int
    fixed_overlap_words: int
    # Strategy "structure": paragraph/sentence packing within section boundaries.
    structure_max_words: int

    @property
    def index_version(self) -> str:
        """Changes whenever the embedding model or a chunking parameter changes,
        which forces a full re-embedding of every document."""
        params = {k: v for k, v in asdict(self).items() if k not in {"db_dsn", "batch_size"}}
        return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]

    @classmethod
    def from_env(cls) -> "RagSettings":
        env = os.environ.get
        dsn = (
            f"host={env('POSTGRES_HOST', 'localhost')} port={env('POSTGRES_PORT', '5432')} "
            f"dbname={env('RAG_DB', 'rag')} user={env('POSTGRES_USER', 'lakehouse')} "
            f"password={env('POSTGRES_PASSWORD', 'lakehouse')}"
        )
        return cls(
            db_dsn=dsn,
            embedding_model=env("EMBEDDING_MODEL", "intfloat/multilingual-e5-small"),
            embedding_dim=int(env("EMBEDDING_DIM", "384")),
            batch_size=int(env("EMBEDDING_BATCH_SIZE", "32")),
            fixed_window_words=int(env("CHUNK_FIXED_WORDS", "180")),
            fixed_overlap_words=int(env("CHUNK_FIXED_OVERLAP", "40")),
            structure_max_words=int(env("CHUNK_STRUCTURE_MAX_WORDS", "220")),
        )
