"""GOES-R ABI L2 Fire/Hot Spot Characterization (FDC), disco completo, cada 10 min.

Se lee desde el bucket público NOAA en AWS (acceso anónimo) y se convierten
los píxeles de fuego de la grilla fija ABI a lat/lon.

Códigos de la máscara (GOES-R PUG, producto FDC):
  10 fuego procesado, 11 saturado, 12 contaminado por nube,
  13 alta probabilidad, 14 media, 15 baja. 30-35: los mismos con filtro temporal.
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone

import numpy as np

from ..store import Hotspot, Store
from .common import goes_keys_in_window, in_bbox, s3_download_many, short_id

log = logging.getLogger(__name__)

FIRE_CONF = {
    10: 0.9, 11: 0.9, 30: 0.9, 31: 0.9,
    13: 0.75, 33: 0.75,
    14: 0.5, 34: 0.5,
    12: 0.4, 32: 0.4,
    15: 0.3, 35: 0.3,
}
FIRE_CODES = np.array(sorted(FIRE_CONF.keys()), dtype=np.int16)


def fixed_grid_to_latlon(x: np.ndarray, y: np.ndarray, proj: dict) -> tuple[np.ndarray, np.ndarray]:
    """Convierte ángulos de escaneo (rad) de la grilla fija ABI a lat/lon (grados)."""
    req = float(proj["semi_major_axis"])
    rpol = float(proj["semi_minor_axis"])
    h = float(proj["perspective_point_height"]) + req
    lon0 = math.radians(float(proj["longitude_of_projection_origin"]))
    sx_, sy_ = np.sin(x), np.sin(y)
    cx_, cy_ = np.cos(x), np.cos(y)
    a = sx_ ** 2 + cx_ ** 2 * (cy_ ** 2 + (req ** 2 / rpol ** 2) * sy_ ** 2)
    b = -2.0 * h * cx_ * cy_
    c = h ** 2 - req ** 2
    disc = b ** 2 - 4 * a * c
    with np.errstate(invalid="ignore"):
        rs = (-b - np.sqrt(disc)) / (2 * a)
    sx = rs * cx_ * cy_
    sy = -rs * sx_
    sz = rs * cx_ * sy_
    lat = np.arctan((req ** 2 / rpol ** 2) * sz / np.sqrt((h - sx) ** 2 + sy ** 2))
    lon = lon0 - np.arctan(sy / (h - sx))
    return np.degrees(lat), np.degrees(lon)


def parse_fdc_file(path: str, bbox, scan_time: datetime, satellite: str) -> list[Hotspot]:
    import netCDF4  # import tardío para que el resto del paquete no lo requiera

    out: list[Hotspot] = []
    with netCDF4.Dataset(path, "r") as ds:
        mask_var = ds.variables["Mask"]
        mask = np.ma.filled(mask_var[:], -1).astype(np.int16)
        idx = np.argwhere(np.isin(mask, FIRE_CODES))
        if idx.size == 0:
            return out
        rows, cols = idx[:, 0], idx[:, 1]
        x = np.asarray(ds.variables["x"][:], dtype=np.float64)[cols]
        y = np.asarray(ds.variables["y"][:], dtype=np.float64)[rows]
        proj = {k: ds.variables["goes_imager_projection"].getncattr(k)
                for k in ("semi_major_axis", "semi_minor_axis", "perspective_point_height",
                          "longitude_of_projection_origin")}
        lat, lon = fixed_grid_to_latlon(x, y, proj)
        power = np.ma.filled(ds.variables["Power"][:], np.nan) if "Power" in ds.variables else None
        temp = np.ma.filled(ds.variables["Temp"][:], np.nan) if "Temp" in ds.variables else None
        for i in range(len(rows)):
            la, lo = float(lat[i]), float(lon[i])
            if not np.isfinite(la) or not np.isfinite(lo) or not in_bbox(lo, la, bbox):
                continue
            code = int(mask[rows[i], cols[i]])
            frp = float(power[rows[i], cols[i]]) if power is not None else None
            if frp is not None and not np.isfinite(frp):
                frp = None
            t = float(temp[rows[i], cols[i]]) if temp is not None else None
            out.append(Hotspot(
                id=short_id("goes", satellite, f"{la:.3f}", f"{lo:.3f}", scan_time.isoformat()),
                source="goes_fdc",
                satellite=satellite,
                lat=la, lon=lo,
                acq_time=scan_time,
                confidence=FIRE_CONF.get(code, 0.3),
                frp=frp,
                raw_conf=f"mask={code}" + (f";T={t:.0f}K" if t and np.isfinite(t) else ""),
                daynight=None,
            ))
    return out


def fetch(cfg_src: dict, bbox, store: Store, now: datetime | None = None) -> list[Hotspot]:
    now = now or datetime.now(timezone.utc)
    bucket = cfg_src.get("bucket", "noaa-goes19")
    product = cfg_src.get("product", "ABI-L2-FDCF")
    lookback = int(cfg_src.get("lookback_min", 60))
    max_files = int(cfg_src.get("max_files", 6))
    satellite = "GOES-19" if "19" in bucket else ("GOES-18" if "18" in bucket else bucket)
    start = now - timedelta(minutes=lookback)
    try:
        keys = goes_keys_in_window(bucket, product, start, now)
    except Exception as e:  # noqa: BLE001
        log.warning("GOES FDC listado falló: %s", e)
        return []
    keys = [k for k in keys if not store.file_ingested(k[0])][-max_files:]
    out: list[Hotspot] = []
    blobs = s3_download_many(bucket, [k[0] for k in keys])
    for key, size, ts in keys:
        try:
            data = blobs.get(key)
            if not isinstance(data, (bytes, bytearray)):
                raise RuntimeError(f"descarga falló: {data}")
            with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as fh:
                fh.write(data)
                tmp = fh.name
            try:
                items = parse_fdc_file(tmp, bbox, ts, satellite)
            finally:
                os.unlink(tmp)
            store.mark_file(key, len(items))
            log.info("GOES FDC %s: %d píxeles de fuego en bbox", ts.strftime("%H:%M"), len(items))
            out.extend(items)
        except Exception as e:  # noqa: BLE001
            log.warning("GOES FDC %s falló: %s", key, e)
    return out
