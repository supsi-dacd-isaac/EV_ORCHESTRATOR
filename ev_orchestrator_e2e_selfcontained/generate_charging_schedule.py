#!/usr/bin/env python3
"""
Generate a synthetic EV charging schedule for the EV Orchestrator E2E test.

Run directly in PyCharm or with:
    python generate_charging_schedule.py

This file does not call the API. It only creates CSV files and diagnostic plots.
The main CSV stores relative time deltas in minutes. The API test runner converts
those deltas into absolute timestamps at runtime and logs the reference start.

Important behaviour:
  - Sessions are allowed to cross midnight when ALLOW_OVERNIGHT_SESSIONS = True.
  - Sessions never overlap on the same charging point.
  - Different charging points can have different numbers of sessions per day.
  - After generation, the script creates plots for connection/disconnection
    events, active sessions, delivered energy, and charging rate.
"""

from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

# Use a non-interactive backend so the script also works from PyCharm, terminals,
# and remote machines without opening GUI windows.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# =============================================================================
# EDITABLE CONFIGURATION
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "outputs"
PLOTS_DIR = OUTPUT_DIR / "plots"

SCHEDULE_CSV_NAME = "synthetic_charging_schedule.csv"
SESSION_SUMMARY_CSV_NAME = "synthetic_session_summary.csv"
GENERATION_SUMMARY_JSON_NAME = "synthetic_generation_summary.json"

PILOT_NAME = "interped_example pilot"
CHARGER_COUNT = 5
NOMINAL_POWER_KW = 11.0

# To run a short LOCAL test, set SCHEDULE_DAYS = 1 (and keep the same value
# in run_e2e_api_test.py). For the full 7-day partner deployment test, use 7.
SCHEDULE_DAYS = 3
UPDATE_INTERVAL_MINUTES = 15
MIN_SESSION_DURATION_MINUTES = 10
MAX_SESSION_DURATION_MINUTES = 8 * 60
RANDOM_SEED = 20260610

# Sessions can cross from one day to the next. This is now enabled by default.
ALLOW_OVERNIGHT_SESSIONS = True

# If overnight sessions are enabled, a session can end after the nominal 7-day
# horizon. This allows late sessions on day 7 to disconnect on day 8.
MAX_EXTENSION_AFTER_FINAL_DAY_MINUTES = 3 * 60

# Each charging point can have a different number of sessions per day.
# Values are inclusive min/max bounds. Keep them within 1..5 as requested.
SESSIONS_PER_DAY_BY_CHARGER = {
    1: (1, 2),
    2: (2, 4),
    3: (1, 5),
    4: (3, 5),
    5: (1, 3),
}

# Daily start window in local time, expressed as minutes since 00:00.
# Late starts are intentionally allowed so that some sessions can continue after midnight.
EARLIEST_START_MINUTE_OF_DAY = 2 * 60        # 05:00
LATEST_START_MINUTE_OF_DAY = 23 * 60 + 30   # 23:30

# Used only when ALLOW_OVERNIGHT_SESSIONS = False.
LATEST_END_MINUTE_OF_DAY = 23 * 60 + 45      # 23:45

# Gaps between two consecutive sessions on the same charging point.
MIN_GAP_BETWEEN_SESSIONS_MINUTES = 5
MAX_GAP_BETWEEN_SESSIONS_MINUTES = 240

# Charging behaviour.
MIN_CHARGING_RATE_KW = 3.0
MAX_CHARGING_RATE_KW = 11                  # must stay below 11 kW nominal power
MAX_TARGET_ENERGY_KWH = 42.0
PROBABILITY_ZERO_ACTION_WHILE_NOT_FULL = 0.08

# Plot output.
PLOT_DPI = 160


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class ScheduleEvent:
    row_id: int
    synthetic_session_id: str
    day_index: int
    charger_index: int
    charger_name: str
    charger_id: str
    event_type: str
    timestamp_delta_minutes: int
    start_delta_minutes: int
    end_delta_minutes: int
    end_charging_delta_minutes: int
    charging_rate_kw: float
    energy_since_last_event_kwh: float
    cumulative_energy_kwh: float
    fully_charged: bool
    action: int
    nominal_power_kw: float
    target_energy_kwh: float
    expected_duration_minutes: int
    crosses_midnight: bool


@dataclass
class SessionSummary:
    synthetic_session_id: str
    day_index: int
    charger_index: int
    charger_name: str
    sessions_requested_for_charger_day: int
    sessions_generated_for_charger_day: int
    start_delta_minutes: int
    end_delta_minutes: int
    end_charging_delta_minutes: int
    start_day_index: int
    end_day_index: int
    crosses_midnight: bool
    duration_minutes: int
    target_energy_kwh: float
    delivered_energy_kwh: float
    fully_charged_at_disconnect: bool
    zero_action_not_full_count: int


# =============================================================================
# BASIC HELPERS
# =============================================================================

def charger_name(charger_index: int) -> str:
    return f"{PILOT_NAME} CP{charger_index}"


def feasible_energy(rate_kw: float, duration_minutes: int) -> float:
    return max(0.0, rate_kw * duration_minutes / 60.0)


def minute_label(delta_minutes: int) -> str:
    day = delta_minutes // (24 * 60) + 1
    minute_in_day = delta_minutes % (24 * 60)
    hh = minute_in_day // 60
    mm = minute_in_day % 60
    return f"D{day} {hh:02d}:{mm:02d}"


def day_index_from_delta(delta_minutes: int) -> int:
    return delta_minutes // (24 * 60) + 1


# =============================================================================
# GENERATION LOGIC
# =============================================================================

def generate_schedule() -> tuple[list[ScheduleEvent], list[SessionSummary], dict]:
    rng = random.Random(RANDOM_SEED)
    events: list[ScheduleEvent] = []
    summaries: list[SessionSummary] = []

    row_id = 0
    global_session_counter = 0

    requested_daily_counts: dict[str, dict[str, int]] = defaultdict(dict)
    generated_daily_counts: dict[str, dict[str, int]] = defaultdict(dict)
    overnight_sessions_by_charger: dict[str, int] = defaultdict(int)

    # One availability cursor per charger. This is what prevents overlaps, even
    # when a session crosses midnight and continues into the next calendar day.
    charger_available_after: dict[int, int] = {i: 0 for i in range(1, CHARGER_COUNT + 1)}

    nominal_horizon_end = SCHEDULE_DAYS * 24 * 60
    absolute_latest_end = nominal_horizon_end + MAX_EXTENSION_AFTER_FINAL_DAY_MINUTES

    for day_index in range(SCHEDULE_DAYS):
        day_offset = day_index * 24 * 60
        latest_start_today = day_offset + LATEST_START_MINUTE_OF_DAY

        for charger_index in range(1, CHARGER_COUNT + 1):
            c_name = charger_name(charger_index)
            min_sessions, max_sessions = SESSIONS_PER_DAY_BY_CHARGER.get(charger_index, (1, 5))
            sessions_requested = rng.randint(min_sessions, max_sessions)
            sessions_generated = 0

            requested_daily_counts[f"day_{day_index + 1}"][c_name] = sessions_requested

            # Initial cursor for this charger/day. It is constrained by both the
            # day start window and the previous session on the same charger.
            day_start_candidate = day_offset + rng.randint(
                EARLIEST_START_MINUTE_OF_DAY,
                min(EARLIEST_START_MINUTE_OF_DAY + 180, LATEST_START_MINUTE_OF_DAY),
            )
            cursor = max(day_start_candidate, charger_available_after[charger_index])

            for _ in range(sessions_requested):
                gap_minutes = rng.randint(MIN_GAP_BETWEEN_SESSIONS_MINUTES, MAX_GAP_BETWEEN_SESSIONS_MINUTES)
                start_delta = max(cursor + gap_minutes, day_offset + EARLIEST_START_MINUTE_OF_DAY)

                # If the previous overnight session pushes the next available
                # start beyond today's start window, the skipped sessions are
                # recorded in the summary counts rather than forced to overlap.
                if start_delta > latest_start_today:
                    break

                requested_duration = rng.randint(MIN_SESSION_DURATION_MINUTES, MAX_SESSION_DURATION_MINUTES)

                if ALLOW_OVERNIGHT_SESSIONS:
                    end_delta = min(start_delta + requested_duration, absolute_latest_end)
                else:
                    end_delta = min(start_delta + requested_duration, day_offset + LATEST_END_MINUTE_OF_DAY)

                duration_minutes = end_delta - start_delta
                if duration_minutes < MIN_SESSION_DURATION_MINUTES:
                    break

                global_session_counter += 1
                sessions_generated += 1
                synthetic_session_id = f"S{global_session_counter:04d}"

                crosses_midnight = start_delta // (24 * 60) != end_delta // (24 * 60)
                if crosses_midnight:
                    overnight_sessions_by_charger[c_name] += 1

                base_rate_kw = round(
                    rng.uniform(MIN_CHARGING_RATE_KW, min(MAX_CHARGING_RATE_KW, NOMINAL_POWER_KW)),
                    2,
                )

                # Keep target energy physically feasible under the base charging
                # rate. Zero-action intervals may still prevent some sessions
                # from becoming fully charged by disconnection.
                physical_max_energy = feasible_energy(base_rate_kw, duration_minutes)
                target_low = max(0.15, physical_max_energy * 0.35)
                target_high = min(MAX_TARGET_ENERGY_KWH, max(target_low + 0.05, physical_max_energy * 0.95))
                target_energy = round(rng.uniform(target_low, target_high), 1)

                delivered = 0.0
                end_charging_delta = end_delta
                zero_action_not_full_count = 0

                # Connection event: no energy delivered yet.
                row_id += 1
                events.append(
                    ScheduleEvent(
                        row_id=row_id,
                        synthetic_session_id=synthetic_session_id,
                        day_index=day_index + 1,
                        charger_index=charger_index,
                        charger_name=c_name,
                        charger_id="",  # Filled by the API runner after charger creation/reuse.
                        event_type="vehicle_connected",
                        timestamp_delta_minutes=start_delta,
                        start_delta_minutes=start_delta,
                        end_delta_minutes=end_delta,
                        end_charging_delta_minutes=end_charging_delta,
                        charging_rate_kw=base_rate_kw,
                        energy_since_last_event_kwh=0.0,
                        cumulative_energy_kwh=0.0,
                        fully_charged=False,
                        action=1,
                        nominal_power_kw=NOMINAL_POWER_KW,
                        target_energy_kwh=target_energy,
                        expected_duration_minutes=duration_minutes,
                        crosses_midnight=crosses_midnight,
                    )
                )

                last_event_delta = start_delta
                t = start_delta + UPDATE_INTERVAL_MINUTES
                # prev_action tracks the action returned by the previous API event.
                # Energy reported at each update reflects what happened since that action.
                prev_action = 1  # vehicle_connected always starts charging (action=1)

                while t < end_delta:
                    elapsed = t - last_event_delta

                    # --- Energy delivered this interval: based on the PREVIOUS action ---
                    if prev_action == 0:
                        # Charger was told to stop; no energy delivered.
                        energy_kwh = 0.0
                        rate_kw = 0.0
                    else:
                        remaining = max(0.0, target_energy - delivered)
                        progress = delivered / target_energy if target_energy > 0 else 1.0
                        taper_factor = 1.0 if progress < 0.70 else max(0.25, 1.0 - (progress - 0.70) * 1.8)
                        desired_rate = min(base_rate_kw * taper_factor, NOMINAL_POWER_KW)
                        max_energy_this_step = feasible_energy(desired_rate, elapsed)
                        energy_kwh = round(min(remaining, max_energy_this_step), 1)
                        rate_kw = round(energy_kwh / (elapsed / 60.0), 1) if elapsed > 0 else 0.0
                        delivered = round(delivered + energy_kwh, 1)

                    remaining = max(0.0, target_energy - delivered)
                    fully_charged = remaining <= 0.005
                    if fully_charged:
                        end_charging_delta = min(end_charging_delta, t)

                    # --- New action for the NEXT interval ---
                    if fully_charged:
                        action = 0
                    else:
                        force_zero = rng.random() < PROBABILITY_ZERO_ACTION_WHILE_NOT_FULL
                        action = 0 if force_zero else 1
                        if force_zero:
                            zero_action_not_full_count += 1

                    row_id += 1
                    events.append(
                        ScheduleEvent(
                            row_id=row_id,
                            synthetic_session_id=synthetic_session_id,
                            day_index=day_index + 1,
                            charger_index=charger_index,
                            charger_name=c_name,
                            charger_id="",
                            event_type="charging_update",
                            timestamp_delta_minutes=t,
                            start_delta_minutes=start_delta,
                            end_delta_minutes=end_delta,
                            end_charging_delta_minutes=end_charging_delta,
                            charging_rate_kw=round(rate_kw, 1),
                            energy_since_last_event_kwh=round(energy_kwh, 1),
                            cumulative_energy_kwh=round(delivered, 1),
                            fully_charged=fully_charged,
                            action=action,
                            nominal_power_kw=NOMINAL_POWER_KW,
                            target_energy_kwh=target_energy,
                            expected_duration_minutes=duration_minutes,
                            crosses_midnight=crosses_midnight,
                        )
                    )

                    prev_action = action
                    last_event_delta = t
                    t += UPDATE_INTERVAL_MINUTES

                # Disconnection event: energy based on prev_action (last action returned).
                elapsed = end_delta - last_event_delta
                remaining = max(0.0, target_energy - delivered)

                if prev_action == 0 or remaining <= 0.005:
                    disc_energy = 0.0
                    disc_rate_kw = 0.0
                    disc_fully_charged = remaining <= 0.005
                else:
                    max_energy = feasible_energy(min(base_rate_kw, NOMINAL_POWER_KW - 0.1), elapsed)
                    disc_energy = round(min(remaining, max_energy), 1)
                    disc_rate_kw = round(disc_energy / (elapsed / 60.0), 1) if elapsed > 0 and disc_energy > 0 else 0.0
                    delivered = round(delivered + disc_energy, 1)
                    disc_fully_charged = delivered >= target_energy - 0.005
                if disc_fully_charged:
                    end_charging_delta = min(end_charging_delta, end_delta)

                row_id += 1
                events.append(
                    ScheduleEvent(
                        row_id=row_id,
                        synthetic_session_id=synthetic_session_id,
                        day_index=day_index + 1,
                        charger_index=charger_index,
                        charger_name=c_name,
                        charger_id="",
                        event_type="vehicle_disconnected",
                        timestamp_delta_minutes=end_delta,
                        start_delta_minutes=start_delta,
                        end_delta_minutes=end_delta,
                        end_charging_delta_minutes=end_charging_delta,
                        charging_rate_kw=round(disc_rate_kw, 1),
                        energy_since_last_event_kwh=round(disc_energy, 1),
                        cumulative_energy_kwh=round(delivered, 1),
                        fully_charged=disc_fully_charged,
                        action=0, # at disconnection, not action is required. This is a placeholder
                        nominal_power_kw=NOMINAL_POWER_KW,
                        target_energy_kwh=target_energy,
                        expected_duration_minutes=duration_minutes,
                        crosses_midnight=crosses_midnight,
                    )
                )

                # Backfill final end_charging_delta for all rows in the session.
                for event in events:
                    if event.synthetic_session_id == synthetic_session_id:
                        event.end_charging_delta_minutes = end_charging_delta

                summaries.append(
                    SessionSummary(
                        synthetic_session_id=synthetic_session_id,
                        day_index=day_index + 1,
                        charger_index=charger_index,
                        charger_name=c_name,
                        sessions_requested_for_charger_day=sessions_requested,
                        sessions_generated_for_charger_day=sessions_generated,
                        start_delta_minutes=start_delta,
                        end_delta_minutes=end_delta,
                        end_charging_delta_minutes=end_charging_delta,
                        start_day_index=day_index_from_delta(start_delta),
                        end_day_index=day_index_from_delta(end_delta),
                        crosses_midnight=crosses_midnight,
                        duration_minutes=duration_minutes,
                        target_energy_kwh=target_energy,
                        delivered_energy_kwh=round(delivered, 1),
                        fully_charged_at_disconnect=disc_fully_charged,
                        zero_action_not_full_count=zero_action_not_full_count,
                    )
                )

                cursor = end_delta
                charger_available_after[charger_index] = end_delta

            generated_daily_counts[f"day_{day_index + 1}"][c_name] = sessions_generated

    events.sort(key=lambda row: (row.timestamp_delta_minutes, row.row_id))

    zero_action_not_full_total = sum(1 for e in events if e.action == 0 and not e.fully_charged)
    zero_by_charger = defaultdict(int)
    for e in events:
        if e.action == 0 and not e.fully_charged:
            zero_by_charger[e.charger_name] += 1

    total_energy_by_charger = defaultdict(float)
    for e in events:
        total_energy_by_charger[e.charger_name] += e.energy_since_last_event_kwh

    summary = {
        "pilot_name": PILOT_NAME,
        "schedule_days": SCHEDULE_DAYS,
        "charger_count": CHARGER_COUNT,
        "nominal_power_kw": NOMINAL_POWER_KW,
        "update_interval_minutes": UPDATE_INTERVAL_MINUTES,
        "random_seed": RANDOM_SEED,
        "allow_overnight_sessions": ALLOW_OVERNIGHT_SESSIONS,
        "n_events": len(events),
        "n_sessions": len(summaries),
        "n_overnight_sessions": sum(1 for s in summaries if s.crosses_midnight),
        "overnight_sessions_by_charger": dict(overnight_sessions_by_charger),
        "requested_daily_session_counts_by_charger": requested_daily_counts,
        "generated_daily_session_counts_by_charger": generated_daily_counts,
        "zero_action_not_fully_charged_total": zero_action_not_full_total,
        "zero_action_not_fully_charged_by_charger": dict(zero_by_charger),
        "total_energy_kwh_by_charger": {k: round(v, 3) for k, v in total_energy_by_charger.items()},
        "note": "All timestamps are relative deltas in minutes. The API runner computes absolute timestamps at runtime.",
    }
    return events, summaries, summary


# =============================================================================
# WRITING OUTPUTS
# =============================================================================

def write_csv(path: Path, rows: list[object]) -> None:
    if not rows:
        raise ValueError(f"No rows to write to {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


# =============================================================================
# PLOTS
# =============================================================================

def _day_boundary_lines(ax, max_minutes: int) -> None:
    for day in range(1, max_minutes // (24 * 60) + 1):
        ax.axvline(day * 24, linestyle="--", linewidth=0.7, alpha=0.35)


def plot_connection_disconnection_events(events: list[ScheduleEvent], path: Path) -> None:
    connects = [e for e in events if e.event_type == "vehicle_connected"]
    disconnects = [e for e in events if e.event_type == "vehicle_disconnected"]
    max_minutes = max(e.timestamp_delta_minutes for e in events)

    fig, ax = plt.subplots(figsize=(13, 4.8))
    ax.scatter(
        [e.timestamp_delta_minutes / 60.0 for e in connects],
        [e.charger_index for e in connects],
        marker="^",
        s=42,
        label="Connection",
        alpha=0.85,
    )
    ax.scatter(
        [e.timestamp_delta_minutes / 60.0 for e in disconnects],
        [e.charger_index for e in disconnects],
        marker="v",
        s=42,
        label="Disconnection",
        alpha=0.85,
    )
    _day_boundary_lines(ax, max_minutes)
    ax.set_title("Connection and disconnection events")
    ax.set_xlabel("Hours from schedule start")
    ax.set_ylabel("Charging point")
    ax.set_yticks(range(1, CHARGER_COUNT + 1))
    ax.set_yticklabels([f"CP{i}" for i in range(1, CHARGER_COUNT + 1)])
    ax.grid(True, axis="x", alpha=0.25)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(path, dpi=PLOT_DPI)
    plt.close(fig)


def plot_active_sessions(events: list[ScheduleEvent], path: Path) -> None:
    timeline: list[tuple[float, int]] = [(0.0, 0)]
    active = 0
    for event in sorted(events, key=lambda e: (e.timestamp_delta_minutes, e.row_id)):
        if event.event_type == "vehicle_connected":
            active += 1
        elif event.event_type == "vehicle_disconnected":
            active = max(0, active - 1)
        timeline.append((event.timestamp_delta_minutes / 60.0, active))

    max_minutes = max(e.timestamp_delta_minutes for e in events)
    x = [p[0] for p in timeline]
    y = [p[1] for p in timeline]

    fig, ax = plt.subplots(figsize=(13, 4.2))
    ax.step(x, y, where="post", linewidth=1.8)
    _day_boundary_lines(ax, max_minutes)
    ax.set_title("Total active charging sessions")
    ax.set_xlabel("Hours from schedule start")
    ax.set_ylabel("Connected vehicles")
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="both", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=PLOT_DPI)
    plt.close(fig)


def plot_daily_energy_by_charger(events: list[ScheduleEvent], path: Path) -> None:
    daily_energy: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    max_day = 1
    for event in events:
        day = event.timestamp_delta_minutes // (24 * 60) + 1
        max_day = max(max_day, day)
        daily_energy[day][event.charger_index] += event.energy_since_last_event_kwh

    days = list(range(1, max_day + 1))
    fig, ax = plt.subplots(figsize=(13, 4.8))
    for charger_index in range(1, CHARGER_COUNT + 1):
        values = [daily_energy[day].get(charger_index, 0.0) for day in days]
        ax.plot(days, values, marker="o", linewidth=1.6, label=f"CP{charger_index}")
    ax.set_title("Daily delivered energy by charging point")
    ax.set_xlabel("Schedule day")
    ax.set_ylabel("Delivered energy [kWh]")
    ax.set_xticks(days)
    ax.grid(True, axis="both", alpha=0.25)
    ax.legend(loc="upper right", ncol=min(CHARGER_COUNT, 5))
    fig.tight_layout()
    fig.savefig(path, dpi=PLOT_DPI)
    plt.close(fig)


def plot_cumulative_energy(events: list[ScheduleEvent], path: Path) -> None:
    by_charger: dict[int, list[ScheduleEvent]] = defaultdict(list)
    for event in events:
        by_charger[event.charger_index].append(event)

    fig, ax = plt.subplots(figsize=(13, 4.8))
    max_minutes = max(e.timestamp_delta_minutes for e in events)
    for charger_index in range(1, CHARGER_COUNT + 1):
        cumulative = 0.0
        xs: list[float] = []
        ys: list[float] = []
        for event in sorted(by_charger.get(charger_index, []), key=lambda e: (e.timestamp_delta_minutes, e.row_id)):
            cumulative += event.energy_since_last_event_kwh
            xs.append(event.timestamp_delta_minutes / 60.0)
            ys.append(cumulative)
        if xs:
            ax.plot(xs, ys, linewidth=1.6, label=f"CP{charger_index}")
    _day_boundary_lines(ax, max_minutes)
    ax.set_title("Cumulative delivered energy")
    ax.set_xlabel("Hours from schedule start")
    ax.set_ylabel("Delivered energy [kWh]")
    ax.grid(True, axis="both", alpha=0.25)
    ax.legend(loc="upper left", ncol=min(CHARGER_COUNT, 5))
    fig.tight_layout()
    fig.savefig(path, dpi=PLOT_DPI)
    plt.close(fig)


def plot_charging_rate(events: list[ScheduleEvent], path: Path) -> None:
    by_charger: dict[int, list[ScheduleEvent]] = defaultdict(list)
    for event in events:
        if event.event_type in {"vehicle_connected", "charging_update", "vehicle_disconnected"}:
            by_charger[event.charger_index].append(event)

    max_minutes = max(e.timestamp_delta_minutes for e in events)
    fig, ax = plt.subplots(figsize=(13, 4.8))
    for charger_index in range(1, CHARGER_COUNT + 1):
        rows = sorted(by_charger.get(charger_index, []), key=lambda e: (e.timestamp_delta_minutes, e.row_id))
        if not rows:
            continue
        ax.plot(
            [e.timestamp_delta_minutes / 60.0 for e in rows],
            [e.charging_rate_kw for e in rows],
            marker=".",
            linewidth=1.0,
            alpha=0.8,
            label=f"CP{charger_index}",
        )
    ax.axhline(NOMINAL_POWER_KW, linestyle="--", linewidth=1.0, alpha=0.5, label="Nominal power")
    _day_boundary_lines(ax, max_minutes)
    ax.set_title("Charging rate over time")
    ax.set_xlabel("Hours from schedule start")
    ax.set_ylabel("Charging rate [kW]")
    ax.set_ylim(bottom=0, top=NOMINAL_POWER_KW + 1)
    ax.grid(True, axis="both", alpha=0.25)
    ax.legend(loc="upper right", ncol=min(CHARGER_COUNT + 1, 6))
    fig.tight_layout()
    fig.savefig(path, dpi=PLOT_DPI)
    plt.close(fig)


def make_plots(events: list[ScheduleEvent]) -> dict[str, str]:
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    plot_paths = {
        "connection_disconnection_events": PLOTS_DIR / "connection_disconnection_events.png",
        "active_sessions_total": PLOTS_DIR / "active_sessions_total.png",
        "daily_energy_by_charger": PLOTS_DIR / "daily_energy_by_charger.png",
        "cumulative_energy": PLOTS_DIR / "cumulative_energy.png",
        "charging_rate": PLOTS_DIR / "charging_rate.png",
    }
    plot_connection_disconnection_events(events, plot_paths["connection_disconnection_events"])
    plot_active_sessions(events, plot_paths["active_sessions_total"])
    plot_daily_energy_by_charger(events, plot_paths["daily_energy_by_charger"])
    plot_cumulative_energy(events, plot_paths["cumulative_energy"])
    plot_charging_rate(events, plot_paths["charging_rate"])
    return {key: str(value) for key, value in plot_paths.items()}


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    events, sessions, summary = generate_schedule()

    schedule_path = OUTPUT_DIR / SCHEDULE_CSV_NAME
    session_summary_path = OUTPUT_DIR / SESSION_SUMMARY_CSV_NAME
    summary_path = OUTPUT_DIR / GENERATION_SUMMARY_JSON_NAME

    write_csv(schedule_path, events)
    write_csv(session_summary_path, sessions)

    plot_paths = make_plots(events)
    summary["plot_paths"] = plot_paths

    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    print("Synthetic charging schedule generated.")
    print(f"  schedule CSV:        {schedule_path}")
    print(f"  session summary CSV: {session_summary_path}")
    print(f"  generation summary:  {summary_path}")
    print(f"  plots folder:        {PLOTS_DIR}")
    print(f"  events:              {summary['n_events']}")
    print(f"  sessions:            {summary['n_sessions']}")
    print(f"  overnight sessions:  {summary['n_overnight_sessions']}")
    print(f"  action=0 while not fully charged: {summary['zero_action_not_fully_charged_total']}")

    print("\nGenerated plot files:")
    for label, path in plot_paths.items():
        print(f"  {label}: {path}")

    print("\nRequested vs generated sessions per day by charger:")
    for day in summary["requested_daily_session_counts_by_charger"]:
        requested = summary["requested_daily_session_counts_by_charger"][day]
        generated = summary["generated_daily_session_counts_by_charger"].get(day, {})
        print(f"  {day}:")
        print(f"    requested: {dict(requested)}")
        print(f"    generated: {dict(generated)}")


if __name__ == "__main__":
    main()
