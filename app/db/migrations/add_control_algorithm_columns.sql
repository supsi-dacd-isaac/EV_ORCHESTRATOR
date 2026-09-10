-- =============================================================================
-- Migration: add control algorithm + policy tracking to chargers and actions
-- =============================================================================
-- Run this file AFTER deploying the updated application image (Steps 5-8).
-- Run it BEFORE starting the new containers.
--
-- Safe to re-run: ADD COLUMN uses IF NOT EXISTS; RENAMEs are checked via
-- DO blocks that skip if the target column already exists.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- BLOCK 1 — chargers table
-- Add two columns that store which control algorithm and specific policy variant
-- each charger is assigned to. NULL means "use the system default rule-based
-- policy". Existing chargers are backfilled to the ANN model that was used
-- before this migration so their behaviour is unchanged.
-- -----------------------------------------------------------------------------

ALTER TABLE chargers
    ADD COLUMN IF NOT EXISTS control_algorithm VARCHAR DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS control_policy    VARCHAR DEFAULT NULL;

-- Backfill all existing chargers to the ANN they were implicitly using.
-- After the migration you can reassign individual chargers via the admin API.
UPDATE chargers
SET
    control_algorithm = 'ann',
    control_policy    = 'policy_20251204_120755_e4999_Actor.pth'
WHERE control_algorithm IS NULL;


-- -----------------------------------------------------------------------------
-- BLOCK 2 — actions table: rename existing columns
--
-- "action"  → "suggested_action"
--   The existing value ("charge" / "not_charge") is the corrected suggestion
--   sent to the charger, not necessarily what the vehicle did.  The new name
--   makes this distinction explicit and leaves room for "real_action" below.
--
-- "policy"  → "control_policy"
--   The existing value is the .pth filename used for the decision.  Renaming
--   aligns it with the new column pair (control_algorithm / control_policy)
--   and avoids a redundant copy.  No data is lost.
--
-- RENAME COLUMN is a metadata-only operation in PostgreSQL — no table rewrite,
-- no row-level locking beyond a brief catalog lock.
-- -----------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'actions' AND column_name = 'action'
    ) THEN
        ALTER TABLE actions RENAME COLUMN action TO suggested_action;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'actions' AND column_name = 'policy'
    ) THEN
        ALTER TABLE actions RENAME COLUMN policy TO control_policy;
    END IF;
END $$;


-- -----------------------------------------------------------------------------
-- BLOCK 3 — actions table: add new columns
--
-- control_algorithm   Which algorithm family produced the raw decision
--                     ("ann" or "rule_based"). NULL for rows written before
--                     this migration.
--
-- correction_applied  TRUE if the correction filter overrode the raw policy
--                     decision (e.g. charged a fully-charged vehicle → flipped
--                     to not_charge). FALSE for rows written before migration.
--
-- suggested_power_kw  The specific kW value suggested by a power-level policy.
--                     NULL for binary policies (charge / not_charge only) and
--                     for all rows written before this migration.
--
-- real_action         What the vehicle actually did, inferred retrospectively
--                     on the next charging-update request by comparing the
--                     energy_delivered_kwh delta. NULL until filled.
--
-- real_power_kw       Average actual charging power inferred from the energy
--                     delta divided by the elapsed time. NULL until filled.
-- -----------------------------------------------------------------------------

ALTER TABLE actions
    ADD COLUMN IF NOT EXISTS control_algorithm  VARCHAR          DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS correction_applied  BOOLEAN         DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS suggested_power_kw  DOUBLE PRECISION DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS real_action         VARCHAR          DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS real_power_kw       DOUBLE PRECISION DEFAULT NULL;


-- -----------------------------------------------------------------------------
-- BLOCK 4 — actions table: backfill existing rows
--
-- Every existing row was produced by the ANN, so control_algorithm = 'ann'.
-- control_policy already has the filename from the renamed "policy" column.
-- correction_applied defaults to FALSE (we cannot know retroactively).
-- suggested_power_kw stays NULL (the old ANN was binary).
-- real_action / real_power_kw stay NULL (cannot be inferred retroactively).
-- -----------------------------------------------------------------------------

UPDATE actions
SET control_algorithm = 'ann'
WHERE control_policy IS NOT NULL
  AND control_algorithm IS NULL;


-- -----------------------------------------------------------------------------
-- BLOCK 5 — intentionally omitted: no retroactive real_action / real_power_kw
--
-- Historical rows keep real_action / real_power_kw = NULL (columns added above).
-- New decisions fill these at runtime via finalize_session_action /
-- _fill_previous_action in the orchestrator.
--
-- A previous version of this migration tried to backfill from energy deltas
-- between consecutive actions, but bare `current_time` is a Postgres builtin
-- (type time with time zone), which broke timestamp subtraction against the
-- actions."current_time" column. Quoting would fix that, but backfill is not
-- required for the new app to run — skipping it keeps the migration simple.
-- -----------------------------------------------------------------------------

COMMIT;
