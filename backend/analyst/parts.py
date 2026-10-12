"""A message with several questions: run order and the combined answer.

OpenAI's reading lists every question in the message as a part with a status (answer now, needs the user's
input, not in the data, outside scope) and the earlier parts whose results it needs. Here the answerable parts
are put in run order - independent parts together, a part after the parts it depends on - and the outcome of
every part, answered or not, becomes one numbered section of the answer. No question is silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

from .understanding import MAX_PARTS, Part

# How each outcome reads, in the overview line and on the part's card.
LABELS = {
    "answered": "answered",
    "flagged": "answered with a caveat",
    "no_rows": "no matching data",
    "failed": "could not be answered",
    "clarify": "needs your input",
    "unsupported": "not in the data",
    "out_of_scope": "outside data analysis",
    "blocked": "waiting on another question",
    "not_finished": "not finished in time",
}


@dataclass
class Outcome:
    status: str                       # a key of LABELS
    answer: object | None = None      # the part's AnalystAnswer when it ran
    note: str = ""                    # why it was not answered
    question: str = ""                # the question actually run (a dependent part is rewritten first)


def run_order(parts: list[Part]) -> list[list[Part]]:
    """Waves of answerable parts: each wave only needs results from earlier waves.

    ``depends_on`` only ever names earlier parts (enforced when the reading is parsed), so there are no
    cycles; a part whose prerequisite is not answerable stays in its wave and is reported as blocked."""
    answerable = {p.number: p for p in parts if p.status == "answer"}
    wave_of: dict[int, int] = {}
    for p in sorted(answerable.values(), key=lambda p: p.number):
        deps = [d for d in p.depends_on if d in answerable]
        wave_of[p.number] = 1 + max((wave_of[d] for d in deps), default=-1)
    waves: dict[int, list[Part]] = {}
    for number, wave in wave_of.items():
        waves.setdefault(wave, []).append(answerable[number])
    return [waves[w] for w in sorted(waves)]


def overview(parts: list[Part], outcomes: dict[int, Outcome], more: int) -> str:
    counts: dict[str, int] = {}
    for p in parts:
        status = outcomes[p.number].status
        counts[status] = counts.get(status, 0) + 1
    total = len(parts) + more
    bits = [f"{n} {LABELS[s]}" for s, n in sorted(counts.items(), key=lambda kv: list(LABELS).index(kv[0]))]
    if more:
        bits.append(f"{more} not read")
    return f"**Your message has {total} question{'s' if total != 1 else ''}:** " + ", ".join(bits) + "."


def section(part: Part, outcome: Outcome) -> str:
    head = f"**{part.number}. {part.question}**"
    if outcome.question and outcome.question != part.question:
        head += f"\n_Read as: {outcome.question}_"
    return f"{head}\n{body(part, outcome)}"


def body(part: Part, outcome: Outcome) -> str:
    """What one part says: its answer, or why it has none."""
    answer = getattr(outcome.answer, "answer", "") or ""
    text = {
        "answered": answer,
        "flagged": answer,
        "no_rows": answer or "The query ran, but no rows matched.",
        "failed": f"Not answered: {answer.strip() or outcome.note or 'no result'}",
        "clarify": "Needs your input - see “Clarification needed” below.",
        "unsupported": f"Not in the data: {outcome.note or 'the fields this needs are not recorded'}.",
        "out_of_scope": "Outside data analysis: I only answer questions about this shopping data.",
        "blocked": f"Not answered: {outcome.note}",
        "not_finished": "Not finished: the thinking-time limit was reached first. Allow more time, or ask it "
                        "on its own.",
    }[outcome.status]
    return text.strip()


def more_note(more: int) -> str:
    return (f"**{more} more question{'s' if more != 1 else ''} in your message {'were' if more != 1 else 'was'} "
            f"not read** - at most {MAX_PARTS} are answered per message. Ask {'them' if more != 1 else 'it'} in a "
            "follow-up message.")
