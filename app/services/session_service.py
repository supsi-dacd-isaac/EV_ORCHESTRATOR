from app.db.session import SessionLocal
from app.models.db.charging_session import ChargingSessionDB


def store_completed_session(session_state):
    """
    Persist a completed charging session to PostgreSQL.
    """
    try:
        db = SessionLocal()

        db_session = ChargingSessionDB(
            charger_id=session_state.charger_id,
            start_time=session_state.start_time,
            end_time=session_state.last_update_time,
            end_charging_time=session_state.end_charging_time,
            energy_delivered_kwh=session_state.energy_delivered_kwh,
            forecasted_energy_kwh=session_state.forecasted_energy_kwh,
            forecasted_duration_hours=session_state.forecasted_duration_hours,
        )

        db.add(db_session)
        db.commit()
    except Exception as e:
        print('Failed to store completed session', e)
    finally:
        db.close()

