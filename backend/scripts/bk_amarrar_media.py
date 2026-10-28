"""AMARRE nocturno media ↔ caso (orquestación ANS, paso 1). Recalcula `wa_media_caso` para el
periodo competente y el anterior (los videos de fin de mes llegan al día siguiente) con lo
que dejó el índice de media. Corre a las 04:00, entre el índice (03:40) y los análisis (04:30).

Uso (en el VPS, desde backend/):
  venv/bin/python scripts/amarrar_media.py                 # competente + anterior
  venv/bin/python scripts/amarrar_media.py 2026-08         # un periodo
  venv/bin/python scripts/amarrar_media.py --verificar     # además, mide contra los análisis aplicados:
                                                           # ¿los videos que la IA atribuyó (del_caso=SI)
                                                           # quedaron amarrados por código? (recall)
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sqlalchemy import select, func  # noqa: E402

from app.db.database import AsyncSessionLocal, engine  # noqa: E402
from app.models.models import Base, AnsRegistro, WaAnalisis, WaMediaCaso  # noqa: E402
from app.services import matcher  # noqa: E402


def _periodo_anterior(p: str) -> str:
    a, m = int(p[:4]), int(p[5:7])
    return f"{a - 1}-12" if m == 1 else f"{a}-{m - 1:02d}"


async def verificar(db):
    """Recall del matcher contra la verdad ya validada por una persona: los videos del_caso=SI de los
    análisis aplicados. Por cada uno: ¿hay amarre ≥ 75 para (msg, reg)? ¿≥ 40?"""
    r = await db.execute(select(WaAnalisis).where(WaAnalisis.estado == "aplicado"))
    total = fuerte = debil = 0
    faltantes = []
    for a in r.scalars().all():
        try:
            res = json.loads(a.resultado or "{}")
        except ValueError:
            continue
        for v in (res.get("ia") or {}).get("videos") or []:
            if v.get("del_caso") != "SI" or not v.get("msg_id"):
                continue
            total += 1
            am = (await db.execute(select(WaMediaCaso).where(WaMediaCaso.msg_id == v["msg_id"],
                                                             WaMediaCaso.reg_id == a.reg_id))).scalars().first()
            if am and am.puntaje >= 75:
                fuerte += 1
            elif am:
                debil += 1
            else:
                reg = await db.get(AnsRegistro, a.reg_id)
                faltantes.append((a.id, getattr(reg, "pdl", a.reg_id), v["msg_id"], v.get("overlay")))
    print(f"\nVERIFICACIÓN contra {total} videos validados por humano en análisis aplicados:")
    if total:
        print(f"  amarre fuerte (≥75): {fuerte} ({100 * fuerte / total:.0f} %) · débil (40): {debil} · sin amarre: {len(faltantes)}")
    for f in faltantes[:20]:
        print("   sin amarre → análisis", f[0], f[1], "msg", f[2], "overlay:", (f[3] or "")[:70])


async def main():
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[WaMediaCaso.__table__])
    async with AsyncSessionLocal() as db:
        if args:
            periodos = args
        else:
            comp = (await db.execute(select(func.max(AnsRegistro.periodo)).where(AnsRegistro.entorno == "real"))).scalar()
            periodos = [comp, _periodo_anterior(comp)] if comp else []
        for p in periodos:
            print(f"periodo {p}:", await matcher.amarrar(db, periodo=p))
        if "--verificar" in sys.argv:
            await verificar(db)


if __name__ == "__main__":
    asyncio.run(main())
