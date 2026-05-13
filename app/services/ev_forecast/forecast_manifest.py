from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import UUID

import yaml

ForecastKind = Literal["reg", "reg_prob", "sim"]


@dataclass(frozen=True)
class ForecastJob:
    id: str
    id_pilot: UUID
    enabled: bool
    predict_periodicity_minutes: int
    artifact_path: str
    kind: ForecastKind
    sim_steps: int = 24
    sim_dt_hours: float = 1.0
    sim_power_kw: float = 11.0
    # Used when artifact is missing: train ForecasterReg / ForecasterRegProb / build ForecasterSim
    freq: str = "1h"
    timezone: str = "UTC"
    cal_fraction: float = 0.2
    # None => ForecasterReg uses horizon = int(24h / freq) (e.g. 96 for 15min)
    horizon: Optional[int] = None
    lags: Optional[list[int]] = None


def forecast_artifacts_dir() -> Path:
    """Directory for pickles (model.pkl, etc.). Default: ``ev_forecast/artifacts``."""
    from app.config import FORECASTERS_ARTIFACTS_DIR

    if FORECASTERS_ARTIFACTS_DIR:
        return Path(FORECASTERS_ARTIFACTS_DIR).resolve()
    return Path(__file__).resolve().parent / "artifacts"


def manifest_path() -> Path:
    from app.config import FORECAST_JOBS_MANIFEST

    if FORECAST_JOBS_MANIFEST:
        return Path(FORECAST_JOBS_MANIFEST).resolve()
    return Path(__file__).resolve().parent / "ev_total_forecast_jobs.yaml"


def _jobs_from_db() -> list[ForecastJob]:
    """Load forecast jobs from the database. Returns empty list on any error."""
    try:
        from app.db.session import SessionLocal
        from app.models import ForecastJobDB

        db = SessionLocal()
        try:
            rows = db.query(ForecastJobDB).all()
            jobs: list[ForecastJob] = []
            for row in rows:
                kind = str(row.kind).lower()
                if kind not in ("reg", "reg_prob", "sim"):
                    kind = "reg"
                jobs.append(
                    ForecastJob(
                        id=row.job_id,
                        id_pilot=row.id_pilot,
                        enabled=row.enabled,
                        predict_periodicity_minutes=row.predict_periodicity_minutes,
                        artifact_path=row.artifact_path,
                        kind=kind,  # type: ignore[arg-type]
                        sim_steps=row.sim_steps,
                        sim_dt_hours=row.sim_dt_hours,
                        sim_power_kw=row.sim_power_kw,
                        freq=row.freq,
                        timezone=row.timezone,
                        cal_fraction=row.cal_fraction,
                        horizon=row.horizon,
                        lags=([int(x) for x in row.lags] if isinstance(row.lags, list) else None),
                    )
                )
            return jobs
        finally:
            db.close()
    except Exception:
        return []


def _jobs_from_yaml() -> list[ForecastJob]:
    """Load forecast jobs from the YAML manifest file (local dev fallback)."""
    path = manifest_path()
    if not path.is_file():
        return []

    with open(path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    jobs: list[ForecastJob] = []
    for item in raw.get("jobs") or []:
        kind = str(item.get("kind", "reg")).lower()
        if kind not in ("reg", "reg_prob", "sim"):
            kind = "reg"
        jobs.append(
            ForecastJob(
                id=str(item["id"]),
                id_pilot=UUID(str(item["id_pilot"])),
                enabled=bool(item.get("enabled", False)),
                predict_periodicity_minutes=int(item.get("predict_periodicity_minutes", 60)),
                artifact_path=str(item["artifact_path"]),
                kind=kind,  # type: ignore[arg-type]
                sim_steps=int(item.get("sim_steps", 24)),
                sim_dt_hours=float(item.get("sim_dt_hours", 1.0)),
                sim_power_kw=float(item.get("sim_power_kw", 11.0)),
                freq=str(item.get("freq", "1h")),
                timezone=str(item.get("timezone", "UTC")),
                cal_fraction=float(item.get("cal_fraction", 0.2)),
                horizon=(int(item["horizon"]) if item.get("horizon") is not None else None),
                lags=(
                    [int(x) for x in item["lags"]]
                    if isinstance(item.get("lags"), list)
                    else None
                ),
            )
        )
    return jobs


def load_forecast_jobs() -> list[ForecastJob]:
    """Load forecast jobs from the database."""
    return _jobs_from_db()


def get_job_by_id(job_id: str) -> ForecastJob | None:
    for j in load_forecast_jobs():
        if j.id == job_id:
            return j
    return None
