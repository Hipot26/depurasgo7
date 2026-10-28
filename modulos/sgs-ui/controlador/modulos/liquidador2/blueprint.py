"""
blueprint.py — Liquidador 2: flujo vigente del Liquidador Automático.
Monta en /liquidador2/ con su propio estado de sesiones, cliente SGS de solo lectura
y monitor de Playwright, independiente del módulo heredado.
Flujo: subir → verificar → aprobar → plan → liquidar → esperar → resultado.
Parser de documentos propio (parser_liquidador2.py); reglas de validación y asociación
compartidas con el verificador (modulos/verificador.py).
La liquidación de mano de obra y materiales se ejecuta en _ejecutar_liquidacion_bg()
con la sesión del ERP del usuario (ver docs/INTEGRACION-SGS.md).
"""

import io
import json
import logging
import os
import pickle
import queue as _queue_mod
import re
import threading
import time as _time_mod
import uuid
from collections import Counter
from datetime import datetime

import requests

import openpyxl
from flask import (Blueprint, render_template, request,
                   redirect, url_for, flash, jsonify,
                   send_from_directory, send_file)

from sgs_readonly_client import SGSReadOnlyClient
from modulos.verificador  import (verificar_fila_mo, verificar_materiales_proyecto,
                                   _nodo_equiv, _nodo_match, _nodo_match_smart,
                                   _nodo_num,
                                   _cable_nodo_match, _nodo_en_rango_cable,
                                   _filtrar_no_linea_viva,
                                   buscar_proyectos_por_df_nombre)
from browser_client       import (PlaywrightClient, _safe as _safe_str, _clasificar_error,
                                   liquidar_mo_parallel_por_proyecto,
                                   liquidar_materiales_lista_modal_ws)

from modulos.liquidador2.parser_liquidador2 import (
    parsear_mo, parsear_mat,
    extraer_total_mo, extraer_pdl_programado,
    extraer_nodos_faltantes_mo,
    extraer_distribucion_mo,
    extraer_fecha_ejecucion,
    es_plantilla_mat_masiva,
)
from modulos.liquidador2.ia_lideres import (
    parsear_lineas_nodo_lider, agrupar_por_nodo, llamar_ia_asignaciones,
    resolver_nodos_contra_planilla,
)

USE_POOL_V2 = False
USE_MAT_WS  = True   # Work-stealing: tab libre ayuda al otro cuando termina su parte
MO_MAX_PROJECT_WORKERS   = 4   # Workers MO simultáneos — máx 4 browsers activos a la vez
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MAT_MAX_CONCURRENT_NODES = 4   # Nodos MAT en paralelo; 2 tabs c/u = 8 workers totales
MO_MAT_STAGGER_S         = 8   # Segundos entre inicio MO y inicio MAT (misma regla que entre workers)
MO_WORKERS_PER_NODE      = 1   # 1 worker por nodo — sin sub-workers ni pre-flight
TERMINADO_MAX_WORKERS    = 4   # Máx workers paralelos para Fase 3 (TERMINADO/fechas); 1 browser por nodo

# ── Blueprint ─────────────────────────────────────────────────────────────────
liquidador2 = Blueprint(
    "liquidador2",
    __name__,
    template_folder="../../templates/liquidador2",
    url_prefix="/liquidador2",
)

log = logging.getLogger("liquidador2")

# ── SGS client (propio, independiente del app principal) ──────────────────────
SGS_URL      = os.environ.get("SGS_URL",      "")
SGS_DB       = os.environ.get("SGS_DB",       "")
# uid de la cuenta de servicio en SGS: filtra las líneas de detalle creadas por el liquidador
SGS_UID_SERVICIO = int(os.environ.get("SGS_UID_SERVICIO") or 0)
SGS_LOGIN    = os.environ.get("SGS_LOGIN",    "")
SGS_PASSWORD = os.environ.get("SGS_PASSWORD", "")

_sgs_cache_l2: SGSReadOnlyClient = None


def _sgs() -> SGSReadOnlyClient:
    global _sgs_cache_l2
    if _sgs_cache_l2 and _sgs_cache_l2.uid:
        try:
            _sgs_cache_l2.search("res.users", [["id", "=", _sgs_cache_l2.uid]], limit=1)
            return _sgs_cache_l2
        except Exception:
            pass
    c = SGSReadOnlyClient(SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD)
    c.autenticar()
    _sgs_cache_l2 = c
    return c


def _cant_sgs(cantidad) -> str:
    """Cantidad en formato es_CO de SGS (ver browser_client._cant_sgs; CHANGELOG 2026-10-16)."""
    from browser_client import _cant_sgs as _f
    return _f(cantidad)


def _nuevo_sgs_readonly() -> SGSReadOnlyClient:
    c = SGSReadOnlyClient(SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD)
    c.autenticar()
    return c


# ── Estado en memoria ─────────────────────────────────────────────────────────
_sesiones: dict = {}

# Persistencia de sesiones: el boton "Reiniciar servidor" mata el proceso con
# os._exit(0), lo que borraba _sesiones y dejaba toda pestana abierta con
# "Sesion no encontrada" al primer click. Las sesiones se copian a disco tras
# cada request mutante (throttled) y se restauran al arrancar el proceso.
_SESIONES_PKL = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "logs", "liquidador2_sesiones.pkl"))
_SESIONES_MAX      = 30   # cap: solo las mas recientes
_SESIONES_MAX_DIAS = 7    # descartar sesiones mas viejas al restaurar
_ses_dump_lock = threading.Lock()
_ses_last_dump = [0.0]


def _persistir_sesiones(force: bool = False):
    """Copia _sesiones a disco (atomico via .tmp+replace; max 1 dump / 2 s)."""
    now = _time_mod.time()
    if not force and now - _ses_last_dump[0] < 2.0:
        return
    if not _ses_dump_lock.acquire(blocking=False):
        return
    try:
        _ses_last_dump[0] = now
        data = {}
        for sid, s in list(_sesiones.items()):
            try:
                # roundtrip = deep-copy congelada: no se rompe si un thread de
                # liquidacion muta la sesion a mitad del dump, y filtra
                # sesiones con objetos no picklables
                data[sid] = pickle.loads(pickle.dumps(s))
            except Exception:
                continue
        tmp = _SESIONES_PKL + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(data, f)
        os.replace(tmp, _SESIONES_PKL)
    except Exception as ex:
        log.debug("[L2] persistir sesiones fallo: %s", ex)
    finally:
        _ses_dump_lock.release()


def _restaurar_sesiones():
    try:
        if not os.path.exists(_SESIONES_PKL):
            return
        with open(_SESIONES_PKL, "rb") as f:
            data = pickle.load(f)
        ahora = datetime.now()

        def _reciente(s):
            try:
                dt = datetime.strptime(s.get("creado_at", ""), "%Y-%m-%d %H:%M")
                return (ahora - dt).days <= _SESIONES_MAX_DIAS
            except Exception:
                return True

        vivas = {sid: s for sid, s in data.items() if isinstance(s, dict) and _reciente(s)}
        orden = sorted(vivas, key=lambda k: vivas[k].get("creado_at", ""), reverse=True)
        _sesiones.update({k: vivas[k] for k in orden[:_SESIONES_MAX]})
        if _sesiones:
            log.info("[L2] %d sesion(es) restauradas de disco tras reinicio",
                     len(_sesiones))
    except Exception as ex:
        log.warning("[L2] no se pudieron restaurar sesiones: %s", ex)


_restaurar_sesiones()

# Endpoints de polling permanente que no tocan sesiones — no disparan dump
_NO_PERSIST_ENDPOINTS = {"liquidador2.pw_status", "liquidador2.sesion_estado"}


@liquidador2.after_request
def _persistir_tras_request(resp):
    if (request.endpoint or "") not in _NO_PERSIST_ENDPOINTS:
        _persistir_sesiones()
    return resp
_trabajos: dict = {}

# ── Playwright persistente ────────────────────────────────────────────────────
_pw_global: PlaywrightClient        = None
_pw_lock                             = threading.Lock()
_cached_storage_state: dict         = None
_cached_storage_state_lock           = threading.Lock()
_browser_commands: _queue_mod.Queue = _queue_mod.Queue()
_monitor_running                     = False

SGS_URL_PW = os.environ.get("SGS_URL", "")


def sesion_activa(usuario=None) -> bool:
    """SGO7: con usuario SGO7 (cabecera) → su propia sesión; sin usuario → la de servicio."""
    u = usuario or _usuario_actual()
    if u:
        e = _pw_users.get(u)
        return bool(e and e.get("logueado"))
    return bool(_pw_global and _pw_global._logueado)


# ── SGO7: sesiones SGS POR USUARIO — cada quien liquida con SU cuenta ────────────────
# La cuenta se registra una vez en SGO7 (Liquidador → Mi cuenta SGS) y queda cifrada en la base
# de sabogal. Cada usuario tiene SU hilo con SU Chromium (la API síncrona de Playwright no admite
# dos navegadores en un mismo hilo): Flask y el monitor solo encolan; el hilo del usuario abre,
# vigila cada 4 s, renueva si SGS cierra la sesión y cierra a las 8 h sin uso.
_pw_users: dict = {}
_pw_users_lock = threading.Lock()
_PW_IDLE_S = 8 * 3600
_PW_SONDA_S = 300        # cada 5 min se pregunta a SGS si la cookie de cada sesión sigue viva
_PW_VERIF_TTL_S = 60     # antes de escribir, una comprobación vale 60 s
_sonda_servicio = [0.0]  # última comprobación de la sesión de servicio (monitor)


def _sesion_sgs_viva(state):
    """Pregunta a SGS (lectura: /web/session/get_session_info) si la cookie del storage_state sigue valiendo.
    True = viva · False = SGS ya no la reconoce (pide login) · None = no se pudo comprobar (red): no bloquea.
    La página persistente de cada sesión no se recarga, así que su URL no dice si SGS cerró la sesión
    (SGS cierra sesiones sin avisar; sin esta sonda, una sesión muerta se seguía dando por viva)."""
    try:
        host = SGS_URL_PW.split("://", 1)[-1].split("/", 1)[0]
        cookies = {c["name"]: c["value"] for c in (state or {}).get("cookies", [])
                   if host.endswith(str(c.get("domain") or "").lstrip("."))}
        if not cookies.get("session_id"):
            return False
        r = requests.post(f"{SGS_URL_PW}/web/session/get_session_info",
                          json={"jsonrpc": "2.0", "method": "call", "params": {}},
                          cookies=cookies, timeout=15)
        j = r.json()
        if j.get("error"):
            return False
        return bool((j.get("result") or {}).get("uid"))
    except Exception:
        return None


def _usuario_actual():
    try:
        return (request.headers.get("X-SGO7-User") or "").strip().lower() or None
    except Exception:
        return None


def _entrada_usuario(u):
    with _pw_users_lock:
        return _pw_users.setdefault(u, {"pw": None, "state": None, "logueado": False, "error": "", "login": None,
                                        "ultimo_uso": 0.0, "ultimo_intento": 0.0, "auto": False,
                                        "pendiente": False, "registrada": None,
                                        "cola": _queue_mod.Queue(), "hilo": None})


def _hilo_de(u):
    e = _entrada_usuario(u)
    h = e.get("hilo")
    if h is None or not h.is_alive():
        h = threading.Thread(target=_hilo_usuario, args=(u,), daemon=True, name=f"pw-user-{u}")
        e["hilo"] = h
        h.start()
    return e


def _asegurar_sesion_usuario(u, motivo="uso", forzar=False):
    """Flask → hilo del usuario: abre o renueva su sesión SGS (no bloquea la petición)."""
    if not u:
        return
    e = _hilo_de(u)
    e["ultimo_uso"] = _time_mod.time()
    e["auto"] = True
    if forzar or (not e["logueado"] and not e["pendiente"]):
        e["pendiente"] = True
        e["cola"].put(("login", motivo))


def _sesion_verificada(u, e) -> bool:
    """True si la sesión propia de `u` sigue viva en SGS (comprobada hace < 60 s, o ahora). Si SGS ya la
    cerró, la marca caída y pide al hilo del usuario entrar de nuevo con su cuenta (navegador limpio)."""
    ahora = _time_mod.time()
    if ahora - (e.get("verificada_ts") or 0.0) < _PW_VERIF_TTL_S:
        return True
    if _sesion_sgs_viva(e.get("state")) is False:
        log.warning(f"[L2 USUARIOS] SGS ya no reconoce la sesión de {u} — reabriendo con su cuenta antes de escribir")
        _reg_sesion(u, "caducada", {"cuenta": e.get("login"), "motivo": "comprobación antes de escribir"})
        e.update(logueado=False, state=None, caducada=True, verificada_ts=0.0)
        if not e.get("pendiente"):
            _asegurar_sesion_usuario(u, "sesion caducada en SGS", forzar=True)
        return False
    e["verificada_ts"] = ahora
    return True


def _storage_state_escritura():
    """storage_state para ESCRIBIR en SGS: solo la sesión propia del usuario SGO7, comprobada contra SGS.
    Sin usuario SGO7 (uso interno) cae a la sesión de servicio. Sin cuenta propia o sin sesión lista en 60 s
    → None (no se liquida)."""
    u = _usuario_actual()
    if not u:
        with _cached_storage_state_lock:
            return _cached_storage_state
    return _storage_state_escritura_de(u)


def _storage_state_escritura_de(u, espera_s: float = 60.0):
    """Como _storage_state_escritura, para hilos de fondo (no hay petición: `u` se captura en la ruta).
    Espera hasta `espera_s` a que la sesión SGS propia del usuario quede lista (tras un reinicio o 8 h sin
    uso se abre sola, ~10-20 s). Sin usuario o sin sesión propia → None: nunca se escribe en SGS con la
    sesión de servicio. La sesión se comprueba contra SGS antes de entregarla; si SGS la cerró o no hay
    sesión abierta, se pide abrirla con la cuenta del usuario y se espera."""
    if not u:
        return None
    t0 = _time_mod.time()
    pedida = False
    while True:
        e = _pw_users.get(u)
        if e and e.get("logueado") and e.get("state") and _sesion_verificada(u, e):
            e["ultimo_uso"] = _time_mod.time()
            return e["state"]
        if (e and e.get("registrada") is False) or _time_mod.time() - t0 >= espera_s:
            return None
        if not pedida and (e is None or not (e.get("logueado") or e.get("pendiente"))):
            _asegurar_sesion_usuario(u, "liquidar")
            pedida = True
        _time_mod.sleep(1.0)


def _motivo_sin_sesion(u) -> str:
    """Por qué no hay sesión SGS propia para escribir (texto para la UI)."""
    if not u:
        return "Sin usuario SGO7 — entra por la plataforma y reintenta"
    e = _pw_users.get(u) or {}
    if e.get("registrada") is False:
        return "Sin cuenta SGS registrada en SGO7 (Liquidador → Mi cuenta SGS)"
    det = (e.get("error") or "no abrió en 60 s")[:160]
    return f"Tu sesión de SGS no está lista ({det}) — recarga esta página y reintenta"


def _reg_sesion(u, estado, detalle):
    try:
        import sgo7_registro
        sgo7_registro.registrar("liquidador_sesion_sgs", estado=estado, usuario=u, detalle=detalle)
    except Exception:
        pass


def _abrir_sesion_usuario(u, motivo="uso"):
    """SOLO desde el hilo del usuario. Abre Chromium sin ventana y entra a SGS con su cuenta."""
    import sgs_cuentas
    e = _entrada_usuario(u)
    e["ultimo_intento"] = _time_mod.time()
    e["pendiente"] = False
    cred = sgs_cuentas.obtener(u)
    if not cred:
        e.update(logueado=False, state=None, registrada=False,
                 error="Sin cuenta SGS registrada en SGO7 (Liquidador → Mi cuenta SGS)")
        return
    e["registrada"] = True
    e["login"] = cred.get("sgs_login")
    pw = e.get("pw")
    try:
        if pw is None or pw._page is None or motivo in ("manual", "cuenta cambiada", "sesion caducada en SGS"):   # manual = navegador limpio: valida la cuenta de verdad
            if pw:
                try: pw.close()
                except Exception: pass
            e.update(logueado=False, state=None)   # mientras se abre, la sesión no cuenta como activa
            pw = PlaywrightClient(headless=_PW_HEADLESS)
            pw.start()
            e["pw"] = pw
        pw.login(cred["sgs_login"], cred["sgs_password"])
        e["state"] = pw._page.context.storage_state()
        pw._logueado = True
        _ahora = _time_mod.time()
        e.update(logueado=True, error="", caducada=False, verificada_ts=_ahora, sondeada_ts=_ahora)
        sgs_cuentas.reportar(u, True)
        log.info(f"[L2 USUARIOS] Sesión SGS de {u} ({e['login']}) lista — {motivo}")
        _reg_sesion(u, "ok", {"cuenta": e["login"], "motivo": motivo})
    except Exception as ex:
        e.update(logueado=False, state=None, error=_safe_str(ex)[:300])
        sgs_cuentas.reportar(u, False, e["error"])
        try:
            if e.get("pw"): e["pw"].close()
        except Exception:
            pass
        e["pw"] = None
        log.error(f"[L2 USUARIOS] Login SGS de {u} ({e.get('login')}) falló: {ex}")
        _reg_sesion(u, "error", {"cuenta": e.get("login"), "motivo": motivo, "error": e["error"]})


def _cerrar_sesion_usuario(u, motivo="manual"):
    e = _pw_users.get(u)
    if not e:
        return
    try:
        if e.get("pw"): e["pw"].close()
    except Exception:
        pass
    e.update(pw=None, state=None, logueado=False, auto=False, pendiente=False)
    log.info(f"[L2 USUARIOS] Sesión SGS de {u} cerrada — {motivo}")


def _check_usuario(u):
    """SOLO desde el hilo del usuario, cada ~4 s: vigila la sesión; si SGS la cerró, la reabre; si
    lleva 8 h sin uso, la cierra."""
    e = _pw_users.get(u)
    if not e:
        return
    ahora = _time_mod.time()
    pw = e.get("pw")
    if pw is None:
        if (e.get("auto") and e.get("registrada") is not False and not e["pendiente"]
                and ahora - e["ultimo_intento"] > 90 and ahora - e["ultimo_uso"] < _PW_IDLE_S):
            _abrir_sesion_usuario(u, "reapertura")
        return
    if ahora - e["ultimo_uso"] > _PW_IDLE_S:
        _cerrar_sesion_usuario(u, "8 h sin uso")
        return
    ok = False
    try:
        cur = pw._page.url
        ok = SGS_URL_PW in cur and "/web" in cur and "/login" not in cur
        if not ok:
            for sel in (".o_main_navbar", ".o_web_client", ".o_action_manager"):
                try:
                    if pw._page.locator(sel).first.is_visible(timeout=1000):
                        ok = True
                        break
                except Exception:
                    continue
    except Exception:
        ok = False
    if ok and e.get("logueado") and e.get("state") and ahora - (e.get("sondeada_ts") or 0.0) >= _PW_SONDA_S:
        # la página quieta conserva su URL aunque SGS haya cerrado la sesión: cada 5 min se le pregunta a SGS
        e["sondeada_ts"] = ahora
        if _sesion_sgs_viva(e["state"]) is False:
            e["caducada"] = True
            _reg_sesion(u, "caducada", {"cuenta": e.get("login"), "motivo": "vigilancia cada 5 min"})
        else:
            e["verificada_ts"] = ahora
    if ok and not e.get("caducada"):
        if not e["logueado"]:
            try: e["state"] = pw._page.context.storage_state()
            except Exception: pass
            e["logueado"] = True
    else:
        e["logueado"] = False
        if e.get("auto") and not e.get("pendiente") and ahora - e["ultimo_intento"] > 60:
            log.info(f"[L2 USUARIOS] SGS cerró la sesión de {u} — reabriendo con su cuenta")
            _abrir_sesion_usuario(u, "sesion caducada en SGS")


def _hilo_usuario(u):
    e = _entrada_usuario(u)
    log.info(f"[L2 USUARIOS] Hilo de sesión SGS iniciado para {u}")
    while True:
        try:
            cmd, motivo = e["cola"].get(timeout=4)
        except _queue_mod.Empty:
            cmd, motivo = "check", ""
        try:
            if cmd == "login":
                _abrir_sesion_usuario(u, motivo)
            elif cmd == "close":
                _cerrar_sesion_usuario(u, motivo)
                break
            else:
                _check_usuario(u)
        except Exception as ex:
            log.warning(f"[L2 USUARIOS] {u}: {ex}")
    e["hilo"] = None
    log.info(f"[L2 USUARIOS] Hilo de {u} terminado")


def _comando_usuario(cmd, u=None, motivo="manual"):
    """Desde el monitor (cola general): solo reenvía al hilo del usuario; nada de Playwright aquí."""
    if cmd == "user_login" and u:
        _asegurar_sesion_usuario(u, motivo, forzar=True)
    elif cmd == "user_close" and u:
        e = _pw_users.get(u)
        if e and e.get("hilo") and e["hilo"].is_alive():
            e["cola"].put(("close", motivo))
        elif e:
            _cerrar_sesion_usuario(u, motivo)


def _tick_usuarios():
    """Compatibilidad: la vigilancia vive en el hilo de cada usuario."""
    return


# ── SGO7 modo servidor: sesión SGS automática (sin ventana, sin login manual) ─────────
_PW_HEADLESS = os.environ.get("PW_HEADLESS", "1") == "1"      # servidor: Chromium sin ventana
_PW_AUTO     = os.environ.get("PW_AUTO_SESION", "1") == "1"   # abrir y entrar a SGS solo
_auto_deseado      = _PW_AUTO
_pw_ultimo_error   = ""
_pw_ultimo_intento = 0.0


def _abrir_sesion_automatica():
    """Abre Chromium y entra a SGS con las credenciales del módulo; deja el storage_state
    listo para los workers. Reemplaza el flujo de PC (ventana visible + login manual)."""
    global _pw_global, _cached_storage_state, _pw_ultimo_error, _pw_ultimo_intento
    _pw_ultimo_intento = _time_mod.time()
    if _pw_global:
        try: _pw_global.close()
        except Exception: pass
        _pw_global = None
    pw = None
    try:
        pw = PlaywrightClient(headless=_PW_HEADLESS)
        pw.start()
        pw.login()
        state = pw._page.context.storage_state()
        with _cached_storage_state_lock:
            _cached_storage_state = state
        pw._logueado = True
        _pw_global = pw
        _pw_ultimo_error = ""
        log.info("[L2 MONITOR] Sesión SGS automática lista (headless=%s)", _PW_HEADLESS)
    except Exception as ex:
        _pw_ultimo_error = _safe_str(ex)[:300]
        log.error(f"[L2 MONITOR] No se pudo abrir la sesión SGS automática: {ex}")
        try:
            if pw: pw.close()
        except Exception: pass
        _pw_global = None


def _relogin_automatico():
    """La sesión de SGS caducó: volver a entrar en el mismo navegador."""
    global _cached_storage_state, _pw_ultimo_error, _pw_ultimo_intento
    _pw_ultimo_intento = _time_mod.time()
    try:
        _pw_global.login()
        state = _pw_global._page.context.storage_state()
        with _cached_storage_state_lock:
            _cached_storage_state = state
        _pw_global._logueado = True
        _pw_ultimo_error = ""
        log.info("[L2 MONITOR] Sesión SGS renovada automáticamente")
    except Exception as ex:
        _pw_ultimo_error = _safe_str(ex)[:300]
        log.warning(f"[L2 MONITOR] Re-login automático falló: {ex}")


def _monitor_loop():
    global _cached_storage_state, _pw_global, _monitor_running, _auto_deseado
    _monitor_running = True
    log.info("[L2 MONITOR] Loop iniciado")

    while True:
        _time_mod.sleep(4)
        try:
            try:
                cmd = _browser_commands.get_nowait()
                log.info(f"[L2 MONITOR] Comando: {cmd}")
                if isinstance(cmd, tuple):            # SGO7: comandos por usuario
                    _comando_usuario(*cmd)
                    continue
                if cmd == "restart_browser":
                    _auto_deseado = True
                    _abrir_sesion_automatica()
                elif cmd == "close_browser":
                    _auto_deseado = False
                    if _pw_global:
                        try: _pw_global.close()
                        except: pass
                        _pw_global = None
                elif cmd == "force_session":
                    if _pw_global and _pw_global._page:
                        try:
                            state = _pw_global._page.context.storage_state()
                            with _cached_storage_state_lock:
                                _cached_storage_state = state
                            _pw_global._logueado = True
                        except Exception as ex:
                            log.error(f"[L2 MONITOR] force_session falló: {ex}")
                elif cmd == "reverificar":
                    if _pw_global:
                        _pw_global._logueado = False
                    with _cached_storage_state_lock:
                        _cached_storage_state = None
            except _queue_mod.Empty:
                pass

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

                # la URL de la página quieta no dice si SGS cerró la sesión de servicio: cada 5 min se le pregunta
                if (url_ok or dom_ok) and _time_mod.time() - _sonda_servicio[0] >= _PW_SONDA_S:
                    _sonda_servicio[0] = _time_mod.time()
                    with _cached_storage_state_lock:
                        _st71 = _cached_storage_state
                    if _st71 and _sesion_sgs_viva(_st71) is False:
                        log.warning("[L2 MONITOR] SGS ya no reconoce la sesión de servicio — volviendo a entrar")
                        url_ok = dom_ok = False
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
                    # SGO7: la sesión de SGS caducó → volver a entrar sin intervención
                    if _auto_deseado and _time_mod.time() - _pw_ultimo_intento > 60:
                        _relogin_automatico()

            _tick_usuarios()   # SGO7: sesiones por usuario
            # SGO7: sin navegador (arranque, caída o error) → abrirlo solo, con pausa entre intentos
            if _pw_global is None and _auto_deseado and _time_mod.time() - _pw_ultimo_intento > 90:
                log.info("[L2 MONITOR] Sin sesión SGS — abriendo automáticamente")
                _abrir_sesion_automatica()

        except Exception as ex:
            log.warning(f"[L2 MONITOR] Excepción: {ex}")


threading.Thread(target=_monitor_loop, daemon=True, name="pw-monitor-l2").start()


# ── Constantes cierre rápido ──────────────────────────────────────────────────
CIERRE_RAPIDO_FECHAS = [
    ("date_assignment",    "Asignacion"),
    ("date_expected",      "Prevista"),
    ("date_real",          "Real"),
    ("certification_date", "Certificacion"),
    ("date_end_expected",  "Fin prevista"),
    ("date_end_real",      "Fin real"),
    ("last_status_change", "Ult. cambio estado"),
]
CIERRE_RAPIDO_ESTADO = "1.4-TERMINADO/EN BALANCE"

# Modo de cierre (Fase 3 del job de liquidación y panel de cierre rápido):
#   fecha_estado → flujo normal: 7 fechas + PDL + estado.
#   solo_estado  → plantilla MAT masiva (Verificador SGS → 'Automático por Mes'):
#                  nodos con MO liquidada en meses anteriores a los que solo
#                  faltaba MAT. Fechas y PDL ya son correctos y NO se tocan;
#                  solo se pone CIERRE_RAPIDO_ESTADO a los nodos que no lo tengan.
CIERRE_MODO_FECHA_ESTADO = "fecha_estado"
CIERRE_MODO_SOLO_ESTADO  = "solo_estado"
CIERRE_MODOS             = (CIERRE_MODO_FECHA_ESTADO, CIERRE_MODO_SOLO_ESTADO)


def _cierre_modo(s) -> str:
    """Modo de cierre de la sesión (default: fecha + estado)."""
    modo = str((s or {}).get("cierre_modo") or "").strip()
    return modo if modo in CIERRE_MODOS else CIERRE_MODO_FECHA_ESTADO


def _estados_proyectos_api(pids) -> dict:
    """{pid: texto del estado actual} leído fresco de project.dominion.status."""
    pids = [int(p) for p in pids if p]
    if not pids:
        return {}
    out = {}
    for r in _sgs().search_read("project.dominion", [["id", "in", pids]],
                                ["id", "status"], limit=len(pids) + 5):
        out[r["id"]] = _texto_m2o(r.get("status")) or ""
    return out


# ── Utilidades ────────────────────────────────────────────────────────────────
def _codigo_programado_desde_nombre(filename: str) -> str:
    m = re.search(r"\b(6300\d{4,})\b", filename or "")
    return m.group(1) if m else ""


def _f4(value) -> float:
    return round(float(value or 0), 4)


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
    for fmt, ancho in [("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16),
                       ("%Y-%m-%d", 10), ("%d-%m-%Y", 10), ("%d/%m/%Y", 10)]:
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

# LCL: 10 dígitos que empiezan por 63 (ej. 6301003394, 6300997921)
_LCL_RE = re.compile(r"^63\d{8}$")


def _lcl_para_nombre(order_number, fallback="") -> str:
    """
    Prefijo del nombre de nodos hijos: el LCL del padre (order_number en SGS).
    Los hijos en SGS se llaman '<LCL> NODO X' / '<LCL> Nx_Ny' — nunca con el PDL.
    Si el padre no tiene un LCL válido se usa el fallback (ticket del Excel).
    """
    v = str(order_number or "").strip()
    if _LCL_RE.match(v):
        return v
    return str(fallback or "").strip()


def _asegurar_lcl_nf(s) -> dict:
    """
    Garantiza nf['lcl'] (prefijo real del nombre de los hijos) cuando hay PRO padre.
    Se cachea en la sesión para no consultar SGS en cada render.
    """
    nf = s.get("nodos_faltantes") or {}
    if not nf.get("parent_pro_code"):
        return nf
    # padre_chk: ¿el PRO padre tiene el check 'Contiene sub proyectos'? Se cachea
    # por código (si la sesión se sana hacia el padre real, se vuelve a leer).
    chk_ok = (nf.get("padre_chk") or {}).get("code") == nf["parent_pro_code"]
    if nf.get("lcl") and chk_ok:
        return nf
    try:
        pr = _sgs().search_read("project.dominion",
                                [["code", "=", nf["parent_pro_code"]]],
                                ["order_number", "name", "contains_subprojects"], limit=1)
        cambios = {}
        if not nf.get("lcl"):
            lcl = _lcl_para_nombre(pr[0].get("order_number") if pr else "",
                                   nf.get("ticket", ""))
            if lcl:
                cambios["lcl"] = lcl
        if pr and not chk_ok:
            chk = {
                "code": nf["parent_pro_code"],
                "name": pr[0].get("name") or "",
                "individual": not pr[0].get("contains_subprojects"),
                "solitarios": False,   # familia sin padre real → crear hermanos individuales
                "nombres": {},
            }
            if chk["individual"]:
                try:
                    full = _sgs().search_read(
                        "project.dominion", [["code", "=", nf["parent_pro_code"]]],
                        ["id", "code", "name", "nodo", "ticket", "order_number"], limit=1)[0]
                    real, _av = _padre_real_para_nodos(_sgs(), full)
                    chk["padre_real"] = real.get("code", "")
                except PadreIndividualError as pie:
                    chk["solitarios"] = True
                    chk["nombres"] = {
                        str(n): _nombre_hermano_individual(pie.individual, pie.fam, n)[0]
                        for n in nf.get("nodos_faltantes", [])}
                except ValueError:
                    pass
            cambios["padre_chk"] = chk
        if cambios:
            nf = {**nf, **cambios}
            s["nodos_faltantes"] = nf
    except Exception as ex:
        log.warning("[L2] _asegurar_lcl_nf %s: %s", nf.get("parent_pro_code"), ex)
    return nf


def _pdl_match(actual: str, esperado: str) -> bool:
    """True si todos los PDL esperados ya estan en el campo actual."""
    if not esperado or esperado == "-":
        return True
    if actual == esperado:
        return True
    actual_set = {p for p in _PDL_SEP.split(str(actual or "")) if p and p != "-"}
    esperado_set = {p for p in _PDL_SEP.split(str(esperado or "")) if p and p != "-"}
    return bool(esperado_set) and esperado_set.issubset(actual_set)


_MAT_QTY_TOL = 0.001
_MAT_ESTADOS_LIQUIDABLES = {"ok", "avance_parcial"}


def _mat_es_liquidable(mc: dict) -> bool:
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


def _mat_cantidad_excel_pendiente(mc: dict, advance_actual=None):
    mat = mc.get("mat", {})
    excel_neto = _f4(mat.get("sgs_neto_excel"))
    # SGS_BALANCE es la verdad: no bloqueamos por diferencia de timing con la API en vivo.
    # verificar_fila_mat ya genera advertencia informativa si difieren.
    advance           = _f4(advance_actual if advance_actual is not None else mc.get("advance_real"))
    pendiente_excel   = _f4(excel_neto - advance)
    if pendiente_excel <= _MAT_QTY_TOL:
        return 0.0, None
    return pendiente_excel, None


def _cedula_lider_mat(mc: dict, nombre: str, sgs_client=None) -> str:
    """
    Cédula 'C<digitos>' del líder para seleccionar el Equipo por vat en el
    dropdown del modal MAT (única en SGS). Buscar por primera palabra del nombre
    elegía al primer homónimo: caso real PRO-060053, dos líderes CARDENAS en el
    nodo → equipo equivocado → "Stock no disponibles para el consumo".
    Fuentes en orden: mapa del verificador (bandejas/details), partner_cedula
    del check, lookup exacto en res.partner (solo si el nombre es único).
    Retorna '' si no se puede resolver — el browser cae a nombre COMPLETO único.
    """
    nombre = (nombre or "").strip()
    if not nombre:
        return ""
    ced = (mc.get("lideres_cedulas") or {}).get(nombre, "")
    if not ced and nombre == (mc.get("partner_nombre") or ""):
        ced = mc.get("partner_cedula", "") or ""
    if not ced and sgs_client is not None:
        try:
            rows = sgs_client.search_read(
                "res.partner", [["name", "=", nombre]], ["id", "vat"], limit=2)
            if len(rows) == 1:
                ced = str(rows[0].get("vat") or "").strip()
        except Exception:
            pass
    num = str(ced or "").strip().lstrip("Cc").strip()
    return ("C" + num) if num.isdigit() else ""


def _mat_res_error(it: dict, error: str, error_tipo: str, cantidad=0) -> dict:
    return {"ok": False, "tipo": "MAT", "codigo": it.get("codigo", ""),
            "pro_code": it.get("pro_code", ""), "cantidad": cantidad,
            "pid": it.get("pid"), "pm_id": it.get("pm_id"),
            "partner_nombre": it.get("partner_nombre", ""),
            "partner_cedula": it.get("partner_cedula", ""),
            "ban_justificacion": it.get("ban_justificacion", ""),
            "error": error, "error_tipo": error_tipo}


def _mat_res_omitido(it: dict, msg: str) -> dict:
    return {"ok": True, "tipo": "MAT", "codigo": it.get("codigo", ""),
            "pro_code": it.get("pro_code", ""), "cantidad": 0,
            "pid": it.get("pid"), "pm_id": it.get("pm_id"),
            "partner_nombre": it.get("partner_nombre", ""),
            "partner_cedula": it.get("partner_cedula", ""),
            "ban_justificacion": it.get("ban_justificacion", ""), "msg": msg}


def _grupos_lideres_mo(checks_mo):
    grupos, vistos = [], set()
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
        grupos.append({"key": key, "pro_code": ck.get("pro_code", ""),
                       "pnombre": ck.get("pnombre", ""), "lideres": lideres})
    return grupos


def _resolver_pid_nodo(c, nodo_str, rows_mo, checks_mo, odm=""):
    import re as _re
    # Detecta si nodo_str es un rango (ej: "1-6", "1AL6") por presencia de 2+ números.
    _nodo_es_rango = len(_re.findall(r'\d+', str(nodo_str or ""))) >= 2

    for row, ck in zip(rows_mo, checks_mo):
        if str(row.get("pro", "")) == str(nodo_str) and ck.get("pid"):
            return ck["pid"]

    if odm:
        try:
            candidatos = c.search_read(
                "project.dominion", [["order_number", "=", odm]],
                ["id", "code", "name", "nodo", "department_id"], limit=250)
            candidatos, bloqueados_lv = _filtrar_no_linea_viva(candidatos)
            if bloqueados_lv:
                log.info(
                    "[L2] _resolver_pid_nodo odm=%s nodo=%s: %s candidato(s) Linea Viva/LLEE omitido(s)",
                    odm, nodo_str, len(bloqueados_lv)
                )
            if candidatos:
                _equiv = [p for p in candidatos if _nodo_equiv(p.get("nodo", ""), nodo_str)]
                if _equiv:
                    _nidx = _nodo_num(nodo_str)
                    if _nidx != -1:
                        _exact = [p for p in _equiv if _nodo_match(p.get("name", ""), _nidx)]
                        if _exact:
                            return _exact[0]["id"]
                    return _equiv[0]["id"]
                if len(candidatos) == 1 and not str(nodo_str or "").strip():
                    return candidatos[0]["id"]
        except Exception as ex:
            log.warning(f"[L2] _resolver_pid_nodo odm={odm} nodo={nodo_str}: {ex}")

        try:
            nodo_idx   = _nodo_num(nodo_str)
            candidatos = buscar_proyectos_por_df_nombre(
                c, odm, ["id", "code", "name", "nodo", "department_id"], limit=80)
            candidatos, bloqueados_lv = _filtrar_no_linea_viva(candidatos)
            if bloqueados_lv:
                log.info(
                    "[L2] _resolver_pid_nodo fallback odm=%s nodo=%s: %s candidato(s) Linea Viva/LLEE omitido(s)",
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
                        f"[L2] _resolver_pid_nodo: nodo={nodo_str} sin proyecto individual — "
                        f"usando proyecto CABLE {matches[0].get('code')} ({matches[0].get('name')})"
                    )
            if matches:
                if nodo_idx != -1:
                    _exact = [p for p in matches if _nodo_match(p.get("name", ""), nodo_idx)]
                    if _exact:
                        return _exact[0]["id"]
                return matches[0]["id"]
        except Exception as ex:
            log.warning(f"[L2] _resolver_pid_nodo nombre odm={odm} nodo={nodo_str}: {ex}")

    return None


def _rescatar_gemelo_vacio(c, pid, nodo, odm, codes, _cand_cache=None):
    """
    Rescate anti-gemelo-vacío. Caso real (familia 6300996351): SGS tiene DOS
    proyectos para el mismo tramo — el nuevo con campo nodo correcto pero SIN
    materiales (PRO-059787 nodo=26AL31, 0 mats) y el gemelo viejo con el BOM
    real (PRO-059666 name="NODO 26AL31", nodo mal escrito "26AL36"). El match
    por campo nodo elige el vacío y todos los códigos dan falso "no existe".

    Si el proyecto resuelto no contiene NINGUNO de los códigos del Excel para
    este nodo, busca un hermano nodo-equivalente (por campo nodo o por nombre)
    que sí los tenga. Solo remapea si hay UN candidato con cobertura máxima —
    ante ambigüedad conserva el pid original (revisión manual, nunca adivinar).

    Devuelve (pid_final, aviso | None).
    """
    codes = sorted({str(cd).strip() for cd in codes if str(cd).strip()})
    if not pid or not codes or not odm:
        return pid, None
    try:
        existentes = c.search_read(
            "project.material",
            [["project_id", "=", pid], ["product_code", "in", codes]],
            ["id"], limit=1)
        if existentes:
            return pid, None      # al menos un código existe — flujo normal

        if _cand_cache is None:
            _cand_cache = {}
        if odm not in _cand_cache:
            _cands = c.search_read(
                "project.dominion", [["order_number", "=", odm]],
                ["id", "code", "name", "nodo", "department_id"], limit=250)
            _cand_cache[odm], _ = _filtrar_no_linea_viva(_cands)
        candidatos = _cand_cache[odm]
        nodo_idx   = _nodo_num(nodo)
        equivalentes = [
            p for p in candidatos
            if p["id"] != pid and (
                _nodo_equiv(p.get("nodo"), nodo)
                or _cable_nodo_match(p.get("name") or "", nodo)
                or _nodo_match_smart(p.get("name") or "", nodo, nodo_idx)
            )
        ]
        if not equivalentes:
            return pid, None

        mats_alt = c.search_read(
            "project.material",
            [["project_id", "in", [p["id"] for p in equivalentes]],
             ["product_code", "in", codes]],
            ["id", "project_id", "product_code"], limit=500)
        cobertura = {}
        for m in mats_alt:
            ref  = m["project_id"]
            apid = ref[0] if isinstance(ref, list) else ref
            cobertura.setdefault(apid, set()).add(str(m.get("product_code") or "").strip())
        if not cobertura:
            return pid, None

        max_cov = max(len(v) for v in cobertura.values())
        mejores = [apid for apid, v in cobertura.items() if len(v) == max_cov]
        if len(mejores) != 1:
            log.warning(
                "[L2] rescate nodo %s ambiguo: %s hermanos con cobertura %s/%s — no se remapea",
                nodo, len(mejores), max_cov, len(codes))
            return pid, None

        alt  = next(p for p in equivalentes if p["id"] == mejores[0])
        orig = next((p for p in candidatos if p["id"] == pid), {})
        aviso = (
            f"Nodo {nodo}: {orig.get('code') or f'pid {pid}'} coincide por nodo pero no "
            f"tiene ninguno de los códigos del Excel (proyecto gemelo vacío en SGS); "
            f"remapeado a {alt.get('code','?')} ({alt.get('name','')}) que contiene "
            f"{max_cov}/{len(codes)} códigos.")
        log.info("[L2] rescate gemelo-vacío nodo %s: pid %s → %s (%s, %s/%s códigos)",
                 nodo, pid, alt["id"], alt.get("code"), max_cov, len(codes))
        return alt["id"], aviso
    except Exception as ex:
        log.warning(f"[L2] rescate gemelo-vacío nodo {nodo} falló: {ex}")
        return pid, None


_NODO_PLACEHOLDERS = {"", "0", "-", "N/A", "NA", "NONE", "FALSE"}


class PadreIndividualError(ValueError):
    """El 'padre' es un nodo SOLITARIO y su familia no tiene padre real.
    Lleva el row del individual y la clave de familia para que los llamadores
    que lo soportan trabajen en modo hermanos-individuales."""
    def __init__(self, msg: str, individual: dict, fam: str):
        super().__init__(msg)
        self.individual = individual
        self.fam = fam


def _es_proyecto_nodo(p: dict) -> bool:
    """
    True si el proyecto ES un nodo (hijo o individual), no un padre contenedor.

    Fuente de verdad: `contains_subprojects` (check "Contiene sub proyectos" del
    form de SGS; los padres azules lo tienen en True). Medido 2026-09-21 sobre
    657 padres reales: 82 tienen '//' en el nombre o nodo='1' propio
    ('DF202664677127//CD47722', 'NNDD WR-24859 LCL 6301026849') y la heurística
    de nombre los bloqueaba sin razón; y 19 raíces SIN el check (nodo='0',
    nombre = LCL pelado) pasaban como padre válido → nodos fantasma.

    Heurística (nodo propio o '//' en el nombre) solo si el row no trae el campo.
    """
    if "contains_subprojects" in p:
        return not p["contains_subprojects"]
    nodo = str(p.get("nodo") or "").strip().upper()
    if nodo not in _NODO_PLACEHOLDERS:
        return True
    return "//" in str(p.get("name") or "")


def _padre_real_para_nodos(c, p: dict) -> tuple[dict, str]:
    """
    Garantiza que el proyecto usado como 'CODIGO PROYECTO PRINCIPAL' sea un
    padre REAL. El importador de cabecera acepta colgar hijos de un proyecto
    individual/nodo, pero SGS no los muestra en ninguna vista → nodos fantasma.
    Caso real (2026-08-18, familia 6301018664): la cabecera se generó con
    parent=PRO-062447 '6301018664//WR-24532' (individual, nodo='1') y el import
    creó PRO-063129/063130 'WR-24532 NODO 3/2' colgados de él — existen en la
    DB pero son invisibles; hubo que crear el padre PRO-063127 y repetir.

    Recibe el row del proyecto (con id/code/name/nodo/ticket/order_number).
    - Si ya es padre real → (p, "").
    - Si es individual/nodo pero la familia tiene UN padre real → (padre, aviso).
    - Si no hay padre real → ValueError con instrucciones.
    """
    # Los llamadores no siempre piden el check — leerlo aquí para que la
    # decisión nunca dependa de la heurística de nombre.
    if "contains_subprojects" not in p and p.get("id"):
        try:
            chk = c.search_read("project.dominion", [["id", "=", p["id"]]],
                                ["contains_subprojects"], limit=1)
            if chk:
                p = {**p, "contains_subprojects": bool(chk[0].get("contains_subprojects"))}
        except Exception as ex:
            log.warning("[L2 NODOS] no se pudo leer contains_subprojects de %s: %s",
                        p.get("code"), ex)

    if not _es_proyecto_nodo(p):
        return p, ""

    code = p.get("code", "?")
    name = str(p.get("name") or "")

    # Clave de familia: prefijo del nombre antes de '//', ticket u order_number
    # que parezca código real (LCL 63xxxxxxxx o DF...).
    fam = ""
    candidatos_clave = [name.split("//")[0].strip(),
                        str(p.get("ticket") or "").strip(),
                        str(p.get("order_number") or "").strip()]
    for cand in candidatos_clave:
        if re.match(r"^63\d{8}$", cand) or re.match(r"^DF\s?\d{6,}$", cand.upper()):
            fam = cand
            break
    if not fam:
        fam = candidatos_clave[0]

    fam_es_codigo = bool(re.match(r"^63\d{8}$", fam)
                         or re.match(r"^DF\s?\d{6,}$", fam.upper()))

    reales = []
    if fam:
        vistos = set()
        doms = [[["name", "=", fam]], [["ticket", "=", fam]]]
        if fam_es_codigo:
            # Padres reales nombrados '<LCL>//<CD...>' (check marcado, '//' en el nombre)
            doms.append([["name", "=like", fam + "//%"],
                         ["contains_subprojects", "=", True]])
        for dom in doms:
            for q in c.search_read("project.dominion", dom,
                                   ["id", "code", "name", "nodo", "ticket",
                                    "order_number", "contains_subprojects"], limit=20):
                if q["id"] in vistos or q["id"] == p.get("id"):
                    continue
                vistos.add(q["id"])
                if not _es_proyecto_nodo(q):
                    reales.append(q)

    if len(reales) > 1:
        # Preferir match exacto de nombre; si sigue ambiguo, no adivinar.
        exactos = [q for q in reales if str(q.get("name") or "").strip() == fam]
        reales = exactos if len(exactos) == 1 else reales
    if len(reales) == 1:
        real = reales[0]
        aviso = (f"'{code}' ({name}) es un proyecto INDIVIDUAL (es su propio nodo) "
                 f"— se usó el padre real {real.get('code','?')} "
                 f"'{real.get('name','')}' para no crear nodos fantasma.")
        log.warning("[L2 NODOS] %s", aviso)
        return real, aviso

    if len(reales) > 1:
        raise ValueError(
            f"'{code}' ({name}) es un proyecto INDIVIDUAL y la familia "
            f"'{fam}' tiene {len(reales)} padres posibles ("
            + ", ".join(str(q.get("code", "?")) for q in reales)
            + ") — no se adivina. Deja un solo padre con el check "
              "'Contiene sub proyectos' y vuelve a intentar."
        )

    raise PadreIndividualError(
        f"'{code}' ({name}) es un proyecto INDIVIDUAL (sin el check "
        f"'Contiene sub proyectos', nodo={p.get('nodo')!r}), no un padre. "
        f"Colgarle nodos crea hijos FANTASMA: el importador los acepta pero "
        f"SGS no los muestra. Usa 'Crear como individual' (navegador) para "
        f"crear el nodo faltante como hermano solitario, o crea primero el "
        f"padre '{fam or 'de la familia'}' CON el check marcado.",
        individual=p, fam=fam,
    )


def _hermanos_individuales(c, ind: dict, fam: str, fields=None) -> list:
    """
    Familia de SOLITARIOS: proyectos sin padre y sin check que comparten LCL/DF
    con `ind` (incluye a `ind`). Unión de order_number (no placeholder),
    ticket = fam y nombre que empieza por fam. Mismo universo que usa el
    verificador para resolver nodo→PRO (order_number / nombre like DF).
    """
    fields = list(dict.fromkeys((fields or []) + [
        "id", "code", "name", "nodo", "macro_id", "parent_id",
        "contains_subprojects", "create_uid"]))
    doms = []
    on = str(ind.get("order_number") or "").strip()
    if on.upper() not in _NODO_PLACEHOLDERS:
        doms.append([["order_number", "=", on]])
    if fam and (re.match(r"^63\d{8}$", fam) or re.match(r"^DF\s?\d{6,}$", fam.upper())):
        doms.append([["ticket", "=", fam]])
        doms.append([["name", "=like", fam + "%"]])
    vistos, out = set(), []
    for dom in doms:
        for h in c.search_read("project.dominion", dom, fields, limit=200):
            if h["id"] in vistos or h.get("parent_id") or h.get("contains_subprojects"):
                continue
            vistos.add(h["id"])
            out.append(h)
    return out


def _nombre_hermano_individual(plantilla: dict, fam: str, nodo_str: str) -> tuple[str, str]:
    """
    (nombre, valor del campo nodo) del hermano solitario, imitando el estilo del
    nodo plantilla: 'DF202664624578//NODO1' + '2' → 'DF202664624578//NODO2'.
    Si el nombre de la plantilla no termina en su propio nodo
    ('6301018664//WR-24532') → '<fam>//NODO<n>'.
    """
    m_comp = re.match(r"^(\d+)AL(\d+)$", str(nodo_str).upper())
    nodo_campo = f"N{m_comp.group(1)}_N{m_comp.group(2)}" if m_comp else str(nodo_str)

    name   = str(plantilla.get("name") or "").strip()
    nodo_p = str(plantilla.get("nodo") or "").strip()
    m = (re.match(rf"^(.*?)(NODO|ND|N)(\s*){re.escape(nodo_p)}$", name, re.IGNORECASE)
         if nodo_p else None)
    if m:
        pref, palabra, sep = m.group(1), m.group(2), m.group(3)
        return (f"{pref}{nodo_campo}" if m_comp
                else f"{pref}{palabra}{sep}{nodo_campo}"), nodo_campo
    base = fam or name.split("//")[0].strip()
    return (f"{base}//{nodo_campo}" if m_comp else f"{base}//NODO{nodo_campo}"), nodo_campo


def _resolver_padre_sgs(parent_code: str) -> dict:
    """
    Consulta SGS para obtener todos los campos del PRO padre necesarios
    para generar el Excel de importación masiva de nodos.
    Devuelve dict con códigos (para hoja Obras) e IDs (para referencia).
    Si parent_code es un proyecto individual/nodo, redirige al padre real de
    la familia (aviso en 'aviso_padre') o lanza ValueError si no existe.
    """
    c = _sgs()

    def _id(v):
        return v[0] if isinstance(v, list) and v else (v if v not in (False, None, "") else None)

    def _name(v):
        return v[1] if isinstance(v, list) and len(v) > 1 else ""

    campos = ["id", "code", "name", "order_number", "nodo", "ticket",
              "contract_id", "region_id", "micro_id", "macro_id", "technology_id",
              "department_id", "formula_id", "pdii", "process_id", "process_type_id",
              "circuito", "gestor_operativo", "odm", "pdl",
              "approve_id", "bandeja_approver_ids"]
    rows = c.search_read("project.dominion", [["code", "=", parent_code]], campos, limit=1)
    if not rows:
        raise ValueError(f"PRO padre '{parent_code}' no encontrado en SGS")
    p = rows[0]

    # Nunca generar cabecera sobre un proyecto individual/nodo (nodos fantasma).
    aviso_padre = ""
    real, aviso_padre = _padre_real_para_nodos(c, p)
    if real["id"] != p["id"]:
        rows = c.search_read("project.dominion", [["id", "=", real["id"]]], campos, limit=1)
        if not rows:
            raise ValueError(f"PRO padre real '{real.get('code','?')}' no se pudo leer en SGS")
        p = rows[0]

    def code_of(model, rec_id, field="code"):
        if not rec_id:
            return ""
        try:
            r = c.search_read(model, [["id", "=", rec_id]], [field], limit=1)
            return r[0].get(field, "") if r else ""
        except Exception:
            return ""

    contract_id  = _id(p.get("contract_id"))
    region_id    = _id(p.get("region_id"))
    micro_id     = _id(p.get("micro_id"))
    technology_id= _id(p.get("technology_id"))
    dept_id      = _id(p.get("department_id"))
    formula_id   = _id(p.get("formula_id"))

    # Supervisor cedula: approve_id (res.users) → partner_id directo → vat.
    # REGLA (2026-07-28): en el contrato nuevo la cedula del supervisor
    # va SIEMPRE con prefijo 'C' (empleados/partners sin 'C' son del contrato
    # viejo). Antes se buscaba el partner por NOMBRE (ambiguo: hay partners
    # duplicados por persona, uno plano y uno 'C') y se quitaba la 'C' a ciegas
    # con lstrip("C") — por eso a unos PROs les funcionaba y a otros no.
    # Ahora: vat del partner PROPIO del usuario aprobador + 'C' garantizada.
    sup_cedula  = ""
    approve_uid = _id(p.get("approve_id"))
    sup_nombre  = _name(p.get("approve_id"))
    try:
        vat = ""
        if approve_uid:
            u = c.search_read("res.users", [["id", "=", approve_uid]],
                              ["partner_id"], limit=1)
            partner_id = _id(u[0].get("partner_id")) if u else None
            if partner_id:
                pr = c.search_read("res.partner", [["id", "=", partner_id]],
                                   ["vat"], limit=1)
                vat = str(pr[0].get("vat") or "").strip() if pr else ""
        if not vat and sup_nombre:
            # Fallback historico si approve_id no resuelve a un partner con vat
            pr = (c.search_read("res.partner", [["name", "=", sup_nombre]], ["vat"], limit=1)
                  or c.search_read("res.partner", [["name", "ilike", sup_nombre]], ["vat"], limit=1))
            vat = str(pr[0].get("vat") or "").strip() if pr else ""
        nucleo = vat[1:] if vat[:1].upper() == "C" and vat[1:].isdigit() else vat
        if nucleo:
            sup_cedula = f"C{nucleo}"
            # El importador de cabecera valida SUPERVISOR con match EXACTO
            # contra hr.employee.identification_id (leerlo esta restringido
            # para la cuenta de servicio, pero filtrar por el en el domain SI funciona).
            try:
                hit = c.search_read("hr.employee",
                                    [["identification_id", "=", sup_cedula]],
                                    ["id"], limit=1)
            except Exception:
                hit = None
            if hit == []:
                log.warning(
                    "[L2] supervisor '%s': la cedula '%s' NO existe como "
                    "hr.employee — el importador rechazara la fila "
                    "([['SUPERVISOR']]); ese supervisor no parece estar "
                    "registrado en el contrato nuevo",
                    sup_nombre, sup_cedula)
    except Exception:
        pass

    return {
        "parent_id":       p["id"],
        "parent_code":     p["code"],
        "aviso_padre":     aviso_padre,
        "order_number":    p.get("order_number") or "",
        "contract_code":   code_of("customer.contracts",              contract_id),
        "region_code":     code_of("region",                          region_id),
        "micro_code":      code_of("micro.area",                      micro_id),
        "technology_code": code_of("project.technology",              technology_id),
        "department_code": code_of("customer.contracts.department",   dept_id),
        "formula_code":    code_of("activities.formula",              formula_id),
        "supervisor_cedula": sup_cedula,
        "macro_id":        _id(p.get("macro_id")),
        "micro_id":        micro_id,
        "pdii":            _id(p.get("pdii")),
        "process_id":      _id(p.get("process_id")),
        "process_type_id": _id(p.get("process_type_id")),
        "circuito":        p.get("circuito") or "",
        "odm":             p.get("odm") or "-",
        "pdl":             p.get("pdl") or "-",
        "bandeja_approver_ids": p.get("bandeja_approver_ids") or [],
    }


def _nodo_ya_existe(hijos: list, nodo_str: str):
    """
    Devuelve el hijo existente que corresponde a nodo_str ('26AL31', '9', '1AL13')
    usando las MISMAS reglas tolerantes del verificador: campo nodo equivalente
    (_nodo_equiv) o nombre con el rango/nodo (_cable_nodo_match / _nodo_match).
    Caso real: se generó 'N26_N31' y se creó duplicado porque el chequeo por
    nombre exacto no vio que '6300996351 NODO 26AL31' ya existía.
    """
    nodo_txt = str(nodo_str or "").strip()
    if not nodo_txt:
        return None
    es_rango = len(re.findall(r"\d+", nodo_txt)) >= 2
    nidx     = _nodo_num(nodo_txt)
    for h in hijos:
        if _nodo_equiv(h.get("nodo"), nodo_txt):
            return h
        nombre = h.get("name") or ""
        if es_rango and _cable_nodo_match(nombre, nodo_txt):
            return h
        # _nodo_match_smart y no _nodo_match: para nodos alfanuméricos ('38A')
        # compara el valor completo — _nodo_match usa solo la parte numérica y
        # marcaba '38A' como ya-existente apuntando a 'NODO 38' (falso positivo
        # que bloqueaba crear el nodo: "Todos los nodos ya existen en SGS").
        if not es_rango and nidx != -1 and _nodo_match_smart(nombre, nodo_txt, nidx):
            return h
    return None


def _hijos_familia(c, parent_id, order_number="", fields=None, limit=500) -> list:
    """
    Hijos de un PRO padre por parent_id ∪ order_number (algunos nodos se crean
    sueltos sin parent_id pero con el LCL en order_number). Ignora order_number
    placeholder ('0', '-', 'NA'...) — lo comparten cientos de proyectos ajenos.
    """
    fields = fields or ["id", "code", "name", "macro_id", "nodo"]
    dominios = [[["parent_id", "=", parent_id]]]
    on = str(order_number or "").strip()
    if on.upper() not in ("", "0", "-", "N/A", "NA", "NONE", "FALSE"):
        dominios.append([["order_number", "=", on], ["id", "!=", parent_id]])
    vistos, out = set(), []
    for dom in dominios:
        for h in c.search_read("project.dominion", dom, fields, limit=limit):
            if h["id"] in vistos:
                continue
            vistos.add(h["id"])
            out.append(h)
    return out


def _generar_excel_nodos_bytes(parent_code: str, nodos_list: list,
                                fecha_iso: str, ticket: str) -> io.BytesIO:
    """
    Genera el Excel de importación masiva de nodos (hoja Obras).
    nodos_list: lista de strings como "10", "10AL21", "125".
    fecha_iso:  "YYYY-MM-DD"
    Retorna BytesIO listo para enviar como descarga.
    """
    from datetime import date as _date

    d = _resolver_padre_sgs(parent_code)

    try:
        fecha_obj = _date.fromisoformat(fecha_iso)
    except Exception:
        fecha_obj = datetime.now().date()

    OBRAS_COLS = ["CODIGO CONTRATO", "CODIGO PROYECTO PRINCIPAL", "NOMBRE PROYECTO",
                  "CODIGO REGION", "CODIGO MICRO AREA", "CODIGO TECNOLOGIA",
                  "CODIGO DEPARTAMENTO", "F. ASIGNACION", "F. INICIO PREVISTO",
                  "F. INICIO REAL", "F. FIN PREVISTO", "F. FIN REAL",
                  "TIPO EQUIPO", "NRO. DOCUMENTO EQUIPO", "SUPERVISOR", "CODIGO FORMULA"]

    # Nodos que ya existen como hijos (R5: no recrear) — nombre exacto Y
    # matching tolerante por nodo: 'N26_N31' ≡ 'NODO 26AL31' ≡ 'NODO 26 AL 31'.
    # Excluir Línea Viva/LLEE con el MISMO filtro del verificador: si el único
    # "existente" para un nodo es un proyecto LV (ej. 'LCL_..._SIMIJACA_30'),
    # el verificador lo declara faltante pero el dedupe aquí lo omitía por
    # "ya existe" → bucle infinito sin poder crear el nodo normal.
    try:
        existentes = _hijos_familia(_sgs(), d["parent_id"], d.get("order_number", ""),
                                    fields=["id", "code", "name", "nodo",
                                            "department_id", "contract_id"])
        existentes, _lv = _filtrar_no_linea_viva(existentes)
        if _lv:
            log.info("generar_excel_nodos: %d hijo(s) Linea Viva excluidos del dedupe: %s",
                     len(_lv), [h.get("code") for h in _lv])
        exist_set = {(h.get("name") or "").strip() for h in existentes}
    except Exception:
        existentes, exist_set = [], set()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Obras"
    for j, col in enumerate(OBRAS_COLS, 1):
        ws.cell(1, j, col)
    # Hoja Tabla (validación requerida por el importador)
    wt = wb.create_sheet("Tabla")
    wt.cell(1, 1, "TIPO"); wt.cell(2, 1, "C"); wt.cell(3, 1, "E")

    # Nombre del hijo = LCL del padre + nodo (nunca el PDL del Excel)
    prefijo = _lcl_para_nombre(d.get("order_number"), ticket)

    omitidos = []
    fila = 2
    for nodo_str in nodos_list:
        m_comp = re.match(r"^(\d+)AL(\d+)$", str(nodo_str).upper())
        if m_comp:
            sufijo = f"N{m_comp.group(1)}_N{m_comp.group(2)}"
        else:
            sufijo = f"NODO {nodo_str}"
        nombre = f"{prefijo} {sufijo}" if prefijo else sufijo

        h_exist = _nodo_ya_existe(existentes, nodo_str)
        if h_exist is not None:
            omitidos.append(f"{nombre} (ya existe: {h_exist.get('code','?')} "
                            f"'{h_exist.get('name','')}')")
            continue
        if nombre.strip() in exist_set:
            omitidos.append(nombre)
            continue

        valores = [
            # d["parent_code"] (no el argumento): si el argumento era un proyecto
            # individual, _resolver_padre_sgs ya redirigió al padre real — usar
            # el argumento colgaría los nodos del individual (nodos fantasma).
            d["contract_code"], d["parent_code"], nombre,
            d["region_code"], d["micro_code"], d["technology_code"],
            d["department_code"],
            fecha_obj, fecha_obj, fecha_obj, None, None,
            "C", None, d["supervisor_cedula"], d["formula_code"],
        ]
        for j, v in enumerate(valores, 1):
            cell = ws.cell(fila, j, v)
            if j in (8, 9, 10) and hasattr(v, "year"):
                cell.number_format = "DD/MM/YYYY"
        fila += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf, omitidos, d


def _todos_lideres_session(s):
    """Devuelve dict nombre→cedula de todos los líderes conocidos en la sesión."""
    result = {}
    for ck in (s.get("checks_mo") or []):
        for l in (ck.get("lideres") or []):
            if l.get("nombre"):
                result[l["nombre"]] = l.get("cedula", "")
    for d in (s.get("distribucion_excel") or []):
        if d.get("nombre"):
            result[d["nombre"]] = d.get("cedula", "")
    return result


def _fuzzy_match_lider(nombre_ia, lideres_dict):
    """Busca la mejor coincidencia por cobertura de palabras. Retorna (nombre, cedula, score) o None.

    Solo usa overlap de palabras (no SequenceMatcher de caracteres): una palabra en
    común no es suficiente para un nombre de 3 palabras.
    Umbral: ≥ 50 % de las palabras del nombre IA deben aparecer en el candidato,
    y el score final ≥ 0.6.
    """
    if not nombre_ia or not lideres_dict:
        return None
    nombre_up = nombre_ia.upper().strip()
    # Ignorar palabras cortas (preposiciones, artículos)
    words_ia = [w for w in nombre_up.split() if len(w) >= 3]
    if not words_ia:
        return None
    words_ia_set = set(words_ia)
    best_name, best_score, best_key = None, 0.0, None
    for nombre in lideres_dict:
        words_sgn = set(w for w in nombre.upper().split() if len(w) >= 3)
        comunes   = words_ia_set & words_sgn
        if not comunes:
            continue  # ni una palabra en común → descartar
        coverage = len(comunes) / max(len(words_ia_set), 1)
        # Para nombres de 2+ palabras, exigir al menos el 50 % de cobertura
        if len(words_ia_set) >= 2 and coverage < 0.5:
            continue
        # A igual cobertura, preferir el candidato con cédula (y con C): la sesión
        # puede traer un «NOMBRE APELLIDO» sin cédula junto al partner real.
        ced = str(lideres_dict.get(nombre) or "").strip()
        key = (coverage, bool(ced), ced.upper().startswith("C"))
        if best_key is None or key > best_key:
            best_key   = key
            best_score = coverage
            best_name  = nombre
    if best_name and best_score >= 0.6:
        return (best_name, lideres_dict[best_name], best_score)
    return None


def _buscar_lider_en_sgs(nombre_ia, c):
    """Fallback: busca un líder en res.partner de SGS cuando no está en la sesión.
    Retorna (nombre_real_sgs, cedula_con_C, score) o None.

    Estrategia unión: busca cada palabra con limit=30, une todos los candidatos y
    aplica cobertura de palabras en Python (evita falsos positivos por apellidos comunes
    sin depender de que el partner aparezca en TODOS los resultados paginados).

    Umbral de cobertura: para N>=2 palabras, exige que al menos ceil(N*0.6) palabras
    del nombre IA aparezcan en el nombre candidato.
    """
    if not nombre_ia:
        return None
    palabras = [w for w in nombre_ia.upper().strip().split() if len(w) >= 3]
    if not palabras:
        return None

    words_ia  = set(palabras)

    # Unión de candidatos de todas las búsquedas por palabra (limit alto para apellidos comunes)
    all_partners = {}   # id → {"name": str, "vat": str}
    for palabra in palabras:
        try:
            results = c.search_read(
                "res.partner",
                [["name", "ilike", palabra], ["active", "=", True]],
                ["id", "name", "vat"], limit=30)
            for p in results:
                pid = p.get("id")
                if pid and pid not in all_partners:
                    all_partners[pid] = {"name": p.get("name", ""), "vat": p.get("vat", "")}
        except Exception as ex:
            log.debug("_buscar_lider_en_sgs '%s' palabra '%s': %s", nombre_ia, palabra, ex)

    if not all_partners:
        return None

    # Cobertura mínima: para 2 palabras exige ambas (>0.5); para 3+ exige 2 de N (>= 0.6)
    min_coverage = 0.6 if len(words_ia) >= 2 else 0.0

    # Desempate (2026-09-25): en SGS conviven partners sin vat (solo nombre y
    # apellido), el del contrato viejo (vat plano) y el correcto (nombre
    # completo, vat C...). Los tres cubren el 100 % de las palabras; a igual
    # cobertura manda el que tenga cédula y, entre ellos, el que la tenga con C.
    best = None
    best_key = None
    for pid, pdata in all_partners.items():
        pname_up  = (pdata.get("name") or "").upper().strip()
        words_sgn = set(w for w in pname_up.split() if len(w) >= 3)
        comunes   = words_ia & words_sgn
        if not comunes:
            continue
        coverage = len(comunes) / max(len(words_ia), 1)
        if coverage < min_coverage:
            continue
        ced_raw = (pdata.get("vat") or "").strip() if pdata.get("vat") else ""
        key = (coverage, bool(ced_raw), ced_raw.upper().startswith("C"))
        if best_key is None or key > best_key:
            best_key = key
            pname = pdata["name"].strip()
            ced   = ced_raw if ced_raw.upper().startswith("C") else ("C" + ced_raw if ced_raw else "")
            best  = (pname, ced, coverage)
    best_score = best_key[0] if best_key else 0.0

    if best and best_score >= 0.5:
        return best
    return None


_LIDER_SEP_RE = re.compile(r"\s*[-–—/;,+]\s*|\s+Y\s+", re.IGNORECASE)


def _parsear_tabla_prestaciones(texto: str):
    """
    Parser determinístico para planillas de asignación por prestación:
    NODO / CODIGO QP / CANT / OBSERVACION (líder o líderes separados por - / y ,).
    Retorna [{"nodo","codigo","cant","lideres":[...]}] o None si el texto no
    parece esa tabla (menos de 3 filas válidas o <60% de las líneas candidatas).
    Este formato no necesita IA — parsearlo directo es más rápido y sin errores.
    """
    filas, candidatas = [], 0
    for linea in (texto or "").splitlines():
        ln = linea.strip()
        if not ln:
            continue
        # Saltar filas de cabecera (NODO  CODIGO E4E  CANT  OBSERVACION)
        if re.match(r"^(NODO|NRO|#|FILA)\b", ln, re.IGNORECASE) and "QP" not in ln.upper():
            continue
        candidatas += 1
        partes = [p.strip() for p in ln.split("\t") if p.strip()]
        if len(partes) >= 4 and re.match(r"^QP", partes[1], re.IGNORECASE):
            nodo, codigo, cant_raw = partes[0], partes[1], partes[2]
            obs = " ".join(partes[3:])
        else:
            m = re.match(r"^(\d[\w\-]*)\s+(QP[\w\d]+)\s+([\d.,]+)\s+(.+)$", ln, re.IGNORECASE)
            if not m:
                continue
            nodo, codigo, cant_raw, obs = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            cant = float(str(cant_raw).replace(",", "."))
        except ValueError:
            continue
        lideres = [l.strip().upper() for l in _LIDER_SEP_RE.split(obs)
                   if l and len(l.strip()) >= 3]
        if not lideres:
            continue
        filas.append({"nodo": str(nodo).strip().upper(),
                      "codigo": codigo.strip().upper(),
                      "cant": cant, "lideres": lideres})
    if len(filas) >= 3 and candidatas and len(filas) >= 0.6 * candidatas:
        return filas
    return None


def _resolver_lideres_ia(nombres, lideres_dict, sgs_fallback):
    """
    Resuelve una lista de nombres interpretados (IA o tabla) contra la sesión y SGS.
    Retorna ([{lider_ia, nombre, cedula, score}], extra_cedulas).
    """
    resueltos, extra = [], {}
    for lider_ia in nombres:
        match = _fuzzy_match_lider(lider_ia, lideres_dict)
        if match and not match[1]:
            m2 = sgs_fallback(lider_ia)
            if m2 and m2[1]:
                match = m2
        if not match:
            match = sgs_fallback(lider_ia)
            if match and match[0] not in lideres_dict:
                extra[match[0]] = match[1]
        resueltos.append({
            "lider_ia": lider_ia,
            "nombre":   match[0] if match else "",
            "cedula":   match[1] if match else "",
            "score":    round((match[2] if match else 0) * 100),
        })
    return resueltos, extra


def _llamar_minimax(texto):
    """Llama a MiniMax chat API para extraer asignaciones nodo→líder del texto."""
    prompt = (
        "Eres un asistente de Dominion Colombia que ayuda a asignar líderes de cuadrilla "
        "en proyectos de telecomunicaciones.\n"
        "Del siguiente texto, determina cómo se deben asignar los líderes:\n\n"
        "• Si el texto indica qué líder va a qué NODO específico "
        "(ej: 'nodo 54 con Juan', 'el 55 le toca a María'), responde:\n"
        '  {"mode":"nodo","asignaciones":[{"nodos":["54","55"],"lider":"NOMBRE"}]}\n\n'
        "• Si el texto especifica por PRESTACIÓN (código QP) de un nodo quién o quiénes "
        "liquidan (tablas NODO/CODIGO/CANT/OBSERVACION, o frases como 'en el nodo 309 "
        "la QP1180 la liquidan Vicente y Willinton'), responde:\n"
        '  {"mode":"prestacion","asignaciones":[{"nodo":"309","codigo":"QP1180","lideres":["VICENTE FUQUENE","WILLINTON CARDENAS"]}]}\n'
        "  - 'lideres' lleva TODOS los nombres mencionados para esa fila "
        "(pueden venir separados por -, /, y, comas).\n"
        "  - Una entrada por cada combinación nodo+código mencionada.\n\n"
        "• En CUALQUIER OTRO CASO (nombrar líderes sin nodo específico, pedir reparto, "
        "división, distribución, o solo listar nombres), usa modo equitativo.\n"
        "  - Si el texto menciona porcentajes o proporciones exactas, extráelos (deben sumar 100).\n"
        "  - Si NO hay porcentajes, omite 'pct' para que el sistema reparta en partes iguales.\n"
        "  Ejemplos:\n"
        '  Con pct:  {"mode":"equitativo","lideres":[{"nombre":"Juan","pct":50},{"nombre":"María","pct":30},{"nombre":"Luis","pct":20}]}\n'
        '  Sin pct:  {"mode":"equitativo","lideres":[{"nombre":"Juan"},{"nombre":"María"},{"nombre":"Luis"}]}\n\n'
        "Responde SOLO con JSON válido, sin texto adicional, sin markdown, sin ```json.\n\n"
        "Texto:\n" + texto
    )
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "MiniMax-Text-01",
        "messages": [{"role": "user", "content": prompt}],
        # Tablas por prestación pueden tener 70+ filas → el JSON de salida es grande.
        "max_tokens": 4000,
        "temperature": 0.1,
    }

    # Intentar con el endpoint internacional primero, luego el de China como fallback
    for endpoint in [
        "https://api.minimax.io/v1/text/chatcompletion_v2",
        "https://api.minimax.chat/v1/text/chatcompletion_v2",
    ]:
        try:
            resp = requests.post(endpoint, headers=headers, json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            log.debug("MiniMax response keys: %s", list(data.keys()))

            # Formato OpenAI-compatible (choices[0].message.content)
            if data.get("choices"):
                choice  = data["choices"][0]
                message = choice.get("message") or {}
                content = (message.get("content") or
                           message.get("text") or
                           (choice.get("messages") or [{}])[0].get("text") or
                           (choice.get("messages") or [{}])[0].get("content") or "")
                if content:
                    break
            # Formato MiniMax nativo (reply directo)
            if data.get("reply"):
                content = data["reply"]
                break
            # Error del servidor
            base = data.get("base_resp") or {}
            raise RuntimeError(
                f"MiniMax sin contenido. Status: {base.get('status_code')} — {base.get('status_msg')} — raw: {str(data)[:300]}"
            )
        except requests.HTTPError as e:
            log.warning("MiniMax endpoint %s falló: %s", endpoint, e)
            if endpoint == "https://api.minimax.chat/v1/text/chatcompletion_v2":
                raise
            content = None
    else:
        raise RuntimeError("Todos los endpoints de MiniMax fallaron.")

    content = (content or "").strip()
    # Quitar bloques markdown si los hay
    if "```" in content:
        parts = content.split("```")
        content = parts[1] if len(parts) > 1 else content
        if content.lower().startswith("json"):
            content = content[4:]
    return json.loads(content.strip())


def _minimax_veredicto_bloqueo_mat(mat_no_enc: list, odm: str = "") -> dict | None:
    """
    Pide a MiniMax un diagnostico en lenguaje claro de por que estos materiales
    quedaron 'no_encontrado' con cantidad real — para mostrarlo en un popup que
    el liquidador NO pueda confundir con 'ya liquidado'.

    IMPORTANTE: esto es solo texto explicativo. La decision de bloquear la
    liquidacion es 100% deterministica (bloqueo_mat > 0) y no depende de lo
    que responda el modelo — nunca se le pregunta si "se puede ignorar".
    Si la llamada falla, el caller debe usar un texto estatico de respaldo.
    """
    items_txt = []
    for ck in mat_no_enc[:30]:
        mat = ck.get("mat", {}) or {}
        errs = "; ".join(ck.get("errores") or []) or ck.get("estado", "no_encontrado")
        pro  = ck.get("pro_code", "")
        linea = (f"- Codigo={mat.get('codigo','?')} Nodo={mat.get('nodo','?')} "
                 f"SGS_neto_excel={mat.get('sgs_neto_excel', 0)} Error={errs}")
        if pro:
            linea += f" ProyectoEncontrado={pro}"
        items_txt.append(linea)
    prompt = (
        "Eres un auditor que revisa liquidaciones de materiales en Odoo para Dominion "
        "Colombia (proyecto SGS, zona Ubaté). El liquidador procesa planillas grandes "
        "y a veces NO revisa cada fila a fondo. Tu trabajo es escribirle una explicacion "
        "corta y muy clara de por que estos materiales especificos NO pudieron liquidarse, "
        "para que NO los confunda con materiales 'ya liquidados' (que es un estado normal "
        "y seguro de ignorar).\n\n"
        "Hay DOS tipos de fallo posibles (puede haber uno o ambos en la misma planilla):\n"
        "TIPO A — sin ProyectoEncontrado: el sistema no pudo resolver el proyecto SGS para "
        "el nodo indicado. Causa comun: el nodo del Excel es individual (ej. '18') pero en "
        "SGS solo existe un proyecto multi-nodo/cable (ej. 'N17_N18', 'NODO 17 AL NODO 18'), "
        "o el nodo esta mal escrito en el Excel.\n"
        "TIPO B — con ProyectoEncontrado: el proyecto SGS si fue encontrado, pero el codigo "
        "SAP no existe en la lista de materiales (project.material) de ese proyecto. Causa "
        "comun: el material pertenece a un sub-proyecto diferente, no fue cargado en el BOM, "
        "o hay un error en el codigo SAP.\n\n"
        f"Orden de trabajo: {odm or '?'}\n"
        "Materiales afectados:\n" + "\n".join(items_txt) + "\n\n"
        "Responde SOLO JSON valido, sin markdown, con este formato exacto:\n"
        '{"diagnostico": "explicacion breve (max 70 palabras) indicando el tipo de fallo '
        'detectado y la causa mas probable, en espanol, dirigida al liquidador", '
        '"accion_requerida": "que debe hacer el liquidador antes de continuar (max 35 palabras)"}'
    )
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "MiniMax-Text-01",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 400,
        "temperature": 0.1,
    }
    for endpoint in [
        "https://api.minimax.io/v1/text/chatcompletion_v2",
        "https://api.minimax.chat/v1/text/chatcompletion_v2",
    ]:
        try:
            resp = requests.post(endpoint, headers=headers, json=body, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            content = None
            if data.get("choices"):
                message = data["choices"][0].get("message") or {}
                content = message.get("content") or message.get("text")
            if not content:
                continue
            content = content.strip()
            if "```" in content:
                parts = content.split("```")
                content = parts[1] if len(parts) > 1 else content
                if content.lower().startswith("json"):
                    content = content[4:]
            veredicto = json.loads(content.strip())
            if isinstance(veredicto, dict) and veredicto.get("diagnostico"):
                return veredicto
        except Exception as ex:
            log.warning("MiniMax veredicto bloqueo_mat endpoint %s falló: %s", endpoint, ex)
    return None


def _items_mo_desde_form(checks_mo, idx_mo, form, sgs_client=None,
                         omitir_ya_liquidadas: bool = False):
    """
    Construye la lista de items MO a liquidar desde los índices y el formulario.
    Sistema nuevo: lee mo_lider_idx_{idx} por fila (asignación greedy o manual desde JS).
    Fallback: primer líder conocido o partner_nombre del check.
    El líder elegido NO está restringido a project.team — puede ser cualquier partner
    válido en Odoo. Playwright lo busca igual si no está en el nodo.
    Si la cédula no está en el formulario ni en el mapa global, se busca en SGS
    por nombre completo (sgs_client requerido para ese fallback).
    omitir_ya_liquidadas=True: incluye filas marcadas ya_liquidada (re-liquidación
    autorizada por el usuario para trabajo nuevo sobre el mismo nodo).
    """
    items_mo, errores = [], []

    def _ced_norm(ced: str) -> str:
        ced = (ced or "").strip()
        if ced.isdigit():
            return "C" + ced
        if ced.upper().startswith("C") and ced[1:].isdigit():
            return ced.upper()
        return ced

    # Mapa global de cédulas de todos los líderes conocidos en esta sesión
    cedula_global = {}
    for ck2 in checks_mo:
        for l in (ck2.get("lideres") or []):
            if l.get("nombre") and l.get("cedula"):
                cedula_global[l["nombre"]] = _ced_norm(l["cedula"])

    for idx in idx_mo:
        if idx >= len(checks_mo):
            continue
        ck = checks_mo[idx]
        # Filas "lider_req": proyecto encontrado pero sin equipo en SGS; el usuario
        # asignó líder manualmente en el select — procesarlas como liquidables.
        lider_req = (not ck.get("ok")) and ck.get("lider_faltante") and ck.get("pid")
        _es_ya_liq      = ck.get("ya_liquidada", False)
        # Solo se puede omitir el bloqueo si fue liquidada en un mes ANTERIOR.
        # Las liquidadas este mes siempre se bloquean independientemente del flag.
        _es_repetible   = _es_ya_liq and ck.get("ya_liquidada_mes_anterior", False)
        _bloqueada_firm = _es_ya_liq and not _es_repetible
        # "Repetible sumable": cant_no_coincide con delta pendiente.
        # Procesable solo en Modo Repetición; usa delta_qty, no cantidad del Excel.
        _es_rep_sumable = ck.get("repetible_sumable", False)
        _procesar = (ck.get("ok") or lider_req
                     or (_es_repetible and omitir_ya_liquidadas)
                     or (_es_rep_sumable and omitir_ya_liquidadas))
        if not _procesar or _bloqueada_firm:
            continue
        lideres        = ck.get("lideres") or []
        if _es_rep_sumable:
            cantidad_total = float(ck.get("delta_qty", 0))
            if cantidad_total <= 0.001:
                errores.append(f"{ck.get('row', {}).get('codigo', idx)}: delta=0 en repetible_sumable.")
                continue
        else:
            cantidad_total = float(ck.get("row", {}).get("cantidad", 1))
        base = {
            "tipo": "MO", "idx": idx, "pid": ck.get("pid"),
            "act_id": ck.get("act_id"), "act_code": ck.get("row", {}).get("codigo", ""),
            "pro_code": ck.get("pro_code", ""),
        }

        # Sistema nuevo: líder asignado por fila (greedy JS o selección manual)
        partner = (form.get(f"mo_lider_idx_{idx}") or "").strip()

        # Fallback al sistema anterior por compatibilidad
        if not partner:
            if len(lideres) <= 1:
                partner = lideres[0]["nombre"] if lideres else ck.get("partner_nombre", "")
            else:
                grupo_key = str(ck.get("pid") or ck.get("pro_code") or idx)
                if (form.get(f"mo_group_mode_{grupo_key}") or "").strip() == "uno":
                    partner = (form.get(f"mo_group_a_{grupo_key}") or "").strip()
                if not partner:
                    partner = ck.get("partner_nombre") or (lideres[0]["nombre"] if lideres else "")

        if not partner:
            _r = ck.get("row") or {}
            errores.append(
                f"Fila {_r.get('fila', idx)} · nodo {_r.get('pro') or _r.get('_odm') or '?'} · "
                f"{base['act_code']}: sin líder asignado"
                + (f" ({base['pro_code']} no tiene equipo en SGS)" if ck.get("lider_faltante") else "")
                + ". Elígelo en «Líder asignado» y vuelve a liquidar — no se escribió nada del lote.")
            continue

        # Cédula: 1) hidden dedicado, 2) mapa global de sesión, 3) SGS por nombre
        cedula_form = (form.get(f"mo_lider_ced_{idx}") or "").strip()
        import re as _re
        if not _re.match(r'^C\d+$', cedula_form):
            cedula_form = cedula_global.get(partner, "")
        if not _re.match(r'^C\d+$', cedula_form) and sgs_client:
            try:
                hit = _buscar_lider_en_sgs(partner, sgs_client)
                if hit and hit[1]:
                    cedula_form = hit[1]   # ya viene con prefijo C
                    log.info("Cédula de '%s' resuelta desde SGS: %s", partner, cedula_form)
            except Exception as _ex:
                log.warning("_buscar_lider_en_sgs('%s'): %s", partner, _ex)
        items_mo.append({
            **base,
            "cantidad": cantidad_total,
            "partner_nombre": partner,
            "partner_cedula": cedula_form,
            "cantidad_proyectada": cantidad_total,
            # Modo Repetición: el pre-check "ya liquidada" del browser debe
            # reportar error claro (sin_pendiente) en vez de OK falso.
            "modo_repeticion": bool(_es_repetible or _es_rep_sumable),
        })

    return items_mo, errores


_NODO_PLANILLA_RE = re.compile(
    r"^\d{1,3}[A-Z]?$|^\d{1,3}[A-Z]?\s*(?:AL|_|-)\s*N?\d{1,3}[A-Z]?$",
    re.IGNORECASE)


def _es_nodo_planilla(val) -> bool:
    """True si el valor de la columna nodo de MO parece un NODO real
    ('54', '10A', '54AL66', '1_5') y no un PRO/DF/LCL. Solo con nodos
    reconocibles en TODAS las filas MO se puede inferir el alcance de la
    planilla para filtrar los MAT."""
    return bool(_NODO_PLANILLA_RE.match(str(val or "").strip()))


def _rango_nodo_simple(nodo_str):
    """(min, max) de los números del nodo: '54AL66'→(54,66), '10A'→(10,10)."""
    nums = [int(x) for x in re.findall(r"\d+", str(nodo_str or ""))]
    return (min(nums), max(nums)) if nums else None


def _nodo_en_alcance_mo(mat_nodo, mo_rangos) -> bool:
    """True si el nodo MAT se solapa con algún nodo de la planilla MO:
    equivalencia exacta (_nodo_equiv) o solape de rangos numéricos —
    permisivo: compartir un extremo basta ('38AL54' entra en '54AL66')."""
    r1 = _rango_nodo_simple(mat_nodo)
    for mo_n, r2 in mo_rangos:
        if _nodo_equiv(mat_nodo, mo_n):
            return True
        if r1 and r2 and r1[0] <= r2[1] and r2[0] <= r1[1]:
            return True
    return False


def _precalentar_pids(pids: list) -> None:
    """
    Navega al form de cada proyecto en un browser headless para forzar la
    recomputación de campos stored en Odoo (balance, despachado, devuelto)
    antes de que el verificador lea via API JSON-RPC.

    Mecanismo: Odoo, al renderizar el form de project.dominion, hace read()
    sobre todos los registros relacionados (project.material). Ese read() pasa
    por el ORM de Odoo, que procesa el queue de recompute de campos stored.
    Así la siguiente lectura API obtiene valores frescos en vez de stale.

    Reusa el storage_state cacheado — ~5-7s por PID, sin re-login. Con varios
    pids reparte la cola entre hasta 3 browsers en paralelo (caso real
    6300996351: 78 pids secuenciales = 520s; el warm-up era el 80% de la
    espera del verificar).
    No-op si no hay storage_state disponible (antes de la primera sesión).
    """
    if not pids:
        return
    storage_state = _obtener_storage_state_activo(timeout_s=3.0)
    if not storage_state:
        log.info("[L2 PRECALENTAR] Sin storage_state — omitiendo warm-up Odoo")
        return
    t0 = _time_mod.time()
    cola   = list(pids)
    q_lock = threading.Lock()
    n_workers = min(3, len(cola))

    def _worker(wid):
        # Cada hilo crea SU PROPIO PlaywrightClient: la API sync de Playwright
        # solo puede usarse desde el hilo que la creó.
        pw = None
        try:
            pw = PlaywrightClient(headless=True, slow_mo=0)
            pw.start(storage_state=storage_state)
            while True:
                with q_lock:
                    if not cola:
                        break
                    pid = cola.pop(0)
                try:
                    pw.precalentar_proyecto(pid)
                    log.info(f"[L2 PRECALENTAR] pid={pid} ok ({_time_mod.time()-t0:.1f}s)")
                except Exception as ex:
                    log.warning(f"[L2 PRECALENTAR] pid={pid} error: {ex}")
        except Exception as ex:
            log.warning(f"[L2 PRECALENTAR] worker {wid} no pudo iniciar browser: {ex}")
        finally:
            if pw:
                try:
                    pw.close()
                except Exception:
                    pass

    if n_workers <= 1:
        _worker(0)
    else:
        hilos = [threading.Thread(target=_worker, args=(i,), daemon=True,
                                  name=f"l2-precal-{i}")
                 for i in range(n_workers)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
    log.info(f"[L2 PRECALENTAR] {len(pids)} pids completados en "
             f"{_time_mod.time()-t0:.1f}s ({n_workers} worker(s))")


def _obtener_storage_state_activo(timeout_s: float = 4.0):
    _u0 = _usuario_actual()
    _e0 = _pw_users.get(_u0) if _u0 else None
    if _e0 and _e0.get("logueado") and _e0.get("state"):   # SGO7: sesión propia del usuario
        _e0["ultimo_uso"] = _time_mod.time()
        return _e0["state"]
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


def _pids_liquidados_desde_sesion(sid):
    s   = _sesiones.get(sid) or {}
    job = _trabajos.get(sid) or {}
    resultados  = s.get("resultados_liquidacion") or []
    checks_mo   = s.get("checks_mo",  []) or []
    checks_mat  = s.get("checks_mat", []) or []
    pids_ok     = {}

    def _resolver_pid(tipo, codigo, pro_code):
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

    def _reg(pid, pro_code):
        if pid:
            pids_ok[pid] = pro_code or pids_ok.get(pid, "")

    for r in resultados:
        if not r.get("ok"):
            continue
        pid = r.get("pid") or _resolver_pid(r.get("tipo"), r.get("codigo", ""), r.get("pro_code", ""))
        _reg(pid, r.get("pro_code", ""))

    for it in job.get("items_mo", []) + job.get("items_mat", []):
        if it.get("estado") != "ok":
            continue
        pid = it.get("pid") or _resolver_pid(it.get("tipo"), it.get("codigo", ""), it.get("pro_code", ""))
        _reg(pid, it.get("pro_code", ""))

    return pids_ok


def _proyectos_identificados_desde_sesion(sid):
    s         = _sesiones.get(sid) or {}
    pids_ok   = _pids_liquidados_desde_sesion(sid)
    checks_mo = s.get("checks_mo",  []) or []
    checks_mat= s.get("checks_mat", []) or []
    proyectos = {}

    def _reg(pid, pro_code="", nombre="", odm="", nodo="", fuente=""):
        if not pid:
            return
        info = proyectos.setdefault(pid, {
            "pid": pid, "pro_code": "", "nombre_local": "",
            "odm": "", "nodos": [], "fuentes": [], "liquidado_ok": False})
        if pro_code and not info["pro_code"]:     info["pro_code"]      = pro_code
        if nombre   and not info["nombre_local"]: info["nombre_local"]  = nombre
        if odm      and not info["odm"]:          info["odm"]           = odm
        nodo_txt = str(nodo or "").strip()
        if nodo_txt and nodo_txt not in info["nodos"]:   info["nodos"].append(nodo_txt)
        fuente_txt = str(fuente or "").strip()
        if fuente_txt and fuente_txt not in info["fuentes"]: info["fuentes"].append(fuente_txt)
        if pid in pids_ok:
            info["liquidado_ok"] = True
        if pids_ok.get(pid) and not info["pro_code"]:
            info["pro_code"] = pids_ok[pid]

    for ck in checks_mo:
        row = ck.get("row", {}) or {}
        _reg(ck.get("pid"), pro_code=ck.get("pro_code",""), nombre=ck.get("pnombre",""),
             odm=row.get("_odm",""), nodo=row.get("pro",""), fuente="MO")
    for ck in checks_mat:
        mat = ck.get("mat", {}) or {}
        _reg(ck.get("pid"), pro_code=ck.get("pro_code",""), nombre=ck.get("pnombre",""),
             odm=mat.get("_odm",""), nodo=mat.get("nodo",""), fuente="MAT")
    for pid, pro_code in pids_ok.items():
        _reg(pid, pro_code=pro_code, fuente="OK")

    return proyectos


def _resolver_objetivos_cierre(sid, fecha_raw="", pdl_raw="", modo_raw=""):
    s         = _sesiones.get(sid) or {}
    fecha_iso = _fecha_iso(str(fecha_raw or "").strip()) or datetime.now().strftime("%Y-%m-%d")
    fecha_dt  = datetime.strptime(fecha_iso, "%Y-%m-%d")
    pdl       = str(pdl_raw or s.get("pdl_detectado", "") or "").strip()
    modo      = str(modo_raw or "").strip()
    if modo not in CIERRE_MODOS:
        modo = _cierre_modo(s)
    return {"fecha_iso": fecha_iso, "fecha_ui": fecha_dt.strftime("%d-%m-%Y"),
            "fecha_input": fecha_dt.strftime("%Y-%m-%d"),
            "pdl_esperado": pdl, "estado_esperado": CIERRE_RAPIDO_ESTADO,
            "modo": modo, "solo_estado": modo == CIERRE_MODO_SOLO_ESTADO}


def _armar_panel_cierre_rapido(sid, fecha_raw="", pdl_raw="", modo_raw=""):
    objetivos = _resolver_objetivos_cierre(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw,
                                           modo_raw=modo_raw)
    proyectos = _proyectos_identificados_desde_sesion(sid)
    rows      = []
    meta      = {**objetivos, "total": 0, "elegibles": 0,
                 "pendientes": 0, "listos": 0, "referencia": 0}

    if not proyectos:
        return rows, meta

    fields = (["id","code","name","order_number","nodo","customer_id",
               "contract_id","department_id","status","pdl"]
              + [campo for campo, _ in CIERRE_RAPIDO_FECHAS])
    registros = _sgs().search_read(
        "project.dominion", [["id","in",list(proyectos.keys())]], fields,
        limit=len(proyectos)+10)
    por_id = {r["id"]: r for r in registros}

    for pid, base in proyectos.items():
        reg          = por_id.get(pid, {}) or {}
        pro_code     = str(reg.get("code")         or base.get("pro_code")     or "").strip()
        nombre       = str(reg.get("name")          or base.get("nombre_local") or "").strip()
        order_number = str(reg.get("order_number")  or base.get("odm")         or "").strip()
        nodo_sgs     = str(reg.get("nodo") or "").strip()
        nodos        = list(base.get("nodos") or [])
        if nodo_sgs and nodo_sgs not in nodos:
            nodos.insert(0, nodo_sgs)

        customer     = _texto_m2o(reg.get("customer_id"))   or "-"
        contract     = _texto_m2o(reg.get("contract_id"))   or "-"
        department   = _texto_m2o(reg.get("department_id")) or "-"
        estado_actual   = _texto_m2o(reg.get("status"))     or "-"
        status_ok       = estado_actual == objetivos["estado_esperado"]
        pdl_actual   = str(reg.get("pdl") or "").strip() or "-"
        pdl_ok       = _pdl_match(pdl_actual, objetivos["pdl_esperado"])

        fechas_dif, fechas_ok_count = [], 0
        for campo, label in CIERRE_RAPIDO_FECHAS:
            actual_iso = _fecha_iso(reg.get(campo))
            if actual_iso == objetivos["fecha_iso"]:
                fechas_ok_count += 1
            else:
                fechas_dif.append({"campo": campo, "label": label,
                                   "actual": _fecha_ui(reg.get(campo)),
                                   "esperado": objetivos["fecha_ui"]})
        fechas_ok             = fechas_ok_count == len(CIERRE_RAPIDO_FECHAS)
        if objetivos["solo_estado"]:
            # SOLO ESTADO: fechas y PDL son referencia, ni criterio ni objetivo.
            necesita_actualizacion = not status_ok
        else:
            necesita_actualizacion = not fechas_ok or not pdl_ok or not status_ok

        if not necesita_actualizacion:
            panel_estado, panel_label = "ok",        "Listo";    meta["listos"] += 1
        else:
            panel_estado, panel_label = "pendiente", "Pendiente"; meta["pendientes"] += 1

        meta["elegibles"] += 1
        rows.append({
            "pid": pid, "pro_code": pro_code, "nombre": nombre or "-",
            "order_number": order_number or "-", "nodos": nodos,
            "nodos_txt": ", ".join(nodos) if nodos else "-",
            "customer": customer, "contract": contract, "department": department,
            "fuentes_txt": ", ".join(base.get("fuentes") or []) or "-",
            "elegible": True, "liquidado_ok": bool(base.get("liquidado_ok")),
            "preseleccionado": necesita_actualizacion,
            "panel_estado": panel_estado, "panel_label": panel_label,
            "necesita_actualizacion": necesita_actualizacion,
            "estado_actual": estado_actual, "estado_esperado": objetivos["estado_esperado"],
            "status_ok": status_ok, "pdl_actual": pdl_actual,
            "pdl_esperado": objetivos["pdl_esperado"] or "-", "pdl_ok": pdl_ok,
            "fecha_esperada_ui": objetivos["fecha_ui"],
            "fechas_ok": fechas_ok, "fechas_ok_count": fechas_ok_count,
            "fechas_total": len(CIERRE_RAPIDO_FECHAS), "fechas_diferentes": fechas_dif,
        })

    rows.sort(key=lambda r: (0 if r["elegible"] else 1, r["pro_code"] or "", r["pid"]))
    meta["total"] = len(rows)
    return rows, meta


def _armar_panel_pdl(sid, pdl_global=""):
    """Panel de verificación y parcheo de PDL por nodo."""
    s         = _sesiones.get(sid) or {}
    pdl_def   = str(pdl_global or s.get("pdl_detectado", "") or "").strip()
    proyectos = _proyectos_identificados_desde_sesion(sid)
    rows      = []
    meta      = {"total": 0, "con_pdl": 0, "sin_pdl": 0, "pdl_global": pdl_def}

    if not proyectos:
        return rows, meta

    registros = _sgs().search_read(
        "project.dominion", [["id", "in", list(proyectos.keys())]],
        ["id", "code", "name", "nodo", "pdl", "order_number"],
        limit=len(proyectos) + 10)
    por_id = {r["id"]: r for r in registros}

    for pid, base in proyectos.items():
        reg       = por_id.get(pid) or {}
        pro_code  = str(reg.get("code")  or base.get("pro_code", "")     or "").strip()
        nombre    = str(reg.get("name")  or base.get("nombre_local", "") or "").strip()
        nodo_sgs  = str(reg.get("nodo", "") or "").strip()
        nodos     = list(base.get("nodos") or [])
        if nodo_sgs and nodo_sgs not in nodos:
            nodos.insert(0, nodo_sgs)
        pdl_actual  = str(reg.get("pdl", "") or "").strip()
        tiene_pdl   = bool(pdl_actual)
        pdl_objetivo = pdl_actual if tiene_pdl else pdl_def
        rows.append({
            "pid": pid, "pro_code": pro_code, "nombre": nombre or "-",
            "nodos_txt": ", ".join(nodos) if nodos else "-",
            "pdl_actual":    pdl_actual or "-",
            "tiene_pdl":     tiene_pdl,
            "pdl_objetivo":  pdl_objetivo,
            "preseleccionado": not tiene_pdl,
        })
        if tiene_pdl:
            meta["con_pdl"] += 1
        else:
            meta["sin_pdl"] += 1

    rows.sort(key=lambda r: (r["pro_code"], r["pid"]))
    meta["total"] = len(rows)
    return rows, meta


def _aplicar_cierre_rapido(sid, pids_objetivo=None, fecha_raw="", pdl_raw="", modo_raw=""):
    s = _sesiones.get(sid)
    if not s:
        return {"hechos": 0, "errores": ["Sesion no encontrada."], "objetivos": 0}

    rows, meta = _armar_panel_cierre_rapido(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw,
                                            modo_raw=modo_raw)
    por_pid    = {row["pid"]: row for row in rows}

    if pids_objetivo:
        objetivos = [int(pid) for pid in pids_objetivo
                     if int(pid) in por_pid and por_pid[int(pid)]["elegible"]]
    else:
        objetivos = [row["pid"] for row in rows if row["necesita_actualizacion"]]

    if not objetivos:
        return {"hechos": 0, "errores": [], "objetivos": 0, "meta": meta}

    storage_state = _obtener_storage_state_activo()
    if not storage_state:
        return {"hechos": 0,
                "errores": ["No hay sesion activa de SGS para aplicar fecha/estado."],
                "objetivos": len(objetivos), "meta": meta}

    hechos, errores, pw = 0, [], None
    try:
        pw = PlaywrightClient(headless=False, slow_mo=200)
        pw.start(storage_state=storage_state)
        for pid in objetivos:
            row = por_pid[pid]
            try:
                if meta.get("solo_estado"):
                    # Plantilla MAT masiva: fechas y PDL intactos, solo estado.
                    res = pw.cambiar_estado_proyecto(
                        pid=pid, pro_code=row["pro_code"],
                        estado_destino=meta["estado_esperado"])
                else:
                    res = pw.actualizar_fechas_proyecto(
                        pid=pid, fecha=meta["fecha_ui"], pro_code=row["pro_code"],
                        pdl=meta["pdl_esperado"] if meta["pdl_esperado"] != "-" else "",
                        estado_destino=meta["estado_esperado"],
                        # PDL vigente leído por API — garantiza que el cierre SUMA
                        # PDL sin borrar los históricos aunque la lectura DOM falle.
                        pdl_actual_api=row.get("pdl_actual", ""))
                if res.get("ok"):
                    hechos += 1
                else:
                    errores.append(f"{row['pro_code'] or pid}: {res.get('error','error desconocido')}")
            except Exception as ex:
                errores.append(f"{row['pro_code'] or pid}: {ex}")
    finally:
        try:
            if pw: pw.close()
        except Exception:
            pass

    return {"hechos": hechos, "errores": errores, "objetivos": len(objetivos), "meta": meta}


# ── Auditoría post-liquidación ────────────────────────────────────────────────

_AUDIT_EXCL = frozenset({"canceled", "canceled_partial", "to_approve"})

def _auditoria_post_nodo(c, pid, pro_code, items_pid, resultados_pid, checks_pid):
    """
    Consulta SGS fresco y clasifica cada material del nodo:
    liquidado / pendiente (con causa) / sin_movimiento.
    Devuelve un dict estructurado para el usuario y para el informe de auditoría.
    """
    from collections import defaultdict

    # --- Lectura fresca ---
    # Nombre del nodo tal como aparece en SGS — el reporte se comparte por
    # pantallazo y el PRO solo no le dice nada al compañero que lo recibe.
    try:
        _proj = c.search_read("project.dominion", [["id", "=", pid]],
                              ["name"], limit=1)
        nodo_nombre = str(_proj[0].get("name") or "") if _proj else ""
    except Exception:
        nodo_nombre = ""

    all_mats = c.search_read(
        "project.material", [["project_id", "=", pid]],
        ["id", "product_code", "advance", "balance", "despachado", "devuelto"],
        limit=500)
    all_pm_ids = [m["id"] for m in all_mats]
    # pm por código (mayúsculas) para detectar la variante con/sin C del mismo material
    _pm_por_codigo_aud: dict = {}
    for _m in all_mats:
        _cc = str(_m.get("product_code") or "").strip().upper()
        if _cc:
            _pm_por_codigo_aud.setdefault(_cc, _m["id"])

    dets = c.search_read(
        "project.material.details", [["project_material_id", "in", all_pm_ids]],
        ["id", "project_material_id", "amount"], limit=2000)
    advance_by_pm = defaultdict(float)
    for d in dets:
        ref   = d["project_material_id"]
        pm_id = ref[0] if isinstance(ref, list) else ref
        advance_by_pm[pm_id] += float(d.get("amount") or 0)

    trays = c.search_read(
        "project.tray", [["project_id", "=", pid]],
        ["id", "type", "state", "state_picking"], limit=300)
    tray_by_id = {t["id"]: t for t in trays}

    tray_mats = c.search_read(
        "project.tray.material", [["project_id", "=", pid]],
        ["id", "project_tray_id", "project_material_id", "deliver_quantity"],
        limit=1000)

    despacho   = defaultdict(float)
    devolucion = defaultdict(float)
    for tm in tray_mats:
        pm_ref = tm.get("project_material_id")
        pm_id  = pm_ref[0] if isinstance(pm_ref, list) else pm_ref
        tid_ref = tm.get("project_tray_id")
        tid     = tid_ref[0] if isinstance(tid_ref, list) else tid_ref
        tray    = tray_by_id.get(tid, {})
        state   = str(tray.get("state") or "").lower()
        sp      = str(tray.get("state_picking") or "").lower()
        ttype   = str(tray.get("type") or "").lower()
        qty     = float(tm.get("deliver_quantity") or 0)
        if state in _AUDIT_EXCL or state not in ("approved", "aprobado"):
            continue
        if ttype == "devolution":
            devolucion[pm_id] += qty
        elif sp in ("atendido", "parcial") or not sp:
            despacho[pm_id] += qty

    # --- Índices ---
    items_by_pm   = {it["pm_id"]: it for it in items_pid if it.get("pm_id")}
    result_by_pm  = {}
    for r in resultados_pid:
        if r.get("pm_id"):
            result_by_pm[r["pm_id"]] = r
    checks_by_pm  = {}
    for mc in checks_pid:
        if mc.get("pm_id"):
            checks_by_pm[mc["pm_id"]] = mc

    # TODOS los intentos por pm (multi-líder = varios items/results por pm_id;
    # los dicts de arriba colapsan al último y perderían líderes).
    items_all_by_pm   = defaultdict(list)
    for it in items_pid:
        if it.get("pm_id"):
            items_all_by_pm[it["pm_id"]].append(it)
    results_all_by_pm = defaultdict(list)
    for r in resultados_pid:
        if r.get("pm_id"):
            results_all_by_pm[r["pm_id"]].append(r)

    def _lideres_de(pm_id):
        """
        A quién intentó liquidar la máquina este material (nombre, cédula,
        cantidad, ok, BAN de despacho). La persona que suma stock lo hace al
        almacén de UN líder — sin esto no sabe a quién sumarle cuando hay
        varios. Fuente: resultados (lo realmente intentado); fallback items;
        último recurso: el líder esperado según el verificador.
        """
        items_pm = items_all_by_pm.get(pm_id) or []
        ban_por_lider = {str(it.get("partner_nombre") or "").strip():
                         str(it.get("ban_justificacion") or "").strip()
                         for it in items_pm}
        fuentes = results_all_by_pm.get(pm_id) or items_pm
        vistos, out = set(), []
        for f in fuentes:
            nom = str(f.get("partner_nombre") or "").strip()
            ced = str(f.get("partner_cedula") or "").strip()
            if (not nom and not ced) or (nom, ced) in vistos:
                continue
            vistos.add((nom, ced))
            out.append({"nombre": nom, "cedula": ced,
                        "cantidad": float(f.get("cantidad") or 0),
                        "ok": bool(f.get("ok")),
                        "ban": ban_por_lider.get(nom, "")})
        if not out and pm_id in checks_by_pm:
            mc   = checks_by_pm[pm_id]
            ceds = mc.get("lideres_cedulas") or {}
            noms = ([mc["partner_nombre"]] if mc.get("partner_nombre")
                    else list((mc.get("partner_splits") or {}).keys()))
            for nom in noms:
                out.append({"nombre": nom, "cedula": ceds.get(nom, ""),
                            "cantidad": 0.0, "ok": False,
                            "ban": ", ".join((mc.get("ban_by_lider") or {})
                                             .get(nom, [])[:2])})
        return out

    def _fmt_lideres(lids):
        """'NOMBRE (C123) · 2 ud [BAN-x]' — solo los intentos fallidos si los
        hay (a esos líderes hay que sumarles stock); si no, todos."""
        if not lids:
            return ""
        fallidos = [l for l in lids if not l.get("ok")]
        partes = []
        for l in (fallidos or lids):
            s = l.get("nombre") or "(sin nombre)"
            if l.get("cedula"):
                s += f" ({l['cedula']})"
            if l.get("cantidad"):
                s += f" · {l['cantidad']:g} ud"
            if l.get("ban"):
                s += f" [{l['ban']}]"
            partes.append(s)
        return " ; ".join(partes)

    # --- Clasificación ---
    liquidados   = []
    pendientes   = []
    sin_mov      = []

    for m in all_mats:
        pm_id = m["id"]
        neto  = round(despacho[pm_id] - devolucion[pm_id], 4)
        adv   = round(advance_by_pm.get(pm_id, 0.0), 4)
        pend  = round(neto - adv, 4)
        cod   = str(m.get("product_code") or "")

        base = {"pm_id": pm_id, "codigo": cod, "neto_bandejas": neto,
                "advance_actual": adv, "pendiente": pend}

        if neto <= 0.001 and adv <= 0.001:
            sin_mov.append(base)
            continue

        if pend <= 0.001:
            liquidados.append(base)
            continue

        # --- Determinar causa del pendiente ---
        if pm_id in items_by_pm:
            res = result_by_pm.get(pm_id, {})
            if res.get("ok"):
                causa  = "ESCRITO_RECOMPUTANDO"
                detalle = "Sistema escribio OK; balance aun recomputando en Odoo"
                raiz   = "recompute"
            elif res.get("error_tipo"):
                # Mostrar el error real de SGS (ej. "Stock no disponibles para
                # el consumo.") — FALLO_SISTEMA solo si no hay mensaje.
                err_msg = str(res.get("error") or "").strip()
                causa   = err_msg[:90] if err_msg else \
                          f"FALLO_SISTEMA ({res.get('error_tipo')})"
                detalle = f"{res.get('error_tipo')}: {err_msg[:120]}"
                raiz    = "error_sistema"
            else:
                causa   = "OMITIDO_CAPA2"
                detalle = "Enviado al pipeline pero filtrado antes de escribir"
                raiz    = "filtro_capa2"
        elif pm_id in checks_by_pm:
            mc = checks_by_pm[pm_id]
            if not mc.get("liquidable"):
                causa   = "BLOQUEADO_VERIFICADOR"
                detalle = (", ".join(mc.get("errores") or [])
                           or f"estado={mc.get('estado')}")
                raiz    = "filtro_verificador"
            else:
                causa   = "NO_SELECCIONADO"
                detalle = "Disponible y verificado pero no fue aprobado para liquidar"
                raiz    = "no_seleccionado"
        else:
            # ¿El Excel traía la OTRA variante (con/sin C) de este mismo código?
            # Sufijo C = material comprado por Dominion: si el cruce apuntó al
            # pm sin C (sin stock), el pendiente real queda en el pm con C.
            _alt_cod = cod[:-1] if cod.upper().endswith("C") else cod + "C"
            _alt_pm  = _pm_por_codigo_aud.get(_alt_cod.upper())
            if _alt_pm and (_alt_pm in items_by_pm or _alt_pm in checks_by_pm):
                causa   = "VARIANTE_C"
                detalle = (f"Excel trae '{_alt_cod}' (pm {_alt_pm}, sin stock); "
                           f"el despacho está en '{cod}' (sufijo C = compra Dominion)")
                raiz    = "variante_c"
            else:
                causa   = "NO_EN_EXCEL"
                detalle = "Material con movimiento en bandejas no incluido en el Excel"
                raiz    = "excel_incompleto"

        _lids = _lideres_de(pm_id)
        pendientes.append({**base, "causa": causa, "detalle": detalle, "raiz": raiz,
                           "lideres": _lids, "lider": _fmt_lideres(_lids)})

    # --- Assessment global ---
    if not pendientes:
        assessment       = "LIMPIO"
        assessment_desc  = "Todos los materiales con movimiento quedaron liquidados."
    else:
        raiz_cnt = Counter(p["raiz"] for p in pendientes)
        partes = []
        if raiz_cnt.get("error_sistema"):
            partes.append(f"{raiz_cnt['error_sistema']} con error al liquidar")
        if raiz_cnt.get("excel_incompleto"):
            partes.append(f"{raiz_cnt['excel_incompleto']} no estaba(n) en Excel")
        if raiz_cnt.get("variante_c"):
            partes.append(f"{raiz_cnt['variante_c']} con variante C (cruce apuntó al código sin C)")
        if raiz_cnt.get("filtro_verificador"):
            partes.append(f"{raiz_cnt['filtro_verificador']} bloqueado(s) por verificador")
        if raiz_cnt.get("filtro_capa2"):
            partes.append(f"{raiz_cnt['filtro_capa2']} filtrado(s) Capa2")
        if raiz_cnt.get("no_seleccionado"):
            partes.append(f"{raiz_cnt['no_seleccionado']} no seleccionado(s)")
        if raiz_cnt.get("recompute"):
            partes.append(f"{raiz_cnt['recompute']} recomputando")
        assessment       = "PENDIENTES"
        assessment_desc  = " | ".join(partes) if partes else f"{len(pendientes)} pendiente(s)"

    # --- Reporte de auditoría (texto plano estructurado) ---
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines_auditoria = [
        "=== AUDITORIA POST-LIQUIDACION ===",
        f"PRO: {pro_code}  PID: {pid}  Timestamp: {ts}",
        f"Nodo SGS: {nodo_nombre or '(sin nombre)'}",
        f"Assessment: {assessment}",
        f"Detalle: {assessment_desc}",
        "",
        f"Resumen: {len(liquidados)+len(pendientes)} con movimiento | "
        f"{len(liquidados)} liquidados | {len(pendientes)} pendientes | "
        f"{len(sin_mov)} sin movimiento",
    ]
    if pendientes:
        lines_auditoria.append("")
        lines_auditoria.append("MATERIALES PENDIENTES:")
        for i, p in enumerate(pendientes, 1):
            lines_auditoria.append(
                f"  {i}. {p['codigo']} (pm#{p['pm_id']})  "
                f"neto={p['neto_bandejas']:g}  advance={p['advance_actual']:g}  "
                f"pendiente={p['pendiente']:g}")
            if p.get("lider"):
                lines_auditoria.append(f"     Lider (sumar stock a): {p['lider']}")
            lines_auditoria.append(f"     Causa: {p['causa']}")
            lines_auditoria.append(f"     Detalle: {p['detalle']}")
            lines_auditoria.append(f"     Raiz: {p['raiz']}")
    lines_auditoria.append("===")
    reporte_auditoria = "\n".join(lines_auditoria)

    return {
        "pid": pid, "pro_code": pro_code, "nodo_nombre": nodo_nombre,
        "timestamp": ts,
        "ok": True,
        "assessment": assessment, "assessment_desc": assessment_desc,
        "resumen": {
            "total_con_movimiento": len(liquidados) + len(pendientes),
            "liquidados":           len(liquidados),
            "pendientes":           len(pendientes),
            "sin_movimiento":       len(sin_mov),
        },
        "pendientes":      pendientes,
        "liquidados_count": len(liquidados),
        "reporte_auditoria":  reporte_auditoria,
    }


def _lanzar_auditorias_post(sid, job, items_mat, resultados_mat, checks_mat_sesion):
    """
    Agrupa items y resultados por pid, lanza un hilo de auditoría por nodo.
    Llamado desde _ejecutar_liquidacion_bg justo después de que terminan los MAT.
    """
    if not items_mat:
        job["auditoria_post"] = {"estado": "listo", "nodos": {}}
        return

    pids_data = {}
    for it, res in zip(items_mat, resultados_mat):
        pid_it = it.get("pid")
        if not pid_it:
            continue
        if pid_it not in pids_data:
            pids_data[pid_it] = {
                "pro_code": it.get("pro_code", ""),
                "items":    [],
                "results":  [],
            }
        pids_data[pid_it]["items"].append(it)
        pids_data[pid_it]["results"].append(res)

    checks_by_pid = {}
    for mc in (checks_mat_sesion or []):
        pid_mc = mc.get("pid")
        if pid_mc:
            checks_by_pid.setdefault(pid_mc, []).append(mc)

    job["auditoria_post"] = {"estado": "corriendo", "nodos": {}}
    total = len(pids_data)
    done  = [0]
    lock  = threading.Lock()

    def _thread(pid, pro_code, items_pid, results_pid, checks_pid):
        try:
            c_a = _nuevo_sgs_readonly()
            result = _auditoria_post_nodo(
                c_a, pid, pro_code, items_pid, results_pid, checks_pid)
            job["auditoria_post"]["nodos"][str(pid)] = result
        except Exception as ex:
            job["auditoria_post"]["nodos"][str(pid)] = {
                "pid": pid, "pro_code": pro_code,
                "ok": False, "error": str(ex),
            }
        finally:
            with lock:
                done[0] += 1
                if done[0] >= total:
                    job["auditoria_post"]["estado"] = "listo"

    for pid, data in pids_data.items():
        t = threading.Thread(
            target=_thread,
            args=(pid, data["pro_code"], data["items"],
                  data["results"], checks_by_pid.get(pid, [])),
            daemon=True)
        t.start()


# ── Background liquidation ────────────────────────────────────────────────────
def _rechazar_si_liquidacion_activa() -> bool:
    """
    Flash + True si ya hay una liquidación corriendo. Solo puede correr UNA a la vez:
    el BROWSER_GOVERNOR es global y su init() resetea la contabilidad de browsers
    activos — dos jobs simultáneos rompen el límite (~24 chromiums) y saturan el
    servidor Odoo, disparando save_timeout (R1: no reintentable).
    Subir/verificar otro proyecto mientras tanto SÍ es seguro (solo lecturas API).
    """
    for jsid, job in list(_trabajos.items()):
        if job.get("estado") == "corriendo":
            mins = int((_time_mod.time() - (job.get("t_inicio") or _time_mod.time())) // 60)
            flash(f"⚠ Ya hay una liquidación corriendo (sesión {jsid[:8]}…, hace {mins} min). "
                  "Solo puede correr una a la vez. Este lote queda verificado y listo — "
                  "lánzalo cuando la otra termine.")
            return True
    return False


def _fase3_solo_estado(s, job, pids_ok, storage_state):
    """
    Fase 3 en modo SOLO ESTADO (plantilla MAT masiva).

    Los nodos ya tuvieron su MO liquidada en meses anteriores: fechas y PDL son
    correctos y NO se tocan. Solo se pone CIERRE_RAPIDO_ESTADO a los nodos 100%
    exitosos que no lo tengan; los que ya lo tienen se registran como
    'ya_estaba' sin abrir browser. Tras cada cambio se relee el estado por API
    y solo cuenta como ok si SGS confirma el estado destino.
    """
    estados = {}
    try:
        estados = _estados_proyectos_api(list(pids_ok.keys()))
    except Exception as ex:
        log.warning(f"[L2 BG ESTADO] No se pudo leer el estado actual por API: {ex}")

    resultados, pendientes = [], []
    for pid, pro_code in pids_ok.items():
        actual = estados.get(pid, "")
        if actual == CIERRE_RAPIDO_ESTADO:
            resultados.append({"pid": pid, "pro_code": pro_code,
                               "estado_antes": actual, "estado_despues": actual,
                               "accion": "ya_estaba", "ok": True, "error": ""})
        else:
            pendientes.append((pid, pro_code, actual))

    log.info(f"[L2 BG ESTADO] Fase 3 SOLO ESTADO — {len(pids_ok)} nodo(s) 100% ok: "
             f"{len(resultados)} ya en '{CIERRE_RAPIDO_ESTADO}', {len(pendientes)} por "
             f"cambiar. Fechas y PDL NO se tocan.")
    job["cierre_estado"] = list(resultados)

    lock = threading.Lock()

    def _worker(w_id, items):
        pw_e = PlaywrightClient(headless=False, slow_mo=200)
        try:
            pw_e.start(storage_state=storage_state)
            for pid, pro_code, antes in items:
                res = {"pid": pid, "pro_code": pro_code, "estado_antes": antes or "-",
                       "estado_despues": "", "accion": "cambiado", "ok": False, "error": ""}
                try:
                    r = pw_e.cambiar_estado_proyecto(
                        pid=pid, pro_code=pro_code, estado_destino=CIERRE_RAPIDO_ESTADO)
                    if not r.get("ok"):
                        res["error"] = r.get("error") or "sin detalle"
                except Exception as ex:
                    res["error"] = _safe_str(ex)
                # Confirmación por API: lo que diga SGS manda, no el browser.
                try:
                    despues = _estados_proyectos_api([pid]).get(pid, "")
                except Exception:
                    despues = ""
                res["estado_despues"] = despues or "-"
                res["ok"] = despues == CIERRE_RAPIDO_ESTADO
                if res["ok"]:
                    log.info(f"[L2 BG ESTADO W{w_id}] pid={pid} {pro_code}: "
                             f"'{antes}' → '{despues}'")
                else:
                    res["accion"] = "error"
                    if not res["error"]:
                        res["error"] = f"SGS sigue en '{despues or '?'}' tras el cambio"
                    log.warning(f"[L2 BG ESTADO W{w_id}] pid={pid} {pro_code}: {res['error']}")
                with lock:
                    resultados.append(res)
                    job["cierre_estado"] = list(resultados)
        except Exception as ex:
            log.warning(f"[L2 BG ESTADO W{w_id}] error general: {ex}")
            with lock:
                hechos = {r["pid"] for r in resultados}
                for pid, pro_code, antes in items:
                    if pid not in hechos:
                        resultados.append({"pid": pid, "pro_code": pro_code,
                                           "estado_antes": antes or "-", "estado_despues": "-",
                                           "accion": "error", "ok": False,
                                           "error": f"worker: {_safe_str(ex)}"})
        finally:
            try:
                pw_e.close()
            except Exception:
                pass

    if pendientes:
        n_w    = min(len(pendientes), TERMINADO_MAX_WORKERS)
        chunks = [pendientes[i::n_w] for i in range(n_w) if pendientes[i::n_w]]
        log.info(f"[L2 BG ESTADO] {len(pendientes)} nodo(s) → {len(chunks)} worker(s)")
        hilos = []
        for w_id, chunk in enumerate(chunks):
            t = threading.Thread(target=_worker, args=(w_id, chunk),
                                 daemon=True, name=f"liq2-estado-{w_id}")
            hilos.append(t)
            t.start()
        for t in hilos:
            t.join()

    resultados.sort(key=lambda r: (r.get("pro_code") or "", r["pid"]))
    s["cierre_estado_resultados"] = resultados
    job["cierre_estado"]          = resultados
    return resultados


_SGO7_ITEM_KEYS = ("tipo", "codigo", "nodo", "pro", "cantidad", "qty", "lider", "partner",
                   "partner_nombre", "ok", "nueva", "estado", "omitido", "error")


def _sgo7_guardar_diagnostico(tipo, sid, texto):
    """SGO7: guarda un informe técnico en el servidor (registros/diagnosticos/liquidador/)."""
    try:
        base = os.path.join("/var/www/sgo7/registros/diagnosticos/liquidador", datetime.now().strftime("%Y-%m"))
        os.makedirs(base, exist_ok=True)
        tipo = re.sub(r"[^a-z0-9_-]", "", (tipo or "informe").lower())[:30] or "informe"
        sid8 = re.sub(r"[^A-Za-z0-9-]", "", sid or "")[:8] or "sin-sesion"
        ruta = os.path.join(base, f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{tipo}_{sid8}.txt")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(texto)
        os.chmod(ruta, 0o640)
        return ruta
    except Exception as ex:
        log.warning(f"[SGO7] diagnóstico no guardado: {ex}")
        return None


def _sgo7_registrar_resultado(sid, s, job, resultados, debug):
    """SGO7 trazabilidad: qué se liquidó, qué falló y quién lo lanzó. Nunca interrumpe el job."""
    try:
        import sgo7_registro
        items = []
        for r in resultados or []:
            it = {k: r.get(k) for k in _SGO7_ITEM_KEYS if k in r}
            if it.get("error"):
                it["error"] = str(it["error"])[:200]
            items.append(it)
        s = s or {}
        ruta_diag = None
        if debug.get("fallidos") or debug.get("error_general"):   # fallos → diagnóstico completo guardado solo
            ruta_diag = _sgo7_guardar_diagnostico("liquidacion-fallos", sid, json.dumps(debug, ensure_ascii=False, indent=1, default=str))
        sgo7_registro.registrar(
            "liquidador_resultado", estado=job.get("estado"), sesion=sid,
            usuario=s.get("sgo7_usuario"), rol=s.get("sgo7_rol"),
            archivo=s.get("archivo") or s.get("nombre_archivo") or s.get("filename"),
            detalle={"total": debug.get("total"), "ok": debug.get("ok"), "fallidos": debug.get("fallidos"),
                     "clases_error": debug.get("clases_error"), "error_general": debug.get("error_general"),
                     "fecha_planilla": s.get("fecha_liquidacion_iso"), "pdl": s.get("pdl"),
                     "lcl": s.get("lcl") or s.get("lcl_odm"), "pro_padre": s.get("pro_padre") or s.get("parent_pro"),
                     "cierre_modo": s.get("cierre_modo"), "diagnostico": ruta_diag, "items": items[:300]})
        if s.get("sgo7_orden"):   # SGO7 facturación: la orden termina con el resultado de la liquidación
            _fall = int(debug.get("fallidos") or 0)
            _est = "liquidada" if (job.get("estado") == "listo" and not _fall and not debug.get("error_general")) else "con_fallos"
            _sgo7_orden_estado(s["sgo7_orden"], _est, sesion=sid, usuario=s.get("sgo7_usuario"),
                               detalle={"total": debug.get("total"), "ok": debug.get("ok"), "fallidos": _fall,
                                        "estado_job": job.get("estado"), "error_general": debug.get("error_general"),
                                        "diagnostico": ruta_diag, "fecha_planilla": s.get("fecha_liquidacion_iso")})
    except Exception as _e:
        log.warning(f"[L2 BG] registro SGO7 no enviado: {_e}")


def _ejecutar_liquidacion_bg(sid, items_mo, items_mat, storage_state):
    """Corre en hilo de fondo — actualiza _trabajos[sid] en tiempo real.

    Fase 0 — Pre-check MAT via API (sin browser).
    Fase 1 — MO (MO_MAX_PROJECT_WORKERS=3) + MAT (MAT_MAX_CONCURRENT_NODES=2 × 2 tabs)
              corren EN PARALELO; MAT arranca con MO_MAT_STAGGER_S de delay.
    Fase 2 — Post-verificación persistencia MAT (API).
    Fase 3 — TERMINADO/fechas en paralelo (1 browser/nodo, max TERMINADO_MAX_WORKERS) solo nodos 100% ok.
    """
    job = _trabajos.get(sid)
    if not job:
        return
    s = _sesiones.get(sid) or {}

    _res_mo  = []   # resultados MO capturados desde el hilo
    _res_mat = []   # resultados MAT: pre-check rechazados + liquidados
    _exc     = [None, None]  # [exc_mo, exc_mat]

    fecha_iso = s.get("fecha_liquidacion_iso", "")
    # SGO7: si ya se conoce el cierre de SGS, la fecha se ajusta antes de liquidar (MO y cierre de nodos)
    try:
        import sgs_cierre as _sgs_cierre
        _fi2, _info = _sgs_cierre.ajustar(fecha_iso)
        if _info and _fi2 != fecha_iso:
            log.info(f"[L2 BG] Fecha de planilla {fecha_iso} → {_fi2} por cierre SGS (rango desde {_info.get('rango_inicio')})")
            fecha_iso = _fi2
            s["fecha_liquidacion_iso"] = _fi2
    except Exception:
        pass
    if not fecha_iso and items_mo:
        # Bloquear solo si hay MO — la fecha se escribe en el modal de cada actividad.
        # Reparaciones MAT-only pueden continuar; la fecha solo afecta fase 3 (fallback a hoy).
        job["estado"] = "error"
        job["error"]  = "Fecha de planilla no definida. Sube el Excel de nuevo con la fecha obligatoria."
        log.error("[L2 BG] Abortado — fecha_liquidacion_iso no configurada y hay items MO")
        return
    if not fecha_iso:
        log.warning("[L2 BG] fecha_liquidacion_iso vacía — reparación MAT sin MO, usando fecha de hoy para fase 3")

    try:
        c = _sgs()

        # ── FASE 0: Pre-check MAT via API (antes de abrir browsers) ─────────
        items_mat_seguros = []
        if items_mat and not job.get("cancelado"):
                try:
                    pm_ids = [it["pm_id"] for it in items_mat]
                    frescos = c.search_read(
                        "project.material", [["id","in",pm_ids]],
                        ["id","advance","amount","balance","despachado","devuelto"], limit=len(pm_ids)+5)
                    dets = c.search_read(
                        "project.material.details", [["project_material_id","in",pm_ids]],
                        ["id","project_material_id","amount"], limit=1000)
                    advance_details = {}
                    for d in dets:
                        amt_det = float(d.get("amount") or 0)
                        if amt_det <= 0: continue
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
                        pendiente_real = round(est.get("balance",  0.0), 4)
                        devuelto       = est.get("devuelto", 0.0)
                        excel_neto     = _f4(it.get("excel_neto", it.get("cantidad", 0)))
                        pendiente_excel= _f4(excel_neto - adv_details)

                        if devuelto > 0.001:
                            log.info(f"[L2 BG MAT] pm_id={it['pm_id']} ({it['codigo']}) "
                                     f"devuelto={devuelto:g} factorizado — continuando Capa2+3.")

                        if pendiente_excel <= 0.001:
                            job_pos = it.get("job_pos")
                            if job_pos is not None and job_pos < len(job["items_mat"]):
                                job["items_mat"][job_pos]["estado"] = "ok"
                                job["items_mat"][job_pos]["error"]  = "Sin pendiente en SGS"
                            _res_mat.append(_mat_res_omitido(it, "Ya liquidado en SGS — omitido"))
                            continue

                        cantidad_contrato = _f4(it.get("cantidad"))
                        if abs(pendiente_excel - cantidad_contrato) > 0.001 and not it.get("multi_lider_split"):
                            msg = (f"Contrato Excel cambio antes de ejecutar: esperado {cantidad_contrato:g}, "
                                   f"ahora pendiente Excel/details={pendiente_excel:g}")
                            job_pos = it.get("job_pos")
                            if job_pos is not None and job_pos < len(job["items_mat"]):
                                job["items_mat"][job_pos]["estado"] = "error"
                                job["items_mat"][job_pos]["error"]  = "Bloqueado: cantidad Excel cambio"
                            _res_mat.append(_mat_res_error(it, msg, "mat_excel_contract_changed", cantidad_contrato))
                            continue

                        # R6 — Sobre-proyección: cantidad pide más que lo presupuestado
                        amount_ppto = _f4(est.get("amount", 0.0))
                        if it.get("multi_lider_split"):
                            log.info(f"[L2 BG MAT] pm_id={it['pm_id']} ({it['codigo']}) "
                                     f"multi_lider partner={it.get('partner_nombre')} "
                                     f"cantidad={cantidad_contrato:g} — saltando check Capa2 individual.")
                        elif devuelto > 0.001:
                            log.info(f"[L2 BG MAT] pm_id={it['pm_id']} ({it['codigo']}) "
                                     f"devuelto={devuelto:g} — saltando check balance Capa2.")
                        else:
                            if abs(pendiente_real - cantidad_contrato) > 0.001:
                                log.info(f"[L2 BG MAT] pm_id={it['pm_id']} ({it['codigo']}) "
                                         f"balance_sgs={pendiente_real:g} vs contrato={cantidad_contrato:g}: "
                                         f"diferencia ignorada (balance stale). Excel es fuente de verdad.")
                            if amount_ppto > 0 and cantidad_contrato > amount_ppto + 0.001:
                                msg = (f"SOBRE_PROYECTADO: Excel pide {cantidad_contrato:g} "
                                       f"pero presupuestado={amount_ppto:g}. "
                                       "Validacion especial requerida — Odoo decidira si acepta o rechaza.")
                                job_pos = it.get("job_pos")
                                if job_pos is not None and job_pos < len(job["items_mat"]):
                                    job["items_mat"][job_pos]["estado"] = "error"
                                    job["items_mat"][job_pos]["error"]  = f"SOBRE_PROYECTADO: {cantidad_contrato:g}>{amount_ppto:g}"
                                _res_mat.append(_mat_res_error(it, msg, "sobre_proyectado", cantidad_contrato))
                                continue

                        items_mat_seguros.append(dict(it, pendiente_api_confirmado=True))

                except Exception as ex:
                    log.error(f"[L2 BG MAT] Pre-check SGS fallo: {ex} — abortando MAT por seguridad")
                    for i, it in enumerate(items_mat):
                        if i < len(job["items_mat"]):
                            job["items_mat"][i]["estado"] = "error"
                            job["items_mat"][i]["error"]  = "No se pudo verificar Pendiente SGS"
                        _res_mat.append({
                            "ok": False, "tipo": "MAT",
                            "codigo": it.get("codigo",""), "pro_code": it.get("pro_code",""),
                            "cantidad": it.get("cantidad",0), "pid": it.get("pid"),
                            "error": f"No se pudo verificar Pendiente SGS: {ex}",
                            "error_tipo": "precheck_pendiente_failed",
                        })
                    items_mat_seguros = []

        # ── FASE 1: MO + MAT en paralelo ─────────────────────────────────────
        def _run_mo():
            try:
                def _progress_mo(orig_idx, worker_id, estado, codigo, error=None):
                    try:
                        job["items_mo"][orig_idx]["worker_id"] = worker_id
                        job["items_mo"][orig_idx]["estado"]    = estado
                        if estado == "corriendo":
                            job["items_mo"][orig_idx]["error"] = None
                        elif error:
                            job["items_mo"][orig_idx]["error"] = str(error)[:120]
                    except Exception:
                        pass
                res_mo = liquidar_mo_parallel_por_proyecto(
                    items_mo,
                    max_workers=MO_MAX_PROJECT_WORKERS,
                    workers_per_node=MO_WORKERS_PER_NODE,
                    storage_state=storage_state,
                    progress_cb=_progress_mo,
                    should_cancel=lambda: bool(job.get("cancelado")),
                    sgs_client_factory=_nuevo_sgs_readonly,
                    fecha=_fecha_mo_fmt(fecha_iso),
                )
                _res_mo.extend(res_mo)
                _SAVE_MARKERS = ("save_timeout", "ya está guardado", "45.0s esperando")
                for i, res in enumerate(res_mo):
                    if i < len(job["items_mo"]):
                        job["items_mo"][i]["estado"] = "ok" if res.get("ok") else "error"
                        if not res.get("ok"):
                            job["items_mo"][i]["error"] = res.get("error","")[:120]
                # Guardar items retryables para el botón manual de reintentar
                _mo_retry = []
                for i, res in enumerate(res_mo):
                    if (not res.get("ok")
                            and res.get("error_tipo","") != "save_timeout"
                            and not any(m in res.get("error","") for m in _SAVE_MARKERS)):
                        item = dict(items_mo[i])
                        if res.get("act_id") and not item.get("act_id"):
                            item["act_id"] = res["act_id"]
                        item["_error"]      = res.get("error","")[:120]
                        item["_error_tipo"] = res.get("error_tipo","")
                        _mo_retry.append(item)
                job["mo_retry_pendientes"] = _mo_retry
            except Exception as e:
                _exc[0] = e
                log.error(f"[L2 BG MO] Hilo MO error: {e}")

        def _run_mat():
            try:
                if not items_mat_seguros or job.get("cancelado"):
                    return
                _time_mod.sleep(MO_MAT_STAGGER_S)  # dejar que MO tome sus workers primero
                if job.get("cancelado"):
                    return

                def _cb(orig_idx, worker_id, estado, codigo, error=None):
                    job_pos = items_mat_seguros[orig_idx].get("job_pos", orig_idx) \
                              if orig_idx < len(items_mat_seguros) else orig_idx
                    if job_pos < len(job["items_mat"]):
                        job["items_mat"][job_pos]["estado"]    = estado
                        job["items_mat"][job_pos]["worker_id"] = worker_id
                        if error:
                            job["items_mat"][job_pos]["error"] = str(error)[:120]

                log.info(f"[L2 BG MAT] WS — {len(items_mat_seguros)} materiales "
                         f"— {MAT_MAX_CONCURRENT_NODES} nodo(s) × 2 tabs")
                res_mat = liquidar_materiales_lista_modal_ws(
                    items_mat_seguros, max_pages=2,
                    max_concurrent_nodes=MAT_MAX_CONCURRENT_NODES,
                    storage_state=storage_state,
                    progress_cb=_cb, should_cancel=lambda: bool(job.get("cancelado")))
                _res_mat.extend(res_mat)
                for i, res in enumerate(res_mat):
                    job_pos = items_mat_seguros[i].get("job_pos", i) \
                              if i < len(items_mat_seguros) else i
                    if job_pos < len(job["items_mat"]):
                        job["items_mat"][job_pos]["estado"] = "ok" if res.get("ok") else "error"
                        if not res.get("ok"):
                            job["items_mat"][job_pos]["error"] = res.get("error","")[:120]
                _lanzar_auditorias_post(sid, job, items_mat_seguros, res_mat,
                                        s.get("checks_mat", []))
            except Exception as e:
                _exc[1] = e
                log.error(f"[L2 BG MAT] Hilo MAT error: {e}")

        # Inicializar el governor global con el total de browsers que pueden coexistir.
        # MO: MO_MAX_PROJECT_WORKERS browsers; MAT-WS: MAT_MAX_CONCURRENT_NODES × 2 tabs.
        # El governor reducirá este límite automáticamente si detecta nav_timeouts repetidos.
        from browser_client import BROWSER_GOVERNOR
        _total_browsers = MO_MAX_PROJECT_WORKERS + MAT_MAX_CONCURRENT_NODES * 2
        BROWSER_GOVERNOR.init(_total_browsers)
        log.info(f"[L2 BG] Lanzando MO ({MO_MAX_PROJECT_WORKERS} workers) + "
                 f"MAT ({MAT_MAX_CONCURRENT_NODES} nodos × 2 tabs) en paralelo "
                 f"— stagger MAT={MO_MAT_STAGGER_S}s — governor limit={_total_browsers}")
        t_mo  = threading.Thread(target=_run_mo,  daemon=True, name="liq2-mo")
        t_mat = threading.Thread(target=_run_mat, daemon=True, name="liq2-mat")
        t_mo.start()
        t_mat.start()
        t_mo.join()
        t_mat.join()

        if _exc[0]:   # error en MO es fatal
            raise _exc[0]
        if _exc[1]:   # error en MAT se loguea pero no aborta (MO ya terminó)
            log.error(f"[L2 BG MAT] Hilo MAT terminó con excepción: {_exc[1]}")

        resultados = list(_res_mo) + list(_res_mat)

        # Adjuntar justificación de bandeja a los resultados MAT que vienen del
        # worker sin ese campo (la tabla Detalle muestra líder + BAN de origen).
        _just_by_key = {(it.get("pm_id"), it.get("partner_nombre", "")):
                        it.get("ban_justificacion", "") for it in items_mat}
        for r in resultados:
            if r.get("tipo") == "MAT" and not r.get("ban_justificacion"):
                r["ban_justificacion"] = _just_by_key.get(
                    (r.get("pm_id"), r.get("partner_nombre", "")), "")

        # ── FASE 2: Post-verificación persistencia MAT ────────────────────────
        pm_ids_mat_ok = [
            r["pm_id"] for r in _res_mat
            if r.get("tipo") == "MAT" and r.get("ok") and r.get("pm_id")
        ]
        if pm_ids_mat_ok:
            try:
                _time_mod.sleep(2)
                # Solo líneas creadas EN ESTA corrida (create_date UTC >= t_inicio − 5 min):
                # sin este filtro, una línea histórica del mismo uid con el mismo amount
                # daba un «persistido» falso cuando el write nunca ocurrió.
                _desde_utc = _time_mod.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    _time_mod.gmtime(job.get("t_inicio", _time_mod.time()) - 300))
                dets_post = c.search_read(
                    "project.material.details",
                    [["project_material_id", "in", pm_ids_mat_ok],
                     ["create_uid", "=", SGS_UID_SERVICIO],
                     ["create_date", ">=", _desde_utc]],
                    ["id", "project_material_id", "amount", "create_date"],
                    limit=500,
                )
                conf_por_pm = {}
                for d in dets_post:
                    pmref  = d.get("project_material_id")
                    pm_key = pmref[0] if isinstance(pmref, list) else pmref
                    conf_por_pm.setdefault(pm_key, []).append(d)
                for r in _res_mat:
                    if not (r.get("tipo") == "MAT" and r.get("ok") and r.get("pm_id")):
                        continue
                    lineas     = conf_por_pm.get(r["pm_id"], [])
                    lineas_ord = sorted(lineas, key=lambda x: x.get("create_date",""), reverse=True)
                    if lineas_ord:
                        ultima        = lineas_ord[0]
                        cant_guardada = float(ultima.get("amount") or 0)
                        confirmado    = abs(cant_guardada - float(r.get("cantidad") or 0)) <= 0.01
                        r["verificacion_persistencia"] = {
                            "linea_id":         ultima.get("id"),
                            "cantidad_guardada": cant_guardada,
                            "fecha":            str(ultima.get("create_date","")),
                            "confirmado":       confirmado,
                        }
                        if not confirmado:
                            log.warning(f"[L2 BG MAT] pm_id={r['pm_id']} post-save: "
                                        f"esperado={r.get('cantidad'):g} guardado={cant_guardada:g}")
                    else:
                        r["verificacion_persistencia"] = {
                            "confirmado": False,
                            "aviso": "No se encontro linea de detalle post-save"
                        }
                        log.warning(f"[L2 BG MAT] pm_id={r['pm_id']} post-save: sin linea (uid de servicio)")
            except Exception as ex:
                log.warning(f"[L2 BG MAT] Post-save check fallo (no critico): {ex}")

        # ── FASE 2b: Reclasificación de falsos negativos via API ─────────────
        # Algunos "errores" son resultados correctos que Playwright clasificó mal:
        #   modal_bad_state   → material sin saldo real en SGS (Excel desactualizado)
        #   post_amount_mismatch → Odoo limitó el amount al balance real (Excel tenía más)
        # Verificamos la situación real en SGS y reclasificamos si el nodo quedó OK.
        _tipos_reclasificables = {"modal_bad_state", "post_amount_mismatch"}
        _candidatos_rc = [r for r in _res_mat
                          if not r.get("ok") and r.get("pm_id")
                          and r.get("error_tipo") in _tipos_reclasificables]
        if _candidatos_rc:
            try:
                pm_ids_rc  = [r["pm_id"] for r in _candidatos_rc]
                frescos_rc = c.search_read(
                    "project.material", [["id", "in", pm_ids_rc]],
                    ["id", "amount", "advance", "balance", "despachado", "devuelto"],
                    limit=len(pm_ids_rc) + 5)
                dets_rc = c.search_read(
                    "project.material.details",
                    [["project_material_id", "in", pm_ids_rc]],
                    ["id", "project_material_id", "amount"],
                    limit=500)
                adv_rc = {}
                for d in dets_rc:
                    ref   = d["project_material_id"]
                    pm_d  = ref[0] if isinstance(ref, list) else ref
                    adv_rc[pm_d] = adv_rc.get(pm_d, 0.0) + float(d.get("amount") or 0)
                por_pm_rc = {f["id"]: f for f in frescos_rc}

                for r in _candidatos_rc:
                    pm_id_rc  = r["pm_id"]
                    tipo_err  = r.get("error_tipo", "")
                    f_rc      = por_pm_rc.get(pm_id_rc, {})
                    adv_total = round(adv_rc.get(pm_id_rc,
                                                  float(f_rc.get("advance") or 0)), 4)
                    desp      = float(f_rc.get("despachado") or 0)
                    dev       = float(f_rc.get("devuelto")   or 0)
                    neto_bnd  = round(desp - dev, 4)
                    amount_pp = float(f_rc.get("amount")     or 0)

                    if tipo_err == "modal_bad_state":
                        # Material sin saldo real: no estaba presupuestado o no tiene
                        # movimiento en bandejas → el modal no tenía AGREGAR. No es fallo.
                        if amount_pp <= 0.001 or neto_bnd <= 0.001:
                            r["ok"]    = True
                            r["aviso"] = (
                                f"Sin saldo real en SGS (presupuesto={amount_pp:g}, "
                                f"neto_bandejas={neto_bnd:g}) — Excel desactualizado"
                            )
                            r["error"] = ""
                            log.info(f"[L2 RC] pm_id={pm_id_rc} ({r.get('codigo')}) "
                                     f"modal_bad_state reclasificado → ok (sin saldo real)")

                    elif tipo_err == "post_amount_mismatch":
                        # Odoo guardó menos de lo que pedía el Excel. Si el advance
                        # ya cubre el neto real de bandejas, el resultado es correcto.
                        if neto_bnd > 0.001 and adv_total >= neto_bnd - 0.001:
                            r["ok"]    = True
                            r["aviso"] = (
                                f"Odoo guardó {adv_total:g} (Excel pedía {r.get('cantidad',0):g}) "
                                f"— neto_bandejas={neto_bnd:g}. Nodo cubierto correctamente."
                            )
                            r["error"] = ""
                            log.info(f"[L2 RC] pm_id={pm_id_rc} ({r.get('codigo')}) "
                                     f"post_amount_mismatch reclasificado → ok "
                                     f"(adv={adv_total:g} >= neto={neto_bnd:g})")
            except Exception as ex_rc:
                log.warning(f"[L2 BG RECLASIF] No se pudo reclasificar falsos negativos: {ex_rc}")

        # ── FASE 3: TERMINADO solo para nodos 100% exitosos (MO + MAT) ───────
        # Un pid se excluye si tiene CUALQUIER fallo en MO o en MAT.
        # Modo SOLO ESTADO (plantilla MAT masiva): ni fecha ni PDL — ver _fase3_solo_estado.
        solo_estado    = _cierre_modo(s) == CIERRE_MODO_SOLO_ESTADO
        fecha_hoy      = "" if solo_estado else _fecha_proj_fmt(fecha_iso)  # fecha de planilla
        pdl_programado = str(s.get("pdl_detectado","") or "").strip()

        pids_con_fallo = set()
        for res in _res_mo:
            if not res.get("ok") and res.get("pid"):
                pids_con_fallo.add(res["pid"])
        for res in _res_mat:
            if not res.get("ok") and res.get("pid"):
                pids_con_fallo.add(res["pid"])

        pids_ok = {}
        for res in list(_res_mo) + list(_res_mat):
            pid = res.get("pid")
            if pid and pid not in pids_con_fallo:
                pids_ok.setdefault(pid, res.get("pro_code",""))

        if pids_con_fallo:
            log.info(f"[L2 BG FECHAS] Nodos excluidos de TERMINADO (fallos): {sorted(pids_con_fallo)}")

        if pids_ok and solo_estado:
            _fase3_solo_estado(s, job, pids_ok, storage_state)
        elif pids_ok:
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
                    log.warning(f"[L2 BG FECHAS] No se pudo leer PDL actual por API: {ex}")

            pid_items  = list(pids_ok.items())
            n_term     = min(len(pid_items), TERMINADO_MAX_WORKERS)
            # Distribuir round-robin: worker 0 → índices 0,n,2n…; worker 1 → 1,n+1…
            chunks = [pid_items[i::n_term] for i in range(n_term) if pid_items[i::n_term]]

            log.info(f"[L2 BG FECHAS] Fase 3 paralela — {len(pid_items)} nodo(s), "
                     f"{len(chunks)} worker(s)")

            def _terminado_worker(w_id, items):
                pw_f = PlaywrightClient(headless=False, slow_mo=200)
                try:
                    pw_f.start(storage_state=storage_state)
                    for pid_liq, pro_liq in items:
                        try:
                            pw_f.actualizar_fechas_proyecto(
                                pid=pid_liq, fecha=fecha_hoy,
                                pro_code=pro_liq, pdl=pdl_programado,
                                pdl_actual_api=_pdl_api_map.get(pid_liq, ""))
                        except Exception as ex:
                            log.warning(f"[L2 BG FECHAS W{w_id}] pid={pid_liq}: {ex}")
                except Exception as ex:
                    log.warning(f"[L2 BG FECHAS W{w_id}] error general: {ex}")
                finally:
                    try:
                        pw_f.close()
                    except Exception:
                        pass

            term_threads = []
            for w_id, chunk in enumerate(chunks):
                t = threading.Thread(
                    target=_terminado_worker, args=(w_id, chunk),
                    daemon=True, name=f"liq2-term-{w_id}")
                term_threads.append(t)
                t.start()
            for t in term_threads:
                t.join()

    except Exception as ex:
        log.error(f"[L2 BG LIQUIDAR] Error general: {ex}")
        job["estado"] = "error"
        job["error"]  = _safe_str(ex)
        _sgo7_registrar_resultado(sid, s, job, [], {"total": 0, "ok": 0, "fallidos": 0, "clases_error": {}, "error_general": _safe_str(ex)[:300]})
        return

    errores_detalle = [
        {"tipo": r.get("tipo","?"), "codigo": r.get("codigo","?"),
         "worker": r.get("worker_id","vis."), "intento": 1,
         "error": r.get("error","")[:200],
         "clase": _clasificar_error(r.get("error",""))}
        for r in resultados if not r.get("ok")
    ]
    debug = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(resultados),
        "ok": sum(1 for r in resultados if r.get("ok")),
        "fallidos": len(resultados) - sum(1 for r in resultados if r.get("ok")),
        "clases_error": dict(Counter(e["clase"] for e in errores_detalle)),
        "errores_detalle": errores_detalle,
    }
    if s:
        s["resultados_liquidacion"] = resultados
        s["debug_liquidacion"]      = debug
    job["resultados"] = resultados
    job["estado"]     = "cancelado" if job.get("cancelado") else "listo"

    # SGO7 trazabilidad: resultado de la liquidación, ítem por ítem (ver sgo7_registro.py)
    _sgo7_registrar_resultado(sid, s, job, resultados, debug)


# ── Rutas ─────────────────────────────────────────────────────────────────────

@liquidador2.route("/api/buscar_lider")
def api_buscar_lider():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    try:
        c = _sgs()
        if q.upper().startswith("C") and len(q) > 1:
            domain = [["vat", "like", q[1:]], ["active", "=", True]]
        else:
            domain = [["name", "ilike", q], ["active", "=", True]]
        partners = c.search_read("res.partner", domain, ["id", "name", "vat"], limit=10)
        return jsonify([
            {"nombre": p["name"], "cedula": "C" + str(p["vat"]) if p.get("vat") else ""}
            for p in partners if p.get("name")
        ])
    except Exception as ex:
        log.warning("[L2] api_buscar_lider: %s", ex)
        return jsonify([])


# ── Crear nodos faltantes ─────────────────────────────────────────────────────

def _crear_hermanos_individuales(s, job, c, pie: "PadreIndividualError",
                                 nodos: list, lider: str, dry_run: bool = False) -> None:
    """
    Familia de SOLITARIOS (el supervisor creó el nodo 1 como individual, sin
    check 'Contiene sub proyectos' y sin padre): cada nodo faltante se crea
    como OTRO individual clonando contrato/departamento/tecnología/ticket del
    nodo plantilla. Colgarlo del individual daría un hijo fantasma.
    Después el verificador lo resuelve solo (order_number / nombre like DF +
    campo nodo). Los 13 campos restantes los copia 'Completar campos'.
    """
    def _m2o_name(v):
        return v[1] if isinstance(v, list) and len(v) > 1 else ""

    fam = pie.fam
    # El form de creación general nace VACÍO (no hay padre que herede): se clona
    # todo lo que el supervisor puso en el nodo plantilla.
    _M2O = {"region": "region_id", "customer": "customer_id", "contract": "contract_id",
            "macro": "macro_id", "micro": "micro_id", "department": "department_id",
            "technology": "technology_id", "approve": "approve_id", "pdii": "pdii",
            "formula": "formula_id", "process": "process_id",
            "process_type": "process_type_id"}
    _TXT = ["order_number", "circuito", "gestor_operativo", "odm", "date_assignment"]
    pl  = c.search_read("project.dominion", [["id", "=", pie.individual["id"]]],
                        ["id", "code", "name", "nodo", "ticket"]
                        + list(_M2O.values()) + _TXT, limit=1)[0]
    base = {k: _m2o_name(pl.get(f)) for k, f in _M2O.items()}
    for f in _TXT:
        v = pl.get(f)
        base[f] = "" if v in (False, None) else str(v).strip()
    _REQ = {"region": "región", "customer": "cliente", "contract": "contrato",
            "department": "departamento", "technology": "tecnología",
            "approve": "supervisor", "formula": "fórmula",
            "date_assignment": "fecha de asignación"}
    faltan = [lbl for k, lbl in _REQ.items() if not base.get(k)]
    if faltan:
        job.update({"estado": "error",
                    "error": f"El nodo plantilla {pl['code']} no tiene {', '.join(faltan)} "
                             f"— no se puede clonar"})
        return

    ticket_pl = str(pl.get("ticket") or "").strip()
    ticket    = ticket_pl if ticket_pl.upper() not in _NODO_PLACEHOLDERS else (fam or "-")
    job["modo"]      = "individual"
    job["plantilla"] = pl["code"]
    log.warning("[CREAR_INDIV] familia de solitarios '%s' — plantilla %s '%s'",
                fam, pl["code"], pl["name"])

    storage_state = _storage_state_escritura_de(job.get("usuario"))
    if not storage_state:
        job.update({"estado": "error",
                    "error": _motivo_sin_sesion(job.get("usuario"))})
        return

    bandeja_approver = lider.split()[0] if lider else ""
    creados = list(s.get("nodos_individuales_creados") or [])

    pw = PlaywrightClient(headless=True, slow_mo=100)
    pw.start(storage_state=storage_state)
    try:
        for nodo_str in nodos:
            nombre, nodo_campo = _nombre_hermano_individual(pl, fam, nodo_str)
            try:
                # R5: releer la familia justo antes de escribir — nunca duplicar.
                hermanos, _lv = _filtrar_no_linea_viva(_hermanos_individuales(c, pl, fam))
                ya = _nodo_ya_existe(hermanos, nodo_str)
                if ya:
                    log.info("[CREAR_INDIV] nodo=%s ya existe: %s", nodo_str, ya.get("code"))
                    job["resultados"].append({"nodo": nodo_str, "status": "ya_existe",
                                              "id": ya["id"], "code": ya.get("code", "")})
                    continue

                log.info("[CREAR_INDIV] Creando nodo=%s nombre='%s' (plantilla %s)",
                         nodo_str, nombre, pl["code"])
                res = pw.crear_proyecto_individual(
                    datos={**base, "name": nombre, "nodo": nodo_campo,
                           "ticket": ticket, "pdl": "-",   # el PDL es por nodo
                           "bandeja_approver": bandeja_approver},
                    dry_run=dry_run, screenshots=True,
                )
                # Lo que dice SGS manda: confirmar por API (el id de la URL no basta).
                nuevo = None
                if not dry_run:
                    hermanos, _lv = _filtrar_no_linea_viva(_hermanos_individuales(c, pl, fam))
                    nuevo = next((h for h in hermanos if h["id"] == res.get("id")), None) \
                        or next((h for h in hermanos
                                 if h["id"] != pl["id"]
                                 and (h.get("name") or "").strip() == nombre), None)
                    if nuevo and nuevo["id"] not in creados:
                        creados.append(nuevo["id"])
                        s["nodos_individuales_creados"] = creados
                status = res.get("status", "?")
                if not dry_run and not nuevo:
                    # R1: GUARDAR ya se clickeó — NO reintentar, revisar en SGS.
                    status = "guardado_verificar"
                job["resultados"].append({
                    "nodo": nodo_str, "status": status, "nombre": nombre,
                    "id":   (nuevo or {}).get("id") or res.get("id"),
                    "code": (nuevo or {}).get("code", ""),
                })
                log.info("[CREAR_INDIV] nodo=%s → status=%s code=%s",
                         nodo_str, status, (nuevo or {}).get("code", ""))
            except Exception as exc:
                log.error("[CREAR_INDIV] Error creando nodo=%s: %s", nodo_str, exc)
                job["resultados"].append({"nodo": nodo_str, "status": "error",
                                          "nombre": nombre, "error": str(exc)})
            finally:
                job["procesados"] = len(job["resultados"])
    finally:
        pw.close()

    job["estado"] = "ok"


def _bg_crear_nodos(sid: str) -> None:
    """Hilo daemon: crea los nodos faltantes en SGS vía Playwright."""
    s   = _sesiones.get(sid)
    job = s.get("job_crear_nodos") if s else None
    if not s or not job:
        return

    try:
        nf          = s.get("nodos_faltantes", {})
        nodos       = nf.get("nodos_faltantes", [])
        parent_pro  = nf.get("parent_pro_code", "")
        ticket      = nf.get("ticket", "")
        lider       = nf.get("lider_nombre", "")

        if not nodos or not parent_pro:
            job.update({"estado": "error", "error": "Datos insuficientes (nodos o PRO padre vacíos)"})
            return

        c = _sgs()

        # Obtener id numérico del proyecto padre
        p_rows = c.search_read("project.dominion", [["code", "=", parent_pro]],
                               ["id", "code", "name", "nodo", "ticket", "order_number"],
                               limit=1)
        if not p_rows:
            job.update({"estado": "error", "error": f"PRO padre '{parent_pro}' no encontrado en SGS"})
            return
        # Nunca colgar nodos de un proyecto individual (nodos fantasma) — si la
        # familia tiene padre real, redirigir; si no, abortar con instrucciones.
        try:
            padre_row, _aviso_padre = _padre_real_para_nodos(c, p_rows[0])
        except PadreIndividualError as pie:
            # Familia de solitarios: el faltante se crea como OTRO individual.
            _crear_hermanos_individuales(s, job, c, pie, nodos, lider)
            return
        except ValueError as ve:
            job.update({"estado": "error", "error": str(ve)})
            return
        if _aviso_padre:
            parent_pro = padre_row["code"]
            s["nodos_faltantes"] = {**nf, "parent_pro_code": parent_pro}
            log.warning("[CREAR_NODOS] %s", _aviso_padre)
        parent_id = padre_row["id"]

        # Nombre del hijo = LCL del padre + nodo (nunca el PDL del Excel)
        try:
            _p_on = c.search_read("project.dominion", [["id", "=", parent_id]],
                                  ["order_number"], limit=1)
            lcl = _lcl_para_nombre(_p_on[0].get("order_number") if _p_on else "", ticket)
        except Exception:
            lcl = ticket

        # Obtener contrato y departamento desde un nodo hermano existente
        hermanos = c.search_read(
            "project.dominion", [["parent_id", "=", parent_id]],
            ["id", "contract_id", "department_id"], limit=1)
        if not hermanos:
            job.update({"estado": "error",
                        "error": f"No hay nodos hermanos en {parent_pro} — crea el primero manualmente"})
            return

        h = hermanos[0]
        contract_name = (h["contract_id"][1]
                         if isinstance(h.get("contract_id"), list) else str(h.get("contract_id") or ""))
        dept_name     = (h["department_id"][1]
                         if isinstance(h.get("department_id"), list) else str(h.get("department_id") or ""))

        if not contract_name:
            job.update({"estado": "error", "error": "No se pudo leer el contrato del nodo hermano"})
            return

        # Primera palabra del líder para el autocomplete de bandeja_approver
        bandeja_approver = lider.split()[0] if lider else ""

        # Obtener storage_state del browser activo (debe estar abierto)
        storage_state = _storage_state_escritura_de(job.get("usuario"))
        if not storage_state:
            job.update({"estado": "error",
                        "error": _motivo_sin_sesion(job.get("usuario"))})
            return

        pw = PlaywrightClient(headless=True, slow_mo=100)
        pw.start(storage_state=storage_state)

        try:
            for nodo_str in nodos:
                # Nodo compuesto "127AL122" → campo="N127_N122", nombre="… N127_N122"
                # Nodo simple   "125"     → campo="125",       nombre="… NODO 125"
                m_comp = re.match(r"^(\d+)AL(\d+)$", nodo_str.upper())
                if m_comp:
                    nodo_campo = f"N{m_comp.group(1)}_N{m_comp.group(2)}"
                    sufijo     = nodo_campo
                else:
                    nodo_campo = nodo_str
                    sufijo     = f"NODO {nodo_str}"
                nombre = f"{lcl} {sufijo}" if lcl else sufijo
                log.info("[CREAR_NODOS] Creando nodo=%s nombre='%s' parent_id=%s", nodo_str, nombre, parent_id)
                try:
                    res = pw.crear_proyecto_hijo(
                        parent_id=parent_id,
                        nombre=nombre,
                        nodo=nodo_campo,
                        ticket=lcl or ticket or "-",
                        pdl="-",
                        contract_id_name=contract_name,
                        department_id_name=dept_name,
                        technology_id_name="PROYECTO",
                        bandeja_approver_name=bandeja_approver,
                        dry_run=False,
                        screenshots=True,
                    )
                    job["resultados"].append({
                        "nodo":   nodo_str,
                        "status": res.get("status", "?"),
                        "id":     res.get("id"),
                    })
                    log.info("[CREAR_NODOS] nodo=%s → status=%s id=%s", nodo_str, res.get("status"), res.get("id"))
                except Exception as exc:
                    log.error("[CREAR_NODOS] Error creando nodo=%s: %s", nodo_str, exc)
                    job["resultados"].append({"nodo": nodo_str, "status": "error", "error": str(exc)})
                finally:
                    job["procesados"] = len(job["resultados"])
        finally:
            pw.close()

        job["estado"] = "ok"

    except Exception as ex:
        log.error("[CREAR_NODOS] Error general: %s", ex)
        if job:
            job.update({"estado": "error", "error": str(ex)})


@liquidador2.route("/crear_nodos_faltantes/<sid>", methods=["POST"])
def crear_nodos_faltantes(sid):
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"error": "Sesión no encontrada"}), 404

    nf    = s.get("nodos_faltantes", {})
    nodos = nf.get("nodos_faltantes", [])
    if not nodos:
        return jsonify({"error": "No hay nodos faltantes en esta sesión"}), 400

    # Iniciar (o reiniciar) el job
    # El hilo no tiene petición → el usuario SGO7 viaja en el job y su sesión SGS se abre ya.
    _u_job = _usuario_actual()
    _asegurar_sesion_usuario(_u_job, "crear nodos")
    s["job_crear_nodos"] = {
        "usuario":    _u_job,
        "estado":     "running",
        "total":      len(nodos),
        "procesados": 0,
        "resultados": [],
    }
    threading.Thread(target=_bg_crear_nodos, args=(sid,), daemon=True).start()
    log.info("[CREAR_NODOS] Job lanzado | sid=%s | nodos=%s | usuario=%s", sid[:8], nodos, _u_job)
    return jsonify({"ok": True, "total": len(nodos), "nodos": nodos})


@liquidador2.route("/crear_nodos_estado/<sid>")
def crear_nodos_estado(sid):
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"estado": "no_sesion"}), 404
    job = s.get("job_crear_nodos")
    if not job:
        return jsonify({"estado": "no_iniciado"})
    return jsonify(job)


# ── Completar campos de nodos ya importados (inyección de 13 campos) ─────────

CAMPOS_COMPLETAR = ["macro_id", "micro_id", "pdii", "process_id", "process_type_id",
                    "circuito", "gestor_operativo", "order_number", "pdl", "odm",
                    "bandeja_approver_ids"]


def _bg_completar_nodos(sid: str) -> None:
    """
    Hilo daemon: lee los 13 campos del PRO padre y los escribe en los hijos
    que el import nativo de Odoo dejó vacíos (macro_id == False).
    Autorización: mismo patrón que completar_nodos_importados.py del proyecto referencia.
    """
    s   = _sesiones.get(sid)
    job = s.get("job_completar_nodos") if s else None
    if not s or not job:
        return

    def _id(v):
        return v[0] if isinstance(v, list) and v else (v if v not in (False, None, "") else None)

    def _extraer_nodo(nombre):
        m = re.search(r"NODO\s+(.+)$", nombre or "", re.IGNORECASE)
        if m: return m.group(1).strip()
        # "6301011198 N10_N23" → "10AL23" (formato compuesto)
        m2 = re.search(r"N(\d+)_N(\d+)$", nombre or "")
        if m2: return f"{m2.group(1)}AL{m2.group(2)}"
        return ""

    try:
        nf          = s.get("nodos_faltantes") or {}
        parent_code = nf.get("parent_pro_code", "")
        if not parent_code:
            job.update({"estado": "error", "error": "PRO padre no detectado en la sesión"}); return

        c   = _sgs()
        RPC = c._inner._call_kw
        CTX = {"context": {"allowed_company_ids": [1]}}

        # 1. Leer campos del padre
        _campos_padre = ["id", "code", "name", "nodo", "ticket"] + CAMPOS_COMPLETAR
        padre = c.search_read("project.dominion", [["code", "=", parent_code]],
                               _campos_padre, limit=1)
        if not padre:
            job.update({"estado": "error", "error": f"Padre '{parent_code}' no encontrado"}); return
        padre = padre[0]
        # Un proyecto individual no es fuente válida de campos de familia
        # (su order_number/ticket son propios, ej. 'WR-24532'/'N/A') — redirigir
        # al padre real o abortar (nodos fantasma).
        modo_individual = False
        try:
            _padre_real, _aviso_padre = _padre_real_para_nodos(c, padre)
        except PadreIndividualError as pie:
            # Familia de solitarios: la plantilla ES el nodo individual y los
            # destinos son solo los hermanos que creó ESTE usuario (nunca el
            # nodo del supervisor ni individuales ajenos de la misma familia).
            modo_individual, _aviso_padre = True, ""
            _fam_ind = pie.fam
        except ValueError as ve:
            job.update({"estado": "error", "error": str(ve)}); return
        if _aviso_padre:
            padre = c.search_read("project.dominion", [["id", "=", _padre_real["id"]]],
                                   _campos_padre, limit=1)[0]
            parent_code = padre["code"]
            s["nodos_faltantes"] = {**nf, "parent_pro_code": parent_code}
            log.warning("[COMPLETAR] %s", _aviso_padre)
        parent_id = padre["id"]

        plantilla = {}
        for f in CAMPOS_COMPLETAR:
            v = padre.get(f)
            if f == "bandeja_approver_ids":
                ids = v if isinstance(v, list) else []
                plantilla[f] = [[6, 0, ids]] if ids else None
            else:
                plantilla[f] = _id(v)

        order_num = plantilla.get("order_number")

        # 2. Buscar hijos incompletos (macro_id=False)
        if modo_individual:
            plantilla.pop("pdl", None)   # el PDL es por nodo — no heredar el del nodo 1
            _creados = set(s.get("nodos_individuales_creados") or [])
            hijos = [h for h in _hermanos_individuales(c, padre, _fam_ind)
                     if h["id"] != parent_id
                     and (h["id"] in _creados
                          or _id(h.get("create_uid")) == c.uid)]
            job["modo"] = "individual"
        else:
            hijos = c.search_read("project.dominion", [["parent_id", "=", parent_id]],
                                   ["id", "code", "name", "macro_id", "nodo"], limit=500)
        incompletos = [h for h in hijos if h.get("macro_id") in (False, None, [], "")]
        job["total"]    = len(incompletos)
        job["procesados"] = 0

        resultados = []
        for h in incompletos:
            vals = {k: v for k, v in plantilla.items() if v is not None}
            if modo_individual:
                # nodo y ticket ya los puso el form de creación — no pisarlos
                nodo_num = str(h.get("nodo") or "").strip()
            else:
                nodo_num = _extraer_nodo(h["name"])
                vals["nodo"] = nodo_num
                if order_num:
                    vals["ticket"] = order_num
            vals.setdefault("pdl", "-")
            vals.setdefault("odm", "-")
            try:
                RPC("project.dominion", "write", [[h["id"]], vals], CTX)
                resultados.append({"id": h["id"], "code": h.get("code",""), "nodo": nodo_num, "status": "ok"})
                log.info("[COMPLETAR] %s nodo=%s → ok", h.get("code",""), nodo_num)
            except Exception as ex:
                resultados.append({"id": h["id"], "code": h.get("code",""), "nodo": nodo_num,
                                    "status": "error", "error": str(ex)})
                log.error("[COMPLETAR] %s → error: %s", h.get("code",""), ex)
            job["procesados"] = len(resultados)

        job["resultados"] = resultados
        job["estado"]     = "ok"

    except Exception as ex:
        log.error("[COMPLETAR] Error general: %s", ex)
        if job:
            job.update({"estado": "error", "error": str(ex)})


@liquidador2.route("/completar_nodos/<sid>", methods=["POST"])
def completar_nodos(sid):
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"error": "Sesión no encontrada"}), 404
    nf = s.get("nodos_faltantes") or {}
    if not nf.get("parent_pro_code"):
        return jsonify({"error": "PRO padre no detectado"}), 400
    s["job_completar_nodos"] = {"estado": "corriendo", "total": 0, "procesados": 0, "resultados": []}
    threading.Thread(target=_bg_completar_nodos, args=(sid,), daemon=True,
                     name=f"completar-{sid[:8]}").start()
    return jsonify({"ok": True})


@liquidador2.route("/completar_nodos_estado/<sid>")
def completar_nodos_estado(sid):
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"estado": "no_sesion"}), 404
    job = s.get("job_completar_nodos")
    if not job:
        return jsonify({"estado": "no_iniciado"})
    return jsonify(job)


@liquidador2.route("/reverificar_nodos/<sid>")
def reverificar_nodos(sid):
    """
    Verificación rápida: solo consulta SGS si los nodos faltantes ya existen.
    No re-procesa las filas MO. Retorna JSON con estado por nodo.
    """
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"error": "Sesión no encontrada"}), 404
    nf = s.get("nodos_faltantes") or {}
    nodos_pendientes = nf.get("nodos_faltantes", [])
    parent_code      = nf.get("parent_pro_code", "")
    ticket           = nf.get("ticket", "")

    if not nodos_pendientes:
        return jsonify({"ok": True, "todos_encontrados": True, "resultados": []})

    if not parent_code:
        return jsonify({"error": "PRO padre no detectado, usa Re-verificar todo"}), 400

    try:
        c = _sgs()
        padre = c.search_read("project.dominion", [["code", "=", parent_code]],
                               ["id", "code", "name", "nodo", "ticket", "order_number"],
                               limit=1)
        if not padre:
            return jsonify({"error": f"Padre '{parent_code}' no encontrado"}), 400
        # Verificar contra la familia del padre REAL: los hijos colgados de un
        # proyecto individual son fantasmas (existen pero SGS no los muestra) y
        # marcarlos "encontrado" dejaría el nodo sin crear de verdad.
        _pie = None
        try:
            padre_row, _aviso_padre = _padre_real_para_nodos(c, padre[0])
        except PadreIndividualError as pie:
            _pie, _aviso_padre = pie, ""
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        if _aviso_padre:
            parent_code = padre_row["code"]
            nf = {**nf, "parent_pro_code": parent_code}
            s["nodos_faltantes"] = nf
            padre = c.search_read("project.dominion", [["id", "=", padre_row["id"]]],
                                   ["id", "order_number"], limit=1)
        parent_id = padre[0]["id"]
        prefijo   = _lcl_para_nombre(padre[0].get("order_number"), ticket)

        # Excluir Línea Viva/LLEE (mismo criterio del verificador): sin esto el
        # botón "Verificar en SGS" marcaría el nodo como encontrado apuntando a
        # un proyecto LV que el verificador rechaza — estados contradictorios.
        _campos_h = ["id", "code", "name", "macro_id", "nodo",
                     "department_id", "contract_id"]
        if _pie:
            # Familia de solitarios: el faltante aparece como hermano SIN padre
            # (los colgados del individual son fantasmas y quedan fuera). El
            # nodo plantilla también es miembro de la familia.
            hijos = _hermanos_individuales(c, padre[0], _pie.fam, _campos_h)
        else:
            hijos = _hijos_familia(c, parent_id, padre[0].get("order_number", ""),
                                   fields=_campos_h)
        hijos, _lv_rev = _filtrar_no_linea_viva(hijos)
        if _lv_rev:
            log.info("reverificar_nodos: %d hijo(s) Linea Viva excluidos: %s",
                     len(_lv_rev), [h.get("code") for h in _lv_rev])

        resultados = []
        nodos_aun_pendientes = []
        for nodo_str in nodos_pendientes:
            m_comp = re.match(r"^(\d+)AL(\d+)$", str(nodo_str).upper())
            sufijo = f"N{m_comp.group(1)}_N{m_comp.group(2)}" if m_comp else f"NODO {nodo_str}"
            nombre_esperado = f"{prefijo} {sufijo}" if prefijo else sufijo

            encontrado = next((h for h in hijos if
                               (h.get("name") or "").strip() == nombre_esperado.strip()), None)
            if not encontrado:
                # Búsqueda flexible por sufijo
                encontrado = next((h for h in hijos if sufijo in (h.get("name") or "")), None)
            if not encontrado:
                # Matching tolerante por nodo: 'NODO 26AL31', 'NODO 31 AL 33',
                # 'N26_N31' — mismas reglas que el verificador (anti-duplicados).
                encontrado = _nodo_ya_existe(hijos, nodo_str)

            if encontrado:
                completo = encontrado.get("macro_id") not in (False, None, [], "")
                resultados.append({
                    "nodo": nodo_str, "status": "encontrado",
                    "code": encontrado.get("code",""),
                    "completo": completo,
                    "nombre": encontrado.get("name",""),
                })
            else:
                nodos_aun_pendientes.append(nodo_str)
                resultados.append({"nodo": nodo_str, "status": "pendiente"})

        # Actualizar nodos_faltantes en sesión con los que siguen pendientes
        if len(nodos_aun_pendientes) < len(nodos_pendientes):
            s["nodos_faltantes"] = {**nf, "nodos_faltantes": nodos_aun_pendientes}

        todos = len(nodos_aun_pendientes) == 0
        return jsonify({"ok": True, "todos_encontrados": todos, "resultados": resultados})

    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


# ── SGO7 línea de facturación: órdenes «Mandar a liquidar» que llegan del Balance Integrado ──────
_SGO7_API = os.environ.get("SGO7_API_URL", "http://127.0.0.1:3008/api/sgo7")
_SGO7_CASOS_DIR = "/var/www/sgo7/registros/casos"


def _sgo7_api(path, payload=None, timeout=10):
    r = requests.request("POST" if payload is not None else "GET", _SGO7_API + path, json=payload,
                         headers={"X-SGO7-Interno": os.environ.get("SGO7_INTERNAL_TOKEN", "")}, timeout=timeout)
    if r.status_code >= 400:
        try:
            det = r.json().get("detail")
        except Exception:
            det = None
        raise RuntimeError(det or f"HTTP {r.status_code}")
    return r.json()


def _sgo7_co(iso):
    try:
        from datetime import timezone, timedelta
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone(timedelta(hours=-5))).strftime("%d/%m %H:%M")
    except Exception:
        return str(iso or "")[:16]


def _sgo7_archivo_de_orden(oid):
    """Devuelve (orden, FileStorage con el Excel del caso). El archivo vive en registros/casos/<id>/."""
    from werkzeug.datastructures import FileStorage
    o = _sgo7_api(f"/interno/ordenes/{int(oid)}")
    ruta = o.get("excel_ruta") or ""
    if not ruta.startswith(_SGO7_CASOS_DIR + "/") or not os.path.isfile(ruta):
        raise RuntimeError("el Excel de la orden no está en el servidor")
    with open(ruta, "rb") as fh:
        data = fh.read()
    nombre = o.get("excel_archivo") or os.path.basename(ruta)
    if not nombre.lower().endswith((".xlsx", ".xlsm", ".xls")):
        nombre += ".xlsx"
    return o, FileStorage(stream=io.BytesIO(data), filename=nombre)


def _sgo7_orden_estado(oid, estado, sesion=None, detalle=None, usuario=None):
    """Avisa al panel el estado de la orden (en_liquidacion / liquidada / con_fallos). Nunca interrumpe."""
    try:
        if usuario is None:
            usuario = _usuario_actual()
        _sgo7_api(f"/interno/ordenes/{int(oid)}/estado", {"usuario": usuario, "estado": estado, "sesion": sesion, "detalle": detalle or {}})
    except Exception as ex:
        log.warning(f"[SGO7] estado de orden {oid} → {estado} no enviado: {ex}")


@liquidador2.route("/orden/<int:oid>")
def orden(oid):
    """Orden de facturación: qué caso es, quién la mandó, semáforo y botón para cargar su Excel."""
    try:
        o = _sgo7_api(f"/interno/ordenes/{oid}")
    except Exception as ex:
        flash(f"No se pudo abrir la orden #{oid}: {ex}")
        return redirect(url_for("liquidador2.subir"))
    o["orden_enviada_co"] = _sgo7_co(o.get("orden_enviada_en"))
    o.setdefault("orden_estado_label", o.get("orden_estado"))
    return render_template("liquidador2/orden.html", o=o)


@liquidador2.route("/")
def index():
    return redirect(url_for("liquidador2.subir"))


@liquidador2.route("/subir", methods=["GET", "POST"])
def subir():
    if request.method == "POST":
        f = request.files.get("excel")
        _orden = None   # SGO7 facturación: la planilla viene de una orden «Mandar a liquidar»
        if not f and (request.form.get("orden_id") or "").strip():
            try:
                _orden, f = _sgo7_archivo_de_orden(request.form.get("orden_id"))
            except Exception as _ex:
                flash(f"No se pudo abrir la orden de facturación: {_ex}")
                return redirect(url_for("liquidador2.subir"))
        if not f or not f.filename.endswith((".xlsx", ".xlsm", ".xls")):
            flash("Sube un archivo Excel (.xlsx)")
            return redirect(url_for("liquidador2.subir"))

        try:
            wb = openpyxl.load_workbook(io.BytesIO(f.read()), data_only=True)
        except Exception as ex:
            flash(f"Error leyendo Excel: {ex}")
            return redirect(url_for("liquidador2.subir"))

        sid = str(uuid.uuid4())

        rows_mo,  errs_mo  = parsear_mo(wb)
        rows_mat, errs_mat = parsear_mat(wb)
        total_mo           = extraer_total_mo(wb)
        pdl_detectado      = extraer_pdl_programado(wb)
        nodos_faltantes_mo = extraer_nodos_faltantes_mo(wb)
        distribucion_excel = extraer_distribucion_mo(wb)
        fecha_excel        = extraer_fecha_ejecucion(wb)
        mat_masiva         = es_plantilla_mat_masiva(wb)
        if mat_masiva:
            # Plantilla MASIVA de MATERIALES (Verificador SGS → 'Automático por
            # Mes'): solo MAT, varios PRO por archivo, sin MO por diseño. El
            # aviso "columnas requeridas" del parser MO no es un error aquí.
            errs_mo = [e for e in errs_mo if e.startswith("[INFO]")]
            _n_pro  = len({r.get("pro_code") for r in rows_mat if r.get("pro_code")})
            errs_mo.insert(0, (
                f"[INFO] Plantilla MAT masiva detectada: {len(rows_mat)} material(es) "
                f"en {_n_pro} nodo(s) PRO, sin mano de obra por diseño. Cierre en modo "
                f"SOLO ESTADO: fechas y PDL del nodo no se tocan; solo se pone "
                f"'{CIERRE_RAPIDO_ESTADO}' a los nodos que no lo tengan."))

        lcl_filename = _codigo_programado_desde_nombre(f.filename)
        if lcl_filename:
            for row in rows_mo:  row["_odm"] = lcl_filename
            for row in rows_mat: row["_odm"] = lcl_filename
            errs_mo.append(f"[INFO] Programado detectado por nombre — LCL: {lcl_filename}")

        fecha_planilla = request.form.get("fecha_planilla", "").strip()
        if not fecha_planilla and fecha_excel:
            # Default automático: hoja LIQUIDACION del Excel ("Fecha real de
            # ejecucion"). Editable después con set_fecha en la verificación.
            fecha_planilla = fecha_excel
            flash(f"Fecha de ejecución tomada de la hoja LIQUIDACION del Excel: "
                  f"{_fecha_proj_fmt(fecha_excel)}. Puedes cambiarla en esta página.")
        elif fecha_planilla and fecha_excel and fecha_planilla != fecha_excel:
            flash(f"Aviso: la fecha que ingresaste ({_fecha_proj_fmt(fecha_planilla)}) "
                  f"difiere de la hoja LIQUIDACION del Excel ({_fecha_proj_fmt(fecha_excel)}). "
                  f"Se usa la que ingresaste.")
        if not fecha_planilla and mat_masiva:
            # La plantilla MAT masiva no trae fecha a propósito (varios proyectos).
            # En SOLO ESTADO no hace falta: los materiales no llevan fecha y las
            # fechas del nodo no se tocan. Cambiar a 'fecha + estado' exige definirla.
            flash("Plantilla MAT masiva sin fecha de planilla: correcto. En modo SOLO "
                  "ESTADO la fecha no se escribe en SGS. Si necesitas cerrar con fecha, "
                  "defínela en la verificación y cambia el modo de cierre.")
        elif not fecha_planilla:
            flash("La fecha de planilla es obligatoria (el Excel no trae hoja "
                  "LIQUIDACION con 'Fecha real de ejecución').")
            return redirect(url_for("liquidador2.subir"))

        _sesiones[sid] = {
            "filename":           f.filename,
            "rows_mo":            rows_mo,
            "rows_mat":           rows_mat,
            "errs_mo":            errs_mo,
            "errs_mat":           errs_mat,
            "total_mo":           total_mo,
            "pdl_detectado":      pdl_detectado,
            "nodos_faltantes":    nodos_faltantes_mo,
            "distribucion_excel": distribucion_excel,
            "checks_mo":          [],
            "checks_mat":         [],
            "verificado_at":      None,
            "creado_at":          datetime.now().strftime("%Y-%m-%d %H:%M"),
            "fecha_liquidacion_iso": _sgo7_fecha_cierre(fecha_planilla),  # YYYY-MM-DD; ajustada si SGS ya cerró el periodo
            # Plantilla MAT masiva → cierre SOLO ESTADO (fechas/PDL intactos).
            "plantilla_mat_masiva": mat_masiva,
            "cierre_modo": CIERRE_MODO_SOLO_ESTADO if mat_masiva else CIERRE_MODO_FECHA_ESTADO,
        }
        log.info(f"[L2] Excel cargado | sid={sid[:8]} | {f.filename} | "
                 f"MO={len(rows_mo)} MAT={len(rows_mat)} | fecha_planilla={fecha_planilla} | "
                 f"mat_masiva={mat_masiva} cierre_modo={_sesiones[sid]['cierre_modo']}")
        _sesiones[sid]["sgo7_usuario"] = _usuario_actual()
        if _orden:   # SGO7 facturación: sesión atada a la orden; la orden pasa a «en liquidación»
            _sesiones[sid]["sgo7_orden"] = _orden.get("id")
            _sgo7_orden_estado(_orden.get("id"), "en_liquidacion", sesion=sid,
                               detalle={"filas_mo": len(rows_mo), "filas_mat": len(rows_mat), "archivo": f.filename,
                                        "fecha_planilla": _sesiones[sid]["fecha_liquidacion_iso"]})
        _asegurar_sesion_usuario(_usuario_actual(), "subida de planilla")   # SGO7: sesión SGS con SU cuenta
        # SGO7 trazabilidad: cada planilla subida queda ligada a su sesión (MO/MAT/PDL leídos, huella y copia)
        try:
            import sgo7_registro
            from flask import g as _g
            _arch = (getattr(_g, "_sgo7_archivos", None) or [None])[0]
            sgo7_registro.registrar("liquidador_planilla", sesion=sid, archivo=f.filename,
                detalle={"filas_mo": len(rows_mo), "filas_mat": len(rows_mat), "total_mo": total_mo,
                         "pdl_detectado": pdl_detectado, "lcl": lcl_filename,
                         "fecha_planilla": _sesiones[sid]["fecha_liquidacion_iso"], "fecha_excel": fecha_excel,
                         "mat_masiva": mat_masiva, "cierre_modo": _sesiones[sid]["cierre_modo"],
                         "nodos_faltantes": len(nodos_faltantes_mo or []),
                         "sha256": _arch[2] if _arch else (_orden or {}).get("excel_sha256"), "bytes": _arch[1] if _arch else (_orden or {}).get("excel_bytes"),
                         "orden": (_orden or {}).get("id"),
                         "copia": _arch[3] if _arch else None})
        except Exception as _e:
            log.warning(f"[SGO7] registro de planilla no enviado: {_e}")
        return redirect(url_for("liquidador2.verificar", sid=sid))

    return render_template("liquidador2/subir.html")


def _sgo7_fecha_cierre(fecha_iso, sid=None):
    """SGO7, regla «cierre mensual de SGS»: si hay un cierre aprendido y la fecha de planilla es
    anterior al rango en curso, se ajusta sola a inicio + 1 día (para todos los usuarios), con
    aviso en pantalla y registro en Registros."""
    try:
        import sgs_cierre
        nueva, info = sgs_cierre.ajustar(fecha_iso)
    except Exception:
        return fecha_iso
    if not info or nueva == fecha_iso:
        return fecha_iso
    flash(f"Fecha de planilla ajustada de {_fecha_proj_fmt(fecha_iso)} a {_fecha_proj_fmt(nueva)} por regla de cierre SGS: "
          f"{sgs_cierre.descripcion()}")
    try:
        import sgo7_registro
        sgo7_registro.registrar("liquidador_fecha_ajustada", estado="ajustado", sesion=sid,
                                detalle={"fecha_original": fecha_iso, "fecha_aplicada": nueva,
                                         "rango_inicio": info.get("rango_inicio"), "rango_fin": info.get("rango_fin"),
                                         "cierre_detectado": info.get("detectado"), "detectado_por": info.get("detectado_por"),
                                         "motivo": info.get("error"),
                                         "regla": "cierre SGS: planillas anteriores al rango se registran con inicio + 1 día (autorizado 2026-10-08)"})
    except Exception:
        pass
    return nueva


def _fecha_mo_fmt(iso: str) -> str:
    """YYYY-MM-DD → DD/MM/YYYY para campo de fecha en actividades MO."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return datetime.now().strftime("%d/%m/%Y")


def _fecha_proj_fmt(iso: str) -> str:
    """YYYY-MM-DD → DD-MM-YYYY para actualizar_fechas_proyecto."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d-%m-%Y")
    except Exception:
        return datetime.now().strftime("%d-%m-%Y")


@liquidador2.route("/set_fecha/<sid>", methods=["POST"])
def set_fecha(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))
    nueva = request.form.get("fecha_planilla", "").strip()
    if not nueva:
        flash("La fecha de planilla no puede estar vacía.", "error")
    else:
        s["fecha_liquidacion_iso"] = _sgo7_fecha_cierre(nueva, sid)
        flash(f"Fecha de planilla actualizada: {_fecha_proj_fmt(nueva)}")
    return redirect(url_for("liquidador2.verificar", sid=sid))


@liquidador2.route("/set_cierre_modo/<sid>", methods=["POST"])
def set_cierre_modo(sid):
    """Cambia el modo de cierre de la sesión (fecha + estado / solo estado)."""
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))
    modo = request.form.get("cierre_modo", "").strip()
    if modo not in CIERRE_MODOS:
        flash("Modo de cierre no válido.", "error")
    elif modo == CIERRE_MODO_FECHA_ESTADO and not s.get("fecha_liquidacion_iso"):
        flash("Para cerrar con fecha + estado define primero la fecha de planilla.", "error")
    else:
        s["cierre_modo"] = modo
        if modo == CIERRE_MODO_SOLO_ESTADO:
            flash(f"Cierre en modo SOLO ESTADO: al final solo se pone "
                  f"'{CIERRE_RAPIDO_ESTADO}' a los nodos que no lo tengan. "
                  f"Fechas y PDL no se tocan.")
        else:
            flash("Cierre en modo FECHA + ESTADO: al final se escriben las 7 fechas, "
                  "el PDL y el estado (flujo normal).")
        log.info(f"[L2] cierre_modo={modo} | sid={sid[:8]}")
    return redirect(url_for("liquidador2.verificar", sid=sid))


@liquidador2.route("/verificar/<sid>")
def verificar(sid):
    _asegurar_sesion_usuario(_usuario_actual(), "verificación")   # SGO7: abre/renueva la sesión SGS del usuario
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada. Sube el Excel de nuevo.")
        return redirect(url_for("liquidador2.subir"))

    # Sesiones de reparación no tienen rows_mo/rows_mat → redirigir al padre
    if "rows_mo" not in s:
        parent_sid = s.get("_parent_sid")
        if parent_sid and _sesiones.get(parent_sid):
            return redirect(url_for("liquidador2.verificar", sid=parent_sid))
        flash("Esta sesión es una reparación. Vuelve a la sesión original.")
        return redirect(url_for("liquidador2.subir"))

    try:
        c = _sgs()
    except Exception as ex:
        flash(f"Error conectando a SGS: {ex}")
        return render_template("liquidador2/subir.html")

    checks_mo = []
    _df_cache = {}   # caché por-request: evita repetir search_read por mismo df_id
    for row in s["rows_mo"]:
        try:
            ck = verificar_fila_mo(c, row, _cache=_df_cache)
        except Exception as ex:
            ck = {"row": row, "ok": False, "errores": [str(ex)], "advertencias": []}
        checks_mo.append(ck)

    # Post-pass delta repetible_sumable: si otras filas del Excel apuntan al
    # mismo (pid, código) y ya están liquidadas, el advance de SGS les pertenece
    # a ellas — no descuenta el delta de esta fila.
    # Caso real: 1AL5 y 5AL48 → PRO-055851/QP1129. advance=600 es de 1AL5
    # (fila ya liquidada); el delta de 5AL48 es sus 2000 ud completas, no 1400.
    for ck in checks_mo:
        if not ck.get("repetible_sumable") or not ck.get("pid"):
            continue
        _key = (ck["pid"], str(ck.get("row", {}).get("codigo", "")).strip().upper())
        _cubierto = 0.0
        for o in checks_mo:
            if o is ck or not o.get("pid"):
                continue
            _ko = (o["pid"], str(o.get("row", {}).get("codigo", "")).strip().upper())
            if _ko == _key and o.get("ya_liquidada"):
                _cubierto += float(o.get("row", {}).get("cantidad") or 0)
        if _cubierto <= 0:
            continue
        _adv        = float((ck.get("act_existente") or {}).get("advance") or 0)
        _atribuible = max(0.0, _adv - _cubierto)
        _cant       = float(ck.get("row", {}).get("cantidad") or 0)
        _delta_new  = round(_cant - _atribuible, 4)
        if _delta_new > 0.001 and abs(_delta_new - float(ck.get("delta_qty") or 0)) > 0.001:
            log.info("[L2] rep_sumable %s: delta %s → %s (advance=%s cubierto "
                     "por filas hermanas=%s)", _key, ck.get("delta_qty"),
                     _delta_new, _adv, _cubierto)
            ck["delta_qty"] = _delta_new
            ck["errores"].append(
                f"Delta ajustado a {_delta_new:.0f} ud: el avance SGS "
                f"({_adv:.0f}) pertenece a otras filas del Excel ya liquidadas "
                f"en este mismo PRO ({_cubierto:.0f} ud).")

    checks_mat = []
    if s["rows_mat"]:
        nodos_unicos = {}
        odm_global   = s["rows_mo"][0].get("_odm","") if s["rows_mo"] else ""

        # ── Alcance de planilla: omitir nodos MAT ajenos a la hoja MO ────────
        # El balance_integrado de familia completa trae TODOS los nodos del LCL
        # (caso real 6300996351: 1287 filas MAT / 78 nodos con la planilla
        # apuntando solo a 54AL66). Verificarlos todos tarda >10 min (78 pids
        # de warm-up + verificación API por nodo) y además deja seleccionables
        # materiales de nodos ajenos a la planilla — un "Seleccionar todas"
        # liquidaría media familia. Si TODAS las filas MO traen nodo
        # reconocible, el alcance MAT = nodos que se solapan (rango-aware) con
        # los nodos MO; el resto se omite con aviso. Filas con PRO explícito
        # del módulo "nuevo" se respetan siempre.
        rows_mat_verif = s["rows_mat"]
        _mo_nodos = {str(r.get("pro") or "").strip().upper()
                     for r in (s["rows_mo"] or [])}
        _mo_nodos.discard("")
        if _mo_nodos and all(_es_nodo_planilla(n) for n in _mo_nodos):
            _mo_rangos = [(n, _rango_nodo_simple(n)) for n in _mo_nodos]
            _dentro, _fuera_cnt, _nodos_fuera = [], 0, set()
            for mat in s["rows_mat"]:
                if mat.get("pro_code") or _nodo_en_alcance_mo(mat.get("nodo"), _mo_rangos):
                    _dentro.append(mat)
                else:
                    _fuera_cnt += 1
                    _nodos_fuera.add(str(mat.get("nodo") or ""))
            if _dentro and _fuera_cnt:
                rows_mat_verif = _dentro
                _lista_nf = ", ".join(sorted(_nodos_fuera)[:15])
                if len(_nodos_fuera) > 15:
                    _lista_nf += f", … (+{len(_nodos_fuera)-15})"
                flash(f"⚠ Excel de familia completa: {_fuera_cnt} fila(s) MAT de "
                      f"{len(_nodos_fuera)} nodo(s) fuera de la planilla MO "
                      f"({', '.join(sorted(_mo_nodos))}) omitidas de la verificación: "
                      f"{_lista_nf}. Para liquidarlas, sube la planilla de su tramo.")
                log.info("[L2 ALCANCE] MAT filtrado por planilla MO %s: %d filas "
                         "dentro / %d fuera (%d nodos fuera)",
                         sorted(_mo_nodos), len(_dentro), _fuera_cnt,
                         len(_nodos_fuera))

        for mat in rows_mat_verif:
            nodo     = mat["nodo"]
            pro_code = mat.get("pro_code", "")
            # Clave compuesta: mismo nodo pero PRO distinto (proyectos multi-padre) → entradas separadas
            _nodo_key = f"{nodo}|{pro_code}" if pro_code else nodo
            if _nodo_key not in nodos_unicos:
                pid = None
                # Vía rápida: PRO code explícito embebido en el Excel por el módulo "nuevo".
                # El módulo "nuevo" ya resolvió el nodo → PRO; Liquidador 2 no necesita
                # reinventar el matching por nombre cuando esa información ya viene dada.
                if pro_code:
                    try:
                        p = c.buscar_por_codigo(pro_code)
                        pid = p["id"] if p else None
                        if pid:
                            log.info(f"[L2] pid resuelto directo por PRO {pro_code} → {pid}")
                    except Exception as ex:
                        log.warning(f"[L2] buscar_por_codigo({pro_code}) falló, usando fuzzy: {ex}")
                if not pid:
                    # Preferir _odm del MAT sobre el global de MO.
                    odm_mat = mat.get("_odm") or odm_global
                    pid = _resolver_pid_nodo(c, nodo, s["rows_mo"], checks_mo, odm_mat)
                    if not pid and odm_mat != odm_global and odm_global:
                        pid = _resolver_pid_nodo(c, nodo, s["rows_mo"], checks_mo, odm_global)
                nodos_unicos[_nodo_key] = pid
            # Retrocompatibilidad: registrar también bajo la clave simple de nodo
            if pro_code and nodo not in nodos_unicos:
                nodos_unicos[nodo] = nodos_unicos[_nodo_key]

        # ── Rescate anti-gemelo-vacío (solo nodos resueltos por matching) ──
        # Si el proyecto elegido no contiene NINGÚN código del Excel para ese
        # nodo, buscar un hermano nodo-equivalente que sí los tenga (duplicados
        # de tramo en SGS: el nuevo vacío gana el match y el viejo tiene el BOM).
        _codes_key, _odm_key = {}, {}
        for mat in rows_mat_verif:
            if mat.get("pro_code"):
                continue          # PRO explícito del Excel — respetarlo
            _codes_key.setdefault(mat["nodo"], set()).add(str(mat.get("codigo") or "").strip())
            _odm_key.setdefault(mat["nodo"], mat.get("_odm") or odm_global)
        _avisos_rescate = {}
        _resc_cache = {}
        for _nk, _codes in _codes_key.items():
            _pid1, _aviso = _rescatar_gemelo_vacio(
                c, nodos_unicos.get(_nk), _nk, _odm_key.get(_nk) or "",
                _codes, _resc_cache)
            if _aviso:
                nodos_unicos[_nk]    = _pid1
                _avisos_rescate[_nk] = _aviso

        for mat in rows_mat_verif:
            nodo     = mat["nodo"]
            pro_code = mat.get("pro_code", "")
            _nodo_key = f"{nodo}|{pro_code}" if pro_code else nodo
            pid  = nodos_unicos.get(_nodo_key) or nodos_unicos.get(nodo)
            if pid:
                checks_mat.append({"mat": mat, "pid": pid, "_pendiente": True})
            else:
                checks_mat.append({"mat": mat, "ok": False, "estado": "no_encontrado",
                                   "errores": [f"No se pudo resolver proyecto para nodo {nodo}"],
                                   "advertencias": []})

        pids_unicos        = set(ck["pid"] for ck in checks_mat if ck.get("_pendiente") and ck.get("pid"))

        # Visitar cada proyecto en el browser antes de leer via API.
        # Esto fuerza a Odoo a procesar el recompute queue de campos stored
        # (balance, despachado, devuelto en project.material). Sin este paso,
        # la API puede devolver valores stale si el picking no está aún 'done'.
        if not _obtener_storage_state_activo(timeout_s=1.0):
            flash("⚠ Browser no iniciado — los datos de materiales pueden ser imprecisos. "
                  "Abre el browser en esta misma página y vuelve a verificar para obtener "
                  "valores actualizados de SGS.", "warning")
        _precalentar_pids(list(pids_unicos))

        resultados_por_pid = {}
        for pid in pids_unicos:
            rows_pid = [ck["mat"] for ck in checks_mat if ck.get("pid") == pid]
            try:
                res = verificar_materiales_proyecto(c, pid, rows_pid)
                resultados_por_pid[pid] = {r["mat"]["codigo"]: r for r in res}
            except Exception as ex:
                log.error(f"[L2] Error verificando MAT pid={pid}: {ex}")

        pid_info = {}
        try:
            proyectos = c.search_read(
                "project.dominion", [["id","in",list(pids_unicos)]],
                ["id","code","name"], limit=len(pids_unicos)+10)
            for p in proyectos:
                pid_info[p["id"]] = {"pro_code": p.get("code",""), "pnombre": p.get("name","")}
        except Exception as ex:
            log.warning(f"[L2] No se pudo leer info de proyectos MAT: {ex}")

        checks_mat_final = []
        for ck in checks_mat:
            if ck.get("_pendiente"):
                pid  = ck["pid"]
                cod  = ck["mat"]["codigo"]
                res  = resultados_por_pid.get(pid, {}).get(cod)
                info = pid_info.get(pid, {})
                if res:
                    res["pro_code"] = info.get("pro_code","")
                    res["pnombre"]  = info.get("pnombre","")
                    if not ck["mat"].get("pro_code"):
                        _av = _avisos_rescate.get(ck["mat"].get("nodo"))
                        if _av:
                            res.setdefault("advertencias", []).append(_av)
                    checks_mat_final.append(res)
                else:
                    checks_mat_final.append({
                        "mat": ck["mat"], "ok": False, "estado": "no_encontrado",
                        "pro_code": info.get("pro_code",""), "pnombre": info.get("pnombre",""),
                        "errores": ["No encontrado en project.material"], "advertencias": []})
            else:
                checks_mat_final.append(ck)
        checks_mat = checks_mat_final

    # Anotar liquidabilidad en cada mc para que el template use el mismo criterio que Python.
    for mc in checks_mat:
        mc["liquidable"] = _mat_es_liquidable(mc)

    # ── Nodos recién creados sin equipo: NO bloquear la MO ──
    # El verificador marca ok=False cuando project.team está vacío, pero el flujo
    # de escritura ya sabe crear el líder: crear_y_liquidar_mo llama
    # _asegurar_lider_en_equipo antes de liquidar (browser_client). Si la falta de
    # líder es el ÚNICO error, la fila pasa a liquidable con aviso; el líder sale
    # de la columna del Excel, la distribución (%, IA) o el select manual.
    # Caso real: 7 proyectos con nodos nuevos quedaron con MO sin liquidar porque
    # estas filas no entraban en "Seleccionar todas" ni en la distribución.
    for ck in checks_mo:
        if not (ck.get("lider_faltante") and ck.get("pid")) or ck.get("ya_liquidada"):
            continue
        errs = ck.get("errores") or []
        otros_errores = [e for e in errs if "sin lider" not in str(e).lower()]
        if otros_errores:
            continue  # tiene errores adicionales reales — se mantiene bloqueada
        ck["ok"]      = True
        ck["errores"] = []
        ck.setdefault("advertencias", []).append(
            "Proyecto sin líder en SGS — el líder que asignes se agregará "
            "automáticamente al equipo al liquidar.")
        if not ck.get("partner_nombre"):
            lid_row = str((ck.get("row") or {}).get("lider") or "").strip()
            if lid_row:
                ck["partner_nombre"] = lid_row

    s["checks_mo"]  = checks_mo
    s["checks_mat"] = checks_mat
    s["verificado_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Detectar nodos que fallaron por "no existe en SGS" en el verificador ──
    _nodos_err = []
    _ticket_err = ""
    for ck in checks_mo:
        if not ck.get("ok"):
            for err in (ck.get("errores") or []):
                if "no existe en SGS" in err:
                    nv = str((ck.get("row") or {}).get("pro") or "").strip()
                    if nv and nv not in _nodos_err:
                        _nodos_err.append(nv)
                    if not _ticket_err:
                        _ticket_err = str((ck.get("row") or {}).get("_odm") or "").strip()
                    break

    if _nodos_err:
        nf_base   = s.get("nodos_faltantes") or {}
        nf_merged = list(set(nf_base.get("nodos_faltantes", []) + _nodos_err))
        parent_c  = nf_base.get("parent_pro_code", "")
        ticket_c  = nf_base.get("ticket", "") or _ticket_err

        # Intentar derivar el parent PRO desde un check MO exitoso
        if not parent_c:
            for ck in checks_mo:
                if ck.get("ok") and ck.get("pid"):
                    try:
                        pr = _sgs().search_read(
                            "project.dominion", [["id", "=", ck["pid"]]],
                            ["parent_id"], limit=1)
                        if pr and pr[0].get("parent_id"):
                            pid_p = pr[0]["parent_id"]
                            pid_p = pid_p[0] if isinstance(pid_p, list) else pid_p
                            pr2   = _sgs().search_read(
                                "project.dominion", [["id", "=", pid_p]],
                                ["code"], limit=1)
                            if pr2:
                                parent_c = pr2[0].get("code", "")
                    except Exception:
                        pass
                    if parent_c:
                        break

        s["nodos_faltantes"] = {
            **nf_base,
            "nodos_faltantes":  nf_merged,
            "parent_pro_code":  parent_c,
            "ticket":           ticket_c,
        }

    def _resumen(checks):
        return {"ok":    sum(1 for c in checks if c.get("ok")),
                "error": sum(1 for c in checks if not c.get("ok")),
                "ya_liquidado": sum(1 for c in checks
                                    if c.get("estado") == "ya_liquidado" or c.get("ya_liquidada")),
                "total": len(checks)}

    # ── Calculo monetario MO usando tabla de precios MDO&MAT (misma que julian) ──
    mo_calculo = {"total": 0.0, "sin_precio": [], "lineas": []}
    valor_por_idx = {}
    try:
        from modulos.julian.parser_mdo_mat import get_precios
        precios_mdo = get_precios()
        _sin_precio_set = set()
        for i, ck in enumerate(checks_mo):
            row = ck.get("row", {})
            cod = str(row.get("codigo", "")).strip().upper()
            if not cod:
                continue
            cant = float(row.get("cantidad") or 0)
            if cant <= 0:
                continue
            pi     = precios_mdo.get(cod) or {}
            precio = float(pi.get("precio") or 0.0)
            valor  = precio * cant
            mo_calculo["total"] += valor
            valor_por_idx[i] = valor
            mo_calculo["lineas"].append({
                "cod":   cod,
                "cant":  cant,
                "precio": precio,
                "valor": valor,
                "pro":   str(row.get("pro") or row.get("_odm") or ""),
                "ya_liq": ck.get("ya_liquidada", False) and not ck.get("cant_no_coincide", False),
                "cant_no_coincide": ck.get("cant_no_coincide", False),
                "repetible_sumable": ck.get("repetible_sumable", False),
                "delta_qty": ck.get("delta_qty", 0),
            })
            if precio == 0.0:
                _sin_precio_set.add(cod)
        mo_calculo["sin_precio"] = sorted(_sin_precio_set)
    except Exception as ex:
        log.warning(f"[L2] calculo_mo_valor: {ex}")
        mo_calculo["sin_precio"] = ["(error cargando precios MDO&MAT)"]

    # ── Datos para distribución greedy en JS ──────────────────────────────────
    todos_lideres_cedula = {}   # nombre → cedula (C\d+), para lookup JS

    def _normalizar_cedula(ced: str) -> str:
        """Asegura que la cédula tenga el prefijo C."""
        ced = ced.strip()
        if not ced:
            return ""
        if ced.isdigit():
            return "C" + ced
        if ced.upper().startswith("C") and ced[1:].isdigit():
            return ced.upper()
        return ced

    mo_items_js = []
    for i, ck in enumerate(checks_mo):
        # Filas con proyecto encontrado pero sin líder en project.team → seleccionables
        # con asignación manual obligatoria antes de liquidar.
        lider_req = (not ck.get("ok")) and ck.get("lider_faltante") and ck.get("pid")
        lideres_ck = ck.get("lideres") or []
        # Recolectar líderes de TODAS las filas (también ya_liquidadas y
        # repetibles sumables) — los selects de Modo Repetición los necesitan.
        for l in lideres_ck:
            todos_lideres_cedula[l["nombre"]] = _normalizar_cedula(l.get("cedula", ""))
        if not lideres_ck and ck.get("partner_nombre"):
            todos_lideres_cedula.setdefault(ck["partner_nombre"], "")
        if (not ck.get("ok") or ck.get("ya_liquidada")) and not lider_req:
            continue
        row = ck.get("row", {})
        default_p   = lideres_ck[0]["nombre"] if lideres_ck else ck.get("partner_nombre", "")
        default_ced = _normalizar_cedula(lideres_ck[0].get("cedula", "")) if lideres_ck else ""
        mo_items_js.append({
            "idx":             i,
            "fila":            row.get("fila"),
            "cod":             str(row.get("codigo", "")),
            "pro":             str(row.get("pro") or row.get("_odm") or ""),
            "cant":            float(row.get("cantidad") or 1),
            "valor":           valor_por_idx.get(i, 0.0),
            "lideres":         [l["nombre"] for l in lideres_ck],
            "default_partner": default_p,
            "default_cedula":  default_ced,
            "lider_req":       bool(lider_req),
        })

    # Fusionar líderes de la distribución explícita del Excel (proyectos programados).
    # Estos líderes se añaden al panel de distribución aunque no estén en bandejas SGS.
    distribucion_excel = s.get("distribucion_excel", [])
    for d in distribucion_excel:
        # El Excel puede traer el nombre invertido («NOMBRE1 NOMBRE2 APELLIDO1 APELLIDO2»)
        # o sin cédula — normalizar contra los líderes ya conocidos de SGS.
        if d["nombre"] not in todos_lideres_cedula:
            m = _fuzzy_match_lider(d["nombre"], todos_lideres_cedula)
            if m:
                d["nombre"] = m[0]
                if not d.get("cedula"):
                    d["cedula"] = m[1] or ""
        if d.get("cedula") or d["nombre"] not in todos_lideres_cedula:
            todos_lideres_cedula[d["nombre"]] = d.get("cedula", "")

    todos_lideres = sorted(todos_lideres_cedula.keys())

    # ── Bloqueo si hay materiales no encontrados con cantidad real ────────────
    _mat_no_enc = [
        ck for ck in checks_mat
        if ck.get("estado") == "no_encontrado"
        and (ck.get("mat", {}).get("sgs_neto_excel") or 0) > 0
    ]
    bloqueo_mat = len(_mat_no_enc)

    veredicto_bloqueo_mat = None
    if bloqueo_mat:
        try:
            veredicto_bloqueo_mat = _minimax_veredicto_bloqueo_mat(
                _mat_no_enc, s.get("odm_global", "")
            )
        except Exception as ex:
            log.warning("veredicto_bloqueo_mat falló, se usa texto estatico: %s", ex)

    return render_template("liquidador2/verificacion.html",
        sid=sid, s=s,
        checks_mo=checks_mo, checks_mat=checks_mat,
        resumen_mo=_resumen(checks_mo), resumen_mat=_resumen(checks_mat),
        mo_lider_grupos=_grupos_lideres_mo(checks_mo),
        mo_calculo=mo_calculo,
        mo_items_js=mo_items_js, todos_lideres=todos_lideres,
        todos_lideres_cedula=todos_lideres_cedula,
        distribucion_excel=distribucion_excel,
        ia_nodo_lider=s.get("ia_nodo_lider", {}),
        ia_resumen=s.get("ia_resumen", []),
        nodos_faltantes=_asegurar_lcl_nf(s),
        bloqueo_mat=bloqueo_mat,
        mat_no_encontrados=_mat_no_enc,
        veredicto_bloqueo_mat=veredicto_bloqueo_mat)


@liquidador2.route("/aprobar/<sid>", methods=["GET", "POST"])
def aprobar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))

    if request.method == "POST":
        indices_mo  = set(int(i) for i in request.form.getlist("idx_mo")  if i.isdigit())
        indices_mat = set(int(i) for i in request.form.getlist("idx_mat") if i.isdigit())

        if not indices_mo and not indices_mat:
            flash("Selecciona al menos 1 item para liquidar.")
            return redirect(url_for("liquidador2.aprobar", sid=sid))

        checks_mat      = s.get("checks_mat", [])
        indices_mat_seg = {i for i in indices_mat
                           if i < len(checks_mat) and _mat_es_liquidable(checks_mat[i])}
        bloqueados = len(indices_mat) - len(indices_mat_seg)

        s["indices_mo_aprobados"]  = indices_mo
        s["indices_mat_aprobados"] = indices_mat_seg
        s["aprobado_at"]           = datetime.now().strftime("%Y-%m-%d %H:%M")

        if bloqueados:
            flash(f"El guardián de cantidades bloqueó {bloqueados} MAT no liquidable(s) del plan.")
        flash(f"Aprobado: {len(indices_mo)} MO + {len(indices_mat_seg)} MAT seleccionados.")
        return redirect(url_for("liquidador2.plan", sid=sid))

    return render_template("liquidador2/aprobar.html", sid=sid, s=s,
        checks_mo=s.get("checks_mo",[]), checks_mat=s.get("checks_mat",[]))


@liquidador2.route("/auditoria_lideres/<sid>")
def auditoria_lideres(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))
    return render_template("liquidador2/auditoria_lideres.html", sid=sid, s=s,
                           checks_mat=s.get("checks_mat",[]))


@liquidador2.route("/plan/<sid>")
def plan(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))
    return render_template("liquidador2/plan.html", sid=sid, s=s)


@liquidador2.route("/probar/<sid>")
def probar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))
    items_mat = [mc for mc in s.get("checks_mat",[]) if _mat_es_liquidable(mc)]
    items_mo  = [ck for ck in s.get("checks_mo",[])
                 if ck.get("ok") and not ck.get("ya_liquidada")
                 and ck.get("act_id") and ck.get("pid")]
    return render_template("liquidador2/probar.html", sid=sid, s=s,
                           items_mat=items_mat, items_mo=items_mo)


@liquidador2.route("/probar/<sid>/inspect", methods=["POST"])
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
        return {"ok": False, "error": str(ex)}, 500


@liquidador2.route("/probar/<sid>/run", methods=["POST"])
def probar_run(sid):
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
            result = pw.liquidar_material(pm_id=pm_id, cantidad=cantidad,
                                          partner_nombre=partner_nombre,
                                          pro_code=pro_code, dry_run=dry_run)
        return result
    except Exception as ex:
        return {"ok": False, "error": str(ex)}, 500


@liquidador2.route("/probar/<sid>/run_mo", methods=["POST"])
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
            result = pw.liquidar_actividad_mo(act_id=act_id, pid=pid, cantidad=cantidad,
                                              act_code=act_code, pro_code=pro_code,
                                              dry_run=dry_run)
        return result
    except Exception as ex:
        return {"ok": False, "error": str(ex)}, 500


@liquidador2.route("/reintentar_mo/<sid>", methods=["POST"])
def reintentar_mo(sid):
    """Relanza la liquidación MO (todos los items fallados) en sesión nueva con pantalla de progreso."""
    s = _sesiones.get(sid)
    if not s:
        flash("Sesión no encontrada.")
        return redirect(url_for("liquidador2.subir"))
    if _rechazar_si_liquidacion_activa():
        return redirect(url_for("liquidador2.resultado", sid=sid))
    job = _trabajos.get(sid) or {}
    items_retry = job.get("mo_retry_pendientes", [])
    if not items_retry:
        flash("No hay items MO pendientes de reintentar.", "info")
        return redirect(url_for("liquidador2.resultado", sid=sid))
    storage_state = _storage_state_escritura()   # SGO7: solo con la cuenta propia
    if not storage_state:
        flash("Sin sesión activa en el browser. Inicia sesión primero.")
        return redirect(url_for("liquidador2.resultado", sid=sid))

    rep_sid = sid + "_mor_" + str(uuid.uuid4())[:8]
    _sesiones[rep_sid] = {
        "filename":              s.get("filename","") + " [RETRY MO]",
        "resultados_liquidacion": None,
        "debug_liquidacion":      None,
        "fecha_liquidacion_iso":  s.get("fecha_liquidacion_iso", ""),
        "pdl_detectado":          s.get("pdl_detectado", ""),
        "_parent_sid":            sid,
    }
    _trabajos[rep_sid] = {
        "estado": "corriendo", "items_mo": [
            {"codigo": it["act_code"], "tipo": "MO",
             "worker_id": 0, "estado": "pendiente", "error": None}
            for it in items_retry
        ],
        "items_mat": [], "resultados": None, "error": None,
        "t_inicio": _time_mod.time(),
    }
    threading.Thread(
        target=_ejecutar_liquidacion_bg,
        args=(rep_sid, items_retry, [], storage_state), daemon=True).start()
    flash(f"Reintentando {len(items_retry)} MO(s)…")
    return redirect(url_for("liquidador2.esperar", sid=rep_sid))


@liquidador2.route("/liquidar/<sid>", methods=["POST"])
def liquidar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))

    if _rechazar_si_liquidacion_activa():
        return redirect(url_for("liquidador2.verificar", sid=sid))

    # Fecha obligatoria si hay MO seleccionada (va en cada actividad) o si el
    # cierre es fecha + estado. En SOLO ESTADO (plantilla MAT masiva) la fecha
    # no se escribe en ningún lado, así que no se exige.
    _hay_mo_sel  = any(i.isdigit() for i in request.form.getlist("idx_mo"))
    _solo_estado = _cierre_modo(s) == CIERRE_MODO_SOLO_ESTADO
    if not s.get("fecha_liquidacion_iso") and (_hay_mo_sel or not _solo_estado):
        flash("Falta la fecha de planilla. " + (
              "La mano de obra la necesita en cada actividad: defínela en la verificación."
              if _hay_mo_sel else
              "Defínela en la verificación o cambia el cierre a 'solo estado'."))
        return redirect(url_for("liquidador2.verificar", sid=sid))

    if not sesion_activa():
        _e = _pw_users.get(_usuario_actual() or "") or {}
        if _e.get("registrada") is False:
            flash("No tienes cuenta de SGS registrada en SGO7. Regístrala en SGO7 → Liquidador Automático → "
                  "Mi cuenta SGS y vuelve a intentar: cada persona liquida con su propia cuenta.")
        elif _e.get("error"):
            flash(f"No se pudo abrir tu sesión de SGS: {_e['error']}. Revisa tu cuenta en SGO7 → Liquidador → Mi cuenta SGS.")
        else:
            flash("Tu sesión de SGS se está abriendo con tu cuenta. Espera a que el indicador de arriba quede en verde e intenta de nuevo.")
        return redirect(url_for("liquidador2.verificar", sid=sid))

    # SGO7 trazabilidad: quién lanzó esta liquidación (el hilo de fondo no tiene request)
    s["sgo7_usuario"] = request.headers.get("X-SGO7-User") or s.get("sgo7_usuario")
    s["sgo7_rol"]     = request.headers.get("X-SGO7-Rol") or s.get("sgo7_rol")
    idx_mo  = [int(i) for i in request.form.getlist("idx_mo")  if i.isdigit()]
    idx_mat = [int(i) for i in request.form.getlist("idx_mat") if i.isdigit()]

    if not idx_mo and not idx_mat:
        flash("Selecciona al menos un item para liquidar.")
        return redirect(url_for("liquidador2.verificar", sid=sid))

    checks_mo  = s.get("checks_mo",  [])
    checks_mat = s.get("checks_mat", [])

    try:
        _sgs_for_ced = SGSReadOnlyClient(SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD)
        _sgs_for_ced.autenticar()
    except Exception:
        _sgs_for_ced = None
    omitir_ya_liq = request.form.get("omitir_ya_liquidadas") == "1"
    log.warning("[L2 LIQUIDAR] idx_mo=%s omitir_ya=%r omitir_raw=%r",
                idx_mo, omitir_ya_liq,
                request.form.get("omitir_ya_liquidadas"))
    if omitir_ya_liq:
        log.warning("MODO REPETICIÓN activo: se liquidarán filas ya_liquidadas")
    # Log estado de cada idx recibido para diagnóstico
    for _i in idx_mo:
        if _i < len(checks_mo):
            _ck = checks_mo[_i]
            log.warning("[L2 LIQUIDAR] idx=%d ok=%r ya_liq=%r mes_ant=%r rep_sum=%r delta=%s",
                        _i, _ck.get("ok"), _ck.get("ya_liquidada"),
                        _ck.get("ya_liquidada_mes_anterior"), _ck.get("repetible_sumable"),
                        _ck.get("delta_qty"))
    items_mo, errores_mo_lider = _items_mo_desde_form(
        checks_mo, idx_mo, request.form, sgs_client=_sgs_for_ced,
        omitir_ya_liquidadas=omitir_ya_liq)
    if errores_mo_lider:
        for err in errores_mo_lider:
            flash(err)
        return redirect(url_for("liquidador2.verificar", sid=sid))

    pm_ids_mat = [checks_mat[idx]["pm_id"] for idx in idx_mat
                  if idx < len(checks_mat) and checks_mat[idx].get("pm_id")]
    advance_real_sgs      = {}
    precheck_pendiente_ok = True
    if pm_ids_mat:
        try:
            frescos = _sgs().search_read(
                "project.material", [["id","in",pm_ids_mat]],
                ["id","advance","amount","balance","devuelto"], limit=len(pm_ids_mat)+5)
            dets = _sgs().search_read(
                "project.material.details", [["project_material_id","in",pm_ids_mat]],
                ["id","project_material_id","amount"], limit=1000)
            advance_details = {}
            for d in dets:
                amt = float(d.get("amount") or 0)
                if amt <= 0: continue
                pm_ref    = d.get("project_material_id")
                pm_det_id = pm_ref[0] if isinstance(pm_ref, list) else pm_ref
                advance_details[pm_det_id] = advance_details.get(pm_det_id, 0.0) + amt
            for f in frescos:
                advance_real_sgs[f["id"]] = {
                    "advance":         float(f.get("advance")  or 0),
                    "advance_details": float(advance_details.get(f["id"], 0.0)),
                    "amount":          float(f.get("amount")   or 0),
                    "balance":         float(f.get("balance")  or 0),
                    "devuelto":        float(f.get("devuelto") or 0),
                }
        except Exception as ex:
            log.warning(f"[L2 LIQUIDAR] No se pudo re-verificar SGS: {ex}")
            precheck_pendiente_ok = False

    if pm_ids_mat and not precheck_pendiente_ok:
        flash("No se pudo verificar el Pendiente fresco en SGS. No se liquidan materiales por seguridad.")
        return redirect(url_for("liquidador2.verificar", sid=sid))

    items_mat, omitidos_ya_liq = [], []
    for idx in idx_mat:
        if idx >= len(checks_mat): continue
        mc = checks_mat[idx]
        if not _mat_es_liquidable(mc):
            # Advertir si el material parece liquidable en la UI pero Python lo rechaza.
            if mc.get("estado") in ("ok", "avance_parcial"):
                cod = mc.get("mat", {}).get("codigo", f"idx={idx}")
                sgs_n = mc.get("sgs_neto")
                exc_n = mc.get("mat", {}).get("sgs_neto_excel")
                flash(f"⚠ {cod}: bloqueado (sgs_neto={sgs_n} vs excel={exc_n}). "
                      f"Reverifica el proyecto en SGS y vuelve a cargar el Excel.")
            continue
        if not mc.get("pm_id"):       continue

        pm_id = mc["pm_id"]
        if pm_id not in advance_real_sgs:
            flash(f"⚠ {mc['mat']['codigo']}: no se pudo confirmar Pendiente fresco — omitido.")
            continue

        fresco         = advance_real_sgs[pm_id]
        adv_details    = fresco.get("advance_details", fresco["advance"])
        pendiente      = round(fresco["balance"], 4)
        devuelto       = fresco["devuelto"]
        cantidad_excel, err_cantidad = _mat_cantidad_excel_pendiente(mc, advance_actual=adv_details)

        if err_cantidad:
            flash(f"⚠ {mc['mat']['codigo']}: {err_cantidad} — omitido por el guardián de cantidades.")
            continue
        if cantidad_excel <= 0.001:
            omitidos_ya_liq.append(mc["mat"]["codigo"])
            continue
        if devuelto > 0.001:
            # balance SGS puede estar stale cuando hay devolucion pendiente de recomputo.
            # CRUCE_LIQUIDADOR es fuente de verdad; cantidad_excel = excel_neto - adv_details.
            log.info(f"[L2 LIQUIDAR] pm_id={pm_id} devuelto={devuelto:g} — "
                     f"saltando check balance stale, usando Excel={cantidad_excel:g}")
        elif abs(pendiente - cantidad_excel) > 0.001:
            # balance es campo stored/stale en SGS — no bloquear por diferencia.
            # advance_details (fresco) ya dedujo correctamente cuanto queda.
            log.info(f"[L2 LIQUIDAR] pm_id={pm_id} ({mc['mat']['codigo']}) "
                     f"balance_sgs={pendiente:g} vs excel_pendiente={cantidad_excel:g}: "
                     f"diferencia ignorada (balance puede ser stale). Excel es fuente de verdad.")

        # ── Elección manual del líder desde el formulario ──────────────────────
        idx_s = str(idx)
        modo_override = request.form.get(f"mat_lider_modo_{idx_s}", "").strip()

        if modo_override.startswith("uno::"):
            # Usuario eligió 100% a un líder específico
            lider_elegido = modo_override[5:].strip()
            if not lider_elegido:
                flash(f"⚠ {mc['mat']['codigo']}: líder elegido vacío — omitido.")
                continue
            items_mat.append({
                "tipo": "MAT", "idx": idx, "pm_id": pm_id, "pid": mc.get("pid"),
                "codigo": mc["mat"]["codigo"], "codigo_sgs": mc.get("codigo_sgs") or mc["mat"]["codigo"],
                "cantidad": float(int(round(cantidad_excel))),
                "excel_neto": _f4(mc["mat"].get("sgs_neto_excel")),
                "advance_precheck": adv_details, "balance_precheck": pendiente,
                "pro_code": mc.get("pro_code",""), "partner_nombre": lider_elegido,
                "partner_cedula": _cedula_lider_mat(mc, lider_elegido, _sgs_for_ced),
                "pendiente_api_confirmado": True,
            })
            continue

        if modo_override == "dos":
            # Usuario eligió dividir por porcentaje entre dos líderes
            lider_a = request.form.get(f"mat_lider_a_{idx_s}", "").strip()
            lider_b = request.form.get(f"mat_lider_b_{idx_s}", "").strip()
            pct_a   = max(1, min(99, int(request.form.get(f"mat_lider_pct_{idx_s}") or 50)))
            if not lider_a or not lider_b or lider_a == lider_b:
                flash(f"⚠ {mc['mat']['codigo']}: división inválida (líderes iguales o vacíos) — omitido.")
                continue
            cantidad_excel_int = int(round(cantidad_excel))
            cant_a = int(cantidad_excel_int * pct_a / 100)
            cant_b = cantidad_excel_int - cant_a
            for lider, cant in [(lider_a, cant_a), (lider_b, cant_b)]:
                if cant <= 0:
                    continue
                items_mat.append({
                    "tipo": "MAT", "idx": idx, "pm_id": pm_id, "pid": mc.get("pid"),
                    "codigo": mc["mat"]["codigo"], "codigo_sgs": mc.get("codigo_sgs") or mc["mat"]["codigo"], "cantidad": float(cant),
                    "excel_neto": _f4(mc["mat"].get("sgs_neto_excel")),
                    "advance_precheck": adv_details, "balance_precheck": pendiente,
                    "pro_code": mc.get("pro_code",""), "partner_nombre": lider,
                    "partner_cedula": _cedula_lider_mat(mc, lider, _sgs_for_ced),
                    "pendiente_api_confirmado": True, "multi_lider_split": True,
                })
            continue

        # Sin override de formulario → usar lo del verificador (partner_nombre o partner_splits)
        partner_nombre = mc.get("partner_nombre","")
        partner_splits = mc.get("partner_splits") or {}

        if not partner_nombre and not partner_splits:
            flash(f"⚠ {mc['mat']['codigo']}: líder no determinado desde despacho — omitido.")
            continue

        if partner_splits and not partner_nombre:
            # Multi-líder: un item por líder con cantidad ENTERA proporcional al despacho neto.
            # Los materiales son siempre enteros — nunca fracciones.
            total_gross = sum(v for v in partner_splits.values() if v > 0.001)
            if total_gross <= 0.001:
                flash(f"⚠ {mc['mat']['codigo']}: splits de líder suman 0 — omitido.")
                continue
            cantidad_excel_int = int(round(cantidad_excel))
            acumulado = 0
            # Filtrar primero para saber quién es el último líder válido real
            lideres_validos = [(l, g) for l, g in
                               sorted(partner_splits.items(), key=lambda x: -x[1])
                               if g > 0.001]
            for j, (lider, gross) in enumerate(lideres_validos):
                if j == len(lideres_validos) - 1:
                    cant_lider = cantidad_excel_int - acumulado  # residuo entero
                else:
                    cant_lider = int(cantidad_excel_int * gross / total_gross)  # floor
                if cant_lider <= 0:
                    continue
                acumulado += cant_lider
                items_mat.append({
                    "tipo": "MAT", "idx": idx, "pm_id": pm_id, "pid": mc.get("pid"),
                    "codigo": mc["mat"]["codigo"], "codigo_sgs": mc.get("codigo_sgs") or mc["mat"]["codigo"], "cantidad": float(cant_lider),
                    "excel_neto": _f4(mc["mat"].get("sgs_neto_excel")),
                    "advance_precheck": adv_details, "balance_precheck": pendiente,
                    "pro_code": mc.get("pro_code",""), "partner_nombre": lider,
                    "partner_cedula": _cedula_lider_mat(mc, lider, _sgs_for_ced),
                    "pendiente_api_confirmado": True, "multi_lider_split": True,
                    "ban_justificacion": ", ".join(
                        (mc.get("ban_by_lider") or {}).get(lider, [])[:3]),
                })
            continue

        items_mat.append({
            "tipo": "MAT", "idx": idx, "pm_id": pm_id, "pid": mc.get("pid"),
            "codigo": mc["mat"]["codigo"], "codigo_sgs": mc.get("codigo_sgs") or mc["mat"]["codigo"], "cantidad": cantidad_excel,
            "excel_neto": _f4(mc["mat"].get("sgs_neto_excel")),
            "advance_precheck": adv_details, "balance_precheck": pendiente,
            "pro_code": mc.get("pro_code",""), "partner_nombre": partner_nombre,
            "partner_cedula": _cedula_lider_mat(mc, partner_nombre, _sgs_for_ced),
            "pendiente_api_confirmado": True,
            "ban_justificacion": ", ".join(
                (mc.get("ban_by_lider") or {}).get(partner_nombre,
                                                   mc.get("ban_despacho") or [])[:3]),
        })

    if omitidos_ya_liq:
        flash(f"⚠ {len(omitidos_ya_liq)} material(es) ya liquidado(s) omitido(s): "
              f"{', '.join(omitidos_ya_liq)}")

    if not items_mo and not items_mat:
        flash("Ningún item seleccionado es liquidable.")
        return redirect(url_for("liquidador2.verificar", sid=sid))

    for pos, it in enumerate(items_mat):
        it["job_pos"] = pos

    storage_state = _storage_state_escritura()   # SGO7: solo con la cuenta propia

    if not storage_state:
        flash("Estado de sesión no disponible. Espera unos segundos e intenta de nuevo.")
        return redirect(url_for("liquidador2.verificar", sid=sid))

    _trabajos[sid] = {
        "estado":    "corriendo",
        "items_mo":  [{"codigo": it["act_code"], "tipo": "MO",
                       "pid": it.get("pid"), "pro_code": it.get("pro_code",""),
                       "worker_id": "vis.", "estado": "pendiente", "error": None,
                       "lider": it.get("partner_nombre",""), "cantidad": it.get("cantidad",0)}
                      for it in items_mo],
        "items_mat": [{"codigo": it["codigo"], "tipo": "MAT",
                       "pm_id": it.get("pm_id"), "pid": it.get("pid"),
                       "pro_code": it.get("pro_code",""),
                       "cantidad": it.get("cantidad",0), "lider": it.get("partner_nombre",""),
                       "worker_id": 0, "estado": "pendiente", "error": None}
                      for it in items_mat],
        "resultados": None, "error": None, "t_inicio": _time_mod.time(),
    }

    threading.Thread(
        target=_ejecutar_liquidacion_bg,
        args=(sid, items_mo, items_mat, storage_state),
        daemon=True,
    ).start()

    return redirect(url_for("liquidador2.esperar", sid=sid))


@liquidador2.route("/esperar/<sid>")
def esperar(sid):
    s   = _sesiones.get(sid)
    job = _trabajos.get(sid)
    if not s or not job:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))
    total = len(job.get("items_mo",[])) + len(job.get("items_mat",[]))
    return render_template("liquidador2/esperar_liquidacion.html", sid=sid, s=s, job=job, total=total)


@liquidador2.route("/progreso/<sid>")
def progreso(sid):
    job = _trabajos.get(sid)
    if not job:
        return jsonify({"estado": "no_encontrado"}), 404
    hechos = sum(1 for it in job.get("items_mo",[]) + job.get("items_mat",[])
                 if it["estado"] in ("ok","error"))
    total  = len(job.get("items_mo",[])) + len(job.get("items_mat",[]))
    return jsonify({"estado": job["estado"], "hechos": hechos, "total": total,
                    "items_mo": job.get("items_mo",[]), "items_mat": job.get("items_mat",[]),
                    "error": job.get("error")})


@liquidador2.route("/auditoria/<sid>")
def auditoria(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))

    resultados = s.get("resultados_liquidacion",[])
    if not resultados:
        flash("Sin resultados de liquidacion para auditar.")
        return redirect(url_for("liquidador2.subir"))

    try:
        c = _sgs()
        # Los ítems con error TAMBIÉN se auditan contra SGS: el browser puede
        # reportar timeout aunque la actividad ya haya quedado liquidada (p.ej.
        # modal en solo lectura porque pendiente=0). Antes se marcaban
        # "no_ejecutado" y el usuario veía un fallo falso sin forma de aclararlo.
        for res in resultados:
            try:
                if res["tipo"] == "MO":
                    act_id = res.get("act_id")
                    if act_id and act_id > 0:
                        dom = [["id", "=", act_id]]
                    elif res.get("pid") and res.get("codigo"):
                        dom = [["project_id", "=", res["pid"]],
                               ["activity_code", "=", res["codigo"]]]
                    else:
                        res["auditoria"] = {"estado": "sin_act_id"}
                        continue
                    r = c.search_read("project.activities", dom,
                                      ["advance","balance","amount"], limit=1)
                    if r:
                        a   = r[0]
                        adv = float(a.get("advance") or 0)
                        bal = float(a.get("balance") or 0)
                        if res.get("ok"):
                            coincide = abs(adv - float(res["cantidad"])) < 0.01
                            estado   = "ok" if coincide else "descuadre"
                        else:
                            liquidada = bal <= 0.01 and adv > 0
                            coincide  = liquidada
                            estado    = "liquidado_sgs" if liquidada else "error_confirmado"
                        res["auditoria"] = {
                            "advance": a["advance"], "balance": a["balance"],
                            "amount": a["amount"], "coincide": coincide,
                            "estado": estado}
                    else:
                        res["auditoria"] = {"estado": "no_encontrado"}
                elif res["tipo"] == "MAT":
                    pm_id = res.get("pm_id")
                    if not pm_id:
                        res["auditoria"] = {"estado": "sin_pm_id"}
                        continue
                    r = c.search_read("project.material", [["id","=",pm_id]],
                                      ["advance","balance","amount"], limit=1)
                    if r:
                        a   = r[0]
                        bal = float(a.get("balance") or 0)
                        if res.get("ok"):
                            coincide = abs(bal) < 0.01
                            estado   = "ok" if coincide else "descuadre"
                        else:
                            liquidado = abs(bal) < 0.01
                            coincide  = liquidado
                            estado    = "liquidado_sgs" if liquidado else "error_confirmado"
                        res["auditoria"] = {
                            "advance": a["advance"], "balance": a["balance"],
                            "amount": a["amount"], "coincide": coincide,
                            "estado": estado}
                    else:
                        res["auditoria"] = {"estado": "no_encontrado"}
            except Exception as ex:
                res["auditoria"] = {"estado": "error", "msg": str(ex)}
    except Exception as ex:
        flash(f"Error conectando a SGS para auditoría: {ex}")

    s["resultados_liquidacion"] = resultados
    job = _trabajos.get(sid) or {}
    return render_template("liquidador2/resultado_liquidacion.html", sid=sid, s=s, resultados=resultados, job=job)


@liquidador2.route("/resultado/<sid>")
def resultado(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))
    resultados = s.get("resultados_liquidacion",[])
    if not resultados:
        flash("No hay resultados aún. La liquidación puede seguir en curso.")
        return redirect(url_for("liquidador2.esperar", sid=sid))
    job = _trabajos.get(sid) or {}
    return render_template("liquidador2/resultado_liquidacion.html", sid=sid, s=s, resultados=resultados, job=job)


@liquidador2.route("/auditoria_post/<sid>")
def auditoria_post_estado(sid):
    """Devuelve JSON con el estado actual de las auditorías post-liquidación."""
    job = _trabajos.get(sid)
    if not job:
        return jsonify({"estado": "no_job"}), 404
    return jsonify(job.get("auditoria_post") or {"estado": "no_iniciado"})


def _estado_sesion_usuario():
    """Estado para el panel: la sesión propia del usuario SGO7; sin usuario, la de servicio."""
    u = _usuario_actual()
    if u:
        e = _pw_users.get(u) or {}
        activa = bool(e.get("logueado"))
        return {"alive": activa, "activa": activa,
                "iniciando": (not activa) and (bool(e.get("pendiente")) or (e.get("pw") is not None) or (e.get("auto") and e.get("registrada") is not False and not e.get("error"))),
                "error": e.get("error", ""), "auto": bool(e.get("auto")), "cuenta": e.get("login"),
                "registrada": e.get("registrada"), "usuario": u}
    activa = sesion_activa()
    browser_abierto = _pw_global is not None and _pw_global._page is not None
    return {"alive": activa, "activa": activa,
            "iniciando": (browser_abierto and not activa) or (not browser_abierto and _auto_deseado and not _pw_ultimo_error),
            "error": _pw_ultimo_error, "auto": _auto_deseado, "cuenta": None, "registrada": None, "usuario": None}


@liquidador2.route("/pw_status")
def pw_status():
    return jsonify(_estado_sesion_usuario())


@liquidador2.route("/sesion_estado")
def sesion_estado():
    return jsonify(_estado_sesion_usuario())


@liquidador2.route("/diagnostico", methods=["POST"])
def diagnostico():
    """SGO7: guarda en el servidor el informe técnico que antes se pedía pegar en un chat."""
    d = request.get_json(force=True, silent=True) or {}
    tipo = (d.get("tipo") or "informe")
    sid = re.sub(r"[^A-Za-z0-9-]", "", d.get("sid") or "")[:40]
    texto = (d.get("texto") or "")[:400_000]
    if not texto.strip():
        return jsonify({"ok": False, "error": "informe vacío"}), 400
    ruta = _sgo7_guardar_diagnostico(tipo, sid, texto)
    try:
        import sgo7_registro
        sgo7_registro.registrar(f"liquidador_diagnostico_{re.sub(r'[^a-z0-9_-]', '', tipo.lower())[:30]}",
                                sesion=sid or None, archivo=os.path.basename(ruta) if ruta else None,
                                detalle={"ruta": ruta, "caracteres": len(texto), "resumen": texto[:1500]})
    except Exception:
        pass
    return jsonify({"ok": True, "ruta": ruta})


@liquidador2.route("/browser_ctrl", methods=["POST"])
def browser_ctrl():
    data = request.get_json(force=True, silent=True) or {}
    cmd  = data.get("cmd","")
    if cmd == "restart_browser":
        u = _usuario_actual()
        if u:
            e = _entrada_usuario(u); e["auto"] = True; e["ultimo_uso"] = _time_mod.time(); e["pendiente"] = True
            _browser_commands.put(("user_login", u, "manual"))
        else:
            _browser_commands.put("restart_browser")
        return jsonify({"ok": True, "msg": "Conectando a SGS…"})
    elif cmd == "close_browser":
        u = _usuario_actual()
        _browser_commands.put(("user_close", u, "manual") if u else "close_browser")
        return jsonify({"ok": True, "msg": "Desconectando…"})
    elif cmd == "reverificar":
        _browser_commands.put("reverificar")
        return jsonify({"ok": True, "msg": "Reverificando…"})
    return jsonify({"ok": False, "error": f"Comando desconocido: {cmd}"}), 400


@liquidador2.route("/cancelar/<sid>", methods=["POST"])
def cancelar(sid):
    job = _trabajos.get(sid)
    if job:
        job["cancelado"] = True
        job["estado"]    = "cancelado"
        parciales = []
        for it in job.get("items_mo",[]) + job.get("items_mat",[]):
            estado = it.get("estado")
            if estado == "pendiente":
                it["estado"] = "error"
                it["error"]  = "Cancelado antes de iniciar"
                estado       = "error"
            parciales.append({
                "ok": estado == "ok", "tipo": it.get("tipo","?"),
                "codigo": it.get("codigo","?"), "pm_id": it.get("pm_id"),
                "pid": it.get("pid"), "pro_code": it.get("pro_code",""),
                "cantidad": it.get("cantidad",0), "worker_id": it.get("worker_id","vis."),
                "partner_nombre": it.get("lider",""),
                "error": "" if estado == "ok" else (it.get("error") or "Cancelado por el usuario"),
                "error_tipo": "cancelado" if estado != "ok" else "",
            })
        debug = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(parciales),
            "ok": sum(1 for r in parciales if r.get("ok")),
            "fallidos": sum(1 for r in parciales if not r.get("ok")),
            "clases_error": {},
            "errores_detalle": [],
        }
        s = _sesiones.get(sid)
        if s and not s.get("resultados_liquidacion"):
            s["resultados_liquidacion"] = parciales
            s["debug_liquidacion"]       = debug
    return redirect(url_for("liquidador2.resultado", sid=sid))


@liquidador2.route("/cierre_rapido/<sid>", methods=["GET", "POST"])
def cierre_rapido(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))

    fecha_raw = request.values.get("fecha","")
    pdl_raw   = request.values.get("pdl","")
    modo_raw  = request.values.get("modo","")   # vacío → modo de la sesión

    if request.method == "POST":
        seleccion = [int(pid) for pid in request.form.getlist("pid") if str(pid).isdigit()]
        res = _aplicar_cierre_rapido(sid, pids_objetivo=seleccion or None,
                                     fecha_raw=fecha_raw, pdl_raw=pdl_raw, modo_raw=modo_raw)
        _meta = res.get("meta") or {}
        if res.get("hechos"):
            flash(f"Cierre rapido aplicado a {res['hechos']} de {res['objetivos']} proyecto(s)"
                  + (" — solo estado, fechas y PDL intactos." if _meta.get("solo_estado") else "."))
        elif res.get("objetivos") == 0 and not res.get("errores"):
            flash("No hay proyectos pendientes elegibles.")
        if res.get("errores"):
            flash("Errores en cierre rapido: " + " | ".join(res["errores"][:3]))
        return redirect(url_for("liquidador2.cierre_rapido", sid=sid,
                                fecha=_meta.get("fecha_input") or fecha_raw,
                                pdl=_meta.get("pdl_esperado") or pdl_raw,
                                modo=_meta.get("modo") or modo_raw))

    try:
        rows, meta = _armar_panel_cierre_rapido(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw,
                                                modo_raw=modo_raw)
    except Exception as ex:
        flash(f"No se pudo consultar SGS para el panel de cierre rapido: {ex}")
        rows, meta = [], _resolver_objetivos_cierre(sid, fecha_raw=fecha_raw, pdl_raw=pdl_raw,
                                                    modo_raw=modo_raw)
        meta.update({"total":0,"elegibles":0,"pendientes":0,"listos":0,"referencia":0})

    return render_template("liquidador2/cierre_rapido.html", sid=sid, s=s, rows=rows, meta=meta)


@liquidador2.route("/post_liquidacion_cerrar/<sid>", methods=["POST"])
def post_liquidacion_cerrar(sid):
    flash("Se abrió el panel de cierre rapido para revisar primero fecha, PDL y estado.")
    return redirect(url_for("liquidador2.cierre_rapido", sid=sid))


@liquidador2.route("/panel_pdl/<sid>", methods=["GET", "POST"])
def panel_pdl(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))

    pdl_global = request.values.get("pdl_global", "").strip()

    if request.method == "POST":
        storage_state = _storage_state_escritura()   # SGO7: solo con la cuenta propia
        if not storage_state:
            flash("Sin sesión activa en el browser. Inicia sesión primero desde el menú principal.")
            return redirect(url_for("liquidador2.panel_pdl", sid=sid, pdl_global=pdl_global))

        pids_sel    = [int(p) for p in request.form.getlist("pid") if str(p).isdigit()]
        pdl_por_pid = {}
        for key, val in request.form.items():
            if key.startswith("pdl_"):
                try:
                    _pid = int(key[4:])
                    _val = str(val or "").strip()
                    if _val:
                        pdl_por_pid[_pid] = _val
                except ValueError:
                    pass

        if not pids_sel:
            flash("Selecciona al menos un proyecto para parchear.", "info")
            return redirect(url_for("liquidador2.panel_pdl", sid=sid, pdl_global=pdl_global))

        if pdl_global:
            s["pdl_detectado"] = pdl_global

        rows_panel, _ = _armar_panel_pdl(sid, pdl_global=pdl_global)
        por_pid_panel = {r["pid"]: r for r in rows_panel}

        pw = PlaywrightClient(headless=False, slow_mo=100)
        resultados_pdl = []
        try:
            pw.start(storage_state=storage_state)
            for pid in pids_sel:
                pdl_target = pdl_por_pid.get(pid) or (por_pid_panel.get(pid) or {}).get("pdl_objetivo", "")
                if not pdl_target:
                    resultados_pdl.append({"pid": pid, "ok": False, "error": "PDL objetivo vacío"})
                    continue
                pro_code = (por_pid_panel.get(pid) or {}).get("pro_code", "")
                try:
                    res = pw.parchear_pdl_proyecto(pid=pid, pdl=pdl_target, pro_code=pro_code)
                except Exception as ex:
                    res = {"pid": pid, "ok": False, "error": str(ex)}
                resultados_pdl.append(res)
        finally:
            try:
                pw.close()
            except Exception:
                pass

        ok_count  = sum(1 for r in resultados_pdl if r.get("ok"))
        err_count = len(resultados_pdl) - ok_count
        s.setdefault("resultados_pdl", []).extend(resultados_pdl)
        flash(f"PDL parcheado: {ok_count} OK, {err_count} con error.")
        return redirect(url_for("liquidador2.panel_pdl", sid=sid, pdl_global=pdl_global))

    try:
        rows, meta = _armar_panel_pdl(sid, pdl_global=pdl_global)
    except Exception as ex:
        flash(f"No se pudo consultar SGS: {ex}")
        rows, meta = [], {"total": 0, "con_pdl": 0, "sin_pdl": 0, "pdl_global": pdl_global}

    return render_template("liquidador2/panel_pdl.html", sid=sid, s=s, rows=rows, meta=meta)


@liquidador2.route("/reparar/<sid>", methods=["POST"])
def reparar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))

    if _rechazar_si_liquidacion_activa():
        return redirect(url_for("liquidador2.resultado", sid=sid))

    resultados = s.get("resultados_liquidacion",[])
    indices    = [int(i) for i in request.form.getlist("idx_reparar") if i.isdigit()]
    if not indices:
        flash("Selecciona al menos 1 item para reintentar.")
        return redirect(url_for("liquidador2.resultado", sid=sid))

    storage_state = _storage_state_escritura()   # SGO7: solo con la cuenta propia
    if not storage_state:
        flash("Sin sesión activa en el browser. Inicia sesión primero.")
        return redirect(url_for("liquidador2.resultado", sid=sid))

    items_mat = []
    for i in indices:
        if i >= len(resultados): continue
        r = resultados[i]
        if r.get("ok") or r.get("tipo") != "MAT": continue
        if not r.get("pm_id"):                     continue
        items_mat.append({
            "tipo": "MAT", "idx": i, "pm_id": r["pm_id"], "pid": r.get("pid"),
            "codigo": r.get("codigo",""), "codigo_sgs": r.get("codigo_sgs") or r.get("codigo",""),
            "cantidad": float(r.get("cantidad",0)),
            "excel_neto": float(r.get("excel_neto") or r.get("cantidad") or 0),
            "pro_code": r.get("pro_code",""), "partner_nombre": r.get("partner_nombre",""),
            "partner_cedula": r.get("partner_cedula",""),
            "pendiente_api_confirmado": True,
        })

    if not items_mat:
        flash("No hay materiales MAT reintentables entre los seleccionados.")
        return redirect(url_for("liquidador2.resultado", sid=sid))

    rep_sid = sid + "_rep_" + str(uuid.uuid4())[:8]
    _sesiones[rep_sid] = {
        "filename":              s.get("filename","") + " [REPARACION]",
        "resultados_liquidacion": None,
        "debug_liquidacion":      None,
        "fecha_liquidacion_iso":  s.get("fecha_liquidacion_iso", ""),
        "pdl_detectado":          s.get("pdl_detectado", ""),
        "_parent_sid":            sid,
    }
    _trabajos[rep_sid] = {
        "estado": "corriendo", "items_mo": [],
        "items_mat": [{"codigo": it["codigo"], "tipo": "MAT",
                       "worker_id": 0, "estado": "pendiente", "error": None}
                      for it in items_mat],
        "resultados": None, "error": None, "t_inicio": _time_mod.time(),
    }
    threading.Thread(
        target=_ejecutar_liquidacion_bg,
        args=(rep_sid, [], items_mat, storage_state), daemon=True).start()
    flash(f"Reintentando {len(items_mat)} material(es)…")
    return redirect(url_for("liquidador2.esperar", sid=rep_sid))


@liquidador2.route("/reparar_mo/<sid>", methods=["POST"])
def reparar_mo(sid):
    """Relanza la liquidación MO para items seleccionados de mo_retry_pendientes."""
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))

    if _rechazar_si_liquidacion_activa():
        return redirect(url_for("liquidador2.resultado", sid=sid))

    job = _trabajos.get(sid) or {}
    mo_pendientes = job.get("mo_retry_pendientes", [])

    indices = [int(i) for i in request.form.getlist("idx_mo_reparar") if i.isdigit()]
    if not indices:
        flash("Selecciona al menos 1 MO para reintentar.")
        return redirect(url_for("liquidador2.resultado", sid=sid))

    storage_state = _storage_state_escritura()   # SGO7: solo con la cuenta propia
    if not storage_state:
        flash("Sin sesión activa en el browser. Inicia sesión primero.")
        return redirect(url_for("liquidador2.resultado", sid=sid))

    items_mo = [mo_pendientes[i] for i in indices if i < len(mo_pendientes)]
    if not items_mo:
        flash("No hay items MO reintentables entre los seleccionados.")
        return redirect(url_for("liquidador2.resultado", sid=sid))

    rep_sid = sid + "_mop_" + str(uuid.uuid4())[:8]
    _sesiones[rep_sid] = {
        "filename":              s.get("filename","") + " [REPARACION MO]",
        "resultados_liquidacion": None,
        "debug_liquidacion":      None,
        "fecha_liquidacion_iso":  s.get("fecha_liquidacion_iso", ""),
        "pdl_detectado":          s.get("pdl_detectado", ""),
        "_parent_sid":            sid,
    }
    _trabajos[rep_sid] = {
        "estado": "corriendo", "items_mo": [
            {"codigo": it["act_code"], "tipo": "MO",
             "worker_id": 0, "estado": "pendiente", "error": None}
            for it in items_mo
        ],
        "items_mat": [], "resultados": None, "error": None,
        "t_inicio": _time_mod.time(),
    }
    threading.Thread(
        target=_ejecutar_liquidacion_bg,
        args=(rep_sid, items_mo, [], storage_state), daemon=True).start()
    flash(f"Reintentando {len(items_mo)} MO(s)…")
    return redirect(url_for("liquidador2.esperar", sid=rep_sid))


@liquidador2.route("/reparar_todo/<sid>", methods=["POST"])
def reparar_todo(sid):
    """Reintenta items MAT y/o MO seleccionados en un único proceso con pantalla de progreso."""
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))

    if _rechazar_si_liquidacion_activa():
        return redirect(url_for("liquidador2.resultado", sid=sid))

    storage_state = _storage_state_escritura()   # SGO7: solo con la cuenta propia
    if not storage_state:
        flash("Sin sesión activa en el browser. Inicia sesión primero.")
        return redirect(url_for("liquidador2.resultado", sid=sid))

    # ── MAT items ──
    resultados = s.get("resultados_liquidacion", [])
    idx_mat = [int(i) for i in request.form.getlist("idx_reparar") if i.isdigit()]
    items_mat = []
    for i in idx_mat:
        if i >= len(resultados): continue
        r = resultados[i]
        if r.get("ok") or r.get("tipo") != "MAT" or not r.get("pm_id"): continue
        items_mat.append({
            "tipo": "MAT", "idx": i, "pm_id": r["pm_id"], "pid": r.get("pid"),
            "codigo": r.get("codigo",""), "codigo_sgs": r.get("codigo_sgs") or r.get("codigo",""),
            "cantidad": float(r.get("cantidad",0)),
            "excel_neto": float(r.get("excel_neto") or r.get("cantidad") or 0),
            "pro_code": r.get("pro_code",""), "partner_nombre": r.get("partner_nombre",""),
            "partner_cedula": r.get("partner_cedula",""),
            "pendiente_api_confirmado": True,
        })

    # ── MO items ──
    job = _trabajos.get(sid) or {}
    mo_pendientes = job.get("mo_retry_pendientes", [])
    idx_mo = [int(i) for i in request.form.getlist("idx_mo_reparar") if i.isdigit()]
    items_mo = [mo_pendientes[i] for i in idx_mo if i < len(mo_pendientes)]

    if not items_mat and not items_mo:
        flash("Selecciona al menos 1 item para reintentar.")
        return redirect(url_for("liquidador2.resultado", sid=sid))

    label_parts = []
    if items_mat: label_parts.append(f"{len(items_mat)} MAT")
    if items_mo:  label_parts.append(f"{len(items_mo)} MO")
    label = " + ".join(label_parts)

    rep_sid = sid + "_tot_" + str(uuid.uuid4())[:8]
    _sesiones[rep_sid] = {
        "filename":              s.get("filename","") + f" [REPARACION {label}]",
        "resultados_liquidacion": None,
        "debug_liquidacion":      None,
        "fecha_liquidacion_iso":  s.get("fecha_liquidacion_iso", ""),
        "pdl_detectado":          s.get("pdl_detectado", ""),
        "_parent_sid":            sid,
    }
    _trabajos[rep_sid] = {
        "estado": "corriendo",
        "items_mo":  [{"codigo": it["act_code"], "tipo": "MO",
                       "worker_id": 0, "estado": "pendiente", "error": None}
                      for it in items_mo],
        "items_mat": [{"codigo": it["codigo"], "tipo": "MAT",
                       "worker_id": 0, "estado": "pendiente", "error": None}
                      for it in items_mat],
        "resultados": None, "error": None, "t_inicio": _time_mod.time(),
    }
    threading.Thread(
        target=_ejecutar_liquidacion_bg,
        args=(rep_sid, items_mo, items_mat, storage_state), daemon=True).start()
    flash(f"Reintentando {label}…")
    return redirect(url_for("liquidador2.esperar", sid=rep_sid))


@liquidador2.route("/cerrar_navegador", methods=["POST"])
def cerrar_navegador():
    _browser_commands.put("close_browser")
    flash("Señal de cierre enviada al navegador.")
    return redirect(request.referrer or url_for("liquidador2.subir"))


# ── Análisis / Pruebas Playwright ────────────────────────────────────────────

_analisis_job = {
    "running":    False,
    "logs":       [],
    "phase":      "idle",   # idle | running | paused | done | error
    "pw":         None,
    "stop_event": None,
}
_analisis_lock = threading.Lock()

# Config del material de prueba (editable aquí o desde la UI)
_PRUEBA_DEFAULT = {
    "pro_buscar":  "053986",   # PRO-053985 tiene 0 materiales; 300013 vive en PRO-053986 pm_id=523158
    "codigo_mat":  "300013",
    "cantidad":    6.0,
    "partner":     "",
}


def _analisis_log(msg: str):
    with _analisis_lock:
        _analisis_job["logs"].append(msg)
    log.info(f"[L2 ANALISIS] {msg}")


def _analisis_thread(cfg: dict):
    """
    Hilo de prueba.
    modo='lista' → navega a la vista lista del proyecto y pausa.
    modo='form'  → abre el form del material, rellena qty y pausa antes de GUARDAR.
    """
    import time as _t
    stop_ev = threading.Event()
    with _analisis_lock:
        _analisis_job["stop_event"] = stop_ev
        _analisis_job["phase"]      = "running"
        _analisis_job["logs"]       = []
        _analisis_job["pw"]         = None

    modo = cfg.get("modo", "lista")
    pw   = None
    try:
        # 1. SGS lookup
        _analisis_log("Conectando a SGS...")
        c  = SGSReadOnlyClient(SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD)
        c.autenticar()
        _analisis_log(f"SGS uid={c.uid}  db={SGS_DB}")

        _analisis_log(f"Buscando proyecto '{cfg['pro_buscar']}'...")
        proyecto = None
        for termino in [cfg["pro_buscar"], f"PRO-{cfg['pro_buscar']}"]:
            for campo in ["code", "name", "order_number"]:
                try:
                    rows = c.search_read(
                        "project.dominion",
                        [[campo, "ilike", termino]],
                        ["id", "code", "name", "order_number", "nodo"],
                        limit=5)
                    if rows:
                        proyecto = rows[0]; break
                except Exception:
                    pass
            if proyecto:
                break
        if not proyecto:
            raise RuntimeError(f"Proyecto '{cfg['pro_buscar']}' no encontrado en SGS.")

        pid = proyecto["id"]
        _analisis_log(f"Proyecto: id={pid}  code={proyecto.get('code','?')}  nombre={proyecto.get('name','?')}")

        _analisis_log(f"Buscando material {cfg['codigo_mat']} en proyecto id={pid}...")
        materiales = c.search_read(
            "project.material",
            [["project_id", "=", pid]],
            ["id", "product_id", "amount", "balance", "advance", "devuelto"],
            limit=300)

        pm_id = None
        for m in materiales:
            prod   = m.get("product_id") or []
            nombre = prod[1] if isinstance(prod, list) and len(prod) > 1 else str(prod)
            if cfg["codigo_mat"] in nombre.upper():
                pm_id = m["id"]
                _analisis_log(
                    f"pm_id={pm_id}  producto={nombre}  "
                    f"amount={m.get('amount')}  advance={m.get('advance')}  "
                    f"balance={m.get('balance')}  devuelto={m.get('devuelto')}")
                break

        if not pm_id:
            raise RuntimeError(f"Material {cfg['codigo_mat']} no encontrado en proyecto id={pid}.")

        # 2. Abrir browser
        _analisis_log("Abriendo Chromium visible (headless=False, slow_mo=100)...")
        pw = PlaywrightClient(headless=False, slow_mo=100)
        pw.start()
        pw.login()
        with _analisis_lock:
            _analisis_job["pw"] = pw
        _analisis_log("Login OK")

        page = pw._page

        # ── MODO LISTA ────────────────────────────────────────────────────────
        if modo == "modal2":
            lista_url = (f"{SGS_URL_PW}/web#active_id={pid}"
                         f"&model=project.material&view_type=list&cids=1&menu_id=352")
            proyecto_url = (f"{SGS_URL_PW}/web#id={pid}&action=485"
                            f"&model=project.dominion&view_type=form&cids=1&menu_id=352")
            trabajos = [
                {"codigo": "300013", "cantidad": 6.0, "partner": cfg.get("partner") or ""},
                {"codigo": "251378", "cantidad": 6.0, "partner": cfg.get("partner") or ""},
            ]

            def _abrir_lista(page_obj, etiqueta: str):
                _analisis_log(f"{etiqueta}: abriendo proyecto {proyecto.get('code','?')}")
                page_obj.goto(f"{SGS_URL_PW}/web?cids=1", wait_until="domcontentloaded", timeout=20_000)
                _t.sleep(0.8)
                page_obj.goto(proyecto_url, wait_until="domcontentloaded", timeout=35_000)
                page_obj.wait_for_selector("button[name='action_button_material']", timeout=90_000)
                _t.sleep(1.2)
                page_obj.locator("button[name='action_button_material']").first.click()
                page_obj.wait_for_selector(".o_data_row", timeout=90_000)
                _t.sleep(1.5)
                _analisis_log(f"{etiqueta}: lista de materiales cargada ({page_obj.locator('.o_data_row').count()} filas)")

            def _abrir_modal_y_rellenar(page_obj, item: dict, etiqueta: str):
                codigo = item["codigo"]
                cantidad = item["cantidad"]
                partner = item["partner"]
                _analisis_log(f"{etiqueta}: abriendo Consumption detail para {codigo}")
                fila = page_obj.locator(f".o_data_row:has-text('{codigo}')").first
                fila.wait_for(state="visible", timeout=60_000)
                fila.scroll_into_view_if_needed()
                _t.sleep(0.5)
                fila.locator("button[name='action_show_details']").first.click()
                page_obj.wait_for_selector(
                    ".modal:has-text('Consumption detail'), .modal-dialog:has-text('Consumption detail')",
                    timeout=30_000,
                )
                _analisis_log(f"{etiqueta}: modal abierto")

                add_sel = (
                    ".modal [name='advance_project_material_ids'] .o_field_x2many_list_row_add a, "
                    ".modal a:has-text('AGREGAR'), "
                    ".modal a:has-text('Agregar')"
                )
                page_obj.locator(add_sel).first.click()
                _t.sleep(0.8)

                qty_sel = (
                    ".modal [name='advance_project_material_ids'] tr.o_selected_row input[name='amount'], "
                    ".modal tr.o_selected_row input[name='amount'], "
                    ".modal [name='amount'] input, "
                    ".modal input[name='amount']"
                )
                qty = page_obj.locator(qty_sel).last
                qty.wait_for(state="visible", timeout=15_000)
                qty.click(click_count=3)
                qty.fill(_cant_sgs(cantidad))
                _analisis_log(f"{etiqueta}: cantidad {cantidad:g} rellenada")

                partner_sel = (
                    ".modal [name='advance_project_material_ids'] tr.o_selected_row [name='partner_id'] input, "
                    ".modal tr.o_selected_row [name='partner_id'] input, "
                    ".modal [name='partner_id'] input"
                )
                p_input = page_obj.locator(partner_sel).last
                p_input.wait_for(state="visible", timeout=15_000)
                termino = partner.split()[0]
                p_input.click()
                p_input.fill(termino)
                _t.sleep(1.0)
                drop_sel = (
                    ".o_field_many2one_dropdown .o_m2o_dropdown_option, "
                    ".dropdown-item.o_m2o_dropdown_option, "
                    ".ui-autocomplete .ui-menu-item a"
                )
                page_obj.wait_for_selector(drop_sel, timeout=10_000)
                opciones = page_obj.locator(drop_sel).all()
                elegida = None
                for op in opciones:
                    txt = op.inner_text().strip().upper()
                    if termino.upper() in txt:
                        elegida = op
                        break
                if not elegida:
                    raise RuntimeError(f"{etiqueta}: lider '{partner}' no encontrado en dropdown")
                elegida.click()
                _analisis_log(f"{etiqueta}: lider {partner} seleccionado")

                shot_name = f"analisis_modal_{codigo}.png"
                shot_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..", "..", "logs", shot_name,
                )
                page_obj.screenshot(path=os.path.normpath(shot_path), full_page=True)
                _analisis_log(f"{etiqueta}: screenshot /liquidador2/log_img/{shot_name}")

            _analisis_log("Modo modal2: dos pestanas, dos Consumption detail, sin GUARDAR")
            _analisis_log(f"URL esperada de lista: {lista_url}")
            state = pw._page.context.storage_state()
            ctx_tabs = pw._browser.new_context(
                storage_state=state,
                viewport={"width": 1400, "height": 900},
            )
            page_a = ctx_tabs.new_page()
            page_b = ctx_tabs.new_page()
            for pg in (page_a, page_b):
                pg.set_default_timeout(120_000)

            _abrir_lista(page_a, "Pestana 1")
            _abrir_lista(page_b, "Pestana 2")
            _abrir_modal_y_rellenar(page_a, trabajos[0], "Pestana 1")
            _abrir_modal_y_rellenar(page_b, trabajos[1], "Pestana 2")

            _analisis_log("NO se ha hecho click en GUARDAR en ninguna pestana.")
            _analisis_log("PAUSA - revisa Chromium. Pulsa 'Cerrar browser' cuando termines.")
            with _analisis_lock:
                _analisis_job["phase"] = "paused"
            stop_ev.wait(timeout=600)

        if modo in ("lista", "lista2"):
            lista_url = (f"{SGS_URL_PW}/web#active_id={pid}"
                         f"&model=project.material&view_type=list&cids=1&menu_id=352")
            proyecto_url = (f"{SGS_URL_PW}/web#id={pid}&action=485"
                            f"&model=project.dominion&view_type=form&cids=1&menu_id=352")

            def _abrir_lista_materiales(page_obj, etiqueta: str):
                _analisis_log(f"{etiqueta}: abriendo proyecto {proyecto.get('code','?')}...")
                page_obj.goto(f"{SGS_URL_PW}/web?cids=1", wait_until="domcontentloaded", timeout=15_000)
                _t.sleep(0.8)
                page_obj.goto(proyecto_url, wait_until="domcontentloaded", timeout=25_000)
                page_obj.wait_for_selector("button[name='action_button_material']", timeout=60_000)
                _t.sleep(1.2)
                _analisis_log(f"{etiqueta}: click en smart button Materiales")
                page_obj.locator("button[name='action_button_material']").first.click()
                page_obj.wait_for_selector(".o_list_view, .o_data_row, .o_list_table", timeout=60_000)
                _t.sleep(1.5)
                n_rows = page_obj.locator(".o_data_row").count()
                _analisis_log(f"{etiqueta}: lista cargada con {n_rows} filas")
                _analisis_log(f"{etiqueta}: URL final {page_obj.url}")
                return n_rows

            _analisis_log("Navegando a vista LISTA via boton del proyecto...")
            _analisis_log(f"URL esperada tras accion: {lista_url}")
            if modo == "lista2":
                _analisis_log("Modo 2 pestanas: creando contexto explicito con la sesion actual...")
                state = pw._page.context.storage_state()
                ctx_tabs = pw._browser.new_context(
                    storage_state=state,
                    viewport={"width": 1400, "height": 900},
                )
                page = ctx_tabs.new_page()
                page.set_default_timeout(120_000)
                page2 = ctx_tabs.new_page()
                page2.set_default_timeout(120_000)
                _abrir_lista_materiales(page, "Pestana 1")
                _abrir_lista_materiales(page2, "Pestana 2")
            else:
                _abrir_lista_materiales(page, "Pestana unica")

            # Esperar que la lista cargue
            try:
                page.wait_for_selector(".o_list_view, .o_data_row, .o_list_table", timeout=20_000)
                _analisis_log("Vista de lista cargada ✓")
            except Exception:
                _analisis_log("AVISO: selector de lista no detectado — revisa el browser")

            # Contar filas visibles
            try:
                n_filas = page.locator(".o_data_row").count()
                _analisis_log(f"Filas visibles en lista: {n_filas}")
            except Exception:
                pass

            # Read-only DOM snapshot for discovering whether the list exposes
            # any usable inline action. No clicks, no writes.
            try:
                headers = page.locator(".o_list_table thead th").evaluate_all("""
                    nodes => nodes
                        .map(n => (n.innerText || n.textContent || '').trim())
                        .filter(Boolean)
                """)
                if headers:
                    _analisis_log("Columnas visibles: " + " | ".join(headers[:20]))
            except Exception as ex:
                _analisis_log(f"AVISO al leer columnas: {ex}")

            try:
                botones = page.locator(
                    ".o_control_panel button, .o_list_buttons button, .o_cp_buttons button"
                ).evaluate_all("""
                    nodes => nodes
                        .filter(n => {
                            const r = n.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        })
                        .map(n => (n.innerText || n.getAttribute('title') || n.getAttribute('aria-label') || '').trim())
                        .filter(Boolean)
                """)
                if botones:
                    _analisis_log("Botones visibles de la vista: " + " | ".join(botones[:25]))
                else:
                    _analisis_log("No se detectaron botones visibles en la barra de la lista.")
            except Exception as ex:
                _analisis_log(f"AVISO al leer botones de lista: {ex}")

            # Buscar la fila del material objetivo
            try:
                fila = page.locator(f".o_data_row:has-text('{cfg['codigo_mat']}')")
                if fila.count() > 0:
                    _analisis_log(f"Material {cfg['codigo_mat']} visible en la lista ✓")
                    # Scroll hasta la fila
                    fila_obj = fila.first
                    fila_obj.scroll_into_view_if_needed()
                    try:
                        cells = fila_obj.locator("td").evaluate_all("""
                            nodes => nodes.map((n, i) => ({
                                i,
                                text: (n.innerText || n.textContent || '').trim(),
                                classes: n.className || '',
                                name: n.getAttribute('name') || '',
                                title: n.getAttribute('title') || ''
                            }))
                        """)
                        for cell in cells[:18]:
                            txt = (cell.get("text") or "").replace("\n", " / ")
                            if txt:
                                _analisis_log(
                                    f"Fila objetivo td[{cell.get('i')}]: "
                                    f"name={cell.get('name') or '-'} text={txt[:120]}"
                                )
                    except Exception as ex:
                        _analisis_log(f"AVISO al leer celdas de fila: {ex}")

                    try:
                        attrs = fila_obj.evaluate("""
                            n => ({
                                className: n.className || '',
                                dataId: n.getAttribute('data-id') || '',
                                dataResId: n.getAttribute('data-res-id') || '',
                                id: n.getAttribute('id') || ''
                            })
                        """)
                        _analisis_log(
                            "Atributos fila objetivo: "
                            f"class={attrs.get('className') or '-'} "
                            f"data-id={attrs.get('dataId') or '-'} "
                            f"data-res-id={attrs.get('dataResId') or '-'} "
                            f"id={attrs.get('id') or '-'}"
                        )
                    except Exception as ex:
                        _analisis_log(f"AVISO al leer atributos de fila: {ex}")

                    try:
                        inline_actions = fila_obj.locator("button, a, [role='button']").evaluate_all("""
                            nodes => nodes.map(n => ({
                                tag: n.tagName,
                                text: (n.innerText || n.textContent || '').trim(),
                                title: n.getAttribute('title') || '',
                                aria: n.getAttribute('aria-label') || '',
                                classes: n.className || ''
                            })).filter(x => x.text || x.title || x.aria || x.classes)
                        """)
                        if inline_actions:
                            for act in inline_actions[:15]:
                                _analisis_log(
                                    "Accion inline candidata: "
                                    f"{act.get('tag')} text={act.get('text') or '-'} "
                                    f"title={act.get('title') or '-'} aria={act.get('aria') or '-'}"
                                )
                        else:
                            _analisis_log("Sin botones/enlaces inline visibles dentro de la fila objetivo.")
                    except Exception as ex:
                        _analisis_log(f"AVISO al leer acciones inline: {ex}")
                else:
                    _analisis_log(f"Material {cfg['codigo_mat']} no visible en pantalla — puede estar en otra página")
            except Exception as ex:
                _analisis_log(f"AVISO al buscar fila: {ex}")

            _analisis_log("══════════════════════════════════════")
            _analisis_log("PAUSA — Vista lista abierta para inspección")
            try:
                shot_name = f"analisis_lista_{pid}_{pm_id}.png"
                shot_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..", "..", "logs", shot_name,
                )
                page.screenshot(path=os.path.normpath(shot_path), full_page=True)
                _analisis_log(f"Screenshot lista: /liquidador2/log_img/{shot_name}")
            except Exception as ex:
                _analisis_log(f"AVISO screenshot lista: {ex}")
            _analisis_log(f"  pid      = {pid}")
            _analisis_log(f"  pm_id    = {pm_id}")
            _analisis_log(f"  URL      = {lista_url}")
            _analisis_log("Pulsa 'Cerrar browser' cuando termines.")
            _analisis_log("══════════════════════════════════════")
            with _analisis_lock:
                _analisis_job["phase"] = "paused"

            stop_ev.wait(timeout=600)

        # ── MODO FORM (dry-run original) ──────────────────────────────────────
        else:
            _analisis_log(f"Navegando a form pm_id={pm_id}...")
            pw._navegar_material(pm_id)
            pendiente_dom, devuelto_dom = pw._leer_pendiente_material_dom()
            _analisis_log(f"Form cargado — Pendiente DOM={pendiente_dom}  Devuelto DOM={devuelto_dom}")
            if devuelto_dom and devuelto_dom > 0.001:
                _analisis_log(f"AVISO: devuelto={devuelto_dom} en DOM")
            if pendiente_dom is not None and pendiente_dom <= 0.001:
                _analisis_log("AVISO: Pendiente DOM = 0 — puede ya estar liquidado")

            try:
                eb = page.locator("button:has-text('EDITAR'), button:has-text('Editar')").first
                eb.wait_for(state="visible", timeout=8_000)
                eb.click()
                _analisis_log("EDITAR clickeado")
            except Exception:
                _analisis_log("Sin botón EDITAR — auto-edit mode")

            page.wait_for_selector(".o_form_view.o_form_editable", timeout=25_000)
            _analisis_log("Formulario en modo edición ✓")
            _t.sleep(2.5)

            add_sel = "[name='advance_project_material_ids'] .o_field_x2many_list_row_add a"
            page.wait_for_selector(add_sel, timeout=30_000)
            page.locator(add_sel).first.click()
            _analisis_log("'Agregar una línea' clickeado")

            qty_sel = "[name='advance_project_material_ids'] tr.o_selected_row input[name='amount']"
            try:
                page.wait_for_selector(qty_sel, timeout=8_000)
            except Exception:
                _t.sleep(0.6)

            qty_input = None
            for sel in [
                qty_sel,
                "[name='advance_project_material_ids'] tr.o_selected_row [name='amount']",
                "[name='advance_project_material_ids'] tbody tr:last-child input[name='amount']",
                "[name='advance_project_material_ids'] input[name='amount']",
            ]:
                el = page.locator(sel).last
                if el.count() > 0:
                    try:
                        if el.is_visible(timeout=1_500):
                            qty_input = el; break
                    except Exception:
                        pass

            if not qty_input:
                raise RuntimeError("Campo 'amount' no encontrado en advance_project_material_ids")

            qty_input.click(click_count=3)
            qty_input.fill(_cant_sgs(cfg["cantidad"]))
            _analisis_log(f"Cantidad {cfg['cantidad']} rellenada ✓")

            if cfg.get("partner"):
                p_sel = ("[name='advance_project_material_ids'] "
                         "tr.o_selected_row [name='partner_id'] input")
                try:
                    p_input = page.locator(p_sel).first
                    if p_input.count() > 0 and p_input.is_visible(timeout=2_000):
                        p_input.click()
                        termino = cfg["partner"].strip().split()[0]
                        p_input.fill(termino)
                        _t.sleep(0.8)
                        drop = (".o_field_many2one_dropdown .o_m2o_dropdown_option, "
                                ".dropdown-item.o_m2o_dropdown_option")
                        page.wait_for_selector(drop, timeout=5_000)
                        page.locator(drop).first.click()
                        _analisis_log(f"Partner '{cfg['partner']}' seleccionado ✓")
                except Exception as ex:
                    _analisis_log(f"AVISO partner: {ex}")
            else:
                _analisis_log("Partner vacío — rellena a mano en el browser si quieres ver el dropdown")

            _analisis_log("══════════════════════════════════════")
            _analisis_log("FORMULARIO LISTO — PAUSA PARA INSPECCIÓN")
            _analisis_log(f"  pm_id    = {pm_id}")
            _analisis_log(f"  Cantidad = {cfg['cantidad']}")
            _analisis_log(f"  Pendiente DOM antes = {pendiente_dom}")
            _analisis_log("NO se ha hecho click en GUARDAR.")
            _analisis_log("Pulsa 'Cerrar browser' cuando termines.")
            _analisis_log("══════════════════════════════════════")
            with _analisis_lock:
                _analisis_job["phase"] = "paused"

            stop_ev.wait(timeout=600)

    except Exception as ex:
        _analisis_log(f"ERROR: {ex}")
        with _analisis_lock:
            _analisis_job["phase"] = "error"
    finally:
        # Descartar si el form sigue abierto
        if pw and pw._page:
            try:
                pw._page.locator(
                    "button:has-text('DESCARTAR'), button:has-text('Descartar')"
                ).first.click(timeout=3_000)
                _analisis_log("Formulario descartado.")
            except Exception:
                pass
            try:
                _t.sleep(0.8)
                pw.close()
                _analisis_log("Browser cerrado.")
            except Exception:
                pass
        with _analisis_lock:
            _analisis_job["running"]    = False
            _analisis_job["pw"]         = None
            _analisis_job["stop_event"] = None
            if _analisis_job["phase"] not in ("error",):
                _analisis_job["phase"] = "done"


@liquidador2.route("/analisis")
def analisis():
    with _analisis_lock:
        estado = {
            "running": _analisis_job["running"],
            "phase":   _analisis_job["phase"],
            "logs":    list(_analisis_job["logs"]),
        }
    cfg = dict(_PRUEBA_DEFAULT)
    return render_template("liquidador2/analisis.html", estado=estado, cfg=cfg)


@liquidador2.route("/analisis/ejecutar", methods=["POST"])
def analisis_ejecutar():
    with _analisis_lock:
        if _analisis_job["running"]:
            return jsonify({"ok": False, "msg": "Ya hay una prueba en ejecución."})
        _analisis_job["running"] = True
        _analisis_job["phase"]   = "running"
        _analisis_job["logs"]    = []

    cfg = {
        "modo":       request.form.get("modo", "lista").strip() or "lista",
        "pro_buscar": request.form.get("pro_buscar", _PRUEBA_DEFAULT["pro_buscar"]).strip(),
        "codigo_mat": request.form.get("codigo_mat", _PRUEBA_DEFAULT["codigo_mat"]).strip(),
        "cantidad":   float(request.form.get("cantidad", _PRUEBA_DEFAULT["cantidad"]) or 6),
        "partner":    request.form.get("partner",    _PRUEBA_DEFAULT["partner"]).strip(),
    }
    if cfg["modo"] not in ("lista", "lista2", "modal2", "form"):
        cfg["modo"] = "lista"
    threading.Thread(target=_analisis_thread, args=(cfg,), daemon=True,
                     name="analisis-dryrun-l2").start()
    return jsonify({"ok": True})


@liquidador2.route("/analisis/estado")
def analisis_estado():
    with _analisis_lock:
        return jsonify({
            "running": _analisis_job["running"],
            "phase":   _analisis_job["phase"],
            "logs":    list(_analisis_job["logs"]),
        })


@liquidador2.route("/analisis/descartar", methods=["POST"])
def analisis_descartar():
    with _analisis_lock:
        ev = _analisis_job.get("stop_event")
    if ev:
        ev.set()
        return jsonify({"ok": True, "msg": "Señal de descarte enviada."})
    return jsonify({"ok": False, "msg": "No hay prueba en pausa."})


@liquidador2.route("/log_img/<path:filename>")
def log_img(filename):
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "logs")
    return send_from_directory(os.path.normpath(logs_dir), filename)


# ── Distribución IA (MiniMax) ────────────────────────────────────────────────

@liquidador2.route("/ia_parsear/<sid>", methods=["POST"])
def ia_parsear(sid):
    """AJAX: llama a MiniMax, hace fuzzy matching, devuelve JSON para el panel inline."""
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"ok": False, "error": "Sesión no encontrada."})
    data = request.get_json(silent=True) or {}
    texto = (data.get("texto") or "").strip()
    if not texto:
        return jsonify({"ok": False, "error": "Texto vacío."})

    lideres_dict = _todos_lideres_session(s)
    checks_mo    = s.get("checks_mo", [])
    nodos_conocidos = set()
    for ck in checks_mo:
        row = ck.get("row") or {}
        nodo = str(row.get("pro") or row.get("_odm") or "").strip()
        if nodo:
            nodos_conocidos.add(nodo)

    # Caché de búsquedas SGS para evitar consultas duplicadas por el mismo lider_ia
    _sgs_cache = {}
    _sgs_con   = None
    def _sgs_fallback(lider_ia):
        nonlocal _sgs_con
        if lider_ia in _sgs_cache:
            return _sgs_cache[lider_ia]
        if _sgs_con is None:
            try:
                _sgs_con = _sgs()
            except Exception:
                _sgs_cache[lider_ia] = None
                return None
        m = _buscar_lider_en_sgs(lider_ia, _sgs_con)
        _sgs_cache[lider_ia] = m
        return m

    # ── Tabla por prestación: parseo determinístico ANTES de llamar a la IA ──
    filas_tabla = _parsear_tabla_prestaciones(texto)
    filas_lineas = None if filas_tabla else parsear_lineas_nodo_lider(texto)
    if filas_tabla:
        ia_data = {"mode": "prestacion", "asignaciones": filas_tabla, "_fuente": "tabla"}
    elif filas_lineas:
        # "Nodo 2: <líder A>" / "Nodo 2 al 6: <líder B>" → determinista,
        # sin IA. Un nodo en varias líneas ACUMULA líderes (no se pisa).
        ia_data = {"mode": "nodo", "asignaciones": filas_lineas, "_fuente": "lineas"}
    else:
        # Texto libre → Cloudflare Workers AI (respaldo MiniMax), ver ia_lideres.py
        try:
            ia_data = llamar_ia_asignaciones(texto, minimax_fn=_llamar_minimax)
        except Exception as e:
            log.warning("ia_parsear IA error: %s", e)
            return jsonify({"ok": False, "error": str(e)})
        ia_data["_fuente"] = "ia"

    mode = ia_data.get("mode", "nodo")

    # ── Modo prestación: asignaciones (nodo, código QP) → líder(es) ──
    if mode == "prestacion":
        asigs = []
        for a in (ia_data.get("asignaciones") or []):
            nodo   = str(a.get("nodo") or "").strip().upper()
            codigo = str(a.get("codigo") or "").strip().upper()
            lids   = a.get("lideres") or ([a.get("lider")] if a.get("lider") else [])
            lids   = [str(l).strip().upper() for l in lids if str(l or "").strip()]
            if not nodo or not codigo or not lids:
                continue
            asigs.append({"nodo": nodo, "codigo": codigo,
                          "cant": a.get("cant"), "lideres": lids})
        if not asigs:
            return jsonify({"ok": False,
                            "error": "La interpretación por prestación no produjo asignaciones."})
        # Nombres únicos (en orden de aparición) → resolver una sola vez
        nombres_unicos = []
        for a in asigs:
            for l in a["lideres"]:
                if l not in nombres_unicos:
                    nombres_unicos.append(l)
        lideres_resueltos, extra_pres = _resolver_lideres_ia(
            nombres_unicos, lideres_dict, _sgs_fallback)
        conteo = {}
        for a in asigs:
            for l in a["lideres"]:
                conteo[l] = conteo.get(l, 0) + 1
        for lr in lideres_resueltos:
            lr["filas"] = conteo.get(lr["lider_ia"], 0)
        log.info("ia_parsear: modo prestacion (%s) — %d filas, %d líderes",
                 ia_data.get("_fuente", "ia"), len(asigs), len(lideres_resueltos))
        return jsonify({
            "ok":            True,
            "mode":          "prestacion",
            "fuente":        ia_data.get("_fuente", "ia"),
            "asignaciones":  asigs,
            "lideres":       lideres_resueltos,
            "extra_cedulas": extra_pres,
        })

    # ── Modo equitativo: devolver lista de líderes resueltos con % ──
    if mode == "equitativo":
        lideres_ia        = ia_data.get("lideres") or []
        extra_cedulas_eq  = {}
        lideres_resueltos = []
        for item in lideres_ia:
            # MiniMax puede devolver str (formato antiguo) o dict {nombre, pct}
            if isinstance(item, str):
                lider_ia = item.strip()
                pct_raw  = None
            else:
                lider_ia = (item.get("nombre") or "").strip()
                pct_raw  = item.get("pct")
            if not lider_ia:
                continue
            match = _fuzzy_match_lider(lider_ia, lideres_dict)
            if match and not match[1]:
                sgs_m = _sgs_fallback(lider_ia)
                if sgs_m and sgs_m[1]:
                    match = sgs_m
            if not match:
                match = _sgs_fallback(lider_ia)
                if match and match[0] not in lideres_dict:
                    extra_cedulas_eq[match[0]] = match[1]
            lideres_resueltos.append({
                "lider_ia": lider_ia,
                "nombre":   match[0] if match else "",
                "cedula":   match[1] if match else "",
                "score":    round((match[2] if match else 0) * 100),
                "pct":      float(pct_raw) if pct_raw is not None else None,
            })
        # Calcular pcts iguales para los que no tienen
        sum_spec = sum(l["pct"] for l in lideres_resueltos if l["pct"] is not None)
        n_libre  = sum(1 for l in lideres_resueltos if l["pct"] is None)
        pct_libre = round((100 - sum_spec) / n_libre, 1) if n_libre else 0
        for l in lideres_resueltos:
            if l["pct"] is None:
                l["pct"] = pct_libre
        return jsonify({
            "ok":            True,
            "mode":          "equitativo",
            "lideres":       lideres_resueltos,
            "extra_cedulas": extra_cedulas_eq,
        })

    # ── Modo nodo: un nodo puede llevar VARIOS líderes (se acumulan, nunca se pisan) ──
    # Ejemplo (2026-09-25): "Nodo 2: <líder A>" y después
    # "Nodo 2 al 6: <líder B>" → el nodo 2 lo trabajaron los dos; antes el
    # segundo pisaba al primero. Ahora sale una fila por (nodo, líder) con k/n y
    # el JS reparte las prestaciones del nodo entre sus líderes por valor.
    por_nodo = agrupar_por_nodo(ia_data.get("asignaciones") or [])
    if not por_nodo:
        return jsonify({"ok": False,
                        "error": "La interpretación no produjo ningún nodo con líder."})
    # "Nodo 2 al 6" = nodo CABLE '2AL6' si la planilla lo tiene (PRO N2_N6);
    # solo se expande a 2..6 individuales cuando no existe el cable.
    por_nodo = resolver_nodos_contra_planilla(por_nodo, nodos_conocidos, _nodo_equiv)
    nombres_unicos = []
    for lids in por_nodo.values():
        for l in lids:
            if l not in nombres_unicos:
                nombres_unicos.append(l)
    resueltos, extra_cedulas = _resolver_lideres_ia(nombres_unicos, lideres_dict, _sgs_fallback)
    res_por_nombre = {r["lider_ia"]: r for r in resueltos}

    def _nn(n):
        return re.sub(r"^0+(?=\d)", "", str(n or "").strip().upper())
    conocidos_norm = {_nn(n) for n in nodos_conocidos}

    asignaciones = []
    for nodo, lids in por_nodo.items():
        for k, l in enumerate(lids):
            r = res_por_nombre.get(l) or {}
            asignaciones.append({
                "nodo":      nodo,
                "k":         k,
                "n_lideres": len(lids),
                "lider_ia":  l,
                "nombre":    r.get("nombre", ""),
                "cedula":    r.get("cedula", ""),
                "score":     r.get("score", 0),
                "en_nodos":  _nn(nodo) in conocidos_norm,
            })
    n_multi = sum(1 for lids in por_nodo.values() if len(lids) > 1)
    log.info("ia_parsear: modo nodo (%s/%s) — %d nodos, %d con varios líderes",
             ia_data.get("_fuente", "ia"), ia_data.get("_proveedor", "-"),
             len(por_nodo), n_multi)
    return jsonify({
        "ok":            True,
        "mode":          "nodo",
        "fuente":        ia_data.get("_fuente", "ia"),
        "proveedor":     ia_data.get("_proveedor", ""),
        "aviso":         ia_data.get("_aviso", ""),
        "asignaciones":  asignaciones,
        "extra_cedulas": extra_cedulas,
    })


@liquidador2.route("/ia_guardar/<sid>", methods=["POST"])
def ia_guardar(sid):
    """AJAX: guarda en sesión el mapa nodo→líder que el usuario confirmó."""
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"ok": False})
    data = request.get_json(silent=True) or {}
    # data is {nodo: {partner_nombre, partner_cedula, ...}}; claves "IDX:<i>" = fila
    # concreta (equitativo / nodo con varios líderes). "_resumen" = lista de textos
    # "Nodo 2: A (3) + B (2)" para el banner (las claves IDX no se leen bien solas).
    nodo_lider = {}
    resumen = data.pop("_resumen", None)
    s["ia_resumen"] = [str(x) for x in resumen] if isinstance(resumen, list) else []
    for nodo, info in data.items():
        if isinstance(info, dict) and info.get("partner_nombre"):
            cedula = info.get("partner_cedula", "")
            if cedula and cedula.isdigit():
                cedula = "C" + cedula
            nodo_lider[nodo] = {"partner_nombre": info["partner_nombre"],
                                  "partner_cedula": cedula}
    s["ia_nodo_lider"] = nodo_lider
    log.info("ia_guardar: %d nodos guardados en sesión %s", len(nodo_lider), sid)
    return jsonify({"ok": True})


@liquidador2.route("/ia_distribucion/<sid>", methods=["GET", "POST"])
def ia_distribucion(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesion no encontrada.")
        return redirect(url_for("liquidador2.subir"))

    lideres_dict = _todos_lideres_session(s)
    checks_mo    = s.get("checks_mo", [])

    nodos_conocidos = set()
    for ck in checks_mo:
        row = ck.get("row") or {}
        nodo = str(row.get("pro") or row.get("_odm") or "").strip()
        if nodo:
            nodos_conocidos.add(nodo)

    if request.method == "GET":
        return render_template("liquidador2/distribucion_ia.html",
            sid=sid, s=s,
            lideres_dict=lideres_dict,
            nodos_conocidos=sorted(nodos_conocidos),
            ia_resultado=None, texto="", error=None,
            ia_nodo_lider=s.get("ia_nodo_lider", {}))

    action = request.form.get("action", "parsear")
    texto  = (request.form.get("texto") or "").strip()

    if action == "parsear":
        if not texto:
            return render_template("liquidador2/distribucion_ia.html",
                sid=sid, s=s, lideres_dict=lideres_dict,
                nodos_conocidos=sorted(nodos_conocidos),
                ia_resultado=None, texto="",
                error="Debes pegar el texto con las asignaciones.",
                ia_nodo_lider=s.get("ia_nodo_lider", {}))
        try:
            filas_lineas = parsear_lineas_nodo_lider(texto)
            if filas_lineas:
                ia_data = {"mode": "nodo", "asignaciones": filas_lineas}
            else:
                ia_data = llamar_ia_asignaciones(texto, minimax_fn=_llamar_minimax)
        except Exception as e:
            return render_template("liquidador2/distribucion_ia.html",
                sid=sid, s=s, lideres_dict=lideres_dict,
                nodos_conocidos=sorted(nodos_conocidos),
                ia_resultado=None, texto=texto,
                error=f"Error llamando a la IA: {e}",
                ia_nodo_lider=s.get("ia_nodo_lider", {}))

        # Caché SGS para no consultar el mismo nombre dos veces
        _sgs_cache2 = {}
        _sgs_con2   = None
        def _sgs_fb2(li):
            nonlocal _sgs_con2
            if li in _sgs_cache2:
                return _sgs_cache2[li]
            if _sgs_con2 is None:
                try:
                    _sgs_con2 = _sgs()
                except Exception:
                    _sgs_cache2[li] = None
                    return None
            m = _buscar_lider_en_sgs(li, _sgs_con2)
            _sgs_cache2[li] = m
            return m

        # Líderes extra encontrados en SGS (para expandir el dropdown)
        lideres_dict_ext = dict(lideres_dict)

        # Página antigua: un líder por nodo. Si un nodo trae varios, se emite una
        # fila por líder (el panel inline de verificación sí reparte dentro del nodo).
        filas = []
        asigs_planas = []
        for asig in (ia_data.get("asignaciones") or []):
            lids = asig.get("lideres") or ([asig.get("lider")] if asig.get("lider") else [])
            for l in lids:
                asigs_planas.append({"lider": l, "nodos": asig.get("nodos") or []})
        for asig in asigs_planas:
            lider_ia   = (asig.get("lider") or "").strip()
            nodos_asig = asig.get("nodos") or []
            match      = _fuzzy_match_lider(lider_ia, lideres_dict_ext)
            # Si la sesión encontró el nombre pero sin cédula, intentar SGS para obtenerla
            if match and not match[1] and lider_ia:
                sgs_m = _sgs_fb2(lider_ia)
                if sgs_m and sgs_m[1]:
                    match = sgs_m
            if not match and lider_ia:
                match = _sgs_fb2(lider_ia)
                if match and match[0] not in lideres_dict_ext:
                    lideres_dict_ext[match[0]] = match[1]
            for nodo in nodos_asig:
                filas.append({
                    "nodo":        nodo,
                    "lider_ia":    lider_ia,
                    "lider_match": match[0] if match else "",
                    "cedula":      match[1] if match else "",
                    "score":       round((match[2] if match else 0) * 100),
                    "en_nodos":    nodo in nodos_conocidos,
                })

        return render_template("liquidador2/distribucion_ia.html",
            sid=sid, s=s, lideres_dict=lideres_dict_ext,
            nodos_conocidos=sorted(nodos_conocidos),
            ia_resultado=filas, texto=texto, error=None,
            ia_nodo_lider=s.get("ia_nodo_lider", {}))

    elif action == "aplicar":
        nodo_lider = {}
        for key, val in request.form.items():
            if key.startswith("lider_nodo_"):
                nodo = key[len("lider_nodo_"):]
                nombre = val.strip()
                cedula = (request.form.get(f"cedula_nodo_{nodo}") or "").strip()
                if nombre:
                    if cedula.isdigit():
                        cedula = "C" + cedula
                    nodo_lider[nodo] = {"partner_nombre": nombre, "partner_cedula": cedula}
        s["ia_nodo_lider"] = nodo_lider
        log.info("ia_distribucion: asignados %d nodos via IA", len(nodo_lider))
        flash(f"Asignación IA aplicada: {len(nodo_lider)} nodo(s).")
        return redirect(url_for("liquidador2.verificar", sid=sid))

    return redirect(url_for("liquidador2.ia_distribucion", sid=sid))


# ── Generación Excel importación masiva de nodos ────────────────────────────

@liquidador2.route("/generar_excel_nodos/<sid>")
def generar_excel_nodos(sid):
    """Genera y descarga el Excel de importación masiva para nodos faltantes."""
    s = _sesiones.get(sid)
    if not s:
        flash("Sesión no encontrada.")
        return redirect(url_for("liquidador2.subir"))

    nf = s.get("nodos_faltantes") or {}
    nodos        = nf.get("nodos_faltantes", [])
    parent_code  = nf.get("parent_pro_code", "")
    ticket       = nf.get("ticket", "")
    fecha_iso    = s.get("fecha_liquidacion_iso", "") or datetime.now().strftime("%Y-%m-%d")

    if not nodos or not parent_code:
        flash("No hay nodos faltantes o PRO padre no detectado.")
        return redirect(url_for("liquidador2.verificar", sid=sid))

    try:
        buf, omitidos, d_padre = _generar_excel_nodos_bytes(parent_code, nodos, fecha_iso, ticket)
    except Exception as e:
        flash(f"Error al generar Excel: {e}")
        return redirect(url_for("liquidador2.verificar", sid=sid))

    # Si el padre de la sesión era un proyecto individual, el resolver redirigió
    # al padre real — avisar y sanar la sesión para los siguientes pasos.
    if d_padre.get("aviso_padre"):
        flash("⚠ " + d_padre["aviso_padre"])
    real_code = d_padre.get("parent_code", "")
    if real_code and real_code != parent_code:
        s["nodos_faltantes"] = {**nf, "parent_pro_code": real_code}
        parent_code = real_code

    if omitidos and len(omitidos) >= len(nodos):
        flash("Todos los nodos ya existen en SGS — no hay nada que importar: "
              + "; ".join(omitidos) + ". Usa 'Verificar en SGS' para refrescar el panel.")
        return redirect(url_for("liquidador2.verificar", sid=sid))
    if omitidos:
        flash(f"{len(omitidos)} nodo(s) omitido(s) del Excel por ya existir: "
              + "; ".join(omitidos))

    import time as _t
    ts       = _t.strftime("%m%d_%H%M%S")
    filename = f"Cabecera_{parent_code}_{len(nodos)}nodos_{ts}.xlsx"
    log.info("generar_excel_nodos: %s → %d nodos, omitidos=%s", parent_code, len(nodos), omitidos)

    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
