"""Dagster definitions. Phase 1: bronze ingestion assets + a manual job."""

from dagster import AssetSelection, Definitions, MaterializeResult, MetadataValue, asset, define_asset_job

from ingestion.bronze import run_ingestion


def _materialize(source: str) -> MaterializeResult:
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
    return _materialize("recalls")


@asset(group_name="bronze", kinds={"python", "iceberg"}, description="Raw NHTSA complaints (append-only, hash-deduplicated).")
def bronze_nhtsa_complaints() -> MaterializeResult:
    return _materialize("complaints")


bronze_job = define_asset_job("bronze_ingestion", selection=AssetSelection.groups("bronze"))

defs = Definitions(assets=[bronze_nhtsa_recalls, bronze_nhtsa_complaints], jobs=[bronze_job])
