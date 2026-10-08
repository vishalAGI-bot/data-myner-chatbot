---
type: Glossary
title: Business terms
description: How the chatbot interprets common words.
status: stable
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Glossary

- **We / us / our** - the home app, set by `ANALYST_HOME_APP` (default Amazon).
- **Visit** - one app session: a non-noise row in `session_master` (`insignificant_session` is not true). "Total visits" = number of sessions. A visit is NOT a journey or a screen event.
- **Visitors / active users / users who visited** - distinct users (1st part of `session_id`) with at least one visit in the period.
- **Total / overall / how many** (no app or period named) - across all apps and all loaded data; the answer states the period covered.
- **Journey** - one product hunt: related searches for the same need, possibly across several apps (`cross_app_journey`).
- **Window** - 24, 48 or 72 hours after the journey's first search (`journey_window.window_hours`). Default 24.
- **Converted** - a purchase was detected within the window.
- **Retained** - the purchase happened on the app where the journey started (first-search app).
- **Leaked** - the purchase happened on a different app than the first-search app.
- **Purchase share** - an app's share of ALL purchases (by purchase app). Not the same as conversion rate.
- **Conversion rate** - converted journeys / journeys that started on the app (finished windows only).
- **Finished window** - `window_end` at or before the newest loaded journey (`window_end <= (SELECT MAX(window_start) FROM journey_window)`). Not `is_final`, which the pipeline does not maintain.
- **Time to purchase** - `journey_window.hours_to_purchase`.
- **Order value** - `journey_window.order_amount` (INR); AOV = average order value.
- **Competitor switch** - `session_master.competition_switch_bfr_5min`: user opened a competitor app within 5 minutes.
- **Sub-app** - an app inside another app (Amazon Now in Amazon, Flipkart Minutes in Flipkart); `app_master.parent_id`.
- **Tier** - loyalty tier (Gold/Silver/Bronze) in `user_master`. City tier is not recorded.
- **Repeat switcher** - a user who searched on 3 or more different apps.
