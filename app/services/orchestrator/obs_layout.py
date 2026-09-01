# Named indices into the observation vector built by observation_builder.prepare_observation().
# Update this file whenever the concatenation order in prepare_observation() changes.

OBS_VEHICLE_CONNECTED               = 0
OBS_FULLY_CHARGED                   = 1
OBS_TIME_CONN_SIN                   = 2
OBS_TIME_CONN_COS                   = 3
OBS_TIME_CURR_SIN                   = 4
OBS_TIME_CURR_COS                   = 5
OBS_MONTH_SIN                       = 6
OBS_MONTH_COS                       = 7
OBS_CONNECTED_TIME_RELATIVE         = 8
OBS_ENERGY_FORECASTED_REL_CAPACITY  = 9
OBS_ENERGY_FORECASTED_LOG           = 10
OBS_ENERGY_FORECASTED_STD_NORM      = 11
OBS_DURATION_FORECASTED_LOG         = 12
OBS_DURATION_FORECASTED_STD_NORM    = 13
OBS_PROBABILITY_DISCONNECTION       = 14
OBS_CUMULATIVE_DURATION_PROBABILITY = 15
OBS_ENERGY_CHARGED_REL_NEEDED       = 16
OBS_CHARGING_RATE_NORM              = 17
OBS_NUM_CHARGING_POINTS_NORM        = 18
OBS_LOAD_LEVEL_RELATIVE             = 19
OBS_COMMUNITY_LOAD_START            = 20  # community_load_kw_normalized vector starts here
