# -*- coding: utf-8 -*-
def classFactory(iface):
    from .all_shp2gpkg import AllShp2Gpkg
    return AllShp2Gpkg(iface)