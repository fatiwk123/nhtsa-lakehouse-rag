FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DAGSTER_HOME=/opt/dagster/home \
    DUCKDB_PATH=/data/duckdb/lakehouse.duckdb \
    HF_HOME=/opt/hf

WORKDIR /app

# CPU-only PyTorch (avoids pulling ~2 GB of CUDA wheels).
RUN pip install --no-cache-dir torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image: no download at runtime, reproducible runs.
ARG EMBEDDING_MODEL=intfloat/multilingual-e5-small
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"
ENV HF_HUB_OFFLINE=1

COPY dagster_home/ /opt/dagster/home/
COPY dbt/ ./dbt/
COPY quality/ ./quality/
COPY ingestion/ ./ingestion/
COPY orchestration/ ./orchestration/
COPY rag/ ./rag/

# Build the dbt manifest at image build time (needed by dagster-dbt; no DB connection).
RUN cd dbt && dbt parse --profiles-dir . --quiet && mkdir -p /data/duckdb
