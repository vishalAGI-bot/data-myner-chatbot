---
type: Metric
title: Repeat switchers vs single-app users
description: Users grouped by how many different apps they searched on (1, 2, 3+), with journeys, conversion and order value.
tags:
- repeat switchers
- switchers
- 3+ apps
- bounce between apps
- segment
- single-app users
- multi app users
tables:
- journey_window
- cross_app_journey
runtime: postgresql
parameters:
- name: window_hours
  type: int
  default: 24
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Repeat switchers vs single-app users

Users grouped by how many different apps they searched on (1, 2, 3+), with journeys, conversion and order value.

## Example questions

- Are repeat switchers (3+ apps) a different segment from single-app users?

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
WITH jw AS (
  SELECT jw.journey_assoc_id, jw.apps_searched, jw.is_converted, jw.order_amount, j.user_assoc_id
  FROM journey_window jw
  JOIN cross_app_journey j ON j.id = jw.journey_assoc_id
  WHERE jw.window_hours = :window_hours AND jw.window_end <= (SELECT MAX(lw.window_start) FROM journey_window lw)
),
user_apps AS (
  SELECT jw.user_assoc_id, COUNT(DISTINCT x.app) AS apps_used
  FROM jw, unnest(jw.apps_searched) AS x(app)
  GROUP BY 1
)
SELECT CASE WHEN ua.apps_used >= 3 THEN '3+ apps' WHEN ua.apps_used = 2 THEN '2 apps' ELSE '1 app' END AS segment,
       COUNT(DISTINCT jw.user_assoc_id) AS users,
       COUNT(*) AS journeys,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_converted) / NULLIF(COUNT(*), 0), 1) AS conversion_pct,
       ROUND(CAST(AVG(jw.order_amount) AS numeric), 0) AS avg_order_value
FROM jw
JOIN user_apps ua ON ua.user_assoc_id = jw.user_assoc_id
GROUP BY 1
ORDER BY 1
```

