---
type: Metric
title: Panel users by attribute
description: Count of panel users by city, state, zone, gender, loyalty tier, device model or age group.
tags:
- users
- panel
- city
- state
- zone
- gender
- tier
- device
- age
- demographic
- how many users
tables:
- user_master
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
  default: city
- name: limit
  type: int
  default: 30
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
  age_group: CASE WHEN u.date_of_birth IS NULL THEN 'Unknown' WHEN EXTRACT(YEAR FROM AGE(CURRENT_DATE, u.date_of_birth)) <
    25 THEN 'Under 25' WHEN EXTRACT(YEAR FROM AGE(CURRENT_DATE, u.date_of_birth)) < 35 THEN '25-34' WHEN EXTRACT(YEAR FROM
    AGE(CURRENT_DATE, u.date_of_birth)) < 45 THEN '35-44' ELSE '45+' END
---

# Panel users by attribute

Count of panel users by city, state, zone, gender, loyalty tier, device model or age group.

## Example questions

- users by city
- gender split of users
- how many users per zone

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
SELECT COALESCE(CAST({{dimension}} AS text), 'Unknown') AS segment, COUNT(*) AS users,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS share_pct
FROM user_master u
WHERE COALESCE(u.is_deleted, false) = false
GROUP BY 1
ORDER BY users DESC, 1
LIMIT :limit
```

