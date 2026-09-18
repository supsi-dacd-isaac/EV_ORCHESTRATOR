"""Unit tests for Phase 4B site load forecasters (demand / generation / net).

Run (from the repo root, with the project venv):
    $env:PYTHONPATH="."; venv\\Scripts\\python.exe -m unittest tests.test_site_load_forecast -v
"""

from __future__ import annotations

import types
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.load_forecast import load_forecaster_service as lfs
from app.services.load_forecast.load_forecaster_service import (
    SiteForecastError,
    features_need_any_site_load,
    features_need_demand,
    features_need_net,
    fetch_site_load_for_features,
)
from app.services.orchestrator.constants import BASELOAD_FORECAST_HORIZON_STEPS


def _points(n: int, base: float = 100.0) -> list[dict]:
    start = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        ts = start.replace(minute=(i * 15) % 60, hour=12 + (i * 15) // 60)
        out.append({"timestamp": ts.isoformat(), "forecast": base + i})
    return out


class FeatureNeedTests(unittest.TestCase):
    def test_net_features_trigger_fetch(self):
        self.assertTrue(features_need_net(["fully_charged", "net_demand_forecast"]))
        self.assertTrue(features_need_any_site_load(["load_level_relative"]))
        self.assertFalse(features_need_any_site_load(["fully_charged", "time_curr_sin"]))

    def test_demand_only(self):
        self.assertTrue(features_need_demand(["demand_forecast"]))
        self.assertFalse(features_need_net(["demand_forecast"]))


class ParseAndTemplateTests(unittest.TestCase):
    def test_fill_start_time_placeholder(self):
        body = {"site": "AIC", "start_time": "{{start_time}}", "meter": "m1"}
        filled = lfs._fill_body_template(
            body, datetime(2026, 9, 14, 10, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(filled["start_time"], "2026-09-14T10:30:00+00:00")
        self.assertEqual(filled["site"], "AIC")

    def test_fill_start_time_floors_to_15_minute_grid(self):
        body = {"start_time": "{{start_time}}"}
        # 10:54 → previous step 10:45 (API only accepts :00/:15/:30/:45)
        filled = lfs._fill_body_template(
            body, datetime(2026, 9, 16, 10, 54, 50, tzinfo=timezone.utc)
        )
        self.assertEqual(filled["start_time"], "2026-09-16T10:45:00+00:00")

    def test_floor_to_forecast_step(self):
        raw = datetime(2026, 9, 16, 10, 54, 50, tzinfo=timezone.utc)
        self.assertEqual(
            lfs.floor_to_forecast_step(raw).isoformat(),
            "2026-09-16T10:45:00+00:00",
        )
        on_grid = datetime(2026, 9, 16, 10, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(
            lfs.floor_to_forecast_step(on_grid).isoformat(),
            "2026-09-16T10:15:00+00:00",
        )

    def test_parse_aic_style_response(self):
        payload = {"demand_forecast": _points(BASELOAD_FORECAST_HORIZON_STEPS, 50.0)}
        values, timestamps = lfs._parse_forecast_list(
            payload,
            response_path="demand_forecast",
            value_field="forecast",
            time_field="timestamp",
            horizon=BASELOAD_FORECAST_HORIZON_STEPS,
        )
        self.assertEqual(len(values), BASELOAD_FORECAST_HORIZON_STEPS)
        self.assertAlmostEqual(values[0], 50.0)
        self.assertEqual(timestamps[0].year, 2026)

    def test_parse_findhorn_style_nested_ok(self):
        # Same shape as AIC for the fields we care about.
        payload = {"demand_forecast": _points(BASELOAD_FORECAST_HORIZON_STEPS, 10.0)}
        values, _ = lfs._parse_forecast_list(
            payload,
            response_path="demand_forecast",
            value_field="forecast",
            time_field="timestamp",
            horizon=BASELOAD_FORECAST_HORIZON_STEPS,
        )
        self.assertEqual(len(values), 24)

    def test_too_short_raises(self):
        payload = {"demand_forecast": _points(5)}
        with self.assertRaises(SiteForecastError):
            lfs._parse_forecast_list(
                payload,
                response_path="demand_forecast",
                value_field="forecast",
                time_field="timestamp",
                horizon=BASELOAD_FORECAST_HORIZON_STEPS,
            )

    def test_meter_list_expands(self):
        bodies = lfs._meter_variants({"meter": ["a", "b"], "site": "AIC"})
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0]["meter"], "a")
        self.assertEqual(bodies[1]["meter"], "b")

    def test_sum_meter_series(self):
        ts = [datetime(2026, 1, 1, tzinfo=timezone.utc)] * 3
        summed, out_ts = lfs._sum_series([([1.0, 2.0, 3.0], ts), ([10.0, 20.0, 30.0], ts)])
        self.assertEqual(summed, [11.0, 22.0, 33.0])
        self.assertEqual(out_ts, ts)


class FetchSiteLoadTests(unittest.TestCase):
    def setUp(self):
        self.pilot_id = uuid4()
        self.event_time = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
        self.pilot = types.SimpleNamespace(
            id=self.pilot_id,
            data_sources={
                "demand_api": {
                    "type": "rest_api",
                    "base_url": "https://example.com/demand_forecaster",
                    "auth_secret": "demand_key",
                    "auth": {"type": "header", "name": "X-API-Key"},
                }
            },
            policy_signals={
                "demand_forecaster": {
                    "enabled": True,
                    "source": "demand_api",
                    "path": "/forecast/example",
                    "body": {
                        "site": "AIC",
                        "meter": "example_meter",
                        "start_time": "{{start_time}}",
                    },
                    "response_path": "demand_forecast",
                    "value_field": "forecast",
                    "time_field": "timestamp",
                },
                "generation_forecaster": {"enabled": False},
            },
        )

    def _db_returning_pilot(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = self.pilot
        return db

    def test_no_fetch_when_features_do_not_need_load(self):
        bundle = fetch_site_load_for_features(
            MagicMock(), self.pilot_id, self.event_time, ["fully_charged"]
        )
        self.assertIsNone(bundle.net)
        self.assertIsNone(bundle.demand)

    @patch("app.services.load_forecast.load_forecaster_service.post_json")
    @patch("app.services.load_forecast.load_forecaster_service.resolve_connection_token")
    def test_aic_net_equals_demand_when_gen_disabled(self, mock_token, mock_post):
        mock_token.return_value = "secret-key"
        mock_post.return_value = {
            "demand_forecast": _points(BASELOAD_FORECAST_HORIZON_STEPS, 40.0)
        }
        bundle = fetch_site_load_for_features(
            self._db_returning_pilot(),
            self.pilot_id,
            self.event_time,
            ["net_demand_forecast"],
        )
        self.assertIsNotNone(bundle.net)
        self.assertEqual(bundle.net.values_kw, bundle.demand.values_kw)
        self.assertTrue(any("generation_forecaster" in w for w in bundle.warnings))
        # Raw curve is snapshotted into meta for actions.decision_context.
        self.assertEqual(bundle.meta["demand"]["values_kw"], bundle.demand.values_kw)
        self.assertEqual(len(bundle.meta["demand"]["timestamps"]), 24)
        self.assertEqual(bundle.meta["net"]["values_kw"], bundle.net.values_kw)
        self.assertEqual(bundle.meta["net"]["mode"], "demand_only")
        mock_post.assert_called_once()
        body = mock_post.call_args.kwargs["body"]
        self.assertEqual(body["site"], "AIC")
        self.assertIn("start_time", body)
        self.assertNotIn("{{", body["start_time"])

    @patch("app.services.load_forecast.load_forecaster_service.post_json")
    @patch("app.services.load_forecast.load_forecaster_service.resolve_connection_token")
    def test_meter_list_summed(self, mock_token, mock_post):
        mock_token.return_value = "secret-key"
        self.pilot.policy_signals["demand_forecaster"]["body"]["meter"] = ["m1", "m2"]
        mock_post.side_effect = [
            {"demand_forecast": _points(BASELOAD_FORECAST_HORIZON_STEPS, 1.0)},
            {"demand_forecast": _points(BASELOAD_FORECAST_HORIZON_STEPS, 10.0)},
        ]
        bundle = fetch_site_load_for_features(
            self._db_returning_pilot(),
            self.pilot_id,
            self.event_time,
            ["demand_forecast"],
        )
        self.assertEqual(mock_post.call_count, 2)
        self.assertAlmostEqual(bundle.demand.values_kw[0], 11.0)
        self.assertIsNone(bundle.net)  # net not requested

    @patch("app.services.load_forecast.load_forecaster_service.post_json")
    @patch("app.services.load_forecast.load_forecaster_service.resolve_connection_token")
    def test_findhorn_path_body(self, mock_token, mock_post):
        mock_token.return_value = "secret-key"
        self.pilot.policy_signals["demand_forecaster"].update(
            {
                "path": "/forecast/findhorn",
                "body": {"location": "Findhorn", "start_time": "{{start_time}}"},
            }
        )
        # drop meter key entirely
        mock_post.return_value = {
            "demand_forecast": _points(BASELOAD_FORECAST_HORIZON_STEPS, 5.0)
        }
        bundle = fetch_site_load_for_features(
            self._db_returning_pilot(),
            self.pilot_id,
            self.event_time,
            ["net_demand_forecast"],
        )
        self.assertAlmostEqual(bundle.net.values_kw[0], 5.0)
        self.assertEqual(mock_post.call_args.kwargs["path"], "/forecast/findhorn")
        self.assertEqual(mock_post.call_args.kwargs["body"]["location"], "Findhorn")

    def test_missing_demand_config_raises(self):
        self.pilot.policy_signals = {}
        with self.assertRaises(SiteForecastError):
            fetch_site_load_for_features(
                self._db_returning_pilot(),
                self.pilot_id,
                self.event_time,
                ["net_demand_forecast"],
            )

    @patch("app.services.load_forecast.load_forecaster_service.resolve_connection_token")
    def test_missing_auth_secret_raises(self, mock_token):
        mock_token.return_value = None
        with self.assertRaises(SiteForecastError) as ctx:
            fetch_site_load_for_features(
                self._db_returning_pilot(),
                self.pilot_id,
                self.event_time,
                ["net_demand_forecast"],
            )
        self.assertIn("token", str(ctx.exception).lower())


class AuthSecretResolutionTests(unittest.TestCase):
    def test_resolve_uses_auth_secret(self):
        from app.services.orchestrator.signals import data_sources as ds

        calls = {}

        def fake_get(_db, pilot_id, name):
            calls["name"] = name
            return "the-key"

        with patch.object(ds, "get_pilot_secret_value", side_effect=fake_get):
            token = ds.resolve_connection_token(
                MagicMock(),
                types.SimpleNamespace(id=uuid4()),
                {"type": "rest_api", "auth_secret": "example_demand_api_key"},
            )
        self.assertEqual(token, "the-key")
        self.assertEqual(calls["name"], "example_demand_api_key")


if __name__ == "__main__":
    unittest.main()
