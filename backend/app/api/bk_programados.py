"""Subtab «3.Programados» — apartado independiente del cálculo ANS.

Sube el Excel de Enel (BASE CONTROL_NNDD), aplica las reglas y exporta un .xlsx con la
estructura, los colores y las fórmulas de la hoja «5.2» del ANS oficial.

No comparte estado con el cálculo ANS: sus propias tablas (prog_*), su propia plantilla y
su propio export. Se monta bajo /api/programados (real) y /api/prueba/programados (sandbox);
el aislamiento lo da la columna `entorno`, con el mismo listener que ya usa el módulo ANS.

Regla de oro: la máquina no inventa datos. Lo que no puede resolver lo deja vacío y lo
anota, para que cada decisión se pueda justificar mirando el registro de cargas.
"""
import io
import json
import os
from datetime import datetime, date
from typing import Optional

import openpyxl
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from openpyxl.workbook.properties import CalcProperties
from openpyxl.styles import PatternFill

from app.db.database import get_db
from app.db.ans_ctx import current_entorno
from app.api.deps import get_current_active_user, assert_puede_escribir
from app.models.models import ProgCarga, ProgRegistro, ProgCambio, ProgZonaMap, Usuario
from app.api.ans import FUENTES_DIR, _recalcular_xlsx
from app.services.programados import (
    leer_fuente, indexar_bolsa, leer_zonas, regla_afectacion, periodo_de, norm, txt,
    formulas_fila, fecha_limite, networkdays, tope_de,
    AFECTACION_POR_TIPO, FECHA_DEL_PERIODO, TOPE_CON_AFECTACION, TOPE_SIN_AFECTACION,
)

router = APIRouter(prefix="/programados", tags=["programados"])

PLANTILLA = os.path.join(os.path.dirname(__file__), "..", "templates", "plantilla_programados.xlsx")
HOJA = "5.2"
NARANJA = "FFFFC000"      # marca «esto lo resolvió la máquina desde la bolsa»

# Campo del modelo → (letra de columna en la 5.2, encabezado)
COLUMNAS = [
    ("radicado",       "A"), ("lcl",            "B"), ("eecc",        "C"),
    ("proyecto",       "D"), ("fecha_entrega",  "E"), ("direccion",   "F"),
    ("municipio",      "G"), ("afectacion",     "H"), ("pdl",         "I"),
    ("fecha_maniobra", "J"), ("dias_exclusion", "K"), ("obs_retraso", "L"),
    # M, N y O son fórmulas de la plantilla: nunca se escriben.
    ("zona",           "P"), ("excluible2",     "Q"), ("excluible3",  "R"),
]
# Lo que la persona llena a mano y la máquina no debe pisar nunca.
CAMPOS_MANUALES = ("dias_exclusion", "obs_retraso", "excluible2", "excluible3")
# Lo que llega por importación directa (§4.1 del prompt).
CAMPOS_IMPORT = ("radicado", "lcl", "eecc", "proyecto", "direccion", "municipio")


def _ser(v):
    """Para el historial y el JSON: las fechas a ISO, el resto tal cual. Ojo con no
    convertir a texto los números (`dias_exclusion` es un entero y el front lo edita
    como tal)."""
    if v is None or isinstance(v, (int, float, str)):
        return v
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _txt(v):
    """Versión en texto, para las columnas valor_anterior/valor_nuevo del historial."""
    s = _ser(v)
    return None if s is None else str(s)


def _dic(r: ProgRegistro) -> dict:
    d = {"id": r.id, "periodo": r.periodo, "fila_origen": r.fila_origen, "carga_id": r.carga_id}
    for campo, letra in COLUMNAS:
        d[campo] = _ser(getattr(r, campo))
    d["procedencia"] = json.loads(r.procedencia) if r.procedencia else {}
    d["avisos"] = json.loads(r.avisos) if r.avisos else []
    # Vista previa de las columnas de fórmula (M, N, O): el MISMO cálculo que hará el Excel
    # exportado, festivos incluidos — lo que se ve en pantalla es lo que va a salir.
    d.update(formulas_fila(r.fecha_entrega, r.fecha_maniobra, r.dias_exclusion, r.afectacion))
    return d


async def _mapa_zonas(db) -> dict:
    rows = (await db.execute(select(ProgZonaMap))).scalars().all()
    return {r.municipio: r.zona for r in rows}


# ─────────────────────────────────────────────────────────────────────────────────
#  Subir el Excel de Enel
# ─────────────────────────────────────────────────────────────────────────────────
@router.post("/cargar")
async def cargar(file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                 current_user: Usuario = Depends(get_current_active_user)):
    """Lee el Excel de Enel y aplica §4.1 (importación directa), §4.2 (fecha y PDL, con la
    bolsa como respaldo) y §5 (columnas H y P). Si la estructura no es la esperada, rechaza
    la carga SIN tocar nada de lo ya cargado."""
    assert_puede_escribir(current_user)
    if not file.filename.lower().endswith((".xlsx", ".xlsm", ".xls")):
        raise HTTPException(status_code=400, detail="El archivo debe ser .xlsx")
    contents = await file.read()
    if len(contents) > 60 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El archivo pasa de 60 MB — no parece el BASE CONTROL")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo: {e}")

    try:
        filas, rep = leer_fuente(wb)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not filas:
        raise HTTPException(status_code=400,
                            detail="El archivo no trae ni una fila con radicado. No se tocó nada de lo ya cargado.")

    bolsa = indexar_bolsa(wb)
    zonas = await _mapa_zonas(db)
    # La hoja PROV del propio archivo siembra los municipios que aún no estén en el mapeo.
    for mun, zona in leer_zonas(wb).items():
        if mun not in zonas:
            db.add(ProgZonaMap(municipio=mun, zona=zona))
            zonas[mun] = zona

    # ── Registro de la carga (procedencia) ───────────────────────────────────────
    periodos = [p for p in (periodo_de(f) for f in filas) if p]
    periodo_pred = max(set(periodos), key=periodos.count) if periodos else datetime.now().strftime("%Y-%m")
    carga = ProgCarga(periodo=periodo_pred, nombre=file.filename[:300], tamano=len(contents),
                      hoja=rep["hoja"], filas_leidas=rep["leidas"],
                      usuario_id=getattr(current_user, "id", None))
    db.add(carga)
    await db.flush()

    carpeta = os.path.join(FUENTES_DIR, current_entorno(), "programados", periodo_pred)
    os.makedirs(carpeta, exist_ok=True)
    ruta_abs = os.path.join(carpeta, f"{carga.id}_{file.filename[:120]}")
    with open(ruta_abs, "wb") as fh:
        fh.write(contents)
    carga.ruta = os.path.relpath(ruta_abs, FUENTES_DIR)

    # ── Registros ya existentes, por (periodo, radicado, fila de origen) ─────────
    previos = (await db.execute(select(ProgRegistro))).scalars().all()
    idx_prev = {}
    for r in previos:
        idx_prev.setdefault((r.periodo, r.radicado), []).append(r)

    aplicadas = celdas = 0
    sin_zona, desde_bolsa, reemplazos = set(), 0, 0

    for f in filas:
        periodo = periodo_de(f)
        if not periodo:
            continue
        avisos = list(f["avisos"])
        proc = {}

        # ── §4.1 · importación directa ──────────────────────────────────────────
        valores = {c: f.get(c) for c in CAMPOS_IMPORT}
        for c in CAMPOS_IMPORT:
            if valores[c] is not None:
                proc[c] = "import"

        # ── §4.2 · fecha, PDL y fecha de maniobra ───────────────────────────────
        # Prioridad 1: el propio archivo. Prioridad 2: la bolsa, solo para lo que quede vacío.
        valores["fecha_entrega"] = f.get("fecha_entrega")
        valores["pdl"] = f.get("pdl")
        valores["fecha_maniobra"] = f.get("fecha_maniobra")
        for c in ("fecha_entrega", "pdl", "fecha_maniobra"):
            if valores[c] is not None:
                proc[c] = "import"

        if valores["pdl"] is None or valores["fecha_entrega"] is None:
            b = bolsa.get(f["radicado"])
            if b:
                if valores["pdl"] is None and b.get("pdl"):
                    valores["pdl"] = b["pdl"]
                    proc["pdl"] = "bolsa"
                    desde_bolsa += 1
                    avisos.append(f"PDL tomado de la bolsa (hoja «{b['hoja']}») por radicado")
                if valores["fecha_entrega"] is None and b.get("fecha"):
                    valores["fecha_entrega"] = b["fecha"]
                    proc["fecha_entrega"] = "bolsa"
                    desde_bolsa += 1
                    avisos.append(f"Fecha de entrega tomada de la bolsa (hoja «{b['hoja']}»)")

        # ── §5.1 · columna H, SIEMPRE después de la I ───────────────────────────
        valores["afectacion"] = regla_afectacion(valores["pdl"])
        if valores["afectacion"] is not None:
            proc["afectacion"] = "regla_h"

        # ── §5.2 · columna P por municipio ──────────────────────────────────────
        mun = norm(valores.get("municipio"))
        valores["zona"] = zonas.get(mun)
        if valores["zona"]:
            proc["zona"] = "regla_p"
        elif mun:
            sin_zona.add(mun)
            avisos.append(f"Municipio «{valores['municipio']}» sin zona asignada")

        # ── Alta o actualización ────────────────────────────────────────────────
        candidatos = idx_prev.get((periodo, f["radicado"]), [])
        reg = next((r for r in candidatos if r.fila_origen == f["fila_origen"]), None) \
            or next((r for r in candidatos if r.carga_id != carga.id), None)
        nuevo = reg is None
        if nuevo:
            reg = ProgRegistro(periodo=periodo, radicado=f["radicado"], fila_origen=f["fila_origen"])
            db.add(reg)
            await db.flush()
            idx_prev.setdefault((periodo, f["radicado"]), []).append(reg)

        proc_previa = json.loads(reg.procedencia) if reg.procedencia else {}
        # Si el PDL lo corrigió una persona, la regla de la H se calcula con ESE PDL y no
        # con el del archivo (que abajo queda protegido y no va a entrar).
        if proc_previa.get("pdl") == "manual" and proc_previa.get("afectacion") != "manual":
            valores["afectacion"] = regla_afectacion(reg.pdl)
            if valores["afectacion"] is not None:
                proc["afectacion"] = "regla_h"
        for campo, _letra in COLUMNAS:
            # Lo manual manda: ni los campos siempre-manuales ni una celda corregida a mano
            # (procedencia «manual» del PATCH) se pisan con una carga.
            if campo in CAMPOS_MANUALES or proc_previa.get(campo) == "manual":
                continue
            antes, ahora = getattr(reg, campo), valores.get(campo)
            # Una carga nunca BORRA un dato: solo lo reemplaza con dato real. Cubre el §4.3
            # (el naranja de la bolsa sigue vigente hasta que un archivo traiga el valor de
            # verdad) y también la celda que en el archivo nuevo viene sucia o vacía.
            if ahora is None and antes is not None:
                continue
            if antes == ahora:
                if ahora is not None:
                    proc_previa[campo] = proc.get(campo, proc_previa.get(campo))
                continue
            setattr(reg, campo, ahora)
            celdas += 1
            if not nuevo:
                reemplazos += 1
            db.add(ProgCambio(registro_id=reg.id, carga_id=carga.id, campo=campo,
                              valor_anterior=_txt(antes), valor_nuevo=_txt(ahora),
                              procedencia=proc.get(campo, "import"),
                              motivo=("Reemplaza la decisión anterior de la máquina"
                                      if not nuevo and proc_previa.get(campo) == "bolsa" else None),
                              usuario_id=getattr(current_user, "id", None)))
            proc_previa[campo] = proc.get(campo, "import")   # ahora nunca es None aquí

        reg.procedencia = json.dumps(proc_previa, ensure_ascii=False)
        reg.avisos = json.dumps(avisos[:12], ensure_ascii=False)
        reg.carga_id = carga.id
        aplicadas += 1

    resumen = {
        "hoja": rep["hoja"],
        "periodos": sorted(set(periodos)),
        "sin_radicado": rep["sin_radicado"],
        "fechas_descartadas": rep["fechas_descartadas"],
        "pdl_descartados": rep["pdl_descartados"],
        "municipios_sin_zona": sorted(sin_zona),
        "celdas_desde_bolsa": desde_bolsa,
        "reemplazos": reemplazos,
        "config": {"afectacion_por_tipo": AFECTACION_POR_TIPO, "fecha_del_periodo": FECHA_DEL_PERIODO},
    }
    carga.filas_aplicadas = aplicadas
    carga.celdas_llenas = celdas
    carga.resumen = json.dumps(resumen, ensure_ascii=False)
    await db.commit()

    return {"carga_id": carga.id, "periodo": periodo_pred, "filas_leidas": rep["leidas"],
            "filas_aplicadas": aplicadas, "celdas_llenas": celdas, **resumen}


# ─────────────────────────────────────────────────────────────────────────────────
#  Consulta
# ─────────────────────────────────────────────────────────────────────────────────
@router.get("/registros")
async def registros(periodo: Optional[str] = None, db: AsyncSession = Depends(get_db),
                    _: Usuario = Depends(get_current_active_user)):
    q = select(ProgRegistro).order_by(ProgRegistro.radicado, ProgRegistro.id)
    if periodo:
        q = q.where(ProgRegistro.periodo == periodo)
    filas = (await db.execute(q)).scalars().all()
    sin_zona = sorted({r.municipio for r in filas if r.municipio and not r.zona})
    return {"periodo": periodo, "total": len(filas), "items": [_dic(r) for r in filas],
            "municipios_sin_zona": sin_zona,
            "columnas": [{"campo": c, "col": l} for c, l in COLUMNAS]}


@router.get("/periodos")
async def periodos(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    rows = (await db.execute(
        select(ProgRegistro.periodo, func.count(ProgRegistro.id))
        .group_by(ProgRegistro.periodo).order_by(ProgRegistro.periodo.desc()))).all()
    return [{"periodo": p, "total": n} for p, n in rows]


@router.get("/cargas")
async def cargas(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    filas = (await db.execute(select(ProgCarga).order_by(ProgCarga.creado_en.desc()).limit(60))).scalars().all()
    return [{"id": c.id, "periodo": c.periodo, "nombre": c.nombre, "hoja": c.hoja,
             "tamano": c.tamano, "filas_leidas": c.filas_leidas, "filas_aplicadas": c.filas_aplicadas,
             "celdas_llenas": c.celdas_llenas, "creado_en": _ser(c.creado_en),
             "resumen": json.loads(c.resumen) if c.resumen else {}} for c in filas]


@router.get("/registro/{registro_id}/historial")
async def historial(registro_id: int, db: AsyncSession = Depends(get_db),
                    _: Usuario = Depends(get_current_active_user)):
    """Qué carga puso o quitó cada dato de este registro. Es la justificación del §6."""
    filas = (await db.execute(
        select(ProgCambio).where(ProgCambio.registro_id == registro_id)
        .order_by(ProgCambio.creado_en.desc(), ProgCambio.id.desc()))).scalars().all()
    return [{"id": c.id, "campo": c.campo, "valor_anterior": c.valor_anterior,
             "valor_nuevo": c.valor_nuevo, "procedencia": c.procedencia, "motivo": c.motivo,
             "carga_id": c.carga_id, "creado_en": _ser(c.creado_en)} for c in filas]


# ─────────────────────────────────────────────────────────────────────────────────
#  Edición manual y mapeo de zonas
# ─────────────────────────────────────────────────────────────────────────────────
@router.patch("/registro/{registro_id}")
async def editar(registro_id: int, payload: dict, db: AsyncSession = Depends(get_db),
                 current_user: Usuario = Depends(get_current_active_user)):
    """Edita una celda a mano. Lo manual queda marcado y las cargas posteriores no lo pisan."""
    assert_puede_escribir(current_user)
    reg = (await db.execute(select(ProgRegistro).where(ProgRegistro.id == registro_id))).scalar_one_or_none()
    if not reg:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    campos = {c for c, _ in COLUMNAS}
    proc = json.loads(reg.procedencia) if reg.procedencia else {}
    for campo, valor in payload.items():
        if campo not in campos:
            raise HTTPException(status_code=400, detail=f"Campo no editable: {campo}")
        antes = getattr(reg, campo)
        if campo in ("fecha_entrega", "fecha_maniobra") and valor:
            try:
                valor = datetime.fromisoformat(str(valor)[:10]).date()
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Fecha inválida en {campo}: {valor}")
        if campo == "dias_exclusion" and valor not in (None, ""):
            try:
                valor = int(valor)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="«Días de exclusión» debe ser un número")
        valor = valor if valor not in ("",) else None
        setattr(reg, campo, valor)
        proc[campo] = "manual" if valor is not None else proc.pop(campo, None)
        db.add(ProgCambio(registro_id=reg.id, campo=campo, valor_anterior=_txt(antes),
                          valor_nuevo=_txt(valor), procedencia="manual",
                          motivo="Edición manual", usuario_id=getattr(current_user, "id", None)))
        # La columna H depende de la I: si cambia el PDL a mano, se recalcula.
        if campo == "pdl":
            reg.afectacion = regla_afectacion(valor)
            proc["afectacion"] = "regla_h" if reg.afectacion else None
    reg.procedencia = json.dumps({k: v for k, v in proc.items() if v}, ensure_ascii=False)
    await db.commit()
    return _dic(reg)


@router.get("/zonas")
async def zonas(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    filas = (await db.execute(select(ProgZonaMap).order_by(ProgZonaMap.municipio))).scalars().all()
    return [{"id": z.id, "municipio": z.municipio, "zona": z.zona} for z in filas]


@router.put("/zonas")
async def set_zona(payload: dict, db: AsyncSession = Depends(get_db),
                   current_user: Usuario = Depends(get_current_active_user)):
    """Relaciona un municipio con su zona (o lo desrelaciona con zona = null) y reaplica la
    columna P a los registros de ese municipio, sin tener que volver a subir el Excel."""
    assert_puede_escribir(current_user)
    mun, zona = norm(payload.get("municipio")), (txt(payload.get("zona")) or None)
    if not mun:
        raise HTTPException(status_code=400, detail="Falta el municipio")
    fila = (await db.execute(select(ProgZonaMap).where(ProgZonaMap.municipio == mun))).scalar_one_or_none()
    if fila:
        fila.zona = zona
    else:
        db.add(ProgZonaMap(municipio=mun, zona=zona))
    n = 0
    for r in (await db.execute(select(ProgRegistro))).scalars().all():
        if norm(r.municipio) == mun and r.zona != zona:
            db.add(ProgCambio(registro_id=r.id, campo="zona", valor_anterior=r.zona,
                              valor_nuevo=zona, procedencia="regla_p",
                              motivo=f"Se relacionó «{mun}» con la zona {zona or '(ninguna)'}",
                              usuario_id=getattr(current_user, "id", None)))
            r.zona = zona
            n += 1
    await db.commit()
    return {"municipio": mun, "zona": zona, "registros_actualizados": n}


@router.delete("/periodo/{periodo}")
async def borrar_periodo(periodo: str, db: AsyncSession = Depends(get_db),
                         current_user: Usuario = Depends(get_current_active_user)):
    assert_puede_escribir(current_user)
    ids = (await db.execute(select(ProgRegistro.id).where(ProgRegistro.periodo == periodo))).scalars().all()
    if ids:
        await db.execute(delete(ProgCambio).where(ProgCambio.registro_id.in_(ids)))
    r = await db.execute(delete(ProgRegistro).where(ProgRegistro.periodo == periodo))
    await db.commit()
    return {"periodo": periodo, "borrados": r.rowcount or 0}


# ─────────────────────────────────────────────────────────────────────────────────
#  Indicador 5.2 y proyección (lo que se pinta en la pestaña Indicadores del ANS)
# ─────────────────────────────────────────────────────────────────────────────────
ZONAS_52 = ("UBATE", "ALMEIDAS", "GUAVIO", "LV")
#: Columnas de la dinámica «Cuenta de Radicado» de la hoja 5.2: la afectación de clientes.
COLS_AFECTACION = ("NO", "SI", "(en blanco)")


def _col_afectacion(v) -> str:
    s = (v or "").strip().upper()
    return s if s in ("SI", "NO") else "(en blanco)"


def _estado_plazo(limite: Optional[date], hoy: date) -> dict:
    """Cómo va un plazo respecto de hoy: cuántos días hábiles quedan (sin contar hoy), si
    vence hoy, o cuántos lleva de más. El día del límite todavía cumple."""
    if limite is None:
        return {"limite": None, "estado": "sin fecha de entrega", "dias": None}
    if limite > hoy:
        return {"limite": limite.isoformat(), "estado": "a tiempo",
                "dias": max((networkdays(hoy, limite) or 1) - 1, 0)}
    if limite == hoy:
        return {"limite": limite.isoformat(), "estado": "vence hoy", "dias": 0}
    return {"limite": limite.isoformat(), "estado": "vencido",
            "dias": -max((networkdays(limite, hoy) or 1) - 1, 0)}


@router.get("/indicadores")
async def indicadores(periodo: Optional[str] = None, db: AsyncSession = Depends(get_db),
                      _: Usuario = Depends(get_current_active_user)):
    """El 5.2 con la misma cuenta que hace el Excel: la dinámica Cumple × Afectación, la tabla
    por zona (la misma forma del 3.2 A) y la PROYECCIÓN de lo que todavía no tiene fecha de
    maniobra ni PDL, con la fecha límite de los dos escenarios posibles."""
    q = select(ProgRegistro).order_by(ProgRegistro.zona, ProgRegistro.radicado, ProgRegistro.id)
    if periodo:
        q = q.where(ProgRegistro.periodo == periodo)
    filas = (await db.execute(q)).scalars().all()
    hoy = date.today()

    por_zona = {z: {"cumple": 0, "no_cumple": 0, "exclusiones": 0} for z in ZONAS_52}
    pivote = {"CUMPLE": dict.fromkeys(COLS_AFECTACION, 0), "NO CUMPLE": dict.fromkeys(COLS_AFECTACION, 0)}
    casos, proyeccion, con_pdl = [], [], []
    sin_zona = 0
    resumen_proy = {z: {"con_afectacion": {"a_tiempo": 0, "vencido": 0},
                        "sin_afectacion": {"a_tiempo": 0, "vencido": 0}} for z in ZONAS_52}

    for r in filas:
        f = formulas_fila(r.fecha_entrega, r.fecha_maniobra, r.dias_exclusion, r.afectacion)
        zona = (r.zona or "").strip().upper()
        base = {"id": r.id, "radicado": r.radicado, "proyecto": r.proyecto,
                "municipio": r.municipio, "zona": zona or None, "afectacion": r.afectacion,
                "pdl": r.pdl, "fecha_entrega": _ser(r.fecha_entrega)}

        # ── Medido: tiene las dos fechas, así que el Excel ya le calcula la columna O ──
        if f["cumple"]:
            clave = "cumple" if f["cumple"] == "CUMPLE" else "no_cumple"
            tope = tope_de(r.afectacion)
            pivote[f["cumple"]][_col_afectacion(r.afectacion)] += 1
            if zona in por_zona:
                por_zona[zona][clave] += 1
                # La fila «Exclusiones» de la 5.2 es informativa: no entra en el cumplimiento
                # (la columna O no la mira; lo que descuenta días es la K).
                if (r.excluible2 or "").strip().upper() == "SI" or (r.excluible3 or "").strip().upper() == "SI":
                    por_zona[zona]["exclusiones"] += 1
            else:
                sin_zona += 1
            casos.append({**base, "fecha_maniobra": _ser(r.fecha_maniobra),
                          "dias": f["dias_con_exclusion"], "dias_ejecucion": f["dias_ejecucion"],
                          "dias_exclusion": r.dias_exclusion, "tope": tope,
                          "exceso": max(f["dias_con_exclusion"] - tope, 0), "cumple": f["cumple"],
                          # Maniobra anterior a la entrega: el Excel lo da por CUMPLE (el <= se
                          # cumple de sobra), pero es un dato al revés y hay que mirarlo.
                          "invertido": f["dias_ejecucion"] < 0,
                          "obs_retraso": r.obs_retraso})
            continue

        # ── Sin fecha de maniobra: todavía no se puede medir, se PROYECTA ──
        if r.fecha_maniobra is not None:
            continue                                  # tiene maniobra pero le falta la entrega
        if txt(r.pdl):
            # Con PDL ya se sabe si hay afectación → un solo plazo, el suyo.
            tope = tope_de(r.afectacion)
            con_pdl.append({**base, "tope": tope,
                            **_estado_plazo(fecha_limite(r.fecha_entrega, tope), hoy)})
            continue
        # Sin maniobra y sin PDL: no se sabe si tendrá afectación → los DOS escenarios.
        con = _estado_plazo(fecha_limite(r.fecha_entrega, TOPE_CON_AFECTACION), hoy)
        sin = _estado_plazo(fecha_limite(r.fecha_entrega, TOPE_SIN_AFECTACION), hoy)
        proyeccion.append({**base, "con_afectacion": con, "sin_afectacion": sin})
        if zona in resumen_proy:
            for k, e in (("con_afectacion", con), ("sin_afectacion", sin)):
                if e["estado"] in ("a tiempo", "vence hoy"):
                    resumen_proy[zona][k]["a_tiempo"] += 1
                elif e["estado"] == "vencido":
                    resumen_proy[zona][k]["vencido"] += 1

    medidos = sum(v["cumple"] + v["no_cumple"] for v in por_zona.values()) + sin_zona
    cumplen = sum(pivote["CUMPLE"].values())
    return {
        "periodo": periodo, "total": len(filas), "medidos": medidos, "sin_zona": sin_zona,
        "cumplimiento": (cumplen / medidos) if medidos else None,
        "por_zona": por_zona,
        "pivote": {"columnas": list(COLS_AFECTACION), "filas": pivote,
                   "total_col": {c: sum(pivote[f][c] for f in pivote) for c in COLS_AFECTACION},
                   "total": medidos},
        "casos": casos,
        "proyeccion": {"hoy": hoy.isoformat(), "filas": proyeccion, "con_pdl": con_pdl,
                       "resumen": resumen_proy,
                       "topes": {"con_afectacion": TOPE_CON_AFECTACION,
                                 "sin_afectacion": TOPE_SIN_AFECTACION}},
    }


# ─────────────────────────────────────────────────────────────────────────────────
#  Exportación
# ─────────────────────────────────────────────────────────────────────────────────
def _expandir_tabla(ws, n_filas):
    """Ajusta el `ref` de Table25 al número real de filas. Sin esto las fórmulas de M, N y O
    no se propagan y el cálculo sale mal en silencio."""
    t = (ws.tables or {}).get("Table25")
    if t is None:
        return
    nuevo = f"A1:R{max(n_filas, 1) + 1}"
    if isinstance(t, str):
        ws.tables["Table25"] = nuevo
    else:
        t.ref = nuevo


def _construir_xlsx(filas) -> bytes:
    wb = openpyxl.load_workbook(PLANTILLA)
    ws = wb[HOJA]
    naranja = PatternFill("solid", fgColor=NARANJA)

    # La plantilla trae 200 filas de fórmulas: se recortan o amplían a las que haya.
    plantilla_filas = ws.max_row - 1
    formulas = {L: ws[f"{L}2"].value for L in ("M", "N", "O")}

    for i, r in enumerate(filas, start=2):
        proc = json.loads(r.procedencia) if r.procedencia else {}
        for campo, L in COLUMNAS:
            c = ws[f"{L}{i}"]
            c.value = getattr(r, campo)
            # Naranja = lo resolvió la máquina desde la bolsa, y solo mientras siga vigente.
            if proc.get(campo) == "bolsa":
                c.fill = naranja
        for L, f in formulas.items():
            ws[f"{L}{i}"] = f

    n = len(filas)
    if n < plantilla_filas:                       # limpia las filas sobrantes de la plantilla
        ws.delete_rows(n + 2, plantilla_filas - n)
    elif n > plantilla_filas:                     # replica las fórmulas en las filas nuevas
        for i in range(plantilla_filas + 2, n + 2):
            for L, f in formulas.items():
                ws[f"{L}{i}"] = f
    _expandir_tabla(ws, n)

    wb.calculation = CalcProperties(fullCalcOnLoad=True)
    buf = io.BytesIO()
    wb.save(buf)
    return _recalcular_xlsx(buf.getvalue())


@router.get("/export")
async def exportar(periodo: Optional[str] = None, db: AsyncSession = Depends(get_db),
                   _: Usuario = Depends(get_current_active_user)):
    """El .xlsx con la estructura, los colores y las fórmulas de la 5.2. Sin datos entrega la
    plantilla vacía, que es el resultado esperado."""
    q = select(ProgRegistro).order_by(ProgRegistro.radicado, ProgRegistro.id)
    if periodo:
        q = q.where(ProgRegistro.periodo == periodo)
    filas = (await db.execute(q)).scalars().all()
    data = _construir_xlsx(filas)
    nombre = f"Programados_{periodo or 'todo'}{'' if current_entorno() == 'real' else '_PRUEBA'}.xlsx"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
