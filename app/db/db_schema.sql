--
-- PostgreSQL database dump
--

\restrict 3ASDVWhPvg2Xh0hIBdB6F8S83F7XSvBZxtINQNNpGjvFaLrf1E6gL4072QHwzU8

-- Dumped from database version 18.1
-- Dumped by pg_dump version 18.1

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: update_updated_at_column(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION public.update_updated_at_column() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
   NEW.updated_at = NOW();
   RETURN NEW;
END;
$$;


ALTER FUNCTION public.update_updated_at_column() OWNER TO postgres;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: actions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.actions (
    id uuid NOT NULL,
    id_cs uuid,
    "current_time" timestamp with time zone NOT NULL,
    current_power_kw double precision NOT NULL,
    energy_delivered_kwh double precision NOT NULL,
    is_fully_charged boolean NOT NULL,
    probability_disconnection double precision NOT NULL,
    cumulative_duration_probability double precision NOT NULL,
    suggested_action character varying,
    control_policy character varying,
    control_algorithm character varying,
    correction_applied boolean DEFAULT false NOT NULL,
    suggested_power_kw double precision,
    real_action character varying,
    real_power_kw double precision,
    decision_context jsonb,
    policy_error boolean DEFAULT false NOT NULL,
    policy_error_message character varying
);


ALTER TABLE public.actions OWNER TO postgres;

--
-- Name: chargers; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.chargers (
    id uuid NOT NULL,
    id_owner uuid,
    name character varying NOT NULL,
    type character varying NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    nominal_power double precision NOT NULL,
    plugs character varying NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    id_pilot uuid,
    control_algorithm character varying,
    control_policy character varying
);


ALTER TABLE public.chargers OWNER TO postgres;

--
-- Name: charging_sessions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.charging_sessions (
    id uuid NOT NULL,
    id_charger uuid,
    start_time timestamp with time zone NOT NULL,
    end_time timestamp with time zone,
    end_charging_time timestamp with time zone,
    duration double precision,
    energy_delivered_kwh double precision NOT NULL,
    forecasted_energy_kwh double precision NOT NULL,
    forecasted_energy_kwh_std double precision NOT NULL,
    forecasted_duration_hours double precision NOT NULL,
    forecasted_duration_hours_std double precision NOT NULL,
    controlled_charging_points integer DEFAULT 1 NOT NULL,
    active boolean DEFAULT true NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    client_utc_offset_minutes integer,
    last_event_time timestamp with time zone
);


ALTER TABLE public.charging_sessions OWNER TO postgres;

--
-- Name: ev_duration_cdf; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ev_duration_cdf (
    id uuid CONSTRAINT ev_duration_cdf_id_not_null1 NOT NULL,
    id_charger uuid,
    local_hour integer CONSTRAINT ev_duration_cdf_local_hour_not_null1 NOT NULL,
    horizon_hours double precision CONSTRAINT ev_duration_cdf_horizon_hours_not_null1 NOT NULL,
    probability double precision CONSTRAINT ev_duration_cdf_probability_not_null1 NOT NULL,
    sample_count integer CONSTRAINT ev_duration_cdf_sample_count_not_null1 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() CONSTRAINT ev_duration_cdf_updated_at_not_null1 NOT NULL
);


ALTER TABLE public.ev_duration_cdf OWNER TO postgres;

--
-- Name: ev_forecast_stats; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ev_forecast_stats (
    id uuid NOT NULL,
    id_charger uuid,
    local_hour integer CONSTRAINT ev_forecast_stats_local_hour_not_null1 NOT NULL,
    mean_energy_kwh double precision CONSTRAINT ev_forecast_stats_mean_energy_kwh_not_null1 NOT NULL,
    std_energy_kwh double precision CONSTRAINT ev_forecast_stats_std_energy_kwh_not_null1 NOT NULL,
    mean_duration_hours double precision CONSTRAINT ev_forecast_stats_mean_duration_hours_not_null1 NOT NULL,
    std_duration_hours double precision CONSTRAINT ev_forecast_stats_std_duration_hours_not_null1 NOT NULL,
    sample_count integer CONSTRAINT ev_forecast_stats_sample_count_not_null1 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() CONSTRAINT ev_forecast_stats_updated_at_not_null1 NOT NULL
);


ALTER TABLE public.ev_forecast_stats OWNER TO postgres;

--
-- Name: grid_load_forecasted; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.grid_load_forecasted (
    id uuid NOT NULL,
    id_action uuid,
    value double precision NOT NULL,
    forecast_timestamp timestamp with time zone NOT NULL
);


ALTER TABLE public.grid_load_forecasted OWNER TO postgres;

--
-- Name: owners; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.owners (
    id uuid NOT NULL,
    "user" character varying NOT NULL,
    password character varying NOT NULL,
    company_name character varying NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    type character varying,
    role character varying,
    token text
);


ALTER TABLE public.owners OWNER TO postgres;

--
-- Name: pilot; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.pilot (
    id uuid NOT NULL,
    name character varying NOT NULL,
    id_owner uuid NOT NULL,
    timezone_name character varying NOT NULL DEFAULT 'Europe/Zurich',
    forecast_meter character varying,
    forecast_site character varying,
    policy_signals jsonb
);


ALTER TABLE public.pilot OWNER TO postgres;

--
-- Name: actions actions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.actions
    ADD CONSTRAINT actions_pkey PRIMARY KEY (id);


--
-- Name: chargers chargers_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chargers
    ADD CONSTRAINT chargers_pkey PRIMARY KEY (id);


--
-- Name: charging_sessions charging_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.charging_sessions
    ADD CONSTRAINT charging_sessions_pkey PRIMARY KEY (id);


--
-- Name: ev_duration_cdf ev_duration_cdf_pkey1; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ev_duration_cdf
    ADD CONSTRAINT ev_duration_cdf_pkey1 PRIMARY KEY (id);


--
-- Name: ev_forecast_stats ev_forecast_stats_pkey1; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ev_forecast_stats
    ADD CONSTRAINT ev_forecast_stats_pkey1 PRIMARY KEY (id);


--
-- Name: grid_load_forecasted grid_load_forecasted_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.grid_load_forecasted
    ADD CONSTRAINT grid_load_forecasted_pkey PRIMARY KEY (id);


--
-- Name: owners owners_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.owners
    ADD CONSTRAINT owners_pkey PRIMARY KEY (id);


--
-- Name: owners owners_user_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.owners
    ADD CONSTRAINT owners_user_key UNIQUE ("user");


--
-- Name: pilot pilot_pk; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.pilot
    ADD CONSTRAINT pilot_pk PRIMARY KEY (id);


--
-- Name: pilot pilot_unique; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.pilot
    ADD CONSTRAINT pilot_unique UNIQUE (name);


--
-- Name: chargers set_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.chargers FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: charging_sessions set_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.charging_sessions FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: ev_duration_cdf set_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.ev_duration_cdf FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: ev_forecast_stats set_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.ev_forecast_stats FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: owners set_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.owners FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: actions actions_id_cs_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.actions
    ADD CONSTRAINT actions_id_cs_fkey FOREIGN KEY (id_cs) REFERENCES public.charging_sessions(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: ev_duration_cdf ev_duration_cdf_id_charger_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ev_duration_cdf
    ADD CONSTRAINT ev_duration_cdf_id_charger_fkey FOREIGN KEY (id_charger) REFERENCES public.chargers(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: ev_forecast_stats ev_forecast_stats_id_charger_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ev_forecast_stats
    ADD CONSTRAINT ev_forecast_stats_id_charger_fkey FOREIGN KEY (id_charger) REFERENCES public.chargers(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: charging_sessions fk_charger; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.charging_sessions
    ADD CONSTRAINT fk_charger FOREIGN KEY (id_charger) REFERENCES public.chargers(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: chargers fk_owner; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chargers
    ADD CONSTRAINT fk_owner FOREIGN KEY (id_owner) REFERENCES public.owners(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: chargers fk_pilot; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.chargers
    ADD CONSTRAINT fk_pilot FOREIGN KEY (id_pilot) REFERENCES public.pilot(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: grid_load_forecasted grid_load_forecasted_id_action_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.grid_load_forecasted
    ADD CONSTRAINT grid_load_forecasted_id_action_fkey FOREIGN KEY (id_action) REFERENCES public.actions(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: pilot id_owner; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.pilot
    ADD CONSTRAINT id_owner FOREIGN KEY (id_owner) REFERENCES public.owners(id) ON UPDATE CASCADE ON DELETE SET NULL;


--
-- Name: ev_pilot_forecast_timeseries; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.ev_pilot_forecast_timeseries (
    id uuid NOT NULL,
    id_pilot uuid NOT NULL,
    run_at timestamp with time zone NOT NULL,
    origin_time timestamp with time zone NOT NULL,
    forecast_time timestamp with time zone NOT NULL,
    presence double precision NOT NULL,
    energy_kwh double precision NOT NULL,
    artifact_name character varying NOT NULL,
    quantiles_json jsonb
);


ALTER TABLE public.ev_pilot_forecast_timeseries OWNER TO postgres;

--
-- Name: ev_pilot_forecast_timeseries ev_pilot_forecast_ts_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ev_pilot_forecast_timeseries
    ADD CONSTRAINT ev_pilot_forecast_ts_pkey PRIMARY KEY (id);


--
-- Name: ev_pilot_forecast_timeseries ev_pilot_forecast_ts_pilot_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.ev_pilot_forecast_timeseries
    ADD CONSTRAINT ev_pilot_forecast_ts_pilot_fkey FOREIGN KEY (id_pilot) REFERENCES public.pilot(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: ev_pilot_forecast_ts_pilot_run_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ev_pilot_forecast_ts_pilot_run_idx ON public.ev_pilot_forecast_timeseries USING btree (id_pilot, run_at DESC);


--
-- Name: forecast_jobs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.forecast_jobs (
    id uuid NOT NULL,
    job_id character varying NOT NULL,
    id_pilot uuid NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    predict_periodicity_minutes integer DEFAULT 60 NOT NULL,
    artifact_path character varying NOT NULL,
    kind character varying DEFAULT 'reg'::character varying NOT NULL,
    freq character varying DEFAULT '1h'::character varying NOT NULL,
    timezone character varying DEFAULT 'UTC'::character varying NOT NULL,
    horizon integer,
    sim_steps integer DEFAULT 24 NOT NULL,
    sim_dt_hours double precision DEFAULT 1.0 NOT NULL,
    sim_power_kw double precision DEFAULT 11.0 NOT NULL,
    cal_fraction double precision DEFAULT 0.2 NOT NULL,
    lags json,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.forecast_jobs OWNER TO postgres;

--
-- Name: forecast_jobs forecast_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_jobs
    ADD CONSTRAINT forecast_jobs_pkey PRIMARY KEY (id);


--
-- Name: forecast_jobs forecast_jobs_job_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_jobs
    ADD CONSTRAINT forecast_jobs_job_id_key UNIQUE (job_id);


--
-- Name: forecast_jobs forecast_jobs_pilot_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.forecast_jobs
    ADD CONSTRAINT forecast_jobs_pilot_fkey FOREIGN KEY (id_pilot) REFERENCES public.pilot(id) ON UPDATE CASCADE ON DELETE CASCADE;


--
-- Name: system_errors; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.system_errors (
    id uuid NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    source character varying NOT NULL,
    charger_id uuid,
    session_id uuid,
    error_message text NOT NULL,
    context jsonb
);


ALTER TABLE public.system_errors OWNER TO postgres;

--
-- Name: system_errors system_errors_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.system_errors
    ADD CONSTRAINT system_errors_pkey PRIMARY KEY (id);


--
-- Name: forecast_jobs set_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER set_updated_at BEFORE UPDATE ON public.forecast_jobs FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- PostgreSQL database dump complete
--

\unrestrict 3ASDVWhPvg2Xh0hIBdB6F8S83F7XSvBZxtINQNNpGjvFaLrf1E6gL4072QHwzU8

