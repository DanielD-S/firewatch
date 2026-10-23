"""Sectores a monitorear (líneas eléctricas, polígonos) y sus corredores."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point, mapping
from shapely.geometry.base import BaseGeometry

from . import geo
from .config import Config


@dataclass
class Sector:
    id: str
    name: str
    props: dict
    geom_wgs: BaseGeometry
    geom_m: BaseGeometry
    near_m: BaseGeometry
    watch_m: BaseGeometry
    samples_m: list[Point] = field(default_factory=list)
    crs_metric: str = "EPSG:32719"

    @property
    def samples_wgs(self) -> list[tuple[float, float]]:
        """Lista de (lat, lon) de los puntos de muestreo meteorológico."""
        out = []
        for p in self.samples_m:
            w = geo.to_wgs(p, self.crs_metric)
            out.append((w.y, w.x))
        return out

    @property
    def centroid_wgs(self) -> tuple[float, float]:
        c = geo.to_wgs(self.geom_m.centroid, self.crs_metric)
        return (c.y, c.x)

    def distance_m(self, lon: float, lat: float) -> float:
        return float(self.geom_m.distance(geo.point_metric(lon, lat, self.crs_metric)))

    def nearest_point_wgs(self, lon: float, lat: float) -> tuple[float, float]:
        from shapely.ops import nearest_points

        p = geo.point_metric(lon, lat, self.crs_metric)
        a, _ = nearest_points(self.geom_m, p)
        w = geo.to_wgs(a, self.crs_metric)
        return (w.y, w.x)

    def zone_of(self, lon: float, lat: float) -> str | None:
        """'near' si está en el corredor cercano, 'watch' si en el de vigilancia."""
        p = geo.point_metric(lon, lat, self.crs_metric)
        if self.near_m.contains(p):
            return "near"
        if self.watch_m.contains(p):
            return "watch"
        return None

    def to_feature(self, extra: dict | None = None) -> dict:
        props = {"id": self.id, "name": self.name, **self.props}
        if extra:
            props.update(extra)
        return {"type": "Feature", "geometry": mapping(self.geom_wgs), "properties": props}

    def buffers_feature(self) -> dict:
        return {
            "type": "Feature",
            "geometry": mapping(geo.to_wgs(self.watch_m, self.crs_metric)),
            "properties": {"id": self.id, "ring": "watch"},
        }

    def near_feature(self) -> dict:
        return {
            "type": "Feature",
            "geometry": mapping(geo.to_wgs(self.near_m, self.crs_metric)),
            "properties": {"id": self.id, "ring": "near"},
        }


def load_sectors(cfg: Config, path: Path | None = None, data: dict | None = None) -> list[Sector]:
    """Carga sectores desde un dict GeoJSON (`data`) o desde un archivo."""
    if data is None:
        path = path or cfg.path("sectors_file")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    gj = data
    feats = gj["features"] if gj.get("type") == "FeatureCollection" else [gj]
    near = float(cfg["buffers_m"]["near"])
    watch = float(cfg["buffers_m"]["watch"])
    spacing = float(cfg["sources"]["weather"].get("sample_spacing_m", 20000))
    crs = cfg.crs_metric
    out: list[Sector] = []
    for i, f in enumerate(feats):
        props = dict(f.get("properties") or {})
        sid = str(props.pop("id", None) or f.get("id") or f"sector-{i + 1}")
        name = str(props.pop("name", None) or sid)
        g = geo.geojson_to_geom(f["geometry"])
        gm = geo.to_metric(g, crs)
        sec = Sector(
            id=sid,
            name=name,
            props=props,
            geom_wgs=g,
            geom_m=gm,
            near_m=gm.buffer(near),
            watch_m=gm.buffer(watch),
            samples_m=geo.sample_points_along(gm, spacing),
            crs_metric=crs,
        )
        out.append(sec)
    return out
