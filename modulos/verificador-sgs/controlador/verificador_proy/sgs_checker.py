"""
sgs_checker.py — Consultas SGS para el Verificador de Proyectos
================================================================
Dos modos:
  1. Manual  → verificar_proyectos(codigos_raw)  — busca por código
  2. Auto    → analisis_mes(año, mes)             — consulta mes completo
                                                    con clasificación de materiales
"""

import sys
import os
import re
from collections import defaultdict
from datetime import datetime

# Path al cliente SGS (local en dev, mismo dir en VPS standalone)
_RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_RAIZ, "..", "SGS UI"))
sys.path.insert(0, _RAIZ)

from sgs_readonly_client import SGSReadOnlyClient
import config as cfg

# ── Campos de consulta ─────────────────────────────────────────────────────────

_CAMPOS_DOM = [
    "id", "code", "name", "display_name", "nodo", "status",
    "parent_id", "order_number", "pdl", "odm", "responsible_id", "approve_id",
    "partner_id", "formula_id", "department_id", "contract_id",
]

_CAMPOS_ACT = [
    "id", "project_id", "activity_code", "name", "amount", "advance", "balance",
]

_CAMPOS_ACT_DET = [
    "id", "project_id", "date", "advanced_price",
    "partner_id", "state", "name", "production_type", "create_uid",
]

_CAMPOS_MAT = [
    "id", "project_id", "product_code", "product_id",
    "advance", "balance", "despachado", "devuelto", "write_date",
]

_CAMPOS_MAT_DET = ["id", "project_material_id", "amount"]

_CAMPOS_TRAY = [
    "id", "project_id", "name", "type", "state",
    "date", "create_date", "state_picking",
]

# Auditoría precisa de materiales (técnica liquidador2 "Integridad post-liquidación")
_CAMPOS_TRAY_AUDIT = ["id", "name", "type", "state", "state_picking", "partner_id", "date", "create_date", "write_date"]
_CAMPOS_TRAY_MAT   = ["id", "project_id", "project_tray_id", "project_material_id", "deliver_quantity"]
_AUDIT_EXCL        = frozenset({"canceled", "canceled_partial", "to_approve"})

MESES_ES = {
    1: "ENERO", 2: "FEBRERO", 3: "MARZO", 4: "ABRIL",
    5: "MAYO", 6: "JUNIO", 7: "JULIO", 8: "AGOSTO",
    9: "SEPTIEMBRE", 10: "OCTUBRE", 11: "NOVIEMBRE", 12: "DICIEMBRE",
}

# Contrato vigente de Zona VII — casilla "Contract" (contract_id) de project.dominion.
# El contrato viejo ("Enel zona VII", id 5) cerró; el Excel del año se limita al nuevo.
# No se puede distinguir por código: hay CDOPPI y CDOPP2026R en ambos contratos.
CONTRATO_2026_ID     = getattr(cfg, "CONTRATO_NUEVO_ID", 0)
CONTRATO_2026_NOMBRE = getattr(cfg, "CONTRATO_NUEVO_NOMBRE", "")


# ── Utilidades ─────────────────────────────────────────────────────────────────

def _conectar() -> SGSReadOnlyClient:
    db = SGSReadOnlyClient.detectar_db(cfg.SGS_URL)
    c  = SGSReadOnlyClient(cfg.SGS_URL, db, cfg.SGS_LOGIN, cfg.SGS_PASSWORD)
    c.autenticar()
    return c


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


def _pid(raw) -> int | None:
    if isinstance(raw, (list, tuple)) and raw:
        return raw[0] if raw[0] else None
    if isinstance(raw, int):
        return raw
    return None


def _nombre_partner(raw) -> str:
    """Nombre legible de un campo many2one partner_id ([id, 'NOMBRE'])."""
    if isinstance(raw, (list, tuple)) and len(raw) > 1 and raw[1]:
        return " ".join(str(raw[1]).split()).strip()
    return ""


def _nombre_material(raw) -> str:
    """Descripción de un material desde product_id, sin el prefijo '[código] '."""
    nombre = _nombre_partner(raw)
    return re.sub(r"^\[[^\]]*\]\s*", "", nombre).strip()


def _dias_desde(fecha_raw) -> int | None:
    """Días transcurridos entre hoy y una fecha SGS ('YYYY-MM-DD...'). None si inválida."""
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


def _lider_desde_splits(splits: dict) -> tuple[str, list]:
    """
    Dado {nombre_lider: neto_despachado}, devuelve (texto, lista_ordenada).
    - 1 líder  → 'JUAN PEREZ'
    - varios   → 'JUAN PEREZ (70%) / PEDRO GOMEZ (30%)' (dominante primero)
    La lista es [{nombre, neto, pct}, ...] para el detalle del Excel.
    """
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


def _acum_bandeja(acc: dict, nombre: str, qty: float, fecha, lider: str,
                  fecha_aprob=None) -> None:
    """
    Acumula una línea de bandeja en {nombre: {qty, fecha, fecha_aprob, lider}}.

    `fecha_aprob` (write_date ≈ aprobación de almacén) se guarda además en crudo
    (`_aprob_raw`, ISO) porque es la que decide a qué mes pertenece la salida:
    comparar strings ISO es exacto, mientras que el `fecha` formateado DD/MM/AAAA
    no sirve para ordenar ni para acotar un rango de mes.
    """
    e = acc.get(nombre)
    if e is None:
        acc[nombre] = {"qty": qty, "fecha": _fmt_fecha(fecha) if fecha else "",
                       "fecha_aprob": _fmt_fecha(fecha_aprob) if fecha_aprob else "",
                       "_aprob_raw": str(fecha_aprob or ""),
                       "lider": lider or ""}
    else:
        e["qty"] += qty
        if fecha and not e["fecha"]:
            e["fecha"] = _fmt_fecha(fecha)
        if fecha_aprob and not e["fecha_aprob"]:
            e["fecha_aprob"] = _fmt_fecha(fecha_aprob)
            e["_aprob_raw"]  = str(fecha_aprob)
        if lider and not e["lider"]:
            e["lider"] = lider


def _bandejas_a_lista(acc: dict) -> list:
    """Convierte el dict de bandejas a lista ordenada por nombre con qty redondeada."""
    out = [{"nombre": n, "qty": round(v["qty"], 2),
            "fecha": v.get("fecha", ""), "fecha_aprob": v.get("fecha_aprob", ""),
            "aprob_raw": v.get("_aprob_raw", ""), "lider": v.get("lider", "")}
           for n, v in acc.items() if abs(v["qty"]) > 0.001]
    out.sort(key=lambda b: b["nombre"])
    return out


def _rango_mes(año: int, mes: int) -> tuple[str, str]:
    inicio = datetime(año, mes, 1)
    fin    = datetime(año + 1, 1, 1) if mes == 12 else datetime(año, mes + 1, 1)
    return inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")


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


def _es_ubate(dom: dict) -> bool:
    dep_raw  = dom.get("department_id")
    dep_name = dep_raw[1] if isinstance(dep_raw, (list, tuple)) and len(dep_raw) > 1 else str(dep_raw or "")
    dep_norm = dep_name.upper().replace("Á", "A").replace("É", "E").replace("\xa0", " ").strip()
    # Departamentos del equipo (cfg.DEPARTAMENTOS_EQUIPO): Ubaté MI/TP y, en el
    # contrato 2026, "NZ7 - Telecontrol" — también lo liquida el equipo.
    deptos = getattr(cfg, "DEPARTAMENTOS_EQUIPO", [])
    return any(d.upper() in dep_norm for d in deptos)


def _es_departamento_compartido(dom: dict) -> bool:
    """Departamento compartido con otro grupo (p. ej. Telecontrol ↔ Línea Viva):
    ahí solo cuenta lo que liquidó el equipo (cfg.LIQUIDADORES_EQUIPO)."""
    dep_raw  = dom.get("department_id")
    dep_name = dep_raw[1] if isinstance(dep_raw, (list, tuple)) and len(dep_raw) > 1 else str(dep_raw or "")
    dep_norm = dep_name.upper().replace("Á", "A").replace("É", "E").replace(" ", " ").strip()
    compartidos = getattr(cfg, "DEPARTAMENTOS_COMPARTIDOS", [])
    return any(d.upper() in dep_norm for d in compartidos)


def _liquidado_por_equipo(nombres_liq) -> bool:
    frags = [x.upper() for x in getattr(cfg, "LIQUIDADORES_EQUIPO", [])]
    return any(f in str(n).upper() for f in frags for n in nombres_liq)


def _nodo_de_familia_del_equipo(dom: dict, doms: dict, liqs: dict) -> bool:
    """
    Un nodo archivado en un departamento ajeno cuenta como del equipo si su PRO
    padre está en un departamento del equipo Y lo liquidó el equipo. Ver el
    rescate de `analisis_mes` (PASO 4) para los casos reales que lo motivan.
    """
    padre = doms.get(_pid(dom.get("parent_id")))
    if not padre or not _es_ubate(padre):
        return False
    return _liquidado_por_equipo(v["nombre"] for v in (liqs or {}).values())


def _pdl_de_dom(dom: dict) -> str:
    pdl = (
        _limpio(dom.get("pdl")) or
        _limpio(dom.get("odm")) or
        _limpio(dom.get("order_number")) or
        _limpio(dom.get("nodo")) or ""
    )
    if not pdl:
        nombre = dom.get("name") or ""
        partes = nombre.split("//")
        if partes:
            pdl = partes[0].strip()
    return pdl or dom.get("code", "")


def _pdl_ot(dom: dict) -> str:
    """
    Código PDL/OT del nodo, igual que el módulo central:
    pdl (CDOPP/CDOPPI) → odm → order_number → nodo → primer tramo del name → code.
    En los nodos PROGRAMADOS el `pdl` trae el CDOPP (ej. CDOPPI202681447).
    """
    code = (
        _limpio(dom.get("pdl")) or
        _limpio(dom.get("odm")) or
        _limpio(dom.get("order_number")) or
        _limpio(dom.get("nodo")) or ""
    )
    if not code:
        nombre = dom.get("name") or ""
        partes = nombre.split("//")
        if partes:
            code = partes[0].strip()
    return code or dom.get("code", "")


def _buscar_lcl(*doms) -> str:
    """Busca la LCL (6300...) en odm/order_number/pdl/name/display_name de los doms dados."""
    for dom in doms:
        if not dom:
            continue
        for campo in (dom.get("odm"), dom.get("order_number"), dom.get("pdl"),
                      dom.get("name"), dom.get("display_name")):
            m = re.search(r"6300\d+", str(campo or ""))
            if m:
                return m.group(0)
    return "N/A"


def _relacionar_pdl(dom: dict, padre: dict = None) -> tuple[str, str, str]:
    """
    Relaciona el código igual que el módulo central (balance), diferenciando
    PDL/OT y LCL:
      - PDL/OT (display)  = el código del nodo (CDOPP para PG, DF para MI),
                            tomado del nodo liquidado `dom` (que trae el CDOPP);
                            si el nodo no tiene, se usa el del `padre`.
      - DF...             → mayor_impacto, lcl="N/A".
      - Hay 6300 (en dom o padre) → programado, lcl=esa 6300.
      - Ni DF ni 6300     → otro.
    Devuelve (display_code, tipo, lcl).
    """
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


# ── Resolución de código de entrada ────────────────────────────────────────────

_RE_PRO  = re.compile(r"^PRO-\d+$",             re.IGNORECASE)
_RE_DF   = re.compile(r"^DF\d{10,14}$",         re.IGNORECASE)
_RE_LCL  = re.compile(r"^(LCL)?\d{8,13}$",      re.IGNORECASE)
_RE_PDL  = re.compile(r"^(CDOPP|CDOPPI|CDOPT)", re.IGNORECASE)


def _buscar_proyecto(c: SGSReadOnlyClient, codigo: str) -> dict | None:
    codigo = codigo.strip()
    if _RE_PRO.match(codigo):
        recs = c.search_read("project.dominion", [["code", "=", codigo]], _CAMPOS_DOM, limit=1)
        return recs[0] if recs else None
    if _RE_DF.match(codigo):
        for domain in (
            [["pdl", "=", codigo]],
            [["name", "ilike", codigo]],
            [["display_name", "ilike", codigo]],
        ):
            recs = c.search_read("project.dominion", domain, _CAMPOS_DOM, limit=20)
            if recs:
                roots = [r for r in recs if not _pid(r.get("parent_id"))]
                return roots[0] if roots else recs[0]
        return None
    if _RE_LCL.match(codigo):
        num = re.sub(r"(?i)^LCL", "", codigo)
        for busqueda in (num, codigo):
            recs = c.search_read("project.dominion", [["order_number", "=", busqueda]], _CAMPOS_DOM, limit=20)
            if recs:
                roots = [r for r in recs if not _pid(r.get("parent_id"))]
                return roots[0] if roots else recs[0]
        return None
    if _RE_PDL.match(codigo):
        dominios_pdl = [
            [["pdl",                 "=",     codigo]],
            [["code",                "=",     codigo]],
            [["order_number",        "=",     codigo]],
            [["pdl",                 "ilike", codigo]],
            [["name",                "ilike", codigo]],
            [["display_name",        "ilike", codigo]],
            [["formula_id.name",     "=",     codigo]],
            [["formula_id.name",     "ilike", codigo]],
        ]
        # Fallback: buscar solo el sufijo (e.g. "2026R03914" de "CDOPP2026R03914")
        sufijo = re.sub(r"^(?:CDOPPI|CDOPP|CDOPT)", "", codigo, flags=re.IGNORECASE)
        if len(sufijo) >= 4:
            dominios_pdl += [
                [["pdl",          "ilike", sufijo]],
                [["name",         "ilike", sufijo]],
                [["display_name", "ilike", sufijo]],
                [["order_number", "ilike", sufijo]],
            ]
        for domain in dominios_pdl:
            try:
                recs = c.search_read("project.dominion", domain, _CAMPOS_DOM, limit=50)
            except Exception:
                continue
            if recs:
                roots = [r for r in recs if not _pid(r.get("parent_id"))]
                return roots[0] if roots else recs[0]
        return None
    recs = c.search_read("project.dominion", [["name", "ilike", codigo]], _CAMPOS_DOM, limit=5)
    return recs[0] if recs else None


def _obtener_familia(c: SGSReadOnlyClient, proyecto: dict) -> tuple[dict, list]:
    actual = proyecto
    vistos = set()
    while True:
        parent_id = _pid(actual.get("parent_id"))
        if not parent_id or actual["id"] in vistos:
            break
        vistos.add(actual["id"])
        padres = c.read("project.dominion", [parent_id], _CAMPOS_DOM)
        if not padres:
            break
        actual = padres[0]
    padre = actual
    hijos = c.search_read("project.dominion", [["parent_id", "=", padre["id"]]], _CAMPOS_DOM, limit=200)
    return padre, hijos


# ── Auditoría precisa de materiales (técnica liquidador2) ─────────────────────

def _search_global(c, model, domain, fields, limit):
    """
    Consulta SGS que FALLA RUIDOSAMENTE (Regla de Oro #4). Un error de red o
    de SGS aquí NO puede degradarse a lista vacía: eso convertiría un nodo con
    material pendiente en un falso "sin_materiales"/"completo".
    También detecta truncado: si el resultado llena el límite, hay registros
    que quedaron fuera y el análisis sería incompleto.
    """
    try:
        recs = c.search_read(model, domain, fields, limit=limit)
    except Exception as e:
        raise RuntimeError(f"Error consultando {model} en SGS: {e}") from e
    if len(recs) >= limit:
        raise RuntimeError(
            f"Consulta {model} truncada (alcanzó el límite de {limit} registros); "
            f"el análisis sería incompleto.")
    return recs


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _estado_materiales_por_nodo(c, node_ids, cache: dict | None = None) -> dict:
    """
    Estado real de materiales por nodo. Si se pasa `cache` ({node_id: estado}),
    los nodos ya calculados se reutilizan y solo se consultan los faltantes —
    el estado es una foto ACTUAL de SGS, así que dentro de una misma ejecución
    (p. ej. el Excel del año que recorre 12 meses) es válido reutilizarlo.
    """
    node_ids = list(node_ids)
    resultado: dict = {}
    if cache:
        for nid in node_ids:
            if nid in cache:
                resultado[nid] = cache[nid]
    faltantes = [nid for nid in node_ids if nid not in resultado]
    if faltantes:
        nuevos = _calcular_estado_materiales(c, faltantes)
        if cache is not None:
            cache.update(nuevos)
        resultado.update(nuevos)
    return resultado


def _calcular_estado_materiales(c, node_ids) -> dict:
    """
    Calcula el estado REAL de materiales por nodo usando la técnica del
    liquidador2 ("Integridad post-liquidación"), que es ultra-precisa:

      * NO usa los campos `despachado`/`devuelto` de project.material
        (poco fiables: incluyen bandejas en borrador, canceladas o proyectadas).
      * Recalcula el movimiento NETO desde las líneas de bandeja reales
        (project.tray.material.deliver_quantity), contando SOLO bandejas
        `approved` y separando devoluciones de despachos atendidos.
      * advance real = Σ project.material.details.amount.
      * pendiente = neto − advance, evaluado por material.

    Retorna {node_id: {
        "n_mat", "neto", "advance", "pendiente",
        "n_con_mov", "n_pend", "estado_mat",
        "fecha_liq", "antiguedad_dias", "lider_mat", "lider_mat_detalle",
        "pendientes": [ {codigo, neto, advance, pendiente,
                         lider, lider_detalle, antiguedad_dias, fecha_despacho} ],
    }}.

    Líder a liquidar (técnica liquidador2 ::verificar_materiales_proyecto):
    sale del partner_id de la bandeja de DESPACHO (project.tray, type≠devolution,
    state=approved). El neto por líder = despacho − devolución de ese líder; el
    dominante (mayor neto) encabeza, con split por porcentaje si hay varios.
    Antigüedad = días desde la bandeja de despacho más antigua del material.
    Un nodo es "pendiente" si CUALQUIER material con movimiento tiene
    pendiente > 0.001; "completo" si todos sus materiales con movimiento
    están liquidados; "sin_materiales" si no hubo movimiento real.
    """
    node_ids = list(node_ids)
    base = {nid: {
        "n_mat": 0, "neto": 0.0, "advance": 0.0, "pendiente": 0.0,
        "n_con_mov": 0, "n_pend": 0, "estado_mat": "sin_materiales",
        "fecha_liq": "", "antiguedad_dias": None, "antiguedad_aprob_dias": None,
        "lider_mat": "", "lider_mat_detalle": [], "pendientes": [],
        "salida_desde": "", "salida_hasta": "",
        "salida_desde_raw": "", "salida_hasta_raw": "",
    } for nid in node_ids}
    if not node_ids:
        return base

    # 1) Materiales de todos los nodos (id → nodo, código, write_date)
    mats = []
    for chunk in _chunks(node_ids, 300):
        mats += _search_global(
            c, "project.material", [["project_id", "in", chunk]],
            ["id", "project_id", "product_code", "product_id", "advance", "write_date"], 50000)
    if not mats:
        return base

    mat_node: dict[int, int] = {}
    mat_info: dict[int, dict] = {}
    pm_ids: list[int] = []
    for m in mats:
        pm_id = m["id"]
        nid   = _pid(m.get("project_id"))
        if nid is None:
            continue
        pm_ids.append(pm_id)
        mat_node[pm_id] = nid
        mat_info[pm_id] = m
        base[nid]["n_mat"] += 1

    # 2) advance real = Σ project.material.details.amount por material.
    #    amount != 0 (no > 0): las correcciones/reversas negativas también
    #    cuentan — excluirlas inflaría el advance y daría falsos "completo".
    advance_by_pm: dict[int, float] = defaultdict(float)
    for chunk in _chunks(pm_ids, 500):
        dets = _search_global(
            c, "project.material.details",
            [["project_material_id", "in", chunk], ["amount", "!=", 0]],
            _CAMPOS_MAT_DET, 200000)
        for d in dets:
            ref   = d.get("project_material_id")
            pm_id = ref[0] if isinstance(ref, (list, tuple)) else ref
            if pm_id:
                advance_by_pm[pm_id] += float(d.get("amount") or 0)

    # 3) Bandejas por nodo (estado / tipo)
    tray_by_id: dict[int, dict] = {}
    for chunk in _chunks(node_ids, 300):
        for t in _search_global(
                c, "project.tray", [["project_id", "in", chunk]],
                _CAMPOS_TRAY_AUDIT, 20000):
            tray_by_id[t["id"]] = t

    # 4) Líneas de bandeja → despacho / devolución por material (y por líder)
    despacho:   dict[int, float] = defaultdict(float)
    devolucion: dict[int, float] = defaultdict(float)
    # Neto por líder a partir de las bandejas: {pm_id: {nombre_lider: qty}}
    desp_lider: dict[int, dict] = defaultdict(lambda: defaultdict(float))
    dev_lider:  dict[int, dict] = defaultdict(lambda: defaultdict(float))
    # Fechas de despacho por material (para la antigüedad)
    desp_fechas: dict[int, list] = defaultdict(list)        # tray.date (lo que dice la bandeja)
    desp_fechas_aprob: dict[int, list] = defaultdict(list)  # tray.write_date (aprobación real almacén)
    # Bandejas por material (justifican las cantidades): {pm_id: {tray_name: {qty, fecha, lider}}}
    ban_desp: dict[int, dict] = defaultdict(dict)
    ban_dev:  dict[int, dict] = defaultdict(dict)
    for chunk in _chunks(node_ids, 300):
        for tm in _search_global(
                c, "project.tray.material", [["project_id", "in", chunk]],
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
            # Solo bandejas aprobadas: descarta borrador, por-aprobar y canceladas
            if state in _AUDIT_EXCL or state not in ("approved", "aprobado"):
                continue
            fecha_ap = tray.get("write_date") or tray.get("create_date")
            if ttype == "devolution":
                devolucion[pm_id] += qty
                if lider:
                    dev_lider[pm_id][lider] += qty
                _acum_bandeja(ban_dev[pm_id], tname, qty, fecha, lider, fecha_ap)
            elif sp in ("atendido", "parcial") or not sp:
                despacho[pm_id] += qty
                if lider:
                    desp_lider[pm_id][lider] += qty
                if fecha:
                    desp_fechas[pm_id].append(str(fecha))
                # Fecha real de aprobación de almacén ≈ write_date (o create_date)
                if fecha_ap:
                    desp_fechas_aprob[pm_id].append(str(fecha_ap))
                _acum_bandeja(ban_desp[pm_id], tname, qty, fecha, lider, fecha_ap)

    # 5) Clasificar por material y agregar por nodo
    for pm_id in pm_ids:
        nid  = mat_node[pm_id]
        neto = round(despacho.get(pm_id, 0.0) - devolucion.get(pm_id, 0.0), 4)
        adv  = round(advance_by_pm.get(pm_id, 0.0), 4)
        pend = round(neto - adv, 4)

        # Sin movimiento real: ni despacho neto ni consumo → no aplica
        if neto <= 0.001 and adv <= 0.001:
            continue

        nodo = base[nid]
        nodo["n_con_mov"] += 1
        nodo["neto"]      += neto
        nodo["advance"]   += adv
        if pend > 0.001:
            nodo["n_pend"]    += 1
            nodo["pendiente"] += pend
            # Líder a liquidar: neto por líder = despacho − devolución de ese líder
            splits: dict[str, float] = defaultdict(float)
            for ln, q in desp_lider.get(pm_id, {}).items():
                splits[ln] += q
            for ln, q in dev_lider.get(pm_id, {}).items():
                splits[ln] -= q
            lider_txt, lider_det = _lider_desde_splits(splits)
            # Antigüedad por fecha de la bandeja (lo que dice el documento)
            fechas = desp_fechas.get(pm_id, [])
            fecha_min = min(fechas) if fechas else ""
            antig = _dias_desde(fecha_min) if fecha_min else None
            # Antigüedad por fecha de aprobación de almacén (fecha real de salida)
            fechas_ap = desp_fechas_aprob.get(pm_id, [])
            fecha_ap_min = min(fechas_ap) if fechas_ap else ""
            antig_ap = _dias_desde(fecha_ap_min) if fecha_ap_min else None
            # La aprobación MÁS RECIENTE es la que decide si este material puede
            # entrar en la liquidación de un mes dado. La mínima sirve para la
            # antigüedad, pero no protege del caso real: un programado dormido
            # meses que recibe una bandeja nueva ayer seguiría pareciendo viejo.
            fecha_ap_max = max(fechas_ap) if fechas_ap else ""
            bdesp = _bandejas_a_lista(ban_desp.get(pm_id, {}))
            bdev  = _bandejas_a_lista(ban_dev.get(pm_id, {}))
            nodo["pendientes"].append({
                "codigo":    str(mat_info[pm_id].get("product_code") or ""),
                "nombre":    _nombre_material(mat_info[pm_id].get("product_id")),
                "neto":      neto, "advance": adv, "pendiente": pend,
                "lider":     lider_txt, "lider_detalle": lider_det,
                "antiguedad_dias": antig,
                "fecha_despacho":  _fmt_fecha(fecha_min) if fecha_min else "",
                "antiguedad_aprob_dias": antig_ap,
                "fecha_aprobacion":      _fmt_fecha(fecha_ap_min) if fecha_ap_min else "",
                # Última salida aprobada: formateada para mostrar y en ISO crudo
                # para que `analisis_mes` la compare contra el fin del mes.
                "fecha_aprob_ultima":     _fmt_fecha(fecha_ap_max) if fecha_ap_max else "",
                "aprob_ultima_raw":       fecha_ap_max,
                "bandejas_despacho":    bdesp,
                "bandejas_devolucion":  bdev,
            })
        wd = mat_info[pm_id].get("write_date")
        if adv > 0 and wd and str(wd) > (nodo.get("_wd_raw") or ""):
            nodo["_wd_raw"]   = str(wd)
            nodo["fecha_liq"] = _fmt_fecha(wd)

    # 6) Estado final por nodo + resumen de líder/antigüedad de lo pendiente
    for nid in node_ids:
        nodo = base[nid]
        nodo.pop("_wd_raw", None)
        if nodo["n_con_mov"] == 0:
            nodo["estado_mat"] = "sin_materiales"
        elif nodo["n_pend"] > 0:
            nodo["estado_mat"] = "pendiente"
        else:
            nodo["estado_mat"] = "completo"
        # Antigüedad del nodo = la del material pendiente más viejo
        antigs = [p["antiguedad_dias"] for p in nodo["pendientes"]
                  if p.get("antiguedad_dias") is not None]
        nodo["antiguedad_dias"] = max(antigs) if antigs else None
        antigs_ap = [p["antiguedad_aprob_dias"] for p in nodo["pendientes"]
                     if p.get("antiguedad_aprob_dias") is not None]
        nodo["antiguedad_aprob_dias"] = max(antigs_ap) if antigs_ap else None
        # Ventana de salida de almacén del nodo: desde la primera aprobación
        # hasta la última. Es lo que permite ver de un vistazo si un nodo mezcla
        # material viejo con material que salió hace días.
        _raws = [p.get("aprob_ultima_raw") for p in nodo["pendientes"]
                 if p.get("aprob_ultima_raw")]
        nodo["salida_desde_raw"] = min(_raws) if _raws else ""
        nodo["salida_hasta_raw"] = max(_raws) if _raws else ""
        nodo["salida_desde"] = _fmt_fecha(nodo["salida_desde_raw"]) if _raws else ""
        nodo["salida_hasta"] = _fmt_fecha(nodo["salida_hasta_raw"]) if _raws else ""
        # Líder dominante del nodo = mayor neto pendiente acumulado entre sus materiales
        agg: dict[str, float] = defaultdict(float)
        for p in nodo["pendientes"]:
            for d in p.get("lider_detalle", []):
                agg[d["nombre"]] += d["neto"]
        nodo["lider_mat"], nodo["lider_mat_detalle"] = _lider_desde_splits(agg)
    return base


# ── Verificación MO (modo manual) ─────────────────────────────────────────────

def _verificar_mo(c: SGSReadOnlyClient, proyectos: list) -> list:
    if not proyectos:
        return []
    pids = [p["id"] for p in proyectos]
    dets = c.search_read(
        "project.activities.details",
        [["project_id", "in", pids], ["advanced_price", "!=", 0]],
        _CAMPOS_ACT_DET, limit=5000,
    )
    acts = c.search_read("project.activities", [["project_id", "in", pids]], _CAMPOS_ACT, limit=5000)
    dets_by_pid: dict[int, list] = {}
    for d in dets:
        p_id = _pid(d.get("project_id"))
        if p_id:
            dets_by_pid.setdefault(p_id, []).append(d)
    acts_by_pid: dict[int, list] = {}
    for a in acts:
        p_id = _pid(a.get("project_id"))
        if p_id:
            acts_by_pid.setdefault(p_id, []).append(a)
    result = []
    for p in proyectos:
        pid = p["id"]
        mis_dets = dets_by_pid.get(pid, [])
        mis_acts = acts_by_pid.get(pid, [])
        mo_value    = sum(float(d.get("advanced_price") or 0) for d in mis_dets)
        act_advance = sum(float(a.get("advance") or 0) for a in mis_acts)
        act_balance = sum(float(a.get("balance") or 0) for a in mis_acts)
        fechas = [d.get("date") for d in mis_dets if d.get("date")]
        fecha_primera = _fmt_fecha(min(str(f) for f in fechas)) if fechas else ""
        fecha_ultima  = _fmt_fecha(max(str(f) for f in fechas)) if fechas else ""
        liquidadores = set()
        for d in mis_dets:
            liq = d.get("partner_id")
            if isinstance(liq, (list, tuple)) and len(liq) > 1:
                liquidadores.add(str(liq[1]).strip())
        result.append({
            "pro_id": pid, "pro_code": p.get("code", ""),
            "nodo": p.get("nodo") or "", "nombre": p.get("name", ""),
            "mo_value": mo_value, "mo_act_advance": act_advance, "mo_act_balance": act_balance,
            "mo_n_avances": len(mis_dets),
            "mo_fecha_primera": fecha_primera, "mo_fecha_ultima": fecha_ultima,
            "mo_liquidadores": sorted(liquidadores),
            "mo_liquidado": mo_value > 0 or act_advance > 0,
            "mo_completo": act_balance <= 0.01 and (mo_value > 0 or act_advance > 0),
            "mo_tiene_acts": len(mis_acts) > 0,
        })
    return result


# ── Verificación Materiales (modo manual) ─────────────────────────────────────

def _verificar_materiales(c: SGSReadOnlyClient, proyectos: list) -> list:
    if not proyectos:
        return []
    pids = [p["id"] for p in proyectos]
    # Estado real de materiales desde bandejas aprobadas (técnica liquidador2)
    estado_por_nodo = _estado_materiales_por_nodo(c, pids)
    trays = c.search_read(
        "project.tray",
        [["project_id", "in", pids], ["state", "=", "approved"]],
        _CAMPOS_TRAY, limit=2000,
    )
    trays_by_pid: dict[int, list] = {}
    for t in trays:
        p_id = _pid(t.get("project_id"))
        if p_id:
            trays_by_pid.setdefault(p_id, []).append(t)
    result = []
    for p in proyectos:
        pid = p["id"]
        est = estado_por_nodo.get(pid, {})
        mis_trays = trays_by_pid.get(pid, [])
        n_mat      = est.get("n_mat", 0)
        despachado = est.get("neto", 0.0)        # movimiento neto real (bandejas aprobadas)
        advance    = est.get("advance", 0.0)     # consumo real (material.details)
        balance    = est.get("pendiente", 0.0)   # pendiente real = neto - advance
        estado_mat = est.get("estado_mat", "sin_materiales")
        tray_dates = [str(t.get("date") or t.get("create_date")) for t in mis_trays if t.get("date") or t.get("create_date")]
        fecha_despacho = _fmt_fecha(max(tray_dates)) if tray_dates else ""
        fecha_liq = est.get("fecha_liq", "")
        tiene = est.get("n_con_mov", 0) > 0
        result.append({
            "pro_id": pid, "pro_code": p.get("code", ""),
            "nodo": p.get("nodo") or "", "nombre": p.get("name", ""),
            "n_mat": n_mat, "mat_despachado": despachado, "mat_devuelto": 0.0,
            "mat_advance": advance, "mat_balance": balance, "mat_n_trays": len(mis_trays),
            "mat_fecha_despacho": fecha_despacho, "mat_fecha_liq": fecha_liq,
            "tiene_materiales": tiene,
            "mat_liquidado": advance > 0,
            "mat_pendiente": estado_mat == "pendiente",
            "mat_completo": estado_mat == "completo",
            "mat_pendientes": est.get("pendientes", []),
        })
    return result


# ── MODO AUTOMÁTICO: análisis por mes ─────────────────────────────────────────

def analisis_mes(año: int = None, mes: int = None,
                 c: "SGSReadOnlyClient" = None,
                 estado_cache: dict | None = None,
                 solo_contrato_id: int | None = None) -> dict:
    """
    Consulta todos los proyectos con MO liquidada en el mes dado,
    filtra por departamento UBATE + equipo de supervisores,
    y clasifica cada familia según estado de materiales:
      'completo'       → materiales liquidados (balance = 0)
      'pendiente'      → hay materiales con balance > 0
      'sin_materiales' → no hay project.material ni despachos

    `c` y `estado_cache` permiten al Excel del año reutilizar la conexión y
    el estado de materiales ya calculado entre meses (misma ejecución).
    `solo_contrato_id` limita a los nodos de esa casilla "Contract" de SGS
    (p. ej. CONTRATO_2026_ID para excluir el contrato viejo ya cerrado).
    """
    hoy = datetime.now()
    if año is None: año = hoy.year
    if mes  is None: mes  = hoy.month

    inicio_str, fin_str = _rango_mes(año, mes)

    if c is None:
        try:
            c = _conectar()
        except Exception as e:
            return {"ok": False, "error": f"No se pudo conectar a SGS: {e}"}

    # ── PASO 1: Avances del mes ────────────────────────────────────────────────
    try:
        avances = _search_global(
            c, "project.activities.details",
            [["date", ">=", inicio_str], ["date", "<", fin_str], ["advanced_price", "!=", 0]],
            _CAMPOS_ACT_DET, 30000,
        )
    except Exception as e:
        return {"ok": False, "error": f"Error al leer avances: {e}"}

    if not avances:
        return {"ok": True, "año": año, "mes": mes, "n_total": 0,
                "n_pendiente": 0, "n_completo": 0, "n_sin_mat": 0, "familias": []}

    # ── PASO 2: Agrupar por proyecto ──────────────────────────────────────────
    mo_by_pid:    dict[int, float] = defaultdict(float)
    dates_by_pid: dict[int, list]  = defaultdict(list)
    lider_by_pid: dict[int, str]   = {}
    liqs_by_pid:  dict[int, dict]  = defaultdict(dict)

    for av in avances:
        pid = _pid(av.get("project_id"))
        if not pid:
            continue
        mo_by_pid[pid]  += float(av.get("advanced_price") or 0)
        if av.get("date"):
            dates_by_pid[pid].append(str(av["date"]))
        if pid not in lider_by_pid:
            p_raw = av.get("partner_id")
            if isinstance(p_raw, (list, tuple)) and len(p_raw) > 1:
                lider_by_pid[pid] = str(p_raw[1]).upper()
        # Acumular liquidadores
        uid_raw = av.get("create_uid")
        if uid_raw and isinstance(uid_raw, (list, tuple)) and len(uid_raw) > 1:
            uid_id = uid_raw[0]
            uid_nombre = " ".join(str(uid_raw[1]).split()).upper()
            if uid_id not in liqs_by_pid[pid]:
                liqs_by_pid[pid][uid_id] = {"nombre": uid_nombre, "mo": 0.0}
            liqs_by_pid[pid][uid_id]["mo"] += float(av.get("advanced_price") or 0)

    pids_todos = list(mo_by_pid.keys())

    # ── PASO 3: Leer project.dominion en batch ─────────────────────────────────
    try:
        dominions = c.search_read(
            "project.dominion", [["id", "in", pids_todos]],
            _CAMPOS_DOM, limit=len(pids_todos) + 10,
        )
    except Exception as e:
        return {"ok": False, "error": f"Error al leer proyectos: {e}"}

    dom_by_pid = {d["id"]: d for d in dominions}

    # ── PASO 4: Filtrar UBATE + supervisores del equipo ────────────────────────
    supervisores = getattr(cfg, "SUPERVISORES_EQUIPO", [])

    # Nodos fuera de los departamentos del equipo pero de una familia del equipo.
    # SGS a veces archiva un nodo suelto en otro departamento (caso real
    # PRO-064594, "NZ7 - Choconta TP" dentro de la familia Ubaté PRO-053113): sin
    # este rescate su MO desaparecía del mes sin aviso ($6,3 M en sept 2026).
    # Se exige además que lo haya liquidado el equipo, para no arrastrar nodos que
    # liquidó otro grupo dentro de familias nuestras (PRO-058688, Línea Viva).
    padres_rescate = {
        _pid(dom_by_pid[pid].get("parent_id"))
        for pid in pids_todos
        if dom_by_pid.get(pid) and not _es_ubate(dom_by_pid[pid])
        and _liquidado_por_equipo(v["nombre"] for v in liqs_by_pid.get(pid, {}).values())
    }
    faltan = [p for p in padres_rescate if p and p not in dom_by_pid]
    if faltan:
        try:
            for p in c.read("project.dominion", faltan, _CAMPOS_DOM):
                dom_by_pid[p["id"]] = p
        except Exception as e:
            return {"ok": False,
                    "error": f"Error al leer padres de nodos fuera de departamento: {e}"}

    pids_filtrados = []
    for pid in pids_todos:
        dom = dom_by_pid.get(pid, {})
        if not dom:
            continue
        if not _es_ubate(dom) and not _nodo_de_familia_del_equipo(
                dom, dom_by_pid, liqs_by_pid.get(pid, {})):
            continue
        # Depto compartido (Telecontrol): solo si lo liquidó el equipo (no Línea Viva)
        if _es_departamento_compartido(dom) and not _liquidado_por_equipo(
                v["nombre"] for v in liqs_by_pid.get(pid, {}).values()):
            continue
        # Filtro de contrato: solo los nodos de la casilla "Contract" pedida
        if solo_contrato_id and _pid(dom.get("contract_id")) != solo_contrato_id:
            continue
        mo_val = abs(mo_by_pid[pid])
        if mo_val < 1.0:
            continue
        if supervisores:
            sup = _resolver_supervisor(dom.get("responsible_id"))
            if sup not in supervisores:
                continue
        pids_filtrados.append(pid)

    if not pids_filtrados:
        return {"ok": True, "año": año, "mes": mes, "n_total": 0,
                "n_pendiente": 0, "n_completo": 0, "n_sin_mat": 0, "familias": [],
                "msg": "Sin proyectos del equipo en este mes."}

    # ── PASO 5: Resolver familias (padre + hijos) en batch ────────────────────
    parent_ids_set: set[int] = set()
    pid_to_parent:  dict[int, int] = {}

    for pid in pids_filtrados:
        dom = dom_by_pid.get(pid, {})
        parent_id = _pid(dom.get("parent_id"))
        if parent_id:
            parent_ids_set.add(parent_id)
            pid_to_parent[pid] = parent_id
        else:
            parent_ids_set.add(pid)
            pid_to_parent[pid] = pid

    # Leer padres que no están ya en dom_by_pid.
    # Errores aquí NO se degradan en silencio: sin el padre/hijos la familia
    # quedaría incompleta y sus materiales pendientes se omitirían (falso OK).
    padres_faltantes = [p for p in parent_ids_set if p not in dom_by_pid]
    if padres_faltantes:
        try:
            padres_extra = c.read("project.dominion", padres_faltantes, _CAMPOS_DOM)
            for p in padres_extra:
                dom_by_pid[p["id"]] = p
        except Exception as e:
            return {"ok": False, "error": f"Error al leer PRO padres: {e}"}

    # Leer hijos de todos los padres en un solo query
    try:
        hijos_all = _search_global(
            c, "project.dominion",
            [["parent_id", "in", list(parent_ids_set)]],
            _CAMPOS_DOM, 10000,
        )
    except Exception as e:
        return {"ok": False, "error": f"Error al leer nodos hijos: {e}"}

    hijos_by_parent: dict[int, list] = defaultdict(list)
    for h in hijos_all:
        pp = _pid(h.get("parent_id"))
        if pp:
            hijos_by_parent[pp].append(h)
            dom_by_pid[h["id"]] = h  # cache

    # ── PASO 6: Estado de materiales por nodo (técnica liquidador2) ───────────
    all_node_ids: set[int] = set(parent_ids_set)
    for h in hijos_all:
        all_node_ids.add(h["id"])

    try:
        estado_por_nodo = _estado_materiales_por_nodo(c, all_node_ids, cache=estado_cache)
    except Exception as e:
        return {"ok": False, "error": f"Error al leer materiales: {e}"}

    # ── PASO 7: Construir resultado por familia ────────────────────────────────
    # Agrupar primero los nodos liquidados por familia: el MO del mes de una
    # familia es la SUMA de todos sus nodos con avances, no solo del primero.
    pids_por_padre: dict[int, list] = defaultdict(list)
    for pid in pids_filtrados:
        pids_por_padre[pid_to_parent[pid]].append(pid)

    familias = []

    for parent_id, pids_fam in pids_por_padre.items():
        # Nodo dominante del mes (mayor |MO|): define PDL/OT y líder de MO
        pid_dom = max(pids_fam, key=lambda x: abs(mo_by_pid.get(x, 0.0)))

        dom    = dom_by_pid.get(pid_dom, {})
        padre  = dom_by_pid.get(parent_id, dom)
        hijos  = hijos_by_parent.get(parent_id, [])
        todos  = [padre] + hijos

        # Clasificación de materiales por nodo (estado real desde bandejas)
        nodos_mat = []
        for nodo in todos:
            nid = nodo["id"]
            est = estado_por_nodo.get(nid, {})
            # Material que salió de almacén DESPUÉS del mes analizado. Liquidarlo
            # dentro de este mes sería un error: el proyecto pudo estar dormido y
            # reactivarse con una bandeja reciente. Se copia cada pendiente en vez
            # de marcarlo en sitio porque `estado_cache` comparte estos dicts
            # entre los meses del Excel del año, y el flag depende del mes.
            _pend = []
            for p in (est.get("pendientes") or []):
                raw = str(p.get("aprob_ultima_raw") or "")
                _pend.append({**p, "posterior_al_mes": bool(raw) and raw >= fin_str})
            _n_post = sum(1 for p in _pend if p["posterior_al_mes"])
            nodos_mat.append({
                "n_pend_posterior": _n_post,
                "salida_desde":     est.get("salida_desde", ""),
                "salida_hasta":     est.get("salida_hasta", ""),
                "pro_id":    nodo.get("id"),
                "pro_code":  nodo.get("code", ""),
                "nodo":      nodo.get("nodo") or "",
                "n_mat":     est.get("n_mat", 0),
                "advance":   est.get("advance", 0.0),
                "balance":   est.get("pendiente", 0.0),
                "despa":     est.get("neto", 0.0),
                "fecha_liq": est.get("fecha_liq", ""),
                "estado_mat": est.get("estado_mat", "sin_materiales"),
                "antiguedad_dias": est.get("antiguedad_dias"),
                "antiguedad_aprob_dias": est.get("antiguedad_aprob_dias"),
                "lider_mat":  est.get("lider_mat", ""),
                "pendientes": _pend,
            })

        con_mat    = [r for r in nodos_mat if r["estado_mat"] != "sin_materiales"]
        pendientes = [r for r in nodos_mat if r["estado_mat"] == "pendiente"]

        if not con_mat:
            clasificacion = "sin_materiales"
        elif pendientes:
            clasificacion = "pendiente"
        else:
            clasificacion = "completo"

        # Datos MO de la familia = agregados de TODOS sus nodos liquidados en el mes
        mo_value  = sum(mo_by_pid.get(x, 0.0) for x in pids_fam)
        fechas_fam: list = []
        for x in pids_fam:
            fechas_fam += dates_by_pid.get(x, [])
        fecha_mo = _max_fecha(fechas_fam)
        # Supervisor = campo 'approve_id' (etiquetado "Supervisor" en SGS) del PRO padre.
        # responsible_id es el "System creator" (el liquidador), no el supervisor de campo.
        sup_raw = (padre.get("approve_id") or dom.get("approve_id")
                   or dom.get("responsible_id"))
        supervisor  = _resolver_supervisor(sup_raw)
        supervisor_full = _nombre_partner(sup_raw) or supervisor
        lider = lider_by_pid.get(pid_dom, "") or next(
            (lider_by_pid[x] for x in pids_fam if lider_by_pid.get(x)), "")
        # Liquidadores fusionados entre los nodos de la familia
        liqs_merged: dict = {}
        for x in pids_fam:
            for uid_id, info in liqs_by_pid.get(x, {}).items():
                if uid_id not in liqs_merged:
                    liqs_merged[uid_id] = {"nombre": info["nombre"], "mo": 0.0}
                liqs_merged[uid_id]["mo"] += info["mo"]
        liqs_sorted = sorted(liqs_merged.values(), key=lambda x: x["mo"], reverse=True)

        # Relación PDL/LCL igual que el módulo central, diferenciando PDL/OT y LCL.
        # Se relaciona el NODO LIQUIDADO (dom), que trae el CDOPP en su campo pdl;
        # el padre se usa como fallback para PDL/OT y para encontrar la LCL.
        display_code, tipo, lcl = _relacionar_pdl(dom, padre)
        # PDL/OT de la familia = unión del historial de TODOS los nodos liquidados en
        # el mes (la casilla PDL de cada nodo acumula; el último es el vigente). Así el
        # PDL con el que se liquidó cualquier nodo aparece en el Excel y se puede buscar.
        if len(pids_fam) > 1:
            union_pdl: list = []
            for x in pids_fam:
                for parte in str(_pdl_ot(dom_by_pid.get(x, {})) or "").replace(":", ";").replace(",", ";").split(";"):
                    s = parte.strip().strip("/").strip()
                    if s and s not in union_pdl:
                        union_pdl.append(s)
            if union_pdl:
                display_code = ";".join(union_pdl)

        # Líder a liquidar (materiales) + antigüedad a nivel de familia:
        # dominante por neto pendiente acumulado entre todos los materiales de la familia.
        agg_fam: dict[str, float] = defaultdict(float)
        antigs_fam = []
        antigs_ap_fam = []
        for r in pendientes:
            for p in r.get("pendientes", []):
                for d in p.get("lider_detalle", []):
                    agg_fam[d["nombre"]] += d["neto"]
                if p.get("antiguedad_dias") is not None:
                    antigs_fam.append(p["antiguedad_dias"])
                if p.get("antiguedad_aprob_dias") is not None:
                    antigs_ap_fam.append(p["antiguedad_aprob_dias"])
        lider_liquidar, lider_liq_detalle = _lider_desde_splits(agg_fam)
        antiguedad_dias = max(antigs_fam) if antigs_fam else None
        antiguedad_aprob_dias = max(antigs_ap_fam) if antigs_ap_fam else None

        # Ventana de salida de almacén de toda la familia + cuánto material
        # quedó fuera del mes. `n_pend_posterior > 0` es la señal de "ojo, aquí
        # hay material que salió después del mes que estás liquidando".
        _sal_desde = [r["salida_desde"] for r in pendientes if r.get("salida_desde")]
        _sal_hasta = [r["salida_hasta"] for r in pendientes if r.get("salida_hasta")]
        n_pend_posterior = sum(r.get("n_pend_posterior", 0) for r in pendientes)
        # Se ordenan por la fecha cruda de cada nodo, no por el texto DD/MM/AAAA.
        _nodos_desde = sorted(
            (r for r in pendientes if r.get("salida_desde")),
            key=lambda r: str(estado_por_nodo.get(r["pro_id"], {}).get("salida_desde_raw", "")))
        _nodos_hasta = sorted(
            (r for r in pendientes if r.get("salida_hasta")),
            key=lambda r: str(estado_por_nodo.get(r["pro_id"], {}).get("salida_hasta_raw", "")))
        salida_desde = _nodos_desde[0]["salida_desde"] if _nodos_desde else ""
        salida_hasta = _nodos_hasta[-1]["salida_hasta"] if _nodos_hasta else ""

        familias.append({
            "padre_id":     padre.get("id"),
            "padre_code":   padre.get("code", ""),
            "padre_name":   padre.get("name", ""),
            "contrato":     _nombre_partner(dom.get("contract_id") or padre.get("contract_id")),
            "pdl_code":     display_code,
            "lcl":          lcl,
            "supervisor":   supervisor,
            "supervisor_full": supervisor_full,
            "lider":        lider,
            "mo_value":     mo_value,
            "fecha_mo":     fecha_mo,
            "liquidadores": liqs_sorted,
            "clasificacion": clasificacion,
            "n_pendientes": len(pendientes),
            "n_con_mat":    len(con_mat),
            "n_nodos":      len(todos),
            "nodos_mat":    nodos_mat,
            "tipo":         tipo,
            "lider_liquidar":     lider_liquidar,
            "lider_liq_detalle":  lider_liq_detalle,
            "antiguedad_dias":    antiguedad_dias,
            "antiguedad_aprob_dias": antiguedad_aprob_dias,
            "salida_desde":       salida_desde,
            "salida_hasta":       salida_hasta,
            "n_pend_posterior":   n_pend_posterior,
        })

    # Ordenar: pendientes primero, luego completos, luego sin materiales
    _orden = {"pendiente": 0, "completo": 1, "sin_materiales": 2}
    familias.sort(key=lambda f: (_orden.get(f["clasificacion"], 3), f.get("pdl_code", "")))

    return {
        "ok":               True,
        "año":              año,
        "mes":              mes,
        "mes_str":          f"{MESES_ES.get(mes, str(mes))} {año}",
        "n_total":          len(familias),
        "n_pendiente":      len([f for f in familias if f["clasificacion"] == "pendiente"]),
        "n_completo":       len([f for f in familias if f["clasificacion"] == "completo"]),
        "n_sin_mat":        len([f for f in familias if f["clasificacion"] == "sin_materiales"]),
        "n_programado":     len([f for f in familias if f["tipo"] == "programado"]),
        "n_mayor_impacto":  len([f for f in familias if f["tipo"] == "mayor_impacto"]),
        # Límite exclusivo del mes (primer día del siguiente) y cuántas familias
        # traen material salido después de él — lo que no debe entrar a liquidar.
        "fin_mes":          fin_str,
        "n_con_posterior":  len([f for f in familias if f.get("n_pend_posterior")]),
        "familias":         familias,
    }


# ── CRUCE EXCEL ───────────────────────────────────────────────────────────────

def _extraer_codigos_excel(ruta: str) -> list[dict]:
    """
    Lee el REPORTE AVANCE del Excel de programación Zona VII y extrae
    todos los registros de UBATE con sus códigos únicos + metadata.
    Devuelve lista de dicts: {codigo, supervisor, fecha, tipo_trabajo, municipio}
    """
    import openpyxl

    _RE_CDOPP = re.compile(r"^(CDOPP|CDOPPI|CDOPT)", re.IGNORECASE)
    _RE_DF    = re.compile(r"^DF\d{8,}", re.IGNORECASE)
    _RE_NUM   = re.compile(r"^\d{8,13}$")

    def _split_codigos(raw: str) -> list[str]:
        raw = (raw or "").replace("\xa0", "").replace(" ", "")
        codigos = []
        for parte in re.split(r"[+,;/]", raw):
            p = parte.strip().upper()
            # limpiar sufijos tipo -TP2026...
            p = re.split(r"-TP\d", p)[0]
            if (_RE_CDOPP.match(p) or _RE_DF.match(p)) and len(p) > 5:
                codigos.append(p)
            elif _RE_NUM.match(p):
                codigos.append(p)
        return codigos

    wb = openpyxl.load_workbook(ruta, data_only=True, read_only=True)
    ws = wb["REPORTE AVANCE"]
    registros = []
    vacias = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if all(v is None for v in (row[:12] if len(row) >= 12 else row)):
            vacias += 1
            if vacias > 15:
                break
            continue
        vacias = 0
        zona = row[1] if len(row) > 1 else None
        if not zona or "UBATE" not in str(zona).upper():
            continue
        fecha     = row[0]
        sup_raw   = str(row[4] or "").strip()
        tipo      = str(row[7] or "").strip()
        num_raw   = str(row[8] or "").strip()
        municipio = str(row[14] or "").strip() if len(row) > 14 else ""
        codigos   = _split_codigos(num_raw)
        for cod in codigos:
            registros.append({
                "codigo":    cod,
                "supervisor": sup_raw,
                "fecha":     fecha.strftime("%d/%m/%Y") if hasattr(fecha, "strftime") else str(fecha or ""),
                "tipo":      tipo,
                "municipio": municipio,
            })
    wb.close()
    # Deduplicar: un código puede aparecer en múltiples fechas; quedarse con la más reciente
    seen: dict[str, dict] = {}
    for r in registros:
        c = r["codigo"]
        if c not in seen:
            seen[c] = r
        # sobreescribir solo si la fecha es más reciente (formato dd/mm/yyyy)
        else:
            try:
                d_new = datetime.strptime(r["fecha"], "%d/%m/%Y")
                d_old = datetime.strptime(seen[c]["fecha"], "%d/%m/%Y")
                if d_new > d_old:
                    seen[c] = r
            except Exception:
                pass
    return list(seen.values())


def _batch_search(c, codigos: list) -> dict:
    """
    Busca múltiples códigos en batch por tipo.
    Retorna {codigo: record} para los encontrados.
    ~3-6 llamadas Odoo en lugar de 236×8.
    """
    _RE_DF_  = re.compile(r"^DF\d{8,}", re.IGNORECASE)
    _RE_PDL_ = re.compile(r"^(CDOPP|CDOPPI|CDOPT|PRO-)", re.IGNORECASE)

    df_codes  = [x for x in codigos if _RE_DF_.match(x)]
    pdl_codes = [x for x in codigos if _RE_PDL_.match(x)]
    num_codes = [x for x in codigos if x and x[0].isdigit()]

    resultado: dict = {}

    def _batch(codes, domains_fn, match_fn, chunk=80):
        for i in range(0, len(codes), chunk):
            bloque = codes[i:i + chunk]
            bloque_set = set(bloque)
            pendientes = set(bloque)  # códigos aún no encontrados en este bloque
            for domain in domains_fn(bloque):
                if not pendientes:
                    break  # ya encontramos todo
                try:
                    recs = c.search_read("project.dominion", domain, _CAMPOS_DOM,
                                         limit=len(bloque) + 20)
                    for rec in recs:
                        key = match_fn(rec, bloque_set)
                        if key and key not in resultado:
                            resultado[key] = rec
                            pendientes.discard(key)
                except Exception:
                    continue
            # sin break: todos los dominios se prueban hasta agotar pendientes

    # DF: code, order_number, pdl (los DFs pueden estar en cualquiera de los 3)
    if df_codes:
        _batch(
            df_codes,
            domains_fn=lambda b: [
                [["code",         "in", b]],
                [["order_number", "in", b]],
                [["pdl",          "in", b]],
            ],
            match_fn=lambda rec, s: next(
                (v for v in [rec.get("code"), rec.get("order_number"), rec.get("pdl")]
                 if v and v in s), None),
        )

    # CDOPP/CDOPT: campo 'pdl' primero, luego 'code'
    if pdl_codes:
        _batch(
            pdl_codes,
            domains_fn=lambda b: [[["pdl", "in", b]], [["code", "in", b]]],
            match_fn=lambda rec, s: (rec.get("pdl") if rec.get("pdl") in s
                                     else rec.get("code") if rec.get("code") in s
                                     else None),
        )

    # Numéricos: pdl u order_number
    if num_codes:
        _batch(
            num_codes,
            domains_fn=lambda b: [[["pdl", "in", b]], [["order_number", "in", b]]],
            match_fn=lambda rec, s: next(
                (v for v in [rec.get("pdl"), rec.get("order_number")] if v in s), None),
        )

    # Fallback individual para los no encontrados (CDOPP que necesitan ilike)
    not_found = [cod for cod in codigos if cod not in resultado]
    for cod in not_found:
        try:
            rec = _buscar_proyecto(c, cod)
            if rec:
                resultado[cod] = rec
        except Exception:
            pass

    return resultado


def _batch_get_familias(c, recs: list) -> tuple:
    """
    Dado records encontrados (posiblemente hijos), sube hasta la raíz en batch
    y retorna ({padre_id: (padre_rec, [hijos])}, {rec_id: padre_id}).
    """
    known: dict = {r["id"]: r for r in recs}

    # Traversal batch hacia arriba (máx 5 niveles)
    to_fetch = list({_pid(r.get("parent_id")) for r in recs
                     if _pid(r.get("parent_id")) and _pid(r.get("parent_id")) not in known})
    for _ in range(5):
        if not to_fetch:
            break
        try:
            parents = c.read("project.dominion", to_fetch, _CAMPOS_DOM)
        except Exception:
            break
        for p in parents:
            known[p["id"]] = p
        to_fetch = list({_pid(p.get("parent_id")) for p in parents
                         if _pid(p.get("parent_id")) and _pid(p.get("parent_id")) not in known})

    def _find_root_id(start_id):
        current = start_id
        visited = set()
        while True:
            rec = known.get(current)
            if not rec:
                return current
            pid = _pid(rec.get("parent_id"))
            if not pid or current in visited:
                return current
            visited.add(current)
            current = pid

    # Raíces de cada record original
    rec_id_to_padre_id: dict = {}
    roots: dict = {}
    for rec in recs:
        root_id = _find_root_id(rec["id"])
        rec_id_to_padre_id[rec["id"]] = root_id
        if root_id in known:
            roots[root_id] = known[root_id]

    # Batch: hijos de todas las raíces en una sola llamada
    all_root_ids = list(roots.keys())
    all_children: list = []
    for i in range(0, len(all_root_ids), 100):
        chunk = all_root_ids[i:i + 100]
        try:
            children = c.search_read("project.dominion",
                                     [["parent_id", "in", chunk]],
                                     _CAMPOS_DOM, limit=1000)
            all_children.extend(children)
        except Exception:
            pass

    children_by_root: dict = {rid: [] for rid in all_root_ids}
    for child in all_children:
        pid = _pid(child.get("parent_id"))
        if pid in children_by_root:
            children_by_root[pid].append(child)

    padre_id_to_familia = {rid: (roots[rid], children_by_root[rid]) for rid in all_root_ids}
    return padre_id_to_familia, rec_id_to_padre_id


def cruce_excel(ruta: str) -> dict:
    """
    Lee el Excel de programación, extrae trabajos UBATE y los cruza contra SGS.
    Versión batch: ~30 llamadas Odoo en lugar de ~1000.
    """
    try:
        registros = _extraer_codigos_excel(ruta)
    except Exception as e:
        return {"ok": False, "error": f"No se pudo leer el Excel: {e}"}

    if not registros:
        return {"ok": True, "n_total": 0, "n_cargado": 0, "n_pendiente": 0,
                "n_no_encontrado": 0, "filas": [],
                "msg": "No se encontraron trabajos de UBATE en el archivo."}

    try:
        c = _conectar()
    except Exception as e:
        return {"ok": False, "error": f"No se pudo conectar a SGS: {e}"}

    # ── 1. Batch search ────────────────────────────────────────────────────────
    all_codigos = [r["codigo"] for r in registros]
    try:
        codigo_to_rec = _batch_search(c, all_codigos)
    except Exception as e:
        return {"ok": False, "error": f"Error en búsqueda batch: {e}"}

    # ── 2. Batch family traversal ──────────────────────────────────────────────
    found_recs = list(codigo_to_rec.values())
    if found_recs:
        try:
            padre_id_to_familia, rec_id_to_padre_id = _batch_get_familias(c, found_recs)
        except Exception as e:
            return {"ok": False, "error": f"Error en familia batch: {e}"}
    else:
        padre_id_to_familia, rec_id_to_padre_id = {}, {}

    codigo_to_padre_id: dict = {}
    for cod, rec in codigo_to_rec.items():
        pid = rec_id_to_padre_id.get(rec["id"])
        if pid is not None:
            codigo_to_padre_id[cod] = pid

    # ── 3. Colectar todos los node IDs ─────────────────────────────────────────
    all_node_ids: list = []
    node_to_padre: dict = {}
    for padre_id, (padre, hijos) in padre_id_to_familia.items():
        all_node_ids.append(padre["id"])
        node_to_padre[padre["id"]] = padre_id
        for h in hijos:
            all_node_ids.append(h["id"])
            node_to_padre[h["id"]] = padre_id

    # ── 4. Batch MO + Materiales (4 llamadas para todos los nodos) ─────────────
    if all_node_ids:
        try:
            all_dets = _search_global(c, "project.activities.details",
                                      [["project_id", "in", all_node_ids],
                                       ["advanced_price", "!=", 0]],
                                      _CAMPOS_ACT_DET, 50000)
            all_acts = _search_global(c, "project.activities",
                                      [["project_id", "in", all_node_ids]],
                                      _CAMPOS_ACT, 50000)
            # Estado de materiales por nodo desde bandejas aprobadas (técnica liquidador2)
            estado_por_nodo = _estado_materiales_por_nodo(c, all_node_ids)
        except Exception as e:
            return {"ok": False, "error": f"Error al leer MO/materiales: {e}"}
    else:
        all_dets = all_acts = []
        estado_por_nodo = {}

    # ── 5. Agrupar MO por padre_id ─────────────────────────────────────────────
    dets_by_p:  dict = {pid: [] for pid in padre_id_to_familia}
    acts_by_p:  dict = {pid: [] for pid in padre_id_to_familia}

    for d in all_dets:
        key = node_to_padre.get(_pid(d.get("project_id")))
        if key is not None:
            dets_by_p[key].append(d)
    for a in all_acts:
        key = node_to_padre.get(_pid(a.get("project_id")))
        if key is not None:
            acts_by_p[key].append(a)

    # ── 6. Calcular estado por familia ─────────────────────────────────────────
    cache: dict = {}
    for padre_id, (padre, hijos) in padre_id_to_familia.items():
        mis_dets  = dets_by_p[padre_id]
        mis_acts  = acts_by_p[padre_id]

        mo_value   = sum(float(d.get("advanced_price") or 0) for d in mis_dets)
        mo_balance = sum(float(a.get("balance") or 0) for a in mis_acts)
        fechas_mo  = [str(d["date"]) for d in mis_dets if d.get("date")]
        fecha_mo   = _fmt_fecha(max(fechas_mo)) if fechas_mo else ""

        # Agregar estado de materiales de todos los nodos de la familia
        nodos_familia = [padre] + list(hijos)
        mat_advance = mat_neto = mat_pendiente_real = 0.0
        n_con_mov = 0
        fechas_liq = []
        for nodo in nodos_familia:
            est = estado_por_nodo.get(nodo["id"], {})
            mat_advance         += est.get("advance", 0.0)
            mat_neto            += est.get("neto", 0.0)
            mat_pendiente_real  += est.get("pendiente", 0.0)
            n_con_mov           += est.get("n_con_mov", 0)
            if est.get("fecha_liq"):
                fechas_liq.append(est["fecha_liq"])
        tiene_mat = n_con_mov > 0
        fecha_mat = max(fechas_liq) if fechas_liq else ""

        if mo_value > 0 and mat_pendiente_real <= 0.01:
            estado = "completo"
        elif mo_value > 0:
            estado = "pendiente_mat"
        elif mo_balance > 0 or mis_acts:
            estado = "pendiente_mo"
        else:
            estado = "sin_mo"

        if mat_pendiente_real > 0.01:
            mat_estado = f"Pendiente ({mat_pendiente_real:.0f} uds)"
        elif mat_advance > 0 or mat_neto > 0:
            mat_estado = "Completo"
        elif tiene_mat:
            mat_estado = "Despachado"
        else:
            mat_estado = "Sin materiales"

        cache[padre_id] = {
            "padre_code": padre.get("code", ""),
            "padre_id":   padre_id,
            "mo_value":   mo_value,
            "fecha_mo":   fecha_mo,
            "mat_estado": mat_estado,
            "fecha_mat":  fecha_mat,
            "estado":     estado,
            "detalle":    "",
        }

    # ── 7. Construir filas ─────────────────────────────────────────────────────
    filas = []
    _vacio = {"padre_code": "", "padre_id": None,
              "mo_value": 0, "fecha_mo": "", "mat_estado": "", "fecha_mat": ""}
    for reg in registros:
        cod = reg["codigo"]
        if cod not in codigo_to_rec:
            filas.append({**reg, **_vacio, "estado": "no_encontrado",
                          "detalle": "No está en SGS"})
            continue
        padre_id = codigo_to_padre_id.get(cod)
        if padre_id is None or padre_id not in cache:
            filas.append({**reg, **_vacio, "estado": "error",
                          "detalle": "No se determinó la familia"})
            continue
        filas.append({**reg, **cache[padre_id]})

    n_cargado       = sum(1 for f in filas if f["estado"] == "completo")
    n_pend_mat      = sum(1 for f in filas if f["estado"] == "pendiente_mat")
    n_pend_mo       = sum(1 for f in filas if f["estado"] in ("pendiente_mo", "sin_mo"))
    n_no_encontrado = sum(1 for f in filas if f["estado"] == "no_encontrado")
    n_error         = sum(1 for f in filas if f["estado"] == "error")

    _ord = {"no_encontrado": 0, "sin_mo": 1, "pendiente_mo": 2,
            "pendiente_mat": 3, "completo": 4, "error": 5}
    filas.sort(key=lambda r: (_ord.get(r["estado"], 9), r.get("codigo", "")))

    return {
        "ok":               True,
        "n_total":          len(filas),
        "n_cargado":        n_cargado,
        "n_pendiente_mat":  n_pend_mat,
        "n_pendiente_mo":   n_pend_mo,
        "n_no_encontrado":  n_no_encontrado,
        "n_error":          n_error,
        "filas":            filas,
    }


# ── MODO MANUAL: verificar por código ─────────────────────────────────────────

def verificar_proyectos(codigos_raw: str) -> dict:
    codigos = []
    for linea in codigos_raw.splitlines():
        for parte in linea.split(","):
            c = parte.strip().upper()
            if c:
                codigos.append(c)
    if not codigos:
        return {"ok": False, "error": "No se ingresaron códigos de proyecto."}
    try:
        c = _conectar()
    except Exception as e:
        return {"ok": False, "error": f"No se pudo conectar a SGS: {e}"}

    familias       = []
    procesados     = set()
    errores_global = []

    for codigo in codigos:
        try:
            proyecto = _buscar_proyecto(c, codigo)
        except Exception as e:
            errores_global.append({"codigo": codigo, "error": str(e)}); continue
        if not proyecto:
            errores_global.append({"codigo": codigo, "error": f"No se encontró '{codigo}' en SGS."}); continue
        try:
            padre, hijos = _obtener_familia(c, proyecto)
        except Exception as e:
            errores_global.append({"codigo": codigo, "error": f"Error al resolver familia: {e}"}); continue

        padre_id = padre["id"]
        if padre_id in procesados:
            continue
        procesados.add(padre_id)
        todos = [padre] + hijos
        try:
            mo_data  = _verificar_mo(c, todos)
            mat_data = _verificar_materiales(c, todos)
        except Exception as e:
            errores_global.append({"codigo": codigo, "error": f"Error al verificar: {e}"}); continue

        total_mo_value    = sum(r["mo_value"]       for r in mo_data)
        total_mo_balance  = sum(r["mo_act_balance"]  for r in mo_data)
        total_mat_advance = sum(r["mat_advance"]     for r in mat_data)
        total_mat_balance = sum(r["mat_balance"]     for r in mat_data)
        nodos_sin_mo   = [r["pro_code"] for r in mo_data  if not r["mo_liquidado"] and r["mo_tiene_acts"]]
        nodos_mat_pend = [r["pro_code"] for r in mat_data if r["mat_pendiente"]]
        nodos_con_mat  = [r["pro_code"] for r in mat_data if r["tiene_materiales"]]

        fechas_mo = [r["mo_fecha_ultima"] for r in mo_data if r["mo_fecha_ultima"]]
        fecha_mo_global = _fmt_fecha(
            max(datetime.strptime(f, "%d/%m/%Y").strftime("%Y-%m-%d") for f in fechas_mo)
        ) if fechas_mo else ""

        familias.append({
            "codigo_input": codigo,
            "padre": {"id": padre["id"], "code": padre.get("code", ""),
                      "name": padre.get("name", ""), "nodo": padre.get("nodo") or "",
                      "status": padre.get("status", ""), "pdl": padre.get("pdl", "") or ""},
            "n_hijos": len(hijos),
            "hijos": [{"id": h["id"], "code": h.get("code", ""),
                       "name": h.get("name", ""), "nodo": h.get("nodo") or "",
                       "pdl": h.get("pdl", "") or ""} for h in hijos],
            "mo": mo_data, "materiales": mat_data,
            "resumen": {
                "total_mo_value": total_mo_value, "total_mo_balance": total_mo_balance,
                "mo_liquidado": total_mo_value > 0,
                "fecha_mo_global": fecha_mo_global,
                "total_mat_advance": total_mat_advance, "total_mat_balance": total_mat_balance,
                "mat_liquidado": total_mat_advance > 0,
                "nodos_sin_mo": nodos_sin_mo, "nodos_mat_pend": nodos_mat_pend,
                "nodos_con_mat": nodos_con_mat,
            },
        })

    return {"ok": True, "n_familias": len(familias), "familias": familias, "errores": errores_global}
