from datetime import datetime, timezone

import numpy as np
import pytest

from firewatch.sources import common, firms, goes_fdc

CHILE = (-76.0, -56.5, -66.0, -17.0)

CSV = """latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight
-34.9000,-71.2000,340.1,0.39,0.36,2026-10-21,1830,N,high,2.0NRT,300.0,12.5,D
-30.2508,-27.4551,328.2,0.39,0.36,2026-10-21,0304,1,nominal,2.0NRT,291.7,2.1,N
-36.9000,-72.6000,310.0,0.5,0.4,2026-10-21,0500,2,low,2.0NRT,290.0,1.0,N
"""

MODIS_CSV = """latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,confidence,version,bright_t31,frp,daynight
-35.0,-71.5,304.3,1.5,1.21,2026-10-21,0005,T,85,6.1NRT,282.8,15.61,N
"""


def test_firms_parse_filters_bbox_and_normalizes():
    items = firms.parse_csv(CSV, "VIIRS", CHILE)
    assert len(items) == 2  # el punto en el Atlántico queda fuera
    a = items[0]
    assert a.satellite == "VIIRS SNPP" and a.confidence == 0.9 and a.frp == 12.5
    assert a.acq_time == datetime(2026, 10, 21, 18, 30, tzinfo=timezone.utc)
    assert items[1].satellite == "VIIRS NOAA-21" and items[1].confidence == 0.3


def test_firms_modis_confidence_percent():
    items = firms.parse_csv(MODIS_CSV, "MODIS", CHILE)
    assert items[0].satellite == "MODIS Terra"
    assert items[0].confidence == pytest.approx(0.85)


def test_goes_filename_time():
    t = common.goes_start_time("ABI-L2-FDCF/2026/295/21/OR_ABI-L2-FDCF-M6_G19_s20262952100212_e20262952109521_c20262952109587.nc")
    assert t == datetime(2026, 10, 22, 21, 0, 21, 200000, tzinfo=timezone.utc)


def test_fixed_grid_nadir_maps_to_subsatellite_point():
    proj = {"semi_major_axis": 6378137.0, "semi_minor_axis": 6356752.31414, "perspective_point_height": 35786023.0,
            "longitude_of_projection_origin": -75.2}
    lat, lon = goes_fdc.fixed_grid_to_latlon(np.array([0.0]), np.array([0.0]), proj)
    assert lat[0] == pytest.approx(0.0, abs=1e-6)
    assert lon[0] == pytest.approx(-75.2, abs=1e-6)
    # un punto al sur del nadir tiene latitud negativa
    lat2, _ = goes_fdc.fixed_grid_to_latlon(np.array([0.0]), np.array([-0.08]), proj)
    assert -35 < lat2[0] < -20


def test_hour_prefixes_cross_day():
    start = datetime(2026, 10, 22, 23, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 23, 0, 10, tzinfo=timezone.utc)
    pre = list(common.hour_prefixes("GLM-L2-LCFA", start, end))
    assert pre == ["GLM-L2-LCFA/2026/295/23/", "GLM-L2-LCFA/2026/296/00/"]
