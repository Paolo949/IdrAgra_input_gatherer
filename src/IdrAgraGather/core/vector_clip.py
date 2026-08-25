from .models import BoundingBox


# Return the EPSG:4326 AOI rectangle transformed into *target_srs*.
def aoi_geometry(bbox: BoundingBox, target_srs, ogr, osr):
    source_srs = osr.SpatialReference()
    source_srs.ImportFromEPSG(4326)
    axis_strategy = getattr(osr, "OAMS_TRADITIONAL_GIS_ORDER", None)
    if axis_strategy is not None:
        source_srs.SetAxisMappingStrategy(axis_strategy)

    ring = ogr.Geometry(ogr.wkbLinearRing)
    ring.AddPoint_2D(bbox.west, bbox.south)
    ring.AddPoint_2D(bbox.east, bbox.south)
    ring.AddPoint_2D(bbox.east, bbox.north)
    ring.AddPoint_2D(bbox.west, bbox.north)
    ring.AddPoint_2D(bbox.west, bbox.south)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    polygon.AssignSpatialReference(source_srs)

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
