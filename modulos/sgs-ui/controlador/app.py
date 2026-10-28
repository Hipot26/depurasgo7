"""
SGS UI â€” App Flask local
========================
Sistema de verificaciÃ³n y liquidaciÃ³n de MO + MAT.
Fase 1: solo verificaciÃ³n (SOLO LECTURA de SGS).
Fase 2: liquidaciÃ³n via Playwright (pendiente).
"""

import io
import logging
import os
import queue as _queue_mod
import re
import sys
import threading
import time as _time_mod
import uuid
from datetime import datetime

import openpyxl
from flask import (Flask, render_template, request,
                   redirect, url_for, flash, jsonify, send_from_directory)

# MÃ³dulos propios
from sgs_readonly_client import SGSReadOnlyClient
from modulos.parser_excel import parsear_mo, parsear_mat, extraer_total_mo, extraer_pdl_programado
from modulos.verificador  import verificar_fila_mo, verificar_materiales_proyecto
from browser_client       import (PlaywrightClient,
                                   BrowserActionError,
                                   _safe as _safe_str,
                                   _clasificar_error,
                                   liquidar_mo_parallel_por_proyecto,
                                   liquidar_materiales_lista_modal_parallel)

# â”€â”€ Feature flag: pool v2 (Auxiliar + Worker) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# True  -> usa v2 con preloaders + semaforos por proyecto (experimental)
# False -> usa v1 validado: 2 workers max, stagger 8s, sin preloaders.
USE_POOL_V2 = False
MO_MAX_PROJECT_WORKERS = 2

# â”€â”€ App â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app = Flask(__name__, template_folder="templates", static_folder="static")

# ── SGO7: trazabilidad — todo archivo subido, proceso o descarga queda registrado en sabogal ──
try:
    import sgo7_registro
    sgo7_registro.instalar(app, modulo="liquidador", ignorar=("/liquidador2/diagnostico",), acciones={
            r"/liquidador2/subir": "liquidador_subir_excel",
            r"/liquidador2/aprobar": "liquidador_aprobar",
            r"/liquidador2/liquidar": "liquidador_liquidar",
            r"/liquidador2/crear_nodo": "liquidador_crear_nodo",
            r"/liquidador2/cierre_rapido": "liquidador_cierre",
            r"/liquidador2/(panel_pdl|aplicar_pdl|parchear_pdl)": "liquidador_pdl",
            r"/liquidador2/reparar": "liquidador_reparar",
            r"/liquidador2/browser_ctrl": "liquidador_navegador",
            r"/liquidador2/cancelar": "liquidador_cancelar",
            r"/liquidador2/distribucion": "liquidador_distribucion_ia",
            r"/liquidador2/agregar_lider": "liquidador_lider",
            r"/(liquidador2/)?reiniciar_servidor": "reiniciar_servidor"
    })
except Exception as _e_sgo7:
    print("sgo7_registro no instalado:", _e_sgo7)
app.secret_key = os.environ.get("SECRET_KEY", "")

# ── Blueprint: Presupuesto (liquidador de primera generación, módulo julian) ──
from modulos.julian.blueprint import julian
app.register_blueprint(julian)

# ── Blueprint: Liquidación lote multi-DF (módulo julian) ─────────────────────
from modulos.julian.blueprint_lote import julian_lote
app.register_blueprint(julian_lote)

# ── Blueprint: Acción Manual (entrada sin Excel) ───────────────────────────────
from modulos.julian.accion_manual import julian_manual
app.register_blueprint(julian_manual)

# ── Blueprint: Liquidador2 (segundo liquidador independiente) ────────────────────────
from modulos.liquidador2.blueprint import liquidador2
app.register_blueprint(liquidador2)

# ── Blueprint: Motoverificador (cambio de líder en actividades MO) ────────────────
from modulos.motoverificador.blueprint import motoverificador_bp
app.register_blueprint(motoverificador_bp)

# ── Blueprint: Inyector de Fechas (repara nodos liquidados sin Fecha Fin) ─────
from modulos.inyectorfechas.blueprint import inyectorfechas
app.register_blueprint(inyectorfechas)

# â”€â”€ Logger â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/app.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
log = logging.getLogger("app")

# â”€â”€ SGS client (cache) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
SGS_URL      = os.environ.get("SGS_URL",      "")
SGS_DB       = os.environ.get("SGS_DB",       "")
SGS_LOGIN    = os.environ.get("SGS_LOGIN",    "")
SGS_PASSWORD = os.environ.get("SGS_PASSWORD", "")

_sgs_cache: SGSReadOnlyClient = None

def _sgs() -> SGSReadOnlyClient:
    global _sgs_cache
    if _sgs_cache and _sgs_cache.uid:
        try:
            _sgs_cache.search("res.users", [["id","=",_sgs_cache.uid]], limit=1)
            return _sgs_cache
        except Exception:
            pass
    c = SGSReadOnlyClient(SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD)
    c.autenticar()
    _sgs_cache = c
    return c


def _nuevo_sgs_readonly() -> SGSReadOnlyClient:
    c = SGSReadOnlyClient(SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD)
    c.autenticar()
    return c

# â”€â”€ Sesiones en memoria â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_sesiones: dict = {}

# â”€â”€ Trabajos de liquidaciÃ³n en curso â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_trabajos: dict = {}


def _codigo_programado_desde_nombre(filename: str) -> str:
    m = re.search(r"\b(6300\d{4,})\b", filename or "")
    return m.group(1) if m else ""

# â”€â”€ Playwright persistente â€” monitor thread + command queue â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# REGLA: Flask solo escribe en _browser_commands.
# El monitor (mismo greenlet que Playwright) lee y ejecuta.
_pw_global:                PlaywrightClient = None
_cached_storage_state:     dict             = None
_cached_storage_state_lock = threading.Lock()
_browser_commands: _queue_mod.Queue = _queue_mod.Queue()
_monitor_running = False

SGS_URL_PW = os.environ.get("SGS_URL", "")
SGS_DB_PW  = "sgsdominion"


def sesion_activa() -> bool:
    return bool(_pw_global and _pw_global._logueado)


def _monitor_loop():
    global _cached_storage_state, _pw_global, _monitor_running
    _monitor_running = True
    log.info("[MONITOR] Loop iniciado")

    while True:
        _time_mod.sleep(4)
        try:
            # Procesar comandos
            try:
                cmd = _browser_commands.get_nowait()
                log.info(f"[MONITOR] Comando: {cmd}")
                if cmd == "restart_browser":
                    if _pw_global:
                        try: _pw_global.close()
                        except: pass
                        _pw_global = None
                    try:
                        _pw_global = PlaywrightClient(headless=False)
                        _pw_global.start()
                        _pw_global.open_to_login_page()
                        log.info("[MONITOR] Browser reiniciado â€” esperando login")
                    except Exception as ex:
                        log.error(f"[MONITOR] Error reiniciando browser: {ex}")
                        _pw_global = None
                elif cmd == "close_browser":
                    if _pw_global:
                        try: _pw_global.close()
                        except: pass
                        _pw_global = None
                    log.info("[MONITOR] Browser cerrado")
                elif cmd == "force_session":
                    if _pw_global and _pw_global._page:
                        try:
                            state = _pw_global._page.context.storage_state()
                            with _cached_storage_state_lock:
                                _cached_storage_state = state
                            _pw_global._logueado = True
                            log.info("[MONITOR] force_session: storage_state exportado")
                        except Exception as ex:
                            log.error(f"[MONITOR] force_session fallÃ³: {ex}")
                elif cmd == "reverificar":
                    if _pw_global:
                        _pw_global._logueado = False
                    with _cached_storage_state_lock:
                        _cached_storage_state = None
            except _queue_mod.Empty:
                pass

            # Verificar sesiÃ³n â€” URL primero, luego selectores DOM
            if _pw_global and _pw_global._page:
                url_ok = False
                try:
                    cur_url = _pw_global._page.url
                    if SGS_URL_PW in cur_url and "/web" in cur_url and "/login" not in cur_url:
                        url_ok = True
                except Exception:
                    pass

                dom_ok = False
                if not url_ok:
                    for sel in [".o_main_navbar", ".o_home_menu_navbar", ".o_user_menu",
                                ".o_action_manager", ".o_web_client", ".o_app_icon"]:
                        try:
                            if _pw_global._page.locator(sel).first.is_visible(timeout=1_500):
                                dom_ok = True
                                break
                        except Exception:
                            continue

                if url_ok or dom_ok:
                    try:
                        state = _pw_global._page.context.storage_state()
                        with _cached_storage_state_lock:
                            _cached_storage_state = state
                        _pw_global._logueado = True
                    except Exception:
                        pass
                else:
                    _pw_global._logueado = False
                    with _cached_storage_state_lock:
                        _cached_storage_state = None

        except Exception as ex:
            log.warning(f"[MONITOR] ExcepciÃ³n: {ex}")


threading.Thread(target=_monitor_loop, daemon=True, name="pw-monitor").start()

# â”€â”€ Rutas â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/")
def index():
    return redirect(url_for("subir"))


@app.route("/subir", methods=["GET", "POST"])
def subir():
    if request.method == "POST":
        f = request.files.get("excel")
        if not f or not f.filename.endswith((".xlsx", ".xlsm", ".xls")):
            flash("Sube un archivo Excel (.xlsx)")
            return redirect(url_for("subir"))

        try:
            wb = openpyxl.load_workbook(io.BytesIO(f.read()), data_only=True)
        except Exception as ex:
            flash(f"Error leyendo Excel: {ex}")
            return redirect(url_for("subir"))

        sid = str(uuid.uuid4())

        rows_mo,  errs_mo  = parsear_mo(wb)
        rows_mat, errs_mat = parsear_mat(wb)
        total_mo = extraer_total_mo(wb)
        pdl_detectado = extraer_pdl_programado(wb)

        lcl_filename = _codigo_programado_desde_nombre(f.filename)
        if lcl_filename:
            for row in rows_mo:
                row["_odm"] = lcl_filename
            for row in rows_mat:
                row["_odm"] = lcl_filename
            errs_mo.append(f"[INFO] Programado detectado por nombre de archivo â€” LCL: {lcl_filename}")

        _sesiones[sid] = {
            "filename":  f.filename,
            "rows_mo":   rows_mo,
            "rows_mat":  rows_mat,
            "errs_mo":   errs_mo,
            "errs_mat":  errs_mat,
            "total_mo":  total_mo,
            "pdl_detectado": pdl_detectado,
            "checks_mo": [],
            "checks_mat":[],
            "verificado_at": None,
            "creado_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

        log.info(f"Excel cargado | sid={sid[:8]} | {f.filename} | "
                 f"MO={len(rows_mo)} MAT={len(rows_mat)}")

        return redirect(url_for("verificar", sid=sid))

    return render_template("subir.html")


@app.route("/verificar/<sid>")
def verificar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada. Sube el Excel de nuevo.")
        return redirect(url_for("subir"))

    # Conectar a SGS
    try:
        c = _sgs()
    except Exception as ex:
        flash(f"Error conectando a SGS: {ex}")
        return render_template("subir.html")

    # Verificar MO
    checks_mo = []
    for row in s["rows_mo"]:
        try:
            ck = verificar_fila_mo(c, row)
        except Exception as ex:
            ck = {"row": row, "ok": False, "errores": [str(ex)], "advertencias": []}
        checks_mo.append(ck)

    # Verificar MAT â€” agrupar por proyecto para hacer bulk queries
    checks_mat = []
    if s["rows_mat"]:
        # Resolver proyectos Ãºnicos por nodo
        nodos_unicos = {}
        # Obtener odm del primer row_mo (todos comparten el mismo DF)
        odm_global = s["rows_mo"][0].get("_odm", "") if s["rows_mo"] else ""

        for mat in s["rows_mat"]:
            nodo = mat["nodo"]
            if nodo not in nodos_unicos:
                pid = _resolver_pid_nodo(c, nodo, s["rows_mo"], checks_mo, odm_global)
                nodos_unicos[nodo] = pid

        for mat in s["rows_mat"]:
            nodo = mat["nodo"]
            pid  = nodos_unicos.get(nodo)
            if pid:
                # Se hace en bulk por proyecto mÃ¡s abajo
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
        resultados_por_pid = {}
        for pid in pids_unicos:
            rows_pid = [ck["mat"] for ck in checks_mat if ck.get("pid") == pid]
            try:
                res = verificar_materiales_proyecto(c, pid, rows_pid)
                resultados_por_pid[pid] = {r["mat"]["codigo"]: r for r in res}
            except Exception as ex:
                log.error(f"Error verificando MAT pid={pid}: {ex}")

        # Leer PRO code + nombre para cada pid resuelto
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
            log.warning(f"No se pudo leer info de proyectos MAT: {ex}")

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
                        "mat": ck["mat"], "ok": False,
                        "estado": "no_encontrado",
                        "pro_code": info.get("pro_code", ""),
                        "pnombre":  info.get("pnombre", ""),
                        "errores": ["No encontrado en project.material"],
                        "advertencias": [],
                    })
            else:
                checks_mat_final.append(ck)
        checks_mat = checks_mat_final

    s["checks_mo"]  = checks_mo
    s["checks_mat"] = checks_mat
    s["verificado_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Resumen
    def _resumen(checks):
        ok      = sum(1 for c in checks if c.get("ok"))
        error   = sum(1 for c in checks if not c.get("ok"))
        ya_liq  = sum(1 for c in checks if c.get("estado") == "ya_liquidado" or c.get("ya_liquidada"))
        return {"ok": ok, "error": error, "ya_liquidado": ya_liq, "total": len(checks)}

    # Nodos faltantes — deduplicados por (df_id, nodo_val)
    _nf_seen = {}
    for _ck in checks_mo:
        if _ck.get("nodo_faltante"):
            _key = (_ck["nodo_faltante_df"], _ck["nodo_faltante_val"])
            if _key not in _nf_seen:
                _nf_seen[_key] = {
                    "df_id":    _ck["nodo_faltante_df"],
                    "nodo_val": _ck["nodo_faltante_val"],
                    "nodo_sgs_disponibles": _ck.get("nodo_sgs_disponibles", []),
                }
    nodos_faltantes = list(_nf_seen.values())

    return render_template("verificacion.html",
        sid=sid, s=s,
        checks_mo=checks_mo,
        checks_mat=checks_mat,
        resumen_mo=_resumen(checks_mo),
        resumen_mat=_resumen(checks_mat),
        mo_lider_grupos=_grupos_lideres_mo(checks_mo),
        nodos_faltantes=nodos_faltantes,
    )


@app.route("/crear_nodo/<sid>", methods=["POST"])
def crear_nodo(sid):
    s = _sesiones.get(sid) or {}
    if not s:
        flash("Sesion no encontrada — sube el Excel de nuevo.")
        return redirect(url_for("subir"))

    df_id    = request.form.get("df_id",    "").strip()
    nodo_val = request.form.get("nodo_val", "").strip()
    pdl_val  = request.form.get("pdl_val",  "").strip() or "-"
    dry_run  = request.form.get("dry_run", "1") != "0"

    if not df_id or not nodo_val:
        flash("Faltan datos para crear el nodo.")
        return redirect(url_for("verificar", sid=sid))

    # ── Consultar SGS: obtener parent_id y campos del hermano ────────────────
    try:
        c = _sgs()
        campos = ["id", "code", "name", "nodo", "parent_id",
                  "contract_id", "department_id", "technology_id",
                  "bandeja_approver_ids", "order_number"]
        todos = c.search_read(
            "project.dominion", [["order_number", "=", df_id]], campos, limit=60
        )
        if not todos:
            flash(f"DF '{df_id}' no encontrado en SGS. Verifique que el proyecto padre exista.")
            return redirect(url_for("verificar", sid=sid))

        # Padre: el que no tiene parent_id (proyecto raíz del DF)
        padre = next((p for p in todos if not p.get("parent_id")), None)
        # Hermano: cualquier hijo existente para clonar campos
        hermano = next((p for p in todos if p.get("parent_id")), todos[0])

        if not padre:
            # Buscar el padre por ID desde un hermano
            ref = hermano.get("parent_id")
            if isinstance(ref, list) and ref:
                filas = c.search_read(
                    "project.dominion", [["id", "=", ref[0]]], campos, limit=1
                )
                padre = filas[0] if filas else None

        if not padre:
            flash(f"No se encontro el proyecto padre para DF '{df_id}'.")
            return redirect(url_for("verificar", sid=sid))

        parent_pid = padre["id"]

        def _m2o(val):
            if isinstance(val, list) and len(val) > 1:
                return str(val[1])
            return str(val or "")

        contract_name   = _m2o(hermano.get("contract_id"))
        department_name = _m2o(hermano.get("department_id"))
        technology_name = _m2o(hermano.get("technology_id")) or "PROYECTO"

        approver_name = ""
        approver_ids  = hermano.get("bandeja_approver_ids") or []
        if approver_ids:
            partners = c.search_read(
                "res.partner", [["id", "=", approver_ids[0]]], ["name"], limit=1
            )
            if partners:
                approver_name = partners[0].get("name", "")

        # Verificar que el nodo no exista ya (Regla R5)
        ya_existe = any(str(p.get("nodo", "")) == str(nodo_val) for p in todos if p.get("parent_id"))
        if ya_existe:
            flash(f"El nodo '{nodo_val}' ya existe en SGS para DF '{df_id}'. No se crea duplicado.")
            return redirect(url_for("verificar", sid=sid))

    except Exception as ex:
        flash(f"Error consultando SGS para crear nodo: {ex}")
        return redirect(url_for("verificar", sid=sid))

    # ── Ejecutar en browser ───────────────────────────────────────────────────
    nombre_nodo = f"{df_id} NODO {nodo_val}"
    try:
        pw = PlaywrightClient(headless=False, slow_mo=300)
        pw.start()
        pw.login()
        resultado = pw.crear_proyecto_hijo(
            parent_id=parent_pid,
            nombre=nombre_nodo,
            nodo=nodo_val,
            ticket=df_id,
            pdl=pdl_val,
            contract_id_name=contract_name,
            department_id_name=department_name,
            technology_id_name=technology_name,
            bandeja_approver_name=approver_name,
            dry_run=dry_run,
            screenshots=True,
        )
        if dry_run:
            # Dejar el navegador abierto para que el operador revise el formulario
            pass
        else:
            pw.close()
    except BrowserActionError as ex:
        flash(f"Error del browser creando nodo '{nodo_val}': {ex}")
        return redirect(url_for("verificar", sid=sid))
    except Exception as ex:
        flash(f"Error inesperado creando nodo: {ex}")
        return redirect(url_for("verificar", sid=sid))

    if dry_run:
        flash(
            f"DRY RUN: formulario del nodo '{nodo_val}' (DF {df_id}) llenado en el browser. "
            f"Revisa que todo sea correcto y luego usa 'Crear (real)' para guardar."
        )
    else:
        status   = resultado.get("status", "?")
        nuevo_id = resultado.get("id")
        if status == "ok":
            flash(
                f"Nodo '{nodo_val}' creado en SGS (id={nuevo_id}). "
                f"Vuelve a verificar para confirmar que aparece."
            )
        else:
            flash(
                f"Nodo guardado — verificar manualmente en SGS (status={status}). "
                f"El registro puede haberse creado aunque no se detectó el ID."
            )

    return redirect(url_for("verificar", sid=sid))


@app.route("/agregar_lider/<sid>", methods=["POST"])
def agregar_lider(sid):
    """Agrega el líder de un nodo hermano al proyecto que no tiene equipo asignado."""
    s = _sesiones.get(sid) or {}
    if not s:
        flash("Sesion no encontrada — sube el Excel de nuevo.")
        return redirect(url_for("subir"))

    pid      = request.form.get("pid", "").strip()
    df_id    = request.form.get("df_id", "").strip()
    pro_code = request.form.get("pro_code", "").strip()

    if not pid or not pid.isdigit():
        flash("Datos insuficientes para agregar el líder.")
        return redirect(url_for("verificar", sid=sid))
    pid = int(pid)

    # ── Buscar líder en un nodo hermano del mismo DF ──────────────────────────
    try:
        c = _sgs()
        hermanos = c.search_read(
            "project.dominion",
            [["order_number", "=", df_id]],
            ["id", "code"],
            limit=20,
        )
        partner_nombre = ""
        partner_cedula = ""
        for h in hermanos:
            if h["id"] == pid:
                continue   # saltar el propio proyecto
            team = c.search_read(
                "project.team",
                [["project_id", "=", h["id"]]],
                ["partner_id"],
                limit=1,
            )
            if team and team[0].get("partner_id"):
                partner_nombre = team[0]["partner_id"][1]
                pid_partner    = team[0]["partner_id"][0]
                vat_rows = c.search_read(
                    "res.partner", [["id", "=", pid_partner]], ["vat"], limit=1
                )
                partner_cedula = (vat_rows[0].get("vat") or "") if vat_rows else ""
                break

        if not partner_nombre:
            flash(f"No se encontró líder en ningún nodo hermano del DF '{df_id}'. "
                  f"Asigna el líder manualmente en SGS.")
            return redirect(url_for("verificar", sid=sid))

    except Exception as ex:
        flash(f"Error consultando SGS para obtener el líder: {ex}")
        return redirect(url_for("verificar", sid=sid))

    # ── Agregar líder via browser ─────────────────────────────────────────────
    try:
        with PlaywrightClient(headless=False, slow_mo=300) as pw:
            pw.login()
            pw._navegar_proyecto(pid, pro_code=pro_code)
            pw._asegurar_lider_en_equipo(
                pid=pid,
                partner_nombre=partner_nombre,
                partner_cedula=partner_cedula,
                pro_code=pro_code,
                sgs_client=c,
            )
    except BrowserActionError as ex:
        flash(f"Error del browser al agregar líder: {ex}")
        return redirect(url_for("verificar", sid=sid))
    except Exception as ex:
        flash(f"Error inesperado al agregar líder: {ex}")
        return redirect(url_for("verificar", sid=sid))

    flash(
        f"✓ Líder '{partner_nombre}' agregado al equipo de {pro_code}. "
        f"La verificación se actualizará — ahora puedes liquidar."
    )
    return redirect(url_for("verificar", sid=sid))


def _resolver_pid_nodo(c, nodo_str, rows_mo, checks_mo, odm="") -> int | None:
    """Obtiene el pid del nodo. Primero desde checks_mo, luego buscando en SGS."""
    from modulos.verificador import (
        _cable_nodo_match,
        _filtrar_no_linea_viva,
        _nodo_en_rango_cable,
        _nodo_equiv,
        _nodo_match,
        _nodo_num,
        buscar_proyectos_por_df_nombre,
    )
    import re as _re
    # Detecta si nodo_str es un rango (ej: "1-6", "1AL6") por presencia de 2+ números.
    _nodo_es_rango = len(_re.findall(r'\d+', str(nodo_str or ""))) >= 2

    # 1. Desde checks_mo ya verificados (la fuente mÃ¡s fiable)
    for row, ck in zip(rows_mo, checks_mo):
        if str(row.get("pro","")) == str(nodo_str) and ck.get("pid"):
            return ck["pid"]

    # 2. Buscar via order_number + nodo (mismo flujo que verificador_mo)
    if odm:
        try:
            candidatos = c.search_read(
                "project.dominion",
                [["order_number", "=", odm]],
                ["id","code","name","nodo","department_id"], limit=60,
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
                # fallback: unico resultado, solo si el Excel no trae nodo
                if len(candidatos) == 1 and not str(nodo_str or "").strip():
                    return candidatos[0]["id"]
        except Exception as ex:
            log.warning(f"_resolver_pid_nodo odm={odm} nodo={nodo_str}: {ex}")

        try:
            nodo_idx = _nodo_num(nodo_str)
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
            matches = [
                p for p in candidatos
                if _nodo_equiv(p.get("nodo", ""), nodo_str)
                or (nodo_idx != -1 and _nodo_match(p.get("name", ""), nodo_idx))
                or (_nodo_es_rango and _cable_nodo_match(p.get("name", ""), nodo_str))
            ]
            if not matches and nodo_idx != -1:
                # Sin proyecto individual: el nodo puede pertenecer solo al
                # proyecto CABLE que tiende el tramo entre dos nodos
                # (ej. nodo Excel='18', proyecto SGS='N17_N18'). Solo se usa
                # cuando no hay ningun candidato individual (prioridad al
                # proyecto especifico si existe).
                matches = [
                    p for p in candidatos
                    if _nodo_en_rango_cable(p.get("name", ""), nodo_idx)
                ]
                if matches:
                    log.warning(
                        f"_resolver_pid_nodo: nodo={nodo_str} sin proyecto individual — "
                        f"usando proyecto CABLE {matches[0].get('code')} ({matches[0].get('name')})"
                    )
            if len(matches) == 1:
                return matches[0]["id"]
            if len(matches) > 1:
                log.warning(
                    f"_resolver_pid_nodo: {len(matches)} candidatos para odm={odm} "
                    f"nodo={nodo_str}. Se usa {matches[0].get('code')}"
                )
                return matches[0]["id"]
        except Exception as ex:
            log.warning(f"_resolver_pid_nodo nombre odm={odm} nodo={nodo_str}: {ex}")

    return None


def _items_mo_desde_form(checks_mo, idx_mo, form):
    items_mo = []
    errores = []

    for idx in idx_mo:
        if idx >= len(checks_mo):
            continue
        ck = checks_mo[idx]
        if not ck.get("ok") or ck.get("ya_liquidada"):
            continue

        lideres = ck.get("lideres") or []
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

        if len(lideres) <= 1:
            partner = (lideres[0]["nombre"] if lideres else ck.get("partner_nombre", ""))
            if not partner:
                errores.append(f"{base['act_code']}: no tiene lider disponible.")
                continue
            items_mo.append({
                **base, "cantidad": cantidad_total, "partner_nombre": partner,
                "partner_cedula": cedula_por_nombre.get(partner, ""),
                "cantidad_proyectada": cantidad_total,
            })
            continue

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

        if modo not in ("uno", "dos"):
            errores.append(f"{base['act_code']}: elige si liquidar a un lider o repartir entre dos.")
            continue
        if lider_a not in nombres_validos:
            errores.append(f"{base['act_code']}: selecciona el lider principal.")
            continue

        if modo == "uno":
            items_mo.append({
                **base, "cantidad": cantidad_total, "partner_nombre": lider_a,
                "partner_cedula": cedula_por_nombre.get(lider_a, ""),
                "cantidad_proyectada": cantidad_total,
            })
            continue

        if lider_b not in nombres_validos or lider_b == lider_a:
            errores.append(f"{base['act_code']}: selecciona dos lideres distintos.")
            continue
        pct_a = form.get(f"mo_pct_a_{idx}", type=float)
        if pct_a is None:
            pct_a = form.get(f"mo_group_pct_{grupo_key}", type=float)
        if pct_a is None or pct_a <= 0 or pct_a >= 100:
            errores.append(f"{base['act_code']}: el porcentaje del lider A debe estar entre 1 y 99.")
            continue

        cant_a = round(cantidad_total * pct_a / 100.0, 4)
        cant_b = round(cantidad_total - cant_a, 4)
        if cant_a <= 0 or cant_b <= 0:
            errores.append(f"{base['act_code']}: el reparto genera cantidades en cero.")
            continue
        items_mo.append({
            **base, "cantidad": cant_a, "partner_nombre": lider_a,
            "partner_cedula": cedula_por_nombre.get(lider_a, ""),
            "cantidad_proyectada": cantidad_total,
            "split": {"parte": "A", "pct": pct_a, "total": cantidad_total},
        })
        items_mo.append({
            **base, "cantidad": cant_b, "partner_nombre": lider_b,
            "partner_cedula": cedula_por_nombre.get(lider_b, ""),
            "cantidad_proyectada": cantidad_total,
            "split": {"parte": "B", "pct": round(100.0 - pct_a, 4), "total": cantidad_total},
        })

    return items_mo, errores


_MAT_QTY_TOL = 0.001
_MAT_ESTADOS_LIQUIDABLES = {"ok", "avance_parcial"}


def _f4(value) -> float:
    return round(float(value or 0), 4)


def _mat_es_liquidable(mc: dict) -> bool:
    """Capa 1 completada en verificacion: MAT ok/parcial sin descuadre, con pm_id."""
    if not mc or not mc.get("ok") or not mc.get("pm_id"):
        return False
    if mc.get("estado") not in _MAT_ESTADOS_LIQUIDABLES:
        return False
    if str(mc.get("mat", {}).get("estado_excel", "")).upper() == "DEVOLUCION":
        return False
    cantidad, err = _mat_cantidad_excel_pendiente(mc)
    if err or cantidad is None or cantidad <= _MAT_QTY_TOL:
        return False
    return True


def _mat_cantidad_excel_pendiente(mc: dict, advance_actual=None) -> tuple[float | None, str | None]:
    """
    Cantidad autorizada para MAT = Excel verificado - avance real.
    SGS puede confirmar o bloquear, pero nunca reemplaza la cantidad del Excel.
    """
    mat = mc.get("mat", {})
    excel_neto = _f4(mat.get("sgs_neto_excel"))
    sgs_neto = mc.get("sgs_neto")
    if sgs_neto is not None and abs(_f4(sgs_neto) - excel_neto) > _MAT_QTY_TOL:
        return None, f"Excel SGS Neto={excel_neto:g} no coincide con SGS Real={_f4(sgs_neto):g}"

    advance = _f4(advance_actual if advance_actual is not None else mc.get("advance_real"))
    pendiente_excel = _f4(excel_neto - advance)
    if pendiente_excel <= _MAT_QTY_TOL:
        return 0.0, None
    return pendiente_excel, None


def _mat_res_error(it: dict, error: str, error_tipo: str, cantidad=0) -> dict:
    return {
        "ok": False,
        "tipo": "MAT",
        "codigo": it.get("codigo", ""),
        "pro_code": it.get("pro_code", ""),
        "cantidad": cantidad,
        "pid": it.get("pid"),
        "pm_id": it.get("pm_id"),
        "partner_nombre": it.get("partner_nombre", ""),
        "error": error,
        "error_tipo": error_tipo,
    }


def _mat_res_omitido(it: dict, msg: str) -> dict:
    return {
        "ok": True,
        "tipo": "MAT",
        "codigo": it.get("codigo", ""),
        "pro_code": it.get("pro_code", ""),
        "cantidad": 0,
        "pid": it.get("pid"),
        "pm_id": it.get("pm_id"),
        "partner_nombre": it.get("partner_nombre", ""),
        "msg": msg,
    }


def _grupos_lideres_mo(checks_mo):
    grupos = []
    vistos = set()
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
            "key": key,
            "pro_code": ck.get("pro_code", ""),
            "pnombre": ck.get("pnombre", ""),
            "lideres": lideres,
        })
    return grupos


@app.route("/aprobar/<sid>", methods=["GET", "POST"])
def aprobar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("subir"))

    if request.method == "POST":
        indices_mo  = set(int(i) for i in request.form.getlist("idx_mo")  if i.isdigit())
        indices_mat = set(int(i) for i in request.form.getlist("idx_mat") if i.isdigit())

        if not indices_mo and not indices_mat:
            flash("Selecciona al menos 1 item para liquidar.")
            return redirect(url_for("aprobar", sid=sid))

        checks_mat = s.get("checks_mat", [])
        indices_mat_seg = {
            i for i in indices_mat
            if i < len(checks_mat) and _mat_es_liquidable(checks_mat[i])
        }
        bloqueados = len(indices_mat) - len(indices_mat_seg)

        s["indices_mo_aprobados"]  = indices_mo
        s["indices_mat_aprobados"] = indices_mat_seg
        s["aprobado_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Por ahora: Fase 1 â€” solo mostrar plan, no ejecutar
        if bloqueados:
            flash(f"El guardián de cantidades bloqueó {bloqueados} MAT no liquidable(s) del plan.")
        flash(f"Aprobado: {len(indices_mo)} MO + {len(indices_mat_seg)} MAT seleccionados. "
              "Liquidacion via Playwright proximamente.")
        return redirect(url_for("plan", sid=sid))

    return render_template("aprobar.html", sid=sid, s=s,
        checks_mo=s.get("checks_mo",[]),
        checks_mat=s.get("checks_mat",[]))


@app.route("/auditoria_lideres/<sid>")
def auditoria_lideres(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("subir"))
    return render_template("auditoria_lideres.html",
                           sid=sid, s=s,
                           checks_mat=s.get("checks_mat", []))


@app.route("/plan/<sid>")
def plan(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("subir"))
    return render_template("plan.html", sid=sid, s=s)


@app.route("/probar/<sid>")
def probar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("subir"))

    # Construir lista de items probables con pm_id conocido
    items_mat = [
        mc for mc in s.get("checks_mat", [])
        if _mat_es_liquidable(mc)
    ]
    items_mo  = [
        ck for ck in s.get("checks_mo", [])
        if ck.get("ok") and not ck.get("ya_liquidada") and ck.get("act_id") and ck.get("pid")
    ]

    return render_template("probar.html", sid=sid, s=s,
                           items_mat=items_mat, items_mo=items_mo)


@app.route("/probar/<sid>/inspect", methods=["POST"])
def probar_inspect(sid):
    """Abre Playwright, navega al pm_id y vuelca selectores. No escribe nada."""
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
        log.info(f"[INSPECT] pm#{pm_id} ok={info.get('ok')} "
                 f"add_line={info.get('add_line_count')} "
                 f"campos={len(info.get('fields_edit', []))}")
        return info
    except Exception as ex:
        log.error(f"[INSPECT] Error: {ex}")
        return {"ok": False, "error": str(ex)}, 500


@app.route("/probar/<sid>/run", methods=["POST"])
def probar_run(sid):
    """Ejecuta una liquidaciÃ³n MAT (dry_run por defecto)."""
    s = _sesiones.get(sid)
    if not s:
        return {"ok": False, "error": "Sesion no encontrada"}, 404

    pm_id          = request.form.get("pm_id",          type=int)
    cantidad       = request.form.get("cantidad",        type=float)
    partner_nombre = request.form.get("partner_nombre",  default="")
    pro_code       = request.form.get("pro_code",        default="")
    dry_run        = request.form.get("dry_run",         default="1") == "1"

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
        log.info(f"[RUN MAT] pm#{pm_id} dry={dry_run} ok={result.get('ok')}")
        return result
    except Exception as ex:
        log.error(f"[RUN MAT] Error: {ex}")
        return {"ok": False, "error": str(ex)}, 500


@app.route("/probar/<sid>/run_mo", methods=["POST"])
def probar_run_mo(sid):
    """Ejecuta una liquidaciÃ³n MO (dry_run por defecto)."""
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
        log.info(f"[RUN MO] act#{act_id} pid={pid} dry={dry_run} ok={result.get('ok')}")
        return result
    except Exception as ex:
        log.error(f"[RUN MO] Error: {ex}")
        return {"ok": False, "error": str(ex)}, 500


@app.route("/buscar_partner_cedula", methods=["POST"])
def buscar_partner_cedula():
    data   = request.get_json() or {}
    cedula = str(data.get("cedula") or "").strip()
    if not cedula:
        return jsonify({"ok": False, "error": "Cedula vacia"}), 400
    advertencia = "" if "C" in cedula.upper() else f"La cedula '{cedula}' no contiene 'C'."
    try:
        c = _sgs()
        partners = c.search_read("res.partner", [["vat", "=", cedula]], ["id", "name", "vat"], limit=10)
        if not partners:
            solo_num = cedula.upper().lstrip("C").strip()
            partners = c.search_read("res.partner", [["vat", "ilike", solo_num]], ["id", "name", "vat"], limit=10)
        if not partners:
            return jsonify({"ok": False, "error": f"No se encontro partner con cedula '{cedula}'.", "advertencia": advertencia})
        con_c   = [p for p in partners if "C" in str(p.get("vat") or "").upper()]
        partner = con_c[0] if con_c else partners[0]
        return jsonify({"ok": True, "nombre": partner["name"], "partner_id": partner["id"],
                        "vat": partner.get("vat") or "", "advertencia": advertencia})
    except Exception as ex:
        log.error(f"[BUSCAR CEDULA] {ex}")
        return jsonify({"ok": False, "error": str(ex)}), 500


@app.route("/calcular_distribucion_pct/<sid>", methods=["POST"])
def calcular_distribucion_pct(sid):
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"ok": False, "error": "Sesion no encontrada"}), 404
    data          = request.get_json() or {}
    lideres_input = data.get("lideres") or []
    if not lideres_input:
        return jsonify({"ok": False, "error": "Define al menos un lider."}), 400
    total_pct = sum(float(l.get("pct") or 0) for l in lideres_input)
    if abs(total_pct - 100.0) > 0.5:
        return jsonify({"ok": False, "error": f"Los porcentajes suman {total_pct:.1f}%, deben ser 100%."}), 400

    checks_mo = s.get("checks_mo") or []
    advertencias = []
    lideres = []
    for l in lideres_input:
        nombre = str(l.get("nombre") or "").strip()
        cedula = str(l.get("cedula") or "").strip()
        pct    = float(l.get("pct") or 0)
        if not nombre or pct <= 0:
            continue
        if cedula and "C" not in cedula.upper():
            advertencias.append(f"Cedula '{cedula}' sin 'C' — verificar.")
        lideres.append({"nombre": nombre, "cedula": cedula, "pct": pct})
    if not lideres:
        return jsonify({"ok": False, "error": "Sin lideres validos."}), 400

    # Distribucion proporcional por cantidad (sin precios MDO)
    items_pendientes = []
    for i, ck in enumerate(checks_mo):
        if not ck.get("ok") or ck.get("ya_liquidada"):
            continue
        row  = ck.get("row") or {}
        cant = float(ck["pendiente_real"] if ck.get("ajuste_pendiente") and ck.get("pendiente_real")
                     else row.get("cantidad") or 0)
        if cant <= 0:
            continue
        items_pendientes.append({
            "idx":      i,
            "act_code": row.get("codigo", ""),
            "pid":      ck.get("pid"),
            "act_id":   ck.get("act_id"),
            "pro_code": ck.get("pro_code", ""),
            "cantidad": cant,
        })

    total_items = len(items_pendientes)
    if not total_items:
        return jsonify({"ok": False, "error": "Sin prestaciones MO pendientes."}), 400

    # Asignar items a lideres por proporcion
    asignaciones = []
    restantes    = list(items_pendientes)
    for k, lider in enumerate(lideres):
        es_ultimo  = (k == len(lideres) - 1)
        target     = len(restantes) if es_ultimo else round(total_items * lider["pct"] / 100.0)
        asignados  = restantes[:target]
        restantes  = restantes[target:]
        pct_real   = round(len(asignados) / total_items * 100, 1) if total_items else 0
        asignaciones.append({
            "lider":    lider["nombre"],
            "cedula":   lider["cedula"],
            "pct_obj":  lider["pct"],
            "pct_real": pct_real,
            "n_items":  len(asignados),
            "target":   target,
            "valor":    0,
            "items":    asignados,
        })

    distribucion = {
        "total":        total_items,
        "asignaciones": asignaciones,
        "errores":      [],
        "preview":      [],
    }
    s["distribucion_pct"] = distribucion

    return jsonify({
        "ok": True,
        "distribucion": {
            "total":        total_items,
            "errores":      [],
            "asignaciones": [
                {"lider": a["lider"], "cedula": a["cedula"],
                 "pct_obj": a["pct_obj"], "pct_real": a["pct_real"],
                 "n_items": a["n_items"], "target": a["target"], "valor": 0}
                for a in asignaciones
            ],
            "preview": [],
        },
        "advertencias": advertencias,
    })


@app.route("/liquidar_pct/<sid>", methods=["POST"])
def liquidar_pct(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("subir"))
    distribucion = s.get("distribucion_pct")
    if not distribucion or not distribucion.get("asignaciones"):
        flash("No hay distribucion calculada. Calcula la distribucion primero.")
        return redirect(url_for("verificar", sid=sid))

    items_mo  = []
    for asig in distribucion["asignaciones"]:
        lider_nombre = asig["lider"]
        lider_cedula = asig.get("cedula", "")
        for it in asig.get("items") or []:
            cant = float(it.get("cantidad") or 0)
            if cant <= 0:
                continue
            idx = it.get("idx")
            items_mo.append({
                "tipo":               "MO",
                "idx":                idx,
                "pid":                it.get("pid"),
                "act_id":             it.get("act_id"),
                "act_code":           it.get("act_code", ""),
                "pro_code":           it.get("pro_code", ""),
                "cantidad":           cant,
                "partner_nombre":     lider_nombre,
                "partner_cedula":     lider_cedula,
                "cantidad_proyectada": cant,
            })

    if not items_mo:
        flash("La distribucion no contiene items liquidables.")
        return redirect(url_for("verificar", sid=sid))

    if not sesion_activa():
        flash("⚠ El browser no tiene sesión activa en SGS. "
              "Abre el browser, inicia sesión y espera el indicador verde.")
        return redirect(url_for("verificar", sid=sid))

    with _cached_storage_state_lock:
        storage_state = _cached_storage_state

    if not storage_state:
        flash("⚠ Estado de sesión no disponible. Espera unos segundos e intenta de nuevo.")
        return redirect(url_for("verificar", sid=sid))

    _trabajos[sid] = {
        "estado":    "corriendo",
        "items_mo":  [{"codigo": it["act_code"], "tipo": "MO",
                        "pid": it.get("pid"), "pro_code": it.get("pro_code", ""),
                        "worker_id": "vis.", "estado": "pendiente", "error": None,
                        "lider": it.get("partner_nombre", ""),
                        "cantidad": it.get("cantidad", 0)}
                      for it in items_mo],
        "items_mat": [],
        "resultados": None,
        "error":      None,
        "t_inicio":   _time_mod.time(),
    }

    threading.Thread(
        target=_ejecutar_liquidacion_bg,
        args=(sid, items_mo, [], storage_state),
        daemon=True,
    ).start()

    return redirect(url_for("esperar", sid=sid))


@app.route("/liquidar/<sid>", methods=["POST"])
def liquidar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("subir"))

    # Gate: sesiÃ³n activa requerida
    if not sesion_activa():
        flash("âš  El browser no tiene sesiÃ³n activa en SGS. "
              "Abre el browser, inicia sesiÃ³n y espera el indicador verde.")
        return redirect(url_for("verificar", sid=sid))

    idx_mo  = [int(i) for i in request.form.getlist("idx_mo")  if i.isdigit()]
    idx_mat = [int(i) for i in request.form.getlist("idx_mat") if i.isdigit()]

    if not idx_mo and not idx_mat:
        flash("Selecciona al menos un item para liquidar.")
        return redirect(url_for("verificar", sid=sid))

    checks_mo  = s.get("checks_mo",  [])
    checks_mat = s.get("checks_mat", [])
    items_mo, items_mat = [], []

    items_mo, errores_mo_lider = _items_mo_desde_form(checks_mo, idx_mo, request.form)
    if errores_mo_lider:
        for err in errores_mo_lider:
            flash(err)
        return redirect(url_for("verificar", sid=sid))

    # Re-verificar estado REAL en SGS antes de armar la lista de materiales.
    # Esto previene doble liquidaciÃ³n cuando el usuario vuelve a la pÃ¡gina de
    # verificaciÃ³n despuÃ©s de que el proceso ya liquidÃ³ algunos Ã­tems.
    pm_ids_mat = [
        checks_mat[idx]["pm_id"]
        for idx in idx_mat
        if idx < len(checks_mat) and checks_mat[idx].get("pm_id")
    ]
    advance_real_sgs = {}
    precheck_pendiente_ok = True
    if pm_ids_mat:
        try:
            frescos = _sgs().search_read(
                "project.material",
                [["id", "in", pm_ids_mat]],
                ["id", "advance", "amount", "balance", "devuelto"],
                limit=len(pm_ids_mat) + 5,
            )
            dets = _sgs().search_read(
                "project.material.details",
                [["project_material_id", "in", pm_ids_mat]],
                ["id", "project_material_id", "amount"],
                limit=1000,
            )
            advance_details = {}
            for d in dets:
                amt = float(d.get("amount") or 0)
                if amt <= 0:
                    continue
                pm_ref = d.get("project_material_id")
                pm_det_id = pm_ref[0] if isinstance(pm_ref, list) else pm_ref
                advance_details[pm_det_id] = advance_details.get(pm_det_id, 0.0) + amt
            for f in frescos:
                advance_real_sgs[f["id"]] = {
                    "advance": float(f.get("advance") or 0),
                    "advance_details": float(advance_details.get(f["id"], 0.0)),
                    "amount": float(f.get("amount") or 0),
                    "balance": float(f.get("balance") or 0),
                    "devuelto": float(f.get("devuelto") or 0),
                }
        except Exception as ex:
            log.warning(f"[LIQUIDAR] No se pudo re-verificar SGS antes de liquidar: {ex}")
            precheck_pendiente_ok = False

    if pm_ids_mat and not precheck_pendiente_ok:
        flash("No se pudo verificar el Pendiente fresco en SGS. No se liquidan materiales por seguridad.")
        return redirect(url_for("verificar", sid=sid))

    omitidos_ya_liq = []
    for idx in idx_mat:
        if idx >= len(checks_mat): continue
        mc = checks_mat[idx]
        if not _mat_es_liquidable(mc):
            log.warning(f"[LIQUIDAR] idx={idx} ({mc.get('mat', {}).get('codigo')}) no liquidable por protocolo - omitido")
            continue
        if not mc.get("pm_id"): continue

        pm_id    = mc["pm_id"]

        # Guardián de cantidades: la cantidad autorizada viene del Excel verificado.
        # SGS solo confirma que el estado no cambio; nunca reemplaza la cantidad.
        if pm_id not in advance_real_sgs:
            flash(f"âš  {mc['mat']['codigo']}: no se pudo confirmar Pendiente fresco en SGS â€” omitido.")
            log.error(f"[LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) no vino en re-verificacion SGS - omitido")
            continue

        if pm_id in advance_real_sgs:
            fresco = advance_real_sgs[pm_id]
            adv_real = fresco["advance"]
            amount_sgs = fresco["amount"]
            pendiente = round(fresco["balance"], 4)
            devuelto = fresco["devuelto"]
            advance_details = fresco.get("advance_details", adv_real)
            cantidad_excel, err_cantidad = _mat_cantidad_excel_pendiente(mc, advance_actual=advance_details)
            if err_cantidad:
                flash(f"⚠ {mc['mat']['codigo']}: {err_cantidad} — omitido por el guardián de cantidades.")
                log.error(f"[LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) {err_cantidad}")
                continue
            if devuelto > 0.001:
                log.info(f"[LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) "
                         f"devuelto={devuelto:g} factorizado — Capa2 confirma balance.")
            # Si ya está liquidado en SGS, omitir y avisar
            if cantidad_excel <= 0.001:
                omitidos_ya_liq.append(mc["mat"]["codigo"])
                log.warning(f"[LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) "
                            f"ya liquidado segun Excel/details (advance={advance_details}) — omitido")
                continue
            # Permitir cantidad_excel < pendiente cuando hay splits multi-líder
            # (pendiente es el total SGS de todos los líder).
            # Solo bloquear si cantidad_excel > pendiente (sobre-liquidación).
            if cantidad_excel > pendiente + 0.001:
                flash(
                    f"⚠ {mc['mat']['codigo']}: Excel pide {cantidad_excel:g}, "
                    f"pero SGS Pendiente={pendiente:g} — omitido por el guardián de cantidades."
                )
                log.error(f"[LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) "
                          f"contrato Excel={cantidad_excel} supera balance SGS={pendiente} "
                          f"advance_api={adv_real} advance_details={advance_details} amount={amount_sgs}")
                continue

        partner_nombre = mc.get("partner_nombre", "")
        partner_splits = mc.get("partner_splits")   # {lider: gross_qty} o None

        if not partner_nombre and not partner_splits:
            flash(f"⚠ {mc['mat']['codigo']}: líder no determinado desde despacho — omitido. Verificar bandeja en SGS.")
            log.warning(f"[LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) sin partner_nombre — omitido")
            continue

        _base_item = {
            "tipo":             "MAT",
            "idx":              idx,
            "pm_id":            pm_id,
            "pid":              mc.get("pid"),
            "codigo":           mc["mat"]["codigo"],
            "excel_neto":       _f4(mc["mat"].get("sgs_neto_excel")),
            "advance_precheck": advance_details,
            "balance_precheck": pendiente,
            "pro_code":         mc.get("pro_code", ""),
            "pendiente_api_confirmado": True,
        }

        if partner_splits:
            # Multi-líder: un ítem por líder con cantidad proporcional al despacho neto
            total_gross = sum(partner_splits.values()) or 1.0
            for lider, gross in sorted(partner_splits.items()):
                cant_lider = round(cantidad_excel * gross / total_gross, 4)
                if cant_lider <= 0.001:
                    continue
                items_mat.append(dict(_base_item,
                    cantidad=cant_lider,
                    partner_nombre=lider,
                    split={"gross": gross, "total_gross": total_gross},
                ))
                log.info(f"[LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) "
                         f"split líder={lider.split()[-1]} gross={gross:g} → qty={cant_lider:g}")
        else:
            items_mat.append(dict(_base_item,
                cantidad=cantidad_excel,
                partner_nombre=partner_nombre,
            ))

    if omitidos_ya_liq:
        flash(f"âš  {len(omitidos_ya_liq)} material(es) ya liquidado(s) en SGS omitido(s): "
              f"{', '.join(omitidos_ya_liq)}")

    if not items_mo and not items_mat:
        flash("NingÃºn item seleccionado es liquidable (todos ya estÃ¡n liquidados en SGS).")
        return redirect(url_for("verificar", sid=sid))

    for pos, it in enumerate(items_mat):
        it["job_pos"] = pos

    # Capturar storage state (exportado por el monitor cada 4s)
    with _cached_storage_state_lock:
        storage_state = _cached_storage_state

    if not storage_state:
        flash("âš  Estado de sesiÃ³n no disponible. Espera unos segundos e intenta de nuevo.")
        return redirect(url_for("verificar", sid=sid))


    # Crear estructura de job para tracking
    _trabajos[sid] = {
        "estado":    "corriendo",
        "items_mo":  [{"codigo": it["act_code"], "tipo": "MO",
                        "pid": it.get("pid"), "pro_code": it.get("pro_code", ""),
                        "worker_id": "vis.", "estado": "pendiente", "error": None,
                        "lider": it.get("partner_nombre", ""),
                        "cantidad": it.get("cantidad", 0)}
                      for it in items_mo],
        "items_mat": [{"codigo": it["codigo"], "tipo": "MAT",
                        "pm_id": it.get("pm_id"), "pid": it.get("pid"),
                        "pro_code": it.get("pro_code", ""),
                        "cantidad": it.get("cantidad", 0), "lider": it.get("partner_nombre", ""),
                        "worker_id": 0, "estado": "pendiente", "error": None}
                      for it in items_mat],
        "resultados": None,
        "error":      None,
        "t_inicio":   _time_mod.time(),
    }

    # Lanzar en hilo de fondo
    threading.Thread(
        target=_ejecutar_liquidacion_bg,
        args=(sid, items_mo, items_mat, storage_state),
        daemon=True,
    ).start()

    return redirect(url_for("esperar", sid=sid))


def _ejecutar_liquidacion_bg(sid, items_mo, items_mat, storage_state):
    """Corre en hilo de fondo â€” actualiza _trabajos[sid] en tiempo real."""
    job = _trabajos.get(sid)
    if not job:
        return
    s = _sesiones.get(sid) or {}

    resultados = []

    try:
        c = _sgs()

        # â”€â”€ MO â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        def _progress_mo(orig_idx, worker_id, estado, codigo, error=None):
            try:
                job["items_mo"][orig_idx]["worker_id"] = worker_id
                job["items_mo"][orig_idx]["estado"] = estado
                if estado == "corriendo":
                    job["items_mo"][orig_idx]["error"] = None
                elif error:
                    job["items_mo"][orig_idx]["error"] = str(error)[:120]
            except Exception:
                pass

        resultados_mo = liquidar_mo_parallel_por_proyecto(
            items_mo,
            max_workers=MO_MAX_PROJECT_WORKERS,
            storage_state=storage_state,
            progress_cb=_progress_mo,
            should_cancel=lambda: bool(job.get("cancelado")),
            sgs_client_factory=_nuevo_sgs_readonly,
        )
        resultados.extend(resultados_mo)

        for i, res in enumerate(resultados_mo):
            job["items_mo"][i]["estado"] = "ok" if res.get("ok") else "error"
            if not res.get("ok"):
                job["items_mo"][i]["error"] = res.get("error", "")[:120]

        # â”€â”€ Re-verificar MAT en SGS justo antes de ejecutar (segunda barrera) â”€â”€
        # Si el usuario volviÃ³ atrÃ¡s y relanzÃ³, algunos pm_ids ya pueden estar
        # liquidados. Esta verificaciÃ³n usa datos frescos de SGS al momento exacto
        # de ejecutar, no los cacheados del upload.
        items_mat_seguros = []
        if items_mat:
            if job.get("cancelado"):
                items_mat = []
            else:
                try:
                    pm_ids = [it["pm_id"] for it in items_mat]
                    frescos = c.search_read(
                        "project.material",
                        [["id", "in", pm_ids]],
                        ["id", "advance", "amount", "balance", "devuelto"],
                        limit=len(pm_ids) + 5,
                    )
                    dets = c.search_read(
                        "project.material.details",
                        [["project_material_id", "in", pm_ids]],
                        ["id", "project_material_id", "amount"],
                        limit=1000,
                    )
                    advance_details = {}
                    for d in dets:
                        amt_det = float(d.get("amount") or 0)
                        if amt_det <= 0:
                            continue
                        pm_ref = d.get("project_material_id")
                        pm_det_id = pm_ref[0] if isinstance(pm_ref, list) else pm_ref
                        advance_details[pm_det_id] = advance_details.get(pm_det_id, 0.0) + amt_det
                    estado_sgs = {
                        f["id"]: {
                            "advance": float(f.get("advance") or 0),
                            "advance_details": float(advance_details.get(f["id"], 0.0)),
                            "amount": float(f.get("amount") or 0),
                            "balance": float(f.get("balance") or 0),
                            "devuelto": float(f.get("devuelto") or 0),
                        }
                        for f in frescos
                    }
                    for it in items_mat:
                        est = estado_sgs.get(it["pm_id"], {})
                        adv = est.get("advance", 0.0)
                        amt = est.get("amount", 0.0)
                        adv_details = est.get("advance_details", adv)
                        pendiente_real = round(est.get("balance", 0.0), 4)
                        devuelto = est.get("devuelto", 0.0)
                        excel_neto = _f4(it.get("excel_neto", it.get("cantidad", 0)))
                        pendiente_excel = _f4(excel_neto - adv_details)
                        if devuelto > 0.001:
                            log.info(f"[BG MAT] pm_id={it['pm_id']} ({it['codigo']}) "
                                     f"devuelto={devuelto:g} factorizado — continuando con Capa2+3.")
                        if pendiente_excel <= 0.001:
                            log.warning(f"[BG MAT] pm_id={it['pm_id']} ({it['codigo']}) "
                                        f"ya liquidado segun Excel/details "
                                        f"(advance_details={adv_details}, advance_api={adv}/{amt}) — saltando")
                            # Marcarlo como ya-liquidado en el job para que la UI lo refleje
                            job_pos = it.get("job_pos")
                            if job_pos is not None and job_pos < len(job["items_mat"]):
                                job["items_mat"][job_pos]["estado"] = "ok"
                                job["items_mat"][job_pos]["error"]  = "Sin pendiente en SGS"
                            resultados.append(_mat_res_omitido(it, "Ya liquidado en SGS — omitido"))
                        else:
                            cantidad_contrato = _f4(it.get("cantidad"))
                            # Para splits multi-líder, cantidad_contrato < pendiente_excel es válido.
                            # Solo bloquear si la cantidad supera lo disponible (sobre-liquidación).
                            if cantidad_contrato > pendiente_excel + 0.001:
                                msg = (f"Contrato Excel supera pendiente: {cantidad_contrato:g} > "
                                       f"pendiente Excel/details={pendiente_excel:g}")
                                log.error(f"[BG MAT] pm_id={it['pm_id']} ({it['codigo']}) {msg}")
                                job_pos = it.get("job_pos")
                                if job_pos is not None and job_pos < len(job["items_mat"]):
                                    job["items_mat"][job_pos]["estado"] = "error"
                                    job["items_mat"][job_pos]["error"] = "Bloqueado: cantidad supera Excel"
                                resultados.append(_mat_res_error(it, msg, "mat_sobre_excel", cantidad_contrato))
                                continue
                            if cantidad_contrato > pendiente_real + 0.001:
                                msg = (f"Excel pide {cantidad_contrato:g} supera SGS Pendiente={pendiente_real:g}. "
                                       "No se puede liquidar mas del balance disponible.")
                                log.error(f"[BG MAT] pm_id={it['pm_id']} ({it['codigo']}) {msg} "
                                          f"advance_api={adv} advance_details={adv_details} amount={amt}")
                                job_pos = it.get("job_pos")
                                if job_pos is not None and job_pos < len(job["items_mat"]):
                                    job["items_mat"][job_pos]["estado"] = "error"
                                    job["items_mat"][job_pos]["error"] = "Bloqueado: Excel vs SGS Pendiente"
                                resultados.append(_mat_res_error(it, msg, "mat_excel_sgs_mismatch", cantidad_contrato))
                                continue
                            items_mat_seguros.append(dict(it, pendiente_api_confirmado=True))
                except Exception as ex:
                    log.error(f"[BG MAT] Re-verificacion SGS fallo: {ex} - abortando MAT por seguridad")
                    for i, it in enumerate(items_mat):
                        if i < len(job["items_mat"]):
                            job["items_mat"][i]["estado"] = "error"
                            job["items_mat"][i]["error"] = "No se pudo verificar Pendiente SGS"
                        resultados.append({
                            "ok": False, "tipo": "MAT",
                            "codigo": it.get("codigo", ""), "pro_code": it.get("pro_code", ""),
                            "cantidad": it.get("cantidad", 0), "pid": it.get("pid"),
                            "error": f"No se pudo verificar Pendiente SGS antes de liquidar: {ex}",
                            "error_tipo": "precheck_pendiente_failed",
                        })
                    items_mat_seguros = []
            items_mat = items_mat_seguros

        # â”€â”€ MAT paralelo â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if len(items_mat) > 0:
            def _cb(orig_idx, worker_id, estado, codigo, error=None):
                job_pos = items_mat[orig_idx].get("job_pos", orig_idx) if orig_idx < len(items_mat) else orig_idx
                if job_pos < len(job["items_mat"]):
                    job["items_mat"][job_pos]["estado"]    = estado
                    job["items_mat"][job_pos]["worker_id"] = worker_id
                    if error:
                        job["items_mat"][job_pos]["error"] = str(error)[:120]

            log.info(f"[BG MAT] Lista/modal - {len(items_mat)} materiales, max 2 paginas por proyecto")
            resultados_mat = liquidar_materiales_lista_modal_parallel(
                items_mat, max_pages=2,
                storage_state=storage_state,
                progress_cb=_cb,
                should_cancel=lambda: bool(job.get("cancelado")),
            )
            for i, res in enumerate(resultados_mat):
                job_pos = items_mat[i].get("job_pos", i) if i < len(items_mat) else i
                if job_pos < len(job["items_mat"]):
                    job["items_mat"][job_pos]["estado"] = "ok" if res.get("ok") else "error"
                    if not res.get("ok"):
                        job["items_mat"][job_pos]["error"] = res.get("error", "")[:120]
            resultados.extend(resultados_mat)

        elif False:
            item = items_mat[0]
            job_pos = item.get("job_pos", 0)
            if job_pos < len(job["items_mat"]):
                job["items_mat"][job_pos]["estado"] = "corriendo"
            try:
                pw_m = PlaywrightClient(headless=False, slow_mo=100)
                pw_m.start(storage_state=storage_state)
                res = pw_m.liquidar_material(
                    pm_id=item["pm_id"], cantidad=item["cantidad"],
                    pro_code=item["pro_code"],
                    partner_nombre=item.get("partner_nombre", ""),
                    pendiente_api_confirmado=item.get("pendiente_api_confirmado", False),
                    dry_run=False, screenshots=False,
                )
                pw_m.close()
            except Exception as ex:
                res = {"ok": False, "error": str(ex)}
            res["tipo"]     = "MAT"
            res["codigo"]   = item["codigo"]
            res["pro_code"] = item["pro_code"]
            res["cantidad"] = item["cantidad"]
            res["excel_neto"] = item.get("excel_neto")
            res["pid"]      = item.get("pid")
            resultados.append(res)
            if job_pos < len(job["items_mat"]):
                job["items_mat"][job_pos]["estado"] = "ok" if res.get("ok") else "error"
                if not res.get("ok"):
                    job["items_mat"][job_pos]["error"] = res.get("error", "")[:120]

        # â”€â”€ Actualizar fechas â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        _fecha_iso_app = s.get("fecha_liquidacion_iso", "")
        try:
            fecha_hoy = datetime.strptime(_fecha_iso_app, "%Y-%m-%d").strftime("%d-%m-%Y") if _fecha_iso_app else datetime.today().strftime("%d-%m-%Y")
        except Exception:
            fecha_hoy = datetime.today().strftime("%d-%m-%Y")
        pdl_programado = str(s.get("pdl_detectado", "") or "").strip()
        pids_ok = {}
        for item, res in zip(items_mo, resultados[:len(items_mo)]):
            if res.get("ok") and item.get("pid"):
                pids_ok[item["pid"]] = item.get("pro_code", "")
        for res in resultados[len(items_mo):]:
            if res.get("ok") and res.get("pid"):
                pids_ok[res["pid"]] = res.get("pro_code", "")
        if pids_ok:
            # PDL vigente por API — actualizar_fechas_proyecto lo combina con el
            # nuevo para que el PDL histórico del nodo nunca se reemplace.
            _pdl_api_map = {}
            if pdl_programado:
                try:
                    for _r in _sgs().search_read(
                            "project.dominion", [["id", "in", list(pids_ok.keys())]],
                            ["id", "pdl"], limit=len(pids_ok) + 5):
                        _pdl_api_map[_r["id"]] = str(_r.get("pdl") or "").strip()
                except Exception as ex:
                    log.warning(f"[BG FECHAS] No se pudo leer PDL actual por API: {ex}")
            try:
                pw_f = PlaywrightClient(headless=False, slow_mo=200)
                pw_f.start(storage_state=storage_state)
                for pid_liq, pro_liq in pids_ok.items():
                    try:
                        pw_f.actualizar_fechas_proyecto(
                            pid=pid_liq,
                            fecha=fecha_hoy,
                            pro_code=pro_liq,
                            pdl=pdl_programado,
                            pdl_actual_api=_pdl_api_map.get(pid_liq, ""),
                        )
                    except Exception as ex:
                        log.warning(f"[BG FECHAS] pid={pid_liq}: {ex}")
                pw_f.close()
            except Exception as ex:
                log.warning(f"[BG FECHAS] error general: {ex}")

    except Exception as ex:
        log.error(f"[BG LIQUIDAR] Error general: {ex}")
        job["estado"] = "error"
        job["error"]  = _safe_str(ex)
        return

    # Debug report
    from collections import Counter
    errores_detalle = [
        {"tipo": r.get("tipo","?"), "codigo": r.get("codigo","?"),
         "worker": r.get("worker_id","vis."), "intento": 1,
         "error": r.get("error","")[:200],
         "clase": _clasificar_error(r.get("error",""))}
        for r in resultados if not r.get("ok")
    ]
    clases = Counter(e["clase"] for e in errores_detalle)
    n_ok   = sum(1 for r in resultados if r.get("ok"))
    debug  = {
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total":           len(resultados),
        "ok":              n_ok,
        "fallidos":        len(resultados) - n_ok,
        "clases_error":    dict(clases),
        "errores_detalle": errores_detalle,
    }

    if s:
        s["resultados_liquidacion"] = resultados
        s["debug_liquidacion"]      = debug
    job["resultados"] = resultados
    job["estado"]     = "cancelado" if job.get("cancelado") else "listo"


@app.route("/esperar/<sid>")
def esperar(sid):
    s = _sesiones.get(sid)
    job = _trabajos.get(sid)
    if not s or not job:
        flash("Sesion no encontrada.")
        return redirect(url_for("subir"))
    total = len(job.get("items_mo", [])) + len(job.get("items_mat", []))
    return render_template("esperar_liquidacion.html",
                           sid=sid, s=s, job=job, total=total)


@app.route("/progreso/<sid>")
def progreso(sid):
    job = _trabajos.get(sid)
    if not job:
        return jsonify({"estado": "no_encontrado"}), 404
    hechos = sum(1 for it in job.get("items_mo", []) + job.get("items_mat", [])
                 if it["estado"] in ("ok", "error"))
    total  = len(job.get("items_mo", [])) + len(job.get("items_mat", []))
    return jsonify({
        "estado":    job["estado"],
        "hechos":    hechos,
        "total":     total,
        "items_mo":  job.get("items_mo", []),
        "items_mat": job.get("items_mat", []),
        "error":     job.get("error"),
    })


@app.route("/auditoria/<sid>")
def auditoria(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("subir"))

    resultados = s.get("resultados_liquidacion", [])
    if not resultados:
        flash("Sin resultados de liquidacion para auditar.")
        return redirect(url_for("subir"))

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
                            "advance":  a["advance"],
                            "balance":  a["balance"],
                            "amount":   a["amount"],
                            "coincide": coincide,
                            "estado":   "ok" if coincide else "descuadre",
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
                            "advance":  a["advance"],
                            "balance":  a["balance"],
                            "amount":   a["amount"],
                            "coincide": coincide,
                            "estado":   "ok" if coincide else "descuadre",
                        }
                    else:
                        res["auditoria"] = {"estado": "no_encontrado"}

            except Exception as ex:
                res["auditoria"] = {"estado": "error", "msg": str(ex)}

    except Exception as ex:
        log.warning(f"[AUDITORIA] Error: {ex}")
        flash(f"Error conectando a SGS para auditorÃ­a: {ex}")

    s["resultados_liquidacion"] = resultados
    return render_template("resultado_liquidacion.html", sid=sid, s=s,
                           resultados=resultados)


@app.route("/resultado/<sid>")
def resultado(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("subir"))
    resultados = s.get("resultados_liquidacion", [])
    if not resultados:
        flash("No hay resultados aÃºn. La liquidaciÃ³n puede seguir en curso.")
        return redirect(url_for("esperar", sid=sid))
    return render_template("resultado_liquidacion.html", sid=sid, s=s,
                           resultados=resultados)


@app.route("/pw_status")
def pw_status():
    activa = sesion_activa()
    browser_abierto = _pw_global is not None and _pw_global._page is not None
    return jsonify({"alive": activa, "activa": activa, "iniciando": browser_abierto and not activa})


@app.route("/sesion_estado")
def sesion_estado():
    activa = sesion_activa()
    browser_abierto = _pw_global is not None and _pw_global._page is not None
    return jsonify({"activa": activa, "iniciando": browser_abierto and not activa})


@app.route("/reiniciar_servidor", methods=["POST"])
def reiniciar_servidor():
    """Reinicia Flask aun cuando use_reloader=False."""
    _programar_restart_servidor()
    return jsonify({"ok": True, "msg": "Reiniciando..."})


def _programar_restart_servidor():
    """
    Programa un proceso nuevo y apaga el actual.

    No usamos os.execv desde el mismo proceso porque puede dejar la peticion HTTP
    colgada o competir con el puerto. Un helper independiente espera a que este
    proceso salga y luego levanta app.py otra vez.
    """
    import subprocess

    python = sys.executable
    script = os.path.abspath(sys.argv[0] if sys.argv else __file__)
    cwd = os.path.dirname(os.path.abspath(__file__))
    helper_code = (
        "import subprocess,time;"
        "time.sleep(1.2);"
        f"subprocess.Popen({[python, script]!r}, cwd={cwd!r})"
    )

    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        [python, "-c", helper_code],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )

    def _exit_current():
        _time_mod.sleep(0.25)
        log.info("[RESTART] Apagando proceso actual; helper levantara uno nuevo")
        os._exit(0)

    threading.Thread(target=_exit_current, daemon=True).start()


@app.route("/browser_ctrl", methods=["POST"])
def browser_ctrl():
    data = request.get_json(force=True, silent=True) or {}
    cmd  = data.get("cmd", "")
    if cmd == "restart_browser":
        _browser_commands.put("restart_browser")
        return jsonify({"ok": True, "msg": "Browser abriendoâ€¦"})
    elif cmd == "close_browser":
        _browser_commands.put("close_browser")
        return jsonify({"ok": True, "msg": "Browser cerrÃ¡ndoseâ€¦"})
    elif cmd == "reverificar":
        _browser_commands.put("reverificar")
        return jsonify({"ok": True, "msg": "Reverificandoâ€¦"})
    return jsonify({"ok": False, "error": f"Comando desconocido: {cmd}"}), 400


@app.route("/cancelar/<sid>", methods=["POST"])
def cancelar(sid):
    job = _trabajos.get(sid)
    if job:
        job["cancelado"] = True
        job["estado"] = "cancelado"

        parciales = []
        for it in job.get("items_mo", []) + job.get("items_mat", []):
            estado = it.get("estado")
            if estado == "pendiente":
                it["estado"] = "error"
                it["error"] = "Cancelado antes de iniciar"
                estado = "error"
            parciales.append({
                "ok": estado == "ok",
                "tipo": it.get("tipo", "?"),
                "codigo": it.get("codigo", "?"),
                "pm_id": it.get("pm_id"),
                "pid": it.get("pid"),
                "pro_code": it.get("pro_code", ""),
                "cantidad": it.get("cantidad", 0),
                "worker_id": it.get("worker_id", "vis."),
                "partner_nombre": it.get("lider", ""),
                "error": "" if estado == "ok" else (it.get("error") or "Cancelado por el usuario"),
                "error_tipo": "cancelado" if estado != "ok" else "",
            })

        from collections import Counter
        errores_detalle = [
            {"tipo": r.get("tipo","?"), "codigo": r.get("codigo","?"),
             "worker": r.get("worker_id","vis."), "intento": 1,
             "error": r.get("error","")[:200],
             "clase": r.get("error_tipo") or _clasificar_error(r.get("error",""))}
            for r in parciales if not r.get("ok")
        ]
        debug = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(parciales),
            "ok": sum(1 for r in parciales if r.get("ok")),
            "fallidos": sum(1 for r in parciales if not r.get("ok")),
            "clases_error": dict(Counter(e["clase"] for e in errores_detalle)),
            "errores_detalle": errores_detalle,
        }
        s = _sesiones.get(sid)
        if s and not s.get("resultados_liquidacion"):
            s["resultados_liquidacion"] = parciales
            s["debug_liquidacion"] = debug
    return redirect(url_for("resultado", sid=sid))


def _pids_liquidados_desde_sesion(sid):
    s = _sesiones.get(sid) or {}
    job = _trabajos.get(sid) or {}
    resultados = s.get("resultados_liquidacion") or []
    pids_ok = {}

    checks_mo = s.get("checks_mo", []) or []
    checks_mat = s.get("checks_mat", []) or []

    def _resolver_pid_desde_checks(tipo, codigo, pro_code):
        if tipo == "MO":
            for ck in checks_mo:
                row = ck.get("row", {})
                if row.get("codigo") == codigo and ck.get("pro_code") == pro_code and ck.get("pid"):
                    return ck.get("pid")
        if tipo == "MAT":
            for ck in checks_mat:
                mat = ck.get("mat", {})
                if mat.get("codigo") == codigo and ck.get("pro_code") == pro_code and ck.get("pid"):
                    return ck.get("pid")
        return None

    def _registrar(pid, pro_code):
        if pid:
            pids_ok[pid] = pro_code or pids_ok.get(pid, "")

    for r in resultados:
        if not r.get("ok"):
            continue
        pid = r.get("pid")
        pro_code = r.get("pro_code", "")
        tipo = r.get("tipo")
        codigo = r.get("codigo", "")

        if not pid:
            pid = _resolver_pid_desde_checks(tipo, codigo, pro_code)
        _registrar(pid, pro_code)

    # Si el proceso quedó a medias o aún no ha consolidado resultados en sesión,
    # usar también el estado vivo del job actual.
    for it in job.get("items_mo", []) + job.get("items_mat", []):
        if it.get("estado") != "ok":
            continue
        pid = it.get("pid")
        pro_code = it.get("pro_code", "")
        tipo = it.get("tipo")
        codigo = it.get("codigo", "")
        if not pid:
            pid = _resolver_pid_desde_checks(tipo, codigo, pro_code)
        _registrar(pid, pro_code)

    return pids_ok


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


def _texto_m2o(valor) -> str:
    if isinstance(valor, (list, tuple)):
        if len(valor) >= 2:
            return str(valor[1] or "").strip()
        if valor:
            return str(valor[0] or "").strip()
        return ""
    return str(valor or "").strip()


def _fecha_iso(valor) -> str:
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


def _fecha_ui(valor) -> str:
    iso = _fecha_iso(valor)
    if not iso:
        return "-"
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d-%m-%Y")
    except Exception:
        return iso


_PDL_SEP = re.compile(r"[;,\s]+")

def _pdl_match(actual: str, esperado: str) -> bool:
    """True si todos los PDL esperados están en actual (soporta multi-valor con ';')."""
    if not esperado or esperado == "-":
        return True
    if actual == esperado:
        return True
    actual_set  = {p for p in _PDL_SEP.split(actual)  if p and p != "-"}
    esperado_set = {p for p in _PDL_SEP.split(esperado) if p}
    return bool(esperado_set) and esperado_set.issubset(actual_set)


def _resolver_objetivos_cierre(sid, fecha_raw="", pdl_raw=""):
    s = _sesiones.get(sid) or {}
    fecha_raw = str(fecha_raw or "").strip()
    fecha_iso = _fecha_iso(fecha_raw)
    if not fecha_iso:
        fecha_iso = datetime.now().strftime("%Y-%m-%d")
    fecha_dt = datetime.strptime(fecha_iso, "%Y-%m-%d")
    pdl = str(pdl_raw or s.get("pdl_detectado", "") or "").strip()
    return {
        "fecha_iso": fecha_iso,
        "fecha_ui": fecha_dt.strftime("%d-%m-%Y"),
        "fecha_input": fecha_dt.strftime("%Y-%m-%d"),
        "pdl_esperado": pdl,
        "estado_esperado": CIERRE_RAPIDO_ESTADO,
    }


def _proyectos_identificados_desde_sesion(sid):
    s = _sesiones.get(sid) or {}
    pids_ok = _pids_liquidados_desde_sesion(sid)
    checks_mo = s.get("checks_mo", []) or []
    checks_mat = s.get("checks_mat", []) or []
    proyectos = {}

    def _registrar(pid, pro_code="", nombre="", odm="", nodo="", fuente=""):
        if not pid:
            return
        info = proyectos.setdefault(pid, {
            "pid": pid,
            "pro_code": "",
            "nombre_local": "",
            "odm": "",
            "nodos": [],
            "fuentes": [],
            "liquidado_ok": False,
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
        _registrar(
            ck.get("pid"),
            pro_code=ck.get("pro_code", ""),
            nombre=ck.get("pnombre", ""),
            odm=row.get("_odm", ""),
            nodo=row.get("pro", ""),
            fuente="MO",
        )

    for ck in checks_mat:
        mat = ck.get("mat", {}) or {}
        _registrar(
            ck.get("pid"),
            pro_code=ck.get("pro_code", ""),
            nombre=ck.get("pnombre", ""),
            odm=mat.get("_odm", ""),
            nodo=mat.get("nodo", ""),
            fuente="MAT",
        )

    for pid, pro_code in pids_ok.items():
        _registrar(pid, pro_code=pro_code, fuente="OK")

    return proyectos


def _armar_panel_cierre_rapido(sid, fecha_raw="", pdl_raw=""):
    objetivos = _resolver_objetivos_cierre(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw)
    proyectos = _proyectos_identificados_desde_sesion(sid)
    rows = []
    meta = {
        **objetivos,
        "total": 0,
        "elegibles": 0,
        "pendientes": 0,
        "listos": 0,
        "referencia": 0,
    }

    if not proyectos:
        return rows, meta

    fields = [
        "id", "code", "name", "order_number", "nodo",
        "customer_id", "contract_id", "department_id",
        "status", "pdl",
    ] + [campo for campo, _ in CIERRE_RAPIDO_FECHAS]

    registros = _sgs().search_read(
        "project.dominion",
        [["id", "in", list(proyectos.keys())]],
        fields,
        limit=len(proyectos) + 10,
    )
    por_id = {r["id"]: r for r in registros}

    for pid, base in proyectos.items():
        reg = por_id.get(pid, {}) or {}
        pro_code = str(reg.get("code") or base.get("pro_code") or "").strip()
        nombre = str(reg.get("name") or base.get("nombre_local") or "").strip()
        order_number = str(reg.get("order_number") or base.get("odm") or "").strip()
        nodo_sgs = str(reg.get("nodo") or "").strip()
        nodos = list(base.get("nodos") or [])
        if nodo_sgs and nodo_sgs not in nodos:
            nodos.insert(0, nodo_sgs)

        customer = _texto_m2o(reg.get("customer_id")) or "-"
        contract = _texto_m2o(reg.get("contract_id")) or "-"
        department = _texto_m2o(reg.get("department_id")) or "-"
        estado_actual = _texto_m2o(reg.get("status")) or "-"
        estado_esperado = objetivos["estado_esperado"]
        status_ok = estado_actual == estado_esperado

        pdl_actual = str(reg.get("pdl") or "").strip() or "-"
        pdl_esperado = objetivos["pdl_esperado"] or "-"
        pdl_ok = _pdl_match(pdl_actual, pdl_esperado)

        fechas_diferentes = []
        fechas_ok_count = 0
        for campo, label in CIERRE_RAPIDO_FECHAS:
            actual_iso = _fecha_iso(reg.get(campo))
            if actual_iso == objetivos["fecha_iso"]:
                fechas_ok_count += 1
            else:
                fechas_diferentes.append({
                    "campo": campo,
                    "label": label,
                    "actual": _fecha_ui(reg.get(campo)),
                    "esperado": objetivos["fecha_ui"],
                })

        fechas_total = len(CIERRE_RAPIDO_FECHAS)
        fechas_ok = fechas_ok_count == fechas_total
        liquidado_ok = bool(base.get("liquidado_ok"))
        elegible = True
        necesita_actualizacion = not fechas_ok or not pdl_ok or not status_ok

        if not necesita_actualizacion:
            panel_estado = "ok"
            panel_label = "Listo"
            meta["listos"] += 1
        elif elegible:
            panel_estado = "pendiente"
            panel_label = "Pendiente"
            meta["pendientes"] += 1
        else:
            panel_estado = "referencia"
            panel_label = "Solo referencia"
            meta["referencia"] += 1

        if elegible:
            meta["elegibles"] += 1

        rows.append({
            "pid": pid,
            "pro_code": pro_code,
            "nombre": nombre or "-",
            "order_number": order_number or "-",
            "nodos": nodos,
            "nodos_txt": ", ".join(nodos) if nodos else "-",
            "customer": customer,
            "contract": contract,
            "department": department,
            "fuentes_txt": ", ".join(base.get("fuentes") or []) or "-",
            "elegible": elegible,
            "liquidado_ok": liquidado_ok,
            "preseleccionado": necesita_actualizacion,
            "panel_estado": panel_estado,
            "panel_label": panel_label,
            "necesita_actualizacion": necesita_actualizacion,
            "estado_actual": estado_actual,
            "estado_esperado": estado_esperado,
            "status_ok": status_ok,
            "pdl_actual": pdl_actual,
            "pdl_esperado": pdl_esperado,
            "pdl_ok": pdl_ok,
            "fecha_esperada_ui": objetivos["fecha_ui"],
            "fechas_ok": fechas_ok,
            "fechas_ok_count": fechas_ok_count,
            "fechas_total": fechas_total,
            "fechas_diferentes": fechas_diferentes,
        })

    rows.sort(key=lambda r: (0 if r["elegible"] else 1, r["pro_code"] or "", r["pid"]))
    meta["total"] = len(rows)
    return rows, meta


def _obtener_storage_state_activo(timeout_s: float = 4.0):
    with _cached_storage_state_lock:
        state = _cached_storage_state
    if state:
        return state

    if _pw_global:
        try:
            _browser_commands.put("force_session")
        except Exception:
            pass

        t0 = _time_mod.time()
        while (_time_mod.time() - t0) < timeout_s:
            _time_mod.sleep(0.25)
            with _cached_storage_state_lock:
                state = _cached_storage_state
            if state:
                return state

    return None


def _aplicar_cierre_rapido(sid, pids_objetivo=None, fecha_raw="", pdl_raw=""):
    s = _sesiones.get(sid)
    if not s:
        return {"hechos": 0, "errores": ["Sesion no encontrada."], "objetivos": 0}

    rows, meta = _armar_panel_cierre_rapido(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw)
    por_pid = {row["pid"]: row for row in rows}

    if pids_objetivo:
        objetivos = [
            int(pid) for pid in pids_objetivo
            if int(pid) in por_pid and por_pid[int(pid)]["elegible"]
        ]
    else:
        objetivos = [row["pid"] for row in rows if row["necesita_actualizacion"]]

    if not objetivos:
        return {"hechos": 0, "errores": [], "objetivos": 0, "meta": meta}

    storage_state = _obtener_storage_state_activo()
    if not storage_state:
        return {
            "hechos": 0,
            "errores": ["No hay sesion activa de SGS para aplicar fecha/estado. Abre el navegador de SGS y deja la sesion iniciada."],
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

    return {
        "hechos": hechos,
        "errores": errores,
        "objetivos": len(objetivos),
        "meta": meta,
    }


@app.route("/cierre_rapido/<sid>", methods=["GET", "POST"])
def cierre_rapido(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("subir"))

    fecha_raw = request.values.get("fecha", "")
    pdl_raw = request.values.get("pdl", "")

    if request.method == "POST":
        seleccion = [int(pid) for pid in request.form.getlist("pid") if str(pid).isdigit()]
        res = _aplicar_cierre_rapido(
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
            flash("No hay proyectos pendientes elegibles para corregir con esos filtros.")
        if res.get("errores"):
            flash("Errores en cierre rapido: " + " | ".join(res["errores"][:3]))
        return redirect(url_for(
            "cierre_rapido",
            sid=sid,
            fecha=(res.get("meta") or {}).get("fecha_input") or fecha_raw,
            pdl=(res.get("meta") or {}).get("pdl_esperado") or pdl_raw,
        ))

    try:
        rows, meta = _armar_panel_cierre_rapido(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw)
    except Exception as ex:
        log.error(f"[CIERRE_RAPIDO] Error armando panel sid={sid[:8]}: {ex}")
        flash(f"No se pudo consultar SGS para el panel de cierre rapido: {ex}")
        rows, meta = [], _resolver_objetivos_cierre(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw)
        meta.update({"total": 0, "elegibles": 0, "pendientes": 0, "listos": 0, "referencia": 0})

    return render_template("cierre_rapido.html", sid=sid, s=s, rows=rows, meta=meta)


@app.route("/post_liquidacion_cerrar/<sid>", methods=["POST"])
def post_liquidacion_cerrar(sid):
    flash("Se abrio el panel de cierre rapido para revisar primero fecha, PDL y estado.")
    return redirect(url_for("cierre_rapido", sid=sid))


@app.route("/log_img/<path:filename>")
def log_img(filename):
    """Sirve screenshots de la carpeta logs/ para la pÃ¡gina de reparaciÃ³n."""
    logs_dir = os.path.join(os.path.dirname(__file__), "logs")
    return send_from_directory(logs_dir, filename)


@app.route("/reparar/<sid>", methods=["POST"])
def reparar(sid):
    """
    Reintenta los Ã­tems MAT fallidos seleccionados por el usuario.
    Crea un nuevo job (rep_sid) y redirige a la pÃ¡gina de espera.
    """
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("subir"))

    resultados = s.get("resultados_liquidacion", [])
    indices = [int(i) for i in request.form.getlist("idx_reparar") if i.isdigit()]

    if not indices:
        flash("Selecciona al menos 1 item para reintentar.")
        return redirect(url_for("resultado", sid=sid))

    with _cached_storage_state_lock:
        storage_state = _cached_storage_state
    if not storage_state:
        flash("âš  Sin sesiÃ³n activa en el browser. Inicia sesiÃ³n primero.")
        return redirect(url_for("resultado", sid=sid))

    items_mat = []
    for i in indices:
        if i >= len(resultados):
            continue
        r = resultados[i]
        if r.get("ok") or r.get("tipo") != "MAT":
            continue
        if not r.get("pm_id"):
            continue
        items_mat.append({
            "tipo":           "MAT",
            "idx":            i,
            "pm_id":          r["pm_id"],
            "pid":            r.get("pid"),
            "codigo":         r.get("codigo", ""),
            "cantidad":       float(r.get("cantidad", 0)),
            "excel_neto":      float(r.get("excel_neto") or r.get("cantidad") or 0),
            "pro_code":       r.get("pro_code", ""),
            "partner_nombre": r.get("partner_nombre", ""),
            "pendiente_api_confirmado": True,
        })

    if not items_mat:
        flash("No hay materiales MAT reintentables entre los seleccionados.")
        return redirect(url_for("resultado", sid=sid))

    rep_sid = sid + "_rep_" + str(uuid.uuid4())[:8]
    _sesiones[rep_sid] = {
        "filename":               s.get("filename", "") + "  [REPARACIÃ“N]",
        "resultados_liquidacion": None,
        "debug_liquidacion":      None,
    }
    _trabajos[rep_sid] = {
        "estado":    "corriendo",
        "items_mo":  [],
        "items_mat": [{"codigo": it["codigo"], "tipo": "MAT",
                       "worker_id": 0, "estado": "pendiente", "error": None}
                      for it in items_mat],
        "resultados": None,
        "error":      None,
        "t_inicio":   _time_mod.time(),
    }
    threading.Thread(
        target=_ejecutar_liquidacion_bg,
        args=(rep_sid, [], items_mat, storage_state),
        daemon=True,
    ).start()

    flash(f"Reintentando {len(items_mat)} material(es)â€¦")
    return redirect(url_for("esperar", sid=rep_sid))


@app.route("/cerrar_navegador", methods=["POST"])
def cerrar_navegador():
    _browser_commands.put("close_browser")
    flash("SeÃ±al de cierre enviada al navegador.")
    return redirect(request.referrer or url_for("subir"))


@app.route("/restart_server", methods=["POST"])
def restart_server():
    """Alias usado por la interfaz del módulo julian."""
    _programar_restart_servidor()
    return jsonify({"ok": True, "msg": "Reiniciando..."})


if __name__ == "__main__":
    # threaded=True: permite servir /progreso/ mientras corre la liquidaciÃ³n en
    # un hilo de fondo. Playwright ya no vive en el hilo principal; cada
    # PlaywrightClient arranca y cierra dentro del thread que lo usa.
    app.run(debug=True, port=5001, host="127.0.0.1", threaded=True,
            use_reloader=False,
            extra_files=[
                # Plantillas del módulo julian
                *__import__("glob").glob("templates/julian/*.html"),
                *__import__("glob").glob("templates/*.html"),
            ])
