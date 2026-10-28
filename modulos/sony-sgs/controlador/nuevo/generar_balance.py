"""
Generador de balance SGS en Excel.
Cruza: bandejas (almacén) vs project.material (liquidación confirmada por usuario).
"""
import io
from collections import defaultdict
from datetime import datetime

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from sgs_client import parsear_producto, nombre_campo

# ── Colores ───────────────────────────────────────────────────────────────────
C_TITULO  = "1F3864"   # azul oscuro  — titulo principal
C_NODO    = "2E75B6"   # azul         — header nodo
C_COL_HDR = "D6E4F0"   # azul claro   — columnas
C_PAR     = "F2F2F2"   # gris         — fila par
C_TOTAL   = "FFE699"   # amarillo     — subtotales
C_VERDE   = "C6EFCE"   # verde        — OK (neto == liquidado)
C_ROJO    = "FFC7CE"   # rojo         — discrepancia
C_AMBAR   = "FFEB9C"   # amarillo     — pendiente sin confirmar
C_DEVOL   = "DEEBF7"   # azul claro   — devolución completa (no liquidar)
C_META    = "EAF2FF"   # azul claro   — meta datos
BLANCO    = "FFFFFF"


def _border(style="thin"):
    s = Side(style=style)
    return Border(left=s, right=s, top=s, bottom=s)

def _fill(hex_color):
    return PatternFill("solid", fgColor=hex_color)

def _font(bold=False, color="000000", size=10, italic=False):
    return Font(bold=bold, color=color, size=size, name="Calibri", italic=italic)

def _align(h="left", v="center", wrap=False):
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)

def _cell(ws, row, col, value="", bold=False, bg=None, fg="000000",
          align_h="left", size=10, wrap=False, border=True, italic=False,
          num_fmt=None, merge_to=None):
    cell = ws.cell(row=row, column=col, value=value)
    cell.font      = _font(bold=bold, color=fg, size=size, italic=italic)
    cell.alignment = _align(h=align_h, wrap=wrap)
    if bg:
        cell.fill = _fill(bg)
    if border:
        cell.border = _border()
    if num_fmt:
        cell.number_format = num_fmt
    if merge_to:
        ws.merge_cells(start_row=row, start_column=col,
                       end_row=row,   end_column=merge_to)
    return cell

def _rh(ws, row, h):
    ws.row_dimensions[row].height = h


# ── Columnas ──────────────────────────────────────────────────────────────────
# A   B        C            D        E        F       G          H         I       J
# #   Codigo   Descripcion  Proyect  Bandejas DevAlm  NeotoAlm   Consumido Pend    Estado
C_NUM  = 1
C_COD  = 2
C_DESC = 3
C_PROY = 4
C_TRAY = 5   # total bandejas deliver
C_DALM = 6   # devoluciones almacen = tray - desp_neto
C_NETO = 7   # neto almacen = desp_neto
C_CONS = 8   # consumido = advance (lo que liquida el usuario)
C_PEND = 9   # pendiente = neto - consumido
C_EST  = 10  # estado
NCOLS  = 10

NUM_FMT = "#,##0.##"


# ── Agregacion bandejas por nodo ──────────────────────────────────────────────

def _agg_bandejas(nodo: dict) -> dict:
    """
    Agrega bandejas por codigo DOM.
    Retorna {cod: {despachado, devuelto, ban_delivers, ban_devs}}
      despachado  = sum qty de bandejas tipo deliver
      devuelto    = sum qty de bandejas tipo devolution
      ban_delivers= [(ban_name, qty), ...] — bandejas que despacharon
      ban_devs    = [(ban_name, qty), ...] — bandejas que devolvieron
    """
    agg = defaultdict(lambda: {
        "despachado": 0.0, "devuelto": 0.0,
        "ban_delivers": [], "ban_devs": [],
    })
    for ban in nodo.get("bandejas", []):
        es_dev  = ban.get("type", "deliver") == "devolution"
        ban_nom = ban.get("name", "")
        for mat in ban.get("materiales", []):
            cod, _, _ = parsear_producto(mat.get("product_id"))
            if not cod:
                continue
            qty = float(mat.get("deliver_quantity", 0) or 0)
            if es_dev:
                agg[cod]["devuelto"] += qty
                agg[cod]["ban_devs"].append((ban_nom, qty))
            else:
                agg[cod]["despachado"] += qty
                agg[cod]["ban_delivers"].append((ban_nom, qty))
    return agg


# ── Cruce bandejas + liquidacion ──────────────────────────────────────────────

def _cruce_nodo(nodo: dict) -> list:
    """
    Combina bandejas (almacén) con project.material (liquidación confirmada).

    FUENTE DE VERDAD para Bandejas/Devoluciones/Neto: bandejas crudas (raw).
    project.material.despachado NO se usa como neto porque NO siempre descuenta
    correctamente las devoluciones (ej: tray=12, desp=6 cuando deliver=6 y devol=6).

    Neto real = ban_deliver - ban_devolucion
    Pendiente = Neto real - consumido
    """
    ban_agg  = _agg_bandejas(nodo)
    liq_mats = nodo.get("liq_materiales", [])

    # Indexar liquidacion por codigo DOM
    liq_idx = {}
    for m in liq_mats:
        cod, _, desc = parsear_producto(m.get("product_id"))
        if cod:
            liq_idx[cod] = {
                "desc":       desc,
                "proyectado": float(m.get("amount",  0) or 0),
                "consumido":  float(m.get("advance", 0) or 0),  # lo que el usuario liquidó
            }

    # Union de todos los codigos — orden de bandejas primero, luego solo-liq
    todos_cod = list(ban_agg.keys())
    for cod in liq_idx:
        if cod not in ban_agg:
            todos_cod.append(cod)

    filas = []
    for cod in todos_cod:
        _ban_empty = {"despachado": 0.0, "devuelto": 0.0, "ban_delivers": [], "ban_devs": []}
        ban = ban_agg.get(cod, _ban_empty)
        liq = liq_idx.get(cod, {"desc": "", "proyectado": 0.0, "consumido": 0.0})

        ban_d    = ban["despachado"]            # qty total de bandejas deliver
        ban_dev  = ban["devuelto"]              # qty total de bandejas devolution
        ban_neto = ban_d - ban_dev              # lo que realmente debía liquidarse
        consumido = liq["consumido"]
        pendiente = ban_neto - consumido        # >0 falta liquidar, <0 sobre-liquido

        # Estado basado en bandejas crudas
        if ban_neto == 0 and consumido == 0:
            # Todo devuelto (o sin movimiento), nada que liquidar
            if ban_dev > 0:
                estado = "DEVOLUCION"
                color  = C_DEVOL
            else:
                estado = "SIN MOV"
                color  = C_PAR
        elif ban_neto == 0 and consumido > 0:
            # Se devolvio todo pero igual se liquido → error B
            estado = f"SOBRE LIQ +{consumido:.2f}"
            color  = C_ROJO
        elif consumido == 0 and ban_neto > 0:
            estado = "PENDIENTE"
            color  = C_AMBAR
        elif abs(pendiente) < 0.001:
            estado = "OK"
            color  = C_VERDE
        elif pendiente > 0:
            estado = f"BAJO LIQ {pendiente:+.2f}"
            color  = C_AMBAR
        else:
            estado = f"SOBRE LIQ {pendiente:+.2f}"
            color  = C_ROJO

        filas.append({
            "cod":          cod,
            "desc":         liq["desc"],
            "proyectado":   liq["proyectado"],
            "tray":         ban_d,        # despacho bruto desde BAN deliver
            "devoluciones": ban_dev,      # devolucion desde BAN devolution
            "desp_neto":    ban_neto,     # neto real = deliver - devolucion
            "consumido":    consumido,
            "pendiente":    pendiente,
            "estado":       estado,
            "color":        color,
            "ban_delivers": ban["ban_delivers"],   # [(ban_name, qty), ...]
            "ban_devs":     ban["ban_devs"],
        })

    return filas


# ── Cruce BAN vs Liquidacion ──────────────────────────────────────────────────
#
#  Expectativa  → bandejas de cada nodo (deliver y devolution)
#  Real         → project.material.advance (lo que el usuario liquidó)
#
#  Situacion A (rojo)   : BAN_neto > 0 pero advance == 0
#                         Segun las bandejas habia material a liquidar y no se hizo.
#  Situacion A- (salmon): advance > 0 pero advance < BAN_neto - epsilon
#                         Se liquido menos de lo que las bandejas indicaban.
#  Situacion B (naranja): advance > BAN_neto + epsilon
#                         Se liquido mas de lo neto (se consumio lo que debia devolverse).
#                         Nota: el campo diferencia del sistema tiene un bug y muestra 0,
#                         por eso usamos las bandejas raw para detectar devoluciones.
#  OK (verde)           : |advance - BAN_neto| < epsilon

C_A_TOTAL  = "FFC7CE"   # rojo    — no liquido nada
C_A_PARCIAL = "FCE4D6"  # salmon  — bajo liquidacion
C_B_SOBRE   = "F4B942"  # naranja — sobre liquidacion (consumio devolucion)
C_CRUCE_COL = "DEEAF1"  # azul claro — header columnas


def _fmt_bans(lista):
    """Formatea [(ban_name, qty), ...] → 'BAN-097710(6), BAN-097905(6)'"""
    if not lista:
        return ""
    return ", ".join(f"{n}({q:g})" for n, q in lista)


def _cruce_ban_liq(nodo: dict) -> list:
    """
    Cruce BAN vs liquidacion.

    Usa bandejas crudas como expectativa — NO project.material.despachado.
    Detecta:
      A: no liquido / bajo liquido lo que las BAN indicaban
      B: liquido mas del neto BAN (consumio material que debia devolver)
      DEVOLUCION: BAN deliver == BAN devolucion, nada debia liquidarse
    """
    ban_agg = _agg_bandejas(nodo)

    liq_idx = {}
    for m in nodo.get("liq_materiales", []):
        cod, _, desc = parsear_producto(m.get("product_id"))
        if cod:
            liq_idx[cod] = {
                "desc":    desc,
                "advance": float(m.get("advance", 0) or 0),
            }

    todos = sorted(set(list(ban_agg.keys()) + list(liq_idx.keys())))
    filas = []

    _ban_empty = {"despachado": 0.0, "devuelto": 0.0, "ban_delivers": [], "ban_devs": []}

    for cod in todos:
        ban  = ban_agg.get(cod, _ban_empty)
        liq  = liq_idx.get(cod, {"desc": "", "advance": 0.0})

        ban_d    = ban["despachado"]
        ban_dev  = ban["devuelto"]
        ban_neto = ban_d - ban_dev
        advance  = liq["advance"]
        dif      = ban_neto - advance   # >0 falta liquidar, <0 sobre-liquido

        # ── Estado ────────────────────────────────────────────────────────────
        if ban_neto == 0 and advance == 0:
            if ban_dev > 0:
                # Devolucion completa: BAN deliver == BAN devolucion, no habia nada que liquidar
                estado = "DEVOLUCION — no liquidar"
                bg_est = C_VERDE
            else:
                estado = "—"
                bg_est = BLANCO
        elif ban_neto == 0 and advance > 0:
            # Se devolvio todo pero se liquido igual → error B clasico
            estado = f"B — LIQUIDO DEVOLUCION (+{advance:g})"
            bg_est = C_B_SOBRE
        elif advance == 0 and ban_neto > 0:
            estado = "A — NO LIQUIDO"
            bg_est = C_A_TOTAL
        elif abs(dif) < 0.001:
            estado = "OK"
            bg_est = C_VERDE
        elif dif > 0:
            estado = f"A — BAJO LIQ ({dif:+g})"
            bg_est = C_A_PARCIAL
        else:
            # advance > ban_neto → consumio mas del neto (parte de la devolucion)
            estado = f"B — SOBRE LIQ ({dif:+g})"
            bg_est = C_B_SOBRE

        filas.append({
            "cod":          cod,
            "desc":         liq["desc"] or "",
            "ban_d":        ban_d,
            "ban_dev":      ban_dev,
            "ban_neto":     ban_neto,
            "advance":      advance,
            "dif":          dif,
            "estado":       estado,
            "bg_est":       bg_est,
            "bans_desp":    _fmt_bans(ban["ban_delivers"]),   # "BAN-097710(6)"
            "bans_dev":     _fmt_bans(ban["ban_devs"]),       # "BAN-097905(6)"
        })

    return filas


def _hoja_verificacion(wb: openpyxl.Workbook, data: dict):
    """
    Hoja CRUCE BAN-LIQ:
      Por nodo, por material — relacion BAN → material y comparativa con liquidado real.
    Columnas:
      # | Codigo | Descripcion | BANs Despacho | Qty D | BANs Devolucion | Qty Dev
        | BAN Neto | Liquidado Real | Diferencia | Estado
    """
    ws = wb.create_sheet("CRUCE BAN-LIQ")
    ws.sheet_view.showGridLines = False

    C_NUM   = 1
    C_COD   = 2
    C_DESC  = 3
    C_BANS_D  = 4   # nombres de BANs deliver  "BAN-097710(6)"
    C_QTY_D   = 5   # qty total deliver
    C_BANS_V  = 6   # nombres de BANs devolucion
    C_QTY_V   = 7   # qty total devolucion
    C_NETO    = 8   # BAN Neto = expectativa
    C_ADV     = 9   # Liquidado real
    C_DIF     = 10  # BAN Neto - Liquidado
    C_EST     = 11  # Estado
    NCOLS     = 11

    anchos = [4, 16, 38, 26, 8, 26, 8, 8, 8, 10, 28]
    for i, w in enumerate(anchos, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    padre = data["padre"]
    nodos = data["nodos"]
    odm   = padre.get("order_number") or padre.get("odm") or "—"
    r = 1

    # Titulo
    _cell(ws, r, 1, f"CRUCE BAN vs LIQUIDACION  —  OdM {odm}  —  {padre.get('name','')}",
          bold=True, bg=C_TITULO, fg=BLANCO, size=12, align_h="center",
          merge_to=NCOLS, border=False)
    _rh(ws, r, 24); r += 1

    # Notas de interpretacion
    r += 1
    notas = [
        ("BAN Neto",       "= Qty Despacho BAN (deliver) − Qty Devolucion BAN  →  lo que DEBÍA liquidarse"),
        ("Liquidado Real", "= project.material.advance  →  lo que el usuario CONFIRMÓ en la liquidacion"),
        ("Diferencia",     "= BAN Neto − Liquidado  (+ : falta liquidar  |  − : sobre-liquidacion)"),
    ]
    for lbl, txt in notas:
        _cell(ws, r, 1, lbl, bold=True, bg=C_META, align_h="right", border=False, size=8)
        _cell(ws, r, 2, txt, bold=False, bg=BLANCO, align_h="left", border=False,
              size=8, merge_to=NCOLS, italic=True)
        _rh(ws, r, 11); r += 1

    # Leyenda
    r += 1
    leyenda = [
        (C_A_TOTAL,  "A — NO LIQUIDO        : BAN despacho > 0, liquidado = 0 (error: no se liquido)"),
        (C_A_PARCIAL,"A — BAJO LIQ.         : Liquido menos de lo que el BAN Neto indica"),
        (C_B_SOBRE,  "B — SOBRE / LIQ.DEV.  : Liquido mas del BAN Neto (consumio parte de la devolucion)"),
        (C_VERDE,    "OK / DEVOLUCION       : Liquidado = BAN Neto, o todo fue devuelto correctamente"),
    ]
    for bg, txt in leyenda:
        _cell(ws, r, 1, "", bg=bg, border=False)
        _cell(ws, r, 2, txt, bg=BLANCO, border=False, size=8, italic=True, merge_to=NCOLS)
        _rh(ws, r, 12); r += 1
    r += 1

    tot_a = tot_b = tot_ok = tot_dev = 0
    idx_global = 0

    for nodo in nodos:
        pro      = nodo.get("code", "")
        nodo_num = nodo.get("nodo") or "—"
        status   = nombre_campo(nodo.get("status")) or "—"
        resp     = nombre_campo(nodo.get("responsible_id")) or "—"

        filas = _cruce_ban_liq(nodo)
        n_a   = sum(1 for f in filas if f["estado"].startswith("A"))
        n_b   = sum(1 for f in filas if f["estado"].startswith("B"))
        n_ok  = sum(1 for f in filas if f["estado"] == "OK")
        n_dev = sum(1 for f in filas if "DEVOLUCION" in f["estado"])
        tot_a += n_a; tot_b += n_b; tot_ok += n_ok; tot_dev += n_dev

        color_hdr = C_ROJO if (n_a + n_b) > 0 else C_NODO
        resumen_n = (f"NODO {nodo_num}  |  {pro}  |  {status}  |  {resp}  "
                     f"|  A={n_a}  B={n_b}  OK={n_ok}  Dev={n_dev}")
        _cell(ws, r, 1, resumen_n, bold=True, bg=color_hdr, fg=BLANCO,
              size=10, merge_to=NCOLS, border=False)
        _rh(ws, r, 18); r += 1

        if not filas:
            _cell(ws, r, 1, "Sin materiales en BAN ni en liquidacion", italic=True, fg="888888",
                  merge_to=NCOLS, border=False, size=9)
            _rh(ws, r, 13); r += 1
            r += 1
            continue

        # Header columnas
        HDRS = ["#", "Codigo DOM", "Descripcion",
                "BANs Despacho", "Qty D",
                "BANs Devolucion", "Qty Dev",
                "BAN Neto", "Liquidado", "Diferencia", "Estado"]
        for ci, h in enumerate(HDRS, 1):
            _cell(ws, r, ci, h, bold=True, bg=C_CRUCE_COL, align_h="center", size=9)
        _rh(ws, r, 14); r += 1

        ws.freeze_panes = ws.cell(row=r, column=1)

        for fila in filas:
            if fila["estado"] == "—":
                continue   # sin movimiento ni liquidacion: omitir
            idx_global += 1
            bg_est = fila["bg_est"]
            bg_row = BLANCO
            # Resaltar fila completa si es error A o B
            if fila["estado"].startswith("A") or fila["estado"].startswith("B"):
                bg_row = bg_est

            bg_dev_col = "FFF2CC" if fila["ban_dev"] > 0 else BLANCO

            _cell(ws, r, C_NUM,    idx_global,          bg=BLANCO,     align_h="center", size=9)
            _cell(ws, r, C_COD,    fila["cod"],          bg=bg_row,     align_h="center", size=9)
            _cell(ws, r, C_DESC,   fila["desc"],         bg=bg_row,     align_h="left",   size=9, wrap=True)
            _cell(ws, r, C_BANS_D, fila["bans_desp"],   bg=bg_row,     align_h="left",   size=8, wrap=True)
            _cell(ws, r, C_QTY_D,  fila["ban_d"],        bg=bg_row,     align_h="center", size=9, num_fmt=NUM_FMT)
            _cell(ws, r, C_BANS_V, fila["bans_dev"],    bg=bg_dev_col, align_h="left",   size=8, wrap=True)
            _cell(ws, r, C_QTY_V,  fila["ban_dev"],      bg=bg_dev_col, align_h="center", size=9, num_fmt=NUM_FMT)
            _cell(ws, r, C_NETO,   fila["ban_neto"],     bg=bg_row,     align_h="center", size=9,
                  num_fmt=NUM_FMT, bold=True)
            _cell(ws, r, C_ADV,    fila["advance"],      bg=bg_row,     align_h="center", size=9,
                  num_fmt=NUM_FMT, bold=True)
            _cell(ws, r, C_DIF,    fila["dif"],          bg=bg_est,     align_h="center", size=9,
                  num_fmt="+#,##0.##;-#,##0.##;0")
            _cell(ws, r, C_EST,    fila["estado"],       bg=bg_est,     align_h="center", size=9, bold=True)
            _rh(ws, r, 28); r += 1   # altura mayor para que se vean los nombres de BAN

        r += 1

    # Resumen final
    r += 1
    tot_err = tot_a + tot_b
    bg_total = C_VERDE if tot_err == 0 else C_ROJO
    _cell(ws, r, 1,
          (f"RESUMEN  —  Errores A (no/bajo liq): {tot_a}  |  "
           f"Errores B (sobre liq / liq devol): {tot_b}  |  "
           f"OK: {tot_ok}  |  Devoluciones correctas: {tot_dev}"),
          bold=True, bg=bg_total, fg=BLANCO, size=11, align_h="center",
          merge_to=NCOLS, border=False)
    _rh(ws, r, 22)


# ── Excel ─────────────────────────────────────────────────────────────────────

def generar_excel(data: dict) -> bytes:
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "BALANCE SGS"
    ws.sheet_view.showGridLines = False

    for i, w in enumerate([4, 16, 46, 10, 10, 10, 10, 10, 10, 14], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    padre = data["padre"]
    nodos = data["nodos"]
    r     = 1

    odm   = padre.get("order_number") or padre.get("odm") or "—"
    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")

    # ── Titulo ────────────────────────────────────────────────────────────────
    _cell(ws, r, 1, f"BALANCE SGS  —  OdM {odm}  —  {padre.get('name','')}",
          bold=True, bg=C_TITULO, fg=BLANCO, size=12, align_h="center",
          merge_to=NCOLS, border=False)
    _rh(ws, r, 24); r += 1

    for lbl, val in [
        ("Codigo padre", padre.get("code", "")),
        ("Contrato",     nombre_campo(padre.get("contract_id"))),
        ("Cliente",      nombre_campo(padre.get("customer_id"))),
        ("Region",       nombre_campo(padre.get("region_id"))),
        ("Nodos",        len(nodos)),
        ("Generado",     fecha),
    ]:
        _cell(ws, r, 1, lbl, bold=True,  bg=C_META,  align_h="right", border=False, size=9)
        _cell(ws, r, 2, val, bold=False, bg=BLANCO,  align_h="left",  border=False, size=9,
              merge_to=NCOLS)
        _rh(ws, r, 14); r += 1

    # ── Leyenda ───────────────────────────────────────────────────────────────
    r += 1
    for bg, txt in [(C_VERDE, "OK — neto almacen = liquidado"),
                    (C_DEVOL, "DEVOLUCION — material devuelto (no liquidar)"),
                    (C_AMBAR, "PENDIENTE — sin confirmar"),
                    (C_ROJO,  "DIFERENCIA — neto != liquidado")]:
        _cell(ws, r, 1, "", bg=bg, border=False)
        _cell(ws, r, 2, txt, bg=BLANCO, border=False, size=8, italic=True, merge_to=NCOLS)
        _rh(ws, r, 12); r += 1
    r += 1

    # ── Header columnas ───────────────────────────────────────────────────────
    HDRS = ["#", "Codigo DOM", "Descripcion", "Proyectado",
            "Bandejas", "Dev.Alm", "Neto Alm.", "Consumido", "Pend.Liq", "Estado"]
    for ci, h in enumerate(HDRS, 1):
        _cell(ws, r, ci, h, bold=True, bg=C_COL_HDR, align_h="center", size=9)
    _rh(ws, r, 14); r += 1

    ws.freeze_panes = ws.cell(row=r, column=1)

    tot_tray = tot_dalm = tot_neto = tot_cons = tot_pend = 0.0
    idx_global = 0

    # ── Por cada nodo ─────────────────────────────────────────────────────────
    for nodo in nodos:
        pro      = nodo.get("code", "")
        nodo_num = nodo.get("nodo") or "—"
        status   = nombre_campo(nodo.get("status")) or "—"
        pdl      = nodo.get("pdl") or "—"
        resp     = nombre_campo(nodo.get("responsible_id")) or "—"
        filas    = _cruce_nodo(nodo)

        nd_tray  = sum(f["tray"]        for f in filas)
        nd_dalm  = sum(f["devoluciones"] for f in filas)
        nd_neto  = sum(f["desp_neto"]   for f in filas)
        nd_cons  = sum(f["consumido"]   for f in filas)
        nd_pend  = sum(f["pendiente"]   for f in filas)
        n_pend   = sum(1 for f in filas if f["estado"] == "PENDIENTE")
        n_dif    = sum(1 for f in filas if f["estado"].startswith("PEND "))

        tot_tray += nd_tray
        tot_dalm += nd_dalm
        tot_neto += nd_neto
        tot_cons += nd_cons
        tot_pend += nd_pend

        # Header nodo
        resumen_nodo = (
            f"NODO {nodo_num}  |  {pro}  |  {status}  |  PDL: {pdl}  "
            f"|  {resp}  |  Pendientes: {n_pend}  |  Diferencias: {n_dif}"
        )
        _cell(ws, r, 1, resumen_nodo, bold=True, bg=C_NODO, fg=BLANCO,
              size=10, merge_to=NCOLS, border=False)
        _rh(ws, r, 18); r += 1

        if not filas:
            _cell(ws, r, 1, "Sin materiales", italic=True, fg="888888",
                  merge_to=NCOLS, border=False, size=9)
            _rh(ws, r, 13); r += 1
            continue

        for fila in filas:
            idx_global += 1
            bg_row  = BLANCO if idx_global % 2 == 0 else C_PAR
            bg_est  = fila["color"]

            _cell(ws, r, C_NUM,  idx_global,           bg=bg_row, align_h="center", size=9)
            _cell(ws, r, C_COD,  fila["cod"],           bg=bg_row, align_h="center", size=9)
            _cell(ws, r, C_DESC, fila["desc"],          bg=bg_row, align_h="left",   size=9, wrap=True)
            _cell(ws, r, C_PROY, fila["proyectado"],    bg=bg_row, align_h="center", size=9, num_fmt=NUM_FMT)
            _cell(ws, r, C_TRAY, fila["tray"],          bg=bg_row, align_h="center", size=9, num_fmt=NUM_FMT)
            _cell(ws, r, C_DALM, fila["devoluciones"],  bg=bg_row, align_h="center", size=9, num_fmt=NUM_FMT)
            _cell(ws, r, C_NETO, fila["desp_neto"],     bg=bg_row, align_h="center", size=9, num_fmt=NUM_FMT, bold=True)
            _cell(ws, r, C_CONS, fila["consumido"],     bg=bg_row, align_h="center", size=9, num_fmt=NUM_FMT, bold=True)
            _cell(ws, r, C_PEND, fila["pendiente"],     bg=bg_est, align_h="center", size=9, num_fmt=NUM_FMT)
            _cell(ws, r, C_EST,  fila["estado"],        bg=bg_est, align_h="center", size=9, bold=True)
            _rh(ws, r, 14); r += 1

        # Subtotal nodo
        bg_sp = C_VERDE if abs(nd_pend) < 0.01 else C_ROJO
        _cell(ws, r, 1, f"Subtotal {pro}", bold=True, bg=C_TOTAL,
              align_h="right", size=9, merge_to=C_PROY)
        _cell(ws, r, C_TRAY, nd_tray,  bold=True, bg=C_TOTAL, align_h="center", size=9, num_fmt=NUM_FMT)
        _cell(ws, r, C_DALM, nd_dalm,  bold=True, bg=C_TOTAL, align_h="center", size=9, num_fmt=NUM_FMT)
        _cell(ws, r, C_NETO, nd_neto,  bold=True, bg=C_TOTAL, align_h="center", size=9, num_fmt=NUM_FMT)
        _cell(ws, r, C_CONS, nd_cons,  bold=True, bg=C_TOTAL, align_h="center", size=9, num_fmt=NUM_FMT)
        _cell(ws, r, C_PEND, nd_pend,  bold=True, bg=bg_sp,   align_h="center", size=9, num_fmt=NUM_FMT)
        _cell(ws, r, C_EST,  "",       bold=True, bg=C_TOTAL)
        _rh(ws, r, 14); r += 1
        r += 1

    # ── Total general ─────────────────────────────────────────────────────────
    bg_tp = C_VERDE if abs(tot_pend) < 0.01 else C_ROJO

    _cell(ws, r, 1, "TOTAL GENERAL", bold=True, bg=C_TITULO, fg=BLANCO,
          size=11, align_h="right", merge_to=C_PROY, border=False)
    _cell(ws, r, C_TRAY, tot_tray, bold=True, bg=C_TITULO, fg=BLANCO,
          align_h="center", size=10, num_fmt=NUM_FMT, border=False)
    _cell(ws, r, C_DALM, tot_dalm, bold=True, bg=C_TITULO, fg=BLANCO,
          align_h="center", size=10, num_fmt=NUM_FMT, border=False)
    _cell(ws, r, C_NETO, tot_neto, bold=True, bg=C_TITULO, fg=BLANCO,
          align_h="center", size=10, num_fmt=NUM_FMT, border=False)
    _cell(ws, r, C_CONS, tot_cons, bold=True, bg=C_TITULO, fg=BLANCO,
          align_h="center", size=10, num_fmt=NUM_FMT, border=False)
    _cell(ws, r, C_PEND, tot_pend, bold=True, bg=bg_tp,
          align_h="center", size=10, num_fmt=NUM_FMT)
    _cell(ws, r, C_EST,  "", bold=True, bg=C_TITULO, border=False)
    _rh(ws, r, 20)

    # ── Hoja de verificacion de stock ─────────────────────────────────────────
    _hoja_verificacion(wb, data)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ── Ticket de reversion ───────────────────────────────────────────────────────

def generar_reversion(data: dict) -> bytes:
    """
    Genera el Excel de ticket de reversion para materiales Situacion B
    (sobre-liquidados o liquidados cuando debian devolverse).

    Formato identico al template 'reversion material.xlsx':
      Fila 2: headers  B=PRO | C=Codigo | D=Descripcion | E=Cantidad | F=Motivo
      Fila 3+: datos
    """
    # Indexar customer_default_code por codigo DOM para cada nodo
    filas = []

    for nodo in data["nodos"]:
        pro = nodo.get("code", "")

        # Mapa cod_dom → {cod_cliente, desc} desde project.material
        liq_idx = {}
        for m in nodo.get("liq_materiales", []):
            cod, _, desc = parsear_producto(m.get("product_id"))
            if cod:
                liq_idx[cod] = {
                    "desc":       desc,
                    "advance":    float(m.get("advance", 0) or 0),
                    "cod_cliente": str(m.get("customer_default_code", "") or cod),
                }

        # Cruce con bandejas para detectar Situacion B
        ban_agg = _agg_bandejas(nodo)
        todos   = sorted(set(list(ban_agg.keys()) + list(liq_idx.keys())))
        _empty  = {"despachado": 0.0, "devuelto": 0.0, "ban_delivers": [], "ban_devs": []}

        for cod in todos:
            ban     = ban_agg.get(cod, _empty)
            liq     = liq_idx.get(cod, {"desc": "", "advance": 0.0, "cod_cliente": cod})
            ban_neto = ban["despachado"] - ban["devuelto"]
            advance  = liq["advance"]
            dif      = ban_neto - advance   # negativo = sobre-liquido

            if ban_neto == 0 and advance > 0:
                # Devolucion completa pero liquido igual — reversar todo lo liquidado
                cantidad = advance
            elif dif < -0.001:
                # Liquido mas del neto — reversar solo el exceso
                cantidad = abs(dif)
            else:
                continue   # OK o Situacion A — no va al ticket

            filas.append({
                "pro":         pro,
                "cod_cliente": liq["cod_cliente"],
                "desc":        liq["desc"],
                "cantidad":    cantidad,
            })

    # Construir Excel con el mismo formato del template
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Reversion"
    ws.sheet_view.showGridLines = False

    for col, w in zip("BCDEF", [11.4, 17.9, 40.0, 18.4, 38.0]):
        ws.column_dimensions[col].width = w

    # Fila 1 vacia (igual que template)
    # Fila 2: headers
    hdrs = ["PRO", "Codigo de material", "Descripcion de material",
            "cantidad a reversar", "Motivo"]
    for ci, h in enumerate(hdrs, 2):   # columna B=2, C=3, D=4, E=5, F=6
        cell = ws.cell(row=2, column=ci, value=h)
        cell.font = Font(bold=True, name="Calibri", size=11)

    # Datos desde fila 3
    motivo = "Queria devolverlo y lo liquide por error"
    for ri, f in enumerate(filas, 3):
        ws.cell(row=ri, column=2, value=f["pro"]).font       = Font(name="Calibri", size=11)
        ws.cell(row=ri, column=3, value=f["cod_cliente"]).font = Font(name="Calibri", size=11)
        ws.cell(row=ri, column=4, value=f["desc"]).font      = Font(name="Calibri", size=11)
        ws.cell(row=ri, column=5, value=f["cantidad"]).font  = Font(name="Calibri", size=11)
        ws.cell(row=ri, column=6, value=motivo).font         = Font(name="Calibri", size=11)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read(), len(filas)
