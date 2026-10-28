"""Altas de personal del 2026-09-24 (pedidas por WhatsApp, remitidas por Daniel Patiño):

  · carlos.ramirez → Carlos Ramírez, ingeniero de APOYO de UBATE. Mismo molde que Jessuad:
    rol `ingeniero_zona` + zona UBATE. Ve toda la programación pero solo edita UBATE, y en el
    ANS sustenta SUS casos (sin cargar STM ni borrar periodos). Su acceso al ANS y al módulo
    SGS Exporte NO sale del rol: sale de config.USUARIOS_APOYO y config.USUARIOS_SGS_EXPORTE,
    donde ya está añadido. OJO: puede que acabe reemplazando a Jessuad, pero NO está
    confirmado — Jessuad conserva intactos rol, zona, apoyo y SGS Exporte.

  · diana → Diana, Planner (asistente del coordinador de zona Daniel Patiño). Sergio pidió
    «el mismo poder que Daniel»: rol `supervisor`, unidad SABOGAL, sin zona. Eso implica lo
    mismo que tiene él — incluida la Relación con SGS (escribe MO real) y el alta de usuarios.

Idempotente: si la cuenta ya existe, no la toca.
Uso:  PASS_CARLOS='...' PASS_DIANA='...' python scripts/crear_cuentas_20260924.py
Las contraseñas van por variable de entorno (nunca al repo).
"""
import os
import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.models import Usuario, RolEnum, ZonaEnum
from app.services.auth import hash_password

# login (email), nombre, rol, unidad, zona, env con la contraseña
CUENTAS = [
    ("carlos.ramirez", "Carlos Ramírez (Apoyo UBATE)",
     RolEnum.ingeniero_zona, "SABOGAL", ZonaEnum.UBATE, "PASS_CARLOS"),
    ("diana", "Diana (Planner)",
     RolEnum.supervisor, "SABOGAL", None, "PASS_DIANA"),
]


async def main():
    async with AsyncSessionLocal() as db:
        for email, nombre, rol, unidad, zona, env in CUENTAS:
            existing = (await db.execute(
                select(Usuario).where(Usuario.email == email)
            )).scalar_one_or_none()
            if existing:
                print(f"YA EXISTE: {email} (rol={existing.rol.value}, unidad={existing.unidad}, "
                      f"zona={existing.zona.value if existing.zona else '-'}, activo={existing.activo})")
                continue
            db.add(Usuario(
                nombre=nombre, email=email,
                hashed_password=hash_password(os.environ[env]),
                rol=rol, unidad=unidad, zona=zona, activo=True,
            ))
            print(f"CREADO: {email} -> {nombre} ({rol.value}, {unidad}, "
                  f"zona={zona.value if zona else '-'})")
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
