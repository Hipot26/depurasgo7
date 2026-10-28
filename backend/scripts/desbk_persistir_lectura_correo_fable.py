"""Persiste en un correo ANS la lectura de un caso hecha por Fable en el chat (ver memoria
sabogal-fable-analiza): mismo post-proceso que `analizar_y_guardar` (cobertura de tiempos por
código + regla de Revisión) y el mismo aviso ntfy que la lectura automática. La lectura previa
de la máquina (si la hubo) queda guardada en `lectura_previa` para poder compararlas.

Uso (en el VPS):
  PYTHONPATH=. venv/bin/python scripts/persistir_lectura_correo_fable.py <correo_id> <reg_id> <lectura.json>
El JSON trae las llaves del esquema 3.2A/3.2B (relacionado, excluible, categoria, causa,
causa_tipo, tramos_justificados, razon, razonamiento, citas).
"""
import asyncio
import json
import sys
from datetime import datetime

from app.api.correo_auditoria import _analisis_mapa
from app.api.correo_bot import _avisar_si_salva_caso
from app.db.database import AsyncSessionLocal
from app.models.models import AnsRegistro, CorreoAns


async def main(correo_id: int, reg_id: int, ruta_json: str):
    lectura = json.load(open(ruta_json, encoding="utf-8"))
    async with AsyncSessionLocal() as db:
        c = await db.get(CorreoAns, correo_id)
        reg = await db.get(AnsRegistro, reg_id)
        assert c and reg, (correo_id, reg_id)
        campo = "excluible" if lectura.get("campo", "excluible") == "excluible" else "excluible_anulado"
        d = {k: v for k, v in lectura.items() if k != "campo"}
        d["analista"] = "fable-chat"
        # Misma regla GENERAL de tiempos que aplica el backend (auditoría 2026-08-24): la
        # cobertura la calcula el código; si el correo no cubre el desfase → Revisión.
        if campo == "excluible":
            from app.services.wa_analista import _calcular_cobertura
            cov = _calcular_cobertura(reg, d)
            d["cobertura_calc"] = cov
            if d.get("relacionado") == "SI" and cov["aplicable"] and not cov["cubierto"]:
                faltas = " · ".join(f"faltan {g['faltan']} min de {gap}"
                                    for gap, g in cov["gaps"].items() if not g["cubierto"])
                d["veredicto_ia"] = d.get("excluible")
                d["excluible"] = None
                d["revision"] = {"motivo": (f"el correo no cubre los tiempos dentro de la "
                                            f"tolerancia de {cov['tolerancia_min']} min por "
                                            f"gap ({faltas})")}
        prev = _analisis_mapa(c)
        anterior = prev.get(str(reg.id))
        if anterior and not anterior.get("pendiente"):
            d["lectura_previa"] = {k: anterior.get(k) for k in
                                   ("cuando", "relacionado", "excluible", "veredicto_ia",
                                    "categoria", "razon", "analista")}
        prev[str(reg.id)] = {"reg_id": reg.id, "campo": campo, "pdl": reg.pdl,
                             "cuando": datetime.now().isoformat(), **d}
        c.analisis = json.dumps(prev, ensure_ascii=False)
        await db.commit()
        slot = prev[str(reg.id)]
        print("guardado:", reg.pdl, "| relacionado", slot.get("relacionado"),
              "| excluible", slot.get("excluible"), "| veredicto_ia", slot.get("veredicto_ia"),
              "| revision", bool(slot.get("revision")))
        await _avisar_si_salva_caso(reg, campo, slot)
        print("aviso ntfy evaluado (se envía solo si propone excluir y el caso no está en SI)")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]))
