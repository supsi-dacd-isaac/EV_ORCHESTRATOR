"""Unit tests for the wind_excess / InfluxDB signal stack (Phase 2).

Run (from the repo root, with the project venv):
    $env:PYTHONPATH="."; venv\\Scripts\\python.exe -m unittest tests.test_signals_wind_excess -v

No live network or database: the Influx client is exercised through an
httpx.MockTransport, and connection resolution uses a lightweight fake pilot.
"""

from __future__ import annotations

import types
import unittest
from datetime import datetime, timezone

import httpx
from cryptography.fernet import Fernet

from app import config
from app.services.common import secrets
from app.services.common.secrets import (
    SecretDecryptError,
    SecretsConfigError,
    decrypt_secret,
    encrypt_secret,
)
from app.services.orchestrator.signals import influx_client
from app.services.orchestrator.signals.data_sources import (
    get_data_sources,
    resolve_connection,
)

SAMPLE_CSV = (
    "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string,string,string\r\n"
    "#group,false,false,true,true,false,false,true,true,true\r\n"
    "#default,_result,,,,,,,,\r\n"
    ",result,table,_start,_stop,_time,_value,_field,_measurement,sensor_id\r\n"
    ",_result,0,2026-09-10T15:00:00Z,2026-09-10T15:30:00Z,2026-09-10T15:29:00.123456789Z,12300.5,active_power,active_power,example-wind-sensor\r\n"
)


class SecretsTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.EV_SECRETS_KEY
        config.EV_SECRETS_KEY = Fernet.generate_key().decode()

    def tearDown(self):
        config.EV_SECRETS_KEY = self._orig_key

    def test_roundtrip(self):
        cipher = encrypt_secret("super-secret-token")
        self.assertNotEqual(cipher, "super-secret-token")
        self.assertEqual(decrypt_secret(cipher), "super-secret-token")

    def test_missing_key_raises(self):
        config.EV_SECRETS_KEY = ""
        with self.assertRaises(SecretsConfigError):
            encrypt_secret("x")

    def test_wrong_key_raises(self):
        cipher = encrypt_secret("x")
        config.EV_SECRETS_KEY = Fernet.generate_key().decode()  # rotate
        with self.assertRaises(SecretDecryptError):
            decrypt_secret(cipher)

    def test_secrets_enabled(self):
        self.assertTrue(secrets.secrets_enabled())
        config.EV_SECRETS_KEY = ""
        self.assertFalse(secrets.secrets_enabled())


class InfluxParseTests(unittest.TestCase):
    def test_parse_last_point(self):
        point = influx_client._parse_last_point(SAMPLE_CSV)
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.value, 12300.5)
        self.assertIsNotNone(point.time)
        self.assertEqual(point.time.year, 2026)
        self.assertEqual(point.time.minute, 29)

    def test_parse_empty(self):
        empty = (
            "#datatype,string\r\n"
            ",result\r\n"
        )
        self.assertIsNone(influx_client._parse_last_point(empty))

    def test_parse_time_variants(self):
        t = influx_client._parse_influx_time("2026-09-10T15:29:00Z")
        self.assertIsNotNone(t)
        self.assertEqual(t.utcoffset().total_seconds(), 0)
        self.assertIsNone(influx_client._parse_influx_time(""))

    def test_flux_str_escapes(self):
        self.assertEqual(influx_client._flux_str('a"b\\c'), 'a\\"b\\\\c')


class InfluxQueryTests(unittest.TestCase):
    def test_query_last_value_builds_request_and_parses(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["org"] = request.url.params.get("org")
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = request.content.decode("utf-8")
            return httpx.Response(200, text=SAMPLE_CSV)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        point = influx_client.query_last_value(
            url="https://host/influxdb/",
            org="myorg",
            bucket="mybucket",
            token="tok",
            measurement="active_power",
            sensor_id="example-wind-sensor",
            lookback_minutes=15,
            client=client,
        )
        self.assertAlmostEqual(point.value, 12300.5)
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["path"].endswith("/api/v2/query"))
        self.assertEqual(captured["org"], "myorg")
        self.assertEqual(captured["auth"], "Token tok")
        self.assertIn('r._measurement == "active_power"', captured["body"])
        self.assertIn('r.sensor_id == "example-wind-sensor"', captured["body"])
        self.assertIn("range(start: -15m)", captured["body"])
        self.assertIn("last()", captured["body"])

    def test_at_time_builds_absolute_range(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content.decode("utf-8")
            return httpx.Response(200, text=SAMPLE_CSV)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        at = datetime(2026, 9, 10, 15, 30, tzinfo=timezone.utc)
        influx_client.query_last_value(
            url="https://host/influxdb/", org="o", bucket="b", token="t",
            measurement="active_power", sensor_id="s",
            at_time=at, lookback_minutes=30, client=client,
        )
        # Window ends at the decision time, not "now".
        self.assertIn("range(start: 2026-09-10T15:00:00Z, stop: 2026-09-10T15:30:00Z)", captured["body"])

    def test_control_char_in_identifier_rejected(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SAMPLE_CSV)))
        with self.assertRaises(influx_client.InfluxQueryError):
            influx_client.query_last_value(
                url="https://host/influxdb/", org="o", bucket="b", token="t",
                measurement="active_power", sensor_id="s\n|> buckets()",
                client=client,
            )

    def test_validate_url_blocks_unsafe(self):
        with self.assertRaises(influx_client.InfluxQueryError):
            influx_client._validate_url(None)
        with self.assertRaises(influx_client.InfluxQueryError):
            influx_client._validate_url("ftp://example.com")
        with self.assertRaises(influx_client.InfluxQueryError):
            influx_client._validate_url("http://127.0.0.1:8086")

    def test_query_http_error_raises_influxqueryerror(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(influx_client.InfluxQueryError):
            influx_client.query_last_value(
                url="https://host/influxdb/",
                org="o", bucket="b", token="bad",
                measurement="m", sensor_id="s",
                client=client,
            )

    def test_check_connection_ok_and_error(self):
        ok_client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="")))
        influx_client.check_connection(url="https://host/influxdb/", org="o", token="t", client=ok_client)

        bad_client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
        with self.assertRaises(influx_client.InfluxQueryError):
            influx_client.check_connection(url="https://host/influxdb/", org="o", token="t", client=bad_client)


class DataSourceResolveTests(unittest.TestCase):
    def _pilot(self, data_sources):
        return types.SimpleNamespace(data_sources=data_sources)

    def test_resolve_connection(self):
        pilot = self._pilot({"example_influx": {"type": "influxdb", "url": "u"}})
        self.assertEqual(resolve_connection(pilot, "example_influx")["url"], "u")
        self.assertIsNone(resolve_connection(pilot, "missing"))

    def test_get_data_sources_defaults(self):
        self.assertEqual(get_data_sources(None), {})
        self.assertEqual(get_data_sources(self._pilot(None)), {})
        self.assertEqual(get_data_sources(self._pilot("notadict")), {})


class DataSourceValidationTests(unittest.TestCase):
    def test_valid_influx_connection(self):
        from app.services.orchestrator.signals.data_sources import validate_data_sources

        validate_data_sources({
            "example_influx": {
                "type": "influxdb",
                "url": "https://example.com/influxdb/",
                "org": "interped",
                "bucket": "interped",
                "token_secret": "example_influx_token",
            }
        })

    def test_reject_unknown_key(self):
        from app.services.orchestrator.signals.data_sources import validate_data_sources

        with self.assertRaises(ValueError) as ctx:
            validate_data_sources({
                "c": {"type": "influxdb", "url": "https://x", "my_token": "leak"}
            })
        self.assertIn("unknown key", str(ctx.exception))
        self.assertIn("my_token", str(ctx.exception))

    def test_valid_rest_api_connection(self):
        from app.services.orchestrator.signals.data_sources import validate_data_sources

        validate_data_sources({
            "example_demand_forecaster": {
                "type": "rest_api",
                "base_url": "https://example.com/demand_forecaster",
                "auth_secret": "example_demand_api_key",
                "auth": {"type": "header", "name": "X-API-Key"},
            }
        })

    def test_reject_unknown_type(self):
        from app.services.orchestrator.signals.data_sources import validate_data_sources

        with self.assertRaises(ValueError) as ctx:
            validate_data_sources({"c": {"type": "postgres", "url": "x"}})
        self.assertIn("unknown type", str(ctx.exception))

    def test_none_is_ok(self):
        from app.services.orchestrator.signals.data_sources import validate_data_sources

        validate_data_sources(None)


class PilotReadRedactionTests(unittest.TestCase):
    def test_owner_and_admin_see_data_sources_others_do_not(self):
        from uuid import uuid4
        from app.api.routes.database import _can_see_data_sources, _pilot_read_for_user
        from app.services.common.auth import TokenData

        owner_id = uuid4()
        other_id = uuid4()
        pilot = types.SimpleNamespace(
            id=uuid4(),
            name="interped",
            id_owner=owner_id,
            timezone_name="UTC",
            policy_signals=None,
            data_sources={"example_influx": {"type": "influxdb", "url": "https://x", "token_secret": "s"}},
        )
        owner = TokenData(user="o", role="user-adv", owner_id=owner_id)
        admin = TokenData(user="a", role="admin", owner_id=uuid4())
        guest = TokenData(user="g", role="guest", owner_id=other_id)

        self.assertTrue(_can_see_data_sources(owner, pilot))
        self.assertTrue(_can_see_data_sources(admin, pilot))
        self.assertFalse(_can_see_data_sources(guest, pilot))

        self.assertIsNotNone(_pilot_read_for_user(pilot, owner).data_sources)
        self.assertIsNotNone(_pilot_read_for_user(pilot, admin).data_sources)
        self.assertIsNone(_pilot_read_for_user(pilot, guest).data_sources)


if __name__ == "__main__":
    unittest.main()
