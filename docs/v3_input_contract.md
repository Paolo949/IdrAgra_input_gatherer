# IdrAgra v3 input notes used by this prototype

These notes come from the attached v3 source snapshot. They are intentionally
limited to facts that influence acquisition and staging.

## Weather

`mod_weather.f90` allocates seven `(cell, simulation_day)` arrays:

- maximum and minimum air temperature;
- maximum and minimum relative humidity;
- solar radiation;
- wind speed;
- rainfall depth.

`read_weather_ts` loads each variable from a separate file. The common
`import_time_series` routine in `mod_utility.f90` requires:

1. a comment line;
2. a header line;
3. one data row per day, beginning with an ISO `yyyy-mm-dd` date and followed
   by exactly one value per simulation cell.

Dates must be consecutive over the simulation period. Leading rows before the
simulation start are accepted, but missing or out-of-order dates are not.

ET0 is not an input series. `standalone_main.f90` reads the seven weather
series and calls `calc_et0`. The ET0 routine uses cell latitude and altitude in
addition to the daily weather values.

The v3 routine and its v2 predecessor indicate the intended weather units:

| Variable | Unit |
| --- | --- |
| temperature | °C |
| relative humidity | % |
| wind speed | m/s, at 2 m for FAO-56 |
| solar radiation | MJ/m²/day |
| precipitation | mm/day |

This is why the staging schema does not contain ET0 and why raw ERA5 10 m wind
cannot be passed through unchanged.

## Spatial cell information

`mod_initialization.f90` reads these fields for every cell:

```text
cell_id soil_id landuse_id iu_id irr_method slope latitude altitude area_m2
```

Consequently, weather acquisition must not assume that v3 still uses the v2
weather-station/Voronoi structure. A future normalization step needs an
explicit sampling policy that maps gridded or station data to v3 cells.

## Soil and land use

The v3 cell table references `soil_id` and `landuse_id`. The soil database is
layered and includes, among other values, layer thickness, saturated hydraulic
conductivity, saturated/field-capacity/wilting/residual water contents, and van
Genuchten parameters. Land-use records reference crop IDs and can be overridden
by year.

Source soil and land-cover layers should therefore remain editable staging
layers. Retrieval, classification, pedotransfer, and IdrAgra database export
are separate operations; none should be hidden in a downloader.

