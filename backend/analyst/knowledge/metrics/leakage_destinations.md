---
type: Metric
title: Where leaked purchases go
description: 'For journeys that start on an app but buy elsewhere: which app got the purchase.'
tags:
- leakage
- leak
- lose to
- lost to
- competitor
- buy elsewhere
- which app do we lose to
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
- name: category
  type: text
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Where leaked purchases go

For journeys that start on an app but buy elsewhere: which app got the purchase.

## Example questions

- When users search on Amazon and then buy elsewhere, where do they buy?
- which app do we lose to most

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(p.app_name, 'Unknown app') AS bought_on,
       COUNT(*) AS purchases,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS share_of_leaked_pct,
       ROUND(CAST(AVG(jw.order_amount) AS numeric), 0) AS avg_order_value
FROM journey_window jw
JOIN app_master f ON f.id = jw.first_search_app_id
LEFT JOIN app_master p ON p.id = jw.purchase_app_id
WHERE jw.window_hours = :window_hours AND jw.is_leaked = true AND f.app_name = :app
  [[ AND CAST(jw.window_start AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(jw.window_start AS date) <= CAST(:date_to AS date) ]]
  [[ AND jw.search_category ILIKE '%' || :category || '%' ]]
GROUP BY 1
ORDER BY purchases DESC, 1
```

