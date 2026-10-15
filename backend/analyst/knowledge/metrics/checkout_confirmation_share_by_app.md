---
type: Metric
title: Checkout-confirmation signal by app
description: Directional share of non-noise app sessions where an order-confirmation screen was detected; this is a behavioural signal, not verified purchase market share.
tags:
- checkout confirmation
- order confirmation
- purchase signal
- checkout share
- confirmation share
tables:
- session_master
runtime: postgresql
parameters:
- name: date_from
  type: date
- name: date_to
  type: date
status: stable
focus_parameter: app
---

# Checkout-confirmation signal by app

Directional share of app sessions where an order-confirmation screen was detected.

## Example questions

- Which apps have the most checkout-confirmation sessions?
- checkout confirmation share by app
- which app has the strongest purchase signal

## Caveats

This is not verified order or market-share data. A screen detector can miss an order or count repeated confirmation screens, so use it only as a directional signal until purchases are linked to journeys.

# Computation

```sql
WITH app_sessions AS (
  SELECT split_part(s.session_id, '_', 2) AS app,
         COUNT(*) AS sessions,
         COUNT(*) FILTER (WHERE s.is_order_confirmation_page) AS checkout_confirmations
  FROM session_master s
  WHERE COALESCE(s.insignificant_session, false) = false
    [[ AND CAST(s.created_at AS date) >= CAST(:date_from AS date) ]]
    [[ AND CAST(s.created_at AS date) <= CAST(:date_to AS date) ]]
  GROUP BY 1
)
SELECT app,
       checkout_confirmations,
       ROUND(100.0 * checkout_confirmations / NULLIF(SUM(checkout_confirmations) OVER (), 0), 1) AS confirmation_share_pct,
       sessions,
       ROUND(100.0 * checkout_confirmations / NULLIF(sessions, 0), 1) AS sessions_with_confirmation_pct
FROM app_sessions
WHERE checkout_confirmations > 0
ORDER BY checkout_confirmations DESC, app
```
