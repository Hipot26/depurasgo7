from typing import Optional
from datetime import date, timedelta
import calendar
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, and_
from app.db.database import get_db
from app.api.deps import get_current_active_user, unidad_filtros
from app.models.models import Programacion, Usuario, ZonaEnum, EstadoEnum
from app.schemas.schemas import KPIGeneral, KPIResumen

router = APIRouter(prefix="/kpis", tags=["kpis"])


class KpiFiltros:
    """Filtros compartidos por todas las gráficas del dashboard.
    Se inyecta con Depends(): expone cada campo como query param y construye
    la lista de filtros SQLAlchemy (incluyendo el scoping por unidad)."""

    def __init__(
        self,
        unidad: Optional[str] = Query(None),
        zona: Optional[ZonaEnum] = Query(None),
        mes: Optional[int] = Query(None),
        anio: Optional[int] = Query(None),
        fecha_inicio: Optional[date] = Query(None),
        fecha_fin: Optional[date] = Query(None),
        lider: Optional[str] = Query(None),
        tipo_grupo: Optional[str] = Query(None),
        proceso: Optional[str] = Query(None),
        subproceso: Optional[str] = Query(None),
        municipio: Optional[str] = Query(None),
        estado: Optional[str] = Query(None),
        estado_pdt: Optional[str] = Query(None),
    ):
        self.unidad = unidad
        self.zona = zona
        self.mes = mes
        self.anio = anio
        self.fecha_inicio = fecha_inicio
        self.fecha_fin = fecha_fin
        self.lider = lider
        self.tipo_grupo = tipo_grupo
        self.proceso = proceso
        self.subproceso = subproceso
        self.municipio = municipio
        self.estado = estado
        self.estado_pdt = estado_pdt

    def filtros(self, current_user: Usuario) -> list:
        f = unidad_filtros(current_user, self.unidad)
        if self.mes:
            f.append(func.extract("month", Programacion.fecha) == self.mes)
        if self.anio:
            f.append(func.extract("year", Programacion.fecha) == self.anio)
        if self.fecha_inicio:
            f.append(Programacion.fecha >= self.fecha_inicio)
        if self.fecha_fin:
            f.append(Programacion.fecha <= self.fecha_fin)
        if self.zona:
            f.append(Programacion.zona == self.zona)
        if self.lider:
            f.append(Programacion.lider.ilike(f"%{self.lider}%"))
        if self.tipo_grupo:
            f.append(Programacion.tipo_grupo == self.tipo_grupo)
        if self.proceso:
            f.append(Programacion.proceso == self.proceso)
        if self.subproceso:
            f.append(Programacion.subproceso == self.subproceso)
        if self.municipio:
            f.append(Programacion.municipio.ilike(f"%{self.municipio}%"))
        if self.estado:
            f.append(func.upper(func.trim(Programacion.estado)) == self.estado.strip().upper())
        if self.estado_pdt:
            f.append(func.upper(func.trim(Programacion.estado_pdt)) == self.estado_pdt.strip().upper())
        return f


@router.get("", response_model=KPIGeneral)
async def kpis_generales(
    f: KpiFiltros = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    filters = f.filtros(current_user)

    # Un PDT CANCELADO (columna ESTADO PDT — incluye variantes tipo «SE CANCELA…») no debe
    # aportar plata al dashboard: su MO proyectada/real y su V. dejado ejecutar salen del
    # gráfico por zona y de los KPI (requisito 2026-08-19). La fila SÍ se sigue
    # contando en los conteos (total/ejecutados/…) y en el donut de estados.
    pdt_vivo = func.upper(func.coalesce(Programacion.estado_pdt, "")).notlike("%CANCEL%")

    q = (
        select(
            Programacion.zona,
            func.count(Programacion.id).label("total"),
            func.sum(case((Programacion.estado == EstadoEnum.EJECUTADO, 1), else_=0)).label("ejecutados"),
            func.sum(case((Programacion.estado == EstadoEnum.PARCIAL, 1), else_=0)).label("parciales"),
            func.sum(case((Programacion.estado == EstadoEnum.CANCELADO, 1), else_=0)).label("cancelados"),
            func.sum(case((Programacion.estado == EstadoEnum.PROGRAMADO, 1), else_=0)).label("programados"),
            func.coalesce(func.sum(case((pdt_vivo, Programacion.mo_proyectada), else_=0)), 0).label("mo_proyectada"),
            func.coalesce(func.sum(case((pdt_vivo, Programacion.mo_real), else_=0)), 0).label("mo_real"),
            func.coalesce(func.sum(case((pdt_vivo, Programacion.valor_dejado_ejecutar), else_=0)), 0).label("valor_dejado"),
            func.coalesce(func.sum(Programacion.inoperatividad), 0).label("inoperatividad"),
        )
        .group_by(Programacion.zona)
    )
    if filters:
        q = q.where(and_(*filters))

    rows = (await db.execute(q)).all()

    por_zona = []
    total_proy = total_real = total_dejado = 0.0

    for r in rows:
        mo_p = float(r.mo_proyectada or 0)
        mo_r = float(r.mo_real or 0)
        vd = float(r.valor_dejado or 0)
        pct = round((mo_r / mo_p * 100) if mo_p else 0, 2)
        total_proy += mo_p
        total_real += mo_r
        total_dejado += vd
        por_zona.append(KPIResumen(
            zona=r.zona.value if r.zona else "SIN ZONA",
            total_registros=r.total,
            ejecutados=r.ejecutados or 0,
            parciales=r.parciales or 0,
            cancelados=r.cancelados or 0,
            programados=r.programados or 0,
            mo_proyectada=mo_p,
            mo_real=mo_r,
            valor_dejado_ejecutar=vd,
            inoperatividad=float(r.inoperatividad or 0),
            pct_ejecucion=pct,
        ))

    pct_general = round((total_real / total_proy * 100) if total_proy else 0, 2)
    return KPIGeneral(
        por_zona=por_zona,
        total_mo_proyectada=total_proy,
        total_mo_real=total_real,
        total_valor_dejado=total_dejado,
        pct_ejecucion_general=pct_general,
    )


@router.get("/por-lider")
async def kpis_por_lider(
    f: KpiFiltros = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    filters = f.filtros(current_user)

    q = (
        select(
            Programacion.lider,
            Programacion.tipo_grupo,
            Programacion.zona,
            func.count(Programacion.id).label("total_dias"),
            func.sum(case((Programacion.estado == EstadoEnum.EJECUTADO, 1), else_=0)).label("ejecutados"),
            func.coalesce(func.sum(Programacion.mo_proyectada), 0).label("mo_proyectada"),
            func.coalesce(func.sum(Programacion.mo_real), 0).label("mo_real"),
        )
        .group_by(Programacion.lider, Programacion.tipo_grupo, Programacion.zona)
        .order_by(Programacion.zona, Programacion.lider)
    )
    if filters:
        q = q.where(and_(*filters))

    rows = (await db.execute(q)).all()
    return [
        {
            "lider": r.lider,
            "tipo_grupo": r.tipo_grupo or None,
            "zona": r.zona.value if r.zona else None,
            "total_dias": r.total_dias,
            "ejecutados": r.ejecutados or 0,
            "pct_ejecucion": round((r.ejecutados / r.total_dias * 100) if r.total_dias else 0, 1),
            "mo_proyectada": float(r.mo_proyectada or 0),
            "mo_real": float(r.mo_real or 0),
        }
        for r in rows
    ]


@router.get("/timeline")
async def kpis_timeline(
    f: KpiFiltros = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    filters = f.filtros(current_user)

    q = (
        select(
            Programacion.fecha,
            func.count(Programacion.id).label("total"),
            func.sum(case((Programacion.estado == EstadoEnum.EJECUTADO, 1), else_=0)).label("ejecutados"),
            func.coalesce(func.sum(Programacion.mo_proyectada), 0).label("mo_proyectada"),
            func.coalesce(func.sum(Programacion.mo_real), 0).label("mo_real"),
        )
        .group_by(Programacion.fecha)
        .order_by(Programacion.fecha)
    )
    if filters:
        q = q.where(and_(*filters))

    rows = (await db.execute(q)).all()
    return [
        {
            "fecha": r.fecha.isoformat(),
            "total": r.total,
            "ejecutados": r.ejecutados or 0,
            "mo_proyectada": float(r.mo_proyectada or 0),
            "mo_real": float(r.mo_real or 0),
        }
        for r in rows
    ]


_CURVA_SERIES = ("valor_dia", "mo_proyectada", "mo_real", "mo_facturada")
_CURVA_MAX_DIAS = 800


@router.get("/curva")
async def kpis_curva(
    f: KpiFiltros = Depends(),
    excluir_cancelados: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Curva acumulada del período (requisito del coordinador, 2026-09-21).

    Replica la tabla dinámica de su PROGRAMACIÓN_GUAVIO (hoja REPORTE): por día
    suma VALOR DÍA, MO PROYECTADA, MO REAL y MO FACTURADA, cuenta los turnos
    —filas con VALOR DÍA diligenciado, igual que su «Cuenta de VALOR DIA»— y a
    la derecha lleva el acumulado; esa columna acumulada es la que se grafica.

    Devuelve el total del período y cada zona por separado, con TODOS los días
    del rango presentes: los días sin trabajo dejan la curva plana, como en su
    Excel.

    Por defecto deja FUERA lo cancelado/anulado: «los cancelados quitémoslos, que
    no aparezcan ahí […] la idea es generarle el ciclo pero a lo que realmente se
    ejecuta» (coordinador, 2026-09-22). Con `excluir_cancelados=false` se ve
    el universo completo, como su tabla dinámica del Excel.
    """
    filters = f.filtros(current_user)
    if excluir_cancelados:
        # En la Programación la cancelación se escribe indistintamente en ESTADO o en
        # ESTADO PDT (CANCELADO, ANULADO, «SE CANCELA PARA ATENDER TUTELA»…): se miran
        # las dos columnas. OJO: los KPI del dashboard usan otra regla, más estrecha
        # (solo %CANCEL% sobre estado_pdt) — no son comparables sin tener esto en cuenta.
        for col in (Programacion.estado, Programacion.estado_pdt):
            v = func.upper(func.coalesce(col, ""))
            filters.append(v.notlike("%CANCEL%"))
            filters.append(v.notlike("%ANULAD%"))

    q = (
        select(
            Programacion.fecha,
            Programacion.zona,
            func.count(Programacion.valor_dia).label("turnos"),
            func.coalesce(func.sum(Programacion.valor_dia), 0).label("valor_dia"),
            func.coalesce(func.sum(Programacion.mo_proyectada), 0).label("mo_proyectada"),
            func.coalesce(func.sum(Programacion.mo_real), 0).label("mo_real"),
            func.coalesce(func.sum(Programacion.mo_facturada), 0).label("mo_facturada"),
        )
        .group_by(Programacion.fecha, Programacion.zona)
        .order_by(Programacion.fecha)
    )
    if filters:
        q = q.where(and_(*filters))
    rows = (await db.execute(q)).all()

    # ── Rango de días a dibujar ───────────────────────────────────────────────
    # El del filtro de fechas; si el filtro es por mes/año, el mes completo;
    # y si no hay filtro de período, el rango que tengan los datos.
    desde, hasta = f.fecha_inicio, f.fecha_fin
    if (not desde or not hasta) and f.anio and f.mes:
        desde = desde or date(f.anio, f.mes, 1)
        hasta = hasta or date(f.anio, f.mes, calendar.monthrange(f.anio, f.mes)[1])
    if (not desde or not hasta) and rows:
        fechas = [r.fecha for r in rows]
        desde = desde or min(fechas)
        hasta = hasta or max(fechas)
    if not desde or not hasta or hasta < desde:
        return {"desde": None, "hasta": None, "zonas": [], "dias": [],
                "por_zona": {}, "totales": {}, "totales_zona": {}, "recortado": False}

    recortado = (hasta - desde).days + 1 > _CURVA_MAX_DIAS
    if recortado:
        hasta = desde + timedelta(days=_CURVA_MAX_DIAS - 1)

    def _cero() -> dict:
        d = {k: 0.0 for k in _CURVA_SERIES}
        d["turnos"] = 0
        return d

    bruto: dict = {}
    for r in rows:
        z = r.zona.value if hasattr(r.zona, "value") else str(r.zona)
        bruto[(r.fecha, z)] = {
            "turnos": int(r.turnos or 0),
            "valor_dia": float(r.valor_dia or 0),
            "mo_proyectada": float(r.mo_proyectada or 0),
            "mo_real": float(r.mo_real or 0),
            "mo_facturada": float(r.mo_facturada or 0),
        }

    zonas = sorted({z for _, z in bruto.keys()})
    vacio = _cero()

    def _fila(dia: date, d: dict, acum: dict) -> dict:
        acum["turnos"] += d["turnos"]
        fila = {"fecha": dia.isoformat(), "turnos": d["turnos"], "acum_turnos": acum["turnos"]}
        for k in _CURVA_SERIES:
            acum[k] += d[k]
            fila[k] = round(d[k], 2)
            fila["acum_" + k] = round(acum[k], 2)
        return fila

    acum_total = _cero()
    acum_zona = {z: _cero() for z in zonas}
    dias: list[dict] = []
    por_zona: dict[str, list] = {z: [] for z in zonas}

    dia = desde
    while dia <= hasta:
        del_dia = _cero()
        for z in zonas:
            d = bruto.get((dia, z), vacio)
            por_zona[z].append(_fila(dia, d, acum_zona[z]))
            del_dia["turnos"] += d["turnos"]
            for k in _CURVA_SERIES:
                del_dia[k] += d[k]
        dias.append(_fila(dia, del_dia, acum_total))
        dia += timedelta(days=1)

    def _tot(a: dict) -> dict:
        return {"turnos": a["turnos"], **{k: round(a[k], 2) for k in _CURVA_SERIES}}

    return {
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "zonas": zonas,
        "dias": dias,
        "por_zona": por_zona,
        "totales": _tot(acum_total),
        "totales_zona": {z: _tot(a) for z, a in acum_zona.items()},
        "recortado": recortado,
    }


def _norm_estado(v) -> Optional[str]:
    """Normaliza un valor de estado de texto libre: trim + mayúsculas + corrige typos."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if not s:
        return None
    # Corrección de typos vistos en los Excel de origen
    fixes = {"APROBRADO": "APROBADO"}
    return fixes.get(s, s)


async def _distribucion(db: AsyncSession, columna, filters) -> dict:
    """Distribución dinámica de una columna de estado (texto libre):
    global + por zona, normalizando valores. Robusto a cualquier vocabulario."""
    q = (
        select(
            Programacion.zona,
            columna,
            func.count(Programacion.id).label("total"),
            func.coalesce(func.sum(Programacion.mo_real), 0).label("mo_real"),
            func.coalesce(func.sum(Programacion.mo_proyectada), 0).label("mo_proy"),
        )
        .group_by(Programacion.zona, columna)
    )
    if filters:
        q = q.where(and_(*filters))
    rows = (await db.execute(q)).all()

    glob: dict = {}        # label -> {total, mo_real, mo_proy}
    por_zona: dict = {}    # zona  -> {label -> total}
    sin = 0
    for zona, val, total, mo_real, mo_proy in rows:
        label = _norm_estado(val)
        zkey = zona.value if zona else "SIN_ZONA"
        if label is None:
            sin += total
            continue
        g = glob.setdefault(label, {"total": 0, "mo_real": 0.0, "mo_proy": 0.0})
        g["total"] += total
        g["mo_real"] += float(mo_real or 0)
        g["mo_proy"] += float(mo_proy or 0)
        por_zona.setdefault(zkey, {})
        por_zona[zkey][label] = por_zona[zkey].get(label, 0) + total

    labels = sorted(glob, key=lambda k: -glob[k]["total"])
    con = sum(g["total"] for g in glob.values())
    return {
        "labels": labels,
        "dist": [
            {"estado": k, "total": glob[k]["total"], "mo_real": glob[k]["mo_real"], "mo_proyectada": glob[k]["mo_proy"]}
            for k in labels
        ],
        "por_zona": [
            {"zona": z, **{l: counts.get(l, 0) for l in labels}}
            for z, counts in por_zona.items()
        ],
        "con_estado": con,
        "sin_estado": sin,
        "total": con + sin,
    }


@router.get("/detalle")
async def kpis_detalle(
    campo: str = Query(..., description="estado | estado_pdt"),
    f: KpiFiltros = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Lista los registros que tienen diligenciado un campo de estado,
    con la info clave del proyecto, para ver exactamente cuáles son."""
    cols = {"estado": Programacion.estado, "estado_pdt": Programacion.estado_pdt}
    col = cols.get(campo)
    if col is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="campo inválido (usa 'estado' o 'estado_pdt')")

    filters = [col.isnot(None), func.trim(col) != ""] + f.filtros(current_user)

    q = (
        select(
            Programacion.zona, Programacion.fecha, Programacion.lider, Programacion.tipo_grupo,
            Programacion.proyecto_antecedente, Programacion.pms, Programacion.circuito,
            Programacion.num_pdt, Programacion.municipio, col.label("estado_val"),
            Programacion.mo_real, Programacion.mo_proyectada,
        )
        .where(and_(*filters))
        .order_by(col, Programacion.zona, Programacion.fecha)
    )
    rows = (await db.execute(q)).all()
    return [
        {
            "estado": _norm_estado(r.estado_val),
            "zona": r.zona.value if r.zona else None,
            "fecha": r.fecha.isoformat() if r.fecha else None,
            "lider": r.lider,
            "tipo_grupo": r.tipo_grupo or None,
            "proyecto": r.proyecto_antecedente or r.pms or r.circuito or None,
            "pms": r.pms,
            "num_pdt": r.num_pdt,
            "municipio": r.municipio,
            "mo_real": float(r.mo_real or 0),
            "mo_proyectada": float(r.mo_proyectada or 0),
        }
        for r in rows
    ]


@router.get("/facturacion")
async def kpis_facturacion(
    f: KpiFiltros = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Estados dinámicos: programación (estado) y SGS/facturación (estado_pdt).
    Funciona con el vocabulario real de los datos (texto libre)."""
    filters = f.filtros(current_user)
    estado_prog = await _distribucion(db, Programacion.estado, filters)
    estado_pdt  = await _distribucion(db, Programacion.estado_pdt, filters)
    return {"estado_prog": estado_prog, "estado_pdt": estado_pdt}


@router.get("/opciones")
async def kpis_opciones(
    f: KpiFiltros = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Valores distintos presentes en los datos (scope solo por unidad) para
    poblar los filtros del dashboard de forma dinámica."""
    base = unidad_filtros(current_user, f.unidad)

    async def distintos(col, normalizar=False, limite=None):
        q = (
            select(col, func.count(Programacion.id).label("n"))
            .where(and_(*base, col.isnot(None)))
            .group_by(col)
            .order_by(func.count(Programacion.id).desc())
        )
        rows = (await db.execute(q)).all()
        out, seen = [], set()
        for v, _ in rows:
            label = _norm_estado(v) if normalizar else (str(v).strip() if v is not None else None)
            if not label or label in seen:
                continue
            seen.add(label)
            out.append(label)
        return out[:limite] if limite else out

    # Zonas presentes (columna enum → .value)
    zq = select(Programacion.zona).where(and_(*base)).group_by(Programacion.zona)
    zonas = [r[0].value for r in (await db.execute(zq)).all() if r[0] is not None]

    return {
        "zonas": zonas,
        "lideres": await distintos(Programacion.lider, limite=300),
        "tipos": await distintos(Programacion.tipo_grupo),
        "procesos": await distintos(Programacion.proceso),
        "subprocesos": await distintos(Programacion.subproceso),
        "municipios": await distintos(Programacion.municipio, limite=300),
        "estados": await distintos(Programacion.estado, normalizar=True),
        "estados_pdt": await distintos(Programacion.estado_pdt, normalizar=True),
    }


# Letras de día de semana (Lun..Dom) — estilo del Excel de Mónica (L M M J V S D)
_DOW = ["L", "M", "M", "J", "V", "S", "D"]


@router.get("/matriz")
async def kpis_matriz(
    anio: int = Query(...),
    mes: int = Query(...),
    unidad: Optional[str] = Query(None),
    zona: Optional[ZonaEnum] = Query(None),
    tipo_grupo: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Matriz de producción cuadrilla × día (vista de dirección): pivote de los datos
    de las unidades SABOGAL/CHOCONTA. Filas=cuadrilla (líder+tipo), columnas=días del mes,
    celda=valor proyectado/real de ese día. Solo lectura, no afecta la grilla."""
    filters = unidad_filtros(current_user, unidad)
    filters.append(func.extract("month", Programacion.fecha) == mes)
    filters.append(func.extract("year", Programacion.fecha) == anio)
    if zona:
        filters.append(Programacion.zona == zona)
    if tipo_grupo:
        filters.append(Programacion.tipo_grupo == tipo_grupo)

    q = (
        select(
            Programacion.zona, Programacion.lider, Programacion.tipo_grupo, Programacion.fecha,
            func.coalesce(func.sum(Programacion.mo_proyectada), 0).label("proy"),
            func.coalesce(func.sum(Programacion.mo_real), 0).label("real"),
            func.count(Programacion.id).label("n"),
        )
        .where(and_(*filters))
        .group_by(Programacion.zona, Programacion.lider, Programacion.tipo_grupo, Programacion.fecha)
    )
    rows = (await db.execute(q)).all()

    cuad: dict = {}        # (zona, lider, tipo) -> fila de cuadrilla
    tot_dia: dict = {}     # dia -> {proy, real}
    for r in rows:
        zval = r.zona.value if r.zona else None
        key = (zval, r.lider, r.tipo_grupo)
        c = cuad.setdefault(key, {
            "zona": zval, "lider": r.lider, "tipo_grupo": r.tipo_grupo,
            "dias": {}, "total_proy": 0.0, "total_real": 0.0, "dias_trab": 0,
        })
        d = r.fecha.day
        proy, real = float(r.proy or 0), float(r.real or 0)
        c["dias"][str(d)] = {"proy": proy, "real": real, "n": r.n}
        c["total_proy"] += proy
        c["total_real"] += real
        if real > 0:
            c["dias_trab"] += 1
        t = tot_dia.setdefault(str(d), {"proy": 0.0, "real": 0.0})
        t["proy"] += proy
        t["real"] += real

    cuadrillas = []
    for v in cuad.values():
        v["cumplimiento"] = round(v["total_real"] / v["total_proy"], 4) if v["total_proy"] > 0 else None
        cuadrillas.append(v)
    cuadrillas.sort(key=lambda x: (x["zona"] or "", x["tipo_grupo"] or "", x["lider"] or ""))

    ndias = calendar.monthrange(anio, mes)[1]
    dias = [
        {"dia": d, "dow": _DOW[date(anio, mes, d).weekday()], "finde": date(anio, mes, d).weekday() >= 5}
        for d in range(1, ndias + 1)
    ]

    total_proy = sum(c["total_proy"] for c in cuadrillas)
    total_real = sum(c["total_real"] for c in cuadrillas)

    def agrupar(keyf):
        g: dict = {}
        for c in cuadrillas:
            k = keyf(c) or "—"
            e = g.setdefault(k, {"grupo": k, "cuadrillas": 0, "total_proy": 0.0, "total_real": 0.0})
            e["cuadrillas"] += 1
            e["total_proy"] += c["total_proy"]
            e["total_real"] += c["total_real"]
        for e in g.values():
            e["cumplimiento"] = round(e["total_real"] / e["total_proy"], 4) if e["total_proy"] > 0 else None
        return sorted(g.values(), key=lambda x: -x["total_real"])

    return {
        "anio": anio, "mes": mes, "dias": dias,
        "cuadrillas": cuadrillas,
        "totales_dia": tot_dia,
        "total_proy": total_proy,
        "total_real": total_real,
        "cumplimiento": round(total_real / total_proy, 4) if total_proy > 0 else None,
        "por_tipo": agrupar(lambda c: c["tipo_grupo"]),
        "por_zona": agrupar(lambda c: c["zona"]),
    }


# ── Matriz por PROYECTO (requisito del coordinador, 2026-10-08) ───────────────────────────
# sgo7-matriz-proyectos v3 (2026-10-09)
# var 1 = PROYECTO / ANTECEDENTE (filas) · var 2 = TIPO (sub-filas) · var 3 = FECHA (columnas = días del mes).
# Celda = cuadrillas (líderes distintos) de ese tipo en ese proyecto ese día. Al final de cada fila: MO proyectada
# y MO real del mes (None = n/a: ninguna fila la tiene registrada).
from fastapi import HTTPException as _HTTPExc

_PROY_COL = func.coalesce(func.nullif(func.trim(Programacion.proyecto_antecedente), ""), "")
_TIPO_COL = func.coalesce(func.nullif(func.trim(Programacion.tipo_grupo), ""), "")


def _filtros_proyectos(current_user, unidad, anio, mes, dia, zona, tipo_grupo):
    filters = unidad_filtros(current_user, unidad)
    filters.append(func.extract("year", Programacion.fecha) == anio)
    if mes:
        filters.append(func.extract("month", Programacion.fecha) == mes)
    if dia:
        if not mes:
            raise _HTTPExc(status_code=400, detail="Para filtrar por día hace falta el mes")
        filters.append(func.extract("day", Programacion.fecha) == dia)
    if zona:
        filters.append(Programacion.zona == zona)
    if tipo_grupo:
        filters.append(_TIPO_COL == tipo_grupo.strip())
    return filters


def _mo(total, n):
    """Suma de MO o None (= n/a) cuando ninguna fila del grupo la tiene registrada."""
    return float(total or 0) if n else None


def _cumpl(proy, real):
    return round(real / proy, 4) if (proy and real is not None) else None


def _agg():
    """Agregados comunes: filas, líderes distintos, MO proyectada y real con su conteo de filas con dato."""
    return (
        func.count(Programacion.id).label("n"),
        func.count(func.distinct(Programacion.lider)).label("cuadrillas"),
        func.sum(Programacion.mo_proyectada).label("proy"), func.count(Programacion.mo_proyectada).label("n_proy"),
        func.sum(Programacion.mo_real).label("real"), func.count(Programacion.mo_real).label("n_real"),
    )


def _celda(r):
    return {"n": r.n, "cuadrillas": r.cuadrillas, "proy": _mo(r.proy, r.n_proy), "real": _mo(r.real, r.n_real)}


@router.get("/matriz-proyectos")
async def kpis_matriz_proyectos(
    anio: int = Query(...),
    mes: int = Query(..., ge=1, le=12),
    unidad: Optional[str] = Query(None),
    zona: Optional[ZonaEnum] = Query(None),
    tipo_grupo: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Matriz Proyecto × Tipo × Día del mes (solo lectura, sale de la Programación).
    Por proyecto y por tipo: `dias[d]` = {cuadrillas, n, proy, real} de ese día, y totales del mes
    (cuadrillas = líderes distintos del mes, n = filas cuadrilla·día, MO None = n/a).
    Las filas sin PROYECTO / ANTECEDENTE van en un grupo aparte (sin_proyecto=True)."""
    filters = _filtros_proyectos(current_user, unidad, anio, mes, None, zona, tipo_grupo)
    W = and_(*filters)

    async def agrupar(*cols):
        return (await db.execute(select(*cols, *_agg()).where(W).group_by(*cols))).all()

    por_pt_dia = await agrupar(_PROY_COL.label("proyecto"), _TIPO_COL.label("tipo"), Programacion.fecha)
    por_pt = await agrupar(_PROY_COL.label("proyecto"), _TIPO_COL.label("tipo"))
    por_p_dia = await agrupar(_PROY_COL.label("proyecto"), Programacion.fecha)
    por_p = await agrupar(_PROY_COL.label("proyecto"))
    por_t = await agrupar(_TIPO_COL.label("tipo"))
    por_dia = await agrupar(Programacion.fecha)
    total = (await db.execute(select(*_agg()).where(W))).one()

    proyectos: dict = {}
    for r in por_p:
        proyectos[r.proyecto] = {"proyecto": r.proyecto, "sin_proyecto": r.proyecto == "", **_celda(r),
                                 "dias_trab": 0, "dias": {}, "tipos": {}}
    for r in por_p_dia:
        p = proyectos[r.proyecto]
        p["dias"][str(r.fecha.day)] = _celda(r)
        p["dias_trab"] += 1
    for r in por_pt:
        proyectos[r.proyecto]["tipos"][r.tipo] = {"tipo": r.tipo, **_celda(r), "dias_trab": 0, "dias": {}}
    for r in por_pt_dia:
        t = proyectos[r.proyecto]["tipos"][r.tipo]
        t["dias"][str(r.fecha.day)] = _celda(r)
        t["dias_trab"] += 1

    lista = []
    for p in proyectos.values():
        p["mo_proy"], p["mo_real"] = p.pop("proy"), p.pop("real")
        p["cumplimiento"] = _cumpl(p["mo_proy"], p["mo_real"])
        tipos = []
        for t in p["tipos"].values():
            t["mo_proy"], t["mo_real"] = t.pop("proy"), t.pop("real")
            t["cumplimiento"] = _cumpl(t["mo_proy"], t["mo_real"])
            tipos.append(t)
        tipos.sort(key=lambda x: (-x["n"], x["tipo"]))
        p["tipos"] = tipos
        lista.append(p)
    lista.sort(key=lambda x: (x["sin_proyecto"], -x["n"], x["proyecto"]))

    ndias = calendar.monthrange(anio, mes)[1]
    dias = [{"dia": d, "dow": _DOW[date(anio, mes, d).weekday()], "finde": date(anio, mes, d).weekday() >= 5}
            for d in range(1, ndias + 1)]
    tipos_res = []
    for r in sorted(por_t, key=lambda x: -x.n):
        c = _celda(r)
        tipos_res.append({"tipo": r.tipo, "n": c["n"], "cuadrillas": c["cuadrillas"],
                          "mo_proy": c["proy"], "mo_real": c["real"], "cumplimiento": _cumpl(c["proy"], c["real"])})
    mo_proy, mo_real = _mo(total.proy, total.n_proy), _mo(total.real, total.n_real)
    sin = next((p for p in lista if p["sin_proyecto"]), None)
    return {
        "anio": anio, "mes": mes, "dias": dias,
        "proyectos": lista,
        "proyectos_con_nombre": sum(1 for p in lista if not p["sin_proyecto"]),
        "sin_proyecto_n": sin["n"] if sin else 0,
        "totales_dia": {str(r.fecha.day): _celda(r) for r in por_dia},
        "n": total.n or 0, "cuadrillas": total.cuadrillas or 0, "dias_trab": len(por_dia),
        "mo_proy": mo_proy, "mo_real": mo_real, "cumplimiento": _cumpl(mo_proy, mo_real),
        "por_tipo": tipos_res,
    }


@router.get("/matriz-proyectos-detalle")
async def kpis_matriz_proyectos_detalle(
    anio: int = Query(...),
    proyecto: str = Query(""),
    tipo: Optional[str] = Query(None),
    mes: Optional[int] = Query(None, ge=1, le=12),
    dia: Optional[int] = Query(None, ge=1, le=31),
    unidad: Optional[str] = Query(None),
    zona: Optional[ZonaEnum] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Filas de programación detrás de una celda (Proyecto × Tipo × día) o de una fila (sin día): justifica el conteo.
    proyecto="" = filas sin PROYECTO / ANTECEDENTE; tipo="" = filas sin TIPO; tipo ausente = todos los tipos."""
    filters = _filtros_proyectos(current_user, unidad, anio, mes, dia, zona, None)
    filters.append(_PROY_COL == proyecto.strip())
    if tipo is not None:
        filters.append(_TIPO_COL == tipo.strip())
    q = select(Programacion).where(and_(*filters)).order_by(Programacion.fecha, Programacion.lider, Programacion.id)
    rows = (await db.execute(q)).scalars().all()
    items = [
        {
            "id": p.id, "fecha": p.fecha.isoformat() if p.fecha else None,
            "zona": p.zona.value if p.zona else None, "lider": p.lider, "tipo": p.tipo_grupo,
            "num_pdt": p.num_pdt, "proceso": p.proceso, "subproceso": p.subproceso, "municipio": p.municipio,
            "estado": p.estado, "estado_pdt": p.estado_pdt,
            "mo_proyectada": float(p.mo_proyectada) if p.mo_proyectada is not None else None,
            "mo_real": float(p.mo_real) if p.mo_real is not None else None,
        }
        for p in rows
    ]
    con_p = [i["mo_proyectada"] for i in items if i["mo_proyectada"] is not None]
    con_r = [i["mo_real"] for i in items if i["mo_real"] is not None]
    return {
        "proyecto": proyecto, "tipo": tipo, "items": items,
        "cuadrillas": len({i["lider"] for i in items}),
        "mo_proy": sum(con_p) if con_p else None,
        "mo_real": sum(con_r) if con_r else None,
    }


@router.get("/matriz-detalle")
async def kpis_matriz_detalle(
    fecha: date = Query(...),
    lider: str = Query(...),
    unidad: Optional[str] = Query(None),
    zona: Optional[ZonaEnum] = Query(None),
    tipo_grupo: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Programaciones reales detrás de UNA celda de la matriz (cuadrilla + día):
    justifica de dónde sale el valor sumado."""
    filters = unidad_filtros(current_user, unidad)
    filters.append(Programacion.fecha == fecha)
    filters.append(Programacion.lider == lider)
    if zona:
        filters.append(Programacion.zona == zona)
    if tipo_grupo:
        filters.append(Programacion.tipo_grupo == tipo_grupo)

    q = (
        select(Programacion)
        .where(and_(*filters))
        .order_by(Programacion.id)
    )
    rows = (await db.execute(q)).scalars().all()
    items = [
        {
            "id": p.id,
            "proyecto": p.proyecto_antecedente or p.pms or p.circuito or None,
            "num_pdt": p.num_pdt,
            "proceso": p.proceso,
            "subproceso": p.subproceso,
            "municipio": p.municipio,
            "estado": p.estado,
            "estado_pdt": p.estado_pdt,
            "mo_proyectada": float(p.mo_proyectada or 0),
            "mo_real": float(p.mo_real or 0),
            "valor_dia": float(p.valor_dia or 0),
        }
        for p in rows
    ]
    return {
        "fecha": fecha.isoformat(),
        "lider": lider,
        "items": items,
        "total_proy": sum(i["mo_proyectada"] for i in items),
        "total_real": sum(i["mo_real"] for i in items),
    }
