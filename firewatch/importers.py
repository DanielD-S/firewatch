"""Importación de trazados subidos por el usuario: KMZ, KML, Shapefile (zip) y GeoJSON.

Devuelve siempre un FeatureCollection GeoJSON en WGS84 con `id` y `name` por
feature, listo para `sectors.load_sectors`.
"""
from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pyproj import CRS, Transformer
from shapely import force_2d
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

ID_KEYS = ["id", "fid", "objectid", "codigo", "código", "cod", "id_tramo", "tramo", "cod_linea", "codigo_linea"]
NAME_KEYS = ["name", "nombre", "nom", "linea", "línea", "line", "tramo", "descripcion", "descripción", "desc", "titulo", "título"]
MAX_FEATURES = 5000


class ImportError_(ValueError):
    """Error de importación con mensaje para el usuario."""


@dataclass
class ImportResult:
    collection: dict
    n_features: int
    n_skipped: int
    source_format: str
    crs_in: str
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- utilidades

def _pick(props: dict, keys: list[str], fuzzy: tuple[str, ...] = ()) -> str | None:
    lower = {str(k).lower(): v for k, v in props.items()}
    for k in keys:
        v = lower.get(k)
        if v not in (None, ""):
            return str(v)
    # coincidencia parcial: COD_TRAMO, ID_LINEA, NOMBRE_LT, etc.
    for k, v in lower.items():
        if v not in (None, "") and any(f in k for f in fuzzy):
            return str(v)
    return None


def _normalize(features: list[dict]) -> tuple[list[dict], int]:
    out: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    for i, f in enumerate(features):
        geom = f.get("geometry")
        if not geom or not geom.get("coordinates") and geom.get("type") != "GeometryCollection":
            skipped += 1
            continue
        try:
            g = force_2d(shape(geom))
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        if g.is_empty:
            skipped += 1
            continue
        props = dict(f.get("properties") or {})
        sid = _pick(props, ID_KEYS, ("cod", "_id", "id_")) or f.get("id") or f"S{i + 1}"
        sid = re.sub(r"[^\w\-.]+", "_", str(sid))[:64]
        base = sid
        n = 2
        while sid in seen:
            sid = f"{base}_{n}"
            n += 1
        seen.add(sid)
        name = _pick(props, NAME_KEYS, ("nom", "name", "desc")) or sid
        props["id"] = sid
        props["name"] = name
        # mapping() devuelve tuplas; se pasan a listas para un GeoJSON canónico
        out.append({"type": "Feature", "geometry": json.loads(json.dumps(mapping(g))), "properties": props})
        if len(out) >= MAX_FEATURES:
            break
    return out, skipped


def _reproject(features: list[dict], crs_in: CRS) -> list[dict]:
    if crs_in.to_epsg() == 4326:
        return features
    tr = Transformer.from_crs(crs_in, "EPSG:4326", always_xy=True)
    out = []
    for f in features:
        g = shp_transform(tr.transform, shape(f["geometry"]))
        out.append({**f, "geometry": mapping(g)})
    return out


def _looks_like_lonlat(features: list[dict]) -> bool:
    for f in features[:50]:
        g = shape(f["geometry"])
        minx, miny, maxx, maxy = g.bounds
        if not (-180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90):
            return False
    return True


# ---------------------------------------------------------------- GeoJSON

def import_geojson(data: bytes) -> ImportResult:
    try:
        gj = json.loads(data.decode("utf-8-sig"))
    except Exception as e:  # noqa: BLE001
        raise ImportError_(f"GeoJSON inválido: {e}") from e
    t = gj.get("type")
    if t == "FeatureCollection":
        feats = gj.get("features") or []
    elif t == "Feature":
        feats = [gj]
    elif t in ("LineString", "MultiLineString", "Polygon", "MultiPolygon", "Point", "MultiPoint"):
        feats = [{"type": "Feature", "geometry": gj, "properties": {}}]
    else:
        raise ImportError_("El GeoJSON no contiene features")
    warnings: list[str] = []
    crs_name = "EPSG:4326"
    crs_obj = gj.get("crs", {}).get("properties", {}).get("name") if isinstance(gj.get("crs"), dict) else None
    if crs_obj and "4326" not in str(crs_obj) and "CRS84" not in str(crs_obj):
        try:
            crs = CRS.from_user_input(str(crs_obj).replace("urn:ogc:def:crs:", "").replace("::", ":"))
            feats = _reproject(feats, crs)
            crs_name = crs.to_string()
        except Exception:  # noqa: BLE001
            warnings.append(f"No se reconoció el CRS declarado ({crs_obj}); se asumió WGS84")
    feats, skipped = _normalize(feats)
    if not feats:
        raise ImportError_("El GeoJSON no tiene geometrías válidas")
    if not _looks_like_lonlat(feats):
        raise ImportError_("Las coordenadas no parecen lon/lat. Reproyecta a WGS84 o sube un Shapefile con .prj")
    return ImportResult({"type": "FeatureCollection", "features": feats}, len(feats), skipped, "geojson", crs_name, warnings)


# ---------------------------------------------------------------- KML / KMZ

def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _coords(text: str | None) -> list[list[float]]:
    pts = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            try:
                pts.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    return pts


def _kml_geoms(el: ET.Element) -> list[dict]:
    """Geometrías GeoJSON dentro de un Placemark (incluye MultiGeometry)."""
    out: list[dict] = []
    for node in el.iter():
        t = _local(node.tag)
        if t == "LineString":
            c = next((x for x in node.iter() if _local(x.tag) == "coordinates"), None)
            pts = _coords(c.text if c is not None else "")
            if len(pts) >= 2:
                out.append({"type": "LineString", "coordinates": pts})
        elif t == "Polygon":
            rings: list[list[list[float]]] = []
            outer = next((x for x in node.iter() if _local(x.tag) == "outerBoundaryIs"), None)
            if outer is not None:
                c = next((x for x in outer.iter() if _local(x.tag) == "coordinates"), None)
                r = _coords(c.text if c is not None else "")
                if len(r) >= 4:
                    rings.append(r)
            for inner in (x for x in node.iter() if _local(x.tag) == "innerBoundaryIs"):
                c = next((x for x in inner.iter() if _local(x.tag) == "coordinates"), None)
                r = _coords(c.text if c is not None else "")
                if len(r) >= 4:
                    rings.append(r)
            if rings:
                out.append({"type": "Polygon", "coordinates": rings})
        elif t == "Point":
            c = next((x for x in node.iter() if _local(x.tag) == "coordinates"), None)
            pts = _coords(c.text if c is not None else "")
            if pts:
                out.append({"type": "Point", "coordinates": pts[0]})
    return out


def _merge_geoms(geoms: list[dict]) -> list[dict]:
    """Une geometrías del mismo tipo en Multi*; tipos distintos quedan separados."""
    by_type: dict[str, list] = {}
    for g in geoms:
        by_type.setdefault(g["type"], []).append(g["coordinates"])
    out = []
    for t, coords in by_type.items():
        if len(coords) == 1:
            out.append({"type": t, "coordinates": coords[0]})
        else:
            out.append({"type": "Multi" + t, "coordinates": coords})
    return out


def import_kml(data: bytes) -> ImportResult:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ImportError_(f"KML inválido: {e}") from e
    feats: list[dict] = []
    for pm in (x for x in root.iter() if _local(x.tag) == "Placemark"):
        props: dict = {}
        name = next((x.text for x in pm if _local(x.tag) == "name"), None)
        if name:
            props["name"] = name.strip()
        desc = next((x.text for x in pm if _local(x.tag) == "description"), None)
        if desc:
            props["description"] = desc.strip()[:500]
        for d in (x for x in pm.iter() if _local(x.tag) == "Data"):
            v = next((x.text for x in d if _local(x.tag) == "value"), None)
            if d.get("name"):
                props[d.get("name")] = (v or "").strip()
        for sd in (x for x in pm.iter() if _local(x.tag) == "SimpleData"):
            if sd.get("name"):
                props[sd.get("name")] = (sd.text or "").strip()
        geoms = _merge_geoms(_kml_geoms(pm))
        for j, g in enumerate(geoms):
            p = dict(props)
            if len(geoms) > 1 and "name" in p:
                p["name"] = f"{p['name']} ({j + 1})"
            feats.append({"type": "Feature", "geometry": g, "properties": p})
    feats, skipped = _normalize(feats)
    if not feats:
        raise ImportError_("El KML no contiene Placemarks con líneas, polígonos o puntos")
    return ImportResult({"type": "FeatureCollection", "features": feats}, len(feats), skipped, "kml", "EPSG:4326")


def import_kmz(data: bytes) -> ImportResult:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ImportError_("KMZ inválido (no es un zip)") from e
    names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
    if not names:
        raise ImportError_("El KMZ no contiene ningún .kml")
    pref = next((n for n in names if PurePosixPath(n).name.lower() == "doc.kml"), names[0])
    r = import_kml(zf.read(pref))
    r.source_format = "kmz"
    return r


# ---------------------------------------------------------------- Shapefile

def import_shapefile_zip(data: bytes) -> ImportResult:
    import shapefile  # pyshp

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ImportError_("El zip es inválido") from e
    members = {PurePosixPath(n).suffix.lower(): n for n in zf.namelist() if not n.endswith("/")}
    shp_names = [n for n in zf.namelist() if n.lower().endswith(".shp")]
    if not shp_names:
        # puede ser un KMZ renombrado o un zip con KML/GeoJSON
        if any(n.lower().endswith(".kml") for n in zf.namelist()):
            return import_kmz(data)
        gj = [n for n in zf.namelist() if n.lower().endswith((".geojson", ".json"))]
        if gj:
            return import_geojson(zf.read(gj[0]))
        raise ImportError_("El zip no contiene un .shp (ni .kml ni .geojson)")
    shp_name = shp_names[0]
    stem = shp_name[:-4]

    def member(ext: str) -> bytes | None:
        for n in zf.namelist():
            if n.lower() == (stem + ext).lower():
                return zf.read(n)
        return None

    shx, dbf, prj, cpg = member(".shx"), member(".dbf"), member(".prj"), member(".cpg")
    if shx is None or dbf is None:
        raise ImportError_("El Shapefile necesita al menos .shp, .shx y .dbf dentro del zip")
    encoding = (cpg.decode("ascii", "ignore").strip() if cpg else "utf-8") or "utf-8"
    feats: list[dict] = []
    warnings: list[str] = []
    try:
        reader = shapefile.Reader(shp=io.BytesIO(zf.read(shp_name)), shx=io.BytesIO(shx), dbf=io.BytesIO(dbf), encoding=encoding)
    except UnicodeDecodeError:
        reader = shapefile.Reader(shp=io.BytesIO(zf.read(shp_name)), shx=io.BytesIO(shx), dbf=io.BytesIO(dbf), encoding="latin-1")
    with reader:
        fields = [f[0] for f in reader.fields if f[0] != "DeletionFlag"]
        for sr in reader.iterShapeRecords():
            try:
                geom = sr.shape.__geo_interface__
            except Exception:  # noqa: BLE001
                continue
            if not geom:
                continue
            rec = sr.record
            props = {}
            for k in fields:
                try:
                    v = rec[k]
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(v, bytes):
                    v = v.decode("latin-1", "ignore")
                if hasattr(v, "isoformat"):
                    v = v.isoformat()
                props[k] = v
            feats.append({"type": "Feature", "geometry": geom, "properties": props})
    crs_name = "EPSG:4326"
    if prj:
        try:
            crs = CRS.from_wkt(prj.decode("utf-8", "ignore").strip())
            crs_name = crs.to_string()
            feats = _reproject(feats, crs)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"No se pudo interpretar el .prj ({e}); se asumió WGS84")
    else:
        warnings.append("El zip no trae .prj; se asumió WGS84")
    feats, skipped = _normalize(feats)
    if not feats:
        raise ImportError_("El Shapefile no tiene geometrías válidas")
    if not _looks_like_lonlat(feats):
        raise ImportError_("Las coordenadas no quedaron en lon/lat; revisa el .prj del Shapefile")
    return ImportResult({"type": "FeatureCollection", "features": feats}, len(feats), skipped, "shapefile", crs_name, warnings)


# ---------------------------------------------------------------- entrada

def import_bytes(filename: str, data: bytes) -> ImportResult:
    ext = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if not data:
        raise ImportError_("El archivo está vacío")
    if ext in (".geojson", ".json"):
        return import_geojson(data)
    if ext == ".kml":
        return import_kml(data)
    if ext == ".kmz":
        return import_kmz(data)
    if ext == ".zip":
        return import_shapefile_zip(data)
    if ext in (".shp", ".shx", ".dbf", ".prj"):
        raise ImportError_("Sube el Shapefile como un .zip con .shp, .shx, .dbf y .prj")
    if ext == ".gpkg":
        raise ImportError_("GeoPackage no soportado aún; exporta a GeoJSON, KML o Shapefile")
    raise ImportError_(f"Formato no soportado: {ext or '(sin extensión)'}. Usa KMZ, KML, Shapefile (zip) o GeoJSON")
