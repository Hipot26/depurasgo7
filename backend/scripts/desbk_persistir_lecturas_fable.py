"""Persiste lecturas hechas por Fable en el chat (ver memoria sabogal-fable-analiza) en los hilos
de consulta cuya lectura automática falló («IA no disponible»). Misma lógica que
/consulta/analizar: turno IA + estado del hilo + bitácora (nunca pisa lo escrito a mano).
Uso: PYTHONPATH=. venv/bin/python scripts/persistir_lecturas_fable.py <lecturas.json>"""
import asyncio, json, sys
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from app.config import settings
from app.models.models import AnsRegistro, AnsConsulta, AnsConsultaMsg
from app.api.ans import _CONSULTA_CFG, _escribir_nota, _nota_espera

items = json.load(open(sys.argv[1], encoding="utf-8"))

async def main():
    eng = create_async_engine(settings.DATABASE_URL)
    async with AsyncSession(eng) as db:
        for it in items:
            c = await db.get(AnsConsulta, it["consulta_id"])
            reg = await db.get(AnsRegistro, c.reg_id)
            dec = dict(it["dec"])
            dec.update({"analista": "fable-chat", "reemplaza_turnos": it["reemplaza"], "fecha_lectura": "2026-09-08"})
            _, razon_field, _, _ = _CONSULTA_CFG.get(c.campo, (c.campo, "obs", "", ""))
            db.add(AnsConsultaMsg(consulta_id=c.id, direccion="ia", persona=None,
                                  texto=dec.get("resumen"), meta=json.dumps(dec, ensure_ascii=False)))
            nota = (dec.get("bitacora") or "").strip() or None
            accion = dec["accion"]
            if accion == "resolver" and dec.get("excluible"):
                c.excluible_sug = dec["excluible"]; c.categoria = dec.get("categoria")
                c.razon_sug = dec.get("razon"); c.resumen = dec.get("resumen")
            elif accion == "redirigir" and dec.get("siguiente_persona") and it["escribir_nota"]:
                c.persona_actual = dec["siguiente_persona"]; c.resumen = dec.get("resumen")
            else:
                if it["escribir_nota"]:
                    c.resumen = dec.get("resumen")
                nota = nota or c.nota_auto or _nota_espera(c.persona_actual)
            escrita = _escribir_nota(reg, c, razon_field, nota) if it["escribir_nota"] else False
            print(f"hilo {c.id} {reg.pdl} [{c.campo}] {accion} | sug={c.excluible_sug} | nota escrita={escrita}")
            print("   obs ahora:", (getattr(reg, razon_field) or "-")[:160])
        await db.commit()
        print("COMMIT")
    await eng.dispose()

asyncio.run(main())
