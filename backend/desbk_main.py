from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.db.database import engine, Base
from app.api import auth, programaciones, kpis, export, catalogos, bolsas, import_excel, feedback, produccion


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="Sabogal Platform API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://sabogal.arasakacode.com", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(programaciones.router, prefix="/api")
app.include_router(kpis.router, prefix="/api")
app.include_router(export.router, prefix="/api")
app.include_router(catalogos.router, prefix="/api")
app.include_router(bolsas.router, prefix="/api")
app.include_router(import_excel.router, prefix="/api")
app.include_router(feedback.router, prefix="/api")
app.include_router(produccion.router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "sabogal-platform"}
