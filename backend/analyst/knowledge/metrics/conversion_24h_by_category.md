---
type: Metric
title: Conversion within a window by category
description: Journeys and conversion rate by search category within a window (is the conversion concentrated in a few categories?).
tags:
- conversion
- within 24h
- category
- categories
- concentrated
- spread
- why is conversion
tables:
- journey_window
runtime: postgresql
parameters:
- name: window_hours
  type: int
  default: 24
- name: app
  type: app
- name: date_from
  type: date
- name: date_to
  type: date
- name: limit
  type: int
  default: 25
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Conversion within a window by category

Journeys and conversion rate by search category within a window (is the conversion concentrated in a few categories?).

## Example questions

- Why is our conversion-within-24h low - is it concentrated in a few categories?
- conversion by category

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(jw.search_category, 'Uncategorised') AS search_category,
       COUNT(*) AS journeys,
       COUNT(*) FILTER (WHERE jw.is_converted) AS converted,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_converted) / NULLIF(COUNT(*), 0), 1) AS conversion_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_converted) / NULLIF(SUM(COUNT(*) FILTER (WHERE jw.is_converted)) OVER (), 0), 1) AS share_of_all_purchases_pct
FROM journey_window jw
LEFT JOIN app_master a ON a.id = jw.first_search_app_id
WHERE jw.window_hours = :window_hours AND jw.window_end <= (SELECT MAX(lw.window_start) FROM journey_window lw)
  [[ AND a.app_name = :app ]]
  [[ AND CAST(jw.window_start AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(jw.window_start AS date) <= CAST(:date_to AS date) ]]
GROUP BY 1
ORDER BY journeys DESC, 1
LIMIT :limit
```

