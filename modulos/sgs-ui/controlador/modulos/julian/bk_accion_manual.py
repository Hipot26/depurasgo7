"""
accion_manual.py — Flujo de entrada manual para el módulo julian.
Permite al usuario pegar filas del Excel del supervisor (Nodo | Prestación | Cantidad)
sin necesidad de subir un archivo Excel completo.

Monta en /julian/manual/ como Blueprint separado.
Escribe la sesión final directamente en _sesiones de blueprint.py,
de modo que el flujo normal (resumen → verificar → liquidar) funciona sin cambios.
"""

import json
import logging
import re
import uuid
from datetime import datetime

from flask import (Blueprint, jsonify, redirect, render_template,
                   request, flash, url_for)

from modulos.julian.blueprint import _sesiones
from modulos.julian.parser_mdo_mat import get_precios
from modulos.julian.parser_presupuesto import _extraer_lcl

log = logging.getLogger("julian_manual")

julian_manual = Blueprint(
    "julian_manual",
    __name__,
    template_folder="../../templates/julian",
    url_prefix="/julian/manual",
)

# Sesiones intermedias del flujo manual (antes de transferir a _sesiones)
_sm: dict = {}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalizar_lcl(lcl: str) -> str:
    """
    Convierte 'LCL6300985782' → '6300985782' (el valor que SGS guarda
    en order_number y que _resolver_pid_nodo usa para buscar proyectos).
    """
    extracted = _extraer_lcl(lcl)
    return extracted or lcl


def _pf(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("$", "").replace("\xa0", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        partes = s.split(",")
        s = s.replace(",", ".") if len(partes) == 2 and len(partes[1]) <= 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _es_nodo_val(n: float) -> bool:
    """True si el número puede ser un nodo (entero entre 1 y 9999)."""
    return n == int(n) and 1 <= int(n) <= 9999


def _detectar_nodos_solos(texto: str) -> list[str] | None:
    """
    Retorna lista de nodos únicos si TODO el texto son números de nodo
    (un número por token, separados por espacios/tabs/newlines).
    Retorna None si el texto tiene columnas mixtas o texto no numérico.
    """
    tokens = [t.strip() for t in re.split(r'[\t\n,; ]+', texto.strip()) if t.strip()]
    if not tokens:
        return None
    nodos = []
    for t in tokens:
        n = _pf(t)
        if n is None or not _es_nodo_val(n):
            return None   # no es nodo-only
        nodos.append(str(int(n)))
    # Deduplicar preservando orden
    seen: set = set()
    unicos = []
    for n in nodos:
        if n not in seen:
            seen.add(n)
            unicos.append(n)
    return unicos


def _parsear_lineas_mo(texto: str, lcl_odm: str) -> tuple[list, list]:
    """
    Parsea texto pegado de Excel: Nodo | Prestación (QP) | Cantidad
    Retorna (rows_mo, errs).
    """
    rows, errs = [], []
    precios = get_precios()
    vistos: set = set()

    for i, linea in enumerate(texto.splitlines(), 1):
        linea = linea.strip()
        if not linea:
            continue

        partes = [p.strip() for p in linea.split("\t")]
        if len(partes) < 3:
            partes = [p.strip() for p in re.split(r"\s{2,}", linea)]
        if len(partes) < 3:
            errs.append(f"Línea {i}: necesita Nodo | Prestación | Cantidad — ignorada")
            continue

        nodo = str(partes[0]).strip()
        qp   = str(partes[1]).strip().upper()
        qty  = _pf(partes[2])

        if not nodo or not qp or qty is None or qty == 0:
            continue

        clave = (nodo, qp)
        if clave in vistos:
            for r in rows:
                if r["pro"] == nodo and r["codigo"] == qp:
                    r["cantidad"] += qty
                    break
            continue
        vistos.add(clave)

        info = precios.get(qp, {})
        rows.append({
            "fila":           i,
            "pro":            nodo,
            "codigo":         qp,
            "cantidad":       qty,
            "lider":          "",
            "qp_code":        qp,
            "baremo":         "",
            "descripcion":    info.get("descripcion", ""),
            "tipo_actividad": "",
            "_odm":           lcl_odm,
        })

    return rows, errs


def _parsear_lineas_mat(texto: str, lcl_odm: str) -> tuple[list, list]:
    """
    Parsea texto pegado de Excel para materiales.

    Formatos aceptados (Tab-separado):
      4 col: Nodo | SAP | Descripción | Cantidad
      3 col: Nodo | SAP | Cantidad     (detectado si col1 es nodo, col2 es SAP)
             SAP  | Descripción | Cant (si col1 no es nodo)
      2 col: Nodo | SAP              (cantidad implícita = 1, si col2 parece SAP)

    Nodo: entero ≤ 9999. SAP: entero ≥ 10000 (6 dígitos típicamente).
    """
    rows, errs = [], []
    vistos: set = set()

    for i, linea in enumerate(texto.splitlines(), 1):
        linea = linea.strip()
        if not linea:
            continue

        partes = [p.strip() for p in linea.split("\t")]

        nodo, sap, desc, qty = None, None, "", None

        if len(partes) >= 4:
            # Siempre: Nodo | SAP | Desc | Qty
            nodo = str(partes[0]).strip()
            sap  = str(partes[1]).strip()
            desc = str(partes[2]).strip()
            qty  = _pf(partes[3])

        elif len(partes) == 3:
            # Distinguir: Nodo|SAP|Qty  vs  SAP|Desc|Qty
            n0 = _pf(partes[0])
            n1 = _pf(partes[1])
            n2 = _pf(partes[2])

            if n0 is not None and _es_nodo_val(n0) and n1 is not None and not _es_nodo_val(n1):
                # Col1 = nodo (pequeño), col2 = SAP (grande) → Nodo|SAP|Qty
                nodo = str(int(n0))
                sap  = str(partes[1]).strip()
                desc = ""
                qty  = n2
            else:
                # SAP|Desc|Qty — nodo por defecto = "1"
                nodo = "1"
                sap  = str(partes[0]).strip()
                desc = str(partes[1]).strip()
                qty  = n2

        elif len(partes) == 2:
            # Nodo|SAP (sin cantidad → qty = 1) — solo si ambos son numéricos
            n0 = _pf(partes[0])
            n1 = _pf(partes[1])
            if n0 is not None and _es_nodo_val(n0) and n1 is not None and not _es_nodo_val(n1):
                nodo = str(int(n0))
                sap  = str(partes[1]).strip()
                desc = ""
                qty  = 1.0
            else:
                continue

        else:
            continue

        if not sap or qty is None or qty == 0:
            continue

        clave = (nodo, sap)
        if clave in vistos:
            for r in rows:
                if r["nodo"] == nodo and r["codigo"] == sap:
                    r["sgs_neto_excel"] = (r["sgs_neto_excel"] or 0) + qty
                    break
            continue
        vistos.add(clave)

        rows.append({
            "fila":           i,
            "nodo":           nodo,
            "codigo":         sap,
            "descripcion":    desc,
            "sip_cant":       None,
            "sgs_neto_excel": qty,
            "estado_excel":   "",
            "tipo_material":  "",
            "_odm":           lcl_odm,
        })

    return rows, errs


def _rows_mat_from_sgs_json(raw_json: str, lcl_odm: str) -> list[dict]:
    """
    Convierte el JSON retornado por api_sgs_mat_by_nodo en rows_mat
    compatibles con el verificador del módulo julian.
    """
    try:
        items = json.loads(raw_json)
    except Exception:
        return []
    rows = []
    for i, item in enumerate(items, 1):
        codigo = str(item.get("codigo") or "").strip()
        nodo   = str(item.get("nodo")   or "1").strip()
        desc   = str(item.get("desc")   or "").strip()
        qty    = float(item.get("cantidad") or 0)
        if not codigo or qty <= 0:
            continue
        rows.append({
            "fila":           i,
            "nodo":           nodo,
            "codigo":         codigo,
            "descripcion":    desc,
            "sip_cant":       None,
            "sgs_neto_excel": qty,
            "estado_excel":   "",
            "tipo_material":  "",
            "_odm":           lcl_odm,
        })
    return rows


def _calcular_total_mo(rows_mo: list) -> float:
    precios = get_precios()
    return sum(
        r.get("cantidad", 0) * precios.get(r.get("qp_code") or r.get("codigo", ""), {}).get("precio", 0)
        for r in rows_mo
    )


def _enriquecer_mo(rows_mo: list) -> list:
    precios = get_precios()
    result = []
    for r in rows_mo:
        info        = precios.get(r.get("qp_code") or r.get("codigo", ""), {})
        precio_unit = info.get("precio", 0)
        result.append({
            **r,
            "precio_unit": precio_unit,
            "subtotal":    r.get("cantidad", 0) * precio_unit,
            "encontrado":  bool(info),
            "unidad":      info.get("unidad", ""),
            "desc_baremo": info.get("descripcion", r.get("descripcion", "")),
        })
    return result


# ── Rutas ─────────────────────────────────────────────────────────────────────

@julian_manual.route("/", methods=["GET", "POST"])
def inicio():
    if request.method == "POST":
        tipo = request.form.get("tipo", "mi").strip().lower()
        lcl  = request.form.get("lcl", "").strip().upper()
        pdl  = request.form.get("pdl", "").strip()

        if not lcl:
            flash("Ingresa el código LCL / ODM del proyecto.")
            return render_template("julian/manual_inicio.html",
                                   tipo_prev=tipo, lcl_prev="", pdl_prev=pdl)

        if tipo == "programado" and not pdl:
            flash("Los proyectos programados requieren PDL.")
            return render_template("julian/manual_inicio.html",
                                   tipo_prev=tipo, lcl_prev=lcl, pdl_prev="")

        # Normalizar: "LCL6300985782" → "6300985782"
        lcl_odm = _normalizar_lcl(lcl)

        sid = str(uuid.uuid4())
        _sm[sid] = {
            "tipo":      tipo,
            "lcl":       lcl,       # display (lo que el usuario ingresó)
            "lcl_odm":   lcl_odm,   # normalizado para SGS (order_number)
            "pdl":       pdl,
            "rows_mo":   [],
            "rows_mat":  [],
            "errs_mo":   [],
            "errs_mat":  [],
            "creado_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        log.info(f"[MANUAL] Nueva sesión | sid={sid[:8]} | tipo={tipo} | LCL={lcl} odm={lcl_odm}")
        return redirect(url_for("julian_manual.mo", sid=sid))

    return render_template("julian/manual_inicio.html")


@julian_manual.route("/mo/<sid>", methods=["GET", "POST"])
def mo(sid):
    sm = _sm.get(sid)
    if not sm:
        flash("Sesión expirada. Inicia de nuevo.")
        return redirect(url_for("julian_manual.inicio"))

    if request.method == "POST":
        texto = request.form.get("texto_mo", "").strip()
        if not texto:
            flash("Pega al menos una fila de MO.")
            return render_template("julian/manual_mo.html", sm=sm, sid=sid)

        rows_mo, errs = _parsear_lineas_mo(texto, sm["lcl_odm"])
        if not rows_mo:
            errs_str = " | ".join(errs[:3]) if errs else "Verifica el formato: Nodo[TAB]QP Code[TAB]Cantidad"
            flash(f"No se detectaron filas MO válidas — {errs_str}")
            return render_template("julian/manual_mo.html", sm=sm, sid=sid,
                                   texto_prev=texto, errs=errs)

        sm["rows_mo"] = rows_mo
        sm["errs_mo"] = errs
        return redirect(url_for("julian_manual.mat", sid=sid))

    return render_template("julian/manual_mo.html", sm=sm, sid=sid)


@julian_manual.route("/mat/<sid>", methods=["GET", "POST"])
def mat(sid):
    sm = _sm.get(sid)
    if not sm:
        flash("Sesión expirada. Inicia de nuevo.")
        return redirect(url_for("julian_manual.inicio"))

    if request.method == "POST":
        rows_mat: list = []
        errs_mat: list = []

        # Opción A: filas cargadas desde SGS (nodo-only mode)
        sgs_rows_json = request.form.get("sgs_rows", "").strip()
        if sgs_rows_json:
            rows_mat = _rows_mat_from_sgs_json(sgs_rows_json, sm["lcl_odm"])
            if not rows_mat:
                flash("No se pudieron procesar los materiales de SGS. Intenta de nuevo.")
                return render_template("julian/manual_mat.html", sm=sm, sid=sid)

        else:
            # Opción B: texto pegado manualmente
            texto = request.form.get("texto_mat", "").strip()
            if texto:
                rows_mat, errs_mat = _parsear_lineas_mat(texto, sm["lcl_odm"])

        sm["rows_mat"] = rows_mat
        sm["errs_mat"] = errs_mat
        return redirect(url_for("julian_manual.preview", sid=sid))

    return render_template("julian/manual_mat.html", sm=sm, sid=sid)


@julian_manual.route("/preview/<sid>", methods=["GET", "POST"])
def preview(sid):
    sm = _sm.get(sid)
    if not sm:
        flash("Sesión expirada. Inicia de nuevo.")
        return redirect(url_for("julian_manual.inicio"))

    if request.method == "POST":
        rows_mo  = sm["rows_mo"]
        rows_mat = sm["rows_mat"]
        lcl_odm  = sm["lcl_odm"]

        for r in rows_mo:
            r["_odm"] = lcl_odm
        for r in rows_mat:
            r["_odm"] = lcl_odm

        total_mo = _calcular_total_mo(rows_mo)

        _sesiones[sid] = {
            "filename":      f"[Manual] {sm['lcl']}",
            "rows_mo":       rows_mo,
            "rows_mat":      rows_mat,
            "errs_mo":       sm.get("errs_mo", []),
            "errs_mat":      sm.get("errs_mat", []),
            "total_mo":      total_mo,
            "pdl_detectado": sm.get("pdl", ""),
            "checks_mo":     [],
            "checks_mat":    [],
            "verificado_at": None,
            "creado_at":     sm["creado_at"],
            "lcl_detectado": lcl_odm,
            "lcl_usado":     lcl_odm,
        }

        _sm.pop(sid, None)
        log.info(f"[MANUAL] Sesión → julian | sid={sid[:8]} | odm={lcl_odm} "
                 f"MO={len(rows_mo)} MAT={len(rows_mat)} total_mo=${total_mo:,.0f}")

        return redirect(url_for("julian.verificar", sid=sid))

    mo_detalle = _enriquecer_mo(sm["rows_mo"])
    total_mo   = sum(r["subtotal"] for r in mo_detalle)

    return render_template("julian/manual_preview.html",
                           sm=sm, sid=sid,
                           mo_detalle=mo_detalle,
                           total_mo=total_mo)


# ── API AJAX ──────────────────────────────────────────────────────────────────

@julian_manual.route("/api/parse_mo", methods=["POST"])
def api_parse_mo():
    texto   = request.form.get("texto", "")
    lcl     = request.form.get("lcl", "")
    lcl_odm = _normalizar_lcl(lcl) if lcl else ""
    rows, errs = _parsear_lineas_mo(texto, lcl_odm)
    precios = get_precios()

    result = []
    total  = 0.0
    for r in rows:
        info        = precios.get(r["qp_code"], {})
        precio_unit = info.get("precio", 0)
        subtotal    = r["cantidad"] * precio_unit
        total      += subtotal
        result.append({
            "nodo":        r["pro"],
            "qp":          r["qp_code"],
            "descripcion": info.get("descripcion", r.get("descripcion", "")),
            "unidad":      info.get("unidad", ""),
            "cantidad":    r["cantidad"],
            "precio_unit": precio_unit,
            "subtotal":    subtotal,
            "encontrado":  bool(info),
        })

    return jsonify({"rows": result, "total": total, "errs": errs, "n": len(result)})


@julian_manual.route("/api/parse_mat", methods=["POST"])
def api_parse_mat():
    texto   = request.form.get("texto", "")
    lcl     = request.form.get("lcl", "")
    lcl_odm = _normalizar_lcl(lcl) if lcl else ""

    # Detectar modo nodo-only primero
    nodos = _detectar_nodos_solos(texto)
    if nodos is not None:
        return jsonify({"modo": "nodo_only", "nodos": nodos, "rows": [], "n": 0})

    rows, errs = _parsear_lineas_mat(texto, lcl_odm)
    result = [
        {"nodo": r["nodo"], "codigo": r["codigo"],
         "desc": r["descripcion"], "cantidad": r["sgs_neto_excel"]}
        for r in rows
    ]
    return jsonify({"modo": "normal", "rows": result, "errs": errs, "n": len(result)})


@julian_manual.route("/api/sgs_mat_by_nodo", methods=["POST"])
def api_sgs_mat_by_nodo():
    """
    Consulta SGS para obtener todos los materiales pendientes (balance > 0)
    de los nodos indicados en el proyecto identificado por LCL.
    """
    lcl    = request.form.get("lcl", "").strip()
    nodos_raw = request.form.get("nodos", "").strip()

    if not lcl or not nodos_raw:
        return jsonify({"error": "LCL y nodos son requeridos", "rows": []})

    lcl_odm = _normalizar_lcl(lcl)
    nodos   = [n.strip() for n in re.split(r"[,\s;]+", nodos_raw) if n.strip()]
    # Deduplicar
    seen: set = set()
    nodos_unicos = [n for n in nodos if not (n in seen or seen.add(n))]  # type: ignore

    try:
        from modulos.julian.blueprint import _sgs, _resolver_pid_nodo
        c = _sgs()
    except Exception as ex:
        return jsonify({"error": f"No se pudo conectar a SGS: {ex}", "rows": []})

    result: list = []
    pids_vistos: dict = {}   # pid → nodo (para no repetir el mismo proyecto)

    for nodo in nodos_unicos:
        try:
            pid = _resolver_pid_nodo(c, nodo, [], [], odm=lcl_odm)
        except Exception as ex:
            log.warning(f"[MANUAL SGS MAT] _resolver_pid_nodo nodo={nodo} odm={lcl_odm}: {ex}")
            pid = None

        if not pid:
            result.append({
                "nodo": nodo, "codigo": None, "desc": None, "cantidad": None,
                "error": f"No se encontró proyecto para nodo {nodo} / LCL {lcl_odm}",
            })
            continue

        if pid in pids_vistos:
            # Mismo proyecto que otro nodo — reutilizar con el nodo correcto
            for r in result:
                if r.get("codigo") and pids_vistos[pid] != nodo:
                    pass   # la info ya está; solo cambia el nodo de display
            # Agregar con este nodo también (el verificar usa nodo para resolver pid)
            pids_vistos[pid] = nodo
            continue

        pids_vistos[pid] = nodo

        try:
            mats = c.search_read(
                "project.material",
                [["project_id", "=", pid]],
                ["id", "product_code", "product_id", "balance",
                 "despachado", "devuelto", "advance"],
                limit=300,
            )
        except Exception as ex:
            log.warning(f"[MANUAL SGS MAT] project.material pid={pid}: {ex}")
            continue

        for m in mats:
            cod = str(m.get("product_code") or "").strip()
            if not cod:
                continue
            balance = float(m.get("balance") or 0)
            if balance <= 0:
                continue

            # Extraer descripción del campo product_id (many2one)
            prod_field = m.get("product_id")
            if isinstance(prod_field, list) and len(prod_field) >= 2:
                txt = str(prod_field[1])
                # "[231052] DESC" o "[231052/C] DESC" → "DESC"
                desc = re.sub(r'^\[[^\]]*\]\s*', '', txt).strip()
            else:
                desc = cod

            result.append({
                "nodo":     nodo,
                "codigo":   cod,
                "desc":     desc,
                "cantidad": balance,
            })

    rows_ok = [r for r in result if r.get("codigo")]
    errors  = [r["error"] for r in result if r.get("error")]

    log.info(f"[MANUAL SGS MAT] LCL={lcl_odm} nodos={nodos_unicos} → {len(rows_ok)} materiales")
    return jsonify({
        "rows":   rows_ok,
        "errors": errors,
        "n":      len(rows_ok),
    })
