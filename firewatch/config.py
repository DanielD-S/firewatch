"""Carga de configuración (JSON) con rutas resueltas respecto al archivo."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Config:
    raw: dict[str, Any]
    base_dir: Path

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def path(self, key: str) -> Path:
        """Ruta de un campo del config, relativa a la carpeta del config."""
        p = Path(self.raw[key])
        return p if p.is_absolute() else self.base_dir / p

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        lon_min, lat_min, lon_max, lat_max = self.raw["bbox"]
        return float(lon_min), float(lat_min), float(lon_max), float(lat_max)

    @property
    def crs_metric(self) -> str:
        return self.raw.get("crs_metric", "EPSG:32719")

    @property
    def timezone(self) -> str:
        return self.raw.get("timezone", "America/Santiago")


def load_config(path: str | os.PathLike | None = None) -> Config:
    if path is None:
        env = os.environ.get("FIREWATCH_CONFIG")
        path = env if env else Path(__file__).resolve().parent.parent / "config.json"
    p = Path(path).resolve()
    with open(p, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return Config(raw=raw, base_dir=p.parent)
