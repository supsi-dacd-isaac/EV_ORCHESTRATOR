from typing import Optional
import datetime
import uuid

from sqlalchemy import Boolean, DateTime, Double, ForeignKeyConstraint, Integer, JSON, PrimaryKeyConstraint, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass


class Owners(Base):
    __tablename__ = 'owners'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='owners_pkey'),
        UniqueConstraint('user', name='owners_user_key')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user: Mapped[str] = mapped_column(String, nullable=False)
    password: Mapped[str] = mapped_column(String, nullable=False)
    company_name: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text('now()'))
    type: Mapped[Optional[str]] = mapped_column(String)
    role: Mapped[Optional[str]] = mapped_column(String)
    token: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)

    pilot: Mapped[list['Pilot']] = relationship('Pilot', back_populates='owners')
    chargers: Mapped[list['Chargers']] = relationship('Chargers', back_populates='owners')


class Pilot(Base):
    __tablename__ = 'pilot'
    __table_args__ = (
        ForeignKeyConstraint(['id_owner'], ['owners.id'], ondelete='SET NULL', onupdate='CASCADE', name='id_owner'),
        PrimaryKeyConstraint('id', name='pilot_pk'),
        UniqueConstraint('name', name='pilot_unique')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    id_owner: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    timezone_name: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'Europe/Zurich'"))
    # How/where to query policy-specific external signals (e.g. wind_excess).
    # Live per-timestep values are stored on actions.decision_context, not here.
    policy_signals: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Named registry of NON-secret external connections (InfluxDB, forecast API,
    # ...). policy_signals entries reference an entry here by name via "source".
    # Credentials for these connections live encrypted in pilot_secret, not here.
    data_sources: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    owners: Mapped['Owners'] = relationship('Owners', back_populates='pilot')
    chargers: Mapped[list['Chargers']] = relationship('Chargers', back_populates='pilot')
    ev_pilot_forecast_timeseries: Mapped[list['EvPilotForecastTimeseries']] = relationship(
        'EvPilotForecastTimeseries', back_populates='pilot', foreign_keys='EvPilotForecastTimeseries.id_pilot'
    )
    forecast_jobs: Mapped[list['ForecastJobDB']] = relationship('ForecastJobDB', back_populates='pilot')
    secrets: Mapped[list['PilotSecret']] = relationship(
        'PilotSecret', back_populates='pilot', cascade='all, delete-orphan'
    )


class PilotSecret(Base):
    """Encrypted credential for one of a pilot's data_sources connections.

    One row per (pilot, name). `ciphertext` is a Fernet token produced by
    app.services.common.secrets; the plaintext value is never stored here and
    is never returned by the API — it can only be overwritten (rotated).
    """
    __tablename__ = 'pilot_secret'
    __table_args__ = (
        ForeignKeyConstraint(['id_pilot'], ['pilot.id'], ondelete='CASCADE', onupdate='CASCADE', name='pilot_secret_pilot_fkey'),
        PrimaryKeyConstraint('id', name='pilot_secret_pkey'),
        UniqueConstraint('id_pilot', 'name', name='pilot_secret_pilot_name_key'),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    id_pilot: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text('now()'))
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text('now()'))

    pilot: Mapped['Pilot'] = relationship('Pilot', back_populates='secrets')


class Chargers(Base):
    __tablename__ = 'chargers'
    __table_args__ = (
        ForeignKeyConstraint(['id_owner'], ['owners.id'], ondelete='SET NULL', onupdate='CASCADE', name='fk_owner'),
        ForeignKeyConstraint(['id_pilot'], ['pilot.id'], ondelete='SET NULL', onupdate='CASCADE', name='fk_pilot'),
        PrimaryKeyConstraint('id', name='chargers_pkey')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    latitude: Mapped[float] = mapped_column(Double(53), nullable=False)
    longitude: Mapped[float] = mapped_column(Double(53), nullable=False)
    nominal_power: Mapped[float] = mapped_column(Double(53), nullable=False)
    plugs: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text('now()'))
    id_owner: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    id_pilot: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    control_algorithm: Mapped[Optional[str]] = mapped_column(String)
    control_policy: Mapped[Optional[str]] = mapped_column(String)

    owners: Mapped[Optional['Owners']] = relationship('Owners', back_populates='chargers')
    pilot: Mapped[Optional['Pilot']] = relationship('Pilot', back_populates='chargers')
    charging_sessions: Mapped[list['ChargingSessions']] = relationship('ChargingSessions', back_populates='chargers')
    ev_duration_cdf: Mapped[list['EvDurationCdf']] = relationship('EvDurationCdf', back_populates='chargers')
    ev_forecast_stats: Mapped[list['EvForecastStats']] = relationship('EvForecastStats', back_populates='chargers')


class ChargingSessions(Base):
    __tablename__ = 'charging_sessions'
    __table_args__ = (
        ForeignKeyConstraint(['id_charger'], ['chargers.id'], ondelete='SET NULL', onupdate='CASCADE', name='fk_charger'),
        PrimaryKeyConstraint('id', name='charging_sessions_pkey')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    start_time: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    energy_delivered_kwh: Mapped[float] = mapped_column(Double(53), nullable=False)
    forecasted_energy_kwh: Mapped[float] = mapped_column(Double(53), nullable=False)
    forecasted_energy_kwh_std: Mapped[float] = mapped_column(Double(53), nullable=False)
    forecasted_duration_hours: Mapped[float] = mapped_column(Double(53), nullable=False)
    forecasted_duration_hours_std: Mapped[float] = mapped_column(Double(53), nullable=False)
    controlled_charging_points: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('1'))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text('true'))
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text('now()'))
    id_charger: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    end_time: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))
    end_charging_time: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))
    duration: Mapped[Optional[float]] = mapped_column(Double(53))
    # Timestamp of the last accepted connected/update/disconnected event; used
    # to reject out-of-order events (must always increase).
    last_event_time: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))

    chargers: Mapped[Optional['Chargers']] = relationship('Chargers', back_populates='charging_sessions')
    actions: Mapped[list['Actions']] = relationship('Actions', back_populates='charging_sessions')


class EvDurationCdf(Base):
    __tablename__ = 'ev_duration_cdf'
    __table_args__ = (
        ForeignKeyConstraint(['id_charger'], ['chargers.id'], ondelete='SET NULL', onupdate='CASCADE', name='ev_duration_cdf_id_charger_fkey'),
        PrimaryKeyConstraint('id', name='ev_duration_cdf_pkey1')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    local_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    horizon_hours: Mapped[float] = mapped_column(Double(53), nullable=False)
    probability: Mapped[float] = mapped_column(Double(53), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text('now()'))
    id_charger: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)

    chargers: Mapped[Optional['Chargers']] = relationship('Chargers', back_populates='ev_duration_cdf')


class EvForecastStats(Base):
    __tablename__ = 'ev_forecast_stats'
    __table_args__ = (
        ForeignKeyConstraint(['id_charger'], ['chargers.id'], ondelete='SET NULL', onupdate='CASCADE', name='ev_forecast_stats_id_charger_fkey'),
        PrimaryKeyConstraint('id', name='ev_forecast_stats_pkey1')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    local_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    mean_energy_kwh: Mapped[float] = mapped_column(Double(53), nullable=False)
    std_energy_kwh: Mapped[float] = mapped_column(Double(53), nullable=False)
    mean_duration_hours: Mapped[float] = mapped_column(Double(53), nullable=False)
    std_duration_hours: Mapped[float] = mapped_column(Double(53), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text('now()'))
    id_charger: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)

    chargers: Mapped[Optional['Chargers']] = relationship('Chargers', back_populates='ev_forecast_stats')


class Actions(Base):
    __tablename__ = 'actions'
    __table_args__ = (
        ForeignKeyConstraint(['id_cs'], ['charging_sessions.id'], ondelete='SET NULL', onupdate='CASCADE', name='actions_id_cs_fkey'),
        PrimaryKeyConstraint('id', name='actions_pkey')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    current_time: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_power_kw: Mapped[float] = mapped_column(Double(53), nullable=False)
    energy_delivered_kwh: Mapped[float] = mapped_column(Double(53), nullable=False)
    is_fully_charged: Mapped[bool] = mapped_column(Boolean, nullable=False)
    probability_disconnection: Mapped[float] = mapped_column(Double(53), nullable=False)
    cumulative_duration_probability: Mapped[float] = mapped_column(Double(53), nullable=False)
    # NULL means no decision could be produced (assigned + default policy both failed).
    suggested_action: Mapped[Optional[str]] = mapped_column(String)
    id_cs: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    control_policy: Mapped[Optional[str]] = mapped_column(String)
    control_algorithm: Mapped[Optional[str]] = mapped_column(String)
    correction_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text('false'))
    suggested_power_kw: Mapped[Optional[float]] = mapped_column(Double(53))
    real_action: Mapped[Optional[str]] = mapped_column(String)
    real_power_kw: Mapped[Optional[float]] = mapped_column(Double(53))
    # Policy-variable inputs for debugging (e.g. wind_excess_kw). Shape is
    # application-defined; v1 stores {"inputs": { ... policy_context ... }}.
    decision_context: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # True when the assigned policy raised and the default policy was used instead.
    policy_error: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text('false'))
    policy_error_message: Mapped[Optional[str]] = mapped_column(String)

    charging_sessions: Mapped[Optional['ChargingSessions']] = relationship('ChargingSessions', back_populates='actions')
    grid_load_forecasted: Mapped[list['GridLoadForecasted']] = relationship('GridLoadForecasted', back_populates='actions')


class GridLoadForecasted(Base):
    __tablename__ = 'grid_load_forecasted'
    __table_args__ = (
        ForeignKeyConstraint(['id_action'], ['actions.id'], ondelete='SET NULL', onupdate='CASCADE', name='grid_load_forecasted_id_action_fkey'),
        PrimaryKeyConstraint('id', name='grid_load_forecasted_pkey')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    value: Mapped[float] = mapped_column(Double(53), nullable=False)
    forecast_timestamp: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    id_action: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)

    actions: Mapped[Optional['Actions']] = relationship('Actions', back_populates='grid_load_forecasted')


class EvPilotForecastTimeseries(Base):
    __tablename__ = 'ev_pilot_forecast_timeseries'
    __table_args__ = (
        ForeignKeyConstraint(
            ['id_pilot'],
            ['pilot.id'],
            ondelete='SET NULL',
            onupdate='CASCADE',
            name='ev_pilot_forecast_ts_pilot_fkey',
        ),
        PrimaryKeyConstraint('id', name='ev_pilot_forecast_ts_pkey'),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    id_pilot: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, nullable=True)
    run_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    origin_time: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    forecast_time: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    presence: Mapped[float] = mapped_column(Double(53), nullable=False)
    energy_kwh: Mapped[float] = mapped_column(Double(53), nullable=False)
    artifact_name: Mapped[str] = mapped_column(String, nullable=False)
    quantiles_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    pilot: Mapped[Optional['Pilot']] = relationship('Pilot', back_populates='ev_pilot_forecast_timeseries')


class ForecastJobDB(Base):
    __tablename__ = 'forecast_jobs'
    __table_args__ = (
        ForeignKeyConstraint(
            ['id_pilot'],
            ['pilot.id'],
            ondelete='CASCADE',
            onupdate='CASCADE',
            name='forecast_jobs_pilot_fkey',
        ),
        PrimaryKeyConstraint('id', name='forecast_jobs_pkey'),
        UniqueConstraint('job_id', name='forecast_jobs_job_id_key'),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    job_id: Mapped[str] = mapped_column(String, nullable=False)
    id_pilot: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text('false'))
    predict_periodicity_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('60'))
    artifact_path: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'reg'"))
    freq: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'1h'"))
    timezone: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'UTC'"))
    horizon: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sim_steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('24'))
    sim_dt_hours: Mapped[float] = mapped_column(Double(53), nullable=False, server_default=text('1.0'))
    sim_power_kw: Mapped[float] = mapped_column(Double(53), nullable=False, server_default=text('11.0'))
    cal_fraction: Mapped[float] = mapped_column(Double(53), nullable=False, server_default=text('0.2'))
    lags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text('now()'))

    pilot: Mapped['Pilot'] = relationship('Pilot', back_populates='forecast_jobs')


class SystemErrors(Base):
    """Generic error log for failures not tied to a single actions row."""

    __tablename__ = 'system_errors'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='system_errors_pkey'),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    occurred_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text('now()'))
    source: Mapped[str] = mapped_column(String, nullable=False)
    charger_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)
    error_message: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
