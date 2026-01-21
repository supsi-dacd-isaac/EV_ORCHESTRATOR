from typing import List
from datetime import datetime


def forecast_load() -> List[float]:
    """
    Returns the predicted community load in kW.
    This will call an external API.
    """

    base_load_kw = 120
    return [base_load_kw for _ in range(24)]