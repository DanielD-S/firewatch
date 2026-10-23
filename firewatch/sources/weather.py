"""Open-Meteo (gratuito, sin clave): meteorología horaria por punto de muestreo,
cálculo del FWI diario con estado persistente y condiciones horarias.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import fwi as fwimod
from ..conditions import HourCondition, hour_condition
from ..store import Store
from .common import http_get

log = logging.getLogger(__name__)

API = "https://api.open-meteo.com/v1/forecast"
HOURLY = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m",
          "wind_gusts_10m", "precipitation", "cloud_cover", "is_day"]


@dataclass
class PointWeather:
    key: str
    lat: float
    lon: float
    fwi: fwimod.FWIResult | None
    now: HourCondition | None
    next_hours: list[HourCondition] = field(default_factory=list)
    precip_24h: float = 0.0
    worst_next: HourCondition | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key, "lat": round(self.lat, 4), "lon": round(self.lon, 4),
            "fwi": self.fwi.as_dict() if self.fwi else None,
            "now": self.now.as_dict() if self.now else None,
            "worst_next": self.worst_next.as_dict() if self.worst_next else None,
            "precip_24h": round(self.precip_24h, 1),
        }


def fetch_point(lat: float, lon: float, tz: str, forecast_days: int = 2) -> dict:
    params = {
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "hourly": ",".join(HOURLY),
        "past_days": 1, "forecast_days": forecast_days,
        "timezone": tz, "wind_speed_unit": "kmh",
    }
    r = http_get(API, params=params)
    return r.json()


def _parse_local(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M")


def compute_point(key: str, lat: float, lon: float, data: dict, store: Store | None,
                  now_utc: datetime, forecast_hours: int = 24) -> PointWeather:
    h = data["hourly"]
    offset = int(data.get("utc_offset_seconds", 0))
    now_local = (now_utc + timedelta(seconds=offset)).replace(tzinfo=None)
    times = [_parse_local(t) for t in h["time"]]
    n = len(times)

    def col(name: str) -> list[float]:
        vals = h.get(name) or [None] * n
        return [float(v) if v is not None else 0.0 for v in vals]

    temp, rh, ws, wd = col("temperature_2m"), col("relative_humidity_2m"), col("wind_speed_10m"), col("wind_direction_10m")
    gust, pr, cc, isd = col("wind_gusts_10m"), col("precipitation"), col("cloud_cover"), col("is_day")

    # índice de la hora actual (la última <= ahora)
    i_now = max(i for i, t in enumerate(times) if t <= now_local) if any(t <= now_local for t in times) else 0

    # lluvia últimas 24 h
    precip_24h = sum(pr[i] for i in range(max(0, i_now - 23), i_now + 1))

    # ---- FWI diario a mediodía local, con estado persistente ----
    fwi_res: fwimod.FWIResult | None = None
    today = now_local.date()
    # último mediodía disponible <= ahora
    noon_idx = [i for i, t in enumerate(times) if t.hour == 12 and t <= now_local]
    if noon_idx:
        i12 = noon_idx[-1]
        d12 = times[i12].date()
        rain24 = sum(pr[i] for i in range(max(0, i12 - 23), i12 + 1))
        state = store.get_fwi_state(key) if store else None
        if state and state["date_local"] == d12.isoformat():
            ffmc0, dmc0, dc0 = state["ffmc"], state["dmc"], state["dc"]
            # ya calculado hoy: recomputar desde el estado del día anterior no es
            # posible sin guardarlo; usamos los valores guardados como resultado.
            isi = fwimod.isi_calc(ffmc0, ws[i12])
            bui = fwimod.bui_calc(dmc0, dc0)
            fwi_res = fwimod.FWIResult(ffmc0, dmc0, dc0, isi, bui, fwimod.fwi_calc(isi, bui))
        else:
            if state:
                ffmc0, dmc0, dc0 = state["ffmc"], state["dmc"], state["dc"]
            else:
                ffmc0, dmc0, dc0 = fwimod.DEFAULT_START
            fwi_res = fwimod.fwi_daily(ffmc0, dmc0, dc0, temp[i12], rh[i12], ws[i12], rain24, lat, d12.month)
            if store:
                store.set_fwi_state(key, d12.isoformat(), fwi_res.ffmc, fwi_res.dmc, fwi_res.dc)

    fwi_val = fwi_res.fwi if fwi_res else None

    def hc(i: int) -> HourCondition:
        return hour_condition(
            time_local=times[i].strftime("%Y-%m-%dT%H:%M"), hour_local=times[i].hour,
            temp_c=temp[i], rh=rh[i], wind_kmh=ws[i], gust_kmh=gust[i], wind_dir=wd[i],
            precip_mm=pr[i], is_day=isd[i] >= 0.5, cloud_fraction=cc[i] / 100.0, fwi_value=fwi_val,
        )

    now_c = hc(i_now)
    nxt = [hc(i) for i in range(i_now + 1, min(n, i_now + 1 + forecast_hours))]
    worst = max(nxt, key=lambda c: c.score) if nxt else None
    return PointWeather(key=key, lat=lat, lon=lon, fwi=fwi_res, now=now_c, next_hours=nxt,
                        precip_24h=precip_24h, worst_next=worst)


def fetch_sector_weather(sector_id: str, samples: list[tuple[float, float]], tz: str, store: Store | None,
                         now_utc: datetime, forecast_hours: int = 24) -> list[PointWeather]:
    out: list[PointWeather] = []
    for i, (lat, lon) in enumerate(samples):
        key = f"{sector_id}:{i}"
        try:
            data = fetch_point(lat, lon, tz)
            out.append(compute_point(key, lat, lon, data, store, now_utc, forecast_hours))
        except Exception as e:  # noqa: BLE001
            log.warning("Open-Meteo %s (%.3f,%.3f) falló: %s", key, lat, lon, e)
    return out
