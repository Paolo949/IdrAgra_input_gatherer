# Bundled Rosetta 3 model assets

`rose3_mod2_0.npz` and `rose3_mod3_0.npz` are unmodified model assets from
USDA-ARS `rosetta-soil` 0.3.2. They implement Rosetta 3 hierarchy levels H2
(sand, silt, clay) and H3 (H2 plus bulk density).

Source: <https://github.com/usda-ars-ussl/rosetta-soil>

The upstream project dedicates these files to the public domain under CC0 1.0;
see `ROSETTA_LICENSE.txt`. The local inference adapter exists only to retain
Python 3.10 compatibility for QGIS 3.28.

Primary model reference:

Zhang, Y. and Schaap, M.G. (2017), *Weighted recalibration of the Rosetta
pedotransfer model with improved estimates of hydraulic parameter distributions
and summary statistics*, Journal of Hydrology 547, 39-53.
