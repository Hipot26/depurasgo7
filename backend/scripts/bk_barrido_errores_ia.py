"""Barrido de lecturas IA que quedaron en error («IA no disponible…» u otro) y siguen VIGENTES.

Uso:  PYTHONPATH=. venv/bin/python scripts/barrido_errores_ia.py [--json salida.json]

Mira las tres fuentes donde la máquina guarda lecturas:
  1. correo_ans.analisis   — slot {reg_id: análisis} de cada correo (Correos ANS)
  2. wa_analisis           — Auditoría IA; cuenta solo el análisis VIGENTE por caso+campo
                             (el más nuevo no descartado), como lo muestra el tab
  3. ans_consulta_msgs     — turnos «ia» de los hilos; cuenta solo si el ÚLTIMO turno ia
                             del hilo es un error (si hubo lectura buena después, ya se saldó)
Solo lee: no toca nada.
"""
import asyncio
import json
import sys

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models.models import AnsConsulta, AnsConsultaMsg, AnsRegistro, CorreoAns, WaAnalisis


def _es_error(txt) -> bool:
    return bool(txt) and ("no disponible" in str(txt).lower() or "falló" in str(txt).lower())


async def main():
    out = {"correos": [], "auditoria": [], "hilos": []}
    async with AsyncSessionLocal() as db:
        regs = {r.id: r for r in (await db.execute(select(AnsRegistro))).scalars().all()}

        def caso(reg_id):
            r = regs.get(reg_id)
            return {"reg_id": reg_id, "pdl": r.pdl if r else None, "periodo": r.periodo if r else None,
                    "zona": r.zona if r else None, "entorno": r.entorno if r else None}

        # 1. Correos ANS
        for c in (await db.execute(select(CorreoAns).where(CorreoAns.estado != "descartado"))).scalars().all():
            try:
                an = json.loads(c.analisis) if c.analisis else {}
            except ValueError:
                continue
            if not isinstance(an, dict):
                continue
            slots = {str(an["reg_id"]): an} if "reg_id" in an else an
            for rid, s in slots.items():
                if isinstance(s, dict) and (s.get("error") or _es_error(s.get("razon"))):
                    out["correos"].append({**caso(int(rid)), "correo_id": c.id, "campo": s.get("campo"),
                                           "asunto": (c.asunto or "")[:90], "estado_correo": c.estado,
                                           "error": s.get("error") or s.get("razon"), "cuando": s.get("cuando")})

        # 2. Auditoría IA — vigente por caso+campo
        vig = {}
        for a in (await db.execute(select(WaAnalisis).where(WaAnalisis.estado != "descartado")
                                   .order_by(WaAnalisis.id))).scalars().all():
            vig[(a.reg_id, a.campo, a.entorno)] = a
        for (rid, campo, ent), a in vig.items():
            try:
                res = json.loads(a.resultado or "{}")
            except ValueError:
                res = {}
            err = (res.get("ia") or {}).get("error") or res.get("error")
            if err:
                out["auditoria"].append({**caso(rid), "analisis_id": a.id, "campo": campo, "estado": a.estado,
                                         "tipo": res.get("tipo"), "error": err, "cuando": str(a.creado_en)[:16]})

        # 3. Hilos — último turno ia en error
        for con in (await db.execute(select(AnsConsulta))).scalars().all():
            ms = (await db.execute(select(AnsConsultaMsg).where(AnsConsultaMsg.consulta_id == con.id,
                                                                AnsConsultaMsg.direccion == "ia")
                                   .order_by(AnsConsultaMsg.creado_en, AnsConsultaMsg.id))).scalars().all()
            if ms and _es_error(ms[-1].texto):
                out["hilos"].append({**caso(con.reg_id), "hilo": con.id, "campo": con.campo, "estado": con.estado,
                                     "error": ms[-1].texto, "cuando": str(ms[-1].creado_en)[:16]})

    for k, v in out.items():
        print(f"### {k}: {len(v)}")
        for x in v:
            print("  ", json.dumps(x, ensure_ascii=False))
    if "--json" in sys.argv:
        json.dump(out, open(sys.argv[sys.argv.index("--json") + 1], "w"), ensure_ascii=False, indent=1)


asyncio.run(main())
