# EV Orchestrator

FastAPI service that registers pilots and chargers, accepts live charging events, and returns a control action (charge or not, and an optional power setpoint) from a rule-based or ANN policy. It also keeps per-charger energy and duration forecasts, and optional pilot-level occupancy and energy forecasts.

Licensed under the [MIT License](LICENSE). Copyright 2026 SUPSI.

Examples below use placeholders (`YOUR_*`). Do not commit real `.env` files, API tokens, JWTs, or password hashes. Local E2E dumps under `tests/_e2e_*.json` can contain live tokens and are gitignored.

## Contents

- [Docker image](#docker-image)
- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Authentication and roles](#authentication-and-roles)
- [Runtime configuration](#runtime-configuration)
- [Policies](#policies)
- [Policy signals](#policy-signals)
- [Operate a site](#operate-a-site)
- [Charging session](#charging-session)
- [Endpoints](#endpoints)
- [Timezones, forecasts, and correction](#timezones-forecasts-and-correction)
- [Tests](#tests)
- [Checklist](#checklist)

Other docs: [`CONFIGURATION.md`](CONFIGURATION.md) (what is a secret vs configuration), [`ev_orchestrator_e2e_selfcontained/README.md`](ev_orchestrator_e2e_selfcontained/README.md) (deployed API test package). A compose deployment guide lives in `ev_orchestrator_deployment/README.md` when that package is present.

## Docker image

A GitHub Release publishes an image to GitHub Container Registry. The repository and the GHCR package are configured separately: if GitHub creates the package as private, set it to public in the package settings.

```bash
docker pull ghcr.io/supsi-dacd-isaac/ev_orchestrator:latest
```

The API listens on port **8000**. A full stack is `db`, `redis`, `orchestrator`, `celery_worker`, and `celery_beat`. Named volumes typically used in deployment are `pg_data` (Postgres) and `forecast_artifacts` (trained forecast models).

First start:

1. Copy `.env.example` to `.env` and set the production secrets listed below.
2. Start the stack.
3. `GET /health` returns `{ "status": "OK" }`.
4. Log in as the seeded admin and create or promote operators.

## What it does

1. Registers **owners** (users), **pilots** (sites), and **chargers**.
2. Receives live events: `vehicle_connected`, then `charging_update`, then `vehicle_disconnected`.
3. On each event, runs the charger's **control policy** and stores an **action**.
4. Builds an ANN **observation** from session state plus optional external signals (wind excess, site demand and generation, grid net power).
5. Maintains per-charger EV forecasts and optional pilot-level forecasts (Celery and Redis).
6. Exposes read APIs for sessions, actions (`decision_context`), and forecasts.

Clients are expected to update about every **15 minutes**. `valid_until` on an action is the event time plus 15 minutes.

## Architecture

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
     PostgreSQL                         Redis + Celery
     owners, pilots, chargers,          pilot forecast jobs
     sessions, actions, forecasts,      (worker + RedBeat)
     pilot_secret (Fernet ciphertext)
```

| Component | Location | Role |
|-----------|----------|------|
| API entry | `app/main.py` | App factory, routers, `init_db()` on startup |
| Settings | `app/config.py` | Environment-driven config |
| Auth | `app/services/common/auth.py`, `authorization.py` | JWT, roles, ownership |
| Orchestrator | `app/services/orchestrator/` | Observation, policies, correction, persist action |
| EV forecast | `app/services/ev_forecast/` | Per-charger stats and duration CDF |
| Load / signals | `app/services/load_forecast/`, `orchestrator/signals/` | REST demand/generation, Influx wind/grid |
| Secrets | `app/services/common/secrets.py`, `pilot_secrets.py` | Fernet encrypt/decrypt for pilot tokens |
| Celery | `app/celery_apps/` | Scheduled pilot forecasts |

| Table | Purpose |
|-------|---------|
| `Owners` | Users; hashed password; `role`; active JWT in `token` |
| `Pilot` | Site; `timezone_name`; `data_sources`; `policy_signals` |
| `PilotSecret` | Named secrets (ciphertext only; never returned as plaintext) |
| `Chargers` | Hardware; `control_algorithm` / `control_policy` |
| `ChargingSessions` | One plug-in; energy, forecasts, `end_charging_time`, `active` |
| `Actions` | Decision plus `decision_context` |
| `EvForecastStats` / `EvDurationCdf` | Per-charger (and generic) hour-of-day stats |
| `ForecastJobDB` / `EvPilotForecastTimeseries` | Celery pilot forecast jobs and results |
| `GridLoadForecasted` | Optional series snapshots linked to actions |

## Authentication and roles

| Role | How obtained | What it can do |
|------|--------------|----------------|
| `guest` | Default on `POST /auth/signup` | Limited read. Cannot create pilots or chargers, or start sessions on someone else's charger |
| `user` | Admin sets `role` | Own chargers and pilots. Can create chargers on own pilots. Cannot create pilots |
| `user-adv` | Admin sets `role` | Everything `user` can, plus `POST /db/pilots` for their own `id_owner` |
| `admin` | Seeded from `ADMIN_INIT_*` when the owners table is empty, or set by another admin | All pilots and chargers, ANN upload, role changes |

JWT claims are `sub` (username), `owner_id`, `role`, and `exp`. The Bearer token must match `owners.token`; logout or a new login invalidates older tokens. Only an admin can change another owner's `role`. After a role change the user must log in again so the new JWT carries the new role.

```http
POST /auth/login
{ "user": "YOUR_USERNAME", "password": "YOUR_PASSWORD" }
```

Protected routes:

```http
Authorization: Bearer YOUR_ACCESS_TOKEN
```

| Method | Path | Auth |
|--------|------|------|
| `POST` | `/auth/signup` | public; creates `guest` |
| `POST` | `/auth/login` | public; returns JWT, `owner_id`, `role` |
| `POST` | `/auth/logout` | Bearer; clears the stored token |
| `GET` | `/auth/owners` | public directory; no passwords |

## Runtime configuration

```bash
cp .env.example .env
```

`.env` is gitignored. `.env.example` contains placeholders only.

| Variable | Purpose |
|----------|---------|
| `POSTGRES_PASSWORD` | Database password |
| `AUTH_SECRET_KEY` | JWT signing key (long random value, for example `openssl rand -hex 32`) |
| `ADMIN_INIT_USER` / `ADMIN_INIT_PASSWORD` / `ADMIN_INIT_COMPANY` | First admin, used only when the owners table is empty |
| `EV_SECRETS_KEY` | Fernet key for pilot API tokens. Generate once and back it up |
| `DATABASE_URL` | Usually composed by Docker. Override for a local Postgres |

| Variable | Purpose |
|----------|---------|
| `APP_ENV` | Runtime mode |
| `ACTOR_MODEL_PATH` | ANN model path under `models/actor/` |
| `CELERY_*` / `REDBEAT_*` | Redis broker and scheduler |
| `FORECAST_*` | Artifact directory, job manifest, artifact max age |

See `CONFIGURATION.md` and `.env.example` for the full list. Do not put plaintext tokens in `data_sources`. Store only secret **names** (`token_secret`, `auth_secret`) and set the values with the secrets API.

## Policies

`GET /policies` lists what you can assign. Preferred assignment:

```http
PATCH /chargers/{charger_id}/policy
Authorization: Bearer …
Content-Type: application/json

{
  "control_algorithm": "rule_based",
  "control_policy": "default"
}
```

Setting both fields to `null` reverts to the system default. Allowed for admin, the charger owner, or the pilot owner.

| `control_algorithm` | `control_policy` | Behaviour |
|---------------------|------------------|-----------|
| `rule_based` | `default` | Charge unless fully charged. No discrete power level. No external signals |
| `rule_based` | `wind_max` | Uses live `wind_excess` and energy catch-up. Suggests a power |
| `ann` | `<filename>.pth` | Actor model. Needs a matching `<stem>.json` sidecar with `observation_features` and optional `power_levels_kw` |

Admin upload is `POST /admin/policies/ann/upload` (`.pth` plus `.json`). The feature catalogue is `GET /admin/policies/observation-features`.

If the assigned policy fails, the orchestrator falls back to rule-based `default`, still stores the action, and records the assigned policy versus the one that actually ran. The session is not dropped.

Every suggestion then passes a correction filter: do not charge when the vehicle is fully charged, and clamp suggested power to the charger's `nominal_power`. Clients should treat `suggested_action` and `suggested_power_kw` as the command for the next interval.

## Policy signals

You do not configure every signal. `data_sources` and `policy_signals` are optional. What you set depends on the policy assigned to the charger.

| Target policy | Minimum pilot config |
|---------------|----------------------|
| `rule_based` / `default` | Name, `id_owner`, timezone. No `data_sources` or `policy_signals` |
| `rule_based` / `wind_max` | Influx connection plus `policy_signals.wind_excess` and its secret |
| `ann` default Actor (`net_demand_forecast`, `load_level_relative`) | `demand_forecaster`. `generation_forecaster` is optional |
| `ann` whose sidecar lists `grid_net_power_*_month` | The demand setup plus Influx and `policy_signals.grid_net_power` |

If a required signal is missing, that policy fails and the orchestrator falls back to `rule_based` / `default`. `wind_max` without config uses `wind_excess_kw = 0.0` and warns when you assign the policy.

Three pieces fit together:

1. **`data_sources`** — named connections (Influx or REST). URLs, org, and bucket only. Secret fields are names, not values.
2. **`policy_signals`** — recipes. Each entry points at a connection with `"source"`.
3. **`pilot_secret`** — the token values, set with `PUT /db/pilots/{id}/secrets/{name}`. The API never returns them.

A signal is fetched only when the active policy needs it.

| Signal | What it is | Where it is used | If it is missing |
|--------|------------|------------------|------------------|
| `wind_excess` | Latest wind-excess power (kW), usually from Influx | `rule_based` / `wind_max` only. Stored as `inputs.wind_excess_kw` and `signal_meta.wind_excess`. Excess at or above nominal power charges at max; a smaller excess charges at half nominal | Treated as `0.0` kW |
| `demand_forecaster` | REST site-demand forecast, typically 24 steps of 15 minutes | ANN features `demand_forecast` and `net_demand_forecast` (the default Actor includes the latter). Raw curve is copied to `signal_meta.site_load` | ANN policy falls back to default |
| `generation_forecaster` | Same shape, for on-site generation | ANN `generation_forecast` and `net_demand_forecast`. Net = demand − generation | Generation is treated as 0, so net equals demand, with a warning |
| `grid_net_power` | Month-to-date grid net power in the pilot timezone: sum of import sensors minus sum of export sensors. Positive means drawing from the grid. Stats are max, 40th percentile, and 70th percentile | ANN only, and only if the sidecar lists `grid_net_power_max_month`, `grid_net_power_q40_month`, or `grid_net_power_q70_month` | ANN policy falls back to default |

`signal_meta.site_load` is not a config key. It is written at decision time when demand or generation was fetched.

| Policy | `wind_excess` | `demand_forecaster` | `generation_forecaster` | `grid_net_power` |
|--------|---------------|---------------------|-------------------------|------------------|
| `rule_based` / `default` | — | — | — | — |
| `rule_based` / `wind_max` | Required (else 0 kW) | — | — | — |
| `ann` default Actor | — | Required | Optional (0 if missing) | — |
| `ann` with `grid_net_power_*_month` | — | As the sidecar requires | As the sidecar requires | Required |

Check a model in its `.json` sidecar (`observation_features`) and with `GET /admin/policies/observation-features`.

Influx queries usually set `"scale": 0.001` to convert watts to kilowatts. Demand bodies may contain `"{{start_time}}"`, which the orchestrator replaces after flooring the time to a 15-minute boundary. Set `"enabled": false` on a signal block to disable it without deleting it.

## Operate a site

Order of work: sign up, promote to `user-adv`, create the pilot, store secrets only if the policy needs them, create chargers, assign a policy, optionally enable the forecast job, then send events.

### 1. User who can create a pilot

```http
POST /auth/signup
{ "user": "site_ops", "password": "YOUR_STRONG_PASSWORD", "company_name": "Example Site Co" }
```

An admin promotes that account. The operator must log in again afterwards.

```http
PUT /db/owners/{new_owner_id}
Authorization: Bearer ADMIN_TOKEN
{ "role": "user-adv" }
```

### 2. Pilot

A non-admin must set `id_owner` to their own id. Creating a pilot also creates a **disabled** forecast job (`pilot_{id}_forecast`, kind `sim`, every 15 minutes). You can add sources later with `PUT /db/pilots/{id}`.

Minimal pilot, enough for `rule_based` / `default`:

```json
{
  "name": "Lab-Pilot",
  "id_owner": "YOUR_OWNER_UUID",
  "timezone_name": "Europe/Zurich"
}
```

Fully featured pilot. Omit every block you do not need:

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

Secret names in `data_sources` must match the secrets API. Values are never echoed back.

```http
PUT /db/pilots/{pilot_id}/secrets/influx_token
{ "value": "YOUR_INFLUX_TOKEN_VALUE" }

PUT /db/pilots/{pilot_id}/secrets/demand_api_key
{ "value": "YOUR_DEMAND_API_KEY_VALUE" }
```

`GET /db/pilots/{pilot_id}/secrets` returns metadata only. Connectivity:

```http
POST /db/pilots/{pilot_id}/data_sources/site_influx/test
POST /db/pilots/{pilot_id}/data_sources/site_demand_api/test
```

`data_sources` on a pilot read are visible to the admin and the pilot owner only.

### 3. Charger and policy

The caller must own the pilot. Guests cannot create chargers. Policy fields can be omitted and set later.

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
  "id_pilot": "YOUR_PILOT_UUID",
  "control_algorithm": "rule_based",
  "control_policy": "default"
}
```

- Wind-aware: `"rule_based"` and `"wind_max"`, after `wind_excess` is configured.
- ANN: `"ann"` and the `.pth` filename. The sidecar must exist. Site-load or grid features must be configured if that model lists them.

### 4. Optional pilot forecast

```http
PATCH /forecaster/pilot/{pilot_id}/forecast_job
{ "enabled": true, "kind": "sim" }
```

Kinds include `sim`, `reg`, and `reg_prob`. Results: `GET /forecaster/pilot/{pilot_id}/total_energy` and `…/total_occupancy`.

## Charging session

Naive timestamps (no `Z` and no offset) are **pilot-local wall clock**, then stored in UTC. Responses that display times are converted back to the pilot timezone. Updates and disconnect must be **strictly after** `last_event_time`; otherwise the API returns `4xx`.

**Connect** — creates the session, seeds EV forecast fields, stores the first action.

```http
POST /events/vehicle_connected
{
  "charger_id": "YOUR_CHARGER_UUID",
  "timestamp": "2026-07-10T09:00:00",
  "measured_power_kw": 7.0,
  "is_fully_charged": false
}
```

**Update** — about every 15 minutes.

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

`end_charging_time` is set only when `is_fully_charged` is true. Low power is not treated as "done", so a policy pause is not confused with a full battery.

**Disconnect** — closes the session. If `end_charging_time` is still null, it is set to the disconnect time. Forecast stats and the duration CDF are updated. The last action gets `real_action` and `real_power_kw`.

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

Inspect with `GET /events/active_sessions` and `GET /sessions/actions/session/{session_id}`. `decision_context` holds `inputs`, optional `signal_meta` (`site_load`, `wind_excess`, `grid_net_power`), `observation` (`features` and `values` for ANN), and `warnings`.

```
vehicle_connected        → action 1
charging_update (t+15m)  → action 2, action 3, …
vehicle_disconnected     → session closed, forecasts updated
```

Per-charger forecast buckets use the **local hour of connection**. If that charger has no stats yet, a shared generic row is used. Generic rebuilds drop outliers above the updater caps (energy and duration).

## Endpoints

Unless noted, routes require `Authorization: Bearer …`.

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/health` | Public. `{ "status": "OK" }` |
| `POST` | `/events/vehicle_connected` | Start session and first action. Charger owner |
| `POST` | `/events/charging_update` | Telemetry and a new action. Ordered timestamps |
| `POST` | `/events/vehicle_disconnected` | Close session and update forecasts |
| `GET` | `/events/active_sessions` | Scoped to accessible chargers |
| `GET` | `/events/active_sessions/pilot/{pilot_id}` | Pilot-scoped |
| `GET` | `/events/active_sessions/owner/{owner_id}` | Owner-scoped |
| `POST` | `/sessions/import_csv/{pilot_id}` | Historical import. May create chargers by name |
| `GET` | `/sessions/all_sessions` | Scoped list |
| `GET` | `/sessions/all_sessions/{charger_id}` | One charger |
| `GET` | `/sessions/owner/{owner_id}` | One owner |
| `GET` | `/sessions/actions/session/{session_id}` | Actions and `decision_context` |
| `PATCH` | `/chargers/{charger_id}/policy` | Assign or clear algorithm and policy |
| `GET` | `/policies` | Assignable policies |
| `GET` | `/policies/ann` | ANN `.pth` filenames |
| `POST` | `/admin/policies/ann/upload` | Admin. Model plus sidecar |
| `GET` | `/admin/policies/observation-features` | Admin. Feature catalogue |
| `GET` | `/forecaster/charger/{id}/latest` | Effective hourly EV forecast |
| `GET` | `/forecaster/charger/{id}/history` | Historical stats |
| `GET` | `/forecaster/pilot/{id}/total_energy` | Latest Celery energy series |
| `GET` | `/forecaster/pilot/{id}/total_occupancy` | Latest occupancy series |
| `PATCH` | `/forecaster/pilot/{id}/forecast_job` | Enable or change job kind |

| Resource | Create | Read | Update | Delete | Auth note |
|----------|--------|------|--------|--------|-----------|
| Owners | `POST /db/owners` | `GET /db/owners` | `PUT /db/owners/{id}` | `DELETE` | Create and delete: admin. Role change: admin |
| Pilots | `POST /db/pilots` | `GET /db/pilots` | `PUT` | `DELETE` | Create: admin or `user-adv` |
| Pilot secrets | `PUT …/secrets/{name}` | `GET …/secrets` | same PUT | `DELETE …/secrets/{name}` | Values never returned |
| Data source test | `POST …/data_sources/{name}/test` | | | | Live connectivity check |
| Chargers | `POST /db/chargers` | `GET /db/chargers` | `PUT` | `DELETE` | Guests cannot create |
| Sessions, actions, forecast tables | mostly admin | scoped `GET` | mostly admin | mostly admin | |

## Timezones, forecasts, and correction

- Each pilot has `timezone_name` (default `Europe/Zurich`).
- Naive event timestamps are pilot-local, stored as UTC, and shown again in the pilot timezone.
- Forecast hour buckets use the local hour of connection.
- Suggested power is clamped to `nominal_power`. A fully charged vehicle is not told to charge.

## Tests

`tests/run_e2e_localhost.py` runs against `http://localhost:8000`: login, catalogues, July sessions across policies, guest limits, and a guest → `user-adv` → pilot → charger → session flow. It writes `tests/_e2e_results.json`, which can contain JWTs. Do not commit that file.

`ev_orchestrator_e2e_selfcontained/` is a separate package for a deployed API. Credentials come from the environment (`EV_ORCH_ADMIN_USER`, `EV_ORCH_ADMIN_PASSWORD`, `EV_ORCH_TEST_PASSWORD`, optional `EV_ORCH_BASE_URL`). See that folder's README.

## Checklist

- [ ] Production secrets are set and `.env` is not in git
- [ ] The admin seed password is changed if it was still a placeholder
- [ ] `EV_SECRETS_KEY` is backed up before any pilot token is stored
- [ ] The pilot timezone matches the site
- [ ] `data_sources` and `policy_signals` include only what the assigned policies need
- [ ] Secrets exist before `wind_max` or an ANN that needs site-load or grid data
- [ ] Data-source tests succeed for the connections you configured
- [ ] The policy is visible on `GET /db/chargers/{id}`
- [ ] Event timestamps increase on every update and disconnect
- [ ] Clients log in again after a role change
