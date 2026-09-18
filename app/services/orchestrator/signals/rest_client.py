"""Generic REST JSON client for pilot data_sources type ``rest_api``.

Used by demand / generation forecasters. Auth is either an HTTP header
(``auth.type == "header"``) or a field merged into the JSON body
(``auth.type == "body"``). Same SSRF hardening as the Influx client:
only http(s), no private hosts, no redirects.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60.0
ALLOWED_URL_SCHEMES = {"http", "https"}


class RestClientError(RuntimeError):
    """Outbound REST call failed (config, network, HTTP, or unsafe URL)."""


def _validate_url(url: Optional[str]) -> None:
    if not url or not isinstance(url, str):
        raise RestClientError("data source base_url is missing")
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_URL_SCHEMES:
        raise RestClientError(
            f"unsupported url scheme {parsed.scheme!r} (allowed: http, https)"
        )
    host = parsed.hostname
    if not host:
        raise RestClientError("data source base_url has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise RestClientError(f"cannot resolve host {host!r}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise RestClientError(f"host {host!r} resolves to a blocked address ({ip})")


def _join_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if not path:
        return base
    return base + (path if path.startswith("/") else "/" + path)


def post_json(
    *,
    base_url: str,
    path: str,
    body: Dict[str, Any],
    token: Optional[str],
    auth: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: Optional[httpx.Client] = None,
) -> Any:
    """POST JSON and return the parsed response body.

    ``auth`` example shapes::

        {"type": "header", "name": "X-API-Key"}
        {"type": "body", "name": "api_key"}
    """
    payload = dict(body)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    auth = auth or {}
    auth_type = (auth.get("type") or "header").lower()
    auth_name = auth.get("name") or "X-API-Key"

    if token:
        if auth_type == "body":
            payload[auth_name] = token
        elif auth_type == "header":
            headers[auth_name] = token
        else:
            raise RestClientError(f"unsupported auth.type {auth_type!r} (header|body)")

    owns_client = client is None
    if owns_client:
        _validate_url(base_url)
        client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        resp = client.post(_join_url(base_url, path), json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise RestClientError(f"REST request failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()


def check_rest_reachable(
    *,
    base_url: str,
    token: Optional[str],
    auth: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Lightweight reachability check for a rest_api data source.

    Issues a GET against ``base_url`` with the configured auth. Treats any HTTP
    response (including 401/404) as success — we only fail on transport/SSRF
    errors. Never returns the token.
    """
    headers = {"Accept": "application/json"}
    auth = auth or {}
    auth_type = (auth.get("type") or "header").lower()
    auth_name = auth.get("name") or "X-API-Key"
    if token and auth_type == "header":
        headers[auth_name] = token

    _validate_url(base_url)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            client.get(base_url.rstrip("/") + "/", headers=headers)
    except httpx.HTTPError as exc:
        raise RestClientError(f"REST reachability check failed: {exc}") from exc
