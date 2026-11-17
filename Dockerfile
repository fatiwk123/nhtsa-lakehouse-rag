FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DAGSTER_HOME=/opt/dagster/home \
    DUCKDB_PATH=/data/duckdb/lakehouse.duckdb

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dagster_home/ /opt/dagster/home/
COPY dbt/ ./dbt/
COPY quality/ ./quality/
COPY ingestion/ ./ingestion/
COPY orchestration/ ./orchestration/

# Build the dbt manifest at image build time (needed by dagster-dbt; no DB connection).
RUN cd dbt && dbt parse --profiles-dir . --quiet && mkdir -p /data/duckdb
