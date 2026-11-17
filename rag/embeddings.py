"""sentence-transformers embeddings (multilingual E5: French questions, English documents)."""

from __future__ import annotations

from functools import lru_cache

import numpy as np


class Embedder:
    def __init__(self, model_name: str, batch_size: int = 32):
        from sentence_transformers import SentenceTransformer  # heavy import, kept lazy

        self.model_name = model_name
        self.batch_size = batch_size
        self.model = SentenceTransformer(model_name, device="cpu")
        # E5 models expect "query: " / "passage: " prefixes.
        self._e5 = "e5" in model_name.lower()

    def _encode(self, texts: list[str]) -> np.ndarray:
        return self.model.encode(
            texts, batch_size=self.batch_size, normalize_embeddings=True, show_progress_bar=False
        ).astype(np.float32)

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return self._encode([f"passage: {t}" if self._e5 else t for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._encode([f"query: {text}" if self._e5 else text])[0]


@lru_cache(maxsize=2)
def get_embedder(model_name: str, batch_size: int = 32) -> Embedder:
    return Embedder(model_name, batch_size)
