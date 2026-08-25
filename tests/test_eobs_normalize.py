from datetime import date
import unittest

import numpy as np

from IdrAgraGather.core.eobs_normalize import (
    RADIATION_W_M2_TO_MJ_M2_DAY,
    _common_date_window,
    _complete_location_mask,
    _spatial_indices,
    _to_canonical_daily,
    _read_subset,
)
from IdrAgraGather.core.era5_normalize import DAILY_FIELDS, WIND_10M_TO_2M
from IdrAgraGather.core.models import BoundingBox, DateWindow
from IdrAgraGather.core.providers.eobs import VARIABLES


class EobsNormalizeTests(unittest.TestCase):
    def test_common_coverage_rejects_internal_calendar_gaps(self):
        first = date(2026, 1, 1)
        third = date(2026, 1, 3)
        dates = {name: {first, third} for name in VARIABLES}
        with self.assertRaisesRegex(ValueError, "internal missing day"):
            _common_date_window(dates, context="in test data")

    def test_packed_netcdf_values_apply_scale_and_preserve_missing_values(self):
        class Dimension:
            def __init__(self, name):
                self.name = name

            def GetName(self):
                return self.name

        class Variable:
            def GetDimensions(self):
                return [Dimension(name) for name in ("time", "latitude", "longitude")]

            def ReadAsArray(self, starts, counts):
                self.selection = (starts, counts)
                return np.array([[[507.0, -9999.0]]])

            def GetScale(self):
                return 0.01

            def GetOffset(self):
                return 0.0

        variable = Variable()
        data = _read_subset(variable, 3, 1, np.array([4]), np.array([5, 6]), "tn")
        self.assertAlmostEqual(data[0, 0, 0], 5.07)
        self.assertTrue(np.isnan(data[0, 0, 1]))
        self.assertEqual(variable.selection, ([3, 4, 5], [1, 1, 2]))

    def test_daily_values_become_the_same_canonical_fields_as_era5(self):
        day = date(2025, 7, 1)
        raw = {
            "tn": {day: np.array([[10.0]])},
            "tx": {day: np.array([[30.0]])},
            "rr": {day: np.array([[4.5]])},
            "hu": {day: np.array([[60.0]])},
            "fg": {day: np.array([[2.0]])},
            "qq": {day: np.array([[200.0]])},
        }
        daily = _to_canonical_daily(raw, DateWindow(day, day))
        values = daily[0].values

        self.assertEqual(tuple(values), DAILY_FIELDS)
        self.assertEqual(values["tmin_c"][0, 0], 10.0)
        self.assertEqual(values["tmax_c"][0, 0], 30.0)
        self.assertAlmostEqual(values["wind2m_m_s"][0, 0], 2.0 * WIND_10M_TO_2M)
        self.assertAlmostEqual(
            values["solar_rad_mj_m2_day"][0, 0],
            200.0 * RADIATION_W_M2_TO_MJ_M2_DAY,
        )
        self.assertEqual(values["precip_mm"][0, 0], 4.5)
        self.assertGreater(values["rhmax_pct"][0, 0], values["rhmin_pct"][0, 0])

    def test_any_missing_daily_field_excludes_the_location(self):
        day = date(2025, 7, 1)
        raw = {
            "tn": {day: np.array([[10.0, 10.0]])},
            "tx": {day: np.array([[30.0, 30.0]])},
            "rr": {day: np.array([[4.5, np.nan]])},
            "hu": {day: np.array([[60.0, 60.0]])},
            "fg": {day: np.array([[2.0, 2.0]])},
            "qq": {day: np.array([[200.0, 200.0]])},
        }
        daily = _to_canonical_daily(raw, DateWindow(day, day))
        np.testing.assert_array_equal(
            _complete_location_mask(daily), np.array([[True, False]])
        )

    def test_small_aoi_selects_outward_grid_coverage(self):
        rows, columns = _spatial_indices(
            np.array([46.0, 46.1, 46.2, 46.3]),
            np.array([9.2, 9.3, 9.4, 9.5, 9.6]),
            BoundingBox(9.376, 46.143, 9.437, 46.176),
        )
        np.testing.assert_array_equal(rows, np.array([1, 2]))
        np.testing.assert_array_equal(columns, np.array([1, 2, 3]))

    def test_spatial_buffer_stops_at_available_dataset_edges(self):
        rows, columns = _spatial_indices(
            np.array([46.0, 46.1]),
            np.array([9.2, 9.3]),
            BoundingBox(9.2, 46.0, 9.3, 46.1),
        )
        np.testing.assert_array_equal(rows, np.array([0, 1]))
        np.testing.assert_array_equal(columns, np.array([0, 1]))


if __name__ == "__main__":
    unittest.main()
