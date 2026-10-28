"""El cierre del circuito 3.2 B: procesar SOLO las respuestas que llegan por WhatsApp.

Regla (2026-08-13): esto NO requiere validación humana — la máquina registra la
respuesta en el hilo, reescribe la bitácora de la observación con lo que vayan enviando y
adjunta la media como evidencia. Lo importante es que TODO quede registrado en el sistema.
La única decisión que sigue siendo humana es aplicar la exclusión (consulta/aplicar).

Cómo encaja: es EXACTAMENTE el flujo del endpoint `consulta/analizar` del chat guiado
(mismos helpers de ans.py, misma IA, misma bitácora), solo que disparado por el bot en vez
de por una persona pegando el texto a mano:

  DM entrante (de_mi=false, estado='nuevo')
    → ¿el teléfono es de un contacto del directorio?
    → ¿ese contacto tiene un hilo de consulta ABIERTO? (el más reciente si hay varios)
    → turno 'recibido' en el hilo (+ media copiada como AnsEvidencia si adjuntó algo)
    → la IA clasifica TODA la conversación y reescribe la bitácora en la celda
    → el mensaje queda 'vinculado' (sale de la bandeja de nuevos)

Los DM de números sin contacto o sin hilo abierto se quedan en 'nuevo': no se pierden,
solo esperan a que exista contexto.

LO QUE SE ESCRIBE A MANO (2026-10-01): si la persona consultada pide un correo para enviar el
soporte y se le da desde WhatsApp Web, ese mensaje queda en `wa_mensajes` (de_mi=true) pero el
hilo solo tenía lo del bot y lo de la otra persona: en «Ver justificación» parecía que nadie le
había contestado. Ahora un DM propio que NO es un envío
del bot entra al hilo como turno 'enviado' (via «manual») con su hora real de WhatsApp, y la IA
relee la conversación completa (su pregunta ya respondida deja de figurar como pendiente).
"""
import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ans_ctx import current_entorno
from app.models.models import (WaMensaje, WaEnvio, AnsConsulta, AnsConsultaMsg, AnsRegistro,
                               AnsEvidencia, Contacto)
from app.services import letrero
from app.services import evid_paquete as _paq
from app.services.transcribe import transcribir_video

log = logging.getLogger("wa_respuestas")

EVID_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                        "data", "ans_evidencias"))


def _telefono_de(m: WaMensaje) -> str:
    """El teléfono del OTRO en un chat directo. En los mensajes propios (de_mi) el bot también
    guarda ahí el número de la contraparte (senderPn / lids.json), no el nuestro."""
    return re.sub(r"\D", "", (m.remitente_jid or m.chat_jid or "").split("@")[0])


def _norm_txt(s: str | None) -> str:
    """Texto comparable: sin espacios repetidos ni mayúsculas (WhatsApp no altera el cuerpo)."""
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


# ── LEER LO QUE MANDAN, no solo guardarlo (2026-09-24) ───────────────────────────
# Hasta hoy solo la nota de voz se transcribía: un VIDEO de respuesta y una FOTO (el
# pantallazo del correo del Centro Operativo, la foto del frente bajo lluvia) entraban al
# hilo como «[adjuntó imagen]» y la IA concluía sobre algo que nunca había mirado. Ahora
# todo adjunto se convierte a texto ANTES de clasificar, y ese texto queda guardado en el
# turno y en la evidencia — auditable, y sin volver a gastar IA para releerlo.
_ETIQUETA = {"audio": "nota de voz transcrita", "video": "audio del video transcrito",
             "imagen": "imagen leída", "documento": "documento adjunto"}


def _data_url(ruta: str, mime: str | None) -> str | None:
    """La imagen como data-URL para la visión (solo formatos que los dos proveedores leen)."""
    import base64
    m = (mime or "").split(";")[0].strip().lower()
    if m == "image/jpg":
        m = "image/jpeg"
    if m not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        return None
    try:
        with open(ruta, "rb") as fh:
            return f"data:{m};base64," + base64.b64encode(fh.read()).decode()
    except OSError:
        return None


# ── ADJUNTOS DEL CHAT, UBICADOS Y CONTESTADOS (14 oct 2026) ──
# El reloj del letrero del último video leído, por ruta: leer_media lo calcula y quien crea la
# evidencia (abajo, y releer en wa_auditoria) lo recoge para fecha/hora y para ubicarlo en el desfase.
_RELOJ_CACHE: dict = {}


async def _res_del_caso(db: AsyncSession, reg_id: int) -> dict:
    """El último análisis del caso (para los tramos justificados), o {}."""
    from app.models.models import WaAnalisis
    r = await db.execute(select(WaAnalisis).where(WaAnalisis.reg_id == reg_id, WaAnalisis.estado != "descartado")
                         .order_by(WaAnalisis.id.desc()).limit(1))
    a = r.scalars().first()
    try:
        return json.loads(a.resultado or "{}") if a else {}
    except ValueError:
        return {}


async def ubicar_video(db: AsyncSession, reg, e, ruta: str) -> str | None:
    """Fecha/hora de la evidencia desde el reloj leído y su veredicto frente al desfase; un video
    fuera del desfase queda fuera del paquete. Devuelve el texto del veredicto (o None)."""
    from app.services import wa_analista
    r_ = _RELOJ_CACHE.pop(ruta, None)
    if not r_:
        return None
    e.fecha, e.hora, e.hora_fin = r_["fecha"], r_["inicio"][:5], r_["fin"][:5]
    v_ = wa_analista.video_vs_desfase(reg, await _res_del_caso(db, reg.id), e.hora, e.hora_fin)
    if v_["veredicto"] == "fuera":
        e.en_paquete = False
        e.que_muestra = e.que_muestra or "Fuera del lapso del desfase"
    return v_["texto"]


async def leer_media(m: WaMensaje) -> tuple[str | None, str | None]:
    """El contenido del adjunto como TEXTO. Devuelve (etiqueta, contenido) o (None, None).
    Nunca lanza: si la transcripción o la visión fallan, el flujo sigue con lo que haya."""
    if not m.media_ruta or not os.path.exists(m.media_ruta):
        return None, None
    tipo = (m.tipo or "").lower()
    try:
        if tipo in ("audio", "video"):
            txt = await asyncio.to_thread(transcribir_video, m.media_ruta)
            if tipo == "video":
                # la hora del letrero va delante: sin ella el clasificador no puede ubicar el video en el desfase
                from app.services.wa_analista import reloj_video_directo
                reloj = await reloj_video_directo(m.media_ruta)
                if reloj:
                    _RELOJ_CACHE[m.media_ruta] = reloj
                    y, mo, d = reloj["fecha"].split("-")
                    txt = f"[grabado {reloj['inicio'][:5]}–{reloj['fin'][:5]} del {d}/{mo} según el letrero] " + (txt or "(sin audio entendible)")
                else:
                    txt = "[sin hora legible en el letrero] " + (txt or "(sin audio entendible)")
            return (_ETIQUETA[tipo], txt) if txt else (None, None)
        if tipo == "imagen":
            du = _data_url(m.media_ruta, m.media_mime)
            if not du:
                return None, None
            from app.services.claude import leer_imagen
            txt = await leer_imagen(du)
            return (_ETIQUETA["imagen"], txt) if txt else (None, None)
    except Exception as ex:                                               # noqa: BLE001
        log.warning("lectura del adjunto de %s (%s) falló: %s", m.wa_id, tipo, ex)
    return None, None


async def _avisar_embudo(reg, consulta, persona: str, dec: dict) -> None:
    """Un aviso al administrador (guion configurado en SGO7_NOTIFY_CMD) cuando la respuesta CAMBIA algo:
    cierra el caso en NO, ya permite decidir, o deja una pregunta de ellos sin responder.
    Una sola línea, sin datos de cliente. Sin guion configurado (PC local), no hace nada."""
    from app.api.ans import CALIDAD_EMBUDO
    paso, etiqueta, _ayuda = CALIDAD_EMBUDO.get(dec.get("calidad") or "", (None, "", ""))
    if paso not in ("cerrado_no", "decidir", "responder", "decidir_ing"):
        return
    try:
        import subprocess
        notify = os.getenv("SGO7_NOTIFY_CMD", "")
        if not notify or not os.path.exists(notify):
            return
        que = {"cerrado_no": "la respuesta es un NO: el caso se cierra",
               "decidir": "ya hay con que decidir",
               "responder": "te dejaron una pregunta sin responder",
               "decidir_ing": "se negaron a responder: decides tu"}[paso]
        subprocess.run([notify,
                        f"SGO7 ANS: {persona} respondio la orden {reg.pdl} ({etiqueta}) - "
                        f"{que}. Revisa el tab Auditoria IA."], timeout=60, check=False)
    except Exception as ex:                                               # noqa: BLE001
        log.warning("aviso del embudo falló: %s", ex)


async def _hora_ultimo_turno(db: AsyncSession, consulta_id: int, excluir_msg: int | None = None):
    """La hora REAL (WhatsApp, UTC naive) del turno más reciente del hilo — enviado o recibido.
    Los turnos con msg_id/wa_id usan la hora del mensaje; los demás, cuándo se registraron."""
    r = await db.execute(select(AnsConsultaMsg).where(AnsConsultaMsg.consulta_id == consulta_id,
                                                      AnsConsultaMsg.direccion.in_(("enviado", "recibido"))))
    ultimo = None
    for t in r.scalars().all():
        try:
            meta = json.loads(t.meta) if t.meta else {}
        except ValueError:
            meta = {}
        if excluir_msg and meta.get("msg_id") == excluir_msg:
            continue
        cuando = t.creado_en
        wm = None
        if meta.get("msg_id"):
            wm = await db.get(WaMensaje, meta["msg_id"])
        elif meta.get("wa_id"):
            wm = (await db.execute(select(WaMensaje).where(WaMensaje.wa_id == meta["wa_id"]))).scalars().first()
        if wm and wm.enviado_en:
            cuando = wm.enviado_en
        if cuando and (ultimo is None or cuando > ultimo):
            ultimo = cuando
    return ultimo


async def procesar_pendientes(db: AsyncSession, simular: bool = False) -> list[dict]:
    """Procesa todos los DM nuevos que se puedan amarrar a un hilo abierto. Devuelve el
    resumen de lo hecho (va al log del bot). `simular`: solo dice qué haría con cada DM —
    sin IA, sin escribir nada (el llamador hace rollback)."""
    # Imports tardíos: son los helpers privados del chat guiado (ans.py) — única fuente
    # del formato de conversación, la IA y la bitácora; duplicarlos aquí los desincroniza.
    from app.api.ans import (_norm_nombre, _CONSULTA_CFG, _clasificar_hilo,
                             _aplicar_lectura)

    # Blindaje anti-avalancha (lección del 2026-08-13: un UPDATE torpe etiquetó 656
    # mensajes históricos como de un contacto y este barrido los procesó TODOS con IA):
    #  - solo respuestas RECIENTES (una respuesta a una consulta llega en días, no meses);
    #  - tope de PROCESADOS por barrido — si algo inunda la bandeja, se va por tandas.
    #
    # FALLO 2026-09-10: el tope se aplicaba en el SELECT (LIMIT 20 por fecha ascendente), y
    # 22 DMs viejos de un número SIN contacto (una persona que no está en el
    # directorio) llenaban el cupo en cada barrido: las respuestas reales de los ingenieros
    # de ese día nunca se alcanzaban («barrido lleno» en el log una y otra vez). Ahora el
    # cupo solo lo consumen los DMs que de verdad se procesan; los de números desconocidos
    # o sin hilo abierto se saltan sin costo.
    # UN SOLO BARRIDO A LA VEZ (19 oct 2026, ver CHANGELOG): el bot llama a /procesar-respuestas ~90 s después de cada DM y
    # en su ciclo de 3 h. Dos barridos a la vez leían el mismo DM «nuevo» y lo registraban dos veces, con dos
    # lecturas de la IA (una respuesta del 16 oct quedó duplicada). El segundo espera aquí a que el
    # primero haga commit y ya lo ve «vinculado». Se suelta solo al cerrar la transacción. Va por la conexión
    # (Core) para que el filtro de entorno de los modelos ANS no lo toque.
    await (await db.connection()).execute(sa_text("select pg_advisory_xact_lock(hashtext('sgo7-procesar-respuestas-wa'))"))
    TOPE = 20
    # Entran también los PROPIOS (de_mi): lo que el operador escribe a mano es la mitad de la
    # conversación. Los envíos del bot se reconocen abajo y se saltan (ya son turno 'enviado').
    r = await db.execute(select(WaMensaje)
                         .where(WaMensaje.es_grupo.is_(False),
                                WaMensaje.estado == "nuevo",
                                WaMensaje.enviado_en >= datetime.now() - timedelta(days=7))
                         .order_by(WaMensaje.enviado_en)
                         .limit(500))
    nuevos = r.scalars().all()
    if not nuevos:
        return []

    # Lo que mandó el BOT, para no confundirlo con lo escrito a mano: por wa_id y, por si el eco
    # llega antes de que el bot reporte su wa_id, por (teléfono, texto) de la cola de salida.
    re_env = await db.execute(select(WaEnvio).where(WaEnvio.creado_en >= datetime.now() - timedelta(days=30)))
    envios_rec = re_env.scalars().all()
    ids_bot = {e.wa_id for e in envios_rec if e.wa_id}
    textos_bot = {(re.sub(r"\D", "", e.destino or ""), _norm_txt(e.texto)) for e in envios_rec}
    rnum_hilo: dict[int, str | None] = {}        # hilo → R##### de su orden (cache)

    r = await db.execute(select(AnsConsulta).where(AnsConsulta.estado == "abierta")
                         .order_by(AnsConsulta.actualizado_en.desc()))
    abiertas = list(r.scalars().all())
    # PASO 1 CON RESPUESTA (19 oct 2026, ver CHANGELOG): la consulta a programación (zona/supervisor) y la del
    # número del supervisor pasan a «respondida» con el PRIMER mensaje, pero la gente contesta por partes: una segunda
    # respuesta de la programadora llegó una hora después de la primera y, sin su hilo entre los abiertos, se
    # fue al Paso 1 de otra orden. Esos hilos siguen escuchando 7 días; un Paso 1 ya resuelto (la orden ya
    # tiene zona y supervisor con nombre) deja de escuchar, esté abierto o no.
    from app.api.ans import _responsable_es_telefono
    rr = await db.execute(select(AnsConsulta).where(
        AnsConsulta.estado == "respondida",
        AnsConsulta.campo.in_(("zona_responsable", "numero_supervisor")),
        AnsConsulta.actualizado_en >= datetime.now() - timedelta(days=7))
        .order_by(AnsConsulta.actualizado_en.desc()))
    abiertas += list(rr.scalars().all())
    vivas = []
    for c in abiertas:
        if c.campo == "zona_responsable":
            rg = await db.get(AnsRegistro, c.reg_id)
            if rg and (rg.zona or "").strip() and not _responsable_es_telefono(rg):
                continue
        vivas.append(c)
    abiertas = vivas
    abiertas_por_caso = {}
    for c in abiertas:                       # una abierta manda sobre una respondida del mismo caso
        k = (c.reg_id, c.campo)
        if k not in abiertas_por_caso or (abiertas_por_caso[k].estado != "abierta" and c.estado == "abierta"):
            abiertas_por_caso[k] = c

    # Directorio por teléfono: un mismo número puede tener VARIOS contactos (la misma persona como
    # supervisor y como líder con dos grafías del nombre; un nombre corto y el nombre completo…).
    # Con `.first()` el pareo por nombre contra el turno 'enviado' fallaba según cuál
    # saliera — se guardan todos y se busca el que calce con el hilo.
    rc = await db.execute(select(Contacto).where(Contacto.activo.is_(True)))
    por_tel: dict[str, list[Contacto]] = {}
    for c in rc.scalars().all():
        t = re.sub(r"\D", "", c.telefono or "")
        if t:
            por_tel.setdefault(t, []).append(c)

    hechos = []
    for m in nuevos:
        if len(hechos) >= TOPE:
            log.warning("barrido lleno (%s procesados): quedan DMs por procesar — "
                        "siguiente barrido", TOPE)
            break
        tel = _telefono_de(m)
        propio = bool(m.de_mi)
        if propio and (m.wa_id in ids_bot or (tel, _norm_txt(m.texto)) in textos_bot):
            continue                     # eco de un envío del bot: ya está en su hilo
        contactos = por_tel.get(tel) or []
        if not contactos:
            continue                     # número desconocido: se queda en la bandeja
        nombres = {_norm_nombre(c.nombre) for c in contactos}
        contacto = contactos[0]
        consulta, metodo = None, None

        def _contacto_para(hilo: AnsConsulta | None, turnos=None) -> Contacto:
            """El contacto cuyo nombre aparece en el hilo (para firmar el turno igual
            que el 'enviado'); si ninguno calza, el primero del directorio."""
            for c in contactos:
                nc = _norm_nombre(c.nombre)
                if hilo and _norm_nombre(hilo.persona_actual or "") == nc:
                    return c
                if turnos and any(_norm_nombre(t.persona or "") == nc for t in turnos):
                    return c
            return contactos[0]

        # 1º — CITA EXACTA: si respondió deslizando sobre un mensaje nuestro, el envío
        #      citado dice EXACTAMENTE de qué caso habla. Cero ambigüedad.
        if m.quote_wa_id:
            rq = await db.execute(select(WaEnvio).where(WaEnvio.wa_id == m.quote_wa_id))
            env = rq.scalars().first()
            if env and env.reg_id:
                consulta = abiertas_por_caso.get((env.reg_id, env.campo))
                metodo = "cita"
            if not consulta:
                # …o citando un mensaje SUYO que ya es turno de un hilo (lo normal cuando el operador
                # contesta a mano deslizando sobre la pregunta del ingeniero)
                rq = await db.execute(select(AnsConsultaMsg).where(
                    AnsConsultaMsg.direccion == "recibido",
                    AnsConsultaMsg.meta.contains(f'"wa_id": "{m.quote_wa_id}"')))
                t = rq.scalars().first()
                hilo = next((c for c in abiertas if t and c.id == t.consulta_id), None)
                if hilo:
                    consulta, metodo = hilo, "cita"

        # 2º — HEURÍSTICA POR TIEMPO: de los hilos abiertos donde se le escribió a esta
        #      persona, el del último mensaje enviado ANTES de su respuesta (uno responde
        #      a lo último que le llegó). Con varios casos en paralelo esto acierta el
        #      natural; la cita (1º) cubre cuando la persona es explícita.
        #      Fuente principal: los ENVÍOS del bot a este TELÉFONO (wa_envios.destino) —
        #      no dependen de cómo esté escrito el nombre en el directorio. El pareo por
        #      nombre contra los turnos 'enviado' queda de respaldo (consultas hechas a
        #      mano desde el chat guiado, sin envío del bot).
        if not consulta:
            candidatos = []              # (fecha del envío a la persona, hilo)
            re_ = await db.execute(select(WaEnvio).where(WaEnvio.estado == "enviado"))
            for e in re_.scalars().all():
                if re.sub(r"\D", "", e.destino or "") != tel:
                    continue
                c = abiertas_por_caso.get((e.reg_id, e.campo))
                if c:
                    candidatos.append((e.enviado_en or e.creado_en, c))
            if not candidatos:
                for c in abiertas:
                    rm = await db.execute(select(AnsConsultaMsg).where(
                        AnsConsultaMsg.consulta_id == c.id,
                        AnsConsultaMsg.direccion == "enviado")
                        .order_by(AnsConsultaMsg.id.desc()))
                    t = next((t for t in rm.scalars().all()
                              if _norm_nombre(t.persona or "") in nombres), None)
                    if t:
                        candidatos.append((t.creado_en, c))
            # 2b — LA ORDEN NOMBRADA MANDA sobre la hora (2026-10-01): con varios hilos abiertos
            #      con la misma persona (Carlos Ramírez tenía 3), «sobre la R10042…» va al hilo
            #      de la R10042 aunque el último envío haya sido de otro caso.
            cods = set(letrero.codigos_r(m.texto or ""))
            if candidatos and cods:
                for _h, c in candidatos:
                    if c.id not in rnum_hilo:
                        rg = await db.get(AnsRegistro, c.reg_id)
                        rnum_hilo[c.id] = letrero.rnum(rg.pdl) if rg else None
                nombrados = [x for x in candidatos if rnum_hilo.get(x[1].id) in cods]
                if nombrados:
                    consulta, metodo = max(nombrados, key=lambda x: x[0])[1], "orden"
            if candidatos and not consulta:
                ref = m.enviado_en or datetime.now()
                antes = [x for x in candidatos if x[0] <= ref]
                consulta = (max(antes, key=lambda x: x[0])[1] if antes
                            else min(candidatos, key=lambda x: x[0])[1])
                metodo = "tiempo"
        if not consulta:
            continue                     # sin hilo abierto: se queda en la bandeja
        reg = await db.get(AnsRegistro, consulta.reg_id)
        if not reg:
            continue
        rt = await db.execute(select(AnsConsultaMsg).where(
            AnsConsultaMsg.consulta_id == consulta.id,
            AnsConsultaMsg.direccion == "enviado"))
        enviados = rt.scalars().all()
        contacto = _contacto_para(consulta, enviados)
        decision, razon_field, label, objetivo = _CONSULTA_CFG.get(
            consulta.campo, (consulta.campo, "obs", "", ""))

        if propio:
            texto = (m.texto or "").strip()
            # Ya registrado desde el chat guiado («registrar lo que envié»): no se duplica
            ya = bool(texto) and any(_norm_txt(t.texto)[:300] == _norm_txt(texto)[:300] for t in enviados)
            if simular:
                ultimo = await _hora_ultimo_turno(db, consulta.id)
                relee = (consulta.campo not in ("zona_responsable", "numero_supervisor")
                         and (ultimo is None or ultimo <= m.enviado_en))
                hechos.append({"msg_id": m.id, "propio": True, "persona": contacto.nombre,
                               "consulta_id": consulta.id, "reg_id": reg.id, "pdl": reg.pdl,
                               "match": metodo, "accion": "ya estaba en el hilo" if ya else "turno enviado (a mano)",
                               "relee_ia": (not ya) and relee, "texto": texto[:120]})
                continue
            if not ya:
                db.add(AnsConsultaMsg(consulta_id=consulta.id, direccion="enviado",
                                      persona=contacto.nombre,
                                      texto=(texto or f"({m.tipo} adjunto)")[:4000],
                                      # la hora REAL de WhatsApp: el hilo se ordena por creado_en
                                      creado_en=m.enviado_en,
                                      meta=json.dumps({"rol_dir": contacto.rol or "otro", "via": "manual",
                                                       "wa_id": m.wa_id, "msg_id": m.id, "tipo": m.tipo,
                                                       "mime": m.media_mime, "match": metodo})))
                await db.flush()
                # La IA relee el hilo con nuestra respuesta adentro: si era lo que preguntaron,
                # la conversación deja de figurar como «te preguntan a ti» y la bitácora lo dice.
                # SOLO si nuestro mensaje es lo último de la conversación: uno que quedó en medio
                # (después le respondieron o se le escribió a otro) ya está cubierto por la lectura
                # vigente, y releer reescribiría la bitácora con un estado viejo (un
                # mensaje a una persona de las 08:30 va antes del envío a otra de las 13:05).
                # Las consultas a programación no tienen decisión ni celda: solo queda el turno.
                ultimo = await _hora_ultimo_turno(db, consulta.id, excluir_msg=m.id)
                if (consulta.campo not in ("zona_responsable", "numero_supervisor")
                        and (ultimo is None or ultimo <= m.enviado_en)):
                    dec, _msgs = await _clasificar_hilo(db, reg, consulta, label, objetivo)
                    db.add(AnsConsultaMsg(consulta_id=consulta.id, direccion="ia", persona=None,
                                          texto=dec.get("resumen") or dec.get("error"),
                                          meta=json.dumps(dec, ensure_ascii=False)))
                    _aplicar_lectura(reg, consulta, dec, razon_field, contacto.nombre)
            m.estado = "vinculado"
            hechos.append({"wa_id": m.wa_id, "persona": contacto.nombre, "consulta_id": consulta.id,
                           "reg_id": reg.id, "accion_ia": "propio" if not ya else "propio-ya-registrado"})
            log.info("mensaje propio a %s → hilo %s (reg %s)%s", contacto.nombre, consulta.id, reg.id,
                     " — ya estaba registrado" if ya else "")
            continue
        if simular:
            hechos.append({"msg_id": m.id, "propio": False, "persona": contacto.nombre,
                           "consulta_id": consulta.id, "reg_id": reg.id, "pdl": reg.pdl, "match": metodo,
                           "accion": "turno recibido", "texto": (m.texto or "")[:120]})
            continue

        # 1. Si adjuntó algo (el correo del C.O. suele llegar como imagen/pdf), directo a
        #    evidencia de la celda — copiado, para que viva aunque el inbox se depure.
        #    Un AUDIO (nota de voz, muy común entre los ingenieros) además se transcribe
        #    en local (faster-whisper, $0) para que la IA tenga QUÉ leer: sin eso el turno
        #    quedaba como «(audio adjunto)» y la bitácora no avanzaba.
        texto = (m.texto or "").strip()
        nota_media, lectura = "", None
        etiqueta, contenido = await leer_media(m)
        if contenido:
            texto = (texto + "\n" if texto else "") + f"[{etiqueta}] {contenido}"
            lectura = contenido
        if m.media_ruta and os.path.exists(m.media_ruta):
            carpeta = os.path.join(EVID_DIR, current_entorno(), str(reg.id), decision)
            os.makedirs(carpeta, exist_ok=True)
            seguro = re.sub(r"[^\w.\-]", "_", os.path.basename(m.media_ruta))[:120]
            destino = os.path.join(carpeta, f"{int(datetime.now().timestamp())}_{seguro}")
            shutil.copyfile(m.media_ruta, destino)
            e = AnsEvidencia(reg_id=reg.id, campo=decision,
                             nombre=f"WhatsApp de {contacto.nombre} ({m.tipo})",
                             ruta=destino, mime=m.media_mime, tamano=m.media_tamano,
                             transcripcion=lectura,
                             nota=f"Recibido por el bot en el chat directo el "
                                  f"{m.enviado_en:%d/%m/%Y %H:%M} UTC")
            _paq.defaults_nueva(e)   # PAQUETE PARA ENEL (14 oct 2026)
            vered = await ubicar_video(db, reg, e, m.media_ruta)
            if vered:
                texto = (texto + " " if texto else "") + f"[el sistema: {vered}]"
            db.add(e)
            await db.flush()
            m.evidencia_id = e.id
            nota_media = (f" [adjuntó {m.tipo} — guardado como evidencia"
                          + ("; ya leído por la máquina]" if lectura else "; la máquina no pudo leerlo]"))

        # 2. El turno 'recibido' en el hilo (mismo formato del chat guiado)
        db.add(AnsConsultaMsg(consulta_id=consulta.id, direccion="recibido",
                              persona=contacto.nombre,
                              texto=((texto + nota_media).strip() or f"({m.tipo} adjunto)")[:4000],
                              meta=json.dumps({"rol_dir": contacto.rol or "otro",
                                               "via": "bot", "wa_id": m.wa_id,
                                               "match": metodo,
                                               # para que el tab reproduzca el audio/video y
                                               # muestre la foto sin salir de la plataforma
                                               "msg_id": m.id, "tipo": m.tipo,
                                               "mime": m.media_mime,
                                               "lectura": (lectura or None)})))
        await db.flush()

        # 3a. Consulta a PROGRAMACIÓN (zona/supervisor, 2026-09-23): no hay decisión de exclusión
        #     que clasificar ni celda de ENEL que tocar. Queda el turno en el hilo, el hilo pasa a
        #     «respondida» y el operador recibe el aviso: corrige la zona/el supervisor y sigue.
        if consulta.campo in ("zona_responsable", "numero_supervisor"):
            consulta.estado = "respondida"
            consulta.resumen = (texto or f"({m.tipo} adjunto)")[:500]
            m.estado = "vinculado"
            hechos.append({"wa_id": m.wa_id, "persona": contacto.nombre, "consulta_id": consulta.id,
                           "reg_id": reg.id, "accion_ia": "programacion"})
            log.info("respuesta de programación (%s) → hilo %s (reg %s)", contacto.nombre, consulta.id, reg.id)
            try:
                import subprocess
                notify = os.getenv("SGO7_NOTIFY_CMD", "")
                if notify and os.path.exists(notify):
                    que = ("la zona/supervisor" if consulta.campo == "zona_responsable"
                           else "el numero del supervisor")
                    subprocess.run([notify, f"SGO7 ANS: {contacto.nombre} respondio por {que} "
                                                       f"de la orden {reg.pdl}. Revisa el tab Auditoria IA."],
                                   timeout=60, check=False)
            except Exception as ex:                                       # noqa: BLE001
                log.warning("aviso de respuesta de programación falló: %s", ex)
            continue

        # 3. La IA clasifica TODA la conversación y gobierna la bitácora (idéntico a
        #    consulta/analizar: misma puerta `_clasificar_hilo` + `_aplicar_lectura`, así el
        #    bot y el chat guiado nunca se desincronizan). La decisión NO se aplica — eso
        #    sigue siendo humano. Lo que sí sale de aquí es la CALIDAD de la respuesta: el
        #    embudo que el tab Auditoría IA usa para saber qué sigue (2026-09-24).
        dec, _msgs = await _clasificar_hilo(db, reg, consulta, label, objetivo)
        db.add(AnsConsultaMsg(consulta_id=consulta.id, direccion="ia", persona=None,
                              texto=dec.get("resumen") or dec.get("error"),
                              meta=json.dumps(dec, ensure_ascii=False)))
        _aplicar_lectura(reg, consulta, dec, razon_field, contacto.nombre)
        await _avisar_embudo(reg, consulta, contacto.nombre, dec)

        m.estado = "vinculado"
        hechos.append({"wa_id": m.wa_id, "persona": contacto.nombre,
                       "consulta_id": consulta.id, "reg_id": reg.id,
                       "accion_ia": dec.get("accion"), "calidad": dec.get("calidad"),
                       "con_media": bool(nota_media), "leido": bool(lectura)})
        log.info("respuesta de %s → hilo %s (reg %s, acción IA: %s, calidad: %s)",
                 contacto.nombre, consulta.id, reg.id, dec.get("accion"), dec.get("calidad"))

    if not simular:                          # PASO 1 CON RESPUESTA (19 oct 2026, ver CHANGELOG)
        await db.commit()
    return hechos
