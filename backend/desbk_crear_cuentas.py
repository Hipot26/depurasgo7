"""Crea/actualiza cuentas de usuario de forma idempotente.
Uso:  NEW_PASS='...' python scripts/crear_cuentas.py
La contraseña se pasa por variable de entorno (no se guarda en el repo)."""
import os
import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.models import Usuario, RolEnum
from app.services.auth import hash_password

PASSWORD = os.environ["NEW_PASS"]

# login (email), nombre, rol, unidad
CUENTAS = [
    ("monica",   "Mónica Suárez",    RolEnum.directivo,  "SABOGAL"),
    ("cristian", "Cristian Santana", RolEnum.directivo,  "SABOGAL"),
    ("agobardo", "Agobardo",         RolEnum.supervisor, "CHOCONTA"),
    ("enel",     "Cliente",          RolEnum.cliente,    "SABOGAL"),
]


async def main():
    async with AsyncSessionLocal() as db:
        for email, nombre, rol, unidad in CUENTAS:
            existing = (await db.execute(
                select(Usuario).where(Usuario.email == email)
            )).scalar_one_or_none()
            if existing:
                print(f"YA EXISTE: {email} (rol={existing.rol.value})")
                continue
            db.add(Usuario(
                nombre=nombre, email=email,
                hashed_password=hash_password(PASSWORD),
                rol=rol, unidad=unidad, activo=True,
            ))
            print(f"CREADO: {email} -> {nombre} ({rol.value}, {unidad})")
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
