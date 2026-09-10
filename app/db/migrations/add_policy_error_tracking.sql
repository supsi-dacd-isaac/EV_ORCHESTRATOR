-- =============================================================================
-- Migration: policy-failure tracking + session ordering support + error log
-- =============================================================================
-- Purpose
--   Change 1 (resilience): when policy computation fails (e.g. an ANN model
--   with a mismatched observation size), the decision falls back to the
--   default rule-based policy instead of losing the session/event. These
--   columns/table record that a fallback happened.
--
--   Change 2 (ordering checks): charging_sessions.last_event_time tracks the
--   timestamp of the most recently accepted event for a session, so new
--   events can be validated as strictly increasing without re-scanning
--   `actions` on every request.
--
-- Safe to re-run: ADD COLUMN / CREATE TABLE use IF NOT EXISTS.
-- Historical rows: policy_error defaults to FALSE, policy_error_message stays
-- NULL, last_event_time is backfilled from disconnection time (or the
-- session's own most recent action, or start_time as a last resort).
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- actions: flag + short message when a decision was produced by the fallback
-- default policy because the charger's assigned policy raised an exception.
-- -----------------------------------------------------------------------------

ALTER TABLE actions
    ADD COLUMN IF NOT EXISTS policy_error         BOOLEAN DEFAULT FALSE NOT NULL,
    ADD COLUMN IF NOT EXISTS policy_error_message VARCHAR DEFAULT NULL;

-- Allow NULL suggested_action when assigned + default policies both fail
-- (NULL = "no decision produced", distinct from an explicit "not_charge").
ALTER TABLE actions
    ALTER COLUMN suggested_action DROP NOT NULL;

-- -----------------------------------------------------------------------------
-- charging_sessions: timestamp of the last accepted vehicle_connected /
-- charging_update / vehicle_disconnected event for this session.
-- -----------------------------------------------------------------------------

ALTER TABLE charging_sessions
    ADD COLUMN IF NOT EXISTS last_event_time TIMESTAMP WITH TIME ZONE DEFAULT NULL;

-- Backfill priority: disconnection time > most recent recorded action > start_time.
-- end_time wins when present because disconnection is not itself recorded as an
-- actions row (see finalize_session_action), so it is always the true last event
-- for a closed session, chronologically after any action row.
UPDATE charging_sessions cs
SET last_event_time = COALESCE(
    cs.end_time,
    (SELECT MAX(a.current_time) FROM actions a WHERE a.id_cs = cs.id),
    cs.start_time
)
WHERE cs.last_event_time IS NULL;

-- -----------------------------------------------------------------------------
-- system_errors: generic error log for failures that are not tied to a single
-- actions row (e.g. forecaster failures) or that need cross-endpoint context.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS system_errors (
    id             UUID PRIMARY KEY,
    occurred_at    TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    source         VARCHAR NOT NULL,   -- e.g. "vehicle_connected", "compute_and_save_action"
    charger_id     UUID,
    session_id     UUID,
    error_message  TEXT NOT NULL,
    context        JSONB
);

COMMIT;
