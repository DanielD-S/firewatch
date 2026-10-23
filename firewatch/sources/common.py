"""Utilidades compartidas por los ingestores: HTTP, listado S3 anónimo, ids."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Iterator

import requests

UA = {"User-Agent": "firewatch/0.1 (+https://github.com/local/firewatch)"}
TIMEOUT = 60


def http_get(url: str, retries: int = 3, **kw) -> requests.Response:
    """GET con reintentos ante 429 (cuota) y 5xx, respetando Retry-After."""
    import logging
    import time

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            last = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        if r.status_code == 429 or 500 <= r.status_code < 600:
            if attempt < retries:
                try:
                    wait = float(r.headers.get("Retry-After", "0"))
                except ValueError:
                    wait = 0.0
                wait = min(max(wait, 10.0 * (attempt + 1)), 90.0)
                logging.getLogger(__name__).warning("HTTP %s en %s; reintento en %.0f s", r.status_code, url.split("?")[0], wait)
                time.sleep(wait)
                continue
        r.raise_for_status()
        return r
    raise last or RuntimeError("http_get agotó los reintentos")


def short_id(*parts: object) -> str:
    h = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return h[:20]


def in_bbox(lon: float, lat: float, bbox: tuple[float, float, float, float]) -> bool:
    lon_min, lat_min, lon_max, lat_max = bbox
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max


# ---- S3 anónimo (buckets NOAA Open Data) ----

_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def s3_list(bucket: str, prefix: str) -> list[tuple[str, int]]:
    """Lista (key, size) bajo un prefijo, con paginación."""
    out: list[tuple[str, int]] = []
    token: str | None = None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        r = http_get(f"https://{bucket}.s3.amazonaws.com/", params=params)
        root = ET.fromstring(r.content)
        for c in root.findall(f"{_S3_NS}Contents"):
            k = c.find(f"{_S3_NS}Key")
            s = c.find(f"{_S3_NS}Size")
            if k is not None and k.text:
                out.append((k.text, int(s.text) if s is not None and s.text else 0))
        trunc = root.find(f"{_S3_NS}IsTruncated")
        if trunc is not None and trunc.text == "true":
            nt = root.find(f"{_S3_NS}NextContinuationToken")
            token = nt.text if nt is not None else None
            if not token:
                break
        else:
            break
    return out


def s3_download(bucket: str, key: str) -> bytes:
    r = http_get(f"https://{bucket}.s3.amazonaws.com/{key}")
    return r.content


def s3_download_many(bucket: str, keys: list[str], workers: int = 6) -> dict[str, bytes | Exception]:
    """Descarga varios objetos en paralelo. Devuelve {key: bytes | Exception}."""
    from concurrent.futures import ThreadPoolExecutor

    out: dict[str, bytes | Exception] = {}

    def one(k: str):
        try:
            return k, s3_download(bucket, k)
        except Exception as e:  # noqa: BLE001
            return k, e

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for k, v in ex.map(one, keys):
            out[k] = v
    return out


_GOES_TS = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)")


def goes_start_time(key: str) -> datetime | None:
    """Hora de inicio de escaneo codificada en el nombre: _sYYYYDDDHHMMSSt."""
    m = _GOES_TS.search(key)
    if not m:
        return None
    year, doy, hh, mm, ss, tenth = (int(g) for g in m.groups())
    base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
    return base.replace(hour=hh, minute=mm, second=ss, microsecond=tenth * 100_000)


def hour_prefixes(product: str, start: datetime, end: datetime) -> Iterator[str]:
    """Prefijos S3 por hora UTC entre start y end: PRODUCT/YYYY/DDD/HH/."""
    t = start.replace(minute=0, second=0, microsecond=0)
    while t <= end:
        yield f"{product}/{t.year:04d}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"
        t += timedelta(hours=1)


def goes_keys_in_window(bucket: str, product: str, start: datetime, end: datetime) -> list[tuple[str, int, datetime]]:
    keys: list[tuple[str, int, datetime]] = []
    for pre in hour_prefixes(product, start, end):
        for key, size in s3_list(bucket, pre):
            ts = goes_start_time(key)
            if ts and start <= ts <= end:
                keys.append((key, size, ts))
    keys.sort(key=lambda k: k[2])
    return keys
