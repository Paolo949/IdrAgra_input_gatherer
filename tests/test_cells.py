import tempfile
import unittest
from pathlib import Path

from IdrAgraGather.core.cells import allocate_landuse_ids, dominant_continuous_value
from IdrAgraGather.core.landuses import LandUseAllocation


class CellAllocationTests(unittest.TestCase):
    def test_dominant_continuous_value_ignores_narrow_extreme(self):
        value = dominant_continuous_value([2.0, 2.1, 2.2, 2.4, 75.0])
        self.assertAlmostEqual(value, 2.15)

    def test_deterministic_split_approaches_requested_area(self):
        items = [(f"Arable\0cell_{index}", 1.0) for index in range(100)]
        rules = [
            LandUseAllocation("Arable", 1, 45.0),
            LandUseAllocation("Arable", 2, 30.0),
            LandUseAllocation("Arable", 3, 25.0),
        ]
        first, requested, generated = allocate_landuse_ids(items, rules)
        second, _, _ = allocate_landuse_ids(list(reversed(items)), rules)
        self.assertEqual(first, second)
        self.assertEqual(requested, {1: 45.0, 2: 30.0, 3: 25.0})
        self.assertEqual(generated, requested)

    def test_allocations_are_independent_for_each_source_class(self):
        items = [("A\0one", 5.0), ("B\0two", 7.0)]
        assigned, _, _ = allocate_landuse_ids(
            items,
            [
                LandUseAllocation("A", 1, 100.0),
                LandUseAllocation("B", 2, 100.0),
            ],
        )
        self.assertEqual(assigned, {"A\0one": 1, "B\0two": 2})


if __name__ == "__main__":
    unittest.main()
