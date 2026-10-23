"""Persistencia en SQLite: focos, rayos, meteorología, estado FWI y alertas."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS hotspots (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  satellite TEXT NOT NULL,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  acq_time TEXT NOT NULL,
  confidence REAL NOT NULL,
  frp REAL,
  raw_conf TEXT,
  daynight TEXT,
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_hotspots_time ON hotspots(acq_time);

CREATE TABLE IF NOT EXISTS lightning (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  flash_time TEXT NOT NULL,
  energy REAL,
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lightning_time ON lightning(flash_time);

CREATE TABLE IF NOT EXISTS ingested_files (
  key TEXT PRIMARY KEY,
  ingested_at TEXT NOT NULL,
  n_records INTEGER
);

CREATE TABLE IF NOT EXISTS fwi_state (
  point_key TEXT PRIMARY KEY,
  date_local TEXT NOT NULL,
  ffmc REAL NOT NULL,
  dmc REAL NOT NULL,
  dc REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sector_state (
  sector_id TEXT PRIMARY KEY,
  updated_at TEXT NOT NULL,
  level TEXT NOT NULL,
  payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  sector_id TEXT NOT NULL,
  level TEXT NOT NULL,
  title TEXT NOT NULL,
  message TEXT NOT NULL,
  evidence TEXT NOT NULL,
  notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alerts_sector ON alerts(sector_id, created_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass
class Hotspot:
    id: str
    source: str
    satellite: str
    lat: float
    lon: float
    acq_time: datetime
    confidence: float
    frp: float | None = None
    raw_conf: str | None = None
    daynight: str | None = None

    def as_row(self) -> tuple:
        return (
            self.id, self.source, self.satellite, self.lat, self.lon, iso(self.acq_time),
            self.confidence, self.frp, self.raw_conf, self.daynight, iso(utcnow()),
        )


@dataclass
class Flash:
    id: str
    source: str
    lat: float
    lon: float
    flash_time: datetime
    energy: float | None = None

    def as_row(self) -> tuple:
        return (self.id, self.source, self.lat, self.lon, iso(self.flash_time), self.energy, iso(utcnow()))


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- ingestión ----
    def add_hotspots(self, items: Iterable[Hotspot]) -> int:
        rows = [h.as_row() for h in items]
        if not rows:
            return 0
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO hotspots VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        self.conn.commit()
        return cur.rowcount if cur.rowcount >= 0 else 0

    def add_flashes(self, items: Iterable[Flash]) -> int:
        rows = [f.as_row() for f in items]
        if not rows:
            return 0
        cur = self.conn.executemany("INSERT OR IGNORE INTO lightning VALUES (?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return cur.rowcount if cur.rowcount >= 0 else 0

    def file_ingested(self, key: str) -> bool:
        r = self.conn.execute("SELECT 1 FROM ingested_files WHERE key=?", (key,)).fetchone()
        return r is not None

    def mark_file(self, key: str, n: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ingested_files VALUES (?,?,?)", (key, iso(utcnow()), n)
        )
        self.conn.commit()

    # ---- consultas ----
    def hotspots_since(self, since: datetime) -> list[Hotspot]:
        rows = self.conn.execute(
            "SELECT * FROM hotspots WHERE acq_time >= ? ORDER BY acq_time", (iso(since),)
        ).fetchall()
        return [
            Hotspot(
                id=r["id"], source=r["source"], satellite=r["satellite"], lat=r["lat"], lon=r["lon"],
                acq_time=parse_iso(r["acq_time"]), confidence=r["confidence"], frp=r["frp"],
                raw_conf=r["raw_conf"], daynight=r["daynight"],
            )
            for r in rows
        ]

    def flashes_since(self, since: datetime) -> list[Flash]:
        rows = self.conn.execute(
            "SELECT * FROM lightning WHERE flash_time >= ? ORDER BY flash_time", (iso(since),)
        ).fetchall()
        return [
            Flash(id=r["id"], source=r["source"], lat=r["lat"], lon=r["lon"],
                  flash_time=parse_iso(r["flash_time"]), energy=r["energy"])
            for r in rows
        ]

    # ---- FWI ----
    def get_fwi_state(self, point_key: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM fwi_state WHERE point_key=?", (point_key,)).fetchone()
        return dict(r) if r else None

    def set_fwi_state(self, point_key: str, date_local: str, ffmc: float, dmc: float, dc: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO fwi_state VALUES (?,?,?,?,?)", (point_key, date_local, ffmc, dmc, dc)
        )
        self.conn.commit()

    # ---- ajustes ----
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
        self.conn.commit()

    def del_setting(self, key: str) -> None:
        self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        self.conn.commit()

    # ---- estado y alertas ----
    def set_sector_state(self, sector_id: str, level: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sector_state VALUES (?,?,?,?)",
            (sector_id, iso(utcnow()), level, json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.commit()

    def keep_only_sectors(self, sector_ids: list[str]) -> None:
        """Elimina el estado de sectores que ya no existen (tras cambiar el trazado)."""
        if not sector_ids:
            self.conn.execute("DELETE FROM sector_state")
        else:
            q = ",".join("?" * len(sector_ids))
            self.conn.execute(f"DELETE FROM sector_state WHERE sector_id NOT IN ({q})", sector_ids)
        self.conn.commit()

    def last_alert(self, sector_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM alerts WHERE sector_id=? ORDER BY id DESC LIMIT 1", (sector_id,)
        ).fetchone()

    def last_alert_level_since(self, sector_id: str, level: str, since: datetime) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM alerts WHERE sector_id=? AND level=? AND created_at>=? ORDER BY id DESC LIMIT 1",
            (sector_id, level, iso(since)),
        ).fetchone()

    def add_alert(self, sector_id: str, level: str, title: str, message: str, evidence: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO alerts (created_at, sector_id, level, title, message, evidence) VALUES (?,?,?,?,?,?)",
            (iso(utcnow()), sector_id, level, title, message, json.dumps(evidence, ensure_ascii=False)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_notified(self, alert_id: int) -> None:
        self.conn.execute("UPDATE alerts SET notified=1 WHERE id=?", (alert_id,))
        self.conn.commit()

    def recent_alerts(self, hours: int = 72) -> list[dict]:
        since = iso(utcnow() - timedelta(hours=hours))
        rows = self.conn.execute(
            "SELECT * FROM alerts WHERE created_at>=? ORDER BY id DESC", (since,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = json.loads(d["evidence"])
            out.append(d)
        return out

    def close(self) -> None:
        self.conn.close()
