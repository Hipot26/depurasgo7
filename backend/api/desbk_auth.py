from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.database import get_db
from app.schemas.schemas import LoginRequest, Token, UsuarioCreate, UsuarioOut
from app.services.auth import authenticate_usuario, create_access_token, hash_password
from app.models.models import Usuario
from app.api.deps import get_current_active_user, require_roles
from app.models.models import RolEnum

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=Token)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = await authenticate_usuario(db, req.email, req.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales incorrectas")
    token = create_access_token({"sub": str(user.id)})
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
