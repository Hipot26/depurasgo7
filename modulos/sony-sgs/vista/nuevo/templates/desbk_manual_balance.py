"""
manual_balance.py — Flujo de Balance Manual SGS Puro (4 pasos).

Sesiones: LCL search (index.html) → MO → Materiales → Preview + Excel
"""
import re
import os
import uuid
import io
import json
import tempfile
import threading
from datetime import datetime
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from generar_balance import _cruce_nodo
from sgs_client import parsear_producto

# ── Baremo de precios MDO&MAT ─────────────────────────────────────────────────
_BAREMO_PATHS = [
    Path(__file__).parent / "precios_mdo_mat.json",
    Path(r"C:\Users\duran\Documents\SGS UI\modulos\julian\precios_mdo_mat.json"),
]
_baremo_mem: dict = {}


def get_precios() -> dict:
    """Carga {QP_code: {precio, unidad, descripcion}} desde JSON cache."""
    global _baremo_mem
    if _baremo_mem:
        return _baremo_mem
    for p in _BAREMO_PATHS:
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f:
                    _baremo_mem = json.load(f)
                return _baremo_mem
            except Exception:
                pass
    return _baremo_mem


def enriquecer_mo(rows: list) -> list:
    """Agrega desc, unidad, precio_unit, subtotal a cada fila de MO."""
    precios = get_precios()
    for r in rows:
        info = precios.get(r["qp"], {})
        r["desc"]       = info.get("descripcion", "")
        r["unidad"]     = info.get("unidad", "C/U")
        r["precio_unit"] = info.get("precio", 0.0)
        r["subtotal"]   = round(r["precio_unit"] * r["cantidad"], 2)
        r["en_baremo"]  = bool(info)
    return rows


# ── Colores (esquema verde SGS) ───────────────────────────────────────────────
C_HDR    = "375623"   # verde oscuro — títulos principales
C_SUBHDR = "E2EFDA"   # verde claro  — cabecera columnas
C_PAR    = "F2F2F2"   # gris         — fila par alternada
C_TOTAL  = "FFE699"   # amarillo     — fila total
C_OK     = "C6EFCE"   # verde        — OK
C_AMBAR  = "FFEB9C"   # ámbar        — pendiente
C_ROJO   = "FFC7CE"   # rojo         — error / descuadre
C_GRIS   = "D9D9D9"   # gris oscuro  — sin movimiento
C_DEVOL  = "DEEBF7"   # azul claro   — devolución completa (no liquidar)
BLANCO   = "FFFFFF"

# ── Sesiones ─────────────────────────────────────────────────────────────────
_sm: dict = {}
_SESSION_DIR = os.path.join(tempfile.gettempdir(), "nuevo_sessions")
os.makedirs(_SESSION_DIR, exist_ok=True)


def _session_path(sid: str) -> str:
    return os.path.join(_SESSION_DIR, f"s_{sid}.json")


def save_sesion(sid: str):
    if sid not in _sm:
        return
    try:
        with open(_session_path(sid), "w", encoding="utf-8") as f:
            json.dump(_sm[sid], f, ensure_ascii=False, default=str)
    except Exception:
        # sgs_data puede no ser serializable — guardar sin él para al menos preservar el estado
        try:
            slim = {k: v for k, v in _sm[sid].items() if k != "sgs_data"}
            with open(_session_path(sid), "w", encoding="utf-8") as f:
                json.dump(slim, f, ensure_ascii=False, default=str)
        except Exception:
            pass


def nueva_sesion(padre: dict, parent_id: int) -> str:
    sid = uuid.uuid4().hex[:8]
    odm = padre.get("order_number") or padre.get("odm") or str(parent_id)
    _sm[sid] = {
        "padre":      padre,
        "parent_id":  parent_id,
        "lcl_odm":    odm,
        "rows_mo":    [],
        "errs_mo":    [],
        "rows_mat":   [],
        "errs_mat":   [],
        "modo_mat":   None,
        "nodos_sel":  [],
        "sgs_data":   None,
        "sgs_ready":  None,
        "sgs_error":  None,
        "texto_mo":            "",
        "texto_nodos":         "",
        "nodos_pendientes_sgs": [],
        "lideres":              [],
        "creado_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    save_sesion(sid)
    return sid


def get_sesion(sid: str):
    if sid in _sm:
        s = _sm[sid]
        # Si el fetch corre en otro worker, consultar disco SOLO si ahí ya terminó
        if s.get("sgs_ready") is False:
            path = _session_path(sid)
            if os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        disk = json.load(f)
                    if disk.get("sgs_ready") is True:
                        # El fetch terminó en otro worker — adoptar su estado
                        # Preservar sgs_data en memoria si el disco no lo tiene
                        if disk.get("sgs_data") is None and s.get("sgs_data") is not None:
                            disk["sgs_data"] = s["sgs_data"]
                        _sm[sid] = disk
                        return disk
                except Exception:
                    pass
        return s
    # Fallback: cargar desde archivo si el worker se reinició
    path = _session_path(sid)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            _sm[sid] = data
            return data
        except Exception:
            pass
    return None


def iniciar_fetch_sgs(sid: str, get_cliente_fn):
    """Lanza un hilo daemon que corre balance_completo() en background."""
    sesion = get_sesion(sid)
    if sesion is None:
        return
    sesion["sgs_ready"] = False
    sesion["sgs_error"] = None
    save_sesion(sid)

    def _fetch():
        # Usar _sm directamente para evitar la race condition donde get_sesion()
        # puede reemplazar _sm[sid] y dejar nuestra referencia como huérfana
        s = _sm.get(sid)
        if s is None:
            path = _session_path(sid)
            if os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        s = json.load(f)
                    _sm[sid] = s
                except Exception:
                    return
            else:
                return
        try:
            cliente  = get_cliente_fn()
            data     = cliente.balance_completo(s["parent_id"])
            s["sgs_data"]  = data
            s["sgs_ready"] = True
            s["sgs_error"] = None
        except Exception as e:
            s["sgs_ready"] = True
            s["sgs_error"] = str(e)
        save_sesion(sid)

    threading.Thread(target=_fetch, daemon=True).start()


def resolver_nodos(sgs_nodos: list, mo_nodo_ids: list) -> dict:
    """
    Cruza los nodos de la MO con los nodos SGS usando ID canónico.
    Tanto nodos simples ('74') como cable ('127AL126', 'N127_N126') son normalizados
    al mismo formato canónico antes de comparar.
    """
    # Índice: id_canónico → nodo SGS
    idx = {}
    for n in sgs_nodos:
        cid = _nodo_id_canonico(n.get("nodo"))
        if cid:
            idx[cid] = n

    encontrados = []
    no_encontrados = []

    for mid in mo_nodo_ids:
        cid = _nodo_id_canonico(str(mid))
        nodo_sgs = idx.get(cid)
        if nodo_sgs:
            encontrados.append({
                "num":      mid,
                "pro":      nodo_sgs.get("code", ""),
                "nodo_raw": nodo_sgs.get("nodo", mid),
                "sgs_id":   nodo_sgs.get("id"),
            })
        else:
            no_encontrados.append(mid)

    return {"encontrados": encontrados, "no_encontrados": no_encontrados}


def preparar_preview_materiales(sgs_data: dict) -> list:
    """Prepara filas de materiales para la vista previa (misma lógica que Excel)."""

    def _n(v):
        """Convierte float entero a int para display limpio."""
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v

    resultado = []
    for nodo in sgs_data.get("nodos", []):
        num   = _num_nodo(nodo.get("nodo"))
        code  = nodo.get("code", "")
        name  = nodo.get("name", "") or f"NODO {num}"
        sap_map   = _dom_a_sap(nodo)
        filas_raw = _cruce_nodo(nodo)

        nd_tray = nd_dev = nd_neto = nd_cons = 0.0
        filas = []
        for r in filas_raw:
            sap_cod = sap_map.get(r["cod"]) or (r["cod"].split("/")[-1] if "/" in r["cod"] else r["cod"])
            filas.append({
                "sap":          sap_cod,
                "desc":         r["desc"],
                "ban_desp":     _fmt_ban_names(r["ban_delivers"]),
                "ban_dev":      _fmt_ban_names(r["ban_devs"]),
                "tray":         _n(r["tray"]),
                "devoluciones": _n(r["devoluciones"]),
                "neto":         _n(r["desp_neto"]),
                "consumido":    _n(r["consumido"]),
                "estado":       r["estado"],
                "color":        r["color"],
            })
            nd_tray += r["tray"]
            nd_dev  += r["devoluciones"]
            nd_neto += r["desp_neto"]
            nd_cons += r["consumido"]

        resultado.append({
            "num":             num,
            "code":            code,
            "nombre_completo": name,
            "filas":           filas,
            "totales": {
                "tray": _n(nd_tray),
                "dev":  _n(nd_dev),
                "neto": _n(nd_neto),
                "cons": _n(nd_cons),
            },
        })
    return resultado


# ── Parseo de MO ──────────────────────────────────────────────────────────────

def parsear_mo(texto: str):
    """
    Nodo TAB QP_Code TAB Cantidad (desde Excel del supervisor).
    Retorna (rows, errors).
    """
    rows, errors = [], []
    for i, linea in enumerate(texto.strip().splitlines(), 1):
        partes = re.split(r"\t| {2,}", linea.strip())
        partes = [p.strip() for p in partes if p.strip()]
        if not partes:
            continue
        if len(partes) < 2:
            errors.append(f"Fila {i}: necesitas Nodo y QP (ej: 74&Tab;QP1630)")
            continue
        nodo_raw = partes[0].strip()
        if re.match(r"^\d+$", nodo_raw):
            nodo = nodo_raw
        else:
            # Nodo cable/tramo: N127_N126, 127_126, N1 AL N2, etc.
            s = re.sub(r"\s+", "", nodo_raw.upper())
            if s.startswith("N") and len(s) > 1 and s[1].isdigit():
                s = s[1:]
            m_tramo = re.match(r"^(\d+[A-Z]?)(?:_|AL|-)N?(\d+[A-Z]?)$", s)
            if m_tramo:
                nodo = f"{m_tramo.group(1)}AL{m_tramo.group(2)}"
            else:
                errors.append(f"Fila {i}: nodo inválido '{nodo_raw}'")
                continue
        qp = partes[1].strip().upper()
        if not re.match(r"^QP\d+", qp):
            errors.append(f"Fila {i}: código QP inválido '{qp}'")
            continue
        try:
            cantidad = float(partes[2].replace(",", ".")) if len(partes) >= 3 else 1.0
        except ValueError:
            errors.append(f"Fila {i}: cantidad inválida '{partes[2]}'")
            continue
        rows.append({"nodo": nodo, "qp": qp, "cantidad": cantidad})

    # Deduplicar por (nodo, qp) sumando cantidades
    dedup: dict = {}
    for r in rows:
        k = (r["nodo"], r["qp"])
        if k in dedup:
            dedup[k]["cantidad"] += r["cantidad"]
        else:
            dedup[k] = dict(r)
    return list(dedup.values()), errors


# ── Parseo de Materiales ──────────────────────────────────────────────────────

def detectar_nodos_solos(texto: str):
    """Retorna lista de identificadores de nodo si cada línea tiene UN SOLO token.
    Acepta números (133, 133.0) y nodos cable (N127_N126, N17_N29).
    Si una línea tiene TAB o dos o más espacios → es modo SAP, retorna None."""
    nodos = []
    for linea in texto.strip().splitlines():
        tok = linea.strip()
        if not tok:
            continue
        if re.search(r'\t| {2,}', tok):
            return None  # Múltiples columnas → modo SAP
        # Normalizar floats de Excel: "133.0" → "133"
        try:
            f = float(tok)
            n = int(f)
            tok = str(n) if abs(f - n) < 0.001 else tok
        except ValueError:
            pass
        nodos.append(tok)
    return list(dict.fromkeys(nodos)) if nodos else None


def parsear_mat(texto: str):
    """
    Nodo TAB SAP [TAB Descripción] TAB Cantidad.
    Retorna (rows, errors).
    """
    rows, errors = [], []
    for i, linea in enumerate(texto.strip().splitlines(), 1):
        partes = re.split(r"\t| {2,}", linea.strip())
        partes = [p.strip() for p in partes if p.strip()]
        if not partes:
            continue
        if len(partes) < 2:
            errors.append(f"Fila {i}: necesitas Nodo y Código SAP")
            continue
        try:
            nodo = str(int(float(partes[0])))
        except ValueError:
            errors.append(f"Fila {i}: nodo inválido '{partes[0]}'")
            continue
        sap = partes[1].strip()
        if len(partes) >= 4:
            desc = partes[2]
            try:
                cantidad = float(partes[3].replace(",", "."))
            except ValueError:
                errors.append(f"Fila {i}: cantidad inválida '{partes[3]}'")
                continue
        elif len(partes) == 3:
            try:
                cantidad = float(partes[2].replace(",", "."))
                desc = ""
            except ValueError:
                desc = partes[2]
                cantidad = 1.0
        else:
            desc, cantidad = "", 1.0
        rows.append({"nodo": nodo, "sap": sap, "desc": desc, "cantidad": cantidad})

    dedup: dict = {}
    for r in rows:
        k = (r["nodo"], r["sap"])
        if k in dedup:
            dedup[k]["cantidad"] += r["cantidad"]
        else:
            dedup[k] = dict(r)
    return list(dedup.values()), errors


# ── Helpers SGS ───────────────────────────────────────────────────────────────

def _num_nodo(nodo_val) -> str:
    """'NODO 74' → '74', '74' → '74', False/None → ''"""
    if not nodo_val:
        return ""
    m = re.match(r"^(?:NODO\s+)?(\d+)", str(nodo_val).strip().upper())
    return m.group(1) if m else str(nodo_val).strip()


def _nodo_id_canonico(nodo_val) -> str:
    """
    Normaliza cualquier representación de nodo al mismo ID canónico para comparación.
    '74'            → '74'
    'NODO 74'       → '74'
    '127AL126'      → '127AL126'
    'N127_N126'     → '127AL126'
    'NODO N119_N123'→ '119AL123'
    """
    if not nodo_val:
        return ""
    s = str(nodo_val).strip().upper()
    s = re.sub(r"^NODO\s+", "", s)          # quitar "NODO " inicial
    if s.startswith("N") and len(s) > 1 and s[1].isdigit():
        s = s[1:]                            # quitar N inicial (N127_N126 → 127_N126)
    m_cable = re.match(r"^(\d+[A-Z]?)(?:_|-|AL)N?(\d+[A-Z]?)$", s)
    if m_cable:
        return f"{m_cable.group(1)}AL{m_cable.group(2)}"
    m_num = re.match(r"^(\d+)", s)
    return m_num.group(1) if m_num else s


def filtrar_nodos_sgs(nodos: list, numeros: list) -> list:
    """Filtra nodos SGS por número. Los nodos cable/tramo se ignoran.
    Si no hay coincidencia (p.ej. números de poste físico), devuelve todos."""
    nums_simples = {n for n in numeros if re.match(r"^\d+$", str(n))}
    if not nums_simples:
        return nodos  # Solo nodos cable → todos
    resultado = [n for n in nodos if _num_nodo(n.get("nodo")) in nums_simples]
    return resultado if resultado else nodos  # Sin coincidencia → todos


def _dom_a_sap(nodo: dict) -> dict:
    """
    Construye {cod_dom: cod_sap} desde bandejas + liq_materiales.
    parsear_producto retorna (dom_prefix, sap_num, desc) — solo guardamos
    los pares donde sap_num no está vacío.
    """
    idx: dict = {}
    for ban in nodo.get("bandejas", []):
        for mat in ban.get("materiales", []):
            dom, sap, _ = parsear_producto(mat.get("product_id"))
            if dom and sap:
                idx[dom] = sap
    for m in nodo.get("liq_materiales", []):
        dom, sap, _ = parsear_producto(m.get("product_id"))
        if dom and sap:
            idx[dom] = sap
    return idx


# ── Excel: helpers de escritura ───────────────────────────────────────────────

def _fill(hex_color: str):
    return PatternFill("solid", fgColor=hex_color)

def _font(bold=False, italic=False, color="000000", size=10):
    return Font(bold=bold, italic=italic, color=color, size=size, name="Calibri")

def _align(h="left", v="center", wrap=False):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)

def _border():
    s = Side(style="thin")
    return Border(left=s, right=s, top=s, bottom=s)

def _w(ws, row, col, value="", bold=False, italic=False, bg=None, fg="000000",
       h="left", size=10, wrap=False, borders=True, num_fmt=None, span=None):
    c = ws.cell(row=row, column=col, value=value)
    c.font      = _font(bold=bold, italic=italic, color=fg, size=size)
    c.alignment = _align(h=h, wrap=wrap)
    if bg:
        c.fill = _fill(bg)
    if borders:
        c.border = _border()
    if num_fmt:
        c.number_format = num_fmt
    if span:
        ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=span)
    return c


def _get_sup(padre: dict) -> str:
    sup = padre.get("responsible_id")
    if isinstance(sup, list) and len(sup) >= 2:
        return sup[1]
    return str(sup) if sup else "—"


def _fmt_ban_names(lista):
    """[(ban_name, qty), ...] → 'BAN-103222, BAN-103993'  or  '—'"""
    if not lista:
        return "—"
    seen = []
    for name, _ in lista:
        if name not in seen:
            seen.append(name)
    return ", ".join(seen)


# ── Hoja 1: MO_MANUAL ─────────────────────────────────────────────────────────

def _hoja_mo(ws, rows_mo: list, lcl_odm: str, padre: dict, pendientes_sgs: list = None, lideres: list = None, supervisor: str = None):
    pendientes_set = set(pendientes_sgs or [])
    NCOLS = 9
    titulo = f"MO — MANO DE OBRA MANUAL  |  {lcl_odm}"
    _w(ws, 1, 1, titulo, bold=True, bg=C_HDR, fg=BLANCO, size=12, span=NCOLS)
    ws.row_dimensions[1].height = 22

    meta = [
        ("Proyecto:",    padre.get("name", "—")),
        ("Código padre:", padre.get("code", "—")),
        ("OdM / LCL:",   lcl_odm),
        ("Supervisor:",  supervisor or _get_sup(padre)),
        ("Generado:",    datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]
    for i, (label, val) in enumerate(meta, 2):
        _w(ws, i, 1, label, bold=True, borders=False, bg=C_SUBHDR)
        _w(ws, i, 2, val, borders=False, span=NCOLS)

    sep = len(meta) + 2
    _w(ws, sep, 1, "MANO DE OBRA", bold=True, bg=C_HDR, fg=BLANCO, span=NCOLS)

    hdr = sep + 1
    for j, h in enumerate(["#", "Nodo", "Código QP", "Descripción", "UM", "Precio Unit.", "Cantidad", "Subtotal ($)", "Observaciones"], 1):
        _w(ws, hdr, j, h, bold=True, bg=C_SUBHDR, h="center")

    NF_NUM = "0"
    NF_PES = "0"
    total_cant = 0.0
    total_sub  = 0.0
    for idx, r in enumerate(rows_mo, 1):
        dr  = hdr + idx
        es_pendiente = r["nodo"] in pendientes_set
        bg  = C_AMBAR if es_pendiente else (BLANCO if idx % 2 else C_PAR)
        _w(ws, dr, 1, idx,                     bg=bg, h="center")
        _w(ws, dr, 2, r["nodo"],               bg=bg, h="center")
        _w(ws, dr, 3, r["qp"],                 bg=bg, h="center")
        _w(ws, dr, 4, r.get("desc", ""),       bg=bg, wrap=True)
        _w(ws, dr, 5, r.get("unidad", "C/U"),  bg=bg, h="center")
        _w(ws, dr, 6, r.get("precio_unit", 0), bg=bg, h="right", num_fmt=NF_PES)
        _w(ws, dr, 7, r["cantidad"],            bg=bg, h="center", num_fmt=NF_NUM)
        _w(ws, dr, 8, r.get("subtotal", 0),    bg=bg, h="right",  num_fmt=NF_PES, bold=True)
        _w(ws, dr, 9, "PENDIENTE CREAR SGS" if es_pendiente else "",
           bg=bg, h="center", bold=es_pendiente, fg="7a5800" if es_pendiente else "000000")
        total_cant += r["cantidad"]
        total_sub  += r.get("subtotal", 0)

    tr = hdr + len(rows_mo) + 1
    _w(ws, tr, 1, "TOTAL MANO DE OBRA", bold=True, bg=C_TOTAL, span=7)
    _w(ws, tr, 8, total_sub,  bold=True, bg=C_TOTAL, h="right",  num_fmt=NF_PES)
    _w(ws, tr, 9, f"{len(pendientes_set)} pend. SGS" if pendientes_set else "",
       bold=True, bg=C_TOTAL, h="center")

    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 8
    ws.column_dimensions["C"].width = 13
    ws.column_dimensions["D"].width = 48
    ws.column_dimensions["E"].width = 8
    ws.column_dimensions["F"].width = 16
    ws.column_dimensions["G"].width = 11
    ws.column_dimensions["H"].width = 16
    ws.column_dimensions["I"].width = 22

    # ── Sección Líderes ────────────────────────────────────────────────────────
    if lideres:
        ldr_start = tr + 2
        _w(ws, ldr_start, 1, "DISTRIBUCIÓN DE LIQUIDACIÓN — LÍDERES RESPONSABLES",
           bold=True, bg=C_HDR, fg=BLANCO, size=11, span=NCOLS)
        ldr_hdr = ldr_start + 1
        for j, h in enumerate(["#", "Cédula", "Nombre del Líder", "% Liquidación", "", "", "", "", ""], 1):
            _w(ws, ldr_hdr, j, h, bold=bool(h), bg=C_SUBHDR, h="center")
        for idx_l, ldr in enumerate(lideres, 1):
            dr = ldr_hdr + idx_l
            bg = BLANCO if idx_l % 2 else C_PAR
            _w(ws, dr, 1, idx_l,                       bg=bg, h="center")
            _w(ws, dr, 2, ldr.get("cedula", ""),       bg=bg, h="center")
            _w(ws, dr, 3, ldr.get("nombre", ""),       bg=bg)
            _w(ws, dr, 4, f"{ldr.get('pct', 0)}%",    bg=bg, h="center", bold=True)
            for col in range(5, NCOLS + 1):
                _w(ws, dr, col, "", bg=bg)


# ── Hoja 2: SGS_BALANCE ───────────────────────────────────────────────────────

def _hoja_sgs_balance(wb, sgs_data: dict, lcl_odm: str):
    """Crea una hoja por nodo — freeze fijo en A3 (header nodo + columnas)."""
    padre  = sgs_data["padre"]
    nodos  = sgs_data["nodos"]
    pdl    = sgs_data.get("pdl", "") or ""
    NF     = "0"
    NCOLS  = 10

    COLS_HDR = ["#", "Código SAP", "Descripción", "Bandejas Desp.", "Bandejas Dev.",
                "Despachos", "Devoluciones", "Bal. Neto", "Consumido", "Estado"]
    widths   = [5, 14, 36, 18, 18, 11, 13, 11, 11, 16]

    for nodo in nodos:
        num  = _num_nodo(nodo.get("nodo")) or ""
        code = nodo.get("code", "")

        # Nombre de la pestaña: N74, N75, … (max 31 chars)
        sheet_name = (f"N{num}" if num else code)[:31]
        ws = wb.create_sheet(sheet_name)
        ws.sheet_view.showGridLines = False
        for j, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = w

        # ── Fila 1: header del nodo (siempre visible — frozen) ────────────────
        hdr_txt = f"Nodo {num}  —  {code}  |  {lcl_odm}"
        if pdl:
            hdr_txt += f"  |  PDL: {pdl}"
        _w(ws, 1, 1, hdr_txt, bold=True, bg=C_HDR, fg=BLANCO, size=11, span=NCOLS)
        ws.row_dimensions[1].height = 18

        # ── Fila 2: cabecera de columnas (siempre visible — frozen) ───────────
        for j, h in enumerate(COLS_HDR, 1):
            _w(ws, 2, j, h, bold=True, bg=C_SUBHDR, h="center")
        ws.row_dimensions[2].height = 14

        # Freeze: header nodo + columnas siempre al tope
        ws.freeze_panes = "A3"

        # ── Filas de datos desde fila 3 ───────────────────────────────────────
        sap_map    = _dom_a_sap(nodo)
        filas_nodo = _cruce_nodo(nodo)
        nd_tray = nd_dalm = nd_neto = nd_cons = nd_pend = 0.0
        seq  = 1
        fila = 3

        for r in filas_nodo:
            color   = r["color"]
            sap_cod = sap_map.get(r["cod"]) or r["cod"]
            bg_dev  = C_DEVOL if r["devoluciones"] > 0 else color
            _w(ws, fila,  1, seq,                               bg=color,  h="center")
            _w(ws, fila,  2, sap_cod,                           bg=color,  h="center")
            _w(ws, fila,  3, r["desc"],                         bg=color,  wrap=True)
            _w(ws, fila,  4, _fmt_ban_names(r["ban_delivers"]), bg=color,  h="left",   size=9, wrap=True)
            _w(ws, fila,  5, _fmt_ban_names(r["ban_devs"]),     bg=color,  h="left",   size=9, wrap=True)
            _w(ws, fila,  6, r["tray"],                         bg=color,  h="center", num_fmt=NF)
            _w(ws, fila,  7, r["devoluciones"],                 bg=bg_dev, h="center", num_fmt=NF,
               bold=(r["devoluciones"] > 0))
            _w(ws, fila,  8, r["desp_neto"],                    bg=color,  h="center", num_fmt=NF, bold=True)
            _w(ws, fila,  9, r["consumido"],                    bg=color,  h="center", num_fmt=NF)
            _w(ws, fila, 10, r["estado"],                       bg=color,  h="center")
            ws.row_dimensions[fila].height = 22
            nd_tray += r["tray"];      nd_dalm += r["devoluciones"]
            nd_neto += r["desp_neto"]; nd_cons += r["consumido"]
            nd_pend += r["pendiente"]
            fila += 1
            seq  += 1

        # ── Subtotal del nodo ─────────────────────────────────────────────────
        bg_sp      = C_OK if abs(nd_pend) < 0.01 else (C_AMBAR if nd_pend > 0 else C_ROJO)
        bg_dev_tot = C_DEVOL if nd_dalm > 0 else C_TOTAL
        _w(ws, fila, 1, f"Subtotal Nodo {num}", bold=True, bg=C_TOTAL, span=5)
        _w(ws, fila, 6, nd_tray,  bold=True, bg=C_TOTAL,    h="center", num_fmt=NF)
        _w(ws, fila, 7, nd_dalm,  bold=True, bg=bg_dev_tot, h="center", num_fmt=NF)
        _w(ws, fila, 8, nd_neto,  bold=True, bg=bg_sp,      h="center", num_fmt=NF)
        _w(ws, fila, 9, nd_cons,  bold=True, bg=C_TOTAL,    h="center", num_fmt=NF)
        _w(ws, fila, 10, "",      bold=True, bg=C_TOTAL)


# ── Hoja 3: CRUCE_MAT ────────────────────────────────────────────────────────

def _sap_desde_cod(cod: str) -> str:
    """'DOM-028733-12/274287' → '274287'  |  '274287' → '274287'"""
    return cod.split("/")[-1] if "/" in cod else cod


def _hoja_cruce_mat(ws, rows_mat: list, sgs_data: dict, modo_mat: str):
    NCOLS = 7

    if modo_mat == "manual_sap":
        titulo = "CRUCE — Materiales Manual vs SGS"
        hdrs   = ["#", "Nodo", "Código SAP", "Descripción", "Manual Cant.", "SGS Neto", "Estado"]
    else:
        titulo = "CRUCE — Balance SGS (Neto vs Consumido)"
        hdrs   = ["#", "Nodo", "Código", "Descripción", "SGS Neto", "Consumido", "Estado"]

    _w(ws, 1, 1, titulo, bold=True, bg=C_HDR, fg=BLANCO, size=12, span=NCOLS)
    ws.row_dimensions[1].height = 22

    leyenda = [
        (C_OK,    "OK — cantidades coinciden o listo"),
        (C_AMBAR, "PENDIENTE — sin liquidar o parcial"),
        (C_GRIS,  "SIN MOV — neto cero"),
        (C_ROJO,  "DESCUADRE — cantidades distintas"),
    ]
    for i, (color, txt) in enumerate(leyenda, 2):
        _w(ws, i, 1, "", bg=color, borders=False)
        _w(ws, i, 2, txt, bg=color, borders=False, span=NCOLS - 1)

    hdr_row = len(leyenda) + 2
    for j, h in enumerate(hdrs, 1):
        _w(ws, hdr_row, j, h, bold=True, bg=C_SUBHDR, h="center")
    ws.freeze_panes = f"A{hdr_row + 1}"

    fila = hdr_row + 1
    seq  = 1
    NF   = "0"

    if modo_mat == "manual_sap":
        # Indexar SGS por nodo → SAP → {neto, desc}
        sgs_idx: dict = defaultdict(dict)
        for nodo in sgs_data["nodos"]:
            num     = _num_nodo(nodo.get("nodo"))
            sap_map = _dom_a_sap(nodo)
            for r in _cruce_nodo(nodo):
                sap = sap_map.get(r["cod"]) or r["cod"]
                sgs_idx[num][sap] = {"neto": r["desp_neto"], "desc": r["desc"]}

        vistos: set = set()
        for r in rows_mat:
            nodo, sap, manual_q = r["nodo"], r["sap"], r["cantidad"]
            sgs_info = sgs_idx.get(nodo, {}).get(sap)
            if sgs_info:
                sgs_neto = sgs_info["neto"]
                desc     = sgs_info["desc"] or r.get("desc", "")
                diff     = manual_q - sgs_neto
                if abs(diff) < 0.001:
                    estado, color = "OK", C_OK
                else:
                    estado, color = f"DIFER {diff:+.2f}", C_ROJO
            else:
                sgs_neto = "—"
                desc     = r.get("desc", "")
                estado, color = "SOLO MANUAL", C_AMBAR

            _w(ws, fila, 1, seq,      bg=color, h="center")
            _w(ws, fila, 2, nodo,     bg=color, h="center")
            _w(ws, fila, 3, sap,      bg=color, h="center")
            _w(ws, fila, 4, desc,     bg=color, wrap=True)
            _w(ws, fila, 5, manual_q, bg=color, h="center", num_fmt=NF)
            _w(ws, fila, 6, sgs_neto, bg=color, h="center", num_fmt=NF if sgs_neto != "—" else None)
            _w(ws, fila, 7, estado,   bg=color, h="center")
            vistos.add((nodo, sap))
            fila += 1; seq += 1

        # Materiales SOLO en SGS (no ingresados manualmente)
        for nodo in sgs_data["nodos"]:
            num     = _num_nodo(nodo.get("nodo"))
            sap_map = _dom_a_sap(nodo)
            for r in _cruce_nodo(nodo):
                sap = sap_map.get(r["cod"]) or r["cod"]
                if (num, sap) in vistos or r["desp_neto"] == 0:
                    continue
                _w(ws, fila, 1, seq,             bg=C_GRIS, h="center")
                _w(ws, fila, 2, num,             bg=C_GRIS, h="center")
                _w(ws, fila, 3, sap,             bg=C_GRIS, h="center")
                _w(ws, fila, 4, r["desc"],       bg=C_GRIS, wrap=True)
                _w(ws, fila, 5, "—",             bg=C_GRIS, h="center")
                _w(ws, fila, 6, r["desp_neto"],  bg=C_GRIS, h="center", num_fmt=NF)
                _w(ws, fila, 7, "SOLO SGS",      bg=C_GRIS, h="center")
                fila += 1; seq += 1

    else:
        # Nodo solo: SGS neto vs consumido
        for nodo in sgs_data["nodos"]:
            num = _num_nodo(nodo.get("nodo"))
            for r in _cruce_nodo(nodo):
                neto = r["desp_neto"]; cons = r["consumido"]
                if neto == 0 and cons == 0:
                    color = C_GRIS
                elif cons >= neto > 0:
                    color = C_OK
                elif cons > 0:
                    color = C_AMBAR
                elif neto > 0:
                    color = C_AMBAR
                else:
                    color = C_GRIS
                sap_cod = _sap_desde_cod(r["cod"])
                _w(ws, fila, 1, seq,      bg=color, h="center")
                _w(ws, fila, 2, num,      bg=color, h="center")
                _w(ws, fila, 3, sap_cod,  bg=color, h="center")
                _w(ws, fila, 4, r["desc"], bg=color, wrap=True)
                _w(ws, fila, 5, neto,     bg=color, h="center", num_fmt=NF, bold=True)
                _w(ws, fila, 6, cons,     bg=color, h="center", num_fmt=NF)
                _w(ws, fila, 7, r["estado"], bg=color, h="center")
                fila += 1; seq += 1

    widths = [5, 8, 14, 40, 12, 12, 16]
    for j, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(j)].width = w


# ── Generador principal ────────────────────────────────────────────────────────

def generar_excel_manual(sesion: dict) -> bytes:
    wb = Workbook()

    ws_mo = wb.active;  ws_mo.title = "MO_MANUAL"

    sgs_data_base = sesion.get("sgs_data") or {"padre": sesion.get("padre", {}), "nodos": []}

    # sgs_data para materiales: solo los nodos que el usuario seleccionó explícitamente
    nodos_mat = sesion.get("nodos_mat", [])
    sgs_data_mat = {
        "padre": sgs_data_base.get("padre") or sesion.get("padre", {}),
        "nodos": nodos_mat,
        "pdl":   sesion.get("pdl", ""),
    }

    _hoja_mo(ws_mo, sesion["rows_mo"], sesion["lcl_odm"], sesion["padre"],
             sesion.get("nodos_pendientes_sgs", []),
             sesion.get("lideres", []),
             sesion.get("supervisor"))
    # Una hoja por nodo de materiales seleccionados; vacío si el usuario no eligió ninguno
    _hoja_sgs_balance(wb, sgs_data_mat, sesion["lcl_odm"])
    ws_cruce = wb.create_sheet("CRUCE_MAT")
    _hoja_cruce_mat(ws_cruce, sesion["rows_mat"], sgs_data_mat,
                    sesion.get("modo_mat") or "nodo_solo")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
