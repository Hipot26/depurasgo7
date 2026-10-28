"""Parseo y cruce de la plantilla Mantilla (hoja 'Seguimiento Pisos').
Matriz: cada cuadrilla = bloque de 7 actividades; por cada fecha un bloque de
3 columnas (Cantidad | MDO | No. Tickets/TDC/ID).

El encabezado de cada bloque trae CÓDIGO_NOMBRE (ej. AL_MOE2_14_JAVIER MARTINEZ);
el archivo es la autoridad sobre qué líder tiene cada código en el mes (los códigos
rotan de un mes a otro). El cruce va 1º por código en notas y 2º por nombre de líder
acotado a la zona del prefijo (AL/GU/UB), estampando el código al cruzar."""
import re
import json
import unicodedata
from collections import defaultdict
from datetime import datetime, date

from sqlalchemy import select, and_

ACT_LABEL = ["TDC", "Despejes", "Motoverif", "Mayor imp", "Apoyo PDL", "Inoperativo", "T.ocioso"]
ACT_FULL = [
    "No. Tickets/TDC", "Despejes", "Motoverificación C100", "Mayor impacto",
    "Apoyo PDL´s", "Inoperativo (% horas día)", "Tiempo ocioso (% horas día)",
]
_KEYS = ["tickets", "despejes", "motoverific", "mayor impacto", "apoyo pdl", "inoperativo", "tiempo ocioso"]


def _act(t):
    t = str(t or "").lower()
    for i, k in enumerate(_KEYS):
        if k in t:
            return i
    return None


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_seguimiento(wb) -> list[dict]:
    if "Seguimiento Pisos" not in wb.sheetnames:
        return []
    sp = wb["Seguimiento Pisos"]
    rows = list(sp.iter_rows(values_only=True))

    def cell(r, c):
        return rows[r - 1][c - 1] if r - 1 < len(rows) and c - 1 < len(rows[r - 1]) else None

    fechas = {}
    for k in range(40):
        v = cell(2, 4 + 3 * k)
        if isinstance(v, datetime):
            fechas[k] = v.date()
        elif isinstance(v, date):
            fechas[k] = v

    out = []
    r, maxr = 4, sp.max_row
    while r <= maxr:
        b = cell(r, 2)
        if b and str(b).strip():
            hdr = str(b).strip().split("_")
            cod = "_".join(hdr[:3])
            lider_arch = " ".join(p.strip() for p in hdr[3:] if p.strip())
            actrows = {}
            for off in range(7):
                ai = _act(cell(r + off, 3))
                if ai is not None:
                    actrows[ai] = r + off
            for k, f in fechas.items():
                cant_c, mdo_c, tic_c = 4 + 3 * k, 5 + 3 * k, 6 + 3 * k
                detalle = []
                for i in range(7):
                    if i in actrows:
                        rr = actrows[i]
                        tv = cell(rr, tic_c)
                        detalle.append({
                            "actividad": ACT_FULL[i],
                            "cantidad": _num(cell(rr, cant_c)),
                            "mdo": round(_num(cell(rr, mdo_c)), 2),
                            "ticket": str(tv).strip() if tv not in (None, "") else "",
                        })
                    else:
                        detalle.append({"actividad": ACT_FULL[i], "cantidad": 0, "mdo": 0, "ticket": ""})
                mo = round(sum(d["mdo"] for d in detalle[:5]), 2)
                ino = round(sum(d["mdo"] for d in detalle[5:7]), 2)
                partes = [f"{ACT_LABEL[i]}: {detalle[i]['ticket']}" for i in range(7) if detalle[i]["ticket"]]
                pdt = (" | ".join(partes) if partes else "-")[:500]
                if mo or ino or partes:
                    out.append({
                        "codigo": cod, "lider": lider_arch, "fecha": f,
                        "mo_real": mo, "inoperatividad": ino,
                        "num_pdt": pdt, "detalle": detalle, "fila": r, "col": cant_c,
                    })
            r += 7
        else:
            r += 1
    return out


# ── Cruce contra la programación ──────────────────────────────────────────────
PREFIJO_ZONA = {"AL": "ALMEIDAS", "GU": "GUAVIO", "UB": "UBATE"}
_TOKEN_RUIDO = {"RELV", "RELEVO"}


def _tokens(nombre):
    s = unicodedata.normalize("NFD", str(nombre or "").upper())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return [t for t in re.split(r"[^A-Z0-9]+", s) if t and t not in _TOKEN_RUIDO]


def _tok_eq(a, b):
    """Igualdad tolerante a 1 typo en tokens de 5+ letras (GERSON≈GERSSON, GAVILA≈GAVILAN)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1 or min(la, lb) < 5:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) == 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = 0
    salto = False
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        elif not salto:
            salto = True
            j += 1
        else:
            return False
    return True


def _hits(nombre_archivo, nombre_grilla):
    ta, tg = _tokens(nombre_archivo), _tokens(nombre_grilla)
    return sum(1 for t in ta if any(_tok_eq(t, g) for g in tg)), len(ta)


def nombre_coincide(nombre_archivo, nombre_grilla):
    """Fuerte (para elegir fila por nombre): ≥2 tokens del archivo en la grilla (o el único que traiga)."""
    h, n = _hits(nombre_archivo, nombre_grilla)
    return n > 0 and h >= min(2, n)


def nombre_contradice(nombre_archivo, nombre_grilla):
    """Débil (para vetar un cruce por código): 0 tokens en común ⇒ personas distintas."""
    h, n = _hits(nombre_archivo, nombre_grilla)
    return n > 0 and h == 0


def _fila_para(filas_dia, c):
    """Fila de programación para la celda, motivo (str) si hay conflicto, o None."""
    nom = c.get("lider") or ""
    por_cod = [r for r in filas_dia if (r.notas or "").strip() == c["codigo"]]
    ok = [r for r in por_cod if not (nom and nombre_contradice(nom, r.lider or ""))]
    if ok:
        return ok[0]
    if nom:
        zona = PREFIJO_ZONA.get(c["codigo"].split("_")[0])
        cands = [r for r in filas_dia if nombre_coincide(nom, r.lider or "")]
        # la zona del prefijo desambigua (dos JAIME PEÑA en zonas distintas), pero si el
        # líder cambió de zona y en la del prefijo no hay nadie, se busca en toda la unidad
        en_zona = [r for r in cands if str(getattr(r.zona, "value", r.zona) or "") == zona] if zona else []
        cands = en_zona or cands
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            return f"Varios líderes del día coinciden con «{nom}» — pon el código en notas manualmente"
    if por_cod:
        return (f"El código lo tiene {por_cod[0].lider} ese día y no coincide con "
                f"«{nom}» del archivo — revisar antes de cruzar")
    return None


async def cruzar_mantilla(db, uni, celdas):
    """Cruza las celdas del archivo contra la programación de la unidad y actualiza
    # PDT / MO REAL / INOPERATIVIDAD. Al cruzar estampa el código en notas y mantiene
    al día el código del catálogo de líderes (el archivo es la autoridad del mes).
    Devuelve (n_actualizadas, [(celda, motivo_o_None), …]). El caller hace commit."""
    from app.models.models import Programacion, Lider

    fechas = {c["fecha"] for c in celdas}
    rows = (await db.execute(
        select(Programacion).where(and_(
            Programacion.unidad == uni,
            Programacion.fecha >= min(fechas),
            Programacion.fecha <= max(fechas),
        ))
    )).scalars().all()
    por_fecha = defaultdict(list)
    for r in rows:
        por_fecha[r.fecha].append(r)

    actualizadas, sin_match, asignaciones = 0, [], {}
    for c in celdas:
        row = _fila_para(por_fecha.get(c["fecha"], []), c)
        if row is None or isinstance(row, str):
            sin_match.append((c, row))
            continue
        row.notas = c["codigo"]
        row.num_pdt = c["num_pdt"]
        row.mo_real = c["mo_real"]
        row.inoperatividad = c["inoperatividad"]
        row.detalle_mantilla = json.dumps(c["detalle"], ensure_ascii=False)
        actualizadas += 1
        if row.lider:
            asignaciones[row.lider] = c["codigo"]

    if asignaciones:
        lids = (await db.execute(
            select(Lider).where(Lider.unidad == uni, Lider.nombre.in_(asignaciones))
        )).scalars().all()
        for L in lids:
            if L.codigo != asignaciones[L.nombre]:
                L.codigo = asignaciones[L.nombre]

    return actualizadas, sin_match
