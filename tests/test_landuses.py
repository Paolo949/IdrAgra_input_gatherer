import tempfile
import unittest
from pathlib import Path

from IdrAgraGather.core.landuses import (
    CropDefinition,
    LandUseAllocation,
    LandUseDefinition,
    parse_idragra_landuses,
    read_configuration,
    validate_allocations,
    write_configuration,
)


class LandUseConfigurationTests(unittest.TestCase):
    def test_parse_idragra_rotation_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "soil_uses.txt"
            path.write_text(
                "# example\n"
                "Cr_ID\tCrop1\tCrop2\t# Comments\n"
                "1\twheat.tab\t*\t# Wheat\n"
                "2\twheat.tab\tmaize.tab\t# Wheat -> maize\n"
                "3\t*\t*\t# Non-agricultural\n"
                "endTable =\n",
                encoding="utf-8",
            )
            crops, landuses = parse_idragra_landuses(path)
        self.assertEqual([crop.parameter_file for crop in crops], ["wheat.tab", "maize.tab"])
        self.assertEqual(landuses[1].crop1_id, "wheat")
        self.assertEqual(landuses[1].crop2_id, "maize")
        self.assertIsNone(landuses[2].crop1_id)

    def test_configuration_round_trip_preserves_split_allocations(self):
        crops = [CropDefinition("maize", "Maize", "maize.tab")]
        landuses = [LandUseDefinition(1, "Maize", "maize")]
        allocations = [LandUseAllocation("Arable", 1, 100.0)]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "landuses.json"
            write_configuration(path, crops, landuses, allocations)
            loaded = read_configuration(path)
        self.assertEqual(loaded, (crops, landuses, allocations))

    def test_allocation_totals_must_equal_100(self):
        landuses = [LandUseDefinition(1, "A"), LandUseDefinition(2, "B")]
        with self.assertRaisesRegex(ValueError, "total 90%"):
            validate_allocations(
                [
                    LandUseAllocation("Source", 1, 45.0),
                    LandUseAllocation("Source", 2, 45.0),
                ],
                landuses,
            )


if __name__ == "__main__":
    unittest.main()
