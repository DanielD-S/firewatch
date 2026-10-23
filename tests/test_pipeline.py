"""Tubería completa sin red: foco inyectado junto a un sector de ejemplo."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from firewatch import pipeline
from firewatch.config import Config
from firewatch.notify import Notifier
from firewatch.store import Hotspot, Store

ROOT = Path(__file__).resolve().parent.parent


class CaptureNotifier(Notifier):
    name = "capture"

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)
        return True


def make_cfg(tmp_path: Path) -> Config:
    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    raw["sectors_file"] = str(ROOT / "data" / "sample_lineas.geojson")
    raw["db_path"] = str(tmp_path / "fw.sqlite")
    raw["web_dir"] = str(tmp_path / "web")
    raw["notify"] = {"console": False}
    raw["risk_grid"]["enabled"] = False
    return Config(raw=raw, base_dir=tmp_path)


def test_injected_hotspot_triggers_alert_and_export(tmp_path):
    cfg = make_cfg(tmp_path)
    now = datetime(2026, 1, 20, 19, 0, tzinfo=timezone.utc)
    store = Store(cfg.path("db_path"))
    # foco a ~150 m del trazado de L-DEMO-02 (segmento -72.85,-36.90 -> -72.65,-36.98)
    store.add_hotspots([Hotspot("inj1", "firms", "VIIRS NOAA-20", -36.9385, -72.75, now - timedelta(minutes=45), 0.9, 25.0, "high", "D")])
    cap = CaptureNotifier()
    r = pipeline.run_once(cfg, store, [cap], now=now, do_ingest=False, fetch_weather=False, do_notify=True)
    levels = {a.sector.id: a.level for a in r.assessments}
    assert levels["L-DEMO-02"] == "rojo"
    assert levels["L-DEMO-01"] == "verde"
    assert len(cap.sent) == 1 and cap.sent[0].sector_id == "L-DEMO-02"
    state = json.loads((tmp_path / "web" / "state.json").read_text(encoding="utf-8"))
    assert state["counts"]["hotspots_24h"] == 1
    sec = {f["properties"]["id"]: f["properties"] for f in state["sectors"]["features"]}
    assert sec["L-DEMO-02"]["level"] == "rojo" and sec["L-DEMO-02"]["n_hits_near"] == 1
    assert len(state["alerts"]) == 1
    # segunda corrida inmediata: en enfriamiento, no vuelve a notificar
    r2 = pipeline.run_once(cfg, store, [cap], now=now + timedelta(minutes=5), do_ingest=False, fetch_weather=False)
    assert len(cap.sent) == 1 and r2.assessments[1].level == "rojo"
    store.close()
