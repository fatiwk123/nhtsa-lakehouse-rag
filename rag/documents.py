"""Build RAG documents from the lakehouse.

- One document per recall campaign, from gold.fct_recalls (Iceberg).
- One document per recall PDF, from bronze.nhtsa_recall_documents (Iceberg) + the file on MinIO,
  text extracted page by page with PyMuPDF.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from pyarrow import fs as pafs
from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import NoSuchTableError

from ingestion.documents import DOC_TYPE_LABELS, SINGLE_VERSION_TYPES

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Section:
    text: str
    heading: str | None = None
    page: int | None = None


@dataclass
class Document:
    doc_id: str
    source_type: str  # "recall" | "pdf"
    title: str
    sections: list[Section]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        payload = {
            "title": self.title,
            "sections": [(s.heading, s.page, s.text) for s in self.sections],
            "metadata": self.metadata,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    @property
    def word_count(self) -> int:
        return sum(len(s.text.split()) for s in self.sections)


def _table_rows(catalog: Catalog, name: str) -> list[dict[str, Any]]:
    try:
        return catalog.load_table(name).scan().to_arrow().to_pylist()
    except NoSuchTableError:
        return []


def _vehicles_label(vehicles: Iterable[tuple[str, str, int]]) -> str:
    by_model: dict[tuple[str, str], set[int]] = defaultdict(set)
    for make, model, year in vehicles:
        by_model[(make, model)].add(year)
    return "; ".join(
        f"{make} {model} ({', '.join(str(y) for y in sorted(years))})"
        for (make, model), years in sorted(by_model.items())
    )


def recall_documents(recall_rows: list[dict[str, Any]]) -> list[Document]:
    """Group gold.fct_recalls rows (campaign x vehicle) into one document per campaign."""
    by_campaign: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in recall_rows:
        by_campaign[row["campaign_number"]].append(row)

    docs = []
    for campaign, rows in sorted(by_campaign.items()):
        first = rows[0]
        vehicles = sorted({(r["make"], r["model"], r["model_year"]) for r in rows})
        label = _vehicles_label(vehicles)
        report_date = first["report_received_date"].isoformat() if first.get("report_received_date") else None
        sections = [
            Section(
                heading="Overview",
                text=(
                    f"NHTSA recall {campaign} filed by {first.get('manufacturer') or first['make']}"
                    f"{' on ' + report_date if report_date else ''}. "
                    f"Component: {first['component']} (category {first['category']}, {first['category_fr']}). "
                    f"Vehicles: {label}."
                ),
            )
        ]
        for heading, key in (("Summary", "summary"), ("Consequence", "consequence"), ("Remedy", "remedy")):
            if first.get(key):
                sections.append(Section(heading=heading, text=first[key]))
        docs.append(
            Document(
                doc_id=f"recall:{campaign}",
                source_type="recall",
                title=f"Recall {campaign} - {first['component_group']} - {label}",
                sections=sections,
                metadata={
                    "campaign_number": campaign,
                    "makes": sorted({v[0] for v in vehicles}),
                    "models": sorted({v[1] for v in vehicles}),
                    "model_years": sorted({v[2] for v in vehicles}),
                    "component": first["component"],
                    "component_group": first["component_group"],
                    "category": first["category"],
                    "report_received_date": report_date,
                    "park_it": first.get("park_it"),
                    "source_url": f"https://www.nhtsa.gov/recalls?nhtsaId={campaign[:6]}",
                },
            )
        )
    return docs


def clean_pdf_text(text: str) -> str:
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf_sections(content: bytes) -> list[Section]:
    import fitz  # PyMuPDF

    sections = []
    with fitz.open(stream=content, filetype="pdf") as pdf:
        for page_no, page in enumerate(pdf, start=1):
            text = clean_pdf_text(page.get_text("text"))
            if text:
                sections.append(Section(text=text, page=page_no))
    return sections


def pdf_documents(
    doc_rows: list[dict[str, Any]],
    recall_docs: dict[str, Document],
    store: pafs.S3FileSystem,
) -> tuple[list[Document], list[str]]:
    """Return (documents, ids of PDFs without extractable text, e.g. scanned images)."""
    docs, no_text = [], []
    for row in doc_rows:
        recall = recall_docs.get(f"recall:{row['campaign_number']}")
        if recall is None:  # campaign no longer in scope
            continue
        path = row["raw_file"].removeprefix("s3://")
        with store.open_input_stream(path) as f:
            sections = extract_pdf_sections(f.read())
        if not sections:
            no_text.append(row["document_id"])
            continue
        label = DOC_TYPE_LABELS.get(row["doc_type"], row["doc_type"])
        docs.append(
            Document(
                doc_id=f"pdf:{row['document_id']}",
                source_type="pdf",
                title=f"{label} - recall {row['campaign_number']} - {recall.metadata['component_group']}",
                sections=sections,
                metadata={
                    **{k: recall.metadata[k] for k in ("campaign_number", "makes", "models", "model_years", "component_group", "category")},
                    "doc_type": row["doc_type"],
                    "doc_type_label": label,
                    "file_name": row["file_name"],
                    "document_summary": row["summary"],
                    "source_url": row["source_url"],
                    "sha256": row["sha256"],
                },
            )
        )
    return docs, no_text


def build_documents(catalog: Catalog, store: pafs.S3FileSystem) -> tuple[list[Document], dict[str, Any]]:
    recalls = recall_documents(_table_rows(catalog, "gold.fct_recalls"))
    by_id = {d.doc_id: d for d in recalls}
    doc_rows = _table_rows(catalog, "bronze.nhtsa_recall_documents")
    # Latest row per document id (bronze is append-only), then one version per
    # campaign for single-version types (amended Part 573 reports).
    latest: dict[str, dict[str, Any]] = {}
    for row in sorted(doc_rows, key=lambda r: r["ingested_at"]):
        latest[row["document_id"]] = row
    selected: dict[str, dict[str, Any]] = {}
    for row in sorted(latest.values(), key=lambda r: r["load_date"] or ""):
        key = (
            f"{row['campaign_number']}:{row['doc_type']}"
            if row["doc_type"] in SINGLE_VERSION_TYPES
            else row["document_id"]
        )
        selected[key] = row  # newest wins
    pdfs, no_text = pdf_documents(list(selected.values()), by_id, store)
    stats = {"recall_documents": len(recalls), "pdf_documents": len(pdfs), "pdf_without_text": len(no_text)}
    return recalls + pdfs, stats
