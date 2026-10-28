"""
sgs_materiales.py — Análisis extendido de MATERIALES (Zona VII)
==============================================================
Fork ampliado del "Automático por mes" del verificador (analisis_mes), con:
  • Rango de fechas arbitrario (varios meses) en vez de un solo mes.
  • Filtro por MACRO-ZONAS configurables (Ubaté, Chocontá, Gachetá, Línea Viva,
    Otros) en vez de Ubaté fijo. Filtro por supervisor opcional.
  • Fecha REAL de salida de almacén vía project.tray.picking_id → stock.picking.date_done
    (requiere la cuenta de lectura ampliada). Fallback: write_date de la bandeja.
  • COSTO de los materiales pendientes vía product.product.standard_price →
    valor pendiente = cantidad pendiente × costo unitario.
  • Agregados de analítica para el dashboard (por supervisor, zona, líder,
    buckets de antigüedad, evolución mensual, totales y valor pendiente).

Conserva la técnica de bandejas del liquidador2 (_estado_materiales_por_nodo),
ultra-precisa: recalcula el neto desde project.tray.material y clasifica por material.
"""

import re
import time
from collections import defaultdict
from datetime import datetime

from sgs_readonly_client import SGSReadOnlyClient
import config as cfg

# ── Campos de consulta ─────────────────────────────────────────────────────────
_CAMPOS_DOM = [
    "id", "code", "name", "display_name", "nodo", "status",
    "parent_id", "order_number", "pdl", "odm", "responsible_id", "approve_id",
    "partner_id", "formula_id", "department_id",
]
_CAMPOS_ACT_DET = [
    "id", "project_id", "date", "advanced_price",
    "partner_id", "state", "name", "production_type", "create_uid",
]
_CAMPOS_MAT_DET = ["id", "project_material_id", "amount"]
# project.tray con enlace a stock.picking (fecha real de almacén)
_CAMPOS_TRAY_AUDIT = ["id", "name", "type", "state", "state_picking",
                      "partner_id", "date", "create_date", "write_date", "picking_id"]
_CAMPOS_TRAY_MAT   = ["id", "project_id", "project_tray_id", "project_material_id", "deliver_quantity"]
_AUDIT_EXCL        = frozenset({"canceled", "canceled_partial", "to_approve"})

MESES_ES = {
    1: "ENERO", 2: "FEBRERO", 3: "MARZO", 4: "ABRIL", 5: "MAYO", 6: "JUNIO",
    7: "JULIO", 8: "AGOSTO", 9: "SEPTIEMBRE", 10: "OCTUBRE", 11: "NOVIEMBRE", 12: "DICIEMBRE",
}


# ── Conexión ───────────────────────────────────────────────────────────────────
def _conectar() -> SGSReadOnlyClient:
    db = SGSReadOnlyClient.detectar_db(cfg.SGS_URL)
    c  = SGSReadOnlyClient(cfg.SGS_URL, db, cfg.SGS_LOGIN, cfg.SGS_PASSWORD)
    c.autenticar()
    return c


# ── Utilidades (heredadas del verificador) ─────────────────────────────────────
def _fmt_fecha(raw) -> str:
    if not raw:
        return ""
    s = str(raw).strip()
    for largo, fmt_in, fmt_out in [
        (19, "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"),
        (10, "%Y-%m-%d",          "%d/%m/%Y"),
    ]:
        try:
            return datetime.strptime(s[:largo], fmt_in).strftime(fmt_out)
        except ValueError:
            continue
    return s[:10]


def _max_fecha(fechas: list) -> str:
    validas = [str(f) for f in fechas if f]
    return _fmt_fecha(max(validas)) if validas else ""


def _pid(raw):
    if isinstance(raw, (list, tuple)) and raw:
        return raw[0] if raw[0] else None
    if isinstance(raw, int):
        return raw
    return None


def _nombre_partner(raw) -> str:
    if isinstance(raw, (list, tuple)) and len(raw) > 1 and raw[1]:
        return " ".join(str(raw[1]).split()).strip()
    return ""


def _nombre_material(raw) -> str:
    nombre = _nombre_partner(raw)
    return re.sub(r"^\[[^\]]*\]\s*", "", nombre).strip()


def _dias_desde(fecha_raw):
    if not fecha_raw:
        return None
    s = str(fecha_raw).strip()
    for largo, fmt in [(19, "%Y-%m-%d %H:%M:%S"), (10, "%Y-%m-%d")]:
        try:
            d = datetime.strptime(s[:largo], fmt)
            return max(0, (datetime.now() - d).days)
        except ValueError:
            continue
    return None


def _lider_desde_splits(splits: dict):
    items = [(n, q) for n, q in splits.items() if n and q > 0.001]
    if not items:
        return "", []
    total = sum(q for _, q in items)
    items.sort(key=lambda x: x[1], reverse=True)
    detalle = [{"nombre": n, "neto": round(q, 2),
                "pct": round(q / total * 100, 1) if total else 0.0}
               for n, q in items]
    if len(detalle) == 1:
        return detalle[0]["nombre"], detalle
    texto = " / ".join(f"{d['nombre']} ({d['pct']:.0f}%)" for d in detalle)
    return texto, detalle


def _acum_bandeja(acc: dict, nombre: str, qty: float, fecha, lider: str) -> None:
    e = acc.get(nombre)
    if e is None:
        acc[nombre] = {"qty": qty, "fecha": _fmt_fecha(fecha) if fecha else "", "lider": lider or ""}
    else:
        e["qty"] += qty
        if fecha and not e["fecha"]:
            e["fecha"] = _fmt_fecha(fecha)
        if lider and not e["lider"]:
            e["lider"] = lider


def _bandejas_a_lista(acc: dict) -> list:
    out = [{"nombre": n, "qty": round(v["qty"], 2),
            "fecha": v.get("fecha", ""), "lider": v.get("lider", "")}
           for n, v in acc.items() if abs(v["qty"]) > 0.001]
    out.sort(key=lambda b: b["nombre"])
    return out


def _limpio(val) -> str:
    if not val:
        return ""
    s = str(val).strip().upper()
    if s in ("N/A", "NA", "FALSE", "NONE", "-", "0"):
        return ""
    return str(val).strip()


def _resolver_supervisor(responsible_raw) -> str:
    supervisores = getattr(cfg, "SUPERVISORES_EQUIPO", [])
    mapa         = getattr(cfg, "MAPA_SUPERVISOR", {})
    if not responsible_raw:
        return "?"
    if isinstance(responsible_raw, (list, tuple)) and len(responsible_raw) > 1:
        nombre_sgs = str(responsible_raw[1]).upper().strip()
    else:
        nombre_sgs = str(responsible_raw).upper().strip()
    for sup in supervisores:
        if sup.upper() == nombre_sgs or sup.upper() in nombre_sgs:
            return sup
    palabras = set(nombre_sgs.split())
    for sup in supervisores:
        for p in sup.upper().split():
            if p in palabras:
                return sup
    for clave, valor in mapa.items():
        if clave.upper() in nombre_sgs:
            return valor
    return nombre_sgs


def _dep_name(dom: dict) -> str:
    dep_raw = dom.get("department_id")
    if isinstance(dep_raw, (list, tuple)) and len(dep_raw) > 1:
        return str(dep_raw[1])
    return str(dep_raw or "")


def _norm(s: str) -> str:
    return (s or "").upper().replace("Á", "A").replace("É", "E").replace("Í", "I") \
        .replace("Ó", "O").replace("Ú", "U").replace("\xa0", " ").strip()


def _zona_macro(dom: dict) -> str:
    """Macro-zona de un nodo según su department_id (palabra clave, prioridad)."""
    name = _norm(_dep_name(dom))
    for z in cfg.MACRO_ZONAS:
        for kw in z["keywords"]:
            if _norm(kw) in name:
                return z["id"]
    return "OTROS"


def _en_zonas(dom: dict, zonas_set) -> bool:
    if not zonas_set:          # vacío = todas
        return True
    return _zona_macro(dom) in zonas_set


def _pdl_ot(dom: dict) -> str:
    code = (_limpio(dom.get("pdl")) or _limpio(dom.get("odm")) or
            _limpio(dom.get("order_number")) or _limpio(dom.get("nodo")) or "")
    if not code:
        nombre = dom.get("name") or ""
        partes = nombre.split("//")
        if partes:
            code = partes[0].strip()
    return code or dom.get("code", "")


def _buscar_lcl(*doms) -> str:
    for dom in doms:
        if not dom:
            continue
        for campo in (dom.get("odm"), dom.get("order_number"), dom.get("pdl"),
                      dom.get("name"), dom.get("display_name")):
            m = re.search(r"6300\d+", str(campo or ""))
            if m:
                return m.group(0)
    return "N/A"


def _relacionar_pdl(dom: dict, padre: dict = None):
    pdl_ot = _pdl_ot(dom)
    if (not pdl_ot or pdl_ot == dom.get("code", "")) and padre:
        alt = _pdl_ot(padre)
        if alt:
            pdl_ot = alt
    if str(pdl_ot).upper().startswith("DF"):
        return pdl_ot, "mayor_impacto", "N/A"
    lcl = _buscar_lcl(dom, padre) if padre else _buscar_lcl(dom)
    if lcl != "N/A":
        return pdl_ot, "programado", lcl
    return pdl_ot, "otro", "N/A"


def _safe_search_global(c, model, domain, fields, limit):
    try:
        return c.search_read(model, domain, fields, limit=limit)
    except Exception:
        return []


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _rango(fecha_inicio: str, fecha_fin: str):
    """Devuelve (inicio_str, fin_excl_str) normalizados a 'YYYY-MM-DD'; fin es exclusivo (+1 día)."""
    di = datetime.strptime(fecha_inicio[:10], "%Y-%m-%d")
    df = datetime.strptime(fecha_fin[:10], "%Y-%m-%d")
    if df < di:
        di, df = df, di
    fin_excl = df.replace(hour=0, minute=0, second=0)
    # fin exclusivo = día siguiente
    from datetime import timedelta
    fin_excl = (fin_excl + timedelta(days=1)).strftime("%Y-%m-%d")
    return di.strftime("%Y-%m-%d"), fin_excl


def _mes_de(fecha_raw) -> str:
    s = str(fecha_raw or "")[:7]
    return s if re.match(r"^\d{4}-\d{2}$", s) else ""


# ── Costos de materiales (cuenta de lectura ampliada) ──────────────────────────────────────
def _costos_por_producto(c, product_ids) -> dict:
    """{product_id: standard_price} en batch. Vacío si los costos están deshabilitados/bloqueados."""
    if not getattr(cfg, "USAR_COSTOS", True) or not product_ids:
        return {}
    out = {}
    for chunk in _chunks(list(product_ids), 300):
        for p in _safe_search_global(c, "product.product", [["id", "in", chunk]],
                                     ["id", "standard_price"], 5000):
            out[p["id"]] = float(p.get("standard_price") or 0)
    return out


# ── Estado de materiales por nodo (técnica liquidador2 + almacén + costo) ──────
def _estado_materiales_por_nodo(c, node_ids) -> dict:
    node_ids = list(node_ids)
    base = {nid: {
        "n_mat": 0, "neto": 0.0, "advance": 0.0, "pendiente": 0.0,
        "n_con_mov": 0, "n_pend": 0, "estado_mat": "sin_materiales",
        "fecha_liq": "", "antiguedad_dias": None, "antiguedad_aprob_dias": None,
        "antiguedad_almacen_dias": None, "valor_pendiente": 0.0,
        "lider_mat": "", "lider_mat_detalle": [], "pendientes": [],
    } for nid in node_ids}
    if not node_ids:
        return base

    # 1) Materiales de todos los nodos
    mats = []
    for chunk in _chunks(node_ids, 300):
        mats += _safe_search_global(
            c, "project.material", [["project_id", "in", chunk]],
            ["id", "project_id", "product_code", "product_id", "advance", "write_date"], 50000)
    if not mats:
        return base

    mat_node, mat_info, pm_ids = {}, {}, []
    product_ids = set()
    for m in mats:
        pm_id = m["id"]
        nid   = _pid(m.get("project_id"))
        if nid is None:
            continue
        pm_ids.append(pm_id)
        mat_node[pm_id] = nid
        mat_info[pm_id] = m
        base[nid]["n_mat"] += 1
        prod = _pid(m.get("product_id"))
        if prod:
            product_ids.add(prod)

    # 2) advance real = Σ project.material.details.amount
    advance_by_pm = defaultdict(float)
    for chunk in _chunks(pm_ids, 500):
        for d in _safe_search_global(
                c, "project.material.details",
                [["project_material_id", "in", chunk], ["amount", ">", 0]],
                _CAMPOS_MAT_DET, 200000):
            ref = d.get("project_material_id")
            pm_id = ref[0] if isinstance(ref, (list, tuple)) else ref
            if pm_id:
                advance_by_pm[pm_id] += float(d.get("amount") or 0)

    # 3) Bandejas por nodo (estado / tipo / picking_id)
    tray_by_id = {}
    for chunk in _chunks(node_ids, 300):
        for t in _safe_search_global(c, "project.tray", [["project_id", "in", chunk]],
                                     _CAMPOS_TRAY_AUDIT, 20000):
            tray_by_id[t["id"]] = t

    # 4) Líneas de bandeja → despacho / devolución (y por líder, fechas, pickings)
    despacho, devolucion = defaultdict(float), defaultdict(float)
    desp_lider = defaultdict(lambda: defaultdict(float))
    dev_lider  = defaultdict(lambda: defaultdict(float))
    desp_fechas       = defaultdict(list)   # tray.date
    desp_fechas_aprob = defaultdict(list)   # tray.write_date
    desp_pickings     = defaultdict(set)    # stock.picking ids del despacho
    ban_desp, ban_dev = defaultdict(dict), defaultdict(dict)
    picking_ids_all = set()

    for chunk in _chunks(node_ids, 300):
        for tm in _safe_search_global(c, "project.tray.material", [["project_id", "in", chunk]],
                                      _CAMPOS_TRAY_MAT, 100000):
            pm_ref = tm.get("project_material_id")
            pm_id  = pm_ref[0] if isinstance(pm_ref, (list, tuple)) else pm_ref
            tid_ref = tm.get("project_tray_id")
            tid     = tid_ref[0] if isinstance(tid_ref, (list, tuple)) else tid_ref
            tray    = tray_by_id.get(tid, {})
            state   = str(tray.get("state") or "").lower()
            sp      = str(tray.get("state_picking") or "").lower()
            ttype   = str(tray.get("type") or "").lower()
            qty     = float(tm.get("deliver_quantity") or 0)
            lider   = _nombre_partner(tray.get("partner_id"))
            tname   = str(tray.get("name") or "").strip() or f"#{tid}"
            fecha   = tray.get("date") or tray.get("create_date")
            pick_id = _pid(tray.get("picking_id"))
            if state in _AUDIT_EXCL or state not in ("approved", "aprobado"):
                continue
            if ttype == "devolution":
                devolucion[pm_id] += qty
                if lider:
                    dev_lider[pm_id][lider] += qty
                _acum_bandeja(ban_dev[pm_id], tname, qty, fecha, lider)
            elif sp in ("atendido", "parcial") or not sp:
                despacho[pm_id] += qty
                if lider:
                    desp_lider[pm_id][lider] += qty
                if fecha:
                    desp_fechas[pm_id].append(str(fecha))
                fecha_ap = tray.get("write_date") or tray.get("create_date")
                if fecha_ap:
                    desp_fechas_aprob[pm_id].append(str(fecha_ap))
                if pick_id:
                    desp_pickings[pm_id].add(pick_id)
                    picking_ids_all.add(pick_id)
                _acum_bandeja(ban_desp[pm_id], tname, qty, fecha, lider)

    # 4b) Fecha REAL de almacén: stock.picking.date_done (cuenta de lectura ampliada)
    picking_date = {}
    if getattr(cfg, "USAR_FECHA_ALMACEN", True) and picking_ids_all:
        for chunk in _chunks(list(picking_ids_all), 300):
            for pk in _safe_search_global(c, "stock.picking", [["id", "in", chunk]],
                                          ["id", "date_done", "scheduled_date", "state"], 5000):
                d = pk.get("date_done") or pk.get("scheduled_date")
                if d:
                    picking_date[pk["id"]] = str(d)

    # 4c) Costos por producto
    costo_prod = _costos_por_producto(c, product_ids)

    # 5) Clasificar por material y agregar por nodo
    for pm_id in pm_ids:
        nid  = mat_node[pm_id]
        neto = round(despacho.get(pm_id, 0.0) - devolucion.get(pm_id, 0.0), 4)
        adv  = round(advance_by_pm.get(pm_id, 0.0), 4)
        pend = round(neto - adv, 4)
        if neto <= 0.001 and adv <= 0.001:
            continue

        nodo = base[nid]
        nodo["n_con_mov"] += 1
        nodo["neto"]      += neto
        nodo["advance"]   += adv
        if pend > 0.001:
            nodo["n_pend"]    += 1
            nodo["pendiente"] += pend
            splits = defaultdict(float)
            for ln, q in desp_lider.get(pm_id, {}).items():
                splits[ln] += q
            for ln, q in dev_lider.get(pm_id, {}).items():
                splits[ln] -= q
            lider_txt, lider_det = _lider_desde_splits(splits)
            fechas = desp_fechas.get(pm_id, [])
            fecha_min = min(fechas) if fechas else ""
            antig = _dias_desde(fecha_min) if fecha_min else None
            fechas_ap = desp_fechas_aprob.get(pm_id, [])
            fecha_ap_min = min(fechas_ap) if fechas_ap else ""
            antig_ap = _dias_desde(fecha_ap_min) if fecha_ap_min else None
            # Fecha real de almacén (date_done de los pickings de despacho)
            pdates = [picking_date[p] for p in desp_pickings.get(pm_id, set()) if p in picking_date]
            fecha_alm_min = min(pdates) if pdates else ""
            antig_alm = _dias_desde(fecha_alm_min) if fecha_alm_min else None
            # Costo / valor pendiente
            prod = _pid(mat_info[pm_id].get("product_id"))
            costo = costo_prod.get(prod, 0.0)
            valor = round(pend * costo, 2)
            nodo["valor_pendiente"] += valor
            nodo["pendientes"].append({
                "codigo":  str(mat_info[pm_id].get("product_code") or ""),
                "nombre":  _nombre_material(mat_info[pm_id].get("product_id")),
                "neto": neto, "advance": adv, "pendiente": pend,
                "lider": lider_txt, "lider_detalle": lider_det,
                "antiguedad_dias": antig,
                "fecha_despacho": _fmt_fecha(fecha_min) if fecha_min else "",
                "antiguedad_aprob_dias": antig_ap,
                "fecha_aprobacion": _fmt_fecha(fecha_ap_min) if fecha_ap_min else "",
                "antiguedad_almacen_dias": antig_alm,
                "fecha_almacen": _fmt_fecha(fecha_alm_min) if fecha_alm_min else "",
                "costo_unit": round(costo, 2),
                "valor_pendiente": valor,
                "bandejas_despacho":   _bandejas_a_lista(ban_desp.get(pm_id, {})),
                "bandejas_devolucion": _bandejas_a_lista(ban_dev.get(pm_id, {})),
            })
        wd = mat_info[pm_id].get("write_date")
        if adv > 0 and wd and str(wd) > (nodo.get("_wd_raw") or ""):
            nodo["_wd_raw"]   = str(wd)
            nodo["fecha_liq"] = _fmt_fecha(wd)

    # 6) Estado final por nodo + resúmenes
    for nid in node_ids:
        nodo = base[nid]
        nodo.pop("_wd_raw", None)
        if nodo["n_con_mov"] == 0:
            nodo["estado_mat"] = "sin_materiales"
        elif nodo["n_pend"] > 0:
            nodo["estado_mat"] = "pendiente"
        else:
            nodo["estado_mat"] = "completo"
        nodo["valor_pendiente"] = round(nodo["valor_pendiente"], 2)
        antigs    = [p["antiguedad_dias"] for p in nodo["pendientes"] if p.get("antiguedad_dias") is not None]
        antigs_ap = [p["antiguedad_aprob_dias"] for p in nodo["pendientes"] if p.get("antiguedad_aprob_dias") is not None]
        antigs_al = [p["antiguedad_almacen_dias"] for p in nodo["pendientes"] if p.get("antiguedad_almacen_dias") is not None]
        nodo["antiguedad_dias"]         = max(antigs) if antigs else None
        nodo["antiguedad_aprob_dias"]   = max(antigs_ap) if antigs_ap else None
        nodo["antiguedad_almacen_dias"] = max(antigs_al) if antigs_al else None
        agg = defaultdict(float)
        for p in nodo["pendientes"]:
            for d in p.get("lider_detalle", []):
                agg[d["nombre"]] += d["neto"]
        nodo["lider_mat"], nodo["lider_mat_detalle"] = _lider_desde_splits(agg)
    return base


# ── Caché en memoria ───────────────────────────────────────────────────────────
_CACHE: dict = {}


def _cache_key(inicio, fin, zonas, sups):
    return (inicio, fin, tuple(sorted(zonas)), tuple(sorted(sups)))


# ── Análisis principal ─────────────────────────────────────────────────────────
def analizar(fecha_inicio: str, fecha_fin: str,
             zonas=None, supervisores=None, use_cache=True) -> dict:
    """
    Analiza materiales por familia en un rango de fechas.
      zonas        : lista de ids de macro-zona (vacío/None = todas)
      supervisores : lista de supervisores a incluir (vacío/None = todos)
    Devuelve {ok, periodo, familias, agregados, totales...}.
    """
    zonas_set = set(zonas or [])
    sups_set  = set(s.upper() for s in (supervisores or []))

    try:
        inicio_str, fin_str = _rango(fecha_inicio, fecha_fin)
    except Exception:
        return {"ok": False, "error": "Fechas inválidas (usa YYYY-MM-DD)."}

    ck = _cache_key(inicio_str, fin_str, zonas_set, sups_set)
    if use_cache and ck in _CACHE:
        ts, data = _CACHE[ck]
        if time.time() - ts < getattr(cfg, "CACHE_TTL", 600):
            return {**data, "cache": True}

    try:
        c = _conectar()
    except Exception as e:
        return {"ok": False, "error": f"No se pudo conectar a SGS: {e}"}

    # PASO 1: avances del rango
    try:
        avances = c.search_read(
            "project.activities.details",
            [["date", ">=", inicio_str], ["date", "<", fin_str], ["advanced_price", "!=", 0]],
            _CAMPOS_ACT_DET, limit=50000,
        )
    except Exception as e:
        return {"ok": False, "error": f"Error al leer avances: {e}"}

    if not avances:
        return _vacio(inicio_str, fin_str)

    # PASO 2: agrupar por proyecto
    mo_by_pid    = defaultdict(float)
    dates_by_pid = defaultdict(list)
    lider_by_pid = {}
    liqs_by_pid  = defaultdict(dict)
    for av in avances:
        pid = _pid(av.get("project_id"))
        if not pid:
            continue
        mo_by_pid[pid] += float(av.get("advanced_price") or 0)
        if av.get("date"):
            dates_by_pid[pid].append(str(av["date"]))
        if pid not in lider_by_pid:
            p_raw = av.get("partner_id")
            if isinstance(p_raw, (list, tuple)) and len(p_raw) > 1:
                lider_by_pid[pid] = str(p_raw[1]).upper()
        uid_raw = av.get("create_uid")
        if uid_raw and isinstance(uid_raw, (list, tuple)) and len(uid_raw) > 1:
            uid_id = uid_raw[0]
            nombre = " ".join(str(uid_raw[1]).split()).upper()
            if uid_id not in liqs_by_pid[pid]:
                liqs_by_pid[pid][uid_id] = {"nombre": nombre, "mo": 0.0}
            liqs_by_pid[pid][uid_id]["mo"] += float(av.get("advanced_price") or 0)

    pids_todos = list(mo_by_pid.keys())

    # PASO 3: project.dominion en batch
    dom_by_pid = {}
    for chunk in _chunks(pids_todos, 300):
        try:
            for d in c.search_read("project.dominion", [["id", "in", chunk]],
                                   _CAMPOS_DOM, limit=len(chunk) + 10):
                dom_by_pid[d["id"]] = d
        except Exception as e:
            return {"ok": False, "error": f"Error al leer proyectos: {e}"}

    # PASO 4: filtrar por zona + supervisor (opcional)
    pids_filtrados = []
    for pid in pids_todos:
        dom = dom_by_pid.get(pid)
        if not dom or abs(mo_by_pid[pid]) < 1.0:
            continue
        if not _en_zonas(dom, zonas_set):
            continue
        if sups_set:
            sup = _resolver_supervisor(dom.get("approve_id") or dom.get("responsible_id"))
            if sup.upper() not in sups_set:
                continue
        pids_filtrados.append(pid)

    if not pids_filtrados:
        return _vacio(inicio_str, fin_str)

    # PASO 5: familias (padre + hijos) en batch
    parent_ids_set, pid_to_parent = set(), {}
    for pid in pids_filtrados:
        parent_id = _pid(dom_by_pid.get(pid, {}).get("parent_id")) or pid
        parent_ids_set.add(parent_id)
        pid_to_parent[pid] = parent_id

    faltantes = [p for p in parent_ids_set if p not in dom_by_pid]
    for chunk in _chunks(faltantes, 300):
        try:
            for p in c.read("project.dominion", chunk, _CAMPOS_DOM):
                dom_by_pid[p["id"]] = p
        except Exception:
            pass

    hijos_by_parent = defaultdict(list)
    for chunk in _chunks(list(parent_ids_set), 200):
        try:
            for h in c.search_read("project.dominion", [["parent_id", "in", chunk]],
                                   _CAMPOS_DOM, limit=5000):
                pp = _pid(h.get("parent_id"))
                if pp:
                    hijos_by_parent[pp].append(h)
                    dom_by_pid[h["id"]] = h
        except Exception:
            pass

    # PASO 6: estado de materiales por nodo
    all_node_ids = set(parent_ids_set)
    for hs in hijos_by_parent.values():
        for h in hs:
            all_node_ids.add(h["id"])
    estado_por_nodo = _estado_materiales_por_nodo(c, all_node_ids)

    # PASO 7: construir familias
    familias, procesados = [], set()
    for pid in pids_filtrados:
        parent_id = pid_to_parent[pid]
        if parent_id in procesados:
            continue
        procesados.add(parent_id)

        dom   = dom_by_pid.get(pid, {})
        padre = dom_by_pid.get(parent_id, dom)
        hijos = hijos_by_parent.get(parent_id, [])
        todos = [padre] + hijos

        nodos_mat = []
        for nodo in todos:
            est = estado_por_nodo.get(nodo["id"], {})
            nodos_mat.append({
                "pro_id": nodo.get("id"), "pro_code": nodo.get("code", ""),
                "nodo": nodo.get("nodo") or "",
                "n_mat": est.get("n_mat", 0), "advance": est.get("advance", 0.0),
                "balance": est.get("pendiente", 0.0), "despa": est.get("neto", 0.0),
                "fecha_liq": est.get("fecha_liq", ""),
                "estado_mat": est.get("estado_mat", "sin_materiales"),
                "antiguedad_dias": est.get("antiguedad_dias"),
                "antiguedad_aprob_dias": est.get("antiguedad_aprob_dias"),
                "antiguedad_almacen_dias": est.get("antiguedad_almacen_dias"),
                "valor_pendiente": est.get("valor_pendiente", 0.0),
                "lider_mat": est.get("lider_mat", ""),
                "pendientes": est.get("pendientes", []),
            })

        con_mat    = [r for r in nodos_mat if r["estado_mat"] != "sin_materiales"]
        pendientes = [r for r in nodos_mat if r["estado_mat"] == "pendiente"]
        clasificacion = "sin_materiales" if not con_mat else ("pendiente" if pendientes else "completo")

        mo_value = mo_by_pid.get(pid, 0.0)
        fecha_mo = _max_fecha(dates_by_pid.get(pid, []))
        mes_mo   = _mes_de(max(dates_by_pid.get(pid, [""])) if dates_by_pid.get(pid) else "")
        sup_raw  = padre.get("approve_id") or dom.get("approve_id") or dom.get("responsible_id")
        supervisor      = _resolver_supervisor(sup_raw)
        supervisor_full = _nombre_partner(sup_raw) or supervisor
        lider    = lider_by_pid.get(pid, "")
        liqs     = sorted(liqs_by_pid.get(pid, {}).values(), key=lambda x: x["mo"], reverse=True)
        display_code, tipo, lcl = _relacionar_pdl(dom, padre)
        zona_id  = _zona_macro(padre if _dep_name(padre) else dom)
        zona_dep = _dep_name(padre) or _dep_name(dom)

        agg_fam = defaultdict(float)
        antigs, antigs_ap, antigs_al = [], [], []
        valor_pend = 0.0
        for r in pendientes:
            valor_pend += r.get("valor_pendiente", 0.0)
            for p in r.get("pendientes", []):
                for d in p.get("lider_detalle", []):
                    agg_fam[d["nombre"]] += d["neto"]
                if p.get("antiguedad_dias") is not None:
                    antigs.append(p["antiguedad_dias"])
                if p.get("antiguedad_aprob_dias") is not None:
                    antigs_ap.append(p["antiguedad_aprob_dias"])
                if p.get("antiguedad_almacen_dias") is not None:
                    antigs_al.append(p["antiguedad_almacen_dias"])
        lider_liquidar, lider_liq_detalle = _lider_desde_splits(agg_fam)

        familias.append({
            "padre_id": padre.get("id"), "padre_code": padre.get("code", ""),
            "padre_name": padre.get("name", ""),
            "pdl_code": display_code, "lcl": lcl,
            "zona": zona_id, "zona_dep": zona_dep,
            "supervisor": supervisor, "supervisor_full": supervisor_full,
            "lider": lider, "mo_value": mo_value, "fecha_mo": fecha_mo, "mes_mo": mes_mo,
            "liquidadores": liqs, "clasificacion": clasificacion,
            "n_pendientes": len(pendientes), "n_con_mat": len(con_mat), "n_nodos": len(todos),
            "nodos_mat": nodos_mat, "tipo": tipo,
            "lider_liquidar": lider_liquidar, "lider_liq_detalle": lider_liq_detalle,
            "valor_pendiente": round(valor_pend, 2),
            "antiguedad_dias": max(antigs) if antigs else None,
            "antiguedad_aprob_dias": max(antigs_ap) if antigs_ap else None,
            "antiguedad_almacen_dias": max(antigs_al) if antigs_al else None,
        })

    _orden = {"pendiente": 0, "completo": 1, "sin_materiales": 2}
    familias.sort(key=lambda f: (_orden.get(f["clasificacion"], 3), -(f.get("valor_pendiente") or 0)))

    data = {
        "ok": True,
        "periodo": _periodo_str(inicio_str, fin_str),
        "fecha_inicio": inicio_str, "fecha_fin": fecha_fin[:10],
        "n_total": len(familias),
        "n_pendiente": sum(1 for f in familias if f["clasificacion"] == "pendiente"),
        "n_completo":  sum(1 for f in familias if f["clasificacion"] == "completo"),
        "n_sin_mat":   sum(1 for f in familias if f["clasificacion"] == "sin_materiales"),
        "n_programado": sum(1 for f in familias if f["tipo"] == "programado"),
        "n_mayor_impacto": sum(1 for f in familias if f["tipo"] == "mayor_impacto"),
        "mo_total": round(sum(f["mo_value"] for f in familias), 2),
        "valor_pendiente_total": round(sum(f["valor_pendiente"] for f in familias), 2),
        "familias": familias,
        "agregados": _agregados(familias),
        "capacidades": {
            "fecha_almacen": getattr(cfg, "USAR_FECHA_ALMACEN", True),
            "costos": getattr(cfg, "USAR_COSTOS", True),
        },
        "cache": False,
    }
    _CACHE[ck] = (time.time(), data)
    return data


def _periodo_str(inicio_str, fin_excl_str) -> str:
    di = datetime.strptime(inicio_str, "%Y-%m-%d")
    from datetime import timedelta
    dfin = datetime.strptime(fin_excl_str, "%Y-%m-%d") - timedelta(days=1)
    return f"{di.strftime('%d/%m/%Y')} – {dfin.strftime('%d/%m/%Y')}"


def _vacio(inicio_str, fin_str) -> dict:
    return {"ok": True, "periodo": _periodo_str(inicio_str, fin_str),
            "fecha_inicio": inicio_str, "fecha_fin": fin_str,
            "n_total": 0, "n_pendiente": 0, "n_completo": 0, "n_sin_mat": 0,
            "n_programado": 0, "n_mayor_impacto": 0,
            "mo_total": 0, "valor_pendiente_total": 0,
            "familias": [], "agregados": _agregados([]),
            "capacidades": {"fecha_almacen": getattr(cfg, "USAR_FECHA_ALMACEN", True),
                            "costos": getattr(cfg, "USAR_COSTOS", True)},
            "msg": "Sin proyectos en el periodo/zonas seleccionadas.", "cache": False}


# ── Agregados de analítica para el dashboard ──────────────────────────────────
def _bucket_antig(d):
    if d is None:
        return None
    if d <= 15:  return "0-15"
    if d <= 30:  return "16-30"
    if d <= 60:  return "31-60"
    return "60+"


def _zona_label(zid):
    for z in cfg.MACRO_ZONAS:
        if z["id"] == zid:
            return z["label"]
    return zid


def _agregados(familias: list) -> dict:
    by_sup, by_zona, by_lider = {}, {}, {}
    buckets = {"0-15": 0, "16-30": 0, "31-60": 0, "60+": 0}
    by_month = {}

    def _acc(d, key, mo, pend, valor):
        e = d.setdefault(key, {"n": 0, "n_pend": 0, "mo": 0.0, "valor_pend": 0.0})
        e["n"] += 1
        e["n_pend"] += 1 if pend else 0
        e["mo"] += mo
        e["valor_pend"] += valor

    for f in familias:
        pend = f["clasificacion"] == "pendiente"
        mo, valor = f.get("mo_value", 0.0), f.get("valor_pendiente", 0.0)
        _acc(by_sup,  f.get("supervisor_full") or f.get("supervisor") or "?", mo, pend, valor)
        _acc(by_zona, _zona_label(f.get("zona")), mo, pend, valor)
        m = f.get("mes_mo") or ""
        if m:
            e = by_month.setdefault(m, {"mo": 0.0, "n": 0, "n_pend": 0, "valor_pend": 0.0})
            e["mo"] += mo; e["n"] += 1; e["n_pend"] += 1 if pend else 0; e["valor_pend"] += valor
        if pend:
            d = f.get("antiguedad_almacen_dias")
            if d is None:
                d = f.get("antiguedad_aprob_dias")
            b = _bucket_antig(d)
            if b:
                buckets[b] += 1
            det = f.get("lider_liq_detalle") or []
            for ld in det:
                e = by_lider.setdefault(ld["nombre"], {"valor_pend": 0.0, "cant": 0.0, "n_fam": 0})
                e["cant"] += ld.get("neto", 0.0)
                e["n_fam"] += 1
            # valor pendiente del líder dominante (aprox: todo el valor de la familia al líder principal)
            if det:
                by_lider[det[0]["nombre"]]["valor_pend"] += valor

    def _top(d, key, n=12):
        items = sorted(d.items(), key=lambda kv: kv[1].get(key, 0), reverse=True)
        return [{"nombre": k, **v} for k, v in items[:n]]

    return {
        "por_supervisor": _top(by_sup, "mo", 20),
        "por_zona": [{"nombre": k, **v} for k, v in sorted(by_zona.items(), key=lambda kv: -kv[1]["mo"])],
        "top_lideres": _top(by_lider, "valor_pend", 12),
        "antiguedad_buckets": buckets,
        "por_mes": [{"mes": k, **v} for k, v in sorted(by_month.items())],
    }


# ── Zonas disponibles (para el selector del frontend) ─────────────────────────
def zonas_disponibles() -> dict:
    return {"ok": True, "zonas": [{"id": z["id"], "label": z["label"]} for z in cfg.MACRO_ZONAS]}
