from __future__ import annotations

import os
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
    env = os.getenv("FORECASTERS_ARTIFACTS_DIR")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent / "artifacts"


def manifest_path() -> Path:
    override = os.getenv("FORECAST_JOBS_MANIFEST")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent / "ev_total_forecast_jobs.yaml"


def load_forecast_jobs() -> list[ForecastJob]:
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


def get_job_by_id(job_id: str) -> ForecastJob | None:
    for j in load_forecast_jobs():
        if j.id == job_id:
            return j
    return None
