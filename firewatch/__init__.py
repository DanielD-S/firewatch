"""FireWatch: alerta temprana de incendios por sector (líneas eléctricas).

Fusiona condiciones de riesgo (FWI canadiense + probabilidad de ignición +
regla tipo Botón Rojo) con detección satelital de focos (NASA FIRMS, GOES
ABI FDC) y rayos (GOES GLM), y emite alertas por sector.
"""

__version__ = "0.1.0"
