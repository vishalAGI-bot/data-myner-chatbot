"""Terminal chat.  Run:  python cli.py

Commands:  /debug   show the generated SQL and evidence
           /reset   clear conversation history
           /quit    exit
"""

import sys

import config
import llm
from chatbot import Chatbot


def main():
    healthy, msg = llm.health()
    if not healthy:
        print(f"\n{msg}\n")
        sys.exit(1)
    print(msg)
    print(f"  sql: {config.SQL_MODEL}  answer: {config.ANSWER_MODEL} "
          f" router: {config.ROUTER_MODEL}")
    if config.AGENT_ENABLED:
        print(f"  agent: {config.AGENT_MODEL} (max {config.AGENT_MAX_STEPS} steps)")
    if config.USE_EMBEDDINGS:
        print(f"  embeddings: {config.EMBED_MODEL}")

    print("Loading taxonomy...")
    try:
        bot = Chatbot()
    except FileNotFoundError as exc:
        print(f"\n{exc}")
        sys.exit(1)

    debug = False
    print("Ready. Ask about the taxonomy.  /debug  /reset  /quit\n")

    while True:
        try:
            question = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not question:
            continue
        if question in ("/quit", "/exit", "/q"):
            break
        if question == "/reset":
            bot.reset()
            print("bot > history cleared\n")
            continue
        if question == "/debug":
            debug = not debug
            print(f"bot > debug {'on' if debug else 'off'}\n")
            continue

        try:
            turn = bot.ask(question)
        except Exception as exc:
            print(f"bot > error: {exc}\n")
            continue

        note = f"  ({len(turn.steps)} steps)" if turn.steps else ""
        print(f"bot > {turn.answer}{note}\n")

        if debug:
            print(f"  [path] {turn.path}")
            if turn.sql:
                print(f"  [sql]  {turn.sql}")
            for i, st in enumerate(turn.steps, 1):
                arg = next(iter(st.args.values()), "") if st.args else ""
                flag = " !" if st.error else ""
                print(f"    {i}. {st.tool}{flag}  {str(arg)[:90]}")
                print(f"       thought: {st.thought[:110]}")
            if turn.evidence:
                label = "trace" if turn.path == "agent" else "evidence"
                print(f"  [{label}]\n{turn.evidence}\n")


if __name__ == "__main__":
    main()
