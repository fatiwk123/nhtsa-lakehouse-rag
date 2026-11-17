"""Recall PDFs (NHTSA "associated documents") -> MinIO + Iceberg bronze metadata.

For every recall campaign present in gold, the NHTSA safetyIssues endpoint lists
the documents filed for it. We keep the technical ones:
  - RCLRPT: Part 573 Safety Recall Report (defect, chronology, remedy)
  - RCMN:   Manufacturer notices to dealers (technical bulletins)

Incremental: a document already in bronze (same NHTSA document id) is never
downloaded again.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pyarrow as pa
import requests
from pyarrow import fs as pafs
from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import DateType, IntegerType, NestedField, StringType, TimestamptzType

from ingestion.bronze import NAMESPACE, IngestionContext
from ingestion.catalog import ensure_namespace, get_catalog, get_object_store
from ingestion.config import Settings
from ingestion.nhtsa_client import NhtsaClient

log = logging.getLogger(__name__)

TABLE = f"{NAMESPACE}.nhtsa_recall_documents"

DOC_TYPE_LABELS = {
    "RCLRPT": "Part 573 Safety Recall Report",
    "RCMN": "Manufacturer notice to dealers",
    "RCONL": "Owner notification letter",
    "RCLQRT": "Recall quarterly report",
}

SCHEMA = Schema(
    NestedField(1, "document_id", StringType(), required=False),
    NestedField(2, "campaign_number", StringType(), required=False),
    NestedField(3, "doc_type", StringType(), required=False),
    NestedField(4, "file_name", StringType(), required=False),
    NestedField(5, "summary", StringType(), required=False),
    NestedField(6, "source_url", StringType(), required=False),
    NestedField(7, "load_date", StringType(), required=False),
    NestedField(8, "file_size", IntegerType(), required=False),
    NestedField(9, "sha256", StringType(), required=False),
    NestedField(10, "raw_file", StringType(), required=False),
    NestedField(11, "ingestion_id", StringType(), required=False),
    NestedField(12, "ingested_at", TimestamptzType(), required=False),
    NestedField(13, "ingest_date", DateType(), required=False),
)
PARTITION_SPEC = PartitionSpec(
    PartitionField(source_id=13, field_id=1000, transform=IdentityTransform(), name="ingest_date")
)
ARROW_SCHEMA = pa.schema(
    [
        ("document_id", pa.string()),
        ("campaign_number", pa.string()),
        ("doc_type", pa.string()),
        ("file_name", pa.string()),
        ("summary", pa.string()),
        ("source_url", pa.string()),
        ("load_date", pa.string()),
        ("file_size", pa.int32()),
        ("sha256", pa.string()),
        ("raw_file", pa.string()),
        ("ingestion_id", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
        ("ingest_date", pa.date32()),
    ]
)


def doc_type(file_name: str) -> str:
    return file_name.split("-", 1)[0].upper()


# Amended Part 573 reports are near-duplicates of each other: keep only the latest one.
SINGLE_VERSION_TYPES = {"RCLRPT"}


def select_documents(documents: list[dict[str, Any]], types: list[str], max_docs: int) -> list[dict[str, Any]]:
    """Keep PDFs of the wanted types (type priority, newest first), one version of
    single-version types, capped at max_docs."""
    priority = {t: i for i, t in enumerate(types)}
    candidates = [
        d
        for d in documents
        if d.get("url") and d.get("fileName") and doc_type(d["fileName"]) in priority
        and (d.get("mimeType") or "application/pdf") == "application/pdf"
    ]
    candidates.sort(key=lambda d: d.get("loadDate") or "", reverse=True)
    candidates.sort(key=lambda d: priority[doc_type(d["fileName"])])  # stable: newest first within a type
    kept, seen_single = [], set()
    for d in candidates:
        t = doc_type(d["fileName"])
        if t in SINGLE_VERSION_TYPES:
            if t in seen_single:
                continue
            seen_single.add(t)
        kept.append(d)
    return kept[:max_docs]


def campaign_numbers(catalog: Catalog) -> list[str]:
    try:
        table = catalog.load_table("gold.fct_recalls")
    except NoSuchTableError:
        return []
    values = table.scan(selected_fields=("campaign_number",)).to_arrow().column("campaign_number").to_pylist()
    return sorted({v for v in values if v})


def ensure_table(catalog: Catalog) -> Table:
    ensure_namespace(catalog, NAMESPACE)
    return catalog.create_table_if_not_exists(TABLE, schema=SCHEMA, partition_spec=PARTITION_SPEC)


def known_document_ids(table: Table) -> set[str]:
    if table.current_snapshot() is None:
        return set()
    return set(table.scan(selected_fields=("document_id",)).to_arrow().column("document_id").to_pylist())


def recall_documents(client: NhtsaClient, campaign: str) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Return (url, raw payload, associated documents) for one campaign (e.g. 18V369000)."""
    url, payload = client.get("safetyIssues/byNhtsaId", {"filter": "nhtsaId", "nhtsaId": campaign[:6]})
    docs: list[dict[str, Any]] = []
    for result in payload.get("results") or []:
        for recall in result.get("recalls") or []:
            if recall.get("nhtsaCampaignNumber") == campaign:
                docs.extend(recall.get("associatedDocuments") or [])
    return url, payload, docs


def run_document_ingestion(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or Settings.from_env()
    catalog = get_catalog(settings)
    store: pafs.S3FileSystem = get_object_store(settings)
    client = NhtsaClient(settings.nhtsa_base_url, timeout=60)
    ctx = IngestionContext()
    table = ensure_table(catalog)
    known = known_document_ids(table)
    campaigns = campaign_numbers(catalog)

    def run(campaign: str) -> tuple[list[dict[str, Any]], int, list[str]]:
        url, payload, docs = recall_documents(client, campaign)
        raw_path = f"{settings.bucket}/raw/nhtsa/safety_issues/ingest_date={ctx.ingest_date.isoformat()}/{campaign}.json"
        with store.open_output_stream(raw_path) as f:
            f.write(json.dumps({"request_url": url, "response": payload}, ensure_ascii=False).encode("utf-8"))

        rows, skipped, failed = [], 0, []
        for doc in select_documents(docs, settings.doc_types, settings.max_docs_per_campaign):
            document_id = str(doc["id"])
            if document_id in known:
                skipped += 1
                continue
            # One unavailable file (e.g. 403 on static.nhtsa.gov) must not fail the whole run:
            # it is reported and retried on the next run.
            try:
                content = client.download(doc["url"])
            except requests.RequestException as exc:
                log.warning("[documents] %s: download failed (%s)", doc["url"], exc)
                failed.append(doc["url"])
                continue
            path = f"{settings.bucket}/raw/nhtsa/documents/{campaign}/{doc['fileName']}"
            with store.open_output_stream(path) as f:
                f.write(content)
            rows.append(
                {
                    "document_id": document_id,
                    "campaign_number": campaign,
                    "doc_type": doc_type(doc["fileName"]),
                    "file_name": doc["fileName"],
                    "summary": (doc.get("summary") or "").strip(),
                    "source_url": doc["url"],
                    "load_date": doc.get("loadDate"),
                    "file_size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "raw_file": f"s3://{path}",
                    "ingestion_id": ctx.ingestion_id,
                    "ingested_at": ctx.ingested_at,
                    "ingest_date": ctx.ingest_date,
                }
            )
        return rows, skipped, failed

    rows: list[dict[str, Any]] = []
    skipped = 0
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=settings.max_workers) as pool:
        for batch, n_skipped, n_failed in pool.map(run, campaigns):
            rows.extend(batch)
            skipped += n_skipped
            failed.extend(n_failed)

    if rows:
        table.append(pa.Table.from_pylist(rows, schema=ARROW_SCHEMA))

    stats = {
        "table": TABLE,
        "campaigns": len(campaigns),
        "documents_downloaded": len(rows),
        "documents_already_known": skipped,
        "bytes_downloaded": sum(r["file_size"] for r in rows),
        "documents_failed": len(failed),
        "failed_urls": failed[:20],
    }
    log.info("[documents] %s", stats)
    return stats
