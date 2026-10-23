"""Interfaz de línea de comandos.

  python -m firewatch run            una corrida completa
  python -m firewatch watch -i 600   corridas periódicas
  python -m firewatch status         estado por sector desde la base
  python -m firewatch serve -p 8765  sirve el visor web, acepta subida de trazados y corre cada 10 min
  python -m firewatch test-alert     envía una alerta de prueba por los canales
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import load_config


def _setup_logging(verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_run(args) -> int:
    from .pipeline import run_once

    cfg = load_config(args.config)
    r = run_once(cfg, do_ingest=not args.no_ingest, fetch_weather=not args.no_weather, do_notify=not args.no_notify)
    print(f"\nCorrida de {r.started_at:%Y-%m-%d %H:%M}Z en {r.elapsed_s:.0f} s · "
          f"{r.n_hotspots_new} focos nuevos · {r.n_flashes_new} rayos nuevos · {len(r.alerts)} alertas")
    _print_table(r.assessments)
    print(f"Estado exportado a {r.state_path}")
    return 0


def _print_table(assessments) -> None:
    print()
    print(f"{'Sector':<42} {'Nivel':<9} {'Det':>5} {'Cond':>5} {'Próx':>5} {'Rayos':>5}  Motivo")
    for a in assessments:
        print(f"{a.sector.name[:42]:<42} {a.level:<9} {a.detection_score:>5.2f} {a.conditions_now:>5.2f} "
              f"{a.conditions_next:>5.2f} {a.lightning_score:>5.2f}  {a.reasons[0]}")
    print()


def cmd_watch(args) -> int:
    from .pipeline import watch

    cfg = load_config(args.config)
    watch(cfg, args.interval)
    return 0


def cmd_status(args) -> int:
    from .store import Store

    cfg = load_config(args.config)
    store = Store(cfg.path("db_path"))
    rows = store.conn.execute("SELECT * FROM sector_state ORDER BY sector_id").fetchall()
    if not rows:
        print("Sin estado todavía. Ejecuta: python -m firewatch run")
        return 0
    for r in rows:
        p = json.loads(r["payload"])
        print(f"[{r['level'].upper():<8}] {p['name']}  (actualizado {r['updated_at']})")
        for reason in p.get("reasons", []):
            print(f"    • {reason}")
    al = store.recent_alerts(24)
    print(f"\nAlertas últimas 24 h: {len(al)}")
    for a in al[:10]:
        print(f"  {a['created_at']}  {a['level']:<8} {a['title']}")
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    cfg = load_config(args.config)
    serve(cfg, host=args.host, port=args.port, interval_s=args.interval)
    return 0


def _grid_cfg_with_sectors(cfg, store) -> dict:
    """Config de grilla extendida a los sectores activos, igual que en la tubería."""
    from .pipeline import active_sectors_collection, grid_cfg_for_sectors
    from .sectors import load_sectors

    rg_cfg = cfg.get("risk_grid", {})
    try:
        sectors = load_sectors(cfg, data=active_sectors_collection(cfg, store))
    except Exception:  # noqa: BLE001
        sectors = []
    return grid_cfg_for_sectors(rg_cfg, sectors, int(rg_cfg.get("max_cells", 2500)))


def cmd_grid(args) -> int:
    """Calcula el mapa de riesgo por zonas y muestra las celdas más críticas."""
    from . import riskgrid
    from .store import Store

    cfg = load_config(args.config)
    store = Store(cfg.path("db_path"))
    rg_cfg = _grid_cfg_with_sectors(cfg, store)
    grid = riskgrid.Grid.from_cfg(rg_cfg)
    print(f"Grilla {grid.ncols}x{grid.nrows} = {grid.n} celdas de {grid.cell_deg}° sobre {grid.bbox}")
    if args.rebuild_fuel:
        print("Leyendo combustible (catastro CONAF si existe, WorldCover)…")
        _, _, src = riskgrid.get_fuel(grid, store, force=True, cfg_fuel=rg_cfg.get("fuel", {}), base_dir=cfg.base_dir)
        print("Fuente de combustible:", src)
    res = riskgrid.get_risk_grid(rg_cfg, cfg.timezone, store, force=True, base_dir=cfg.base_dir)
    if res is None:
        print("Riesgo por zonas desactivado o sin datos.")
        return 1
    s = res.summary()
    print(f"Combustible: {res.fuel_source}")
    print(f"Celdas válidas {s['n_valid']}/{s['n_cells']} · por clase {s['by_class']} · "
          f"Botón Rojo {s['n_boton_rojo']} · 30-30-30 {s['n_303030']}")
    print(f"\n{'#':>3} {'lat':>8} {'lon':>8} {'riesgo':>6} {'clase':<9} {'comb':>5} {'T°':>4} {'HR':>3} {'viento':>6} {'ign':>4} {'FWI':>5}  cobertura")
    for k, c in enumerate(res.top(args.top, "risk_now"), 1):
        cov = ", ".join(f"{n} {int(f * 100)}%" for n, f in sorted(c["cover"].items(), key=lambda x: -x[1])[:3])
        print(f"{k:>3} {c['lat']:>8.3f} {c['lon']:>8.3f} {c['risk_now']:>6.2f} {c['class']:<9} {c['fuel']:>5.2f} "
              f"{c['temp']:>4.0f} {c['rh']:>3.0f} {c['wind']:>6.0f} {c['pi']:>4.0f} {c['fwi'] or 0:>5.1f}  {cov}")
    store.close()
    return 0


def cmd_fuel(args) -> int:
    """Inspecciona el catastro CONAF detectado y cómo se clasifican sus polígonos."""
    from collections import Counter

    from . import fuel as fuelmod
    from . import riskgrid
    from .store import Store

    cfg = load_config(args.config)
    store = Store(cfg.path("db_path"))
    rg_cfg = _grid_cfg_with_sectors(cfg, store)
    store.close()
    cfg_fuel = rg_cfg.get("fuel", {})
    sources = fuelmod.find_catastros(cfg_fuel, cfg.base_dir)
    if not sources:
        d = cfg_fuel.get("conaf_dir", "data/catastro")
        print(f"No hay catastro: deja un .shp (con shx, dbf y prj), .gpkg, .gdb, .zip o .geojson en {d}, "
              f"o fija risk_grid.fuel.conaf_path en config.json. Se usa ESA WorldCover.")
        return 0
    infos = []
    for src in sources:
        info = fuelmod.catastro_info(src)
        infos.append((src, info))
        print(f"Catastro: {info['path']}\n  capa {info['layer']} · {info['geometry_type']} · {info['features']} entidades · CRS {info['crs']}")
    print(f"  campos: {', '.join(infos[0][1]['fields'])}")
    if args.all:
        import pyogrio.raw

        print("Clasificando todas las entidades por atributos (sin geometría)…")
        cats = []
        combos: Counter = Counter()
        n = 0
        for src, info in infos:
            meta, _f, _g, data = pyogrio.raw.read(src.gdal_path, layer=info["layer"], read_geometry=False,
                                                  encoding=cfg_fuel.get("encoding"))
            names = [str(nm) for nm in meta["fields"]]
            ni = len(data[0]) if data else 0
            n += ni
            for i in range(ni):
                props = {nm: data[j][i] for j, nm in enumerate(names)}
                c, _w = fuelmod.classify(props, cfg_fuel.get("rules"))
                cats.append(c)
                combos[(fuelmod.CATEGORIES[c][0], str(props.get("USO") or ""), str(props.get("SUBUSO") or ""))] += 1
        cnt = Counter(fuelmod.CATEGORIES[c][0] for c in cats)
        print(f"{n} entidades clasificadas en {len(infos)} catastro(s):")
        for name, k in cnt.most_common():
            w = next(wt for _k, (nm, wt) in fuelmod.CATEGORIES.items() if nm == name)
            print(f"  {name:<16} {k:>7}  peso {w:.2f}")
        print("\nCombinaciones USO / SUBUSO más frecuentes y categoría asignada:")
        for (cat, uso, sub), k in combos.most_common(25):
            print(f"  {k:>6}  {cat:<16} <- {uso} / {sub}")
        others = [(u, s, k) for (cat, u, s), k in combos.most_common() if cat == "otro"]
        if others:
            print("\nSin regla (quedan como 'otro'):")
            for u, s, k in others[:15]:
                print(f"  {k:>6}  {u} / {s}")
        return 0
    grid = riskgrid.Grid.from_cfg(rg_cfg)
    print(f"Leyendo polígonos dentro de la grilla {grid.bbox} (máx. {args.max} por catastro)…")
    items = []
    for src, _info in infos:
        items += fuelmod.read_catastro(src, grid.bbox, cfg_fuel.get("rules"), max_features=args.max,
                                       encoding=cfg_fuel.get("encoding"))
    cnt = Counter(fuelmod.CATEGORIES[c][0] for _g, c, _w in items)
    print(f"{len(items)} polígonos clasificados:")
    for name, n in cnt.most_common():
        w = next(wt for k, (nm, wt) in fuelmod.CATEGORIES.items() if nm == name)
        print(f"  {name:<16} {n:>7}  peso {w:.2f}")
    if cnt.get("otro", 0) > 0.1 * max(1, len(items)):
        print("\nAviso: más del 10 % quedó como 'otro'. Revisa los nombres de campo con `fuel --sample` y ajusta "
              "risk_grid.fuel.rules en config.json.")
    if args.sample:
        import pyogrio.raw

        src, info = infos[0]
        meta, _f, _g, data = pyogrio.raw.read(src.gdal_path, layer=info["layer"], read_geometry=False,
                                              max_features=args.sample, encoding=cfg_fuel.get("encoding"))
        names = [str(n) for n in meta["fields"]]
        for i in range(min(args.sample, len(data[0]) if data else 0)):
            print("  ", {n: data[j][i] for j, n in enumerate(names)})
    return 0


def cmd_test_alert(args) -> int:
    from .alerts import Alert
    from .notify import build_notifiers, dispatch

    cfg = load_config(args.config)
    nots = build_notifiers(cfg.get("notify", {}))
    print("Canales activos:", ", ".join(n.name for n in nots) or "ninguno")
    a = Alert(0, "TEST", "Sector de prueba", "naranja", "🟠 NARANJA · Sector de prueba",
              "🟠 NARANJA · Sector de prueba\n\n• Mensaje de prueba de FireWatch\n  https://maps.google.com/?q=-34.9,-71.2", {})
    dispatch([a], nots)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="firewatch", description="Alerta temprana de incendios por sector")
    p.add_argument("-c", "--config", default=None, help="ruta a config.json")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"firewatch {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="una corrida completa")
    r.add_argument("--no-ingest", action="store_true", help="no descargar focos ni rayos")
    r.add_argument("--no-weather", action="store_true", help="no consultar meteorología")
    r.add_argument("--no-notify", action="store_true", help="no enviar notificaciones")
    r.set_defaults(fn=cmd_run)

    w = sub.add_parser("watch", help="corridas periódicas")
    w.add_argument("-i", "--interval", type=int, default=600, help="segundos entre corridas")
    w.set_defaults(fn=cmd_watch)

    s = sub.add_parser("status", help="estado por sector")
    s.set_defaults(fn=cmd_status)

    v = sub.add_parser("serve", help="servir el visor web con subida de trazados y corridas automáticas")
    v.add_argument("-p", "--port", type=int, default=8765)
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("-i", "--interval", type=int, default=600, help="segundos entre corridas automáticas (0 = desactivar)")
    v.set_defaults(fn=cmd_serve)

    t = sub.add_parser("test-alert", help="enviar alerta de prueba")
    t.set_defaults(fn=cmd_test_alert)

    g = sub.add_parser("grid", help="calcular el mapa de riesgo por zonas y listar las celdas críticas")
    g.add_argument("--rebuild-fuel", action="store_true", help="volver a leer el combustible desde WorldCover")
    g.add_argument("--top", type=int, default=15)
    g.set_defaults(fn=cmd_grid)

    fu = sub.add_parser("fuel", help="inspeccionar el catastro CONAF detectado y su clasificación")
    fu.add_argument("--max", type=int, default=200000, help="máximo de polígonos a leer")
    fu.add_argument("--sample", type=int, default=0, help="mostrar N registros de atributos")
    fu.add_argument("--all", action="store_true", help="clasificar todas las entidades por atributos, sin leer geometría")
    fu.set_defaults(fn=cmd_fuel)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
