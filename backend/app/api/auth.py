from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from urllib.parse import quote
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.database import get_db
from app.schemas.schemas import LoginRequest, Token, UsuarioCreate, UsuarioOut
from app.services.auth import authenticate_usuario, create_access_token, hash_password, get_current_user
from app.config import settings
from app.models.models import Usuario
from app.api.deps import get_current_active_user, require_roles
from app.models.models import RolEnum

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=Token)
async def login(req: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    user = await authenticate_usuario(db, req.email, req.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales incorrectas")
    token = create_access_token({"sub": str(user.id)})
    _sgo7_set_cookie(response, token)   # SGO7: cookie para la puerta única de los módulos
    return Token(access_token=token, token_type="bearer", usuario=UsuarioOut.model_validate(user))


@router.get("/me", response_model=UsuarioOut)
async def me(current_user: Usuario = Depends(get_current_active_user)):
    return current_user


@router.post("/usuarios", response_model=UsuarioOut, dependencies=[Depends(require_roles(RolEnum.supervisor))])
async def crear_usuario(req: UsuarioCreate, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    existing = await db.execute(select(Usuario).where(Usuario.email == req.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email ya registrado")
    user = Usuario(
        nombre=req.nombre,
        email=req.email,
        hashed_password=hash_password(req.password),
        rol=req.rol,
        zona=req.zona,
        unidad=req.unidad or "SABOGAL",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/usuarios", response_model=list[UsuarioOut], dependencies=[Depends(require_roles(RolEnum.supervisor))])
async def listar_usuarios(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    result = await db.execute(select(Usuario).where(Usuario.activo == True))
    return result.scalars().all()


# ── SGO7: puerta única para los módulos (Balance, Materiales, Verificador, Devoluciones,
#    Liquidador). nginx llama a /auth/verify (auth_request) ANTES de pasar cualquier
#    petición a un módulo; sin sesión válida responde 401 y nginx manda al login de SGO7.
#    La sesión viaja en la cookie HttpOnly `sgo7_session` que /auth/login deja al entrar.
SGO7_COOKIE = "sgo7_session"
# Roles de SGO7 autorizados por módulo. Ampliar aquí cuando se abra a otras áreas
# (p. ej. {"desarrollador", "supervisor"} o un rol de almacén para Devoluciones).
SGO7_MODULOS: dict[str, set[str]] = {
    "balance":      {"desarrollador", "facturacion"},   # línea de facturación desde 2026-10-10
    "materiales":   {"desarrollador"},
    "verificador":  {"desarrollador"},
    "devoluciones": {"desarrollador"},
    "liquidador2":  {"desarrollador"},
    "centro-mo":    {"desarrollador"},   # Centro MO (correo → planilla MO), 2026-10-14
}


def _sgo7_set_cookie(response: Response, token: str) -> None:
    response.set_cookie(SGO7_COOKIE, token, max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                        path="/", httponly=True, secure=True, samesite="lax")


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SGO7_COOKIE, path="/")
    return {"ok": True}


@router.get("/verify", include_in_schema=False)
async def verify(request: Request, modulo: str = "", db: AsyncSession = Depends(get_db)):
    token = request.cookies.get(SGO7_COOKIE)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
    user = await get_current_user(token, db) if token else None
    if not user or not user.activo:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sin sesión SGO7")
    rol = user.rol.value if hasattr(user.rol, "value") else str(user.rol)
    from app.config import puede_liquidador as _puede_liq
    from app.config import puede_control as _puede_ctrl
    permitido = (rol in SGO7_MODULOS.get(modulo, set()) or (modulo == "liquidador2" and _puede_liq(rol, user.email))
                 or (modulo == "verificador" and _puede_ctrl(rol, user.email)))   # Control Mensual por cuenta (CHANGELOG 2026-10-22)
    if not permitido:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Módulo no autorizado para este rol")
    return Response(status_code=204, headers={"X-SGO7-User": user.email, "X-SGO7-Rol": rol,
                                               "X-SGO7-Nombre": quote(user.nombre or "")})
