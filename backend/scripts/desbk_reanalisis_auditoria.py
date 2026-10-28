"""Re-análisis puntual (auditoría de Sergio 2026-08-24): corre el analista con el motor
nuevo de tiempos (tramos + tolerancia 10/10 + estado Revisión) sobre PDLs concretos.
Genera propuestas NUEVAS en el tab — no toca lo aplicado."""
import asyncio, json, sys
sys.path.insert(0, '/var/www/sabogal-platform/backend')
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.models import AnsRegistro
from app.services import wa_analista

PDLS = sys.argv[1:] or ["CDOPP2026R07474", "CDOPP2026R07773"]

async def main():
    async with AsyncSessionLocal() as db:
        plan = []
        for pdl in PDLS:
            r = await db.execute(select(AnsRegistro).where(
                AnsRegistro.pdl.ilike(f"%{pdl}%"), AnsRegistro.entorno == "real"))
            reg = r.scalars().first()
            if not reg:
                print(f"{pdl}: NO ENCONTRADO", flush=True); continue
            plan.append((reg.id, pdl))
        for rid, pdl in plan:
            reg = await db.get(AnsRegistro, rid)
            try:
                a = await wa_analista.analizar_caso(db, reg)
                res = json.loads(a.resultado)
                ia = res.get("ia") or {}
                cov = ia.get("cobertura_calc") or {}
                gaps = {g: f"{v['justificado']}/{v['requerido']}min cubierto={v['cubierto']}"
                        for g, v in (cov.get("gaps") or {}).items()}
                print(f"{pdl}: analisis #{a.id} sugerido={ia.get('excluible_sugerido')} "
                      f"revision={bool(ia.get('revision'))} causa_tipo={ia.get('causa_tipo')} "
                      f"causa={ia.get('causa')!r} gaps={gaps}", flush=True)
                print(f"  OBS: {(ia.get('observacion') or '')[:400]}", flush=True)
            except Exception as e:
                await db.rollback()
                print(f"{pdl}: ERROR {type(e).__name__}: {e}", flush=True)

asyncio.run(main())
