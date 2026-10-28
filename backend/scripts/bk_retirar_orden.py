"""Retira UNA orden del ANS por decisión de una persona y deja constancia.

Caso de uso (2026-10-02): una orden que resultó ser la copia de un PdL anterior, validada por el
aprobador sin que se hubiera programado nada para ese día; la dirección decidió retirarla del ANS.

Qué hace con --aplicar (una sola transacción):
  1. Guarda el respaldo completo (fila + consultas con sus mensajes + análisis + evidencias + nodos +
     media amarrada) en `ans_retirados.respaldo` y en un archivo de `backups_deploy/retirados/`.
  2. Anota la orden en `ans_retirados` (quién ordenó, motivo, fuente): el import la salta desde ya,
     aunque el STM la siga trayendo.
  3. Borra la fila de `ans_registros` y lo que cuelga de ella.
NO borra: el historial (`ans_cambios`, `auditoria`), el registro del WhatsApp ya enviado (`wa_envios`,
ese mensaje salió de verdad) ni los archivos de evidencia en disco (quedan sus rutas en el respaldo).

Uso (en el servidor, desde backend/):
  PYTHONPATH=. venv/bin/python scripts/retirar_orden.py <orden> \
      --ordena "<quién lo ordena>" --fuente "<canal · persona · fecha hora>" \
      --motivo "…"            # solo muestra
  … --aplicar                 # borra y deja constancia
"""
import os
import json
import asyncio
import argparse
from datetime import datetime

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.models.models import (AnsRegistro, AnsRetirado, AnsConsulta, AnsConsultaMsg, AnsEvidencia,
                               AnsNodosCaso, AnsCambio, WaAnalisis, WaEnvio, WaMediaCaso)

RESPALDOS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backups_deploy", "retirados"))


def _fila(o) -> dict:
    return {c.name: getattr(o, c.name) for c in o.__table__.columns}


async def main(a):
    async with AsyncSessionLocal() as db:
        regs = (await db.execute(select(AnsRegistro).where(
            AnsRegistro.pdl == a.pdl, AnsRegistro.entorno == "real"))).scalars().all()
        ya = (await db.execute(select(AnsRetirado).where(
            AnsRetirado.pdl == a.pdl, AnsRetirado.entorno == "real"))).scalar_one_or_none()
        if ya:
            print(f"{a.pdl} ya está retirada desde {ya.creado_en} (ordenó {ya.ordenado_por}).")
        if not regs:
            print(f"{a.pdl}: no hay fila en la tabla del ANS. Nada que borrar.")
            return
        if len(regs) > 1 and not a.periodo:
            print(f"{a.pdl} está en {len(regs)} periodos ({', '.join(r.periodo for r in regs)}): indica --periodo.")
            return
        reg = next(r for r in regs if not a.periodo or r.periodo == a.periodo)

        consultas = (await db.execute(select(AnsConsulta).where(AnsConsulta.reg_id == reg.id))).scalars().all()
        msgs = []
        for c in consultas:
            msgs += (await db.execute(select(AnsConsultaMsg).where(AnsConsultaMsg.consulta_id == c.id))).scalars().all()
        evid = (await db.execute(select(AnsEvidencia).where(AnsEvidencia.reg_id == reg.id))).scalars().all()
        anal = (await db.execute(select(WaAnalisis).where(WaAnalisis.reg_id == reg.id))).scalars().all()
        media = (await db.execute(select(WaMediaCaso).where(WaMediaCaso.reg_id == reg.id))).scalars().all()
        nodos = (await db.execute(select(AnsNodosCaso).where(AnsNodosCaso.registro_id == reg.id))).scalars().all()
        envios = (await db.execute(select(WaEnvio).where(WaEnvio.reg_id == reg.id))).scalars().all()
        cambios = (await db.execute(select(AnsCambio).where(AnsCambio.registro_id == reg.id))).scalars().all()

        print(f"== {reg.pdl} · id {reg.id} · {reg.periodo} · {reg.tipo} · {reg.zona} · {reg.estado_actual}")
        print(f"   responsable {reg.nombre_responsable} · sigla {reg.sigla_elaborador} · inicio {reg.fecha_inicio}")
        print(f"   se ejecutó {reg.se_ejecuto} · excluible anulado {reg.excluible_anulado} · obs: {reg.obs_anulado}")
        print("   SE BORRA:")
        print("     1 fila del ANS")
        print(f"     {len(consultas)} consulta(s) {[(c.id, c.campo, c.estado) for c in consultas]} con {len(msgs)} mensaje(s)")
        print(f"     {len(anal)} análisis de Auditoría IA {[(x.id, x.estado) for x in anal]}")
        print(f"     {len(evid)} evidencia(s) {[e.ruta for e in evid]} (los archivos quedan en disco)")
        print(f"     {len(media)} media amarrada · {len(nodos)} fila(s) de nodos")
        print("   SE CONSERVA:")
        print(f"     {len(cambios)} entrada(s) del historial · {len(envios)} WhatsApp ya enviado(s) "
              f"{[(e.id, e.persona, e.estado) for e in envios]}")

        respaldo = {"registro": _fila(reg), "consultas": [_fila(c) for c in consultas],
                    "consulta_msgs": [_fila(m) for m in msgs], "evidencias": [_fila(e) for e in evid],
                    "wa_analisis": [_fila(x) for x in anal], "wa_media_caso": [_fila(m) for m in media],
                    "nodos": [_fila(n) for n in nodos]}
        txt = json.dumps(respaldo, ensure_ascii=False, default=str)
        if not a.aplicar:
            print(f"(solo muestra; respaldo de {len(txt)} caracteres listo. Con --aplicar se borra.)")
            return
        if not (a.ordena and a.fuente and a.motivo):
            print("Faltan --ordena, --fuente y --motivo: sin eso no se borra nada."); return

        os.makedirs(RESPALDOS, exist_ok=True)
        ruta = os.path.join(RESPALDOS, f"{reg.pdl}_{datetime.now():%Y%m%d_%H%M%S}.json")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(txt)
        if not ya:
            db.add(AnsRetirado(entorno="real", pdl=reg.pdl, protocolo=reg.protocolo, periodo=reg.periodo,
                               tipo=reg.tipo, motivo=a.motivo, ordenado_por=a.ordena, fuente=a.fuente,
                               usuario_nombre=a.usuario, respaldo=txt))
        db.add(AnsCambio(entorno="real", registro_id=reg.id, periodo=reg.periodo, protocolo=reg.protocolo,
                         campo="(orden retirada del ANS)", valor_anterior=reg.estado_actual,
                         valor_nuevo=f"Retirada por orden de {a.ordena}. {a.fuente}"[:2000], tipo="edicion",
                         usuario_id=None, usuario_nombre=a.usuario))
        for o in [*msgs, *consultas, *anal, *media, *nodos, *evid, reg]:
            await db.delete(o)
        await db.commit()
        print(f"BORRADA y anotada en ans_retirados. Respaldo: {ruta}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("pdl")
    p.add_argument("--periodo")
    p.add_argument("--ordena")
    p.add_argument("--fuente")
    p.add_argument("--motivo")
    p.add_argument("--usuario", default="desarrollador")
    p.add_argument("--aplicar", action="store_true")
    asyncio.run(main(p.parse_args()))
