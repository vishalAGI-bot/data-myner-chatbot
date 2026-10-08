"""Terminal chat interface for the isolated journey agent.

    python -m journey_agent.cli [path/to/journeys.json]
"""

import sys

import llm

from .agent import JourneyAgent
from .store import DEFAULT_JSON_PATH, JourneyStore


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_JSON_PATH
    healthy, message = llm.health()
    if not healthy:
        print(f"Note: {message}\nBuilt-in questions still work; unusual ones need the model.")
    agent = JourneyAgent(JourneyStore(path))
    stats = agent.store.summary()
    print(f"Journey agent ready: {stats['journeys']} journeys, {stats['users']} users, "
          f"{stats['events']} searches. Type 'quit' to exit, 'debug' to toggle details.")
    history: list[dict] = []
    debug = False
    while True:
        try:
            question = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.lower() in {"quit", "exit"}:
            break
        if question.lower() == "debug":
            debug = not debug
            print(f"debug {'on' if debug else 'off'}")
            continue
        if not question:
            continue
        result = agent.ask(question, history)
        print(f"Agent: {result.answer}")
        if debug:
            print(f"  [route={result.route} resolved={result.resolved_question!r} "
                  f"{result.latency_ms}ms]\n  [sql={result.sql}]")
        history += [{"role": "user", "content": question},
                    {"role": "assistant", "content": result.answer}]


if __name__ == "__main__":
    main()
