# Knowledge bundle - DataMyner analyst

OKF v0.2 bundle describing the `public` schema of `datamyner_db`.

## Tables

- [Apps (shopping app master)](tables/app_master.md) - `app_master`
- [Category taxonomy](tables/category_master.md) - `category_master`
- [Product catalogue](tables/product_master.md) - `product_master`
- [Panel users](tables/user_master.md) - `user_master`
- [App sessions](tables/session_master.md) - `session_master`
- [Sub-app sessions](tables/sub_session_master.md) - `sub_session_master`
- [Screen events (raw accessibility log)](tables/testing_accessibility_infos.md) - `testing_accessibility_infos`
- [Product-search journeys](tables/cross_app_journey.md) - `cross_app_journey`
- [Journey search events (ordered)](tables/cross_app_journey_event.md) - `cross_app_journey_event`
- [Journey outcome windows (conversion, retention, leakage)](tables/journey_window.md) - `journey_window`
- [Detected orders](tables/order.md) - `order`
- [Products in orders](tables/order_product.md) - `order_product`
- [User journeys (legacy)](tables/user_journey.md) - `user_journey`
- [Detected searches (raw)](tables/accessibility_searches_done.md) - `accessibility_searches_done`
- [Pipeline LLM calls (operations)](tables/llm_call_log.md) - `llm_call_log`
- [Data pipeline runs](tables/pipeline_run.md) - `pipeline_run`
- [Steps inside pipeline runs](tables/pipeline_step.md) - `pipeline_step`
- [Data import jobs](tables/import_job.md) - `import_job`

## Metrics

- [Purchase share by app](metrics/purchase_share_by_app.md)
- [Conversion, retention and leakage by first-search app](metrics/conversion_by_first_app.md)
- [Where leaked purchases go](metrics/leakage_destinations.md)
- [Leakage by search category](metrics/leakage_by_category.md)
- [Who we lose to: fashion vs grocery](metrics/lost_to_by_category_group.md)
- [Time to purchase by app](metrics/time_to_purchase_by_app.md)
- [Order value by app](metrics/order_value_by_app.md)
- [Conversion within a window by category](metrics/conversion_24h_by_category.md)
- [Search category share by day](metrics/search_category_trend.md)
- [Purchase share by app per day](metrics/purchase_share_trend.md)
- [Single-app vs multi-app journeys by day](metrics/multi_app_trend.md)
- [App switches inside journeys](metrics/app_switch_paths.md)
- [What users search right before switching apps](metrics/queries_before_switch.md)
- [Competitor switching within 5 minutes](metrics/competitor_switch_by_app.md)
- [Which competitor users switch to](metrics/competitor_destinations.md)
- [Session summary by app](metrics/session_summary_by_app.md)
- [Conversion and retention by user attribute or time of day](metrics/conversion_by_user_attribute.md)
- [Repeat switchers vs single-app users](metrics/repeat_switchers.md)
- [Panel users by attribute](metrics/users_by_attribute.md)
- [Screens by page type](metrics/page_type_breakdown.md)
- [Top search queries](metrics/top_search_queries.md)
- [Journey overview](metrics/journey_overview.md)
- [Apps in the data](metrics/app_list.md)
- [Pipeline LLM usage](metrics/pipeline_llm_usage.md)

## Other

- [Glossary](glossary.md)
- [Access policy](policy.md)
