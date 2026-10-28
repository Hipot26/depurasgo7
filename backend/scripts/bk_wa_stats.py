"""ESTADÍSTICA de lo que llega al WhatsApp del ANS y cuánto espacio ocupa (2026-09-03): todo se
guarda y esta estadística sirve para decidir la retención.

Uso (en el VPS, desde backend/):  venv/bin/python scripts/wa_stats.py [--json]
Imprime: captura en vivo (¿se guarda todo?), volumen por mes / tipo / grupo, ritmo diario de
los últimos 30 días, proyección a 6 y 12 meses, disco libre y avance del índice (wa_media_meta).
"""
import argparse
import asyncio
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sqlalchemy import text  # noqa: E402

from app.db.database import AsyncSessionLocal  # noqa: E402

WA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "whatsapp"))
MB = 1048576.0


def _num(v):
    return float(v) if v is not None and type(v).__name__ == "Decimal" else v


async def q(db, sql):
    return [tuple(_num(x) for x in r) for r in (await db.execute(text(sql)))]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = {}
    async with AsyncSessionLocal() as db:
        out["total"] = (await q(db, "select count(*), count(media_ruta), coalesce(sum(media_tamano),0) from wa_mensajes"))[0]
        out["por_mes"] = await q(db, """select to_char(enviado_en,'YYYY-MM'), count(*), count(media_ruta),
            coalesce(sum(media_tamano),0) from wa_mensajes group by 1 order by 1""")
        out["por_tipo"] = await q(db, """select tipo, es_grupo, count(*), count(media_ruta), coalesce(sum(media_tamano),0)
            from wa_mensajes group by 1,2 order by 5 desc""")
        out["por_grupo"] = await q(db, """select coalesce(g.nombre, m.chat_jid), g.vigilado, count(*), count(m.media_ruta),
            coalesce(sum(m.media_tamano),0) from wa_mensajes m left join wa_grupos g on g.jid = m.chat_jid
            where m.es_grupo group by 1,2 order by 5 desc""")
        # captura en vivo: desde que el bot corre (2026-08-13), ¿cuánta media se bajó?
        out["captura_vivo"] = await q(db, """select tipo, count(*), count(media_ruta) from wa_mensajes
            where enviado_en >= '2026-08-13' and tipo <> 'texto' group by 1""")
        out["ritmo_30d"] = (await q(db, """select count(*) filter (where media_ruta is not null),
            coalesce(sum(media_tamano),0) from wa_mensajes where enviado_en >= now() - interval '30 days'"""))[0]
        out["indice"] = await q(db, """select estado, count(*), avg(tiempo_s), count(transcripcion),
            count(overlay_codigo), count(overlay_fecha) from wa_media_meta group by 1""") \
            if (await q(db, "select to_regclass('wa_media_meta')"))[0][0] else []
    disco = shutil.disk_usage("/")
    out["disco"] = {"total_gb": disco.total / MB / 1024, "libre_gb": disco.free / MB / 1024}
    out["carpeta_gb"] = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(WA_DIR) for f in fs) / MB / 1024 \
        if os.path.isdir(WA_DIR) else 0

    if a.json:
        print(json.dumps(out, default=str, ensure_ascii=False, indent=1))
        return
    n, nm, b = out["total"]
    print(f"Mensajes: {n} · con archivo: {nm} · {b / MB / 1024:.2f} GB en base (carpeta real {out['carpeta_gb']:.2f} GB)")
    print(f"Disco: {out['disco']['libre_gb']:.1f} GB libres de {out['disco']['total_gb']:.0f} GB")
    print("\nCaptura en vivo (desde 2026-08-13, bot corriendo):")
    for tipo, t, con in out["captura_vivo"]:
        print(f"  {tipo:10s} {con}/{t} con archivo ({100 * con / t:.0f} %)")
    print("\nPor mes:")
    for mes, t, con, bb in out["por_mes"]:
        print(f"  {mes}  {t:5d} msgs · {con:4d} archivos · {bb / MB:8.0f} MB")
    print("\nPor tipo:")
    for tipo, grupo, t, con, bb in out["por_tipo"]:
        print(f"  {tipo:10s} {'grupo' if grupo else 'directo':8s} {t:5d} msgs · {con:4d} archivos · {bb / MB:8.0f} MB")
    print("\nPor grupo:")
    for g, vig, t, con, bb in out["por_grupo"]:
        print(f"  {'●' if vig else '○'} {str(g)[:34]:34s} {t:5d} msgs · {con:4d} archivos · {bb / MB:8.0f} MB")
    con30, b30 = out["ritmo_30d"]
    dia = b30 / MB / 30
    print(f"\nRitmo últimos 30 días: {con30} archivos · {b30 / MB:.0f} MB → {dia:.0f} MB/día ≈ {dia * 30 / 1024:.1f} GB/mes")
    print(f"Proyección: +{dia * 182 / 1024:.1f} GB en 6 meses · +{dia * 365 / 1024:.1f} GB en 12 meses "
          f"(el disco libre alcanza ≈ {out['disco']['libre_gb'] * 1024 / dia / 30:.0f} meses a este ritmo, sin contar otros proyectos)")
    if out["indice"]:
        print("\nÍndice wa_media_meta:")
        for est, t, tm, tr, cod, fec in out["indice"]:
            print(f"  {est:9s} {t:5d} · {float(tm or 0):.1f} s/archivo · transcritos {tr} · con código {cod} · con fecha {fec}")


if __name__ == "__main__":
    asyncio.run(main())
