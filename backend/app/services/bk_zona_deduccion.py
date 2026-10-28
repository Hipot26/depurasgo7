"""Deducción de la ZONA de una orden de Programados (FT) cuando el nombre del supervisor no calza con
el directorio. Requisito 2026-09-29, tras ver que el 3.2 B le pedía al apoyo de una zona
el correo de órdenes de otra.

Por qué hace falta: el CD10 no trae nada geográfico (la unidad operativa es la misma en todas las
órdenes) y el supervisor llega escrito de mil formas («NOMBRE APELLIDO» = «NOMBRE SEGUNDO APELLIDO»,
solo los nombres de pila, un celular en la columna del nombre…). Sin zona, el reporte manda la orden a UBATE y el 3.2 B
no sabe a qué apoyo preguntarle.

Qué hace: votan señales independientes, todas con datos que YA están cargados, y la zona solo se pone
si hay CONSENSO — al menos 2 votos por la misma zona y NINGUNO por otra:

  · palabras  el nombre del supervisor contiene (o está contenido en) el de un supervisor o apoyo del
              directorio o de la semilla, con ≥2 palabras, y todos los que calzan son de UNA zona.
  · celular   el celular del supervisor que trae el CD10 (columna AX, o la del nombre cuando el
              digitador escribió el número ahí) está en el directorio o en órdenes con zona, de UNA zona.
  · lider     el líder (RI) tiene ≥3 órdenes con zona y ≥80 % en una sola.
  · lcl       el LCL de la descripción está en la Programación diaria de ENEL (la de los 20 minutos)
              o en 3.Programados con UNA zona.
  · pdl       el PDL aparece en esas mismas programaciones con UNA zona.

Prueba retrospectiva sobre 460 órdenes FT con zona (2026-09-29), SIN usar el nombre exacto: el consenso
acierta 182 de 185 (98,4 %). Cada señal sola: líder 99 %, celular 95 %, palabras 94 %, LCL 85 %. Se
descartaron el municipio en la descripción (52 %) y la unidad operativa (constante).

Con un solo voto, o con votos cruzados, la orden se queda SIN zona y va al Paso 1 (preguntar a
programación): mejor preguntar que molestar al apoyo equivocado. Una zona puesta a mano manda sobre
cualquier deducción. Cómo se llegó a cada zona queda en `datos_extra._zona_origen` y lo explica la
celda Zona del grid (`explicar`).
"""
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ans_ctx import current_entorno
from app.models.models import AnsCambio, AnsProg20, AnsRegistro, Contacto, ProgRegistro

ZONAS = ("UBATE", "ALMEIDAS", "GUAVIO", "LV")
MIN_VOTOS = 2
LIDER_MIN, LIDER_PUREZA = 3, 0.8
LCL_GENERICO = "6301000000"            # la programación lo usa como comodín: no identifica nada
_PARTICULAS = {"DE", "DEL", "LA", "LAS", "LOS", "SAN", "SANTA", "DA", "DI", "Y"}

SEÑALES = {                             # etiqueta legible de cada voto (grid y botón del dev)
    "palabras": "nombre del supervisor parecido",
    "celular": "celular del supervisor",
    "lider": "historial del líder",
    "lcl": "LCL en la programación",
    "pdl": "PDL en la programación",
}


def _norm(s) -> str:
    s = re.sub(r"\(.*?\)", " ", str(s or ""))                      # fuera «(CO123…)»
    s = unicodedata.normalize("NFKD", s.upper())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s) -> frozenset:
    return frozenset(t for t in re.sub(r"[^A-Z ]", " ", _norm(s)).split()
                     if t not in _PARTICULAS and len(t) > 1)


def _tel(s) -> Optional[str]:
    """Celular a E.164 sin «+» (como el directorio). None si no parece un teléfono."""
    d = re.sub(r"\D", "", str(s or "")).lstrip("0")
    if len(d) == 10 and d.startswith("3"):
        d = "57" + d
    return d if 10 <= len(d) <= 15 else None


def _es_telefono(s) -> bool:
    return bool(s) and not re.search(r"[A-Za-z]", str(s)) and _tel(s) is not None


def _lcls(descripcion) -> set:
    return {m for m in re.findall(r"LCL[\s:.]*(\d{8,})", _norm(descripcion)) if m != LCL_GENERICO}


def _rnums(s) -> set:
    return set(re.findall(r"R\d{5}", _norm(s)))


def _unica(zonas) -> Optional[str]:
    zs = {z for z in zonas if z}
    return next(iter(zs)) if len(zs) == 1 else None


def extra_de(reg) -> dict:
    """datos_extra de un registro como dict (tolera vacío o JSON roto)."""
    try:
        d = json.loads(getattr(reg, "datos_extra", None) or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


class Deductor:
    """Índices cargados UNA vez (import o re-resolución) y consultados por orden."""

    def __init__(self):
        self.sup_tokens: list[tuple[str, frozenset, str]] = []   # (nombre, palabras, zona)
        self.tel_dir: dict[str, set] = defaultdict(set)          # celular → zonas (directorio)
        self.tel_hist: dict[str, Counter] = defaultdict(Counter)  # celular → zonas (órdenes)
        self.lider_hist: dict[str, Counter] = defaultdict(Counter)
        self.lcl_zona: dict[str, set] = defaultdict(set)
        self.pdl_zona: dict[str, set] = defaultdict(set)

    @classmethod
    async def cargar(cls, db: AsyncSession) -> "Deductor":
        from app.api.ans import SUPERVISOR_ZONA          # import tardío: ans.py importa este módulo
        d = cls()
        ent = current_entorno()

        nombres = {_norm(k): v for k, v in SUPERVISOR_ZONA.items() if v}
        for c in (await db.execute(select(Contacto).where(Contacto.activo.is_(True)))).scalars().all():
            z = (c.zona or "").strip().upper()
            if not z or z == "LV":      # aquí solo se deducen FT, y a un FT nunca se le saca LV
                continue                # del directorio (regla del ANS; ver _zona_map en ans.py)
            if c.rol in ("supervisor", "apoyo"):              # los líderes no: un nombre de líder largo
                nombres[_norm(c.nombre)] = z                  # se tragaba al de un supervisor
            t = _tel(c.telefono)
            if t:
                d.tel_dir[t].add(z)
        d.sup_tokens = [(n, _tokens(n), z) for n, z in nombres.items() if len(_tokens(n)) >= 2]

        # Historial: solo zonas que NO salieron de este mismo consenso (no se retroalimenta).
        filas = (await db.execute(select(AnsRegistro.ri_jt, AnsRegistro.zona, AnsRegistro.datos_extra).where(
            AnsRegistro.entorno == ent, AnsRegistro.tipo == "FT", AnsRegistro.zona.in_(ZONAS)))).all()
        for ri_jt, zona, extra in filas:
            try:
                ex = json.loads(extra or "{}") or {}
            except ValueError:
                ex = {}
            if not isinstance(ex, dict) or (ex.get("_zona_origen") or {}).get("metodo") == "consenso":
                continue
            if ri_jt:
                d.lider_hist[_norm(ri_jt)][zona] += 1
            t = _tel(ex.get("telefono_responsable_cd10"))
            if t:
                d.tel_hist[t][zona] += 1

        for p in (await db.execute(select(AnsProg20).where(AnsProg20.entorno == ent))).scalars().all():
            z = (p.zona or "").strip().upper()
            if z in ZONAS:
                if p.lcl and p.lcl.strip() != LCL_GENERICO:
                    d.lcl_zona[p.lcl.strip()].add(z)
                for r in _rnums(p.pdt):
                    d.pdl_zona[r].add(z)
        for p in (await db.execute(select(ProgRegistro).where(ProgRegistro.entorno == ent))).scalars().all():
            z = (p.zona or "").strip().upper()
            if z in ZONAS:
                if p.lcl and p.lcl.strip() != LCL_GENERICO:
                    d.lcl_zona[p.lcl.strip()].add(z)
                for r in _rnums(p.pdl):
                    d.pdl_zona[r].add(z)
        return d

    def votos(self, nombre_responsable, ri_jt, descripcion, pdl, telefono_cd10=None) -> dict:
        """{señal: {"zona": Z, "por": "…"}} — solo las señales que dan UNA zona."""
        v: dict = {}
        # palabras
        tn = _tokens(nombre_responsable)
        if len(tn) >= 2:
            hits = [(n, z) for n, t, z in self.sup_tokens if tn <= t or t <= tn]
            z = _unica(z for _, z in hits)
            if z:
                v["palabras"] = {"zona": z, "por": ", ".join(sorted({n for n, _ in hits})[:3])}
        # celular (AX, o el número escrito en la columna del supervisor)
        tels = {t for t in (_tel(telefono_cd10),
                            _tel(nombre_responsable) if _es_telefono(nombre_responsable) else None) if t}
        if tels:
            zs = set()
            for t in tels:
                zs |= self.tel_dir.get(t, set()) | {z for z, n in self.tel_hist.get(t, {}).items() if n > 0}
            z = _unica(zs)
            if z:
                v["celular"] = {"zona": z, "por": ", ".join("+" + t for t in sorted(tels))}
        # líder
        h = self.lider_hist.get(_norm(ri_jt)) if ri_jt else None
        if h:
            tot = sum(h.values())
            z, n = h.most_common(1)[0]
            if tot >= LIDER_MIN and n / tot >= LIDER_PUREZA:
                v["lider"] = {"zona": z, "por": f"{n} de {tot} órdenes"}
        # LCL
        lcls = _lcls(descripcion)
        if lcls:
            z = _unica(set().union(*[self.lcl_zona.get(x, set()) for x in lcls]))
            if z:
                v["lcl"] = {"zona": z, "por": ", ".join(sorted(lcls))}
        # PDL
        rn = _rnums(pdl)
        if rn:
            z = _unica(set().union(*[self.pdl_zona.get(x, set()) for x in rn]))
            if z:
                v["pdl"] = {"zona": z, "por": ", ".join(sorted(rn))}
        return v

    def deducir(self, nombre_responsable, ri_jt, descripcion, pdl,
                telefono_cd10=None) -> tuple[Optional[str], dict]:
        """(zona o None, origen). El origen va a datos_extra._zona_origen aunque no haya zona:
        así la celda explica por qué quedó vacía."""
        v = self.votos(nombre_responsable, ri_jt, descripcion, pdl, telefono_cd10)
        cuenta = Counter(x["zona"] for x in v.values())
        zona = None
        if len(cuenta) == 1:
            z, n = cuenta.most_common(1)[0]
            if n >= MIN_VOTOS:
                zona = z
        return zona, {"metodo": "consenso" if zona else "sin_consenso", "zona": zona, "votos": v,
                      "fecha": datetime.now().strftime("%Y-%m-%d")}


def _votos_legibles(votos: dict) -> str:
    partes = [f"{SEÑALES.get(k, k)} → {x.get('zona')} ({x.get('por')})" for k, x in (votos or {}).items()]
    return "; ".join(partes) or "ninguna señal"


def explicar(zona: Optional[str], responsable: Optional[str], origen: dict) -> Optional[dict]:
    """Texto de la celda Zona para el popup «por qué este dato». None = usar el texto de siempre."""
    m = (origen or {}).get("metodo")
    resp = responsable or "—"
    if not m:
        return None
    if m == "manual":
        if not zona:
            return None
        return {"origen": "manual", "texto": (
            f"«{zona}» puesta a mano por {origen.get('usuario') or 'una persona'} el {origen.get('fecha') or '—'}. "
            "El directorio la aprendió para los próximos imports.")}
    if zona and origen.get("zona") and origen["zona"] != zona:
        return {"origen": "manual", "texto": f"«{zona}» puesta a mano (la deducción había dado {origen['zona']})."}
    if m == "consenso" and zona:
        return {"origen": "auto", "texto": (
            f"«{zona}» DEDUCIDA: el supervisor «{resp}» no está en el directorio con ese nombre, y "
            f"coincidieron {len(origen.get('votos') or {})} señales — {_votos_legibles(origen.get('votos'))}. "
            "Si no es, corrígela aquí: el directorio lo aprende.")}
    if m == "directorio" and zona:
        return {"origen": "auto", "texto": (
            f"«{zona}» puesta después del import, cuando el directorio ya conocía a «{resp}».")}
    if m == "conservada" and zona:
        return {"origen": "auto", "texto": (
            f"«{zona}» se conservó de antes (corrección a mano o deducción): el último CD10 no permite "
            f"saberla por el supervisor «{resp}».")}
    if m == "sin_consenso" and not zona:
        return {"origen": "manual", "texto": (
            f"SIN ZONA: «{resp}» no está en el directorio y las señales no alcanzan para decidir "
            f"({_votos_legibles(origen.get('votos'))}; hacen falta {MIN_VOTOS} que coincidan y ninguna "
            "en contra). Va al Paso 1: preguntar a programación. Si la sabes, escríbela y el directorio "
            "la aprende.")}
    return None


async def periodos_trabajo(db: AsyncSession) -> list[str]:
    """Los periodos abiertos (el competente y los anteriores que siguen en trabajo)."""
    from app.api.correo_bot import _periodo_competente, periodos_abiertos
    return periodos_abiertos(await _periodo_competente(db))


async def resolver_sin_zona(db: AsyncSession, periodos: list[str], aplicar: bool = False,
                           usuario_id: Optional[int] = None,
                           usuario_nombre: str = "Sistema (zona deducida)") -> dict:
    """Recorre las órdenes FT SIN zona de `periodos` (entorno actual): primero el directorio tal
    como está hoy (quizá ya conoce al supervisor), después el consenso. Con `aplicar` escribe la
    zona, su AnsCambio y el origen; sin él solo dice qué haría."""
    from app.api.ans import _zona_map, _zona_de
    filas = (await db.execute(select(AnsRegistro).where(
        AnsRegistro.entorno == current_entorno(), AnsRegistro.tipo == "FT",
        AnsRegistro.periodo.in_(periodos), func.coalesce(AnsRegistro.zona, "") == "")
        .order_by(AnsRegistro.periodo, AnsRegistro.pdl))).scalars().all()
    out = {"periodos": periodos, "filas": [], "resueltas": 0, "aplicado": aplicar}
    if not filas:
        return out
    zmap = await _zona_map(db)
    ded = await Deductor.cargar(db)
    hoy = datetime.now().strftime("%Y-%m-%d")
    for r in filas:
        extra = extra_de(r)
        z = _zona_de("FT", r.nombre_responsable, zmap)
        if z:
            origen = {"metodo": "directorio", "zona": z, "votos": {}, "fecha": hoy}
        else:
            z, origen = ded.deducir(r.nombre_responsable, r.ri_jt, r.descripcion, r.pdl,
                                    extra.get("telefono_responsable_cd10"))
        out["filas"].append({"reg_id": r.id, "pdl": r.pdl, "periodo": r.periodo,
                             "supervisor": r.nombre_responsable, "lider": r.ri_jt,
                             "se_ejecuto": r.se_ejecuto, "zona": z, "metodo": origen["metodo"],
                             "votos": origen.get("votos") or {}})
        if z:
            out["resueltas"] += 1
        if not aplicar:
            continue
        extra["_zona_origen"] = origen
        r.datos_extra = json.dumps(extra, ensure_ascii=False, default=str)
        if z:
            r.zona = z
            db.add(AnsCambio(registro_id=r.id, periodo=r.periodo, protocolo=r.protocolo, campo="zona",
                             valor_anterior="", valor_nuevo=z, tipo="edicion",
                             usuario_id=usuario_id, usuario_nombre=usuario_nombre))
    if aplicar:
        await db.commit()
    return out
