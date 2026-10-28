"""El analista automático del 3.2 A: junta el material de WhatsApp de un caso y se lo da a
la IA con el método documentado en docs/IA.md.

Flujo por caso (reg del ANS):
  1. Día + zona → grupos coherentes (Exclusiones/Cuadrillas de la zona) → mensajes del día.
  2. Videos del día: frames con ffmpeg (para que la IA LEA el overlay: fecha, hora, GPS,
     PDL) + transcripción del audio (faster-whisper, la misma del ANS).
  3. La IA arma la línea de tiempo, atribuye videos por PDL del overlay, decide cobertura del
     desfase y redacta la observación borrador.
  4. Todo el proceso queda en `wa_analisis` (JSON) para que el desarrollador lo AUDITE en su
     tab antes de aplicar nada a la celda.

La BD del bot guarda UTC; Colombia es UTC-5 — toda la ventana del día se maneja en local.
"""
import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import WaMensaje, WaGrupo, WaAnalisis, AnsRegistro, WaMediaMeta
from app.services import claude, letrero
from app.services.transcribe import transcribir_video

log = logging.getLogger("wa_analista")

UTC_OFFSET = timedelta(hours=5)          # local Colombia = UTC - 5
MAX_VIDEOS = 8                           # techo de videos por análisis (costo/contexto)
FRAMES_POR_VIDEO = 3                     # inicio + mitad + FINAL: los relojes de los dos
                                         # extremos dan el lapso grabado (auditoría 24-08)
TOLERANCIA_MIN = 10                      # desviación aceptada POR GAP (10 apertura + 10
                                         # cierre) al sumar tramos contra el desfase

# Zona del ANS → patrones de nombre de grupo. Chocontá pertenece a la zona ALMEIDAS.
# LV (Línea Viva, órdenes CDOPT) no tiene grupos propios: sus cuadrillas reportan en los
# grupos de Ubaté (patrón verificado en wa_mensajes 2026-08-18; la atribución real de
# cada video la decide el overlay, así que el falso positivo se filtra solo).
GRUPOS_ZONA = {
    # «cumplimentaci» = grupo «Cumplimentación ZONA 7»: ahí las cuadrillas de TODAS las
    # zonas publican sus PLANTILLAS OFICIALES (hallazgo 2026-08-31: una
    # plantilla vivía ahí y el grupo no estaba vigilado).
    "GUAVIO": ["guavio", "cumplimentaci"],
    "UBATE": ["ubate", "ubat", "cumplimentaci"],
    "ALMEIDAS": ["almeidas", "choconta", "cumplimentaci"],
    "LV": ["ubate", "ubat", "cumplimentaci"],
}


def _local(dt_utc: datetime) -> datetime:
    return dt_utc - UTC_OFFSET


async def _ffprobe_duracion(ruta: str) -> float:
    p = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", ruta,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


async def _frame_b64(ruta: str, segundo: float) -> str | None:
    """Un frame como JPEG base64, reescalado a 640px de ancho (suficiente para leer el
    overlay y barato en tokens de visión)."""
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "quiet", "-ss", f"{max(segundo, 0):.2f}", "-i", ruta,
        "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    return base64.b64encode(out).decode() if out else None


def _parse_fecha(ddmmyyyy: str):
    try:
        return datetime.strptime((ddmmyyyy or "").strip()[:10], "%d/%m/%Y")
    except ValueError:
        return None


def _desfase_min(fecha_plan, hora_plan, real):
    """Minutos de desfase de un gap (plan vs real), o None si algo no parsea."""
    try:
        plan = datetime.strptime(f"{(fecha_plan or '').strip()} {(hora_plan or '').strip()}",
                                 "%d/%m/%Y %H:%M")
        rdt = datetime.strptime((real or "").strip(), "%d/%m/%Y %H:%M")
        return int((rdt - plan).total_seconds() // 60)
    except (ValueError, AttributeError):
        return None


def _desfase_txt(etiqueta, fecha_plan, hora_plan, real) -> str:
    mins = _desfase_min(fecha_plan, hora_plan, real)
    if mins is None:
        return ""
    if mins <= 0:
        return f"\nDESFASE DE {etiqueta}: ninguno (cumplió)"
    return f"\nDESFASE DE {etiqueta}: {mins // 60} h {mins % 60} min ({mins} min)"


def _contexto_orden(reg) -> str:
    """El caso, como se lo contamos a la IA — con los DOS desfases calculados (apertura y
    cierre). El piloto solo miraba el cierre y la auditoría lo detectó: la apertura
    incumplida también se analiza."""
    desfases = (_desfase_txt("APERTURA", reg.fecha_inicio, reg.hora_inicio, reg.maniobra_inicio)
                + _desfase_txt("CIERRE", reg.fecha_fin, reg.hora_fin, reg.maniobra_fin))
    return (f"PDL: {reg.pdl}\nZONA: {reg.zona}\nRESPONSABLE: {reg.nombre_responsable}\n"
            f"DESCRIPCIÓN: {reg.descripcion}\n"
            f"APERTURA PLANEADA: {reg.fecha_inicio} {reg.hora_inicio}\n"
            f"CIERRE PLANEADO: {reg.fecha_fin} {reg.hora_fin}\n"
            f"MANIOBRA (real): {reg.maniobra_inicio} → {reg.maniobra_fin}{desfases}\n"
            f"(Todas las horas en hora local de Colombia.)")


def _es_32b(reg) -> bool:
    """¿Este caso es una ANULACIÓN (3.2 B)? Mismo criterio del ANS: estado anulado o
    se_ejecuto=NO. En esos casos NO hay videos que buscar: el soporte es el correo del
    Centro Operativo, y quien lo consigue es el profesional de apoyo de la zona."""
    from app.api.ans import _es_anulado          # criterio único (no duplicarlo aquí)
    return _es_anulado(reg.estado_actual) or (reg.se_ejecuto or "").strip().upper() == "NO"


async def _proponer_consulta_32b(db: AsyncSession, reg) -> WaAnalisis:
    """Propuesta 3.2 B: el mensaje YA REDACTADO (plantilla determinista del ANS, $0 de IA)
    al profesional de apoyo de la zona pidiendo el correo del Centro Operativo. El
    desarrollador lo audita y al aplicar se encola al bot."""
    from app.api.ans import _mensaje_wa          # la plantilla oficial (tono, trato, ficha)
    from app.models.models import Contacto

    zona = (reg.zona or "").strip().upper()
    apoyo = None
    # Sin zona NO se elige apoyo (fallo 2026-09-29: ilike '%%' calzaba con todos y salía el apoyo
    # de otra zona). Sin zona manda el Paso 1 (programación).
    if zona:
        r = await db.execute(select(Contacto).where(Contacto.activo, Contacto.rol == "apoyo",
                                                    Contacto.zona.ilike(f"%{zona}%")).order_by(Contacto.id))
        apoyo = r.scalars().first()
    mensaje = _mensaje_wa(reg, "excluible_anulado",
                          quien=apoyo.nombre if apoyo else "", rol_dir="apoyo")
    resultado = {
        "tipo": "3.2B",
        "caso": {"pdl": reg.pdl, "zona": reg.zona, "responsable": reg.nombre_responsable,
                 "fecha": reg.fecha_fin, "estado_actual": reg.estado_actual,
                 "contexto": (f"PDL: {reg.pdl}\nZONA: {reg.zona}\n"
                              f"ESTADO: {reg.estado_actual} (se_ejecuto={reg.se_ejecuto})\n"
                              f"DESCRIPCIÓN: {reg.descripcion}\n"
                              f"CIERRE PLANEADO: {reg.fecha_fin} {reg.hora_fin}")},
        "consulta": {
            "persona": apoyo.nombre if apoyo else None,
            "rol": "apoyo",
            "telefono": apoyo.telefono if apoyo else None,
            "mensaje": mensaje,
            "sin_zona": None if zona else True,
            "sin_contacto": None if apoyo else (
                f"No hay profesional de apoyo con teléfono para la zona {zona} en el directorio" if zona else
                "La orden no tiene zona: no se sabe qué profesional de apoyo le corresponde. "
                "Primero el Paso 1 (preguntar a programación) o corrige la zona en el grid."),
        },
        "ia": {"resumen": (f"CASO 3.2 B: ORDEN ANULADA ({reg.estado_actual}). EL PROCEDIMIENTO NO ES "
                           "BUSCAR VIDEOS: EL SOPORTE ES EL CORREO DEL CENTRO OPERATIVO Y LO CONSIGUE "
                           "EL PROFESIONAL DE APOYO DE LA ZONA. SE PROPONE EL MENSAJE YA REDACTADO "
                           "(PLANTILLA OFICIAL); AL APLICAR, EL BOT LO ENVÍA Y QUEDA EL HILO ABIERTO "
                           "ESPERANDO LA RESPUESTA."),
               "confianza": "alta", "excluible_sugerido": None, "categoria": None},
    }
    a = WaAnalisis(reg_id=reg.id, campo="excluible_anulado", estado="propuesto",
                   resultado=json.dumps(resultado, ensure_ascii=False))
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _proponer_sin_material(db: AsyncSession, reg, motivo: str) -> WaAnalisis:
    """3.2 A que NO se puede buscar en los grupos (sin zona, zona sin grupos vigilados o
    fecha ilegible): el protocolo NO se detiene — queda la propuesta en el tab con el
    PASO 2 (preguntar al líder/supervisor), que no depende de la zona. Requisito
    2026-08-19: antes esto solo fallaba en el log del barrido y el
    caso quedaba invisible."""
    resultado = {
        "tipo": "3.2A",
        "caso": {"pdl": reg.pdl, "zona": reg.zona, "responsable": reg.nombre_responsable,
                 "fecha": reg.fecha_fin, "cierre_planeado": reg.hora_fin,
                 "maniobra_inicio": reg.maniobra_inicio, "maniobra_fin": reg.maniobra_fin,
                 "contexto": _contexto_orden(reg)},
        "grupos": [],
        "eventos": [],
        "videos": [],
        "ia": {"resumen": (f"NO SE PUDO BUSCAR MATERIAL EN WHATSAPP: {motivo.upper()}. "
                           "EL PROTOCOLO SIGUE POR EL PASO 2: PREGUNTAR AL LÍDER (COL E) "
                           "O AL SUPERVISOR (COL G) POR LO OCURRIDO ESE DÍA."),
               "videos": [], "linea_tiempo": [], "categoria": None,
               "excluible_sugerido": None,
               "cobertura_desfase": f"SIN MATERIAL — {motivo.upper()}",
               "observacion": "", "confianza": "baja"},
    }
    a = WaAnalisis(reg_id=reg.id, campo="excluible", estado="propuesto",
                   resultado=json.dumps(resultado, ensure_ascii=False))
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


def _rnum(pdl: str) -> str | None:
    """El número corto del PDL («CDOPP2026R07958» → «R07958»): es lo que las cuadrillas
    estampan en el overlay y escriben en los mensajes."""
    import re as _re
    m = _re.search(r"R\d{4,6}", (pdl or "").upper())
    return m.group(0) if m else None


async def _rastrear_otros_grupos(db: AsyncSession, reg, ini_utc, fin_utc,
                                 jids_excluidos: list) -> tuple[list, str]:
    """FASE AMPLIADA (requisito 2026-08-20: las cuadrillas suben a distintos
    grupos): rastrea el día en TODOS los demás grupos vigilados buscando el PDL del
    caso, con costo mínimo — grep gratis en los textos + UN frame por video y una llamada
    liviana que solo lee el código del overlay. Devuelve (jids con rastro, nota)."""
    num = _rnum(reg.pdl)
    r = await db.execute(select(WaGrupo).where(WaGrupo.vigilado))
    resto = [g.jid for g in r.scalars().all() if g.jid not in jids_excluidos]
    if not resto or not num:
        return [], "sin fase ampliada (no hay más grupos o PDL ilegible)"

    hallados = set()
    # 1. Textos que mencionen el número — gratis
    r = await db.execute(select(WaMensaje).where(
        WaMensaje.chat_jid.in_(resto), WaMensaje.de_mi.isnot(True),
        WaMensaje.enviado_en >= ini_utc, WaMensaje.enviado_en < fin_utc,
        WaMensaje.texto.ilike(f"%{num}%")))
    for m in r.scalars().all():
        hallados.add(m.chat_jid)

    # 2. Videos del día en el resto de grupos: 1 frame c/u → lectura barata del overlay
    r = await db.execute(select(WaMensaje).where(
        WaMensaje.chat_jid.in_(resto), WaMensaje.tipo == "video", WaMensaje.de_mi.isnot(True),
        WaMensaje.media_ruta.isnot(None),
        WaMensaje.enviado_en >= ini_utc, WaMensaje.enviado_en < fin_utc)
        .order_by(WaMensaje.enviado_en))
    vids, tam_vistos = [], set()
    for m in r.scalars().all():
        if m.media_tamano in tam_vistos or not os.path.exists(m.media_ruta):
            continue
        tam_vistos.add(m.media_tamano)
        vids.append(m)
    revisados = 0
    # 2a. Lo que el ÍNDICE ya leyó (OCR del letrero) resuelve sin IA (orquestación 2026-09-07)
    metas = {x.msg_id: x for x in (await db.execute(select(WaMediaMeta).where(
        WaMediaMeta.msg_id.in_([m.id for m in vids]), WaMediaMeta.estado == "listo"))).scalars().all()} if vids else {}
    from app.services import matcher as _mt
    for m in vids:
        cod = _mt.codigo_letrero(metas.get(m.id))
        if cod and cod == num:
            hallados.add(m.chat_jid)
    revisados = len([m for m in vids if m.id in metas])
    vids = [m for m in vids if m.id not in metas]     # a la IA solo lo que el índice no cubrió
    if vids:
        recorte = vids[:12]                       # techo de la pasada barata (costo)
        frames = []
        for m in recorte:
            b64 = await _frame_b64(m.media_ruta, 1.0)
            if b64:
                frames.append({"msg_id": m.id, "frame": b64, "jid": m.chat_jid})
        revisados += len(frames)
        if frames:
            lectura = await claude.leer_overlays(
                [{"msg_id": f["msg_id"], "frame": f["frame"]} for f in frames])
            por_id = {f["msg_id"]: f["jid"] for f in frames}
            for v in lectura.get("videos", []):
                if letrero.codigo_principal(v.get("pdl_overlay") or "") == num:
                    hallados.add(por_id.get(v["msg_id"]))
        if len(vids) > 12:
            log.warning("fase ampliada reg %s: %d videos, solo se revisaron 12", reg.id, len(vids))

    hallados.discard(None)
    if hallados:
        nota = f"rastro del {num} hallado en otros grupos (fase ampliada)"
    else:
        nota = (f"fase ampliada: se rastrearon los otros {len(resto)} grupos del día "
                f"({revisados} videos revisados por overlay + textos) sin rastro del {num}")
    return sorted(hallados), nota


async def _material_del_dia(db: AsyncSession, jids: list, ini_utc, fin_utc):
    r = await db.execute(select(WaMensaje)
                         .where(WaMensaje.chat_jid.in_(jids),
                                # lo propio en grupos se guarda desde el 26-09; el
                                # analista sigue leyendo solo lo de los demás, como antes
                                WaMensaje.de_mi.isnot(True),
                                WaMensaje.enviado_en >= ini_utc,
                                WaMensaje.enviado_en < fin_utc)
                         .order_by(WaMensaje.enviado_en))
    return r.scalars().all()


def _hallo_algo(ia: dict) -> bool:
    """¿La fase de zona encontró algo del caso? Videos atribuidos o un veredicto
    sustentado en textos. Si no, vale la pena la fase ampliada."""
    if any(v.get("del_caso") == "SI" for v in (ia.get("videos") or [])):
        return True
    return ia.get("excluible_sugerido") is not None


async def analizar_caso(db: AsyncSession, reg) -> WaAnalisis:
    """Corre el método sobre un registro del ANS y persiste el análisis. PRIMERO el router
    de tipo (3.2 B → consulta al apoyo); en 3.2 A la búsqueda es EN DOS FASES:
      1ª — los grupos de la zona de la tabla (rápida, como siempre);
      2ª — si la 1ª no atribuye nada (o no hay zona), rastreo barato del PDL en TODOS los
           demás grupos vigilados (textos + overlay de 1 frame/video); si aparece, el
           análisis completo se corre donde de verdad está el material.
    Así el método no depende 100% de la zona de la tabla (las cuadrillas a veces suben
    al grupo equivocado)."""
    if _es_32b(reg):
        return await _proponer_consulta_32b(db, reg)
    # Orquestación (2026-09-07): antes de mirar nada, el amarre determinista del caso se pone
    # al día — así los videos amarrados por código/caption/líder entran SIEMPRE al análisis.
    from app.services import matcher
    try:
        await matcher.amarrar(db, reg_ids=[reg.id])
    except Exception as e:                            # noqa: BLE001 — el matcher nunca frena el análisis
        log.warning("matcher falló para reg %s: %s", reg.id, e)
    dia = _parse_fecha(reg.fecha_fin)
    if not dia:
        return await _proponer_sin_material(
            db, reg, f"el registro no tiene fecha de cierre legible ({reg.fecha_fin!r})")

    # Ventana del día LOCAL completo (y la madrugada siguiente: los cierres tardíos)
    ini_utc = dia + UTC_OFFSET
    fin_utc = dia + timedelta(hours=30) + UTC_OFFSET   # hasta las 6 a.m. locales del día siguiente

    patrones = GRUPOS_ZONA.get((reg.zona or "").upper().replace("É", "E"), [])
    r = await db.execute(select(WaGrupo).where(WaGrupo.vigilado))
    jids = [g.jid for g in r.scalars().all()
            if any(p in (g.nombre or "").lower().replace("é", "e") for p in patrones)]

    # FASE 1 — grupos de la zona (si hay). Si atribuye material, ahí termina.
    resultado_zona = None
    if jids:
        resultado_zona = await _analizar_en_grupos(db, reg, jids, ini_utc, fin_utc,
                                                   alcance="grupos de la zona")
        if _hallo_algo(resultado_zona["ia"]):
            return await _persistir(db, reg, resultado_zona)

    # FASE 2 — rastreo barato del PDL en los demás grupos vigilados
    hallados, nota = await _rastrear_otros_grupos(db, reg, ini_utc, fin_utc, jids)
    if hallados:
        resultado = await _analizar_en_grupos(db, reg, hallados, ini_utc, fin_utc,
                                              alcance=f"fase ampliada — {nota}")
        resultado["ia"]["resumen"] = ("[BÚSQUEDA AMPLIADA: EL MATERIAL NO ESTABA EN LOS "
                                      "GRUPOS DE LA ZONA] " + (resultado["ia"].get("resumen") or ""))
        return await _persistir(db, reg, resultado)

    # Nada en ningún lado: queda el resultado de zona (si hubo) con la constancia del
    # rastreo ampliado, o la propuesta de Paso 2.
    if resultado_zona:
        resultado_zona["ia"]["resumen"] = ((resultado_zona["ia"].get("resumen") or "")
                                           + f" [{nota.upper()}]")
        resultado_zona["ia"]["cobertura_desfase"] = ((resultado_zona["ia"].get("cobertura_desfase") or "")
                                                     + f" · {nota}")
        return await _persistir(db, reg, resultado_zona)
    motivo = ("el caso no tiene zona asignada en el grid" if not (reg.zona or "").strip()
              else f"la zona {reg.zona} no tiene grupos de WhatsApp vigilados")
    return await _proponer_sin_material(db, reg, f"{motivo}; {nota}")


async def _analizar_en_grupos(db: AsyncSession, reg, jids: list, ini_utc, fin_utc,
                              alcance: str) -> dict:
    """El método completo (textos + frames + transcripción + IA) sobre un conjunto de
    grupos. Devuelve el resultado SIN persistir — el orquestador decide qué fase vale."""
    mensajes = await _material_del_dia(db, jids, ini_utc, fin_utc)

    # ── Amarres deterministas (orquestación): media del caso esté en el grupo que esté ──
    from app.services import matcher
    amarres = await matcher.amarres_de(db, reg.id, minimo=75)
    ids_dia = {m.id for m in mensajes}
    extra_ids = [i for i in amarres if i not in ids_dia]
    if extra_ids:
        r_extra = await db.execute(select(WaMensaje).where(WaMensaje.id.in_(extra_ids)))
        mensajes = list(mensajes) + list(r_extra.scalars().all())
        jids = list(dict.fromkeys(list(jids) + [m.chat_jid for m in mensajes]))

    # Nombre corto de cada grupo — la línea de tiempo cita el GRUPO de origen de cada
    # dato (requisito de auditoría: saber de dónde salió cada dato).
    r = await db.execute(select(WaGrupo).where(WaGrupo.jid.in_(jids)))
    nombre_grupo = {g.jid: (g.nombre or g.jid) for g in r.scalars().all()}

    # Textos del día (dedupe: los grupos Exclusiones/Cuadrillas espejan los mismos mensajes).
    # Cada mensaje va ETIQUETADO por cuadrilla (auditoría 2026-09-22: Llama justificó el cierre
    # de un caso con «el centro de control no contesta» escrito por la cuadrilla de otra orden):
    # quien ese día publicó un texto con el número de un PDL (la plantilla oficial) es la
    # cuadrilla de ese PDL. Del caso → [CUADRILLA DEL CASO]; de otro → [CUADRILLA DE OTRO PDL].
    rnum_caso, dfs_caso = matcher._codigos_del_caso(reg)
    textos = [m for m in mensajes if m.tipo == "texto" and (m.texto or "").strip()]
    rn_por_autor = {}
    for m in textos:
        if m.remitente_jid:
            rn_por_autor.setdefault(m.remitente_jid, set()).update(letrero.codigos_r(m.texto))
    eventos, vistos, eventos_caso = [], set(), 0
    for m in textos:
        clave = m.texto.strip()[:120]
        if clave in vistos:
            continue
        vistos.add(clave)
        quien = f" ({m.remitente_nombre})" if m.remitente_nombre else ""
        rn_txt = set(letrero.codigos_r(m.texto))
        rn_autor = rn_por_autor.get(m.remitente_jid, set()) if m.remitente_jid else set()
        otros = sorted((rn_txt | rn_autor) - ({rnum_caso} if rnum_caso else set()))
        if rnum_caso and (rnum_caso in rn_txt or rnum_caso in rn_autor):
            tag = " [CUADRILLA DEL CASO]" + (f" (también reporta {', '.join(otros)})" if otros else "")
            eventos_caso += 1
        elif otros:
            tag = f" [CUADRILLA DE OTRO PDL: {', '.join(otros)} — NO ES EVIDENCIA DE ESTE CASO]"
        else:
            tag = ""
        eventos.append(f"{_local(m.enviado_en):%H:%M} [{nombre_grupo.get(m.chat_jid, '?')}]"
                       f"{quien}{tag}: {m.texto.strip()[:600]}")

    # Videos del día con archivo (dedupe por tamaño: el mismo video espejado pesa igual).
    candidatos, tam_vistos = [], set()
    for m in sorted(mensajes, key=lambda x: (0 if x.id in amarres else 1, x.enviado_en or datetime.min)):
        if m.tipo != "video" or not m.media_ruta or not os.path.exists(m.media_ruta):
            continue
        if m.media_tamano in tam_vistos:
            continue
        tam_vistos.add(m.media_tamano)
        candidatos.append(m)

    # Índice de media de TODOS los candidatos (letrero ya leído, frames, transcripción)
    metas = {x.msg_id: x for x in (await db.execute(select(WaMediaMeta).where(
        WaMediaMeta.msg_id.in_([m.id for m in candidatos]), WaMediaMeta.estado == "listo"))).scalars().all()} if candidatos else {}

    # ── FILTRO DETERMINISTA POR LETRERO (auditoría 2026-09-22) ────────────────────────
    # Antes la IA llegó a anexar videos cuyo letrero era de otra orden. Ahora el código
    # del letrero se lee ANTES (OCR crudo con el lector tolerante; si no sale, visión IA
    # sobre el primer frame) y decide: propio → material obligatorio del caso; ajeno →
    # fuera, ni se le muestra a la IA (queda listado en `videos_excluidos`); sin_codigo →
    # candidato que la IA solo puede afirmar con amarre fuerte o leyendo el código.
    rnum_caso, dfs_caso = matcher._codigos_del_caso(reg)
    clasif = {}                                   # msg_id → {"clase", "codigo", "fuente"}
    for m in candidatos:
        cod = matcher.codigo_letrero(metas.get(m.id))
        clasif[m.id] = {"clase": letrero.clasificar(cod, rnum_caso, dfs_caso), "codigo": cod,
                        "fuente": "ocr" if cod else None}
    sin_cod = [m for m in candidatos if clasif[m.id]["clase"] == "sin_codigo"][:12]
    # MÁS PUNTOS DEL LETRERO (16 oct 2026): el letrero del primer frame puede quedar ilegible y verse
    # nítido al final. Lo que el frame 1 no resolvió se mira en el último.
    for sacar_frame in (_frame1_b64, _frame_final_b64):
        sin_cod = [m for m in sin_cod if clasif[m.id]["clase"] == "sin_codigo"]
        if not sin_cod:
            break
        frames1 = []
        for m in sin_cod:
            b64 = await sacar_frame(m, metas.get(m.id))
            if b64:
                frames1.append({"msg_id": m.id, "frame": b64})
        if frames1:
            lectura = await claude.leer_overlays(frames1)
            for v in lectura.get("videos", []):
                cod = letrero.codigo_principal(v.get("pdl_overlay") or "")
                if cod and v.get("msg_id") in clasif:
                    clasif[v["msg_id"]] = {"clase": letrero.clasificar(cod, rnum_caso, dfs_caso),
                                           "codigo": cod, "fuente": "vision"}
                    # Lo que la visión leyó queda en el ÍNDICE: el matcher lo amarra por código la
                    # próxima vez y la auditoría fría lo ve (2026-09-23: dos videos propios por visión
                    # que la auditoría marcaba como «SI sin código»).
                    meta = metas.get(v["msg_id"])
                    if meta and not meta.overlay_codigo:
                        meta.overlay_codigo = cod
                        meta.overlay_txt = (meta.overlay_txt or "") + "\n\n[vision IA @ analisis]\npdl " + cod
                        db.add(meta)
            if lectura.get("error"):
                log.warning("lectura de overlays falló (reg %s): %s", reg.id, lectura["error"])
    videos_excluidos = [{"msg_id": m.id, "grupo": nombre_grupo.get(m.chat_jid, "?"),
                         "hora_subida": f"{_local(m.enviado_en):%H:%M}",
                         "codigo": clasif[m.id]["codigo"], "fuente": clasif[m.id]["fuente"],
                         "motivo": f"letrero de otra orden ({clasif[m.id]['codigo']})"}
                        for m in candidatos if clasif[m.id]["clase"] == "ajeno"]
    candidatos = [m for m in candidatos if clasif[m.id]["clase"] != "ajeno"]

    # Los OBLIGATORIOS (amarre ≥ 75 o letrero propio) van primero y no cuentan para el tope:
    # jamás se quedan por fuera.
    obligatorios = [m for m in candidatos if m.id in amarres or clasif[m.id]["clase"] == "propio"]
    resto = [m for m in candidatos if m not in obligatorios]
    videos_meta = obligatorios + resto[:max(0, MAX_VIDEOS - len(obligatorios))]

    videos_ia = []
    for m in videos_meta:
        meta = metas.get(m.id)
        frames, segs = [], []                     # segs[i] = segundo del video del frame i
        dur = meta.duracion_s if meta and meta.duracion_s else await _ffprobe_duracion(m.media_ruta)
        if meta and meta.frames_dir and os.path.isdir(meta.frames_dir):
            # los puntos del índice (indexar_media.procesar): 1 s, mitad y final − 1,5 s
            for i, seg in zip((1, 2, 3), (1.0, (dur or 2) / 2, max((dur or 2) - 1.5, 0.5))):
                f = os.path.join(meta.frames_dir, f"f{i}.jpg")
                if os.path.exists(f):
                    with open(f, "rb") as fh:
                        frames.append(base64.b64encode(fh.read()).decode())
                    segs.append(seg)
        if not frames:
            # Inicio, mitad y final: el reloj del overlay en los extremos es lo que permite
            # calcular el lapso exacto que el video justifica (tramos_justificados).
            for seg in (1.0, dur / 2 if dur > 4 else 2.0,
                        max(dur - 1.5, 2.5))[:FRAMES_POR_VIDEO]:
                b64 = await _frame_b64(m.media_ruta, seg)
                if b64:
                    frames.append(b64)
                    segs.append(seg)
        # CUÁNDO SE GRABÓ (auditoría 2026-10-01): del letrero, con dos lecturas que cuadren. La
        # visión solo se gasta en los videos que van a contar para el caso.
        es_del_caso = m.id in amarres or (clasif.get(m.id) or {}).get("clase") == "propio"
        grabado = await _reloj_del_video(db, m, meta, dur, frames, segs, con_vision=es_del_caso)
        if meta and meta.transcripcion is not None:
            trans = meta.transcripcion or None
        else:
            # La transcripción es CPU-pesada (whisper) → hilo aparte para no congelar el worker
            try:
                trans = await asyncio.to_thread(transcribir_video, m.media_ruta)
            except Exception as e:                    # noqa: BLE001 — sin audio no se cae el análisis
                log.warning("transcripción falló para msg %s: %s", m.id, e)
                trans = None
        am = amarres.get(m.id)
        # Un video que aún no pasó por el índice no tiene meta (fallo 2026-10-01: dos casos
        # se caían con «'NoneType' object has no attribute 'overlay_fecha'» y no salían en el tab)
        letrero_txt = (f"{meta.overlay_fecha or ''} {meta.overlay_hora or ''} · {meta.overlay_codigo or ''} · "
                       f"{meta.overlay_lugar or ''} · {('#' + meta.overlay_lider) if meta.overlay_lider else ''}"
                       ).strip(" ·") if meta else None
        cl = clasif.get(m.id) or {}
        videos_ia.append({"msg_id": m.id,
                          "clase": cl.get("clase"), "codigo_letrero": cl.get("codigo"),
                          "fecha_ok": _fecha_cuadra(reg, m, meta),
                          "grupo": nombre_grupo.get(m.chat_jid, "?"),
                          "hora_subida": f"{_local(m.enviado_en):%H:%M}",
                          "grabado": grabado,
                          "duracion": round(dur or 0),
                          "transcripcion": trans,
                          "letrero": letrero_txt if meta else None,
                          "caption": (m.texto or "")[:300] or None,
                          "amarre": f"{am.criterio} ({am.puntaje}): {am.detalle}" if am else None,
                          "frames": frames})

    ia = await claude.analizar_caso_wa(_contexto_orden(reg), "\n".join(eventos), videos_ia)
    if not ia.get("error"):
        conocidos = await _pdls_del_periodo(db, reg)
        _validar_atribucion(reg, ia, videos_ia, conocidos, eventos_caso, eventos)

    return {
        "tipo": "3.2A",
        "alcance": alcance,
        "videos_excluidos": videos_excluidos,
        "eventos_caso": eventos_caso,
        "caso": {"pdl": reg.pdl, "zona": reg.zona, "responsable": reg.nombre_responsable,
                 "fecha": reg.fecha_fin, "cierre_planeado": reg.hora_fin,
                 "maniobra_inicio": reg.maniobra_inicio, "maniobra_fin": reg.maniobra_fin,
                 "contexto": _contexto_orden(reg)},
        "grupos": jids,
        "eventos": eventos,
        "videos": [{"msg_id": v["msg_id"], "grupo": v["grupo"], "hora_subida": v["hora_subida"],
                    "duracion": v["duracion"], "transcripcion": v["transcripcion"],
                    "grabado": v.get("grabado"),
                    "clase": v.get("clase"), "codigo_letrero": v.get("codigo_letrero"),
                    "fuente_codigo": (clasif.get(v["msg_id"]) or {}).get("fuente")}
                   for v in videos_ia],
        "ia": ia,
    }


async def _reloj_del_video(db: AsyncSession, m, meta, dur: float, frames: list, segs: list,
                           con_vision: bool) -> dict | None:
    """Cuándo se grabó el video, según su letrero (`letrero.reloj_grabacion`: dos lecturas de
    reloj que den el mismo inicio). Primero lo que el OCR del índice ya leyó; si no alcanza y
    el video cuenta para el caso, la visión IA lee el letrero de los frames (primero y último;
    el del medio solo si esos dos no cuadran). Lo leído por visión queda en el ÍNDICE como
    bloques «[vision frame i @ Ns]»: la próxima vez no se gasta visión y `--reparsear` lo ve.
    None = no se pudo leer con certeza — y entonces el video no tiene hora, no se le inventa."""
    subido = _local(m.enviado_en) if m.enviado_en else None
    crudo = (meta.overlay_txt if meta else None) or ""
    reloj = letrero.reloj_grabacion(letrero.lecturas_reloj(crudo), dur, subido)
    if reloj or not con_vision or not frames:
        return reloj
    orden = [0, len(frames) - 1] + list(range(1, len(frames) - 1))
    for n, i in enumerate(dict.fromkeys(orden)):
        try:
            txt = await claude.leer_letrero_frame(frames[i])
        except Exception as e:                        # noqa: BLE001 — sin reloj el análisis sigue
            log.warning("lectura del letrero falló (msg %s frame %s): %s", m.id, i + 1, e)
            txt = None
        if txt and letrero.fecha_hora(txt):
            crudo += f"\n\n[vision frame {i + 1} @ {segs[i]:.1f}s]\n{txt.strip()}"
            # una lectura de visión también puede confirmar la única que tenía el OCR
            reloj = letrero.reloj_grabacion(letrero.lecturas_reloj(crudo), dur, subido)
            if reloj:
                break
    # MÁS PUNTOS DEL LETRERO (16 oct 2026): con el letrero sobre follaje los 3 frames pueden no dar dos
    # lecturas; se miran hasta 4 puntos más del video.
    if not reloj and dur and dur > 6 and m.media_ruta:
        for k, frac in enumerate((0.75, 0.25, 0.6, 0.4), len(frames) + 1):
            seg = dur * frac
            b64 = await _frame_b64(m.media_ruta, seg)
            if not b64:
                continue
            try:
                txt = await claude.leer_letrero_frame(b64)
            except Exception as e:                    # noqa: BLE001 — sin reloj el análisis sigue
                log.warning("lectura del letrero falló (msg %s @ %.1fs): %s", m.id, seg, e)
                txt = None
            if txt and letrero.fecha_hora(txt):
                crudo += f"\n\n[vision frame {k} @ {seg:.1f}s]\n{txt.strip()}"
                reloj = letrero.reloj_grabacion(letrero.lecturas_reloj(crudo), dur, subido)
                if reloj:
                    break
    if meta and crudo != (meta.overlay_txt or ""):
        meta.overlay_txt = crudo.strip()
        if reloj and _hora_indice_mala(meta, reloj, dur):
            meta.overlay_fecha, meta.overlay_hora = reloj["fecha"], reloj["inicio"]
        db.add(meta)
    return reloj


def _hora_indice_mala(meta, reloj: dict, dur: float) -> bool:
    """¿La hora que el índice tiene del letrero falta o no cabe en el lapso grabado?"""
    if not meta.overlay_hora or meta.overlay_fecha != reloj["fecha"]:
        return True
    try:
        return abs(letrero._seg_dia(meta.overlay_hora) - letrero._seg_dia(reloj["inicio"])) > (dur or 0) + 60
    except ValueError:
        return True


async def _frame1_b64(m, meta) -> str | None:
    """El primer frame del video: el que dejó el índice (f1.jpg) o uno nuevo con ffmpeg."""
    if meta and meta.frames_dir:
        f = os.path.join(meta.frames_dir, "f1.jpg")
        if os.path.exists(f):
            with open(f, "rb") as fh:
                return base64.b64encode(fh.read()).decode()
    return await _frame_b64(m.media_ruta, 1.0)


async def _frame_final_b64(m, meta) -> str | None:
    """El último frame del video: el del índice (f3.jpg) o uno nuevo con ffmpeg a 1,5 s del final."""
    if meta and meta.frames_dir:
        f = os.path.join(meta.frames_dir, "f3.jpg")
        if os.path.exists(f):
            with open(f, "rb") as fh:
                return base64.b64encode(fh.read()).decode()
    dur = (meta.duracion_s if meta and meta.duracion_s else None) or await _ffprobe_duracion(m.media_ruta)
    return await _frame_b64(m.media_ruta, max((dur or 2) - 1.5, 0.5))


def _fecha_cuadra(reg, m, meta) -> bool:
    """¿El video es del día del caso (±1 día)? Por la fecha del letrero o, si no, la de subida."""
    dias = [d for d in (_parse_fecha(reg.fecha_inicio), _parse_fecha(reg.fecha_fin)) if d]
    if not dias:
        return True
    f = None
    if meta and meta.overlay_fecha:
        try:
            f = datetime.strptime(meta.overlay_fecha, "%Y-%m-%d")
        except ValueError:
            f = None
    if f is None and m.enviado_en:
        f = _local(m.enviado_en).replace(hour=0, minute=0, second=0, microsecond=0)
    return f is None or any(abs((f - d).days) <= 1 for d in dias)


async def _pdls_del_periodo(db: AsyncSession, reg) -> set:
    """R##### de todas las órdenes del periodo: para reconocer cuando un audio dicta OTRO PDL."""
    r = await db.execute(select(AnsRegistro.pdl).where(AnsRegistro.periodo == reg.periodo,
                                                       AnsRegistro.entorno == reg.entorno))
    return {x for x in (letrero.rnum(p) for (p,) in r.all()) if x}


def _validar_atribucion(reg, ia: dict, videos_ia: list, conocidos: set, eventos_caso: int = 0,
                        eventos: list | None = None) -> None:
    """VALIDACIÓN DETERMINISTA de lo que la IA atribuyó (auditoría 2026-09-22).
    La IA propone; estas reglas mandan:
      · letrero de OTRA orden → NO (no debería ni haber llegado, pero por si acaso);
      · letrero de ESTA orden y fecha del caso → SI;
      · SI sin código en el letrero: si el audio dicta OTRO PDL del periodo → NO; si además no
        hay amarre fuerte, el audio no dicta este PDL y la IA tampoco leyó el código → DUDA.
    Después se rehacen tramos, línea de tiempo y anexos con los videos que quedaron en SI,
    y si no queda ninguno la máquina retira su veredicto. Todo queda en `ia.validacion`."""
    import re as _re
    info = {v["msg_id"]: v for v in videos_ia}
    rnum_caso = letrero.rnum(reg.pdl)
    ajustes = []
    for v in ia.get("videos") or []:
        vi = info.get(v.get("msg_id"))
        if not vi:
            continue
        clase, cod = vi.get("clase"), vi.get("codigo_letrero")
        trans = vi.get("transcripcion") or ""
        antes = v.get("del_caso")
        if clase == "ajeno" and antes != "NO":
            v["del_caso"] = "NO"
            ajustes.append(f"video {v['msg_id']}: el letrero dice {cod} (otra orden) → NO (la IA decía {antes})")
        elif clase == "propio" and antes != "SI" and vi.get("fecha_ok"):
            v["del_caso"] = "SI"
            ajustes.append(f"video {v['msg_id']}: el letrero dice {cod} = esta orden → SI (la IA decía {antes})")
        elif antes == "SI" and clase != "propio":
            otro = letrero.audio_menciona_otro(trans, rnum_caso, conocidos)
            propio_audio = letrero.audio_menciona(trans, rnum_caso)
            leyo = letrero.codigo_principal(f"{v.get('pdl_overlay') or ''} {v.get('overlay') or ''}") == rnum_caso
            if otro and not propio_audio and not leyo:
                v["del_caso"] = "NO"
                ajustes.append(f"video {v['msg_id']}: sin código en el letrero y el audio dicta {otro} (otro PDL del periodo) → NO")
            elif not vi.get("amarre") and not propio_audio and not leyo:
                v["del_caso"] = "DUDA"
                ajustes.append(f"video {v['msg_id']}: sin código en el letrero, sin amarre y el audio no dicta {rnum_caso} → DUDA")
    # Videos PROPIOS (letrero = orden, en fecha) que la IA ni siquiera listó: entran como SI
    # igual — la atribución es del código, no de la IA (auditoría 22-09: dos videos propios
    # se le pasaron a Llama).
    listados = {v.get("msg_id") for v in ia.get("videos") or []}
    for vi in videos_ia:
        if vi.get("clase") == "propio" and vi.get("fecha_ok") and vi["msg_id"] not in listados:
            ia.setdefault("videos", []).append({
                "msg_id": vi["msg_id"], "overlay": vi.get("letrero"), "hora_overlay": None,
                "pdl_overlay": vi.get("codigo_letrero"), "del_caso": "SI",
                "que_se_ve": "(la IA no describió este video; atribuido al caso por el código del letrero)",
                "que_se_oye": (vi.get("transcripcion") or "")[:300] or None})
            ajustes.append(f"video {vi['msg_id']}: letrero {vi.get('codigo_letrero')} = esta orden y la IA no lo listó → SI")
    si = {v["msg_id"] for v in ia.get("videos") or [] if v.get("del_caso") == "SI"}
    no_si = {v["msg_id"] for v in ia.get("videos") or [] if v.get("del_caso") != "SI"}
    # RELOJ DEL VIDEO (auditoría 2026-10-01: «reloj 09:12» en un video grabado 09:44 —
    # la hora la había DICHO el líder en el audio). La hora de un video es la de su letrero,
    # leída por el sistema con dos lecturas que cuadran; lo que diga la IA no la reemplaza. Y en
    # la línea de tiempo, un hecho cuya hora cae fuera del lapso grabado es algo DICHO en el audio.
    grabados = {}
    for v in ia.get("videos") or []:
        vi = info.get(v.get("msg_id")) or {}
        g = vi.get("grabado")
        v["grabado"] = g
        if v.get("del_caso") == "SI":
            grabados[v["msg_id"]] = g
        if g:
            h_ia, ini, fin = _hhmm_a_min(v.get("hora_overlay")), _hhmm_a_min(g["inicio"]), _hhmm_a_min(g["fin"])
            if h_ia is not None and not (ini - 2 <= h_ia <= max(fin, ini) + 2):
                ajustes.append(f"video {v['msg_id']}: la IA puso reloj {v['hora_overlay']}; el letrero marca "
                               f"{g['inicio'][:5]}–{g['fin'][:5]} → corregido")
            v["hora_overlay"] = g["inicio"][:5]
        elif v.get("del_caso") == "SI" and not v.get("hora_overlay"):
            ajustes.append(f"video {v['msg_id']}: no se pudo leer la hora del letrero (subido "
                           f"{vi.get('hora_subida') or '¿?'}); las horas que aporta salen del audio — verifícalas en el video")
    for mid, horas in letrero.marcar_dichos(ia.get("linea_tiempo") or [], grabados).items():
        g = grabados[mid]
        ajustes.append(f"video {mid}: se grabó {g['inicio'][:5]}–{g['fin'][:5]} (reloj del letrero); la(s) hora(s) "
                       f"{', '.join(sorted(horas))} las DICE el audio — son declaración, no el reloj del video")
    # Sin videos del caso NI mensajes de su cuadrilla no hay evidencia atribuible: la máquina no
    # propone veredicto ni tramos (lo que haya en el grupo es de otras cuadrillas).
    if not si and eventos_caso == 0 and (ia.get("excluible_sugerido") or ia.get("tramos_justificados")):
        ia["veredicto_ia"] = ia.get("excluible_sugerido")
        ia["excluible_sugerido"] = None
        ia["confianza"] = "baja"
        ia["tramos_justificados"] = []
        ia["linea_tiempo"] = []
        ia["observacion"] = ""
        ia["resumen"] = ("[VALIDACIÓN: SIN EVIDENCIA ATRIBUIBLE AL CASO — NI VIDEOS CON SU CÓDIGO NI MENSAJES DE SU "
                         f"CUADRILLA; LA IA PROPONÍA {ia['veredicto_ia'] or 'TRAMOS'} CON MATERIAL DE OTRAS CUADRILLAS] "
                         + (ia.get("resumen") or ""))
        ajustes.append(f"sin videos del caso ni mensajes de su cuadrilla: se retiran el veredicto "
                       f"{ia['veredicto_ia'] or ''} y los tramos de la IA".replace("  ", " "))

    def _cita_no_si(texto: str) -> bool:
        ids = {int(x) for x in _re.findall(r"msg[_ ]?id[= ]?(\d+)", texto or "", _re.I)}
        return bool(ids & no_si) and not (ids & si)

    tramos = ia.get("tramos_justificados") or []
    ia["tramos_justificados"] = [t for t in tramos if not _cita_no_si(t.get("evidencia") or "")]
    if len(ia["tramos_justificados"]) != len(tramos):
        ajustes.append(f"se retiraron {len(tramos) - len(ia['tramos_justificados'])} tramo(s) sustentados en videos que no son del caso")

    # ANCLAS: un tramo que no cita un video del caso tiene que salir de horas que el caso sí
    # tiene (mensajes de SU cuadrilla, apertura/cierre planeados y reales, relojes de sus
    # videos). Un tramo anclado en un mensaje de OTRA cuadrilla («17:44 el centro de
    # control no contesta», escrito por la cuadrilla de otra orden) se retira.
    anclas = set()
    for h in (reg.hora_inicio, reg.hora_fin, _hhmm_de(reg.maniobra_inicio), _hhmm_de(reg.maniobra_fin)):
        mm = _hhmm_a_min(h)
        if mm is not None:
            anclas.add(mm)
    for e in eventos or []:
        mm = _re.match(r"\s*(\d{1,2}:\d{2}) ", e or "")
        if mm and "[CUADRILLA DEL CASO]" in e:
            anclas.add(_hhmm_a_min(mm.group(1)))
            # …y las horas que su cuadrilla ESCRIBE en el mensaje: la plantilla oficial trae
            # llegada, llamada al C.C., primera respuesta (2026-10-01: el tramo 11:20→11:28
            # de la demora del C.C. se retiraba porque solo valía la hora de envío, 17:42)
            for h in _re.findall(r"(?<!\d)(\d{1,2}[:.]\d{2})(?!\d)", e[mm.end():]):
                hm = _hhmm_a_min(h.replace(".", ":"))
                if hm is not None and hm < 24 * 60:
                    anclas.add(hm)
    for v in ia.get("videos") or []:
        if v.get("del_caso") == "SI":
            vi = info.get(v.get("msg_id")) or {}
            for h in (v.get("hora_overlay"), vi.get("hora_subida")):
                mm = _hhmm_a_min(h)
                if mm is not None:
                    anclas.add(mm)
                    anclas.add(mm + int((vi.get("duracion") or 0) // 60) + 1)

    def _anclado(hhmm) -> bool:
        mm = _hhmm_a_min(hhmm)
        return mm is not None and any(abs(mm - a) <= 3 for a in anclas)

    con_ancla = []
    for t in ia["tramos_justificados"]:
        if _re.search(r"msg[_ ]?id", t.get("evidencia") or "", _re.I) or (_anclado(t.get("desde")) and _anclado(t.get("hasta"))):
            con_ancla.append(t)
        else:
            ajustes.append(f"tramo {t.get('desde')}→{t.get('hasta')} ({t.get('evidencia')}): sus horas no salen de "
                           f"evidencia del caso (videos propios, mensajes de su cuadrilla o la maniobra) → retirado")
    ia["tramos_justificados"] = con_ancla
    if not si and not con_ancla and ia.get("excluible_sugerido") and not ia.get("veredicto_ia"):
        ia["veredicto_ia"] = ia.get("excluible_sugerido")
        ia["excluible_sugerido"] = None
        ia["confianza"] = "baja"
        ia["resumen"] = ("[VALIDACIÓN: SIN VIDEOS DEL CASO NI TRAMOS SUSTENTADOS — LA IA PROPONÍA "
                         f"{ia['veredicto_ia']} SIN EVIDENCIA ATRIBUIBLE] " + (ia.get("resumen") or ""))
        ia["observacion_ia"] = ia.get("observacion")
        ia["observacion"] = ""                  # nada va a la celda sin evidencia del caso
        ia["observacion_favorable"] = None
        ajustes.append(f"sin videos del caso ni tramos sustentados: se retira el veredicto {ia['veredicto_ia']} de la IA")
    lt = ia.get("linea_tiempo") or []
    ia["linea_tiempo"] = [x for x in lt if not _cita_no_si(f"{x.get('fuente') or ''} {x.get('hecho') or ''}")]
    # PAQUETE PARA ENEL (14 oct 2026): la frase de tiempos la cierra el código.
    ia["observacion"] = claude._cuadrar_tiempos(claude._cuadrar_anexos(ia.get("observacion") or "", len(si)))
    if not si and ia.get("excluible_sugerido") and not ia.get("veredicto_ia"):
        ia["veredicto_ia"] = ia.get("excluible_sugerido")
        ia["excluible_sugerido"] = None
        ia["confianza"] = "baja"
        ia["resumen"] = ("[VALIDACIÓN: NINGÚN VIDEO QUEDÓ ATRIBUIDO AL CASO — LA IA PROPONÍA "
                         f"{ia['veredicto_ia']} CON MATERIAL DE OTRO PDL] " + (ia.get("resumen") or ""))
        ajustes.append(f"sin videos del caso: se retira el veredicto {ia['veredicto_ia']} de la IA")
    ia["validacion"] = ajustes
    if ajustes:
        log.info("validación reg %s: %s", reg.id, " | ".join(ajustes))


def _hhmm_a_min(s):
    """Minutos del día de un «HH:MM», «HH:MM:SS», «16/09/2026 12:10» o «2026-09-16 12:10» (la
    IA a veces antepone la fecha; un caso perdió su tramo del stop work por eso, 2026-09-23)."""
    import re as _re
    m = _re.match(r"\s*(\d{1,2}):(\d{2})", s or "") or _re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*$", s or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _hhmm_de(s):
    """El «HH:MM» final de un texto tipo «21/08/2026 14:35» (o el texto tal cual)."""
    import re as _re
    m = _re.search(r"(\d{1,2}:\d{2})\s*$", (s or "").strip())
    return m.group(1) if m else None


def _calcular_cobertura(reg, ia: dict) -> dict:
    """El ANÁLISIS RÁPIDO de tiempos (auditoría 2026-08-24): la IA declara los
    tramos [desde, hasta] que la evidencia demuestra; LA SUMA LA HACE ESTE CÓDIGO —
    fusiona solapados, suma por gap y compara contra el desfase real con tolerancia de
    10 min de apertura y 10 de cierre. A la IA no se le confía aritmética (un caso
    quedó justificado con franjas horarias que no sumaban el desfase)."""
    gaps = {}
    # Límites LÓGICOS de cada gap (auditoría 2026-09-22: un tramo 08:17→14:04 «justificó» una
    # apertura tardía de las 07:49): la causa de una APERTURA tardía ocurre ANTES de la
    # apertura real; la de un CIERRE tardío, ENTRE la apertura real y el cierre real. Lo que
    # cae fuera se recorta o se descarta, y queda anotado.
    ap_real = _hhmm_a_min(_hhmm_de(reg.maniobra_inicio))
    ci_real = _hhmm_a_min(_hhmm_de(reg.maniobra_fin))
    if ap_real is not None and ci_real is not None and ci_real < ap_real:
        ci_real += 24 * 60                      # cierre pasada la medianoche
    limites = {"apertura": (None, ap_real), "cierre": (ap_real, ci_real)}
    for gap, (fp, hp, real) in {
            "apertura": (reg.fecha_inicio, reg.hora_inicio, reg.maniobra_inicio),
            "cierre": (reg.fecha_fin, reg.hora_fin, reg.maniobra_fin)}.items():
        req = _desfase_min(fp, hp, real)
        if req is None or req <= 0:
            continue
        gaps[gap] = {"requerido": req,
                     "ventana": {"plan": _hhmm_de(hp) or (hp or "").strip(),
                                 "real": _hhmm_de(real)},
                     "tramos": [], "descartados": [], "justificado": 0}
    for t in (ia.get("tramos_justificados") or []):
        g = gaps.get(t.get("gap"))
        d, h = _hhmm_a_min(t.get("desde")), _hhmm_a_min(t.get("hasta"))
        if not g or d is None or h is None:
            continue
        if h < d:                               # cruza medianoche
            h += 24 * 60
        lo, hi = limites.get(t.get("gap"), (None, None))
        d0, h0 = d, h
        if lo is not None and d < lo:
            d = lo
        if hi is not None and h > hi:
            h = hi
        if h <= d:
            g["descartados"].append({"desde": t.get("desde"), "hasta": t.get("hasta"),
                                     "evidencia": t.get("evidencia") or "",
                                     "motivo": ("ocurre después de la apertura real: no explica la apertura tardía"
                                                if t.get("gap") == "apertura" else
                                                "cae fuera del lapso entre la apertura real y el cierre real")})
            continue
        g["tramos"].append({"desde": t.get("desde"), "hasta": t.get("hasta"),
                            "minutos": h - d, "evidencia": t.get("evidencia") or "",
                            "recortado": (d != d0 or h != h0), "_i": d, "_f": h})
    for g in gaps.values():
        # Fusión de solapados: el mismo lapso grabado dos veces no justifica el doble
        total, cur_i, cur_f = 0, None, None
        for i, f in sorted((t["_i"], t["_f"]) for t in g["tramos"]):
            if cur_f is None or i > cur_f:
                if cur_f is not None:
                    total += cur_f - cur_i
                cur_i, cur_f = i, f
            else:
                cur_f = max(cur_f, f)
        if cur_f is not None:
            total += cur_f - cur_i
        g["justificado"] = total
        g["faltan"] = max(0, g["requerido"] - total)
        g["cubierto"] = total >= g["requerido"] - TOLERANCIA_MIN
        for t in g["tramos"]:
            t.pop("_i"), t.pop("_f")
    aplicable = bool(gaps)
    return {"tolerancia_min": TOLERANCIA_MIN, "gaps": gaps, "aplicable": aplicable,
            "cubierto": aplicable and all(g["cubierto"] for g in gaps.values())}


# ── LO QUE JUSTIFICA Y LO QUE SE ADJUNTA (14 oct 2026) ──────────────────────
# Quien expone ante ENEL deduce sobre la marcha: encima de la observación necesita ver, por gap con
# desfase, el tramo que lo justifica, los hechos de ese tramo con su fuente y qué archivos lo
# soportan. Los videos del caso que caen FUERA de los tramos (el trancón de la mañana cuando la
# apertura se cumplió y el desfase fue de cierre) no justifican nada: quedan fuera del paquete y de
# la frase «SE ANEXAN». Puro código: se calcula al persistir y en vivo para los análisis viejos.
MARGEN_TRAMO_MIN = 15


def _min_a_hhmm(m):
    return None if m is None else f"{(int(m) // 60) % 24:02d}:{int(m) % 60:02d}"


def _ventana_video(v: dict, res: dict | None) -> tuple:
    """[inicio, fin] en minutos del día de un video del caso (reloj del letrero; si solo hay hora
    de inicio, + la duración del archivo). (None, None) si no tiene hora."""
    g = v.get("grabado") or {}
    ini = _hhmm_a_min(g.get("inicio")) if g.get("inicio") else _hhmm_a_min(v.get("hora_overlay"))
    if ini is None:
        return None, None
    fin = _hhmm_a_min(g.get("fin")) if g.get("fin") else None
    if fin is None:
        dur = next((x.get("duracion") for x in (res or {}).get("videos") or []
                    if str(x.get("msg_id")) == str(v.get("msg_id"))), None)
        try:
            fin = ini + max(1, int(round(float(dur) / 60))) if dur else ini + 1
        except (TypeError, ValueError):
            fin = ini + 1
    if fin < ini:
        fin += 24 * 60
    return ini, fin


def resumen_tiempos(reg, ia: dict, res: dict | None = None, docs: dict | None = None,
                    evidencias: list | None = None) -> dict:
    """Por gap con desfase: sus tramos justificados con los hechos y los videos que caen dentro;
    aparte, los videos del caso fuera de todo tramo (contexto) y los sin hora. Marca en
    `ia["videos"]` `justifica` (True / False / None = sin hora o sin tramos) y `gap`.
    `anexar` = cuántos videos van al paquete y a la frase «SE ANEXAN»."""
    import re as _re
    cov = ia.get("cobertura_calc") or _calcular_cobertura(reg, ia)
    tramos = []
    for gap, g in (cov.get("gaps") or {}).items():
        for t in g.get("tramos") or []:
            i, f = _hhmm_a_min(t.get("desde")), _hhmm_a_min(t.get("hasta"))
            if i is None or f is None:
                continue
            if f < i:
                f += 24 * 60
            # VIDEOS DE LA CELDA EN EL BLOQUE (14 oct 2026): dos tramos del mismo gap que se solapan ≥90 % son el
            # mismo lapso contado por dos canales (plantilla 09:16–12:11 y «Video de 09:40» 09:17–12:11): uno solo.
            dup = next((x for x in tramos if x["gap"] == gap
                        and min(x["_f"], f) - max(x["_i"], i) >= 0.9 * max(1, min(x["_f"] - x["_i"], f - i))), None)
            if dup:
                ev = (t.get("evidencia") or "").strip()
                if ev and ev not in dup["evidencia"]:
                    dup["evidencia"] += " · También: " + ev
                continue
            tramos.append({"gap": gap, "_i": i, "_f": f, "desde": t.get("desde"), "hasta": t.get("hasta"),
                           "minutos": t.get("minutos"), "evidencia": t.get("evidencia") or "",
                           "hechos": [], "videos": []})
    con_tramos = bool(tramos)
    justifican, contexto, sin_hora = [], [], []
    for v in ia.get("videos") or []:
        if v.get("del_caso") != "SI":
            continue
        ini, fin = _ventana_video(v, res)
        mid = str(v.get("msg_id"))
        d = {"msg_id": v.get("msg_id"), "hora": _min_a_hhmm(ini), "hora_fin": _min_a_hhmm(fin),
             "que_se_ve": (v.get("que_se_ve") or "")[:160]}
        hit = None
        for t in tramos:
            citado = _re.search(r"msg(?:_id)?\s*[=: ]?\s*" + _re.escape(mid) + r"\b", t["evidencia"], _re.I)
            if citado or (ini is not None and ini <= t["_f"] + MARGEN_TRAMO_MIN and fin >= t["_i"] - MARGEN_TRAMO_MIN):
                hit = t
                break
        if not con_tramos:
            v["justifica"], v["gap"] = None, None
            justifican.append({**d, "gap": None})
        elif hit:
            v["justifica"], v["gap"] = True, hit["gap"]
            hit["videos"].append(d)
            justifican.append({**d, "gap": hit["gap"]})
        elif ini is None:
            v["justifica"], v["gap"] = None, None
            sin_hora.append(d)
        else:
            v["justifica"], v["gap"] = False, None
            contexto.append({**d, "motivo": "grabado fuera de los tramos que justifican el desfase"})
    # Los VIDEOS DE LA CELDA que no vienen del analista (chat directo, subidos a mano) ya traen hora de
    # letrero: se ubican en los tramos igual que los del análisis y cuentan para el paquete y la solidez.
    for ev in evidencias or []:
        h = hhmm_ev = ev.get("hora")
        ini = _hhmm_a_min(h) if h else None
        fin = _hhmm_a_min(ev.get("hora_fin")) if ev.get("hora_fin") else None
        if ini is not None and (fin is None or fin < ini):
            fin = ini + 1
        d = {"msg_id": None, "evidencia_id": ev.get("id"), "hora": hhmm_ev, "hora_fin": ev.get("hora_fin") or hhmm_ev,
             "que_se_ve": (ev.get("que_se_ve") or "")[:160]}
        hit = None
        if ini is not None:
            hit = next((t for t in tramos if ini <= t["_f"] + MARGEN_TRAMO_MIN and fin >= t["_i"] - MARGEN_TRAMO_MIN), None)
        if not con_tramos:
            justifican.append({**d, "gap": None})
        elif hit:
            hit["videos"].append(d)
            justifican.append({**d, "gap": hit["gap"]})
        elif ini is None:
            sin_hora.append(d)
        else:
            contexto.append({**d, "motivo": "grabado fuera de los tramos que justifican el desfase"})
    hechos_ctx = []
    for e in ia.get("linea_tiempo") or []:
        h = _hhmm_a_min(e.get("hora"))
        hit = next((t for t in tramos
                    if h is not None and t["_i"] - MARGEN_TRAMO_MIN <= h <= t["_f"] + MARGEN_TRAMO_MIN), None)
        (hit["hechos"] if hit else hechos_ctx).append(e)
    # LÓGICA DEL TRAMO Y CUENTA HECHA (14 oct 2026): quien lee bajo presión ve horas, no motivos. Cada tramo
    # dice POR QUÉ prueba el desfase según dónde cae (una espera de la mañana corre toda la jornada y por eso
    # prueba un cierre tardío), y cada gap trae la cuenta explícita para que nadie tenga que sumar en la reunión.
    # CAPTURA CUANDO EL TIEMPO SALE DE LA PLANTILLA (14 oct 2026): si la evidencia del tramo cita la plantilla o
    # mensajes del grupo, el tiempo sale del chat → la captura tiene que ir al paquete (aunque haya videos).
    for t in tramos:
        ev_txt = t.get("evidencia") or ""
        t["soporte_chat"] = bool(_re.search(r"plantilla|mensaje", ev_txt, _re.I))
        # CAPTURA NOMBRADA EN CORTO (14 oct 2026): sin las horas del propio tramo (son horas dichas dentro de la plantilla)
        _bordes = {str(t.get("desde") or "")[-5:], str(t.get("hasta") or "")[-5:]}
        t["horas_chat"] = sorted({f"{int(h):02d}:{m}" for h, m in _re.findall(r"\b(\d{1,2}):(\d{2})\b", ev_txt)} - _bordes) if t["soporte_chat"] else []
    for t in tramos:
        g = (cov.get("gaps") or {}).get(t["gap"]) or {}
        plan = _hhmm_a_min((g.get("ventana") or {}).get("plan"))
        mins = t.get("minutos") or (t["_f"] - t["_i"])
        if plan is None:
            t["logica"] = None
        elif t["gap"] == "cierre":
            if t["_f"] <= plan - 60:
                t["logica"] = (f"Este lapso ({t['desde']}–{t['hasta']}) termina mucho antes del cierre planeado ({(g.get('ventana') or {}).get('plan')}): "
                               f"la espera de {mins} min corrió toda la jornada hacia adelante, y por eso prueba el cierre tardío aunque sus horas sean de la mañana.")
            elif t["_i"] >= plan - 15:
                t["logica"] = "Este lapso ocurre en la hora del cierre: explica directamente los minutos de demora."
            else:
                t["logica"] = f"Este lapso cae dentro de la jornada: retrasó el trabajo {mins} min y con él el cierre."
        else:
            if t["_f"] <= plan + 15:
                t["logica"] = "Este lapso ocurre antes de la apertura planeada: explica directamente la apertura tardía."
            else:
                t["logica"] = f"Este lapso va más allá de la apertura planeada: la apertura no pudo darse hasta que terminó ({t['hasta']})."
    gaps_out = []
    for gap, g in (cov.get("gaps") or {}).items():
        gaps_out.append({"gap": gap, "plan": (g.get("ventana") or {}).get("plan"),
                         "real": (g.get("ventana") or {}).get("real"),
                         "requerido": g.get("requerido"), "justificado": g.get("justificado"),
                         "cubierto": g.get("cubierto"), "faltan": g.get("faltan"),
                         "cuenta": (f"{g.get('justificado')} min justificados frente a {g.get('requerido')} min de desfase de {gap}: "
                                    + ("cubre con holgura" if (g.get("justificado") or 0) >= (g.get("requerido") or 0) + 10
                                       else "cubre" if g.get("cubierto") else f"faltan {g.get('faltan')} min")),
                         "tramos": [{k: t[k] for k in t if not k.startswith("_")} for t in tramos if t["gap"] == gap]})
    # EVIDENCIA NO SÓLIDA (14 oct 2026): ENEL exige correos y pruebas puntuales. Un tramo que se sostiene
    # solo en mensajes de WhatsApp y la plantilla del líder es contexto nuestro, no prueba: «no_solida».
    # Con un video dentro del tramo o un correo/PDF adjunto a la celda es «solida»; con solo una imagen
    # o captura del chat adjunta, «parcial». `docs` = {"correo": n, "pdf": n, "imagen": n} de la celda.
    n_doc = sum((docs or {}).get(k, 0) for k in ("correo", "pdf"))
    n_img = (docs or {}).get("imagen", 0)
    for g in gaps_out:
        if not g["tramos"]:
            g["solidez"] = "n/a"
        elif any(t["videos"] for t in g["tramos"]) or n_doc:
            g["solidez"] = "solida"
        elif n_img:
            g["solidez"] = "parcial"
        else:
            g["solidez"] = "no_solida"
    niveles = [g["solidez"] for g in gaps_out if g["solidez"] != "n/a"]
    solidez = ("no_solida" if "no_solida" in niveles else "parcial" if "parcial" in niveles
               else "solida" if niveles else "n/a")
    return {"con_tramos": con_tramos, "gaps": gaps_out, "videos_justifican": justifican,
            "videos_sin_hora": sin_hora, "videos_contexto": contexto, "hechos_contexto": hechos_ctx,
            "anexar": len(justifican) + len(sin_hora), "tolerancia_min": cov.get("tolerancia_min", TOLERANCIA_MIN),
            "solo_mensajes": con_tramos and not justifican and not sin_hora,
            "solidez": solidez, "docs": docs or {},
            "necesita_captura": any(t.get("soporte_chat") for t in tramos),
            # COBERTURA INCOMPLETA (14 oct 2026): solidez (calidad de la prueba) ≠ cobertura (si tapa el desfase)
            "cobertura_completa": all(g.get("cubierto") for g in gaps_out) if gaps_out else True,
            "gaps_sin_cubrir": [{"gap": g["gap"], "faltan": g.get("faltan"), "sin_tramo": not g["tramos"]}
                                for g in gaps_out if not g.get("cubierto")]}


def cuadrar_captura(obs: str, horas: list) -> str:
    """CAPTURA AUTOMÁTICA DEL CHAT (14 oct 2026): si el tramo solo tiene mensajes, la captura del chat se
    adjunta sola y la observación lo dice — «SE ANEXA CAPTURA DEL CHAT DEL GRUPO (MENSAJES 12:15, 18:29).»"""
    if not obs or "CAPTURA" in obs.upper():
        return obs
    o = obs.rstrip()
    if not o.endswith((".", ";")):
        o += "."
    h = ", ".join(horas[:4])
    return o + (f" SE ANEXA CAPTURA DEL CHAT DEL GRUPO (MENSAJES {h})." if h else " SE ANEXA CAPTURA DEL CHAT DEL GRUPO.")


# ── OBSERVACIÓN COMPUESTA POR CÓDIGO (14 oct 2026) ──
def _mayus(s) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", str(s or "")).strip().rstrip(".;").upper()


def soporte_fallback(evidencia: str) -> str:
    """El soporte de un tramo, solo por su nombre (sin horas internas): por palabras clave."""
    t = (evidencia or "").lower()
    partes = []
    if "plantilla" in t:
        partes.append("PLANTILLA DEL LÍDER")
    if "mensaje" in t:
        partes.append("MENSAJES DEL GRUPO")
    if "correo" in t:
        partes.append("CORREO DEL CENTRO DE CONTROL")
    if "llamada" in t:
        partes.append("REGISTRO DE LLAMADAS")
    if "pantallazo" in t:
        partes.append("PANTALLAZO")
    if not partes and "plataforma" in t:
        partes.append("REGISTRO DE LA PLATAFORMA")
    return " Y ".join(partes)


def _lista(items) -> str:
    items = [x for x in items if x]
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " Y " + items[-1]


def componer_observacion(reg, rt: dict, causa: str, soportes: dict | None = None, docs: dict | None = None,
                         captura: dict | None = None) -> str:
    """OBSERVACIÓN SIMPLE EN CUATRO FRASES (14 oct 2026) — para quien lee bajo presión: qué se retrasó y por qué;
    cuándo fue la espera (y que corrió el día); el soporte, por su nombre; y la cuenta en una línea.
    Un bloque por gap con tramos; los gaps sin tramo y sin cubrir quedan declarados."""
    frases = []
    for g in rt.get("gaps") or []:
        tr = g.get("tramos") or []
        if not tr:
            continue
        gap = g["gap"]
        que = "EL CIERRE" if gap == "cierre" else "LA APERTURA"
        req, just, faltan = g.get("requerido") or 0, g.get("justificado") or 0, g.get("faltan") or 0
        t0 = tr[0]
        mins = t0.get("minutos") or 0
        plan = _hhmm_a_min(g.get("plan"))
        ini, fin = _hhmm_a_min(t0.get("desde")), _hhmm_a_min(t0.get("hasta"))
        frases.append(f"{que} SE RETRASÓ {req} MIN POR {_mayus(causa)[:90] or 'CAUSA EXTERNA'}.")
        segun = ""
        if t0.get("soporte_chat"):
            nom = soporte_fallback(t0.get("evidencia") or "")
            nom = nom.split(" Y ")[0]
            segun = f" SEGÚN {('LA ' + nom) if nom.startswith('PLANTILLA') else ('LOS ' + nom if nom else 'EL CHAT')}"
        if gap == "cierre" and plan is not None and fin is not None and fin <= plan - 60:
            frases.append(f"LA ESPERA FUE DE {t0['desde']} A {t0['hasta']} ({mins} MIN){segun} Y CORRIÓ TODO EL DÍA.")
            de_la = "DE ESA ESPERA"
        elif gap == "cierre" and plan is not None and ini is not None and ini >= plan - 15:
            frases.append(f"LA DEMORA FUE A LA HORA DEL CIERRE, DE {t0['desde']} A {t0['hasta']} ({mins} MIN).")
            de_la = "DE ESA DEMORA"
        elif gap == "cierre":
            frases.append(f"LA ESPERA FUE DE {t0['desde']} A {t0['hasta']} ({mins} MIN) Y ATRASÓ TODO LO DEMÁS.")
            de_la = "DE ESA ESPERA"
        else:
            frases.append(f"LA DEMORA FUE ANTES DE ABRIR, DE {t0['desde']} A {t0['hasta']} ({mins} MIN).")
            de_la = "DE ESA DEMORA"
        sop = []
        chat_nombres, chat_horas = [], []
        for t in tr:
            for x in (soporte_fallback(t.get("evidencia") or "") or "").split(" Y "):
                if not x:
                    continue
                if t.get("soporte_chat") and x in ("PLANTILLA DEL LÍDER", "MENSAJES DEL GRUPO"):
                    if x not in chat_nombres:
                        chat_nombres.append(x)
                    chat_horas += [h for h in (t.get("horas_chat") or []) if h not in chat_horas]
                elif x not in sop:
                    sop.append(x)
        if chat_nombres:
            # el archivo del paquete donde está esa prueba: la captura del chat
            cortos = [{"PLANTILLA DEL LÍDER": "PLANTILLA", "MENSAJES DEL GRUPO": "MENSAJES"}.get(x, x) for x in chat_nombres]
            sop.insert(0, f"CAPTURA DEL CHAT ({' Y '.join(cortos)}" + (f", {_lista(sorted(chat_horas))}" if chat_horas else "") + ")")
        vids = sorted([v for t in tr for v in (t.get("videos") or [])], key=lambda v: v.get("hora") or "")
        if vids:
            n = len(vids)
            sop.append(f"{n} VIDEO{'S' if n > 1 else ''} {de_la} ({_lista([v.get('hora') for v in vids])})")
        if captura and captura.get("mensajes") and rt.get("solidez") == "no_solida" and not chat_nombres:
            sop.append("CAPTURA DEL CHAT")
        nc = (docs or {}).get("correo", 0)
        if nc:
            sop.append("CORREO DEL CENTRO DE CONTROL" if nc == 1 else f"{nc} CORREOS DEL CENTRO DE CONTROL")
        if sop:
            frases.append(f"SOPORTE: {_lista(sop)}.")
        if just >= req:
            frases.append(f"{just} MIN JUSTIFICADOS CUBREN LOS {req} MIN DE RETRASO.")
        elif g.get("cubierto"):
            frases.append(f"{just} MIN JUSTIFICADOS CUBREN LOS {req} MIN DE RETRASO (DENTRO DE LA TOLERANCIA).")
        else:
            frases.append(f"SOLO {just} DE LOS {req} MIN: FALTAN {faltan} MIN.")
    for g in rt.get("gaps") or []:
        if not g.get("tramos") and not g.get("cubierto"):
            que = "EL CIERRE" if g["gap"] == "cierre" else "LA APERTURA"
            frases.append(f"{que} SE RETRASÓ {g.get('requerido')} MIN SIN SOPORTE.")
    return " ".join(frases).strip()


def resumen_tiempos_txt(rt: dict, max_hechos: int = 8) -> str:
    """La misma línea de tiempo en texto plano, para el 00_RESUMEN_<orden>.txt del paquete."""
    out = []
    for g in rt.get("gaps") or []:
        estado = "CUBRE" if g.get("cubierto") else f"NO CUBRE (faltan {g.get('faltan')} min)"
        out.append(f"{(g.get('gap') or '').upper()}: planeado {g.get('plan')}, real {g.get('real')} → desfase "
                   f"{g.get('requerido')} min · justificado {g.get('justificado')} min · {estado}")
        if g.get("cuenta"):
            out.append(f"  Cuenta: {g['cuenta']}.")
        for t in g.get("tramos") or []:
            out.append(f"  Tramo {t.get('desde')}–{t.get('hasta')} ({t.get('minutos')} min): {t.get('evidencia')}")
            if t.get("logica"):
                out.append(f"    Por qué prueba el desfase: {t['logica']}")
            hechos = t.get("hechos") or []
            for e in hechos[:max_hechos]:
                fuente = (e.get("fuente") or "") + (f" · {e['grupo']}" if e.get("grupo") else "")
                out.append(f"    {e.get('hora')}  {e.get('hecho')}  [{fuente}]")
            if len(hechos) > max_hechos:
                out.append(f"    (+{len(hechos) - max_hechos} hechos más en el análisis)")
            for v in t.get("videos") or []:
                lapso = v.get("hora") or "?"
                if v.get("hora_fin") and v.get("hora_fin") != v.get("hora"):
                    lapso += f"–{v['hora_fin']}"
                out.append(f"    Video {lapso}: {v.get('que_se_ve')}")
        if not g.get("tramos"):
            out.append(f"  (desfase dentro de la tolerancia de {rt.get('tolerancia_min')} min: no necesita tramo)"
                       if g.get("cubierto") else "  (sin tramos justificados para este gap)")
    if rt.get("solo_mensajes"):
        out.append("El soporte de los tramos son mensajes y la plantilla del grupo de WhatsApp (no hay archivo en el paquete).")
    if rt.get("solidez") == "no_solida":
        out.append("EVIDENCIA NO SÓLIDA: el tramo se sostiene solo en mensajes de WhatsApp y la plantilla del líder; "
                   "ENEL exige correos y pruebas puntuales.")
    if rt.get("gaps_sin_cubrir"):
        out.append("COBERTURA INCOMPLETA: " + "; ".join(
            f"{g['gap']} sin cubrir (faltan {g['faltan']} min{', sin tramo' if g.get('sin_tramo') else ''})"
            for g in rt["gaps_sin_cubrir"]) + ".")
    elif rt.get("solidez") == "parcial":
        out.append("EVIDENCIA PARCIAL: solo hay captura/pantallazo del chat adjunto; ENEL puede exigir el correo del Centro de Control.")
    if rt.get("videos_contexto"):
        out.append("Videos del caso que NO justifican el desfase (fuera del paquete): "
                   + ", ".join(v.get("hora") or "?" for v in rt["videos_contexto"]))
    return "\n".join(out)


# ── ADJUNTOS DEL CHAT, UBICADOS Y CONTESTADOS (14 oct 2026) ──────────────
async def reloj_video_directo(ruta: str, fecha_orden: str | None = None) -> dict | None:
    """Reloj del letrero de un video que llegó por el CHAT DIRECTO (sin índice OCR del bot): tres
    frames (inicio, mitad, final) leídos por visión. La visión confunde el día («3 sept», «6 oct»):
    valen las lecturas cuya fecha es la de la orden (o la más repetida), y con que dos cuadren a ±90 s
    —o haya una sola— se acepta. Devuelve {"fecha", "inicio", "fin", "fuente", "lecturas"} o None."""
    try:
        dur = await _ffprobe_duracion(ruta)
        segs = [1.0, dur / 2, max(dur - 1.0, 1.5)] if dur > 4 else [min(1.0, max(dur - 0.2, 0.1))]
        lect = []
        for i, sg in enumerate(segs):
            b64 = await _frame_b64(ruta, sg)
            if not b64:
                continue
            txt = await claude.leer_letrero_frame(b64)
            fh = letrero.fecha_hora(txt or "")
            if fh:
                lect.append({**fh, "seg": sg, "ini": letrero._seg_dia(fh["hora"]) - sg})
        if not lect:
            return None
        objetivo = None
        if fecha_orden:
            m = re.search(r"(\d{2})/(\d{2})/(\d{4})", fecha_orden)
            if m:
                objetivo = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        if objetivo and any(l["fecha"] == objetivo for l in lect):
            lect = [l for l in lect if l["fecha"] == objetivo]
        else:
            fechas = [l["fecha"] for l in lect]
            moda = max(set(fechas), key=fechas.count)
            lect = [l for l in lect if l["fecha"] == moda]
        lect.sort(key=lambda l: l["ini"])
        grupo = [lect[0]]
        for l in lect[1:]:
            if abs(l["ini"] - grupo[0]["ini"]) <= 90:
                grupo.append(l)
        ini = sorted(l["ini"] for l in grupo)[len(grupo) // 2]
        return {"fecha": grupo[0]["fecha"], "inicio": letrero._hhmmss(ini), "fin": letrero._hhmmss(ini + dur),
                "fuente": "vision", "lecturas": len(grupo)}
    except Exception as ex:  # noqa: BLE001 — sin reloj el flujo sigue
        log.warning("reloj del video directo %s: %s", ruta, ex)
        return None


def video_vs_desfase(reg, res: dict | None, hora: str | None, hora_fin: str | None = None) -> dict:
    """Un video del chat frente al desfase de la orden: «justifica» (cae en un tramo justificado del
    análisis), «en_ventana» (cae en el lapso lógico de un gap con desfase, sin tramo), «fuera» o
    «sin_hora». `texto` es lo que se le dice al clasificador (y a la persona)."""
    ini = _hhmm_a_min(hora)
    if ini is None:
        return {"veredicto": "sin_hora", "gap": None, "texto": "sin hora legible en el letrero: no se puede ubicar en el desfase"}
    fin = _hhmm_a_min(hora_fin) if hora_fin else None
    if fin is None or fin < ini:
        fin = ini + 1
    ia = (res or {}).get("ia") or {}
    try:
        cov = ia.get("cobertura_calc") or _calcular_cobertura(reg, ia)
    except Exception:  # noqa: BLE001
        cov = {"gaps": {}}
    gaps = cov.get("gaps") or {}
    ap_real = _hhmm_a_min(_hhmm_de(reg.maniobra_inicio))
    ci_real = _hhmm_a_min(_hhmm_de(reg.maniobra_fin))
    if ap_real is not None and ci_real is not None and ci_real < ap_real:
        ci_real += 1440
    for gap, g in gaps.items():
        for t in g.get("tramos") or []:
            i, f = _hhmm_a_min(t.get("desde")), _hhmm_a_min(t.get("hasta"))
            if i is None or f is None:
                continue
            if f < i:
                f += 1440
            if ini <= f + MARGEN_TRAMO_MIN and fin >= i - MARGEN_TRAMO_MIN:
                return {"veredicto": "justifica", "gap": gap,
                        "texto": f"grabado {hora}, dentro del lapso {t.get('desde')}–{t.get('hasta')} que justifica el {gap}: SÍ sirve"}
    for gap, g in gaps.items():
        lo, hi = ((None, ap_real) if gap == "apertura" else (ap_real, ci_real))
        if (lo is None or fin >= lo) and (hi is None or ini <= hi):
            v = g.get("ventana") or {}
            return {"veredicto": "en_ventana", "gap": gap,
                    "texto": f"grabado {hora}, dentro del lapso del desfase de {gap} (planeado {v.get('plan')}, real {v.get('real')}) "
                             f"pero sin un tramo que lo justifique todavía: sirve si muestra la causa con horas"}
    desc = ", ".join(f"{gap} (planeado {(g.get('ventana') or {}).get('plan')}, real {(g.get('ventana') or {}).get('real')})"
                     for gap, g in gaps.items()) or "ninguno"
    return {"veredicto": "fuera", "gap": None,
            "texto": f"grabado {hora}, fuera del lapso del desfase ({desc}): NO sirve para este caso"}


# ── ANÁLISIS AUTOMÁTICO DE LOS PENDIENTES (requisito 2026-09-07) ───────────────────
# Hasta hoy el análisis solo corría a mano («Analizar caso» con un PDL): al cargar la STM de
# septiembre el tab quedó vacío con 7 casos por auditar. Ahora la máquina sabe cuáles son
# (3.2 A con tiempos NO CUMPLE sin decisión; 3.2 B anulados sin decisión) y los corre sola:
# al cargar una STM, desde el botón «Analizar pendientes» y en el ciclo nocturno.
async def casos_pendientes(db: AsyncSession, periodo: str, reintentar: bool = False) -> list:
    """[(reg, motivo)] del periodo sin análisis vigente (o con análisis fallido, si
    `reintentar`). Vigente = el más nuevo no descartado."""
    from app.api.ans import computar
    from app.db.ans_ctx import current_entorno
    r = await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo,
                                                   AnsRegistro.entorno == current_entorno())
                         .order_by(AnsRegistro.id))
    regs = r.scalars().all()
    if not regs:
        return []
    ra = await db.execute(select(WaAnalisis).where(WaAnalisis.reg_id.in_([x.id for x in regs]),
                                                   WaAnalisis.estado != "descartado")
                          .order_by(WaAnalisis.id.desc()))
    vigente = {}
    for a in ra.scalars().all():
        vigente.setdefault(a.reg_id, a)
    out = []
    for reg in regs:
        if _es_32b(reg):
            if (reg.excluible_anulado or "").strip():
                continue
            motivo = "3.2 B · orden anulada sin decisión"
        else:
            c = computar(reg)
            # La MISMA condición que hace parpadear la celda (ans._pend_campos): NO CUMPLE en
            # tiempos o en apertura. Si no, la tabla pide una decisión que el tab nunca trae.
            if not _no_cumple_32a(c) or (reg.excluible or "").strip():
                continue
            motivo = (f"3.2 A · tiempos NO CUMPLE (apertura {c.get('desfase_apertura')} min · "
                      f"cierre {c.get('desfase_cierre')} min)")
        a = vigente.get(reg.id)
        if a:
            try:
                ia = (json.loads(a.resultado or "{}").get("ia") or {})
            except ValueError:
                ia = {}
            if not (reintentar and ia.get("error")):
                continue
            motivo += f" · reintento (el anterior falló: {str(ia.get('error'))[:70]})"
        out.append((reg, motivo))
    return out


async def analizar_pendientes(periodo: str, reintentar: bool = False) -> list[dict]:
    """Corre `analizar_caso` sobre cada pendiente del periodo, cada uno en su propia
    sesión (va en segundo plano tras la carga de la STM o desde el botón; también lo llama
    el script nocturno). Un fallo (p. ej. API sin créditos) queda PERSISTIDO como análisis
    con error — visible en el tab, nunca un tab vacío sin explicación."""
    from app.db.database import AsyncSessionLocal
    hechos = []
    async with AsyncSessionLocal() as db:
        casos = [(reg.id, reg.pdl, motivo) for reg, motivo in await casos_pendientes(db, periodo, reintentar)]
    log.info("analizar_pendientes %s: %d caso(s)", periodo, len(casos))
    for reg_id, pdl, motivo in casos:
        async with AsyncSessionLocal() as db:
            reg = await db.get(AnsRegistro, reg_id)
            if not reg:
                continue
            try:
                a = await analizar_caso(db, reg)
                h = {"reg_id": reg_id, "pdl": pdl, "analisis_id": a.id, "motivo": motivo}
                # Circuito automático: sin material → se pide la evidencia por WhatsApp
                try:
                    from app.api.wa_auditoria import pedir_material_faltante
                    p = await pedir_material_faltante(db, a, reg)
                    if p:
                        h["pedido"] = p
                        log.info("  → pedido: %s", p)
                except Exception as e:                # noqa: BLE001 — el pedido nunca frena el ciclo
                    log.warning("  pedido automático falló para %s: %s", pdl, e)
                hechos.append(h)
                log.info("  ✓ %s → análisis #%s", pdl, a.id)
            except Exception as e:  # noqa: BLE001 — un caso roto no frena a los demás
                log.warning("  ✗ %s: %s", pdl, e)
                hechos.append({"reg_id": reg_id, "pdl": pdl, "error": str(e)[:200], "motivo": motivo})
                # Que el caso SE VEA en el tab con su error (2026-10-01: dos casos se
                # cayeron 3 veces y no aparecían en ningún lado; la tabla los pedía y el tab no).
                # Queda como análisis con error: el botón «Reintentar fallidos» lo recoge.
                try:
                    await db.rollback()
                    db.add(WaAnalisis(reg_id=reg_id, campo="excluible_anulado" if _es_32b(reg) else "excluible",
                                      estado="propuesto", resultado=json.dumps({
                                          "tipo": "3.2B" if _es_32b(reg) else "3.2A",
                                          "caso": {"pdl": reg.pdl, "zona": reg.zona, "fecha": reg.fecha_fin},
                                          "ia": {"error": f"el análisis se cayó: {str(e)[:300]}",
                                                 "resumen": "EL ANÁLISIS AUTOMÁTICO FALLÓ — REINTENTAR."}},
                                          ensure_ascii=False)))
                    await db.commit()
                except Exception as e2:  # noqa: BLE001
                    log.warning("  no se pudo dejar constancia del fallo de %s: %s", pdl, e2)
    return hechos


def _no_cumple_32a(c: dict) -> bool:
    """¿El 3.2 A pide decidir exclusión? Igual que ans._pend_campos (tiempos O apertura)."""
    nc = lambda v: str(v or "").upper() == "NO CUMPLE"
    return nc(c.get("cumple_tiempos")) or nc(c.get("cumple_apertura"))


async def _persistir(db: AsyncSession, reg, resultado: dict) -> WaAnalisis:
    # ── Post-proceso determinista (auditoría 2026-08-24) ──────────────────────────
    # 1. Cobertura calculada por código (no por la IA).
    # 2. Estado REVISIÓN: hay material del caso pero los tiempos no quedan dentro de
    #    la tolerancia → la máquina NO se compromete (excluible_sugerido queda vacío,
    #    posición provisional de no exclusión) y el SI/NO definitivo lo pone el ING.
    #    SIN excepción por causa interna: aunque la inclinación sea NO, el
    #    NO definitivo de un caso con material es del ING. Sin comentarios raros.
    ia = resultado.get("ia") or {}
    if resultado.get("tipo") == "3.2A" and not ia.get("error"):
        cov = _calcular_cobertura(reg, ia)
        ia["cobertura_calc"] = cov
        # La celda no puede decir más que la suma (auditoría 2026-10-01: «EL LAPSO
        # 09:08-09:12 JUSTIFICA LA TOTALIDAD DEL DESFASE DE 12 MIN» — eran 4 de 12, cubierto
        # solo por la tolerancia). No se reescribe el texto: se avisa para que el ING lo ajuste.
        import re as _re
        if _re.search(r"TOTALIDAD|TOTALMENTE|PLENAMENTE|POR COMPLETO|COMPLETAMENTE",
                      (ia.get("observacion") or "").upper()):
            for gap, g in cov["gaps"].items():
                if g["justificado"] < g["requerido"]:
                    ia.setdefault("validacion", []).append(
                        f"la observación afirma que el desfase queda justificado del todo, pero los tramos suman "
                        f"{g['justificado']} de {g['requerido']} min de {gap} (entra por la tolerancia de "
                        f"{cov['tolerancia_min']} min): ajusta esa frase antes de aplicar")
                    break
        hay_material = (any(v.get("del_caso") == "SI" for v in (ia.get("videos") or []))
                        or bool(ia.get("tramos_justificados")))
        if cov["aplicable"] and hay_material and not cov["cubierto"]:
            faltas = " · ".join(f"faltan {g['faltan']} min de {gap}"
                                for gap, g in cov["gaps"].items() if not g["cubierto"])
            ia["veredicto_ia"] = ia.get("excluible_sugerido")
            ia["excluible_sugerido"] = None
            ia["revision"] = {"motivo": (f"la evidencia no cubre los tiempos dentro de la "
                                         f"tolerancia de {cov['tolerancia_min']} min por "
                                         f"gap ({faltas})")}
        elif (cov["aplicable"] and hay_material and not ia.get("excluible_sugerido")
              and not ia.get("veredicto_ia") and not ia.get("revision")):
            # Hay material y la suma entra en la tolerancia, pero la IA no se pronunció (caso del
            # 2026-10-01: con el reloj bien leído solo 4 de los 12 min eran demora del C.C.). El
            # caso no puede quedar sin botones de decisión: Revisión, y decide el ING.
            partes = " · ".join(f"{g['justificado']} de {g['requerido']} min de {gap}"
                                for gap, g in cov["gaps"].items())
            ia["revision"] = {"motivo": (f"la máquina no propone veredicto con este material: la evidencia "
                                         f"demuestra {partes} (lo demás entra por la tolerancia de "
                                         f"{cov['tolerancia_min']} min por gap)")}
    if resultado.get("tipo") == "3.2A" and not ia.get("error"):
        rt = resumen_tiempos(reg, ia, resultado)
        ia["resumen_tiempos"] = rt
        if rt["con_tramos"]:
            ia["observacion"] = claude._cuadrar_tiempos(claude._cuadrar_anexos(ia.get("observacion") or "", rt["anexar"]))
    a = WaAnalisis(reg_id=reg.id, campo="excluible", estado="propuesto",
                   resultado=json.dumps(resultado, ensure_ascii=False))
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a
