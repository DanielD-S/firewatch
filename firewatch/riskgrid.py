"""Mapa de riesgo por zonas: grilla regular donde se combinan meteorología
(temperatura, humedad, viento, lluvia), sequedad del combustible (FWI,
probabilidad de ignición) y carga de combustible (cobertura ESA WorldCover).

riesgo = condiciones (0-1) x combustible (0-1)

Clases: bajo < 0.25, moderado < 0.45, alto < 0.65, muy alto < 0.80, extremo.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from .sources import weather as wx
from .sources.common import http_get
from .store import Store, iso, parse_iso, utcnow

log = logging.getLogger(__name__)

# Pesos de combustible por clase WorldCover (0-1). Matorral esclerófilo y
# plantaciones son los combustibles más peligrosos de Chile central.
FUEL_WEIGHTS = {10: 1.0, 20: 0.9, 30: 0.7, 40: 0.4, 50: 0.05, 60: 0.0, 70: 0.0, 80: 0.0, 90: 0.3, 95: 0.3, 100: 0.1}
CLASS_NAMES = {10: "bosque", 20: "matorral", 30: "pastizal", 40: "cultivo", 50: "urbano", 60: "suelo desnudo",
               70: "nieve", 80: "agua", 90: "humedal", 95: "manglar", 100: "musgo/líquen"}
SUB_DEG = 0.005  # ~500 m: resolución a la que se lee WorldCover para agregar por celda
WORLDCOVER_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"


def risk_class(r: float) -> str:
    if r < 0.25:
        return "bajo"
    if r < 0.45:
        return "moderado"
    if r < 0.65:
        return "alto"
    if r < 0.80:
        return "muy alto"
    return "extremo"


@dataclass
class Grid:
    bbox: tuple[float, float, float, float]   # lon_min, lat_min, lon_max, lat_max
    cell_deg: float
    ncols: int
    nrows: int

    @classmethod
    def from_cfg(cls, cfg_grid: dict) -> "Grid":
        lon_min, lat_min, lon_max, lat_max = (float(v) for v in cfg_grid["bbox"])
        cell = float(cfg_grid.get("cell_deg", 0.2))
        ncols = max(1, int(math.ceil((lon_max - lon_min) / cell - 1e-9)))
        nrows = max(1, int(math.ceil((lat_max - lat_min) / cell - 1e-9)))
        return cls((lon_min, lat_min, lon_min + ncols * cell, lat_min + nrows * cell), cell, ncols, nrows)

    @property
    def n(self) -> int:
        return self.ncols * self.nrows

    def center(self, idx: int) -> tuple[float, float]:
        """(lat, lon) del centro de la celda idx (fila-mayor desde el sur-oeste)."""
        r, c = divmod(idx, self.ncols)
        return (self.bbox[1] + (r + 0.5) * self.cell_deg, self.bbox[0] + (c + 0.5) * self.cell_deg)

    def bounds(self, idx: int) -> tuple[float, float, float, float]:
        r, c = divmod(idx, self.ncols)
        x0 = self.bbox[0] + c * self.cell_deg
        y0 = self.bbox[1] + r * self.cell_deg
        return (x0, y0, x0 + self.cell_deg, y0 + self.cell_deg)

    def index_of(self, lon: float, lat: float) -> int | None:
        if not (self.bbox[0] <= lon < self.bbox[2] and self.bbox[1] <= lat < self.bbox[3]):
            return None
        c = int((lon - self.bbox[0]) / self.cell_deg)
        r = int((lat - self.bbox[1]) / self.cell_deg)
        return r * self.ncols + c

    def key(self) -> str:
        return f"{self.bbox[0]:.4f},{self.bbox[1]:.4f},{self.bbox[2]:.4f},{self.bbox[3]:.4f}@{self.cell_deg}"


# ---------------------------------------------------------------- combustible

def worldcover_tiles(bbox: tuple[float, float, float, float]) -> list[tuple[str, float, float]]:
    """Nombres de teselas 3°x3° (esquina suroeste) que intersectan el bbox."""
    lon_min, lat_min, lon_max, lat_max = bbox
    out = []
    lat0 = int(math.floor(lat_min / 3.0)) * 3
    while lat0 < lat_max:
        lon0 = int(math.floor(lon_min / 3.0)) * 3
        while lon0 < lon_max:
            ns = "N" if lat0 >= 0 else "S"
            ew = "E" if lon0 >= 0 else "W"
            out.append((f"{ns}{abs(lat0):02d}{ew}{abs(lon0):03d}", float(lon0), float(lat0)))
            lon0 += 3
        lat0 += 3
    return out


def fuel_from_classes(arr: np.ndarray) -> tuple[float, dict[str, float]]:
    """Peso de combustible medio y fracciones de clase de un bloque de píxeles."""
    vals = arr[arr > 0]
    if vals.size == 0:
        return 0.0, {}
    classes, counts = np.unique(vals, return_counts=True)
    total = counts.sum()
    w = sum(FUEL_WEIGHTS.get(int(c), 0.0) * n for c, n in zip(classes, counts)) / total
    frac = {CLASS_NAMES.get(int(c), str(int(c))): round(float(n) / total, 3) for c, n in zip(classes, counts)
            if n / total >= 0.02}
    return float(w), frac


def read_worldcover_subgrid(grid: Grid, sub_deg: float = SUB_DEG) -> np.ndarray:
    """Lee WorldCover a `sub_deg` sobre el bbox (vía vsicurl, usando overviews)."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds

    lon_min, lat_min, lon_max, lat_max = grid.bbox
    w = int(round((lon_max - lon_min) / sub_deg))
    h = int(round((lat_max - lat_min) / sub_deg))
    full = np.zeros((h, w), dtype=np.uint8)
    env = rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                       GDAL_HTTP_MULTIRANGE="YES", GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES")
    with env:
        for tile, tlon, tlat in worldcover_tiles(grid.bbox):
            x0, x1 = max(lon_min, tlon), min(lon_max, tlon + 3.0)
            y0, y1 = max(lat_min, tlat), min(lat_max, tlat + 3.0)
            if x1 <= x0 or y1 <= y0:
                continue
            i0 = int(round((x0 - lon_min) / sub_deg)); i1 = int(round((x1 - lon_min) / sub_deg))
            j0 = int(round((y0 - lat_min) / sub_deg)); j1 = int(round((y1 - lat_min) / sub_deg))
            if i1 <= i0 or j1 <= j0:
                continue
            url = WORLDCOVER_URL.format(tile=tile)
            try:
                with rasterio.open(url) as ds:
                    win = from_bounds(lon_min + i0 * sub_deg, lat_min + j0 * sub_deg,
                                      lon_min + i1 * sub_deg, lat_min + j1 * sub_deg, ds.transform)
                    arr = ds.read(1, window=win, out_shape=(j1 - j0, i1 - i0), resampling=Resampling.mode)
                # filas: la imagen va de norte a sur; nuestra grilla de sur a norte
                full[h - j1:h - j0, i0:i1] = arr
                log.info("WorldCover %s leído (%dx%d px)", tile, i1 - i0, j1 - j0)
            except Exception as e:  # noqa: BLE001
                log.warning("WorldCover %s no disponible (%s); se asume sin combustible", tile, e)
    return full


def fuel_grid(grid: Grid, sub: np.ndarray, sub_deg: float = SUB_DEG) -> tuple[list[float], list[dict]]:
    """Agrega la subgrilla (orientación de imagen: fila 0 = norte) a las celdas
    de la grilla (fila 0 = sur)."""
    k = int(round(grid.cell_deg / sub_deg))
    h, w = sub.shape
    fuel: list[float] = []
    fracs: list[dict] = []
    for idx in range(grid.n):
        r, c = divmod(idx, grid.ncols)
        r0, r1 = max(0, h - (r + 1) * k), h - r * k
        block = sub[r0:r1, c * k:min(w, (c + 1) * k)]
        f, fr = fuel_from_classes(block)
        fuel.append(round(f, 3))
        fracs.append(fr)
    return fuel, fracs


def get_fuel(grid: Grid, store: Store | None, force: bool = False, cfg_fuel: dict | None = None,
             base_dir: "Path | None" = None) -> tuple[list[float], list[dict], str]:
    """Combustible por celda (catastro CONAF si existe, con relleno WorldCover),
    con caché en la base. La clave incluye bbox, tamaño de celda y la firma del
    archivo de catastro, así que cambiar el archivo invalida la caché."""
    from pathlib import Path

    from . import fuel as fuelmod

    cfg_fuel = cfg_fuel or {}
    base_dir = Path(base_dir) if base_dir else Path.cwd()
    key = "fuel_grid:" + grid.key() + "|" + fuelmod.catastros_signature(fuelmod.find_catastros(cfg_fuel, base_dir), cfg_fuel.get("rules"))
    if store and not force:
        raw = store.get_setting(key)
        if raw:
            d = json.loads(raw)
            return d["fuel"], d["frac"], d.get("source", "ESA WorldCover 2021")
    res = fuelmod.build_fuel(grid, cfg_fuel, base_dir, SUB_DEG)
    if store:
        store.set_setting(key, json.dumps({"fuel": res.fuel, "frac": res.fracs, "source": res.source,
                                           "detail": res.detail, "computed_at": iso(utcnow())}, ensure_ascii=False))
        store.set_setting("fuel_source", res.source)
    return res.fuel, res.fracs, res.source


# ---------------------------------------------------------------- meteorología

def fetch_weather_batch(points: list[tuple[float, float]], tz: str, chunk: int = 100) -> list[dict | None]:
    """Open-Meteo admite varias coordenadas por llamada; devuelve una lista alineada."""
    out: list[dict | None] = [None] * len(points)
    for s in range(0, len(points), chunk):
        part = points[s:s + chunk]
        params = {
            "latitude": ",".join(f"{la:.3f}" for la, _ in part),
            "longitude": ",".join(f"{lo:.3f}" for _, lo in part),
            "hourly": ",".join(wx.HOURLY), "past_days": 1, "forecast_days": 2,
            "timezone": tz, "wind_speed_unit": "kmh",
        }
        try:
            data = http_get(wx.API, params=params).json()
        except Exception as e:  # noqa: BLE001
            log.warning("Open-Meteo grilla (celdas %d-%d) falló: %s", s, s + len(part) - 1, e)
            continue
        if isinstance(data, dict):
            data = [data]
        for i, d in enumerate(data):
            if s + i < len(out):
                out[s + i] = d
    return out


# ---------------------------------------------------------------- cálculo

def compute_cells(grid: Grid, fuel: list[float], fracs: list[dict], weather: list[dict | None],
                  store: Store | None, now_utc: datetime, forecast_hours: int = 24) -> list[dict]:
    cells: list[dict] = []
    for idx in range(grid.n):
        lat, lon = grid.center(idx)
        d = weather[idx] if idx < len(weather) else None
        cell = {"i": idx, "lat": round(lat, 4), "lon": round(lon, 4), "fuel": fuel[idx], "cover": fracs[idx]}
        if d is None:
            cell.update({"cond_now": None, "cond_next": None, "risk_now": None, "risk_next": None, "class": "sin datos"})
            cells.append(cell)
            continue
        try:
            pw = wx.compute_point(f"grid:{idx}", lat, lon, d, store, now_utc, forecast_hours)
        except Exception as e:  # noqa: BLE001
            log.debug("celda %d: %s", idx, e)
            cell.update({"cond_now": None, "cond_next": None, "risk_now": None, "risk_next": None, "class": "sin datos"})
            cells.append(cell)
            continue
        cn = pw.now.score if pw.now else 0.0
        cx = pw.worst_next.score if pw.worst_next else cn
        rn, rx = cn * fuel[idx], cx * fuel[idx]
        cell.update({
            "cond_now": round(cn, 3), "cond_next": round(cx, 3),
            "risk_now": round(rn, 3), "risk_next": round(rx, 3), "class": risk_class(rn), "class_next": risk_class(rx),
            "temp": round(pw.now.temp_c, 1) if pw.now else None, "rh": round(pw.now.rh) if pw.now else None,
            "wind": round(pw.now.wind_kmh) if pw.now else None, "wind_dir": round(pw.now.wind_dir) if pw.now else None,
            "pi": round(pw.now.ignition_prob) if pw.now else None,
            "boton_rojo": bool(pw.now.rule_boton_rojo) if pw.now else False,
            "r303030": bool(pw.now.rule_303030) if pw.now else False,
            "fwi": round(pw.fwi.fwi, 1) if pw.fwi else None, "fwi_class": pw.fwi.danger_class if pw.fwi else None,
            "precip_24h": round(pw.precip_24h, 1),
            "worst_next_time": pw.worst_next.time_local if pw.worst_next else None,
        })
        cells.append(cell)
    return cells


@dataclass
class RiskGridResult:
    grid: Grid
    computed_at: datetime
    cells: list[dict] = field(default_factory=list)
    fuel_source: str = "ESA WorldCover 2021"

    def top(self, n: int = 10, key: str = "risk_now") -> list[dict]:
        valid = [c for c in self.cells if c.get(key) is not None and c["fuel"] > 0.05]
        return sorted(valid, key=lambda c: -c[key])[:n]

    def cells_near(self, lon: float, lat: float, radius_deg: float) -> list[dict]:
        return [c for c in self.cells if abs(c["lon"] - lon) <= radius_deg and abs(c["lat"] - lat) <= radius_deg]

    def as_dict(self) -> dict:
        return {
            "bbox": list(self.grid.bbox), "cell_deg": self.grid.cell_deg, "ncols": self.grid.ncols,
            "nrows": self.grid.nrows, "computed_at": iso(self.computed_at), "cells": self.cells,
            "top_now": self.top(10, "risk_now"), "top_next": self.top(10, "risk_next"),
            "summary": self.summary(), "fuel_source": self.fuel_source,
        }

    def summary(self) -> dict:
        valid = [c for c in self.cells if c.get("risk_now") is not None]
        counts: dict[str, int] = {}
        for c in valid:
            counts[c["class"]] = counts.get(c["class"], 0) + 1
        return {"n_cells": len(self.cells), "n_valid": len(valid), "by_class": counts,
                "n_boton_rojo": sum(1 for c in valid if c.get("boton_rojo")),
                "n_303030": sum(1 for c in valid if c.get("r303030"))}

    @classmethod
    def from_dict(cls, d: dict) -> "RiskGridResult":
        g = Grid(tuple(d["bbox"]), float(d["cell_deg"]), int(d["ncols"]), int(d["nrows"]))
        return cls(g, parse_iso(d["computed_at"]), d["cells"], d.get("fuel_source", "ESA WorldCover 2021"))


def compute_risk_grid(cfg_grid: dict, tz: str, store: Store | None, now_utc: datetime | None = None,
                      weather_data: list[dict | None] | None = None,
                      fuel_data: tuple[list[float], list[dict]] | None = None,
                      base_dir: "Path | None" = None) -> RiskGridResult:
    now_utc = now_utc or utcnow()
    grid = Grid.from_cfg(cfg_grid)
    if fuel_data is not None:
        fuel, fracs = fuel_data
        source = "proporcionado"
    else:
        fuel, fracs, source = get_fuel(grid, store, cfg_fuel=cfg_grid.get("fuel", {}), base_dir=base_dir)
    pts = [grid.center(i) for i in range(grid.n)]
    weather = weather_data if weather_data is not None else fetch_weather_batch(pts, tz, int(cfg_grid.get("chunk", 100)))
    cells = compute_cells(grid, fuel, fracs, weather, store, now_utc, int(cfg_grid.get("forecast_hours", 24)))
    res = RiskGridResult(grid, now_utc, cells, source)
    if store:
        store.set_setting("risk_grid", json.dumps(res.as_dict(), ensure_ascii=False))
    log.info("Riesgo por zonas: %d celdas, %s", grid.n, res.summary()["by_class"])
    return res


def load_cached(store: Store, max_age_min: float) -> RiskGridResult | None:
    raw = store.get_setting("risk_grid")
    if not raw:
        return None
    try:
        res = RiskGridResult.from_dict(json.loads(raw))
    except Exception:  # noqa: BLE001
        return None
    if utcnow() - res.computed_at > timedelta(minutes=max_age_min):
        return None
    s = res.summary()
    if max_age_min < 1e8 and s["n_valid"] < s["n_cells"]:
        log.info("Grilla en caché incompleta (%d/%d celdas); se recalcula", s["n_valid"], s["n_cells"])
        return None
    return res


def get_risk_grid(cfg_grid: dict, tz: str, store: Store, now_utc: datetime | None = None, force: bool = False,
                  base_dir: "Path | None" = None) -> RiskGridResult | None:
    """Grilla fresca desde caché si tiene menos de refresh_min; si no, se recalcula.
    Un cambio del archivo de catastro también fuerza el recálculo."""
    if not cfg_grid.get("enabled", True):
        return None
    if not force:
        cached = load_cached(store, float(cfg_grid.get("refresh_min", 120)))
        if cached and cached.grid.key() == Grid.from_cfg(cfg_grid).key():
            from pathlib import Path

            from . import fuel as fuelmod

            cfg_fuel = cfg_grid.get("fuel", {})
            sig = fuelmod.catastros_signature(fuelmod.find_catastros(cfg_fuel, Path(base_dir) if base_dir else Path.cwd()),
                                              cfg_fuel.get("rules"))
            prev = store.get_setting("fuel_signature")
            if prev == sig or (prev is None and sig == "worldcover"):
                return cached
            log.info("El combustible cambió (%s -> %s); se recalcula la grilla", prev, sig)
    try:
        res = compute_risk_grid(cfg_grid, tz, store, now_utc, base_dir=base_dir)
        from pathlib import Path

        from . import fuel as fuelmod

        cfg_fuel = cfg_grid.get("fuel", {})
        store.set_setting("fuel_signature", fuelmod.catastros_signature(
            fuelmod.find_catastros(cfg_fuel, Path(base_dir) if base_dir else Path.cwd()), cfg_fuel.get("rules")))
        return res
    except Exception as e:  # noqa: BLE001
        log.warning("Riesgo por zonas falló: %s", e)
        return load_cached(store, 1e9)
