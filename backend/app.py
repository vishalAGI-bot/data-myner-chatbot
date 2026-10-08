"""Streamlit UI.  Run:  streamlit run app.py"""

import streamlit as st

import config
import db
import llm
from chatbot import Chatbot

st.set_page_config(page_title="Taxonomy chatbot", page_icon=":mag:", layout="centered")


@st.cache_resource
def get_bot():
    return Chatbot()


st.title("Taxonomy chatbot")

healthy, msg = llm.health()
if not healthy:
    st.error(msg)
    st.stop()

try:
    bot = get_bot()
except FileNotFoundError as exc:
    st.error(f"{exc}")
    st.stop()

with st.sidebar:
    st.subheader("Data")
    conn = db.connect()
    n_master, n_cat, n_sub = conn.execute(
        f"""SELECT COUNT(DISTINCT master_category), COUNT(DISTINCT category),
                   COUNT(DISTINCT sub_category) FROM {config.TABLE}"""
    ).fetchone()
    st.metric("Master categories", n_master)
    st.metric("Categories", n_cat)
    st.metric("Sub-categories", n_sub)
    st.divider()
    st.subheader("Models")
    st.caption(f"SQL: `{config.SQL_MODEL}`")
    st.caption(f"Answer: `{config.ANSWER_MODEL}`")
    st.caption(f"Router: `{config.ROUTER_MODEL}`")
    st.caption(f"Agent: `{config.AGENT_MODEL}`" if config.AGENT_ENABLED
               else "Agent: off")
    st.caption(f"Lookup: `{config.EMBED_MODEL}`" if config.USE_EMBEDDINGS
               else "Lookup: rapidfuzz (no model)")
    st.divider()
    st.subheader("Agent")
    config.AGENT_ENABLED = st.toggle(
        "Allow multi-step answers", value=config.AGENT_ENABLED
    )
    config.AGENT_MAX_STEPS = st.slider(
        "Max steps", 2, 8, config.AGENT_MAX_STEPS
    )
    st.caption("Multi-part questions loop through tools until done. Slower, "
               "but handles questions one query cannot.")
    st.divider()
    show_debug = st.checkbox("Show SQL and evidence", value=False)
    if st.button("Clear chat"):
        st.session_state.messages = []
        bot.reset()
        st.rerun()

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if show_debug and msg.get("debug"):
            with st.expander("How this was answered"):
                st.caption(f"Path: {msg['debug']['path']}")
                if msg["debug"]["sql"]:
                    st.code(msg["debug"]["sql"], language="sql")
                for i, stp in enumerate(msg["debug"].get("steps") or [], 1):
                    st.markdown(f"**Step {i} - `{stp.tool}`**  \n{stp.thought}")
                st.text(msg["debug"]["evidence"])

if prompt := st.chat_input("How many master categories are there?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                bot.agent.max_steps = config.AGENT_MAX_STEPS
                turn = bot.ask(prompt)
                answer, debug = turn.answer, {
                    "path": turn.path, "sql": turn.sql, "evidence": turn.evidence,
                    "steps": turn.steps, "early": turn.stopped_early,
                }
                if turn.path == "agent":
                    answer += f"\n\n*Answered in {len(turn.steps)} steps"
                    answer += ", step limit reached.*" if turn.stopped_early else ".*"
            except Exception as exc:
                answer, debug = f"Error: {exc}", None
        st.markdown(answer)
        if show_debug and debug:
            with st.expander("How this was answered"):
                st.caption(f"Path: {debug['path']}")
                if debug["sql"]:
                    st.code(debug["sql"], language="sql")
                for i, stp in enumerate(debug.get("steps") or [], 1):
                    st.markdown(f"**Step {i} - `{stp.tool}`**  \n{stp.thought}")
                st.text(debug["evidence"])

    st.session_state.messages.append(
        {"role": "assistant", "content": answer, "debug": debug}
    )
