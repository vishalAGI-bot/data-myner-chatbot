---
type: Metric
title: App scorecard by segment
description: Executive-summary scorecard - each app's conversion, retention, leakage and purchase share per
  breakdown, with a minimum base per group.
tags:
- scorecard
- executive summary
- how did we perform
- performance by segment
- where competition did better
tables:
- journey_window
- cross_app_journey
- user_master
- app_master
- category_master
runtime: postgresql
parameters:
- name: dimension
  type: enum
  values:
  - overall
  - master_category
  - category
  - sub_category
  - city
  - state
  - zone
  - gender
  - tier
  - age_group
  - device_model
  default: overall
- name: window_hours
  type: int
  default: 24
- name: date_from
  type: date
- name: date_to
  type: date
- name: categories
  type: list
- name: min_users
  type: int
  default: 30
- name: limit
  type: int
  default: 400
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-10-09T12:00:00+05:30'
combine_dimensions: true
dimensions:
  overall: '''All journeys'''
  master_category: COALESCE(cat.master_category, 'Uncategorised')
  category: COALESCE(cat.category, 'Uncategorised')
  sub_category: COALESCE(jw.search_category, 'Uncategorised')
  city: u.city
  state: u.state
  zone: u.zone
  gender: u.gender
  tier: u.tier
  device_model: u.device_model
  age_group: CASE WHEN u.date_of_birth IS NULL THEN 'Unknown' WHEN EXTRACT(YEAR FROM AGE(CURRENT_DATE, u.date_of_birth)) <
    25 THEN 'Under 25' WHEN EXTRACT(YEAR FROM AGE(CURRENT_DATE, u.date_of_birth)) < 35 THEN '25-34' WHEN EXTRACT(YEAR FROM
    AGE(CURRENT_DATE, u.date_of_birth)) < 45 THEN '35-44' ELSE '45+' END
---

# App scorecard by segment

Executive-summary briefs run this once per breakdown. For each breakdown value and each app: distinct users and
journeys that started on the app, conversion, retention and leakage of those journeys, purchases the app won and
its share of all purchases there. Groups (value x app) under `min_users` distinct users are left out, so no figure
rests on a small base. `dimension` may cross two dimensions ("category+gender").

## Example questions

- executive summary of how we performed yesterday and where competitors did better
- scorecard for the sale day

## Caveats

A journey belongs to the app of its first search. Conversion, retention and leakage use finished windows only, so
journeys from the last hours of the period whose window has not ended yet are not counted. Purchase share is the
app's share of all purchases in the segment, including purchases on apps that fall below the user base. City tier is
not recorded; 'tier' is the loyalty tier (Gold/Silver/Bronze).

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from
the `dimensions` allow-list above. `categories` are taxonomy paths matched exactly: "Master", "Master > Category" or
"Master > Category > Sub-category". A journey's sub-category is placed in the taxonomy only when it has exactly one
parent; a name such as "Others" that recurs under several categories cannot be placed and counts as Uncategorised.

```sql
WITH cat AS (
  SELECT cm.sub_category, MIN(cm.category) AS category, MIN(cm.master_category) AS master_category
  FROM category_master cm
  GROUP BY cm.sub_category
  HAVING COUNT(DISTINCT CONCAT(cm.master_category, ' > ', cm.category)) = 1
),
base AS (
  SELECT COALESCE(CAST({{dimension}} AS text), 'Unknown') AS segment,
         jw.first_search_app_id, jw.purchase_app_id, jw.is_converted, jw.is_retained, jw.is_leaked,
         j.user_assoc_id
  FROM journey_window jw
  JOIN cross_app_journey j ON j.id = jw.journey_assoc_id
  LEFT JOIN user_master u ON u.id = j.user_assoc_id
  LEFT JOIN cat ON cat.sub_category = jw.search_category
  WHERE jw.window_hours = :window_hours
    AND jw.window_end <= (SELECT MAX(lw.window_start) FROM journey_window lw)
    [[ AND CAST(jw.window_start AS date) >= CAST(:date_from AS date) ]]
    [[ AND CAST(jw.window_start AS date) <= CAST(:date_to AS date) ]]
    [[ AND (cat.master_category IN :categories
            OR CONCAT(cat.master_category, ' > ', cat.category) IN :categories
            OR CONCAT(cat.master_category, ' > ', cat.category, ' > ', cat.sub_category) IN :categories) ]]
),
started AS (
  SELECT segment, first_search_app_id AS app_id,
         COUNT(DISTINCT user_assoc_id) AS users,
         COUNT(*) AS journeys,
         COUNT(*) FILTER (WHERE is_converted) AS converted,
         COUNT(*) FILTER (WHERE is_retained) AS retained,
         COUNT(*) FILTER (WHERE is_leaked) AS leaked
  FROM base
  GROUP BY segment, first_search_app_id
),
won AS (
  SELECT segment, purchase_app_id AS app_id, COUNT(*) AS purchases_won
  FROM base
  WHERE is_converted AND purchase_app_id IS NOT NULL
  GROUP BY segment, purchase_app_id
),
segment_purchases AS (
  SELECT segment, SUM(purchases_won) AS purchases
  FROM won
  GROUP BY segment
),
segment_size AS (
  SELECT segment, COUNT(DISTINCT user_assoc_id) AS segment_users
  FROM base
  GROUP BY segment
)
SELECT s.segment,
       COALESCE(a.app_name, 'Unknown app') AS app,
       s.users,
       s.journeys,
       s.converted,
       ROUND(100.0 * s.converted / NULLIF(s.journeys, 0), 1) AS conversion_pct,
       ROUND(100.0 * s.retained / NULLIF(s.journeys, 0), 1) AS retained_pct,
       ROUND(100.0 * s.leaked / NULLIF(s.journeys, 0), 1) AS leaked_pct,
       COALESCE(w.purchases_won, 0) AS purchases_won,
       ROUND(100.0 * COALESCE(w.purchases_won, 0) / NULLIF(sp.purchases, 0), 1) AS purchase_share_pct
FROM started s
JOIN segment_size z ON z.segment = s.segment
LEFT JOIN won w ON w.segment = s.segment AND w.app_id = s.app_id
LEFT JOIN segment_purchases sp ON sp.segment = s.segment
LEFT JOIN app_master a ON a.id = s.app_id
WHERE s.users >= :min_users
ORDER BY z.segment_users DESC, s.segment, s.journeys DESC, app
LIMIT :limit
```
