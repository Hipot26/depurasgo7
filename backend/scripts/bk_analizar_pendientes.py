"""Auditoría IA — ciclo NOCTURNO (2026-09-07): analiza solo los casos del
periodo competente que deberían estar en el tab y no están (3.2 A con tiempos NO CUMPLE y
3.2 B anulados, sin decisión y sin análisis vigente). Complementa al disparo que ya hace la
carga de la STM y al botón «Analizar pendientes» del tab: atrapa el material que llega
después (videos subidos al día siguiente) sin que nadie tenga que acordarse.

Uso (en el VPS, desde backend/):
  venv/bin/python scripts/analizar_pendientes.py                # periodo competente
  venv/bin/python scripts/analizar_pendientes.py 2026-09        # un periodo concreto
  venv/bin/python scripts/analizar_pendientes.py 2026-09 --reintentar   # también los que fallaron

Un fallo de la API (p. ej. sin créditos) queda persistido como análisis con error — visible
en el tab; NO se reintenta solo cada noche (eso sería gastar whisper y frames en vano):
el reintento es manual (botón «Reintentar fallidos» o --reintentar)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sqlalchemy import select, func  # noqa: E402

from app.db.database import AsyncSessionLocal  # noqa: E402
from app.models.models import AnsRegistro  # noqa: E402
from app.services import wa_analista  # noqa: E402


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    reintentar = "--reintentar" in sys.argv
    periodo = args[0] if args else None
    if not periodo:
        async with AsyncSessionLocal() as db:
            periodo = (await db.execute(select(func.max(AnsRegistro.periodo))
                                        .where(AnsRegistro.entorno == "real"))).scalar()
    if not periodo:
        print("sin periodo")
        return
    print(f"periodo {periodo} · reintentar={reintentar}")
    hechos = await wa_analista.analizar_pendientes(periodo, reintentar)
    for h in hechos:
        print("  ", "✗" if h.get("error") else "✓", h["pdl"], "·", h["motivo"], "·", h.get("error") or f"análisis #{h.get('analisis_id')}")
    print(f"listo: {len(hechos)} caso(s)")


if __name__ == "__main__":
    asyncio.run(main())
