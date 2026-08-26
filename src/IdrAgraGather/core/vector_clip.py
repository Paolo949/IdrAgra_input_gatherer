from pathlib import Path

from .staging import AOI_GEOMETRY_SOURCE

# Return the authoritative saved AOI polygon in *target_srs*.
def aoi_geometry(path: str | Path, target_srs, ogr, osr):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Workspace has no saved AOI polygon: {path}")
    polygon = _read_aoi_polygon(path, ogr)
    source_srs = polygon.GetSpatialReference()
    if source_srs is None:
        source_srs = osr.SpatialReference()
        source_srs.ImportFromEPSG(4326)
    axis_strategy = getattr(osr, "OAMS_TRADITIONAL_GIS_ORDER", None)
    if axis_strategy is not None:
        source_srs.SetAxisMappingStrategy(axis_strategy)

    if target_srs is None:
        return polygon
    destination_srs = target_srs.Clone()
    if axis_strategy is not None:
        destination_srs.SetAxisMappingStrategy(axis_strategy)
    transformation = osr.CoordinateTransformation(source_srs, destination_srs)
    if polygon.Transform(transformation) != 0:
        raise RuntimeError("failed to transform the AOI into the vector layer CRS")
    polygon.AssignSpatialReference(destination_srs)
    return polygon


def _read_aoi_polygon(path: str | Path, ogr):
    database = ogr.Open(str(path), 0)
    if database is None:
        raise ValueError(f"OGR could not open saved AOI: {path}")
    layer = database.GetLayer(0)
    feature = layer.GetNextFeature() if layer is not None else None
    source_index = layer.GetLayerDefn().GetFieldIndex("geometry_source") if layer is not None else -1
    if feature is None or source_index < 0 or feature.GetField(source_index) != AOI_GEOMETRY_SOURCE:
        database = None
        raise ValueError(f"Saved AOI predates exact polygon support; redraw the study area: {path}")
    geometry = feature.GetGeometryRef() if feature is not None else None
    if geometry is None or geometry.IsEmpty():
        database = None
        raise ValueError(f"Saved AOI contains no polygon geometry: {path}")
    polygon = geometry.Clone()
    spatial_reference = geometry.GetSpatialReference() or layer.GetSpatialRef()
    if spatial_reference is not None:
        polygon.AssignSpatialReference(spatial_reference.Clone())
    database = None
    return polygon
