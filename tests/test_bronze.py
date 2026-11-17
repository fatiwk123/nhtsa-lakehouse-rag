import pyarrow as pa
import pytest

from ingestion.bronze import ARROW_SCHEMA, IngestionContext, build_rows, filter_new, row_hash, slug
from ingestion.config import _parse_years
from ingestion.nhtsa_client import SOURCES

RECALL = {"NHTSACampaignNumber": "19V188000", "Component": "SUSPENSION", "Model": "GOLF", "ModelYear": "2019"}


def test_parse_years():
    assert _parse_years("2018-2020") == [2018, 2019, 2020]
    assert _parse_years("2021, 2019,2019") == [2019, 2021]
    with pytest.raises(ValueError):
        _parse_years("2020-2018")


def test_slug():
    assert slug("GOLF SPORTWAGEN") == "golf-sportwagen"
    assert slug("E-GOLF") == "e-golf"


def test_row_hash_is_deterministic_and_key_order_independent():
    reordered = dict(reversed(list(RECALL.items())))
    assert row_hash("VW", "GOLF", 2019, RECALL) == row_hash("VW", "GOLF", 2019, reordered)
    assert row_hash("VW", "GOLF", 2019, RECALL) != row_hash("VW", "GOLF", 2020, RECALL)
    assert row_hash("VW", "GOLF", 2019, RECALL) != row_hash("VW", "GOLF", 2019, {**RECALL, "Remedy": "new"})


def test_build_rows_matches_arrow_schema():
    ctx = IngestionContext()
    rows = build_rows(SOURCES["recalls"], ctx, "VOLKSWAGEN", "GOLF", 2019, "url", "s3://f.json", {"results": [RECALL]})
    assert rows[0]["record_id"] == "19V188000"
    table = pa.Table.from_pylist(rows, schema=ARROW_SCHEMA)
    assert table.num_rows == 1


def test_build_rows_handles_empty_payload():
    assert build_rows(SOURCES["complaints"], IngestionContext(), "VW", "GOLF", 2019, "u", "f", {}) == []


def test_filter_new_skips_known_and_in_batch_duplicates():
    rows = [{"row_hash": h} for h in ("a", "b", "b", "c")]
    assert [r["row_hash"] for r in filter_new(rows, {"a"})] == ["b", "c"]


def test_rate_limiter_spaces_requests():
    import time

    from ingestion.nhtsa_client import RateLimiter

    limiter = RateLimiter(0.05)
    start = time.monotonic()
    for _ in range(4):
        limiter.wait()
    assert time.monotonic() - start >= 0.15
