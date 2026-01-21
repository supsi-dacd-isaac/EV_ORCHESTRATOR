from datetime import datetime
from typing import Optional

class ChargingSessionState:
    def __init__(
        self,
        charger_id: str,
        start_time: datetime,
        charged_energy_kwh: float,
        avg_measured_power_kw: float,
        is_fully_charged: bool,
        forecasted_energy_kwh: float,
        forecasted_energy_kwh_std: float,
        forecasted_duration_hours: float,
        forecasted_duration_hours_std: float,
        controlled_charging_points: int
    ):

        self.charger_id = charger_id
        self.start_time = start_time
        self.controlled_charging_points = controlled_charging_points

        #Forecasts
        self.forecasted_energy_kwh = forecasted_energy_kwh
        self.forecasted_duration_hours = forecasted_duration_hours
        self.forecasted_energy_kwh_std = forecasted_energy_kwh_std
        self.forecasted_duration_hours_std = forecasted_duration_hours_std

        #Dynamic state
        self.energy_delivered_kwh = charged_energy_kwh
        self.avg_measured_power_kw = avg_measured_power_kw
        self.is_fully_charged = is_fully_charged
        self.last_update_time: Optional[datetime] = None

        self.end_charging_time = None

        
