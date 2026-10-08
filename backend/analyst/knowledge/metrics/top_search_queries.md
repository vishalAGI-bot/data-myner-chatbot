---
type: Metric
title: Top search queries
description: Most frequent normalised search queries, optionally for one app.
tags:
- top searches
- top queries
- most searched
- search terms
- keywords
- what do people search
tables:
- cross_app_journey_event
- app_master
runtime: postgresql
parameters:
- name: app
  type: app
- name: limit
  type: int
  default: 20
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Top search queries

Most frequent normalised search queries, optionally for one app.

## Example questions

- top searches on Meesho
- most searched queries

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(ev.normalized_query, ev.query) AS query, COUNT(*) AS searches, COUNT(DISTINCT ev.journey_assoc_id) AS journeys
FROM cross_app_journey_event ev
JOIN app_master a ON a.id = ev.app_assoc_id
WHERE 1 = 1 [[ AND a.app_name = :app ]]
GROUP BY 1
ORDER BY searches DESC, 1
LIMIT :limit
```

