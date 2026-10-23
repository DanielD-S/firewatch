"""Condiciones horarias de ignición y reglas de aviso (30-30-30, tipo Botón Rojo).

Probabilidad de ignición: componente de ignición del NFDRS de EE.UU.
(Deeming, Burgan & Cohen 1977), a partir de la humedad del combustible fino
(1 h) estimada con la humedad de equilibrio de Simard (1968). Es una
aproximación: CONAF usa el sistema KITRAL, cuyas tablas no son públicas.

Regla tipo Botón Rojo (umbrales públicos de CONAF): probabilidad de ignición
>= 70 %, viento >= 20 km/h y hora local entre 14:00 y 18:59.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def equilibrium_moisture(temp_c: float, rh: float) -> float:
    """Humedad de equilibrio (%) del combustible fino, Simard 1968."""
    t = c_to_f(temp_c)
    rh = min(max(rh, 0.0), 100.0)
    if rh < 10.0:
        emc = 0.03229 + 0.281073 * rh - 0.000578 * rh * t
    elif rh < 50.0:
        emc = 2.22749 + 0.160107 * rh - 0.01478 * t
    else:
        emc = 21.0606 + 0.005565 * rh ** 2 - 0.00035 * rh * t - 0.483199 * rh
    return max(emc, 1.0)


def fine_fuel_moisture(temp_c: float, rh: float, is_day: bool) -> float:
    """Humedad del combustible de 1 h (%). De día en sitio expuesto el
    combustible está más seco que el equilibrio por calentamiento solar."""
    emc = equilibrium_moisture(temp_c, rh)
    return max(emc * (0.85 if is_day else 1.05), 1.0)


def ignition_probability(temp_c: float, rh: float, is_day: bool, cloud_fraction: float = 0.0) -> float:
    """Probabilidad de ignición 0-100 (componente de ignición NFDRS 1988).

    QIGN: calor necesario para llevar el combustible fino a ignición (cal/g);
    CHI y PNORM según Burgan (1988). Referencia: FM 10 % a 80 °F ≈ 40 %,
    FM 5 % a 100 °F ≈ 95 %, FM 20 % ≈ 5 %.
    """
    mc = fine_fuel_moisture(temp_c, rh, is_day)
    # Temperatura del combustible: el aire más calentamiento solar según nubosidad.
    heating_f = (25.0 * (1.0 - min(max(cloud_fraction, 0.0), 1.0))) if is_day else 0.0
    tf = c_to_f(temp_c) + heating_f
    qig = (144.5 - 0.266 * tf - 0.00058 * tf ** 2 - 0.01 * tf * mc
           + 18.54 * (1.0 - math.exp(-0.151 * mc)) + 6.4 * mc)
    if qig >= 344.0:
        return 0.0
    chi = (344.0 - qig) / 10.0
    pnorm = (chi ** 3.66) * 0.000923 / 50.0
    return float(min(max(pnorm * 100.0, 0.0), 100.0))


@dataclass
class HourCondition:
    time_local: str
    temp_c: float
    rh: float
    wind_kmh: float
    gust_kmh: float
    wind_dir: float
    precip_mm: float
    is_day: bool
    fuel_moisture: float
    ignition_prob: float
    rule_303030: bool
    rule_boton_rojo: bool
    score: float

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ("temp_c", "rh", "wind_kmh", "gust_kmh", "wind_dir", "precip_mm", "fuel_moisture", "ignition_prob"):
            d[k] = round(d[k], 1)
        d["score"] = round(self.score, 2)
        return d


def hour_condition(time_local: str, hour_local: int, temp_c: float, rh: float, wind_kmh: float,
                   gust_kmh: float, wind_dir: float, precip_mm: float, is_day: bool,
                   cloud_fraction: float = 0.0, fwi_value: float | None = None) -> HourCondition:
    pi = ignition_probability(temp_c, rh, is_day, cloud_fraction)
    r303030 = temp_c >= 30.0 and rh <= 30.0 and max(wind_kmh, gust_kmh * 0.8) >= 30.0
    boton = pi >= 70.0 and wind_kmh >= 20.0 and 14 <= hour_local <= 18
    # Puntaje de condiciones 0-1: ignición, viento y (si hay) FWI diario.
    s_pi = pi / 100.0
    s_wind = min(wind_kmh / 40.0, 1.0)
    s_fwi = min((fwi_value or 0.0) / 38.0, 1.0)  # 38 = umbral "muy alto" EFFIS
    if fwi_value is None:
        score = 0.65 * s_pi + 0.35 * s_wind
    else:
        score = 0.45 * s_pi + 0.25 * s_wind + 0.30 * s_fwi
    if precip_mm >= 1.0:
        score *= 0.3
    if r303030 or boton:
        score = max(score, 0.7)
    return HourCondition(
        time_local=time_local, temp_c=temp_c, rh=rh, wind_kmh=wind_kmh, gust_kmh=gust_kmh,
        wind_dir=wind_dir, precip_mm=precip_mm, is_day=is_day, fuel_moisture=fine_fuel_moisture(temp_c, rh, is_day),
        ignition_prob=pi, rule_303030=r303030, rule_boton_rojo=boton, score=min(score, 1.0),
    )
