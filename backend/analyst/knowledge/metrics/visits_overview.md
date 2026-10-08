---
type: Metric
title: Total visits and visitors
description: Overall totals across all apps (or one app) - total visits (app sessions), distinct users who visited, visits per user, apps used and average visit length. Period = last N hours of data, or a date range; default is all data.
tags:
- total visits
- total user visits
- user visits
- how many visits
- number of visits
- visit count
- visits
- total visitors
- visitors
- total active users
- overall users
- overall visits
- total sessions
- how many sessions
- sessions in total
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
  at: '2026-09-30T19:00:00+05:30'
---

# Total visits and visitors

A **visit** is one (non-noise) app session in `session_master`. `visits` = number of sessions, `users` = distinct
users with at least one visit, `visits_per_user` = visits / users, `apps` = distinct apps visited. The session
start (local time) is read from `session_id` (`<user>_<App>_<YYYYMMDDTHHMMSS>`); "last N hours" counts back from the
newest session. With no app or period, the totals cover every app and all loaded session data, and the answer
states that period. Session data can start earlier than the journey data. Sessions stamped more than two years
before the newest session are device-clock errors and are excluded.

**Do not write `CROSS JOIN latest`.** PostgreSQL cannot estimate the selectivity of the regex over
`split_part(session_id, ...)`, so it costs the `s` CTE at ~32 rows instead of ~240,000, puts the
`latest` aggregate on the inner side of a nested loop, and re-aggregates the whole CTE once per row
(the query then runs for over 20 minutes). Referencing it as the scalar subquery
`(SELECT newest FROM latest)` is uncorrelated, so it is evaluated once as an InitPlan: same result,
about 4 seconds.

## Example questions

- how many total user visits
- total visits
- how many visits and visitors in the last 24 hours
- how many sessions in total
- total visits on Amazon

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value.

```sql
WITH s AS (
    SELECT split_part(session_id, '_', 1) AS user_key,
           split_part(session_id, '_', 2) AS app,
           total_session_time_min,
           CAST(substr(split_part(session_id, '_', 3), 1, 4) || '-' || substr(split_part(session_id, '_', 3), 5, 2)
                || '-' || substr(split_part(session_id, '_', 3), 7, 2) AS date)
             + CAST(substr(split_part(session_id, '_', 3), 10, 2) AS integer) * INTERVAL '1 hour'
             + CAST(substr(split_part(session_id, '_', 3), 12, 2) AS integer) * INTERVAL '1 minute'
             + CAST(substr(split_part(session_id, '_', 3), 14, 2) AS integer) * INTERVAL '1 second' AS started
    FROM session_master
    WHERE COALESCE(insignificant_session, false) = false
      AND split_part(session_id, '_', 3) ~ '^20[0-9]{6}T[0-9]{6}'
),
latest AS (SELECT MAX(started) AS newest FROM s)
SELECT COUNT(*) AS visits,
       COUNT(DISTINCT s.user_key) AS users,
       ROUND(CAST(COUNT(*) AS numeric) / NULLIF(COUNT(DISTINCT s.user_key), 0), 1) AS visits_per_user,
       COUNT(DISTINCT s.app) AS apps,
       ROUND(CAST(AVG(s.total_session_time_min) AS numeric), 1) AS avg_visit_minutes,
       MIN(s.started) AS period_start,
       MAX(s.started) AS period_end
FROM s
WHERE s.started IS NOT NULL
  AND s.started > (SELECT newest FROM latest) - INTERVAL '2 years'
  [[ AND s.app = :app ]]
  [[ AND s.started > (SELECT newest FROM latest) - :hours * INTERVAL '1 hour' ]]
  [[ AND s.started >= CAST(:date_from AS date) ]]
  [[ AND s.started < CAST(:date_to AS date) + INTERVAL '1 day' ]]
```
