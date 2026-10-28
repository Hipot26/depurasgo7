from typing import Optional
from fastapi import APIRouter, Depends, Header, Request, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.database import get_db
from app.api.deps import require_dev
from app.models.models import Dispositivo, Usuario

router = APIRouter(prefix="/acceso", tags=["acceso"])


def client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("x-real-ip") or (request.client.host if request.client else None)


class SolicitudAcceso(BaseModel):
    nombre: Optional[str] = None


async def upsert_dispositivo(db: AsyncSession, device_id: str, request: Request,
                             nombre: Optional[str] = None, estado: Optional[str] = None) -> Dispositivo:
    d = (await db.execute(select(Dispositivo).where(Dispositivo.device_id == device_id))).scalar_one_or_none()
    if not d:
        d = Dispositivo(
            device_id=device_id,
            nombre=nombre,
            user_agent=(request.headers.get("user-agent") or "")[:400],
            ip=client_ip(request),
            estado=estado or "pendiente",
        )
        db.add(d)
    else:
        if nombre:
            d.nombre = nombre
        d.ip = client_ip(request)
        if estado:
            d.estado = estado
    await db.commit()
    await db.refresh(d)
    return d


@router.get("/estado")
async def estado(
    request: Request,
    x_device_id: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not x_device_id:
        return {"estado": "desconocido"}
    d = (await db.execute(select(Dispositivo).where(Dispositivo.device_id == x_device_id))).scalar_one_or_none()
    return {"estado": d.estado if d else "desconocido"}


@router.post("/solicitar")
async def solicitar(
    req: SolicitudAcceso,
    request: Request,
    x_device_id: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    if not x_device_id:
        raise HTTPException(status_code=400, detail="Falta identificador de dispositivo")
    existing = (await db.execute(select(Dispositivo).where(Dispositivo.device_id == x_device_id))).scalar_one_or_none()
    if existing and existing.estado == "aprobado":
        return {"estado": "aprobado"}
    d = await upsert_dispositivo(db, x_device_id, request, nombre=req.nombre, estado="pendiente")
    return {"estado": d.estado}


# ── Panel del desarrollador ──────────────────────────────────────────────────

@router.get("/dispositivos")
async def listar_dispositivos(
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_dev),
):
    rows = (await db.execute(select(Dispositivo).order_by(Dispositivo.creado_en.desc()))).scalars().all()
    return [
        {
            "id": d.id, "device_id": d.device_id, "nombre": d.nombre,
            "user_agent": d.user_agent, "ip": d.ip, "estado": d.estado,
            "creado_en": d.creado_en.isoformat() if d.creado_en else None,
            "actualizado_en": d.actualizado_en.isoformat() if d.actualizado_en else None,
        }
        for d in rows
    ]


class CambioEstado(BaseModel):
    estado: str  # aprobado | rechazado | pendiente


@router.patch("/dispositivos/{disp_id}")
async def cambiar_estado(
    disp_id: int,
    req: CambioEstado,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_dev),
):
    if req.estado not in ("aprobado", "rechazado", "pendiente"):
        raise HTTPException(status_code=400, detail="Estado inválido")
    d = (await db.execute(select(Dispositivo).where(Dispositivo.id == disp_id))).scalar_one_or_none()
    if not d:
        raise HTTPException(status_code=404, detail="No encontrado")
    d.estado = req.estado
    await db.commit()
    return {"ok": True, "estado": d.estado}


@router.delete("/dispositivos/{disp_id}")
async def eliminar_dispositivo(
    disp_id: int,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_dev),
):
    d = (await db.execute(select(Dispositivo).where(Dispositivo.id == disp_id))).scalar_one_or_none()
    if not d:
        raise HTTPException(status_code=404, detail="No encontrado")
    await db.delete(d)
    await db.commit()
    return {"ok": True}
