"""Configura la cuenta de Agobardo para el módulo Bolsas MOES.

El acceso a Bolsas MOES se decide por UNIDAD (CHOCONTA), no por un rol especial:
Agobardo es **supervisor de CHOCONTA**, así conserva la programación/matriz de su
unidad y, por ser de la unidad MOES, ve Bolsas MOES en vez de las bolsas generales.

Este script solo se asegura de que la cuenta `agobardo` sea supervisor + CHOCONTA
(idempotente). El rol `moes` quedó deprecado; si una cuenta lo tuviera, la corrige.

Uso:  PYTHONPATH=. python scripts/migrar_agobardo_moes.py
"""
import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.models.models import Usuario, RolEnum

EMAIL = "agobardo"


async def main():
    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(Usuario).where(Usuario.email == EMAIL))).scalar_one_or_none()
        if not u:
            print(f"NO EXISTE la cuenta '{EMAIL}'. Créala primero con crear_cuentas.py")
            return
        antes = u.rol.value if hasattr(u.rol, "value") else u.rol
        cambios = []
        if antes != RolEnum.supervisor.value:
            u.rol = RolEnum.supervisor
            cambios.append(f"rol {antes} -> supervisor")
        if u.unidad != "CHOCONTA":
            cambios.append(f"unidad {u.unidad} -> CHOCONTA")
            u.unidad = "CHOCONTA"
        if not cambios:
            print(f"{EMAIL} ya es supervisor / CHOCONTA. Nada que hacer.")
            return
        await db.commit()
        print(f"{EMAIL}: " + ", ".join(cambios))


if __name__ == "__main__":
    asyncio.run(main())
