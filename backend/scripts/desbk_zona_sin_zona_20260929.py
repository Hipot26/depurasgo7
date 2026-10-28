"""Corrección 2026-09-29 (aprobada por Sergio en chat): dos órdenes 3.2 B sin zona se le preguntaron a
Jhon Quiroga (apoyo GUAVIO) porque la búsqueda del apoyo con zona vacía calzaba con cualquiera.
El código ya quedó con la guarda; esto arregla los DATOS que causaron el hueco:

  1. «CARLOS MAURICIO FONSECA NOVOA» (contacto con el celular de Carlos Fonseca, GUAVIO) no tenía zona.
  2. «OMAR GOMEZ» (así lo escribe el CD10) no calzaba con «OMAR ORLANDO GOMEZ» (supervisor UBATE):
     todos los líderes de sus órdenes trabajan en UBATE. Se crea el alias, como los del 08-09.
  3. Las órdenes de 2026-09 que siguen sin zona y ahora resuelven por el directorio reciben la zona,
     con su AnsCambio (R08981 → GUAVIO; R10042, R10033, R09552 → UBATE). «OMAR GOMEZ REINA» NO se toca:
     falta que Sergio confirme que es la misma persona.
  4. R10042 se le había preguntado a Jhon Quiroga: se deja una propuesta 3.2 B NUEVA (estado «propuesto»)
     para el apoyo de UBATE. NO se envía nada: sale solo con el clic de Sergio en el tab.

Uso (en el VPS, desde backend/): PYTHONPATH=. venv/bin/python scripts/zona_sin_zona_20260929.py [--aplicar]
Sin --aplicar solo muestra. Idempotente.
"""
import sys
import asyncio

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models.models import AnsRegistro, AnsCambio, Contacto, Usuario, WaAnalisis
from app.api.ans import _norm_nombre, _zona_map, _zona_de

APLICAR = "--aplicar" in sys.argv
PERIODO = "2026-09"
FONSECA = "CARLOS MAURICIO FONSECA NOVOA"
ALIAS = ("OMAR GOMEZ", "UBATE",
         "Alias de OMAR ORLANDO GOMEZ (UBATE): el CD10 lo trae como «OMAR GOMEZ». Todos los líderes de sus "
         "órdenes trabajan en UBATE. 2026-09-29 (Sergio en chat).")
RE_PREGUNTAR = "CDOPP2026R10042"


async def main():
    async with AsyncSessionLocal() as db:
        dev = (await db.execute(select(Usuario).where(Usuario.nombre == "Sergio (Dev)"))).scalars().first()
        uid = dev.id if dev else None

        # 1. Fonseca largo → GUAVIO
        c = (await db.execute(select(Contacto).where(Contacto.nombre_norm == _norm_nombre(FONSECA)))).scalar_one_or_none()
        if c is None:
            print("OJO: no existe", FONSECA)
        elif (c.zona or "").upper() != "GUAVIO":
            print(f"contacto {c.id} {c.nombre}: zona {c.zona!r} -> 'GUAVIO'")
            c.zona = "GUAVIO"
            c.notas = ((c.notas or "").rstrip() + "\n2026-09-29: zona GUAVIO (mismo celular que CARLOS FONSECA, "
                       "34 órdenes en GUAVIO). Sin zona, R08981 quedó sin zona.").strip()
        else:
            print(f"contacto {c.id} {c.nombre}: ya en GUAVIO")

        # 2. Alias OMAR GOMEZ → UBATE
        nombre, zona, nota = ALIAS
        n = _norm_nombre(nombre)
        a = (await db.execute(select(Contacto).where(Contacto.nombre_norm == n))).scalar_one_or_none()
        if a is None:
            db.add(Contacto(nombre=nombre, nombre_norm=n, telefono="", rol="supervisor", zona=zona,
                            activo=True, creado_por=uid, notas=nota))
            print(f"alias creado: {nombre} -> {zona}")
        else:
            print(f"alias ya existía: id={a.id} {a.nombre} rol={a.rol} zona={a.zona} activo={a.activo}")
        await db.flush()

        # 3. Zona de las órdenes del periodo que ahora resuelven
        zm = await _zona_map(db)
        filas = (await db.execute(select(AnsRegistro).where(
            AnsRegistro.entorno == "real", AnsRegistro.periodo == PERIODO, AnsRegistro.tipo == "FT",
            AnsRegistro.zona.is_(None)).order_by(AnsRegistro.pdl))).scalars().all()
        for f in filas:
            z = _zona_de("FT", f.nombre_responsable, zm)
            if z:
                print(f"{f.pdl} (sup {f.nombre_responsable!r}, se_ejecuto={f.se_ejecuto}): zona -> {z}")
                f.zona = z
                db.add(AnsCambio(registro_id=f.id, periodo=f.periodo, protocolo=f.protocolo, campo="zona",
                                 valor_anterior="", valor_nuevo=z, tipo="edicion",
                                 usuario_id=uid, usuario_nombre="Sergio (Dev)"))
            else:
                print(f"{f.pdl} (sup {f.nombre_responsable!r}): sigue SIN zona")
        await db.flush()

        # 4. Propuesta nueva de R10042 para el apoyo de su zona (no envía nada)
        reg = (await db.execute(select(AnsRegistro).where(AnsRegistro.entorno == "real",
                                                          AnsRegistro.pdl == RE_PREGUNTAR))).scalars().first()
        ya = (await db.execute(select(WaAnalisis).where(WaAnalisis.reg_id == reg.id, WaAnalisis.estado == "propuesto"))
              ).scalars().first() if reg else None
        if not reg or not reg.zona:
            print(f"{RE_PREGUNTAR}: sin zona todavía, no se propone nada")
        elif ya:
            print(f"{RE_PREGUNTAR}: ya hay una propuesta pendiente (análisis {ya.id})")
        elif APLICAR:
            await db.commit()                     # _proponer_consulta_32b hace su propio commit
            from app.services.wa_analista import _proponer_consulta_32b
            import json
            nuevo = await _proponer_consulta_32b(db, reg)
            c0 = json.loads(nuevo.resultado).get("consulta") or {}
            print(f"{RE_PREGUNTAR}: propuesta nueva análisis {nuevo.id} -> {c0.get('persona')} +{c0.get('telefono')}")
        else:
            print(f"{RE_PREGUNTAR}: se crearía una propuesta 3.2 B nueva para el apoyo de {reg.zona}")

        if APLICAR:
            await db.commit()
            print("COMMIT")
        else:
            await db.rollback()
            print("(solo lectura; con --aplicar se aplica)")


asyncio.run(main())
