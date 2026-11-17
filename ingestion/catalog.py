"""Iceberg catalog (SQL catalog backed by Postgres, data files on MinIO)."""

from __future__ import annotations

from pyarrow import fs as pafs
from pyiceberg.catalog.sql import SqlCatalog

from ingestion.config import Settings


def get_catalog(settings: Settings) -> SqlCatalog:
    return SqlCatalog(
        "lakehouse",
        **{
            "uri": settings.catalog_uri,
            "warehouse": settings.warehouse,
            "s3.endpoint": settings.minio_endpoint,
            "s3.access-key-id": settings.minio_access_key,
            "s3.secret-access-key": settings.minio_secret_key,
            "s3.region": "us-east-1",
        },
    )


def get_object_store(settings: Settings) -> pafs.S3FileSystem:
    """Filesystem used to land raw JSON files next to the Iceberg warehouse."""
    scheme, _, host = settings.minio_endpoint.partition("://")
    return pafs.S3FileSystem(
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        endpoint_override=host,
        scheme=scheme,
        region="us-east-1",
    )
