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


@dataclass(frozen=True)
class AssistantSettings:
    """Retrieval + generation settings (not part of index_version: changing them needs no re-embedding)."""

    reranker_model: str
    candidates: int  # chunks per retrieval leg before fusion / reranking
    top_k: int  # passages handed to the LLM
    strategy: str  # chunking strategy searched by the assistant ("fixed" won the phase 5 eval)
    min_rerank_score: float  # below this (cross-encoder logit) the best passage is "not relevant"
    ollama_url: str
    llm_model: str
    llm_timeout_s: int
    max_agent_steps: int
    duckdb_path: str
    sql_max_rows: int
    sql_timeout_s: float

    @classmethod
    def from_env(cls) -> "AssistantSettings":
        env = os.environ.get
        return cls(
            reranker_model=env("RERANKER_MODEL", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"),
            candidates=int(env("RAG_CANDIDATES", "30")),
            top_k=int(env("RAG_TOP_K", "5")),
            strategy=env("RAG_STRATEGY", "fixed"),
            min_rerank_score=float(env("RAG_MIN_RERANK_SCORE", "0.0")),
            ollama_url=env("OLLAMA_URL", "http://localhost:11434"),
            llm_model=env("OLLAMA_MODEL", "qwen3:4b-instruct"),
            llm_timeout_s=int(env("OLLAMA_TIMEOUT_S", "300")),
            max_agent_steps=int(env("AGENT_MAX_STEPS", "4")),
            duckdb_path=env("DUCKDB_PATH", "/data/duckdb/lakehouse.duckdb"),
            sql_max_rows=int(env("SQL_MAX_ROWS", "50")),
            sql_timeout_s=float(env("SQL_TIMEOUT_S", "10")),
        )
