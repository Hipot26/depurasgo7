"""
config.py — Configuración del módulo BALANCE FINAL SGS / Verificador
=====================================================================
Conexión a SGS, identidad del equipo liquidador y usuarios del login propio.

Nada de esto vive en el código:
  · credenciales y URL de SGS → variables de entorno (.env del módulo);
  · equipo liquidador, supervisores y mapas de resolución → equipo.json
    (ruta en VERIFICADOR_EQUIPO_JSON; estructura en equipo.example.json);
  · usuarios del login propio → auth_users.json
    (ruta en VERIFICADOR_AUTH_USERS_JSON; estructura en auth_users.example.json).
Los dos JSON quedan fuera del control de versiones.
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _cargar_json(ruta, defecto):
    try:
        with open(ruta, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return defecto


_EQUIPO = _cargar_json(os.getenv("VERIFICADOR_EQUIPO_JSON", os.path.join(_HERE, "equipo.json")), {})

# ── Conexión SGS (solo lectura) ────────────────────────────────────────────────
SGS_URL      = os.getenv("SGS_URL",      "")
SGS_LOGIN    = os.getenv("SGS_LOGIN",    "")
SGS_PASSWORD = os.getenv("SGS_PASSWORD", "")

# ── Identidad del liquidador (equipo.json) ─────────────────────────────────────
# Nombre corto con el que se rotula al liquidador en el Excel y en la interfaz.
LIQUIDADOR = _EQUIPO.get("liquidador", "")
SUPERVISOR_YO = LIQUIDADOR
# Nombre completo tal como SGS lo escribe en responsible_id (marca «supervisor manual»
# cuando el responsable del proyecto es el propio liquidador).
LIQUIDADOR_NOMBRE_SGS = _EQUIPO.get("liquidador_nombre_sgs", "")
# uid del liquidador en SGS: si responsible_id es el liquidador, el dato está mal asignado.
SGS_UID_LIQUIDADOR = _EQUIPO.get("sgs_uid_liquidador") or None

# ── Equipo liquidador (fragmentos de apellido que SGS muestra en create_uid) ───
LIQUIDADORES_EQUIPO = list(_EQUIPO.get("liquidadores_equipo", []))

# ── Supervisores de campo (orden de las columnas J-Q del reporte) ──────────────
# El supervisor de cada proyecto viene de responsible_id en SGS.
SUPERVISORES_EQUIPO = list(_EQUIPO.get("supervisores_equipo", []))

# ── Fragmento del nombre SGS → nombre corto del supervisor ─────────────────────
# SGS guarda los usuarios con su nombre completo; cuando no coincide de forma
# automática con SUPERVISORES_EQUIPO se resuelve por este mapa.
MAPA_SUPERVISOR = dict(_EQUIPO.get("mapa_supervisor", {}))

# ── Proyectos con supervisor incorrecto en SGS: código PRO → supervisor real ──
MAPA_PROYECTO_SUPERVISOR = dict(_EQUIPO.get("mapa_proyecto_supervisor", {}))

# ── Líder (cuadrilla) → supervisor, respaldo cuando responsible_id no sirve ────
# Conviene omitir líderes que trabajan con varios supervisores.
MAPA_LIDER_SUPERVISOR = dict(_EQUIPO.get("mapa_lider_supervisor", {}))

# ── Responsables (responsible_id) que no son supervisores de campo ─────────────
# Fragmento del nombre SGS, sin tildes. Se resuelven como cuando el responsable es
# el liquidador: mapa manual, casilla Supervisor/Coordinador (approve_id), padre o líder.
RESPONSABLES_NO_SUPERVISOR = list(_EQUIPO.get("responsables_no_supervisor", []))

# ── Departamentos SGS del equipo (fragmento, sin tildes) ───────────────────────
DEPARTAMENTOS_EQUIPO = list(_EQUIPO.get("departamentos_equipo", []))
# Departamentos compartidos con otro grupo: solo cuenta lo que liquidó el equipo.
DEPARTAMENTOS_COMPARTIDOS = list(_EQUIPO.get("departamentos_compartidos", []))

# ── Contrato vigente (casilla Contract de project.dominion) ────────────────────
CONTRATO_NUEVO_ID = int(os.getenv("SGS_CONTRATO_ID", "0") or 0)
CONTRATO_NUEVO_NOMBRE = os.getenv("SGS_CONTRATO_NOMBRE", "")

# ── Tipos de proceso ───────────────────────────────────────────────────────────
# MI = Mayor Impacto / trabajos correctivos (PDL que empieza por DF).
# PG = Programados / AMP Eléctrico (contratos CDOPP, CDOPPI, CDOPT).
TIPOS_PROCESO = {
    "MI": "Mayor Impacto",
    "PG": "Programado / AMP Eléctrico",
}

# ── Directorio de salida ───────────────────────────────────────────────────────
OUTPUT_DIR = os.path.join(_HERE, "output")

# ── Ruta al módulo SGS UI (para importar SGSReadOnlyClient) ───────────────────
SGS_UI_DIR = os.path.join(_HERE, "..", "SGS UI")

# ── Login propio del Verificador (hash werkzeug) ───────────────────────────────
# El login normaliza el usuario con .strip().lower(): las claves van en minúsculas.
AUTH_USERS = _cargar_json(
    os.getenv("VERIFICADOR_AUTH_USERS_JSON", os.path.join(_HERE, "auth_users.json")), {})
