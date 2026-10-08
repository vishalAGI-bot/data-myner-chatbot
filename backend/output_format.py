"""Shared scope guard for the data-analysis chat agents."""

from __future__ import annotations

import re


OUT_OF_SCOPE_MESSAGE = (
    "Sorry, I’m only able to answer questions about the available data and its analysis. "
    "I can’t generate general content, programming code, or project source files."
)

# These patterns cover explicit non-analytics requests. Ambiguous questions still go through the
# normal planner, so legitimate searches such as "users who searched for Python books" are allowed.
_CODE_REQUEST = re.compile(
    r"\b(?:write|generate|create|make|provide|give|show|build|need|want)\b"
    r"[^?\n]{0,80}\b(?:code|script|program|function|algorithm|snippet)\b"
    r"|\b(?:code|script|program|function|algorithm|snippet)\s+(?:for|of|to|that)\b"
    r"|\b(?:python|javascript|typescript|java|c\+\+|c#|golang|rust|php|bash|shell)\b"
    r"[^?\n]{0,40}\b(?:code|script|program|function)\b",
    re.I,
)
_PROJECT_CONTENT = re.compile(
    r"\b(?:show|give|print|dump|reveal|generate|create|write|list|output)\b"
    r"[^?\n]{0,80}\b(?:project(?:[’']s)?|repository|repo|source)\s+"
    r"(?:code|files?|content|structure)\b"
    r"|\b(?:project(?:[’']s)?|repository|repo)\s+(?:source\s+)?(?:code|files?|content)\b",
    re.I,
)
_CREATIVE_CONTENT = re.compile(
    r"\b(?:write|generate|create|compose|tell|give)\b[^?\n]{0,60}"
    r"\b(?:poem|story|essay|joke|email|letter|recipe|song|lyrics|article|blog post)\b",
    re.I,
)
_GENERAL_REQUEST = re.compile(
    r"\b(?:weather forecast|latest news|sports score|stock price|capital of|president of)\b"
    r"|\b(?:what is|explain|teach me)\b[^?\n]{0,50}"
    r"\b(?:python|javascript|programming|quantum physics|machine learning)\b"
    r"|\b(?:translate|summarize)\b[^?\n]{0,80}\b(?:this text|this article|the following)\b",
    re.I,
)
_SEARCH_TERM_CONTEXT = re.compile(
    r"\b(?:searched?|searches|searching)\s+for\b|\b(?:keyword|query\s+text|search\s+term)\b"
    r"|\b(?:journeys?|searches|queries)\s+(?:containing|mentioning|matching)\b",
    re.I,
)


def is_explicitly_out_of_scope(question: str) -> bool:
    """Return true for clear content-generation or unrelated general requests."""
    text = (question or "").strip()
    non_code_match = any(pattern.search(text) for pattern in (
        _PROJECT_CONTENT, _CREATIVE_CONTENT, _GENERAL_REQUEST,
    ))
    code_match = bool(_CODE_REQUEST.search(text)) and not _SEARCH_TERM_CONTEXT.search(text)
    return non_code_match or code_match
