import json
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.database import get_db
from app.api.deps import get_current_active_user, require_dev
from app.models.models import Feedback, Usuario
from app.schemas.schemas import FeedbackCreate, FeedbackUpdate, FeedbackOut

router = APIRouter(prefix="/feedback", tags=["feedback"])

# Límites razonables para las capturas (base64 en BD)
MAX_ADJUNTOS = 6
MAX_PAYLOAD  = 12 * 1024 * 1024  # ~12 MB de base64 en total
ESTADOS_VALIDOS = {"nueva", "en_revision", "planeada", "implementada", "rechazada"}


def _to_out(fb: Feedback, include_contexto: bool = False) -> FeedbackOut:
    adj = []
    if fb.adjuntos:
        try: adj = json.loads(fb.adjuntos)
        except Exception: adj = []
    ctx = None
    if include_contexto and fb.contexto:
        try: ctx = json.loads(fb.contexto)
        except Exception: ctx = None
    return FeedbackOut(
        id=fb.id, usuario_id=fb.usuario_id, autor_nombre=fb.autor_nombre,
        autor_rol=fb.autor_rol, autor_zona=fb.autor_zona, tipo=fb.tipo,
        titulo=fb.titulo, descripcion=fb.descripcion, url_pagina=fb.url_pagina,
        adjuntos=adj, contexto=ctx, estado=fb.estado, prioridad=fb.prioridad,
        respuesta_dev=fb.respuesta_dev, creado_en=fb.creado_en, actualizado_en=fb.actualizado_en,
    )


@router.post("", response_model=FeedbackOut)
async def crear_feedback(
    req: FeedbackCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    if not req.titulo.strip() or not req.descripcion.strip():
        raise HTTPException(status_code=400, detail="Título y descripción son obligatorios")

    adjuntos = [a for a in (req.adjuntos or []) if isinstance(a, str) and a.startswith("data:")]
    if len(adjuntos) > MAX_ADJUNTOS:
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_ADJUNTOS} capturas")
    adj_json = json.dumps(adjuntos) if adjuntos else None
    if adj_json and len(adj_json) > MAX_PAYLOAD:
        raise HTTPException(status_code=413, detail="Las capturas pesan demasiado, reduce su tamaño")

    ctx_json = None
    if req.contexto:
        try: ctx_json = json.dumps(req.contexto)[:20000]
        except Exception: ctx_json = None

    fb = Feedback(
        usuario_id=current_user.id,
        autor_nombre=current_user.nombre,
        autor_rol=current_user.rol.value if current_user.rol else None,
        autor_zona=current_user.zona.value if current_user.zona else None,
        tipo=(req.tipo or "Mejora").strip()[:40],
        titulo=req.titulo.strip()[:200],
        descripcion=req.descripcion.strip(),
        url_pagina=(req.url_pagina or None),
        adjuntos=adj_json,
        contexto=ctx_json,
        estado="nueva",
    )
    db.add(fb)
    await db.commit()
    await db.refresh(fb)
    return _to_out(fb)  # sin contexto: oculto al usuario


@router.get("/mias", response_model=List[FeedbackOut])
async def mis_feedbacks(
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    q = select(Feedback).where(Feedback.usuario_id == current_user.id).order_by(Feedback.creado_en.desc())
    items = (await db.execute(q)).scalars().all()
    return [_to_out(f) for f in items]


# ── Buzón privado del desarrollador ─────────────────────────────────────────────

@router.get("", response_model=List[FeedbackOut])
async def listar_feedback(
    estado: Optional[str] = None,
    tipo: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_dev),
):
    q = select(Feedback).order_by(Feedback.creado_en.desc())
    if estado: q = q.where(Feedback.estado == estado)
    if tipo:   q = q.where(Feedback.tipo == tipo)
    items = (await db.execute(q)).scalars().all()
    return [_to_out(f, include_contexto=True) for f in items]


@router.get("/stats")
async def stats_feedback(
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_dev),
):
    q = select(Feedback.estado, func.count()).group_by(Feedback.estado)
    rows = (await db.execute(q)).all()
    by_estado = {e: n for e, n in rows}
    total = sum(by_estado.values())
    return {"total": total, "por_estado": by_estado}


@router.patch("/{fb_id}", response_model=FeedbackOut)
async def actualizar_feedback(
    fb_id: int,
    req: FeedbackUpdate,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_dev),
):
    fb = (await db.execute(select(Feedback).where(Feedback.id == fb_id))).scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="No encontrado")
    data = req.model_dump(exclude_unset=True)
    if "estado" in data and data["estado"] and data["estado"] not in ESTADOS_VALIDOS:
        raise HTTPException(status_code=400, detail="Estado inválido")
    for k, v in data.items():
        setattr(fb, k, v)
    await db.commit()
    await db.refresh(fb)
    return _to_out(fb, include_contexto=True)


@router.delete("/{fb_id}")
async def eliminar_feedback(
    fb_id: int,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_dev),
):
    fb = (await db.execute(select(Feedback).where(Feedback.id == fb_id))).scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="No encontrado")
    await db.delete(fb)
    await db.commit()
    return {"ok": True}
