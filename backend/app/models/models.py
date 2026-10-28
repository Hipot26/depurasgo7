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
from sqlalchemy.dialects.postgresql import JSONB


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
    ans = "ans"                  # cuenta ANS: solo ve los módulos ANS y Bolsas
    moes = "moes"                # cuenta MOES (unidad CHOCONTA): solo ve el módulo Bolsas MOES
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

    # Unidad operativa (aislamiento SABOGAL / CHOCONTA …)
    unidad: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, server_default="SABOGAL", index=True)

    # Identificación
    zona: Mapped[ZonaEnum] = mapped_column(Enum(ZonaEnum), index=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    lider: Mapped[str] = mapped_column(String(100))
    tipo_grupo: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # texto libre: CAMP5… / MOE2, TAI, SUP.

    # Datos técnicos - llenan ingenieros de zona (azul oscuro)
    proceso: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    subproceso: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    proyecto_antecedente: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # CHANGELOG 2026-10-20: sin tope (texto del DF; era String(400))
    pms: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    circuito: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    lcl: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    fecha_vigencia: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    tipo_pdt: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    num_pdt: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    estado: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    cd_afectados: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    saidi: Mapped[Optional[float]] = mapped_column(Float, nullable=True)          # plantilla 2026-08
    hora: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)        # legado (plantilla vieja: una sola HORA)
    hora_ab: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)     # hora apertura
    hora_cr: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)     # hora cierre
    municipio: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    vereda: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    alcance: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Datos programadores - llenan programadores (verde)
    gestor_cliente: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    supervisor: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    valor_dia: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mo_proyectada: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mo_real: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mo_facturada: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # col. AD de la plantilla (2026-09-21)
    valor_dejado_ejecutar: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    valor_material: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Datos facturación/SGS - llenan facturación (azul claro)
    estado_pdt: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    notas: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    restricciones: Mapped[Optional[str]] = mapped_column(Text, nullable=True)                # plantilla 2026-08
    se_gestiona: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)            # SI / NO
    num_proliquidado: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    fecha_liquidacion_sgs: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    estado_facturacion: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    fecha_envio_facturacion: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    ot: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    estado_cumplimentacion: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    inoperatividad: Mapped[Optional[float]] = mapped_column(Float, nullable=True)            # valor $ (unidad CHOCONTA / plantilla Mantilla)
    inoperatividad_tipo: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)   # recurso inoperativo: GRUA, PERSONAL… (unidad SABOGAL, 2026-08)
    observaciones_inoperatividad: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detalle_mantilla: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON: desglose por actividad (unidad CHOCONTA)

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
    # «ES EMERGENCIAL» (data U) ajustado a mano: SI/NO manda sobre la fórmula de la causal en
    # todo el cálculo (apertura/cierre N/A, 3.4 N/A, exporte); vacío = automático. Solo dev y
    # cuentas habilitadas (config.puede_editar_emergencial). 2026-10-01.
    es_emergencial_manual: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
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


class AnsRetirado(Base):
    """Órdenes RETIRADAS del ANS por decisión de una persona (desde 2026-10-02). El STM las
    sigue trayendo en cada descarga, así que el import consulta
    esta tabla y las salta. Guarda quién lo ordenó, la fuente y un respaldo completo de lo que
    se borró (fila + consultas + análisis) para poder restaurarla."""
    __tablename__ = "ans_retirados"
    __table_args__ = (UniqueConstraint("pdl", "entorno", name="uq_ans_retirados_pdl_entorno"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                               # real / prueba
    pdl: Mapped[str] = mapped_column(String(80), index=True)            # CDOPP2026R09256
    protocolo: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    periodo: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    tipo: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    motivo: Mapped[str] = mapped_column(Text)
    ordenado_por: Mapped[str] = mapped_column(String(120))              # quién tomó la decisión
    fuente: Mapped[str] = mapped_column(String(300))                    # dónde quedó dicha (WhatsApp, correo…)
    usuario_nombre: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)  # quién la ejecutó
    respaldo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON de lo borrado
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


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
    """Snapshot mensual de las tablas 3.1/3.5 de bolsas (lo que el proceso manual pegaba
    del BOLSAS ENEL del mes). Se guarda por periodo para que 3.1/3.5 cuadren por mes."""
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
    oficial del ANS venían de un archivo externo de materiales ('RESULTADO INDICADOR'); aquí
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
    colapsados), para que «Apellido  Apellido  Nombre» encuentre lo guardado como «APELLIDO APELLIDO NOMBRE».
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
    Es la evidencia de cómo se llegó a la determinación, lo que ENEL podría pedir. La IA
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
    # Última nota que ESTE hilo escribió solo en la celda de observación (la "bitácora viva" del
    # proceso manual: «A LA ESPERA DEL CORREO DE…»). Se guarda para poder actualizarla sin pisar
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
    # PAQUETE PARA ENEL (2026-10-14): requisito de la dirección tras la conciliación mensual. El ZIP
    # nombra cada soporte «Evidencia_N_Video_16-50_a_16-53_<qué muestra>» en orden de hora y solo
    # van los marcados `en_paquete`. hora/hora_fin = reloj del letrero (HH:MM); fecha = del correo o
    # del letrero (AAAA-MM-DD); que_muestra = etiqueta corta (IA o a mano) para el nombre y el resumen.
    # Las columnas las añade el guion con ALTER TABLE (create_all no altera tablas existentes).
    en_paquete: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    fecha: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    hora: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    hora_fin: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    que_muestra: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
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
    por LCL sino en plata) o NULL = no cuenta. Se ajusta desde el dashboard; se
    auto-siembra con los subprocesos que traiga cada archivo."""
    __tablename__ = "ans_subproc_map"
    __table_args__ = (UniqueConstraint("subproceso", "entorno", name="uq_ans_subproc_map_sub_entorno"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()                                    # real / prueba
    subproceso: Mapped[str] = mapped_column(String(120), index=True)
    proceso: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # proceso 3.5 o NULL
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


# ── Bolsas MOES (unidad CHOCONTA / Pisos Móviles) ───────────────────────────────
# Módulo aislado para la cuenta `moes`: ingiere los Excel "BOLSA PISOS
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
    Excel (o edición manual del coordinador). Apunta al import que lo originó. Es la
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


# ── Subtab «3.Programados» (ANS NUEVO) — espejo de la hoja 5.2, Excel BASE CONTROL_NNDD ──
# Reconstruidos desde migrations/2026-08-08_programados.sql (el original vivía solo
# en el servidor y un sync del 2026-08-11 lo pisó — mantener alineados con ese DDL).

class ProgCarga(Base):
    """Una fila por Excel Enel subido (procedencia + auditoría)."""
    __tablename__ = "prog_carga"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()
    periodo: Mapped[str] = mapped_column(String(7), index=True)                # YYYY-MM
    nombre: Mapped[str] = mapped_column(String(300))                           # nombre original del archivo
    ruta: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)    # respaldo en disco
    tamano: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    hoja: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    filas_leidas: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    filas_aplicadas: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    celdas_llenas: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    resumen: Mapped[Optional[str]] = mapped_column(Text, nullable=True)        # JSON
    usuario_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("usuarios.id"), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ProgRegistro(Base):
    """Una fila por radicado — espejo de las columnas A..R de la hoja 5.2."""
    __tablename__ = "prog_registro"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()
    periodo: Mapped[str] = mapped_column(String(7), index=True)                # YYYY-MM
    radicado: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    lcl: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    eecc: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    proyecto: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    fecha_entrega: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    direccion: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    municipio: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    afectacion: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)     # regla SI/NO
    pdl: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    fecha_maniobra: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    dias_exclusion: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    obs_retraso: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    zona: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)           # regla municipio→zona
    excluible2: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    excluible3: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    procedencia: Mapped[Optional[str]] = mapped_column(Text, nullable=True)          # JSON {campo: origen}
    avisos: Mapped[Optional[str]] = mapped_column(Text, nullable=True)               # JSON [str]
    fila_origen: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("prog_carga.id", ondelete="SET NULL"), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class ProgCambio(Base):
    """Qué carga puso o quitó cada dato (justificación celda a celda)."""
    __tablename__ = "prog_cambio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()
    registro_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("prog_registro.id", ondelete="CASCADE"), nullable=True, index=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("prog_carga.id", ondelete="SET NULL"), nullable=True)
    campo: Mapped[str] = mapped_column(String(40))
    valor_anterior: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    valor_nuevo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    procedencia: Mapped[str] = mapped_column(String(20))                       # import | bolsa | regla_h | regla_p | manual
    motivo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    usuario_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("usuarios.id"), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ProgZonaMap(Base):
    """Mapeo editable municipio → zona (alimenta la columna P)."""
    __tablename__ = "prog_zona_map"
    __table_args__ = (UniqueConstraint("municipio", "entorno", name="uq_prog_zona_map_mun_entorno"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()
    municipio: Mapped[str] = mapped_column(String(150), index=True)            # sin tildes, MAYÚSCULAS
    zona: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)     # NULL = sin zona
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


# ══════════════════════════════ BOT DE WHATSAPP ══════════════════════════════
# El bot (Baileys, proceso Node aparte en el VPS) es un "dispositivo vinculado" del
# número que ya está en los grupos de ENEL. Todo lo que ve entra aquí crudo; el
# relacionamiento con órdenes/consultas es un paso posterior (matcher).

class WaMensaje(Base):
    """Bandeja de ENTRADA del bot: cada mensaje visto en los grupos vigilados o en chats
    directos. La media (video/imagen/documento) ya viene descargada por el bot y guardada
    en disco. `estado` gobierna el pipeline: nuevo → el matcher lo cruza con órdenes →
    vinculado/descartado."""
    __tablename__ = "wa_mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wa_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)   # id del mensaje (dedupe en reintentos)
    chat_jid: Mapped[str] = mapped_column(String(120), index=True)             # grupo o contacto
    chat_nombre: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    es_grupo: Mapped[bool] = mapped_column(Boolean, default=False)
    remitente_jid: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    remitente_nombre: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # En chats directos también se guardan los mensajes PROPIOS (de_mi=True): son la mitad
    # de la conversación con los ingenieros y sin ellos no hay resumen cronológico (3.2 B).
    de_mi: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    tipo: Mapped[str] = mapped_column(String(20), default="texto")             # texto/imagen/video/documento/audio
    texto: Mapped[Optional[str]] = mapped_column(Text, nullable=True)          # cuerpo o caption
    # Si el mensaje RESPONDE CITANDO otro (deslizar en WhatsApp): el wa_id del citado.
    # Si la cita apunta a un envío nuestro, el match con su caso es exacto.
    quote_wa_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    media_ruta: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    media_mime: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    media_tamano: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    enviado_en: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # timestamp del mensaje en WhatsApp
    estado: Mapped[str] = mapped_column(String(15), default="nuevo", index=True)     # nuevo/vinculado/descartado
    evidencia_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)      # si terminó como AnsEvidencia
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class WaEnvio(Base):
    """Cola de SALIDA del bot: mensajes que la plataforma quiere mandar (las consultas del
    ANS, hoy manuales con el botón WhatsApp). El bot la lee en cada ciclo y los manda con
    pausas humanas — nunca en ráfaga, para no exponer el número."""
    __tablename__ = "wa_envios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    destino: Mapped[str] = mapped_column(String(30))                           # teléfono E.164 sin '+' (573001234567)
    persona: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    texto: Mapped[str] = mapped_column(Text)
    reg_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)      # celda ANS que lo originó
    campo: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    estado: Mapped[str] = mapped_column(String(15), default="pendiente", index=True)  # pendiente/enviado/error
    wa_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)   # id del mensaje realmente enviado
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    creado_por: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    enviado_en: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class WaGrupo(Base):
    """Grupos que el bot vigila. El bot reporta todos los grupos donde está el número
    (sincroniza nombre/jid); una persona marca `vigilado` en los que importan. Solo de
    los vigilados lee el analista de IA; desde el 26-09 el bot guarda todos los grupos."""
    __tablename__ = "wa_grupos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    jid: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    vigilado: Mapped[bool] = mapped_column(Boolean, default=False)
    visto_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class WaAnalisis(Base):
    """Un análisis de caso hecho por la máquina (matcher + IA de visión/audio) sobre el
    material de WhatsApp: la propuesta completa queda aquí como JSON para que el
    desarrollador la AUDITE en su tab (ver el proceso, los videos y la observación) antes
    de aplicarla a la celda. Nada se escribe en el ANS sin ese visto bueno."""
    __tablename__ = "wa_analisis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entorno: Mapped[str] = _col_entorno()
    reg_id: Mapped[int] = mapped_column(Integer, index=True)                  # → ans_registros.id
    campo: Mapped[str] = mapped_column(String(30), default="excluible")       # celda objetivo (3.2 A)
    estado: Mapped[str] = mapped_column(String(15), default="propuesto", index=True)  # propuesto/aplicado/descartado
    resultado: Mapped[Optional[str]] = mapped_column(Text, nullable=True)     # JSON: eventos, videos, veredicto IA
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class CorreoAns(Base):
    """Bandeja de CORREOS etiquetados «ANS» en el buzón vigilado. El agente local
    (correo-bot/cazador_ans.py, corre en el PC del operador; el servidor no ve Outlook) los exporta a .msg
    y los sube aquí. Al entrar se les cazan los códigos PDL (asunto +
    cuerpo reciente) y se cruzan contra los registros del ANS: `pdls` guarda esas
    coincidencias para que el tab Correos ANS proponga a qué caso sirve cada correo."""
    __tablename__ = "correo_ans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(300), unique=True, index=True)  # EntryID de Outlook (dedupe)
    asunto: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    remitente: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    fecha_correo: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)  # como la da Outlook
    categorias: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    msg_ruta: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)     # .msg en disco del VPS
    cuerpo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)              # texto ya limpio (parse_msg)
    pdls: Mapped[Optional[str]] = mapped_column(Text, nullable=True)                # JSON: matches contra el ANS
    estado: Mapped[str] = mapped_column(String(15), default="nuevo", index=True)    # nuevo/vinculado/descartado
    evidencia_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)     # si terminó adjunto a una celda
    analisis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)            # JSON: lectura IA (analizar_correo)
    # JSON con la HISTORIA del correo en la bandeja (2026-10-02): `descarte` {cuando, casos,
    # sin_caso} — descartar no es definitivo, se re-cruza en cada STM; `revivido` [{cuando,
    # motivo}] — volvió solo a la bandeja; `revisado` {reg_id: {cuando, decision}} — la persona
    # vio el correo y mantuvo su decisión; `avisado` [claves] — avisos ya enviados.
    meta: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class WaMediaMeta(Base):
    """ÍNDICE de la media de WhatsApp (2026-09-03): todo lo que llega al
    número se guarda; esto es lo que la máquina extrae de cada video/imagen SIN IA, una sola
    vez, para que los análisis automáticos partan de datos y no de bytes:
      · ffprobe: duración y tamaño del cuadro;
      · 3 frames (inicio/mitad/final) en disco, para mostrarlos o dárselos a la IA;
      · OCR (tesseract) del letrero de la cámara: fecha/hora reales, GPS, lugar, código de
        la orden (PDL/DF) y el #líder — con eso el video se atribuye a su caso por consulta,
        no por lectura;
      · transcripción (faster-whisper local) de lo que la cuadrilla dice.
    Una fila por mensaje con media. `estado`: pendiente → listo | error (con `error`)."""
    __tablename__ = "wa_media_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    msg_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)      # → wa_mensajes.id
    tipo: Mapped[str] = mapped_column(String(20))                              # video / imagen / audio / documento
    estado: Mapped[str] = mapped_column(String(12), default="pendiente", index=True)
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    # ffprobe
    duracion_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ancho: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    alto: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    frames_dir: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)   # f1.jpg f2.jpg f3.jpg
    # OCR del letrero (texto crudo de los frames + campos parseados)
    overlay_txt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    overlay_fecha: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, index=True)   # YYYY-MM-DD
    overlay_hora: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)                 # HH:MM:SS (24 h)
    overlay_lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    overlay_lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    overlay_lugar: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    overlay_codigo: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)  # R06705 / DF2026… normalizado
    overlay_lider: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)               # #Alvaro
    # Audio → texto (whisper local). "" = se intentó y no había audio útil.
    transcripcion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Cuánto costó (segundos de CPU de pared) — para dimensionar el ciclo nocturno
    tiempo_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    procesado_en: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class WaMediaCaso(Base):
    """AMARRE determinista media ↔ caso (paso 1 de la orquestación, 2026-09-07): qué video o
    imagen de WhatsApp pertenece a qué orden del ANS, decidido por CÓDIGO (letrero o caption),
    por el LÍDER estampado, o por la ventana de fecha/hora + zona del grupo — sin IA. Es lo
    que garantiza que ningún video del caso se quede por fuera del análisis, esté en el grupo
    que esté. Una fila por (media, caso); `puntaje` dice qué tan fuerte es el amarre
    (100 código en fecha · 90 caption con el PDL · 75 líder en ventana · 60 código fuera de
    fecha · 40 solo ventana+zona). Se recalcula cada noche y antes de cada análisis."""
    __tablename__ = "wa_media_caso"
    __table_args__ = (UniqueConstraint("msg_id", "reg_id", name="uq_wa_media_caso"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    msg_id: Mapped[int] = mapped_column(Integer, index=True)                  # → wa_mensajes.id
    reg_id: Mapped[int] = mapped_column(Integer, index=True)                  # → ans_registros.id
    criterio: Mapped[str] = mapped_column(String(20))                         # codigo / caption / lider / ventana_zona / codigo_sin_fecha
    puntaje: Mapped[int] = mapped_column(Integer, default=0)
    detalle: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())


# ── Metas de la senda SGS (tab «Senda de meta» de SGS Exporte, 2026-09-17) ─────
class SgsMeta(Base):
    """Meta mensual de MO liquidada en SGS por «senda» (UBATE TP / LV / MOE).
    Valores variables por mes (2026-09-17: las metas no coinciden
    con la programación, así que se guardan y se editan aquí). Si un mes no tiene
    filas, el endpoint hereda las del último mes configurado (o los defaults del
    código) sin persistirlas hasta que alguien las guarde.
    `zonas`: zonas SGS separadas por coma ("" = todas) · `procesos`: "MI,PG" o ""
    (todos) · `cuadrillas`: todas | moe | sin_moe (cuadrillas MOE = líderes con
    tipo_grupo MOE% en la programación del mes)."""
    __tablename__ = "sgs_metas"
    __table_args__ = (UniqueConstraint("anio", "mes", "clave", name="uq_sgs_metas_periodo_clave"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    anio: Mapped[int] = mapped_column(Integer, index=True)
    mes: Mapped[int] = mapped_column(Integer, index=True)
    clave: Mapped[str] = mapped_column(String(20))
    nombre: Mapped[str] = mapped_column(String(60))
    meta: Mapped[float] = mapped_column(Float, default=0.0)
    zonas: Mapped[str] = mapped_column(String(100), default="")
    procesos: Mapped[str] = mapped_column(String(20), default="")
    cuadrillas: Mapped[str] = mapped_column(String(10), default="todas")
    orden: Mapped[int] = mapped_column(Integer, default=0)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    actualizado_por: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)


class RegistroSGO7(Base):
    """Trazabilidad de los módulos SGO7 (Balance, Materiales, Verificador, Devoluciones,
    Liquidador): un registro por archivo subido, proceso ejecutado o descarga generada,
    con el usuario SGO7 que lo hizo. Lo alimentan los módulos vía POST /api/sgo7/registros."""
    __tablename__ = "sgo7_registros"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), index=True)
    modulo: Mapped[str] = mapped_column(String(40), index=True)
    usuario: Mapped[Optional[str]] = mapped_column(String(150), index=True, nullable=True)
    rol: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    accion: Mapped[str] = mapped_column(String(80), index=True)
    metodo: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    ruta: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    archivo: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    archivo_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    archivo_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    archivo_copia: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    estado: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    duracion_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sesion: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    detalle: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class CredencialSGS(Base):
    """Cuenta de SGS de cada usuario de SGO7 (contraseña cifrada con SGO7_CRED_KEY). El
    Liquidador la usa para abrir y renovar la sesión SGS de ese usuario: cada quien liquida
    con su propia cuenta."""
    __tablename__ = "sgo7_credenciales_sgs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario: Mapped[str] = mapped_column(String(150), unique=True, index=True)   # email/usuario SGO7
    sgs_login: Mapped[str] = mapped_column(String(120))
    sgs_password_enc: Mapped[str] = mapped_column(Text)
    actualizado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_por: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    ultimo_ok: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ultimo_error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    ultimo_intento: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class CasoFacturacion(Base):
    """SGO7 línea de facturación — un «caso» por consulta de Balance Integrado (planilla SIPWEBORA +
    proyecto SGS). Guarda el semáforo de la última consulta (OK / descuadre / alertas), el estado del
    caso, cuántas veces se consultó y por quién, el Excel generado (copia + huella) y la orden
    «Mandar a liquidar» que atiende el Liquidador Automático. Lo alimentan el Balance (consultas, Excel,
    órdenes), el cron de reconsulta y el Liquidador (estado de la orden)."""
    __tablename__ = "sgo7_casos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    creado_por: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    # identidad del caso
    id_planilla: Mapped[str] = mapped_column(String(120), index=True)          # «17352» o «18505+18374»
    proyecto_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)   # lo que escribió el facturador
    parent_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)  # id SGS resuelto
    pro_code: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    proyecto_nombre: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    lcl: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    odm: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    ticket: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    zona: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    empresa: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # estado y semáforo
    estado: Mapped[str] = mapped_column(String(30), default="consultado", index=True)
    semaforo: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)   # verde / ambar / rojo
    resumen: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    consultas: Mapped[int] = mapped_column(Integer, default=0)
    ultima_consulta_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ultima_consulta_por: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    ultima_consulta_origen: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)   # manual / auto
    ultimo_error: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    reconsulta_auto: Mapped[bool] = mapped_column(Boolean, default=True)
    proxima_reconsulta: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    nota: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Excel generado (fase final del Balance)
    excel_archivo: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    excel_ruta: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    excel_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    excel_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    excel_generado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    excel_generado_por: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    fecha_ejecucion: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    lideres: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    rg11_archivo: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    rg11_ruta: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    # orden «Mandar a liquidar»
    orden_estado: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    orden_enviada_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    orden_enviada_por: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    orden_nota: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    orden_atendida_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    orden_atendida_por: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    orden_sesion: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    orden_resultado: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    orden_terminada_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class CasoEvento(Base):
    """Línea de tiempo de cada caso de facturación: consultas, cambios de estado, Excel, orden."""
    __tablename__ = "sgo7_casos_eventos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    caso_id: Mapped[int] = mapped_column(Integer, ForeignKey("sgo7_casos.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), index=True)
    usuario: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    tipo: Mapped[str] = mapped_column(String(40))
    detalle: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class GestionProyecto(Base):
    """SGO7 Gestión General (22 oct 2026) — un caso por proyecto (LCL 63…, ticket DF… o PDL) con el flujo
    FL-PS-E-03 de Dominion (39 pasos, 3 pilares) como columna vertebral. El paso actual, el pilar y las
    alertas los calcula el backend a partir de los eventos (correo, SGS, Sonny, SGO7)."""
    __tablename__ = "sgo7_gg_proyectos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    clave: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    tipo: Mapped[str] = mapped_column(String(30), default="programado", index=True)
    codigo: Mapped[Optional[str]] = mapped_column(String(60), nullable=True, index=True)   # LCL 63… o ticket DF…; lo comparten los gemelos (CHANGELOG 2026-10-22)
    padre: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)                  # el padre en SGS: code, name, supervisor, departamento, zona
    nombre: Mapped[Optional[str]] = mapped_column(String(220), nullable=True)
    hilo: Mapped[str] = mapped_column(String(180), default="", index=True)
    zona: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    supervisor: Mapped[Optional[str]] = mapped_column(String(150), nullable=True, index=True)
    codigos: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    fijados: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    paso_actual: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    max_paso: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pilar: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    paso_fecha: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    estado_texto: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    fuente_paso: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    alertas: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    ultima_actividad: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    n_eventos: Mapped[int] = mapped_column(Integer, default=0)
    n_correos: Mapped[int] = mapped_column(Integer, default=0)
    fuentes: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    sgs: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    facturacion: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    nota: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    origen: Mapped[str] = mapped_column(String(12), default="correo", server_default="correo", index=True)   # CHANGELOG 2026-10-23: fuente que creó el caso
    dinero: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)        # CHANGELOG 2026-10-23: franja PMS → SGS → Sonny → SCM → SAP
    cerrado: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, index=True)      # CHANGELOG 2026-10-25: todas sus LCL en CONCLUSA (SCM); None = sin LCL en SCM
    cerrado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)   # CHANGELOG 2026-10-25: exporte en que la última ya figuraba CONCLUSA


class GestionEvento(Base):
    """Línea de tiempo del proyecto: cada mensaje de correo (directo o citado en el hilo) ubicado en un paso del
    FL-03, y los hechos que vienen de SGS (Control Mensual, Liquidador), Sonny (Balance, Sonny Bot) y SGO7."""
    __tablename__ = "sgo7_gg_eventos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proyecto_id: Mapped[int] = mapped_column(Integer, ForeignKey("sgo7_gg_proyectos.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fuente: Mapped[str] = mapped_column(String(12), index=True)          # correo / sgs / sonny / sgo7
    paso: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    cond: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    actor: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    prueba: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    de: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    de_correo: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    para: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    asunto: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    texto: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    html: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    directo: Mapped[bool] = mapped_column(Boolean, default=False)
    entry_id: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    carpeta: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    buzon: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    adjuntos: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    huella: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ref: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class GestionAgente(Base):
    """Estado del agente de correo de cada buzón (PC): hasta dónde leyó, cuándo habló por última vez."""
    __tablename__ = "sgo7_gg_agentes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buzon: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    cursor: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ultimo_scan: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ultimo_contacto: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    correos_total: Mapped[int] = mapped_column(Integer, default=0)
    ultimo_error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)


class GestionSonny(Base):
    """SGO7 Gestión General (23 oct 2026) — Historial de Planillas de Sonny (export manual; lo sube
    agentes-pc/gestion/cargar_sonny_gestion.py): un cambio de estado de una planilla por fila, con su fecha oficial. Se liga al
    caso por `codigo` (LCL 63…, o el ticket si la planilla no trae LCL) o por `ticket` (DF…); los eventos «sonny» de los
    pasos 24-39 se rehacen desde aquí en cada recálculo del caso."""
    __tablename__ = "sgo7_gg_sonny"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    id_cambio: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    id_planilla: Mapped[str] = mapped_column(String(40), index=True)
    codigo: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    ticket: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    nodo: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    supervisor: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    estado: Mapped[str] = mapped_column(String(60), index=True)
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observacion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cargado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)   # CHANGELOG 2026-10-23


class GestionSonnyPlanilla(Base):
    """CHANGELOG 2026-10-23 — Lista de Planillas de Sonny (export manual; la sube cargar_sonny_gestion.py): una fila por planilla con su MO y MAT
    real, el QR de PMS y las fechas de aprobación del supervisor y del ingeniero. El campo `libreto_sonny` se guarda solo para
    mostrarlo: NO es llave (en 232 de 907 planillas no es el número del libreto en SCM)."""
    __tablename__ = "sgo7_gg_sonny_planilla"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    id_planilla: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    lcl: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    ticket: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    pdl: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    qr: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    odm: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    nodo: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    supervisor: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    estado: Mapped[Optional[str]] = mapped_column(String(60), nullable=True, index=True)
    libreto_sonny: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    mo_plan: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mat_plan: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mo_real: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mat_real: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fecha_planilla: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fecha_supervisor: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fecha_ingeniero: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fecha_enel: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    estado_supervisor: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    estado_ingeniero: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    cargado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)   # CHANGELOG 2026-10-23


class GestionPms(Base):
    """CHANGELOG 2026-10-23 — Solicitudes de PMS (Enel; zip «exported-request», archivo request.csv; lo sube cargar_pms_gestion.py sin nombres,
    cédulas ni usuarios). `services` = MO valorada, `materials` = MAT valorado. Se liga al caso por LCL (`lcls`) o por el DF de
    `subprojects` (`dfs`); a Sonny por request_code = QR."""
    __tablename__ = "sgo7_gg_pms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_code: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    subprojects: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    lcls: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    dfs: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    pdls: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    odms: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    agp: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    macroproceso: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    services: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    materials: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(60), nullable=True, index=True)
    substatus: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    request_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fecha_estado: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cargado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)   # CHANGELOG 2026-10-23: de qué carga vino


class GestionPmsLog(Base):
    """CHANGELOG 2026-10-23 — Historial de estados de PMS (logs.csv): un cambio por fila, llave `huella` (sha del request_code, la fecha y los
    estados). Sin usuario ni comentarios (traen nombres)."""
    __tablename__ = "sgo7_gg_pms_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    huella: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    request_code: Mapped[str] = mapped_column(String(100), index=True)
    estado_ini: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    estado_fin: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    substatus: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)   # CHANGELOG 2026-10-23


class GestionScmLibreto(Base):
    """CHANGELOG 2026-10-23 — Libretos de medida de SCM (Enel; consulta «Informe LCL_LCA» + prestaciones; lo sube cargar_scm_gestion.py). MO en
    pesos = Σ cantidad × precio QP de sony-sgs/nuevo/precios_mdo_mat.json (los puntos de SCM no son pesos). `planilla` y
    `regla_enlace` los pone el servidor (gestion_flujo.enlazar_libretos). LAV/BOC/CHI no salen en prestaciones ni materiales."""
    __tablename__ = "sgo7_gg_scm_libreto"
    __table_args__ = (UniqueConstraint("lcl", "libreto", name="uq_sgo7_gg_scm_libreto"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lcl: Mapped[str] = mapped_column(String(20), index=True)
    libreto: Mapped[str] = mapped_column(String(20))
    descripcion: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    pdls: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    tickets: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    estado_libreto: Mapped[Optional[str]] = mapped_column(String(12), nullable=True, index=True)
    estado_lcl: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    fecha_autocert: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    mo_pesos: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    puntos: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    n_prest: Mapped[int] = mapped_column(Integer, default=0)
    sin_precio: Mapped[int] = mapped_column(Integer, default=0)
    planilla: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    regla_enlace: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    corte: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cargado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)   # CHANGELOG 2026-10-23
    candidatas: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)   # CHANGELOG 2026-10-23: planillas que consideró la regla
    trabajos: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)   # CHANGELOG 2026-10-23: órdenes «Trabajo» (= Orden de SAP)


class GestionScmMat(Base):
    """CHANGELOG 2026-10-23 — Material posado y recuperado por libreto de SCM (consulta «Informe Materiales», agregado por código en el PC). Sin
    precio: SCM no lo trae."""
    __tablename__ = "sgo7_gg_scm_mat"
    __table_args__ = (UniqueConstraint("lcl", "libreto", "material", name="uq_sgo7_gg_scm_mat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lcl: Mapped[str] = mapped_column(String(20), index=True)
    libreto: Mapped[str] = mapped_column(String(20))
    material: Mapped[str] = mapped_column(String(40))
    posada: Mapped[float] = mapped_column(Float, default=0)
    recuperada: Mapped[float] = mapped_column(Float, default=0)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)   # CHANGELOG 2026-10-23
    unidad: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)   # CHANGELOG 2026-10-23: unidad de medida de SCM


class GestionDeuda(Base):
    """CHANGELOG 2026-10-23 — Informe de deuda de facturación (hoja Data, a mano; lo sube cargar_scm_gestion.py): lo que facturación sigue
    como pendiente de cobro, con su corte. Solo códigos, montos y estado (el informe trae nombres y cédulas: no se copian)."""
    __tablename__ = "sgo7_gg_deuda"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    huella: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    corte: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lcl: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    qr: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    id_planilla: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    pdl_ticket: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    libreto: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    mo: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    estado: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    estado_desc: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    responsabilidad: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    fecha_estado: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)   # CHANGELOG 2026-10-23


class GestionSinCaso(Base):
    """CHANGELOG 2026-10-23 — Códigos que aparecen en Sonny, PMS o SCM y no son caso de Gestión: no existen en SGS, o quedaron fuera del corte de
    la primera carga. Se revisan de nuevo pasados 3 días (POST /api/gestion/agente/nacer)."""
    __tablename__ = "sgo7_gg_sin_caso"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    fuentes: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    motivo: Mapped[str] = mapped_column(String(80))
    fecha_mov: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revisado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


class GestionCarga(Base):
    """CHANGELOG 2026-10-23 — Cada carga de un exporte desde el PC (los cargadores del PC (agentes-pc/gestion)): de qué archivo salen las filas de Sonny, PMS, SCM y la
    deuda. Mismo archivo (sistema, parte, sha256) = la misma carga (se actualizan la fecha y quién cargó). `controles` es la foto de
    los controles automáticos al cerrarla (panel «Fuentes y controles» de /gestion)."""
    __tablename__ = "sgo7_gg_cargas"
    __table_args__ = (UniqueConstraint("sistema", "parte", "sha256", name="uq_sgo7_gg_cargas"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sistema: Mapped[str] = mapped_column(String(20), index=True)
    parte: Mapped[str] = mapped_column(String(30))
    archivo: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    tamano: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    modificado: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    corte: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    usuario: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    equipo: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    filas_archivo: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    filas_enviadas: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duplicados: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fuera_rango: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    detalle: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    resultado: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    controles: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    primera_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    abierta_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    cerrada_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # CHANGELOG 2026-10-25: el archivo de la carga, guardado en /var/www/sgo7/registros/gestion/cargas/<id>/ para «Descargar» (sha256 = el de la carga)
    archivo_ruta: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    archivo_nombre: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    archivo_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    archivo_tamano: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    archivo_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class GestionDecision(Base):
    """CHANGELOG 2026-10-23 — Confirmación humana de un cruce planilla de Sonny ↔ libreto de SCM («Es este» = si, «No es» = no). Manda sobre las reglas
    en todo recálculo (gestion_flujo.enlazar_libretos), como las equivalencias del Balance (CHANGELOG 2026-10-22). Cada una queda también en Registros."""
    __tablename__ = "sgo7_gg_decisiones"
    __table_args__ = (UniqueConstraint("tipo", "lcl", "libreto", "id_planilla", name="uq_sgo7_gg_decisiones"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proyecto_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    tipo: Mapped[str] = mapped_column(String(30), default="planilla_libreto")
    lcl: Mapped[str] = mapped_column(String(20), index=True)
    libreto: Mapped[str] = mapped_column(String(20))
    id_planilla: Mapped[str] = mapped_column(String(40), index=True)
    llaves: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    decision: Mapped[str] = mapped_column(String(4))
    usuario: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    rol: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    nota: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    monto: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class GestionScmPrest(Base):
    """CHANGELOG 2026-10-23 — Cada prestación de un libreto de SCM (Informe Prestaciones): cantidad × precio QP de precios_mdo_mat.json = pesos. Son las
    filas que suman la MO del libreto (y la caja «Libreto SCM»); el servidor compara el precio con la lista de producción."""
    __tablename__ = "sgo7_gg_scm_prest"
    __table_args__ = (UniqueConstraint("lcl", "libreto", "n", name="uq_sgo7_gg_scm_prest"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lcl: Mapped[str] = mapped_column(String(20), index=True)
    libreto: Mapped[str] = mapped_column(String(20))
    n: Mapped[int] = mapped_column(Integer)
    prestacion: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    unidad: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    cantidad: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    puntos_unit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    puntos: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    precio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pesos: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ticket: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)


class GestionSap(Base):
    """CHANGELOG 2026-10-23 — Cada fila del MB51 del SAP de Enel (centro CD00, almacén CDMT, lotes de Dominion por zona) que sube cargar_sap_gestion.py
    desde el PC. Upsert por (documento, posición). El consumo contra la obra son las clases 261/262/281 (neto descargado = −Σ cantidad,
    valor = −Σ Importe ML, pesos de Enel); lo recuperado, 965/966 (KG). Sin usuarios ni cédulas; los textos solo si siguen un patrón
    conocido (gestion_flujo.sap_texto_seguro), si no «(texto libre)». No trae factura, HES ni pago."""
    __tablename__ = "sgo7_gg_sap"
    __table_args__ = (UniqueConstraint("doc", "pos", name="uq_sgo7_gg_sap"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc: Mapped[str] = mapped_column(String(20))
    pos: Mapped[int] = mapped_column(Integer)
    clase: Mapped[str] = mapped_column(String(4), index=True)
    material: Mapped[str] = mapped_column(String(20), index=True)
    descripcion: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    cantidad: Mapped[float] = mapped_column(Float, default=0)
    um: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    importe: Mapped[float] = mapped_column(Float, default=0)
    f_contab: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    lote: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    zona: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    centro: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    almacen: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    centro_rec: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    almacen_rec: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    lote_rec: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    lcl: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    df: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    orden: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    orden_2: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    pedido: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    referencia: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    cuenta: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    texto_cab: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    texto: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    texto_clase: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    texto_tipo: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    dh: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    cargado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class GestionPrecioMat(Base):
    """CHANGELOG 2026-10-23 — Precio de Enel por código de material: mediana de Importe ML / cantidad de las descargas (clase 261) del MB51 cargado. Valora
    el material de todas las cajas de la franja (SGS MAT, SCM MAT, SAP) y de las alertas de material; un código sin precio aquí va al costo
    de SGS (provisional). Se rehace al terminar cada carga de SAP (fuente, fecha y carga para «¿De dónde sale?»)."""
    __tablename__ = "sgo7_gg_precio_mat"
    __table_args__ = (UniqueConstraint("material", name="uq_sgo7_gg_precio_mat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material: Mapped[str] = mapped_column(String(20))
    precio: Mapped[float] = mapped_column(Float)
    um: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    n: Mapped[int] = mapped_column(Integer, default=0)
    fuente: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    fecha: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    desde: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    hasta: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    detalle: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class GestionSonnyNodo(Base):
    """CHANGELOG 2026-10-25 — Un nodo de la «Lista Cruzada de Nodos» de Sonny (export manual por Contrato; lo sube cargar_sonny_gestion.py) con lo de «Nodos por
    gestion almacen» (mismo Id_nodo) y la confirmación de almacén del Historial de Actividades. Upsert por id_nodo. `codigo` es la columna Lcl
    (LCL 63… o, en mayor impacto, el ticket DF…). `id_planillas` vacío = el nodo no tiene planilla. `vigente` = vino en la última carga completa
    (los que Sonny ya no trae no se borran: dejan de contar). Sin Lider_cuadrilla ni Nombre_corto (personas)."""
    __tablename__ = "sgo7_gg_sonny_nodo"
    __table_args__ = (UniqueConstraint("id_nodo", name="uq_sgo7_gg_sonny_nodo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    id_nodo: Mapped[str] = mapped_column(String(20))
    codigo: Mapped[str] = mapped_column(String(20), index=True)
    codigos: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)      # CHANGELOG 2026-10-25: todos los LCL o DF de la columna Lcl (a veces trae dos)
    odm: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    pdl: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    qr: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    nodo: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    estado_nodo: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    id_planillas: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    estado_planilla: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    f_creacion: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    f_estado: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    estado_material: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    movimiento: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    f_almacen: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    n_almacen: Mapped[int] = mapped_column(Integer, default=0)
    id_gestion: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    f_confirmado: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    vigente: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    cargado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class GestionSonnyPlanAct(Base):
    """CHANGELOG 2026-10-25 — Lo que dice el «Historial de Actividades» de Sonny de cada planilla (el PC lee el texto y manda solo fechas y tipos de acción, sin
    personas): creación, último «Cambio el estado … a X» con su fecha, primera «Asignó la planilla … al <firmante>» después de ese cambio,
    cuántas firmas y de qué lado, y la última acción (crea / estado / firma / edita). Upsert por id_planilla."""
    __tablename__ = "sgo7_gg_sonny_plan_act"
    __table_args__ = (UniqueConstraint("id_planilla", name="uq_sgo7_gg_sonny_plan_act"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    id_planilla: Mapped[str] = mapped_column(String(40))
    f_creada: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    estado_act: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    f_estado_act: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    f_firma: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    n_firmas: Mapped[int] = mapped_column(Integer, default=0)
    roles: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    f_ultima: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    que_ultima: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    n_act: Mapped[int] = mapped_column(Integer, default=0)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    cargado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class GestionScmLcl(Base):
    """CHANGELOG 2026-10-25 — Una fila del exporte de LCL de SCM («lcl_export», Informe LCL/LCA): una LCL × Trabajo (= orden de SAP, llave exacta con la
    «Orden» del MB51) con el estado de la LCL (CONCLUSA = concluida según SCM; si eso equivale a pagada está por confirmar con FBL1N), las
    fechas de envío y de validez y el Importe. Upsert por (lcl, trabajo); lo sube cargar_scm_gestion.py. estado_desde = el corte del primer
    exporte cargado en que la LCL figura con ese estado (SCM no fecha el cambio de estado); vigente = el último exporte que trajo la LCL
    también traía este Trabajo (los demás no se borran: dejan de contar). Sin «Nombre técnico EDE», «Ejecutor» ni la descripción."""
    __tablename__ = "sgo7_gg_scm_lcl"
    __table_args__ = (UniqueConstraint("lcl", "trabajo", name="uq_sgo7_gg_scm_lcl"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lcl: Mapped[str] = mapped_column(String(20), index=True)
    trabajo: Mapped[str] = mapped_column(String(20), index=True)
    estado: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    estado_anterior: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    estado_desde: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fecha_envio: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    inicio_validez: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fin_validez: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    importe: Mapped[float] = mapped_column(Float, default=0)
    obra: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    acumulativa: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    tam: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    corte: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    vigente: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    cargado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class GestionDrapeFactura(Base):
    """CHANGELOG 2026-10-25 — Una factura de DRAPE (portal de facturación de Enel, lado proveedor): la lista en que está (borrador, enviada, rechazada,
    registrada, pagada), sus LCL («Num. Pedido»: la llave con Gestión; la orden de SAP no aparece en DRAPE), sus EM/HES, el registro de Enel y la
    fecha de pago; con el detalle, el vencimiento, el documento de pago y las condiciones. Upsert por DRAPE ID; datos cargados desde
    el PC. estado_desde = la carga en que se vio la lista actual; vigente = la última carga completa de su lista la trajo.
    Sin textos libres (pueden traer nombres), emisor, códigos fiscales ni datos bancarios."""
    __tablename__ = "sgo7_gg_drape_factura"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    drape_id: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    factura: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, index=True)
    estado: Mapped[str] = mapped_column(String(20), index=True)
    estado_anterior: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    estado_desde: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    destinatario: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    f_factura: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    n_registro: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    f_registro: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    f_pago: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    f_envio: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    f_creacion: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    pedidos: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    lcls: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    ems: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    total: Mapped[float] = mapped_column(Float, default=0)
    moneda: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    f_vence: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    doc_pago: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    condiciones: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    estado_detalle: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    total_neto: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_impuesto: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    n_pos: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    suma_pos: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pos_lcls: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    eventos: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    detalle_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    corte: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    vigente: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    carga_det_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cargado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class GestionDrapePos(Base):
    """CHANGELOG 2026-10-25 — Una línea del «Resumen de Posición» de una factura de DRAPE (LCL/posición, descripción, cantidad, precio e importe): el importe por
    LCL. Upsert por DRAPE ID + posición. Solo de las facturas cuyo detalle se abrió."""
    __tablename__ = "sgo7_gg_drape_pos"
    __table_args__ = (UniqueConstraint("drape_id", "posicion", name="uq_sgo7_gg_drape_pos"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    drape_id: Mapped[str] = mapped_column(String(30), index=True)
    factura: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    posicion: Mapped[str] = mapped_column(String(12))
    lcl: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    pedido_pos: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    producto: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    descripcion: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    unidad: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    cantidad: Mapped[float] = mapped_column(Float, default=0)
    precio: Mapped[float] = mapped_column(Float, default=0)
    impuesto: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    importe: Mapped[float] = mapped_column(Float, default=0)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    cargado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class GestionDrapeEm(Base):
    """CHANGELOG 2026-10-25 — Una EM (HES) pendiente de facturar en DRAPE («Entradas de bienes a facturar»), si el portal entrega la lista (puede venir
    solo el total). Upsert por EM; vigente = la última carga completa de la lista la trajo."""
    __tablename__ = "sgo7_gg_drape_em"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    em: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    fecha: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    importe: Mapped[float] = mapped_column(Float, default=0)
    lcl: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    empresa: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    moneda: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    corte: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    vigente: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    carga_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    cargado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    actualizado_en: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
