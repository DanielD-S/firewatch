"""Capa de combustible: clasificación del catastro, rasterización y fusión con WorldCover."""
import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
import shapefile

from firewatch import fuel, riskgrid

CFG = {"bbox": [-72.0, -36.0, -71.0, -35.0], "cell_deg": 0.5}


# ---------------- clasificación por atributos ----------------

@pytest.mark.parametrize("props,cat,weight", [
    ({"USO": "Bosques", "SUBUSO": "Plantación", "ESPECIE1": "Pinus radiata"}, 1, 1.0),
    ({"USO_TIERRA": "Bosques", "SUBUSO_1": "Bosque Nativo", "ESTRUCTURA": "Adulto", "TIPO_FOR": "Esclerófilo"}, 2, 0.95),
    ({"USO": "Bosques", "SUBUSO": "Bosque Nativo", "TIPO_FOR": "Roble-Raulí-Coigüe"}, 2, 0.85),
    ({"USO": "Praderas y Matorrales", "SUBUSO": "Matorral Arborescente"}, 3, 0.9),
    ({"USO": "Praderas y Matorrales", "SUBUSO": "Praderas"}, 4, 0.7),
    ({"USO": "Terrenos Agrícolas", "SUBUSO": "Terrenos de Uso Agrícola"}, 5, 0.4),
    ({"USO": "Áreas Urbanas e Industriales", "SUBUSO": "Ciudades-Pueblos-Zonas Industriales"}, 7, 0.05),
    ({"USO": "Áreas Desprovistas de Vegetación", "SUBUSO": "Afloramientos Rocosos"}, 8, 0.0),
    ({"USO": "Cuerpos de Agua", "SUBUSO": "Lagos-Lagunas-Embalses-Tranques"}, 9, 0.0),
    ({"USO": "Nieves y Glaciares", "SUBUSO": "Glaciares"}, 9, 0.0),
    ({"USO": "Humedales", "SUBUSO": "Vegas"}, 6, 0.3),
    ({"USO": "Bosques", "SUBUSO": "Bosque Mixto"}, 2, 0.85),
    ({"CLASE": "sin información"}, 10, 0.3),
    # campos reales del catastro 2025 (Coquimbo)
    ({"USO_TIERRA": "Matorral Abierto", "USO": "Praderas y Matorrales", "SUBUSO": "Matorral", "ESTRUCTURA": "No Aplica",
      "TIPO_FORES": None, "ESPECI1_CI": "Adesmia hystrix"}, 3, 0.9),
    ({"USO": "Áreas Desprovistas de Vegetación", "SUBUSO": "Otros Terrenos sin Vegetación"}, 8, 0.0),
    ({"USO": "Humedales", "SUBUSO": "Vegas"}, 6, 0.3),
    ({"USO": "Bosques", "SUBUSO": "Plantación", "ESTRUCTURA": "Plantación Joven o Recién Cosechada", "ESPECI1_CI": "Pinus radiata"}, 1, 1.0),
    ({"USO": "Bosques", "SUBUSO": "Bosque Nativo", "ESTRUCTURA": "Adulto", "TIPO_FORES": "Esclerófilo", "ESPECI1_CI": "Quillaja saponaria"}, 2, 0.95),
    ({"USO": "Bosques", "SUBUSO": "Bosque Mixto", "ESPECI1_CI": "Eucalyptus globulus"}, 1, 1.0),
    ({"USO": "Praderas y Matorrales", "SUBUSO": "Matorral con Suculentas"}, 3, 0.9),
    ({"USO": "Praderas y Matorrales", "SUBUSO": "Matorral-Pradera"}, 3, 0.9),
    ({"USO": "Cuerpos de Agua", "SUBUSO": "Ríos"}, 9, 0.0),
    # especies nativas con nombre parecido a exóticas no deben volverse plantación
    ({"USO": "Bosques", "SUBUSO": "Bosque Nativo", "ESTRUCTURA": "Adulto", "ESPECI1_CI": "Acacia caven", "ESPECI2_CI": "Prosopis chilensis"}, 2, 0.85),
    ({"USO": "Praderas y Matorrales", "SUBUSO": "Matorral", "ESPECI1_CI": "Acacia caven"}, 3, 0.9),
    ({"USO": "Praderas y Matorrales", "SUBUSO": "Plantación de Arbustos", "ESPECI1_CI": "Atriplex nummularia"}, 3, 0.8),
    ({"USO": "Bosques", "SUBUSO": "Bosque Mixto", "ESPECI1_CI": "Acacia caven"}, 2, 0.85),
    ({"USO": "Terrenos Agrícolas", "SUBUSO": "Rotación Cultivo-Pradera"}, 4, 0.7),
    # especie exótica fuera de USO "Bosques" no convierte en plantación
    ({"USO": "Praderas y Matorrales", "SUBUSO": "Praderas", "ESPECI1_CI": "Eucalyptus globulus"}, 4, 0.7),
    ({"USO": "Bosques", "SUBUSO": "Bosque Mixto", "ESPECI1_CI": "Pinus radiata"}, 1, 1.0),
])
def test_classify_catastro_attributes(props, cat, weight):
    assert fuel.classify(props) == (cat, weight)


def test_short_region_names():
    assert fuel._short_region("04__regi_n_de_coquimbo_actualizaci_n_2025.shp") == "Coquimbo 2025"
    assert fuel._short_region("05__regi_n_de_valpara_so_actualizaci_n_2019_v2.shp") == "Valparaíso 2019"
    assert fuel._short_region("13__regi_n_metropolitana_actualizaci_n_2019.shp") == "Metropolitana 2019"
    assert fuel._short_region("08__regi_n_del_biob_o_actualizaci_n_2021.shp") == "Biobío 2021"
    assert fuel._short_region("regionA.shp") == "regionA"


def test_rules_fingerprint_changes_signature(tmp_path):
    (tmp_path / "a.geojson").write_text("{}")
    srcs = fuel.find_catastros({"conaf_path": str(tmp_path / "a.geojson")}, tmp_path)
    s1 = fuel.catastros_signature(srcs)
    s2 = fuel.catastros_signature(srcs, [{"fields": ["uso"], "contains": ["x"], "cat": 1}])
    assert s1 != s2 and "rules=" in s1
    assert fuel.classify({"USO": "x"}, [{"fields": ["uso"], "contains": ["x"], "cat": 1,
                                         "when": {"fields": ["subuso"], "contains": ["y"]}}]) == (10, 0.3)


def test_classify_respects_custom_rules():
    rules = [{"fields": ["tipo"], "contains": ["eucal"], "cat": 1, "weight": 0.98}]
    assert fuel.classify({"TIPO": "Eucalyptus globulus"}, rules) == (1, 0.98)
    assert fuel.classify({"TIPO": "otra cosa"}, rules) == (10, 0.3)


# ---------------- rasterización, fusión y agregación ----------------

def test_rasterize_and_aggregate_with_worldcover_fallback():
    g = riskgrid.Grid.from_cfg(CFG)
    from shapely.geometry import box

    # plantación cubriendo exactamente la celda 0 (suroeste); agua en la celda 3 (noreste)
    items = [(box(-72.0, -36.0, -71.5, -35.5), 1, 1.0), (box(-71.5, -35.5, -71.0, -35.0), 9, 0.0)]
    cw, cc = fuel.rasterize_catastro(items, g.bbox, riskgrid.SUB_DEG)
    h, w = cw.shape
    assert (h, w) == (200, 200)
    assert cw[-1, 0] == 1.0 and cc[-1, 0] == 1          # fila inferior = sur
    assert cw[0, -1] == 0.0 and cc[0, -1] == 9
    assert cw[0, 0] == fuel.NODATA and cc[0, 0] == 0    # noroeste sin catastro
    # WorldCover de relleno: pastizal en todo el área
    wc = np.full((h, w), 30, dtype=np.uint8)
    ww, wcat = fuel.worldcover_to_weight(wc)
    mw, mc = fuel.merge(cw, cc, ww, wcat)
    fuel_vals, fracs = fuel.aggregate(g, mw, mc, riskgrid.SUB_DEG)
    assert fuel_vals == [1.0, 0.7, 0.7, 0.0]
    assert fracs[0] == {"plantación": 1.0} and fracs[1] == {"pradera": 1.0} and fracs[3] == {"agua/nieve": 1.0}


def test_worldcover_to_weight_categories():
    arr = np.array([[10, 20], [80, 0]], dtype=np.uint8)
    w, c = fuel.worldcover_to_weight(arr)
    assert np.allclose(w, [[1.0, 0.9], [0.0, fuel.NODATA]])
    assert c.tolist() == [[11, 3], [9, 0]]


# ---------------- lectura de archivos reales (pyogrio) ----------------

def make_shapefile_utm(tmp_path: Path, name: str = "catastro", cells: tuple[int, int] = (0, 3)) -> Path:
    """Catastro sintético en UTM 19S con dos polígonos (plantación y matorral)
    sobre dos celdas del bbox de prueba."""
    w = shapefile.Writer(str(tmp_path / name), shapeType=shapefile.POLYGON)
    w.field("USO", "C", 40)
    w.field("SUBUSO", "C", 40)
    w.field("ESPECIE1", "C", 40)
    # celda 0 del grid (-72..-71.5, -36..-35.5) aprox en UTM 19S
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", "EPSG:32719", always_xy=True)

    def ring(lon0, lat0, lon1, lat1):
        pts = [(lon0, lat0), (lon1, lat0), (lon1, lat1), (lon0, lat1), (lon0, lat0)]
        return [list(tr.transform(x, y)) for x, y in pts]

    g = riskgrid.Grid.from_cfg(CFG)
    b0, b1 = g.bounds(cells[0]), g.bounds(cells[1])
    w.poly([ring(*b0)])
    w.record("Bosques", "Plantación", "Pinus radiata")
    w.poly([ring(*b1)])
    w.record("Praderas y Matorrales", "Matorral", "")
    w.close()
    (tmp_path / f"{name}.prj").write_text(
        'PROJCS["WGS 84 / UTM zone 19S",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
        'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",-69],PARAMETER["scale_factor",0.9996],'
        'PARAMETER["false_easting",500000],PARAMETER["false_northing",10000000],UNIT["metre",1]]')
    return tmp_path / f"{name}.shp"


def test_read_catastro_shapefile_reprojects_and_classifies(tmp_path):
    shp = make_shapefile_utm(tmp_path)
    src = fuel.CatastroSource(shp)
    info = fuel.catastro_info(src)
    assert "32719" in info["crs"] and info["features"] == 2
    items = fuel.read_catastro(src, tuple(CFG["bbox"]))
    assert len(items) == 2
    cats = sorted(c for _g, c, _w in items)
    assert cats == [1, 3]
    g0 = next(g for g, c, _w in items if c == 1)
    assert g0.bounds[0] == pytest.approx(-72.0, abs=0.01) and g0.bounds[1] == pytest.approx(-36.0, abs=0.01)
    # fuera del bbox no devuelve nada
    assert fuel.read_catastro(src, (-70.0, -34.0, -69.5, -33.5)) == []


def test_find_catastros_dir_and_explicit(tmp_path):
    d = tmp_path / "catastro"
    d.mkdir()
    assert fuel.find_catastros({"conaf_dir": "catastro"}, tmp_path) == []
    (d / "a.geojson").write_text("{}")
    (d / "b.gpkg").write_bytes(b"")
    (d / "c.shp").write_bytes(b"")
    (d / "c.zip").write_bytes(b"")     # zip ya extraído (c.shp existe): se omite
    (d / "d.zip").write_bytes(b"")
    (d / "LEEME.txt").write_text("x")
    names = [s.path.name for s in fuel.find_catastros({"conaf_dir": "catastro"}, tmp_path)]
    assert names == ["a.geojson", "b.gpkg", "c.shp", "d.zip"]
    assert fuel.find_catastro({"conaf_dir": "catastro"}, tmp_path).path.name == "a.geojson"
    two = fuel.find_catastros({"conaf_path": [str(d / "a.geojson"), str(d / "no.shp"), str(d / "b.gpkg")]}, tmp_path)
    assert [s.path.name for s in two] == ["a.geojson", "b.gpkg"]
    assert fuel.find_catastros({"conaf_path": str(d / "no.shp")}, tmp_path) == []
    assert fuel.catastros_signature([]) == "worldcover"
    assert "+" in fuel.catastros_signature(two)


def test_build_fuel_merges_two_catastros(tmp_path):
    # región A cubre celdas 0 y 3; región B cubre celdas 1 y 2: las cuatro quedan con catastro
    make_shapefile_utm(tmp_path, "regionA", (0, 3))
    make_shapefile_utm(tmp_path, "regionB", (1, 2))
    g = riskgrid.Grid.from_cfg(CFG)
    res = fuel.build_fuel(g, {"conaf_dir": str(tmp_path), "worldcover": False}, tmp_path, riskgrid.SUB_DEG)
    assert res.fuel == pytest.approx([1.0, 1.0, 0.9, 0.9], abs=0.01)
    assert res.detail["catastros"] == ["regionA.shp", "regionB.shp"] and res.detail["polygons"] == 4
    assert res.detail["coverage"] == pytest.approx(1.0, abs=0.01)
    assert "regionA, regionB" in res.source  # nombres sin patrón CONAF se muestran tal cual


def test_build_fuel_with_catastro_and_no_worldcover(tmp_path):
    shp = make_shapefile_utm(tmp_path)
    g = riskgrid.Grid.from_cfg(CFG)
    res = fuel.build_fuel(g, {"conaf_path": str(shp), "worldcover": False}, tmp_path, riskgrid.SUB_DEG)
    assert res.fuel[0] == pytest.approx(1.0, abs=0.01)
    assert res.fuel[3] == pytest.approx(0.9, abs=0.01)
    assert res.fuel[1] == 0.0 and res.fuel[2] == 0.0      # sin catastro ni WorldCover
    assert res.detail["polygons"] == 2 and "Catastro CONAF" in res.source
    assert res.detail["coverage"] == pytest.approx(0.5, abs=0.01)
    assert res.detail["by_category"] == {"plantación": 1, "matorral": 1}


def test_zip_shapefile_gdal_path(tmp_path):
    shp = make_shapefile_utm(tmp_path)
    zpath = tmp_path / "cat.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for ext in ("shp", "shx", "dbf", "prj"):
            zf.write(tmp_path / f"catastro.{ext}", f"catastro.{ext}")
    src = fuel.CatastroSource(zpath)
    assert src.gdal_path.startswith("/vsizip/") and src.gdal_path.endswith("catastro.shp")
    items = fuel.read_catastro(src, tuple(CFG["bbox"]))
    assert len(items) == 2
