"""
Phase 4 checks for flexible ANN observation features (CHANGE 3).

Lives under tests/ (not app/), so Docker image builds that only COPY app/ and
models/ do not include these files. Kept in git on purpose — they document the
sidecar contract.

Run (from repo root, with PYTHONPATH set and venv active):
  python -m unittest tests.test_observation_features -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.services.orchestrator.obs_layout import (
    DEFAULT_OBSERVATION_FEATURES,
    OBSERVATION_SIZE,
    FEATURES,
    list_observation_features,
    observation_dim,
    validate_feature_names,
)
from app.services.orchestrator.observation_builder import (
    prepare_observation,
    select_observation_from_default,
)
from app.services.orchestrator.policy.ann_policy import (
    read_observation_features,
    require_ann_sidecar_config,
    sidecar_path_for,
)


def _sample_prepare_kwargs():
    return dict(
        charger_id="00000000-0000-0000-0000-000000000001",
        is_fully_charged=False,
        time_connection=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        time_current=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        forecasted_duration_hours=4.0,
        forecasted_energy_kwh=20.0,
        current_power_kw=11.0,
        forecasted_energy_kwh_std=2.0,
        forecasted_duration_hours_std=0.5,
        energy_delivered_kwh=5.0,
        net_demand_kw=[10.0 + 0.1 * i for i in range(24)],
        controlled_charging_points=3,
        probability_disconnection=0.1,
        cumulative_duration_probability=0.2,
    )


class ObservationCatalogTests(unittest.TestCase):
    def test_default_observation_size_is_44(self):
        self.assertEqual(OBSERVATION_SIZE, 44)
        self.assertEqual(observation_dim(DEFAULT_OBSERVATION_FEATURES), 44)
        self.assertEqual(
            sum(FEATURES[n].size for n in DEFAULT_OBSERVATION_FEATURES),
            44,
        )
        self.assertIn("net_demand_forecast", DEFAULT_OBSERVATION_FEATURES)
        self.assertNotIn("community_load", FEATURES)

    def test_prepare_observation_default_shape(self):
        obs = prepare_observation(**_sample_prepare_kwargs())
        self.assertEqual(obs.shape, (44,))
        self.assertEqual(obs.ndim, 1)

    def test_select_subset_from_default_observation(self):
        full = prepare_observation(**_sample_prepare_kwargs())
        names = [
            "fully_charged",
            "time_curr_sin",
            "time_curr_cos",
            "connected_time_relative",
            "energy_charged_rel_needed",
            "net_demand_forecast",
        ]
        subset = select_observation_from_default(full, names)
        self.assertEqual(subset.shape, (observation_dim(names),))
        self.assertEqual(subset.shape, (29,))

    def test_extra_forecast_features_not_in_default_layout(self):
        by_name = {r["name"]: r for r in list_observation_features()}
        self.assertFalse(by_name["demand_forecast"]["in_default_layout"])
        self.assertFalse(by_name["generation_forecast"]["in_default_layout"])
        self.assertTrue(by_name["net_demand_forecast"]["in_default_layout"])

    def test_unknown_feature_name_rejected(self):
        problems = validate_feature_names(["fully_charged", "not_a_real_feature"])
        self.assertTrue(any("Unknown" in p for p in problems))

        full = np.zeros(OBSERVATION_SIZE, dtype=np.float32)
        with self.assertRaises(ValueError):
            select_observation_from_default(full, ["not_a_real_feature"])

    def test_wrong_full_obs_length_rejected(self):
        with self.assertRaisesRegex(ValueError, "Default observation has shape"):
            select_observation_from_default(
                np.zeros(10, dtype=np.float32),
                ["fully_charged"],
            )

    def test_sidecar_missing_observation_features(self):
        names, problems = read_observation_features(
            {"power_levels_kw": [0.0, 11.0]}
        )
        self.assertIsNone(names)
        self.assertTrue(problems)
        self.assertTrue(any("observation_features" in p for p in problems))

    def test_sidecar_valid_observation_features(self):
        names, problems = read_observation_features(
            {"observation_features": list(DEFAULT_OBSERVATION_FEATURES)}
        )
        self.assertEqual(problems, [])
        self.assertEqual(names, list(DEFAULT_OBSERVATION_FEATURES))
        self.assertEqual(observation_dim(names), 44)

    def test_list_observation_features_catalog(self):
        rows = list_observation_features()
        self.assertEqual(len(rows), len(FEATURES))
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["net_demand_forecast"]["size"], 24)
        self.assertTrue(by_name["fully_charged"]["in_default_layout"])


class AnnSidecarIntegrationTests(unittest.TestCase):
    """Needs models/actor files on disk (host folder / compose bind mount)."""

    def test_legacy_default_ann_sidecar_is_usable(self):
        from app.config import ACTOR_MODEL_PATH

        model_path = Path(ACTOR_MODEL_PATH)
        if not model_path.exists():
            self.skipTest(f"Default ANN model not found: {model_path}")

        sidecar = sidecar_path_for(model_path)
        if not sidecar.exists():
            self.skipTest(f"Default ANN sidecar not found: {sidecar}")

        features, power_levels = require_ann_sidecar_config(model_path)
        self.assertEqual(observation_dim(features), 44)
        self.assertIsNone(power_levels)  # binary legacy model
        self.assertIn("net_demand_forecast", features)

    def test_ann_policy_rejects_feature_dim_mismatch(self):
        """Hard-fail when sidecar feature length != network input dim."""
        from app.config import ACTOR_MODEL_PATH
        from app.services.orchestrator.policy.ann_policy import AnnPolicy

        model_path = Path(ACTOR_MODEL_PATH)
        if not model_path.exists():
            self.skipTest(f"Default ANN model not found: {model_path}")

        tiny = ["fully_charged", "net_demand_forecast"]
        self.assertNotEqual(observation_dim(tiny), 44)

        with self.assertRaisesRegex(ValueError, "observation_dim"):
            AnnPolicy(
                model_path=str(model_path),
                observation_features=tiny,
                check_input_dim=True,
            )


if __name__ == "__main__":
    unittest.main()
