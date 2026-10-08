---
type: Metric
title: Search category share by day
description: Daily journeys and share of the day for the top search categories plus the named one, to see if a category is a spike or steady compared with the others.
tags:
- category
- trend
- spike
- dominant
- this week
- by day
- over time
- women's traditional wear
tables:
- journey_window
runtime: postgresql
parameters:
- name: category
  type: text
- name: window_hours
  type: int
  default: 24
- name: date_from
  type: date
- name: date_to
  type: date
status: draft
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Search category share by day

Daily journeys and share of the day for the top search categories plus the named one, to see if a category is a spike or steady compared with the others.

## Example questions

- Is Women's Traditional Wear always this dominant, or is this a spike this week?
- category trend by day
- which categories lead each day

## Caveats

Shows the named category next to the 5 largest categories of the latest 7 days, Uncategorised and Other categories (all the rest); shares are of all journeys that day.

# Computation

`[[ ... ]]` blocks are included only when every `:parameter` inside them has a value. `{{dimension}}` is replaced from the `dimensions` allow-list above.

```sql
WITH daily AS (
  SELECT CAST(jw.window_start AS date) AS day,
         COALESCE(NULLIF(jw.search_category, ''), 'Uncategorised') AS search_category, COUNT(*) AS journeys
  FROM journey_window jw
  WHERE jw.window_hours = :window_hours
    [[ AND CAST(jw.window_start AS date) >= CAST(:date_from AS date) ]]
    [[ AND CAST(jw.window_start AS date) <= CAST(:date_to AS date) ]]
  GROUP BY 1, 2
),
top_categories AS (
  SELECT search_category FROM daily
  WHERE search_category <> 'Uncategorised' AND day > (SELECT MAX(day) FROM daily) - 7
  GROUP BY search_category
  ORDER BY SUM(journeys) DESC, search_category
  LIMIT 5
),
folded AS (
  SELECT d.day,
         CASE WHEN d.search_category = 'Uncategorised' OR t.search_category IS NOT NULL
                   [[ OR d.search_category ILIKE '%' || :category || '%' ]]
              THEN d.search_category ELSE 'Other categories' END AS search_category,
         SUM(d.journeys) AS journeys
  FROM daily d
  LEFT JOIN top_categories t ON t.search_category = d.search_category
  GROUP BY 1, 2
)
SELECT day, search_category, journeys,
       ROUND(100.0 * journeys / SUM(journeys) OVER (PARTITION BY day), 1) AS share_of_day_pct
FROM folded
ORDER BY day, journeys DESC, search_category
```
