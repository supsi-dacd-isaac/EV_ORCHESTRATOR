import requests
import json

BASE = "http://localhost:8000"

try:
    r = requests.post(
        f"{BASE}/auth/login",
        json={"user": "supsi_admin", "password": "replace-with-initial-admin-password"},
        timeout=5,
    )
    print("Login:", r.status_code)
    token = r.json()["access_token"]
except Exception as e:
    print("Login failed:", e)
    raise SystemExit(1)

headers = {"Authorization": f"Bearer {token}"}
pid = "3d76cb63-bb2b-442e-8e2c-f7aeed51921e"

# Active sessions
r2 = requests.get(f"{BASE}/events/active_sessions/pilot/{pid}", headers=headers, timeout=5)
print(f"Active sessions ({r2.status_code}): {len(r2.json())} sessions")

# Total occupancy forecast
r3 = requests.get(f"{BASE}/forecaster/pilot/{pid}/total_occupancy", headers=headers, timeout=5)
body = r3.json()
print(f"\ntotal_occupancy ({r3.status_code}): {len(body) if isinstance(body, list) else 'error'} rows")
if isinstance(body, list):
    for row in body[:4]:
        print(f"  {row['timestamp']}  occupancy={row['occupancy']}")
else:
    print(json.dumps(body, indent=2, default=str))


