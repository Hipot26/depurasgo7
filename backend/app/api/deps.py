import uuid
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.database import get_db
from app.services.auth import get_current_user
from app.models.models import Usuario, RolEnum, UNIDAD_DEFAULT

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# Roles que ven TODAS las unidades (pueden filtrar por ?unidad=).
# Los coordinadores (rol supervisor: programados y no programados) también cambian de unidad,
# como el dev. Las escrituras siguen acotadas por los gates propios de cada módulo (SGS solo
# para el supervisor de la unidad SABOGAL).
ROLES_GLOBALES = {RolEnum.directivo, RolEnum.desarrollador, RolEnum.supervisor}
# Roles de solo lectura (no crean, editan ni borran)
ROLES_SOLO_LECTURA = {RolEnum.consulta, RolEnum.directivo, RolEnum.cliente}


async def audit_context(request: Request):
    """Dependencia GLOBAL: deja el contexto de la petición (request_id + ruta/método/ip) en
    contextvars para la auditoría. Corre en la task del endpoint → propaga al flush. El mismo
    request_id agrupa todas las filas de una carga masiva en una sola 'operación'."""
    try:
        from app.services.auditoria import set_audit_request
        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else None)
        set_audit_request(str(uuid.uuid4()), request.url.path, request.method, ip)
    except Exception:
        pass


def unidad_de(user: Usuario) -> str:
    """Unidad a la que pertenece el usuario (default SABOGAL)."""
    return user.unidad or UNIDAD_DEFAULT


def unidad_filtros(user: Usuario, unidad: str | None = None) -> list:
    """Filtros SQLAlchemy de scoping por unidad para Programacion.
    Roles globales ven todo (o la unidad pedida); el resto, solo la suya."""
    from app.models.models import Programacion
    if user.rol in ROLES_GLOBALES:
        return [Programacion.unidad == unidad] if unidad else []
    return [Programacion.unidad == unidad_de(user)]


def assert_puede_escribir(user: Usuario):
    """Bloquea escritura a roles de solo lectura."""
    if user.rol in ROLES_SOLO_LECTURA:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Su rol es de solo lectura")


async def get_current_active_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    user = await get_current_user(token, db)
    if not user or not user.activo:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No autenticado")
    # Deja el usuario en el contexto para la auditoría global (punto único de toda petición auth)
    try:
        from app.services.auditoria import set_audit_user
        set_audit_user(user)
    except Exception:
        pass
    return user


def require_roles(*roles: RolEnum):
    async def checker(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
        if current_user.rol not in roles and current_user.rol != RolEnum.supervisor:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sin permiso")
        return current_user
    return checker


async def require_dev(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """Buzón privado del desarrollador — exclusivo del rol desarrollador."""
    if current_user.rol != RolEnum.desarrollador:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Buzón privado del desarrollador")
    return current_user


async def require_auditoria_wa(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """Tab «Auditoría IA» del ANS (analista automático de WhatsApp): el dev y las cuentas
    de config.USUARIOS_AUDITORIA_WA. Quien entra, opera."""
    from app.config import puede_auditoria_wa
    if not puede_auditoria_wa(current_user.rol, current_user.email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="La Auditoría IA es del desarrollador y el directivo")
    return current_user


async def require_correos_ans(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """Tab «Correos ANS»: el dev y las cuentas de config.USUARIOS_CORREOS_ANS.
    Quien entra, opera (adjuntar, analizar, aprobar 3.2 B)."""
    from app.config import puede_correos_ans
    if not puede_correos_ans(current_user.rol, current_user.email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Los Correos ANS son del desarrollador y el directivo")
    return current_user


async def require_sgs_access(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """Relación con SGS: el desarrollador o el supervisor de la unidad SABOGAL (coordinador
    de programados). El supervisor de otra unidad (CHOCONTA) queda fuera."""
    if current_user.rol == RolEnum.desarrollador:
        return current_user
    if current_user.rol == RolEnum.supervisor and (current_user.unidad or UNIDAD_DEFAULT) == UNIDAD_DEFAULT:
        return current_user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sin acceso a la Relación con SGS")


async def require_sgs_exporte(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """Módulo SGS Exporte (solo consulta): el dev y las cuentas de config.USUARIOS_SGS_EXPORTE.
    No abre la Relación con SGS: relacionar/aplicar siguen en require_sgs_access."""
    from app.config import puede_sgs_exporte
    if not puede_sgs_exporte(current_user.rol, current_user.email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sin acceso a SGS Exporte")
    return current_user


async def bloquear_cliente(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """La cuenta cliente solo accede a Bolsas: se bloquea en el resto de módulos."""
    if current_user.rol == RolEnum.cliente:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cuenta de solo consulta de Bolsas")
    return current_user


# Cuentas restringidas a unos pocos módulos: cliente (solo Bolsas), ans (Bolsas + ANS)
# y cumplimentador (solo ANS).
ROLES_RESTRINGIDOS = {RolEnum.cliente, RolEnum.ans, RolEnum.cumplimentador}

# Unidad cuyas "bolsas" son las MOES (no programados / Pisos Móviles). Sus usuarios (p. ej. su
# supervisor) ven el módulo Bolsas MOES en vez de las bolsas generales, pero conservan el resto
# de módulos de su unidad (programación, matriz, etc.).
UNIDAD_MOES = "CHOCONTA"


async def bloquear_restringidos(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """Bloquea las cuentas restringidas (cliente, ans, cumplimentador) en los módulos de
    programación. La cuenta ans ve ANS + Bolsas; la cliente solo Bolsas; el cumplimentador solo ANS."""
    if current_user.rol in ROLES_RESTRINGIDOS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Su cuenta no tiene acceso a este módulo")
    return current_user


async def bloquear_cumplimentador(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """El cumplimentador SOLO ve el módulo ANS real. Se usa donde bloquear_restringidos no
    aplica porque cliente/ans sí entran: las Bolsas generales y el sandbox /api/prueba."""
    if current_user.rol == RolEnum.cumplimentador:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Su cuenta solo tiene acceso al módulo ANS")
    return current_user


# Cuentas concretas (por login) con acceso PLENO al módulo ANS, igual que la cuenta `ans`
# (pueden ver, editar y subir archivos) aunque su rol no lo incluya. P. ej. el directivo que
# supervisa el ANS: en el RESTO del sistema sigue siendo solo lectura, pero en ANS es editor
# completo. No se abre por rol para no dárselo a todos los directivos.
# La lista y la regla viven en config.py (única fuente de verdad: el frontend la recibe en el
# campo `puede_ans` del usuario, así no hay listas duplicadas que se desincronicen).
from app.config import (USUARIOS_ANS_EXTRA, puede_ans as _puede_ans,   # noqa: E402
                        es_apoyo_ans as _es_apoyo_ans, es_ing_zona_ans as _es_ing_zona_ans)


async def require_ans(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """Módulo ANS — accesible al desarrollador, a la cuenta ans y a las cuentas autorizadas
    en USUARIOS_ANS_EXTRA (el directivo que supervisa y quienes diligencian: programadoras y nodos)."""
    if not _puede_ans(current_user.rol, current_user.email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sin acceso al módulo ANS")
    return current_user


async def bloquear_ans_escritura(
    request: Request,
    current_user: Usuario = Depends(get_current_active_user),
) -> Usuario:
    """En ANS, las cuentas de solo lectura (directivo/consulta/cliente) pueden consultar pero
    NO mutar. EXCEPCIÓN: las cuentas de USUARIOS_ANS_EXTRA tienen ANS pleno, como la cuenta
    `ans`, y sí pueden importar/editar/borrar. Bloquea escrituras (POST/PATCH/PUT/DELETE)
    solo para las de solo lectura que NO estén en esa lista."""
    if (request.method in ("POST", "PATCH", "PUT", "DELETE")
            and current_user.rol in ROLES_SOLO_LECTURA
            and current_user.email not in USUARIOS_ANS_EXTRA):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Su cuenta es de solo lectura en el módulo ANS")
    # El cumplimentador diligencia CELDAS y nada más: solo el PATCH de un registro
    # (/api/ans/{id}). Nada de imports, borrar periodo, evidencias, líderes ni catálogos.
    if (current_user.rol == RolEnum.cumplimentador
            and request.method in ("POST", "PATCH", "PUT", "DELETE")
            and not (request.method == "PATCH"
                     and request.url.path.rstrip("/").rsplit("/", 1)[-1].isdigit())):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Su cuenta solo puede diligenciar celdas del ANS")
    # El profesional de apoyo aporta el soporte de SUS casos (celdas y evidencias), pero no
    # carga los STM del periodo ni borra en bloque: eso sigue siendo de la programadora. Los
    # ingenieros de zona (cuentas por zona) ven el cálculo completo con el mismo límite.
    if ((_es_apoyo_ans(current_user.rol, current_user.email)
         or _es_ing_zona_ans(current_user.email)) and _es_carga_o_borrado_ans(request)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Su cuenta no carga archivos ni borra periodos del ANS")
    return current_user


def _es_carga_o_borrado_ans(request: Request) -> bool:
    """Las operaciones «de dueño del periodo» dentro del ANS: subir los STM / la programación
    de los 20', borrar un periodo entero o vaciar el sandbox. Se identifican por ruta porque
    los endpoints no repiten el gate (el router se monta con las dependencias en main.py)."""
    ruta = request.url.path.rstrip("/")
    if request.method == "POST":
        return ruta.endswith("/import") or ruta.endswith("/prog20") or ruta.endswith("/cargar")
    if request.method == "DELETE":
        return "/periodo/" in ruta or ruta.endswith("/reset")
    return False


def _es_unidad_moes(user: Usuario) -> bool:
    return (user.unidad or UNIDAD_DEFAULT) == UNIDAD_MOES


def _puede_moes(user: Usuario) -> bool:
    """Acceso a Bolsas MOES: dev/directivo (ven todas las unidades) o usuarios de la
    unidad MOES (CHOCONTA)."""
    return user.rol in ROLES_GLOBALES or _es_unidad_moes(user)


async def bloquear_moes(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """Los usuarios de la unidad MOES (CHOCONTA) NO ven las bolsas generales: usan Bolsas MOES.
    Dev/directivo sí (supervisan todas las unidades)."""
    if current_user.rol not in ROLES_GLOBALES and _es_unidad_moes(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Su unidad usa el módulo Bolsas MOES")
    return current_user


async def require_moes(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """Módulo Bolsas MOES — dev/directivo o usuarios de la unidad MOES (CHOCONTA)."""
    if not _puede_moes(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sin acceso al módulo Bolsas MOES")
    return current_user


async def require_auditoria(current_user: Usuario = Depends(get_current_active_user)) -> Usuario:
    """Panel de auditoría global — desarrollador y directivo (jefe superior que ve todo)."""
    if current_user.rol not in (RolEnum.desarrollador, RolEnum.directivo):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sin acceso a la auditoría")
    return current_user
