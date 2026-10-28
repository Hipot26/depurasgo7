"""Sigla del elaborador en Línea Viva — 2026-10-02 (Sergio por chat, con el WhatsApp de Yuly Arévalo
y de Cristian Santana del mismo día).

Qué pasó: el CD10 de ST no trae la sigla ni el creador del PdL. La plataforma la presumía con la
regla «ZONA VII → Andrea; si no → Yuli», y desde que Juan Carlos Prieto elabora (agosto) todo lo
suyo le caía a Yuli. CDOPT2026R07351 quedó a nombre de ella; consultó el STM y el creador es
PRIETO BEJARANO JUAN CARLOS (WhatsApp 02-10-2026 14:28, con pantalla del STM). En agosto había
pasado lo mismo con CDOPT2026R06745, que ella misma corrigió en la tabla el 06-09.

Qué hace este script (idempotente):
  1. R07351 (periodo 2026-09): sigla → Prieto, con su entrada en el historial (ans_cambios).
  2. Marca `datos_extra._sigla_origen = manual` en R07351 y en R06745: son las dos ST cuya sigla
     confirmó una persona. Las demás ST siguen siendo PRESUNTAS (marca ≈ en el grid).
No toca ninguna otra fila ni ninguna otra celda.

Uso (en el VPS, desde /var/www/sabogal-platform/backend):
  PYTHONPATH=. venv/bin/python scripts/sigla_lv_20261002.py            # solo muestra
  PYTHONPATH=. venv/bin/python scripts/sigla_lv_20261002.py --aplicar  # aplica
"""
import sys
import json
import asyncio

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models.models import AnsRegistro, AnsCambio
from app.api.ans import LV_PROG_PRIETO, _sigla_lv_regla, _sigla_presunta
from app.services import zona_deduccion

CASOS = [
    # (pdl, periodo, sigla correcta, quién la confirmó, fecha)
    ("CDOPT2026R07351", "2026-09", LV_PROG_PRIETO,
     "Yuly Arévalo (WhatsApp 02-10-2026 14:28, pantalla del STM: creador Juan Carlos Prieto)", "2026-10-02"),
    ("CDOPT2026R06745", "2026-08", LV_PROG_PRIETO,
     "Yuli Arévalo (Programadora), en la tabla", "2026-09-06"),
]
AUTOR = "Sergio (Dev) · confirmó Yuly Arévalo por WhatsApp 02-10"


async def main(aplicar: bool):
    async with AsyncSessionLocal() as db:
        for pdl, periodo, sigla, quien, fecha in CASOS:
            reg = (await db.execute(select(AnsRegistro).where(
                AnsRegistro.pdl == pdl, AnsRegistro.periodo == periodo, AnsRegistro.tipo == "ST",
                AnsRegistro.entorno == "real"))).scalar_one_or_none()
            if not reg:
                print(f"{pdl} {periodo}: NO ENCONTRADA — no se toca nada")
                continue
            extra = zona_deduccion.extra_de(reg)
            antes = (reg.sigla_elaborador or "").strip()
            print(f"{pdl} {periodo} (id {reg.id}): sigla «{antes}» · presunta={_sigla_presunta(reg)} · "
                  f"regla nueva → «{_sigla_lv_regla(reg.descripcion)[0]}»")
            if antes != sigla:
                print(f"   → sigla pasa a «{sigla}»")
                if aplicar:
                    db.add(AnsCambio(registro_id=reg.id, periodo=reg.periodo, protocolo=reg.protocolo,
                                     campo="sigla_elaborador", valor_anterior=antes, valor_nuevo=sigla,
                                     tipo="edicion", usuario_id=None, usuario_nombre=AUTOR))
                    reg.sigla_elaborador = sigla
            if (extra.get("_sigla_origen") or {}).get("metodo") != "manual":
                print(f"   → origen: confirmada por {quien}")
                if aplicar:
                    extra["_sigla_origen"] = {"metodo": "manual", "usuario": quien, "fecha": fecha}
                    reg.datos_extra = json.dumps(extra, ensure_ascii=False, default=str)
        if aplicar:
            await db.commit()
            print("APLICADO")
        else:
            print("(solo muestra; con --aplicar se escribe)")


if __name__ == "__main__":
    asyncio.run(main("--aplicar" in sys.argv))
