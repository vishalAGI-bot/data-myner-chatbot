"""Check the analyst's data connection and knowledge, then run a few analyses.

    python -m analyst.tools.check                 # uses backend/.env
    python -m analyst.tools.check --question "purchase share by app"
"""

from __future__ import annotations

import argparse
import sys

from analyst.agent import AnalystAgent
from analyst.catalog import Catalog
from analyst import settings_store
from analyst.sources import SourceError, open_source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--question", action="append", help="question to try (repeatable)")
    args = parser.parse_args()
    settings = settings_store.effective()          # .env + values saved from the Settings page
    settings_store.apply_llm(settings_store.load_overrides())
    print(f"Source: {settings.source}" + (f" (SSH {settings.ssh_host})" if settings.source == "postgres" and settings.ssh_host else ""))
    catalog = Catalog(settings.knowledge_dir, settings.home_app)
    try:
        source = open_source(settings, catalog.blocked_tables)
        catalog.attach(source)
    except SourceError as exc:
        print(f"Connection failed: {exc}")
        return 1
    print(f"Connected: {source.describe()}")
    print(f"\nTables available to the chatbot ({len(catalog.allowed_tables)}):")
    for name in sorted(catalog.allowed_tables):
        doc = catalog.tables[name]
        print(f"  {name:32} ~{doc.row_count if doc.row_count is not None else '?':>10} rows  {doc.title}")
    missing = [n for n, d in catalog.tables.items() if not d.available]
    if missing:
        print(f"\nDocumented but missing or empty in this database: {', '.join(missing)}")
    print(f"\nAnalyses ready: {sum(m.available for m in catalog.metrics.values())} of {len(catalog.metrics)}")
    print(f"Apps: {', '.join(catalog.apps) or '(none)'}\nData range: {catalog.date_range[0]} to {catalog.date_range[1]}")
    agent = AnalystAgent(source, catalog, settings)
    for question in args.question or ["purchase share by app", "conversion rate by app within 24h"]:
        result = agent.ask(question, depth="fast")
        print(f"\nQ: {question}\n[{result.route} · {result.metric} · {result.latency_ms} ms]\n{result.answer[:1200]}")
    source.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
