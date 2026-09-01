-- =============================================================================
-- Migration: add policy_signals JSONB on pilot
-- =============================================================================
-- Purpose
--   Per-pilot configuration for policy-specific external signals (how/where to
--   query them). The live per-timestep values are NOT stored here — they are
--   snapshotted into actions.decision_context when a decision is made.
--
-- Example shape (convention; flexible by design):
--   {
--     "wind_excess": {
--       "enabled": true,
--       "source": "external_db",
--       "connection": {
--         "url": null,
--         "database": null,
--         "schema": null,
--         "table": null
--       },
--       "query": {
--         "value_column": null,
--         "time_column": null,
--         "extra": {}
--       }
--     }
--   }
--
-- NULL / missing key / enabled=false → signal not configured for this pilot.
-- Safe to re-run: ADD COLUMN uses IF NOT EXISTS.
-- =============================================================================

BEGIN;

ALTER TABLE pilot
    ADD COLUMN IF NOT EXISTS policy_signals JSONB DEFAULT NULL;

COMMIT;
