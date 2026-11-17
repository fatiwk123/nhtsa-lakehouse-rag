from datetime import date

import pytest

from ingestion.documents import doc_type, select_documents
from rag.chunking import fixed_window, structure_aware
from rag.documents import Document, Section, clean_pdf_text, recall_documents
from rag.indexer import plan_sync

RECALL_ROWS = [
    {
        "campaign_number": "18V369000", "make": "VOLKSWAGEN", "model": model, "model_year": 2018,
        "manufacturer": "Volkswagen Group of America, Inc.",
        "component": "SERVICE BRAKES, HYDRAULIC:FOUNDATION COMPONENTS:DISC:CALIPER",
        "component_group": "SERVICE BRAKES, HYDRAULIC", "category": "BRAKES", "category_fr": "Freinage",
        "report_received_date": date(2018, 6, 1), "summary": "Caliper pistons may have insufficient coating.",
        "consequence": "Reduced braking performance.", "remedy": "Dealers will bleed the calipers.", "park_it": True,
    }
    for model in ("GOLF", "PASSAT")
]


def test_recall_rows_grouped_into_one_document_per_campaign():
    docs = recall_documents(RECALL_ROWS)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.doc_id == "recall:18V369000"
    assert doc.metadata["models"] == ["GOLF", "PASSAT"]
    assert doc.metadata["category"] == "BRAKES"
    assert [s.heading for s in doc.sections] == ["Overview", "Summary", "Consequence", "Remedy"]


def test_content_hash_changes_only_with_content():
    a, b = recall_documents(RECALL_ROWS)[0], recall_documents(RECALL_ROWS)[0]
    assert a.content_hash == b.content_hash
    changed = [{**r, "remedy": "New remedy."} for r in RECALL_ROWS]
    assert recall_documents(changed)[0].content_hash != a.content_hash


def _pdf(pages: list[str]) -> Document:
    return Document("pdf:1", "pdf", "Title", [Section(text=t, page=i + 1) for i, t in enumerate(pages)])


def test_fixed_window_overlap_and_pages():
    doc = _pdf([" ".join(f"a{i}" for i in range(100)), " ".join(f"b{i}" for i in range(100))])
    chunks = fixed_window(doc, window=80, overlap=20)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert chunks[0].text.split()[-20:] == chunks[1].text.split()[:20]
    assert chunks[0].page_start == 1 and chunks[-1].page_end == 2
    assert all(len(c.text.split()) <= 80 for c in chunks)
    with pytest.raises(ValueError):
        fixed_window(doc, window=10, overlap=10)


def test_structure_chunks_respect_pages_and_budget():
    page1 = "\n\n".join(" ".join(["word"] * 30) for _ in range(5))  # 5 paragraphs x 30 words
    page2 = "Short page."
    chunks = structure_aware(_pdf([page1, page2]), max_words=70)
    assert {c.page_start for c in chunks} == {1, 2}
    assert all(c.page_start == c.page_end for c in chunks)
    assert all(len(c.text.split()) <= 70 for c in chunks)
    assert chunks[-1].text == "Short page."


def test_structure_splits_overlong_paragraph():
    long_paragraph = ". ".join(" ".join(["w"] * 40) for _ in range(5))
    chunks = structure_aware(_pdf([long_paragraph]), max_words=50)
    assert len(chunks) >= 4
    assert all(len(c.text.split()) <= 50 for c in chunks)


def test_plan_sync_detects_new_changed_deleted_and_version_bump():
    docs = recall_documents(RECALL_ROWS)
    doc = docs[0]
    assert plan_sync(docs, {}, "v1") == (docs, [], 0)
    assert plan_sync(docs, {doc.doc_id: (doc.content_hash, "v1")}, "v1") == ([], [], 1)
    assert plan_sync(docs, {doc.doc_id: ("old", "v1")}, "v1")[0] == docs
    assert plan_sync(docs, {doc.doc_id: (doc.content_hash, "v1")}, "v2")[0] == docs
    assert plan_sync(docs, {"recall:GONE": ("h", "v1")}, "v1")[1] == ["recall:GONE"]


def test_select_documents_filters_types_and_caps():
    docs = [
        {"id": 1, "fileName": "RCLQRT-18V369-1.PDF", "url": "u", "loadDate": "2018-01"},
        {"id": 2, "fileName": "RCMN-18V369-2.pdf", "url": "u", "loadDate": "2018-02"},
        {"id": 3, "fileName": "RCLRPT-18V369-3.PDF", "url": "u", "loadDate": "2018-03"},
        {"id": 4, "fileName": "RCMN-18V369-4.pdf", "url": "u", "loadDate": "2018-01"},
        {"id": 5, "fileName": "RCLRPT-18V369-5.PDF", "url": "u", "loadDate": "2018-06"},  # amended report
    ]
    # Only the latest Part 573 report, then dealer notices newest first.
    assert [d["id"] for d in select_documents(docs, ["RCLRPT", "RCMN"], 3)] == [5, 2, 4]
    assert [d["id"] for d in select_documents(docs, ["RCLRPT", "RCMN"], 2)] == [5, 2]
    assert doc_type("rcmn-18V369-2.pdf") == "RCMN"


def test_clean_pdf_text():
    assert clean_pdf_text("a  \t b \n\n\n\n c d") == "a b\n\nc d"
