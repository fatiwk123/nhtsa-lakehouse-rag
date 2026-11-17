"""NHTSA API -> MinIO (raw JSON) -> Iceberg bronze tables.

Bronze keeps every record exactly as returned by the API (``payload`` column),
plus lineage columns (ingestion id, timestamp, request URL, raw file path).

Incremental: each row carries a ``row_hash`` of (queried vehicle, payload).
Rows whose hash is already in the table are skipped, so a daily run only
appends new or modified records. Cleaning and deduplication by business key
happen later, in silver.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import pyarrow as pa
from pyarrow import fs as pafs
from pyiceberg.catalog import Catalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import DateType, IntegerType, NestedField, StringType, TimestamptzType

from ingestion.catalog import ensure_namespace, get_catalog, get_object_store
from ingestion.config import Settings
from ingestion.nhtsa_client import SOURCES, NhtsaClient, SourceSpec

log = logging.getLogger(__name__)

NAMESPACE = "bronze"

ICEBERG_SCHEMA = Schema(
    NestedField(1, "ingestion_id", StringType(), required=False),
    NestedField(2, "ingested_at", TimestamptzType(), required=False),
    NestedField(3, "ingest_date", DateType(), required=False),
    NestedField(4, "source", StringType(), required=False),
    NestedField(5, "make", StringType(), required=False),
    NestedField(6, "model", StringType(), required=False),
    NestedField(7, "model_year", IntegerType(), required=False),
    NestedField(8, "record_id", StringType(), required=False),
    NestedField(9, "row_hash", StringType(), required=False),
    NestedField(10, "payload", StringType(), required=False),
    NestedField(11, "request_url", StringType(), required=False),
    NestedField(12, "raw_file", StringType(), required=False),
)

PARTITION_SPEC = PartitionSpec(
    PartitionField(source_id=3, field_id=1000, transform=IdentityTransform(), name="ingest_date")
)

ARROW_SCHEMA = pa.schema(
    [
        ("ingestion_id", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
        ("ingest_date", pa.date32()),
        ("source", pa.string()),
        ("make", pa.string()),
        ("model", pa.string()),
        ("model_year", pa.int32()),
        ("record_id", pa.string()),
        ("row_hash", pa.string()),
        ("payload", pa.string()),
        ("request_url", pa.string()),
        ("raw_file", pa.string()),
    ]
)


@dataclass
class IngestionContext:
    ingestion_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ingested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ingest_date(self) -> date:
        return self.ingested_at.date()


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


def row_hash(make: str, model: str, model_year: int, record: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"make": make, "model": model, "model_year": model_year, "payload": record},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_rows(
    source: SourceSpec,
    ctx: IngestionContext,
    make: str,
    model: str,
    model_year: int,
    request_url: str,
    raw_file: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for record in payload.get("results") or []:
        rows.append(
            {
                "ingestion_id": ctx.ingestion_id,
                "ingested_at": ctx.ingested_at,
                "ingest_date": ctx.ingest_date,
                "source": source.name,
                "make": make,
                "model": model,
                "model_year": model_year,
                "record_id": str(record.get(source.id_field) or ""),
                "row_hash": row_hash(make, model, model_year, record),
                "payload": json.dumps(record, sort_keys=True, ensure_ascii=False),
                "request_url": request_url,
                "raw_file": raw_file,
            }
        )
    return rows


def ensure_table(catalog: Catalog, source: SourceSpec) -> Table:
    ensure_namespace(catalog, NAMESPACE)
    return catalog.create_table_if_not_exists(
        f"{NAMESPACE}.nhtsa_{source.name}",
        schema=ICEBERG_SCHEMA,
        partition_spec=PARTITION_SPEC,
    )


def existing_hashes(table: Table) -> set[str]:
    if table.current_snapshot() is None:
        return set()
    hashes = table.scan(selected_fields=("row_hash",)).to_arrow().column("row_hash")
    return set(hashes.to_pylist())


def filter_new(rows: list[dict[str, Any]], known: set[str]) -> list[dict[str, Any]]:
    """Drop rows already in the table, and duplicates inside the batch."""
    seen = set(known)
    fresh = []
    for row in rows:
        if row["row_hash"] in seen:
            continue
        seen.add(row["row_hash"])
        fresh.append(row)
    return fresh


def _land_raw(store: pafs.S3FileSystem, path: str, request_url: str, payload: dict[str, Any], ctx: IngestionContext) -> None:
    body = json.dumps(
        {
            "ingestion_id": ctx.ingestion_id,
            "ingested_at": ctx.ingested_at.isoformat(),
            "request_url": request_url,
            "response": payload,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    with store.open_output_stream(path) as f:
        f.write(body)


def ingest_source(
    source_name: str,
    settings: Settings,
    catalog: Catalog,
    store: pafs.S3FileSystem,
    client: NhtsaClient,
    ctx: IngestionContext,
) -> dict[str, Any]:
    source = SOURCES[source_name]
    table = ensure_table(catalog, source)

    tasks: list[tuple[str, str, int]] = []
    for make in settings.makes:
        for year in settings.model_years:
            models = client.list_models(make, year, source.issue_type)
            if settings.models:
                models = [m for m in models if m in settings.models]
            tasks.extend((make, model, year) for model in models)

    def run(task: tuple[str, str, int]) -> list[dict[str, Any]]:
        make, model, year = task
        url, payload = client.fetch(source, make, model, year)
        path = (
            f"{settings.bucket}/raw/nhtsa/{source.name}/ingest_date={ctx.ingest_date.isoformat()}"
            f"/make={slug(make)}/{slug(model)}_{year}_{ctx.ingestion_id}.json"
        )
        _land_raw(store, path, url, payload, ctx)
        return build_rows(source, ctx, make, model, year, url, f"s3://{path}", payload)

    log.info("[%s] %d API calls (makes=%s, years=%s)", source.name, len(tasks), settings.makes, settings.model_years)
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=settings.max_workers) as pool:
        for batch in pool.map(run, tasks):
            rows.extend(batch)

    fresh = filter_new(rows, existing_hashes(table))
    if fresh:
        table.append(pa.Table.from_pylist(fresh, schema=ARROW_SCHEMA))

    stats = {
        "source": source.name,
        "table": f"{NAMESPACE}.nhtsa_{source.name}",
        "ingestion_id": ctx.ingestion_id,
        "api_calls": len(tasks),
        "records_fetched": len(rows),
        "records_appended": len(fresh),
        "records_skipped": len(rows) - len(fresh),
    }
    log.info("[%s] %s", source.name, stats)
    return stats


def run_ingestion(settings: Settings | None = None, sources: list[str] | None = None) -> dict[str, dict[str, Any]]:
    settings = settings or Settings.from_env()
    catalog = get_catalog(settings)
    store = get_object_store(settings)
    client = NhtsaClient(settings.nhtsa_base_url)
    ctx = IngestionContext()
    return {
        name: ingest_source(name, settings, catalog, store, client, ctx)
        for name in (sources or list(SOURCES))
    }
