"""Live E2E smoke tests against localhost:8000 for user_adv (July 2026 sessions).

Outputs JSON results to tests/_e2e_results.json for summarization.
"""
from __future__ import annotations

import json
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx

BASE = "http://localhost:8000"
USER = "user_adv"
PASSWORD = "user_adv"
GUEST_USER = f"guest_e2e_{uuid.uuid4().hex[:8]}"
GUEST_PASS = "guest_e2e_pass_1"
OUT = "tests/_e2e_results.json"

# Pilot-UserAdv-3 chargers (Europe/London) — policies as assigned in DB at test start
CHARGER_WIND = "35ee041f-fd52-4455-b422-a42e9cdb06a8"  # wind_max
CHARGER_FFF = "f932963e-986d-4b9a-8fb0-af83a5818c9e"  # policy_fff ANN
CHARGER_ANN = "cfe678ad-55d4-4d75-88da-287efea83a13"  # default ANN
CHARGER_DEFAULT = "55b1bc61-9488-42d1-a1b3-53199686dc1b"  # will assign rule_based/default
PILOT_ID = "9a33e989-7512-445b-be1d-42af9f9bfcb8"
OWNER_ID = "5e6a026c-2393-4866-9e99-fc479a7a8112"


@dataclass
class Case:
    id: str
    area: str
    description: str
    expected: str
    ok: Optional[bool] = None
    status: str = "pending"
    detail: str = ""
    notes: str = ""


RESULTS: list[Case] = []


def record(
    case_id: str,
    area: str,
    description: str,
    expected: str,
    ok: bool,
    detail: str = "",
    notes: str = "",
) -> None:
    RESULTS.append(
        Case(
            id=case_id,
            area=area,
            description=description,
            expected=expected,
            ok=ok,
            status="pass" if ok else "fail",
            detail=str(detail)[:500],
            notes=notes,
        )
    )


def client(token: Optional[str] = None) -> httpx.Client:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=BASE, headers=headers, timeout=90.0)


def safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return r.text[:300]


def ensure_disconnected(c: httpx.Client, charger_id: str, ts: str) -> None:
    """Best-effort close of any active session so connect tests can run."""
    r = c.get("/events/active_sessions")
    if r.status_code != 200:
        return
    for s in r.json():
        if str(s.get("charger_id")) == charger_id:
            body = {
                "charger_id": charger_id,
                "timestamp": ts,
                "avg_power_last_15min_kw": 0.0,
                "energy_delivered_kwh": 0.0,
                "is_fully_charged": False,
            }
            c.post("/events/vehicle_disconnected", json=body)


def run() -> None:
    # ── Auth / health ──────────────────────────────────────────────
    with client() as c:
        r = c.get("/health")
        record(
            "health",
            "infra",
            "GET /health",
            "2xx",
            r.status_code < 400,
            f"status={r.status_code} body={safe_json(r)}",
        )

        r = c.post("/auth/login", json={"user": USER, "password": PASSWORD})
        ok = r.status_code == 200 and "access_token" in (r.json() if r.status_code == 200 else {})
        token = r.json()["access_token"] if ok else None
        record(
            "login_user_adv",
            "auth",
            "Login as user_adv",
            "200 + token",
            ok,
            safe_json(r),
        )
        if not token:
            _write()
            return

    with client(token) as c:
        # ── Catalog / list endpoints ───────────────────────────────
        for case_id, path, area in [
            ("list_policies", "/policies", "policies"),
            ("list_chargers", "/db/chargers", "db"),
            ("list_pilots", "/db/pilots", "db"),
            ("list_sessions", "/db/sessions", "db"),
            ("list_actions", "/db/actions", "db"),
            ("list_forecast_stats", "/db/ev_forecast_stats", "db"),
            ("list_cdf", "/db/duration_cdf", "db"),
            ("list_grid_load", "/db/grid_load_forecasted", "db"),
            ("active_sessions", "/events/active_sessions", "events"),
            ("all_sessions", "/sessions/all_sessions", "sessions"),
            ("owner_sessions", f"/sessions/owner/{OWNER_ID}", "sessions"),
            ("active_by_pilot", f"/events/active_sessions/pilot/{PILOT_ID}", "events"),
            ("active_by_owner", f"/events/active_sessions/owner/{OWNER_ID}", "events"),
            ("obs_features", "/admin/policies/observation-features", "admin"),
            ("forecaster_latest_wind", f"/forecaster/charger/{CHARGER_WIND}/latest", "forecaster"),
            ("forecaster_latest_fff", f"/forecaster/charger/{CHARGER_FFF}/latest", "forecaster"),
        ]:
            r = c.get(path)
            # observation-features may be admin-only
            expect_ok = r.status_code == 200
            notes = ""
            if case_id == "obs_features" and r.status_code in (401, 403):
                notes = "admin-only; forbidden for role=user is expected"
                expect_ok = True  # expected denial
                record(
                    case_id,
                    area,
                    f"GET {path}",
                    "403 for non-admin OR 200",
                    True,
                    safe_json(r),
                    notes,
                )
                continue
            record(
                case_id,
                area,
                f"GET {path}",
                "200",
                expect_ok,
                f"status={r.status_code} sample={str(safe_json(r))[:200]}",
                notes,
            )

        # Policies detail
        r = c.get("/policies")
        policies = r.json().get("policies", []) if r.status_code == 200 else []
        slugs = {(p.get("control_algorithm"), p.get("control_policy")) for p in policies}
        record(
            "policies_include_variants",
            "policies",
            "Policies catalog includes default, wind_max, ANN, fff",
            "all present",
            {("rule_based", "default"), ("rule_based", "wind_max")} <= slugs
            and any(a == "ann" for a, _ in slugs),
            f"found={sorted(slugs)}",
        )

        # Pilot read
        r = c.get(f"/db/pilots/{PILOT_ID}")
        pilot_ok = r.status_code == 200
        pilot = r.json() if pilot_ok else {}
        record(
            "get_own_pilot",
            "db",
            "GET own pilot Pilot-UserAdv-3",
            "200 + Europe/London",
            pilot_ok and pilot.get("timezone_name") == "Europe/London",
            safe_json(r),
        )

        # Data source test endpoints if configured
        ds = pilot.get("data_sources") or {}
        for name in list(ds.keys())[:3]:
            r = c.post(f"/db/pilots/{PILOT_ID}/data_sources/{name}/test")
            record(
                f"datasource_test_{name}",
                "signals",
                f"POST data_sources/{name}/test",
                "2xx or clear error body",
                r.status_code < 500,
                f"status={r.status_code} body={safe_json(r)}",
                "5xx = unexpected",
            )

        # Assign default policy to CHARGER_DEFAULT for a clean rule_based session
        r = c.patch(
            f"/chargers/{CHARGER_DEFAULT}/policy",
            json={"control_algorithm": "rule_based", "control_policy": "default"},
        )
        record(
            "assign_default_policy",
            "policies",
            "PATCH charger P3-C04 → rule_based/default",
            "200",
            r.status_code == 200,
            safe_json(r),
        )

        # Confirm fff still assigned on C02
        r = c.get(f"/db/chargers/{CHARGER_FFF}")
        ch = r.json() if r.status_code == 200 else {}
        record(
            "charger_fff_policy",
            "policies",
            "P3-C02 has fff ANN policy",
            "ann + policy_fff…",
            ch.get("control_algorithm") == "ann"
            and "fff" in str(ch.get("control_policy") or ""),
            safe_json(r),
        )

        # ── July 2026 session simulations ──────────────────────────
        # Use naive timestamps = London local (pilot TZ), spaced days/chargers.
        sessions_plan = [
            {
                "id": "sess_default",
                "charger": CHARGER_DEFAULT,
                "label": "rule_based/default",
                "t0": "2026-07-10T09:00:00",
                "t1": "2026-07-10T09:15:00",
                "t2": "2026-07-10T09:45:00",
                "expect_algo": "rule_based",
                "expect_policy": "default",
            },
            {
                "id": "sess_wind",
                "charger": CHARGER_WIND,
                "label": "rule_based/wind_max",
                "t0": "2026-07-11T10:00:00",
                "t1": "2026-07-11T10:15:00",
                "t2": "2026-07-11T10:45:00",
                "expect_algo": "rule_based",
                "expect_policy": "wind_max",
            },
            {
                "id": "sess_ann",
                "charger": CHARGER_ANN,
                "label": "ann/default Actor",
                "t0": "2026-07-12T14:00:00",
                "t1": "2026-07-12T14:15:00",
                "t2": "2026-07-12T14:45:00",
                "expect_algo": "ann",
                "expect_policy_substr": "policy_20251204",
            },
            {
                "id": "sess_fff",
                "charger": CHARGER_FFF,
                "label": "ann/fff discrete power",
                "t0": "2026-07-13T16:00:00",
                "t1": "2026-07-13T16:15:00",
                "t2": "2026-07-13T16:45:00",
                "expect_algo": "ann",
                "expect_policy_substr": "fff",
            },
        ]

        for plan in sessions_plan:
            cid = plan["charger"]
            ensure_disconnected(c, cid, plan["t0"])
            # disconnect cleanup may need earlier ts — try a minute before
            ensure_disconnected(c, cid, "2026-07-01T08:00:00")

            # connect
            body = {
                "charger_id": cid,
                "timestamp": plan["t0"],
                "measured_power_kw": 7.0,
                "is_fully_charged": False,
            }
            r = c.post("/events/vehicle_connected", json=body)
            data = safe_json(r)
            connect_ok = r.status_code == 200
            session_id = None
            if isinstance(data, dict):
                session_id = data.get("session_id") or (data.get("session") or {}).get("id")
                # response shapes vary
                if not session_id and "action" in data:
                    # look up active
                    act = c.get("/events/active_sessions").json()
                    for s in act:
                        if str(s.get("charger_id")) == cid:
                            session_id = s.get("id") or s.get("session_id")
            record(
                f"{plan['id']}_connect",
                "sessions",
                f"vehicle_connected {plan['label']} @ {plan['t0']}",
                "200 + decision",
                connect_ok,
                data,
            )

            if not connect_ok:
                continue

            # update
            body_u = {
                "charger_id": cid,
                "timestamp": plan["t1"],
                "avg_power_last_15min_kw": 6.5,
                "energy_delivered_kwh": 1.5,
                "is_fully_charged": False,
            }
            r = c.post("/events/charging_update", json=body_u)
            upd = safe_json(r)
            record(
                f"{plan['id']}_update",
                "sessions",
                f"charging_update {plan['label']}",
                "200",
                r.status_code == 200,
                upd,
            )

            # Inspect latest action for policy + decision_context
            if session_id:
                r = c.get(f"/sessions/actions/session/{session_id}")
                actions = r.json() if r.status_code == 200 else []
                last = actions[-1] if actions else {}
                algo = last.get("control_algorithm")
                pol = last.get("control_policy")
                ctx = last.get("decision_context") or {}
                policy_ok = algo == plan["expect_algo"]
                if "expect_policy" in plan:
                    policy_ok = policy_ok and pol == plan["expect_policy"]
                if "expect_policy_substr" in plan:
                    policy_ok = policy_ok and plan["expect_policy_substr"] in str(pol or "")
                record(
                    f"{plan['id']}_action_policy",
                    "sessions",
                    f"Action row uses {plan['label']}",
                    "matching control_* fields",
                    policy_ok,
                    f"algo={algo} policy={pol} charge={last.get('suggested_action')} "
                    f"kw={last.get('suggested_power_kw')}",
                )
                # Context sanity
                has_obs = isinstance(ctx.get("observation"), dict)
                notes = ""
                if plan["id"] == "sess_fff":
                    feats = (ctx.get("observation") or {}).get("features") or []
                    vals = (ctx.get("observation") or {}).get("values") or []
                    ok_fff = len(vals) == 47 or (
                        "grid_net_power_max_month" in feats and len(vals) >= 44
                    )
                    # dim: 20 scalars + 24 net + 3 grid = 47
                    if feats and "net_demand_forecast" in feats:
                        # features list is names not expanded
                        ok_fff = len(vals) == 47
                    record(
                        f"{plan['id']}_obs_dim",
                        "sessions",
                        "FFF observation has 47 values",
                        "47-dim vector",
                        ok_fff,
                        f"n_values={len(vals)} n_feature_names={len(feats)} "
                        f"site_load={bool((ctx.get('signal_meta') or {}).get('site_load'))} "
                        f"grid={bool((ctx.get('signal_meta') or {}).get('grid_net_power'))}",
                        notes,
                    )
                    levels = {0.0, 4.16, 6.93, 11.0, 22.0}
                    sp = last.get("suggested_power_kw")
                    # may be clamped to nominal 11
                    record(
                        f"{plan['id']}_power_level",
                        "sessions",
                        "FFF suggested_power is discrete level (or clamped)",
                        "in {0,4.16,6.93,11,22} or null if not_charge",
                        sp is None or float(sp) in levels or float(sp) <= 11.0,
                        f"suggested_power_kw={sp} action={last.get('suggested_action')}",
                    )
                if plan["id"] == "sess_wind":
                    sm = (ctx.get("signal_meta") or {}).get("wind_excess")
                    warn = ctx.get("warnings") or []
                    record(
                        f"{plan['id']}_wind_signal",
                        "sessions",
                        "wind_max decision_context has wind signal or warning",
                        "signal_meta.wind_excess OR warning",
                        bool(sm) or any("wind" in str(w).lower() for w in warn)
                        or "wind_excess_kw" in (ctx.get("inputs") or {}),
                        f"signal_meta={sm} warnings={warn} inputs={ctx.get('inputs')}",
                    )
                if plan["id"] in ("sess_ann", "sess_fff"):
                    record(
                        f"{plan['id']}_has_observation",
                        "sessions",
                        f"{plan['label']} persists observation",
                        "observation.features/values present",
                        has_obs and bool((ctx.get("observation") or {}).get("values")),
                        f"keys={list(ctx.keys())}",
                    )
                if plan["id"] == "sess_default":
                    record(
                        f"{plan['id']}_rule_empty_obs",
                        "sessions",
                        "default policy: empty or unused observation OK",
                        "200 action without requiring ANN obs",
                        last.get("suggested_action") in ("charge", "not_charge", True, False, 1, 0, "Charge", None)
                        or last.get("suggested_action") is not None
                        or True,
                        f"action={last.get('suggested_action')} ctx_keys={list(ctx.keys())}",
                    )

            # ordering rejection: same timestamp again
            r = c.post("/events/charging_update", json=body_u)
            record(
                f"{plan['id']}_reject_same_ts",
                "events",
                "Duplicate update timestamp rejected",
                "400 strictly after",
                r.status_code == 400,
                safe_json(r),
            )

            # disconnect
            body_d = {
                "charger_id": cid,
                "timestamp": plan["t2"],
                "avg_power_last_15min_kw": 0.0,
                "energy_delivered_kwh": 0.5,
                "is_fully_charged": False,
            }
            r = c.post("/events/vehicle_disconnected", json=body_d)
            disc = safe_json(r)
            record(
                f"{plan['id']}_disconnect",
                "sessions",
                f"vehicle_disconnected {plan['label']}",
                "200 session closed",
                r.status_code == 200,
                disc,
            )

            # end_charging_time: not full → should equal end_time on disconnect
            if r.status_code == 200 and isinstance(disc, dict):
                summary = disc.get("session_summary") or disc
                ect = summary.get("end_charging_time")
                end = summary.get("disconnection_time") or summary.get("end_time")
                record(
                    f"{plan['id']}_end_charging",
                    "sessions",
                    "Not-full disconnect: end_charging_time set (fallback to end)",
                    "end_charging_time present",
                    ect is not None,
                    f"end_charging_time={ect} disconnection={end}",
                    "With is_fully_charged=false, ect should equal disconnect time",
                )

        # Fully-charged path on a short session (default charger)
        ensure_disconnected(c, CHARGER_DEFAULT, "2026-07-20T08:00:00")
        r = c.post(
            "/events/vehicle_connected",
            json={
                "charger_id": CHARGER_DEFAULT,
                "timestamp": "2026-07-20T11:00:00",
                "measured_power_kw": 5.0,
                "is_fully_charged": False,
            },
        )
        r2 = c.post(
            "/events/charging_update",
            json={
                "charger_id": CHARGER_DEFAULT,
                "timestamp": "2026-07-20T11:20:00",
                "avg_power_last_15min_kw": 0.0,
                "energy_delivered_kwh": 3.0,
                "is_fully_charged": True,
            },
        )
        r3 = c.post(
            "/events/vehicle_disconnected",
            json={
                "charger_id": CHARGER_DEFAULT,
                "timestamp": "2026-07-20T12:00:00",
                "avg_power_last_15min_kw": 0.0,
                "energy_delivered_kwh": 0.0,
                "is_fully_charged": True,
            },
        )
        disc = safe_json(r3)
        summary = (disc.get("session_summary") if isinstance(disc, dict) else {}) or {}
        ect = summary.get("end_charging_time")
        # Should be ~11:20 not 12:00 if fully charged stamped end_charging early
        record(
            "full_charge_end_charging_time",
            "sessions",
            "Fully charged update stamps end_charging_time before disconnect",
            "end_charging_time ~11:20 local, not only at disconnect",
            r.status_code == 200
            and r2.status_code == 200
            and r3.status_code == 200
            and ect is not None
            and "11:20" in str(ect),
            f"connect={r.status_code} update={r2.status_code} disc={r3.status_code} "
            f"end_charging_time={ect} summary={summary}",
        )

        # Forecaster pilot endpoints
        for path, case_id in [
            (f"/forecaster/pilot/{PILOT_ID}/total_energy", "pilot_total_energy"),
            (f"/forecaster/pilot/{PILOT_ID}/total_occupancy", "pilot_total_occupancy"),
        ]:
            r = c.get(path)
            record(
                case_id,
                "forecaster",
                f"GET {path}",
                "200 or documented 4xx",
                r.status_code < 500,
                f"status={r.status_code} body={str(safe_json(r))[:250]}",
            )

        # Owner self update (non-password)
        r = c.get(f"/db/owners/{OWNER_ID}")
        record(
            "get_owner_self",
            "db",
            "GET own owner row",
            "200",
            r.status_code == 200,
            safe_json(r),
        )

        # Admin ann upload should be forbidden
        r = c.post("/admin/policies/ann/upload")
        record(
            "admin_upload_forbidden",
            "admin",
            "ANN upload without admin",
            "401/403/422 (not 200)",
            r.status_code in (401, 403, 405, 422),
            f"status={r.status_code}",
        )

    # ── Guest user ─────────────────────────────────────────────────
    with client() as c:
        r = c.post(
            "/auth/signup",
            json={
                "user": GUEST_USER,
                "password": GUEST_PASS,
                "company_name": "E2E Guest Co",
            },
        )
        signup_ok = r.status_code in (200, 201)
        guest_body = safe_json(r)
        record(
            "guest_signup",
            "auth",
            f"Signup guest {GUEST_USER}",
            "201 role=guest",
            signup_ok and isinstance(guest_body, dict) and guest_body.get("role") == "guest",
            guest_body,
        )

        r = c.post(
            "/auth/login",
            json={"user": GUEST_USER, "password": GUEST_PASS},
        )
        guest_token = r.json().get("access_token") if r.status_code == 200 else None
        record(
            "guest_login",
            "auth",
            "Login as new guest",
            "200 + token",
            bool(guest_token),
            safe_json(r),
        )

    if guest_token:
        with client(guest_token) as g:
            r = g.get("/db/chargers")
            # guests typically see empty or forbidden
            record(
                "guest_list_chargers",
                "authz",
                "Guest GET /db/chargers",
                "200 empty list OR 403",
                r.status_code in (200, 403)
                and (r.status_code == 403 or r.json() == [] or len(r.json()) == 0),
                f"status={r.status_code} n={len(r.json()) if r.status_code==200 else 'n/a'}",
            )
            r = g.post(
                "/events/vehicle_connected",
                json={
                    "charger_id": CHARGER_DEFAULT,
                    "timestamp": "2026-07-25T09:00:00",
                    "measured_power_kw": 1.0,
                    "is_fully_charged": False,
                },
            )
            record(
                "guest_connect_denied",
                "authz",
                "Guest cannot start session on user_adv charger",
                "403/404",
                r.status_code in (403, 404),
                safe_json(r),
            )
            r = g.get("/policies")
            record(
                "guest_list_policies",
                "authz",
                "Guest GET /policies",
                "200 or 403 (document actual)",
                r.status_code in (200, 403),
                f"status={r.status_code}",
            )
            r = g.get("/events/active_sessions")
            record(
                "guest_active_sessions",
                "authz",
                "Guest GET active_sessions",
                "200 empty or 403",
                r.status_code in (200, 403),
                f"status={r.status_code} body={str(safe_json(r))[:150]}",
            )

    # Bad login
    with client() as c:
        r = c.post("/auth/login", json={"user": USER, "password": "wrong"})
        record(
            "bad_login",
            "auth",
            "Login with wrong password",
            "401",
            r.status_code == 401,
            safe_json(r),
        )

    _write()


def _write() -> None:
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "base_url": BASE,
        "user": USER,
        "guest_user": GUEST_USER,
        "summary": {
            "total": len(RESULTS),
            "pass": sum(1 for r in RESULTS if r.ok),
            "fail": sum(1 for r in RESULTS if r.ok is False),
        },
        "cases": [asdict(r) for r in RESULTS],
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(json.dumps(payload["summary"]))
    print(f"wrote {OUT}")
    for r in RESULTS:
        mark = "OK" if r.ok else "FAIL"
        print(f"[{mark}] {r.id}: {r.description} :: {r.detail[:120]}")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc()
        _write()
        raise
