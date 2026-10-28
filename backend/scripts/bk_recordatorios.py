"""RECORDATORIOS y ESCALAMIENTO de las preguntas por WhatsApp (orquestación ANS, paso 4,
2026-09-07): «el sistema da las órdenes a los auditores y se va re-verificando».

Por cada pregunta ENVIADA (WaEnvio estado=enviado, campo excluible / excluible_anulado) cuyo
hilo sigue abierto y sin respuesta después del envío:
  · ≥ 24 h sin respuesta → 1er recordatorio a la misma persona;
  · ≥ 24 h después del 1º → 2º recordatorio;
  · ≥ 24 h después del 2º → ESCALAMIENTO: una sola vez, al supervisor de la zona (directorio,
    rol supervisor) — o, en 3.2 A, al supervisor de la orden (col G) si no era él el preguntado.
Los mensajes se encolan en la cola del bot (sale en su próximo ciclo). Todo queda en el hilo
del caso como turno «enviado» con meta {recordatorio: n} / {escalamiento: true}.
Horario: lo pone el cron (08:30 y 14:30 Bogotá) — nunca de noche ni domingo.
Apagar: WA_RECORDATORIOS=false en .env.

Uso (en el VPS, desde backend/):  venv/bin/python scripts/recordatorios.py [--prueba]
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.database import AsyncSessionLocal  # noqa: E402
from app.models.models import (AnsRegistro, AnsConsulta, AnsConsultaMsg, WaEnvio, Contacto)  # noqa: E402

MAX_RECORDATORIOS = 2
ESPERA_H = 24


def _corto(nombre: str) -> str:
    from app.api.ans import _nombre_corto
    try:
        return _nombre_corto(nombre, apellidos_primero=True)
    except Exception:  # noqa: BLE001
        return (nombre or "").split()[0].title() if nombre else ""


def _que_falta(campo: str) -> str:
    if campo == "excluible_anulado":
        return "el correo del Centro Operativo con el motivo de la anulación"
    if campo == "numero_supervisor":
        return "el número del supervisor responsable de la cuadrilla"
    return "el video del frente de trabajo o la explicación del desfase de tiempos"


def _texto_recordatorio(reg: AnsRegistro, campo: str, persona: str, n: int) -> str:
    return (f"Recordatorio {n}/{MAX_RECORDATORIOS}: buen día {_corto(persona)}. Sigo pendiente de la orden "
            f"{reg.pdl} del {reg.fecha_inicio or reg.fecha_fin}: {_que_falta(campo)}. Cualquier soporte me "
            f"sirve para cerrar el ANS del periodo. Gracias.")


def _texto_escalamiento(reg: AnsRegistro, campo: str, supervisor: str, preguntado: str) -> str:
    return (f"Buen día {_corto(supervisor)}. Le escribo por la orden {reg.pdl} del {reg.fecha_inicio or reg.fecha_fin} "
            f"({reg.zona or ''}): se le pidió a {_corto(preguntado)} {_que_falta(campo)} hace más de 3 días y "
            f"no hemos recibido respuesta. ¿Me puede ayudar a conseguirlo o indicarme quién lo tiene? "
            f"Es para el ANS del periodo. Gracias.")


async def escalar_programacion(db, ahora, prueba: bool) -> list:
    """Consultas a PROGRAMACIÓN (campo zona_responsable, 2026-09-23): sin recordatorios; si la
    primera programadora no responde en ESPERA_H, se le pregunta UNA vez a la siguiente del
    directorio (rol programadora, orden por id). Se detiene si una persona ya corrigió la celda
    (zona puesta y supervisor con nombre) o si el hilo ya no está abierto."""
    from app.api.wa_auditoria import encolar_pregunta, programadoras
    from app.api.ans import _mensaje_programadora, _responsable_es_telefono
    hechos = []
    r = await db.execute(select(WaEnvio).where(WaEnvio.estado == "enviado", WaEnvio.reg_id.isnot(None),
                                               WaEnvio.campo == "zona_responsable").order_by(WaEnvio.id))
    por_caso = {}
    for e in r.scalars().all():
        por_caso.setdefault(e.reg_id, []).append(e)
    lista = await programadoras(db)
    for reg_id, envs in por_caso.items():
        if len(envs) >= 2:
            continue                                        # ya se escaló: no más
        e = envs[-1]
        reg = await db.get(AnsRegistro, reg_id)
        if not reg or ((reg.zona or "").strip() and not _responsable_es_telefono(reg)):
            continue                                        # ya se corrigió la celda
        con = (await db.execute(select(AnsConsulta).where(AnsConsulta.reg_id == reg_id,
                                                          AnsConsulta.campo == "zona_responsable",
                                                          AnsConsulta.estado == "abierta")
                                .order_by(AnsConsulta.id.desc()))).scalars().first()
        if not con:
            continue
        desde = e.enviado_en or e.creado_en
        resp = (await db.execute(select(AnsConsultaMsg.id).where(
            AnsConsultaMsg.consulta_id == con.id, AnsConsultaMsg.direccion == "recibido",
            AnsConsultaMsg.creado_en >= desde).limit(1))).scalars().first()
        if resp or ahora - desde < timedelta(hours=ESPERA_H) or ahora - desde > timedelta(days=14):
            continue
        siguiente = next((p for p in lista if p.nombre.upper() != (e.persona or "").upper() and p.telefono), None)
        if not siguiente:
            hechos.append({"pdl": reg.pdl, "accion": "programación: sin segunda programadora con número para escalar"})
            continue
        texto = _mensaje_programadora(reg, siguiente.nombre, escalada_de=e.persona)
        if prueba:
            hechos.append({"pdl": reg.pdl, "accion": "escalamiento a programación", "a": siguiente.nombre})
            continue
        res = await encolar_pregunta(db, reg, "zona_responsable", siguiente.nombre, siguiente.telefono,
                                     "programadora", texto, forzar=True, origen="escalamiento", nota=False)
        hechos.append({"pdl": reg.pdl, "accion": "escalamiento a programación", "a": siguiente.nombre, "ok": res.get("ok")})
    return hechos


async def main():
    prueba = "--prueba" in sys.argv
    if not settings.WA_RECORDATORIOS and not prueba:
        print("WA_RECORDATORIOS=false: nada que hacer")
        return
    from app.api.wa_auditoria import encolar_pregunta
    from sqlalchemy import func
    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    hechos = []
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(WaEnvio).where(WaEnvio.estado == "enviado", WaEnvio.reg_id.isnot(None),
                                                   WaEnvio.campo.in_(["excluible", "excluible_anulado", "numero_supervisor"]))
                             .order_by(WaEnvio.id))
        envios = r.scalars().all()
        hechos += await escalar_programacion(db, ahora, prueba)
        # Solo el periodo COMPETENTE y pedidos de menos de 14 días: los casos viejos no se
        # recuerdan solos (spam a la gente por periodos ya cerrados).
        competente = (await db.execute(select(func.max(AnsRegistro.periodo)).where(AnsRegistro.entorno == "real"))).scalar()
        # último envío por (reg, campo, persona) y cuántos recordatorios lleva
        ultimo, recordatorios, escalado = {}, {}, set()
        for e in envios:
            k = (e.reg_id, e.campo, e.persona)
            ultimo[k] = e
            if (e.texto or "").startswith("Recordatorio "):
                recordatorios[k] = recordatorios.get(k, 0) + 1
            if "no hemos recibido respuesta" in (e.texto or ""):
                escalado.add((e.reg_id, e.campo))
        for k, e in ultimo.items():
            reg_id, campo, persona = k
            if (e.texto or "").find("no hemos recibido respuesta") >= 0:
                continue                                    # los escalamientos no se recuerdan
            reg = await db.get(AnsRegistro, reg_id)
            if not reg or reg.periodo != competente:
                continue
            if ahora - (e.enviado_en or e.creado_en) > timedelta(days=14):
                continue
            decision = reg.excluible_anulado if campo == "excluible_anulado" else reg.excluible
            if (decision or "").strip():
                continue                                    # ya se decidió: no molestar
            con = (await db.execute(select(AnsConsulta).where(AnsConsulta.reg_id == reg_id, AnsConsulta.campo == campo,
                                                              AnsConsulta.estado == "abierta")
                                    .order_by(AnsConsulta.id.desc()))).scalars().first()
            if not con:
                continue
            desde = e.enviado_en or e.creado_en
            resp = (await db.execute(select(AnsConsultaMsg.id).where(
                AnsConsultaMsg.consulta_id == con.id, AnsConsultaMsg.direccion == "recibido",
                AnsConsultaMsg.creado_en >= desde).limit(1))).scalars().first()
            if resp:
                continue                                    # ya respondió: el procesador de respuestas sigue
            if ahora - desde < timedelta(hours=ESPERA_H):
                continue
            n = recordatorios.get(k, 0)
            if n < MAX_RECORDATORIOS:
                texto = _texto_recordatorio(reg, campo, persona, n + 1)
                if prueba:
                    hechos.append({"pdl": reg.pdl, "accion": f"recordatorio {n + 1}", "a": persona})
                    continue
                res = await encolar_pregunta(db, reg, campo, persona, e.destino, "recordatorio", texto,
                                             forzar=True, origen="recordatorio")
                hechos.append({"pdl": reg.pdl, "accion": f"recordatorio {n + 1}", "a": persona, "ok": res.get("ok")})
                continue
            if (reg_id, campo) in escalado or campo == "numero_supervisor":
                continue                                    # pedir el número no se escala: es al apoyo
            # Escalamiento al supervisor de la zona (o de la orden)
            sup_nombre = (reg.nombre_responsable or "").split("(")[0].strip()
            sup = None
            if sup_nombre and sup_nombre.upper() != (persona or "").upper():
                from app.api.ans import _norm_nombre
                sup = (await db.execute(select(Contacto).where(Contacto.nombre_norm == _norm_nombre(sup_nombre),
                                                               Contacto.activo.is_(True), Contacto.telefono != ""))).scalars().first()
            if not sup and reg.zona:
                sup = (await db.execute(select(Contacto).where(Contacto.rol == "supervisor", Contacto.activo.is_(True),
                                                               Contacto.telefono != "", Contacto.zona.ilike(f"%{reg.zona}%")))).scalars().first()
            if not sup:
                hechos.append({"pdl": reg.pdl, "accion": "sin supervisor con número para escalar"})
                continue
            texto = _texto_escalamiento(reg, campo, sup.nombre, persona)
            if prueba:
                hechos.append({"pdl": reg.pdl, "accion": "escalamiento", "a": sup.nombre})
                continue
            res = await encolar_pregunta(db, reg, campo, sup.nombre, sup.telefono, "supervisor", texto,
                                         forzar=True, origen="escalamiento")
            hechos.append({"pdl": reg.pdl, "accion": "escalamiento", "a": sup.nombre, "ok": res.get("ok")})
    print(f"{datetime.now():%Y-%m-%d %H:%M} · {'PRUEBA · ' if prueba else ''}{len(hechos)} acción(es)")
    for h in hechos:
        print("  ", h)


if __name__ == "__main__":
    asyncio.run(main())
