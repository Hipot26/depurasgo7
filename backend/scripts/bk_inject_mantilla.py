"""Inyección manual de la plantilla Mantilla en la programación (CHOCONTA por defecto).
Usa el mismo parser y cruce que el endpoint (app.services.mantilla): 1º por código en
notas, 2º por nombre de líder del encabezado; estampa código en notas y catálogo.
Uso: python scripts/inject_mantilla.py /ruta/Libro.xlsx [UNIDAD] [--dry-run]
"""
import sys
import asyncio
import openpyxl
from app.db.database import AsyncSessionLocal
from app.services.mantilla import parse_seguimiento, cruzar_mantilla

args = [a for a in sys.argv[1:] if a != "--dry-run"]
DRY = "--dry-run" in sys.argv
PATH = args[0] if args else "/tmp/Libro2.xlsx"
UNI = args[1] if len(args) > 1 else "CHOCONTA"


async def main():
    wb = openpyxl.load_workbook(PATH, data_only=True)
    celdas = parse_seguimiento(wb)
    async with AsyncSessionLocal() as db:
        upd, sin_match = await cruzar_mantilla(db, UNI, celdas)
        if DRY:
            await db.rollback()
        else:
            await db.commit()
    print(f"celdas={len(celdas)} actualizadas={upd} sin_match={len(sin_match)}{' [DRY-RUN]' if DRY else ''}")
    for c, m in sin_match:
        print(f"  {c['codigo']} · {c['fecha']} · {c.get('lider') or '(sin nombre)'} — {m or 'sin fila ese día'}")


if __name__ == "__main__":
    asyncio.run(main())
