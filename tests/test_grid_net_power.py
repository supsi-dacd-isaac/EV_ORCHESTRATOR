"""Unit tests for Phase 4A measured grid net-power stats.

Run (from the repo root, with the project venv):
    $env:PYTHONPATH="."; venv\\Scripts\\python.exe -m unittest tests.test_grid_net_power -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.services.orchestrator.constants import GRID_NET_POWER_NORM_EPS_KW
from app.services.orchestrator.observation_builder import _normalize_grid_net_power_kw
from app.services.orchestrator.signals import grid_net_power as gnp
from app.services.orchestrator.signals.grid_net_power import (
    GridNetPowerError,
    bin_to_grid,
    build_net_series_kw,
    compute_stats_kw,
    features_need_grid_net_power,
    floor_to_grid,
)


class FeatureGateTests(unittest.TestCase):
    def test_needs_any_of_three(self):
        self.assertTrue(features_need_grid_net_power(["fully_charged", "grid_net_power_q40_month"]))
        self.assertFalse(features_need_grid_net_power(["fully_charged", "net_demand_forecast"]))


class NetSeriesTests(unittest.TestCase):
    def test_import_minus_export(self):
        t0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 9, 1, 12, 15, tzinfo=timezone.utc)
        imports = [{t0: 100.0, t1: 80.0}]
        exports = [{t0: 10.0, t1: 20.0}]
        net = build_net_series_kw(imports, exports)
        self.assertEqual(net, [90.0, 60.0])

    def test_empty_export_is_sum_imports(self):
        t0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        imports = [{t0: 40.0}, {t0: 10.0}]
        net = build_net_series_kw(imports, [])
        self.assertEqual(net, [50.0])

    def test_misaligned_sensors_are_binned_then_summed(self):
        # A at :00, B at :05 — do NOT treat those as two net samples with zeros.
        import_a = {
            datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc): 100.0,
            datetime(2026, 9, 1, 10, 15, tzinfo=timezone.utc): 90.0,
            datetime(2026, 9, 1, 10, 30, tzinfo=timezone.utc): 80.0,
            datetime(2026, 9, 1, 10, 45, tzinfo=timezone.utc): 70.0,
        }
        import_b = {
            datetime(2026, 9, 1, 10, 5, tzinfo=timezone.utc): 10.0,
            datetime(2026, 9, 1, 10, 20, tzinfo=timezone.utc): 90.0,
            datetime(2026, 9, 1, 10, 35, tzinfo=timezone.utc): 60.0,
            datetime(2026, 9, 1, 10, 50, tzinfo=timezone.utc): 80.0,
        }
        net = build_net_series_kw([import_a, import_b], [], every_seconds=15 * 60)
        self.assertEqual(net, [110.0, 180.0, 140.0, 150.0])

    def test_bin_to_grid_averages_points_in_same_box(self):
        series = {
            datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc): 100.0,
            datetime(2026, 9, 1, 10, 5, tzinfo=timezone.utc): 80.0,
        }
        binned = bin_to_grid(series, 15 * 60)
        box = floor_to_grid(
            datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc), 15 * 60
        )
        self.assertEqual(list(binned.keys()), [box])
        self.assertAlmostEqual(binned[box], 90.0)


class StatsTests(unittest.TestCase):
    def test_max_and_quantiles(self):
        # 0..100 step 10 → max 100, q40=40, q70=70
        series = [float(i) for i in range(0, 101, 10)]
        mx, q40, q70 = compute_stats_kw(series)
        self.assertAlmostEqual(mx, 100.0)
        self.assertAlmostEqual(q40, 40.0)
        self.assertAlmostEqual(q70, 70.0)

    def test_empty_raises(self):
        with self.assertRaises(GridNetPowerError):
            compute_stats_kw([])


class MonthWindowTests(unittest.TestCase):
    def test_pilot_timezone_month_start(self):
        # Event in Europe/Zurich afternoon on 15 Sep → month start local midnight
        at = datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc)
        start, stop = gnp._month_window_utc(at, "Europe/Zurich")
        self.assertEqual(stop, at)
        # Zurich is UTC+2 in September → local midnight 1 Sep = 2026-08-31 22:00 UTC
        self.assertEqual(start.isoformat(), "2026-08-31T22:00:00+00:00")


class NormalizeTests(unittest.TestCase):
    def test_uses_current_power_when_positive(self):
        # 100 kW / (2 points * 10 kW) = 5
        v = _normalize_grid_net_power_kw(
            100.0,
            controlled_charging_points=2,
            current_power_kw=10.0,
            nominal_power_kw=22.0,
        )
        self.assertAlmostEqual(v, 5.0)

    def test_falls_back_to_nominal(self):
        v = _normalize_grid_net_power_kw(
            44.0,
            controlled_charging_points=2,
            current_power_kw=0.0,
            nominal_power_kw=22.0,
        )
        self.assertAlmostEqual(v, 1.0)

    def test_eps_when_both_rates_zero(self):
        v = _normalize_grid_net_power_kw(
            1.0,
            controlled_charging_points=3,
            current_power_kw=0.0,
            nominal_power_kw=0.0,
        )
        self.assertAlmostEqual(v, 1.0 / GRID_NET_POWER_NORM_EPS_KW)


class CatalogTests(unittest.TestCase):
    def test_features_in_catalog_not_default(self):
        from app.services.orchestrator.obs_layout import (
            DEFAULT_OBSERVATION_FEATURES,
            FEATURES,
            list_observation_features,
        )

        for name in (
            "grid_net_power_max_month",
            "grid_net_power_q40_month",
            "grid_net_power_q70_month",
        ):
            self.assertIn(name, FEATURES)
            self.assertNotIn(name, DEFAULT_OBSERVATION_FEATURES)
        by_name = {r["name"]: r for r in list_observation_features()}
        self.assertFalse(by_name["grid_net_power_max_month"]["in_default_layout"])
        self.assertIn("import", by_name["grid_net_power_max_month"]["description"].lower())
        self.assertIn("export", by_name["grid_net_power_max_month"]["description"].lower())


if __name__ == "__main__":
    unittest.main()
