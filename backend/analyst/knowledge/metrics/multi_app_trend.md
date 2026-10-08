---
type: Metric
title: Single-app vs multi-app journeys by day
description: Share of journeys that used one app vs several apps, per day.
tags:
- multi app
- cross app
- single app
- 1 app per journey
- pattern
- increasing
- over time
- trend
tables:
- cross_app_journey
runtime: postgresql
parameters:
- name: date_from
  type: date
- name: date_to
  type: date
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Single-app vs multi-app journeys by day

Share of journeys that used one app vs several apps, per day.

## Example questions

- Has the 1 app per journey pattern always been this high, or is cross-app searching increasing?

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT CAST(j.first_search_time AS date) AS day,
       COUNT(*) AS journeys,
       COUNT(*) FILTER (WHERE j.total_apps > 1) AS multi_app_journeys,
       ROUND(100.0 * COUNT(*) FILTER (WHERE j.total_apps = 1) / NULLIF(COUNT(*), 0), 1) AS single_app_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE j.total_apps > 1) / NULLIF(COUNT(*), 0), 1) AS multi_app_pct
FROM cross_app_journey j
WHERE j.first_search_time IS NOT NULL
  [[ AND CAST(j.first_search_time AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(j.first_search_time AS date) <= CAST(:date_to AS date) ]]
GROUP BY 1
ORDER BY 1
```

