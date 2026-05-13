#!/usr/bin/env python3
"""
EV Orchestrator — full integration debug script.

Runs a complete scenario through the API covering:
  - Auth (login / logout / password change)
  - Owner / pilot / charger creation
  - One-month historical charging simulation
  - Live session lifecycle (connect → update → disconnect)
  - Forecast endpoints
  - Authorization boundary checks (user-adv vs admin resources, guest restrictions)

Usage (from project root, with Docker running):
    pip install requests
    python debug_api.py              # full run with historical simulation (~few minutes)
    python debug_api.py --skip-history   # skip the month simulation, test live flow only
"""

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone

import requests

# ── Configuration ─────────────────────────────────────────────────────────────
BASE_URL = "http://localhost:8000"

# Must match ADMIN_INIT_USER / ADMIN_INIT_PASSWORD in your .env
ADMIN_USER = "supsi_admin"
ADMIN_PASSWORD = "replace-with-initial-admin-password"

# Number of historical sessions to simulate per charger (spec: 30–50)
HISTORICAL_SESSIONS_PER_CHARGER = 3
HISTORICAL_DAYS = 30
GENERIC_CHARGER_ID = "bdd8919c-1714-4776-bbbb-bed44ffc5886"
# ──────────────────────────────────────────────────────────────────────────────

rng = random.Random(42)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _j(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


def section(title: str) -> None:
    bar = "=" * 70
    print(f"\n{bar}\n  {title}\n{bar}")


def show(label: str, resp: requests.Response):
    """Print and return the JSON body of a response."""
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    icon = "✓" if resp.ok else "✗"
    print(f"\n{icon} [{resp.status_code}] {label}")
    print(_j(body))
    return body


def _ts(dt: datetime) -> str:
    """Return ISO-8601 string suitable for API timestamp fields."""
    return dt.isoformat()


# ── API client ────────────────────────────────────────────────────────────────

class Client:
    def __init__(self, token: str | None = None, owner_id: str | None = None):
        self.token = token
        self.owner_id = owner_id

    @property
    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def login(self, user: str, password: str) -> "Client":
        r = requests.post(f"{BASE_URL}/auth/login", json={"user": user, "password": password})
        body = show(f"LOGIN  {user}", r)
        r.raise_for_status()
        return Client(token=body["access_token"], owner_id=body["owner_id"])

    def logout(self) -> None:
        r = requests.post(f"{BASE_URL}/auth/logout", headers=self._h)
        show("LOGOUT", r)

    def post(self, path: str, **kw) -> requests.Response:
        return requests.post(f"{BASE_URL}{path}", headers=self._h, **kw)

    def get(self, path: str, **kw) -> requests.Response:
        return requests.get(f"{BASE_URL}{path}", headers=self._h, **kw)

    def put(self, path: str, **kw) -> requests.Response:
        return requests.put(f"{BASE_URL}{path}", headers=self._h, **kw)

    def patch(self, path: str, **kw) -> requests.Response:
        return requests.patch(f"{BASE_URL}{path}", headers=self._h, **kw)


# ── Reusable actions ──────────────────────────────────────────────────────────

def active_sessions(client: Client, label: str = "") -> dict:
    r = client.get("/events/active_sessions")
    body = show(f"ACTIVE SESSIONS{' — ' + label if label else ''}", r)
    return body if isinstance(body, dict) else {}


def forecast_occupancy(client: Client, pilot_id: str, label: str = "") -> None:
    r = client.get(f"/forecaster/pilot/{pilot_id}/total_occupancy")
    body = show(f"FORECAST total_occupancy{' — ' + label if label else ''}", r)
    if isinstance(body, list) and len(body) == 0:
        print("  ℹ  Empty — Celery has not yet produced a forecast for this pilot.")
        print("     Enable a job for this pilot_id in ev_total_forecast_jobs.yaml and wait ~1 min.")


def forecast_energy(client: Client, pilot_id: str, label: str = "") -> None:
    r = client.get(f"/forecaster/pilot/{pilot_id}/total_energy")
    body = show(f"FORECAST total_energy{' — ' + label if label else ''}", r)
    if isinstance(body, list) and len(body) == 0:
        print("  ℹ  Empty — Celery has not yet produced a forecast for this pilot.")


def simulate_historical_session(
    client: Client,
    charger_id: str,
    start: datetime,
    duration_h: float,
    total_energy_kwh: float,
) -> None:
    """vehicle_connected → 2 charging_updates → vehicle_disconnected (past timestamps)."""
    power_kw = total_energy_kwh / duration_h
    chunk = total_energy_kwh / 4

    r = client.post("/events/vehicle_connected", json={
        "charger_id": charger_id,
        "timestamp": _ts(start),
        "measured_power_kw": round(power_kw, 2),
        "is_fully_charged": False,
    })
    if not r.ok:
        print(f"    [WARN] vehicle_connected {r.status_code}: {r.text[:100]}")
        return

    # Two intermediate updates (at 1/3 and 2/3 of the session)
    for step in [1, 2]:
        t = start + timedelta(hours=duration_h * step / 3)
        client.post("/events/charging_update", json={
            "charger_id": charger_id,
            "timestamp": _ts(t),
            "avg_power_last_15min_kw": round(power_kw * rng.uniform(0.7, 1.0), 2),
            "energy_delivered_kwh": round(chunk, 2),
            "is_fully_charged": False,
        })

    end = start + timedelta(hours=duration_h)
    r = client.post("/events/vehicle_disconnected", json={
        "charger_id": charger_id,
        "timestamp": _ts(end),
        "avg_power_last_15min_kw": round(power_kw * 0.2, 2),
        "energy_delivered_kwh": round(chunk * 2, 2),
        "is_fully_charged": False,
    })
    if r.status_code >= 400:
        print(f"    [ERROR] vehicle_disconnected {r.status_code}: {r.text[:200]}")


def show_cdf_for_charger(client: Client, charger_id: str, label: str = "") -> None:
    """Fetch /db/duration_cdf and print the latest snapshot for charger_id."""
    r = client.get("/db/duration_cdf")
    if not r.ok:
        show(f"CDF {label}", r)
        return
    rows = [row for row in r.json() if str(row["id_charger"]) == str(charger_id)]
    if not rows:
        print(f"\n  ℹ  No CDF rows found for {label} ({charger_id[:8]}…)")
        return
    max_sc = max(row["sample_count"] for row in rows)
    latest = sorted(
        [row for row in rows if row["sample_count"] == max_sc],
        key=lambda r: r["horizon_hours"],
    )
    print(f"\n✓ CDF — {label}  (sample_count={max_sc}, {len(latest)} horizons)")
    for row in latest:
        print(f"    horizon={row['horizon_hours']:5.1f}h  P(dur<=h)={row['probability']:.3f}")


def show_ev_forecast_for_charger(client: Client, charger_id: str, label: str = "") -> None:
    """Fetch /db/ev_forecast_stats and print the latest snapshot per hour for charger_id."""
    r = client.get("/db/ev_forecast_stats")
    if not r.ok:
        show(f"EV FORECAST STATS {label}", r)
        return
    rows = [row for row in r.json() if str(row["id_charger"]) == str(charger_id)]
    if not rows:
        print(f"\n  ℹ  No ev_forecast_stats rows found for {label} ({charger_id[:8]}…)")
        return
    # latest snapshot per hour = highest sample_count per hour
    by_hour: dict = {}
    for row in rows:
        h = row["local_hour"]
        if h not in by_hour or row["sample_count"] > by_hour[h]["sample_count"]:
            by_hour[h] = row
    print(f"\n✓ EV FORECAST STATS — {label}  ({len(by_hour)} hours with data)")
    for h in sorted(by_hour):
        row = by_hour[h]
        print(
            f"    hour={row['local_hour']:2d}  "
            f"mean_E={row['mean_energy_kwh']:.2f} kWh  std_E={row['std_energy_kwh']:.2f}  "
            f"mean_D={row['mean_duration_hours']:.2f}h  std_D={row['std_duration_hours']:.2f}  "
            f"n={row['sample_count']}"
        )


def make_chargers(
    admin: Client,
    pilot_id: str,
    owner_id: str,
    prefix: str,
    n: int = 5,
) -> list[str]:
    ids = []
    for i in range(1, n + 1):
        r = admin.post("/db/chargers", json={
            "name": f"{prefix}-C{i:02d}",
            "type": "AC",
            "latitude": round(46.0 + rng.uniform(-0.5, 0.5), 6),
            "longitude": round(8.9 + rng.uniform(-0.5, 0.5), 6),
            "nominal_power": rng.choice([7.4, 11.0, 22.0]),
            "plugs": "Type2",
            "id_owner": owner_id,
            "id_pilot": pilot_id,
        })
        if r.ok:
            ids.append(r.json()["id"])
        else:
            print(f"  [WARN] Failed to create {prefix}-C{i:02d}: {r.text[:80]}")
    print(f"  Created {len(ids)}/{n} chargers for {prefix}")
    return ids


# ── Main script ───────────────────────────────────────────────────────────────

def main(skip_history: bool = False) -> None:

    # ── 1. Login as admin ────────────────────────────────────────────────────
    section("STEP 1 — LOGIN AS ADMIN")
    admin = Client().login(ADMIN_USER, ADMIN_PASSWORD)

    # ── 2. Create user-adv owner ─────────────────────────────────────────────
    section("STEP 2 — CREATE USER-ADV OWNER")
    r = admin.post("/db/owners", json={
        "user": "user_adv",
        "password": "password",
        "company_name": "TestCo ADV",
        "role": "user",
        "type": "user-adv",
    })
    uadv_data = show("CREATE user_adv (role=user)", r)
    r.raise_for_status()
    user_adv_id: str = uadv_data["id"]

    # ── 3. Create guest owner ────────────────────────────────────────────────
    section("STEP 3 — CREATE GUEST OWNER")
    r = admin.post("/db/owners", json={
        "user": "guest_user",
        "password": "password",
        "company_name": "TestCo Guest",
        "role": "guest",
        "type": "guest",
    })
    show("CREATE guest_user (role=guest)", r)
    r.raise_for_status()

    # ── 4. Create 3 pilots ───────────────────────────────────────────────────
    section("STEP 4 — CREATE 3 PILOTS  (2 owned by admin, 1 by user-adv)")

    r = admin.post("/db/pilots", json={"name": "Pilot-Admin-1", "id_owner": admin.owner_id})
    show("CREATE Pilot-Admin-1", r); r.raise_for_status()
    pilot1_id: str = r.json()["id"]

    r = admin.post("/db/pilots", json={"name": "Pilot-Admin-2", "id_owner": admin.owner_id})
    show("CREATE Pilot-Admin-2", r); r.raise_for_status()
    pilot2_id: str = r.json()["id"]

    r = admin.post("/db/pilots", json={"name": "Pilot-UserAdv-3", "id_owner": user_adv_id})
    show("CREATE Pilot-UserAdv-3", r); r.raise_for_status()
    pilot3_id: str = r.json()["id"]

    # ── 5. Create 10 chargers per pilot ──────────────────────────────────────
    section("STEP 5 — CREATE 10 CHARGERS PER PILOT (30 total)")

    # Admin is charger owner for pilots 1 & 2.
    # user_adv_id is charger owner for pilot 3 so user-adv can use event endpoints.
    chargers_p1 = make_chargers(admin, pilot1_id, admin.owner_id, "P1")
    chargers_p2 = make_chargers(admin, pilot2_id, admin.owner_id, "P2")
    chargers_p3 = make_chargers(admin, pilot3_id, user_adv_id, "P3")

    # ── 6. Simulate one month of charging sessions ───────────────────────────
    if not skip_history:
        section("STEP 6 — SIMULATE ONE MONTH OF CHARGING SESSIONS")
        print(f"  {HISTORICAL_SESSIONS_PER_CHARGER} sessions × 30 chargers — may take a few minutes…")
        base_date = datetime.now(timezone.utc) - timedelta(days=HISTORICAL_DAYS + 2)

        groups = [
            ("Pilot-Admin-1", chargers_p1),
            ("Pilot-Admin-2", chargers_p2),
            ("Pilot-UserAdv-3", chargers_p3),
        ]
        # Admin can call event endpoints on all chargers regardless of id_owner
        for pilot_name, charger_ids in groups:
            print(f"\n  ▶ {pilot_name}")
            for idx, cid in enumerate(charger_ids):
                for _ in range(HISTORICAL_SESSIONS_PER_CHARGER):
                    start = base_date + timedelta(
                        days=rng.uniform(0, HISTORICAL_DAYS - 1),
                        hours=rng.uniform(6, 22),
                        minutes=rng.randint(0, 59),
                    )
                    duration_h = rng.uniform(0.5, 6.0)
                    energy_kwh = rng.uniform(5.0, 50.0)
                    simulate_historical_session(admin, cid, start, duration_h, energy_kwh)
                print(f"    Charger {idx + 1:02d}/{len(charger_ids)} done ({HISTORICAL_SESSIONS_PER_CHARGER} sessions)")
    else:
        section("STEP 6 — SKIPPED (--skip-history)")

    # ── 6b. List all forecast jobs (admin) + enable admin's two pilots ───────
    section("STEP 6b — LIST FORECAST JOBS (admin) + ENABLE PILOT-ADMIN-1 & PILOT-ADMIN-2")
    r = admin.get("/db/forecast_jobs")
    show("LIST all forecast jobs", r)
    for pid, label in [(pilot1_id, "Pilot-Admin-1"), (pilot2_id, "Pilot-Admin-2")]:
        r = admin.patch(f"/forecaster/pilot/{pid}/forecast_job", json={"enabled": True})
        body = show(f"ENABLE forecast job — {label}", r)
        r.raise_for_status()
        job_id = body["job_id"]
        r = admin.put(f"/db/forecast_jobs/{job_id}", json={"horizon": 6})
        show(f"SET HORIZON=3h (admin DB endpoint) — {label}", r)

    # ── 6c. CDF + EV forecast stats BEFORE import ────────────────────────────
    section("STEP 6c — CDF + EV FORECAST STATS  (before CSV import)")
    show_cdf_for_charger(admin, GENERIC_CHARGER_ID, "generic charger (before)")
    show_cdf_for_charger(admin, chargers_p1[0], "P1-C01 (before)")
    show_ev_forecast_for_charger(admin, GENERIC_CHARGER_ID, "generic charger (before)")
    show_ev_forecast_for_charger(admin, chargers_p1[0], "P1-C01 (before)")

    # ── 6d. Import CSV sessions for Pilot-Admin-1 ────────────────────────────
    section("STEP 6d — IMPORT CSV SESSIONS  (Pilot-Admin-1)")
    with open("charging_sessions_example.csv", "rb") as csv_file:
        r = admin.post(
            f"/sessions/import_csv/{pilot1_id}",
            files={"file": ("charging_sessions_example.csv", csv_file, "text/csv")},
        )
    show("IMPORT CSV — Pilot-Admin-1", r)

    # ── 6e. CDF + EV forecast stats AFTER import ─────────────────────────────
    section("STEP 6e — CDF + EV FORECAST STATS  (after CSV import)")
    show_cdf_for_charger(admin, GENERIC_CHARGER_ID, "generic charger (after)")
    show_cdf_for_charger(admin, chargers_p1[0], "P1-C01 (after)")
    show_ev_forecast_for_charger(admin, GENERIC_CHARGER_ID, "generic charger (after)")
    show_ev_forecast_for_charger(admin, chargers_p1[0], "P1-C01 (after)")

    # ── 7. First pilot forecast ───────────────────────────────────────────────
    section("STEP 7 — FORECAST AGGREGATED OCCUPANCY  (Pilot-Admin-1)")
    forecast_occupancy(admin, pilot1_id, "Pilot-Admin-1 after history")
    forecast_energy(admin, pilot1_id, "Pilot-Admin-1 after history")

    # ── 8. Two live sessions — 43 min apart ──────────────────────────────────
    section("STEP 8 — TWO NEW SESSIONS IN PILOT-ADMIN-1 (43 min apart)")

    # Start 3 hours before now so the sessions are already visible to the
    # aggregated forecaster (which queries active sessions at current wall time).
    T0 = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=3)
    cA = chargers_p1[0]
    cB = chargers_p1[1]

    # Session A — connects at T0
    r = admin.post("/events/vehicle_connected", json={
        "charger_id": cA, "timestamp": _ts(T0),
        "measured_power_kw": 7.2, "is_fully_charged": False,
    })
    show("CONNECT charger P1-C01 (session A) @ T0", r)

    # One update for session A before session B connects
    r = admin.post("/events/charging_update", json={
        "charger_id": cA, "timestamp": _ts(T0 + timedelta(minutes=15)),
        "avg_power_last_15min_kw": 7.1, "energy_delivered_kwh": 1.78, "is_fully_charged": False,
    })
    show("UPDATE session A @ T0+15 min", r)

    r = admin.post("/events/charging_update", json={
        "charger_id": cA, "timestamp": _ts(T0 + timedelta(minutes=30)),
        "avg_power_last_15min_kw": 7.0, "energy_delivered_kwh": 1.75, "is_fully_charged": False,
    })
    show("UPDATE session A @ T0+30 min", r)

    # Session B — connects at T0+43 min (session A already has 2 updates at this point)
    T43 = T0 + timedelta(minutes=43)
    r = admin.post("/events/vehicle_connected", json={
        "charger_id": cB, "timestamp": _ts(T43),
        "measured_power_kw": 11.0, "is_fully_charged": False,
    })
    show("CONNECT charger P1-C02 (session B) @ T0+43 min", r)

    # ── 9–10. Active sessions & forecast ─────────────────────────────────────
    section("STEP 9 — CHECK ACTIVE SESSIONS")
    active_sessions(admin, "after 2 connections")

    section("STEP 10 — FORECAST AGGREGATED OCCUPANCY")
    forecast_occupancy(admin, pilot1_id, "Pilot-Admin-1 — 2 active sessions")
    forecast_energy(admin, pilot1_id, "Pilot-Admin-1 — 2 active sessions")

    # ── 11. Both sessions updated every 15 min for 2 hours ───────────────────
    section("STEP 11 — CHARGING UPDATES BOTH SESSIONS  (n steps every 15 min)")
    t_live = T43
    for step in range(1, 5):
        t_live = T43 + timedelta(minutes=step * 15)
        for cid, pwr, nrg in [(cA, 7.0, 1.75), (cB, 10.5, 2.63)]:
            admin.post("/events/charging_update", json={
                "charger_id": cid, "timestamp": _ts(t_live),
                "avg_power_last_15min_kw": pwr, "energy_delivered_kwh": nrg,
                "is_fully_charged": False,
            })
        print(f"  step {step}/5  t={t_live.strftime('%H:%M UTC')}")

    # ── 12–13. Active sessions & forecast ────────────────────────────────────
    section("STEP 12 — CHECK ACTIVE SESSIONS")
    active_sessions(admin, "after 2-hour updates")

    section("STEP 13 — FORECAST AGGREGATED OCCUPANCY")
    forecast_occupancy(admin, pilot1_id, "after 2-hour updates")
    forecast_energy(admin, pilot1_id, "after 2-hour updates")

    # ── 14. Third connection ─────────────────────────────────────────────────
    section("STEP 14 — ADDITIONAL CONNECTION (charger P1-C03)")
    cC = chargers_p1[2]
    T_C = t_live + timedelta(minutes=10)
    r = admin.post("/events/vehicle_connected", json={
        "charger_id": cC, "timestamp": _ts(T_C),
        "measured_power_kw": 22.0, "is_fully_charged": False,
    })
    show("CONNECT charger P1-C03 (session C)", r)

    section("STEP 15 — CHECK ACTIVE SESSIONS")
    active_sessions(admin, "3 active sessions")

    section("STEP 16 — FORECAST AGGREGATED OCCUPANCY")
    forecast_occupancy(admin, pilot1_id, "3 active sessions")
    forecast_energy(admin, pilot1_id, "3 active sessions")

    # ── 17. Fourth connection ────────────────────────────────────────────────
    section("STEP 17 — ANOTHER ADDITIONAL CONNECTION (charger P1-C04)")
    cD = chargers_p1[3]
    T_D = T_C + timedelta(minutes=18)
    r = admin.post("/events/vehicle_connected", json={
        "charger_id": cD, "timestamp": _ts(T_D),
        "measured_power_kw": 7.4, "is_fully_charged": False,
    })
    show("CONNECT charger P1-C04 (session D)", r)

    section("STEP 18 — CHECK ACTIVE SESSIONS")
    active_sessions(admin, "4 active sessions")

    section("STEP 19 — FORECAST AGGREGATED OCCUPANCY")
    forecast_occupancy(admin, pilot1_id, "4 active sessions")
    forecast_energy(admin, pilot1_id, "4 active sessions")

    # ── 20. Disconnect 3 vehicles ────────────────────────────────────────────
    section("STEP 20 — DISCONNECT 3 VEHICLES (A, B, C)")
    T_disc = T_D + timedelta(minutes=20)
    for label, cid in [("A (P1-C01)", cA), ("B (P1-C02)", cB), ("C (P1-C03)", cC)]:
        r = admin.post("/events/vehicle_disconnected", json={
            "charger_id": cid, "timestamp": _ts(T_disc),
            "avg_power_last_15min_kw": 0.5, "energy_delivered_kwh": 0.5,
            "is_fully_charged": True,
        })
        show(f"DISCONNECT session {label}", r)

    section("STEP 21 — CHECK ACTIVE SESSIONS")
    active_sessions(admin, "1 active session remaining (D)")

    section("STEP 22 — FORECAST AGGREGATED OCCUPANCY")
    forecast_occupancy(admin, pilot1_id, "1 active session")
    forecast_energy(admin, pilot1_id, "1 active session")

    # ── 23. Disconnect last vehicle ───────────────────────────────────────────
    section("STEP 23 — DISCONNECT REMAINING VEHICLE (D)")
    T_disc2 = T_disc + timedelta(minutes=45)
    r = admin.post("/events/vehicle_disconnected", json={
        "charger_id": cD, "timestamp": _ts(T_disc2),
        "avg_power_last_15min_kw": 0.2, "energy_delivered_kwh": 0.3,
        "is_fully_charged": False,
    })
    show("DISCONNECT session D (P1-C04)", r)

    section("STEP 24 — CHECK ACTIVE SESSIONS")
    active_sessions(admin, "should be empty")

    section("STEP 25 — FORECAST AGGREGATED OCCUPANCY")
    forecast_occupancy(admin, pilot1_id, "all disconnected")
    forecast_energy(admin, pilot1_id, "all disconnected")

    # ── 26. Charger latest forecast ───────────────────────────────────────────
    section("STEP 26 — CHARGER LATEST FORECAST (P1-C01 and P1-C02)")
    for cid in [chargers_p1[0], chargers_p1[1]]:
        r = admin.get(f"/forecaster/charger/{cid}/latest")
        show(f"CHARGER LATEST FORECAST  {cid[:8]}…", r)

    # ── 27. Charger forecast history ──────────────────────────────────────────
    section("STEP 27 — CHARGER FORECAST HISTORY (P1-C01)")
    r = admin.get(f"/forecaster/charger/{chargers_p1[0]}/history")
    show("CHARGER FORECAST HISTORY  P1-C01", r)

    # ── 28. Logout admin ──────────────────────────────────────────────────────
    section("STEP 28 — LOGOUT ADMIN")
    admin.logout()

    # ═══════════════════════════════════════════════════════════════════════════
    section("STEP 29 — LOGIN AS USER-ADV")
    uadv = Client().login("user_adv", "password")

    # ── 29b. List forecast jobs + enable own pilot ────────────────────────────
    section("STEP 29b — LIST FORECAST JOBS (user-adv) + ENABLE PILOT-USERADV-3")
    r = uadv.get("/db/forecast_jobs")
    show("LIST forecast jobs (user-adv view)", r)
    r = uadv.patch(f"/forecaster/pilot/{pilot3_id}/forecast_job", json={"enabled": True, "kind": "reg"})
    body = show("ENABLE forecast job — Pilot-UserAdv-3", r)
    r.raise_for_status()
    job_id_p3 = body["job_id"]
    admin2 = Client().login(ADMIN_USER, ADMIN_PASSWORD)
    r = admin2.put(f"/db/forecast_jobs/{job_id_p3}", json={"horizon": 6})
    show("SET HORIZON=3h (admin DB endpoint) — Pilot-UserAdv-3", r)
    admin2.logout()

    # ── 30. Change password ───────────────────────────────────────────────────
    section("STEP 30 — CHANGE PASSWORD TO  test_user")
    r = uadv.put(f"/db/owners/{uadv.owner_id}", json={"password": "test_user"})
    show("CHANGE PASSWORD", r)

    section("STEP 31 — CHECK ACTIVE SESSIONS (user-adv sees only own pilot)")
    active_sessions(uadv, "user-adv view")

    section("STEP 32 — FORECAST AGGREGATED OCCUPANCY (Pilot-UserAdv-3)")
    forecast_occupancy(uadv, pilot3_id, "Pilot-UserAdv-3")
    forecast_energy(uadv, pilot3_id, "Pilot-UserAdv-3")

    # ── 33. Create 2 sessions on user-adv pilot ───────────────────────────────
    section("STEP 33 — CREATE 2 CHARGING SESSIONS  (user-adv, Pilot-UserAdv-3)")
    T_uadv = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    cP3A = chargers_p3[0]
    cP3B = chargers_p3[1]

    r = uadv.post("/events/vehicle_connected", json={
        "charger_id": cP3A, "timestamp": _ts(T_uadv),
        "measured_power_kw": 11.0, "is_fully_charged": False,
    })
    show("CONNECT P3-C01 (user-adv session E)", r)

    r = uadv.post("/events/vehicle_connected", json={
        "charger_id": cP3B, "timestamp": _ts(T_uadv + timedelta(minutes=5)),
        "measured_power_kw": 7.4, "is_fully_charged": False,
    })
    show("CONNECT P3-C02 (user-adv session F)", r)

    section("STEP 34 — CHECK ACTIVE SESSIONS (user-adv)")
    active_sessions(uadv, "2 user-adv sessions")

    section("STEP 35 — FORECAST AGGREGATED OCCUPANCY (Pilot-UserAdv-3)")
    forecast_occupancy(uadv, pilot3_id, "2 active sessions")
    forecast_energy(uadv, pilot3_id, "2 active sessions")

    # ── 36. Try to connect on admin pilot — expect 403 ────────────────────────
    section("STEP 36 — TRY CREATE SESSION ON ADMIN PILOT (expect 403)")
    r = uadv.post("/events/vehicle_connected", json={
        "charger_id": chargers_p1[4],
        "timestamp": _ts(T_uadv),
        "measured_power_kw": 7.0,
        "is_fully_charged": False,
    })
    show("CONNECT on admin charger P1-C05 as user-adv — expected 403", r)

    # ── 37. Try charging_update on admin pilot — expect 403/404 ───────────────
    section("STEP 37 — TRY UPDATE SESSION ON ADMIN PILOT (expect 403 or 404)")
    r = uadv.post("/events/charging_update", json={
        "charger_id": chargers_p1[0],
        "timestamp": _ts(T_uadv),
        "avg_power_last_15min_kw": 7.0,
        "energy_delivered_kwh": 1.75,
        "is_fully_charged": False,
    })
    show("UPDATE on admin charger P1-C01 as user-adv — expected 403/404", r)

    # ── 38. Simulate 45 min updates on own sessions ───────────────────────────
    section("STEP 38 — SIMULATE UPDATES FOR 45 MIN (3 × 15 min)  — user-adv sessions")
    t_uadv_live = T_uadv
    for step in range(1, 4):
        t_uadv_live = T_uadv + timedelta(minutes=step * 15)
        for cid, pwr, nrg in [(cP3A, 10.8, 2.7), (cP3B, 7.2, 1.8)]:
            uadv.post("/events/charging_update", json={
                "charger_id": cid, "timestamp": _ts(t_uadv_live),
                "avg_power_last_15min_kw": pwr, "energy_delivered_kwh": nrg,
                "is_fully_charged": False,
            })
        print(f"  step {step}/3  t={t_uadv_live.strftime('%H:%M UTC')}")

    section("STEP 39 — CHECK ACTIVE SESSIONS (user-adv)")
    active_sessions(uadv, "after 45-min updates")

    section("STEP 40 — FORECAST AGGREGATED OCCUPANCY (Pilot-UserAdv-3)")
    forecast_occupancy(uadv, pilot3_id, "after 45-min updates")
    forecast_energy(uadv, pilot3_id, "after 45-min updates")

    # ── 41. Disconnect one vehicle ────────────────────────────────────────────
    section("STEP 41 — DISCONNECT ONE VEHICLE (P3-C01)")
    T_uadv_disc = t_uadv_live + timedelta(minutes=15)
    r = uadv.post("/events/vehicle_disconnected", json={
        "charger_id": cP3A, "timestamp": _ts(T_uadv_disc),
        "avg_power_last_15min_kw": 0.5, "energy_delivered_kwh": 0.4,
        "is_fully_charged": True,
    })
    show("DISCONNECT P3-C01 (session E)", r)

    # ── 42. Charger latest forecast for the disconnected charger ──────────────
    section("STEP 42 — CHARGER LATEST FORECAST — JUST-DISCONNECTED CHARGER (P3-C01)")
    r = uadv.get(f"/forecaster/charger/{cP3A}/latest")
    show("CHARGER LATEST FORECAST  P3-C01 (after disconnect)", r)

    section("STEP 43 — CHECK ACTIVE SESSIONS (user-adv)")
    active_sessions(uadv, "1 remaining session (F)")

    section("STEP 44 — FORECAST AGGREGATED OCCUPANCY (Pilot-UserAdv-3)")
    forecast_occupancy(uadv, pilot3_id, "1 active session remaining")
    forecast_energy(uadv, pilot3_id, "1 active session remaining")

    # ── 45. 30 more minutes of updates ────────────────────────────────────────
    section("STEP 45 — CHARGING UPDATES 30 MORE MINUTES (2 × 15 min)  — P3-C02")
    t_uadv_live2 = T_uadv_disc
    for step in range(1, 3):
        t_uadv_live2 = T_uadv_disc + timedelta(minutes=step * 15)
        uadv.post("/events/charging_update", json={
            "charger_id": cP3B, "timestamp": _ts(t_uadv_live2),
            "avg_power_last_15min_kw": 7.0, "energy_delivered_kwh": 1.75,
            "is_fully_charged": False,
        })
        print(f"  step {step}/2  t={t_uadv_live2.strftime('%H:%M UTC')}")

    section("STEP 46 — CHECK ACTIVE SESSIONS (user-adv)")
    active_sessions(uadv, "P3-C02 still active")

    section("STEP 47 — FORECAST AGGREGATED OCCUPANCY (Pilot-UserAdv-3)")
    forecast_occupancy(uadv, pilot3_id, "P3-C02 still active")
    forecast_energy(uadv, pilot3_id, "P3-C02 still active")

    # ═══════════════════════════════════════════════════════════════════════════
    section("STEP 48 — LOGIN AS GUEST")
    guest = Client().login("guest_user", "password")

    # ── 48b. Guest lists forecast jobs (first action after login) ─────────────
    section("STEP 48b — LIST FORECAST JOBS (guest — first action)")
    r = guest.get("/db/forecast_jobs")
    show("LIST forecast jobs as guest", r)

    section("STEP 49 — TRY CHECK ACTIVE SESSIONS (guest — expect empty result)")
    active_sessions(guest, "guest view")

    section("STEP 50 — FORECAST AGGREGATED OCCUPANCY (guest — expect 403)")
    r = guest.get(f"/forecaster/pilot/{pilot3_id}/total_occupancy")
    show("FORECAST occupancy as guest — expected 403", r)

    section("STEP 51 — TRY CREATE SESSION ON USER-ADV PILOT (guest — expect 403)")
    r = guest.post("/events/vehicle_connected", json={
        "charger_id": cP3B,
        "timestamp": _ts(datetime.now(timezone.utc)),
        "measured_power_kw": 7.0,
        "is_fully_charged": False,
    })
    show("CONNECT on user-adv charger as guest — expected 403", r)

    section("STEP 52 — LOGOUT GUEST")
    guest.logout()

    # ═══════════════════════════════════════════════════════════════════════════
    section("STEP 53 — BACK TO USER-ADV (login with new password  test_user)")
    uadv2 = Client().login("user_adv", "test_user")

    # ── 54. Check actions on still-active session ─────────────────────────────
    section("STEP 54 — CHECK ACTIONS ON STILL-ACTIVE SESSION (P3-C02)")
    r = uadv2.get("/events/active_sessions")
    active = r.json() if r.ok else {}
    if active:
        session_id = list(active.keys())[0]
        print(f"\n  Found active session: {session_id}")
        r = uadv2.get(f"/sessions/actions/session/{session_id}")
        show(f"ACTIONS for session {session_id[:8]}…", r)
    else:
        print("\n  No active sessions found for user-adv.")

    # ── 55. Disconnect remaining vehicle ──────────────────────────────────────
    section("STEP 55 — DISCONNECT REMAINING VEHICLE (P3-C02)")
    T_final = datetime.now(timezone.utc)
    r = uadv2.post("/events/vehicle_disconnected", json={
        "charger_id": cP3B, "timestamp": _ts(T_final),
        "avg_power_last_15min_kw": 0.3, "energy_delivered_kwh": 0.2,
        "is_fully_charged": False,
    })
    show("DISCONNECT P3-C02 (session F)", r)

    section("STEP 56 — CHECK ACTIVE SESSIONS (should be empty)")
    active_sessions(uadv2, "all disconnected")

    section("STEP 57 — FORECAST AGGREGATED OCCUPANCY (Pilot-UserAdv-3)")
    forecast_occupancy(uadv2, pilot3_id, "final state")
    forecast_energy(uadv2, pilot3_id, "final state")

    section("STEP 58 — LOGOUT USER-ADV")
    uadv2.logout()

    # ── Summary ───────────────────────────────────────────────────────────────
    section("ALL STEPS COMPLETE")
    print("""
  Key things to verify in the output:
  ─────────────────────────────────────────────────────────────────────
  ✓ Steps 2–5      owners / pilots / chargers created (201 responses)
  ✓ Step 6b        admin lists + enables forecast jobs for pilots 1 & 2 (after history)
  ✓ Step 8         sessions A & B created; A has 2 updates before B connects
  ✓ Steps 29b      user-adv lists + enables forecast job for pilot 3
  ✓ Step 48b       guest can list forecast jobs
  ✓ Steps 36–37    user-adv gets 403 on admin chargers
  ✓ Steps 49–51    guest gets empty active sessions and 403 on connect
  ✓ Step 53        user-adv can log in with new password  test_user
  ✓ Steps 26,42    /forecaster/charger/{id}/latest returns per-hour stats
  ✓ Step 27        /forecaster/charger/{id}/history returns all historical rows

  Note on pilot forecasts (steps 7, 10, 13, 16, 19, 22, 25, 32, 35, 40, 44, 47, 57):
    These return [] until Celery beat has picked up the enabled ForecastJobDB entries
    and run the forecast task at least once. Enable is done in steps 4b / 29b.
    Charger-level forecasts (step 26, 42) use DB data and work immediately.
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EV Orchestrator integration debug script")
    parser.add_argument(
        "--skip-history",
        action="store_true",
        help="Skip the 30-day historical simulation (faster, tests live flow only)",
    )
    args = parser.parse_args()

    try:
        main(skip_history=args.skip_history)
    except KeyboardInterrupt:
        print("\n\n[interrupted]")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        print(f"\n[ERROR] Cannot connect to {BASE_URL} — is `docker compose up -d` running?")
        sys.exit(1)
