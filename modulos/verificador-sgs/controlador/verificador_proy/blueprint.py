"""
blueprint.py — Blueprint Flask del Verificador de Proyectos SGS
===============================================================
Rutas públicas:
  GET  /verificador/login         → formulario de login
  POST /verificador/login         → procesa credenciales
  GET  /verificador/logout        → cierra sesión

Rutas protegidas (requieren login):
  GET  /verificador/              → UI principal (manual + automático)
  POST /verificador/api/verificar → búsqueda manual por código
  POST /verificador/api/auto      → análisis automático del mes
"""

import io
import os
import tempfile
from functools import wraps
from flask import (
    Blueprint, render_template, request, jsonify,
    session, redirect, url_for, send_file,
)
from werkzeug.security import check_password_hash

# Auto-detect templates folder (local dev: templates_balance, VPS: templates)
_HERE = os.path.dirname(os.path.abspath(__file__))
_TPL_LOCAL = os.path.join(_HERE, "..", "templates_balance")
_TPL_VPS   = os.path.join(_HERE, "..", "templates")
_TPL_DIR   = _TPL_LOCAL if os.path.isdir(_TPL_LOCAL) else _TPL_VPS

bp = Blueprint(
    "verificador_proy",
    __name__,
    url_prefix="/verificador",
    template_folder=_TPL_DIR,
)

# ── Auth helpers ───────────────────────────────────────────────────────────────

def _get_users() -> dict:
    """Devuelve el mapa usuario→hash desde config."""
    try:
        import config as cfg
        return getattr(cfg, "AUTH_USERS", {})
    except Exception:
        return {}


# ── SGO7 (puerta única): usuario validado por nginx en cabeceras X-SGO7-* ─────────
@bp.before_app_request
def _sgo7_sso():
    u = request.headers.get("X-SGO7-User")
    if u and (not session.get("autenticado") or session.get("usuario") != u):
        session["autenticado"] = True
        session["usuario"] = u


def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("autenticado"):
            if request.path.startswith("/verificador/api/"):
                return jsonify({"ok": False, "error": "Sesión expirada. Recarga la página e inicia sesión.", "login_required": True}), 401
            return redirect(url_for("verificador_proy.login", next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── Rutas de autenticación ────────────────────────────────────────────────────

@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("autenticado"):
        return redirect(url_for("verificador_proy.index"))

    error = None
    if request.method == "POST":
        usuario  = (request.form.get("usuario") or "").strip().lower()
        password = request.form.get("password") or ""
        users    = _get_users()
        hash_guardado = users.get(usuario)

        if hash_guardado and check_password_hash(hash_guardado, password):
            session.permanent = True
            session["autenticado"] = True
            session["usuario"]     = usuario
            next_url = request.args.get("next") or url_for("verificador_proy.index")
            return redirect(next_url)
        else:
            error = "Usuario o contraseña incorrectos."

    return render_template("verificador_login.html", error=error)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("verificador_proy.login"))


# ── Rutas protegidas ──────────────────────────────────────────────────────────

@bp.route("/")
@_login_required
def index():
    # La base de los enlaces a SGS viaja al template en vez de estar escrita a
    # mano en el JS: cuando el host cambió, los enlaces de nodo quedaron
    # apuntando a un dominio muerto sin que nadie lo notara.
    try:
        import config as cfg
        sgs_web = getattr(cfg, "SGS_WEB_URL", "") or getattr(cfg, "SGS_URL", "")
    except Exception:
        sgs_web = ""
    return render_template("verificador.html",
                           usuario=session.get("usuario", ""),
                           sgs_web_url=str(sgs_web).rstrip("/"),
                           # «Abrir en Outlook (PC)» abre el buzón del PC del operador: solo el desarrollador
                           puede_outlook=request.headers.get("X-SGO7-Rol", "") == "desarrollador")


@bp.route("/api/verificar", methods=["POST"])
@_login_required
def api_verificar():
    data        = request.get_json() or {}
    codigos_raw = (data.get("codigos") or "").strip()
    if not codigos_raw:
        return jsonify({"ok": False, "error": "Ingresa al menos un código de proyecto."}), 400
    try:
        from verificador_proy.sgs_checker import verificar_proyectos
        return jsonify(verificar_proyectos(codigos_raw))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/auto", methods=["POST"])
@_login_required
def api_auto():
    data = request.get_json() or {}
    try:
        from datetime import datetime
        hoy  = datetime.now()
        año  = int(data.get("año",  hoy.year))
        mes  = int(data.get("mes",  hoy.month))
        if not (1 <= mes <= 12) or año < 2020:
            return jsonify({"ok": False, "error": "Mes o año inválido."}), 400
        from verificador_proy.sgs_checker import analisis_mes
        return jsonify(analisis_mes(año, mes))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/auto/export", methods=["GET"])
@_login_required
def api_auto_export():
    """
    Exporta el análisis automático a Excel (.xlsx).
      ?año=2026&mes=4   → un mes
      ?año=2026         → año completo (todos los meses con datos)
    """
    try:
        from datetime import datetime
        hoy = datetime.now()
        año = int(request.args.get("año", hoy.year))
        mes_raw = request.args.get("mes", "").strip()
        mes = int(mes_raw) if mes_raw and mes_raw != "0" else None
        if año < 2020 or (mes is not None and not (1 <= mes <= 12)):
            return jsonify({"ok": False, "error": "Mes o año inválido."}), 400
        from verificador_proy.export_auto import generar_excel_auto
        contenido, fname = generar_excel_auto(año, mes)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return send_file(
        io.BytesIO(contenido),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=fname,
    )


@bp.route("/api/cruce", methods=["POST"])
@_login_required
def api_cruce():
    if "archivo" not in request.files:
        return jsonify({"ok": False, "error": "No se recibió ningún archivo."}), 400
    archivo = request.files["archivo"]
    if not archivo.filename:
        return jsonify({"ok": False, "error": "Archivo vacío."}), 400
    suffix = ".xlsx" if archivo.filename.lower().endswith("xlsx") else ".xls"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        archivo.save(tmp.name)
        tmp.close()
        from verificador_proy.sgs_checker import cruce_excel
        return jsonify(cruce_excel(tmp.name))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@bp.route("/api/plantilla-mat", methods=["POST"])
@_login_required
def api_plantilla_mat():
    """
    Genera la plantilla masiva de MATERIALES para Liquidador 2 a partir de los
    proyectos que el usuario marcó con checkbox en "Automático por Mes".

    El cliente envía las familias ya analizadas (mismo shape que devuelve
    `analisis_mes`) en vez de un listado de códigos: reanalizar el mes tarda
    ~30 s y devolvería exactamente los datos que el navegador ya tiene en
    pantalla. La generación es pura transformación, sin tocar SGS.
    """
    data = request.get_json(silent=True) or {}
    familias = data.get("familias") or []
    if not familias:
        return jsonify({"ok": False, "error": "No se seleccionó ningún proyecto."}), 400
    try:
        from verificador_proy.export_plantilla_mat import generar_plantilla_materiales
        contenido, fname = generar_plantilla_materiales(
            familias, periodo=str(data.get("periodo") or "").strip())
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return send_file(
        io.BytesIO(contenido),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=fname,
    )


# ── Validado por correos ────────────────────────────────────────────

@bp.route("/api/validado-correos", methods=["POST"])
@_login_required
def api_validado_correos():
    """Lo que Centro MO mandó a liquidar (presupuesto del correo) y SGS muestra sin liquidar."""
    import re as _re
    data = request.get_json(silent=True) or {}
    mes = str(data.get("mes") or "").strip()
    if mes and mes != "todos" and not _re.match(r"^\d{4}-\d{2}$", mes):
        return jsonify({"ok": False, "error": "Mes inválido (AAAA-MM o «todos»)."}), 400
    try:
        from verificador_proy.validado_correos import analizar
        res = analizar(mes)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(res), (200 if res.get("ok") else 502)


@bp.route("/api/validado-correos/excel", methods=["POST"])
@_login_required
def api_validado_correos_excel():
    """Excel del análisis con lo que quedó marcado en pantalla (pura transformación, sin SGS)."""
    data = request.get_json(silent=True) or {}
    familias = data.get("familias") or []
    if not familias:
        return jsonify({"ok": False, "error": "No hay análisis que exportar."}), 400
    try:
        from verificador_proy.validado_correos import generar_excel
        contenido, fname = generar_excel(familias, mes=str(data.get("mes") or "").strip())
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return send_file(
        io.BytesIO(contenido),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=fname,
    )


# ── «Ver correo»: cuerpo guardado por Gestión General y apertura en el Outlook del PC ──────

@bp.route("/api/correo/<rid>")
@_login_required
def api_correo(rid):
    """Cabecera de Centro MO + cuerpo (HTML saneado) de Gestión General. El cuerpo se pide al backend con
    la sesión del usuario (cookie de la puerta única = su JWT): solo lo ve quien ya entró a SGO7."""
    from verificador_proy.validado_correos import correo_de
    res, code = correo_de(rid, request.cookies.get("sgo7_session", ""))
    return jsonify(res), code


@bp.route("/api/correo/<rid>/outlook", methods=["POST"])
@_login_required
def api_correo_outlook(rid):
    """Pide al agente del PC (vía Centro MO) abrir el correo en Outlook. Queda en Registros.
    Solo el desarrollador (es el buzón del PC del operador); los demás ven el popup con el correo guardado."""
    if request.headers.get("X-SGO7-Rol", "") != "desarrollador":
        return jsonify({"ok": False, "error": "Abrir en Outlook es solo del desarrollador"}), 403
    from verificador_proy.validado_correos import abrir_en_outlook
    res, code = abrir_en_outlook(rid, {k: request.headers.get(k, "")
                                       for k in ("X-SGO7-User", "X-SGO7-Rol", "X-SGO7-Nombre")})
    return jsonify(res), code


@bp.route("/api/pedido-correo/<pid>")
@_login_required
def api_pedido_correo(pid):
    if request.headers.get("X-SGO7-Rol", "") != "desarrollador":
        return jsonify({"error": "solo el desarrollador"}), 403
    from verificador_proy.validado_correos import pedido_estado
    res, code = pedido_estado(pid)
    return jsonify(res), code
