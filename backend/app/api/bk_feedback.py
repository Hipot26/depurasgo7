import json
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.database import get_db
from app.api.deps import get_current_active_user, require_dev
from app.models.models import Feedback, FeedbackMensaje, Usuario, RolEnum
from app.schemas.schemas import (FeedbackCreate, FeedbackUpdate, FeedbackOut,
                                 FeedbackMensajeCreate, FeedbackMensajeOut, FeedbackMensajeEdit)

router = APIRouter(prefix="/feedback", tags=["feedback"])

# Límites razonables para las capturas (base64 en BD)
MAX_ADJUNTOS = 6
MAX_PAYLOAD  = 12 * 1024 * 1024  # ~12 MB de base64 en total
ESTADOS_VALIDOS = {"nueva", "en_revision", "planeada", "implementada", "rechazada"}


def _es_dev(user: Usuario) -> bool:
    return user.rol == RolEnum.desarrollador


def _puede_ver(fb: Feedback, user: Usuario) -> bool:
    """Acceso al hilo: el desarrollador o el autor de la solicitud."""
    return _es_dev(user) or (fb.usuario_id is not None and fb.usuario_id == user.id)


def _meta_hilo(fb: Feedback, msgs: list, viewer: str) -> dict:
    """Calcula nº de mensajes, fecha del último y si hay no-leídos para el lado que mira.
    viewer = 'dev' | 'autor'. msgs = mensajes del hilo (ya filtrados a este feedback)."""
    n = len(msgs)
    ultimo = max((m.creado_en for m in msgs), default=None)
    if viewer == "dev":
        # entrante para el dev = la solicitud (raíz) + mensajes del autor (es_dev=False)
        entrantes = [fb.creado_en] + [m.creado_en for m in msgs if not m.es_dev]
        last_in = max(entrantes)
        no_leido = fb.leido_dev_en is None or last_in > fb.leido_dev_en
    else:  # autor: entrante = respuestas del dev (es_dev=True)
        dev_msgs = [m.creado_en for m in msgs if m.es_dev]
        if not dev_msgs:
            no_leido = False
        else:
            last_in = max(dev_msgs)
            no_leido = fb.leido_autor_en is None or last_in > fb.leido_autor_en
    return {"n_mensajes": n, "no_leido": no_leido, "ultimo_mensaje_en": ultimo}


def _to_out(fb: Feedback, include_contexto: bool = False, viewer: str = "dev",
            msgs: Optional[list] = None) -> FeedbackOut:
    adj = []
    if fb.adjuntos:
        try: adj = json.loads(fb.adjuntos)
        except Exception: adj = []
    ctx = None
    if include_contexto and fb.contexto:
        try: ctx = json.loads(fb.contexto)
        except Exception: ctx = None
    meta = _meta_hilo(fb, msgs or [], viewer)
    return FeedbackOut(
        id=fb.id, usuario_id=fb.usuario_id, autor_nombre=fb.autor_nombre,
        autor_rol=fb.autor_rol, autor_zona=fb.autor_zona, tipo=fb.tipo,
        titulo=fb.titulo, descripcion=fb.descripcion, url_pagina=fb.url_pagina,
        adjuntos=adj, contexto=ctx, estado=fb.estado, prioridad=fb.prioridad,
        respuesta_dev=fb.respuesta_dev, creado_en=fb.creado_en, actualizado_en=fb.actualizado_en,
        **meta,
    )


async def _msgs_por_feedback(db: AsyncSession, fb_ids: list[int]) -> dict[int, list]:
    """Trae los mensajes de varios hilos en una sola consulta, agrupados por feedback_id."""
    if not fb_ids:
        return {}
    rows = (await db.execute(
        select(FeedbackMensaje).where(FeedbackMensaje.feedback_id.in_(fb_ids))
        .order_by(FeedbackMensaje.creado_en)
    )).scalars().all()
    out: dict[int, list] = {}
    for m in rows:
        out.setdefault(m.feedback_id, []).append(m)
    return out


def _msg_out(m: FeedbackMensaje) -> FeedbackMensajeOut:
    adj = []
    if m.adjuntos:
        try: adj = json.loads(m.adjuntos)
        except Exception: adj = []
    return FeedbackMensajeOut(
        id=m.id, feedback_id=m.feedback_id, autor_id=m.autor_id, autor_nombre=m.autor_nombre,
        autor_rol=m.autor_rol, es_dev=m.es_dev, cuerpo=m.cuerpo, adjuntos=adj, creado_en=m.creado_en,
        editado_en=m.editado_en,
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
    return _to_out(fb, viewer="autor")  # sin contexto: oculto al usuario


@router.get("/mias", response_model=List[FeedbackOut])
async def mis_feedbacks(
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    q = select(Feedback).where(Feedback.usuario_id == current_user.id).order_by(Feedback.creado_en.desc())
    items = (await db.execute(q)).scalars().all()
    msgs = await _msgs_por_feedback(db, [f.id for f in items])
    return [_to_out(f, viewer="autor", msgs=msgs.get(f.id, [])) for f in items]


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
    msgs = await _msgs_por_feedback(db, [f.id for f in items])
    return [_to_out(f, include_contexto=True, viewer="dev", msgs=msgs.get(f.id, [])) for f in items]


@router.get("/stats")
async def stats_feedback(
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_dev),
):
    q = select(Feedback.estado, func.count()).group_by(Feedback.estado)
    rows = (await db.execute(q)).all()
    by_estado = {e: n for e, n in rows}
    total = sum(by_estado.values())
    # Nº de hilos con respuesta/mensaje nuevo sin leer por el dev
    fbs = (await db.execute(select(Feedback))).scalars().all()
    msgs = await _msgs_por_feedback(db, [f.id for f in fbs])
    no_leidos = sum(1 for f in fbs if _meta_hilo(f, msgs.get(f.id, []), "dev")["no_leido"])
    return {"total": total, "por_estado": by_estado, "no_leidos": no_leidos}


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
    msgs = (await _msgs_por_feedback(db, [fb.id])).get(fb.id, [])
    return _to_out(fb, include_contexto=True, viewer="dev", msgs=msgs)


# ── Hilo de conversación (estilo Slack, autor ↔ dev) ─────────────────────────────

@router.get("/{fb_id}/mensajes", response_model=List[FeedbackMensajeOut])
async def listar_mensajes(
    fb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Mensajes del hilo. Acceso: dev o autor. Abrir el hilo lo marca como leído por ese lado."""
    fb = (await db.execute(select(Feedback).where(Feedback.id == fb_id))).scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="No encontrado")
    if not _puede_ver(fb, current_user):
        raise HTTPException(status_code=403, detail="Sin acceso a este hilo")
    msgs = (await _msgs_por_feedback(db, [fb_id])).get(fb_id, [])
    # Marcar como leído por el lado que abre el hilo
    if _es_dev(current_user):
        fb.leido_dev_en = datetime.utcnow()
    elif fb.usuario_id == current_user.id:
        fb.leido_autor_en = datetime.utcnow()
    await db.commit()
    return [_msg_out(m) for m in msgs]


@router.post("/{fb_id}/mensajes", response_model=FeedbackMensajeOut)
async def crear_mensaje(
    fb_id: int,
    req: FeedbackMensajeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Agrega un mensaje al hilo (autor o dev). Bubblea la solicitud y marca leído por quien escribe."""
    fb = (await db.execute(select(Feedback).where(Feedback.id == fb_id))).scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="No encontrado")
    if not _puede_ver(fb, current_user):
        raise HTTPException(status_code=403, detail="Sin acceso a este hilo")
    cuerpo = (req.cuerpo or "").strip()
    adjuntos = [a for a in (req.adjuntos or []) if isinstance(a, str) and a.startswith("data:")]
    if not cuerpo and not adjuntos:
        raise HTTPException(status_code=400, detail="El mensaje está vacío")
    if len(adjuntos) > MAX_ADJUNTOS:
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_ADJUNTOS} capturas")
    adj_json = json.dumps(adjuntos) if adjuntos else None
    if adj_json and len(adj_json) > MAX_PAYLOAD:
        raise HTTPException(status_code=413, detail="Las capturas pesan demasiado, reduce su tamaño")

    es_dev = _es_dev(current_user)
    m = FeedbackMensaje(
        feedback_id=fb_id, autor_id=current_user.id, autor_nombre=current_user.nombre,
        autor_rol=current_user.rol.value if current_user.rol else None,
        es_dev=es_dev, cuerpo=cuerpo or "(captura)", adjuntos=adj_json,
    )
    db.add(m)
    fb.actualizado_en = datetime.utcnow()          # bubblea el hilo al tope
    if es_dev:
        fb.leido_dev_en = datetime.utcnow()        # el que escribe ya lo vio
    elif fb.usuario_id == current_user.id:
        fb.leido_autor_en = datetime.utcnow()
    await db.commit()
    await db.refresh(m)
    return _msg_out(m)


@router.patch("/{fb_id}/mensajes/{msg_id}", response_model=FeedbackMensajeOut)
async def editar_mensaje(
    fb_id: int,
    msg_id: int,
    req: FeedbackMensajeEdit,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Corrige el texto de un mensaje YA ENVIADO. Solo el autor puede editar el suyo (ni el dev
    edita los de otros: sería reescribirle la palabra a alguien). Queda marcado como «(editado)»;
    las capturas no se tocan."""
    m = (await db.execute(select(FeedbackMensaje).where(
        FeedbackMensaje.id == msg_id, FeedbackMensaje.feedback_id == fb_id))).scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")
    if m.autor_id != current_user.id:
        raise HTTPException(status_code=403, detail="Solo puedes editar tus propios mensajes")
    cuerpo = (req.cuerpo or "").strip()
    if not cuerpo:
        raise HTTPException(status_code=400, detail="El mensaje no puede quedar vacío")
    if cuerpo == m.cuerpo:
        return _msg_out(m)                       # sin cambios: no marca «editado»
    m.cuerpo = cuerpo
    m.editado_en = datetime.utcnow()
    await db.commit()
    await db.refresh(m)
    return _msg_out(m)


@router.post("/{fb_id}/leido")
async def marcar_leido(
    fb_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Marca el hilo como leído para el lado que consulta (dev o autor)."""
    fb = (await db.execute(select(Feedback).where(Feedback.id == fb_id))).scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="No encontrado")
    if not _puede_ver(fb, current_user):
        raise HTTPException(status_code=403, detail="Sin acceso a este hilo")
    if _es_dev(current_user):
        fb.leido_dev_en = datetime.utcnow()
    elif fb.usuario_id == current_user.id:
        fb.leido_autor_en = datetime.utcnow()
    await db.commit()
    return {"ok": True}


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
