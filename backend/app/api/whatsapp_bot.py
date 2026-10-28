"""API del bot de WhatsApp (Baileys, proceso Node aparte en el mismo VPS).

El bot NO es un usuario: se autentica con un token de servicio (header X-Bot-Token,
WHATSAPP_BOT_TOKEN en .env). Tres responsabilidades:

  1. INBOX  — el bot empuja aquí todo lo que ve en los grupos vigilados y en los chats
              directos (texto + media ya descargada). Queda crudo en `wa_mensajes`; el
              matcher lo cruza con órdenes después.
  2. COLA   — el bot pregunta cada ciclo (3 h) qué mensajes salientes hay pendientes
              (`wa_envios`) y reporta el resultado de cada uno.
  3. GRUPOS — el bot sincroniza la lista de grupos donde está el número; una persona
              marca cuáles vigilar. Solo de esos se descarga media.

La media entra siempre por multipart (el archivo ya descargado por el bot) y se guarda
en data/whatsapp/<YYYY-MM>/ — misma filosofía que EVID_DIR de ans.py.
"""
import os
import re
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile, File, Form, Body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_db
from app.models.models import WaMensaje, WaEnvio, WaGrupo

router = APIRouter(prefix="/wa-bot", tags=["whatsapp-bot"])

WA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                      "data", "whatsapp"))
WA_MAX_MB = 200            # mismo techo que las evidencias del ANS


async def require_bot(x_bot_token: str = Header(default="")):
    """Gate de TODOS los endpoints del bot. Sin token configurado, el módulo está apagado
    (mismo patrón que ANTHROPIC_API_KEY: falta el .env → degrada sin romper)."""
    esperado = settings.WHATSAPP_BOT_TOKEN
    if not esperado:
        raise HTTPException(status_code=503, detail="Bot de WhatsApp no configurado")
    if not secrets.compare_digest(x_bot_token or "", esperado):
        raise HTTPException(status_code=401, detail="Token de bot inválido")


@router.get("/ping", dependencies=[Depends(require_bot)])
async def ping():
    return {"ok": True}


@router.get("/estado", dependencies=[Depends(require_bot)])
async def estado(db: AsyncSession = Depends(get_db)):
    """Pulso para el vigilante externo del bot (scripts/bot_check.sh en el servidor donde
    corre el bot, cron */5). Desde 2026-10-10 el vigilante lee aquí el estado en vez de
    consultar una copia de la base que ya no se actualiza.
    `ultimo_mensaje_utc` es max(wa_mensajes.enviado_en) tal como está en la tabla (naive, UTC);
    `pendientes` son los wa_envios aprobados que el bot aún no sacó."""
    from sqlalchemy import func
    ultimo = (await db.execute(select(func.max(WaMensaje.enviado_en)))).scalar()
    pend = (await db.execute(select(func.count()).select_from(WaEnvio)
                             .where(WaEnvio.estado == "pendiente"))).scalar()
    return {"ultimo_mensaje_utc": ultimo.isoformat() if ultimo else None,
            "pendientes": int(pend or 0),
            "ahora_utc": datetime.utcnow().isoformat()}


# ── 1. INBOX ──────────────────────────────────────────────────────────────────

@router.get("/inbox/existe", dependencies=[Depends(require_bot)])
async def inbox_existe(wa_id: str, db: AsyncSession = Depends(get_db)):
    """¿Ya tenemos este mensaje? El bot pregunta ANTES de descargar la media en los
    lotes históricos/backfill — descargar un video para que el dedupe lo tire es ancho
    de banda del teléfono desperdiciado."""
    ya = await db.execute(select(WaMensaje.id).where(WaMensaje.wa_id == wa_id))
    return {"existe": ya.scalar_one_or_none() is not None}


@router.post("/inbox", dependencies=[Depends(require_bot)])
async def inbox(wa_id: str = Form(...), chat_jid: str = Form(...),
                chat_nombre: Optional[str] = Form(None),
                es_grupo: bool = Form(False),
                remitente_jid: Optional[str] = Form(None),
                remitente_nombre: Optional[str] = Form(None),
                de_mi: bool = Form(False),
                tipo: str = Form("texto"),
                texto: Optional[str] = Form(None),
                quote_wa_id: Optional[str] = Form(None),
                enviado_en: Optional[str] = Form(None),      # ISO 8601
                media: Optional[UploadFile] = File(None),
                db: AsyncSession = Depends(get_db)):
    """Un mensaje visto por el bot. Idempotente por `wa_id`: los reintentos del bot
    (red caída, backend reiniciando) no duplican filas ni archivos."""
    ya = await db.execute(select(WaMensaje).where(WaMensaje.wa_id == wa_id))
    if ya.scalar_one_or_none():
        return {"ok": True, "duplicado": True}

    ruta = mime = None
    tamano = None
    if media is not None:
        datos = await media.read()
        if len(datos) > WA_MAX_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"Media pasa de {WA_MAX_MB} MB")
        mes = datetime.now().strftime("%Y-%m")
        carpeta = os.path.join(WA_DIR, mes)
        os.makedirs(carpeta, exist_ok=True)
        seguro = re.sub(r"[^\w.\-]", "_", os.path.basename(media.filename or "media"))[:120]
        ruta = os.path.join(carpeta, f"{re.sub(r'[^A-Za-z0-9]', '', wa_id)[:40]}_{seguro}")
        with open(ruta, "wb") as f:
            f.write(datos)
        mime, tamano = media.content_type, len(datos)

    ts = None
    if enviado_en:
        try:
            ts = datetime.fromisoformat(enviado_en.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            ts = None

    m = WaMensaje(wa_id=wa_id, chat_jid=chat_jid, chat_nombre=chat_nombre,
                  es_grupo=es_grupo, remitente_jid=remitente_jid,
                  remitente_nombre=remitente_nombre, de_mi=de_mi, tipo=tipo,
                  quote_wa_id=quote_wa_id or None,
                  texto=(texto or "")[:8000] or None,
                  media_ruta=ruta, media_mime=mime, media_tamano=tamano,
                  enviado_en=ts)
    db.add(m)
    await db.commit()
    return {"ok": True, "id": m.id}


# ── 2. COLA DE SALIDA ─────────────────────────────────────────────────────────

# ── SEGUIMIENTOS Y FORZADOS EN LA COLA DEL BOT (14 oct 2026) ──
async def _origen_envio(db: AsyncSession, e) -> tuple:
    """(seguimiento, forzado) de un envío pendiente, según el turno «enviado» que encolar_pregunta
    dejó en el hilo del caso con el mismo texto (meta.origen / meta.forzado). Sin eso, el chequeo
    del bot tomaba un seguimiento del embudo —o un envío forzado a sabiendas— por una pregunta
    repetida y lo mataba (caso CDOPP2026R09920, envío #128)."""
    import json as _json
    from app.models.models import AnsConsulta, AnsConsultaMsg
    if not e.reg_id or not e.campo:
        return False, False
    r = await db.execute(select(AnsConsultaMsg)
                         .join(AnsConsulta, AnsConsulta.id == AnsConsultaMsg.consulta_id)
                         .where(AnsConsulta.reg_id == e.reg_id, AnsConsulta.campo == e.campo,
                                AnsConsultaMsg.direccion == "enviado")
                         .order_by(AnsConsultaMsg.id.desc()).limit(8))
    for m in r.scalars().all():
        if (m.texto or "").strip()[:200] != (e.texto or "").strip()[:200]:
            continue
        try:
            meta = _json.loads(m.meta or "{}") or {}
        except ValueError:
            return False, False
        return meta.get("origen") == "seguimiento", bool(meta.get("forzado"))
    return False, False


@router.get("/cola", dependencies=[Depends(require_bot)])
async def cola(db: AsyncSession = Depends(get_db)):
    """Los mensajes que la plataforma quiere que el bot mande en este ciclo.

    ÚLTIMA LÍNEA DE DEFENSA (incidente 2026-08-27): si entre el encolado y
    el envío llegó el correo del caso a Correos ANS — o alguien ya mandó la misma
    pregunta — el envío se bloquea AQUÍ y queda en error con el motivo. El bot nunca
    lo ve; nadie vuelve a pedir un correo que ya está en el sistema."""
    from app.api.wa_auditoria import _bloqueo_envio
    r = await db.execute(select(WaEnvio).where(WaEnvio.estado == "pendiente")
                         .order_by(WaEnvio.id).limit(50))
    out, bloqueados = [], 0
    for e in r.scalars().all():
        motivo = None
        if e.reg_id and e.campo:
            seguimiento, forzado = await _origen_envio(db, e)
            # forzado: sin la regla de duplicado (destino/persona en blanco); la de «ya llegó el
            # correo después de encolar» sigue viva (creado_envio).
            motivo = await _bloqueo_envio(db, e.reg_id, e.campo,
                                          None if forzado else e.destino,
                                          None if forzado else e.persona,
                                          creado_envio=e.creado_en,
                                          ignorar_envio_id=e.id, seguimiento=seguimiento)
        if motivo:
            e.estado = "error"
            e.error = f"bloqueado antes de enviar: {motivo}"[:2000]
            bloqueados += 1
            continue
        out.append({"id": e.id, "destino": e.destino, "persona": e.persona,
                    "texto": e.texto, "campo": e.campo})
    if bloqueados:
        await db.commit()
    return out


@router.post("/cola/{envio_id}/resultado", dependencies=[Depends(require_bot)])
async def cola_resultado(envio_id: int, datos: dict = Body(...),
                         db: AsyncSession = Depends(get_db)):
    """El bot reporta cómo le fue con un envío. Body: {ok: bool, error?: str}."""
    e = await db.get(WaEnvio, envio_id)
    if not e:
        raise HTTPException(status_code=404, detail="Envío no encontrado")
    if datos.get("ok"):
        e.estado, e.enviado_en = "enviado", datetime.now()
        e.wa_id = (datos.get("wa_id") or "").strip() or None
    else:
        e.estado, e.error = "error", str(datos.get("error") or "desconocido")[:2000]
    await db.commit()
    return {"ok": True}


# ── 3. RESPUESTAS: el bot pide procesar los DM nuevos contra los hilos abiertos ──

@router.post("/procesar-respuestas", dependencies=[Depends(require_bot)])
async def procesar_respuestas(db: AsyncSession = Depends(get_db)):
    """Cierra el circuito 3.2 B sin intervención humana: cada DM nuevo de un contacto con
    hilo abierto se registra como turno, la IA reescribe la bitácora y la media queda como
    evidencia (ver services/wa_respuestas). El bot llama esto ~90 s después de recibir un
    DM y en cada ciclo de 3 h."""
    from app.services import wa_respuestas
    hechos = await wa_respuestas.procesar_pendientes(db)
    return {"procesados": len(hechos), "detalle": hechos}


# ── 4. GRUPOS ─────────────────────────────────────────────────────────────────

@router.post("/grupos/sync", dependencies=[Depends(require_bot)])
async def grupos_sync(grupos: list = Body(...), db: AsyncSession = Depends(get_db)):
    """El bot manda TODOS los grupos donde está el número: [{jid, nombre}]. Los nuevos
    entran como no-vigilados (una persona decide cuáles importan); los conocidos solo
    refrescan el nombre. Devuelve los jid vigilados, que es lo único que el bot necesita."""
    r = await db.execute(select(WaGrupo))
    conocidos = {g.jid: g for g in r.scalars().all()}
    for g in grupos:
        jid, nombre = g.get("jid"), g.get("nombre")
        if not jid:
            continue
        if jid in conocidos:
            conocidos[jid].nombre = nombre or conocidos[jid].nombre
        else:
            db.add(WaGrupo(jid=jid, nombre=nombre, vigilado=False))
    await db.commit()
    r = await db.execute(select(WaGrupo).where(WaGrupo.vigilado))
    return {"vigilados": [g.jid for g in r.scalars().all()]}
