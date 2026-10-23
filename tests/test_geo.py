import pytest
from shapely.geometry import LineString

from firewatch import geo


def test_bearing_cardinals():
    assert geo.bearing_deg(-35.0, -71.0, -34.0, -71.0) == pytest.approx(0.0, abs=0.5)      # norte
    assert geo.bearing_deg(-35.0, -71.0, -35.0, -70.0) == pytest.approx(90.0, abs=1.0)     # este
    assert geo.bearing_deg(-35.0, -71.0, -36.0, -71.0) == pytest.approx(180.0, abs=0.5)    # sur


def test_wind_blows_toward():
    # viento del oeste (270) sopla hacia el este (90)
    assert geo.wind_blows_toward(270.0, 90.0, 45.0)
    assert not geo.wind_blows_toward(270.0, 270.0, 45.0)
    assert geo.wind_blows_toward(350.0, 175.0, 45.0)


def test_haversine():
    # ~111 km por grado de latitud
    assert geo.haversine_m(-35.0, -71.0, -36.0, -71.0) == pytest.approx(111_000, rel=0.01)


def test_sample_points_along_line():
    ln = LineString([(0, 0), (100_000, 0)])
    pts = geo.sample_points_along(ln, 20_000)
    assert 5 <= len(pts) <= 7
    assert pts[0].x == pytest.approx(0) and pts[-1].x == pytest.approx(100_000)


def test_metric_roundtrip():
    from shapely.geometry import Point

    p = Point(-71.0, -35.0)
    m = geo.to_metric(p, "EPSG:32719")
    back = geo.to_wgs(m, "EPSG:32719")
    assert back.x == pytest.approx(-71.0, abs=1e-6)
    assert back.y == pytest.approx(-35.0, abs=1e-6)
