"""Auditoría del analista automático de WhatsApp (tab «Auditoría IA» del ANS, SOLO dev).

El circuito: la máquina analiza un caso (service `wa_analista`) y deja TODO su proceso en
`wa_analisis`; aquí el desarrollador lo revisa — línea de tiempo, videos reproducibles,
transcripciones, veredicto y observación borrador — y decide:
  · APLICAR  → escribe la observación en la celda del 3.2 A y adjunta los videos del caso
               como AnsEvidencia (la decisión SI/NO de `excluible` sigue siendo humana,
               regla del 2026-07-21).
  · DESCARTAR → el análisis queda archivado como descartado, nada toca el ANS.

El router se monta con require_dev + entorno real (ver main.py).
"""
import json
import os
import re

# Nombre de pila de quien opera el ANS (variable ANS_NOMBRE_OPERADOR): en las respuestas de WhatsApp
# el saludo («Hola <nombre>») nombra a quien pregunta, no a un contacto, y no debe tomarse como persona
# a la que remiten. Vacío = no se excluye ninguna palabra.
_PALABRAS_OPERADOR = {w for w in os.getenv("ANS_NOMBRE_OPERADOR", "").upper().split() if len(w) >= 3}

# Nombre de pila de quien opera el ANS (variable ANS_NOMBRE_OPERADOR): en las respuestas de WhatsApp
# el saludo («Hola <nombre>») nombra a quien pregunta, no a un contacto, y no debe tomarse como persona
# a la que remiten. Vacío = no se excluye ninguna palabra.
_PALABRAS_OPERADOR = {w for w in os.getenv("ANS_NOMBRE_OPERADOR", "").upper().split() if len(w) >= 3}
import shutil
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.ans_ctx import current_entorno
from app.models.models import (AnsRegistro, AnsEvidencia, AnsConsulta, AnsConsultaMsg,
                               WaAnalisis, WaMensaje, WaEnvio, WaGrupo, Contacto,
                               CorreoAns)
from app.services import wa_analista
from app.services import claude
from app.services import evid_paquete as _paq

# PAQUETE PARA ENEL (14 oct 2026): etiqueta corta («qué muestra») de las evidencias recién adjuntadas.
async def _etiquetar_nuevas(db: AsyncSession, reg, ids: list) -> None:
    """Va al nombre del archivo en el paquete para ENEL (Evidencia_1_Video_16-50_<qué muestra>).
    Mejor esfuerzo: si la IA no responde, el caso queda igual y el tab Evidencias lo completa."""
    if not ids:
        return
    try:
        evs = [await db.get(AnsEvidencia, i) for i in ids]
        if await _paq.etiquetar(db, [e for e in evs if e], reg):
            await db.commit()
    except Exception as ex:  # noqa: BLE001 — nunca romper el aplicado por una etiqueta
        import logging
        logging.getLogger("wa_auditoria").warning("etiquetas de evidencias %s: %s", ids, ex)


router = APIRouter(prefix="/ans/wa", tags=["ans-wa-auditoria"])


# EVIDENCIA NO SÓLIDA (14 oct 2026): qué documentos tiene adjuntos la celda (para la solidez del tramo).
# VIDEOS DE LA CELDA EN EL BLOQUE (14 oct 2026): los videos adjuntos a la celda que NO vienen del analista
# (esos ya están en ia.videos): chat directo o subidos a mano, con su hora de letrero.
async def _videos_celda(db: AsyncSession, reg_id: int, campo: str = "excluible") -> list:
    r = await db.execute(select(AnsEvidencia).where(AnsEvidencia.reg_id == reg_id, AnsEvidencia.campo == campo)
                         .order_by(AnsEvidencia.id))
    out = []
    for e in r.scalars().all():
        if not e.en_paquete or _paq.tipo_de(e) != "Video":
            continue
        if (e.nota or "").startswith(("Adjuntado por el analista", "Adjuntado al decidir")):
            continue
        out.append({"id": e.id, "hora": e.hora, "hora_fin": e.hora_fin,
                    "que_se_ve": e.que_muestra or (e.transcripcion or "")[:120] or e.nombre or ""})
    return out


async def _docs_de(db: AsyncSession, reg_id: int, campo: str = "excluible") -> dict:
    r = await db.execute(select(AnsEvidencia).where(AnsEvidencia.reg_id == reg_id, AnsEvidencia.campo == campo))
    docs = {"correo": 0, "pdf": 0, "imagen": 0, "captura": 0}
    for e in r.scalars().all():
        if not e.en_paquete:
            continue
        t = _paq.tipo_de(e)
        if t == "Correo":
            docs["correo"] += 1
        elif t == "PDF":
            docs["pdf"] += 1
        elif t == "Imagen":
            docs["captura" if (e.nota or "").startswith("Captura del chat generada") else "imagen"] += 1
    return docs

EVID_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                        "data", "ans_evidencias"))


async def _bloqueo_envio(db: AsyncSession, reg_id: int, campo: str,
                         destino: str | None = None, persona: str | None = None,
                         creado_envio=None, ignorar_envio_id: int | None = None,
                         seguimiento: bool = False) -> str | None:
    """BLOQUEADORES duros del circuito de preguntas (incidente 2026-08-27: se
    pidió por WhatsApp el correo de dos casos cuyos correos ya estaban en Correos
    ANS desde hacía dos días). Devuelve el MOTIVO del
    bloqueo o None si se puede enviar.

    1. CORREO YA EXISTE — si el caso tiene un correo vivo en Correos ANS, no se
       pregunta: se revisa/aprueba ese correo (pedir un correo ya enviado es ilógico;
       para el video del 3.2 A también aplica: primero se lee lo que ya llegó).
       Con `creado_envio` (chequeo del bot ANTES de enviar) solo bloquean los correos
       llegados DESPUÉS de encolar — así un envío forzado a sabiendas no se mata solo.
    2. DUPLICADO — si ya hay un envío pendiente/enviado del mismo caso+campo a la
       misma persona o número, no se repite."""
    r = await db.execute(select(CorreoAns).where(CorreoAns.estado != "descartado"))
    for c in r.scalars().all():
        try:
            pdls = json.loads(c.pdls) if c.pdls else []
        except ValueError:
            continue
        if not any(m.get("reg_id") == reg_id for m in pdls):
            continue
        if creado_envio is not None and (not c.creado_en or c.creado_en <= creado_envio):
            continue
        f = ((c.creado_en - timedelta(hours=5)).strftime("%d/%m %H:%M")
             if c.creado_en else "s/f")
        return (f"el caso ya tiene correo en Correos ANS (#{c.id}, del {f}) — "
                f"no se pregunta por WhatsApp: revisa y aprueba ese correo")
    tel = re.sub(r"\D", "", destino or "")
    r = await db.execute(select(WaEnvio).where(
        WaEnvio.reg_id == reg_id, WaEnvio.campo == campo,
        WaEnvio.estado.in_(("pendiente", "enviado"))))
    for e in r.scalars().all():
        if ignorar_envio_id and e.id == ignorar_envio_id:
            continue
        # En el chequeo del bot, un pendiente POSTERIOR no bloquea a este (se
        # bloqueará él mismo al evaluarse) — si no, dos pendientes se matan entre sí.
        if ignorar_envio_id and e.estado == "pendiente" and e.id > ignorar_envio_id:
            continue
        if (tel and re.sub(r"\D", "", e.destino or "") == tel) or \
                (persona and (e.persona or "").strip().upper() == persona.strip().upper()):
            # …salvo que sea un SEGUIMIENTO del embudo y YA HAYAN RESPONDIDO (2026-09-24):
            # ahí el siguiente mensaje no repite la pregunta, CONTINÚA la conversación —
            # justo lo que hace falta cuando la respuesta fue ambigua, una repregunta o una
            # promesa. El pedido automático sigue bloqueado: eso sí sería insistir.
            if (seguimiento and e.estado == "enviado"
                    and await _respondido_despues(db, reg_id, campo,
                                                  e.enviado_en or e.creado_en)):
                continue
            estado = "está encolada" if e.estado == "pendiente" else "ya se le envió"
            return (f"esta pregunta {estado} a {e.persona} (envío #{e.id}) — no se repite")
    return None


async def _respondido_despues(db: AsyncSession, reg_id: int, campo: str, desde) -> bool:
    """¿Hay un turno 'recibido' en el hilo de este caso posterior a `desde`?"""
    if not desde:
        return False
    r = await db.execute(
        select(AnsConsultaMsg.id)
        .join(AnsConsulta, AnsConsulta.id == AnsConsultaMsg.consulta_id)
        .where(AnsConsulta.reg_id == reg_id, AnsConsulta.campo == campo,
               AnsConsultaMsg.direccion == "recibido",
               AnsConsultaMsg.creado_en >= desde).limit(1))
    return r.scalars().first() is not None


_MARCAS_LECTURA = ("[nota de voz transcrita]", "[audio del video transcrito]",
                   "[imagen leída]", "[documento adjunto]")


def _sin_markdown(t: str | None) -> str | None:
    """El modelo de visión a veces escribe con asteriscos y títulos; en pantalla, texto plano."""
    if not t:
        return None
    t = re.sub(r"\*\*|__|^#+\s*", "", t, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", t).strip() or None


def _partir_turno(texto: str | None) -> tuple[str, str | None, bool]:
    """Lo que ESCRIBIÓ la persona, aparte de lo que la máquina leyó del adjunto. En el turno
    viven juntos (así lo lee la IA, de corrido); en pantalla van separados: la foto o el audio
    primero, las palabras de la persona después y la lectura de la máquina plegada. Antes el
    bubble era una pared de transcripción con el botón de la imagen escondido al final
    (requisito 2026-09-24).
    Devuelve (texto_humano, lectura, hubo_adjunto)."""
    t = texto or ""
    adjunto = bool(re.search(r"\[adjuntó [^\]]*\]|^\((?:audio|imagen|video|documento) adjunto\)$",
                             t.strip(), flags=re.M))
    t = re.sub(r"\s*\[adjuntó [^\]]*\]", "", t)
    t = re.sub(r"^\((?:audio|imagen|video|documento) adjunto\)$", "", t.strip(), flags=re.M)
    lectura = None
    for marca in _MARCAS_LECTURA:
        i = t.find(marca)
        if i >= 0:
            lectura = t[i + len(marca):]
            t = t[:i]
            break
    return t.strip(), _sin_markdown(lectura), adjunto


def _campo_de(res: dict) -> str:
    """La celda de exclusión del caso: 3.2 B pregunta por el correo, 3.2 A por el video."""
    return "excluible_anulado" if (res or {}).get("tipo") == "3.2B" else "excluible"


async def _hilo_y_turnos(db: AsyncSession, reg_id: int, campo: str):
    """El hilo de consulta vigente del caso y sus turnos, en orden."""
    r = await db.execute(select(AnsConsulta).where(
        AnsConsulta.reg_id == reg_id, AnsConsulta.campo == campo,
        AnsConsulta.estado != "descartada").order_by(AnsConsulta.id.desc()).limit(1))
    c = r.scalars().first()
    if not c:
        return None, []
    rm = await db.execute(select(AnsConsultaMsg)
                          .where(AnsConsultaMsg.consulta_id == c.id)
                          .order_by(AnsConsultaMsg.creado_en, AnsConsultaMsg.id))
    return c, rm.scalars().all()


def _cobertura_acumulada(reg: AnsRegistro, res: dict, turnos) -> dict | None:
    """El análisis rápido de tiempos SUMANDO LOS DOS CANALES: los tramos que declaró la
    evidencia del caso (video/correo) más los que ha ido declarando la conversación. Es lo
    que hace que la conclusión sobre si es excluible sea la real y no la de un solo canal.
    Solo aplica al 3.2 A (el 3.2 B no tiene desfase que cubrir)."""
    if (res or {}).get("tipo") == "3.2B":
        return None
    from app.api.ans import _tramos_del_hilo
    from app.services.wa_analista import _calcular_cobertura
    tramos = list(((res or {}).get("ia") or {}).get("tramos_justificados") or [])
    for t in _tramos_del_hilo(turnos):
        if t not in tramos:
            tramos.append(t)
    return _calcular_cobertura(reg, {"tramos_justificados": tramos})


# ── REMITEN A OTRA PERSONA (19 oct 2026, ver CHANGELOG) ──
async def _remitido_a(db: AsyncSession, reg: AnsRegistro, campo: str, quien: str,
                      ult: AnsConsultaMsg, turnos: list, rol_rem: str) -> dict:
    """La persona a la que REMITIERON, con la forma de un candidato del Paso 2: número del directorio o
    sugerencias de los grupos, el mensaje ya redactado («<quien remitió> me indicó que usted…», el mismo del
    chat guiado), bloqueadores y estado del envío. Antes el embudo decía «Hay que escribirle a quien
    indicaron» sin número ni mensaje y el caso se quedaba quieto (caso del 16 oct 2026: una persona
    remitió a otra y nadie le escribió)."""
    from app.api.ans import _mensaje_wa, _norm_nombre, _tel_de, _ya_escrito_hoy
    ct = await _tel_de(db, quien)
    tel = (ct.telefono or None) if ct else None
    sugerencias = []
    if not tel:
        ra = await db.execute(select(Contacto).where(Contacto.activo.is_(True), Contacto.telefono != ""))
        agenda = [{"nombre": x.nombre, "telefono": x.telefono, "rol": x.rol, "zona": x.zona,
                   "tokens": _tokens_nombre(x.nombre)} for x in ra.scalars().all()]
        sugerencias = _sugerir_telefonos(quien, await _perfiles_whatsapp(db), agenda, zona_caso=reg.zona)
    n = _norm_nombre(quien)
    re_env = await db.execute(select(WaEnvio).where(WaEnvio.reg_id == reg.id, WaEnvio.campo == campo)
                              .order_by(WaEnvio.id.desc()))
    env = next((e for e in re_env.scalars().all()
                if _norm_nombre(e.persona or "") == n
                or (tel and re.sub(r"\D", "", e.destino or "") == tel)), None)
    envio = {"pendiente": "encolado", "enviado": "enviado"}.get(env.estado) if env else None
    # Escrito a mano desde el chat guiado (turno «enviado» sin envío del bot): también cuenta
    if not envio and any(t.direccion == "enviado" and _norm_nombre(t.persona or "") == n and t.id > ult.id
                         for t in turnos):
        envio = "enviado"
    bloqueo = None if envio else await _bloqueo_envio(db, reg.id, campo, tel, quien)
    return {"nombre": quien, "rol": "otro", "campo": campo, "telefono": tel,
            "sugerencias": sugerencias, "bloqueo": bloqueo, "envio": envio,
            "error_envio": (env.error if env and env.estado == "error" else None),
            "remitido_por": ult.persona,
            "mensaje": _mensaje_wa(reg, campo, quien, "otro", remitido_por=ult.persona,
                                   remitido_rol=rol_rem, saludar=not await _ya_escrito_hoy(db, quien))}


async def _embudo(db: AsyncSession, reg: AnsRegistro, res: dict,
                  a: WaAnalisis | None = None) -> dict | None:
    """EL EMBUDO (2026-09-24): en qué punto de la conversación está el caso y qué sigue.
    Devuelve el paso, la calidad de la última respuesta, la cobertura acumulada de los dos
    canales, la propuesta de decisión y —si la conversación debe continuar— el borrador del
    SIGUIENTE mensaje. Nada se envía desde aquí: la persona responsable aprueba cada mensaje."""
    from app.api.ans import (CALIDAD_EMBUDO, _ultima_lectura, _mensaje_seguimiento,
                             _linea_faltante, _ya_escrito_hoy, _rol_dir_de, _tel_de,
                             _limpiar_respuesta, _mensaje_cierre)   # DOBLE SALUDO / MENSAJE DE CIERRE DEL EMBUDO (14 oct 2026)
    campo = _campo_de(res)
    c, turnos = await _hilo_y_turnos(db, reg.id, campo)
    if not c:
        return None
    recibidos = [m for m in turnos if m.direccion == "recibido"]
    cov = _cobertura_acumulada(reg, res, turnos)
    dec = _ultima_lectura(turnos) or {}
    if not recibidos:
        return {"paso": "sin_respuesta", "etiqueta": "sin respuesta todavía",
                "ayuda": "Aún no han contestado. El bot ya entregó el mensaje.",
                "calidad": None, "cobertura": cov, "persona": c.persona_actual,
                "hilo_id": c.id, "mensaje": None, "puede_decidir": False,
                "decision_sugerida": None, "observacion_sugerida": None,
                "falta": None, "pregunta_pendiente": None}
    ult = recibidos[-1]
    calidad = dec.get("calidad")
    paso, etiqueta, ayuda = CALIDAD_EMBUDO.get(calidad or "", (
        "revisar", "sin clasificar",
        "La máquina no pudo clasificar la respuesta: léela y decide."))
    persona = ult.persona or c.persona_actual
    rol_dir = "otro"
    if ult.meta:
        try:
            rol_dir = _rol_dir_de((json.loads(ult.meta) or {}).get("rol_dir") or "")
        except ValueError:
            pass
    ct = await _tel_de(db, persona) if persona else None
    mensaje = _mensaje_seguimiento(reg, campo, persona or "", rol_dir, dec, cov,
                                   saludar=not await _ya_escrito_hoy(db, persona or ""))
    mensaje_cierre = (_mensaje_cierre(reg, persona or "", rol_dir, dec, paso,
                                      saludar=not await _ya_escrito_hoy(db, persona or ""))
                      if not mensaje else None)
    # REMITEN A OTRA PERSONA (19 oct 2026, ver CHANGELOG): a quién escribirle y con qué mensaje
    remitido = None
    sig = (dec.get("siguiente_persona") or "").strip()
    if paso == "redirigir" and sig:
        from app.api.ans import _norm_nombre
        if _norm_nombre(sig) != _norm_nombre(persona or ""):
            remitido = await _remitido_a(db, reg, campo, sig, ult, turnos, rol_dir)
            if remitido["envio"]:
                paso, etiqueta = "esperar_remitido", f"esperando a {sig}"
                ayuda = (f"Ya se le escribió a {sig} (lo indicó {persona}). Cuando responda, la máquina "
                         "lee la respuesta sola.")
            elif remitido["error_envio"]:
                ayuda = (f"El envío a {sig} falló ({remitido['error_envio'][:120]}): revisa el número y "
                         "vuelve a encolar.")
            elif not remitido["telefono"]:
                ayuda = (f"Hay que escribirle a {sig} (lo indicó {persona}): guarda su número y encola el "
                         "mensaje de abajo.")
            else:
                ayuda = f"Hay que escribirle a {sig} (lo indicó {persona}): revisa el mensaje y encólalo."
    # La decisión que el sistema propone HOY: la de la conversación manda sobre la del
    # análisis de videos — es información posterior y de la propia fuente.
    ia_a = (res or {}).get("ia") or {}
    decision_sug = c.excluible_sug or ia_a.get("excluible_sugerido")
    obs_sug = (c.razon_sug or "").strip() or (ia_a.get("observacion") or "").strip() or None
    return {"paso": paso, "etiqueta": etiqueta, "ayuda": ayuda, "calidad": calidad,
            "cobertura": cov, "persona": persona, "telefono": ct.telefono if ct else None,
            "rol_dir": rol_dir, "hilo_id": c.id,
            "ultima_respuesta": (ult.texto or "")[:600],
            "cuando": ((ult.creado_en - timedelta(hours=5)).strftime("%d/%m %H:%M")
                       if ult.creado_en else None),
            "resumen_ia": dec.get("resumen"), "falta": dec.get("falta"),
            "pregunta_pendiente": dec.get("pregunta_pendiente"),
            "respuesta_sugerida": _limpiar_respuesta(dec.get("respuesta_sugerida")) or dec.get("respuesta_sugerida"),
            "linea_faltante": _linea_faltante(reg, cov),
            "remitido": remitido,
            "mensaje": mensaje, "mensaje_cierre": mensaje_cierre, "puede_decidir": True,
            "decision_sugerida": decision_sug, "observacion_sugerida": obs_sug,
            "categoria": c.categoria or ia_a.get("categoria"),
            "hilo_resuelto": c.estado == "resuelta"}


async def _flujo(db: AsyncSession, a: WaAnalisis, res: dict) -> dict:
    """El estado REAL del caso a lo largo del circuito, no solo el del análisis:
    propuesto → (3.2 A) aplicado · (3.2 B) encolado → enviado → respondido / error_envio.
    Es lo que el tab muestra para que quien revisa sepa en qué va cada cosa sin adivinar."""
    if a.estado == "descartado":
        return {"flujo": "descartado"}
    # 3.2 B pregunta por el correo (campo excluible_anulado); un 3.2 A sin evidencia
    # pregunta por el video (campo excluible). Si hay envío, el envío manda el estado —
    # aunque el análisis siga «propuesto»: se puede preguntar a 1 o 2 personas en
    # paralelo sin cerrar el caso.
    campo = _campo_de(res)
    r = await db.execute(select(WaEnvio).where(WaEnvio.reg_id == a.reg_id,
                                               WaEnvio.campo == campo)
                         .order_by(WaEnvio.id.desc()).limit(1))
    e = r.scalars().first()
    if not e:
        # REVISIÓN (auditoría 2026-08-24): material del caso sin cubrir los tiempos
        # dentro de tolerancia — la máquina no se compromete, decide el ING. Si ya se
        # encoló una pregunta, el estado del envío manda (abajo), como siempre.
        if a.estado == "propuesto" and (res.get("ia") or {}).get("revision"):
            return {"flujo": "revision"}
        return {"flujo": a.estado}
    if e.estado == "pendiente":
        return {"flujo": "encolado"}
    if e.estado == "error":
        return {"flujo": "error_envio", "flujo_detalle": e.error}
    # enviado → ¿ya respondió la persona? (turno 'recibido' posterior al envío)
    rr = await db.execute(
        select(AnsConsultaMsg.id)
        .join(AnsConsulta, AnsConsulta.id == AnsConsultaMsg.consulta_id)
        .where(AnsConsulta.reg_id == a.reg_id, AnsConsulta.campo == campo,
               AnsConsultaMsg.direccion == "recibido",
               AnsConsultaMsg.creado_en >= (e.enviado_en or e.creado_en)).limit(1))
    if rr.scalars().first():
        # «Respondió» no dice nada por sí solo (requisito 2026-09-24): lo que importa es QUÉ
        # respondieron. La calidad de la última lectura convierte el estado plano en el
        # paso real del embudo — un NO cierra el caso aquí mismo.
        return {"flujo": "respondido", **(await _calidad_flujo(db, a.reg_id, campo))}
    # CONVERGENCIA de canales (2026-08-25): a veces la persona contesta el WhatsApp
    # MANDANDO el correo formal («ya le envié el correo»). Si un
    # correo del caso llegó después del envío, el ciclo está respondido — por el otro
    # canal — aunque el chat quede en visto.
    rc = await db.execute(select(CorreoAns).where(CorreoAns.estado != "descartado"))
    for c in rc.scalars().all():
        try:
            pdls = json.loads(c.pdls) if c.pdls else []
        except ValueError:
            continue
        if (any(m.get("reg_id") == a.reg_id for m in pdls) and c.creado_en
                and c.creado_en >= (e.enviado_en or e.creado_en)):
            return {"flujo": "respondido", "flujo_detalle": "por correo",
                    "embudo_paso": "decidir", "calidad_label": "llegó el correo"}
    return {"flujo": "enviado",
            "flujo_detalle": e.enviado_en.isoformat() if e.enviado_en else None}


async def _calidad_flujo(db: AsyncSession, reg_id: int, campo: str) -> dict:
    """La calidad de la ÚLTIMA lectura del hilo, para la lista del tab (barato: solo el
    último turno 'ia'). Sin lectura clasificada, queda «sin clasificar»."""
    from app.api.ans import CALIDAD_EMBUDO, _lectura_util
    # Las últimas 5, no la última: una relectura que falló por el proveedor de IA no puede
    # tumbar la clasificación buena que ya tenía el caso (misma regla que _ultima_lectura).
    r = await db.execute(
        select(AnsConsultaMsg.meta)
        .join(AnsConsulta, AnsConsulta.id == AnsConsultaMsg.consulta_id)
        .where(AnsConsulta.reg_id == reg_id, AnsConsulta.campo == campo,
               AnsConsultaMsg.direccion == "ia")
        .order_by(AnsConsultaMsg.id.desc()).limit(5))
    calidad = None
    for meta in r.scalars().all():                 # ya vienen de la más nueva a la más vieja
        try:
            d = json.loads(meta) or {}
        except ValueError:
            continue
        if _lectura_util(d):
            calidad = d.get("calidad")
            break
    paso, etiqueta, _ayuda = CALIDAD_EMBUDO.get(calidad or "",
                                                ("revisar", "sin clasificar", ""))
    return {"calidad": calidad, "calidad_label": etiqueta, "embudo_paso": paso}


def _dict(a: WaAnalisis, reg: AnsRegistro | None = None) -> dict:
    d = {"id": a.id, "reg_id": a.reg_id, "campo": a.campo, "estado": a.estado,
         "creado_en": a.creado_en.isoformat() if a.creado_en else None,
         "resultado": json.loads(a.resultado) if a.resultado else None}
    if reg is not None:
        d["pdl"], d["zona"], d["fecha"] = reg.pdl, reg.zona, reg.fecha_fin
    return d


@router.post("/analizar")
async def analizar(datos: dict = Body(...), db: AsyncSession = Depends(get_db)):
    """Corre el análisis de un caso. Body: {reg_id} o {pdl}."""
    reg = None
    if datos.get("reg_id"):
        reg = await db.get(AnsRegistro, int(datos["reg_id"]))
    elif (datos.get("pdl") or "").strip():
        q = f"%{datos['pdl'].strip()}%"
        r = await db.execute(select(AnsRegistro).where(
            AnsRegistro.pdl.ilike(q), AnsRegistro.entorno == current_entorno()).limit(2))
        regs = r.scalars().all()
        if len(regs) > 1:
            raise HTTPException(status_code=400, detail="Más de un registro coincide con ese PDL")
        reg = regs[0] if regs else None
    if not reg:
        raise HTTPException(status_code=404, detail="Registro no encontrado (dame reg_id o pdl)")
    try:
        a = await wa_analista.analizar_caso(db, reg)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _dict(a, reg)


@router.get("/media-caso/{reg_id}")
async def media_caso(reg_id: int, minimo: int = 40, db: AsyncSession = Depends(get_db)):
    """Media de WhatsApp amarrada a un caso por el matcher determinista (con su criterio,
    puntaje y lo que leyó el índice). Es la lista de «no se te pasa ningún video»."""
    from app.models.models import WaMediaCaso, WaMediaMeta
    r = await db.execute(select(WaMediaCaso, WaMensaje, WaMediaMeta)
                         .join(WaMensaje, WaMensaje.id == WaMediaCaso.msg_id)
                         .outerjoin(WaMediaMeta, WaMediaMeta.msg_id == WaMediaCaso.msg_id)
                         .where(WaMediaCaso.reg_id == reg_id, WaMediaCaso.puntaje >= minimo)
                         .order_by(WaMediaCaso.puntaje.desc(), WaMensaje.enviado_en))
    out = []
    for a, m, meta in r.all():
        out.append({"msg_id": m.id, "tipo": m.tipo, "grupo": m.chat_nombre or m.chat_jid,
                    "enviado_en": m.enviado_en.isoformat() if m.enviado_en else None,
                    "caption": (m.texto or "")[:300] or None, "evidencia_id": m.evidencia_id,
                    "media": bool(m.media_ruta and os.path.exists(m.media_ruta)), "mime": m.media_mime,
                    "criterio": a.criterio, "puntaje": a.puntaje, "detalle": a.detalle,
                    "letrero": ({"fecha": meta.overlay_fecha, "hora": meta.overlay_hora, "codigo": meta.overlay_codigo,
                                 "lugar": meta.overlay_lugar, "lider": meta.overlay_lider,
                                 "transcripcion": (meta.transcripcion or "")[:600] or None} if meta else None)})
    return out


@router.post("/amarrar")
async def amarrar(datos: dict = Body(default={}), db: AsyncSession = Depends(get_db)):
    """Recalcula los amarres de un periodo ({periodo}) o de un caso ({reg_id})."""
    from app.services import matcher
    if datos.get("reg_id"):
        return await matcher.amarrar(db, reg_ids=[int(datos["reg_id"])])
    periodo = (datos.get("periodo") or "").strip()
    if not re.match(r"^\d{4}-\d{2}$", periodo):
        raise HTTPException(status_code=400, detail="Dame reg_id o periodo (YYYY-MM)")
    return await matcher.amarrar(db, periodo=periodo)


@router.get("/tablero")
async def tablero(periodo: str = Query(..., pattern=r"^\d{4}-\d{2}$"), db: AsyncSession = Depends(get_db)):
    """CICLO DE VIDA de cada caso por auditar del periodo (orquestación, paso 3): en qué estado
    va (nuevo → recolectando → analizado → decidido → verificado), qué evidencia tiene amarrada
    y QUÉ FALTA. Es el tablero «no se me pasa nada» de la Auditoría IA."""
    from app.api.ans import computar
    from app.models.models import WaMediaCaso
    r = await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo,
                                                   AnsRegistro.entorno == current_entorno()).order_by(AnsRegistro.id))
    regs = r.scalars().all()
    ids = [x.id for x in regs]
    if not ids:
        return []
    # análisis vigente por caso
    ra = await db.execute(select(WaAnalisis).where(WaAnalisis.reg_id.in_(ids), WaAnalisis.estado != "descartado")
                          .order_by(WaAnalisis.id.desc()))
    vigente = {}
    for a in ra.scalars().all():
        vigente.setdefault(a.reg_id, a)
    # amarres, evidencias, envíos, respuestas, correos
    ram = await db.execute(select(WaMediaCaso.reg_id, WaMediaCaso.puntaje).where(WaMediaCaso.reg_id.in_(ids)))
    fuertes, candidatos = {}, {}
    for reg_id, p in ram.all():
        (fuertes if p >= 75 else candidatos)[reg_id] = (fuertes if p >= 75 else candidatos).get(reg_id, 0) + 1
    rev = await db.execute(select(AnsEvidencia.reg_id, AnsEvidencia.campo).where(AnsEvidencia.reg_id.in_(ids)))
    evid = {}
    for reg_id, campo in rev.all():
        evid[(reg_id, campo)] = evid.get((reg_id, campo), 0) + 1
    ren = await db.execute(select(WaEnvio).where(WaEnvio.reg_id.in_(ids)).order_by(WaEnvio.id))
    envios = {}
    for e in ren.scalars().all():
        envios[(e.reg_id, e.campo)] = e                     # el último manda
    rq = await db.execute(select(AnsConsulta).where(AnsConsulta.reg_id.in_(ids)))
    consultas = {(c.reg_id, c.campo): c for c in rq.scalars().all()}
    rr = await db.execute(select(AnsConsultaMsg.consulta_id, AnsConsultaMsg.creado_en)
                          .where(AnsConsultaMsg.direccion == "recibido"))
    ult_resp = {}
    for cid, t in rr.all():
        if t and (cid not in ult_resp or t > ult_resp[cid]):
            ult_resp[cid] = t
    rc = await db.execute(select(CorreoAns).where(CorreoAns.estado != "descartado"))
    correos = {}
    for c in rc.scalars().all():
        try:
            for m in (json.loads(c.pdls) if c.pdls else []):
                if m.get("reg_id") in ids:
                    correos[m["reg_id"]] = correos.get(m["reg_id"], 0) + 1
        except ValueError:
            pass

    out = []
    for reg in regs:
        es_b = wa_analista._es_32b(reg)
        if es_b:
            tipo, decision_campo, decision = "3.2B", "excluible_anulado", (reg.excluible_anulado or "").strip()
            obs = (reg.obs_anulado or "").strip()
            pend = True
        else:
            c = computar(reg)
            tipo, decision_campo, decision = "3.2A", "excluible", (reg.excluible or "").strip()
            obs = (reg.obs or "").strip()
            pend = wa_analista._no_cumple_32a(c)       # misma regla que el parpadeo de la celda
        if not pend:
            continue
        a = vigente.get(reg.id)
        ia = {}
        if a:
            try:
                ia = (json.loads(a.resultado or "{}").get("ia") or {})
            except ValueError:
                ia = {}
        propuesta = (ia.get("excluible_sugerido") or ("Revisión" if ia.get("revision") else None)) if a else None
        n_f, n_c = fuertes.get(reg.id, 0), candidatos.get(reg.id, 0)
        n_e = evid.get((reg.id, decision_campo), 0)
        n_corr = correos.get(reg.id, 0)
        e = envios.get((reg.id, decision_campo))
        propuesto = None
        if a and not e and not (decision and obs):
            try:
                propuesto = await proponer_pedido(db, a, reg)
            except Exception:  # noqa: BLE001
                propuesto = None
        con = consultas.get((reg.id, decision_campo))
        respondido = bool(e and con and ult_resp.get(con.id) and ult_resp[con.id] >= (e.enviado_en or e.creado_en))
        soporte = (n_f > 0 or n_e > 0 or n_corr > 0)
        if decision and obs:
            estado = "verificado" if soporte else "decidido_sin_soporte"
        elif tipo == "3.2B" and n_corr > 0:
            estado = "analizado"                      # el correo del CO ya está: se aprueba en Correos ANS
            propuesta = propuesta or "correo en Correos ANS"
        elif a and (propuesta or ia.get("error")):
            estado = "analizado"
        elif e:
            estado = "recolectando"
        else:
            estado = "nuevo"
        if estado == "verificado":
            falta = "nada: decidido y con soporte"
        elif estado == "decidido_sin_soporte":
            falta = "adjuntar el soporte de la decisión (video, correo o chat)"
        elif estado == "analizado" and tipo == "3.2B" and n_corr > 0:
            falta = "aprobar la lectura del correo en Correos ANS (SI / NO)"
        elif estado == "analizado":
            falta = ("tu decisión (hay propuesta)" if propuesta else
                     ("reintentar el análisis (IA falló)" if ia.get("error") else "tu decisión"))
            if not soporte and not respondido:
                falta += " · evidencia pendiente" + (f" (pedida a {e.persona})" if e else " (sin pedido)")
        elif estado == "recolectando":
            if respondido:
                # Ya contestaron: lo que falta lo dice el EMBUDO, no un genérico «análisis
                # con la respuesta recibida» que no indica si hay que decidir, insistir o
                # cerrar (2026-09-24).
                cal = await _calidad_flujo(db, reg.id, decision_campo)
                falta = (f"{e.persona} respondió — {cal['calidad_label']}"
                         if cal.get("calidad")
                         else f"{e.persona} respondió — releer la conversación para clasificarla")
            else:
                falta = (f"respuesta de {e.persona} ({e.estado}"
                         + (f", enviado {e.enviado_en:%d/%m %H:%M}" if e.enviado_en else "") + ")")
        else:
            falta = "análisis y evidencia (aún no se ha pedido nada)"
        out.append({"reg_id": reg.id, "pdl": reg.pdl, "tipo": tipo, "zona": reg.zona, "fecha": reg.fecha_fin or reg.fecha_inicio,
                    "estado": estado, "decision": decision or None, "obs": bool(obs),
                    "analisis_id": a.id if a else None, "propuesta": propuesta, "ia_error": bool(ia.get("error")) if a else False,
                    "amarres_fuertes": n_f, "candidatos": n_c, "evidencias": n_e, "correos": n_corr,
                    "pedido": ({"persona": e.persona, "estado": e.estado,
                                "enviado_en": e.enviado_en.isoformat() if e.enviado_en else None,
                                "respondido": respondido} if e else None),
                    "pedido_propuesto": ({"persona": propuesto.get("persona"), "rol": propuesto.get("rol"),
                                          "posible": propuesto.get("posible"), "bloqueo": propuesto.get("bloqueo") or propuesto.get("motivo")}
                                         if propuesto else None),
                    "falta": falta})
    orden = {"nuevo": 0, "recolectando": 1, "analizado": 2, "decidido_sin_soporte": 3, "verificado": 4}
    return sorted(out, key=lambda x: (orden.get(x["estado"], 9), x["pdl"] or ""))


@router.get("/pendientes")
async def pendientes(periodo: str = Query(..., pattern=r"^\d{4}-\d{2}$"), reintentar: bool = False,
                     db: AsyncSession = Depends(get_db)):
    """Casos del periodo que la Auditoría IA debería tener y no tiene (o cuyo análisis
    falló, con `reintentar`). Alimenta el botón «Analizar pendientes» del tab."""
    casos = await wa_analista.casos_pendientes(db, periodo, reintentar)
    return [{"reg_id": r.id, "pdl": r.pdl, "zona": r.zona, "fecha": r.fecha_fin, "motivo": m}
            for r, m in casos]


@router.post("/analizar-pendientes")
async def analizar_pendientes(background: BackgroundTasks, datos: dict = Body(default={}),
                              db: AsyncSession = Depends(get_db)):
    """Encola en segundo plano el análisis de TODOS los pendientes del periodo (1–3 min
    por caso; la lista del tab se refresca sola). Body: {periodo, reintentar?}."""
    periodo = (datos.get("periodo") or "").strip()
    if not re.match(r"^\d{4}-\d{2}$", periodo):
        raise HTTPException(status_code=400, detail="Falta el periodo (YYYY-MM)")
    reintentar = bool(datos.get("reintentar"))
    casos = await wa_analista.casos_pendientes(db, periodo, reintentar)
    if casos:
        background.add_task(wa_analista.analizar_pendientes, periodo, reintentar)
    return {"en_cola": len(casos), "casos": [{"pdl": r.pdl, "motivo": m} for r, m in casos]}


@router.get("/analisis")
async def listar(periodo: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
                 db: AsyncSession = Depends(get_db)):
    """`periodo` (YYYY-MM): solo los análisis de casos de ESE periodo del ANS — el tab va
    de la mano del selector de periodo de la página (requisito 2026-09-07: antes la
    Auditoría IA mostraba todos los periodos mezclados)."""
    r = await db.execute(select(WaAnalisis).where(WaAnalisis.entorno == current_entorno())
                         .order_by(WaAnalisis.id.desc()).limit(300))
    todos = r.scalars().all()
    # Solo el análisis VIGENTE de cada caso (requisito 2026-08-25): el re-análisis REEMPLAZA
    # al anterior en el tab. En la BD queda todo como historia. Un descartado solo aparece (tras su
    # toggle) si el caso no tiene ningún análisis vivo más nuevo.
    vigente = {}
    for a in todos:                       # ya vienen DESC: el primero es el más nuevo
        if a.estado != "descartado" and a.reg_id not in vigente:
            vigente[a.reg_id] = a.id
    todos = [a for a in todos
             if vigente.get(a.reg_id) == a.id
             or (a.estado == "descartado" and a.reg_id not in vigente)]
    from app.api.ans import _pend_campos
    out = []
    for a in todos:
        reg = await db.get(AnsRegistro, a.reg_id)
        if periodo and getattr(reg, "periodo", None) != periodo:
            continue
        res = json.loads(a.resultado) if a.resultado else {}
        ia = res.get("ia") or {}
        # ¿Qué le falta a la CELDA en la tabla? (2026-10-01: «Ver siguiente pendiente (16)»
        # paseaba por 15 casos ya decididos en la grilla o en Correos ANS mientras la tabla
        # pedía 3). Misma regla que el parpadeo: decidir SI/NO, o escribir la razón.
        tipo = res.get("tipo") or "3.2A"
        c_dec, c_obs = ("excluible_anulado", "obs_anulado") if tipo == "3.2B" else ("excluible", "obs")
        pend = _pend_campos(reg) if reg else {}
        falta_celda = "decidir" if c_dec in pend else ("razón" if c_obs in pend else None)
        out.append({"id": a.id, "reg_id": a.reg_id, "estado": a.estado,
                    "celda": (getattr(reg, c_dec, None) or "").strip() or None if reg else None,
                    "celda_pendiente": bool(falta_celda), "falta_celda": falta_celda,
                    "tipo": tipo,
                    "pdl": getattr(reg, "pdl", None), "zona": getattr(reg, "zona", None),
                    "fecha": getattr(reg, "fecha_fin", None),
                    "excluible_sugerido": ia.get("excluible_sugerido"),
                    "categoria": ia.get("categoria"), "confianza": ia.get("confianza"),
                    "error": ia.get("error"),
                    "creado_en": a.creado_en.isoformat() if a.creado_en else None,
                    **(await _flujo(db, a, res))})
    return out


def _tokens_nombre(s: str) -> set:
    """Tokens de un nombre para el pareo (mayúsculas, sin tildes, palabras de 3+ letras
    — fuera «DE», «LA»...). Mismo espíritu que cuenta_de_sigla: palabra completa, nunca
    subcadena."""
    import unicodedata
    s = unicodedata.normalize("NFD", (s or "").upper())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return set(re.findall(r"[A-ZÑ]{3,}", s))


async def _perfiles_whatsapp(db: AsyncSession) -> list[dict]:
    """La gente vista hablando en los GRUPOS: (número real, nombre de perfil, dónde y
    cuánto). Es la mina para sugerir teléfonos de líderes/supervisores — en esos grupos
    están justamente esos cargos (2026-08-20)."""
    from sqlalchemy import func
    r = await db.execute(
        select(WaMensaje.remitente_jid, WaMensaje.remitente_nombre, WaMensaje.chat_jid,
               func.count().label("n"))
        .where(WaMensaje.es_grupo.is_(True), WaMensaje.remitente_nombre.isnot(None),
               WaMensaje.remitente_jid.like("57%@s.whatsapp.net"))
        .group_by(WaMensaje.remitente_jid, WaMensaje.remitente_nombre, WaMensaje.chat_jid))
    rg = await db.execute(select(WaGrupo))
    nombres_grupo = {g.jid: (g.nombre or g.jid) for g in rg.scalars().all()}
    por_persona = {}
    for jid, nombre, chat, n in r.all():
        tel = re.sub(r"\D", "", jid.split("@")[0])
        p = por_persona.setdefault((tel, nombre), {"telefono": tel, "nombre_wa": nombre,
                                                   "mensajes": 0, "grupos": set()})
        p["mensajes"] += n
        p["grupos"].add(nombres_grupo.get(chat, "?"))
    return [{**p, "grupos": sorted(p["grupos"])[:3], "tokens": _tokens_nombre(p["nombre_wa"])}
            for p in por_persona.values()]


def _sugerir_telefonos(nombre_ans: str, perfiles: list, agenda: list,
                       zona_caso: str | None = None) -> list[dict]:
    """Sugerencias de teléfono para un nombre del ANS, de DOS minas: los perfiles de
    WhatsApp («APELLIDO1 APELLIDO2 NOMBRE1 NOMBRE2» ↔ perfil «Nombre2 Apellido1») y el
    DIRECTORIO con otra grafía («APELLIDO SEGUNDO NOMBRE» ↔ contacto «Nombre apellido» — el
    pareo exacto por nombre no los une). Mínimo 2 palabras en común; máx 3 sugerencias,
    mejores primero. Son SUGERENCIAS: la persona responsable aprueba o rechaza, nunca se rellena solo.

    Blindaje contra HOMÓNIMOS (caso 2026-08-25: nombre + primer apellido casaron con un
    perfil de otra zona que compartía ambos pero tenía otro segundo apellido):
    · `zona_ok` — False si la zona/grupos del sugerido NO tocan la zona del caso;
    · `extras`  — palabras del perfil que NO están en el nombre buscado (apellidos que
      no cuadran). El tab las muestra como advertencia."""
    objetivo = _tokens_nombre(nombre_ans)
    patrones = wa_analista.GRUPOS_ZONA.get(
        (zona_caso or "").upper().replace("É", "E"), [])

    def _zona_ok_dir(zona):
        if not patrones or not (zona or "").strip():
            return None
        return any(p in zona.lower().replace("é", "e") for p in patrones)

    def _zona_ok_wa(grupos):
        if not patrones or not grupos:
            return None
        return any(any(p in (g or "").lower().replace("é", "e") for p in patrones)
                   for g in grupos)

    hits = []
    for c in agenda:                              # el directorio primero: dato ya curado
        comunes = objetivo & c["tokens"]
        if len(comunes) >= 2:
            hits.append({"origen": "directorio", "telefono": c["telefono"],
                         "nombre_wa": c["nombre"], "rol": c.get("rol"),
                         "zona": c.get("zona"), "zona_ok": _zona_ok_dir(c.get("zona")),
                         "extras": sorted(c["tokens"] - objetivo)[:3],
                         "coincidencias": sorted(comunes), "mensajes": 0, "grupos": []})
    for p in perfiles:
        comunes = objetivo & p["tokens"]
        if len(comunes) >= 2 and not any(h["telefono"] == p["telefono"] for h in hits):
            hits.append({"origen": "whatsapp", "telefono": p["telefono"],
                         "nombre_wa": p["nombre_wa"], "rol": None,
                         "zona": None, "zona_ok": _zona_ok_wa(p["grupos"]),
                         "extras": sorted(p["tokens"] - objetivo)[:3],
                         "coincidencias": sorted(comunes), "mensajes": p["mensajes"],
                         "grupos": p["grupos"]})
    hits.sort(key=lambda h: (h["origen"] != "directorio",
                             -len(h["coincidencias"]), -h["mensajes"]))
    return hits[:3]


def _parrafo_hallazgos(ia: dict | None) -> str | None:
    """El «esto YA lo tengo» del mensaje de Paso 2 (requisito 2026-08-25): si la
    máquina ya encontró videos del caso pero los tiempos no cubren, el mensaje LO DICE —
    pedir «el video» a secas hace que la persona conteste con lo que ya tenemos (le pasó
    con el líder y el supervisor de un caso: llamaron a repetir lo ya hallado)."""
    if not ia:
        return None
    cov = ia.get("cobertura_calc") or {}
    vids = [v for v in (ia.get("videos") or []) if v.get("del_caso") == "SI"]
    tramos = ia.get("tramos_justificados") or []
    if (not vids and not tramos) or not cov.get("aplicable") \
            or cov.get("legado") or cov.get("cubierto"):
        return None
    partes = []
    for gap, g in (cov.get("gaps") or {}).items():
        if g.get("cubierto"):
            continue
        partes.append(f"del desfase de {gap} de {g['requerido']} min ya tengo "
                      f"soportados {g['justificado']} min y me faltan {g['faltan']} min")
    if not partes:
        return None
    if vids:
        horas = ", ".join((v.get("hora_overlay") or "¿?") for v in vids[:6])
        n = len(vids)
        fuente = (f"{n} video{'s' if n != 1 else ''} del frente de trabajo de "
                  f"ese día ({horas})")
    else:
        # Sin videos pero con tramos: la evidencia salió del reporte del caso en el
        # grupo (plantilla oficial con horas de llamadas/respuestas del CC).
        fuente = "el reporte del caso en el grupo (plantilla oficial con sus horas)"
    return f"Ya cuento con {fuente}; {'; '.join(partes)}."


async def _candidatos_32a(db: AsyncSession, reg: AnsRegistro,
                          ia: dict | None = None) -> list[dict]:
    """El PASO 2 del popup para un 3.2 A: a quién preguntarle por el video — el líder RI
    (col E, estuvo en campo) y el supervisor (col G, responde por la cuadrilla). El
    teléfono sale del directorio EN VIVO (no se congela en el análisis: si se
    guarda hoy, aparece de una). El mensaje parte de la plantilla oficial del chat
    guiado; si la máquina ya halló videos sin cubrir los tiempos, se enriquece con lo
    que ya se tiene y lo que falta (pedido 2026-08-25)."""
    from app.api.ans import _mensaje_wa, _norm_nombre, _ya_escrito_hoy
    out = []
    perfiles = None       # la mina de WhatsApp se carga solo si algún candidato no tiene número
    for crudo, rol in ((reg.ri_jt, "lider"), (reg.nombre_responsable, "supervisor")):
        # «APELLIDO1 APELLIDO2 NOMBRE1 NOMBRE2 (CO00000000)» → sin el código CO
        nombre = re.sub(r"\s+", " ", str(crudo or "")).split("(")[0].strip()
        if not nombre or any(c["nombre"] == nombre for c in out):
            continue
        # Si HOY ya se le escribió (por cualquier caso), el mensaje sigue la conversación
        # sin repetir el «Buen día» — 4 saludos idénticos seguidos se ven robóticos.
        saludar = not await _ya_escrito_hoy(db, nombre)
        rc = await db.execute(select(Contacto).where(
            Contacto.nombre_norm == _norm_nombre(nombre), Contacto.activo.is_(True)))
        c = rc.scalars().first()
        # ¿A ESTE candidato ya se le encoló/envió la pregunta de este caso? Cada uno
        # lleva su propio estado: se puede preguntar a ambos en paralelo.
        re_env = await db.execute(select(WaEnvio).where(
            WaEnvio.reg_id == reg.id, WaEnvio.campo == "excluible",
            WaEnvio.persona == nombre).order_by(WaEnvio.id.desc()).limit(1))
        env = re_env.scalars().first()
        # Sin número en el directorio → sugerencias desde los perfiles de WhatsApp
        # (nombre de perfil ↔ nombre del ANS, mínimo 2 palabras en común). La persona
        # responsable aprueba o rechaza — nunca se rellena solo.
        sugerencias = []
        if not (c and c.telefono):
            if perfiles is None:
                perfiles = await _perfiles_whatsapp(db)
                ra = await db.execute(select(Contacto).where(
                    Contacto.activo.is_(True), Contacto.telefono != ""))
                agenda = [{"nombre": x.nombre, "telefono": x.telefono, "rol": x.rol,
                           "tokens": _tokens_nombre(x.nombre)} for x in ra.scalars().all()]
            sugerencias = _sugerir_telefonos(nombre, perfiles, agenda, zona_caso=reg.zona)
        mensaje = _mensaje_wa(reg, "excluible", quien=nombre, rol_dir=rol, saludar=saludar)
        # Mensaje CONSCIENTE de lo ya hallado: se reemplaza la pregunta genérica de la
        # plantilla por «esto tengo, esto falta». Si la plantilla oficial cambia de
        # texto, los replace no encuentran nada y queda la genérica — degrada seguro.
        hall = _parrafo_hallazgos(ia)
        if hall and mensaje:
            # La plantilla ya abre con el mini-resumen del desfase (_linea_desfase);
            # aquí solo se cambia la PREGUNTA genérica por la dirigida a lo que falta.
            mensaje = mensaje.replace(
                "Por favor, ¿me podría indicar qué ocurrió ese día? (condiciones "
                "climáticas, vía cerrada, terreno, un tercero, o si fue un tema "
                "interno).",
                hall + " ¿Cuenta con algún otro video o evidencia de ese día, o me "
                "puede indicar qué ocurrió en el tiempo que me falta por soportar?")
            mensaje = mensaje.replace(
                "Si cuenta con el video del frente de trabajo, le agradezco me lo pueda "
                "compartir. ",
                "Cualquier video o soporte adicional me sirve para completar el ANS. ")
        # Bloqueadores a la vista ANTES de encolar (correo ya existente / repetida)
        bloqueo = None
        if not env:
            bloqueo = await _bloqueo_envio(db, reg.id, "excluible",
                                           (c.telefono if c else None), nombre)
        cand = {"nombre": nombre, "rol": rol,
                "telefono": (c.telefono or None) if c else None,
                "sugerencias": sugerencias,
                "bloqueo": bloqueo,
                "envio": ({"pendiente": "encolado", "enviado": "enviado",
                           "error": "error_envio"}.get(env.estado) if env else None),
                "mensaje": mensaje}
        # SUPERVISOR SIN NÚMERO (regla 2026-09-23): quien lo consigue es el
        # profesional de apoyo de la zona. Se propone pedírselo (campo numero_supervisor) y,
        # si ya respondió, los números que dictó salen como sugerencia para guardar.
        if rol == "supervisor" and not cand["telefono"]:
            cand["pedir_numero"] = await _pedir_numero_al_apoyo(db, reg, nombre)
            # El CD10 trae el celular del supervisor en la columna AX (2026-09-23): primera sugerencia
            try:
                tel_cd10 = (json.loads(reg.datos_extra or "{}") or {}).get("telefono_responsable_cd10")
            except ValueError:
                tel_cd10 = None
            if tel_cd10 and len(tel_cd10) >= 12:
                cand["sugerencias"] = [{"origen": "cd10", "nombre_wa": nombre, "telefono": tel_cd10,
                                        "zona_ok": True, "extras": []}] + cand["sugerencias"]
            cand["sugerencias"] = cand["sugerencias"] + await _numeros_dictados(db, reg, nombre)
        out.append(cand)
    return out


async def _pedir_numero_al_apoyo(db: AsyncSession, reg: AnsRegistro, supervisor: str) -> dict:
    """La propuesta de pedirle al apoyo de la zona el número del supervisor (misma forma que un
    candidato: persona, teléfono, mensaje, estado del envío)."""
    from app.api.ans import _mensaje_pedir_numero, _ya_escrito_hoy
    zona = (reg.zona or "").strip().upper()
    apoyo = None
    if zona:
        apoyo = (await db.execute(select(Contacto).where(
            Contacto.activo.is_(True), Contacto.rol == "apoyo", Contacto.telefono != "",
            Contacto.zona.ilike(f"%{zona}%")))).scalars().first()
    if not apoyo:
        return {"posible": False, "motivo": (f"no hay profesional de apoyo con número para la zona {zona}"
                                             if zona else "la orden no tiene zona (primero el Paso 1)")}
    re_env = await db.execute(select(WaEnvio).where(WaEnvio.reg_id == reg.id, WaEnvio.campo == "numero_supervisor",
                                                    WaEnvio.persona == apoyo.nombre)
                              .order_by(WaEnvio.id.desc()).limit(1))
    env = re_env.scalars().first()
    return {"posible": True, "campo": "numero_supervisor", "nombre": apoyo.nombre, "rol": "apoyo",
            "telefono": apoyo.telefono, "supervisor": supervisor,
            "envio": ({"pendiente": "encolado", "enviado": "enviado", "error": "error_envio"}.get(env.estado)
                      if env else None),
            "mensaje": _mensaje_pedir_numero(reg, apoyo.nombre, supervisor,
                                             saludar=not await _ya_escrito_hoy(db, apoyo.nombre))}


async def _numeros_dictados(db: AsyncSession, reg: AnsRegistro, supervisor: str) -> list[dict]:
    """Celulares que aparecen en las respuestas del hilo «numero_supervisor» del caso → sugerencias
    (origen «respuesta») para guardarlas con un clic. Nunca se guardan solas."""
    r = await db.execute(select(AnsConsultaMsg).join(AnsConsulta, AnsConsulta.id == AnsConsultaMsg.consulta_id)
                         .where(AnsConsulta.reg_id == reg.id, AnsConsulta.campo == "numero_supervisor",
                                AnsConsultaMsg.direccion == "recibido").order_by(AnsConsultaMsg.id))
    out, vistos = [], set()
    for t in r.scalars().all():
        for n in re.findall(r"(?<!\d)(?:\+?57)?\s?(3\d{2})[\s.\-]?(\d{3})[\s.\-]?(\d{4})(?!\d)", t.texto or ""):
            tel = "57" + "".join(n)
            if tel in vistos:
                continue
            vistos.add(tel)
            out.append({"origen": "respuesta", "nombre_wa": t.persona or "profesional de apoyo", "telefono": tel,
                        "texto": (t.texto or "")[:160], "zona_ok": True, "extras": []})
    return out


@router.get("/analisis/{aid}")
async def detalle(aid: int, db: AsyncSession = Depends(get_db)):
    a = await db.get(WaAnalisis, aid)
    if not a:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    reg = await db.get(AnsRegistro, a.reg_id)
    d = _dict(a, reg)
    res = json.loads(a.resultado or "{}")
    d.update(await _flujo(db, a, res))

    # ── El GRUPO de origen, resuelto por el panel (no por la IA) ──────────────────
    # Requisito de auditoría: hay que saber de qué grupo salió cada dato.
    # Se enriquece EN VIVO y por msg_id — determinista y retroactivo para los análisis
    # viejos, en vez de confiar en que el modelo lo haya citado.
    res_d = d.get("resultado") or {}

    # ── Cobertura calculada EN VIVO también para análisis viejos (requisito
    # 2026-08-25: los ajustes son para TODOS los casos, no solo los nuevos). Es puro
    # código ($0): si el análisis se congeló sin cobertura_calc, se calcula aquí con
    # los datos actuales del registro. Los anteriores a la regla no declararon tramos
    # → se marca `legado` y el tab muestra la ventana del desfase sin veredicto.
    ia_d = res_d.get("ia") or {}
    if (reg is not None and (res_d.get("tipo") or "3.2A") == "3.2A"
            and not ia_d.get("error") and not ia_d.get("cobertura_calc")):
        from app.services.wa_analista import _calcular_cobertura
        cov = _calcular_cobertura(reg, ia_d)
        if "tramos_justificados" not in ia_d:
            cov["legado"] = True
        ia_d["cobertura_calc"] = cov

    # LECTURA DINÁMICA DE LA CONVERSACIÓN (14 oct 2026): el bloque «Lo que justifica» ve la cobertura ACUMULADA de los
    # dos canales (videos del análisis + tramos declarados en la conversación), la misma del embudo; y los
    # tramos de la conversación entran como hechos con su fuente. Sin esto, lo que respondía la persona no
    # llegaba al bloque (la persona explicaba la apertura y el bloque seguía en «0 min»).
    # (MODELO RÁPIDO DE RESPALDO (14 oct 2026): el embudo se arma más abajo; la acumulada se calcula aquí directo.)
    _c, _turnos = (None, [])
    if reg is not None and (res_d.get("tipo") or "3.2A") == "3.2A" and not ia_d.get("error"):
        _c, _turnos = await _hilo_y_turnos(db, reg.id, "excluible")
    cov_acum = _cobertura_acumulada(reg, res_d, _turnos) if _c else None
    if cov_acum:
        ia_d["cobertura_calc"] = cov_acum
        ia_d["cobertura_acumulada"] = True
        from app.api.ans import _tramos_del_hilo
        lt = ia_d.setdefault("linea_tiempo", [])
        _ult = next((t for t in reversed(_turnos or []) if t.direccion == "recibido" and t.persona), None)
        quien = (_ult.persona if _ult else (_c.persona_actual if _c else "")) or ""
        for t in _tramos_del_hilo(_turnos or []):
            h = {"hora": (t.get("desde") or "")[-5:], "fuente": "respuesta por WhatsApp" + (f" · {quien}" if quien else ""),
                 "hecho": f"{t.get('desde')}–{t.get('hasta')} ({t.get('gap')}): {t.get('evidencia') or ''}"}
            if h not in lt:
                lt.append(h)

    jids = res_d.get("grupos") or []
    nombres = {}
    if jids:
        rg = await db.execute(select(WaGrupo).where(WaGrupo.jid.in_(jids)))
        nombres = {g.jid: (g.nombre or g.jid) for g in rg.scalars().all()}
        d["grupos_nombres"] = [nombres.get(j, j) for j in jids]

    async def _grupo_de_msg(mid):
        m = await db.get(WaMensaje, mid) if mid else None
        if not m:
            return None
        return nombres.get(m.chat_jid) or m.chat_nombre

    for v in (res_d.get("videos") or []):
        if not v.get("grupo"):
            v["grupo"] = await _grupo_de_msg(v.get("msg_id"))
    for item in ((res_d.get("ia") or {}).get("linea_tiempo") or []):
        if item.get("grupo"):
            continue
        hit = re.search(r"msg[_ ]?id[= ]?(\d+)",
                        f"{item.get('fuente') or ''} {item.get('hecho') or ''}", re.I)
        if hit:
            item["grupo"] = await _grupo_de_msg(int(hit.group(1)))
    # LO QUE JUSTIFICA Y LO QUE SE ADJUNTA (14 oct 2026): en vivo, también para análisis viejos; y la
    # observación propuesta con la cuenta de «SE ANEXAN» corregida (solo los que justifican).
    if reg is not None and (res_d.get("tipo") or "3.2A") == "3.2A" and not ia_d.get("error"):
        from app.services.claude import _cuadrar_anexos, _cuadrar_tiempos
        rt = wa_analista.resumen_tiempos(reg, ia_d, res_d, docs=await _docs_de(db, reg.id),
                                         evidencias=await _videos_celda(db, reg.id))
        ia_d["resumen_tiempos"] = rt
        if rt["con_tramos"] and a.estado == "propuesto":
            ia_d["observacion"] = _cuadrar_tiempos(_cuadrar_anexos(ia_d.get("observacion") or "", rt["anexar"]))
        rt["captura"] = await _info_captura(db, a, reg, res_d, rt)
        if a.estado == "propuesto" and rt.get("solidez") == "no_solida" and rt["captura"].get("mensajes"):
            ia_d["observacion"] = wa_analista.cuadrar_captura(ia_d.get("observacion") or "", rt["captura"].get("citadas") or [])
    if reg is not None and (res.get("tipo") or "3.2A") == "3.2A":
        d["preguntar"] = await _candidatos_32a(db, reg, ia_d)
    # PASO 1 (2026-09-23): orden sin zona o con celular de supervisor → preguntar a programación
    if reg is not None and a.estado == "propuesto":
        d["programadora"] = await consulta_programadora(db, reg)
    if reg is not None and res.get("tipo") == "3.2B" and d.get("resultado"):
        # El contacto del apoyo se resuelve EN VIVO, no con lo congelado en el análisis:
        # si el apoyo de la zona se guardó DESPUÉS del barrido, el caso lo ve de una. El
        # mensaje también se regenera en vivo: con «Buen día» solo si es el primero del
        # día para esa persona (regla del chat guiado — nada de 4 saludos seguidos).
        from app.api.ans import _mensaje_wa, _ya_escrito_hoy
        zona = (reg.zona or "").strip().upper()
        apoyo = None
        # SIN ZONA NO SE ELIGE APOYO (fallo 2026-09-29): con zona vacía, ilike '%%' calzaba con
        # TODOS los apoyos y `.first()` devolvía al apoyo de otra zona — se le pidió el correo
        # de una orden que no era suya. Sin zona, primero el Paso 1 (preguntar a programación).
        if zona:
            rc = await db.execute(select(Contacto).where(
                Contacto.activo.is_(True), Contacto.rol == "apoyo",
                Contacto.zona.ilike(f"%{zona}%")).order_by(Contacto.id))
            apoyo = rc.scalars().first()
        c0 = d["resultado"].setdefault("consulta", {})
        if apoyo and apoyo.telefono:
            c0["mensaje"] = _mensaje_wa(reg, "excluible_anulado",
                                        quien=apoyo.nombre, rol_dir="apoyo",
                                        saludar=not await _ya_escrito_hoy(db, apoyo.nombre))
            c0.update({"persona": apoyo.nombre, "telefono": apoyo.telefono,
                       "sin_contacto": None, "sin_zona": None})
        else:
            # Nada congelado del análisis queda a la vista: el botón manda lo que se ve.
            c0.update({"persona": None, "telefono": None, "sin_zona": None if zona else True,
                       "mensaje": _mensaje_wa(reg, "excluible_anulado", quien="", rol_dir="apoyo")})
            c0["sin_contacto"] = (f"No hay profesional de apoyo con teléfono para la "
                                  f"zona {zona} en el directorio" if zona else
                                  "La orden no tiene zona: no se sabe qué profesional de apoyo le corresponde. "
                                  "Primero el Paso 1 (arriba): preguntarle a quien creó la orden la zona y el "
                                  "supervisor, o corregir la zona en el grid.")
        # Bloqueador a la vista (2026-08-27): si el correo del caso ya llegó, el tab
        # lo dice ANTES del botón de encolar — pedirlo de nuevo es ilógico.
        d["bloqueo_envio"] = await _bloqueo_envio(
            db, a.reg_id, "excluible_anulado",
            (c0.get("telefono") or None), (c0.get("persona") or None))

    # ── Conversación de WhatsApp del caso, EN la plataforma (pedido 2026-08-25) ──
    # Trazabilidad sin abrir WhatsApp: el hilo de consulta completo (lo que el bot
    # envió y lo que respondieron, ya procesado) en orden y con hora local.
    if reg is not None:
        from app.api.ans import _turno_wa, _chat_whatsapp
        campo_con = _campo_de(res)
        rc = await db.execute(select(AnsConsulta).where(
            AnsConsulta.reg_id == a.reg_id, AnsConsulta.campo == campo_con)
            .order_by(AnsConsulta.id))
        conv, chat = [], []
        for con in rc.scalars().all():
            rm = await db.execute(select(AnsConsultaMsg)
                                  .where(AnsConsultaMsg.consulta_id == con.id)
                                  .order_by(AnsConsultaMsg.creado_en, AnsConsultaMsg.id))
            turnos_con = rm.scalars().all()
            # El chat TAL CUAL WhatsApp (2026-10-01): mismo armado que «Ver justificación»
            try:
                chat += await _chat_whatsapp(db, con, turnos_con)
            except Exception:  # noqa: BLE001 — si falla, queda la conversación por turnos
                pass
            for m in turnos_con:
                # Quién lo escribió (bot / a mano / chat guiado) y la hora REAL de WhatsApp
                # (2026-10-01: lo que se contestaba a mano desde WhatsApp no aparecía)
                tw = await _turno_wa(db, a.reg_id, m)
                real = datetime.fromisoformat(tw["cuando_wa"]) if tw["cuando_wa"] else None
                fila = {"direccion": m.direccion, "persona": m.persona, "texto": m.texto,
                        "via": tw["via"],
                        "hora": (real.strftime("%d/%m %H:%M") if real else
                                 (m.creado_en - timedelta(hours=5)).strftime("%d/%m %H:%M")
                                 if m.creado_en else None)}
                # LA MEDIA, reproducible en el tab (2026-09-24): hasta hoy un audio o una
                # foto solo dejaban el rótulo «[adjuntó imagen]» y había que abrir WhatsApp
                # para verlos — con la conversación aquí y la media allá, no se audita nada.
                # El texto del turno se PARTE: lo que escribió la persona va en `texto`, lo
                # que la máquina leyó del adjunto en `lectura` (plegado en pantalla).
                if m.direccion == "recibido":
                    humano, lectura_txt, adjunto = _partir_turno(m.texto)
                    fila.update({"texto": humano, "lectura": lectura_txt, "adjunto": adjunto})
                    try:
                        meta = json.loads(m.meta) if m.meta else {}
                    except ValueError:
                        meta = {}
                    if meta.get("msg_id"):
                        fila.update({"msg_id": meta["msg_id"], "tipo": meta.get("tipo"),
                                     "mime": meta.get("mime"),
                                     "lectura": _sin_markdown(meta.get("lectura")) or lectura_txt})
                    elif meta.get("wa_id"):
                        # Turnos anteriores al 24-09: el msg_id no se guardaba, se busca por wa_id
                        rw = await db.execute(select(WaMensaje).where(
                            WaMensaje.wa_id == meta["wa_id"]))
                        wm = rw.scalars().first()
                        if wm and wm.media_ruta:
                            fila.update({"msg_id": wm.id, "tipo": wm.tipo, "mime": wm.media_mime})
                conv.append(fila)
        d["conversacion"] = conv
        d["chat"] = chat

        # ── EL EMBUDO: qué respondieron, qué calidad tuvo y qué sigue ────────────
        d["embudo"] = await _embudo(db, reg, res, a)
        # OBSERVACIÓN FINAL ÚNICA (14 oct 2026): con el embudo en «decidir», el mismo texto arriba y abajo.
        emb_x = d.get("embudo")
        if emb_x and emb_x.get("paso") == "decidir" and (res.get("tipo") or "3.2A") == "3.2A":
            try:
                texto_final = await _observacion_final(db, a, reg, res, emb_x)
            except Exception as ex:  # noqa: BLE001 — el detalle nunca se cae por esto
                import logging
                logging.getLogger("wa_auditoria").warning("observación final (detalle %s): %s", a.id, ex)
                texto_final = None
            if texto_final:
                emb_x["observacion_sugerida"] = texto_final
                ia_x = (d.get("resultado") or {}).get("ia")
                if isinstance(ia_x, dict):
                    ia_x["observacion"] = texto_final

        # Correos del caso (convergencia): desde Auditoría IA se ve si el caso también
        # tiene material en Correos ANS — dos canales, un solo expediente.
        rc2 = await db.execute(select(CorreoAns).where(CorreoAns.estado != "descartado"))
        correos = []
        for c2 in rc2.scalars().all():
            try:
                pdls = json.loads(c2.pdls) if c2.pdls else []
            except ValueError:
                continue
            if any(m.get("reg_id") == a.reg_id for m in pdls):
                correos.append({"id": c2.id, "asunto": (c2.asunto or "")[:80],
                                "fecha": c2.fecha_correo, "estado": c2.estado})
        d["correos_caso"] = correos
    return d


async def programadoras(db: AsyncSession) -> list[Contacto]:
    """Las programadoras del directorio (rol «programadora», activas) en orden de consulta: la
    primera es a quien se le pregunta; la siguiente, el escalamiento si no responde en 24 h.
    Orden = id; los cambios de personal se hacen en el directorio."""
    r = await db.execute(select(Contacto).where(Contacto.rol == "programadora", Contacto.activo.is_(True))
                         .order_by(Contacto.id))
    return r.scalars().all()


# ── PASO 1 A QUIEN CREÓ LA ORDEN (19 oct 2026, ver CHANGELOG) ──
async def _creador_orden(db: AsyncSession, reg: AnsRegistro) -> dict | None:
    """Quién CREÓ la orden: la col H («sigla elaborador», programador o cumplimentador; en las FT viene del STM, en
    Línea Viva es presunta). Se busca en el directorio por nombre exacto, por la cédula del «(CO…)» o por 2+ palabras
    con UNA sola programadora del directorio («APELLIDO1 APELLIDO2 NOMBRE1 NOMBRE2» ↔ «Nombre2 Apellido1»). Sin col H → None."""
    from app.api.ans import _norm_nombre, _sigla_presunta
    crudo = re.sub(r"\s+", " ", str(reg.sigla_elaborador or "")).strip()
    nombre = crudo.split("(")[0].strip()
    if not nombre:
        return None
    m = re.search(r"\(([^)]*)\)", crudo)
    cedula = re.sub(r"\D", "", m.group(1)) if m else ""
    activos = (await db.execute(select(Contacto).where(Contacto.activo.is_(True)))).scalars().all()
    n = _norm_nombre(nombre)
    c = next((x for x in activos if x.nombre_norm == n), None)
    if c is None and len(cedula) >= 6:
        c = next((x for x in activos if re.sub(r"\D", "", x.cedula or "") == cedula), None)
    if c is None:
        objetivo = _tokens_nombre(nombre)
        hits = [x for x in activos if x.rol == "programadora" and len(objetivo & _tokens_nombre(x.nombre)) >= 2]
        c = hits[0] if len(hits) == 1 else None
    return {"nombre": c.nombre if c else nombre, "contacto": c, "cedula": cedula or None,
            "presunta": _sigla_presunta(reg), "nombre_stm": nombre}


async def consulta_programadora(db: AsyncSession, reg: AnsRegistro) -> dict | None:
    """PASO 1 (regla 2026-09-23; a quien creó la orden desde el 19 oct 2026, ver CHANGELOG): si la orden llegó sin zona o con un CELULAR en la
    columna de supervisor, antes de buscar evidencia hay que preguntar a qué zona pertenece y quién es el
    supervisor. Se le pregunta a QUIEN CREÓ LA ORDEN (col H: el programador o cumplimentador que la elaboró).
    Si no tiene número, `alternas` ofrece a programación (primera programadora con número) para no frenar el caso;
    si lo tiene, `escalamiento` dice a quién le pregunta el sistema solo a las 24 h sin respuesta
    (recordatorios.escalar_programacion). Sin col H, a programación como antes. Devuelve la misma forma que un
    candidato del Paso 2 (el tab reusa el componente), o None si no aplica."""
    from app.api.ans import _mensaje_programadora, _responsable_es_telefono, _ya_escrito_hoy, _norm_nombre
    if (reg.zona or "").strip() and not _responsable_es_telefono(reg):
        return None
    # ¿El supervisor que trae la orden ya está en el directorio? Cambia la frase del hallazgo.
    sup = re.sub(r"\s+", " ", str(reg.nombre_responsable or "")).split("(")[0].strip()
    en_dir = bool(sup) and not _responsable_es_telefono(reg) and (await db.execute(select(Contacto.id).where(
        Contacto.nombre_norm == _norm_nombre(sup), Contacto.activo.is_(True)))).scalars().first() is not None
    lista = await programadoras(db)
    creador = await _creador_orden(db, reg)
    resp = (reg.nombre_responsable or "").strip()
    hallazgo = (f"el supervisor viene como número ({resp}) y la orden no tiene zona" if _responsable_es_telefono(reg)
                else f"la orden no tiene zona (supervisor: {resp or 'vacío'})")
    if not creador and not lista:
        return {"nombre": "Programación", "rol": "programadora", "telefono": None, "sugerencias": [],
                "campo": "zona_responsable", "sin_directorio": True, "mensaje": None,
                "hallazgo": "no hay contactos con rol «programadora» en el directorio"}
    # Cada candidato lleva SU envío (a quién ya se le preguntó este caso en este campo)
    envios = (await db.execute(select(WaEnvio).where(WaEnvio.reg_id == reg.id, WaEnvio.campo == "zona_responsable")
                               .order_by(WaEnvio.id.desc()))).scalars().all()
    mina = {}

    async def _cand(nombre: str, tel: str | None, info: dict | None = None,
                    remitido_por: str | None = None) -> dict:
        nn = _norm_nombre(nombre)
        env = next((e for e in envios if _norm_nombre(e.persona or "") == nn
                    or (tel and re.sub(r"\D", "", e.destino or "") == tel)), None)
        sugerencias = []
        if not tel:
            if not mina:
                ra = await db.execute(select(Contacto).where(Contacto.activo.is_(True), Contacto.telefono != ""))
                mina["perfiles"] = await _perfiles_whatsapp(db)
                mina["agenda"] = [{"nombre": x.nombre, "telefono": x.telefono, "rol": x.rol, "zona": x.zona,
                                   "tokens": _tokens_nombre(x.nombre)} for x in ra.scalars().all()]
            sugerencias = _sugerir_telefonos(nombre, mina["perfiles"], mina["agenda"], zona_caso=reg.zona)
        return {"nombre": nombre, "rol": "programadora", "telefono": tel or None, "sugerencias": sugerencias,
                "bloqueo": None, "campo": "zona_responsable", "hallazgo": hallazgo,
                "envio": ({"pendiente": "encolado", "enviado": "enviado", "error": "error_envio"}.get(env.estado)
                          if env else None),
                "envio_persona": env.persona if env else None,
                "en_directorio": en_dir,
                "creador": bool(info), "presunta": bool(info and info["presunta"]),
                "remitido_por": remitido_por,
                "cedula": (info or {}).get("cedula"),
                "mensaje": _mensaje_programadora(reg, nombre, saludar=not await _ya_escrito_hoy(db, nombre),
                                                 en_directorio=en_dir, creador=bool(info),
                                                 presunta=bool(info and info["presunta"]),
                                                 remitido_por=remitido_por)}

    if creador:
        ct = creador["contacto"]
        out = await _cand(creador["nombre"], (ct.telefono or None) if ct else None, creador)
    else:
        out = await _cand(lista[0].nombre, lista[0].telefono or None)
    # Programación: la primera con número que no sea la misma persona (criterio de recordatorios.py)
    prog = next((x for x in lista if x.telefono and _norm_nombre(x.nombre) != _norm_nombre(out["nombre"])), None)
    out["alternas"], out["escalamiento"] = [], None
    if prog and creador and (not out["telefono"] or any(_norm_nombre(e.persona or "") == _norm_nombre(prog.nombre)
                                                         for e in envios)):
        out["alternas"].append(await _cand(prog.nombre, prog.telefono))
    if prog and out["telefono"]:
        out["escalamiento"] = {"nombre": prog.nombre, "telefono": prog.telefono, "horas": 24}
    # ── PASO 1 CON RESPUESTA (19 oct 2026, ver CHANGELOG): lo que respondieron y lo que sigue ──
    from app.api.ans import _turno_wa
    respuestas = []
    hilos = (await db.execute(select(AnsConsulta).where(AnsConsulta.reg_id == reg.id,
                                                        AnsConsulta.campo == "zona_responsable")
                              .order_by(AnsConsulta.id))).scalars().all()
    for h in hilos:
        turnos = (await db.execute(select(AnsConsultaMsg).where(AnsConsultaMsg.consulta_id == h.id,
                                                                AnsConsultaMsg.direccion == "recibido")
                                   .order_by(AnsConsultaMsg.creado_en, AnsConsultaMsg.id))).scalars().all()
        for t in turnos:
            tw = await _turno_wa(db, reg.id, t)
            real = datetime.fromisoformat(tw["cuando_wa"]) if tw.get("cuando_wa") else None
            respuestas.append({"persona": t.persona, "texto": t.texto or "",
                               "hora": real.strftime("%d/%m %H:%M") if real else None})
    out["respuestas"] = respuestas
    quienes = {_norm_nombre(x["persona"] or "") for x in respuestas}
    for c in [out] + out["alternas"]:
        c["respondio"] = _norm_nombre(c["nombre"]) in quienes
    out["lectura"], out["siguiente"] = await _leer_paso1(db, respuestas), None
    rem = out["lectura"].get("remitido")
    ya = {_norm_nombre(c["nombre"]) for c in [out] + out["alternas"]}
    if rem and _norm_nombre(rem["nombre"]) not in ya:
        out["siguiente"] = await _cand(rem["nombre"], rem["telefono"], remitido_por=rem["por"])
    return out


_ZONAS_TXT = ((r"\bUBATE\b", "UBATE"), (r"\bALMEIDAS\b|\bCHOCONTA\b", "ALMEIDAS"),
              (r"\bGUAVIO\b|\bGACHETA\b", "GUAVIO"))


async def _leer_paso1(db: AsyncSession, respuestas: list) -> dict:
    """Lector DETERMINISTA de la respuesta del Paso 1 (PASO 1 CON RESPUESTA (19 oct 2026, ver CHANGELOG)): la zona, si nombra una sola
    de las tres FT, y la persona a la que remite («es de <nombre>», «pregúntele a <nombre>»): alguien del
    directorio o un elaborador de la col H, por su nombre de pila (y apellido si lo dice), uno solo. Lo que
    no entienda queda en None: la persona responsable lee la respuesta, que el tab muestra completa."""
    from app.api.ans import _norm_nombre
    if not respuestas:
        return {}
    txt = _norm_nombre(" ".join(x["texto"] or "" for x in respuestas))
    zonas = {z for pat, z in _ZONAS_TXT if re.search(pat, txt)}
    palabras = set(re.findall(r"[A-ZÑ]{3,}", txt)) - _PALABRAS_OPERADOR   # el saludo nombra a quien pregunta
    quienes = {_norm_nombre(x["persona"] or "") for x in respuestas}
    por = respuestas[-1]["persona"]
    activos = (await db.execute(select(Contacto).where(Contacto.activo.is_(True)))).scalars().all()
    gente = {}          # nombre normalizado → (nombre, teléfono, nombres de pila, todas las palabras)
    for c in activos:
        toks = [t for t in _norm_nombre(c.nombre).split() if len(t) >= 3]
        if toks:
            gente[c.nombre_norm] = (c.nombre, c.telefono or None, {toks[0]}, set(toks))
    # Elaboradores de la col H que no están en el directorio («APELLIDOS NOMBRES (CO…)»); si su cédula
    # ya es de un contacto, es esa persona (dos grafías del mismo nombre)
    cedulas = {re.sub(r"\D", "", c.cedula or ""): c for c in activos if c.cedula}
    rs = await db.execute(select(AnsRegistro.sigla_elaborador).where(AnsRegistro.sigla_elaborador.isnot(None))
                          .distinct())
    for (sig,) in rs.all():
        m = re.search(r"\(([^)]*)\)", sig or "")
        if m and re.sub(r"\D", "", m.group(1)) in cedulas:
            continue
        nombre = (sig or "").split("(")[0].strip()
        toks = [t for t in _norm_nombre(nombre).split() if len(t) >= 3]
        if len(toks) >= 3 and _norm_nombre(nombre) not in gente:
            gente[_norm_nombre(nombre)] = (nombre, None, set(toks[2:]), set(toks))
    hits = []
    for n, (nombre, tel, pila, todas) in gente.items():
        if n in quienes or not (palabras & pila):
            continue
        hits.append((len(palabras & todas), nombre, tel))
    remitido = None
    if hits:
        top = max(h[0] for h in hits)
        mejores = [h for h in hits if h[0] == top]
        if len(mejores) == 1:
            remitido = {"nombre": mejores[0][1], "telefono": mejores[0][2], "por": por}
    return {"zona": zonas.pop() if len(zonas) == 1 else None, "remitido": remitido}


async def encolar_pregunta(db: AsyncSession, reg: AnsRegistro, campo: str, persona: str, telefono: str,
                           rol: str, mensaje: str, forzar: bool = False, origen: str = "tab",
                           nota: bool = True) -> dict:
    """Encola UNA pregunta por WhatsApp y deja el hilo/bitácora como el botón del tab.
    Devuelve {"ok": True, …} o {"ok": False, "bloqueo": motivo}. Lo usan el botón y el
    circuito automático (orquestación 2026-09-07)."""
    from app.api.ans import _escribir_nota, _nota_espera
    telefono = re.sub(r"\D", "", telefono or "")
    if not persona or len(telefono) < 10 or not mensaje:
        return {"ok": False, "bloqueo": "faltan persona, teléfono válido o el mensaje"}
    if not forzar:
        motivo = await _bloqueo_envio(db, reg.id, campo, telefono, persona,
                                      seguimiento=(origen == "seguimiento"))
        if motivo:
            return {"ok": False, "bloqueo": motivo}
    db.add(WaEnvio(destino=telefono, persona=persona, texto=mensaje, reg_id=reg.id, campo=campo))
    r = await db.execute(select(AnsConsulta).where(
        AnsConsulta.reg_id == reg.id, AnsConsulta.campo == campo,
        AnsConsulta.estado != "descartada").order_by(AnsConsulta.id.desc()).limit(1))
    c = r.scalars().first()
    if not c:
        c = AnsConsulta(reg_id=reg.id, campo=campo, estado="abierta", persona_actual=persona)
        db.add(c)
        await db.flush()
    c.persona_actual = persona
    db.add(AnsConsultaMsg(consulta_id=c.id, direccion="enviado", persona=persona, texto=mensaje[:4000],
                          meta=json.dumps({"rol_dir": rol or "otro", "via": "bot", "origen": origen,
                                           "forzado": bool(forzar)})))   # SEGUIMIENTOS Y FORZADOS EN LA COLA DEL BOT (14 oct 2026)
    razon_field = "obs_anulado" if campo == "excluible_anulado" else "obs"
    # La consulta a programación (zona/supervisor) NO escribe en la observación de la celda:
    # esa celda es de ENEL y este dato es interno.
    if nota and campo not in ("zona_responsable", "numero_supervisor") and not (c.nota_auto or "").strip():
        _escribir_nota(reg, c, razon_field, _nota_espera(persona))
    await db.commit()
    return {"ok": True, "persona": persona, "telefono": telefono, "consulta_id": c.id}


def _hay_material(res: dict) -> bool:
    ia = res.get("ia") or {}
    return (any(v.get("del_caso") == "SI" for v in (ia.get("videos") or []))
            or bool(ia.get("tramos_justificados")))


async def proponer_pedido(db: AsyncSession, a: WaAnalisis, reg: AnsRegistro) -> dict | None:
    """Qué pediría el sistema para este caso (a quién, con qué mensaje) — SIN enviar nada.
    Regla (2026-09-07): la máquina propone, la persona responsable aprueba cada envío."""
    res = json.loads(a.resultado or "{}")
    if res.get("tipo") == "3.2B":
        c0 = res.get("consulta") or {}
        if not (reg.zona or "").strip():
            return {"posible": False, "motivo": "3.2 B sin zona: primero el Paso 1 (preguntar a programación)"}
        if not (c0.get("persona") and c0.get("telefono") and c0.get("mensaje")):
            return {"posible": False, "motivo": "3.2 B sin profesional de apoyo con número"}
        bloqueo = await _bloqueo_envio(db, reg.id, "excluible_anulado", c0["telefono"], c0["persona"])
        return {"posible": not bloqueo, "campo": "excluible_anulado", "persona": c0["persona"], "rol": "apoyo",
                "telefono": c0["telefono"], "mensaje": c0["mensaje"], "bloqueo": bloqueo}
    if (res.get("ia") or {}).get("error") or _hay_material(res):
        return None
    for cand in await _candidatos_32a(db, reg, res.get("ia")):
        if cand.get("envio"):
            return {"posible": False, "motivo": f"ya se le preguntó a {cand['nombre']} ({cand['envio']})"}
        if not cand.get("telefono"):
            continue
        return {"posible": not cand.get("bloqueo"), "campo": "excluible", "persona": cand["nombre"], "rol": cand["rol"],
                "telefono": cand["telefono"], "mensaje": cand["mensaje"], "bloqueo": cand.get("bloqueo")}
    return {"posible": False, "motivo": "ni el líder ni el supervisor tienen número en el directorio"}


async def pedir_material_faltante(db: AsyncSession, a: WaAnalisis, reg: AnsRegistro) -> dict | None:
    """CIRCUITO AUTOMÁTICO (orquestación 2026-09-07): si el análisis quedó sin material,
    el sistema pide la evidencia él mismo — 3.2 A al líder (o al supervisor si el líder no
    tiene número), 3.2 B al profesional de apoyo — con los MISMOS bloqueadores del botón
    (correo ya existente, pregunta repetida). Devuelve qué hizo, o None si no aplicaba."""
    from app.config import settings
    if not settings.WA_PEDIDOS_AUTO:
        return None
    res = json.loads(a.resultado or "{}")
    if res.get("tipo") == "3.2B":
        c0 = res.get("consulta") or {}
        if not (reg.zona or "").strip():
            return {"reg_id": reg.id, "pdl": reg.pdl, "accion": "sin_candidato", "detalle": "3.2 B sin zona: primero el Paso 1"}
        if not (c0.get("persona") and c0.get("telefono") and c0.get("mensaje")):
            return {"reg_id": reg.id, "pdl": reg.pdl, "accion": "sin_candidato", "detalle": "3.2 B sin apoyo con número"}
        r = await encolar_pregunta(db, reg, "excluible_anulado", c0["persona"], c0["telefono"], "apoyo",
                                   c0["mensaje"], origen="auto")
        return {"reg_id": reg.id, "pdl": reg.pdl, "accion": "encolado" if r["ok"] else "bloqueado",
                "persona": c0["persona"], "detalle": r.get("bloqueo")}
    if (res.get("ia") or {}).get("error") or _hay_material(res):
        return None
    for cand in await _candidatos_32a(db, reg, res.get("ia")):
        if cand.get("envio"):
            return {"reg_id": reg.id, "pdl": reg.pdl, "accion": "ya_pedido", "persona": cand["nombre"]}
        if not cand.get("telefono"):
            continue
        if cand.get("bloqueo"):
            return {"reg_id": reg.id, "pdl": reg.pdl, "accion": "bloqueado", "persona": cand["nombre"], "detalle": cand["bloqueo"]}
        r = await encolar_pregunta(db, reg, "excluible", cand["nombre"], cand["telefono"], cand["rol"],
                                   cand["mensaje"], origen="auto")
        return {"reg_id": reg.id, "pdl": reg.pdl, "accion": "encolado" if r["ok"] else "bloqueado",
                "persona": cand["nombre"], "rol": cand["rol"], "detalle": r.get("bloqueo")}
    return {"reg_id": reg.id, "pdl": reg.pdl, "accion": "sin_candidato",
            "detalle": "ni el líder ni el supervisor tienen número en el directorio"}


@router.get("/pedidos-propuestos")
async def pedidos_propuestos(periodo: str = Query(..., pattern=r"^\d{4}-\d{2}$"), db: AsyncSession = Depends(get_db)):
    """Lo que el sistema PROPONE pedir por WhatsApp para los casos del periodo sin material
    (persona, rol, mensaje completo). No envía nada: la persona responsable marca y aprueba."""
    r = await db.execute(select(WaAnalisis).join(AnsRegistro, AnsRegistro.id == WaAnalisis.reg_id)
                         .where(AnsRegistro.periodo == periodo, AnsRegistro.entorno == current_entorno(),
                                WaAnalisis.estado == "propuesto").order_by(WaAnalisis.id.desc()))
    vistos, out = set(), []
    for a in r.scalars().all():
        if a.reg_id in vistos:
            continue
        vistos.add(a.reg_id)
        reg = await db.get(AnsRegistro, a.reg_id)
        if not reg:
            continue
        decision = reg.excluible_anulado if res_tipo(a) == "3.2B" else reg.excluible
        if (decision or "").strip():
            continue
        p = await proponer_pedido(db, a, reg)
        if p:
            out.append({"reg_id": reg.id, "pdl": reg.pdl, "zona": reg.zona, "tipo": res_tipo(a), **p})
    return out


@router.post("/pedir-faltantes")
async def pedir_faltantes(datos: dict = Body(default={}), db: AsyncSession = Depends(get_db)):
    """Envía (encola al bot) los pedidos que la persona responsable APROBÓ: Body {periodo, reg_ids: [...]}.
    Sin `reg_ids` no se envía nada — regla «la máquina propone, la persona aprueba»."""
    periodo = (datos.get("periodo") or "").strip()
    if not re.match(r"^\d{4}-\d{2}$", periodo):
        raise HTTPException(status_code=400, detail="Falta el periodo (YYYY-MM)")
    aprobados = {int(x) for x in (datos.get("reg_ids") or []) if str(x).isdigit()}
    if not aprobados:
        raise HTTPException(status_code=400, detail="Indica los casos aprobados (reg_ids): la máquina no envía sin tu visto bueno")
    r = await db.execute(select(WaAnalisis).join(AnsRegistro, AnsRegistro.id == WaAnalisis.reg_id)
                         .where(AnsRegistro.periodo == periodo, AnsRegistro.entorno == current_entorno(),
                                WaAnalisis.estado == "propuesto").order_by(WaAnalisis.id.desc()))
    vistos, hechos = set(), []
    for a in r.scalars().all():
        if a.reg_id in vistos or a.reg_id not in aprobados:
            continue
        vistos.add(a.reg_id)
        reg = await db.get(AnsRegistro, a.reg_id)
        if not reg:
            continue
        p = await proponer_pedido(db, a, reg)
        if not p or not p.get("posible"):
            hechos.append({"reg_id": reg.id, "pdl": reg.pdl, "accion": "bloqueado", "detalle": (p or {}).get("bloqueo") or (p or {}).get("motivo")})
            continue
        rr = await encolar_pregunta(db, reg, p["campo"], p["persona"], p["telefono"], p["rol"], p["mensaje"], origen="aprobado")
        hechos.append({"reg_id": reg.id, "pdl": reg.pdl, "accion": "encolado" if rr["ok"] else "bloqueado",
                       "persona": p["persona"], "rol": p["rol"], "detalle": rr.get("bloqueo")})
    return {"casos": len(vistos), "acciones": hechos}


def res_tipo(a: WaAnalisis) -> str:
    try:
        return (json.loads(a.resultado or "{}").get("tipo")) or "3.2A"
    except ValueError:
        return "3.2A"


@router.post("/analisis/{aid}/preguntar")
async def preguntar(aid: int, datos: dict = Body(...), db: AsyncSession = Depends(get_db)):
    """PASO 2 del 3.2 A: encolar la pregunta por WhatsApp al líder/supervisor y dejar el
    caso en el MISMO circuito del 3.2 B — hilo de consulta abierto, bitácora «a la
    espera», y cuando responda, el procesador automático hace el resto.
    Body: {persona, telefono, rol, mensaje}."""
    a = await db.get(WaAnalisis, aid)
    if not a:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    reg = await db.get(AnsRegistro, a.reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="El registro del ANS ya no existe")
    persona = (datos.get("persona") or "").strip()
    telefono = re.sub(r"\D", "", datos.get("telefono") or "")
    mensaje = (datos.get("mensaje") or "").strip()
    # `campo`: excluible (Paso 2, líder/supervisor) o zona_responsable (Paso 1, programación —
    # 2026-09-23). Cualquier otro valor se rechaza.
    campo = (datos.get("campo") or "excluible").strip()
    # excluible_anulado: el remitido de un 3.2 B se encola por aquí (REMITEN A OTRA PERSONA (19 oct 2026, ver CHANGELOG))
    if campo not in ("excluible", "excluible_anulado", "zona_responsable", "numero_supervisor"):
        raise HTTPException(status_code=400, detail="campo inválido")
    if not persona or len(telefono) < 10 or not mensaje:
        raise HTTPException(status_code=400, detail="Faltan persona, teléfono válido o el mensaje")
    # BLOQUEADORES (2026-08-27): correo ya existente o pregunta repetida → no sale.
    # `forzar` solo llega del botón explícito del tab, ya con el motivo a la vista.
    # OJO: el análisis se queda en su estado — se puede encolar también al otro candidato
    # (líder Y supervisor en paralelo; responde cualquiera, o ambos, o ninguno) y todavía
    # aplicar/descartar la observación. El ciclo de vida del envío lo cuenta _flujo.
    r = await encolar_pregunta(db, reg, campo, persona, telefono, datos.get("rol") or "otro", mensaje,
                               forzar=bool(datos.get("forzar")), origen="tab")
    if not r.get("ok"):
        raise HTTPException(status_code=409, detail=f"BLOQUEADO: {r.get('bloqueo')}")
    return {"ok": True, "encolado": True, "persona": persona, "telefono": telefono, "campo": campo}


@router.get("/media/{msg_id}")
async def media(msg_id: int, db: AsyncSession = Depends(get_db)):
    """El archivo tal como llegó al grupo — para reproducir el video en el tab."""
    m = await db.get(WaMensaje, msg_id)
    if not m or not m.media_ruta or not os.path.exists(m.media_ruta):
        raise HTTPException(status_code=404, detail="Media no encontrada")
    nombre = os.path.basename(m.media_ruta)
    return StreamingResponse(open(m.media_ruta, "rb"),
                             media_type=m.media_mime or "application/octet-stream",
                             headers={"Content-Disposition": f'inline; filename="{nombre}"'})


# ── CAPTURA AUTOMÁTICA DEL CHAT (14 oct 2026) ──
async def _mensajes_captura(db: AsyncSession, reg: AnsRegistro, res: dict, rt: dict) -> tuple:
    """Los mensajes del grupo que sostienen los tramos: los que caen en la ventana del tramo (±margen)
    escritos por la cuadrilla del caso, y los que la evidencia del tramo cita por su hora (siempre).
    Devuelve (msgs, info) con info = {fecha, grupos_txt, tramos_txt, citadas: ["12:15", …]}."""
    ventanas, citadas, tramos_txt = [], set(), []
    for g in rt.get("gaps") or []:
        for t in g.get("tramos") or []:
            i, f = wa_analista._hhmm_a_min(t.get("desde")), wa_analista._hhmm_a_min(t.get("hasta"))
            if i is not None and f is not None:
                if f < i:
                    f += 1440
                ventanas.append((i - wa_analista.MARGEN_TRAMO_MIN, f + wa_analista.MARGEN_TRAMO_MIN))
                tramos_txt.append(f"{t.get('desde')}–{t.get('hasta')}")
            for h, mi in re.findall(r"\b(\d{1,2}):(\d{2})\b", t.get("evidencia") or ""):
                citadas.add(int(h) * 60 + int(mi))
    jids = res.get("grupos") or []
    if (not ventanas and not citadas) or not jids:
        return [], {}
    fecha = None
    caso = res.get("caso") if isinstance(res.get("caso"), dict) else {}
    for cand in (reg.fecha_inicio, caso.get("fecha")):
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})", cand or "")
        if m:
            fecha = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            break
    if not fecha:
        return [], {}
    r = await db.execute(select(WaMensaje).where(WaMensaje.chat_jid.in_(jids),
                                                 WaMensaje.enviado_en >= fecha + timedelta(hours=5),
                                                 WaMensaje.enviado_en < fecha + timedelta(hours=35))
                         .order_by(WaMensaje.enviado_en))
    filas = r.scalars().all()
    # NOMBRE DEL GRUPO EN LA CAPTURA (14 oct 2026): el nombre vive en wa_grupos (los mensajes no siempre lo traen)
    nombres = {g.jid: (g.nombre or g.jid) for g in
               (await db.execute(select(WaGrupo).where(WaGrupo.jid.in_(jids)))).scalars().all()}
    cuadrilla = {x.strip().upper() for x in re.findall(r"\(([^)]+)\) \[CUADRILLA DEL CASO\]",
                                                      "\n".join(str(e) for e in res.get("eventos") or []))}

    def _mins(m):
        loc = m.enviado_en - timedelta(hours=5)
        return loc, loc.hour * 60 + loc.minute + (1440 if loc.date() > fecha.date() else 0)
    for m in filas:
        _, mins = _mins(m)
        if any(abs(mins - c) <= 2 for c in citadas) and m.remitente_nombre:
            cuadrilla.add(m.remitente_nombre.strip().upper())
    msgs = []
    for m in filas:
        loc, mins = _mins(m)
        en_ventana = any(i <= mins <= f for i, f in ventanas)
        citado = any(abs(mins - c) <= 2 for c in citadas)
        quien = (m.remitente_nombre or "").strip().upper()
        if not (citado or (en_ventana and (not cuadrilla or quien in cuadrilla))):
            continue
        msgs.append({"hora": loc.strftime("%H:%M"), "remitente": m.remitente_nombre or m.remitente_jid or "—",
                     "texto": ((m.texto or "").strip() or f"({m.tipo} adjunto)")[:1200],
                     "destacado": citado, "grupo": nombres.get(m.chat_jid) or m.chat_nombre})
    grupos_txt = " · ".join(sorted({x["grupo"] or "" for x in msgs if x.get("grupo")})) or "grupo"
    horas_cit = [f"{c // 60:02d}:{c % 60:02d}" for c in sorted(citadas)]
    return msgs, {"fecha": fecha, "grupos_txt": grupos_txt, "tramos_txt": "; ".join(tramos_txt) or "—",
                  "citadas": horas_cit}


def _png_captura(reg: AnsRegistro, msgs: list, info: dict) -> bytes:
    from app.services import captura_chat as _cap
    ahora = datetime.now() - timedelta(hours=5)
    return _cap.captura_chat_png(f"Captura de chat · {reg.protocolo}",
                                 f"{info['grupos_txt']} · {info['fecha']:%d/%m/%Y} · mensajes del tramo {info['tramos_txt']}",
                                 msgs, pie=f"Generada por SGO7 el {ahora:%d/%m/%Y %H:%M} a partir del archivo del grupo de "
                                           "WhatsApp. Evidencia parcial: no sustituye el correo del Centro de Control.")


async def _captura_existente(db: AsyncSession, reg_id: int, aid: int | None = None):
    """La captura ya adjunta a la celda 3.2 A (de este análisis si se indica; si no, cualquiera)."""
    r = await db.execute(select(AnsEvidencia).where(AnsEvidencia.reg_id == reg_id, AnsEvidencia.campo == "excluible")
                         .order_by(AnsEvidencia.id))
    for e in r.scalars().all():
        n = e.nota or ""
        if n.startswith("Captura del chat generada") and (aid is None or f"(análisis #{aid})" in n):
            return e
    return None


async def _info_captura(db: AsyncSession, a: WaAnalisis, reg: AnsRegistro, res: dict, rt: dict) -> dict:
    """Lo que el bloque pinta en la línea del tramo: cuántos mensajes irían en la captura, desde–hasta,
    las horas citadas y si ya está adjunta (evidencia_id)."""
    if rt.get("solidez") not in ("no_solida", "parcial") and not rt.get("necesita_captura"):   # CAPTURA CUANDO EL TIEMPO SALE DE LA PLANTILLA (14 oct 2026)
        return {"mensajes": 0}
    msgs, info = await _mensajes_captura(db, reg, res, rt)
    e = await _captura_existente(db, reg.id)
    # «citadas» = las horas de los MENSAJES que entran destacados en la captura (los que la evidencia cita),
    # no todas las horas que la evidencia menciona (09:16 y 12:11 son horas dichas dentro de la plantilla).
    citadas = sorted({m["hora"] for m in msgs if m.get("destacado")}) or (info.get("citadas") or [])
    return {"mensajes": len(msgs), "desde": msgs[0]["hora"] if msgs else None, "hasta": msgs[-1]["hora"] if msgs else None,
            "citadas": citadas, "evidencia_id": e.id if e else None,
            "nombre": e.nombre if e else None}


async def _adjuntar_captura(db: AsyncSession, a: WaAnalisis, reg: AnsRegistro, res: dict, rt: dict,
                            forzar: bool = False):
    """Genera el PNG y lo deja como evidencia de la celda (en el paquete). Una por análisis: si ya
    existe, la devuelve sin repetir. Con `forzar` la hace aunque la solidez no lo pida."""
    if not forzar and rt.get("solidez") != "no_solida" and not rt.get("necesita_captura"):
        return None
    ya = await _captura_existente(db, reg.id, a.id)
    if ya and not forzar:
        return ya
    msgs, info = await _mensajes_captura(db, reg, res, rt)
    if not msgs:
        return None
    from app.services import captura_chat as _cap
    png = _png_captura(reg, msgs, info)
    carpeta = os.path.join(EVID_DIR, current_entorno(), str(reg.id), "excluible")
    os.makedirs(carpeta, exist_ok=True)
    destino = os.path.join(carpeta, f"{int(datetime.now().timestamp())}_captura_chat.png")
    with open(destino, "wb") as fh:
        fh.write(png)
    e = AnsEvidencia(reg_id=reg.id, campo="excluible", nombre=_cap.nombre_captura(info["grupos_txt"], info["fecha"].strftime("%Y-%m-%d")),
                     ruta=destino, mime="image/png", tamano=len(png),
                     nota=f"Captura del chat generada por el sistema a partir de {len(msgs)} mensaje(s) del grupo "
                          f"(análisis #{a.id}). Evidencia parcial: no sustituye el correo.",
                     fecha=info["fecha"].strftime("%Y-%m-%d"), hora=msgs[0]["hora"], hora_fin=msgs[-1]["hora"],
                     que_muestra="Captura del chat del grupo", en_paquete=True)
    db.add(e)
    await db.flush()
    return e


async def _adjuntar_captura_auto(db: AsyncSession, a: WaAnalisis, reg: AnsRegistro, res: dict) -> None:
    """Al aplicar/decidir un 3.2 A sostenido solo en mensajes: la captura se adjunta sola. Nunca rompe el aplicado."""
    try:
        ia = res.get("ia") or {}
        if (res.get("tipo") or "3.2A") != "3.2A" or ia.get("error"):
            return
        rt = wa_analista.resumen_tiempos(reg, ia, res, docs=await _docs_de(db, reg.id),
                                         evidencias=await _videos_celda(db, reg.id))
        e = await _adjuntar_captura(db, a, reg, res, rt)
        if e is not None:
            await db.commit()
    except Exception as ex:  # noqa: BLE001
        import logging
        logging.getLogger("wa_auditoria").warning("captura automática del análisis %s: %s", a.id, ex)


# ── OBSERVACIÓN FINAL ÚNICA (14 oct 2026) ──
async def _observacion_final(db: AsyncSession, a: WaAnalisis, reg: AnsRegistro, res: dict, emb: dict) -> str | None:
    """OBSERVACIÓN COMPUESTA POR CÓDIGO (14 oct 2026): con el embudo en «decidir», la observación final la
    arma el código con los hechos cuadrados (encadenamiento, videos de la espera con su etiqueta, cuenta hecha); el
    modelo solo pone la etiqueta corta de los videos y resume el soporte del tramo. Una vez por lectura: queda en el
    hilo (razon_sug + meta.observacion_final del último turno «ia»)."""
    from app.api.ans import _ultima_lectura
    c, turnos = await _hilo_y_turnos(db, reg.id, "excluible")
    if not c:
        return None
    dec = _ultima_lectura(turnos) or {}
    if dec.get("observacion_final"):
        return dec["observacion_final"]
    ia = res.get("ia") or {}
    # 1. etiqueta corta de los videos de la celda que no la tengan (modelo rápido)
    r = await db.execute(select(AnsEvidencia).where(AnsEvidencia.reg_id == reg.id, AnsEvidencia.campo == "excluible"))
    sin = [e for e in r.scalars().all()
           if e.en_paquete and _paq.tipo_de(e) == "Video" and not (e.que_muestra or "").strip()]
    if sin:
        try:
            if await _paq.etiquetar(db, sin, reg, modelo=claude._MODELO_RAPIDO):
                await db.commit()
        except Exception as ex:  # noqa: BLE001
            import logging
            logging.getLogger("wa_auditoria").warning("etiquetas para la observación final (%s): %s", a.id, ex)
    rt = wa_analista.resumen_tiempos(reg, ia, res, docs=await _docs_de(db, reg.id),
                                     evidencias=await _videos_celda(db, reg.id))
    cap = await _info_captura(db, a, reg, res, rt)
    # 2. el soporte de cada tramo, en corto (modelo rápido; si falla, por palabras clave)
    soportes = {}
    for g in rt.get("gaps") or []:
        for t in g.get("tramos") or []:
            # OBSERVACIÓN SIMPLE EN CUATRO FRASES (14 oct 2026): el soporte solo por su nombre, sin modelo
            soportes[(g["gap"], t.get("desde"))] = wa_analista.soporte_fallback(t.get("evidencia") or "")
    causa = dec.get("razon") or ia.get("causa") or (ia.get("categoria") or "").replace("_", " ")
    texto = wa_analista.componer_observacion(reg, rt, causa, soportes, docs=rt.get("docs"), captura=cap)
    if not texto:
        return None
    c.razon_sug = texto
    for m in reversed(turnos):
        if m.direccion == "ia" and m.meta:
            try:
                meta = json.loads(m.meta) or {}
            except ValueError:
                break
            meta["observacion_final"] = texto
            m.meta = json.dumps(meta, ensure_ascii=False)
            break
    await db.commit()
    return texto


@router.get("/analisis/{aid}/captura-chat/imagen")   # sin .png: la regla de estáticos de nginx la atrapaba (14-10-2026)
async def captura_chat_png(aid: int, db: AsyncSession = Depends(get_db)):
    """La captura del chat pintada EN MEMORIA (sin guardar nada): es lo que abre el clic en «captura del
    chat» de la línea del tramo, antes de aplicar el caso."""
    a = await db.get(WaAnalisis, aid)
    if not a:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    reg = await db.get(AnsRegistro, a.reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="El registro del ANS ya no existe")
    res = json.loads(a.resultado or "{}")
    rt = wa_analista.resumen_tiempos(reg, res.get("ia") or {}, res)
    msgs, info = await _mensajes_captura(db, reg, res, rt)
    if not msgs:
        raise HTTPException(status_code=404, detail="No hay mensajes del grupo dentro de los tramos justificados")
    from fastapi.responses import Response
    return Response(content=_png_captura(reg, msgs, info), media_type="image/png")


@router.post("/analisis/{aid}/captura-chat")
async def captura_chat(aid: int, db: AsyncSession = Depends(get_db)):
    """Forzar la captura a mano (la automática va al aplicar/decidir). Una por análisis."""
    a = await db.get(WaAnalisis, aid)
    if not a:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    reg = await db.get(AnsRegistro, a.reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="El registro del ANS ya no existe")
    res = json.loads(a.resultado or "{}")
    if (res.get("tipo") or "3.2A") != "3.2A":
        raise HTTPException(status_code=400, detail="La captura del chat es para casos 3.2 A")
    rt = wa_analista.resumen_tiempos(reg, res.get("ia") or {}, res)
    e = await _adjuntar_captura(db, a, reg, res, rt, forzar=True)
    if e is None:
        raise HTTPException(status_code=404, detail="No hay mensajes del grupo dentro de los tramos justificados")
    await db.commit()
    await db.refresh(e)
    return {"ok": True, "evidencia_id": e.id, "nombre": e.nombre}


@router.post("/analisis/{aid}/aplicar")
async def aplicar(aid: int, datos: dict = Body(default={}), db: AsyncSession = Depends(get_db)):
    """Aprueba el análisis: escribe la observación (editada o la propuesta) en la celda del
    3.2 A y adjunta como evidencia los videos que la IA atribuyó al caso. El SI/NO de
    `excluible` NO se toca: lo marca una persona en el grid."""
    a = await db.get(WaAnalisis, aid)
    if not a:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    if a.estado == "aplicado":
        raise HTTPException(status_code=400, detail="Este análisis ya se aplicó")
    reg = await db.get(AnsRegistro, a.reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="El registro del ANS ya no existe")

    res = json.loads(a.resultado or "{}")
    ia = res.get("ia") or {}

    # ── Caso 3.2 B (anulada): aplicar = ENCOLAR el mensaje al profesional de apoyo ──
    # El bot lo envía en su próximo ciclo; queda el hilo de consulta abierto (el mismo
    # del chat guiado) y la bitácora «A LA ESPERA DE…» en la observación del 3.2 B.
    if res.get("tipo") == "3.2B":
        from app.api.ans import _escribir_nota, _nota_espera
        if not (reg.zona or "").strip():
            raise HTTPException(status_code=409, detail=(
                "BLOQUEADO: la orden no tiene zona, así que no se sabe qué profesional de apoyo le "
                "corresponde. Primero el Paso 1 (preguntar a programación) o corrige la zona en el grid."))
        c0 = res.get("consulta") or {}
        persona = (datos.get("persona") or c0.get("persona") or "").strip()
        telefono = re.sub(r"\D", "", datos.get("telefono") or c0.get("telefono") or "")
        # MENSAJE 3.2 B EN VIVO (20 oct 2026, ver CHANGELOG): sin edición, el texto se arma AHORA para la persona
        # que muestra el tab, nunca el guardado en el análisis: un análisis hecho sin zona guardaba un saludo sin
        # nombre y ese texto salía aunque el recuadro mostrara el mensaje actualizado.
        mensaje = (datos.get("mensaje") or "").strip()
        if not mensaje and persona:
            from app.api.ans import _mensaje_wa, _ya_escrito_hoy
            mensaje = _mensaje_wa(reg, "excluible_anulado", quien=persona, rol_dir="apoyo",
                                  saludar=not await _ya_escrito_hoy(db, persona))
        if not persona or not telefono or not mensaje:
            raise HTTPException(status_code=400,
                                detail="Faltan persona, teléfono o el mensaje a enviar")
        # BLOQUEADORES (incidente 2026-08-27): si el correo del caso YA está
        # en Correos ANS, pedirlo por WhatsApp es ilógico — y una pregunta repetida a
        # la misma persona tampoco sale. `forzar` solo del botón explícito del tab.
        if not datos.get("forzar"):
            motivo = await _bloqueo_envio(db, reg.id, "excluible_anulado",
                                          telefono, persona)
            if motivo:
                raise HTTPException(status_code=409, detail=f"BLOQUEADO: {motivo}")
        db.add(WaEnvio(destino=telefono, persona=persona, texto=mensaje,
                       reg_id=reg.id, campo="excluible_anulado"))
        # El hilo de consulta: el mismo que usa el chat guiado, para que la respuesta
        # (cuando llegue por el bot) siga el flujo de siempre (analizar → aplicar).
        r = await db.execute(select(AnsConsulta).where(
            AnsConsulta.reg_id == reg.id, AnsConsulta.campo == "excluible_anulado",
            AnsConsulta.estado != "descartada").order_by(AnsConsulta.id.desc()).limit(1))
        c = r.scalars().first()
        if not c:
            c = AnsConsulta(reg_id=reg.id, campo="excluible_anulado", estado="abierta",
                            persona_actual=persona)
            db.add(c)
            await db.flush()
        c.persona_actual = persona
        db.add(AnsConsultaMsg(consulta_id=c.id, direccion="enviado", persona=persona,
                              texto=mensaje[:4000],
                              meta=json.dumps({"rol_dir": "apoyo", "via": "bot"})))
        if not (c.nota_auto or "").strip():
            _escribir_nota(reg, c, "obs_anulado", _nota_espera(persona))
        a.estado = "aplicado"
        await db.commit()
        return {"ok": True, "encolado": True, "persona": persona, "telefono": telefono}

    # ── Caso 3.2 A: aplicar = observación + evidencias (+ decisión si la persona la da) ──
    obs = (datos.get("observacion") or ia.get("observacion") or "").strip()
    if not obs:
        raise HTTPException(status_code=400, detail="No hay observación para aplicar")

    # 1. La observación a la celda del 3.2 A
    reg.obs = obs

    # 1b. La DECISIÓN, si viene en el aprobado (evolución de la regla del 21-jul,
    #     requisito del 19-08: el clic de aprobación ES la decisión humana —
    #     el botón dice qué SI/NO se escribe, la máquina solo lo propuso).
    decision = datos.get("decision") if datos.get("decision") in ("SI", "NO") else None
    if decision:
        reg.excluible = decision
        # El hilo de consulta (si el Paso 2 lo abrió) queda resuelto con esta razón
        from app.api.ans import _hilo_de
        con = await _hilo_de(db, reg.id, "excluible")
        if con:
            con.excluible_sug = decision
            con.razon_sug = obs
            con.nota_auto = obs
            con.estado = "resuelta"

    # Qué videos justifican el desfase: los demás se adjuntan pero quedan fuera del paquete para ENEL
    wa_analista.resumen_tiempos(reg, ia, res)
    # 2. Los videos del caso (del_caso=SI) → AnsEvidencia (se COPIA el archivo: la
    #    evidencia del ANS vive su propia vida aunque el inbox del bot se depure)
    adjuntos = []
    carpeta = os.path.join(EVID_DIR, current_entorno(), str(reg.id), "excluible")
    os.makedirs(carpeta, exist_ok=True)
    for v in (ia.get("videos") or []):
        if v.get("del_caso") != "SI":
            continue
        m = await db.get(WaMensaje, v.get("msg_id"))
        if not m or not m.media_ruta or not os.path.exists(m.media_ruta):
            continue
        # Ya vinculado por un análisis anterior (re-análisis de auditoría): no duplicar
        # la evidencia — el archivo y su fila siguen siendo los del primer aplicado.
        if m.evidencia_id:
            continue
        seguro = re.sub(r"[^\w.\-]", "_", os.path.basename(m.media_ruta))[:120]
        destino = os.path.join(carpeta, f"{int(datetime.now().timestamp())}_{seguro}")
        shutil.copyfile(m.media_ruta, destino)
        e = AnsEvidencia(reg_id=reg.id, campo="excluible",
                         nombre=f"WhatsApp {m.chat_nombre or ''} {v.get('hora_overlay') or ''}".strip() + ".mp4",
                         ruta=destino, mime=m.media_mime,
                         tamano=m.media_tamano, transcripcion=next(
                             (x.get("transcripcion") for x in res.get("videos", [])
                              if x.get("msg_id") == m.id), None),
                         nota=f"Adjuntado por el analista automático (análisis #{a.id}); "
                              f"overlay: {v.get('overlay') or 's/d'}")
        _paq.horas_de_analisis(e, v, res)
        db.add(e)
        await db.flush()
        m.estado, m.evidencia_id = "vinculado", e.id
        adjuntos.append(e.id)

    await _adjuntar_captura_auto(db, a, reg, res)
    a.estado = "aplicado"
    await db.commit()
    await _etiquetar_nuevas(db, reg, adjuntos)
    return {"ok": True, "reg_id": reg.id, "obs": obs, "evidencias": adjuntos,
            "decision": decision}


# ── EL EMBUDO EN ACCIÓN: seguir la conversación, decidir, releer ─────────────────
# Las tres salidas que le faltaban al tab (requisito 2026-09-24): «respondió» dejaba el caso
# en un limbo — ni se podía contestar lo que preguntaban, ni decidir sin irse al grid.


@router.post("/analisis/{aid}/seguimiento")
async def seguimiento(aid: int, datos: dict = Body(...), db: AsyncSession = Depends(get_db)):
    """CONTINUAR la conversación: encola el siguiente mensaje (el borrador que propuso la
    máquina, tal como la persona lo haya editado) a quien ya venía respondiendo. Queda en el
    mismo hilo, así la próxima lectura ve la conversación completa.
    Body: {mensaje, persona?, telefono?, forzar?}. Nada sale sin este clic."""
    a = await db.get(WaAnalisis, aid)
    if not a:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    reg = await db.get(AnsRegistro, a.reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="El registro del ANS ya no existe")
    res = json.loads(a.resultado or "{}")
    campo = _campo_de(res)
    emb = await _embudo(db, reg, res, a)
    if not emb:
        raise HTTPException(status_code=400, detail="Este caso todavía no tiene conversación abierta")
    mensaje = (datos.get("mensaje") or emb.get("mensaje") or "").strip()
    persona = (datos.get("persona") or emb.get("persona") or "").strip()
    telefono = re.sub(r"\D", "", datos.get("telefono") or emb.get("telefono") or "")
    if not mensaje:
        raise HTTPException(status_code=400, detail="No hay mensaje que enviar")
    if not persona or len(telefono) < 10:
        raise HTTPException(status_code=400,
                            detail=f"No tengo el número de {persona or 'esa persona'} en el "
                                   f"directorio: agrégalo en Contactos y vuelve a intentarlo")
    r = await encolar_pregunta(db, reg, campo, persona, telefono, emb.get("rol_dir") or "otro",
                               mensaje, forzar=bool(datos.get("forzar")), origen="seguimiento",
                               nota=False)
    if not r.get("ok"):
        raise HTTPException(status_code=409, detail=f"BLOQUEADO: {r.get('bloqueo')}")
    return {"ok": True, "encolado": True, "persona": persona, "telefono": telefono,
            "campo": campo}


@router.post("/analisis/{aid}/decidir")
async def decidir(aid: int, datos: dict = Body(...), db: AsyncSession = Depends(get_db)):
    """LA DECISIÓN, con la conversación ya respondida. Escribe el SI/NO y la observación en
    la celda del caso (3.2 A o 3.2 B), cierra el hilo y deja la media del chat amarrada como
    evidencia. Funciona aunque el análisis ya esté «aplicado» — el primer aplicar del 3.2 B
    solo encolaba la pregunta, y antes de hoy no había manera de rematar el caso desde aquí.
    Body: {decision: 'SI'|'NO', observacion?}."""
    from app.api.ans import _eval_reglas_nodos, _log_reglas_nodos
    a = await db.get(WaAnalisis, aid)
    if not a:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    reg = await db.get(AnsRegistro, a.reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="El registro del ANS ya no existe")
    decision = datos.get("decision")
    if decision not in ("SI", "NO"):
        raise HTTPException(status_code=400, detail="La decisión debe ser SI o NO")
    res = json.loads(a.resultado or "{}")
    campo = _campo_de(res)
    emb = await _embudo(db, reg, res, a)
    obs = (datos.get("observacion") or (emb or {}).get("observacion_sugerida") or "").strip()
    if not obs:
        raise HTTPException(status_code=400, detail="Escribe la observación que va en la celda")

    razon_field = "obs_anulado" if campo == "excluible_anulado" else "obs"
    setattr(reg, campo, decision)
    setattr(reg, razon_field, obs)
    if campo == "excluible_anulado":
        # Igual que el chat guiado: al excluir la anulación, «Se ejecutó» va N/A; si no, NO.
        reg.se_ejecuto = "N/A" if decision == "SI" else "NO"
        _log_reglas_nodos(db, reg, _eval_reglas_nodos(reg))

    c, turnos = await _hilo_y_turnos(db, reg.id, campo)
    if c:
        c.excluible_sug, c.razon_sug, c.nota_auto = decision, obs, obs
        c.estado = "resuelta"

    # La media que llegó por el chat ya quedó como AnsEvidencia al procesarse la respuesta;
    # aquí solo se suman los videos que el análisis atribuyó al caso y que aún no estén.
    adjuntos = []
    carpeta = os.path.join(EVID_DIR, current_entorno(), str(reg.id), campo)
    if res.get("tipo") == "3.2A":
        wa_analista.resumen_tiempos(reg, res.get("ia") or {}, res)
    for v in ((res.get("ia") or {}).get("videos") or []):
        if v.get("del_caso") != "SI":
            continue
        m = await db.get(WaMensaje, v.get("msg_id"))
        if not m or not m.media_ruta or not os.path.exists(m.media_ruta) or m.evidencia_id:
            continue
        os.makedirs(carpeta, exist_ok=True)
        seguro = re.sub(r"[^\w.\-]", "_", os.path.basename(m.media_ruta))[:120]
        destino = os.path.join(carpeta, f"{int(datetime.now().timestamp())}_{seguro}")
        shutil.copyfile(m.media_ruta, destino)
        e = AnsEvidencia(reg_id=reg.id, campo=campo,
                         nombre=f"WhatsApp {m.chat_nombre or ''} {v.get('hora_overlay') or ''}".strip() + ".mp4",
                         ruta=destino, mime=m.media_mime, tamano=m.media_tamano,
                         transcripcion=next((x.get("transcripcion") for x in res.get("videos", [])
                                             if x.get("msg_id") == m.id), None),
                         nota=f"Adjuntado al decidir el caso (análisis #{a.id})")
        _paq.horas_de_analisis(e, v, res)
        db.add(e)
        await db.flush()
        m.estado, m.evidencia_id = "vinculado", e.id
        adjuntos.append(e.id)

    if res.get("tipo") == "3.2A":
        await _adjuntar_captura_auto(db, a, reg, res)
    a.estado = "aplicado"
    await db.commit()
    await _etiquetar_nuevas(db, reg, adjuntos)
    return {"ok": True, "reg_id": reg.id, "campo": campo, "decision": decision,
            "campo_razon": razon_field, "obs": obs, "evidencias": adjuntos}


@router.post("/analisis/{aid}/releer")
async def releer(aid: int, db: AsyncSession = Depends(get_db)):
    """RELEER la conversación completa con la máquina. Antes de clasificar, completa lo que
    nunca se leyó: transcribe los audios y videos y lee las fotos que entraron cuando el
    procesador aún no sabía mirarlas. Sirve para los hilos viejos y para cuando se
    quiere una segunda lectura tras añadir algo al caso."""
    from app.api.ans import _CONSULTA_CFG, _clasificar_hilo, _aplicar_lectura
    from app.services.wa_respuestas import leer_media
    a = await db.get(WaAnalisis, aid)
    if not a:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    reg = await db.get(AnsRegistro, a.reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="El registro del ANS ya no existe")
    res = json.loads(a.resultado or "{}")
    campo = _campo_de(res)
    c, turnos = await _hilo_y_turnos(db, reg.id, campo)
    if not c:
        raise HTTPException(status_code=400, detail="Este caso todavía no tiene conversación")

    # 1. Completar las lecturas que faltan (audio/video → transcripción, foto → visión)
    leidos = 0
    for m in turnos:
        if m.direccion != "recibido" or not m.meta:
            continue
        try:
            meta = json.loads(m.meta) or {}
        except ValueError:
            continue
        if meta.get("lectura") or not (meta.get("msg_id") or meta.get("wa_id")):
            continue
        if meta.get("msg_id"):
            wm = await db.get(WaMensaje, meta["msg_id"])
        else:
            rw = await db.execute(select(WaMensaje).where(WaMensaje.wa_id == meta["wa_id"]))
            wm = rw.scalars().first()
        if not wm or not wm.media_ruta:
            continue
        etiqueta, contenido = await leer_media(wm)
        if not contenido:
            continue
        m.texto = ((m.texto or "") + f"\n[{etiqueta}] {contenido}")[:4000]
        meta.update({"lectura": contenido, "msg_id": wm.id, "tipo": wm.tipo,
                     "mime": wm.media_mime})
        m.meta = json.dumps(meta, ensure_ascii=False)
        if wm.evidencia_id:
            ev = await db.get(AnsEvidencia, wm.evidencia_id)
            if ev and not ev.transcripcion:
                ev.transcripcion = contenido
            # ADJUNTOS DEL CHAT, UBICADOS Y CONTESTADOS (14 oct 2026)
            if ev and wm.tipo == "video":
                from app.services.wa_respuestas import ubicar_video
                vered = await ubicar_video(db, reg, ev, wm.media_ruta)
                if vered:
                    m.texto = (m.texto + f" [el sistema: {vered}]")[:4000]
        leidos += 1
    if leidos:
        await db.flush()

    # 2. Reclasificar el hilo completo con lo que ahora sí se puede leer
    _dec_campo, razon_field, label, objetivo = _CONSULTA_CFG.get(
        campo, (campo, "obs", "3.2 A", ""))
    dec, _msgs = await _clasificar_hilo(db, reg, c, label, objetivo)
    db.add(AnsConsultaMsg(consulta_id=c.id, direccion="ia", persona=None,
                          texto=dec.get("resumen") or dec.get("error"),
                          meta=json.dumps(dec, ensure_ascii=False)))
    _aplicar_lectura(reg, c, dec, razon_field, c.persona_actual)
    await db.commit()
    return {"ok": True, "adjuntos_leidos": leidos, "calidad": dec.get("calidad"),
            "accion": dec.get("accion"), "error": dec.get("error")}


async def _sin_clasificar(db: AsyncSession, periodo: str) -> list[dict]:
    r = await db.execute(select(WaAnalisis).join(AnsRegistro, AnsRegistro.id == WaAnalisis.reg_id)
                         .where(AnsRegistro.periodo == periodo,
                                AnsRegistro.entorno == current_entorno(),
                                WaAnalisis.estado != "descartado")
                         .order_by(WaAnalisis.id.desc()))
    vistos, out = set(), []
    for a in r.scalars().all():
        if a.reg_id in vistos:
            continue
        vistos.add(a.reg_id)
        res = json.loads(a.resultado or "{}")
        f = await _flujo(db, a, res)
        if f.get("flujo") == "respondido" and not f.get("calidad"):
            reg = await db.get(AnsRegistro, a.reg_id)
            out.append({"id": a.id, "pdl": getattr(reg, "pdl", None)})
    return out


async def _releer_lote(periodo: str) -> None:
    """El barrido en segundo plano: cada caso con su propia sesión, para que uno que falle
    no se lleve a los demás."""
    from app.db.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        casos = await _sin_clasificar(db, periodo)
    for c in casos:
        try:
            async with AsyncSessionLocal() as db:
                await releer(c["id"], db)
        except Exception as ex:                                       # noqa: BLE001
            import logging
            logging.getLogger("wa_auditoria").warning(
                "relectura de %s falló: %s", c["pdl"], ex)


@router.post("/releer-pendientes")
async def releer_pendientes(background: BackgroundTasks, datos: dict = Body(default={}),
                            db: AsyncSession = Depends(get_db)):
    """Relee TODAS las respuestas del periodo que quedaron sin clasificar: transcribe los
    adjuntos que nunca se leyeron y las pasa por el embudo. Va en segundo plano (1-2 min por
    caso); la lista del tab se refresca sola. Body: {periodo}."""
    periodo = (datos.get("periodo") or "").strip()
    if not re.match(r"^\d{4}-\d{2}$", periodo):
        raise HTTPException(status_code=400, detail="Falta el periodo (YYYY-MM)")
    casos = await _sin_clasificar(db, periodo)
    if casos:
        background.add_task(_releer_lote, periodo)
    return {"en_cola": len(casos), "casos": [c["pdl"] for c in casos]}


@router.post("/analisis/{aid}/descartar")
async def descartar(aid: int, db: AsyncSession = Depends(get_db)):
    a = await db.get(WaAnalisis, aid)
    if not a:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    a.estado = "descartado"
    await db.commit()
    return {"ok": True}
