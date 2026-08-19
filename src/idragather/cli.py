from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Sequence

from .models import BoundingBox, DateWindow
from .providers.era5_land import DATASET, fetch, plan_jobs
from .staging import CATEGORIES, StagingArea
from .weather import validate_weather_csv, write_weather_template


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="idragather")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan-era5", help="print ERA5-Land CDS jobs as JSON")
    _add_spatiotemporal_arguments(plan)

    download = commands.add_parser("fetch-era5", help="download raw ERA5-Land NetCDF")
    download.add_argument("output", type=Path)
    _add_spatiotemporal_arguments(download)
    download.add_argument("--overwrite", action="store_true")

    stage = commands.add_parser("stage-local", help="copy a user-provided raw file")
    stage.add_argument("output", type=Path)
    stage.add_argument("category", choices=CATEGORIES)
    stage.add_argument("source", type=Path)
    stage.add_argument("--source-name")
    stage.add_argument("--overwrite", action="store_true")

    template = commands.add_parser(
        "weather-template", help="create an editable normalized daily-weather CSV"
    )
    template.add_argument("output", type=Path)
    template.add_argument("--locations", nargs="+", required=True)
    template.add_argument("--start", required=True)
    template.add_argument("--end", required=True)

    validate = commands.add_parser("validate-weather", help="validate normalized weather CSV")
    validate.add_argument("input", type=Path)
    validate.add_argument("--require-values", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan-era5":
        bbox, window = _bbox_window(args)
        jobs = plan_jobs(bbox, window)
        print(
            json.dumps(
                {
                    "dataset": DATASET,
                    "jobs": [
                        {"target": job.target_name, "request": job.request} for job in jobs
                    ],
                },
                indent=2,
            )
        )
        return 0
    if args.command == "fetch-era5":
        bbox, window = _bbox_window(args)
        for path in fetch(args.output, bbox, window, overwrite=args.overwrite):
            print(path)
        return 0
    if args.command == "stage-local":
        path = StagingArea(args.output).stage_local(
            args.source,
            category=args.category,
            source_name=args.source_name,
            overwrite=args.overwrite,
        )
        print(path)
        return 0
    if args.command == "weather-template":
        path = write_weather_template(
            args.output,
            locations=args.locations,
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
        )
        print(path)
        return 0
    if args.command == "validate-weather":
        issues = validate_weather_csv(
            args.input,
            allow_missing_values=not args.require_values,
        )
        for issue in issues:
            position = f"row {issue.row}" if issue.row else "file"
            if issue.field:
                position += f", {issue.field}"
            print(f"{issue.severity.upper()}: {position}: {issue.message}")
        return 1 if any(issue.severity == "error" for issue in issues) else 0
    raise AssertionError(args.command)


def _add_spatiotemporal_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        required=True,
    )
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)


def _bbox_window(args: argparse.Namespace) -> tuple[BoundingBox, DateWindow]:
    return BoundingBox(*args.bbox), DateWindow.from_iso(args.start, args.end)


if __name__ == "__main__":
    raise SystemExit(main())

