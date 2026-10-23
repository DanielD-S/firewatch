"""Utilidades geométricas: proyección métrica, buffers, distancias y rumbos."""
from __future__ import annotations

import math
from functools import lru_cache

from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, Point, Polygon, MultiPolygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform


@lru_cache(maxsize=8)
def _transformers(crs_metric: str) -> tuple[Transformer, Transformer]:
    fwd = Transformer.from_crs("EPSG:4326", crs_metric, always_xy=True)
    inv = Transformer.from_crs(crs_metric, "EPSG:4326", always_xy=True)
    return fwd, inv


def to_metric(geom: BaseGeometry, crs_metric: str) -> BaseGeometry:
    fwd, _ = _transformers(crs_metric)
    return shp_transform(fwd.transform, geom)


def to_wgs(geom: BaseGeometry, crs_metric: str) -> BaseGeometry:
    _, inv = _transformers(crs_metric)
    return shp_transform(inv.transform, geom)


def point_metric(lon: float, lat: float, crs_metric: str) -> Point:
    fwd, _ = _transformers(crs_metric)
    x, y = fwd.transform(lon, lat)
    return Point(x, y)


def geojson_to_geom(geometry: dict) -> BaseGeometry:
    return shape(geometry)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Rumbo inicial desde el punto 1 hacia el punto 2, en grados 0-360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def angular_diff_deg(a: float, b: float) -> float:
    d = abs((a - b + 180.0) % 360.0 - 180.0)
    return d


def wind_blows_toward(wind_from_deg: float, bearing_to_target_deg: float, tol_deg: float) -> bool:
    """El viento se reporta como la dirección DESDE la que sopla (meteorológico).

    Sopla HACIA wind_from + 180. Devuelve True si esa dirección apunta al
    objetivo dentro de la tolerancia.
    """
    blows_to = (wind_from_deg + 180.0) % 360.0
    return angular_diff_deg(blows_to, bearing_to_target_deg) <= tol_deg


def sample_points_along(geom_metric: BaseGeometry, spacing_m: float) -> list[Point]:
    """Puntos de muestreo cada `spacing_m` a lo largo de líneas, o el centroide
    y vértices para polígonos. Siempre devuelve al menos un punto."""
    pts: list[Point] = []
    if isinstance(geom_metric, (LineString,)):
        lines = [geom_metric]
    elif isinstance(geom_metric, MultiLineString):
        lines = list(geom_metric.geoms)
    elif isinstance(geom_metric, (Polygon, MultiPolygon)):
        pts.append(geom_metric.centroid)
        boundary = geom_metric.boundary
        lines = list(boundary.geoms) if hasattr(boundary, "geoms") else [boundary]
    else:
        return [geom_metric.centroid]
    for ln in lines:
        length = ln.length
        if length == 0:
            pts.append(Point(ln.coords[0]))
            continue
        n = max(1, int(length // spacing_m))
        for i in range(n + 1):
            d = min(length, i * spacing_m) if n > 0 else 0
            pts.append(ln.interpolate(d))
        if n * spacing_m < length:
            pts.append(ln.interpolate(length))
    # eliminar duplicados cercanos
    out: list[Point] = []
    for p in pts:
        if all(p.distance(q) > spacing_m * 0.25 for q in out):
            out.append(p)
    return out or [geom_metric.centroid]
