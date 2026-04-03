"""
EV Utilities - Functions for generating time series from EV charging sessions data.

This module provides utilities to convert EV charging session data (with start_time,
end_time_connection, energy, station_id) into time series of presence and energy consumption.
"""

import pandas as pd
import numpy as np
from typing import Optional, Tuple

def station_capacities_by_sweepline(
    df: pd.DataFrame,
    start_col: str = 'start_time',
    end_col: str = 'end_time_connection',
    freq: str = '5min',   # choose '1min' for finest, '15min' / 'H' for coarser
) -> dict:
    """
    Capacity per station = max observed concurrent sessions using a sweep-line
    at resolution `freq`. Works even if time columns are strings.
    """
    # 1) Coerce to datetimes (handles strings); keep timezone if present
    g = df.copy()
    g[start_col] = pd.to_datetime(g[start_col], errors='coerce', utc=True)
    g[end_col]   = pd.to_datetime(g[end_col],   errors='coerce', utc=True)

    # drop rows we couldn't parse
    g = g.dropna(subset=[start_col, end_col])

    # ensure start <= end; if equal, bump end by one freq so it counts as 1 slot
    bad = g[end_col] < g[start_col]
    if bad.any():
        # swap or drop — here we drop clearly bad spans
        g = g.loc[~bad]
    equal = g[end_col] == g[start_col]
    if equal.any():
        g.loc[equal, end_col] = g.loc[equal, end_col] + pd.Timedelta(freq)

    # 2) Floor/ceil to grid
    g['start_floor'] = g[start_col].dt.floor(freq)
    g['end_ceil']    = g[end_col].dt.ceil(freq)

    # 3) Build +1/-1 events per station and take cumulative max
    capacities = {}
    for station, s in g.groupby('station_id', sort=False):
        # count starts and ends on the grid
        starts = s['start_floor'].value_counts()
        ends   = s['end_ceil'].value_counts() * -1

        events = pd.concat([starts, ends]).groupby(level=0).sum().sort_index()

        # make continuous index to cover gaps (optional but safer for cumsum)
        if not events.empty:
            full_idx = pd.date_range(events.index.min(), events.index.max(), freq=freq, inclusive='both')
            events = events.reindex(full_idx, fill_value=0)

        concurrency = events.cumsum()
        capacities[station] = int(concurrency.max()) if not concurrency.empty else 0

    return capacities

def generate_ev_timeseries_by_station(
    df: pd.DataFrame,
    freq: str = '1h',
    start_time_col: str = 'start_time',
    end_time_col: str = 'end_time_connection',
    energy_col: str = 'energy (kWh)',
    station_col: str = 'station_id',
    time_range: Optional[Tuple[pd.Timestamp, pd.Timestamp]] = None,
    timezone: str = 'UTC'
) -> pd.DataFrame:
    """
    Generate time series of EV presence and energy consumed, grouped by station.
    
    Returns a DataFrame with MultiIndex columns (station_id, metric) where
    metric is 'presence' or 'energy_consumed'.
    
    Parameters
    ----------
    df : pd.DataFrame
        DataFrame containing charging session data
    freq : str, default '1h'
        Frequency of the output time series
    start_time_col, end_time_col, energy_col, station_col : str
        Column names for the respective fields
    time_range : tuple, optional
        Time range for the output
    timezone : str, default 'UTC'
        Timezone for the output
    
    Returns
    -------
    pd.DataFrame
        Time series with MultiIndex columns (station_id, metric)
    """
    stations = df[station_col].unique()
    
    results = {}
    for station in stations:
        station_df = df[df[station_col] == station]
        ts = generate_ev_timeseries(
            station_df,
            freq=freq,
            start_time_col=start_time_col,
            end_time_col=end_time_col,
            energy_col=energy_col,
            time_range=time_range,
            timezone=timezone
        )
        results[station] = ts
    
    # Combine into MultiIndex DataFrame
    combined = pd.concat(results, axis=1, names=['station_id', 'metric'])
    
    return combined


def generate_ev_timeseries(
    df: pd.DataFrame,
    freq: str = '1h',
    start_time_col: str = 'start_time',
    end_time_col: str = 'end_time_connection',
    energy_col: str = 'energy (kWh)',
    time_range: Optional[Tuple[pd.Timestamp, pd.Timestamp]] = None,
    timezone: str = 'UTC'
) -> pd.DataFrame:
    """
    Generate time series of total EV presence and energy consumed from charging session data.
    
    This function creates a time series DataFrame containing:
    - 'presence': Total number of EVs connected at each time step
    - 'energy_consumed': Energy consumed during each time step (assuming constant charging rates)
    
    All internal calculations are done in UTC to avoid DST issues, then converted
    to the requested timezone at the end.
    
    Parameters
    ----------
    df : pd.DataFrame
        DataFrame containing charging session data
    freq : str, default '1h'
        Frequency of the output time series (e.g., '15min', '30min', '1h')
    start_time_col : str, default 'start_time'
        Name of the column containing session start times
    end_time_col : str, default 'end_time_connection'
        Name of the column containing session end times
    energy_col : str, default 'energy (kWh)'
        Name of the column containing total energy consumed per session
    time_range : tuple of (start, end) timestamps, optional
        If provided, limits the output to this time range.
    timezone : str, default 'UTC'
        Timezone for the output time series
    
    Returns
    -------
    pd.DataFrame
        Time series DataFrame with DatetimeIndex and columns:
        - 'presence': Number of EVs connected at each time step
        - 'energy_consumed': Energy consumed (kWh) during each time step
    """
    sessions = df.copy()
    
    # Convert time columns to UTC (all calculations done in UTC to avoid DST issues)
    sessions[start_time_col] = pd.to_datetime(sessions[start_time_col], utc=True)
    sessions[end_time_col] = pd.to_datetime(sessions[end_time_col], utc=True)
    
    # Handle invalid sessions
    invalid_mask = sessions[end_time_col] <= sessions[start_time_col]
    sessions = sessions[~invalid_mask].copy()
    
    if len(sessions) == 0:
        raise ValueError("No valid sessions after filtering")
    
    # Calculate charging rate
    sessions['duration_hours'] = (
        sessions[end_time_col] - sessions[start_time_col]
    ).dt.total_seconds() / 3600
    sessions['charging_rate_kw'] = sessions[energy_col] / sessions['duration_hours']
    
    # Determine time range (in UTC)
    if time_range is None:
        ts_start = sessions[start_time_col].min().floor(freq)
        ts_end = sessions[end_time_col].max().ceil(freq)
    else:
        # Convert time_range to UTC if needed
        ts_start = pd.Timestamp(time_range[0]).tz_convert('UTC') if pd.Timestamp(time_range[0]).tz is not None else pd.Timestamp(time_range[0], tz='UTC')
        ts_end = pd.Timestamp(time_range[1]).tz_convert('UTC') if pd.Timestamp(time_range[1]).tz is not None else pd.Timestamp(time_range[1], tz='UTC')
    
    # Create time index in UTC (no DST issues)
    time_index_utc = pd.date_range(start=ts_start, end=ts_end, freq=freq, tz='UTC')
    freq_td = pd.Timedelta(freq)
    
    # Initialize output arrays
    presence = np.zeros(len(time_index_utc))
    energy_consumed = np.zeros(len(time_index_utc))
    
    # Convert to numpy for faster processing
    start_times = sessions[start_time_col].values
    end_times = sessions[end_time_col].values
    charging_rates = sessions['charging_rate_kw'].values
    
    # For each time step, calculate presence and energy
    for i, t in enumerate(time_index_utc):
        t_np = t.to_numpy()
        t_next_np = (t + freq_td).to_numpy()
        
        # Find sessions that overlap with this time interval [t, t+freq)
        # A session overlaps if: start < t+freq AND end > t
        active_mask = (start_times < t_next_np) & (end_times > t_np)
        
        # Count presence (number of active sessions)
        presence[i] = active_mask.sum()
        
        # Calculate energy consumed in this interval
        if active_mask.any():
            active_starts = start_times[active_mask]
            active_ends = end_times[active_mask]
            active_rates = charging_rates[active_mask]
            
            # Overlap start = max(session_start, interval_start)
            overlap_starts = np.maximum(active_starts, t_np)
            # Overlap end = min(session_end, interval_end)
            overlap_ends = np.minimum(active_ends, t_next_np)
            
            # Duration of overlap in hours
            overlap_hours = (overlap_ends - overlap_starts).astype('timedelta64[s]').astype(float) / 3600
            overlap_hours = np.maximum(overlap_hours, 0)
            
            # Energy = rate * overlap_duration
            energy_consumed[i] = (active_rates * overlap_hours).sum()
    
    # Create result DataFrame
    result = pd.DataFrame({
        'presence': presence.astype(int),
        'energy_consumed': energy_consumed
    }, index=time_index_utc)
    
    result.index.name = 'time'
    
    # Convert index to requested timezone (safe conversion from UTC)
    if timezone != 'UTC':
        result.index = result.index.tz_convert(timezone)
    
    return result

def build_station_connection_dists(df: pd.DataFrame, tz: str = "Europe/Zurich") -> dict[str, pd.DataFrame]:
    """
    Returns a dict: {station_id -> DataFrame}
      - index: dates (one row per day with at least one record for that station)
      - columns: 0..23 (ALWAYS 24 columns)
      - values: number of session starts at that station on that date-hour (zeros where absent)
    """
    s = df.copy()
    s["start_time"] = pd.to_datetime(s["start_time"], errors="coerce", utc=True)
    s = s.dropna(subset=["start_time", "station_id"])
    s["local_dt"] = s["start_time"].dt.tz_convert(tz)
    s["date"] = s["local_dt"].dt.date
    s["hour"] = s["local_dt"].dt.hour

    # Count starts per (station, date, hour)
    grp = s.groupby(["station_id", "date", "hour"]).size().rename("starts")

    out: dict[str, pd.DataFrame] = {}
    for sid, sub in grp.groupby(level=0):
        # pivot -> rows: date, cols: hour
        pivot = (
            sub.droplevel(0)
               .unstack("hour")                 # some hours may be missing
               .reindex(columns=range(24))      # ensure 0..23 present
               .fillna(0)                       # <- crucial before astype
        )
        # make sure integer dtype; handle empty/edge cases safely
        if not pivot.empty:
            pivot = pivot.astype("int64", copy=False)
        else:
            pivot = pd.DataFrame(index=pd.Index([], name="date"),
                                 columns=range(24), dtype="int64")
        # enforce column order
        pivot = pivot.loc[:, list(range(24))]
        out[str(sid)] = pivot

    # Optional: ensure all seen stations appear (even those with zero rows)
    for sid in s["station_id"].astype(str).unique():
        if sid not in out:
            out[sid] = pd.DataFrame(index=pd.Index([], name="date"),
                                    columns=range(24), dtype="int64")

    return out

if __name__ == '__main__':
    import matplotlib.pyplot as plt
    from pathlib import Path
    
    # Load the dataset - resolve path relative to this script
    script_dir = Path(__file__).parent
    data_path = script_dir / '../../datasets/AIC/capriasca_dataset_matched_occupancy_power.csv'
    data_path = data_path.resolve()
    df = pd.read_csv(str(data_path))

    # Generate time series at 1-hour resolution
    print("\nGenerating time series (1h resolution)...")
    ts_1h = generate_ev_timeseries(
        df,
        freq='1h',
        timezone='Europe/Zurich'
    )

    ts_1h_bs = generate_ev_timeseries_by_station(
        df,
        freq='1h',
        timezone='Europe/Zurich'
    )


    # Generate time series at 15-minute resolution
    print("\nGenerating time series (15min resolution)...")
    ts_15min = generate_ev_timeseries(
        df,
        freq='15min',
        timezone='Europe/Zurich'
    )

    # Create plots
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=False)
    
    # Plot 1: Presence over full period (1h)
    ax1 = axes[0]
    ax1.plot(ts_1h.index, ts_1h['presence'], color='steelblue', linewidth=0.8)
    ax1.fill_between(ts_1h.index, 0, ts_1h['presence'], alpha=0.3, color='steelblue')
    ax1.set_ylabel('Number of EVs Connected')
    ax1.set_title('EV Presence Over Time (1h resolution)')
    ax1.grid(True, alpha=0.3)
    ax1.set_xlim(ts_1h.index.min(), ts_1h.index.max())
    
    # Plot 2: Energy consumption over full period (1h)
    ax2 = axes[1]
    ax2.plot(ts_1h.index, ts_1h['energy_consumed'], color='darkorange', linewidth=0.8)
    ax2.fill_between(ts_1h.index, 0, ts_1h['energy_consumed'], alpha=0.3, color='darkorange')
    ax2.set_ylabel('Energy Consumed (kWh)')
    ax2.set_title('Energy Consumption Over Time (1h resolution)')
    ax2.grid(True, alpha=0.3)
    ax2.set_xlim(ts_1h.index.min(), ts_1h.index.max())
    
    # Plot 3: Zoomed view of one week (15min resolution)
    ax3 = axes[2]
    # Select one week of data for the zoomed view
    week_start = ts_15min.index.min() + pd.Timedelta(days=7)
    week_end = week_start + pd.Timedelta(days=7)
    ts_week = ts_15min.loc[week_start:week_end]
    
    ax3_twin = ax3.twinx()
    line1, = ax3.plot(ts_week.index, ts_week['presence'], color='steelblue', linewidth=1.2, label='Presence')
    line2, = ax3_twin.plot(ts_week.index, ts_week['energy_consumed'], color='darkorange', linewidth=1.2, label='Energy')
    
    ax3.set_ylabel('Number of EVs Connected', color='steelblue')
    ax3_twin.set_ylabel('Energy Consumed (kWh)', color='darkorange')
    ax3.set_title(f'EV Presence and Energy (15min resolution) - Week of {week_start.strftime("%Y-%m-%d")}')
    ax3.grid(True, alpha=0.3)
    ax3.tick_params(axis='y', labelcolor='steelblue')
    ax3_twin.tick_params(axis='y', labelcolor='darkorange')
    
    # Add legend
    lines = [line1, line2]
    labels = [l.get_label() for l in lines]
    ax3.legend(lines, labels, loc='upper right')
    
    plt.tight_layout()
    output_path = script_dir / 'ev_timeseries_plot.png'
    plt.savefig(str(output_path), dpi=150, bbox_inches='tight')
    print(f"\nPlot saved to '{output_path}'")
    plt.show()
    
    # Additional statistics
    print("\n" + "="*60)
    print("STATISTICS")
    print("="*60)
    print(f"\nTotal energy consumed: {ts_1h['energy_consumed'].sum():.2f} kWh")
    print(f"Max simultaneous EVs: {ts_1h['presence'].max()}")
    print(f"Average EVs connected: {ts_1h['presence'].mean():.2f}")
    print(f"Max hourly energy: {ts_1h['energy_consumed'].max():.2f} kWh")
    print(f"Average hourly energy: {ts_1h['energy_consumed'].mean():.2f} kWh")
    
    # Daily patterns
    ts_1h_local = ts_1h.copy()
    ts_1h_local['hour'] = ts_1h_local.index.hour
    ts_1h_local['dayofweek'] = ts_1h_local.index.dayofweek
    
    print("\n--- Average Presence by Hour of Day ---")
    hourly_avg = ts_1h_local.groupby('hour')['presence'].mean()
    print(hourly_avg.round(2))
    
    print("\n--- Average Presence by Day of Week ---")
    daily_avg = ts_1h_local.groupby('dayofweek')['presence'].mean()
    daily_avg.index = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    print(daily_avg.round(2))


