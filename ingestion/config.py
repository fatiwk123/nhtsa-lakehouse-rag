"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _parse_years(value: str) -> list[int]:
    """Parse "2018-2020" or "2019,2021" into a sorted list of years."""
    years: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
            if start > end:
                raise ValueError(f"Invalid year range: {part!r}")
            years.update(range(start, end + 1))
        else:
            years.add(int(part))
    return sorted(years)


def _parse_list(value: str) -> list[str]:
    return [x.strip().upper() for x in value.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    postgres_user: str
    postgres_password: str
    postgres_host: str
    postgres_port: int
    catalog_db: str

    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    bucket: str

    makes: list[str]
    model_years: list[int]
    models: list[str]
    max_workers: int

    # Recall PDFs: document types (file name prefix) and cap per campaign.
    doc_types: list[str]
    max_docs_per_campaign: int

    nhtsa_base_url: str = "https://api.nhtsa.gov"

    @property
    def catalog_uri(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.catalog_db}"
        )

    @property
    def warehouse(self) -> str:
        return f"s3://{self.bucket}/warehouse"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            postgres_user=env("POSTGRES_USER", "lakehouse"),
            postgres_password=env("POSTGRES_PASSWORD", "lakehouse"),
            postgres_host=env("POSTGRES_HOST", "localhost"),
            postgres_port=int(env("POSTGRES_PORT", "5432")),
            catalog_db=env("ICEBERG_CATALOG_DB", env("POSTGRES_DB", "lakehouse")),
            minio_endpoint=env("MINIO_ENDPOINT", "http://localhost:9000"),
            minio_access_key=env("MINIO_ACCESS_KEY", "minioadmin"),
            minio_secret_key=env("MINIO_SECRET_KEY", "minioadmin"),
            bucket=env("LAKEHOUSE_BUCKET", "lakehouse"),
            makes=_parse_list(env("NHTSA_MAKES", "VOLKSWAGEN")),
            model_years=_parse_years(env("NHTSA_MODEL_YEARS", "2018-2020")),
            models=_parse_list(env("NHTSA_MODELS", "")),
            max_workers=int(env("NHTSA_MAX_WORKERS", "2")),
            doc_types=_parse_list(env("NHTSA_DOC_TYPES", "RCLRPT,RCMN")),
            max_docs_per_campaign=int(env("NHTSA_MAX_DOCS_PER_CAMPAIGN", "4")),
        )
