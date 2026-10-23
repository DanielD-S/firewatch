"""Exporta el estado actual a web/state.json para el visor Leaflet."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .scoring import SectorAssessment
from .store import Flash, Hotspot, Store, iso, utcnow


def _hotspot_feature(h: Hotspot) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(h.lon, 5), round(h.lat, 5)]},
        "properties": {
            "id": h.id, "source": h.source, "satellite": h.satellite, "acq_time": iso(h.acq_time),
            "confidence": round(h.confidence, 2), "frp": h.frp, "raw_conf": h.raw_conf,
        },
    }


def _flash_feature(f: Flash) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(f.lon, 5), round(f.lat, 5)]},
        "properties": {"id": f.id, "time": iso(f.flash_time), "energy": f.energy},
    }


def export_state(assessments: list[SectorAssessment], store: Store, web_dir: Path, now: datetime | None = None,
                 hotspot_hours: int = 24, lightning_hours: int = 6, source: dict | None = None,
                 risk_grid: dict | None = None) -> Path:
    now = now or utcnow()
    web_dir.mkdir(parents=True, exist_ok=True)
    hs = store.hotspots_since(now - timedelta(hours=hotspot_hours))
    fl = store.flashes_since(now - timedelta(hours=lightning_hours))
    sectors_fc = {"type": "FeatureCollection", "features": []}
    rings_fc = {"type": "FeatureCollection", "features": []}
    weather_pts = []
    for a in assessments:
        s = a.summary()
        sectors_fc["features"].append(a.sector.to_feature({k: v for k, v in s.items() if k != "hits"} | {"hits": s["hits"]}))
        rings_fc["features"].append(a.sector.buffers_feature())
        rings_fc["features"].append(a.sector.near_feature())
        for w in a.weather:
            d = w.as_dict()
            d["sector_id"] = a.sector.id
            weather_pts.append(d)
    state = {
        "generated_at": iso(now),
        "sectors": sectors_fc,
        "rings": rings_fc,
        "hotspots": {"type": "FeatureCollection", "features": [_hotspot_feature(h) for h in hs]},
        "lightning": {"type": "FeatureCollection", "features": [_flash_feature(f) for f in fl]},
        "weather_points": weather_pts,
        "alerts": store.recent_alerts(72),
        "counts": {"hotspots_24h": len(hs), "lightning_6h": len(fl), "sectors": len(assessments)},
        "sectors_source": source or {},
        "risk_grid": risk_grid,
    }
    out = web_dir / "state.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    return out
