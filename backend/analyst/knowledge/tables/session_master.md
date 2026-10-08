---
type: Table
title: App sessions
description: 'One row per app session on a panel user''s phone: duration, screens, searches, order confirmation, competitor
  switch.'
resource: postgresql://datamyner_db/public/session_master
tags:
- session
- sessions
- session time
- screens
- competitor
- switch
- order confirmation
- order amount
status: stable
domain: analytics
grain: one row per app session
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# App sessions

One row per app session on a panel user's phone: duration, screens, searches, order confirmation, competitor switch.

## Grain

one row per app session

## Joins

- `sub_session_master.session_assoc_id = session_master.id`
- `cross_app_journey_event.session_assoc_id = session_master.id`
- `testing_accessibility_infos.session_assoc_id = session_master.id`

## Notes

There is no app id column: the app is the 2nd part of session_id (split_part(session_id,'_',2), e.g. 'Meesho'). The user key is the 1st part (user_master.m_id). competition_switch_bfr_5min = the user switched to a competitor app within 5 minutes; competitor_name is that app ('not applicable' otherwise). is_order_confirmation_page = an order confirmation screen was seen in the session.

A visit = one row with insignificant_session not true. The visit (session) START is encoded in the 3rd part of
session_id as YYYYMMDDTHHMMSS local time; created_at is only the record load time and must never be used as the
visit time. Parse the start only for rows where split_part(session_id,'_',3) ~ '^20[0-9]{6}T[0-9]{6}': visit day =
to_date(substr(split_part(session_id,'_',3),1,8),'YYYYMMDD'); full start =
to_timestamp(substr(split_part(session_id,'_',3),1,15),'YYYYMMDD"T"HH24MISS'). Exclude starts more than 2 years
before the newest session start (device-clock errors, e.g. 2010-01-01). Session data can start months before the
journey data.

## Example questions

- average session time by app
- which competitor do users switch to from Meesho
- sessions with order confirmation

# Schema

| column | type | meaning | policy |
|---|---|---|---|
| id | integer | Session id (referenced as session_assoc_id) | ok |
| is_active | boolean | Active flag | ok |
| is_deleted | boolean | Soft-delete flag | ok |
| created_at | timestamptz | Record load time (NOT the visit time; see Notes) | ok |
| updated_at | timestamptz | Record updated | ok |
| remarks | text | Free text | heavy |
| session_id | text | <user m_id>_<App>_<start time> | ok |
| total_session_time_min | numeric | Session length in minutes | ok |
| total_session_time_sec | numeric | Session length in seconds | ok |
| is_order_confirmation_page | boolean | Order confirmation screen seen | ok |
| order_confirmation_with_paymentselection | boolean | Confirmation after payment selection | ok |
| order_confirmation_count | integer | Number of confirmation screens | ok |
| last_cart_page_ofsession | text | Last cart page text | ok |
| last_order_review_page_of_session | text | Last order review page text | ok |
| last_hierarchy_ofsession | text | Last page type in the session | ok |
| competition_switch_bfr_5min | boolean | Switched to a competitor app within 5 minutes | ok |
| competitor_name | text | Competitor app switched to | ok |
| number_of_screens | integer | Screens viewed | ok |
| insignificant_session | boolean | Too short / noise session | ok |
| order_amount | unknown | Order amount (INR) when detected | ok |
| no_of_products_purchased | unknown | Products purchased | ok |
| no_of_searches | integer | Searches in session | ok |
| no_of_product_pages_visited | integer | Product pages viewed | ok |
| is_parent_id | unknown | Parent-session flag | ok |
