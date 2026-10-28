"""Vuelca el MATERIAL de un caso del ANS (contexto + textos del día + frames y
transcripciones de sus videos) a una carpeta, sin llamar a la API de IA.

Sirve para revisar un caso a mano o con una herramienta externa a partir del mismo material
que usa el análisis automático; los tokens de la API quedan para los ciclos desatendidos (bot
de WhatsApp, auto-análisis de correos al ingresar). Flujo:

  ./venv/bin/python scripts/material_caso.py <orden> /tmp/material_<orden>
  # (opcional --sin-audio para saltarse whisper y tener los frames en segundos)

Deja en la carpeta: contexto.txt (orden + desfases), eventos.txt (mensajes del día,
dedupe), y por video v<msgid>_1.jpg/_2.jpg/_3.jpg (inicio/mitad/final) + v<msgid>.txt
(grupo, hora de subida, duración, transcripción). La búsqueda es la misma de FASE 1 del
analista (grupos de la zona); si no atribuyes nada, corre también el rastreo ampliado a
mano con los demás grupos (--todos)."""
import asyncio
import base64
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models.models import AnsRegistro, WaGrupo
from app.services import wa_analista as wa


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sin_audio = "--sin-audio" in sys.argv
    todos = "--todos" in sys.argv
    if len(args) < 2:
        print(__doc__)
        return
    pdl, out = args[0], args[1]
    os.makedirs(out, exist_ok=True)

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(AnsRegistro).where(
            AnsRegistro.pdl.ilike(f"%{pdl}%"), AnsRegistro.entorno == "real"))
        reg = r.scalars().first()
        if not reg:
            print(f"{pdl}: registro no encontrado")
            return
        dia = wa._parse_fecha(reg.fecha_fin)
        if not dia:
            print(f"{pdl}: fecha de cierre ilegible ({reg.fecha_fin!r})")
            return
        ini_utc = dia + wa.UTC_OFFSET
        fin_utc = dia + timedelta(hours=30) + wa.UTC_OFFSET

        rg = await db.execute(select(WaGrupo).where(WaGrupo.vigilado))
        grupos = rg.scalars().all()
        if todos:
            jids = [g.jid for g in grupos]
        else:
            patrones = wa.GRUPOS_ZONA.get((reg.zona or "").upper().replace("É", "E"), [])
            jids = [g.jid for g in grupos
                    if any(p in (g.nombre or "").lower().replace("é", "e") for p in patrones)]
        nombre_grupo = {g.jid: (g.nombre or g.jid) for g in grupos}
        if not jids:
            print(f"{pdl}: la zona {reg.zona!r} no tiene grupos vigilados (prueba --todos)")
            return

        with open(os.path.join(out, "contexto.txt"), "w", encoding="utf-8") as f:
            f.write(wa._contexto_orden(reg) + "\n")

        mensajes = await wa._material_del_dia(db, jids, ini_utc, fin_utc)

        # Textos del día (mismo dedupe del analista: los grupos espejan mensajes)
        eventos, vistos = [], set()
        for m in mensajes:
            if m.tipo != "texto" or not (m.texto or "").strip():
                continue
            clave = m.texto.strip()[:120]
            if clave in vistos:
                continue
            vistos.add(clave)
            quien = f" ({m.remitente_nombre})" if m.remitente_nombre else ""
            eventos.append(f"{wa._local(m.enviado_en):%H:%M} "
                           f"[{nombre_grupo.get(m.chat_jid, '?')}]"
                           f"{quien}: {m.texto.strip()[:600]}")
        with open(os.path.join(out, "eventos.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(eventos) or "(sin mensajes de texto ese día)")

        # Videos del día (dedupe por tamaño), frames inicio/mitad/final + transcripción
        videos, tam_vistos = [], set()
        for m in mensajes:
            if m.tipo != "video" or not m.media_ruta or not os.path.exists(m.media_ruta):
                continue
            if m.media_tamano in tam_vistos:
                continue
            tam_vistos.add(m.media_tamano)
            videos.append(m)
        for m in videos[:wa.MAX_VIDEOS]:
            dur = await wa._ffprobe_duracion(m.media_ruta)
            for i, seg in enumerate((1.0, dur / 2 if dur > 4 else 2.0,
                                     max(dur - 1.5, 2.5))[:wa.FRAMES_POR_VIDEO], 1):
                b64 = await wa._frame_b64(m.media_ruta, seg)
                if b64:
                    with open(os.path.join(out, f"v{m.id}_{i}.jpg"), "wb") as f:
                        f.write(base64.b64decode(b64))
            trans = None
            if not sin_audio:
                from app.services.transcribe import transcribir_video
                try:
                    trans = await asyncio.to_thread(transcribir_video, m.media_ruta)
                except Exception as e:                                     # noqa: BLE001
                    trans = f"(transcripción falló: {e})"
            with open(os.path.join(out, f"v{m.id}.txt"), "w", encoding="utf-8") as f:
                f.write(f"msg_id: {m.id}\ngrupo: {nombre_grupo.get(m.chat_jid, '?')}\n"
                        f"subido: {wa._local(m.enviado_en):%H:%M} (hora local)\n"
                        f"duración: {round(dur)}s\n"
                        f"transcripción:\n{trans or '(sin audio / --sin-audio)'}\n")
        print(f"{pdl}: {len(eventos)} textos · {min(len(videos), wa.MAX_VIDEOS)} videos "
              f"({len(videos)} hallados) → {out}")

asyncio.run(main())
