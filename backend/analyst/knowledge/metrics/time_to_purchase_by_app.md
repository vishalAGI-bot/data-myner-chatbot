---
type: Metric
title: Time to purchase by app
description: Average and median hours from a journey's first search to the purchase, by the app where the purchase happened.
tags:
- time to purchase
- hours to purchase
- how fast
- speed
- average time
- median time
tables:
- journey_window
- app_master
runtime: postgresql
parameters:
- name: window_hours
  type: int
  default: 72
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

# Time to purchase by app

Average and median hours from a journey's first search to the purchase, by the app where the purchase happened.

## Example questions

- What's our average time-to-purchase compared to Meesho and Flipkart?
- how long until people buy

## Caveats

Uses the 72h window by default so slower purchases are included.

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(a.app_name, 'Unknown app') AS purchase_app,
       COUNT(*) AS purchases,
       ROUND(CAST(AVG(jw.hours_to_purchase) AS numeric), 1) AS avg_hours,
       ROUND(CAST(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY jw.hours_to_purchase) AS numeric), 1) AS median_hours
FROM journey_window jw
LEFT JOIN app_master a ON a.id = jw.purchase_app_id
WHERE jw.window_hours = :window_hours AND jw.is_converted = true AND jw.hours_to_purchase IS NOT NULL
  [[ AND a.app_name = :app ]]
  [[ AND CAST(jw.window_start AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(jw.window_start AS date) <= CAST(:date_to AS date) ]]
  [[ AND jw.search_category ILIKE '%' || :category || '%' ]]
GROUP BY 1
ORDER BY purchases DESC, 1
```

