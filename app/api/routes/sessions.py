import csv
import io
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile

from app.db.session import SessionLocal
from app.models import Actions, Chargers, ChargingSessions, Owners, Pilot
from app.schemas.database import ActionsRead, ChargingSessionsRead
from app.services.common.auth import TokenData, get_current_user
from app.services.common.authorization import (
    ensure_charger_access,
    ensure_pilot_access,
    ensure_session_access,
    get_accessible_charger_ids,
    is_admin,
    require_roles,
)
from app.config import CSV_IMPORT_MAX_BYTES, CSV_IMPORT_MAX_ROWS
from app.db.ev_duration_cdf_defaults import DEFAULT_DURATION_CDF
from app.models import EvDurationCdf, EvForecastStats
from app.services.common.constants import CSV_SESSION_DT_FMT, CSV_SESSION_DT_FMT_TZ, GENERIC_CHARGER_ID, NEW_CHARGER_NOMINAL_POWER_KW
from app.services.common.db_utils import get_local_hour, get_pilot_tz_for_charger, to_response_tz

router = APIRouter(prefix="/sessions", tags=["sessions"])

@router.post("/import_csv/{pilot_id}")
async def import_sessions_csv(
    pilot_id: UUID,
    file: UploadFile,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Upload a semicolon-delimited CSV of historical charging sessions for a pilot.

    Expected columns (case-insensitive, order-independent):
      start_time, end_time, end_charging_time, energy_delivered_kwh,
      id_charger (holds charger name), controlled charging points

    Timestamps: DD.MM.YYYY HH:MM — no timezone in the CSV format, treated as UTC
    and stored as UTC-aware (consistent with the repo convention).
    Chargers are matched by name within the pilot; unrecognised names are auto-created
    with nominal_power=11 kW. 
    At most {CSV_IMPORT_MAX_ROWS} rows are imported. File size limit: {CSV_IMPORT_MAX_BYTES} bytes.
    """
    db = SessionLocal()
    try:
        # Authorization: admin can import for any pilot; user/user-adv only their own pilot
        pilot = ensure_pilot_access(
            db, current_user, pilot_id,
            allow_user=True, allow_guest=False, require_user_ownership=True,
        )

        # Size check — read one extra byte to detect oversized files without loading them fully
        content_bytes = await file.read(CSV_IMPORT_MAX_BYTES + 1)
        if len(content_bytes) > CSV_IMPORT_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum allowed is {CSV_IMPORT_MAX_BYTES} bytes (~300 sessions).",
            )

        try:
            text = content_bytes.decode("utf-8-sig")  # strips BOM if present
        except UnicodeDecodeError:
            raise HTTPException(status_code=422, detail="File must be UTF-8 encoded.")

        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        if not reader.fieldnames:
            raise HTTPException(status_code=422, detail="CSV file is empty or has no header row.")

        # Normalise header names once
        reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]

        def _parse_dt(raw: str) -> datetime:
            """Parse DD.MM.YYYY HH:MM [+HHMM] → UTC-aware datetime."""
            raw = raw.strip()
            try:
                return datetime.strptime(raw, CSV_SESSION_DT_FMT_TZ).astimezone(timezone.utc)
            except ValueError:
                return datetime.strptime(raw, CSV_SESSION_DT_FMT).replace(tzinfo=timezone.utc)

        required_cols = {"start_time", "end_time", "end_charging_time", "id_charger", "energy_delivered_kwh"}
        missing_cols = required_cols - set(reader.fieldnames)
        if missing_cols:
            raise HTTPException(
                status_code=422,
                detail=f"Missing required CSV columns: {', '.join(sorted(missing_cols))}",
            )

        # Pre-load all chargers for this pilot to avoid N+1 queries
        existing_chargers: dict[str, Chargers] = {
            c.name: c
            for c in db.query(Chargers).filter(Chargers.id_pilot == pilot_id).all()
        }

        created_sessions = 0
        new_charger_names: set[str] = set()
        affected_charger_names: set[str] = set()  # new + existing chargers that got sessions
        skipped: list[dict] = []
        rows_seen = 0

        for row in reader:
            row_num = rows_seen + 2  # 1-based; row 1 is the header
            rows_seen += 1

            if rows_seen > CSV_IMPORT_MAX_ROWS:
                skipped.append({"row": row_num, "reason": f"row limit ({CSV_IMPORT_MAX_ROWS}) reached; remaining rows ignored"})
                break

            # Normalise all keys/values
            row = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items()}

            # start_time (required)
            raw_start = row.get("start_time", "")
            if not raw_start:
                skipped.append({"row": row_num, "reason": "missing start_time"})
                continue
            try:
                start_time = _parse_dt(raw_start)
            except ValueError:
                skipped.append({"row": row_num, "reason": f"invalid start_time format: {raw_start!r}"})
                continue

            # end_time (required)
            end_time: datetime | None = None
            raw_end = row.get("end_time", "")
            if not raw_end:
                skipped.append({"row": row_num, "reason": "missing end_time"})
                continue
            try:
                end_time = _parse_dt(raw_end)
            except ValueError:
                skipped.append({"row": row_num, "reason": f"invalid end_time format: {raw_end!r}"})
                continue

            # end_charging_time (required)
            end_charging_time: datetime | None = None
            raw_ect = row.get("end_charging_time", "")
            if not raw_ect:
                skipped.append({"row": row_num, "reason": "missing end_charging_time"})
                continue
            try:
                end_charging_time = _parse_dt(raw_ect)
            except ValueError:
                skipped.append({"row": row_num, "reason": f"invalid end_charging_time format: {raw_ect!r}"})
                continue

            # energy_delivered_kwh (required)
            raw_energy = row.get("energy_delivered_kwh", "")
            if not raw_energy:
                skipped.append({"row": row_num, "reason": "missing energy_delivered_kwh"})
                continue
            try:
                energy_kwh = float(raw_energy.replace(",", "."))
            except ValueError:
                skipped.append({"row": row_num, "reason": f"invalid energy_delivered_kwh: {raw_energy!r}"})
                continue

            # controlled charging points (optional, default 1)
            raw_ccp = row.get("controlled charging points", "")
            try:
                controlled_charging_points = int(raw_ccp) if raw_ccp else 1
            except ValueError:
                controlled_charging_points = 1

            # charger name (CSV column is called id_charger but holds a human name)
            charger_name = row.get("id_charger", "").strip()
            if not charger_name:
                skipped.append({"row": row_num, "reason": "missing id_charger (charger name)"})
                continue

            # Resolve or auto-create charger scoped to this pilot
            charger = existing_chargers.get(charger_name)
            if charger is None:
                charger = Chargers(
                    id=uuid4(),
                    name=charger_name,
                    type="AC",
                    latitude=0.0,
                    longitude=0.0,
                    nominal_power=NEW_CHARGER_NOMINAL_POWER_KW,
                    plugs=1,
                    id_owner=pilot.id_owner,
                    id_pilot=pilot_id,
                )
                db.add(charger)
                db.flush()  # obtain PK before referencing in sessions
                existing_chargers[charger_name] = charger
                new_charger_names.add(charger_name)

            # duration in hours (None if end_time absent)
            duration: float | None = None
            if end_time is not None:
                duration = (end_time - start_time).total_seconds() / 3600.0

            db.add(ChargingSessions(
                id=uuid4(),
                id_charger=charger.id,
                start_time=start_time,
                end_time=end_time,
                end_charging_time=end_charging_time,
                duration=duration,
                energy_delivered_kwh=energy_kwh,
                forecasted_energy_kwh=0.0,
                forecasted_energy_kwh_std=0.0,
                forecasted_duration_hours=0.0,
                forecasted_duration_hours_std=0.0,
                controlled_charging_points=controlled_charging_points,
                active=False,
            ))
            created_sessions += 1
            affected_charger_names.add(charger_name)

        db.commit()

        # ── Single rollout: recompute ev_duration_cdf + ev_forecast_stats ────
        # Runs once after all sessions are committed, for every charger that
        # received at least one new session (both newly created and pre-existing).
        # Queries the full session history for each charger so the stats reflect
        # all data, not just what was just imported.
        all_affected_sessions: list = []
        for charger_name in affected_charger_names:
            charger = existing_chargers[charger_name]
            charger_id = charger.id

            all_sessions = (
                db.query(ChargingSessions)
                .filter(
                    ChargingSessions.id_charger == charger_id,
                    ChargingSessions.end_time.isnot(None),
                )
                .all()
            )
            all_affected_sessions.extend(all_sessions)

            # ev_duration_cdf — empirical CDF of session durations (local_hour=-1 = global)
            durations = [
                s.duration for s in all_sessions
                if s.duration is not None and s.duration >= 0
            ]
            if durations:
                n_dur = len(durations)
                for horizon in DEFAULT_DURATION_CDF.keys():
                    prob = sum(1 for d in durations if d <= horizon) / n_dur
                    db.add(EvDurationCdf(
                        id=uuid4(),
                        id_charger=charger_id,
                        local_hour=-1,
                        horizon_hours=horizon,
                        probability=prob,
                        sample_count=n_dur,
                    ))

            # ev_forecast_stats — per-local-hour mean/std of energy and duration
            pilot_tz = pilot.timezone_name
            hourly: dict[int, list[tuple[float, float]]] = defaultdict(list)
            for s in all_sessions:
                if s.start_time is not None:
                    h = get_local_hour(s.start_time, pilot_tz)
                    dur = s.duration if s.duration is not None else 0.0
                    hourly[h].append((s.energy_delivered_kwh, dur))

            for hour, entries in hourly.items():
                energies = [e for e, _ in entries]
                durs = [d for _, d in entries]
                mean_e = statistics.mean(energies)
                mean_d = statistics.mean(durs)
                std_e = statistics.stdev(energies) if len(energies) >= 2 else 0.0
                std_d = statistics.stdev(durs) if len(durs) >= 2 else 0.0
                db.add(EvForecastStats(
                    id=uuid4(),
                    id_charger=charger_id,
                    local_hour=hour,
                    mean_energy_kwh=mean_e,
                    std_energy_kwh=std_e,
                    mean_duration_hours=mean_d,
                    std_duration_hours=std_d,
                    sample_count=len(entries),
                ))

        # ── Rebuild generic charger stats from all sessions (last 1000) ──
        if affected_charger_names:
            generic_id = UUID(GENERIC_CHARGER_ID)

            # Use the 1000 most recent completed sessions across all chargers/pilots
            generic_sessions = (
                db.query(ChargingSessions)
                .filter(ChargingSessions.end_time.isnot(None))
                .order_by(ChargingSessions.end_time.desc())
                .limit(1000)
                .all()
            )

            durations = [
                s.duration for s in generic_sessions
                if s.duration is not None and s.duration >= 0
            ]
            if durations:
                n_dur = len(durations)
                for horizon in DEFAULT_DURATION_CDF.keys():
                    prob = sum(1 for d in durations if d <= horizon) / n_dur
                    db.add(EvDurationCdf(
                        id=uuid4(),
                        id_charger=generic_id,
                        local_hour=-1,
                        horizon_hours=horizon,
                        probability=prob,
                        sample_count=n_dur,
                    ))

            # All chargers in existing_chargers belong to this pilot → all share pilot_tz.
            # generic_sessions spans all pilots, so sessions from other pilots fall through
            # to the DB-lookup fallback.
            charger_tz_cache: dict[str, str] = {str(c.id): pilot_tz for c in existing_chargers.values()}
            hourly_generic: dict[int, list[tuple[float, float]]] = defaultdict(list)
            for s in generic_sessions:
                if s.start_time is not None:
                    cid = str(s.id_charger)
                    if cid not in charger_tz_cache:
                        charger_tz_cache[cid] = get_pilot_tz_for_charger(db, cid)
                    h = get_local_hour(s.start_time, charger_tz_cache[cid])
                    dur = s.duration if s.duration is not None else 0.0
                    hourly_generic[h].append((s.energy_delivered_kwh, dur))

            for hour, entries in hourly_generic.items():
                energies = [e for e, _ in entries]
                durs = [d for _, d in entries]
                mean_e = statistics.mean(energies)
                mean_d = statistics.mean(durs)
                std_e = statistics.stdev(energies) if len(energies) >= 2 else 0.0
                std_d = statistics.stdev(durs) if len(durs) >= 2 else 0.0
                db.add(EvForecastStats(
                    id=uuid4(),
                    id_charger=generic_id,
                    local_hour=hour,
                    mean_energy_kwh=mean_e,
                    std_energy_kwh=std_e,
                    mean_duration_hours=mean_d,
                    std_duration_hours=std_d,
                    sample_count=len(entries),
                ))

            db.commit()

        notes = []
        if new_charger_names:
            notes.append(
                f"Auto-created chargers have nominal_power={NEW_CHARGER_NOMINAL_POWER_KW} kW, "
                "type=AC, plugs=Unknown, latitude=0, longitude=0. "
                "Update them via PUT /db/chargers/{id} if needed."
            )

        return {
            "created_sessions": created_sessions,
            "created_chargers": len(new_charger_names),
            "new_charger_names": sorted(new_charger_names),
            "skipped_rows": skipped,
            "notes": notes,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/all_sessions")
def get_all_sessions(
    current_user: TokenData = Depends(get_current_user),
    tz: str = Query(default=None, description="Timezone for timestamps in the response. Defaults to each charger's pilot timezone."),
):
    """
    Return all charging sessions stored in the DB
    """
    db = SessionLocal()
    try:
        sessions = db.query(ChargingSessions).all()
        if not is_admin(current_user):
            charger_ids = set(get_accessible_charger_ids(db, current_user, allow_guest=False))
            sessions = [s for s in sessions if s.id_charger in charger_ids]

        return [
            {
                "id": s.id,
                "charger_id": s.id_charger,
                "start_time": to_response_tz(s.start_time, tz if tz is not None else get_pilot_tz_for_charger(db, str(s.id_charger))),
                "end_time": to_response_tz(s.end_time, tz if tz is not None else get_pilot_tz_for_charger(db, str(s.id_charger))) if s.end_time else None,
                "energy_delivered_kwh": s.energy_delivered_kwh,
                "forecasted_energy_kwh": s.forecasted_energy_kwh,
                "forecasted_duration_hours": s.forecasted_duration_hours,
            }
            for s in sessions
        ]
    finally:
        db.close()

@router.get("/all_sessions/{charger_id}")
def get_charger_sessions(
    charger_id: str,
    current_user: TokenData = Depends(get_current_user),
    tz: str = Query(default=None, description="Timezone for timestamps in the response. Defaults to the charger's pilot timezone."),
):
    """
    Return all sessions of an specific charger stored in the DB
    """
    db = SessionLocal()
    try:
        ensure_charger_access(
            db,
            current_user,
            charger_id,
            allow_charger_owner=True,
            allow_pilot_owner=True,
            allow_guest=False,
        )

        sessions = (
            db.query(ChargingSessions)
            .filter(ChargingSessions.id_charger == charger_id)
            .all()
        )

        if not sessions:
            raise HTTPException(status_code=404, detail="Sessions for that charger ID not found")

        effective_tz = tz if tz is not None else get_pilot_tz_for_charger(db, charger_id)
        return [
            {
                "id": s.id,
                "start_time": to_response_tz(s.start_time, effective_tz),
                "end_time": to_response_tz(s.end_time, effective_tz) if s.end_time else None,
                "energy_delivered_kwh": s.energy_delivered_kwh,
            }
            for s in sessions
        ]
    finally:
        db.close()


@router.get("/owner/{owner_id}")
def list_sessions_by_owner(
    owner_id: UUID,
    current_user: TokenData = Depends(get_current_user),
    tz: str = Query(default=None, description="Timezone for timestamps in the response. Defaults to each charger's pilot timezone."),
):
    db = SessionLocal()
    try:
        if not is_admin(current_user):
            require_roles(current_user, "user")
            if current_user.owner_id != owner_id:
                raise HTTPException(status_code=403, detail="Forbidden")

        owner = db.query(Owners).filter(Owners.id == owner_id).first()
        if not owner:
            raise HTTPException(status_code=404, detail="Owner not found")

        sessions = (
            db.query(ChargingSessions)
            .join(Chargers, ChargingSessions.id_charger == Chargers.id)
            .filter(Chargers.id_owner == owner_id)
            .all()
        )
        return [
            {
                "id": s.id,
                "id_charger": s.id_charger,
                "start_time": to_response_tz(s.start_time, tz if tz is not None else get_pilot_tz_for_charger(db, str(s.id_charger))),
                "end_time": to_response_tz(s.end_time, tz if tz is not None else get_pilot_tz_for_charger(db, str(s.id_charger))) if s.end_time else None,
                "end_charging_time": to_response_tz(s.end_charging_time, tz if tz is not None else get_pilot_tz_for_charger(db, str(s.id_charger))) if s.end_charging_time else None,
                "energy_delivered_kwh": s.energy_delivered_kwh,
                "forecasted_energy_kwh": s.forecasted_energy_kwh,
                "forecasted_duration_hours": s.forecasted_duration_hours,
                "duration": s.duration,
                "active": s.active,
            }
            for s in sessions
        ]
    finally:
        db.close()


@router.get("/actions/session/{session_id}")
def list_actions_by_session(
    session_id: UUID,
    current_user: TokenData = Depends(get_current_user),
    tz: str = Query(default=None, description="Timezone for current_time in the response. Defaults to the charger's pilot timezone."),
):
    db = SessionLocal()
    try:
        session = ensure_session_access(db, current_user, session_id, allow_guest=False)
        effective_tz = tz if tz is not None else get_pilot_tz_for_charger(db, str(session.id_charger))
        actions = db.query(Actions).filter(Actions.id_cs == session_id).all()
        return [
            {
                "id": a.id,
                "current_time": to_response_tz(a.current_time, effective_tz),
                "current_power_kw": a.current_power_kw,
                "energy_delivered_kwh": a.energy_delivered_kwh,
                "is_fully_charged": a.is_fully_charged,
                "probability_disconnection": a.probability_disconnection,
                "cumulative_duration_probability": a.cumulative_duration_probability,
                "action": a.action,
                "id_cs": a.id_cs,
                "policy": a.policy,
            }
            for a in actions
        ]
    finally:
        db.close()



