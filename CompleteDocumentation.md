# EV Orchestrator — Complete Documentation

This document describes the EV Orchestrator service end-to-end: architecture, authentication, configuration, main HTTP APIs, and the recommended operational sequences (create a pilot, attach chargers, assign a policy, run a charging session).

Related shorter docs:

- `README.md` — Docker image / GHCR pull
- `CONFIGURATION.md` — secret vs configuration conventions
- `ev_orchestrator_deployment/README.md` — compose-based deployment

**Security note:** examples below use placeholders only (`YOUR_*`, `replace-with-…`). Never commit real `.env` files, API tokens, JWTs, or password hashes. Local E2E result dumps under `tests/_e2e_*.json` may contain live tokens — keep them untracked.

---

## 1. What the service does

EV Orchestrator is a FastAPI backend that:

1. Registers **owners** (users), **pilots** (sites), and **chargers**.
2. Receives live EV charging **events** (`vehicle_connected` → `charging_update` → `vehicle_disconnected`).
3. At each event, runs a **control policy** (rule-based or ANN) and stores an **action** (charge / not charge, optional suggested power).
4. Builds an **observation** for ANN policies from session state plus optional external signals (wind excess, site demand/generation forecast, grid net power).
5. Maintains **per-charger EV energy/duration forecasts** and optional **pilot-level** occupancy/energy forecasts (Celery + Redis).
6. Exposes read APIs for sessions, actions (`decision_context`), and forecasts.

Default decision cadence expected by clients is about **15 minutes** between updates (`valid_until` on actions is event time + 15 minutes).

---

## 2. Architecture overview

```
Clients (charger backend / operator tools)
        │  HTTPS + Bearer JWT
        ▼
┌─────────────────── FastAPI (app/main.py) ───────────────────┐
│  /auth (public)                                             │
│  /events  /sessions  /db  /forecaster  /policies  /chargers │
│  /admin/policies                                            │
│  GET /health (public)                                       │
└─────────────┬───────────────────────────────┬───────────────┘
              │                               │
              ▼                               ▼
     PostgreSQL (ORM)                   Redis + Celery
     owners, pilots, chargers,          pilot forecast jobs
     sessions, actions, forecasts,      (worker + RedBeat)
     pilot_secret (Fernet ciphertext)
```

| Component | Location | Role |
|-----------|----------|------|
| API entry | `app/main.py` | App factory, router mount, `init_db()` on startup |
| Settings | `app/config.py` | Env-driven config |
| Auth | `app/services/common/auth.py`, `authorization.py` | JWT, roles, ownership |
| Orchestrator | `app/services/orchestrator/` | Observation, policy registry, correction, persist action |
| EV forecast | `app/services/ev_forecast/` | Per-charger stats/CDF query & update |
| Load / signals | `app/services/load_forecast/`, `orchestrator/signals/` | REST demand/generation, Influx wind/grid |
| Secrets | `app/services/common/secrets.py`, `pilot_secrets.py` | Fernet encrypt/decrypt for pilot tokens |
| Celery | `app/celery_apps/` | Scheduled pilot forecasts |

### Main database entities

| Table / model | Purpose |
|---------------|---------|
| `Owners` | Users; hashed password; `role`; active JWT in `token` |
| `Pilot` | Site; `timezone_name`; `data_sources` (JSON); `policy_signals` (JSON) |
| `PilotSecret` | Named secrets (ciphertext only; never returned as plaintext) |
| `Chargers` | Hardware; links to pilot/owner; `control_algorithm` / `control_policy` |
| `ChargingSessions` | One plug session; energy, forecasts, `end_charging_time`, `active` |
| `Actions` | Per-timestep decision + `decision_context` (+ optional policy error fields) |
| `EvForecastStats` / `EvDurationCdf` | Per-charger (and generic) hour-of-day stats |
| `ForecastJobDB` / `EvPilotForecastTimeseries` | Celery pilot forecast jobs and results |
| `GridLoadForecasted` | Optional series snapshots linked to actions |

---

## 3. Roles and authentication

### Roles

| Role | How obtained | Capabilities (summary) |
|------|--------------|------------------------|
| `guest` | Default on `POST /auth/signup` | Limited read; cannot create pilots/chargers or start sessions on others’ chargers |
| `user` | Admin sets `role` | Own chargers/pilots (ownership checks); create chargers on own pilots; **cannot** create pilots |
| `user-adv` | Admin sets `role` | Everything `user` can, **plus** `POST /db/pilots` for own `id_owner` |
| `admin` | Seeded via `ADMIN_INIT_*` when owners table is empty, or set by another admin | Full admin APIs, all pilots/chargers, ANN upload, role changes |

Notes:

- JWT claims: `sub` (username), `owner_id`, `role`, `exp`.
- The presented Bearer token must match `owners.token` (logout or re-login invalidates old tokens).
- Only **admin** may change another owner’s `role` (`PUT /db/owners/{id}`).
- After a role change, the user must **log in again** so the JWT carries the new role.

### Auth endpoints (public except logout)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/auth/signup` | Create account as `guest` |
| `POST` | `/auth/login` | Issue JWT |
| `POST` | `/auth/logout` | Clear stored token (Bearer required) |
| `GET` | `/auth/owners` | Public owner directory (no passwords) |

Login body:

```json
{ "user": "YOUR_USERNAME", "password": "YOUR_PASSWORD" }
```

Use header on protected routes:

```http
Authorization: Bearer YOUR_ACCESS_TOKEN
```

---

## 4. Runtime configuration (no secrets in git)

Copy the template and edit **locally** (file is gitignored):

```bash
cp .env.example .env
```

### Variables you must set in production

| Variable | Purpose |
|----------|---------|
| `POSTGRES_PASSWORD` | Database password |
| `AUTH_SECRET_KEY` | JWT signing key (long random; e.g. `openssl rand -hex 32`) |
| `ADMIN_INIT_USER` / `ADMIN_INIT_PASSWORD` / `ADMIN_INIT_COMPANY` | First admin seed (**only** if owners table is empty) |
| `EV_SECRETS_KEY` | Fernet key for pilot API tokens (generate once, back up) |
| `DATABASE_URL` | Usually composed by Docker; override for bare-metal local |

### Non-secret operational settings

| Variable | Purpose |
|----------|---------|
| `APP_ENV` | Runtime mode |
| `ACTOR_MODEL_PATH` | Default / path context for ANN models under `models/actor/` |
| `CELERY_*` / `REDBEAT_*` | Redis broker and scheduler |
| `FORECAST_*` | Artifact dir, job manifest, artifact max age |

See `CONFIGURATION.md` and `.env.example` for the full list. **Do not** put real tokens inside `data_sources` JSON — only secret **name references** (`token_secret`, `auth_secret`).

---

## 5. Policies

Assignable policies are listed at `GET /policies`.

### Rule-based

| `control_algorithm` | `control_policy` | Behaviour |
|---------------------|------------------|-----------|
| `rule_based` | `default` | System default: charge unless fully charged; no discrete power level |
| `rule_based` | `wind_max` | Uses live `wind_excess` signal (+ energy catch-up); suggests power |

### ANN

| `control_algorithm` | `control_policy` | Behaviour |
|---------------------|------------------|-----------|
| `ann` | `<filename>.pth` | Actor model; requires matching sidecar `<stem>.json` with `observation_features` (and optional `power_levels_kw`) |

Which pilot signals you must configure depends on that sidecar’s feature list (see **§5b**). Default Actor layout includes `net_demand_forecast` / `load_level_relative` → needs demand forecast; FFF-style models may also need `grid_net_power_*_month`.

Admin upload: `POST /admin/policies/ann/upload` (`.pth` + `.json`). Feature catalog: `GET /admin/policies/observation-features`.

Assign (preferred):

```http
PATCH /chargers/{charger_id}/policy
Authorization: Bearer …
Content-Type: application/json

{
  "control_algorithm": "rule_based",
  "control_policy": "default"
}
```

Setting both fields to `null` reverts to the system default policy. Allowed for admin, charger owner, or pilot owner.

On policy failure the orchestrator **soft-falls back** to rule-based `default`, still persists the action, and records assigned vs executed policy in `decision_context` / action columns so sessions are not lost.

---

## 5b. Policy signals and data sources (what they are, when you need them)

**You do not need to configure every signal.**  
`data_sources` and `policy_signals` are optional extras. What you set depends on the **control policy** assigned to the charger:

| Target policy | Minimum pilot config |
|---------------|----------------------|
| `rule_based` / `default` | Pilot name + `id_owner` + timezone. **No** `data_sources` / `policy_signals` required. |
| `rule_based` / `wind_max` | Influx `data_sources` entry + `policy_signals.wind_excess` (+ matching secret). |
| `ann` (default 44-dim Actor) | Usually `demand_forecaster` (and optionally `generation_forecaster`) because the default sidecar includes `net_demand_forecast`. |
| `ann` with FFF / grid features in sidecar | Above **plus** Influx + `policy_signals.grid_net_power` if the sidecar lists `grid_net_power_*_month`. |

If a required signal is missing for the **assigned** ANN/rule policy, the orchestrator typically fails that policy and **falls back** to `rule_based` / `default` (session is still saved). For `wind_max` without config, wind is treated as `0.0` kW (with a warning when assigning the policy).

### How the two JSON blobs fit together

1. **`data_sources`** — named connections (how to reach Influx or a REST API). Holds URLs/org/bucket and **secret name refs** only (`token_secret` / `auth_secret`), never plaintext tokens.
2. **`policy_signals`** — named signal recipes. Each entry points at a `data_sources` name via `"source"` and describes the query/path.
3. **`pilot_secret`** — actual token values, set with `PUT /db/pilots/{id}/secrets/{name}`.

At decision time, the orchestrator only **fetches** a signal if the active policy needs it (ANN: features listed in the model sidecar; wind_max: always tries wind).

### Signal catalogue

#### `wind_excess` (`policy_signals.wind_excess`)

| | |
|--|--|
| **What it is** | Latest measured “wind excess” power (kW) for the site, usually from Influx. |
| **Where used** | **Only** `rule_based` / `wind_max` (`WindMaxPolicy.enrich_context`). Stored as `inputs.wind_excess_kw` and `signal_meta.wind_excess` on the action. **Not** used by `default` or by ANN observation features. |
| **Behaviour** | If excess ≥ charger nominal → charge at max; if above a small minimum and below nominal → charge at half nominal; else follow energy catch-up / not-charge rules. Missing/failed read → `wind_excess_kw = 0.0`. |
| **Needs** | `data_sources` Influx connection + secret; query `measurement`, `sensor_id`, optional `lookback_minutes`, `scale` (often `0.001` for W→kW). |

#### `grid_net_power` (`policy_signals.grid_net_power`)

| | |
|--|--|
| **What it is** | Calendar-month statistics of measured **grid net power** = Σ(import sensors) − Σ(export sensors). Positive = drawing **from** the grid. Stats: max, 40th and 70th percentiles over the month-to-date in the pilot timezone (15‑minute bins). |
| **Where used** | **ANN only**, and only if the model sidecar lists one or more of: `grid_net_power_max_month`, `grid_net_power_q40_month`, `grid_net_power_q70_month`. Fetched in `orchestrator_service` when those features are required; normalized into the observation; raw stats also appear under `signal_meta.grid_net_power`. **Not** used by rule-based policies. |
| **Needs** | Influx `data_sources` + secret; query `measurement`, non-empty `import_sensors`, optional `export_sensors`, `scale`, `aggregate_every`. |

#### `demand_forecaster` (`policy_signals.demand_forecaster`)

| | |
|--|--|
| **What it is** | REST call that returns a short-horizon **site demand** forecast series (typically 24 × 15‑minute steps = 6 h). |
| **Where used** | **ANN only**, when the sidecar needs `demand_forecast` and/or `net_demand_forecast` (the **default** Actor layout includes `net_demand_forecast`). Contributes to observation vectors and to `signal_meta.site_load` (raw demand curve snapshot). **Not** used by rule-based policies. |
| **Needs** | `data_sources` of type `rest_api` + `auth_secret`; signal fields `path`, `body` (often with `"{{start_time}}"`), `response_path`, `value_field`, `time_field`. Start time is floored to the 15‑minute grid before the call. |

#### `generation_forecaster` (`policy_signals.generation_forecaster`)

| | |
|--|--|
| **What it is** | Same pattern as demand, but for on-site **generation** forecast. |
| **Where used** | **ANN only**, when the sidecar needs `generation_forecast` and/or `net_demand_forecast`. Net load = demand − generation. If generation is not configured/enabled but net is required, generation is treated as **0** (net = demand) with a warning—so demand alone is often enough for default ANN. |
| **Needs** | Same REST shape as demand (can share or use a separate `data_sources` entry). Set `"enabled": false` to disable without deleting the block. |

#### `signal_meta.site_load` (runtime only — not a `policy_signals` key)

This is **not** something you put in `policy_signals`. When demand/generation are fetched for ANN, the orchestrator writes a `site_load` object into `actions.decision_context.signal_meta` (sources, timestamps, raw kW curves) for debugging/audit. Configure `demand_forecaster` / `generation_forecaster` instead.

### Policy → signal cheat sheet

| Policy | `wind_excess` | `demand_forecaster` | `generation_forecaster` | `grid_net_power` |
|--------|---------------|---------------------|-------------------------|------------------|
| `rule_based` / `default` | — | — | — | — |
| `rule_based` / `wind_max` | **Required** (else 0 kW) | — | — | — |
| `ann` default Actor (`net_demand_forecast`, `load_level_relative`) | — | **Required** (feeds net) | Optional (treated as 0 if missing) | — |
| `ann` FFF (or any sidecar with grid_* features) | — | As required by sidecar | As required by sidecar | **Required** if sidecar lists `grid_net_power_*_month` |

Check a model’s exact needs with `GET /admin/policies/observation-features` and the model’s `.json` sidecar `observation_features` list.

### Minimal vs full examples

**Minimal (default policy only):**

```json
{
  "name": "Lab-Pilot",
  "id_owner": "YOUR_OWNER_UUID",
  "timezone_name": "Europe/Zurich"
}
```

**Wind only (for `wind_max`):** Influx connection + `policy_signals.wind_excess` + secret — no demand/grid blocks.

**ANN with site net load:** REST demand connection + `demand_forecaster` (+ optional generation).

**ANN with grid month stats (e.g. FFF):** previous **plus** Influx + `grid_net_power`.

---

## 6. End-to-end sequence: new pilot → chargers → live session


This is the recommended happy path.

### Step A — Create or promote a user

1. Operator signs up (becomes `guest`):

```http
POST /auth/signup
{ "user": "site_ops", "password": "YOUR_STRONG_PASSWORD", "company_name": "Example Site Co" }
```

2. An **admin** promotes them to `user-adv` (required to create pilots):

```http
POST /auth/login
{ "user": "YOUR_ADMIN_USER", "password": "YOUR_ADMIN_PASSWORD" }

PUT /db/owners/{new_owner_id}
Authorization: Bearer ADMIN_TOKEN
{ "role": "user-adv" }
```

3. The operator logs in again and keeps `owner_id` from the login response.

### Step B — Create the pilot

**You do not need the full payload below.** Start with name / owner / timezone if you only plan to use `rule_based` / `default`. Add `data_sources` + the matching `policy_signals` entries only for the policies you will assign (see §5b).

Example of a **fully featured** pilot (wind + grid + demand) — trim unused blocks:

```http
POST /db/pilots
Authorization: Bearer OPERATOR_TOKEN
Content-Type: application/json

{
  "name": "Example-Pilot-1",
  "id_owner": "YOUR_OWNER_UUID",
  "timezone_name": "Europe/Zurich",
  "data_sources": {
    "site_influx": {
      "type": "influxdb",
      "url": "https://YOUR_INFLUX_HOST/",
      "org": "YOUR_ORG",
      "bucket": "YOUR_BUCKET",
      "token_secret": "influx_token"
    },
    "site_demand_api": {
      "type": "rest_api",
      "base_url": "https://YOUR_DEMAND_API/",
      "auth_secret": "demand_api_key",
      "auth": { "type": "header", "name": "X-API-Key" }
    }
  },
  "policy_signals": {
    "wind_excess": {
      "enabled": true,
      "source": "site_influx",
      "query": {
        "measurement": "active_power",
        "sensor_id": "YOUR_WIND_SENSOR_ID",
        "aggregate": "last",
        "lookback_minutes": 30,
        "scale": 0.001
      }
    },
    "grid_net_power": {
      "enabled": true,
      "source": "site_influx",
      "query": {
        "measurement": "active_power",
        "import_sensors": ["YOUR_IMPORT_SENSOR"],
        "export_sensors": [],
        "scale": 0.001,
        "aggregate_every": "15m"
      }
    },
    "demand_forecaster": {
      "enabled": true,
      "source": "site_demand_api",
      "path": "/forecast/YOUR_PATH",
      "body": {
        "site": "YOUR_SITE",
        "meter": "YOUR_METER",
        "start_time": "{{start_time}}"
      },
      "response_path": "demand_forecast",
      "value_field": "forecast",
      "time_field": "timestamp"
    }
  }
}
```


Rules:

- Non-admin callers must use `id_owner` equal to their own `owner_id`.
- `data_sources` must **not** contain plaintext tokens; only `token_secret` / `auth_secret` names.
- Creating a pilot also creates a **disabled** forecast job (`pilot_{id}_forecast`, kind `sim`, 15 min).

You may create a minimal pilot first (`name`, `id_owner`, `timezone_name`) and `PUT /db/pilots/{id}` later to add sources/signals.

### Step C — Store secrets (values never echoed)

```http
PUT /db/pilots/{pilot_id}/secrets/influx_token
{ "value": "YOUR_INFLUX_TOKEN_VALUE" }

PUT /db/pilots/{pilot_id}/secrets/demand_api_key
{ "value": "YOUR_DEMAND_API_KEY_VALUE" }
```

List metadata only: `GET /db/pilots/{pilot_id}/secrets`.

Connectivity checks:

```http
POST /db/pilots/{pilot_id}/data_sources/site_influx/test
POST /db/pilots/{pilot_id}/data_sources/site_demand_api/test
```

### Step D — Create chargers

```http
POST /db/chargers
Authorization: Bearer OPERATOR_TOKEN

{
  "name": "CP-01",
  "type": "AC",
  "latitude": 46.0,
  "longitude": 8.9,
  "nominal_power": 11.0,
  "plugs": "Type2",
  "id_owner": "YOUR_OWNER_UUID",
  "id_pilot": "YOUR_PILOT_UUID"
}
```

Caller must own the pilot (for `user` / `user-adv`). Guests cannot create chargers.

### Step E — Assign a control policy

```http
PATCH /chargers/{charger_id}/policy
{
  "control_algorithm": "rule_based",
  "control_policy": "default"
}
```

Examples:

- Wind-aware: `"rule_based"` + `"wind_max"` (configure `policy_signals.wind_excess` first).
- ANN FFF / Actor: `"ann"` + `"policy_….pth"` (sidecar required; may need site-load / grid features).

### Step F — (Optional) Enable pilot forecast job

```http
PATCH /forecaster/pilot/{pilot_id}/forecast_job
{ "enabled": true, "kind": "sim" }
```

Kinds include `sim`, `reg`, `reg_prob` (see API). Results: `GET /forecaster/pilot/{pilot_id}/total_energy` and `…/total_occupancy`.

### Step G — Run a charging session

Timestamps are interpreted in the **pilot timezone** when sent naive (no `Z` / offset). Prefer consistent naive local wall-clock or explicit offsets.

**1. Connect**

```http
POST /events/vehicle_connected
{
  "charger_id": "YOUR_CHARGER_UUID",
  "timestamp": "2026-07-10T09:00:00",
  "measured_power_kw": 7.0,
  "is_fully_charged": false
}
```

Creates an active session, seeds EV forecast fields, computes the first action.

**2. Periodic updates** (must be **strictly after** the last event time)

```http
POST /events/charging_update
{
  "charger_id": "YOUR_CHARGER_UUID",
  "timestamp": "2026-07-10T09:15:00",
  "avg_power_last_15min_kw": 6.5,
  "energy_delivered_kwh": 1.5,
  "is_fully_charged": false
}
```

If `is_fully_charged` becomes `true`, `end_charging_time` is stamped (fully charged only — not inferred from low power).

**3. Disconnect**

```http
POST /events/vehicle_disconnected
{
  "charger_id": "YOUR_CHARGER_UUID",
  "timestamp": "2026-07-10T10:00:00",
  "avg_power_last_15min_kw": 0.0,
  "energy_delivered_kwh": 4.0,
  "is_fully_charged": false
}
```

Closes the session; if `end_charging_time` was still null, it is set to the disconnect time. Updates charger forecast stats / duration CDF. Finalizes `real_action` / `real_power_kw` on the last action.

**4. Inspect decisions**

```http
GET /events/active_sessions
GET /sessions/actions/session/{session_id}
```

`decision_context` typically includes `inputs`, optional `signal_meta` (`site_load`, `wind_excess`, `grid_net_power`), `observation` (`features` / `values` for ANN), and `warnings`.

---

## 7. Main endpoints reference

Unless noted, routes require `Authorization: Bearer …`.

### Health

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/health` | public | `{ "status": "OK" }` |

### Auth — `/auth`

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST` | `/auth/signup` | public | Creates `guest` |
| `POST` | `/auth/login` | public | Returns JWT + `owner_id` + `role` |
| `POST` | `/auth/logout` | Bearer | Invalidates token |
| `GET` | `/auth/owners` | public | Directory listing |

### Events — `/events`

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/events/vehicle_connected` | Start session + first action (charger owner) |
| `POST` | `/events/charging_update` | Telemetry + new action; ordered timestamps |
| `POST` | `/events/vehicle_disconnected` | Close session + forecast update |
| `GET` | `/events/active_sessions` | Scoped to accessible chargers |
| `GET` | `/events/active_sessions/pilot/{pilot_id}` | Pilot-scoped |
| `GET` | `/events/active_sessions/owner/{owner_id}` | Owner-scoped |

### Sessions — `/sessions`

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/sessions/import_csv/{pilot_id}` | Historical import (may auto-create chargers by name) |
| `GET` | `/sessions/all_sessions` | List (scoped) |
| `GET` | `/sessions/all_sessions/{charger_id}` | Per charger |
| `GET` | `/sessions/owner/{owner_id}` | Per owner |
| `GET` | `/sessions/actions/session/{session_id}` | Actions + `decision_context` |

### Chargers policy — `/chargers`

| Method | Path | Notes |
|--------|------|-------|
| `PATCH` | `/chargers/{charger_id}/policy` | Assign / clear algorithm+policy |

### Policies — `/policies` and `/admin/policies`

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/policies` | Catalogue of assignable policies |
| `GET` | `/policies/ann` | List ANN `.pth` files |
| `POST` | `/admin/policies/ann/upload` | **Admin** — upload model + sidecar |
| `GET` | `/admin/policies/observation-features` | **Admin** — feature catalog |

### Forecaster — `/forecaster`

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/forecaster/charger/{id}/latest` | Effective hourly EV forecast |
| `GET` | `/forecaster/charger/{id}/history` | Historical stats rows |
| `GET` | `/forecaster/pilot/{id}/total_energy` | Latest Celery energy series |
| `GET` | `/forecaster/pilot/{id}/total_occupancy` | Latest occupancy series |
| `PATCH` | `/forecaster/pilot/{id}/forecast_job` | Enable / change job kind |

### Database CRUD — `/db` (selected)

| Resource | Create | List / Get | Update | Delete | Notable auth |
|----------|--------|------------|--------|--------|--------------|
| Owners | `POST /db/owners` | `GET …` | `PUT …` | `DELETE …` | Create/delete admin; role change admin-only |
| Pilots | `POST /db/pilots` | `GET …` | `PUT …` | `DELETE …` | Create: **admin or user-adv** |
| Pilot secrets | `PUT …/secrets/{name}` | `GET …/secrets` | (overwrite PUT) | `DELETE …/secrets/{name}` | Values never returned |
| Data source test | `POST …/data_sources/{name}/test` | | | | Live connectivity |
| Chargers | `POST /db/chargers` | `GET …` | `PUT …` | `DELETE …` | Guest forbidden on create |
| Sessions / actions / forecast tables | mostly admin writes | scoped GETs | | | Admin for most mutations |

`data_sources` on pilot read responses are visible to **admin** and the **pilot owner** only.

---

## 8. Configuration examples (sanitized)

### Minimal pilot (no external signals)

Enough for `rule_based` / `default` — **do not** set `data_sources` or `policy_signals` unless a later policy needs them:

```json
{
  "name": "Lab-Pilot",
  "id_owner": "00000000-0000-0000-0000-000000000000",
  "timezone_name": "Europe/London"
}
```

### Pilot with wind + grid + demand (structure only)

Use the Step B “fully featured” example **only if** you will run `wind_max` and/or ANN models that need those features. Omit any block you do not need (see §5b cheat sheet).

Remember:

- Secret **names** in `data_sources` must match `PUT /db/pilots/{id}/secrets/{name}`.
- `scale: 0.001` typically converts W → kW when the source stores watts.
- `{{start_time}}` in demand body is substituted by the orchestrator at query time.

### Charger create

```json
{
  "name": "Bay-A1",
  "type": "AC",
  "latitude": 51.5,
  "longitude": -0.1,
  "nominal_power": 22.0,
  "plugs": "Type2",
  "id_owner": "YOUR_OWNER_UUID",
  "id_pilot": "YOUR_PILOT_UUID",
  "control_algorithm": "rule_based",
  "control_policy": "default"
}
```

Policy can also be left unset and assigned later via `PATCH /chargers/{id}/policy`.

---

## 9. Expected sequences (quick reference)

### A. First-time deployment

1. Set production env vars (DB password, `AUTH_SECRET_KEY`, admin seed, `EV_SECRETS_KEY`).
2. Start compose stack (`db`, `redis`, `orchestrator`, `celery_worker`, `celery_beat`).
3. `GET /health` → OK.
4. Login as seeded admin; create or promote operators.

### B. Onboard a new site

1. Signup → admin sets `user-adv` → re-login.
2. `POST /db/pilots` → set secrets → test data sources.
3. `POST /db/chargers` (one or more).
4. `PATCH /chargers/{id}/policy`.
5. Optionally enable forecast job.
6. Start sending events from the charger integration.

### C. Live control loop

```
vehicle_connected
    → action_1 (policy decision)
charging_update (t+15m, …)
    → action_2, action_3, …
vehicle_disconnected
    → session closed, forecasts updated
```

Duplicate / out-of-order timestamps on update/disconnect → `4xx` with detail (ordering vs `last_event_time`).

### D. Inspect after a session

1. `GET /sessions/all_sessions/{charger_id}` or owner/pilot lists.
2. `GET /sessions/actions/session/{session_id}` — verify `control_algorithm` / `control_policy` and `decision_context`.
3. `GET /forecaster/charger/{id}/latest` — updated hour stats.

---

## 10. Timezones and forecasts

- Each pilot has `timezone_name` (default `Europe/Zurich`).
- Naive event timestamps are treated as **pilot-local wall clock**, then stored in UTC.
- API responses that display times are converted back to pilot-local where applicable.
- Per-charger EV forecast buckets use **local hour of connection**.
- A shared **generic charger** stats row is used when charger-specific stats are missing; generic rebuild applies outlier caps (e.g. energy / duration limits) in the updater.

---

## 11. Correction layer

Every policy suggestion passes through a correction filter before persistence, for example:

- Do not charge if already fully charged / disconnected semantics require it.
- Clamp suggested power to charger `nominal_power`.

Clients should treat `suggested_action` / `suggested_power_kw` on the action as the authoritative command for the next interval.

---

## 12. Local live E2E script

`tests/run_e2e_localhost.py` exercises login, catalogues, July session simulations across policies, guest authz, and a full guest→`user-adv`→pilot→charger→session lifecycle against `http://localhost:8000`.

It writes `tests/_e2e_results.json`. That file can contain **live JWTs** — do not commit it (ignored via `.gitignore`).

---

## 13. Deployment pointer

See `ev_orchestrator_deployment/README.md` for image load, `.env` creation, compose up/down, volumes (`pg_data`, `forecast_artifacts`), and log commands. API default port: **8000**.

Published image (from root README):

```bash
docker pull ghcr.io/supsi-dacd-isaac/ev_orchestrator:latest
```

---

## 14. Operational checklist

- [ ] Production secrets set; `.env` not in git
- [ ] Admin seed password changed after first login if using example placeholders
- [ ] `EV_SECRETS_KEY` backed up before storing pilot tokens
- [ ] Pilot timezone correct for the site
- [ ] Only configure `data_sources` / `policy_signals` required by the policies you assign (§5b)
- [ ] Secrets created **before** enabling signal-dependent policies (`wind_max`, ANN with site-load/grid)
- [ ] Data-source tests return OK (for connections you actually configured)- [ ] Policy assigned and visible on `GET /db/chargers/{id}`
- [ ] Event timestamps strictly increasing per session
- [ ] After role changes, clients re-login

---

*Generated from the EV_ORCHESTRATOR codebase structure (FastAPI routes, schemas, orchestrator, forecast, and auth modules). Keep this file updated when APIs or configuration contracts change.*
