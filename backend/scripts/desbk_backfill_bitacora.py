"""Rellena la bitácora (observación) de los hilos de consulta que se abrieron ANTES de que
existiera la función — o de cualquiera al que le falte la nota. Reconstruye el estado desde los
turnos guardados y escribe la nota que le habría correspondido.

Solo toca hilos ABIERTOS cuya celda de observación esté VACÍA: nunca pisa nada escrito a mano.

    PYTHONPATH=. ./venv/bin/python scripts/backfill_bitacora.py [--aplicar]
"""
import sys
import json
import asyncio

sys.path.insert(0, ".")

from sqlalchemy import select                                    # noqa: E402
from app.db.database import AsyncSessionLocal                    # noqa: E402
from app.db.ans_ctx import set_entorno                           # noqa: E402
from app.models.models import AnsConsulta, AnsConsultaMsg, AnsRegistro   # noqa: E402
from app.api.ans import _CONSULTA_CFG, _nota_espera, _nota_remite, _escribir_nota  # noqa: E402

APLICAR = "--aplicar" in sys.argv


async def main():
    set_entorno("real")
    async with AsyncSessionLocal() as db:
        hilos = (await db.execute(
            select(AnsConsulta).where(AnsConsulta.estado == "abierta")
        )).scalars().all()

        tocados = 0
        for c in hilos:
            reg = await db.get(AnsRegistro, c.reg_id)
            if not reg:
                continue
            razon_field = _CONSULTA_CFG.get(c.campo, (None, "obs"))[1]
            if (getattr(reg, razon_field, None) or "").strip():
                continue                                  # ya tiene algo: no se toca
            if (c.nota_auto or "").strip():
                continue

            msgs = (await db.execute(
                select(AnsConsultaMsg).where(AnsConsultaMsg.consulta_id == c.id)
                .order_by(AnsConsultaMsg.id)
            )).scalars().all()

            # ¿La última lectura de la IA fue una redirección? Entonces la nota lo dice.
            nota = None
            for m in reversed(msgs):
                if m.direccion == "ia" and m.meta:
                    try:
                        dec = json.loads(m.meta)
                    except Exception:
                        break
                    if dec.get("bitacora"):
                        nota = dec["bitacora"]
                    elif dec.get("accion") == "redirigir" and dec.get("siguiente_persona"):
                        de = next((x.persona for x in reversed(msgs) if x.direccion == "recibido"), None)
                        nota = _nota_remite(de, dec["siguiente_persona"])
                    break
            nota = nota or _nota_espera(c.persona_actual)

            print(f"  hilo {c.id} · {reg.protocolo} · {razon_field} ← {nota}")
            if APLICAR:
                _escribir_nota(reg, c, razon_field, nota)
            tocados += 1

        if APLICAR and tocados:
            await db.commit()
            print(f"\n✔ {tocados} hilo(s) actualizados.")
        else:
            print(f"\n({tocados} hilo(s) a actualizar. Añade --aplicar para escribir.)")


if __name__ == "__main__":
    asyncio.run(main())
