"""Deduce la zona de las órdenes FT SIN zona de los periodos abiertos (mismo código que el botón
«Sin zona» del ANS: services/zona_deduccion.py). Primero el directorio de hoy, después el consenso
de señales (≥2 iguales y ninguna en contra). Las que no alcanzan siguen sin zona (Paso 1).

Uso (en el VPS, desde backend/):
  PYTHONPATH=. venv/bin/python scripts/deducir_zonas.py            # vista previa, no escribe
  PYTHONPATH=. venv/bin/python scripts/deducir_zonas.py --aplicar  # escribe zona + AnsCambio + origen
"""
import sys
import asyncio

from app.db.database import AsyncSessionLocal
from app.services import zona_deduccion as zd


async def main():
    aplicar = "--aplicar" in sys.argv
    async with AsyncSessionLocal() as db:
        per = await zd.periodos_trabajo(db)
        r = await zd.resolver_sin_zona(db, per, aplicar=aplicar, usuario_nombre="Sistema (zona deducida)")
        print(f"periodos {per} · sin zona: {len(r['filas'])} · con zona: {r['resueltas']} · "
              f"{'APLICADO' if aplicar else 'vista previa (con --aplicar se escribe)'}")
        for f in r["filas"]:
            v = ", ".join(f"{k}={x['zona']}" for k, x in f["votos"].items()) or "-"
            print(f"  {f['pdl']} {f['periodo']} sup={f['supervisor']!r}: "
                  f"{f['zona'] or 'Paso 1'} ({f['metodo']}) [{v}]")
        if not aplicar:
            await db.rollback()


if __name__ == "__main__":
    asyncio.run(main())
