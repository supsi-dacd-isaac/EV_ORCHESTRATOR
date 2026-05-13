import logging
import warnings

import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, List
import uuid
from datetime import datetime
from tqdm import tqdm
import lightgbm as lgb

from app.services.ev_forecast.ev_utilities import (
    station_capacities_by_sweepline,
    build_station_connection_dists,
    generate_ev_timeseries,
)

logger = logging.getLogger(__name__)



class ForecasterSim:
    """
    EV charging presence & energy simulator driven by per-hour empirical distributions.

    - State: self.state[station][ev_id] = {"remaining_time": float, "remaining_energy": float}
    - Arrivals are sampled per-station per-hour; excess arrivals are rejected if capacity is full.
    - Duration and energy are sampled as correlated pairs from individual session records.
    - simulate(): constant power charging; disconnect only when BOTH energy <= 0 and time <= 0.
    - update(): reconcile to observed counts by smart removals or sampled additions.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        rng: Optional[np.random.Generator] = None,
    ) -> None:
        self.station_capacities = station_capacities_by_sweepline(df, freq='5min')
        self.connection_dists = build_station_connection_dists(df)
        self.session_dists = self._build_session_dists(df)

        self.rng = rng if rng is not None else np.random.default_rng()
        self.reset()

    @staticmethod
    def _build_session_dists(
        df: pd.DataFrame,
        tz: str = "Europe/Zurich",
    ) -> Dict[str, Dict[int, np.ndarray]]:
        """Build per-station, per-hour arrays of (duration_h, energy_kWh) pairs
        from individual session records, preserving the natural correlation."""
        _df = df.copy()
        _df['start_time'] = pd.to_datetime(_df['start_time'], utc=True, errors='coerce')
        _df = _df.dropna(subset=['start_time', 'duration_connection', 'energy (kWh)'])
        _df = _df[(_df['duration_connection'] > 0) & (_df['energy (kWh)'] > 0)]
        _df['hour'] = _df['start_time'].dt.tz_convert(tz).dt.hour

        out: Dict[str, Dict[int, np.ndarray]] = {}
        for sid, grp in _df.groupby('station_id'):
            sid = str(sid)
            out[sid] = {}
            for hour, hgrp in grp.groupby('hour'):
                pairs = hgrp[['duration_connection', 'energy (kWh)']].to_numpy()
                out[sid][int(hour)] = pairs
        return out

    def _sample_session_pair(
        self, station: str, hour: int, size: int = 1,
    ) -> np.ndarray:
        """Sample (duration_h, energy_kWh) pairs for a station/hour.
        Falls back to all-hours for that station, then to global pool."""
        pairs = self._get_session_pool(station, hour)
        idx = self.rng.integers(0, len(pairs), size=size)
        return pairs[idx]

    def _get_session_pool(self, station: str, hour: int) -> np.ndarray:
        """Retrieve the best available pool of (T, E) pairs, with fallbacks."""
        if station in self.session_dists:
            if hour in self.session_dists[station]:
                return self.session_dists[station][hour]
            all_pairs = [v for v in self.session_dists[station].values() if len(v) > 0]
            if all_pairs:
                return np.concatenate(all_pairs)

        global_pairs = []
        for st_dict in self.session_dists.values():
            for v in st_dict.values():
                if len(v) > 0:
                    global_pairs.append(v)
        if global_pairs:
            return np.concatenate(global_pairs)

        raise ValueError(
            f"No session data available for station={station}, hour={hour} "
            f"(and no global fallback)."
        )

    def _get_df_for_station(self, source, station: str | None):
        if isinstance(source, dict):
            if station in source and source[station] is not None:
                return source[station]
            if "global" in source:
                return source["global"]
            raise KeyError(f"No distribution for station={station}")
        return source

    def _hour_values(self, source, hour: int, station: str | None) -> np.ndarray:
        df = self._get_df_for_station(source, station)
        if hour in df.columns:
            vals = df[hour].to_numpy()
            return vals[~pd.isna(vals)]
        if hour in df.index:
            s = df.loc[hour]
            vals = s.to_numpy() if isinstance(s, pd.Series) else s.to_numpy().ravel()
            return vals[~pd.isna(vals)]
        if isinstance(df.index, pd.MultiIndex) and "hour" in df.index.names:
            s = df.xs(hour, level="hour", drop_level=False)
            vals = s.to_numpy().ravel()
            return vals[~pd.isna(vals)]
        if "hour" in df.columns:
            vals = df[df["hour"] == hour].values.ravel()
            return vals[~pd.isna(vals)]
        raise KeyError(f"Could not extract values for hour={hour} (station={station}).")

    def _sample_from_dist(self, source, hour: int, station: str | None, size: int = 1) -> np.ndarray:
        vals = self._hour_values(source, hour, station)
        if vals.size == 0:
            raise ValueError(f"No empirical values for hour={hour} (station={station}).")
        idx = self.rng.integers(0, vals.size, size=size)
        return vals[idx].astype(float)

    def _gen_ev_id(self) -> str:
        return f"ev-{uuid.uuid4().hex[:12]}"

    def _available_slots(self, station: str) -> int:
        return max(self.station_capacities[station] - len(self.state[station]), 0)

    # ---------- public API ----------
    def reset(self) -> None:
        self.state: Dict[str, Dict[str, Dict[str, float]]] = {
            s: {} for s in self.station_capacities
        }

    def sample_new_evs(
        self,
        time_index: pd.Timestamp,
        dt_hours: float = 1.0,
        new_evs: dict[str, int] | None = None,
        p_max_kw: float = 11.0,
        max_resamples: int = 20,
        respect_capacity: bool = True,
    ) -> dict[str, list[str]]:
        hour = int(time_index.hour)
        admitted: dict[str, list[str]] = {s: [] for s in self.station_capacities}

        if new_evs is None:
            arrivals: dict[str, int] = {}
            for s in self.station_capacities:
                raw = float(self._sample_from_dist(self.connection_dists, hour, s, 1)[0])
                n = int(max(round(raw * dt_hours), 0))
                arrivals[s] = n
        else:
            arrivals = {
                s: int(max(v, 0))
                for s, v in new_evs.items()
                if s in self.station_capacities
            }

        for s, n in arrivals.items():
            if n <= 0:
                continue
            if respect_capacity:
                admit_target = min(n, self._available_slots(s))
            else:
                admit_target = n
            if admit_target <= 0:
                continue

            k = 0
            resample_failures = 0
            while k < admit_target:
                ok = False
                attempts = 0
                while attempts < max_resamples and not ok:
                    pair = self._sample_session_pair(s, hour, size=1)[0]
                    T, E = float(pair[0]), float(pair[1])
                    ok = (E > 0) and (T > 0) and (E <= p_max_kw * T)
                    attempts += (not ok)
                if not ok:
                    resample_failures += 1
                    break
                ev_id = self._gen_ev_id()
                self.state[s][ev_id] = {
                    "remaining_time": T,
                    "remaining_energy": E,
                }
                admitted[s].append(ev_id)
                k += 1

            if resample_failures > 0:
                logger.warning(
                    "station %s, hour %d: resample limit (%d) exhausted for "
                    "%d of %d EVs — check p_max_kw (%.1f) vs data",
                    s, hour, max_resamples, admit_target - k, admit_target, p_max_kw,
                )

        return admitted

    def simulate(
        self,
        time_index: pd.Timestamp,
        dt_hours: float = 1.0,
        power_kw: float = 11.0,
    ) -> Dict[str, List[str]]:
        """Advance by *dt_hours* with constant power per EV.
        Disconnect EVs only when (remaining_energy <= 0) AND (remaining_time <= 0).
        Returns {station: [disconnected_ev_ids]}.
        """
        disconnected: Dict[str, List[str]] = {s: [] for s in self.station_capacities}
        dE = power_kw * dt_hours

        for s, evs in self.state.items():
            to_remove = []
            for ev_id, info in evs.items():
                if info["remaining_energy"] > 0:
                    info["remaining_energy"] = float(max(info["remaining_energy"] - dE, 0.0))
                info["remaining_time"] = float(info["remaining_time"] - dt_hours)
                if info["remaining_energy"] <= 0.0 and info["remaining_time"] <= 0.0:
                    to_remove.append(ev_id)
            for ev_id in to_remove:
                disconnected[s].append(ev_id)
                del evs[ev_id]
        return disconnected

    def update(
        self,
        time_index: pd.Timestamp,
        observed_stations_evs: Dict[str, int],
    ) -> Dict[str, Any]:
        """Reconcile to observed counts.

        Removal prefers EVs closest to departure (smallest remaining_time).
        Addition bypasses capacity limits since observed counts are ground truth.
        """
        removed: Dict[str, list] = {s: [] for s in self.station_capacities}
        added: Dict[str, list] = {s: [] for s in self.station_capacities}

        for s, obs in observed_stations_evs.items():
            if s not in self.state:
                # Station not seen during training (no completed sessions for this
                # charger yet). Initialize it so its connected EVs are tracked.
                self.state[s] = {}
            cur_ids = list(self.state[s].keys())
            diff = len(cur_ids) - int(obs)
            if diff > 0:
                sorted_ids = sorted(
                    cur_ids,
                    key=lambda eid: self.state[s][eid]["remaining_time"],
                )
                to_remove = sorted_ids[:diff]
                for ev_id in to_remove:
                    del self.state[s][ev_id]
                removed[s].extend(to_remove)

        need: dict[str, int] = {}
        for s, obs in observed_stations_evs.items():
            cur = len(self.state[s])  # state[s] is guaranteed to exist after loop above
            if obs > cur:
                need[s] = int(obs - cur)

        # Split: known stations go through sample_new_evs (uses connection_dists);
        # unknown stations (no training data) get EVs injected directly from the
        # global session pool so capacity/distribution limits don't silently block them.
        known_need = {s: n for s, n in need.items() if s in self.station_capacities}
        unknown_need = {s: n for s, n in need.items() if s not in self.station_capacities}

        if known_need:
            admitted = self.sample_new_evs(
                time_index=time_index,
                new_evs=known_need,
                respect_capacity=False,
            )
            for s, ids in admitted.items():
                added[s].extend(ids)

        # Force-inject any EVs that sample_new_evs failed to admit (resample limit exhausted).
        # Ground-truth observed counts must be honoured regardless of distribution validity.
        # Use a fixed physics-safe pair (T=8h, E=30kWh) so the EV survives multiple
        # simulation steps without violating the charging-power constraint.
        for s, n in known_need.items():
            shortfall = n - len(added.get(s, []))
            if shortfall > 0:
                for _ in range(shortfall):
                    ev_id = self._gen_ev_id()
                    self.state[s][ev_id] = {"remaining_time": 8.0, "remaining_energy": 30.0}
                    added.setdefault(s, []).append(ev_id)

        if unknown_need:
            hour = int(time_index.hour)
            for s, n in unknown_need.items():
                for _ in range(n):
                    try:
                        pair = self._sample_session_pair(s, hour, size=1)[0]
                        T, E = float(pair[0]), float(pair[1])
                    except Exception:
                        T, E = 2.0, 10.0  # conservative fallback
                    ev_id = self._gen_ev_id()
                    self.state[s][ev_id] = {
                        "remaining_time": T,
                        "remaining_energy": E,
                    }
                    added.setdefault(s, []).append(ev_id)

        final_counts = {s: len(self.state[s]) for s in self.state}
        return {"removed": removed, "added": added, "final_counts": final_counts}

    def predict(
        self,
        steps: int = 500,
        dt_hours: float = 1.0,
        power_kw: float = 11.0,
        start_time: pd.Timestamp = pd.Timestamp(datetime(2025, 1, 1)),
        observed_counts: Optional[Dict[str, int]] = None,
    ) -> pd.DataFrame:
        """Run a forward simulation and return presence / energy time series.

        Parameters
        ----------
        observed_counts : dict mapping station_id (str) → number of EVs
            currently connected.  If provided the internal state is
            initialised to match these counts (via ``update``) before the
            simulation starts, so the first predicted step already reflects
            the real occupancy at *start_time*.
        """
        saved_state = self.snapshot()
        self.reset()

        if observed_counts is not None:
            self.update(time_index=start_time,
                        observed_stations_evs=observed_counts)

        presence = []
        energy_delivered = []
        times = []

        for k in range(steps):
            t = start_time + pd.Timedelta(hours=k * dt_hours)
            self.sample_new_evs(time_index=t, dt_hours=dt_hours)

            self.simulate(time_index=t, dt_hours=dt_hours, power_kw=power_kw)

            n_connected = sum(len(v) for v in self.state.values())
            deliverable = 0.0
            for s, evs in self.state.items():
                for ev_id, info in evs.items():
                    if info["remaining_energy"] > 0:
                        deliverable += min(info["remaining_energy"], power_kw * dt_hours)

            presence.append(n_connected)
            energy_delivered.append(deliverable)
            times.append(t)

        results = pd.DataFrame(
            {
                "connected_evs": presence,
                "energy_delivered_kwh": energy_delivered,
                "cumulative_energy_kwh": np.cumsum(energy_delivered),
            },
            index=pd.DatetimeIndex(times, name="time"),
        )

        self.state = {
            s: {e: dict(info) for e, info in evs.items()}
            for s, evs in saved_state.items()
        }
        return results

    # convenience
    def counts(self) -> Dict[str, int]:
        return {s: len(self.state[s]) for s in self.station_capacities}

    def snapshot(self) -> Dict[str, Dict[str, Dict[str, float]]]:
        return {
            s: {e: dict(info) for e, info in evs.items()}
            for s, evs in self.state.items()
        }


class ForecasterReg:
    """
    Direct multi-step LightGBM forecaster for EV presence and energy consumption.

    Uses ``generate_ev_timeseries`` to convert raw session data into regular
    time series, then builds lagged features and trains one LightGBM regressor
    per forecast step (direct strategy).

    Parameters
    ----------
    df : pd.DataFrame
        Raw charging-session DataFrame (must contain start_time,
        end_time_connection, energy (kWh), etc.).
    freq : str
        Resolution of the time series and forecasts (e.g. '1h', '15min').
    horizon : int or None
        Number of future steps to forecast.  If *None* it is computed as the
        number of steps that fit in 24 hours at the given ``freq``
        (e.g. 24 for '1h', 96 for '15min').
    n_lags : int or None
        Number of past steps used as lag features.  Defaults to ``horizon``.
    timezone : str
        Timezone forwarded to ``generate_ev_timeseries``.
    lgb_params : dict or None
        Extra keyword arguments forwarded to every ``lgb.LGBMRegressor``.
    """

    # --------------------------------------------------------------------- #
    #  init
    # --------------------------------------------------------------------- #
    def __init__(
        self,
        df: pd.DataFrame,
        freq: str = "15min",
        horizon: Optional[int] = None,
        lags: Optional[list] = None,
        timezone: str = "UTC",
        lgb_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.df = df
        self.freq = freq
        self.timezone = timezone

        steps_per_day = int(pd.Timedelta("24h") / pd.Timedelta(freq))
        self.horizon = horizon if horizon is not None else steps_per_day
        self.lags = lags if lags is not None else self.horizon
        self.lgb_params = lgb_params or {}

        self.models_presence: List[lgb.LGBMRegressor] = []
        self.models_energy: List[lgb.LGBMRegressor] = []
        self.feature_cols: List[str] = []
        self._is_trained: bool = False

    # --------------------------------------------------------------------- #
    #  format – feature engineering
    # --------------------------------------------------------------------- #
    def format(
        self,
        df: Optional[pd.DataFrame] = None,
        *,
        inference: bool = False,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Build feature / target matrices from raw session data.

        1. Convert sessions → regular time series via ``generate_ev_timeseries``.
        2. Create lag features for the last ``n_lags`` steps (presence + energy)
           plus calendar features (hour, day-of-week, month, weekend flag).
        3. When ``inference`` is False (training), create target columns for the next
           ``horizon`` steps and keep rows where features and all targets are finite.
           When ``inference`` is True, skip targets and keep rows where features alone
           are finite — so the latest time step (no future observations yet) remains.

        Returns
        -------
        X : pd.DataFrame            – feature matrix (rows = valid time steps)
        Y_presence : pd.DataFrame   – target matrix for presence (empty when ``inference``)
        Y_energy : pd.DataFrame     – target matrix for energy consumed (empty when ``inference``)
        """
        source = df if df is not None else self.df

        # 1. Session data → time series
        ts = generate_ev_timeseries(source, freq=self.freq, timezone=self.timezone)
        presence = ts["presence"].astype(float)
        energy = ts["energy_consumed"].astype(float)

        # 2. Feature matrix — build all columns up-front, then concat once
        steps_per_hour = max(int(pd.Timedelta("1h") / pd.Timedelta(self.freq)), 1)

        # Fractional hour-of-day (e.g. 10.25 for 10:15)
        hour_of_day = ts.index.hour + ts.index.minute / 60.0
        # Cyclical encoding of time-of-day and day-of-week
        hour_rad = 2 * np.pi * hour_of_day / 24.0
        dow_rad = 2 * np.pi * ts.index.dayofweek / 7.0

        calendar = {
            "hour": hour_of_day,
            "hour_sin": np.sin(hour_rad),
            "hour_cos": np.cos(hour_rad),
            "dayofweek": ts.index.dayofweek,
            "dow_sin": np.sin(dow_rad),
            "dow_cos": np.cos(dow_rad),
            "month": ts.index.month,
            "is_weekend": (ts.index.dayofweek >= 5).astype(int),
        }

        # Lag features: merge user-specified strategic lags with a dense recent window
        if isinstance(self.lags, list):
            strategic = set(self.lags)
        else:
            strategic = set(range(1, self.lags + 1))
        recent_window = set(range(1, min(steps_per_hour * 4, 17) + 1))
        all_lags = sorted(strategic | recent_window)

        lags = {}
        for lag in all_lags:
            lags[f"presence_lag_{lag}"] = presence.shift(lag)
            lags[f"energy_lag_{lag}"] = energy.shift(lag)

        # Rolling statistics over recent windows
        for win in [steps_per_hour, steps_per_hour * 4, steps_per_hour * 24]:
            if win < 2:
                continue
            lags[f"presence_rmean_{win}"] = presence.shift(1).rolling(win).mean()
            lags[f"presence_rstd_{win}"] = presence.shift(1).rolling(win).std()
            lags[f"energy_rmean_{win}"] = energy.shift(1).rolling(win).mean()
            lags[f"energy_rstd_{win}"] = energy.shift(1).rolling(win).std()

        feats = pd.concat(
            [pd.DataFrame(calendar, index=ts.index),
             pd.DataFrame(lags, index=ts.index)],
            axis=1,
        )

        # 3. Target matrices (training only — future shifts drop the tail at inference)
        if inference:
            valid = feats.notna().all(axis=1)
            X = feats.loc[valid]
            Y_presence = pd.DataFrame()
            Y_energy = pd.DataFrame()
        else:
            targets_presence = pd.DataFrame(
                {f"t+{h}": presence.shift(-h) for h in range(1, self.horizon + 1)},
                index=ts.index,
            )
            targets_energy = pd.DataFrame(
                {f"t+{h}": energy.shift(-h) for h in range(1, self.horizon + 1)},
                index=ts.index,
            )
            valid = (
                feats.notna().all(axis=1)
                & targets_presence.notna().all(axis=1)
                & targets_energy.notna().all(axis=1)
            )
            X = feats.loc[valid]
            Y_presence = targets_presence.loc[valid]
            Y_energy = targets_energy.loc[valid]

        self.feature_cols = list(X.columns)
        return X, Y_presence, Y_energy

    # --------------------------------------------------------------------- #
    #  train
    # --------------------------------------------------------------------- #
    def train(
        self,
        df: Optional[pd.DataFrame] = None,
        lgb_params: Optional[Dict[str, Any]] = None,
        verbose: int = -1,
    ) -> "ForecasterReg":
        """Train ``horizon`` LightGBM models for each target (presence, energy).

        Parameters
        ----------
        df : pd.DataFrame or None
            Raw session DataFrame to build features from.  If *None* the
            DataFrame passed at init (``self.df``) is used.  Pass a training
            subset here to avoid data leakage during evaluation.
        lgb_params : dict or None
            Override / extend the default ``lgb_params`` passed at init.
        verbose : int
            LightGBM verbosity (``-1`` silences training output).

        Returns
        -------
        self
        """
        params = {**self.lgb_params, **(lgb_params or {})}
        params.setdefault("verbosity", verbose)

        X, Y_presence, Y_energy = self.format(df=df)

        self.models_presence = []
        self.models_energy = []

        for h in tqdm(range(self.horizon)):
            model_p = lgb.LGBMRegressor(**params)
            model_p.fit(X, Y_presence.iloc[:, h])
            self.models_presence.append(model_p)

            model_e = lgb.LGBMRegressor(**params)
            model_e.fit(X, Y_energy.iloc[:, h])
            self.models_energy.append(model_e)

        self._is_trained = True
        return self

    # --------------------------------------------------------------------- #
    #  predict
    # --------------------------------------------------------------------- #
    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        """Predict ``horizon`` steps ahead for every row in *X*.

        Parameters
        ----------
        X : pd.DataFrame
            Feature matrix with the same columns produced by ``format()``.
            Typically the last available row(s) from the formatted dataset, or
            hand-crafted feature vectors for an arbitrary forecast origin.

        Returns
        -------
        pd.DataFrame
            Long-format DataFrame with columns ``[origin, time, presence, energy_consumed]``
            where *origin* is the index value of the input row and *time* is the
            forecasted timestamp.
        """
        if not self._is_trained:
            raise RuntimeError("Call train() before predict().")

        if isinstance(X, pd.Series):
            X = X.to_frame().T

        X_feat = X[self.feature_cols]
        freq_td = pd.Timedelta(self.freq)

        records: List[Dict[str, Any]] = []
        for i, origin in enumerate(X.index):
            for h in range(self.horizon):
                records.append({
                    "origin": origin,
                    "time": origin + (h + 1) * freq_td,
                    "presence": self.models_presence[h].predict(
                        X_feat.iloc[[i]]
                    )[0],
                    "energy_consumed": self.models_energy[h].predict(
                        X_feat.iloc[[i]]
                    )[0],
                })

        return pd.DataFrame.from_records(records)


class ForecasterRegProb(ForecasterReg):
    """
    Probabilistic extension of :class:`ForecasterReg` using *conformal prediction*.

    During training the formatted dataset is split temporally into a
    **training** set and a **calibration** set.  The LightGBM models are fitted
    on the training portion; residuals are then collected on the calibration
    portion and grouped by **(hour-of-prediction, step-ahead)**.  For every
    group the empirical quantiles of the signed errors are stored.

    At prediction time, ``predict_pdf`` adds those error quantiles to the
    point forecast to produce calibrated prediction quantiles.

    Parameters
    ----------
    df, freq, horizon, n_lags, timezone, lgb_params :
        Forwarded to :class:`ForecasterReg`.
    cal_fraction : float
        Fraction of rows (tail of the time-ordered data) reserved for
        calibration.  Default ``0.2``.
    quantiles : list[float] or None
        Probability levels for the error quantiles.
        Default ``[0.01, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.99]``.
    """

    DEFAULT_QUANTILES: List[float] = [
        0.01, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.99,
    ]

    # ------------------------------------------------------------------ #
    #  init
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        df: pd.DataFrame,
        freq: str = "1h",
        horizon: Optional[int] = None,
        lags: Optional[list] = None,
        timezone: str = "UTC",
        lgb_params: Optional[Dict[str, Any]] = None,
        cal_fraction: float = 0.2,
        quantiles: Optional[List[float]] = None,
    ) -> None:
        super().__init__(
            df,
            freq=freq,
            horizon=horizon,
            lags=lags,
            timezone=timezone,
            lgb_params=lgb_params,
        )
        self.cal_fraction = cal_fraction
        self.quantiles = quantiles if quantiles is not None else list(self.DEFAULT_QUANTILES)

        # Populated by train → _calibrate
        self.error_quantiles_presence: Optional[pd.DataFrame] = None
        self.error_quantiles_energy: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------ #
    #  train  (override)
    # ------------------------------------------------------------------ #
    def train(
        self,
        df: Optional[pd.DataFrame] = None,
        lgb_params: Optional[Dict[str, Any]] = None,
        verbose: int = -1,
    ) -> "ForecasterRegProb":
        """Train on the training split, then calibrate on the held-out tail."""
        params = {**self.lgb_params, **(lgb_params or {})}
        params.setdefault("verbosity", verbose)

        X, Y_presence, Y_energy = self.format(df=df)

        # Temporal split ---------------------------------------------------
        n = len(X)
        n_train = int(n * (1 - self.cal_fraction))

        X_train, X_cal = X.iloc[:n_train], X.iloc[n_train:]
        Y_pres_train, Y_pres_cal = Y_presence.iloc[:n_train], Y_presence.iloc[n_train:]
        Y_ener_train, Y_ener_cal = Y_energy.iloc[:n_train], Y_energy.iloc[n_train:]

        # Fit one model per step -------------------------------------------
        self.models_presence = []
        self.models_energy = []

        for h in tqdm(range(self.horizon)):
            model_p = lgb.LGBMRegressor(**params)
            model_p.fit(X_train, Y_pres_train.iloc[:, h])
            self.models_presence.append(model_p)

            model_e = lgb.LGBMRegressor(**params)
            model_e.fit(X_train, Y_ener_train.iloc[:, h])
            self.models_energy.append(model_e)

        self._is_trained = True

        # Calibrate --------------------------------------------------------
        self._calibrate(X_cal, Y_pres_cal, Y_ener_cal)
        return self

    # ------------------------------------------------------------------ #
    #  _calibrate
    # ------------------------------------------------------------------ #
    def _calibrate(
        self,
        X_cal: pd.DataFrame,
        Y_presence_cal: pd.DataFrame,
        Y_energy_cal: pd.DataFrame,
    ) -> None:
        """Collect signed residuals on the calibration set and compute
        quantiles grouped by (hour-of-prediction, step-ahead)."""

        hours = X_cal["hour"].values
        X_feat = X_cal[self.feature_cols]

        rows_p: List[Dict[str, Any]] = []
        rows_e: List[Dict[str, Any]] = []

        for h in range(self.horizon):
            preds_p = self.models_presence[h].predict(X_feat)
            preds_e = self.models_energy[h].predict(X_feat)

            errors_p = Y_presence_cal.iloc[:, h].values - preds_p
            errors_e = Y_energy_cal.iloc[:, h].values - preds_e

            step = h + 1
            for i in range(len(X_cal)):
                rows_p.append({"hour": int(hours[i]), "step": step, "error": errors_p[i]})
                rows_e.append({"hour": int(hours[i]), "step": step, "error": errors_e[i]})

        df_err_p = pd.DataFrame(rows_p)
        df_err_e = pd.DataFrame(rows_e)

        self.error_quantiles_presence = self._grouped_quantiles(df_err_p)
        self.error_quantiles_energy = self._grouped_quantiles(df_err_e)

    def _grouped_quantiles(self, df_err: pd.DataFrame) -> pd.DataFrame:
        """Return a DataFrame indexed by ``(hour, step)`` with one column
        per quantile level, containing the empirical quantile of the
        calibration errors."""

        q_labels = [f"q{q}" for q in self.quantiles]

        def _qrow(grp: pd.DataFrame) -> pd.Series:
            return pd.Series(
                np.quantile(grp["error"].values, self.quantiles),
                index=q_labels,
            )

        return df_err.groupby(["hour", "step"]).apply(_qrow)

    # ------------------------------------------------------------------ #
    #  predict_pdf
    # ------------------------------------------------------------------ #
    def predict_pdf(self, X: pd.DataFrame) -> pd.DataFrame:
        """Produce probabilistic (quantile) forecasts for every row in *X*.

        For each (origin, step-ahead) pair the point prediction from
        ``predict()`` is augmented with calibrated prediction quantiles
        derived from the conformal calibration table.

        Parameters
        ----------
        X : pd.DataFrame
            Feature matrix (same columns as ``format()`` output).

        Returns
        -------
        pd.DataFrame
            Long-format table with columns:
            ``origin, time, step, hour, presence, energy_consumed,
            presence_q<level>, ..., energy_q<level>, ...``
        """
        if not self._is_trained:
            raise RuntimeError("Call train() before predict_pdf().")
        if self.error_quantiles_presence is None:
            raise RuntimeError("Model not calibrated – call train() first.")

        if isinstance(X, pd.Series):
            X = X.to_frame().T

        X_feat = X[self.feature_cols]
        freq_td = pd.Timedelta(self.freq)

        records: List[Dict[str, Any]] = []

        for i, origin in enumerate(X.index):
            hour = int(X.iloc[i]["hour"])
            for h in range(self.horizon):
                step = h + 1
                pred_p = self.models_presence[h].predict(X_feat.iloc[[i]])[0]
                pred_e = self.models_energy[h].predict(X_feat.iloc[[i]])[0]

                row: Dict[str, Any] = {
                    "origin": origin,
                    "time": origin + step * freq_td,
                    "step": step,
                    "hour": hour,
                    "presence": pred_p,
                    "energy_consumed": pred_e,
                }

                # Look up error quantiles for this (hour, step) pair
                eq_p = self._lookup_error_quantiles(
                    self.error_quantiles_presence, hour, step
                )
                eq_e = self._lookup_error_quantiles(
                    self.error_quantiles_energy, hour, step
                )

                for j, q in enumerate(self.quantiles):
                    row[f"presence_q{q}"] = pred_p + eq_p[j]
                    row[f"energy_q{q}"] = pred_e + eq_e[j]

                records.append(row)

        return pd.DataFrame.from_records(records)

    # ------------------------------------------------------------------ #
    #  helpers
    # ------------------------------------------------------------------ #
    def _lookup_error_quantiles(
        self,
        eq_table: pd.DataFrame,
        hour: int,
        step: int,
    ) -> np.ndarray:
        """Retrieve quantile vector for *(hour, step)* with graceful fallback."""
        if (hour, step) in eq_table.index:
            return eq_table.loc[(hour, step)].values

        # Fallback: marginal over all hours for this step
        step_mask = eq_table.index.get_level_values("step") == step
        if step_mask.any():
            return eq_table.loc[step_mask].mean(axis=0).values

        # Last resort: global marginal
        return eq_table.mean(axis=0).values
