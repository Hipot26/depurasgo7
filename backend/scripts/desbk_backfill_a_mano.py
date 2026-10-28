"""Mensajes que Sergio escribió A MANO en chats directos (de_mi, no envío del bot) y que no están
en el hilo de consulta al que pertenecen — TODO el historial, hilos abiertos o cerrados.

Por defecto SOLO dice qué haría. Con --aplicar mete cada uno como turno 'enviado' (via manual,
hora real de WhatsApp). Nunca relee con IA ni toca celdas: es solo el registro de la conversación.

Asignación (misma lógica del barrido en vivo, ampliada a hilos cerrados):
  · teléfono del otro = remitente_jid del mensaje propio (lids.json);
  · candidatos = hilos (no descartados) con un envío del bot a ese teléfono, o con un turno
    'enviado' a una persona del directorio con ese teléfono;
  · si el texto nombra una orden (R#####), va al hilo de ESA orden — y si ese hilo ya tiene su
    envío registrado (chat guiado, con otro saludo) no se duplica; una orden sin hilo → fuera;
  · sin orden en el texto, solo entra si cae dentro de la conversación activa de un hilo (del
    primer envío a un día después del último turno); si hay varios, el del envío más reciente.

Uso (en el VPS, cwd = backend/): venv/bin/python scripts/backfill_a_mano.py [--aplicar]
Re-correr hasta que diga a_meter=0: cada pasada alarga la ventana de los hilos tocados (01-10: 22 + 8)."""
import argparse, asyncio, json, os, re, sys
from datetime import timedelta
sys.path.insert(0, os.path.abspath("."))
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.models import WaMensaje, WaEnvio, AnsConsulta, AnsConsultaMsg, AnsRegistro, Contacto
from app.services.wa_respuestas import _telefono_de, _norm_txt
from app.services import letrero


def _tel(s):
    return re.sub(r"\D", "", s or "")


LIDS = "/var/www/sabogal-platform/whatsapp-bot/lids.json"


def _mapa_lids() -> dict:
    """lid → teléfono que el bot aprendió (desde el 03-09). Los mensajes guardados antes quedaron
    con el @lid como remitente: sin esto no se sabe con quién era la conversación."""
    try:
        with open(LIDS, encoding="utf-8") as fh:
            return {k.split("@")[0]: _tel(v.split("@")[0]) for k, v in json.load(fh).items()}
    except (OSError, ValueError):
        return {}


async def main(aplicar: bool):
    from app.api.ans import _norm_nombre
    async with AsyncSessionLocal() as db:
        envios = (await db.execute(select(WaEnvio))).scalars().all()
        ids_bot = {e.wa_id for e in envios if e.wa_id}
        textos_bot = {(_tel(e.destino), _norm_txt(e.texto)) for e in envios}
        contactos = (await db.execute(select(Contacto))).scalars().all()
        por_tel = {}
        for c in contactos:
            if _tel(c.telefono):
                por_tel.setdefault(_tel(c.telefono), []).append(c)
        hilos = {c.id: c for c in (await db.execute(select(AnsConsulta).where(AnsConsulta.estado != "descartada"))).scalars().all()}
        turnos = {}
        for t in (await db.execute(select(AnsConsultaMsg).where(AnsConsultaMsg.consulta_id.in_(list(hilos))))).scalars().all():
            turnos.setdefault(t.consulta_id, []).append(t)
        regs_h = {r.id: r for r in (await db.execute(select(AnsRegistro).where(
            AnsRegistro.id.in_({c.reg_id for c in hilos.values()})))).scalars().all()}
        rnum_hilo = {hid: letrero.rnum(regs_h[c.reg_id].pdl) for hid, c in hilos.items() if c.reg_id in regs_h}
        ya_msg = set()
        for ts in turnos.values():
            for t in ts:
                try:
                    mid = (json.loads(t.meta or "{}") or {}).get("msg_id")
                except ValueError:
                    mid = None
                if mid:
                    ya_msg.add(mid)

        propios = (await db.execute(select(WaMensaje).where(WaMensaje.es_grupo.is_(False), WaMensaje.de_mi.is_(True))
                                    .order_by(WaMensaje.enviado_en))).scalars().all()
        lids = _mapa_lids()
        # teléfono por chat, de los mensajes del OTRO que sí traen número
        tel_chat = {}
        for jid, rem in (await db.execute(select(WaMensaje.chat_jid, WaMensaje.remitente_jid).where(
                WaMensaje.es_grupo.is_(False), WaMensaje.de_mi.is_(False),
                WaMensaje.remitente_jid.like("%@s.whatsapp.net")))).all():
            tel_chat.setdefault(jid, _tel(rem.split("@")[0]))
        resumen = {"eco_bot": 0, "sin_contacto": 0, "sin_hilo": 0, "otra_orden_sin_hilo": 0, "ya_en_hilo": 0, "duplicado_chat_guiado": 0, "a_meter": 0}
        plan = []
        for m in propios:
            tel = _telefono_de(m)
            if (m.remitente_jid or m.chat_jid or "").endswith("@lid"):
                tel = lids.get(tel) or tel_chat.get(m.chat_jid) or tel
            if m.wa_id in ids_bot or (tel, _norm_txt(m.texto)) in textos_bot:
                resumen["eco_bot"] += 1; continue
            if m.id in ya_msg:
                resumen["ya_en_hilo"] += 1; continue
            cts = por_tel.get(tel) or []
            if not cts:
                resumen["sin_contacto"] += 1; continue
            nombres = {_norm_nombre(c.nombre) for c in cts}
            txt = (m.texto or "").strip()
            # Hilos de ESTA persona: con envío del bot a su teléfono o turno 'enviado' a su nombre
            suyos = []                              # (hilo, horas de envío, ventana fin)
            for hid, c in hilos.items():
                horas = [e.enviado_en or e.creado_en for e in envios
                         if e.reg_id == c.reg_id and e.campo == c.campo and _tel(e.destino) == tel]
                horas += [t.creado_en for t in turnos.get(hid, [])
                          if t.direccion == "enviado" and _norm_nombre(t.persona or "") in nombres]
                horas = [h for h in horas if h]
                if not horas:
                    continue
                conv = [t.creado_en for t in turnos.get(hid, []) if t.direccion in ("enviado", "recibido") and t.creado_en]
                suyos.append((c, horas, max(conv + horas) + timedelta(days=1)))
            codigos = set(letrero.codigos_r(txt))
            elegidos = []
            if codigos:
                # Nombra órdenes: va al hilo de CADA una (contenido antes que hora), o a ninguno
                del_codigo = [c for c, _h, _f in suyos if rnum_hilo.get(c.id) in codigos]
                if not del_codigo:
                    resumen["otra_orden_sin_hilo"] += 1; continue
                for c in del_codigo:
                    # ¿Su envío ya está registrado en el hilo (chat guiado, con otro saludo)?
                    if any(t.direccion == "enviado" and rnum_hilo.get(c.id) in set(letrero.codigos_r(t.texto or ""))
                           and t.creado_en and abs((t.creado_en - m.enviado_en).total_seconds()) <= 2 * 86400
                           for t in turnos.get(c.id, [])):
                        resumen["duplicado_chat_guiado"] += 1; continue
                    elegidos.append(c)
            else:
                # Sin orden en el texto: solo dentro de la conversación activa, y al hilo de la
                # ÚLTIMA conversación con esa persona antes del mensaje (envío o respuesta)
                dentro = []
                for c, horas, fin_v in suyos:
                    if not (any(h <= m.enviado_en for h in horas) and m.enviado_en <= fin_v):
                        continue
                    con_el = [t.creado_en for t in turnos.get(c.id, []) if t.creado_en and t.creado_en <= m.enviado_en
                              and t.direccion in ("enviado", "recibido") and _norm_nombre(t.persona or "") in nombres]
                    dentro.append((max(con_el + [h for h in horas if h <= m.enviado_en]), c))
                if not dentro:
                    resumen["sin_hilo"] += 1; continue
                elegidos = [max(dentro, key=lambda x: x[0])[1]]
            for c in elegidos:
                if txt and any(t.direccion == "enviado" and _norm_txt(t.texto)[:300] == _norm_txt(txt)[:300]
                               for t in turnos.get(c.id, [])):
                    resumen["duplicado_chat_guiado"] += 1; continue
                contacto = next((x for x in cts if _norm_nombre(x.nombre) == _norm_nombre(c.persona_actual or "")), None)                     or next((x for x in cts if any(t.direccion == "enviado" and _norm_nombre(t.persona or "") == _norm_nombre(x.nombre)
                                                    for t in turnos.get(c.id, []))), cts[0])
                reg = await db.get(AnsRegistro, c.reg_id)
                resumen["a_meter"] += 1
                plan.append((m, c, contacto, reg))
                print(f"  {m.id} {(m.enviado_en - timedelta(hours=5)):%d/%m %H:%M} → hilo {c.id} ({c.estado}) {reg.pdl if reg else '?'} "
                      f"{c.campo} · a {contacto.nombre}: {txt[:80]!r}")
        print("\nRESUMEN:", resumen, f"(de {len(propios)} mensajes propios en chats directos)")
        if aplicar and plan:
            for m, c, contacto, reg in plan:
                db.add(AnsConsultaMsg(consulta_id=c.id, direccion="enviado", persona=contacto.nombre,
                                      texto=((m.texto or "").strip() or f"({m.tipo} adjunto)")[:4000],
                                      creado_en=m.enviado_en,
                                      meta=json.dumps({"rol_dir": contacto.rol or "otro", "via": "manual",
                                                       "wa_id": m.wa_id, "msg_id": m.id, "tipo": m.tipo,
                                                       "mime": m.media_mime, "match": "backfill"})))
                if m.estado == "nuevo":
                    m.estado = "vinculado"
            await db.commit()
            print(f"APLICADO: {len(plan)} turnos nuevos (sin IA, sin tocar celdas)")
        else:
            await db.rollback()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true")
    asyncio.run(main(ap.parse_args().aplicar))
