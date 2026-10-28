import calendar
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from app.db.database import get_db
from app.api.deps import get_current_active_user, ROLES_GLOBALES, unidad_de
from app.models.models import (
    Programacion, Usuario, RolEnum, UNIDAD_DEFAULT,
    DimensionCuadrilla, IntegranteCuadrilla,
)
from app.schemas.schemas import DimensionUpsert, IntegranteCreate, IntegranteUpdate, IntegranteOut
from app.services.festivos import es_no_laborable

router = APIRouter(prefix="/produccion", tags=["produccion"])

# Roles que pueden RELLENAR estos datos de planeación (vista Mónica).
# Nota: aquí el directivo SÍ edita (son sus instrumentos), aunque sea de solo
# lectura en la programación.
ROLES_EDIT = {RolEnum.supervisor, RolEnum.directivo, RolEnum.desarrollador}


def _unidad_efectiva(user: Usuario, unidad: Optional[str]) -> str:
    if user.rol in ROLES_GLOBALES:
        return unidad or UNIDAD_DEFAULT
    return unidad_de(user)


def _assert_edit(user: Usuario):
    if user.rol not in ROLES_EDIT:
        raise HTTPException(status_code=403, detail="Sin permiso para editar estos datos")


# ── Dimensionamiento: requeridas (manual) vs activas (de la programación) ────────

@router.get("/dimension")
async def get_dimension(
    unidad: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    uni = _unidad_efectiva(current_user, unidad)

    # Activas = nº de cuadrillas (líderes distintos) por zona y tipo en la programación
    aq = (
        select(
            Programacion.zona, Programacion.tipo_grupo,
            func.count(func.distinct(Programacion.lider)).label("n"),
        )
        .where(Programacion.unidad == uni)
        .group_by(Programacion.zona, Programacion.tipo_grupo)
    )
    activas = [
        {"zona": r.zona.value if r.zona else None, "tipo_grupo": r.tipo_grupo, "n": r.n}
        for r in (await db.execute(aq)).all()
        if r.tipo_grupo
    ]

    rq = select(DimensionCuadrilla).where(DimensionCuadrilla.unidad == uni)
    requeridas = [
        {"zona": d.zona, "tipo_grupo": d.tipo_grupo, "requeridas": d.requeridas}
        for d in (await db.execute(rq)).scalars().all()
    ]
    return {"unidad": uni, "activas": activas, "requeridas": requeridas}


@router.put("/dimension")
async def upsert_dimension(
    req: DimensionUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    _assert_edit(current_user)
    uni = _unidad_efectiva(current_user, req.unidad)
    existing = (await db.execute(
        select(DimensionCuadrilla).where(and_(
            DimensionCuadrilla.unidad == uni,
            DimensionCuadrilla.zona == req.zona,
            DimensionCuadrilla.tipo_grupo == req.tipo_grupo,
        ))
    )).scalar_one_or_none()
    if existing:
        existing.requeridas = req.requeridas
    else:
        db.add(DimensionCuadrilla(unidad=uni, zona=req.zona, tipo_grupo=req.tipo_grupo, requeridas=req.requeridas))
    await db.commit()
    return {"ok": True, "unidad": uni, "zona": req.zona, "tipo_grupo": req.tipo_grupo, "requeridas": req.requeridas}


# ── Planner de recursos: matriz mensual con semáforo capacidad vs programado ─────

# Los tipos de cuadrilla vienen con grafías mezcladas en la programación
# (CMAP5/CAMP5, ESTACA/ESTACAS…). Se normalizan a la grafía del cliente.
_TIPO_CANON = {
    "CMAP5": "CMAP5", "CAMP5": "CMAP5",
    "CMAP3": "CMAP3", "CAMP3": "CMAP3", "CMAM3": "CMAP3",
    "ARRIME": "ARRIME",
    "GRUA": "GRUA", "GRÚA": "GRUA",
    "ESTACA": "ESTACA", "ESTACAS": "ESTACA",
    "LVL3": "LVL3", "LV3": "LVL3",
    "MIE": "MIE",
}

def _canon_tipo(t: Optional[str]) -> str:
    return _TIPO_CANON.get((t or "").strip().upper(), "OTROS")


def _situacion(rows: list[Programacion]) -> str:
    """Cómo está el líder ese día según sus filas:
    'trabajo'  = al menos una fila con contenido real (cuenta para el semáforo)
    'descanso' = solo filas DESCANSO
    'vacio'    = filas generadas sin diligenciar → recurso SIN programar (rojo)."""
    descanso = False
    for r in rows:
        proc = (r.proceso or "").strip().upper()
        if proc == "DESCANSO":
            descanso = True
            continue
        if proc or (r.num_pdt or "").strip() or (r.proyecto_antecedente or "").strip() or (r.alcance or "").strip():
            return "trabajo"
    return "descanso" if descanso else "vacio"


@router.get("/planner")
async def get_planner(
    mes: int = Query(..., ge=1, le=12),
    anio: int = Query(..., ge=2024, le=2035),
    unidad: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Resumen visual de la programación del mes: por zona y día, cuántas cuadrillas
    de cada tipo están programadas vs la capacidad (Dimensionamiento), MO proyectada
    diaria con sus responsables, y distribución del estado PDT.
    Regla: un líder repetido en varios trabajos el mismo día cuenta UNA sola vez."""
    uni = _unidad_efectiva(current_user, unidad)

    # Capacidad (tabla editable en Dimensionamiento), normalizada
    cap_rows = (await db.execute(
        select(DimensionCuadrilla).where(DimensionCuadrilla.unidad == uni)
    )).scalars().all()
    capacidad: dict = {}
    for d in cap_rows:
        if d.requeridas and d.requeridas > 0:
            z = (d.zona or "").strip()
            capacidad.setdefault(z, {})
            t = _canon_tipo(d.tipo_grupo)
            capacidad[z][t] = capacidad[z].get(t, 0) + d.requeridas

    ndias = calendar.monthrange(anio, mes)[1]
    fechas = [date(anio, mes, d) for d in range(1, ndias + 1)]
    dias = [{"fecha": f.isoformat(), "dia": f.day, "no_laborable": es_no_laborable(f)} for f in fechas]

    progs = (await db.execute(
        select(Programacion).where(and_(
            Programacion.unidad == uni,
            func.extract("month", Programacion.fecha) == mes,
            func.extract("year", Programacion.fecha) == anio,
        ))
    )).scalars().all()

    # Agrupar: zona → fecha → líder → sus filas del día
    por_dia: dict = {}
    for p in progs:
        if not p.fecha or not p.lider:
            continue
        z = p.zona.value if p.zona else "?"
        por_dia.setdefault(z, {}).setdefault(p.fecha.isoformat(), {}).setdefault(p.lider.strip(), []).append(p)

    zonas_out: dict = {}
    for z, dias_z in por_dia.items():
        out_dias = {}
        for fiso, lideres in dias_z.items():
            tipos: dict = {}
            mo_total = 0.0
            estado_pdt: dict = {}
            for lider, rows in lideres.items():
                t = _canon_tipo(next((r.tipo_grupo for r in rows if r.tipo_grupo), None))
                sit = _situacion(rows)
                mo = sum(r.mo_proyectada or 0 for r in rows)
                mo_total += mo
                estados = sorted({(r.estado_pdt or "").strip().upper() for r in rows if (r.estado_pdt or "").strip()})
                for e in estados:
                    estado_pdt[e] = estado_pdt.get(e, 0) + 1
                info = tipos.setdefault(t, {"programados": 0, "descanso": 0, "sin_programar": 0, "lideres": []})
                if sit == "trabajo":     info["programados"] += 1
                elif sit == "descanso":  info["descanso"] += 1
                else:                    info["sin_programar"] += 1
                info["lideres"].append({
                    "lider": lider, "situacion": sit, "mo": mo,
                    "estados": estados, "trabajos": len(rows),
                })
            for info in tipos.values():
                info["lideres"].sort(key=lambda x: (x["situacion"] != "trabajo", -x["mo"], x["lider"]))
            out_dias[fiso] = {"tipos": tipos, "mo_total": mo_total, "estado_pdt": estado_pdt}
        zonas_out[z] = out_dias

    return {
        "unidad": uni, "mes": mes, "anio": anio,
        "capacidad": capacidad,
        "dias": dias,
        "zonas": zonas_out,
    }


# ── Roster / costos: integrantes de cuadrilla ────────────────────────────────────

@router.get("/integrantes", response_model=list[IntegranteOut])
async def listar_integrantes(
    unidad: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    uni = _unidad_efectiva(current_user, unidad)
    q = (
        select(IntegranteCuadrilla)
        .where(IntegranteCuadrilla.unidad == uni)
        .order_by(IntegranteCuadrilla.zona, IntegranteCuadrilla.codigo_cuadrilla, IntegranteCuadrilla.cargo)
    )
    return (await db.execute(q)).scalars().all()


@router.post("/integrantes", response_model=IntegranteOut)
async def crear_integrante(
    req: IntegranteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    _assert_edit(current_user)
    data = req.model_dump()
    data["unidad"] = _unidad_efectiva(current_user, req.unidad)
    obj = IntegranteCuadrilla(**data)
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.patch("/integrantes/{item_id}", response_model=IntegranteOut)
async def actualizar_integrante(
    item_id: int,
    req: IntegranteUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    _assert_edit(current_user)
    obj = (await db.execute(select(IntegranteCuadrilla).where(IntegranteCuadrilla.id == item_id))).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="No encontrado")
    if current_user.rol not in ROLES_GLOBALES and obj.unidad != unidad_de(current_user):
        raise HTTPException(status_code=403, detail="Sin acceso a esa unidad")
    for k, v in req.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.delete("/integrantes/{item_id}")
async def eliminar_integrante(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    _assert_edit(current_user)
    obj = (await db.execute(select(IntegranteCuadrilla).where(IntegranteCuadrilla.id == item_id))).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="No encontrado")
    if current_user.rol not in ROLES_GLOBALES and obj.unidad != unidad_de(current_user):
        raise HTTPException(status_code=403, detail="Sin acceso a esa unidad")
    await db.delete(obj)
    await db.commit()
    return {"ok": True}
