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

4. **Topography**
   - accept a local DEM first;
   - clip/resample it in QGIS;
   - derive cell altitude and slope only when the grid definition is known;
   - add a remote Copernicus DEM provider only if distribution terms and API
     stability justify the maintenance cost.

5. **Soil and land use**
   - local vector/raster staging and clipping first;
   - DUSAF/ERSAF provider experiments behind optional adapters;
   - CORINE and SoilGrids only as generic fallbacks;
   - keep class mapping and pedotransfer as explicit editable steps.

6. **v3 exporter**
   - pivot validated daily weather into seven IdrAgra files;
   - verify cell ordering against `cell_info`;
   - write two header rows and ISO dates exactly as the v3 reader expects;
   - add an integration fixture accepted by the v3 executable/parser.
