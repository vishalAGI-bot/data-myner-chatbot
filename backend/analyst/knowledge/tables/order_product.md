---
type: Table
title: Products in orders
description: Products inside each detected order. Columns are read from the live database.
resource: postgresql://datamyner_db/public/order_product
tags:
- order product
- products bought
status: draft
domain: analytics
grain: one row per product in an order (assumed)
primary_key: id
generated:
  by: claude/opus-5.5
  at: '2026-09-24T12:00:00+05:30'
---

# Products in orders

Products inside each detected order. Columns are read from the live database.

## Grain

one row per product in an order (assumed)

## Notes

Empty in the sample export. Verify with the data team.

# Schema

Columns are read from the live database at start-up (the sample export had no rows).
