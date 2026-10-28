"""Zona de los trabajos de LÍNEA MUERTA (FT) de David Linares — 2026-10-02.

Regla de origen (Paola, «AJUSTES PENDIENTES SERGIO.docx», 22-07-2026): «AJUSTAR A DAVID LINARES COMO
SUP DE UBATE PARA TRABAJOS DE LINEA MUERTA». Sus ST siguen en LV. Así están sus hojas «data» de mayo
(5 órdenes) y julio (R06853), y así lo decía la semilla del código.

Qué se dañó: el 08-09 David Linares quedó en el directorio como apoyo de LV (para recibir las
consultas de Línea Viva) y `_zona_map` dejaba que el directorio pisara la semilla: desde entonces
sus FT entraron con zona LV. Hallado el 02-10 al comparar la tabla con las hojas de Paola. El
código ya no saca LV del directorio para un FT; este script corrige las órdenes ya cargadas.

Qué toca (idempotente): FT del periodo dado con responsable David Linares y zona «LV» cuya zona NO
fue escrita a mano → «UBATE», con su entrada en el historial. Nada más.

Uso (en el VPS, desde /var/www/sabogal-platform/backend):
  PYTHONPATH=. venv/bin/python scripts/zona_linares_ft_20261002.py 2026-09            # solo muestra
  PYTHONPATH=. venv/bin/python scripts/zona_linares_ft_20261002.py 2026-09 --aplicar  # aplica
"""
import sys
import asyncio

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models.models import AnsRegistro, AnsCambio
from app.api.ans import _norm_nombre, _zona_de, _zona_map
from app.services import zona_deduccion

NOMBRES = {"DAVID LINARES", "JESUS DAVID LINARES"}
AUTOR = "Sistema (regla de Paola 22-07: David Linares FT → UBATE)"


async def main(periodo: str, aplicar: bool):
    async with AsyncSessionLocal() as db:
        zmap = await _zona_map(db)
        regs = (await db.execute(select(AnsRegistro).where(
            AnsRegistro.entorno == "real", AnsRegistro.periodo == periodo, AnsRegistro.tipo == "FT",
            AnsRegistro.zona == "LV").order_by(AnsRegistro.id))).scalars().all()
        n = 0
        for r in regs:
            if _norm_nombre(r.nombre_responsable or "") not in {_norm_nombre(x) for x in NOMBRES}:
                print(f"{r.pdl}: responsable «{r.nombre_responsable}» — no es David Linares, no se toca")
                continue
            if (zona_deduccion.extra_de(r).get("_zona_origen") or {}).get("metodo") == "manual":
                print(f"{r.pdl}: la zona LV la escribió alguien a mano — no se toca")
                continue
            nueva = _zona_de("FT", r.nombre_responsable, zmap)
            print(f"{r.pdl} (id {r.id}) · {r.estado_actual} · fin {r.fecha_fin}: zona LV → {nueva}")
            if nueva != "UBATE":
                print("   el código no da UBATE: revisar antes de aplicar"); continue
            n += 1
            if aplicar:
                db.add(AnsCambio(registro_id=r.id, periodo=r.periodo, protocolo=r.protocolo, campo="zona",
                                 valor_anterior="LV", valor_nuevo=nueva, tipo="edicion",
                                 usuario_id=None, usuario_nombre=AUTOR))
                r.zona = nueva
        if aplicar:
            await db.commit()
            print(f"APLICADO: {n} orden(es)")
        else:
            print(f"(solo muestra: {n} orden(es) cambiarían; con --aplicar se escribe)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    asyncio.run(main(args[0] if args else "2026-09", "--aplicar" in sys.argv))
