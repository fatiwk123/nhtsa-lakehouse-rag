"""End-to-end assistant evaluation (LLM included, run locally: ~1-3 min per question on CPU).

  python -m eval.agent_eval                     # every question
  python -m eval.agent_eval --types sql --limit 5
  python -m eval.agent_eval --judge             # + LLM-as-judge faithfulness (one extra LLM call per answer)

Metrics
  doc           answered rate, citation precision (cited passages from an expected campaign),
                key-term coverage (must_mention)
  sql           exact answer: the reference query's value(s) appear in the answer
  out_of_scope  refusal rate ("je ne sais pas")
  all           faithfulness proxies: answers without unsupported numbers / with valid citations,
                optional LLM judge; latency p50/p95; tokens and cost per question
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any

from eval.common import load_dataset, mean, percentile, publish_markdown, write_json
from rag.agent import Answer
from rag.service import Assistant
from rag.sql_tool import run_sql

JUDGE_PROMPT = """You check an assistant's answer against the context it was given.
Context (tool outputs):
{context}

Question: {question}
Answer: {answer}

Is every factual statement of the answer supported by the context? Paraphrases and translations are fine.
Reply with JSON only: {{"faithful": true or false, "unsupported": "<the unsupported statement, or empty>"}}"""

_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    text = re.sub(r"(?<=\d)[\s ](?=\d{3}\b)", "", text)  # 1 234 -> 1234
    return {n.replace(",", ".").rstrip("0").rstrip(".") if "." in n.replace(",", ".") else n for n in _NUM.findall(text)}


def value_in_answer(value: Any, answer: str) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        v = f"{value:g}" if isinstance(value, float) else str(value)
        return v in _numbers(answer)
    return str(value).lower().replace("_", " ") in answer.lower().replace("_", " ")


def expected_values(q: dict[str, Any], duckdb_path: str) -> list[Any]:
    result = run_sql(q["reference_sql"], duckdb_path, max_rows=10)
    if result.error:
        raise RuntimeError(f"{q['id']}: reference SQL failed: {result.error}")
    if q["expect"] == "scalar":
        return [result.rows[0][0]] if result.rows else [0]
    n = 3 if q["expect"] == "top3" else 1
    return [row[0] for row in result.rows[:n]]


def judge(assistant: Assistant, q: dict[str, Any], ans: Answer) -> dict[str, Any]:
    context = "\n\n---\n\n".join(ans.context)[:12000]
    reply = assistant.llm.chat(
        [{"role": "user", "content": JUDGE_PROMPT.format(context=context, question=q["question"], answer=ans.answer)}]
    )
    match = re.search(r"\{.*\}", reply.content, re.DOTALL)
    try:
        verdict = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        verdict = {}
    return {"faithful": bool(verdict.get("faithful")), "unsupported": verdict.get("unsupported", ""),
            "tokens": reply.prompt_tokens + reply.completion_tokens}


def evaluate(q: dict[str, Any], ans: Answer, assistant: Assistant) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": q["id"], "type": q["type"], "question": q["question"], "answer": ans.answer,
        "dont_know": ans.dont_know, "latency_ms": ans.latency_ms, "llm_calls": ans.llm_calls,
        "tokens": ans.prompt_tokens + ans.completion_tokens, "prompt_tokens": ans.prompt_tokens,
        "completion_tokens": ans.completion_tokens, "unsupported_numbers": ans.unsupported_numbers,
        "tools": [f"{c.tool}:{c.summary}" for c in ans.tool_calls], "trace_id": ans.trace_id,
    }
    if q["type"] == "doc":
        relevant = set(q["relevant"])
        cited = [s.campaign_number for s in ans.sources] if ans.cited else []
        row["answered"] = not ans.dont_know
        row["cited"] = ans.cited
        row["citation_precision"] = mean([c in relevant for c in cited]) if cited else None
        terms = q.get("must_mention") or []
        row["mentions_ok"] = all(any(t.lower() in ans.answer.lower() for t in group) for group in terms) if terms else None
        row["correct"] = row["answered"] and (row["citation_precision"] or 0) > 0 and row["mentions_ok"] is not False
    elif q["type"] == "sql":
        expected = expected_values(q, assistant.settings.duckdb_path)
        row["expected"] = expected
        row["correct"] = (not ans.dont_know) and all(value_in_answer(v, ans.answer) for v in expected)
        row["sql"] = [s["sql"] for s in ans.sql]
    else:
        row["correct"] = ans.dont_know
    return row


def summarize(rows: list[dict[str, Any]], price_in: float, price_out: float, judged: bool) -> dict[str, Any]:
    by_type = {t: [r for r in rows if r["type"] == t] for t in ("doc", "sql", "out_of_scope")}
    answered = [r for r in rows if not r["dont_know"]]
    doc = by_type["doc"]
    precisions = [r["citation_precision"] for r in doc if r.get("citation_precision") is not None]
    cost = [(r["prompt_tokens"] * price_in + r["completion_tokens"] * price_out) / 1e6 for r in rows]
    summary = {
        "questions": len(rows),
        "accuracy": {t: round(mean([r["correct"] for r in rs]), 3) for t, rs in by_type.items() if rs},
        "overall_accuracy": round(mean([r["correct"] for r in rows]), 3),
        "doc_answered": round(mean([r["answered"] for r in doc]), 3) if doc else None,
        "doc_cited": round(mean([r["cited"] for r in doc]), 3) if doc else None,
        "doc_citation_precision": round(mean(precisions), 3) if precisions else None,
        "false_refusals": sum(r["dont_know"] for r in rows if r["type"] != "out_of_scope"),
        "hallucinated_refusal_misses": sum(not r["dont_know"] for r in by_type["out_of_scope"]),
        "answers_with_unsupported_numbers": round(mean([bool(r["unsupported_numbers"]) for r in answered]), 3),
        "latency_p50_s": round(percentile([r["latency_ms"] for r in rows], 50) / 1000, 1),
        "latency_p95_s": round(percentile([r["latency_ms"] for r in rows], 95) / 1000, 1),
        "tokens_per_question": round(mean([r["tokens"] for r in rows])),
        "llm_calls_per_question": round(mean([r["llm_calls"] for r in rows]), 2),
        "cost_per_question_usd": round(mean(cost), 5),
        "price_per_m_tokens_usd": {"input": price_in, "output": price_out},
    }
    if judged:
        verdicts = [r["judge"]["faithful"] for r in answered if "judge" in r]
        summary["judge_faithfulness"] = round(mean(verdicts), 3) if verdicts else None
    return summary


def markdown(summary: dict[str, Any], model: str) -> str:
    acc = summary["accuracy"]
    lines = [
        f"### Assistant de bout en bout ({summary['questions']} questions, LLM `{model}`)",
        "",
        "| Métrique | Valeur |",
        "|---|---|",
        f"| Exactitude globale | {summary['overall_accuracy']:.0%} |",
        f"| Documents : réponse correcte et citée | {acc.get('doc', 0):.0%} |",
        f"| Documents : précision des citations | {summary['doc_citation_precision'] or 0:.0%} |",
        f"| SQL : valeur exacte | {acc.get('sql', 0):.0%} |",
        f"| Hors périmètre : « je ne sais pas » | {acc.get('out_of_scope', 0):.0%} |",
        f"| Refus à tort (dans le périmètre) | {summary['false_refusals']} |",
        f"| Réponses avec un nombre non soutenu | {summary['answers_with_unsupported_numbers']:.0%} |",
    ]
    if "judge_faithfulness" in summary:
        lines.append(f"| Fidélité (LLM juge) | {summary['judge_faithfulness'] or 0:.0%} |")
    lines += [
        f"| Latence p50 / p95 | {summary['latency_p50_s']} s / {summary['latency_p95_s']} s |",
        f"| Tokens / question | {summary['tokens_per_question']} ({summary['llm_calls_per_question']} appels LLM) |",
        f"| Coût / question | {summary['cost_per_question_usd']} $ |",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--types", nargs="*", default=["doc", "sql", "out_of_scope"])
    parser.add_argument("--limit", type=int, default=0, help="max questions per type (0 = all)")
    parser.add_argument("--judge", action="store_true")
    args = parser.parse_args()

    # Local model: 0 $. Set a hosted model's prices to compare (USD per million tokens).
    price_in = float(os.environ.get("EVAL_PRICE_INPUT_PER_M", "0"))
    price_out = float(os.environ.get("EVAL_PRICE_OUTPUT_PER_M", "0"))

    assistant = Assistant()
    assistant.warm_up()
    questions = [q for q in load_dataset() if q["type"] in args.types]
    if args.limit:
        questions = [q for t in args.types for q in [x for x in questions if x["type"] == t][: args.limit]]

    rows = []
    for i, q in enumerate(questions, start=1):
        ans = assistant.ask(q["question"])
        row = evaluate(q, ans, assistant)
        if args.judge and not ans.dont_know:
            row["judge"] = judge(assistant, q, ans)
        rows.append(row)
        print(f"[{i}/{len(questions)}] {q['id']} correct={row['correct']} {ans.latency_ms / 1000:.0f}s", flush=True)
        write_json("agent_rows.json", rows)  # partial results survive an interruption
    assistant.tracer.flush()

    summary = summarize(rows, price_in, price_out, args.judge)
    write_json("agent.json", {"model": assistant.settings.llm_model, "summary": summary, "rows": rows})
    publish_markdown(markdown(summary, assistant.settings.llm_model))


if __name__ == "__main__":
    main()
