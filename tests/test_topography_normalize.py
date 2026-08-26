import tempfile
import unittest
from pathlib import Path

from IdrAgraGather.core.models import BoundingBox
from IdrAgraGather.core.staging import StagingArea
from IdrAgraGather.core.topography_normalize import (
    ELEVATION_NAME,
    SLOPE_NAME,
    normalize_dem_files,
    utm_epsg_for_bbox,
)


class TopographyNormalizeTests(unittest.TestCase):
    def test_utm_crs_is_selected_from_aoi_centre(self):
        self.assertEqual(utm_epsg_for_bbox(BoundingBox(9.0, 45.0, 10.0, 46.0)), 32632)
        self.assertEqual(utm_epsg_for_bbox(BoundingBox(18.0, -34.0, 19.0, -33.0)), 32734)
        with self.assertRaisesRegex(ValueError, "UTM coverage"):
            utm_epsg_for_bbox(BoundingBox(9.0, 85.0, 10.0, 86.0))

    def test_gdal_pipeline_writes_aligned_elevation_and_slope(self):
        try:
            from osgeo import gdal, osr
        except ImportError:
            self.skipTest("GDAL Python bindings are not installed")

        gdal.UseExceptions()
        osr.UseExceptions()
        bbox = BoundingBox(9.0, 45.0, 9.02, 45.02)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "dem.tif"
            driver = gdal.GetDriverByName("GTiff")
            dataset = driver.Create(str(source), 20, 20, 1, gdal.GDT_Float32)
            dataset.SetGeoTransform((9.0, 0.001, 0.0, 45.02, 0.0, -0.001))
            spatial_reference = osr.SpatialReference()
            spatial_reference.ImportFromEPSG(4326)
            dataset.SetProjection(spatial_reference.ExportToWkt())
            band = dataset.GetRasterBand(1)
            for row in range(20):
                values = bytes()
                # Avoid a numpy dependency: use GDAL's packed Float32 write path.
                import struct

                values = struct.pack("<20f", *[100.0 + row + column for column in range(20)])
                band.WriteRaster(0, row, 20, 1, values, buf_type=gdal.GDT_Float32)
            band = None
            dataset = None

            project = Path(temporary) / "project"
            StagingArea(project).write_aoi(
                bbox,
                geometry={
                    "type": "Polygon",
                    "coordinates": [[[9.0, 45.0], [9.02, 45.0], [9.02, 45.02], [9.0, 45.02], [9.0, 45.0]]],
                },
            )
            result = normalize_dem_files(
                [source],
                project,
                bbox=bbox,
                resolution_m=100.0,
                dem_instance="COPERNICUS_30",
            )
            self.assertEqual(result.elevation_path.name, ELEVATION_NAME)
            self.assertEqual(result.slope_path.name, SLOPE_NAME)
            elevation = gdal.Open(str(result.elevation_path))
            slope = gdal.Open(str(result.slope_path))
            self.assertGreater(elevation.RasterXSize, 0)
            self.assertEqual(elevation.RasterXSize, slope.RasterXSize)
            self.assertEqual(elevation.RasterYSize, slope.RasterYSize)
            self.assertIn("32632", elevation.GetProjectionRef())
            self.assertEqual(elevation.GetRasterBand(1).GetUnitType(), "m")
            self.assertEqual(slope.GetRasterBand(1).GetUnitType(), "percent")
            self.assertIn(
                "Copernicus WorldDEM-30",
                elevation.GetMetadataItem("COPYRIGHT"),
            )
            elevation = None
            slope = None


if __name__ == "__main__":
    unittest.main()
