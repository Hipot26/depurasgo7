#!/usr/bin/env python3
"""
[PRUEBAS] procesar_sgs.py — Extensión con categorización, validación de territorio y notificaciones.

Cambios respecto a la versión raíz:
  · Validación de territorio: solo Ubate / Zona VII. Cualquier otra zona
    dispara la notificación y detiene el proceso.
  · Categorización:
      - DF presente  → Xiomara (Asociación)
      - Sin DF + ID empieza con 6300  → Daniela  (ref: 17352)
      - Cualquier otro caso → error
  · Notificación: registra accesos no autorizados en DATA_DIR/logs/accesos_no_autorizados.log

Uso:
  Xiomara : python procesar_sgs.py <id> DF202664259239 ruta1.xlsx [ruta2.xlsx ...]
  Daniela : python procesar_sgs.py <id> ruta1.xlsx [ruta2.xlsx ...]
            (id debe empezar con 6300)
"""

import sys
import os
import re
import json
import openpyxl
from datetime import datetime
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from collections import defaultdict


# ── Normalizacion de nodos ─────────────────────────────────────────────────────

def normalizar_nodo(texto):
    if texto is None:
        return None
    t = str(texto).strip()
    m = re.search(r'(?:nodo|node|nd?)\s*0*(\d+[A-Za-z]*)', t, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.fullmatch(r'0*(\d+[A-Za-z]*)', t, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return t


def _nodo_key(n):
    m = re.match(r'^0*(\d+)([A-Za-z]*)$', str(n))
    if m:
        return (int(m.group(1)), m.group(2).upper())
    return (float('inf'), str(n))


def _extraer_df_nodo(proyecto_str):
    if not proyecto_str:
        return None, None
    s = str(proyecto_str)
    # 1. Intentar DF (Xiomara — mayor impacto)
    df_m = re.search(r'(DF\d+)', s, re.IGNORECASE)
    if df_m:
        key = df_m.group(1).upper()
    else:
        # 2. Intentar 6300... (Daniela programado) — prefijo irrelevante (LCL, tausavita, etc.)
        lcl_m = re.search(r'(6300\d+)', s)
        key = f"LCL{lcl_m.group(1)}" if lcl_m else None
    nodo_m = re.search(r'(?:nodo|node|nd?)\s*0*(\d+[A-Za-z]*)', s, re.IGNORECASE)
    nodo = nodo_m.group(1).upper() if nodo_m else None
    return key, nodo


# ── Notificación de acceso no autorizado ───────────────────────────────────────

def notificar_acceso_no_autorizado(id_planilla, zona_detectada, usuario):
    """
    Registra en DATA_DIR/logs/accesos_no_autorizados.log un evento de acceso
    desde una zona no permitida (fuera de Ubate / Zona VII).

    Formato: una línea JSON por evento (JSONL).
    Respeta DATA_DIR para compatibilidad con Railway.
    """
    base_dir = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
    log_dir  = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "accesos_no_autorizados.log")

    evento = {
        "timestamp":    datetime.now().isoformat(timespec="seconds"),
        "id_planilla":  id_planilla,
        "zona_detectada": zona_detectada,
        "usuario":      usuario,
    }

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(evento, ensure_ascii=False) + "\n")

    print(f"[!] ACCESO NO AUTORIZADO registrado en: {log_path}")
    print(f"    Zona detectada : {zona_detectada}")
    print(f"    Usuario        : {usuario}")
    print(f"    Planilla       : {id_planilla}")


def _leer_usuario_config():
    """Lee el campo 'usuario' de config.json (un nivel arriba de /pruebas/)."""
    # config.json vive en la raíz del proyecto, no en /pruebas/
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config.json"
    )
    config_path = os.path.normpath(config_path)
    if not os.path.exists(config_path):
        return "desconocido"
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("usuario", "desconocido").strip() or "desconocido"
    except Exception:
        return "desconocido"


# ── Validación de territorio ───────────────────────────────────────────────────

_ZONAS_PERMITIDAS = [
    re.compile(r'ubate',   re.IGNORECASE),
    re.compile(r'zona\s*v{2}|zona\s*7\b', re.IGNORECASE),
]


def _zona_permitida(zona_str):
    """Retorna True si la zona coincide con Ubate o Zona VII."""
    for pat in _ZONAS_PERMITIDAS:
        if pat.search(zona_str):
            return True
    return False


def _leer_resumen_sin_validar(ruta_excel):
    """Lee zona, LCL y OdM del RESUMEN sin aplicar validación de territorio."""
    wb = openpyxl.load_workbook(ruta_excel, read_only=True, data_only=True)
    zona = lcl = odm = "—"
    if "RESUMEN" in wb.sheetnames:
        for row in wb["RESUMEN"].iter_rows(values_only=True):
            if not row[0]:
                continue
            label = str(row[0]).strip()
            valor = str(row[1]).strip() if row[1] else "—"
            if label == "Zona nombre":
                zona = valor
            elif label == "LCL":
                lcl = valor
            elif label in ("OdM / Grafo",):
                odm = valor
    wb.close()
    return zona, lcl, odm


def validar_territorio(ruta_excel, id_planilla):
    """
    Lee la hoja RESUMEN del balance_liquidar y verifica que la zona sea
    Ubate o Zona VII. Si no lo es, llama a notificar_acceso_no_autorizado
    y termina el proceso.

    Retorna la zona detectada como string si la validación pasa.
    """
    wb = openpyxl.load_workbook(ruta_excel, read_only=True, data_only=True)
    if "RESUMEN" not in wb.sheetnames:
        wb.close()
        print("[ERROR] La hoja RESUMEN no existe en el Excel. No se puede validar el territorio.")
        sys.exit(1)

    ws = wb["RESUMEN"]
    zona_nombre    = "—"
    zona_cd        = "—"
    lcl_valor      = "—"
    odm_valor      = "—"
    for row in ws.iter_rows(values_only=True):
        if not row[0]:
            continue
        label = str(row[0]).strip()
        valor = str(row[1]).strip() if row[1] else "—"
        if label == "Zona nombre":
            zona_nombre = valor
        elif label == "Zona CD":
            zona_cd = valor
        elif label == "LCL":
            lcl_valor = valor
        elif label in ("OdM / Grafo", "OdM / Grafo"):
            odm_valor = valor
    wb.close()

    if _zona_permitida(zona_nombre):
        print(f"[OK] Territorio validado: {zona_nombre} (CD: {zona_cd})")
        return zona_nombre, lcl_valor, odm_valor

    # Zona no identificada o no permitida → notificar y detener
    zona_detectada = zona_nombre if zona_nombre != "—" else zona_cd
    usuario = _leer_usuario_config()
    notificar_acceso_no_autorizado(id_planilla, zona_detectada, usuario)
    print(f"[ERROR] Zona '{zona_detectada}' no está permitida. Solo Ubate y Zona VII.")
    sys.exit(1)


# ── Categorización de planilla ─────────────────────────────────────────────────

CATEGORIA_XIOMARA = "xiomara"
CATEGORIA_DANIELA = "daniela"


def categorizar_planilla(id_planilla, lcl, odm, df_sgs_arg):
    """
    Determina quién procesa la planilla usando los campos del RESUMEN:

      · LCL empieza con '6300'  → DANIELA  (programado)
      · OdM empieza con 'DF'    → XIOMARA  (Asociación)
      · df_sgs_arg presente     → XIOMARA  (override explícito de CLI)
      · ningún caso             → error

    Prioridad: LCL (campo primario) > OdM > argumento CLI.
    Retorna el string de categoría: 'xiomara' o 'daniela'.
    """
    lcl_clean = lcl.strip() if lcl else ""
    odm_clean = odm.strip() if odm else ""

    if lcl_clean.startswith("6300"):
        print(f"[+] Categoría: DANIELA (programado)  —  LCL={lcl_clean}")
        return CATEGORIA_DANIELA

    if odm_clean.upper().startswith("DF") or df_sgs_arg:
        df_ref = df_sgs_arg or odm_clean
        print(f"[+] Categoría: XIOMARA (Asociación)  —  OdM/DF={df_ref}")
        return CATEGORIA_XIOMARA

    print(f"[ERROR] No se pudo categorizar la planilla '{id_planilla}'.")
    print(f"        LCL={lcl_clean!r}  OdM={odm_clean!r}")
    print("        Reglas:")
    print("          · LCL empieza '6300' → Daniela")
    print("          · OdM empieza 'DF'   → Xiomara")
    sys.exit(1)


# ── Lectura SGS ────────────────────────────────────────────────────────────────

def leer_sgs(ruta_sgs):
    wb = openpyxl.load_workbook(ruta_sgs, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    _BAN_PAT = re.compile(r'(BAN-[\w\d]+)', re.IGNORECASE)

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

    balance  = defaultdict(float)
    detalle  = defaultdict(lambda: {"despachos": 0.0, "devoluciones": 0.0, "bandejas": []})
    dfs_encontrados = set()
    last_proyecto = None
    last_tipo     = None

    for i, row in enumerate(rows[1:]):
        col0 = row[0]
        if col0 is not None:
            col0_str = str(col0).strip()
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


def leer_sgs_multiples(rutas):
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


# ── Lectura materiales Sony ────────────────────────────────────────────────────

def leer_sony_materiales(ruta_excel):
    wb = openpyxl.load_workbook(ruta_excel, read_only=True, data_only=True)
    ws = wb["MATERIALES"]
    materiales = []
    for row in ws.iter_rows(values_only=True):
        num = row[0]
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


def _escribir_balance_sgs(ws_sgs, sgs_lookup, detalle_lookup):
    """
    sgs_lookup    : {(nodo, cod): balance_float}
    detalle_lookup: {(nodo, cod): {"despachos": float, "devoluciones": float, "bandejas": [...]}}
    """
    ws_sgs.sheet_view.showGridLines = False
    for col, w in zip("ABCDE", [20, 38, 14, 14, 14]):
        ws_sgs.column_dimensions[col].width = w

    nodos_data = defaultdict(list)
    for (nodo, cod), bal in sgs_lookup.items():
        det = detalle_lookup.get((nodo, cod), {})
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
        r += 2
    return total


# ── Procesamiento Xiomara ──────────────────────────────────────────────────────

def agregar_hojas_xiomara(ruta_excel, balance_sgs, materiales_sony, df_sgs, detalle_sgs=None):
    """
    Flujo Xiomara (Asociación): hoja balance_SGS + cruce_materiales_xiomara + cruce_materiales_sergio.
    Idéntico a la versión raíz — no se modifican las reglas de negocio de Xiomara.
    """
    if detalle_sgs is None:
        detalle_sgs = {}

    wb = openpyxl.load_workbook(ruta_excel)

    for nombre in ("balance_SGS", "cruce_materiales",
                   "cruce_materiales_xiomara", "cruce_materiales_sergio",
                   "cruce_materiales_daniela"):
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

    # Pre-computar lookups filtrados por df_sgs
    sgs_lookup = {
        (nodo, cod): bal
        for (df, nodo, cod), bal in balance_sgs.items()
        if df == df_sgs
    }
    detalle_lookup = {
        (nodo, cod): detalle_sgs.get((df_sgs, nodo, cod), {})
        for (nodo, cod) in sgs_lookup
    }

    # Hoja 1: balance_SGS
    ws_sgs = wb.create_sheet("balance_SGS")
    total_mat_rows = _escribir_balance_sgs(ws_sgs, sgs_lookup, detalle_lookup)

    # Hoja 2: cruce_materiales_xiomara
    ws_xio = wb.create_sheet("cruce_materiales_xiomara")
    ws_xio.sheet_view.showGridLines = False
    xio_sony_qty  = {}
    xio_sony_tipo = {}
    for m in materiales_sony:
        cant = m["cantidad"]
        if isinstance(cant, str):
            try:
                cant = float(cant)
            except ValueError:
                cant = 0.0
        key = (m["nodo"], m["codigo"])
        xio_sony_qty[key]  = xio_sony_qty.get(key, 0.0) + float(cant)
        if key not in xio_sony_tipo:
            xio_sony_tipo[key] = m.get("tipo", "")
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

    # Hoja 3: cruce_materiales_sergio
    ws_ser = wb.create_sheet("cruce_materiales_sergio")
    ws_ser.sheet_view.showGridLines = False
    ser_sgs_snapshot = dict(sgs_lookup)
    chatarra_keys = set()
    for m in materiales_sony:
        if m.get("tipo", "") in CHATARRA_TIPOS:
            chatarra_keys.add((m["nodo"], m["codigo"]))
            chatarra_keys.add((m["nodo"], m["codigo"] + "C"))
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
        if not cod.endswith("C") and (nodo, cod + "C") in ser_sgs_snapshot:
            cod = cod + "C"
        ser_sony_qty[(nodo, cod)] = ser_sony_qty.get((nodo, cod), 0.0) + float(cant)
    sgs_qty_ser = {k: v for k, v in ser_sgs_snapshot.items() if k not in chatarra_keys}
    ser_all_keys = sorted(
        set(sgs_qty_ser.keys()) | set(ser_sony_qty.keys()),
        key=lambda x: (_nodo_key(x[0]), x[1])
    )
    def bg_sergio(nodo, cod, c_sony, c_sgs):
        return COLOR_VERDE if float(c_sony) == float(c_sgs) else COLOR_ROJO
    _escribir_hoja_cruce(ws_ser, ser_all_keys, ser_sony_qty, sgs_qty_ser, bg_sergio)

    _guardar_wb(wb, ruta_excel)
    return total_mat_rows, len(xio_all_keys), len(ser_all_keys)


# ── Procesamiento Daniela ──────────────────────────────────────────────────────

def agregar_hojas_daniela(ruta_excel, balance_sgs, materiales_sony, detalle_sgs=None):
    """
    Flujo Daniela (Programado): mismo esquema que Xiomara pero sin filtro DF.
    Agrega todos los LCL encontrados en los archivos SGS. Crea:
      · balance_SGS              — despachos / devoluciones / balance neto por nodo
      · cruce_materiales_daniela — chatarra y códigos C en amarillo, verde/rojo resto
      · cruce_materiales_sergio  — sin chatarra, con normalización de sufijo C, verde/rojo
    """
    if detalle_sgs is None:
        detalle_sgs = {}

    wb = openpyxl.load_workbook(ruta_excel)

    for nombre in ("balance_SGS", "cruce_materiales",
                   "cruce_materiales_xiomara", "cruce_materiales_sergio",
                   "cruce_materiales_daniela"):
        if nombre in wb.sheetnames:
            del wb[nombre]

    COLOR_VERDE    = "00B050"
    COLOR_ROJO     = "FF0000"
    COLOR_AMARILLO = "FFC000"
    CHATARRA_TIPOS = {"I RZ", "R CH", "R RZ"}

    # ── Lookups agregados (sin filtro DF — suma todos los LCL) ─────────────────
    dan_sgs_lookup = defaultdict(float)
    dan_det_lookup = defaultdict(lambda: {"despachos": 0.0, "devoluciones": 0.0, "bandejas": []})
    for (df, nodo, cod), bal in balance_sgs.items():
        dan_sgs_lookup[(nodo, cod)] += bal
    for (df, nodo, cod), d in detalle_sgs.items():
        dan_det_lookup[(nodo, cod)]["despachos"]   += d["despachos"]
        dan_det_lookup[(nodo, cod)]["devoluciones"] += d["devoluciones"]
        for ban in d["bandejas"]:
            if ban not in dan_det_lookup[(nodo, cod)]["bandejas"]:
                dan_det_lookup[(nodo, cod)]["bandejas"].append(ban)
    dan_sgs_lookup = dict(dan_sgs_lookup)
    dan_det_lookup = dict(dan_det_lookup)

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

    # Hoja 1: balance_SGS
    ws_sgs = wb.create_sheet("balance_SGS")
    total_mat_rows = _escribir_balance_sgs(ws_sgs, dan_sgs_lookup, dan_det_lookup)

    # Hoja 2: cruce_materiales_daniela
    # Chatarra → amarillo; códigos terminados en C → amarillo; resto verde/rojo
    ws_dan = wb.create_sheet("cruce_materiales_daniela")
    ws_dan.sheet_view.showGridLines = False
    dan_sony_qty  = {}
    dan_sony_tipo = {}
    for m in materiales_sony:
        cant = m["cantidad"]
        if isinstance(cant, str):
            try:
                cant = float(cant)
            except ValueError:
                cant = 0.0
        key = (m["nodo"], m["codigo"])
        dan_sony_qty[key]  = dan_sony_qty.get(key, 0.0) + float(cant)
        if key not in dan_sony_tipo:
            dan_sony_tipo[key] = m.get("tipo", "")
    dan_all_keys = sorted(
        set(dan_sgs_lookup.keys()) | set(dan_sony_qty.keys()),
        key=lambda x: (_nodo_key(x[0]), x[1])
    )
    def bg_daniela(nodo, cod, c_sony, c_sgs):
        tipo = dan_sony_tipo.get((nodo, cod), "")
        if tipo in CHATARRA_TIPOS or cod.endswith("C"):
            return COLOR_AMARILLO
        return COLOR_VERDE if float(c_sony) == float(c_sgs) else COLOR_ROJO
    _escribir_hoja_cruce(ws_dan, dan_all_keys, dan_sony_qty, dan_sgs_lookup, bg_daniela)

    # Hoja 3: cruce_materiales_sergio
    # Sin chatarra, normaliza sufijo C, verde/rojo
    ws_ser = wb.create_sheet("cruce_materiales_sergio")
    ws_ser.sheet_view.showGridLines = False
    chatarra_keys = set()
    for m in materiales_sony:
        if m.get("tipo", "") in CHATARRA_TIPOS:
            chatarra_keys.add((m["nodo"], m["codigo"]))
            chatarra_keys.add((m["nodo"], m["codigo"] + "C"))
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
        if not cod.endswith("C") and (nodo, cod + "C") in dan_sgs_lookup:
            cod = cod + "C"
        ser_sony_qty[(nodo, cod)] = ser_sony_qty.get((nodo, cod), 0.0) + float(cant)
    sgs_qty_ser = {k: v for k, v in dan_sgs_lookup.items() if k not in chatarra_keys}
    ser_all_keys = sorted(
        set(sgs_qty_ser.keys()) | set(ser_sony_qty.keys()),
        key=lambda x: (_nodo_key(x[0]), x[1])
    )
    def bg_sergio(nodo, cod, c_sony, c_sgs):
        return COLOR_VERDE if float(c_sony) == float(c_sgs) else COLOR_ROJO
    _escribir_hoja_cruce(ws_ser, ser_all_keys, ser_sony_qty, sgs_qty_ser, bg_sergio)

    _guardar_wb(wb, ruta_excel)
    return total_mat_rows, len(dan_all_keys), len(ser_all_keys)


# ── Guardar con fallback por PermissionError ───────────────────────────────────

def _guardar_wb(wb, ruta):
    try:
        wb.save(ruta)
    except PermissionError:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ruta = ruta.replace(".xlsx", f"_{ts}.xlsx")
        print(f"[!] Archivo en uso, guardando como: {os.path.basename(ruta)}")
        wb.save(ruta)


# ── Main ───────────────────────────────────────────────────────────────────────

def _parsear_args(argv):
    """
    Parsea los argumentos de forma flexible:
      · Si argv[2] empieza con 'DF' (case-insensitive) → es df_sgs; rutas desde argv[3]
      · Si no → df_sgs = ""; rutas desde argv[2]
    Retorna (id_planilla, df_sgs, rutas_sgs)
    """
    if len(argv) < 3:
        return None, None, None
    id_planilla = argv[1].strip()
    if argv[2].strip().upper().startswith("DF"):
        df_sgs    = argv[2].strip().upper()
        rutas_sgs = [r.strip() for r in argv[3:]]
    else:
        df_sgs    = ""
        rutas_sgs = [r.strip() for r in argv[2:]]
    return id_planilla, df_sgs, rutas_sgs


def main():
    id_planilla, df_sgs, rutas_sgs = _parsear_args(sys.argv)

    if id_planilla is None or not rutas_sgs:
        print(__doc__)
        print("ERROR: Argumentos insuficientes.")
        print("Uso Xiomara : python procesar_sgs.py <id> DF202664... ruta1.xlsx [ruta2.xlsx ...]")
        print("Uso Daniela : python procesar_sgs.py <id>          ruta1.xlsx [ruta2.xlsx ...]")
        print("  (categoría se detecta automáticamente: LCL 6300... → Daniela / OdM DF... → Xiomara)")
        sys.exit(1)

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

    # ── 1. Validar territorio + leer LCL y OdM del RESUMEN ───────────────────
    # TODO: validador desactivado temporalmente — revisar extracción zona_nombre
    # _zona, lcl, odm = validar_territorio(ruta_excel, id_planilla)
    _zona, lcl, odm = _leer_resumen_sin_validar(ruta_excel)

    # ── 2. Categorizar: LCL empieza '6300' → Daniela; OdM empieza 'DF' → Xiomara
    categoria = categorizar_planilla(id_planilla, lcl, odm, df_sgs)

    # ── 3. Leer archivos SGS ───────────────────────────────────────────────────
    print(f"[*] Leyendo {len(rutas_sgs)} archivo(s) SGS...")
    for ruta in rutas_sgs:
        print(f"    - {os.path.basename(ruta)}")
    balance_sgs, dfs, detalle_sgs = leer_sgs_multiples(rutas_sgs)
    print(f"    DFs encontrados: {dfs}")

    # ── 4. Leer materiales Sony ────────────────────────────────────────────────
    print(f"[*] Leyendo materiales Sony: balance_liquidar_{id_planilla}.xlsx")
    materiales_sony = leer_sony_materiales(ruta_excel)
    print(f"    Items Sony: {len(materiales_sony)}")

    # ── 5. Procesar según categoría ───────────────────────────────────────────
    if categoria == CATEGORIA_XIOMARA:
        # df_sgs puede venir del CLI o del OdM extraído del RESUMEN
        df_efectivo = df_sgs or odm
        if df_efectivo not in dfs:
            print(f"[ERROR] DF '{df_efectivo}' no está en los archivos SGS.")
            print(f"        Disponibles: {dfs}")
            sys.exit(1)
        n_sgs, n_xio, n_ser = agregar_hojas_xiomara(
            ruta_excel, balance_sgs, materiales_sony, df_efectivo, detalle_sgs
        )
        print(f"[OK] Hoja balance_SGS                : {n_sgs} filas (DF={df_efectivo})")
        print(f"[OK] Hoja cruce_materiales_xiomara   : {n_xio} filas")
        print(f"[OK] Hoja cruce_materiales_sergio    : {n_ser} filas")

    elif categoria == CATEGORIA_DANIELA:
        n_sgs, n_dan, n_ser = agregar_hojas_daniela(
            ruta_excel, balance_sgs, materiales_sony, detalle_sgs
        )
        print(f"[OK] Hoja balance_SGS                : {n_sgs} filas")
        print(f"[OK] Hoja cruce_materiales_daniela   : {n_dan} filas")
        print(f"[OK] Hoja cruce_materiales_sergio    : {n_ser} filas")

    print(f"[OK] Archivo actualizado             : {ruta_excel}")


if __name__ == "__main__":
    main()
