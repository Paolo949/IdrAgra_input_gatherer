# Soil hydraulic pedotransfer step

The normalized `soil/soil_profiles.gpkg` remains editable source data. The
separate **IdrAgra → Derive soil hydraulic properties...** action writes
`soil/soil_hydraulics.gpkg`; it never adds PTF values to or silently modifies
the source polygons.

The dialog uses colored field sections so each method declares its contract:

- blue: inputs required by the selected method and options;
- green: canonical fields generated for the later v2/v3 exporters;
- gray: normalized inputs available but unused by this application.

This metadata comes from the same core field specification recorded in the
workspace manifest, rather than being duplicated as display-only text. That is
the extension point for future Saxton–Rawls and Rawls–Brakensiek choices.

## First method: Rosetta 3

The plugin bundles the unmodified H2 and H3 neural-network assets from USDA-ARS
`rosetta-soil` 0.3.2 under CC0 1.0:

- H2 uses sand, silt, and clay percentages;
- H3 (the default) also uses bulk density in g/cm³.

Texture percentages must be non-negative and total 99–101%; H3 bulk density
must be 0.5–2.0 g/cm³, matching Rosetta's input domain checks. Organic carbon
and skeleton/coarse-fragment percentage are not Rosetta H2/H3 inputs. They are
copied into the result table as source context, but no coarse-fragment
correction is applied. The task log and manifest state both facts explicitly.

Each bundled model contains 1,000 bootstrap neural networks. For every output,
the implementation uses the arithmetic mean of the linear parameter values and
stores the bootstrap sample standard deviation for Rosetta's five direct
predictions. The numerical results are regression-tested against the upstream
package examples.

Rosetta directly predicts:

| Rosetta result | Published unit | Stored field | Stored unit |
| --- | --- | --- | --- |
| residual water content | m³/m³ | `theta_res` | m³/m³ |
| saturated water content | m³/m³ | `theta_sat` | m³/m³ |
| van Genuchten alpha | 1/cm | `vg_alpha` | 1/mm (divide by 10) |
| van Genuchten n | – | `vg_n` | – |
| saturated conductivity | cm/day | `ksat_mm_h` | mm/h (multiply by 10/24) |

The remaining canonical values are derived from the mean parameters. The
Mualem constraint is used:

```text
vg_m = 1 - 1/vg_n
```

Water content at a pressure head is evaluated with the van Genuchten retention
curve:

```text
theta(h) = theta_res + (theta_sat - theta_res)
           / [1 + (vg_alpha * |h|)^vg_n]^vg_m
```

where `h` is in millimetres of water. The conversion is
`1 kPa = 101.9716212978 mm H₂O`. Consequently `theta_fc` is evaluated at
33 kPa and `theta_wp` at 1500 kPa. This produces one internally consistent
curve; it is not a separate refit through those two derived points. Rosetta's
native `theta_res` is retained, so the provisional `theta_wp / 2` convention
discussed for a future Saxton–Rawls adapter is not used here.

## Output schema and replacement

`soil_hydraulics.gpkg` contains two non-spatial tables:

- `soil_hydraulic_metadata`: method, hierarchy, source checksum,
  implementation, citation, creation time, field roles, tensions, and applied
  options;
- `soil_hydraulic_layers`: one row per profile/horizon, retaining source values,
  the eight canonical hydraulic fields, and direct-prediction standard
  deviations.

Running another configuration atomically replaces this GeoPackage. A user who
wants to compare alternatives can save a copy before running the next method.
The workspace `manifest.json` records the current method, field roles, units,
tensions, VG constraint, source checksum, and coarse-fragment policy.

Primary reference: Zhang, Y. and Schaap, M.G. (2017), “Weighted recalibration
of the Rosetta pedotransfer model with improved estimates of hydraulic
parameter distributions and summary statistics,” *Journal of Hydrology* 547,
39–53.
