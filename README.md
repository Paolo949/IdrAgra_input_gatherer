# IdrAgra input gatherer and QGIS prototype

This repository is a deliberately small acquisition core for a future QGIS
plugin. It does **not** export an IdrAgra project yet. Its job is to collect raw
inputs, preserve their provenance, and define an editable intermediate weather
table.

The core has no QGIS dependency. The included QGIS prototype calls those same
functions from a background `QgsTask`, while the command line remains useful
for tests and batch work.

## Try the QGIS interface

Install the archive whose name begins with `INSTALL_THIS_` using **Plugins → Manage and Install
Plugins → Install from ZIP**. After installation, use **IdrAgra → Gather
IdrAgra inputs…** or the new toolbar action.

Do not install the developer source archive. A valid plugin ZIP has exactly one
top-level directory named `IdrAgraGather`; QGIS then imports the Python package
with that same name.

The workspace lets the user:

1. drag a rectangle on the current map or use the canvas extent;
2. choose a weather period;
3. acquire ERA5-Land through CDS or E-OBS from the official KNMI files;
4. save the AOI and source-specific raw weather subsets;
5. optionally transform hourly ERA5-Land data into the seven daily IdrAgra
   weather fields using `Europe/Rome` civil time;
6. optionally stage local weather, soil, land-use, and elevation files;
7. download AOI-clipped SoilGrids mean texture and bulk-density coverages;
8. download CORINE Land Cover 2018 polygons intersecting the AOI as GeoJSON;
9. run weather acquisition and transformation independently or consecutively;
10. load raw layers into collapsed, visible category groups such as `weather_raw`
   and `soil_raw`; numeric rasters use a first-band pseudocolor stretch so time
   bands are not mistaken for RGB channels. Geographic NetCDF grids are detected
   from their longitude/latitude axes and assigned WGS 84 automatically. If a
   subset has lost its pixel geotransform, the plugin creates a lightweight VRT
   display wrapper under `.qgis_previews`; QGIS then reprojects it to the project
   CRS on the fly without modifying the acquired source file.

The gathered-inputs group is kept at the top of the project layer tree. Reloading
the workspace AOI replaces its prior map layer instead of adding a duplicate.
Transforming weather when a normalized GeoPackage already exists asks for
confirmation first and releases any loaded QGIS layer before replacing the file.
For running-year E-OBS data, acquisition reports the date interval actually
available across all six variables. Normalization uses that common interval and
records a warning when the provisional files end before the requested date.
Changing the dialog end date does not lose the staged acquisition: compatible
files are resolved from manifest provenance instead of an exact filename hash.

Source-specific controls use stacked pages: choosing ERA5-Land shows its period
and timezone, while choosing a local source shows only its file picker. Status
lines display the exact detected file or provider directory and can be refreshed
after files are changed outside QGIS. The output-loading checkbox applies only
to outputs returned by the action that has just completed; it does not scan and
load every pre-existing file in the staging folder.

ERA5 download mode requires `cdsapi` in QGIS's Python environment and valid CDS
credentials. E-OBS does not require credentials. It uses GDAL HTTP range reads
to save only the requested AOI/date subset from the much larger official
NetCDF files, and requires confirmation of the E-OBS non-commercial-use terms.

## Why the weather schema looks this way

The attached IdrAgra v3 source reads seven daily, cell-by-cell series and then
calculates ET0 internally. The required variables are therefore:

| Field | Canonical unit |
| --- | --- |
| `tmax_c`, `tmin_c` | °C |
| `rhmax_pct`, `rhmin_pct` | % |
| `wind2m_m_s` | m/s at 2 m |
| `solar_rad_mj_m2_day` | MJ/m²/day |
| `precip_mm` | mm/day |

`weather_daily.csv` is long-form (`date`, `location_id`, variables). Long-form
data is easier to validate and edit than seven wide IdrAgra files. A later
exporter can pivot it to the v3 format once simulation cells and weather
sampling/interpolation rules are known.

## Current scope

- ERA5-Land request planning and optional download through `cdsapi`;
- resumable monthly raw downloads, spatially subset to a bounding box;
- safe staging of user-provided weather, soil, land-use, or topography files;
- a versioned `manifest.json` with checksums and acquisition provenance;
- generation and validation of an editable normalized weather CSV;
- post-download ERA5-Land normalization into `weather_daily_points.gpkg`;
- HTTP-range-subset E-OBS acquisition and normalization into the same canonical
  `weather_daily_points.gpkg` schema;
- clipped ISRIC SoilGrids WCS downloads for `sand`, `silt`, `clay`, and `bdod`
  at either three topsoil or all six standard depth intervals;
- AOI-filtered CORINE Land Cover 2018 vector acquisition from the EEA ArcGIS
  REST service, retaining the published `Code_18` classification;
- an installable QGIS dialog with rectangle drawing, planning, background
  acquisition, and result-layer loading.

The raw download remains separate from normalization. The QGIS interface can
chain them in one run, while keeping the NetCDF files so a different timezone
or aggregation rule can be applied without downloading again.

## Quick start

```bash
python -m pip install -e .

# Inspect the exact CDS jobs without credentials or network access.
idragather plan-era5 \
  --bbox 8.5 44.7 10.2 46.2 \
  --start 2024-04-01 --end 2024-09-30

# Download after configuring the CDS API credentials.
python -m pip install -e '.[era5]'
idragather fetch-era5 study_inputs \
  --bbox 8.5 44.7 10.2 46.2 \
  --start 2024-04-01 --end 2024-09-30

# Or stage data that the user downloaded manually.
idragather stage-local study_inputs weather downloads/weather.nc \
  --source-name "user supplied weather"

# Make an editable daily table and validate it after editing.
idragather weather-template study_inputs/weather/weather_daily.csv \
  --locations cell_1 cell_2 \
  --start 2024-04-01 --end 2024-09-30
idragather validate-weather study_inputs/weather/weather_daily.csv
```

The bounding-box order is `west south east north` in EPSG:4326.

## Output layout

```text
study_inputs/
  manifest.json
  raw/
    weather/era5_land/*.nc
    weather/eobs/*.nc
    weather/local/*
    soil/local/*
    soil/soilgrids/*.tif
    landuse/corine/clc2018.geojson
    landuse/local/*
    topography/local/*
  weather/
    weather_daily.csv
    weather_daily_points.gpkg
```

The GeoPackage contains one `weather_daily_points` layer. Each row represents
one weather grid centre on one date and contains the date, location ID, point
geometry, and seven IdrAgra weather variables. Repeating the small point
geometry makes the layer directly filterable, editable, styleable, and usable
with QGIS temporal tools. Edge-hour substitutions and other warnings are
written to the task log and manifest rather than stored as data columns.

For E-OBS, wind is converted from 10 m to 2 m and daily mean radiation is
converted to MJ/m²/day. E-OBS publishes only daily mean relative humidity, so
`rhmin_pct` and `rhmax_pct` are estimated from mean humidity, Tmin, and Tmax
using a constant actual-vapour-pressure approximation. The estimation is
recorded in the manifest and task warnings.

SoilGrids files are kept in their published integer units. Divide sand, silt,
and clay by 10 for percent; divide `bdod` by 100 for kg/dm³ (equivalent in
value to g/cm³). The plugin does not yet apply a pedotransfer function.

## Intended QGIS boundary

The plugin UI collects the AOI, dates, source choice, and local files. It runs
acquisition in a background task and loads spatial staging outputs into QGIS.
QGIS-specific clipping and GeoPackage creation still belong in a thin adapter;
provider clients and data contracts stay in the independent core.

To rebuild the installable plugin archive:

```bash
python tools/build_qgis_plugin.py dist/IdrAgraGather_qgis.zip
```

## Important ERA5-Land details for the next step

- relative humidity extrema must be derived from hourly 2 m temperature and
  dew-point temperature, not from daily mean humidity;
- 10 m wind components must be converted to wind speed and adjusted to 2 m;
- surface solar radiation is delivered in J/m² and must become MJ/m²/day;
- precipitation is delivered in metres and must become mm/day;
- aggregation-day boundaries must be chosen explicitly (UTC versus local
  civil day), especially around daylight-saving changes.

These decisions will be recorded in the manifest when normalization is added.
