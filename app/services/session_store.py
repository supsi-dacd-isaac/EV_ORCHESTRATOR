from typing import Dict
from app.services.session_state import ChargingSessionState

# Key: charger_id
ACTIVE_SESSIONS: Dict[str, ChargingSessionState] = {}