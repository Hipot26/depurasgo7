"""gestion_arbitro.py — El Centro MO como árbitro de SGS para Gestión General (22 oct 2026).

Gestión General (backend sabogal, /api/gestion) no interpreta códigos ni liquidaciones por su cuenta: le
pregunta al Centro MO, que ya tiene las reglas (balance_mo_bridge) y el cliente SGS que funciona (cofe, solo
lectura). Dos rutas internas, solo desde el servidor, con el token interno de SGO7 (cabecera X-SGO7-Interno):

  POST /interno/gestion/sgs        {"codigos": ["LCL6301000001", "DF2026…", "6301000002"], "detalle": true}
       → por código: existe, método (order_number / DF truncado / nombre), TODOS los padres con ese
         order_number (regla 16: un LCL puede tener varios padres), supervisor SGS (approve_id), departamento y
         zona; con detalle, la MO liquidada de cada familia (project.activities.details advanced_price):
         total, avances, nodos, por mes del avance y por DÍA EN QUE SE LIQUIDÓ (create_date) con quién.
  GET  /interno/gestion/registros  → los correos de MO que el Centro MO ya interpretó (código validado y su
         fuente, PDL, fecha de ejecución, estado, completado_en, entry_id de Outlook).
  Desde 2026-10-23: con "mat": true, cada padre trae también el MATERIAL de su familia (project.material, con la compañía
         en el contexto): por código de Enel (customer_default_code, solo material del cliente) lo despachado, lo consumido y
         el costo de SGS (standard_price), y el valor consumido / despachado sin consumir a ese costo (provisional: SCM no
         trae precio). Es lo que Gestión General compara contra lo posado en los libretos de SCM.
  Desde 2026-10-23: el material trae también, por código, el nombre y la unidad del producto en SGS («nombres») y el valor
         consumido y despachado sumado línea a línea («valor_codigo»), y lo de cliente sin código de Enel («sin_codigo»): con eso
         la suma de las filas reproduce al peso el valor de la caja. «recientes» acepta "prefijo": "DF" (tickets de mayor impacto).
  Desde 2026-10-25: el material trae también las BANDEJAS aprobadas de la familia por código («mov_codigo»: despachado, devuelto,
         fecha y nombre de la última bandeja de salida BAN-, la primera, cuántas bandejas), con la técnica del Liquidador 2 y del
         Control Mensual: despacho = líneas de bandejas «deliver» aprobadas y atendidas (o parciales); devolución = bandejas
         «devolution» aprobadas; borrador, por aprobar y canceladas no cuentan. El campo «despachado» de project.material NO resta lo
         devuelto (ejemplo: 57 despachados, 18 devueltos, 37 consumidos): por liquidar = despachado − devuelto − consumido.
         Lo de cliente sin código de Enel trae su por liquidar línea a línea. Si las bandejas no se pueden leer, «bandejas_error» y
         Gestión General no inventa el por liquidar.

Solo search_read / read: nunca escribe en SGS.
"""
import hmac
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

import balance_mo_bridge as bridge

TOKEN = os.getenv("SGO7_INTERNAL_TOKEN", "")
TTL_EXISTE = 6 * 3600
TTL_MO = 15 * 60
_cache: dict = {}
_cache_lock = threading.Lock()

ZONAS = [("LV&TLC", ("LINEA VIVA", "TELECONTROL", "LV&TLC")), ("UBATE", ("UBATE",)), ("GUAVIO", ("GACHETA", "GUAVIO")),
         ("ALMEIDAS", ("CHOCONTA", "ALMEIDAS"))]

bp = Blueprint("gestion_arbitro", __name__)


def _autorizado() -> bool:
    return bool(TOKEN) and hmac.compare_digest(request.headers.get("X-SGO7-Interno", ""), TOKEN)


def _norm(s) -> str:
    return (str(s or "").upper().replace("Á", "A").replace("É", "E").replace("Í", "I").replace("Ó", "O")
            .replace("Ú", "U").replace("\xa0", " ").strip())


def _zona(dep: str):
    d = _norm(dep)
    for z, claves in ZONAS:
        if any(k in d for k in claves):
            return z
    return None


def _m2o(v, idx=1):
    return v[idx] if isinstance(v, (list, tuple)) and len(v) > idx else ("" if idx else None)


def _limpio(cod: str) -> str:
    c = (cod or "").strip().upper()
    if c.startswith("LCL"):
        return c[3:].lstrip("_- ")
    return c


def _chunks(xs, n):
    xs = list(xs)
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _cache_get(clave, ttl):
    with _cache_lock:
        v = _cache.get(clave)
        if v and time.time() - v[0] < ttl:
            return v[1]
    return None


def _cache_put(clave, valor):
    with _cache_lock:
        _cache[clave] = (time.time(), valor)
        if len(_cache) > 6000:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:1500]:
                _cache.pop(k, None)


_CAMPOS_PADRE = ["id", "code", "name", "order_number", "approve_id", "department_id"]


def _padre_out(r: dict) -> dict:
    dep = _m2o(r.get("department_id"))
    return {"id": r["id"], "code": r.get("code") or "", "name": (r.get("name") or "")[:160], "order_number": r.get("order_number") or "",
            "supervisor": _m2o(r.get("approve_id")), "departamento": dep, "zona": _zona(dep)}


def existencia(cliente, codigos: list) -> dict:
    """codigo → {"existe", "metodo", "padres": [...]} con las reglas del Centro MO."""
    out, faltan = {}, []
    for cod in codigos:
        v = _cache_get(("e", cod), TTL_EXISTE)
        if v is not None:
            out[cod] = v
        else:
            faltan.append(cod)
    if faltan:
        por_on = defaultdict(list)
        for ch in _chunks(sorted({_limpio(c) for c in faltan}), 80):
            for r in cliente.search_read("project.dominion", [["order_number", "in", ch], ["parent_id", "=", False]], _CAMPOS_PADRE, limit=800):
                por_on[str(r.get("order_number") or "").strip().upper()].append(r)
        for cod in faltan:
            lim = _limpio(cod)
            hits = [r for r in por_on.get(lim, []) if str(r.get("order_number") or "").strip() not in ("0", "1")]
            metodo = "order_number" if hits else ""
            if not hits:
                try:
                    proy, metodo = bridge._buscar_proyecto_flexible(cliente, cod)   # DF truncado / número en el nombre
                except Exception:
                    proy, metodo = None, ""
                if proy and proy.get("id"):
                    hits = cliente.search_read("project.dominion", [["id", "=", proy["id"]]], _CAMPOS_PADRE, limit=1)
            v = {"existe": bool(hits), "metodo": metodo, "padres": [_padre_out(r) for r in hits][:4]}
            _cache_put(("e", cod), v)
            out[cod] = v
    return out


def mo_familias(cliente, padre_ids: list) -> dict:
    """padre_id → MO liquidada de la familia (padre + nodos, hasta 5 niveles)."""
    out, faltan = {}, []
    for pid in padre_ids:
        v = _cache_get(("m", pid), TTL_MO)
        if v is not None:
            out[pid] = v
        else:
            faltan.append(pid)
    if not faltan:
        return out
    raiz = {pid: pid for pid in faltan}
    frontera = list(faltan)
    for _ in range(5):
        if not frontera:
            break
        nuevos = []
        for ch in _chunks(frontera, 300):
            for h in cliente.search_read("project.dominion", [["parent_id", "in", ch]], ["id", "parent_id"], limit=10000):
                if h["id"] in raiz:
                    continue
                p = _m2o(h.get("parent_id"), 0)
                raiz[h["id"]] = raiz.get(p, p)
                nuevos.append(h["id"])
        frontera = nuevos
    nodos = defaultdict(int)
    for pid, r in raiz.items():
        if pid != r:
            nodos[r] += 1
    acc = {pid: {"mo_total": 0.0, "n_avances": 0, "por_mes": defaultdict(float), "lotes": {}} for pid in faltan}
    for ch in _chunks(list(raiz), 300):
        dets = cliente.search_read("project.activities.details", [["project_id", "in", ch], ["advanced_price", "!=", 0]],
                                   ["date", "advanced_price", "create_date", "create_uid", "project_id"], limit=30000)
        for d in dets:
            r = raiz.get(_m2o(d.get("project_id"), 0))
            if r is None:
                continue
            a = acc[r]
            v = float(d.get("advanced_price") or 0)
            a["mo_total"] += v
            a["n_avances"] += 1
            a["por_mes"][str(d.get("date") or "")[:7]] += v
            cd = str(d.get("create_date") or "")
            dia = cd[:10]
            lote = a["lotes"].setdefault(dia, {"dia": dia, "ts": cd, "mo": 0.0, "n": 0, "usuarios": set()})
            lote["mo"] += v
            lote["n"] += 1
            if cd and cd < lote["ts"]:
                lote["ts"] = cd
            u = _m2o(d.get("create_uid"))
            if u:
                lote["usuarios"].add(re.sub(r"\s+", " ", u).strip())
    for pid, a in acc.items():
        lotes = []
        for l in sorted(a["lotes"].values(), key=lambda x: x["ts"]):
            lotes.append({"dia": l["dia"], "ts": l["ts"].replace(" ", "T") + ("+00:00" if l["ts"] else ""), "mo": round(l["mo"], 2),
                          "n": l["n"], "usuarios": sorted(l["usuarios"])})
        v = {"mo_total": round(a["mo_total"], 2), "n_avances": a["n_avances"], "nodos": nodos.get(pid, 0),
             "por_mes": {k: round(x, 2) for k, x in sorted(a["por_mes"].items()) if k}, "lotes": lotes,
             "ultima_liq": lotes[-1] if lotes else None}
        _cache_put(("m", pid), v)
        out[pid] = v
    return out


TTL_COSTO = 6 * 3600


def _arbol(cliente, padre_ids: list) -> dict:
    """padre_id → ids de la familia (el padre y sus nodos, hasta 5 niveles). Caché corta, como la MO."""
    out, faltan = {}, []
    for pid in padre_ids:
        v = _cache_get(("t", pid), TTL_MO)
        if v is not None:
            out[pid] = v
        else:
            faltan.append(pid)
    if not faltan:
        return out
    raiz = {pid: pid for pid in faltan}
    frontera = list(faltan)
    for _ in range(5):
        if not frontera:
            break
        nuevos = []
        for ch in _chunks(frontera, 300):
            for h in cliente.search_read("project.dominion", [["parent_id", "in", ch]], ["id", "parent_id"], limit=10000):
                if h["id"] in raiz:
                    continue
                p = _m2o(h.get("parent_id"), 0)
                raiz[h["id"]] = raiz.get(p, p)
                nuevos.append(h["id"])
        frontera = nuevos
    fam = defaultdict(list)
    for n, r in raiz.items():
        fam[r].append(n)
    for pid in faltan:
        _cache_put(("t", pid), fam.get(pid, [pid]))
        out[pid] = fam.get(pid, [pid])
    return out


def _costos(cliente, prod_ids: list) -> dict:
    """Product.product id → standard_price (costo de SGS). De paso guarda en la caché la unidad de medida (("u", id))."""
    out, faltan = {}, []
    for pid in prod_ids:
        v = _cache_get(("c", pid), TTL_COSTO)
        if v is not None and _cache_get(("u", pid), TTL_COSTO) is not None:
            out[pid] = v
        else:
            faltan.append(pid)
    for ch in _chunks(faltan, 200):
        for p in cliente.read("product.product", ch, ["standard_price", "uom_id"]):
            v = float(p.get("standard_price") or 0)
            _cache_put(("c", p["id"]), v)
            _cache_put(("u", p["id"]), _m2o(p.get("uom_id")) or "")
            out[p["id"]] = v
    return out


_CAMPOS_BAN = ["id", "name", "type", "state", "state_picking", "date", "create_date", "write_date"]
_LIM_BAN, _LIM_BAN_LIN = 20000, 100000


def _dia_co(ts) -> str | None:
    """«2026-08-25 22:21:08» (UTC de Odoo) → «2026-08-25» en hora Colombia (UTC−5)."""
    t = str(ts or "")[:19]
    try:
        return (datetime.strptime(t, "%Y-%m-%d %H:%M:%S") - timedelta(hours=5)).strftime("%Y-%m-%d")
    except ValueError:
        return t[:10] or None


def _bandejas(cliente, nodos: list) -> dict:
    """project.material id → lo que movieron las bandejas APROBADAS de esos nodos (técnica del Liquidador 2, sgs_checker):
    {"d": despachado (deliver atendido / parcial), "v": devuelto (devolution), "f": fecha de la última bandeja de salida, "b": su nombre
    (BAN-…), "f0": la primera, "nd" / "nv": bandejas de salida / de devolución}. La fecha de salida es la de APROBACIÓN de la bandeja por
    almacén (write_date, como «fecha real de salida» del Control Mensual; si no, create_date), en hora Colombia: el campo «date» de la bandeja
    es una fecha programada y muchas veces futura (una bandeja creada y aprobada el 06/10 puede traer «date» 07/11). Falla en voz alta si una consulta
    llega al límite (un resultado truncado daría un por liquidar falso)."""
    trays, lineas = {}, []
    ctx = {"allowed_company_ids": [1]}
    for ch in _chunks(nodos, 300):
        ts = cliente._call_kw("project.tray", "search_read", [[["project_id", "in", ch]]], {"fields": _CAMPOS_BAN, "limit": _LIM_BAN, "context": ctx})
        ls = cliente._call_kw("project.tray.material", "search_read", [[["project_id", "in", ch]]],
                              {"fields": ["project_tray_id", "project_material_id", "deliver_quantity"], "limit": _LIM_BAN_LIN, "context": ctx})
        if len(ts) >= _LIM_BAN or len(ls) >= _LIM_BAN_LIN:
            raise RuntimeError("consulta de bandejas truncada (llegó al límite)")
        trays.update({t["id"]: t for t in ts})
        lineas += ls
    out: dict = {}
    for tm in lineas:
        t = trays.get(_m2o(tm.get("project_tray_id"), 0)) or {}
        pm = _m2o(tm.get("project_material_id"), 0)
        if pm is None or str(t.get("state") or "").lower() not in ("approved", "aprobado"):
            continue
        q = float(tm.get("deliver_quantity") or 0)
        x = out.setdefault(pm, {"d": 0.0, "v": 0.0, "f": None, "b": "", "f0": None, "nd": set(), "nv": set()})
        nombre = str(t.get("name") or "").strip() or f"#{t.get('id')}"
        fecha = _dia_co(t.get("write_date") or t.get("create_date"))
        sp = str(t.get("state_picking") or "").lower()
        if str(t.get("type") or "").lower() == "devolution":
            x["v"] += q
            x["nv"].add(nombre)
        elif sp in ("atendido", "parcial") or not sp:
            x["d"] += q
            x["nd"].add(nombre)
            if fecha and q:
                if x["f"] is None or fecha > x["f"] or (fecha == x["f"] and nombre > x["b"]):
                    x["f"], x["b"] = fecha, nombre
                if x["f0"] is None or fecha < x["f0"]:
                    x["f0"] = fecha
    return out


def _nombre_producto(display: str, cod: str) -> str:
    """«[330666] CABLE ACSR 2 AWG» → «CABLE ACSR 2 AWG» (el código ya va aparte)."""
    s = re.sub(r"\s+", " ", re.sub(r"^\s*\[[^\]]*\]\s*", "", str(display or ""))).strip()
    return (s or str(display or cod or ""))[:80]


def mat_familias(cliente, padre_ids: list) -> dict:
    """padre_id → material de la familia: {por_codigo {cód Enel: [despachado, consumido, costo]}, consumido_valor,
    pendiente_valor (despachado sin consumir, sin restar devoluciones: se conserva), propio_consumido_valor (material de Dominion), n_lineas,
    leido; bandejas (True si se leyeron), mov_codigo {cód: {d, v, dc, f, b, f0, nd, nv}} (d / v de las bandejas aprobadas, dc el campo
    «devuelto» de project.material, para contrastar) y en sin_codigo neto_valor, por_liquidar_valor y sobre_valor línea a línea}."""
    out, faltan = {}, []
    for pid in padre_ids:
        v = _cache_get(("x", pid), TTL_MO)
        if v is not None:
            out[pid] = v
        else:
            faltan.append(pid)
    if not faltan:
        return out
    arbol = _arbol(cliente, faltan)
    de_quien = {n: pid for pid, ns in arbol.items() for n in ns}
    lineas = []
    for ch in _chunks(list(de_quien), 300):
        lineas += cliente._call_kw("project.material", "search_read", [[["project_id", "in", ch]]],
                                   {"fields": ["project_id", "product_id", "despachado", "devuelto", "advance", "is_customer_material", "customer_default_code"],
                                    "limit": 40000, "context": {"allowed_company_ids": [1]}})
    costo = _costos(cliente, sorted({_m2o(l.get("product_id"), 0) for l in lineas if l.get("product_id")}))
    try:                                         # si las bandejas fallan, el material sigue (sin por liquidar)
        ban, ban_err = _bandejas(cliente, list(de_quien)), None
    except Exception as e:
        ban, ban_err = None, str(e)[:160]
    acc = {pid: {"por_codigo": {}, "consumido_valor": 0.0, "pendiente_valor": 0.0, "propio_consumido_valor": 0.0, "n_lineas": 0, "leido": True,
                 "valor_codigo": {}, "nombres": {}, "mov_codigo": {}, "bandejas": ban is not None,
                 "sin_codigo": {"consumido_valor": 0.0, "pendiente_valor": 0.0, "n_lineas": 0, "neto_valor": 0.0, "por_liquidar_valor": 0.0, "sobre_valor": 0.0}}
           for pid in faltan}
    if ban_err:
        for a in acc.values():
            a["bandejas_error"] = ban_err
    for l in lineas:
        pid = de_quien.get(_m2o(l.get("project_id"), 0))
        if pid is None:
            continue
        a = acc[pid]
        a["n_lineas"] += 1
        prod = _m2o(l.get("product_id"), 0)
        c = costo.get(prod, 0.0)
        desp, cons = float(l.get("despachado") or 0), float(l.get("advance") or 0)
        if not l.get("is_customer_material"):
            a["propio_consumido_valor"] += cons * c
            continue
        a["consumido_valor"] += cons * c
        a["pendiente_valor"] += max(desp - cons, 0) * c
        cod = str(l.get("customer_default_code") or "").strip()
        if cod and cod.lower() != "false":
            x = a["por_codigo"].setdefault(cod, [0.0, 0.0, 0.0])
            x[0] += desp
            x[1] += cons
            x[2] = max(x[2], c)
            v = a["valor_codigo"].setdefault(cod, [0.0, 0.0])        # valor línea a línea (reproduce consumido_valor)
            v[0] += cons * c
            v[1] += desp * c
            if cod not in a["nombres"]:
                a["nombres"][cod] = [_nombre_producto(_m2o(l.get("product_id")), cod), _cache_get(("u", prod), TTL_COSTO) or ""]
            if ban is not None:                  # bandejas aprobadas del código (y el campo «devuelto», para contrastar)
                b = ban.get(l.get("id")) or {}
                m = a["mov_codigo"].setdefault(cod, {"d": 0.0, "v": 0.0, "dc": 0.0, "f": None, "b": "", "f0": None, "nd": set(), "nv": set()})
                m["d"] += float(b.get("d") or 0)
                m["v"] += float(b.get("v") or 0)
                m["dc"] += float(l.get("devuelto") or 0)
                if b.get("f") and (m["f"] is None or b["f"] > m["f"] or (b["f"] == m["f"] and b["b"] > m["b"])):
                    m["f"], m["b"] = b["f"], b["b"]
                if b.get("f0") and (m["f0"] is None or b["f0"] < m["f0"]):
                    m["f0"] = b["f0"]
                m["nd"] |= b.get("nd") or set()
                m["nv"] |= b.get("nv") or set()
        else:
            s = a["sin_codigo"]
            s["consumido_valor"] += cons * c
            s["pendiente_valor"] += max(desp - cons, 0) * c
            s["n_lineas"] += 1
            if ban is not None:                  # sin código no hay «código» que juntar: el por liquidar va línea a línea
                b = ban.get(l.get("id")) or {}
                neto = float(b.get("d") or 0) - float(b.get("v") or 0)
                s["neto_valor"] += neto * c
                s["por_liquidar_valor"] += max(neto - cons, 0) * c
                s["sobre_valor"] += max(cons - neto, 0) * c
    for pid, a in acc.items():
        a["por_codigo"] = {k: [round(v[0], 3), round(v[1], 3), round(v[2], 2)] for k, v in sorted(a["por_codigo"].items())}
        a["valor_codigo"] = {k: [round(v[0], 2), round(v[1], 2)] for k, v in sorted(a["valor_codigo"].items())}
        a["mov_codigo"] = {k: {"d": round(v["d"], 3), "v": round(v["v"], 3), "dc": round(v["dc"], 3), "f": v["f"], "b": v["b"], "f0": v["f0"],
                               "nd": len(v["nd"]), "nv": len(v["nv"])} for k, v in sorted(a["mov_codigo"].items())}
        for k in ("consumido_valor", "pendiente_valor", "neto_valor", "por_liquidar_valor", "sobre_valor"):
            a["sin_codigo"][k] = round(a["sin_codigo"][k], 2)
        for k in ("consumido_valor", "pendiente_valor", "propio_consumido_valor"):
            a[k] = round(a[k], 2)
        _cache_put(("x", pid), a)
        out[pid] = a
    return out


def registrar(app, get_store, lock):
    @bp.route("/interno/gestion/sgs", methods=["POST"])
    def gestion_sgs():
        if not _autorizado():
            return jsonify({"error": "no autorizado"}), 401
        d = request.get_json(force=True) or {}
        if d.get("recientes"):
            # los LCL de los proyectos padre más recientes de SGS (solo lectura). Lo usa la prueba de Gestión General para
            # encontrar un LCL real que no sea caso ni esté en ninguna tabla (con más de mil casos ya no quedan en el Centro MO).
            pref = "DF" if str(d.get("prefijo") or "").upper() == "DF" else "63"      # también tickets DF (mayor impacto)
            try:
                cliente = bridge._sgs_cliente()
                rs = cliente._call_kw("project.dominion", "search_read", [[["parent_id", "=", False], ["order_number", "=like", f"{pref}%"]]],
                                      {"fields": ["order_number"], "limit": max(1, min(int(d.get("recientes") or 0), 500)), "order": "id desc"})
            except Exception as e:
                return jsonify({"error": f"SGS no disponible: {str(e)[:120]}"}), 503
            return jsonify({"recientes": [str(r.get("order_number") or "").strip() for r in rs if str(r.get("order_number") or "").strip()]})
        codigos = [str(c).strip().upper() for c in (d.get("codigos") or []) if str(c).strip()][:400]
        if not codigos:
            return jsonify({"codigos": {}})
        try:
            cliente = bridge._sgs_cliente()
        except Exception as e:
            return jsonify({"error": f"SGS no disponible: {str(e)[:120]}"}), 503
        try:
            ex = existencia(cliente, codigos)
            # copia: la MO y el material se cuelgan de cada padre de la respuesta, no de la caché de existencia
            ex = {c: dict(v, padres=[dict(p) for p in v["padres"]]) for c, v in ex.items()}
            if d.get("detalle"):
                ids = sorted({p["id"] for v in ex.values() for p in v["padres"]})
                mo = mo_familias(cliente, ids)
                for v in ex.values():
                    for p in v["padres"]:
                        p.update(mo.get(p["id"]) or {})
            if d.get("mat"):                     # material de cada familia (solo lectura); si falla, la MO sigue
                ids = sorted({p["id"] for v in ex.values() for p in v["padres"]})
                try:
                    mat, err = mat_familias(cliente, ids), None
                except Exception as e:
                    mat, err = {}, str(e)[:160]
                for v in ex.values():
                    for p in v["padres"]:
                        if p["id"] in mat:
                            p["mat"] = mat[p["id"]]
                        elif err:
                            p["mat_error"] = err
        except Exception as e:
            return jsonify({"error": f"Error leyendo SGS: {str(e)[:160]}"}), 502
        return jsonify({"codigos": ex})

    @bp.route("/interno/gestion/registros")
    def gestion_registros():
        if not _autorizado():
            return jsonify({"error": "no autorizado"}), 401
        campos = ("id", "entry_id", "conversacion", "asunto", "fecha", "detectado_en", "codigo", "codigo_fuente", "codigo_validado",
                  "lcl", "pdl", "tipo", "estado", "carril", "fecha_ejecucion", "remitente", "remitente_email", "completado_en", "faltantes")
        with lock:
            regs = [{k: r.get(k) for k in campos} | {"n_filas_mo": len(r.get("filas_mo") or []),
                                                      "nodos": sorted({str(f.get("nodo")) for f in (r.get("filas_mo") or []) if f.get("nodo")})[:60],
                                                      "lideres": (r.get("lideres") or [])[:8]}
                    for r in get_store().values()]
        return jsonify({"registros": regs})

    app.register_blueprint(bp)
