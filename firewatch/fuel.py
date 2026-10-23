"""Capa de combustible para el mapa de riesgo.

Dos fuentes, combinadas:
  1. Catastro de Uso de Suelo y Vegetación de CONAF (vectorial: Shapefile,
     GeoPackage, File Geodatabase, GeoJSON, o zip con Shapefile). Se clasifica
     cada polígono por sus atributos (USO, SUBUSO, ESTRUCTURA, TIPO_FOR,
     ESPECIE...) en una categoría con peso de combustible 0-1.
  2. ESA WorldCover 10 m como relleno donde el catastro no cubre.

Ambas se rasterizan a la subgrilla (~500 m) y se agregan por celda.
"""
from __future__ import annotations

import logging
import math
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# ---- categorías unificadas (catastro y WorldCover) ----
CATEGORIES = {
    1: ("plantación", 1.0),
    2: ("bosque nativo", 0.85),
    3: ("matorral", 0.9),
    4: ("pradera", 0.7),
    5: ("agrícola", 0.4),
    6: ("humedal", 0.3),
    7: ("urbano", 0.05),
    8: ("sin vegetación", 0.0),
    9: ("agua/nieve", 0.0),
    10: ("otro", 0.3),
    11: ("bosque", 1.0),          # WorldCover: árboles sin distinguir nativo/plantación
}
NODATA = -1

# Clases WorldCover -> categoría unificada
WORLDCOVER_CAT = {10: 11, 20: 3, 30: 4, 40: 5, 50: 7, 60: 8, 70: 9, 80: 9, 90: 6, 95: 6, 100: 10}

# Reglas por defecto para el catastro CONAF: se evalúan en orden, gana la primera.
# "fields": nombres de campo (coincidencia por substring, sin mayúsculas ni acentos)
# "contains": substrings del valor; "cat": categoría; "weight": peso opcional
DEFAULT_RULES: list[dict] = [
    # Nombres de campo del catastro 2025: USO_TIERRA, USO, SUBUSO, ESTRUCTURA, COBERTURA,
    # TIPO_FORES, SUBTIPOFOR, ESPECI1_CI..ESPECI6_CI (científico), ESPECI1_CO.. (común).
    # "Plantación de Arbustos" (forrajeras tipo atriplex) no es plantación forestal
    {"fields": ["subuso"], "contains": ["plantacion de arbustos"], "cat": 3, "weight": 0.8},
    {"fields": ["subuso", "estructura", "uso"], "contains": ["plantac"], "cat": 1},
    {"fields": ["tipo_for", "tipofor", "tipo for"], "contains": ["escler"], "cat": 2, "weight": 0.95},
    {"fields": ["subuso", "estructura"], "contains": ["bosque nativo", "renoval", "bosque achaparrado", "bosque adulto"], "cat": 2},
    # Especies exóticas de plantación (solo nombre científico: Acacia caven y otros nativos quedan fuera),
    # y solo en polígonos de USO "Bosques": una pradera con eucaliptos dispersos sigue siendo pradera.
    {"fields": ["especi", "esp_c"], "contains": ["pinus", "eucalyptus", "populus", "pseudotsuga"], "cat": 1,
     "when": {"fields": ["uso"], "contains": ["bosque"]}},
    {"fields": ["subuso", "estructura"], "contains": ["bosque mixto"], "cat": 2},
    # Lo que no quema va antes que las categorías vegetales para evitar coincidencias parciales
    # ("Áreas Desprovistas de Vegetación" contiene "vega").
    {"fields": ["uso", "subuso"], "contains": ["desprovist", "afloramiento", "playa", "duna", "derrumbe", "cajas de rio", "corrida de lava", "sin vegetacion"], "cat": 8},
    {"fields": ["uso", "subuso"], "contains": ["cuerpos de agua", "nieve", "glaciar", "lago", "rio", "salar", "embalse", "laguna", "mar "], "cat": 9},
    {"fields": ["uso", "subuso"], "contains": ["urban", "industrial", "ciudad", "minero", "minera"], "cat": 7},
    {"fields": ["uso", "subuso"], "contains": ["humedal", "vegas", "nadi", "turbera", "marisma", "terrenos humedos", "bofedal"], "cat": 6},
    # SUBUSO distingue matorral de pradera; USO los agrupa ("Praderas y Matorrales"), por eso va después
    {"fields": ["subuso"], "contains": ["matorral"], "cat": 3},
    {"fields": ["subuso"], "contains": ["pradera", "estepa", "pastizal"], "cat": 4},
    {"fields": ["uso", "subuso"], "contains": ["agric", "rotacion", "frutal", "vina", "cultivo"], "cat": 5},
    {"fields": ["uso"], "contains": ["matorral"], "cat": 3},
    {"fields": ["uso"], "contains": ["pradera", "estepa"], "cat": 4},
    {"fields": ["uso", "subuso"], "contains": ["bosque"], "cat": 2},
]

SUPPORTED_EXT = (".shp", ".gpkg", ".gdb", ".geojson", ".json", ".zip")


def _norm(s: object) -> str:
    t = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii")
    return t.lower().strip()


def classify(props: dict, rules: list[dict] | None = None) -> tuple[int, float]:
    """Categoría y peso de combustible de un polígono del catastro según sus atributos."""
    rules = rules or DEFAULT_RULES
    normed = {_norm(k): _norm(v) for k, v in props.items()}

    def matches(fields: list[str], contains: list[str]) -> bool:
        for fkey in fields:
            for k, v in normed.items():
                if fkey in k and v and any(_norm(sub) in v for sub in contains):
                    return True
        return False

    for rule in rules:
        when = rule.get("when")
        if when and not matches(when["fields"], when["contains"]):
            continue
        if matches(rule["fields"], rule["contains"]):
            cat = int(rule["cat"])
            return cat, float(rule.get("weight", CATEGORIES[cat][1]))
    return 10, CATEGORIES[10][1]


def rules_fingerprint(rules: list[dict] | None = None) -> str:
    """Huella corta de las reglas vigentes: cambiarlas invalida la caché de combustible."""
    import hashlib
    import json

    payload = json.dumps(rules or DEFAULT_RULES, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------- lectura del catastro

@dataclass
class CatastroSource:
    path: Path
    layer: str | None = None

    @property
    def signature(self) -> str:
        p = self.path
        try:
            if p.is_dir():
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                mtime = max((f.stat().st_mtime for f in p.rglob("*") if f.is_file()), default=0)
            else:
                st = p.stat()
                size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0
        return f"{p.name}|{self.layer or ''}|{size}|{int(mtime)}"

    @property
    def gdal_path(self) -> str:
        if self.path.suffix.lower() == ".zip":
            with zipfile.ZipFile(self.path) as zf:
                shp = next((n for n in zf.namelist() if n.lower().endswith(".shp")), None)
            if shp:
                return f"/vsizip/{self.path.as_posix()}/{shp}"
        return str(self.path)


def find_catastros(cfg_fuel: dict, base_dir: Path) -> list[CatastroSource]:
    """Todos los catastros disponibles: rutas explícitas (`conaf_path`, una o
    varias) o todos los archivos soportados en `conaf_dir` (una región por archivo).
    Un zip cuyo Shapefile ya está extraído al lado se omite."""
    layer = cfg_fuel.get("layer")
    explicit = cfg_fuel.get("conaf_path")
    if explicit:
        paths = explicit if isinstance(explicit, list) else [explicit]
        out = []
        for e in paths:
            p = Path(e)
            p = p if p.is_absolute() else base_dir / p
            if p.exists():
                out.append(CatastroSource(p, layer))
        return out
    d = Path(cfg_fuel.get("conaf_dir", "data/catastro"))
    d = d if d.is_absolute() else base_dir / d
    if not d.exists():
        return []
    found: list[CatastroSource] = []
    entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
    stems_with_shp = {p.stem.lower() for p in entries if p.suffix.lower() == ".shp"}
    for p in entries:
        ext = p.suffix.lower()
        if p.is_dir():
            if p.name.lower().endswith(".gdb"):
                found.append(CatastroSource(p, layer))
            continue
        if ext in (".gpkg", ".shp", ".geojson", ".json"):
            found.append(CatastroSource(p, layer))
        elif ext == ".zip" and p.stem.lower() not in stems_with_shp:
            found.append(CatastroSource(p, layer))
    return found


def find_catastro(cfg_fuel: dict, base_dir: Path) -> CatastroSource | None:
    """Primer catastro disponible (compatibilidad)."""
    srcs = find_catastros(cfg_fuel, base_dir)
    return srcs[0] if srcs else None


def catastros_signature(sources: list[CatastroSource], rules: list[dict] | None = None) -> str:
    if not sources:
        return "worldcover"
    return "+".join(s.signature for s in sources) + "|rules=" + rules_fingerprint(rules)


def _pick_layer(gdal_path: str, wanted: str | None) -> str | None:
    import pyogrio

    try:
        layers = pyogrio.list_layers(gdal_path)
    except Exception:  # noqa: BLE001
        return wanted
    if wanted:
        return wanted
    for name, gtype in layers:
        if gtype and "Polygon" in str(gtype):
            return str(name)
    return str(layers[0][0]) if len(layers) else None


def catastro_info(src: CatastroSource) -> dict:
    import pyogrio

    layer = _pick_layer(src.gdal_path, src.layer)
    info = pyogrio.read_info(src.gdal_path, layer=layer)
    return {"path": str(src.path), "layer": layer, "crs": str(info.get("crs")), "features": int(info.get("features", 0)),
            "fields": [str(f) for f in info.get("fields", [])], "geometry_type": str(info.get("geometry_type"))}


def read_catastro(src: CatastroSource, bbox_wgs: tuple[float, float, float, float], rules: list[dict] | None = None,
                  max_features: int | None = None, encoding: str | None = None) -> list[tuple[object, int, float]]:
    """Polígonos del catastro dentro del bbox, en WGS84: (geometría, categoría, peso)."""
    import pyogrio
    import pyogrio.raw
    import shapely
    from pyproj import CRS, Transformer

    layer = _pick_layer(src.gdal_path, src.layer)
    info = pyogrio.read_info(src.gdal_path, layer=layer)
    crs_txt = info.get("crs")
    crs = CRS.from_user_input(crs_txt) if crs_txt else CRS.from_epsg(4326)
    # bbox en el CRS de la capa
    if crs.to_epsg() != 4326:
        to_layer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        xs, ys = zip(*[to_layer.transform(x, y) for x, y in
                       ((bbox_wgs[0], bbox_wgs[1]), (bbox_wgs[2], bbox_wgs[1]), (bbox_wgs[2], bbox_wgs[3]), (bbox_wgs[0], bbox_wgs[3]))])
        bbox_layer = (min(xs), min(ys), max(xs), max(ys))
    else:
        bbox_layer = tuple(bbox_wgs)
    # columnas de atributos relevantes para no cargar todo el dbf
    fields = [str(f) for f in info.get("fields", [])]
    keys = ("uso", "subuso", "estructura", "tipo", "especie", "cobertura", "clase")
    cols = [f for f in fields if any(k in _norm(f) for k in keys)] or fields[:12]
    meta, _fids, geoms_wkb, field_data = pyogrio.raw.read(src.gdal_path, layer=layer, columns=cols, bbox=bbox_layer,
                                                           force_2d=True, max_features=max_features, encoding=encoding)
    names = [str(n) for n in meta["fields"]]
    geoms = shapely.from_wkb(geoms_wkb)
    if crs.to_epsg() != 4326:
        tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        geoms = shapely.transform(geoms, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))
    out = []
    for i, g in enumerate(geoms):
        if g is None or g.is_empty:
            continue
        props = {n: field_data[j][i] for j, n in enumerate(names)}
        cat, w = classify(props, rules)
        out.append((g, cat, w))
    log.info("Catastro %s: %d polígonos en el área (capa %s, CRS %s)", src.path.name, len(out), layer, crs.to_string())
    return out


# ---------------------------------------------------------------- rasterización y fusión

def subgrid_shape(bbox: tuple[float, float, float, float], sub_deg: float) -> tuple[int, int]:
    lon_min, lat_min, lon_max, lat_max = bbox
    return int(round((lat_max - lat_min) / sub_deg)), int(round((lon_max - lon_min) / sub_deg))


def rasterize_catastro(items: list[tuple[object, int, float]], bbox: tuple[float, float, float, float],
                       sub_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Rásteres (fila 0 = norte) de peso (float, NODATA donde no hay polígono) y categoría (uint8, 0 = nada)."""
    from rasterio import features
    from rasterio.transform import from_origin

    h, w = subgrid_shape(bbox, sub_deg)
    tr = from_origin(bbox[0], bbox[3], sub_deg, sub_deg)
    if not items:
        return np.full((h, w), NODATA, dtype=np.float32), np.zeros((h, w), dtype=np.uint8)
    wt = features.rasterize(((g, int(round(wgt * 1000))) for g, _c, wgt in items), out_shape=(h, w), transform=tr,
                            fill=-1, dtype="int32", all_touched=False)
    ct = features.rasterize(((g, c) for g, c, _w in items), out_shape=(h, w), transform=tr, fill=0, dtype="uint8")
    weight = np.where(wt >= 0, wt / 1000.0, NODATA).astype(np.float32)
    return weight, ct


def worldcover_to_weight(sub_classes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Clases WorldCover -> (peso, categoría unificada). 0 (sin dato) -> NODATA."""
    weight = np.full(sub_classes.shape, NODATA, dtype=np.float32)
    cat = np.zeros(sub_classes.shape, dtype=np.uint8)
    for cls, c in WORLDCOVER_CAT.items():
        m = sub_classes == cls
        weight[m] = CATEGORIES[c][1]
        cat[m] = c
    return weight, cat


def merge(primary_w: np.ndarray, primary_c: np.ndarray, fallback_w: np.ndarray, fallback_c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    use = primary_w >= 0
    return np.where(use, primary_w, fallback_w), np.where(use, primary_c, fallback_c)


def aggregate(grid, weight: np.ndarray, cat: np.ndarray, sub_deg: float) -> tuple[list[float], list[dict]]:
    """Peso medio y fracciones de categoría por celda (la subgrilla va de norte a sur)."""
    k = int(round(grid.cell_deg / sub_deg))
    h, w = weight.shape
    fuel: list[float] = []
    fracs: list[dict] = []
    for idx in range(grid.n):
        r, c = divmod(idx, grid.ncols)
        r0, r1 = max(0, h - (r + 1) * k), h - r * k
        bw = weight[r0:r1, c * k:min(w, (c + 1) * k)]
        bc = cat[r0:r1, c * k:min(w, (c + 1) * k)]
        valid = bw >= 0
        if not valid.any():
            fuel.append(0.0)
            fracs.append({})
            continue
        fuel.append(round(float(bw[valid].mean()), 3))
        cats, counts = np.unique(bc[valid], return_counts=True)
        total = counts.sum()
        fr = {CATEGORIES[int(cc)][0]: round(float(n) / total, 3) for cc, n in zip(cats, counts) if cc > 0 and n / total >= 0.02}
        fracs.append(fr)
    return fuel, fracs


@dataclass
class FuelResult:
    fuel: list[float]
    fracs: list[dict]
    source: str
    detail: dict = field(default_factory=dict)


def build_fuel(grid, cfg_fuel: dict, base_dir: Path, sub_deg: float) -> FuelResult:
    """Combustible por celda: catastro CONAF (si hay archivo) con relleno WorldCover."""
    from . import riskgrid

    sources = find_catastros(cfg_fuel or {}, base_dir)
    wc_w = wc_c = None
    if cfg_fuel.get("worldcover", True):
        try:
            wc_classes = riskgrid.read_worldcover_subgrid(grid, sub_deg)
            wc_w, wc_c = worldcover_to_weight(wc_classes)
        except Exception as e:  # noqa: BLE001
            log.warning("WorldCover no disponible: %s", e)
    h, w = subgrid_shape(grid.bbox, sub_deg)
    if wc_w is None:
        wc_w, wc_c = np.full((h, w), NODATA, dtype=np.float32), np.zeros((h, w), dtype=np.uint8)
    if not sources:
        fuel, fracs = aggregate(grid, wc_w, wc_c, sub_deg)
        return FuelResult(fuel, fracs, "ESA WorldCover 2021", {"worldcover": True})
    # Un ráster por catastro; cada uno se superpone al anterior (y todos sobre WorldCover).
    mw, mc = wc_w, wc_c
    used: list[str] = []
    errors: list[str] = []
    counts: dict[str, int] = {}
    n_items = 0
    cov_any = np.zeros(mw.shape, dtype=bool)
    for src in sources:
        try:
            items = read_catastro(src, grid.bbox, cfg_fuel.get("rules"), encoding=cfg_fuel.get("encoding"))
            cw, cc = rasterize_catastro(items, grid.bbox, sub_deg)
        except Exception as e:  # noqa: BLE001
            log.warning("Catastro CONAF %s no se pudo usar: %s", src.path, e)
            errors.append(f"{src.path.name}: {e}")
            continue
        mw, mc = merge(cw, cc, mw, mc)
        cov_any |= cw >= 0
        used.append(src.path.name)
        n_items += len(items)
        for _g, c, _w in items:
            counts[CATEGORIES[c][0]] = counts.get(CATEGORIES[c][0], 0) + 1
    fuel, fracs = aggregate(grid, mw, mc, sub_deg)
    if not used:
        return FuelResult(fuel, fracs, "ESA WorldCover 2021 (catastro no legible)", {"worldcover": True, "errors": errors})
    covered = float(cov_any.mean()) if cov_any.size else 0.0
    names = ", ".join(_short_region(n) for n in used)
    desc = f"Catastro CONAF ({names}; cubre {covered:.1%} del área) + WorldCover"
    return FuelResult(fuel, fracs, desc, {"catastros": used, "coverage": round(covered, 3), "polygons": n_items,
                                           "by_category": counts, "errors": errors,
                                           "signature": catastros_signature(sources, cfg_fuel.get("rules"))})


_REGION_NAMES = {
    "coquimbo": "Coquimbo", "valpara_so": "Valparaíso", "metropolitana": "Metropolitana", "atacama": "Atacama",
    "antofagasta": "Antofagasta", "tarapac_": "Tarapacá", "de_arica_y_parinacota": "Arica y Parinacota",
    "del_libertador_gral__bernardo_ohiggins": "O'Higgins", "del_maule": "Maule", "de__uble": "Ñuble",
    "del_biob_o": "Biobío", "de_la_araucan_a": "La Araucanía", "de_los_r_os": "Los Ríos", "de_los_lagos": "Los Lagos",
    "de_ais_n": "Aysén", "de_magallanes_y_ant_rtica_chilena": "Magallanes",
}


def _short_region(filename: str) -> str:
    """'04__regi_n_de_coquimbo_actualizaci_n_2025.shp' -> 'Coquimbo 2025'.
    Los nombres de SIT-CONAF traen los acentos reemplazados por '_'."""
    import re

    stem = Path(filename).stem
    m = re.search(r"regi_?n_(.+?)_actualizaci_?n_(\d{4})", stem, flags=re.I)
    if not m:
        return stem[:40]
    raw = m.group(1).lower()
    name = _REGION_NAMES.get(raw) or _REGION_NAMES.get(raw.removeprefix("de_")) or _REGION_NAMES.get("de_" + raw)
    if not name:
        name = raw.removeprefix("de_").replace("_", " ").strip().title()
    return f"{name} {m.group(2)}"
