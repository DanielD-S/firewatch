"""Servidor local: subida de trazado por la API, sin red externa."""
import http.server
import json
import threading
from pathlib import Path

import requests

from firewatch import server as srv
from firewatch.config import Config

ROOT = Path(__file__).resolve().parent.parent


def make_cfg(tmp_path: Path) -> Config:
    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    raw["sectors_file"] = str(ROOT / "data" / "sample_lineas.geojson")
    raw["db_path"] = str(tmp_path / "fw.sqlite")
    raw["web_dir"] = str(tmp_path / "web")   # nunca la carpeta web real: sobrescribiría state.json
    raw["notify"] = {"console": False}
    raw["sources"]["weather"]["enabled"] = False
    raw["risk_grid"]["enabled"] = False
    return Config(raw=raw, base_dir=tmp_path)


def start(cfg: Config):
    state = srv.State(cfg)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(state))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_upload_geojson_switches_sectors(tmp_path):
    cfg = make_cfg(tmp_path)
    httpd, base = start(cfg)
    try:
        st = requests.get(f"{base}/api/status", timeout=10).json()
        assert st["sectors_source"]["uploaded"] is False
        gj = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"codigo": "LT-77", "nombre": "Linea subida"},
             "geometry": {"type": "LineString", "coordinates": [[-71.3, -35.3], [-71.4, -35.4]]}}]}
        r = requests.post(f"{base}/api/sectors/upload", data=json.dumps(gj).encode(),
                          headers={"X-Filename": "mi_linea.geojson"}, timeout=30)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["n_sectors"] == 1 and j["sectors"][0]["id"] == "LT-77" and j["evaluated"]
        assert j["levels"] == {"LT-77": "verde"}
        st = requests.get(f"{base}/api/status", timeout=10).json()
        assert st["sectors_source"]["uploaded"] is True and st["sectors_source"]["name"] == "mi_linea.geojson"
        assert [r_["sector_id"] for r_ in st["sector_levels"]] == ["LT-77"]
        sec = requests.get(f"{base}/api/sectors", timeout=10).json()
        assert sec["features"][0]["properties"]["name"] == "Linea subida"
        state = json.loads((Path(cfg["web_dir"]) / "state.json").read_text(encoding="utf-8"))
        assert state["counts"]["sectors"] == 1 and state["sectors_source"]["uploaded"] is True
        # formato inválido -> 400 con mensaje
        r = requests.post(f"{base}/api/sectors/upload", data=b"hola", headers={"X-Filename": "x.txt"}, timeout=30)
        assert r.status_code == 400 and "Formato" in r.json()["error"]
        # volver al ejemplo
        r = requests.post(f"{base}/api/sectors/reset", timeout=30)
        assert r.status_code == 200
        st = requests.get(f"{base}/api/status", timeout=10).json()
        assert st["sectors_source"]["uploaded"] is False and len(st["sector_levels"]) == 3
    finally:
        httpd.shutdown()
        httpd.server_close()
