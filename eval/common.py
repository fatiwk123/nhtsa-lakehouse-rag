"""Shared helpers for the evaluation scripts."""

from __future__ import annotations

import json
import os
import statistics
from pathlib import Path
from typing import Any

import yaml

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"


def load_dataset(path: Path = EVAL_DIR / "dataset.yaml") -> list[dict[str, Any]]:
    questions = yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]
    ids = [q["id"] for q in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate question ids in the dataset")
    return questions


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[idx]


def mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def write_json(name: str, data: Any) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def publish_markdown(markdown: str) -> None:
    """Print, and add to the GitHub Actions job summary when running in CI."""
    print(markdown)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")
