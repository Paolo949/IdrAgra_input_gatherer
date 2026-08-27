# Suggested next increments

Each increment should stay usable without the later ones.

1. **ERA5 daily normalizer — first implementation complete**
   - reads the current CDS NetCDF-4 structure through GDAL;
   - derives hourly RH from temperature and dew point, then daily extrema;
   - converts 10 m vector wind to 2 m speed;
   - de-accumulates radiation and precipitation before daily sums;
   - respects `Europe/Rome` legal-time day boundaries;
   - stores a clean point-date weather layer in GeoPackage;
   - reports edge filling as warnings and records transformations in the manifest.

2. **QGIS adapter — prototype implemented**
   - rectangle drawing and canvas-extent selection are available;
   - AOIs are transformed to EPSG:4326;
   - plan/fetch/stage operations run in a cancellable `QgsTask`;
   - AOI, NetCDF sublayers, and local spatial inputs are loaded into a group;
   - next: use QGIS Processing for clipping and GeoPackage writes.

3. **Local weather import mapping**
   - preview delimiter, date field, location field, variables, and units;
   - save mappings as small JSON recipes;
   - normalize CSV and station layers through the same weather contract.

4. **Topography — Copernicus acquisition and generic rasters implemented**
   - local DEM staging remains available;
   - GLO-30 and GLO-90 are acquired from the authenticated Sentinel Hub Process
     API in bounded-size tiles;
   - normalization mosaics and clips elevation into a local metric UTM raster
     and derives an aligned percent-slope raster;
   - next: sample these rasters only after the regular-grid or free-form-vector
     simulation-cell definition has been chosen.

5. **Soil and land use — SoilGrids and CORINE normalization implemented**
   - local vector/raster staging and clipping first;
   - DUSAF/ERSAF provider experiments behind optional adapters;
   - CORINE and SoilGrids only as generic fallbacks;
   - SoilGrids acquisition retains all six standard horizons and the texture,
     coarse-fragment, organic-carbon, and bulk-density PTF inputs;
   - SoilGrids normalization writes editable full-profile polygons in canonical
     units, condenses similar profiles to a user-selected maximum class count,
     fills all NoData gaps to cover the AOI, and does not apply a pedotransfer
     function;
   - a separate Rosetta 3 PTF dialog derives a complete canonical hydraulic set
     per profile/horizon and records method inputs, outputs, unused fields,
     units, uncertainty, and provenance;
   - CORINE and SoilGrids normalized polygons are clipped to the exact AOI;
   - CORINE normalization writes a minimal Shapefile with readable level-three
     land-use categories and automatic categorical QGIS styling;
   - next: add Saxton–Rawls and legacy v2 Rawls–Brakensiek adapters behind the
     same field-contract and output schema, including selectable documented bulk-
     density/coarse-fragment corrections where the method supports them.

6. **Simulation-cell view — first implementation complete**
   - a separate QGIS dialog maintains reusable crop parameter references and
     zero-, one-, or two-crop annual land-use rotations;
   - every normalized source class can be allocated to one or more rotations,
     with shares validated to total 100%;
   - grid mode creates a canonical cell GeoPackage plus four aligned soil-ID,
     land-use-ID, elevation, and slope rasters;
   - categorical values use greatest intersected area, elevation defaults to
     the median, and slope defaults to the dominant 1% band;
   - vector mode dissolves contiguous soil/source-land-use combinations and
     samples topography at their centroids;
   - next: refine percentage-driven subdivision of large vector regions and
     add map-preview/summary controls.

7. **v2/v3 exporter — first static v2 contract implemented**
   - v2 regular grids, two-layer soil aggregation, weather station series and
     inverse-distance weight maps are generated from grid cells;
   - canonical Ksat is converted to v2 cm/h and v2 Brooks-Corey `N` is derived
     with the legacy IdrAgraTools equation;
   - legacy texture-class capillary-rise parameter grids and provenance are
     written, while capillary uptake remains disabled pending water-table input;
   - CropCoef rotations are exported, while its phenology run remains an
     explicit prerequisite;
   - next: operational irrigation, annual land-use variation, water-table and
     capillary activation, then v3 cell-ordered weather and parameter files;
   - add integration fixtures accepted by the v2 and v3 executables/parsers.
