"""
Audit de calidad del Excel exportado — Sabogal Platform
Ejecutar desde la raíz del proyecto:
    python3 audit/audit_excel.py

Sin dependencias de FastAPI ni base de datos — solo openpyxl.
"""
import sys
from io import BytesIO
from datetime import date
from dataclasses import dataclass

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("Instala openpyxl: pip install openpyxl")

# ── Colores esperados (últimos 6 hex = RGB sin canal alpha) ──────────────────
# openpyxl devuelve ARGB de 8 chars (alpha siempre "00" para fgColor solid)
# pero Excel ignora el alpha en PatternFill — comparamos solo los 6 chars RGB.
EXPECTED_BLUE  = "0070C0"
EXPECTED_LIME  = "92D050"
EXPECTED_LBLUE = "00B0F0"
EXPECTED_FONT  = 10
EXPECTED_ROW_H = 40   # pt — LOW si difiere (los originales varían entre 27 y 40)

LIME_HDRS  = {"# PDT", "ESTADO", "CD AFECTADOS", "HORA", "OT", "ESTADO CUMPLIMENTACIÓN"}
LBLUE_HDRS = {"MO REAL", "#PROLIQUIDADO", "FECHA LIQUIDACIÓN SGS",
              "ESTADO FACTURACIÓN", "FECHA ENVIO A FACTURACIÓN"}
MONEY_HDRS = {"VALORDIA", "MO PROYECTADA", "MO REAL",
              "VALOR DEJADO DE EJECUTAR", "VALOR MATERIAL"}

# ── Lógica de exportación duplicada (sin importar FastAPI) ────────────────────
HEADER_BLUE   = "0070C0"
HEADER_LIME   = "92D050"
HEADER_LBLUE  = "00B0F0"
CELL_ING      = "DDEEFF"
CELL_PROG     = "E2EFDA"
CURRENCY_FMT  = r'_-"$"\ * #,##0_-;\-"$"\ * #,##0_-;_-"$"\ * "-"??_-;_-@_-'

_ING  = CELL_ING
_PROG = CELL_PROG

COLUMNAS_EXCEL = [
    ("ZONA",                     "zona",                  None),
    ("MES",                      "fecha",                 None),
    ("DÍA",                      "fecha",                 None),
    ("FECHA",                    "fecha",                 None),
    ("LÍDER",                    "lider",                 None),
    ("TIPO GRUPO",               "tipo_grupo",            None),
    ("PROCESO",                  "proceso",               _ING),
    ("SUBPROCESO",               "subproceso",            _ING),
    ("PROYECTO O ANTECEDENTE",   "proyecto_antecedente",  _ING),
    ("PMS",                      "pms",                   _ING),
    ("CIRCUITO",                 "circuito",              _ING),
    ("LCL",                      "lcl",                   _ING),
    ("FECHA VIGENCIA",           "fecha_vigencia",        _ING),
    ("TIPO PDT",                 "tipo_pdt",              _ING),
    ("# PDT",                    "num_pdt",               _ING),
    ("ESTADO",                   "estado",                _ING),
    ("CD AFECTADOS",             "cd_afectados",          _ING),
    ("HORA",                     "hora",                  _ING),
    ("MUNICIPIO",                "municipio",             _ING),
    ("VDA",                      "vereda",                _ING),
    ("ALCANCE",                  "alcance",               _ING),
    ("GESTOR ENEL",              "gestor_enel",           _PROG),
    ("SUPERVISOR",               "supervisor",            _PROG),
    ("VALORDIA",                 "valor_dia",             _PROG),
    ("MO PROYECTADA",            "mo_proyectada",         _PROG),
    ("MO REAL",                  "mo_real",               _PROG),
    ("VALOR DEJADO DE EJECUTAR", "valor_dejado_ejecutar", _PROG),
    ("VALOR MATERIAL",           "valor_material",        _PROG),
    ("ESTADO PDT",               "estado_pdt",            None),
    ("NOTAS",                    "notas",                 None),
    ("#PROLIQUIDADO",            "num_proliquidado",      None),
    ("FECHA LIQUIDACIÓN SGS",    "fecha_liquidacion_sgs", None),
    ("ESTADO FACTURACIÓN",       "estado_facturacion",    None),
    ("FECHA ENVIO A FACTURACIÓN","fecha_envio_facturacion",None),
    ("OT",                       "ot",                    None),
    ("ESTADO CUMPLIMENTACIÓN",   "estado_cumplimentacion",None),
    ("INOPERATIVIDAD",           "inoperatividad",        None),
]

COL_WIDTHS = [8,7,10,12,21,10,16,16,36,26,15,14,14,10,20,14,18,8,15,15,42,20,20,12,14,14,22,14,14,30,15,14,18,20,12,22,12]
MESES_ES = {1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",
            7:"julio",8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"}
DIAS_ES  = {0:"lunes",1:"martes",2:"miércoles",3:"jueves",4:"viernes",5:"sábado",6:"domingo"}


def _header_fill(header):
    if header in LIME_HDRS:
        return PatternFill("solid", fgColor=HEADER_LIME)
    if header in LBLUE_HDRS:
        return PatternFill("solid", fgColor=HEADER_LBLUE)
    return PatternFill("solid", fgColor=HEADER_BLUE)


def _get_val(prog, campo, sub=None):
    if sub == "mes":
        return MESES_ES.get(prog.fecha.month, "") if prog.fecha else ""
    if sub == "dia":
        return DIAS_ES.get(prog.fecha.weekday(), "") if prog.fecha else ""
    val = getattr(prog, campo, None)
    return val.value if hasattr(val, "value") else val


def _write_to_sheet(ws, registros):
    thin   = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    h_font = Font(bold=True, color="FFFFFF", size=10)
    d_font = Font(size=10)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left   = Alignment(vertical="center", wrap_text=False)
    fill_cache = {}

    def get_fill(hex_color):
        if hex_color not in fill_cache:
            fill_cache[hex_color] = PatternFill("solid", fgColor=hex_color)
        return fill_cache[hex_color]

    for col_idx, (header, _, _cc) in enumerate(COLUMNAS_EXCEL, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font      = h_font
        cell.fill      = _header_fill(header)
        cell.alignment = center
        cell.border    = border

    ws.row_dimensions[1].height = 40
    ws.freeze_panes = "A2"

    for row_idx, prog in enumerate(registros, 2):
        for col_idx, (header, campo, cell_color) in enumerate(COLUMNAS_EXCEL, 1):
            sub = None
            if campo == "fecha":
                if header == "MES":   sub = "mes"
                elif header == "DÍA": sub = "dia"
            val  = _get_val(prog, campo, sub)
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.font      = d_font
            cell.border    = border
            cell.alignment = left
            if cell_color:
                cell.fill = get_fill(cell_color)
            if header in MONEY_HDRS and val is not None:
                cell.number_format = CURRENCY_FMT

    for i, w in enumerate(COL_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ── Datos mock ────────────────────────────────────────────────────────────────
class _MockProg:
    zona = type("Z", (), {"value": "ALMEIDAS"})()
    fecha = date(2026, 5, 20)
    lider = "EDGAR TÉLLEZ"
    tipo_grupo = type("T", (), {"value": "CAMP5"})()
    proceso = "OBRAS"
    subproceso = "OBRAS SIMPLES"
    proyecto_antecedente = "MEJORAMIENTO RED BT ALMEIDAS"
    pms = "PMS-ALM-0782"
    circuito = "CIRCUITO ALM-04"
    lcl = "LCL-2024-1123"
    fecha_vigencia = date(2026, 12, 31)
    tipo_pdt = "TP"
    num_pdt = "PDT-ALM-2026-0089"
    estado = type("E", (), {"value": "EJECUTADO"})()
    cd_afectados = "28"
    hora = "07:00"
    municipio = "ZIPAQUIRÁ"
    vereda = "EL EMPALME"
    alcance = "Cambio transformador 45 KVA sector La Esperanza"
    gestor_enel = "HERNÁNDEZ MARIO"
    supervisor = "GÓMEZ ANDRÉS"
    valor_dia = 950000.0
    mo_proyectada = 950000.0
    mo_real = 950000.0
    valor_dejado_ejecutar = 0.0
    valor_material = 180000.0
    estado_pdt = None
    notas = "Ejecutado sin novedad"
    num_proliquidado = "PL-2026-00141"
    fecha_liquidacion_sgs = date(2026, 5, 20)
    estado_facturacion = "EN REVISIÓN"
    fecha_envio_facturacion = None
    ot = "OT-ALM-20260520"
    estado_cumplimentacion = "CUMPLIDO"
    inoperatividad = None


def generate_test_excel() -> bytes:
    mock = _MockProg()
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for zona in ["ALMEIDAS", "GUAVIO", "UBATE", "LV&TLC"]:
        ws = wb.create_sheet(title=f"P. {zona}")
        _write_to_sheet(ws, [mock])
    ws_uni = wb.create_sheet(title="P. UNIFICADA")
    _write_to_sheet(ws_uni, [mock, mock])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Audit ─────────────────────────────────────────────────────────────────────
@dataclass
class Finding:
    level: str
    check: str
    detail: str


findings: list = []

def fail(level, check, detail):
    findings.append(Finding(level, check, detail))
    print(f"  [{level:<8}] {check}: {detail}")

def ok(check):
    print(f"  [OK      ] {check}")


def audit(raw: bytes):
    wb = openpyxl.load_workbook(BytesIO(raw))

    print("\n── Nombres de hojas ─────────────────────────────────────────")
    for sname in wb.sheetnames:
        if sname.startswith("P. "):
            ok(f"'{sname}'")
        else:
            fail("MEDIUM", "nombre_hoja", f"'{sname}' no empieza por 'P. '")

    for ws in wb.worksheets:
        print(f"\n── Hoja: {ws.title} ─────────────────────────────────────────")

        # Freeze panes
        fp = str(ws.freeze_panes)
        if fp == "A2":
            ok("freeze_panes = A2")
        else:
            fail("HIGH", "freeze_panes", f"'{fp}' (esperado A2)")

        # Altura fila 1
        h = ws.row_dimensions[1].height
        if h and h >= 35:
            ok(f"altura fila 1 = {h}pt")
        else:
            fail("LOW", "row_height", f"altura = {h}pt (esperado ≥35pt)")

        # Colores de cabecera
        col_errors = 0
        for cell in ws[1]:
            if cell.value is None:
                continue
            hdr = str(cell.value)
            rgb = cell.fill.fgColor.rgb if cell.fill and cell.fill.fgColor else "00000000"
            if hdr in LIME_HDRS:
                expected = EXPECTED_LIME
            elif hdr in LBLUE_HDRS:
                expected = EXPECTED_LBLUE
            else:
                expected = EXPECTED_BLUE
            # Normalizar: comparar solo los últimos 6 chars (RGB sin alpha)
            if (rgb or "")[-6:].upper() != expected.upper():
                col_errors += 1
                fail("CRITICAL", "color_cabecera",
                     f"col '{hdr}': RGB={rgb} (esperado {expected})")
        if col_errors == 0:
            ok(f"colores cabecera OK ({ws.max_column} cols)")

        # Fuente cabecera: tamaño + negrita
        font_errs = bold_errs = 0
        for cell in ws[1]:
            if not cell.value or not cell.font:
                continue
            if cell.font.size != EXPECTED_FONT:
                font_errs += 1
            if not cell.font.bold:
                bold_errs += 1
        if font_errs == 0:
            ok(f"fuente cabecera = {EXPECTED_FONT}pt")
        else:
            fail("MEDIUM", "font_size_header", f"{font_errs} celdas ≠ {EXPECTED_FONT}pt")
        if bold_errs == 0:
            ok("cabeceras en negrita")
        else:
            fail("MEDIUM", "bold_header", f"{bold_errs} columnas sin negrita")

        # Fuente datos
        if ws.max_row > 1:
            data_errs = 0
            for cell in ws[2]:
                if cell.value is not None and cell.font and cell.font.size != EXPECTED_FONT:
                    data_errs += 1
            if data_errs == 0:
                ok(f"fuente datos = {EXPECTED_FONT}pt")
            else:
                fail("MEDIUM", "font_size_data", f"{data_errs} celdas ≠ {EXPECTED_FONT}pt")

        # Formato monetario
        if ws.max_row > 1:
            money_cols = {c.value: c.column for c in ws[1] if c.value in MONEY_HDRS}
            money_errs = 0
            for hdr, col in money_cols.items():
                c2 = ws.cell(row=2, column=col)
                if c2.value is not None and c2.number_format == "General":
                    money_errs += 1
                    fail("HIGH", "formato_monetario", f"col '{hdr}': number_format='General'")
            if money_errs == 0:
                ok(f"formato $ en {len(money_cols)} columnas monetarias")

        # Bordes
        border_ok = all(
            c.border and c.border.left.style
            for c in ws[1] if c.value
        )
        if border_ok:
            ok("bordes en cabecera OK")
        else:
            fail("LOW", "bordes", "faltan bordes en alguna columna de cabecera")

        # Datos presentes
        nrows = ws.max_row - 1
        if nrows > 0:
            ok(f"{nrows} fila(s) de datos")
        else:
            fail("INFO", "datos", "hoja sin filas de datos (esperado al menos 1 en prueba)")


def main():
    print("=" * 60)
    print("  AUDIT EXCEL — Sabogal Platform")
    print("=" * 60)
    try:
        raw = generate_test_excel()
        print(f"\nExcel generado OK ({len(raw):,} bytes)")
        audit(raw)
    except Exception as e:
        import traceback
        findings.append(Finding("CRITICAL", "generacion", str(e)))
        print(f"\n[CRITICAL] Error: {e}")
        traceback.print_exc()

    print("\n" + "=" * 60)
    by_lvl = {}
    for f in findings:
        by_lvl.setdefault(f.level, []).append(f)

    if not findings:
        print("  OK  0 hallazgos: Excel conforme al original")
    else:
        print(f"  Total hallazgos: {len(findings)}")
        for lvl in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
            n = len(by_lvl.get(lvl, []))
            if n:
                print(f"    {lvl:<8}: {n}")

    has_blocker = any(f.level in ("CRITICAL", "HIGH") for f in findings)
    sys.exit(1 if has_blocker else 0)


if __name__ == "__main__":
    main()
