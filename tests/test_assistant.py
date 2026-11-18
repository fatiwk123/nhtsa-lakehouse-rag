import duckdb
import pytest

from rag.agent import DONT_KNOW, Agent
from rag.llm import LlmReply
from rag.retrieval import RetrievalResult, rrf_fuse
from rag.search import Hit
from rag.sql_tool import SqlResult, run_sql


def hit(chunk_id: str, score: float = 0.0) -> Hit:
    return Hit(chunk_id, f"doc:{chunk_id}", f"Recall {chunk_id}", f"text {chunk_id}", 1, {"source_url": "u"}, score)


def test_rrf_rewards_agreement_between_legs():
    dense = [hit("a"), hit("b"), hit("c")]
    keyword = [hit("c"), hit("d"), hit("b")]
    fused = rrf_fuse([dense, keyword])
    assert [h.chunk_id for h in fused][:2] == ["c", "b"]  # in both lists beats rank 1 in one
    assert {h.chunk_id for h in fused} == {"a", "b", "c", "d"}
    assert fused[0].score == pytest.approx(1 / 63 + 1 / 61)


# --- SQL guard ----------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "t.duckdb")
    conn = duckdb.connect(path)
    conn.execute("CREATE SCHEMA gold; CREATE SCHEMA silver")
    conn.execute("CREATE TABLE gold.fct_recalls AS SELECT * FROM (VALUES ('18V369000', 'GOLF', 2018), ('19V188000', 'JETTA', 2019)) t(campaign_number, model, model_year)")
    conn.execute("CREATE TABLE silver.secret AS SELECT 1 AS x")
    conn.close()
    return path


def test_sql_allows_select_on_gold(db):
    r = run_sql("WITH g AS (SELECT * FROM fct_recalls) SELECT model, count(*) n FROM g GROUP BY 1 ORDER BY 1;", db)
    assert r.error is None and r.rows == [["GOLF", 1], ["JETTA", 1]]


def test_sql_caps_rows(db):
    r = run_sql("SELECT * FROM gold.fct_recalls", db, max_rows=1)
    assert len(r.rows) == 1 and r.truncated


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM gold.fct_recalls",
        "DROP TABLE gold.fct_recalls",
        "SELECT * FROM silver.secret",
        "SELECT 1; DROP TABLE gold.fct_recalls",
        "SELECT * FROM read_csv('/etc/passwd')",
        "ATTACH 'x.duckdb' AS x",
        "COPY gold.fct_recalls TO 'out.csv'",
    ],
)
def test_sql_rejects_writes_and_other_tables(db, sql):
    assert run_sql(sql, db).error


# --- Agent loop (scripted LLM) ---------------------------------------------------

class ScriptedLlm:
    model = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(list(messages))
        return self.replies.pop(0)


def search_ok(_q):
    return RetrievalResult([hit("x", 3.2), hit("y", 1.0)], "hybrid_rerank", relevant=True)


def search_nothing(_q):
    return RetrievalResult([hit("x", -8.0)], "hybrid_rerank", relevant=False)


def no_sql(_q):
    raise AssertionError("SQL should not be called")


def test_agent_cites_and_drops_unknown_citations():
    llm = ScriptedLlm([
        LlmReply("", [{"name": "search_docs", "arguments": {"query": "brake"}}]),
        LlmReply("Le défaut vient de l'étrier [S1] [S9]."),
    ])
    ans = Agent(llm, search_ok, no_sql).ask("frein ?")
    assert not ans.dont_know and ans.cited
    assert [s.id for s in ans.sources] == ["S1"]
    assert "[S9]" not in ans.answer and "[S1]" in ans.answer


def test_agent_prefetches_passages_for_the_question():
    llm = ScriptedLlm([LlmReply("Réponse [S2].")])
    ans = Agent(llm, search_ok, no_sql).ask("Golf ?")
    assert ans.tool_calls[0].tool == "search_docs" and ans.tool_calls[0].arguments["auto"]
    assert "[S1]" in llm.calls[0][1]["content"] and ans.answer == "Réponse [S2]." and ans.llm_calls == 1


def test_agent_says_dont_know_without_evidence_even_if_llm_answers():
    llm = ScriptedLlm([
        LlmReply("", [{"name": "search_docs", "arguments": {"query": "renault clio"}}]),
        LlmReply("La Clio a un rappel airbag."),  # hallucination: no evidence returned
        LlmReply("La Clio a un rappel airbag."),  # still no evidence after the retry
    ])
    ans = Agent(llm, search_nothing, no_sql).ask("Rappels Renault Clio ?")
    assert ans.dont_know and ans.answer == DONT_KNOW and ans.sources == []


def test_agent_sql_path():
    def sql(q):
        return SqlResult(sql=q, columns=["n"], rows=[[52]])

    llm = ScriptedLlm([
        LlmReply("", [{"name": "query_sql", "arguments": {"sql": "SELECT 52 AS n"}}]),
        LlmReply("D'après la base : 52 plaintes."),
    ])
    ans = Agent(llm, search_ok, sql).ask("Combien ?")
    assert not ans.dont_know and ans.sql[0]["rows"] == [[52]] and ans.answer.endswith("52 plaintes.")


def test_agent_stops_after_max_steps():
    loop = [LlmReply("", [{"name": "search_docs", "arguments": {"query": "q"}}])] * 2
    llm = ScriptedLlm(loop + [LlmReply("Fin [S1].")])
    ans = Agent(llm, search_ok, no_sql, max_steps=2).ask("q")
    assert ans.llm_calls == 3 and ans.answer == "Fin [S1]."


def test_text_tool_calls_are_recovered():
    from rag.llm import parse_text_tool_calls

    calls, content = parse_text_tool_calls('<tool_call>\n{"name": "query_sql", "arguments": {"sql": "SELECT 1"}}\n</tool_call>')
    assert calls == [{"name": "query_sql", "arguments": {"sql": "SELECT 1"}}] and content == ""
    assert parse_text_tool_calls("Réponse normale [S1].") == ([], "Réponse normale [S1].")


def test_nested_json_tool_call_is_recovered():
    from rag.llm import parse_text_tool_calls

    text = 'Je vais chercher.\n<tool_call>\n{"name": "search_docs", "arguments": {"query": "brake {caliper}"}}'
    assert parse_text_tool_calls(text)[0] == [{"name": "search_docs", "arguments": {"query": "brake {caliper}"}}]


def test_empty_sql_result_is_not_evidence():
    def sql(q):
        return SqlResult(sql=q, columns=["component"], rows=[])

    llm = ScriptedLlm([
        LlmReply("", [{"name": "query_sql", "arguments": {"sql": "SELECT component FROM fct_recalls WHERE 1=0"}}]),
        LlmReply("Il n'y a aucun rappel."),
        LlmReply("Il n'y a aucun rappel."),
    ])
    assert Agent(llm, search_nothing, sql).ask("q").dont_know


def test_refusal_appended_to_a_real_answer_is_dropped():
    llm = ScriptedLlm([
        LlmReply("", [{"name": "search_docs", "arguments": {"query": "q"}}]),
        LlmReply(f"Le Tiguan [S1].\n\n{DONT_KNOW}"),
    ])
    ans = Agent(llm, search_ok, no_sql).ask("q")
    assert ans.answer == "Le Tiguan [S1]." and not ans.dont_know


def test_agent_retries_once_after_a_failed_tool():
    calls = []

    def sql(q):
        calls.append(q)
        if "category" in q:
            return SqlResult(sql=q, error='Binder Error: column "category" not found')
        return SqlResult(sql=q, columns=["n"], rows=[[52]])

    llm = ScriptedLlm([
        LlmReply("", [{"name": "query_sql", "arguments": {"sql": "SELECT count(*) FROM fct_complaints WHERE category='ENGINE'"}}]),
        LlmReply(DONT_KNOW),  # gives up
        LlmReply("", [{"name": "query_sql", "arguments": {"sql": "SELECT 52 AS n"}}]),
        LlmReply("D'après la base : 52 plaintes."),
    ])
    ans = Agent(llm, search_ok, sql).ask("Combien de plaintes moteur ?")
    assert len(calls) == 2 and not ans.dont_know and "52" in ans.answer
    assert "did not work" in llm.calls[2][-1]["content"]


def test_unsupported_numbers_flags_invented_counts():
    from rag.agent import unsupported_numbers

    outputs = ["model | n\nTIGUAN | 5\nJETTA | 3"]
    assert unsupported_numbers("TIGUAN (5 plaintes), JETTA (3) [S12]", "modèles 2025 ?", outputs) == []
    assert unsupported_numbers("TIGUAN (5), E-GOLF (2 plaintes) en 2025", "modèles 2025 ?", outputs) == ["2"]
    assert unsupported_numbers("5, 3 et 1 200 plaintes", "", ["5 3 1200"]) == []


def test_quantitative_question_requires_sql():
    from rag.agent import SQL_PROMPT, is_quantitative

    assert is_quantitative("Combien de plaintes moteur en 2025 ?")
    assert is_quantitative("Quels modèles ont le plus de plaintes ?")
    assert not is_quantitative("Pourquoi les Golf 2018 ont-elles été rappelées ?")

    def sql(q):
        return SqlResult(sql=q, columns=["n"], rows=[[52]])

    llm = ScriptedLlm([
        LlmReply("Aucune plainte moteur [S1]."),  # answers from the passages
        LlmReply("", [{"name": "query_sql", "arguments": {"sql": "SELECT 52 AS n"}}]),
        LlmReply("D'après la base : 52 plaintes."),
    ])
    ans = Agent(llm, search_ok, sql).ask("Combien de plaintes moteur en 2025 ?")
    assert llm.calls[1][-1]["content"] == SQL_PROMPT and ans.answer == "D'après la base : 52 plaintes."


def test_sql_answer_does_not_list_unrelated_passages():
    def sql(q):
        return SqlResult(sql=q, columns=["n"], rows=[[52]])

    llm = ScriptedLlm([
        LlmReply("", [{"name": "query_sql", "arguments": {"sql": "SELECT 52 AS n"}}]),
        LlmReply("D'après la base : 52 plaintes."),
    ])
    assert Agent(llm, search_ok, sql).ask("Combien ?").sources == []
