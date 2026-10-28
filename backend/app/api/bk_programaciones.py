import calendar
import json
from datetime import date
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, extract, Date, Float, Integer, Enum as SAEnum
from app.db.database import get_db
from app.api.deps import (
    get_current_active_user, unidad_filtros, unidad_de,
    assert_puede_escribir, ROLES_GLOBALES,
)
from app.models.models import Programacion, Lider, Usuario, RolEnum, ZonaEnum, EstadoEnum, UNIDAD_DEFAULT, Auditoria
from app.schemas.schemas import ProgramacionCreate, ProgramacionUpdate, ProgramacionOut, ProgramacionListResponse
from app.services.auth import CAMPO_PERMISOS
from app.config import es_profesional_apoyo

router = APIRouter(prefix="/programaciones", tags=["programaciones"])


def _check_zona_access(user: Usuario, zona: ZonaEnum):
    if user.rol in (RolEnum.supervisor, *ROLES_GLOBALES):
        return
    if user.zona and user.zona != zona:
        raise HTTPException(status_code=403, detail="Sin acceso a esa zona")


def _check_unidad_access(user: Usuario, unidad: str | None):
    if user.rol in ROLES_GLOBALES:
        return
    if (unidad or UNIDAD_DEFAULT) != unidad_de(user):
        raise HTTPException(status_code=403, detail="Sin acceso a esa unidad")


@router.get("", response_model=ProgramacionListResponse)
async def listar(
    zona: Optional[ZonaEnum] = None,
    unidad: Optional[str] = None,
    fecha_inicio: Optional[date] = None,
    fecha_fin: Optional[date] = None,
    lider: Optional[str] = None,
    estado: Optional[EstadoEnum] = None,
    mes: Optional[int] = None,
    anio: Optional[int] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(500, le=2000),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    # Scoping por unidad (cada unidad ve solo lo suyo; globales ven todo / ?unidad=)
    filters = unidad_filtros(current_user, unidad)
    # Ingenieros solo ven su zona. EXCEPCIÓN: el profesional de apoyo VE todas las zonas
    # (requisito 2026-08-19) — aunque solo puede EDITAR la suya (_check_zona_access).
    if (current_user.rol == RolEnum.ingeniero_zona and current_user.zona
            and not es_profesional_apoyo(current_user.email)):
        filters.append(Programacion.zona == current_user.zona)
    elif zona:
        filters.append(Programacion.zona == zona)

    if fecha_inicio:
        filters.append(Programacion.fecha >= fecha_inicio)
    if fecha_fin:
        filters.append(Programacion.fecha <= fecha_fin)
    if lider:
        filters.append(Programacion.lider.ilike(f"%{lider}%"))
    if estado:
        filters.append(Programacion.estado == estado)
    if mes:
        filters.append(func.extract("month", Programacion.fecha) == mes)
    if anio:
        filters.append(func.extract("year", Programacion.fecha) == anio)

    count_q = select(func.count()).select_from(Programacion)
    if filters:
        count_q = count_q.where(and_(*filters))
    total = (await db.execute(count_q)).scalar()

    q = select(Programacion).order_by(Programacion.fecha, Programacion.zona, Programacion.lider)
    if filters:
        q = q.where(and_(*filters))
    q = q.offset(skip).limit(limit)
    items = (await db.execute(q)).scalars().all()

    return ProgramacionListResponse(total=total, items=items)


@router.post("", response_model=ProgramacionOut)
async def crear(
    req: ProgramacionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    assert_puede_escribir(current_user)
    _check_zona_access(current_user, req.zona)
    data = req.model_dump()
    # La unidad la fija el usuario que crea; los roles globales pueden indicarla.
    if current_user.rol in ROLES_GLOBALES:
        data["unidad"] = req.unidad or UNIDAD_DEFAULT
    else:
        data["unidad"] = unidad_de(current_user)
    prog = Programacion(**data, creado_por=current_user.id)
    db.add(prog)
    await db.commit()
    await db.refresh(prog)
    return prog


@router.post("/generar-mes")
async def generar_mes(
    mes: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    unidad: Optional[str] = None,
    forzar: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Genera la mensualidad en vacío: por cada día del mes y cada líder activo del catálogo
    de la unidad, crea una fila con zona/fecha/líder/tipo-grupo + código Mantilla en notas
    (si el catálogo lo tiene) y el resto en blanco (proceso, #PDT…) para que el jefe de zona
    solo diligencie. Idempotente: no duplica líder+fecha.
    SEGURIDAD: si el mes ya tiene programaciones se BLOQUEA (solo aplica a meses vacíos), para
    no afectar un mes ya trabajado. `forzar=true` lo permite y solo completa lo que falte."""
    assert_puede_escribir(current_user)
    if current_user.rol not in (RolEnum.supervisor, *ROLES_GLOBALES):
        raise HTTPException(status_code=403, detail="Solo el jefe de zona puede generar la mensualidad")
    # Los roles globales pueden indicar la unidad (la activa en la grilla); sin ella, la suya
    uni = unidad if (current_user.rol in ROLES_GLOBALES and unidad) else unidad_de(current_user)

    lideres = (await db.execute(
        select(Lider).where(Lider.unidad == uni, Lider.activo == True)  # noqa: E712
    )).scalars().all()
    if not lideres:
        raise HTTPException(status_code=400, detail=f"No hay líderes en el catálogo de la unidad {uni}")

    anio, m = int(mes[:4]), int(mes[5:7])
    ndias = calendar.monthrange(anio, m)[1]
    fechas = [date(anio, m, d) for d in range(1, ndias + 1)]

    # Existentes (líder, fecha) en el mes → para no duplicar
    existentes_rows = (await db.execute(
        select(Programacion.lider, Programacion.fecha).where(
            Programacion.unidad == uni,
            Programacion.fecha >= fechas[0], Programacion.fecha <= fechas[-1],
        )
    )).all()
    existentes = set((row.lider, row.fecha) for row in existentes_rows)

    # SEGURIDAD: no generar sobre un mes que ya tiene contenido (evita dañar lo trabajado)
    if existentes_rows and not forzar:
        raise HTTPException(
            status_code=409,
            detail=(f"El mes {mes} ya tiene {len(existentes_rows)} programaciones. "
                    "«Generar mensualidad» solo aplica a meses vacíos para no afectar lo ya trabajado."),
        )

    creados = 0
    sin_zona = 0
    for f in fechas:
        for L in lideres:
            if (L.nombre, f) in existentes:
                continue
            try:
                z = ZonaEnum(L.zona)
            except (ValueError, TypeError):
                sin_zona += 1
                continue
            db.add(Programacion(
                unidad=uni, zona=z, fecha=f, lider=L.nombre,
                tipo_grupo=L.tipo_grupo, notas=L.codigo, creado_por=current_user.id,
            ))
            creados += 1
    await db.commit()
    return {
        "mes": mes, "unidad": uni, "lideres": len(lideres), "dias": ndias,
        "creados": creados,
        "omitidos_existentes": ndias * len(lideres) - creados - sin_zona,
        "lideres_sin_zona_valida": sin_zona,
    }


# ── Historial de cambios (como en ANS) — sale de la auditoría automática ─────────────
# La tabla `auditoria` (listener before_flush) ya guarda cada celda editada, cada fila creada
# o borrada, venga de donde venga: celda, pegado, importación, Mantilla, SGS o mensualidad.
# Aquí solo se lee acotado a la unidad/zona/mes y se permite REVERTIR una edición de campo.
_CAMPOS_HIST_OMITIDOS = {"actualizado_por", "actualizado_en", "creado_por", "creado_en", "id"}
_CAMPOS_NO_REVERTIBLES = _CAMPOS_HIST_OMITIDOS | {"unidad"}
_CAMPOS_FILA = ("zona", "fecha", "lider", "tipo_grupo")


def _fila_de_snapshot(datos):
    try:
        d = json.loads(datos or "{}")
    except Exception:
        return {}
    return {k: d.get(k) for k in _CAMPOS_FILA}


def _coerce_valor(campo: str, v):
    """Convierte el texto guardado en la auditoría al tipo de la columna."""
    if v in (None, ""):
        return None
    t = Programacion.__table__.columns[campo].type
    if isinstance(t, SAEnum):
        return ZonaEnum(v) if campo == "zona" else v
    if isinstance(t, Date):
        return date.fromisoformat(str(v)[:10])
    if isinstance(t, Float):
        return float(v)
    if isinstance(t, Integer):
        return int(float(v))
    return v


@router.get("/historial")
async def historial(
    unidad: Optional[str] = None,
    zona: Optional[ZonaEnum] = None,
    mes: Optional[int] = None,
    anio: Optional[int] = None,
    registro_id: Optional[int] = None,
    q: Optional[str] = None,
    limite: int = Query(300, le=2000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Quién cambió qué en la programación (antes → después), más recientes primero.
    Mismo alcance que el listado: la unidad activa (roles globales) o la propia; el
    ingeniero de zona solo ve su zona (el profesional de apoyo, todas)."""
    uni = (unidad or UNIDAD_DEFAULT) if current_user.rol in ROLES_GLOBALES else unidad_de(current_user)
    _check_unidad_access(current_user, uni)
    A, P = Auditoria, Programacion
    f = [
        A.tabla == "programaciones",
        or_(A.campo.is_(None), A.campo.not_in(_CAMPOS_HIST_OMITIDOS)),
        # Unidad de la FILA (la auditoría guarda la del usuario): por el join si la fila
        # existe, por el snapshot JSON si fue borrada, por el usuario como último recurso.
        or_(P.unidad == uni,
            and_(P.id.is_(None), A.datos.like('%"unidad": "' + uni + '"%')),
            and_(P.id.is_(None), A.datos.is_(None), A.unidad == uni)),
    ]
    if (current_user.rol == RolEnum.ingeniero_zona and current_user.zona
            and not es_profesional_apoyo(current_user.email)):
        zona = current_user.zona
    if zona:
        f.append(or_(P.zona == zona, and_(P.id.is_(None), A.datos.like('%"zona": "' + zona.value + '"%'))))
    if anio and mes:
        pref = f"{int(anio):04d}-{int(mes):02d}"
        f.append(or_(and_(extract("year", P.fecha) == int(anio), extract("month", P.fecha) == int(mes)),
                     and_(P.id.is_(None), A.datos.like('%"fecha": "' + pref + '%'))))
    if registro_id:
        f.append(A.registro_id == registro_id)
    if q:
        like = f"%{q}%"
        f.append(or_(func.coalesce(A.campo, "").ilike(like),
                     func.coalesce(A.valor_anterior, "").ilike(like),
                     func.coalesce(A.valor_nuevo, "").ilike(like),
                     func.coalesce(A.usuario_nombre, "").ilike(like),
                     func.coalesce(P.lider, "").ilike(like),
                     func.coalesce(A.datos, "").ilike(like)))
    base = select(A, P.zona, P.fecha, P.lider, P.tipo_grupo).outerjoin(P, P.id == A.registro_id).where(and_(*f))
    total = (await db.execute(
        select(func.count()).select_from(A).outerjoin(P, P.id == A.registro_id).where(and_(*f))
    )).scalar_one()
    rows = (await db.execute(
        base.order_by(A.creado_en.desc(), A.id.desc()).limit(limite).offset(offset)
    )).all()
    items = []
    for a, pz, pf, pl, pt in rows:
        if pf is not None or pl is not None:
            fila = {"zona": pz.value if hasattr(pz, "value") else pz, "fecha": pf.isoformat() if pf else None,
                    "lider": pl, "tipo_grupo": pt, "existe": True}
        else:
            fila = {**_fila_de_snapshot(a.datos), "existe": False}
        items.append({
            "id": a.id, "creado_en": a.creado_en.isoformat() if a.creado_en else None,
            "usuario": a.usuario_nombre, "usuario_rol": a.usuario_rol,
            "accion": a.accion, "campo": a.campo,
            "valor_anterior": a.valor_anterior, "valor_nuevo": a.valor_nuevo,
            "registro_id": a.registro_id, "fila": fila,
            "request_id": a.request_id, "ruta": a.ruta, "resumen": a.resumen,
            "revertible": bool(a.accion == "editar" and a.campo and a.campo not in _CAMPOS_NO_REVERTIBLES
                               and a.registro_id and pf is not None),
        })
    return {"total": total, "items": items}


@router.post("/historial/{aud_id}/revertir", response_model=ProgramacionOut)
async def revertir_cambio(
    aud_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    """Devuelve un campo a su valor anterior (deshacer). Queda registrado como una edición
    más, marcada «Reversión del cambio #N». Mismos permisos que editar esa celda."""
    assert_puede_escribir(current_user)
    a = (await db.execute(select(Auditoria).where(Auditoria.id == aud_id))).scalar_one_or_none()
    if not a or a.tabla != "programaciones":
        raise HTTPException(status_code=404, detail="Cambio no encontrado")
    if a.accion != "editar" or not a.campo or a.campo in _CAMPOS_NO_REVERTIBLES:
        raise HTTPException(status_code=400, detail="Solo se pueden revertir ediciones de un campo")
    prog = (await db.execute(select(Programacion).where(Programacion.id == a.registro_id))).scalar_one_or_none()
    if not prog:
        raise HTTPException(status_code=404, detail="La fila ya no existe")
    _check_unidad_access(current_user, prog.unidad)
    _check_zona_access(current_user, prog.zona)
    if current_user.rol != RolEnum.supervisor and a.campo not in CAMPO_PERMISOS.get(current_user.rol, []):
        raise HTTPException(status_code=403, detail=f"No puede editar el campo '{a.campo}'")
    try:
        destino = _coerce_valor(a.campo, a.valor_anterior)
    except Exception:
        raise HTTPException(status_code=400, detail="El valor anterior no se puede restaurar")
    setattr(prog, a.campo, destino)
    prog.actualizado_por = current_user.id
    await db.flush()   # el listener de auditoría registra la edición aquí
    nueva = (await db.execute(
        select(Auditoria).where(Auditoria.tabla == "programaciones", Auditoria.registro_id == prog.id,
                                Auditoria.campo == a.campo).order_by(Auditoria.id.desc()).limit(1)
    )).scalar_one_or_none()
    if nueva and nueva.id != a.id:
        nueva.resumen = f"Reversión del cambio #{a.id}"
    await db.commit()
    await db.refresh(prog)
    return prog


@router.patch("/{prog_id}", response_model=ProgramacionOut)
async def actualizar(
    prog_id: int,
    req: ProgramacionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    assert_puede_escribir(current_user)
    result = await db.execute(select(Programacion).where(Programacion.id == prog_id))
    prog = result.scalar_one_or_none()
    if not prog:
        raise HTTPException(status_code=404, detail="No encontrado")
    _check_unidad_access(current_user, prog.unidad)
    _check_zona_access(current_user, prog.zona)

    # Validar que el rol solo toque sus campos
    campos_permitidos = CAMPO_PERMISOS.get(current_user.rol, [])
    update_data = req.model_dump(exclude_unset=True)

    for campo, valor in update_data.items():
        if current_user.rol != RolEnum.supervisor and campo not in campos_permitidos:
            raise HTTPException(status_code=403, detail=f"No puede editar el campo '{campo}'")
        setattr(prog, campo, valor)

    prog.actualizado_por = current_user.id
    await db.commit()
    await db.refresh(prog)
    return prog


@router.post("/{prog_id}/duplicar", response_model=ProgramacionOut)
async def duplicar(
    prog_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    assert_puede_escribir(current_user)
    if current_user.rol not in (RolEnum.supervisor, RolEnum.ingeniero_zona):
        raise HTTPException(status_code=403, detail="Sin permiso")
    result = await db.execute(select(Programacion).where(Programacion.id == prog_id))
    orig = result.scalar_one_or_none()
    if not orig:
        raise HTTPException(status_code=404, detail="No encontrado")
    _check_unidad_access(current_user, orig.unidad)
    _check_zona_access(current_user, orig.zona)
    copia = Programacion(
        unidad=orig.unidad,
        zona=orig.zona, fecha=orig.fecha, lider=orig.lider,
        tipo_grupo=orig.tipo_grupo, proceso=orig.proceso,
        subproceso=orig.subproceso, proyecto_antecedente=orig.proyecto_antecedente,
        pms=orig.pms, circuito=orig.circuito, lcl=orig.lcl,
        fecha_vigencia=orig.fecha_vigencia, tipo_pdt=orig.tipo_pdt,
        num_pdt=orig.num_pdt, cd_afectados=orig.cd_afectados, saidi=orig.saidi,
        hora=orig.hora, hora_ab=orig.hora_ab, hora_cr=orig.hora_cr,
        municipio=orig.municipio, vereda=orig.vereda,
        alcance=orig.alcance, restricciones=orig.restricciones,
        se_gestiona=orig.se_gestiona, gestor_cliente=orig.gestor_cliente,
        supervisor=orig.supervisor, valor_dia=orig.valor_dia,
        mo_proyectada=orig.mo_proyectada, valor_material=orig.valor_material,
        creado_por=current_user.id,
    )
    db.add(copia)
    await db.commit()
    await db.refresh(copia)
    return copia


@router.delete("/{prog_id}", dependencies=[Depends(get_current_active_user)])
async def eliminar(
    prog_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    assert_puede_escribir(current_user)
    if current_user.rol not in (RolEnum.supervisor, RolEnum.ingeniero_zona):
        raise HTTPException(status_code=403, detail="Sin permiso")
    result = await db.execute(select(Programacion).where(Programacion.id == prog_id))
    prog = result.scalar_one_or_none()
    if not prog:
        raise HTTPException(status_code=404, detail="No encontrado")
    _check_unidad_access(current_user, prog.unidad)
    _check_zona_access(current_user, prog.zona)
    await db.delete(prog)
    await db.commit()
    return {"ok": True}


@router.get("/campos-editables", response_model=List[str])
async def campos_editables(current_user: Usuario = Depends(get_current_active_user)):
    if current_user.rol == RolEnum.supervisor:
        return ["*"]
    return CAMPO_PERMISOS.get(current_user.rol, [])
