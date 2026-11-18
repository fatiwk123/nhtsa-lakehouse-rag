"""Tool-using assistant: search_docs (hybrid RAG) + query_sql (read-only gold), answers with citations.

Guarantees enforced in code, not only in the prompt:
  - the model must ground itself: if it answers without calling a tool, a search is forced first;
  - no evidence (no relevant passage, no SQL rows) -> a fixed "je ne sais pas" answer, the LLM is not trusted;
  - citations are checked: only [S#] that were really returned are kept as sources.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from rag.llm import ChatModel
from rag.retrieval import RetrievalResult
from rag.search import Hit
from rag.sql_tool import SCHEMA_DOC, SqlResult
from rag.tracing import NullTrace, now

DONT_KNOW = (
    "Je ne sais pas : je n'ai trouvé aucune information fiable sur ce point dans les rappels, "
    "plaintes et documents NHTSA indexés."
)

SYSTEM_PROMPT = f"""You are an assistant for vehicle safety data from NHTSA (US recalls, consumer complaints,
recall reports) covering Volkswagen vehicles. You know nothing yourself: use only the passages and tool results.

Passages retrieved for the question are given with it: use them for explanations (defect, cause, remedy).
Passages are a few examples, never statistics: for any count, list or ranking, call query_sql.
Tools:
- query_sql for numbers: counts, lists, rankings, dates, trends ("combien", "quels modèles", "le plus de").
- search_docs for explanations: defect, cause, consequence, remedy of a recall ("pourquoi", "comment", "quel remède").
  Write the search query in English with the key terms (e.g. "Golf 2018 brake caliper recall").
- You may call both, or call a tool again with a better query or a fixed SQL query.

query_sql runs DuckDB SQL on these tables:
{SCHEMA_DOC}
Examples:
  -- engine complaints filed in 2025
  SELECT count(DISTINCT c.odi_number) AS complaints FROM fct_complaints c
  JOIN bridge_complaint_component b ON b.odi_number = c.odi_number AND b.vehicle_key = c.vehicle_key
  WHERE b.category = 'ENGINE' AND year(c.complaint_filed_date) = 2025
  -- models with the most fire complaints (always select the count you rank on)
  SELECT model, count(DISTINCT odi_number) AS complaints FROM fct_complaints WHERE fire GROUP BY model ORDER BY complaints DESC LIMIT 5
  -- brake recalls of the 2018 Golf
  SELECT DISTINCT campaign_number, component, summary FROM fct_recalls WHERE model = 'GOLF' AND model_year = 2018 AND category = 'BRAKES'

Step 2 - answer, in the user's language (usually French), concisely, using only the tool results:
- cite document passages with their ids in square brackets, e.g. [S2];
- for numbers from SQL, say they come from the database ("d'après la base : ...").
Only if the tool results are empty or unrelated, answer: "{DONT_KNOW}"
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Search NHTSA recall descriptions and recall PDF reports. Returns numbered passages [S#] to cite.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query, in English."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_sql",
            "description": "Run one read-only DuckDB SELECT over the gold tables (fct_recalls, fct_complaints, "
            "bridge_complaint_component, dim_vehicle, dim_component). Returns rows.",
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "A single SELECT statement."}},
                "required": ["sql"],
            },
        },
    },
]

SQL_PROMPT = (
    "This question asks for numbers, a list or a ranking: the passages are only examples. "
    "Call query_sql on the gold tables to answer it."
)

_QUANTITATIVE = re.compile(
    r"\b(combien|nombre|total|le plus|la plus|les plus|le moins|classement|top|pourcentage|moyenne|"
    r"how many|number of|most|fewest|ranking|count|average)\b",
    re.IGNORECASE,
)


def is_quantitative(question: str) -> bool:
    return bool(_QUANTITATIVE.search(question))


RECONSIDER_PROMPT = (
    "The passages above were selected as relevant to the question. Read them again: if one of them "
    "answers it, even partially, answer from it with citations [S#]. Only if none is related, reply "
    "with the refusal sentence."
)

RETRY_PROMPT = (
    "The last tool call did not work ({failure}). Do not give up yet: fix the SQL (check the column names in "
    "the schema; fct_complaints has no category column, join bridge_complaint_component on odi_number), "
    "or rephrase the search in English, or use the other tool."
)

SearchFn = Callable[[str], RetrievalResult]
SqlFn = Callable[[str], SqlResult]


@dataclass
class Source:
    id: str
    chunk_id: str
    doc_id: str
    title: str
    page: int | None
    url: str | None
    campaign_number: str | None
    score: float
    excerpt: str


@dataclass
class ToolCall:
    tool: str
    arguments: dict[str, Any]
    summary: str
    duration_ms: int


@dataclass
class Answer:
    question: str
    answer: str
    sources: list[Source] = field(default_factory=list)  # cited sources (all retrieved ones if none cited)
    sql: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    dont_know: bool = False
    cited: bool = False
    unsupported_numbers: list[str] = field(default_factory=list)  # numbers in the answer found in no tool output
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    context: list[str] = field(default_factory=list)  # tool outputs seen by the LLM (faithfulness eval)
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CITATION = re.compile(r"\[S(\d+)\]")


class Agent:
    def __init__(self, llm: ChatModel, search: SearchFn, sql: SqlFn, max_steps: int = 4, passage_chars: int = 800):
        self.llm = llm
        self.search = search
        self.sql = sql
        self.max_steps = max_steps
        self.passage_chars = passage_chars

    # --- tools ---------------------------------------------------------------

    def _search_docs(self, query: str, state: dict[str, Any]) -> tuple[str, str]:
        result = self.search(query)
        if not result.relevant or not result.hits:
            state["failure"] = "search_docs found no relevant passage"
            return "NO_RELEVANT_DOCUMENTS: nothing in the index answers this query.", "0 relevant passage"
        blocks = []
        for hit in result.hits:
            source = state["by_chunk"].get(hit.chunk_id)
            if source is None:
                source = _source(f"S{len(state['sources']) + 1}", hit)
                state["sources"].append(source)
                state["by_chunk"][hit.chunk_id] = source
            page = f", page {hit.page_start}" if hit.page_start else ""
            blocks.append(f"[{source.id}] {hit.title}{page}\n{hit.content[: self.passage_chars]}")
        state["evidence"] = state["passages"] = True
        return "\n\n".join(blocks), f"{len(result.hits)} passages"

    def _query_sql(self, sql: str, state: dict[str, Any]) -> tuple[str, str]:
        result = self.sql(sql)
        state["sql"].append(asdict(result))
        if result.error:
            state["failure"] = f"query_sql failed: {result.error}"
            return result.as_text(), f"error: {result.error[:80]}"
        if not result.rows:
            state["failure"] = "query_sql returned no rows"
            # Usually a wrong filter (e.g. component_group instead of category): ask for a retry, not evidence.
            return (
                "No rows. Check the filters (UPPERCASE values, use `category` for topics) and retry, "
                "or use search_docs.",
                "0 rows",
            )
        state["evidence"] = True  # includes aggregates equal to 0 ("0 recalls")
        return result.as_text(), f"{len(result.rows)} rows"

    def _run_tool(self, name: str, args: dict[str, Any], state: dict[str, Any]) -> str:
        start, started = time.perf_counter(), now()
        state["failure"] = None
        if isinstance(args, str):  # some models send JSON as a string
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if name == "search_docs" and args.get("query"):
            text, summary = self._search_docs(str(args["query"]), state)
        elif name == "query_sql" and args.get("sql"):
            text, summary = self._query_sql(str(args["sql"]), state)
        else:
            text, summary = f"Unknown tool or missing argument: {name}({args})", "invalid call"
            state["failure"] = text
        state["outputs"].append(text)
        state["calls"].append(ToolCall(name, args, summary, int((time.perf_counter() - start) * 1000)))
        state["trace"].tool(name=name, arguments=args, output=text, summary=summary, start=started, end=now())
        return text

    # --- loop ----------------------------------------------------------------

    def _prefetch(self, question: str, state: dict[str, Any]) -> str:
        """Retrieve passages for the raw question before the first LLM call: small local models
        often skip the search tool, so the documents are always in context."""
        start, started = time.perf_counter(), now()
        text, summary = self._search_docs(question, state)
        args = {"query": question, "auto": True}
        state["calls"].append(ToolCall("search_docs", args, summary, int((time.perf_counter() - start) * 1000)))
        state["outputs"].append(text)
        state["trace"].tool(name="search_docs", arguments=args, output=text, summary=summary, start=started, end=now())
        state["failure"] = None  # a failed prefetch must not trigger the retry prompt
        if text.startswith("NO_RELEVANT_DOCUMENTS"):
            return f"{question}\n\n(No relevant passage was found in the documents for this question.)"
        return f"{question}\n\nPassages retrieved for this question:\n\n{text}"

    def ask(self, question: str, trace: NullTrace | None = None) -> Answer:
        start = time.perf_counter()
        trace = trace or NullTrace()
        state: dict[str, Any] = {"sources": [], "by_chunk": {}, "sql": [], "calls": [], "evidence": False,
                                 "failure": None, "outputs": [], "trace": trace, "passages": False}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._prefetch(question, state)},
        ]
        answer = Answer(question=question, answer="")
        final = ""
        retried = asked_sql = reconsidered = False
        for step in range(self.max_steps + 1):
            last = step == self.max_steps  # last turn: no tools, the model must conclude
            started = now()
            reply = self.llm.chat(messages, tools=None if last else TOOLS)
            trace.generation(
                model=getattr(self.llm, "model", "?"), messages=list(messages),
                output={"content": reply.content, "tool_calls": reply.tool_calls}, start=started, end=now(),
                prompt_tokens=reply.prompt_tokens, completion_tokens=reply.completion_tokens,
            )
            answer.llm_calls += 1
            answer.prompt_tokens += reply.prompt_tokens
            answer.completion_tokens += reply.completion_tokens

            calls = reply.tool_calls
            gave_up = not state["evidence"] or _is_dont_know(reply.content)
            if not calls and gave_up and state["failure"] and not retried and not last:
                # Small models give up after one failed call: one explicit retry with the error.
                retried = True
                messages.append({"role": "assistant", "content": reply.content})
                messages.append({"role": "user", "content": RETRY_PROMPT.format(failure=state["failure"])})
                continue
            if not calls and not last and not state["sql"] and not asked_sql and is_quantitative(question):
                # A count / ranking cannot come from a handful of passages: require the database.
                asked_sql = True
                messages.append({"role": "assistant", "content": reply.content})
                messages.append({"role": "user", "content": SQL_PROMPT})
                continue
            if (not calls and not last and not reconsidered and _is_dont_know(reply.content)
                    and state["passages"] and not is_quantitative(question)):
                # Small models refuse too often although a relevant passage (above the rerank
                # threshold) was given: one invitation to re-read them; refusing stays allowed.
                reconsidered = True
                messages.append({"role": "assistant", "content": reply.content})
                messages.append({"role": "user", "content": RECONSIDER_PROMPT})
                continue
            if not calls:
                final = reply.content
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content,
                    "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls],
                }
            )
            for c in calls:
                messages.append({"role": "tool", "content": self._run_tool(c["name"], c["arguments"], state)})

        answer.tool_calls = state["calls"]
        answer.sql = state["sql"]
        sql_answered = any(q["rows"] and not q["error"] for q in state["sql"])
        # A number / ranking without a successful query would come from a few passages: refuse instead.
        unbacked_count = is_quantitative(question) and not sql_answered
        if not state["evidence"] or not final or _is_dont_know(final) or unbacked_count:
            answer.answer, answer.dont_know = DONT_KNOW, True
            answer.sources = []
        else:
            known = {s.id: s for s in state["sources"]}
            cited_ids = [f"S{n}" for n in dict.fromkeys(CITATION.findall(final))]
            cited = [known[i] for i in cited_ids if i in known]
            final = CITATION.sub(lambda m: m.group(0) if f"S{m.group(1)}" in known else "", final)
            answer.answer = _drop_dont_know_tail(final)
            answer.cited = bool(cited)
            # Uncited answer: show the passages only if the answer did not come from SQL.
            answer.sources = cited or ([] if sql_answered else state["sources"])
            answer.unsupported_numbers = unsupported_numbers(answer.answer, question, state["outputs"])
        answer.latency_ms = int((time.perf_counter() - start) * 1000)
        answer.context = state["outputs"]
        answer.trace_id = trace.id
        trace.end(output=answer.answer, metadata={
            "dont_know": answer.dont_know, "cited": answer.cited, "latency_ms": answer.latency_ms,
            "unsupported_numbers": answer.unsupported_numbers, "llm_calls": answer.llm_calls,
        })
        return answer


def _is_dont_know(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("je ne sais pas") or t.startswith("i don't know")


def _drop_dont_know_tail(text: str) -> str:
    """A real answer followed by the refusal sentence (small models do that): keep the answer."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if not _is_dont_know(p)]
    return "\n\n".join(paragraphs).strip()


def _source(source_id: str, hit: Hit) -> Source:
    meta = hit.metadata or {}
    return Source(
        id=source_id,
        chunk_id=hit.chunk_id,
        doc_id=hit.doc_id,
        title=hit.title,
        page=hit.page_start,
        url=meta.get("source_url"),
        campaign_number=meta.get("campaign_number"),
        score=round(float(hit.score), 4),
        excerpt=hit.content[:400],
    )


_NUMBER = re.compile(r"(?<![\w.,])(?:\d{1,3}(?:[  .,]\d{3})+|\d+(?:[.,]\d+)?)(?![\w])")


def _normalise(n: str) -> str:
    return re.sub(r"[\s.,]", "", n)


def unsupported_numbers(answer: str, question: str, outputs: list[str]) -> list[str]:
    """Numbers stated in the answer that appear neither in the question nor in any tool output:
    a cheap hallucination signal (also used by the phase 5 faithfulness eval)."""
    text = CITATION.sub("", answer)
    known = {_normalise(n) for src in [question, *outputs] for n in _NUMBER.findall(src)}
    found = dict.fromkeys(_normalise(n) for n in _NUMBER.findall(text))
    return [n for n in found if n and n not in known]
