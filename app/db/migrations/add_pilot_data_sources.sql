-- =============================================================================
-- Migration: per-pilot external data sources + encrypted secrets
-- =============================================================================
-- Purpose
--   Let each pilot declare named connections to external systems (e.g. an
--   InfluxDB that serves wind_excess, or later a forecast REST API) and store
--   the credentials for those connections safely.
--
--   Two pieces:
--   1. pilot.data_sources (JSONB)  — NON-secret connection config, e.g.
--        {
--          "example_influx": {
--            "type": "influxdb",
--            "url": "https://.../influxdb/",
--            "org": "interped",
--            "bucket": "interped",
--            "token_secret": "example_influx_token"   -- references a row below
--          }
--        }
--      policy_signals entries point at one of these by name via "source".
--
--   2. pilot_secret (table)        — one row PER secret PER pilot. The token is
--      stored ENCRYPTED (Fernet) in `ciphertext`; the plaintext never touches
--      the DB. Decryption needs the app-level master key EV_SECRETS_KEY, which
--      lives only in the environment (.env.local), never in git or this DB.
--
-- Safe to re-run: ADD COLUMN / CREATE TABLE both use IF NOT EXISTS.
-- Depends on: the pilot table (baseline schema). No dependency on other
-- migrations, so ordering relative to them does not matter.
-- =============================================================================

BEGIN;

-- 1. Non-secret connection registry on the pilot row.
ALTER TABLE pilot
    ADD COLUMN IF NOT EXISTS data_sources JSONB DEFAULT NULL;

-- 2. Encrypted per-pilot secrets (one row per (pilot, secret name)).
CREATE TABLE IF NOT EXISTS public.pilot_secret (
    id          uuid NOT NULL,
    id_pilot    uuid NOT NULL,
    name        character varying NOT NULL,
    ciphertext  text NOT NULL,
    created_at  timestamp with time zone NOT NULL DEFAULT now(),
    updated_at  timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT pilot_secret_pkey PRIMARY KEY (id),
    -- A pilot cannot have two secrets with the same name; this also lets the
    -- app upsert (rotate) a token by (id_pilot, name).
    CONSTRAINT pilot_secret_pilot_name_key UNIQUE (id_pilot, name),
    -- Deleting a pilot removes its secrets automatically.
    CONSTRAINT pilot_secret_pilot_fkey FOREIGN KEY (id_pilot)
        REFERENCES public.pilot (id) ON UPDATE CASCADE ON DELETE CASCADE
);

COMMIT;
