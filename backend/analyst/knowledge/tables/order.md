---
type: Table
title: Detected orders
description: Orders detected on panel phones (referenced by journey_window.purchase_order_id). Columns are read from the live
  database.
resource: postgresql://datamyner_db/public/order
tags:
- order
- orders
- purchase
status: draft
domain: analytics
grain: one row per order (assumed)
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Detected orders

Orders detected on panel phones (referenced by journey_window.purchase_order_id). Columns are read from the live database.

## Grain

one row per order (assumed)

## Joins

- `journey_window.purchase_order_id = "order".id`
- `order_product.order_assoc_id = "order".id (assumed)`

## Notes

Empty in the sample export; the table name must be quoted as "order". Verify columns with the data team.

## Example questions

- orders per app

# Schema

Columns are read from the live database at start-up (the sample export had no rows).
