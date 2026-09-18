"""Tests for EV forecast stats update / rebuild-from-history."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.common.constants import GENERIC_CHARGER_ID
from app.services.ev_forecast import updater as forecast_updater


def _session(
    *,
    energy: float,
    duration_h: float,
    start: datetime,
    charger_id,
):
    end = start + timedelta(hours=duration_h)
    return SimpleNamespace(
        id=uuid4(),
        id_charger=charger_id,
        start_time=start,
        end_time=end,
        end_charging_time=end,
        energy_delivered_kwh=energy,
    )


class RebuildGenericForecastTests(TestCase):
    def test_missing_generic_row_rebuilds_from_all_hour_sessions(self):
        """Deleting stats then updating must recompute from remaining sessions."""
        charger_a = uuid4()
        charger_b = uuid4()
        # Both at local hour 8 in their pilots (naive-local already stored as UTC).
        # London BST: 08:00 local = 07:00 UTC; Zurich CEST: 08:00 local = 06:00 UTC.
        s1 = _session(
            energy=10.0,
            duration_h=2.0,
            start=datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc),
            charger_id=charger_a,
        )
        s2 = _session(
            energy=20.0,
            duration_h=4.0,
            start=datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc),
            charger_id=charger_b,
        )

        db = MagicMock()
        # First query in _update_or_initialize_stat: latest generic row → None
        # Second path uses _sessions_for_local_hour → query().filter().all()
        stats_query = MagicMock()
        stats_query.filter.return_value.order_by.return_value.first.return_value = None

        sessions_query = MagicMock()
        sessions_query.filter.return_value.all.return_value = [s1, s2]

        def _query(model):
            if model.__name__ == "EvForecastStats":
                return stats_query
            return sessions_query

        db.query.side_effect = _query

        added = []
        db.add.side_effect = lambda obj: added.append(obj)

        def _tz(_db, cid):
            if str(cid) == str(charger_a):
                return "Europe/London"
            return "Europe/Zurich"

        with patch.object(forecast_updater, "get_pilot_tz_for_charger", side_effect=_tz):
            forecast_updater._update_or_initialize_stat(
                db,
                charger_id=GENERIC_CHARGER_ID,
                local_hour=8,
                energy_kwh=99.0,  # ignored when history exists
                duration_hours=99.0,
                tz_name="Europe/London",
            )

        self.assertEqual(len(added), 1)
        row = added[0]
        self.assertEqual(row.id_charger, GENERIC_CHARGER_ID)
        self.assertEqual(row.local_hour, 8)
        self.assertEqual(row.sample_count, 2)
        self.assertAlmostEqual(row.mean_energy_kwh, 15.0)
        self.assertAlmostEqual(row.mean_duration_hours, 3.0)

    def test_missing_generic_row_falls_back_to_single_session_if_no_history(self):
        db = MagicMock()
        stats_query = MagicMock()
        stats_query.filter.return_value.order_by.return_value.first.return_value = None
        sessions_query = MagicMock()
        sessions_query.filter.return_value.all.return_value = []

        def _query(model):
            if model.__name__ == "EvForecastStats":
                return stats_query
            return sessions_query

        db.query.side_effect = _query
        added = []
        db.add.side_effect = lambda obj: added.append(obj)

        forecast_updater._update_or_initialize_stat(
            db,
            charger_id=GENERIC_CHARGER_ID,
            local_hour=8,
            energy_kwh=12.0,
            duration_hours=1.5,
            tz_name="UTC",
        )

        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].sample_count, 1)
        self.assertAlmostEqual(added[0].mean_energy_kwh, 12.0)
        self.assertAlmostEqual(added[0].mean_duration_hours, 1.5)
        self.assertAlmostEqual(added[0].std_energy_kwh, 0.0)

    def test_generic_rebuild_excludes_energy_and_duration_outliers(self):
        charger = uuid4()
        ok = _session(
            energy=20.0,
            duration_h=2.0,
            start=datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc),
            charger_id=charger,
        )
        too_much_energy = _session(
            energy=81.0,
            duration_h=2.0,
            start=datetime(2026, 9, 17, 7, 10, tzinfo=timezone.utc),
            charger_id=charger,
        )
        too_long = _session(
            energy=20.0,
            duration_h=73.0,
            start=datetime(2026, 9, 17, 7, 20, tzinfo=timezone.utc),
            charger_id=charger,
        )

        db = MagicMock()
        stats_query = MagicMock()
        stats_query.filter.return_value.order_by.return_value.first.return_value = None
        sessions_query = MagicMock()
        sessions_query.filter.return_value.all.return_value = [ok, too_much_energy, too_long]

        def _query(model):
            if model.__name__ == "EvForecastStats":
                return stats_query
            return sessions_query

        db.query.side_effect = _query
        added = []
        db.add.side_effect = lambda obj: added.append(obj)

        with patch.object(
            forecast_updater, "get_pilot_tz_for_charger", return_value="Europe/London"
        ):
            forecast_updater._update_or_initialize_stat(
                db,
                charger_id=GENERIC_CHARGER_ID,
                local_hour=8,
                energy_kwh=20.0,
                duration_hours=2.0,
                tz_name="Europe/London",
            )

        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].sample_count, 1)
        self.assertAlmostEqual(added[0].mean_energy_kwh, 20.0)

    def test_generic_online_update_skips_outlier_session(self):
        latest = SimpleNamespace(
            mean_energy_kwh=10.0,
            std_energy_kwh=0.0,
            mean_duration_hours=2.0,
            std_duration_hours=0.0,
            sample_count=1,
        )
        db = MagicMock()
        stats_query = MagicMock()
        stats_query.filter.return_value.order_by.return_value.first.return_value = latest
        db.query.return_value = stats_query
        added = []
        db.add.side_effect = lambda obj: added.append(obj)

        forecast_updater._update_or_initialize_stat(
            db,
            charger_id=GENERIC_CHARGER_ID,
            local_hour=8,
            energy_kwh=100.0,
            duration_hours=2.0,
            tz_name="UTC",
        )
        self.assertEqual(added, [])
