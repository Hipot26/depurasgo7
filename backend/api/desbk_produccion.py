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
