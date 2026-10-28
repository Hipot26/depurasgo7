"""MATCHER determinista media ↔ caso (paso 1 de la orquestación del ANS, 2026-09-07).

Antes, qué videos entraban al análisis lo decidía «los del día en los grupos de la zona» (con un
tope de 8) y la IA los atribuía leyendo el letrero. Eso dejaba huecos: video subido a otro grupo,
más de 8 videos ese día, letrero ilegible. Ahora el amarre lo hace código, con lo que ya extrajo
el índice (`wa_media_meta`) y con el mensaje mismo, y queda en `wa_media_caso` con un puntaje:

  100 codigo          el código del letrero (R##### o DF…) es el de la orden, en su fecha
   90 caption         el caption/texto del mensaje trae el número del PDL, en su fecha
   75 lider           el #líder del letrero o el nombre del líder en el caption, dentro de la
                      ventana horaria del caso
   75 autor           quien subió el video es quien ese día publicó un texto con el número del
                      PDL (la plantilla oficial): la cuadrilla del caso (auditoría 2026-09-22)
   60 codigo_sin_fecha código coincide pero la fecha no cuadra (o no se pudo leer)
   40 ventana_zona    solo por hora y zona del grupo (candidato; la IA confirma)

El analista usa los amarres ≥ 75 como material OBLIGATORIO del caso (van primero, no cuentan
para el tope) y los de 40 como candidatos si cabe.

REGLA NEGATIVA (auditoría 2026-09-22, un caso con videos de otra orden): si el letrero trae el
código de OTRA orden, el video NO se amarra a este caso por ningún criterio — ni por líder, ni
por lugar, ni por ventana. El código del letrero lo lee `letrero.py` sobre el OCR crudo, así
que también cubre los videos cuyo `overlay_codigo` quedó vacío por el parser viejo. «Ver justificación» los muestra.
Se recalcula cada noche (scripts/amarrar_media.py) y antes de cada análisis de un caso.
"""
import difflib
import logging
import re
import unicodedata
from datetime import datetime, timedelta

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AnsRegistro, WaMensaje, WaGrupo, WaMediaMeta, WaMediaCaso
from app.services import letrero

log = logging.getLogger("matcher")
PUNTAJE = {"codigo": 100, "caption": 90, "lider": 75, "autor": 75, "codigo_sin_fecha": 60, "lugar": 55, "ventana_zona": 40}
_LUGAR_GENERICO = {"NODO", "VEREDA", "MUNICIPIO", "CUNDINAMARCA", "ZONA", "CAMBIO", "APOYO", "UNNAMED", "ROAD",
                   "SAGRADO", "CORAZON", "CIRCUITO", "SECTOR", "ALTO", "BAJO", "SANTA", "SAN"}
UTC_OFFSET = timedelta(hours=5)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn").upper()


def _tokens_nombre(s: str) -> set:
    """Palabras de 4+ letras de un nombre, sin la cédula «(CO…)»."""
    s = re.sub(r"\(.*?\)", " ", _norm(s))
    return {t for t in re.findall(r"[A-Z]{4,}", s) if t not in {"JUAN", "JOSE", "LUIS", "MARIA"}}


def _rnum(s: str) -> str | None:
    m = re.search(r"R\s?(\d{4,6})", _norm(s))
    return f"R{m.group(1)}" if m else None


def _fecha(ddmmyyyy: str):
    try:
        return datetime.strptime((ddmmyyyy or "").strip()[:10], "%d/%m/%Y")
    except ValueError:
        return None


def _dt(fecha: str, hora: str):
    try:
        return datetime.strptime(f"{(fecha or '').strip()[:10]} {(hora or '').strip()[:5]}", "%d/%m/%Y %H:%M")
    except ValueError:
        return None


def _dt_maniobra(s: str):
    try:
        return datetime.strptime((s or "").strip()[:16], "%d/%m/%Y %H:%M")
    except ValueError:
        return None


def ventana_caso(reg: AnsRegistro) -> tuple[datetime | None, datetime | None]:
    """[inicio - 2 h, fin + 3 h] en hora local, a partir de lo planeado y las maniobras reales."""
    puntos = [p for p in (_dt(reg.fecha_inicio, reg.hora_inicio), _dt(reg.fecha_fin, reg.hora_fin),
                          _dt_maniobra(reg.maniobra_inicio), _dt_maniobra(reg.maniobra_fin)) if p]
    if not puntos:
        d = _fecha(reg.fecha_inicio) or _fecha(reg.fecha_fin)
        if not d:
            return None, None
        return d + timedelta(hours=5), d + timedelta(hours=23)
    return min(puntos) - timedelta(hours=2), max(puntos) + timedelta(hours=3)


def zonas_de_grupo(nombre: str) -> set:
    """Zonas del ANS a las que sirve un grupo (por su nombre). «*» = todas (Cumplimentación)."""
    from app.services.wa_analista import GRUPOS_ZONA
    n = _norm(nombre or "").replace("É", "E")
    if "CUMPLIMENTACI" in n:
        return {"*"}
    out = set()
    for zona, patrones in GRUPOS_ZONA.items():
        if any(p.upper() in n for p in patrones if p != "cumplimentaci"):
            out.add(zona)
    return out


def _codigos_del_caso(reg: AnsRegistro) -> tuple[str | None, set]:
    """(R##### del PDL, {dígitos de códigos DF/otros mencionados en la orden})."""
    r = _rnum(reg.pdl or reg.protocolo or "")
    texto = " ".join(x or "" for x in (reg.asociado_pdl, reg.descripcion, reg.ri_jt, reg.protocolo))
    dfs = set(re.findall(r"\b(2026\d{8})\b", texto))
    return r, dfs


def _hora_local_media(m: WaMensaje, meta: WaMediaMeta | None) -> datetime | None:
    if meta and meta.overlay_fecha and meta.overlay_hora:
        try:
            return datetime.strptime(f"{meta.overlay_fecha} {meta.overlay_hora[:5]}", "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return (m.enviado_en - UTC_OFFSET) if m.enviado_en else None


def codigo_letrero(meta: WaMediaMeta | None) -> str | None:
    """El código estampado en el video: el que dejó el índice o, si quedó vacío, el que se
    lee ahora del OCR crudo con el lector tolerante (misma lógica para todos)."""
    if not meta:
        return None
    if meta.overlay_codigo:
        return _norm(meta.overlay_codigo)
    return letrero.codigo_principal(meta.overlay_txt or "")


def codigo_ajeno(reg: AnsRegistro, meta: WaMediaMeta | None) -> str | None:
    """Si el letrero es de OTRA orden, devuelve ese código; si es de esta o no hay, None."""
    rnum, dfs = _codigos_del_caso(reg)
    cod = codigo_letrero(meta)
    return cod if letrero.clasificar(cod, rnum, dfs) == "ajeno" else None


def evaluar(reg: AnsRegistro, m: WaMensaje, meta: WaMediaMeta | None, zonas_grupo: set,
            autores: set | None = None) -> tuple[str, str] | None:
    """El mejor criterio con que este mensaje se amarra a este caso, o None.
    `autores` = JIDs que ese día publicaron un texto con el número del PDL (la cuadrilla)."""
    ini, fin = ventana_caso(reg)
    t = _hora_local_media(m, meta)
    en_ventana = bool(ini and fin and t and ini <= t <= fin)
    fecha_cerca = bool(ini and fin and t and (ini - timedelta(days=1)) <= t <= (fin + timedelta(days=1)))
    rnum, dfs = _codigos_del_caso(reg)
    codigo = codigo_letrero(meta)
    caption = _norm(m.texto or "")

    # 0. Letrero de OTRA orden → este video no es de este caso, punto.
    if letrero.clasificar(codigo, rnum, dfs) == "ajeno":
        return None

    # 1. Código del letrero
    if codigo and ((rnum and codigo == rnum) or (codigo.startswith("DF") and codigo[2:] in dfs)):
        if fecha_cerca or not (ini and fin):
            return "codigo", f"letrero {codigo} = orden"
        return "codigo_sin_fecha", f"letrero {codigo} = orden, pero fuera de la fecha del caso"
    # 2. Caption con el PDL
    if rnum and (rnum in caption.replace(" ", "") or (reg.pdl or "").upper() in caption):
        if fecha_cerca or not (ini and fin):
            return "caption", f"el mensaje cita {rnum}"
        return "codigo_sin_fecha", f"el mensaje cita {rnum}, pero fuera de la fecha del caso"
    # 3. Líder (hashtag del letrero o nombre en el caption) dentro de la ventana
    if en_ventana:
        toks = _tokens_nombre(reg.ri_jt or "")
        if meta and meta.overlay_lider:
            ol = _norm(meta.overlay_lider)
            if ol in toks or any(difflib.SequenceMatcher(None, ol, tk).ratio() >= 0.8 for tk in toks):
                return "lider", f"#{meta.overlay_lider} = líder de la orden, en la ventana horaria"
        cap_toks = set(re.findall(r"[A-Z]{4,}", caption))
        if len(toks & cap_toks) >= 2:
            return "lider", f"el mensaje nombra al líder ({' '.join(sorted(toks & cap_toks))}), en la ventana horaria"
    # 3b. Autor: quien subió el video publicó ese día un texto con el número del PDL
    if en_ventana and autores and m.remitente_jid and m.remitente_jid in autores:
        return "autor", "quien subió el video publicó ese día la plantilla/texto del PDL, en la ventana horaria"
    # 4. Lugar del letrero presente en la descripción de la orden (vereda, municipio, circuito)
    if en_ventana and meta and meta.overlay_lugar:
        lug = {t for t in re.findall(r"[A-Z]{5,}", _norm(meta.overlay_lugar)) if t not in _LUGAR_GENERICO}
        desc = set(re.findall(r"[A-Z]{5,}", _norm(" ".join(x or "" for x in (reg.descripcion, reg.asociado_pdl)))))
        comun = lug & desc
        if comun:
            return "lugar", f"el lugar del letrero ({' '.join(sorted(comun))}) está en la descripción de la orden"
    # 5. Solo ventana + zona
    if en_ventana and ("*" in zonas_grupo or (reg.zona or "").upper() in zonas_grupo):
        return "ventana_zona", f"grupo de la zona {reg.zona or '?'} dentro de la ventana {ini:%d/%m %H:%M}–{fin:%H:%M}"
    return None


async def amarrar(db: AsyncSession, periodo: str | None = None, reg_ids: list | None = None) -> dict:
    """Recalcula los amarres de los casos indicados (o de un periodo). Borra y reescribe los
    de esos casos: idempotente. Devuelve conteos por criterio."""
    q = select(AnsRegistro).where(AnsRegistro.entorno == "real")
    if reg_ids:
        q = q.where(AnsRegistro.id.in_(reg_ids))
    elif periodo:
        q = q.where(AnsRegistro.periodo == periodo)
    regs = (await db.execute(q)).scalars().all()
    if not regs:
        return {"casos": 0, "amarres": 0}

    # Ventana global de fechas → solo la media de esos días (± 2 días)
    lo, hi = None, None
    for r in regs:
        i, f = ventana_caso(r)
        if i and f:
            lo = i if lo is None or i < lo else lo
            hi = f if hi is None or f > hi else hi
    if lo is None:
        return {"casos": len(regs), "amarres": 0, "nota": "casos sin fechas"}
    rm = await db.execute(select(WaMensaje).where(
        WaMensaje.media_ruta.isnot(None),
        WaMensaje.enviado_en >= lo - timedelta(days=2) + UTC_OFFSET,
        WaMensaje.enviado_en <= hi + timedelta(days=2) + UTC_OFFSET))
    medias = rm.scalars().all()
    metas = {x.msg_id: x for x in (await db.execute(select(WaMediaMeta).where(
        WaMediaMeta.msg_id.in_([m.id for m in medias]) if medias else False))).scalars().all()}
    grupos = {g.jid: zonas_de_grupo(g.nombre) for g in (await db.execute(select(WaGrupo))).scalars().all()}

    # Autores: por caso, los JIDs que en la ventana (±1 día) escribieron un texto con su R#####
    rt = await db.execute(select(WaMensaje.remitente_jid, WaMensaje.texto, WaMensaje.enviado_en).where(
        WaMensaje.tipo == "texto", WaMensaje.remitente_jid.isnot(None),
        WaMensaje.enviado_en >= lo - timedelta(days=1) + UTC_OFFSET,
        WaMensaje.enviado_en <= hi + timedelta(days=1) + UTC_OFFSET))
    textos = [(j, _norm(t or "").replace(" ", ""), e - UTC_OFFSET) for j, t, e in rt.all() if t]
    autores_por_reg = {}
    for reg in regs:
        rnum, _ = _codigos_del_caso(reg)
        ini, fin = ventana_caso(reg)
        if not (rnum and ini and fin):
            continue
        autores_por_reg[reg.id] = {j for j, t, e in textos
                                   if rnum in t and (ini - timedelta(days=1)) <= e <= (fin + timedelta(days=1))}

    await db.execute(delete(WaMediaCaso).where(WaMediaCaso.reg_id.in_([r.id for r in regs])))
    conteo, n = {}, 0
    for reg in regs:
        for m in medias:
            zg = grupos.get(m.chat_jid, set()) if m.es_grupo else {"*"}   # un DM es de quien sea: solo por código/caption/líder
            res = evaluar(reg, m, metas.get(m.id), zg if m.es_grupo else set(),
                          autores=autores_por_reg.get(reg.id))
            if not res:
                continue
            criterio, detalle = res
            db.add(WaMediaCaso(msg_id=m.id, reg_id=reg.id, criterio=criterio,
                               puntaje=PUNTAJE[criterio], detalle=detalle[:300]))
            conteo[criterio] = conteo.get(criterio, 0) + 1
            n += 1
    await db.commit()
    log.info("amarres: %d casos · %d amarres · %s", len(regs), n, conteo)
    return {"casos": len(regs), "amarres": n, **conteo}


async def amarres_de(db: AsyncSession, reg_id: int, minimo: int = 40) -> dict:
    """{msg_id: WaMediaCaso} del caso con puntaje ≥ mínimo."""
    r = await db.execute(select(WaMediaCaso).where(WaMediaCaso.reg_id == reg_id, WaMediaCaso.puntaje >= minimo)
                         .order_by(WaMediaCaso.puntaje.desc()))
    return {a.msg_id: a for a in r.scalars().all()}
