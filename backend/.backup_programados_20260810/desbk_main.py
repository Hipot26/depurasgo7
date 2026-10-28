from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.db.database import engine, Base
from app.api import (auth, programaciones, kpis, export, catalogos, bolsas, import_excel,
                     feedback, produccion, mantilla, ans, auditoria, moes_bolsas, sgs)
from app.api.deps import (bloquear_restringidos, require_ans, bloquear_ans_escritura,
                          require_moes, bloquear_moes, bloquear_cumplimentador, audit_context)
from app.services import auditoria as _auditoria_svc   # registra el listener before_flush al importar
from fastapi import Depends


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
    dependencies=[Depends(audit_context)],   # contexto de petición para la auditoría global
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://sabogal.arasakacode.com", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
# Bolsas generales: las usan cliente/ans y los roles internos, pero NO los usuarios de la unidad
# MOES (Agobardo / CHOCONTA), que tienen su propio módulo Bolsas MOES, ni el cumplimentador
# (solo ANS).
app.include_router(bolsas.router, prefix="/api",
                   dependencies=[Depends(bloquear_moes), Depends(bloquear_cumplimentador)])
app.include_router(feedback.router, prefix="/api")

# Resto de módulos: bloqueados para las cuentas restringidas (cliente solo Bolsas; ans solo Bolsas+ANS)
_restringidos = [Depends(bloquear_restringidos)]
app.include_router(programaciones.router, prefix="/api", dependencies=_restringidos)
app.include_router(kpis.router, prefix="/api", dependencies=_restringidos)
app.include_router(export.router, prefix="/api", dependencies=_restringidos)
app.include_router(catalogos.router, prefix="/api", dependencies=_restringidos)
app.include_router(import_excel.router, prefix="/api", dependencies=_restringidos)
app.include_router(produccion.router, prefix="/api", dependencies=_restringidos)
app.include_router(mantilla.router, prefix="/api", dependencies=_restringidos)

# Módulo ANS: accesible al desarrollador y a la cuenta ans (Paola). Se monta DOS veces con
# el MISMO código: /api/ans = datos reales; /api/prueba/ans = sandbox de experimentación
# (datos 100% aislados por la columna `entorno`, ver ans.py / ans_ctx.py).
app.include_router(ans.router, prefix="/api",
                   dependencies=[Depends(require_ans), Depends(bloquear_ans_escritura),
                                 Depends(ans.usar_entorno_real)])
# El sandbox NO se le abre al cumplimentador: su único trabajo es la columna AQ del ANS
# real, y llenarla en el de prueba creyendo que es el real perdería su trabajo.
app.include_router(ans.router, prefix="/api/prueba",
                   dependencies=[Depends(require_ans), Depends(bloquear_ans_escritura),
                                 Depends(bloquear_cumplimentador),
                                 Depends(ans.usar_entorno_prueba)])

# Módulo Bolsas MOES: desarrollador + cuenta moes (Agobardo) + directivo (jefe superior)
app.include_router(moes_bolsas.router, prefix="/api", dependencies=[Depends(require_moes)])

# Auditoría global (historial de toda acción de todo usuario): dev + directivo. El gate va dentro
# del router para que cada endpoint dependa de require_auditoria.
app.include_router(auditoria.router, prefix="/api")

# Relación con SGS (autocompletar MO real liquidada): exclusivo del desarrollador (cuenta Sergio).
# El gate require_dev va dentro de cada endpoint del router.
app.include_router(sgs.router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "sabogal-platform"}
