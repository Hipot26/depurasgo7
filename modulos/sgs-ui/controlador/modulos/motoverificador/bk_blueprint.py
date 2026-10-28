"""
motoverificador/blueprint.py — Cambio de líder en actividades MO.

Standalone en /motoverificador/. No depende de sesiones del módulo julian ni de liquidador2.

Flujo:
  1. Usuario ingresa código PRO → sistema carga nodos hijos (project.dominion)
  2. Selecciona nodos → sistema analiza actividades con avance > 0
  3. Por cada línea de avance (detail) el usuario asigna un nuevo líder via cédula
  4. Revisar el plan → Ejecutar
  5. Background: restar_avance_mo_lote + crear_y_liquidar_mo por cada corrección
"""

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
from datetime import datetime

from flask import (Blueprint, render_template, request,
                   redirect, url_for, flash, jsonify)

from sgs_readonly_client import SGSReadOnlyClient
from browser_client import PlaywrightClient

# Sesión compartida con el browser principal del app
def _obtener_storage_state():
    try:
        import app as _app
        return _app._obtener_storage_state_activo(timeout_s=5.0)
    except Exception:
        return None

motoverificador_bp = Blueprint(
    "motoverificador",
    __name__,
    template_folder="../../templates/motoverificador",
    url_prefix="/motoverificador",
)

log = logging.getLogger("motoverificador")

# ── SGS client (propio) ───────────────────────────────────────────────────────
SGS_URL      = os.environ.get("SGS_URL",      "")
SGS_DB       = os.environ.get("SGS_DB",       "")
SGS_LOGIN    = os.environ.get("SGS_LOGIN",    "")
SGS_PASSWORD = os.environ.get("SGS_PASSWORD", "")

_sgs_cache: SGSReadOnlyClient | None = None


def _sgs() -> SGSReadOnlyClient:
    global _sgs_cache
    if _sgs_cache and _sgs_cache.uid:
        try:
            _sgs_cache.search("res.users", [["id", "=", _sgs_cache.uid]], limit=1)
            return _sgs_cache
        except Exception:
            pass
    _sgs_cache = SGSReadOnlyClient(SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD)
    _sgs_cache.autenticar()
    return _sgs_cache


# ── Sesiones ─────────────────────────────────────────────────────────────────
_sesiones: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# AJAX APIs
# ─────────────────────────────────────────────────────────────────────────────

@motoverificador_bp.route("/api/buscar_pro", methods=["POST"])
def api_buscar_pro():
    """
    Busca proyecto padre por código PRO/LCL y retorna sus nodos hijos
    (project.dominion) con conteo de actividades con avance > 0.
    """
    data     = request.get_json() or {}
    pro_code = str(data.get("pro_code") or "").strip().upper()
    if not pro_code:
        return jsonify({"ok": False, "error": "Ingresa un código de proyecto"}), 400

    try:
        c = _sgs()
        proyecto = c.buscar_por_codigo(pro_code)
        if not proyecto:
            return jsonify({"ok": False, "error": f"Proyecto '{pro_code}' no encontrado en SGS"})

        pid_padre    = proyecto["id"]
        nombre_padre = proyecto.get("name") or pro_code

        nodos_raw = c.search_read(
            "project.dominion",
            [["parent_id", "=", pid_padre]],
            ["id", "name", "code", "nodo", "order_number"],
            limit=200,
        )

        nodos = []
        for n in nodos_raw:
            try:
                acts = c.search_read(
                    "project.activities",
                    [["project_id", "=", n["id"]]],
                    ["id", "advance"],
                    limit=100,
                )
                advance_count = sum(1 for a in acts if float(a.get("advance") or 0) > 0)
            except Exception:
                advance_count = 0

            nodos.append({
                "pid":          n["id"],
                "nombre":       n.get("name") or "",
                "code":         n.get("code") or "",
                "nodo":         n.get("nodo") or "",
                "order_number": n.get("order_number") or "",
                "advance_count": advance_count,
            })

        # Nodo vagabundo: el proyecto mismo no tiene hijos, él ES el nodo
        es_vagabundo = False
        if not nodos:
            try:
                acts_self = c.search_read(
                    "project.activities",
                    [["project_id", "=", pid_padre]],
                    ["id", "advance"],
                    limit=100,
                )
                advance_count = sum(1 for a in acts_self if float(a.get("advance") or 0) > 0)
            except Exception:
                advance_count = 0
            nodos.append({
                "pid":           pid_padre,
                "nombre":        nombre_padre,
                "code":          pro_code,
                "nodo":          "",
                "order_number":  "",
                "advance_count": advance_count,
            })
            es_vagabundo = True

        return jsonify({
            "ok":            True,
            "pid_padre":     pid_padre,
            "nombre_padre":  nombre_padre,
            "pro_code":      pro_code,
            "nodos":         nodos,
            "es_vagabundo":  es_vagabundo,
        })
    except Exception as ex:
        log.error("[MOTO] buscar_pro: %s", ex)
        return jsonify({"ok": False, "error": str(ex)}), 500


@motoverificador_bp.route("/api/analizar_nodos", methods=["POST"])
def api_analizar_nodos():
    """
    Para cada pid seleccionado retorna las actividades con advance > 0
    y sus details (líneas positivas), con líder, fecha y cantidad.
    """
    data = request.get_json() or {}
    pids_raw = data.get("pids") or []
    try:
        pids = [int(p) for p in pids_raw]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "pids inválidos"}), 400

    if not pids:
        return jsonify({"ok": False, "error": "Selecciona al menos un nodo"}), 400

    try:
        c = _sgs()
        analisis = []

        for pid in pids:
            proyectos = c.search_read(
                "project.dominion",
                [["id", "=", pid]],
                ["id", "name", "code"],
                limit=1,
            )
            if not proyectos:
                continue
            nodo_info = proyectos[0]
            pro_code  = nodo_info.get("code") or nodo_info.get("name") or str(pid)

            acts = c.search_read(
                "project.activities",
                [["project_id", "=", pid]],
                ["id", "activity_code", "amount", "advance", "balance"],
                limit=100,
            )

            actividades = []
            for a in acts:
                adv = float(a.get("advance") or 0)
                if adv <= 0:
                    continue

                # Traer TODAS las líneas (positivas y negativas) para calcular neto por partner.
                # La corrección no borra líneas — agrega una negativa + una nueva positiva.
                # Si usáramos solo amount>0, veríamos la línea original cancelada como "activa".
                dets_raw = c.search_read(
                    "project.activities.details",
                    [["project_activities_id", "=", a["id"]]],
                    ["id", "amount", "partner_id", "date"],
                    limit=60,
                )

                # Agrupar por partner: neto (suma de todas sus líneas) y línea de referencia
                neto_por_partner = {}   # partner_id → {neto, nombre, id_ref, fecha_raw}
                for d in dets_raw:
                    amt = float(d.get("amount") or 0)
                    partner_raw = d.get("partner_id")
                    if isinstance(partner_raw, list):
                        pid_p  = partner_raw[0]
                        pnom   = partner_raw[1]
                    else:
                        pid_p  = None
                        pnom   = str(partner_raw or "?")
                    key = pid_p or pnom
                    if key not in neto_por_partner:
                        fecha_raw = str(d.get("date") or "")[:10]
                        neto_por_partner[key] = {
                            "neto":    0.0,
                            "nombre":  pnom,
                            "pid":     pid_p,
                            "id_ref":  d["id"],
                            "fecha_raw": fecha_raw,
                        }
                    neto_por_partner[key]["neto"] += amt

                # Solo incluir partners con neto > 0 (líneas canceladas tienen neto ≈ 0)
                detalles = []
                for info in neto_por_partner.values():
                    if info["neto"] < 0.001:
                        continue
                    fecha_raw = info["fecha_raw"]
                    parts     = fecha_raw.split("-")
                    fecha_pw  = (
                        f"{parts[2]}/{parts[1]}/{parts[0]}"
                        if len(parts) == 3 else fecha_raw
                    )
                    detalles.append({
                        "id":             info["id_ref"],
                        "amount":         info["neto"],
                        "partner_id":     info["pid"],
                        "partner_nombre": info["nombre"],
                        "fecha_sgs":      fecha_raw,
                        "fecha_pw":       fecha_pw,
                    })

                if detalles:
                    actividades.append({
                        "act_id":           a["id"],
                        "act_code":         a.get("activity_code") or "",
                        "amount_proyectado": float(a.get("amount") or 0),
                        "advance":          adv,
                        "balance":          float(a.get("balance") or 0),
                        "detalles":         detalles,
                    })

            if actividades:
                analisis.append({
                    "pid":        pid,
                    "nombre":     nodo_info.get("name") or "",
                    "pro_code":   pro_code,
                    "actividades": actividades,
                })

        return jsonify({"ok": True, "analisis": analisis})
    except Exception as ex:
        log.error("[MOTO] analizar_nodos: %s", ex)
        return jsonify({"ok": False, "error": str(ex)}), 500


@motoverificador_bp.route("/panel")
def panel():
    return render_template("motoverificador/panel.html")


@motoverificador_bp.route("/api/panel_analisis")
def api_panel_analisis():
    """
    Análisis completo de un PRO: por cada nodo con avances muestra
    actividades, líderes actuales y % de avance.
    Usado en el panel de diagnóstico (antes y después de correcciones).
    """
    pro_code = (request.args.get("pro") or "").strip().upper()
    if not pro_code:
        return jsonify({"ok": False, "error": "Ingresa un código de proyecto"}), 400

    try:
        c = _sgs()
        proyecto = c.buscar_por_codigo(pro_code)
        if not proyecto:
            return jsonify({"ok": False, "error": f"Proyecto '{pro_code}' no encontrado"})

        pid_padre    = proyecto["id"]
        nombre_padre = proyecto.get("name") or pro_code

        nodos_raw = c.search_read(
            "project.dominion",
            [["parent_id", "=", pid_padre]],
            ["id", "name", "code"],
            limit=200,
        )

        nodos_analisis = []
        total_proy_all = 0.0
        total_adv_all  = 0.0

        for n in nodos_raw:
            pid = n["id"]
            acts = c.search_read(
                "project.activities",
                [["project_id", "=", pid]],
                ["id", "activity_code", "amount", "advance", "balance"],
                limit=100,
            )

            actividades    = []
            total_proy_nodo = 0.0
            total_adv_nodo  = 0.0

            for a in acts:
                adv  = float(a.get("advance") or 0)
                proy = float(a.get("amount")  or 0)
                if adv <= 0 and proy <= 0:
                    continue

                # Todas las líneas para calcular neto por partner (positivas+negativas)
                dets = c.search_read(
                    "project.activities.details",
                    [["project_activities_id", "=", a["id"]]],
                    ["partner_id", "amount"],
                    limit=60,
                )
                lideres_neto = {}
                for d in dets:
                    pr     = d.get("partner_id")
                    nombre = pr[1] if isinstance(pr, list) else str(pr or "?")
                    lideres_neto[nombre] = lideres_neto.get(nombre, 0) + float(d.get("amount") or 0)
                # Solo partners con neto > 0
                lideres = {k: v for k, v in lideres_neto.items() if v > 0.001}

                total_proy_nodo += proy
                total_adv_nodo  += adv
                pct_act = round(adv / proy * 100, 1) if proy > 0 else 0.0

                actividades.append({
                    "act_id":   a["id"],
                    "act_code": a.get("activity_code") or "",
                    "proy":     proy,
                    "advance":  adv,
                    "balance":  float(a.get("balance") or 0),
                    "pct":      pct_act,
                    "lideres":  [{"nombre": k, "qty": round(v, 3)} for k, v in lideres.items()],
                })

            if not actividades:
                continue

            total_proy_all += total_proy_nodo
            total_adv_all  += total_adv_nodo
            pct_nodo = round(total_adv_nodo / total_proy_nodo * 100, 1) if total_proy_nodo > 0 else 0.0

            nodos_analisis.append({
                "pid":        pid,
                "nombre":     n.get("name") or "",
                "total_proy": total_proy_nodo,
                "total_adv":  total_adv_nodo,
                "pct":        pct_nodo,
                "actividades": actividades,
            })

        pct_proyecto = round(total_adv_all / total_proy_all * 100, 1) if total_proy_all > 0 else 0.0

        return jsonify({
            "ok":           True,
            "pro_code":     pro_code,
            "nombre_padre": nombre_padre,
            "total_proy":   total_proy_all,
            "total_adv":    total_adv_all,
            "pct":          pct_proyecto,
            "nodos":        nodos_analisis,
        })
    except Exception as ex:
        log.error("[MOTO] panel_analisis: %s", ex)
        return jsonify({"ok": False, "error": str(ex)}), 500


@motoverificador_bp.route("/api/historial_actividades")
def api_historial_actividades():
    """
    GET ?act_ids=1,2,3  — devuelve el historial completo de líneas de avance
    (project.activities.details) para las actividades indicadas.
    Incluye positivas y negativas, ordenadas por fecha.
    Usado en esperar.html para verificar si la corrección se aplicó.
    """
    raw = request.args.get("act_ids", "")
    act_ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    if not act_ids:
        return jsonify({"ok": False, "error": "act_ids requerido"}), 400

    try:
        c = _sgs()
        c.autenticar()

        acts_raw = c.search_read(
            "project.activities",
            [["id", "in", act_ids]],
            ["id", "activity_code", "amount", "advance", "balance"],
            limit=len(act_ids) + 5,
        )
        act_map = {a["id"]: a for a in acts_raw}

        dets_raw = c.search_read(
            "project.activities.details",
            [["project_activities_id", "in", act_ids]],
            ["id", "project_activities_id", "partner_id", "amount", "date"],
            limit=300,
        )

        from collections import defaultdict
        lines_by_act: dict = defaultdict(list)
        for d in dets_raw:
            pa = d.get("project_activities_id")
            aid = pa[0] if isinstance(pa, list) else int(pa or 0)
            lines_by_act[aid].append(d)

        actividades = []
        for act_id in act_ids:
            act = act_map.get(act_id, {})
            raw_lines = sorted(
                lines_by_act.get(act_id, []),
                key=lambda x: str(x.get("date") or ""),
            )
            lineas = []
            neto: dict = {}
            for l in raw_lines:
                amt = float(l.get("amount") or 0)
                pr  = l.get("partner_id")
                nom = pr[1] if isinstance(pr, list) else str(pr or "?")
                fecha = str(l.get("date") or "")[:10]
                neto[nom] = neto.get(nom, 0.0) + amt
                lineas.append({
                    "id":     l["id"],
                    "fecha":  fecha,
                    "lider":  nom,
                    "amount": round(amt, 3),
                    "signo":  "pos" if amt >= 0 else "neg",
                })

            proy = float(act.get("amount")   or 0)
            adv  = float(act.get("advance")  or 0)
            bal  = float(act.get("balance")  or 0)

            actividades.append({
                "act_id":   act_id,
                "act_code": act.get("activity_code") or str(act_id),
                "proy":     proy,
                "advance":  adv,
                "balance":  bal,
                "lineas":   lineas,
                "neto_por_partner": [
                    {"nombre": k, "neto": round(v, 3)}
                    for k, v in sorted(neto.items(), key=lambda x: -x[1])
                ],
            })

        return jsonify({"ok": True, "actividades": actividades})

    except Exception as ex:
        log.error("[MOTO] historial_actividades: %s", ex)
        return jsonify({"ok": False, "error": str(ex)}), 500


@motoverificador_bp.route("/api/buscar_partner")
def api_buscar_partner():
    """
    Autocomplete GET ?q=<texto> — igual que liquidador2/api/buscar_lider.
    - Empieza con C  → vat like <sin-C>  (regla de cédula)
    - Otro texto     → name ilike
    Retorna [{nombre, cedula}].
    """
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    try:
        c = _sgs()
        # Cédula: empieza con "C" seguido de dígitos (ej. C79161805)
        # Nombre: cualquier otro texto, incluidos los que empiezan con letra (ej. "garcia")
        es_cedula = q.upper().startswith("C") and len(q) > 1 and q[1:2].isdigit()
        if es_cedula:
            # Incluir activos e inactivos: trabajadores archivados siguen siendo válidos
            domain = [["vat", "like", q[1:]], ["active", "in", [True, False]]]
        else:
            # Cada palabra debe aparecer en el nombre (AND implícito en Odoo)
            # Sin filtro active: empleados archivados también deben aparecer
            palabras = [p for p in q.split() if p]
            domain   = [["active", "in", [True, False]]]
            for p in palabras:
                domain.append(["name", "ilike", p])
        partners = c.search_read("res.partner", domain, ["id", "name", "vat"], limit=20)

        def _normalizar_cedula(vat_raw) -> str:
            """Siempre devuelve 'C<dígitos>' sin importar si vat ya trae C/CC."""
            if not vat_raw:
                return ""
            digits = str(vat_raw).lstrip("Cc").strip()
            return ("C" + digits) if digits else ""

        # Deduplicar: mismo (nombre_upper, cedula) → conservar solo uno
        vistos: set = set()
        resultado = []
        for p in partners:
            if not p.get("name"):
                continue
            cedula = _normalizar_cedula(p.get("vat"))
            clave  = (p["name"].upper(), cedula)
            if clave in vistos:
                continue
            vistos.add(clave)
            resultado.append({"nombre": p["name"], "cedula": cedula})

        return jsonify(resultado[:10])
    except Exception as ex:
        log.warning("[MOTO] api_buscar_partner: %s", ex)
        return jsonify([])


# ─────────────────────────────────────────────────────────────────────────────
# Rutas principales
# ─────────────────────────────────────────────────────────────────────────────

@motoverificador_bp.route("/")
def inicio():
    return render_template("motoverificador/inicio.html")


@motoverificador_bp.route("/preparar", methods=["POST"])
def preparar():
    """
    Recibe correcciones_json del formulario, crea sesión y redirige a revisar.

    correcciones_json = JSON array de:
    {
      pid, act_id, act_code, pro_code, nombre_nodo, amount_proyectado,
      lineas: [{
        detail_id, partner_incorrecto, cantidad, fecha_pw,
        partner_nuevo_nombre, partner_nuevo_cedula
      }]
    }
    """
    raw = request.form.get("correcciones_json") or "[]"
    try:
        correcciones = json.loads(raw)
    except Exception:
        flash("Datos de correcciones inválidos.")
        return redirect(url_for("motoverificador.inicio"))

    correcciones = [c for c in correcciones if c.get("lineas")]
    if not correcciones:
        flash("No hay correcciones preparadas. Asigna un nuevo líder a al menos una línea.")
        return redirect(url_for("motoverificador.inicio"))

    sid = str(uuid.uuid4())
    _sesiones[sid] = {
        "creado_at":   datetime.now().strftime("%Y-%m-%d %H:%M"),
        "correcciones": correcciones,
        "job":         None,
        "resultados":  [],
        "storage_state": None,
    }
    log.info("[MOTO] Sesión preparada sid=%s  correcciones=%d", sid[:8], len(correcciones))
    return redirect(url_for("motoverificador.revisar", sid=sid))


@motoverificador_bp.route("/revisar/<sid>")
def revisar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesión no encontrada.")
        return redirect(url_for("motoverificador.inicio"))
    total_lineas = sum(len(c.get("lineas", [])) for c in s.get("correcciones", []))
    return render_template("motoverificador/revisar.html",
                           sid=sid, s=s, total_lineas=total_lineas)


@motoverificador_bp.route("/ejecutar/<sid>", methods=["POST"])
def ejecutar(sid):
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"ok": False, "error": "Sesión no encontrada"}), 404

    if s.get("job") and s["job"].get("estado") == "running":
        return jsonify({"ok": False, "error": "Ya hay un job en curso"}), 400

    total = sum(len(c.get("lineas", [])) for c in s.get("correcciones", []))
    job   = {
        "estado":   "running",
        "inicio":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "logs":     [],
        "progreso": 0,
        "total":    total,
        "errores":  0,
        "ok_count": 0,
    }
    s["job"]       = job
    s["resultados"] = []

    threading.Thread(target=_bg_ejecutar, args=(sid,), daemon=True).start()
    return jsonify({"ok": True, "sid": sid})


@motoverificador_bp.route("/esperar/<sid>")
def esperar(sid):
    s = _sesiones.get(sid)
    if not s:
        flash("Sesión no encontrada.")
        return redirect(url_for("motoverificador.inicio"))
    return render_template("motoverificador/esperar.html", sid=sid, s=s)


@motoverificador_bp.route("/api/job_estado/<sid>")
def api_job_estado(sid):
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"estado": "not_found"}), 404
    job = s.get("job") or {}
    return jsonify({
        "estado":     job.get("estado", "idle"),
        "progreso":   job.get("progreso", 0),
        "total":      job.get("total", 0),
        "logs":       job.get("logs", []),
        "errores":    job.get("errores", 0),
        "ok_count":   job.get("ok_count", 0),
        "resultados": s.get("resultados", []),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Background execution
# ─────────────────────────────────────────────────────────────────────────────

MAX_WORKERS_MOTO = 5   # máximo de nodos en paralelo
STAGGER_S        = 8   # segundos entre arranque de workers (desfasa los saves)


def _bg_ejecutar(sid: str) -> None:
    s   = _sesiones.get(sid)
    job = s.get("job") if s else None
    if not s or not job:
        return

    correcciones  = s.get("correcciones") or []
    validas       = [c for c in correcciones if c.get("lineas")]
    resultados    = [None] * len(validas)
    job_lock      = threading.Lock()

    def _log(msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        with job_lock:
            job["logs"].append(f"[{ts}] {msg}")
        log.info("[MOTO BG] %s", msg)

    # ── Fase 0: obtener storage_state (login único si es necesario) ──────────
    storage_state = s.get("storage_state") or _obtener_storage_state()
    if not storage_state:
        try:
            _log("Sin sesión activa — iniciando login…")
            pw_setup = PlaywrightClient(headless=False, slow_mo=100)
            pw_setup.start()
            pw_setup.login()
            storage_state = pw_setup._page.context.storage_state()
            s["storage_state"] = storage_state
            pw_setup.close()
            _log("Login OK — sesión capturada")
        except Exception as ex:
            _log(f"✗ Error en login: {ex}")
            job["estado"] = "listo"
            return
    else:
        _log("Sesión existente encontrada")

    # ── Agrupar correcciones por pid: mismo nodo → mismo worker ─────────────
    # Garantiza que actividades del mismo nodo nunca se ejecuten en paralelo,
    # evitando carreras en _asegurar_lider_en_equipo y en el equipo del proyecto.
    _pid_grupos: dict = {}   # pid_key → [(cor_idx, cor), ...]
    for cor_idx, cor in enumerate(validas):
        pid_key = int(cor["pid"])
        if pid_key not in _pid_grupos:
            _pid_grupos[pid_key] = []
        _pid_grupos[pid_key].append((cor_idx, cor))

    n_workers = min(MAX_WORKERS_MOTO, len(_pid_grupos))
    _log(
        f"Lanzando {n_workers} worker(s) en paralelo — "
        f"{len(_pid_grupos)} nodo(s) distinto(s), {len(validas)} actividad(es) total…"
    )

    # ── Worker por nodo (grupo de actividades del mismo pid) ─────────────────
    def _worker(worker_id: int, pid_key: int, grupo: list) -> None:
        """
        grupo = [(cor_idx, cor), ...]  — todas las correcciones del mismo pid.
        Se procesan secuencialmente: _asegurar_lider_en_equipo se llama UNA sola vez
        y luego se itera sobre cada actividad en orden.
        """
        cor0         = grupo[0][1]
        pro_code     = cor0.get("pro_code") or ""
        nombre_nodo  = cor0.get("nombre_nodo") or pro_code
        lineas0      = cor0.get("lineas") or []
        nuevo_nombre = lineas0[0]["partner_nuevo_nombre"] if lineas0 else ""
        nuevo_cedula = (lineas0[0].get("partner_nuevo_cedula") or "") if lineas0 else ""

        prefix = f"[W{worker_id}]"

        def wlog(msg):
            _log(f"{prefix} {msg}")

        pw = PlaywrightClient(headless=False, slow_mo=100)
        c  = None

        try:
            pw.start(storage_state=storage_state)
            c = _sgs()

            acts_str = ", ".join(g[1]["act_code"] for g in grupo)
            wlog(f"Nodo {nombre_nodo} — {len(grupo)} actividad(es): {acts_str}")

            # ── Paso 1: asegurar líder en equipo (UNA VEZ por nodo) ──────────
            wlog(f"Verificando líder '{nuevo_nombre}' en equipo del nodo…")
            try:
                lider_ok = pw._asegurar_lider_en_equipo(
                    pid_key, nuevo_nombre,
                    partner_cedula=nuevo_cedula,
                    pro_code=pro_code,
                    sgs_client=c,
                )
                wlog("Líder agregado al equipo OK" if lider_ok else "Líder ya estaba en el equipo")
            except Exception as ex:
                err = f"No se pudo agregar líder al equipo: {ex}"
                wlog(f"✗ {err}")
                with job_lock:
                    for cor_idx, cor in grupo:
                        resultados[cor_idx] = {
                            "act_code":    cor["act_code"],
                            "nombre_nodo": nombre_nodo,
                            "ok":          False,
                            "lineas":      [],
                            "error":       err,
                        }
                        job["errores"] += len(cor.get("lineas") or [])
                    s["resultados"] = [r for r in resultados if r is not None]
                return

            # ── Pasos 2+3: cada actividad del nodo, en orden ─────────────────
            for seq_i, (cor_idx, cor) in enumerate(grupo):
                act_id      = int(cor["act_id"])
                act_code    = cor["act_code"]
                amount_proy = float(cor.get("amount_proyectado") or 0)
                lineas      = cor.get("lineas") or []

                wlog(f"[{seq_i + 1}/{len(grupo)}] {act_code} — {len(lineas)} línea(s)")

                cor_result = {
                    "act_code":    act_code,
                    "nombre_nodo": nombre_nodo,
                    "ok":          False,
                    "lineas":      [],
                    "error":       None,
                }

                try:
                    # Detectar ejecución parcial
                    ya_restado = False
                    try:
                        neg_lines = c.search_read(
                            "project.activities.details",
                            [["project_activities_id", "=", act_id], ["amount", "<", 0]],
                            ["id"],
                            limit=2,
                        )
                        ya_restado = len(neg_lines) > 0
                    except Exception as ex:
                        log.warning("[MOTO W%d] No se pudo verificar líneas negativas: %s", worker_id, ex)

                    if ya_restado:
                        wlog("⚠ Líneas negativas ya existen — saltando resta")
                    else:
                        detalles_restar = [
                            {
                                "partner_nombre":  l["partner_incorrecto"],
                                "cantidad_restar": float(l["cantidad"]),
                                "fecha":           l["fecha_pw"],
                            }
                            for l in lineas
                        ]
                        wlog(f"Restando {len(detalles_restar)} avance(s)…")
                        res_restar = pw.restar_avance_mo_lote(
                            pid               = pid_key,
                            act_code          = act_code,
                            detalles          = detalles_restar,
                            pro_code          = pro_code,
                            proyectado_actual = amount_proy,
                            act_id            = act_id,
                            sgs_client        = c,
                            dry_run           = False,
                        )
                        if not res_restar.get("ok"):
                            err = res_restar.get("error") or "Error en restar_avance_mo_lote"
                            wlog(f"✗ Error restando: {err}")
                            cor_result["error"] = err
                            with job_lock:
                                job["errores"] += len(lineas)
                            # No interrumpe el worker: continúa con la siguiente actividad
                            continue
                        wlog(f"✓ Resta OK — {res_restar.get('lineas_revertidas', 0)} revertida(s)")

                    # Liquidar con nuevo líder
                    usar_skip_nav = not ya_restado

                    for l in lineas:
                        cantidad = float(l["cantidad"])
                        fecha    = l.get("fecha_pw") or None

                        linea_result = {
                            "partner_incorrecto": l["partner_incorrecto"],
                            "partner_nuevo":      nuevo_nombre,
                            "cantidad":           cantidad,
                            "ok":                 False,
                            "msg":                "",
                            "error":              None,
                        }

                        try:
                            wlog(f"Liquidando {int(cantidad)} ud. → {nuevo_nombre} (fecha {fecha or 'hoy'})…")
                            res_liq = pw.crear_y_liquidar_mo(
                                pid            = pid_key,
                                act_code       = act_code,
                                cantidad       = cantidad,
                                pro_code       = pro_code,
                                act_id         = act_id,
                                sgs_client     = c,
                                partner_nombre = nuevo_nombre,
                                partner_cedula = nuevo_cedula,
                                dry_run        = False,
                                fecha          = fecha,
                                skip_nav       = usar_skip_nav,
                            )
                            usar_skip_nav = True  # siguientes líneas de la misma actividad

                            if res_liq.get("ok"):
                                linea_result["ok"]  = True
                                linea_result["msg"] = f"OK — {int(cantidad)} ud. a {nuevo_nombre}"
                                wlog("✓ Liquidado OK")
                                with job_lock:
                                    job["ok_count"] += 1
                            else:
                                err = res_liq.get("error") or "Error en crear_y_liquidar_mo"
                                linea_result["error"] = err
                                wlog(f"✗ Error liquidando: {err}")
                                with job_lock:
                                    job["errores"] += 1

                        except Exception as ex:
                            linea_result["error"] = str(ex)
                            wlog(f"✗ Excepción: {ex}")
                            with job_lock:
                                job["errores"] += 1

                        cor_result["lineas"].append(linea_result)

                    # Restaurar proyectado si todo OK
                    todas_ok = all(ll["ok"] for ll in cor_result["lineas"])
                    if todas_ok:
                        try:
                            wlog(f"Restaurando proyectado a {int(amount_proy)}…")
                            pw._rpc_set_proyectado(act_id, amount_proy)
                            wlog("✓ Proyectado restaurado OK")
                        except Exception as ex:
                            wlog(f"⚠ No se pudo restaurar proyectado: {ex}")

                    cor_result["ok"] = todas_ok

                except Exception as ex:
                    cor_result["error"] = str(ex)
                    wlog(f"✗ Error crítico en {act_code}: {ex}")
                    log.error("[MOTO W%d] Error en %s: %s", worker_id, act_code, ex, exc_info=True)
                    with job_lock:
                        job["errores"] += 1
                finally:
                    with job_lock:
                        resultados[cor_idx] = cor_result
                        s["resultados"] = [r for r in resultados if r is not None]
                        job["progreso"] = int(
                            sum(1 for r in resultados if r is not None) / max(len(validas), 1) * 100
                        )

        except Exception as ex:
            wlog(f"✗ Error crítico del worker: {ex}")
            log.error("[MOTO W%d] Error crítico: %s", worker_id, ex, exc_info=True)
            with job_lock:
                for cor_idx, cor in grupo:
                    if resultados[cor_idx] is None:
                        resultados[cor_idx] = {
                            "act_code":    cor["act_code"],
                            "nombre_nodo": nombre_nodo,
                            "ok":          False,
                            "lineas":      [],
                            "error":       f"Worker abortado: {ex}",
                        }
                        job["errores"] += 1
                s["resultados"] = [r for r in resultados if r is not None]
        finally:
            try:
                pw.close()
            except Exception:
                pass

    # ── Fase 1: lanzar workers con stagger (uno por nodo distinto) ───────────
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = []
        for w_idx, (pid_key, grupo) in enumerate(_pid_grupos.items()):
            if w_idx > 0:
                time.sleep(STAGGER_S)
            futures.append(executor.submit(_worker, w_idx, pid_key, grupo))

        for fut in _as_completed(futures):
            try:
                fut.result()
            except Exception as ex:
                _log(f"✗ Worker terminó con excepción: {ex}")

    # ── Finalizar ─────────────────────────────────────────────────────────────
    s["resultados"] = [r or {"ok": False, "error": "Sin resultado — worker interrumpido"}
                       for r in resultados]
    job["estado"]   = "listo"
    job["progreso"] = 100
    total_ok  = sum(1 for r in s["resultados"] if r.get("ok"))
    total_err = len(s["resultados"]) - total_ok
    _log(f"Finalizado — {total_ok} OK / {total_err} con error(es).")
