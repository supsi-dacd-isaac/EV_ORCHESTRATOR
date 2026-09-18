"""Minimal InfluxDB v2 client (query only) built on httpx.

We avoid the official `influxdb-client` dependency: the v2 query API is a single
HTTP POST with a Flux script, and httpx is already a project dependency. This
module knows nothing about pilots or wind — it just runs a query and returns the
last value, so it is trivially unit-testable with a mocked transport.

Safety notes:
- The target URL comes from user-editable pilot config, so before making a REAL
  outbound request we validate the scheme and block private/loopback/link-local
  hosts (SSRF hardening), and we never follow redirects.
- measurement / sensor_id / bucket are interpolated into Flux; we escape quotes
  and reject control characters to prevent Flux injection.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20.0
ALLOWED_URL_SCHEMES = {"http", "https"}


class InfluxQueryError(RuntimeError):
    """The InfluxDB query failed (network, auth, HTTP status, or bad response)."""


@dataclass
class InfluxPoint:
    """A single value read from InfluxDB and its measurement time (UTC)."""
    value: float
    time: Optional[datetime]


def _flux_str(value: str) -> str:
    """Escape a string for safe interpolation into a Flux double-quoted literal."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _assert_safe_identifier(value: str, field: str) -> None:
    """Reject control characters in values interpolated into Flux (defense in depth).

    Quotes are escaped by _flux_str, but we additionally forbid CR/LF/NUL so a
    config value can never introduce newlines into the generated script.
    """
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise InfluxQueryError(f"invalid character in {field!r}")


def _validate_url(url: Optional[str]) -> None:
    """SSRF guard for a user-supplied base URL. Raises InfluxQueryError if unsafe.

    Only http/https are allowed, and the host must not resolve to a private,
    loopback, link-local, reserved, or multicast address (blocks cloud metadata
    endpoints and internal services).
    """
    if not url or not isinstance(url, str):
        raise InfluxQueryError("data source url is missing")
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_URL_SCHEMES:
        raise InfluxQueryError(f"unsupported url scheme {parsed.scheme!r} (allowed: http, https)")
    host = parsed.hostname
    if not host:
        raise InfluxQueryError("data source url has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise InfluxQueryError(f"cannot resolve host {host!r}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise InfluxQueryError(f"host {host!r} resolves to a blocked address ({ip})")


def _query_url(base_url: str) -> str:
    """Join the configured base URL with the v2 query path, tolerating a trailing '/'."""
    return base_url.rstrip("/") + "/api/v2/query"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/vnd.flux",  # body is raw Flux, not JSON
        "Accept": "application/csv",             # response is annotated CSV
    }


def _to_rfc3339(dt: datetime) -> str:
    """Format an (aware or naive-as-UTC) datetime as an RFC3339 Flux time literal."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_influx_time(raw: str) -> Optional[datetime]:
    """Parse an RFC3339 Influx timestamp into an aware datetime, or None.

    Handles the 'Z' suffix and nanosecond precision, neither of which
    datetime.fromisoformat accepts before normalization.
    """
    if not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, frac = s.split(".", 1)
        digits = ""
        rest = ""
        for i, ch in enumerate(frac):
            if ch.isdigit():
                digits += ch
            else:
                rest = frac[i:]
                break
        digits = (digits + "000000")[:6]  # nanoseconds -> microseconds
        s = f"{head}.{digits}{rest}"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        logger.warning("Could not parse Influx time %r", raw)
        return None


def _parse_last_point(csv_text: str) -> Optional[InfluxPoint]:
    """Parse annotated-CSV query output and return the latest (_time, _value)."""
    reader = csv.reader(io.StringIO(csv_text))
    header_index: Optional[dict] = None
    points: list[InfluxPoint] = []

    for row in reader:
        if not row or all(cell == "" for cell in row):
            continue
        if row[0].startswith("#"):  # annotation lines (#datatype, #group, ...)
            continue
        if header_index is None:
            header_index = {name: i for i, name in enumerate(row)}
            continue
        # data row
        v_idx = header_index.get("_value")
        if v_idx is None or v_idx >= len(row):
            continue
        try:
            value = float(row[v_idx])
        except ValueError:
            continue
        t_idx = header_index.get("_time")
        t = _parse_influx_time(row[t_idx]) if (t_idx is not None and t_idx < len(row)) else None
        points.append(InfluxPoint(value=value, time=t))

    if not points:
        return None
    timed = [p for p in points if p.time is not None]
    if timed:
        return max(timed, key=lambda p: p.time)
    return points[-1]


def _range_clause(at_time: Optional[datetime], lookback_minutes: int) -> str:
    """Build the Flux range() call.

    When at_time is given (the decision timestamp), we query the absolute window
    ending at that instant so the decision uses the wind value AT the event time
    rather than at query-execution time. Otherwise we fall back to a relative
    window ending now.
    """
    if at_time is not None:
        start = _to_rfc3339(at_time - timedelta(minutes=lookback_minutes))
        stop = _to_rfc3339(at_time)
        return f"range(start: {start}, stop: {stop})"
    return f"range(start: -{int(lookback_minutes)}m)"


def query_last_value(
    *,
    url: str,
    org: str,
    bucket: str,
    token: str,
    measurement: str,
    sensor_id: str,
    at_time: Optional[datetime] = None,
    lookback_minutes: int = 30,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: Optional[httpx.Client] = None,
) -> Optional[InfluxPoint]:
    """Return the last matching point in the window, or None if empty.

    If ``at_time`` is provided, the window ends at that timestamp (decision time);
    otherwise it ends now. Raises InfluxQueryError on any transport/HTTP/parse
    failure or unsafe URL/identifier.
    """
    _assert_safe_identifier(bucket, "bucket")
    _assert_safe_identifier(measurement, "measurement")
    _assert_safe_identifier(sensor_id, "sensor_id")
    flux = (
        f'from(bucket: "{_flux_str(bucket)}")\n'
        f"  |> {_range_clause(at_time, lookback_minutes)}\n"
        f'  |> filter(fn: (r) => r._measurement == "{_flux_str(measurement)}")\n'
        f'  |> filter(fn: (r) => r.sensor_id == "{_flux_str(sensor_id)}")\n'
        f"  |> last()"
    )
    owns_client = client is None
    if owns_client:
        _validate_url(url)  # only guard real outbound calls; injected clients are trusted (tests)
        client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        resp = client.post(_query_url(url), params={"org": org}, headers=_headers(token), content=flux)
        resp.raise_for_status()
        return _parse_last_point(resp.text)
    except httpx.HTTPError as exc:
        raise InfluxQueryError(f"InfluxDB query failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()


def _parse_series_points(csv_text: str) -> list[InfluxPoint]:
    """Parse annotated-CSV query output into all (_time, _value) points."""
    reader = csv.reader(io.StringIO(csv_text))
    header_index: Optional[dict] = None
    points: list[InfluxPoint] = []

    for row in reader:
        if not row or all(cell == "" for cell in row):
            continue
        if row[0].startswith("#"):
            continue
        if header_index is None:
            header_index = {name: i for i, name in enumerate(row)}
            continue
        v_idx = header_index.get("_value")
        if v_idx is None or v_idx >= len(row):
            continue
        try:
            value = float(row[v_idx])
        except ValueError:
            continue
        t_idx = header_index.get("_time")
        t = _parse_influx_time(row[t_idx]) if (t_idx is not None and t_idx < len(row)) else None
        if t is None:
            continue
        points.append(InfluxPoint(value=value, time=t))
    return points


def _assert_safe_aggregate_every(value: str) -> None:
    """Allow only simple Flux durations like ``15m``, ``1h``."""
    if not re.fullmatch(r"\d+[smhdw]", value):
        raise InfluxQueryError(
            f"invalid aggregate_every {value!r}; expected e.g. '1m', '15m', '1h'"
        )


def query_sensor_series(
    *,
    url: str,
    org: str,
    bucket: str,
    token: str,
    measurement: str,
    sensor_id: str,
    start: datetime,
    stop: datetime,
    aggregate_every: str = "15m",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: Optional[httpx.Client] = None,
) -> list[InfluxPoint]:
    """Return an aggregated timeseries for one sensor over ``[start, stop)``.

    Uses Flux ``aggregateWindow`` (default every 15 minutes) so multiple sensors
    share a common time grid without downloading raw high-frequency points.
    Raises InfluxQueryError on transport/HTTP/unsafe config failures. Empty
    result → ``[]``.
    """
    _assert_safe_identifier(bucket, "bucket")
    _assert_safe_identifier(measurement, "measurement")
    _assert_safe_identifier(sensor_id, "sensor_id")
    _assert_safe_aggregate_every(aggregate_every)
    start_s = _to_rfc3339(start)
    stop_s = _to_rfc3339(stop)
    flux = (
        f'from(bucket: "{_flux_str(bucket)}")\n'
        f"  |> range(start: {start_s}, stop: {stop_s})\n"
        f'  |> filter(fn: (r) => r._measurement == "{_flux_str(measurement)}")\n'
        f'  |> filter(fn: (r) => r.sensor_id == "{_flux_str(sensor_id)}")\n'
        f"  |> aggregateWindow(every: {aggregate_every}, fn: mean, createEmpty: false)\n"
        f'  |> keep(columns: ["_time", "_value"])\n'
        f'  |> sort(columns: ["_time"])'
    )
    owns_client = client is None
    if owns_client:
        _validate_url(url)
        client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        resp = client.post(
            _query_url(url), params={"org": org}, headers=_headers(token), content=flux
        )
        resp.raise_for_status()
        return _parse_series_points(resp.text)
    except httpx.HTTPError as exc:
        raise InfluxQueryError(f"InfluxDB series query failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()


def check_connection(
    *,
    url: str,
    org: str,
    token: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: Optional[httpx.Client] = None,
) -> None:
    """Validate URL/org/token by running a trivial query. Raises InfluxQueryError on failure."""
    flux = "buckets() |> limit(n: 1)"
    owns_client = client is None
    if owns_client:
        _validate_url(url)
        client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        resp = client.post(_query_url(url), params={"org": org}, headers=_headers(token), content=flux)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise InfluxQueryError(f"InfluxDB connection check failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()
