-- =============================================================================
-- Migration: drop legacy pilot.forecast_meter / forecast_site
-- =============================================================================
-- These columns pointed at a single global BASE_LOAD_FORECAST_API_URL shape.
-- Load forecasts are now configured per pilot via data_sources (rest_api) +
-- policy_signals.demand_forecaster / generation_forecaster (Phase 4B).
--
-- Safe to re-run: DROP COLUMN IF EXISTS.
-- =============================================================================

BEGIN;

ALTER TABLE pilot DROP COLUMN IF EXISTS forecast_meter;
ALTER TABLE pilot DROP COLUMN IF EXISTS forecast_site;

COMMIT;
