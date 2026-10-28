"""Crea las cuentas de los CUMPLIMENTADORES (rol cumplimentador: solo módulo ANS,
pendiente = columna AQ en filas ejecutadas). Idempotente, como crear_cuentas.py.

Nombres y correos enviados por Cristian Santana (WhatsApp, 14/07/2026).
Uso:  NEW_PASS='...' python -m scripts.crear_cumplimentadores
La contraseña se pasa por variable de entorno (no se guarda en el repo).

Requiere que el enum rolenum ya tenga el valor 'cumplimentador'
(migración 2026-08-03_rol_cumplimentador.sql).
"""
import os
import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.models import Usuario, RolEnum
from app.services.auth import hash_password

PASSWORD = os.environ["NEW_PASS"]

# login (email), nombre
CUENTAS = [
    ("andres.almanza@dominion-global.com",       "Andrés Almanza"),
    ("programadordescargosz7@dominion-global.com", "Juan Carlos Prieto Bejarano"),
]


async def main():
    async with AsyncSessionLocal() as db:
        for email, nombre in CUENTAS:
            existing = (await db.execute(
                select(Usuario).where(Usuario.email == email)
            )).scalar_one_or_none()
            if existing:
                print(f"YA EXISTE: {email} (rol={existing.rol.value})")
                continue
            db.add(Usuario(
                nombre=nombre, email=email,
                hashed_password=hash_password(PASSWORD),
                rol=RolEnum.cumplimentador, unidad="SABOGAL", activo=True,
            ))
            print(f"CREADO: {email} -> {nombre} (cumplimentador, SABOGAL)")
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
