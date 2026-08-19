from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QCursor, QkeyEvent
from qgis.core import Qgis, QgsGeometry, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand, QgsMapMouseEvent


# QGIS 4 uses the scoped Qt 6 enums. These lookups retain QGIS 3 compatibility.
CURSOR_SHAPE = getattr(Qt, "CursorShape", Qt)
KEY = getattr(Qt, "Key", Qt)
try:
    POLYGON_GEOMETRY = Qgis.GeometryType.Polygon
except AttributeError:
    POLYGON_GEOMETRY = QgsWkbTypes.PolygonGeometry


class RectangleMapTool(QgsMapTool):
    rectangleCreated = pyqtSignal(QgsRectangle)
    cancelled = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.start_point = None
        self.rubber_band = QgsRubberBand(canvas, POLYGON_GEOMETRY)
        self.rubber_band.setColor(QColor(28, 120, 180, 160))
        self.rubber_band.setFillColor(QColor(28, 120, 180, 45))
        self.rubber_band.setWidth(2)
        self.setCursor(QCursor(CURSOR_SHAPE.CrossCursor))

    def canvasPressEvent(self, e: QgsMapMouseEvent):
        self.start_point = self.toMapCoordinates(e.pos())
        self._show_rectangle(self.start_point)

    def canvasMoveEvent(self, e: QgsMapMouseEvent):
        if self.start_point is not None:
            self._show_rectangle(self.toMapCoordinates(e.pos()))

    def canvasReleaseEvent(self, e: QgsMapMouseEvent):
        if self.start_point is None:
            return
        end_point = self.toMapCoordinates(e.pos())
        rectangle = QgsRectangle(self.start_point, end_point)
        self.start_point = None
        if rectangle.width() > 0 and rectangle.height() > 0:
            self._show_rectangle(end_point, rectangle=rectangle)
            self.rectangleCreated.emit(rectangle)

    def keyPressEvent(self, e: QkeyEvent):
        if e.key() == KEY.Key_Escape:
            self.start_point = None
            self.clear()
            self.cancelled.emit()
        else:
            super().keyPressEvent(e)

    def clear(self):
        self.rubber_band.reset(POLYGON_GEOMETRY)

    def _show_rectangle(self, end_point, rectangle=None):
        rectangle = rectangle or QgsRectangle(self.start_point, end_point)
        self.rubber_band.setToGeometry(QgsGeometry.fromRect(rectangle), None)
