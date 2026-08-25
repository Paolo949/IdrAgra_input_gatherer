# IdrAgra v2 export contract

The fourth plugin dialog exports only regular-grid cell views. It intentionally
starts with a narrow model configuration that can be represented faithfully by
the normalized workspace:

- static `soiluse.asc` (`SoilUseVarFlag = F`);
- rain-fed simulation (`Mode = 0`);
- field-capacity initialization (`InitialThetaFlag = F`);
- no capillary rise (`CapillaryFlag = F`);
- complete, equal-sized square cells;
- crop-less land-use classes excluded from `domain.asc`.

## Generated files

`geodata/` contains the v2 domain, land use, slope, hydrologic condition and
group, two-layer hydraulic grids, and one weather-weight grid per selected
neighbour. `meteodata/` contains one daily station file per normalized weather
location. `weather_stations.dat` maps those files to projected coordinates.
`landuses/soil_uses.txt` and any resolvable crop `.tab` files form the hand-off
to CropCoef. A Mode-0 `irrmethods/irrmethods.txt` parser stub and an annotated
`idragra_parameters.txt` template are also written.

Phenology is not synthesized. CropCoef must be run for every exported station,
and its `pheno_station_NNN` directories placed below `pheno/`, before IdrAgra v2
can run. Operational irrigation/network inputs, yearly land-use grids,
water-table depth, and capillary-rise grids remain outside this first contract.

## Soil transformation

The dialog's default layer thicknesses reproduce the legacy IdrAgraTools
aggregation intervals: layer I is 0-0.10 m and layer II is 0.10-1.00 m. Ksat is
aggregated using a thickness-weighted harmonic mean. Volumetric water contents
use a thickness-weighted arithmetic mean.

Canonical Ksat is stored in mm/h; v2 grids and `rice_soilparam.txt` require
cm/h, so exported values are divided by ten. v2 `N` is the Brooks-Corey drainage
exponent used by the v2 percolation model, not van Genuchten `n`:

```text
N = log((0.2 mm/day) / Ksat) /
    log((theta_fc - theta_res) / (theta_sat - theta_res))
```

The ratio is evaluated with consistent time and length units. Hydrologic soil
groups follow the legacy Mockus/NEH Ksat thresholds for a profile deeper than
1 m and no shallow water table. Hydrologic condition is an explicit dialog
choice because it is a management/cover condition, not a soil PTF result.

## Weather transformation

Every station must contain the same complete daily interval. Station files use
the v2 column order `T_max, T_min, P_tot, U_max, U_min, V_med, RG_CORR`.
Weather grids encode `station_id + fractional_weight`, following IdrAgraTools.
For two or more stations, the nearest requested stations receive normalized
inverse planar-distance weights. A single station is represented in two maps at
0.5 weight each, matching the legacy special case.

`export_provenance.json` records input hashes, PTF metadata, aggregation rules,
units, selected options, exclusions, and known gaps.
