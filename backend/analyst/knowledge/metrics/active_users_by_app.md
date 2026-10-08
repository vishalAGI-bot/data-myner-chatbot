---
type: Metric
title: Active users by app
description: How many different users visited (had a session on) each app in a period, how many used ONLY that app and no other, sessions, and share of all active users. Period = last N hours of data, or a date range.
tags:
- active users
- users visited
- visited
- visit
- visitors
- unique users
- how many users visited
- used the app
- only used
- only visited
- only on
- exclusive users
- loyal users
- single app users
- daily active users
- dau
- last 24 hours
- last hours
tables:
- session_master
runtime: postgresql
parameters:
- name: app
  type: app
- name: hours
  type: int
- name: date_from
  type: date
- name: date_to
  type: date
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T15:30:00+05:30'
focus_parameter: app
---

# Active users by app

A user "visited" an app when they have a (non-noise) session on it. The session start time (local time) is read
from `session_id` (`<user>_<App>_<YYYYMMDDTHHMMSS>`). "Last N hours" counts back from the newest session in the
database (the data can lag the calendar), so the answer always says which period it covers.
`users_only_this_app` = users who used this app and no other app in the same period.

**Do not join or `CROSS JOIN` back onto `p` or `s`.** PostgreSQL cannot estimate the selectivity of the
regex over `split_part(session_id, ...)`, so it costs those CTEs at ~32 rows instead of ~240,000 and picks
a nested loop that re-runs the inner aggregate once per row - the query then runs for over 20 minutes.
Aggregate to one row per app x user first and use a window function (and `(SELECT newest FROM latest)`,
which is uncorrelated and evaluated once): same numbers, about 5 seconds.

## Example questions

- how many users visited Amazon in the last 24 hours
- how many users visited Amazon only in the last 24 hours
- users who only use Flipkart
- active users by app today
- which app had the most unique users yesterday

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value.

```sql
WITH s AS (
    SELECT split_part(session_id, '_', 1) AS user_key,
           split_part(session_id, '_', 2) AS app,
           CAST(substr(split_part(session_id, '_', 3), 1, 4) || '-' || substr(split_part(session_id, '_', 3), 5, 2)
                || '-' || substr(split_part(session_id, '_', 3), 7, 2) AS date)
             + CAST(substr(split_part(session_id, '_', 3), 10, 2) AS integer) * INTERVAL '1 hour'
             + CAST(substr(split_part(session_id, '_', 3), 12, 2) AS integer) * INTERVAL '1 minute'
             + CAST(substr(split_part(session_id, '_', 3), 14, 2) AS integer) * INTERVAL '1 second' AS started
    FROM session_master
    WHERE COALESCE(insignificant_session, false) = false
      AND split_part(session_id, '_', 3) ~ '^20[0-9]{6}T[0-9]{6}'
),
latest AS (SELECT MAX(started) AS newest FROM s),
p AS (
    SELECT s.user_key, s.app, s.started
    FROM s
    WHERE s.started IS NOT NULL
      AND s.started > (SELECT newest FROM latest) - INTERVAL '2 years'
      [[ AND s.started > (SELECT newest FROM latest) - :hours * INTERVAL '1 hour' ]]
      [[ AND s.started >= CAST(:date_from AS date) ]]
      [[ AND s.started < CAST(:date_to AS date) + INTERVAL '1 day' ]]
),
-- One row per app x user collapses ~240,000 sessions to a few thousand rows, so the
-- user's app count comes from a window function instead of a join back onto p.
app_user AS (
    SELECT app, user_key, COUNT(*) AS sessions,
           MIN(started) AS first_started, MAX(started) AS last_started
    FROM p GROUP BY app, user_key
),
marked AS (
    SELECT app, user_key, sessions, first_started, last_started,
           COUNT(*) OVER (PARTITION BY user_key) AS apps_used
    FROM app_user
)
SELECT app,
       COUNT(*) AS users,
       COUNT(*) FILTER (WHERE apps_used = 1) AS users_only_this_app,
       SUM(sessions) AS sessions,
       ROUND(100.0 * COUNT(*) / NULLIF((SELECT COUNT(DISTINCT user_key) FROM marked), 0), 1) AS pct_of_active_users,
       MIN(first_started) AS period_start,
       MAX(last_started) AS period_end
FROM marked
WHERE 1 = 1
  [[ AND app = :app ]]
GROUP BY app
ORDER BY users DESC, app
```
