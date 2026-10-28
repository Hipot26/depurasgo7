"""Corrección retroactiva AX/AY del CD10 de Programados (hallazgo de Andrea, 2026-09-23).

Para cada registro FT del periodo, busca su fila en los CD10 guardados en data/ans_fuentes
(el archivo más reciente que la traiga) y:
  · si el supervisor (AY) venía como teléfono y AX trae un nombre → corrige nombre_responsable
    y recalcula la zona (mismo mapa de la importación);
  · guarda en datos_extra.telefono_responsable_cd10 el celular del supervisor (AX).
No toca decisiones ni observaciones. Idempotente.

Uso (en el VPS, desde backend/):  venv/bin/python scripts/corregir_ax_ay.py 2026-09 [--aplicar]
"""
import asyncio
import glob
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import openpyxl  # noqa: E402
from openpyxl.utils import column_index_from_string as ci  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db.database import AsyncSessionLocal  # noqa: E402
from app.models.models import AnsRegistro  # noqa: E402
from app.api.ans import _es_telefono, _tel_e164, _zona_de, _zona_map, _txt  # noqa: E402

DATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "ans_fuentes", "real"))


def _g(fila, letra):
    i = ci(letra) - 1
    return fila[i] if i < len(fila) else None


def leer_cd10(periodo: str) -> dict:
    """pdl → (AX, AY) de la fila más reciente entre los CD10 de Programados del periodo."""
    out = {}
    for f in sorted(glob.glob(os.path.join(DATA, periodo, "programados", "*.xlsx"))):
        wb = openpyxl.load_workbook(f, data_only=True)
        ws = wb["Hoja1"] if "Hoja1" in wb.sheetnames else wb[wb.sheetnames[0]]
        for fila in ws.iter_rows(min_row=2, values_only=True):
            d = _txt(_g(fila, "D"))
            if d:
                out[d.split("/")[0].strip()] = (_txt(_g(fila, "AX")), _txt(_g(fila, "AY")), os.path.basename(f))
    return out


async def main():
    periodo = next((a for a in sys.argv[1:] if not a.startswith("--")), "2026-09")
    aplicar = "--aplicar" in sys.argv
    filas = leer_cd10(periodo)
    print(f"{len(filas)} PDL leídos de los CD10 de {periodo}")
    async with AsyncSessionLocal() as db:
        zmap = await _zona_map(db)
        regs = (await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo, AnsRegistro.entorno == "real",
                                                            AnsRegistro.tipo == "FT"))).scalars().all()
        cambios = tel_n = 0
        for reg in regs:
            fila = filas.get(reg.pdl)
            if not fila:
                continue
            ax, ay, archivo = fila
            try:
                extra = json.loads(reg.datos_extra or "{}") or {}
            except ValueError:
                extra = {}
            nombre_nuevo = None
            if _es_telefono(reg.nombre_responsable) and ax and any(ch.isalpha() for ch in ax):
                nombre_nuevo = " ".join(ax.split())
                telefono = reg.nombre_responsable
            else:
                telefono = ax if _es_telefono(ax) else None
            if telefono and extra.get("telefono_responsable_cd10") != _tel_e164(telefono):
                extra["telefono_responsable_cd10"] = _tel_e164(telefono)
                tel_n += 1
                if aplicar:
                    reg.datos_extra = json.dumps(extra, ensure_ascii=False)
            if nombre_nuevo:
                zona = _zona_de("FT", nombre_nuevo, zmap)
                print(f"  {reg.pdl}: supervisor «{reg.nombre_responsable}» → «{nombre_nuevo}» · zona {reg.zona!r} → {zona!r} "
                      f"· celular {extra.get('telefono_responsable_cd10')} ({archivo})")
                cambios += 1
                if aplicar:
                    extra["_correccion"] = "AX/AY invertidas por el digitador: el nombre venía en AX y el celular en AY"
                    reg.nombre_responsable = nombre_nuevo
                    if zona and not (reg.zona or "").strip():
                        reg.zona = zona
                    reg.datos_extra = json.dumps(extra, ensure_ascii=False)
        if aplicar:
            await db.commit()
        print(f"{'APLICADO' if aplicar else 'SIMULACIÓN'}: {cambios} supervisor(es) corregidos · {tel_n} celular(es) de CD10 guardados")


asyncio.run(main())
