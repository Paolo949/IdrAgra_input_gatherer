"""Export the normalized grid workspace to IdrAgra v2 input conventions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import shutil
from typing import Callable, Sequence

from .landuses import read_configuration
from .manifest import Manifest


NODATA = -9999.0
WEATHER_FIELDS = ("tmax_c", "tmin_c", "precip_mm", "rhmax_pct", "rhmin_pct", "wind2m_m_s", "solar_rad_mj_m2_day")
HYDRAULIC_FIELDS = ("ksat_mm_h", "theta_fc", "theta_wp", "theta_res", "theta_sat")
TEXTURE_FIELDS = ("sand_pct", "silt_pct", "clay_pct")
CAPILLARY_PARAMETERS = {
    101: {"b1": -0.16, "b2": -0.54, "a3": -0.15, "b3": 2.1, "a4": 7.55, "b4": -2.03},
    102: {"b1": -0.17, "b2": -0.27, "a3": -1.3, "b3": 6.6, "a4": 4.6, "b4": -0.65},
    103: {"b1": -0.32, "b2": -0.16, "a3": -1.4, "b3": 6.8, "a4": 1.11, "b4": -0.98},
}


@dataclass(frozen=True)
class V2ExportResult:
    output_path: Path
    file_count: int
    station_count: int
    active_landuses: tuple[int, ...]
    start: date
    end: date
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _Grid:
    values: object
    geotransform: tuple[float, ...]
    projection: str
    nodata: float


@dataclass(frozen=True)
class _Station:
    source_id: str
    station_id: int
    filename: str
    x: float
    y: float
    latitude: float
    altitude: float
    rows: tuple[tuple[date, tuple[float, ...]], ...]


# Export the normalized workspace to IdrAgra v2 input conventions and return a summary of the export.
def export_v2_workspace(
    workspace: str | Path,
    destination: str | Path,
    *,
    evap_layer_m: float = 0.1,
    root_layer_m: float = 0.9,
    weather_neighbors: int = 2,
    hydrologic_condition: int = 2,
    crop_parameter_folder: str | Path | None = None,
    overwrite: bool = False,
    on_status: Callable[[str], None] | None = None,
) -> V2ExportResult:
    root = Path(workspace).resolve()
    destination = Path(destination).resolve()
    if destination == root or root.is_relative_to(destination):
        raise ValueError("The export destination cannot be the workspace or one of its parent folders.")
    if destination.is_relative_to(root):
        relative_destination = destination.relative_to(root)
        if not relative_destination.parts or relative_destination.parts[0] != "exports":
            raise ValueError("Exports stored inside the workspace must be below its 'exports' folder.")
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"Export destination is not a folder: {destination}")

    # Ensure that we have the required data
    required = {
        "soil grid": root / "cells" / "soil_id.tif",
        "land-use grid": root / "cells" / "landuse_id.tif",
        "slope grid": root / "cells" / "slope_pct.tif",
        "elevation grid": root / "cells" / "elevation_m_asl.tif",
        "cell view": root / "cells" / "simulation_cells.gpkg",
        "land-use configuration": root / "cells" / "landuse_configuration.json",
        "soil hydraulics": root / "soil" / "soil_hydraulics.gpkg",
        "weather": root / "weather" / "weather_daily_points.gpkg",
    }
    missing = [f"{label}: {path}" for label, path in required.items() if not path.is_file()]
    if missing:
        raise ValueError("Missing required v2 export input(s):\n" + "\n".join(missing))
    if weather_neighbors < 1:
        raise ValueError("The number of weather neighbours must be positive.")
    if hydrologic_condition not in {1, 2, 3}:
        raise ValueError("Hydrologic condition must be 1 (good), 2 (fair), or 3 (poor).")
    if destination.exists() and any(destination.iterdir()) and not overwrite:
        raise FileExistsError(f"Export destination is not empty: {destination}")

    try:
        from osgeo import gdal, ogr, osr
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("IdrAgra v2 export requires GDAL/OGR in QGIS.") from exc
    gdal.UseExceptions()
    ogr.UseExceptions()

    soil_grid = _read_grid(required["soil grid"], gdal)
    landuse_grid = _read_grid(required["land-use grid"], gdal)
    slope_grid = _read_grid(required["slope grid"], gdal)
    elevation_grid = _read_grid(required["elevation grid"], gdal)
    _validate_aligned((soil_grid, landuse_grid, slope_grid, elevation_grid))
    crops, landuses, _allocations = read_configuration(required["land-use configuration"])
    landuse_by_id = {item.landuse_id: item for item in landuses}
    configured_ids = set(landuse_by_id)
    grid_landuse_ids = {int(value) for value in np.unique(landuse_grid.values) if value != landuse_grid.nodata}
    cell_landuse_ids = _read_cell_landuse_ids(required["cell view"], ogr)
    unknown = sorted((grid_landuse_ids | cell_landuse_ids) - configured_ids)
    if unknown:
        raise ValueError(f"Simulation cells refer to undefined land-use ID(s): {unknown}")
    active_ids = tuple(sorted(
        landuse_id for landuse_id in cell_landuse_ids
        if landuse_by_id[landuse_id].crop1_id
    ))
    if not active_ids:
        raise ValueError("No crop-bearing land-use cells are available for v2 export.")
    exported_id_by_landuse = {
        landuse_id: exported_id
        for exported_id, landuse_id in enumerate(active_ids, start=1)
    }
    exported_ids = tuple(exported_id_by_landuse.values())
    mask = np.isin(landuse_grid.values, active_ids) & (soil_grid.values != soil_grid.nodata)
    if not np.any(mask):
        raise ValueError("No cells remain after excluding non-simulated land uses.")
    invalid_slope = mask & ((slope_grid.values == slope_grid.nodata) | ~np.isfinite(slope_grid.values))
    if np.any(invalid_slope):
        raise ValueError(f"{int(np.count_nonzero(invalid_slope))} simulated cell(s) have no slope value.")

    if on_status:
        on_status("Aggregating six soil horizons into the two IdrAgra v2 layers.")
    profiles, soil_metadata = _read_hydraulic_profiles(required["soil hydraulics"], ogr)
    profile_layers = {
        profile_id: aggregate_profile_layers(rows, (evap_layer_m, root_layer_m)) for profile_id, rows in profiles.items()
    }
    used_profiles = {int(value) for value in np.unique(soil_grid.values[mask])}
    absent_profiles = sorted(used_profiles - set(profile_layers))
    if absent_profiles:
        raise ValueError(f"PTF output is missing soil profile ID(s): {absent_profiles}")

    staging = destination.with_name(destination.name + ".tmp-idragather")
    _remove_tree(staging)
    staging.mkdir(parents=True)
    warnings: list[str] = []
    try:
        geodata = staging / "geodata"
        meteodata = staging / "meteodata"
        landuse_output = staging / "landuses"
        irrigation_output = staging / "irrmethods"
        pheno_output = staging / "pheno"
        geodata.mkdir()
        meteodata.mkdir()
        landuse_output.mkdir()
        irrigation_output.mkdir()
        pheno_output.mkdir()

        def announce_write(path):
            if on_status:
                relative = Path(path).relative_to(staging).as_posix()
                on_status(f"Writing {relative}...")

        def write_ascii(path, values, **kwargs):
            announce_write(path)
            _write_ascii(path, values, **kwargs)

        grid_kwargs = dict(geotransform=soil_grid.geotransform, nodata=NODATA)
        write_ascii(geodata / "domain.asc", np.where(mask, 1, NODATA), integer=True, **grid_kwargs)
        exported_landuses = np.full(landuse_grid.values.shape, NODATA, dtype=float)
        for landuse_id, exported_id in exported_id_by_landuse.items():
            exported_landuses[mask & (landuse_grid.values == landuse_id)] = exported_id
        write_ascii(geodata / "soiluse.asc", exported_landuses, integer=True, **grid_kwargs)
        write_ascii(geodata / "slope.asc", np.where(mask, slope_grid.values, NODATA), **grid_kwargs)
        write_ascii(geodata / "hydr_cond.asc", np.where(mask, hydrologic_condition, NODATA), integer=True, **grid_kwargs)

        layer_maps = {
            name: []
            for name in (
                "Ksat_I",
                "Ksat_II",
                "N_I",
                "N_II",
                "ThetaI_FC",
                "ThetaII_FC",
                "ThetaI_WP",
                "ThetaII_WP",
                "ThetaI_r",
                "ThetaII_r",
                "ThetaI_sat",
                "ThetaII_sat",
            )
        }
        mapping = {
            "Ksat": "ksat_mm_h",
            "N": "v2_brooks_corey_n",
            "Theta_FC": "theta_fc",
            "Theta_WP": "theta_wp",
            "Theta_r": "theta_res",
            "Theta_sat": "theta_sat",
        }
        for layer_index, suffix in enumerate(("I", "II")):
            for stem, field in mapping.items():
                name = f"{stem}_{suffix}" if stem in {"Ksat", "N"} else f"Theta{suffix}_{stem[6:]}"
                layer_maps[name] = _profile_lookup_grid(soil_grid.values, mask, profile_layers, layer_index, field, np)
                if field == "ksat_mm_h":
                    # IdrAgra v2 consumes Ksat in cm/h; canonical PTF output is mm/h.
                    layer_maps[name][mask] /= 10.0
                write_ascii(geodata / f"{name}.asc", layer_maps[name], **grid_kwargs)
        hsg = _hydrologic_group_grid(soil_grid.values, mask, profiles, np)
        write_ascii(geodata / "hydr_group.asc", hsg, integer=True, **grid_kwargs)
        capillary_by_profile = {
            profile_id: capillary_rise_parameters(rows, evap_layer_m + root_layer_m)
            for profile_id, rows in profiles.items()
        }
        for parameter in ("a3", "a4", "b1", "b2", "b3", "b4"):
            values = _simple_profile_lookup_grid(
                soil_grid.values, mask, capillary_by_profile, parameter, np
            )
            write_ascii(geodata / f"CapRisePar_{parameter}.asc", values, **grid_kwargs)

        stations, start, end = _read_weather_stations(required["weather"], soil_grid, elevation_grid, ogr, osr)
        for station in stations:
            announce_write(meteodata / station.filename)
            _write_station_file(meteodata / station.filename, station, start, end)
        announce_write(staging / "weather_stations.dat")
        _write_station_list(staging / "weather_stations.dat", stations)
        weight_count = 2 if len(stations) == 1 else min(weather_neighbors, len(stations))
        weights = _weather_weight_grids(soil_grid, mask, stations, weight_count, np)
        for index, values in enumerate(weights, start=1):
            write_ascii(geodata / f"meteo_{index}.asc", values, decimals=9, **grid_kwargs)

        _write_landuses(
            landuse_output,
            crops,
            landuses,
            active_ids,
            exported_id_by_landuse,
            Path(crop_parameter_folder).resolve() if crop_parameter_folder else None,
            warnings,
            on_write=announce_write,
        )
        announce_write(staging / "idragra_parameters.txt")
        _write_parameter_template(
            staging / "idragra_parameters.txt",
            start,
            end,
            stations,
            weight_count,
            exported_ids,
            evap_layer_m,
            root_layer_m,
            profile_layers,
            np,
        )
        announce_write(irrigation_output / "irrmethods.txt")
        (irrigation_output / "irrmethods.txt").write_text(
            "# Parser stub for Mode 0; no operational irrigation methods.\nIrrMethNum = 0\nList =\nEndList =\n",
            encoding="ascii",
        )
        announce_write(pheno_output / "README.txt")
        (pheno_output / "README.txt").write_text(
            "Run CropCoef for every exported station and place its pheno_station_NNN "
            "directory here. IdrAgra v2 cannot run without those daily crop series.\n",
            encoding="ascii",
        )
        warnings.append(
            "IdrAgra v2 phenology directories are not generated. Run CropCoef for each exported station before starting IdrAgra."
        )
        warnings.append(
            "This first contract is static land use, Mode 0 (rain-fed), and capillary "
            "rise disabled. Texture-derived capillary parameter grids are included, but "
            "irrigation, yearly land-use, and water-table inputs are omitted."
        )
        provenance = {
            "schema_version": 1,
            "target": "IdrAgra v2",
            "workspace": str(root),
            "inputs": {label: {"path": str(path), "sha256": _sha256(path)} for label, path in required.items()},
            "soil_ptf": soil_metadata,
            "layer_intervals_m": [[0.0, evap_layer_m], [evap_layer_m, evap_layer_m + root_layer_m]],
            "aggregation": {
                "ksat": "thickness-weighted harmonic mean",
                "ksat_export_unit": "cm/h (canonical mm/h divided by 10)",
                "water_contents": "thickness-weighted arithmetic mean",
                "N": "legacy IdrAgraTools Brooks-Corey drainage exponent; reference conductivity 0.2 mm/day",
            },
            "capillary_rise": {
                "model": "Liu et al. (2006) parameters reproduced from IdrAgraTools",
                "representative_texture": "legacy depth-weighted horizon rank below the modeled root zone to profile bottom",
                "usda_macro_classes": {
                    "101": "sand, loamy sand, sandy loam",
                    "102": "loam, silt loam, silt",
                    "103": "sandy clay loam, clay loam, silty clay loam, sandy clay, silty clay, clay",
                },
                "coefficients": CAPILLARY_PARAMETERS,
                "enabled_in_template": False,
                "reason_disabled": "water-table depth is not yet part of the normalized workspace",
            },
            "weather_weights": "nearest stations, normalized inverse planar distance; encoded as station_id + fractional_weight",
            "excluded_landuses": sorted(cell_landuse_ids - set(active_ids)),
            "landuse_id_mapping": {
                str(source_id): exported_id
                for source_id, exported_id in exported_id_by_landuse.items()
            },
            "warnings": warnings,
        }
        announce_write(staging / "export_provenance.json")
        (staging / "export_provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
        announce_write(staging / "README_EXPORT.txt")
        (staging / "README_EXPORT.txt").write_text(
            "IdrAgra v2 export\n\n" + "\n".join(f"- {item}" for item in warnings) + "\n", encoding="utf-8"
        )
        if destination.exists():
            _remove_tree(destination)
        staging.replace(destination)
    except Exception:
        _remove_tree(staging)
        raise

    files = tuple(path for path in destination.rglob("*") if path.is_file())
    try:
        destination.relative_to(root)
    except ValueError:
        pass
    else:
        manifest = Manifest(root)
        manifest.add_asset(
            destination / "export_provenance.json",
            category="export",
            provider="idragather",
            dataset="IdrAgra v2 static rain-fed export",
            request={
                "destination": str(destination),
                "evap_layer_m": evap_layer_m,
                "root_layer_m": root_layer_m,
                "weather_neighbors": weather_neighbors,
                "hydrologic_condition": hydrologic_condition,
                "warnings": warnings,
            },
        )
    return V2ExportResult(destination, len(files), len(stations), active_ids, start, end, tuple(warnings))


# Collapse physical horizons into v2 layers using legacy IdrAgraTools rules.
def aggregate_profile_layers(
    horizons: Sequence[dict[str, float]], layer_thicknesses_m=(0.1, 0.9)
) -> tuple[dict[str, float], ...]:
    if not horizons:
        raise ValueError("A soil profile has no hydraulic horizons.")
    boundaries = [0.0]
    for thickness in layer_thicknesses_m:
        if not math.isfinite(thickness) or thickness <= 0:
            raise ValueError("v2 soil layer thicknesses must be positive.")
        boundaries.append(boundaries[-1] + float(thickness))
    ordered = sorted(horizons, key=lambda row: row["top_cm"])
    available_bottom = max(float(row["bottom_cm"]) / 100.0 for row in ordered)
    if available_bottom + 1e-9 < boundaries[-1]:
        raise ValueError(f"Soil profile ends at {available_bottom:g} m but v2 layers require {boundaries[-1]:g} m.")
    result = []
    for lower, upper in zip(boundaries, boundaries[1:]):
        overlaps = []
        for row in ordered:
            top = float(row["top_cm"]) / 100.0
            bottom = float(row["bottom_cm"]) / 100.0
            overlap = max(0.0, min(bottom, upper) - max(top, lower))
            if overlap:
                overlaps.append((row, overlap))
        if sum(weight for _row, weight in overlaps) < upper - lower - 1e-8:
            raise ValueError(f"Soil horizons do not completely cover {lower:g}-{upper:g} m.")
        layer = {}
        total = sum(weight for _row, weight in overlaps)
        for field in HYDRAULIC_FIELDS:
            values = [(float(row[field]), weight) for row, weight in overlaps]
            if any(not math.isfinite(value) for value, _weight in values):
                raise ValueError(f"Non-finite soil hydraulic value in {field}.")
            if field == "ksat_mm_h":
                if any(value <= 0 for value, _weight in values):
                    raise ValueError("Ksat must be positive for harmonic aggregation.")
                layer[field] = total / sum(weight / value for value, weight in values)
            else:
                layer[field] = sum(value * weight for value, weight in values) / total
        theta_r = layer["theta_res"]
        theta_wp = layer["theta_wp"]
        theta_fc = layer["theta_fc"]
        theta_sat = layer["theta_sat"]
        if not 0 <= theta_r < theta_wp < theta_fc < theta_sat <= 1:
            raise ValueError("v2 water contents must satisfy 0 <= theta_res < theta_wp < theta_fc < theta_sat <= 1.")
        ratio = (theta_fc - theta_r) / (theta_sat - theta_r)
        if not 0 < ratio < 1:
            raise ValueError("v2 N requires theta_res < theta_fc < theta_sat.")
        # Legacy IdrAgraTools uses 0.2 mm/day as the reference conductivity.
        layer["v2_brooks_corey_n"] = math.log((0.2 / 24.0) / layer["ksat_mm_h"]) / math.log(ratio)
        result.append(layer)
    return tuple(result)


def usda_texture_class(sand_pct: float, silt_pct: float, clay_pct: float) -> int:
    """Return the legacy 1-12 USDA texture code from fine-earth percentages."""

    sand, silt, clay = (float(sand_pct), float(silt_pct), float(clay_pct))
    if any(not math.isfinite(value) or value < 0 for value in (sand, silt, clay)):
        raise ValueError("USDA texture fractions must be finite and non-negative.")
    total = sand + silt + clay
    if not 99.0 <= total <= 101.0:
        raise ValueError("USDA texture fractions must sum to 99-101 percent.")
    sand, silt, clay = (100.0 * value / total for value in (sand, silt, clay))

    # Ordered boundary rules follow the USDA textural triangle; returned codes
    # match IdrAgraTools' texture_code.csv.
    if silt >= 80 and clay < 12:
        return 6  # silt
    if clay >= 40 and silt >= 40:
        return 11  # silty clay
    if clay >= 35 and sand >= 45:
        return 10  # sandy clay
    if clay >= 40:
        return 12  # clay
    if 27 <= clay < 40 and silt >= 40 and sand <= 20:
        return 9  # silty clay loam
    if 27 <= clay < 40 and 20 < sand <= 45:
        return 8  # clay loam
    if 20 <= clay < 35 and sand > 45 and silt < 28:
        return 7  # sandy clay loam
    if silt >= 50 and clay < 27:
        return 5  # silt loam
    if 7 <= clay < 27 and 28 <= silt < 50 and sand <= 52:
        return 4  # loam
    if (
        (7 <= clay < 20 and sand > 52 and silt + 2 * clay >= 30)
        or (clay < 7 and silt < 50 and silt + 2 * clay >= 30)
    ):
        return 3  # sandy loam
    if sand >= 70 and silt + 1.5 * clay >= 15 and silt + 2 * clay < 30:
        return 2  # loamy sand
    if sand >= 85 and silt + 1.5 * clay < 15:
        return 1  # sand
    # Boundary points not captured above belong to the adjacent loam class.
    return 4


def capillary_rise_parameters(
    horizons: Sequence[dict[str, float]], modeled_depth_m: float
) -> dict[str, float | int]:
    """Reproduce IdrAgraTools' below-root texture selection and Liu parameters."""

    if not horizons:
        raise ValueError("A soil profile has no texture horizons.")
    ordered = sorted(horizons, key=lambda row: float(row["bottom_cm"]))
    profile_bottom = max(float(row["bottom_cm"]) / 100.0 for row in ordered)
    if modeled_depth_m < 0 or modeled_depth_m > profile_bottom + 1e-9:
        raise ValueError("Modeled depth must fall within the soil profile.")
    weights = []
    for row in ordered:
        top = float(row["top_cm"]) / 100.0
        bottom = float(row["bottom_cm"]) / 100.0
        weights.append(max(0.0, min(bottom, profile_bottom) - max(top, modeled_depth_m)))
    if sum(weights) > 0:
        weighted_rank = sum(index * weight for index, weight in enumerate(weights)) / sum(weights)
        selected_index = int(round(weighted_rank))
    else:
        selected_index = len(ordered) - 1
    selected = ordered[selected_index]
    texture_code = usda_texture_class(*(selected[field] for field in TEXTURE_FIELDS))
    macro_class = 101 if texture_code <= 3 else 102 if texture_code <= 6 else 103
    return {
        "usda_texture_code": texture_code,
        "macro_texture_class": macro_class,
        **CAPILLARY_PARAMETERS[macro_class],
    }


def _read_cell_landuse_ids(path, ogr):
    database = ogr.Open(str(path))
    layer = database.GetLayerByName("simulation_cells") if database else None
    if layer is None:
        raise ValueError(f"Missing simulation_cells layer in {path}")
    definition = layer.GetLayerDefn()
    fields = {
        definition.GetFieldDefn(index).GetName()
        for index in range(definition.GetFieldCount())
    }
    if "landuse_id" not in fields:
        raise ValueError(f"Simulation cells layer has no landuse_id field: {path}")
    identifiers = {
        int(feature.GetField("landuse_id"))
        for feature in layer
        if feature.IsFieldSetAndNotNull("landuse_id")
    }
    layer = database = None
    return identifiers


def _read_grid(path, gdal):
    dataset = gdal.Open(str(path))
    if dataset is None:
        raise ValueError(f"Could not open raster: {path}")
    band = dataset.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    result = _Grid(
        band.ReadAsArray(),
        tuple(dataset.GetGeoTransform()),
        dataset.GetProjectionRef(),
        float(nodata if nodata is not None else NODATA),
    )
    band = dataset = None
    return result


def _validate_aligned(grids):
    first = grids[0]
    for grid in grids[1:]:
        if (
            grid.values.shape != first.values.shape
            or any(abs(a - b) > 1e-8 for a, b in zip(grid.geotransform, first.geotransform))
            or grid.projection != first.projection
        ):
            raise ValueError("Cell-builder rasters are not exactly aligned.")
    if abs(first.geotransform[1] + first.geotransform[5]) > 1e-8 or first.geotransform[2] or first.geotransform[4]:
        raise ValueError("IdrAgra v2 export requires north-up square grid cells.")


def _read_hydraulic_profiles(path, ogr):
    database = ogr.Open(str(path), 0)
    layer = database.GetLayerByName("soil_hydraulic_layers") if database else None
    metadata_layer = database.GetLayerByName("soil_hydraulic_metadata") if database else None
    if layer is None:
        raise ValueError(f"Missing soil_hydraulic_layers table in {path}")
    required = {
        "profile_id", "top_cm", "bottom_cm", *HYDRAULIC_FIELDS, *TEXTURE_FIELDS
    }
    fields = {layer.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(layer.GetLayerDefn().GetFieldCount())}
    if missing := sorted(required - fields):
        raise ValueError(f"Hydraulic table is missing field(s): {missing}")
    profiles = {}
    for feature in layer:
        row = {name: float(feature.GetField(name)) for name in required - {"profile_id"}}
        profiles.setdefault(int(feature.GetField("profile_id")), []).append(row)
    metadata = {}
    if metadata_layer is not None and metadata_layer.GetFeatureCount():
        feature = next(iter(metadata_layer))
        metadata = {
            metadata_layer.GetLayerDefn().GetFieldDefn(i).GetName(): feature.GetField(i)
            for i in range(metadata_layer.GetLayerDefn().GetFieldCount())
        }
    database = None
    return profiles, metadata


def _profile_lookup_grid(ids, mask, profiles, layer_index, field, np):
    result = np.full(ids.shape, NODATA, dtype=float)
    for profile_id in np.unique(ids[mask]):
        result[mask & (ids == profile_id)] = profiles[int(profile_id)][layer_index][field]
    return result


def _simple_profile_lookup_grid(ids, mask, profiles, field, np):
    result = np.full(ids.shape, NODATA, dtype=float)
    for profile_id in np.unique(ids[mask]):
        result[mask & (ids == profile_id)] = profiles[int(profile_id)][field]
    return result


def _hydrologic_group_grid(ids, mask, profiles, np):
    result = np.full(ids.shape, NODATA, dtype=float)
    for profile_id in np.unique(ids[mask]):
        rows = profiles[int(profile_id)]
        values = [row["ksat_mm_h"] for row in rows if row["top_cm"] < 100 and row["bottom_cm"] > 0]
        minimum = min(values)
        group = 1 if minimum > 144 else 2 if minimum > 36 else 3 if minimum > 3.6 else 4
        result[mask & (ids == profile_id)] = group
    return result


def _read_weather_stations(path, grid, elevation, ogr, osr):
    database = ogr.Open(str(path), 0)
    layer = database.GetLayerByName("weather_daily_points") if database else None
    if layer is None:
        raise ValueError(f"Missing weather_daily_points layer in {path}")
    fields = {layer.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(layer.GetLayerDefn().GetFieldCount())}
    required = {"date", "location_id", *WEATHER_FIELDS}
    if missing := sorted(required - fields):
        raise ValueError(f"Weather layer is missing field(s): {missing}")
    source_srs = layer.GetSpatialRef()
    target_srs = osr.SpatialReference()
    target_srs.ImportFromWkt(grid.projection)
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    axis = getattr(osr, "OAMS_TRADITIONAL_GIS_ORDER", None)
    for srs in (source_srs, target_srs, wgs84):
        if srs is not None and axis is not None:
            srs.SetAxisMappingStrategy(axis)
    to_grid = osr.CoordinateTransformation(source_srs, target_srs)
    to_wgs = osr.CoordinateTransformation(source_srs, wgs84)
    grouped = {}
    coordinates = {}
    for feature in layer:
        location = str(feature.GetField("location_id")).strip()
        day = date.fromisoformat(str(feature.GetField("date")))
        values = tuple(float(feature.GetField(field)) for field in WEATHER_FIELDS)
        if any(not math.isfinite(value) for value in values):
            raise ValueError(f"Weather has a non-finite value at {location}, {day}.")
        if day in grouped.setdefault(location, {}):
            raise ValueError(f"Duplicate weather row for {location}, {day}.")
        grouped[location][day] = values
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            raise ValueError(f"Weather row has no point geometry: {location}, {day}.")
        coordinates.setdefault(location, (geometry.GetX(), geometry.GetY()))
    if not grouped:
        raise ValueError("Normalized weather has no rows.")
    starts = {min(rows) for rows in grouped.values()}
    ends = {max(rows) for rows in grouped.values()}
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError("All weather stations must have the same date coverage.")
    start, end = starts.pop(), ends.pop()
    expected_days = (end - start).days + 1
    stations = []
    for station_id, location in enumerate(sorted(grouped), start=1):
        rows = grouped[location]
        if len(rows) != expected_days or any(start.fromordinal(start.toordinal() + i) not in rows for i in range(expected_days)):
            raise ValueError(f"Weather station {location!r} is not a complete daily series.")
        x0, y0 = coordinates[location]
        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint_2D(x0, y0)
        projected = point.Clone()
        projected.Transform(to_grid)
        geographic = point.Clone()
        geographic.Transform(to_wgs)
        altitude = _sample_grid(elevation, projected.GetX(), projected.GetY())
        stations.append(
            _Station(
                location,
                station_id,
                f"station_{station_id:03d}.dat",
                projected.GetX(),
                projected.GetY(),
                geographic.GetY(),
                altitude,
                tuple((day, rows[day]) for day in sorted(rows)),
            )
        )
    database = None
    return tuple(stations), start, end


def _sample_grid(grid, x, y):
    gt = grid.geotransform
    column = int(math.floor((x - gt[0]) / gt[1]))
    row = int(math.floor((y - gt[3]) / gt[5]))
    if not (0 <= row < grid.values.shape[0] and 0 <= column < grid.values.shape[1]):
        return 0.0
    value = float(grid.values[row, column])
    return 0.0 if value == grid.nodata or not math.isfinite(value) else value


def _weather_weight_grids(grid, mask, stations, count, np):
    rows, columns = grid.values.shape
    gt = grid.geotransform
    result = [np.full((rows, columns), NODATA, dtype=float) for _ in range(count)]
    if len(stations) == 1:
        encoded = stations[0].station_id + 0.5
        for values in result:
            values[mask] = encoded
        return result
    for row, column in zip(*np.where(mask)):
        x = gt[0] + (column + 0.5) * gt[1]
        y = gt[3] + (row + 0.5) * gt[5]
        nearest = sorted(stations, key=lambda item: ((item.x - x) ** 2 + (item.y - y) ** 2, item.station_id))[:count]
        distances = np.asarray([math.hypot(item.x - x, item.y - y) for item in nearest])
        if np.any(distances < 1e-10):
            weights = np.full(count, 1e-6 / max(count - 1, 1))
            weights[int(np.argmin(distances))] = 1.0 - weights.sum()
        else:
            inverse = 1.0 / distances
            weights = inverse / inverse.sum()
            if float(weights.max()) >= 0.9999999995:
                winner = int(np.argmax(weights))
                weights = np.full(count, 1e-9 / max(count - 1, 1))
                weights[winner] = 1.0 - 1e-9
        for index, (station, weight) in enumerate(zip(nearest, weights)):
            result[index][row, column] = station.station_id + float(weight)
    return result


def _write_ascii(path, values, *, geotransform, nodata, integer=False, decimals=6):
    rows, columns = values.shape
    xll = geotransform[0]
    yll = geotransform[3] + rows * geotransform[5]
    with path.open("w", encoding="ascii", newline="\n") as stream:
        stream.write(
            f"ncols {columns}\nnrows {rows}\nxllcorner {xll:.12g}\nyllcorner {yll:.12g}\ncellsize {geotransform[1]:.12g}\nNODATA_value {int(nodata)}\n"
        )
        for row in values:
            if integer:
                stream.write(" ".join(str(int(round(value))) for value in row) + "\n")
            else:
                stream.write(
                    " ".join(
                        str(int(nodata)) if value == nodata or not math.isfinite(float(value)) else f"{float(value):.{decimals}f}"
                        for value in row
                    )
                    + "\n"
                )


def _write_station_file(path, station, start, end):
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(f"Id station: {station.station_id}, location: {station.source_id}\n")
        stream.write(f"{station.latitude:.6f}  {station.altitude:.1f}\n")
        stream.write(f"{start:%d/%m/%Y} -> {end:%d/%m/%Y}\n")
        stream.write("T_max   T_min   P_tot   U_max   U_min   V_med   RG_CORR\n")
        for _day, values in station.rows:
            stream.write("".join(f"{value:9.3f}" for value in values) + "\n")


def _write_station_list(path, stations):
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("# Generated by IdrAgra Input Gatherer\n")
        stream.write(f"StatNum = {len(stations)}\nTable =\nFileName X Y\n")
        for station in stations:
            stream.write(f"{station.filename} {station.x:.3f} {station.y:.3f}\n")
        stream.write("endTable =\n")


def _write_landuses(
    output, crops, landuses, active_ids, exported_id_by_landuse, source_folder, warnings, *, on_write=None,
):
    crop_by_id = {item.crop_id: item for item in crops}
    crop_output = output / "crop_parameters"
    crop_output.mkdir(exist_ok=True)
    if on_write:
        on_write(output / "soil_uses.txt")
    with (output / "soil_uses.txt").open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("Cr_ID\tCrop1\tCrop2\t# Comments\n")
        for item in sorted(landuses, key=lambda value: value.landuse_id):
            if item.landuse_id not in active_ids:
                continue
            crop1 = crop_by_id[item.crop1_id].parameter_file
            crop2 = crop_by_id[item.crop2_id].parameter_file if item.crop2_id else "*"
            exported_id = exported_id_by_landuse[item.landuse_id]
            stream.write(f"{exported_id}\t{Path(crop1).name}\t{Path(crop2).name}\t# {item.name}\n")
        stream.write("endTable =\n")
    needed = {
        crop_by_id[crop_id].parameter_file
        for item in landuses
        if item.landuse_id in active_ids
        for crop_id in (item.crop1_id, item.crop2_id)
        if crop_id
    }
    search_folders = [source_folder] if source_folder else []
    for reference in sorted(needed):
        source = Path(reference)
        candidates = [source] if source.is_absolute() else [folder / source for folder in search_folders]
        found = next((candidate for candidate in candidates if candidate.is_file()), None)
        if found is None:
            warnings.append(f"Crop parameter file was not found and was not copied: {reference}")
        else:
            if on_write:
                on_write(crop_output / source.name)
            shutil.copy2(found, crop_output / source.name)


# writes idragra_parameters.txt (todo: doublecheck)
def _write_parameter_template(path, start, end, stations, weight_count, active_ids, evap, root_layer, profile_layers, np):
    first = np.asarray([layers[0]["ksat_mm_h"] for layers in profile_layers.values()])
    second = np.asarray([layers[1]["ksat_mm_h"] for layers in profile_layers.values()])
    q1 = np.quantile(first, (0.1, 0.9)) / 10.0
    q2 = np.quantile(second, (0.1, 0.9)) / 10.0
    text = f"""# IdrAgra v2 static rain-fed template generated by IdrAgra Input Gatherer
# IMPORTANT: generate pheno_station_NNN directories with CropCoef before running.
OutputPath = simout\\
InputPath = geodata\\
MeteoPath = meteodata\\
MeteoFileName = weather_stations.dat
PhenoPath = pheno\\
PhenoFileRoot = pheno_
IrrMethPath = irrmethods\\
IrrMethFileName = irrmethods.txt
WatSourPath = wsources\\
Mode = 0
InitialThetaFlag = F
FinalThetaFlag = F
StartSimulation = {start:%d/%m/%Y}
EndSimulation = {end:%d/%m/%Y}
CapillaryFlag = F
SoilUseVarFlag = F
MeteoStatTotNum = {len(stations)}
MeteoStatWeightNum = {weight_count}
SoilUsesNum = {max(active_ids)}
SimulatedSoilUses = {" ".join(map(str, active_ids))}
RandSowDaysSym = symmetric
RandSowDaysWind = 0
Repeatable = T
Forecast_day = 1
MonthlyFlag = monthly
01q_eva = {q1[0]:.6f}
09q_eva = {q1[1]:.6f}
01q_trasp = {q2[0]:.6f}
09q_trasp = {q2[1]:.6f}
zEvap = {evap:.6f}
zRoot = {root_layer:.6f}
LambdaCN = 0.20
lim_prec = 0.10
h_maxpond = 0.0
fc_ratio = 1.0
DTxMode = none
DTxNumXs = 3
DTx_x = 10 20 30
DTxDeltaDate = 10
DTxDelayDays = 1
DTxMinCard = 3
"""
    path.write_text(text, encoding="ascii")


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_tree(path):
    if path.exists():
        shutil.rmtree(path)
