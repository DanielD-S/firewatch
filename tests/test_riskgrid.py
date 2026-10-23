"""Mapa de riesgo por zonas, sin red: grilla, combustible y combinación."""
from datetime import datetime, timezone

import numpy as np
import pytest
from shapely.geometry import LineString

from firewatch import geo, riskgrid, scoring
from firewatch.sectors import Sector
from firewatch.store import Store

CFG = {"bbox": [-72.0, -36.0, -71.0, -35.0], "cell_deg": 0.5}


def test_grid_dimensions_and_indexing():
    g = riskgrid.Grid.from_cfg(CFG)
    assert (g.ncols, g.nrows, g.n) == (2, 2, 4)
    assert g.center(0) == pytest.approx((-35.75, -71.75))     # suroeste
    assert g.center(3) == pytest.approx((-35.25, -71.25))     # noreste
    assert g.index_of(-71.3, -35.3) == 3
    assert g.index_of(-70.9, -35.3) is None
    assert g.bounds(1) == pytest.approx((-71.5, -36.0, -71.0, -35.5))


def test_worldcover_tiles_cover_bbox():
    tiles = riskgrid.worldcover_tiles((-73.6, -38.6, -70.0, -32.4))
    names = {t[0] for t in tiles}
    assert {"S39W075", "S39W072", "S36W075", "S36W072", "S33W075", "S33W072"} == names


def test_fuel_from_classes_weights():
    forest = np.full((10, 10), 10, dtype=np.uint8)
    water = np.full((10, 10), 80, dtype=np.uint8)
    half = np.concatenate([np.full((5, 10), 20, dtype=np.uint8), np.full((5, 10), 40, dtype=np.uint8)])
    assert riskgrid.fuel_from_classes(forest)[0] == pytest.approx(1.0)
    assert riskgrid.fuel_from_classes(water)[0] == pytest.approx(0.0)
    f, frac = riskgrid.fuel_from_classes(half)
    assert f == pytest.approx((0.9 + 0.4) / 2)
    assert frac == {"matorral": 0.5, "cultivo": 0.5}
    assert riskgrid.fuel_from_classes(np.zeros((3, 3), dtype=np.uint8))[0] == 0.0


def test_fuel_grid_aggregates_blocks():
    g = riskgrid.Grid.from_cfg(CFG)
    k = int(round(g.cell_deg / riskgrid.SUB_DEG))
    sub = np.zeros((g.nrows * k, g.ncols * k), dtype=np.uint8)
    # la subgrilla tiene orientación de imagen: fila 0 = norte
    sub[-k:, :k] = 30           # suroeste de la imagen -> celda 0: pastizal
    sub[:k, k:] = 10            # noreste de la imagen -> celda 3: bosque
    fuel, fracs = riskgrid.fuel_grid(g, sub)
    assert fuel == [0.7, 0.0, 0.0, 1.0]
    assert fracs[0] == {"pastizal": 1.0} and fracs[3] == {"bosque": 1.0}


def synthetic_weather(temp: float, rh: float, wind: float, precip: float = 0.0, n_hours: int = 72) -> dict:
    # serie horaria local de 3 días (ayer, hoy, mañana) con valores constantes
    times, isd = [], []
    for d in range(3):
        for h in range(24):
            times.append(f"2026-01-{14 + d:02d}T{h:02d}:00")
            isd.append(1 if 7 <= h <= 20 else 0)
    n = len(times)
    return {
        "utc_offset_seconds": -10800,
        "hourly": {
            "time": times, "temperature_2m": [temp] * n, "relative_humidity_2m": [rh] * n,
            "wind_speed_10m": [wind] * n, "wind_direction_10m": [270.0] * n, "wind_gusts_10m": [wind * 1.3] * n,
            "precipitation": [precip] * n, "cloud_cover": [0.0] * n, "is_day": isd,
        },
    }


def test_compute_cells_combines_conditions_and_fuel(tmp_path):
    g = riskgrid.Grid.from_cfg(CFG)
    store = Store(tmp_path / "t.sqlite")
    now = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)   # 15:00 hora local
    hot = synthetic_weather(36.0, 12.0, 35.0)
    cold = synthetic_weather(10.0, 90.0, 5.0, precip=2.0)
    fuel = [1.0, 0.0, 0.6, 1.0]
    fracs = [{"bosque": 1.0}, {"agua": 1.0}, {"cultivo": 1.0}, {"bosque": 1.0}]
    cells = riskgrid.compute_cells(g, fuel, fracs, [hot, hot, hot, cold], store, now)
    assert cells[0]["risk_now"] > 0.7 and cells[0]["class"] in ("muy alto", "extremo")
    assert cells[0]["boton_rojo"] and cells[0]["r303030"]
    assert cells[1]["risk_now"] == 0.0 and cells[1]["cond_now"] > 0.5   # agua: condiciones altas, riesgo nulo
    assert cells[2]["risk_now"] == pytest.approx(cells[0]["risk_now"] * 0.6, abs=0.01)
    assert cells[3]["risk_now"] < 0.1 and cells[3]["class"] == "bajo"
    res = riskgrid.RiskGridResult(g, now, cells)
    assert [c["i"] for c in res.top(2)] == [0, 2]
    s = res.summary()
    assert s["n_valid"] == 4 and s["n_boton_rojo"] == 3
    d = res.as_dict()
    back = riskgrid.RiskGridResult.from_dict(d)
    assert back.grid.key() == g.key() and len(back.cells) == 4
    store.close()


def test_missing_weather_gives_no_data_cell():
    g = riskgrid.Grid.from_cfg(CFG)
    cells = riskgrid.compute_cells(g, [1.0] * 4, [{}] * 4, [None] * 4, None, datetime.now(timezone.utc))
    assert all(c["class"] == "sin datos" and c["risk_now"] is None for c in cells)


def test_zone_risk_raises_sector_to_yellow():
    gm = geo.to_metric(LineString([(-71.0, -35.0), (-71.0, -35.1)]), "EPSG:32719")
    s = Sector("L1", "L", {}, LineString([(-71.0, -35.0), (-71.0, -35.1)]), gm, gm.buffer(500), gm.buffer(5000), [], "EPSG:32719")
    now = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    sc = {"levels": {}}
    zone = {"i": 0, "lat": -35.05, "lon": -71.0, "fuel": 0.9, "cover": {"matorral": 1.0}, "cond_now": 0.8, "cond_next": 0.8,
            "risk_now": 0.72, "risk_next": 0.72, "class": "muy alto", "class_next": "muy alto", "temp": 34, "rh": 15,
            "wind": 30, "pi": 85, "fwi": 30, "boton_rojo": True, "r303030": True}
    a = scoring.assess_sector(s, [], [], [], sc, [], now, zone=zone, zone_cfg={"zone_amarillo": 0.55})
    assert a.level == "amarillo"
    assert any("Riesgo de zona" in r for r in a.reasons)
    assert a.summary()["zone"]["risk_now"] == 0.72
    low = {**zone, "risk_now": 0.2, "risk_next": 0.2, "class": "bajo"}
    b = scoring.assess_sector(s, [], [], [], sc, [], now, zone=low, zone_cfg={"zone_amarillo": 0.55})
    assert b.level == "verde"
