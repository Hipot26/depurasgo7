"""Relevo de coordinadores (2026-09-07): Sabogal y Agobardo ya no están.
  · daniel.patino@dominion-global.com  → Coordinador de PROGRAMADOS    (unidad SABOGAL,  antes Ing. Sabogal)
  · david.tous@dominion-global.com     → Coordinador de NO PROGRAMADOS (unidad CHOCONTA, antes Agobardo)
Mismo rol `supervisor` que sus antecesores (mismos permisos por unidad). Idempotente.
Uso:  PASS_DANIEL='...' PASS_DAVID='...' DESACTIVAR_VIEJOS=1 python scripts/crear_coordinadores.py
Las contraseñas van por variable de entorno (no se guardan en el repo).
DESACTIVAR_VIEJOS=1 pone activo=false a las cuentas `sabogal` y `agobardo` (reversible)."""
import os
import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.models import Usuario, RolEnum
from app.services.auth import hash_password

# login (email), nombre, rol, unidad, env con la contraseña
CUENTAS = [
    ("daniel.patino@dominion-global.com", "Daniel Patiño (Coord. programados)",
     RolEnum.supervisor, "SABOGAL", "PASS_DANIEL"),
    ("david.tous@dominion-global.com", "David Tous (Coord. no programados)",
     RolEnum.supervisor, "CHOCONTA", "PASS_DAVID"),
]
VIEJOS = ["sabogal", "agobardo"]


async def main():
    async with AsyncSessionLocal() as db:
        for email, nombre, rol, unidad, env in CUENTAS:
            existing = (await db.execute(
                select(Usuario).where(Usuario.email == email)
            )).scalar_one_or_none()
            if existing:
                print(f"YA EXISTE: {email} (rol={existing.rol.value}, unidad={existing.unidad}, activo={existing.activo})")
                continue
            db.add(Usuario(
                nombre=nombre, email=email,
                hashed_password=hash_password(os.environ[env]),
                rol=rol, unidad=unidad, zona=None, activo=True,
            ))
            print(f"CREADO: {email} -> {nombre} ({rol.value}, {unidad})")
        if os.environ.get("DESACTIVAR_VIEJOS") == "1":
            for login in VIEJOS:
                u = (await db.execute(select(Usuario).where(Usuario.email == login))).scalar_one_or_none()
                if u and u.activo:
                    u.activo = False
                    print(f"DESACTIVADO: {login} ({u.nombre})")
                else:
                    print(f"SIN CAMBIO: {login} ({'ya inactivo' if u else 'no existe'})")
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
