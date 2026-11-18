from eval.agent_eval import value_in_answer
from eval.check_regression import compare
from eval.common import load_dataset
from eval.retrieval_eval import auc, best_threshold, score_question
from rag.search import Hit


def hit(campaign):
    return Hit("c", "d", "t", "x", 1, {"campaign_number": campaign}, 0.0)


def test_dataset_is_well_formed():
    questions = load_dataset()
    assert 40 <= len(questions) <= 60
    for q in questions:
        assert q["type"] in {"doc", "sql", "out_of_scope"}
        if q["type"] == "doc":
            assert q["relevant"]
        if q["type"] == "sql":
            assert q["expect"] in {"scalar", "top1", "top3"} and q["reference_sql"]


def test_recall_and_mrr_count_distinct_relevant_campaigns():
    hits = [hit("A"), hit("X"), hit("A"), hit("B")]
    s = score_question(hits, {"A", "B"})
    assert s["recall@1"] == 0.5 and s["recall@3"] == 0.5 and s["recall@5"] == 1.0 and s["mrr"] == 1.0
    assert score_question([hit("X")], {"A"})["mrr"] == 0.0


def test_auc_and_threshold():
    assert auc([3, 4], [-2, -1]) == 1.0 and auc([0], [0]) == 0.5
    t, j = best_threshold([3, 4], [-2, -1])
    assert -1 < t < 3 and j == 1.0


def test_value_in_answer():
    assert value_in_answer(52, "D'après la base : 52 plaintes.")
    assert not value_in_answer(52, "520 plaintes")
    assert value_in_answer(1234, "1 234 plaintes")
    assert value_in_answer("TIGUAN LIMITED", "le Tiguan Limited (3)")
    assert value_in_answer("SEAT_BELTS", "les seat belts")


def test_regression_gate():
    ok, _ = compare({"m": 0.90}, {"m": 0.88}, tolerance=0.03)
    assert ok
    ok, report = compare({"m": 0.90}, {"m": 0.85}, tolerance=0.03)
    assert not ok and "❌" in report
