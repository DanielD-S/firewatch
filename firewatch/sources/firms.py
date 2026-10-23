"""NASA FIRMS: CSV regionales públicos (sin MAP_KEY) de las últimas 24 h.

Productos: VIIRS SNPP / NOAA-20 / NOAA-21 (375 m) y MODIS Terra/Aqua (1 km).
Latencia típica para Sudamérica: 1 a 3 horas tras la pasada.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone

from ..store import Hotspot
from .common import http_get, in_bbox, short_id

log = logging.getLogger(__name__)

BASE = "https://firms.modaps.eosdis.nasa.gov/data/active_fire"

SAT_NAMES = {
    "N": "VIIRS SNPP",
    "1": "VIIRS NOAA-20",
    "2": "VIIRS NOAA-21",
    "T": "MODIS Terra",
    "A": "MODIS Aqua",
}

VIIRS_CONF = {"l": 0.3, "low": 0.3, "n": 0.6, "nominal": 0.6, "h": 0.9, "high": 0.9}


def normalize_confidence(raw: str) -> float:
    s = (raw or "").strip().lower()
    if s in VIIRS_CONF:
        return VIIRS_CONF[s]
    try:
        v = float(s)
        return max(0.0, min(1.0, v / 100.0))
    except ValueError:
        return 0.5


def parse_csv(text: str, source_name: str, bbox) -> list[Hotspot]:
    out: list[Hotspot] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
        except (KeyError, ValueError):
            continue
        if not in_bbox(lon, lat, bbox):
            continue
        hhmm = row.get("acq_time", "0000").zfill(4)
        try:
            t = datetime.strptime(f"{row['acq_date']} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        sat_code = (row.get("satellite") or "").strip()
        sat = SAT_NAMES.get(sat_code, f"{source_name} {sat_code}".strip())
        frp = row.get("frp")
        try:
            frp_v = float(frp) if frp not in (None, "") else None
        except ValueError:
            frp_v = None
        hs = Hotspot(
            id=short_id("firms", sat, f"{lat:.4f}", f"{lon:.4f}", t.isoformat()),
            source="firms",
            satellite=sat,
            lat=lat,
            lon=lon,
            acq_time=t,
            confidence=normalize_confidence(row.get("confidence", "")),
            frp=frp_v,
            raw_conf=row.get("confidence"),
            daynight=row.get("daynight"),
        )
        out.append(hs)
    return out


def fetch(cfg_src: dict, bbox) -> list[Hotspot]:
    region = cfg_src.get("region", "South_America")
    window = cfg_src.get("window", "24h")
    out: list[Hotspot] = []
    for prod in cfg_src.get("products", []):
        url = f"{BASE}/{prod['dir']}/csv/{prod['prefix']}_{region}_{window}.csv"
        try:
            r = http_get(url)
        except Exception as e:  # noqa: BLE001
            log.warning("FIRMS %s no disponible: %s", prod["name"], e)
            continue
        items = parse_csv(r.text, prod["name"], bbox)
        log.info("FIRMS %s: %d focos en bbox", prod["name"], len(items))
        out.extend(items)
    return out
