---
type: Metric
title: What users search right before switching apps
description: The search query on the first app immediately before the user's next search moved to another app.
tags:
- before switch
- right before
- search before
- typically searching
- switch
- amazon to meesho
- query
tables:
- cross_app_journey_event
- app_master
runtime: postgresql
parameters:
- name: from_app
  type: app
  default: HOME
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

# What users search right before switching apps

The search query on the first app immediately before the user's next search moved to another app.

## Example questions

- For users who go Amazon -> Meesho, what are they typically searching for right before they switch?

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
WITH e AS (
  SELECT ev.journey_assoc_id, a.app_name,
         LAG(a.app_name) OVER (PARTITION BY ev.journey_assoc_id ORDER BY ev.step, ev.search_time) AS prev_app,
         LAG(COALESCE(ev.normalized_query, ev.query)) OVER (PARTITION BY ev.journey_assoc_id ORDER BY ev.step, ev.search_time) AS prev_query
  FROM cross_app_journey_event ev
  JOIN app_master a ON a.id = ev.app_assoc_id
)
SELECT prev_query AS query_before_switch, COUNT(*) AS switches, COUNT(DISTINCT journey_assoc_id) AS journeys
FROM e
WHERE prev_app IS NOT NULL AND prev_app <> app_name AND prev_app = :from_app
  [[ AND app_name = :to_app ]]
GROUP BY 1
ORDER BY switches DESC, 1
LIMIT :limit
```

