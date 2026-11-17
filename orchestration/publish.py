"""Publish silver and gold tables built by dbt (DuckDB) to Iceberg.

Iceberg stays the system of record for every layer; downstream consumers
(RAG indexing, SQL agent) read Iceberg without holding a lock on the DuckDB file.
"""

from __future__ import annotations

import os

import duckdb
from pyiceberg.catalog import Catalog

from ingestion.catalog import ensure_namespace

LAYERS = ("silver", "gold")


def duckdb_path() -> str:
    return os.environ.get("DUCKDB_PATH", "/data/duckdb/lakehouse.duckdb")


def publish_layers(catalog: Catalog, layers: tuple[str, ...] = LAYERS) -> dict[str, int]:
    published: dict[str, int] = {}
    con = duckdb.connect(duckdb_path(), read_only=True)
    try:
        con.execute("SET TimeZone = 'UTC'")
        for layer in layers:
            ensure_namespace(catalog, layer)
            tables = [
                row[0]
                for row in con.execute(
                    "select table_name from information_schema.tables "
                    "where table_schema = ? and table_type = 'BASE TABLE'",
                    [layer],
                ).fetchall()
            ]
            for name in tables:
                data = con.execute(f'select * from "{layer}"."{name}"').fetch_arrow_table()
                table = catalog.create_table_if_not_exists(f"{layer}.{name}", schema=data.schema)
                with table.update_schema() as update:
                    update.union_by_name(data.schema)
                table.overwrite(data)
                published[f"{layer}.{name}"] = data.num_rows
    finally:
        con.close()
    return published
