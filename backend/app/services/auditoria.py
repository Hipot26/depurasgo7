"""Auditoría global e independiente — captura TODA acción de TODO usuario (crear/editar/borrar)
en todos los módulos, sin instrumentar cada endpoint.

Cómo funciona:
  - `get_current_active_user` (deps.py) y una middleware (main.py) dejan el contexto de la
    petición en `contextvars` (usuario + request_id + ruta/método/ip).
  - El listener `before_flush` sobre la Session lee ese contexto y, por cada objeto de un modelo
    auditado, agrega filas `Auditoria` a la misma sesión (sin I/O async → seguro).
  - Carga masiva = N objetos en un flush → N filas con el MISMO request_id (la UI las agrupa).

Seguridad: el listener NUNCA propaga excepciones (un bug de auditoría no puede romper un guardado)
y se puede apagar con `settings.AUDIT_ENABLED = false` sin redeploy.
"""
import json
import contextvars
from datetime import datetime, date, time
from decimal import Decimal

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session
from sqlalchemy.event import listens_for

from app.config import settings
from app.models.models import Auditoria

# ── Contexto de la petición (por task async) ─────────────────────────────────────
_ctx_user = contextvars.ContextVar("audit_user", default=None)        # dict id/nombre/rol/unidad
_ctx_req = contextvars.ContextVar("audit_req", default=None)          # dict request_id/ruta/metodo/ip


def set_audit_user(user):
    """Snapshot mínimo del usuario actual (lo llama get_current_active_user)."""
    try:
        _ctx_user.set({"id": user.id, "nombre": user.nombre,
                       "rol": getattr(user.rol, "value", user.rol), "unidad": user.unidad})
    except Exception:
        pass


def set_audit_request(request_id, ruta, metodo, ip):
    _ctx_req.set({"request_id": request_id, "ruta": ruta, "metodo": metodo, "ip": ip})


# ── Qué se audita ────────────────────────────────────────────────────────────────
# tabla → nombre de módulo amable (solo estas tablas se auditan).
TABLAS_AUDITADAS = {
    "programaciones": "Programación",
    "bolsas": "Bolsas",
    "lideres": "Catálogos",
    "proyectos": "Catálogos",
    "usuarios": "Usuarios",
    "dimension_cuadrillas": "Cuadrillas",
    "integrantes_cuadrilla": "Cuadrillas",
    "ans_registros": "ANS",
}
# Campos que NUNCA se guardan en la auditoría (sensibles o ruido).
CAMPOS_OMITIDOS = {"hashed_password"}
_MAX = 4000   # recorta valores muy largos


def _str(v):
    if v is None:
        return None
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False, default=str)
    s = str(v)
    return s[:_MAX]


def _snapshot(obj) -> str:
    """JSON con todas las columnas del objeto (respaldo), sin campos omitidos."""
    out = {}
    for c in sa_inspect(obj).mapper.column_attrs:
        if c.key in CAMPOS_OMITIDOS:
            continue
        out[c.key] = _str(getattr(obj, c.key, None))
    return json.dumps(out, ensure_ascii=False, default=str)


def _pk(obj):
    try:
        ident = sa_inspect(obj).identity
        if ident:
            return ident[0]
        return getattr(obj, "id", None)
    except Exception:
        return getattr(obj, "id", None)


def _base_kwargs(tabla):
    u = _ctx_user.get() or {}
    r = _ctx_req.get() or {}
    return dict(
        usuario_id=u.get("id"), usuario_nombre=u.get("nombre"),
        usuario_rol=u.get("rol"), unidad=u.get("unidad"),
        modulo=TABLAS_AUDITADAS.get(tabla), tabla=tabla,
        request_id=r.get("request_id"), ruta=r.get("ruta"),
        metodo=r.get("metodo"), ip=r.get("ip"),
    )


def _entradas_para(obj, accion):
    """Lista de objetos Auditoria para un objeto, según la colección de la que viene
    (`crear`/`editar`/`borrar`). Vacía si la tabla no se audita o no hubo cambios reales.
    Se llama dentro de before_flush, donde el snapshot y el history aún están disponibles."""
    tabla = getattr(obj, "__tablename__", None)
    if tabla not in TABLAS_AUDITADAS:
        return []
    state = sa_inspect(obj)
    base = _base_kwargs(tabla)

    if accion == "crear":
        # registro_id puede ser None (autoincrement aún sin asignar); el snapshot es el respaldo.
        return [Auditoria(accion="crear", registro_id=_pk(obj), datos=_snapshot(obj), **base)]
    if accion == "borrar":
        return [Auditoria(accion="borrar", registro_id=_pk(obj), datos=_snapshot(obj), **base)]

    # editar: una fila por campo realmente cambiado
    filas = []
    for attr in state.mapper.column_attrs:
        if attr.key in CAMPOS_OMITIDOS:
            continue
        hist = state.attrs[attr.key].history
        if not hist.has_changes():
            continue
        old = hist.deleted[0] if hist.deleted else None
        new = hist.added[0] if hist.added else None
        if _str(old) == _str(new):
            continue
        filas.append(Auditoria(accion="editar", registro_id=_pk(obj), campo=attr.key,
                               valor_anterior=_str(old), valor_nuevo=_str(new), **base))
    return filas


@listens_for(Session, "before_flush")
def _auditar_before_flush(session, flush_context, instances):
    if not getattr(settings, "AUDIT_ENABLED", True):
        return
    try:
        nuevas = []
        for obj in session.new:
            if not isinstance(obj, Auditoria):
                nuevas.extend(_entradas_para(obj, "crear"))
        for obj in session.dirty:
            if not isinstance(obj, Auditoria):
                nuevas.extend(_entradas_para(obj, "editar"))
        for obj in session.deleted:
            if not isinstance(obj, Auditoria):
                nuevas.extend(_entradas_para(obj, "borrar"))
        for a in nuevas:
            session.add(a)
    except Exception:
        # La auditoría jamás debe romper una escritura del negocio.
        pass


def registrar_listener():
    """No-op explícito para forzar el import del módulo desde main.py (registra el listener)."""
    return True
