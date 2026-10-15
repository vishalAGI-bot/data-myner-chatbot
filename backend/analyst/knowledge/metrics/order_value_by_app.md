---
type: Metric
title: Order value by app
description: Number of purchases, total and average order value by the app where the purchase happened.
tags:
- order value
- aov
- average order value
- basket
- revenue
- gmv
- order amount
- spend
tables:
- journey_window
- app_master
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
- name: category
  type: text
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
focus_parameter: app
---

# Order value by app

Number of purchases, total and average order value by the app where the purchase happened.

## Example questions

- average order value by app
- which app has the highest order value

# Computation

`category` matches a sub-category name (search_category, partly) or every sub-category of a category or master category of that name (any case): `:category_subs` is filled from category_master. A sub-category name shared by several parents is not attributed to either.

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(a.app_name, 'Unknown app') AS purchase_app,
       COUNT(*) AS purchases,
       ROUND(CAST(SUM(jw.order_amount) AS numeric), 0) AS total_order_value,
       ROUND(CAST(AVG(jw.order_amount) AS numeric), 0) AS avg_order_value
FROM journey_window jw
LEFT JOIN app_master a ON a.id = jw.purchase_app_id
WHERE jw.window_hours = :window_hours AND jw.is_converted = true AND jw.order_amount IS NOT NULL
  [[ AND a.app_name = :app ]]
  [[ AND CAST(jw.window_start AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(jw.window_start AS date) <= CAST(:date_to AS date) ]]
  [[ AND (jw.search_category ILIKE '%' || :category || '%' OR jw.search_category IN :category_subs) ]]
GROUP BY 1
ORDER BY total_order_value DESC, 1
```

