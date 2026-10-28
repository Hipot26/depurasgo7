from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.database import get_db
from app.api.deps import get_current_active_user, require_roles, ROLES_GLOBALES, unidad_de
from app.models.models import Lider, Proyecto, Usuario, ZonaEnum, RolEnum, UNIDAD_DEFAULT
from app.schemas.schemas import LiderCreate, LiderOut, ProyectoCreate, ProyectoOut
from typing import Optional

router = APIRouter(prefix="/catalogos", tags=["catalogos"])


@router.get("/lideres", response_model=list[LiderOut])
async def listar_lideres(
    zona: Optional[str] = None,
    unidad: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    q = select(Lider).where(Lider.activo == True)
    # Scope por unidad: cada usuario ve los líderes de su unidad; globales ven todo / ?unidad=
    if current_user.rol in ROLES_GLOBALES:
        if unidad:
            q = q.where(Lider.unidad == unidad)
    else:
        q = q.where(Lider.unidad == unidad_de(current_user))
    if zona:
        q = q.where(Lider.zona == zona)
    result = await db.execute(q.order_by(Lider.zona, Lider.nombre))
    return result.scalars().all()


@router.post("/lideres", response_model=LiderOut, dependencies=[Depends(require_roles(RolEnum.supervisor))])
async def crear_lider(
    req: LiderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    data = req.model_dump()
    if not data.get("unidad"):
        data["unidad"] = unidad_de(current_user)
    lider = Lider(**data)
    db.add(lider)
    await db.commit()
    await db.refresh(lider)
    return lider


@router.get("/proyectos", response_model=list[ProyectoOut])
async def listar_proyectos(
    zona: Optional[ZonaEnum] = None,
    q_text: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_active_user),
):
    q = select(Proyecto).where(Proyecto.activo == True)
    if zona:
        q = q.where(Proyecto.zona == zona)
    if q_text:
        q = q.where(Proyecto.nombre.ilike(f"%{q_text}%"))
    result = await db.execute(q.order_by(Proyecto.nombre).limit(50))
    return result.scalars().all()


@router.post("/proyectos", response_model=ProyectoOut, dependencies=[Depends(require_roles(RolEnum.supervisor, RolEnum.programador))])
async def crear_proyecto(req: ProyectoCreate, db: AsyncSession = Depends(get_db)):
    proyecto = Proyecto(**req.model_dump())
    db.add(proyecto)
    await db.commit()
    await db.refresh(proyecto)
    return proyecto


@router.get("/zonas")
async def listar_zonas(_: Usuario = Depends(get_current_active_user)):
    return [z.value for z in ZonaEnum]


@router.get("/estados")
async def listar_estados(_: Usuario = Depends(get_current_active_user)):
    from app.models.models import EstadoEnum
    return [e.value for e in EstadoEnum]


@router.get("/tipos-grupo")
async def tipos_grupo(_: Usuario = Depends(get_current_active_user)):
    from app.models.models import TipoGrupoEnum
    return [t.value for t in TipoGrupoEnum]
