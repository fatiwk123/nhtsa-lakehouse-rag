"""Iceberg catalog (SQL catalog backed by Postgres, data files on MinIO)."""

from __future__ import annotations

import time
from typing import Callable, TypeVar

from pyarrow import fs as pafs
from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog
from sqlalchemy.exc import IntegrityError, ProgrammingError

from ingestion.config import Settings

T = TypeVar("T")


def retry_on_conflict(fn: Callable[[], T], attempts: int = 5, delay: float = 0.5) -> T:
    """Retry an idempotent catalog operation that can race with another process
    (e.g. two Dagster steps creating the catalog tables or a namespace at the same time)."""
    for attempt in range(attempts):
        try:
            return fn()
        except (IntegrityError, ProgrammingError):
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))
    raise AssertionError("unreachable")


def get_catalog(settings: Settings) -> SqlCatalog:
    # SqlCatalog creates its own tables on first use, which races between parallel steps.
    return retry_on_conflict(
        lambda: SqlCatalog(
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
    )


def ensure_namespace(catalog: Catalog, namespace: str) -> None:
    retry_on_conflict(lambda: catalog.create_namespace_if_not_exists(namespace))


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
