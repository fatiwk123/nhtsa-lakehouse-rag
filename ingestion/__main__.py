"""CLI: python -m ingestion [recalls] [complaints]"""

import json
import logging
import sys

from ingestion.bronze import run_ingestion

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

if __name__ == "__main__":
    stats = run_ingestion(sources=sys.argv[1:] or None)
    print(json.dumps(stats, indent=2))
