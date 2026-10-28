"""HORA ORIGINAL de los mensajes que el bot no pudo descifrar al primer intento (2026-10-01).

Cuando falla el descifrado, WhatsApp reenvía el mensaje y el reenvío trae OTRA hora: el bot la
guardaba como `enviado_en` (Edison 9:00 → 09:06; Carlos 8:40 → 08:56) y el chat quedaba
desordenado. El log del bot registra cada intento fallido («failed to decrypt message», con el
id del mensaje y la hora en ms). La hora del PRIMER intento es cuándo llegó de verdad.

Por defecto solo dice qué cambiaría. Con --aplicar corrige `wa_mensajes.enviado_en` (solo si la
hora del primer intento es ANTERIOR a la guardada) y deja la hora vieja en `respaldo_horas_reenvio`.

Uso (en el VPS, cwd = backend/): venv/bin/python scripts/horas_reenvio.py [--aplicar]"""
import argparse, asyncio, json, os, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.abspath("."))
from sqlalchemy import select, text
from app.db.database import AsyncSessionLocal
from app.models.models import WaMensaje

LOG = "/root/.pm2/logs/sabogal-wa-bot-out.log"
MARGEN_S = 20          # menos que esto es ruido de red, no un reenvío


def primeros_intentos() -> dict:
    out = {}
    with open(LOG, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            if "failed to decrypt message" not in ln or not ln.startswith("{"):
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            mid, t = (d.get("key") or {}).get("id"), d.get("time")
            if mid and t and (mid not in out or t < out[mid]):
                out[mid] = t
    return out


async def main(aplicar: bool):
    intentos = primeros_intentos()
    print(f"ids con descifrado fallido en el log: {len(intentos)}")
    async with AsyncSessionLocal() as db:
        cambios = []
        ids = list(intentos)
        for i in range(0, len(ids), 1000):
            r = await db.execute(select(WaMensaje).where(WaMensaje.wa_id.in_(ids[i:i + 1000])))
            for m in r.scalars().all():
                orig = datetime.fromtimestamp(intentos[m.wa_id] / 1000, tz=timezone.utc).replace(tzinfo=None)
                if m.enviado_en and (m.enviado_en - orig).total_seconds() > MARGEN_S:
                    cambios.append((m, orig))
        por_tipo = {}
        for m, orig in cambios:
            k = ("grupo" if m.es_grupo else "directo") + (" propio" if m.de_mi else "")
            por_tipo[k] = por_tipo.get(k, 0) + 1
        print(f"mensajes guardados con la hora del reenvío: {len(cambios)} · {por_tipo}")
        dif = sorted((m.enviado_en - o).total_seconds() / 60 for m, o in cambios)
        if dif:
            print(f"desfase: mediana {dif[len(dif) // 2]:.1f} min · máx {dif[-1]:.0f} min")
        for m, orig in sorted(cambios, key=lambda x: x[0].enviado_en)[-12:]:
            print(f"  {m.id} {'grupo' if m.es_grupo else 'DM'} {m.tipo}: {(m.enviado_en - timedelta(hours=5)):%d/%m %H:%M:%S}"
                  f" → {(orig - timedelta(hours=5)):%d/%m %H:%M:%S} · {(m.texto or '')[:50]!r}")
        if aplicar and cambios:
            await db.execute(text("create table if not exists respaldo_horas_reenvio "
                                  "(msg_id integer primary key, enviado_en_viejo timestamp, enviado_en_nuevo timestamp, "
                                  "corregido_en timestamp default now())"))
            for m, orig in cambios:
                await db.execute(text("insert into respaldo_horas_reenvio (msg_id, enviado_en_viejo, enviado_en_nuevo) "
                                      "values (:i, :v, :n) on conflict (msg_id) do nothing"),
                                 {"i": m.id, "v": m.enviado_en, "n": orig})
                m.enviado_en = orig
            await db.commit()
            print(f"APLICADO: {len(cambios)} horas corregidas (respaldo en respaldo_horas_reenvio)")
        else:
            await db.rollback()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true")
    asyncio.run(main(ap.parse_args().aplicar))
