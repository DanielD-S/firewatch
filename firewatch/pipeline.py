"""Corrida completa: ingestar → meteorología → puntaje → alertas → notificar → exportar."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import alerts as alertsmod
from . import export, notify, riskgrid, scoring
from .config import Config
from .sectors import Sector, load_sectors
from .sources import firms, glm, goes_fdc, weather
from .store import Store, utcnow

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    started_at: datetime
    n_hotspots_new: int = 0
    n_flashes_new: int = 0
    assessments: list[scoring.SectorAssessment] = field(default_factory=list)
    alerts: list[alertsmod.Alert] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    state_path: str | None = None
    elapsed_s: float = 0.0
    risk_grid: riskgrid.RiskGridResult | None = None


def active_sectors_collection(cfg: Config, store: Store) -> dict:
    """GeoJSON del trazado activo: el subido por el usuario (guardado en la base)
    o, si no hay, el archivo del config."""
    import json

    raw = store.get_setting("sectors_geojson")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Trazado guardado en la base es inválido; se usa el del config")
    with open(cfg.path("sectors_file"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def active_sectors_info(cfg: Config, store: Store) -> dict:
    uploaded = bool(store.get_setting("sectors_geojson"))
    return {
        "path": store.get_setting("sectors_file") if uploaded else str(cfg.path("sectors_file")),
        "name": store.get_setting("sectors_name") if uploaded else cfg.path("sectors_file").name,
        "uploaded": uploaded,
        "uploaded_at": store.get_setting("sectors_uploaded_at"), "format": store.get_setting("sectors_format"),
    }


def ingest(cfg: Config, store: Store, now: datetime) -> tuple[int, int]:
    src = cfg["sources"]
    bbox = cfg.bbox
    n_h = n_f = 0
    if src.get("firms", {}).get("enabled", True):
        try:
            n_h += store.add_hotspots(firms.fetch(src["firms"], bbox))
        except Exception as e:  # noqa: BLE001
            log.warning("FIRMS falló: %s", e)
    if src.get("goes_fdc", {}).get("enabled", True):
        try:
            n_h += store.add_hotspots(goes_fdc.fetch(src["goes_fdc"], bbox, store, now))
        except Exception as e:  # noqa: BLE001
            log.warning("GOES FDC falló: %s", e)
    if src.get("glm", {}).get("enabled", True):
        try:
            n_f += store.add_flashes(glm.fetch(src["glm"], bbox, store, now))
        except Exception as e:  # noqa: BLE001
            log.warning("GLM falló: %s", e)
    return n_h, n_f


def grid_cfg_for_sectors(cfg_grid: dict, sectors: list[Sector], max_cells: int = 2500) -> dict:
    """Extiende el bbox de la grilla para cubrir todos los sectores (con margen de
    dos celdas), alineado a múltiplos del tamaño de celda para que el combustible
    cacheado siga siendo válido mientras el bbox no cambie."""
    if not cfg_grid.get("auto_extend", True) or not sectors:
        return cfg_grid
    import math

    cell = float(cfg_grid.get("cell_deg", 0.2))
    lon_min, lat_min, lon_max, lat_max = (float(v) for v in cfg_grid["bbox"])
    for s in sectors:
        x0, y0, x1, y1 = s.geom_wgs.bounds
        lon_min, lat_min = min(lon_min, x0 - 2 * cell), min(lat_min, y0 - 2 * cell)
        lon_max, lat_max = max(lon_max, x1 + 2 * cell), max(lat_max, y1 + 2 * cell)
    snap = lambda v, f: f(v / cell) * cell  # noqa: E731
    bbox = [round(snap(lon_min, math.floor), 4), round(snap(lat_min, math.floor), 4),
            round(snap(lon_max, math.ceil), 4), round(snap(lat_max, math.ceil), 4)]
    if bbox == [float(v) for v in cfg_grid["bbox"]]:
        return cfg_grid
    n = int(round((bbox[2] - bbox[0]) / cell)) * int(round((bbox[3] - bbox[1]) / cell))
    if n > max_cells:
        log.warning("Grilla extendida a los sectores tendría %d celdas (> %d); se mantiene el bbox del config", n, max_cells)
        return cfg_grid
    log.info("Grilla extendida para cubrir los sectores: %s (%d celdas)", bbox, n)
    return {**cfg_grid, "bbox": bbox}


def zone_for_sector(sector: Sector, rg: riskgrid.RiskGridResult | None) -> dict | None:
    """Celda de mayor riesgo cuyo centro cae en el corredor de vigilancia; si
    ninguna, la celda más cercana al centroide (hasta 1.5 celdas)."""
    if rg is None or not rg.cells:
        return None
    inside = [c for c in rg.cells if c.get("risk_now") is not None and sector.zone_of(c["lon"], c["lat"]) is not None]
    if not inside:
        lat0, lon0 = sector.centroid_wgs
        near = rg.cells_near(lon0, lat0, rg.grid.cell_deg * 1.5)
        inside = [c for c in near if c.get("risk_now") is not None]
    if not inside:
        return None
    return max(inside, key=lambda c: c["risk_now"])


def assess_all(cfg: Config, store: Store, sectors: list[Sector], now: datetime,
               fetch_weather: bool = True, rg: riskgrid.RiskGridResult | None = None) -> list[scoring.SectorAssessment]:
    sc = cfg["scoring"]
    hs = store.hotspots_since(now - timedelta(hours=24))
    fl = store.flashes_since(now - timedelta(hours=float(sc.get("lightning_lookback_h", 6))))
    wcfg = cfg["sources"].get("weather", {})
    out = []
    for s in sectors:
        wx = []
        if fetch_weather and wcfg.get("enabled", True):
            wx = weather.fetch_sector_weather(s.id, s.samples_wgs, cfg.timezone, store, now,
                                              int(wcfg.get("forecast_hours", 24)))
        a = scoring.assess_sector(s, hs, fl, wx, sc, cfg.get("exclusions", []), now,
                                  float(sc.get("lightning_dry_precip_mm", 1.0)),
                                  zone=zone_for_sector(s, rg), zone_cfg=cfg.get("risk_grid", {}))
        out.append(a)
        log.info("Sector %s: %s · det %.2f · cond %.2f · %s", s.id, a.level.upper(), a.detection_score,
                 a.conditions_now, a.reasons[0])
    return out


def run_once(cfg: Config, store: Store | None = None, notifiers: list[notify.Notifier] | None = None,
             now: datetime | None = None, do_ingest: bool = True, fetch_weather: bool = True,
             do_notify: bool = True) -> RunResult:
    t0 = time.time()
    now = now or utcnow()
    res = RunResult(started_at=now)
    own_store = store is None
    store = store or Store(cfg.path("db_path"))
    try:
        sectors = load_sectors(cfg, data=active_sectors_collection(cfg, store))
        if do_ingest:
            res.n_hotspots_new, res.n_flashes_new = ingest(cfg, store, now)
        rg_cfg = cfg.get("risk_grid", {})
        if rg_cfg.get("enabled", False):
            rg_cfg = grid_cfg_for_sectors(rg_cfg, sectors, int(rg_cfg.get("max_cells", 2500)))
        if rg_cfg.get("enabled", False) and fetch_weather:
            res.risk_grid = riskgrid.get_risk_grid(rg_cfg, cfg.timezone, store, now, base_dir=cfg.base_dir)
        elif rg_cfg.get("enabled", False):
            res.risk_grid = riskgrid.load_cached(store, 1e9)
        res.assessments = assess_all(cfg, store, sectors, now, fetch_weather, res.risk_grid)
        store.keep_only_sectors([s.id for s in sectors])
        res.alerts = alertsmod.process(res.assessments, store, cfg["alerts"].get("cooldown_min", {}), now)
        if do_notify and res.alerts:
            nots = notifiers if notifiers is not None else notify.build_notifiers(cfg.get("notify", {}))
            notify.dispatch(res.alerts, nots, store)
        res.state_path = str(export.export_state(res.assessments, store, cfg.path("web_dir"), now,
                                                 source=active_sectors_info(cfg, store),
                                                 risk_grid=res.risk_grid.as_dict() if res.risk_grid else None))
    finally:
        if own_store:
            store.close()
    res.elapsed_s = time.time() - t0
    return res


def watch(cfg: Config, interval_s: int = 600) -> None:
    store = Store(cfg.path("db_path"))
    nots = notify.build_notifiers(cfg.get("notify", {}))
    log.info("Vigilancia cada %d s. Canales: %s", interval_s, ", ".join(n.name for n in nots) or "ninguno")
    try:
        while True:
            try:
                r = run_once(cfg, store, nots)
                log.info("Corrida ok en %.0f s: %d focos nuevos, %d rayos nuevos, %d alertas",
                         r.elapsed_s, r.n_hotspots_new, r.n_flashes_new, len(r.alerts))
            except Exception as e:  # noqa: BLE001
                log.exception("Corrida falló: %s", e)
            time.sleep(interval_s)
    finally:
        store.close()
