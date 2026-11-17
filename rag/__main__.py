"""CLI:
  python -m rag sync                      # incremental index sync
  python -m rag search "brake caliper"    # quick check of both retrieval legs
"""

import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "sync":
        from rag.indexer import sync_index

        print(json.dumps(sync_index(), indent=2, default=str))
    elif cmd == "search":
        from rag import store
        from rag.config import RagSettings
        from rag.embeddings import get_embedder
        from rag.search import keyword_search, vector_search

        query = " ".join(sys.argv[2:])
        rag = RagSettings.from_env()
        conn = store.connect(rag.db_dsn)
        embedder = get_embedder(rag.embedding_model)
        for name, hits in (
            ("vector", vector_search(conn, embedder, query, k=5)),
            ("keyword", keyword_search(conn, query, k=5)),
        ):
            print(f"--- {name}")
            for h in hits:
                print(f"{h.score:.3f}  {h.doc_id:<22} p.{h.page_start}  {h.title[:90]}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
