"""
blueprint.py — Liquidador de primera generación para plantillas de Presupuesto (módulo julian).
Monta en /julian/ con su propio estado de sesiones y cliente SGS.
Reutiliza verificador.py y browser_client.py del proyecto principal.
"""

import io
import json
import logging
import uuid
import threading
import time as _time_mod
from datetime import datetime

import openpyxl
from openpyxl.styles import Border, Side
from flask import (Blueprint, render_template, request,
                   redirect, url_for, flash, jsonify, send_file)

from sgs_readonly_client import SGSReadOnlyClient
from modulos.verificador  import verificar_fila_mo, verificar_materiales_proyecto
from browser_client       import (PlaywrightClient, _safe as _safe_str, _clasificar_error,
                                   liquidar_mo_parallel_por_proyecto,
                                   liquidar_materiales_lista_modal_parallel,
                                   liquidar_materiales_lista_modal_ws)

USE_POOL_V2 = False
USE_MAT_WS  = True   # Work-stealing: mismo modo que liquidador2
MO_MAX_PROJECT_WORKERS = 2
from modulos.julian.parser_presupuesto import (
    parsear_mo_presupuesto,
    parsear_mat_presupuesto,
    extraer_total_mo_presupuesto,
    extraer_pdl_presupuesto,
)
import os
import re

# ── Blueprint ─────────────────────────────────────────────────────────────────
julian = Blueprint(
    "julian",
    __name__,
    template_folder="../../templates/julian",
    url_prefix="/julian",
)

log = logging.getLogger("julian")

# ── Filtro de bandejas válidas ─────────────────────────────────────────────────
# Una bandeja solo entra al balance si:
#   state = 'approved'  (Estado: Aprobado)
#   delivery_state ≠ valores "no atendido"  (Estado Entrega: Atendido o At. parcial)
# Si el campo delivery_state no existe en SGS (devuelve False/None),
# se incluye la bandeja conservadoramente y se emite un warning.
# Para descubrir el nombre exacto del campo ejecutar: descubrir_campos_tray.py
_TRAY_DELIVERY_FIELD = "state_picking"   # campo verificado via fields_get en SGS
# Valores válidos de state_picking (atendido o parcial).
# "noatendido" queda excluido — la bandeja existe pero almacén no entregó nada.
_PICKING_VALIDO = frozenset({"atendido", "parcial"})
# Estados de bandeja que invalidan la bandeja independientemente del picking.
_STATE_EXCLUIDO = frozenset({"canceled", "canceled_partial", "to_approve"})


def _bandeja_valida_tray(tray: dict) -> bool:
    """Filtro de despacho: state=approved Y state_picking in (atendido, parcial)."""
    state = str(tray.get("state") or "").lower()
    if state in _STATE_EXCLUIDO or state not in ("approved", "aprobado"):
        return False
    sp = tray.get(_TRAY_DELIVERY_FIELD)
    if sp is None or sp is False:
        return True
    return str(sp).lower() in _PICKING_VALIDO


def _bandeja_activa_tray(tray: dict) -> bool:
    """
    Filtro por tipo de bandeja:
    - Despacho: state=approved Y state_picking in {atendido, parcial}.
    - Devolucion: state=approved, state_picking ignorado.
      Una devolución aprobada reduce el neto aunque el almacén no haya completado
      el picking físico; el Excel (CRUCE_LIQUIDADOR) ya la contabiliza en sgs_neto_excel.
    """
    state = str(tray.get("state") or "").lower()
    if state in _STATE_EXCLUIDO or state not in ("approved", "aprobado"):
        return False
    if str(tray.get("type") or "").lower() == "devolution":
        return True
    return _bandeja_valida_tray(tray)


def _fetch_trays_validas(c, domain: list, extra_fields: list = None, limit: int = 200) -> list:
    """
    Busca project.tray filtrando por tipo:
    Despacho: state=approved y state_picking in (atendido, parcial).
    Devolucion: state=approved, state_picking ignorado.
    """
    base_fields = ["id", "name", "type", "partner_id", "date",
                   "project_tray_material_ids", "state", _TRAY_DELIVERY_FIELD]
    extra = list(extra_fields or [])
    try:
        trays = c.search_read("project.tray", domain,
                              base_fields + extra, limit=limit)
    except Exception as ex:
        log.warning(f"[FETCH TRAYS] project.tray domain={domain}: {ex}")
        return []
    antes = len(trays)
    trays = [t for t in trays if _bandeja_activa_tray(t)]
    descartadas = antes - len(trays)
    if descartadas:
        log.info(f"[FETCH TRAYS] {descartadas}/{antes} bandejas descartadas "
                 f"(despacho noatendido o state=canceled/to_approve)")
    return trays


# ── SGS client (propio, independiente del app principal) ──────────────────────
SGS_URL      = os.environ.get("SGS_URL",      "")
SGS_DB       = os.environ.get("SGS_DB",       "")
# uid de la cuenta de servicio en SGS: filtra las líneas de detalle creadas por el liquidador
SGS_UID_SERVICIO = int(os.environ.get("SGS_UID_SERVICIO") or 0)
SGS_LOGIN    = os.environ.get("SGS_LOGIN",    "")
SGS_PASSWORD = os.environ.get("SGS_PASSWORD", "")

_sgs_cache_j: SGSReadOnlyClient = None

def _sgs() -> SGSReadOnlyClient:
    global _sgs_cache_j
    if _sgs_cache_j and _sgs_cache_j.uid:
        try:
            _sgs_cache_j.search("res.users", [["id", "=", _sgs_cache_j.uid]], limit=1)
            return _sgs_cache_j
        except Exception:
            pass
    c = SGSReadOnlyClient(SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD)
    c.autenticar()
    _sgs_cache_j = c
    return c


def _nuevo_sgs_readonly() -> SGSReadOnlyClient:
    c = SGSReadOnlyClient(SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD)
    c.autenticar()
    return c


# ── Constantes de cierre rápido ────────────────────────────────────────────────
CIERRE_RAPIDO_FECHAS = [
    ("date_assignment",   "Asignacion"),
    ("date_expected",     "Prevista"),
    ("date_real",         "Real"),
    ("certification_date","Certificacion"),
    ("date_end_expected", "Fin prevista"),
    ("date_end_real",     "Fin real"),
    ("last_status_change","Ult. cambio estado"),
]
CIERRE_RAPIDO_ESTADO = "1.4-TERMINADO/EN BALANCE"


# ── Utilidades de cierre rápido ────────────────────────────────────────────────
def _texto_m2o_j(valor) -> str:
    if isinstance(valor, (list, tuple)):
        if len(valor) >= 2:
            return str(valor[1] or "").strip()
        if valor:
            return str(valor[0] or "").strip()
        return ""
    return str(valor or "").strip()


def _fecha_iso_j(valor) -> str:
    s = str(valor or "").strip()
    if not s:
        return ""
    s = s.replace("T", " ")
    candidatos = [
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
        ("%d-%m-%Y", 10),
        ("%d/%m/%Y", 10),
    ]
    for fmt, ancho in candidatos:
        try:
            return datetime.strptime(s[:ancho], fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    return ""


def _fecha_ui_j(valor) -> str:
    iso = _fecha_iso_j(valor)
    if not iso:
        return "-"
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d-%m-%Y")
    except Exception:
        return iso


_PDL_SEP_J = re.compile(r"[;,\s]+")


def _pdl_match_j(actual: str, esperado: str) -> bool:
    """True si todos los PDL esperados ya estan en el campo actual."""
    if not esperado or esperado == "-":
        return True
    if actual == esperado:
        return True
    actual_set = {p for p in _PDL_SEP_J.split(str(actual or "")) if p and p != "-"}
    esperado_set = {p for p in _PDL_SEP_J.split(str(esperado or "")) if p and p != "-"}
    return bool(esperado_set) and esperado_set.issubset(actual_set)


def _lcl_activo_j(s: dict | None) -> str:
    s = s or {}
    return str(s.get("lcl_usado") or s.get("lcl_detectado") or "").strip()


def _es_programado_j(s: dict | None) -> bool:
    return bool(re.match(r"6300\d{4,}", _lcl_activo_j(s)))


def _pdl_actual_j(s: dict | None) -> str:
    return str((s or {}).get("pdl_detectado") or "").strip()


def _ctx_verificacion_pdl_j(s: dict | None) -> dict:
    es_programado = _es_programado_j(s)
    pdl_actual = _pdl_actual_j(s)
    return {
        "es_programado": es_programado,
        "pdl_actual": pdl_actual,
        "pdl_requerido": bool(es_programado and not pdl_actual),
    }


def _capturar_pdl_form_j(s: dict | None) -> str:
    if not s:
        return ""
    pdl = str(request.form.get("pdl_input") or "").strip()
    if pdl:
        s["pdl_detectado"] = pdl
    return _pdl_actual_j(s)


def _pdl_obligatorio_ok_j(s: dict | None) -> bool:
    pdl = _capturar_pdl_form_j(s)
    return not _es_programado_j(s) or bool(pdl)


def _obtener_storage_state_activo_j(timeout_s: float = 4.0):
    with _cached_storage_state_lock:
        state = _cached_storage_state
    if state:
        return state
    if _pw_global:
        try:
            _browser_commands.put("force_session")
        except Exception:
            pass
        import time as _t
        t0 = _t.time()
        while (_t.time() - t0) < timeout_s:
            _t.sleep(0.25)
            with _cached_storage_state_lock:
                state = _cached_storage_state
            if state:
                return state
    return None


def _precalentar_pids_j(pids: list) -> None:
    """
    Navega al form de cada proyecto en un browser headless para forzar la
    recomputación de campos stored en Odoo antes de la lectura API.
    Misma lógica que _precalentar_pids en liquidador2 — ver comentario allí.
    """
    if not pids:
        return
    import time as _t
    storage_state = _obtener_storage_state_activo_j(timeout_s=3.0)
    if not storage_state:
        log.info("[JULIAN PRECALENTAR] Sin storage_state — omitiendo warm-up Odoo")
        return
    t0 = _t.time()
    pw = None
    try:
        pw = PlaywrightClient(headless=True, slow_mo=0)
        pw.start(storage_state=storage_state)
        for pid in pids:
            try:
                pw.precalentar_proyecto(pid)
                log.info(f"[JULIAN PRECALENTAR] pid={pid} ok ({_t.time()-t0:.1f}s)")
            except Exception as ex:
                log.warning(f"[JULIAN PRECALENTAR] pid={pid} error: {ex}")
    except Exception as ex:
        log.warning(f"[JULIAN PRECALENTAR] Error iniciando browser warm-up: {ex}")
    finally:
        if pw:
            try:
                pw.close()
            except Exception:
                pass
    log.info(f"[JULIAN PRECALENTAR] {len(pids)} pids en {_t.time()-t0:.1f}s")


def _pids_liquidados_desde_sesion_j(sid) -> dict:
    """Devuelve {pid: pro_code} de proyectos OK en la sesión julian."""
    s = _sesiones.get(sid) or {}
    pids = dict(s.get("pids_liquidados", {}) or {})
    # Complementar desde resultados si los hay
    for r in s.get("resultados_liquidacion", []) or []:
        if r.get("ok") and r.get("pid"):
            pids.setdefault(r["pid"], r.get("pro_code", ""))
    return pids


def _proyectos_identificados_desde_sesion_j(sid) -> dict:
    s = _sesiones.get(sid) or {}
    pids_ok = _pids_liquidados_desde_sesion_j(sid)
    checks_mo  = s.get("checks_mo",  []) or []
    checks_mat = s.get("checks_mat", []) or []
    proyectos = {}

    def _reg(pid, pro_code="", nombre="", odm="", nodo="", fuente=""):
        if not pid:
            return
        info = proyectos.setdefault(pid, {
            "pid": pid, "pro_code": "", "nombre_local": "",
            "odm": "", "nodos": [], "fuentes": [], "liquidado_ok": False,
        })
        if pro_code and not info["pro_code"]:
            info["pro_code"] = pro_code
        if nombre and not info["nombre_local"]:
            info["nombre_local"] = nombre
        if odm and not info["odm"]:
            info["odm"] = odm
        nodo_txt = str(nodo or "").strip()
        if nodo_txt and nodo_txt not in info["nodos"]:
            info["nodos"].append(nodo_txt)
        fuente_txt = str(fuente or "").strip()
        if fuente_txt and fuente_txt not in info["fuentes"]:
            info["fuentes"].append(fuente_txt)
        if pid in pids_ok:
            info["liquidado_ok"] = True
        if pids_ok.get(pid) and not info["pro_code"]:
            info["pro_code"] = pids_ok[pid]

    for ck in checks_mo:
        row = ck.get("row", {}) or {}
        _reg(ck.get("pid"), pro_code=ck.get("pro_code", ""),
             nombre=ck.get("pnombre", ""), odm=row.get("_odm", ""),
             nodo=row.get("pro", ""), fuente="MO")

    for ck in checks_mat:
        mat = ck.get("mat", {}) or {}
        _reg(ck.get("pid"), pro_code=ck.get("pro_code", ""),
             nombre=ck.get("pnombre", ""), odm=mat.get("_odm", ""),
             nodo=mat.get("nodo", ""), fuente="MAT")

    for pid, pro_code in pids_ok.items():
        _reg(pid, pro_code=pro_code, fuente="OK")

    return proyectos


def _resolver_objetivos_cierre_j(sid, fecha_raw="", pdl_raw=""):
    s = _sesiones.get(sid) or {}
    fecha_raw = str(fecha_raw or "").strip()
    fecha_iso = _fecha_iso_j(fecha_raw)
    if not fecha_iso:
        fecha_iso = datetime.now().strftime("%Y-%m-%d")
    fecha_dt = datetime.strptime(fecha_iso, "%Y-%m-%d")
    pdl = str(pdl_raw or s.get("pdl_detectado", "") or "").strip()
    return {
        "fecha_iso":       fecha_iso,
        "fecha_ui":        fecha_dt.strftime("%d-%m-%Y"),
        "fecha_input":     fecha_dt.strftime("%Y-%m-%d"),
        "pdl_esperado":    pdl,
        "estado_esperado": CIERRE_RAPIDO_ESTADO,
    }


def _armar_panel_cierre_rapido_j(sid, fecha_raw="", pdl_raw=""):
    objetivos  = _resolver_objetivos_cierre_j(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw)
    proyectos  = _proyectos_identificados_desde_sesion_j(sid)
    rows = []
    meta = {**objetivos, "total": 0, "elegibles": 0, "pendientes": 0,
            "listos": 0, "referencia": 0}

    if not proyectos:
        return rows, meta

    fields = (
        ["id", "code", "name", "order_number", "nodo",
         "customer_id", "contract_id", "department_id", "status", "pdl"]
        + [campo for campo, _ in CIERRE_RAPIDO_FECHAS]
    )
    registros = _sgs().search_read(
        "project.dominion",
        [["id", "in", list(proyectos.keys())]],
        fields,
        limit=len(proyectos) + 10,
    )
    por_id = {r["id"]: r for r in registros}

    for pid, base in proyectos.items():
        reg = por_id.get(pid, {}) or {}
        pro_code     = str(reg.get("code")         or base.get("pro_code")      or "").strip()
        nombre       = str(reg.get("name")          or base.get("nombre_local")  or "").strip()
        order_number = str(reg.get("order_number")  or base.get("odm")           or "").strip()
        nodo_sgs     = str(reg.get("nodo") or "").strip()
        nodos = list(base.get("nodos") or [])
        if nodo_sgs and nodo_sgs not in nodos:
            nodos.insert(0, nodo_sgs)

        customer     = _texto_m2o_j(reg.get("customer_id"))   or "-"
        contract     = _texto_m2o_j(reg.get("contract_id"))   or "-"
        department   = _texto_m2o_j(reg.get("department_id")) or "-"
        estado_actual   = _texto_m2o_j(reg.get("status"))     or "-"
        estado_esperado = objetivos["estado_esperado"]
        status_ok    = estado_actual == estado_esperado

        pdl_actual  = str(reg.get("pdl") or "").strip() or "-"
        pdl_esperado = objetivos["pdl_esperado"] or "-"
        pdl_ok = _pdl_match_j(pdl_actual, pdl_esperado)

        fechas_diferentes = []
        fechas_ok_count = 0
        for campo, label in CIERRE_RAPIDO_FECHAS:
            actual_iso = _fecha_iso_j(reg.get(campo))
            if actual_iso == objetivos["fecha_iso"]:
                fechas_ok_count += 1
            else:
                fechas_diferentes.append({
                    "campo":    campo,
                    "label":    label,
                    "actual":   _fecha_ui_j(reg.get(campo)),
                    "esperado": objetivos["fecha_ui"],
                })

        fechas_total = len(CIERRE_RAPIDO_FECHAS)
        fechas_ok = fechas_ok_count == fechas_total
        liquidado_ok = bool(base.get("liquidado_ok"))
        necesita_actualizacion = not fechas_ok or not pdl_ok or not status_ok

        if not necesita_actualizacion:
            panel_estado = "ok";     panel_label = "Listo";          meta["listos"] += 1
        else:
            panel_estado = "pendiente"; panel_label = "Pendiente";   meta["pendientes"] += 1

        meta["elegibles"] += 1
        rows.append({
            "pid": pid, "pro_code": pro_code, "nombre": nombre or "-",
            "order_number": order_number or "-", "nodos": nodos,
            "nodos_txt": ", ".join(nodos) if nodos else "-",
            "customer": customer, "contract": contract, "department": department,
            "fuentes_txt": ", ".join(base.get("fuentes") or []) or "-",
            "elegible": True, "liquidado_ok": liquidado_ok,
            "preseleccionado": necesita_actualizacion,
            "panel_estado": panel_estado, "panel_label": panel_label,
            "necesita_actualizacion": necesita_actualizacion,
            "estado_actual": estado_actual, "estado_esperado": estado_esperado,
            "status_ok": status_ok,
            "pdl_actual": pdl_actual, "pdl_esperado": pdl_esperado, "pdl_ok": pdl_ok,
            "fecha_esperada_ui": objetivos["fecha_ui"],
            "fechas_ok": fechas_ok, "fechas_ok_count": fechas_ok_count,
            "fechas_total": fechas_total, "fechas_diferentes": fechas_diferentes,
        })

    rows.sort(key=lambda r: (0 if r["elegible"] else 1, r["pro_code"] or "", r["pid"]))
    meta["total"] = len(rows)
    return rows, meta


def _aplicar_cierre_rapido_j(sid, pids_objetivo=None, fecha_raw="", pdl_raw=""):
    s = _sesiones.get(sid)
    if not s:
        return {"hechos": 0, "errores": ["Sesion no encontrada."], "objetivos": 0}

    rows, meta = _armar_panel_cierre_rapido_j(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw)
    por_pid = {row["pid"]: row for row in rows}

    if pids_objetivo:
        objetivos = [int(pid) for pid in pids_objetivo
                     if int(pid) in por_pid and por_pid[int(pid)]["elegible"]]
    else:
        objetivos = [row["pid"] for row in rows if row["necesita_actualizacion"]]

    if not objetivos:
        return {"hechos": 0, "errores": [], "objetivos": 0, "meta": meta}

    storage_state = _obtener_storage_state_activo_j()
    if not storage_state:
        return {
            "hechos": 0,
            "errores": ["No hay sesion activa de SGS. Abre el navegador de verificacion e inicia sesion primero."],
            "objetivos": len(objetivos),
            "meta": meta,
        }

    hechos = 0
    errores = []
    pw = None
    try:
        pw = PlaywrightClient(headless=False, slow_mo=200)
        pw.start(storage_state=storage_state)
        for pid in objetivos:
            row = por_pid[pid]
            try:
                res = pw.actualizar_fechas_proyecto(
                    pid=pid,
                    fecha=meta["fecha_ui"],
                    pro_code=row["pro_code"],
                    pdl=meta["pdl_esperado"] if meta["pdl_esperado"] != "-" else "",
                    estado_destino=meta["estado_esperado"],
                    pdl_actual_api=row.get("pdl_actual", ""),
                )
                if res.get("ok"):
                    hechos += 1
                else:
                    errores.append(f"{row['pro_code'] or pid}: {res.get('error', 'error desconocido')}")
            except Exception as ex:
                errores.append(f"{row['pro_code'] or pid}: {ex}")
    finally:
        try:
            if pw:
                pw.close()
        except Exception:
            pass

    return {"hechos": hechos, "errores": errores, "objetivos": len(objetivos), "meta": meta}


# ── Sesiones en memoria ───────────────────────────────────────────────────────
_sesiones: dict = {}

# ── Trabajos de liquidación en curso ─────────────────────────────────────────
_trabajos: dict = {}   # sid -> job dict

# ── Playwright persistente ────────────────────────────────────────────────────
_pw_global: PlaywrightClient = None
_pw_lock = threading.Lock()

# Storage state cacheado: exportado por el thread del browser cada 5s.
# Los handlers de Flask leen de aquí — SIN tocar Playwright.
_cached_storage_state: dict = None
_cached_storage_state_lock = threading.Lock()

# ── Command queue ─────────────────────────────────────────────────────────────
# REGLA DE ORO: Flask solo escribe aquí. El monitor lee y ejecuta.
# Nunca al revés — así se evitan los greenlet.error entre threads.
#
# Comandos disponibles:
#   'restart_browser'  → cierra browser actual (si existe) y abre uno nuevo
#
import queue as _cmd_queue_mod
_browser_commands: _cmd_queue_mod.Queue = _cmd_queue_mod.Queue()

# _monitor_running: True una vez que el monitor loop está activo.
# Flask lo lee para saber si el monitor existe (sin llamar DOM).
_monitor_running = False


def sesion_activa() -> bool:
    """True si el browser global tiene sesión activa en SGS. Thread-safe."""
    return bool(_pw_global and _pw_global._logueado)


def _monitor_loop():
    """
    Corre PERMANENTEMENTE en el mismo thread/greenlet que creó el browser.
    Todas las llamadas Playwright ocurren aquí — nunca en hilos de Flask.

    Cada ~4s:
      1. Procesa comandos del queue (ej. restart_browser)
      2. Verifica si navbar de Odoo está visible → actualiza _logueado
      3. Si logueado → exporta storage_state al caché Python
    """
    global _cached_storage_state, _pw_global, _monitor_running
    import time as _t
    _monitor_running = True
    log.info("[MONITOR] Loop iniciado")

    while True:
        _t.sleep(4)
        try:
            # ── Procesar comandos ─────────────────────────────────────────────
            try:
                cmd = _browser_commands.get_nowait()
                log.info(f"[MONITOR] Comando recibido: {cmd}")

                if cmd == 'restart_browser':
                    # Cerrar browser actual (seguro — mismo greenlet)
                    if _pw_global:
                        try:
                            _pw_global.close()
                        except Exception:
                            pass
                        _pw_global = None
                    # Crear browser nuevo (seguro — mismo greenlet)
                    try:
                        _pw_global = PlaywrightClient(headless=False)
                        _pw_global.start()
                        _pw_global.open_to_login_page()
                        log.info("[MONITOR] Browser reiniciado — esperando login manual")
                    except Exception as ex:
                        log.error(f"[MONITOR] Error al reiniciar browser: {ex}")
                        _pw_global = None

                elif cmd == 'close_browser':
                    if _pw_global:
                        try:
                            _pw_global.close()
                        except Exception:
                            pass
                        _pw_global = None
                    log.info("[MONITOR] Browser cerrado por comando close_browser")

                elif cmd == 'force_session':
                    # Fuerza exportar storage_state aunque los selectores no detecten nada
                    if _pw_global and _pw_global._page:
                        try:
                            state = _pw_global._page.context.storage_state()
                            with _cached_storage_state_lock:
                                _cached_storage_state = state
                            _pw_global._logueado = True
                            log.info("[MONITOR] force_session ejecutado — storage_state exportado ✓")
                        except Exception as ex:
                            log.error(f"[MONITOR] force_session falló: {ex}")

            except _cmd_queue_mod.Empty:
                pass

            # ── Verificar sesión ──────────────────────────────────────────────
            if _pw_global and _pw_global._page:
                # Estrategia 1: URL — si estamos en /web (sin /login) ya estamos logueados
                url_ok = False
                try:
                    cur_url = _pw_global._page.url
                    if SGS_URL in cur_url and "/web" in cur_url and "/login" not in cur_url:
                        url_ok = True
                        log.debug(f"[MONITOR] URL indica sesión activa: {cur_url}")
                except Exception:
                    pass

                # Estrategia 2: selectores DOM (varios fallbacks porque varía según la pantalla)
                dom_ok = False
                if not url_ok:
                    _SELECTORS = [
                        ".o_main_navbar",       # navbar principal (dentro de una app)
                        ".o_home_menu_navbar",  # navbar del home/app-switcher
                        ".o_user_menu",         # menú de usuario (esquina superior derecha)
                        ".o_action_manager",    # contenedor de acciones (toda pantalla logueada)
                        "nav.o_main_navbar",    # variante con tag nav
                        ".o_app_icon",          # iconos de apps en el home
                        ".o_web_client",        # div raíz del cliente web Odoo
                    ]
                    for sel in _SELECTORS:
                        try:
                            if _pw_global._page.locator(sel).first.is_visible(timeout=1_500):
                                dom_ok = True
                                log.debug(f"[MONITOR] Selector '{sel}' visible → sesión activa")
                                break
                        except Exception:
                            continue

                visible = url_ok or dom_ok

                if visible:
                    try:
                        state = _pw_global._page.context.storage_state()
                        with _cached_storage_state_lock:
                            _cached_storage_state = state
                        _pw_global._logueado = True
                        log.info("[MONITOR] Sesión detectada ✓")
                    except Exception:
                        pass
                else:
                    _pw_global._logueado = False
                    with _cached_storage_state_lock:
                        _cached_storage_state = None

        except Exception as ex:
            log.warning(f"[MONITOR] Excepción en loop: {ex}")


def _thread_browser():
    """
    Thread de larga vida: SOLO ejecuta el monitor loop.
    NO abre el browser automáticamente al arrancar.

    El browser se abre únicamente cuando el usuario presiona
    "Abrir browser SGS", que pone 'restart_browser' en el queue.
    """
    log.info("[BROWSER-THREAD] Monitor iniciado — sin browser (esperando botón del usuario)")
    _monitor_loop()


# Lanzar el thread del browser al cargar el módulo (daemon — no bloquea shutdown)
threading.Thread(target=_thread_browser, daemon=True, name="pw-monitor").start()


# ── Resolver pid por nodo ─────────────────────────────────────────────────────

def _resolver_pid_nodo(c, nodo_str, rows_mo, checks_mo, odm="") -> int | None:
    """
    Obtiene el pid del nodo usando tres estrategias en cascada:
      1. Cache de checks_mo (si MO ya resolvió este nodo, reutilizar).
      2. Búsqueda por order_number + campo nodo en SGS.
      3. Búsqueda por nombre ilike odm + _nodo_match tolerante
         (NODO9, N9, ND9, N 9, N-9, N09 …).
    """
    from modulos.verificador import (
        _nodo_equiv,
        _nodo_match,
        _filtrar_no_linea_viva,
        buscar_proyectos_por_df_nombre,
    )

    # ── 1. Cache de MO ──────────────────────────────────────────────────────
    for row, ck in zip(rows_mo, checks_mo):
        if str(row.get("pro", "")) == str(nodo_str) and ck.get("pid"):
            return ck["pid"]

    if not odm:
        return None

    # Normalizar nodo a entero cuando aplique. Programados pueden traer nodos
    # alfanumericos como 4AL6.
    m_num = re.search(r"\d+", str(nodo_str))
    nodo_idx = int(m_num.group()) if m_num else None

    # ── 2. Por order_number + campo nodo ────────────────────────────────────
    try:
        candidatos = c.search_read(
            "project.dominion",
            [["order_number", "=", odm]],
            ["id", "code", "name", "nodo", "department_id"], limit=60,
        )
        candidatos, bloqueados_lv = _filtrar_no_linea_viva(candidatos)
        if bloqueados_lv:
            log.info(
                "_resolver_pid_nodo odm=%s nodo=%s: %s candidato(s) Linea Viva/LLEE omitido(s)",
                odm, nodo_str, len(bloqueados_lv)
            )
        if candidatos:
            for p in candidatos:
                if _nodo_equiv(p.get("nodo", ""), nodo_str):
                    return p["id"]
            # nodo field failed (may contain LCL code or wrong format) — try name
            if nodo_idx is not None:
                name_hits = [p for p in candidatos if _nodo_match(p.get("name", ""), nodo_idx)]
                if name_hits:
                    log.info(
                        f"_resolver_pid_nodo nodo={nodo_idx} odm={odm} → "
                        f"{name_hits[0]['code']} (name fallback; nodo field incorrect)"
                    )
                    return name_hits[0]["id"]
            if len(candidatos) == 1 and not str(nodo_str or "").strip():
                return candidatos[0]["id"]
    except Exception as ex:
        log.warning(f"_resolver_pid_nodo step2 odm={odm} nodo={nodo_str}: {ex}")

    # ── 3. Por nombre ilike + _nodo_match tolerante ─────────────────────────
    try:
        candidatos = buscar_proyectos_por_df_nombre(
            c,
            odm,
            ["id", "code", "name", "nodo", "department_id"],
            limit=80,
        )
        candidatos, bloqueados_lv = _filtrar_no_linea_viva(candidatos)
        if bloqueados_lv:
            log.info(
                "_resolver_pid_nodo fallback odm=%s nodo=%s: %s candidato(s) Linea Viva/LLEE omitido(s)",
                odm, nodo_str, len(bloqueados_lv)
            )
        if candidatos:
            matches = [
                p for p in candidatos
                if _nodo_equiv(p.get("nodo", ""), nodo_str)
                or (nodo_idx is not None and _nodo_match(p.get("name", ""), nodo_idx))
            ]
            if len(matches) == 1:
                log.info(f"_resolver_pid_nodo nodo={nodo_idx} → {matches[0]['code']} (vía nombre)")
                return matches[0]["id"]
            if len(matches) > 1:
                log.warning(
                    f"_resolver_pid_nodo: {len(matches)} candidatos para nodo={nodo_idx} "
                    f"en '{odm}'. Se usa el primero: {matches[0]['code']}"
                )
                return matches[0]["id"]
    except Exception as ex:
        log.warning(f"_resolver_pid_nodo step3 odm={odm} nodo={nodo_str}: {ex}")

    return None


# ── Helpers MO ───────────────────────────────────────────────────────────────

MAT_TOL = 0.001


def _cod_sap_desde_producto_j(product_field) -> str:
    if not product_field:
        return ""
    txt = product_field[1] if isinstance(product_field, list) else str(product_field)
    m = re.match(r"\[([^\]/]+)(?:/([^\]]+))?\]", txt)
    if m:
        return (m.group(2) or m.group(1)).strip()
    return str(txt or "").strip()


def _cod_alt_j(codigo: str) -> str:
    codigo = str(codigo or "").strip()
    if not codigo:
        return ""
    return codigo[:-1] if codigo.upper().endswith("C") else codigo + "C"


def _pick_qty_codigo_j(totales: dict, pid: int, codigo: str) -> float:
    codigo = str(codigo or "").strip()
    alt = _cod_alt_j(codigo)
    exacto = float(totales.get((pid, codigo), 0.0) or 0.0)
    if abs(exacto) > MAT_TOL:
        return exacto
    return float(totales.get((pid, alt), 0.0) or 0.0)


def _pick_set_codigo_j(totales: dict, pid: int, codigo: str) -> set:
    codigo = str(codigo or "").strip()
    alt = _cod_alt_j(codigo)
    exacto = set(totales.get((pid, codigo), set()) or set())
    if exacto:
        return exacto
    return set(totales.get((pid, alt), set()) or set())


def _pick_dict_codigo_j(totales: dict, pid: int, codigo: str) -> dict:
    """Retorna {partner: qty} para (pid, codigo), con fallback a variante base/C."""
    codigo = str(codigo or "").strip()
    alt = _cod_alt_j(codigo)
    exacto = totales.get((pid, codigo)) or {}
    if exacto:
        return dict(exacto)
    return dict(totales.get((pid, alt)) or {})


def _mejor_lider_j(partner_qty_dict: dict) -> str:
    """Líder con mayor cantidad despachada. '' si no hay candidatos."""
    if not partner_qty_dict:
        return ""
    return max(partner_qty_dict, key=partner_qty_dict.get)


def _resultado_mat_bloqueado_j(it: dict, msg: str, tipo: str, ok: bool = False) -> dict:
    res = {
        "ok": ok,
        "tipo": "MAT",
        "codigo": it.get("codigo", ""),
        "codigo_sgs": it.get("codigo_sgs", it.get("codigo", "")),
        "pro_code": it.get("pro_code", ""),
        "cantidad": 0,
        "pid": it.get("pid"),
        "pm_id": it.get("pm_id"),
        "partner_nombre": it.get("partner_nombre", ""),
        "idx_job": it.get("idx_job"),
    }
    if ok:
        res["msg"] = msg
    else:
        res["error"] = msg
        res["error_tipo"] = tipo
    return res


def _validar_materiales_julian_seguro(c, items_mat: list, contexto: str = "MAT") -> tuple[list, list]:
    """
    Barrera única antes de escribir MAT en el módulo julian.

    Fuente de verdad para cantidades: project.material.balance (campo propio de SGS).
    balance = despachado - devuelto - advance, mantenido por Odoo.
    Las bandejas solo se usan para confirmar/corregir el líder (partner).

    Bloqueos:
      - Duplicado (pm_id, líder) en el lote (doble escritura)
      - balance SGS stale: devuelto procesado pero balance > D-R-A (Odoo no recomputó aún)
      - Sin bandeja de despacho válida para este código (no hay a quién asignar)
      - suma líderes excede balance SGS del pm_id
      - balance <= 0 (sin pendiente — omitido, no bloqueado)
    NO bloquea:
      - Múltiples líderes → se elige el de mayor cantidad despachada
      - Diferencia balance vs recomputo manual desde bandejas (son coherentes por diseño)
    """
    seguros = []
    resultados_bloqueados = []
    if not items_mat:
        return seguros, resultados_bloqueados

    # ── 1. Detectar duplicados (pm_id, partner) — mismo pm_id con distinto líder ES VÁLIDO ──
    vistos_combo: dict = {}
    for pos, it in enumerate(items_mat):
        pm_id   = it.get("pm_id")
        partner = str(it.get("partner_nombre") or "").strip()
        if pm_id is not None:
            combo = (pm_id, partner)
            vistos_combo.setdefault(combo, []).append(pos)

    combos_bloqueados: set = set()
    for combo, posiciones in vistos_combo.items():
        if len(posiciones) <= 1:
            continue
        combos_bloqueados.add(combo)
        pm_id, partner = combo
        codigos = ", ".join(str(items_mat[p].get("codigo", "?")) for p in posiciones)
        msg = (
            f"Duplicado (pm_id={pm_id}, líder='{partner}'): aparece {len(posiciones)} veces "
            f"({codigos}). No se liquida para evitar doble escritura."
        )
        log.error(f"[{contexto}] {msg}")
        for p in posiciones:
            resultados_bloqueados.append(
                _resultado_mat_bloqueado_j(items_mat[p], msg, "duplicado_pm_lider")
            )

    # Pre-calcular suma total por pm_id para verificar que no exceda balance
    pm_total_qty: dict = {}
    for it in items_mat:
        pm_id   = it.get("pm_id")
        partner = str(it.get("partner_nombre") or "").strip()
        if pm_id is not None and (pm_id, partner) not in combos_bloqueados:
            pm_total_qty[pm_id] = pm_total_qty.get(pm_id, 0.0) + float(it.get("cantidad") or 0)

    pm_ids = list({it.get("pm_id") for it in items_mat
                   if it.get("pm_id") is not None
                   and (it.get("pm_id"), str(it.get("partner_nombre") or "").strip()) not in combos_bloqueados})
    if not pm_ids:
        return seguros, resultados_bloqueados

    # ── 2. Leer state fresco de project.material (fuente de verdad) ──────────
    pm_rows = c.search_read(
        "project.material",
        [["id", "in", pm_ids]],
        ["id", "project_id", "product_code", "despachado", "devuelto", "advance", "balance"],
        limit=len(pm_ids) + 5,
    )
    pm_by_id = {pm["id"]: pm for pm in pm_rows}
    pids = set()
    for pm in pm_rows:
        pr = pm.get("project_id")
        pid = pr[0] if isinstance(pr, list) else pr
        if pid:
            pids.add(pid)

    # ── 3. Bandejas válidas de DESPACHO — solo para confirmar líder ──────────
    partner_qty = {}   # (pid, cod) → {partner_name: qty_total_despachada}
    bandejas_map = {}  # (pid, cod) → {ban_name, ...}
    if pids:
        valid_trays = _fetch_trays_validas(
            c, [["project_id", "in", list(pids)]], extra_fields=["project_id"], limit=500
        )
        tray_mat_ids = []
        for tray in valid_trays:
            tray_mat_ids.extend(tray.get("project_tray_material_ids") or [])
        tray_mats = {}
        if tray_mat_ids:
            for tm in c.read(
                "project.tray.material", tray_mat_ids,
                ["id", "product_id", "deliver_quantity"],
            ):
                tray_mats[tm["id"]] = tm

        for tray in valid_trays:
            if str(tray.get("type") or "").lower() == "devolution":
                continue  # solo despacho para determinar líder
            pr = tray.get("project_id")
            pid = pr[0] if isinstance(pr, list) else pr
            if not pid:
                continue
            partner_ref = tray.get("partner_id")
            partner = partner_ref[1] if isinstance(partner_ref, list) else str(partner_ref or "")
            ban = str(tray.get("name") or "")
            for mat_id in tray.get("project_tray_material_ids") or []:
                tm = tray_mats.get(mat_id)
                if not tm:
                    continue
                cod = _cod_sap_desde_producto_j(tm.get("product_id"))
                qty = float(tm.get("deliver_quantity") or 0)
                if not cod or qty <= 0:
                    continue
                key = (pid, cod)
                if partner:
                    partner_qty.setdefault(key, {})
                    partner_qty[key][partner] = partner_qty[key].get(partner, 0.0) + qty
                if ban:
                    bandejas_map.setdefault(key, set()).add(ban)

    # ── 4. Validar cada item ──────────────────────────────────────────────────
    for it in items_mat:
        pm_id   = it.get("pm_id")
        partner = str(it.get("partner_nombre") or "").strip()
        if (pm_id, partner) in combos_bloqueados:
            continue
        pm = pm_by_id.get(pm_id)
        if not pm:
            msg = f"No se pudo leer project.material pm_id={pm_id}; bloqueado."
            log.error(f"[{contexto}] {msg}")
            resultados_bloqueados.append(_resultado_mat_bloqueado_j(it, msg, "pm_no_leido"))
            continue

        pr = pm.get("project_id")
        pid = pr[0] if isinstance(pr, list) else pr
        cod_sgs = str(pm.get("product_code") or it.get("codigo_sgs") or it.get("codigo") or "").strip()

        # Campos propios de SGS — siempre coherentes entre sí
        balance_sgs    = round(float(pm.get("balance")    or 0.0), 4)
        devuelto_sgs   = round(float(pm.get("devuelto")   or 0.0), 4)
        despachado_sgs = round(float(pm.get("despachado") or 0.0), 4)
        advance_sgs    = round(float(pm.get("advance")    or 0.0), 4)

        # Bandejas válidas para confirmar/completar al líder
        pq       = _pick_dict_codigo_j(partner_qty, pid, cod_sgs)
        bandejas = _pick_set_codigo_j(bandejas_map, pid, cod_sgs)

        razones = []

        # Cuarentena: SOLO si el balance SGS es stale (no refleja aún la devolución).
        # Si balance ≈ D-R-A → Odoo ya recomputó → el neto de bandejas es correcto.
        # Si balance > D-R-A → la recomputación aún no ocurrió → riesgo de sobre-liquidar.
        if devuelto_sgs > MAT_TOL:
            balance_esperado = round(despachado_sgs - devuelto_sgs - advance_sgs, 4)
            if balance_sgs > balance_esperado + MAT_TOL:
                razones.append(
                    f"CUARENTENA balance stale: balance SGS={balance_sgs:g} "
                    f"pero D-R-A={balance_esperado:g} "
                    f"(D={despachado_sgs:g} R={devuelto_sgs:g} A={advance_sgs:g})"
                )

        # Sin bandeja de despacho válida → no hay a quién asignar
        if not pq and not partner:
            razones.append(
                "sin bandeja de despacho válida y sin líder — material no despachado o bandeja inválida"
            )

        # Verificar que la suma de todos los líderes de este pm_id no excede el balance
        total_lote = round(pm_total_qty.get(pm_id, 0.0), 4)
        if total_lote > balance_sgs + MAT_TOL:
            razones.append(
                f"suma líderes={total_lote:g} supera balance SGS={balance_sgs:g} "
                f"(D={despachado_sgs:g} R={devuelto_sgs:g} A={advance_sgs:g})"
            )

        # Sin pendiente (no es un error, se omite)
        if balance_sgs <= MAT_TOL and not razones:
            msg = f"Sin pendiente SGS (balance={balance_sgs:g}) — omitido"
            log.info(f"[{contexto}] OMITIDO pm_id={pm_id} cod={cod_sgs}: {msg}")
            resultados_bloqueados.append(_resultado_mat_bloqueado_j(it, msg, "sin_pendiente", ok=True))
            continue

        if razones:
            msg = "; ".join(razones)
            log.error(
                f"[{contexto}] BLOQUEADO pm_id={pm_id} cod={cod_sgs}: {msg}. "
                f"bandejas={', '.join(sorted(bandejas)) or '-'}"
            )
            resultados_bloqueados.append(_resultado_mat_bloqueado_j(it, msg, "barrera_sgs"))
            continue

        # El líder viene ya calculado desde _items_mat_vivos_por_nodos (por bandeja).
        # Si el item llegó sin líder pero la validación encuentra bandejas, usar la mejor.
        partner_final = partner or _mejor_lider_j(pq)
        if not partner and partner_final:
            log.info(f"[{contexto}] líder asignado en validación cod={cod_sgs}: '{partner_final}'")

        # Cantidad: la que viene del item (calculada por bandeja proporcional)
        cantidad_item = round(float(it.get("cantidad") or 0.0), 4)

        log.info(
            f"[{contexto}] APROBADO pm_id={pm_id} cod={cod_sgs} líder={partner_final}: "
            f"qty={cantidad_item:g} | balance_total={balance_sgs:g} "
            f"(D={despachado_sgs:g} R={devuelto_sgs:g} A={advance_sgs:g}) "
            f"bandejas={', '.join(sorted(bandejas)) or '-'}"
        )
        seguros.append(dict(
            it,
            pid=pid,
            codigo_sgs=cod_sgs,
            cantidad=cantidad_item,
            partner_nombre=partner_final,
            sgs_despachado_real=despachado_sgs,
            sgs_devuelto_real=devuelto_sgs,
            sgs_neto_real=round(despachado_sgs - devuelto_sgs, 4),
            advance_real=advance_sgs,
            balance_precheck=balance_sgs,
            pendiente_api_confirmado=True,
        ))

    return seguros, resultados_bloqueados


def _nodos_desde_indices_mat(checks_mat: list, idx_mat: list) -> set:
    nodos = set()
    for idx in idx_mat:
        if idx >= len(checks_mat):
            continue
        nodo = str((checks_mat[idx].get("mat") or {}).get("nodo") or "").strip()
        if nodo:
            nodos.add(nodo)
    return nodos


def _lider_neto_por_bandeja(dispatch_pq: dict, return_pq: dict) -> dict:
    """
    Calcula net qty por líder: dispatch - return.
    Retornos sin líder conocido ("" key) se distribuyen proporcionalmente al dispatch.
    Retorna {partner: net_qty} con net > 0 únicamente.
    """
    dispatch_pq = dict(dispatch_pq)
    return_pq = dict(return_pq)

    # Retornos sin líder → distribuir por share de despacho
    unattr = return_pq.pop("", 0.0)
    if unattr > 0 and dispatch_pq:
        total_d = sum(dispatch_pq.values()) or 1.0
        for lider, d_qty in dispatch_pq.items():
            return_pq[lider] = return_pq.get(lider, 0.0) + unattr * d_qty / total_d

    neto = {}
    for lider, d_qty in dispatch_pq.items():
        gross = round(d_qty - return_pq.get(lider, 0.0), 4)
        if gross > MAT_TOL:
            neto[lider] = gross
    return neto


def _items_mat_vivos_por_nodos(c, s: dict, nodos_objetivo: set, contexto: str = "LIVE MAT") -> list:
    """
    Arma candidatos MAT desde SGS vivo por nodo.

    Regla de liquidación por líder:
      • Cada líder se liquida exactamente por su despacho neto en bandejas válidas
        (dispatch_lider − return_lider).
      • Si hay advance (ya liquidado), se descuenta proporcionalmente entre líderes
        según su share de despacho neto.
      • Genera UN item por (pm_id, líder) → mismo pm_id puede aparecer N veces.

    Fuente de verdad para cantidades totales: project.material (balance, despachado, devuelto, advance).
    Fuente para splits por líder: bandejas válidas de despacho/devolución.
    """
    checks_mat = s.get("checks_mat", []) or []
    nodo_pid = {}
    nodo_pro = {}
    for ck in checks_mat:
        nodo = str((ck.get("mat") or {}).get("nodo") or "").strip()
        if nodo in nodos_objetivo and ck.get("pid"):
            nodo_pid.setdefault(nodo, ck.get("pid"))
            if ck.get("pro_code"):
                nodo_pro.setdefault(nodo, ck.get("pro_code"))

    log.info(f"[{contexto}] nodos_objetivo={sorted(nodos_objetivo)} → nodo_pid={nodo_pid}")
    nodos_sin_pid = sorted(nodos_objetivo - set(nodo_pid))
    if nodos_sin_pid:
        log.warning(f"[{contexto}] nodos SIN pid (no llegarán al plan): {nodos_sin_pid}")
    if not nodo_pid:
        log.warning(f"[{contexto}] sin pid para nodos={sorted(nodos_objetivo)}")
        return []

    pids = list(set(nodo_pid.values()))
    try:
        proyectos = c.search_read(
            "project.dominion", [["id", "in", pids]], ["id", "code", "name"], limit=len(pids) + 5,
        )
        pro_por_pid = {p["id"]: p.get("code", "") for p in proyectos}
    except Exception as ex:
        log.warning(f"[{contexto}] no se pudo leer project.dominion: {ex}")
        pro_por_pid = {}

    # Fuente de verdad para totales: campos propios de SGS
    pm_all = c.search_read(
        "project.material",
        [["project_id", "in", pids]],
        ["id", "project_id", "product_code", "product_id", "despachado", "devuelto", "advance", "balance"],
        limit=2000,
    )

    # Bandejas válidas (dispatch + devolution) — para splits por líder
    valid_trays = _fetch_trays_validas(
        c, [["project_id", "in", pids]], extra_fields=["project_id"], limit=1000
    )
    tray_mat_ids = []
    for tray in valid_trays:
        tray_mat_ids.extend(tray.get("project_tray_material_ids") or [])
    tray_mats = {}
    if tray_mat_ids:
        for tm in c.read(
            "project.tray.material", tray_mat_ids, ["id", "product_id", "deliver_quantity"],
        ):
            tray_mats[tm["id"]] = tm

    # dispatch_pq[(pid, cod)] = {partner: qty_despachada}
    # return_pq[(pid, cod)]   = {partner: qty_devuelta}  ("" = sin líder conocido)
    # bans[(pid, cod)]        = {nombre_bandeja}
    dispatch_pq: dict = {}
    return_pq:   dict = {}
    bans:        dict = {}
    for tray in valid_trays:
        pr = tray.get("project_id")
        pid = pr[0] if isinstance(pr, list) else pr
        if not pid:
            continue
        es_dev = str(tray.get("type") or "").lower() == "devolution"
        partner_ref = tray.get("partner_id")
        partner = partner_ref[1] if isinstance(partner_ref, list) else str(partner_ref or "")
        ban = str(tray.get("name") or "")
        target = return_pq if es_dev else dispatch_pq
        for mat_id in tray.get("project_tray_material_ids") or []:
            tm = tray_mats.get(mat_id)
            if not tm:
                continue
            cod = _cod_sap_desde_producto_j(tm.get("product_id"))
            qty = float(tm.get("deliver_quantity") or 0)
            if not cod or qty <= 0:
                continue
            key = (pid, cod)
            target.setdefault(key, {})
            target[key][partner] = target[key].get(partner, 0.0) + qty
            if not es_dev and ban:
                bans.setdefault(key, set()).add(ban)

    # Dedup por (pid, base_code): mismo SAP base/C dentro del mismo proyecto → 1 pm_id
    pm_por_base = {}
    for pm in pm_all:
        pr = pm.get("project_id")
        pid_pm = pr[0] if isinstance(pr, list) else pr
        cod_pm = str(pm.get("product_code") or "").strip()
        base = cod_pm[:-1] if cod_pm.upper().endswith("C") else cod_pm
        key = (pid_pm, base)
        old = pm_por_base.get(key)
        if old is None:
            pm_por_base[key] = pm
            continue
        new_is_c = cod_pm.upper().endswith("C")
        old_cod = str(old.get("product_code") or "").strip()
        old_is_c = old_cod.upper().endswith("C")
        if new_is_c and not old_is_c:
            pm_por_base[key] = pm
        elif new_is_c == old_is_c:
            new_bal = float(pm.get("balance") or 0)
            old_bal = float(old.get("balance") or 0)
            if new_bal > old_bal or (abs(new_bal - old_bal) <= MAT_TOL and pm.get("id", 0) > old.get("id", 0)):
                pm_por_base[key] = pm

    items = []
    for pm in pm_por_base.values():
        pr = pm.get("project_id")
        pid = pr[0] if isinstance(pr, list) else pr
        nodo = next((n for n, p in nodo_pid.items() if p == pid), "")
        if not nodo:
            log.warning(
                f"[{contexto}] pm_id={pm.get('id')} cod={pm.get('product_code')} "
                f"pid={pid} — sin nodo en nodo_pid (pid no seleccionado)"
            )
            continue
        cod = str(pm.get("product_code") or "").strip()

        # Totales de SGS — siempre coherentes (balance = despachado - devuelto - advance)
        balance_sgs = round(float(pm.get("balance")    or 0.0), 4)
        d_sgs       = round(float(pm.get("despachado") or 0.0), 4)
        r_sgs       = round(float(pm.get("devuelto")   or 0.0), 4)
        a_sgs       = round(float(pm.get("advance")    or 0.0), 4)

        if balance_sgs <= MAT_TOL:
            log.info(
                f"[{contexto}] EXCLUIDO nodo={nodo} pm_id={pm['id']} cod={cod} "
                f"balance={balance_sgs:g} (D={d_sgs:g} R={r_sgs:g} A={a_sgs:g}) — sin pendiente"
            )
            continue

        # Neto por líder desde bandejas válidas
        dpq = _pick_dict_codigo_j(dispatch_pq, pid, cod)
        rpq = _pick_dict_codigo_j(return_pq,   pid, cod)
        ban_set = _pick_set_codigo_j(bans, pid, cod)
        neto_lider = _lider_neto_por_bandeja(dpq, rpq)

        # Denominador para escalar: despacho_sgs - devuelto_sgs
        # (más fiable que sumar bandejas manualmente)
        denom = round(d_sgs - r_sgs, 4)

        product_ref = pm.get("product_id")
        descripcion = product_ref[1] if isinstance(product_ref, list) and len(product_ref) > 1 else ""
        codigo_excel = cod[:-1] if cod.upper().endswith("C") else cod
        pro_code = nodo_pro.get(nodo) or pro_por_pid.get(pid, "")

        if not neto_lider:
            if dpq:
                # Había bandejas de despacho pero todas canceladas por devoluciones.
                # El balance stale puede mostrar > 0; el neto real de bandejas es 0.
                log.info(
                    f"[{contexto}] EXCLUIDO nodo={nodo} pm_id={pm['id']} cod={cod} "
                    f"despacho_bandejas={dpq} — completamente devuelto, neto=0"
                )
                continue
            # Genuinamente sin bandejas de despacho — incluir para diagnóstico
            log.warning(
                f"[{contexto}] pm_id={pm['id']} cod={cod} sin bandejas válidas de despacho; "
                f"balance={balance_sgs:g} — se incluye sin líder para diagnóstico"
            )
            items.append({
                "tipo": "MAT", "idx": -1, "pm_id": pm["id"], "pid": pid,
                "codigo": codigo_excel, "codigo_sgs": cod,
                "es_custodia": cod.upper().endswith("C"),
                "descripcion": descripcion[:80],
                "cantidad": balance_sgs, "balance_precheck": balance_sgs,
                "sgs_despachado": d_sgs, "sgs_devuelto": r_sgs, "sgs_advance": a_sgs,
                "pro_code": pro_code, "nodo": nodo, "partner_nombre": "",
                "bandeja_despacho": "", "pendiente_api_confirmado": True, "live_sgs": True,
            })
            continue

        # Un item por líder con su pendiente proporcional
        for lider, gross in neto_lider.items():
            # Escalar por balance/denom para distribuir advance proporcionalmente
            if denom > MAT_TOL:
                pendiente_lider = round(gross * balance_sgs / denom, 4)
            else:
                pendiente_lider = round(gross, 4)

            if pendiente_lider <= MAT_TOL:
                continue

            log.info(
                f"[{contexto}] vivo nodo={nodo} pm_id={pm['id']} cod={cod} "
                f"líder={lider} gross={gross:g} → pendiente={pendiente_lider:g} "
                f"(balance={balance_sgs:g} denom={denom:g})"
            )
            items.append({
                "tipo": "MAT", "idx": -1, "pm_id": pm["id"], "pid": pid,
                "codigo": codigo_excel, "codigo_sgs": cod,
                "es_custodia": cod.upper().endswith("C"),
                "descripcion": descripcion[:80],
                "cantidad": pendiente_lider, "balance_precheck": balance_sgs,
                "sgs_despachado": d_sgs, "sgs_devuelto": r_sgs, "sgs_advance": a_sgs,
                "pro_code": pro_code, "nodo": nodo,
                "partner_nombre": lider,
                "bandeja_despacho": ", ".join(sorted(ban_set)),
                "pendiente_api_confirmado": True, "live_sgs": True,
            })

    items.sort(key=lambda it: (str(it.get("nodo", "")), str(it.get("codigo_sgs", "")),
                                str(it.get("partner_nombre", "")), int(it.get("pm_id") or 0)))
    return items


def _grupos_lideres_mo(checks_mo: list) -> list:
    """
    Devuelve los grupos de proyecto que tienen más de un líder disponible.
    Usado para renderizar el panel de selección de líderes en la plantilla.
    """
    grupos = []
    vistos: set = set()
    for ck in checks_mo:
        if not ck.get("ok") or ck.get("ya_liquidada"):
            continue
        lideres = ck.get("lideres") or []
        if len(lideres) <= 1:
            continue
        key = str(ck.get("pid") or ck.get("pro_code") or "")
        if not key or key in vistos:
            continue
        vistos.add(key)
        grupos.append({
            "key":      key,
            "pro_code": ck.get("pro_code", ""),
            "pnombre":  ck.get("pnombre", ""),
            "lideres":  lideres,
        })
    return grupos


def _items_mo_desde_form(checks_mo: list, idx_mo: list, form) -> tuple[list, list]:
    """
    Construye la lista de items MO a liquidar a partir de los índices y el
    formulario de selección de líderes.

    - Proyectos con un solo líder: usa partner_nombre directamente del check.
    - Proyectos con múltiples líderes: lee los controles de grupo del formulario
      (mo_group_mode_*, mo_group_a_*, mo_group_b_*, mo_group_pct_*) y puede
      generar hasta dos ítems por fila (split A/B).

    Retorna (items_mo, errores). Si hay errores, no se debe ejecutar la liquidación.
    """
    items_mo = []
    errores  = []

    for idx in idx_mo:
        if idx >= len(checks_mo):
            continue
        ck = checks_mo[idx]
        if not ck.get("ok") or ck.get("ya_liquidada"):
            continue

        lideres = ck.get("lideres") or []
        # Usar pendiente_real si hubo ajuste por sobre-solicitud
        cantidad_total = float(
            ck["pendiente_real"] if ck.get("ajuste_pendiente") and ck.get("pendiente_real")
            else ck.get("row", {}).get("cantidad", 1)
        )
        base = {
            "tipo":     "MO",
            "idx":      idx,
            "pid":      ck.get("pid"),
            "act_id":   ck.get("act_id"),
            "act_code": ck.get("row", {}).get("codigo", ""),
            "pro_code": ck.get("pro_code", ""),
        }

        cedula_por_nombre = {l["nombre"]: l.get("cedula", "") for l in lideres}

        # ── Caso 1: un solo líder ─────────────────────────────────────────────
        if len(lideres) <= 1:
            partner = lideres[0]["nombre"] if lideres else ck.get("partner_nombre", "")
            if not partner:
                errores.append(f"{base['act_code']}: no tiene líder disponible.")
                continue
            items_mo.append({
                **base,
                "cantidad":            cantidad_total,
                "partner_nombre":      partner,
                "partner_cedula":      cedula_por_nombre.get(partner, ""),
                "cantidad_proyectada": cantidad_total,
            })
            continue

        # ── Caso 2: múltiples líderes — leer panel de grupo ──────────────────
        grupo_key = str(ck.get("pid") or ck.get("pro_code") or idx)
        modo = (
            form.get(f"mo_lider_mode_{idx}", "").strip()
            or form.get(f"mo_group_mode_{grupo_key}", "").strip()
        )
        lider_a = (
            form.get(f"mo_lider_a_{idx}", "").strip()
            or form.get(f"mo_group_a_{grupo_key}", "").strip()
        )
        lider_b = (
            form.get(f"mo_lider_b_{idx}", "").strip()
            or form.get(f"mo_group_b_{grupo_key}", "").strip()
        )
        nombres_validos = {l["nombre"] for l in lideres}

        # Si el usuario no configura reparto, usar el control del proyecto:
        # el primer lider que trae project.team, que ya queda en partner_nombre.
        if not modo:
            partner = ck.get("partner_nombre") or lideres[0]["nombre"]
            if not partner:
                errores.append(f"{base['act_code']}: no tiene lider disponible.")
                continue
            items_mo.append({
                **base,
                "cantidad":            cantidad_total,
                "partner_nombre":      partner,
                "partner_cedula":      cedula_por_nombre.get(partner, ""),
                "cantidad_proyectada": cantidad_total,
                "lider_default":       True,
            })
            continue

        if modo not in ("uno", "dos"):
            errores.append(f"{base['act_code']}: elige si liquidar a un líder o repartir entre dos.")
            continue
        if lider_a not in nombres_validos:
            errores.append(f"{base['act_code']}: selecciona el líder principal.")
            continue

        if modo == "uno":
            items_mo.append({
                **base,
                "cantidad":            cantidad_total,
                "partner_nombre":      lider_a,
                "partner_cedula":      cedula_por_nombre.get(lider_a, ""),
                "cantidad_proyectada": cantidad_total,
            })
            continue

        if lider_b not in nombres_validos or lider_b == lider_a:
            errores.append(f"{base['act_code']}: selecciona dos líderes distintos.")
            continue

        pct_a = form.get(f"mo_pct_a_{idx}", type=float)
        if pct_a is None:
            pct_a = form.get(f"mo_group_pct_{grupo_key}", type=float)
        if pct_a is None or pct_a <= 0 or pct_a >= 100:
            errores.append(f"{base['act_code']}: el porcentaje del líder A debe estar entre 1 y 99.")
            continue

        cant_a = round(cantidad_total * pct_a / 100.0, 4)
        cant_b = round(cantidad_total - cant_a, 4)
        if cant_a <= 0 or cant_b <= 0:
            errores.append(f"{base['act_code']}: el reparto genera cantidades en cero.")
            continue

        items_mo.append({
            **base,
            "cantidad":            cant_a,
            "partner_nombre":      lider_a,
            "partner_cedula":      cedula_por_nombre.get(lider_a, ""),
            "cantidad_proyectada": cantidad_total,
            "split": {"parte": "A", "pct": pct_a, "total": cantidad_total},
        })
        items_mo.append({
            **base,
            "cantidad":            cant_b,
            "partner_nombre":      lider_b,
            "partner_cedula":      cedula_por_nombre.get(lider_b, ""),
            "cantidad_proyectada": cantidad_total,
            "split": {"parte": "B", "pct": round(100.0 - pct_a, 4), "total": cantidad_total},
        })

    return items_mo, errores


# ── Rutas ─────────────────────────────────────────────────────────────────────

@julian.route("/")
def index():
    return redirect(url_for("julian.subir"))


@julian.route("/subir", methods=["GET", "POST"])
def subir():
    if request.method == "POST":
        f = request.files.get("excel")
        if not f or not f.filename.endswith((".xlsx", ".xlsm", ".xls")):
            flash("Sube un archivo Excel (.xlsx)")
            return redirect(url_for("julian.subir"))

        try:
            wb = openpyxl.load_workbook(io.BytesIO(f.read()), data_only=True)
        except Exception as ex:
            flash(f"Error leyendo Excel: {ex}")
            return redirect(url_for("julian.subir"))

        sid = str(uuid.uuid4())

        rows_mo,  errs_mo  = parsear_mo_presupuesto(wb)
        rows_mat, errs_mat = parsear_mat_presupuesto(wb)
        total_mo = extraer_total_mo_presupuesto(wb)
        pdl_detectado = extraer_pdl_presupuesto(wb)

        m_lcl_filename = re.search(r"\b(6300\d{4,})\b", f.filename or "")
        if m_lcl_filename:
            lcl_filename = m_lcl_filename.group(1)
            for row in rows_mo:
                row["_odm"] = lcl_filename
            for row in rows_mat:
                row["_odm"] = lcl_filename
            errs_mo.append(f"[INFO] Programado detectado por nombre de archivo — LCL: {lcl_filename}")

        # Fallback: cuando OdM/LCL de SIPWEBORA es 0, extraer Proyecto padre de SGS_BALANCE.
        # Permite resolver el pid aunque el ticket SIP y el DF de SGS sean distintos.
        if "SGS_BALANCE" in wb.sheetnames and any(not r.get("_odm") for r in rows_mat):
            ws_sgs = wb["SGS_BALANCE"]
            for r_hdr in range(1, min(ws_sgs.max_row + 1, 15)):
                lbl_raw = str(ws_sgs.cell(r_hdr, 1).value or "").upper().strip()
                if "PROYECTO" in lbl_raw and "PADRE" in lbl_raw:
                    val_raw = str(ws_sgs.cell(r_hdr, 2).value or "").strip().upper()
                    if val_raw and val_raw not in {"0", "N/A", "NA", "-", "NONE"}:
                        m_df = re.search(r"\b(DF[\w\d]+|6300\d{4,})\b", val_raw)
                        sgs_odm = m_df.group(1) if m_df else val_raw
                        if sgs_odm:
                            for row in rows_mat:
                                if not row.get("_odm"):
                                    row["_odm"] = sgs_odm
                            errs_mat.append(f"[INFO] Proyecto padre SGS_BALANCE: {sgs_odm} (OdM SIP=0)")
                    break

        # Extraer LCL detectado del primer row
        lcl_detectado = ""
        if rows_mo:
            lcl_detectado = rows_mo[0].get("_odm", "")
        elif rows_mat:
            lcl_detectado = rows_mat[0].get("_odm", "")

        fecha_planilla = request.form.get("fecha_planilla", "").strip()
        if not fecha_planilla:
            flash("La fecha de planilla es obligatoria.")
            return redirect(url_for("julian.subir"))

        _sesiones[sid] = {
            "filename":      f.filename,
            "rows_mo":       rows_mo,
            "rows_mat":      rows_mat,
            "errs_mo":       errs_mo,
            "errs_mat":      errs_mat,
            "total_mo":      total_mo,
            "pdl_detectado": pdl_detectado,
            "checks_mo":     [],
            "checks_mat":    [],
            "verificado_at": None,
            "creado_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
            "lcl_detectado": lcl_detectado,
            "lcl_usado":     "",   # se llena en /confirmar
            "fecha_liquidacion_iso": fecha_planilla,  # YYYY-MM-DD obligatorio
        }

        log.info(f"[JULIAN] Excel cargado | sid={sid[:8]} | {f.filename} | "
                 f"MO={len(rows_mo)} MAT={len(rows_mat)} | LCL={lcl_detectado}")

        return redirect(url_for("julian.confirmar", sid=sid))

    return render_template("julian/subir.html")


@julian.route("/confirmar/<sid>", methods=["GET", "POST"])
def confirmar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada. Sube el Excel de nuevo.")
        return redirect(url_for("julian.subir"))

    if request.method == "POST":
        lcl_manual = request.form.get("lcl_manual", "").strip().upper()
        lcl_final  = lcl_manual or s.get("lcl_detectado", "")

        # Aplicar LCL a todas las filas antes de verificar
        for row in s["rows_mo"]:
            row["_odm"] = lcl_final
        for row in s["rows_mat"]:
            row["_odm"] = lcl_final

        s["lcl_usado"] = lcl_final
        log.info(f"[JULIAN] Confirmar sid={sid[:8]} LCL={lcl_final}")

        if request.form.get("accion") == "balance":
            return redirect(url_for("julian.balance_sgs_directo", sid=sid))

        return redirect(url_for("julian.resumen", sid=sid))

    return render_template("julian/confirmar.html", sid=sid, s=s)


@julian.route("/verificar/<sid>")
def verificar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada. Sube el Excel de nuevo.")
        return redirect(url_for("julian.subir"))

    try:
        c = _sgs()
    except Exception as ex:
        flash(f"Error conectando a SGS: {ex}")
        return render_template("julian/subir.html")

    # ── Verificar MO ─────────────────────────────────────────────────────────
    checks_mo = []
    for row in s["rows_mo"]:
        try:
            ck = verificar_fila_mo(c, row)
        except Exception as ex:
            ck = {"row": row, "ok": False, "errores": [str(ex)], "advertencias": []}
        checks_mo.append(ck)

    # ── Verificar MAT ─────────────────────────────────────────────────────────
    checks_mat = []
    if s["rows_mat"]:
        nodos_unicos = {}
        odm_global = s["rows_mo"][0].get("_odm", "") if s["rows_mo"] else ""

        for mat in s["rows_mat"]:
            nodo = mat["nodo"]
            if nodo not in nodos_unicos:
                odm_mat = mat.get("_odm", odm_global)
                pid = _resolver_pid_nodo(c, nodo, s["rows_mo"], checks_mo, odm_mat)
                nodos_unicos[nodo] = pid

        for mat in s["rows_mat"]:
            nodo = mat["nodo"]
            pid  = nodos_unicos.get(nodo)
            if pid:
                checks_mat.append({"mat": mat, "pid": pid, "_pendiente": True})
            else:
                checks_mat.append({
                    "mat":    mat,
                    "ok":     False,
                    "estado": "no_encontrado",
                    "errores": [f"No se pudo resolver proyecto para nodo {nodo}"],
                    "advertencias": [],
                })

        # Bulk verify por proyecto
        pids_unicos = set(ck["pid"] for ck in checks_mat if ck.get("_pendiente") and ck.get("pid"))

        # Visitar cada proyecto en el browser antes de leer via API.
        # Fuerza a Odoo a procesar el recompute queue de campos stored.
        if not _obtener_storage_state_activo_j(timeout_s=1.0):
            flash("⚠ Browser no iniciado — los datos de materiales pueden ser imprecisos. "
                  "Abre el browser en esta misma página y vuelve a verificar para obtener "
                  "valores actualizados de SGS.", "warning")
        _precalentar_pids_j(list(pids_unicos))

        resultados_por_pid = {}
        for pid in pids_unicos:
            rows_pid = [ck["mat"] for ck in checks_mat if ck.get("pid") == pid]
            try:
                res = verificar_materiales_proyecto(c, pid, rows_pid)
                resultados_por_pid[pid] = {r["mat"]["codigo"]: r for r in res}
            except Exception as ex:
                log.error(f"[JULIAN MAT] Error verificando pid={pid}: {ex}")

        # Info de proyectos
        pid_info = {}
        try:
            proyectos = c.search_read(
                "project.dominion",
                [["id", "in", list(pids_unicos)]],
                ["id", "code", "name"],
                limit=len(pids_unicos) + 10,
            )
            for p in proyectos:
                pid_info[p["id"]] = {"pro_code": p.get("code", ""), "pnombre": p.get("name", "")}
        except Exception as ex:
            log.warning(f"[JULIAN MAT] No se pudo leer info proyectos: {ex}")

        # Mapear resultados
        checks_mat_final = []
        for ck in checks_mat:
            if ck.get("_pendiente"):
                pid  = ck["pid"]
                cod  = ck["mat"]["codigo"]
                res  = resultados_por_pid.get(pid, {}).get(cod)
                info = pid_info.get(pid, {})
                if res:
                    res["pro_code"] = info.get("pro_code", "")
                    res["pnombre"]  = info.get("pnombre", "")
                    checks_mat_final.append(res)
                else:
                    checks_mat_final.append({
                        "mat":         ck["mat"], "ok": False,
                        "estado":      "no_encontrado",
                        "pro_code":    info.get("pro_code", ""),
                        "pnombre":     info.get("pnombre", ""),
                        "errores":     ["No encontrado en project.material"],
                        "advertencias": [],
                        "advance_real": None,
                        "pm_id":       None,
                    })
            else:
                checks_mat_final.append(ck)
        checks_mat = checks_mat_final

    # Regla del módulo: post-procesar ya_liquidado basado solo en datos SGS.
    # verificar_fila_mat puede marcar "descuadre" si Excel != SGS, pero si
    # advance_real >= sgs_neto el material YA está liquidado — no es un error.
    for _mc in checks_mat:
        if _mc.get("ok") or _mc.get("estado") == "ya_liquidado":
            continue
        _sgs_neto  = float(_mc.get("sgs_neto")  or 0)
        _adv_real  = float(_mc.get("advance_real") or 0)
        if _sgs_neto > 0 and _adv_real >= _sgs_neto:
            _mc["ok"]     = True
            _mc["estado"] = "ya_liquidado"
            _mc.setdefault("advertencias", []).append(
                f"Liquidado en SGS (avance={_adv_real:g} ≥ neto={_sgs_neto:g}). "
                "El Excel puede tener cantidad diferente — no es error en este liquidador."
            )
            _mc["errores"] = []

    s["checks_mo"]  = checks_mo
    s["checks_mat"] = checks_mat
    s["verificado_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    def _resumen(checks):
        ok     = sum(1 for c in checks if c.get("ok"))
        error  = sum(1 for c in checks if not c.get("ok"))
        ya_liq = sum(1 for c in checks if c.get("estado") == "ya_liquidado" or c.get("ya_liquidada"))
        return {"ok": ok, "error": error, "ya_liquidado": ya_liq, "total": len(checks)}

    return render_template("julian/verificacion.html",
        sid=sid, s=s,
        checks_mo=checks_mo,
        checks_mat=checks_mat,
        resumen_mo=_resumen(checks_mo),
        resumen_mat=_resumen(checks_mat),
        mo_lider_grupos=_grupos_lideres_mo(checks_mo),
        **_ctx_verificacion_pdl_j(s),
    )


@julian.route("/ver_verificacion/<sid>")
def ver_verificacion(sid):
    """Muestra verificacion.html desde datos en sesion sin re-ejecutar SGS."""
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada. Sube el Excel de nuevo.")
        return redirect(url_for("julian.subir"))

    checks_mo  = s.get("checks_mo")  or []
    checks_mat = s.get("checks_mat") or []
    if not checks_mo and not checks_mat:
        flash("Sin verificacion en sesion. Ejecuta la verificacion primero.")
        return redirect(url_for("julian.verificar", sid=sid))

    def _res(checks):
        ok     = sum(1 for c in checks if c.get("ok"))
        error  = sum(1 for c in checks if not c.get("ok"))
        ya_liq = sum(1 for c in checks if c.get("estado") == "ya_liquidado" or c.get("ya_liquidada"))
        return {"ok": ok, "error": error, "ya_liquidado": ya_liq, "total": len(checks)}

    return render_template("julian/verificacion.html",
        sid=sid, s=s,
        checks_mo=checks_mo,
        checks_mat=checks_mat,
        resumen_mo=_res(checks_mo),
        resumen_mat=_res(checks_mat),
        mo_lider_grupos=_grupos_lideres_mo(checks_mo),
        **_ctx_verificacion_pdl_j(s),
    )


@julian.route("/reverificar_lideres/<sid>")
def reverificar_lideres(sid):
    """Re-lee project.team en SGS y actualiza lideres en checks_mo de la sesion."""
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    checks_mo = s.get("checks_mo") or []
    if not checks_mo:
        flash("Sin verificacion MO en sesion.")
        return redirect(url_for("julian.verificar", sid=sid))

    try:
        c = _sgs()
    except Exception as ex:
        flash(f"Error conectando a SGS: {ex}")
        return redirect(url_for("julian.ver_verificacion", sid=sid))

    pids = list({ck["pid"] for ck in checks_mo if ck.get("pid")})
    equipos_por_pid = {}
    for pid in pids:
        try:
            equipo = c.search_read("project.team",
                [["project_id", "=", pid]], ["id", "partner_id"], limit=10)
            equipos_por_pid[pid] = equipo
        except Exception as ex:
            log.warning(f"[REVERIFICAR LIDERES] pid={pid}: {ex}")

    # Traer cédula (VAT) de todos los partners del equipo en un solo batch
    todos_partner_ids = list({
        e["partner_id"][0]
        for eq in equipos_por_pid.values()
        for e in eq if e.get("partner_id")
    })
    cedula_by_pid = {}
    if todos_partner_ids:
        try:
            pvat = c.search_read(
                "res.partner", [["id", "in", todos_partner_ids]], ["id", "vat"], limit=200
            )
            cedula_by_pid = {p["id"]: (p.get("vat") or "") for p in pvat}
        except Exception as ex:
            log.warning(f"[REVERIFICAR LIDERES] No se pudo traer VAT: {ex}")

    cambios = 0
    for ck in checks_mo:
        pid = ck.get("pid")
        if not pid or pid not in equipos_por_pid:
            continue
        equipo = equipos_por_pid[pid]
        if not equipo:
            continue
        nuevos = [
            {"id": e["partner_id"][0], "nombre": e["partner_id"][1],
             "cedula": cedula_by_pid.get(e["partner_id"][0], "")}
            for e in equipo if e.get("partner_id")
        ]
        if nuevos != ck.get("lideres"):
            ck["lideres"] = nuevos
            if nuevos:
                ck["partner_nombre"] = nuevos[0]["nombre"]
            ck["advertencias"] = [
                a for a in ck.get("advertencias", [])
                if "lider" not in a.lower() and "distribucion" not in a.lower()
            ]
            if len(nuevos) > 1:
                nombres = ", ".join(l["nombre"] for l in nuevos)
                log.info(
                    "[REVERIFICAR LIDERES] pid=%s con %s lideres; se usa control del proyecto por defecto: %s",
                    pid, len(nuevos), nombres,
                )
            cambios += 1

    s["checks_mo"] = checks_mo
    if cambios:
        flash(f"Lideres actualizados: {cambios} fila(s) con cambios.")
    else:
        flash("Sin cambios en la asignacion de lideres desde SGS.")

    return redirect(url_for("julian.ver_verificacion", sid=sid))


@julian.route("/aprobar/<sid>", methods=["GET", "POST"])
def aprobar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    if request.method == "POST":
        indices_mo  = set(int(i) for i in request.form.getlist("idx_mo")  if i.isdigit())
        indices_mat = set(int(i) for i in request.form.getlist("idx_mat") if i.isdigit())

        if not indices_mo and not indices_mat:
            flash("Selecciona al menos 1 item para liquidar.")
            return redirect(url_for("julian.aprobar", sid=sid))

        s["indices_mo_aprobados"]  = indices_mo
        s["indices_mat_aprobados"] = indices_mat
        s["aprobado_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        flash(f"Aprobado: {len(indices_mo)} MO + {len(indices_mat)} MAT seleccionados.")
        return redirect(url_for("julian.plan", sid=sid))

    return render_template("julian/aprobar.html", sid=sid, s=s,
        checks_mo=s.get("checks_mo", []),
        checks_mat=s.get("checks_mat", []))


@julian.route("/plan/<sid>")
def plan(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))
    return render_template("julian/plan.html", sid=sid, s=s)


@julian.route("/probar/<sid>")
def probar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    items_mat = [
        mc for mc in s.get("checks_mat", [])
        if mc.get("pm_id") and mc.get("estado") in ("ok", "avance_parcial", "sin_mov", "sin_mov_ok")
    ]
    items_mo = [
        ck for ck in s.get("checks_mo", [])
        if ck.get("ok") and not ck.get("ya_liquidada") and ck.get("act_id") and ck.get("pid")
    ]

    return render_template("julian/probar.html", sid=sid, s=s,
                           items_mat=items_mat, items_mo=items_mo)


@julian.route("/probar/<sid>/inspect", methods=["POST"])
def probar_inspect(sid):
    s = _sesiones.get(sid)
    if not s:
        return {"ok": False, "error": "Sesion no encontrada"}, 404

    pm_id = request.form.get("pm_id", type=int)
    if not pm_id:
        return {"ok": False, "error": "pm_id requerido"}, 400

    try:
        with PlaywrightClient(headless=False) as pw:
            pw.login()
            info = pw.inspect_material(pm_id)
        return info
    except Exception as ex:
        log.error(f"[JULIAN INSPECT] Error: {ex}")
        return {"ok": False, "error": str(ex)}, 500


@julian.route("/probar/<sid>/run", methods=["POST"])
def probar_run(sid):
    s = _sesiones.get(sid)
    if not s:
        return {"ok": False, "error": "Sesion no encontrada"}, 404

    pm_id          = request.form.get("pm_id",         type=int)
    cantidad       = request.form.get("cantidad",       type=float)
    partner_nombre = request.form.get("partner_nombre", default="")
    pro_code       = request.form.get("pro_code",       default="")
    dry_run        = request.form.get("dry_run",        default="1") == "1"

    if not pm_id or not cantidad or not partner_nombre:
        return {"ok": False, "error": "Faltan pm_id, cantidad o partner_nombre"}, 400

    try:
        with PlaywrightClient(headless=False) as pw:
            pw.login()
            result = pw.liquidar_material(
                pm_id=pm_id, cantidad=cantidad,
                partner_nombre=partner_nombre,
                pro_code=pro_code,
                dry_run=dry_run,
            )
        return result
    except Exception as ex:
        log.error(f"[JULIAN RUN MAT] Error: {ex}")
        return {"ok": False, "error": str(ex)}, 500


@julian.route("/probar/<sid>/run_mo", methods=["POST"])
def probar_run_mo(sid):
    s = _sesiones.get(sid)
    if not s:
        return {"ok": False, "error": "Sesion no encontrada"}, 404

    act_id   = request.form.get("act_id",   type=int)
    pid      = request.form.get("pid",       type=int)
    cantidad = request.form.get("cantidad",  type=float, default=1.0)
    act_code = request.form.get("act_code",  default="")
    pro_code = request.form.get("pro_code",  default="")
    dry_run  = request.form.get("dry_run",   default="1") == "1"

    if not act_id or not pid:
        return {"ok": False, "error": "Faltan act_id y pid"}, 400

    try:
        with PlaywrightClient(headless=False) as pw:
            pw.login()
            result = pw.liquidar_actividad_mo(
                act_id=act_id, pid=pid, cantidad=cantidad,
                act_code=act_code, pro_code=pro_code,
                dry_run=dry_run,
            )
        return result
    except Exception as ex:
        log.error(f"[JULIAN RUN MO] Error: {ex}")
        return {"ok": False, "error": str(ex)}, 500


@julian.route("/preview_liquidar/<sid>", methods=["POST"])
def preview_liquidar(sid):
    """
    Muestra el plan de distribución de workers ANTES de ejecutar.
    El usuario puede revisar qué worker liquidará qué material y confirmar.
    """
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    # ── Gate: requiere sesión activa ─────────────────────────────────────────
    if not _pdl_obligatorio_ok_j(s):
        flash("Proyecto programado (6300...): el PDL es obligatorio antes de liquidar MO o MAT.")
        return redirect(url_for("julian.resumen", sid=sid))

    if not sesion_activa():
        flash("⚠ El browser visible no tiene sesión activa en SGS. "
              "Inicia sesión en la ventana del browser antes de continuar.")
        return redirect(url_for("julian.resumen", sid=sid))

    idx_mo  = [int(i) for i in request.form.getlist("idx_mo")  if i.isdigit()]
    idx_mat = [int(i) for i in request.form.getlist("idx_mat") if i.isdigit()]

    if not idx_mo and not idx_mat:
        flash("Selecciona al menos un item para liquidar.")
        return redirect(url_for("julian.resumen", sid=sid))

    checks_mo  = s.get("checks_mo",  [])
    checks_mat = s.get("checks_mat", [])

    # ── Construir lista de items (igual que en liquidar) ─────────────────────
    items_mo, items_mat = [], []

    items_mo_raw, errores_mo = _items_mo_desde_form(checks_mo, idx_mo, request.form)
    if errores_mo:
        for err in errores_mo:
            flash(err)
        return redirect(url_for("julian.resumen", sid=sid))
    for it in items_mo_raw:
        idx = it.get("idx")
        ck = checks_mo[idx] if idx is not None and idx < len(checks_mo) else {}
        items_mo.append({**it, "nodo": ck.get("row", {}).get("pro", "")})
    s["preview_items_mo"] = items_mo_raw

    if idx_mat:
        try:
            items_mat = _items_mat_vivos_por_nodos(
                _sgs(), s, _nodos_desde_indices_mat(checks_mat, idx_mat),
                contexto="PREVIEW LIVE MAT",
            )
        except Exception as ex:
            flash(f"No se pudo reconsultar SGS vivo para Step 6: {ex}")
            return redirect(url_for("julian.resumen", sid=sid))

    pm_ids_preview = [
        checks_mat[idx]["pm_id"]
        for idx in idx_mat
        if idx < len(checks_mat) and checks_mat[idx].get("pm_id")
    ]
    pendiente_preview_sgs = {}
    if pm_ids_preview:
        try:
            from app import _sgs as _sgs_fn
            frescos = _sgs_fn().search_read(
                "project.material",
                [["id", "in", pm_ids_preview]],
                ["id", "balance", "devuelto"],
                limit=len(pm_ids_preview) + 5,
            )
            pendiente_preview_sgs = {
                f["id"]: {
                    "balance": float(f.get("balance") or 0),
                    "devuelto": float(f.get("devuelto") or 0),
                }
                for f in frescos
            }
        except Exception as ex:
            flash(f"No se pudo refrescar Pendiente SGS para preview: {ex}")
            return redirect(url_for("julian.resumen", sid=sid))

    for idx in ([] if items_mat else idx_mat):
        if idx >= len(checks_mat): continue
        mc = checks_mat[idx]
        if not mc.get("ok") or mc.get("estado") == "ya_liquidado": continue
        if not mc.get("pm_id"): continue
        pm_id = mc["pm_id"]
        fresco = pendiente_preview_sgs.get(pm_id)
        if not fresco:
            flash(f"⚠ {mc['mat']['codigo']}: no se pudo confirmar Pendiente fresco en SGS — omitido.")
            continue
        pendiente = round(fresco["balance"], 4)

        partner_nombre = mc.get("partner_nombre", "")
        if not partner_nombre:
            flash(f"⚠ {mc['mat']['codigo']}: líder no determinado desde despacho — omitido. Verificar bandeja en SGS.")
            log.warning(f"[LIQUIDAR] pm_id={mc['pm_id']} ({mc['mat']['codigo']}) sin partner_nombre — omitido")
            continue

        items_mat.append({
            "tipo":             "MAT",
            "idx":              idx,
            "pm_id":            pm_id,
            "codigo":           mc["mat"]["codigo"],         # código Excel
            "codigo_sgs":       mc.get("codigo_sgs", mc["mat"]["codigo"]),  # código SGS real
            "es_custodia":      mc.get("es_custodia", False),
            "descripcion":      mc["mat"].get("descripcion", "")[:40],
            "cantidad":         pendiente,
            "pro_code":         mc.get("pro_code", ""),
            "nodo":             mc["mat"].get("nodo", ""),
            "partner_nombre":   partner_nombre,
            "bandeja_despacho": mc.get("bandeja_despacho", ""),
        })

    if items_mat:
        try:
            items_mat, bloqueados_preview = _validar_materiales_julian_seguro(
                _sgs(), items_mat, contexto="PREVIEW MAT"
            )
            for b in bloqueados_preview:
                flash(f"{b.get('codigo_sgs') or b.get('codigo')}: {b.get('error') or b.get('msg')}")
        except Exception as ex:
            flash(f"No se pudo validar despacho-devolucion SGS para preview: {ex}")
            return redirect(url_for("julian.resumen", sid=sid))

    if not items_mo and not items_mat:
        flash("Ningún item seleccionado es liquidable.")
        return redirect(url_for("julian.resumen", sid=sid))

    # ── Calcular distribución de workers para MAT ─────────────────────────────
    MAX_WORKERS = 2
    usar_paralelo = len(items_mat) > 1
    n = min(MAX_WORKERS, len(items_mat)) if usar_paralelo else 1
    chunks_mat = []
    if items_mat:
        if usar_paralelo:
            chunks_mat = [
                {"worker_id": i, "mats": items_mat[i::n]}
                for i in range(n)
                if items_mat[i::n]
            ]
        else:
            chunks_mat = [{"worker_id": "vis.", "mats": items_mat}]

    # ── Diagnóstico P3 → P6 ─────────────────────────────────────────────────
    # Compara los nodos seleccionados en Paso 3 con los que llegaron al plan.
    p3_nodos = _nodos_desde_indices_mat(checks_mat, idx_mat)
    p6_nodos = {it["nodo"] for it in items_mat}
    nodos_faltantes_p6 = sorted(p3_nodos - p6_nodos)

    diag_exclusiones = []
    for nodo_f in nodos_faltantes_p6:
        real_idxs_mat = set(idx_mat)
        mats_p3_sel = [
            ck for ck in checks_mat
            if str((ck.get("mat") or {}).get("nodo", "")).strip() == nodo_f
            and ck.get("_real_idx") in real_idxs_mat
        ]
        if not mats_p3_sel:
            # fallback: mostrar todos del nodo (cuando _real_idx no está seteado)
            mats_p3_sel = [
                ck for ck in checks_mat
                if str((ck.get("mat") or {}).get("nodo", "")).strip() == nodo_f
            ]

        entradas = []
        for ck in mats_p3_sel:
            mat = ck.get("mat") or {}
            estado = ck.get("estado", "")
            ok = ck.get("ok", False)
            devuelto = float(ck.get("devuelto") or 0)
            balance = float(ck.get("balance_sgs") or ck.get("balance") or 0)
            partner = ck.get("partner_nombre", "")

            if estado == "ya_liquidado":
                razon = "ya liquidado en SGS"
            elif devuelto > 0 and balance > (float(ck.get("despachado_sgs") or 0) - devuelto - float(ck.get("advance_sgs") or 0) + 0.001):
                razon = f"bloqueado: balance stale — devuelto={devuelto:g} pero balance={balance:g} no recomputado aún"
            elif not ok:
                razon = f"error en verificación ({ck.get('estado_sgs', '')})"
            elif not partner:
                razon = "sin líder — material no encontrado en bandeja de despacho válida"
            elif balance <= 0:
                razon = "pendiente SGS = 0 (ya liquidado o sin despacho)"
            else:
                razon = "sin pendiente en SGS vivo (Despachado − Devuelto − Avance ≤ 0)"

            entradas.append({
                "codigo":  mat.get("codigo", ""),
                "pm_id":   ck.get("pm_id", ""),
                "estado":  estado,
                "razon":   razon,
            })

        diag_exclusiones.append({
            "nodo":     nodo_f,
            "pro_code": mats_p3_sel[0].get("pro_code", "") if mats_p3_sel else "",
            "mats":     entradas,
        })
        log.warning(
            f"[PREVIEW DIAG] nodo={nodo_f} seleccionado en P3 pero AUSENTE en P6 "
            f"— {len(entradas)} material(es): {[e['codigo'] for e in entradas]}"
        )

    bloqueados_diag = locals().get("bloqueados_preview", [])
    hay_mismatch = bool(nodos_faltantes_p6) or bool(bloqueados_diag)

    # ── Estado de sesión (solo Python vars — sin llamadas DOM) ───────────────
    session_ok  = sesion_activa()
    session_msg = (
        "Sesión activa en browser visible — workers heredan sesión sin re-login"
        if session_ok else
        "Sin sesión activa — el gate debería haber bloqueado esto"
    )

    return render_template("julian/preview_liquidacion.html",
        sid=sid, s=s,
        items_mo=items_mo,
        items_mat=items_mat,
        chunks_mat=chunks_mat,
        usar_paralelo=usar_paralelo,
        session_ok=session_ok,
        session_msg=session_msg,
        idx_mo=idx_mo,
        idx_mat=idx_mat,
        p3_nodos=sorted(p3_nodos),
        p6_nodos=sorted(p6_nodos),
        nodos_faltantes_p6=nodos_faltantes_p6,
        diag_exclusiones=diag_exclusiones,
        bloqueados_diag=bloqueados_diag,
        hay_mismatch=hay_mismatch,
    )


@julian.route("/liquidar/<sid>", methods=["POST"])
def liquidar(sid):
    print(f"[LIQUIDAR] sid={sid}", flush=True)

    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    # ── Gate: fecha de planilla obligatoria ────────────────────────────────────
    if not s.get("fecha_liquidacion_iso"):
        flash("Falta la fecha de planilla. Sube el Excel de nuevo e ingresa la fecha obligatoria.")
        return redirect(url_for("julian.subir"))

    # ── Gate: sesión activa requerida ────────────────────────────────────────
    if not _pdl_obligatorio_ok_j(s):
        flash("Proyecto programado (6300...): el PDL es obligatorio antes de liquidar MO o MAT.")
        return redirect(url_for("julian.resumen", sid=sid))

    if not sesion_activa():
        flash("⚠ El browser visible no tiene sesión activa en SGS. "
              "Inicia sesión en la ventana del browser y espera el indicador verde.")
        return redirect(url_for("julian.resumen", sid=sid))

    idx_mo  = [int(i) for i in request.form.getlist("idx_mo")  if i.isdigit()]
    idx_mat = [int(i) for i in request.form.getlist("idx_mat") if i.isdigit()]

    if not idx_mo and not idx_mat:
        flash("Selecciona al menos un item para liquidar.")
        return redirect(url_for("julian.resumen", sid=sid))

    checks_mo  = s.get("checks_mo",  [])
    checks_mat = s.get("checks_mat", [])

    items_mo, items_mat = [], []

    preview_mo = s.pop("preview_items_mo", None)
    if preview_mo is not None:
        # Validated items with partner_nombre from preview step
        items_mo = [it for it in preview_mo if it.get("idx") in set(idx_mo)]
    else:
        # Fallback: re-validate from form (e.g. direct POST without preview)
        items_mo_raw, errores_mo = _items_mo_desde_form(checks_mo, idx_mo, request.form)
        if errores_mo:
            for err in errores_mo:
                flash(err)
            return redirect(url_for("julian.resumen", sid=sid))
        items_mo = items_mo_raw

    # Re-verificar estado REAL en SGS antes de armar la lista — barrera anti-doble-liq.
    # Cubre el caso: usuario pausa → vuelve atrás → relanza con datos cacheados viejos.
    if idx_mat:
        try:
            items_mat = _items_mat_vivos_por_nodos(
                _sgs(), s, _nodos_desde_indices_mat(checks_mat, idx_mat),
                contexto="LIQUIDAR LIVE MAT",
            )
        except Exception as ex:
            flash(f"No se pudo reconsultar SGS vivo antes de liquidar: {ex}")
            return redirect(url_for("julian.resumen", sid=sid))

    pm_ids_mat = [
        checks_mat[idx]["pm_id"]
        for idx in idx_mat
        if idx < len(checks_mat) and checks_mat[idx].get("pm_id")
    ]
    advance_real_sgs = {}
    precheck_pendiente_ok = True
    if pm_ids_mat:
        try:
            from app import _sgs as _sgs_fn
            frescos = _sgs_fn().search_read(
                "project.material",
                [["id", "in", pm_ids_mat]],
                ["id", "advance", "amount", "balance", "devuelto"],
                limit=len(pm_ids_mat) + 5,
            )
            for f in frescos:
                advance_real_sgs[f["id"]] = {
                    "advance": float(f.get("advance") or 0),
                    "amount": float(f.get("amount") or 0),
                    "balance": float(f.get("balance") or 0),
                    "devuelto": float(f.get("devuelto") or 0),
                }
        except Exception as ex:
            log.warning(f"[JULIAN LIQUIDAR] Re-verificación SGS falló: {ex} — usando caché")
            precheck_pendiente_ok = False

    if pm_ids_mat and not precheck_pendiente_ok:
        flash("No se pudo verificar el Pendiente fresco en SGS. No se liquidan materiales por seguridad.")
        return redirect(url_for("julian.resumen", sid=sid))

    omitidos_ya_liq = []
    for idx in ([] if items_mat else idx_mat):
        if idx >= len(checks_mat): continue
        mc = checks_mat[idx]
        if not mc.get("ok") or mc.get("estado") == "ya_liquidado": continue
        if not mc.get("pm_id"): continue

        pm_id = mc["pm_id"]

        if pm_id not in advance_real_sgs:
            flash(f"⚠ {mc['mat']['codigo']}: no se pudo confirmar Pendiente fresco en SGS — omitido.")
            log.error(f"[JULIAN LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) no vino en re-verificacion SGS - omitido")
            continue

        fresco    = advance_real_sgs[pm_id]
        pendiente = round(fresco["balance"], 4)
        devuelto  = fresco.get("devuelto", 0.0)

        if devuelto > 0.001:
            log.info(
                f"[JULIAN LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) "
                f"devuelto={devuelto:g} factorizado — Capa1 (neto bandejas) ya validó."
            )

        # Regla del módulo: SGS es la fuente de verdad. La cantidad es el balance SGS fresco
        # (despachado − devuelto − advance). El Excel solo identifica los nodos.
        cantidad_a_liquidar = pendiente

        if False and cantidad_a_liquidar <= 0.001:
            omitidos_ya_liq.append(mc["mat"]["codigo"])
            log.info(
                f"[JULIAN LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) "
                f"balance SGS={pendiente} — ya liquidado o sin pendiente, omitido"
            )
            continue

        partner_nombre = mc.get("partner_nombre", "")
        if not partner_nombre:
            flash(f"⚠ {mc['mat']['codigo']}: lider no determinado desde despacho — omitido. Verificar bandeja en SGS.")
            log.warning(f"[JULIAN LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) sin partner_nombre — omitido")
            continue

        log.info(
            f"[JULIAN LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) "
            f"balance_sgs={pendiente:g} → liquidar"
        )
        items_mat.append({
            "tipo":                    "MAT",
            "idx":                     idx,
            "pm_id":                   pm_id,
            "pid":                     mc.get("pid"),
            "codigo":                  mc["mat"]["codigo"],
            "codigo_sgs":              mc.get("codigo_sgs", mc["mat"]["codigo"]),
            "es_custodia":             mc.get("es_custodia", False),
            "cantidad":                cantidad_a_liquidar,
            "excel_neto":              float(mc.get("sgs_neto") or mc["mat"].get("sgs_neto_excel") or 0),
            "advance_precheck":        round(fresco.get("advance", 0), 4),
            "balance_precheck":        pendiente,
            "pro_code":                mc.get("pro_code", ""),
            "partner_nombre":          partner_nombre,
            "bandeja_despacho":        mc.get("bandeja_despacho", ""),
            "pendiente_api_confirmado": True,
        })

    if items_mat:
        try:
            items_mat, bloqueados_liq = _validar_materiales_julian_seguro(
                _sgs(), items_mat, contexto="LIQUIDAR MAT"
            )
            for b in bloqueados_liq:
                if b.get("ok"):
                    omitidos_ya_liq.append(b.get("codigo_sgs") or b.get("codigo", "MAT"))
                else:
                    flash(f"{b.get('codigo_sgs') or b.get('codigo')}: {b.get('error')}")
        except Exception as ex:
            flash(f"No se pudo validar despacho-devolucion SGS antes de liquidar: {ex}")
            return redirect(url_for("julian.resumen", sid=sid))

    if omitidos_ya_liq:
        flash(f"⚠ {len(omitidos_ya_liq)} material(es) ya liquidado(s) en SGS omitido(s): "
              f"{', '.join(omitidos_ya_liq)}")

    if not items_mo and not items_mat:
        flash("Ningún item seleccionado es liquidable (todos ya están liquidados en SGS).")
        return redirect(url_for("julian.resumen", sid=sid))

    # Leer storage state del caché (exportado por el thread del browser cada 5s).
    # Nunca llamamos DOM de Playwright desde hilos de Flask — evita greenlet.error.
    with _cached_storage_state_lock:
        storage_state = _cached_storage_state

    if not storage_state:
        flash("⚠ Estado de sesión no disponible aún. Espera unos segundos e intenta de nuevo.")
        return redirect(url_for("julian.resumen", sid=sid))

    log.info("[JULIAN LIQUIDAR] Storage state listo — workers heredan sesión")

    # Calcular distribución de workers para el tracking

    for i, it in enumerate(items_mat):
        it["idx_job"] = i

    # Crear estructura del trabajo con estado inicial
    job_items_mo = [
        {"codigo": it["act_code"], "tipo": "MO", "worker_id": "vis.",
         "estado": "pendiente", "error": None}
        for it in items_mo
    ]
    job_items_mat = [
        {"codigo": it["codigo"], "tipo": "MAT",
         "codigo_sgs":  it.get("codigo_sgs", it["codigo"]),
         "es_custodia": it.get("es_custodia", False),
         "worker_id": 0,
         "estado": "pendiente", "error": None}
        for i, it in enumerate(items_mat)
    ]

    _trabajos[sid] = {
        "estado":    "corriendo",
        "items_mo":  job_items_mo,
        "items_mat": job_items_mat,
        "resultados": None,
        "error":     None,
        "t_inicio":  _time_mod.time(),
    }

    # Lanzar liquidación en hilo de fondo
    t = threading.Thread(
        target=_ejecutar_liquidacion_bg,
        args=(sid, items_mo, items_mat, storage_state),
        daemon=True,
    )
    t.start()
    print(f"[LIQUIDAR] Thread lanzado — redirigiendo a /esperar/{sid[:8]}", flush=True)

    return redirect(url_for("julian.esperar", sid=sid))


def _ejecutar_liquidacion_bg(sid, items_mo, items_mat, storage_state):
    """Corre en hilo de fondo — actualiza _trabajos[sid] con progreso en tiempo real."""
    job = _trabajos.get(sid)
    if not job:
        return

    s_init = _sesiones.get(sid) or {}
    _fecha_iso_j = s_init.get("fecha_liquidacion_iso", "")
    try:
        _fecha_mo_j = datetime.strptime(_fecha_iso_j, "%Y-%m-%d").strftime("%d/%m/%Y") if _fecha_iso_j else None
    except Exception:
        _fecha_mo_j = None

    def _cancelled():
        return _trabajos.get(sid, {}).get("cancelado", False)

    resultados = []

    try:
        c = _sgs()

        # ── MO: browser headless con sesión heredada ─────────────────────────
        def _progress_mo(orig_idx, worker_id, estado, codigo, error=None):
            try:
                job["items_mo"][orig_idx]["worker_id"] = worker_id
                job["items_mo"][orig_idx]["estado"] = estado
                if estado == "corriendo":
                    job["items_mo"][orig_idx]["error"] = None
                elif error:
                    job["items_mo"][orig_idx]["error"] = str(error)[:100]
            except Exception:
                pass

        resultados_mo = liquidar_mo_parallel_por_proyecto(
            items_mo,
            max_workers=MO_MAX_PROJECT_WORKERS,
            storage_state=storage_state,
            progress_cb=_progress_mo,
            should_cancel=_cancelled,
            sgs_client_factory=_nuevo_sgs_readonly,
            fecha=_fecha_mo_j,
        )
        resultados.extend(resultados_mo)

        for i, res in enumerate(resultados_mo):
            job["items_mo"][i]["estado"] = "ok" if res.get("ok") else "error"
            if not res.get("ok"):
                job["items_mo"][i]["error"] = res.get("error", "Error desconocido")[:100]

        # ── Chequeo de cancelación antes de MAT ─────────────────────────────
        if _cancelled():
            log.info(f"[BG LIQUIDAR] sid={sid[:8]} CANCELADO antes de MAT")
            job["estado"] = "cancelado"
            s2 = _sesiones.get(sid)
            if s2:
                s2["resultados_liquidacion"] = resultados
            job["resultados"] = resultados
            return

        # ── MAT ─────────────────────────────────────────────────────────────
        # Segunda barrera anti-devoluciones: justo antes de escribir, refrescar
        # Pendiente desde SGS. Nunca liquidar Proyectado/Excel si hubo Devuelto.
        # ── Re-verificar MAT en SGS (segunda barrera — igual que liquidador2) ────
        # Lee advance_details fresco y compara pendiente_excel contra contrato.
        items_mat_seguros = []
        if items_mat:
            if job.get("cancelado"):
                items_mat = []
            else:
                try:
                    pm_ids = [it["pm_id"] for it in items_mat]
                    frescos = c.search_read(
                        "project.material", [["id", "in", pm_ids]],
                        ["id", "advance", "amount", "balance", "devuelto"],
                        limit=len(pm_ids) + 5)
                    dets = c.search_read(
                        "project.material.details", [["project_material_id", "in", pm_ids]],
                        ["id", "project_material_id", "amount"], limit=1000)
                    advance_details = {}
                    for d in dets:
                        amt_det = float(d.get("amount") or 0)
                        if amt_det <= 0:
                            continue
                        pm_ref    = d.get("project_material_id")
                        pm_det_id = pm_ref[0] if isinstance(pm_ref, list) else pm_ref
                        advance_details[pm_det_id] = advance_details.get(pm_det_id, 0.0) + amt_det
                    estado_sgs = {
                        f["id"]: {
                            "advance":         float(f.get("advance")  or 0),
                            "advance_details": float(advance_details.get(f["id"], 0.0)),
                            "amount":          float(f.get("amount")   or 0),
                            "balance":         float(f.get("balance")  or 0),
                            "devuelto":        float(f.get("devuelto") or 0),
                        }
                        for f in frescos
                    }
                    for it in items_mat:
                        est            = estado_sgs.get(it["pm_id"], {})
                        adv_details    = est.get("advance_details", est.get("advance", 0.0))
                        pendiente_real = round(est.get("balance", 0.0), 4)
                        devuelto       = est.get("devuelto", 0.0)
                        excel_neto     = round(float(it.get("excel_neto", it.get("cantidad", 0))), 4)
                        pendiente_excel = round(excel_neto - adv_details, 4)
                        job_pos = it.get("idx_job")

                        if devuelto > 0.001:
                            log.info(f"[BG MAT] pm_id={it['pm_id']} ({it['codigo']}) "
                                     f"devuelto={devuelto:g} factorizado — continuando Capa2+3.")

                        if pendiente_excel <= 0.001:
                            if job_pos is not None and job_pos < len(job["items_mat"]):
                                job["items_mat"][job_pos]["estado"] = "ok"
                                job["items_mat"][job_pos]["error"]  = "Sin pendiente en SGS"
                            resultados.append({
                                "ok": True, "tipo": "MAT",
                                "codigo": it.get("codigo", ""), "pro_code": it.get("pro_code", ""),
                                "cantidad": 0, "pid": it.get("pid"), "pm_id": it.get("pm_id"),
                                "msg": "Ya liquidado en SGS — omitido",
                            })
                            continue

                        cantidad_contrato = round(float(it.get("cantidad", 0)), 4)
                        if abs(pendiente_excel - cantidad_contrato) > 0.001:
                            msg = (f"Contrato Excel cambio antes de ejecutar: esperado {cantidad_contrato:g}, "
                                   f"ahora pendiente Excel/details={pendiente_excel:g}")
                            if job_pos is not None and job_pos < len(job["items_mat"]):
                                job["items_mat"][job_pos]["estado"] = "error"
                                job["items_mat"][job_pos]["error"]  = "Bloqueado: cantidad Excel cambio"
                            resultados.append({
                                "ok": False, "tipo": "MAT",
                                "codigo": it.get("codigo", ""), "pro_code": it.get("pro_code", ""),
                                "cantidad": cantidad_contrato, "pid": it.get("pid"),
                                "pm_id": it.get("pm_id"),
                                "error": msg, "error_tipo": "mat_excel_contract_changed",
                            })
                            continue

                        amount_ppto = round(est.get("amount", 0.0), 4)
                        # Bloquear solo si la cantidad supera el balance SGS disponible.
                        # Splits multi-líder: cantidad_contrato < pendiente_real es válido
                        # (pm.balance es el total de todos los líderes combinados).
                        if cantidad_contrato > pendiente_real + 0.001:
                            if amount_ppto > 0 and cantidad_contrato > amount_ppto + 0.001:
                                msg = (f"SOBRE_PROYECTADO: Excel pide {cantidad_contrato:g} "
                                       f"pero presupuestado={amount_ppto:g}. "
                                       "Odoo decidira si acepta o rechaza.")
                                error_tipo = "sobre_proyectado"
                            else:
                                msg = (f"Excel pide {cantidad_contrato:g} supera SGS Pendiente={pendiente_real:g}. "
                                       "No se puede liquidar mas del balance disponible.")
                                error_tipo = "mat_sobre_balance"
                            if job_pos is not None and job_pos < len(job["items_mat"]):
                                job["items_mat"][job_pos]["estado"] = "error"
                                job["items_mat"][job_pos]["error"]  = msg[:120]
                            resultados.append({
                                "ok": False, "tipo": "MAT",
                                "codigo": it.get("codigo", ""), "pro_code": it.get("pro_code", ""),
                                "cantidad": cantidad_contrato, "pid": it.get("pid"),
                                "pm_id": it.get("pm_id"),
                                "error": msg, "error_tipo": error_tipo,
                            })
                            continue

                        items_mat_seguros.append(dict(it, pendiente_api_confirmado=True))

                except Exception as ex:
                    log.error(f"[BG MAT] Re-verificacion SGS fallo: {ex} — abortando MAT por seguridad")
                    for i, it in enumerate(items_mat):
                        jp = it.get("idx_job", i)
                        if jp < len(job["items_mat"]):
                            job["items_mat"][jp]["estado"] = "error"
                            job["items_mat"][jp]["error"]  = "No se pudo verificar Pendiente SGS"
                        resultados.append({
                            "ok": False, "tipo": "MAT",
                            "codigo": it.get("codigo", ""), "pro_code": it.get("pro_code", ""),
                            "cantidad": it.get("cantidad", 0), "pid": it.get("pid"),
                            "error": f"No se pudo verificar Pendiente SGS: {ex}",
                            "error_tipo": "precheck_pendiente_failed",
                        })
                    items_mat_seguros = []
            items_mat = items_mat_seguros

        # ── Barrera despacho/devolucion (_validar_materiales_julian_seguro) ───
        if items_mat:
            try:
                items_mat, bloqueados_bg = _validar_materiales_julian_seguro(
                    c, items_mat, contexto="BG MAT"
                )
                for b in bloqueados_bg:
                    idx_job = b.get("idx_job")
                    if idx_job is not None and idx_job < len(job["items_mat"]):
                        job["items_mat"][idx_job]["estado"] = "ok" if b.get("ok") else "error"
                        job["items_mat"][idx_job]["error"] = (b.get("error") or b.get("msg") or "")[:120]
                    resultados.append(b)
            except Exception as ex:
                log.error(f"[BG MAT] Validacion despacho-devolucion fallo: {ex} — abortando MAT")
                for it in items_mat:
                    idx_job = it.get("idx_job")
                    if idx_job is not None and idx_job < len(job["items_mat"]):
                        job["items_mat"][idx_job]["estado"] = "error"
                        job["items_mat"][idx_job]["error"] = "No se pudo verificar despacho-devolucion SGS"
                    resultados.append({
                        "ok": False, "tipo": "MAT",
                        "codigo": it.get("codigo", ""), "pro_code": it.get("pro_code", ""),
                        "cantidad": 0, "pid": it.get("pid"),
                        "error": f"No se pudo verificar despacho-devolucion SGS: {ex}",
                        "error_tipo": "precheck_sgs_neto_failed",
                    })
                items_mat = []

        # ── Liquidar MAT (mismo modo que liquidador2: work-stealing modal) ───
        if len(items_mat) > 0:
            def _progress_cb(orig_idx, worker_id, estado, codigo, error=None):
                idx_job = items_mat[orig_idx].get("idx_job", orig_idx) if orig_idx < len(items_mat) else orig_idx
                if idx_job < len(job["items_mat"]):
                    job["items_mat"][idx_job]["estado"]    = estado
                    job["items_mat"][idx_job]["worker_id"] = worker_id
                    if error:
                        job["items_mat"][idx_job]["error"] = str(error)[:120]

            _mat_fn = liquidar_materiales_lista_modal_ws if USE_MAT_WS else liquidar_materiales_lista_modal_parallel
            log.info(f"[BG MAT] Paralelo — {len(items_mat)} materiales "
                     f"({'WS work-stealing' if USE_MAT_WS else 'static'})")
            resultados_mat = _mat_fn(
                items_mat, max_pages=2, storage_state=storage_state,
                progress_cb=_progress_cb,
                should_cancel=lambda: bool(job.get("cancelado")))
            for i, res in enumerate(resultados_mat):
                idx_job = items_mat[i].get("idx_job", i) if i < len(items_mat) else i
                if idx_job < len(job["items_mat"]):
                    job["items_mat"][idx_job]["estado"] = "ok" if res.get("ok") else "error"
                    if not res.get("ok"):
                        job["items_mat"][idx_job]["error"] = res.get("error", "")[:100]
            resultados.extend(resultados_mat)

        # ── Verificación post-save MAT — R2+R3 ────────────────────────────────
        pm_ids_mat_ok = [
            r["pm_id"] for r in resultados
            if r.get("tipo") == "MAT" and r.get("ok") and r.get("pm_id")
        ]
        if pm_ids_mat_ok:
            try:
                _time_mod.sleep(2)
                dets_post = c.search_read(
                    "project.material.details",
                    [["project_material_id", "in", pm_ids_mat_ok],
                     ["create_uid", "=", SGS_UID_SERVICIO]],
                    ["id", "project_material_id", "amount", "create_date"],
                    limit=500,
                )
                conf_por_pm = {}
                for d in dets_post:
                    pmref  = d.get("project_material_id")
                    pm_key = pmref[0] if isinstance(pmref, list) else pmref
                    conf_por_pm.setdefault(pm_key, []).append(d)
                for r in resultados:
                    if r.get("tipo") != "MAT" or not r.get("ok") or not r.get("pm_id"):
                        continue
                    lineas = sorted(conf_por_pm.get(r["pm_id"], []),
                                    key=lambda x: x.get("create_date", ""), reverse=True)
                    if lineas:
                        ultima = lineas[0]
                        cant_guardada = float(ultima.get("amount") or 0)
                        confirmado = abs(cant_guardada - float(r.get("cantidad") or 0)) <= 0.01
                        r["verificacion_persistencia"] = {
                            "linea_id": ultima.get("id"),
                            "cantidad_guardada": cant_guardada,
                            "fecha": str(ultima.get("create_date", "")),
                            "confirmado": confirmado,
                        }
                        if not confirmado:
                            log.warning(f"[BG MAT] pm_id={r['pm_id']} post-save: "
                                        f"esperado={r.get('cantidad'):g} guardado={cant_guardada:g}")
                    else:
                        r["verificacion_persistencia"] = {
                            "confirmado": False,
                            "aviso": "No se encontro linea de detalle post-save",
                        }
                        log.warning(f"[BG MAT] pm_id={r['pm_id']} post-save: sin linea de detalle (uid de servicio)")
            except Exception as ex:
                log.warning(f"[BG MAT] Verificacion post-save fallo (no critico): {ex}")

        # ── Actualizar fechas + estado ────────────────────────────────────────
        s_bg = _sesiones.get(sid) or {}
        _fecha_iso_bg = s_bg.get("fecha_liquidacion_iso", "")
        try:
            fecha_hoy = datetime.strptime(_fecha_iso_bg, "%Y-%m-%d").strftime("%d-%m-%Y") if _fecha_iso_bg else datetime.now().strftime("%d-%m-%Y")
        except Exception:
            fecha_hoy = datetime.now().strftime("%d-%m-%Y")
        pdl_bg = str(s_bg.get("pdl_detectado", "") or "").strip()

        # Rastrear OK y FALLOS por PID para decidir cuáles nodos se cierran.
        # Un nodo solo transiciona a TERMINADO si TODOS sus ítems resultaron ok.
        # Razón: si liquidamos 2/3 materiales de un nodo, el nodo aún tiene trabajo
        # pendiente y marcarlo como terminado sería incorrecto.
        _pids_ok_count:     dict = {}
        _pids_fail_count:   dict = {}
        _pids_pro_code:     dict = {}

        for item, res in zip(items_mo, resultados[:len(items_mo)]):
            pid = item.get("pid")
            if not pid:
                continue
            _pids_pro_code.setdefault(pid, item.get("pro_code", ""))
            if res.get("ok"):
                _pids_ok_count[pid] = _pids_ok_count.get(pid, 0) + 1
            else:
                _pids_fail_count[pid] = _pids_fail_count.get(pid, 0) + 1

        for res in resultados[len(items_mo):]:
            pid = res.get("pid")
            if not pid:
                continue
            _pids_pro_code.setdefault(pid, res.get("pro_code", ""))
            if res.get("ok"):
                _pids_ok_count[pid] = _pids_ok_count.get(pid, 0) + 1
            else:
                _pids_fail_count[pid] = _pids_fail_count.get(pid, 0) + 1

        # Solo incluir PIDs donde ningún ítem falló
        pids_liquidados = {
            pid: _pids_pro_code.get(pid, "")
            for pid in _pids_ok_count
            if _pids_fail_count.get(pid, 0) == 0
        }

        # PIDs con fallos: registrar en log para el operador
        for pid in _pids_fail_count:
            if _pids_fail_count[pid] > 0:
                pro = _pids_pro_code.get(pid, str(pid))
                log.warning(
                    f"[BG FECHAS] pid={pid} ({pro}) tiene {_pids_fail_count[pid]} "
                    f"ítem(s) fallido(s) — nodo NO transiciona a TERMINADO"
                )

        # Persistir en sesión para que cierre_rapido pueda consultarlos
        if s_bg and pids_liquidados:
            s_bg["pids_liquidados"] = pids_liquidados

        if pids_liquidados:
            # PDL vigente por API — actualizar_fechas_proyecto lo combina con el
            # nuevo para que el PDL histórico del nodo nunca se reemplace.
            _pdl_api_map = {}
            if pdl_bg:
                try:
                    for _r in _sgs().search_read(
                            "project.dominion",
                            [["id", "in", list(pids_liquidados.keys())]],
                            ["id", "pdl"], limit=len(pids_liquidados) + 5):
                        _pdl_api_map[_r["id"]] = str(_r.get("pdl") or "").strip()
                except Exception as ex:
                    log.warning(f"[BG FECHAS] No se pudo leer PDL actual por API: {ex}")
            try:
                pw_f = PlaywrightClient(headless=False, slow_mo=200)
                pw_f.start(storage_state=storage_state)
                # storage_state garantizado por el gate — no se intenta login
                for pid_liq, pro_liq in pids_liquidados.items():
                    log.info(f"[BG FECHAS] pid={pid_liq} fecha={fecha_hoy} pdl={pdl_bg or '—'}")
                    try:
                        res_f = pw_f.actualizar_fechas_proyecto(
                            pid=pid_liq, fecha=fecha_hoy, pro_code=pro_liq,
                            pdl=pdl_bg,
                            estado_destino=CIERRE_RAPIDO_ESTADO,
                            pdl_actual_api=_pdl_api_map.get(pid_liq, ""),
                        )
                        log.info(f"[BG FECHAS] pid={pid_liq} ok={res_f.get('ok')} estado={res_f.get('estado_actualizado','—')}")
                    except Exception as ex:
                        log.warning(f"[BG FECHAS] Error pid={pid_liq}: {ex}")
                pw_f.close()
            except Exception as ex:
                log.warning(f"[BG FECHAS] Error general: {ex}")

    except Exception as ex:
        log.error(f"[BG LIQUIDAR] Error general: {ex}")
        job["estado"] = "error"
        job["error"]  = _safe_str(ex)
        return

    job["fase"] = "listo"

    # ── Debug report ─────────────────────────────────────────────────────────
    errores_detalle = []
    for r in resultados:
        if not r.get("ok"):
            errores_detalle.append({
                "tipo":       r.get("tipo", "?"),
                "codigo":     r.get("codigo", "?"),
                "pro_code":   r.get("pro_code", ""),
                "worker":     r.get("worker_id", "vis."),
                "intento":    r.get("intentos", 1),
                "error":      r.get("error", "")[:200],
                "clase":      r.get("error_tipo") or _clasificar_error(r.get("error", "")),
            })

    from collections import Counter
    clases = Counter(e["clase"] for e in errores_detalle)
    total  = len(resultados)
    n_ok   = sum(1 for r in resultados if r.get("ok"))

    debug = {
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total":          total,
        "ok":             n_ok,
        "fallidos":       total - n_ok,
        "reintentados":   0,
        "retry_ok":       0,
        "clases_error":   dict(clases),
        "errores_detalle": errores_detalle,
    }
    job["debug"] = debug

    s = _sesiones.get(sid)
    if s:
        s["resultados_liquidacion"] = resultados
        s["debug_liquidacion"]      = debug
    job["resultados"] = resultados
    job["estado"]     = "listo"

    # ── Guardar errores_<sid>.json para consulta rápida ──────────────────────
    try:
        import json as _json
        logs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
        os.makedirs(logs_dir, exist_ok=True)
        errores_path = os.path.join(logs_dir, f"errores_{sid}.json")
        with open(errores_path, "w", encoding="utf-8") as _f:
            _json.dump(debug, _f, ensure_ascii=False, indent=2)
        log.info(f"[BG LIQUIDAR] Errores guardados → logs/errores_{sid[:8]}.json")
    except Exception as _ex:
        log.warning(f"[BG LIQUIDAR] No se pudo guardar errores JSON: {_ex}")

    print(
        f"[BG LIQUIDAR] sid={sid[:8]} LISTO — "
        f"{n_ok}/{total} OK | fallidos={total - n_ok}",
        flush=True,
    )


@julian.route("/cancelar/<sid>", methods=["POST"])
def cancelar(sid):
    """Señal de cancelación — marca el job, espera a que el hilo active la señal y redirige."""
    job = _trabajos.get(sid)
    if job:
        job["cancelado"] = True
        job["estado"]    = "cancelado"
        log.info(f"[CANCELAR] sid={sid[:8]} — señal enviada")
    return redirect(url_for("julian.resultado", sid=sid))


@julian.route("/reintento/<sid>", methods=["POST"])
def reintento(sid):
    """
    Reintentar solo los ítems MAT que fallaron con errores retryables
    (nav_timeout, addline_timeout, edit_timeout, save_error).

    Flujo:
    1. Leer errores del último job.
    2. Identificar ítems retryables por código.
    3. Buscar pm_id y partner en checks_mat de sesión.
    4. Re-verificar balance fresco en SGS.
    5. Lanzar nuevo job de liquidación con solo esos ítems.
    """
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    if not sesion_activa():
        flash("El browser visible no tiene sesión activa en SGS. Inicia sesión primero.")
        return redirect(url_for("julian.resultado", sid=sid))

    job_ant = _trabajos.get(sid)
    if not job_ant or job_ant.get("estado") not in ("listo", "cancelado", "error"):
        flash("No hay liquidación finalizada para reintentar.")
        return redirect(url_for("julian.resultado", sid=sid))

    # ── Identificar ítems MAT fallidos retryables ─────────────────────────────
    RETRYABLE = {"nav_timeout", "addline_timeout", "edit_timeout", "save_error",
                 "generic_timeout", "login_timeout"}
    debug = job_ant.get("debug") or {}
    errores = debug.get("errores_detalle", [])
    fallidos_codigos = {
        e["codigo"] for e in errores
        if e.get("tipo") == "MAT" and e.get("clase") in RETRYABLE
    }

    if not fallidos_codigos:
        flash("No hay ítems MAT con errores retryables en la última liquidación.")
        return redirect(url_for("julian.resultado", sid=sid))

    # ── Buscar pm_id y partner en checks_mat ─────────────────────────────────
    checks_mat = s.get("checks_mat", [])
    candidatos = []
    for mc in checks_mat:
        codigo = mc.get("mat", {}).get("codigo", "")
        if codigo not in fallidos_codigos:
            continue
        pm_id   = mc.get("pm_id")
        partner = mc.get("partner_nombre", "")
        if not pm_id or not partner:
            log.warning(f"[RETRY] {codigo}: sin pm_id o partner — omitido en reintento")
            continue
        candidatos.append(mc)

    if not candidatos:
        flash("No se encontraron los materiales fallidos en la sesión actual.")
        return redirect(url_for("julian.resultado", sid=sid))

    # ── Re-verificar balance fresco en SGS ────────────────────────────────────
    pm_ids_retry = [mc["pm_id"] for mc in candidatos]
    try:
        from app import _sgs as _sgs_fn
        frescos_retry = _sgs_fn().search_read(
            "project.material",
            [["id", "in", pm_ids_retry]],
            ["id", "balance", "devuelto"],
            limit=len(pm_ids_retry) + 5,
        )
        balance_retry = {f["id"]: round(float(f.get("balance") or 0), 4)
                         for f in frescos_retry}
    except Exception as ex:
        flash(f"No se pudo verificar balance SGS para reintento: {ex}")
        return redirect(url_for("julian.resultado", sid=sid))

    items_mat_retry = []
    for mc in candidatos:
        pm_id  = mc["pm_id"]
        codigo = mc["mat"]["codigo"]
        bal    = balance_retry.get(pm_id, 0.0)
        if False and bal <= 0.001:
            log.info(f"[RETRY] {codigo} pm#{pm_id} balance={bal} — ya liquidado, omitiendo")
            continue
        items_mat_retry.append({
            "tipo":                    "MAT",
            "idx":                     0,
            "pm_id":                   pm_id,
            "pid":                     mc.get("pid"),
            "codigo":                  codigo,
            "codigo_sgs":              mc.get("codigo_sgs", codigo),
            "es_custodia":             mc.get("es_custodia", False),
            "cantidad":                bal,
            "excel_neto":              bal,
            "advance_precheck":        0,
            "balance_precheck":        bal,
            "pro_code":                mc.get("pro_code", ""),
            "partner_nombre":          mc.get("partner_nombre", ""),
            "bandeja_despacho":        mc.get("bandeja_despacho", ""),
            "pendiente_api_confirmado": True,
        })

    if items_mat_retry:
        try:
            items_mat_retry, bloqueados_retry = _validar_materiales_julian_seguro(
                _sgs(), items_mat_retry, contexto="RETRY MAT"
            )
            for b in bloqueados_retry:
                if not b.get("ok"):
                    flash(f"{b.get('codigo_sgs') or b.get('codigo')}: {b.get('error')}")
        except Exception as ex:
            flash(f"No se pudo validar despacho-devolucion SGS para reintento: {ex}")
            return redirect(url_for("julian.resultado", sid=sid))

    if not items_mat_retry:
        flash("Todos los ítems fallidos ya están liquidados en SGS (balance = 0).")
        return redirect(url_for("julian.resultado", sid=sid))

    # ── Obtener storage state ─────────────────────────────────────────────────
    with _cached_storage_state_lock:
        storage_state = _cached_storage_state
    if not storage_state:
        flash("Estado de sesión no disponible. Espera e intenta de nuevo.")
        return redirect(url_for("julian.resultado", sid=sid))

    # ── Crear nuevo job ────────────────────────────────────────────────────────
    job_items_mat = [
        {
            "codigo":      it["codigo"],
            "tipo":        "MAT",
            "codigo_sgs":  it.get("codigo_sgs", it["codigo"]),
            "es_custodia": it.get("es_custodia", False),
            "worker_id":   0,
            "estado":      "pendiente",
            "error":       None,
        }
        for it in items_mat_retry
    ]
    _trabajos[sid] = {
        "estado":       "corriendo",
        "items_mo":     [],
        "items_mat":    job_items_mat,
        "resultados":   None,
        "error":        None,
        "t_inicio":     _time_mod.time(),
        "es_reintento": True,
    }

    t = threading.Thread(
        target=_ejecutar_liquidacion_bg,
        args=(sid, [], items_mat_retry, storage_state),
        daemon=True,
    )
    t.start()
    log.info(f"[RETRY] sid={sid[:8]} — {len(items_mat_retry)} ítem(s) reintentando")
    flash(f"Reintentando {len(items_mat_retry)} material(es) fallido(s).")

    return redirect(url_for("julian.esperar", sid=sid))


@julian.route("/esperar/<sid>")
def esperar(sid):
    """Página de loading que se muestra mientras corre la liquidación en segundo plano."""
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    job = _trabajos.get(sid)
    if not job:
        flash("No hay trabajo en curso para esta sesion.")
        return redirect(url_for("julian.resumen", sid=sid))

    # Si ya terminó antes de que el usuario llegara aquí, redirigir directo
    if job["estado"] == "listo":
        return redirect(url_for("julian.resultado", sid=sid))
    if job["estado"] == "error":
        flash(f"Error en liquidacion: {job.get('error', '?')}")
        return redirect(url_for("julian.resumen", sid=sid))

    total = len(job["items_mo"]) + len(job["items_mat"])
    return render_template("julian/esperar_liquidacion.html", sid=sid, s=s,
                           job=job, total=total)


@julian.route("/progreso/<sid>")
def progreso(sid):
    """Devuelve JSON con el estado actual del trabajo para polling del frontend."""
    job = _trabajos.get(sid)
    if not job:
        return jsonify({"estado": "no_encontrado"})

    transcurrido = int(_time_mod.time() - job["t_inicio"])
    all_items = job["items_mo"] + job["items_mat"]
    total  = len(all_items)
    hechos = sum(1 for it in all_items if it["estado"] in ("ok", "error"))

    return jsonify({
        "estado":       job["estado"],
        "fase":         job.get("fase", "primera_pasada"),
        "error":        job.get("error"),
        "items_mo":     job["items_mo"],
        "items_mat":    job["items_mat"],
        "total":        total,
        "hechos":       hechos,
        "transcurrido": transcurrido,
    })


@julian.route("/resultado/<sid>")
def resultado(sid):
    """Muestra el resultado de liquidacion (GET tras redireccion desde esperar)."""
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    job = _trabajos.get(sid)
    cancelado = job and job.get("cancelado")

    # Si se canceló, leer resultados directamente del job (la sesión puede no tenerlos aún)
    resultados = s.get("resultados_liquidacion") or (job.get("resultados") if job else None)
    if not resultados:
        flash("Sin resultados de liquidacion.")
        return redirect(url_for("julian.resumen", sid=sid))

    debug = s.get("debug_liquidacion") or (job.get("debug") if job else None)
    return render_template("julian/resultado_liquidacion.html", sid=sid, s=s,
                           resultados=resultados, debug=debug, cancelado=cancelado)


@julian.route("/auditoria/<sid>")
def auditoria(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    resultados = s.get("resultados_liquidacion", [])
    if not resultados:
        flash("Sin resultados de liquidacion para auditar.")
        return redirect(url_for("julian.subir"))

    try:
        c = _sgs()
        for res in resultados:
            if not res.get("ok"):
                res["auditoria"] = {"estado": "no_ejecutado"}
                continue
            try:
                if res["tipo"] == "MO":
                    act_id = res.get("act_id")
                    if not act_id:
                        res["auditoria"] = {"estado": "sin_act_id"}
                        continue
                    r = c.search_read("project.activities",
                        [["id", "=", act_id]],
                        ["advance", "balance", "amount"], limit=1)
                    if r:
                        a = r[0]
                        coincide = abs(float(a["advance"]) - float(res["cantidad"])) < 0.01
                        res["auditoria"] = {
                            "advance": a["advance"], "balance": a["balance"],
                            "amount": a["amount"], "coincide": coincide,
                            "estado": "ok" if coincide else "descuadre",
                        }
                    else:
                        res["auditoria"] = {"estado": "no_encontrado"}

                elif res["tipo"] == "MAT":
                    pm_id = res.get("pm_id")
                    if not pm_id:
                        res["auditoria"] = {"estado": "sin_pm_id"}
                        continue
                    r = c.search_read("project.material",
                        [["id", "=", pm_id]],
                        ["advance", "balance", "amount"], limit=1)
                    if r:
                        a = r[0]
                        coincide = abs(float(a["balance"])) < 0.01
                        res["auditoria"] = {
                            "advance": a["advance"], "balance": a["balance"],
                            "amount": a["amount"], "coincide": coincide,
                            "estado": "ok" if coincide else "descuadre",
                        }
                    else:
                        res["auditoria"] = {"estado": "no_encontrado"}

                    # ── Auditoría de lider: leer quién quedó grabado en SGS ───
                    partner_esperado = res.get("partner_nombre", "")
                    try:
                        dets = c.search_read(
                            "project.material.details",
                            [["project_material_id", "=", pm_id]],
                            ["id", "amount", "partner_id"],
                            limit=100,
                        )
                        if dets:
                            # La línea de mayor ID es la que acabamos de crear
                            ultima = max(dets, key=lambda x: x.get("id", 0))
                            partner_raw = ultima.get("partner_id")
                            partner_liq = (partner_raw[1]
                                           if isinstance(partner_raw, list)
                                           else str(partner_raw or ""))
                            if partner_esperado and partner_liq:
                                # Comparar tolerante: apellido o contenido
                                ape_esp = partner_esperado.strip().split()[-1].lower()
                                ape_liq = str(partner_liq).strip().split()[-1].lower()
                                lider_ok = (
                                    ape_esp == ape_liq
                                    or partner_esperado.lower() in partner_liq.lower()
                                    or partner_liq.lower() in partner_esperado.lower()
                                )
                            elif not partner_esperado:
                                lider_ok = None  # no teníamos expectativa
                            else:
                                lider_ok = False  # esperábamos pero no hay partner
                            res["auditoria"]["lider_liquidado"] = partner_liq
                            res["auditoria"]["lider_esperado"]  = partner_esperado
                            res["auditoria"]["lider_ok"]        = lider_ok
                        else:
                            res["auditoria"]["lider_ok"] = None
                    except Exception as ex:
                        log.warning(f"[AUDITORIA] partner check pm#{pm_id}: {ex}")
                        res["auditoria"]["lider_ok"] = None

            except Exception as ex:
                res["auditoria"] = {"estado": "error", "msg": str(ex)}

    except Exception as ex:
        log.warning(f"[JULIAN AUDITORIA] Error: {ex}")
        flash(f"Error conectando a SGS para auditoría: {ex}")

    s["resultados_liquidacion"] = resultados
    debug = s.get("debug_liquidacion")
    return render_template("julian/resultado_liquidacion.html", sid=sid, s=s,
                           resultados=resultados, debug=debug)


@julian.route("/cierre_rapido/<sid>", methods=["GET", "POST"])
def cierre_rapido(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    fecha_raw = request.values.get("fecha", "")
    pdl_raw   = request.values.get("pdl", "")
    if str(pdl_raw or "").strip():
        s["pdl_detectado"] = str(pdl_raw or "").strip()

    if request.method == "POST":
        seleccion = [int(pid) for pid in request.form.getlist("pid") if str(pid).isdigit()]
        res = _aplicar_cierre_rapido_j(
            sid,
            pids_objetivo=seleccion or None,
            fecha_raw=fecha_raw,
            pdl_raw=pdl_raw,
        )
        if res.get("hechos"):
            flash(
                f"Cierre rapido aplicado a {res['hechos']} de {res['objetivos']} proyecto(s). "
                "Se revisaron fecha, PDL y estado."
            )
        elif res.get("objetivos") == 0 and not res.get("errores"):
            flash("No hay proyectos pendientes para corregir con esos filtros.")
        if res.get("errores"):
            flash("Errores: " + " | ".join(res["errores"][:3]))
        return redirect(url_for(
            "julian.cierre_rapido",
            sid=sid,
            fecha=(res.get("meta") or {}).get("fecha_input") or fecha_raw,
            pdl=(res.get("meta") or {}).get("pdl_esperado") or pdl_raw,
        ))

    try:
        rows, meta = _armar_panel_cierre_rapido_j(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw)
    except Exception as ex:
        log.error(f"[CIERRE_RAPIDO] Error armando panel sid={sid[:8]}: {ex}")
        flash(f"No se pudo consultar SGS: {ex}")
        rows, meta = [], _resolver_objetivos_cierre_j(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw)
        meta.update({"total": 0, "elegibles": 0, "pendientes": 0, "listos": 0, "referencia": 0})

    return render_template("julian/cierre_rapido.html", sid=sid, s=s, rows=rows, meta=meta)


@julian.route("/resumen/<sid>")
def resumen(sid):
    """Muestra la verificacion desde sesion sin re-consultar SGS."""
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    # Auto-verificar MO si aún no se ha hecho (primera vez que llega aquí tras
    # el flujo balance_sgs, que solo verifica MAT).
    if not s.get("checks_mo") and s.get("rows_mo"):
        try:
            c = _sgs()
            nuevos_checks_mo = []
            for row in s["rows_mo"]:
                try:
                    ck = verificar_fila_mo(c, row)
                except Exception as ex:
                    ck = {"row": row, "ok": False, "errores": [str(ex)], "advertencias": []}
                nuevos_checks_mo.append(ck)
            s["checks_mo"] = nuevos_checks_mo
            if not s.get("verificado_at"):
                s["verificado_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            log.info(f"[RESUMEN] MO auto-verificado — {len(nuevos_checks_mo)} filas")
        except Exception as ex:
            log.warning(f"[RESUMEN] Auto-verificar MO falló: {ex}")
            flash(f"Advertencia: no se pudo verificar MO contra SGS ({ex})")

    # Filtrar por nodos aprobados en balance (si aplica)
    nodos_en_liq = set(str(n) for n in s.get("nodos_en_liquidacion", []))

    all_checks_mo  = s.get("checks_mo",  [])
    all_checks_mat = s.get("checks_mat", [])

    if nodos_en_liq:
        # Preservar índice REAL en la lista completa como "_real_idx"
        # para que el form de liquidar use el índice correcto
        checks_mo  = []
        for i, c in enumerate(all_checks_mo):
            if str(c.get("row", {}).get("pro", "")) in nodos_en_liq:
                c["_real_idx"] = i
                checks_mo.append(c)
        checks_mat = []
        for i, c in enumerate(all_checks_mat):
            if str(c.get("mat", {}).get("nodo", "")) in nodos_en_liq:
                c["_real_idx"] = i
                checks_mat.append(c)
        # Nodos pendientes = los que tienen pid pero aún no están en liquidación
        nodos_pendientes = sorted(set(
            str(c.get("mat", {}).get("nodo", ""))
            for c in all_checks_mat
            if str(c.get("mat", {}).get("nodo", "")) not in nodos_en_liq
               and c.get("pid")
        ), key=lambda x: int(x) if x.isdigit() else 0)
    else:
        # Sin filtro: asignar índice real directamente
        for i, c in enumerate(all_checks_mo):  c["_real_idx"] = i
        for i, c in enumerate(all_checks_mat): c["_real_idx"] = i
        checks_mo  = all_checks_mo
        checks_mat = all_checks_mat
        nodos_pendientes = []

    def _resumen(checks):
        ok     = sum(1 for c in checks if c.get("ok"))
        error  = sum(1 for c in checks if not c.get("ok"))
        ya_liq = sum(1 for c in checks if c.get("estado") == "ya_liquidado" or c.get("ya_liquidada"))
        return {"ok": ok, "error": error, "ya_liquidado": ya_liq, "total": len(checks)}

    return render_template("julian/verificacion.html",
        sid=sid, s=s,
        checks_mo=checks_mo,
        checks_mat=checks_mat,
        resumen_mo=_resumen(checks_mo),
        resumen_mat=_resumen(checks_mat),
        mo_lider_grupos=_grupos_lideres_mo(checks_mo),
        nodos_en_liquidacion=sorted(nodos_en_liq, key=lambda x: int(x) if x.isdigit() else 0),
        nodos_pendientes=nodos_pendientes,
        **_ctx_verificacion_pdl_j(s),
    )


def _armar_nodos_data_j(s: dict, c) -> list:
    """
    Construye y devuelve nodos_data a partir de s["checks_mat"] consultando SGS.
    También enriquece s["checks_mat"] con datos SGS (pm_id, sgs_neto, advance_real…)
    y actualiza s["reversiones_ticket"] y s["balance_lider_por_nodo"].
    Parámetros:
        s  — sesión activa del usuario (dict)
        c  — cliente SGSReadOnlyClient ya autenticado
    """
    import re as _re_nd
    import copy as _copy_nd

    checks_mat = s.get("checks_mat", [])

    pid_por_nodo: dict = {}
    for ck in checks_mat:
        nodo = str(ck.get("mat", {}).get("nodo", ""))
        pid  = ck.get("pid")
        if pid and nodo not in pid_por_nodo:
            pid_por_nodo[nodo] = pid

    # Info de proyectos
    pids_set = set(pid_por_nodo.values())
    pid_info: dict = {}
    try:
        proyectos = c.search_read(
            "project.dominion",
            [["id", "in", list(pids_set)]],
            ["id", "code", "name"], limit=len(pids_set) + 5,
        )
        for p in proyectos:
            pid_info[p["id"]] = {"pro_code": p.get("code", ""), "pnombre": p.get("name", "")}
    except Exception as ex:
        log.warning(f"[ARMAR NODOS] pid_info: {ex}")

    # checks_mat indexado por (nodo, codigo) — incluye variante C
    checks_idx: dict = {}
    checks_idx_por_cod: dict = {}
    for ck in checks_mat:
        nodo = str(ck.get("mat", {}).get("nodo", ""))
        cod  = ck.get("mat", {}).get("codigo", "")
        checks_idx[(nodo, cod)] = ck
        alt = cod[:-1] if cod.upper().endswith("C") else cod + "C"
        if (nodo, alt) not in checks_idx:
            checks_idx[(nodo, alt)] = ck
        if cod not in checks_idx_por_cod:
            checks_idx_por_cod[cod] = ck
        if alt not in checks_idx_por_cod:
            checks_idx_por_cod[alt] = ck

    nodos_data = []

    # Reiniciar la lista de reversiones por cada barrido
    s["reversiones_ticket"] = []

    for nodo in sorted(pid_por_nodo, key=lambda x: int(x) if x.isdigit() else 0):
        pid = pid_por_nodo[nodo]

        # ── project.material del proyecto ────────────────────────────────────
        try:
            pm_all = c.search_read(
                "project.material",
                [["project_id", "=", pid]],
                ["id", "product_code", "despachado", "devuelto", "advance", "balance"],
                limit=500,
            )
        except Exception as ex:
            log.warning(f"[ARMAR NODOS] pm_all pid={pid}: {ex}")
            pm_all = []

        # ── Advance real y Auditoría de Líderes por material ──
        advance_by_pm: dict = {}
        liq_partners_by_pm: dict = {}
        try:
            dets = c.search_read(
                "project.material.details",
                [["project_material_id.project_id", "=", pid]],
                ["project_material_id", "amount", "partner_id", "picking_id"],
                limit=2000,
            )
            for d in dets:
                amt = float(d.get("amount") or 0)
                if amt <= 0:
                    continue
                pm_ref = d.get("project_material_id")
                pmid   = pm_ref[0] if isinstance(pm_ref, list) else pm_ref
                advance_by_pm[pmid] = advance_by_pm.get(pmid, 0.0) + amt

                picking_ref  = d.get("picking_id")
                picking_name = picking_ref[1] if isinstance(picking_ref, list) else str(picking_ref or "")
                if "CONS" in picking_name.upper():
                    p_ref = d.get("partner_id")
                    if isinstance(p_ref, list):
                        p_full = p_ref[1]
                    else:
                        p_full = "Desconocido"
                    if pmid not in liq_partners_by_pm:
                        liq_partners_by_pm[pmid] = set()
                    liq_partners_by_pm[pmid].add(p_full)
            log.info(f"[ARMAR NODOS] advance_by_pm pid={pid}: {len(advance_by_pm)} materiales")
        except Exception as ex:
            log.warning(f"[ARMAR NODOS] details advance pid={pid}: {ex}")

        # ── Bandejas desde project.tray ───────────────────────────────────────
        trays = _fetch_trays_validas(c, [["project_id", "=", pid]], limit=200)

        all_mat_ids = []
        for tray in trays:
            all_mat_ids.extend(tray.get("project_tray_material_ids") or [])

        tray_mats: dict = {}
        if all_mat_ids:
            try:
                mats = c.read(
                    "project.tray.material",
                    all_mat_ids,
                    ["id", "product_id", "deliver_quantity", "project_tray_id"],
                )
                for m in mats:
                    tray_mats[m["id"]] = m
            except Exception as ex:
                log.warning(f"[ARMAR NODOS] project.tray.material: {ex}")

        def _cod_sap_nd(product_field):
            if not product_field:
                return ""
            txt = product_field[1] if isinstance(product_field, list) else str(product_field)
            m = _re_nd.match(r"\[([^\]/]+)(?:/([^\]]+))?\]", txt)
            if m:
                return (m.group(2) or m.group(1)).strip()
            return ""

        band_desp_por_cod: dict  = {}
        band_devol_por_cod: dict = {}
        lider_nodo = ""

        for tray in trays:
            es_dev  = str(tray.get("type") or "").lower() == "devolution"
            ban_nom = tray.get("name", "")
            partner_raw = tray.get("partner_id")
            partner_nom = partner_raw[1] if isinstance(partner_raw, list) else ""
            date_raw    = str(tray.get("date") or "")[:10]

            if not es_dev and not lider_nodo and partner_nom:
                lider_nodo = partner_nom

            for mat_id in tray.get("project_tray_material_ids") or []:
                mat = tray_mats.get(mat_id)
                if not mat:
                    continue
                cod = _cod_sap_nd(mat.get("product_id"))
                if not cod:
                    continue
                qty_ban = float(mat.get("deliver_quantity") or 0)
                lst_d = band_devol_por_cod if es_dev else band_desp_por_cod
                lst = lst_d.setdefault(cod, [])
                existing_ban = next((b for b in lst if b["name"] == ban_nom), None)
                if existing_ban:
                    existing_ban["qty"] = existing_ban.get("qty", 0.0) + qty_ban
                else:
                    lst.append({"name": ban_nom, "partner": partner_nom,
                                "date": date_raw, "qty": qty_ban})

        devuelto_from_trays: dict = {}
        for tray in trays:
            if str(tray.get("type") or "").lower() != "devolution":
                continue
            for mat_id in tray.get("project_tray_material_ids") or []:
                mat = tray_mats.get(mat_id)
                if not mat:
                    continue
                cod_dev = _cod_sap_nd(mat.get("product_id"))
                qty_dev = float(mat.get("deliver_quantity") or 0)
                if cod_dev and qty_dev > 0:
                    devuelto_from_trays[cod_dev] = (
                        devuelto_from_trays.get(cod_dev, 0.0) + qty_dev
                    )

        ref_order: list = []
        mayor_len = 0
        for tray in trays:
            if str(tray.get("type") or "").lower() == "devolution":
                continue
            mat_ids = tray.get("project_tray_material_ids") or []
            if len(mat_ids) > mayor_len:
                mayor_len = len(mat_ids)
                ref_order = []
                for mat_id in mat_ids:
                    mat = tray_mats.get(mat_id)
                    if not mat:
                        continue
                    cod_ref = _cod_sap_nd(mat.get("product_id"))
                    if cod_ref and cod_ref not in ref_order:
                        ref_order.append(cod_ref)
        ref_pos = {cod: i for i, cod in enumerate(ref_order)}

        lider_qty_por_cod: dict = {}
        lider_por_cod: dict     = {}
        for tray in trays:
            if str(tray.get("type") or "").lower() == "devolution":
                continue
            pr_raw   = tray.get("partner_id")
            pr_nom   = pr_raw[1] if isinstance(pr_raw, list) else ""
            ban_nom_l = tray.get("name", "")
            if not pr_nom:
                continue
            for mat_id in tray.get("project_tray_material_ids") or []:
                mat = tray_mats.get(mat_id)
                if not mat:
                    continue
                c_sap  = _cod_sap_nd(mat.get("product_id"))
                qty_l  = float(mat.get("deliver_quantity") or 0)
                if not c_sap or qty_l <= 0:
                    continue
                if c_sap not in lider_por_cod:
                    lider_por_cod[c_sap] = pr_nom
                lst_l = lider_qty_por_cod.setdefault(c_sap, [])
                existing_l = next(
                    (e for e in lst_l if e["partner"] == pr_nom and e["bandeja"] == ban_nom_l),
                    None,
                )
                if existing_l:
                    existing_l["qty"] += qty_l
                else:
                    lst_l.append({"partner": pr_nom, "qty": qty_l, "bandeja": ban_nom_l})

        # ── Deduplicar variantes custodia ────────────────────────────────────
        pm_por_base: dict = {}
        for pm in pm_all:
            c_pm   = str(pm.get("product_code") or "").strip()
            base_c = c_pm[:-1] if c_pm.upper().endswith("C") else c_pm
            existing = pm_por_base.get(base_c)
            if existing is None:
                pm_por_base[base_c] = pm
                continue
            c_new = c_pm.upper().endswith("C")
            c_old = str(existing.get("product_code") or "").upper().endswith("C")
            if c_new and not c_old:
                log.info(
                    f"[ARMAR NODOS] pid={pid} custodia dedup: "
                    f"'{c_pm}' (C-variant) > '{existing.get('product_code')}' — usando C"
                )
                pm_por_base[base_c] = pm
                continue
            if c_old and not c_new:
                continue
            bal_new = float(pm.get("balance") or 0)
            bal_old = float(existing.get("balance") or 0)
            if bal_new > 0.001 and bal_old <= 0.001:
                pm_por_base[base_c] = pm
            elif bal_old > 0.001 and bal_new <= 0.001:
                pass
            elif float(pm.get("despachado") or 0) > float(existing.get("despachado") or 0):
                pm_por_base[base_c] = pm
            elif pm.get("id", 0) > existing.get("id", 0):
                pm_por_base[base_c] = pm
        pm_all_dedup = list(pm_por_base.values())

        # ── Construir filas ───────────────────────────────────────────────────
        materiales = []
        for pm in pm_all_dedup:
            cod  = str(pm.get("product_code") or "").strip()
            desp = float(pm.get("despachado") or 0)

            devto_pm    = float(pm.get("devuelto") or 0)
            alt_cod = cod[:-1] if cod.upper().endswith("C") else cod + "C"
            devto_trays = (devuelto_from_trays.get(cod, 0.0)
                           or devuelto_from_trays.get(alt_cod, 0.0))
            devto = devto_trays if devto_trays > 0 else devto_pm
            if devto_trays > 0 and abs(devto_trays - devto_pm) > 0.001:
                log.info(
                    f"[ARMAR NODOS] cod={cod} devuelto_pm={devto_pm} "
                    f"devuelto_bandejas={devto_trays} → usando bandejas"
                )

            neto    = round(desp - devto, 4)
            adv_pm  = advance_by_pm.get(pm["id"], 0.0)

            ck = checks_idx.get((nodo, cod))

            if not ck and (neto > 0 or adv_pm > 0):
                ck_otro_nodo = checks_idx_por_cod.get(cod) or checks_idx_por_cod.get(
                    cod[:-1] if cod.upper().endswith("C") else cod + "C"
                )
                if ck_otro_nodo:
                    ck = ck_otro_nodo
                    checks_idx[(nodo, cod)] = ck
                    log.info(
                        f"[ARMAR NODOS] cod={cod} nodo_sgs={nodo} → "
                        f"reutilizando check de nodo {ck.get('mat', {}).get('nodo', '?')} "
                        f"(evitando phantom duplicado)"
                    )
                else:
                    codigo_excel = cod[:-1] if cod.upper().endswith("C") else cod
                    nuevo_ck = {
                        "mat": {
                            "nodo": nodo,
                            "codigo": codigo_excel,
                            "descripcion": "",
                            "sgs_neto_excel": 0,
                        },
                        "pid": pid,
                        "ok": False,
                        "estado": "descuadre",
                        "_pendiente": True,
                        "errores": [],
                        "advertencias": ["Material ausente en LCL original. Interceptado forzosamente desde Odoo."],
                        "agregado_sgs": True,
                    }
                    checks_mat.append(nuevo_ck)
                    checks_idx[(nodo, cod)] = nuevo_ck
                    checks_idx_por_cod[cod] = nuevo_ck
                    checks_idx_por_cod[codigo_excel] = nuevo_ck
                    ck = nuevo_ck

            excel_neto  = float(ck["mat"]["sgs_neto_excel"]) if ck else None
            descripcion = ck["mat"].get("descripcion", "") if ck else ""
            estado_ck   = ck.get("estado", "no_en_excel") if ck else "no_en_excel"

            doble_ok = (
                ck is not None
                and not ck.get("agregado_sgs")
                and excel_neto is not None
                and abs(neto - excel_neto) < 0.001
                and estado_ck != "descuadre"
            )

            ya_liq_pm        = (adv_pm >= neto > 0)
            sin_pendiente_pm = (neto <= 0 and adv_pm == 0)

            lideres_cod = (lider_qty_por_cod.get(cod)
                           or lider_qty_por_cod.get(alt_cod) or [])
            partner_mat = (lideres_cod[0]["partner"] if lideres_cod
                           else lider_por_cod.get(cod) or lider_por_cod.get(alt_cod) or "")
            multi_lider         = len(lideres_cod) > 1
            multi_lider_ambiguo = multi_lider and devto > 0

            bd_desp = band_desp_por_cod.get(cod) or band_desp_por_cod.get(alt_cod, [])
            bd_devl = band_devol_por_cod.get(cod) or band_devol_por_cod.get(alt_cod, [])

            if ck:
                codigo_excel = ck["mat"]["codigo"]
            elif cod.upper().endswith("C"):
                codigo_excel = cod[:-1]
            else:
                codigo_excel = cod
            es_custodia = cod.upper().endswith("C")

            liq_p  = list(liq_partners_by_pm.get(pm["id"], set()))
            desp_p = set(b.get("partner") for b in bd_desp if b.get("partner"))
            if not desp_p and partner_mat:
                desp_p.add(partner_mat)

            nombres_malos = [l for l in liq_p if l not in desp_p]

            if len(nombres_malos) > 0:
                lider_n  = " y ".join(nombres_malos) if nombres_malos else "Desconocido"
                lider_c  = partner_mat or "Desconocido"
                b_name   = ", ".join([b["name"] for b in bd_desp if b.get("name")]) or "ninguna bandeja"
                motivo   = (
                    f"se liquidaron a los lideres incorrectos este material se liquido a {lider_n} "
                    f"y debia liquidarse a {lider_c} segun {b_name}"
                )
                pro_real = pid_info.get(pid, {}).get("pro_code") or str(nodo)
                s["reversiones_ticket"].append({
                    "nodo":        pro_real,
                    "codigo":      cod,
                    "descripcion": str(descripcion),
                    "cantidad":    float(adv_pm),
                    "motivo":      motivo,
                })

            materiales.append({
                "pm_id":               pm["id"],
                "codigo":              cod,
                "codigo_excel":        codigo_excel,
                "es_custodia":         es_custodia,
                "descripcion":         descripcion,
                "band_despacho":       bd_desp,
                "band_devolucion":     bd_devl,
                "despachado":          desp,
                "devuelto":            devto,
                "neto":                neto,
                "excel_neto":          excel_neto,
                "advance_real":        adv_pm,
                "ya_liquidado":        ya_liq_pm,
                "sin_pendiente":       sin_pendiente_pm,
                "tiene_devolucion":    devto > 0 or bool(bd_devl),
                "descuadre_lideres":   len(nombres_malos) > 0,
                "nombres_malos":       nombres_malos,
                "liq_partners":        liq_p,
                "descuadre":           estado_ck == "descuadre",
                "en_excel":            ck is not None,
                "agregado_sgs":        ck.get("agregado_sgs", False) if ck else False,
                "doble_ok":            doble_ok,
                "partner_nombre":      partner_mat,
                "lideres_mat":         lideres_cod,
                "multi_lider":         multi_lider,
                "multi_lider_ambiguo": multi_lider_ambiguo,
            })

            # ── Enriquecer checks_mat con datos SGS ──────────────────────────
            if ck:
                adv_sgs = advance_by_pm.get(pm["id"], 0.0)
                ck["pm_id"]      = pm["id"]
                ck["codigo_sgs"] = cod
                ck["es_custodia"] = es_custodia
                ck["sgs_neto"]   = neto
                ck["advance_real"] = adv_sgs
                if not ck.get("partner_nombre"):
                    ck["partner_nombre"] = partner_mat
                if not ck.get("bandeja_despacho") and bd_desp:
                    ck["bandeja_despacho"] = ", ".join(
                        b["name"] for b in bd_desp if b.get("name")
                    )
                if neto <= 0:
                    ck["estado"] = "sin_mov" if adv_sgs == 0 else "ya_liquidado"
                    ck["ok"]     = True
                    ck["errores"] = []
                    if devto > 0:
                        ck.setdefault("advertencias", []).append(
                            f"Sin pendiente SGS: {desp:g} despachado - {devto:g} devuelto = {neto:g}."
                        )
                elif adv_sgs >= neto:
                    ck["estado"] = "ya_liquidado"
                    ck["ok"]     = True
                elif adv_sgs > 0:
                    ck["estado"] = "avance_parcial"
                    ck["ok"]     = True
                else:
                    ck["estado"] = "ok"
                    ck["ok"]     = True
                pro_code_nodo = pid_info.get(pid, {}).get("pro_code", "")
                if pro_code_nodo and not ck.get("pro_code"):
                    ck["pro_code"] = pro_code_nodo
                if not ck.get("pid"):
                    ck["pid"] = pid

                if multi_lider and not multi_lider_ambiguo and lideres_cod:
                    ck["partner_nombre"] = lideres_cod[0]["partner"]
                    ck["sgs_neto"]       = lideres_cod[0]["qty"]
                    ck["_bandeja_split"] = lideres_cod[0].get("bandeja", "")
                    for _extra in lideres_cod[1:]:
                        _ck2 = _copy_nd.deepcopy(ck)
                        _ck2["partner_nombre"] = _extra["partner"]
                        _ck2["sgs_neto"]       = _extra["qty"]
                        _ck2["_bandeja_split"] = _extra.get("bandeja", "")
                        _ck2["_split_lider"]   = True
                        checks_mat.append(_ck2)
                elif multi_lider_ambiguo:
                    ck["_multi_lider_ambiguo"] = True

        # Reordenar materiales
        n_total = len(materiales)
        materiales.sort(key=lambda m: ref_pos.get(m["codigo"], n_total))

        info    = pid_info.get(pid, {})
        n_ok    = sum(1 for m in materiales if m["doble_ok"])
        n_desc  = sum(1 for m in materiales if m["descuadre"])
        n_devol = sum(1 for m in materiales if m["tiene_devolucion"])
        n_liq   = sum(1 for m in materiales if m["ya_liquidado"])
        n_en_excel = sum(1 for m in materiales if m["en_excel"])
        nodo_ok    = (n_desc == 0 and n_ok == n_en_excel)
        todo_liq   = (n_en_excel > 0 and n_liq == n_en_excel)

        nodos_data.append({
            "nodo":           nodo,
            "pid":            pid,
            "pro_code":       info.get("pro_code", ""),
            "pnombre":        info.get("pnombre", ""),
            "materiales":     materiales,
            "n_ok":           n_ok,
            "n_descuadre":    n_desc,
            "n_devolucion":   n_devol,
            "n_liquidado":    n_liq,
            "nodo_ok":        nodo_ok,
            "todo_liquidado": todo_liq,
            "lider":          lider_nodo,
        })

    s["balance_lider_por_nodo"] = {nd["nodo"]: nd["lider"] for nd in nodos_data if nd.get("lider")}
    return nodos_data


@julian.route("/balance_sgs_directo/<sid>")
def balance_sgs_directo(sid):
    """
    Acceso directo al Balance SGS desde la página de confirmación,
    sin necesidad de haber corrido la verificación completa primero.
    Aplica el LCL detectado y resuelve los pids directamente.
    """
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    # Aplicar LCL detectado si aún no se aplicó
    lcl = s.get("lcl_usado") or s.get("lcl_detectado", "")
    if lcl and not s.get("lcl_usado"):
        for row in s["rows_mo"]:  row["_odm"] = lcl
        for row in s["rows_mat"]: row["_odm"] = lcl
        s["lcl_usado"] = lcl

    # Si checks_mat ya está poblado, ir directo al balance normal
    if s.get("checks_mat"):
        return redirect(url_for("julian.balance_sgs", sid=sid))

    # Si no hay materiales en el Excel, no hay balance de materiales que mostrar
    if not s.get("rows_mat"):
        flash("Este archivo no tiene materiales — el balance por bandeja no aplica. "
              "Usa 'Verificar MO' para continuar.")
        return redirect(url_for("julian.confirmar", sid=sid))

    # Resolver pids por nodo usando solo rows_mat
    try:
        c = _sgs()
    except Exception as ex:
        flash(f"Error conectando a SGS: {ex}")
        return redirect(url_for("julian.confirmar", sid=sid))

    nodos_unicos = {}
    for mat in s["rows_mat"]:
        nodo = mat["nodo"]
        if nodo not in nodos_unicos:
            pid = _resolver_pid_nodo(c, nodo, s["rows_mo"], [], mat.get("_odm", lcl))
            nodos_unicos[nodo] = pid

    # Construir checks_mat mínimo (solo pid, sin verificación de cantidades)
    checks_mat = []
    for mat in s["rows_mat"]:
        pid = nodos_unicos.get(mat["nodo"])
        checks_mat.append({
            "mat":    mat,
            "pid":    pid,
            "ok":     False,
            "estado": "pendiente",
            "errores": [],
            "advertencias": [],
        })
    s["checks_mat"] = checks_mat

    return redirect(url_for("julian.balance_sgs", sid=sid))


@julian.route("/balance_sgs/<sid>", methods=["GET", "POST"])
def balance_sgs(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    checks_mat = s.get("checks_mat", [])

    # ── POST: el usuario aprobó uno o más nodos ──────────────────────────────
    if request.method == "POST":
        # ── PDL gate para proyectos programados ──────────────────────────────
        lcl_post = str(s.get("lcl_usado") or s.get("lcl_detectado", "") or "")
        es_prog_post = bool(re.match(r"6300\d{4,}", lcl_post))
        pdl_input = str(request.form.get("pdl_input", "") or "").strip()
        if pdl_input:
            s["pdl_detectado"] = pdl_input
        pdl_ok = str(s.get("pdl_detectado", "") or "").strip()
        if es_prog_post and not pdl_ok:
            flash("⚠ Proyecto programado (6300…): el PDL es obligatorio. Ingrésalo antes de aprobar.")
            return redirect(url_for("julian.balance_sgs", sid=sid))

        nodos_aprobados = set(request.form.getlist("aprobado_nodo"))
        n_aprobados = 0

        # Reconstruir lider por nodo desde nodos_data si está en sesión
        lider_por_nodo = s.get("balance_lider_por_nodo", {})

        for ck in checks_mat:
            nodo = str(ck.get("mat", {}).get("nodo", ""))
            if nodo not in nodos_aprobados:
                continue
            # Aceptar tanto descuadre como pendiente (acceso directo)
            if ck.get("estado") not in ("descuadre", "pendiente"):
                continue

            sgs_neto     = float(ck.get("sgs_neto") or
                              ck.get("mat", {}).get("sgs_neto_excel") or 0)
            advance_real = float(ck.get("advance_real") or 0)

            ck["ok"]      = True
            ck["errores"] = []
            ck["advertencias"] = ck.get("advertencias", []) + [
                "Balance aprobado manualmente desde vista SGS."
            ]
            # Guardar lider si lo tenemos
            if nodo in lider_por_nodo and not ck.get("partner_nombre"):
                ck["partner_nombre"] = lider_por_nodo[nodo]

            if sgs_neto <= 0:
                ck["estado"] = "sin_mov" if advance_real == 0 else "avance_parcial"
            elif advance_real >= sgs_neto:
                ck["estado"] = "ya_liquidado"
            elif advance_real > 0:
                ck["estado"] = "avance_parcial"
            else:
                ck["estado"] = "ok"

            n_aprobados += 1

        s["checks_mat"] = checks_mat
        s["balance_aprobado_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        # Acumular nodos aprobados (puede haber varias rondas)
        ya_aprobados = set(s.get("nodos_en_liquidacion", []))
        ya_aprobados.update(nodos_aprobados)
        s["nodos_en_liquidacion"] = list(ya_aprobados)

        flash(f"Nodo(s) {', '.join(sorted(nodos_aprobados))} listos para liquidar.")
        return redirect(url_for("julian.resumen", sid=sid))

    # ── GET: construir tabla por nodo ─────────────────────────────────────────
    # ── PDL / Programado ──────────────────────────────────────────────────────
    lcl_activo = str(s.get("lcl_usado") or s.get("lcl_detectado", "") or "")
    es_programado = bool(re.match(r"6300\d{4,}", lcl_activo))
    pdl_actual = str(s.get("pdl_detectado", "") or "").strip()

    # Excluir nodos que ya están en proceso de liquidación en esta sesión
    ya_en_liq = set(str(n) for n in s.get("nodos_en_liquidacion", []))

    pid_por_nodo = {}
    for ck in checks_mat:
        nodo = str(ck.get("mat", {}).get("nodo", ""))
        pid  = ck.get("pid")
        if pid and nodo not in pid_por_nodo:
            pid_por_nodo[nodo] = pid

    if not pid_por_nodo:
        flash("No hay proyectos resueltos. Verifica primero el LCL.")
        return redirect(url_for("julian.confirmar", sid=sid))

    try:
        c = _sgs()
    except Exception as ex:
        flash(f"Error conectando a SGS: {ex}")
        return redirect(url_for("julian.confirmar", sid=sid))

    # ── MO: auto-verificar si aún no se ha hecho ─────────────────────────────
    if not s.get("checks_mo") and s.get("rows_mo"):
        try:
            nuevos_checks_mo = []
            for row in s["rows_mo"]:
                try:
                    ck = verificar_fila_mo(c, row)
                except Exception as ex_mo:
                    ck = {"row": row, "ok": False, "errores": [str(ex_mo)], "advertencias": []}
                nuevos_checks_mo.append(ck)
            s["checks_mo"] = nuevos_checks_mo
            if not s.get("verificado_at"):
                s["verificado_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            log.info(f"[BALANCE SGS] MO auto-verificado — {len(nuevos_checks_mo)} filas")
        except Exception as ex_mo:
            log.warning(f"[BALANCE SGS] Auto-verificar MO falló: {ex_mo}")

    checks_mo = s.get("checks_mo", [])

    def _resumen_mo(checks):
        ok     = sum(1 for c2 in checks if c2.get("ok"))
        error  = sum(1 for c2 in checks if not c2.get("ok"))
        ya_liq = sum(1 for c2 in checks if c2.get("ya_liquidada"))
        return {"ok": ok, "error": error, "ya_liquidado": ya_liq, "total": len(checks)}

    # ── Construir nodos_data vía helper compartido ───────────────────────────
    nodos_data = _armar_nodos_data_j(s, c)

    # Contar nodos totales con pid para mostrar progreso
    total_nodos_con_pid = len(set(
        str(ck.get("mat", {}).get("nodo", ""))
        for ck in checks_mat if ck.get("pid")
    ))
    nodos_ya_aprobados = sorted(ya_en_liq, key=lambda x: int(x) if x.isdigit() else 0)

    return render_template("julian/balance_sgs.html", sid=sid, s=s,
                           nodos_data=nodos_data,
                           nodos_ya_aprobados=nodos_ya_aprobados,
                           total_nodos=total_nodos_con_pid,
                           checks_mo=checks_mo,
                           resumen_mo=_resumen_mo(checks_mo),
                           mo_lider_grupos=_grupos_lideres_mo(checks_mo),
                           es_programado=es_programado,
                           pdl_actual=pdl_actual,
                           pdl_requerido=(es_programado and not pdl_actual),
                           )


@julian.route("/debug_mat/<sid>/<nodo>")
def debug_mat(sid, nodo):
    from flask import jsonify
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"error": "sesion no encontrada"})
    try:
        c = _sgs()
    except Exception as ex:
        return jsonify({"error": str(ex)})

    checks_mat = s.get("checks_mat", [])
    pid = None
    for ck in checks_mat:
        if str(ck.get("mat", {}).get("nodo", "")) == str(nodo) and ck.get("pid"):
            pid = ck["pid"]
            break

    if not pid:
        return jsonify({"error": f"nodo {nodo} sin pid"})

    # 1. project.tray para el proyecto
    trays = []
    try:
        trays = c.search_read("project.tray",
            [["project_id", "=", pid]],
            ["id","name","type","partner_id","date","project_tray_material_ids"],
            limit=20)
    except Exception as ex:
        trays = [{"error": str(ex)}]

    # 2. Materiales de las bandejas
    mat_ids = []
    for t in trays:
        if isinstance(t.get("project_tray_material_ids"), list):
            mat_ids.extend(t["project_tray_material_ids"])
    tray_mats = []
    if mat_ids:
        try:
            tray_mats = c.read("project.tray.material",
                mat_ids[:30],
                ["id","product_id","deliver_quantity","project_tray_id"])
        except Exception as ex:
            tray_mats = [{"error": str(ex)}]

    return jsonify({
        "pid": pid,
        "nodo": nodo,
        "trays": trays[:5],
        "tray_mat_sample": tray_mats[:10],
    })


# ─────────────────────────────────────────────────────────────────────────────
# exportar_sgs — Excel de datos SGS puros (sin SIP). Solo lectura.
# ─────────────────────────────────────────────────────────────────────────────

@julian.route("/exportar_sgs/<sid>")
def exportar_sgs(sid):
    """
    Descarga un .xlsx con datos SGS puros: sin SIPWEBORA, sin liquidación.
    Hojas: SGS_BALANCE · CRUCE_SGS · MO_SGS
    """
    s = _sesiones.get(sid)
    if not s:
        return redirect(url_for("julian.subir"))

    checks_mat = s.get("checks_mat", [])
    if not checks_mat:
        flash("No hay materiales verificados. Ejecuta el balance SGS primero.")
        return redirect(url_for("julian.confirmar", sid=sid))

    try:
        c = _sgs()
    except Exception as ex:
        flash(f"Error conectando a SGS: {ex}")
        return redirect(url_for("julian.balance_sgs", sid=sid))

    # Construir (o refrescar) nodos_data usando la misma lógica que balance_sgs
    try:
        nodos_data = _armar_nodos_data_j(s, c)
    except Exception as ex:
        log.error(f"[EXPORTAR SGS] Error construyendo nodos_data: {ex}")
        flash(f"Error preparando datos SGS: {ex}")
        return redirect(url_for("julian.balance_sgs", sid=sid))

    wb = openpyxl.Workbook()
    _hoja_sgs_balance_xls(wb, nodos_data, s)
    _hoja_cruce_sgs_xls(wb, nodos_data)
    _hoja_mo_sgs_xls(wb, s)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)

    lcl = s.get("lcl_usado") or s.get("lcl_detectado") or "sgs"
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"sgs_{lcl}_{ts}.xlsx"

    return send_file(
        out,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ── Paleta de colores modelo (extraída de balance_integrado_*.xlsx) ──────────
_XLS_HDR_VERDE   = "375623"   # cabecera principal — verde oscuro
_XLS_SUB_VERDE   = "E2EFDA"   # sub-header columnas — verde muy claro
_XLS_PENDIENTE   = "FFEB9C"   # ámbar — PENDIENTE
_XLS_DEVOLUCION  = "C6EFCE"   # verde claro — DEVOLUCION / neto cero con dev
_XLS_OK_VERDE    = "00B050"   # verde fuerte — OK (consumido >= neto)
_XLS_GRIS        = "D9D9D9"   # gris — SIN MOV
_XLS_ROJO        = "FF0000"   # rojo — error / descuadre
_XLS_AMBAR_HDR   = "833C00"   # marrón — cabecera CRUCE
_XLS_SUB_AZUL    = "D6E4F0"   # azul claro — sub-header CRUCE
_XLS_BLANCO      = "FFFFFF"


def _xls_cell(ws, row, col, value="", bold=False, bg=None, fg="000000",
               align_h="left", size=9, wrap=False, border=True):
    """Escribe una celda con estilo estándar."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    cell = ws.cell(row=row, column=col, value=value)
    cell.font = Font(bold=bold, color=fg, size=size, name="Calibri")
    cell.alignment = Alignment(horizontal=align_h, vertical="center", wrap_text=wrap)
    if bg:
        cell.fill = PatternFill("solid", fgColor=bg)
    if border:
        s = Side(style="thin")
        cell.border = Border(left=s, right=s, top=s, bottom=s)
    return cell


def _hoja_sgs_balance_xls(wb: openpyxl.Workbook, nodos_data: list, s: dict):
    """
    Hoja SGS_BALANCE: agrupada por nodo con sub-headers.
    Columnas: # | Código | Descripción | Bandejas Despacho | Bandejas Devol. |
              Despachos | Devoluciones | Bal. Neto | Consumido | Estado
    Colores replicados del modelo balance_integrado_*.xlsx.
    """
    from openpyxl.utils import get_column_letter

    ws = wb.active
    ws.title = "SGS_BALANCE"
    ws.sheet_view.showGridLines = False

    # Anchos de columna (modelo: A=5, B=14, C=42, D=20, E=16, F=11, G=12, H=11, I=10, J=16)
    anchos = [5, 14, 42, 20, 16, 11, 12, 11, 10, 16]
    for i, w in enumerate(anchos, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    lcl = s.get("lcl_usado") or s.get("lcl_detectado") or ""
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Fila 1: título principal
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=10)
    t = ws.cell(row=1, column=1, value=f"SGS -- BALANCE  |  {lcl}  ({ts})")
    from openpyxl.styles import Alignment, Font, PatternFill
    t.font  = Font(bold=True, color=_XLS_BLANCO, size=12, name="Calibri")
    t.fill  = PatternFill("solid", fgColor=_XLS_HDR_VERDE)
    t.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    # Filas 2-3: metadatos
    _xls_cell(ws, 2, 1, "LCL/ODM:", bold=True, bg=_XLS_SUB_VERDE, border=False)
    _xls_cell(ws, 2, 2, lcl, bg=_XLS_BLANCO, border=False)
    _xls_cell(ws, 3, 1, "Generado:", bold=True, bg=_XLS_SUB_VERDE, border=False)
    _xls_cell(ws, 3, 2, ts, bg=_XLS_BLANCO, border=False)

    HDR = ["#", "Código", "Descripción", "Bandejas Despacho", "Bandejas Devol.",
           "Despachos", "Devoluciones", "Bal. Neto", "Consumido", "Estado"]

    r = 4
    idx_global = 1

    for nd in nodos_data:
        nodo    = nd.get("nodo", "")
        pro     = nd.get("pro_code", "")
        pnombre = nd.get("pnombre", "")

        # Sub-header: nodo
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=10)
        cell_nodo = ws.cell(row=r, column=1,
                            value=f"  Nodo: {nodo}  |  {pro}  {pnombre}")
        cell_nodo.font  = Font(bold=True, color=_XLS_BLANCO, size=10, name="Calibri")
        cell_nodo.fill  = PatternFill("solid", fgColor=_XLS_HDR_VERDE)
        cell_nodo.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[r].height = 16
        r += 1

        # Cabecera de columnas
        for ci, h in enumerate(HDR, 1):
            _xls_cell(ws, r, ci, h, bold=True, bg=_XLS_SUB_VERDE, align_h="center")
        ws.row_dimensions[r].height = 14
        r += 1

        for mat in nd.get("materiales", []):
            neto    = float(mat.get("neto") or 0)
            devto   = float(mat.get("devuelto") or 0)
            desp    = float(mat.get("despachado") or 0)
            adv     = float(mat.get("advance_real") or 0)

            # Determinar Estado y color
            if neto > 0 and adv >= neto:
                estado = "OK"
                bg = _XLS_OK_VERDE
            elif neto > 0 and adv == 0:
                estado = "PENDIENTE"
                bg = _XLS_PENDIENTE
            elif neto > 0 and 0 < adv < neto:
                diff = round(neto - adv, 4)
                estado = f"BAJO LIQ +{diff:g}"
                bg = _XLS_PENDIENTE
            elif devto > 0 and neto == 0:
                estado = "DEVOLUCION"
                bg = _XLS_DEVOLUCION
            else:
                estado = "SIN MOV"
                bg = _XLS_GRIS

            bd_desp_names = ", ".join(b.get("name", "") for b in (mat.get("band_despacho") or []))
            bd_devl_names = ", ".join(b.get("name", "") for b in (mat.get("band_devolucion") or []))

            _xls_cell(ws, r, 1,  idx_global,             bg=bg, align_h="center")
            _xls_cell(ws, r, 2,  mat.get("codigo_excel") or mat.get("codigo", ""), bg=bg, align_h="center")
            _xls_cell(ws, r, 3,  mat.get("descripcion", ""),  bg=bg, wrap=True)
            _xls_cell(ws, r, 4,  bd_desp_names or "—",  bg=bg, wrap=True)
            _xls_cell(ws, r, 5,  bd_devl_names or "—",  bg=bg, wrap=True)
            _xls_cell(ws, r, 6,  desp if desp else None,    bg=bg, align_h="center")
            _xls_cell(ws, r, 7,  devto if devto else None,  bg=bg, align_h="center")
            _xls_cell(ws, r, 8,  neto  if neto else None,   bg=bg, align_h="center", bold=True)
            _xls_cell(ws, r, 9,  adv   if adv else None,    bg=bg, align_h="center")
            _xls_cell(ws, r, 10, estado,                    bg=bg, align_h="center", bold=True)
            ws.row_dimensions[r].height = 14
            r += 1
            idx_global += 1

        r += 1  # fila vacía entre nodos


def _hoja_cruce_sgs_xls(wb: openpyxl.Workbook, nodos_data: list):
    """
    Hoja CRUCE_SGS: resumen por material sin columna SIP.
    Columnas: # | Nodo | Código | Descripción | SGS Neto | Estado
    Estilo inspirado en CRUCE_LIQUIDADOR del modelo.
    """
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet(title="CRUCE_SGS")
    ws.sheet_view.showGridLines = False

    # Anchos (modelo CRUCE_LIQUIDADOR: A=5, B=8, C=14, D=50, E=12, G=14)
    anchos = [5, 8, 14, 50, 12, 14]
    for i, w in enumerate(anchos, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # Leyenda de colores (fila 1-5)
    leyenda = [
        (_XLS_OK_VERDE,   _XLS_BLANCO, "Verde     — OK: SGS neto > 0 y consumido >= neto"),
        (_XLS_PENDIENTE,  "000000",    "Ambar     — PENDIENTE: neto > 0 y sin avance"),
        (_XLS_GRIS,       "000000",    "Gris      — SIN MOV: neto = 0 y sin devolución"),
        (_XLS_DEVOLUCION, "000000",    "Verde cl. — DEVOLUCION: devuelto > 0 y neto = 0"),
        (_XLS_ROJO,       _XLS_BLANCO, "Rojo      — DESCUADRE: datos SGS no cuadran con Excel"),
    ]
    for ri, (bg, fg, txt) in enumerate(leyenda, 1):
        ws.merge_cells(start_row=ri, start_column=1, end_row=ri, end_column=6)
        cell = ws.cell(row=ri, column=1, value=f"  {txt}")
        cell.font  = Font(size=8, italic=True, color=fg, name="Calibri")
        cell.fill  = PatternFill("solid", fgColor=bg)
        cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[ri].height = 13

    # Fila en blanco
    r = 6

    # Cabecera de columnas
    HDR = ["#", "Nodo", "Código", "Descripción", "SGS Neto", "Estado"]
    for ci, h in enumerate(HDR, 1):
        _xls_cell(ws, r, ci, h, bold=True, bg=_XLS_SUB_AZUL, align_h="center")
    ws.row_dimensions[r].height = 14
    ws.freeze_panes = ws["A7"]
    r += 1

    idx = 1
    for nd in nodos_data:
        nodo = nd.get("nodo", "")

        # Sub-header nodo
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        sh = ws.cell(row=r, column=1, value=f"  Nodo: {nodo}  —  {nd.get('pro_code', '')}  {nd.get('pnombre', '')}")
        sh.font  = Font(bold=True, color=_XLS_BLANCO, size=9, name="Calibri")
        sh.fill  = PatternFill("solid", fgColor=_XLS_AMBAR_HDR)
        sh.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[r].height = 15
        r += 1

        for mat in nd.get("materiales", []):
            neto  = float(mat.get("neto") or 0)
            devto = float(mat.get("devuelto") or 0)
            adv   = float(mat.get("advance_real") or 0)

            if neto > 0 and adv >= neto:
                estado, bg = "OK", _XLS_OK_VERDE
            elif neto > 0 and adv < neto:
                estado, bg = "PENDIENTE", _XLS_PENDIENTE
            elif neto == 0 and devto == 0:
                estado, bg = "SIN MOV", _XLS_GRIS
            elif devto > 0 and neto == 0:
                estado, bg = "DEVOLUCION", _XLS_DEVOLUCION
            else:
                estado, bg = "SIN MOV", _XLS_GRIS

            _xls_cell(ws, r, 1, idx,                                        bg=_XLS_BLANCO, align_h="center")
            _xls_cell(ws, r, 2, nodo,                                        bg=_XLS_BLANCO, align_h="center")
            _xls_cell(ws, r, 3, mat.get("codigo_excel") or mat.get("codigo", ""), bg=_XLS_BLANCO, align_h="center")
            _xls_cell(ws, r, 4, mat.get("descripcion", ""),                  bg=_XLS_BLANCO, wrap=True)
            _xls_cell(ws, r, 5, neto if neto else None,                      bg=bg, align_h="center", bold=True)
            _xls_cell(ws, r, 6, estado,                                      bg=bg, align_h="center", bold=True)
            ws.row_dimensions[r].height = 14
            r += 1
            idx += 1

        r += 1  # fila vacía entre nodos


def _hoja_mo_sgs_xls(wb: openpyxl.Workbook, s: dict):
    """
    Hoja MO_SGS: Mano de Obra de la sesión.
    Columnas: Nodo | QP Code | Baremo | Cantidad | Partner | Estado SGS
    Fuente: s["checks_mo"] enriquecido por verificar_fila_mo.
    """
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet(title="MO_SGS")
    ws.sheet_view.showGridLines = False

    anchos = [8, 14, 40, 10, 30, 16]
    for i, w in enumerate(anchos, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # Título
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=6)
    t = ws.cell(row=1, column=1, value="MO SGS — Mano de Obra verificada")
    t.font  = Font(bold=True, color=_XLS_BLANCO, size=11, name="Calibri")
    t.fill  = PatternFill("solid", fgColor=_XLS_HDR_VERDE)
    t.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 20

    # Cabecera
    HDR = ["Nodo", "QP Code", "Baremo / Actividad", "Cantidad", "Partner / Líder", "Estado SGS"]
    r = 2
    for ci, h in enumerate(HDR, 1):
        _xls_cell(ws, r, ci, h, bold=True, bg=_XLS_SUB_VERDE, align_h="center")
    ws.row_dimensions[r].height = 14
    ws.freeze_panes = ws["A3"]
    r = 3

    checks_mo = s.get("checks_mo") or []
    if not checks_mo:
        ws.cell(row=r, column=1, value="(sin datos de MO en esta sesión)")
        return

    C_OK   = "C6EFCE"
    C_ERR  = "FFC7CE"
    C_YA   = "D9D9D9"

    for i, ck in enumerate(checks_mo):
        row  = ck.get("row") or {}
        nodo = str(row.get("pro") or "")
        qp   = str(row.get("codigo") or "")
        bar  = str(row.get("baremo") or "")
        cant = row.get("cantidad")
        part = str(ck.get("partner_nombre") or "")
        ya   = ck.get("ya_liquidada", False)
        ok   = ck.get("ok", False)
        estado_raw = str(ck.get("estado") or ("ya_liquidada" if ya else ("ok" if ok else "error")))

        if ya:
            bg, estado_txt = C_YA, "Ya liquidada"
        elif ok:
            bg, estado_txt = C_OK, "OK"
        else:
            bg, estado_txt = C_ERR, estado_raw

        _xls_cell(ws, r, 1, nodo,       bg=bg, align_h="center")
        _xls_cell(ws, r, 2, qp,         bg=bg, align_h="center")
        _xls_cell(ws, r, 3, bar,        bg=bg, wrap=True)
        _xls_cell(ws, r, 4, cant,       bg=bg, align_h="center")
        _xls_cell(ws, r, 5, part,       bg=bg)
        _xls_cell(ws, r, 6, estado_txt, bg=bg, align_h="center", bold=True)
        ws.row_dimensions[r].height = 14
        r += 1


# ─────────────────────────────────────────────────────────────────────────────

@julian.route("/cruce_sgs/<sid>")
def cruce_sgs(sid):
    """
    Genera y descarga un Excel estilo CRUCE_SGS con el balance de materiales:
    Despachos (+) - Devoluciones (-) = Neto vs Avance vs Pendiente.
    Basado en los datos de checks_mat de la sesión (post-verificacion).
    """
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    checks_mat = s.get("checks_mat") or []
    if not checks_mat:
        flash("No hay materiales verificados. Ejecuta /verificar primero.")
        return redirect(url_for("julian.verificar", sid=sid))

    try:
        excel_bytes = _generar_cruce_sgs_excel(checks_mat, s)
    except Exception as ex:
        log.error(f"[JULIAN CRUCE_SGS] Error generando Excel: {ex}")
        flash(f"Error generando CRUCE_SGS: {ex}")
        return redirect(url_for("julian.verificar", sid=sid))

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    nombre = f"cruce_sgs_{ts}.xlsx"
    return send_file(
        io.BytesIO(excel_bytes),
        as_attachment=True,
        download_name=nombre,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _generar_cruce_sgs_excel(checks_mat: list, sesion: dict) -> bytes:
    """
    Genera un Excel con hoja CRUCE_SGS mostrando el balance de materiales:
      # | PRO | Nodo | Código SAP | Descripción | Excel Cant | Despachado | Devuelto |
        SGS Neto | Avance | Pendiente | Partner | Estado

    Colores (inspirados en CRUCE_LIQUIDADOR de SGS+SONY):
      Verde claro  — pendiente == 0 (ya liquidado / sin movimiento)
      Verde fuerte — ok pendiente > 0 (listo para liquidar)
      Rojo claro   — bloqueado / no encontrado / descuadre
      Ámbar        — avance parcial
      Gris         — sin movimiento neto
    """
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    C_HDR     = "833C00"  # marrón — mismo que CRUCE_LIQUIDADOR
    C_SUBHDR  = "D6E4F0"
    C_VERDE_F = "00B050"
    C_VERDE_C = "C6EFCE"
    C_ROJO_C  = "FFC7CE"
    C_AMBAR   = "FFEB9C"
    C_SALMON  = "FFCCBC"  # devolución presente (verificar vs salidas físicas)
    C_GRIS    = "D9D9D9"
    C_PAR     = "F2F2F2"
    BLANCO    = "FFFFFF"

    def _s():
        return Side(style="thin")

    def _c(ws, row, col, value="", bold=False, bg=None, fg="000000",
           align_h="left", size=9, wrap=False):
        cell = ws.cell(row=row, column=col, value=value)
        cell.font = Font(bold=bold, color=fg, size=size, name="Calibri")
        cell.alignment = Alignment(horizontal=align_h, vertical="center", wrap_text=wrap)
        if bg:
            cell.fill = PatternFill("solid", fgColor=bg)
        b = _s()
        cell.border = Border(left=b, right=b, top=b, bottom=b)
        return cell

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CRUCE_SGS"
    ws.sheet_view.showGridLines = False

    anchos = [5, 12, 8, 14, 40, 10, 11, 11, 11, 10, 10, 28, 18, 30, 30]
    for i, w in enumerate(anchos, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ── Título ──────────────────────────────────────────────────────────────────
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=15)
    t = ws.cell(row=1, column=1,
                value="CRUCE SGS — Balance Despachos / Devoluciones / Avance")
    t.font = Font(bold=True, color=BLANCO, size=12, name="Calibri")
    t.fill = PatternFill("solid", fgColor=C_HDR)
    t.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    # Leyenda
    for r_ley, (bg, txt) in enumerate([
        (C_VERDE_F, "Verde fuerte — OK: listo para liquidar (pendiente > 0)"),
        (C_VERDE_C, "Verde claro  — Ya liquidado o sin movimiento pendiente"),
        (C_AMBAR,   "Ambar        — Avance parcial"),
        (C_SALMON,  "Salmón       — Devolución activa (verificar BAN dev vs salidas físicas)"),
        (C_ROJO_C,  "Rojo claro   — Bloqueado / no encontrado / descuadre"),
        (C_GRIS,    "Gris         — Sin movimiento neto en bandejas"),
    ], start=2):
        ws.merge_cells(start_row=r_ley, start_column=1, end_row=r_ley, end_column=15)
        cell = ws.cell(row=r_ley, column=1, value=f"  {txt}")
        cell.font = Font(size=8, italic=True, name="Calibri")
        cell.fill = PatternFill("solid", fgColor=bg)
        cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[r_ley].height = 13

    # Cabecera de columnas
    HDR = ["#", "PRO", "Nodo", "Código SAP", "Descripción",
           "Excel Cant", "Despachado", "Devuelto", "SGS Neto",
           "Avance", "Pendiente", "Partner", "Estado",
           "BAN Despacho", "BAN Devolución"]
    r_hdr = 8
    for col_i, h in enumerate(HDR, 1):
        _c(ws, r_hdr, col_i, h, bold=True, bg=C_SUBHDR, align_h="center")
    ws.row_dimensions[r_hdr].height = 14
    ws.freeze_panes = ws["A9"]

    def _fmt(v):
        if v is None:
            return "—"
        try:
            f = float(v)
            return int(f) if f == int(f) else round(f, 3)
        except Exception:
            return str(v)

    tot_ok = tot_pend = tot_ya = tot_err = tot_sin = 0
    idx_global = 1
    r = r_hdr + 1

    for ck in checks_mat:
        mat = ck.get("mat") or {}
        nodo      = str(mat.get("nodo") or "")
        codigo    = str(mat.get("codigo") or "")
        desc      = str(mat.get("descripcion") or "")
        excel_cnt = mat.get("sgs_neto_excel")
        pro_code  = str(ck.get("pro_code") or "")
        partner   = str(ck.get("partner_nombre") or "")
        estado    = str(ck.get("estado") or ("ok" if ck.get("ok") else "no_encontrado"))

        desp    = ck.get("sgs_despachado")
        devto   = ck.get("sgs_devuelto")
        neto    = ck.get("sgs_neto")
        advance = ck.get("advance_real")
        pendiente = (round(float(neto) - float(advance), 4)
                     if neto is not None and advance is not None else None)

        ban_desp = ", ".join(ck.get("ban_despacho")   or [])
        ban_dev  = ", ".join(ck.get("ban_devolucion")  or [])
        tiene_dev = float(devto or 0) > 0

        # Color según estado; salmón cuando hay devolución activa (ok o parcial)
        if not ck.get("ok") or estado in ("no_encontrado", "error"):
            bg = C_ROJO_C;  tot_err += 1
        elif tiene_dev and estado not in ("ya_liquidado", "sin_mov_ok", "sin_mov"):
            bg = C_SALMON;  tot_pend += 1
        elif estado in ("ya_liquidado", "sin_mov_ok"):
            bg = C_VERDE_C; tot_ya += 1
        elif estado == "sin_mov":
            bg = C_GRIS;    tot_sin += 1
        elif estado == "avance_parcial":
            bg = C_AMBAR;   tot_pend += 1
        elif estado == "ok":
            bg = C_VERDE_F; tot_ok += 1
        else:
            bg = C_PAR if idx_global % 2 == 0 else BLANCO

        _c(ws, r, 1,  idx_global,     bg=bg, align_h="center")
        _c(ws, r, 2,  pro_code,       bg=bg, align_h="center")
        _c(ws, r, 3,  nodo,           bg=bg, align_h="center")
        _c(ws, r, 4,  codigo,         bg=bg, align_h="center")
        _c(ws, r, 5,  desc,           bg=bg, align_h="left", wrap=True)
        _c(ws, r, 6,  _fmt(excel_cnt),bg=bg, align_h="center")
        _c(ws, r, 7,  _fmt(desp),     bg=bg, align_h="center")
        # col 8 Devuelto: fondo naranja fuerte cuando hay devolución para resaltar
        bg_dev = "FF6D00" if tiene_dev else bg
        fg_dev = BLANCO   if tiene_dev else "000000"
        _c(ws, r, 8,  _fmt(devto),    bg=bg_dev, align_h="center",
           bold=tiene_dev, fg=fg_dev)
        _c(ws, r, 9,  _fmt(neto),     bg=bg, align_h="center", bold=True)
        _c(ws, r, 10, _fmt(advance),  bg=bg, align_h="center")
        _c(ws, r, 11, _fmt(pendiente),bg=bg, align_h="center", bold=True)
        _c(ws, r, 12, partner,        bg=bg, align_h="left")
        _c(ws, r, 13, estado,         bg=bg, align_h="center", bold=True)
        _c(ws, r, 14, ban_desp,       bg=bg, align_h="left", wrap=True)
        bg_bdev = C_SALMON if ban_dev else bg
        _c(ws, r, 15, ban_dev,        bg=bg_bdev, align_h="left",
           bold=bool(ban_dev), wrap=True)
        ws.row_dimensions[r].height = 16 if (ban_desp or ban_dev) else 14
        r += 1
        idx_global += 1

    # Fila resumen
    r += 1
    total = tot_ok + tot_pend + tot_ya + tot_err + tot_sin
    bg_res = C_VERDE_F if tot_err == 0 else C_ROJO_C
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=15)
    cell = ws.cell(row=r, column=1,
                   value=(f"RESUMEN — Total: {total}  |  Listos: {tot_ok}  |  "
                          f"Parcial: {tot_pend}  |  Ya liquidados: {tot_ya}  |  "
                          f"Sin mov: {tot_sin}  |  Bloqueados/Error: {tot_err}"))
    cell.font = Font(bold=True, color=BLANCO, size=10, name="Calibri")
    cell.fill = PatternFill("solid", fgColor=bg_res)
    cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[r].height = 20

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@julian.route("/liq_visual/<sid>", methods=["GET", "POST"])
def liq_visual(sid):
    """
    Prueba visual de liquidación: abre un navegador visible y ejecuta
    liquidar_material con dry_run=True para diagnóstico.
    GET  → muestra lista de materiales custodia con pm_id.
    POST → ejecuta la prueba y devuelve JSON con el resultado.
    """
    s = _sesiones.get(sid)
    if not s:
        return {"ok": False, "error": "Sesion no encontrada"}, 404

    if request.method == "GET":
        # Listar todos los materiales con pm_id (incluyendo custodia)
        mats = []
        for mc in s.get("checks_mat", []):
            if not mc.get("pm_id"):
                continue
            mats.append({
                "pm_id":        mc["pm_id"],
                "codigo":       mc["mat"]["codigo"],
                "codigo_sgs":   mc.get("codigo_sgs", mc["mat"]["codigo"]),
                "es_custodia":  mc.get("es_custodia", False),
                "nodo":         mc["mat"].get("nodo", ""),
                "pro_code":     mc.get("pro_code", ""),
                "cantidad":     mc.get("sgs_neto") or mc["mat"].get("sgs_neto_excel") or 1,
                "partner":      mc.get("partner_nombre", ""),
            })
        rows = ""
        for m in mats:
            badge = "★" if m["es_custodia"] else ""
            p = str(m['partner']).replace("'", "\\'")
            rows += (
                f"<tr id='row-{m['pm_id']}'>"
                f"<td><code>{m['pm_id']}</code></td>"
                f"<td><strong>{m['codigo_sgs']}</strong></td>"
                f"<td>{badge}</td>"
                f"<td>{m['nodo']}</td>"
                f"<td style='font-size:0.8em'>{m['pro_code']}</td>"
                f"<td>{m['cantidad']}</td>"
                f"<td style='font-size:0.8em'>{m['partner']}</td>"
                f"<td>"
                f"<button onclick=\"lanzar({m['pm_id']},{m['cantidad']},'{p}','{m['pro_code']}',true)\" "
                f"style='background:#1565c0;color:white;border:none;padding:4px 10px;cursor:pointer;border-radius:4px;margin-right:4px;'>▶ dry_run</button>"
                f"<button onclick=\"if(confirm('REAL — se grabará en SGS'))lanzar({m['pm_id']},{m['cantidad']},'{p}','{m['pro_code']}',false)\" "
                f"style='background:#e74c3c;color:white;border:none;padding:4px 10px;cursor:pointer;border-radius:4px;margin-right:4px;'>★ REAL</button>"
                f"<button onclick=\"diagnosticar({m['pm_id']},{m['cantidad']},'{p}')\" "
                f"style='background:#6a0dad;color:white;border:none;padding:4px 10px;cursor:pointer;border-radius:4px;'>Diag</button>"
                f"</td>"
                f"<td id='res-{m['pm_id']}' style='font-size:0.78em;max-width:220px;color:#555;'></td>"
                f"<td id='diag-{m['pm_id']}' style='font-size:0.75em;max-width:320px;color:#555;'></td>"
                f"</tr>"
            )
        html = f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<style>body{{font-family:sans-serif;font-size:0.88rem;padding:16px}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:6px 10px}}
th{{background:#2d5a42;color:white}}.ok{{color:#27ae60;font-weight:700}}.err{{color:#e74c3c;font-weight:700}}
.spin{{display:inline-block;width:14px;height:14px;border:2px solid #ddd;border-top-color:#1565c0;border-radius:50%;animation:sp 0.8s linear infinite}}
@keyframes sp{{to{{transform:rotate(360deg)}}}}</style></head><body>
<h2>Prueba visual — {s['filename']}</h2>
<p style='color:#888;font-size:0.82rem;'>Los botones no navegan — el resultado aparece en la columna de la derecha.</p>
<table><tr><th>pm_id</th><th>Código SGS</th><th>CUS</th><th>Nodo</th><th>PRO</th><th>Cant</th><th>Lider</th><th>Acción</th><th>Resultado</th><th>Diagnóstico</th></tr>
{rows}</table>
<script>
async function lanzar(pm_id, cantidad, partner, pro_code, dry_run) {{
  const cel = document.getElementById('res-' + pm_id);
  cel.innerHTML = '<span class=spin></span> ejecutando…';
  const fd = new FormData();
  fd.append('pm_id', pm_id);
  fd.append('cantidad', cantidad);
  fd.append('partner_nombre', partner);
  fd.append('pro_code', pro_code);
  fd.append('dry_run', dry_run ? '1' : '0');
  try {{
    const r = await fetch('', {{method:'POST', body:fd}});
    const d = await r.json();
    if (d.ok) {{
      cel.innerHTML = '<span class=ok>✓ ' + (dry_run ? 'dry OK' : 'GUARDADO') + '</span>';
    }} else {{
      cel.innerHTML = '<span class=err>✗ ' + (d.error || 'error') + '</span>';
    }}
  }} catch(e) {{
    cel.innerHTML = '<span class=err>✗ ' + e + '</span>';
  }}
}}

async function diagnosticar(pm_id, cantidad, partner) {{
  const cel = document.getElementById('diag-' + pm_id);
  cel.innerHTML = '<span class=spin></span> diagnosticando…';
  const fd = new FormData();
  fd.append('pm_id', pm_id);
  fd.append('cantidad', cantidad);
  fd.append('partner_nombre', partner);
  try {{
    const r = await fetch(window.location.pathname.replace('/liq_visual/', '/diagnosticar/'), {{method:'POST', body:fd}});
    const d = await r.json();
    if (d.ok) {{
      let txt = '<span class=ok>✓ OK</span>';
      if (d.fix_aplicado) txt += ' <em style="color:#e67e22">(fix: ' + d.fix_aplicado + ')</em>';
      cel.innerHTML = txt + '<br><small>' + (d.diagnosis || '') + '</small>';
    }} else {{
      const paso = d.paso_fallido ? '[' + d.paso_fallido + '] ' : '';
      const fix = d.fix_aplicado ? '<br><em style="color:#e67e22">Fix aplicado: ' + d.fix_aplicado + ' → ' + (d.fix_exitoso ? '✓ OK' : '✗ fallido') + '</em>' : '';
      cel.innerHTML = '<span class=err>✗ ' + paso + (d.error || 'error') + '</span>' + fix + '<br><small style="color:#666">' + (d.diagnosis || '') + '</small>';
    }}
    // Mostrar detalles útiles
    const det = d.detalles || {{}};
    const extras = [];
    if (det.editable !== undefined) extras.push('editable=' + det.editable);
    if (det.add_line_en_dom !== undefined) extras.push('add_line_dom=' + det.add_line_en_dom);
    if (det.add_line_visible !== undefined) extras.push('visible=' + det.add_line_visible);
    if (det.partner_ok !== undefined) extras.push('partner_ok=' + det.partner_ok);
    if (extras.length) cel.innerHTML += '<br><small style="color:#888">' + extras.join(' | ') + '</small>';
  }} catch(e) {{
    cel.innerHTML = '<span class=err>✗ ' + e + '</span>';
  }}
}}
</script></body></html>"""
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    # POST — ejecutar prueba visual
    pm_id          = request.form.get("pm_id",          type=int)
    cantidad       = request.form.get("cantidad",        type=float)
    partner_nombre = request.form.get("partner_nombre",  default="")
    pro_code       = request.form.get("pro_code",        default="")
    dry_run        = request.form.get("dry_run",         default="1") == "1"

    if not pm_id or not cantidad:
        return {"ok": False, "error": "Faltan pm_id o cantidad"}, 400

    log.info(f"[LIQ_VISUAL] pm#{pm_id} qty={cantidad} dry_run={dry_run} partner='{partner_nombre}'")
    try:
        with PlaywrightClient(headless=False, slow_mo=600) as pw:
            pw.login()
            result = pw.liquidar_material(
                pm_id=pm_id, cantidad=cantidad,
                partner_nombre=partner_nombre,
                pro_code=pro_code,
                dry_run=dry_run,
                screenshots=True,
            )
        result["pm_id_usado"] = pm_id
        result["dry_run"]     = dry_run
        return jsonify(result)
    except Exception as ex:
        log.error(f"[LIQ_VISUAL] Error pm#{pm_id}: {ex}")
        return jsonify({"ok": False, "error": str(ex), "pm_id_usado": pm_id})


@julian.route("/diagnosticar/<sid>", methods=["POST"])
def diagnosticar(sid):
    """
    Auto-diagnóstico de un material: ejecuta el flujo paso a paso con Playwright
    visible, captura DOM en cada punto, clasifica el error y aplica fix conocido.
    """
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"ok": False, "error": "Sesion no encontrada"}), 404

    pm_id          = request.form.get("pm_id",         type=int)
    cantidad       = request.form.get("cantidad",       type=float)
    partner_nombre = request.form.get("partner_nombre", default="")

    if not pm_id or not cantidad:
        return jsonify({"ok": False, "error": "Faltan pm_id o cantidad"}), 400

    log.info(f"[DIAG] pm#{pm_id} qty={cantidad} partner='{partner_nombre}'")
    try:
        with PlaywrightClient(headless=False, slow_mo=600) as pw:
            pw.login()
            result = pw.diagnosticar_material(
                pm_id=pm_id,
                cantidad=cantidad,
                partner_nombre=partner_nombre,
            )
        # Limpiar html_campo (puede ser muy largo)
        if "detalles" in result and "html_campo" in result["detalles"]:
            result["detalles"]["html_campo"] = result["detalles"]["html_campo"][:800] + "…"
        return jsonify(result)
    except Exception as ex:
        log.error(f"[DIAG] Error pm#{pm_id}: {ex}")
        return jsonify({"ok": False, "error": str(ex), "pm_id_usado": pm_id})


@julian.route("/pw_status")
def pw_status():
    alive = _pw_global is not None and _pw_global._logueado
    return jsonify({"alive": alive})


@julian.route("/browser_debug")
def browser_debug():
    """Estado interno del browser — para diagnóstico rápido."""
    # _page.url es una propiedad Python segura (no llama DOM de Playwright)
    cur_url = None
    if _pw_global and _pw_global._page:
        try:
            cur_url = _pw_global._page.url
        except Exception:
            cur_url = "(error leyendo url)"

    return jsonify({
        "monitor_running":   _monitor_running,
        "pw_global_exists":  _pw_global is not None,
        "pw_page_exists":    _pw_global is not None and _pw_global._page is not None,
        "logueado":          _pw_global._logueado if _pw_global else False,
        "sesion_activa":     sesion_activa(),
        "storage_state_ok":  _cached_storage_state is not None,
        "queue_size":        _browser_commands.qsize(),
        "url_actual":        cur_url,
        "instruccion": (
            "SESION ACTIVA — listo para liquidar" if sesion_activa()
            else "Busca la ventana de Chromium en tu barra de tareas e inicia sesion en SGS. "
                 "Luego recarga esta pagina para confirmar."
        ),
    })


@julian.route("/test_sesion")
def test_sesion():
    """Página HTML de diagnóstico de sesión — auto-recarga cada 2s."""
    return """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Test Sesión SGS</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f0f2f5; color: #222;
      padding: 30px; max-width: 760px; margin: 0 auto;
    }
    h1 { font-size: 1.3rem; margin: 0 0 4px; }
    .subtitle { color: #888; font-size: 0.85rem; margin: 0 0 24px; }

    /* ── Indicador de sesión ── */
    .sesion-banner {
      border-radius: 10px; padding: 16px 20px;
      display: flex; align-items: center; gap: 14px;
      margin-bottom: 20px; font-weight: 700; font-size: 1rem;
      transition: background 0.3s, color 0.3s;
    }
    .sesion-banner .dot {
      width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0;
    }
    .banner-ok   { background: #e8f5e9; color: #2e7d32; }
    .banner-warn { background: #fff8e1; color: #856404; }
    .banner-err  { background: #fdecea; color: #c0392b; }
    .dot-ok   { background: #27ae60; }
    .dot-warn { background: #f39c12; animation: pulso 0.9s infinite; }
    .dot-err  { background: #e74c3c; }
    @keyframes pulso {
      0%,100% { opacity:1; transform:scale(1); }
      50%      { opacity:.5; transform:scale(1.35); }
    }

    /* ── Tabla de estado ── */
    .card {
      background: white; border-radius: 10px;
      box-shadow: 0 2px 8px rgba(0,0,0,.07);
      margin-bottom: 16px; overflow: hidden;
    }
    .card-hdr {
      padding: 10px 18px; font-weight: 700; font-size: 0.82rem;
      color: #555; background: #f8f9fa; border-bottom: 1px solid #eee;
      text-transform: uppercase; letter-spacing: .04em;
    }
    table { width: 100%; border-collapse: collapse; }
    td { padding: 9px 18px; font-size: 0.88rem; border-bottom: 1px solid #f5f5f5; }
    tr:last-child td { border-bottom: none; }
    td.lbl { color: #666; width: 40%; }
    td.val { font-weight: 600; }

    .pill { display:inline-block; padding: 2px 10px; border-radius: 20px;
            font-size: 0.78rem; font-weight: 700; }
    .pill-ok   { background: #e8f5e9; color: #2e7d32; }
    .pill-err  { background: #fdecea; color: #c0392b; }
    .pill-warn { background: #fff8e1; color: #856404; }

    .url-val { color: #6a1b9a; font-family: monospace; font-size: 0.8rem;
               word-break: break-all; }

    /* ── Botones ── */
    .btn-row {
      display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px;
    }
    .btn {
      border: none; border-radius: 7px; padding: 9px 18px;
      cursor: pointer; font-weight: 700; font-size: 0.88rem;
      display: inline-flex; align-items: center; gap: 7px;
      transition: opacity .15s, transform .1s;
    }
    .btn:active { transform: scale(.97); }
    .btn:disabled { opacity: .45; cursor: default; }
    .btn:not(:disabled):hover { opacity: .88; }
    .btn-open    { background: #27ae60; color: white; }
    .btn-close   { background: #e74c3c; color: white; }
    .btn-check   { background: #1565c0; color: white; }
    .btn-force   { background: #f39c12; color: white; }

    .feedback {
      font-size: 0.82rem; padding: 8px 14px; border-radius: 7px;
      margin-top: 8px; display: none;
    }
    .feedback.ok   { background: #e8f5e9; color: #2e7d32; display: block; }
    .feedback.err  { background: #fdecea; color: #c0392b; display: block; }
    .feedback.info { background: #e3f2fd; color: #1565c0; display: block; }

    /* ── JSON raw ── */
    details summary {
      cursor: pointer; font-size: 0.82rem; font-weight: 700; color: #888;
      padding: 8px 14px; background: #f8f9fa; border-radius: 7px;
      user-select: none;
    }
    pre {
      background: #1e1e2e; color: #cdd6f4; border-radius: 8px;
      padding: 14px 18px; font-size: 0.78rem; line-height: 1.7;
      white-space: pre-wrap; overflow-x: auto; margin-top: 6px;
    }

    .cronometro { font-size: 0.78rem; color: #aaa; margin-left: auto; }
  </style>
</head>
<body>

<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
  <h1>Diagnóstico de Sesión SGS</h1>
  <span class="cronometro" id="lbl-tick">actualizando…</span>
</div>
<p class="subtitle">Actualización automática cada 2 s — todos los estados en tiempo real.</p>

<!-- Banner de estado -->
<div class="sesion-banner banner-warn" id="banner">
  <div class="dot dot-warn" id="banner-dot"></div>
  <span id="banner-txt">Cargando…</span>
</div>

<!-- Botones de control -->
<div class="btn-row">
  <button class="btn btn-open"  onclick="accion('abrir')">
    Abrir browser SGS
  </button>
  <button class="btn btn-close" onclick="accion('cerrar')">
    ✕ Cerrar browser
  </button>
  <button class="btn btn-check" onclick="accion('reverificar')">
    ↻ Reverificar sesión
  </button>
  <button class="btn btn-force" onclick="accion('forzar')">
     Forzar sesión activa
  </button>
</div>
<div class="feedback" id="feedback"></div>

<!-- Tabla de estado -->
<div class="card" style="margin-top:14px;">
  <div class="card-hdr">Estado interno</div>
  <table>
    <tbody id="tabla-estado">
      <tr><td class="lbl">Cargando…</td><td class="val"></td></tr>
    </tbody>
  </table>
</div>

<!-- JSON raw -->
<details>
  <summary>JSON raw (browser_debug)</summary>
  <pre id="raw">{}</pre>
</details>

<script>
const pill = (val, t, f, warnVal) => {
  const cls = val ? 'pill-ok' : (warnVal ? 'pill-warn' : 'pill-err');
  return `<span class="pill ${cls}">${val ? t : f}</span>`;
};

let lastPoll = Date.now();

async function poll() {
  try {
    const r = await fetch('/julian/browser_debug');
    if (!r.ok) return;
    const d = await r.json();
    lastPoll = Date.now();
    document.getElementById('lbl-tick').textContent =
      new Date().toLocaleTimeString('es-CO');
    document.getElementById('raw').textContent = JSON.stringify(d, null, 2);

    /* ── Banner ── */
    const banner = document.getElementById('banner');
    const dot    = document.getElementById('banner-dot');
    const txt    = document.getElementById('banner-txt');
    banner.className = 'sesion-banner';
    dot.className = 'dot';
    if (d.sesion_activa) {
      banner.classList.add('banner-ok');
      dot.classList.add('dot-ok');
      txt.textContent = 'Sesión activa — listo para liquidar';
    } else if (d.pw_page_exists) {
      banner.classList.add('banner-warn');
      dot.classList.add('dot-warn');
      txt.textContent = 'Browser abierto — esperando login en SGS';
    } else {
      banner.classList.add('banner-err');
      dot.classList.add('dot-err');
      txt.textContent = 'Sin browser — presiona "Abrir browser SGS"';
    }

    /* ── Tabla ── */
    document.getElementById('tabla-estado').innerHTML = `
      <tr><td class="lbl">Monitor activo</td>
          <td class="val">${pill(d.monitor_running,'✓ Corriendo','✗ Caído')}</td></tr>
      <tr><td class="lbl">Browser abierto</td>
          <td class="val">${pill(d.pw_global_exists,'✓ Sí','✗ No')}</td></tr>
      <tr><td class="lbl">Página cargada</td>
          <td class="val">${pill(d.pw_page_exists,'✓ Sí','✗ No')}</td></tr>
      <tr><td class="lbl">URL actual</td>
          <td class="val"><span class="url-val">${d.url_actual || '—'}</span></td></tr>
      <tr><td class="lbl">Logueado (DOM/URL)</td>
          <td class="val">${pill(d.logueado,'✓ Sí','✗ No')}</td></tr>
      <tr><td class="lbl">Sesión activa</td>
          <td class="val">${pill(d.sesion_activa,'✓ Sí','✗ No')}</td></tr>
      <tr><td class="lbl">Storage state</td>
          <td class="val">${pill(d.storage_state_ok,'✓ Exportado','✗ Sin estado')}</td></tr>
      <tr><td class="lbl">Cola de comandos</td>
          <td class="val">${d.queue_size > 0
            ? `<span class="pill pill-warn">${d.queue_size} pendiente(s)</span>`
            : `<span class="pill pill-ok">Vacía</span>`}</td></tr>
    `;
  } catch(e) {
    document.getElementById('banner-txt').textContent = 'Error de red: ' + e;
  }
}

async function accion(tipo) {
  const fb = document.getElementById('feedback');
  fb.className = 'feedback info';
  fb.textContent = 'Enviando…';

  const endpoints = {
    abrir:       ['/julian/browser_ctrl', {cmd:'restart_browser'}],
    cerrar:      ['/julian/browser_ctrl', {cmd:'close_browser'}],
    reverificar: ['/julian/browser_ctrl', {cmd:'reverificar'}],
    forzar:      ['/julian/forzar_sesion', {}],
  };
  const [url, body] = endpoints[tipo];

  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.ok !== false) {
      fb.className = 'feedback ok';
      fb.textContent = '✓ ' + (d.msg || d.message || 'Comando enviado');
    } else {
      fb.className = 'feedback err';
      fb.textContent = '✗ ' + (d.error || 'Error desconocido');
    }
  } catch(e) {
    fb.className = 'feedback err';
    fb.textContent = '✗ Error de red: ' + e;
  }
  setTimeout(poll, 800);
}

poll();
setInterval(poll, 2000);
</script>
</body>
</html>"""


@julian.route("/browser_ctrl", methods=["POST"])
def browser_ctrl():
    """
    Endpoint JSON para controlar el browser desde la página test_sesion.
    Body JSON: { "cmd": "restart_browser" | "close_browser" | "reverificar" }
    """
    data = request.get_json(force=True, silent=True) or {}
    cmd  = data.get("cmd", "")

    if cmd == "restart_browser":
        _browser_commands.put("restart_browser")
        return jsonify({"ok": True, "msg": "Comando enviado — el browser abrirá en unos segundos"})

    elif cmd == "close_browser":
        _browser_commands.put("close_browser")
        return jsonify({"ok": True, "msg": "Comando enviado — el browser se cerrará"})

    elif cmd == "reverificar":
        # Fuerza al monitor a re-evaluar el estado en el próximo tick.
        # Solo reseteamos _logueado=False para que el monitor lo re-detecte limpio.
        if _pw_global:
            _pw_global._logueado = False
            with _cached_storage_state_lock:
                global _cached_storage_state
                _cached_storage_state = None
        return jsonify({"ok": True, "msg": "Estado reseteado — el monitor reverificará en ~4 s"})

    return jsonify({"ok": False, "error": f"Comando desconocido: {cmd}"}), 400


@julian.route("/forzar_sesion", methods=["POST"])
def forzar_sesion():
    """
    Fuerza _logueado=True y exporta storage_state desde el monitor.
    Útil cuando el selector DOM no detecta el login pero el usuario sí está logueado.
    El monitor debe ejecutar esto — lo hacemos vía comando especial.
    """
    if not (_pw_global and _pw_global._page):
        return jsonify({"ok": False, "error": "Browser no está abierto"})
    # Poner comando en la cola para que el monitor exporte el estado
    _browser_commands.put('force_session')
    # También marcar _logueado directamente (Python bool, thread-safe)
    _pw_global._logueado = True
    return jsonify({"ok": True, "msg": "Forzado — el monitor exportará el storage_state en el próximo tick"})


@julian.route("/sesion_estado")
def sesion_estado():
    """Endpoint de polling para el indicador de sesión en la UI."""
    activa = sesion_activa()
    # "iniciando": browser existe (página abierta) pero aún no hay login
    # Solo True cuando el usuario ya presionó el botón y el browser está visible.
    # Si _pw_global es None el browser no se ha abierto todavía → no "iniciando".
    browser_abierto = _pw_global is not None and _pw_global._page is not None
    return jsonify({
        "activa":    activa,
        "iniciando": browser_abierto and not activa,
    })


@julian.route("/errores/<sid>")
def errores_json(sid):
    """Sirve el archivo errores_<sid>.json para descarga/consulta rápida."""
    import json as _json
    logs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
    path = os.path.join(logs_dir, f"errores_{sid}.json")
    if not os.path.exists(path):
        return jsonify({"error": "Archivo no encontrado — la liquidación puede no haber completado aún"}), 404
    with open(path, encoding="utf-8") as _f:
        data = _json.load(_f)
    return jsonify(data)


@julian.route("/reporte/<sid>")
def reporte(sid):
    """
    Página de informe técnico consolidado:
    errores JSON + lista de capturas de pantalla + resumen del job.
    """
    import json as _json
    logs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
    path = os.path.join(logs_dir, f"errores_{sid}.json")
    debug = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as _f:
            debug = _json.load(_f)

    # Buscar capturas relacionadas con este sid
    capturas = []
    try:
        for fname in sorted(os.listdir(logs_dir)):
            if fname.endswith(".png"):
                capturas.append(fname)
    except Exception:
        pass

    s = _sesiones.get(sid)
    job = _trabajos.get(sid)
    return render_template(
        "julian/reporte_auditoria.html",
        sid=sid, s=s, job=job, debug=debug, capturas=capturas,
    )


@julian.route("/abrir_browser", methods=["POST"])
def abrir_browser():
    """
    Abre (o reabre) el browser visible en la página de login de SGS.

    Regla: NUNCA se llama Playwright desde aquí.
    Solo pone un comando en _browser_commands.
    El monitor (que tiene el greenlet correcto) lo ejecuta y abre/reabre el browser.
    """
    # Siempre enviamos restart_browser — el monitor cierra el viejo (si existe)
    # y abre uno nuevo. Es la única operación segura desde Flask.
    _browser_commands.put('restart_browser')
    log.info("[JULIAN] Comando restart_browser encolado desde abrir_browser route")

    if sesion_activa():
        flash("✓ Browser reabierto en la página de login.")
    else:
        flash("✓ Browser abriendo — aparecerá en unos segundos. "
              "Inicia sesión y espera el indicador verde.")

    return redirect(request.referrer or url_for("julian.subir"))


@julian.route("/cerrar_navegador", methods=["POST"])
def cerrar_navegador():
    _browser_commands.put('close_browser')
    flash("Navegador cerrándose…")
    return redirect(request.referrer or url_for("julian.subir"))


@julian.route("/exportar_reversiones/<sid>")
def exportar_reversiones(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("La sesión expiró. Sube el Excel de nuevo.")
        return redirect(url_for("julian.subir"))

    reversiones = s.get("reversiones_ticket", [])
    if not reversiones:
        flash("No hay materiales con descuadre de líder para reversar.")
        return redirect(url_for("julian.balance_sgs", sid=sid))

    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plantillas", "reversiones_molde.xlsx")
    try:
        wb = openpyxl.load_workbook(template_path)
    except Exception as e:
        log.error(f"[JULIAN] Error abriendo plantilla: {e}")
        flash("Error al leer la plantilla de reversiones.")
        return redirect(url_for("julian.balance_sgs", sid=sid))

    ws = wb.active

    thin_border = Border(
        left=Side(style='thin', color='000000'),
        right=Side(style='thin', color='000000'),
        top=Side(style='thin', color='000000'),
        bottom=Side(style='thin', color='000000')
    )

    start_row = 3
    for i, rev in enumerate(reversiones):
        row_idx = start_row + i

        # PRO (Aca tu nodo)
        cell_pro = ws.cell(row=row_idx, column=2)
        cell_pro.value = rev["nodo"]
        cell_pro.border = thin_border

        # Codigo de material
        cell_cod = ws.cell(row=row_idx, column=3)
        cell_cod.value = rev["codigo"]
        cell_cod.border = thin_border

        # Descripcion de material
        cell_desc = ws.cell(row=row_idx, column=4)
        cell_desc.value = rev["descripcion"]
        cell_desc.border = thin_border

        # cantidad a reversar
        cell_cant = ws.cell(row=row_idx, column=5)
        cell_cant.value = rev["cantidad"]
        cell_cant.border = thin_border

        # Motivo
        cell_mot = ws.cell(row=row_idx, column=6)
        cell_mot.value = rev["motivo"]
        cell_mot.border = thin_border

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)

    return send_file(
        out,
        as_attachment=True,
        download_name=f"Reversiones_Ticket_{sid[:6]}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# ══════════════════════════════════════════════════════════════════════════════
# PROPORCIÓN GENERAL POR PORCENTAJES — rutas de soporte
# ══════════════════════════════════════════════════════════════════════════════

@julian.route("/buscar_partner_cedula", methods=["POST"])
def buscar_partner_cedula():
    """
    AJAX — busca un partner en SGS por cédula (campo vat de res.partner).
    Retorna JSON {ok, nombre, partner_id, vat, advertencia}.
    No requiere sid — consulta directamente el cliente SGS.
    """
    data   = request.get_json() or {}
    cedula = str(data.get("cedula") or "").strip()
    if not cedula:
        return jsonify({"ok": False, "error": "Cédula vacía"}), 400

    tiene_c    = "C" in cedula.upper()
    advertencia = (
        "" if tiene_c
        else f"La cédula '{cedula}' no contiene 'C' — puede no encontrar el registro correcto."
    )

    try:
        c = _sgs()
        partners = c.search_read(
            "res.partner", [["vat", "=", cedula]],
            ["id", "name", "vat"], limit=10,
        )
        if not partners:
            solo_num = cedula.upper().lstrip("C").strip()
            partners = c.search_read(
                "res.partner", [["vat", "ilike", solo_num]],
                ["id", "name", "vat"], limit=10,
            )
        if not partners:
            return jsonify({
                "ok": False,
                "error": f"No se encontró partner con cédula '{cedula}' en SGS.",
                "advertencia": advertencia,
            })

        # Preferir el que tiene C en su VAT (Regla C)
        con_c = [p for p in partners if "C" in str(p.get("vat") or "").upper()]
        partner = con_c[0] if con_c else partners[0]

        return jsonify({
            "ok":        True,
            "nombre":    partner["name"],
            "partner_id": partner["id"],
            "vat":       partner.get("vat") or "",
            "advertencia": advertencia,
        })
    except Exception as ex:
        log.error(f"[BUSCAR CEDULA] {ex}")
        return jsonify({"ok": False, "error": str(ex)}), 500


@julian.route("/calcular_distribucion_pct/<sid>", methods=["POST"])
def calcular_distribucion_pct(sid):
    """
    AJAX — calcula la distribución de prestaciones MO según porcentajes.
    Body JSON: { lideres: [{cedula, nombre, pct}] }
    Guarda el resultado en s["distribucion_pct"] para que liquidar_pct lo use.
    """
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"ok": False, "error": "Sesión no encontrada"}), 404

    data          = request.get_json() or {}
    lideres_input = data.get("lideres") or []

    if not lideres_input:
        return jsonify({"ok": False, "error": "Define al menos un líder."}), 400

    total_pct = sum(float(l.get("pct") or 0) for l in lideres_input)
    if abs(total_pct - 100.0) > 0.5:
        return jsonify({
            "ok": False,
            "error": f"Los porcentajes suman {total_pct:.1f}%, deben ser exactamente 100%.",
        }), 400

    # ── Construir items desde checks_mo OK ────────────────────────────────────
    checks_mo  = s.get("checks_mo") or []
    dist_items = []
    for i, ck in enumerate(checks_mo):
        if not ck.get("ok") or ck.get("ya_liquidada"):
            continue
        row = ck.get("row") or {}
        # Usar pendiente_real si hubo ajuste por sobre-solicitud
        cant = float(
            ck["pendiente_real"] if ck.get("ajuste_pendiente") and ck.get("pendiente_real")
            else row.get("cantidad") or 0
        )
        if cant <= 0:
            continue
        dist_items.append({
            "idx":      i,
            "pid":      ck.get("pid"),
            "act_id":   ck.get("act_id"),
            "act_code": str(row.get("codigo") or "").strip().upper(),
            "pro_code": ck.get("pro_code", ""),
            "pro":      str(row.get("pro") or "").strip(),
            "cantidad": cant,
        })

    if not dist_items:
        return jsonify({"ok": False, "error": "No hay prestaciones MO liquidables en esta sesión."}), 400

    # ── Validar y preparar líderes ────────────────────────────────────────────
    advertencias: list[str] = []
    lideres = []
    for l in lideres_input:
        nombre = str(l.get("nombre") or "").strip()
        cedula = str(l.get("cedula") or "").strip()
        pct    = float(l.get("pct") or 0)
        if not nombre or pct <= 0:
            continue
        if cedula and "C" not in cedula.upper():
            advertencias.append(f"Cédula '{cedula}' sin 'C' — verificar si es el registro correcto.")
        lideres.append({"nombre": nombre, "cedula": cedula, "pct": pct})

    if not lideres:
        return jsonify({"ok": False, "error": "Sin líderes válidos (nombre o porcentaje faltante)."}), 400

    # ── Cargar precios MDO&MAT ────────────────────────────────────────────────
    try:
        from modulos.julian.parser_mdo_mat import get_precios
        precios = get_precios()
    except Exception as ex:
        log.warning(f"[PCT DIST] Precios no disponibles: {ex}")
        precios = {}
        advertencias.append("No se pudieron cargar los precios MDO&MAT — distribución por cantidad.")

    # ── Calcular ──────────────────────────────────────────────────────────────
    from modulos.julian.distribuidor_porcentaje import distribuir_prestaciones
    resultado = distribuir_prestaciones(dist_items, lideres, precios)

    if resultado["errores"] and not any(a.get("n_items", 0) for a in resultado["asignaciones"]):
        return jsonify({"ok": False, "error": resultado["errores"][0]}), 400

    # Guardar en sesión para que liquidar_pct lo recupere
    s["distribucion_pct"] = resultado

    return jsonify({
        "ok": True,
        "distribucion": {
            "total":   resultado["total"],
            "errores": resultado["errores"],
            "asignaciones": [
                {
                    "lider":    a["lider"],
                    "cedula":   a["cedula"],
                    "pct_obj":  a["pct_obj"],
                    "pct_real": a["pct_real"],
                    "target":   a["target"],
                    "valor":    a["valor"],
                    "n_items":  a["n_items"],
                }
                for a in resultado["asignaciones"]
            ],
            "preview": resultado["preview"][:60],
        },
        "advertencias": advertencias + resultado["errores"],
    })


@julian.route("/liquidar_pct/<sid>", methods=["POST"])
def liquidar_pct(sid):
    """
    Lanza la liquidación MO usando la distribución por porcentaje almacenada
    en s['distribucion_pct'].  Usa el mismo _ejecutar_liquidacion_bg que el
    flujo normal — solo cambia cómo se construye la lista de items_mo.
    """
    s = _sesiones.get(sid)
    if not s:
        flash("Sesión no encontrada.")
        return redirect(url_for("julian.subir"))

    distribucion = s.get("distribucion_pct")
    if not distribucion or not distribucion.get("asignaciones"):
        flash("No hay distribución calculada. Calcula la distribución primero.")
        return redirect(url_for("julian.ver_verificacion", sid=sid))

    if not _pdl_obligatorio_ok_j(s):
        flash("Proyecto programado (6300...): el PDL es obligatorio antes de liquidar MO.")
        return redirect(url_for("julian.ver_verificacion", sid=sid))

    if not sesion_activa():
        flash("El browser visible no tiene sesión activa en SGS. Inicia sesión primero.")
        return redirect(url_for("julian.ver_verificacion", sid=sid))

    # ── Aplanar distribución en lista de items MO ─────────────────────────────
    items_mo = []
    for asig in distribucion["asignaciones"]:
        lider_nombre = asig["lider"]
        lider_cedula = asig.get("cedula", "")
        for it in asig.get("items") or []:
            cant = float(it.get("cantidad") or 0)
            if cant <= 0:
                continue
            items_mo.append({
                "tipo":               "MO",
                "idx":                it.get("idx"),
                "pid":                it.get("pid"),
                "act_id":             it.get("act_id"),
                "act_code":           str(it.get("act_code") or ""),
                "pro_code":           str(it.get("pro_code") or ""),
                "cantidad":           cant,
                "partner_nombre":     lider_nombre,
                "partner_cedula":     lider_cedula,
                "cantidad_proyectada": float(it.get("_cant_int") or cant),
            })

    if not items_mo:
        flash("La distribución no generó ítems liquidables.")
        return redirect(url_for("julian.ver_verificacion", sid=sid))

    with _cached_storage_state_lock:
        storage_state = _cached_storage_state
    if not storage_state:
        flash("Estado de sesión no disponible. Espera e intenta de nuevo.")
        return redirect(url_for("julian.ver_verificacion", sid=sid))

    # ── Crear job (mismo esquema que la liquidación normal) ───────────────────
    job_items_mo = [
        {
            "codigo":    it["act_code"],
            "tipo":      "MO",
            "worker_id": "vis.",
            "estado":    "pendiente",
            "error":     None,
        }
        for it in items_mo
    ]
    _trabajos[sid] = {
        "estado":    "corriendo",
        "items_mo":  job_items_mo,
        "items_mat": [],
        "resultados": None,
        "error":     None,
        "t_inicio":  _time_mod.time(),
        "es_pct":    True,
    }

    t = threading.Thread(
        target=_ejecutar_liquidacion_bg,
        args=(sid, items_mo, [], storage_state),
        daemon=True,
    )
    t.start()
    log.info(f"[LIQUIDAR PCT] sid={sid[:8]} — {len(items_mo)} item(s) distribuidos")
    return redirect(url_for("julian.esperar", sid=sid))


# ══════════════════════════════════════════════════════════════════════════════
# MOTOVERIFICADOR — Detección y corrección de nodos incorrectos
# ══════════════════════════════════════════════════════════════════════════════

@julian.route("/motoverificador/<sid>")
def motoverificador(sid):
    """
    Analiza la sesión buscando actividades MO con nodo CABLE que fueron
    liquidadas en nodos individuales incorrectos.

    El verificador ya resolvió cada check CABLE al proyecto individual
    (p.ej. NODO1) porque "1AL4" → nodo_idx=1. Por tanto ck["pid"] apunta
    directamente al nodo incorrecto y ck["pnombre"] tiene su nombre completo.
    Solo hay que leer las activities.details positivas de ese pid.
    """
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("julian.subir"))

    checks_mo    = s.get("checks_mo") or []
    cable_checks = [ck for ck in checks_mo if ck.get("_nodo_cable")]

    analisis  = []
    error_sgs = None
    try:
        c = _sgs()

        for ck in cable_checks:
            pid        = ck.get("pid")
            act_code   = ck.get("row", {}).get("codigo", "")
            pro_code   = ck.get("pro_code", "")
            nodo_excel = ck.get("row", {}).get("pro", "")
            pnombre    = ck.get("pnombre", pro_code)   # nombre completo del proyecto

            info = {
                "act_code":          act_code,
                "pro_code_correcto": pro_code,
                "nodo_excel":        nodo_excel,
                "pnombre_incorrecto": pnombre,
                "pid_incorrecto":    pid,
                "advertencias":      ck.get("advertencias", []),
                "avances_erroneos":  [],
            }

            if not pid or not act_code:
                analisis.append(info)
                continue

            # pid ya apunta al nodo individual (incorrecto) — consulta directa
            acts = c.search_read(
                "project.activities",
                [["project_id", "=", pid],
                 ["activity_code", "=", act_code]],
                ["id", "activity_code", "amount", "advance", "balance"],
                limit=5,
            )

            for a in acts:
                adv = float(a.get("advance") or 0)
                if adv <= 0:
                    continue

                detalles_raw = c.search_read(
                    "project.activities.details",
                    [["project_activities_id", "=", a["id"]]],
                    ["id", "amount", "partner_id", "date"],
                    limit=30,
                )
                detalles = []
                for d in detalles_raw:
                    amt = float(d.get("amount") or 0)
                    if amt <= 0:
                        continue
                    partner_raw = d.get("partner_id")
                    pdet = (
                        partner_raw[1]
                        if isinstance(partner_raw, list)
                        else str(partner_raw or "?")
                    )
                    fecha_raw = str(d.get("date") or "")[:10]
                    parts = fecha_raw.split("-")
                    fecha_pw = (
                        f"{parts[2]}/{parts[1]}/{parts[0]}"
                        if len(parts) == 3 else fecha_raw
                    )
                    detalles.append({
                        "id":             d["id"],
                        "amount":         amt,
                        "partner_nombre": pdet,
                        "fecha_sgs":      fecha_raw,
                        "fecha_pw":       fecha_pw,
                    })

                if detalles:
                    info["avances_erroneos"].append({
                        "pid_incorrecto":      pid,
                        "pro_code_incorrecto": pro_code,
                        "nombre_nodo":         pnombre,
                        "act_id":              a["id"],
                        "advance":             adv,
                        "amount_proyectado":   float(a.get("amount") or 0),
                        "detalles":            detalles,
                        # total a revertir = suma de detalles positivos
                        "total_revertir":      sum(d["amount"] for d in detalles),
                    })

            # ── Nodo CABLE destino: buscar en df_nodos del check ──────────────
            from modulos.verificador import _es_nodo_cable_sgs
            df_nodos = ck.get("df_nodos") or []
            cable_targets = [
                n for n in df_nodos
                if _es_nodo_cable_sgs(n.get("name", ""))
            ]
            info["cable_target"] = cable_targets[0] if cable_targets else None

            analisis.append(info)

    except Exception as ex:
        error_sgs = str(ex)
        log.error(f"[MOTO] Error consultando SGS: {ex}")

    return render_template(
        "julian/motoverificador.html",
        sid=sid, s=s,
        analisis=analisis,
        cable_checks_total=len(cable_checks),
        error_sgs=error_sgs,
    )


@julian.route("/ejecutar_rollback_mo/<sid>", methods=["POST"])
def ejecutar_rollback_mo(sid):
    """
    Ejecuta rollback de avance MO en nodo incorrecto.

    Form params:
      pid_incorrecto     — pid del nodo donde SE liquidó (incorrecto)
      act_id             — id de la actividad en ese nodo
      act_code           — código QP de la actividad
      cantidad_restar    — cantidad positiva a restar
      partner_nombre     — nombre del líder original
      fecha_original_pw  — fecha DD/MM/YYYY de la liquidación original
      proyectado_actual  — amount actual de la actividad
      pro_code_incorrecto — código PRO del nodo incorrecto
    """
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"ok": False, "error": "Sesion no encontrada"}), 404

    form = request.form
    try:
        pid_incorrecto    = int(form["pid_incorrecto"])
        act_id            = int(form["act_id"])
        act_code          = form["act_code"].strip()
        cantidad_restar   = float(form["cantidad_restar"])
        partner_nombre    = form["partner_nombre"].strip()
        fecha_original_pw = form["fecha_original_pw"].strip()
        proyectado_actual = float(form["proyectado_actual"])
        pro_code_incorrecto = form.get("pro_code_incorrecto", "").strip()
    except (KeyError, ValueError) as ex:
        return jsonify({"ok": False, "error": f"Parámetro inválido: {ex}"}), 400

    log.info(
        f"[MOTO] Rollback solicitado: pid={pid_incorrecto} act={act_code} "
        f"qty={cantidad_restar} partner={partner_nombre} fecha={fecha_original_pw}"
    )

    storage_state = s.get("storage_state")

    try:
        from browser_client import PlaywrightClient
        pw = PlaywrightClient(headless=True, slow_mo=100)
        if storage_state:
            pw.start(storage_state=storage_state)
        else:
            pw.start()
            pw.login()
        try:
            c = _sgs()
            res = pw.restar_avance_mo(
                pid               = pid_incorrecto,
                act_code          = act_code,
                cantidad_restar   = cantidad_restar,
                partner_nombre    = partner_nombre,
                fecha_original    = fecha_original_pw,
                pro_code          = pro_code_incorrecto,
                proyectado_actual = proyectado_actual,
                act_id            = act_id,
                sgs_client        = c,
                dry_run           = False,
            )
        finally:
            pw.close()
    except Exception as ex:
        log.error(f"[MOTO] Error en rollback: {ex}")
        return jsonify({"ok": False, "error": str(ex)}), 500

    if res.get("ok"):
        log.info(f"[MOTO] Rollback OK: {act_code} pid={pid_incorrecto}")
    else:
        log.error(f"[MOTO] Rollback FAIL: {res.get('error')}")

    return jsonify(res)


@julian.route("/ejecutar_rollback_lote_mo/<sid>", methods=["POST"])
def ejecutar_rollback_lote_mo(sid):
    """
    Flujo completo de corrección de nodo MO (Playwright):
      1. Resta todos los avances incorrectos en pid_incorrecto (NODO1).
      2. Para cada líder, liquida en pid_cable (NODO//CABLE) usando crear_y_liquidar_mo.

    Form params:
      pid_incorrecto   — pid del nodo donde SE liquidó (incorrecto)
      act_id           — id de la actividad en el nodo incorrecto
      act_code         — código QP
      pro_code         — nombre/código del nodo incorrecto
      proyectado_actual — amount actual en el nodo incorrecto
      detalles_json    — [{partner_nombre, cantidad_restar, fecha}]
      pid_cable        — pid del nodo CABLE correcto (opcional)
      pro_code_cable   — nombre del proyecto CABLE (para display)
    """
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"ok": False, "error": "Sesion no encontrada"}), 404

    form = request.form
    try:
        pid_incorrecto    = int(form["pid_incorrecto"])
        act_id            = int(form["act_id"])
        act_code          = form["act_code"].strip()
        pro_code          = form.get("pro_code", "").strip()
        proyectado_actual = float(form["proyectado_actual"])
        detalles          = json.loads(form["detalles_json"])
        pid_cable         = int(form["pid_cable"]) if form.get("pid_cable") else None
        pro_code_cable    = form.get("pro_code_cable", "").strip()
    except (KeyError, ValueError) as ex:
        return jsonify({"ok": False, "error": f"Parámetro inválido: {ex}"}), 400

    if not detalles:
        return jsonify({"ok": False, "error": "detalles_json vacío"}), 400

    log.info(
        f"[MOTO] Corrección nodo: pid_inc={pid_incorrecto} → pid_cable={pid_cable} "
        f"act={act_code} n_lineas={len(detalles)}"
    )

    storage_state = s.get("storage_state")
    try:
        from browser_client import PlaywrightClient
        pw = PlaywrightClient(headless=True, slow_mo=100)
        if storage_state:
            pw.start(storage_state=storage_state)
        else:
            pw.start()
            pw.login()
        try:
            c = _sgs()

            # ── Paso 1: Restar avances en nodo incorrecto (Playwright modal) ──
            res = pw.restar_avance_mo_lote(
                pid               = pid_incorrecto,
                act_code          = act_code,
                detalles          = detalles,
                pro_code          = pro_code,
                proyectado_actual = proyectado_actual,
                act_id            = act_id,
                sgs_client        = c,
                dry_run           = False,
            )

            # ── Paso 2: Liquidar en nodo CABLE correcto (Playwright modal) ───
            if res.get("ok") and pid_cable:
                liq_resultados = []
                for det in detalles:
                    liq = pw.crear_y_liquidar_mo(
                        pid            = pid_cable,
                        act_code       = act_code,
                        cantidad       = float(det["cantidad_restar"]),
                        partner_nombre = det["partner_nombre"],
                        sgs_client     = c,
                        dry_run        = False,
                    )
                    liq_resultados.append({
                        "partner": det["partner_nombre"],
                        "cantidad": det["cantidad_restar"],
                        "ok": liq.get("ok"),
                        "error": liq.get("error"),
                    })
                    if not liq.get("ok"):
                        log.error(f"[MOTO] Liquidación CABLE falló: {liq.get('error')}")
                res["liq_cable"]    = liq_resultados
                res["pro_cable"]    = pro_code_cable
                liq_ok = sum(1 for l in liq_resultados if l["ok"])
                res["msg"] = (
                    f"{act_code}: {res.get('lineas_revertidas',0)} líneas revertidas en {pro_code} "
                    f"+ {liq_ok}/{len(detalles)} liquidadas en {pro_code_cable}"
                )
            elif res.get("ok") and not pid_cable:
                res["msg"] += " (sin nodo CABLE destino — re-liquidar manualmente)"

        finally:
            pw.close()
    except Exception as ex:
        log.error(f"[MOTO] Error corrección nodo: {ex}")
        return jsonify({"ok": False, "error": str(ex)}), 500

    if res.get("ok"):
        log.info(f"[MOTO] Rollback lote OK: {act_code}")
    else:
        log.error(f"[MOTO] Rollback lote FAIL: {res.get('error')}")

    return jsonify(res)
