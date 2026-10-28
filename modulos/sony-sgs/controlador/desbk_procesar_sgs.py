#!/usr/bin/env python3
"""
Agrega hojas 'balance_SGS' y 'cruce_materiales' al balance_liquidar_{id}.xlsx.
Uso: python procesar_sgs.py <id_planilla> <ruta_sgs_excel> <df_sgs>

Hoja balance_SGS:
    Nodo | Codigo Material | Cantidad SGS (balance neto)
    Balance = Despacho - Devolucion por (nodo, codigo)

Hoja cruce_materiales:
    Nodo | Codigo Material | Cantidad Sony | Cantidad SGS | Balance Absoluto
    Verde (#00B050) si Sony == SGS, Rojo (#FF0000) si difieren.
"""

import sys
import os
import re
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from collections import defaultdict


# ── Normalizacion de nodos ──────────────────────────────────────────────────────

def normalizar_nodo(texto):
    """
    Extrae el numero de nodo de variantes textuales.
    Soporta: nodo1, nodo 1, NODO1, N1, N 1, ND1, ND 1, node1, 1,
             nodo1A, N1A, ND1A, 1A, etc. (sufijo letra incluido)
    Retorna string con el identificador (ej: '1', '1A', '2B') o fallback.
    """
    if texto is None:
        return None
    t = str(texto).strip()
    # Patron: prefijo + numero + sufijo letra opcional
    m = re.search(r'(?:nodo|node|nd?)\s*0*(\d+[A-Za-z]*)', t, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    # Solo numero (con sufijo letra opcional)
    m = re.fullmatch(r'0*(\d+[A-Za-z]*)', t, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return t  # fallback


def _nodo_key(n):
    """Clave de ordenamiento para nodos: '1' < '1A' < '1B' < '2' < '2A'."""
    m = re.match(r'^0*(\d+)([A-Za-z]*)$', str(n))
    if m:
        return (int(m.group(1)), m.group(2).upper())
    return (float('inf'), str(n))


def _extraer_df_nodo(proyecto_str):
    """
    Extrae (df, nodo) de un string como '[PRO-051208]DF202664259239 nodo1'.
    df  -> 'DF202664259239'
    nodo -> '1'
    """
    if not proyecto_str:
        return None, None
    s = str(proyecto_str)
    df_m = re.search(r'(DF\d+)', s, re.IGNORECASE)
    df = df_m.group(1).upper() if df_m else None
    nodo_m = re.search(r'(?:nodo|node|nd?)\s*0*(\d+[A-Za-z]*)', s, re.IGNORECASE)
    nodo = nodo_m.group(1).upper() if nodo_m else None
    return df, nodo


# ── Lectura SGS ────────────────────────────────────────────────────────────────

def leer_sgs(ruta_sgs):
    """
    Lee el Excel SGS y calcula balance neto por (df, nodo, codigo_material).

    Estructura esperada del Excel (columnas 0-based):
        0: Materiales/Bandeja/Proyecto  -> '[PRO-XXXXXX]DFXXXXXXXXXXXX nodoY'
                                           o 'BAN-XXXX' (identificador de bandeja)
        1: Tipo                         -> 'Despacho' | 'Devolucion' (forward-fill)
        6: Codigo Material
        7: Cantidad Entregada

    Balance = sum(Despacho) - sum(Devolucion)
    Las filas BAN-XXXX actualizan el contexto de bandeja sin pisar el proyecto.

    Retorna:
        balance : dict  {(df, nodo, codigo): float}
        dfs     : list  [str]  DFs unicos encontrados en el archivo
        detalle : dict  {(df, nodo, codigo): {"despachos": float,
                                               "devoluciones": float,
                                               "bandejas": set}}
    """
    wb = openpyxl.load_workbook(ruta_sgs, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    _BAN_PAT = re.compile(r'(BAN-[\w\d]+)', re.IGNORECASE)

    # ── Pasada 1: forward-fill de bandejas ────────────────────────────────────
    # Busca BAN-XXXX en CUALQUIER columna de cada fila (no asume posicion fija).
    # Cuando una fila tiene BAN, lo registra; las filas siguientes sin BAN
    # heredan la ultima bandeja vista.
    bandeja_por_fila = []
    last_ban = None
    for row in rows[1:]:
        ban_en_fila = None
        for cell in row:
            if cell is not None:
                m = _BAN_PAT.search(str(cell))
                if m:
                    ban_en_fila = m.group(1).upper()
                    break
        if ban_en_fila:
            last_ban = ban_en_fila
        bandeja_por_fila.append(last_ban)

    # ── Pasada 2: calcular balance y detalle ──────────────────────────────────
    balance  = defaultdict(float)
    # bandejas es lista (no set) para preservar orden de aparicion en el SGS
    detalle  = defaultdict(lambda: {"despachos": 0.0, "devoluciones": 0.0, "bandejas": []})
    dfs_encontrados = set()
    last_proyecto = None
    last_tipo     = None

    for i, row in enumerate(rows[1:]):
        col0 = row[0]

        if col0 is not None:
            col0_str = str(col0).strip()
            # Actualizar proyecto solo si la celda NO es un identificador BAN
            if not _BAN_PAT.search(col0_str):
                last_proyecto = col0

        tipo = row[1] if row[1] is not None else last_tipo
        if row[1] is not None:
            last_tipo = row[1]

        codigo = str(row[6]).strip() if row[6] is not None else None
        try:
            cantidad = float(row[7]) if row[7] is not None else 0.0
        except (TypeError, ValueError):
            cantidad = 0.0

        if not codigo or last_proyecto is None:
            continue

        df, nodo = _extraer_df_nodo(last_proyecto)
        if df is None or nodo is None:
            continue

        dfs_encontrados.add(df)
        tipo_s  = str(tipo).strip().lower() if tipo else ""
        key     = (df, nodo, codigo)
        bandeja = bandeja_por_fila[i]

        if bandeja and bandeja not in detalle[key]["bandejas"]:
            detalle[key]["bandejas"].append(bandeja)

        if "despacho" in tipo_s:
            balance[key] += cantidad
            detalle[key]["despachos"] += cantidad
        elif "devoluci" in tipo_s:
            balance[key] -= cantidad
            detalle[key]["devoluciones"] += cantidad

    return dict(balance), sorted(dfs_encontrados), dict(detalle)


# ── Lectura materiales Sony desde Excel existente ──────────────────────────────

def leer_sony_materiales(ruta_excel):
    """
    Lee la hoja MATERIALES del balance_liquidar_{id}.xlsx.
    Identifica filas de datos por columna A = entero positivo (numero de fila).
    Columnas: A=# | B=Nodo | C=Codigo | D=Material | E=Cantidad | F=Tipo | G=Observacion

    Retorna lista de dicts: {nodo, codigo, desc, cantidad, tipo}
    """
    wb = openpyxl.load_workbook(ruta_excel, read_only=True, data_only=True)
    ws = wb["MATERIALES"]
    materiales = []

    for row in ws.iter_rows(values_only=True):
        num = row[0]
        # Fila de dato: columna A es entero positivo
        if not isinstance(num, (int, float)):
            continue
        if float(num) != int(float(num)) or int(float(num)) < 1:
            continue

        nodo   = normalizar_nodo(row[1]) if row[1] is not None else ""
        codigo = str(row[2]).strip() if row[2] is not None else ""
        desc   = str(row[3]).strip() if row[3] is not None else ""
        cant   = row[4]
        tipo   = str(row[5]).strip() if len(row) > 5 and row[5] is not None else ""

        if cant is None:
            cant = 0
        elif isinstance(cant, float) and cant == int(cant):
            cant = int(cant)

        if codigo:
            materiales.append({
                "nodo":     nodo,
                "codigo":   codigo,
                "desc":     desc,
                "cantidad": cant,
                "tipo":     tipo,
            })

    wb.close()
    return materiales


# ── Helpers de estilo Excel ────────────────────────────────────────────────────

def _border():
    s = Side(style="thin")
    return Border(left=s, right=s, top=s, bottom=s)

def _fill(hex_color):
    return PatternFill("solid", fgColor=hex_color)

def _header_cell(ws, r, c, val):
    cell = ws.cell(row=r, column=c, value=val)
    cell.font      = Font(bold=True, color="FFFFFF", size=10, name="Calibri")
    cell.fill      = _fill("1F3864")
    cell.alignment = Alignment(horizontal="center", vertical="center")
    cell.border    = _border()

def _data_cell(ws, r, c, val, bg=None, align_h="center"):
    cell = ws.cell(row=r, column=c, value=val)
    cell.font      = Font(bold=False, color="000000", size=9, name="Calibri")
    cell.alignment = Alignment(horizontal=align_h, vertical="center")
    cell.border    = _border()
    if bg:
        cell.fill = _fill(bg)


# ── Helpers compartidos de balance_SGS ────────────────────────────────────────

_C_NODO   = "1F3864"
_C_SUBHDR = "2E75B6"
_C_TOTAL  = "FFE699"
_C_BP = ("C6EFCE", "375623")
_C_BN = ("FFC7CE", "9C0006")
_C_BZ = ("D9D9D9", "595959")


def _neat(v):
    return int(v) if isinstance(v, float) and v == int(v) else v


def _bal_style(val):
    if   val > 0: return _C_BP
    elif val < 0: return _C_BN
    else:         return _C_BZ


def _bal_cell(ws, r, val_num, val_neat):
    bg_c, ft_c = _bal_style(val_num)
    c = ws.cell(row=r, column=5, value=val_neat)
    c.font      = Font(bold=True, color=ft_c, size=9, name="Calibri")
    c.fill      = _fill(bg_c)
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.border    = _border()


def _escribir_balance_sgs(ws_sgs, balance_sgs, df_sgs, detalle_sgs):
    """
    Escribe la hoja balance_SGS en ws_sgs ya creado.
    Retorna el total de filas de materiales escritas.
    """
    ws_sgs.sheet_view.showGridLines = False
    for col, w in zip("ABCDE", [20, 38, 14, 14, 14]):
        ws_sgs.column_dimensions[col].width = w

    sgs_lookup = {
        (nodo, cod): bal
        for (df, nodo, cod), bal in balance_sgs.items()
        if df == df_sgs
    }
    nodos_data = defaultdict(list)
    for (nodo, cod), bal in sgs_lookup.items():
        det = detalle_sgs.get((df_sgs, nodo, cod), {})
        nodos_data[nodo].append({
            "cod":  cod,
            "desp": det.get("despachos",    0.0),
            "dev":  det.get("devoluciones", 0.0),
            "bal":  bal,
            "bans": det.get("bandejas", []),
        })
    nodos_sorted = sorted(nodos_data.keys(), key=_nodo_key)

    r = 1
    total = 0
    for nodo in nodos_sorted:
        items = nodos_data[nodo]
        ws_sgs.merge_cells(f"A{r}:E{r}")
        cn = ws_sgs.cell(row=r, column=1, value=f"NODO {nodo}")
        cn.font      = Font(bold=True, color="FFFFFF", size=11, name="Calibri")
        cn.fill      = _fill(_C_NODO)
        cn.alignment = Alignment(horizontal="left", vertical="center")
        cn.border    = _border()
        for ci in range(2, 6):
            cc = ws_sgs.cell(row=r, column=ci)
            cc.fill = _fill(_C_NODO); cc.border = _border()
        ws_sgs.row_dimensions[r].height = 18
        r += 1
        for ci, h in enumerate(
            ["Codigo Material", "Bandejas Asociadas", "Despachos", "Devoluciones", "Balance Neto"], 1
        ):
            ch = ws_sgs.cell(row=r, column=ci, value=h)
            ch.font      = Font(bold=True, color="FFFFFF", size=9, name="Calibri")
            ch.fill      = _fill(_C_SUBHDR)
            ch.alignment = Alignment(horizontal="center", vertical="center")
            ch.border    = _border()
        ws_sgs.row_dimensions[r].height = 14
        r += 1
        tot_desp = tot_dev = tot_bal = 0.0
        for item in items:
            bans_s = ", ".join(item["bans"]) if item["bans"] else "\u2014"
            _data_cell(ws_sgs, r, 1, item["cod"],          align_h="left")
            _data_cell(ws_sgs, r, 2, bans_s,               align_h="left")
            _data_cell(ws_sgs, r, 3, _neat(item["desp"]))
            _data_cell(ws_sgs, r, 4, _neat(item["dev"]))
            _bal_cell(ws_sgs, r, item["bal"], _neat(item["bal"]))
            ws_sgs.row_dimensions[r].height = 14
            r += 1; total += 1
            tot_desp += item["desp"]
            tot_dev  += item["dev"]
            tot_bal  += item["bal"]
        ws_sgs.merge_cells(f"A{r}:B{r}")
        ct = ws_sgs.cell(row=r, column=1, value=f"TOTAL NODO {nodo}")
        ct.font = Font(bold=True, color="000000", size=9, name="Calibri")
        ct.fill = _fill(_C_TOTAL)
        ct.alignment = Alignment(horizontal="right", vertical="center")
        ct.border = _border()
        ws_sgs.cell(row=r, column=2).fill   = _fill(_C_TOTAL)
        ws_sgs.cell(row=r, column=2).border = _border()
        for ci, val in zip([3, 4], [_neat(tot_desp), _neat(tot_dev)]):
            cx = ws_sgs.cell(row=r, column=ci, value=val)
            cx.font = Font(bold=True, color="000000", size=9, name="Calibri")
            cx.fill = _fill(_C_TOTAL)
            cx.alignment = Alignment(horizontal="center", vertical="center")
            cx.border = _border()
        _bal_cell(ws_sgs, r, tot_bal, _neat(tot_bal))
        ws_sgs.cell(row=r, column=5).font = Font(
            bold=True, color=_bal_style(tot_bal)[1], size=9, name="Calibri"
        )
        ws_sgs.cell(row=r, column=5).fill = _fill(_bal_style(tot_bal)[0])
        ws_sgs.row_dimensions[r].height = 14
        r += 2  # fila total + separador entre nodos
    return total


def crear_balance_sgs(ruta_salida, balance_sgs, df_sgs, detalle_sgs=None):
    """
    Genera un Excel nuevo con solo la hoja balance_SGS.
    Retorna el numero de filas de materiales escritas.
    """
    if detalle_sgs is None:
        detalle_sgs = {}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "balance_SGS"
    total = _escribir_balance_sgs(ws, balance_sgs, df_sgs, detalle_sgs)
    wb.save(ruta_salida)
    return total


# ── Generacion de hojas ────────────────────────────────────────────────────────

def agregar_hojas_sgs(ruta_excel, balance_sgs, materiales_sony, df_sgs, detalle_sgs=None):
    """
    Abre el Excel existente y agrega (o reemplaza) las hojas:
        - balance_SGS           (agrupado por nodo, con BAN / Despachos / Dev / Balance)
        - cruce_materiales_xiomara  (todos; chatarra=amarillo, I N/M A verde/rojo)
        - cruce_materiales_sergio   (sin chatarra; normaliza sufijo C)

    Retorna (n_sgs, n_xio, n_ser): filas de materiales en cada hoja.
    """
    if detalle_sgs is None:
        detalle_sgs = {}

    wb = openpyxl.load_workbook(ruta_excel)

    # Eliminar hojas previas (incluye nombre antiguo por compatibilidad)
    for nombre in ("balance_SGS", "cruce_materiales",
                   "cruce_materiales_xiomara", "cruce_materiales_sergio"):
        if nombre in wb.sheetnames:
            del wb[nombre]

    COLOR_VERDE    = "00B050"
    COLOR_ROJO     = "FF0000"
    COLOR_AMARILLO = "FFC000"
    CHATARRA_TIPOS = {"I RZ", "R CH", "R RZ"}

    def _escribir_hoja_cruce(ws, all_keys, sony_qty, sgs_qty, get_bg):
        for col, w in zip("ABCDE", [10, 22, 16, 16, 20]):
            ws.column_dimensions[col].width = w
        for ci, h in enumerate(
            ["Nodo", "Codigo Material", "Cantidad Sony", "Cantidad SGS", "Balance Absoluto"], 1
        ):
            _header_cell(ws, 1, ci, h)
        ws.row_dimensions[1].height = 16
        for r, (nodo, cod) in enumerate(all_keys, 2):
            c_sony = _neat(sony_qty.get((nodo, cod), 0.0))
            c_sgs  = _neat(sgs_qty.get((nodo, cod),  0.0))
            c_diff = _neat(abs(float(c_sony) - float(c_sgs)))
            bg = get_bg(nodo, cod, c_sony, c_sgs)
            for ci, val in enumerate([nodo, cod, c_sony, c_sgs, c_diff], 1):
                _data_cell(ws, r, ci, val, bg=bg)
            ws.row_dimensions[r].height = 14

    # ── Hoja 1: balance_SGS ───────────────────────────────────────────────────
    ws_sgs = wb.create_sheet("balance_SGS")
    total_mat_rows = _escribir_balance_sgs(ws_sgs, balance_sgs, df_sgs, detalle_sgs)

    # Lookup SGS filtrado al DF (necesario para hojas de cruce)
    sgs_lookup = {
        (nodo, cod): bal
        for (df, nodo, cod), bal in balance_sgs.items()
        if df == df_sgs
    }

    # ══════════════════════════════════════════════════════════════════════════
    # Hoja 2: cruce_materiales_xiomara
    # REGLAS EXCLUSIVAS — NO comparte lógica con sergio:
    #   · Cruce LITERAL de codigos: 310421 ≠ 310421C, siempre filas separadas.
    #   · Chatarra (I RZ / R CH / R RZ) → amarillo.
    #   · I N / M A: verde si coinciden, rojo si difieren.
    #   · NUNCA se aplica normalización de sufijo C en este bloque.
    # ══════════════════════════════════════════════════════════════════════════
    ws_xio = wb.create_sheet("cruce_materiales_xiomara")
    ws_xio.sheet_view.showGridLines = False

    # Lookup Sony para xiomara: codigos originales sin ninguna modificacion
    xio_sony_qty  = {}   # {(nodo, cod): float}
    xio_sony_tipo = {}   # {(nodo, cod): str}   para determinar chatarra
    for m in materiales_sony:
        cant = m["cantidad"]
        if isinstance(cant, str):
            try:
                cant = float(cant)
            except ValueError:
                cant = 0.0
        key = (m["nodo"], m["codigo"])   # codigo original, sin tocar
        xio_sony_qty[key]  = xio_sony_qty.get(key, 0.0) + float(cant)
        if key not in xio_sony_tipo:
            xio_sony_tipo[key] = m.get("tipo", "")

    # Lookup SGS para xiomara: copia independiente, sin modificar
    xio_sgs_qty = dict(sgs_lookup)

    xio_all_keys = sorted(
        set(xio_sgs_qty.keys()) | set(xio_sony_qty.keys()),
        key=lambda x: (_nodo_key(x[0]), x[1])
    )

    def bg_xiomara(nodo, cod, c_sony, c_sgs):
        tipo = xio_sony_tipo.get((nodo, cod), "")
        if tipo in CHATARRA_TIPOS:
            return COLOR_AMARILLO
        return COLOR_VERDE if float(c_sony) == float(c_sgs) else COLOR_ROJO

    _escribir_hoja_cruce(ws_xio, xio_all_keys, xio_sony_qty, xio_sgs_qty, bg_xiomara)

    # ══════════════════════════════════════════════════════════════════════════
    # Hoja 3: cruce_materiales_sergio
    # REGLAS EXCLUSIVAS — NO comparte lógica con xiomara:
    #   · Sin chatarra.
    #   · Normalización sufijo C SOLO AQUI: 310421 (Sony) + 310421C (SGS)
    #     → una sola fila bajo 310421C. La fila sin C se omite.
    #   · Verde si cantidades coinciden, rojo si difieren.
    # ══════════════════════════════════════════════════════════════════════════
    ws_ser = wb.create_sheet("cruce_materiales_sergio")
    ws_ser.sheet_view.showGridLines = False

    # Snapshot inmutable de SGS para la verificacion de sufijo C en sergio
    ser_sgs_snapshot = dict(sgs_lookup)

    # Claves chatarra de Sony (y su variante +C) para excluirlas del SGS de sergio
    chatarra_keys = set()
    for m in materiales_sony:
        if m.get("tipo", "") in CHATARRA_TIPOS:
            chatarra_keys.add((m["nodo"], m["codigo"]))
            chatarra_keys.add((m["nodo"], m["codigo"] + "C"))

    # Sony sergio: excluir chatarra; normalizar sufijo C usando el snapshot
    ser_sony_qty = {}
    for m in materiales_sony:
        if m.get("tipo", "") in CHATARRA_TIPOS:
            continue
        nodo = m["nodo"]
        cod  = m["codigo"]
        cant = m["cantidad"]
        if isinstance(cant, str):
            try:
                cant = float(cant)
            except ValueError:
                cant = 0.0
        # Normalización C: si SGS tiene cod+"C", ese es el identificador unico
        if not cod.endswith("C") and (nodo, cod + "C") in ser_sgs_snapshot:
            cod = cod + "C"
        ser_sony_qty[( nodo, cod)] = ser_sony_qty.get((nodo, cod), 0.0) + float(cant)

    # SGS sergio: excluir chatarra
    sgs_qty_ser = {k: v for k, v in ser_sgs_snapshot.items() if k not in chatarra_keys}

    ser_all_keys = sorted(
        set(sgs_qty_ser.keys()) | set(ser_sony_qty.keys()),
        key=lambda x: (_nodo_key(x[0]), x[1])
    )

    def bg_sergio(nodo, cod, c_sony, c_sgs):
        return COLOR_VERDE if float(c_sony) == float(c_sgs) else COLOR_ROJO

    _escribir_hoja_cruce(ws_ser, ser_all_keys, ser_sony_qty, sgs_qty_ser, bg_sergio)

    try:
        wb.save(ruta_excel)
    except PermissionError:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ruta_excel = ruta_excel.replace(".xlsx", f"_{ts}.xlsx")
        print(f"[!] Archivo en uso, guardando como: {os.path.basename(ruta_excel)}")
        wb.save(ruta_excel)
    return total_mat_rows, len(xio_all_keys), len(ser_all_keys)


# ── Merge de multiples archivos SGS ───────────────────────────────────────────

def leer_sgs_multiples(rutas):
    """
    Combina el balance y detalle de varios archivos SGS (uno por nodo).
    Retorna (balance_combinado, dfs_encontrados, detalle_combinado).
    """
    balance_total = defaultdict(float)
    detalle_total = defaultdict(lambda: {"despachos": 0.0, "devoluciones": 0.0, "bandejas": []})
    dfs_total = set()
    for ruta in rutas:
        bal, dfs, det = leer_sgs(ruta)
        dfs_total.update(dfs)
        for key, val in bal.items():
            balance_total[key] += val
        for key, d in det.items():
            detalle_total[key]["despachos"]   += d["despachos"]
            detalle_total[key]["devoluciones"] += d["devoluciones"]
            for ban in d["bandejas"]:
                if ban not in detalle_total[key]["bandejas"]:
                    detalle_total[key]["bandejas"].append(ban)
    return dict(balance_total), sorted(dfs_total), dict(detalle_total)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    # Nuevo formato: procesar_sgs.py <id_planilla> <df_sgs> <ruta1> [ruta2] ...
    if len(sys.argv) < 4:
        print(__doc__)
        print("ERROR: Argumentos insuficientes.")
        print("Uso: python procesar_sgs.py <id_planilla> <df_sgs> <ruta1> [ruta2] ...")
        sys.exit(1)

    id_planilla = sys.argv[1].strip()
    df_sgs      = sys.argv[2].strip().upper()
    rutas_sgs   = [r.strip() for r in sys.argv[3:]]

    base_dir   = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
    carpeta_id = os.path.join(base_dir, id_planilla)
    ruta_excel = os.path.join(carpeta_id, f"balance_liquidar_{id_planilla}.xlsx")

    for ruta in rutas_sgs:
        if not os.path.isfile(ruta):
            print(f"[ERROR] Archivo SGS no encontrado: {ruta}")
            sys.exit(1)
    if not os.path.isfile(ruta_excel):
        print(f"[ERROR] Excel Sony no encontrado: {ruta_excel}")
        sys.exit(1)

    print(f"[*] Leyendo {len(rutas_sgs)} archivo(s) SGS...")
    for ruta in rutas_sgs:
        print(f"    - {os.path.basename(ruta)}")
    balance_sgs, dfs, detalle_sgs = leer_sgs_multiples(rutas_sgs)
    print(f"    DFs encontrados: {dfs}")

    if df_sgs not in dfs:
        print(f"[ERROR] DF '{df_sgs}' no esta en los archivos SGS.")
        print(f"        Disponibles: {dfs}")
        sys.exit(1)

    print(f"[*] Leyendo materiales Sony: balance_liquidar_{id_planilla}.xlsx")
    materiales_sony = leer_sony_materiales(ruta_excel)
    print(f"    Items Sony: {len(materiales_sony)}")

    n_sgs, n_xio, n_ser = agregar_hojas_sgs(ruta_excel, balance_sgs, materiales_sony, df_sgs, detalle_sgs)
    print(f"[OK] Hoja balance_SGS                : {n_sgs} filas (DF={df_sgs})")
    print(f"[OK] Hoja cruce_materiales_xiomara   : {n_xio} filas")
    print(f"[OK] Hoja cruce_materiales_sergio    : {n_ser} filas")
    print(f"[OK] Archivo actualizado             : {ruta_excel}")


if __name__ == "__main__":
    main()
