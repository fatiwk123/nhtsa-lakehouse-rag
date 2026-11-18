"""FastAPI: /search (retrieval only, 3 modes) and /ask (agent with citations).

Run: uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from rag import store
from rag.service import Assistant

assistant = Assistant()

Mode = Literal["vector", "hybrid", "hybrid_rerank"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    assistant.warm_up()
    yield


app = FastAPI(title="NHTSA lakehouse RAG", version="0.4.0", lifespan=lifespan)


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    mode: Mode = "hybrid_rerank"
    k: int = Field(5, ge=1, le=50)
    strategy: Literal["structure", "fixed"] = "structure"


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    mode: Mode = "hybrid_rerank"


@app.get("/health")
def health() -> dict:
    with assistant._conn() as conn:
        index = store.counts(conn)
    return {
        "status": "ok",
        "index": index,
        "llm": {"model": assistant.settings.llm_model, "available": assistant.llm.available()},
        "reranker": assistant.settings.reranker_model,
    }


@app.post("/search")
def search(req: SearchRequest) -> dict:
    result = assistant.search(req.query, mode=req.mode, k=req.k, strategy=req.strategy)
    return {"mode": result.mode, "relevant": result.relevant, "hits": [asdict(h) for h in result.hits]}


@app.post("/ask")
def ask(req: AskRequest) -> dict:
    if not assistant.llm.available():
        raise HTTPException(503, f"LLM {assistant.settings.llm_model} not available on Ollama (is ollama-pull done?)")
    return assistant.ask(req.question, mode=req.mode).to_dict()
