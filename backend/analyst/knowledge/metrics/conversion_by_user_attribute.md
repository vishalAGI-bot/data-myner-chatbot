---
type: Metric
title: Conversion and retention by user attribute or time of day
description: Conversion, retention and leakage split by a user attribute (city, state, zone, gender, loyalty tier, device
  model, age group) or by hour of day.
tags:
- city
- state
- zone
- gender
- tier
- device
- age
- time of day
- hour
- demographic
- retention rate differ
tables:
- journey_window
- cross_app_journey
- user_master
- app_master
runtime: postgresql
parameters:
- name: dimension
  type: enum
  values:
  - city
  - state
  - zone
  - gender
  - tier
  - device_model
  - age_group
  - hour_of_day
  default: city
- name: app
  type: app
- name: window_hours
  type: int
  default: 24
- name: limit
  type: int
  default: 25
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
dimensions:
  city: u.city
  state: u.state
  zone: u.zone
  gender: u.gender
  tier: u.tier
  device_model: u.device_model
  hour_of_day: EXTRACT(HOUR FROM jw.window_start)
  age_group: CASE WHEN u.date_of_birth IS NULL THEN 'Unknown' WHEN EXTRACT(YEAR FROM AGE(CURRENT_DATE, u.date_of_birth)) <
    25 THEN 'Under 25' WHEN EXTRACT(YEAR FROM AGE(CURRENT_DATE, u.date_of_birth)) < 35 THEN '25-34' WHEN EXTRACT(YEAR FROM
    AGE(CURRENT_DATE, u.date_of_birth)) < 45 THEN '35-44' ELSE '45+' END
---

# Conversion and retention by user attribute or time of day

Conversion, retention and leakage split by a user attribute (city, state, zone, gender, loyalty tier, device model, age group) or by hour of day.

## Example questions

- Does our retention rate differ by city, device type, or time of day?
- conversion by gender
- retention by zone

## Caveats

City tier is not in the data; 'tier' is the loyalty tier (Gold/Silver/Bronze).

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(CAST({{dimension}} AS text), 'Unknown') AS segment,
       COUNT(*) AS journeys,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_converted) / NULLIF(COUNT(*), 0), 1) AS conversion_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_retained) / NULLIF(COUNT(*), 0), 1) AS retained_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE jw.is_leaked) / NULLIF(COUNT(*), 0), 1) AS leaked_pct
FROM journey_window jw
JOIN cross_app_journey j ON j.id = jw.journey_assoc_id
LEFT JOIN user_master u ON u.id = j.user_assoc_id
LEFT JOIN app_master a ON a.id = jw.first_search_app_id
WHERE jw.window_hours = :window_hours AND jw.window_end <= (SELECT MAX(lw.window_start) FROM journey_window lw)
  [[ AND a.app_name = :app ]]
GROUP BY 1
ORDER BY journeys DESC, 1
LIMIT :limit
```

