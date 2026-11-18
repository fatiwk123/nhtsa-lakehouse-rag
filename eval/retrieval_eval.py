"""Retrieval evaluation (no LLM): recall@k / MRR for every mode x chunking strategy, latency,
and calibration of the "je ne sais pas" threshold (in-scope vs out-of-scope questions).

  python -m eval.retrieval_eval            # writes eval/results/retrieval.json and prints a markdown report
"""

from __future__ import annotations

import time
from typing import Any

from eval.common import load_dataset, mean, percentile, publish_markdown, write_json
from rag.retrieval import MODES
from rag.service import Assistant

KS = (1, 3, 5, 10)
STRATEGIES = ("structure", "fixed")


def relevant_ranks(hits, relevant: set[str]) -> dict[str, int]:
    """campaign -> best (1-based) rank at which one of its passages appears."""
    ranks: dict[str, int] = {}
    for rank, hit in enumerate(hits, start=1):
        campaign = (hit.metadata or {}).get("campaign_number")
        if campaign in relevant and campaign not in ranks:
            ranks[campaign] = rank
    return ranks


def score_question(hits, relevant: set[str]) -> dict[str, float]:
    ranks = relevant_ranks(hits, relevant)
    out = {f"recall@{k}": sum(r <= k for r in ranks.values()) / len(relevant) for k in KS}
    out["mrr"] = 1 / min(ranks.values()) if ranks else 0.0
    return out


def auc(positives: list[float], negatives: list[float]) -> float:
    """P(score of an in-scope question > score of an out-of-scope one); ties count 1/2."""
    if not positives or not negatives:
        return 0.0
    wins = sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
    return wins / (len(positives) * len(negatives))


def best_threshold(positives: list[float], negatives: list[float]) -> tuple[float, float]:
    """Threshold maximising Youden's J = TPR - FPR (midpoint between consecutive scores)."""
    scores = sorted(set(positives + negatives))
    best = (0.0, -1.0)
    for lo, hi in zip(scores, scores[1:]):
        t = (lo + hi) / 2
        j = mean([p >= t for p in positives]) - mean([n >= t for n in negatives])
        if j > best[1]:
            best = (t, j)
    return best


def run(assistant: Assistant) -> dict[str, Any]:
    questions = load_dataset()
    docs = [q for q in questions if q["type"] == "doc"]
    oos = [q for q in questions if q["type"] == "out_of_scope"]
    k_max = max(KS)

    configs: dict[str, Any] = {}
    per_question: dict[str, Any] = {}
    top_scores: dict[str, float] = {}
    for mode in MODES:
        for strategy in STRATEGIES:
            key = f"{mode}/{strategy}"
            scores, latencies = [], []
            for q in docs:
                start = time.perf_counter()
                result = assistant.search(q["question"], mode=mode, k=k_max, strategy=strategy)
                latencies.append((time.perf_counter() - start) * 1000)
                s = score_question(result.hits, set(q["relevant"]))
                scores.append(s)
                per_question.setdefault(q["id"], {})[key] = s["recall@5"]
                if key == "hybrid_rerank/structure" and result.hits:
                    top_scores[q["id"]] = result.hits[0].score
            configs[key] = {
                **{m: round(mean([s[m] for s in scores]), 4) for m in scores[0]},
                "latency_p50_ms": round(percentile(latencies, 50)),
                "latency_p95_ms": round(percentile(latencies, 95)),
            }

    # Threshold calibration on the reranker score of the best passage.
    for q in oos:
        result = assistant.search(q["question"], mode="hybrid_rerank", k=1)
        top_scores[q["id"]] = result.hits[0].score if result.hits else float("-inf")
    pos = [top_scores[q["id"]] for q in docs if q["id"] in top_scores]
    neg = [top_scores[q["id"]] for q in oos]
    threshold = assistant.settings.min_rerank_score
    best_t, best_j = best_threshold(pos, neg)
    calibration = {
        "threshold": threshold,
        "auc": round(auc(pos, neg), 4),
        "in_scope_accepted": round(mean([p >= threshold for p in pos]), 4),
        "out_of_scope_rejected": round(mean([n < threshold for n in neg]), 4),
        "best_threshold": round(best_t, 3),
        "best_youden_j": round(best_j, 4),
        "in_scope_min_score": round(min(pos), 3) if pos else None,
        "out_of_scope_max_score": round(max(neg), 3) if neg else None,
    }
    return {
        "dataset": {"doc_questions": len(docs), "out_of_scope_questions": len(oos)},
        "configs": configs,
        "calibration": calibration,
        "per_question_recall@5": per_question,
        "top_rerank_scores": {k: round(v, 3) for k, v in top_scores.items()},
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        f"### Retrieval ({report['dataset']['doc_questions']} questions documentaires)",
        "",
        "| Mode | Chunking | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR | Latence p50 | p95 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key, m in report["configs"].items():
        mode, strategy = key.split("/")
        lines.append(
            f"| {mode} | {strategy} | {m['recall@1']:.2f} | {m['recall@3']:.2f} | {m['recall@5']:.2f} | "
            f"{m['recall@10']:.2f} | {m['mrr']:.2f} | {m['latency_p50_ms']} ms | {m['latency_p95_ms']} ms |"
        )
    c = report["calibration"]
    lines += [
        "",
        f"### Seuil « je ne sais pas » ({report['dataset']['out_of_scope_questions']} questions hors périmètre)",
        "",
        f"- AUC (score du meilleur passage, dans vs hors périmètre) : **{c['auc']:.2f}**",
        f"- Seuil actuel {c['threshold']} : {c['in_scope_accepted']:.0%} des questions documentaires acceptées, "
        f"{c['out_of_scope_rejected']:.0%} des hors-périmètre rejetées",
        f"- Meilleur seuil (Youden) : {c['best_threshold']} (J = {c['best_youden_j']:.2f}) ; "
        f"score min. dans le périmètre {c['in_scope_min_score']}, max. hors périmètre {c['out_of_scope_max_score']}",
    ]
    return "\n".join(lines)


def main() -> None:
    assistant = Assistant()
    assistant.warm_up()
    report = run(assistant)
    path = write_json("retrieval.json", report)
    publish_markdown(markdown(report))
    print(f"\n-> {path}")


if __name__ == "__main__":
    main()
