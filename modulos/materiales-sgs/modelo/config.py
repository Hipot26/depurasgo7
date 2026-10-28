"""
config.py — Configuración del módulo MATERIALES (Zona VII)
==========================================================
Módulo independiente servido bajo /materiales. Lee SGS en modo solo lectura.

Las credenciales y secretos se leen de variables de entorno (.env del módulo);
el equipo de supervisores se lee de equipo.json (ruta en MATERIALES_EQUIPO_JSON,
estructura en equipo.example.json), que queda fuera del control de versiones.
"""

import json
import os
from werkzeug.security import generate_password_hash

_HERE = os.path.dirname(os.path.abspath(__file__))


def _cargar_json(ruta, defecto):
    try:
        with open(ruta, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return defecto


_EQUIPO = _cargar_json(os.getenv("MATERIALES_EQUIPO_JSON", os.path.join(_HERE, "equipo.json")), {})

# ── Conexión SGS (solo lectura) ────────────────────────────────────────────────
# La cuenta de lectura ampliada desbloquea la fecha real de almacén y los costos.
SGS_URL      = os.getenv("SGS_URL",      "")
SGS_LOGIN    = os.getenv("SGS_LOGIN",    "")
SGS_PASSWORD = os.getenv("SGS_PASSWORD", "")

# ── Login del sitio web (independiente de SGS) ────────────────────────────────
_WEB_USER = os.getenv("WEB_USER", "").strip().lower()
_WEB_PASS = os.getenv("WEB_PASS", "")
AUTH_USERS = {_WEB_USER: generate_password_hash(_WEB_PASS)}

SECRET_KEY = os.getenv("APP_SECRET", "")

# ── Macro-zonas (Zona VII) — filtro por palabra clave sobre department_id ──────
# El usuario elige macro-zonas; cada una agrupa varios departamentos SGS.
# Orden importa: LINEA_VIVA se evalúa antes que las geográficas.
MACRO_ZONAS = [
    {"id": "UBATE",      "label": "Ubaté",       "keywords": ["UBATE"]},
    {"id": "CHOCONTA",   "label": "Chocontá",    "keywords": ["CHOCONTA"]},
    {"id": "GACHETA",    "label": "Gachetá",     "keywords": ["GACHETA"]},
    {"id": "LINEA_VIVA", "label": "Línea Viva",  "keywords": ["LINEA VIVA"]},
    {"id": "OTROS",      "label": "Otros (Telecontrol, Forestal, Estructura…)",
     "keywords": ["TELECONTROL", "FORESTAL", "ESTRUCTURA", "EMG", "COMERCIAL", "TP/SOT"]},
]

# ── Equipo de supervisores (nombres legibles; filtro opcional) — equipo.json ───
SUPERVISORES_EQUIPO = list(_EQUIPO.get("supervisores_equipo", []))

# Fragmento del nombre SGS → nombre corto del supervisor.
MAPA_SUPERVISOR = dict(_EQUIPO.get("mapa_supervisor", {}))

# ── Flags de capacidades (dependen de los permisos de la cuenta de lectura) ────
USAR_FECHA_ALMACEN = os.getenv("USAR_FECHA_ALMACEN", "1") == "1"   # stock.picking.date_done
USAR_COSTOS        = os.getenv("USAR_COSTOS",        "1") == "1"   # product.product.standard_price

# ── Caché en memoria del análisis (segundos) ──────────────────────────────────
CACHE_TTL = int(os.getenv("CACHE_TTL", "600"))

# ── Umbral de antigüedad alta (días) ──────────────────────────────────────────
ANTIG_ALERTA = 30
