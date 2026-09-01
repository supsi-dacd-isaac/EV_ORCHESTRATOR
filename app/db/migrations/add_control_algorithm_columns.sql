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
-- BLOCK 5 — actions table: retroactive backfill of real_action / real_power_kw
--
-- Actions.energy_delivered_kwh is the cumulative energy at action time.
-- The delta between consecutive rows within the same session equals the energy
-- actually delivered during that 15-minute interval.
--
-- Part A: all rows that have a following row in the same session (LEAD window).
-- Part B: the last row of each completed session, using the session final total.
-- Rows from still-active sessions and isolated single-action sessions keep NULL.
-- -----------------------------------------------------------------------------

-- Part A: rows with a known following row
WITH next_values AS (
    SELECT
        id,
        LEAD(energy_delivered_kwh) OVER (PARTITION BY id_cs ORDER BY current_time) AS next_energy,
        LEAD(current_time)         OVER (PARTITION BY id_cs ORDER BY current_time) AS next_time
    FROM actions
)
UPDATE actions a
SET
    real_action   = CASE
                      WHEN (nv.next_energy - a.energy_delivered_kwh) > 0.05 THEN 'charge'
                      ELSE 'not_charge'
                    END,
    real_power_kw = (nv.next_energy - a.energy_delivered_kwh) /
                    GREATEST(
                        EXTRACT(EPOCH FROM (nv.next_time - a.current_time)) / 3600.0,
                        0.001
                    )
FROM next_values nv
WHERE a.id = nv.id
  AND nv.next_time IS NOT NULL
  AND a.real_action IS NULL;

-- Part B: last row of each completed session (no following action row exists)
UPDATE actions a
SET
    real_action   = CASE
                      WHEN (cs.energy_delivered_kwh - a.energy_delivered_kwh) > 0.05 THEN 'charge'
                      ELSE 'not_charge'
                    END,
    real_power_kw = (cs.energy_delivered_kwh - a.energy_delivered_kwh) /
                    GREATEST(
                        EXTRACT(EPOCH FROM (cs.end_time - a.current_time)) / 3600.0,
                        0.001
                    )
FROM charging_sessions cs
WHERE a.id_cs = cs.id
  AND cs.active = FALSE
  AND a.real_action IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM actions a2
      WHERE a2.id_cs = a.id_cs
        AND a2.current_time > a.current_time
  );

COMMIT;
