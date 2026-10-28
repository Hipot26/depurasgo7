"""Verificación del EMBUDO (2026-09-24) contra los casos reales que ya respondieron.

No escribe nada: solo construye el embudo de cada caso con respuesta y lo imprime —
paso, calidad, cobertura sumando los dos canales, y el borrador del siguiente mensaje.
Con --releer=<analisis_id> sí vuelve a leer ESE hilo con la IA (transcribe los adjuntos
que nunca se leyeron y reclasifica).

    cd backend && venv/bin/python scripts/verificar_embudo.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sqlalchemy import select                                          # noqa: E402
from app.db.database import AsyncSessionLocal                          # noqa: E402
from app.models.models import WaAnalisis, AnsRegistro                  # noqa: E402
from app.api import wa_auditoria as wa                                 # noqa: E402


async def main():
    releer = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--releer=")), None)
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(WaAnalisis)
                             .where(WaAnalisis.entorno == "real",
                                    WaAnalisis.estado != "descartado")
                             .order_by(WaAnalisis.id.desc()).limit(60))
        vistos, n = set(), 0
        for a in r.scalars().all():
            if a.reg_id in vistos:
                continue
            vistos.add(a.reg_id)
            reg = await db.get(AnsRegistro, a.reg_id)
            if not reg:
                continue
            res = json.loads(a.resultado or "{}")
            flujo = await wa._flujo(db, a, res)
            if flujo.get("flujo") != "respondido":
                continue
            emb = await wa._embudo(db, reg, res, a)
            n += 1
            print("=" * 78)
            print(f"#{a.id} {reg.pdl} · {reg.zona} · {res.get('tipo') or '3.2A'} · flujo={flujo}")
            if not emb:
                print("  (sin hilo)")
                continue
            print(f"  paso={emb['paso']}  calidad={emb['calidad']}  persona={emb.get('persona')} "
                  f"tel={emb.get('telefono')}")
            print(f"  ultima: {(emb.get('ultima_respuesta') or '')[:180]!r}")
            print(f"  falta={emb.get('falta')!r}  pregunta={emb.get('pregunta_pendiente')!r}")
            print(f"  linea_faltante={emb.get('linea_faltante')!r}")
            cov = emb.get("cobertura")
            if cov and cov.get("aplicable"):
                print("  cobertura: " + " · ".join(
                    f"{g} {v['justificado']}/{v['requerido']} min (faltan {v['faltan']})"
                    for g, v in cov["gaps"].items()) + f"  cubierto={cov['cubierto']}")
            print(f"  decision_sug={emb.get('decision_sugerida')}  obs_sug="
                  f"{(emb.get('observacion_sugerida') or '')[:120]!r}")
            if emb.get("mensaje"):
                print("  --- siguiente mensaje propuesto ---")
                for ln in emb["mensaje"].splitlines():
                    print("  | " + ln)
        print("=" * 78)
        print(f"{n} caso(s) con respuesta")

        if releer:
            print(f"\n>>> RELEYENDO el análisis #{releer} con la IA…")
            out = await wa.releer(int(releer), db)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            a = await db.get(WaAnalisis, int(releer))
            reg = await db.get(AnsRegistro, a.reg_id)
            emb = await wa._embudo(db, reg, json.loads(a.resultado or "{}"), a)
            print("\n>>> EMBUDO tras la relectura:")
            print(json.dumps({k: v for k, v in (emb or {}).items() if k != "cobertura"},
                             ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
