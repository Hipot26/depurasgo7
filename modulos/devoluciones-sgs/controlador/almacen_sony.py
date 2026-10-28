"""
Módulo Materiales Sony — copia para el Módulo Almacén (devoluciones_sgs.py)
=====================================================================
Origen: SGS + SONY/nuevo/materiales_sony.py (2026-09-16). Cambios: rutas de datos
propias, sesiones persistentes en disco, y las hojas fotografiadas llegan por
parámetro (misma app) en vez de por HTTP.

Genera el "cargue de instalaciones" (formato PLANTILLA de SSSSONY.xlsx) a partir
de las bandejas SGS de un proyecto, marcando la columna Aportacion (K) según el
ANEXO Nº 14 — LISTADO MATERIAL MENOR que el usuario sube periódicamente.

Flujo:
  1. Usuario sube/actualiza el ANEXO 14 (persistente en nuevo/data/materiales_sony/).
  2. Usuario ingresa cualquier PRO (hijo o padre) → se resuelve el padre y sus nodos.
  3. Selecciona nodos → selecciona bandejas (despachos y devoluciones, vista tipo SGS).
  4. Página de cuadre: devoluciones SGS auto-asignadas a los materiales de cada
     despacho + devoluciones manuales editables + toggle Aportación por fila.
  5. Excel final con el formato exacto de la plantilla (Hoja1, columnas A–L).
"""
import io
import json
import os
import pickle
import re
import time
import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime

import openpyxl

from almacen_sgs_client import parsear_producto, nombre_campo

_HERE     = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_HERE, "almacen_data")

_SESSION_DIR = os.path.join(_HERE, "almacen_data", "sesiones")
os.makedirs(_SESSION_DIR, exist_ok=True)
os.makedirs(_DATA_DIR, exist_ok=True)

# ── Plantilla (formato exacto de SSSSONY.xlsx / cargue_instalaciones) ───────────

PLANTILLA_HEADERS = [
    "Id_nodo", "Usuario", "Fecha_asignacion", "Codigo", "Cantidad_entregada",
    "Devolucion", "Fecha_entrega", "Pdi", "Consecutivo", "Observaciones",
    "Aportacion", "Fecha_reintegro",
]
FMT_FECHA_HORA = "m/d/yy h:mm"   # columna C (como el cargue de ejemplo)
FMT_FECHA      = "d-mmm"         # columnas G y L


# ══════════════════════════════════════════════════════════════════════════════
# ANEXO Nº 14 — almacenamiento persistente + matcher de aportaciones
# ══════════════════════════════════════════════════════════════════════════════

def _anexo_json_path():
    return os.path.join(_DATA_DIR, "anexo14.json")


def _anexo_xlsx_path():
    return os.path.join(_DATA_DIR, "anexo14.xlsx")


def _normalizar_texto(s: str) -> str:
    """Mayúsculas, sin acentos, solo alfanumérico — para match por descripción."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", "", s.upper())


def _limpiar_codigo(v) -> str:
    """Convierte celda a código string: 274415.0 → '274415', None/'NA' → ''."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    if s.upper() in ("NA", "N/A", "#N/A", "-", ""):
        return ""
    return s


def parsear_anexo14(xlsx_bytes: bytes) -> list:
    """
    Lee el ANEXO Nº 14 y retorna [{item, sap, e4e, familia, material, desc, et}].
    Localiza el header por la celda que contenga 'E4E' (tolerante a variaciones).
    """
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    for ws in wb.worksheets:
        # Buscar fila de headers en las primeras 15 filas
        hdr_row, cols = None, {}
        for r in range(1, min(16, ws.max_row + 1)):
            fila = {c: ws.cell(row=r, column=c).value for c in range(1, min(25, ws.max_column + 1))}
            norm = {c: _normalizar_texto(v) for c, v in fila.items() if v}
            col_e4e = next((c for c, v in norm.items() if "E4E" in v), None)
            if col_e4e:
                hdr_row = r
                cols["e4e"]      = col_e4e
                cols["sap"]      = next((c for c, v in norm.items() if "SAP" in v), None)
                cols["item"]     = next((c for c, v in norm.items() if v in ("ITEM", "TEM")), None)
                cols["familia"]  = next((c for c, v in norm.items() if "FAMILIA" in v), None)
                cols["material"] = next((c for c, v in norm.items() if v == "MATERIAL"), None)
                cols["desc"]     = next((c for c, v in norm.items() if "DESCRIPCION" in v), None)
                cols["et"]       = next((c for c, v in norm.items() if v == "ET"), None)
                break
        if hdr_row is None:
            continue

        materiales, vacias = [], 0
        for r in range(hdr_row + 1, ws.max_row + 1):
            def _get(key):
                c = cols.get(key)
                return ws.cell(row=r, column=c).value if c else None

            e4e  = _limpiar_codigo(_get("e4e"))
            sap  = _limpiar_codigo(_get("sap"))
            desc = str(_get("desc") or "").strip()
            if not e4e and not sap and not desc:
                vacias += 1
                if vacias > 5:
                    break
                continue
            vacias = 0
            materiales.append({
                "item":     _limpiar_codigo(_get("item")),
                "sap":      sap,
                "e4e":      e4e,
                "familia":  str(_get("familia") or "").strip(),
                "material": str(_get("material") or "").strip(),
                "desc":     desc,
                "et":       str(_get("et") or "").strip(),
            })
        if materiales:
            return materiales
    raise ValueError("No se encontró el listado de materiales en el archivo "
                     "(se esperaba una columna 'Código E4E').")


def guardar_anexo(xlsx_bytes: bytes, filename: str) -> dict:
    """Parsea y guarda el ANEXO 14. Retorna el status resultante."""
    materiales = parsear_anexo14(xlsx_bytes)
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_anexo_xlsx_path(), "wb") as f:
        f.write(xlsx_bytes)
    meta = {
        "filename":  filename,
        "subido":    datetime.now().strftime("%d/%m/%Y %H:%M"),
        "materiales": materiales,
    }
    with open(_anexo_json_path(), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return anexo_status()


def eliminar_anexo():
    for p in (_anexo_json_path(), _anexo_xlsx_path()):
        if os.path.isfile(p):
            os.remove(p)


def _cargar_anexo() -> dict | None:
    p = _anexo_json_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def anexo_status() -> dict:
    meta = _cargar_anexo()
    if not meta:
        return {"cargado": False}
    mats = meta.get("materiales", [])
    return {
        "cargado":   True,
        "filename":  meta.get("filename", "anexo14.xlsx"),
        "subido":    meta.get("subido", "—"),
        "total":     len(mats),
        "sin_e4e":   sum(1 for m in mats if not m["e4e"]),
    }


class AportacionMatcher:
    """Decide si un material es aportación (1) o Enel (0) según el ANEXO 14."""

    def __init__(self):
        meta = _cargar_anexo()
        self.cargado = meta is not None
        self._e4e   = set()
        self._descs = {}
        for m in (meta or {}).get("materiales", []):
            if m["e4e"]:
                self._e4e.add(m["e4e"])
            dn = _normalizar_texto(m["desc"])
            if dn:
                self._descs[dn] = m["e4e"] or m["sap"]

    def es_aportacion(self, codigo_e4e: str, descripcion: str = "") -> tuple:
        """
        Retorna (k, via):
          k   : 1 aportación / 0 Enel
          via : 'e4e' | 'desc' | '' (sin match → 0)
        """
        if codigo_e4e and str(codigo_e4e).strip() in self._e4e:
            return 1, "e4e"
        dn = _normalizar_texto(descripcion)
        if dn and dn in self._descs:
            return 1, "desc"
        return 0, ""


# ══════════════════════════════════════════════════════════════════════════════
# Sesiones (pickle en tempdir, mismo patrón que _PREVIEW_DIR de app.py)
# ══════════════════════════════════════════════════════════════════════════════

def nueva_sesion_mat(data: dict) -> str:
    sid = uuid.uuid4().hex[:12]
    save_sesion_mat(sid, data)
    return sid


def save_sesion_mat(sid: str, data: dict):
    data["_saved_at"] = time.time()
    with open(os.path.join(_SESSION_DIR, f"{sid}.pkl"), "wb") as f:
        pickle.dump(data, f)


def get_sesion_mat(sid: str):
    p = os.path.join(_SESSION_DIR, f"{sid}.pkl")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Presentación de bandejas (vista tipo SGS)
# ══════════════════════════════════════════════════════════════════════════════

_ESTADOS_TRAY = {
    "approved": ("Aprobado",  "#C6EFCE", "#375623"),
    "aprobado": ("Aprobado",  "#C6EFCE", "#375623"),
    "done":     ("Hecho",     "#C6EFCE", "#375623"),
    "draft":    ("Borrador",  "#EDEDED", "#555555"),
    "cancel":   ("Cancelado", "#FFC7CE", "#9C0006"),
    "cancelled":("Cancelado", "#FFC7CE", "#9C0006"),
}

# Valores reales verificados en SGS: 'parcial' | 'atendido' | 'noatendido'
_ESTADOS_PICKING = {
    "atendido":            ("Atendido",    "#C6EFCE", "#375623"),
    "done":                ("Atendido",    "#C6EFCE", "#375623"),
    "attended":            ("Atendido",    "#C6EFCE", "#375623"),
    "parcial":             ("At. parcial", "#BDD7EE", "#1F4E79"),
    "partial":             ("At. parcial", "#BDD7EE", "#1F4E79"),
    "partially_available": ("At. parcial", "#BDD7EE", "#1F4E79"),
    "partially_attended":  ("At. parcial", "#BDD7EE", "#1F4E79"),
    "noatendido":          ("No atendido", "#FFC7CE", "#9C0006"),
    "no_atendido":         ("No atendido", "#FFC7CE", "#9C0006"),
    "not_attended":        ("No atendido", "#FFC7CE", "#9C0006"),
    "confirmed":           ("No atendido", "#FFC7CE", "#9C0006"),
    "waiting":             ("No atendido", "#FFC7CE", "#9C0006"),
    "assigned":            ("Preparado",   "#BDD7EE", "#1F4E79"),
    "draft":               ("No atendido", "#FFC7CE", "#9C0006"),
    "cancel":              ("Cancelado",   "#EDEDED", "#555555"),
}


def _badge(mapa: dict, valor) -> dict:
    v = str(valor or "").strip()
    if not v or v == "False":
        return {"txt": "—", "bg": "", "fg": "#888"}
    key = v.lower()
    if key in mapa:
        txt, bg, fg = mapa[key]
    elif "partial" in key or "parcial" in key:
        txt, bg, fg = "At. parcial", "#BDD7EE", "#1F4E79"
    else:
        txt, bg, fg = v, "#EDEDED", "#555555"
    return {"txt": txt, "bg": bg, "fg": fg}


def presentar_bandeja(ban: dict, nodo: dict) -> dict:
    """Convierte una bandeja SGS cruda en el dict que renderiza la vista."""
    es_dev = ban.get("type", "deliver") == "devolution"
    n_mats = len(ban.get("materiales", []))
    qty    = sum(float(m.get("deliver_quantity", 0) or 0) for m in ban.get("materiales", []))
    return {
        "id":        ban["id"],
        "name":      ban.get("name", ""),
        "tipo":      "Devolución" if es_dev else "Despacho",
        "es_dev":    es_dev,
        "equipo":    nombre_campo(ban.get("partner_id")) or "—",
        "gestor":    nombre_campo(ban.get("responsible_id")) or "—",
        "fecha":     str(ban.get("date") or "")[:10],
        "picking":   nombre_campo(ban.get("picking_id")) or "—",
        "proyecto":  nodo.get("name", ""),
        "estado":    _badge(_ESTADOS_TRAY, ban.get("state")),
        "entrega":   _badge(_ESTADOS_PICKING, ban.get("state_picking")),
        "n_mats":    n_mats,
        "qty_total": qty,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Cuadre: despachos + devoluciones SGS auto-asignadas + aportación
# ══════════════════════════════════════════════════════════════════════════════

def _codigo_material(mat: dict) -> tuple:
    """Retorna (codigo E4E preferido, dom, desc) del material de bandeja."""
    dom, alt, desc = parsear_producto(mat.get("product_id"))
    return (alt or dom), dom, desc


def construir_cuadre(nodos: list, ban_ids_sel: set) -> dict:
    """
    Arma la estructura del cuadre para los nodos con bandejas seleccionadas.

    Retorna:
      nodos_cuadre : [{pro, nodo_num, name, id_nodo_sug, despachos: [...],
                       devoluciones_info: [...], }]
        cada despacho: {ban_id, ban_name, fecha, picking, equipo, filas: [...]}
        cada fila: {idx, cod, dom, desc, qty, dev_auto, k, k_via, es_cliente}
      advertencias : [str]
    idx es global y correlativo — es la clave de los inputs del formulario.
    """
    matcher = AportacionMatcher()
    advertencias = []
    if not matcher.cargado:
        advertencias.append("No hay ANEXO Nº 14 cargado — toda la columna Aportación "
                            "quedará en 0 salvo que la marques manualmente.")

    nodos_cuadre = []
    idx = 0

    for nodo in nodos:
        bans_sel = [b for b in nodo.get("bandejas", []) if b["id"] in ban_ids_sel]
        if not bans_sel:
            continue
        despachos_b = sorted(
            [b for b in bans_sel if b.get("type", "deliver") != "devolution"],
            key=lambda b: str(b.get("date") or ""),
        )
        devol_b = sorted(
            [b for b in bans_sel if b.get("type", "deliver") == "devolution"],
            key=lambda b: str(b.get("date") or ""),
        )

        # ── Agregar devoluciones seleccionadas por código ──────────────────────
        # En devoluciones aún no atendidas deliver_quantity=0 y la cantidad
        # solicitada está en 'amount' (verificado contra SGS real).
        dev_agg = defaultdict(float)      # cod → qty devuelta
        dev_detalle = defaultdict(list)   # cod → [(ban_name, qty)]
        for ban in devol_b:
            for mat in ban.get("materiales", []):
                cod, _, _ = _codigo_material(mat)
                if not cod:
                    continue
                q = float(mat.get("deliver_quantity", 0) or 0) \
                    or float(mat.get("amount", 0) or 0)
                if q:
                    dev_agg[cod] += q
                    dev_detalle[cod].append((ban.get("name", ""), q))

        # ── Filas de despacho ──────────────────────────────────────────────────
        despachos = []
        filas_por_cod = defaultdict(list)
        for ban in despachos_b:
            filas = []
            for mat in ban.get("materiales", []):
                cod, dom, desc = _codigo_material(mat)
                qty = float(mat.get("deliver_quantity", 0) or 0)
                k, via = matcher.es_aportacion(cod, desc)
                fila = {
                    "idx":        idx,
                    "cod":        cod,
                    "dom":        dom,
                    "desc":       desc,
                    "qty":        qty,
                    "qty_sol":    float(mat.get("amount", 0) or 0),  # solicitado en la BAN
                    "dev_auto":   0.0,
                    "k":          k,
                    "k_via":      via,
                    "es_cliente": bool(mat.get("is_customer_material")),
                }
                if not cod:
                    advertencias.append(
                        f"{nodo.get('code','')} / {ban.get('name','')}: material sin código "
                        f"({desc[:40]}) — revisa la columna Codigo antes de cargar.")
                filas.append(fila)
                filas_por_cod[fila["cod"]].append(fila)
                idx += 1
            despachos.append({
                "ban_id":   ban["id"],
                "ban_name": ban.get("name", ""),
                "fecha":    str(ban.get("date") or "")[:10],
                "picking":  nombre_campo(ban.get("picking_id")) or "—",
                "equipo":   nombre_campo(ban.get("partner_id")) or "—",
                "filas":    filas,
            })

        # ── Auto-asignar devoluciones a las filas de despacho ─────────────────
        for cod, qdev in dev_agg.items():
            objetivo = filas_por_cod.get(cod, [])
            restante = qdev
            for fila in objetivo:
                if restante <= 0:
                    break
                toma = min(fila["qty"], restante)
                fila["dev_auto"] += toma
                restante -= toma
            if restante > 0.001:
                if objetivo:
                    # Sobra devolución: dejarla visible en la última fila del código
                    objetivo[-1]["dev_auto"] += restante
                    advertencias.append(
                        f"{nodo.get('code','')}: la devolución de {cod} "
                        f"({qdev:g}) supera lo despachado en las bandejas seleccionadas — revísala.")
                else:
                    bans_txt = ", ".join(f"{b}({q:g})" for b, q in dev_detalle[cod])
                    advertencias.append(
                        f"{nodo.get('code','')}: devolución sin despacho par — "
                        f"código {cod} qty {qdev:g} ({bans_txt}). No se incluirá en el cargue.")

        # Id_nodo (col. A) viene del sistema de instalaciones, no de SGS: el campo
        # 'nodo' de SGS suele ser corto ('1', '2'...) y NO es el Id_nodo del cargue.
        # Solo se sugiere si parece un id real (4+ dígitos); si no, lo digita el usuario.
        nodo_num = str(nodo.get("nodo") or "").strip()
        id_nodo_sug = nodo_num if (nodo_num.isdigit() and len(nodo_num) >= 4) else ""

        nodos_cuadre.append({
            "pro":         nodo.get("code", ""),
            "nodo_num":    nodo_num or "—",
            "name":        nodo.get("name", ""),
            "id_nodo_sug": id_nodo_sug,
            "despachos":   despachos,
            "devoluciones_sel": [
                {"ban_name": b.get("name", ""), "fecha": str(b.get("date") or "")[:10],
                 "n_mats": len(b.get("materiales", []))}
                for b in devol_b
            ],
        })

    return {"nodos_cuadre": nodos_cuadre, "advertencias": advertencias, "n_filas": idx}


# ══════════════════════════════════════════════════════════════════════════════
# Excel final — formato exacto de la plantilla
# ══════════════════════════════════════════════════════════════════════════════

def _parse_fecha(s: str):
    """'2026-07-14' o '14/07/2026' → datetime | None."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _num(v):
    """'6' → 6 (int), '2.5' → 2.5, inválido → 0."""
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0
    return int(f) if f.is_integer() else f


def generar_cargue(filas: list) -> bytes:
    """
    filas: [{id_nodo, usuario, fecha_asignacion(dt), codigo, qty, dev,
             fecha_entrega(dt|None), pdi, consecutivo, obs, k, fecha_reintegro(dt|None)}]
    Genera el xlsx con el formato de la plantilla (Hoja1, headers A–L).
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Hoja1"

    for c, h in enumerate(PLANTILLA_HEADERS, 1):
        ws.cell(row=1, column=c, value=h)

    for r, f in enumerate(filas, 2):
        idn = str(f.get("id_nodo", "")).strip()
        ws.cell(row=r, column=1, value=int(idn) if idn.isdigit() else idn)
        ws.cell(row=r, column=2, value=f.get("usuario", ""))
        c3 = ws.cell(row=r, column=3, value=f.get("fecha_asignacion"))
        c3.number_format = FMT_FECHA_HORA
        cod = str(f.get("codigo", "")).strip()
        ws.cell(row=r, column=4, value=int(cod) if cod.isdigit() else cod)
        ws.cell(row=r, column=5, value=_num(f.get("qty")))
        ws.cell(row=r, column=6, value=_num(f.get("dev")))
        if f.get("fecha_entrega"):
            c7 = ws.cell(row=r, column=7, value=f["fecha_entrega"])
            c7.number_format = FMT_FECHA
        ws.cell(row=r, column=8, value=f.get("pdi", ""))
        ws.cell(row=r, column=9, value=f.get("consecutivo", ""))
        if f.get("obs"):
            ws.cell(row=r, column=10, value=f["obs"])
        ws.cell(row=r, column=11, value=1 if str(f.get("k")) == "1" else 0)
        if f.get("fecha_reintegro"):
            c12 = ws.cell(row=r, column=12, value=f["fecha_reintegro"])
            c12.number_format = FMT_FECHA

    # Anchos aproximados a la plantilla original
    for col, w in zip("ABCDEFGHIJKL", [9, 26, 16, 10, 17, 10, 13, 13, 12, 14, 10, 14]):
        ws.column_dimensions[col].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ══════════════════════════════════════════════════════════════════════════════
# Hojas de bandeja fotografiadas en el módulo de devoluciones (almacén las
# valida con el material en la mano) → columna "Dev. hoja" del cuadre.
#   Misma app: devoluciones_sgs.hojas_por_ban(bans) → aplicar_hojas_devoluciones(cuadre, hojas)
# ══════════════════════════════════════════════════════════════════════════════
def _norm_dom(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def aplicar_hojas_devoluciones(cuadre: dict, hojas: dict) -> dict:
    """Enriquece IN PLACE cada fila de despacho con lo leído en su hoja:
    fila.dev_hoja (cantidad a devolver según la hoja), fila.dev_hoja_color
    (semáforo), fila.dev_final (valor por defecto del input: Dev. SGS si ya
    existe la bandeja de devolución, si no la hoja), y despacho.hoja
    (quién validó, cuándo, semáforo). Devuelve un resumen para la vista."""
    bans = [d["ban_name"] for nc in cuadre.get("nodos_cuadre", []) for d in nc.get("despachos", [])]
    hojas = {str(k).upper(): v for k, v in (hojas or {}).items()}
    err = None
    n_con, n_val = 0, 0
    for nc in cuadre.get("nodos_cuadre", []):
        for d in nc.get("despachos", []):
            h = hojas.get(str(d["ban_name"]).upper())
            d["hoja"] = None
            por_dom, por_ref = {}, {}
            if h:
                n_con += 1
                if h.get("validada"):
                    n_val += 1
                d["hoja"] = {"validada": h.get("validada"), "semaforo": h.get("semaforo"),
                             "creada_por": h.get("creada_por"), "sesion": h.get("sesion"),
                             "foto_n": h.get("foto_n"), "lider": h.get("lider_sgs")}
                for m in h.get("materiales", []):
                    if _norm_dom(m.get("dom")):
                        por_dom.setdefault(_norm_dom(m["dom"]), m)
                    if str(m.get("ref") or "").strip():
                        por_ref.setdefault(str(m["ref"]).strip(), m)
            for f in d.get("filas", []):
                m = por_dom.get(_norm_dom(f.get("dom"))) or por_ref.get(str(f.get("cod") or "").strip())
                f["dev_hoja"] = None
                f["dev_hoja_color"] = ""
                if m and isinstance(m.get("devolucion"), (int, float)):
                    f["dev_hoja"] = float(m["devolucion"])
                    f["dev_hoja_color"] = m.get("color") or ""
                # Valor por defecto de "Devolución final": la bandeja de devolución
                # real en SGS manda; si aún no existe (no se ha creado), la hoja.
                f["dev_final"] = f.get("dev_auto") or 0.0
                if not f["dev_final"] and f["dev_hoja"]:
                    f["dev_final"] = f["dev_hoja"]
    return {"error": err, "con_hoja": n_con, "validadas": n_val, "total": len(bans)}


# ── Preferencias persistentes (usuario del cargue, etc.) ───────────────────────

def _prefs_path():
    return os.path.join(_DATA_DIR, "prefs.json")


def get_prefs() -> dict:
    try:
        with open(_prefs_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(**kwargs):
    os.makedirs(_DATA_DIR, exist_ok=True)
    prefs = get_prefs()
    prefs.update({k: v for k, v in kwargs.items() if v is not None})
    with open(_prefs_path(), "w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False)
