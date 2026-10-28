"""Barrido de pendientes del ANS de un periodo: corre el analista automático (router
3.2A/3.2B de services/wa_analista) sobre cada caso sin diligenciar que aún no tenga
análisis. Secuencial a propósito (whisper es CPU-pesado). Genera PROPUESTAS en el tab
Auditoría IA: nada se aplica solo.

Uso:  ./venv/bin/python scripts/barrido_agosto.py [YYYY-MM]   (default: 2026-08)

Trampa aprendida (2 crashes el 2026-08-13): tras un `rollback`, TODOS los objetos ORM de
la sesión expiran, y leer un atributo expirado dentro de un loop async dispara IO
síncrono → MissingGreenlet. Por eso el plan (id, pdl, zona) se materializa ANTES del
ciclo y cada registro se recarga fresco con db.get() en su iteración.
"""
import asyncio
import json
import sys

sys.path.insert(0, '/var/www/sabogal-platform/backend')
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models.models import AnsRegistro, WaAnalisis
from app.services import wa_analista

PERIODO = sys.argv[1] if len(sys.argv) > 1 else "2026-08"


async def main():
    async with AsyncSessionLocal() as db:
        from app.api.ans import computar, _es_anulado
        r = await db.execute(select(AnsRegistro).where(
            AnsRegistro.periodo == PERIODO, AnsRegistro.entorno == "real"))
        regs = r.scalars().all()
        r2 = await db.execute(select(WaAnalisis.reg_id).where(
            WaAnalisis.estado.in_(("propuesto", "aplicado"))))
        ya = {x for (x,) in r2.all()}

        # El plan se materializa AQUÍ (ver docstring): solo escalares, nada de ORM vivo
        plan = []
        for reg in regs:
            if reg.id in ya:
                continue
            anulado = _es_anulado(reg.estado_actual) or (reg.se_ejecuto or "").strip().upper() == "NO"
            if anulado:
                if not (reg.excluible_anulado or "").strip():
                    plan.append((reg.id, reg.pdl, reg.zona))
            else:
                c = computar(reg)
                if ("NO CUMPLE" in (c.get("cumple_cierre") or "", c.get("cumple_apertura") or "")
                        and not (reg.excluible or "").strip()):
                    plan.append((reg.id, reg.pdl, reg.zona))

        print(f"BARRIDO {PERIODO}: {len(plan)} casos por analizar", flush=True)
        for i, (rid, pdl, zona) in enumerate(plan, 1):
            try:
                reg = await db.get(AnsRegistro, rid)
                a = await wa_analista.analizar_caso(db, reg)
                res = json.loads(a.resultado)
                ia = res.get("ia") or {}
                if res.get("tipo") == "3.2B":
                    c0 = res.get("consulta") or {}
                    det = f"mensaje a {c0.get('persona')} ({c0.get('telefono') or 'SIN TELEFONO'})"
                else:
                    vids = [v for v in ia.get("videos", []) if v.get("del_caso") == "SI"]
                    det = (f"{ia.get('excluible_sugerido') or 'sin veredicto'} · "
                           f"{ia.get('categoria') or '-'} · conf {ia.get('confianza')} · "
                           f"{len(vids)} video(s) del caso")
                print(f"[{i}/{len(plan)}] OK   #{a.id} {res.get('tipo')} {pdl} ({zona}) -> {det}",
                      flush=True)
            except Exception as e:                      # noqa: BLE001 — un caso no tumba el barrido
                msg = str(e)
                await db.rollback()
                print(f"[{i}/{len(plan)}] FALLO {pdl} ({zona}): {msg}", flush=True)
        print("BARRIDO COMPLETO", flush=True)


asyncio.run(main())
