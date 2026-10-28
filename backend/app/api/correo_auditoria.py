"""Tab «Correos ANS» (SOLO desarrollador): la bandeja de correos etiquetados ANS con sus
PDL cazados, para replicar en un clic el proceso manual — subir el correo a la
celda como evidencia y (cuando se quiera) analizarlo con la IA contra el caso.

  · ADJUNTAR  → copia el .msg como AnsEvidencia de la celda del match (3.2 A o 3.2 B) y
                deja la bitácora estándar. La decisión SI/NO sigue siendo humana.
  · ANALIZAR  → la lectura IA del correo contra el caso (services.claude.analizar_correo,
                el mismo del botón manual) — queda guardada en el correo para auditarla.
  · DESCARTAR → correos que no aplican (spam de la etiqueta, otros periodos). NO es
                definitivo (2026-10-02): en cada carga de STM, en cada «Actualizar» y al decidir
                una celda se vuelve a cruzar; si aparece su caso, el correo regresa solo.

NADA SE PIERDE (auditoría del 2026-10-02, dos fallos detectados el 01-10):
  · Caso 1 — el correo con la solicitud de exclusión se descartó cuando la orden aún no estaba
    en la tabla; al cargar la STM nadie lo volvió a mirar y la celda se marcó NO.
  · Caso 2 — se aprobó SI con el correo del apoyo y después Enel respondió «no es posible
    considerar la exclusión»: entró como «ya excluido en SI · referencia» y nadie lo leyó.
  Ahora: re-cruce de TODOS los correos (no solo los «nuevo»), vuelta automática a la bandeja,
  y un correo que contradice una decisión en SI la pone en RECONSIDERAR (ver correo_bot.flags_vivos).

Se monta con require_dev (main.py): privado del desarrollador.
"""
import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.ans_ctx import current_entorno
from app.models.models import (CorreoAns, AnsRegistro, AnsEvidencia, AnsConsulta, WaAnalisis,
                               AnsCambio)
from app.services.correo import parse_msg, img_a_data_url
from app.services import evid_paquete as _paq

log = logging.getLogger("correo_auditoria")

# Frases de las bitácoras GENÉRICAS (plantilla/espera): no cuentan como observación
# sustantiva y sí se pueden reemplazar sin perder nada.
_OBS_GENERICAS = ("PENDIENTE DETERMINAR", "A LA ESPERA")


def _es_sustantiva(texto: str) -> bool:
    t = (texto or "").strip().upper()
    return bool(t) and not any(g in t for g in _OBS_GENERICAS)


async def _obs_whatsapp(db: AsyncSession, reg_id: int) -> str | None:
    """La observación redactada por el analista de WhatsApp para este caso (si existe).
    Es la redacción rica (videos, relojes, cronología) que NUNCA debe perderse."""
    r = await db.execute(select(WaAnalisis).where(
        WaAnalisis.reg_id == reg_id, WaAnalisis.estado != "descartado")
        .order_by(WaAnalisis.id.desc()))
    for a in r.scalars().all():
        ia = (json.loads(a.resultado or "{}").get("ia") or {})
        if (ia.get("observacion") or "").strip():
            return ia["observacion"].strip()
    return None


async def _fusionar_observacion(db: AsyncSession, reg, campo: str,
                                razon_correo: str) -> tuple[str, str]:
    """La regla anti-pisadas (caso presente en WhatsApp Y correo): si la celda ya
    tiene una observación sustantiva — o el analista de WhatsApp redactó una — se
    CONSERVA LITERAL y el correo se AÑADE como complemento. El correo solo queda solo
    cuando no hay nada mejor. Devuelve (observación final, modo)."""
    from app.api.ans import _CONSULTA_CFG
    razon_field = _CONSULTA_CFG[campo][1]
    actual = (getattr(reg, razon_field) or "").strip()
    base = actual if _es_sustantiva(actual) else (await _obs_whatsapp(db, reg.id))
    if not base:
        return razon_correo, "correo"
    if razon_correo.strip().upper() in base.upper():
        return base, "sin_cambio"                  # el correo ya está incorporado
    anexo = razon_correo.strip()
    anexo = anexo if anexo == anexo.upper() else anexo.upper()   # estilo bitácora
    return f"{base} SE COMPLEMENTA CON CORREO: {anexo}", "combinada"

router = APIRouter(prefix="/ans/correos", tags=["ans-correos"])

EVID_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                        "data", "ans_evidencias"))


# SIN ETIQUETA ANS (14 oct 2026): la regla es «entra solo lo que el operador etiqueta como ANS». Los correos que
# entraron el 8 oct 2026 sin etiqueta del agente (categoría «MO») quedan apartados y no vuelven solos.
def _etiquetado_ans(c: CorreoAns) -> bool:
    cats = [x.strip().upper() for x in (c.categorias or "").split(";")]
    return "ANS" in cats


def _dict(c: CorreoAns, con_cuerpo: bool = False) -> dict:
    from app.api.correo_bot import meta_de
    from app.services.correo import respuesta_enel
    d = {"id": c.id, "asunto": c.asunto, "remitente": c.remitente,
         "fecha_correo": c.fecha_correo, "estado": c.estado,
         "categorias": c.categorias, "etiqueta_ans": _etiquetado_ans(c),
         "evidencia_id": c.evidencia_id,
         "pdls": json.loads(c.pdls) if c.pdls else [],
         "analisis": json.loads(c.analisis) if c.analisis else None,
         "meta": meta_de(c),
         # ¿El mensaje reciente es de Enel y responde la solicitud? {postura, frase literal}
         "respuesta_enel": respuesta_enel(c.remitente, c.cuerpo),
         "creado_en": c.creado_en.isoformat() if c.creado_en else None}
    if con_cuerpo:
        d["cuerpo"] = c.cuerpo
    return d


async def _mapa_competencia(db: AsyncSession) -> dict:
    """reg_id → [ids de correos vivos que le apuntan]. Detecta el caso real:
    primero llega un correo malo y después el bueno — si un caso tiene 2+ correos, el tab
    lo marca (doble/triple correo) para elegir el que más convenga."""
    r = await db.execute(select(CorreoAns).where(CorreoAns.estado != "descartado"))
    por_reg = {}
    for c in r.scalars().all():
        for m in (json.loads(c.pdls) if c.pdls else []):
            if m.get("reg_id"):
                por_reg.setdefault(m["reg_id"], []).append(c.id)
    return por_reg


def _anotar_competencia(d: dict, por_reg: dict) -> dict:
    for m in d.get("pdls", []):
        if m.get("reg_id"):
            otros = [i for i in por_reg.get(m["reg_id"], []) if i != d["id"]]
            if otros:
                m["competencia"] = otros
    return d


async def _refrescar_flags(db: AsyncSession, d: dict, competente: str, c: CorreoAns,
                           cache: dict | None = None) -> dict:
    """Las señales del cruce se recalculan EN VIVO al listar/abrir (no valen las congeladas
    al ingresar): hoy el periodo competente puede ser otro, la celda ya se decidió, o llegó
    la respuesta de Enel. Una sola regla: correo_bot.flags_vivos."""
    from app.api.correo_bot import flags_vivos
    await flags_vivos(db, d.get("pdls", []), estado=c.estado, fecha_correo=c.fecha_correo,
                      remitente=c.remitente, cuerpo=c.cuerpo, meta=d.get("meta"),
                      competente=competente, cache=cache)
    return d


def _correo_del_periodo(d: dict, periodo: str) -> bool:
    """¿Este correo pertenece al periodo que muestra la página? Sí si alguno de sus PDL es
    un caso de ese periodo o está «espera STM» de ese mes; si está amarrado solo a casos de
    OTROS periodos, no; y si no tiene caso alguno, decide la fecha del correo."""
    ms = d.get("pdls") or []
    if any(m.get("periodo") == periodo or m.get("espera_stm") == periodo for m in ms):
        return True
    if any(m.get("reg_id") for m in ms):
        return False
    return (d.get("fecha_correo") or "")[:7] == periodo


@router.get("")
async def listar(periodo: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
                 db: AsyncSession = Depends(get_db)):
    """`periodo` (YYYY-MM): la bandeja de la mano del selector de periodo de la página
    (requisito 2026-09-07). Sin `periodo`, todo como antes."""
    from app.api.correo_bot import _periodo_competente
    competente = await _periodo_competente(db)
    r = await db.execute(select(CorreoAns).order_by(CorreoAns.id.desc()).limit(200))
    por_reg = await _mapa_competencia(db)
    cache = {}
    out = [await _refrescar_flags(db, _anotar_competencia(_dict(c), por_reg), competente, c, cache)
           for c in r.scalars().all()]
    if periodo:
        out = [d for d in out if _correo_del_periodo(d, periodo)]
    return out


def _guardar_meta(c: CorreoAns, meta: dict) -> None:
    c.meta = json.dumps(meta, ensure_ascii=False) if meta else None


async def recruzar_correos(db: AsyncSession, solo_ids: list | None = None,
                           motivo_extra: str | None = None) -> dict:
    """Re-caza los PDL de TODOS los correos contra el ANS DE HOY — también los descartados y
    los ya adjuntos (2026-10-02; antes solo los «nuevo», y así se perdía información).

    · VUELTA AUTOMÁTICA: un correo DESCARTADO regresa a «por revisar» si una orden que NO estaba
      en la tabla cuando se descartó ahora existe y hay algo que hacer con ella, o si Enel negó la
      exclusión de un caso que está en SI. Un correo ya ADJUNTO regresa si le aparece un caso
      nuevo abierto. Queda en `meta.revivido` con el motivo, y se avisa.
    · Devuelve {"n", "pendientes": [(correo_id, objetivos IA)], "revividos": [...]} — los casos
      accionables sin lectura IA se analizan en segundo plano, como al ingresar.
    Lo llaman el botón «Actualizar», la carga de una STM (ans.importar) y la decisión de una
    celda (ans.actualizar → correos_al_decidir)."""
    from app.api.correo_bot import (_cazar_pdls, _accionable, _periodo_competente, flags_vivos,
                                    meta_de, avisar, avisar_contradicciones)
    competente = await _periodo_competente(db)
    q = select(CorreoAns).order_by(CorreoAns.id)
    if solo_ids is not None:
        q = q.where(CorreoAns.id.in_(solo_ids or [0]))
    n, pendientes, revividos, cache = 0, [], [], {}
    for c in (await db.execute(q)).scalars().all():
        if (c.cuerpo or "").startswith("(no se pudo parsear"):
            continue                                  # se repara al abrirlo (_reparar_cuerpo)
        antes = json.loads(c.pdls) if c.pdls else []
        regs_antes = {m["reg_id"] for m in antes if m.get("reg_id")}
        meta = meta_de(c)
        matches = await _cazar_pdls(db, c.asunto or "", c.cuerpo or "", c.fecha_correo)

        async def _flags(estado):
            return await flags_vivos(db, matches, estado=estado, fecha_correo=c.fecha_correo,
                                     remitente=c.remitente, cuerpo=c.cuerpo, meta=meta,
                                     competente=competente, cache=cache)
        await _flags(c.estado)
        motivo = None
        if c.estado == "descartado":
            base = set((meta.get("descarte") or {}).get("casos") or regs_antes)
            await _flags("nuevo")                     # ¿qué habría que hacer si estuviera en la bandeja?
            nuevos = [m for m in matches if m.get("reg_id") and m["reg_id"] not in base and _accionable(m)]
            contra = [m for m in matches if m.get("contradice")]
            if not _etiquetado_ans(c):
                await _flags("descartado")             # sin etiqueta ANS y apartado: se queda apartado
            elif nuevos:
                motivo = f"la orden {nuevos[0]['pdl']} entró a la tabla después de descartarlo"
            elif contra:
                motivo = f"Enel negó la exclusión de {contra[0]['pdl']} y la celda está en SI"
            elif motivo_extra and any(_accionable(m) for m in matches):
                motivo = motivo_extra
            else:
                await _flags("descartado")
        elif c.estado in ("vinculado", "revisado"):
            await _flags("nuevo")
            nuevos = [m for m in matches if m.get("reg_id") and m["reg_id"] not in regs_antes and _accionable(m)]
            contra = [m for m in matches if m.get("contradice")]
            if nuevos:
                motivo = f"le apareció un caso nuevo: {nuevos[0]['pdl']}"
            elif contra:
                motivo = f"Enel negó la exclusión de {contra[0]['pdl']} y la celda está en SI"
            else:
                await _flags(c.estado)
        if motivo:
            meta.setdefault("revivido", []).append(
                {"cuando": datetime.now().isoformat(timespec="minutes"), "motivo": motivo, "antes": c.estado})
            c.estado = "nuevo"
            _guardar_meta(c, meta)
            revividos.append({"id": c.id, "asunto": c.asunto, "motivo": motivo})
            log.info("correo %s vuelve a la bandeja: %s", c.id, motivo)
        c.pdls = json.dumps(matches, ensure_ascii=False)
        n += 1
        if c.estado == "nuevo":
            await avisar_contradicciones(db, c, matches)
            ya = _analisis_mapa(c)
            objetivos = [(m["reg_id"], m["campo"]) for m in matches
                         if _accionable(m) and str(m["reg_id"]) not in ya]
            if objetivos:
                pendientes.append((c.id, objetivos))
    await db.commit()
    for rv in revividos:
        if "Enel negó" in rv["motivo"]:
            continue                                  # ese aviso ya salió (avisar_contradicciones)
        await avisar(f"Sabogal ANS: un correo apartado volvio a la bandeja de Correos ANS: {rv['motivo']}. "
                     f"Revisalo antes de decidir ese caso.")
    return {"n": n, "pendientes": pendientes, "revividos": revividos}


async def recruzar_nuevos(db: AsyncSession) -> tuple[int, list[tuple[int, list]]]:
    """Compatibilidad (ans.importar): el re-cruce completo con la forma de antes."""
    r = await recruzar_correos(db)
    return r["n"], r["pendientes"]


async def correos_al_decidir(db: AsyncSession, reg: AnsRegistro, campo: str) -> tuple[list, list]:
    """AL MARCAR UNA EXCLUSIÓN en la tabla (requisito 2026-10-02: al marcar un ANS se
    aprovecha para re-verificar el cruce, de modo que no se pierda un dato): busca los
    correos que nombran esa orden y NO están adjuntos a la celda, los re-cruza (un descartado
    vuelve a la bandeja) y los devuelve para avisar en el acto. Así no se deja un NO —ni un SI
    sin soporte— habiendo un correo del caso que nadie abrió.
    Devuelve ([{id, estado, asunto, fecha, respuesta_enel, revivido}], pendientes IA)."""
    from app.services.correo import respuesta_enel
    codigo = (reg.pdl or "").strip().upper()
    if len(codigo) < 8:
        return [], []
    candidatos = []
    for c in (await db.execute(select(CorreoAns))).scalars().all():
        en_pdls = any(m.get("reg_id") == reg.id for m in (json.loads(c.pdls) if c.pdls else []))
        if not en_pdls and codigo not in f"{c.asunto or ''} {c.cuerpo or ''}".upper():
            continue
        if await _evidencia_de_correo(db, c, reg.id, campo):
            continue                                  # ya es soporte de esta celda
        candidatos.append(c)
    if not candidatos:
        return [], []
    r = await recruzar_correos(db, solo_ids=[c.id for c in candidatos],
                               motivo_extra=f"se decidió la celda de {reg.pdl} y este correo la nombra")
    vueltos = {x["id"] for x in r["revividos"]}
    out = []
    for c in candidatos:
        await db.refresh(c)
        resp = respuesta_enel(c.remitente, c.cuerpo)
        out.append({"id": c.id, "estado": c.estado, "asunto": (c.asunto or "")[:90],
                    "fecha": (c.fecha_correo or "")[:16], "revivido": c.id in vueltos,
                    "respuesta_enel": resp["postura"] if resp else None})
    return out, r["pendientes"]


@router.post("/recruzar")
async def recruzar(background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Botón «Actualizar» del tab: re-caza los PDL de TODOS los correos contra el ANS DE HOY
    (también descartados y adjuntos). Sirve para el caso típico: el correo llegó antes de
    cargar la orden y quedó «sin caso» — al re-cruzar, el match aparece y, si estaba
    descartado, vuelve solo a la bandeja."""
    from app.api.correo_bot import _auto_analisis
    r = await recruzar_correos(db)
    for cid, objetivos in r["pendientes"]:
        background.add_task(_auto_analisis, cid, objetivos)
    return {"recruzados": r["n"], "analisis_nuevos": sum(len(o) for _, o in r["pendientes"]),
            "revividos": r["revividos"]}


_IA_PENDIENTE_MAX_MIN = 10   # un slot «pendiente» más viejo que esto se considera caído y se reintenta


async def _lanzar_lecturas_faltantes(db: AsyncSession, c: CorreoAns, d: dict,
                                     background: BackgroundTasks) -> list:
    """Al ABRIR un correo: todo caso accionable sin lectura IA la dispara en segundo plano
    (mismo motor del ingreso). Cubre el hueco reportado el 2026-09-04: el MT16
    entró como «otro periodo» (sin IA), luego pasó a accionable y al abrirlo no había
    razonamiento — solo botones. Se deja un slot «pendiente» con hora para no disparar
    dos veces mientras corre; si se cae, a los 10 min se reintenta. Devuelve los reg_id
    lanzados."""
    from app.api.correo_bot import _accionable, _auto_analisis
    if c.estado == "descartado":
        return []
    prev = _analisis_mapa(c)
    ahora = datetime.now()
    objetivos = []
    for m in d.get("pdls", []):
        if not _accionable(m):
            continue
        slot = prev.get(str(m["reg_id"]))
        if slot and not slot.get("pendiente"):
            continue                                   # ya hay lectura (o error guardado)
        if slot and slot.get("pendiente"):
            try:
                inicio = datetime.fromisoformat(slot.get("iniciado") or "")
            except ValueError:
                inicio = ahora
            if (ahora - inicio) < timedelta(minutes=_IA_PENDIENTE_MAX_MIN):
                continue                               # sigue corriendo
        prev[str(m["reg_id"])] = {"reg_id": m["reg_id"], "campo": m["campo"], "pdl": m["pdl"],
                                  "pendiente": True, "iniciado": ahora.isoformat()}
        objetivos.append((m["reg_id"], m["campo"]))
    if objetivos:
        c.analisis = json.dumps(prev, ensure_ascii=False)
        await db.commit()
        background.add_task(_auto_analisis, c.id, objetivos)
        d["analisis"] = prev
    return [r for r, _ in objetivos]


async def _reparar_cuerpo(db: AsyncSession, c: CorreoAns) -> bool:
    """Un correo que se guardó SIN cuerpo (codepage mal declarado en el .msg, 2026-09-23) se
    re-lee con el parser tolerante al abrirlo: se guarda el cuerpo y se vuelven a cazar sus
    PDL, que sin cuerpo salían vacíos. Devuelve True si lo reparó."""
    if not (c.cuerpo or "").startswith("(no se pudo parsear"):
        return False
    if not c.msg_ruta or not os.path.exists(c.msg_ruta):
        return False
    from app.api.correo_bot import _cazar_pdls
    try:
        p = parse_msg(c.msg_ruta)
    except Exception as e:                                # noqa: BLE001 — sigue roto: se deja el aviso
        log.warning("correo %s: el .msg sigue sin poder leerse: %s", c.id, e)
        return False
    c.cuerpo = p.get("cuerpo") or ""
    c.asunto = c.asunto or (p.get("asunto") or "")[:400] or None
    c.remitente = c.remitente or (p.get("de") or "")[:200] or None
    matches = await _cazar_pdls(db, c.asunto or "", c.cuerpo, c.fecha_correo)
    c.pdls = json.dumps(matches, ensure_ascii=False)
    await db.commit()
    log.info("correo %s reparado: %d caracteres de cuerpo · PDL %s", c.id, len(c.cuerpo),
             [m["pdl"] for m in matches])
    return True


@router.get("/{cid}")
async def detalle(cid: int, background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    c = await db.get(CorreoAns, cid)
    if not c:
        raise HTTPException(status_code=404, detail="Correo no encontrado")
    await _reparar_cuerpo(db, c)
    from app.api.correo_bot import _periodo_competente
    por_reg = await _mapa_competencia(db)
    d = _anotar_competencia(_dict(c, con_cuerpo=True), por_reg)
    d = await _refrescar_flags(db, d, await _periodo_competente(db), c)
    d["ia_lanzada"] = await _lanzar_lecturas_faltantes(db, c, d, background)
    # ── Cobertura EN VIVO también para análisis viejos (regla general 2026-08-25: los
    # ajustes son para TODOS los casos): si un análisis 3.2 A guardado no trae
    # cobertura_calc, se calcula aquí con puro código ($0); los del esquema viejo (sin
    # tramos declarados) quedan marcados `legado` y el tab lo dice en vez de fingir
    # un «no cubre».
    from app.services.wa_analista import _calcular_cobertura
    for a in (d.get("analisis") or {}).values():
        if not isinstance(a, dict) or a.get("campo") != "excluible":
            continue
        if a.get("error") or a.get("cobertura_calc") or a.get("pendiente"):
            continue
        reg_a = await db.get(AnsRegistro, a.get("reg_id") or 0)
        if not reg_a:
            continue
        cov = _calcular_cobertura(reg_a, a)
        if "tramos_justificados" not in a:
            cov["legado"] = True
        a["cobertura_calc"] = cov
    # CRONOLOGÍA del caso con varios correos (el escenario típico: llega un correo
    # malo y días después el que corrige): por cada otro correo del mismo caso, su fecha
    # y el veredicto que la IA le dio A ESTE caso — así se ve qué pasó y con cuál aprobar.
    for m in d.get("pdls", []):
        if not m.get("competencia"):
            continue
        otros = []
        for oid in m["competencia"]:
            o = await db.get(CorreoAns, oid)
            if not o:
                continue
            a = _analisis_mapa(o).get(str(m["reg_id"]))
            otros.append({"id": o.id, "fecha": o.fecha_correo, "estado": o.estado,
                          "asunto": (o.asunto or "")[:80],
                          "veredicto": ({"excluible": a.get("excluible"),
                                         "relacionado": a.get("relacionado"),
                                         "revision": bool(a.get("revision"))} if a else None)})
        m["competencia_detalle"] = sorted(otros, key=lambda x: x.get("fecha") or "")
    # ¿El caso ya tiene observación rica (WhatsApp o manual)? El tab lo avisa ANTES de
    # aprobar: al aprobar se combina, no se reemplaza (caso con evidencia en ambos canales).
    from app.api.ans import _CONSULTA_CFG
    for m in d.get("pdls", []):
        if not m.get("reg_id"):
            continue
        reg = await db.get(AnsRegistro, m["reg_id"])
        if not reg:
            continue
        actual = (getattr(reg, _CONSULTA_CFG[m["campo"]][1]) or "").strip()
        if _es_sustantiva(actual):
            m["obs_previa"] = {"origen": "celda", "texto": actual[:500]}
        else:
            wa = await _obs_whatsapp(db, m["reg_id"])
            if wa:
                m["obs_previa"] = {"origen": "whatsapp", "texto": wa[:500]}
    return d


@router.get("/{cid}/descargar")
async def descargar(cid: int, db: AsyncSession = Depends(get_db)):
    c = await db.get(CorreoAns, cid)
    if not c or not c.msg_ruta or not os.path.exists(c.msg_ruta):
        raise HTTPException(status_code=404, detail="El .msg no está en el servidor")
    nombre = re.sub(r"[^\w.\-]", "_", (c.asunto or "correo"))[:80] + ".msg"
    return StreamingResponse(open(c.msg_ruta, "rb"),
                             media_type="application/vnd.ms-outlook",
                             headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


async def _evidencia_de_correo(db: AsyncSession, c: CorreoAns, reg_id: int, campo: str):
    """La AnsEvidencia que ESTE correo ya dejó en esa celda (si existe). Se reconoce por la
    marca «correo ANS #id» de la nota; para las viejas (sin marca) vale evidencia_id."""
    r = await db.execute(select(AnsEvidencia).where(
        AnsEvidencia.reg_id == reg_id, AnsEvidencia.campo == campo,
        AnsEvidencia.mime == "application/vnd.ms-outlook"))
    for e in r.scalars().all():
        if f"correo ANS #{c.id}]" in (e.nota or "") or e.id == c.evidencia_id:
            return e
    return None


async def _adjuntar_msg(db: AsyncSession, c: CorreoAns, reg, campo: str,
                        entorno: str | None = None, origen: str = "adjuntado desde el tab Correos ANS"):
    """Copia el .msg del correo a la carpeta de evidencias de la celda y crea la AnsEvidencia.
    Es lo que hace visible el correo en la pestaña Evidencias (carpeta 3.2 B - Anulaciones /
    3.2 A - Tiempos) y lo mete en el ZIP que se envía. Lo usan ADJUNTAR y APROBAR: aprobar
    sin adjuntar dejaba el caso sin soporte en el paquete (bug 2026-09-04: 13
    correos aprobados en agosto no estaban en el exporte)."""
    if not c.msg_ruta or not os.path.exists(c.msg_ruta):
        raise HTTPException(status_code=404, detail="El .msg de este correo no está en el servidor")
    carpeta = os.path.join(EVID_DIR, entorno or current_entorno(), str(reg.id), campo)
    os.makedirs(carpeta, exist_ok=True)
    seguro = re.sub(r"[^\w.\-]", "_", (c.asunto or "correo"))[:80]
    destino = os.path.join(carpeta, f"{int(datetime.now().timestamp())}_{seguro}.msg")
    shutil.copyfile(c.msg_ruta, destino)
    e = AnsEvidencia(reg_id=reg.id, campo=campo,
                     nombre=(c.asunto or "Correo ANS")[:290] + ".msg",
                     ruta=destino, mime="application/vnd.ms-outlook",
                     tamano=os.path.getsize(destino),
                     nota=f"Correo etiquetado ANS en Outlook (de {c.remitente or 's/d'}, "
                          f"{c.fecha_correo or 's/d'}); {origen} [correo ANS #{c.id}]")
    _paq.defaults_nueva(e, fecha_txt=c.fecha_correo)   # PAQUETE PARA ENEL (14 oct 2026)
    db.add(e)
    await db.flush()
    if not c.evidencia_id:
        c.evidencia_id = e.id
    return e


@router.post("/{cid}/aplicar")
async def aplicar(cid: int, datos: dict = Body(...), db: AsyncSession = Depends(get_db)):
    """Adjunta el correo a la celda del caso elegido — el mismo efecto del flujo manual
    de subir el .msg como evidencia. Body: {reg_id, campo}."""
    c = await db.get(CorreoAns, cid)
    if not c or not c.msg_ruta or not os.path.exists(c.msg_ruta):
        raise HTTPException(status_code=404, detail="Correo no encontrado en el servidor")
    reg = await db.get(AnsRegistro, int(datos.get("reg_id") or 0))
    if not reg:
        raise HTTPException(status_code=404, detail="Registro del ANS no encontrado")
    campo = datos.get("campo") if datos.get("campo") in ("excluible", "excluible_anulado") \
        else "excluible_anulado"

    e = await _adjuntar_msg(db, c, reg, campo)

    # La bitácora estándar del correo-soporte (misma redacción del flujo manual)
    from app.api.ans import _escribir_nota, _hilo_de, _CONSULTA_CFG
    razon_field = _CONSULTA_CFG[campo][1]
    con = await _hilo_de(db, reg.id, campo)
    if not con:
        con = AnsConsulta(reg_id=reg.id, campo=campo, estado="abierta")
        db.add(con)
        await db.flush()
    pend = "" if (getattr(reg, campo, None) or "").strip() else " PENDIENTE DETERMINAR LA EXCLUSIÓN."
    if campo == "excluible_anulado":
        nota = "SE ANEXA CORREO DEL CENTRO OPERATIVO COMO SOPORTE DE LA ANULACIÓN." + pend
    else:
        nota = "SE ANEXA 1 CORREO COMO SOPORTE." + pend
    _escribir_nota(reg, con, razon_field, nota)

    c.estado, c.evidencia_id = "vinculado", e.id
    await db.commit()
    return {"ok": True, "evidencia_id": e.id, "reg_id": reg.id, "campo": campo}


def _analisis_mapa(c: CorreoAns) -> dict:
    """El mapa {reg_id: análisis} del correo (migrando al vuelo el formato viejo plano)."""
    try:
        prev = json.loads(c.analisis) if c.analisis else {}
    except ValueError:
        prev = {}
    if isinstance(prev, dict) and "reg_id" in prev:
        prev = {str(prev["reg_id"]): prev}
    return prev if isinstance(prev, dict) else {}


async def analizar_y_guardar(db: AsyncSession, c: CorreoAns, reg: AnsRegistro,
                             campo: str) -> dict:
    """El motor de la lectura IA correo↔caso, compartido por el botón del tab y el
    ANÁLISIS AUTOMÁTICO al ingresar (correo_bot lo dispara en segundo plano para los
    matches pendientes del periodo — requisito: al abrir el correo, la lectura
    ya debe estar). Guarda en el slot del caso y devuelve el análisis."""
    from app.api.ans import _ctx_orden, _CONSULTA_CFG
    from app.models.models import AnsConsulta, AnsConsultaMsg
    from app.services import claude
    p = parse_msg(c.msg_ruta)
    imgs = [img_a_data_url(n, d) for n, d in p.get("imagenes", [])]
    label = _CONSULTA_CFG[campo][2]

    # ── CONVERGENCIA de canales (requisito 2026-08-25): si al caso ya se le
    # preguntó por WhatsApp (y la persona respondió o anunció «ya le envío el correo»),
    # la lectura del correo INCLUYE ese hilo — verifica coherencia chat↔correo y la
    # observación sale UNA sola, sustentada en el correo (el soporte formal).
    rc = await db.execute(select(AnsConsulta).where(
        AnsConsulta.reg_id == reg.id, AnsConsulta.campo == campo)
        .order_by(AnsConsulta.id))
    lineas = []
    for con in rc.scalars().all():
        rm = await db.execute(select(AnsConsultaMsg)
                              .where(AnsConsultaMsg.consulta_id == con.id)
                              .order_by(AnsConsultaMsg.creado_en, AnsConsultaMsg.id))
        for m in rm.scalars().all():
            hora = ((m.creado_en - timedelta(hours=5)).strftime("%d/%m %H:%M")
                    if m.creado_en else "¿?")
            rol = {"enviado": "NOSOTROS", "ia": "LECTURA-IA"}.get(
                m.direccion, m.persona or "RESPUESTA")
            lineas.append(f"[{hora}] {rol}: {(m.texto or '')[:400]}")
    hilo_txt = "\n".join(lineas) if lineas else None

    # ── LA RESPUESTA DE ENEL MANDA (2026-10-02): si el mensaje reciente es de Enel y
    # acepta o niega la exclusión, eso se detecta con reglas (frase literal) ANTES de la IA, se
    # le dice a la IA, y después se impone: la IA redacta, no decide contra un escrito de Enel.
    from app.services.correo import respuesta_enel
    resp = respuesta_enel(c.remitente or p.get("de"), c.cuerpo or p.get("cuerpo"))
    decision_hoy = (getattr(reg, campo, None) or "").strip().upper()
    pista = None
    if resp:
        pista = (f"DETECCIÓN DEL SISTEMA (determinista): el mensaje más reciente lo escribe ENEL "
                 f"({resp['de'] or 's/d'}) y {'NIEGA' if resp['postura'] == 'NIEGA' else 'ACEPTA'} la "
                 f"exclusión solicitada. Frase literal: «{resp['frase']}». "
                 + (f"HOY la celda de exclusión está en {decision_hoy}. " if decision_hoy else "")
                 + "Aplica la regla RESPUESTA DE ENEL.")
    elif decision_hoy:
        pista = (f"HOY la celda de exclusión de esta orden está en {decision_hoy}. Si este correo la "
                 f"contradice o la confirma, dilo en el razonamiento.")
    # UN CORREO, VARIOS CASOS (2026-10-08): un correo puede justificar varias órdenes a la vez
    # (un correo con 6 PDL cancelados en septiembre, cada uno con su motivo). Cada lectura es de UNA
    # orden: se le dice a la IA cuál es la suya y cuáles son las otras, para que no mezcle los
    # motivos ni diga «no corresponde» por ver otros códigos.
    otros = [m["pdl"] for m in (json.loads(c.pdls) if c.pdls else []) if m.get("pdl") and m["pdl"] != (reg.pdl or "").strip().upper()]
    if otros:
        nota_multi = (f"ESTE CORREO NOMBRA VARIAS ÓRDENES ({', '.join(otros[:12])}). Esta lectura es SOLO sobre "
                      f"{reg.pdl}: toma el motivo que el correo da para ESA orden (su línea o frase), ignora los "
                      f"motivos de las otras y no la declares «no relacionada» solo porque hay más códigos.")
        pista = f"{pista}\n{nota_multi}" if pista else nota_multi

    # La FECHA que ve la IA es la hora de Colombia (la que guarda la bandeja). La del .msg viene
    # en UTC: el correo de Enel del 01/10 9:34 p. m. se citaba como «correo del 02/10» y los de
    # las 13:08 como «18:08» (2026-10-02) — y esa fecha termina escrita en la celda.
    fecha_ia = (f"{c.fecha_correo[:16]} (hora de Colombia)" if (c.fecha_correo or "").strip()
                else p.get("fecha"))
    d = await claude.analizar_correo(
        _ctx_orden(reg, label),
        {"asunto": p.get("asunto"), "de": p.get("de"), "fecha": fecha_ia,
         "cuerpo": p.get("cuerpo")},
        imgs, indicador="3.2A" if campo == "excluible" else "3.2B",
        hilo_wa=hilo_txt, pista=pista)
    if lineas:
        d["hilo_wa_msgs"] = len(lineas)
    if resp and not d.get("error"):
        ia_decia = d.get("excluible")
        quiere = "NO" if resp["postura"] == "NIEGA" else "SI"
        d["respuesta_enel"] = resp
        d["relacionado"] = "SI"
        d["citas"] = [resp["frase"][:200]] + [x for x in (d.get("citas") or []) if x not in resp["frase"]][:3]
        if ia_decia != quiere:
            d.setdefault("validacion", []).append(
                f"Enel {'negó' if quiere == 'NO' else 'aceptó'} la exclusión por escrito: la lectura queda en "
                f"{quiere} (la IA decía {ia_decia or '—'})")
            d["excluible"] = quiere
            fecha_txt = (c.fecha_correo or "")[:10]
            fecha_txt = "/".join(reversed(fecha_txt.split("-"))) if fecha_txt.count("-") == 2 else fecha_txt
            d["razon"] = (f"ENEL NIEGA LA EXCLUSIÓN SOLICITADA (CORREO DEL {fecha_txt}): LA CAUSA CORRESPONDE A LA "
                          f"GESTIÓN DEL CONTRATISTA. NO EXCLUIBLE." if quiere == "NO" else
                          f"ENEL ACEPTA LA EXCLUSIÓN (CORREO DEL {fecha_txt}). SE ANEXA CORREO SOPORTE.")
        if quiere == "NO":
            d["categoria"] = "INTERNO"           # Enel la atribuye al contratista: no hay causa externa
            if campo == "excluible":
                d["causa_tipo"] = "interna"
    # ── Regla GENERAL de tiempos (auditoría 2026-08-24, mismo motor de WhatsApp) ──
    # La cobertura del desfase la calcula el código; correo 3.2 A relacionado que no
    # cubre los tiempos dentro de la tolerancia → REVISIÓN: la máquina no se compromete
    # (su inclinación queda en veredicto_ia) y el SI/NO definitivo es del ING.
    if campo == "excluible" and not d.get("error"):
        from app.services.wa_analista import _calcular_cobertura
        cov = _calcular_cobertura(reg, d)
        d["cobertura_calc"] = cov
        # Un escrito de Enel que acepta o niega es concluyente: no pasa a Revisión por tiempos
        if d.get("relacionado") == "SI" and cov["aplicable"] and not cov["cubierto"] and not resp:
            faltas = " · ".join(f"faltan {g['faltan']} min de {gap}"
                                for gap, g in cov["gaps"].items() if not g["cubierto"])
            d["veredicto_ia"] = d.get("excluible")
            d["excluible"] = None
            d["revision"] = {"motivo": (f"el correo no cubre los tiempos dentro de la "
                                        f"tolerancia de {cov['tolerancia_min']} min por "
                                        f"gap ({faltas})")}
    prev = _analisis_mapa(c)
    prev[str(reg.id)] = {"reg_id": reg.id, "campo": campo, "pdl": reg.pdl,
                         "cuando": datetime.now().isoformat(), **d}
    c.analisis = json.dumps(prev, ensure_ascii=False)
    await db.commit()
    return prev[str(reg.id)]


@router.post("/{cid}/analizar")
async def analizar(cid: int, datos: dict = Body(...), db: AsyncSession = Depends(get_db)):
    """La lectura IA del correo contra un caso (el mismo motor del botón manual de la
    celda). Body: {reg_id, campo}. El resultado queda guardado en el correo."""
    c = await db.get(CorreoAns, cid)
    if not c or not c.msg_ruta or not os.path.exists(c.msg_ruta):
        raise HTTPException(status_code=404, detail="Correo no encontrado en el servidor")
    reg = await db.get(AnsRegistro, int(datos.get("reg_id") or 0))
    if not reg:
        raise HTTPException(status_code=404, detail="Registro del ANS no encontrado")
    campo = datos.get("campo") if datos.get("campo") in ("excluible", "excluible_anulado") \
        else "excluible_anulado"
    an = await analizar_y_guardar(db, c, reg, campo)
    return {"ok": True, "analisis": an}


@router.post("/{cid}/descartar")
async def descartar(cid: int, db: AsyncSession = Depends(get_db)):
    """Aparta el correo de la bandeja. NO es definitivo (2026-10-02): se guarda qué casos tenía
    al descartarlo; en cada carga de STM / «Actualizar» / decisión de celda se vuelve a cruzar
    y, si aparece una orden que entonces no estaba, regresa solo."""
    from app.api.correo_bot import meta_de
    c = await db.get(CorreoAns, cid)
    if not c:
        raise HTTPException(status_code=404, detail="Correo no encontrado")
    ms = json.loads(c.pdls) if c.pdls else []
    meta = meta_de(c)
    meta["descarte"] = {"cuando": datetime.now().isoformat(timespec="minutes"),
                        "casos": [m["reg_id"] for m in ms if m.get("reg_id")],
                        "sin_caso": [m["pdl"] for m in ms if not m.get("reg_id")]}
    _guardar_meta(c, meta)
    c.estado = "descartado"
    await db.commit()
    return {"ok": True, "espera": meta["descarte"]["sin_caso"]}


@router.post("/{cid}/restaurar")
async def restaurar(cid: int, background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Devuelve a la bandeja un correo descartado (o revisado) y lo re-cruza."""
    from app.api.correo_bot import meta_de, _auto_analisis
    c = await db.get(CorreoAns, cid)
    if not c:
        raise HTTPException(status_code=404, detail="Correo no encontrado")
    meta = meta_de(c)
    meta.setdefault("revivido", []).append({"cuando": datetime.now().isoformat(timespec="minutes"),
                                            "motivo": "restaurado a mano", "antes": c.estado})
    meta.pop("revisado", None)
    _guardar_meta(c, meta)
    c.estado = "nuevo"
    await db.commit()
    r = await recruzar_correos(db, solo_ids=[c.id])
    for cid2, objetivos in r["pendientes"]:
        background.add_task(_auto_analisis, cid2, objetivos)
    return {"ok": True}


@router.post("/{cid}/mantener")
async def mantener(cid: int, datos: dict = Body(...), db: AsyncSession = Depends(get_db)):
    """«Ya lo revisé: mantengo mi decisión». Para un correo que llegó después de la decisión
    (o que la contradice): deja constancia en el correo y en el historial de la celda, y el
    caso deja de pedir reconsideración por ESTE correo. No cambia la celda. Body: {reg_id, campo}."""
    from app.api.correo_bot import meta_de, _periodo_competente, flags_vivos, _accionable
    c = await db.get(CorreoAns, cid)
    if not c:
        raise HTTPException(status_code=404, detail="Correo no encontrado")
    reg = await db.get(AnsRegistro, int(datos.get("reg_id") or 0))
    if not reg:
        raise HTTPException(status_code=404, detail="Registro del ANS no encontrado")
    campo = datos.get("campo") if datos.get("campo") in ("excluible", "excluible_anulado") \
        else "excluible_anulado"
    decision = (getattr(reg, campo, None) or "").strip().upper()
    meta = meta_de(c)
    meta.setdefault("revisado", {})[str(reg.id)] = {
        "cuando": datetime.now().isoformat(timespec="minutes"), "decision": decision or None}
    _guardar_meta(c, meta)
    # Constancia en el historial de la celda (no cambia el valor: queda quién revisó y qué)
    db.add(AnsCambio(registro_id=reg.id, periodo=reg.periodo, protocolo=reg.protocolo, campo=campo,
                     valor_anterior=decision or None, valor_nuevo=decision or None, tipo="edicion",
                     usuario_id=None,
                     usuario_nombre=f"Correos ANS (revisado el correo #{c.id}: se mantiene {decision or 'sin decidir'})"))
    matches = json.loads(c.pdls) if c.pdls else []
    await flags_vivos(db, matches, estado=c.estado, fecha_correo=c.fecha_correo, remitente=c.remitente,
                      cuerpo=c.cuerpo, meta=meta, competente=await _periodo_competente(db))
    c.pdls = json.dumps(matches, ensure_ascii=False)
    if c.estado == "nuevo" and not any(_accionable(m) for m in matches):
        c.estado = "revisado"                       # nada más que hacer con este correo
    await db.commit()
    return {"ok": True, "estado": c.estado, "decision": decision}


@router.post("/{cid}/aprobar")
async def aprobar(cid: int, datos: dict = Body(...), db: AsyncSession = Depends(get_db)):
    """Aprobación de la persona responsable sobre la lectura IA del correo — el paso final del flujo:
    Body: {reg_id, campo, decision: 'SI'|'NO'|null, razon}.

    · Siempre escribe la OBSERVACIÓN (la razón, editada o la de la IA) en la celda.
    · 3.2 B con decision: escribe también excluible_anulado y ajusta SE EJECUTÓ
      (N/A si se excluye, NO si no) + reglas de nodos — idéntico a consulta_aplicar.
    · 3.2 A con decision: escribe también `excluible` (celda AL). Regla del proceso
      2026-07-21: el SI/NO de tiempos NO lo decide la IA — sigue siendo así: la IA solo
      propone y la PERSONA lo marca; desde 2026-09-08 lo marca aquí mismo con los botones
      («debería tener los botoncitos de sí o no aquí mismo») en vez de ir al grid.
    · Cada celda que cambia (decisión, observación, SE EJECUTÓ) queda en `ans_cambios`
      igual que una edición del grid: reversible desde el historial.
    · `razon_editada: true` = la persona ESCRIBIÓ la observación en el tab → se guarda
      TAL CUAL (reemplaza la celda). La fusión anti-pisadas («se conserva y se añade») es
      solo para la redacción de la IA. Hallazgo 2026-09-08 («no se actualiza la observación»):
      su texto quedaba escondido al final de la celda vieja tras «SE COMPLEMENTA CON CORREO».
    """
    c = await db.get(CorreoAns, cid)
    if not c:
        raise HTTPException(status_code=404, detail="Correo no encontrado")
    reg = await db.get(AnsRegistro, int(datos.get("reg_id") or 0))
    if not reg:
        raise HTTPException(status_code=404, detail="Registro del ANS no encontrado")
    campo = datos.get("campo") if datos.get("campo") in ("excluible", "excluible_anulado") \
        else "excluible_anulado"
    razon = (datos.get("razon") or "").strip()
    if not razon:
        an = json.loads(c.analisis) if c.analisis else {}
        if isinstance(an, dict) and "reg_id" not in an:      # formato mapa {reg_id: análisis}
            an = an.get(str(reg.id)) or {}
        razon = (an.get("razon") or "").strip()
    if not razon:
        raise HTTPException(status_code=400, detail="No hay observación/razón para escribir")
    decision = datos.get("decision") if datos.get("decision") in ("SI", "NO") else None

    from app.api.ans import (_CONSULTA_CFG, _hilo_de, _log_reglas_nodos, _eval_reglas_nodos)
    razon_field = _CONSULTA_CFG[campo][1]
    antes = {razon_field: getattr(reg, razon_field), campo: getattr(reg, campo),
             "se_ejecuto": reg.se_ejecuto}
    # Fusión anti-pisadas SOLO para la redacción de la IA: si el caso ya tiene observación
    # rica (WhatsApp o manual), se conserva literal y el correo se añade. Si la persona
    # editó el texto en el tab, eso ES la observación: se escribe tal cual.
    if datos.get("razon_editada"):
        modo_obs = "manual"
    else:
        razon, modo_obs = await _fusionar_observacion(db, reg, campo, razon)
    setattr(reg, razon_field, razon)
    extra = {}
    if decision:
        setattr(reg, campo, decision)
        if campo == "excluible_anulado":     # regla del ANS: excluida → N/A; no → NO
            reg.se_ejecuto = "N/A" if decision == "SI" else "NO"
            extra["se_ejecuto"] = reg.se_ejecuto
        _log_reglas_nodos(db, reg, _eval_reglas_nodos(reg))
    # Historial como el del grid: cada celda que cambió queda en ans_cambios (reversible).
    for k, v_antes in antes.items():
        v_ahora = getattr(reg, k)
        if (v_antes or "") != (v_ahora or ""):
            db.add(AnsCambio(registro_id=reg.id, periodo=reg.periodo, protocolo=reg.protocolo,
                             campo=k, valor_anterior=v_antes, valor_nuevo=v_ahora, tipo="edicion",
                             usuario_id=None, usuario_nombre=f"Correos ANS (aprobar correo #{c.id})"))

    # El hilo de consulta queda resuelto con esta razón como nota final (como al aplicar
    # desde el chat guiado) — así la campanita deja de pedir el caso.
    con = await _hilo_de(db, reg.id, campo)
    if not con:
        con = AnsConsulta(reg_id=reg.id, campo=campo, estado="abierta")
        db.add(con)
        await db.flush()
    con.nota_auto = razon
    con.razon_sug = razon
    if decision:
        con.excluible_sug = decision
        con.estado = "resuelta"

    # El correo queda ADJUNTO como evidencia de la celda automáticamente (si no lo estaba):
    # aprobar es la decisión final, y la decisión sin su soporte en el paquete no sirve.
    ev = await _evidencia_de_correo(db, c, reg.id, campo)
    if not ev:
        ev = await _adjuntar_msg(db, c, reg, campo, origen="adjuntado al aprobar desde el tab Correos ANS")
        extra["evidencia_nueva"] = True
    extra["evidencia_id"] = ev.id
    c.estado = "vinculado"
    await db.commit()
    return {"ok": True, "reg_id": reg.id, "campo_razon": razon_field, "razon": razon,
            "decision": decision, "modo_obs": modo_obs, **extra}


@router.get("/agente/estado")
async def agente_estado(db: AsyncSession = Depends(get_db)):
    """¿Cuándo fue la última pasada del agente del PC? (latido que él mismo reporta) + el
    periodo competente (último con STM) y, si el calendario ya va adelante, la STM que falta."""
    from app.api.correo_bot import _periodo_competente, periodos_abiertos
    ruta = os.path.join(os.path.dirname(EVID_DIR), "correos_ans", "latido.txt")
    try:
        ultima = open(ruta).read().strip()
    except OSError:
        ultima = None
    competente = await _periodo_competente(db)
    hoy = datetime.now().strftime("%Y-%m")
    return {"ultima_pasada": ultima, "periodo_competente": competente,
            "periodos_abiertos": periodos_abiertos(competente),
            "stm_pendiente": hoy if hoy > competente else None}
