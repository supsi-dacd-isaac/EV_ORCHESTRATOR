ENERGY_NEED_95Q = 20
DURATION_95Q = 5
CHARGING_RATE_CONSTANT = 22.0
NUMBER_CHARGING_POINTS_CONSTANT = 40.0

# ---------------------------------------------------------------------------
# Site load forecast horizon
# ---------------------------------------------------------------------------
# Number of 15-minute forecast steps consumed from demand/generation APIs.
# Also the size of `net_demand_forecast` / `demand_forecast` / `generation_forecast`
# observation features (see obs_layout.FEATURES).
BASELOAD_FORECAST_HORIZON_STEPS = 24
# Duration of each forecast step in minutes (15-minute resolution).
FORECAST_STEP_MINUTES = 15

# ---------------------------------------------------------------------------
# Measured grid net power (Influx, Phase 4A)
# ---------------------------------------------------------------------------
# Floor for ANN normalization denom = n_charging_points × rate_kw.
GRID_NET_POWER_NORM_EPS_KW = 1e-6

# Default observation length: obs_layout.OBSERVATION_SIZE
# (derived from DEFAULT_OBSERVATION_FEATURES = 20 scalars + net_demand_forecast).
