"""Persiste un análisis hecho por FABLE (el Claude del chat, $0 de API) como propuesta
del tab Auditoría IA — pasando por el MISMO post-proceso determinista del analista
(cobertura calculada por código + regla de Revisión). Pareja de material_caso.py.

Uso:
  ./venv/bin/python scripts/persistir_analisis_fable.py CDOPP2026R07691 /tmp/ia.json

/tmp/ia.json = el objeto `ia` con el MISMO esquema de _SCHEMA_CASO_WA (claude.py):
videos[], linea_tiempo[], categoria, causa, causa_tipo, tramos_justificados[],
excluible_sugerido, cobertura_desfase, observacion, confianza, resumen. Los campos
grupos/eventos/videos-meta del resultado se completan livianos (el detalle del tab
enriquece grupo por msg_id en vivo). Nada se aplica solo: queda «propuesto»."""
import asyncio
import json
import sys

sys.path.insert(0, "/var/www/sabogal-platform/backend")
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models.models import AnsRegistro
from app.services import wa_analista as wa


async def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    pdl, ruta = sys.argv[1], sys.argv[2]
    with open(ruta, encoding="utf-8") as f:
        ia = json.load(f)
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(AnsRegistro).where(
            AnsRegistro.pdl.ilike(f"%{pdl}%"), AnsRegistro.entorno == "real"))
        reg = r.scalars().first()
        if not reg:
            print(f"{pdl}: registro no encontrado")
            return
        ia.setdefault("analista", "fable-chat")   # constancia de que no gastó API
        resultado = {
            "tipo": "3.2A",
            "alcance": "análisis de Fable en el chat (sin API)",
            "caso": {"pdl": reg.pdl, "zona": reg.zona, "responsable": reg.nombre_responsable,
                     "fecha": reg.fecha_fin, "cierre_planeado": reg.hora_fin,
                     "maniobra_inicio": reg.maniobra_inicio, "maniobra_fin": reg.maniobra_fin,
                     "contexto": wa._contexto_orden(reg)},
            "grupos": ia.pop("_grupos", []),
            "eventos": ia.pop("_eventos", []),
            "videos": ia.pop("_videos_meta", []),
            "ia": ia,
        }
        a = await wa._persistir(db, reg, resultado)   # cobertura + Revisión, como siempre
        cov = (resultado["ia"].get("cobertura_calc") or {})
        print(f"{pdl}: análisis #{a.id} propuesto · "
              f"sugerido={resultado['ia'].get('excluible_sugerido')} "
              f"revision={bool(resultado['ia'].get('revision'))} "
              f"cubierto={cov.get('cubierto')}")

asyncio.run(main())
