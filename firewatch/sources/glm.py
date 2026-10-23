"""GOES-R GLM L2 Lightning Cluster-Filter Algorithm: descargas de rayos cada 20 s.

Se usan los *flashes* (no eventos ni grupos) con lat/lon y energía.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

import numpy as np

from ..store import Flash, Store
from .common import goes_keys_in_window, in_bbox, s3_download_many, short_id

log = logging.getLogger(__name__)

_UNITS = re.compile(r"seconds since (\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(\.\d+)?")


def _epoch_from_units(units: str, fallback: datetime) -> datetime:
    m = _UNITS.search(units or "")
    if not m:
        return fallback
    frac = m.group(3) or ""
    return datetime.strptime(f"{m.group(1)} {m.group(2)}{frac}", "%Y-%m-%d %H:%M:%S" + (".%f" if frac else "")).replace(tzinfo=timezone.utc)


def parse_glm_file(path: str, bbox, file_time: datetime, satellite: str) -> list[Flash]:
    import netCDF4

    out: list[Flash] = []
    with netCDF4.Dataset(path, "r") as ds:
        if "flash_lat" not in ds.variables:
            return out
        lat = np.asarray(ds.variables["flash_lat"][:], dtype=np.float64)
        lon = np.asarray(ds.variables["flash_lon"][:], dtype=np.float64)
        energy = np.ma.filled(ds.variables["flash_energy"][:], np.nan).astype(np.float64) if "flash_energy" in ds.variables else np.full(lat.shape, np.nan)
        tvar = ds.variables.get("flash_time_offset_of_first_event")
        if tvar is not None:
            offs = np.ma.filled(tvar[:], 0.0).astype(np.float64)
            epoch = _epoch_from_units(getattr(tvar, "units", ""), file_time)
        else:
            offs = np.zeros(lat.shape)
            epoch = file_time
        for i in range(len(lat)):
            la, lo = float(lat[i]), float(lon[i])
            if not in_bbox(lo, la, bbox):
                continue
            t = epoch + timedelta(seconds=float(offs[i]))
            e = float(energy[i]) if np.isfinite(energy[i]) else None
            out.append(Flash(
                id=short_id("glm", satellite, f"{la:.4f}", f"{lo:.4f}", t.isoformat()),
                source="glm", lat=la, lon=lo, flash_time=t, energy=e,
            ))
    return out


def fetch(cfg_src: dict, bbox, store: Store, now: datetime | None = None) -> list[Flash]:
    now = now or datetime.now(timezone.utc)
    bucket = cfg_src.get("bucket", "noaa-goes19")
    product = cfg_src.get("product", "GLM-L2-LCFA")
    lookback = int(cfg_src.get("lookback_min", 20))
    max_files = int(cfg_src.get("max_files", 60))
    satellite = "GOES-19" if "19" in bucket else ("GOES-18" if "18" in bucket else bucket)
    start = now - timedelta(minutes=lookback)
    try:
        keys = goes_keys_in_window(bucket, product, start, now)
    except Exception as e:  # noqa: BLE001
        log.warning("GLM listado falló: %s", e)
        return []
    keys = [k for k in keys if not store.file_ingested(k[0])][-max_files:]
    out: list[Flash] = []
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
                items = parse_glm_file(tmp, bbox, ts, satellite)
            finally:
                os.unlink(tmp)
            store.mark_file(key, len(items))
            out.extend(items)
        except Exception as e:  # noqa: BLE001
            log.warning("GLM %s falló: %s", key, e)
    log.info("GLM: %d archivos, %d rayos en bbox", len(keys), len(out))
    return out
