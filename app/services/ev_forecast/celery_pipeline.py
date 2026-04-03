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
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Chargers, ChargingSessions, EvPilotForecastTimeseries
from app.services.ev_forecast.forecast_manifest import ForecastJob, forecast_artifacts_dir

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


def _to_utc_naive(value: pd.Timestamp | datetime) -> datetime:
    """
    Columns use ``timestamp without time zone``; store UTC wall time as naive datetime.
    Passing tz-aware values otherwise lets the driver shift to the OS timezone (e.g. +2h in CEST).
    """
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    dt = ts.to_pydatetime()
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


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


def _sessions_end_after_cutoff_for_prediction(
    model: ForecasterReg | ForecasterRegProb,
    db: Session,
    pilot_id: UUID,
) -> datetime:
    """
    Naive UTC: keep sessions with ``end_time > cutoff`` for prediction.

    Anchor the window on **latest session end in the DB**, not wall-clock ``now``, so stale /
    historical-only datasets (e.g. dev seed ending in 2024) still get a tight window instead of
    always falling back to full history.
    """
    n_bars = _reg_format_min_bars(model)
    delta = n_bars * pd.Timedelta(model.freq)
    ref_end = _pilot_max_session_end(db, pilot_id)
    if ref_end is None:
        anchor = pd.Timestamp.now(tz="UTC")
    else:
        anchor = pd.Timestamp(ref_end)
        if anchor.tzinfo is None:
            anchor = anchor.tz_localize("UTC")
        else:
            anchor = anchor.tz_convert("UTC")
    start = anchor - delta
    return start.to_pydatetime().replace(tzinfo=None)


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
    anchor_dt = _to_utc_naive(forecast_anchor)
    to_add: list[EvPilotForecastTimeseries] = []
    for _, row in pred.iterrows():
        ft = row["time"]
        if not isinstance(ft, pd.Timestamp):
            ft = pd.Timestamp(ft)
        ft_dt = _to_utc_naive(ft)
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
                origin_time=_to_utc_naive(start_time),
                forecast_time=_to_utc_naive(ft),
                presence=float(row["connected_evs"]),
                energy_kwh=float(row["energy_delivered_kwh"]),
                artifact_name=artifact,
                quantiles_json=None,
            )
        )
    db.add_all(to_add)
    db.commit()
    return len(to_add)


def run_forecast_job(db: Session, job: ForecastJob) -> dict:
    from app.services.ev_forecast.ev_total_forecaster import (  # noqa: WPS433
        ForecasterReg,
        ForecasterRegProb,
        ForecasterSim,
    )

    run_at = datetime.now(timezone.utc).replace(tzinfo=None)
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
        ref_end = _pilot_max_session_end(db, job.id_pilot)
        if ref_end is None:
            start_time = pd.Timestamp.now(tz="UTC")
        else:
            start_time = pd.Timestamp(ref_end)
            if start_time.tzinfo is None:
                start_time = start_time.tz_localize("UTC")
            else:
                start_time = start_time.tz_convert("UTC")
        start_time = _floor_to_sim_step(start_time, job.sim_dt_hours)
        pred = model.predict(
            steps=job.sim_steps,
            dt_hours=job.sim_dt_hours,
            power_kw=job.sim_power_kw,
            start_time=start_time,
            observed_counts=None,
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
    if df.empty:
        df = charging_sessions_to_forecast_df(db, job.id_pilot)
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
    if X.empty:
        return {**out_base, "status": "skipped", "reason": "format_yielded_no_rows", "rows": 0}

    X_last = X.iloc[[-1]]
    forecast_anchor = X_last.index[-1]
    if not isinstance(forecast_anchor, pd.Timestamp):
        forecast_anchor = pd.Timestamp(forecast_anchor)

    if job.kind == "reg_prob":
        if not isinstance(model, ForecasterRegProb):
            return {
                **out_base,
                "status": "error",
                "reason": "model_type_mismatch_expected_ForecasterRegProb",
                "rows": 0,
            }
        pred = model.predict_pdf(X_last)
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
    n = _persist_reg_like(
        db,
        job,
        pred,
        use_pdf=False,
        run_at=run_at,
        forecast_anchor=forecast_anchor,
    )
    return {**out_base, "status": "ok", "rows": n}
