from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class BoundingBox:
    """A non-wrapping EPSG:4326 bounding box."""

    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError("west/east must satisfy -180 <= west < east <= 180")
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError("south/north must satisfy -90 <= south < north <= 90")

    def as_dict(self) -> dict[str, float | str]:
        return {
            "crs": "EPSG:4326",
            "west": self.west,
            "south": self.south,
            "east": self.east,
            "north": self.north,
        }


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("start date must not be after end date")

    @classmethod
    def from_iso(cls, start: str, end: str) -> "DateWindow":
        return cls(date.fromisoformat(start), date.fromisoformat(end))

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

