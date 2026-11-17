"""Thin client for the public NHTSA API (https://api.nhtsa.gov)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


@dataclass(frozen=True)
class SourceSpec:
    name: str
    path: str
    id_field: str
    issue_type: str  # "r" (recalls) or "c" (complaints), used by the products endpoint


SOURCES: dict[str, SourceSpec] = {
    "recalls": SourceSpec("recalls", "recalls/recallsByVehicle", "NHTSACampaignNumber", "r"),
    "complaints": SourceSpec("complaints", "complaints/complaintsByVehicle", "odiNumber", "c"),
}


class NhtsaClient:
    def __init__(self, base_url: str = "https://api.nhtsa.gov", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers["User-Agent"] = "nhtsa-lakehouse-rag/0.1"

    def get(self, path: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """GET an endpoint; returns (final URL, JSON payload)."""
        resp = self.session.get(f"{self.base_url}/{path}", params=params, timeout=self.timeout)
        # The API answers 400 for unknown make/model/year combinations: treat as empty.
        if resp.status_code in (400, 404):
            return resp.url, {"results": []}
        resp.raise_for_status()
        return resp.url, resp.json()

    def list_models(self, make: str, model_year: int, issue_type: str) -> list[str]:
        _, payload = self.get(
            "products/vehicle/models",
            {"modelYear": model_year, "make": make, "issueType": issue_type},
        )
        # The endpoint returns duplicates; normalise and dedupe.
        return sorted({r["model"].strip().upper() for r in payload.get("results", []) if r.get("model")})

    def fetch(self, source: SourceSpec, make: str, model: str, model_year: int) -> tuple[str, dict[str, Any]]:
        return self.get(source.path, {"make": make, "model": model, "modelYear": model_year})
