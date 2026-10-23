import io
import json
import zipfile

import pytest
import shapefile
from shapely.geometry import shape

from firewatch import importers

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document><name>Lineas</name>
<Folder><name>Tramos</name>
<Placemark><name>Tramo A</name>
  <ExtendedData><Data name="tension_kv"><value>220</value></Data></ExtendedData>
  <LineString><coordinates>-71.0,-35.0,0 -71.0,-35.1,0 -71.02,-35.2,0</coordinates></LineString>
</Placemark>
<Placemark><name>Subestacion</name>
  <Polygon><outerBoundaryIs><LinearRing><coordinates>
    -71.1,-35.0 -71.09,-35.0 -71.09,-35.01 -71.1,-35.01 -71.1,-35.0
  </coordinates></LinearRing></outerBoundaryIs></Polygon>
</Placemark>
<Placemark><name>Multi</name>
  <MultiGeometry>
    <LineString><coordinates>-72.0,-36.0 -72.0,-36.1</coordinates></LineString>
    <LineString><coordinates>-72.1,-36.0 -72.1,-36.1</coordinates></LineString>
  </MultiGeometry>
</Placemark>
<Placemark><name>Sin geometria</name></Placemark>
</Folder></Document></kml>"""


def test_kml_placemarks():
    r = importers.import_bytes("lineas.kml", KML.encode("utf-8"))
    assert r.source_format == "kml" and r.n_features == 3
    f = {x["properties"]["name"]: x for x in r.collection["features"]}
    assert f["Tramo A"]["geometry"]["type"] == "LineString"
    assert f["Tramo A"]["properties"]["tension_kv"] == "220"
    assert f["Tramo A"]["geometry"]["coordinates"][0] == [-71.0, -35.0]  # sin altitud
    assert f["Subestacion"]["geometry"]["type"] == "Polygon"
    assert f["Multi"]["geometry"]["type"] == "MultiLineString"
    ids = [x["properties"]["id"] for x in r.collection["features"]]
    assert len(set(ids)) == 3


def test_kmz_wraps_kml():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("doc.kml", KML)
        zf.writestr("images/icon.png", b"\x89PNG")
    r = importers.import_bytes("lineas.kmz", buf.getvalue())
    assert r.source_format == "kmz" and r.n_features == 3


def test_shapefile_zip_utm_reprojected(tmp_path):
    # línea en UTM 19S (EPSG:32719) con atributos
    w = shapefile.Writer(str(tmp_path / "lineas"), shapeType=shapefile.POLYLINE)
    w.field("COD_TRAMO", "C", 20)
    w.field("NOMBRE", "C", 50)
    w.field("KV", "N", 5, 0)
    w.line([[(317000, 6125000), (318000, 6115000), (319000, 6105000)]])
    w.record("T-01", "Tramo uno", 220)
    w.line([[(330000, 6125000), (331000, 6115000)]])
    w.record("T-02", "Tramo dos", 110)
    w.close()
    prj = ('PROJCS["WGS 84 / UTM zone 19S",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
           'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
           'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",-69],PARAMETER["scale_factor",0.9996],'
           'PARAMETER["false_easting",500000],PARAMETER["false_northing",10000000],UNIT["metre",1]]')
    (tmp_path / "lineas.prj").write_text(prj)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for ext in ("shp", "shx", "dbf", "prj"):
            zf.write(tmp_path / f"lineas.{ext}", f"lineas.{ext}")
    r = importers.import_bytes("lineas.zip", buf.getvalue())
    assert r.source_format == "shapefile" and r.n_features == 2 and not r.warnings
    f = r.collection["features"][0]
    assert f["properties"]["id"] == "T-01" and f["properties"]["name"] == "Tramo uno" and f["properties"]["KV"] == 220
    g = shape(f["geometry"])
    assert -72.5 < g.bounds[0] < -70.5 and -35.5 < g.bounds[1] < -34.5  # llegó a lon/lat en Chile


def test_shapefile_zip_without_prj_warns(tmp_path):
    w = shapefile.Writer(str(tmp_path / "l"), shapeType=shapefile.POLYLINE)
    w.field("ID", "C", 10)
    w.line([[(-71.0, -35.0), (-71.1, -35.1)]])
    w.record("X1")
    w.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for ext in ("shp", "shx", "dbf"):
            zf.write(tmp_path / f"l.{ext}", f"l.{ext}")
    r = importers.import_bytes("l.zip", buf.getvalue())
    assert r.n_features == 1 and any("prj" in w for w in r.warnings)


def test_geojson_passthrough_and_ids():
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-71, -35, 100], [-71.1, -35.1, 120]]},
         "properties": {"nombre": "L1"}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-71, -35], [-71.1, -35.1]]},
         "properties": {"nombre": "L1"}},
        {"type": "Feature", "geometry": None, "properties": {"nombre": "vacio"}},
    ]}
    r = importers.import_bytes("x.geojson", json.dumps(gj).encode())
    assert r.n_features == 2 and r.n_skipped == 1
    ids = [f["properties"]["id"] for f in r.collection["features"]]
    assert len(set(ids)) == 2
    assert r.collection["features"][0]["geometry"]["coordinates"][0] == [-71.0, -35.0]


def test_geojson_projected_coords_rejected():
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[317000, 6125000], [318000, 6115000]]}, "properties": {}}]}
    with pytest.raises(importers.ImportError_):
        importers.import_bytes("utm.geojson", json.dumps(gj).encode())


def test_bad_extension_and_empty():
    with pytest.raises(importers.ImportError_):
        importers.import_bytes("x.gpkg", b"abc")
    with pytest.raises(importers.ImportError_):
        importers.import_bytes("x.shp", b"abc")
    with pytest.raises(importers.ImportError_):
        importers.import_bytes("x.kml", b"")
