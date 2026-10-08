---
type: Metric
title: Purchase share by app per day
description: Each app's share of purchases per day, to see whether an app is gaining or losing share.
tags:
- trend
- grown
- shrunk
- growing
- over time
- by day
- weekly
- share trend
- gaining share
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
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
focus_parameter: app
---

# Purchase share by app per day

Each app's share of purchases per day, to see whether an app is gaining or losing share.

## Example questions

- Has Meesho's purchase share grown or shrunk?
- is any smaller app gaining share faster

## Caveats

The trend can only cover the dates present in the database.

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
WITH daily AS (
  SELECT CAST(jw.purchase_time AS date) AS day, COALESCE(a.app_name, 'Unknown app') AS purchase_app, COUNT(*) AS purchases
  FROM journey_window jw
  LEFT JOIN app_master a ON a.id = jw.purchase_app_id
  WHERE jw.window_hours = :window_hours AND jw.is_converted = true AND jw.purchase_time IS NOT NULL
    [[ AND CAST(jw.purchase_time AS date) >= CAST(:date_from AS date) ]]
    [[ AND CAST(jw.purchase_time AS date) <= CAST(:date_to AS date) ]]
  GROUP BY 1, 2
),
shares AS (
  SELECT day, purchase_app, purchases,
         ROUND(100.0 * purchases / SUM(purchases) OVER (PARTITION BY day), 1) AS share_of_day_pct
  FROM daily
)
SELECT day, purchase_app, purchases, share_of_day_pct
FROM shares
WHERE 1 = 1 [[ AND purchase_app = :app ]]
ORDER BY day, purchases DESC, purchase_app
```

