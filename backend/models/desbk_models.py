from datetime import datetime, date
from typing import Optional
from sqlalchemy import (
    String, Integer, BigInteger, Date, DateTime, Float,
    ForeignKey, Enum, Text, Boolean, func
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.database import Base
import enum


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
    respuesta_dev: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    creado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


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
