"""Valores de referencia del paquete R cffdrs (ejemplo clásico de Van Wagner)."""
import pytest

from firewatch import fwi


def test_fwi_reference_day():
    # Inicio 85/6/15; T 17 °C, HR 42 %, viento 25 km/h, lluvia 0, abril, lat 55 N
    r = fwi.fwi_daily(85.0, 6.0, 15.0, 17.0, 42.0, 25.0, 0.0, lat=55.0, month=4)
    assert r.ffmc == pytest.approx(87.69, abs=0.05)
    assert r.dmc == pytest.approx(8.54, abs=0.05)
    assert r.dc == pytest.approx(19.01, abs=0.05)
    assert r.isi == pytest.approx(10.85, abs=0.05)
    assert r.bui == pytest.approx(8.49, abs=0.05)
    assert r.fwi == pytest.approx(10.10, abs=0.05)


def test_rain_lowers_codes():
    dry = fwi.fwi_daily(90.0, 40.0, 300.0, 30.0, 20.0, 20.0, 0.0, lat=-35.0, month=1)
    wet = fwi.fwi_daily(90.0, 40.0, 300.0, 30.0, 20.0, 20.0, 15.0, lat=-35.0, month=1)
    assert wet.ffmc < dry.ffmc
    assert wet.dmc < dry.dmc
    assert wet.dc < dry.dc
    assert wet.fwi < dry.fwi


def test_southern_summer_dries_more_than_winter():
    jan = fwi.fwi_daily(85.0, 6.0, 15.0, 25.0, 30.0, 15.0, 0.0, lat=-35.0, month=1)
    jul = fwi.fwi_daily(85.0, 6.0, 15.0, 25.0, 30.0, 15.0, 0.0, lat=-35.0, month=7)
    assert jan.dmc > jul.dmc
    assert jan.dc > jul.dc


def test_danger_classes():
    assert fwi.FWIResult(0, 0, 0, 0, 0, 3).danger_class == "muy bajo"
    assert fwi.FWIResult(0, 0, 0, 0, 0, 25).danger_class == "alto"
    assert fwi.FWIResult(0, 0, 0, 0, 0, 60).danger_class == "extremo"
