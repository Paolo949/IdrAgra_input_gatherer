# Simulation-cell view

The **IdrAgra -> Build IdrAgra simulation cells...** action combines the
normalized workspace inputs into an editable, version-neutral spatial model.
It intentionally precedes the final IdrAgra v2/v3 exporter.

## Land-use model

The cell builder keeps three related concepts separate:

1. a crop refers to a reusable IdrAgra crop-parameter filename;
2. an IdrAgra land use is an ordered annual rotation containing zero, one, or
   two crops;
3. a source-class allocation assigns one normalized map class to one or more
   IdrAgra land uses whose percentages total 100%.

The starter catalogue mirrors the supplied IdrAgra-ready example. Existing
`soil_uses.txt` tables can be imported. The selected catalogue and allocation
rules are saved as `cells/landuse_configuration.json`; the normalized land-use
layer remains unchanged and does not need a numerical IdrAgra ID.

## Grid mode

The requested cell width is interpreted in the metric CRS of the normalized
topography. The output grid is deterministically aligned to multiples of that
width. Every emitted geometry is a complete square of exactly that width; grid
cells are never clipped to the AOI. The selectable boundary policy either:

- retains only squares fully inside the AOI; or
- retains every full square with a positive-area AOI intersection, allowing the
  simulated footprint to cross the boundary.

The `aoi_fraction` field records how much of each square overlaps the AOI. It is
always 1 in the first mode. In crossing mode, soil and source land-use dominance
are calculated only over the overlapping portion, while `area_m2` remains the
area of the complete simulation square. Raster positions excluded by the chosen
policy are NoData.

Soil and source land-use categories are selected by greatest intersected area.
Elevation defaults to the median of covered source pixels. Slope defaults to
the median value in the most populated 1-percentage-point band, suppressing
narrow banks, canals, and other edge gradients in otherwise even agricultural
fields. Mean, median, dominant-band, and centroid options remain selectable.
When the requested grid is finer than the normalized topography—for example,
50 m cells over a 90 m DEM—elevation and slope are bilinearly interpolated onto
every target cell. Aggregation resumes once target cells cover one or more
complete source pixels.

Percentage allocations are assigned deterministically across indivisible
cells, so repeating a build produces the same result. The allocation approaches
the requested area shares as closely as the chosen cell size permits.

## Vector mode

Soil and source-land-use polygons are intersected, dissolved by their unique
combination, and separated into contiguous polygon cells. Elevation and slope
are bilinearly sampled at each centroid. The first implementation allocates
whole contiguous regions rather than drawing artificial subdivision lines;
the task reports land-use targets whose achieved area differs by more than 5%.

## Outputs

Both modes create `cells/simulation_cells.gpkg` with the fields needed by a
later v2/v3 adapter, including stable cell ID, soil and land-use IDs, topography,
area, projected centroid, latitude, source class, and coverage diagnostics.

Grid mode additionally creates four co-registered rasters:

- `cells/soil_id.tif`
- `cells/landuse_id.tif`
- `cells/elevation_m_asl.tif`
- `cells/slope_pct.tif`

Switching the workspace to vector mode removes these grid-only outputs so they
cannot be mistaken for current results. All current outputs and generation
options are recorded in `manifest.json`.
