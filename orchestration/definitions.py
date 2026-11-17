"""Dagster definitions:
bronze ingestion -> dbt silver/gold -> Soda -> Iceberg publish -> recall PDFs -> RAG index (pgvector).

Runs daily; any failure (API, dbt test, Soda check, index sync check) fails the run and fires the alert sensor.
"""

import os
from pathlib import Path

import requests
from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    AssetExecutionContext,
    AssetSelection,
    DefaultScheduleStatus,
    DefaultSensorStatus,
    Definitions,
    Failure,
    MaterializeResult,
    MetadataValue,
    RunFailureSensorContext,
    ScheduleDefinition,
    asset,
    asset_check,
    define_asset_job,
    multiprocess_executor,
    run_failure_sensor,
)
from dagster_dbt import DbtCliResource, DbtProject, dbt_assets, get_asset_key_for_model

from ingestion.bronze import run_ingestion
from ingestion.catalog import get_catalog
from ingestion.config import Settings
from ingestion.documents import run_document_ingestion
from orchestration.publish import publish_layers
from orchestration.quality import run_soda_scan
from rag import store as rag_store
from rag.config import RagSettings
from rag.indexer import sync_index

DBT_DIR = Path(__file__).resolve().parents[1] / "dbt"
dbt_project = DbtProject(project_dir=DBT_DIR, profiles_dir=DBT_DIR)
dbt_project.prepare_if_dev()

# Steps calling the public NHTSA API run one at a time (politeness / rate limits).
NHTSA_API = {"nhtsa_api": "true"}
polite_executor = multiprocess_executor.configured(
    {"tag_concurrency_limits": [{"key": "nhtsa_api", "value": "true", "limit": 1}]}
)

GOLD_MODELS = ["fct_recalls", "fct_complaints", "dim_vehicle", "dim_component", "bridge_complaint_component"]


# --- Bronze -----------------------------------------------------------------

def _ingest(source: str) -> MaterializeResult:
    stats = run_ingestion(sources=[source])[source]
    return MaterializeResult(
        metadata={
            "table": MetadataValue.text(stats["table"]),
            "ingestion_id": MetadataValue.text(stats["ingestion_id"]),
            "api_calls": stats["api_calls"],
            "records_fetched": stats["records_fetched"],
            "records_appended": stats["records_appended"],
            "records_skipped": stats["records_skipped"],
        }
    )


@asset(group_name="bronze", kinds={"python", "iceberg"}, op_tags=NHTSA_API, description="Raw NHTSA recalls (append-only, hash-deduplicated).")
def bronze_nhtsa_recalls() -> MaterializeResult:
    return _ingest("recalls")


@asset(group_name="bronze", kinds={"python", "iceberg"}, op_tags=NHTSA_API, description="Raw NHTSA complaints (append-only, hash-deduplicated).")
def bronze_nhtsa_complaints() -> MaterializeResult:
    return _ingest("complaints")


# --- Silver / Gold (dbt) ----------------------------------------------------

@dbt_assets(manifest=dbt_project.manifest_path)
def nhtsa_dbt_models(context: AssetExecutionContext, dbt: DbtCliResource):
    # `build` = seeds + models + dbt tests; a failing test fails the run.
    yield from dbt.cli(["build"], context=context).stream()


# --- Quality (Soda) ---------------------------------------------------------

@asset(
    group_name="quality",
    kinds={"soda"},
    deps=[get_asset_key_for_model([nhtsa_dbt_models], m) for m in GOLD_MODELS],
    description="Soda checks on gold (nulls, uniqueness, freshness, ranges). Blocks publishing when failing.",
)
def soda_gold_checks(context: AssetExecutionContext) -> MaterializeResult:
    ok, results, logs = run_soda_scan()
    context.log.info(logs)
    checks = results.get("checks", [])
    failed = [c["name"] for c in checks if c.get("outcome") != "pass"]
    metadata = {
        "checks_total": len(checks),
        "checks_failed": len(failed),
        "failed_checks": MetadataValue.json(failed),
    }
    if not ok:
        raise Failure(description=f"Soda: {len(failed)} check(s) failed: {failed}", metadata=metadata)
    return MaterializeResult(metadata=metadata)


# --- Publish to Iceberg -----------------------------------------------------

@asset(
    group_name="publish",
    kinds={"iceberg"},
    deps=[soda_gold_checks],
    description="Silver and gold tables written to Iceberg (overwrite), only after quality checks pass.",
)
def iceberg_silver_gold() -> MaterializeResult:
    published = publish_layers(get_catalog(Settings.from_env()))
    return MaterializeResult(metadata={"tables": MetadataValue.json(published)})


# --- Recall PDFs + RAG index -------------------------------------------------

@asset(
    group_name="bronze",
    kinds={"python", "iceberg", "pdf"},
    op_tags=NHTSA_API,
    deps=[iceberg_silver_gold],
    description="Recall PDFs (Part 573 reports, dealer notices) for every gold campaign; new documents only.",
)
def bronze_recall_documents() -> MaterializeResult:
    stats = run_document_ingestion()
    return MaterializeResult(metadata={**stats, "failed_urls": MetadataValue.json(stats["failed_urls"])})


@asset(
    group_name="rag",
    kinds={"pgvector", "postgres"},
    deps=[iceberg_silver_gold, bronze_recall_documents],
    description="Chunks + embeddings (pgvector) + tsvector, synced incrementally from the lakehouse (content hash).",
)
def rag_index() -> MaterializeResult:
    stats = sync_index()
    return MaterializeResult(
        metadata={
            "documents_indexed": stats["documents_indexed"],
            "documents_unchanged": stats["documents_unchanged"],
            "documents_deleted": stats["documents_deleted"],
            "chunks_embedded": stats["chunks_embedded"],
            "pdf_without_text": stats["pdf_without_text"],
            "index_version": MetadataValue.text(stats["index_version"]),
            "totals": MetadataValue.json(stats["index_totals"]),
            "duration_s": stats["duration_s"],
        }
    )


@asset_check(asset=rag_index, blocking=True, description="Every gold recall campaign is in the index, with chunks for both strategies.")
def rag_index_in_sync() -> AssetCheckResult:
    catalog = get_catalog(Settings.from_env())
    campaigns = set(
        catalog.load_table("gold.fct_recalls").scan(selected_fields=("campaign_number",)).to_arrow()
        .column("campaign_number").to_pylist()
    )
    rag = RagSettings.from_env()
    conn = rag_store.connect(rag.db_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT metadata->>'campaign_number' FROM rag_documents WHERE source_type = 'recall'")
            indexed = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT count(*) FROM rag_documents d WHERE (SELECT count(DISTINCT strategy) FROM rag_chunks c WHERE c.doc_id = d.doc_id) < 2"
            )
            incomplete = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM rag_documents WHERE index_version <> %s", (rag.index_version,))
            stale = cur.fetchone()[0]
    finally:
        conn.close()
    missing, extra = sorted(campaigns - indexed), sorted(indexed - campaigns)
    return AssetCheckResult(
        passed=not (missing or extra or incomplete or stale),
        severity=AssetCheckSeverity.ERROR,
        metadata={
            "gold_campaigns": len(campaigns),
            "indexed_campaigns": len(indexed),
            "missing": MetadataValue.json(missing[:20]),
            "extra": MetadataValue.json(extra[:20]),
            "documents_missing_a_strategy": incomplete,
            "documents_with_stale_index_version": stale,
        },
    )


# --- Jobs, schedule, alerting -----------------------------------------------

daily_pipeline = define_asset_job("daily_pipeline", selection=AssetSelection.all())
bronze_job = define_asset_job("bronze_ingestion", selection=AssetSelection.groups("bronze"))

daily_schedule = ScheduleDefinition(
    job=daily_pipeline,
    cron_schedule="0 6 * * *",
    execution_timezone="Europe/Paris",
    default_status=DefaultScheduleStatus.RUNNING,
)


@run_failure_sensor(default_status=DefaultSensorStatus.RUNNING)
def pipeline_failure_alert(context: RunFailureSensorContext) -> None:
    message = (
        f"NHTSA pipeline failed - job `{context.dagster_run.job_name}`, "
        f"run {context.dagster_run.run_id}: {context.failure_event.message}"
    )
    context.log.error(message)
    webhook = os.environ.get("ALERT_WEBHOOK_URL")
    if webhook:  # Slack / Teams / Discord compatible incoming webhook
        try:
            requests.post(webhook, json={"text": message, "content": message}, timeout=10).raise_for_status()
        except requests.RequestException as exc:
            context.log.error(f"Alert webhook call failed: {exc}")


defs = Definitions(
    assets=[
        bronze_nhtsa_recalls,
        bronze_nhtsa_complaints,
        nhtsa_dbt_models,
        soda_gold_checks,
        iceberg_silver_gold,
        bronze_recall_documents,
        rag_index,
    ],
    asset_checks=[rag_index_in_sync],
    jobs=[daily_pipeline, bronze_job],
    schedules=[daily_schedule],
    sensors=[pipeline_failure_alert],
    resources={"dbt": DbtCliResource(project_dir=dbt_project)},
    executor=polite_executor,
)
