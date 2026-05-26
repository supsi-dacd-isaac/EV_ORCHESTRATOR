from __future__ import annotations

import logging
import pickle
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pandas as pd
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import Chargers, ChargingSessions, EvPilotForecastTimeseries
from app.services.ev_forecast.forecast_manifest import ForecastJob, forecast_artifacts_dir
from app.config import FORECAST_ARTIFACT_MAX_AGE_DAYS

if TYPE_CHECKING:
    from app.services.ev_forecast.ev_total_forecaster import ForecasterReg, ForecasterRegProb

logger = logging.getLogger(__name__)

_LEGACY_PICKLE_MODULE = "forecasters_and_artifacts.ev_forecaster"


def _floor_to_sim_step(ts: pd.Timestamp, sim_dt_hours: float) -> pd.Timestamp:
    """Floor *ts* to a grid with spacing ``sim_dt_hours`` (used as simulation / origin anchor)."""
    minutes = int(round(max(sim_dt_hours, 1e-9) * 60))
    if minutes <= 0:
        return ts
    if minutes % 60 == 0:
        return ts.floor(f"{minutes // 60}h")
    return ts.floor(f"{minutes}min")


def _to_utc(value: pd.Timestamp | datetime) -> datetime:
    """
    Normalise a pd.Timestamp or datetime to a UTC-aware Python datetime.
    Columns use TIMESTAMP WITH TIME ZONE; psycopg2 with timezone=True round-trips
    tz-aware datetimes correctly.
    """
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.to_pydatetime()


def _reg_format_min_bars(model: ForecasterReg | ForecasterRegProb) -> int:
    """
    Minimum freq-sized bins so ``format()`` can yield at least one row (lags, rolling windows,
    horizon targets). Matches logic in ``ForecasterReg.format``.
    """
    freq_td = pd.Timedelta(model.freq)
    steps_per_hour = max(int(pd.Timedelta("1h") / freq_td), 1)
    h = model.horizon
    if isinstance(model.lags, list):
        max_lag = max(model.lags) if model.lags else 1
    else:
        max_lag = int(model.lags)
    recent_max = min(steps_per_hour * 4, 17)
    max_lag_bars = max(max_lag, recent_max)
    max_roll = steps_per_hour * 24
    return max_lag_bars + max_roll + h + 10


def _pilot_max_session_end(db: Session, pilot_id: UUID) -> datetime | None:
    """Latest ``end_time`` among completed sessions for this pilot (any charger)."""
    return (
        db.query(func.max(ChargingSessions.end_time))
        .select_from(ChargingSessions)
        .join(Chargers, ChargingSessions.id_charger == Chargers.id)
        .filter(
            Chargers.id_pilot == pilot_id,
            ChargingSessions.end_time.isnot(None),
        )
        .scalar()
    )


def _observed_counts_at(
    db: Session,
    pilot_id: UUID,
    at: pd.Timestamp,
) -> dict[str, int] | None:
    """Per-station count of sessions that started but had not yet ended at *at* (UTC-aware).

    Returns None when no active sessions exist (so ForecasterSim.predict keeps its
    empty-state default rather than receiving an all-zero dict).
    """
    at_utc = _to_utc(at)
    rows = (
        db.query(Chargers.id, func.count(ChargingSessions.id))
        .join(ChargingSessions, ChargingSessions.id_charger == Chargers.id)
        .filter(
            Chargers.id_pilot == pilot_id,
            ChargingSessions.start_time <= at_utc,
            or_(
                ChargingSessions.end_time.is_(None),
                ChargingSessions.end_time > at_utc,
            ),
        )
        .group_by(Chargers.id)
        .all()
    )
    if not rows:
        return None
    return {str(charger_id): int(count) for charger_id, count in rows}


def _sessions_end_after_cutoff_for_prediction(
    model: ForecasterReg | ForecasterRegProb,
    db: Session,
    pilot_id: UUID,
) -> datetime:
    """
    UTC-aware: keep sessions with ``end_time > cutoff`` for prediction.

    Anchors on current wall-clock time so the lookback window always ends at
    "now", regardless of when the last session ended.
    """
    n_bars = _reg_format_min_bars(model)
    delta = n_bars * pd.Timedelta(model.freq)
    anchor = pd.Timestamp.now(tz="UTC")
    start = anchor - delta
    return start.to_pydatetime()


def _register_legacy_forecaster_pickle_modules() -> None:
    """Allow unpickling models saved as ``forecasters_and_artifacts.ev_forecaster``."""
    if _LEGACY_PICKLE_MODULE in sys.modules:
        return
    from app.services.ev_forecast import ev_total_forecaster as ev_forecaster_mod

    pkg = types.ModuleType("forecasters_and_artifacts")
    sys.modules["forecasters_and_artifacts"] = pkg
    sys.modules[_LEGACY_PICKLE_MODULE] = ev_forecaster_mod


def _resolved_artifact_path(job: ForecastJob) -> Path:
    base = forecast_artifacts_dir().resolve()
    path = (base / job.artifact_path).resolve()
    base_s = str(base)
    if not str(path).startswith(base_s) or path == base:
        raise ValueError("artifact_path escapes FORECASTERS_ARTIFACTS_DIR")
    return path


def _reg_ctor_kwargs(job: ForecastJob) -> dict:
    """Match YAML → ForecasterReg / ForecasterRegProb constructor (freq, horizon, lags)."""
    k: dict = {"freq": job.freq, "timezone": job.timezone}
    if job.horizon is not None:
        k["horizon"] = job.horizon
    if job.lags is not None:
        k["lags"] = job.lags
    return k


def _load_pickle(path: Path):
    _register_legacy_forecaster_pickle_modules()
    with open(path, "rb") as f:
        return pickle.load(f)


def _train_and_save_model(job: ForecastJob, df: pd.DataFrame):
    """Fit model from session data and write artifact_path (creates parent dirs)."""
    from app.services.ev_forecast.ev_total_forecaster import (  # noqa: WPS433
        ForecasterReg,
        ForecasterRegProb,
        ForecasterSim,
    )

    path = _resolved_artifact_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)

    if job.kind == "sim":
        model = ForecasterSim(df)
    elif job.kind == "reg_prob":
        model = ForecasterRegProb(
            df,
            cal_fraction=job.cal_fraction,
            **_reg_ctor_kwargs(job),
        )
        model.train(df=df, verbose=-1)
    else:
        model = ForecasterReg(df, **_reg_ctor_kwargs(job))
        model.train(df=df, verbose=-1)

    with open(path, "wb") as f:
        pickle.dump(model, f, protocol=pickle.HIGHEST_PROTOCOL)
    logger.info("Trained and saved forecast artifact %s (kind=%s)", path, job.kind)
    return model


def _ensure_model(
    db: Session,
    job: ForecastJob,
) -> tuple[object | None, dict]:
    """
    Load pickle if present and readable; otherwise train from pilot sessions and save.
    Returns (model, extra) where extra may include artifact_trained: bool and error reason.
    """
    path = _resolved_artifact_path(job)
    extra: dict = {"artifact_trained": False}

    if path.is_file() and path.stat().st_size > 0:
        stale = (
            FORECAST_ARTIFACT_MAX_AGE_DAYS > 0
            and (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 86400
            > FORECAST_ARTIFACT_MAX_AGE_DAYS
        )
        if stale:
            logger.info(
                "Artifact %s is older than %d day(s); will retrain.",
                path, FORECAST_ARTIFACT_MAX_AGE_DAYS,
            )
        else:
            try:
                return _load_pickle(path), extra
            except (OSError, pickle.UnpicklingError, EOFError, AttributeError) as e:
                logger.warning("Failed to load %s (%s); will retrain if data allows", path, e)

    df = charging_sessions_to_forecast_df(db, job.id_pilot)
    if df.empty:
        return None, {**extra, "error": "no_completed_sessions"}

    if job.kind == "sim":
        try:
            model = _train_and_save_model(job, df)
        except Exception as e:
            logger.exception("Training ForecasterSim failed")
            return None, {**extra, "error": f"train_failed:{e!s}"}
        return model, {**extra, "artifact_trained": True}

    from app.services.ev_forecast.ev_total_forecaster import ForecasterReg  # noqa: WPS433

    probe = ForecasterReg(df, **_reg_ctor_kwargs(job))
    X, _, _ = probe.format(df=df, inference=True)
    if X.empty:
        return None, {**extra, "error": "insufficient_history_for_training"}

    try:
        model = _train_and_save_model(job, df)
    except Exception as e:
        logger.exception("Training failed for job kind=%s", job.kind)
        return None, {**extra, "error": f"train_failed:{e!s}"}

    return model, {**extra, "artifact_trained": True}


def charging_sessions_to_forecast_df(
    db: Session,
    pilot_id: UUID,
    *,
    end_time_after: datetime | None = None,
) -> pd.DataFrame:
    """
    Completed sessions for the pilot. If ``end_time_after`` is set, only rows with
    ``end_time > end_time_after`` (naive UTC wall time) — enough for prediction when
    combined with a model-derived lookback; training passes ``None`` for full history.
    """
    q = (
        db.query(ChargingSessions, Chargers)
        .join(Chargers, ChargingSessions.id_charger == Chargers.id)
        .filter(
            Chargers.id_pilot == pilot_id,
            ChargingSessions.end_time.isnot(None),
        )
    )
    if end_time_after is not None:
        q = q.filter(ChargingSessions.end_time > end_time_after)
    rows: list[dict] = []
    for session, charger in q.all():
        duration_h = session.duration
        if duration_h is None and session.end_time and session.start_time:
            duration_h = (session.end_time - session.start_time).total_seconds() / 3600.0
        if duration_h is None:
            duration_h = 0.0
        end_conn = session.end_time
        rows.append(
            {
                "start_time": session.start_time,
                "end_time_connection": end_conn,
                "energy (kWh)": float(session.energy_delivered_kwh),
                "station_id": str(charger.id),
                "duration_connection": float(duration_h),
            }
        )
    return pd.DataFrame(rows)


def _quantile_dict_from_row(row: pd.Series) -> dict | None:
    out = {}
    for k, v in row.items():
        if not isinstance(k, str):
            continue
        if k.startswith("presence_q") or k.startswith("energy_q"):
            if pd.notna(v):
                out[k] = float(v)
    return out or None


def _persist_reg_like(
    db: Session,
    job: ForecastJob,
    pred: pd.DataFrame,
    *,
    use_pdf: bool,
    run_at: datetime,
    forecast_anchor: pd.Timestamp | datetime,
) -> int:
    """
    ``forecast_anchor`` is the time index of the last formatted input row (as-of instant the
    forecast conditions on). Stored as ``origin_time``; ``run_at`` remains job execution time.
    """
    artifact = job.artifact_path
    count = 0
    anchor_dt = _to_utc(forecast_anchor)
    to_add: list[EvPilotForecastTimeseries] = []
    for _, row in pred.iterrows():
        ft = row["time"]
        if not isinstance(ft, pd.Timestamp):
            ft = pd.Timestamp(ft)
        ft_dt = _to_utc(ft)
        qjson = None
        if use_pdf:
            qjson = _quantile_dict_from_row(row)
        to_add.append(
            EvPilotForecastTimeseries(
                id=uuid4(),
                id_pilot=job.id_pilot,
                run_at=run_at,
                origin_time=anchor_dt,
                forecast_time=ft_dt,
                presence=float(row["presence"]),
                energy_kwh=float(row["energy_consumed"]),
                artifact_name=artifact,
                quantiles_json=qjson,
            )
        )
        count += 1
    db.add_all(to_add)
    db.commit()
    return count


def _persist_sim(
    db: Session,
    job: ForecastJob,
    pred: pd.DataFrame,
    *,
    run_at: datetime,
    start_time: pd.Timestamp,
) -> int:
    artifact = job.artifact_path
    to_add: list[EvPilotForecastTimeseries] = []
    for ts, row in pred.iterrows():
        ft = ts if isinstance(ts, pd.Timestamp) else pd.Timestamp(ts)
        to_add.append(
            EvPilotForecastTimeseries(
                id=uuid4(),
                id_pilot=job.id_pilot,
                run_at=run_at,
                origin_time=_to_utc(start_time),
                forecast_time=_to_utc(ft),
                presence=float(row["connected_evs"]),
                energy_kwh=float(row["energy_delivered_kwh"]),
                artifact_name=artifact,
                quantiles_json=None,
            )
        )
    db.add_all(to_add)
    db.commit()
    return len(to_add)


def _apply_reg_post_processing(
    pred: pd.DataFrame,
    now: pd.Timestamp,
    freq_td: pd.Timedelta,
    current_occupancy: int,
) -> pd.DataFrame:
    """Post-process a reg/reg_prob prediction DataFrame:

    1. Re-anchor ``time`` so the horizon starts from *now* (not from the last
       historical bin, which could be hours in the past).
    2. Round ``presence`` to the nearest integer and clip to ≥ 0.
    3. Force the first forecast step's ``presence`` to be ≥ *current_occupancy*
       (vehicles already connected cannot disappear in the very next step).
    4. Clip ``energy_consumed`` to ≥ 0.
    """
    pred = pred.copy()
    n = len(pred)
    pred["time"] = [now + (i + 1) * freq_td for i in range(n)]

    presences = pred["presence"].to_numpy(dtype=float).copy()
    for i in range(n):
        v = max(0.0, presences[i])
        v = float(round(v))
        if i == 0:
            v = max(v, float(current_occupancy))
        presences[i] = v
    pred["presence"] = presences

    pred["energy_consumed"] = pred["energy_consumed"].clip(lower=0.0)
    return pred


def run_forecast_job(db: Session, job: ForecastJob) -> dict:
    from app.services.ev_forecast.ev_total_forecaster import (  # noqa: WPS433
        ForecasterReg,
        ForecasterRegProb,
        ForecasterSim,
    )

    run_at = datetime.now(timezone.utc)
    model, ensure_meta = _ensure_model(db, job)
    if model is None:
        reason = ensure_meta.get("error", "unknown")
        return {
            "status": "skipped" if reason.startswith("no_") or "insufficient" in reason else "error",
            "reason": reason,
            "rows": 0,
            "artifact_trained": False,
        }

    out_base = {
        "artifact_trained": bool(ensure_meta.get("artifact_trained")),
    }

    if job.kind == "sim":
        if not isinstance(model, ForecasterSim):
            return {
                **out_base,
                "status": "error",
                "reason": "model_type_mismatch_expected_ForecasterSim",
                "rows": 0,
            }
        # Use current wall-clock time as simulation origin so observed_counts
        # reflects vehicles that are connected RIGHT NOW, not historical data.
        now = pd.Timestamp.now(tz="UTC")
        start_time = _floor_to_sim_step(now, job.sim_dt_hours)
        # Query active sessions at actual now, NOT at the floored start_time.
        # Sessions that connected after the floor boundary (e.g. at 13:07 when
        # floor is 13:00) would fail start_time <= floored_at and be missed,
        # causing observed_counts=None and a zero-occupancy forecast.
        observed_counts = _observed_counts_at(db, job.id_pilot, now)
        pred = model.predict(
            steps=job.sim_steps,
            dt_hours=job.sim_dt_hours,
            power_kw=job.sim_power_kw,
            start_time=start_time,
            observed_counts=observed_counts,
        )
        n = _persist_sim(db, job, pred, run_at=run_at, start_time=start_time)
        return {**out_base, "status": "ok", "rows": n}

    if not isinstance(model, (ForecasterReg, ForecasterRegProb)):
        return {
            **out_base,
            "status": "error",
            "reason": "model_type_mismatch_expected_ForecasterReg",
            "rows": 0,
        }

    cutoff = _sessions_end_after_cutoff_for_prediction(model, db, job.id_pilot)
    df = charging_sessions_to_forecast_df(db, job.id_pilot, end_time_after=cutoff)
    used_full_history = False
    if df.empty:
        df = charging_sessions_to_forecast_df(db, job.id_pilot)
        used_full_history = True
        logger.info(
            "No sessions after prediction cutoff %s; using full pilot history (%s rows)",
            cutoff,
            len(df),
        )
    else:
        logger.info(
            "Prediction sessions: end_time > %s (%s rows, ~%s bars from latest session end)",
            cutoff,
            len(df),
            _reg_format_min_bars(model),
        )

    if df.empty:
        return {**out_base, "status": "skipped", "reason": "no_completed_sessions", "rows": 0}

    X, _, _ = model.format(df=df, inference=True)
    if X.empty and not used_full_history:
        # The cutoff window is too short for the lag features (e.g. a single recent
        # session only spans a few bins). Retry with the full session history.
        df_full = charging_sessions_to_forecast_df(db, job.id_pilot)
        if len(df_full) > len(df):
            logger.info(
                "Cutoff window insufficient for feature matrix (%s rows); "
                "retrying with full history (%s rows)",
                len(df),
                len(df_full),
            )
            df = df_full
            X, _, _ = model.format(df=df, inference=True)
    if X.empty:
        return {**out_base, "status": "skipped", "reason": "format_yielded_no_rows", "rows": 0}

    X_last = X.iloc[[-1]]

    # Anchor the reg forecast to now so that predicted timestamps start from the
    # current moment rather than from the last historical bin (which may be stale).
    now_reg = pd.Timestamp.now(tz="UTC")
    freq_td = pd.Timedelta(model.freq)
    observed_counts = _observed_counts_at(db, job.id_pilot, now_reg)
    current_occupancy: int = sum(observed_counts.values()) if observed_counts else 0
    logger.info(
        "Reg forecast pilot=%s: current_occupancy=%d (per-charger: %s)",
        job.id_pilot, current_occupancy, observed_counts,
    )
    forecast_anchor: pd.Timestamp = now_reg

    if job.kind == "reg_prob":
        if not isinstance(model, ForecasterRegProb):
            return {
                **out_base,
                "status": "error",
                "reason": "model_type_mismatch_expected_ForecasterRegProb",
                "rows": 0,
            }
        pred = model.predict_pdf(X_last)
        pred = _apply_reg_post_processing(pred, now_reg, freq_td, current_occupancy)
        n = _persist_reg_like(
            db,
            job,
            pred,
            use_pdf=True,
            run_at=run_at,
            forecast_anchor=forecast_anchor,
        )
        return {**out_base, "status": "ok", "rows": n}

    pred = model.predict(X_last)
    pred = _apply_reg_post_processing(pred, now_reg, freq_td, current_occupancy)
    n = _persist_reg_like(
        db,
        job,
        pred,
        use_pdf=False,
        run_at=run_at,
        forecast_anchor=forecast_anchor,
    )
    return {**out_base, "status": "ok", "rows": n}
