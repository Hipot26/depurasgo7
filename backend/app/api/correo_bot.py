"""Ingesta de correos ANS desde el agente local (correo-bot/cazador_ans.py).

El agente corre EN EL PC del operador (Outlook Classic por COM — el servidor jamás ve el buzón):
busca los correos con categoría «ANS», los exporta a .msg y los sube aquí con el mismo
token de servicio del bot de WhatsApp (X-Bot-Token).

Al recibir cada correo:
  1. Se guarda el .msg en data/correos_ans/<YYYY-MM>/ y se parsea (services.correo).
  2. Se CAZAN los códigos PDL (técnica CazadorPDL: regex sobre asunto + cuerpo).
  3. Se cruzan contra los registros del ANS (entorno real): cada match queda en `pdls`
     con el caso y su tipo (anulada → 3.2 B; ejecutada → 3.2 A).
El tab «Correos ANS» (correo_auditoria.py) es donde la persona responsable decide qué se adjunta a qué celda.
"""
import json
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import get_db
from app.models.models import CorreoAns, AnsRegistro
from app.api.whatsapp_bot import require_bot

router = APIRouter(prefix="/correo-bot", tags=["correo-bot"])

CORREOS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                           "data", "correos_ans"))
# Formato de los códigos PDL: CDOPP<año>R<n> (programados) / CDOPT<año>R<n> (línea viva)
RE_PDL = re.compile(r"\bCDOP[A-Z][A-Z0-9]{6,14}\b", re.IGNORECASE)
# Corta el cuerpo en «mensaje reciente» vs «cadena de reenvíos vieja»: la cadena suele
# traer códigos de OTROS casos/periodos (técnica CazadorPDL — nos pasó con «bocatoma»:
# el asunto traía un PDL de junio y el correo era realmente sobre uno de agosto).
RE_CORTE_CADENA = re.compile(r"\n\s*(?:De|From|-{3,}\s*Mensaje original)\s*:", re.IGNORECASE)


async def _periodo_competente(db: AsyncSession) -> str:
    """El periodo sobre el que se TRABAJA en Correos ANS: el último mes con STM cargada
    (max periodo de ans_registros reales), NO el mes del calendario. Regla del proceso
    (2026-09-03): «agosto está al día en teoría, pero la STM de septiembre no se ha subido:
    esos correos no deben descartarse» — mientras no entre la STM del mes nuevo, los correos
    que llegan siguen sirviendo a los casos del mes anterior."""
    from sqlalchemy import func
    r = await db.execute(select(func.max(AnsRegistro.periodo)).where(AnsRegistro.entorno == "real"))
    return r.scalar_one_or_none() or datetime.now().strftime("%Y-%m")


def _mes_de_fecha(fecha: Optional[str]) -> Optional[str]:
    """YYYY-MM de la fecha del correo (Outlook la da como '2026-09-03 20:45:58…')."""
    m = re.search(r"(\d{4})-(\d{2})", fecha or "")
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _mes_menos(periodo: str, n: int) -> str:
    """YYYY-MM n meses atrás."""
    y, m = int(periodo[:4]), int(periodo[5:7]) - n
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def periodos_abiertos(competente: str) -> list[str]:
    """Los periodos sobre los que TODAVÍA se trabaja: el competente y los N anteriores
    (settings.CORREOS_PERIODOS_ABIERTOS, 1 por defecto). Regla del proceso (2026-09-08, un correo
    para un caso de agosto cuando la STM de septiembre ya estaba cargada):
    «información que logre salvar un caso debe ser analizada y sustentada» — el ANS de
    agosto se reporta en septiembre, así que agosto sigue abierto mientras entra septiembre.
    Un caso de periodo abierto en NO/vacío es trabajo; solo el SI o un periodo cerrado lo
    dejan como referencia."""
    n = max(0, int(settings.CORREOS_PERIODOS_ABIERTOS or 0))
    return [competente] + [_mes_menos(competente, i) for i in range(1, n + 1)]


def _flags_match(reg: AnsRegistro, competente: str) -> dict:
    """Señales del cruce PDL→caso, calculadas EN VIVO (no se congelan al ingresar):
    · tipo/campo: anulada → 3.2 B, ejecutada → 3.2 A.
    · otro_periodo: el caso no es del periodo competente (último con STM) — informativo.
    · periodo_cerrado: el caso es de un periodo que YA no se trabaja (más atrás que los
      periodos abiertos, ver `periodos_abiertos`). Solo esto (o el SI) apaga el caso.
    · ya_diligenciado: SOLO cuando la exclusión ya está en «SI» (caso cerrado). Un «NO»
      no cierra nada: el correo del Centro Operativo que llega después es justo lo que
      puede convertirlo en SI (caso del 2026-09-02) — se deja
      accionable y se avisa con `decision_previa`."""
    from app.api.ans import _es_anulado
    anulado = _es_anulado(reg.estado_actual) or (reg.se_ejecuto or "").strip().upper() == "NO"
    campo = "excluible_anulado" if anulado else "excluible"
    decision = ((reg.excluible_anulado if anulado else reg.excluible) or "").strip().upper()
    return {"reg_id": reg.id, "periodo": reg.periodo, "zona": reg.zona, "fecha": reg.fecha_fin,
            "tipo": "3.2B" if anulado else "3.2A", "campo": campo,
            "otro_periodo": (reg.periodo or "") != competente,
            "periodo_cerrado": (reg.periodo or "") not in periodos_abiertos(competente),
            "ya_diligenciado": decision == "SI",
            "decision_previa": decision or None}


async def _cazar_pdls(db: AsyncSession, asunto: str, cuerpo: str,
                      fecha_correo: Optional[str] = None) -> list[dict]:
    """Los códigos del correo cruzados contra el ANS, cada uno con su FUENTE (asunto >
    cuerpo reciente > cadena de reenvíos) y las señales de _flags_match — el tab muestra
    esas señales para calibrar la confianza del match. Un match por código, exista o no.
    Si el PDL no está en la tabla y el correo es de un mes POSTERIOR al periodo competente,
    queda marcado `espera_stm` (la STM de ese mes aún no se carga): no es un correo
    perdido, se re-cruza solo cuando entre la STM."""
    m0 = RE_CORTE_CADENA.search(cuerpo or "")
    reciente = (cuerpo or "")[:m0.start()] if m0 else (cuerpo or "")
    cadena = (cuerpo or "")[m0.start():] if m0 else ""
    vistos, out = {}, []
    for fuente, texto in (("asunto", asunto or ""), ("cuerpo", reciente), ("cadena", cadena)):
        for m in RE_PDL.finditer(texto):
            c = m.group(0).upper()
            if c not in vistos:
                vistos[c] = fuente
    competente = await _periodo_competente(db)
    mes_correo = _mes_de_fecha(fecha_correo)
    for c, fuente in vistos.items():
        # Si el PDL existe en varios periodos gana el más reciente (el competente primero)
        r = await db.execute(select(AnsRegistro).where(
            AnsRegistro.pdl.ilike(f"%{c}%"), AnsRegistro.entorno == "real")
            .order_by(AnsRegistro.periodo.desc(), AnsRegistro.id.desc()))
        reg = r.scalars().first()
        if reg:
            out.append({"pdl": c, "fuente": fuente, **_flags_match(reg, competente)})
        else:
            d = {"pdl": c, "reg_id": None, "fuente": fuente}
            if mes_correo and mes_correo > competente:
                d["espera_stm"] = mes_correo
            out.append(d)
    return out


def _accionable(m: dict) -> bool:
    """Match sobre el que se trabaja: caso de un periodo ABIERTO (competente o el anterior)
    y no cerrado en SI. «Otro periodo» ya no apaga el caso por sí solo (2026-09-08): un NO
    de agosto con correo nuevo en septiembre es justo lo que hay que analizar.
    Desde el 2026-10-02 el SI tampoco apaga el caso cuando el correo lo CONTRADICE (Enel negó
    la exclusión), cuando llegó DESPUÉS de la decisión, o cuando el SI no tiene soporte adjunto
    y este correo puede serlo (`flags_vivos` deja el veredicto en `accionable`)."""
    if "accionable" in m:
        return bool(m["accionable"])
    return bool(m.get("reg_id")) and not m.get("periodo_cerrado") and not m.get("ya_diligenciado")


def meta_de(c: CorreoAns) -> dict:
    """La historia del correo en la bandeja (ver CorreoAns.meta)."""
    try:
        d = json.loads(c.meta) if c.meta else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def _fecha_local(fecha_correo: Optional[str]) -> Optional[datetime]:
    """La fecha del correo como datetime LOCAL de Bogotá. OJO: el texto dice «+00:00», pero
    Outlook por COM (pywin32) entrega la hora local con esa etiqueta — verificado 2026-10-02:
    los 41 correos de la bandeja entraron ≥ 5 h «después» de su fecha, ninguno antes."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?", fecha_correo or "")
    return datetime(*(int(x or 0) for x in m.groups())) if m else None


async def flags_vivos(db: AsyncSession, matches: list, *, estado: str, fecha_correo: Optional[str],
                      remitente: Optional[str], cuerpo: Optional[str], meta: Optional[dict],
                      competente: str, cache: Optional[dict] = None) -> list:
    """Las señales del cruce, EN VIVO y completas (una sola regla para ingresar, listar, abrir y
    re-cruzar). Sobre `_flags_match` añade lo que faltó el 2026-10-01:

    · `respuesta_enel`: ACEPTA / NIEGA si el mensaje más reciente es de Enel y responde la
      solicitud (determinista, services.correo.respuesta_enel).
    · `contradice`: la celda está en SI y Enel NEGÓ la exclusión → hay que reconsiderar.
    · `novedad`: el correo llegó DESPUÉS de la decisión en SI y nadie lo ha revisado.
    · `si_sin_soporte`: la celda está en SI pero no tiene ningún soporte adjunto: este correo
      puede serlo (SI puesto a mano con el correo descartado).
    · `favorece`: la celda está en NO y Enel ACEPTÓ la exclusión.
    · `accionable`: se trabaja sobre el caso (ver `_accionable`).
    · sin caso: `espera_stm` también cuando el correo es del periodo abierto — la STM del mes
      se carga por tandas, así que «no está en la tabla» casi siempre es «todavía no»."""
    from datetime import timedelta
    from app.models.models import AnsCambio, AnsEvidencia
    from app.services.correo import respuesta_enel
    cache = cache if cache is not None else {}
    resp = respuesta_enel(remitente, cuerpo)
    fl = _fecha_local(fecha_correo)
    mes_correo = _mes_de_fecha(fecha_correo)
    revisado = (meta or {}).get("revisado") or {}
    for m in matches:
        for k in ("espera_stm", "stm_parcial"):
            m.pop(k, None)
        if not m.get("reg_id"):
            if mes_correo and mes_correo > competente:
                m["espera_stm"] = mes_correo
            elif mes_correo == competente:
                # correo del mes de trabajo: su STM se carga por tandas → «todavía no», no «sin caso»
                m["espera_stm"], m["stm_parcial"] = competente, True
            continue
        reg = await db.get(AnsRegistro, m["reg_id"])
        if not reg:
            continue
        m.update(_flags_match(reg, competente))
        campo = m["campo"]
        clave = (reg.id, campo)
        if clave not in cache:
            dec = (await db.execute(select(AnsCambio.creado_en).where(
                AnsCambio.registro_id == reg.id, AnsCambio.campo == campo)
                .order_by(AnsCambio.id.desc()).limit(1))).scalar_one_or_none()
            n_ev = len((await db.execute(select(AnsEvidencia.id).where(
                AnsEvidencia.reg_id == reg.id, AnsEvidencia.campo == campo))).all())
            cache[clave] = ((dec - timedelta(hours=5)) if dec else None, n_ev)   # ans_cambios va en UTC
        dec_local, n_ev = cache[clave]
        si = m["ya_diligenciado"]
        posterior = bool(fl and dec_local and fl > dec_local)
        rev = revisado.get(str(reg.id)) or {}
        ya_rev = rev.get("decision") == m["decision_previa"]
        niega = bool(resp and resp["postura"] == "NIEGA")
        m["respuesta_enel"] = resp["postura"] if resp else None
        m["decidido_en"] = dec_local.isoformat(timespec="minutes") if dec_local else None
        m["posterior_a_decision"] = posterior
        m["contradice"] = bool(si and niega and not ya_rev and (posterior or dec_local is None))
        m["novedad"] = bool(si and estado == "nuevo" and posterior and not ya_rev and not m["contradice"])
        m["si_sin_soporte"] = bool(si and estado == "nuevo" and n_ev == 0 and not ya_rev)
        m["favorece"] = bool(m["decision_previa"] == "NO" and resp and resp["postura"] == "ACEPTA")
        m["revisado"] = bool(ya_rev)
        m["accionable"] = bool(not m["periodo_cerrado"]
                               and (not si or m["contradice"] or m["novedad"] or m["si_sin_soporte"]))
    return matches


async def avisar(texto: str, titulo: str = "SG07 - Correos ANS") -> None:
    """Aviso al administrador. Método preferido: el guion de notificación configurado en SGO7_NOTIFY_CMD; si no
    existe, el canal push de settings.NTFY_URL. Sin ninguno de los dos no hace nada. Texto corto, una línea, sin
    datos de clientes. Nunca lanza."""
    import asyncio
    import logging
    import subprocess
    texto = re.sub(r"\s+", " ", texto).strip()
    try:
        notify = os.getenv("SGO7_NOTIFY_CMD", "")
        if notify and os.path.exists(notify):
            await asyncio.to_thread(subprocess.run, [notify, texto], timeout=60, check=False)
            return
        if not settings.NTFY_URL:
            return
        import requests
        await asyncio.to_thread(requests.post, settings.NTFY_URL, data=texto.encode("utf-8"),
                                headers={"Title": titulo, "Priority": "high"}, timeout=10)
    except Exception as e:                                   # noqa: BLE001 — el aviso nunca rompe
        logging.getLogger("correo_bot").warning("aviso falló: %s", e)


async def avisar_contradicciones(db: AsyncSession, c: CorreoAns, matches: list) -> int:
    """UNA vez por correo+caso: Enel negó la exclusión de un caso que está en SI. Devuelve
    cuántos avisos mandó (deja la marca en meta.avisado; el llamador hace commit)."""
    meta = meta_de(c)
    ya = set(meta.get("avisado") or [])
    n = 0
    for m in matches:
        clave = f"contradice:{m.get('reg_id')}"
        if not m.get("contradice") or clave in ya:
            continue
        await avisar(f"Sabogal ANS: Enel NEGO la exclusion del {m['pdl']} y la celda esta en SI. "
                     f"Hay que reconsiderarla: abre el correo en el tab Correos ANS.")
        ya.add(clave)
        n += 1
    if n:
        meta["avisado"] = sorted(ya)
        c.meta = json.dumps(meta, ensure_ascii=False)
    return n


def _propone_excluir(a: dict) -> bool:
    """¿La lectura del correo inclina el caso a SI? (decisión directa o veredicto en Revisión)."""
    return (a.get("relacionado") == "SI"
            and (a.get("excluible") == "SI" or a.get("veredicto_ia") == "SI"))


async def _avisar_si_salva_caso(reg: AnsRegistro, campo: str, a: dict) -> None:
    """Aviso push cuando la lectura automática propone EXCLUIR un caso que hoy está en NO o
    sin decidir: es la información que puede salvar el caso y la persona responsable debe verla y aprobarla,
    no descubrirla semanas después (pedido 2026-09-08). Sin datos sensibles (solo PDL y
    periodo). Nunca lanza."""
    if not settings.AVISO_CORREO_SALVA_CASO or not _propone_excluir(a):
        return
    decision = (getattr(reg, campo, None) or "").strip().upper()
    if decision == "SI":
        return
    estado = "hoy en NO" if decision == "NO" else "sin decidir"
    await avisar(f"Sabogal ANS: llego un correo que propone EXCLUIR el {reg.pdl} "
                 f"({reg.periodo}, {estado}). Abrelo en el tab Correos ANS y aprueba o corrige.",
                 titulo="SG07 - correo que puede salvar un caso")


async def _auto_analisis(correo_id: int, objetivos: list[tuple[int, str]]):
    """Análisis IA automático tras la ingesta (en segundo plano, sesión propia): los matches
    ACCIONABLES (periodo abierto, sin SI) — al abrir el correo en el tab, la lectura ya está
    esperando con sus botones de aprobar; si propone excluir un caso en NO, avisa.
    SIN tope de casos (2026-10-08): había un `[:2]` por costo y un correo que justifica 6
    anulaciones («PDL CANCELADOS EN EL MES DE SEPTIEMBRE») solo leía dos — las otras
    cuatro quedaban sin lectura y parecía que «la máquina solo toma un correo por caso».
    Las lecturas van una tras otra (no en paralelo) para no saturar al proveedor de IA."""
    import logging
    from app.db.database import AsyncSessionLocal
    from app.api.correo_auditoria import analizar_y_guardar
    log = logging.getLogger("correo_bot")
    async with AsyncSessionLocal() as db:
        c = await db.get(CorreoAns, correo_id)
        if not c:
            return
        for reg_id, campo in objetivos:
            reg = await db.get(AnsRegistro, reg_id)
            if not reg:
                continue
            try:
                a = await analizar_y_guardar(db, c, reg, campo)
                log.info("auto-análisis correo %s vs reg %s: excluible=%s",
                         correo_id, reg_id, a.get("excluible"))
                await _avisar_si_salva_caso(reg, campo, a)
            except Exception as e:                # noqa: BLE001 — el botón manual queda de respaldo
                log.warning("auto-análisis correo %s vs reg %s falló: %s", correo_id, reg_id, e)
                # El slot «pendiente» (si lo dejó el detalle) pasa a error visible, no a limbo
                from app.api.correo_auditoria import _analisis_mapa
                await db.refresh(c)
                prev = _analisis_mapa(c)
                if (prev.get(str(reg_id)) or {}).get("pendiente"):
                    prev[str(reg_id)] = {"reg_id": reg_id, "campo": campo, "pdl": reg.pdl,
                                         "error": f"La lectura IA falló: {e}"[:300]}
                    c.analisis = json.dumps(prev, ensure_ascii=False)
                    await db.commit()


@router.post("/inbox", dependencies=[Depends(require_bot)])
async def inbox(background: BackgroundTasks,
                entry_id: str = Form(...), asunto: Optional[str] = Form(None),
                remitente: Optional[str] = Form(None), fecha: Optional[str] = Form(None),
                categorias: Optional[str] = Form(None),
                msg: UploadFile = File(...),
                db: AsyncSession = Depends(get_db)):
    """Un correo etiquetado ANS. Idempotente por EntryID de Outlook — y como el EntryID
    CAMBIA si el correo se mueve de carpeta (nos duplicó el MT16), segundo nivel de
    dedupe por asunto+fecha: si ya existe, se adopta el EntryID nuevo y no se duplica."""
    ya = await db.execute(select(CorreoAns).where(CorreoAns.entry_id == entry_id))
    if ya.scalar_one_or_none():
        return {"ok": True, "duplicado": True}
    if (asunto or "").strip() and (fecha or "").strip():
        r2 = await db.execute(select(CorreoAns).where(
            CorreoAns.asunto == (asunto or "")[:400],
            CorreoAns.fecha_correo == (fecha or "")[:60]))
        previo = r2.scalars().first()
        if previo:
            previo.entry_id = entry_id          # el correo se movió de carpeta: adoptar el id nuevo
            await db.commit()
            return {"ok": True, "duplicado": True}

    datos = await msg.read()
    mes = datetime.now().strftime("%Y-%m")
    carpeta = os.path.join(CORREOS_DIR, mes)
    os.makedirs(carpeta, exist_ok=True)
    seguro = re.sub(r"[^\w.\-]", "_", (asunto or "correo"))[:80]
    ruta = os.path.join(carpeta, f"{int(datetime.now().timestamp())}_{seguro}.msg")
    with open(ruta, "wb") as f:
        f.write(datos)

    cuerpo = None
    try:
        from app.services.correo import parse_msg
        p = parse_msg(ruta)
        cuerpo = p.get("cuerpo")
        asunto = asunto or p.get("asunto")
        remitente = remitente or p.get("de")
    except Exception as e:                       # noqa: BLE001 — un .msg raro no tumba la ingesta
        cuerpo = f"(no se pudo parsear el .msg: {e})"

    matches = await _cazar_pdls(db, asunto or "", cuerpo or "", fecha)
    # Señales completas en vivo: respuesta de Enel, contradicción con una decisión en SI, SI sin
    # soporte… (2026-10-02). De aquí sale qué se analiza y qué se avisa.
    matches = await flags_vivos(db, matches, estado="nuevo", fecha_correo=fecha, remitente=remitente,
                                cuerpo=cuerpo, meta=None, competente=await _periodo_competente(db))

    c = CorreoAns(entry_id=entry_id, asunto=(asunto or "")[:400] or None,
                  remitente=(remitente or "")[:200] or None,
                  fecha_correo=(fecha or "")[:60] or None,
                  categorias=(categorias or "")[:200] or None,
                  msg_ruta=ruta, cuerpo=cuerpo,
                  pdls=json.dumps(matches, ensure_ascii=False))
    db.add(c)
    await db.commit()
    # Enel negó la exclusión de un caso que está en SI: aviso inmediato (una vez)
    if await avisar_contradicciones(db, c, matches):
        await db.commit()

    # Análisis IA automático de los matches PENDIENTES del periodo (al grano: al
    # abrir el correo la lectura ya está). Los de otro periodo o ya diligenciados no
    # gastan IA — son contexto, no trabajo.
    objetivos = [(m["reg_id"], m["campo"]) for m in matches if _accionable(m)]
    if objetivos:
        background.add_task(_auto_analisis, c.id, objetivos)

    return {"ok": True, "id": c.id,
            "pdls": [m["pdl"] for m in matches],
            "casos": [m["pdl"] for m in matches if m.get("reg_id")]}


@router.post("/latido", dependencies=[Depends(require_bot)])
async def latido():
    """El agente reporta que hizo una pasada (aunque no subiera nada): el tab muestra
    «última pasada hace X min» real, en vez de inferirlo mal de los correos subidos."""
    os.makedirs(CORREOS_DIR, exist_ok=True)
    with open(os.path.join(CORREOS_DIR, "latido.txt"), "w") as f:
        f.write(datetime.utcnow().isoformat() + "Z")
    return {"ok": True}
