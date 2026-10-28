"""
═══════════════════════════════════════════════════════════════════════════
  DEVOLUCIONES SGS — captura móvil + OCR IA + verificación de seguridad
═══════════════════════════════════════════════════════════════════════════
Submódulo del flujo de materiales. Fases implementadas (paso 1 y 2):

  PASO 1 — Captura desde celular:
    · El celular abre https://<IP-del-PC>:47297/movil
    · Detección de hoja en vivo estilo CamScanner (OpenCV.js + jscanify)
    · Cámara preconfigurada: enfoque continuo, resolución máxima,
      linterna y zoom si el teléfono los soporta
    · Recorte de perspectiva automático y subida inmediata a la sesión
    · La sesión se sincroniza en vivo con el PC (mismo servidor)

  PASO 2 — OCR IA + verificación de seguridad:
    · Cada foto se procesa con la API de visión del proveedor de IA (salida
      estructurada JSON)
    · Se extrae: BAN, PRO, líder, tabla de materiales y notas al pie.
      REGLA DEL NEGOCIO: lo manuscrito en la columna "Consumido" ES la
      cantidad a DEVOLVER (no el consumo). consumo ≈ entregado − devolución
    · Verificación SGS SOLO LECTURA: BAN existe → su proyecto es el PRO
      de la hoja → su líder coincide con "Asignada a" → cada material con
      devolución salió en esa bandeja
    · Semáforo por material: verde / amarillo / rojo según confianza OCR
      + verificación SGS

  PASO 3 — Verificación humana en pantalla dividida (tabla editable).
  PASO 4 — Ejecución de devoluciones, aprobaciones y despachos adicionales
    únicamente a través de PlaywrightClient (navegador), nunca por RPC.

Las lecturas de SGS van siempre por SGSReadOnlyClient.

Puerto local: 47297 (HTTPS, certificado autofirmado en devoluciones_certs/)
"""
import sys
import os

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import threading
import time
import base64
import re
import socket
import unicodedata
import logging
import ipaddress
import concurrent.futures
from collections import deque
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify, send_file, Response, redirect, session, render_template
from werkzeug.security import check_password_hash
import secrets

import anthropic
import httpx           # ya viene como dependencia de anthropic (backend Kimi)
import config as cfg
from sgs_readonly_client import SGSReadOnlyClient

# ── Rutas / constantes ────────────────────────────────────────────────────────
_BASE        = os.path.dirname(os.path.abspath(__file__))
DIR_SESIONES = os.path.join(_BASE, "devoluciones_sesiones")
DIR_STATIC   = os.path.join(_BASE, "devoluciones_static")
DIR_CERTS    = os.path.join(_BASE, "devoluciones_certs")
os.makedirs(DIR_SESIONES, exist_ok=True)
os.makedirs(DIR_CERTS, exist_ok=True)

PORT       = int(os.getenv("PORT", "47297"))
# Supervisores autorizados para despachos ADICIONALES automáticos: nombres completos tal como
# los escribe SGS, separados por coma en DEV_SUPERVISORES_ADICIONALES (vacío = ninguno).
# La hoja debe tener a uno de ellos como supervisor (2º nombre de la cabecera).
SUPERVISORES_ADICIONALES = [s.strip() for s in (os.getenv("DEV_SUPERVISORES_ADICIONALES") or "").split(",")
                            if s.strip()]
# En el servidor (detrás de nginx) start.sh define DEV_PUBLIC_URL con el dominio público
PUBLIC_URL = os.getenv("DEV_PUBLIC_URL", "").rstrip("/")


def _url_publica():
    """Base pública (https://host) del dominio por el que ENTRÓ el usuario.
    La app puede atender varios dominios y el enlace del celular debe ser el mismo
    que tiene abierto en el PC: nginx manda
    Host y X-Forwarded-Proto. Sin request real (token servidor-a-servidor, local) →
    DEV_PUBLIC_URL."""
    try:
        host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
        nombre = host.split(":")[0]
        if host and nombre not in ("localhost", "127.0.0.1") and not nombre.replace(".", "").isdigit():
            proto = request.headers.get("X-Forwarded-Proto") or request.scheme or "https"
            return f"{proto}://{host}"
    except RuntimeError:          # fuera de una petición
        pass
    return PUBLIC_URL
# Bench 2026-08-31 sobre hojas reales (BAN-118599 de 42 filas + continuación):
# sonnet-5 con effort=medium leyó 8/8 manuscritos igual que opus-5 a 42% menos
# costo ($0.127 vs $0.221/hoja); sonnet sin effort NO ahorra (piensa de más);
# haiku-4-5 descalificado (5/8 manuscritos mal + alucinó una nota del ejemplo
# del prompt + confundió un "BAN=..." manuscrito con el BAN de la hoja).
# Volver a Opus: MODELO_OCR=claude-opus-5 (y OCR_EFFORT=high) en start.sh.
MODELO_OCR = os.getenv("MODELO_OCR", "claude-sonnet-5")
# Bench 2026-09-03 (modo compacto, hoja de 42 filas): low = 19s/$0.042 con 8/8
# manuscritos y 40/40 filas idénticas al formato completo; medium = 42s/$0.062
# (y pegó un sufijo a una ref). Si la calidad baja en hojas feas: OCR_EFFORT=medium.
OCR_EFFORT = os.getenv("OCR_EFFORT", "low")   # low|medium|high|xhigh|max
# OCR_MODO "compacto" (2026-09-03): el modelo NO transcribe lo impreso (código DOM,
# descripción, solicitado — eso ya está en SGS), solo ref + números por fila →
# ~75% menos tokens de salida, que son el 85% del costo. "completo" = formato viejo.
OCR_MODO = os.getenv("OCR_MODO", "compacto")
# Auto-OCR (2026-09-16): cada foto que sube el celular se procesa sola, sin pulsar
# «Procesar». Por sesión se puede apagar (auto_ocr=false).
OCR_AUTO = os.getenv("OCR_AUTO", "1").strip().lower() in ("1", "true", "si", "sí")

# Misma key que panel_nodos.py (o variable de entorno ANTHROPIC_API_KEY)
ANTHROPIC_API_KEY = os.getenv(
    "ANTHROPIC_API_KEY",
    "",
)

# ── Kimi (Moonshot) — backend de OCR alternativo, mucho más barato ──
# OCR_BACKEND: "kimi" usa Moonshot con FALLBACK automático a Claude si falla
# (429 sostenido, JSON inválido, truncado); "claude" vuelve al comportamiento previo.
# ⚠ La cuenta Moonshot tiene CONCURRENCIA MÁXIMA 1: toda llamada va serializada
#   por _KIMI_LOCK y con reintentos ante 429. El modelo solo acepta temperature=1
#   y sin streaming el gateway corta las respuestas largas.
KIMI_API_KEY = os.getenv(
    "KIMI_API_KEY",
    "",
)
KIMI_URL     = os.getenv("KIMI_URL", "https://api.moonshot.ai/v1/chat/completions")
# k2.6 y k3 piensan DEMASIADO para OCR (k2.6: ~8 min/hoja); highspeed es la
# misma familia con salida rápida
KIMI_MODEL   = os.getenv("KIMI_MODEL", "kimi-k2.7-code-highspeed")
# Evaluación 2026-08-31: la calidad de Kimi en estas hojas es insuficiente; el backend
# queda como opción de emergencia y el defecto vuelve al proveedor principal.
OCR_BACKEND  = os.getenv("OCR_BACKEND", "claude").strip().lower()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s — %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("devoluciones")

# Buffer circular de logs del proceso (para el informe técnico de soporte)
_LOG_BUFFER = deque(maxlen=600)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            _LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


_bh = _BufferHandler()
_bh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s"))
logging.getLogger().addHandler(_bh)   # root: captura también browser_client

app = Flask(__name__, template_folder=os.path.join(_BASE, "almacen_templates"))

# ── SGO7: trazabilidad — todo archivo subido, proceso o descarga queda registrado en sabogal ──
try:
    import sgo7_registro
    sgo7_registro.instalar(app, modulo="devoluciones", ignorar=("/api/dev/diagnostico",), acciones={
            r"/api/dev/sesion/[^/]+/foto": "devoluciones_foto",
            r"/api/dev/sesion/[^/]+/(procesar|auto|reprocesar)": "devoluciones_ocr",
            r"/api/dev/sesion/[^/]+/hoja/\d+/validar": "devoluciones_validar_hoja",
            r"/api/dev/sesion/[^/]+/hoja/\d+/(editar|editar-nota|agregar-nota|plan|reverificar)": "devoluciones_editar_hoja",
            r"/api/dev/sesion/[^/]+/devoluciones": "devoluciones_ejecutar_sgs",
            r"/api/dev/sesion/[^/]+/adicionales": "devoluciones_adicionales",
            r"/api/dev/sesion/[^/]+/adic-todos": "devoluciones_adicionales",
            r"/api/dev/sesion/[^/]+/aprobar-bandejas": "devoluciones_aprobar_bandejas",
            r"/api/dev/sesion/[^/]+/cerrar": "devoluciones_cerrar",
            r"/api/dev/sesion/[^/]+/aprender": "devoluciones_aprender",
            r"/api/dev/sesion/[^/]+/verificar-devoluciones": "devoluciones_verificar",
            r"/api/dev/sesion/[^/]+/excel": "devoluciones_excel",
            r"/api/dev/sesion$": "devoluciones_nueva_sesion",
            r"/almacen/anexo": "almacen_anexo",
            r"/almacen/buscar": "almacen_buscar",
            r"/almacen/generar": "almacen_cargue",
            r"/almacen/cargue-sesion": "almacen_cargue_hojas",
            r"/almacen/descargar": "descargar",
            r"/almacen": "almacen"
    })
except Exception as _e_sgo7:
    print("sgo7_registro no instalado:", _e_sgo7)

# ══════════════════════════════════════════════════════════════════════════
#  Perfiles (2026-09-16, Módulo Almacén): login propio con roles.
#    admin   → responsable del módulo: todo (crea bandejas, adicionales, preanálisis, aprender)
#    almacen → procesa/valida hojas y saca su Excel; NUNCA escribe en SGS.
#  DEV_REQUIRE_AUTH=1 activa el login (prod). En local queda apagado y el
#  usuario es DEV_LOCAL_USER/DEV_LOCAL_ROL (para probar la vista de almacén).
#  DEV_USERS="admin:<clave-o-hash>:admin;almacen:<clave-o-hash>:almacen"
#  (hash werkzeug scrypt/pbkdf2 o clave en claro; en el .env entre comillas simples)
# ══════════════════════════════════════════════════════════════════════════
DEV_REQUIRE_AUTH = os.getenv("DEV_REQUIRE_AUTH", "0").strip().lower() in ("1", "true", "si", "sí")
app.secret_key = os.getenv("DEV_APP_SECRET") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("DEV_PUBLIC_URL", "").startswith("https")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
ROLES = ("admin", "almacen", "visor")
# Lo que puede abrir el rol almacén: su módulo, la captura móvil, el panel de hojas
# (los endpoints que escriben en SGS devuelven 403 por _solo_admin) y estáticos.
_RUTAS_ALMACEN = ("/almacen", "/movil", "/api/dev/", "/static-dev/", "/login", "/logout")

# ══════════════════════════════════════════════════════════════════════════
#  Zonas (2026-10-07). Verificado en SGS (solo lectura, project.tray desde
#  2026-08-01): las tres zonas trabajan igual, mismo contrato "Enel zona VII -
#  2026"; solo cambia la bodega. Despacho sale de <BODEGA>/Stock y la devolución
#  entra a <BODEGA>/Recupero.
#    ubate    → UBAT2 (1234 despachos / 573 devoluciones)  ← la que opera hoy
#    gacheta  → GACH2 ( 729 / 374)
#    choconta → CHOC2 (1021 / 592)
#  Cada usuario y cada sesión llevan zona. La zona decide (a) la bodega y
#  (b) con qué cuenta de SGS se ESCRIBE: Ubaté = la de siempre (config.py);
#  otra zona = SGS_LOGIN_<ZONA> / SGS_PASSWORD_<ZONA> en start.sh. Zona sin
#  credenciales = no operativa: nada se escribe en SGS desde sus sesiones.
#  Las LECTURAS de SGS van siempre con la cuenta de lectura de config.py.
#  rol visor = observador: ve las sesiones de la zona base (para conocer el
#  flujo) y las de su zona; no crea, no edita, no gasta OCR, no escribe.
# ══════════════════════════════════════════════════════════════════════════
ZONAS = {
    "ubate":    {"nombre": "Ubaté",    "bodega": "UBAT2", "region": "UBATE"},
    "gacheta":  {"nombre": "Gachetá",  "bodega": "GACH2", "region": "GACHETA"},
    "choconta": {"nombre": "Chocontá", "bodega": "CHOC2", "region": "CHOCONTA"},
}
ZONA_BASE = "ubate"
# Usuarios que ven las sesiones de TODAS las zonas (el dueño del sistema).
DEV_SUPER = {u.strip().lower() for u in (os.getenv("DEV_SUPER") or "").split(",")
             if u.strip()}
MSG_VISOR = ("Modo observador: puedes ver cómo funciona, pero tu cuenta aún no "
             "crea ni cambia nada.")


def _zona_ok(zona):
    z = (zona or "").strip().lower()
    return z if z in ZONAS else ZONA_BASE


def _almacen_zona(zona, tipo):
    """'UBAT2/Recupero', 'GACH2/Stock'… — tipo = 'Stock' (despacho) | 'Recupero'."""
    return f'{ZONAS[_zona_ok(zona)]["bodega"]}/{tipo}'


def _cred_zona(zona):
    """Con qué cuenta de SGS escribe la zona, como kwargs de PlaywrightClient.
    Zona base → {} (la cuenta de config.py, igual que siempre).
    Otra zona → SGS_LOGIN_<ZONA>/SGS_PASSWORD_<ZONA>; sin ellas → None."""
    z = _zona_ok(zona)
    if z == ZONA_BASE:
        return {}
    login = os.getenv(f"SGS_LOGIN_{z.upper()}", "").strip()
    clave = os.getenv(f"SGS_PASSWORD_{z.upper()}", "")
    return {"sgs_login": login, "sgs_password": clave} if login and clave else None


def _zona_operativa(zona):
    return _cred_zona(zona) is not None
# Token servidor-a-servidor: el cuadre de Materiales Sony (módulo Balance)
# pide las hojas validadas por BAN con la cabecera X-Dev-Token. Sin token
# configurado el endpoint queda apagado (503).
DEV_API_TOKEN = os.getenv("DEV_API_TOKEN", "").strip()


def _token_ok():
    import hmac
    t = request.headers.get("X-Dev-Token", "")
    return bool(DEV_API_TOKEN) and hmac.compare_digest(t, DEV_API_TOKEN)


def _cargar_usuarios():
    out = {}
    for item in (os.getenv("DEV_USERS") or "").split(";"):
        # nombre:clave:rol — la clave puede ser un hash werkzeug, que trae ":"
        # ("scrypt:32768:8:1$..."): el rol es lo que va tras el ÚLTIMO ":"
        nombre, _, resto = item.strip().partition(":")
        clave, _, rol = resto.rpartition(":")
        if nombre and clave:
            # rol@zona ("visor@gacheta"); sin @ → zona base (Ubaté)
            rol, _, zona = rol.strip().lower().partition("@")
            out[nombre.strip().lower()] = {"clave": clave,
                                           "rol": rol if rol in ROLES else "visor",
                                           "zona": _zona_ok(zona)}
    return out


_USUARIOS = _cargar_usuarios()
# Usuarios cuyas sesiones hacen ADICIONALES DE TODAS LAS HOJAS, sin la lista de
# supervisores autorizados (2026-10-05: hay perfiles que fotografían todos los casos y
# hacen adicionales a todos). DEV_ADIC_TODOS="usuario1,usuario2". La marca se guarda en
# la sesión al crearla (adic_todos) y un admin la puede cambiar por sesión.
DEV_ADIC_TODOS = {u.strip().lower() for u in (os.getenv("DEV_ADIC_TODOS") or "").split(",")
                  if u.strip()}
_login_fails = {}   # ip → [intentos, bloqueado_hasta]


def _clave_ok(guardada, dada):
    if guardada.startswith(("scrypt:", "pbkdf2:")):
        return check_password_hash(guardada, dada or "")
    import hmac
    return hmac.compare_digest(guardada, dada or "")


def _usuario():
    """Usuario actual: {nombre, rol, zona}. Sin auth (local) → DEV_LOCAL_USER/ROL/ZONA.
    Rol y zona salen de DEV_USERS en cada petición, no de la cookie (que dura 30
    días): subir un visor a admin, bajarlo o quitar la cuenta en start.sh aplica
    al reiniciar, sin que nadie tenga que volver a entrar."""
    if not DEV_REQUIRE_AUTH:
        return {"nombre": os.getenv("DEV_LOCAL_USER", ""),
                "rol": os.getenv("DEV_LOCAL_ROL", "admin"),
                "zona": _zona_ok(os.getenv("DEV_LOCAL_ZONA"))}
    # SGO7 (puerta única): nginx ya validó la sesión de sabogal; el usuario llega en
    # cabeceras X-SGO7-* que solo nginx puede poner (esta app escucha en 127.0.0.1).
    su = request.headers.get("X-SGO7-User")
    if su:
        rol_sgo7 = request.headers.get("X-SGO7-Rol", "")
        return {"nombre": su,
                "rol": "admin" if rol_sgo7 in ("desarrollador", "supervisor", "directivo") else "almacen",
                "zona": _zona_ok(None)}
    u = session.get("usuario")
    reg = _USUARIOS.get(u) if u else None
    return {"nombre": u, "rol": reg["rol"], "zona": reg["zona"]} if reg else None


def _ve_zona(u, zona):
    """¿El usuario puede abrir sesiones de esa zona? La suya siempre; el dueño
    (DEV_SUPER) todas; un visor además las de la zona base, que es su vitrina."""
    z = _zona_ok(zona)
    if (u.get("nombre") or "").lower() in DEV_SUPER or z == u.get("zona", ZONA_BASE):
        return True
    return u.get("rol") == "visor" and z == ZONA_BASE


_ZONA_SID = {}      # sid → zona (no cambia tras crear la sesión; lo llena _cargar)


def _zona_sid(sid):
    z = _ZONA_SID.get(sid)
    if z is None:
        try:
            _cargar(sid)                 # llena la caché
        except Exception:
            return ZONA_BASE             # sesión inexistente: el endpoint dará su 404
        z = _ZONA_SID.get(sid, ZONA_BASE)
    return z


def _cred_sid(sid):
    """kwargs de PlaywrightClient para ESCRIBIR en SGS desde esta sesión."""
    z = _zona_sid(sid)
    cred = _cred_zona(z)
    if cred is None:
        raise RuntimeError(f"La zona {ZONAS[z]['nombre']} aún no tiene credenciales "
                           f"de SGS configuradas — no se escribe nada")
    return cred


def _zona_bloqueada(sid):
    """409 si la zona de la sesión todavía no tiene cuenta de SGS para escribir."""
    z = _zona_sid(sid)
    if _zona_operativa(z):
        return None
    return jsonify({"error": f"La zona {ZONAS[z]['nombre']} aún no tiene credenciales "
                             f"de SGS: desde sus sesiones no se escribe nada"}), 409


def _es_admin():
    u = _usuario()
    return bool(u) and u["rol"] == "admin"


def _solo_admin():
    """Guardia de los endpoints que escriben en SGS o entrenan el OCR."""
    if _es_admin():
        return None
    return jsonify({"error": "Solo un perfil admin puede hacer esto"}), 403


@app.before_request
def _sgo7_sesion():
    # Deja también el usuario SGO7 en la cookie de esta app (algunas rutas lo leen de session).
    su = request.headers.get("X-SGO7-User")
    if su and session.get("usuario") != su:
        session["usuario"] = su
        session["rol"] = "admin" if request.headers.get("X-SGO7-Rol") in ("desarrollador", "supervisor", "directivo") else "almacen"


@app.before_request
def _guard():
    if not DEV_REQUIRE_AUTH:
        return
    pth = request.path
    if pth in ("/login", "/logout") or pth.startswith("/static-dev/"):
        return
    if pth == "/api/dev/hojas-por-ban" and _token_ok():
        return
    u = _usuario()
    if not u:
        if pth.startswith("/api/"):
            return jsonify({"error": "no autenticado"}), 401
        return redirect("/login?next=" + pth)
    if u["rol"] == "almacen" and pth != "/" and not pth.startswith(_RUTAS_ALMACEN):
        return redirect("/almacen")
    if u["rol"] == "visor":
        # Lista blanca: solo LEER el panel. Fuera quedan también los GET que
        # crean algo (sesion-activa, cargue-sesion) o sacan datos (excel, debug).
        lee = request.method in ("GET", "HEAD")
        if not (lee and _VISOR_GET.match(pth)):
            if pth.startswith("/api/") or not lee:
                return jsonify({"error": MSG_VISOR}), 403
            return redirect("/?pc=1")
    m = _RE_SID.match(pth)
    if m and not _ve_zona(u, _zona_sid(m.group(1))):
        return jsonify({"error": "Esa sesión es de otra zona"}), 403


_VISOR_GET = re.compile(r"^/(?:$|robots\.txt$|api/dev/(?:info|sesiones|buscar)$"
                        r"|api/dev/sesion/[^/]+(?:/foto/\d+)?$)")
_RE_SID = re.compile(r"^/(?:api/dev/sesion|almacen/cargue-sesion)/([^/]+)")


_HTML_LOGIN = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow"><title>Acceso</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;font-family:system-ui,Segoe UI,sans-serif;color:#e2e8f0;padding:16px;box-sizing:border-box}
form{background:#1e293b;border:1px solid #334155;border-radius:14px;padding:28px;width:100%;max-width:340px;box-sizing:border-box}
h1{font-size:18px;margin:0 0 4px;color:#7dd3fc}small{color:#94a3b8}
label{display:block;font-size:12px;color:#94a3b8;margin:14px 0 4px}
input{width:100%;box-sizing:border-box;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:8px;padding:12px;font-size:16px}
button{width:100%;margin-top:18px;background:#38bdf8;color:#0f172a;border:0;border-radius:8px;padding:13px;font-weight:700;font-size:16px;cursor:pointer}
p.err{color:#f87171;margin:12px 0 0;font-size:13px}</style></head>
<body><form method="post" action="/login"><h1>Devoluciones y Almacén</h1><small>Entra con tu usuario</small>
<input type="hidden" name="next" value="{next}">
<label>Usuario</label><input name="usuario" autocomplete="username" autocapitalize="none" autofocus>
<label>Contraseña</label><input name="clave" type="password" autocomplete="current-password">
<button type="submit">Entrar</button>{err}</form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = request.values.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if not DEV_REQUIRE_AUTH:
        return redirect(nxt)
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    rec = _login_fails.get(ip, [0, 0])
    err = ""
    if request.method == "POST":
        if rec[1] > time.time():
            err = "Demasiados intentos. Espera unos minutos."
        else:
            nombre = (request.form.get("usuario") or "").strip().lower()
            u = _USUARIOS.get(nombre)
            if u and _clave_ok(u["clave"], request.form.get("clave")):
                session.clear()
                session["usuario"], session["rol"] = nombre, u["rol"]
                session.permanent = True
                _login_fails.pop(ip, None)
                log.info("Login OK: %s (%s · %s) desde %s", nombre, u["rol"], u["zona"], ip)
                if u["rol"] == "almacen" and nxt in ("/", "/?pc=1"):
                    nxt = "/almacen"          # su puerta de entrada
                if u["rol"] == "visor":
                    nxt = "/?pc=1"            # solo tiene el panel, también en el celular
                return redirect(nxt)
            rec[0] += 1
            if rec[0] >= 6:
                rec[1], rec[0] = time.time() + 300, 0
            _login_fails[ip] = rec
            time.sleep(1)
            err = "Usuario o contraseña incorrectos."
    html = _HTML_LOGIN.replace("{next}", nxt.replace('"', "")) \
                      .replace("{err}", f'<p class="err">{err}</p>' if err else "")
    return Response(html, mimetype="text/html", status=401 if err else 200)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.after_request
def _sin_indexar(resp):
    """Plataforma privada: ningún buscador debe indexarla, pase por el nginx que pase."""
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet")
    return resp


@app.get("/robots.txt")
def robots_txt():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

_LOCK = threading.Lock()          # protege lectura/escritura de session.json
_JOBS = {}                        # sid -> thread de procesamiento
_JOBS_LOCK = threading.Lock()     # arranque/cierre atómico del job de OCR (auto-OCR)


# ══════════════════════════════════════════════════════════════════════════
#  Certificado autofirmado (la cámara del celular exige HTTPS)
# ══════════════════════════════════════════════════════════════════════════

def _ips_locales():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except Exception:
        pass
    ips.discard("127.0.0.1")

    def _prioridad(ip):
        # Preferir la LAN doméstica para la URL del celular
        if ip.startswith("192.168.56."):   # host-only de VirtualBox: al final
            return (3, ip)
        if ip.startswith("192.168.") or ip.startswith("10."):
            return (0, ip)
        if ip.startswith("172."):
            return (1, ip)
        return (2, ip)                     # Tailscale (100.x) y demás

    return sorted(ips, key=_prioridad)


def asegurar_certificado():
    cert_p = os.path.join(DIR_CERTS, "cert.pem")
    key_p  = os.path.join(DIR_CERTS, "key.pem")
    if os.path.exists(cert_p) and os.path.exists(key_p):
        return cert_p, key_p

    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    log.info("Generando certificado autofirmado (primera vez)...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "devoluciones-sgs.local")])
    sans = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    for ip in _ips_locales():
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    cert = (
        x509.CertificateBuilder()
        .subject_name(nombre).issuer_name(nombre)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow() - timedelta(days=1))
        .not_valid_after(datetime.utcnow() + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(key_p, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    with open(cert_p, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    log.info("Certificado generado en %s", DIR_CERTS)
    return cert_p, key_p


# ══════════════════════════════════════════════════════════════════════════
#  Sesiones (almacenamiento en disco, sincronizadas celular ↔ PC)
# ══════════════════════════════════════════════════════════════════════════

def _dir_sesion(sid):
    return os.path.join(DIR_SESIONES, sid)


def _path_json(sid):
    return os.path.join(_dir_sesion(sid), "session.json")


def _cargar(sid):
    with open(_path_json(sid), encoding="utf-8") as f:
        d = json.load(f)
    if sid not in _ZONA_SID:
        _ZONA_SID[sid] = _zona_ok(d.get("zona"))     # sesiones viejas = zona base
    return d


def _guardar(sid, data):
    data["actualizada"] = datetime.now().isoformat(timespec="seconds")
    tmp = _path_json(sid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path_json(sid))


def crear_sesion(origen="pc", usuario=None):
    usuario = usuario or _usuario() or {"nombre": "?", "rol": "admin"}
    with _LOCK:
        sid = datetime.now().strftime("%Y%m%d-%H%M%S")
        os.makedirs(os.path.join(_dir_sesion(sid), "fotos"), exist_ok=True)
        data = {
            "id": sid,
            "creada": datetime.now().isoformat(timespec="seconds"),
            "origen": origen,
            "creada_por": usuario["nombre"],   # perfil que la abrió (admin o almacén)
            "rol_creador": usuario["rol"],
            "zona": _zona_ok(usuario.get("zona")),   # decide bodega y cuenta SGS
            "estado": "captura",       # captura | procesando | procesado | error | cerrada
            "siguiente_n": 1,
            "fotos": [],               # [{n, archivo, origen, ts}]
            "progreso": {"actual": 0, "total": 0, "msg": ""},
            "hojas": [],               # resultados OCR + verificación por foto
            "auto_ocr": OCR_AUTO,      # procesar cada foto al llegar (sin pulsar «Procesar»)
            # adicionales de TODOS los supervisores (no solo la lista autorizada)
            "adic_todos": (usuario.get("nombre") or "").lower() in DEV_ADIC_TODOS,
        }
        _guardar(sid, data)
        log.info("Sesión creada: %s (origen=%s)", sid, origen)
        return data


def listar_sesiones():
    out = []
    for sid in sorted(os.listdir(DIR_SESIONES), reverse=True):
        try:
            d = _cargar(sid)
            hojas_reales = [h for h in d["hojas"] if h.get("ocr") and not h.get("error")
                            and not h.get("continuacion_de") and not h.get("duplicada_de")]
            out.append({"id": d["id"], "creada": d["creada"], "estado": d["estado"],
                        "n_fotos": len(d["fotos"]), "n_hojas": len(d["hojas"]),
                        "creada_por": d.get("creada_por", ""), "rol_creador": d.get("rol_creador", ""),
                        "zona": _zona_ok(d.get("zona")),
                        "n_validadas": len([h for h in hojas_reales if h.get("validada")]),
                        "n_reales": len(hojas_reales)})
        except Exception:
            continue
    return out


def sesion_activa(origen, usuario=None):
    """La sesión de captura abierta más reciente de hoy DEL USUARIO; si no
    hay, crea una. Almacén y administrador nunca comparten la sesión activa del
    celular (cada uno fotografía en la suya)."""
    usuario = usuario or _usuario() or {"nombre": "?", "rol": "admin"}
    hoy = datetime.now().strftime("%Y%m%d")
    with _LOCK:
        for sid in sorted(os.listdir(DIR_SESIONES), reverse=True):
            if not sid.startswith(hoy):
                continue
            try:
                d = _cargar(sid)
            except Exception:
                continue
            creador = d.get("creada_por")
            mia = (creador == usuario["nombre"]) or (not creador and usuario["rol"] == "admin")
            if mia and d["estado"] in ("captura", "procesado", "error"):
                return d
    return crear_sesion(origen, usuario)


# ══════════════════════════════════════════════════════════════════════════
#  OCR con la API de Claude (visión + salida estructurada)
# ══════════════════════════════════════════════════════════════════════════

# Los textos NO son nullables (cadena vacía = no aplica/ilegible) para no superar
# el límite de la API de 16 parámetros con uniones; solo los números admiten null.
_STR_N = {"type": "string"}
_NUM_N = {"anyOf": [{"type": "number"}, {"type": "null"}]}

SCHEMA_OCR = {
    "type": "object",
    "properties": {
        "es_hoja_bandeja": {"type": "boolean"},
        "es_continuacion": {"type": "boolean"},
        "ban_code": _STR_N,
        "pro_code": _STR_N,
        "proyecto_desc": _STR_N,
        "asignada_a": {"type": "array", "items": {"type": "string"}},
        "consecutivo_naranja": _STR_N,
        "fecha_despachado": _STR_N,
        "fecha_recibido": _STR_N,
        "materiales": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "codigo_dom": _STR_N,
                    "ref_interna": _STR_N,
                    "descripcion": {"type": "string"},
                    "solicitado": _NUM_N,
                    "entregado": _NUM_N,
                    "devolucion_manuscrita": _NUM_N,
                    "devolucion_texto": _STR_N,
                    "devolucion": _NUM_N,
                    "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
                    "nota": _STR_N,
                },
                "required": ["codigo_dom", "ref_interna", "descripcion", "solicitado",
                             "entregado", "devolucion_manuscrita", "devolucion_texto",
                             "devolucion", "confianza", "nota"],
                "additionalProperties": False,
            },
        },
        "notas_manuscritas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string"},
                    "ref_interna": _STR_N,
                    "cantidad": _NUM_N,
                    "tipo": {"type": "string", "enum": ["devolucion", "adicional", "duda"]},
                    "interpretacion": _STR_N,
                },
                "required": ["texto", "ref_interna", "cantidad", "tipo", "interpretacion"],
                "additionalProperties": False,
            },
        },
        "confianza_global": {"type": "string", "enum": ["alta", "media", "baja"]},
        "advertencias": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["es_hoja_bandeja", "es_continuacion", "ban_code", "pro_code",
                 "proyecto_desc", "asignada_a",
                 "consecutivo_naranja", "fecha_despachado", "fecha_recibido", "materiales",
                 "notas_manuscritas", "confianza_global", "advertencias"],
    "additionalProperties": False,
}

PROMPT_OCR = """Eres un OCR experto de hojas de "bandeja de salida" de DOMINION COLOMBIA SAS \
impresas desde SGS (Odoo) y anotadas a mano por cuadrillas eléctricas en campo (zona Ubaté).

ESTRUCTURA DE LA HOJA:
- Cabecera izquierda: código de bandeja BAN-XXXXXX y las etiquetas "Proyecto:", "LCL:", \
"Asignada a:", "Supervisor:".
- Cabecera derecha: código de proyecto PRO-XXXXXX, debajo la descripción del proyecto/NODO \
y debajo UNO O DOS nombres de personas (líderes de cuadrilla). El primero corresponde a \
"Asignada a" y el segundo a "Supervisor".
- Sticker naranja con un número manuscrito: consecutivo interno (consecutivo_naranja).
- Tabla "Materiales Solicitados". Cada fila: [DOM-XXXXXX/RRRRRR] Descripción del material. \
Columnas: Solicitado (impreso), Entregado (impreso, casi siempre con un chulo ✓ manuscrito), \
"Consu mido" (número MANUSCRITO) y UdM.
- ⚠ CLAVE DEL NEGOCIO: aunque la columna impresa se titula "Consu mido", lo que la cuadrilla \
escribe a mano ahí es LA CANTIDAD QUE DEVUELVE al almacén (la devolución), NO el consumo. \
El chulo ✓ sobre Entregado solo confirma la entrega, no es una cantidad.
- Las cantidades impresas usan coma decimal (ej. "6,0" = 6.0).
- MANUSCRITO TÍPICO DE LAS CUADRILLAS (aprendido de cientos de hojas revisadas a mano): \
el dígito 1 lo escriben como un trazo vertical con base o serifa (⊥, T, L); ese trazo en la \
columna "Consu mido" ES un 1 (devolución de 1 unidad), no un chulo. Un chulo ✓ SOLO (sin \
dígito) no es una cantidad: equivale a "sin anotación". Un trazo con gancho o barra superior \
(7 vs 1) sí es ambiguo: transcríbelo como "7/1".
- OJO: la descripción puede ocupar dos renglones y las cantidades quedan desplazadas medio \
renglón respecto al texto. Alinea con máximo cuidado cada cantidad con SU material; este es \
el error más común.
- Al pie: firmas, sellos DESPACHADO / RECIBIDO con fecha, y a veces NOTAS MANUSCRITAS del tipo \
"213341 = 2" o "4320 = 6 Dev". El número antes del "=" es la referencia interna RRRRRR (la \
parte después de la barra en [DOM-XXXXXX/RRRRRR], a veces abreviada a sus últimos dígitos) y \
el número después es la CANTIDAD DEVUELTA de ese material.

TU TAREA:
1. Cabecera: ban_code (formato BAN-######), pro_code (formato PRO-######), proyecto_desc, \
asignada_a (lista de nombres de la cabecera derecha, en orden), consecutivo_naranja, y fechas \
de los sellos si son legibles (formato DD/MM/YYYY).
2. Extrae TODAS las filas de la tabla, incluso si Consumido está vacío. codigo_dom = "DOM-XXXXXX", \
ref_interna = "RRRRRR".
3. devolucion_manuscrita: SOLO el número manuscrito de la columna "Consu mido" (que en \
realidad es la cantidad A DEVOLVER). Si hay tachones, fracciones o trazos ambiguos, \
transcribe el trazo bruto en devolucion_texto y pon en devolucion_manuscrita el número más \
probable (o null si es realmente ilegible).
4. devolucion (valor final por fila):
   - Si entregado = 0 → devolucion = 0 (no se entregó nada, no hay nada que devolver).
   - Si devolucion_manuscrita es legible y ≤ entregado → devolucion = devolucion_manuscrita.
   - Si devolucion_manuscrita > entregado → devolucion = null y confianza baja (es imposible \
devolver más de lo entregado; algo está mal leído o mal anotado).
   - Si NO hay número manuscrito y entregado > 0 → devolucion = 0 con confianza media y nota \
"sin anotación manuscrita: se asume sin devolución — confirmar" (SALVO que una nota del pie \
dé la devolución de esa referencia: en ese caso úsala, con confianza media).
   - Si el trazo es ilegible → devolucion = null y confianza baja.
5. Notas manuscritas del pie/esquinas: transcríbelas TODAS y CLASIFICA cada una en "tipo":
   - "devolucion": incluye la palabra "Dev"/"D"/"devolución" (ej. "4320 = 6 Dev"): devolver \
N unidades de esa referencia. SOLO estas se cruzan con la columna manuscrita de la tabla; \
si complementan o contradicen lo anotado en un material, explícalo en interpretacion y en \
la nota del material, y baja su confianza a "media" (o "baja" si no se resuelve).
   - "adicional": referencias con cantidad SIN la palabra Dev (ej. "170856 = 3" en una \
esquina, a veces junto al "POR:" de un sello): la cuadrilla PIDE UN DESPACHO ADICIONAL de \
N unidades de esa referencia. NO son devoluciones — NO ajustes la tabla con ellas.
   - "duda": si el contexto no deja claro si es devolución o adicional (p. ej. la referencia \
coincide con un material que tiene tachones en su columna), clasifícala "duda" y explica.
6. confianza por material:
   - alta: datos impresos claros Y devolución manuscrita inequívoca (o entregado = 0).
   - media: legible pero con alguna duda (trazo dudoso, fracción, corrección, posible \
desalineación de fila, sin anotación y se asumió devolución 0, o nota del pie que ajusta \
el valor).
   - baja: ilegible, ambiguo, contradicción sin resolver, o devolución mayor que lo entregado.
7. confianza_global: refleja el estado general de la hoja. advertencias: lista todo lo que un \
humano deba revisar manualmente.
8. Si la imagen NO es una hoja de bandeja de salida SGS, devuelve es_hoja_bandeja = false y \
deja el resto en cadenas vacías / listas vacías.
9. HOJA DE CONTINUACIÓN (página 2+ de una bandeja grande): cuando una bandeja tiene muchos \
materiales, SGS la imprime en DOS o más hojas. Las páginas siguientes NO traen el bloque de \
cabecera (no hay BAN-, ni "Proyecto:", ni "LCL:", ni "Asignada a:"): solo el membrete \
"DOMINION COLOMBIA SAS / NIT" arriba y la tabla de materiales que CONTINÚA con el mismo \
formato de filas; suelen rematar con las notas manuscritas, firmas y sellos \
DESPACHADO/RECIBIDO del pie (que en una bandeja de 2 páginas quedan en la ÚLTIMA). Para \
estas páginas devuelve es_hoja_bandeja = true y es_continuacion = true, con ban_code, \
pro_code, proyecto_desc y asignada_a en vacío, y extrae la tabla y las notas manuscritas \
EXACTAMENTE igual que en una hoja normal (mismas reglas de devolución).
⚠ es_continuacion = true SOLO cuando el bloque de cabecera NO EXISTE en la imagen. Si la \
cabecera está presente pero borrosa o ilegible, es_continuacion = false con ban_code = "" \
(un humano la corregirá — NO la confundas con una continuación). En una hoja normal con \
cabecera, es_continuacion = false.

REGLA DE FORMATO: en los campos de TEXTO usa cadena vacía "" cuando el dato no aplica o es \
ilegible (nunca null). En los campos NUMÉRICOS usa null cuando el número no existe o es ilegible.

Sé conservador: ante la duda, baja la confianza. Es preferible un amarillo/rojo de más que \
una devolución mal digitada en SGS.

Responde ÚNICAMENTE con un objeto JSON válido, sin texto adicional, con EXACTAMENTE esta \
estructura (mismos nombres de campos):
{"es_hoja_bandeja": bool, "es_continuacion": bool, "ban_code": str, "pro_code": str, "proyecto_desc": str,
 "asignada_a": [str], "consecutivo_naranja": str, "fecha_despachado": str, "fecha_recibido": str,
 "materiales": [{"codigo_dom": str, "ref_interna": str, "descripcion": str,
   "solicitado": num|null, "entregado": num|null, "devolucion_manuscrita": num|null,
   "devolucion_texto": str, "devolucion": num|null, "confianza": "alta"|"media"|"baja",
   "nota": str}],
 "notas_manuscritas": [{"texto": str, "ref_interna": str, "cantidad": num|null,
   "tipo": "devolucion"|"adicional"|"duda", "interpretacion": str}],
 "confianza_global": "alta"|"media"|"baja", "advertencias": [str]}"""

# ── Modo COMPACTO: solo lo que SGS no tiene (cabecera + ref y números por fila) ──
SCHEMA_OCR_COMPACTO = {
    "type": "object",
    "properties": {
        "es_hoja_bandeja": {"type": "boolean"},
        "es_continuacion": {"type": "boolean"},
        "ban_code": _STR_N, "pro_code": _STR_N, "proyecto_desc": _STR_N,
        "asignada_a": {"type": "array", "items": {"type": "string"}},
        "consecutivo_naranja": _STR_N, "fecha_despachado": _STR_N, "fecha_recibido": _STR_N,
        "filas": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "ref": _STR_N, "ent": _NUM_N, "man": _NUM_N, "txt": _STR_N,
                "conf": {"type": "string", "enum": ["alta", "media", "baja"]},
                "nota": _STR_N},
            "required": ["ref", "ent", "man", "txt", "conf", "nota"],
            "additionalProperties": False}},
        "notas": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "texto": {"type": "string"}, "ref": _STR_N, "cant": _NUM_N,
                "tipo": {"type": "string", "enum": ["devolucion", "adicional", "duda"]},
                "interp": _STR_N},
            "required": ["texto", "ref", "cant", "tipo", "interp"],
            "additionalProperties": False}},
        "confianza_global": {"type": "string", "enum": ["alta", "media", "baja"]},
        "advertencias": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["es_hoja_bandeja", "es_continuacion", "ban_code", "pro_code", "proyecto_desc",
                 "asignada_a", "consecutivo_naranja", "fecha_despachado", "fecha_recibido",
                 "filas", "notas", "confianza_global", "advertencias"],
    "additionalProperties": False,
}

# Misma descripción de la hoja que el prompt completo; cambia SOLO la tarea/salida.
PROMPT_OCR_COMPACTO = PROMPT_OCR.split("TU TAREA:")[0] + """TU TAREA — SALIDA COMPACTA. Los datos IMPRESOS de cada material (código DOM, descripción, cantidad solicitada) ya los tiene el sistema en SGS: NO los transcribas. Lee la cabecera y, por cada fila, SOLO la referencia y los números.
1. Cabecera: ban_code (formato BAN-######), pro_code (formato PRO-######), proyecto_desc, asignada_a (nombres de la cabecera derecha, en orden), consecutivo_naranja, y fechas de los sellos si son legibles (DD/MM/YYYY).
2. filas: UNA entrada por CADA fila de la tabla, en orden, incluso si Consumido está vacío:
   - ref: la referencia interna RRRRRR, lo que va DESPUÉS de la barra en [DOM-XXXXXX/RRRRRR]. Es la llave con la que el sistema cruza la fila contra SGS: léela dígito a dígito.
   - ent: cantidad IMPRESA de la columna Entregado (número; "6,0" = 6).
   - man: SOLO el número MANUSCRITO de la columna "Consu mido" (= la cantidad a DEVOLVER). null si no hay número o es ilegible. Un chulo ✓ solo NO es un número (man null, txt ""). Un trazo vertical con base (⊥/T/L) SÍ es un 1: man = 1, txt = "⊥", conf media.
   - txt: el trazo bruto cuando hay tachones, fracciones o ambigüedad ("" si es limpio o no hay). Usa "7/1" para el gancho ambiguo y "chulo" si solo hay chulo y dudas.
   - conf: alta (impreso claro y manuscrito inequívoco, o sin manuscrito), media (trazo dudoso, corrección, posible desalineación de fila), baja (ilegible, contradictorio, o man > ent: es imposible devolver más de lo entregado, algo se leyó mal).
   - nota: "" salvo que haya algo que un humano deba revisar en ESA fila (breve).
   OJO: la descripción puede ocupar dos renglones y las cantidades quedan desplazadas medio renglón respecto al texto. Alinear cada número con SU ref es el error más común: verifica fila por fila.
3. notas: TODAS las notas manuscritas del pie/esquinas, clasificadas en tipo:
   - "devolucion": incluye la palabra "Dev"/"D"/"devolución" (ej. "4320 = 6 Dev") → devolver N unidades de esa ref.
   - "adicional": ref con cantidad SIN la palabra Dev (ej. "170856 = 3", a veces junto al "POR:" de un sello) → la cuadrilla PIDE UN DESPACHO ADICIONAL. NO es devolución.
   - "duda": si el contexto no aclara.
   ref = los dígitos antes del "=" (a veces abreviados a los últimos dígitos); cant = el número después; interp = interpretación breve. El sistema cruza estas notas con las filas por su cuenta: NO modifiques man por una nota.
4. confianza_global refleja la hoja en general; advertencias lista todo lo que un humano deba revisar a mano.
5. Si la imagen NO es una hoja de bandeja de salida SGS: es_hoja_bandeja = false y el resto vacío.
6. HOJA DE CONTINUACIÓN (página 2+ de una bandeja grande): SGS imprime las bandejas largas en DOS o más hojas; las páginas siguientes NO traen el bloque de cabecera (no hay BAN-, ni "Proyecto:", ni "LCL:", ni "Asignada a:"), solo el membrete "DOMINION COLOMBIA SAS / NIT" y la tabla que CONTINÚA, y suelen rematar con notas, firmas y sellos. Para ellas: es_hoja_bandeja = true, es_continuacion = true, cabecera vacía, y filas/notas igual que siempre.
⚠ es_continuacion = true SOLO si el bloque de cabecera NO EXISTE en la imagen. Cabecera presente pero borrosa → es_continuacion = false con ban_code = "" (un humano la corregirá).

REGLA DE FORMATO: textos vacíos "" (nunca null); números null cuando no existen o son ilegibles.
Sé conservador: ante la duda, baja la confianza.

Responde ÚNICAMENTE con un objeto JSON válido con EXACTAMENTE esta estructura:
{"es_hoja_bandeja": bool, "es_continuacion": bool, "ban_code": str, "pro_code": str,
 "proyecto_desc": str, "asignada_a": [str], "consecutivo_naranja": str,
 "fecha_despachado": str, "fecha_recibido": str,
 "filas": [{"ref": str, "ent": num|null, "man": num|null, "txt": str,
   "conf": "alta"|"media"|"baja", "nota": str}],
 "notas": [{"texto": str, "ref": str, "cant": num|null,
   "tipo": "devolucion"|"adicional"|"duda", "interp": str}],
 "confianza_global": "alta"|"media"|"baja", "advertencias": [str]}"""

_client_ia = None


def _anthropic_client():
    global _client_ia
    if _client_ia is None:
        _client_ia = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client_ia


def _extraer_json(texto):
    """Fallback: extrae el primer objeto JSON válido de un texto."""
    m = re.search(r"\{.*\}", texto, re.DOTALL)
    if not m:
        raise ValueError("La respuesta del modelo no contiene JSON")
    return json.loads(m.group(0))


def _kwargs_ocr_claude(img_b64):
    """Request de OCR (modo directo; el modo LOTE/Batch API se retiró el 2026-09-04).
    El prompt va como `system` con cache_control: es idéntico para todas las
    hojas de una sesión, así desde la 2ª se cobra al 10% (la imagen, que sí
    cambia, va después del prefijo cacheado)."""
    schema = SCHEMA_OCR_COMPACTO if OCR_MODO == "compacto" else SCHEMA_OCR
    return dict(
        model=MODELO_OCR,
        max_tokens=32000,          # el pensamiento del modelo también consume de aquí
        system=[{"type": "text", "text": _prompt_ocr_actual(),
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
            {"type": "text", "text": "Procesa esta hoja."},
        ]}],
        output_config={"format": {"type": "json_schema", "schema": schema},
                       "effort": OCR_EFFORT},
    )


def _ocr_hoja_claude(img_b64):
    """OCR con Claude (visión + salida estructurada json_schema)."""
    client = _anthropic_client()
    kwargs = _kwargs_ocr_claude(img_b64)

    def _llamar(kw):
        with client.messages.stream(**kw) as stream:
            return stream.get_final_message()

    try:
        resp = _llamar(kwargs)
        estructurado = True
    except anthropic.AuthenticationError:
        raise RuntimeError(
            "ANTHROPIC_API_KEY inválida o revocada — configurar una key válida "
            "(variable de entorno; en el servidor: start.sh)")
    except anthropic.BadRequestError as e:
        log.warning("output_config rechazado (%s); reintento sin salida estructurada", e)
        kw2 = dict(kwargs)
        kw2.pop("output_config", None)
        resp = _llamar(kw2)
        estructurado = False

    if resp.stop_reason == "refusal":
        raise RuntimeError("El modelo declinó procesar la imagen (refusal)")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Respuesta truncada (max_tokens) — reintentar")

    texto = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(texto) if estructurado else _extraer_json(texto)
    u = resp.usage
    data["_uso"] = {"backend": "claude", "modelo": MODELO_OCR, "modo": OCR_MODO,
                    "input": u.input_tokens, "output": u.output_tokens,
                    "cache_read": getattr(u, "cache_read_input_tokens", None),
                    "cache_write": getattr(u, "cache_creation_input_tokens", None)}
    return data


_KIMI_LOCK = threading.Lock()      # concurrencia máxima 1 de la cuenta Moonshot


def _ocr_hoja_kimi(img_b64):
    """OCR con Kimi (Moonshot, API estilo OpenAI). Sin esquema forzado: solo
    json_object + prompt; _normalizar_ocr() garantiza la forma después.
    Serializado (cuenta con concurrencia 1) y con reintentos ante 429."""
    body = {
        "model": KIMI_MODEL,
        # ⚠ Los Kimi son modelos razonadores: el pensamiento consume del MISMO
        # max_tokens (misma lección que con Claude). Con 16k una hoja de 42
        # filas quedaba truncada a la mitad del JSON.
        "max_tokens": 32000,
        "temperature": 1,                     # el modelo solo acepta 1
        "stream": True,                       # sin stream el gateway corta salidas largas
        "stream_options": {"include_usage": True},
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            {"type": "text", "text": _prompt_ocr_actual()},
        ]}],
    }
    t0 = time.time()
    with _KIMI_LOCK:
        ok = False
        for intento in range(10):
            trozos, usage, fin = [], {}, None
            with httpx.stream("POST", KIMI_URL, json=body,
                              headers={"Authorization": f"Bearer {KIMI_API_KEY}"},
                              timeout=httpx.Timeout(30.0, read=600.0)) as r:
                if r.status_code == 429:
                    r.read()
                    time.sleep(6 + 3 * intento)
                    continue
                if r.status_code == 401:
                    raise RuntimeError("KIMI_API_KEY inválida o revocada (401)")
                if r.status_code != 200:
                    r.read()
                    raise RuntimeError(f"Kimi HTTP {r.status_code}: {r.text[:200]}")
                for linea in r.iter_lines():
                    if not linea or not linea.startswith("data:"):
                        continue
                    dato = linea[5:].strip()
                    if dato == "[DONE]":
                        break
                    ev = json.loads(dato)
                    if ev.get("usage"):
                        usage = ev["usage"]
                    for ch in ev.get("choices") or []:
                        delta = (ch.get("delta") or {}).get("content")
                        if delta:
                            trozos.append(delta)
                        fin = ch.get("finish_reason") or fin
                ok = True
                break
        if not ok:
            raise RuntimeError("Kimi saturado (429 sostenido tras reintentos)")
    if fin == "length":
        raise RuntimeError("Respuesta de Kimi truncada (max_tokens)")
    if fin is None:
        # Visto en pruebas: el gateway de Moonshot corta streams muy largos sin
        # finish_reason ni usage → JSON a medias. Mejor fallback que basura.
        raise RuntimeError(f"Stream de Kimi cortado a medias ({len(''.join(trozos))} chars)")
    texto = "".join(trozos)
    try:
        data = json.loads(texto)
    except Exception:
        data = _extraer_json(texto)
    data["_uso"] = {"backend": "kimi", "modelo": KIMI_MODEL,
                    "input": usage.get("prompt_tokens"),
                    "output": usage.get("completion_tokens"),
                    "segundos": round(time.time() - t0)}
    return data


def _num_ocr(v):
    """Número tolerante: acepta int/float o texto tipo '6,0'; si no, None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v.strip().replace(",", "."))
        except ValueError:
            return None
    return None


# Patrones aprendidos de las correcciones humanas en producción (80 ediciones,
# sesiones 2026-09-03 → 09-16): el modelo describe el trazo pero no se atreve a
# decidir; el humano decidía SIEMPRE igual → la regla vive aquí, en servidor.
#   · "⊥ / tipo T / tipo L / trazo vertical con serifa" → el humano puso 1 (13/13)
#   · "solo chulo / checkmark"                          → el humano puso 0 (10/11)
#   · nota del pie "ref = N" SIN 'Dev' aplicada a la tabla → el humano puso 0 (3/3)
_RE_TRAZO_UNO = re.compile(
    r"(⊥|⅂|\btipo\s*['\"]?[TL]\b|\b[TL]\s*/\s*1\b|\b1\s*/\s*[TL]\b|\b[TL]\s+o\s+1\b"
    r"|\b1\s+o\s+[TL]\b|trazos?\s+vertical|serifa|similar\s+a\s+[TL]\b)", re.I)
_RE_CHULO = re.compile(r"(chulo|check\s*mark|checkmark|palomita|\bvisto\b|\btick\b|[✓✔])", re.I)
_RE_NOTA_DEV = re.compile(r"\bdev|devol|\bD\b\.?", re.I)   # "Dev", "D.", "devolución"


def _regla_trazo(txt, nota, ent):
    """Decide la devolución de una fila SIN número manuscrito a partir de la
    descripción del trazo (txt/nota del modelo). Devuelve (dev, conf, motivo,
    regla) o None si ninguna regla aplica."""
    s = f"{txt} {nota}"
    if not s.strip() or not isinstance(ent, (int, float)) or ent < 1:
        return None
    if _RE_TRAZO_UNO.search(s) and not re.search(r"[02-9]", txt) \
            and not re.search(r"\b7\s*/\s*1\b|\b1\s*/\s*7\b|\b7\b", s):
        return (1, "media", "trazo vertical tipo ⊥/T: así escriben el 1 las cuadrillas "
                            "→ se propone devolución 1 — confirmar", "trazo_uno")
    if txt.strip() and _RE_CHULO.search(txt) and not re.search(r"\d", txt):
        return (0, "media", "solo chulo ✓ (sin cifra): se asume sin devolución — confirmar",
                "chulo")
    return None


def _expandir_compacto(d):
    """Salida COMPACTA del modelo (filas: ref/ent/man/txt/conf/nota) → formato
    interno completo (materiales/notas_manuscritas) que usa todo el sistema.
    codigo_dom/descripcion/solicitado quedan vacíos: _consolidar_hoja los
    rellena desde SGS (la bandeja ya tiene lo impreso). La regla de devolución
    que antes decidía el modelo se aplica aquí, en servidor."""
    mats = []
    for f in (d.pop("filas", None) or []):
        if not isinstance(f, dict):
            continue
        ent = _num_ocr(f.get("ent"))
        man = _num_ocr(f.get("man"))
        txt = str(f.get("txt") or "")
        conf = f.get("conf")
        if conf not in ("alta", "media", "baja"):
            conf = "media"
        nota = str(f.get("nota") or "")
        regla = None
        if isinstance(ent, (int, float)) and ent == 0:
            dev = 0
        elif isinstance(man, (int, float)):
            dev = man                        # _consolidar_hoja valida man <= ent
        elif (r := _regla_trazo(txt, nota, ent)):
            dev, conf, motivo, regla = r
            nota = (nota + " | " if nota else "") + motivo
        elif txt.strip():
            dev, conf = None, "baja"         # hay trazo pero no se pudo leer
            nota = (nota + " | " if nota else "") + f"Trazo ilegible: '{txt}'"
        else:
            dev = 0                          # sin anotación → sin devolución (asumido)
            conf = "media" if conf == "alta" else conf
            nota = (nota + " | " if nota else "") + \
                   "sin anotación manuscrita: se asume sin devolución — confirmar"
        ref = str(f.get("ref") or "").strip()
        mref = re.match(r"^(\d{6})-\d+$", ref)
        if mref:                             # "251360-3": el sufijo es del código DOM
            ref = mref.group(1)
        m = {"codigo_dom": "", "ref_interna": ref,
             "descripcion": "", "solicitado": None, "entregado": ent,
             "devolucion_manuscrita": man, "devolucion_texto": txt,
             "devolucion": dev, "confianza": conf, "nota": nota}
        if regla:
            m["regla"] = regla               # la UI lo marca: valor propuesto por regla
        mats.append(m)
    notas = []
    for n in (d.pop("notas", None) or []):
        if not isinstance(n, dict):
            continue
        nt = {"texto": str(n.get("texto") or ""),
              "ref_interna": str(n.get("ref") or "").strip(),
              "cantidad": _num_ocr(n.get("cant")), "tipo": n.get("tipo"),
              "interpretacion": str(n.get("interp") or "")}
        # Regla del negocio aplicada en servidor (con effort bajo el modelo tiende a
        # dejar todo en "duda"): "RRRRRR = N" limpio y sin "Dev" ES adicional; con
        # "Dev" ES devolución. "duda" queda solo para notas con tachones/flechas/texto.
        limpio = re.match(r"^\W*(\d{3,})\s*=\s*(\d+(?:[.,]\d+)?)\W*$", nt["texto"])
        if nt["tipo"] == "duda" and limpio:
            nt["tipo"] = "adicional"
            nt["ref_interna"] = nt["ref_interna"] or limpio.group(1)
            nt["cantidad"] = nt["cantidad"] if nt["cantidad"] is not None else _num_ocr(limpio.group(2))
            nt["interpretacion"] = (nt["interpretacion"] + " | " if nt["interpretacion"] else "") +                                    "reclasificada por regla: ref = N sin 'Dev' → pedido adicional"
        elif nt["tipo"] == "duda" and re.search(r"\bdev", nt["texto"], re.I) \
                and re.search(r"\d{3,}\s*=\s*\d", nt["texto"]):
            nt["tipo"] = "devolucion"
            nt["interpretacion"] = (nt["interpretacion"] + " | " if nt["interpretacion"] else "") +                                    "reclasificada por regla: dice 'Dev'"
        elif nt["tipo"] == "devolucion" and not _RE_NOTA_DEV.search(nt["texto"]):
            # Sin la palabra Dev NO es devolución (regla del negocio): el modelo con
            # effort bajo a veces lo llama devolución y la tabla se contaminaba.
            nt["tipo"] = "adicional" if limpio else "duda"
            nt["interpretacion"] = ((nt["interpretacion"] + " | " if nt["interpretacion"] else "")
                                    + ("reclasificada por regla: sin 'Dev' → pedido adicional"
                                       if limpio else
                                       "reclasificada por regla: sin 'Dev' no es devolución → duda"))
        notas.append(nt)
    # Nota "Dev" del pie sobre una fila SIN manuscrito (misma regla que tenía el
    # prompt completo, ahora en servidor): solo si la ref es única y cabe en ent
    for nt in notas:
        ref, cant = nt["ref_interna"], nt["cantidad"]
        if nt.get("tipo") != "devolucion" or not ref or not isinstance(cant, (int, float)):
            continue
        obj = [m for m in mats if m["ref_interna"] == ref
               or (len(ref) >= 4 and m["ref_interna"].endswith(ref))]
        if len(obj) != 1:
            continue
        m = obj[0]
        ent = m["entregado"]
        if m["devolucion_manuscrita"] is None and isinstance(ent, (int, float)) \
                and 0 < cant <= ent:
            m["devolucion"], m["confianza"] = cant, "media"
            m["nota"] = f"Devolución tomada de la nota del pie: '{nt['texto']}'"
    d["materiales"], d["notas_manuscritas"] = mats, notas
    return d


def _normalizar_ocr(d):
    """Garantiza la forma del esquema aunque el backend no la imponga (Kimi usa
    json_object sin esquema): claves faltantes, números en texto, enums raros.
    Inofensivo para la salida ya-validada de Claude."""
    if not isinstance(d, dict):
        raise RuntimeError("El OCR no devolvió un objeto JSON")
    if "filas" in d and "materiales" not in d:
        d = _expandir_compacto(d)
    if d.get("es_continuacion") and not _es_ban_valido(d.get("ban_code")):
        # La página 2+ NO trae cabecera: lo que el modelo ponga ahí es ruido (p.ej.
        # un "BAN=..." manuscrito del pie tomado como sticker naranja)
        for k in ("ban_code", "pro_code", "proyecto_desc", "consecutivo_naranja"):
            d[k] = ""
        d["asignada_a"] = []
    d["es_hoja_bandeja"] = bool(d.get("es_hoja_bandeja"))
    d["es_continuacion"] = bool(d.get("es_continuacion"))
    for k in ("ban_code", "pro_code", "proyecto_desc", "consecutivo_naranja",
              "fecha_despachado", "fecha_recibido"):
        d[k] = str(d.get(k) or "").strip()
    d["asignada_a"] = [str(x).strip() for x in (d.get("asignada_a") or []) if x]
    if d.get("confianza_global") not in ("alta", "media", "baja"):
        d["confianza_global"] = "media"
    d["advertencias"] = [str(x) for x in (d.get("advertencias") or [])]
    mats = []
    for m in (d.get("materiales") or []):
        if not isinstance(m, dict):
            continue
        m["codigo_dom"] = str(m.get("codigo_dom") or "").strip()
        m["ref_interna"] = str(m.get("ref_interna") or "").strip()
        m["descripcion"] = str(m.get("descripcion") or "")
        for k in ("solicitado", "entregado", "devolucion_manuscrita", "devolucion"):
            m[k] = _num_ocr(m.get(k))
        m["devolucion_texto"] = str(m.get("devolucion_texto") or "")
        if m.get("confianza") not in ("alta", "media", "baja"):
            m["confianza"] = "media"
        m["nota"] = str(m.get("nota") or "")
        mats.append(m)
    d["materiales"] = mats
    notas = []
    for n in (d.get("notas_manuscritas") or []):
        if not isinstance(n, dict):
            continue
        n["texto"] = str(n.get("texto") or "")
        n["ref_interna"] = str(n.get("ref_interna") or "").strip()
        n["cantidad"] = _num_ocr(n.get("cantidad"))
        if n.get("tipo") not in ("devolucion", "adicional", "duda", "descartada"):
            n["tipo"] = "duda"
        n["interpretacion"] = str(n.get("interpretacion") or "")
        notas.append(n)
    d["notas_manuscritas"] = notas
    return d


def ocr_hoja(ruta_jpg):
    """Procesa una foto de hoja y devuelve el dict del esquema.
    Backend según OCR_BACKEND: 'kimi' (Moonshot, barato) con fallback automático
    a Claude si falla; 'claude' usa solo Claude."""
    with open(ruta_jpg, "rb") as f:
        img_b64 = base64.standard_b64encode(f.read()).decode()
    if OCR_BACKEND == "kimi" and KIMI_API_KEY:
        try:
            data = _normalizar_ocr(_ocr_hoja_kimi(img_b64))
            u = data.get("_uso") or {}
            log.info("OCR Kimi OK (%s): in=%s out=%s en %ss",
                     KIMI_MODEL, u.get("input"), u.get("output"), u.get("segundos"))
            return data
        except Exception as e:
            log.warning("OCR Kimi falló (%s) — fallback a Claude", e)
    return _normalizar_ocr(_ocr_hoja_claude(img_b64))


# ══════════════════════════════════════════════════════════════════════════
#  Verificación de seguridad contra SGS (SOLO LECTURA)
# ══════════════════════════════════════════════════════════════════════════

_sgs_client = None
_sgs_error  = None


class SGSAuthError(RuntimeError):
    """Fallo al autenticar (credenciales/servidor caído). NO es reintentable
    con un re-login — se distingue de los errores de sesión caducada, que sí."""


# Odoo caduca la sesión con las horas y el proceso vive días: re-login proactivo
_SGS_TTL = 900          # segundos de vida útil del cliente antes de renovarlo
_sgs_login_ts = 0.0


def _sgs(fresco=False):
    """Cliente SGS de solo lectura, autenticado perezosamente.
    Renueva la sesión si se pidió `fresco` o si superó su TTL."""
    global _sgs_client, _sgs_error, _sgs_login_ts
    if fresco or (_sgs_client is not None and time.time() - _sgs_login_ts > _SGS_TTL):
        _sgs_client = None
    if _sgs_client is not None:
        return _sgs_client
    try:
        c = SGSReadOnlyClient(cfg.SGS_URL, cfg.SGS_DB, cfg.SGS_USER, cfg.SGS_PASS)
        c.autenticar()
        _sgs_client = c
        _sgs_login_ts = time.time()
        _sgs_error = None
        log.info("[SGS] Sesión de lectura autenticada")
        return c
    except Exception as e:
        _sgs_error = f"No se pudo autenticar en SGS: {e}"
        raise SGSAuthError(_sgs_error)


def _sgs_op(fn):
    """Ejecuta fn(cliente) con UN re-login automático si la llamada falla.
    ⚠ El cliente interno lanza RuntimeError('SGS API error: Session expired'),
    por eso NO se puede usar RuntimeError para decidir si reintentar: solo
    SGSAuthError (fallo real de login) se propaga sin reintento."""
    global _sgs_client
    try:
        return fn(_sgs())
    except SGSAuthError:
        raise
    except Exception as e:
        _sgs_client = None         # no dejar cacheado un cliente envenenado
        log.warning("Llamada SGS falló (%s) — reintentando con re-login fresco", e)
        return fn(_sgs(fresco=True))


class _SGSAuto:
    """Proxy de SOLO LECTURA: cada llamada pasa por `_sgs_op`, así toda
    lectura reintenta con re-login si la sesión de Odoo caducó."""

    def __getattr__(self, nombre):
        if nombre.startswith("_"):
            raise AttributeError(nombre)

        def _llamar(*a, **kw):
            return _sgs_op(lambda cli: getattr(cli, nombre)(*a, **kw))
        return _llamar


_SGS = _SGSAuto()


def _norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", s.upper()).strip()


def _nombres_coinciden(a, b):
    """Coincidencia laxa de nombres de personas (orden y tokens parciales)."""
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    if not ta or not tb:
        return False
    comunes = ta & tb
    return len(comunes) >= 2 or ta <= tb or tb <= ta


def _leer_tray(c, ban):
    """Lee la bandeja por código con fallback si algún campo no existe.
    OJO: si TODOS los intentos fallan, LANZA el error — un fallo de conexión
    o sesión caducada jamás debe confundirse con 'la bandeja no existe'."""
    ultimo = None
    for fields in (["id", "name", "project_id", "partner_id", "project_tray_material_ids"],
                   ["id", "name", "project_id", "project_tray_material_ids"],
                   ["id", "name", "project_id"]):
        try:
            return c.search_read("project.tray", [["name", "=", ban]], fields, limit=2)
        except Exception as e:
            ultimo = e
            continue
    raise ultimo if ultimo else RuntimeError("project.tray sin respuesta")


def verificar_hoja_sgs(hoja_ocr):
    """
    Verificación de seguridad (solo lectura):
      1. BAN-XXXXXX existe en project.tray
      2. El proyecto de esa bandeja es el PRO-XXXXXX de la hoja
      3. El líder de la bandeja/equipo coincide con "Asignada a"
      4. Cada material con devolución salió en esa bandeja
    Regla dura: el material de BAN de líder A se devuelve a líder A →
    lider_sgs (lo que dice SGS) es la verdad; la hoja solo se contrasta.
    """
    v = {"disponible": True, "ban_existe": False, "tray_id": None, "project_id": None,
         "pro_coincide": None, "proyecto_sgs": None, "lider_sgs": None,
         "lider_coincide": None, "materiales_bandeja": [], "mensajes": [], "estado": "rojo"}

    try:
        c = _sgs()
    except Exception as e:
        return {"disponible": False, "mensajes": [str(e)], "estado": "amarillo"}

    ban = (hoja_ocr.get("ban_code") or "").strip().upper()
    pro = (hoja_ocr.get("pro_code") or "").strip().upper()
    if not re.match(r"^BAN-\d+$", ban):
        v["mensajes"].append(f"Código de bandeja ilegible o inválido: '{ban}'")
        return v

    # 1. Bandeja (con re-login automático si la sesión SGS caducó)
    try:
        trays = _sgs_op(lambda cli: _leer_tray(cli, ban))
        c = _SGS        # proxy con re-login automático
    except Exception as e:
        v["disponible"] = False
        v["estado"] = "amarillo"
        v["mensajes"].append(f"SGS no respondió (se intentó re-login): {e}")
        return v
    if not trays:
        v["mensajes"].append(f"{ban} NO existe en SGS")
        return v
    tray = trays[0]
    v["ban_existe"] = True
    v["tray_id"] = tray["id"]

    # 2. Proyecto de la bandeja vs PRO de la hoja
    pid = tray.get("project_id") and tray["project_id"][0]
    pdisplay = tray.get("project_id") and tray["project_id"][1] or ""
    v["project_id"] = pid
    v["proyecto_sgs"] = pdisplay
    v["url_sgs"] = _url_sgs_nodo(pid)
    if pro and re.match(r"^PRO-\d+$", pro):
        coincide = pro in _norm(pdisplay)
        if not coincide:
            try:
                p = c.buscar_por_codigo(pro)
                ids = []
                if isinstance(p, dict):
                    ids = [p.get("id")]
                elif isinstance(p, list):
                    ids = [x.get("id") for x in p if isinstance(x, dict)]
                coincide = pid in ids
            except Exception as e:
                v["mensajes"].append(f"No se pudo resolver {pro} en SGS: {e}")
                coincide = None
        v["pro_coincide"] = coincide
        if coincide is False:
            v["mensajes"].append(
                f"⚠ {ban} pertenece a '{pdisplay}' en SGS, NO al {pro} de la hoja")
    else:
        v["pro_coincide"] = None
        v["mensajes"].append("PRO ilegible en la hoja — no se pudo contrastar")

    # 3. Líder: partner de la bandeja; si no hay, equipo del proyecto
    lider_sgs = None
    if tray.get("partner_id"):
        lider_sgs = tray["partner_id"][1]
    if not lider_sgs and pid:
        try:
            equipo = c.search_read("project.team", [["project_id", "=", pid]],
                                   ["partner_id"], limit=10)
            nombres = [e["partner_id"][1] for e in equipo if e.get("partner_id")]
            lider_sgs = nombres[0] if nombres else None
            if len(nombres) > 1:
                v["lideres_equipo"] = nombres
        except Exception:
            pass
    v["lider_sgs"] = lider_sgs

    asignados = hoja_ocr.get("asignada_a") or []
    if lider_sgs and asignados:
        v["lider_coincide"] = any(_nombres_coinciden(lider_sgs, a) for a in asignados)
        if not v["lider_coincide"]:
            v["mensajes"].append(
                f"⚠ Líder en SGS: '{lider_sgs}' ≠ hoja: {asignados}. "
                f"La devolución SIEMPRE va al líder de la bandeja según SGS.")
    elif not lider_sgs:
        v["mensajes"].append("SGS no reporta líder para esta bandeja/proyecto")

    # 4. Materiales que salieron en esta bandeja
    mats_bandeja = []
    ids_tm = tray.get("project_tray_material_ids") or []
    if ids_tm:
        try:
            tm = c.search_read("project.tray.material", [["id", "in", ids_tm]],
                               ["product_id", "amount"], limit=500)
            tm = [t for t in tm if t.get("product_id")]
            mats_bandeja = [t["product_id"][1] for t in tm]
            v["materiales_bandeja_detalle"] = [{"display": t["product_id"][1],
                                                "amount": t.get("amount")} for t in tm]
        except Exception as e:
            v["mensajes"].append(f"No se pudieron leer materiales de la bandeja: {e}")
    v["materiales_bandeja"] = mats_bandeja

    # Estado de la hoja
    if v["ban_existe"] and v["pro_coincide"] is not False and v["lider_coincide"] is not False:
        v["estado"] = "verde" if (v["pro_coincide"] and v["lider_coincide"]) else "amarillo"
    else:
        v["estado"] = "rojo"
    return v


def _material_en_bandeja(mat, mats_bandeja_norm):
    """True/False/None: ¿el material OCR salió en la bandeja según SGS?"""
    if not mats_bandeja_norm:
        return None
    dom = _norm(mat.get("codigo_dom") or "")
    ref = (mat.get("ref_interna") or "").strip()
    for mb in mats_bandeja_norm:
        if dom and dom in mb:
            return True
        if ref and f"/{ref}]" in mb:
            return True
    return False


_RE_DISPLAY_SGS = re.compile(r"^\[([^/\]]+)/([^\]]+)\]\s*(.*)$")


def _rellenar_desde_sgs(m, ref, mats_display, detalle):
    """Modo compacto: el modelo no transcribe lo impreso; DOM, descripción y
    solicitado vienen del display SGS '[DOM-XXXXXX/RRRRRR] Descripción' + amount."""
    if not ref:
        return
    for i, disp in enumerate(mats_display):
        mm = _RE_DISPLAY_SGS.match(disp or "")
        if not mm or mm.group(2).strip() != ref:
            continue
        if not m.get("codigo_dom"):
            m["codigo_dom"] = mm.group(1).strip()
        if not m.get("descripcion"):
            m["descripcion"] = mm.group(3).strip()
        if m.get("solicitado") is None and i < len(detalle):
            amt = (detalle[i] or {}).get("amount")
            if isinstance(amt, (int, float)):
                m["solicitado"] = amt
        return


def _consolidar_hoja(ocr, verif):
    """Valida devoluciones, cruza con SGS y asigna color a cada material.
    Es re-ejecutable (tras ediciones humanas o reverificación) sin duplicar notas,
    y NUNCA pisa un valor corregido por el humano (fuente == "humano")."""
    mats_display = verif.get("materiales_bandeja") or []
    detalle = verif.get("materiales_bandeja_detalle") or []
    mats_norm = [_norm(m) for m in mats_display]
    hoja_mal = verif.get("estado") == "rojo"

    def _nota_add(m, txt):
        if txt not in (m.get("nota") or ""):
            m["nota"] = ((m.get("nota") or "") + " | " + txt).strip(" |")

    leidos = set()
    for m in ocr.get("materiales", []):
        ent = m.get("entregado")
        en_band = _material_en_bandeja(m, mats_norm)
        m["en_bandeja"] = en_band
        dom = _norm(m.get("codigo_dom") or "")
        ref = (m.get("ref_interna") or "").strip()
        for i, mb in enumerate(mats_norm):
            if (dom and dom in mb) or (ref and f"/{ref}]" in mb):
                leidos.add(i)
        if not m.get("codigo_dom") or not m.get("descripcion") or m.get("solicitado") is None:
            _rellenar_desde_sgs(m, ref, mats_display, detalle)

        if m.get("fuente") == "humano":
            # La corrección humana manda: no se recalcula desde el manuscrito
            dev = m.get("devolucion")
            if isinstance(ent, (int, float)) and isinstance(dev, (int, float)) and dev > ent:
                m["color"] = "rojo"
                _nota_add(m, f"Corrección humana {dev} > entregado {ent}: revisar")
            elif isinstance(dev, (int, float)) and dev > 0 and en_band is False:
                m["color"] = "rojo"
                _nota_add(m, "Este material NO salió en esta bandeja según SGS")
            elif hoja_mal:
                m["color"] = "rojo"
            else:
                m["color"] = "verde"
            m["consumido_calc"] = (round(ent - dev, 3)
                                   if isinstance(ent, (int, float)) and isinstance(dev, (int, float))
                                   else None)
            continue

        man = m.get("devolucion_manuscrita")
        # Regla del negocio: lo manuscrito en la columna "Consumido" ES la devolución.
        # El servidor la valida (no confía ciegamente en la decisión del OCR).
        if isinstance(ent, (int, float)) and ent == 0:
            m["devolucion"] = 0
        elif isinstance(man, (int, float)):
            if isinstance(ent, (int, float)) and man > ent:
                m["devolucion"] = None
                m["confianza"] = "baja"
                _nota_add(m, f"Manuscrito {man} > entregado {ent}: imposible devolver "
                             f"más de lo entregado — revisar a mano")
            else:
                if m.get("devolucion") != man:
                    _nota_add(m, f"Devolución tomada del manuscrito: {man}")
                m["devolucion"] = man
        else:
            # Sin manuscrito: el OCR ya decidió (0 asumido con nota, o null si ilegible)
            m["devolucion"] = m.get("devolucion") if isinstance(m.get("devolucion"), (int, float)) else None

        # Consumo de referencia (informativo): lo que la cuadrilla gastó
        if isinstance(ent, (int, float)) and isinstance(m.get("devolucion"), (int, float)):
            m["consumido_calc"] = round(ent - m["devolucion"], 3)
        else:
            m["consumido_calc"] = None

        # Semáforo
        if hoja_mal or m["confianza"] == "baja":
            m["color"] = "rojo"
        elif (m.get("devolucion") or 0) > 0 and en_band is False:
            m["color"] = "rojo"
            _nota_add(m, "Este material NO salió en esta bandeja según SGS")
        elif m["confianza"] == "media" or en_band is None:
            m["color"] = "amarillo"
        elif m.get("devolucion") is None:
            m["color"] = "rojo"
        else:
            m["color"] = "verde"

        if m.get("confirmado") and not hoja_mal:
            m["color"] = "verde"

    # Cobertura inversa: materiales que según SGS salieron en la bandeja
    # pero que el OCR no leyó en la foto
    advs = [a for a in ocr.get("advertencias", []) if not a.startswith("SGS: la bandeja incluye")]
    for i, disp in enumerate(mats_display):
        if i not in leidos:
            advs.append(f"SGS: la bandeja incluye {disp} y no se leyó en la foto — revisar")
    ocr["advertencias"] = advs
    return ocr


# ══════════════════════════════════════════════════════════════════════════
#  Preanálisis de devolución (SOLO LECTURA) — anti doble-devolución
#  Basado en la lógica validada del Liquidador (sgs-ui):
#    saldo devolvible = project.material.despachado − devuelto
#    bandejas válidas: state=approved (devolution ignora state_picking)
# ══════════════════════════════════════════════════════════════════════════

_ESTADOS_TRAY_MALOS = {"canceled", "canceled_partial", "to_approve"}


def _tray_activa(t):
    state = str(t.get("state") or "").lower()
    if state in _ESTADOS_TRAY_MALOS or state not in ("approved", "aprobado"):
        return False
    if str(t.get("type") or "").lower() == "devolution":
        return True
    sp = t.get("state_picking")
    if sp in (None, False):
        return True
    return str(sp).lower() in ("atendido", "parcial")


def _pm_match(m, pms):
    """Encuentra el project.material del NODO que corresponde al material OCR."""
    dom = _norm(m.get("codigo_dom") or "")
    ref = (m.get("ref_interna") or "").strip()
    for pm in pms:
        disp = _norm(pm["product_id"][1]) if pm.get("product_id") else ""
        pcode = str(pm.get("product_code") or "").strip()
        if (dom and dom in disp) or (ref and f"/{ref}]" in disp) \
           or (ref and pcode == ref) or (dom and _norm(pcode) == dom):
            return pm
    return None


def fecha_devolucion_propuesta():
    """Día hábil siguiente: mañana; si cae sábado/domingo, el lunes."""
    d = datetime.now() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%d/%m/%Y")


def _marcar_duplicados(d):
    """Detecta fotos repetidas de la misma hoja (mismo BAN de despacho) y marca
    todas menos la principal con `duplicada_de` = foto_n de la principal.
    La principal es la que ya tenga devolución ejecutada; si ninguna, la primera.
    Anti doble-devolución por error humano al fotografiar."""
    grupos = {}
    for idx, h in enumerate(d.get("hojas", [])):
        h.pop("duplicada_de", None)
        ban = ((h.get("ocr") or {}).get("ban_code") or "").strip().upper()
        if re.match(r"^BAN-\d+$", ban):
            grupos.setdefault(ban, []).append(idx)
    n_dup = 0
    hojas = d.get("hojas", [])
    for ban, idxs in grupos.items():
        if len(idxs) < 2:
            continue
        principal = next((i for i in idxs if hojas[i].get("devolucion_ejecutada")), idxs[0])
        for i in idxs:
            if i != principal:
                hojas[i]["duplicada_de"] = hojas[principal].get("foto_n")
                n_dup += 1
        log.warning("Duplicados de %s: fotos %s (principal #%s)",
                    ban, [hojas[i].get("foto_n") for i in idxs],
                    hojas[principal].get("foto_n"))
    return n_dup


def _es_ban_valido(ban):
    return bool(re.match(r"^BAN-\d+$", (ban or "").strip().upper()))


def _vincular_continuaciones(d):
    """Una bandeja grande sale impresa en 2+ hojas: las páginas siguientes no traen
    cabecera (sin BAN/PRO/Asignada — solo membrete y la tabla continúa). El OCR las
    marca es_continuacion=true y aquí se VINCULAN a la hoja anterior con BAN válido
    y se FUSIONAN sus filas y notas en esa hoja principal: una bandeja = una hoja
    lógica = UNA sola bandeja de devolución/adicional en SGS.
    Re-ejecutable: las filas fusionadas llevan de_foto/de_idx (su origen) y las que
    el humano ya editó (fuente=humano) se conservan tal cual en la re-fusión.
    Devuelve el número de hojas de continuación vinculadas."""
    hojas = d.get("hojas", [])
    for h in hojas:
        h.pop("continuacion_de", None)
        h.pop("continuacion_ban", None)
        h.pop("paginas_extra", None)

    # 1. Vincular cada continuación con su hoja principal (la anterior "real")
    links = {}                      # foto_n de la continuación → índice principal
    for idx, h in enumerate(hojas):
        o = h.get("ocr") or {}
        if not (o.get("es_continuacion") and not _es_ban_valido(o.get("ban_code"))):
            continue
        principal = None
        for j in range(idx - 1, -1, -1):
            hj, oj = hojas[j], (hojas[j].get("ocr") or {})
            if hj.get("duplicada_de"):
                continue            # foto repetida: saltar a la anterior
            if oj.get("es_continuacion") and not _es_ban_valido(oj.get("ban_code")):
                continue            # otra página intermedia de la misma bandeja
            if _es_ban_valido(oj.get("ban_code")):
                principal = j
            break                   # la primera hoja "real" decide (con o sin BAN)
        if principal is None:
            h["error"] = ("Hoja de continuación (sin cabecera BAN/PRO) pero la hoja "
                          "anterior no tiene BAN válido — no se pudo vincular. "
                          "Revisa el orden de las fotos o corrige el BAN de la anterior.")
            continue
        h["error"] = None
        links[h["foto_n"]] = principal
        h["continuacion_de"] = hojas[principal]["foto_n"]
        h["continuacion_ban"] = (hojas[principal]["ocr"].get("ban_code") or "").strip().upper()

    por_principal = {}
    for foto_n, p in links.items():
        por_principal.setdefault(p, []).append(foto_n)

    # 2. Fusionar filas/notas en la principal (recomputable, conserva ediciones)
    for idx, h in enumerate(hojas):
        o = h.get("ocr")
        if not o or h.get("continuacion_de"):
            continue
        previas_m = {(m.get("de_foto"), m.get("de_idx")): m
                     for m in o.get("materiales") or [] if m.get("de_foto")}
        previas_n = {(n.get("de_foto"), n.get("de_idx")): n
                     for n in o.get("notas_manuscritas") or [] if n.get("de_foto")}
        o["materiales"] = [m for m in o.get("materiales") or [] if not m.get("de_foto")]
        o["notas_manuscritas"] = [n for n in o.get("notas_manuscritas") or []
                                  if not n.get("de_foto")]
        o["advertencias"] = [a for a in o.get("advertencias") or []
                             if not a.startswith("pág. (foto #")]
        extras = sorted(por_principal.get(idx, []))
        if not extras:
            continue
        h["paginas_extra"] = extras
        for fn in extras:
            oc = next(x for x in hojas if x.get("foto_n") == fn)["ocr"]
            for i2, m in enumerate(oc.get("materiales") or []):
                prev = previas_m.get((fn, i2))
                if prev is not None:
                    o["materiales"].append(prev)     # conserva ediciones humanas
                else:
                    m2 = dict(m)
                    m2["de_foto"], m2["de_idx"] = fn, i2
                    o["materiales"].append(m2)
            for i2, n2 in enumerate(oc.get("notas_manuscritas") or []):
                prev = previas_n.get((fn, i2))
                if prev is not None:
                    o["notas_manuscritas"].append(prev)
                else:
                    n3 = dict(n2)
                    n3["de_foto"], n3["de_idx"] = fn, i2
                    o["notas_manuscritas"].append(n3)
            for a in oc.get("advertencias") or []:
                t = f"pág. (foto #{fn}): {a}"
                if t not in o["advertencias"]:
                    o["advertencias"].append(t)
        log.info("Continuaciones de %s (foto #%s): fotos %s fusionadas",
                 o.get("ban_code"), h.get("foto_n"), extras)

        # 3. Cruce nota↔fila ENTRE páginas (dentro de una página ya lo hizo el OCR):
        #    una nota "Dev" del pie de la última página puede referirse a una fila
        #    de otra página. Conservador: solo si la fila no tiene manuscrito ni
        #    edición humana, y la cantidad cabe en lo entregado.
        for nt in o["notas_manuscritas"]:
            if nt.get("tipo") != "devolucion" or nt.get("fuente") == "humano":
                continue
            ref = str(nt.get("ref_interna") or "").strip()
            cant = nt.get("cantidad")
            if not ref or not isinstance(cant, (int, float)) or cant <= 0:
                continue
            objetivo = [m for m in o["materiales"]
                        if (m.get("ref_interna") or "").strip() == ref
                        or (len(ref) >= 4
                            and (m.get("ref_interna") or "").strip().endswith(ref))]
            if len(objetivo) != 1:
                continue                       # ambiguo o inexistente: no tocar
            m = objetivo[0]
            if (nt.get("de_foto") or h["foto_n"]) == (m.get("de_foto") or h["foto_n"]):
                continue                       # misma página: ya cruzada por el OCR
            if (m.get("fuente") == "humano" or m.get("devolucion_manuscrita") is not None
                    or (m.get("devolucion") or 0) > 0):
                continue
            ent = m.get("entregado")
            if isinstance(ent, (int, float)) and 0 < cant <= ent:
                m["devolucion"] = cant
                m["confianza"] = "media"
                txt = (f"Devolución tomada de nota del pie de otra página: "
                       f"'{nt.get('texto', '')}'")
                if txt not in (m.get("nota") or ""):
                    m["nota"] = ((m.get("nota") or "") + " | " + txt).strip(" |")

        # 4. Re-consolidar la principal: en_bandeja/semáforo/cobertura inversa
        #    ahora se calculan con TODAS las páginas de la bandeja
        v = h.get("verificacion")
        if v:
            h["ocr"] = _consolidar_hoja(o, v)
    return len(links)


def _devolucion_en_otra_sesion(sid_actual, ban, dias=60, marca="devolucion_ejecutada"):
    """¿Esta BAN de despacho ya tiene devolución (o adicional, con
    marca="adicional_ejecutado") ejecutada en OTRA sesión? (la misma hoja
    fotografiada dos días distintos). Devuelve {sesion, ban_dev} o None."""
    ban = (ban or "").strip().upper()
    if not _es_ban_valido(ban):
        return None
    limite = (datetime.now() - timedelta(days=dias)).strftime("%Y%m%d")
    for sid in sorted(os.listdir(DIR_SESIONES), reverse=True):
        if sid == sid_actual or sid[:8] < limite:
            continue
        try:
            with _LOCK:
                d = _cargar(sid)
        except Exception:
            continue
        for h in d.get("hojas", []):
            ej = h.get(marca)
            if ej and ((h.get("ocr") or {}).get("ban_code") or "").strip().upper() == ban:
                return {"sesion": sid, "ban_dev": ej.get("ban_dev") or ej.get("ban")}
    return None


def _url_sgs_nodo(pid):
    """Link directo al form del NODO en SGS (para revisar/crear a mano)."""
    if not pid:
        return None
    return (f"{cfg.SGS_URL}/web#id={pid}&action=485&model=project.dominion"
            f"&view_type=form&cids=1&menu_id=352")


def _comparar_bandeja_con_plan(ban_dev, plan):
    """Motoverificador de la bandeja de devolución recién creada (solo lectura).
    Cuadra si: type=devolution, equipo coincide, TODOS los materiales del plan
    están con la cantidad exacta, y no hay materiales extra.
    Devuelve (ok, detalle) — detalle incluye la bandeja completa para el panel."""
    t = _sgs_op(lambda cli: cli.search_read(
        "project.tray", [["name", "=", ban_dev]],
        ["id", "name", "type", "state", "partner_id", "date",
         "project_tray_material_ids"], limit=1))
    c = _SGS        # proxy con re-login automático
    det = {"ban": ban_dev, "tray_id": None, "estado": None, "fecha": None,
           "equipo": None, "materiales": [], "checks": [], "ok": False,
           "ts": datetime.now().isoformat(timespec="seconds")}
    if not t:
        det["checks"].append("✗ La bandeja no aparece en SGS")
        return False, det
    t = t[0]
    det.update({"tray_id": t["id"], "estado": t.get("state"),
                "fecha": str(t.get("date") or "")[:10],
                "equipo": t["partner_id"][1] if t.get("partner_id") else ""})
    ok = True
    if str(t.get("type") or "").lower() != "devolution":
        ok = False
        det["checks"].append(f"✗ type = {t.get('type')} (se esperaba devolution)")
    if plan.get("equipo") and not _nombres_coinciden(det["equipo"], plan["equipo"]):
        ok = False
        det["checks"].append(f"✗ Equipo '{det['equipo']}' ≠ plan '{plan['equipo']}'")

    # Cantidad real = campo `amount` (deliver_quantity devuelve 0 via API)
    tms = c.search_read("project.tray.material",
                        [["id", "in", t.get("project_tray_material_ids") or []]],
                        ["product_id", "amount", "deliver_quantity"], limit=200)
    esperados = {}
    for m in plan.get("materiales", []):
        if m.get("estado") in ("ok", "parcial") and (m.get("devolucion_ejecutable") or 0) > 0:
            clave = (_norm(m.get("codigo_dom") or ""), (m.get("ref_interna") or "").strip())
            esperados[clave] = {"cantidad": float(m["devolucion_ejecutable"]),
                                "visto": False, "texto": f"{m.get('codigo_dom')}/{m.get('ref_interna')}"}
    for tm in tms:
        disp = tm["product_id"][1] if tm.get("product_id") else ""
        qty = float(tm.get("amount") or tm.get("deliver_quantity") or 0)
        fila = {"producto": disp, "cantidad": qty, "ok": False, "detalle": ""}
        dn = _norm(disp)
        enc = None
        for (dom, ref), e in esperados.items():
            if (dom and dom in dn) or (ref and f"/{ref}]" in dn):
                enc = e
                break
        if enc is None:
            ok = False
            fila["detalle"] = "material EXTRA no planeado"
            det["checks"].append(f"✗ Material extra en la bandeja: {disp[:60]}")
        else:
            enc["visto"] = True
            if abs(qty - enc["cantidad"]) > 0.001:
                ok = False
                fila["detalle"] = f"cantidad {qty:g} ≠ plan {enc['cantidad']:g}"
                det["checks"].append(f"✗ {enc['texto']}: cantidad {qty:g} ≠ plan {enc['cantidad']:g}")
            else:
                fila["ok"] = True
        det["materiales"].append(fila)
    for e in esperados.values():
        if not e["visto"]:
            det["checks"].append(f"⚠ {e['texto']}: planeado pero NO está en la bandeja "
                                 f"(¿excluido por Odoo?)")
    if ok and not det["checks"]:
        det["checks"].append("✓ Bandeja = plan (equipo, materiales y cantidades)")
    det["ok"] = ok
    return ok, det


def _ref_pm(pm):
    """Referencia interna de un project.material: '274314' de '[DOM-028802/274314] ...'."""
    disp = pm["product_id"][1] if pm.get("product_id") else ""
    m = re.search(r"/([^\]]+)\]", disp)
    return (m.group(1) if m else str(pm.get("product_code") or "")).strip().upper()


# Palabras de las notas que no describen un material (ruido del OCR o del pie)
_PALABRAS_NOTA_VACIAS = {
    "DEV", "BAN", "POR", "PARA", "FALTA", "FALTAN", "MANO", "AGREGADO", "TACHADO",
    "SUBRAYADO", "PARCIALMENTE", "CORTADO", "BORDE", "DIGITO", "CORREGIDO", "SOBRE",
    "NOTA", "ILEGIBLE", "INFERIOR", "SUPERIOR", "IZQUIERDO", "DERECHO", "SELLO",
    "DESPACHADO", "RECIBIDO", "CANTIDAD", "METROS", "UNIDADES",
}


def _casar_adicional(ref, texto, pms):
    """Busca en los materiales del NODO el material que pide una nota de adicional.
    → (pm o None, modo, sugerencias). modo: 'exacto' | 'sufijo' | ''.
    La cuadrilla suele escribir solo las ÚLTIMAS cifras del código ("74314",
    "0011", "1369"): en producción 25 de 76 notas "no está en el NODO" eran eso.
    Un sufijo de 4+ cifras que casa con UN solo material se acepta (⚙); más corto
    o con varios candidatos queda como sugerencia para que el humano elija."""
    refs = [(_ref_pm(p), p) for p in pms if p.get("product_id")]

    def _sug(lista):
        return [{"ref": r, "producto": p["product_id"][1]} for r, p in lista[:6]]

    ref = (ref or "").strip().upper()
    if ref:
        for r, p in refs:
            if r == ref or str(p.get("product_code") or "").strip().upper() == ref:
                return p, "exacto", []
        if ref.isdigit():
            suf = [(r, p) for r, p in refs if r.endswith(ref)]
            if len(ref) >= 4 and len(suf) == 1:
                return suf[0][1], "sufijo", []
            if suf:
                return None, "", _sug(suf)
            if len(ref) > 6:     # una cifra de más ("2513649" → 251364)
                sub = [(r, p) for r, p in refs if len(r) >= 5 and r in ref]
                if sub:
                    return None, "", _sug(sub)
    # Sin código útil: palabras de la nota contra la descripción ("Percha 5P = 1")
    palabras = [w for w in re.findall(r"[A-Z]{4,}", _norm(texto))
                if w not in _PALABRAS_NOTA_VACIAS]
    if palabras:
        puntaje = [(sum(w in _norm(p["product_id"][1]) for w in palabras), r, p)
                   for r, p in refs]
        mejor = max((s for s, _, _ in puntaje), default=0)
        if mejor:
            return None, "", _sug([(r, p) for s, r, p in puntaje if s == mejor])
    return None, "", []


def _adic_todos(sid):
    """¿La sesión hace adicionales de TODOS los supervisores? (perfil con adicionales de todos)"""
    with _LOCK:
        return bool(_cargar(sid).get("adic_todos"))


def preanalisis_hoja(hoja, adic_todos=False, zona=None):
    """Cruce de SOLO LECTURA contra SGS antes de crear la bandeja de devolución.
    Detecta EXACTAMENTE lo que dispara el error de validación de Odoo
    ("no tienen saldo pendiente de devolución o cero unidades despachadas")
    y el riesgo de doble devolución por bandejas ya aprobadas por almacén.
    adic_todos=True: los adicionales no exigen supervisor de la lista.
    zona: la de la sesión — decide la bodega (<BODEGA>/Recupero y /Stock)."""
    ocr = hoja.get("ocr") or {}
    verif = hoja.get("verificacion") or {}
    pid = verif.get("project_id")
    plan = {"disponible": False, "nodo_id": pid, "equipo": verif.get("lider_sgs"),
            "fecha": fecha_devolucion_propuesta(), "almacen": _almacen_zona(zona, "Recupero"),
            "materiales": [], "bandejas_dev_previas": [], "mensajes": [],
            "ejecutables": 0, "bloqueados": 0,
            "ts": datetime.now().isoformat(timespec="seconds")}
    if not pid:
        plan["mensajes"].append("Sin proyecto verificado en SGS — corrige BAN/PRO y usa ↻ SGS")
        return plan
    plan["url_sgs"] = _url_sgs_nodo(pid)
    try:
        c = _sgs()
    except Exception as e:
        plan["mensajes"].append(str(e))
        return plan
    plan["disponible"] = True

    try:
        pms = _sgs_op(lambda cli: cli.search_read(
            "project.material", [["project_id", "=", pid]],
            ["id", "product_id", "product_code", "amount", "tray", "despachado",
             "devuelto", "advance", "balance"], limit=500))
        c = _SGS        # proxy con re-login automático
    except Exception as e:
        plan["disponible"] = False
        plan["mensajes"].append(f"SGS no respondió (se intentó re-login): {e}")
        return plan
    trays = c.search_read("project.tray", [["project_id", "=", pid]],
                          ["id", "name", "type", "state", "state_picking",
                           "partner_id", "date", "project_tray_material_ids"], limit=200)
    devs = [t for t in trays if _tray_activa(t)
            and str(t.get("type") or "").lower() == "devolution"]
    ids_tm = [i for t in devs for i in (t.get("project_tray_material_ids") or [])]
    tm_por_id = {}
    if ids_tm:
        for m2 in c.search_read("project.tray.material", [["id", "in", ids_tm]],
                                ["product_id", "deliver_quantity", "amount"], limit=500):
            tm_por_id[m2["id"]] = m2
    for t in devs:
        mats_t = [tm_por_id[i]["product_id"][1]
                  for i in (t.get("project_tray_material_ids") or [])
                  if i in tm_por_id and tm_por_id[i].get("product_id")]
        plan["bandejas_dev_previas"].append({
            "name": t.get("name"), "fecha": str(t.get("date") or "")[:10],
            "partner": t["partner_id"][1] if t.get("partner_id") else "",
            "materiales": mats_t})
    if devs:
        plan["mensajes"].append(
            f"⚠ Este NODO ya tiene {len(devs)} bandeja(s) de devolución aprobada(s): "
            + ", ".join(str(t.get("name") or "?") for t in devs)
            + " — el saldo de abajo ya las descuenta")

    mats_prev_norm = [_norm(x) for b in plan["bandejas_dev_previas"] for x in b["materiales"]]
    # Cantidad ya comprometida en devoluciones previas, POR PRODUCTO, incluidas las que
    # almacén aún no recibe: `devuelto` solo sube cuando el picking queda hecho, así que
    # una devolución aprobada ayer y no recibida dejaba el saldo "lleno" (incidente
    # 2026-09-23: BAN-125980 ya devuelta en BAN-126289 → Odoo la frenó con sin_saldo).
    comprometido = {}
    try:
        devs_pend = [t for t in trays if str(t.get("type") or "").lower() == "devolution"
                     and str(t.get("state") or "").lower() in ("approved", "aprobado", "to_approve")]
        ids_pend = [i for t in devs_pend for i in (t.get("project_tray_material_ids") or [])]
        if ids_pend:
            for m2 in c.search_read("project.tray.material", [["id", "in", ids_pend]],
                                    ["product_id", "amount", "deliver_quantity"], limit=1000):
                if m2.get("product_id"):
                    k = _norm(m2["product_id"][1])
                    comprometido[k] = comprometido.get(k, 0.0) + float(
                        m2.get("amount") or m2.get("deliver_quantity") or 0)
    except Exception as e:
        plan["mensajes"].append(f"No se pudieron sumar devoluciones previas ({e}) — revisa el saldo a mano")

    for m in ocr.get("materiales", []):
        dev = m.get("devolucion")
        if not isinstance(dev, (int, float)) or dev <= 0:
            continue
        pm = _pm_match(m, pms)
        item = {"codigo_dom": m.get("codigo_dom"), "ref_interna": m.get("ref_interna"),
                "descripcion": m.get("descripcion"), "devolucion": dev,
                "despachado": None, "devuelto": None, "saldo": None,
                "estado": "", "detalle": "", "devolucion_ejecutable": None}
        if pm is None:
            item["estado"] = "no_encontrado"
            item["detalle"] = "El material no existe en los materiales del NODO en SGS"
            plan["bloqueados"] += 1
        else:
            desp = float(pm.get("despachado") or 0)
            devto = float(pm.get("devuelto") or 0)
            saldo = round(desp - devto, 3)
            dom = _norm(m.get("codigo_dom") or "")
            ref = (m.get("ref_interna") or "").strip()
            en_prev = any((dom and dom in x) or (ref and f"/{ref}]" in x)
                          for x in mats_prev_norm)
            prev = sum(q for k, q in comprometido.items()
                       if (dom and dom in k) or (ref and f"/{ref}]" in k))
            if prev > devto:
                # hay devoluciones previas que almacén aún no recibe: cuentan igual
                saldo = round(desp - prev, 3)
                devto = prev
            item.update({"despachado": desp, "devuelto": devto, "saldo": saldo,
                         "pm_id": pm["id"]})
            if saldo <= 0:
                item["estado"] = "bloqueado"
                item["detalle"] = ("Sin saldo pendiente: ya se devolvió todo lo despachado"
                                   + (" — está en una devolución previa aprobada" if en_prev else
                                      (" — sin despacho registrado" if desp == 0 else "")))
                plan["bloqueados"] += 1
            elif dev > saldo:
                item["estado"] = "parcial"
                item["devolucion_ejecutable"] = saldo
                item["detalle"] = (f"La hoja pide {dev:g} pero el saldo devolvible es {saldo:g}"
                                   + (f" (ya devueltos {devto:g})" if devto > 0 else "")
                                   + " — se devolvería máximo el saldo")
                plan["ejecutables"] += 1
            else:
                item["estado"] = "ok"
                item["devolucion_ejecutable"] = dev
                if en_prev:
                    item["detalle"] = "Ojo: hubo una devolución previa de este material; el saldo aún alcanza"
                plan["ejecutables"] += 1
        plan["materiales"].append(item)

    if not plan["materiales"]:
        plan["mensajes"].append("La hoja no tiene devoluciones > 0 — nada que ejecutar")
    if not plan["equipo"]:
        plan["mensajes"].append("⚠ SGS no reporta líder para la bandeja — definir Equipo a mano")

    # ── Adicionales: despachos extra pedidos en notas de esquina ──────────────
    ads = []
    for ni, nota in enumerate(ocr.get("notas_manuscritas") or []):
        # hojas procesadas con el prompt viejo no traen "tipo": heurística
        tipo = nota.get("tipo") or ("devolucion"
                                    if re.search(r"dev", nota.get("texto") or "", re.I)
                                    else "adicional")
        if tipo != "adicional":
            continue
        ref = (nota.get("ref_interna") or "").strip()
        cant = nota.get("cantidad")
        item = {"nota_idx": ni, "nota_idxs": [ni], "ref": ref, "ref_nota": ref,
                "texto": nota.get("texto"),
                "cantidad": cant, "existe": False, "pm_id": None, "producto": "",
                "proyectado_actual": None, "proyectado_nuevo": None,
                "estado": "", "detalle": "", "modo": "", "sugerencias": [],
                "fuente": nota.get("fuente") or ""}
        pm, modo, sugs = _casar_adicional(ref, nota.get("texto"), pms)
        item["sugerencias"] = sugs
        if pm is not None:
            # ref = la COMPLETA del NODO: con ella el navegador ubica la fila
            item.update({"ref": _ref_pm(pm), "producto": pm["product_id"][1], "modo": modo})
            if modo == "sufijo":
                item["detalle"] = f"⚙ Código corto '{ref}' → {item['ref']} (único en el NODO)"
        if not isinstance(cant, (int, float)) or cant <= 0:
            item["estado"] = "invalido"
            item["detalle"] = "Falta la cantidad (o es 0) — escríbela"
        elif pm is None:
            if sugs:
                item["estado"] = "sugerido"
                item["detalle"] = ((f"'{ref}' no está tal cual en el NODO" if ref
                                    else "Sin código legible") + " — elige el material")
            elif not ref:
                item["estado"] = "invalido"
                item["detalle"] = "Sin código legible — escribe el código del material"
            else:
                item["estado"] = "no_existe"
                item["detalle"] = ("El material no está en el NODO — agrégalo en SGS "
                                   "y pulsa ↻, o corrige el código")
        else:
            proy = float(pm.get("amount") or 0)
            item.update({"existe": True, "pm_id": pm["id"],
                         "proyectado_actual": proy,
                         "tray": float(pm.get("tray") or 0),
                         "proyectado_nuevo": proy + float(cant),
                         "estado": "ok"})
        ads.append(item)
    # Dos notas con la misma referencia = UN solo objetivo (sumar pedidos):
    # editar dos veces el mismo Proyectado deja el segundo valor y pierde el primero
    vistos = {}
    fusion = []
    for it in ads:
        if it["estado"] == "ok" and it["ref"] in vistos:
            prev = vistos[it["ref"]]
            prev["cantidad"] = float(prev["cantidad"]) + float(it["cantidad"])
            prev["texto"] = f"{prev['texto']} + {it['texto']}"
            prev["nota_idxs"] = prev.get("nota_idxs", [prev["nota_idx"]]) + [it["nota_idx"]]
            prev["detalle"] = (prev["detalle"] + " | notas duplicadas fusionadas").strip(" |")
            continue
        if it["estado"] == "ok":
            vistos[it["ref"]] = it
        fusion.append(it)
    ads = fusion

    # ¿El adicional ya se hizo (a mano o en otra sesión)? Un despacho del NODO
    # POSTERIOR a la BAN de la hoja que ya lleva el material es la huella: el
    # sistema no lo crea otra vez sin que el humano confirme "es otro pedido".
    def _num_ban(nombre):
        m = re.search(r"(\d+)", str(nombre or ""))
        return int(m.group(1)) if m else 0
    n_hoja = _num_ban(ocr.get("ban_code"))
    posteriores = [t for t in trays if n_hoja and _num_ban(t.get("name")) > n_hoja
                   and str(t.get("type") or "").lower() != "devolution"
                   and str(t.get("state") or "").lower() not in ("canceled", "canceled_partial")]
    if posteriores and any(a["estado"] == "ok" for a in ads):
        try:
            ids_p = [i for t in posteriores for i in (t.get("project_tray_material_ids") or [])]
            tm_p = {m2["id"]: m2 for m2 in c.search_read(
                "project.tray.material", [["id", "in", ids_p]],
                ["product_id", "amount"], limit=1000)} if ids_p else {}
            notas = ocr.get("notas_manuscritas") or []
            for it in ads:
                if it["estado"] != "ok":
                    continue
                ya = [f"{t['name']} ({float(tm_p[i].get('amount') or 0):g})"
                      for t in posteriores for i in (t.get("project_tray_material_ids") or [])
                      if i in tm_p and tm_p[i].get("product_id")
                      and f"/{it['ref']}]" in _norm(tm_p[i]["product_id"][1])]
                if not ya:
                    continue
                forzado = any((notas[n] if n < len(notas) else {}).get("forzar")
                              for n in it.get("nota_idxs", [it["nota_idx"]]))
                if forzado:
                    it["detalle"] = (it["detalle"] + " | confirmado: es otro pedido (ya salió en "
                                     + ", ".join(ya) + ")").strip(" |")
                else:
                    it["estado"] = "ya_salio"
                    it["ya_salio"] = ya
                    it["detalle"] = ("⚠ Ya salió después de esta hoja en " + ", ".join(ya)
                                     + " — ¿el adicional ya se hizo?")
        except Exception as e:
            plan["mensajes"].append(f"No se pudo revisar si los adicionales ya salieron ({e})")

    # Objetivo ABSOLUTO del Proyectado: max(proyectado, bandeja) + pedido.
    # (Si la bandeja ya superó el proyectado, subir solo proyectado+pedido deja
    # pendiente 0 y Odoo excluye el material del modal.)
    # Idempotencia: si un intento anterior ya fijó el objetivo, ese manda —
    # jamás se vuelve a sumar sobre un proyectado ya subido.
    mapa = hoja.get("adicional_proyectados") or {}
    for it in ads:
        if it["estado"] != "ok":
            continue
        base = max(float(it["proyectado_actual"]), float(it.get("tray") or 0))
        it["proyectado_nuevo"] = round(base + float(it["cantidad"]), 3)
        if (it.get("tray") or 0) > float(it["proyectado_actual"]):
            it["detalle"] = (it["detalle"] + f" | bandeja ya en {it['tray']:g} → objetivo "
                             f"{it['proyectado_nuevo']:g}").strip(" |")
        ya = mapa.get(str(it["ref"]))
        if ya is not None:
            it["proyectado_nuevo"] = float(ya)
            it["detalle"] = (it["detalle"] + " | objetivo fijado en intento anterior").strip(" |")

    if ads:
        asignados = ocr.get("asignada_a") or []
        supervisor = asignados[1] if len(asignados) > 1 else ""
        autorizado = adic_todos or any(_nombres_coinciden(supervisor, s)
                                       for s in SUPERVISORES_ADICIONALES)
        plan["adicionales"] = {
            "supervisor": supervisor, "autorizado": autorizado, "todos": bool(adic_todos),
            "equipo": plan["equipo"], "fecha": plan["fecha"],
            "almacen": _almacen_zona(zona, "Stock"), "items": ads,
            "ejecutables": (sum(1 for a in ads if a["estado"] == "ok")
                            if autorizado else 0),
            "por_resolver": sum(1 for a in ads if a["estado"] != "ok"),
        }
        if not autorizado:
            plan["adicionales"]["detalle"] = (
                f"Supervisor '{supervisor or '?'}' NO autorizado para adicionales "
                f"automáticos (autorizados: {', '.join(SUPERVISORES_ADICIONALES) or 'ninguno configurado'})")
    return plan


# ══════════════════════════════════════════════════════════════════════════
#  Autoreparación — aprender de las correcciones humanas
# ══════════════════════════════════════════════════════════════════════════

RUTA_LECCIONES = os.path.join(_BASE, "devoluciones_lecciones.json")

SCHEMA_LECCIONES = {
    "type": "object",
    "properties": {"lecciones": {"type": "array", "items": {"type": "string"}}},
    "required": ["lecciones"], "additionalProperties": False,
}

PROMPT_APRENDER = """Eres el módulo de AUTORREPARACIÓN de un OCR de hojas de bandeja SGS \
(las mismas hojas que ya conoces: columna "Consu mido" manuscrita = cantidad a devolver).

Arriba tienes las fotos originales. Abajo, las correcciones que hizo el humano sobre lo \
que el OCR leyó (campo, valor de la máquina, valor correcto del humano):

{correcciones}

LECCIONES YA APRENDIDAS en sesiones anteriores (NO las repitas):
{previas}

TAREA: mira las fotos, entiende POR QUÉ la máquina se equivocó en cada corrección \
(ej.: los chulos ✓ de almacén se confunden con números, trazos que parecen letras, \
desalineación por descripciones de dos renglones, fracciones, tachones...) y escribe \
MÁXIMO 5 lecciones NUEVAS, cortas y generalizables, en imperativo, que se puedan añadir \
al prompt del OCR para no repetir esos errores. Solo lecciones que apliquen a futuras \
hojas (no detalles de una hoja concreta). Si no hay nada nuevo que aprender, devuelve \
lista vacía."""


def _cargar_lecciones():
    try:
        with open(RUTA_LECCIONES, encoding="utf-8") as f:
            return json.load(f).get("lecciones", [])
    except Exception:
        return []


def _guardar_lecciones(lecciones):
    with open(RUTA_LECCIONES, "w", encoding="utf-8") as f:
        json.dump({"lecciones": lecciones}, f, ensure_ascii=False, indent=1)


def _prompt_ocr_actual():
    base = PROMPT_OCR_COMPACTO if OCR_MODO == "compacto" else PROMPT_OCR
    lec = _cargar_lecciones()
    if not lec:
        return base
    return (base + "\n\nLECCIONES APRENDIDAS de correcciones humanas en sesiones "
            "anteriores — aplícalas SIEMPRE:\n"
            + "\n".join(f"- {l['texto']}" for l in lec[-15:]))


# ══════════════════════════════════════════════════════════════════════════
#  Job de procesamiento (OCR + verificación) en segundo plano
# ══════════════════════════════════════════════════════════════════════════

OCR_WORKERS = int(os.getenv("OCR_WORKERS", "3"))   # hojas en paralelo contra el proveedor de OCR
_SGS_SEM = threading.Lock()   # la verificación SGS va serializada (cliente compartido)


def _hoja_desde_ocr(foto_n, ocr):
    """Lógica post-OCR: clasificar la
    hoja (continuación / no-bandeja / normal) y verificar contra SGS."""
    hoja = {"foto_n": foto_n, "error": None, "ocr": None, "verificacion": None}
    if ocr.get("es_continuacion") and not _es_ban_valido(ocr.get("ban_code")):
        # Página 2+ de una bandeja grande: sin cabecera no hay nada que
        # verificar aún — _vincular_continuaciones la fusiona con su hoja
        # principal al terminar el job.
        hoja["ocr"] = ocr
    elif not ocr.get("es_hoja_bandeja"):
        hoja["error"] = "La imagen no parece una hoja de bandeja SGS"
        hoja["ocr"] = ocr
    else:
        with _SGS_SEM:
            verif = verificar_hoja_sgs(ocr)
        hoja["ocr"] = _consolidar_hoja(ocr, verif)
        hoja["verificacion"] = verif
    return hoja


def _procesar_foto(sid, foto):
    try:
        ruta = os.path.join(_dir_sesion(sid), "fotos", foto["archivo"])
        ocr = ocr_hoja(ruta)
        return _hoja_desde_ocr(foto["n"], ocr)
    except Exception as e:
        log.exception("Error procesando foto %s de %s", foto["n"], sid)
        return {"foto_n": foto["n"], "error": str(e), "ocr": None, "verificacion": None}


# Marcas que un reproceso JAMÁS debe perder: son el seguro anti doble-escritura
# (la devolución/adicional ya existe en SGS aunque el OCR se rehaga).
_MARCAS_EJECUCION = ("devolucion_ejecutada", "adicional_ejecutado",
                     "adicional_proyectados", "bandeja_creada")


def _mapa_marcas(d):
    """Marcas de ejecución por foto_n: de las hojas actuales + de las guardadas
    por un 'Reprocesar desde cero' (marcas_ejecucion)."""
    marcas = {}
    for n_str, m in (d.get("marcas_ejecucion") or {}).items():
        marcas[int(n_str)] = dict(m)
    for h in d.get("hojas", []):
        for k in _MARCAS_EJECUCION:
            if h.get(k):
                marcas.setdefault(h["foto_n"], {})[k] = h[k]
    return marcas


def _job_procesar(sid):
    """Procesa las fotos pendientes (OCR + verificación), OCR_WORKERS a la vez.
    Es REANUDABLE: las hojas ya procesadas sin error no se repiten.
    Da VUELTAS: si mientras corría llegaron fotos nuevas (auto-OCR desde el
    celular), las procesa antes de terminar. Las hojas con error solo se
    reintentan en la primera vuelta (la que dispara «Procesar» o la primera foto):
    en las siguientes solo entran fotos sin hoja, para no gastar API en bucle
    contra un error persistente."""
    vuelta = 0
    try:
        while True:
            with _LOCK:
                d = _cargar(sid)
                fotos = list(d["fotos"])
                ns_validos = {f["n"] for f in fotos}
                marcas = _mapa_marcas(d)
                previas = {h["foto_n"]: h for h in d.get("hojas", [])
                           if (h.get("ocr") and not h.get("error") or (vuelta > 0 and h.get("error")))
                           and h["foto_n"] in ns_validos}
                pendientes = [f for f in fotos if f["n"] not in previas]
                d["estado"] = "procesando"
                d["hojas"] = sorted(previas.values(), key=lambda h: h["foto_n"])
                d["progreso"] = {"actual": len(previas), "total": len(fotos),
                                 "msg": (f"Procesando {len(pendientes)} hojas con IA "
                                         f"({OCR_WORKERS} a la vez)...")}
                _guardar(sid, d)

            if pendientes:
                with concurrent.futures.ThreadPoolExecutor(max_workers=OCR_WORKERS) as ex:
                    futuros = [ex.submit(_procesar_foto, sid, f) for f in pendientes]
                    for fut in concurrent.futures.as_completed(futuros):
                        hoja = fut.result()
                        for k, v in (marcas.get(hoja["foto_n"]) or {}).items():
                            hoja.setdefault(k, v)
                        with _LOCK:
                            d = _cargar(sid)
                            d["hojas"] = [h for h in d["hojas"] if h["foto_n"] != hoja["foto_n"]]
                            d["hojas"].append(hoja)
                            d["hojas"].sort(key=lambda h: h["foto_n"])
                            hechas = len(d["hojas"])
                            total = len(d["fotos"])      # pueden haber llegado más
                            d["progreso"] = {"actual": hechas, "total": total,
                                             "msg": f"{hechas}/{total} hojas listas..."}
                            _guardar(sid, d)

            with _LOCK:
                d = _cargar(sid)
                d["estado"] = "procesado"
                ndup = _marcar_duplicados(d)
                ncont = _vincular_continuaciones(d)
                d["progreso"]["msg"] = ("✓ Procesamiento completo"
                                        + (f" — ⚠ {ndup} foto(s) DUPLICADA(S) detectadas"
                                           if ndup else "")
                                        + (f" — {ncont} página(s) de continuación "
                                           f"integradas a su bandeja" if ncont else ""))
                _guardar(sid, d)

            # ¿Llegaron fotos nuevas mientras se procesaba? Decidirlo y cerrar el job
            # bajo _JOBS_LOCK: así una foto que entra justo ahora o bien la vemos
            # aquí, o bien encuentra el job cerrado y arranca uno nuevo. Nunca queda
            # una foto sin procesar por carrera.
            with _JOBS_LOCK:
                with _LOCK:
                    d = _cargar(sid)
                    hechas_n = {h["foto_n"] for h in d.get("hojas", [])
                                if h.get("ocr") or h.get("error")}
                    hay_nuevas = any(f["n"] not in hechas_n for f in d.get("fotos", []))
                if not hay_nuevas:
                    _JOBS.pop(sid, None)
                    return
            vuelta += 1
            log.info("Sesión %s: fotos nuevas durante el OCR — otra vuelta (%d)", sid, vuelta)
    except Exception as e:
        log.exception("Job de %s falló", sid)
        with _LOCK:
            d = _cargar(sid)
            d["estado"] = "error"
            d["progreso"]["msg"] = f"✗ {e}"
            _guardar(sid, d)
    finally:
        with _JOBS_LOCK:
            _JOBS.pop(sid, None)


def _lanzar_job_procesar(sid):
    """Arranca el job de OCR de la sesión si no está corriendo. Devuelve True si
    arrancó, False si ya había uno (que verá las fotos nuevas en su próxima
    vuelta). NO llamar con _LOCK tomado (orden de locks: _JOBS_LOCK → _LOCK)."""
    with _JOBS_LOCK:
        if sid in _JOBS:
            return False
        t = threading.Thread(target=_job_procesar, args=(sid,), daemon=True)
        _JOBS[sid] = t
        t.start()
        return True


def _auto_ocr_activo(d):
    return bool(d.get("auto_ocr", OCR_AUTO))


# ══════════════════════════════════════════════════════════════════════════
#  Ejecución de devoluciones en SGS (Playwright — paso 4)
#  Reglas: 1 worker secuencial · preanálisis FRESCO antes de cada bandeja ·
#  el lote NUNCA se interrumpe por un fallo (reporte al final + reintento) ·
#  Tras GUARDAR jamás se reintenta (regla «sin reintento tras guardar») · idempotente por hoja.
# ══════════════════════════════════════════════════════════════════════════

_RETRYABLES = ("nav_timeout", "field_timeout", "dropdown_timeout")
DEV_WORKERS = 2      # límite duro del proyecto: máx 2 Playwright simultáneos
DEV_STAGGER = 8      # s entre arranques de workers (no saturar Odoo)


def _job_devoluciones(sid, dry_run):
    _mtx = threading.Lock()          # protege resultados/contador entre workers
    resultados = []
    contador = {"hechas": 0, "total": 0}

    def _set_job(**kw):
        with _LOCK:
            d = _cargar(sid)
            d.setdefault("dev_job", {}).update(kw)
            _guardar(sid, d)

    def _progreso(msg_extra=""):
        t = contador["total"] or 1
        h = contador["hechas"]
        _set_job(hechas=h, total=contador["total"], pct=round(100 * h / t),
                 resultados=sorted(resultados, key=lambda r: r["hoja"]),
                 msg=(f"↩ {h}/{contador['total']} hojas ({round(100*h/t)}%) · "
                      f"{DEV_WORKERS} navegadores en paralelo"
                      + (f" · {msg_extra}" if msg_extra else "")))

    try:
        from browser_client import PlaywrightClient, BrowserActionError
    except Exception as e:
        _set_job(estado="error", msg=f"Playwright no disponible en este servidor: {e}")
        _JOBS.pop(sid + "_dev", None)
        return

    def _procesar_hoja(pw, i):
        with _LOCK:
            h = _cargar(sid)["hojas"][i]
        ocr = h.get("ocr") or {}
        ban_origen = ocr.get("ban_code") or f"hoja {i+1}"
        res = {"hoja": i, "ban_origen": ban_origen, "status": "", "ban_dev": None,
               "estado_sgs": None, "error": None, "advertencias": [], "intentos": 0,
               "url_sgs": None, "aprobada": False}
        try:
            if h.get("duplicada_de"):
                res["status"] = "duplicada"
                res["error"] = (f"Foto repetida de la hoja #{h['duplicada_de']} — "
                                f"NO se ejecuta (anti doble-devolución). Borra la foto sobrante.")
                return res
            if h.get("continuacion_de"):
                res["status"] = "continuacion"
                res["error"] = (f"Página de continuación de la foto #{h['continuacion_de']} "
                                f"({h.get('continuacion_ban') or ''}) — sus materiales se "
                                f"ejecutan con esa hoja.")
                return res
            otra = None if dry_run else _devolucion_en_otra_sesion(sid, (h.get("ocr") or {}).get("ban_code"))
            if otra:
                res["status"] = "ya_ejecutada"
                res["ban_dev"] = otra["ban_dev"]
                res["error"] = (f"Esta hoja ya se devolvió en la sesión {otra['sesion']} "
                                f"({otra['ban_dev']}) — no se repite (anti doble devolución)")
                return res
            if h.get("devolucion_ejecutada") and not dry_run:
                res["status"] = "ya_ejecutada"
                res["ban_dev"] = h["devolucion_ejecutada"].get("ban_dev")
                res["estado_sgs"] = h["devolucion_ejecutada"].get("estado")
                res["url_sgs"] = (h.get("preanalisis") or {}).get("url_sgs")
                return res
            if (h.get("verificacion") or {}).get("estado") == "rojo":
                res["status"] = "omitida"
                res["error"] = "Verificación BAN→PRO→líder en rojo — corregir y reverificar"
                return res

            # Preanálisis FRESCO (el saldo pudo cambiar desde el último cruce)
            todos = _adic_todos(sid)
            with _SGS_SEM:
                plan = preanalisis_hoja(h, adic_todos=todos, zona=_zona_sid(sid))
            res["url_sgs"] = plan.get("url_sgs")
            with _LOCK:
                d = _cargar(sid)
                d["hojas"][i]["preanalisis"] = plan
                _guardar(sid, d)

            mats = [{"ref": (m.get("ref_interna") or "").strip(),
                     "dom": (m.get("codigo_dom") or "").strip(),
                     "cantidad": m["devolucion_ejecutable"]}
                    for m in plan.get("materiales", [])
                    if m.get("estado") in ("ok", "parcial")
                    and isinstance(m.get("devolucion_ejecutable"), (int, float))
                    and m["devolucion_ejecutable"] > 0]
            bloqueados = [m for m in plan.get("materiales", [])
                          if m.get("estado") in ("bloqueado", "no_encontrado")]
            if bloqueados:
                res["advertencias"].append(
                    f"{len(bloqueados)} material(es) bloqueados por preanálisis — no se incluyen")
            if plan.get("disponible") and not plan.get("materiales"):
                # hoja que solo trae adicionales (o nada): no es un fallo de saldo
                res["status"] = "omitida"
                res["error"] = "La hoja no pide devoluciones" + (
                    " — sus adicionales van en + Adicionales" if plan.get("adicionales") else "")
                return res
            if not mats:
                res["status"] = "sin_ejecutables"
                res["error"] = "Ningún material con saldo devolvible"
                return res
            if not plan.get("equipo"):
                res["status"] = "error"
                res["error"] = "SGS no reporta líder (Equipo) — asignar a mano"
                return res

            # Motoverificador pre-APROBAR: solo se aprueba si bandeja == plan
            def _aprobar_si(ban_dev, _plan=plan, _res=res):
                with _SGS_SEM:
                    ok, det = _comparar_bandeja_con_plan(ban_dev, _plan)
                _res["motoverificacion"] = det
                return ok

            intento, max_intentos = 0, 2
            while True:
                intento += 1
                res["intentos"] = intento
                try:
                    r = pw.crear_bandeja_devolucion(
                        nodo_id=plan["nodo_id"], partner_name=plan["equipo"],
                        materiales=mats, date_str=plan["fecha"],
                        location_search=plan.get("almacen") or _almacen_zona(_zona_sid(sid), "Recupero"),
                        dry_run=dry_run, screenshots=True,
                        aprobar_si=None if dry_run else _aprobar_si)
                    res["status"] = r["status"]
                    res["ban_dev"] = r.get("ban_code")
                    res["estado_sgs"] = r.get("estado")
                    res["aprobada"] = bool(r.get("aprobada"))
                    res["advertencias"] += r.get("advertencias") or []
                    break
                except BrowserActionError as e:
                    msg = str(e)
                    expulsado = msg.startswith("session_expired") or \
                        "/web/login" in (getattr(getattr(pw, "_page", None), "url", "") or "")
                    if intento < max_intentos and (expulsado or any(msg.startswith(p) for p in _RETRYABLES)):
                        if expulsado:
                            # Todo esto pasa ANTES de GUARDAR (no hay escritura): volver a
                            # entrar es seguro. Si el login falla, lanza y el worker suelta.
                            log.warning("Devolución %s: SGS cerró la sesión del navegador — re-login", ban_origen)
                            pw.login()
                        log.warning("Devolución %s intento %d falló — reintento", ban_origen, intento)
                        continue
                    res["status"] = "error"
                    res["error"] = msg
                    break

            # Verificación post-escritura (solo lectura) + idempotencia
            if not dry_run and res["status"] == "ok" and res["ban_dev"]:
                try:
                    with _SGS_SEM:
                        chk = _SGS.search_read("project.tray", [["name", "=", res["ban_dev"]]],
                                            ["id", "name", "state", "type"], limit=1)
                    if chk:
                        res["verificada"] = True
                        # La API es la verdad: tras APROBAR, el estado del modal puede
                        # seguir diciendo "Para aprobar" unos segundos (falso negativo
                        # visto 2026-09-11: 2 de 3 bandejas aprobadas salían "False").
                        if str(chk[0].get("state") or "").lower() == "approved":
                            res["aprobada"] = True
                            res["estado_sgs"] = "APROBADO"
                        with _LOCK:
                            d = _cargar(sid)
                            d["hojas"][i]["devolucion_ejecutada"] = {
                                "ban_dev": res["ban_dev"], "tray_id": chk[0]["id"],
                                "estado": chk[0].get("state"),
                                "aprobada": res["aprobada"],
                                "ts": datetime.now().isoformat(timespec="seconds")}
                            if res.get("motoverificacion"):
                                d["hojas"][i]["bandeja_creada"] = res["motoverificacion"]
                            _guardar(sid, d)
                    else:
                        res["advertencias"].append(
                            "⚠ La bandeja no apareció en la verificación de lectura — revisar en SGS")
                except Exception as e:
                    res["advertencias"].append(f"Verificación post-escritura falló: {e}")
            elif not dry_run and res["status"] == "error" and "GUARDAR" in (res["error"] or ""):
                res["advertencias"].append(
                    "⚠ hubo error tras GUARDAR — VERIFICAR EN SGS antes de reintentar")
        except Exception as e:
            log.exception("Devolución hoja %s de %s falló", i, sid)
            res["status"] = "error"
            res["error"] = str(e)
        return res

    # Cola COMPARTIDA de hojas pendientes (antes: reparto fijo round-robin). Si un
    # navegador no logra entrar a SGS, el otro sigue con TODAS las hojas en vez de
    # que las del caído queden en "error" (incidente 2026-09-11: 4 de 9 hojas).
    cola = deque()

    def _fila_error(i, msg):
        return {"hoja": i, "ban_origen": f"hoja {i+1}", "status": "error",
                "ban_dev": None, "estado_sgs": None, "intentos": 0, "error": msg,
                "advertencias": [], "url_sgs": None, "aprobada": False}

    def _worker(wid):
        time.sleep(wid * DEV_STAGGER)
        with _mtx:
            if not cola:
                return                # el otro navegador ya se llevó todo
        pw = None
        actual = None
        try:
            pw = PlaywrightClient(headless=True, slow_mo=100, **_cred_sid(sid))
            pw.start()
            pw.login()                # reintenta solo; si aun así falla, NO toma hojas
            while True:
                with _mtx:
                    if not cola:
                        break
                    actual = cola.popleft()
                res = _procesar_hoja(pw, actual)
                with _mtx:
                    resultados.append(res)
                    contador["hechas"] += 1
                actual = None
                _progreso()
        except Exception as e:
            log.exception("Worker %d de devoluciones falló", wid)
            if actual is not None:
                # Murió A MITAD de una hoja: pudo haber alcanzado GUARDAR → sin reintento:
                # esa hoja NO vuelve a la cola, se reporta para verificar a mano.
                with _mtx:
                    if not any(r["hoja"] == actual for r in resultados):
                        resultados.append(_fila_error(
                            actual, f"navegador caído a mitad de la hoja: {e} — ⚠ "
                                    f"VERIFICAR EN SGS antes de reintentar"))
                        contador["hechas"] += 1
                _progreso()
            # Las hojas que este navegador NO tomó siguen en la cola para el otro
        finally:
            if pw:
                try:
                    pw.close()
                except Exception:
                    pass

    try:
        with _LOCK:
            d = _cargar(sid)
            _marcar_duplicados(d)     # veto ANTES de repartir a los workers:
            _guardar(sid, d)          # cierra la carrera de 2 fotos en paralelo
            indices = [i for i, h in enumerate(d.get("hojas", [])) if h.get("preanalisis")]
        contador["total"] = len(indices)
        _set_job(estado="corriendo", dry_run=dry_run, resultados=[], hechas=0,
                 total=len(indices), pct=0,
                 msg=f"Abriendo {DEV_WORKERS} navegadores y entrando a SGS...")

        cola.extend(indices)
        hilos = []
        for w in range(min(DEV_WORKERS, len(indices))):
            t = threading.Thread(target=_worker, args=(w,), daemon=True)
            hilos.append(t)
            t.start()
        for t in hilos:
            t.join()
        # Hojas que ningún navegador alcanzó a tomar (todos fallaron al entrar a
        # SGS): no se escribió NADA de ellas → seguro volver a pulsar el botón.
        with _mtx:
            while cola:
                resultados.append(_fila_error(
                    cola.popleft(), "no procesada: ningún navegador pudo entrar a SGS "
                                    "(no se escribió nada) — pulsa el botón de nuevo"))
                contador["hechas"] += 1
        _progreso()

        ok = sum(1 for r in resultados if r["status"] in ("ok", "dry_run_ok", "ya_ejecutada"))
        apr = sum(1 for r in resultados if r.get("aprobada"))
        err = sum(1 for r in resultados if r["status"] == "error")
        _set_job(estado="fin", hechas=contador["hechas"], pct=100,
                 msg=(f"{'PRUEBA' if dry_run else '✓ EJECUCIÓN'} completa: {ok} ok "
                      f"({apr} aprobadas), {err} con error, "
                      f"{len(resultados)-ok-err} omitidas — ver reporte"),
                 resultados=sorted(resultados, key=lambda r: r["hoja"]),
                 fin=datetime.now().isoformat(timespec="seconds"))
    except Exception as e:
        log.exception("Job de devoluciones %s falló", sid)
        _set_job(estado="error", msg=f"✗ {e}",
                 resultados=sorted(resultados, key=lambda r: r["hoja"]))
    finally:
        _JOBS.pop(sid + "_dev", None)


def _comparar_despacho_con_plan(ban, adic):
    """Motoverificador del despacho adicional: type despacho, equipo y
    cantidades exactas de lo solicitado."""
    t = _sgs_op(lambda cli: cli.search_read(
        "project.tray", [["name", "=", ban]],
        ["id", "name", "type", "state", "partner_id", "project_tray_material_ids"],
        limit=1))
    c = _SGS        # proxy con re-login automático
    if not t:
        return False, {"ban": ban, "ok": False, "checks": ["✗ Bandeja no encontrada"]}
    t = t[0]
    det = {"ban": ban, "tray_id": t["id"], "estado": t.get("state"),
           "equipo": t["partner_id"][1] if t.get("partner_id") else "", "checks": [],
           "ok": False}
    ok = str(t.get("type") or "").lower() != "devolution"
    if not ok:
        det["checks"].append("✗ type devolution (se esperaba despacho)")
    if adic.get("equipo") and not _nombres_coinciden(det["equipo"], adic["equipo"]):
        ok = False
        det["checks"].append(f"✗ Equipo '{det['equipo']}' ≠ plan '{adic['equipo']}'")
    tms = c.search_read("project.tray.material",
                        [["id", "in", t.get("project_tray_material_ids") or []]],
                        ["product_id", "amount"], limit=100)
    esperados = {str(i["ref"]): float(i["cantidad"]) for i in adic.get("items", [])
                 if i.get("estado") == "ok"}
    for tm in tms:
        disp = _norm(tm["product_id"][1]) if tm.get("product_id") else ""
        qty = float(tm.get("amount") or 0)
        enc = next((r for r in esperados if f"/{r}]" in disp), None)
        if enc is None:
            ok = False
            det["checks"].append("✗ Material extra en la bandeja")
        elif abs(qty - esperados[enc]) > 0.001:
            ok = False
            det["checks"].append(f"✗ {enc}: cantidad {qty:g} ≠ pedido {esperados[enc]:g}")
    if ok and not det["checks"]:
        det["checks"].append("✓ Despacho = pedido")
    det["ok"] = ok
    return ok, det


def _job_adicionales(sid, dry_run, solo=None):
    """Despachos adicionales: SECUENCIAL (1 worker) hasta validar el flujo,
    como manda la regla del proyecto para flujos nuevos. No se interrumpe:
    reporte por hoja + idempotencia (adicional_ejecutado).
    solo: índices de hoja a ejecutar (botón por hoja del panel +); None = todas.
    Orden = orden de las fotos, el mismo de la pila de hojas físicas."""
    def _set(**kw):
        with _LOCK:
            d = _cargar(sid)
            d.setdefault("adic_job", {}).update(kw)
            _guardar(sid, d)
    resultados = []
    pw = None
    try:
        from browser_client import PlaywrightClient, BrowserActionError
        with _LOCK:
            d = _cargar(sid)
            _marcar_duplicados(d)
            _guardar(sid, d)
            indices = [i for i, h in enumerate(d.get("hojas", []))
                       if (h.get("preanalisis") or {}).get("adicionales")
                       and (solo is None or i in solo)]
        if not indices:
            _set(estado="fin", msg="Ninguna hoja tiene adicionales — abre + Adicionales")
            return
        _set(estado="corriendo", dry_run=dry_run, total=len(indices), hechas=0, pct=0,
             resultados=[], solo=solo, msg="Abriendo navegador...")
        pw = PlaywrightClient(headless=True, slow_mo=100, **_cred_sid(sid))
        pw.start()
        pw.login()
        for n, i in enumerate(indices, start=1):
            with _LOCK:
                h = _cargar(sid)["hojas"][i]
            ocr = h.get("ocr") or {}
            ban_origen = ocr.get("ban_code") or f"hoja {i+1}"
            res = {"hoja": i, "ban_origen": ban_origen, "status": "", "ban_dev": None,
                   "estado_sgs": None, "error": None, "advertencias": [],
                   "aprobada": False, "url_sgs": None, "intentos": 1}
            try:
                if h.get("duplicada_de"):
                    res["status"] = "duplicada"
                    res["error"] = f"Foto repetida de #{h['duplicada_de']}"
                elif h.get("continuacion_de"):
                    res["status"] = "continuacion"
                    res["error"] = (f"Página de continuación de #{h['continuacion_de']} — "
                                    f"sus notas se ejecutan con esa hoja")
                elif h.get("adicional_ejecutado") and not dry_run:
                    res["status"] = "ya_ejecutada"
                    res["ban_dev"] = h["adicional_ejecutado"].get("ban")
                elif not dry_run and (otra := _devolucion_en_otra_sesion(
                        sid, ocr.get("ban_code"), marca="adicional_ejecutado")):
                    res["status"] = "ya_ejecutada"
                    res["ban_dev"] = otra["ban_dev"]
                    res["error"] = (f"El adicional de esta hoja ya se creó en la sesión "
                                    f"{otra['sesion']} ({otra['ban_dev']}) — no se repite")
                else:
                    todos = _adic_todos(sid)
                    with _SGS_SEM:
                        plan = preanalisis_hoja(h, adic_todos=todos, zona=_zona_sid(sid))     # fresco
                    with _LOCK:
                        d = _cargar(sid)
                        d["hojas"][i]["preanalisis"] = plan
                        _guardar(sid, d)
                    adic = plan.get("adicionales") or {}
                    res["url_sgs"] = plan.get("url_sgs")
                    items = [{"ref": a["ref"], "cantidad": a["cantidad"],
                              "proyectado_nuevo": a["proyectado_nuevo"]}
                             for a in adic.get("items", []) if a.get("estado") == "ok"]
                    if not adic:
                        res["status"] = "omitida"
                        res["error"] = "Sin adicionales en el preanálisis fresco"
                    elif not adic.get("autorizado"):
                        res["status"] = "omitida"
                        res["error"] = adic.get("detalle") or "Supervisor no autorizado"
                    elif not items:
                        res["status"] = "sin_ejecutables"
                        res["error"] = "Ningún adicional ejecutable (¿material no existe?)"
                    elif not adic.get("equipo"):
                        res["status"] = "error"
                        res["error"] = "SGS no reporta líder (Equipo)"
                    else:
                        def _ap(ban, _a=adic, _r=res):
                            with _SGS_SEM:
                                ok, det = _comparar_despacho_con_plan(ban, _a)
                            _r["motoverificacion"] = det
                            return ok

                        def _va(esperados, _pid=plan["nodo_id"]):
                            """Qué proyectados NO persistieron de verdad en SGS."""
                            with _SGS_SEM:
                                pms2 = _SGS.search_read(
                                    "project.material", [["project_id", "=", _pid]],
                                    ["product_id", "amount"], limit=500)
                            mal = {}
                            for rf, obj in esperados.items():
                                pm2 = next((p2 for p2 in pms2 if p2.get("product_id")
                                            and f"/{rf}]" in _norm(p2["product_id"][1])), None)
                                act = float(pm2["amount"]) if pm2 else None
                                if act is None or abs(act - float(obj)) > 0.001:
                                    mal[rf] = act
                            return mal

                        if not dry_run:
                            # Fijar el objetivo de cada ref UNA sola vez (idempotencia
                            # entre reintentos: nunca se re-suma sobre lo ya subido)
                            with _LOCK:
                                d = _cargar(sid)
                                hj = d["hojas"][i]
                                mapa = hj.get("adicional_proyectados") or {}
                                for it2 in items:
                                    mapa.setdefault(str(it2["ref"]), it2["proyectado_nuevo"])
                                hj["adicional_proyectados"] = mapa
                                _guardar(sid, d)

                        _set(msg=f"+ [{n}/{len(indices)}] {ban_origen}: "
                                 f"{len(items)} adicionales ({'TEST' if dry_run else 'REAL'})...")
                        try:
                            r = pw.crear_despacho_adicional(
                                nodo_id=plan["nodo_id"], items=items,
                                partner_name=adic["equipo"], date_str=adic["fecha"],
                                location_search=adic.get("almacen") or _almacen_zona(_zona_sid(sid), "Stock"),
                                dry_run=dry_run, screenshots=True,
                                aprobar_si=None if dry_run else _ap,
                                verificar_amounts=None if dry_run else _va)
                            res.update(status=r["status"], ban_dev=r.get("ban_code"),
                                       estado_sgs=r.get("estado"),
                                       aprobada=bool(r.get("aprobada")))
                            res["advertencias"] += r.get("advertencias") or []
                            if not dry_run and r["status"] == "ok" and r.get("ban_code"):
                                # mismo falso negativo que en devoluciones: la API manda
                                try:
                                    with _SGS_SEM:
                                        chk = _SGS.search_read(
                                            "project.tray", [["name", "=", r["ban_code"]]],
                                            ["state"], limit=1)
                                    if chk and str(chk[0].get("state") or "").lower() == "approved":
                                        res["aprobada"] = True
                                        res["estado_sgs"] = "APROBADO"
                                except Exception:
                                    pass
                                with _LOCK:
                                    d = _cargar(sid)
                                    d["hojas"][i]["adicional_ejecutado"] = {
                                        "ban": r["ban_code"], "estado": res.get("estado_sgs"),
                                        "aprobada": bool(res.get("aprobada")),
                                        "ts": datetime.now().isoformat(timespec="seconds")}
                                    _guardar(sid, d)
                        except BrowserActionError as e:
                            res["status"] = "error"
                            res["error"] = str(e)
            except Exception as e:
                log.exception("Adicional hoja %s de %s falló", i, sid)
                res["status"] = "error"
                res["error"] = str(e)
            resultados.append(res)
            _set(hechas=n, pct=round(100 * n / len(indices)), resultados=resultados)
        ok = sum(1 for r in resultados if r["status"] in ("ok", "dry_run_ok", "ya_ejecutada"))
        err = sum(1 for r in resultados if r["status"] == "error")
        _set(estado="fin", pct=100,
             msg=(f"{'PRUEBA' if dry_run else '✓'} adicionales: {ok} ok, {err} error, "
                  f"{len(resultados)-ok-err} omitidas"),
             resultados=resultados)
    except Exception as e:
        log.exception("Job adicionales %s falló", sid)
        _set(estado="error", msg=f"✗ {e}", resultados=resultados)
    finally:
        if pw:
            try:
                pw.close()
            except Exception:
                pass
        _JOBS.pop(sid + "_adic", None)


def _recuperar_sesiones_interrumpidas():
    """Al arrancar el servidor: sesiones que quedaron 'procesando' por un reinicio
    se marcan para reanudar (el botón Procesar continúa donde iba, sin repetir)."""
    try:
        for sid in os.listdir(DIR_SESIONES):
            try:
                d = _cargar(sid)
            except Exception:
                continue
            cambio = False
            if d.get("estado") == "procesando":
                hechas = len([h for h in d.get("hojas", []) if h.get("ocr")])
                d["estado"] = "error"
                d.pop("ocr_batch", None)   # modo LOTE retirado 2026-09-04: nada que retomar
                d["progreso"]["msg"] = (f"⚠ Interrumpido por reinicio del servidor "
                                        f"({hechas}/{len(d.get('fotos', []))} hojas listas). "
                                        f"Pulsa «Procesar» para continuar donde iba.")
                cambio = True
            if (d.get("dev_job") or {}).get("estado") == "corriendo":
                creadas = len([h for h in d.get("hojas", []) if h.get("devolucion_ejecutada")])
                d["dev_job"]["estado"] = "error"
                d["dev_job"]["msg"] = (f"⚠ Interrumpido por reinicio del servidor "
                                       f"({creadas} bandejas ya creadas). Pulsa el botón de "
                                       f"nuevo: las creadas NO se repiten.")
                cambio = True
            if (d.get("aprob_job") or {}).get("estado") == "corriendo":
                d["aprob_job"]["estado"] = "error"
                d["aprob_job"]["msg"] = "⚠ Interrumpido por reinicio — vuelve a pulsar Aprobar"
                cambio = True
            if (d.get("verif_job") or {}).get("estado") == "corriendo":
                d["verif_job"]["estado"] = "error"
                d["verif_job"]["msg"] = "⚠ Interrumpido por reinicio — reabre la revisión"
                cambio = True
            if (d.get("adic_job") or {}).get("estado") == "corriendo":
                d["adic_job"]["estado"] = "error"
                d["adic_job"]["msg"] = ("⚠ Interrumpido por reinicio — pulsa de nuevo: "
                                        "las hojas ejecutadas no se repiten")
                cambio = True
            if cambio:
                _guardar(sid, d)
                log.warning("Sesión %s recuperada de jobs interrumpidos", sid)
    except Exception:
        log.exception("Error recuperando sesiones interrumpidas")


_recuperar_sesiones_interrumpidas()


# ══════════════════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/dev/info")
def api_info():
    u = _usuario() or {}
    z = _zona_ok(u.get("zona"))
    extra = {"usuario": u.get("nombre"), "rol": u.get("rol"), "auth": DEV_REQUIRE_AUTH,
             "zona": z, "zona_nombre": ZONAS[z]["nombre"], "bodega": ZONAS[z]["bodega"],
             "zona_operativa": _zona_operativa(z),
             "zona_base_nombre": ZONAS[ZONA_BASE]["nombre"]}
    base = _url_publica()
    if base:
        return jsonify({"ips": [], "puerto": PORT, "url_movil": f"{base}/movil", **extra})
    ips = _ips_locales()
    return jsonify({"ips": ips, "puerto": PORT,
                    "url_movil": f"https://{ips[0]}:{PORT}/movil" if ips else None, **extra})


@app.post("/api/dev/sesion")
def api_crear_sesion():
    origen = (request.json or {}).get("origen", "pc") if request.is_json else "pc"
    return jsonify(crear_sesion(origen))


@app.get("/api/dev/sesion-activa")
def api_sesion_activa():
    origen = request.args.get("origen", "movil")
    return jsonify(sesion_activa(origen, _usuario()))


@app.get("/api/dev/sesiones")
def api_sesiones():
    u = _usuario() or {}
    return jsonify([x for x in listar_sesiones() if _ve_zona(u, x.get("zona"))])


@app.get("/api/dev/sesion/<sid>")
def api_sesion(sid):
    try:
        with _LOCK:
            return jsonify(_cargar(sid))
    except FileNotFoundError:
        return jsonify({"error": "Sesión no existe"}), 404


@app.post("/api/dev/sesion/<sid>/foto")
def api_subir_foto(sid):
    blob = request.get_data()
    if not blob or len(blob) < 1000:
        return jsonify({"error": "Imagen vacía"}), 400
    origen = request.headers.get("X-Origen", "desconocido")
    with _LOCK:
        d = _cargar(sid)
        n = d["siguiente_n"]
        d["siguiente_n"] = n + 1
        archivo = f"{n}.jpg"
        with open(os.path.join(_dir_sesion(sid), "fotos", archivo), "wb") as f:
            f.write(blob)
        d["fotos"].append({"n": n, "archivo": archivo, "origen": origen,
                           "ts": datetime.now().isoformat(timespec="seconds")})
        if d["estado"] in ("procesado", "error"):
            d["estado"] = "captura"      # nuevas fotos reabren la captura
        _guardar(sid, d)
    log.info("Foto %s subida a %s (%d KB, %s)", n, sid, len(blob) // 1024, origen)
    auto = False
    if _auto_ocr_activo(d):
        _lanzar_job_procesar(sid)        # si ya corre, la toma en su próxima vuelta
        auto = True
    return jsonify({"n": n, "total": len(d["fotos"]), "auto_ocr": auto})


@app.delete("/api/dev/sesion/<sid>/foto/<int:n>")
def api_borrar_foto(sid, n):
    with _LOCK:
        d = _cargar(sid)
        d["fotos"] = [f for f in d["fotos"] if f["n"] != n]
        try:
            os.remove(os.path.join(_dir_sesion(sid), "fotos", f"{n}.jpg"))
        except OSError:
            pass
        _guardar(sid, d)
    return jsonify({"ok": True, "total": len(d["fotos"])})


@app.get("/api/dev/sesion/<sid>/foto/<int:n>")
def api_ver_foto(sid, n):
    ruta = os.path.join(_dir_sesion(sid), "fotos", f"{n}.jpg")
    if not os.path.exists(ruta):
        return "no existe", 404
    return send_file(ruta, mimetype="image/jpeg", max_age=3600)


@app.post("/api/dev/sesion/<sid>/procesar")
def api_procesar(sid):
    with _LOCK:
        d = _cargar(sid)
        if not d["fotos"]:
            return jsonify({"error": "La sesión no tiene fotos"}), 400
    if not _lanzar_job_procesar(sid):
        return jsonify({"error": "Ya se está procesando"}), 409
    return jsonify({"ok": True})


@app.post("/api/dev/sesion/<sid>/auto")
def api_auto_ocr(sid):
    """Enciende/apaga el auto-OCR de la sesión. Al encenderlo, si hay fotos sin
    hoja arranca el job de una vez."""
    valor = bool((request.json or {}).get("valor", True))
    with _LOCK:
        d = _cargar(sid)
        d["auto_ocr"] = valor
        hechas = {h["foto_n"] for h in d.get("hojas", []) if h.get("ocr") or h.get("error")}
        pendientes = [f for f in d["fotos"] if f["n"] not in hechas]
        _guardar(sid, d)
    if valor and pendientes:
        _lanzar_job_procesar(sid)
    return jsonify({"ok": True, "auto_ocr": valor})


@app.post("/api/dev/sesion/<sid>/reprocesar")
def api_reprocesar(sid):
    """Borra el OCR de TODAS las hojas (las fotos quedan intactas) para volver
    a procesar desde cero — p.ej. tras un cambio de modelo/backend que dejó
    lecturas malas. Las marcas de ejecución (devoluciones/adicionales YA
    creados en SGS) se conservan en `marcas_ejecucion` y los jobs las
    re-adjuntan por foto_n: el seguro anti doble-escritura sobrevive.
    Las correcciones humanas sobre el OCR viejo se descartan (eran sobre
    lecturas que van a desaparecer)."""
    with _LOCK:
        d = _cargar(sid)
        if d["estado"] == "procesando" or sid in _JOBS:
            return jsonify({"error": "La sesión se está procesando — espera a que termine"}), 409
        for j in ("dev_job", "aprob_job", "adic_job", "verif_job"):
            if (d.get(j) or {}).get("estado") == "corriendo":
                return jsonify({"error": f"Hay un job en curso ({j}) — espera a que termine"}), 409
        n_ed = sum(len(h.get("ediciones") or []) for h in d.get("hojas", []))
        marcas = {}
        for h in d.get("hojas", []):
            m = {k: h[k] for k in _MARCAS_EJECUCION if h.get(k)}
            if m:
                marcas[str(h["foto_n"])] = m
        # fusionar con marcas de un reproceso anterior que aún no se re-adjuntaron
        for n_str, m in (d.get("marcas_ejecucion") or {}).items():
            marcas.setdefault(n_str, m)
        if marcas:
            d["marcas_ejecucion"] = marcas
        n_hojas = len(d.get("hojas", []))
        d["hojas"] = []
        d.pop("ocr_batch", None)     # un lote viejo no debe "retomarse" con OCR borrado
        d["estado"] = "captura"
        d["progreso"] = {"actual": 0, "total": len(d.get("fotos", [])),
                         "msg": ("↻ OCR borrado — pulsa «Procesar» para reprocesar desde cero"
                                 + (f" · {len(marcas)} marca(s) de ejecución conservadas"
                                    if marcas else "")
                                 + (f" · {n_ed} edición(es) descartadas" if n_ed else ""))}
        _guardar(sid, d)
        log.warning("Sesión %s: reproceso solicitado — %d hojas borradas, "
                    "%d marcas conservadas, %d ediciones descartadas",
                    sid, n_hojas, len(marcas), n_ed)
    if _auto_ocr_activo(d):
        _lanzar_job_procesar(sid)
    return jsonify({"ok": True, "hojas_borradas": n_hojas,
                    "marcas_conservadas": len(marcas), "ediciones_descartadas": n_ed})


@app.post("/api/dev/sesion/<sid>/hoja/<int:idx>/editar")
def api_editar_hoja(sid, idx):
    """Corrección humana de una hoja procesada.

    Body JSON:
      {"mat": 3, "campo": "devolucion", "valor": 5}      → edita un material
      {"mat": 3, "campo": "confirmado", "valor": true}   → confirma/desconfirma fila
      {"campo": "ban_code", "valor": "BAN-116660"}       → edita cabecera (BAN/PRO)
    """
    body = request.json or {}
    campo, valor = body.get("campo"), body.get("valor")
    CAMPOS_MAT_NUM = ("devolucion", "entregado", "solicitado", "devolucion_manuscrita")
    with _LOCK:
        d = _cargar(sid)
        hojas = d.get("hojas", [])
        if not (0 <= idx < len(hojas)):
            return jsonify({"error": "Hoja no existe"}), 404
        h = hojas[idx]
        o = h.get("ocr")
        if not o:
            return jsonify({"error": "La hoja no tiene OCR"}), 400
        registro = {"ts": datetime.now().isoformat(timespec="seconds"),
                    "campo": campo, "mat": body.get("mat"), "valor": valor,
                    "usuario": (_usuario() or {}).get("nombre")}

        if body.get("mat") is not None:
            mi = int(body["mat"])
            if not (0 <= mi < len(o.get("materiales", []))):
                return jsonify({"error": "Material no existe"}), 404
            m = o["materiales"][mi]
            if campo in CAMPOS_MAT_NUM:
                registro["antes"] = m.get(campo)
                try:
                    m[campo] = None if valor in (None, "") else float(valor)
                except (TypeError, ValueError):
                    return jsonify({"error": f"Valor no numérico: {valor}"}), 400
                m["fuente"] = "humano"
                m["confianza"] = "alta"
            elif campo == "confirmado":
                registro["antes"] = m.get("confirmado")
                m["confirmado"] = bool(valor)
            else:
                return jsonify({"error": f"Campo no editable: {campo}"}), 400
        elif campo in ("ban_code", "pro_code"):
            registro["antes"] = o.get(campo)
            o[campo] = str(valor or "").strip().upper()
        else:
            return jsonify({"error": f"Campo no editable: {campo}"}), 400

        h.setdefault("ediciones", []).append(registro)
        h["ocr"] = _consolidar_hoja(o, h.get("verificacion") or {})
        _marcar_duplicados(d)     # editar un BAN puede crear/deshacer duplicados
        _guardar(sid, d)
    return jsonify({"ok": True})


@app.post("/api/dev/sesion/<sid>/hoja/<int:idx>/validar")
def api_validar_hoja(sid, idx):
    """Almacén (o el administrador) marca la hoja como contada y revisada. Queda quién y
    cuándo; el administrador la ve al reverificar y crear las bandejas."""
    valor = bool((request.json or {}).get("valor", True))
    u = _usuario() or {"nombre": "?", "rol": "?"}
    with _LOCK:
        d = _cargar(sid)
        hojas = d.get("hojas", [])
        if not (0 <= idx < len(hojas)):
            return jsonify({"error": "Hoja no existe"}), 404
        h = hojas[idx]
        if valor:
            mats = (h.get("ocr") or {}).get("materiales") or []
            h["validada"] = {"por": u["nombre"], "rol": u["rol"],
                             "ts": datetime.now().isoformat(timespec="seconds"),
                             "rojos": len([m for m in mats if m.get("color") == "rojo"]),
                             "amarillos": len([m for m in mats if m.get("color") == "amarillo"])}
        else:
            h.pop("validada", None)
        h.setdefault("ediciones", []).append({
            "ts": datetime.now().isoformat(timespec="seconds"), "campo": "validada",
            "mat": None, "valor": valor, "usuario": u["nombre"]})
        _guardar(sid, d)
    return jsonify({"ok": True, "validada": h.get("validada")})


def _semaforo_hoja(h):
    v = h.get("verificacion") or {}
    mats = (h.get("ocr") or {}).get("materiales") or []
    if h.get("error") or v.get("estado") == "rojo" or any(m.get("color") == "rojo" for m in mats):
        return "rojo"
    if any(m.get("color") == "amarillo" for m in mats):
        return "amarillo"
    return "verde"


def hojas_por_ban(bans, dias=120):
    """Para cada BAN de despacho, la hoja fotografiada más útil: primero la
    VALIDADA más reciente; si ninguna está validada, la última leída. Salta
    duplicadas, continuaciones y hojas con error. Recorre las sesiones de los
    últimos `dias` días (solo lectura de session.json)."""
    bans = {b.strip().upper() for b in bans if b and b.strip()}
    if not bans:
        return {}
    limite = (datetime.now() - timedelta(days=dias)).strftime("%Y%m%d")
    mejor = {}
    for sid in sorted(os.listdir(DIR_SESIONES), reverse=True):
        if sid[:8] < limite:
            break
        try:
            with _LOCK:
                d = _cargar(sid)
        except Exception:
            continue
        for h in d.get("hojas", []):
            o = h.get("ocr") or {}
            ban = (o.get("ban_code") or "").strip().upper()
            if ban not in bans or h.get("error") or h.get("duplicada_de") or h.get("continuacion_de"):
                continue
            val = h.get("validada") or {}
            clave = (1 if val else 0, val.get("ts") or "", sid, h.get("foto_n") or 0)
            if ban in mejor and mejor[ban]["_clave"] >= clave:
                continue
            v = h.get("verificacion") or {}
            mejor[ban] = {
                "_clave": clave, "sesion": sid, "foto_n": h.get("foto_n"),
                "creada_por": d.get("creada_por", ""), "validada": val or None,
                "semaforo": _semaforo_hoja(h), "pro_code": o.get("pro_code", ""),
                "proyecto_desc": o.get("proyecto_desc", ""), "lider_sgs": v.get("lider_sgs", ""),
                "fecha_recibido": o.get("fecha_recibido", ""),
                "url": f"{_url_publica() or ''}/?pc=1#{sid}",
                "materiales": [{
                    "dom": m.get("codigo_dom", ""), "ref": m.get("ref_interna", ""),
                    "descripcion": m.get("descripcion", ""), "entregado": m.get("entregado"),
                    "devolucion": m.get("devolucion"), "color": m.get("color", ""),
                    "fuente": m.get("fuente", "ocr"), "confirmado": bool(m.get("confirmado")),
                } for m in o.get("materiales", [])],
            }
    for x in mejor.values():
        x.pop("_clave", None)
    return mejor


def buscar_hojas(q, dias=365, limite=60):
    """Buscador global: BAN, PRO, texto del NODO (p.ej. '6301025493 NODO3') o líder,
    en todas las sesiones de los últimos `dias` días. Solo lectura de session.json.
    Los números se comparan sin prefijo ni guion: '124687' encuentra BAN-124687 y
    '65328' encuentra PRO-065328."""
    q = (q or "").strip()
    if len(q) < 3:
        return []
    qn = _norm(q)
    qdig = re.sub(r"\D", "", q)
    tipo = "ban" if q.upper().startswith("BAN") else "pro" if q.upper().startswith("PRO") else ""
    lim = (datetime.now() - timedelta(days=dias)).strftime("%Y%m%d")
    out = []
    for sid in sorted(os.listdir(DIR_SESIONES), reverse=True):
        if sid[:8] < lim:
            break
        try:
            with _LOCK:
                d = _cargar(sid)
        except Exception:
            continue
        for h in d.get("hojas", []):
            o = h.get("ocr") or {}
            if not o:
                continue
            ban, pro = (o.get("ban_code") or "").upper(), (o.get("pro_code") or "").upper()
            v = h.get("verificacion") or {}
            hit = False
            if qdig and len(qdig) >= 3:
                bd, pd = re.sub(r"\D", "", ban), re.sub(r"\D", "", pro)
                if tipo != "pro" and bd and bd.endswith(qdig):
                    hit = True
                if tipo != "ban" and pd and pd.lstrip("0").endswith(qdig.lstrip("0")) and qdig.lstrip("0"):
                    hit = True
            if not hit and not tipo:
                texto = _norm(" ".join([o.get("proyecto_desc") or "", v.get("proyecto_sgs") or "",
                                        v.get("lider_sgs") or "", " ".join(o.get("asignada_a") or [])]))
                if qn and qn in texto:
                    hit = True
            if not hit:
                continue
            ej = h.get("devolucion_ejecutada") or {}
            mats = o.get("materiales") or []
            out.append({
                "sesion": sid, "foto_n": h.get("foto_n"), "creada_por": d.get("creada_por", ""),
                "ban": ban, "pro": pro, "nodo": o.get("proyecto_desc") or "",
                "lider": v.get("lider_sgs") or "", "semaforo": _semaforo_hoja(h),
                "validada": (h.get("validada") or {}).get("por", ""),
                "ban_dev": ej.get("ban_dev", ""), "aprobada": bool(ej.get("aprobada")),
                "adicional": (h.get("adicional_ejecutado") or {}).get("ban", ""),
                "duplicada": bool(h.get("duplicada_de")), "continuacion": bool(h.get("continuacion_de")),
                "n_dev": len([m for m in mats if isinstance(m.get("devolucion"), (int, float)) and m["devolucion"] > 0]),
                "fecha": d.get("creada", "")[:16].replace("T", " "),
            })
            if len(out) >= limite:
                return out
    return out


@app.get("/api/dev/buscar")
def api_buscar():
    u = _usuario() or {}
    return jsonify([r for r in buscar_hojas(request.args.get("q", ""),
                                            int(request.args.get("dias") or 365))
                    if _ve_zona(u, _zona_sid(r.get("sesion")))])


@app.get("/api/dev/hojas-por-ban")
def api_hojas_por_ban():
    """Servidor-a-servidor (X-Dev-Token) o usuario logueado. ?bans=BAN-1,BAN-2"""
    if not DEV_API_TOKEN:
        return jsonify({"error": "DEV_API_TOKEN no configurado"}), 503
    if not (_token_ok() or (_usuario() and (session.get("usuario") or not DEV_REQUIRE_AUTH))):
        return jsonify({"error": "no autorizado"}), 401
    bans = (request.args.get("bans") or "").split(",")
    try:
        dias = int(request.args.get("dias") or 120)
    except ValueError:
        dias = 120
    return jsonify(hojas_por_ban(bans, dias))


@app.get("/api/dev/sesion/<sid>/excel")
def api_excel(sid):
    """Excel de la sesión para almacén: una fila por material con devolución
    (hoja 'Devoluciones') y todas las filas leídas (hoja 'Todas las filas').
    ⚠ Formato provisional: se ajusta al reporte real que usa almacén."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    with _LOCK:
        d = _cargar(sid)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Devoluciones"
    ws2 = wb.create_sheet("Todas las filas")
    cab = ["Fecha recibido", "BAN", "PRO", "NODO", "Líder (SGS)", "Código DOM", "Ref",
           "Descripción", "Entregado", "Devolución", "Semáforo", "Validada por", "Foto"]
    for w in (ws, ws2):
        w.append(cab)
        for c in w[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="DBEEF2")
    for h in d.get("hojas", []):
        o = h.get("ocr") or {}
        if not o or h.get("error") or h.get("continuacion_de") or h.get("duplicada_de"):
            continue
        v = h.get("verificacion") or {}
        val = h.get("validada") or {}
        base = [o.get("fecha_recibido") or "", o.get("ban_code") or "", o.get("pro_code") or "",
                o.get("proyecto_desc") or "", v.get("lider_sgs") or ""]
        for m in o.get("materiales", []):
            fila = base + [m.get("codigo_dom") or "", m.get("ref_interna") or "",
                           m.get("descripcion") or "", m.get("entregado"), m.get("devolucion"),
                           m.get("color") or "", val.get("por") or "", h.get("foto_n")]
            ws2.append(fila)
            if isinstance(m.get("devolucion"), (int, float)) and m["devolucion"] > 0:
                ws.append(fila)
    for w in (ws, ws2):
        for col, ancho in zip("ABCDEFGHIJKLM", (14, 12, 12, 26, 30, 14, 9, 44, 10, 11, 10, 14, 6)):
            w.column_dimensions[col].width = ancho
        w.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    nombre = f"devoluciones_{sid}.xlsx"
    return send_file(buf, as_attachment=True, download_name=nombre,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/api/dev/sesion/<sid>/reverificar-todo")
def api_reverificar_todo(sid):
    """Re-verifica TODAS las hojas contra SGS (sin repetir OCR). Útil cuando la
    sesión SGS caducó y toda la verificación quedó en rojo falso."""
    with _LOCK:
        d = _cargar(sid)
        indices = [i for i, h in enumerate(d.get("hojas", []))
                   if h.get("ocr") and not h.get("continuacion_de")]
    if not indices:
        return jsonify({"error": "Sin hojas procesadas"}), 400
    verdes = 0
    for i in indices:
        with _LOCK:
            h = _cargar(sid)["hojas"][i]
        with _SGS_SEM:
            verif = verificar_hoja_sgs(h["ocr"])
        if verif.get("estado") == "verde":
            verdes += 1
        with _LOCK:
            d = _cargar(sid)
            d["hojas"][i]["verificacion"] = verif
            d["hojas"][i]["ocr"] = _consolidar_hoja(d["hojas"][i]["ocr"], verif)
            _guardar(sid, d)
    with _LOCK:
        d = _cargar(sid)
        ndup = _marcar_duplicados(d)
        _vincular_continuaciones(d)
        _guardar(sid, d)
    return jsonify({"ok": True, "hojas": len(indices), "verdes": verdes,
                    "duplicadas": ndup})


@app.post("/api/dev/sesion/<sid>/hoja/<int:idx>/editar-nota")
def api_editar_nota(sid, idx):
    """Corrección humana de una nota manuscrita (adicionales): referencia,
    cantidad o tipo (descartar/restaurar). Se guarda en la NOTA del OCR para
    sobrevivir a los recálculos del preanálisis, y el plan se recalcula ya."""
    body = request.json or {}
    # Un cambio ({campo, valor}) o varios a la vez ({cambios: {campo: valor}}):
    # el panel + convierte una duda en adicional con código + cantidad en un clic
    cambios = body.get("cambios") or {body.get("campo"): body.get("valor")}
    for campo in cambios:
        if campo not in ("ref_interna", "cantidad", "tipo", "forzar"):
            return jsonify({"error": f"Campo no editable: {campo}"}), 400
    u = _usuario() or {}
    with _LOCK:
        d = _cargar(sid)
        hojas = d.get("hojas", [])
        if not (0 <= idx < len(hojas)):
            return jsonify({"error": "Hoja no existe"}), 404
        h = hojas[idx]
        notas = (h.get("ocr") or {}).get("notas_manuscritas") or []
        ni = body.get("nota")
        if ni is None or not (0 <= int(ni) < len(notas)):
            return jsonify({"error": "Nota no existe"}), 404
        nota = notas[int(ni)]
        for campo, valor in cambios.items():
            antes = nota.get(campo)
            if campo == "cantidad":
                try:
                    nota["cantidad"] = None if valor in (None, "") else float(valor)
                except (TypeError, ValueError):
                    return jsonify({"error": f"Cantidad no numérica: {valor}"}), 400
            elif campo == "ref_interna":
                nota["ref_interna"] = str(valor or "").strip().upper()
            elif campo == "forzar":
                # "ya salió en otro despacho, pero es OTRO pedido": crear igual
                nota["forzar"] = bool(valor)
            else:
                if valor not in ("adicional", "devolucion", "duda", "descartada"):
                    return jsonify({"error": f"Tipo inválido: {valor}"}), 400
                nota["tipo"] = valor
            h.setdefault("ediciones", []).append({
                "ts": datetime.now().isoformat(timespec="seconds"), "usuario": u.get("nombre"),
                "campo": f"nota.{campo}", "nota": int(ni), "antes": antes, "valor": valor})
        if "tipo" not in cambios and nota.get("tipo") in (None, "", "duda"):
            nota["tipo"] = "adicional"    # la editó en la tabla de adicionales
        nota["fuente"] = "humano"
        _guardar(sid, d)
    # Recalcular el plan con SGS fresco: la corrección puede casar el material
    with _LOCK:
        d0 = _cargar(sid)
        h, todos = d0["hojas"][idx], bool(d0.get("adic_todos"))
    with _SGS_SEM:
        plan = preanalisis_hoja(h, adic_todos=todos, zona=_zona_sid(sid))
    with _LOCK:
        d = _cargar(sid)
        d["hojas"][idx]["preanalisis"] = plan
        _guardar(sid, d)
    return jsonify({"ok": True})


@app.post("/api/dev/sesion/<sid>/hoja/<int:idx>/agregar-nota")
def api_agregar_nota(sid, idx):
    """Agrega a mano un adicional que el OCR no leyó (ref + cantidad)."""
    body = request.json or {}
    ref = str(body.get("ref_interna") or "").strip()
    try:
        cant = float(body.get("cantidad"))
    except (TypeError, ValueError):
        return jsonify({"error": "Cantidad inválida"}), 400
    if not ref or cant <= 0:
        return jsonify({"error": "Referencia vacía o cantidad ≤ 0"}), 400
    with _LOCK:
        d = _cargar(sid)
        hojas = d.get("hojas", [])
        if not (0 <= idx < len(hojas)) or not hojas[idx].get("ocr"):
            return jsonify({"error": "Hoja no existe o sin OCR"}), 404
        h = hojas[idx]
        h["ocr"].setdefault("notas_manuscritas", []).append({
            "texto": f"(agregado a mano) {ref} = {cant:g}",
            "ref_interna": ref, "cantidad": cant, "tipo": "adicional",
            "interpretacion": "", "fuente": "humano"})
        h.setdefault("ediciones", []).append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "campo": "nota.agregada", "valor": f"{ref}={cant:g}"})
        _guardar(sid, d)
    with _LOCK:
        d0 = _cargar(sid)
        h, todos = d0["hojas"][idx], bool(d0.get("adic_todos"))
    with _SGS_SEM:
        plan = preanalisis_hoja(h, adic_todos=todos, zona=_zona_sid(sid))
    with _LOCK:
        d = _cargar(sid)
        d["hojas"][idx]["preanalisis"] = plan
        _guardar(sid, d)
    return jsonify({"ok": True})


@app.post("/api/dev/sesion/<sid>/hoja/<int:idx>/reverificar")
def api_reverificar_hoja(sid, idx):
    """Re-ejecuta la verificación SGS (solo lectura) de una hoja, sin repetir el
    OCR. Útil después de corregir a mano el BAN/PRO de la cabecera."""
    with _LOCK:
        d = _cargar(sid)
        hojas = d.get("hojas", [])
        if not (0 <= idx < len(hojas)) or not hojas[idx].get("ocr"):
            return jsonify({"error": "Hoja no existe o sin OCR"}), 404
        ocr = hojas[idx]["ocr"]
    verif = verificar_hoja_sgs(ocr)          # I/O de red fuera del lock
    with _LOCK:
        d = _cargar(sid)
        h = d["hojas"][idx]
        h["verificacion"] = verif
        h["ocr"] = _consolidar_hoja(h["ocr"], verif)
        _marcar_duplicados(d)
        _guardar(sid, d)
    return jsonify({"ok": True, "estado": verif.get("estado")})


@app.post("/api/dev/sesion/<sid>/preanalisis")
def api_preanalisis(sid):
    if (_r := _solo_admin()):
        return _r
    """Corre el preanálisis de devolución (solo lectura) para todas las hojas."""
    with _LOCK:
        d = _cargar(sid)
        hojas_ok = [i for i, h in enumerate(d.get("hojas", []))
                    if h.get("ocr") and not h.get("continuacion_de")]
    if not hojas_ok:
        return jsonify({"error": "La sesión no tiene hojas procesadas"}), 400
    resultados = {}
    for i in hojas_ok:
        with _LOCK:
            d0 = _cargar(sid)
            h, todos = d0["hojas"][i], bool(d0.get("adic_todos"))
        with _SGS_SEM:
            plan = preanalisis_hoja(h, adic_todos=todos, zona=_zona_sid(sid))
        resultados[i] = plan
        with _LOCK:
            d = _cargar(sid)
            d["hojas"][i]["preanalisis"] = plan
            _guardar(sid, d)
    tot_ok = sum(p["ejecutables"] for p in resultados.values())
    tot_bloq = sum(p["bloqueados"] for p in resultados.values())
    return jsonify({"ok": True, "hojas": len(resultados),
                    "ejecutables": tot_ok, "bloqueados": tot_bloq})


@app.post("/api/dev/sesion/<sid>/aprender")
def api_aprender(sid):
    if (_r := _solo_admin()):
        return _r
    """Autoreparación: analiza con IA las correcciones humanas de esta sesión
    y guarda lecciones generalizables que se inyectan al prompt OCR futuro."""
    with _LOCK:
        d = _cargar(sid)
    correcciones = []
    for h in d.get("hojas", []):
        o = h.get("ocr") or {}
        mats = o.get("materiales") or []
        for ed in h.get("ediciones", []):
            if ed.get("mat") is None:
                continue
            if ed.get("campo") not in ("devolucion", "entregado", "solicitado",
                                       "devolucion_manuscrita"):
                continue
            mi = int(ed["mat"])
            if mi >= len(mats):
                continue
            m = mats[mi]
            correcciones.append({
                # una fila fusionada desde una página de continuación se leyó en
                # OTRA foto: la lección debe mirar esa imagen, no la de la cabecera
                "foto_n": m.get("de_foto") or h["foto_n"],
                "material": f"[{m.get('codigo_dom')}/{m.get('ref_interna')}] {m.get('descripcion')}",
                "campo": ed["campo"],
                "leyo_la_maquina": ed.get("antes"),
                "corrigio_el_humano": ed.get("valor"),
                "trazo_transcrito": m.get("devolucion_texto") or "",
                "nota_ocr": m.get("nota") or "",
            })
    if not correcciones:
        return jsonify({"error": "Esta sesión no tiene correcciones humanas de las que aprender"}), 400

    contenido = []
    for n in sorted({c["foto_n"] for c in correcciones})[:4]:
        ruta = os.path.join(_dir_sesion(sid), "fotos", f"{n}.jpg")
        if os.path.exists(ruta):
            with open(ruta, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode()
            contenido.append({"type": "text", "text": f"FOTO #{n}:"})
            contenido.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": b64}})
    previas = _cargar_lecciones()
    contenido.append({"type": "text", "text": PROMPT_APRENDER.format(
        correcciones=json.dumps(correcciones, ensure_ascii=False, indent=1),
        previas="\n".join("- " + l["texto"] for l in previas) or "(ninguna)")})

    client = _anthropic_client()
    try:
        with client.messages.stream(
                model=MODELO_OCR, max_tokens=8000,
                messages=[{"role": "user", "content": contenido}],
                output_config={"format": {"type": "json_schema",
                                          "schema": SCHEMA_LECCIONES}}) as st:
            resp = st.get_final_message()
    except anthropic.AuthenticationError:
        return jsonify({"error": "ANTHROPIC_API_KEY inválida"}), 500
    if resp.stop_reason == "refusal":
        return jsonify({"error": "El modelo declinó el análisis"}), 500
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    nuevas = [t.strip() for t in data.get("lecciones", []) if t.strip()]
    ahora = datetime.now().isoformat(timespec="seconds")
    agregadas = []
    for t in nuevas:
        if not any(_norm(t) == _norm(p["texto"]) for p in previas):
            previas.append({"texto": t, "fecha": ahora, "sesion": sid})
            agregadas.append(t)
    _guardar_lecciones(previas)
    log.info("Aprendizaje de %s: %d lecciones nuevas (total %d)",
             sid, len(agregadas), len(previas))
    return jsonify({"ok": True, "nuevas": agregadas, "total": len(previas),
                    "correcciones_analizadas": len(correcciones)})


@app.get("/api/dev/lecciones")
def api_lecciones():
    return jsonify({"lecciones": _cargar_lecciones()})


def _job_verificar(sid):
    """Lee de SGS cada bandeja de devolución creada y la compara con el plan,
    con progreso visible (verif_job) para el popup de revisión."""
    def _set(**kw):
        with _LOCK:
            d = _cargar(sid)
            d.setdefault("verif_job", {}).update(kw)
            _guardar(sid, d)
    try:
        with _LOCK:
            d = _cargar(sid)
            objetivo = [(i, h) for i, h in enumerate(d.get("hojas", []))
                        if h.get("devolucion_ejecutada")]
        if not objetivo:
            _set(estado="fin", hechas=0, total=0, pct=100,
                 msg="No hay bandejas de devolución creadas todavía")
            return
        _set(estado="corriendo", total=len(objetivo), hechas=0, pct=0,
             msg="Leyendo bandejas de SGS...")
        ok_n = 0
        for n, (i, h) in enumerate(objetivo, start=1):
            plan = h.get("preanalisis") or {}
            ban = h["devolucion_ejecutada"].get("ban_dev")
            try:
                with _SGS_SEM:
                    ok, det = _comparar_bandeja_con_plan(ban, plan)
            except Exception as e:
                ok, det = False, {"ban": ban, "ok": False, "estado": None, "materiales": [],
                                  "checks": [f"✗ Error leyendo SGS: {e}"]}
            if ok:
                ok_n += 1
            with _LOCK:
                d = _cargar(sid)
                d["hojas"][i]["bandeja_creada"] = det
                de = d["hojas"][i]["devolucion_ejecutada"]
                if det.get("tray_id"):
                    de["tray_id"] = det["tray_id"]
                    de["estado"] = det.get("estado")
                    if det.get("estado") == "approved":
                        de["aprobada"] = True
                _guardar(sid, d)
            _set(hechas=n, pct=round(100 * n / len(objetivo)),
                 msg=f"{n}/{len(objetivo)} bandejas leídas de SGS...")
        _set(estado="fin",
             msg=f"{len(objetivo)} bandejas leídas — {ok_n} cuadran con el plan")
    except Exception as e:
        log.exception("Job verificar de %s falló", sid)
        _set(estado="error", msg=f"✗ {e}")
    finally:
        _JOBS.pop(sid + "_ver", None)


@app.post("/api/dev/sesion/<sid>/verificar-devoluciones")
def api_verificar_devoluciones(sid):
    if (_r := _solo_admin()):
        return _r
    """Lanza (en fondo) la lectura+comparación de las bandejas creadas."""
    if (sid + "_ver") in _JOBS:
        return jsonify({"ok": True, "ya_corriendo": True})
    t = threading.Thread(target=_job_verificar, args=(sid,), daemon=True)
    _JOBS[sid + "_ver"] = t
    t.start()
    return jsonify({"ok": True})


def _job_aprobar(sid):
    def _set(**kw):
        with _LOCK:
            d = _cargar(sid)
            d.setdefault("aprob_job", {}).update(kw)
            _guardar(sid, d)
    try:
        from browser_client import PlaywrightClient
        with _LOCK:
            d = _cargar(sid)
            objetivo = []
            for i, h in enumerate(d.get("hojas", [])):
                de = h.get("devolucion_ejecutada") or {}
                bc = h.get("bandeja_creada") or {}
                if (de.get("ban_dev") and de.get("tray_id") and bc.get("ok")
                        and not de.get("aprobada")
                        and str(de.get("estado") or "").lower() in ("to_approve", "para aprobar", "")):
                    objetivo.append((i, de["tray_id"], de["ban_dev"]))
        if not objetivo:
            _set(estado="fin", msg="Nada que aprobar: ninguna bandeja verificada pendiente")
            return
        _set(estado="corriendo", total=len(objetivo), hechas=0,
             msg=f"✓ Aprobando {len(objetivo)} bandejas verificadas...")
        pw = PlaywrightClient(headless=True, slow_mo=100, **_cred_sid(sid))
        pw.start()
        pw.login()
        resultados = []
        try:
            for n, (i, tid, ban) in enumerate(objetivo, start=1):
                r = {"ban": ban, "status": "", "estado": None}
                try:
                    out = pw.aprobar_bandeja(tid, ban)
                    r["status"] = out["status"]
                    r["estado"] = out.get("estado")
                    if "APROBAD" in str(out.get("estado") or "").upper():
                        with _LOCK:
                            d = _cargar(sid)
                            d["hojas"][i]["devolucion_ejecutada"]["aprobada"] = True
                            d["hojas"][i]["devolucion_ejecutada"]["estado"] = "approved"
                            _guardar(sid, d)
                except Exception as e:
                    r["status"] = "error"
                    r["estado"] = str(e)[:120]
                resultados.append(r)
                _set(hechas=n, pct=round(100 * n / len(objetivo)),
                     msg=f"✓ {n}/{len(objetivo)} aprobadas...", resultados=resultados)
        finally:
            pw.close()
        okn = sum(1 for r in resultados if r["status"] == "ok")
        _set(estado="fin", msg=f"✓ Aprobación completa: {okn}/{len(objetivo)} aprobadas",
             resultados=resultados)
    except Exception as e:
        log.exception("Job aprobar de %s falló", sid)
        _set(estado="error", msg=f"✗ {e}")
    finally:
        _JOBS.pop(sid + "_apr", None)


@app.post("/api/dev/sesion/<sid>/aprobar-bandejas")
def api_aprobar_bandejas(sid):
    if (_r := _solo_admin() or _zona_bloqueada(sid)):
        return _r
    """Aprueba en SGS las bandejas creadas cuya verificación cuadró con el plan.
    Requiere haber corrido antes verificar-devoluciones (o que el job las verificó)."""
    if (sid + "_apr") in _JOBS:
        return jsonify({"error": "Ya hay una aprobación en curso"}), 409
    t = threading.Thread(target=_job_aprobar, args=(sid,), daemon=True)
    _JOBS[sid + "_apr"] = t
    t.start()
    return jsonify({"ok": True})


@app.post("/api/dev/diagnostico")
def api_dev_diagnostico():
    """SGO7: guarda en el servidor el informe técnico (antes se pedía pegarlo en un chat)."""
    d = request.get_json(force=True, silent=True) or {}
    tipo = re.sub(r"[^a-z0-9_-]", "", (d.get("tipo") or "informe").lower())[:30] or "informe"
    sid = re.sub(r"[^A-Za-z0-9_-]", "", d.get("sid") or "")[:40]
    texto = (d.get("texto") or "")[:400_000]
    if not texto.strip():
        return jsonify({"error": "informe vacío"}), 400
    base = os.path.join("/var/www/sgo7/registros/diagnosticos/devoluciones", datetime.now().strftime("%Y-%m"))
    os.makedirs(base, exist_ok=True)
    ruta = os.path.join(base, f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{tipo}_{sid or 'sin-sesion'}.md")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto)
    os.chmod(ruta, 0o640)
    try:
        import sgo7_registro
        sgo7_registro.registrar(f"devoluciones_diagnostico_{tipo}", sesion=sid or None, archivo=os.path.basename(ruta),
                                detalle={"ruta": ruta, "caracteres": len(texto), "resumen": texto[:1500]})
    except Exception:
        pass
    return jsonify({"ok": True, "ruta": ruta})


@app.get("/api/dev/sesion/<sid>/debug")
def api_debug(sid):
    if (_r := _solo_admin()):
        return _r
    """Informe técnico: estado completo de la
    sesión (sin imágenes), errores de todos los jobs y logs del servidor."""
    with _LOCK:
        d = _cargar(sid)
    errores = []
    for i, h in enumerate(d.get("hojas", [])):
        if h.get("error"):
            errores.append(f"hoja {i} (foto #{h.get('foto_n')}): OCR error: {h['error']}")
        v = h.get("verificacion") or {}
        for m in v.get("mensajes", []):
            errores.append(f"hoja {i} verificación: {m}")
        p = h.get("preanalisis") or {}
        for m in p.get("mensajes", []):
            errores.append(f"hoja {i} preanálisis: {m}")
        bc = h.get("bandeja_creada") or {}
        for chk in bc.get("checks", []):
            if not chk.startswith("✓"):
                errores.append(f"hoja {i} bandeja {bc.get('ban')}: {chk}")
    for r in (d.get("dev_job") or {}).get("resultados", []):
        if r.get("error"):
            errores.append(f"devolución {r.get('ban_origen')}: {r['error']}")
        for a in r.get("advertencias", []):
            errores.append(f"devolución {r.get('ban_origen')}: {a}")
    for r in (d.get("aprob_job") or {}).get("resultados", []):
        if r.get("status") != "ok":
            errores.append(f"aprobar {r.get('ban')}: {r.get('estado')}")
    for r in (d.get("adic_job") or {}).get("resultados", []):
        if r.get("error"):
            errores.append(f"adicional {r.get('ban_origen')}: {r['error']}")
        for a in r.get("advertencias", []):
            errores.append(f"adicional {r.get('ban_origen')}: {a}")
    return jsonify({
        "sesion": sid,
        "generado": datetime.now().isoformat(timespec="seconds"),
        "estado_sesion": d.get("estado"),
        "dev_job": d.get("dev_job"),
        "aprob_job": d.get("aprob_job"),
        "errores_y_advertencias": errores,
        "n_hojas": len(d.get("hojas", [])),
        "ediciones_humanas": sum(len(h.get("ediciones", [])) for h in d.get("hojas", [])),
        "lecciones_aprendidas": len(_cargar_lecciones()),
        "logs_servidor": list(_LOG_BUFFER)[-250:],
    })


@app.post("/api/dev/sesion/<sid>/adicionales")
def api_adicionales(sid):
    if (_r := _solo_admin() or _zona_bloqueada(sid)):
        return _r
    """Lanza el job de despachos adicionales (TEST o REAL).
    body.hojas = [índices] → solo esas hojas (botón por hoja del panel +)."""
    body = request.json or {}
    dry_run = bool(body.get("dry_run", True))
    solo = body.get("hojas")
    if solo is not None:
        try:
            solo = sorted({int(x) for x in solo})
        except (TypeError, ValueError):
            return jsonify({"error": "hojas debe ser una lista de números"}), 400
    with _LOCK:
        d = _cargar(sid)
        con_plan = [i for i, h in enumerate(d.get("hojas", []))
                    if (h.get("preanalisis") or {}).get("adicionales")
                    and (solo is None or i in solo)]
    if not con_plan:
        return jsonify({"error": "Ninguna hoja tiene adicionales — abre + Adicionales"}), 400
    if (sid + "_adic") in _JOBS:
        return jsonify({"error": "Ya hay una ejecución de adicionales en curso"}), 409
    if (sid + "_dev") in _JOBS:
        # máximo 2 navegadores contra SGS: las devoluciones ya usan los dos
        return jsonify({"error": "Hay devoluciones creándose en esta sesión — espera a que terminen"}), 409
    t = threading.Thread(target=_job_adicionales, args=(sid, dry_run, solo), daemon=True)
    _JOBS[sid + "_adic"] = t
    t.start()
    return jsonify({"ok": True, "dry_run": dry_run, "hojas": len(con_plan)})


@app.post("/api/dev/sesion/<sid>/hoja/<int:idx>/plan")
def api_plan_hoja(sid, idx):
    """Cruce SOLO LECTURA de UNA hoja contra SGS (devolución + adicionales).
    El panel + lo llama hoja por hoja con progreso: así no hace falta pulsar
    Preanálisis y ninguna petición se acerca al timeout del proxy."""
    if (_r := _solo_admin()):
        return _r
    with _LOCK:
        d0 = _cargar(sid)
        hojas = d0.get("hojas", [])
        if not (0 <= idx < len(hojas)) or not hojas[idx].get("ocr"):
            return jsonify({"error": "Hoja no existe o sin OCR"}), 404
        h, todos = hojas[idx], bool(d0.get("adic_todos"))
    with _SGS_SEM:
        plan = preanalisis_hoja(h, adic_todos=todos, zona=_zona_sid(sid))
    if plan.get("adicionales"):
        plan["adicionales"]["otra_sesion"] = _devolucion_en_otra_sesion(
            sid, (h.get("ocr") or {}).get("ban_code"), marca="adicional_ejecutado")
    with _LOCK:
        d = _cargar(sid)
        d["hojas"][idx]["preanalisis"] = plan
        _guardar(sid, d)
    return jsonify({"ok": True, "adicionales": bool(plan.get("adicionales"))})


@app.post("/api/dev/sesion/<sid>/adic-todos")
def api_adic_todos(sid):
    """Cambia el modo de adicionales de la sesión: todos los supervisores
    (perfil con adicionales de todos) o solo la lista autorizada. El panel re-cruza después."""
    if (_r := _solo_admin()):
        return _r
    valor = bool((request.json or {}).get("valor"))
    with _LOCK:
        d = _cargar(sid)
        d["adic_todos"] = valor
        _guardar(sid, d)
    log.info("Sesión %s: adicionales de %s (por %s)", sid,
             "TODOS los supervisores" if valor else "supervisores autorizados",
             (_usuario() or {}).get("nombre"))
    return jsonify({"ok": True, "adic_todos": valor})


@app.post("/api/dev/sesion/<sid>/devoluciones")
def api_devoluciones(sid):
    if (_r := _solo_admin() or _zona_bloqueada(sid)):
        return _r
    """Lanza el job de creación de bandejas de devolución (TEST o REAL)."""
    body = request.json or {}
    dry_run = bool(body.get("dry_run", True))
    with _LOCK:
        d = _cargar(sid)
        con_plan = [h for h in d.get("hojas", []) if h.get("preanalisis")]
        if not con_plan:
            return jsonify({"error": "Primero corre Preanálisis SGS"}), 400
        if (sid + "_dev") in _JOBS:
            return jsonify({"error": "Ya hay una ejecución en curso"}), 409
        if (sid + "_adic") in _JOBS:
            return jsonify({"error": "Hay adicionales creándose en esta sesión — espera a que terminen"}), 409
        # (si dev_job quedó "corriendo" por un reinicio pero no hay hilo vivo,
        #  se permite relanzar: la idempotencia evita duplicados)
    t = threading.Thread(target=_job_devoluciones, args=(sid, dry_run), daemon=True)
    _JOBS[sid + "_dev"] = t
    t.start()
    return jsonify({"ok": True, "dry_run": dry_run})


@app.post("/api/dev/sesion/<sid>/cerrar")
def api_cerrar(sid):
    with _LOCK:
        d = _cargar(sid)
        d["estado"] = "cerrada"
        _guardar(sid, d)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════
#  Módulo Almacén — flujo Materiales Sony (cargue de instalaciones SIPWEBORA)
#  dentro de la misma app: PRO → nodos → bandejas → cuadre → Excel. El cuadre
#  trae la devolución de la HOJA fotografiada/validada (hojas_por_ban) o de la
#  bandeja de devolución real de SGS, y se puede escribir a mano. SOLO LECTURA
#  de SGS (almacen_sgs_client no tiene write/create).
# ══════════════════════════════════════════════════════════════════════════

_sony_cli = None
_sony_cli_ts = 0


def _sony_cliente(fresco=False):
    """Cliente SGS (JSON-RPC solo lectura) para el flujo del cargue; re-login
    cada 15 min o si se pide fresco."""
    global _sony_cli, _sony_cli_ts
    from almacen_sgs_client import SGSClient
    if _sony_cli is None or fresco or time.time() - _sony_cli_ts > _SGS_TTL:
        c = SGSClient(cfg.SGS_URL, cfg.SGS_DB, cfg.SGS_LOGIN, cfg.SGS_PASSWORD)
        c.autenticar()
        _sony_cli, _sony_cli_ts = c, time.time()
    return _sony_cli


_ARCHIVOS_ALMACEN = os.path.join(_BASE, "almacen_data", "archivos")


def _guardar_archivo_almacen(token, data, filename, advertencias=None):
    os.makedirs(_ARCHIVOS_ALMACEN, exist_ok=True)
    with open(os.path.join(_ARCHIVOS_ALMACEN, f"{token}.xlsx"), "wb") as f:
        f.write(data)
    with open(os.path.join(_ARCHIVOS_ALMACEN, f"{token}.json"), "w", encoding="utf-8") as f:
        json.dump({"filename": filename, "advertencias": advertencias or []}, f)


def _leer_archivo_almacen(token):
    xls = os.path.join(_ARCHIVOS_ALMACEN, f"{token}.xlsx")
    meta = os.path.join(_ARCHIVOS_ALMACEN, f"{token}.json")
    if not (re.match(r"^[0-9a-f]{32}$", token or "") and os.path.isfile(xls) and os.path.isfile(meta)):
        return None
    with open(meta, encoding="utf-8") as f:
        m = json.load(f)
    with open(xls, "rb") as f:
        return {"bytes": f.read(), "filename": m["filename"]}


def _render_almacen(error=None, ok=None, **kw):
    from almacen_sony import anexo_status
    try:
        anexo = anexo_status()
    except Exception:
        anexo = {"cargado": False}
    u = _usuario() or {}
    mias = [x for x in listar_sesiones()
            if (x.get("creada_por") == u.get("nombre") or u.get("rol") == "admin")
            and _ve_zona(u, x.get("zona"))][:8]
    return render_template("almacen_index.html", anexo_info=anexo, error=error, ok=ok,
                           usuario=u.get("nombre"), es_admin=u.get("rol") == "admin",
                           sesiones=mias, **kw)


@app.get("/almacen")
def almacen_index():
    return _render_almacen(ok=request.args.get("ok"))


@app.post("/almacen/anexo")
def almacen_anexo():
    from almacen_sony import guardar_anexo
    archivo = request.files.get("anexo_file")
    if not archivo or not archivo.filename:
        return _render_almacen(error="Selecciona el archivo ANEXO Nº 14 (.xlsx).")
    try:
        st = guardar_anexo(archivo.read(), archivo.filename)
    except ValueError as e:
        return _render_almacen(error=str(e))
    except Exception as e:
        return _render_almacen(error=f"No se pudo leer el ANEXO: {e}")
    return _render_almacen(ok=f"ANEXO cargado: {st['total']} materiales de aportación.")


@app.post("/almacen/anexo/eliminar")
def almacen_anexo_eliminar():
    from almacen_sony import eliminar_anexo
    eliminar_anexo()
    return _render_almacen(ok="ANEXO Nº 14 eliminado.")


@app.post("/almacen/buscar")
def almacen_buscar():
    """Paso 1 — cualquier PRO (hijo o padre) → padre y sus nodos con bandejas."""
    from almacen_sony import nueva_sesion_mat
    pro_raw = (request.form.get("pro_materiales") or "").strip()
    if not pro_raw:
        return _render_almacen(error="Ingresa un código PRO (hijo o padre).")
    try:
        try:
            cliente = _sony_cliente()
            proyecto = (cliente.leer_proyecto(int(pro_raw)) if pro_raw.isdigit()
                        else cliente.buscar_por_codigo(pro_raw))
        except RuntimeError:
            cliente = _sony_cliente(fresco=True)       # sesión SGS caducada → re-login
            proyecto = (cliente.leer_proyecto(int(pro_raw)) if pro_raw.isdigit()
                        else cliente.buscar_por_codigo(pro_raw))
        if not proyecto:
            return _render_almacen(error=f"'{pro_raw}' no encontrado en SGS.", pro_materiales=pro_raw)
        parent_field = proyecto.get("parent_id")
        if isinstance(parent_field, list) and parent_field:
            parent_id, pro_origen = parent_field[0], proyecto.get("code", pro_raw)
        else:
            parent_id, pro_origen = proyecto["id"], None
        data = cliente.balance_completo(parent_id)
        sid = nueva_sesion_mat({"padre": data["padre"], "parent_id": parent_id,
                                "nodos": data["nodos"], "pro_origen": pro_origen,
                                "usuario": (_usuario() or {}).get("nombre")})
        return redirect(f"/almacen/nodos/{sid}")
    except Exception as e:
        log.exception("almacen/buscar %s", pro_raw)
        return _render_almacen(error=f"Error consultando SGS: {e}", pro_materiales=pro_raw)


@app.route("/almacen/nodos/<sid>", methods=["GET", "POST"])
def almacen_nodos(sid):
    from almacen_sony import get_sesion_mat, save_sesion_mat
    sesion = get_sesion_mat(sid)
    if not sesion:
        return _render_almacen(error="La sesión expiró — busca el PRO nuevamente."), 404
    if request.method == "POST":
        sel = request.form.getlist("nodo_codes")
        if sel:
            sesion["nodos_sel"] = sel
            save_sesion_mat(sid, sesion)
            return redirect(f"/almacen/bandejas/{sid}")
    nodos_resumen = []
    for n in sesion["nodos"]:
        bans = n.get("bandejas", [])
        nodos_resumen.append({
            "pro": n.get("code", ""), "nodo": n.get("nodo") or "—", "name": n.get("name", ""),
            "status": (n.get("status")[1] if isinstance(n.get("status"), list) else str(n.get("status") or "—")),
            "despachos": sum(1 for b in bans if b.get("type", "deliver") != "devolution"),
            "devoluciones": sum(1 for b in bans if b.get("type", "deliver") == "devolution"),
            "es_origen": n.get("code") == sesion.get("pro_origen"),
        })
    error = "Selecciona al menos un nodo." if request.method == "POST" else None
    return render_template("materiales_nodos.html", sid=sid, padre=sesion["padre"],
                           nodos=nodos_resumen, pro_origen=sesion.get("pro_origen"), error=error)


@app.route("/almacen/bandejas/<sid>", methods=["GET", "POST"])
def almacen_bandejas(sid):
    from almacen_sony import get_sesion_mat, save_sesion_mat, presentar_bandeja, construir_cuadre
    sesion = get_sesion_mat(sid)
    if not sesion:
        return _render_almacen(error="La sesión expiró — busca el PRO nuevamente."), 404
    nodos_sel = sesion.get("nodos_sel", [])
    nodos = [n for n in sesion["nodos"] if n.get("code") in nodos_sel]
    if request.method == "POST":
        ban_ids = {int(x) for x in request.form.getlist("ban_ids") if x.isdigit()}
        if ban_ids:
            sesion["ban_ids_sel"] = sorted(ban_ids)
            sesion["cuadre"] = construir_cuadre(nodos, ban_ids)
            save_sesion_mat(sid, sesion)
            return redirect(f"/almacen/cuadre/{sid}")
    grupos = []
    for n in nodos:
        bans = sorted(n.get("bandejas", []), key=lambda b: str(b.get("date") or ""))
        grupos.append({"pro": n.get("code", ""), "nodo": n.get("nodo") or "—",
                       "name": n.get("name", ""),
                       "bandejas": [presentar_bandeja(b, n) for b in bans]})
    error = "Selecciona al menos una bandeja de despacho." if request.method == "POST" else None
    return render_template("materiales_bandejas.html", sid=sid, padre=sesion["padre"],
                           grupos=grupos, error=error)


def _nodo_num_desde(desc):
    m = re.search(r"NODO\s*(\d+)", desc or "", re.I)
    return m.group(1) if m else "—"


def _fecha_iso(s):
    """'15/09/2026' o '2026-09-15…' → 'YYYY-MM-DD' ('' si no se puede)."""
    s = str(s or "").strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else ""


@app.get("/almacen/cargue-sesion/<sid>")
def almacen_cargue_sesion(sid):
    """Cargue de instalaciones DIRECTO desde las hojas fotografiadas de una sesión
    (validadas o solo leídas). Es la gracia del módulo para almacén: no espera
    las bandejas de devolución formales del administrador ni obliga a navegar
    PRO → nodos → bandejas. Cada hoja = un despacho (BAN) con sus filas; la
    devolución sale de la hoja; Aportación del ANEXO 14; fecha/equipo del tray
    en SGS (solo lectura, una llamada)."""
    from almacen_sony import nueva_sesion_mat, AportacionMatcher, get_prefs
    try:
        with _LOCK:
            d = _cargar(sid)
    except FileNotFoundError:
        return _render_almacen(error=f"La sesión {sid} no existe."), 404
    hojas = [h for h in d.get("hojas", [])
             if h.get("ocr") and not h.get("error") and not h.get("duplicada_de")
             and not h.get("continuacion_de") and _es_ban_valido((h["ocr"] or {}).get("ban_code"))]
    if not hojas:
        return _render_almacen(error=f"La sesión {sid} no tiene hojas leídas con BAN válido todavía.")
    bans = sorted({h["ocr"]["ban_code"].strip().upper() for h in hojas})
    info_tray = {}
    try:
        for t in _SGS.search_read("project.tray", [["name", "in", bans]],
                                  ["id", "name", "date", "partner_id", "picking_id"], limit=len(bans) + 5):
            info_tray[str(t.get("name") or "").upper()] = t
    except Exception as e:
        log.warning("cargue-sesion %s: sin datos de tray en SGS (%s)", sid, e)
    matcher = AportacionMatcher()
    prefs = get_prefs()
    por_pro = prefs.get("por_pro") or {}
    nodos, advert, idx = {}, [], 0
    if not matcher.cargado:
        advert.append("No hay ANEXO Nº 14 cargado — la columna Aportación sale en 0 salvo que la marques a mano.")
    for h in hojas:
        o, v = h["ocr"], (h.get("verificacion") or {})
        ban = o["ban_code"].strip().upper()
        pro = (o.get("pro_code") or "").strip().upper() or "SIN-PRO"
        t = info_tray.get(ban, {})
        rec = por_pro.get(pro) or {}
        nc = nodos.setdefault(pro, {
            "pro": pro, "nodo_num": _nodo_num_desde(o.get("proyecto_desc")),
            "name": v.get("proyecto_sgs") or o.get("proyecto_desc") or "",
            "id_nodo_sug": rec.get("id_nodo", ""), "pdi_sug": rec.get("pdi", ""),
            "despachos": [], "devoluciones_sel": []})
        filas = []
        for m in o.get("materiales", []):
            cod = (m.get("ref_interna") or "").strip()
            dom, desc = (m.get("codigo_dom") or ""), (m.get("descripcion") or "")
            k, via = matcher.es_aportacion(cod, desc)
            ent = m.get("entregado")
            filas.append({"idx": idx, "cod": cod, "dom": dom, "desc": desc,
                          "qty": float(ent) if isinstance(ent, (int, float)) else 0.0,
                          "qty_sol": float(m.get("solicitado") or 0), "dev_auto": 0.0,
                          "k": k, "k_via": via, "es_cliente": False})
            if not cod:
                advert.append(f"{ban}: material sin referencia ({desc[:40]}) — revisa la columna Codigo.")
            idx += 1
        if not h.get("validada"):
            advert.append(f"{ban} (foto #{h.get('foto_n')}): hoja leída pero SIN validar — revísala antes de cargar.")
        nc["despachos"].append({
            "ban_id": t.get("id") or f"f{h.get('foto_n')}", "ban_name": ban,
            "fecha": _fecha_iso(t.get("date")) or _fecha_iso(o.get("fecha_despachado")),
            "picking": (t.get("picking_id") or [None, "—"])[1] if isinstance(t.get("picking_id"), list) else "—",
            "equipo": v.get("lider_sgs") or ((o.get("asignada_a") or [""])[0]),
            "filas": filas, "hoja_foto": h.get("foto_n")})
    cuadre = {"nodos_cuadre": list(nodos.values()), "advertencias": advert, "n_filas": idx}
    padre = {"name": f"Hojas de la sesión {sid} ({len(hojas)} bandejas)", "code": "",
             "order_number": f"sesion_{sid}"}
    msid = nueva_sesion_mat({"padre": padre, "parent_id": None, "nodos": [], "pro_origen": None,
                             "cuadre": cuadre, "desde_sesion": sid,
                             "usuario": (_usuario() or {}).get("nombre")})
    log.info("Cargue desde sesión %s por %s: %d bandejas, %d filas", sid,
             (_usuario() or {}).get("nombre"), len(hojas), idx)
    return redirect(f"/almacen/cuadre/{msid}")


@app.get("/almacen/cuadre/<sid>")
def almacen_cuadre(sid):
    """Paso 4 — cuadre editable. La devolución de cada despacho viene de la hoja
    fotografiada/validada (misma app) o de la bandeja real de SGS; se puede
    escribir a mano igual que siempre."""
    from almacen_sony import get_sesion_mat, get_prefs, aplicar_hojas_devoluciones
    sesion = get_sesion_mat(sid)
    if not sesion or not sesion.get("cuadre"):
        return _render_almacen(error="La sesión expiró — busca el PRO nuevamente."), 404
    cuadre = sesion["cuadre"]
    bans = [d["ban_name"] for nc in cuadre.get("nodos_cuadre", []) for d in nc.get("despachos", [])]
    try:
        hojas = hojas_por_ban(bans)
    except Exception:
        log.exception("hojas_por_ban en cuadre")
        hojas = {}
    info = aplicar_hojas_devoluciones(cuadre, hojas)
    por_pro = get_prefs().get("por_pro") or {}
    for nc in cuadre.get("nodos_cuadre", []):
        rec = por_pro.get(nc.get("pro")) or {}
        if not nc.get("id_nodo_sug") and rec.get("id_nodo"):
            nc["id_nodo_sug"] = rec["id_nodo"]
        if not nc.get("pdi_sug") and rec.get("pdi"):
            nc["pdi_sug"] = rec["pdi"]
    return render_template("materiales_cuadre.html", sid=sid, padre=sesion["padre"],
                           cuadre=cuadre, usuario_def=get_prefs().get("usuario_cargue", ""),
                           hoy=datetime.now().strftime("%Y-%m-%d"), info_hojas=info)


@app.post("/almacen/generar/<sid>")
def almacen_generar(sid):
    from almacen_sony import get_sesion_mat, generar_cargue, save_prefs, _parse_fecha, _num
    import uuid
    sesion = get_sesion_mat(sid)
    if not sesion or not sesion.get("cuadre"):
        return _render_almacen(error="La sesión expiró — busca el PRO nuevamente."), 404
    from almacen_sony import get_prefs
    form = request.form
    usuario = (form.get("usuario") or "").strip()
    if usuario:
        save_prefs(usuario_cargue=usuario)
    # Id_nodo y Pdi se recuerdan por PRO: la próxima vez ya vienen puestos
    por_pro = dict(get_prefs().get("por_pro") or {})
    for nc in sesion["cuadre"]["nodos_cuadre"]:
        idn, pdi = (form.get(f"idnodo_{nc['pro']}") or "").strip(), (form.get(f"pdi_{nc['pro']}") or "").strip()
        if idn or pdi:
            por_pro[nc["pro"]] = {"id_nodo": idn or (por_pro.get(nc["pro"]) or {}).get("id_nodo", ""),
                                  "pdi": pdi or (por_pro.get(nc["pro"]) or {}).get("pdi", "")}
    save_prefs(por_pro=por_pro)
    fecha_asig = datetime.now()
    advertencias = list(sesion["cuadre"].get("advertencias", []))
    filas = []
    for nc in sesion["cuadre"]["nodos_cuadre"]:
        pro = nc["pro"]
        id_nodo = (form.get(f"idnodo_{pro}") or "").strip()
        pdi = (form.get(f"pdi_{pro}") or "").strip()
        obs = (form.get(f"obs_{pro}") or "").strip()
        reintegro = _parse_fecha(form.get(f"reintegro_{pro}", ""))
        if not id_nodo:
            advertencias.append(f"{pro}: sin Id_nodo — las filas salen con la celda vacía.")
        for desp in nc["despachos"]:
            f_ent = _parse_fecha(form.get(f"fban_{desp['ban_id']}", "")) or _parse_fecha(desp["fecha"])
            for fila in desp["filas"]:
                i = fila["idx"]
                if not form.get(f"r{i}_incl"):
                    continue
                qty = _num(form.get(f"r{i}_qty", fila["qty"]))
                dev = _num(form.get(f"r{i}_dev", 0))
                if dev > qty:
                    advertencias.append(f"{pro} / {desp['ban_name']} / {fila['cod']}: devolución "
                                        f"({dev:g}) mayor que lo entregado ({qty:g}).")
                filas.append({"id_nodo": id_nodo, "usuario": usuario, "fecha_asignacion": fecha_asig,
                              "codigo": fila["cod"], "qty": qty, "dev": dev, "fecha_entrega": f_ent,
                              "pdi": pdi, "consecutivo": desp["ban_name"], "obs": obs,
                              "k": form.get(f"r{i}_k", str(fila["k"])), "fecha_reintegro": reintegro})
    if not filas:
        return _render_almacen(error="No quedó ninguna fila incluida — revisa el cuadre.")
    excel_bytes = generar_cargue(filas)
    padre = sesion["padre"]
    odm = padre.get("order_number") or padre.get("odm") or padre.get("code") or "proyecto"
    filename = f"cargue_instalaciones_{odm}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    token = uuid.uuid4().hex
    _guardar_archivo_almacen(token, excel_bytes, filename, advertencias)
    log.info("Cargue generado por %s: %s (%d filas)", (_usuario() or {}).get("nombre"), filename, len(filas))
    resumen = {"n_filas": len(filas), "n_nodos": len(sesion["cuadre"]["nodos_cuadre"]),
               "total_ent": sum(float(f["qty"] or 0) for f in filas),
               "total_dev": sum(float(f["dev"] or 0) for f in filas),
               "n_aportacion": sum(1 for f in filas if str(f["k"]) == "1")}
    return render_template("materiales_resultado.html", sid=sid, padre=padre, token=token,
                           filename=filename, filas=filas, resumen=resumen, advertencias=advertencias)


@app.get("/almacen/descargar/<token>")
def almacen_descargar(token):
    import io as _io
    a = _leer_archivo_almacen(token)
    if not a:
        return _render_almacen(error="El archivo expiró o no está disponible. Genera el cargue nuevamente."), 404
    return send_file(_io.BytesIO(a["bytes"]), as_attachment=True, download_name=a["filename"],
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/static-dev/<path:fname>")
def static_dev(fname):
    ruta = os.path.join(DIR_STATIC, fname)
    if not os.path.exists(ruta):
        return "no existe", 404
    return send_file(ruta, max_age=86400 * 7)


# ══════════════════════════════════════════════════════════════════════════
#  Página MÓVIL — captura estilo CamScanner
# ══════════════════════════════════════════════════════════════════════════

HTML_MOVIL = r"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="robots" content="noindex, nofollow"><title>Devoluciones</title>
<style>
:root{--verde:#22c55e;--amarillo:#eab308;--rojo:#ef4444;--fondo:#0b1220;--panel:#131c2e;--azul:#0ea5e9}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{background:var(--fondo);color:#e2e8f0;font-family:system-ui,sans-serif;height:100dvh;
     display:flex;flex-direction:column;overflow:hidden}
header{padding:10px 14px;display:flex;justify-content:space-between;align-items:center;
       background:var(--panel);flex:0 0 auto}
header b{color:#7dd3fc}
header a{color:#64748b;text-decoration:none;font-size:16px;padding:4px}
#pill{padding:3px 12px;border-radius:99px;background:#334155;font-size:12px;white-space:nowrap}
#pill.ok{background:#052e16;color:#4ade80}

/* pantallas centradas (inicio / modo nativo) */
.pantalla{flex:1 1 auto;display:flex;flex-direction:column;justify-content:center;
  align-items:center;padding:20px;text-align:center;overflow-y:auto}
.pantalla .logo{font-size:52px;margin-bottom:6px}
.pantalla h1{font-size:21px;margin-bottom:4px}
.pantalla .sub{color:#94a3b8;font-size:13px;margin-bottom:18px}
.pantalla .hint{color:#64748b;font-size:13px;margin-top:16px;line-height:1.5}
button.grande{display:block;width:100%;max-width:340px;margin:8px auto;padding:18px;
  border:none;border-radius:16px;background:#1e293b;color:#e2e8f0;font-size:17px;font-weight:600}
button.grande.primario{background:var(--azul);color:#fff;font-size:19px;padding:22px}
button.grande small{display:block;font-weight:400;font-size:13px;opacity:.85;margin-top:3px}
button.grande:active{filter:brightness(1.25)}

/* visor cámara en vivo */
#visor{position:relative;flex:1 1 auto;background:#000;display:none;align-items:center;
       justify-content:center;overflow:hidden}
#caja-video{position:relative;margin:auto;max-width:100%;max-height:100%}
video{display:block;width:100%;height:100%}
#overlay{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
#msj-detect{position:absolute;top:10px;left:50%;transform:translateX(-50%);
  padding:5px 16px;border-radius:99px;font-size:14px;font-weight:600;background:#000a;
  color:#cbd5e1;white-space:nowrap}

/* pie común: miniaturas + controles */
footer{flex:0 0 auto;background:var(--panel);padding:6px 12px calc(10px + env(safe-area-inset-bottom))}
#tira{display:flex;gap:8px;overflow-x:auto;padding:6px 0 4px;min-height:0}
#tira:not(:empty){min-height:72px}
.mini{position:relative;flex:0 0 auto}
.mini img{height:60px;border-radius:8px;border:2px solid #334155}
.mini.verde img{border-color:#4ade80}.mini.amarillo img{border-color:#facc15}
.mini.rojo img{border-color:#f87171}.mini.azul img{border-color:#7dd3fc}
.mini.leyendo img{border-color:#7dd3fc;animation:lat 1.2s infinite}
@keyframes lat{50%{border-color:#1e3a5f}}
.mini button{position:absolute;top:-7px;right:-7px;width:22px;height:22px;border-radius:50%;
  border:none;background:var(--rojo);color:#fff;font-size:12px;line-height:1}
#zoom-caja{display:none;align-items:center;gap:8px;font-size:13px;margin:2px 0}
#zoom-caja input{flex:1}
#controles-vivo{display:none;align-items:center;justify-content:space-around;margin-top:4px}
.btn-red{width:50px;height:50px;border-radius:50%;border:none;background:#1e293b;color:#e2e8f0;
         font-size:22px}
.btn-red.on{background:#eab308;color:#000}
#disparador{width:78px;height:78px;border-radius:50%;border:5px solid #e2e8f0;background:#fff2;
            font-size:32px;color:#fff}
#disparador:active{background:#fff6}
#lnk-nativa{text-align:center;margin-top:6px}
#lnk-nativa a{color:#64748b;font-size:12px;text-decoration:underline}

#toast{position:fixed;left:50%;bottom:130px;transform:translateX(-50%) translateY(16px);
  background:#0b1220f0;border:1px solid #334155;color:#e2e8f0;padding:11px 18px;
  border-radius:12px;font-size:14px;opacity:0;transition:.25s;pointer-events:none;z-index:60;
  max-width:92vw;text-align:center}
#toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
#flash-captura{position:fixed;inset:0;background:#fff;opacity:0;pointer-events:none;
  transition:opacity .18s;z-index:50}
</style></head><body>

<header>
  <div><b>Devoluciones</b> <small style="opacity:.45">v3</small></div>
  <div style="display:flex;gap:10px;align-items:center">
    <span id="pill">conectando…</span>
    <a onclick="nuevaSesionMovil()" title="Empezar sesión nueva" style="font-size:19px">+</a>
    <a href="/?pc=1" title="Vista de PC">PC</a>
  </div>
</header>

<!-- Pantalla de inicio -->
<div id="pantalla-inicio" class="pantalla">
  <div class="logo">◉</div>
  <h1>Escáner de devoluciones</h1>
  <p class="sub" id="ses-info">conectando con la sesión…</p>
  <button class="grande primario" onclick="elegirVivo()">▶ Iniciar cámara
    <small>detecta y recorta la hoja solo, como CamScanner</small></button>
  <button class="grande" onclick="elegirNativa()">Usar la cámara del teléfono
    <small>si la cámara en vivo no funciona en tu navegador</small></button>
  <button class="grande" style="padding:12px;font-size:15px;opacity:.9"
    onclick="nuevaSesionMovil()">+ Empezar sesión nueva
    <small>para arrancar otro lote de hojas</small></button>
  <p class="hint">Toma aquí las fotos de las hojas.<br>
    Aparecen al instante en el PC, donde se procesan y corrigen.</p>
</div>

<!-- Cámara en vivo -->
<div id="visor">
  <div id="caja-video">
    <video id="video" autoplay playsinline muted></video>
    <canvas id="overlay"></canvas>
    <div id="msj-detect">Iniciando cámara…</div>
  </div>
</div>

<!-- Modo cámara nativa -->
<div id="pantalla-nativa" class="pantalla" style="display:none">
  <div class="logo">◉</div>
  <h1>Cámara del teléfono</h1>
  <p class="sub" id="sub-nativa">Cada foto se recorta y se sube sola.</p>
  <button class="grande primario" onclick="document.getElementById('inp-file').click()">
    TOMAR FOTO</button>
  <button class="grande" id="btn-recorte" onclick="toggleRecorte()"
    style="padding:12px;font-size:15px"></button>
  <p class="hint" id="estado-cv">Cargando el detector de hoja…</p>
</div>
<input id="inp-file" type="file" accept="image/*" capture="environment" hidden>

<footer>
  <div id="tira"></div>
  <div id="zoom-caja"><input id="zoom" type="range"></div>
  <div id="controles-vivo">
    <button id="btn-flash" class="btn-red" style="visibility:hidden"></button>
    <button id="disparador" title="Tomar foto">●</button>
    <button id="btn-auto" class="btn-red on" title="Captura automática al detectar la hoja">AUTO</button>
  </div>
  <div id="lnk-nativa" style="display:none"><a onclick="aNativa()">¿Problemas? usar la cámara del teléfono</a></div>
</footer>

<div id="toast"></div>
<div id="flash-captura"></div>

<script src="/static-dev/jscanify.js"></script>
<script>
"use strict";
let SID=null, cvListo=false, scanner=null, track=null, imageCapture=null;
let ultEsq=null, estables=0, ultimaCaptura=0, autoCaptura=true, detectando=false, subiendo=0, nFotos=0;
const $=id=>document.getElementById(id);
const video=$('video'), overlay=$('overlay'), msj=$('msj-detect');

function toast(t,ms){const e=$('toast');e.textContent=t;e.classList.add('on');
  clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove('on'),ms||1800);}

/* ── Recorte automático ON/OFF (persistente) ──
   El warp de perspectiva a veces se engancha con un falso cuadrilátero (la tabla
   impresa, una sombra) y tuerce/corta la hoja. Este interruptor permite subir la
   foto COMPLETA tal como sale de la cámara. */
let recorteAuto = localStorage.getItem('dev_recorte') !== '0';
function pintarRecorte(){
  const b=$('btn-recorte');
  b.innerHTML = recorteAuto
    ? ' Recorte automático: SÍ<small>si tuerce o corta la hoja, tócalo para apagarlo</small>'
    : '⬜ Recorte automático: NO<small>las fotos suben completas, tal como las tomas</small>';
  b.style.background = recorteAuto ? '#1e293b' : '#7c2d12';
  $('sub-nativa').textContent = recorteAuto
    ? 'Cada foto se recorta y se sube sola.'
    : 'Cada foto se sube COMPLETA, sin recorte.';
}
function toggleRecorte(){
  recorteAuto=!recorteAuto;
  localStorage.setItem('dev_recorte', recorteAuto?'1':'0');
  pintarRecorte();
  toast(recorteAuto?' Recorte automático activado':'⬜ Recorte apagado: la foto sube completa',2500);
}

/* ── Sesión (compartida en vivo con el PC) ── */
async function unirseSesion(){
  try{
    const u=new URL(location.href); const s=u.searchParams.get('s');
    const r=await fetch(s?`/api/dev/sesion/${s}`:'/api/dev/sesion-activa?origen=movil');
    if(r.status===401){ location.href='/login?next=/movil'; return; }
    const d=await r.json(); SID=d.id;
    $('ses-info').textContent=`Sesión de hoy · ${d.fotos.length} fotos tomadas`;
    pintarEstado(d);
    setInterval(refrescar, 4000);
  }catch(e){
    $('ses-info').textContent='⚠ Sin conexión con el servidor';
    $('pill').textContent='sin conexión';
  }
}
async function refrescar(){
  if(!SID) return;
  try{ pintarEstado(await (await fetch(`/api/dev/sesion/${SID}`)).json()); }
  catch(e){ $('pill').textContent='sin conexión'; $('pill').className=''; }
}
let ultHashTira='';
function pintarEstado(d){
  nFotos=d.fotos.length;
  const p=$('pill');
  if(subiendo>0){ p.textContent=' subiendo…'; p.className=''; }
  else if(d.estado==='procesando'){ p.textContent=`leyendo ${(d.progreso&&d.progreso.actual)||0}/${nFotos}`; p.className=''; }
  else{
    const listas=(d.hojas||[]).filter(h=>h.ocr&&!h.error).length;
    p.textContent=`${nFotos} fotos ✓`+(listas?` · ${listas} leídas`:''); p.className='ok';
  }
  pintarSemaforoTira(d);
  // Reconstruir la tira SOLO si las fotos cambiaron (recrear <img> en cada poll
  // reinicia descargas: miniaturas a medias + datos móviles desperdiciados)
  const hf=SID+'|'+JSON.stringify(d.fotos);
  if(hf===ultHashTira) return;
  ultHashTira=hf;
  const t=$('tira'); t.innerHTML='';
  d.fotos.forEach(f=>{
    const el=document.createElement('div'); el.className='mini'; el.dataset.n=f.n;
    el.innerHTML=`<img src="/api/dev/sesion/${SID}/foto/${f.n}?ts=${f.ts}" loading="lazy">`+
      `<button onclick="borrarFoto(${f.n})">✕</button>`;
    t.appendChild(el);
  });
  t.scrollLeft=t.scrollWidth;
  pintarSemaforoTira(d);
}
// Borde de cada miniatura = estado de su hoja: azul (leyendo), verde/amarillo/rojo
// (semáforo del OCR+SGS). Así desde el celular se ve si una hoja salió mal y se
// repite la foto ahí mismo, sin ir al PC.
function pintarSemaforoTira(d){
  const porFoto={};
  (d.hojas||[]).forEach(h=>{
    let c='verde';
    if(h.error||(h.verificacion&&h.verificacion.estado==='rojo')) c='rojo';
    else if(h.continuacion_de) c='azul';
    else{
      const ms=(h.ocr&&h.ocr.materiales)||[];
      if(ms.some(m=>m.color==='rojo')) c='rojo';
      else if(ms.some(m=>m.color==='amarillo')) c='amarillo';
    }
    if(h.duplicada_de) c='rojo';
    porFoto[h.foto_n]=c;
  });
  document.querySelectorAll('#tira .mini').forEach(el=>{
    const c=porFoto[el.dataset.n]||(d.estado==='procesando'?'leyendo':'');
    el.className='mini'+(c?' '+c:'');
  });
}
async function nuevaSesionMovil(){
  if(nFotos>0 && !confirm('¿Empezar una sesión NUEVA?\n'+
      `Las ${nFotos} fotos actuales quedan guardadas en la sesión anterior.`)) return;
  try{
    const d=await (await fetch('/api/dev/sesion',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:'{"origen":"movil"}'})).json();
    SID=d.id; nFotos=0;
    $('ses-info').textContent='Sesión de hoy · 0 fotos tomadas';
    pintarEstado(d);
    toast('Sesión nueva creada — lo que escanees va aquí');
  }catch(e){ toast('⚠ No se pudo crear la sesión — revisa la conexión', 3500); }
}

/* ── Elección de modo (el gesto del usuario habilita la cámara) ── */
function elegirVivo(){
  $('pantalla-inicio').style.display='none';
  $('visor').style.display='flex';
  $('controles-vivo').style.display='flex';
  $('lnk-nativa').style.display='';
  iniciarCamara();
}
function elegirNativa(){
  $('pantalla-inicio').style.display='none';
  $('pantalla-nativa').style.display='flex';
}
function aNativa(){
  try{ if(track) track.stop(); }catch(e){}
  $('visor').style.display='none';
  $('controles-vivo').style.display='none';
  $('zoom-caja').style.display='none';
  $('lnk-nativa').style.display='none';
  $('pantalla-nativa').style.display='flex';
}

/* ── Cámara en vivo ── */
async function iniciarCamara(){
  if(!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)){
    toast('Este navegador no soporta cámara en vivo — usa la cámara del teléfono', 3500);
    aNativa(); return;
  }
  try{
    const stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{
      facingMode:{ideal:'environment'}, width:{ideal:4096}, height:{ideal:2160}}});
    video.srcObject=stream;
    track=stream.getVideoTracks()[0];
    try{ imageCapture=('ImageCapture' in window)?new ImageCapture(track):null; }catch(e){}
    await new Promise(res=>{ video.onloadedmetadata=res; setTimeout(res,3000); });
    try{ await video.play(); }catch(e){}
    $('caja-video').style.aspectRatio=video.videoWidth+'/'+video.videoHeight;
    overlay.width=video.videoWidth; overlay.height=video.videoHeight;
    configurarCamara();
    try{ if('wakeLock' in navigator) await navigator.wakeLock.request('screen'); }catch(e){}
    msj.textContent = cvListo ? 'Buscando hoja…' : 'Cargando detector…';
    requestAnimationFrame(bucleDeteccion);
  }catch(e){
    toast(e && e.name==='NotAllowedError'
      ? 'Permiso de cámara denegado — usa la cámara del teléfono o habilita el permiso'
      : '⚠ No se pudo abrir la cámara en vivo — usa la cámara del teléfono', 4000);
    aNativa();
  }
}
function configurarCamara(){
  const caps = track.getCapabilities ? track.getCapabilities() : {};
  if(caps.focusMode && caps.focusMode.includes('continuous'))
    track.applyConstraints({advanced:[{focusMode:'continuous'}]}).catch(()=>{});
  if(caps.whiteBalanceMode && caps.whiteBalanceMode.includes('continuous'))
    track.applyConstraints({advanced:[{whiteBalanceMode:'continuous'}]}).catch(()=>{});
  if(caps.torch){
    const b=$('btn-flash'); b.style.visibility='visible';
    let on=false;
    b.onclick=()=>{on=!on; b.classList.toggle('on',on);
      track.applyConstraints({advanced:[{torch:on}]}).catch(()=>{});};
  }
  if(caps.zoom){
    const z=$('zoom'); $('zoom-caja').style.display='flex';
    z.min=caps.zoom.min; z.max=Math.min(caps.zoom.max, caps.zoom.min*4); z.step=caps.zoom.step||0.1;
    z.value=track.getSettings().zoom||caps.zoom.min;
    z.oninput=()=>track.applyConstraints({advanced:[{zoom:parseFloat(z.value)}]}).catch(()=>{});
  }
}

/* ── Cámara nativa del teléfono ── */
$('inp-file').onchange=async ev=>{
  const file=ev.target.files[0]; if(!file) return;
  try{
    // imageOrientation explícito: sin él, algunos navegadores ignoran el EXIF y
    // la foto llega rotada 90° → el detector recorta un falso cuadrilátero
    let bmp;
    try{ bmp=await createImageBitmap(file,{imageOrientation:'from-image'}); }
    catch(e){ bmp=await createImageBitmap(file); }
    const c=document.createElement('canvas'); c.width=bmp.width; c.height=bmp.height;
    c.getContext('2d').drawImage(bmp,0,0);
    await recortarYSubir(c);
  }catch(e){ toast('⚠ No se pudo leer la foto', 3000); }
  ev.target.value='';
};

/* ── Detector de hoja (OpenCV + jscanify) ── */
async function cargarOpenCV(){
  try{
    const resp=await fetch('/static-dev/opencv.js');
    if(!resp.ok) throw new Error(resp.status);
    const total=+resp.headers.get('Content-Length')||0;
    const reader=resp.body.getReader(); const chunks=[]; let recibido=0;
    while(true){
      const {done,value}=await reader.read(); if(done) break;
      chunks.push(value); recibido+=value.length;
      if(total) progresoCV(Math.min(99, Math.round(recibido/total*100)));
    }
    const s=document.createElement('script');
    s.src=URL.createObjectURL(new Blob(chunks,{type:'application/javascript'}));
    s.onload=()=>{ const espera=()=>{
      if(window.cv && cv.Mat){ cvOK(); }
      else if(window.cv && cv.onRuntimeInitialized!==undefined){
        cv['onRuntimeInitialized']=cvOK; }
      else setTimeout(espera,150); }; espera(); };
    document.head.appendChild(s);
  }catch(e){
    $('estado-cv').textContent='Detector no disponible: la foto se sube completa.';
    if($('visor').style.display!=='none') msj.textContent='Sin detector — usa el disparador';
  }
}
function progresoCV(p){
  const t=`Cargando detector… ${p}%`;
  if($('visor').style.display!=='none') msj.textContent=t;
  $('estado-cv').textContent=t+' (solo la primera vez)';
}
function cvOK(){
  cvListo=true; scanner=new jscanify();
  ultDetOk=performance.now();
  if($('visor').style.display!=='none') msj.textContent='Buscando hoja…';
  $('estado-cv').textContent = recorteAuto
    ? ' Detector de hoja listo: el recorte es automático.'
    : '⬜ Detector listo, pero el recorte está apagado: la foto sube completa.';
}

/* Detección propia (más robusta que la de jscanify, que no dilata los bordes
   y falla con fondos reales): Canny + dilatación → cuadrilátero convexo más
   grande; si no aparece, segunda pasada segmentando la hoja por brillo (Otsu). */
const DET_W=480;
const detCanvas=document.createElement('canvas');

function ordenarEsquinas(pts){
  let tl=pts[0], tr=pts[0], br=pts[0], bl=pts[0];
  for(const p of pts){
    if(p.x+p.y < tl.x+tl.y) tl=p;
    if(p.x+p.y > br.x+br.y) br=p;
    if(p.x-p.y > tr.x-tr.y) tr=p;
    if(p.x-p.y < bl.x-bl.y) bl=p;
  }
  return {topLeftCorner:tl, topRightCorner:tr, bottomRightCorner:br, bottomLeftCorner:bl};
}
function buscarQuad(binaria, minArea){
  const contours=new cv.MatVector(), hierarchy=new cv.Mat();
  let mejor=null, mejorArea=0;
  try{
    cv.findContours(binaria, contours, hierarchy, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE);
    for(let i=0;i<contours.size();i++){
      const cnt=contours.get(i);
      const area=cv.contourArea(cnt);
      if(area>minArea && area>mejorArea){
        const peri=cv.arcLength(cnt,true);
        for(const eps of [0.02, 0.04, 0.06]){
          const approx=new cv.Mat();
          cv.approxPolyDP(cnt, approx, eps*peri, true);
          const cuadro = approx.rows===4 && cv.isContourConvex(approx);
          if(cuadro){
            mejorArea=area; mejor=[];
            for(let j=0;j<4;j++) mejor.push({x:approx.data32S[j*2], y:approx.data32S[j*2+1]});
          }
          approx.delete();
          if(cuadro) break;
        }
      }
      cnt.delete();
    }
  }finally{ contours.delete(); hierarchy.delete(); }
  return mejor;
}
function detectar(fuente, sw, sh){
  if(!cvListo) return null;
  const esc=DET_W/sw;
  detCanvas.width=DET_W; detCanvas.height=Math.round(sh*esc);
  detCanvas.getContext('2d').drawImage(fuente,0,0,detCanvas.width,detCanvas.height);
  const minArea=0.15*detCanvas.width*detCanvas.height;
  let src=null, gray=null, blur=null, edges=null, kernel=null, bin=null;
  try{
    src=cv.imread(detCanvas);
    gray=new cv.Mat();  cv.cvtColor(src, gray, cv.COLOR_RGBA2GRAY);
    blur=new cv.Mat();  cv.GaussianBlur(gray, blur, new cv.Size(5,5), 0);
    kernel=cv.getStructuringElement(cv.MORPH_RECT, new cv.Size(5,5));
    // 1) bordes con dilatación (cierra los huecos del contorno de la hoja)
    edges=new cv.Mat(); cv.Canny(blur, edges, 60, 180);
    cv.dilate(edges, edges, kernel);
    let quad=buscarQuad(edges, minArea);
    // 2) la hoja suele ser lo más claro de la escena
    if(!quad){
      bin=new cv.Mat();
      cv.threshold(blur, bin, 0, 255, cv.THRESH_BINARY + cv.THRESH_OTSU);
      cv.morphologyEx(bin, bin, cv.MORPH_CLOSE, kernel);
      quad=buscarQuad(bin, minArea);
    }
    if(!quad) return null;
    const f=sw/DET_W;
    const e=ordenarEsquinas(quad.map(p=>({x:p.x*f, y:p.y*f})));
    const ks=[e.topLeftCorner,e.topRightCorner,e.bottomRightCorner,e.bottomLeftCorner];
    for(let i=0;i<4;i++) for(let j=i+1;j<4;j++)
      if(Math.hypot(ks[i].x-ks[j].x, ks[i].y-ks[j].y) < sw*0.05) return null;
    return e;
  }catch(err){ return null; }
  finally{
    [src,gray,blur,edges,kernel,bin].forEach(m=>{ if(m) try{m.delete();}catch(e){} });
  }
}

let ultFrame=0, ultDetOk=0;
function bucleDeteccion(ts){
  if($('visor').style.display==='none') return;   // salió del modo vivo
  requestAnimationFrame(bucleDeteccion);
  if(!cvListo || detectando || video.readyState<2) return;
  if(ts-ultFrame<160) return; ultFrame=ts;
  detectando=true;
  try{
    const esq=detectar(video, video.videoWidth, video.videoHeight);
    const ctx=overlay.getContext('2d'); ctx.clearRect(0,0,overlay.width,overlay.height);
    if(esq){
      let mov=0;
      if(ultEsq){ for(const k of Object.keys(esq))
        mov=Math.max(mov, Math.hypot(esq[k].x-ultEsq[k].x, esq[k].y-ultEsq[k].y)); }
      estables = (ultEsq && mov < overlay.width*0.02) ? estables+1 : 0;
      ultEsq=esq;
      const estable = estables>=7;
      ctx.strokeStyle=estable?'#22c55e':'#eab308';
      ctx.lineWidth=Math.max(4, overlay.width/220);
      ctx.beginPath();
      ctx.moveTo(esq.topLeftCorner.x,esq.topLeftCorner.y);
      ctx.lineTo(esq.topRightCorner.x,esq.topRightCorner.y);
      ctx.lineTo(esq.bottomRightCorner.x,esq.bottomRightCorner.y);
      ctx.lineTo(esq.bottomLeftCorner.x,esq.bottomLeftCorner.y);
      ctx.closePath(); ctx.stroke();
      msj.textContent = estable ? '✓ Hoja detectada' : 'Hoja a la vista — mantén firme';
      msj.style.color = estable ? '#22c55e' : '#eab308';
      ultDetOk=performance.now();
      if(estable && autoCaptura && (performance.now()-ultimaCaptura)>3000){
        ultimaCaptura=performance.now(); capturar();
      }
    }else{
      ultEsq=null; estables=0;
      msj.textContent = (performance.now()-ultDetOk>8000)
        ? 'Acerca la hoja: que llene la pantalla y contraste con el fondo'
        : 'Buscando hoja…';
      msj.style.color='#cbd5e1';
    }
  }finally{ detectando=false; }
}

/* ── Realce determinista y control de nitidez ──
   Solo matemáticas sobre los píxeles reales (CLAHE + nitidez): jamás se
   inventa un trazo — a diferencia de un "mejorador" generativo. */
const UMBRAL_BORROSA=60;    // varianza Laplaciano (a 800px): debajo = rechazar
const UMBRAL_DUDOSA=140;    // debajo = subir pero avisar

function nivelNitidez(canvas){
  if(!cvListo) return null;
  let src=null,gray=null,lap=null,media=null,std=null;
  try{
    const c=document.createElement('canvas');
    const esc=Math.min(1, 800/canvas.width);
    c.width=Math.round(canvas.width*esc); c.height=Math.round(canvas.height*esc);
    c.getContext('2d').drawImage(canvas,0,0,c.width,c.height);
    src=cv.imread(c);
    gray=new cv.Mat(); cv.cvtColor(src,gray,cv.COLOR_RGBA2GRAY);
    lap=new cv.Mat(); cv.Laplacian(gray,lap,cv.CV_64F);
    media=new cv.Mat(); std=new cv.Mat();
    cv.meanStdDev(lap,media,std);
    const s=std.doubleAt(0,0);
    return s*s;
  }catch(e){ return null; }
  finally{ [src,gray,lap,media,std].forEach(m=>{ if(m) try{m.delete();}catch(e){} }); }
}

function mejorarImagen(canvas){
  if(!cvListo) return canvas;
  let src=null,gray=null,eq=null,blur=null,sharp=null,clahe=null;
  try{
    src=cv.imread(canvas);
    gray=new cv.Mat(); cv.cvtColor(src,gray,cv.COLOR_RGBA2GRAY);
    if(typeof cv.CLAHE==='function'){
      eq=new cv.Mat();
      clahe=new cv.CLAHE(2.0, new cv.Size(8,8));   // contraste adaptativo suave
      clahe.apply(gray, eq);
    }else{
      eq=gray.clone();
    }
    // nitidez suave (unsharp mask): eq*1.5 − desenfocado*0.5
    blur=new cv.Mat(); cv.GaussianBlur(eq, blur, new cv.Size(0,0), 2);
    sharp=new cv.Mat(); cv.addWeighted(eq, 1.5, blur, -0.5, 0, sharp);
    const out=document.createElement('canvas');
    cv.imshow(out, sharp);
    return out;
  }catch(e){ return canvas; }
  finally{
    [src,gray,eq,blur,sharp].forEach(m=>{ if(m) try{m.delete();}catch(e){} });
    if(clahe) try{clahe.delete();}catch(e){}
  }
}

/* ── Captura y subida ── */
const MAX_LADO=2576;   // resolución máxima que aprovecha la visión de la IA
async function capturar(){
  if(navigator.vibrate) navigator.vibrate(60);
  const fl=$('flash-captura'); fl.style.opacity=.7; setTimeout(()=>fl.style.opacity=0,120);
  let bmp=null;
  if(imageCapture && imageCapture.takePhoto){
    try{ const b=await imageCapture.takePhoto(); bmp=await createImageBitmap(b); }catch(e){}
  }
  if(!bmp){ try{ bmp=await createImageBitmap(video); }catch(e){ return; } }
  const full=document.createElement('canvas'); full.width=bmp.width; full.height=bmp.height;
  full.getContext('2d').drawImage(bmp,0,0);
  await recortarYSubir(full);
}
/* El warp SOLO se aplica si el cuadrilátero realmente parece la hoja entera Y
   VISIBLE. Devuelve 'ok' o el motivo del rechazo:
   · 'borde'   — alguna esquina pegada al borde de la foto: la hoja quedó cortada
                 por el encuadre. El detector arma un cuadrilátero con los bordes
                 de la FOTO (no del papel) y "enderezar" contra eso tuerce todo.
                 Sin los 4 bordes del papel a la vista no existe warp correcto.
   · 'chico'   — ocupa <35% de la foto (una tabla impresa, una sombra).
   · 'torcido' — esquinas lejos de 90°: no es un papel visto de frente/leve ángulo. */
function quadValido(esq, w, h){
  const ks=[esq.topLeftCorner,esq.topRightCorner,esq.bottomRightCorner,esq.bottomLeftCorner];
  const mx=w*0.02, my=h*0.02;
  for(const p of ks)
    if(p.x<mx || p.y<my || p.x>w-mx || p.y>h-my) return 'borde';
  let area=0;
  for(let i=0;i<4;i++){ const a=ks[i], b=ks[(i+1)%4]; area+=a.x*b.y-b.x*a.y; }
  if(Math.abs(area)/2 < 0.35*w*h) return 'chico';
  for(let i=0;i<4;i++){
    const p=ks[(i+3)%4], v=ks[i], n=ks[(i+1)%4];
    let ang=Math.abs(Math.atan2(p.y-v.y,p.x-v.x)-Math.atan2(n.y-v.y,n.x-v.x))*180/Math.PI;
    if(ang>180) ang=360-ang;
    if(ang<55 || ang>125) return 'torcido';
  }
  return 'ok';
}
async function recortarYSubir(full){
  let salida=full, notaRecorte='';
  if(cvListo && recorteAuto){
    const esq=detectar(full, full.width, full.height);
    const veredicto = esq ? quadValido(esq, full.width, full.height) : null;
    if(veredicto==='ok'){
      const d=(a,b)=>Math.hypot(a.x-b.x,a.y-b.y);
      let W=Math.max(d(esq.topLeftCorner,esq.topRightCorner), d(esq.bottomLeftCorner,esq.bottomRightCorner));
      let H=Math.max(d(esq.topLeftCorner,esq.bottomLeftCorner), d(esq.topRightCorner,esq.bottomRightCorner));
      const escala=Math.min(1, MAX_LADO/Math.max(W,H));
      W=Math.round(W*escala); H=Math.round(H*escala);
   try{ const rec=scanner.extractPaper(full, W, H, esq); if(rec){ salida=rec; notaRecorte=' enderezada'; } }catch(e){}
    }else if(veredicto==='borde'){
      notaRecorte=' — subió COMPLETA: la hoja quedó cortada por el encuadre. '+
        'Para que se enderece sola, toma la foto con los 4 bordes de la hoja a la vista';
    }else if(veredicto){
      notaRecorte=' — subió COMPLETA (sin recorte: detección dudosa)';
    }
  }
  const lado=Math.max(salida.width,salida.height);
  if(lado>MAX_LADO){
    const esc=MAX_LADO/lado;
    const c=document.createElement('canvas');
    c.width=Math.round(salida.width*esc); c.height=Math.round(salida.height*esc);
    c.getContext('2d').drawImage(salida,0,0,c.width,c.height);
    salida=c;
  }
  // Control de nitidez: la foto borrosa se rechaza AQUÍ, con la hoja aún en la mano
  const nit=nivelNitidez(salida);
  if(nit!==null){
    if(nit<UMBRAL_BORROSA){
      if(navigator.vibrate) navigator.vibrate([90,60,90]);
      toast('Foto BORROSA — no se subió. Limpia el lente, acércate y repítela.', 4500);
      return;
    }
    if(nit<UMBRAL_DUDOSA)
      toast('⚠ Nitidez justa — se subió, pero revisa la miniatura', 3000);
  }
  // Realce determinista para que el OCR lea mejor el manuscrito
  salida=mejorarImagen(salida);
  // 0.85: el contraste CLAHE hace que el grano del papel comprima peor; con 0.9
  // salían JPEG de 1.5MB (lentos de subir y de miniaturizar) sin ganancia visible
  const blob=await new Promise(res=>salida.toBlob(res,'image/jpeg',0.85));
  subirBlob(blob, notaRecorte);
}
async function subirBlob(blob, nota){
  subiendo++; $('pill').textContent=' subiendo…'; $('pill').className='';
  for(let i=0;i<3;i++){
    try{
      const r=await fetch(`/api/dev/sesion/${SID}/foto`,{method:'POST',
        headers:{'Content-Type':'image/jpeg','X-Origen':'movil'}, body:blob});
      if(r.ok){
        const j=await r.json();
        subiendo--; toast(`✓ Foto ${j.n} sincronizada${j.auto_ocr?' · leyendo':''}${nota||''}`, (nota&&nota.length>30)?7000:(nota?3000:1800));
        refrescar(); return;
      }
    }catch(e){}
    await new Promise(res=>setTimeout(res, 1200*(i+1)));
  }
  subiendo--;
  toast('⚠ No se pudo subir la foto — revisa la conexión', 4000);
  refrescar();
}

$('disparador').onclick=()=>{ ultimaCaptura=performance.now(); capturar(); };
$('btn-auto').onclick=()=>{ autoCaptura=!autoCaptura;
  $('btn-auto').classList.toggle('on',autoCaptura);
  toast(autoCaptura?'Captura automática ACTIVADA':'Captura automática apagada'); };

pintarRecorte();
unirseSesion();
cargarOpenCV();
</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════════
#  Página PC — sesiones, fotos en vivo, procesamiento y resultados
# ══════════════════════════════════════════════════════════════════════════

HTML_PC = r"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="robots" content="noindex, nofollow"><title>Devoluciones</title>
<style>
:root{--verde:#16a34a;--amarillo:#ca8a04;--rojo:#dc2626;--fondo:#0f172a;--panel:#1e293b;
      --borde:#334155;--texto:#e2e8f0;--sub:#94a3b8}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--fondo);color:var(--texto);font-family:system-ui,sans-serif;font-size:14px;
     overflow-x:hidden}
header{padding:12px 20px;background:var(--panel);display:flex;align-items:center;gap:16px;
       border-bottom:1px solid var(--borde);flex-wrap:wrap}
header h1{font-size:17px}
header .url{font-size:12px;color:var(--sub)}
header .url b{color:#7dd3fc;user-select:all}
/* minmax(0,1fr): sin esto, una tabla ancha fuerza la columna más allá del
   viewport y toda la página gana un scroll lateral invisible */
#layout{display:grid;grid-template-columns:230px minmax(0,1fr);min-height:calc(100vh - 53px)}
aside{border-right:1px solid var(--borde);padding:12px}
aside h2{font-size:12px;color:var(--sub);text-transform:uppercase;margin-bottom:8px}
.ses{padding:8px 10px;border-radius:8px;cursor:pointer;margin-bottom:4px;border:1px solid transparent}
.ses:hover{background:var(--panel)}
.ses.activa{background:var(--panel);border-color:#0ea5e9}
.ses small{color:var(--sub);display:block}
main{padding:16px 20px;max-width:1500px;min-width:0}
button{border:none;border-radius:8px;padding:9px 16px;font-size:14px;cursor:pointer;
       background:#334155;color:var(--texto)}
button.primario{background:#0ea5e9;color:#fff;font-weight:600}
button.peligro{background:#7f1d1d;color:#fecaca}
button:disabled{opacity:.45;cursor:default}
#barra-acciones{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
#chip-estado{padding:3px 12px;border-radius:99px;background:#334155;font-size:12px}
#grid-fotos{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px}
.foto{position:relative}
.foto img{height:130px;border-radius:8px;border:1px solid var(--borde);cursor:zoom-in}
.foto span{position:absolute;left:6px;top:6px;background:#000a;padding:1px 7px;border-radius:6px;
           font-size:11px}
.foto button{position:absolute;right:4px;top:4px;padding:1px 7px;font-size:11px;background:#7f1d1d}
#progreso{margin:10px 0;color:#7dd3fc}
.hoja{background:var(--panel);border:1px solid var(--borde);border-radius:12px;padding:14px 16px;
      margin-bottom:16px;overflow-x:auto}
.hoja h3{font-size:15px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.chip{padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600}
.chip.verde{background:#052e16;color:#4ade80;border:1px solid #166534}
.chip.amarillo{background:#3b2f04;color:#fde047;border:1px solid #854d0e}
.chip.rojo{background:#450a0a;color:#fca5a5;border:1px solid #991b1b}
.chip.azul{background:#082f49;color:#7dd3fc;border:1px solid #0c4a6e}
.meta{color:var(--sub);font-size:13px;margin-bottom:8px}
.msgs{margin:6px 0;font-size:13px}
.msgs div{padding:2px 0;color:#fbbf24}
table{border-collapse:collapse;width:100%;font-size:13px}
th{background:#0b1220;color:var(--sub);text-align:left;padding:6px 8px;font-weight:600;
   position:sticky;top:0}
td{padding:5px 8px;border-top:1px solid var(--borde)}
tr.verde td{background:#052e1622}
tr.amarillo td{background:#3b2f0433}
tr.rojo td{background:#450a0a44}
td .punto{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
.punto.verde{background:var(--verde)}.punto.amarillo{background:var(--amarillo)}
.punto.rojo{background:var(--rojo)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.dev{font-weight:700}
input.edit{width:58px;background:#0b1220;border:1px solid #334155;color:#e2e8f0;
  border-radius:6px;padding:3px 6px;text-align:right;font-size:13px}
input.edit:focus{border-color:#0ea5e9;outline:none}
input.edit.editado{border-color:#0ea5e9;color:#7dd3fc;font-weight:700}
input.edit-head{width:118px;background:#0b1220;border:1px solid #334155;color:#e2e8f0;
  border-radius:6px;padding:3px 8px;font-size:13px;font-weight:600}
input.edit-head:focus{border-color:#0ea5e9;outline:none}
.btn-conf{width:26px;height:26px;padding:0;border-radius:6px;background:#1e293b;
  color:#64748b;font-size:14px;line-height:1}
.btn-conf.on{background:#166534;color:#fff}
.btn-del{padding:4px 10px;border-radius:6px;background:#7f1d1d;color:#fecaca;
  font-size:12px;white-space:nowrap}
.btn-del:hover{background:#991b1b}
.agregar-nota{margin-top:8px;display:flex;gap:8px;align-items:center;color:var(--sub);
  font-size:13px;flex-wrap:wrap}
.btn-sgs{padding:3px 10px;font-size:12px;background:#1e293b}
.prean{margin-top:12px;border-top:1px dashed var(--borde);padding-top:10px}
.prean h4{font-size:14px;display:flex;gap:10px;align-items:center;margin-bottom:6px}
.barra{height:14px;background:#0b1220;border:1px solid var(--borde);border-radius:99px;
  overflow:hidden;margin:6px 0}
.barra div{height:100%;background:linear-gradient(90deg,#0ea5e9,#22c55e);transition:width .5s}
.lnk-sgs{color:#7dd3fc;font-size:13px;text-decoration:none;border:1px solid #334155;
  padding:2px 10px;border-radius:99px}
.lnk-sgs:hover{background:#1e293b}
#modal-rev{position:fixed;inset:0;background:#000b;display:none;align-items:flex-start;
  justify-content:center;z-index:40;padding:30px 12px;overflow-y:auto}
.rev-caja{background:var(--fondo);border:1px solid var(--borde);border-radius:14px;
  max-width:980px;width:100%;padding:16px 20px}
.rev-top{display:flex;justify-content:space-between;align-items:center;gap:12px;
  margin-bottom:12px;position:sticky;top:-16px;background:var(--fondo);padding:8px 0;z-index:2}
.rev-top h3{font-size:16px}
#rev-buscar{flex:1;max-width:320px;background:#0b1220;border:1px solid #334155;
  color:var(--texto);border-radius:99px;padding:8px 16px;font-size:14px;outline:none}
#rev-buscar:focus{border-color:#0ea5e9}
.rev-card{background:var(--panel);border:1px solid var(--borde);border-radius:12px;
  padding:14px 16px;margin-bottom:14px}
#modal-adic{position:fixed;inset:0;background:#000b;display:none;align-items:flex-start;
  justify-content:center;z-index:40;padding:30px 12px;overflow-y:auto}
#modal-adic .rev-caja{max-width:1180px}
#adic-buscar{flex:1;max-width:280px;background:#0b1220;border:1px solid #334155;
  color:var(--texto);border-radius:99px;padding:8px 16px;font-size:14px;outline:none}
#adic-buscar:focus{border-color:#a855f7}
button.morado{background:#7c3aed;color:#fff;font-weight:600}
.adic-barra{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.adic-filtro{padding:5px 12px;font-size:13px;border-radius:99px;background:#1e293b;
  border:1px solid var(--borde)}
.adic-filtro.on{background:#3b0764;border-color:#a855f7;color:#f3e8ff}
.adic-card.lista{border-color:#166534}.adic-card.resolver{border-color:#854d0e}
.adic-card.creada{border-color:#6b21a8}.adic-card.sin_nodo,.adic-card.no_aut{border-color:#991b1b}
.adic-num{font-size:13px;font-weight:800;color:#d8b4fe;background:#3b0764;border-radius:99px;
  padding:2px 10px;white-space:nowrap}
.adic-acc{display:flex;gap:8px;justify-content:flex-end;margin-top:10px;flex-wrap:wrap}
.sug{padding:3px 9px;font-size:12px;background:#082f49;color:#7dd3fc;border:1px solid #0c4a6e;
  border-radius:6px;margin:2px 4px 2px 0;text-align:left}
.sug:hover{background:#0c4a6e}
.nota-txt{color:#fde68a;font-family:ui-monospace,Consolas,monospace;font-size:12px}
input.edit.ancho{width:84px;text-align:left}
.rev-card.mal{border-color:#991b1b}
.rev-head{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:4px}
.ban-grande{font-size:30px;font-weight:800;color:#7dd3fc;letter-spacing:1px;
  user-select:all}
.rev-sub{color:var(--sub);font-size:14px;margin-bottom:8px}
.rev-sub b{color:var(--texto)}
.rev-sub b.pro{color:#fbbf24;font-size:17px;user-select:all}
.rev-tag{font-size:12px;font-weight:800;padding:3px 12px;border-radius:99px;white-space:nowrap}
.rev-tag.dev{background:#0c4a6e;color:#7dd3fc}
.rev-tag.adi{background:#3b0764;color:#d8b4fe}
.ban-grande.adi{color:#d8b4fe}
.lnk-hoja{color:#7dd3fc;cursor:pointer;text-decoration:underline dotted;font-weight:600}
.lnk-hoja:hover{color:#bae6fd}
#modal-hoja{position:fixed;inset:0;background:#000c;display:none;align-items:stretch;
  justify-content:center;z-index:45;padding:20px}
.mh-caja{background:var(--fondo);border:1px solid var(--borde);border-radius:14px;
  display:flex;gap:0;max-width:1500px;width:100%;overflow:hidden}
.mh-foto{flex:0 0 52%;background:#000;display:flex;align-items:center;justify-content:center;
  overflow:hidden}
#mh-lupa{overflow:hidden;width:100%;height:100%;display:flex;align-items:center;
  justify-content:center;cursor:zoom-in}
#mh-img{max-width:100%;max-height:calc(100vh - 40px);display:block;
  transition:transform .12s ease-out}
#mh-img.zoom{transform:scale(2.6)}
.mh-datos{flex:1 1 auto;padding:16px 20px;overflow-y:auto;max-height:calc(100vh - 40px);
  min-width:0}
.mh-datos h4{font-size:14px;margin:12px 0 6px;color:#7dd3fc}
#resumen{background:#052e16;border:1px solid #166534;border-radius:12px;padding:14px 16px;
         margin-bottom:18px;overflow-x:auto}
#resumen h3{margin-bottom:8px;color:#4ade80}
#visor-foto{position:fixed;inset:0;background:#000c;display:none;align-items:center;
            justify-content:center;z-index:50;cursor:zoom-out}
#visor-foto img{max-width:95vw;max-height:95vh;border-radius:8px}
.vacio{color:var(--sub);padding:30px;text-align:center}
/* Modo observador (rol visor): solo mirar. El servidor además rechaza con 403
   cualquier escritura, esto solo evita botones que no van a funcionar. */
body.visor #barra-acciones button, body.visor .foto button, body.visor aside>button,
body.visor .btn-sgs, body.visor .sug, body.visor #adic-acciones{display:none!important}
body.visor input.edit, body.visor input.edit-head{pointer-events:none;border-color:transparent}
#aviso-zona{margin:0 20px;padding:9px 14px;border-radius:0 0 10px 10px;font-size:13px;
  background:#1e3a5f;border:1px solid #2563eb;border-top:0;display:none}
</style></head><body>
<header>
  <h1>Devoluciones SGS — captura + OCR IA</h1>
  <div id="quien" style="position:absolute;top:14px;right:18px;font-size:12px;color:#94a3b8"></div>
  <div class="url">En el celular abre: <b id="url-movil">...</b>
    <span style="color:#64748b">(aceptar el aviso del certificado la primera vez)</span></div>
</header>
<div id="aviso-zona"></div>
<div id="layout">
<aside>
  <button class="primario" style="width:100%;margin-bottom:10px" onclick="nuevaSesion()">+ Nueva sesión</button>
  <input id="buscar" type="search" placeholder="BAN, PRO, NODO o líder…" autocomplete="off"
    style="width:100%;box-sizing:border-box;margin-bottom:8px;padding:8px 10px;border-radius:8px;border:1px solid var(--borde);background:#0b1220;color:#e2e8f0"
    oninput="buscarHojas(this.value)" title="Busca una hoja en TODAS las sesiones: escribe el BAN (124687), el PRO (65328), parte del nombre del NODO o del líder">
  <div id="res-buscar"></div>
  <h2>Sesiones</h2>
  <div id="lista-ses"></div>
</aside>
<main>
  <div id="sin-sesion" class="vacio">Selecciona o crea una sesión.<br><br>
    Flujo: escanea las hojas desde el celular → aquí aparecen en vivo y
    se leen solas ( Auto-OCR) → revisar semáforo verde/amarillo/rojo.</div>
  <div id="panel-sesion" style="display:none">
    <div id="barra-acciones">
      <span id="chip-estado"></span>
      <button class="primario" id="btn-procesar" onclick="procesar()"
        title="Procesa las fotos que aún no tienen OCR (con auto-OCR encendido casi nunca hace falta)">Procesar con IA</button>
      <button id="btn-auto" onclick="toggleAuto()"
        title="Auto-OCR: cada foto que llega del celular se procesa sola, sin pulsar «Procesar». Puedes ir corrigiendo hojas mientras las demás se leen."> Auto-OCR</button>
      <button id="btn-revtodo" onclick="reverificarTodo()"
        title="Re-verifica todas las hojas contra SGS sin repetir el OCR (gratis) — úsalo si todo salió en rojo por caída de SGS">↻ Reverificar todo</button>
      <button id="btn-cargue" class="primario" onclick="location.href='/almacen/cargue-sesion/'+SID"
        title="Cargue de instalaciones (SIPWEBORA) armado con las hojas de esta sesión: cada bandeja con sus materiales, devolución de la hoja y Aportación del ANEXO 14. No necesita las bandejas de devolución de SGS.">Cargue de instalaciones</button>
      <button id="btn-excel" data-rol="admin" onclick="window.open(`/api/dev/sesion/${SID}/excel`,'_blank')"
        title="Excel plano de la sesión (una fila por material con devolución + todas las filas leídas)">Excel plano</button>
      <button id="btn-prean" data-rol="admin" onclick="preanalisis()"
        title="Cruce solo-lectura contra SGS: saldo devolvible por material, devoluciones previas — evita dobles devoluciones">Preanálisis SGS</button>
      <button id="btn-aprender" data-rol="admin" onclick="aprender()"
        title="Analiza tus correcciones con IA y guarda lecciones para que el OCR no repita esos errores">Aprender de correcciones</button>
      <button id="btn-dev-test" data-rol="admin" onclick="devoluciones(true)"
        title="Simula todo el flujo en SGS hasta antes de GUARDAR — no crea nada">↩ TEST devoluciones</button>
      <button id="btn-dev-real" data-rol="admin" class="peligro" onclick="devoluciones(false)"
        title="Crea las bandejas de devolución REALES en SGS y las aprueba si cuadran con el plan">↩ CREAR EN SGS</button>
      <button id="btn-adic" data-rol="admin" class="morado" onclick="abrirAdic()"
        title="Todos los adicionales de la sesión en una sola lista, en el orden de las fotos: corregir, probar y crear hoja por hoja o todas las listas">+ Adicionales</button>
      <button data-rol="admin" onclick="abrirRevision()"
        title="Popup para comparar visualmente cada devolución con tu hoja física: BAN de devolución en grande + PRO del NODO + materiales">↩ Revisión de devoluciones</button>
      <button data-rol="admin" onclick="aprobarBandejas()"
        title="Aprueba en SGS las bandejas creadas cuya verificación cuadró">✓ Aprobar verificadas</button>
      <button data-rol="admin" onclick="informeTecnico()"
        title="Guarda en el servidor un informe completo de errores y logs para soporte">Informe técnico</button>
      <button class="peligro" onclick="reprocesarSesion()"
        title="Borra el OCR de todas las hojas (las fotos quedan) para volver a procesarlas desde cero — útil si un modelo leyó mal. Las devoluciones/adicionales ya ejecutados en SGS se conservan.">↻ Reprocesar desde cero</button>
      <button onclick="cerrarSesion()">Cerrar sesión de captura</button>
      <span id="progreso"></span>
    </div>
    <div id="grid-fotos"></div>
    <div id="resultados"></div>
  </div>
</main>
</div>
<div id="visor-foto" onclick="this.style.display='none'"><img></div>
<div id="modal-hoja">
  <div class="mh-caja">
    <div class="mh-foto">
      <div id="mh-lupa" title="Mueve el cursor para hacer zoom">
        <img id="mh-img" alt="hoja escaneada">
      </div>
    </div>
    <div class="mh-datos">
      <div class="rev-top" style="position:static;padding:0 0 8px">
        <h3 id="mh-titulo">Hoja</h3>
        <button onclick="cerrarHoja()">✕ Cerrar</button>
      </div>
      <div id="mh-datos-body"></div>
    </div>
  </div>
</div>
<div id="modal-rev">
  <div class="rev-caja">
    <div class="rev-top">
      <h3>↩ Revisión de devoluciones — anota la BAN nueva en tu hoja física</h3>
      <input id="rev-buscar" type="search" placeholder="BAN, PRO o líder — ej: 116, 130, apellido..."
        oninput="pintarRevision()" autocomplete="off">
      <button onclick="cerrarRevision()">✕ Cerrar</button>
    </div>
    <div id="rev-body"></div>
  </div>
</div>
<div id="modal-adic">
  <div class="rev-caja">
    <div class="rev-top">
      <h3>+ Adicionales <small style="color:#94a3b8;font-weight:400">— en el orden de las fotos</small></h3>
      <span id="adic-modo"></span>
      <input id="adic-buscar" type="search" placeholder="BAN, PRO o líder"
        oninput="ultHashAdic='';pintarAdic()" autocomplete="off">
      <span id="adic-prog" class="meta" style="margin:0"></span>
      <button onclick="cerrarAdic()">✕ Cerrar</button>
    </div>
    <div id="adic-acciones"></div>
    <div id="adic-body"></div>
  </div>
</div>
<script>
"use strict";
let SID=null, timer=null, ultHashFotos='', ROL='admin', QUIEN='', ZONA='ubate', BODEGA='UBAT2';
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function etq(m){ // [DOM/ref] descripción — en modo compacto DOM/desc los rellena SGS
  const ref=esc(m.ref_interna||''), dom=m.codigo_dom?esc(m.codigo_dom)+'/':'';
  return `[${dom}${ref}] ${esc(m.descripcion||'')}`; }

async function init(){
  try{const i=await (await fetch('/api/dev/info')).json();
    $('url-movil').textContent=i.url_movil||('https://<IP-del-PC>:'+i.puerto+'/movil');
    ROL=i.rol||'admin'; QUIEN=i.usuario||''; ZONA=i.zona||'ubate'; BODEGA=i.bodega||'UBAT2';
    const rolTxt={admin:'admin',almacen:'almacén',visor:'observador'}[ROL]||ROL;
    $('quien').innerHTML=(QUIEN?('<b>'+esc(QUIEN)+'</b> · '+rolTxt+' · '+esc(i.zona_nombre||'')+' ('+esc(BODEGA)+') · '):'')+
      (ROL==='visor'?'':'<a href="/almacen" style="color:#7dd3fc" title="Cargue de instalaciones (Materiales Sony) con las hojas validadas">Almacén / cargue</a>')+
      (i.auth?' · <a href="/logout" style="color:#7dd3fc">salir</a>':'');
    if(ROL!=='admin') document.querySelectorAll('[data-rol="admin"]').forEach(b=>b.remove());
    const av=$('aviso-zona');
    if(ROL==='visor'){
      document.body.classList.add('visor');
      document.querySelector('header .url').style.display='none';
      av.style.display='block';
      av.innerHTML='<b>Modo observador</b> — zona '+esc(i.zona_nombre||'')+'. Puedes ver cómo funciona el flujo con las sesiones de '+
        esc(i.zona_base_nombre||'Ubaté')+', pero tu cuenta aún no crea ni cambia nada. Cuando '+esc(i.zona_nombre||'tu zona')+
        ' entregue sus credenciales de SGS, se activa con bodega <b>'+esc(BODEGA)+'</b>.';
    } else if(i.zona_operativa===false){
      av.style.display='block'; av.style.background='#422006'; av.style.borderColor='#ca8a04';
      av.innerHTML='⚠ Zona '+esc(i.zona_nombre||'')+' <b>sin credenciales de SGS</b>: puedes fotografiar, leer y revisar, pero ↩ CREAR y + Adicionales no escriben nada hasta configurarlas.';
    }
  }catch(e){}
  await cargarSesiones();
  const ses=await (await fetch('/api/dev/sesiones')).json();
  const h=location.hash.replace('#',''), [hs,hn]=h.split('/');
  if(hn) FOTO_PENDIENTE=parseInt(hn,10);
  if(hs && ses.some(s=>s.id===hs)) seleccionar(hs);
  else if(ses.length) seleccionar(ses[0].id);
}
async function cargarSesiones(){
  const ses=await (await fetch('/api/dev/sesiones')).json();
  const l=$('lista-ses'); l.innerHTML='';
  ses.forEach(s=>{
    const d=document.createElement('div');
    d.className='ses'+(s.id===SID?' activa':'');
    const quien=s.rol_creador==='almacen'?''+esc(s.creada_por):(s.creada_por?''+esc(s.creada_por):'');
    const val=s.n_reales?` · ✓ ${s.n_validadas}/${s.n_reales}`:'';
    const zona=(s.zona&&s.zona!==ZONA)?' · '+esc(s.zona):'';
    d.innerHTML=`<b>${esc(s.id)}</b><small>${esc(s.estado)} · ${s.n_fotos} fotos${val}${quien?' · '+quien:''}${zona}</small>`;
    d.onclick=()=>seleccionar(s.id);
    l.appendChild(d);
  });
}
async function nuevaSesion(){
  const d=await (await fetch('/api/dev/sesion',{method:'POST',
    headers:{'Content-Type':'application/json'},body:'{"origen":"pc"}'})).json();
  await cargarSesiones(); seleccionar(d.id);
}
function seleccionar(sid){
  SID=sid; $('sin-sesion').style.display='none'; $('panel-sesion').style.display='';
  cargarSesiones();
  if(timer) clearInterval(timer);
  refrescar(); timer=setInterval(refrescar, 2500);
}
async function refrescar(){
  if(!SID) return;
  const d=await (await fetch(`/api/dev/sesion/${SID}`)).json();
  pintar(d);
}
function pintar(d){
  ultData=d;
  $('chip-estado').textContent=d.estado;
  $('btn-procesar').disabled = d.estado==='procesando' || !d.fotos.length;
  const auto = d.auto_ocr!==false;
  $('btn-auto').textContent = auto ? ' Auto-OCR: SÍ' : ' Auto-OCR: NO';
  $('btn-auto').className = auto ? 'primario' : '';
  $('progreso').textContent = d.estado==='procesando'
    ? `${d.progreso.msg} (${d.progreso.actual}/${d.progreso.total})` : (d.progreso.msg||'');
  // Reconstruir la grilla SOLO si las fotos cambiaron: recrear las <img> en cada
  // poll reinicia sus descargas y las miniaturas quedan cargadas a medias.
  const hf=SID+'|'+JSON.stringify(d.fotos);
  if(hf!==ultHashFotos){
    ultHashFotos=hf;
    const g=$('grid-fotos'); g.innerHTML='';
    d.fotos.forEach(f=>{
      const el=document.createElement('div'); el.className='foto';
      el.innerHTML=`<img src="/api/dev/sesion/${SID}/foto/${f.n}?ts=${f.ts}" loading="lazy"
          onclick="verFoto(${f.n})"><span>#${f.n} ${esc(f.origen)}</span>
        <button onclick="borrarFoto(${f.n})">✕</button>`;
      g.appendChild(el);
    });
  }
  pintarResultados(d);
  pintarRevision();
  pintarAdic();
  pintarHoja();
}
function verFoto(n){
  const v=$('visor-foto'); v.querySelector('img').src=`/api/dev/sesion/${SID}/foto/${n}`;
  v.style.display='flex';
}
async function borrarFoto(n){
  await fetch(`/api/dev/sesion/${SID}/foto/${n}`,{method:'DELETE'}); refrescar();
}
async function toggleAuto(){
  const auto = ultData && ultData.auto_ocr!==false;
  await fetch(`/api/dev/sesion/${SID}/auto`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({valor:!auto})});
  refrescar();
}
let _tBuscar=null, FOTO_PENDIENTE=null;
function buscarHojas(q){
  clearTimeout(_tBuscar);
  const R=$('res-buscar');
  if(!q || q.trim().length<3){ R.innerHTML=''; return; }
  _tBuscar=setTimeout(async()=>{
    const rs=await (await fetch('/api/dev/buscar?q='+encodeURIComponent(q.trim()))).json();
    if(!rs.length){ R.innerHTML='<div class="ses" style="color:#94a3b8">Sin resultados en el último año</div>'; return; }
    R.innerHTML=rs.map(r=>{
      const est=r.duplicada?'duplicada':r.continuacion?'continuación':
        (r.ban_dev?('↩ '+esc(r.ban_dev)+(r.aprobada?' ✓':' (para aprobar)')):(r.n_dev+' dev.'));
      return `<div class="ses" style="border-color:#1e3a5f" onclick="abrirHoja('${esc(r.sesion)}',${r.foto_n})">
        <b><span style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;background:${({verde:'#4ade80',amarillo:'#facc15',rojo:'#f87171'})[r.semaforo]||'#64748b'}"></span>${esc(r.ban||'sin BAN')} · ${esc(r.pro||'')}</b>
        <small>${esc(r.nodo||'')}${r.lider?(' · '+esc(r.lider)):''}</small>
        <small>${esc(r.sesion)} · foto #${r.foto_n}${r.creada_por?(' · '+esc(r.creada_por)):''} · ${est}${r.validada?(' · ✓ '+esc(r.validada)):''}</small>
      </div>`;}).join('');
  },300);
}
function abrirHoja(sid,n){
  FOTO_PENDIENTE=n;
  if(SID!==sid){ seleccionar(sid); } else { irAFoto(n); }
  history.replaceState(null,'','#'+sid+'/'+n);
}
function irAFoto(n){
  const el=document.getElementById('hoja-'+n);
  if(!el) return false;
  el.scrollIntoView({behavior:'smooth',block:'start'});
  el.style.outline='3px solid #38bdf8'; setTimeout(()=>{el.style.outline='';},2500);
  FOTO_PENDIENTE=null; return true;
}
async function procesar(){
  const r=await fetch(`/api/dev/sesion/${SID}/procesar`,{method:'POST'});
  if(!r.ok){ const e=await r.json(); alert(e.error||'error'); }
  refrescar();
}
async function reprocesarSesion(){
  if(!confirm('¿Borrar el OCR de TODAS las hojas de esta sesión para reprocesarlas desde cero?\n\n'+
      '· Las fotos NO se tocan.\n'+
      '· Las devoluciones/adicionales YA EJECUTADOS en SGS se conservan (anti doble-escritura).\n'+
      '· Las correcciones manuales sobre el OCR viejo SÍ se pierden.\n\n'+
   'Con Auto-OCR encendido el reproceso arranca solo; si no, pulsa «Procesar».')) return;
  const r=await fetch(`/api/dev/sesion/${SID}/reprocesar`,{method:'POST'});
  if(!r.ok){ const e=await r.json().catch(()=>({})); alert(e.error||'error'); }
  refrescar();
}
async function cerrarSesion(){
  await fetch(`/api/dev/sesion/${SID}/cerrar`,{method:'POST'});
  cargarSesiones(); refrescar();
}

/* ── Corrección humana de la tabla ── */
async function _postEdicion(hi, body){
  ultHashRes='';
  const r=await fetch(`/api/dev/sesion/${SID}/hoja/${hi}/editar`,{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  if(!r.ok){ const e=await r.json().catch(()=>({})); alert(e.error||'Error al guardar'); }
  refrescar();
}
function editar(hi,mi,campo,valor){ _postEdicion(hi,{mat:mi,campo,valor}); }
function confirmar(hi,mi,v){ _postEdicion(hi,{mat:mi,campo:'confirmado',valor:v}); }
async function validarHoja(hi,v){
  const r=await fetch(`/api/dev/sesion/${SID}/hoja/${hi}/validar`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({valor:v})});
  if(!r.ok){ const e=await r.json().catch(()=>({})); alert(e.error||'error'); }
  refrescar();
}
function editarCabecera(hi,campo,valor){ _postEdicion(hi,{campo,valor}); }
async function reverificarTodo(){
  const b=$('btn-revtodo'); b.disabled=true; b.textContent='↻ Reverificando...';
  try{
    const r=await fetch(`/api/dev/sesion/${SID}/reverificar-todo`,{method:'POST'});
    const j=await r.json();
    if(!r.ok){ alert(j.error||'error'); }
    else alert(`Reverificación completa: ${j.verdes}/${j.hojas} hojas en verde.`);
  }finally{ b.disabled=false; b.textContent='↻ Reverificar todo'; ultHashRes=''; refrescar(); }
}
async function reverificar(hi){
  ultHashRes='';
  const r=await fetch(`/api/dev/sesion/${SID}/hoja/${hi}/reverificar`,{method:'POST'});
  if(!r.ok){ const e=await r.json().catch(()=>({})); alert(e.error||'Error al reverificar'); }
  refrescar();
}

let ultHashRes='';
function pintarResultados(d){
  const R=$('resultados');
  // no repintar SOLO mientras el usuario escribe en un campo de texto
  // (un botón con foco tras el clic NO debe congelar la tabla)
  const act=document.activeElement;
  if(act && act.closest && act.closest('#resultados')
     && (act.tagName==='INPUT' || act.tagName==='TEXTAREA')) return;
  const hash=JSON.stringify([d.hojas, d.dev_job, d.aprob_job, d.adic_job, d.adic_todos]);
  if(hash===ultHashRes) return;
  ultHashRes=hash;
  if(!d.hojas.length){ R.innerHTML=''; return; }
  let html='';

  // Reporte del job de devoluciones (TEST o REAL)
  if(d.dev_job){
    const j=d.dev_job;
    const cl=j.estado==='corriendo'?'amarillo':j.estado==='error'?'rojo':'verde';
    html+=`<div class="hoja"><h3>↩ Devoluciones en SGS
      <span class="chip ${cl}">${esc(j.estado==='corriendo'?'EN CURSO':(j.estado==='error'?'INTERRUMPIDO':(j.dry_run?'TEST':'REAL')))}</span></h3>`;
    if(j.total){
      const pct=j.pct||0;
      html+=`<div class="barra"><div style="width:${pct}%"></div></div>
        <div class="meta"><b>${pct}%</b> — ${j.hechas||0}/${j.total} hojas · ${esc(j.msg||'')}</div>`;
    }else{
      html+=`<div class="meta">${esc(j.msg||'')}</div>`;
    }
    if(j.resultados&&j.resultados.length){
      html+=`<table><tr><th></th><th>Hoja</th><th>Resultado</th><th>Bandeja dev.</th>
        <th>Estado SGS</th><th>SGS</th><th>Detalle</th></tr>`;
      j.resultados.forEach(r=>{
        const ok=r.status==='ok'||r.status==='dry_run_ok'||r.status==='ya_ejecutada';
        const cl2=ok?'verde':(r.status==='error'?'rojo':'amarillo');
        const txt=r.status==='ok'?(r.aprobada?'✓ creada y APROBADA':'✓ creada'):
          r.status==='dry_run_ok'?'prueba OK':
          r.status==='ya_ejecutada'?'↩ ya ejecutada antes':r.status==='omitida'?'omitida':
          r.status==='duplicada'?'foto duplicada':
          r.status==='sin_ejecutables'?'— sin saldo':'✗ error';
        html+=`<tr class="${cl2}"><td><span class="punto ${cl2}"></span></td>
          <td><a class="lnk-hoja" onclick="verHoja(${r.hoja})" title="Ver la hoja escaneada con zoom">${esc(r.ban_origen)}</a></td>
          <td>${txt} (${r.intentos||0} int.)</td>
          <td><b>${esc(r.ban_dev||'—')}</b></td><td>${esc(r.estado_sgs||'—')}</td>
          <td>${r.url_sgs?('<a href="'+esc(r.url_sgs)+'" target="_blank">NODO</a>'):'—'}</td>
          <td style="color:#94a3b8">${esc(r.error||'')} ${(r.advertencias||[]).map(esc).join(' · ')}</td></tr>`;
      });
      html+='</table>';
      if(j.estado!=='corriendo'&&j.resultados.some(r=>r.status==='error'))
        html+=`<div class="meta" style="margin-top:6px">Para reintentar los fallos: corrige la causa
          y vuelve a pulsar el mismo botón — las hojas ya ejecutadas no se repiten.
          El botón «Informe técnico» arma el informe técnico para soporte.</div>`;
    }
    html+='</div>';
  }
  if(d.adic_job && d.adic_job.estado){
    const j=d.adic_job;
    const cl=j.estado==='corriendo'?'amarillo':j.estado==='error'?'rojo':'verde';
    html+=`<div class="hoja"><h3>+ Despachos adicionales
      <span class="chip ${cl}">${esc(j.estado==='corriendo'?'EN CURSO':(j.estado==='error'?'INTERRUMPIDO':(j.dry_run?'TEST':'REAL')))}</span></h3>`;
    if(j.total){
      html+=`<div class="barra"><div style="width:${j.pct||0}%"></div></div>
        <div class="meta"><b>${j.pct||0}%</b> — ${j.hechas||0}/${j.total} · ${esc(j.msg||'')}</div>`;
    }else html+=`<div class="meta">${esc(j.msg||'')}</div>`;
    if(j.resultados&&j.resultados.length){
      html+=`<table><tr><th></th><th>Hoja</th><th>Resultado</th><th>Bandeja</th>
        <th>Estado</th><th>SGS</th><th>Detalle</th></tr>`;
      j.resultados.forEach(r=>{
        const ok=r.status==='ok'||r.status==='dry_run_ok'||r.status==='ya_ejecutada';
        const cl2=ok?'verde':(r.status==='error'?'rojo':'amarillo');
        const txt=r.status==='ok'?(r.aprobada?'✓ creado y APROBADO':'✓ creado'):
          r.status==='dry_run_ok'?'prueba OK':r.status==='ya_ejecutada'?'+ ya ejecutado':
          r.status==='duplicada'?'foto duplicada':
          r.status==='omitida'?'omitida':r.status==='sin_ejecutables'?'— nada ejecutable':'✗ error';
        html+=`<tr class="${cl2}"><td><span class="punto ${cl2}"></span></td>
          <td><a class="lnk-hoja" onclick="verHoja(${r.hoja})" title="Ver la hoja escaneada con zoom">${esc(r.ban_origen)}</a></td>
          <td>${txt}</td>
          <td><b>${esc(r.ban_dev||'—')}</b></td><td>${esc(r.estado_sgs||'—')}</td>
          <td>${r.url_sgs?('<a href="'+esc(r.url_sgs)+'" target="_blank">NODO</a>'):'—'}</td>
          <td style="color:#94a3b8">${esc(r.error||'')} ${(r.advertencias||[]).map(esc).join(' · ')}</td></tr>`;
      });
      html+='</table>';
    }
    html+='</div>';
  }
  if(d.aprob_job && d.aprob_job.estado){
    const a=d.aprob_job;
    const cl=a.estado==='corriendo'?'amarillo':a.estado==='error'?'rojo':'verde';
    html+=`<div class="hoja"><h3>✓ Aprobación de bandejas
      <span class="chip ${cl}">${esc(a.estado==='corriendo'?'EN CURSO':'LISTO')}</span></h3>`;
    if(a.estado==='corriendo'&&a.total)
      html+=`<div class="barra"><div style="width:${a.pct||0}%"></div></div>`;
    html+=`<div class="meta">${esc(a.msg||'')}</div></div>`;
  }

  // Resumen global de devoluciones detectadas
  const devs=[];
  d.hojas.forEach((h,hi)=>{
    if(!h.ocr||!h.ocr.materiales) return;
    if(h.duplicada_de) return;   // foto repetida: no contar doble
    if(h.continuacion_de) return; // página 2: sus filas ya están en la principal
    h.ocr.materiales.forEach(m=>{
      if((m.devolucion||0)>0 || m.color!=='verde')
        devs.push({hi, ban:h.ocr.ban_code, lider:(h.verificacion&&h.verificacion.lider_sgs)||
          (h.ocr.asignada_a&&h.ocr.asignada_a[0])||'?', m});
    });
  });
  const conDev=devs.filter(x=>(x.m.devolucion||0)>0);
  if(conDev.length){
    html+=`<div id="resumen"><h3>↩ Devoluciones detectadas (${conDev.length})</h3>
      <table><tr><th></th><th>Bandeja</th><th>Líder (SGS)</th><th>Material</th>
      <th>Entreg.</th><th>Devolución </th></tr>`;
    conDev.forEach(x=>{
      html+=`<tr class="${x.m.color}"><td><span class="punto ${x.m.color}"></span></td>
        <td><a class="lnk-hoja" onclick="verHoja(${x.hi})" title="Ver la hoja escaneada con zoom">${esc(x.ban)}</a></td>
        <td>${esc(x.lider)}</td>
        <td>${etq(x.m)}</td>
        <td class="num">${x.m.entregado??'—'}</td>
        <td class="num dev">${x.m.devolucion??'?'}</td></tr>`;
    });
    html+='</table></div>';
  }
  const adh=ROL==='admin'?adicHojas(d):[];
  if(adh.length){
    const c={}; adh.forEach(x=>{c[x.est]=(c[x.est]||0)+1;});
    html+=`<div class="hoja" style="border-color:#6b21a8;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <h3 style="margin:0">+ Adicionales: ${adh.length} hoja${adh.length===1?'':'s'}</h3>
      ${['creada','lista','resolver','sin_cruzar','sin_nodo','no_aut'].filter(e=>c[e])
        .map(e=>`<span class="chip ${ADIC_EST[e][0]}">${ADIC_EST[e][1]}: ${c[e]}</span>`).join(' ')}
      <span class="chip ${d.adic_todos?'verde':'amarillo'}">${d.adic_todos?'todos los supervisores':'solo los supervisores autorizados'}</span>
      <span style="flex:1"></span>
      <button class="morado" onclick="abrirAdic()">Abrir panel +</button></div>`;
  }

  d.hojas.forEach((h,hi)=>{
    if(h.error && !h.ocr){
      html+=`<div class="hoja"><h3>Foto #${h.foto_n}
        <span class="chip rojo">ERROR</span></h3>
        <div class="msgs"><div>${esc(h.error)}</div></div></div>`;
      return;
    }
    if(h.continuacion_de){
      const oc=h.ocr||{};
      html+=`<div class="hoja"><h3>Foto #${h.foto_n}
        <span class="chip azul">Página 2 de ${esc(h.continuacion_ban||('foto #'+h.continuacion_de))} (foto #${h.continuacion_de})</span>
        <a class="lnk-hoja" onclick="verHoja(${hi})" title="Ver esta foto con zoom">ver foto</a></h3>
        <div class="meta">Hoja de continuación sin cabecera: sus ${(oc.materiales||[]).length} fila(s)
        y ${(oc.notas_manuscritas||[]).length} nota(s) del pie están integradas en la hoja de
        <b>${esc(h.continuacion_ban||'')}</b> — revísalas y edítalas allá.</div></div>`;
      return;
    }
    const o=h.ocr||{}, v=h.verificacion||{};
    const estadoV=v.estado||'rojo';
    const tituloV = !v.disponible ? 'SGS no disponible' :
      estadoV==='verde' ? '✓ BAN→PRO→Líder verificado en SGS' :
      estadoV==='amarillo' ? '⚠ Verificación con dudas' : '✗ Verificación FALLÓ';
    html+=`<div class="hoja" id="hoja-${h.foto_n}">
      <h3>Foto #${h.foto_n} —
        <input class="edit-head" value="${esc(o.ban_code||'')}" placeholder="BAN-######"
          onchange="editarCabecera(${hi},'ban_code',this.value)">
        <input class="edit-head" value="${esc(o.pro_code||'')}" placeholder="PRO-######"
          onchange="editarCabecera(${hi},'pro_code',this.value)">
        <button class="btn-sgs" onclick="reverificar(${hi})"
          title="Volver a verificar BAN/PRO/líder/materiales en SGS (solo lectura)">↻ SGS</button>
        <a class="lnk-hoja" onclick="verHoja(${hi})" title="Abre la foto de esta hoja con zoom, al lado de la tabla">Ver hoja escaneada</a>
        ${v.url_sgs?('<a class="lnk-sgs" href="'+esc(v.url_sgs)+'" target="_blank" title="Abrir el NODO en SGS">Ver en SGS</a>'):''}
        ${h.devolucion_ejecutada?('<span class="chip '+(h.devolucion_ejecutada.aprobada?'verde':'amarillo')+'">↩ '+esc(h.devolucion_ejecutada.ban_dev||'')+(h.devolucion_ejecutada.aprobada?' APROBADA':' para aprobar')+'</span>'):''}
        ${h.duplicada_de?('<span class="chip rojo">DUPLICADA de foto #'+h.duplicada_de+' — borra esta foto</span>'):''}
        ${h.paginas_extra?('<span class="chip azul" title="Bandeja impresa en varias hojas: las filas de esas fotos están integradas en esta tabla">'+h.paginas_extra.length+' pág. extra (foto '+h.paginas_extra.map(n=>'#'+n).join(', ')+')</span>'):''}
        ${h.adicional_ejecutado?('<span class="chip '+(h.adicional_ejecutado.aprobada?'verde':'amarillo')+'">+ '+esc(h.adicional_ejecutado.ban||'')+(h.adicional_ejecutado.aprobada?' APROBADO':'')+'</span>'):''}
        ${h.validada?('<span class="chip verde" title="Hoja contada y revisada por '+esc(h.validada.por)+' el '+esc((h.validada.ts||'').replace('T',' ').slice(0,16))+(h.validada.rojos?' · quedaron '+h.validada.rojos+' rojos':'')+'">✓ Validada · '+esc(h.validada.por)+'</span><button class="btn-sgs" onclick="validarHoja('+hi+',false)" title="Quitar la validación">↩</button>'):('<button class="btn-sgs" onclick="validarHoja('+hi+',true)" title="Marca la hoja como contada y revisada (queda quién y cuándo)">✓ Validar hoja</button>')}
        <span class="chip ${estadoV}">${tituloV}</span>
        <span class="chip ${o.confianza_global||'baja'}" title="${esc((o._uso&&o._uso.modelo)||'')}">OCR: ${esc(o.confianza_global||'?')}${o._uso&&o._uso.backend?(' · '+esc(o._uso.backend)):''}</span>
      </h3>
      <div class="meta">${esc(o.proyecto_desc||'')} · Hoja: ${esc((o.asignada_a||[]).join(' / '))}
        ${v.lider_sgs?(' · Líder SGS: <b>'+esc(v.lider_sgs)+'</b>'):''}
        ${o.consecutivo_naranja?(' · '+esc(o.consecutivo_naranja)):''}</div>`;
    const msgs=[...(v.mensajes||[]), ...((o.advertencias||[]))];
    if(h.error) msgs.unshift(h.error);
    if(msgs.length) html+=`<div class="msgs">${msgs.map(m=>`<div>• ${esc(m)}</div>`).join('')}</div>`;
    if(o.materiales&&o.materiales.length){
      html+=`<table><tr><th></th><th>Material</th><th>Solic.</th><th>Entreg.</th>
        <th>Manuscrito </th><th>DEVOLUCIÓN</th><th>Consumo≈</th><th>En BAN</th>
        <th title="Confirmar fila revisada">OK</th><th>Nota</th></tr>`;
      o.materiales.forEach((m,mi)=>{
        const ed = m.fuente==='humano';
        html+=`<tr class="${m.color}">
          <td><span class="punto ${m.color}"></span>${ed?'<small title="Corregido a mano"></small>':(m.regla?'<small title="Valor propuesto por regla del sistema (patrón de tus correcciones anteriores): confírmalo con ✓ o corrígelo">⚙</small>':'')}</td>
          <td>${m.de_foto?('<small style="color:#7dd3fc" title="Fila leída en la página de continuación (foto #'+m.de_foto+')">p'+m.de_foto+'</small> '):''}${etq(m)}</td>
          <td class="num">${m.solicitado??'—'}</td>
          <td class="num"><input class="edit${ed?' editado':''}" value="${m.entregado??''}"
              onchange="editar(${hi},${mi},'entregado',this.value)"></td>
          <td class="num">${m.devolucion_manuscrita??'—'}${m.devolucion_texto?(' <small style="color:#94a3b8">('+esc(m.devolucion_texto)+')</small>'):''}</td>
          <td class="num dev"><input class="edit${ed?' editado':''}" value="${m.devolucion??''}"
              onchange="editar(${hi},${mi},'devolucion',this.value)"></td>
          <td class="num" style="color:#94a3b8">${m.consumido_calc??'—'}</td>
          <td>${m.en_bandeja===true?'✓':m.en_bandeja===false?'✗':'—'}</td>
          <td><button class="btn-conf${m.confirmado?' on':''}"
              onclick="confirmar(${hi},${mi},${m.confirmado?'false':'true'})">✓</button></td>
          <td style="color:#94a3b8">${esc(m.nota||'')}</td></tr>`;
      });
      html+='</table>';
    }
    if(o.notas_manuscritas&&o.notas_manuscritas.length){
      html+=`<div class="meta" style="margin-top:8px"> Notas al pie: `+
        o.notas_manuscritas.map(n=>`"${esc(n.texto)}"${n.interpretacion?(' → '+esc(n.interpretacion)):''}`).join(' · ')+
        `</div>`;
    }
    if(h.preanalisis){
      const p=h.preanalisis;
      html+=`<div class="prean"><h4>Preanálisis de devolución
        <span class="chip ${p.bloqueados?'amarillo':'verde'}">${p.ejecutables} ejecutables · ${p.bloqueados} bloqueados</span></h4>
        <div class="meta">Plan: Equipo <b>${esc(p.equipo||'?')}</b> · Fecha <b>${esc(p.fecha)}</b> · Almacén <b>${esc(p.almacen)}</b></div>`;
      (p.mensajes||[]).forEach(x=>{ html+=`<div class="msgs"><div>• ${esc(x)}</div></div>`; });
      (p.bandejas_dev_previas||[]).forEach(b=>{
        html+=`<div class="meta">↩ Devolución previa: <b>${esc(b.name)}</b> (${esc(b.fecha)} · ${esc(b.partner)}) — ${b.materiales.length} materiales</div>`;
      });
      if(p.materiales&&p.materiales.length){
        html+=`<table><tr><th></th><th>Material</th><th>Hoja pide</th><th>Despach.</th>
          <th>Ya dev.</th><th>Saldo</th><th>EJECUTABLE</th><th>Detalle</th></tr>`;
        p.materiales.forEach(i=>{
          const cl=i.estado==='ok'?'verde':i.estado==='parcial'?'amarillo':'rojo';
          html+=`<tr class="${cl}"><td><span class="punto ${cl}"></span></td>
            <td>${etq(i)}</td>
            <td class="num">${i.devolucion}</td><td class="num">${i.despachado??'—'}</td>
            <td class="num">${i.devuelto??'—'}</td><td class="num">${i.saldo??'—'}</td>
            <td class="num dev">${i.devolucion_ejecutable??''}</td>
            <td style="color:#94a3b8">${esc(i.detalle||'')}</td></tr>`;
        });
        html+='</table>';
      }
      if(p.adicionales){
        const a=p.adicionales;
        html+=`<div class="meta" style="margin-top:10px">+ ${a.items.length} adicional${a.items.length===1?'':'es'} pedido${a.items.length===1?'':'s'} en esta hoja
          · ${a.ejecutables} listo${a.ejecutables===1?'':'s'}${a.por_resolver?(' · '+a.por_resolver+' por resolver'):''}
          ${a.autorizado?'':' · supervisor no autorizado'}
          · <a class="lnk-hoja" onclick="abrirAdic(${hi})">abrir en el panel +</a></div>`;
      }
      html+='</div>';
    }
    if(h.bandeja_creada){
      const b=h.bandeja_creada;
      html+=`<div class="prean"><h4>Bandeja creada: ${esc(b.ban||'')}
        <span class="chip ${b.ok?'verde':'rojo'}">${b.ok?'✓ CUADRA CON EL PLAN':'✗ NO CUADRA'}</span>
        <span class="chip ${(b.estado==='approved')?'verde':'amarillo'}">${esc(b.estado==='approved'?'APROBADA':(b.estado||'?'))}</span></h4>
        <div class="meta">Equipo: <b>${esc(b.equipo||'?')}</b> · Fecha: ${esc(b.fecha||'?')}</div>`;
      (b.checks||[]).forEach(x=>{ html+=`<div class="msgs"><div>• ${esc(x)}</div></div>`; });
      if(b.materiales&&b.materiales.length){
        html+=`<table><tr><th></th><th>Material en la bandeja</th><th>Cantidad</th><th>Detalle</th></tr>`;
        b.materiales.forEach(m=>{
          const cl=m.ok?'verde':'rojo';
          html+=`<tr class="${cl}"><td><span class="punto ${cl}"></span></td>
            <td>${esc(m.producto)}</td><td class="num dev">${m.cantidad}</td>
            <td style="color:#94a3b8">${esc(m.detalle||'')}</td></tr>`;
        });
        html+='</table>';
      }
      html+='</div>';
    }
    html+='</div>';
  });
  R.innerHTML=html;
  if(FOTO_PENDIENTE!=null) setTimeout(()=>irAFoto(FOTO_PENDIENTE),150);
}

/* ── Popup de verificación hoja-vs-sistema (foto con lupa + lecturas) ── */
let hojaAbierta=null;
function verHoja(hi){
  hojaAbierta=hi;
  document.getElementById('modal-hoja').style.display='flex';
  pintarHoja();
}
function cerrarHoja(){
  hojaAbierta=null;
  document.getElementById('modal-hoja').style.display='none';
}
function pintarHoja(){
  if(hojaAbierta===null || !ultData) return;
  const h=(ultData.hojas||[])[hojaAbierta]; if(!h) return;
  const o=h.ocr||{}, v=h.verificacion||{}, p=h.preanalisis||{};
  const img=document.getElementById('mh-img');
  const src=`/api/dev/sesion/${SID}/foto/${h.foto_n}`;
  if(!img.src.endsWith(src)) img.src=src;
  document.getElementById('mh-titulo').innerHTML=
    `${esc(o.ban_code||h.continuacion_ban||'?')} · <span style="color:#fbbf24">${esc(o.pro_code||'?')}</span>`;
  let html=`<div class="meta">${esc(o.proyecto_desc||'')}<br>
    Líder: <b>${esc(v.lider_sgs||(o.asignada_a||[])[0]||'?')}</b>
    ${o.consecutivo_naranja?(' · '+esc(o.consecutivo_naranja)):''}
    · foto #${h.foto_n}
    ${h.continuacion_de?(' · <span style="color:#7dd3fc">página 2 de la foto #'+h.continuacion_de+'</span>'):''}</div>`;
  const devs=(o.materiales||[]).filter(m=>(m.devolucion||0)>0);
  if(devs.length){
    html+=`<h4>↩ Devoluciones leídas (${devs.length})</h4>
      <table><tr><th></th><th>Material</th><th>Ent.</th><th></th><th>DEV</th></tr>`;
    devs.forEach(m=>{
      html+=`<tr class="${m.color}"><td><span class="punto ${m.color}"></span></td>
        <td>${etq(m)}</td>
        <td class="num">${m.entregado??'—'}</td>
        <td class="num">${m.devolucion_manuscrita??'—'}</td>
        <td class="num dev">${m.devolucion}</td></tr>`;
    });
    html+='</table>';
  }else{
    html+=`<div class="meta">Sin devoluciones &gt; 0 leídas en esta hoja.</div>`;
  }
  if(p.adicionales){
    const a=p.adicionales;
    html+=`<h4>+ Adicionales pedidos
      <span class="chip ${a.autorizado?'verde':'rojo'}" style="font-size:11px">
      ${a.autorizado?'✓ autorizado':'✗ '+esc(a.supervisor||'?')}</span></h4>
      <table><tr><th></th><th>Nota</th><th>Pide</th><th>Proyectado</th></tr>`;
    a.items.forEach(i=>{
      const cl=i.estado==='ok'?(a.autorizado?'verde':'amarillo'):'rojo';
      html+=`<tr class="${cl}"><td><span class="punto ${cl}"></span></td>
        <td>"${esc(i.texto)}"</td><td class="num dev">${i.cantidad??'?'}</td>
        <td class="num">${i.proyectado_actual??'—'} → <b>${i.proyectado_nuevo??'—'}</b></td></tr>`;
    });
    html+='</table>';
  }
  const notasDev=(o.notas_manuscritas||[]).filter(n=>(n.tipo||'devolucion')!=='adicional');
  if(notasDev.length){
    html+=`<h4> Otras notas</h4>`+notasDev.map(n=>
      `<div class="meta">"${esc(n.texto)}"${n.interpretacion?(' → '+esc(n.interpretacion)):''}</div>`).join('');
  }
  document.getElementById('mh-datos-body').innerHTML=html;
}
// Lupa: el zoom sigue el cursor sobre la foto
(function(){
  const lupa=document.getElementById('mh-lupa'), img=document.getElementById('mh-img');
  lupa.addEventListener('mousemove', e=>{
    const r=img.getBoundingClientRect();
    const x=Math.max(0,Math.min(100,(e.clientX-r.left)/r.width*100));
    const y=Math.max(0,Math.min(100,(e.clientY-r.top)/r.height*100));
    img.style.transformOrigin=`${x.toFixed(1)}% ${y.toFixed(1)}%`;
  });
  lupa.addEventListener('mouseenter', ()=>img.classList.add('zoom'));
  lupa.addEventListener('mouseleave', ()=>img.classList.remove('zoom'));
})();

/* ── Popup de revisión de devoluciones (comparación visual con la hoja física) ── */
let revisionAbierta=false, ultData=null;
function abrirRevision(){
  revisionAbierta=true;
  document.getElementById('modal-rev').style.display='flex';
  pintarRevision();
  setTimeout(()=>document.getElementById('rev-buscar').focus(), 100);
  // refrescar desde SGS en segundo plano (job con progreso)
  fetch(`/api/dev/sesion/${SID}/verificar-devoluciones`,{method:'POST'}).catch(()=>{});
  ultHashRes=''; refrescar();
}
function cerrarRevision(){
  revisionAbierta=false;
  document.getElementById('modal-rev').style.display='none';
}
function pintarRevision(){
  if(!revisionAbierta || !ultData) return;
  const d=ultData, v=d.verif_job||{};
  let html='';
  if(v.estado==='corriendo'){
    html+=`<div class="barra"><div style="width:${v.pct||0}%"></div></div>
      <div class="meta" style="margin-bottom:10px">${esc(v.msg||'')} — puedes ir revisando las ya cargadas</div>`;
  }else if(v.msg){
    html+=`<div class="meta" style="margin-bottom:10px">${esc(v.msg)}</div>`;
  }
  // Hojas con devolución ejecutada, adicional ejecutado o adicional planificado
  let hojas=(d.hojas||[]).filter(h=>h.devolucion_ejecutada || h.adicional_ejecutado ||
      (((h.preanalisis||{}).adicionales||{}).ejecutables||0)>0);
  const total=hojas.length;
  // Buscador laxo: BAN despacho / PRO / BAN devolución / BAN adicional / líder
  const q=(document.getElementById('rev-buscar').value||'')
      .toUpperCase().replace(/[^A-Z0-9]/g,'');
  if(q){
    hojas=hojas.filter(h=>{
      const o=h.ocr||{}, de=h.devolucion_ejecutada||{}, b=h.bandeja_creada||{};
      const ae=h.adicional_ejecutado||{};
      const pajar=((o.ban_code||'')+' '+(o.pro_code||'')+' '+(de.ban_dev||'')+' '+
        (ae.ban||'')+' '+(b.equipo||'')+' '+((h.verificacion||{}).lider_sgs||''))
        .toUpperCase().replace(/[^A-Z0-9]/g,'');
      return pajar.includes(q);
    });
    html+=`<div class="meta" style="margin-bottom:8px">${hojas.length} de ${total} coinciden con "${esc(document.getElementById('rev-buscar').value)}"</div>`;
  }
  if(!hojas.length){
    html+= q ? '<div class="vacio">Sin coincidencias — prueba con otro pedazo del código.</div>'
             : '<div class="vacio">No hay devoluciones ni adicionales creados todavía.</div>';
  }
  hojas.forEach(h=>{
    const o=h.ocr||{}, de=h.devolucion_ejecutada||{}, b=h.bandeja_creada||{};
    const ae=h.adicional_ejecutado||{};
    const ad=(h.preanalisis||{}).adicionales||null;
    const url=(h.preanalisis||{}).url_sgs||(h.verificacion||{}).url_sgs;
    html+=`<div class="rev-card${b.ok===false?' mal':''}">
      <div class="rev-sub" style="margin-bottom:8px">Hoja <b>${esc(o.ban_code||'?')}</b> ·
        NODO <b class="pro">${esc(o.pro_code||'?')}</b>
        · Líder <b>${esc(b.equipo||(h.verificacion||{}).lider_sgs||(o.asignada_a||[])[0]||'?')}</b>
        ${url?(' · <a class="lnk-sgs" target="_blank" href="'+esc(url)+'">Ver NODO en SGS</a>'):''}</div>`;

    // ↩ DEVOLUCIÓN (arriba)
    if(de.ban_dev || b.ban){
      const aprobada = de.aprobada || b.estado==='approved';
      html+=`<div class="rev-head">
        <span class="rev-tag dev">↩ DEVOLUCIÓN</span>
        <span class="ban-grande">${esc(de.ban_dev||b.ban)}</span>
        <span class="chip ${aprobada?'verde':'amarillo'}">${aprobada?'APROBADA':'PARA APROBAR'}</span>
        ${b.ok===true?'<span class="chip verde">✓ cuadra</span>':
          b.ok===false?'<span class="chip rojo">✗ REVISAR</span>':''}
        <span class="meta">Fecha ${esc(b.fecha||'')}</span>
      </div>`;
      if(b.materiales&&b.materiales.length){
        html+=`<table><tr><th>Material devuelto</th><th>Cant.</th><th></th></tr>`;
        b.materiales.forEach(m=>{
          html+=`<tr class="${m.ok?'verde':'rojo'}"><td>${esc(m.producto)}</td>
            <td class="num dev">${m.cantidad}</td>
            <td>${m.ok?'✓':esc(m.detalle||'✗')}</td></tr>`;
        });
        html+='</table>';
      }else{
        html+='<div class="meta">Cargando materiales de SGS…</div>';
      }
      (b.checks||[]).filter(c=>!c.startsWith('✓')).forEach(c=>{
        html+=`<div class="msgs"><div>• ${esc(c)}</div></div>`;
      });
    }

    // + ADICIONAL (debajo — mismo PRO: ambas BAN se anotan en la misma hoja)
    if(ae.ban || (ad && (ad.ejecutables||0)>0)){
      html+=`<div class="rev-head" style="margin-top:10px">
        <span class="rev-tag adi">+ ADICIONAL</span>
        <span class="ban-grande adi">${esc(ae.ban||'(sin crear aún)')}</span>
        ${ae.ban?('<span class="chip '+(ae.aprobada?'verde':'amarillo')+'">'+
          (ae.aprobada?'APROBADO':'PARA APROBAR')+'</span>')
          :'<span class="chip amarillo">pendiente — panel + Adicionales</span>'}
      </div>`;
      if(ad && ad.items){
        const oks=ad.items.filter(i=>i.estado==='ok');
        if(oks.length){
          html+=`<table><tr><th>Material adicional</th><th>Pide</th><th>Proyectado</th></tr>`;
          oks.forEach(i=>{
            html+=`<tr class="verde"><td>${esc(i.producto||i.ref)}</td>
              <td class="num dev">${i.cantidad}</td>
              <td class="num">${i.proyectado_actual??'—'} → <b>${i.proyectado_nuevo??'—'}</b></td></tr>`;
          });
          html+='</table>';
        }
      }
    }
    html+='</div>';
  });
  document.getElementById('rev-body').innerHTML=html;
}
async function aprobarBandejas(){
  if(!confirm('Aprueba en SGS las bandejas creadas cuya verificación CUADRÓ con el plan.\n¿Continuar?')) return;
  const r=await fetch(`/api/dev/sesion/${SID}/aprobar-bandejas`,{method:'POST'});
  if(!r.ok){ const e=await r.json().catch(()=>({})); alert(e.error||'error'); }
  ultHashRes=''; refrescar();
}
async function informeTecnico(){
  const r=await fetch(`/api/dev/sesion/${SID}/debug`);
  const j=await r.json();
  let md=`# Informe técnico Devoluciones — sesión ${j.sesion} (${j.generado})\n\n`;
  md+=`Estado: ${j.estado_sesion} · hojas: ${j.n_hojas} · ediciones humanas: ${j.ediciones_humanas} · lecciones: ${j.lecciones_aprendidas}\n\n`;
  md+=`## Errores y advertencias (${(j.errores_y_advertencias||[]).length})\n`;
  (j.errores_y_advertencias||[]).forEach(e=>{ md+=`- ${e}\n`; });
  if(j.dev_job) md+=`\n## dev_job\n\`\`\`json\n${JSON.stringify(j.dev_job,null,1)}\n\`\`\`\n`;
  if(j.aprob_job) md+=`\n## aprob_job\n\`\`\`json\n${JSON.stringify(j.aprob_job,null,1)}\n\`\`\`\n`;
  md+=`\n## Últimos logs del servidor\n\`\`\`\n${(j.logs_servidor||[]).join('\n')}\n\`\`\`\n`;
  try{ await fetch('/api/dev/diagnostico',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tipo:'errores',sid:SID,texto:md})}); }catch(e){}
  try{
    await navigator.clipboard.writeText(md);
    alert(`Informe guardado y copiado al portapapeles (${md.length} caracteres).\nQuedó guardado en el servidor (Registros de SGO7); cópialo para soporte si hace falta.`);
  }catch(e){
    const w=window.open('','_blank'); w.document.write('<pre>'+md.replace(/</g,'&lt;')+'</pre>');
  }
}

async function preanalisis(){
  const b=$('btn-prean'); b.disabled=true; b.textContent='Cruzando con SGS...';
  try{
    const r=await fetch(`/api/dev/sesion/${SID}/preanalisis`,{method:'POST'});
    const j=await r.json();
    if(!r.ok){ alert(j.error||'error'); }
    else alert(`Preanálisis listo: ${j.ejecutables} devoluciones ejecutables, ${j.bloqueados} bloqueadas (ver detalle en cada hoja).`);
  }finally{ b.disabled=false; b.textContent='Preanálisis SGS'; ultHashRes=''; refrescar(); }
}
async function aprender(){
  const b=$('btn-aprender'); b.disabled=true; b.textContent='Analizando...';
  try{
    const r=await fetch(`/api/dev/sesion/${SID}/aprender`,{method:'POST'});
    const j=await r.json();
    if(!r.ok){ alert(j.error||'error'); }
    else alert(`Aprendizaje listo (${j.correcciones_analizadas} correcciones analizadas).\n`+
      `Lecciones nuevas (${j.nuevas.length}):\n`+(j.nuevas.map(x=>'• '+x).join('\n')||'(ninguna nueva)')+
      `\n\nTotal acumulado: ${j.total}. Se aplicarán en el próximo OCR.`);
  }finally{ b.disabled=false; b.textContent='Aprender de correcciones'; }
}
/* ── + Panel de adicionales: UNA lista, en el orden de las fotos (= la pila de
   hojas físicas). Cada hoja dice en qué va: creado (BAN para anotar), listo,
   por resolver, sin cruzar con SGS. Se crea hoja por hoja o todas las listas. ── */
let adicAbierto=false, adicFiltro='todas', ultHashAdic='', adicCruzando=false;
const ADIC_EST={
  creada:['verde','✓ Creado'], lista:['verde','Listo para crear'],
  resolver:['amarillo','Por resolver'], sin_cruzar:['azul','… Sin cruzar con SGS'],
  sin_nodo:['rojo',' Sin NODO verificado'], no_aut:['rojo','Supervisor no autorizado'],
  nada:['azul','— Nada que crear']};
// Una nota "duda" merece decisión humana solo si parece código = cantidad
// (nombres, cédulas o "BAN-xxxx" sueltos no son adicionales)
function _dudaAdic(n){
  if(n.tipo!=='duda') return false;
  const t=String(n.texto||'');
  if(/BAN/i.test(t) && typeof n.cantidad!=='number') return false;
  return typeof n.cantidad==='number' || !!String(n.ref_interna||'').trim() || /=\s*\d/.test(t);
}
function adicHojas(d){
  const out=[];
  (d.hojas||[]).forEach((h,hi)=>{
    if(!h.ocr || h.duplicada_de || h.continuacion_de) return;
    const notas=(h.ocr.notas_manuscritas||[]).map((n,ni)=>({n,ni}));
    const adic=notas.filter(x=>x.n.tipo==='adicional');
    const dudas=notas.filter(x=>_dudaAdic(x.n));
    const quitadas=notas.filter(x=>x.n.tipo==='descartada');
    const ae=h.adicional_ejecutado||null, p=h.preanalisis||null;
    const a=(p&&p.adicionales)||null;
    if(!adic.length && !dudas.length && !ae && !a) return;
    let est;
    if((ae && ae.ban) || (a && a.otra_sesion)) est='creada';
    else if(!adic.length && !dudas.length) est='nada';
    else if(!p) est='sin_cruzar';
    else if(!p.nodo_id) est='sin_nodo';
    else if(!a) est=adic.length?'sin_cruzar':'resolver';
    else{
      const conoc=new Set(a.items.flatMap(i=>i.nota_idxs||[i.nota_idx]));
      if(adic.some(x=>!conoc.has(x.ni))) est='sin_cruzar';
      else if(!a.autorizado) est='no_aut';
      else if(dudas.length || a.items.some(i=>i.estado!=='ok')) est='resolver';
      else est='lista';
    }
    out.push({hi,h,est,adic,dudas,quitadas,ae,p,a});
  });
  return out;
}
function abrirAdic(hi){
  adicAbierto=true; ultHashAdic='';
  $('modal-adic').style.display='flex';
  pintarAdic();
  if(hi!==undefined) setTimeout(()=>{ const el=$('adic-'+hi);
    if(el){ el.scrollIntoView({behavior:'smooth',block:'start'});
      el.style.outline='3px solid #a855f7'; setTimeout(()=>{el.style.outline='';},2500); } },150);
  cruzarAdic(false);     // las hojas que nunca se cruzaron con SGS, solas
}
function cerrarAdic(){ adicAbierto=false; $('modal-adic').style.display='none'; }
function filtroAdic(f){ adicFiltro=f; ultHashAdic=''; pintarAdic(); }
async function cruzarAdic(forzar){
  if(adicCruzando || !ultData) return;
  const lista=adicHojas(ultData).filter(x=>x.est!=='creada' && (forzar || x.est==='sin_cruzar'));
  if(!lista.length) return;
  adicCruzando=true;
  try{
    for(let k=0;k<lista.length;k++){
      $('adic-prog').textContent=`↻ Cruzando con SGS ${k+1}/${lista.length}…`;
      try{ await fetch(`/api/dev/sesion/${SID}/hoja/${lista[k].hi}/plan`,{method:'POST'}); }catch(e){}
    }
  }finally{
    adicCruzando=false; $('adic-prog').textContent=''; ultHashAdic=''; ultHashRes='';
    await refrescar();
  }
}
async function toggleAdicTodos(){
  const v=!(ultData&&ultData.adic_todos);
  if(!confirm(v?'Esta sesión hará adicionales de TODOS los supervisores (sin la lista de autorizados).\n¿Continuar?'
               :'Esta sesión volverá a hacer adicionales SOLO de los supervisores autorizados.\n¿Continuar?')) return;
  const r=await fetch(`/api/dev/sesion/${SID}/adic-todos`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({valor:v})});
  if(!r.ok){ const e=await r.json().catch(()=>({})); alert(e.error||'error'); return; }
  await refrescar(); cruzarAdic(true);
}
async function editarNotaAdic(hi,ni,cambios){
  ultHashAdic=''; ultHashRes='';
  $('adic-prog').textContent='Guardando y cruzando con SGS…';
  const r=await fetch(`/api/dev/sesion/${SID}/hoja/${hi}/editar-nota`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({nota:ni,cambios})});
  $('adic-prog').textContent='';
  if(!r.ok){ const e=await r.json().catch(()=>({})); alert(e.error||'Error al guardar'); }
  refrescar();
}
function dudaEsAdic(hi,ni){
  const ref=($('dq-ref-'+hi+'-'+ni).value||'').trim(), cant=($('dq-cant-'+hi+'-'+ni).value||'').trim();
  const c={tipo:'adicional'};
  if(ref) c.ref_interna=ref;
  if(cant) c.cantidad=cant;
  editarNotaAdic(hi,ni,c);
}
async function agregarNotaAdic(hi){
  const ref=($('aa-ref-'+hi).value||'').trim(), cant=($('aa-cant-'+hi).value||'').trim();
  if(!ref||!cant){ alert('Escribe el código y la cantidad'); return; }
  ultHashAdic=''; ultHashRes='';
  $('adic-prog').textContent='Agregando y cruzando con SGS…';
  const r=await fetch(`/api/dev/sesion/${SID}/hoja/${hi}/agregar-nota`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({ref_interna:ref,cantidad:cant})});
  $('adic-prog').textContent='';
  if(!r.ok){ const e=await r.json().catch(()=>({})); alert(e.error||'Error al agregar'); }
  refrescar();
}
async function crearAdic(dry,hojas){
  if(!hojas.length) return;
  if(!dry){
    const bans=hojas.map(hi=>{ const h=ultData.hojas[hi]||{};
      return '• '+(((h.ocr||{}).ban_code)||('foto #'+h.foto_n)); }).join('\n');
    if(!confirm(`⚠ Crear ${hojas.length} despacho(s) adicional(es) REALES en SGS:\n${bans}\n\n`+
        'Sube el Proyectado de cada material, crea la bandeja de despacho ('+BODEGA+'/Stock) '+
        'y la aprueba si cuadra con lo pedido.\n\n¿Continuar?')) return;
  }
  const r=await fetch(`/api/dev/sesion/${SID}/adicionales`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({dry_run:dry,hojas})});
  if(!r.ok){ const e=await r.json().catch(()=>({})); alert(e.error||'error'); }
  ultHashAdic=''; ultHashRes=''; refrescar();
}
function pintarAdic(){
  if(!adicAbierto || !ultData) return;
  const B=$('adic-body'), act=document.activeElement;
  // no repintar mientras se escribe un código o una cantidad
  if(act && act.closest && act.closest('#adic-body') && act.tagName==='INPUT') return;
  const d=ultData, q=($('adic-buscar').value||'').toUpperCase().replace(/[^A-Z0-9]/g,'');
  const hash=JSON.stringify([d.hojas,d.adic_job,d.adic_todos,adicFiltro,q]);
  if(hash===ultHashAdic) return;
  ultHashAdic=hash;
  const todas=adicHojas(d), N=todas.length;
  const cuenta={}; todas.forEach(x=>{cuenta[x.est]=(cuenta[x.est]||0)+1;});
  const listas=todas.filter(x=>x.est==='lista').map(x=>x.hi);
  const j=d.adic_job||{}, corriendo=j.estado==='corriendo';
  $('adic-modo').innerHTML=d.adic_todos
    ? `<span class="chip verde" title="Se crean adicionales de cualquier supervisor">Todos los supervisores</span>`
    : `<span class="chip amarillo" title="Solo se crean adicionales de hojas de los supervisores autorizados">Solo supervisores autorizados</span>`;
  $('adic-modo').innerHTML+=` <a class="lnk-hoja" style="font-size:12px" onclick="toggleAdicTodos()">cambiar</a>`;

  // ── Barra: conteo + acciones globales + filtros ──
  let A=`<div class="adic-barra">
    <b>${N} hoja${N===1?'':'s'} con adicionales</b>
    ${['creada','lista','resolver','sin_cruzar','sin_nodo','no_aut'].filter(e=>cuenta[e])
      .map(e=>`<span class="chip ${ADIC_EST[e][0]}">${ADIC_EST[e][1]}: ${cuenta[e]}</span>`).join(' ')}
    <span style="flex:1"></span>
    <button onclick="cruzarAdic(true)" title="Vuelve a cruzar todas las hojas con SGS (solo lectura): material en el NODO, Proyectado actual, líder">↻ Cruzar con SGS</button>
    <button ${listas.length&&!corriendo?'':'disabled'} onclick="crearAdic(true,[${listas}])"
      title="Simula las listas hasta antes de GUARDAR — no escribe nada">Probar listas</button>
    <button class="morado" ${listas.length&&!corriendo?'':'disabled'} onclick="crearAdic(false,[${listas}])"
      title="Crea en SGS los adicionales de todas las hojas listas, en orden">+ Crear las ${listas.length} listas</button>
  </div><div class="adic-barra">`+
    [['todas','Todas',N],['resolver','Por resolver',(cuenta.resolver||0)+(cuenta.sin_cruzar||0)+(cuenta.sin_nodo||0)+(cuenta.no_aut||0)],
     ['lista','Listas',cuenta.lista||0],['creada','Creadas',cuenta.creada||0]]
    .map(([k,t,c])=>`<button class="adic-filtro${adicFiltro===k?' on':''}" onclick="filtroAdic('${k}')">${t} (${c})</button>`).join('')+
    `</div>`;
  if(j.estado){
    if(corriendo) A+=`<div class="barra"><div style="width:${j.pct||0}%"></div></div>`;
    A+=`<div class="meta">${corriendo?'…':''} ${esc(j.msg||'')}</div>`;
  }
  $('adic-acciones').innerHTML=A;

  // ── Tarjetas, una por hoja, en el orden de las fotos ──
  const resJob={}; (j.resultados||[]).forEach(r=>{resJob[r.hoja]=r;});
  let html='', vis=0;
  todas.forEach((x,k)=>{
    const {hi,h,est,adic,dudas,quitadas,ae,p,a}=x;
    if(adicFiltro==='lista' && est!=='lista') return;
    if(adicFiltro==='creada' && est!=='creada') return;
    if(adicFiltro==='resolver' && !['resolver','sin_cruzar','sin_nodo','no_aut'].includes(est)) return;
    const o=h.ocr||{}, v=h.verificacion||{};
    const lider=(a&&a.equipo)||v.lider_sgs||(o.asignada_a||[])[0]||'?';
    const sup=(a&&a.supervisor)||(o.asignada_a||[])[1]||'?';
    if(q){
      const pajar=((o.ban_code||'')+(o.pro_code||'')+(ae&&ae.ban||'')+lider+sup).toUpperCase().replace(/[^A-Z0-9]/g,'');
      if(!pajar.includes(q)) return;
    }
    vis++;
    const url=(p&&p.url_sgs)||v.url_sgs, cerrada=est==='creada';
    html+=`<div class="rev-card adic-card ${est}" id="adic-${hi}">
      <div class="rev-head">
        <span class="adic-num" title="Posición en la pila de fotos">${k+1}/${N}</span>
        <span class="chip ${ADIC_EST[est][0]}">${ADIC_EST[est][1]}</span>
        <span class="rev-sub" style="margin:0">Hoja <b>${esc(o.ban_code||'?')}</b> · NODO <b class="pro">${esc(o.pro_code||'?')}</b> · foto #${h.foto_n}</span>
        <a class="lnk-hoja" onclick="verHoja(${hi})" title="Foto con zoom">ver hoja</a>
        ${url?('<a class="lnk-sgs" target="_blank" href="'+esc(url)+'">NODO en SGS</a>'):''}
      </div>
      <div class="rev-sub">Líder <b>${esc(lider)}</b> · Supervisor <b>${esc(sup)}</b>
        ${a?(' · Fecha '+esc(a.fecha)+' · '+esc(a.almacen)):''}</div>`;
    if(ae && ae.ban){
      html+=`<div class="rev-head"><span class="rev-tag adi">+ ADICIONAL</span>
        <span class="ban-grande adi">${esc(ae.ban)}</span>
        <span class="chip ${ae.aprobada?'verde':'amarillo'}">${ae.aprobada?'APROBADO':'PARA APROBAR'}</span>
        <span class="meta" style="margin:0"> anótala en la hoja física</span></div>`;
    }else if(a && a.otra_sesion){
      html+=`<div class="rev-head"><span class="rev-tag adi">+ YA CREADO</span>
        <span class="ban-grande adi">${esc(a.otra_sesion.ban_dev||'?')}</span>
        <span class="meta" style="margin:0">en otra sesión (${esc(a.otra_sesion.sesion)}) — esta foto no lo repite</span></div>`;
    }
    const msgs=[];
    if(est==='sin_nodo') msgs.push('La hoja no quedó verificada en SGS: corrige BAN/PRO en su tarjeta (↻ SGS) y vuelve a cruzar.');
    if(est==='no_aut' && a && a.detalle) msgs.push(a.detalle+' — usa "cambiar" arriba para hacer adicionales de todos.');
    const rj=resJob[hi];
    if(rj && rj.error && !cerrada) msgs.push('Último intento: '+rj.error);
    if(rj && (rj.advertencias||[]).length) msgs.push(...rj.advertencias);
    if(msgs.length) html+=`<div class="msgs">${msgs.map(m=>'<div>• '+esc(m)+'</div>').join('')}</div>`;

    html+=`<table><tr><th></th><th>Dice la hoja</th><th>Código</th><th>Cant.</th>
      <th>Material en el NODO</th><th>Proyectado</th><th>Detalle</th><th></th></tr>`;
    const dis=cerrada?'disabled':'';
    const filas=(a&&est!=='sin_cruzar')?a.items:adic.map(y=>({nota_idx:y.ni,texto:y.n.texto,
        ref:y.n.ref_interna,cantidad:y.n.cantidad,fuente:y.n.fuente,estado:'',detalle:'… falta cruzar con SGS'}));
    filas.forEach(i=>{
      const cl=i.estado==='ok'?'verde':(['ya_salio','sugerido'].includes(i.estado)?'amarillo':(i.estado?'rojo':''));
      const marca=(i.fuente==='humano'?'<small title="Corregido a mano"></small>':'')+
        (i.modo==='sufijo'?'<small title="Código corto completado con el único material del NODO que termina así">⚙</small>':'');
      let mat=esc(i.producto||'');
      if(!mat && (i.sugerencias||[]).length && !cerrada)
        mat='<div style="color:#fde047;font-size:12px;margin-bottom:2px">¿Cuál es?</div>'+
          i.sugerencias.map(s=>`<button class="sug" onclick="editarNotaAdic(${hi},${i.nota_idx},{ref_interna:'${esc(s.ref)}'})">${esc(s.producto)}</button>`).join('');
      if(!mat) mat='—';
      html+=`<tr class="${cl}"><td><span class="punto ${cl||'amarillo'}"></span>${marca}</td>
        <td class="nota-txt">${esc(i.texto||'')}</td>
        <td><input class="edit ancho${i.fuente==='humano'?' editado':''}" ${dis} value="${esc(i.ref||'')}"
            placeholder="código" onchange="editarNotaAdic(${hi},${i.nota_idx},{ref_interna:this.value})"></td>
        <td class="num"><input class="edit${i.fuente==='humano'?' editado':''}" ${dis} value="${i.cantidad??''}"
            onchange="editarNotaAdic(${hi},${i.nota_idx},{cantidad:this.value})"></td>
        <td>${mat}</td>
        <td class="num">${i.proyectado_actual!=null?(i.proyectado_actual+' → <b>'+i.proyectado_nuevo+'</b>'):'—'}</td>
        <td style="color:#94a3b8">${esc(i.detalle||'')}${i.estado==='ya_salio'&&!cerrada?
          `<br><button class="btn-sgs" style="margin-top:4px" title="Ya salió en otro despacho, pero la hoja pide OTRO: crear igual"
            onclick="editarNotaAdic(${hi},${i.nota_idx},{forzar:true})">Es otro pedido — crear igual</button>
           <button class="btn-sgs" style="margin-top:4px" title="El adicional ya se hizo: quitar esta nota"
            onclick="editarNotaAdic(${hi},${i.nota_idx},{tipo:'descartada'})">Ya se hizo — quitar</button>`:''}</td>
        <td>${cerrada?'':`<button class="btn-del" title="Esta nota NO es un adicional"
            onclick="editarNotaAdic(${hi},${i.nota_idx},{tipo:'descartada'})">✕</button>`}</td></tr>`;
    });
    if(!cerrada) dudas.forEach(y=>{
      html+=`<tr class="amarillo"><td title="El sistema no supo si es adicional">?</td>
        <td class="nota-txt">${esc(y.n.texto||'')}${y.n.interpretacion?('<br><small style="color:#94a3b8;font-family:inherit">'+esc(y.n.interpretacion)+'</small>'):''}</td>
        <td><input class="edit ancho" id="dq-ref-${hi}-${y.ni}" value="${esc(y.n.ref_interna||'')}" placeholder="código"></td>
        <td class="num"><input class="edit" id="dq-cant-${hi}-${y.ni}" value="${y.n.cantidad??''}"></td>
        <td colspan="4"><button class="btn-sgs" style="background:#3b0764;color:#f3e8ff" onclick="dudaEsAdic(${hi},${y.ni})">+ Es adicional</button>
          <button class="btn-sgs" onclick="editarNotaAdic(${hi},${y.ni},{tipo:'descartada'})">No es adicional</button></td></tr>`;
    });
    html+='</table>';
    if(quitadas.length && !cerrada)
      html+=`<div class="meta" style="margin-top:4px">✕ Quitadas: `+quitadas.map(y=>`"${esc(y.n.texto)}"
        <a class="lnk-hoja" onclick="editarNotaAdic(${hi},${y.ni},{tipo:'adicional'})">↩ restaurar</a>`).join(' · ')+`</div>`;
    if(!cerrada){
      html+=`<div class="adic-acc">
        <span class="agregar-nota" style="margin:0;flex:1">+ Otro que el OCR no leyó:
          <input class="edit ancho" id="aa-ref-${hi}" placeholder="código">
          <input class="edit" id="aa-cant-${hi}" placeholder="cant.">
          <button class="btn-sgs" onclick="agregarNotaAdic(${hi})">Agregar</button></span>`;
      if(est==='lista')
        html+=`<button ${corriendo?'disabled':''} onclick="crearAdic(true,[${hi}])">Probar</button>
          <button class="morado" ${corriendo?'disabled':''} onclick="crearAdic(false,[${hi}])">+ Crear este adicional</button>`;
      html+=`</div>`;
    }
    html+='</div>';
  });
  if(!N) html='<div class="vacio">Ninguna hoja de esta sesión pide adicionales.</div>';
  else if(!vis) html='<div class="vacio">Nada en este filtro.</div>';
  B.innerHTML=html;
}
async function devoluciones(dry){
  if(!dry && !confirm('⚠ Esto crea las bandejas de devolución REALES en SGS '+
      '(quedan "Para aprobar" por almacén).\n\n¿Continuar?')) return;
  const r=await fetch(`/api/dev/sesion/${SID}/devoluciones`,{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({dry_run:dry})});
  if(!r.ok){ const e=await r.json().catch(()=>({})); alert(e.error||'error'); }
  ultHashRes=''; refrescar();
}
init();
</script>
</body></html>"""


@app.get("/")
def pagina_pc():
    # Un celular que entra a la raíz va directo a la app de captura;
    # /?pc=1 fuerza la vista de escritorio desde un celular.
    ua = (request.headers.get("User-Agent") or "").lower()
    es_movil = any(k in ua for k in ("android", "iphone", "ipod", "mobile"))
    if es_movil and "pc" not in request.args:
        return redirect("/movil")
    r = Response(HTML_PC, mimetype="text/html")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/movil")
def pagina_movil():
    # no-store: un fix desplegado debe llegar al celular con solo recargar,
    # sin pelear con la caché del navegador
    r = Response(HTML_MOVIL, mimetype="text/html")
    r.headers["Cache-Control"] = "no-store"
    return r


# ══════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cert, key = asegurar_certificado()
    ips = _ips_locales()
    print("=" * 62)
    print("  DEVOLUCIONES SGS — servidor iniciado (HTTPS)")
    print(f"  PC:      https://localhost:{PORT}/")
    for ip in ips:
        print(f"  Celular: https://{ip}:{PORT}/movil")
    print("  (En el celular: aceptar el aviso de certificado la 1ª vez)")
    print("=" * 62)
    app.run(host="0.0.0.0", port=PORT, ssl_context=(cert, key),
            debug=False, threaded=True)
