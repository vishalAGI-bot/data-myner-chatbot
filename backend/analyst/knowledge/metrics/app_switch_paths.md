---
type: Metric
title: App switches inside journeys
description: Consecutive searches in the same journey on different apps (from app -> to app), counted.
tags:
- switch
- switches
- from
- to
- path
- sequence
- amazon to meesho
- go from
- move from
tables:
- cross_app_journey_event
- app_master
runtime: postgresql
parameters:
- name: from_app
  type: app
- name: to_app
  type: app
- name: limit
  type: int
  default: 20
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# App switches inside journeys

Consecutive searches in the same journey on different apps (from app -> to app), counted.

## Example questions

- How often do users go from Amazon to Meesho?
- most common app switches

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
WITH e AS (
  SELECT ev.journey_assoc_id, a.app_name,
         LAG(a.app_name) OVER (PARTITION BY ev.journey_assoc_id ORDER BY ev.step, ev.search_time) AS prev_app
  FROM cross_app_journey_event ev
  JOIN app_master a ON a.id = ev.app_assoc_id
)
SELECT prev_app AS from_app, app_name AS to_app, COUNT(*) AS switches, COUNT(DISTINCT journey_assoc_id) AS journeys
FROM e
WHERE prev_app IS NOT NULL AND prev_app <> app_name
  [[ AND prev_app = :from_app ]]
  [[ AND app_name = :to_app ]]
GROUP BY 1, 2
ORDER BY switches DESC, 1
LIMIT :limit
```

