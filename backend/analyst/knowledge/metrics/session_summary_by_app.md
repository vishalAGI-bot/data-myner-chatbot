---
type: Metric
title: Session summary by app
description: Sessions, average minutes, screens, searches, product pages and order-confirmation rate by app.
tags:
- session
- sessions
- session time
- screens
- engagement
- order confirmation
- sessions per app
- sessions by app
- visits per app
- visits by app
tables:
- session_master
runtime: postgresql
parameters:
- name: app
  type: app
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
focus_parameter: app
---

# Session summary by app

Sessions, average minutes, screens, searches, product pages and order-confirmation rate by app.

## Example questions

- average session time by app
- which app has the longest sessions
- how many sessions per app

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT split_part(s.session_id, '_', 2) AS app,
       COUNT(*) AS sessions,
       ROUND(CAST(AVG(s.total_session_time_min) AS numeric), 1) AS avg_minutes,
       ROUND(CAST(AVG(s.number_of_screens) AS numeric), 0) AS avg_screens,
       ROUND(CAST(AVG(s.no_of_searches) AS numeric), 1) AS avg_searches,
       ROUND(100.0 * COUNT(*) FILTER (WHERE s.is_order_confirmation_page) / NULLIF(COUNT(*), 0), 1) AS order_confirmation_pct
FROM session_master s
WHERE COALESCE(s.insignificant_session, false) = false
  [[ AND split_part(s.session_id, '_', 2) = :app ]]
GROUP BY 1
ORDER BY avg_minutes DESC, sessions DESC, 1
```
