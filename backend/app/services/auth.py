from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.models.models import Usuario, RolEnum

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Columnas que puede editar cada rol
CAMPO_PERMISOS = {
    RolEnum.ingeniero_zona: [
        "fecha", "lider", "tipo_grupo",
        "proceso", "subproceso", "proyecto_antecedente", "pms", "circuito",
        "lcl", "fecha_vigencia", "tipo_pdt", "num_pdt", "estado",
        "cd_afectados", "saidi", "hora", "hora_ab", "hora_cr",
        "municipio", "vereda", "alcance", "restricciones", "se_gestiona",
    ],
    RolEnum.programador: [
        "gestor_cliente", "supervisor", "valor_dia", "mo_proyectada",
        "mo_real", "mo_facturada", "valor_dejado_ejecutar", "valor_material",
        "se_gestiona",   # columna ROJA de la plantilla: todos los roles
    ],
    RolEnum.facturacion: [
        "estado_pdt", "notas", "num_proliquidado", "fecha_liquidacion_sgs",
        "estado_facturacion", "fecha_envio_facturacion", "ot",
        "estado_cumplimentacion", "inoperatividad",
        "inoperatividad_tipo", "observaciones_inoperatividad",
        "se_gestiona",   # columna ROJA de la plantilla: todos los roles
    ],
    RolEnum.supervisor: [],  # puede editar todo
    RolEnum.consulta: [],    # solo lectura (refuerzo: bloqueado en endpoints)
    RolEnum.directivo: [],   # solo lectura, ve todas las unidades
}


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


async def get_usuario_by_email(db: AsyncSession, email: str) -> Optional[Usuario]:
    result = await db.execute(select(Usuario).where(Usuario.email == email))
    return result.scalar_one_or_none()


async def authenticate_usuario(db: AsyncSession, email: str, password: str) -> Optional[Usuario]:
    user = await get_usuario_by_email(db, email)
    if not user or not verify_password(password, user.hashed_password):
        return None
    return user


async def get_current_user(token: str, db: AsyncSession) -> Optional[Usuario]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: int = payload.get("sub")
        if user_id is None:
            return None
    except JWTError:
        return None
    result = await db.execute(select(Usuario).where(Usuario.id == int(user_id)))
    return result.scalar_one_or_none()
