from datetime import datetime, date
from typing import Optional
from sqlalchemy import (
    String, Integer, BigInteger, Date, DateTime, Float,
    ForeignKey, Enum, Text, Boolean, func, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.database import Base
from app.db.ans_ctx import current_entorno
import enum


def _col_entorno():
    """Columna discriminadora real/prueba de los modelos ANS. El default lee el
    ContextVar del request (ans_ctx) → los inserts del router de prueba quedan en
    'prueba' y los del real en 'real', sin tocar el código de los endpoints."""
    return mapped_column(String(10), default=current_entorno, server_default="real", index=True)


class ZonaEnum(str, enum.Enum):
    ALMEIDAS = "ALMEIDAS"
    GUAVIO = "GUAVIO"
    UBATE = "UBATE"
    LVTLC = "LV&TLC"


class RolEnum(str, enum.Enum):
    supervisor = "supervisor"
    ingeniero_zona = "ingeniero_zona"
    programador = "programador"
    facturacion = "facturacion"
    desarrollador = "desarrollador"
    consulta = "consulta"        # solo lectura (ve dashboard + grilla, no edita)
    directivo = "directivo"      # ve todas las unidades (jefe superior)
    cliente = "cliente"          # cuenta cliente: solo lectura y solo la pestaña de Bolsas
    ans = "ans"                  # cuenta ANS (Paola): solo ve los módulos ANS y Bolsas
    moes = "moes"                # cuenta MOES (Agobardo): solo ve el módulo Bolsas MOES
    cumplimentador = "cumplimentador"  # cumplimentadores Dominion (3.3): solo ven el módulo ANS


# Unidades operativas aisladas (cada una con sus zonas, líderes y datos).
# Por defecto todo lo existente pertenece a SABOGAL.
UNIDAD_DEFAULT = "SABOGAL"


class TipoGrupoEnum(str, enum.Enum):
    CAMP5 = "CAMP5"
    CAMP3 = "CAMP3"
    ARRIME = "ARRIME"
    GRUA = "GRUA"
    ESTACA = "ESTACA"
    LVL3 = "LVL3"
    MIE = "MIE"


class EstadoEnum(str, enum.Enum):
    PROGRAMADO = "PROGRAMADO"
    EJECUTADO = "EJECUTADO"
    PARCIAL = "PARCIAL"
    CANCELADO = "CANCELADO"
    REPROGRAMADO = "REPROGRAMADO"


class EstadoPDTEnum(str, enum.Enum):
    PENDIENTE = "PENDIENTE"
    ENTREGADO_FACTURACION = "ENTREGADO_FACTURACION"
    LIQUIDADO_SGS = "LIQUIDADO_SGS"
    RECHAZADO = "RECHAZADO"


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(200))
    rol: Mapped[RolEnum] = mapped_column(Enum(RolEnum))
    zona: Mapped[Optional[ZonaEnum]] = mapped_column(Enum(ZonaEnum), nullable=True)
    unidad: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, server_default="SABOGAL")
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class Lider(Base):
    __tablename__ = "lideres"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(150))
    zona: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tipo_grupo: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    unidad: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, server_default="SABOGAL")
    # Código de cuadrilla Mantilla (AL_MOE2_1…); puede cambiar de mes a mes,
    # el import de Mantilla lo mantiene al día con lo que traiga el archivo.
    codigo: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)


class Proyecto(Base):
    __tablename__ = "proyectos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(300))
    pms: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    lcl: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    circuito: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    fecha_vigencia: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    zona: Mapped[Optional[ZonaEnum]] = mapped_column(Enum(ZonaEnum), nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)


class Programacion(Base):
    __tablename__ = "programaciones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Unidad operativa (aislamiento Sabogal / Agobardo-Chocontá …)
    unidad: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, server_default="SABOGAL", index=True)

    # Identificación
    zona: Mapped[ZonaEnum] = mapped_column(Enum(ZonaEnum), index=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    lider: Mapped[str] = mapped_column(String(100))
    tipo_grupo: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # texto libre: CAMP5… / MOE2, TAI, SUP.

    # Datos técnicos - llenan ingenieros de zona (azul oscuro)
    proceso: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    subproceso: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    proyecto_antecedente: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    pms: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    circuito: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    lcl: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    fecha_vigencia: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    tipo_pdt: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    num_pdt: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    estado: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    cd_afectados: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    hora: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    municipio: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    vereda: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    alcance: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Datos programadores - llenan programadores (verde)
    gestor_cliente: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    supervisor: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    valor_dia: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mo_proyectada: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mo_real: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    valor_dejado_ejecutar: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    valor_material: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Datos facturación/SGS - llenan facturación (azul claro)
    estado_pdt: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    notas: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    num_proliquidado: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    fecha_liquidacion_sgs: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    estado_facturacion: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    fecha_envio_facturacion: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    ot: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    estado_cumplimentacion: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    inoperatividad: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    detalle_mantilla: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON: desglose por actividad (Agobardo)

    # Auditoría
    creado_por: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("usuarios.id"), nullable=True)
    actualizado_por: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("usuarios.id"), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class Feedback(Base):
    """Peticiones de mejora / reportes enviados por cualquier usuario.
    Llegan al buzón privado del desarrollador para valoración."""
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("usuarios.id"), nullable=True)
    autor_nombre: Mapped[str] = mapped_column(String(150))
    autor_rol: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    autor_zona: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    tipo: Mapped[str] = mapped_column(String(40))           # Mejora, Error, Idea, Otro
    titulo: Mapped[str] = mapped_column(String(200))
    descripcion: Mapped[str] = mapped_column(Text)
    url_pagina: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    adjuntos: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON: lista de data-URIs (capturas)
    contexto: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON: metadatos técnicos ocultos (solo dev)

    estado: Mapped[str] = mapped_column(String(30), default="nueva")      # nueva, en_revision, planeada, implementada, rechazada
    prioridad: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # baja, media, alta
    respuesta_dev: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # legado: 1ª respuesta antes de los hilos

    # Hilo (estilo Slack): última vez que cada lado leyó la conversación → para el indicador "respuesta nueva".
    leido_autor_en: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    leido_dev_en: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class FeedbackMensaje(Base):
    """Mensaje dentro del hilo de una solicitud (conversación ida y vuelta autor↔dev).
    La solicitud (Feedback) es la raíz del hilo; aquí van las respuestas con autor/fecha
    y capturas opcionales. `es_dev` marca de qué lado vino para pintar el hilo."""
    __tablename__ = "feedback_mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feedback_id: Mapped[int] = mapped_column(Integer, ForeignKey("feedback.id", ondelete="CASCADE"), index=True)
    autor_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("usuarios.id"), nullable=True)
    autor_nombre: Mapped[str] = mapped_column(String(150))
    autor_rol: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    es_dev: Mapped[bool] = mapped_column(Boolean, default=False)
    cuerpo: Mapped[str] = mapped_column(Text)
    adjuntos: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON: lista de data-URIs (capturas)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    editado_en: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # se marca «(editado)»


class Dispositivo(Base):
    """Control de acceso por dispositivo: cada navegador genera un device_id.
    Desconocidos quedan 'pendiente' hasta que el desarrollador los apruebe."""
    __tablename__ = "dispositivos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    estado: Mapped[str] = mapped_column(String(20), default="pendiente")  # pendiente, aprobado, rechazado
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class Bolsa(Base):
    """Caso de 'bolsa' (TUTELA, PQR, IPPE, CUN100, AMP, NNDD, TLC…).
    Núcleo común estructurado + datos_extra (JSON) con las columnas propias de
    cada tipo, para no perder nada y que la importación sea automática."""
    __tablename__ = "bolsas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unidad: Mapped[str] = mapped_column(String(40), index=True, server_default="SABOGAL")
    tipo: Mapped[str] = mapped_column(String(50), index=True)   # TUTELA, PQR, IPPE…
    zona: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    caso: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)   # identificador principal
    municipio: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    circuito: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    lcl: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    pdl: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    estado: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    supervisor: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    fecha_entrega: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    fecha_prog: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    observacion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    datos_extra: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # JSON: columnas específicas del tipo
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class DimensionCuadrilla(Base):
    """Dimensionamiento (vista Mónica): cuántas cuadrillas REQUERIDAS por tipo y
    zona en cada unidad. Las 'activas' se calculan de la programación real."""
    __tablename__ = "dimension_cuadrillas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unidad: Mapped[str] = mapped_column(String(40), index=True, server_default="SABOGAL")
    zona: Mapped[str] = mapped_column(String(100))
    tipo_grupo: Mapped[str] = mapped_column(String(50))
    requeridas: Mapped[int] = mapped_column(Integer, default=0)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class IntegranteCuadrilla(Base):
    """Roster/costos (vista Mónica): integrantes de cada cuadrilla, su estado y
    costo. Datos de RRHH que el equipo rellena (no vienen de la programación)."""
    __tablename__ = "integrantes_cuadrilla"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unidad: Mapped[str] = mapped_column(String(40), index=True, server_default="SABOGAL")
    zona: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tipo_grupo: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    codigo_cuadrilla: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # p.ej. MOE2_1
    cargo: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)             # Jefe de cuadrilla, Electricista…
    nombre: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    estado: Mapped[str] = mapped_column(String(20), default="activo")                   # activo, vacante
    salario: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bono_mov: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    aux_transporte: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    no_facturable: Mapped[bool] = mapped_column(Boolean, default=False)
    causa_vacante: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    observacion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


# ── Módulo ANS (cálculo de indicadores) ─────────────────────────────────────────
# Independiente: replica el proceso manual de "Calculo_ANS_<Mes>.xlsx" (hoja 'data'
# + reportes 3.x). Solo LEE el módulo Bolsas para 3.1/3.5/5.2; no lo modifica.

class AnsRegistro(Base):
    """Una fila de la hoja 'data' del Calculo_ANS. Núcleo importado de los CD10
    (Programados / Línea Viva), campos manuales que rellenan las programadoras
    (persisten entre re-importes) y datos_extra con la fila CD10 cruda."""
    __tablename__ = "ans_registros"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                               # real / prueba
    unidad: Mapped[str] = mapped_column(String(40), index=True, server_default="SABOGAL")
    periodo: Mapped[str] = mapped_column(String(7), index=True)          # YYYY-MM
    tipo: Mapped[str] = mapped_column(String(4), index=True)             # FT (programado) / ST (línea viva)
    # ── Núcleo importado (mapeo de la leyenda data filas 1-2) ──
    protocolo: Mapped[str] = mapped_column(String(120), index=True)      # data C
    pdl: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)        # data A (antes de "/")
    asociado_pdl: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)  # data D
    ri_jt: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)    # data E
    zona: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)  # data F
    nombre_responsable: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # data G
    sigla_elaborador: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)    # data H
    descripcion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)     # data I
    fecha_inicio: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # data J
    hora_inicio: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)   # data K
    fecha_fin: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)     # data L
    hora_fin: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)      # data M
    unidad_resp: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)  # data N
    elaborado_por: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # data O (aprobador)
    estado_actual: Mapped[Optional[str]] = mapped_column(String(60), nullable=True, index=True)  # data P
    modif_consistencia: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # data Q
    causal: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)        # data R
    maniobra_inicio: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # data S
    maniobra_fin: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)     # data T
    # ── Campos manuales (programadora) — persisten entre re-importes ──
    fecha_debio_validar: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # data W
    fecha_envio_validar: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # data Y
    excluible_tiempos: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)    # data AA
    obs_tiempos: Mapped[Optional[str]] = mapped_column(Text, nullable=True)               # data AB
    excluible: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)            # data AL
    obs: Mapped[Optional[str]] = mapped_column(Text, nullable=True)                       # data AM
    se_ejecuto: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)           # data AN
    excluible_anulado: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)    # data AO
    obs_anulado: Mapped[Optional[str]] = mapped_column(Text, nullable=True)               # data AP
    nombre_cumplimentador: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # data AQ
    obs_uucc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)                  # data AR
    excluible2: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)           # data AT
    obs3: Mapped[Optional[str]] = mapped_column(Text, nullable=True)                      # data AU
    nodos_planeados: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)         # data AV
    nodos_ejecutados: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)        # data AW
    nods_excluible: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)       # data AX
    obs_nodos: Mapped[Optional[str]] = mapped_column(Text, nullable=True)                 # data AY
    # Marca de las AUTOMATIZACIONES de nodos (reglas 3.2B/3.2C): JSON {regla, campo, antes, valor,
    # estado ok|invalida, cuando, detalle}. La pinta el grid (amarillo=aplicada, rojo=la condición
    # que la justificaba ya no se cumple). Solo sistema; nunca va al exporte.
    nodos_auto: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    datos_extra: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # JSON: fila CD10 cruda
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class AnsAprobador(Base):
    """Catálogo editable de aprobadores ENEL (filtro de ingesta). Semilla desde
    'ENEL APROBADORES DE PDL.xlsx'."""
    __tablename__ = "ans_aprobadores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(200), index=True)         # "APELLIDOS NOMBRES (CODIGO)"
    codigo: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class AnsPeriodo(Base):
    """Metadatos de un corte mensual ANS (para listar/limpiar)."""
    __tablename__ = "ans_periodos"
    __table_args__ = (UniqueConstraint("periodo", "entorno", name="uq_ans_periodos_periodo_entorno"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                               # real / prueba
    periodo: Mapped[str] = mapped_column(String(7), index=True)         # YYYY-MM
    unidad: Mapped[str] = mapped_column(String(40), server_default="SABOGAL")
    notas: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class AnsFuente(Base):
    """Respaldo de PROCEDENCIA: cada archivo fuente subido (CD10 STM programados/ST
    o reporte 'no programado') se guarda en disco y se puede volver a descargar para
    justificar de dónde salieron los datos."""
    __tablename__ = "ans_fuentes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                                  # real / prueba
    periodo: Mapped[str] = mapped_column(String(7), index=True)             # YYYY-MM
    tipo: Mapped[str] = mapped_column(String(20), index=True)               # programados / st / no_programado
    nombre: Mapped[str] = mapped_column(String(300))                        # nombre original del archivo
    ruta: Mapped[str] = mapped_column(String(500))                          # ruta relativa en disco
    tamano: Mapped[int] = mapped_column(Integer, default=0)                 # bytes
    registros: Mapped[int] = mapped_column(Integer, default=0)             # filas que pasaron el filtro (STM)
    hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)  # sha256 (detecta archivo repetido)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class AnsCambio(Base):
    """Historial/auditoría de cada edición manual de un AnsRegistro (datos sensibles):
    quién, qué campo, valor anterior → nuevo, cuándo. Permite revertir."""
    __tablename__ = "ans_cambios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                              # real / prueba
    registro_id: Mapped[int] = mapped_column(Integer, index=True)
    periodo: Mapped[str] = mapped_column(String(7), index=True)
    protocolo: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    campo: Mapped[str] = mapped_column(String(50))
    valor_anterior: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    valor_nuevo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tipo: Mapped[str] = mapped_column(String(12), default="edicion")   # edicion / reversion
    usuario_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    usuario_nombre: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class AnsBolsaSnapshot(Base):
    """Snapshot mensual de las tablas 3.1/3.5 de bolsas (lo que Paola pegaba del
    BOLSAS ENEL del mes). Se guarda por periodo para que 3.1/3.5 cuadren por mes."""
    __tablename__ = "ans_bolsa_snapshots"
    __table_args__ = (UniqueConstraint("periodo", "entorno", name="uq_ans_bolsa_snapshots_periodo_entorno"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                                    # real / prueba
    periodo: Mapped[str] = mapped_column(String(7), index=True)              # YYYY-MM
    archivo: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)  # nombre del BOLSAS ENEL
    datos: Mapped[str] = mapped_column(Text)                                 # JSON con {3.1, 3.5}
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class AnsImpacto36(Base):
    """Cifras $ de la hoja 3.6 (2.9 'Mayor Impacto' + 3.6 'Programado'). En el Excel
    de Paola venían de un archivo externo de materiales ('RESULTADO INDICADOR'); aquí
    el sistema es dueño de esos valores por periodo y se inyectan al exportar (en vez
    de la referencia externa rota). datos = JSON {"29": {fila: {zona: valor}}, "36": {...}}."""
    __tablename__ = "ans_impacto36"
    __table_args__ = (UniqueConstraint("periodo", "entorno", name="uq_ans_impacto36_periodo_entorno"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                                    # real / prueba
    periodo: Mapped[str] = mapped_column(String(7), index=True)              # YYYY-MM
    datos: Mapped[str] = mapped_column(Text)                                 # JSON {29:{...}, 36:{...}}
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class AnsProg20(Base):
    """Filas de la 'Programación diaria ENEL' (la de los 20'). Alimenta el PLANEADO de 3.5
    (conteo de LCL únicos por zona × subproceso) y el INVR (VALOR MO + VALOR MATERIALES).
    Se REEMPLAZA todo al re-subir el archivo (es un corte acumulado)."""
    __tablename__ = "ans_prog20"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                                    # real / prueba
    mes: Mapped[str] = mapped_column(String(7), index=True)                  # YYYY-MM de la fecha de ejecución
    lcl: Mapped[Optional[str]] = mapped_column(String(60), nullable=True, index=True)
    proceso: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    subproceso: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    zona: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    valor_mo: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    valor_mat: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    fecha_ejec: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    estado: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    # ── Columnas para el cruce de NODOS (apartado de María Inés, 3.2 C) ──
    # Crudas tal cual vienen (F y G llegan a veces intercambiadas): normaliza el cruce, no la ingesta.
    tipo_pdt: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)       # H: PDL/Ticket/PI/PST…
    pdt: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)  # I (a veces trae VARIOS códigos)
    nodos_prog: Mapped[Optional[str]] = mapped_column(Text, nullable=True)           # F: "N1,N2,N3" (lista)
    cant_nodos: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)    # G: cantidad (cruda)
    avance: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)        # T: avance 20:30 (1 = cerró)
    municipio: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)     # M
    jefe_cuadrilla: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # P
    alcance: Mapped[Optional[str]] = mapped_column(Text, nullable=True)              # J: alcance del trabajo
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class Contacto(Base):
    """Directorio de teléfonos de la gente a la que hay que preguntarle (líderes, supervisores,
    profesionales de apoyo). NO se importa de ninguna parte: se llena a mano, un contacto a la vez,
    cuando lo necesitas — por eso el número siempre es de fiar (lo guardó una persona, no un cruce).

    `nombre_norm` es la llave: el nombre del ANS normalizado (sin tildes, mayúsculas, espacios
    colapsados), para que «Ramírez  Moya  Julio» encuentre lo guardado como «RAMIREZ MOYA JULIO».
    No lleva `entorno`: un teléfono es el mismo en real y en prueba."""
    __tablename__ = "contactos"
    __table_args__ = (UniqueConstraint("nombre_norm", name="uq_contactos_nombre_norm"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(200))                          # tal como aparece en el ANS
    nombre_norm: Mapped[str] = mapped_column(String(200), index=True)
    telefono: Mapped[str] = mapped_column(String(20))                         # E.164 sin '+': 573001234567
    cedula: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # para distinguir homónimos
    rol: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)     # lider / supervisor / apoyo / otro
    zona: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    notas: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    creado_por: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class AnsConsulta(Base):
    """Hilo de investigación de UNA celda de exclusión: la conversación (posiblemente con varias
    personas, una remite a otra) que lleva a decidir si el incumplimiento es excluible y por qué.
    Es la evidencia de cómo se llegó a la determinación — lo que ENEL podría pedir. La IA (Claude)
    lee los turnos y propone; la decisión final la aplica una persona."""
    __tablename__ = "ans_consultas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                                    # real / prueba
    reg_id: Mapped[int] = mapped_column(Integer, index=True)                 # AnsRegistro
    campo: Mapped[str] = mapped_column(String(30))                           # 'excluible'/'excluible_anulado'/'excluible_tiempos'
    estado: Mapped[str] = mapped_column(String(15), default="abierta")       # abierta / resuelta / descartada
    persona_actual: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # a quién esperamos
    excluible_sug: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)     # SI / NO propuesto por IA
    categoria: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    razon_sug: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resumen: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Última nota que ESTE hilo escribió solo en la celda de observación (la "bitácora viva", como
    # la llevaba Paola: «A LA ESPERA DEL CORREO DE…»). Se guarda para poder actualizarla sin pisar
    # nunca lo que una persona haya escrito a mano.
    nota_auto: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    creado_por: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    mensajes: Mapped[list["AnsConsultaMsg"]] = relationship(
        back_populates="consulta", cascade="all, delete-orphan", order_by="AnsConsultaMsg.id")


class AnsEvidencia(Base):
    """El soporte de una exclusión: los videos que la cuadrilla sube al grupo de WhatsApp (o el
    correo, si viene de ENEL). Sin esto la exclusión no se sostiene ante ENEL, así que el archivo
    se guarda en el servidor y queda amarrado a la celda concreta (reg_id + campo de decisión)."""
    __tablename__ = "ans_evidencias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                                    # real / prueba
    reg_id: Mapped[int] = mapped_column(Integer, index=True)
    campo: Mapped[str] = mapped_column(String(30), index=True)               # excluible / excluible_anulado / …
    nombre: Mapped[str] = mapped_column(String(300))                         # nombre original
    ruta: Mapped[str] = mapped_column(String(500))                           # en disco
    mime: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    tamano: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)    # bytes
    nota: Mapped[Optional[str]] = mapped_column(Text, nullable=True)         # de dónde salió
    # Audio del video → texto (faster-whisper, local). Cacheado para no re-transcribir en cada
    # análisis; "" = ya se intentó y no había audio útil (para distinguirlo de "aún no intentado").
    transcripcion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subido_por: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class AnsConsultaMsg(Base):
    """Un turno del hilo: lo que se envió, lo que respondieron, o la lectura de la IA. Se accede
    siempre por `consulta_id` (que ya está aislado por entorno), así que no lleva `entorno` propio."""
    __tablename__ = "ans_consulta_msgs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consulta_id: Mapped[int] = mapped_column(ForeignKey("ans_consultas.id"), index=True)
    direccion: Mapped[str] = mapped_column(String(10))                       # enviado / recibido / ia
    persona: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    texto: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[str]] = mapped_column(Text, nullable=True)         # JSON de la decisión IA
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    consulta: Mapped["AnsConsulta"] = relationship(back_populates="mensajes")


class AnsNodosCaso(Base):
    """Bandeja de NODOS de María Inés (3.2 C): un caso por fila ANS del periodo. La máquina
    cruza el PDL contra su 'Programación diaria ENEL' (ans_prog20) y le PROPONE los nodos con
    evidencia (sus propias filas); NUNCA escribe sola — ella aprueba caso a caso y al aprobar
    se llenan AU/AV/AX del registro con historial (AnsCambio) y procedencia."""
    __tablename__ = "ans_nodos_casos"
    __table_args__ = (UniqueConstraint("registro_id", name="uq_ans_nodos_casos_registro"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                                    # real / prueba
    periodo: Mapped[str] = mapped_column(String(7), index=True)              # YYYY-MM
    registro_id: Mapped[int] = mapped_column(Integer, index=True)            # → ans_registros.id
    # pendiente / aprobado / omitido / revisar (aprobado cuyo dato cambió al re-subir el archivo)
    estado: Mapped[str] = mapped_column(String(12), index=True, default="pendiente", server_default="pendiente")
    confianza: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)  # alta / revisar / sin_cruce
    motivo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)       # por qué la propuesta (legible)
    sugerido_planeados: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sugerido_ejecutados: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    evidencia: Mapped[Optional[str]] = mapped_column(Text, nullable=True)    # JSON: filas de SU Excel que cruzaron
    hash_evidencia: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # detecta cambios al re-subir
    fuente_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # AnsFuente (prog20) del cruce
    aprobado_por: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    aprobado_en: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class AnsSubprocMap(Base):
    """Mapeo EDITABLE subproceso (de la programación de los 20') → proceso del indicador 3.5
    (PQR / AMP / IPPE / CUND 100 / TLC / TUTELAS, u OBRAS = fila de inversión, que no cuenta
    por LCL sino en plata) o NULL = no cuenta. Paola lo ajusta desde el dashboard; se
    auto-siembra con los subprocesos que traiga cada archivo."""
    __tablename__ = "ans_subproc_map"
    __table_args__ = (UniqueConstraint("subproceso", "entorno", name="uq_ans_subproc_map_sub_entorno"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                                    # real / prueba
    subproceso: Mapped[str] = mapped_column(String(120), index=True)
    proceso: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # proceso 3.5 o NULL
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


# ── Bolsas MOES (Agobardo / Pisos Móviles) ──────────────────────────────────────
# Módulo aislado para la cuenta `moes` (Agobardo): ingiere los Excel "BOLSA PISOS
# MOVILES" que Enel envía, lleva el estado ACTUAL consolidado de cada ticket y la
# LÍNEA DE TIEMPO de cambios entre versiones (qué cambió, cuándo, en qué Excel).

class BolsaMoesImport(Base):
    """Cada Excel de Enel subido: respaldo del archivo + estadísticas del cruce
    (altas / modificaciones / sin cambio / duplicados). Es la pestaña 'Excel subidos'
    que justifica todo el módulo: cada cambio apunta al import que lo originó."""
    __tablename__ = "bolsa_moes_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unidad: Mapped[str] = mapped_column(String(40), index=True, server_default="CHOCONTA")
    nombre: Mapped[str] = mapped_column(String(300))                        # nombre original del archivo
    ruta: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # ruta relativa en disco (descargable)
    hoja: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)  # hoja usada del libro
    tamano: Mapped[int] = mapped_column(Integer, default=0)
    hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)  # sha256 (re-subida)
    cifrado: Mapped[bool] = mapped_column(Boolean, default=False)            # llegó cifrado y se desbloqueó
    fecha_version: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)  # fecha de la versión (la indica quien sube)
    aplicado: Mapped[bool] = mapped_column(Boolean, default=True)            # si actualizó el estado actual (False = más vieja, solo archivada)
    filas_total: Mapped[int] = mapped_column(Integer, default=0)
    filas_alta: Mapped[int] = mapped_column(Integer, default=0)
    filas_mod: Mapped[int] = mapped_column(Integer, default=0)
    filas_igual: Mapped[int] = mapped_column(Integer, default=0)
    duplicados: Mapped[int] = mapped_column(Integer, default=0)
    columnas_nuevas: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON: columnas que aparecen por primera vez
    subido_por_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    subido_por: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)


class BolsaMoes(Base):
    """Estado ACTUAL consolidado de un ticket de la bolsa MOES, identificado por
    `clave` (ANTECEDENTE + LCL normalizados). El núcleo se estructura para filtrar;
    `datos_extra` (JSON) guarda TODAS las columnas originales sin perder nada."""
    __tablename__ = "bolsa_moes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unidad: Mapped[str] = mapped_column(String(40), index=True, server_default="CHOCONTA")
    clave: Mapped[str] = mapped_column(String(220), index=True)             # ANTECEDENTE|LCL normalizado
    antecedente: Mapped[Optional[str]] = mapped_column(String(300), nullable=True, index=True)
    lcl: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    odm: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    estado: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    responsable_enel: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    proyecto: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    municipio: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    cto: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    valor_mo: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # VALOR MO PRESUPUESTADO
    fecha_max: Mapped[Optional[date]] = mapped_column(Date, nullable=True)   # FECHA MAX EJECUCION
    fecha_ejecucion: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    pdl_tck: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    se_ejecuto: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    pct_ejecutado: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    observaciones: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    datos_extra: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON: todas las columnas originales
    duplicado: Mapped[bool] = mapped_column(Boolean, default=False)          # apareció >1 vez en alguna carga
    import_alta_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # carga donde apareció primero
    import_visto_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)  # última carga que lo trae
    version_aplicada: Mapped[Optional[date]] = mapped_column(Date, nullable=True)   # fecha de la versión cuyos datos están vigentes
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class BolsaMoesCambio(Base):
    """Línea de tiempo: cada alta/modificación/baja de un ticket entre versiones del
    Excel (o edición manual de Agobardo). Apunta al import que lo originó. Es la
    trazabilidad que pidieron: qué cambió, de qué valor a cuál, cuándo y por qué archivo."""
    __tablename__ = "bolsa_moes_cambios"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    unidad: Mapped[str] = mapped_column(String(40), index=True, server_default="CHOCONTA")
    bolsa_id: Mapped[int] = mapped_column(Integer, index=True)
    import_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    clave: Mapped[Optional[str]] = mapped_column(String(220), nullable=True, index=True)
    antecedente: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    tipo: Mapped[str] = mapped_column(String(14), default="modificacion")   # alta / modificacion / baja / reaparece / edicion
    campo: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    valor_anterior: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    valor_nuevo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    inconsistencia: Mapped[bool] = mapped_column(Boolean, default=False)    # cambió un campo de Enel que no debería
    usuario_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    usuario_nombre: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)


class Auditoria(Base):
    """Historial/auditoría GLOBAL e independiente de toda acción de todo usuario (crear/editar/
    borrar) en todos los módulos. Se llena automáticamente con un listener `before_flush`
    (ver app/services/auditoria.py): no hace falta instrumentar cada endpoint. Sirve para
    determinar el comportamiento de los usuarios y como copia de seguridad (snapshot por fila,
    fila-por-fila también en cargas masivas, agrupadas por `request_id`)."""
    __tablename__ = "auditoria"

    # BIGINT en Postgres (auditoría puede crecer mucho); INTEGER en SQLite para que autoincremente.
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)
    # Quién (snapshot inmutable, sobrevive aunque borren al usuario)
    usuario_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    usuario_nombre: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    usuario_rol: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    unidad: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # Qué
    modulo: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)   # nombre amable
    tabla: Mapped[str] = mapped_column(String(60), index=True)
    registro_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    accion: Mapped[str] = mapped_column(String(10))                            # crear/editar/borrar
    campo: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    valor_anterior: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    valor_nuevo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    datos: Mapped[Optional[str]] = mapped_column(Text, nullable=True)          # snapshot JSON (crear/borrar)
    # Contexto de la petición (agrupa cargas masivas en una "operación")
    request_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    ruta: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    metodo: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    resumen: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
