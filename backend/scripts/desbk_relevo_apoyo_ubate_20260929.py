"""Relevo del profesional de apoyo de UBATE — 2026-09-29 (Sergio por chat, con pantallazo del
WhatsApp de «ING Carlos»): sale JESSUAD AVILA, entra CARLOS RAMIREZ con el corporativo de UBATE
(+57 314 3690162 — el mismo número que tenía Geraldine Mendez cuando era la apoyo).

Por qué basta con el directorio: la Auditoría IA resuelve al apoyo EN VIVO (contactos activos con
rol «apoyo» y zona: `wa_auditoria.py` ~l.897, `wa_analista.py` ~l.137 y `_apoyo_map` en `ans.py`),
así que todas las propuestas 3.2 B de UBATE pendientes pasan a Carlos sin regenerar nada.
`_zona_apoyo_de` cruza el nombre de la cuenta «Carlos Ramírez (Apoyo UBATE)» con el del contacto
(«CARLOS RAMIREZ» tras normalizar), por eso el contacto se llama así. El saludo sale «Ing. Carlos».

Nada se borra: Jessuad SIGUE ACTIVO con rol «otro» y sin zona (el pareo de respuestas en
`wa_respuestas.py` solo indexa contactos activos y le quedan hilos abiertos esperándolo; ningún
flujo lo elige como apoyo porque todos filtran rol «apoyo») y Geraldine sigue inactiva con su
número (los envíos del 07-09 a ese número siguen amarrados a ella).
NO toca cuentas (`usuarios`) ni config.USUARIOS_APOYO / USUARIOS_SGS_EXPORTE: eso lo decide Sergio.

Uso (en el VPS, desde /var/www/sabogal-platform/backend):
  PYTHONPATH=. venv/bin/python scripts/relevo_apoyo_ubate_20260929.py            # solo muestra
  PYTHONPATH=. venv/bin/python scripts/relevo_apoyo_ubate_20260929.py --aplicar  # aplica (idempotente)
"""
import sys
import asyncio

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models.models import Contacto, Usuario, AnsConsulta, AnsRegistro, WaEnvio
from app.api.ans import _norm_nombre

SALE = "JESSUAD AVILA"
ENTRA = "CARLOS RAMIREZ"
TEL = "573143690162"          # corporativo de UBATE (antes Geraldine Mendez)
ZONA = "UBATE"
FECHA = "2026-09-29"
NOTA_ENTRA = (f"Profesional de apoyo de UBATE desde {FECHA} (relevo de Jessuad Ávila; Sergio por chat). "
              "Corporativo de UBATE, el mismo número que tuvo Geraldine Mendez. En WhatsApp figura como «ING Carlos».")
NOTA_SALE = (f"{FECHA}: relevado por Carlos Ramírez como apoyo de UBATE (Sergio por chat). Sigue activo con rol "
             "«otro» y sin zona solo para que sus respuestas a los hilos abiertos pareen.")
NOTA_SALE_VIEJA = f"{FECHA}: relevado por Carlos Ramírez como apoyo de UBATE (Sergio por chat); queda inactivo."
NOTA_GERALDINE = f"{FECHA}: su corporativo (+57 314 3690162) pasó a Carlos Ramírez, nuevo apoyo de UBATE."


def _fila(c: Contacto) -> str:
    return (f"  id={c.id!s:<4} {c.nombre:<28} tel={c.telefono or '-':<13} rol={c.rol or '-':<12} "
            f"zona={c.zona or '-':<8} activo={c.activo}")


async def mostrar(db):
    print("== Contactos rol apoyo (activos e inactivos) ==")
    r = await db.execute(select(Contacto).where(Contacto.rol == "apoyo").order_by(Contacto.zona, Contacto.id))
    for c in r.scalars().all():
        print(_fila(c))

    print(f"== Contactos con el número {TEL} ==")
    filas = (await db.execute(select(Contacto).where(Contacto.telefono == TEL).order_by(Contacto.id))).scalars().all()
    for c in filas:
        print(_fila(c))
    if not filas:
        print("  (ninguno)")

    print(f"== Contacto {ENTRA} por nombre ==")
    c = (await db.execute(select(Contacto).where(Contacto.nombre_norm == _norm_nombre(ENTRA)))).scalar_one_or_none()
    print(_fila(c) if c else "  (no existe)")

    print(f"== Contacto {SALE} por nombre ==")
    j = (await db.execute(select(Contacto).where(Contacto.nombre_norm == _norm_nombre(SALE)))).scalar_one_or_none()
    print(_fila(j) if j else "  (no existe)")

    print("== Cuenta carlos.ramirez ==")
    u = (await db.execute(select(Usuario).where(Usuario.email == "carlos.ramirez"))).scalar_one_or_none()
    print(f"  id={u.id} nombre={u.nombre!r} rol={u.rol.value} zona={u.zona.value if u.zona else '-'} "
          f"activo={u.activo} -> cruza con el directorio como {_norm_nombre(u.nombre)!r}" if u else "  (no existe)")

    print("== Hilos abiertos esperando a Jessuad (ans_consultas) ==")
    hilos = (await db.execute(select(AnsConsulta).where(
        AnsConsulta.estado == "abierta", AnsConsulta.persona_actual.ilike("%JESSUAD%")))).scalars().all()
    if hilos:
        regs = {x.id: x for x in (await db.execute(select(AnsRegistro).where(
            AnsRegistro.id.in_([h.reg_id for h in hilos])))).scalars().all()}
        for h in hilos:
            rg = regs.get(h.reg_id)
            print(f"  consulta {h.id}: {rg.pdl if rg else h.reg_id} ({rg.zona if rg else '?'}) "
                  f"campo={h.campo} periodo={rg.periodo if rg else '?'}")
    else:
        print("  ninguno")

    print("== Envíos en cola (pendiente, aún no salen) a Jessuad ==")
    jess = (await db.execute(select(Contacto).where(Contacto.nombre_norm == _norm_nombre(SALE)))).scalar_one_or_none()
    env = []
    if jess and jess.telefono:
        env = (await db.execute(select(WaEnvio).where(
            WaEnvio.estado == "pendiente", WaEnvio.destino == jess.telefono))).scalars().all()
    for e in env:
        print(f"  envío {e.id}: reg {e.reg_id} campo={e.campo} creado={e.creado_en}")
    if not env:
        print("  ninguno")


async def aplicar(db):
    dev = (await db.execute(select(Usuario).where(Usuario.nombre == "Sergio (Dev)"))).scalars().first()
    uid = dev.id if dev else None
    cambios = 0

    # 1. Jessuad deja de ser apoyo pero SIGUE ACTIVO con rol «otro» y sin zona: el pareo de
    #    respuestas (wa_respuestas.py) solo indexa contactos activos y a él le quedan hilos
    #    abiertos; ningún flujo lo elige como apoyo porque todos filtran rol «apoyo».
    jess = (await db.execute(select(Contacto).where(Contacto.nombre_norm == _norm_nombre(SALE)))).scalar_one_or_none()
    if jess is None:
        print("OJO: no existe el contacto", SALE)
    elif jess.rol == "apoyo" or not jess.activo or jess.zona or NOTA_SALE not in (jess.notas or ""):
        jess.activo, jess.rol, jess.zona = True, "otro", None
        notas = (jess.notas or "").replace(NOTA_SALE_VIEJA, "").rstrip()
        if NOTA_SALE not in notas:
            notas = (notas + chr(10) + NOTA_SALE).strip()
        jess.notas = notas
        cambios += 1
        print("ya no es apoyo (activo, rol otro, sin zona):" + chr(10) + _fila(jess))
    else:
        print("ya estaba (activo, rol otro):" + chr(10) + _fila(jess))

    # 2. Carlos entra (upsert por nombre_norm, igual que POST /contactos)
    c = (await db.execute(select(Contacto).where(Contacto.nombre_norm == _norm_nombre(ENTRA)))).scalar_one_or_none()
    if c is None:
        db.add(Contacto(nombre=ENTRA, nombre_norm=_norm_nombre(ENTRA), telefono=TEL, rol="apoyo", zona=ZONA,
                        activo=True, creado_por=uid, notas=NOTA_ENTRA))
        cambios += 1
        print(f"creado: {ENTRA} tel={TEL} rol=apoyo zona={ZONA}")
    elif not (c.activo and c.telefono == TEL and c.rol == "apoyo" and (c.zona or "").upper() == ZONA):
        c.activo, c.telefono, c.rol, c.zona = True, TEL, "apoyo", ZONA
        if NOTA_ENTRA not in (c.notas or ""):
            c.notas = ((c.notas or "").rstrip() + "\n" + NOTA_ENTRA).strip()
        cambios += 1
        print("actualizado:\n" + _fila(c))
    else:
        print("ya estaba:\n" + _fila(c))

    # 3. Quien tuviera ese número (Geraldine): nota; se queda como está por el histórico del 07-09
    otros = (await db.execute(select(Contacto).where(
        Contacto.telefono == TEL, Contacto.nombre_norm != _norm_nombre(ENTRA)))).scalars().all()
    for g in otros:
        if g.activo and g.rol == "apoyo":
            print(f"OJO: {g.nombre} (id {g.id}) sigue ACTIVO como apoyo con el mismo número: revisar a mano")
        if NOTA_GERALDINE not in (g.notas or ""):
            g.notas = ((g.notas or "").rstrip() + "\n" + NOTA_GERALDINE).strip()
            cambios += 1
            print("nota añadida:\n" + _fila(g))

    await db.commit()
    print(f"COMMIT ({cambios} cambios)")


async def main():
    async with AsyncSessionLocal() as db:
        await mostrar(db)
        if "--aplicar" in sys.argv:
            print("\n== APLICAR ==")
            await aplicar(db)
            print("\n== Después ==")
            await mostrar(db)
        else:
            print("\n(solo lectura; con --aplicar se aplica el relevo)")


if __name__ == "__main__":
    asyncio.run(main())
