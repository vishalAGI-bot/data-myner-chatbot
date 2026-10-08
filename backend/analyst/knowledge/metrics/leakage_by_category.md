---
type: Metric
title: Leakage by search category
description: 'For journeys starting on an app: by search category, how many converted, stayed (retained) or leaked.'
tags:
- leakage
- category
- categories
- drive leakage
- fashion
- grocery
- apparel
tables:
- journey_window
- app_master
runtime: postgresql
parameters:
- name: app
  type: app
  default: HOME
- name: window_hours
  type: int
  default: 24
- name: date_from
  type: date
- name: date_to
  type: date
- name: limit
  type: int
  default: 20
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Leakage by search category

For journeys starting on an app: by search category, how many converted, stayed (retained) or leaked.

## Example questions

- When users search on Amazon and then leave, which categories drive most of that leakage?
- leakage by category for Flipkart

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(jw.search_category, 'Uncategorised') AS search_category,
       COUNT(*) AS journeys,
       COUNT(*) FILTER (WHERE jw.is_converted) AS converted,
       COUNT(*) FILTER (WHERE jw.is_retained) AS retained,
       COUNT(*) FILTER (WHERE jw.is_leaked) AS leaked,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_leaked) / NULLIF(COUNT(*) FILTER (WHERE jw.is_converted), 0), 1) AS leaked_pct_of_purchases
FROM journey_window jw
JOIN app_master f ON f.id = jw.first_search_app_id
WHERE jw.window_hours = :window_hours AND jw.window_end <= (SELECT MAX(lw.window_start) FROM journey_window lw) AND f.app_name = :app
  [[ AND CAST(jw.window_start AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(jw.window_start AS date) <= CAST(:date_to AS date) ]]
GROUP BY 1
ORDER BY leaked DESC, journeys DESC, 1
LIMIT :limit
```

