"""Standalone Streamlit chat UI for the isolated journey agent.

Run from the upload directory:
    streamlit run journey_agent/app.py
"""

import streamlit as st

import llm

from journey_agent.agent import JourneyAgent


st.set_page_config(page_title="Cross-App Journey Chatbot", page_icon=":mag:")


@st.cache_resource
def get_agent() -> JourneyAgent:
    return JourneyAgent()


st.title("Cross-App Journey Chatbot")
st.caption("Answers only from the aggregated cross-app journey JSON.")

healthy, health_message = llm.health()
if not healthy:
    st.warning(health_message + "\n\nBuilt-in questions still work; unusual ones need the model.")

try:
    agent = get_agent()
except Exception as exc:
    st.error(str(exc))
    st.stop()

summary = agent.store.summary()
with st.sidebar:
    st.subheader("Journey data")
    st.metric("Journeys", summary["journeys"])
    st.metric("Users", summary["users"])
    st.metric("Search events", summary["events"])
    st.metric("Multi-app journeys", summary["cross_app"])
    st.subheader("Thinking")
    depth_label = st.radio(
        "Depth", ["Auto", "Fast", "Deep"], horizontal=True,
        help="Auto thinks deeper only for complex questions. Deep always splits the "
             "question, answers each part from the data and verifies the final answer.")
    max_seconds = st.slider("Maximum thinking time (seconds)", 10, 60, 60, step=5,
                            help="Hard limit. When it is reached you get the best verified "
                                 "answer found so far.")
    if st.button("Clear chat"):
        st.session_state.journey_messages = []
        st.rerun()

if "journey_messages" not in st.session_state:
    st.session_state.journey_messages = []

for message in st.session_state.journey_messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if question := st.chat_input("Ask about users, journeys, apps, searches, or outcomes"):
    st.session_state.journey_messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        history = st.session_state.journey_messages[:-1]   # context before this question
        status = st.status("Thinking...", expanded=depth_label == "Deep")

        def show(event: dict) -> None:
            icon = "" if event.get("ok", True) else "(!) "
            status.write(f"{event['elapsed_ms'] / 1000:.1f}s - {icon}{event['text']}")

        try:
            result = agent.ask(question, history, depth=depth_label.lower(),
                               max_seconds=max_seconds, on_event=show)
            answer = result.answer
            label = (f"Thought for {result.latency_ms / 1000:.1f}s"
                     + (" (stopped at the time limit)" if result.timed_out else "")
                     + (" - verified" if result.verified else ""))
            status.update(label=label, state="error" if result.timed_out else "complete",
                          expanded=False)
        except Exception as exc:
            answer = f"Error: {exc}"
            status.update(label="Failed", state="error")
            result = None
        st.markdown(answer)
        if result is not None and result.sql:
            with st.expander("How this was calculated"):
                st.caption(f"Understood as: {result.resolved_question}")
                st.code(result.sql, language="sql")
    st.session_state.journey_messages.append({"role": "assistant", "content": answer})
