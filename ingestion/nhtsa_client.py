"""Thin client for the public NHTSA API (https://api.nhtsa.gov)."""

from __future__ import annotations

import os
import threading
import time
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


class RateLimiter:
    """Thread-safe minimum interval between two requests (politeness towards a public API)."""

    def __init__(self, min_interval_s: float):
        self.min_interval_s = min_interval_s
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self.min_interval_s <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.min_interval_s
        if delay > 0:
            time.sleep(delay)


class NhtsaClient:
    def __init__(self, base_url: str = "https://api.nhtsa.gov", timeout: float = 30.0, min_interval_s: float | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        # Retry rate limiting (429) and server errors with exponential backoff, honouring Retry-After.
        # 403 is not retried: it means we are blocked, and hammering would only make it worse.
        retry = Retry(
            total=5,
            backoff_factor=2.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers["User-Agent"] = "nhtsa-lakehouse-rag/0.1"
        if min_interval_s is None:
            min_interval_s = float(os.environ.get("NHTSA_MIN_INTERVAL_S", "0.5"))
        self.limiter = RateLimiter(min_interval_s)

    def download(self, url: str, timeout: float = 120.0) -> bytes:
        self.limiter.wait()
        resp = self.session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    def get(self, path: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """GET an endpoint; returns (final URL, JSON payload)."""
        self.limiter.wait()
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
