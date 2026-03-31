-- Pilot-level EV time-series forecasts produced by Celery + local model artifacts.
CREATE TABLE IF NOT EXISTS public.ev_pilot_forecast_timeseries (
    id uuid NOT NULL,
    id_pilot uuid NOT NULL,
    run_at timestamp without time zone NOT NULL,
    origin_time timestamp without time zone NOT NULL,
    forecast_time timestamp without time zone NOT NULL,
    presence double precision NOT NULL,
    energy_kwh double precision NOT NULL,
    artifact_name character varying NOT NULL,
    quantiles_json jsonb NULL,
    CONSTRAINT ev_pilot_forecast_ts_pkey PRIMARY KEY (id),
    CONSTRAINT ev_pilot_forecast_ts_pilot_fkey FOREIGN KEY (id_pilot)
        REFERENCES public.pilot (id) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ev_pilot_forecast_ts_pilot_run_idx
    ON public.ev_pilot_forecast_timeseries (id_pilot, run_at DESC);
