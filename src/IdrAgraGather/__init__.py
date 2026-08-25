"""QGIS entry point for the IdrAgra input-gathering prototype."""


def classFactory(iface):
    from .plugin import IdrAgraGatherPlugin

    return IdrAgraGatherPlugin(iface)
