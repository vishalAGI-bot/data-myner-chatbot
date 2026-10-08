-- Run once as a database admin. Creates a login the chatbot uses, which can only
-- READ the documented business tables and never sees personal columns.
-- Replace the password, then put these credentials in backend/.env (DB_USERNAME / DB_PASSWORD).

CREATE ROLE datamyner_chatbot LOGIN PASSWORD 'change-me-strong-password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 10;
ALTER ROLE datamyner_chatbot SET default_transaction_read_only = on;
ALTER ROLE datamyner_chatbot SET statement_timeout = '15s';
ALTER ROLE datamyner_chatbot SET idle_in_transaction_session_timeout = '30s';

GRANT CONNECT ON DATABASE datamyner_db TO datamyner_chatbot;
GRANT USAGE ON SCHEMA public TO datamyner_chatbot;

-- Whole-table read access (no personal data in these tables).
GRANT SELECT ON public.app_master, public.category_master, public.product_master,
                public.cross_app_journey, public.cross_app_journey_event, public.journey_window,
                public.session_master, public.sub_session_master, public.testing_accessibility_infos,
                public."order", public.order_product, public.user_journey, public.accessibility_searches_done,
                public.llm_call_log, public.pipeline_run, public.pipeline_step, public.import_job
      TO datamyner_chatbot;

-- user_master: column-level grant WITHOUT name, phone, email, address, password, device ids.
GRANT SELECT (id, m_id, city, state, zone, tier, gender, marital_status, date_of_birth, pincode, population,
              device_model, android_version, app_version, current_status, total_points, new_total_points,
              is_active, is_deleted, date_joined, created_at, updated_at, version)
      ON public.user_master TO datamyner_chatbot;

-- Nothing on auth_*, django_*, authtoken_token, analytics_token, internal_user*, llm_api_key,
-- llm_provider, llm_configuration, llm_prompt_template, audit_log.
