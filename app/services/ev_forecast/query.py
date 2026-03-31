"""Compatibility entrypoint for per-session EV stats forecast (DB-backed)."""

from app.services.ev_forecast.ev_single_forecaster import get_ev_forecast

__all__ = ["get_ev_forecast"]
