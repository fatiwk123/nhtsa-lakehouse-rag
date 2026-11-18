"""Fail (exit 1) when retrieval quality drops below the committed baseline.

  python -m eval.check_regression            # compare eval/results/retrieval.json to eval/baseline.json
  python -m eval.check_regression --update   # accept the current results as the new baseline

Gated metrics: the production configuration (hybrid_rerank on the assistant's chunking strategy)
and the "je ne sais pas" threshold. A drop larger than the tolerance (default: one question out
of 34, i.e. 0.03) fails the build; improvements are reported so the baseline can be raised.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from eval.common import EVAL_DIR, RESULTS_DIR, publish_markdown

BASELINE = EVAL_DIR / "baseline.json"


def gated_metrics(report: dict, strategy: str) -> dict[str, float]:
    prod = report["configs"][f"hybrid_rerank/{strategy}"]
    cal = report["calibration"]
    return {
        f"hybrid_rerank/{strategy} recall@5": prod["recall@5"],
        f"hybrid_rerank/{strategy} recall@1": prod["recall@1"],
        f"hybrid_rerank/{strategy} mrr": prod["mrr"],
        "threshold in_scope_accepted": cal["in_scope_accepted"],
        "threshold out_of_scope_rejected": cal["out_of_scope_rejected"],
    }


def compare(baseline: dict[str, float], current: dict[str, float], tolerance: float) -> tuple[bool, str]:
    lines = ["### Régression retrieval", "", "| Métrique | Baseline | Actuel | Écart | |", "|---|---|---|---|---|"]
    ok = True
    for name, base in baseline.items():
        cur = current.get(name)
        if cur is None:
            ok = False
            lines.append(f"| {name} | {base:.3f} | absent | | ❌ |")
            continue
        delta = cur - base
        status = "❌" if delta < -tolerance else ("⬆️" if delta > tolerance else "✅")
        ok &= status != "❌"
        lines.append(f"| {name} | {base:.3f} | {cur:.3f} | {delta:+.3f} | {status} |")
    lines += ["", f"Tolérance : {tolerance}. " + ("**OK**" if ok else "**Régression : la build échoue.**")]
    return ok, "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="write the current results as the baseline")
    parser.add_argument("--tolerance", type=float, default=float(os.environ.get("EVAL_TOLERANCE", "0.03")))
    args = parser.parse_args()

    report = json.loads((RESULTS_DIR / "retrieval.json").read_text(encoding="utf-8"))
    strategy = os.environ.get("RAG_STRATEGY", "fixed")
    current = gated_metrics(report, strategy)
    if args.update:
        BASELINE.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        print(f"baseline updated -> {BASELINE}")
        return
    ok, markdown = compare(json.loads(BASELINE.read_text(encoding="utf-8")), current, args.tolerance)
    publish_markdown(markdown)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
