"""Render a metric's vetted SQL with validated parameters.

Parameters are validated by type (int range, ISO date, a known app name, a
short text, an enum value) and inserted as properly escaped SQL literals by
sqlglot. The rendered SQL then goes through the same guard as any other query.
"""

from __future__ import annotations

import re
from datetime import date

from sqlglot import exp

from .catalog import Catalog, MetricDoc

_PARAM = re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)\b")
_BLOCK = re.compile(r"\[\[(.*?)\]\]", re.S)


class MetricError(ValueError):
    pass


def resolve_params(metric: MetricDoc, given: dict, catalog: Catalog) -> dict:
    """Validate and complete parameters (defaults, HOME app). Raises MetricError."""
    out: dict = {}
    for p in metric.params:
        value = given.get(p.name)
        no_filter = p.name in given and isinstance(value, str) \
            and value.strip().lower() in {"", "all", "any", "none", "null", "*"}
        if no_filter:
            value = None                         # explicit planner shorthand: do not apply a default
        # Local planning models commonly emit the symbolic HOME value. Resolve
        # it when explicitly supplied, as well as when it appears as a default.
        if p.type == "app" and isinstance(value, str) and value.upper() == "HOME":
            value = catalog.home_app or None
        if value in (None, "", []) and not no_filter:
            value = p.default
            if value == "HOME":
                value = catalog.home_app or None
        if value is None:
            continue
        if p.type == "int":
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise MetricError(f"{p.name} must be a whole number") from exc
            if p.name == "window_hours" and value not in (24, 48, 72):
                raise MetricError("window_hours must be 24, 48 or 72")
            if p.name == "limit":
                value = max(1, min(value, 500))
            if p.name == "hours" and not 1 <= value <= 24 * 366:
                raise MetricError("hours must be between 1 and 8784")
        elif p.type == "date":
            value = str(value)[:10]
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise MetricError(f"{p.name} must be a date like 2026-09-15") from exc
        elif p.type == "app":
            match = next((a for a in catalog.apps if a.lower() == str(value).lower()), None)
            if match is None and catalog.apps:
                raise MetricError(f"Unknown app '{value}'. Known apps: {', '.join(catalog.apps)}")
            value = match or str(value)
        elif p.type == "enum":
            if str(value) not in p.values:
                raise MetricError(f"{p.name} must be one of: {', '.join(p.values)}")
            value = str(value)
        else:
            value = str(value).strip()[:80]
            if not value:
                continue
        out[p.name] = value
    required_missing = [p.name for p in metric.params
                        if p.name not in out and _required(metric, p.name)]
    if required_missing:
        raise MetricError(f"{metric.title} needs: {', '.join(required_missing)}")
    return out


def _required(metric: MetricDoc, name: str) -> bool:
    """A parameter used outside any [[optional]] block is required."""
    outside = _BLOCK.sub("", metric.sql)
    return f":{name}" in outside or (name == "dimension" and "{{dimension}}" in outside)


def _literal(value) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    return exp.Literal.string(str(value)).sql(dialect="postgres")


def render(metric: MetricDoc, params: dict) -> str:
    sql = metric.sql
    if "{{dimension}}" in sql:
        dim = params.get("dimension")
        if dim not in metric.dimensions:
            raise MetricError(f"Choose a dimension: {', '.join(metric.dimensions)}")
        sql = sql.replace("{{dimension}}", metric.dimensions[dim])

    def keep_block(match: re.Match) -> str:
        names = _PARAM.findall(match.group(1))
        return match.group(1) if all(n in params for n in names) else ""

    sql = _BLOCK.sub(keep_block, sql)

    def fill(match: re.Match) -> str:
        name = match.group(1)
        if name not in params:
            raise MetricError(f"Missing parameter: {name}")
        return _literal(params[name])

    return _PARAM.sub(fill, sql)


def describe_params(metric: MetricDoc, params: dict) -> str:
    bits = []
    if "window_hours" in params:
        bits.append(f"{params['window_hours']}h window")
    if "hours" in params:
        bits.append(f"last {params['hours']} hours of data")
    for key in ("app", "from_app", "to_app"):
        if key in params:
            bits.append({"app": "app", "from_app": "from", "to_app": "to"}[key] + f" {params[key]}")
    if "category" in params:
        bits.append(f"category '{params['category']}'")
    if "dimension" in params:
        bits.append(f"by {params['dimension'].replace('_', ' ')}")
    if "date_from" in params or "date_to" in params:
        bits.append(f"{params.get('date_from', '…')} to {params.get('date_to', '…')}")
    return ", ".join(bits)
