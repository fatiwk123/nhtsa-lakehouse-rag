"""Wiring of the assistant: models loaded once, one DB connection per request (thread-safe for the API)."""

from __future__ import annotations

from contextlib import contextmanager
from functools import cached_property

from rag import store
from rag.agent import Agent, Answer
from rag.config import AssistantSettings, RagSettings
from rag.embeddings import get_embedder
from rag.llm import OllamaChat
from rag.retrieval import RetrievalResult, get_reranker, retrieve
from rag.sql_tool import SqlResult, run_sql
from rag.tracing import Tracer


class Assistant:
    def __init__(self, rag: RagSettings | None = None, settings: AssistantSettings | None = None):
        self.rag = rag or RagSettings.from_env()
        self.settings = settings or AssistantSettings.from_env()
        self.tracer = Tracer()

    @cached_property
    def embedder(self):
        return get_embedder(self.rag.embedding_model)

    @cached_property
    def reranker(self):
        return get_reranker(self.settings.reranker_model)

    @cached_property
    def llm(self) -> OllamaChat:
        s = self.settings
        return OllamaChat(s.ollama_url, s.llm_model, timeout_s=s.llm_timeout_s)

    def warm_up(self) -> None:
        self.embedder, self.reranker  # noqa: B018 - load the models before the first request

    @contextmanager
    def _conn(self):
        conn = store.connect(self.rag.db_dsn)
        try:
            yield conn
        finally:
            conn.close()

    def search(self, query: str, mode: str = "hybrid_rerank", k: int | None = None, strategy: str | None = None) -> RetrievalResult:
        s = self.settings
        with self._conn() as conn:
            return retrieve(
                conn,
                self.embedder,
                query,
                mode=mode,
                k=k or s.top_k,
                candidates=s.candidates,
                reranker=self.reranker if mode == "hybrid_rerank" else None,
                min_rerank_score=s.min_rerank_score,
                strategy=strategy or s.strategy,
            )

    def sql(self, query: str) -> SqlResult:
        s = self.settings
        return run_sql(query, s.duckdb_path, max_rows=s.sql_max_rows, timeout_s=s.sql_timeout_s)

    def ask(self, question: str, mode: str = "hybrid_rerank") -> Answer:
        agent = Agent(self.llm, lambda q: self.search(q, mode=mode), self.sql, max_steps=self.settings.max_agent_steps)
        trace = self.tracer.start(question, mode=mode, model=self.settings.llm_model)
        return agent.ask(question, trace=trace)
