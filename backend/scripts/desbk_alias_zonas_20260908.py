"""Corrección de datos 2026-09-08 (pedido de Sergio): el CD10 escribe a algunos supervisores con
otra grafía y esas órdenes quedan sin zona (el reporte las manda a UBATE por defecto). Se crean
los alias en el directorio (lo mismo que aprende el grid al corregir la zona a mano) y se resuelven
las filas de agosto que sigan sin zona. Idempotente.

Uso (en el VPS): PYTHONPATH=. venv/bin/python scripts/alias_zonas_20260908.py
"""
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.config import settings
from app.models.models import AnsRegistro, AnsCambio, Contacto, Usuario
from app.api.ans import _norm_nombre, _zona_map, _zona_de

ALIAS = [
    ("WILLINGTON RODRIGUEZ", "ALMEIDAS",
     "Alias: el CD10 escribe WILLINTON / WILLINTONG / WILLINGTON RODRIGUEZ (mismo supervisor de "
     "ALMEIDAS). Zona aprendida al corregir el ANS, 2026-09-08."),
    ("CARLOS MAURICIO FONSECA", "GUAVIO",
     "Alias de CARLOS FONSECA (GUAVIO): el CD10 lo trae con segundo nombre. 2026-09-08."),
    ("JUAN MEJIA", "UBATE",
     "Alias de JUAN CARLOS MEJIA (UBATE): el CD10 lo trae sin segundo nombre. 2026-09-08."),
]


async def main():
    eng = create_async_engine(settings.DATABASE_URL)
    async with AsyncSession(eng) as db:
        u = (await db.execute(select(Usuario).where(Usuario.nombre == "Sergio (Dev)"))).scalars().first()
        uid = u.id if u else None

        for nombre, zona, nota in ALIAS:
            n = _norm_nombre(nombre)
            if (await db.execute(select(Contacto).where(Contacto.nombre_norm == n))).scalar_one_or_none():
                print("ya existía:", nombre)
                continue
            db.add(Contacto(nombre=nombre, nombre_norm=n, telefono="", rol="supervisor", zona=zona,
                            activo=True, creado_por=uid, notas=nota))
            print("alias creado:", nombre, "->", zona)
        await db.flush()

        zm = await _zona_map(db)
        filas = (await db.execute(select(AnsRegistro).where(
            AnsRegistro.entorno == "real", AnsRegistro.periodo == "2026-08", AnsRegistro.tipo == "FT",
            AnsRegistro.zona.is_(None)))).scalars().all()
        for f in filas:
            z = _zona_de("FT", f.nombre_responsable, zm)
            if z:
                f.zona = z
                db.add(AnsCambio(registro_id=f.id, periodo=f.periodo, protocolo=f.protocolo, campo="zona",
                                 valor_anterior="", valor_nuevo=z, tipo="edicion",
                                 usuario_id=uid, usuario_nombre="Sergio (Dev)"))
                print(f"{f.pdl} ({f.nombre_responsable}): zona -> {z}")
            else:
                print(f"{f.pdl} ({f.nombre_responsable}): sigue SIN zona")

        await db.commit()
        print("COMMIT")
    await eng.dispose()


asyncio.run(main())
