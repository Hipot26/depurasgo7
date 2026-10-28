from datetime import date
from typing import Optional
from io import BytesIO
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from app.db.database import get_db
from app.api.deps import get_current_active_user, unidad_de, ROLES_GLOBALES
from app.models.models import Programacion, Usuario, ZonaEnum, UNIDAD_DEFAULT

router = APIRouter(prefix="/export", tags=["export"])


def _unidad_efectiva(user: Usuario, unidad: Optional[str]) -> str:
    if user.rol in ROLES_GLOBALES:
        return unidad or UNIDAD_DEFAULT
    return unidad_de(user)


# Columnas que NO aplican a cada unidad (Agobardo retiró estas del Excel MOES)
UNIDAD_OCULTA = {
    "CHOCONTA": {"pms", "fecha_vigencia", "cd_afectados", "ot", "estado_cumplimentacion"},
}
# Etiqueta de la columna de tipo según la unidad
UNIDAD_TIPO_LABEL = {"CHOCONTA": "TIPO RECURSO"}

# ── Colores exactos de los Excel originales del Ing. Sabogal ─────────────────
HEADER_BLUE      = "0070C0"   # azul dominante (cols 1-14, 19-25, 28-29, 37)
HEADER_LIME      = "92D050"   # verde lima (# PDT, ESTADO, CD AFECTADOS, HORA, OT, ESTADO CUMPLIM.)
HEADER_LBLUE     = "00B0F0"   # azul claro (MO REAL, #PROLIQUIDADO, FECHA LIQ. SGS, etc.)

# Colores de fondo en celdas de datos (por rol, ayuda visual para el usuario)
CELL_INGENIERO   = "DDEEFF"
CELL_PROGRAMADOR = "E2EFDA"

# Formato contable colombiano ($)
CURRENCY_FMT = r'_-"$"\ * #,##0_-;\-"$"\ * #,##0_-;_-"$"\ * "-"??_-;_-@_-'

# Columnas con cabecera lima en el original
_LIME_HEADERS = {"# PDT", "ESTADO", "CD AFECTADOS", "HORA", "OT", "ESTADO CUMPLIMENTACIÓN"}
# Columnas con cabecera azul claro en el original
_LBLUE_HEADERS = {"MO REAL", "#PROLIQUIDADO", "FECHA LIQUIDACIÓN SGS",
                  "ESTADO FACTURACIÓN", "FECHA ENVIO A FACTURACIÓN"}
# Columnas monetarias que reciben formato de número
_MONEY_HEADERS = {"VALORDIA", "MO PROYECTADA", "MO REAL",
                  "VALOR DEJADO DE EJECUTAR", "VALOR MATERIAL"}

# ── Definición de columnas ────────────────────────────────────────────────────
# (header, campo_model, color_celda_dato)
# color_celda_dato: None = sin color | CELL_INGENIERO | CELL_PROGRAMADOR
_ING  = CELL_INGENIERO
_PROG = CELL_PROGRAMADOR

COLUMNAS_EXCEL = [
    ("ZONA",                     "zona",                  None),
    ("MES",                      "fecha",                 None),   # sub="mes"
    ("DÍA",                      "fecha",                 None),   # sub="dia"
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
    ("GESTOR CLIENTE",              "gestor_cliente",           _PROG),
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

# Anchos de columna (en unidades Excel)
COL_WIDTHS = [8,7,10,12,21,10,16,16,36,26,15,14,14,10,20,14,18,8,15,15,42,20,20,12,14,14,22,14,14,30,15,14,18,20,12,22,12]


def cols_y_anchos(unidad: Optional[str]):
    """Devuelve (columnas, anchos) según la unidad: oculta las columnas que no
    aplican y ajusta la etiqueta del tipo (TIPO GRUPO → TIPO RECURSO en Agobardo)."""
    ocultas    = UNIDAD_OCULTA.get(unidad, set())
    tipo_label = UNIDAD_TIPO_LABEL.get(unidad)
    cols, anchos = [], []
    for (header, field, color), w in zip(COLUMNAS_EXCEL, COL_WIDTHS):
        if field in ocultas:
            continue
        if field == "tipo_grupo" and tipo_label:
            header = tipo_label
        cols.append((header, field, color))
        anchos.append(w)
    return cols, anchos

MESES_ES = {1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",
            7:"julio",8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"}
DIAS_ES  = {0:"lunes",1:"martes",2:"miércoles",3:"jueves",4:"viernes",5:"sábado",6:"domingo"}


def _get_val(prog: Programacion, campo: str, sub: Optional[str] = None):
    if sub == "mes":
        return MESES_ES.get(prog.fecha.month, "") if prog.fecha else ""
    if sub == "dia":
        return DIAS_ES.get(prog.fecha.weekday(), "") if prog.fecha else ""
    val = getattr(prog, campo, None)
    return val.value if hasattr(val, "value") else val


def _header_fill(header: str) -> PatternFill:
    if header in _LIME_HEADERS:
        return PatternFill("solid", fgColor=HEADER_LIME)
    if header in _LBLUE_HEADERS:
        return PatternFill("solid", fgColor=HEADER_LBLUE)
    return PatternFill("solid", fgColor=HEADER_BLUE)


def _write_to_sheet(ws, registros: list, columnas=None, anchos=None):
    """Escribe headers y datos en un worksheet existente con todos los estilos."""
    if columnas is None:
        columnas, anchos = COLUMNAS_EXCEL, COL_WIDTHS
    thin   = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    h_font = Font(bold=True, color="FFFFFF", size=10)
    d_font = Font(size=10)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left   = Alignment(vertical="center", wrap_text=False)

    fill_cache: dict = {}

    def get_fill(hex_color: str) -> PatternFill:
        if hex_color not in fill_cache:
            fill_cache[hex_color] = PatternFill("solid", fgColor=hex_color)
        return fill_cache[hex_color]

    # ── Header row ────────────────────────────────────────────────────────────
    for col_idx, (header, _, _cell_color) in enumerate(columnas, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font      = h_font
        cell.fill      = _header_fill(header)
        cell.alignment = center
        cell.border    = border

    ws.row_dimensions[1].height = 40
    ws.freeze_panes = "A2"

    # ── Data rows ─────────────────────────────────────────────────────────────
    for row_idx, prog in enumerate(registros, 2):
        for col_idx, (header, campo, cell_color) in enumerate(columnas, 1):
            # Determine sub for date columns
            sub = None
            if campo == "fecha":
                if header == "MES":
                    sub = "mes"
                elif header == "DÍA":
                    sub = "dia"

            val  = _get_val(prog, campo, sub)
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.font      = d_font
            cell.border    = border
            cell.alignment = left

            if cell_color:
                cell.fill = get_fill(cell_color)

            if header in _MONEY_HEADERS and val is not None:
                cell.number_format = CURRENCY_FMT

    # ── Column widths ─────────────────────────────────────────────────────────
    for i, w in enumerate(anchos, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _zona_sheet_name(zona_value: str) -> str:
    return f"P. {zona_value}"[:31]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/excel")
async def exportar_excel(
    zona: Optional[ZonaEnum] = None,
    mes: Optional[int] = None,
    anio: Optional[int] = None,
    unidad: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    uni = _unidad_efectiva(current_user, unidad)
    cols, anchos = cols_y_anchos(uni)
    filters = [Programacion.unidad == uni]
    if zona:
        filters.append(Programacion.zona == zona)
    if mes:
        filters.append(func.extract("month", Programacion.fecha) == mes)
    if anio:
        filters.append(func.extract("year", Programacion.fecha) == anio)

    q = select(Programacion).where(and_(*filters)).order_by(Programacion.fecha, Programacion.zona, Programacion.lider)
    registros = (await db.execute(q)).scalars().all()

    zona_label = zona.value if zona else "TODAS"
    sheet_name = _zona_sheet_name(zona_label) if zona else "P. UNIFICADA"
    mes_str    = MESES_ES.get(mes, "todos") if mes else "todos"
    filename   = f"programacion_{uni.lower()}_{zona_label}_{mes_str}_{anio or 'todos'}.xlsx"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    _write_to_sheet(ws, registros, cols, anchos)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/excel/unificado")
async def exportar_excel_unificado(
    mes: Optional[int] = None,
    anio: Optional[int] = None,
    unidad: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Excel con una hoja por zona (P. ALMEIDAS, etc.) + hoja P. UNIFICADA, de la unidad."""
    uni = _unidad_efectiva(current_user, unidad)
    cols, anchos = cols_y_anchos(uni)
    filters = [Programacion.unidad == uni]
    if mes:
        filters.append(func.extract("month", Programacion.fecha) == mes)
    if anio:
        filters.append(func.extract("year", Programacion.fecha) == anio)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # Una hoja por zona
    for zona in ZonaEnum:
        zona_filters = filters + [Programacion.zona == zona]
        q = select(Programacion).where(and_(*zona_filters)).order_by(Programacion.fecha, Programacion.lider)
        registros = (await db.execute(q)).scalars().all()
        if not registros:
            continue
        ws = wb.create_sheet(title=_zona_sheet_name(zona.value))
        _write_to_sheet(ws, registros, cols, anchos)

    # Hoja unificada con todos los registros de la unidad
    q_all = select(Programacion).where(and_(*filters)).order_by(Programacion.zona, Programacion.fecha, Programacion.lider)
    todos = (await db.execute(q_all)).scalars().all()
    ws_uni = wb.create_sheet(title="P. UNIFICADA")
    _write_to_sheet(ws_uni, todos, cols, anchos)

    mes_str  = MESES_ES.get(mes, "todos") if mes else "todos"
    filename = f"programacion_unificada_{uni.lower()}_{mes_str}_{anio or ''}.xlsx"
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/plantilla")
async def descargar_plantilla(
    unidad: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Plantilla Excel vacía (solo encabezados) con el formato exacto de la unidad,
    para llenar masivamente en Excel y luego importar."""
    uni = _unidad_efectiva(current_user, unidad)
    cols, anchos = cols_y_anchos(uni)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PROGRAMACIÓN"
    _write_to_sheet(ws, [], cols, anchos)   # sin registros → solo encabezados

    filename = f"plantilla_programacion_{uni.lower()}.xlsx"
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
