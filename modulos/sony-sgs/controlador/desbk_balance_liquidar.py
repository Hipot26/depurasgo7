#!/usr/bin/env python3
"""
Balance para Liquidar - SIPWEBORA  (Plantilla Sergio)
Uso: python balance_liquidar.py <idplanilla>
Ejemplo: python balance_liquidar.py 17079

Genera balance_liquidar_{id}.xlsx con:
  - Hoja MANO DE OBRA: código, descripción, unidad, cantidad
  - Hoja MATERIALES:   código, descripción, unidad, I N / I RZ / R CH / R RZ / M A
"""

import sys
import os
import re
import urllib.request
import urllib.error
from datetime import datetime

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("[ERROR] Falta beautifulsoup4. Ejecuta: pip install beautifulsoup4 lxml openpyxl")
    sys.exit(1)

try:
    import openpyxl
    from openpyxl.styles import (
        Font, PatternFill, Alignment, Border, Side, numbers
    )
    from openpyxl.utils import get_column_letter
except ImportError:
    print("[ERROR] Falta openpyxl. Ejecuta: pip install openpyxl")
    sys.exit(1)

BASE_URL = "https://sipwebora.com/sonny/html_excel/"

# ─── Colores de la plantilla "Sergio" ────────────────────────────────────────
COLOR_TITULO     = "1F3864"   # Azul oscuro
COLOR_MO_HEADER  = "2E75B6"   # Azul mano de obra
COLOR_MAT_HEADER = "375623"   # Verde materiales
COLOR_SUBHEADER  = "D6E4F0"   # Azul claro (fila col)
COLOR_MAT_SUB    = "E2EFDA"   # Verde claro
COLOR_FILA_ALT   = "F2F2F2"   # Gris claro alterno
COLOR_TOTAL      = "FFE699"   # Amarillo totales
BLANCO           = "FFFFFF"

def _border(style="thin"):
    s = Side(style=style)
    return Border(left=s, right=s, top=s, bottom=s)

def _fill(hex_color):
    return PatternFill("solid", fgColor=hex_color)

def _font(bold=False, color="000000", size=10):
    return Font(bold=bold, color=color, size=size, name="Calibri")

def _align(h="center", v="center", wrap=False):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)

def _set_cell(ws, row, col, value, bold=False, bg=None, fg="000000",
              align_h="left", size=10, wrap=False, border=True, num_fmt=None):
    cell = ws.cell(row=row, column=col, value=value)
    cell.font      = _font(bold=bold, color=fg, size=size)
    cell.alignment = _align(h=align_h, wrap=wrap)
    if bg:
        cell.fill = _fill(bg)
    if border:
        cell.border = _border()
    if num_fmt:
        cell.number_format = num_fmt
    return cell

# ─── Descarga y parseo del HTML ──────────────────────────────────────────────

def descargar_html(id_planilla: str) -> str:
    url = f"{BASE_URL}acta.php?idplanilla={id_planilla}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "es-CO,es;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.URLError as e:
        print(f"[ERROR] No se pudo descargar: {e}")
        sys.exit(1)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def extraer_datos(html: str) -> dict:
    """Extrae metadata, mano de obra y materiales del HTML."""
    soup = BeautifulSoup(html, "lxml")
    rows = soup.find_all("tr")

    def text_of(td):
        return td.get_text(" ", strip=True).replace("\xa0", "").strip()

    def row_cells_with_pos(row):
        """Retorna lista de (col_start, colspan, texto) por cada TD."""
        result = []
        pos = 0
        for td in row.find_all("td"):
            cs  = int(td.get("colspan", 1))
            txt = text_of(td)
            result.append((pos, cs, txt))
            pos += cs
        return result

    # ── Metadata ─────────────────────────────────────────────────────────
    # Extracción por patrones sobre el texto plano de las primeras ~30 filas.
    # Cada planilla tiene estructura fija pero valores en filas separadas.
    metadata = {}

    # Texto plano de todas las celdas no vacías de las primeras 30 filas
    early_texts = []
    for row in rows[:30]:
        for td in row.find_all("td"):
            t = text_of(td)
            if t:
                early_texts.append(t)

    # Helper: devuelve el valor que sigue a un label en early_texts
    def valor_tras_label(label_substr, skip_labels=None):
        skip = skip_labels or []
        for j, t in enumerate(early_texts):
            if label_substr in t:
                for k in range(j + 1, min(j + 6, len(early_texts))):
                    candidate = early_texts[k]
                    # Ignorar si parece otro label o texto de convención
                    is_other_label = any(
                        sub in candidate
                        for sub in ["Fecha de", "Empresa", "OdM", "Ticket",
                                    "Zona /", "Barrio", "Libreto", "N\u00ba Proyecto",
                                    "Protocolo", "LCL", "COD CTO", "Nombre del",
                                    "Descripci", "Punto de", "LM=", "LV=", "FR=", "OC="]
                                   + skip
                    )
                    if not is_other_label and len(candidate) > 1:
                        return candidate
        return ""

    # Empresa: texto tras "Empresa Colaboradora"
    metadata["empresa"] = valor_tras_label("Empresa Colaboradora")

    # OdM/Grafo: buscar patrón DF/OT/ET… directamente en el texto completo
    for t in early_texts:
        if re.match(r'^DF\d{12,}$', t.strip()):
            metadata["odm"] = t.strip()
            break
    if not metadata.get("odm"):
        metadata["odm"] = valor_tras_label("OdM / Grafo",
            skip_labels=["N\u00ba Proyecto", "Protocolo", "LCL", "COD CTO"])

    # Código Matriz E1
    metadata["codigo_matriz"] = valor_tras_label("C\u00f3digo Matriz",
        skip_labels=["Nombre del Proyecto", "Ticket", "Zona /"])

    # Ticket: patrón tipo CD/ET/OT/PD seguido de dígitos
    for t in early_texts:
        if re.match(r'^(CD|ET|OT|PD|PI|WT)\d+$', t.strip()):
            metadata["ticket"] = t.strip()
            break

    # Fecha de ejecución: dd/mm/yyyy
    full_text_early = " ".join(early_texts)
    fecha_match = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', full_text_early)
    if fecha_match:
        metadata["fecha_ejecucion"] = fecha_match.group(1)

    # ID planilla desde el título
    title = soup.find("title")
    if title:
        metadata["titulo"] = title.get_text(strip=True)

    # TOTAL TRABAJO: buscar $X,XXX,XXX.XX
    for row in rows:
        t = row.get_text()
        if "TOTAL TRABAJO" in t:
            valores = re.findall(r'\$[\d,]+\.\d{2}', t)
            if len(valores) >= 2:
                metadata["total_mo"]  = valores[0]
                metadata["total_mat"] = valores[1]
            elif len(valores) == 1:
                metadata["total_mo"] = valores[0]

    # ── Detectar filas de sección ─────────────────────────────────────────
    idx_mo_header  = None
    idx_mo_end     = None
    idx_mat_header = None
    idx_mat_end    = None

    for i, row in enumerate(rows):
        t = row.get_text(" ", strip=True)
        if idx_mo_header is None and "Descripci" in t and "Actividad" in t and "Prestaci" in t:
            idx_mo_header = i
        if idx_mo_header and idx_mo_end is None and "VALOR MANO OBRA" in t:
            idx_mo_end = i
        if idx_mat_header is None and "Cod. Material" in t and "I N" in t:
            idx_mat_header = i
        if idx_mat_header and idx_mat_end is None and "CONVENCIONES MATERIALES" in t:
            idx_mat_end = i

    if idx_mo_header is None or idx_mo_end is None:
        print("[ADVERTENCIA] No se encontró sección de Mano de Obra.")
    if idx_mat_header is None or idx_mat_end is None:
        print("[ADVERTENCIA] No se encontró sección de Materiales.")

    # ── Mano de Obra ─────────────────────────────────────────────────────
    # Col positions en header MO: Prestación cs=11 (pos≈95), Desc cs=42, Unidad cs=6, Cantidad cs=6
    # Nodo: primera celda con cs=7 y texto numérico
    mo_data = []
    if idx_mo_header is not None and idx_mo_end is not None:
        for row in rows[idx_mo_header + 1 : idx_mo_end]:
            cells = row_cells_with_pos(row)
            nodo = cod = desc = unidad = cantidad = ""
            for pos, cs, txt in cells:
                if cs == 7 and txt and re.match(r'^\d+[A-Za-z]*$', txt) and not nodo:
                    nodo = txt.upper()
                elif cs == 11 and txt:
                    cod = txt
                elif cs == 42 and txt:
                    desc = txt
                elif cs == 6 and txt and not unidad:
                    unidad = txt
                elif cs == 6 and txt and unidad:
                    cantidad = txt
            if cod and desc:
                try:
                    cantidad_num = float(cantidad.replace(",", ".")) if cantidad else 1
                except ValueError:
                    cantidad_num = cantidad
                mo_data.append({
                    "nodo":     nodo,
                    "codigo":   cod,
                    "desc":     desc,
                    "unidad":   unidad,
                    "cantidad": cantidad_num,
                })

    # ── Materiales ────────────────────────────────────────────────────────
    # Posiciones fijas en las filas de materiales:
    #   col_start=48  cs=10 → Código
    #   col_start=58  cs=4  → I N
    #   col_start=62  cs=4  → I RZ
    #   col_start=66  cs=4  → R CH
    #   col_start=70  cs=4  → R RZ
    #   col_start=74  cs=4  → M A
    #   col_start=78  cs=9  → Unidad
    #   col_start=87  cs=12 → Movimiento
    #   col_start=99  cs=52 → Descripción
    COL_MAT_POSITIONS = {
        48: "codigo",
        58: "i_n",
        62: "i_rz",
        66: "r_ch",
        70: "r_rz",
        74: "m_a",
        78: "unidad",
        99: "desc",
    }
    mat_data = []
    if idx_mat_header is not None and idx_mat_end is not None:
        for row in rows[idx_mat_header + 1 : idx_mat_end]:
            cells = row_cells_with_pos(row)
            entry = {k: "" for k in ["nodo","codigo","desc","unidad","i_n","i_rz","r_ch","r_rz","m_a"]}
            has_cod = False
            for pos, cs, txt in cells:
                # Nodo: cs=7, digito(s) con sufijo letra opcional (ej: 1, 1A, 2B)
                if cs == 7 and txt and re.match(r'^\d+[A-Za-z]*$', txt) and not entry["nodo"]:
                    entry["nodo"] = txt.upper()
                elif pos in COL_MAT_POSITIONS and txt:
                    key = COL_MAT_POSITIONS[pos]
                    entry[key] = txt
                    if key == "codigo":
                        has_cod = True
            if has_cod and entry["desc"]:
                # Convertir cantidades a número
                for k in ["i_n","i_rz","r_ch","r_rz","m_a"]:
                    v = entry[k]
                    if v:
                        try:
                            entry[k] = float(v.replace(",", "."))
                        except ValueError:
                            pass
                mat_data.append(entry)

    # Zona y Barrio: se toman directamente de los datos de actividades (más fiables)
    if mo_data:
        # Las filas de MO tienen direccion (cs=25) con el barrio/vereda
        # y CD (cs=8) con el código de zona — extraemos del HTML directo
        pass
    # Buscar en las filas de datos MO/materiales: la dirección y zona
    if idx_mo_header is not None and idx_mo_end is not None:
        for row in rows[idx_mo_header + 1: idx_mo_end]:
            cells = row_cells_with_pos(row)
            for pos, cs, txt in cells:
                if cs == 25 and txt and "metadata" not in txt:
                    metadata.setdefault("barrio", txt)
                if cs == 8 and txt and txt.isdigit():
                    metadata.setdefault("zona_cd", txt)

    return {
        "metadata": metadata,
        "mo":       mo_data,
        "mat":      mat_data,
    }


# ─── Generación del Excel ─────────────────────────────────────────────────────

def crear_excel(datos: dict, id_planilla: str, ruta_salida: str):
    wb = openpyxl.Workbook()

    meta   = datos["metadata"]
    mo     = datos["mo"]
    mat    = datos["mat"]
    titulo = meta.get("titulo", f"Planilla {id_planilla}")

    # ═══════════════════════════════════════════════════════════════════════
    # HOJA 1: MANO DE OBRA
    # ═══════════════════════════════════════════════════════════════════════
    ws_mo = wb.active
    ws_mo.title = "MANO DE OBRA"
    ws_mo.sheet_view.showGridLines = False

    # Anchos: # | Nodo | Código | Descripción | Unidad | Cantidad | Observación
    col_widths = [5, 8, 14, 55, 12, 12, 20]
    for i, w in enumerate(col_widths, 1):
        ws_mo.column_dimensions[get_column_letter(i)].width = w

    r = 1
    # ── Bloque título ──
    ws_mo.merge_cells(f"A{r}:G{r}")
    c = ws_mo.cell(row=r, column=1, value=f"BALANCE PARA LIQUIDAR  —  {titulo}")
    c.font      = Font(bold=True, color=BLANCO, size=13, name="Calibri")
    c.fill      = _fill(COLOR_TITULO)
    c.alignment = _align(h="center")
    ws_mo.row_dimensions[r].height = 22
    r += 1

    # ── Metadata en 2 columnas ──
    meta_fields = [
        ("Empresa:",       meta.get("empresa", "")),
        ("OdM / Grafo:",   meta.get("odm", "")),
        ("Ticket / PDL:",  meta.get("ticket", "")),
        ("Zona CD:",       meta.get("zona_cd", "")),
        ("Barrio/Vereda:", meta.get("barrio", "")),
        ("Fecha ejec.:",   meta.get("fecha_ejecucion", "")),
        ("Cod. Matriz:",   meta.get("codigo_matriz", "")),
    ]
    for label, value in meta_fields:
        ws_mo.merge_cells(f"A{r}:A{r}")
        ws_mo.merge_cells(f"B{r}:D{r}")
        _set_cell(ws_mo, r, 1, label,  bold=True,  bg=COLOR_SUBHEADER, align_h="right",  border=False)
        _set_cell(ws_mo, r, 2, value,  bold=False, bg=BLANCO,          align_h="left",   border=False)
        ws_mo.row_dimensions[r].height = 15
        r += 1

    r += 1  # espacio

    # ── Header de tabla ──
    # Columnas: # | Nodo | Código | Descripción de la Actividad | Unidad | Cantidad | Observación
    headers_mo = ["#", "Nodo", "Código", "Descripción de la Actividad", "Unidad", "Cantidad", "Observación"]
    ws_mo.merge_cells(f"A{r}:G{r}")
    c = ws_mo.cell(row=r, column=1, value="MANO DE OBRA")
    c.font      = Font(bold=True, color=BLANCO, size=11, name="Calibri")
    c.fill      = _fill(COLOR_MO_HEADER)
    c.alignment = _align(h="center")
    ws_mo.row_dimensions[r].height = 18
    r += 1

    for col_i, h in enumerate(headers_mo, 1):
        _set_cell(ws_mo, r, col_i, h,
                  bold=True, bg=COLOR_SUBHEADER, align_h="center", size=9)
    ws_mo.row_dimensions[r].height = 14
    r += 1

    # ── Datos ──
    for idx, item in enumerate(mo, 1):
        bg = BLANCO if idx % 2 == 0 else COLOR_FILA_ALT
        _set_cell(ws_mo, r, 1, idx,              bg=bg, align_h="center", size=9)
        _set_cell(ws_mo, r, 2, item["nodo"],     bg=bg, align_h="center", size=9,
                  bold=True if item["nodo"] else False)
        _set_cell(ws_mo, r, 3, item["codigo"],   bg=bg, align_h="center", size=9)
        _set_cell(ws_mo, r, 4, item["desc"],     bg=bg, align_h="left",   size=9, wrap=True)
        _set_cell(ws_mo, r, 5, item["unidad"],   bg=bg, align_h="center", size=9)
        cant = item["cantidad"]
        if isinstance(cant, float) and cant == int(cant):
            cant = int(cant)
        _set_cell(ws_mo, r, 6, cant,             bg=bg, align_h="center", size=9)
        _set_cell(ws_mo, r, 7, "",               bg=bg, size=9)
        ws_mo.row_dimensions[r].height = 14
        r += 1

    # ── Fila de totales MO ──
    ws_mo.merge_cells(f"A{r}:E{r}")
    _set_cell(ws_mo, r, 1, "TOTAL MANO DE OBRA", bold=True, bg=COLOR_TOTAL,
              align_h="right", size=10)
    _set_cell(ws_mo, r, 6, meta.get("total_mo", ""), bold=True, bg=COLOR_TOTAL,
              align_h="center", size=10)
    _set_cell(ws_mo, r, 7, "", bg=COLOR_TOTAL)
    ws_mo.row_dimensions[r].height = 16

    # Freeze header
    ws_mo.freeze_panes = ws_mo[f"A{r - len(mo)}"]

    # ═══════════════════════════════════════════════════════════════════════
    # HOJA 2: MATERIALES
    # ═══════════════════════════════════════════════════════════════════════
    ws_mat = wb.create_sheet("MATERIALES")
    ws_mat.sheet_view.showGridLines = False

    # Columnas: # | Nodo | Código | Material | Cantidad | Tipo | Observación
    col_widths_mat = [5, 8, 14, 60, 12, 10, 24]
    for i, w in enumerate(col_widths_mat, 1):
        ws_mat.column_dimensions[get_column_letter(i)].width = w

    r = 1
    # ── Título ──
    ws_mat.merge_cells(f"A{r}:G{r}")
    c = ws_mat.cell(row=r, column=1, value=f"BALANCE PARA LIQUIDAR  —  {titulo}")
    c.font      = Font(bold=True, color=BLANCO, size=13, name="Calibri")
    c.fill      = _fill(COLOR_TITULO)
    c.alignment = _align(h="center")
    ws_mat.row_dimensions[r].height = 22
    r += 1

    # ── Metadata ──
    for label, value in meta_fields:
        ws_mat.merge_cells(f"A{r}:A{r}")
        ws_mat.merge_cells(f"B{r}:G{r}")
        _set_cell(ws_mat, r, 1, label,  bold=True,  bg=COLOR_MAT_SUB, align_h="right",  border=False)
        _set_cell(ws_mat, r, 2, value,  bold=False, bg=BLANCO,         align_h="left",   border=False)
        ws_mat.row_dimensions[r].height = 15
        r += 1

    r += 1

    # ── Header ──
    ws_mat.merge_cells(f"A{r}:G{r}")
    c = ws_mat.cell(row=r, column=1, value="MATERIALES")
    c.font      = Font(bold=True, color=BLANCO, size=11, name="Calibri")
    c.fill      = _fill(COLOR_MAT_HEADER)
    c.alignment = _align(h="center")
    ws_mat.row_dimensions[r].height = 18
    r += 1

    headers_mat = ["#", "Nodo", "Codigo", "Material", "Cantidad", "Tipo", "Observacion"]
    for col_i, h in enumerate(headers_mat, 1):
        _set_cell(ws_mat, r, col_i, h,
                  bold=True, bg=COLOR_MAT_SUB, align_h="center", size=9)
    ws_mat.row_dimensions[r].height = 14
    r += 1

    # ── Datos ──
    tipo_map = {"i_n": "I N", "i_rz": "I RZ", "r_ch": "R CH", "r_rz": "R RZ", "m_a": "M A"}
    for idx, item in enumerate(mat, 1):
        bg = BLANCO if idx % 2 == 0 else COLOR_FILA_ALT
        # Cantidad y Tipo = primer valor no vacío entre las columnas de movimiento
        tipo_key  = next((k for k in ["i_n","i_rz","r_ch","r_rz","m_a"] if item[k] != ""), None)
        cant_mat  = item[tipo_key] if tipo_key else ""
        tipo_val  = tipo_map.get(tipo_key, "") if tipo_key else ""
        # Convertir a entero si es número entero (ej: 1.0 → 1)
        if isinstance(cant_mat, float) and cant_mat == int(cant_mat):
            cant_mat = int(cant_mat)
        _set_cell(ws_mat, r, 1, idx,            bg=bg, align_h="center", size=9)
        _set_cell(ws_mat, r, 2, item["nodo"],   bg=bg, align_h="center", size=9,
                  bold=True if item["nodo"] else False)
        _set_cell(ws_mat, r, 3, item["codigo"], bg=bg, align_h="center", size=9)
        _set_cell(ws_mat, r, 4, item["desc"],   bg=bg, align_h="left",   size=9, wrap=True)
        _set_cell(ws_mat, r, 5, cant_mat,       bg=bg, align_h="center", size=9)
        _set_cell(ws_mat, r, 6, tipo_val,       bg=bg, align_h="center", size=9)
        _set_cell(ws_mat, r, 7, "",             bg=bg, size=9)
        ws_mat.row_dimensions[r].height = 14
        r += 1

    # ── Fila total MAT ──
    ws_mat.merge_cells(f"A{r}:D{r}")
    _set_cell(ws_mat, r, 1, "TOTAL MATERIALES", bold=True, bg=COLOR_TOTAL,
              align_h="right", size=10)
    _set_cell(ws_mat, r, 5, meta.get("total_mat", ""), bold=True,
              bg=COLOR_TOTAL, align_h="center", size=10)
    _set_cell(ws_mat, r, 6, "", bg=COLOR_TOTAL)
    _set_cell(ws_mat, r, 7, "", bg=COLOR_TOTAL)
    ws_mat.row_dimensions[r].height = 16

    ws_mat.freeze_panes = ws_mat[f"A{r - len(mat)}"]

    # ═══════════════════════════════════════════════════════════════════════
    # HOJA 3: RESUMEN
    # ═══════════════════════════════════════════════════════════════════════
    ws_res = wb.create_sheet("RESUMEN")
    ws_res.sheet_view.showGridLines = False
    ws_res.column_dimensions["A"].width = 28
    ws_res.column_dimensions["B"].width = 40

    r = 1
    ws_res.merge_cells(f"A{r}:B{r}")
    c = ws_res.cell(row=r, column=1, value=f"RESUMEN  —  {titulo}")
    c.font = Font(bold=True, color=BLANCO, size=13, name="Calibri")
    c.fill = _fill(COLOR_TITULO)
    c.alignment = _align(h="center")
    ws_res.row_dimensions[r].height = 22
    r += 2

    resumen_items = [
        ("Planilla ID",        id_planilla),
        ("Empresa",            meta.get("empresa",       "—")),
        ("OdM / Grafo",        meta.get("odm",           "—")),
        ("Ticket / PDL",       meta.get("ticket",        "—")),
        ("Zona / CD",          meta.get("zona_cd",       "—")),
        ("Barrio / Vereda",    meta.get("barrio",        "—")),
        ("Codigo Matriz E1",   meta.get("codigo_matriz", "—")),
        ("Fecha Ejecucion",    meta.get("fecha_ejecucion","—")),
        ("",                   ""),
        ("Total Mano de Obra", meta.get("total_mo",  "—")),
        ("Total Materiales",   meta.get("total_mat", "—")),
        ("",                   ""),
        ("Actividades MO",     len(mo)),
        ("Items de Material",  len(mat)),
    ]
    for label, val in resumen_items:
        if label == "":
            r += 1
            continue
        bold_row = label in ("Total Mano de Obra", "Total Materiales")
        bg_row = COLOR_TOTAL if bold_row else (COLOR_SUBHEADER if "Total" not in label else BLANCO)
        _set_cell(ws_res, r, 1, label, bold=True,   bg=bg_row, align_h="right")
        _set_cell(ws_res, r, 2, val,   bold=bold_row, bg=BLANCO, align_h="left")
        ws_res.row_dimensions[r].height = 15
        r += 1

    # ─────────────────────────────────────────────────────────────────────
    wb.save(ruta_salida)
    print(f"[OK] Archivo guardado: {ruta_salida}")
    print(f"     Tamaño: {os.path.getsize(ruta_salida):,} bytes")
    print(f"     MO: {len(mo)} actividades  |  Materiales: {len(mat)} items")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("ERROR: Debes proporcionar el ID de la planilla.")
        print("Ejemplo: python balance_liquidar.py 17079")
        sys.exit(1)

    id_planilla = sys.argv[1].strip()
    if not id_planilla.isdigit():
        print(f"ERROR: El ID debe ser número. Recibido: '{id_planilla}'")
        sys.exit(1)

    print(f"[*] Procesando planilla {id_planilla}...")

    html  = descargar_html(id_planilla)
    datos = extraer_datos(html)

    # Carpeta por ID: DATA_DIR / 17082 / balance_liquidar_17082.xlsx
    base_dir   = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
    carpeta_id = os.path.join(base_dir, id_planilla)
    os.makedirs(carpeta_id, exist_ok=True)

    nombre      = f"balance_liquidar_{id_planilla}.xlsx"
    ruta_salida = os.path.join(carpeta_id, nombre)

    try:
        crear_excel(datos, id_planilla, ruta_salida)
    except PermissionError:
        ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre  = f"balance_liquidar_{id_planilla}_{ts}.xlsx"
        ruta_salida = os.path.join(carpeta_id, nombre)
        print(f"[!] Archivo anterior en uso, guardando como: {nombre}")
        crear_excel(datos, id_planilla, ruta_salida)

    print()
    print("  Hojas generadas:")
    print("    MANO DE OBRA  - actividades: codigo, descripcion, unidad, cantidad")
    print("    MATERIALES    - codigo, descripcion, I N / I RZ / R CH / R RZ / M A")
    print("    RESUMEN       - totales y datos generales de la planilla")


if __name__ == "__main__":
    main()
