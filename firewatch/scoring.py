"""Puntaje por sector: fusión de detección (focos, rayos) con condiciones.

Idea central: las condiciones actúan como *prior*. Un foco de confianza media
con condiciones críticas pesa más que el mismo foco en una noche húmeda; y
unas condiciones críticas sin indicio de fuego solo generan aviso preventivo.

Niveles: verde < amarillo < naranja < rojo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import geo
from .sectors import Sector
from .sources.weather import PointWeather
from .store import Flash, Hotspot

LEVELS = ["verde", "amarillo", "naranja", "rojo"]
LEVEL_RANK = {lv: i for i, lv in enumerate(LEVELS)}


@dataclass
class HotspotHit:
    hotspot: Hotspot
    zone: str                      # near | watch
    distance_m: float
    bearing_to_sector: float
    wind_toward: bool | None
    age_h: float
    base_score: float
    effective_score: float
    persistence: int
    multi_source: bool
    eta_min: float | None

    def as_dict(self) -> dict:
        h = self.hotspot
        return {
            "id": h.id, "source": h.source, "satellite": h.satellite, "lat": round(h.lat, 4), "lon": round(h.lon, 4),
            "acq_time": h.acq_time.strftime("%Y-%m-%dT%H:%M:%SZ"), "confidence": round(h.confidence, 2),
            "frp": round(h.frp, 1) if h.frp is not None else None, "raw_conf": h.raw_conf,
            "zone": self.zone, "distance_m": round(self.distance_m), "bearing": round(self.bearing_to_sector),
            "wind_toward": self.wind_toward, "age_h": round(self.age_h, 1),
            "base_score": round(self.base_score, 2), "effective_score": round(self.effective_score, 2),
            "persistence": self.persistence, "multi_source": self.multi_source,
            "eta_min": round(self.eta_min) if self.eta_min is not None else None,
        }


@dataclass
class SectorAssessment:
    sector: Sector
    level: str
    detection_score: float
    conditions_now: float
    conditions_next: float
    lightning_score: float
    reasons: list[str]
    hits: list[HotspotHit] = field(default_factory=list)
    flashes: list[Flash] = field(default_factory=list)
    weather: list[PointWeather] = field(default_factory=list)
    precip_24h: float = 0.0
    dry_lightning: bool = False
    zone: dict | None = None          # celda de mayor riesgo del mapa de zonas sobre el corredor

    @property
    def worst_now(self):
        pts = [w.now for w in self.weather if w.now]
        return max(pts, key=lambda c: c.score) if pts else None

    @property
    def worst_next(self):
        pts = [w.worst_next for w in self.weather if w.worst_next]
        return max(pts, key=lambda c: c.score) if pts else None

    @property
    def fwi(self):
        vals = [w.fwi for w in self.weather if w.fwi]
        return max(vals, key=lambda f: f.fwi) if vals else None

    def summary(self) -> dict:
        wn, wx, fw = self.worst_now, self.worst_next, self.fwi
        return {
            "sector_id": self.sector.id, "name": self.sector.name, "level": self.level,
            "detection_score": round(self.detection_score, 2),
            "conditions_now": round(self.conditions_now, 2),
            "conditions_next": round(self.conditions_next, 2),
            "lightning_score": round(self.lightning_score, 2),
            "dry_lightning": self.dry_lightning,
            "precip_24h": round(self.precip_24h, 1),
            "reasons": self.reasons,
            "n_hits_near": sum(1 for h in self.hits if h.zone == "near"),
            "n_hits_watch": sum(1 for h in self.hits if h.zone == "watch"),
            "n_flashes": len(self.flashes),
            "fwi": fw.as_dict() if fw else None,
            "now": wn.as_dict() if wn else None,
            "worst_next": wx.as_dict() if wx else None,
            "zone": {k: self.zone.get(k) for k in ("i", "lat", "lon", "fuel", "cover", "cond_now", "cond_next", "risk_now",
                                                   "risk_next", "class", "class_next", "temp", "rh", "wind", "pi", "fwi",
                                                   "boton_rojo", "r303030")} if self.zone else None,
            "hits": [h.as_dict() for h in sorted(self.hits, key=lambda h: -h.effective_score)[:20]],
        }


def _excluded(h: Hotspot, exclusions: list[dict]) -> str | None:
    for ex in exclusions:
        if geo.haversine_m(h.lat, h.lon, float(ex["lat"]), float(ex["lon"])) <= float(ex.get("radius_m", 3000)):
            return str(ex.get("name", "exclusion"))
    return None


def _nearest_weather(lat: float, lon: float, weather: list[PointWeather]) -> PointWeather | None:
    if not weather:
        return None
    return min(weather, key=lambda w: geo.haversine_m(lat, lon, w.lat, w.lon))


def crude_ros_m_per_min(wind_kmh: float, cond: float) -> float:
    """Velocidad de propagación gruesa (m/min). Marcador de posición hasta
    integrar un simulador (Cell2Fire). ~1.3 km/h con 30 km/h y condiciones altas."""
    return 1.0 + 0.6 * max(wind_kmh, 0.0) * (0.3 + min(max(cond, 0.0), 1.0))


def assess_sector(sector: Sector, hotspots: list[Hotspot], flashes: list[Flash], weather: list[PointWeather],
                  cfg_scoring: dict, exclusions: list[dict], now: datetime,
                  lightning_dry_mm: float = 1.0, zone: dict | None = None,
                  zone_cfg: dict | None = None) -> SectorAssessment:
    tol = float(cfg_scoring.get("wind_toward_tolerance_deg", 50))
    min_conf = float(cfg_scoring.get("hotspot_min_conf", 0.25))
    lv = cfg_scoring.get("levels", {})
    t_rojo_near = float(lv.get("rojo_near_score", 0.45))
    t_rojo_watch = float(lv.get("rojo_watch_score", 0.6))
    t_naranja = float(lv.get("naranja_watch_score", 0.35))
    t_amarillo_cond = float(lv.get("amarillo_cond_score", 0.55))

    cond_now = max((w.now.score for w in weather if w.now), default=0.0)
    cond_next = max((w.worst_next.score for w in weather if w.worst_next), default=0.0)
    precip = min((w.precip_24h for w in weather), default=0.0)

    # ---- focos dentro del corredor de vigilancia ----
    cand: list[tuple[Hotspot, str, float]] = []
    for h in hotspots:
        if h.confidence < min_conf:
            continue
        zn = sector.zone_of(h.lon, h.lat)
        if zn is None:
            continue
        if _excluded(h, exclusions):
            continue
        cand.append((h, zn, sector.distance_m(h.lon, h.lat)))

    hits: list[HotspotHit] = []
    for h, zn, dist in cand:
        # persistencia: otros focos a < 2.5 km con distinta hora de adquisición
        others = [o for o, _, _ in cand if o.id != h.id and geo.haversine_m(h.lat, h.lon, o.lat, o.lon) < 2500]
        persistence = len({o.acq_time.replace(second=0, microsecond=0) for o in others if o.acq_time != h.acq_time})
        multi = any(o.source != h.source for o in others)
        age_h = max(0.0, (now - h.acq_time).total_seconds() / 3600.0)
        base = h.confidence + min(0.3, 0.15 * persistence) + (0.2 if multi else 0.0)
        if age_h > 12:
            base *= 0.3
        elif age_h > 6:
            base *= 0.6
        base = min(base, 1.0)
        # prior de condiciones: multiplica entre 0.6 (húmedo/frío) y 1.2 (crítico)
        eff = min(1.0, base * (0.6 + 0.6 * cond_now))
        npt = sector.nearest_point_wgs(h.lon, h.lat)
        bearing = geo.bearing_deg(h.lat, h.lon, npt[0], npt[1])
        w = _nearest_weather(h.lat, h.lon, weather)
        wind_toward = None
        eta = None
        if w and w.now:
            wind_toward = geo.wind_blows_toward(w.now.wind_dir, bearing, tol)
            if zn == "watch" and wind_toward:
                ros = crude_ros_m_per_min(max(w.now.wind_kmh, w.now.gust_kmh * 0.7), cond_now)
                eta = dist / ros
        hits.append(HotspotHit(h, zn, dist, bearing, wind_toward, age_h, base, eff, persistence, multi, eta))

    det = max((x.effective_score for x in hits), default=0.0)

    # ---- rayos ----
    fl_in = [f for f in flashes if sector.zone_of(f.lon, f.lat) is not None]
    dry = precip < lightning_dry_mm
    if fl_in:
        l_score = min(0.6, 0.2 * len(fl_in)) if dry else min(0.2, 0.05 * len(fl_in))
    else:
        l_score = 0.0

    # ---- nivel ----
    reasons: list[str] = []
    level = "verde"
    near_hits = [x for x in hits if x.zone == "near"]
    watch_hits = [x for x in hits if x.zone == "watch"]
    best_near = max(near_hits, key=lambda x: x.effective_score, default=None)
    best_watch = max(watch_hits, key=lambda x: x.effective_score, default=None)

    if best_near and best_near.effective_score >= t_rojo_near:
        level = "rojo"
        reasons.append(f"Foco {best_near.hotspot.satellite} a {best_near.distance_m:.0f} m del trazado "
                       f"(confianza {best_near.hotspot.confidence:.0%}, puntaje {best_near.effective_score:.2f})")
    elif best_watch and best_watch.effective_score >= t_rojo_watch and best_watch.wind_toward and cond_now >= 0.5:
        level = "rojo"
        eta_txt = f", llegada estimada en {best_watch.eta_min:.0f} min" if best_watch.eta_min else ""
        reasons.append(f"Foco {best_watch.hotspot.satellite} a {best_watch.distance_m / 1000:.1f} km con viento hacia el "
                       f"trazado y condiciones críticas{eta_txt}")
    elif best_watch and best_watch.effective_score >= t_naranja:
        level = "naranja"
        wt = " con viento hacia el trazado" if best_watch.wind_toward else ""
        reasons.append(f"Foco {best_watch.hotspot.satellite} a {best_watch.distance_m / 1000:.1f} km del trazado{wt} "
                       f"(puntaje {best_watch.effective_score:.2f})")
    elif best_near:
        level = "naranja"
        reasons.append(f"Foco de baja confianza a {best_near.distance_m:.0f} m del trazado")
    elif fl_in and dry and cond_now >= 0.5:
        level = "naranja"
        reasons.append(f"{len(fl_in)} rayos secos en el corredor con condiciones críticas (lluvia 24 h {precip:.1f} mm)")
    elif fl_in and dry:
        level = "amarillo"
        reasons.append(f"{len(fl_in)} rayos en el corredor sin lluvia reciente: vigilar las próximas horas")

    if level in ("verde", "amarillo"):
        wn = max((w.now for w in weather if w.now), key=lambda c: c.score, default=None)
        wx = max((w.worst_next for w in weather if w.worst_next), key=lambda c: c.score, default=None)
        if wn and (wn.rule_boton_rojo or wn.rule_303030):
            level = max(level, "amarillo", key=lambda s: LEVEL_RANK[s])
            tag = "regla Botón Rojo" if wn.rule_boton_rojo else "regla 30-30-30"
            reasons.append(f"Condiciones críticas ahora ({tag}): {wn.temp_c:.0f} °C, HR {wn.rh:.0f} %, "
                           f"viento {wn.wind_kmh:.0f} km/h, ignición {wn.ignition_prob:.0f} %")
        elif cond_now >= t_amarillo_cond:
            level = max(level, "amarillo", key=lambda s: LEVEL_RANK[s])
            reasons.append(f"Condiciones de riesgo altas ahora (puntaje {cond_now:.2f})")
        elif wx and wx.score >= t_amarillo_cond:
            level = max(level, "amarillo", key=lambda s: LEVEL_RANK[s])
            reasons.append(f"Condiciones críticas previstas para {wx.time_local[11:16]} "
                           f"({wx.temp_c:.0f} °C, HR {wx.rh:.0f} %, viento {wx.wind_kmh:.0f} km/h)")
    fw = max((w.fwi for w in weather if w.fwi), key=lambda f: f.fwi, default=None)
    if fw and fw.fwi >= 21.3:
        reasons.append(f"FWI diario {fw.fwi:.0f} ({fw.danger_class})")

    # ---- riesgo de zona (mapa de celdas: condiciones x combustible) ----
    zc = zone_cfg or {}
    if zone and zone.get("risk_now") is not None:
        zr = float(zone["risk_now"])
        zx = float(zone.get("risk_next") or 0.0)
        desc = (f"combustible {zone.get('fuel', 0):.0%}, ignición {zone.get('pi') or 0:.0f} %, "
                f"viento {zone.get('wind') or 0:.0f} km/h")
        if zr >= float(zc.get("zone_amarillo", 0.55)):
            if LEVEL_RANK[level] < LEVEL_RANK["amarillo"]:
                level = "amarillo"
            reasons.append(f"Riesgo de zona {zone.get('class', '')} ({zr:.2f}) sobre el corredor: {desc}")
        elif zx >= float(zc.get("zone_amarillo", 0.55)):
            if LEVEL_RANK[level] < LEVEL_RANK["amarillo"]:
                level = "amarillo"
            reasons.append(f"Riesgo de zona {zone.get('class_next', '')} previsto en 24 h ({zx:.2f}): {desc}")
        elif zr >= float(zc.get("zone_mention", 0.35)):
            reasons.append(f"Riesgo de zona {zone.get('class', '')} ({zr:.2f}): {desc}")

    if not reasons:
        reasons.append("Sin focos en el corredor ni condiciones críticas")

    return SectorAssessment(
        sector=sector, level=level, detection_score=det, conditions_now=cond_now, conditions_next=cond_next,
        lightning_score=l_score, reasons=reasons, hits=hits, flashes=fl_in, weather=weather,
        precip_24h=precip, dry_lightning=bool(fl_in and dry), zone=zone,
    )
