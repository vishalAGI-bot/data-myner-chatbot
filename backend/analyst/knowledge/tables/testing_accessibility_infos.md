---
type: Table
title: Screen events (raw accessibility log)
description: 'Raw screen-level activity captured on the phone: one row per screen event, classified by app, page type, cart
  and order signals.'
resource: postgresql://datamyner_db/public/testing_accessibility_infos
tags:
- event
- screen
- page
- page type
- click
- cart
- add to cart
- product page
- home
- weekday
- day
status: stable
domain: analytics
grain: one row per captured screen event (very large table)
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Screen events (raw accessibility log)

Raw screen-level activity captured on the phone: one row per screen event, classified by app, page type, cart and order signals.

## Grain

one row per captured screen event (very large table)

## Joins

- `testing_accessibility_infos.app_assoc_id = app_master.id`
- `testing_accessibility_infos.session_assoc_id = session_master.id`
- `testing_accessibility_infos.user_assoc_id = user_master.id`
- `testing_accessibility_infos.product_assoc_id = product_master.id`
- `testing_accessibility_infos.category_assoc_id = category_master.id`
- `testing_accessibility_infos.sub_session_assoc_id = sub_session_master.id`
- `cross_app_journey_event.source_accessibility_id = testing_accessibility_infos.id`

## Notes

Very large: always filter (date, app, user) and aggregate. page_type values include Home, Search Results, Product Detail Page, Cart, Order Confirmation Page, Unclassified. final_appname is the app name. Raw text columns are blocked from output.

## Example questions

- page views by page type
- add to cart events by app
- events by day of week

# Schema

| column | type | meaning | policy |
|---|---|---|---|
| id | integer | Event id | ok |
| is_active | boolean |  | ok |
| is_deleted | boolean |  | ok |
| created_at | timestamptz |  | ok |
| updated_at | timestamptz |  | ok |
| remarks | text | (hidden) | heavy |
| m_id | text | User device key | ok |
| date | timestamptz | Event time | ok |
| event_type | text | Android accessibility event type | ok |
| event_text | text | (hidden) | heavy |
| version | integer |  | ok |
| createdat | timestamptz |  | ok |
| updatedat | timestamptz |  | ok |
| pageid | text | Screen id | ok |
| weekday | boolean | True on weekdays | ok |
| day_name | text | Day name | ok |
| half_of_the_month | text | First Half / Second Half | ok |
| resolved_sub_section | text | Main App or sub-app name | ok |
| final_appname | text | App name | ok |
| page_type | text | Classified page type | ok |
| matched_page_type_count | integer |  | ok |
| all_matched_pagetype | text | (hidden) | heavy |
| all_matched_content | text | (hidden) | heavy |
| reason | text | (hidden) | heavy |
| clicked_on_content_found | unknown |  | ok |
| clicked_on_content | unknown | (hidden) | heavy |
| add_to_cart_keyword_present | text | Add-to-cart text present | ok |
| add_to_cart_count | integer | Add-to-cart count | ok |
| session_status | text | Session status | ok |
| is_order_confirmation_page | boolean | Order confirmation screen | ok |
| order_confirmation_with_paymentselection | boolean |  | ok |
| cart_quantity | text | Cart quantity text | ok |
| cart_status | text | Cart status text | ok |
| cart_value | unknown | Cart value | ok |
| order_confirmation_count | integer |  | ok |
| last_cart_page_ofsession | text |  | ok |
| last_orderreview_page_ofsession | text |  | ok |
| last_hierarchy_of_session | text |  | ok |
| competition_switch_bfr_5min | boolean | Competitor switch within 5 min | ok |
| number_of_screens | integer | Screens so far in session | ok |
| order_amount | unknown | Order amount | ok |
| no_of_products_purchased | unknown | Products purchased | ok |
| is_searches | boolean | Screen is a search | ok |
| is_product_pages_visited | boolean | Screen is a product page | ok |
| no_of_searches | integer | Searches so far in session | ok |
| no_of_product_pages_visited | integer | Product pages so far in session | ok |
| competitor_name | text | Competitor app | ok |
| matched_from_previous_page | numeric |  | ok |
| matched_from_previous_page_reason | text | (hidden) | heavy |
| matched_from_next_page | numeric |  | ok |
| matched_from_next_page_reason | text | (hidden) | heavy |
| unique_page_insession | boolean |  | ok |
| raw_payload | unknown | (hidden) | heavy |
| product_assoc_id | unknown | Product id | ok |
| search_done | unknown | Search text detected | ok |
| processed_at | unknown | Pipeline processed time | ok |
| normalized_query | unknown | Normalised search query | ok |
| canonical_query | unknown | Canonical search query | ok |
| app_assoc_id | integer | App id | ok |
| category_assoc_id | unknown | Category id | ok |
| session_assoc_id | integer | Session id | ok |
| sub_session_assoc_id | unknown | Sub-session id | ok |
| user_assoc_id | integer | User id | ok |
