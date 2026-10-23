"""Servidor local: visor estático + API de subida de trazados + corridas periódicas.

  GET  /                      visor
  GET  /state.json            estado exportado
  GET  /api/status            sectores activos, origen del trazado, última corrida
  GET  /api/sectors           GeoJSON del trazado activo
  POST /api/sectors/upload    cuerpo = archivo; cabecera X-Filename = nombre (KMZ, KML, zip, GeoJSON)
  POST /api/sectors/reset     vuelve al trazado de ejemplo
  POST /api/run               lanza una corrida completa en segundo plano
"""
from __future__ import annotations

import functools
import http.server
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import importers, notify, pipeline
from .config import Config
from .store import Store, iso, utcnow

log = logging.getLogger(__name__)
MAX_UPLOAD = 60 * 1024 * 1024


class State:
    """Estado compartido entre hilos: candado de corrida y última corrida."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.running = False
        self.last_run: dict | None = None
        self.notifiers = notify.build_notifiers(cfg.get("notify", {}))

    def run(self, do_ingest: bool = True, fetch_weather: bool = True) -> pipeline.RunResult | None:
        if not self.lock.acquire(blocking=False):
            return None
        self.running = True
        try:
            r = pipeline.run_once(self.cfg, None, self.notifiers, do_ingest=do_ingest, fetch_weather=fetch_weather)
            self.last_run = {
                "at": iso(r.started_at), "elapsed_s": round(r.elapsed_s), "hotspots_new": r.n_hotspots_new,
                "flashes_new": r.n_flashes_new, "alerts": len(r.alerts),
                "sectors": [{"id": a.sector.id, "name": a.sector.name, "level": a.level} for a in r.assessments],
            }
            return r
        finally:
            self.running = False
            self.lock.release()

    def run_in_background(self) -> bool:
        if self.running:
            return False
        threading.Thread(target=self._safe_run, daemon=True).start()
        return True

    def run_when_free(self, do_ingest: bool = False, fetch_weather: bool = True) -> None:
        """Espera a que termine la corrida en curso y evalúa de nuevo (tras una subida)."""

        def _go():
            with self.lock:
                pass  # espera el turno
            try:
                self.run(do_ingest=do_ingest, fetch_weather=fetch_weather)
            except Exception as e:  # noqa: BLE001
                log.exception("Reevaluación tras subida falló: %s", e)

        threading.Thread(target=_go, daemon=True).start()

    def _safe_run(self) -> None:
        try:
            self.run()
        except Exception as e:  # noqa: BLE001
            log.exception("Corrida en segundo plano falló: %s", e)

    def watcher(self, interval_s: int) -> None:
        while True:
            self._safe_run()
            time.sleep(interval_s)


class Handler(http.server.SimpleHTTPRequestHandler):
    state: State  # se fija en make_handler

    def log_message(self, fmt, *args):  # menos ruido
        log.debug("%s " + fmt, self.address_string(), *args)

    def end_headers(self) -> None:
        # el visor y state.json cambian en cada corrida: nunca cachear
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # ---- helpers ----
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        if n > MAX_UPLOAD:
            raise ValueError(f"Archivo demasiado grande (máximo {MAX_UPLOAD // (1024 * 1024)} MB)")
        return self.rfile.read(n)

    # ---- GET ----
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            return self._status()
        if path == "/api/sectors":
            return self._sectors()
        if path == "/state.json":
            # sin caché: el visor lo pide cada minuto
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            p = self.state.cfg.path("web_dir") / "state.json"
            if not p.exists():
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            data = p.read_bytes()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return super().do_GET()

    def _status(self) -> None:
        cfg = self.state.cfg
        store = Store(cfg.path("db_path"))
        try:
            src = pipeline.active_sectors_info(cfg, store)
            rows = store.conn.execute("SELECT sector_id, level, updated_at FROM sector_state ORDER BY sector_id").fetchall()
        finally:
            store.close()
        self._json(200, {
            "sectors_source": src, "running": self.state.running, "last_run": self.state.last_run,
            "sector_levels": [dict(r) for r in rows], "server_time": iso(utcnow()),
        })

    def _sectors(self) -> None:
        cfg = self.state.cfg
        store = Store(cfg.path("db_path"))
        try:
            gj = pipeline.active_sectors_collection(cfg, store)
        finally:
            store.close()
        self._json(200, gj)

    # ---- POST ----
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/sectors/upload":
                return self._upload()
            if path == "/api/sectors/reset":
                return self._reset()
            if path == "/api/run":
                started = self.state.run_in_background()
                return self._json(202 if started else 409, {"started": started, "running": self.state.running})
            return self._json(404, {"error": "ruta no encontrada"})
        except importers.ImportError_ as e:
            return self._json(400, {"error": str(e)})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log.exception("POST %s falló", path)
            return self._json(500, {"error": f"error interno: {e}"})

    def _upload(self) -> None:
        filename = unquote(self.headers.get("X-Filename") or "archivo")
        data = self._read_body()
        res = importers.import_bytes(filename, data)
        cfg = self.state.cfg
        text = json.dumps(res.collection, ensure_ascii=False)
        # La base de datos es la fuente de verdad; la copia en disco es solo para el usuario.
        safe = re.sub(r"[^\w\-.]+", "_", Path(filename).stem)[:48] or "trazado"
        out = cfg.path("db_path").parent / f"trazado_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{safe}.geojson"
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(text)
            saved_path = str(out)
        except OSError as e:
            log.warning("No se pudo guardar copia del trazado en %s: %s", out, e)
            saved_path = ""
        store = Store(cfg.path("db_path"))
        try:
            store.set_setting("sectors_geojson", text)
            store.set_setting("sectors_file", saved_path)
            store.set_setting("sectors_name", filename)
            store.set_setting("sectors_uploaded_at", iso(utcnow()))
            store.set_setting("sectors_format", res.source_format)
        finally:
            store.close()
        log.info("Trazado cargado: %s (%d sectores, %s)", filename, res.n_features, res.source_format)
        # evaluación inmediata sin re-ingestar (usa focos y rayos ya guardados)
        wx = bool(cfg["sources"].get("weather", {}).get("enabled", True))
        r = self.state.run(do_ingest=False, fetch_weather=wx)
        payload = {
            "ok": True, "filename": filename, "format": res.source_format, "crs_in": res.crs_in,
            "n_sectors": res.n_features, "n_skipped": res.n_skipped, "warnings": res.warnings,
            "sectors": [{"id": f["properties"]["id"], "name": f["properties"]["name"]} for f in res.collection["features"]],
            "evaluated": r is not None,
        }
        if r is not None:
            payload["levels"] = {a.sector.id: a.level for a in r.assessments}
            payload["alerts"] = len(r.alerts)
        else:
            self.state.run_when_free(do_ingest=False, fetch_weather=wx)
            payload["note"] = "Hay una corrida en curso; los nuevos sectores se evaluarán apenas termine"
        self._json(200, payload)

    def _reset(self) -> None:
        cfg = self.state.cfg
        store = Store(cfg.path("db_path"))
        try:
            for k in ("sectors_geojson", "sectors_file", "sectors_name", "sectors_uploaded_at", "sectors_format"):
                store.del_setting(k)
        finally:
            store.close()
        wx = bool(cfg["sources"].get("weather", {}).get("enabled", True))
        r = self.state.run(do_ingest=False, fetch_weather=wx)
        self._json(200, {"ok": True, "evaluated": r is not None})


def make_handler(state: State):
    web = str(state.cfg.path("web_dir"))

    class _H(Handler):
        pass

    _H.state = state
    return functools.partial(_H, directory=web)


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765, interval_s: int = 600) -> None:
    state = State(cfg)
    httpd = http.server.ThreadingHTTPServer((host, port), make_handler(state))
    httpd.daemon_threads = True
    if interval_s > 0:
        threading.Thread(target=state.watcher, args=(interval_s,), daemon=True).start()
        log.info("Corridas automáticas cada %d s. Canales: %s", interval_s, ", ".join(n.name for n in state.notifiers) or "ninguno")
    print(f"Visor en http://{host}:{port}/  (Ctrl+C para salir)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
