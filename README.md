# FireWatch

Alerta temprana de incendios por sector, pensada para líneas eléctricas en Chile.
Fusiona **condiciones de riesgo** (al estilo Botón Rojo de CONAF) con **detección
satelital** de focos de calor y rayos (al estilo Satellites on Fire), y emite
alertas por WhatsApp, Telegram, webhook o consola cuando un sector lo amerita.

Todas las fuentes son públicas y gratuitas, sin registro.

## Qué hace

Para cada sector (trazado de línea, subestación, predio) se generan dos corredores:
500 m ("cercano") y 5 km ("vigilancia"). En cada corrida:

1. **Ingesta focos de calor**
   - NASA FIRMS: VIIRS SNPP, NOAA-20, NOAA-21 (375 m) y MODIS (1 km), CSV
     regional de 24 h. Latencia 1 a 3 h en Sudamérica.
   - GOES-19 ABI Fire/Hot Spot Characterization (FDC), disco completo cada
     10 minutos, desde el bucket público de NOAA en AWS. Los píxeles de fuego se
     convierten de la grilla fija ABI a lat/lon.
2. **Ingesta rayos**: GOES-19 GLM, descargas cada 20 s, desde el mismo bucket.
3. **Meteorología** (Open-Meteo) en puntos cada 20 km a lo largo del trazado:
   - FWI canadiense diario (FFMC, DMC, DC, ISI, BUI, FWI) con estado persistente.
   - Probabilidad de ignición horaria (componente de ignición NFDRS).
   - Regla 30-30-30 y regla tipo Botón Rojo (ignición >= 70 %, viento >= 20 km/h,
     14:00 a 18:59 hora local), ahora y para las próximas 24 h.
4. **Puntaje por sector**: las condiciones actúan como *prior* de la detección.
   Un foco de confianza media con condiciones críticas pesa más que el mismo foco
   en una noche húmeda. Se considera persistencia (varias pasadas), coincidencia
   multi-fuente, antigüedad, si el viento sopla hacia el trazado y una estimación
   gruesa de tiempo de llegada.
5. **Nivel y alerta**: verde, amarillo (condiciones críticas o rayos sin lluvia),
   naranja (foco en vigilancia, rayos secos con condiciones críticas), rojo
   (foco en el corredor cercano, o foco en vigilancia con viento hacia la línea y
   condiciones críticas). Enfriamiento por nivel, escalada inmediata, y un foco
   nuevo en el corredor cercano rompe el enfriamiento.
6. **Mapa de riesgo por zonas**: grilla regular (20 km por defecto, de Valparaíso
   a La Araucanía) donde cada celda combina condiciones meteorológicas y sequedad
   (ignición, viento, FWI) con la carga de combustible derivada de la cobertura
   ESA WorldCover 10 m (bosque, matorral, pastizal, cultivo). Riesgo = condiciones
   × combustible, en cinco clases. Cada sector hereda el riesgo de la celda más
   crítica sobre su corredor, y pasa a amarillo si supera el umbral.
7. **Visor web** (Leaflet) con sectores en semáforo, corredores, focos, rayos,
   mapa de riesgo por zonas con ranking de celdas críticas, puntos meteorológicos
   y alertas de 72 h.

## Instalación

Python 3.11 o superior.

```bash
pip install -r requirements.txt
```

## Uso

```bash
python -m firewatch serve -p 8765    # visor + subida de trazados + corridas automáticas cada 10 min
python -m firewatch run              # una corrida completa por consola (2 a 3 min la primera vez)
python -m firewatch watch -i 600     # corridas cada 10 min sin visor
python -m firewatch status           # estado por sector y alertas recientes
python -m firewatch test-alert       # prueba los canales configurados
```

Lo habitual es `serve`: abre http://127.0.0.1:8765/, carga tu trazado y deja la
ventana abierta. Opciones de `serve`: `-i 0` desactiva las corridas automáticas.
Opciones de `run`: `--no-ingest`, `--no-weather`, `--no-notify`.

## Tus sectores: cargar un KMZ, KML, Shapefile o GeoJSON

Desde el visor, en el bloque **Cargar trazado**, elige el archivo y pulsa el
botón. Formatos:

| Formato | Cómo subirlo |
|---|---|
| KMZ o KML | tal cual, por ejemplo exportado desde Google Earth |
| Shapefile | un `.zip` con `.shp`, `.shx`, `.dbf` y `.prj` (cualquier CRS; se reproyecta) |
| GeoJSON | en WGS84 (lon/lat) |

Cada entidad (Placemark, registro del Shapefile, feature) se vuelve un sector.
El identificador y el nombre se toman de atributos como `id`, `codigo`,
`cod_tramo`, `nombre`, `name`; si no existen se generan. Las demás columnas se
conservan y se muestran en el visor. Las alturas (Z) se descartan.

El trazado se guarda en la base de datos y queda activo hasta que subas otro o
pulses **Volver al ejemplo**. También queda una copia GeoJSON en `data/`.
Tras subir, los sectores se evalúan de inmediato con los focos y rayos ya
descargados y la meteorología actual; **Correr ahora** lanza una descarga completa.

Por API, lo mismo:

```bash
curl -X POST http://127.0.0.1:8765/api/sectors/upload -H "X-Filename: lineas.kmz" --data-binary @lineas.kmz
```

`data/demo_trazado.kmz` es un KMZ de prueba con dos líneas ficticias. Los
trazados del ejemplo incluido también son ficticios.

## Mapa de riesgo por zonas

```bash
python -m firewatch grid --rebuild-fuel   # calcula la grilla y lista las celdas más críticas
```

La primera vez lee ESA WorldCover desde AWS (seis teselas para la zona central,
unos segundos a un minuto) y guarda el combustible por celda en la base. Después,
cada corrida recalcula la meteorología de la grilla si la caché tiene más de
`refresh_min` minutos (120 por defecto).

### Combustible: catastro de CONAF con relleno WorldCover

Deja el Catastro de Uso de Suelo y Vegetación de CONAF en `data/catastro/`
(GeoPackage, File Geodatabase, Shapefile con prj, zip o GeoJSON; cualquier
CRS). Puede haber varios archivos, uno por región: se combinan todos. Se
detectan solos, se leen únicamente los polígonos dentro de la grilla y cada
uno se clasifica por sus atributos (`USO`, `SUBUSO`, `ESTRUCTURA`,
`TIPO_FORES`, `ESPECI*_CI`) en una categoría con peso:

| Categoría | Peso | Cómo se reconoce |
|---|---|---|
| plantación | 1.00 | SUBUSO o ESTRUCTURA "Plantación"; ESPECI*_CI Pinus, Eucalyptus, Populus |
| bosque nativo esclerófilo | 0.95 | TIPO_FORES "Esclerófilo" |
| bosque nativo (otros tipos), mixto, renoval | 0.85 | SUBUSO "Bosque Nativo", "Bosque Mixto", "Renoval" |
| matorral | 0.90 | SUBUSO "Matorral", "Matorral con Suculentas", "Matorral-Pradera", "Matorral Arborescente" |
| plantación de arbustos (forrajeras) | 0.80 | SUBUSO "Plantación de Arbustos" |
| pradera | 0.70 | SUBUSO "Praderas", "Estepa" |
| agrícola | 0.40 | USO "Terrenos Agrícolas" |
| humedal | 0.30 | USO "Humedales" |
| urbano | 0.05 | USO "Áreas Urbanas e Industriales" |
| sin vegetación | 0.00 | USO "Áreas Desprovistas de Vegetación" |
| agua y nieve | 0.00 | USO "Cuerpos de Agua", "Nieves y Glaciares" |

Las reglas se pueden reemplazar en `risk_grid.fuel.rules` (lista de
`{"fields": [...], "contains": [...], "cat": N, "weight": x}`). Antes de confiar
en el resultado:

```bash
python -m firewatch fuel --all          # clasifica todas las entidades por atributos y lista USO/SUBUSO por categoría
python -m firewatch fuel --sample 5     # campos, CRS y polígonos dentro de la grilla
python -m firewatch grid --rebuild-fuel # recalcula combustible y grilla
```

Validado con el catastro de Coquimbo 2025 (58.899 entidades, UTM 19S, UTF-8):
ninguna queda sin regla. Descarga: en sit.conaf.cl, ficha de la región, botón
"Descargar" (el servidor tarda unos minutos en preparar el zip; Coquimbo pesa
547 MB comprimido y 1 GB extraído). Extrae el zip en `data/catastro/`.

Donde el catastro no cubre, se usa ESA WorldCover (bosque 1.0, matorral 0.9,
pastizal 0.7, cultivo 0.4, humedal 0.3, urbano 0.05; suelo desnudo, agua y
nieve 0). Al cambiar el archivo de catastro la grilla se recalcula sola en la
siguiente corrida. El visor indica qué fuente de combustible está activa.

Cuota de Open-Meteo: cada celda cuenta como una consulta. Con 558 celdas y
refresco cada 2 h son unas 6.700 consultas diarias, dentro del límite gratuito
de 10.000. Si reduces `cell_deg` o amplías `bbox`, sube `refresh_min`.

En el visor, el bloque **Riesgo por zona** permite ver riesgo ahora, riesgo
previsto a 24 h, combustible o condiciones, y lista las celdas más críticas.

## Configuración (`config.json`)

| Campo | Qué controla |
|---|---|
| `bbox` | Área de recorte de focos y rayos (Chile por defecto) |
| `crs_metric` | CRS métrico para buffers (UTM 19S por defecto; usa 32718 para el norte) |
| `buffers_m.near` / `watch` | Corredores en metros |
| `sources.goes_fdc.lookback_min` | Ventana de archivos GOES a revisar por corrida |
| `sources.glm.lookback_min` | Ventana de archivos GLM (cada 20 s; 20 min = 60 archivos) |
| `exclusions` | Máscara de falsos positivos: volcanes, fundiciones, etc. |
| `risk_grid.bbox` / `cell_deg` | Área y tamaño de celda del mapa de riesgo (0.2° ≈ 20 km) |
| `risk_grid.refresh_min` | Minutos entre recálculos de la grilla |
| `risk_grid.zone_amarillo` | Riesgo de zona que pone un sector en amarillo |
| `scoring.levels` | Umbrales de puntaje por nivel |
| `alerts.cooldown_min` | Minutos entre alertas repetidas por nivel |
| `notify` | Canales y nombres de variables de entorno con credenciales |

## Notificaciones

Las credenciales se leen de variables de entorno. Sin ellas, el canal queda apagado.

| Canal | Variables |
|---|---|
| Telegram | `FW_TELEGRAM_TOKEN`, `FW_TELEGRAM_CHAT` |
| WhatsApp Cloud API (Meta) | `FW_WA_TOKEN`, `FW_WA_PHONE_ID`, `FW_WA_TO` (varios separados por coma) |
| Webhook | `FW_WEBHOOK_URL` (POST JSON) |

Telegram es el camino más corto: crea un bot con @BotFather, obtén el `chat_id`
y exporta las dos variables. WhatsApp requiere una app de Meta y, fuera de la
ventana de 24 h, una plantilla aprobada.

## Pruebas

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
```

Incluye el caso de referencia del FWI (`cffdrs`), reglas de condiciones,
geometría, parseo FIRMS y GOES, puntaje, enfriamiento de alertas y una corrida
completa sin red con un foco inyectado.

## Limitaciones conocidas

- **Probabilidad de ignición**: aproximación NFDRS, no el KITRAL de CONAF.
- **Tiempo de llegada**: heurística gruesa; el siguiente paso es Cell2Fire
  (simulador abierto de la Universidad de Chile con modelos de combustible KITRAL).
- **GOES-19** ve Chile centro-sur en oblicuo: pixel efectivo de más de 3 km, menor
  sensibilidad a focos pequeños. De noche funciona bien.
- **Máscara de exclusión** mínima; agrega quemas agrícolas, plantas solares,
  hornos y chimeneas de tu zona.
- **Botón Rojo** no se consume directamente (sin API pública conocida): se replica
  su lógica con umbrales publicados. Si CONAF expone un servicio, se puede agregar
  como capa.
- **Deforestación** (Global Forest Watch) y **cámaras** quedan fuera del MVP.

## Estructura

```
firewatch/
  config.json            configuración
  data/sample_lineas.geojson
  firewatch/
    sources/firms.py     NASA FIRMS
    sources/goes_fdc.py  GOES ABI FDC (netCDF, grilla fija -> lat/lon)
    sources/glm.py       GOES GLM
    sources/weather.py   Open-Meteo + FWI + condiciones horarias
    fwi.py               ecuaciones FWI
    conditions.py        ignición, 30-30-30, Botón Rojo
    sectors.py geo.py    sectores, buffers, rumbos
    scoring.py           fusión detección x condiciones, niveles
    alerts.py notify.py  enfriamiento y canales
    export.py            state.json para el visor
    importers.py         KMZ / KML / Shapefile / GeoJSON -> GeoJSON WGS84
    riskgrid.py          mapa de riesgo por zonas (Open-Meteo multi-punto)
    fuel.py              combustible: catastro CONAF (pyogrio) + WorldCover (rasterio)
    server.py            visor + API de subida + corridas automáticas
    pipeline.py cli.py
  web/index.html         visor Leaflet con formulario de carga
  tests/
```
