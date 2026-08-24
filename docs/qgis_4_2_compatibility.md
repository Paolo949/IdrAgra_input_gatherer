# QGIS 4.2 compatibility target

The installable package targets QGIS 4.2 while retaining QGIS 3.28 support.

- plugin metadata declares QGIS 3.28 through the QGIS 4.x series;
- Qt enums use their scoped PyQt6 names with PyQt5 fallbacks;
- QGIS message and geometry enums use QGIS 4 names with legacy fallbacks;
- the plugin archive contains exactly one importable root, `IdrAgraGather`;
- the acquisition code runs through `QgsTask`, without touching GUI objects in
  the worker function.

The automated packaging test rejects nested roots such as
`src/IdrAgraGather`, because QGIS would turn that path into an invalid Python
package name.
