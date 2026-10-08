"""Conversation memory: which user / journey / app is the chat talking about?

The original agent left pronoun resolution ("this user", "of particular user")
to a 7B model reading raw history. It failed exactly there: *"all app name of
this user"* was answered with every app in the file. This module resolves
references deterministically and records how it did so, so the answer can say
"User 5330 (from your earlier message) used ...".

Resolution order for a reference such as "this user":
1. An explicit id in the current message always wins.
2. Otherwise walk back through the conversation, newest first. The first turn
   that mentions user ids decides:
   - exactly one id -> that id;
   - several ids and the message has an ordinal ("top", "second", "last")
     -> the id at that position;
   - several ids and no ordinal -> ambiguous; the agent asks which one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #

_USER_ID = re.compile(
    r"\buser(?:[ \t_]*(?:assoc(?:iate)?[ \t_]*)?id)?[ \t]*(?:no\.?|number|num|#|:|=|is)?[ \t]*(\d{1,12})\b",
    re.I,
)
_USER_ID_LIST = re.compile(       # "users 5330, 4598 and 517"
    r"\busers(?:[ \t_]*ids?)?[ \t]*:?[ \t]*(\d{1,12}(?:[ \t]*(?:,|and|&|or|vs)[ \t]*\d{1,12})+)\b", re.I)
_USER_ID_SUFFIX = re.compile(r"\b(\d{1,12})[ \t]*(?:number[ \t]+)?user\b", re.I)   # "5330 user"
_JOURNEY_ID = re.compile(r"\bj\s?-?(\d{3,})\b", re.I)
_NUMBER = re.compile(r"\d{1,12}")

USER_REFERENCE = re.compile(
    r"\b(?:this|that|same|particular|specific|selected|above|previous|said|"
    r"mentioned|the)\s+(?:user|person|customer|shopper|guy)\b(?!\s*(?:id\s*)?\d)"
    r"|\b(?:this|that)\s+one\b"
    r"|\b(?:he|she|him|her|his|hers)\b"
    r"|\b(?:this|that|same|particular)\s+(?:id|user\s*id)\b",
    re.I,
)
JOURNEY_REFERENCE = re.compile(
    r"\b(?:this|that|same|particular|specific|above|previous|the)\s+journey\b", re.I)
APP_REFERENCE = re.compile(
    r"\b(?:this|that|same|particular|specific|above|previous|the)\s+(?:app|platform|store)\b", re.I)

_ORDINALS = {
    "first": 0, "1st": 0, "top": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3, "fifth": 4, "5th": 4, "sixth": 5, "6th": 5,
    "seventh": 6, "7th": 6, "eighth": 7, "8th": 7, "ninth": 8, "9th": 8,
    "tenth": 9, "10th": 9, "last": -1, "bottom": -1,
}
_ORDINAL = re.compile(
    r"\b(" + "|".join(_ORDINALS) + r")\s+(?:one|user|person|customer|journey|app)\b"
    r"|\b(?:number|no\.?|#)\s*(\d{1,2})\s+(?:user|journey|one)\b",
    re.I,
)

# Extra spellings people use for the app surfaces. Keys are lower-case.
APP_ALIASES = {
    "instamart": "Swiggy Instamart",
    "swiggy instamart": "Swiggy Instamart",
    "big basket": "Bigbasket",
    "bb now": "Bigbasket",
    "flipkart minute": "Flipkart Minutes",
    "fk minutes": "Flipkart Minutes",
    "amazon fresh": "Amazon Now",
    "fk": "Flipkart",
    "amzn": "Amazon",
}


# --------------------------------------------------------------------------- #
# Entity extraction
# --------------------------------------------------------------------------- #

def extract_user_ids(text: str) -> list[int]:
    found: list[tuple[int, int]] = []           # (position, id)
    for match in _USER_ID.finditer(text or ""):
        found.append((match.start(), int(match.group(1))))
    for match in _USER_ID_LIST.finditer(text or ""):
        for number in _NUMBER.finditer(match.group(1)):
            found.append((match.start(1) + number.start(), int(number.group(0))))
    for match in _USER_ID_SUFFIX.finditer(text or ""):
        found.append((match.start(), int(match.group(1))))
    ids: list[int] = []
    for _, value in sorted(found):
        if value not in ids:
            ids.append(value)
    return ids


def extract_journey_ids(text: str) -> list[str]:
    ids: list[str] = []
    for match in _JOURNEY_ID.finditer(text or ""):
        jid = "J" + match.group(1)
        if jid not in ids:
            ids.append(jid)
    return ids


class AppMatcher:
    """Map free text to the exact app names present in the loaded file."""

    def __init__(self, apps: list[str]):
        self.apps = list(apps)
        known = {a.lower(): a for a in self.apps}
        table: dict[str, str] = {}
        for app in self.apps:
            table[app.lower()] = app
            table[app.lower().replace(" ", "")] = app
        for alias, target in APP_ALIASES.items():
            if target in self.apps:
                table.setdefault(alias, target)
        # Longest alias first so "amazon now" is never read as "amazon".
        self._aliases = sorted(table.items(), key=lambda kv: -len(kv[0]))
        self._pattern = re.compile(
            r"\b(" + "|".join(re.escape(a) for a, _ in self._aliases) + r")\b", re.I
        ) if self._aliases else None
        self._single_words = [a for a in known if " " not in a]

    def find(self, text: str) -> list[tuple[str, tuple[int, int]]]:
        """Return (app, span) for each app mentioned, in order of appearance."""
        found: list[tuple[str, tuple[int, int]]] = []
        if not text or self._pattern is None:
            return found
        lookup = dict(self._aliases)
        for match in self._pattern.finditer(text):
            app = lookup[match.group(1).lower()]
            if app not in [f[0] for f in found]:
                found.append((app, match.span()))
        if found:
            return found
        # Typo tolerance ("meesho" -> "mesho", "flipkar"): single words only.
        try:
            from rapidfuzz import fuzz
        except ImportError:
            return found
        for match in re.finditer(r"[a-zA-Z]{5,}", text):
            word = match.group(0).lower()
            for candidate in self._single_words:
                if abs(len(candidate) - len(word)) <= 2 and fuzz.ratio(word, candidate) >= 86:
                    app = lookup[candidate]
                    if app not in [f[0] for f in found]:
                        found.append((app, match.span()))
        return found

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Every mention (not de-duplicated), for masking."""
        if not text or self._pattern is None:
            return []
        return [m.span() for m in self._pattern.finditer(text)] or [s for _, s in self.find(text)]

    def names(self, text: str) -> list[str]:
        return [app for app, _ in self.find(text)]


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

@dataclass
class Resolution:
    user_ids: list[int] = field(default_factory=list)
    journey_ids: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # how references were resolved
    ambiguous: str | None = None                          # clarifying question to ask
    user_from_context: bool = False
    journey_from_context: bool = False
    app_from_context: bool = False


def _ordinal(text: str) -> int | None:
    match = _ORDINAL.search(text or "")
    if not match:
        return None
    if match.group(1):
        return _ORDINALS[match.group(1).lower()]
    return int(match.group(2)) - 1


def _from_history(history: list[dict], extract, ordinal: int | None, label: str):
    """Walk the conversation newest-first and return (value, note, ambiguity)."""
    for distance, turn in enumerate(reversed(history)):
        ids = extract(str(turn.get("content") or ""))
        if not ids or (ordinal is not None and len(ids) == 1):
            continue                    # "the second user" refers to a list, not a single mention
        if turn.get("role") == "user":
            who = "your last message" if distance <= 1 else "your earlier message"
        else:
            who = "the previous answer" if distance == 0 else "an earlier answer"
        if ordinal is not None and len(ids) > 1:
            try:
                value = ids[ordinal]
            except IndexError:
                return None, None, f"The previous answer only listed {len(ids)} {label}s."
            return value, f"{label} {value}, number {ordinal + 1 if ordinal >= 0 else len(ids)} in {who}", None
        if len(ids) == 1:
            return ids[0], f"{label} {ids[0]} from {who}", None
        shown = ", ".join(str(i) for i in ids[:10])
        return None, None, (f"Which {label} do you mean? {who.capitalize()} mentioned "
                            f"{len(ids)}: {shown}. Reply with the {label} number, or say "
                            f"'the top {label}'.")
    return None, None, None


import os

HOME_APP = os.getenv("JOURNEY_HOME_APP", "Amazon").strip()   # who "we / us / our" is
_HOME_REFERENCE = re.compile(r"\b(we|us|our|ours|ourselves)\b", re.I)


def resolve_references(question: str, history: list[dict], apps: AppMatcher,
                       inherit_all: bool = False) -> Resolution:
    """Resolve entity ids for ``question``.

    ``inherit_all`` is set for fragment follow-ups ("of particular user",
    "and for Flipkart?") where every missing entity should come from context.
    """
    res = Resolution(
        user_ids=extract_user_ids(question),
        journey_ids=extract_journey_ids(question),
        apps=apps.names(question),
    )
    ordinal = _ordinal(question)
    if HOME_APP and HOME_APP in apps.apps and _HOME_REFERENCE.search(question) \
            and HOME_APP not in res.apps:
        res.apps.append(HOME_APP)
        res.notes.append(f'"we/our" = {HOME_APP}')

    wants_user = bool(USER_REFERENCE.search(question)) or (
        ordinal is not None and re.search(r"\b(user|person|customer|one)\b", question, re.I))
    if not res.user_ids and (wants_user or inherit_all):
        value, note, ambiguity = _from_history(history, extract_user_ids, ordinal, "user")
        if value is not None:
            res.user_ids = [value]
            res.user_from_context = True
            res.notes.append(note)
        elif wants_user:
            res.ambiguous = ambiguity or ("Which user do you mean? I could not find a user number "
                                          "earlier in this chat. Please include it, e.g. 'user 5330'.")

    wants_journey = bool(JOURNEY_REFERENCE.search(question)) or (
        ordinal is not None and re.search(r"\bjourney\b", question, re.I))
    if not res.journey_ids and (wants_journey or (inherit_all and not res.user_ids)):
        value, note, ambiguity = _from_history(history, extract_journey_ids, ordinal, "journey")
        if value is not None:
            res.journey_ids = [value]
            res.journey_from_context = True
            res.notes.append(note)
        elif wants_journey and not res.ambiguous:
            res.ambiguous = ambiguity or ("Which journey do you mean? Please include its id, "
                                          "e.g. 'J000002'.")

    if not res.apps and APP_REFERENCE.search(question):
        value, note, ambiguity = _from_history(history, apps.names, ordinal, "app")
        if value is not None:
            res.apps = [value]
            res.app_from_context = True
            res.notes.append(note)
        elif ambiguity and not res.ambiguous:
            res.ambiguous = ambiguity
    return res


def previous_user_turns(history: list[dict]) -> list[tuple[int, str]]:
    """(index, text) of earlier user messages, newest first."""
    return [(i, str(t.get("content") or "")) for i, t in reversed(list(enumerate(history)))
            if t.get("role") == "user" and str(t.get("content") or "").strip()]


def substitute_references(question: str, res: Resolution) -> str:
    """Rewrite 'this user' -> 'user 5330' so downstream prompts are standalone."""
    text = question
    if res.user_from_context and res.user_ids:
        text = USER_REFERENCE.sub(f"user {res.user_ids[0]}", text, count=1)
    if res.journey_from_context and res.journey_ids:
        text = JOURNEY_REFERENCE.sub(f"journey {res.journey_ids[0]}", text, count=1)
    if res.app_from_context and res.apps:
        text = APP_REFERENCE.sub(res.apps[0], text, count=1)
    return text


def split_style(message: str) -> tuple[str, str]:
    """Separate the UI's 'Simple meaning' wrapper from the actual question.

    The front end sends ``<instructions> Original question: <question>`` when
    the toggle is on. Returns (question, style).
    """
    text = str(message or "").strip()
    if "Original question:" in text:
        head, question = text.rsplit("Original question:", 1)
        style = "simple" if re.search(r"simple|real[- ]life|story|meaning", head, re.I) else "normal"
        return question.strip(), style
    return text, "normal"
