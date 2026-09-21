# EV Orchestrator E2E self-contained test package

This folder contains a two-step test setup for the deployed EV Orchestrator API.

The package reads the deployment URL and credentials from the environment. Do not commit passwords.

```text
EV_ORCH_BASE_URL          default http://localhost:8000
EV_ORCH_ADMIN_USER
EV_ORCH_ADMIN_PASSWORD
EV_ORCH_TEST_USER         default example_user1
EV_ORCH_TEST_PASSWORD
```

## Files

```text
.
├── generate_charging_schedule.py   # Step 1: creates the synthetic CSV schedule
├── run_e2e_api_test.py             # Step 2: creates/reuses API objects and executes the schedule
├── README.md                       # This explanation
└── outputs/
    ├── synthetic_charging_schedule.csv
    ├── synthetic_session_summary.csv
    ├── synthetic_generation_summary.json
    └── plots/
        ├── connection_disconnection_events.png
        ├── active_sessions_total.png
        ├── daily_energy_by_charger.png
        ├── cumulative_energy.png
        └── charging_rate.png
```

## Step 1 — Generate the synthetic charging schedule

Open `generate_charging_schedule.py` in PyCharm and run it.

Or from a terminal:

```bash
python generate_charging_schedule.py
```

This creates:

```text
outputs/synthetic_charging_schedule.csv
outputs/synthetic_session_summary.csv
outputs/synthetic_generation_summary.json
outputs/plots/connection_disconnection_events.png
outputs/plots/active_sessions_total.png
outputs/plots/daily_energy_by_charger.png
outputs/plots/cumulative_energy.png
outputs/plots/charging_rate.png
```

The CSV contains **relative deltas in minutes**, not fixed absolute timestamps. This is intentional. The API test runner generates a fresh runtime reference start timestamp and converts all deltas into absolute timestamps when it runs.

### What the generator does

The generator creates a 7-day synthetic schedule for 5 charging points:

```text
example pilot CP1
example pilot CP2
example pilot CP3
example pilot CP4
example pilot CP5
```

Each charging point can have a different number of sessions per day. This is controlled by the dictionary:

```python
SESSIONS_PER_DAY_BY_CHARGER = {
    1: (1, 2),
    2: (2, 4),
    3: (1, 5),
    4: (3, 5),
    5: (1, 3),
}
```

The tuple is interpreted as an inclusive `(minimum, maximum)` range. For each day and each charging point, the generator samples one value in that range. This means that CP1, CP2, CP3, CP4, and CP5 can follow different usage profiles.

### Overnight sessions

Sessions are now allowed to cross from one day to the next. This is controlled by:

```python
ALLOW_OVERNIGHT_SESSIONS = True
```

When this is enabled, a session that starts late in the evening may disconnect after midnight. The generator still prevents overlap on the same charging point by keeping one availability cursor per charger. Therefore, if a charger is still occupied after midnight, the next session on that same charger can only start after the previous session has ended.

Late sessions on the final simulated day may also disconnect after the nominal 7-day horizon. The allowed extension is controlled by:

```python
MAX_EXTENSION_AFTER_FINAL_DAY_MINUTES = 8 * 60
```

For each session, the generator creates:

- one `vehicle_connected` event,
- zero or more `charging_update` events approximately every 15 minutes,
- one `vehicle_disconnected` event.

The generator enforces these sanity rules:

- session duration is at least 10 minutes,
- charging power is below the 11 kW nominal charger power,
- energy delivered since the previous event is feasible with respect to elapsed time and charging rate,
- if `action == 0`, then both `charging_rate_kw` and `energy_since_last_event_kwh` are zero,
- `fully_charged == true` once no more energy needs to be charged,
- the number of `action == 0` events while the vehicle is not fully charged is counted globally and per charger.

### Diagnostic plots generated after the CSV

After writing the CSV files, `generate_charging_schedule.py` also creates five PNG plots in `outputs/plots/`:

```text
connection_disconnection_events.png
active_sessions_total.png
daily_energy_by_charger.png
cumulative_energy.png
charging_rate.png
```

They show:

- connection and disconnection events by charging point,
- total number of active sessions over time,
- daily delivered energy per charging point,
- cumulative delivered energy per charging point,
- charging rate over time, including the 11 kW nominal-power reference line.

These plots are produced locally only; they are not sent to the API.

## Step 2 — Run the API test

Set the environment variables listed at the top of this file, then run:

```bash
python run_e2e_api_test.py
```

The runner stops immediately if `EV_ORCH_ADMIN_USER`, `EV_ORCH_ADMIN_PASSWORD`, or `EV_ORCH_TEST_PASSWORD` is missing.

## What the API runner does

### 0. Health check

It first calls:

```text
GET /health
```

If `/health` is not available, it falls back to:

```text
GET /docs
```

This verifies that the deployed service is reachable.

### 1. Admin login

It logs in with:

```text
POST /auth/login
```

using the constants at the top of the file.

If a token later expires, the runner automatically logs in again and retries the failed request once.

### 2. Create or reuse `example_user1`

It lists owners with:

```text
GET /db/owners
```

If `example_user1` already exists, it reuses it. Otherwise it creates it using:

```text
POST /db/owners
```

with:

```text
user: example_user1
password: value of EV_ORCH_TEST_PASSWORD (not stored in this repo)
role: user
type: user-adv
```

### 3. Create or reuse the pilot

It lists pilots with:

```text
GET /db/pilots
```

If `example pilot` already exists, it reuses it. Otherwise it creates it using:

```text
POST /db/pilots
```

with:

```text
name: example pilot
owner: example_user1
timezone: Europe/Zurich
```

### 4. Admin logout

It logs out from the admin session with:

```text
POST /auth/logout
```

### 5. Login as `example_user1`

It logs in as the new/reused test user.

### 6. Check pilot visibility

It calls:

```text
GET /db/pilots
```

and verifies that `example pilot` is visible to `example_user1`.

### 7. Create or reuse 5 chargers

It creates or reuses:

```text
example pilot CP1
example pilot CP2
example pilot CP3
example pilot CP4
example pilot CP5
```

The payload uses:

```text
type: V1G
nominal_power: 11
plugs: Type2
owner: example_user1
pilot: example pilot
latitude/longitude: Swiss coordinates around Lugano/Ticino
```

If charger creation as `example_user1` is not allowed by the deployed API, the runner logs this and retries the charger setup as admin.

### 8. Resolve the CSV and execute the schedule

The input CSV generated in step 1 contains charger names but not API charger IDs. Once the API chargers exist, the runner maps each charger name to the corresponding API ID.

It then writes a resolved CSV:

```text
outputs/synthetic_charging_schedule_resolved_<run_id>.csv
```

This resolved CSV includes:

- the real API charger IDs,
- the absolute timestamps used in the API calls,
- the runtime reference start timestamp.

The runner then sends the lifecycle events:

```text
POST /events/vehicle_connected
POST /events/charging_update
POST /events/vehicle_disconnected
```

### Execution speed

The execution speed is controlled by:

```python
TIME_SCALE = 1440.0
USE_SLEEP_BETWEEN_SCHEDULED_CALLS = True
```

Meaning:

```text
TIME_SCALE = 1       -> 7 simulated days take 7 real days
TIME_SCALE = 1440    -> 1 simulated day takes 1 real minute
```

With the default value, the full 7-day schedule should take around 7 minutes plus HTTP overhead.

To send all events immediately, set:

```python
USE_SLEEP_BETWEEN_SCHEDULED_CALLS = False
```

### 9. Active-session checks

The runner calls:

```text
GET /events/active_sessions
```

4 times per simulated day.

For each check it logs:

```text
timestamp_delta_minutes
timestamp_abs
expected_active_sessions
actual_active_sessions
expected_synthetic_session_ids
match / mismatch
full endpoint response
```

### Additional endpoint checks

After the schedule has been executed, the runner calls each of these once:

```text
GET /db/owners
GET /db/pilots
GET /db/chargers
```

### 10. Charger history

It calls:

```text
GET /forecaster/charger/{charger_id}/history
```

for `CP1`.

### 11. Latest charger forecast and validation

It calls:

```text
GET /forecaster/charger/{charger_id}/latest
```

for `CP2`.

Then it computes schedule-derived averages grouped by starting hour:

- average charged energy per session,
- average session duration.

It computes these values in two ways:

1. charger-specific averages for the reference charger,
2. global averages across all chargers.

If the API forecast row has:

```text
source == generic
```

then the runner compares the API value with the global average.

Otherwise, it compares with the charger-specific average.

All differences are reported in the log.

### 12. Last-session actions

For the last synthetic session, the runner tries to resolve the API session ID and calls:

```text
GET /sessions/actions/session/{session_id}
```

The response is printed and logged.

### 13. Forecast jobs

It calls and prints:

```text
GET /db/forecast_jobs
```

### 14. Logout

Finally, it logs out from `example_user1`.

## Logs produced by the runner

Each run creates two log files:

```text
outputs/ev_orchestrator_e2e_<run_id>.log
outputs/ev_orchestrator_e2e_<run_id>.jsonl
```

The `.log` file is human-readable.

The `.jsonl` file is structured and easier to post-process later.

Both logs redact passwords and access tokens.

## Important assumptions

The endpoint paths and payload conventions are based on the API usage pattern from the provided debug script and the deployed API documentation. In particular, the runner assumes these payload fields:

```text
/auth/login:
  user, password

/db/owners:
  user, password, company_name, role, type

/db/pilots:
  name, id_owner, timezone_name

/db/chargers:
  name, type, latitude, longitude, nominal_power, plugs, id_owner, id_pilot

/events/vehicle_connected:
  charger_id, timestamp, measured_power_kw, is_fully_charged

/events/charging_update:
  charger_id, timestamp, avg_power_last_15min_kw, energy_delivered_kwh, is_fully_charged

/events/vehicle_disconnected:
  charger_id, timestamp, avg_power_last_15min_kw, energy_delivered_kwh, is_fully_charged
```

If the deployed API uses slightly different field names, adjust the payload construction functions in `run_e2e_api_test.py`, especially:

```python
event_payload(...)
get_or_create_owner(...)
get_or_create_pilot(...)
get_or_create_chargers(...)
```

## Additional checks that would be useful later

1. Verify authorization boundaries explicitly, for example that `example_user1` cannot access chargers or pilots owned by another user.
2. Add duplicate event tests, such as sending the same connection twice for the same charger and checking that the API rejects or handles it consistently.
3. Add invalid physical values, such as negative energy or power greater than nominal power, and verify that the API rejects them.
4. Add timezone edge cases around daylight saving time changes in `Europe/Zurich`.
5. Add a post-test database consistency check if read endpoints expose session totals, for example total delivered energy per charger and number of closed sessions.
6. Add forecast-job timing checks if the forecaster runs asynchronously, because forecast endpoints may not update immediately after the synthetic session sequence.
