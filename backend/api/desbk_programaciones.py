from datetime import date
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from app.db.database import get_db
from app.api.deps import (
    get_current_active_user, unidad_filtros, unidad_de,
    assert_puede_escribir, ROLES_GLOBALES,
)
from app.models.models import Programacion, Usuario, RolEnum, ZonaEnum, EstadoEnum, UNIDAD_DEFAULT
from app.schemas.schemas import ProgramacionCreate, ProgramacionUpdate, ProgramacionOut, ProgramacionListResponse
from app.services.auth import CAMPO_PERMISOS

router = APIRouter(prefix="/programaciones", tags=["programaciones"])


def _check_zona_access(user: Usuario, zona: ZonaEnum):
    if user.rol in (RolEnum.supervisor, *ROLES_GLOBALES):
        return
    if user.zona and user.zona != zona:
        raise HTTPException(status_code=403, detail="Sin acceso a esa zona")


def _check_unidad_access(user: Usuario, unidad: str | None):
    if user.rol in ROLES_GLOBALES:
        return
    if (unidad or UNIDAD_DEFAULT) != unidad_de(user):
        raise HTTPException(status_code=403, detail="Sin acceso a esa unidad")


@router.get("", response_model=ProgramacionListResponse)
async def listar(
    zona: Optional[ZonaEnum] = None,
    unidad: Optional[str] = None,
    fecha_inicio: Optional[date] = None,
    fecha_fin: Optional[date] = None,
    lider: Optional[str] = None,
    estado: Optional[EstadoEnum] = None,
    mes: Optional[int] = None,
    anio: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(500, le=2000),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    # Scoping por unidad (cada unidad ve solo lo suyo; globales ven todo / ?unidad=)
    filters = unidad_filtros(current_user, unidad)
    # Ingenieros solo ven su zona
    if current_user.rol == RolEnum.ingeniero_zona and current_user.zona:
        filters.append(Programacion.zona == current_user.zona)
    elif zona:
        filters.append(Programacion.zona == zona)

    if fecha_inicio:
        filters.append(Programacion.fecha >= fecha_inicio)
    if fecha_fin:
        filters.append(Programacion.fecha <= fecha_fin)
    if lider:
        filters.append(Programacion.lider.ilike(f"%{lider}%"))
    if estado:
        filters.append(Programacion.estado == estado)
    if mes:
        filters.append(func.extract("month", Programacion.fecha) == mes)
    if anio:
        filters.append(func.extract("year", Programacion.fecha) == anio)

    count_q = select(func.count()).select_from(Programacion)
    if filters:
        count_q = count_q.where(and_(*filters))
    total = (await db.execute(count_q)).scalar()

    q = select(Programacion).order_by(Programacion.fecha, Programacion.zona, Programacion.lider)
    if filters:
        q = q.where(and_(*filters))
    q = q.offset(skip).limit(limit)
    items = (await db.execute(q)).scalars().all()

    return ProgramacionListResponse(total=total, items=items)


@router.post("", response_model=ProgramacionOut)
async def crear(
    req: ProgramacionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    assert_puede_escribir(current_user)
    _check_zona_access(current_user, req.zona)
    data = req.model_dump()
    # La unidad la fija el usuario que crea; los roles globales pueden indicarla.
    if current_user.rol in ROLES_GLOBALES:
        data["unidad"] = req.unidad or UNIDAD_DEFAULT
    else:
        data["unidad"] = unidad_de(current_user)
    prog = Programacion(**data, creado_por=current_user.id)
    db.add(prog)
    await db.commit()
    await db.refresh(prog)
    return prog


@router.patch("/{prog_id}", response_model=ProgramacionOut)
async def actualizar(
    prog_id: int,
    req: ProgramacionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    assert_puede_escribir(current_user)
    result = await db.execute(select(Programacion).where(Programacion.id == prog_id))
    prog = result.scalar_one_or_none()
    if not prog:
        raise HTTPException(status_code=404, detail="No encontrado")
    _check_unidad_access(current_user, prog.unidad)
    _check_zona_access(current_user, prog.zona)

    # Validar que el rol solo toque sus campos
    campos_permitidos = CAMPO_PERMISOS.get(current_user.rol, [])
    update_data = req.model_dump(exclude_unset=True)

    for campo, valor in update_data.items():
        if current_user.rol != RolEnum.supervisor and campo not in campos_permitidos:
            raise HTTPException(status_code=403, detail=f"No puede editar el campo '{campo}'")
        setattr(prog, campo, valor)

    prog.actualizado_por = current_user.id
    await db.commit()
    await db.refresh(prog)
    return prog


@router.post("/{prog_id}/duplicar", response_model=ProgramacionOut)
async def duplicar(
    prog_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    assert_puede_escribir(current_user)
    if current_user.rol not in (RolEnum.supervisor, RolEnum.ingeniero_zona):
        raise HTTPException(status_code=403, detail="Sin permiso")
    result = await db.execute(select(Programacion).where(Programacion.id == prog_id))
    orig = result.scalar_one_or_none()
    if not orig:
        raise HTTPException(status_code=404, detail="No encontrado")
    _check_unidad_access(current_user, orig.unidad)
    _check_zona_access(current_user, orig.zona)
    copia = Programacion(
        unidad=orig.unidad,
        zona=orig.zona, fecha=orig.fecha, lider=orig.lider,
        tipo_grupo=orig.tipo_grupo, proceso=orig.proceso,
        subproceso=orig.subproceso, proyecto_antecedente=orig.proyecto_antecedente,
        pms=orig.pms, circuito=orig.circuito, lcl=orig.lcl,
        fecha_vigencia=orig.fecha_vigencia, tipo_pdt=orig.tipo_pdt,
        num_pdt=orig.num_pdt, cd_afectados=orig.cd_afectados,
        hora=orig.hora, municipio=orig.municipio, vereda=orig.vereda,
        alcance=orig.alcance, gestor_cliente=orig.gestor_cliente,
        supervisor=orig.supervisor, valor_dia=orig.valor_dia,
        mo_proyectada=orig.mo_proyectada, valor_material=orig.valor_material,
        creado_por=current_user.id,
    )
    db.add(copia)
    await db.commit()
    await db.refresh(copia)
    return copia


@router.delete("/{prog_id}", dependencies=[Depends(get_current_active_user)])
async def eliminar(
    prog_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    assert_puede_escribir(current_user)
    if current_user.rol not in (RolEnum.supervisor, RolEnum.ingeniero_zona):
        raise HTTPException(status_code=403, detail="Sin permiso")
    result = await db.execute(select(Programacion).where(Programacion.id == prog_id))
    prog = result.scalar_one_or_none()
    if not prog:
        raise HTTPException(status_code=404, detail="No encontrado")
    _check_unidad_access(current_user, prog.unidad)
    _check_zona_access(current_user, prog.zona)
    await db.delete(prog)
    await db.commit()
    return {"ok": True}


@router.get("/campos-editables", response_model=List[str])
async def campos_editables(current_user: Usuario = Depends(get_current_active_user)):
    if current_user.rol == RolEnum.supervisor:
        return ["*"]
    return CAMPO_PERMISOS.get(current_user.rol, [])
