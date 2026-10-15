---
type: Metric
title: Top users by visits
description: The individual users who visited most - their visit (session) count, how many apps they used, total time and first/last visit. Optionally for one app, a recent period or a date range.
tags:
- user list
- list of users
- top users
- top 10 users
- which users visited most
- user ids of the most active users
- who visited most
- most active users
- heaviest users
- users who visited the most
- most frequent users
- user wise visits
- per user visits
- loyal users list
tables:
- session_master
- user_master
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
- name: limit
  type: int
  default: 10
status: draft
generated:
  by: claude/opus-5
  at: '2026-10-07T15:30:00+05:30'
---

# Top users by visits

One row per user, ordered by how many visits (non-noise `session_master` sessions) they made. The user is the
first part of `session_id` (`<user>_<App>_<YYYYMMDDTHHMMSS>`), which is the same pseudonymous key the other
session analyses count as a "visitor". It is reported as `user_master.id` (the user number the journey
tables use) via the documented join `split_part(session_id,'_',1) = user_master.m_id`, falling back to the
session key when a panel row is missing. No name, phone, email or address is read - those columns are
blocked by policy. `visits` counts sessions,
`apps_used` counts the distinct apps that user visited in the period, and `total_minutes` sums session time.

Without `app` the ranking covers every app; with it, only visits to that app count (so `apps_used` is 1).
"Last N hours" counts back from the newest session in the database, as in the other session analyses.

This analysis exists because questions such as "give me the top 10 users who visited Meesho most" used to
match an analysis returning one row per **app**, which could only report how many users there were and never
which ones.

The ranking is cut to `:limit` rows *before* joining `user_master`, so the join touches ten rows rather than
every session. **Do not `CROSS JOIN latest`** - see `visits_overview.md`: PostgreSQL misjudges the size of a CTE filtered by
a regex over `split_part(session_id, ...)` and re-aggregates it once per row. `(SELECT newest FROM latest)` is
uncorrelated and runs once.

## Example questions

- give me the user list who visited Meesho the most
- top 10 users by visits
- which users visited most often
- user ids of the most active shoppers
- most frequent users on Flipkart in the last 24 hours

## Caveats

Ranks by number of sessions, not by time spent; ask for time spent instead if you mean duration. A user
is counted once per session, so a long single visit ranks below several short ones.

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
latest AS (SELECT MAX(started) AS newest FROM s),
p AS (
    SELECT s.user_key, s.app, s.started, s.total_session_time_min
    FROM s
    WHERE s.started IS NOT NULL
      AND s.started > (SELECT newest FROM latest) - INTERVAL '2 years'
      [[ AND s.app = :app ]]
      [[ AND s.started > (SELECT newest FROM latest) - :hours * INTERVAL '1 hour' ]]
      [[ AND s.started >= CAST(:date_from AS date) ]]
      [[ AND s.started < CAST(:date_to AS date) + INTERVAL '1 day' ]]
),
top AS (
    SELECT p.user_key,
           COUNT(*) AS visits,
           COUNT(DISTINCT p.app) AS apps_used,
           ROUND(CAST(SUM(p.total_session_time_min) AS numeric), 1) AS total_minutes,
           MIN(p.started) AS first_visit,
           MAX(p.started) AS last_visit
    FROM p
    GROUP BY p.user_key
    ORDER BY COUNT(*) DESC, p.user_key
    LIMIT :limit
)
SELECT COALESCE(CAST(u.id AS text), t.user_key) AS user_id,
       t.visits, t.apps_used, t.total_minutes, t.first_visit, t.last_visit
FROM top t
LEFT JOIN user_master u ON u.m_id = t.user_key
ORDER BY t.visits DESC, user_id
LIMIT :limit
```
