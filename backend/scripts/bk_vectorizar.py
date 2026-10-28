"""BÚSQUEDA SEMÁNTICA (orquestación ANS, paso 6 — 2026-09-07): todo texto que llega al ANS
(captions y textos de WhatsApp, letrero + transcripción de cada video, correos, respuestas de
los hilos) se convierte en vectores con `@cf/baai/bge-m3` (Workers AI) y se guarda en el índice
Vectorize `sabogal-ans`. Es la TERCERA capa del matcher: «todo lo que habla de este caso»
aunque nadie escribiera el código del PDL.

Uso (en el VPS, desde backend/):
  venv/bin/python scripts/vectorizar.py                 # indexa lo nuevo (cron 04:10)
  venv/bin/python scripts/vectorizar.py --limite 200
  venv/bin/python scripts/vectorizar.py --buscar "cierre de vía por derrumbe en Gachetá"
  venv/bin/python scripts/vectorizar.py --amarrar 2026-09     # amarres «semantico» (50) para el periodo

Idempotente: data/vectorize_manifest.json recuerda los ids ya subidos.
"""
import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import httpx  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.database import AsyncSessionLocal  # noqa: E402

INDICE = "sabogal-ans"
MODELO = "@cf/baai/bge-m3"
DIM = 1024
DATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
MANIFIESTO = os.path.join(DATA, "vectorize_manifest.json")
BASE = f"https://api.cloudflare.com/client/v4/accounts/{settings.CF_ACCOUNT_ID}"
H_ORQ = {"Authorization": f"Bearer {settings.CF_ORQ_TOKEN}"}
H_AI = {"Authorization": f"Bearer {settings.CF_AI_TOKEN}"}


def cargar_manifiesto() -> set:
    try:
        with open(MANIFIESTO, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def guardar_manifiesto(m: set) -> None:
    tmp = MANIFIESTO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(m), f)
    os.replace(tmp, MANIFIESTO)


async def asegurar_indice(cli: httpx.AsyncClient) -> None:
    r = await cli.get(f"{BASE}/vectorize/v2/indexes/{INDICE}", headers=H_ORQ)
    if r.status_code == 200 and r.json().get("success"):
        return
    r = await cli.post(f"{BASE}/vectorize/v2/indexes", headers=H_ORQ,
                       json={"name": INDICE, "description": "Texto del ANS (WhatsApp, videos, correos)",
                             "config": {"dimensions": DIM, "metric": "cosine"}})
    d = r.json()
    print("índice creado:", d.get("success"), d.get("errors") or "")
    if d.get("success"):
        for prop in ("tipo", "fecha", "zona"):
            await cli.post(f"{BASE}/vectorize/v2/indexes/{INDICE}/metadata_index/create", headers=H_ORQ,
                           json={"propertyName": prop, "indexType": "string"})


async def embed(cli: httpx.AsyncClient, textos: list) -> list:
    r = await cli.post(f"{BASE}/ai/run/{MODELO}", headers=H_AI, json={"text": textos}, timeout=120)
    d = r.json()
    if not d.get("success"):
        raise RuntimeError(f"embedding: {d.get('errors')}")
    return d["result"]["data"]


async def upsert(cli: httpx.AsyncClient, vectores: list) -> None:
    cuerpo = "\n".join(json.dumps(v, ensure_ascii=False) for v in vectores)
    r = await cli.post(f"{BASE}/vectorize/v2/indexes/{INDICE}/upsert", headers={**H_ORQ, "Content-Type": "application/x-ndjson"},
                       content=cuerpo.encode("utf-8"), timeout=120)
    d = r.json()
    if not d.get("success"):
        raise RuntimeError(f"upsert: {d.get('errors')}")


async def consultar(cli: httpx.AsyncClient, texto: str, top: int = 20, filtro: dict | None = None) -> list:
    vec = (await embed(cli, [texto]))[0]
    cuerpo = {"vector": vec, "topK": top, "returnMetadata": "all"}
    if filtro:
        cuerpo["filter"] = filtro
    r = await cli.post(f"{BASE}/vectorize/v2/indexes/{INDICE}/query", headers=H_ORQ, json=cuerpo, timeout=60)
    d = r.json()
    if not d.get("success"):
        raise RuntimeError(f"query: {d.get('errors')}")
    return (d.get("result") or {}).get("matches") or []


async def documentos(db, hechos: set):
    """Todo texto del ANS aún no vectorizado: (id, texto, metadata)."""
    out = []
    r = await db.execute(text("""select m.id, m.texto, m.chat_nombre, m.enviado_en, m.tipo from wa_mensajes m
        where m.texto is not null and length(m.texto) >= 25 order by m.id"""))
    for mid, txt, chat, env, tipo in r:
        k = f"m{mid}"
        if k in hechos:
            continue
        out.append((k, txt[:1500], {"tipo": "mensaje", "ref": mid, "fecha": env.date().isoformat() if env else "",
                                     "zona": (chat or "")[:60]}))
    r = await db.execute(text("""select w.msg_id, w.overlay_fecha, w.overlay_hora, w.overlay_codigo, w.overlay_lugar,
        w.overlay_lider, w.transcripcion, m.texto, m.chat_nombre, m.enviado_en, w.tipo
        from wa_media_meta w join wa_mensajes m on m.id = w.msg_id where w.estado = 'listo' order by w.msg_id"""))
    for mid, f, h, cod, lug, lid, tr, cap, chat, env, tipo in r:
        k = f"v{mid}"
        if k in hechos:
            continue
        partes = [p for p in (f"{f or ''} {h or ''}".strip(), cod, lug, (f"#{lid}" if lid else None), cap, tr) if p]
        txt = " · ".join(str(p) for p in partes).strip()
        if len(txt) < 15:
            continue
        out.append((k, txt[:2000], {"tipo": tipo or "video", "ref": mid, "fecha": (f or (env.date().isoformat() if env else "")),
                                     "zona": (chat or "")[:60]}))
    r = await db.execute(text("select id, asunto, cuerpo, fecha_correo from correo_ans where estado <> 'descartado'"))
    for cid, asunto, cuerpo, fc in r:
        k = f"c{cid}"
        if k in hechos:
            continue
        out.append((k, f"{asunto or ''}\n{(cuerpo or '')[:2500]}", {"tipo": "correo", "ref": cid, "fecha": (fc or "")[:10], "zona": ""}))
    r = await db.execute(text("""select q.id, q.texto, q.creado_en, c.reg_id from ans_consulta_msgs q
        join ans_consultas c on c.id = q.consulta_id where q.direccion = 'recibido' and length(coalesce(q.texto,'')) >= 20"""))
    for qid, txt, t, reg_id in r:
        k = f"q{qid}"
        if k in hechos:
            continue
        out.append((k, txt[:1500], {"tipo": "respuesta", "ref": qid, "fecha": t.date().isoformat() if t else "", "zona": str(reg_id)}))
    return out


async def indexar(limite: int) -> None:
    hechos = cargar_manifiesto()
    async with httpx.AsyncClient(timeout=120) as cli, AsyncSessionLocal() as db:
        await asegurar_indice(cli)
        docs = await documentos(db, hechos)
        if limite:
            docs = docs[:limite]
        print(f"{len(docs)} documento(s) por vectorizar")
        t0 = time.time()
        for i in range(0, len(docs), 40):
            lote = docs[i:i + 40]
            vecs = await embed(cli, [d[1] for d in lote])
            await upsert(cli, [{"id": d[0], "values": v, "metadata": d[2]} for d, v in zip(lote, vecs)])
            hechos.update(d[0] for d in lote)
            guardar_manifiesto(hechos)
            print(f"  {min(i + 40, len(docs))}/{len(docs)} · {time.time() - t0:.0f}s")
    print(f"listo: manifiesto {len(hechos)} vectores")


async def amarrar_semantico(periodo: str) -> None:
    """Tercera capa del matcher: por cada caso del periodo, los textos más parecidos a la orden
    (descripción + líder + zona) dentro de su ventana de fechas → amarre «semantico» (50) si no
    había uno más fuerte."""
    from app.models.models import AnsRegistro, WaMediaCaso
    from app.services import matcher
    from datetime import timedelta
    n = 0
    async with httpx.AsyncClient(timeout=120) as cli, AsyncSessionLocal() as db:
        regs = (await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo, AnsRegistro.entorno == "real"))).scalars().all()
        for reg in regs:
            ini, fin = matcher.ventana_caso(reg)
            if not ini:
                continue
            consulta = (f"orden {reg.pdl} zona {reg.zona or ''} líder {reg.ri_jt or ''} responsable {reg.nombre_responsable or ''} "
                        f"{(reg.descripcion or '')[:400]}")
            try:
                hits = await consultar(cli, consulta, top=25)
            except Exception as e:  # noqa: BLE001
                print("  consulta falló", reg.pdl, e)
                continue
            for h in hits:
                md = h.get("metadata") or {}
                if md.get("tipo") not in ("video", "imagen", "mensaje") or h.get("score", 0) < 0.55:
                    continue
                f = md.get("fecha") or ""
                if not (f and (ini - timedelta(days=1)).date().isoformat() <= f <= (fin + timedelta(days=1)).date().isoformat()):
                    continue
                msg_id = int(md.get("ref"))
                ya = (await db.execute(select(WaMediaCaso).where(WaMediaCaso.msg_id == msg_id, WaMediaCaso.reg_id == reg.id))).scalars().first()
                if ya:
                    if ya.puntaje < 50:
                        ya.criterio, ya.puntaje, ya.detalle = "semantico", 50, f"texto parecido a la orden (similitud {h['score']:.2f})"
                        n += 1
                    continue
                db.add(WaMediaCaso(msg_id=msg_id, reg_id=reg.id, criterio="semantico", puntaje=50,
                                   detalle=f"texto parecido a la orden (similitud {h['score']:.2f})"))
                n += 1
        await db.commit()
    print(f"amarres semánticos nuevos/subidos: {n}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--buscar", default="")
    ap.add_argument("--amarrar", default="")
    a = ap.parse_args()
    if not (settings.CF_ORQ_TOKEN and settings.CF_AI_TOKEN):
        raise SystemExit("faltan CF_ORQ_TOKEN / CF_AI_TOKEN")
    if a.buscar:
        async with httpx.AsyncClient(timeout=120) as cli:
            for h in await consultar(cli, a.buscar, top=10):
                print(f"  {h['score']:.3f} {h['id']:10s} {h.get('metadata')}")
        return
    if a.amarrar:
        periodo = a.amarrar
        if periodo == "auto":
            from sqlalchemy import func
            from app.models.models import AnsRegistro
            async with AsyncSessionLocal() as db:
                periodo = (await db.execute(select(func.max(AnsRegistro.periodo)).where(AnsRegistro.entorno == "real"))).scalar()
        await amarrar_semantico(periodo)
        return
    await indexar(a.limite)


if __name__ == "__main__":
    asyncio.run(main())
