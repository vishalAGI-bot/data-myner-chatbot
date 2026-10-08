---
type: Metric
title: Purchase share by app
description: Each app's share of all purchases (by the app where the purchase happened) within a window.
tags:
- purchase share
- market share
- share of purchases
- who is ahead
- ranking
- purchases by app
- where do people buy
tables:
- journey_window
- app_master
runtime: postgresql
parameters:
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
focus_parameter: app
---

# Purchase share by app

Each app's share of all purchases (by the app where the purchase happened) within a window.

## Example questions

- What's Amazon's overall purchase share and who is ahead of us?
- purchase share by app
- which app gets the most purchases

## Caveats

Counts purchases detected within the window after a journey's first search. Unknown app = purchase app not identified.

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(a.app_name, 'Unknown app') AS purchase_app,
       COUNT(*) AS purchases,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS share_pct
FROM journey_window jw
LEFT JOIN app_master a ON a.id = jw.purchase_app_id
WHERE jw.window_hours = :window_hours AND jw.is_converted = true
  [[ AND CAST(jw.window_start AS date) >= CAST(:date_from AS date) ]]
  [[ AND CAST(jw.window_start AS date) <= CAST(:date_to AS date) ]]
  [[ AND jw.search_category ILIKE '%' || :category || '%' ]]
GROUP BY 1
ORDER BY purchases DESC, 1
```

