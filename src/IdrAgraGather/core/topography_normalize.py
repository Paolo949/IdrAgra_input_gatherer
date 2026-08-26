from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Iterable

from .manifest import Manifest
from .models import BoundingBox


ELEVATION_NAME = "elevation_m_asl.tif"
SLOPE_NAME = "slope_pct.tif"
NODATA = -9999.0
MODIFIED_ATTRIBUTION = {
    "COPERNICUS_30": (
        "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus "
        "Defence and Space GmbH 2014-2018 provided under COPERNICUS by the "
        "European Union and ESA; all rights reserved"
    ),
    "COPERNICUS_90": (
        "produced using Copernicus WorldDEM-90 © DLR e.V. 2010-2014 and © Airbus "
        "Defence and Space GmbH 2014-2018 provided under COPERNICUS by the "
        "European Union and ESA; all rights reserved"
    ),
}


@dataclass(frozen=True)
class TopographyNormalizationResult:
    elevation_path: Path
    slope_path: Path
    target_crs: str
    resolution_m: float

    @property
    def paths(self) -> tuple[Path, Path]:
        return self.elevation_path, self.slope_path


# Mosaic, clip and reproject a DEM, then derive percent slope.
#
# The elevation raster is kept in orthometric metres above mean sea level.
# A local metric CRS is essential here: terrain slope must not be calculated
# with longitude/latitude degrees as horizontal units.
def normalize_dem_files(
    source_paths: Iterable[str | Path],
    output_root: str | Path,
    *,
    bbox: BoundingBox,
    resolution_m: float = 30.0,
    dem_instance: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> TopographyNormalizationResult:
    sources = tuple(Path(path).resolve() for path in source_paths)
    if not sources:
        raise ValueError("At least one DEM input is required.")
    missing = [path for path in sources if not path.is_file()]
    if missing:
        raise ValueError(f"DEM input does not exist: {missing[0]}")
    if resolution_m <= 0:
        raise ValueError("Topography resolution must be positive.")
    try:
        from osgeo import gdal, osr
    except ImportError as exc:
        raise RuntimeError("Topography normalization requires GDAL in QGIS") from exc

    gdal.UseExceptions()
    osr.UseExceptions()
    for source in sources:
        dataset = gdal.Open(str(source), gdal.GA_ReadOnly)
        if dataset is None or dataset.RasterCount < 1:
            raise ValueError(f"GDAL could not read a DEM band from {source}")
        if not (dataset.GetProjectionRef() or "").strip():
            raise ValueError(f"DEM input has no coordinate reference system: {source}")
        dataset = None

    output_root = Path(output_root).resolve()
    output_dir = output_root / "topography"
    output_dir.mkdir(parents=True, exist_ok=True)
    elevation = output_dir / ELEVATION_NAME
    slope = output_dir / SLOPE_NAME
    target_crs = f"EPSG:{utm_epsg_for_bbox(bbox)}"
    creation_options = [
        "TILED=YES",
        "COMPRESS=DEFLATE",
        "PREDICTOR=3",
        "BIGTIFF=IF_SAFER",
    ]

    if on_status:
        on_status(f"Mosaicking and clipping {len(sources)} DEM tile(s) in {target_crs} at {resolution_m:g} m.")
    with TemporaryDirectory(prefix=".topography-", dir=output_dir) as temporary:
        temporary_dir = Path(temporary)
        temporary_elevation = temporary_dir / ELEVATION_NAME
        temporary_slope = temporary_dir / SLOPE_NAME
        cutline = output_root / "aoi.geojson"
        if not cutline.is_file():
            raise ValueError(f"Workspace has no saved AOI polygon: {cutline}")
        warped = gdal.Warp(
            str(temporary_elevation),
            [str(path) for path in sources],
            options=gdal.WarpOptions(
                format="GTiff",
                dstSRS=target_crs,
                xRes=float(resolution_m),
                yRes=float(resolution_m),
                targetAlignedPixels=True,
                resampleAlg="bilinear",
                outputType=gdal.GDT_Float32,
                dstNodata=NODATA,
                cutlineDSName=str(cutline),
                cropToCutline=True,
                multithread=True,
                creationOptions=creation_options,
            ),
        )
        if warped is None:
            raise RuntimeError("GDAL failed to create the normalized elevation raster.")
        elevation_band = warped.GetRasterBand(1)
        elevation_band.SetDescription("elevation above mean sea level")
        elevation_band.SetUnitType("m")
        warped.SetMetadataItem("VERTICAL_REFERENCE", "EGM2008 orthometric height")
        warped.SetMetadataItem("SOURCE", "Copernicus DEM")
        if dem_instance in MODIFIED_ATTRIBUTION:
            warped.SetMetadataItem("COPYRIGHT", MODIFIED_ATTRIBUTION[dem_instance])
        warped.FlushCache()
        elevation_band = None
        warped = None

        if on_status:
            on_status("Calculating terrain slope in percent using the Horn algorithm.")
        slope_dataset = gdal.DEMProcessing(
            str(temporary_slope),
            str(temporary_elevation),
            "slope",
            options=gdal.DEMProcessingOptions(
                format="GTiff",
                slopeFormat="percent",
                computeEdges=True,
                alg="Horn",
                creationOptions=creation_options,
            ),
        )
        if slope_dataset is None:
            raise RuntimeError("GDAL failed to calculate the normalized slope raster.")
        slope_band = slope_dataset.GetRasterBand(1)
        slope_band.SetDescription("terrain slope")
        slope_band.SetUnitType("percent")
        slope_dataset.SetMetadataItem("SLOPE_FORMAT", "percent rise")
        slope_dataset.SetMetadataItem("ALGORITHM", "Horn")
        slope_dataset.FlushCache()
        slope_band = None
        slope_dataset = None

        _replace_outputs(
            (temporary_elevation, temporary_slope),
            (elevation, slope),
        )

    common_request = {
        "source_paths": [str(path) for path in sources],
        "clip_aoi": bbox.as_dict(),
        "clip_geometry": "aoi.geojson" if (output_root / "aoi.geojson").is_file() else None,
        "target_crs": target_crs,
        "resolution_m": float(resolution_m),
        "resampling": "bilinear",
        "nodata": NODATA,
        "dem_instance": dem_instance,
        "attribution": MODIFIED_ATTRIBUTION.get(dem_instance),
    }
    manifest = Manifest(output_root)
    manifest.add_asset(
        elevation,
        category="topography",
        provider="idragather",
        dataset="normalized elevation above mean sea level",
        source=";".join(str(path) for path in sources),
        request={
            **common_request,
            "variable": "elevation",
            "unit": "m",
            "vertical_reference": "EGM2008 orthometric height",
        },
    )
    manifest.add_asset(
        slope,
        category="topography",
        provider="idragather",
        dataset="normalized terrain slope",
        source=str(elevation),
        request={
            **common_request,
            "variable": "slope",
            "unit": "percent",
            "algorithm": "Horn",
        },
    )
    if on_status:
        on_status(f"Wrote {elevation.name} and {slope.name}.")
    return TopographyNormalizationResult(
        elevation,
        slope,
        target_crs,
        float(resolution_m),
    )


# Choose the local WGS 84 UTM CRS at the centre of a compact AOI.
def utm_epsg_for_bbox(bbox: BoundingBox) -> int:
    longitude = (bbox.west + bbox.east) / 2.0
    latitude = (bbox.south + bbox.north) / 2.0
    if not -80.0 <= latitude <= 84.0:
        raise ValueError(
            "Automatic metric topography normalization supports AOIs between 80°S and 84°N (the WGS 84 UTM coverage)."
        )
    zone = min(60, max(1, int((longitude + 180.0) // 6.0) + 1))
    return (32600 if latitude >= 0 else 32700) + zone


def _replace_outputs(temporary_paths: tuple[Path, ...], targets: tuple[Path, ...]) -> None:
    backup_directory = temporary_paths[0].parent / "old"
    backup_directory.mkdir()
    moved_old: list[tuple[Path, Path]] = []
    installed_new: list[Path] = []
    try:
        for target in targets:
            if target.exists():
                backup = backup_directory / target.name
                target.replace(backup)
                moved_old.append((target, backup))
        for temporary, target in zip(temporary_paths, targets):
            if not temporary.is_file():
                raise RuntimeError(f"GDAL did not create the expected raster: {temporary}")
            temporary.replace(target)
            installed_new.append(target)
    except PermissionError as exc:
        for installed in installed_new:
            installed.unlink(missing_ok=True)
        for target, backup in moved_old:
            if backup.exists():
                backup.replace(target)
        raise RuntimeError(
            "Could not replace normalized topography because a raster is open in QGIS or another application."
        ) from exc
    except Exception:
        for installed in installed_new:
            installed.unlink(missing_ok=True)
        for target, backup in moved_old:
            if backup.exists():
                backup.replace(target)
        raise
