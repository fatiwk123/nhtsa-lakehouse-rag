#!/bin/bash
# Runs once, on first initialisation of the Postgres volume.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE ${DAGSTER_DB:-dagster};
    CREATE DATABASE ${RAG_DB:-rag};
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "${RAG_DB:-rag}" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS vector;
EOSQL
