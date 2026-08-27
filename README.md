# IdrAgra input gatherer and QGIS prototype

This repository is a deliberately small acquisition and spatial-preparation
core for a QGIS plugin. It collects raw inputs, preserves their provenance,
normalizes them, combines the spatial inputs into an editable simulation-cell
view, and exports the supported static rain-fed IdrAgra v2 input contract.

The core has no QGIS dependency. The included QGIS prototype calls those same
functions from a background `QgsTask`, while the command line remains useful
for tests and batch work.

The authoritative source tree mirrors the installed plugin: reusable modules
live under `src/IdrAgraGather/core`, beside the QGIS adapter. Tests and
the plugin therefore import the same modules, and the build does not copy or
rename Python packages.

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
7. download AOI-clipped SoilGrids mean texture, coarse-fragment, organic-carbon,
   and bulk-density coverages for the complete 0–200 cm profile;
8. normalize SoilGrids into editable full-profile polygons without applying a PTF;
9. apply Rosetta 3 H2/H3 in a separate dialog to derive canonical hydraulic
   values for every profile/horizon;
10. download CORINE Land Cover 2018 polygons intersecting the AOI as GeoJSON;
11. normalize CORINE codes into readable, categorized land-use polygons;
12. download AOI-clipped Copernicus DEM GLO-30 or GLO-90 elevation through the
   Sentinel Hub Process API;
13. normalize the DEM into aligned metric elevation-above-sea-level and percent-
   slope rasters;
14. open a separate cell-builder dialog, edit/import annual crop rotations, and
   allocate every normalized source class to one or more IdrAgra land uses;
15. generate either regular grid cells with four aligned property rasters or
   contiguous vector cells in a canonical GeoPackage;
16. export regular-grid cells to IdrAgra v2 spatial grids, station series,
   weather-weight grids, CropCoef rotation inputs, and a parameter template;
17. run acquisition and normalization independently or consecutively;
18. load raw layers into collapsed, visible category groups such as `weather_raw`
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
Staged files are resolved from their provider records in the manifest rather
than by reconstructing storage paths in the plugin.

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
Copernicus DEM download requires a free CDSE account and a Sentinel Hub OAuth
client. Its client ID and secret can be entered in the Topography page or
provided as `SH_CLIENT_ID` and `SH_CLIENT_SECRET`; the dialog does not save them.

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
- clipped ISRIC SoilGrids WCS downloads for `sand`, `silt`, `clay`, `cfvo`,
  `soc`, and `bdod` at all six standard depth intervals;
- SoilGrids normalization into `soil_profiles.gpkg`, with a complete six-horizon
  attribute profile attached to each polygon and no PTF-derived values;
- Rosetta 3 H2/H3 derivation into replaceable `soil_hydraulics.gpkg` metadata
  and profile/horizon tables, including canonical v2/v3 fields and provenance;
- an IdrAgra v2 exporter for static land use, Mode 0 (rain-fed), internally
  initialized soil moisture, and disabled capillary rise. It aggregates the six
  normalized horizons to v2's two layers, converts Ksat from canonical mm/h to
  v2 cm/h, derives v2's Brooks-Corey `N`, writes legacy texture-class
  capillary-rise parameters and weather station/IDW inputs, and records every
  transformation in `export_provenance.json`;
- AOI-filtered CORINE Land Cover 2018 vector acquisition from the EEA ArcGIS
  REST service, retaining the published `Code_18` classification;
- CORINE normalization into an AOI-clipped minimal `landuse.shp`, categorized
  by the official readable level-three class name;
- tiled Copernicus DEM GLO-30/GLO-90 acquisition through the authenticated
  Sentinel Hub Process API, retaining Float32 orthometric heights in metres;
- DEM normalization into `elevation_m_asl.tif` and `slope_pct.tif`, clipped to
  the AOI and reprojected to its local metric WGS 84 UTM CRS before calculating
  slope with the Horn algorithm;
- a crop/rotation catalogue and percentage allocation editor, including import
  of existing IdrAgra `soil_uses.txt` tables;
- grid and vector simulation-cell generation into `simulation_cells.gpkg`;
  grid cells are always equal complete squares, with selectable fully-inside or
  boundary-crossing AOI handling;
- aligned soil-ID, land-use-ID, elevation, and slope rasters in grid mode;
- an installable QGIS dialog with rectangle drawing, planning, background
  acquisition, and result-layer loading.

Raw acquisition remains separate from normalization. The QGIS interface can
chain them in one run while retaining the source NetCDF, GeoJSON, and GeoTIFF
files, so transformations can be repeated without downloading again.

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
    topography/copernicus_dem/<aoi-and-instance-id>/*.tif
    topography/local/*
  weather/
    weather_daily.csv
    weather_daily_points.gpkg
  soil/
    soil_profiles.gpkg
    soil_hydraulics.gpkg
  landuse/
    landuse.shp (+ Shapefile sidecars)
  topography/
    elevation_m_asl.tif
    slope_pct.tif
  cells/
    landuse_configuration.json
    simulation_cells.gpkg
    soil_id.tif              # grid mode only
    landuse_id.tif           # grid mode only
    elevation_m_asl.tif      # grid mode only
    slope_pct.tif            # grid mode only
  exports/idragra_v2/        # default fourth-dialog destination
    idragra_parameters.txt
    weather_stations.dat
    geodata/*.asc
    meteodata/station_*.dat
    landuses/soil_uses.txt
    landuses/crop_parameters/*.tab
    export_provenance.json
```

Use **IdrAgra → Build IdrAgra simulation cells...** after the normalized soil,
land-use, elevation, and slope inputs are ready. See
[`docs/cell_view.md`](docs/cell_view.md) for the cell schema, aggregation rules,
and current vector-allocation limitation.

Use **IdrAgra → Derive soil hydraulic properties...** after normalizing soil.
The default Rosetta H3 run uses texture and bulk density; H2 can be selected to
use texture alone. Required inputs, generated outputs, and unused normalized
fields are highlighted separately. See [`docs/soil_ptf.md`](docs/soil_ptf.md)
for equations, units, validation, uncertainty, and the output schema.

Use **IdrAgra -> Export IdrAgra v2 inputs...** after building a regular-grid
cell view and running a soil PTF. See [`docs/v2_export.md`](docs/v2_export.md)
for the supported static rain-fed contract, equations, unit conversions, and
the explicit CropCoef phenology hand-off.

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

SoilGrids raw files are kept in their published integer units. Unit conversion
follows the [official SoilGrids layer definitions](https://docs.isric.org/globaldata/soilgrids/SoilGrids_faqs_01.html).
Raw WCS GeoTIFFs that omit CRS metadata are assigned the native SoilGrids
WGS84 Interrupted Goode Homolosine CRS (`ESRI:54052`) before being loaded.
Normalization creates the `soil_profiles` polygon layer. A `profile_id` identifies each unique
representative six-horizon soil class; disconnected polygons can share the same
profile ID. The user selects the maximum number of output soil classes. Complete
cell profiles are clustered jointly across every parameter and horizon using
cell-count weighting. For distance calculation, one similarity unit is 5
percentage points for each texture fraction, 10 percentage points for skeleton,
0.5 percentage points for organic carbon, or 0.1 g/cm³ for bulk density. The
stored class parameters are cell-weighted means. Each horizon
block (`h1_` through `h6_`) records its top and bottom
depth and contains:

- `sand_pct`, `silt_pct`, and `clay_pct`, rescaled per horizon to total 100%;
- `skel_pct`, the coarse-fragment volume percentage from `cfvo`;
- `oc_pct`, the soil organic-carbon mass percentage from `soc`;
- `bd_g_cm3`, bulk density in g/cm³ from `bdod`.

Cells missing any input in any horizon are excluded initially. Every NoData
cell in the acquired AOI raster is then filled from the nearest valid soil
class, including corridors connected to the raster boundary, so the polygon
layer covers the full AOI. The polygonized raster-cell boundary is then clipped
to the exact selected AOI rectangle. PTF application remains a separate,
repeatable transformation and writes `soil_hydraulics.gpkg` without modifying
these normalized polygons.

When loaded by the plugin, `soil_profiles` is styled with stable categorical
colors keyed by `profile_id`. The colors look shuffled but are deterministic,
so a given profile ID keeps the same color when the layer is reloaded.

CORINE normalization preserves the raw `clc2018.geojson` for provenance and
creates `landuse/landuse.shp`, clipped to the exact selected AOI rectangle. The
normalized layer contains the Shapefile/OGR
feature ID plus only one user attribute, `landuse`, whose value is the official
readable [CORINE level-three category](https://land.copernicus.eu/content/corine-land-cover-nomenclature-guidelines/html/)
(for example, `Rice fields`). The numeric
`Code_18`, source identifiers, remarks, and source area estimate are intentionally
not duplicated. QGIS styles the normalized polygons categorically by `landuse`.

Copernicus DEM normalization produces two co-registered Float32 GeoTIFFs. The
elevation raster stores EGM2008 orthometric height in metres (including valid
zero-valued sea pixels), and the slope raster stores percent rise. The API's
geographic source tiles are mosaicked and clipped, then reprojected to the UTM
zone containing the AOI centre at the nominal source resolution (30 m or 90 m).
This metric intermediate form is suitable for either zonal statistics on a
future regular cell grid or centroid sampling for future vector cells. Source
tiles, API parameters, output CRS, units, and transformations remain recorded
in `manifest.json`.

## Intended QGIS boundary

The plugin UI collects the AOI, dates, source choice, and local files. It runs
acquisition in a background task and loads spatial staging outputs into QGIS.
QGIS-specific clipping and GeoPackage creation still belong in a thin adapter;
provider clients and data contracts stay in the independent core.

To rebuild the installable plugin archive:

```bash
python tools/build_qgis_plugin.py IdrAgraGather_QGIS_installer.zip
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
