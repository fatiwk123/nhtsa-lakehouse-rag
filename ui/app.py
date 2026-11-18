"""Streamlit chat UI over the FastAPI assistant.  Run: streamlit run ui/app.py"""

import os

import requests
import streamlit as st

API_URL = os.environ.get("API_URL", "http://localhost:8000")
MODES = {"Hybride + rerank": "hybrid_rerank", "Hybride (RRF)": "hybrid", "Vectoriel seul": "vector"}
EXAMPLES = [
    "Pourquoi les Golf 2018 ont-elles été rappelées pour le freinage ?",
    "Combien de plaintes moteur pour Volkswagen déposées en 2025 ?",
    "Quel est le remède du rappel 18V369000 ?",
    "Quels modèles VW ont le plus de plaintes avec incendie ?",
]

st.set_page_config(page_title="NHTSA RAG", page_icon=":material/directions_car:", layout="wide")

with st.sidebar:
    st.title("NHTSA RAG")
    st.caption("Rappels, plaintes et rapports NHTSA (Volkswagen) - lakehouse Iceberg + pgvector + LLM local (Ollama).")
    mode = MODES[st.radio("Recherche", list(MODES))]
    try:
        health = requests.get(f"{API_URL}/health", timeout=5).json()
        llm = health["llm"]
        st.success(f"API ok - {sum(health['index']['documents'].values())} documents indexés")
        (st.success if llm["available"] else st.warning)(f"LLM {llm['model']}: {'prêt' if llm['available'] else 'indisponible'}")
    except (requests.RequestException, KeyError, ValueError):
        st.error(f"API injoignable ({API_URL})")
    if st.button("Nouvelle conversation", icon=":material/refresh:"):
        st.session_state.history = []

if "history" not in st.session_state:
    st.session_state.history = []


def render_answer(data: dict) -> None:
    st.markdown(data["answer"])
    meta = f"{data['latency_ms'] / 1000:.1f} s - {data['llm_calls']} appel(s) LLM - {data['prompt_tokens'] + data['completion_tokens']} tokens"
    st.caption(meta)
    if data["sources"]:
        with st.expander(f"Sources ({len(data['sources'])})", icon=":material/menu_book:"):
            for s in data["sources"]:
                page = f" - p. {s['page']}" if s.get("page") else ""
                link = f" - [ouvrir]({s['url']})" if s.get("url") else ""
                st.markdown(f"**[{s['id']}]** {s['title']}{page}{link}")
                st.caption(s["excerpt"])
    for q in data["sql"]:
        with st.expander("Requête SQL", icon=":material/database:"):
            st.code(q["sql"], language="sql")
            if q.get("error"):
                st.error(q["error"])
            elif q["rows"]:
                st.dataframe([dict(zip(q["columns"], r)) for r in q["rows"]], hide_index=True)
    if data["tool_calls"]:
        with st.expander("Raisonnement (outils)", icon=":material/build:"):
            for c in data["tool_calls"]:
                st.markdown(f"- `{c['tool']}` {c['arguments']} -> {c['summary']} ({c['duration_ms']} ms)")


for turn in st.session_state.history:
    with st.chat_message("user"):
        st.markdown(turn["question"])
    with st.chat_message("assistant"):
        render_answer(turn["answer"])

if not st.session_state.history:
    st.markdown("#### Exemples")
    cols = st.columns(2)
    for i, ex in enumerate(EXAMPLES):
        if cols[i % 2].button(ex, use_container_width=True):
            st.session_state.pending = ex
            st.rerun()

question = st.chat_input("Pose une question sur les rappels et plaintes NHTSA...") or st.session_state.pop("pending", None)
if question:
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("Recherche et rédaction (LLM local, peut prendre ~1 min)..."):
            try:
                resp = requests.post(f"{API_URL}/ask", json={"question": question, "mode": mode}, timeout=600)
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as exc:
                st.error(f"Erreur API : {exc}")
                st.stop()
        render_answer(data)
    st.session_state.history.append({"question": question, "answer": data})
