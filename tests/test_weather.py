import csv
from datetime import date
import tempfile
import unittest
from pathlib import Path

from IdrAgraGather.core.weather import FIELDS, validate_weather_csv, write_weather_template


class WeatherTests(unittest.TestCase):
    def test_template_is_long_form_and_editable(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = write_weather_template(
                Path(temporary) / "weather.csv",
                locations=["1", "2"],
                start=date(2024, 2, 28),
                end=date(2024, 3, 1),
            )
            with output.open(encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 6)
            self.assertEqual(tuple(rows[0]), FIELDS)
            self.assertEqual(
                {row["date"] for row in rows},
                {"2024-02-28", "2024-02-29", "2024-03-01"},
            )

    def test_validator_reports_ranges_and_ordering(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "weather.csv"
            path.write_text(
                ",".join(FIELDS) + "\n" + "2024-01-01,a,1,2,20,30,-1,3,0\n",
                encoding="utf-8",
            )
            messages = [issue.message for issue in validate_weather_csv(path)]
            self.assertIn("tmax_c is below tmin_c", messages)
            self.assertIn("rhmax_pct is below rhmin_pct", messages)
            self.assertIn("must be non-negative", messages)


if __name__ == "__main__":
    unittest.main()
