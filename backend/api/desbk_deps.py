from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.database import get_db
from app.services.auth import get_current_user
from app.models.models import Usuario, RolEnum, UNIDAD_DEFAULT

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# Roles que ven TODAS las unidades (pueden filtrar por ?unidad=)
ROLES_GLOBALES = {RolEnum.directivo, RolEnum.desarrollador}
# Roles de solo lectura (no crean, editan ni borran)
ROLES_SOLO_LECTURA = {RolEnum.consulta, RolEnum.directivo, RolEnum.cliente}


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
