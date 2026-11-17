"""Dagster definitions: bronze ingestion -> dbt silver/gold -> Soda -> Iceberg publish.

Runs daily; any failure (API, dbt test, Soda check) fails the run and fires the alert sensor.
"""

import os
from pathlib import Path

import requests
from dagster import (
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
    define_asset_job,
    run_failure_sensor,
)
from dagster_dbt import DbtCliResource, DbtProject, dbt_assets, get_asset_key_for_model

from ingestion.bronze import run_ingestion
from ingestion.catalog import get_catalog
from ingestion.config import Settings
from orchestration.publish import publish_layers
from orchestration.quality import run_soda_scan

DBT_DIR = Path(__file__).resolve().parents[1] / "dbt"
dbt_project = DbtProject(project_dir=DBT_DIR, profiles_dir=DBT_DIR)
dbt_project.prepare_if_dev()

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


@asset(group_name="bronze", kinds={"python", "iceberg"}, description="Raw NHTSA recalls (append-only, hash-deduplicated).")
def bronze_nhtsa_recalls() -> MaterializeResult:
    return _ingest("recalls")


@asset(group_name="bronze", kinds={"python", "iceberg"}, description="Raw NHTSA complaints (append-only, hash-deduplicated).")
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
    assets=[bronze_nhtsa_recalls, bronze_nhtsa_complaints, nhtsa_dbt_models, soda_gold_checks, iceberg_silver_gold],
    jobs=[daily_pipeline, bronze_job],
    schedules=[daily_schedule],
    sensors=[pipeline_failure_alert],
    resources={"dbt": DbtCliResource(project_dir=dbt_project)},
)
