"""
blueprint_lote.py — Liquidación en lote de múltiples DFs desde un solo archivo Excel.
Formato: tabla plana con columnas DF | NODO | PRESTACION | CANTIDAD | LIDER.
Monta en /julian/lote/. Solo MO. No modifica el flujo habitual del módulo julian.
"""

import io
import logging
import threading
import uuid
from datetime import datetime

import openpyxl
from flask import (Blueprint, render_template, request,
                   redirect, url_for, flash, jsonify)

from modulos.verificador import verificar_fila_mo
from browser_client import liquidar_mo_parallel_por_proyecto

# Utilidades del blueprint principal del módulo julian (sin dependencia circular —
# blueprint_lote importa de blueprint, no al revés).
from modulos.julian.blueprint import (
    _sgs,
    _nuevo_sgs_readonly,
    _obtener_storage_state_activo_j,
    sesion_activa,
    MO_MAX_PROJECT_WORKERS,
)
from modulos.julian.parser_presupuesto import parsear_mo_presupuesto

log = logging.getLogger("julian_lote")

# ── Blueprint ─────────────────────────────────────────────────────────────────
julian_lote = Blueprint(
    "julian_lote",
    __name__,
    template_folder="../../templates/julian",
    url_prefix="/julian/lote",
)

# ── Estado en memoria ─────────────────────────────────────────────────────────
_sesiones_lote: dict = {}
_trabajos_lote:  dict = {}


# ── Helpers internos ──────────────────────────────────────────────────────────

def _pro_key(ck: dict, row: dict) -> str:
    """Clave única de proyecto: pro_code SGS o DF//NODO como fallback."""
    pro = (ck.get("pro_code") or "") if ck else ""
    if pro:
        return pro
    return f"{row.get('_odm','?')}//N{row.get('pro','?')}"


def _agrupar_por_df(rows_mo: list, checks_mo: list) -> list:
    """
    Agrupa por DF y dentro de cada DF por proyecto (pro_code/pid).
    Retorna:
      [{df, proyectos: [{pro_key, pro_code, pid, nodo, partner_nombre,
                         partner_cedula, filas: [(idx, row, ck)]}]}]
    """
    from collections import OrderedDict
    dfs: dict = OrderedDict()

    for i, row in enumerate(rows_mo):
        df = row.get("_odm", "?")
        ck = checks_mo[i] if i < len(checks_mo) else None
        key = _pro_key(ck, row)

        if df not in dfs:
            dfs[df] = OrderedDict()
        if key not in dfs[df]:
            dfs[df][key] = {
                "pro_key":        key,
                "pro_code":       (ck.get("pro_code") or "") if ck else "",
                "pid":            (ck.get("pid") or 0) if ck else 0,
                "nodo":           row.get("pro", "?"),
                "partner_nombre": (ck.get("partner_nombre") or "") if ck else "",
                "partner_cedula": (ck.get("partner_cedula") or "") if ck else "",
                "filas":          [],
            }
        proj = dfs[df][key]
        # Tomar el primer partner_nombre no vacío
        if not proj["partner_nombre"] and ck and ck.get("partner_nombre"):
            proj["partner_nombre"] = ck["partner_nombre"]
            proj["partner_cedula"] = ck.get("partner_cedula") or ""
        proj["filas"].append((i, row, ck))

    return [{"df": df, "proyectos": list(projs.values())}
            for df, projs in dfs.items()]


def _items_mo_lote(checks_mo: list, idx_set: set, form=None) -> tuple[list, list]:
    """
    Construye items_mo para liquidar_mo_parallel_por_proyecto.

    Asignación de líder por proyecto (pro_key), no por fila individual.
    Campos de form leídos por pro_key:
      lider_mode_{pro_key}     → "sgs" | "otro" | "dividir"
      lider_b_nombre_{pro_key} → nombre líder alternativo
      lider_b_ced_{pro_key}    → cédula líder alternativo
      pct_a_{pro_key}          → % para líder A/SGS (1-99), solo en modo dividir
    """
    items, errores = [], []
    asig_cache: dict = {}  # pro_key -> {mode, lider_b, lider_b_ced, pct_a}

    def _asig(pro_key: str) -> dict:
        if pro_key not in asig_cache:
            if form:
                mode    = form.get(f"lider_mode_{pro_key}", "sgs").strip()
                lider_b = form.get(f"lider_b_nombre_{pro_key}", "").strip()
                ced_b   = form.get(f"lider_b_ced_{pro_key}", "").strip()
                try:    pct_a = float(form.get(f"pct_a_{pro_key}", 50))
                except: pct_a = 50.0
            else:
                mode, lider_b, ced_b, pct_a = "sgs", "", "", 50.0
            asig_cache[pro_key] = {"mode": mode, "lider_b": lider_b,
                                   "lider_b_ced": ced_b, "pct_a": pct_a}
        return asig_cache[pro_key]

    for idx in sorted(idx_set):
        if idx >= len(checks_mo):
            continue
        ck  = checks_mo[idx]
        row = ck.get("row", {})
        if not ck.get("ok") or ck.get("ya_liquidada"):
            continue

        partner_sgs = ck.get("partner_nombre", "")
        if not partner_sgs:
            lideres = ck.get("lideres") or []
            partner_sgs = lideres[0]["nombre"] if lideres else ""
        if not partner_sgs:
            errores.append(f"{ck.get('row',{}).get('codigo','?')}: sin líder SGS — omitido")
            continue

        cantidad  = float(row.get("cantidad", 1))
        pro_key   = _pro_key(ck, row)
        asig      = _asig(pro_key)
        mode      = asig["mode"]
        lider_b   = asig["lider_b"]
        ced_b     = asig["lider_b_ced"]
        pct_a     = asig["pct_a"]
        cod       = row.get("codigo", f"idx={idx}")

        base = {
            "tipo":                "MO",
            "idx":                 idx,
            "pid":                 ck.get("pid"),
            "act_id":              ck.get("act_id"),
            "act_code":            cod,
            "pro_code":            ck.get("pro_code", ""),
            "cantidad_proyectada": cantidad,
            "partner_cedula":      ck.get("partner_cedula", ""),
        }

        if mode == "otro":
            if not lider_b:
                errores.append(f"{pro_key}: modo 'otro líder' sin nombre — usando SGS")
                mode = "sgs"
            else:
                items.append({**base, "cantidad": cantidad, "partner_nombre": lider_b,
                               "partner_cedula": ced_b})
                continue

        if mode == "dividir":
            if not lider_b:
                errores.append(f"{pro_key}: modo dividir sin líder B — usando solo SGS")
                mode = "sgs"
            elif not (1 <= pct_a <= 99):
                pct_a = 50.0
            if mode == "dividir":
                cant_a = round(cantidad * pct_a / 100.0, 4)
                cant_b = round(cantidad - cant_a, 4)
                if cant_a > 0 and cant_b > 0:
                    items.append({**base, "cantidad": cant_a, "partner_nombre": partner_sgs,
                                  "split": {"parte": "A", "pct": pct_a, "total": cantidad}})
                    items.append({**base, "cantidad": cant_b, "partner_nombre": lider_b,
                                  "partner_cedula": ced_b,
                                  "split": {"parte": "B", "pct": round(100 - pct_a, 4), "total": cantidad}})
                    continue
                else:
                    errores.append(f"{pro_key}/{cod}: reparto genera cero — usando solo SGS")

        # Modo "sgs" (default o fallback)
        items.append({**base, "cantidad": cantidad, "partner_nombre": partner_sgs})

    return items, errores


def _ejecutar_liquidacion_bg_lote(sid: str, items_mo: list, storage_state: dict):
    """Hilo de fondo — liquida items_mo y actualiza _trabajos_lote[sid]."""
    job = _trabajos_lote.get(sid)
    if not job:
        return

    def _cancelled():
        return _trabajos_lote.get(sid, {}).get("cancelado", False)

    resultados = []
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

        resultados_mo = liquidar_mo_parallel_por_proyecto(
            items_mo,
            max_workers=MO_MAX_PROJECT_WORKERS,
            storage_state=storage_state,
            progress_cb=_progress_mo,
            should_cancel=_cancelled,
            sgs_client_factory=_nuevo_sgs_readonly,
        )
        resultados.extend(resultados_mo)

        for i, res in enumerate(resultados_mo):
            job["items_mo"][i]["estado"] = "ok" if res.get("ok") else "error"
            if not res.get("ok"):
                job["items_mo"][i]["error"] = res.get("error", "Error desconocido")[:120]

    except Exception as ex:
        log.error(f"[LOTE BG] sid={sid[:8]} error global: {ex}", exc_info=True)
        job["error_global"] = str(ex)

    s = _sesiones_lote.get(sid)
    if s:
        s["resultados_liquidacion"] = resultados
    job["resultados"] = resultados
    job["estado"]     = "listo"

    ok_n  = sum(1 for r in resultados if r.get("ok"))
    err_n = len(resultados) - ok_n
    log.info(f"[LOTE BG] sid={sid[:8]} listo — ok={ok_n} err={err_n}")


# ── Rutas ─────────────────────────────────────────────────────────────────────

@julian_lote.route("/")
def index():
    return redirect(url_for("julian_lote.subir"))


@julian_lote.route("/subir", methods=["GET", "POST"])
def subir():
    if request.method == "POST":
        f = request.files.get("excel")
        if not f or not f.filename.endswith((".xlsx", ".xlsm", ".xls")):
            flash("Sube un archivo Excel (.xlsx / .xlsm / .xls)")
            return redirect(url_for("julian_lote.subir"))

        try:
            wb = openpyxl.load_workbook(io.BytesIO(f.read()), data_only=True)
        except Exception as ex:
            flash(f"Error leyendo Excel: {ex}")
            return redirect(url_for("julian_lote.subir"))

        rows_mo, errs_mo = parsear_mo_presupuesto(wb)

        if not rows_mo:
            flash("No se detectaron filas MO con formato multi-DF. "
                  "El archivo debe tener columnas: DF | NODO | PRESTACION | CANTIDAD | LIDER.")
            return redirect(url_for("julian_lote.subir"))

        dfs_unicos = sorted(set(r.get("_odm", "") for r in rows_mo if r.get("_odm")))

        sid = str(uuid.uuid4())
        _sesiones_lote[sid] = {
            "filename":     f.filename,
            "rows_mo":      rows_mo,
            "errs_mo":      errs_mo,
            "checks_mo":    [],
            "verificado_at": None,
            "creado_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
            "dfs_unicos":   dfs_unicos,
        }

        log.info(f"[LOTE] Cargado {f.filename} | sid={sid[:8]} | "
                 f"rows={len(rows_mo)} DFs={len(dfs_unicos)}")
        return redirect(url_for("julian_lote.verificar", sid=sid))

    return render_template("julian/lote_subir.html")


@julian_lote.route("/verificar/<sid>")
def verificar(sid):
    s = _sesiones_lote.get(sid)
    if not s:
        flash("Sesión no encontrada.")
        return redirect(url_for("julian_lote.subir"))

    try:
        c = _sgs()
    except Exception as ex:
        flash(f"Error conectando a SGS: {ex}")
        return redirect(url_for("julian_lote.subir"))

    checks_mo = []
    for row in s["rows_mo"]:
        try:
            ck = verificar_fila_mo(c, row)
        except Exception as ex:
            ck = {"row": row, "ok": False, "errores": [str(ex)], "advertencias": []}
        checks_mo.append(ck)

    s["checks_mo"]    = checks_mo
    s["verificado_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ok_n  = sum(1 for ck in checks_mo if ck.get("ok"))
    err_n = len(checks_mo) - ok_n
    log.info(f"[LOTE] Verificación completa sid={sid[:8]} ok={ok_n} err={err_n}")

    return redirect(url_for("julian_lote.ver", sid=sid))


@julian_lote.route("/ver/<sid>")
def ver(sid):
    s = _sesiones_lote.get(sid)
    if not s:
        flash("Sesión no encontrada.")
        return redirect(url_for("julian_lote.subir"))

    checks_mo = s.get("checks_mo") or []

    # Si aún no se verificó, lanzar verificación
    if not checks_mo and s.get("rows_mo"):
        return redirect(url_for("julian_lote.verificar", sid=sid))

    grupos    = _agrupar_por_df(s["rows_mo"], checks_mo)
    ok_total  = sum(1 for ck in checks_mo if ck.get("ok"))
    err_total = len(checks_mo) - ok_total

    return render_template("julian/lote_ver.html",
        sid=sid, s=s,
        grupos=grupos,
        ok_total=ok_total,
        err_total=err_total,
        sesion_browser_ok=sesion_activa(),
    )


@julian_lote.route("/liquidar/<sid>", methods=["POST"])
def liquidar(sid):
    s = _sesiones_lote.get(sid)
    if not s:
        flash("Sesión no encontrada.")
        return redirect(url_for("julian_lote.subir"))

    if not sesion_activa():
        flash("El browser visible no tiene sesión activa en SGS. "
              "Inicia sesión en la ventana del browser y espera el indicador verde.")
        return redirect(url_for("julian_lote.ver", sid=sid))

    idx_mo = {int(i) for i in request.form.getlist("idx_mo") if i.isdigit()}
    if not idx_mo:
        flash("Selecciona al menos un ítem para liquidar.")
        return redirect(url_for("julian_lote.ver", sid=sid))

    checks_mo = s.get("checks_mo", [])
    items_mo, errores = _items_mo_lote(checks_mo, idx_mo, form=request.form)

    if errores:
        for e in errores:
            flash(f"Advertencia: {e}")

    if not items_mo:
        flash("Sin ítems liquidables en la selección.")
        return redirect(url_for("julian_lote.ver", sid=sid))

    storage_state = _obtener_storage_state_activo_j()
    if not storage_state:
        flash("No se pudo obtener la sesión del browser. Verifica que esté logueado.")
        return redirect(url_for("julian_lote.ver", sid=sid))

    job = {
        "estado":    "corriendo",
        "items_mo":  [{"estado": "pendiente", "error": None, "worker_id": None}
                      for _ in items_mo],
        "resultados": [],
        "cancelado":  False,
        "error_global": None,
    }
    _trabajos_lote[sid] = job

    t = threading.Thread(
        target=_ejecutar_liquidacion_bg_lote,
        args=(sid, items_mo, storage_state),
        daemon=True,
    )
    t.start()
    log.info(f"[LOTE] Liquidación iniciada sid={sid[:8]} — {len(items_mo)} ítems")
    return redirect(url_for("julian_lote.esperar", sid=sid))


@julian_lote.route("/esperar/<sid>")
def esperar(sid):
    s = _sesiones_lote.get(sid)
    if not s:
        flash("Sesión no encontrada.")
        return redirect(url_for("julian_lote.subir"))
    job = _trabajos_lote.get(sid, {})
    return render_template("julian/lote_esperar.html", sid=sid, s=s, job=job)


@julian_lote.route("/progreso/<sid>")
def progreso(sid):
    job = _trabajos_lote.get(sid)
    if not job:
        return jsonify({"estado": "no_encontrado", "total": 0, "hechos": 0, "errores": 0})
    items  = job.get("items_mo", [])
    total  = len(items)
    hechos = sum(1 for it in items if it["estado"] in ("ok", "error"))
    err_n  = sum(1 for it in items if it["estado"] == "error")
    return jsonify({
        "estado":       job.get("estado", "corriendo"),
        "total":        total,
        "hechos":       hechos,
        "errores":      err_n,
        "error_global": job.get("error_global"),
        "items":        items,
    })


@julian_lote.route("/resultado/<sid>")
def resultado(sid):
    s = _sesiones_lote.get(sid)
    if not s:
        flash("Sesión no encontrada.")
        return redirect(url_for("julian_lote.subir"))
    job        = _trabajos_lote.get(sid, {})
    resultados = job.get("resultados") or s.get("resultados_liquidacion") or []
    ok_n   = sum(1 for r in resultados if r.get("ok"))
    err_n  = len(resultados) - ok_n
    return render_template("julian/lote_resultado.html",
        sid=sid, s=s,
        resultados=resultados,
        ok_n=ok_n,
        err_n=err_n,
    )


@julian_lote.route("/buscar_lider", methods=["POST"])
def buscar_lider():
    """
    AJAX — busca partners en SGS por cédula o por nombre.
    Body: {cedula: "...", nombre: "..."}  (al menos uno requerido)
    Retorna {ok, resultados: [{nombre, vat}], advertencia}
    """
    data   = request.get_json() or {}
    cedula = str(data.get("cedula") or "").strip()
    nombre = str(data.get("nombre") or "").strip()

    if not cedula and not nombre:
        return jsonify({"ok": False, "error": "Ingresa cédula o nombre"}), 400

    advertencia = ""
    try:
        c = _sgs()
        partners = []

        if cedula:
            advertencia = "" if "C" in cedula.upper() else (
                f"'{cedula}' sin 'C' — puede no encontrar el registro correcto.")
            partners = c.search_read("res.partner", [["vat", "=", cedula]],
                                     ["id", "name", "vat"], limit=10)
            if not partners:
                solo_num = cedula.upper().lstrip("C").strip()
                partners = c.search_read("res.partner", [["vat", "ilike", solo_num]],
                                         ["id", "name", "vat"], limit=10)

        if not partners and nombre:
            # Búsqueda por nombre: palabras clave separadas
            termino = nombre.strip().upper()
            partners = c.search_read("res.partner", [["name", "ilike", termino]],
                                     ["id", "name", "vat"], limit=10)

        if not partners:
            q = cedula or nombre
            return jsonify({"ok": False,
                            "error": f"No se encontró nadie con '{q}' en SGS.",
                            "advertencia": advertencia})

        # Preferir los que tienen C en vat (Regla C)
        con_c = [p for p in partners if "C" in str(p.get("vat") or "").upper()]
        ordenados = con_c + [p for p in partners if p not in con_c]

        resultados = [{"nombre": p["name"], "vat": p.get("vat") or ""}
                      for p in ordenados[:8]]

        return jsonify({"ok": True, "resultados": resultados, "advertencia": advertencia})

    except Exception as ex:
        log.error(f"[LOTE BUSCAR LIDER] {ex}")
        return jsonify({"ok": False, "error": str(ex)}), 500


@julian_lote.route("/cancelar/<sid>", methods=["POST"])
def cancelar(sid):
    job = _trabajos_lote.get(sid)
    if job:
        job["cancelado"] = True
    return redirect(url_for("julian_lote.esperar", sid=sid))
