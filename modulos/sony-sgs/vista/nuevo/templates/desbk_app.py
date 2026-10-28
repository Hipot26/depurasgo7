#!/usr/bin/env python3
"""
/nuevo — Balance Integrado SIPWEBORA + SGS
==========================================
App Flask independiente. No modifica /pruebas ni sgs_api.

Uso:
  cd "c:/Users/duran/Documents/SGS + SONY/nuevo"
  python app.py

Luego abre: http://localhost:5002
"""

import io
import os
import sys
import uuid
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, send_file, redirect, url_for, session

# Asegurar que nuevo/ esté en sys.path para que gunicorn pueda importar integracion.py
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

app = Flask(__name__, template_folder=os.path.join(_HERE, "templates"))
app.secret_key = os.environ.get("NUEVO_SECRET", "cambiar-en-produccion-32chars")

# ── Credenciales (variables de entorno obligatorias en producción) ─────────────
_AUTH_USER = os.environ.get("NUEVO_USER", "")
_AUTH_PASS = os.environ.get("NUEVO_PASS", "")


def _check_auth(usuario: str, password: str) -> bool:
    return bool(_AUTH_USER) and usuario == _AUTH_USER and password == _AUTH_PASS


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("autenticado"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


# Almacén en memoria de archivos generados: token → {bytes, filename}
_archivos: dict = {}

# ── Cliente reutilizable (Balance Integrado) ───────────────────────────────────

_balance_cliente = None


def _get_cliente():
    global _balance_cliente
    from integracion import BalanceIntegrado, cfg_desde_archivo
    if _balance_cliente is not None:
        try:
            c = _balance_cliente._cliente
            c._call_kw("res.users", "read", [[c.uid]], {"fields": ["id"]})
            return _balance_cliente
        except Exception:
            _balance_cliente = None

    cfg = cfg_desde_archivo()
    _balance_cliente = BalanceIntegrado(**cfg)
    return _balance_cliente


# ── Cliente SGS puro (sin SIPWEBORA) ──────────────────────────────────────────
# Reutiliza el _cliente interno de BalanceIntegrado, que ya maneja auto-detección de DB.

def _get_sgs_cliente():
    return _get_cliente()._cliente


# ── Rutas ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("autenticado"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if _check_auth(request.form.get("usuario", ""), request.form.get("password", "")):
            session["autenticado"] = True
            next_url = request.form.get("next") or "/"
            return redirect(next_url)
        error = "Usuario o contraseña incorrectos."
    return render_template("login.html", error=error, next=request.args.get("next", ""))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/generar", methods=["POST"])
@login_required
def generar():
    id_planilla   = request.form.get("id_planilla",   "").strip()
    proyecto_id   = request.form.get("proyecto_id",   "").strip() or None

    ids_sip = [x.strip() for x in id_planilla.replace(";", ",").split(",") if x.strip()]
    if not ids_sip or not all(x.isdigit() for x in ids_sip):
        return render_template(
            "index.html",
            error="El ID de planilla debe ser un número, o varios números separados por coma.",
            id_planilla=id_planilla,
            proyecto_id=proyecto_id or "",
        )

    try:
        cliente   = _get_cliente()
        resultado = cliente.ejecutar(id_planilla, proyecto_id_sgs=proyecto_id)

        token = str(uuid.uuid4())
        _archivos[token] = {
            "bytes":    resultado["excel_bytes"],
            "filename": resultado["filename"],
            "advertencias": resultado.get("advertencias", []),
        }

        return render_template(
            "resultado.html",
            token=token,
            filename=resultado["filename"],
            metadata=resultado["metadata"],
            sgs_padre=resultado["sgs_padre"],
            nodos=resultado["nodos_resumen"],
            advertencias=resultado.get("advertencias", []),
        )

    except RuntimeError as e:
        return render_template(
            "index.html",
            error=str(e),
            id_planilla=id_planilla,
            proyecto_id=proyecto_id or "",
        )
    except Exception as e:
        return render_template(
            "index.html",
            error=f"Error inesperado: {e}",
            id_planilla=id_planilla,
            proyecto_id=proyecto_id or "",
        )


@app.route("/descargar/<token>")
@login_required
def descargar(token):
    archivo = _archivos.get(token)
    if not archivo:
        return "Archivo no encontrado o expirado.", 404
    return send_file(
        io.BytesIO(archivo["bytes"]),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=archivo["filename"],
    )


@app.route("/generar-sgs", methods=["POST"])
@login_required
def generar_sgs():
    from generar_balance import generar_excel

    # Paso 1 → viene de index.html (solo proyecto_id_sgs)
    # Paso 2 → viene de seleccionar_nodos.html (parent_id + nodo_codes[])
    proyecto_id  = request.form.get("proyecto_id_sgs", "").strip()
    parent_id_hd = request.form.get("parent_id", "").strip()   # hidden en paso 2
    nodos_sel    = request.form.getlist("nodo_codes")           # lista de PRO-XXXXXX seleccionados
    es_paso2     = bool(parent_id_hd)                          # True cuando viene de seleccionar_nodos

    pid_raw = parent_id_hd or proyecto_id
    if not pid_raw:
        return render_template("index.html", error_sgs="Ingresa un ID o código de proyecto SGS.", tab="sgs")

    try:
        cliente = _get_sgs_cliente()

        if pid_raw.isdigit():
            parent_id = int(pid_raw)
        else:
            proyecto = cliente.buscar_por_codigo(pid_raw)
            if not proyecto:
                return render_template(
                    "index.html",
                    error_sgs=f"Proyecto '{pid_raw}' no encontrado en SGS.",
                    proyecto_id_sgs=pid_raw,
                    tab="sgs",
                )
            parent_id = proyecto["id"]

        data  = cliente.balance_completo(parent_id)
        padre = data["padre"]
        nodos = data["nodos"]

        nodos_resumen = [
            {
                "pro":        n.get("code", ""),
                "nodo":       n.get("nodo", "—"),
                "status":     (n.get("status", ["", ""])[1]
                               if isinstance(n.get("status"), list)
                               else str(n.get("status", ""))),
                "bandejas":   len(n.get("bandejas", [])),
                "materiales": len(n.get("liq_materiales", [])),
            }
            for n in nodos
        ]

        # ── Paso 1: mostrar pantalla de selección de nodos ────────────────────
        if not es_paso2:
            return render_template(
                "seleccionar_nodos.html",
                padre=padre,
                parent_id=str(parent_id),
                nodos=nodos_resumen,
            )

        # ── Paso 2: filtrar nodos seleccionados y generar Excel ───────────────
        if not nodos_sel:
            return render_template(
                "seleccionar_nodos.html",
                padre=padre,
                parent_id=str(parent_id),
                nodos=nodos_resumen,
                error="Selecciona al menos un nodo.",
            )

        nodos_filtrados = [n for n in nodos if n.get("code", "") in nodos_sel]
        data_filtrada   = dict(data, nodos=nodos_filtrados)

        odm      = padre.get("order_number") or padre.get("odm") or str(parent_id)
        ts       = datetime.now().strftime("%Y%m%d_%H%M")
        sufijo   = f"_{len(nodos_filtrados)}nodos" if len(nodos_filtrados) < len(nodos) else ""
        filename = f"balance_sgs_{odm}{sufijo}_{ts}.xlsx"

        excel_bytes = generar_excel(data_filtrada)

        token = str(uuid.uuid4())
        _archivos[token] = {"bytes": excel_bytes, "filename": filename}

        nodos_resultado = [nr for nr in nodos_resumen if nr["pro"] in nodos_sel]

        return render_template(
            "resultado_sgs.html",
            token=token,
            filename=filename,
            padre=padre,
            odm=odm,
            nodos=nodos_resultado,
        )

    except Exception as e:
        msg = str(e)
        try:
            return render_template("index.html", error_sgs=msg, proyecto_id_sgs=pid_raw, tab="sgs")
        except Exception:
            return f"<pre style='color:red;padding:20px'>ERROR SGS:\n{msg}</pre>", 500


@app.route("/sgs-libre", methods=["POST"])
@login_required
def sgs_libre():
    """
    Opción B — búsqueda por LCL / DF / ODM.
    Encuentra el proyecto padre en SGS y abre el wizard de 4 pasos.
    """
    from manual_balance import nueva_sesion

    codigo_raw = request.form.get("lcl_codigo", "").strip()
    pdl_raw    = request.form.get("pdl", "").strip()
    if not codigo_raw:
        return render_template("index.html", error_sgs_libre="Ingresa un código LCL, DF u ODM.", tab="sgs", pdl=pdl_raw)

    codigo = codigo_raw.upper()
    if codigo.startswith("LCL"):
        codigo = codigo[3:].lstrip("_- ")

    try:
        cliente  = _get_sgs_cliente()
        proyecto = cliente.buscar_por_codigo(codigo)
        if not proyecto:
            return render_template(
                "index.html",
                error_sgs_libre=f"No se encontró ningún proyecto con código '{codigo_raw}' en SGS.",
                lcl_codigo=codigo_raw,
                tab="sgs",
            )

        parent_id = proyecto["id"]
        padre     = cliente.leer_proyecto(parent_id)
        sid       = nueva_sesion(padre or proyecto, parent_id)
        if pdl_raw:
            from manual_balance import get_sesion, save_sesion
            s = get_sesion(sid)
            s["pdl"] = pdl_raw
            save_sesion(sid)
        # Lanzar fetch SGS en background para que esté listo en paso 3
        from manual_balance import iniciar_fetch_sgs
        iniciar_fetch_sgs(sid, _get_sgs_cliente)
        return redirect(url_for("manual_paso2", sid=sid))

    except Exception as e:
        return render_template(
            "index.html",
            error_sgs_libre=f"Error consultando SGS: {e}",
            lcl_codigo=codigo_raw,
            tab="sgs",
        )


# ── Wizard Manual — Balance SGS Puro ──────────────────────────────────────────

@app.route("/manual/mo/<sid>", methods=["GET", "POST"])
@login_required
def manual_mo(sid):
    import json as _json
    from manual_balance import get_sesion, parsear_mo, get_precios, save_sesion
    sesion = get_sesion(sid)
    if not sesion:
        return render_template("index.html", tab="sgs",
                               error_sgs_libre="Sesión expirada — ingresa el código del proyecto nuevamente.")

    precios_json = _json.dumps(get_precios(), ensure_ascii=False)

    if request.method == "POST":
        from manual_balance import enriquecer_mo
        texto = request.form.get("texto_mo", "").strip()
        rows, errors = parsear_mo(texto)
        enriquecer_mo(rows)
        sesion["rows_mo"] = rows
        sesion["errs_mo"] = errors
        save_sesion(sid)
        if not errors and rows:
            return redirect(url_for("manual_nodos", sid=sid))
        if not rows:
            errors = errors or ["Pega al menos una fila con Nodo y QP."]
        return render_template("manual_mo.html", sesion=sesion, sid=sid,
                               texto=texto, errors=errors,
                               precios_json=precios_json)

    return render_template("manual_mo.html", sesion=sesion, sid=sid,
                           texto="", errors=[], precios_json=precios_json)


@app.route("/manual/paso2/<sid>", methods=["GET"])
@login_required
def manual_paso2(sid):
    import json as _json
    from manual_balance import get_sesion, get_precios, resolver_nodos
    sesion = get_sesion(sid)
    if not sesion:
        return render_template("index.html", tab="sgs",
                               error_sgs_libre="Sesión expirada — ingresa el código del proyecto nuevamente.")
    precios_json = _json.dumps(get_precios(), ensure_ascii=False)
    sgs_ready = sesion.get("sgs_ready")
    nodos_resueltos = None
    if sgs_ready is True and sesion.get("sgs_data"):
        mo_nodos = [r["nodo"] for r in sesion.get("rows_mo", [])]
        nodos_resueltos = resolver_nodos(sesion["sgs_data"]["nodos"], mo_nodos)
    # Supervisor: use saved override only — responsible_id is always the API user, not the real supervisor
    sup_val = sesion.get("supervisor") or ""
    return render_template("manual_paso2.html",
                           sesion=sesion, sid=sid,
                           precios_json=precios_json,
                           sgs_ready=sgs_ready,
                           nodos_resueltos=nodos_resueltos,
                           texto_mo=sesion.get("texto_mo", ""),
                           texto_nodos=sesion.get("texto_nodos", ""),
                           supervisor=sup_val)


@app.route("/api/guardar-mo/<sid>", methods=["POST"])
@login_required
def api_guardar_mo(sid):
    from manual_balance import get_sesion, parsear_mo, enriquecer_mo, save_sesion, resolver_nodos
    sesion = get_sesion(sid)
    if not sesion:
        return {"ok": False, "error": "Sesión expirada"}, 404
    data = request.get_json(silent=True) or {}
    texto = data.get("texto", "").strip()
    rows, errors = parsear_mo(texto)
    if not errors and rows:
        enriquecer_mo(rows)
        sesion["rows_mo"]  = rows
        sesion["errs_mo"]  = []
        sesion["texto_mo"] = texto
        save_sesion(sid)
        # Verificar nodos contra SGS si ya está listo
        resueltos = None
        if sesion.get("sgs_ready") is True and sesion.get("sgs_data"):
            mo_nodos = list(dict.fromkeys(r["nodo"] for r in rows))
            resueltos = resolver_nodos(sesion["sgs_data"]["nodos"], mo_nodos)
        return {"ok": True, "count": len(rows), "resueltos": resueltos,
                "sgs_ready": sesion.get("sgs_ready")}
    return {"ok": False, "errors": errors or ["Sin filas válidas"], "count": len(rows)}


@app.route("/api/sgs-status/<sid>")
@login_required
def api_sgs_status(sid):
    from manual_balance import get_sesion, resolver_nodos
    sesion = get_sesion(sid)
    if not sesion:
        return {"ready": None, "error": "Sesión expirada"}, 404
    ready = sesion.get("sgs_ready")
    error = sesion.get("sgs_error")
    if ready is True and not error:
        sgs_data = sesion.get("sgs_data") or {}
        nodos = sgs_data.get("nodos", [])
        mo_nodos = [r["nodo"] for r in sesion.get("rows_mo", [])]
        resueltos = resolver_nodos(nodos, mo_nodos)
        return {"ready": True, "error": None, "nodos_count": len(nodos), "resueltos": resueltos}
    return {"ready": ready, "error": error, "nodos_count": 0}


@app.route("/api/buscar-nodos/<sid>", methods=["POST"])
@login_required
def api_buscar_nodos(sid):
    from manual_balance import get_sesion, save_sesion, resolver_nodos, detectar_nodos_solos
    sesion = get_sesion(sid)
    if not sesion:
        return {"ok": False, "error": "Sesión expirada"}, 404
    if sesion.get("sgs_ready") is not True:
        return {"ok": False, "sgs_loading": True, "error": "SGS aún cargando, espera un momento"}
    sgs_data = sesion.get("sgs_data")
    if not sgs_data:
        return {"ok": False, "error": "Datos SGS no disponibles"}
    data = request.get_json(silent=True) or {}
    texto = data.get("texto", "").strip()
    sesion["texto_nodos"] = texto
    save_sesion(sid)
    nodos_solos = detectar_nodos_solos(texto)
    if not nodos_solos:
        return {"ok": False, "error": "Formato no reconocido — pon un nodo por línea"}
    resueltos = resolver_nodos(sgs_data["nodos"], nodos_solos)
    return {"ok": True, "resueltos": resueltos}


@app.route("/manual/paso2/<sid>/confirmar", methods=["POST"])
@login_required
def manual_paso2_confirmar(sid):
    from manual_balance import get_sesion, save_sesion, detectar_nodos_solos, resolver_nodos
    sesion = get_sesion(sid)
    if not sesion:
        return render_template("index.html", tab="sgs",
                               error_sgs_libre="Sesión expirada.")
    omitir = request.form.get("omitir_mat") == "1"
    pros_sel   = request.form.getlist("pro_sel")
    pros_extra = [v.strip() for v in request.form.getlist("pro_extra") if v.strip()]
    todos = list(dict.fromkeys(pros_sel + pros_extra))

    # texto_nodos viene del form (enviado por JS) o de la sesión previa (cuando clickaron "Buscar")
    texto_nodos_form = (request.form.get("texto_nodos") or "").strip()
    if texto_nodos_form:
        sesion["texto_nodos"] = texto_nodos_form

    sgs_data = sesion.get("sgs_data")
    nodos_cache = (sgs_data or {}).get("nodos", [])

    sesion["rows_mat"] = []
    sesion["modo_mat"] = "nodo_solo"

    # sgs_data["nodos"] NUNCA se toca aquí — es el fetch completo para verificación MO.
    # Los nodos de materiales van a sesion["nodos_mat"] por separado.

    if omitir:
        # "Solo MO" — sin materiales
        sesion["nodos_mat"] = []
        sesion["nodos_sel"] = []

    elif not todos:
        # No llegaron checkboxes — intentar resolver desde el textarea (fallback servidor)
        texto_nodos = (texto_nodos_form or sesion.get("texto_nodos", "")).strip()
        if texto_nodos and nodos_cache:
            nodos_solos = detectar_nodos_solos(texto_nodos)
            if nodos_solos:
                resueltos = resolver_nodos(nodos_cache, nodos_solos)
                pros_ok = [r["pro"] for r in resueltos.get("encontrados", [])]
                nodos_filtrados = [n for n in nodos_cache if n.get("code") in set(pros_ok)]
                sesion["nodos_mat"] = nodos_filtrados
                sesion["nodos_sel"] = [n.get("code") for n in nodos_filtrados]
            else:
                sesion["nodos_mat"] = []
                sesion["nodos_sel"] = []
        else:
            sesion["nodos_mat"] = []
            sesion["nodos_sel"] = []

    else:
        # PROs seleccionados explícitamente para materiales
        pros_en_cache = {n.get("code") for n in nodos_cache}
        pros_faltantes = [p for p in todos if p not in pros_en_cache]
        nodos_extra = []
        if pros_faltantes:
            try:
                cliente = _get_sgs_cliente()
                for pro in pros_faltantes:
                    p = cliente.buscar_por_codigo(pro)
                    if p:
                        d = cliente.balance_completo(p["id"])
                        nodos_extra.extend(d.get("nodos", []))
            except Exception:
                pass  # best effort — usar lo que haya en cache
        nodos_filtrados = [n for n in nodos_cache if n.get("code") in set(todos)] + nodos_extra
        sesion["nodos_mat"] = nodos_filtrados
        sesion["nodos_sel"] = [n.get("code") for n in nodos_filtrados]

    sesion["nodos_pendientes_sgs"] = request.form.getlist("pendiente_sgs")
    cedulas = request.form.getlist("lider_cedula")
    nombres = request.form.getlist("lider_nombre")
    pcts    = request.form.getlist("lider_pct")
    sesion["lideres"] = [
        {"cedula": c.strip(), "nombre": n.strip(), "pct": p}
        for c, n, p in zip(cedulas, nombres, pcts)
        if c.strip() or n.strip()
    ]
    sup_form = request.form.get("supervisor", "").strip()
    if sup_form and sup_form != "—":
        sesion["supervisor"] = sup_form
    save_sesion(sid)
    return redirect(url_for("manual_preview", sid=sid))


@app.route("/manual/nodos/<sid>", methods=["GET", "POST"])
@login_required
def manual_nodos(sid):
    import json as _json
    from manual_balance import get_sesion, save_sesion, resolver_nodos, iniciar_fetch_sgs

    sesion = get_sesion(sid)
    if not sesion:
        return render_template("index.html", tab="sgs",
                               error_sgs_libre="Sesión expirada — ingresa el código del proyecto nuevamente.")

    # POST: el usuario confirmó la selección de nodos (checkboxes + PROs manuales)
    if request.method == "POST":
        pros_sel   = request.form.getlist("pro_sel")        # PROs seleccionados del fetch
        pros_extra = [v.strip() for v in request.form.getlist("pro_extra") if v.strip()]
        todos = list(dict.fromkeys(pros_sel + pros_extra))  # deduplicar, preservar orden

        if not todos:
            # re-mostrar con error
            pass
        else:
            sgs_data = sesion.get("sgs_data") or {}
            nodos_cache = sgs_data.get("nodos", [])

            # Los PROs extra que no estén en el cache → consultarlos ahora
            pros_en_cache = {n.get("code") for n in nodos_cache}
            pros_faltantes = [p for p in todos if p not in pros_en_cache]

            nodos_extra = []
            errores_pro = []
            if pros_faltantes:
                try:
                    cliente = _get_sgs_cliente()
                    for pro in pros_faltantes:
                        proy = cliente.buscar_por_codigo(pro)
                        if proy:
                            nodo_data = cliente.balance_completo(proy["id"])
                            nodos_extra.extend(nodo_data.get("nodos", []))
                        else:
                            errores_pro.append(f"PRO '{pro}' no encontrado en SGS")
                except Exception as e:
                    errores_pro.append(f"Error consultando SGS: {e}")

            if errores_pro:
                # Re-mostrar pantalla con errores
                sgs_ready = sesion.get("sgs_ready")
                nodos_resueltos = resolver_nodos(nodos_cache, [r["nodo"] for r in sesion.get("rows_mo", [])])
                return render_template("manual_nodos.html", sesion=sesion, sid=sid,
                                       sgs_ready=sgs_ready, nodos_resueltos=nodos_resueltos,
                                       errores_pro=errores_pro)

            # Filtrar nodos del cache por PROs seleccionados + agregar extras
            nodos_filtrados = [n for n in nodos_cache if n.get("code") in set(todos)]
            nodos_filtrados += nodos_extra

            padre_data = (sesion.get("sgs_data") or {}).get("padre") or sesion["padre"]
            sesion["sgs_data"]  = {"padre": padre_data, "nodos": nodos_filtrados}
            sesion["nodos_sel"] = [n.get("code") for n in nodos_filtrados]
            sesion["modo_mat"]  = "nodo_solo"
            sesion["rows_mat"]  = []
            save_sesion(sid)
            return redirect(url_for("manual_preview", sid=sid))

    # GET: mostrar pantalla de selección
    sgs_ready = sesion.get("sgs_ready")  # None | False | True
    sgs_error = sesion.get("sgs_error")
    nodos_resueltos = None

    if sgs_ready is True:
        # sgs_data puede ser None si el fetch terminó en otro worker y el disco no lo guardó
        if sesion.get("sgs_data") is None and not sgs_error:
            try:
                cliente  = _get_sgs_cliente()
                data     = cliente.balance_completo(sesion["parent_id"])
                sesion["sgs_data"] = data
                save_sesion(sid)
            except Exception as e:
                sgs_error = str(e)
        if sesion.get("sgs_data") and not sgs_error:
            sgs_data   = sesion.get("sgs_data") or {}
            nodos_sgs  = sgs_data.get("nodos", [])
            mo_nodos   = [r["nodo"] for r in sesion.get("rows_mo", [])]
            nodos_resueltos = resolver_nodos(nodos_sgs, mo_nodos)

    return render_template("manual_nodos.html", sesion=sesion, sid=sid,
                           sgs_ready=sgs_ready, sgs_error=sgs_error,
                           nodos_resueltos=nodos_resueltos, errores_pro=[])


@app.route("/api/lideres-sugeridos/<sid>")
@login_required
def api_lideres_sugeridos(sid):
    """Detecta líderes desde el partner_id de las bandejas de cada nodo
    (el Equipo del proyecto en SGS) y enriquece con cédula via res.partner.vat."""
    import re as _re
    from manual_balance import get_sesion
    sesion = get_sesion(sid)
    if not sesion or not sesion.get("sgs_data"):
        return {"ok": False, "error": "Sin datos SGS"}

    cedula_re = _re.compile(r"^C\d+$", _re.IGNORECASE)

    # ── Fuente principal: partner_id de las bandejas (ya está en sesión) ──────
    # Cada bandeja tiene el partner que recibió/usó los materiales = el líder real
    ban_partners: dict = {}  # partner_id → nombre
    for nodo in sesion["sgs_data"].get("nodos", []):
        for ban in nodo.get("bandejas", []):
            partner = ban.get("partner_id")
            if isinstance(partner, list) and len(partner) >= 2 and partner[0]:
                ban_partners[partner[0]] = partner[1]

    # ── Fallback: responsible_id del nodo si no hay bandejas ──────────────────
    responsibles: dict = {}  # user_id → nombre
    if not ban_partners:
        for nodo in sesion["sgs_data"].get("nodos", []):
            resp = nodo.get("responsible_id")
            if isinstance(resp, list) and len(resp) >= 2 and resp[0]:
                responsibles[resp[0]] = resp[1]

    if not ban_partners and not responsibles:
        return {"ok": True, "lideres": []}

    try:
        cliente = _get_sgs_cliente()

        # Inicializar entradas desde bandejas (son res.partner directamente)
        pid_to_ldr: dict = {
            pid: {"cedula": "", "nombre": nombre, "pct": 0, "cedula_valida": False}
            for pid, nombre in ban_partners.items()
        }

        # Para el fallback: resolver res.users → partner_id
        if responsibles:
            try:
                users = cliente.read("res.users", list(responsibles.keys()), ["id", "partner_id"]) or []
                for u in users:
                    pid_raw = u.get("partner_id")
                    if isinstance(pid_raw, list) and pid_raw and pid_raw[0] not in pid_to_ldr:
                        pid_to_ldr[pid_raw[0]] = {
                            "cedula": "", "nombre": responsibles.get(u["id"], ""),
                            "pct": 0, "cedula_valida": False,
                        }
            except Exception:
                pass

        # Enriquecer todos con cédula desde res.partner.vat
        if pid_to_ldr:
            partners = cliente.read("res.partner", list(pid_to_ldr.keys()), ["id", "name", "vat"]) or []
            for p in partners:
                pid = p["id"]
                if pid not in pid_to_ldr:
                    continue
                cedula = _re.sub(r"^[Cc]{2}(\d)", r"C\1", (p.get("vat") or "")).upper()
                pid_to_ldr[pid]["cedula"] = cedula
                pid_to_ldr[pid]["cedula_valida"] = bool(cedula_re.match(cedula))
                if p.get("name"):
                    pid_to_ldr[pid]["nombre"] = p["name"]

        # Deduplicar por nombre y devolver (con y sin cédula)
        seen: set = set()
        lideres = []
        for ldr in pid_to_ldr.values():
            if not ldr["nombre"] or ldr["nombre"] in seen:
                continue
            seen.add(ldr["nombre"])
            lideres.append(ldr)

        return {"ok": True, "lideres": lideres}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/api/buscar-lider")
@login_required
def api_buscar_lider():
    """AJAX: busca líderes/socios por nombre o cédula en SGS (res.partner)."""
    import re as _re
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return {"ok": True, "results": []}
    try:
        cliente = _get_sgs_cliente()
        # Normalizar: CC11111 → C11111
        q_norm = _re.sub(r"^[Cc]{2}(\d)", r"C\1", q)

        by_name = cliente.search_read(
            "res.partner", [["name", "ilike", q]],
            ["id", "name", "vat"], limit=10)
        by_vat = cliente.search_read(
            "res.partner", [["vat", "ilike", q_norm]],
            ["id", "name", "vat"], limit=5)

        seen = set()
        results = []
        for r in (by_name + by_vat):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            cedula = r.get("vat") or ""
            cedula = _re.sub(r"^[Cc]{2}(\d)", r"C\1", cedula)
            results.append({
                "id":     r["id"],
                "nombre": r.get("name", ""),
                "cedula": cedula,
            })
        return {"ok": True, "results": results[:10]}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/api/validar-pro")
@login_required
def api_validar_pro():
    """AJAX: verifica si un código PRO existe en SGS. Retorna JSON."""
    pro = request.args.get("pro", "").strip().upper()
    if not pro:
        return {"ok": False, "error": "Código vacío"}, 400
    try:
        cliente = _get_sgs_cliente()
        proy = cliente.buscar_por_codigo(pro)
        if proy:
            return {"ok": True, "name": proy.get("name", ""), "code": proy.get("code", pro)}
        return {"ok": False, "error": f"'{pro}' no encontrado en SGS"}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/manual/mat/<sid>", methods=["GET", "POST"])
@login_required
def manual_mat(sid):
    from manual_balance import get_sesion, parsear_mat, detectar_nodos_solos, filtrar_nodos_sgs, save_sesion
    sesion = get_sesion(sid)
    if not sesion:
        return render_template("index.html", tab="sgs",
                               error_sgs_libre="Sesión expirada — ingresa el código del proyecto nuevamente.")

    if request.method == "POST":
        texto  = request.form.get("texto_mat", "").strip()
        omitir = request.form.get("omitir_mat") == "1"

        if omitir or not texto:
            sesion["rows_mat"] = []
            sesion["errs_mat"] = []
            sesion["modo_mat"] = "nodo_solo"
            # Nodos de la MO como fallback
            sesion["nodos_sel"] = list({r["nodo"] for r in sesion["rows_mo"]})
        else:
            nodos_solos = detectar_nodos_solos(texto)
            if nodos_solos:
                sesion["modo_mat"]  = "nodo_solo"
                sesion["nodos_sel"] = nodos_solos
                sesion["rows_mat"]  = []
                sesion["errs_mat"]  = []
            else:
                rows, errors = parsear_mat(texto)
                sesion["rows_mat"]  = rows
                sesion["errs_mat"]  = errors
                sesion["modo_mat"]  = "manual_sap"
                sesion["nodos_sel"] = list({r["nodo"] for r in rows})
                if errors:
                    return render_template("manual_mat.html", sesion=sesion, sid=sid,
                                           texto=texto, errors=errors)

        # Obtener datos SGS para los nodos seleccionados
        try:
            cliente   = _get_sgs_cliente()
            sgs_full  = cliente.balance_completo(sesion["parent_id"])
            if sesion["nodos_sel"]:
                nodos_f = filtrar_nodos_sgs(sgs_full["nodos"], sesion["nodos_sel"])
            else:
                nodos_f = sgs_full["nodos"]
            sesion["sgs_data"] = {"padre": sgs_full["padre"], "nodos": nodos_f}
        except Exception as e:
            return render_template("manual_mat.html", sesion=sesion, sid=sid,
                                   texto=texto, errors=[], error_sgs=f"Error consultando SGS: {e}")

        save_sesion(sid)
        return redirect(url_for("manual_preview", sid=sid))

    return render_template("manual_mat.html", sesion=sesion, sid=sid,
                           texto="", errors=[])


@app.route("/manual/preview/<sid>")
@login_required
def manual_preview(sid):
    from manual_balance import get_sesion, preparar_preview_materiales
    sesion = get_sesion(sid)
    if not sesion:
        return redirect(url_for("index") + "?tab=sgs")
    if not sesion.get("sgs_data"):
        sesion["sgs_data"] = {"padre": sesion.get("padre", {}), "nodos": []}
    preview_materiales = []
    try:
        # Usar solo los nodos que el usuario seleccionó para materiales
        nodos_mat = sesion.get("nodos_mat", [])
        preview_materiales = preparar_preview_materiales({"nodos": nodos_mat})
    except Exception:
        pass
    return render_template("manual_preview.html", sesion=sesion, sid=sid,
                           preview_materiales=preview_materiales)


@app.route("/manual/generar/<sid>", methods=["POST"])
@login_required
def manual_generar(sid):
    from manual_balance import get_sesion, generar_excel_manual
    sesion = get_sesion(sid)
    if not sesion or not sesion.get("sgs_data"):
        return redirect(url_for("index") + "?tab=sgs")

    excel_bytes = generar_excel_manual(sesion)
    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    lcl      = sesion["lcl_odm"]
    filename = f"balance_sgs_manual_{lcl}_{ts}.xlsx"

    token = str(uuid.uuid4())
    _archivos[token] = {"bytes": excel_bytes, "filename": filename}
    return redirect(url_for("descargar", token=token))


@app.route("/test-sgs")
@login_required
def test_sgs():
    """Diagnóstico: muestra estado de conexión SGS y busca PRO pasado por query string."""
    from integracion import cfg_desde_archivo
    from sgs_client import SGSClient
    import traceback

    resultado = {}
    pro = request.args.get("pro", "").strip()

    try:
        cfg = cfg_desde_archivo()
        resultado["config"] = {
            "url":   cfg.get("sgs_url", ""),
            "db":    cfg.get("sgs_db",  "(vacío — se auto-detecta)"),
            "login": cfg.get("sgs_login", ""),
        }
    except Exception as e:
        return {"error": f"cfg_desde_archivo falló: {e}"}, 500

    try:
        if not cfg["sgs_db"]:
            detected = SGSClient.detectar_db(cfg["sgs_url"])
            resultado["db_detectada"] = detected or "ninguna"
        cliente = _get_sgs_cliente()
        resultado["auth"] = "OK"
        resultado["uid"]  = cliente.uid
    except Exception as e:
        resultado["auth"] = f"FALLÓ: {e}"
        resultado["traceback"] = traceback.format_exc()
        return resultado, 200

    if pro:
        try:
            p = cliente.buscar_por_codigo(pro)
            resultado["proyecto"] = {"id": p["id"], "name": p.get("name"), "code": p.get("code")} if p else "no encontrado"
        except Exception as e:
            resultado["proyecto"] = f"error: {e}"

    return resultado, 200


@app.route("/health")
def health():
    return {"ok": True, "service": "balance-integrado"}


@app.route("/reiniciar", methods=["POST"])
def reiniciar():
    import threading
    def _exit():
        import time, os
        time.sleep(0.5)
        os._exit(3)  # código 3 → launcher.py reinicia el proceso
    threading.Thread(target=_exit, daemon=True).start()
    return ("", 204)


@app.route("/debug")
@login_required
def debug():
    """Verifica qué versión de integracion.py está cargada."""
    import sys, os, inspect
    from integracion import BalanceIntegrado, _normalizar_desc, _construir_desc_idx
    src = inspect.getsource(BalanceIntegrado._generar_excel)
    hojas = [l.strip() for l in src.splitlines() if "hoja" in l]
    modulo = sys.modules.get("integracion")
    return {
        "integracion_file": modulo.__file__ if modulo else "no cargado",
        "hojas": hojas,
        "tiene_normalizar_desc": hasattr(modulo, "_normalizar_desc"),
        "tiene_construir_desc_idx": hasattr(modulo, "_construir_desc_idx"),
        "test_normalizar": _normalizar_desc("Fusible Dual 1.0A 15 kV"),
    }


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Añadir /nuevo al path para que `from integracion import ...` funcione
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)

    port = int(os.environ.get("PORT", 5002))
    print(f"[/nuevo] Iniciando en http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
