from app.services.orchestrator.duration_cdf.query import get_cumulative_duration_probability
import numpy as np

def get_disconnection_prob(station_id, connected_duration):
    """Get disconnection probability"""
    F_to = get_cumulative_duration_probability(station_id, connected_duration)
    F_to_deltat = get_cumulative_duration_probability(station_id, connected_duration + 1)

    remains = ( 1 -F_to)

    if remains <= 1e-8:
        return 1.0

    disconnection_prob = (F_to_deltat - F_to) / remains

    # Numerical clipping to valid range
    return float(np.clip(disconnection_prob, 0.0, 1.0))