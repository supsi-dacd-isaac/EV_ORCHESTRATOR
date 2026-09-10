ENERGY_NEED_95Q = 20
DURATION_95Q = 5
CHARGING_RATE_CONSTANT = 22.0
NUMBER_CHARGING_POINTS_CONSTANT = 40.0

# ---------------------------------------------------------------------------
# Base-load forecast
# ---------------------------------------------------------------------------
# Number of 15-minute forecast steps to consume from the external API response
# or to generate for the static fallback. Also the size of the `community_load`
# observation feature (see obs_layout.FEATURES).
BASELOAD_FORECAST_HORIZON_STEPS = 24
# Duration of each forecast step in minutes (15-minute resolution).
FORECAST_STEP_MINUTES = 15

# Default observation length: obs_layout.OBSERVATION_SIZE
# (derived from DEFAULT_OBSERVATION_FEATURES = 20 scalars + community_load).
