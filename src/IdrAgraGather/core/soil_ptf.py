"""Pedotransfer functions for normalized IdrAgra soil profiles.

The normalized soil layer remains source data.  PTF results are written to a
separate, long-form GeoPackage keyed by ``profile_id`` and horizon so several
methods can share one stable schema and a later IdrAgra exporter can consume it.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np

from .manifest import Manifest, sha256_file
from .soilgrids_normalize import DEPTH_BOUNDS


OUTPUT_NAME = "soil_hydraulics.gpkg"
METADATA_LAYER = "soil_hydraulic_metadata"
LAYERS_LAYER = "soil_hydraulic_layers"
ROSETTA_IMPLEMENTATION = "USDA rosetta-soil 0.3.2 model assets"
ROSETTA_CITATION = (
    "Zhang and Schaap (2017), Weighted recalibration of the Rosetta "
    "pedotransfer model with improved estimates of hydraulic parameter "
    "distributions and summary statistics, J. Hydrol. 547, 39-53"
)
PRESSURE_HEAD_MM_PER_KPA = 101.9716212978

INPUT_FIELD_INFO = {
    "sand_pct": ("Sand", "% fine earth"),
    "silt_pct": ("Silt", "% fine earth"),
    "clay_pct": ("Clay", "% fine earth"),
    "bd_g_cm3": ("Bulk density", "g/cm3"),
    "skel_pct": ("Skeleton / coarse fragments", "vol. %"),
    "oc_pct": ("Organic carbon", "mass %"),
}
OUTPUT_FIELD_INFO = {
    "ksat_mm_h": ("Saturated conductivity", "mm/h"),
    "theta_sat": ("Saturated water content", "m3/m3"),
    "theta_fc": ("Water content at 33 kPa (field capacity)", "m3/m3"),
    "theta_wp": ("Water content at 1500 kPa (wilting point)", "m3/m3"),
    "theta_res": ("Residual water content", "m3/m3"),
    "vg_alpha": ("van Genuchten alpha", "1/mm"),
    "vg_n": ("van Genuchten n", "-"),
    "vg_m": ("van Genuchten m = 1 - 1/n", "-"),
}


@dataclass(frozen=True)
class PtfFieldGroups:
    required: tuple[str, ...]
    generated: tuple[str, ...]
    unused: tuple[str, ...]


@dataclass(frozen=True)
class RosettaPrediction:
    """Rosetta ensemble statistics in the canonical IdrAgra units."""

    values: np.ndarray
    standard_deviations: np.ndarray
    fields: tuple[str, ...]
    model_code: int


@dataclass(frozen=True)
class SoilPtfResult:
    path: Path
    profile_count: int
    horizon_count: int
    model_code: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _SourceHorizon:
    profile_id: int
    horizon: int
    top_cm: int
    bottom_cm: int
    sand_pct: float
    silt_pct: float
    clay_pct: float
    skel_pct: float
    oc_pct: float
    bd_g_cm3: float


def rosetta_field_groups(*, use_bulk_density: bool = True) -> PtfFieldGroups:
    """Describe method inputs/outputs for both the dialog and provenance."""

    required = ["sand_pct", "silt_pct", "clay_pct"]
    unused = ["skel_pct", "oc_pct"]
    if use_bulk_density:
        required.append("bd_g_cm3")
    else:
        unused.append("bd_g_cm3")
    return PtfFieldGroups(
        tuple(required), tuple(OUTPUT_FIELD_INFO), tuple(unused)
    )


def predict_rosetta3(
    samples: Sequence[Sequence[float]] | np.ndarray,
    *,
    use_bulk_density: bool = True,
) -> RosettaPrediction:
    """Predict Rosetta 3 H2/H3 parameters using arithmetic ensemble means.

    Input columns are sand, silt and clay percentages, followed by bulk density
    in g/cm3 for H3.  Rosetta's alpha (1/cm) and Ksat (cm/day) are converted to
    the canonical 1/mm and mm/h used by this project.
    """

    values = np.asarray(samples, dtype=np.float64)
    if values.ndim == 1:
        values = values[np.newaxis, :]
    model_code = 3 if use_bulk_density else 2
    expected_columns = 4 if use_bulk_density else 3
    if values.ndim != 2 or values.shape[1] != expected_columns:
        raise ValueError(
            f"Rosetta H{model_code} input must have {expected_columns} columns"
        )
    if not np.all(np.isfinite(values)):
        raise ValueError("Rosetta inputs must all be finite numbers")
    texture = values[:, :3]
    texture_total = np.sum(texture, axis=1)
    invalid_texture = (
        np.any(texture < 0.0, axis=1)
        | (texture_total < 99.0)
        | (texture_total > 101.0)
    )
    if np.any(invalid_texture):
        indices = ", ".join(str(index + 1) for index in np.flatnonzero(invalid_texture))
        raise ValueError(
            "Rosetta requires non-negative sand/silt/clay summing to 99-101%; "
            f"invalid input row(s): {indices}"
        )
    if use_bulk_density:
        invalid_density = (values[:, 3] < 0.5) | (values[:, 3] > 2.0)
        if np.any(invalid_density):
            indices = ", ".join(
                str(index + 1) for index in np.flatnonzero(invalid_density)
            )
            raise ValueError(
                "Rosetta H3 requires bulk density in the range 0.5-2.0 g/cm3; "
                f"invalid input row(s): {indices}"
            )

    boot = _rosetta_bootstrap(model_code, values)
    # The neural network emits theta_r, theta_s, log10(alpha [1/cm]),
    # log10(n), and log10(Ksat [cm/day]).
    boot[:, :, 2:] = np.power(10.0, boot[:, :, 2:])
    mean = np.mean(boot, axis=0)
    std = np.std(boot, axis=0, ddof=1)
    mean[:, 2] /= 10.0
    std[:, 2] /= 10.0
    mean[:, 4] *= 10.0 / 24.0
    std[:, 4] *= 10.0 / 24.0
    return RosettaPrediction(
        mean,
        std,
        ("theta_res", "theta_sat", "vg_alpha", "vg_n", "ksat_mm_h"),
        model_code,
    )


def van_genuchten_theta(
    pressure_kpa: float | np.ndarray,
    theta_res: float | np.ndarray,
    theta_sat: float | np.ndarray,
    vg_alpha: float | np.ndarray,
    vg_n: float | np.ndarray,
) -> np.ndarray:
    """Evaluate the constrained van Genuchten curve (m = 1 - 1/n)."""

    pressure = np.asarray(pressure_kpa, dtype=np.float64)
    residual = np.asarray(theta_res, dtype=np.float64)
    saturated = np.asarray(theta_sat, dtype=np.float64)
    alpha = np.asarray(vg_alpha, dtype=np.float64)
    n_value = np.asarray(vg_n, dtype=np.float64)
    m_value = 1.0 - 1.0 / n_value
    head_mm = np.abs(pressure) * PRESSURE_HEAD_MM_PER_KPA
    return residual + (saturated - residual) / np.power(
        1.0 + np.power(alpha * head_mm, n_value), m_value
    )


def apply_rosetta3_to_workspace(
    output_root: str | Path,
    *,
    source_path: str | Path | None = None,
    use_bulk_density: bool = True,
    on_status: Callable[[str], None] | None = None,
) -> SoilPtfResult:
    """Apply Rosetta 3 to every profile/horizon in a normalized workspace."""

    root = Path(output_root).resolve()
    source = Path(source_path or root / "soil" / "soil_profiles.gpkg").resolve()
    if not source.is_file():
        raise ValueError(f"Normalized soil profiles were not found: {source}")
    if on_status is not None:
        on_status("Reading unique normalized soil profiles.")
    horizons = _read_source_horizons(source)
    groups = rosetta_field_groups(use_bulk_density=use_bulk_density)
    samples = np.asarray(
        [
            [
                item.sand_pct,
                item.silt_pct,
                item.clay_pct,
                *([item.bd_g_cm3] if use_bulk_density else []),
            ]
            for item in horizons
        ],
        dtype=np.float64,
    )
    if on_status is not None:
        on_status(
            f"Running Rosetta 3 H{3 if use_bulk_density else 2} for "
            f"{len(horizons)} profile horizon(s)."
        )
    prediction = predict_rosetta3(samples, use_bulk_density=use_bulk_density)
    generated = _canonical_rows(horizons, prediction)
    output = root / "soil" / OUTPUT_NAME
    output.parent.mkdir(parents=True, exist_ok=True)
    if on_status is not None:
        on_status("Writing hydraulic results and method provenance.")
    _write_hydraulic_geopackage(
        output,
        source,
        horizons,
        generated,
        prediction,
        use_bulk_density=use_bulk_density,
    )
    warnings = (
        "Rosetta does not use organic carbon in H2/H3; oc_pct was retained "
        "only as source context.",
        "No coarse-fragment correction was applied; skel_pct was retained "
        "only as source context.",
    )
    Manifest(root).add_asset(
        output,
        category="soil",
        provider="idragather-rosetta3",
        dataset="current soil hydraulic PTF result",
        source=str(source),
        request={
            "method": "rosetta3",
            "model_code": prediction.model_code,
            "model_hierarchy": f"H{prediction.model_code}",
            "implementation": ROSETTA_IMPLEMENTATION,
            "citation": ROSETTA_CITATION,
            "estimate": "arithmetic mean of 1000 bootstrap neural networks",
            "required_fields": list(groups.required),
            "generated_fields": list(groups.generated),
            "unused_fields": list(groups.unused),
            "units": {name: unit for name, (_, unit) in OUTPUT_FIELD_INFO.items()},
            "theta_fc_tension_kpa": 33.0,
            "theta_wp_tension_kpa": 1500.0,
            "vg_constraint": "m = 1 - 1/n",
            "coarse_fragment_correction": False,
            "source_sha256": sha256_file(source),
        },
    )
    return SoilPtfResult(
        output,
        len({item.profile_id for item in horizons}),
        len(horizons),
        prediction.model_code,
        warnings,
    )


def _canonical_rows(
    horizons: Sequence[_SourceHorizon], prediction: RosettaPrediction
) -> list[dict[str, float]]:
    field_index = {name: index for index, name in enumerate(prediction.fields)}
    rows: list[dict[str, float]] = []
    for row_index, _item in enumerate(horizons):
        direct = {
            name: float(prediction.values[row_index, index])
            for name, index in field_index.items()
        }
        direct["vg_m"] = 1.0 - 1.0 / direct["vg_n"]
        direct["theta_fc"] = float(
            van_genuchten_theta(
                33.0,
                direct["theta_res"],
                direct["theta_sat"],
                direct["vg_alpha"],
                direct["vg_n"],
            )
        )
        direct["theta_wp"] = float(
            van_genuchten_theta(
                1500.0,
                direct["theta_res"],
                direct["theta_sat"],
                direct["vg_alpha"],
                direct["vg_n"],
            )
        )
        for name, index in field_index.items():
            direct[name + "_sd"] = float(
                prediction.standard_deviations[row_index, index]
            )
        rows.append(direct)
    return rows


def _read_source_horizons(path: Path) -> list[_SourceHorizon]:
    try:
        from osgeo import ogr
    except ImportError as exc:
        raise RuntimeError("Soil PTF application requires GDAL/OGR in QGIS") from exc

    database = ogr.Open(str(path), 0)
    if database is None:
        raise RuntimeError(f"Could not open normalized soil profiles: {path}")
    layer = database.GetLayerByName("soil_profiles")
    if layer is None:
        database = None
        raise ValueError(f"{path} has no 'soil_profiles' layer")
    required = ["profile_id"]
    for horizon in range(1, len(DEPTH_BOUNDS) + 1):
        required.extend(
            [
                f"h{horizon}_top_cm",
                f"h{horizon}_bottom_cm",
                *(f"h{horizon}_{name}" for name in INPUT_FIELD_INFO),
            ]
        )
    definition = layer.GetLayerDefn()
    missing = [name for name in required if definition.GetFieldIndex(name) < 0]
    if missing:
        database = None
        raise ValueError(
            "Normalized soil layer is missing required field(s): " + ", ".join(missing)
        )

    profiles: dict[int, tuple[tuple[float, ...], ...]] = {}
    layer.ResetReading()
    for feature in layer:
        profile_id = int(feature.GetField("profile_id"))
        values = tuple(
            tuple(
                float(feature.GetField(f"h{horizon}_{name}"))
                for name in (
                    "top_cm",
                    "bottom_cm",
                    "sand_pct",
                    "silt_pct",
                    "clay_pct",
                    "skel_pct",
                    "oc_pct",
                    "bd_g_cm3",
                )
            )
            for horizon in range(1, len(DEPTH_BOUNDS) + 1)
        )
        previous = profiles.get(profile_id)
        if previous is not None and previous != values:
            database = None
            raise ValueError(
                f"Normalized polygons with profile_id {profile_id} have inconsistent attributes"
            )
        profiles[profile_id] = values
    database = None
    if not profiles:
        raise ValueError("Normalized soil layer contains no profiles")

    result: list[_SourceHorizon] = []
    for profile_id, profile in sorted(profiles.items()):
        for horizon, values in enumerate(profile, start=1):
            result.append(
                _SourceHorizon(
                    profile_id,
                    horizon,
                    int(values[0]),
                    int(values[1]),
                    *values[2:],
                )
            )
    return result


def _write_hydraulic_geopackage(
    path: Path,
    source: Path,
    horizons: Sequence[_SourceHorizon],
    generated: Sequence[Mapping[str, float]],
    prediction: RosettaPrediction,
    *,
    use_bulk_density: bool,
) -> None:
    try:
        from osgeo import gdal, ogr
    except ImportError as exc:
        raise RuntimeError("Soil PTF application requires GDAL/OGR in QGIS") from exc

    gdal.UseExceptions()
    ogr.UseExceptions()
    driver = ogr.GetDriverByName("GPKG")
    working = path.with_name(path.stem + ".tmp.gpkg")
    working.unlink(missing_ok=True)
    database = driver.CreateDataSource(str(working))
    if database is None:
        raise RuntimeError(f"Could not open hydraulic GeoPackage for writing: {working}")

    try:
        groups = rosetta_field_groups(use_bulk_density=use_bulk_density)
        metadata = _create_table(
            database,
            ogr,
            METADATA_LAYER,
            (
                ("method", ogr.OFTString),
                ("model_code", ogr.OFTInteger),
                ("use_bd", ogr.OFTInteger),
                ("created_utc", ogr.OFTString),
                ("source_path", ogr.OFTString),
                ("source_sha256", ogr.OFTString),
                ("implementation", ogr.OFTString),
                ("citation", ogr.OFTString),
                ("estimate", ogr.OFTString),
                ("required_fields", ogr.OFTString),
                ("generated_fields", ogr.OFTString),
                ("unused_fields", ogr.OFTString),
                ("theta_fc_kpa", ogr.OFTReal),
                ("theta_wp_kpa", ogr.OFTReal),
                ("vg_constraint", ogr.OFTString),
                ("cf_correction", ogr.OFTInteger),
            ),
        )
        layers = _create_table(
            database,
            ogr,
            LAYERS_LAYER,
            (
                ("profile_id", ogr.OFTInteger),
                ("horizon", ogr.OFTInteger),
                ("top_cm", ogr.OFTInteger),
                ("bottom_cm", ogr.OFTInteger),
                ("sand_pct", ogr.OFTReal),
                ("silt_pct", ogr.OFTReal),
                ("clay_pct", ogr.OFTReal),
                ("skel_pct", ogr.OFTReal),
                ("oc_pct", ogr.OFTReal),
                ("bd_g_cm3", ogr.OFTReal),
                ("model_code", ogr.OFTInteger),
                *((name, ogr.OFTReal) for name in OUTPUT_FIELD_INFO),
                ("theta_res_sd", ogr.OFTReal),
                ("theta_sat_sd", ogr.OFTReal),
                ("vg_alpha_sd", ogr.OFTReal),
                ("vg_n_sd", ogr.OFTReal),
                ("ksat_mm_h_sd", ogr.OFTReal),
            ),
        )
        database.StartTransaction()
        try:
            _create_feature(
                metadata,
                ogr,
                {
                    "method": "rosetta3",
                    "model_code": prediction.model_code,
                    "use_bd": int(use_bulk_density),
                    "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "source_path": str(source),
                    "source_sha256": sha256_file(source),
                    "implementation": ROSETTA_IMPLEMENTATION,
                    "citation": ROSETTA_CITATION,
                    "estimate": "arithmetic mean of 1000 bootstrap networks",
                    "required_fields": json.dumps(groups.required),
                    "generated_fields": json.dumps(tuple(OUTPUT_FIELD_INFO)),
                    "unused_fields": json.dumps(groups.unused),
                    "theta_fc_kpa": 33.0,
                    "theta_wp_kpa": 1500.0,
                    "vg_constraint": "m = 1 - 1/n",
                    "cf_correction": 0,
                },
            )
            for item, values in zip(horizons, generated):
                row = {
                    "profile_id": item.profile_id,
                    "horizon": item.horizon,
                    "top_cm": item.top_cm,
                    "bottom_cm": item.bottom_cm,
                    "sand_pct": item.sand_pct,
                    "silt_pct": item.silt_pct,
                    "clay_pct": item.clay_pct,
                    "skel_pct": item.skel_pct,
                    "oc_pct": item.oc_pct,
                    "bd_g_cm3": item.bd_g_cm3,
                    "model_code": prediction.model_code,
                    **values,
                }
                _create_feature(layers, ogr, row)
            database.ExecuteSQL(
                "CREATE INDEX IF NOT EXISTS soil_hydraulic_layers_profile "
                "ON soil_hydraulic_layers (profile_id, horizon)"
            )
            database.CommitTransaction()
        except Exception:
            database.RollbackTransaction()
            raise
    except Exception:
        database = None
        working.unlink(missing_ok=True)
        raise
    finally:
        database = None

    try:
        working.replace(path)
    except PermissionError as exc:
        working.unlink(missing_ok=True)
        raise RuntimeError(
            "Could not replace the hydraulic GeoPackage because it is open in QGIS "
            f"or another application: {path}"
        ) from exc


def _create_table(database, ogr, name: str, fields: Iterable[tuple[str, int]]):
    layer = database.CreateLayer(name, None, ogr.wkbNone)
    if layer is None:
        raise RuntimeError(f"Could not create {name!r} in hydraulic GeoPackage")
    for field_name, field_type in fields:
        field_definition = ogr.FieldDefn(field_name, field_type)
        if field_type == ogr.OFTString:
            field_definition.SetWidth(512)
        if layer.CreateField(field_definition) != 0:
            raise RuntimeError(f"Could not create field {name}.{field_name}")
    return layer


def _create_feature(layer, ogr, values: Mapping[str, object]) -> None:
    feature = ogr.Feature(layer.GetLayerDefn())
    for name, value in values.items():
        feature.SetField(name, value)
    if layer.CreateFeature(feature) != 0:
        raise RuntimeError(f"Could not write a row to {layer.GetName()}")
    feature = None


@lru_cache(maxsize=2)
def _load_rosetta_model(model_code: int) -> dict[str, object]:
    if model_code not in (2, 3):
        raise ValueError("This implementation includes only Rosetta 3 H2 and H3")
    path = Path(__file__).with_name("_rosetta_data") / f"rose3_mod{model_code}_0.npz"
    if not path.is_file():
        raise RuntimeError(f"Bundled Rosetta model asset is missing: {path}")
    with np.load(path, allow_pickle=True) as archive:
        return {name: archive[name].copy() for name in archive.files}


def _rosetta_bootstrap(model_code: int, inputs: np.ndarray) -> np.ndarray:
    model = _load_rosetta_model(model_code)
    scaled = np.zeros_like(inputs)
    for index, metadata in enumerate(model["inputs"].tolist()):
        scaled[:, index] = _scale_values(
            inputs[:, index], metadata["scale"], metadata["params"]
        )

    weights = model["weights"].tolist()
    biases = model["biases"].tolist()
    layers = model["layers"].tolist()
    activations = scaled[np.newaxis, :, :]
    for layer_index, metadata in enumerate(layers):
        stacked_weights = np.stack(
            [np.asarray(item[layer_index]).T for item in weights]
        )
        stacked_biases = np.stack(
            [np.atleast_2d(np.asarray(item[layer_index])) for item in biases]
        )
        activations = _activate(
            np.matmul(activations, stacked_weights) + stacked_biases,
            metadata["activate"],
        )

    outputs = np.zeros_like(activations)
    for index, metadata in enumerate(model["outputs"].tolist()):
        outputs[:, :, index] = _scale_values(
            activations[:, :, index], metadata["scale"], metadata["params"]
        )
    return outputs


def _scale_values(values: np.ndarray, method: str, params: Sequence[float]) -> np.ndarray:
    method = method.casefold()
    if method == "asis":
        return values
    if method == "division":
        return values / params[0]
    if method == "logtr":
        return np.log10(values) / params[0] + params[1]
    if method == "affine":
        return params[0] * (values + params[1])
    if method == "minmax":
        return (values - params[0]) * params[1] + params[2]
    if method == "linear":
        return values * params[0] + params[1]
    raise ValueError(f"Unsupported Rosetta scaler: {method}")


def _activate(values: np.ndarray, method: str) -> np.ndarray:
    method = method.casefold()
    if method == "relu":
        return np.maximum(0.0, values)
    if method == "sigmoid":
        return 1.0 / (1.0 + np.exp(-values))
    if method == "tansig":
        return -1.0 + 2.0 / (1.0 + np.exp(-2.0 * values))
    if method == "linear":
        return values.copy()
    raise ValueError(f"Unsupported Rosetta activation: {method}")
