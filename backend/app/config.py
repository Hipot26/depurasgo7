import json
import re
import unicodedata

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    ENVIRONMENT: str = "production"
    AUDIT_ENABLED: bool = True   # historial de auditoría global; se apaga por entorno sin redeploy
    # Orígenes permitidos por CORS (separados por coma): el dominio público del frontend y el de desarrollo.
    CORS_ORIGINS: str = "http://localhost:5173"

    # ── SGS (solo lectura): relación de MO real liquidada en Programación ──────
    # Cuenta de lectura. Sin valores por defecto: se definen en el .env del servidor.
    SGS_URL: str = ""
    SGS_LOGIN: str = ""
    SGS_PASSWORD: str = ""

    # ── Proveedor de IA (clasifica mensajes, correos y videos del ANS) ─────────
    # La clave vive solo en el .env del servidor. Vacía, la consulta guiada pasa a modo manual.
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_MODEL: str = "claude-opus-4-8"
    # IA_PROVIDER: "anthropic" | "cloudflare" | "auto".
    # auto = proveedor principal y, si falla, Cloudflare Workers AI cuando está configurado
    # (endpoint compatible con OpenAI, ver services/ia_cf.py).
    IA_PROVIDER: str = "auto"
    CF_ACCOUNT_ID: str = ""
    CF_AI_TOKEN: str = ""
    CF_MODEL_TEXTO: str = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    CF_MODEL_VISION: str = "@cf/meta/llama-3.2-11b-vision-instruct"
    # AI Gateway: con nombre, las llamadas a Workers AI pasan por el gateway (registro por
    # llamada, costo visible, caché y reintentos). Vacío = directo a Workers AI.
    CF_GATEWAY: str = ""
    # Token de orquestación (R2, AI Gateway, Vectorize, Workers Scripts): solo scripts del servidor.
    CF_ORQ_TOKEN: str = ""
    # Recursos de la cuenta de Cloudflare que usan los scripts (scripts/r2_sync.py y
    # scripts/desplegar_worker_latido.py). Vacíos = el script correspondiente no opera.
    R2_BUCKET: str = ""            # bucket R2 del archivo de media y evidencias
    CF_WORKER_LATIDO: str = ""     # nombre del Worker de latido externo
    LATIDO_SALUD_URL: str = ""     # URL pública de /api/health que vigila el Worker
    # ── Correos ANS: periodos abiertos ─────────────────────────────────────────
    # El ANS de un mes se trabaja durante el mes siguiente. Un correo que llega para un caso
    # del periodo anterior que sigue en NO o vacío se analiza y se avisa. Cuántos periodos
    # hacia atrás del competente siguen abiertos (1 = solo el anterior).
    CORREOS_PERIODOS_ABIERTOS: int = 1
    # Aviso push (ntfy; solo PDL y periodo) cuando una lectura automática de correo propone
    # excluir un caso que está en NO o sin decidir. Vacío = sin aviso.
    NTFY_URL: str = ""
    AVISO_CORREO_SALVA_CASO: bool = True
    # ── Circuito automático de pedidos por WhatsApp ────────────────────────────
    # Tras analizar un caso sin material, el sistema pide la evidencia al líder o al apoyo
    # (con los mismos bloqueadores del botón) y recuerda a las 24 y 48 h; a las 72 h escala
    # al supervisor de la zona. Se apaga por entorno sin redeploy.
    WA_PEDIDOS_AUTO: bool = True
    WA_RECORDATORIOS: bool = True

    # ── Bot de WhatsApp (proceso Node aparte) ──────────────────────────────────
    # Token de servicio con el que el bot se autentica contra /api/wa-bot/*. Vacío = endpoints apagados.
    WHATSAPP_BOT_TOKEN: str = ""
    SGO7_INTERNAL_TOKEN: str = ""   # módulos SGO7 → POST /api/sgo7/registros (trazabilidad)
    SGO7_CRED_KEY: str = ""         # Fernet: cifra las cuentas SGS de los usuarios (sgo7_credenciales_sgs)

    # ── Habilitaciones por cuenta (logins separados por coma; vacío = nadie) ───
    # Las habilitaciones que no dependen del rol se asignan por login en el .env. Cada
    # variable alimenta el conjunto del mismo nombre más abajo; las funciones puede_* son la
    # única fuente de verdad para el backend (gates) y el frontend (campos de UsuarioOut).
    USUARIOS_ANS_EXTRA: str = ""
    USUARIOS_APOYO: str = ""
    USUARIOS_ING_ZONA_ANS: str = ""
    USUARIOS_APOYO_IMPORTA_PROG: str = ""
    USUARIOS_ANS_VE_TODO: str = ""
    USUARIOS_AUDITORIA_WA: str = ""
    USUARIOS_CORREOS_ANS: str = ""
    USUARIOS_EMERGENCIAL: str = ""
    USUARIOS_FACTURACION: str = ""
    USUARIOS_LIQUIDADOR: str = ""
    USUARIOS_SGS_EXPORTE: str = ""
    USUARIOS_NODOS: str = ""
    # Elaboradores (programación + cumplimentación), objeto JSON:
    # {"<login>": {"nombre": "<etiqueta visible>", "tokens": ["APELLIDO", "NOMBRE"]}, ...}
    ELABORADORES_JSON: str = ""
    USUARIOS_CONTROL_MENSUAL: str = ""
    USUARIOS_GESTION: str = ""
    GESTION_AGENTE_TOKEN: str = ""   # agente de correo de Gestión General (PC) → /api/gestion/agente/*

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


def _lista(valor) -> set:
    """Convierte «a,b,c» en {'a','b','c'} (minúsculas, sin espacios). Vacío → set()."""
    return {x.strip().lower() for x in (valor or "").split(",") if x.strip()}


def _objeto(valor) -> dict:
    """Parsea un objeto JSON de configuración. Vacío o inválido → {}."""
    try:
        d = json.loads(valor) if (valor or "").strip() else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


# ── Acceso al módulo ANS: única fuente de verdad ─────────────────────────────────
# Cuentas (por login) con acceso pleno al ANS aunque su rol no lo incluya: el directivo
# que supervisa el ANS, quienes lo diligencian (programadoras y la ingeniera de nodos) y el
# coordinador de programados. Se abre por cuenta y no por rol, para no dar el ANS a todos
# los `programador` / `ingeniero_zona` del sistema.
USUARIOS_ANS_EXTRA = _lista(settings.USUARIOS_ANS_EXTRA)

# ── Profesionales de apoyo (uno por zona) ────────────────────────────────────────
# El profesional de apoyo de una zona consigue el correo del Centro Operativo que explica
# una anulación (indicador 3.2 B). Va por cuenta y no por rol (su rol de login es
# `ingeniero_zona`, pero el resto de los ingenieros no son apoyo). La zona de cada apoyo no
# se escribe aquí: la zona de su cuenta y el directorio (tab Líderes → rol «apoyo» + zona,
# ver `_zona_apoyo_de` en ans.py) la determinan.
# Capacidades:
#   · Programación: ve todas las zonas, pero solo edita la suya (gate real:
#     _check_zona_access en programaciones.py; el grid bloquea las otras filas).
#   · ANS: entra a ver y sustentar sus casos (sin cargas masivas ni borrados).
USUARIOS_APOYO = _lista(settings.USUARIOS_APOYO)

# ── Ingenieros de zona (cuentas por zona) con vista del Cálculo ANS ─────────────
# Las cuentas de zona entran al Cálculo ANS con la misma vista completa que las
# programadoras (todas las zonas, todos los indicadores) y diligencian sus celdas, pero no
# son dueñas del periodo: sin cargas masivas (STM/prog20) ni borrados (gate real:
# deps.bloquear_ans_escritura; el frontend les oculta esos botones).
USUARIOS_ING_ZONA_ANS = _lista(settings.USUARIOS_ING_ZONA_ANS)


def es_ing_zona_ans(email) -> bool:
    """¿Es una cuenta de zona con vista del ANS? Fuente única para backend (deps) y
    frontend (campo `es_ing_zona_ans` de UsuarioOut, que oculta cargas y borrados)."""
    return email in USUARIOS_ING_ZONA_ANS


def es_profesional_apoyo(email) -> bool:
    """¿Esta cuenta es el profesional de apoyo de una zona? Fuente única para el
    backend (programaciones/ans) y el frontend (campo `es_apoyo_ans` de UsuarioOut)."""
    return email in USUARIOS_APOYO


def es_apoyo_ans(rol, email) -> bool:
    """¿Esta cuenta entra al ANS como profesional de apoyo? Su acceso es acotado: ve y
    diligencia, pero sin cargas masivas ni borrados. Fuente única para backend y frontend."""
    return es_profesional_apoyo(email)


# Apoyos que importan el Excel de programación de su zona (Exportar → «Importar Excel de
# programación»). Solo modo Inteligente (agrega filas nuevas y omite duplicados; nunca borra)
# y solo filas de la zona de su cuenta (usuarios.zona), como en Programación. Reemplazar zona
# y Reemplazar todo siguen siendo del coordinador. Fuente única para backend
# (api/import_excel) y frontend (campo `puede_importar_prog` de UsuarioOut).
USUARIOS_APOYO_IMPORTA_PROG = _lista(settings.USUARIOS_APOYO_IMPORTA_PROG)


# Roles que importan con los tres modos (Inteligente, Reemplazar zona, Reemplazar todo) y a
# la unidad que elijan: el coordinador y el desarrollador.
ROLES_IMPORTA_PROG_COMPLETO = {"supervisor", "desarrollador"}


def importa_prog_completo(rol) -> bool:
    """¿Importa la programación con todos los modos y a la unidad seleccionada? (supervisor y desarrollador)"""
    return getattr(rol, "value", str(rol)) in ROLES_IMPORTA_PROG_COMPLETO


def puede_importar_prog(rol, email) -> bool:
    """¿Ve y usa «Importar Excel de programación»? El coordinador y el desarrollador con todos
    los modos; los apoyos de USUARIOS_APOYO_IMPORTA_PROG solo su zona y en modo Inteligente
    (gate real en api/import_excel.py)."""
    return importa_prog_completo(rol) or email in USUARIOS_APOYO_IMPORTA_PROG

# Quién ve el ANS completo (todas las tareas de la campanita y el parpadeo de todos los
# roles, no solo el suyo). Es la vista de quien supervisa el diligenciamiento, no de quien lo
# llena. Por rol: dev, la cuenta ans y el directivo. Por cuenta: el coordinador de
# programados (es `supervisor`, y ese rol por sí solo no debe abrir el ANS de los demás).
ROLES_ANS_VE_TODO = ("desarrollador", "ans", "directivo")
USUARIOS_ANS_VE_TODO = _lista(settings.USUARIOS_ANS_VE_TODO)


def puede_ans(rol, email) -> bool:
    """¿Esta persona entra (y edita) el módulo ANS? Lo usan el backend (deps) y el frontend
    (vía el campo `puede_ans` de UsuarioOut), para que no haya listas duplicadas que se
    desincronicen: habilitar a alguien aquí lo habilita en los dos lados a la vez."""
    r = getattr(rol, "value", str(rol))
    return (r in ("desarrollador", "ans", "cumplimentador")
            or email in USUARIOS_ANS_EXTRA or es_apoyo_ans(rol, email)
            or es_ing_zona_ans(email))


# ── Elaboradores: programación + cumplimentación unificadas ──────────────────────
# Programadoras y cumplimentadores son un solo rol de diligenciamiento («elaborador») y la
# columna H (sigla del elaborador) dice de quién es cada fila: sus celdas de programación
# (3.4 W·Y·AA·AB + G·H) y de cumplimentación (AQ) parpadean solo para la cuenta cuyo nombre
# coincide con la sigla. Fila sin sigla (o de alguien fuera de la lista) queda sin dueño:
# parpadea para todos hasta que alguien la firme en la H.
#
# La sigla trae el nombre legal completo con la cédula entre paréntesis, así que el pareo es
# por tokens de palabra completa (con acentos normalizados), nunca por subcadena: un nombre
# corto no debe casar con otro que lo contenga.
# `nombre` es la etiqueta visible en los subfiltros por persona de la leyenda del ANS.
# Estructura (ELABORADORES_JSON): {"<login>": {"nombre": "...", "tokens": ["APELLIDO", "NOMBRE"]}}
ELABORADORES = _objeto(settings.ELABORADORES_JSON)


def es_elaborador(email) -> bool:
    """¿Esta cuenta es una de las del proceso programación+cumplimentación? El frontend
    lo recibe en UsuarioOut para filtrar «lo mío» por la sigla de la columna H."""
    return email in ELABORADORES


def cuenta_de_sigla(sigla) -> str | None:
    """Parsea la sigla de la columna H → login de la cuenta dueña de la fila (o None si
    está vacía o no corresponde a ningún elaborador). Única fuente del «quién es quién»."""
    s = unicodedata.normalize("NFD", str(sigla or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").upper()
    tokens = set(re.findall(r"[A-ZÑ]+", s))
    if not tokens:
        return None
    for email, e in ELABORADORES.items():
        if all(t in tokens for t in e["tokens"]):
            return email
    return None


# Tab «Auditoría IA» del ANS (el analista automático de WhatsApp): el dev y las cuentas
# habilitadas. Quien entra opera: corre análisis, guarda números en el directorio, encola
# mensajes al bot y aplica observaciones.
USUARIOS_AUDITORIA_WA = _lista(settings.USUARIOS_AUDITORIA_WA)


def puede_auditoria_wa(rol, email) -> bool:
    """¿Ve y opera el tab Auditoría IA? Única fuente para backend (gate del router) y
    frontend (campo de UsuarioOut), como puede_ans/puede_nodos."""
    r = getattr(rol, "value", str(rol))
    return r == "desarrollador" or email in USUARIOS_AUDITORIA_WA


# Tab «Correos ANS» (correos etiquetados en el buzón vigilado, cazados a sus casos): el dev
# y las cuentas habilitadas. Quien entra opera: adjunta, analiza con IA y aprueba exclusiones 3.2 B.
USUARIOS_CORREOS_ANS = _lista(settings.USUARIOS_CORREOS_ANS)


def puede_correos_ans(rol, email) -> bool:
    """¿Ve y opera el tab Correos ANS? Misma mecánica de fuente única."""
    r = getattr(rol, "value", str(rol))
    return r == "desarrollador" or email in USUARIOS_CORREOS_ANS


# «ES EMERGENCIAL» a mano (columna U de la DATA): la fórmula lo saca de la causal (FT
# «Programada Sin Preaviso» / ST «ACC»), pero hay casos que la causal no refleja. Lo ajustan
# solo el dev y las cuentas habilitadas.
USUARIOS_EMERGENCIAL = _lista(settings.USUARIOS_EMERGENCIAL)


def puede_editar_emergencial(rol, email) -> bool:
    r = getattr(rol, "value", str(rol))
    return r == "desarrollador" or email in USUARIOS_EMERGENCIAL


# Cuentas extra con el panel de facturación (además del rol facturacion).
USUARIOS_FACTURACION = _lista(settings.USUARIOS_FACTURACION)


def puede_facturacion(rol, email) -> bool:
    """¿Entra a la línea de facturación de SGO7 (panel /facturacion + módulo Balance)? El rol
    `facturacion` y el desarrollador. Única fuente para la puerta nginx (auth.verify balance),
    el backend (casos/órdenes) y el frontend (`puede_facturacion`)."""
    r = getattr(rol, "value", str(rol))
    return r in ("desarrollador", "facturacion") or email in USUARIOS_FACTURACION


# Liquidador Automático (módulo SGO7): cuentas habilitadas además del desarrollador.
USUARIOS_LIQUIDADOR = _lista(settings.USUARIOS_LIQUIDADOR)


def puede_liquidador(rol, email) -> bool:
    """¿Entra al Liquidador Automático (módulo SGO7)? dev + cuentas habilitadas.
    Única fuente para la puerta nginx (auth.verify), el backend (registros) y el frontend
    (campo `puede_liquidador` de UsuarioOut). Cada quien liquida con su propia cuenta SGS."""
    r = getattr(rol, "value", str(rol))
    return r == "desarrollador" or email in USUARIOS_LIQUIDADOR


# Módulo «SGS Exporte» (solo consulta del universo completo liquidado en SGS): el dev y las
# cuentas habilitadas, con la misma vista completa del dev. No abre la Relación con SGS
# (relacionar/aplicar, que escribe MO real): esa sigue en deps.require_sgs_access.
USUARIOS_SGS_EXPORTE = _lista(settings.USUARIOS_SGS_EXPORTE)


def puede_sgs_exporte(rol, email) -> bool:
    """¿Ve el módulo SGS Exporte? Única fuente para backend (deps.require_sgs_exporte) y
    frontend (campo `puede_sgs_exporte` de UsuarioOut), como puede_ans/puede_nodos."""
    r = getattr(rol, "value", str(rol))
    return r == "desarrollador" or email in USUARIOS_SGS_EXPORTE


# Apartado «Nodos» del ANS (cruce con la Programación diaria de nodos): solo quien lleva los
# nodos, el directivo y el dev. A propósito no se abre a todo USUARIOS_ANS_EXTRA: las
# programadoras no llevan los nodos y no deben ver esta bandeja.
USUARIOS_NODOS = _lista(settings.USUARIOS_NODOS)


def puede_nodos(rol, email) -> bool:
    """¿Ve el apartado Nodos (subir la Programación diaria + aprobar el cruce 3.2C caso a
    caso)? Igual que `puede_ans`: única fuente de verdad para backend y frontend."""
    r = getattr(rol, "value", str(rol))
    return r == "desarrollador" or email in USUARIOS_NODOS


def ans_ve_todo(rol, email) -> bool:
    """¿Ve el ANS de todos los roles (campanita completa + parpadeo de los 4 colores)?
    Esto no incluye «¿A quién pregunto?» ni el chat con IA: eso sigue siendo solo del
    desarrollador (`rol == desarrollador`), a propósito."""
    r = getattr(rol, "value", str(rol))
    return r in ROLES_ANS_VE_TODO or email in USUARIOS_ANS_VE_TODO


def rol_pend_ans(rol, email) -> str:
    """Rol efectivo de diligenciamiento dentro del ANS (campanita, leyenda y parpadeo).
    Programador y cumplimentador son un solo rol («elaborador»). El rol de login
    ingeniero_zona se divide: quien lleva los nodos (puede_nodos) conserva 'ingeniero_zona',
    porque su tarea 3.2 C es solo suya; el resto (cuentas de zona y profesionales de apoyo)
    son 'ing_zona': la misma tarea del encargado ANS pero acotada a los casos de su zona."""
    r = getattr(rol, "value", str(rol))
    if r in ("programador", "cumplimentador"):
        return "elaborador"
    if r == "ingeniero_zona" and not puede_nodos(rol, email):
        return "ing_zona"
    return r


# ── Control Mensual de Liquidación (/verificador/) por cuenta ─────────────────────
# El desarrollador y las cuentas de USUARIOS_CONTROL_MENSUAL. «Abrir en Outlook (PC)» sigue siendo solo del
# desarrollador (gate en el propio módulo).
USUARIOS_CONTROL_MENSUAL = _lista(settings.USUARIOS_CONTROL_MENSUAL)


def puede_control(rol, email) -> bool:
    """¿Entra al Control Mensual de Liquidación? Única fuente para la puerta (auth.verify), el backend y el
    frontend (campo `puede_control` de UsuarioOut)."""
    r = getattr(rol, "value", str(rol))
    return r == "desarrollador" or email in USUARIOS_CONTROL_MENSUAL


# ── Gestión General ────────────────────────────────────────────────────────────────
# Un caso por proyecto padre del ERP con el flujo de gestión (39 pasos, 3 pilares), su paso actual y sus alertas,
# armado desde correo, ERP, Sonny y los módulos de la plataforma. Hoy solo el rol desarrollador; cuentas extra por lista.
USUARIOS_GESTION = _lista(settings.USUARIOS_GESTION)
ROLES_GESTION = ("desarrollador",)


def puede_gestion(rol, email) -> bool:
    """¿Entra a Gestión General? Única fuente para backend (`require_gestion`), frontend (`puede_gestion`) y menú."""
    r = getattr(rol, "value", str(rol))
    return r in ROLES_GESTION or email in USUARIOS_GESTION


# Umbrales de Gestión General. Se cambian aquí y se recarga el proceso (HUP gracioso).
# Diferencia de material SCM > ERP por caso (pesos, a costo del ERP) por debajo de la cual no hay alerta:
# queda como «diferencias menores» en «¿De dónde sale?» de la caja SCM MAT.
GESTION_MAT_MINIMO = 100_000

# Material proyectado del ERP (liquidado + por liquidar) contra PMS: ± este % «cuadra»; más, «excede» (alerta si la
# diferencia también pasa GESTION_MAT_MINIMO). Material por liquidar cuya última salida de almacén tiene
# GESTION_MAT_DIAS_SIN_LIQUIDAR días hábiles o más: alerta informativa.
GESTION_MAT_EXCESO_PCT = 10
GESTION_MAT_DIAS_SIN_LIQUIDAR = 15

# Sonny por etapa. Nodo asignado o finalizado sin planilla hace GESTION_SONNY_DIAS_NODO días hábiles (desde la fecha
# de su estado en la Lista Cruzada); planilla en curso sin cambios hace GESTION_SONNY_DIAS_QUIETA días; planilla en
# firmas hace GESTION_SONNY_DIAS_FIRMAS días hábiles; solicitud de PMS en «5-Trabajos finalizados» hace
# GESTION_SONNY_DIAS_PMS5 días sin ninguna planilla (y de GESTION_MAT_MINIMO o más).
GESTION_SONNY_DIAS_NODO = 15
GESTION_SONNY_DIAS_QUIETA = 15
GESTION_SONNY_DIAS_FIRMAS = 5
GESTION_SONNY_DIAS_PMS5 = 30

# Cierre en SCM. LCL sin ningún libreto con su fin de validez a GESTION_SCM_DIAS_VENCE días calendario o menos (o ya
# pasado; con Importe de GESTION_MAT_MINIMO o más); LCL CONCLUSA cuyo Importe no da la MO de sus libretos vivos con
# ±GESTION_SCM_IMPORTE_PCT % (y la diferencia pasa GESTION_MAT_MINIMO).
GESTION_SCM_DIAS_VENCE = 30
GESTION_SCM_IMPORTE_PCT = 5

# Cobro (portal de facturación del cliente). LCL CONCLUSA sin factura viva con el último libreto autocertificado hace
# GESTION_DRAPE_DIAS_SIN_FACTURA días hábiles o más; factura registrada sin pago con el vencimiento pasado hace más de
# GESTION_DRAPE_DIAS_MORA días hábiles (vencimiento del detalle; si no se leyó, fecha de factura + GESTION_DRAPE_VENCE_DIAS);
# entrada de mercancía pendiente de facturar hace GESTION_DRAPE_DIAS_EM días hábiles.
GESTION_DRAPE_DIAS_SIN_FACTURA = 10
GESTION_DRAPE_DIAS_MORA = 0
GESTION_DRAPE_VENCE_DIAS = 90
GESTION_DRAPE_DIAS_EM = 15

# Vistas para directivos. GESTION_PAGO_CONFIRMADO: mientras sea False, «pagado» se muestra como «por confirmar», en gris,
# y no cuenta como cobro ni como deuda; True + recarga + recálculo cuando facturación confirme la fuente del pago.
# GESTION_CASO_EJEMPLO: el trabajo con el que se ilustra «¿Cómo fluye?».
GESTION_PAGO_CONFIRMADO = False
GESTION_CASO_EJEMPLO = "6301018885"

# GESTION_MOSTRAR_DRAPE = False: los datos del portal de facturación del cliente no aparecen en ninguna vista (fuentes,
# caja, cadena, qué falta, alertas, Excel, controles, «¿Cómo fluye?»); la etapa de facturación sale del informe de deuda
# de facturación y el pago queda «fuente por definir». Las tablas sgo7_gg_drape_* se conservan. True + recarga + recálculo
# lo devuelve todo.
GESTION_MOSTRAR_DRAPE = False
