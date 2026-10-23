"""Canadian Forest Fire Weather Index (Van Wagner 1987; Van Wagner & Pickett 1985).

Implementación equivalente a la del paquete R `cffdrs`, con factores de
duración del día según hemisferio y latitud. Entradas diarias a mediodía
local: temperatura (°C), humedad relativa (%), viento (km/h) y lluvia 24 h (mm).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Factores de duración del día para DMC (ell) por mes, según banda de latitud.
_DMC_EL = {
    "N30": [6.5, 7.5, 9.0, 12.8, 13.9, 13.9, 12.4, 10.9, 9.4, 8.0, 7.0, 6.0],      # lat >= 30 N
    "N10": [7.9, 8.4, 8.9, 9.5, 9.9, 10.2, 10.1, 9.7, 9.1, 8.6, 8.1, 7.8],           # 10 N .. 30 N
    "EQ": [9.0] * 12,                                                                 # -10 .. 10
    "S10": [10.1, 9.6, 9.1, 8.5, 8.1, 7.8, 7.9, 8.3, 8.9, 9.4, 9.9, 10.2],           # -30 .. -10
    "S30": [11.5, 10.5, 9.2, 7.9, 6.8, 6.2, 6.5, 7.4, 8.7, 10.0, 11.2, 11.8],        # lat <= -30
}
# Factores de duración del día para DC (fl) por mes.
_DC_FL = {
    "N": [-1.6, -1.6, -1.6, 0.9, 3.8, 5.8, 6.4, 5.0, 2.4, 0.4, -1.6, -1.6],          # lat >= 20 N
    "EQ": [1.4] * 12,                                                                 # -20 .. 20
    "S": [6.4, 5.0, 2.4, 0.4, -1.6, -1.6, -1.6, -1.6, -1.6, 0.9, 3.8, 5.8],          # lat <= -20
}


def _dmc_el(lat: float, month: int) -> float:
    if lat >= 30:
        band = "N30"
    elif lat >= 10:
        band = "N10"
    elif lat > -10:
        band = "EQ"
    elif lat > -30:
        band = "S10"
    else:
        band = "S30"
    return _DMC_EL[band][month - 1]


def _dc_fl(lat: float, month: int) -> float:
    if lat >= 20:
        band = "N"
    elif lat > -20:
        band = "EQ"
    else:
        band = "S"
    return _DC_FL[band][month - 1]


def ffmc_calc(ffmc0: float, temp: float, rh: float, ws: float, rain: float) -> float:
    rh = min(max(rh, 0.0), 100.0)
    mo = 147.2 * (101.0 - ffmc0) / (59.5 + ffmc0)
    if rain > 0.5:
        rf = rain - 0.5
        if mo > 150.0:
            mo = (mo + 42.5 * rf * math.exp(-100.0 / (251.0 - mo)) * (1.0 - math.exp(-6.93 / rf))
                  + 0.0015 * (mo - 150.0) ** 2 * math.sqrt(rf))
        else:
            mo = mo + 42.5 * rf * math.exp(-100.0 / (251.0 - mo)) * (1.0 - math.exp(-6.93 / rf))
        mo = min(mo, 250.0)
    ed = (0.942 * rh ** 0.679 + 11.0 * math.exp((rh - 100.0) / 10.0)
          + 0.18 * (21.1 - temp) * (1.0 - math.exp(-0.115 * rh)))
    if mo > ed:
        ko = 0.424 * (1.0 - (rh / 100.0) ** 1.7) + 0.0694 * math.sqrt(ws) * (1.0 - (rh / 100.0) ** 8)
        kd = ko * 0.581 * math.exp(0.0365 * temp)
        m = ed + (mo - ed) * 10.0 ** (-kd)
    else:
        ew = (0.618 * rh ** 0.753 + 10.0 * math.exp((rh - 100.0) / 10.0)
              + 0.18 * (21.1 - temp) * (1.0 - math.exp(-0.115 * rh)))
        if mo < ew:
            k1 = (0.424 * (1.0 - ((100.0 - rh) / 100.0) ** 1.7)
                  + 0.0694 * math.sqrt(ws) * (1.0 - ((100.0 - rh) / 100.0) ** 8))
            kw = k1 * 0.581 * math.exp(0.0365 * temp)
            m = ew - (ew - mo) * 10.0 ** (-kw)
        else:
            m = mo
    ffmc = 59.5 * (250.0 - m) / (147.2 + m)
    return min(max(ffmc, 0.0), 101.0)


def dmc_calc(dmc0: float, temp: float, rh: float, rain: float, lat: float, month: int) -> float:
    rh = min(max(rh, 0.0), 100.0)
    temp = max(temp, -1.1)
    el = _dmc_el(lat, month)
    rk = 1.894 * (temp + 1.1) * (100.0 - rh) * el * 1e-4
    if rain > 1.5:
        rw = 0.92 * rain - 1.27
        wmi = 20.0 + 280.0 / math.exp(0.023 * dmc0)
        if dmc0 <= 33.0:
            b = 100.0 / (0.5 + 0.3 * dmc0)
        elif dmc0 <= 65.0:
            b = 14.0 - 1.3 * math.log(dmc0)
        else:
            b = 6.2 * math.log(dmc0) - 17.2
        wmr = wmi + 1000.0 * rw / (48.77 + b * rw)
        pr = 43.43 * (5.6348 - math.log(wmr - 20.0))
    else:
        pr = dmc0
    pr = max(pr, 0.0)
    return max(pr + rk, 0.0)


def dc_calc(dc0: float, temp: float, rain: float, lat: float, month: int) -> float:
    temp = max(temp, -2.8)
    fl = _dc_fl(lat, month)
    pe = max((0.36 * (temp + 2.8) + fl) / 2.0, 0.0)
    if rain > 2.8:
        rw = 0.83 * rain - 1.27
        smi = 800.0 * math.exp(-dc0 / 400.0)
        dr = max(dc0 - 400.0 * math.log(1.0 + 3.937 * rw / smi), 0.0)
    else:
        dr = dc0
    return max(dr + pe, 0.0)


def isi_calc(ffmc: float, ws: float) -> float:
    fm = 147.2 * (101.0 - ffmc) / (59.5 + ffmc)
    ff = 91.9 * math.exp(-0.1386 * fm) * (1.0 + fm ** 5.31 / 4.93e7)
    return 0.208 * ff * math.exp(0.05039 * ws)


def bui_calc(dmc: float, dc: float) -> float:
    if dmc == 0 and dc == 0:
        return 0.0
    if dmc <= 0.4 * dc:
        bui = 0.8 * dmc * dc / (dmc + 0.4 * dc)
    else:
        bui = dmc - (1.0 - 0.8 * dc / (dmc + 0.4 * dc)) * (0.92 + (0.0114 * dmc) ** 1.7)
    return max(bui, 0.0)


def fwi_calc(isi: float, bui: float) -> float:
    if bui <= 80.0:
        bb = 0.1 * isi * (0.626 * bui ** 0.809 + 2.0)
    else:
        bb = 0.1 * isi * (1000.0 / (25.0 + 108.64 / math.exp(0.023 * bui)))
    if bb > 1.0:
        return math.exp(2.72 * (0.434 * math.log(bb)) ** 0.647)
    return bb


@dataclass
class FWIResult:
    ffmc: float
    dmc: float
    dc: float
    isi: float
    bui: float
    fwi: float

    @property
    def danger_class(self) -> str:
        """Clases usadas por EFFIS/Copernicus para el FWI."""
        f = self.fwi
        if f < 5.2:
            return "muy bajo"
        if f < 11.2:
            return "bajo"
        if f < 21.3:
            return "moderado"
        if f < 38.0:
            return "alto"
        if f < 50.0:
            return "muy alto"
        return "extremo"

    def as_dict(self) -> dict:
        return {
            "ffmc": round(self.ffmc, 1), "dmc": round(self.dmc, 1), "dc": round(self.dc, 1),
            "isi": round(self.isi, 1), "bui": round(self.bui, 1), "fwi": round(self.fwi, 1),
            "clase": self.danger_class,
        }


def fwi_daily(ffmc0: float, dmc0: float, dc0: float, temp: float, rh: float, ws: float,
              rain: float, lat: float, month: int) -> FWIResult:
    ffmc = ffmc_calc(ffmc0, temp, rh, ws, rain)
    dmc = dmc_calc(dmc0, temp, rh, rain, lat, month)
    dc = dc_calc(dc0, temp, rain, lat, month)
    isi = isi_calc(ffmc, ws)
    bui = bui_calc(dmc, dc)
    return FWIResult(ffmc=ffmc, dmc=dmc, dc=dc, isi=isi, bui=bui, fwi=fwi_calc(isi, bui))


DEFAULT_START = (85.0, 6.0, 15.0)
