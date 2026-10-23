from datetime import datetime, timedelta, timezone

from firewatch import alerts as alertsmod
from firewatch import fwi as fwimod
from firewatch import geo, scoring
from firewatch.conditions import hour_condition
from firewatch.sectors import Sector
from firewatch.sources.weather import PointWeather
from firewatch.store import Flash, Hotspot, Store
from shapely.geometry import LineString

CRS = "EPSG:32719"
NOW = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
SCORING = {
    "hotspot_min_conf": 0.25, "wind_toward_tolerance_deg": 50,
    "levels": {"rojo_near_score": 0.45, "rojo_watch_score": 0.6, "naranja_watch_score": 0.35, "amarillo_cond_score": 0.55},
}


def make_sector() -> Sector:
    # línea norte-sur de ~20 km en lon -71.0
    g = LineString([(-71.0, -35.0), (-71.0, -35.18)])
    gm = geo.to_metric(g, CRS)
    return Sector("L1", "Línea test", {}, g, gm, gm.buffer(500), gm.buffer(5000), geo.sample_points_along(gm, 20000), CRS)


def weather_for(sector: Sector, temp: float, rh: float, wind: float, wind_dir: float, precip24: float = 0.0,
                fwi_val: float = 20.0) -> list[PointWeather]:
    out = []
    for i, (lat, lon) in enumerate(sector.samples_wgs):
        now = hour_condition("2026-01-15T15:00", 15, temp, rh, wind, wind * 1.3, wind_dir, 0.0, True, 0.0, fwi_val)
        nxt = hour_condition("2026-01-15T16:00", 16, temp, rh, wind, wind * 1.3, wind_dir, 0.0, True, 0.0, fwi_val)
        f = fwimod.FWIResult(90, 30, 300, 10, 40, fwi_val)
        out.append(PointWeather(f"L1:{i}", lat, lon, f, now, [nxt], precip24, nxt))
    return out


def hs(lat, lon, conf=0.9, source="firms", sat="VIIRS SNPP", age_min=30, hid="h1") -> Hotspot:
    return Hotspot(hid, source, sat, lat, lon, NOW - timedelta(minutes=age_min), conf, 10.0, None, "D")


def test_green_when_nothing():
    s = make_sector()
    a = scoring.assess_sector(s, [], [], weather_for(s, 18, 60, 10, 270), SCORING, [], NOW)
    assert a.level == "verde"


def test_yellow_on_critical_conditions_only():
    s = make_sector()
    a = scoring.assess_sector(s, [], [], weather_for(s, 35, 15, 35, 270), SCORING, [], NOW)
    assert a.level == "amarillo"
    assert any("30-30-30" in r or "Botón Rojo" in r for r in a.reasons)


def test_red_when_hotspot_in_near_corridor():
    s = make_sector()
    h = hs(-35.09, -71.002)  # ~180 m de la línea
    a = scoring.assess_sector(s, [h], [], weather_for(s, 30, 25, 20, 270), SCORING, [], NOW)
    assert a.level == "rojo"
    assert a.hits[0].zone == "near"


def test_watch_hotspot_wind_toward_and_critical_is_red_but_not_with_calm_humid():
    s = make_sector()
    h = hs(-35.09, -71.035)  # ~3.2 km al oeste de la línea
    # viento del oeste (270) sopla hacia el este, o sea hacia la línea; condiciones críticas
    a = scoring.assess_sector(s, [h], [], weather_for(s, 36, 12, 35, 270, fwi_val=40), SCORING, [], NOW)
    assert a.level == "rojo"
    assert a.hits[0].wind_toward is True and a.hits[0].eta_min is not None
    # mismo foco, viento del este (aleja) y noche húmeda: baja a naranja
    b = scoring.assess_sector(s, [h], [], weather_for(s, 14, 85, 5, 90, fwi_val=3), SCORING, [], NOW)
    assert b.level == "naranja"
    assert b.hits[0].wind_toward is False
    assert b.hits[0].effective_score < a.hits[0].effective_score


def test_conditions_act_as_prior():
    s = make_sector()
    h = hs(-35.09, -71.035, conf=0.5)
    hot = scoring.assess_sector(s, [h], [], weather_for(s, 36, 12, 35, 270), SCORING, [], NOW)
    cold = scoring.assess_sector(s, [h], [], weather_for(s, 10, 90, 2, 270, fwi_val=1), SCORING, [], NOW)
    assert hot.detection_score > cold.detection_score


def test_exclusion_mask_suppresses():
    s = make_sector()
    h = hs(-35.09, -71.002)
    ex = [{"name": "fundición", "lat": -35.09, "lon": -71.002, "radius_m": 1000}]
    a = scoring.assess_sector(s, [h], [], weather_for(s, 30, 25, 20, 270), SCORING, ex, NOW)
    assert a.level in ("verde", "amarillo") and not a.hits


def test_persistence_and_multisource_bonus():
    s = make_sector()
    h1 = hs(-35.09, -71.03, conf=0.5, source="goes_fdc", sat="GOES-19", age_min=20, hid="g1")
    h2 = hs(-35.091, -71.031, conf=0.5, source="goes_fdc", sat="GOES-19", age_min=10, hid="g2")
    h3 = hs(-35.09, -71.03, conf=0.6, source="firms", sat="VIIRS NOAA-20", age_min=60, hid="v1")
    a = scoring.assess_sector(s, [h1, h2, h3], [], weather_for(s, 30, 25, 20, 270), SCORING, [], NOW)
    best = max(a.hits, key=lambda x: x.effective_score)
    assert best.persistence >= 1 and best.multi_source


def test_dry_lightning_raises_level():
    s = make_sector()
    f = Flash("f1", "glm", -35.05, -71.01, NOW - timedelta(minutes=40), 1e-14)
    a = scoring.assess_sector(s, [], [f], weather_for(s, 33, 20, 25, 270, precip24=0.0), SCORING, [], NOW)
    assert a.level == "naranja" and a.dry_lightning
    b = scoring.assess_sector(s, [], [f], weather_for(s, 20, 70, 10, 270, precip24=8.0), SCORING, [], NOW)
    assert b.level == "verde"


def test_alert_cooldown_and_escalation(tmp_path):
    s = make_sector()
    store = Store(tmp_path / "t.sqlite")
    cd = {"amarillo": 720, "naranja": 90, "rojo": 20}
    a_amar = scoring.assess_sector(s, [], [], weather_for(s, 35, 15, 35, 270), SCORING, [], NOW)
    out1 = alertsmod.process([a_amar], store, cd, NOW)
    assert len(out1) == 1 and out1[0].level == "amarillo"
    # misma situación 10 min después: en enfriamiento
    out2 = alertsmod.process([a_amar], store, cd, NOW + timedelta(minutes=10))
    assert out2 == []
    # escalada a rojo: notifica de inmediato
    a_rojo = scoring.assess_sector(s, [hs(-35.09, -71.002)], [], weather_for(s, 35, 15, 35, 270), SCORING, [], NOW)
    out3 = alertsmod.process([a_rojo], store, cd, NOW + timedelta(minutes=11))
    assert len(out3) == 1 and out3[0].level == "rojo"
    assert "maps.google.com" in out3[0].message
    # mismo rojo 5 min después, mismo foco: en enfriamiento
    assert alertsmod.process([a_rojo], store, cd, NOW + timedelta(minutes=16)) == []
    # nuevo foco cercano distinto: rompe el enfriamiento
    a_rojo2 = scoring.assess_sector(s, [hs(-35.09, -71.002), hs(-35.12, -71.001, hid="h9")], [],
                                    weather_for(s, 35, 15, 35, 270), SCORING, [], NOW)
    assert len(alertsmod.process([a_rojo2], store, cd, NOW + timedelta(minutes=17))) == 1
    store.close()
