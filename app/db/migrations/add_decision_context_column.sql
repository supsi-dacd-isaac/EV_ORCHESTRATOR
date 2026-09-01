-- =============================================================================
-- Migration: add decision_context JSONB on actions
-- =============================================================================
-- Purpose
--   Store policy-variable inputs used for a decision (e.g. wind_excess_kw,
--   nominal_power_kw, forecasted energy/duration) without adding a typed column
--   per signal. Existing typed columns on actions are unchanged.
--
-- Expected payload written by the app (v1):
--   {
--     "inputs": {
--       "nominal_power_kw": ...,
--       "energy_delivered_kwh": ...,
--       "connected_time_hours": ...,
--       "forecasted_energy_kwh": ...,
--       "forecasted_duration_hours": ...,
--       "wind_excess_kw": ...
--     }
--   }
--
-- When to run
--   After add_control_algorithm_columns.sql (so actions already has the new
--   policy / suggested_* columns), and before starting the app version that
--   writes decision_context.
--
-- Safe to re-run: ADD COLUMN uses IF NOT EXISTS.
-- Historical rows stay NULL (no backfill) — only new decisions get a value.
-- =============================================================================

BEGIN;

ALTER TABLE actions
    ADD COLUMN IF NOT EXISTS decision_context JSONB DEFAULT NULL;

COMMIT;
