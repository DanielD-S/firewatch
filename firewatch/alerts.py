"""Decisión de alertar: enfriamiento por nivel, escalada inmediata y evidencia nueva."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from .scoring import LEVEL_RANK, SectorAssessment
from .store import Store, utcnow

EMOJI = {"verde": "🟢", "amarillo": "🟡", "naranja": "🟠", "rojo": "🔴"}


@dataclass
class Alert:
    id: int
    sector_id: str
    sector_name: str
    level: str
    title: str
    message: str
    evidence: dict


def build_message(a: SectorAssessment) -> tuple[str, str]:
    s = a.sector
    title = f"{EMOJI[a.level]} {a.level.upper()} · {s.name}"
    lines = [title, ""]
    for r in a.reasons:
        lines.append(f"• {r}")
    top = sorted(a.hits, key=lambda h: -h.effective_score)[:3]
    if top:
        lines.append("")
        lines.append("Focos:")
        for h in top:
            hs = h.hotspot
            wt = " viento→línea" if h.wind_toward else ""
            eta = f" ETA≈{h.eta_min:.0f} min" if h.eta_min else ""
            lines.append(f"- {hs.satellite} {hs.acq_time.strftime('%H:%M')}Z {hs.lat:.4f},{hs.lon:.4f} "
                         f"{h.distance_m / 1000:.1f} km {h.zone}{wt}{eta}")
            lines.append(f"  https://maps.google.com/?q={hs.lat:.5f},{hs.lon:.5f}")
    wn = a.worst_now
    if wn:
        lines.append("")
        lines.append(f"Ahora: {wn.temp_c:.0f} °C · HR {wn.rh:.0f} % · viento {wn.wind_kmh:.0f} km/h "
                     f"({wn.wind_dir:.0f}°) · ignición {wn.ignition_prob:.0f} %")
    fw = a.fwi
    if fw:
        lines.append(f"FWI {fw.fwi:.0f} ({fw.danger_class}) · lluvia 24 h {a.precip_24h:.1f} mm")
    lines.append(f"Puntajes: detección {a.detection_score:.2f} · condiciones {a.conditions_now:.2f} "
                 f"(próx. 24 h {a.conditions_next:.2f}) · rayos {a.lightning_score:.2f}")
    return title, "\n".join(lines)


def should_alert(a: SectorAssessment, store: Store, cooldown_min: dict, now: datetime | None = None) -> tuple[bool, str]:
    now = now or utcnow()
    if a.level == "verde":
        return False, "verde"
    last = store.last_alert(a.sector.id)
    if last is None:
        return True, "primera alerta"
    last_level = last["level"]
    last_time = datetime.strptime(last["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=now.tzinfo)
    if LEVEL_RANK[a.level] > LEVEL_RANK[last_level]:
        return True, f"escalada {last_level}→{a.level}"
    cd = timedelta(minutes=float(cooldown_min.get(a.level, 60)))
    same_recent = store.last_alert_level_since(a.sector.id, a.level, now - cd)
    if same_recent is None:
        return True, "enfriamiento cumplido"
    # evidencia nueva: foco cercano no alertado antes
    try:
        prev_ids = set(json.loads(same_recent["evidence"]).get("hit_ids", []))
    except Exception:  # noqa: BLE001
        prev_ids = set()
    new_near = [h for h in a.hits if h.zone == "near" and h.hotspot.id not in prev_ids]
    if a.level == "rojo" and new_near:
        return True, "nuevo foco en corredor cercano"
    return False, "en enfriamiento"


def process(assessments: list[SectorAssessment], store: Store, cooldown_min: dict, now: datetime | None = None) -> list[Alert]:
    out: list[Alert] = []
    for a in assessments:
        store.set_sector_state(a.sector.id, a.level, a.summary())
        ok, why = should_alert(a, store, cooldown_min, now)
        if not ok:
            continue
        title, msg = build_message(a)
        evidence = {"why": why, "hit_ids": [h.hotspot.id for h in a.hits], **a.summary()}
        aid = store.add_alert(a.sector.id, a.level, title, msg, evidence)
        out.append(Alert(aid, a.sector.id, a.sector.name, a.level, title, msg, evidence))
    return out
