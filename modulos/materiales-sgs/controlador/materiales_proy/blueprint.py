"""
blueprint.py — Blueprint Flask del módulo MATERIALES (Zona VII)
==============================================================
Todas las rutas viven bajo /materiales (detrás del proxy nginx de la plataforma).

  GET  /materiales/login          → formulario de login
  POST /materiales/login          → procesa credenciales (login propio)
  GET  /materiales/logout         → cierra sesión
  GET  /materiales/               → dashboard (protegido)
  GET  /materiales/api/zonas      → macro-zonas disponibles
  POST /materiales/api/analizar   → análisis por rango + zonas
  GET  /materiales/api/export     → Excel del rango
"""

import io
import os
from functools import wraps
from datetime import datetime

from flask import (
    Blueprint, render_template, request, jsonify,
    session, redirect, url_for, send_file,
)
from werkzeug.security import check_password_hash

_HERE = os.path.dirname(os.path.abspath(__file__))
_TPL_DIR = os.path.join(_HERE, "..", "templates")

bp = Blueprint(
    "materiales_proy", __name__,
    url_prefix="/materiales",
    template_folder=_TPL_DIR,
)


# ── Auth ───────────────────────────────────────────────────────────────────────
def _get_users() -> dict:
    try:
        import config as cfg
        return getattr(cfg, "AUTH_USERS", {})
    except Exception:
        return {}


# ── SGO7 (puerta única): usuario validado por nginx en cabeceras X-SGO7-* ─────────
@bp.before_app_request
def _sgo7_sso():
    u = request.headers.get("X-SGO7-User")
    if u and (not session.get("mat_auth") or session.get("mat_user") != u):
        session["mat_auth"] = True
        session["mat_user"] = u


def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("mat_auth"):
            if request.path.startswith("/materiales/api/"):
                return jsonify({"ok": False, "error": "Sesión expirada. Recarga e inicia sesión.",
                                "login_required": True}), 401
            return redirect(url_for("materiales_proy.login", next=request.path))
        return f(*args, **kwargs)
    return decorated


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("mat_auth"):
        return redirect(url_for("materiales_proy.index"))
    error = None
    if request.method == "POST":
        usuario  = (request.form.get("usuario") or "").strip().lower()
        password = request.form.get("password") or ""
        users = _get_users()
        h = users.get(usuario)
        if h and check_password_hash(h, password):
            session.permanent = True
            session["mat_auth"] = True
            session["mat_user"] = usuario
            return redirect(request.args.get("next") or url_for("materiales_proy.index"))
        error = "Usuario o contraseña incorrectos."
    return render_template("materiales_login.html", error=error)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("materiales_proy.login"))


# ── Dashboard ──────────────────────────────────────────────────────────────────
@bp.route("/")
@_login_required
def index():
    return render_template("materiales.html", usuario=session.get("mat_user", ""))


# ── API ────────────────────────────────────────────────────────────────────────
def _default_range():
    hoy = datetime.now()
    inicio = hoy.replace(day=1).strftime("%Y-%m-%d")
    return inicio, hoy.strftime("%Y-%m-%d")


@bp.route("/api/zonas", methods=["GET"])
@_login_required
def api_zonas():
    from materiales_proy.sgs_materiales import zonas_disponibles
    return jsonify(zonas_disponibles())


@bp.route("/api/analizar", methods=["POST"])
@_login_required
def api_analizar():
    data = request.get_json() or {}
    di_def, df_def = _default_range()
    fecha_inicio = (data.get("fecha_inicio") or di_def).strip()
    fecha_fin    = (data.get("fecha_fin") or df_def).strip()
    zonas        = data.get("zonas") or []
    supervisores = data.get("supervisores") or []
    try:
        from materiales_proy.sgs_materiales import analizar
        return jsonify(analizar(fecha_inicio, fecha_fin, zonas, supervisores))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/export", methods=["GET"])
@_login_required
def api_export():
    di_def, df_def = _default_range()
    fecha_inicio = (request.args.get("fecha_inicio") or di_def).strip()
    fecha_fin    = (request.args.get("fecha_fin") or df_def).strip()
    zonas_raw    = (request.args.get("zonas") or "").strip()
    sups_raw     = (request.args.get("supervisores") or "").strip()
    zonas = [z for z in zonas_raw.split(",") if z]
    sups  = [s for s in sups_raw.split(",") if s]
    try:
        from materiales_proy.export_mat import generar_excel
        contenido, fname = generar_excel(fecha_inicio, fecha_fin, zonas, sups)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return send_file(
        io.BytesIO(contenido),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=fname,
    )
