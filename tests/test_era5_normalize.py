from datetime import date, datetime, timedelta, timezone
import unittest

import numpy as np

from IdrAgraGather.core.era5_normalize import (
    Era5Cube,
    OUTPUT_LAYER,
    WIND_10M_TO_2M,
    _deaccumulate_era5_land,
    _daily_utc_hours,
    aggregate_daily,
)
from IdrAgraGather.core.models import DateWindow
from zoneinfo import ZoneInfo


UTC = timezone.utc


class Era5NormalizeTests(unittest.TestCase):
    def test_output_is_one_daily_point_layer(self):
        self.assertEqual(OUTPUT_LAYER, "weather_daily_points")

    def test_hourly_cube_becomes_clean_daily_idragra_fields(self):
        times = tuple(
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)
            for index in range(48)
        )
        shape = (48, 1, 1)
        temperature = np.empty(shape)
        precipitation = np.empty(shape)
        radiation = np.empty(shape)
        for index, stamp in enumerate(times):
            temperature[index, 0, 0] = 273.15 + index
            step = 24 if stamp.hour == 0 else stamp.hour
            precipitation[index, 0, 0] = step * 0.001
            radiation[index, 0, 0] = step * 1_000_000.0

        cube = Era5Cube(
            times,
            np.array([46.2]),
            np.array([9.3]),
            {
                "t2m": temperature,
                "d2m": temperature - 2.0,
                "u10": np.ones(shape),
                "v10": np.zeros(shape),
                "ssrd": radiation,
                "tp": precipitation,
            },
        )
        daily, warnings = aggregate_daily(
            cube,
            DateWindow(date(2026, 1, 1), date(2026, 1, 1)),
            timezone_name="Europe/Rome",
        )

        self.assertEqual(len(daily), 1)
        values = daily[0].values
        self.assertAlmostEqual(values["tmin_c"][0, 0], 0.0)
        self.assertAlmostEqual(values["tmax_c"][0, 0], 22.0)
        self.assertAlmostEqual(values["wind2m_m_s"][0, 0], WIND_10M_TO_2M)
        self.assertAlmostEqual(values["solar_rad_mj_m2_day"][0, 0], 24.0)
        self.assertAlmostEqual(values["precip_mm"][0, 0], 24.0)
        self.assertEqual(
            set(values),
            {
                "tmax_c",
                "tmin_c",
                "rhmax_pct",
                "rhmin_pct",
                "wind2m_m_s",
                "solar_rad_mj_m2_day",
                "precip_mm",
            },
        )
        self.assertTrue(any("missing edge instantaneous" in item for item in warnings))

    def test_rome_legal_time_has_23_and_25_hour_days(self):
        spring, _ = _daily_utc_hours(date(2026, 3, 29), ZoneInfo("Europe/Rome"))
        autumn, _ = _daily_utc_hours(date(2026, 10, 25), ZoneInfo("Europe/Rome"))
        self.assertEqual(len(spring), 23)
        self.assertEqual(len(autumn), 25)

    def test_interior_missing_hour_is_rejected(self):
        times = tuple(
            datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)
            for index in range(49)
            if index != 12
        )
        shape = (len(times), 1, 1)
        values = {name: np.ones(shape) for name in ("t2m", "d2m", "u10", "v10", "ssrd", "tp")}
        cube = Era5Cube(times, np.array([46.2]), np.array([9.3]), values)
        with self.assertRaisesRegex(ValueError, "non-hourly gap"):
            aggregate_daily(
                cube,
                DateWindow(date(2026, 1, 1), date(2026, 1, 1)),
                timezone_name="Europe/Rome",
            )

    def test_tiny_negative_ssrd_rounding_residue_is_clamped(self):
        times = (
            datetime(2026, 1, 1, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, 2, tzinfo=UTC),
        )
        accumulated = np.array([[[10.0]], [[0.0]], [[-4.0]]])
        warnings = []
        increments = _deaccumulate_era5_land(
            accumulated, times, "ssrd", warnings
        )
        self.assertEqual(increments[2, 0, 0], 0.0)


if __name__ == "__main__":
    unittest.main()
