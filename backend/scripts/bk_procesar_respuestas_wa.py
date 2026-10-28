"""Lanza a mano el barrido de respuestas de WhatsApp (el mismo que dispara el bot a los 90 s
de cada DM y en su ciclo de 3 h): DMs nuevos de contactos con hilo abierto → turno recibido +
evidencia + bitácora por IA. Útil cuando el bot no lo disparó o el barrido quedó corto.

    cd backend && venv/bin/python scripts/procesar_respuestas_wa.py
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.db.database import AsyncSessionLocal  # noqa: E402
from app.services import wa_respuestas  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def main():
    async with AsyncSessionLocal() as db:
        hechos = await wa_respuestas.procesar_pendientes(db)
    print(json.dumps({"procesados": len(hechos), "detalle": hechos}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
