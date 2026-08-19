from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable


ID_FIELD = "location_id"
DATE_FIELD = "date"
VALUE_FIELDS = (
    "tmax_c",
    "tmin_c",
    "rhmax_pct",
    "rhmin_pct",
    "wind2m_m_s",
    "solar_rad_mj_m2_day",
    "precip_mm",
)
FIELDS = (DATE_FIELD, ID_FIELD, *VALUE_FIELDS)


@dataclass(frozen=True)
class ValidationIssue:
    row: int | None
    field: str | None
    severity: str
    message: str


def write_weather_template(
    path: str | Path,
    *,
    locations: Iterable[str],
    start: date,
    end: date,
) -> Path:
    if start > end:
        raise ValueError("start date must not be after end date")
    location_ids = tuple(dict.fromkeys(item.strip() for item in locations if item.strip()))
    if not location_ids:
        raise ValueError("at least one non-empty location ID is required")

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        current = start
        while current <= end:
            for location_id in location_ids:
                writer.writerow({DATE_FIELD: current.isoformat(), ID_FIELD: location_id})
            current += timedelta(days=1)
    return output


def validate_weather_csv(
    path: str | Path,
    *,
    allow_missing_values: bool = True,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen: set[tuple[date, str]] = set()
    dates_by_location: dict[str, list[date]] = {}

    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        actual = tuple(reader.fieldnames or ())
        missing_fields = [field for field in FIELDS if field not in actual]
        if missing_fields:
            return [
                ValidationIssue(
                    None,
                    None,
                    "error",
                    f"missing required columns: {', '.join(missing_fields)}",
                )
            ]

        for row_number, row in enumerate(reader, start=2):
            parsed_date = _date(row.get(DATE_FIELD, ""), row_number, issues)
            location_id = (row.get(ID_FIELD) or "").strip()
            if not location_id:
                issues.append(ValidationIssue(row_number, ID_FIELD, "error", "empty location ID"))

            values: dict[str, float] = {}
            for field in VALUE_FIELDS:
                raw_value = (row.get(field) or "").strip()
                if not raw_value:
                    if not allow_missing_values:
                        issues.append(ValidationIssue(row_number, field, "error", "missing value"))
                    continue
                try:
                    values[field] = float(raw_value)
                except ValueError:
                    issues.append(ValidationIssue(row_number, field, "error", "not a number"))
                else:
                    if not math.isfinite(values[field]):
                        issues.append(
                            ValidationIssue(row_number, field, "error", "must be a finite number")
                        )
                        del values[field]

            _check_ranges(row_number, values, issues)
            if parsed_date is not None and location_id:
                key = (parsed_date, location_id)
                if key in seen:
                    issues.append(
                        ValidationIssue(row_number, None, "error", "duplicate date/location row")
                    )
                seen.add(key)
                dates_by_location.setdefault(location_id, []).append(parsed_date)

    for location_id, location_dates in dates_by_location.items():
        ordered = sorted(set(location_dates))
        for previous, current in zip(ordered, ordered[1:]):
            if current != previous + timedelta(days=1):
                issues.append(
                    ValidationIssue(
                        None,
                        DATE_FIELD,
                        "warning",
                        f"{location_id!r} has a gap between {previous} and {current}",
                    )
                )
    return issues


def _date(
    raw_value: str,
    row_number: int,
    issues: list[ValidationIssue],
) -> date | None:
    try:
        return date.fromisoformat(raw_value.strip())
    except ValueError:
        issues.append(
            ValidationIssue(row_number, DATE_FIELD, "error", "expected ISO date yyyy-mm-dd")
        )
        return None


def _check_ranges(
    row_number: int,
    values: dict[str, float],
    issues: list[ValidationIssue],
) -> None:
    for field in ("rhmax_pct", "rhmin_pct"):
        if field in values and not 0 <= values[field] <= 100:
            issues.append(ValidationIssue(row_number, field, "error", "must be in [0, 100]"))
    for field in ("wind2m_m_s", "solar_rad_mj_m2_day", "precip_mm"):
        if field in values and values[field] < 0:
            issues.append(ValidationIssue(row_number, field, "error", "must be non-negative"))
    if {"tmax_c", "tmin_c"} <= values.keys() and values["tmax_c"] < values["tmin_c"]:
        issues.append(ValidationIssue(row_number, None, "error", "tmax_c is below tmin_c"))
    if {"rhmax_pct", "rhmin_pct"} <= values.keys() and values["rhmax_pct"] < values["rhmin_pct"]:
        issues.append(ValidationIssue(row_number, None, "error", "rhmax_pct is below rhmin_pct"))
