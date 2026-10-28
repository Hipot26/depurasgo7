"""sgo7.py — Registro de trazabilidad de los módulos SGO7.

Cada módulo (Balance, Materiales, Verificador, Devoluciones, Liquidador) envía un registro
por archivo subido, proceso ejecutado o descarga generada, con el usuario SGO7 que lo hizo.

- POST /api/sgo7/registros  → lo llaman los módulos desde el propio servidor con el token
  interno SGO7_INTERNAL_TOKEN (cabecera X-SGO7-Interno). nginx niega este POST desde fuera.
- GET  /api/sgo7/registros  → lista con filtros. GET /api/sgo7/registros/resumen → conteos.
  Por ahora solo rol desarrollador (require_dev).
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_user as _usuario_activo
from app.config import settings
from app.db.database import get_db
from app.models.models import RegistroSGO7, CredencialSGS, Usuario, CasoFacturacion, CasoEvento
from app.api.deps import get_current_active_user, require_dev
from cryptography.fernet import Fernet, InvalidToken

router = APIRouter(prefix="/sgo7", tags=["sgo7"])


async def require_registros(user=Depends(_usuario_activo)):
    """Registros: el desarrollador ve todo; quien puede_liquidador solo el módulo liquidador y solo lo suyo."""
    from app.config import puede_liquidador
    rol = user.rol.value if hasattr(user.rol, "value") else str(user.rol)
    if rol == "desarrollador":
        return {"user": user, "solo_modulo": None}
    if puede_liquidador(user.rol, user.email):
        # SGO7: quien no es desarrollador solo ve lo que hizo con SU cuenta (login = email)
        return {"user": user, "solo_modulo": "liquidador",
                "solo_usuario": (user.email or "").strip().lower()}
    raise HTTPException(status_code=403, detail="Sin acceso a los registros")
_TZ = ZoneInfo("America/Bogota")


class RegistroIn(BaseModel):
    modulo: str = Field(max_length=40)
    usuario: Optional[str] = Field(default=None, max_length=150)
    rol: Optional[str] = Field(default=None, max_length=40)
    accion: str = Field(max_length=80)
    metodo: Optional[str] = Field(default=None, max_length=10)
    ruta: Optional[str] = Field(default=None, max_length=300)
    archivo: Optional[str] = Field(default=None, max_length=300)
    archivo_hash: Optional[str] = Field(default=None, max_length=64)
    archivo_bytes: Optional[int] = None
    archivo_copia: Optional[str] = Field(default=None, max_length=400)
    estado: Optional[str] = Field(default=None, max_length=40)
    duracion_ms: Optional[int] = None
    ip: Optional[str] = Field(default=None, max_length=64)
    sesion: Optional[str] = Field(default=None, max_length=120)
    detalle: Optional[dict[str, Any]] = None
    ts: Optional[datetime] = None


class RegistroOut(RegistroIn):
    id: int
    ts: datetime
    model_config = {"from_attributes": True}


def _interno(x_sgo7_interno: str = Header(default="")):
    """Solo los módulos, desde el servidor, con el token interno."""
    if not settings.SGO7_INTERNAL_TOKEN or x_sgo7_interno != settings.SGO7_INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="Token interno inválido")


def _aware(d: Optional[datetime]) -> Optional[datetime]:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_TZ)


@router.post("/registros", status_code=201, dependencies=[Depends(_interno)], include_in_schema=False)
async def crear_registro(reg: RegistroIn, db: AsyncSession = Depends(get_db)):
    r = RegistroSGO7(**reg.model_dump(exclude={"ts"}), ts=_aware(reg.ts) or datetime.now(timezone.utc))
    db.add(r)
    await db.commit()
    return {"ok": True, "id": r.id}


@router.get("/registros", response_model=list[RegistroOut])
async def listar_registros(
    db: AsyncSession = Depends(get_db), acceso: dict = Depends(require_registros),
    modulo: Optional[str] = None, usuario: Optional[str] = None, accion: Optional[str] = None,
    desde: Optional[datetime] = None, hasta: Optional[datetime] = None, q: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
):
    stmt = select(RegistroSGO7)
    if acceso.get("solo_modulo"):
        modulo = acceso["solo_modulo"]
    if acceso.get("solo_usuario"):
        usuario = None
        stmt = stmt.where(func.lower(RegistroSGO7.usuario) == acceso["solo_usuario"])
    if modulo:
        stmt = stmt.where(RegistroSGO7.modulo == modulo)
    if usuario:
        stmt = stmt.where(RegistroSGO7.usuario.ilike(f"%{usuario}%"))
    if accion:
        stmt = stmt.where(RegistroSGO7.accion == accion)
    if desde:
        stmt = stmt.where(RegistroSGO7.ts >= _aware(desde))
    if hasta:
        stmt = stmt.where(RegistroSGO7.ts <= _aware(hasta))
    if q:
        stmt = stmt.where(or_(RegistroSGO7.archivo.ilike(f"%{q}%"), RegistroSGO7.ruta.ilike(f"%{q}%"),
                              RegistroSGO7.sesion.ilike(f"%{q}%")))
    stmt = stmt.order_by(desc(RegistroSGO7.ts), desc(RegistroSGO7.id)).offset(offset).limit(limit)
    return (await db.execute(stmt)).scalars().all()


@router.get("/registros/resumen")
async def resumen_registros(db: AsyncSession = Depends(get_db), acceso: dict = Depends(require_registros)):
    hace7 = datetime.now(timezone.utc) - timedelta(days=7)
    stmt = (select(RegistroSGO7.modulo, func.count().label("total"),
                   func.count().filter(RegistroSGO7.ts >= hace7).label("ultimos_7d"),
                   func.max(RegistroSGO7.ts).label("ultimo"))
            .group_by(RegistroSGO7.modulo).order_by(RegistroSGO7.modulo))
    if acceso.get("solo_modulo"):
        stmt = stmt.where(RegistroSGO7.modulo == acceso["solo_modulo"])
    if acceso.get("solo_usuario"):
        stmt = stmt.where(func.lower(RegistroSGO7.usuario) == acceso["solo_usuario"])
    filas = (await db.execute(stmt)).all()
    return {"modulos": [{"modulo": m, "total": t, "ultimos_7d": u7, "ultimo": ul} for m, t, u7, ul in filas]}


# ── Cuenta SGS por usuario (para que cada quien liquide con su propia cuenta) ─────────────
def _fernet() -> Fernet:
    if not settings.SGO7_CRED_KEY:
        raise HTTPException(status_code=500, detail="SGO7_CRED_KEY no configurada")
    return Fernet(settings.SGO7_CRED_KEY.encode())


class CuentaSGSIn(BaseModel):
    sgs_login: str = Field(min_length=2, max_length=120)
    sgs_password: str = Field(min_length=1, max_length=200)


async def _registro(db: AsyncSession, usuario: str, accion: str, detalle: dict):
    db.add(RegistroSGO7(modulo="sgo7", usuario=usuario, accion=accion, estado="ok", detalle=detalle,
                        ts=datetime.now(timezone.utc)))


def _cuenta_out(c: Optional[CredencialSGS]) -> dict:
    if not c:
        return {"registrada": False}
    return {"registrada": True, "sgs_login": c.sgs_login, "actualizado": c.actualizado,
            "ultimo_ok": c.ultimo_ok, "ultimo_error": c.ultimo_error, "ultimo_intento": c.ultimo_intento}


@router.get("/sgs-cuenta")
async def mi_cuenta_sgs(user: Usuario = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(CredencialSGS).where(CredencialSGS.usuario == user.email))).scalar_one_or_none()
    return _cuenta_out(c)


@router.put("/sgs-cuenta")
async def guardar_cuenta_sgs(body: CuentaSGSIn, user: Usuario = Depends(get_current_active_user),
                             db: AsyncSession = Depends(get_db)):
    enc = _fernet().encrypt(body.sgs_password.encode("utf-8")).decode("ascii")
    c = (await db.execute(select(CredencialSGS).where(CredencialSGS.usuario == user.email))).scalar_one_or_none()
    nueva = c is None
    if c is None:
        c = CredencialSGS(usuario=user.email)
        db.add(c)
    c.sgs_login = body.sgs_login.strip()
    c.sgs_password_enc = enc
    c.actualizado = datetime.now(timezone.utc)
    c.actualizado_por = user.email
    c.ultimo_ok = None
    c.ultimo_error = None
    await _registro(db, user.email, "cuenta_sgs_guardada", {"sgs_login": c.sgs_login, "nueva": nueva})
    await db.commit()
    return _cuenta_out(c)


@router.delete("/sgs-cuenta")
async def borrar_cuenta_sgs(user: Usuario = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(CredencialSGS).where(CredencialSGS.usuario == user.email))).scalar_one_or_none()
    if c:
        await db.delete(c)
        await _registro(db, user.email, "cuenta_sgs_borrada", {"sgs_login": c.sgs_login})
        await db.commit()
    return {"registrada": False}


@router.get("/interno/sgs-cuenta", dependencies=[Depends(_interno)], include_in_schema=False)
async def interno_cuenta_sgs(usuario: str, db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(CredencialSGS).where(CredencialSGS.usuario == usuario.strip().lower()))).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Sin cuenta SGS registrada")
    try:
        pwd = _fernet().decrypt(c.sgs_password_enc.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise HTTPException(status_code=500, detail="No se pudo descifrar la cuenta (clave cambiada)")
    return {"sgs_login": c.sgs_login, "sgs_password": pwd}


class EstadoCuentaIn(BaseModel):
    usuario: str
    ok: bool
    error: Optional[str] = None


@router.post("/interno/sgs-cuenta/estado", dependencies=[Depends(_interno)], include_in_schema=False)
async def interno_estado_cuenta(body: EstadoCuentaIn, db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(CredencialSGS).where(CredencialSGS.usuario == body.usuario.strip().lower()))).scalar_one_or_none()
    if c:
        ahora = datetime.now(timezone.utc)
        c.ultimo_intento = ahora
        if body.ok:
            c.ultimo_ok = ahora
            c.ultimo_error = None
        else:
            c.ultimo_error = (body.error or "error")[:300]
        await db.commit()
    return {"ok": True}


# ═════════════════════════════════════════════════════════════════════════════════════════
# SGO7 línea de facturación (2026-10-10) — casos del Balance Integrado y órdenes «Mandar a liquidar»
#   Flujo: facturador consulta en Balance → caso (semáforo, estado) → si descuadra queda en espera y
#   el cron lo reconsulta solo → cuando cuadra genera el Excel → «Mandar a liquidar» → orden en el
#   panel del Liquidador → el Liquidador carga el Excel de la orden → resultado vuelve al caso.
# ═════════════════════════════════════════════════════════════════════════════════════════
import os as _os
import re as _re
from fastapi.responses import FileResponse

CASOS_DIR = "/var/www/sgo7/registros/casos"
RECONSULTA_HORAS = 2
ESTADOS_CASO = {
    "consultado": "Consultado", "descuadre": "Con descuadre", "espera_almacen": "En espera de almacén",
    "cuadra": "Cuadra", "enviado": "Enviado a liquidar", "en_liquidacion": "En liquidación",
    "liquidado": "Liquidado", "con_fallos": "Liquidado con fallos", "archivado": "Archivado",
}
ESTADOS_ORDEN = {"pendiente": "Pendiente", "en_liquidacion": "En liquidación", "liquidada": "Liquidada",
                 "con_fallos": "Con fallos", "cancelada": "Cancelada"}
_ESTADOS_FINALES = ("enviado", "en_liquidacion", "liquidado", "con_fallos")


def _ahora():
    return datetime.now(timezone.utc)


def _norm_planilla(v: str) -> str:
    partes = [p for p in _re.split(r"[,;+\s]+", str(v or "").strip()) if p]
    return "+".join(partes)


def _caso_out(c: CasoFacturacion, eventos=None) -> dict:
    res = c.resumen or {}
    d = {k: getattr(c, k) for k in (
        "id", "creado_en", "creado_por", "actualizado_en", "id_planilla", "proyecto_id", "parent_id", "pro_code",
        "proyecto_nombre", "lcl", "odm", "ticket", "zona", "empresa", "estado", "semaforo", "resumen", "consultas",
        "ultima_consulta_en", "ultima_consulta_por", "ultima_consulta_origen", "ultimo_error", "reconsulta_auto",
        "proxima_reconsulta", "nota", "excel_archivo", "excel_ruta", "excel_sha256", "excel_bytes", "excel_generado_en",
        "excel_generado_por", "fecha_ejecucion", "lideres", "rg11_archivo", "rg11_ruta", "orden_estado",
        "orden_enviada_en", "orden_enviada_por", "orden_nota", "orden_atendida_en", "orden_atendida_por",
        "orden_sesion", "orden_resultado", "orden_terminada_en")}
    d["estado_label"] = ESTADOS_CASO.get(c.estado, c.estado)
    d["orden_estado_label"] = ESTADOS_ORDEN.get(c.orden_estado or "", c.orden_estado)
    d["rojos"] = int(res.get("rojos") or 0)
    d["ok"] = int(res.get("ok") or 0)
    d["alertas"] = len(res.get("alertas") or []) + len(res.get("advertencias") or [])
    if eventos is not None:
        d["eventos"] = [{"id": e.id, "ts": e.ts, "usuario": e.usuario, "tipo": e.tipo, "detalle": e.detalle} for e in eventos]
    return d


def _evento(db: AsyncSession, caso: CasoFacturacion, tipo: str, usuario=None, detalle=None):
    db.add(CasoEvento(caso_id=caso.id, ts=_ahora(), usuario=usuario, tipo=tipo, detalle=detalle))


def _reg_fact(db: AsyncSession, usuario, rol, accion, detalle, sesion=None):
    db.add(RegistroSGO7(ts=_ahora(), modulo="facturacion", usuario=usuario, rol=rol, accion=accion,
                        estado="ok", detalle=detalle, sesion=sesion))


async def _caso(db: AsyncSession, cid: int) -> CasoFacturacion:
    c = (await db.execute(select(CasoFacturacion).where(CasoFacturacion.id == cid))).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Caso no encontrado")
    return c


async def _eventos(db: AsyncSession, cid: int):
    return (await db.execute(select(CasoEvento).where(CasoEvento.caso_id == cid)
                             .order_by(CasoEvento.ts, CasoEvento.id))).scalars().all()


async def require_facturacion(user=Depends(_usuario_activo)):
    from app.config import puede_facturacion
    if not puede_facturacion(user.rol, user.email):
        raise HTTPException(status_code=403, detail="Sin acceso a la línea de facturación")
    return user


async def require_ordenes(user=Depends(_usuario_activo)):
    from app.config import puede_facturacion, puede_liquidador
    if not (puede_facturacion(user.rol, user.email) or puede_liquidador(user.rol, user.email)):
        raise HTTPException(status_code=403, detail="Sin acceso a las órdenes de liquidación")
    return user


def _rol_de(user) -> str:
    return user.rol.value if hasattr(user.rol, "value") else str(user.rol)


class ConsultaIn(BaseModel):
    usuario: Optional[str] = None
    rol: Optional[str] = None
    origen: str = "manual"                   # manual (Balance) / auto (cron de reconsulta)
    sid: Optional[str] = None
    id_planilla: str
    proyecto_id: Optional[str] = None
    parent_id: Optional[int] = None
    pro_code: Optional[str] = None
    proyecto_nombre: Optional[str] = None
    lcl: Optional[str] = None
    odm: Optional[str] = None
    ticket: Optional[str] = None
    zona: Optional[str] = None
    empresa: Optional[str] = None
    resumen: dict = Field(default_factory=dict)
    advertencias: list = Field(default_factory=list)


class ExcelIn(BaseModel):
    usuario: Optional[str] = None
    rol: Optional[str] = None
    sid: Optional[str] = None
    excel_archivo: str
    excel_ruta: str
    excel_sha256: Optional[str] = None
    excel_bytes: Optional[int] = None
    fecha_ejecucion: Optional[str] = None
    lideres: Optional[list] = None
    rg11_archivo: Optional[str] = None
    rg11_ruta: Optional[str] = None


class OrdenIn(BaseModel):
    usuario: Optional[str] = None
    rol: Optional[str] = None
    nota: Optional[str] = None


class EstadoIn(BaseModel):
    usuario: Optional[str] = None
    rol: Optional[str] = None
    estado: Optional[str] = None            # espera_almacen / descuadre / archivado / reabrir
    nota: Optional[str] = None
    reconsulta_auto: Optional[bool] = None


class OrdenEstadoIn(BaseModel):
    usuario: Optional[str] = None
    estado: str                              # en_liquidacion / liquidada / con_fallos
    sesion: Optional[str] = None
    detalle: Optional[dict] = None


class ReconsultaErrorIn(BaseModel):
    error: str


def _aplicar_consulta(c: CasoFacturacion, body: ConsultaIn) -> dict:
    """Actualiza semáforo, contadores y estado del caso con el resultado de una consulta."""
    res = dict(body.resumen or {})
    rojos = int(res.get("descuadre") or 0) + int(res.get("error_clasif") or 0)
    criticos = [a for a in (res.get("alertas") or []) if str(a).strip().upper().startswith("CRITICO")]
    res["rojos"] = rojos
    res["advertencias"] = [dict(a) if isinstance(a, dict) else {"mensaje": str(a)} for a in (body.advertencias or [])]
    sem = "rojo" if rojos else ("ambar" if (res["advertencias"] or criticos) else "verde")
    previo = c.estado
    ahora = _ahora()
    c.semaforo, c.resumen = sem, res
    c.consultas = (c.consultas or 0) + 1
    c.ultima_consulta_en, c.ultima_consulta_por, c.ultima_consulta_origen = ahora, body.usuario, body.origen
    c.ultimo_error = None
    for k in ("pro_code", "proyecto_nombre", "lcl", "odm", "ticket", "zona", "empresa"):
        v = getattr(body, k)
        if v:
            setattr(c, k, str(v)[:200])
    if body.parent_id and not c.parent_id:
        c.parent_id = body.parent_id
    if body.proyecto_id and not c.proyecto_id:
        c.proyecto_id = body.proyecto_id
    if previo in _ESTADOS_FINALES or (previo == "archivado" and body.origen != "manual"):
        pass                                           # la orden manda; solo se refresca el semáforo
    elif rojos == 0:
        c.estado, c.reconsulta_auto, c.proxima_reconsulta = "cuadra", False, None
    else:
        c.estado = "espera_almacen" if previo == "espera_almacen" else "descuadre"
        if previo not in ("descuadre", "espera_almacen"):
            c.reconsulta_auto = True
        c.proxima_reconsulta = ahora + timedelta(hours=RECONSULTA_HORAS)
    return {"origen": body.origen, "semaforo": sem, "rojos": rojos, "ok": res.get("ok"), "alertas": len(res.get("alertas") or []),
            "advertencias": len(res["advertencias"]), "estado_previo": previo, "estado": c.estado, "sid": body.sid}


def _crear_orden(c: CasoFacturacion, usuario, nota):
    if not c.excel_ruta:
        raise HTTPException(status_code=400, detail="Primero genera el Excel del balance (fase final) y luego envíalo a liquidar")
    if c.orden_estado in ("pendiente", "en_liquidacion"):
        raise HTTPException(status_code=400, detail=f"Este caso ya tiene una orden {ESTADOS_ORDEN[c.orden_estado].lower()}")
    if c.semaforo != "verde" and not (nota or "").strip():
        raise HTTPException(status_code=400, detail="El balance no cuadra: escribe una observación para quien liquida")
    c.orden_estado, c.orden_enviada_en, c.orden_enviada_por, c.orden_nota = "pendiente", _ahora(), usuario, (nota or "").strip() or None
    c.orden_atendida_en = c.orden_atendida_por = c.orden_sesion = c.orden_resultado = c.orden_terminada_en = None
    c.estado, c.reconsulta_auto, c.proxima_reconsulta = "enviado", False, None


# ── canal interno (Balance, cron de reconsulta, Liquidador) ──────────────────────────────
@router.post("/interno/casos/consulta", dependencies=[Depends(_interno)], include_in_schema=False)
async def interno_consulta(body: ConsultaIn, db: AsyncSession = Depends(get_db)):
    idp = _norm_planilla(body.id_planilla)
    if not idp:
        raise HTTPException(status_code=400, detail="id_planilla vacío")
    stmt = select(CasoFacturacion).where(CasoFacturacion.id_planilla == idp)
    stmt = stmt.where(CasoFacturacion.parent_id == body.parent_id) if body.parent_id else stmt.where(CasoFacturacion.parent_id.is_(None))
    c = (await db.execute(stmt.order_by(desc(CasoFacturacion.id)))).scalars().first()
    nuevo = c is None
    if nuevo:
        c = CasoFacturacion(id_planilla=idp, proyecto_id=body.proyecto_id, parent_id=body.parent_id,
                            creado_en=_ahora(), creado_por=body.usuario, estado="consultado", consultas=0, reconsulta_auto=True)
        db.add(c)
        await db.flush()
    det = _aplicar_consulta(c, body)
    det["nuevo"] = nuevo
    _evento(db, c, "consulta", body.usuario, det)
    if det["estado_previo"] in ("descuadre", "espera_almacen") and c.estado == "cuadra":
        _evento(db, c, "cuadro", body.usuario, {"origen": body.origen, "consultas": c.consultas})
    _reg_fact(db, body.usuario, body.rol, "caso_consulta", dict(det, caso=c.id, id_planilla=idp, parent_id=c.parent_id), sesion=body.sid)
    await db.commit()
    await db.refresh(c)
    return _caso_out(c)


@router.post("/interno/casos/{cid}/excel", dependencies=[Depends(_interno)], include_in_schema=False)
async def interno_excel(cid: int, body: ExcelIn, db: AsyncSession = Depends(get_db)):
    c = await _caso(db, cid)
    c.excel_archivo, c.excel_ruta, c.excel_sha256, c.excel_bytes = body.excel_archivo[:300], body.excel_ruta[:400], body.excel_sha256, body.excel_bytes
    c.excel_generado_en, c.excel_generado_por = _ahora(), body.usuario
    c.fecha_ejecucion, c.lideres = (body.fecha_ejecucion or None), body.lideres
    c.rg11_archivo, c.rg11_ruta = body.rg11_archivo, body.rg11_ruta
    _evento(db, c, "excel_generado", body.usuario, {"archivo": body.excel_archivo, "sha256": body.excel_sha256,
                                                    "fecha_ejecucion": body.fecha_ejecucion, "lideres": body.lideres, "sid": body.sid})
    _reg_fact(db, body.usuario, body.rol, "caso_excel", {"caso": c.id, "archivo": body.excel_archivo, "sha256": body.excel_sha256,
                                                        "copia": body.excel_ruta, "fecha_ejecucion": body.fecha_ejecucion}, sesion=body.sid)
    await db.commit()
    await db.refresh(c)
    return _caso_out(c)


@router.post("/interno/casos/{cid}/orden", dependencies=[Depends(_interno)], include_in_schema=False)
async def interno_orden(cid: int, body: OrdenIn, db: AsyncSession = Depends(get_db)):
    c = await _caso(db, cid)
    _crear_orden(c, body.usuario, body.nota)
    _evento(db, c, "orden_enviada", body.usuario, {"nota": c.orden_nota, "archivo": c.excel_archivo, "semaforo": c.semaforo})
    _reg_fact(db, body.usuario, body.rol, "caso_orden_enviada", {"caso": c.id, "id_planilla": c.id_planilla, "proyecto": c.proyecto_nombre,
                                                                "archivo": c.excel_archivo, "sha256": c.excel_sha256, "nota": c.orden_nota})
    await db.commit()
    await db.refresh(c)
    return _caso_out(c)


def _aplicar_estado(c: CasoFacturacion, body: EstadoIn):
    cambios = {}
    if body.estado:
        if c.estado in ("en_liquidacion",):
            raise HTTPException(status_code=400, detail="El caso está en liquidación; espera el resultado")
        if body.estado == "espera_almacen":
            c.estado, c.reconsulta_auto, c.proxima_reconsulta = "espera_almacen", True, _ahora()
        elif body.estado == "descuadre":
            c.estado = "descuadre"
        elif body.estado == "archivado":
            c.estado, c.reconsulta_auto, c.proxima_reconsulta = "archivado", False, None
        elif body.estado == "reabrir":
            c.estado = "cuadra" if c.semaforo == "verde" else "descuadre"
            c.reconsulta_auto = c.estado == "descuadre"
            c.proxima_reconsulta = _ahora() if c.reconsulta_auto else None
        else:
            raise HTTPException(status_code=400, detail=f"Estado no permitido: {body.estado}")
        cambios["estado"] = c.estado
    if body.nota is not None:
        c.nota = body.nota.strip() or None
        cambios["nota"] = c.nota
    if body.reconsulta_auto is not None:
        c.reconsulta_auto = bool(body.reconsulta_auto)
        if c.reconsulta_auto and not c.proxima_reconsulta:
            c.proxima_reconsulta = _ahora()
        cambios["reconsulta_auto"] = c.reconsulta_auto
    return cambios


@router.post("/interno/casos/{cid}/estado", dependencies=[Depends(_interno)], include_in_schema=False)
async def interno_estado(cid: int, body: EstadoIn, db: AsyncSession = Depends(get_db)):
    c = await _caso(db, cid)
    cambios = _aplicar_estado(c, body)
    _evento(db, c, "estado_manual", body.usuario, cambios)
    _reg_fact(db, body.usuario, body.rol, "caso_estado", dict(cambios, caso=c.id))
    await db.commit()
    await db.refresh(c)
    return _caso_out(c)


@router.get("/interno/casos/reconsultar", dependencies=[Depends(_interno)], include_in_schema=False)
async def interno_reconsultar(limite: int = Query(8, ge=1, le=50), db: AsyncSession = Depends(get_db)):
    ahora = _ahora()
    stmt = (select(CasoFacturacion)
            .where(CasoFacturacion.reconsulta_auto.is_(True), CasoFacturacion.estado.in_(("descuadre", "espera_almacen")),
                   or_(CasoFacturacion.proxima_reconsulta.is_(None), CasoFacturacion.proxima_reconsulta <= ahora))
            .order_by(CasoFacturacion.proxima_reconsulta.nulls_first(), CasoFacturacion.id).limit(limite))
    casos = (await db.execute(stmt)).scalars().all()
    return [{"id": c.id, "id_planilla": c.id_planilla, "proyecto_id": c.proyecto_id, "parent_id": c.parent_id,
             "estado": c.estado, "consultas": c.consultas} for c in casos]


@router.post("/interno/casos/{cid}/reconsulta-error", dependencies=[Depends(_interno)], include_in_schema=False)
async def interno_reconsulta_error(cid: int, body: ReconsultaErrorIn, db: AsyncSession = Depends(get_db)):
    c = await _caso(db, cid)
    c.ultimo_error = body.error[:400]
    c.proxima_reconsulta = _ahora() + timedelta(hours=RECONSULTA_HORAS)
    _evento(db, c, "reconsulta_error", "sistema", {"error": c.ultimo_error})
    await db.commit()
    return {"ok": True}


@router.get("/interno/ordenes/{cid}", dependencies=[Depends(_interno)], include_in_schema=False)
async def interno_orden_ver(cid: int, db: AsyncSession = Depends(get_db)):
    c = await _caso(db, cid)
    if not c.orden_estado:
        raise HTTPException(status_code=404, detail="Este caso no tiene orden de liquidación")
    return _caso_out(c)


@router.post("/interno/ordenes/{cid}/estado", dependencies=[Depends(_interno)], include_in_schema=False)
async def interno_orden_estado(cid: int, body: OrdenEstadoIn, db: AsyncSession = Depends(get_db)):
    c = await _caso(db, cid)
    if not c.orden_estado:
        raise HTTPException(status_code=404, detail="Este caso no tiene orden de liquidación")
    ahora = _ahora()
    if body.estado == "en_liquidacion":
        c.orden_estado, c.orden_atendida_en, c.orden_atendida_por, c.orden_sesion = "en_liquidacion", ahora, body.usuario, body.sesion
        c.estado = "en_liquidacion"
    elif body.estado in ("liquidada", "con_fallos"):
        c.orden_estado, c.orden_terminada_en, c.orden_resultado = body.estado, ahora, body.detalle
        if body.sesion:
            c.orden_sesion = body.sesion
        c.estado = "liquidado" if body.estado == "liquidada" else "con_fallos"
    else:
        raise HTTPException(status_code=400, detail=f"Estado de orden no permitido: {body.estado}")
    _evento(db, c, "orden_" + body.estado, body.usuario, dict(body.detalle or {}, sesion=body.sesion))
    _reg_fact(db, body.usuario, None, "caso_orden_" + body.estado, dict(body.detalle or {}, caso=c.id, id_planilla=c.id_planilla), sesion=body.sesion)
    await db.commit()
    await db.refresh(c)
    return _caso_out(c)


# ── sesión SGO7: panel de facturación y panel del Liquidador ─────────────────────────────
@router.get("/casos")
async def listar_casos(db: AsyncSession = Depends(get_db), user=Depends(require_ordenes),
                       estado: Optional[str] = None, q: Optional[str] = None,
                       limit: int = Query(300, ge=1, le=1000), offset: int = Query(0, ge=0)):
    stmt = select(CasoFacturacion)
    if estado:
        stmt = stmt.where(CasoFacturacion.estado.in_([e for e in estado.split(",") if e]))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(CasoFacturacion.id_planilla.ilike(like), CasoFacturacion.proyecto_nombre.ilike(like),
                              CasoFacturacion.pro_code.ilike(like), CasoFacturacion.lcl.ilike(like),
                              CasoFacturacion.odm.ilike(like), CasoFacturacion.nota.ilike(like)))
    stmt = stmt.order_by(desc(CasoFacturacion.actualizado_en), desc(CasoFacturacion.id)).offset(offset).limit(limit)
    return [_caso_out(c) for c in (await db.execute(stmt)).scalars().all()]


@router.get("/casos/{cid}")
async def ver_caso(cid: int, db: AsyncSession = Depends(get_db), user=Depends(require_ordenes)):
    c = await _caso(db, cid)
    return _caso_out(c, await _eventos(db, cid))


@router.patch("/casos/{cid}")
async def editar_caso(cid: int, body: EstadoIn, db: AsyncSession = Depends(get_db), user=Depends(require_facturacion)):
    c = await _caso(db, cid)
    body.usuario, body.rol = user.email, _rol_de(user)
    cambios = _aplicar_estado(c, body)
    _evento(db, c, "estado_manual", user.email, cambios)
    _reg_fact(db, user.email, _rol_de(user), "caso_estado", dict(cambios, caso=c.id))
    await db.commit()
    await db.refresh(c)
    return _caso_out(c)


@router.post("/casos/{cid}/orden")
async def enviar_orden(cid: int, body: OrdenIn, db: AsyncSession = Depends(get_db), user=Depends(require_facturacion)):
    c = await _caso(db, cid)
    _crear_orden(c, user.email, body.nota)
    _evento(db, c, "orden_enviada", user.email, {"nota": c.orden_nota, "archivo": c.excel_archivo, "semaforo": c.semaforo, "desde": "panel"})
    _reg_fact(db, user.email, _rol_de(user), "caso_orden_enviada", {"caso": c.id, "id_planilla": c.id_planilla, "proyecto": c.proyecto_nombre,
                                                                   "archivo": c.excel_archivo, "sha256": c.excel_sha256, "nota": c.orden_nota})
    await db.commit()
    await db.refresh(c)
    return _caso_out(c)


@router.post("/casos/{cid}/orden/cancelar")
async def cancelar_orden(cid: int, db: AsyncSession = Depends(get_db), user=Depends(require_ordenes)):
    c = await _caso(db, cid)
    if c.orden_estado not in ("pendiente", "en_liquidacion", "con_fallos"):
        raise HTTPException(status_code=400, detail="No hay una orden activa que cancelar")
    c.orden_estado = "cancelada"
    c.estado = "cuadra" if c.semaforo == "verde" else "descuadre"
    _evento(db, c, "orden_cancelada", user.email, {})
    _reg_fact(db, user.email, _rol_de(user), "caso_orden_cancelada", {"caso": c.id, "id_planilla": c.id_planilla})
    await db.commit()
    await db.refresh(c)
    return _caso_out(c)


@router.get("/casos/{cid}/excel")
async def descargar_excel_caso(cid: int, db: AsyncSession = Depends(get_db), user=Depends(require_ordenes)):
    c = await _caso(db, cid)
    ruta = c.excel_ruta or ""
    if not ruta.startswith(CASOS_DIR + "/") or not _os.path.isfile(ruta):
        raise HTTPException(status_code=404, detail="El Excel de este caso no está disponible")
    return FileResponse(ruta, filename=c.excel_archivo or _os.path.basename(ruta),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.get("/ordenes")
async def listar_ordenes(db: AsyncSession = Depends(get_db), user=Depends(require_ordenes),
                         estado: Optional[str] = None, limit: int = Query(200, ge=1, le=1000)):
    stmt = select(CasoFacturacion).where(CasoFacturacion.orden_estado.is_not(None))
    if estado:
        stmt = stmt.where(CasoFacturacion.orden_estado.in_([e for e in estado.split(",") if e]))
    stmt = stmt.order_by(desc(CasoFacturacion.orden_enviada_en), desc(CasoFacturacion.id)).limit(limit)
    return [_caso_out(c) for c in (await db.execute(stmt)).scalars().all()]


# ── «Ver correo» del Control Mensual (CHANGELOG 2026-10-22): el correo guardado por Gestión General ──────────────
@router.get("/correo", dependencies=[Depends(require_dev)])
async def correo_por_entry(entry_id: str = Query(..., min_length=20, max_length=300),
                           db: AsyncSession = Depends(get_db)):
    """El correo (HTML ya saneado por Gestión General al ingresar) para un EntryID de Outlook. Si el mismo
    correo se guardó varias veces (varios proyectos), se devuelve el que tiene HTML."""
    from app.models.models import GestionEvento
    evs = (await db.execute(
        select(GestionEvento).where(GestionEvento.entry_id == entry_id)
        .order_by(desc(GestionEvento.ts)).limit(10))).scalars().all()
    if not evs:
        raise HTTPException(status_code=404, detail="Ese correo no está guardado en Gestión General")
    e = next((x for x in evs if x.html), evs[0])
    return dict(id=e.id, proyecto_id=e.proyecto_id, asunto=e.asunto, de=e.de, de_correo=e.de_correo, para=e.para,
                fecha=e.ts.isoformat() if e.ts else None, carpeta=e.carpeta, adjuntos=e.adjuntos or [],
                html=e.html or "", texto=e.texto or "", directo=e.directo)


# ── El mismo correo por el canal interno (CHANGELOG 2026-10-22): lo pide el Control Mensual para quien entra al módulo ──
@router.get("/interno/correo", dependencies=[Depends(_interno)], include_in_schema=False)
async def interno_correo_por_entry(entry_id: str = Query(..., min_length=20, max_length=300),
                                   db: AsyncSession = Depends(get_db)):
    """Como /correo, con el token interno. El Control Mensual solo lo pide para los correos que el Centro MO tiene
    registrados (busca el EntryID en su store); nginx niega /api/sgo7/interno/ desde fuera."""
    return await correo_por_entry(entry_id=entry_id, db=db)
