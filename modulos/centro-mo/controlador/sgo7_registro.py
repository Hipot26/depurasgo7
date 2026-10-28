"""sgo7_registro.py — Trazabilidad SGO7 para los módulos Flask (Balance, Materiales,
Verificador, Devoluciones, Liquidador).

Cada acción que cambia algo queda registrada en la plataforma padre (sabogal):
  - toda subida de archivo: nombre, tamaño, huella SHA-256 y una COPIA guardada;
  - todo proceso (POST/PUT/PATCH/DELETE): ruta, datos del formulario (sin contraseñas),
    código de respuesta, duración;
  - toda descarga generada (Content-Disposition: attachment): nombre y tamaño.
Con el usuario SGO7 que validó la puerta (cabeceras X-SGO7-*), la IP real y la sesión
del módulo si va en la ruta.

Nunca bloquea al módulo: el envío va en un hilo aparte con 5 s de tope y, si el backend
no responde, el registro se guarda en un JSONL local (pendientes.jsonl) para reinyectarlo.

Uso (una línea tras crear la app):
    import sgo7_registro
    sgo7_registro.instalar(app, modulo="balance", acciones={r"/generar": "balance_integrado"})
Registro explícito desde una ruta (p. ej. resultado de una liquidación):
    sgo7_registro.registrar("liquidador_resultado", detalle={"ok": 12, "error": 1}, sesion=sid)
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone

from flask import g, has_request_context, request

URL = os.environ.get("SGO7_REGISTROS_URL", "http://127.0.0.1:3008/api/sgo7/registros")
TOKEN = os.environ.get("SGO7_INTERNAL_TOKEN", "")
MODULO = os.environ.get("SGO7_MODULO", "modulo")
DIR_COPIAS = os.environ.get("SGO7_COPIAS_DIR", "/var/www/sgo7/registros/archivos")
FALLBACK = os.environ.get("SGO7_REGISTROS_FALLBACK", "/var/www/sgo7/registros/pendientes.jsonl")
MAX_COPIA = 60 * 1024 * 1024          # copias de hasta 60 MB
_SENSIBLE = re.compile(r"(pass|clave|secret|token|contrase|cookie)", re.I)
_RUTAS_IGNORADAS = ("/login", "/logout")
_RE_SESION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b\d{8}-\d{6}\b")


def _usuario():
    h = request.headers
    return (h.get("X-SGO7-User") or None), (h.get("X-SGO7-Rol") or None)


def _ip():
    return request.headers.get("X-Real-IP") or request.remote_addr


def _enviar(payload):
    def _go():
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        try:
            req = urllib.request.Request(
                URL, data=data, method="POST",
                headers={"Content-Type": "application/json", "X-SGO7-Interno": TOKEN})
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
        except Exception as e:               # nunca se pierde: queda en el JSONL local
            try:
                os.makedirs(os.path.dirname(FALLBACK), exist_ok=True)
                with open(FALLBACK, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"error": str(e), "registro": payload},
                                       ensure_ascii=False, default=str) + "\n")
            except Exception:
                pass
    threading.Thread(target=_go, daemon=True).start()


def _guardar_copia(fs, sha, nombre):
    try:
        sub = os.path.join(DIR_COPIAS, MODULO, datetime.now().strftime("%Y-%m"))
        os.makedirs(sub, exist_ok=True)
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", nombre)[:120] or "archivo"
        dest = os.path.join(sub, f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{sha[:10]}_{base}")
        fs.stream.seek(0)
        with open(dest, "wb") as out:
            while True:
                chunk = fs.stream.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        fs.stream.seek(0)
        os.chmod(dest, 0o640)
        return dest
    except Exception:
        return None


def _archivos():
    """[(nombre, bytes, sha256, ruta_copia)] de los archivos subidos en la petición."""
    out = []
    try:
        for campo in request.files:
            for fs in request.files.getlist(campo):
                if not fs or not fs.filename:
                    continue
                fs.stream.seek(0)
                h = hashlib.sha256()
                n = 0
                while True:
                    chunk = fs.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
                    n += len(chunk)
                fs.stream.seek(0)
                sha = h.hexdigest()
                copia = _guardar_copia(fs, sha, fs.filename) if n <= MAX_COPIA else None
                out.append((fs.filename, n, sha, copia))
    except Exception:
        pass
    return out


def _form():
    """Campos del formulario o JSON, sin nada que parezca contraseña y recortados."""
    d = {}
    try:
        for k, v in request.form.items():
            if _SENSIBLE.search(k):
                continue
            d[k] = v[:300]
        if request.is_json:
            j = request.get_json(silent=True)
            if isinstance(j, dict):
                for k, v in list(j.items())[:40]:
                    if _SENSIBLE.search(str(k)):
                        continue
                    d[str(k)] = v if isinstance(v, (int, float, bool)) or v is None else str(v)[:300]
    except Exception:
        pass
    return d


def registrar(accion, detalle=None, estado="ok", archivo=None, sesion=None, usuario=None, rol=None):
    """Registro explícito desde una ruta o un hilo de trabajo (en un hilo no hay petición:
    pásale usuario/rol guardados cuando se lanzó el trabajo)."""
    try:
        en_req = has_request_context()
        if en_req and not usuario:
            usuario, rol = _usuario()
        _enviar({"modulo": MODULO, "usuario": usuario, "rol": rol, "accion": accion,
                 "estado": estado, "detalle": detalle, "archivo": archivo, "sesion": sesion,
                 "metodo": request.method if en_req else None,
                 "ruta": request.path[:300] if en_req else None,
                 "ip": _ip() if en_req else None,
                 "ts": datetime.now(timezone.utc).isoformat()})
    except Exception:
        pass


def instalar(app, modulo=None, acciones=None, ignorar=()):
    """Engancha el registro automático a la app Flask.
    acciones: {regex_de_ruta: nombre_de_accion} (se prueba en orden; la primera que casa gana).
    ignorar:  prefijos de ruta que no se registran (p. ej. sondeos POST muy frecuentes)."""
    global MODULO
    if modulo:
        MODULO = modulo
    reglas = [(re.compile(k), v) for k, v in (acciones or {}).items()]

    @app.before_request
    def _sgo7_reg_inicio():
        g._sgo7_t0 = time.time()
        g._sgo7_archivos = (_archivos()
                            if request.method in ("POST", "PUT", "PATCH") and request.files else [])

    @app.after_request
    def _sgo7_reg_fin(resp):
        try:
            p = request.path
            if any(p.startswith(x) for x in ignorar) or any(p.endswith(x) for x in _RUTAS_IGNORADAS):
                return resp
            cd = resp.headers.get("Content-Disposition", "")
            es_descarga = "attachment" in cd
            if request.method not in ("POST", "PUT", "PATCH", "DELETE") and not es_descarga:
                return resp
            usuario, rol = _usuario()
            archivos = getattr(g, "_sgo7_archivos", None) or []
            accion = next((n for rx, n in reglas if rx.match(p)), None)
            if not accion:
                accion = "descargar" if es_descarga else ("subir_archivo" if archivos else "procesar")
            m = _RE_SESION.search(p)
            base = {"modulo": MODULO, "usuario": usuario, "rol": rol, "accion": accion,
                    "metodo": request.method, "ruta": p[:300], "estado": str(resp.status_code),
                    "duracion_ms": int((time.time() - getattr(g, "_sgo7_t0", time.time())) * 1000),
                    "ip": _ip(), "sesion": m.group(0) if m else None,
                    "detalle": _form() or None, "ts": datetime.now(timezone.utc).isoformat()}
            if es_descarga:
                mm = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
                base["archivo"] = mm.group(1) if mm else None
                try:
                    base["archivo_bytes"] = int(resp.headers.get("Content-Length") or 0) or None
                except Exception:
                    pass
            if archivos:
                for nombre, n, sha, copia in archivos:
                    _enviar(dict(base, archivo=nombre, archivo_bytes=n, archivo_hash=sha, archivo_copia=copia))
            else:
                _enviar(base)
        except Exception:
            pass
        return resp
