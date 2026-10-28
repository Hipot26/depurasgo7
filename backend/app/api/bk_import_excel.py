"""
Endpoint de importación de Excel.
Solo accesible para el Jefe de Zona (supervisor).
Lógica inteligente: evita duplicados por (zona, fecha, lider).
"""
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, delete, String as _SAString, Enum as _SAEnum
from typing import Optional
from datetime import date, datetime, time
import io
import logging
import re
import openpyxl

from app.db.database import get_db
from app.api.deps import get_current_active_user, ROLES_GLOBALES, unidad_de
from app.config import puede_importar_prog as _puede_importar_prog, importa_prog_completo as _importa_prog_completo
from app.models.models import Usuario, Programacion

router = APIRouter(prefix="/import", tags=["import"])
log = logging.getLogger(__name__)

def _norm(s: str) -> str:
    return (s or "").strip().lower()\
        .replace("á","a").replace("é","e").replace("í","i").replace("ó","o").replace("ú","u")

COL_MAP = {
    "zona":                        "zona",
    "fecha":                       "fecha",
    "lider":                       "lider",
    "tipo grupo":                  "tipo_grupo",
    "tipo recurso":                "tipo_grupo",
    "proceso":                     "proceso",
    "subproceso":                  "subproceso",
    "proyecto o antecedente":      "proyecto_antecedente",
    "pms":                         "pms",
    "circuito":                    "circuito",
    "lcl":                         "lcl",
    "fecha vigencia":              "fecha_vigencia",
    "tipo pdt":                    "tipo_pdt",
    "# pdt":                       "num_pdt",
    "estado":                      "estado",
    "cd afectados":                "cd_afectados",
    "saidi":                       "saidi",
    "hora":                        "hora",
    "hora ab":                     "hora_ab",
    "hora cr":                     "hora_cr",
    "municipio":                   "municipio",
    "vda":                         "vereda",
    "alcance":                     "alcance",
    "gestor enel":                 "gestor_cliente",
    "gestor cliente":              "gestor_cliente",
    "gestor":                      "gestor_cliente",
    "supervisor":                  "supervisor",
    "valordia":                    "valor_dia",
    "valor dia":                   "valor_dia",
    "mo proyectada":               "mo_proyectada",
    "moproyectada":                "mo_proyectada",
    "mo real":                     "mo_real",
    "mo facturada":                "mo_facturada",
    "mofacturada":                 "mo_facturada",
    "valor dejado de ejecutar":    "valor_dejado_ejecutar",
    "valor material":              "valor_material",
    "valormaterial":               "valor_material",
    "estado pdt":                  "estado_pdt",
    "notas":                       "notas",
    "restricciones":               "restricciones",
    "se gestiona si o no":         "se_gestiona",
    "se gestiona":                 "se_gestiona",
    "#proliquidado":               "num_proliquidado",
    "fecha liquidacion sgs":       "fecha_liquidacion_sgs",
    "estado facturcion":           "estado_facturacion",
    "estado facturacion":          "estado_facturacion",
    "fecha envio a factuacion":    "fecha_envio_facturacion",
    "fecha envio a facturacion":   "fecha_envio_facturacion",
    "ot":                          "ot",
    "estado cumplimentacion":      "estado_cumplimentacion",
    "inoperatividad":              "inoperatividad",
    "observaciones inoperatividad":"observaciones_inoperatividad",
}
SKIP            = {"mes", "dia"}
NUMERIC_FIELDS  = {"valor_dia","mo_proyectada","mo_real","mo_facturada","valor_dejado_ejecutar","valor_material","saidi"}
DATE_FIELDS     = {"fecha","fecha_vigencia","fecha_liquidacion_sgs","fecha_envio_facturacion"}
HORA_FIELDS     = {"hora","hora_ab","hora_cr"}
VALID_ZONAS     = {"ALMEIDAS","GUAVIO","UBATE","LV&TLC"}
VALID_ESTADOS   = {"PROGRAMADO","EJECUTADO","PARCIAL","CANCELADO","REPROGRAMADO"}
VALID_TIPOS     = {"CAMP5","CAMP3","ARRIME","GRUA","ESTACA","LVL3","MIE"}
VALID_EPDT      = {"PENDIENTE","ENTREGADO_FACTURACION","LIQUIDADO_SGS","RECHAZADO"}


def _to_str(v) -> Optional[str]:
    if v is None: return None
    s = str(v).strip(); return s or None

def _to_date(v) -> Optional[date]:
    if v is None: return None
    if isinstance(v, datetime): return v.date()
    if isinstance(v, date):     return v
    for fmt in ("%Y-%m-%d","%d/%m/%Y","%d-%m-%Y"):
        try: return datetime.strptime(str(v).strip(), fmt).date()
        except: pass
    return None

def _to_num(v) -> Optional[float]:
    if v is None: return None
    try: return float(v)
    except: return None

def _to_hora(v) -> Optional[str]:
    """Hora en formato HH:MM. Acepta time/datetime de Excel, fracción de día (0.33) o texto."""
    if v is None: return None
    if isinstance(v, datetime): v = v.time()
    if isinstance(v, time):     return v.strftime("%H:%M")
    if isinstance(v, (int, float)) and 0 <= v < 1:   # fracción de día de Excel
        total = round(v * 24 * 60)
        return f"{total // 60:02d}:{total % 60:02d}"
    s = str(v).strip()
    return s[:5] if len(s) >= 5 and s[2] == ":" else (s or None)


# Largo máximo de cada columna, leído del modelo (String(n)); el texto libre (Text) no tiene tope (ver CHANGELOG 2026-10-20).
LARGO_AVISO = 400   # PROYECTO O ANTECEDENTE más largo que esto entra completo, pero se avisa para revisarlo
_ENCABEZADO = {f: h.upper() for h, f in reversed(list(COL_MAP.items()))}   # campo → primer encabezado de COL_MAP
_ENCABEZADO["inoperatividad_tipo"] = "INOPERATIVIDAD"


def _limites() -> dict:
    return {c.name: c.type.length for c in Programacion.__table__.columns
            if isinstance(c.type, _SAString) and not isinstance(c.type, _SAEnum) and c.type.length}


def _revisar_largos(records: list[dict]) -> tuple[list[str], list[str]]:
    """(celdas que no caben en su columna → se rechaza el archivo; avisos de PROYECTO O ANTECEDENTE muy largo → entra)."""
    lim, largos, avisos = _limites(), [], []
    for rec in records:
        fila = rec.get("_fila", "?")
        for campo, v in rec.items():
            if not isinstance(v, str):
                continue
            if campo in lim and len(v) > lim[campo]:
                largos.append(f"fila {fila} · {_ENCABEZADO.get(campo, campo.upper())}: "
                              f"{len(v)} caracteres (máximo {lim[campo]})")
            elif campo == "proyecto_antecedente" and len(v) > LARGO_AVISO:
                avisos.append(f"Fila {fila}: PROYECTO O ANTECEDENTE con {len(v)} caracteres — entró completo; "
                              "revise que sea el texto correcto")
    return largos, avisos


def parse_workbook(wb: openpyxl.Workbook) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    records: list[dict] = []

    # Buscar la hoja principal: la primera que tenga columnas FECHA y LIDER en la fila 1.
    # (La plantilla 2026-08 trae una hoja de catálogos «PROCESOS» de primera — se salta sola,
    #  igual que TELECONTROL, RESUMEN u Hoja1.)
    sheet = headers = None
    for name in wb.sheetnames:
        ws = wb[name]
        hdrs = [_norm(c.value) for c in next(ws.iter_rows(min_row=1, max_row=1))]
        if "fecha" in hdrs and "lider" in hdrs:
            sheet, headers = ws, hdrs
            break

    if sheet is None:
        errors.append("Ninguna hoja tiene columnas FECHA y LIDER en la fila 1 — no sé cuál importar")
        return records, errors

    fecha_idx = headers.index("fecha")

    for row_num, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        if fecha_idx >= len(row) or row[fecha_idx] is None:
            continue
        rec: dict = {}
        for i, val in enumerate(row):
            h = headers[i] if i < len(headers) else None
            if not h or h in SKIP: continue
            field = COL_MAP.get(h)
            if not field: continue
            if field == "inoperatividad":
                # Doble sentido: CHOCONTA/Mantilla = valor $ (número);
                # plantilla SABOGAL 2026-08 = recurso inoperativo (texto: GRUA, PERSONAL…)
                num = _to_num(val)
                if num is not None:          rec["inoperatividad"] = num
                else:                        rec["inoperatividad_tipo"] = _to_str(val)
            elif field in DATE_FIELDS:    rec[field] = _to_date(val)
            elif field in HORA_FIELDS:    rec[field] = _to_hora(val)
            elif field in NUMERIC_FIELDS: rec[field] = _to_num(val)
            else:                        rec[field] = _to_str(val)

        if not rec.get("fecha"): continue

        zona = (rec.get("zona") or "").upper().strip()
        if zona not in VALID_ZONAS:
            errors.append(f"Fila {row_num}: zona '{zona}' inválida — omitida"); continue
        rec["zona"] = zona

        # estado, tipo_grupo y estado_pdt son TEXTO LIBRE (multi-unidad):
        # se conservan tal cual (SABOGAL: CAMP5/APROBADO… · CHOCONTA: MOE2/TAI…)
        rec["estado"]     = (rec.get("estado") or "").strip().upper() or None
        rec["tipo_grupo"] = (rec.get("tipo_grupo") or "").strip().upper() or None
        rec["estado_pdt"] = (rec.get("estado_pdt") or "").strip().upper() or None

        rec["_fila"] = row_num   # para nombrar la fila en los avisos (no se guarda)
        records.append(rec)

    return records, errors


@router.post("/excel")
async def importar_excel(
    file: UploadFile = File(...),
    mode: str = Form("smart"),   # smart | replace_zona | replace_all
    unidad: Optional[str] = Form(None),   # unidad elegida en el selector (solo roles globales)
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    # Apoyos de USUARIOS_APOYO_IMPORTA_PROG (ver CHANGELOG 2026-10-19): importan SU zona (usuarios.zona) y solo en
    # modo Inteligente. Reemplazar zona / todo son del coordinador y del desarrollador (CHANGELOG 2026-10-21).
    zona_propia = None
    if not _importa_prog_completo(current_user.rol):
        if not _puede_importar_prog(current_user.rol, current_user.email) or not current_user.zona:
            raise HTTPException(status_code=403, detail="Solo el coordinador puede importar")
        if mode != "smart":
            raise HTTPException(status_code=403, detail="Su cuenta solo importa en modo Inteligente "
                                "(agrega filas nuevas y omite duplicados)")
        zona_propia = str(getattr(current_user.zona, "value", current_user.zona))

    # Unidad destino: los coordinadores (rol global desde 2026-09-07) importan a la unidad
    # que tengan seleccionada; cualquier otro, solo a la suya. Nunca a una unidad desconocida.
    if current_user.rol in ROLES_GLOBALES and unidad in ("SABOGAL", "CHOCONTA"):
        uni = unidad
    else:
        uni = unidad_de(current_user)

    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="El archivo debe ser .xlsx o .xls")

    contents = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo: {e}")

    records, parse_errors = parse_workbook(wb)
    if not records:
        return {"inserted": 0, "skipped": 0, "errors": parse_errors}

    zonas_en_archivo = {r["zona"] for r in records}
    if zona_propia is not None:
        ajenas = sorted(z for z in zonas_en_archivo if z != zona_propia)
        if ajenas:   # se rechaza entero, antes de tocar la base
            raise HTTPException(status_code=403, detail=(
                f"El archivo trae filas de {', '.join(ajenas)}. Su cuenta solo importa la zona "
                f"{zona_propia}: quite esas filas y vuelva a importar. No se importó nada."))

    # Una celda más larga que su columna tumbaba la importación entera con un 500 sin explicación
    # (ver CHANGELOG 2026-10-20). Se revisa antes de tocar la base; PROYECTO O ANTECEDENTE (Text) entra completo y se avisa.
    largos, avisos = _revisar_largos(records)
    if largos:
        raise HTTPException(status_code=400, detail=(
            f"No se importó nada: {len(largos)} celda(s) pasan del largo que admite su columna. "
            + "; ".join(largos[:5]) + ("…" if len(largos) > 5 else "") + ". Recórtelas y vuelva a importar."))
    parse_errors = avisos + parse_errors

    # «Reemplazar todo» borra TODAS las zonas de la unidad; si el archivo no las trae todas, las que falten quedan
    # en cero (2026-10-20: una carga de una sola zona en «Reemplazar todo» dejó otra vacía).
    if mode == "replace_all":
        en_base = (await db.execute(
            select(Programacion.zona).where(Programacion.unidad == uni).distinct())).scalars().all()
        faltan = sorted({str(getattr(z, "value", z)) for z in en_base} - zonas_en_archivo)
        if faltan:
            raise HTTPException(status_code=409, detail=(
                f"«Reemplazar todo» borraría {', '.join(faltan)}, que no vienen en este archivo "
                f"(trae {', '.join(sorted(zonas_en_archivo))}). Para cargar solo esas zonas use «Reemplazar zona». "
                "No se importó nada."))

    # ── Modos de limpieza (SIEMPRE acotados a la unidad del importador) ─────────
    if mode == "replace_all":
        await db.execute(delete(Programacion).where(Programacion.unidad == uni))
        await db.flush()

    elif mode == "replace_zona":
        for z in zonas_en_archivo:
            await db.execute(
                delete(Programacion).where(and_(Programacion.zona == z, Programacion.unidad == uni))
            )
        await db.flush()

    # ── Importación inteligente: skip duplicados (zona+fecha+lider) en la unidad ─
    inserted = skipped = 0

    if mode == "smart":
        existing_q = await db.execute(
            select(Programacion.zona, Programacion.fecha, Programacion.lider)
            .where(and_(Programacion.zona.in_(list(zonas_en_archivo)), Programacion.unidad == uni))
        )
        existing_keys = {(r.zona, r.fecha, r.lider) for r in existing_q.all()}
    else:
        existing_keys = set()

    for rec in records:
        key = (rec.get("zona"), rec.get("fecha"), rec.get("lider"))
        if mode == "smart" and key in existing_keys:
            skipped += 1
            continue
        db.add(Programacion(**{k: v for k, v in rec.items() if v is not None and k != "_fila"},
                            unidad=uni, creado_por=current_user.id))
        inserted += 1

    try:
        await db.commit()
    except Exception as e:   # nunca un 500 mudo; la transacción se deshace entera
        await db.rollback()
        log.exception("importar_excel: la base rechazó %s", file.filename)
        causa = re.sub(r"^<class '[^']*'>:\s*", "", str(getattr(e, "orig", e)).strip().splitlines()[-1])[:200]
        raise HTTPException(status_code=500, detail=(
            "No se importó nada: la base rechazó el archivo y la programación quedó como estaba. "
            f"Detalle: {causa}"))

    return {
        "inserted": inserted,
        "skipped":  skipped,
        "errors":   parse_errors[:20],   # max 20 errores reportados
    }
