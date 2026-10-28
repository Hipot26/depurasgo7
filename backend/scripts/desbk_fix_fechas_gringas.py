"""Corrige las fechas que quedaron en formato GRINGO (mes/día/año) en las columnas de fecha del
ANS, pasándolas a colombiano DD/MM/YYYY. Vinieron de un import cuyo Excel tenía las celdas como
texto M/D/YYYY. Además de verse mal, rompen el cálculo (el sistema no puede leer «mes 17»).

Detección de gringa (conservadora): la cadena es d/d/AAAA y (el 2.º número > 12 → imposible como
mes) O (algún componente va sin cero a la izquierda → nuestro generador SIEMPRE rellena con cero,
así que un dígito suelto = vino de fuera en formato gringo). Las colombianas con cero a la
izquierda («06/12/2026») NO se tocan.

    PYTHONPATH=. ./venv/bin/python scripts/fix_fechas_gringas.py            # simulacro
    PYTHONPATH=. ./venv/bin/python scripts/fix_fechas_gringas.py --aplicar  # escribe
"""
import re
import sys
import asyncio

sys.path.insert(0, ".")

from sqlalchemy import select                                    # noqa: E402
from app.db.database import AsyncSessionLocal                    # noqa: E402
from app.db.ans_ctx import set_entorno                           # noqa: E402
from app.models.models import AnsRegistro                        # noqa: E402

APLICAR = "--aplicar" in sys.argv
CAMPOS = ("fecha_inicio", "fecha_fin", "fecha_debio_validar", "fecha_envio_validar")
_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def es_gringa(s: str) -> bool:
    m = _RE.match((s or "").strip())
    if not m:
        return False
    a, b, _ = m.groups()
    if int(b) > 12:                       # 2.º número > 12 → es un día → formato mes/día (gringo)
        return True
    if len(a) == 1 or len(b) == 1:        # sin cero a la izquierda → no lo generamos nosotros
        return True
    return False


def a_colombiana(s: str) -> str:
    """De M/D/YYYY (gringa) a DD/MM/YYYY (colombiana)."""
    a, b, y = _RE.match(s.strip()).groups()
    mes, dia = int(a), int(b)             # gringo: primero mes, luego día
    return f"{dia:02d}/{mes:02d}/{y}"


async def main():
    set_entorno("real")
    async with AsyncSessionLocal() as db:
        regs = (await db.execute(select(AnsRegistro))).scalars().all()
        cambios = 0
        for r in regs:
            for campo in CAMPOS:
                v = getattr(r, campo, None)
                if es_gringa(v):
                    nuevo = a_colombiana(v)
                    print(f"  reg {r.id:5} · {r.protocolo:33} · {campo:20} {v:11} → {nuevo}")
                    if APLICAR:
                        setattr(r, campo, nuevo)
                    cambios += 1
        if APLICAR and cambios:
            await db.commit()
            print(f"\n✔ {cambios} fecha(s) corregidas.")
        else:
            print(f"\n({cambios} fecha(s) a corregir. Añade --aplicar para escribir.)")


if __name__ == "__main__":
    asyncio.run(main())
