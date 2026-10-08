"""Conversation memory: what earlier turns in this chat mean for the current question.

A chat is one session, so "which of those is growing?" only makes sense next to the answer before
it. Two things were missing for that: the model saw earlier turns as 300 characters of rendered
prose (a heading and two table rows, never the columns, the metric or the values), and Deep mode
dropped the previous answer's parameters altogether.

This module keeps a small structured record of recent turns - the question, how it was answered, and
the first rows of the result - and renders it for a prompt. :func:`needs_context` says whether the
new message leans on that record at all, so a self-contained question costs nothing extra.

The record travels in the ``state`` the API already round-trips through the client, so it survives
across requests without the server holding per-session state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MEMORY_TURNS = 3           # how many earlier turns to carry
MEMORY_ROWS = 5            # rows of each earlier result the model gets to see
MEMORY_ENTITIES = 12       # first-column values kept for "the second one" / "those apps"

# Words that point at something earlier instead of naming it. "this week" and "that day" are
# periods, not references, so the time nouns are excluded.
_TIME_NOUN = r"(?!\s*(?:week|month|year|day|date|quarter|period|time|morning|evening|weekend)s?\b)"
_REFERRING = re.compile(
    rf"\b(?:this|that|these|those)\b{_TIME_NOUN}"
    r"|\b(?:it|its|they|them|their|theirs|he|she|him|her|his|hers)\b"
    r"|\b(?:same|above|below|previous|earlier|prior|former|latter|aforementioned)\b"
    r"|\b(?:the\s+)?(?:first|second|third|fourth|fifth|last|top|bottom)\s+"
    r"(?:one|row|app|apps|user|users|item|items|entry|entries|result|results|category|categories)\b"
    r"|\bboth\b|\bothers?\b|\brest\b|\beach\s+of\s+them\b"
    # "the result", "that answer", "this query": a noun standing in for the previous answer.
    r"|\b(?:the|that|this)\s+(?:result|results|answer|answers|output|query|number|numbers|figure|"
    r"figures|total|totals|count|list|table|chart|breakdown|analysis)\b",
    re.I)
# A message that only restates a setting ("with a 24 hour window", "for Flipkart", "by city") is a
# change to the previous question, not a question of its own: it has no verb and no measure.
_SETTING_ONLY = re.compile(
    r"^\s*(?:what\s+(?:is|are|about)\s+)?(?:the\s+)?(?:result|results|answer|output|same)?\s*"
    r"(?:with|for|in|on|by|at|using|within|over)\b", re.I)
# A message that continues the previous one rather than starting fresh.
_CONTINUATION = re.compile(
    r"^\s*(?:and|also|plus|but|then|now|ok(?:ay)?|so|what\s+about|how\s+about|w?hat\s+of|"
    r"instead|only|just|again|more|why|why\s+not|how\s+come|which\s+one|same|compare\s+(?:it|them))\b",
    re.I)
_WORD = re.compile(r"[A-Za-z0-9']+")


def needs_context(question: str, has_memory: bool) -> bool:
    """Whether the new message cannot be read on its own.

    Erring towards True costs one small model call; erring towards False silently answers the wrong
    question, so short messages and bare continuations count even without an explicit pronoun.
    """
    if not has_memory:
        return False
    text = (question or "").strip()
    if not text:
        return False
    if _CONTINUATION.search(text) or _REFERRING.search(text) or _SETTING_ONLY.search(text):
        return True
    return len(_WORD.findall(text)) <= 5          # "by city?", "flipkart too"


def _clip(value, limit: int = 60) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Memory:
    """The last few question/answer pairs, structured enough to resolve a reference against."""

    turns: list[dict] = field(default_factory=list)

    @classmethod
    def from_state(cls, state: dict | None) -> "Memory":
        raw = (state or {}).get("recent")
        turns = [t for t in raw if isinstance(t, dict)][-MEMORY_TURNS:] if isinstance(raw, list) else []
        return cls(turns)

    def __bool__(self) -> bool:
        return bool(self.turns)

    @property
    def last(self) -> dict | None:
        return self.turns[-1] if self.turns else None

    def remember(self, question: str, result) -> None:
        """Record one completed turn. Only what a later question could refer back to is kept."""
        table = result.table or {}
        columns = [str(c) for c in (table.get("columns") or [])][:12]
        rows = [[_clip(v, 40) for v in row[:12]] for row in (table.get("rows") or [])[:MEMORY_ROWS]]
        entities = []
        if columns and (table.get("rows") or []):
            entities = [_clip(row[0], 40) for row in table["rows"][:MEMORY_ENTITIES] if row]
        turn = {
            "question": _clip(question, 300),
            "understood_as": _clip(result.resolved, 120),
            "route": result.route,
            "metric": result.metric,
            "params": {k: v for k, v in (result.params or {}).items() if not isinstance(v, (dict, list))},
            "tables": list(result.tables or [])[:6],
            "sql": _clip(result.sql, 1800) if result.sql else "",
            "columns": columns,
            "rows": rows,
            "total_rows": int(table.get("total_rows") or len(table.get("rows") or [])),
            "entities": entities,
            "answered": bool(table.get("rows")),
        }
        self.turns = (self.turns + [turn])[-MEMORY_TURNS:]

    def to_state(self) -> dict:
        return {"recent": self.turns}

    def prompt(self, limit: int = MEMORY_TURNS) -> str:
        """Earlier turns as the model should see them: the question, the analysis, and real rows."""
        if not self.turns:
            return ""
        blocks = []
        for n, turn in enumerate(self.turns[-limit:], 1):
            lines = [f"[{n}] The user asked: {turn.get('question', '')}"]
            if turn.get("understood_as"):
                detail = turn["understood_as"]
                if turn.get("metric"):
                    detail += f" (vetted analysis '{turn['metric']}')"
                lines.append(f"    Answered with: {detail}")
            if turn.get("params"):
                lines.append("    Settings: " + ", ".join(f"{k}={v}" for k, v in turn["params"].items()))
            if turn.get("sql"):
                lines.append("    Query used:\n      " + turn["sql"].replace("\n", "\n      "))
            if turn.get("columns"):
                lines.append("    Result columns: " + ", ".join(turn["columns"]))
                for row in turn.get("rows") or []:
                    lines.append("      " + " | ".join(row))
                total = turn.get("total_rows") or 0
                if total > len(turn.get("rows") or []):
                    lines.append(f"      ... {total} rows in total")
            elif not turn.get("answered"):
                lines.append("    That question was not answered from the data.")
            blocks.append("\n".join(lines))
        return "Earlier in this conversation (most recent last):\n" + "\n".join(blocks) + "\n\n"

    def ordinal_entity(self, question: str) -> str | None:
        """"the second app" -> that value from the previous result, so a rank needs no model call."""
        last = self.last
        if not last or not last.get("entities"):
            return None
        match = re.search(r"\b(?:the\s+)?(first|second|third|fourth|fifth|last)\b", question, re.I)
        if not match:
            return None
        word = match.group(1).lower()
        entities = last["entities"]
        index = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}.get(word)
        if word == "last":
            index = len(entities) - 1
        return entities[index] if index is not None and 0 <= index < len(entities) else None


RESOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "used_context": {"type": "boolean"},
    },
    "required": ["question"],
}

RESOLVE_SYSTEM = """Rewrite the user's new message as ONE standalone question about shopping-app data.

The earlier conversation is given only so you can fill in what the new message leaves out: which app,
which measure, which period, which category, or what a pronoun such as "it", "them", "those" or "the
second one" points to. Replace those references with the actual names and values from the earlier
turns.

Rules:
- Keep the user's intent exactly. Never add a measure, filter, breakdown or period they did not ask
  for, and never narrow one they left open.
- Do not answer the question, explain it, or mention the conversation.
- When the new message already stands on its own, return it unchanged with used_context false.
- When it asks for the same analysis on a different entity ("what about Flipkart?"), repeat the
  earlier question with the new entity substituted.
- When it asks for more detail about a named row ("why is Flipkart so high?"), keep that name.

Reply with JSON: {"question": "<standalone question>", "used_context": true|false}"""
