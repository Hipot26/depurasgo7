"""Módulo ANS — automatiza el "Calculo_ANS_<Mes>.xlsx" que se hacía a mano.

Flujo:
  CD10 Programados (FT) / Línea Viva ST  ──ingesta+filtro──▶  ans_registros (hoja 'data')
  Bolsas (solo lectura)                  ──agregación────────▶  3.1 / 3.5 / 5.2
  ans_registros                          ──cómputo──────────▶  3.2 / 3.3 / 3.4 / Dinamica
                                         ──export openpyxl──▶  Calculo_ANS_<Mes>.xlsx

Gate: solo rol desarrollador (se monta dev-only en main.py).
Reutiliza el patrón núcleo+JSON y el estilo de export de `bolsas.py`.
"""
import io
import os
import re
import base64
import html as _html
import json
import asyncio
import zipfile
import unicodedata
import shutil
import hashlib
import tempfile
import subprocess
from datetime import date, datetime, timedelta, time, timezone as _tz
from collections import defaultdict, Counter
from typing import Optional

from fastapi import APIRouter, Depends, Query, UploadFile, File, Form, Body, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, delete, event
from sqlalchemy.orm import Session as _SyncSession, with_loader_criteria
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter, column_index_from_string as ci
from openpyxl.formula.translate import Translator
from openpyxl.workbook.properties import CalcProperties
from openpyxl.cell.cell import MergedCell

from app.config import (ans_ve_todo as _ans_ve_todo, puede_nodos as _puede_nodos,
                        es_apoyo_ans as _es_apoyo_ans)
from app.db.database import get_db
from app.api.deps import get_current_active_user
from app.models.models import (AnsRegistro, AnsAprobador, AnsPeriodo, AnsFuente, AnsBolsaSnapshot,
                                AnsCambio, AnsImpacto36, AnsProg20, AnsSubprocMap, AnsNodosCaso,
                                AnsConsulta, AnsConsultaMsg, AnsEvidencia,
                                Bolsa, Contacto, Programacion, Usuario, ZonaEnum)
from app.schemas.schemas import AnsRegistroUpdate, AnsAprobadorCreate
from app.services.claude import clasificar as _ia_clasificar, analizar_correo as _ia_correo, analizar_video as _ia_video
from app.services.correo import parse_msg as _parse_msg, parse_eml as _parse_eml, img_a_data_url as _img_url
from app.services.transcribe import transcribir_video
from app.db.ans_ctx import current_entorno, set_entorno

router = APIRouter(prefix="/ans", tags=["ans"])

# ── Aislamiento real/prueba ───────────────────────────────────────────────────────
# El router se monta dos veces (main.py): /api/ans (real) y /api/prueba/ans (prueba).
# Estos modelos llevan la columna `entorno`; un único listener filtra TODOS sus SELECT
# por el entorno del request, así el mismo código sirve a ambos sin mezclar datos.
_MODELOS_ENTORNO = (AnsRegistro, AnsPeriodo, AnsFuente, AnsCambio, AnsBolsaSnapshot, AnsImpacto36,
                    AnsProg20, AnsSubprocMap, AnsNodosCaso, AnsConsulta, AnsEvidencia)


@event.listens_for(_SyncSession, "do_orm_execute")
def _scope_ans_por_entorno(state):
    if state.is_select and not state.is_column_load and not state.is_relationship_load:
        ent = current_entorno()
        for M in _MODELOS_ENTORNO:
            state.statement = state.statement.options(
                with_loader_criteria(M, M.entorno == ent, include_aliases=True)
            )


async def usar_entorno_real():
    set_entorno("real")


async def usar_entorno_prueba():
    set_entorno("prueba")

# Carpeta de respaldo de archivos fuente (provenance). Vive fuera de backend/ para
# que el deploy (rsync sin --delete) no la borre.
FUENTES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "ans_fuentes"))

# ── Configuración / catálogos (constantes editables, estilo TIPOS_BOLSA) ─────────

ZONAS = ["UBATE", "ALMEIDAS", "GUAVIO"]          # zonas FT; "LV" es la 4ª columna (línea viva)
ZONAS_REPORTE = ZONAS + ["LV"]

# Mapeo columna-letra del CD10 → campo del núcleo, por tipo (leyenda hoja 'data' filas 1-2).
MAP_PROGRAMADOS = {  # LM / FT
    "protocolo": "D", "asociado_pdl": "E", "ri_jt": "T", "nombre_responsable": "AY",
    "sigla_elaborador": "DO", "descripcion": "G", "fecha_inicio": "I", "hora_inicio": "J",
    "fecha_fin": "K", "hora_fin": "L", "unidad_resp": "U", "elaborado_por": "BF",
    "estado_actual": "CB", "modif_consistencia": "CD", "causal": "CF",
    "maniobra_inicio": "DJ", "maniobra_fin": "DM",
}
MAP_ST = {  # LV / ST
    # nombre_responsable ← columna AQ del CD10 ST = la persona que ejecutó el trabajo
    # (nombre informal; AR es el "elaborado por" formal). Se trae tal cual del archivo;
    # donde el STM no lo traiga queda en blanco para que la programadora lo complete.
    "protocolo": "D", "asociado_pdl": "E", "ri_jt": "S", "nombre_responsable": "AQ",
    "sigla_elaborador": None, "descripcion": "G", "fecha_inicio": "I", "hora_inicio": "J",
    "fecha_fin": "K", "hora_fin": "L", "unidad_resp": "T", "elaborado_por": "AR",
    "estado_actual": "AV", "modif_consistencia": "AX", "causal": "BA",
    "maniobra_inicio": None, "maniobra_fin": "CB",
}

# Sigla elaborador de Línea Viva (col H): NO viene del CD10. Regla de Paola (validada al
# 100% en marzo–junio): si la descripción contiene "ZONA VII" → Andrea, si no → Yuli.
# Se usa como default al importar un ST nuevo; editable y preservado en re-importes.
LV_PROG_ANDREA = "ROJAS CONTRERAS LUZ ANDREA (CO20458308)"
LV_PROG_YULI = "AREVALO NARVAEZ YULI PAOLA (CO53064790)"


def _sigla_lv_auto(descripcion: Optional[str]) -> str:
    return LV_PROG_ANDREA if "ZONA VII" in (descripcion or "").upper() else LV_PROG_YULI


def _se_ejecuto_auto(estado: Optional[str]) -> Optional[str]:
    """SE EJECUTÓ (col AN): cerrado/completado → SI; anulado → NO. A los anulados con
    exclusión la programadora los pasa a N/A a mano ('buscar la exclusión')."""
    e = (estado or "").upper()
    if "CERRAD" in e or "COMPLET" in e or "EJECUTAD" in e:
        return "SI"
    if "ANULAD" in e or "AN. POR" in e or e.startswith("AN."):
        return "NO"
    return None


# 3.4 — "Fecha en que se debió validar" (col W). Deducido del último ANS que llenó Paola
# (Calculo_ANS_Julio): W = fecha de inicio MENOS 9 días hábiles (lun-vie; NO descuenta festivos
# — con festivos descontados sus números no cuadran). Verificado en 4 de 5 filas suyas.
# El sistema la pre-llena y queda EDITABLE: si un caso va distinto, se corrige a mano.
DIAS_HABILES_VALIDACION = 9


def _menos_habiles(d: date, n: int) -> date:
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:          # 0-4 = lunes a viernes
            n -= 1
    return d


def _debio_validar_auto(fecha_inicio) -> Optional[str]:
    """W = fecha de inicio − 9 días hábiles, en DD/MM/YYYY (formato de la hoja)."""
    f = _parse_dt(fecha_inicio)
    if not f:
        return None
    return _menos_habiles(f.date(), DIAS_HABILES_VALIDACION).strftime("%d/%m/%Y")


def _emerg_o_apoyo(rec: dict) -> bool:
    """Emergencial (FT 'Programada Sin Preaviso' / ST 'ACC') o apoyo a PDL (ST con
    asociado_pdl). En ambos W e Y van 'N/A' (no aplica tiempo de programación)."""
    t = rec.get("tipo")
    causal = (rec.get("causal") or "").strip()
    emerg = (t == "FT" and causal == "Programada Sin Preaviso") or (t == "ST" and causal == "ACC")
    apoyo = bool(t == "ST" and (rec.get("asociado_pdl") or "").strip())
    return emerg or apoyo

# Semilla de aprobadores ENEL (de "ENEL APROBADORES DE PDL.xlsx") — fallback si la tabla está vacía.
APROBADORES_SEED = [
    "BOCANEGRA GONZALEZ SARA YULIETH (CO1088313957)",
    "FONSECA SOTELDO GABRIEL JOSE (CO1080439615)",
    "GOMEZ CRUZADO OSWALDO MAURICIO (CO1095921511)",
    "GOMEZ OROZCO PAOLA ANDREA (CO1088304848)",
    "JARA ESTUPIÑAN JEFFERSON (CO1012412665)",
    "LOPEZ MARTINEZ ANDRES FELIPE (CO1095701224)",
    "MENDOZA GONZALEZ YERSON RAFAEL (CO1052404891)",
    "NOVOA CASAS DANNY FABIAN (CO1013586642)",
    "PARADA SANCHEZ DIEGO ANDRES (CO1098791353)",
    "PARRA GUSTAVO ANDRES (CO1012358558)",
    "ROA RODRIGUEZ JHON SEBASTIAN (CO1022383494)",
    "ROJAS TIBANA MAURICIO (CO80491965)",
    "SALAZAR DIAZ JHON ELBER (CO1052381319)",
    "SALCEDO ACOSTA JHASMIN ADRIANA (CO53041024)",
    "SUARIQUE ARCOS CARLOS ARTURO (CO80421001)",
]

# Supervisor (Nombre Responsable) → zona. Semilla deducida del pivote 'Dinamica'.
# Lo no mapeado queda sin zona → la programadora la asigna en el grid.
SUPERVISOR_ZONA = {
    "JAIRO MONROY": "ALMEIDAS", "OMAR HERNANDEZ": "ALMEIDAS", "WILLINTONG RODRIGUEZ": "ALMEIDAS",
    "DANIEL MORENO": "ALMEIDAS", "LUIS ROMERO": "ALMEIDAS",
    "CARLOS FONSECA": "GUAVIO", "FREDY BARRETO": "GUAVIO", "MAICOL CARVAJAL": "GUAVIO",
    "DANIEL PARRA": "GUAVIO",
    "JULIAN ALVAREZ": "UBATE", "NELSON MEDIORREAL": "UBATE", "JUAN CARLOS MEJIA": "UBATE",
    "VICTOR GUERRERO": "UBATE", "JHON OROSCO": "UBATE",
    # Pedido de Paola (doc AJUSTES PENDIENTES): David Linares es sup. de UBATE para
    # línea muerta (FT). Sus ST siguen contando en LV (la regla ST→LV manda).
    "JESUS DAVID LINARES": "UBATE", "DAVID LINARES": "UBATE",
}

# tipo de bolsa → concepto de 3.1 (cumplimiento ejecución) y proceso de 3.5 (envío).
BOLSA_CONCEPTO_31 = {
    "IPPE": "IPPE", "CUN100": "CUN100", "PQR": "PQR - BOCATOMA", "BOCATOMA": "PQR - BOCATOMA",
    "BP": "BOLETINES", "TUTELA": "TUTELAS",
}
CONCEPTOS_31 = ["IPPE", "CUN100", "PQR - BOCATOMA", "BOLETINES", "TUTELAS"]
BOLSA_PROCESO_35 = {
    "PQR": "PQR", "BOCATOMA": "PQR", "AMP": "AMP", "IPPE": "IPPE", "CUN100": "CUND 100",
    "TLC": "TLC", "TUTELA": "TUTELAS",
}
PROCESOS_35 = ["PQR", "AMP", "INVR", "IPPE", "CUND 100", "TLC", "TUTELAS"]


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _norm_nombre(s) -> str:
    """Nombre normalizado para CUALQUIER cruce de personas: sin sufijo «(CÓDIGO)», sin tildes
    (NFKD), espacios colapsados, MAYÚSCULAS. ÚNICA definición — llegó a haber DOS funciones con
    este nombre (ésta, que quitaba el código, y otra más abajo para contactos, que no lo quitaba)
    y Python dejó viva la segunda EN SILENCIO: los aprobadores del seed (con «(CO…)») dejaron de
    cruzar con el «elaborado por» de los ST (sin código) y Línea Viva importó 0 filas durante
    semanas (bug jul-2026). Si necesitas otra normalización, PONLE OTRO NOMBRE."""
    s = re.sub(r"\(.*?\)", "", str(s or ""))
    s = unicodedata.normalize("NFKD", s.strip().upper())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip()


def _es_estado_objetivo(e) -> bool:
    e = str(e or "").strip().lower()
    return any(k in e for k in ("cerrad", "anulad", "an. por", "anu", "complet"))


def _es_anulado(e) -> bool:
    e = str(e or "").strip().lower()
    return ("anulad" in e) or ("an. por" in e) or ("anu" in e)


def _cell(v):
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y %H:%M") if (v.hour or v.minute) else v.strftime("%d/%m/%Y")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, (int, float)):
        return v
    return str(v).strip()


def _bog(dt):
    """Datetime naive-UTC (func.now() del Postgres en docker) → hora de Bogotá (UTC-5, sin
    horario de verano). SOLO para mostrar fechas al usuario: `al_dia` y el corte del correo
    hacen su propia resta y no deben pasar por aquí (se restaría dos veces)."""
    return dt - timedelta(hours=5) if dt else None


def _txt(v) -> Optional[str]:
    if v is None:
        return None
    s = _cell(v)
    s = str(s).strip()
    return s or None


def _parse_dt(s):
    """'01/06/2026 08:21' o ('01/06/2026','08:30') → datetime, o None."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    if isinstance(s, date):
        return datetime(s.year, s.month, s.day)
    s = str(s).strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _planeada(fecha, hora):
    f = _parse_dt(fecha)
    if not f:
        return None
    h = str(hora or "").strip()
    m = re.match(r"(\d{1,2}):(\d{2})", h)
    if m:
        return f.replace(hour=int(m.group(1)), minute=int(m.group(2)))
    return f


async def _aprobadores_set(db: AsyncSession) -> set:
    rows = (await db.execute(select(AnsAprobador.nombre).where(AnsAprobador.activo == True))).scalars().all()
    fuente = rows if rows else APROBADORES_SEED
    return {_norm_nombre(n) for n in fuente}


async def _zona_map(db: AsyncSession) -> dict:
    """{nombre_norm: ZONA} para deducir la zona del responsable. Combina la SEMILLA (mapa fijo del
    código) con lo APRENDIDO en el directorio de contactos (supervisores/apoyo con zona) — y el
    directorio manda sobre la semilla. Así, agregar o corregir un supervisor es editar datos, no
    código: resiste renuncias, traslados y nombres nuevos sin redesplegar."""
    m = {_norm_nombre(k): v for k, v in SUPERVISOR_ZONA.items()}
    r = await db.execute(select(Contacto).where(Contacto.activo.is_(True), Contacto.zona.isnot(None)))
    for c in r.scalars().all():
        if (c.zona or "").strip():
            m[_norm_nombre(c.nombre)] = c.zona.strip().upper()
    return m


def _zona_de(tipo: str, nombre_responsable: Optional[str], zmap: Optional[dict] = None) -> Optional[str]:
    if tipo == "ST":
        return "LV"
    if nombre_responsable:
        return (zmap if zmap is not None else SUPERVISOR_ZONA).get(_norm_nombre(nombre_responsable))
    return None


# ── ETL: parsear un CD10 a registros (aplicando el filtro) ───────────────────────

def _detectar_tipo_cd10(wb):
    """FT vs ST por el PROTOCOLO (columna D en ambos formatos): los Programados traen
    CD0PP…/PP####R y los de Línea Viva CD0PT…/PT####R. Devuelve ("FT"|"ST"|None, n_ft, n_st).
    Nació del bug de 2026-07: los ST subidos por el botón de CD10 importaban 0 filas en
    silencio (el filtro DOMINION caía en otra columna) y julio quedó sin Línea Viva."""
    ws = wb["Hoja1"] if "Hoja1" in wb.sheetnames else wb[wb.sheetnames[0]]
    n_ft = n_st = 0
    for r in ws.iter_rows(min_row=2, max_row=500, min_col=4, max_col=4, values_only=True):
        v = str(r[0] or "").upper()
        if re.search(r"PP\d{4}R", v):
            n_ft += 1
        elif re.search(r"PT\d{4}R", v):
            n_st += 1
    if n_ft > n_st:
        return "FT", n_ft, n_st
    if n_st > n_ft:
        return "ST", n_ft, n_st
    return None, n_ft, n_st


def _parse_cd10(wb, tipo: str, aprobadores: set, zmap: Optional[dict] = None) -> list[dict]:
    """tipo: 'FT' (Programados) o 'ST' (Línea Viva). Devuelve filas que pasan el
    filtro Dominion + aprobador + estado objetivo, ya mapeadas al núcleo."""
    mp = MAP_PROGRAMADOS if tipo == "FT" else MAP_ST
    ws = wb["Hoja1"] if "Hoja1" in wb.sheetnames else wb[wb.sheetnames[0]]
    out: list[dict] = []
    # Cabecera en fila 1; datos desde la 2.
    headers = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None) or ()
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r:
            continue

        def col(letter):
            idx = ci(letter) - 1
            return r[idx] if (letter and idx < len(r)) else None

        protocolo = _txt(col(mp["protocolo"]))
        if not protocolo:
            continue
        unidad_resp = str(col(mp["unidad_resp"]) or "")
        elaborado = _norm_nombre(col(mp["elaborado_por"]))
        estado = col(mp["estado_actual"])
        # Filtro de ingesta
        if "DOMINION" not in unidad_resp.upper():
            continue
        if elaborado not in aprobadores:
            continue
        if not _es_estado_objetivo(estado):
            continue

        rec: dict = {"tipo": tipo, "protocolo": protocolo[:120]}
        for campo, letra in mp.items():
            if campo in ("protocolo",) or not letra:
                continue
            rec[campo] = _txt(col(letra))
        rec["pdl"] = protocolo.split("/")[0].strip()[:80]
        rec["zona"] = _zona_de(tipo, rec.get("nombre_responsable"), zmap)
        # Fila cruda para datos_extra (no perder nada)
        cruda = {}
        for j, val in enumerate(r):
            if j < len(headers) and headers[j] and val is not None and str(val).strip():
                cruda[str(headers[j]).strip()] = _cell(val)
        rec["datos_extra"] = cruda
        out.append(rec)
    return out


# ── Cómputo de columnas derivadas (réplica de las fórmulas del Excel) ─────────────

CAMPOS_MANUALES = [
    "fecha_debio_validar", "fecha_envio_validar", "excluible_tiempos", "obs_tiempos",
    "excluible", "obs", "se_ejecuto", "excluible_anulado", "obs_anulado",
    "nombre_cumplimentador", "obs_uucc",
    "excluible2", "obs3", "nodos_planeados", "nodos_ejecutados", "nods_excluible", "obs_nodos",
]


def computar(reg: "AnsRegistro | dict") -> dict:
    """Columnas U..AZ de la hoja 'data', calculadas en Python igual que las fórmulas."""
    g = (lambda k: reg.get(k)) if isinstance(reg, dict) else (lambda k: getattr(reg, k, None))
    tipo = g("tipo")
    causal = (g("causal") or "").strip()
    es_ft, es_st = tipo == "FT", tipo == "ST"

    es_emergencial = (es_ft and causal == "Programada Sin Preaviso") or (es_st and causal == "ACC")
    apoyo_pdl = bool(es_st and (g("asociado_pdl") or "").strip())

    def cumple_lado(fecha, hora, real):
        plan = _planeada(fecha, hora)
        rdt = _parse_dt(real)
        if es_emergencial or plan is None or rdt is None:
            return "N/A", None
        desfase = round((rdt - plan).total_seconds() / 60)
        return ("CUMPLE" if desfase <= 10 else "NO CUMPLE"), desfase

    cumple_ap, desf_ap = cumple_lado(g("fecha_inicio"), g("hora_inicio"), g("maniobra_inicio"))
    cumple_ci, desf_ci = cumple_lado(g("fecha_fin"), g("hora_fin"), g("maniobra_fin"))

    # AK: cumple tiempos (apertura+cierre)
    if apoyo_pdl:
        cumple_tiempos = "N/A"
    elif cumple_ap == "CUMPLE" and cumple_ci == "CUMPLE":
        cumple_tiempos = "CUMPLE"
    elif {cumple_ap, cumple_ci} <= {"CUMPLE", "N/A"} and "CUMPLE" in (cumple_ap, cumple_ci):
        cumple_tiempos = "CUMPLE"
    elif cumple_ap == "N/A" and cumple_ci == "N/A":
        cumple_tiempos = "N/A"
    elif (g("excluible") or "").upper() == "SI":
        cumple_tiempos = "N/A"
    else:
        cumple_tiempos = "NO CUMPLE"

    # Z: cumple tiempo de programación (3.4)
    debio_validar = _parse_dt(g("fecha_debio_validar"))
    envio = _parse_dt(g("fecha_envio_validar"))
    if envio is None or (g("excluible_tiempos") or "").upper() == "SI" or debio_validar is None:
        cumple_tprog = "N/A"
    else:
        cumple_tprog = "CUMPLE" if envio <= (debio_validar - timedelta(days=1)) else "NO CUMPLE"

    # AS: cumple UUCC (3.3)
    modif = (g("modif_consistencia") or "").strip().lower()
    obs_uucc = (g("obs_uucc") or "").strip()
    if modif == "no" and not obs_uucc:
        cumple_uucc = "N/A"
    elif (g("excluible2") or "").upper() == "SI":
        cumple_uucc = "CUMPLE"
    elif not obs_uucc:
        cumple_uucc = "CUMPLE"
    else:
        cumple_uucc = "NO CUMPLE"

    return {
        "es_emergencial": es_emergencial, "apoyo_pdl": apoyo_pdl,
        "cumple_apertura": cumple_ap, "desfase_apertura": desf_ap,
        "cumple_cierre": cumple_ci, "desfase_cierre": desf_ci,
        "cumple_tiempos": cumple_tiempos, "cumple_tprog": cumple_tprog,
        "cumple_uucc": cumple_uucc,
    }


def _justificar(reg: AnsRegistro, fuente_nombre: Optional[str]) -> dict:
    """Devuelve, por campo, la justificación de su valor: de qué archivo/columna del STM
    salió, qué regla de automatización lo decidió, o si es un campo manual. Para el popup
    'por qué este dato' en el grid → rectificación más precisa."""
    tipo = reg.tipo
    mp = MAP_PROGRAMADOS if tipo == "FT" else MAP_ST
    tlbl = "Programados (FT)" if tipo == "FT" else "Línea Viva (ST)"
    desde = f"del archivo «{fuente_nombre}»" if fuente_nombre else f"del CD10 de {tlbl}"
    c = computar(reg)
    g = lambda k: getattr(reg, k, None)
    J: dict = {}

    def fuente(campo):
        letra = mp.get(campo)
        col = f", columna {letra} del STM" if letra else ""
        return {"origen": "fuente", "texto": f"Se trajo tal cual {desde}{col}."}

    # Campos directos del CD10
    for campo in ("protocolo", "asociado_pdl", "ri_jt", "descripcion", "fecha_inicio", "hora_inicio",
                  "fecha_fin", "hora_fin", "unidad_resp", "elaborado_por", "estado_actual",
                  "modif_consistencia", "causal", "maniobra_inicio", "maniobra_fin"):
        if mp.get(campo):
            J[campo] = fuente(campo)
    if tipo == "FT" and mp.get("sigla_elaborador"):
        J["sigla_elaborador"] = fuente("sigla_elaborador")

    J["pdl"] = {"origen": "derivado", "texto": "Derivado del protocolo: el texto antes de «/»."}
    J["tipo"] = {"origen": "derivado", "texto": f"«{tipo}»: el protocolo contiene «{'PP2026R' if tipo == 'FT' else 'PT'}»."}

    # Zona
    if tipo == "ST":
        J["zona"] = {"origen": "derivado", "texto": "«LV» porque es un trabajo de Línea Viva (ST)."}
    else:
        J["zona"] = {"origen": "derivado",
                     "texto": f"«{g('zona') or '—'}» derivada del responsable «{g('nombre_responsable') or '—'}» (mapa supervisor→zona)."}

    # Sigla elaborador en ST (regla ZONA VII)
    if tipo == "ST":
        z7 = "ZONA VII" in (g("descripcion") or "").upper()
        regla = (f"La descripción {'SÍ' if z7 else 'NO'} contiene «ZONA VII» → "
                 f"{'Andrea (Rojas)' if z7 else 'Yuli (Arévalo)'}.")
        val = (g("sigla_elaborador") or "").strip()
        if val and val != _sigla_lv_auto(g("descripcion")):
            J["sigla_elaborador"] = {"origen": "manual",
                "texto": f"El valor guardado «{val}» no coincide con la regla (que daría: {regla.rstrip('.')}). Puede ser una corrección manual o un dato cargado antes de aplicar la regla."}
        else:
            J["sigla_elaborador"] = {"origen": "auto", "texto": f"Asignado automático. {regla} (Editable si lo montó otra persona.)"}

    # Responsable: del STM (columna AY en FT, AQ en ST) donde el archivo lo traiga; manual si vacío
    if g("nombre_responsable"):
        J["nombre_responsable"] = fuente("nombre_responsable")
    else:
        J["nombre_responsable"] = {"origen": "manual", "texto": "El STM no trajo responsable en esta fila; la programadora escribe quién montó el trabajo."}

    # SE EJECUTÓ
    est = g("estado_actual"); auto_se = _se_ejecuto_auto(est); se = (g("se_ejecuto") or "").strip()
    if se:
        if auto_se and se.upper() == auto_se:
            por = "cerrado/completado → se ejecutó" if auto_se == "SI" else "anulado → no se ejecutó"
            J["se_ejecuto"] = {"origen": "auto", "texto": f"«{se}» automático porque el Estado es «{est}» ({por})."}
        else:
            J["se_ejecuto"] = {"origen": "manual", "texto": f"«{se}» ajustado a mano (p. ej. anulado con exclusión → N/A)."}

    # W / Y (debió/envió validar)
    causal = (g("causal") or "")
    emerg = (tipo == "FT" and causal == "Programada Sin Preaviso") or (tipo == "ST" and causal == "ACC")
    apoyo = bool(tipo == "ST" and (g("asociado_pdl") or "").strip())
    motivo = "emergencial" if emerg else ("apoyo a PDL" if apoyo else None)
    for campo in ("fecha_debio_validar", "fecha_envio_validar"):
        v = g(campo)
        if str(v) == "N/A" and motivo:
            J[campo] = {"origen": "auto", "texto": f"«N/A» porque la actividad es {motivo} → no aplica tiempo de programación."}
        elif campo == "fecha_debio_validar":
            # La llena la PROGRAMADORA; el sistema ya NO la calcula. Se ofrece la regla habitual
            # solo como referencia por si le sirve, no como un valor puesto por el sistema.
            referencia = _debio_validar_auto(g("fecha_inicio"))
            ref_txt = (f" Como referencia, lo habitual es inicio ({g('fecha_inicio')}) − "
                       f"{DIAS_HABILES_VALIDACION} días hábiles = {referencia}.") if referencia else ""
            if v:
                J[campo] = {"origen": "manual", "texto": "La diligencia la programadora." + ref_txt}
            else:
                J[campo] = {"origen": "manual", "texto": "Pendiente: la llena la programadora." + ref_txt}
        elif v:
            J[campo] = {"origen": "manual", "texto": "Fecha en que se envió realmente la programación a validar (dato de correo/SharePoint)."}
        else:
            J[campo] = {"origen": "manual", "texto": "Pendiente: la fecha en que se envió la programación a validar."}

    # Calculados (fórmulas de indicadores)
    J["es_emergencial"] = {"origen": "calculo",
        "texto": ("«Sí»: " + ("FT con causal «Programada Sin Preaviso»." if tipo == "FT" else "ST con causal «ACC».")) if c["es_emergencial"] else "«No»: la causal no es de emergencia."}
    J["apoyo_pdl"] = {"origen": "calculo",
        "texto": "«Sí»: es ST y tiene «Asociado a PdL»." if c["apoyo_pdl"] else "«No»: no es apoyo a PDL."}

    def cmp_lado(lado, pf, ph, real, ck, dk):
        if c[ck] == "N/A":
            return {"origen": "calculo", "texto": f"«N/A»: faltan datos de {lado} o la actividad es emergencial."}
        return {"origen": "calculo",
                "texto": f"Planeada {g(pf)} {g(ph)} vs real {g(real)} → desfase {c[dk]} min ({'≤10 = CUMPLE' if c[ck] == 'CUMPLE' else '>10 = NO CUMPLE'})."}
    J["cumple_apertura"] = cmp_lado("apertura", "fecha_inicio", "hora_inicio", "maniobra_inicio", "cumple_apertura", "desfase_apertura")
    J["cumple_cierre"] = cmp_lado("cierre", "fecha_fin", "hora_fin", "maniobra_fin", "cumple_cierre", "desfase_cierre")
    J["desfase_apertura"] = {"origen": "calculo", "texto": "Minutos entre la maniobra real de apertura y la planeada."}
    J["desfase_cierre"] = {"origen": "calculo", "texto": "Minutos entre la maniobra real de cierre y la planeada."}
    J["cumple_tiempos"] = {"origen": "calculo", "texto": f"«{c['cumple_tiempos']}»: combina el cumplimiento de apertura y cierre (3.2)."}
    J["cumple_tprog"] = {"origen": "calculo", "texto": f"«{c['cumple_tprog']}»: compara la fecha de envío vs la fecha en que se debió enviar (debió validar − 1 día) (3.4)."}
    J["cumple_uucc"] = {"origen": "calculo", "texto": f"«{c['cumple_uucc']}»: según la devolución de unidades constructivas / observación UUCC (3.3)."}

    J.setdefault("nombre_cumplimentador", {"origen": "manual", "texto":
        "Quién cumplimentó la orden en el sistema de ENEL (lo escriben los cumplimentadores; "
        "con él la dinámica 3.3 atribuye las devoluciones por persona)."})
    # Resto de manuales (exclusiones, observaciones, nodos)
    for campo in ("excluible_tiempos", "obs_tiempos", "excluible", "obs", "excluible_anulado",
                  "obs_anulado", "obs_uucc", "excluible2", "obs3",
                  "nodos_planeados", "nodos_ejecutados", "nods_excluible", "obs_nodos"):
        J.setdefault(campo, {"origen": "manual", "texto": "Campo manual que diligencia la programadora."})
    return J


# ══════════════════════════════════════════════════════════════════════════════
#  REGLAS AUTOMÁTICAS DE NODOS (3.2 C) — patrones descifrados de la auditoría real
#  (jul-2026): María Inés llena a su disposición y la máquina ajusta sola:
#   1. «anulado_excluido» (patrón Sergio 23-jul): orden ANULADA con exclusión 3.2 B
#      aprobada (AO=SI) y sin ejecutar → nodos PLANEADOS (AV) a 0, para que la orden
#      excluida no pese en el 3.2 C.
#   2. «cerrado_igualado» (patrón Santana 23-jul): orden que SÍ se ejecutó (AN=SI) y
#      sin exclusión, con ejecutados < planeados → EJECUTADOS (AW) se igualan a AV.
#  La marca queda en reg.nodos_auto y el grid la pinta AMARILLO (solo en el sistema,
#  nunca en el exporte). Si la condición que justificó el ajuste deja de cumplirse
#  (p. ej. quitan la exclusión), la máquina NO revierte: pinta ROJO para revisión.
#  Si un humano cambia la celda a otro valor, la marca muere sola.
# ══════════════════════════════════════════════════════════════════════════════

def _eval_reglas_nodos(reg: AnsRegistro) -> list:
    """Evalúa/aplica las 2 reglas sobre reg y mantiene reg.nodos_auto.
    Devuelve [(campo, antes, después)] de lo que cambió (para el historial)."""
    excl = (reg.excluible_anulado or "").strip().upper() == "SI"
    se = (reg.se_ejecuto or "").strip().upper()
    au, av = reg.nodos_planeados, reg.nodos_ejecutados
    try:
        marca = json.loads(reg.nodos_auto) if reg.nodos_auto else None
    except (ValueError, TypeError):
        marca = None
    cambios = []

    # ¿Sigue vigente la marca previa?
    if marca:
        if _s(getattr(reg, marca.get("campo", ""), None)) != _s(marca.get("valor")):
            marca = None            # un humano cambió la celda a otro valor → marca fuera
        else:
            cond = (excl and se in ("NO", "N/A", "")) if marca.get("regla") == "anulado_excluido" \
                else (se == "SI" and not excl)
            marca["estado"] = "ok" if cond else "invalida"

    # Aplicar (solo si no hay una marca en rojo esperando revisión humana)
    if marca is None or marca.get("estado") == "ok":
        if excl and se in ("NO", "N/A", "") and (au or 0) != 0 and (av or 0) == 0:
            marca = {"regla": "anulado_excluido", "campo": "nodos_planeados", "antes": au, "valor": 0,
                     "estado": "ok", "cuando": datetime.now().isoformat(timespec="seconds"),
                     "detalle": (f"Orden anulada con exclusión 3.2 B aprobada (AO=SI) y sin ejecutar: "
                                 f"el sistema puso los nodos planeados en 0 (antes {au}) para que la "
                                 f"orden excluida no pese en el 3.2 C.")}
            reg.nodos_planeados = 0
            cambios.append(("nodos_planeados", au, 0))
        elif se == "SI" and not excl and au is not None and av is not None and av < au:
            marca = {"regla": "cerrado_igualado", "campo": "nodos_ejecutados", "antes": av, "valor": au,
                     "estado": "ok", "cuando": datetime.now().isoformat(timespec="seconds"),
                     "detalle": (f"La orden se ejecutó (AN=SI) sin exclusión y los nodos ejecutados ({av}) "
                                 f"eran menores que los planeados ({au}): el sistema los igualó a lo "
                                 f"planeado, como se hace en el 3.2 C.")}
            reg.nodos_ejecutados = au
            cambios.append(("nodos_ejecutados", av, au))

    reg.nodos_auto = json.dumps(marca, ensure_ascii=False) if marca else None
    return cambios


def _log_reglas_nodos(db, reg: AnsRegistro, cambios: list):
    """Deja en el historial (AnsCambio) lo que las reglas cambiaron solas — reversible
    y auditable igual que una edición humana, pero firmado por el sistema."""
    for campo, antes, despues in cambios:
        db.add(AnsCambio(registro_id=reg.id, periodo=reg.periodo, protocolo=reg.protocolo,
                         campo=campo, valor_anterior=_s(antes), valor_nuevo=_s(despues),
                         tipo="edicion", usuario_id=None, usuario_nombre="Sistema (reglas nodos)"))


def _pend_campos(reg: AnsRegistro) -> dict:
    """{campo: rol} de las celdas que a alguien le FALTA llenar en esta fila. Alimenta el
    parpadeo del grid: cada rol ve parpadear solo lo suyo (el dev ve todo)."""
    txt = lambda v: str(v or "").strip()
    out = {}
    # Programadoras → 3.4 (W/Y) + sigla FT (H)
    if not _emerg_o_apoyo({"tipo": reg.tipo, "causal": reg.causal, "asociado_pdl": reg.asociado_pdl}):
        if not txt(reg.fecha_debio_validar):
            out["fecha_debio_validar"] = "programador"
        if not txt(reg.fecha_envio_validar):
            out["fecha_envio_validar"] = "programador"
    if reg.tipo == "FT" and not txt(reg.sigla_elaborador):
        out["sigla_elaborador"] = "programador"
    # Responsable (col G): el CD10 lo trae casi siempre, pero lo deja vacío en ~1 de cada 7 filas
    # (verificado en el archivo real: 231 con / 39 sin). Cuando falta, lo escribe la programadora
    # —es quien sabe quién montó el trabajo—; sin él no se sabe a quién preguntarle nada.
    if not txt(reg.nombre_responsable):
        out["nombre_responsable"] = "programador"
    # María Inés / Ingeniería → 3.2 C (nodos)
    if reg.nodos_planeados is None and reg.nodos_ejecutados is None:
        out["nodos_planeados"] = "ingeniero_zona"
        out["nodos_ejecutados"] = "ingeniero_zona"
    # Cumplimentadores → 3.3 (AQ): toda fila EJECUTADA debe decir quién la cumplimentó en el
    # sistema de ENEL (así la dinámica 3.3 atribuye las devoluciones por persona). Las no
    # ejecutadas (anuladas) no se cumplimentan — esas no se piden.
    if str(reg.se_ejecuto or "").strip().upper() == "SI" and not txt(reg.nombre_cumplimentador):
        out["nombre_cumplimentador"] = "cumplimentador"
    # Exclusiones. Son DOS momentos, y los dos parpadean:
    #   1) decidir si se excluye (SI/NO),
    #   2) DECIR POR QUÉ — una exclusión sin razón no se sostiene ante ENEL.
    # Por eso, en cuanto se decide (sea SI o NO), la observación queda pendiente.
    # El ROL depende del indicador: el 3.4 es ENTERO de las programadoras (ellas envían la
    # programación, así que también deciden y justifican su propio retraso); el resto (3.2 A/B y
    # 3.3) los lleva el encargado del ANS.
    c = computar(reg)
    nc = lambda v: str(v or "").upper() == "NO CUMPLE"

    def _excl(toca_decidir, decision, campo_decision, razon, campo_razon, rol="ans"):
        if toca_decidir and not txt(decision):
            out[campo_decision] = rol              # falta decidir si aplica exclusión
        elif txt(decision) and not txt(razon):
            out[campo_razon] = rol                 # ya decidiste → falta la razón

    _excl(nc(c.get("cumple_tprog")),                                    # 3.4   AA → AB
          reg.excluible_tiempos, "excluible_tiempos", reg.obs_tiempos, "obs_tiempos",
          rol="programador")
    _excl(nc(c.get("cumple_tiempos")) or nc(c.get("cumple_apertura")),  # 3.2 A AL → AM
          reg.excluible, "excluible", reg.obs, "obs")
    _excl(str(reg.se_ejecuto or "").upper() == "NO",                    # 3.2 B AO → AP (anulados)
          reg.excluible_anulado, "excluible_anulado", reg.obs_anulado, "obs_anulado")
    _excl(nc(c.get("cumple_uucc")),                                     # 3.3   AT → AU
          reg.excluible2, "excluible2", reg.obs3, "obs3")
    return out


def _to_dict(b: AnsRegistro) -> dict:
    d = {c.name: getattr(b, c.name) for c in b.__table__.columns}
    d["datos_extra"] = json.loads(b.datos_extra) if b.datos_extra else {}
    try:                               # marca de reglas de nodos → el grid pinta amarillo/rojo
        d["nodos_auto"] = json.loads(b.nodos_auto) if b.nodos_auto else None
    except (ValueError, TypeError):
        d["nodos_auto"] = None
    d.update(computar(b))
    d["_pend"] = _pend_campos(b)       # celdas que parpadean, por rol
    return d


# ── Ingesta ──────────────────────────────────────────────────────────────────────

def _periodo_registro(rec: dict, fallback: str) -> str:
    """Mes (YYYY-MM) al que pertenece el registro según su FECHA FIN (o inicio si falta). Si no hay
    fecha parseable, cae al periodo elegido. Permite clasificar automáticamente un CD10 que trae
    trabajos de varios meses (ej. corte al 02/07 con filas de 30/06 → junio y 01/07 → julio)."""
    for campo in ("fecha_fin", "fecha_inicio"):
        d = _parse_dt(rec.get(campo))
        if d:
            return f"{d.year:04d}-{d.month:02d}"
    return fallback


@router.post("/import")
async def importar(
    file: UploadFile = File(...),
    tipo: str = Query(..., pattern="^(programados|st|auto)$"),
    periodo: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="El archivo debe ser .xlsx")
    contents = await file.read()
    return await _importar_cd10(db, contents, file.filename, tipo, periodo,
                                current_user.unidad or "SABOGAL")


async def _importar_cd10(db: AsyncSession, contents: bytes, filename: str,
                         tipo: str, periodo: str, uni: str) -> dict:
    """Núcleo del import STM — separado del endpoint para poder re-procesar archivos ya
    guardados en procedencia (reparación del bug FT/ST de 2026-07) con LA MISMA lógica.
    `tipo`: 'programados' | 'st' | 'auto'. El TIPO REAL siempre se detecta del contenido
    (_detectar_tipo_cd10): con 'auto' se usa el detectado, y si el botón dijo una cosa pero
    el archivo es claramente la otra, SE CORRIGE solo (con aviso) — así el botón equivocado
    ya no puede volver a dejar un mes cojo importando 0 filas en silencio."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo: {e}")

    avisos = []
    det, n_ft, n_st = _detectar_tipo_cd10(wb)
    if tipo == "auto":
        if det is None:
            raise HTTPException(status_code=400, detail=(
                "No pude clasificar el archivo: no encontré protocolos PP####R (Programados) ni "
                "PT####R (Línea Viva) en la columna D. ¿Seguro que es un CD10 de planes de trabajo?"))
        t = det
        avisos.append(f"Clasificado automáticamente como {'Programados (FT)' if t == 'FT' else 'Línea Viva (ST)'} "
                      f"({n_ft} protocolos PP · {n_st} PT).")
    else:
        t = "FT" if tipo == "programados" else "ST"
        if det and det != t:
            avisos.append(f"⚠ El botón decía {'Programados' if t == 'FT' else 'Línea Viva'} pero el archivo es "
                          f"claramente {'Línea Viva (ST)' if det == 'ST' else 'Programados (FT)'} "
                          f"({n_ft} protocolos PP · {n_st} PT): se importó como lo que ES. ")
            t = det
    tipo_final = "programados" if t == "FT" else "st"      # carpeta/tipo de la procedencia

    aprob = await _aprobadores_set(db)
    zmap = await _zona_map(db)                    # semilla + directorio aprendido
    registros = _parse_cd10(wb, t, aprob, zmap)
    if not registros:
        avisos.append("⚠ NINGUNA fila pasó el filtro de ingesta (Dominion + aprobador + estado). "
                      "Si el archivo sí trae trabajos de Dominion, algo anda mal — avísale a Sergio.")

    # ── Clasificación automática por mes (un CD10 puede traer varios meses) ─────
    # Cada registro va al periodo de su FECHA FIN (o inicio); sin fecha parseable → periodo elegido.
    for rec in registros:
        rec["_periodo"] = _periodo_registro(rec, periodo)
    reparto: dict = {}
    for rec in registros:
        reparto[rec["_periodo"]] = reparto.get(rec["_periodo"], 0) + 1
    periodos_dest = sorted(reparto.keys()) or [periodo]

    # ── Controles de subida (procedencia) ──────────────────────────────────────
    # (el control de «¿tipo correcto?» vive arriba: el detector clasifica/corrige SIEMPRE,
    #  incluso cuando el filtro deja 0 filas — el aviso viejo solo miraba filas sobrevivientes
    #  y por eso el bug de los ST subidos como CD10 pasó callado)
    file_hash = hashlib.sha256(contents).hexdigest()
    # 1) ¿archivo idéntico ya subido? (mismo contenido, cualquier nombre/periodo)
    rep = (await db.execute(select(AnsFuente).where(AnsFuente.hash == file_hash))).scalars().first()
    if rep:
        avisos.append(f"Archivo idéntico ya subido como «{rep.nombre}» (periodo {rep.periodo}). Es un re-proceso, no se duplican datos.")
    # 3) Info: cómo quedó clasificado por mes (si abarca más de uno o cae en otro mes)
    if len(reparto) > 1 or (reparto and periodo not in reparto):
        avisos.append("Clasificado automáticamente por fecha fin: " +
                      ", ".join(f"{p} → {n}" for p, n in sorted(reparto.items())))
    # 4) ¿códigos que ya existen en un periodo que NO tocamos? (posible doble conteo)
    prots = [r["protocolo"] for r in registros]
    dup_otro = []
    if prots:
        rows = (await db.execute(
            select(AnsRegistro.protocolo, AnsRegistro.periodo)
            .where(AnsRegistro.protocolo.in_(prots), AnsRegistro.periodo.notin_(periodos_dest))
        )).all()
        dup_otro = [{"protocolo": p, "periodo": pe} for p, pe in rows]
        if dup_otro:
            avisos.append(f"⚠ {len(dup_otro)} código(s) ya existen en otro periodo (se contarían doble): " +
                          ", ".join(f"{d['protocolo'].split('/')[0]}→{d['periodo']}" for d in dup_otro[:5]) +
                          ("…" if len(dup_otro) > 5 else ""))

    # Asegura cada periodo destino + índice de existentes por periodo (upsert preserva manuales)
    existentes_por_per = {}
    for pr in periodos_dest:
        if not (await db.execute(select(AnsPeriodo).where(AnsPeriodo.periodo == pr))).scalar_one_or_none():
            db.add(AnsPeriodo(periodo=pr, unidad=uni))
        existentes_por_per[pr] = {
            r.protocolo: r for r in (await db.execute(
                select(AnsRegistro).where(AnsRegistro.periodo == pr, AnsRegistro.tipo == t)
            )).scalars().all()
        }

    insertados = actualizados = 0
    for rec in registros:
        pr = rec.pop("_periodo")
        extra = rec.pop("datos_extra", {})
        prot = rec["protocolo"]
        ex = existentes_por_per[pr].get(prot)
        if ex:  # actualiza núcleo, NO pisa campos manuales ya diligenciados
            for k, v in rec.items():
                if k not in ("tipo", "protocolo"):
                    setattr(ex, k, v)
            # Autocompletados solo si el campo manual sigue vacío (no destruye ediciones)
            if t == "ST" and not ex.sigla_elaborador:
                ex.sigla_elaborador = _sigla_lv_auto(ex.descripcion)            # #8
            if not ex.se_ejecuto:
                ex.se_ejecuto = _se_ejecuto_auto(ex.estado_actual)              # #9
            # Regla de Santana/Paola (3.2B): si la anulada YA tiene decidida su exclusión
            # (AO=SI/NO), «Se ejecutó» (AN) debe seguirla — SI→N/A, NO→NO — aunque el campo
            # hubiera quedado en NO por el auto de arriba o por un import previo a la decisión.
            # Cierra el hueco que dejaba filas con Excluye=SI y AN=NO sin re-guardar a mano.
            if _es_anulado(ex.estado_actual):
                ao = (ex.excluible_anulado or "").strip().upper()
                an = "N/A" if ao == "SI" else ("NO" if ao == "NO" else None)
                if an and (ex.se_ejecuto or "").strip().upper() != an:
                    db.add(AnsCambio(registro_id=ex.id, periodo=ex.periodo, protocolo=ex.protocolo,
                                     campo="se_ejecuto", valor_anterior=_s(ex.se_ejecuto), valor_nuevo=an,
                                     tipo="edicion", usuario_id=None, usuario_nombre="Sistema (regla 3.2B)"))
                    ex.se_ejecuto = an
            if _emerg_o_apoyo({"tipo": ex.tipo, "causal": ex.causal, "asociado_pdl": ex.asociado_pdl}):
                # Regla absoluta de Paola: emergencial/apoyo NUNCA lleva fecha → siempre N/A
                # (corrige casos atípicos que hubieran quedado con fecha por error).
                ex.fecha_debio_validar = "N/A"                                  # #10
                ex.fecha_envio_validar = "N/A"
            # W (fecha "debió validar") la llenan las PROGRAMADORAS, no el sistema: no se calcula
            # sola (por pedido de Sergio). La celda queda vacía y les parpadea para que la diligencien.
            ex.datos_extra = json.dumps(extra, ensure_ascii=False, default=str)
            # El re-import puede cambiar estado/«se ejecutó» → revalida las reglas de nodos
            _log_reglas_nodos(db, ex, _eval_reglas_nodos(ex))
            actualizados += 1
        else:
            # Autocompletados al crear (editables; se preservan en re-importes):
            if t == "ST" and not rec.get("sigla_elaborador"):
                rec["sigla_elaborador"] = _sigla_lv_auto(rec.get("descripcion"))  # #8 ZONA VII→Andrea
            if not rec.get("se_ejecuto"):
                rec["se_ejecuto"] = _se_ejecuto_auto(rec.get("estado_actual"))    # #9 cerrado→SI/anulado→NO
            if _es_anulado(rec.get("estado_actual")):                            # regla 3.2B (Santana)
                ao = (rec.get("excluible_anulado") or "").strip().upper()
                an = "N/A" if ao == "SI" else ("NO" if ao == "NO" else None)
                if an:
                    rec["se_ejecuto"] = an
            if _emerg_o_apoyo(rec):                                               # #10 emergencial/apoyo→N/A
                rec["fecha_debio_validar"] = "N/A"
                rec["fecha_envio_validar"] = "N/A"
            # W (fecha "debió validar") NO se auto-calcula: la llenan las programadoras (Sergio).
            db.add(AnsRegistro(
                unidad=uni, periodo=pr,
                datos_extra=json.dumps(extra, ensure_ascii=False, default=str), **rec,
            ))
            insertados += 1
    # Respaldo de procedencia: guarda el CD10 bajo cada periodo que recibió registros,
    # SIEMPRE con el tipo real detectado (no el del botón)
    for pr in periodos_dest:
        await _guardar_fuente(db, pr, tipo_final, filename, contents, reparto.get(pr, 0), file_hash)
    await db.commit()
    return {"tipo": t, "periodo": periodo, "leidos_filtrados": len(registros),
            "insertados": insertados, "actualizados": actualizados, "reparto": reparto,
            "avisos": avisos, "duplicados_otro_periodo": dup_otro, "archivo_repetido": bool(rep)}


async def _guardar_fuente(db, periodo, tipo, nombre, contents, registros=0, file_hash=None):
    """Guarda el archivo fuente en disco y crea/actualiza su registro AnsFuente."""
    nombre = os.path.basename(nombre or "archivo.xlsx")
    # Namespacing por entorno: prueba/<periodo>/<tipo> vs real/<periodo>/<tipo>
    carpeta = os.path.join(FUENTES_DIR, current_entorno(), periodo, tipo)
    os.makedirs(carpeta, exist_ok=True)
    ruta_abs = os.path.join(carpeta, nombre)
    with open(ruta_abs, "wb") as fh:
        fh.write(contents)
    ruta_rel = os.path.relpath(ruta_abs, FUENTES_DIR)
    if file_hash is None:
        file_hash = hashlib.sha256(contents).hexdigest()
    existente = (await db.execute(
        select(AnsFuente).where(AnsFuente.periodo == periodo, AnsFuente.tipo == tipo, AnsFuente.nombre == nombre)
    )).scalar_one_or_none()
    if existente:
        existente.ruta = ruta_rel
        existente.tamano = len(contents)
        existente.registros = registros
        existente.hash = file_hash
        # `creado_en` funciona como «última subida»: sin esto, re-subir un nombre ya usado
        # conserva su fecha vieja y el flag «EN USO» de /prog20/archivos señala otro archivo.
        # Naive-UTC como func.now() de la BD (a _bog le toca restar las 5h al mostrar).
        existente.creado_en = datetime.now(_tz.utc).replace(tzinfo=None)
    else:
        db.add(AnsFuente(periodo=periodo, tipo=tipo, nombre=nombre, ruta=ruta_rel,
                         tamano=len(contents), registros=registros, hash=file_hash))


# ── Listado / edición ────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
#  CAMPANITA DE TAREAS: qué columna le toca llenar a cada rol (guía de Paola).
#  Cadena real: el SISTEMA vuelca el STM → aviso "DILIGENCIAMIENTO ANS" →
#  PROGRAMADORAS (3.4 COMPLETO: W/Y + su exclusión AA/AB + sigla FT) +
#  MARÍA INÉS (3.2 C: nodos) + CUMPLIMENTADORES (AQ: quién cumplimentó cada ejecutada) →
#  ENCARGADO ANS aplica las EXCLUSIONES del resto (AL/AO/AT).
#  El 3.4 es enteramente de las programadoras: ellas envían la programación, así que también
#  deciden y justifican su propio retraso (confirmado por Sergio).
# ══════════════════════════════════════════════════════════════════════════════
TAREAS_ANS = [
    {
        "id": "3.4", "rol": "programador", "rol_label": "Programadoras",
        "titulo": "Oportunidad de programación (3.4)", "columnas": "W · Y · AA · AB",
        "ayuda": ("El 3.4 es tuyo de principio a fin. (1) Las DOS fechas: la W («debió validar») — de "
                  "referencia la regla es inicio − 9 días hábiles — y la Y («envió validar»), la fecha en que "
                  "REALMENTE enviaste la programación (búscala en el correo / SharePoint donde la mandaste). "
                  "La X («debió enviar») se calcula sola = W − 1 y CUMPLE si enviaste al menos 1 día antes. "
                  "(2) Si quedó NO CUMPLE: decide en AA si es excluible y escribe SIEMPRE el porqué en la "
                  "observación AB — sin razón, la exclusión no se sostiene ante ENEL. Las emergenciales y "
                  "las de apoyo a PDL quedan en N/A solas — esas NO se llenan."),
    },
    {
        "id": "sigla", "rol": "programador", "rol_label": "Programadoras",
        "titulo": "Responsable y sigla", "columnas": "G · H",
        "ayuda": ("Datos de personas que el STM no siempre trae. (G) RESPONSABLE: quién montó el "
                  "trabajo. El CD10 lo trae en la mayoría de las filas, pero lo deja vacío en "
                  "algunas — ahí lo escribes tú; sin él no se sabe a quién preguntarle si algo "
                  "falla, y de él se deduce la zona. (H) SIGLA del elaborador: en las ST el sistema "
                  "la pone sola (regla ZONA VII); en las FT hay que confirmarla. Sirve para "
                  "desglosar el 3.4 por programadora."),
    },
    {
        "id": "3.2C", "rol": "ingeniero_zona", "rol_label": "María Inés / Ingeniería",
        "titulo": "Nodos planeados / ejecutados (3.2 C)", "columnas": "AV · AW",
        "ayuda": ("Cuántos nodos estaban planeados y cuántos se ejecutaron en la orden. Con eso el sistema "
                  "calcula el 3.2 C (alcance). La vía cómoda es la pestaña «Nodos»: sube tu Programación "
                  "diaria, el sistema cruza cada PDL con tu archivo y tú apruebas caso a caso."),
    },
    {
        "id": "cumplimentador", "rol": "cumplimentador", "rol_label": "Cumplimentadores",
        "titulo": "Nombre del cumplimentador (3.3)", "columnas": "AQ",
        "ayuda": ("En cada orden EJECUTADA, escribe en la AQ el nombre de quien la cumplimentó en el "
                  "sistema de ENEL (quien registró la ejecución). Con ese nombre la dinámica del 3.3 "
                  "atribuye las devoluciones por UUCC a la persona correcta. Las anuladas no se "
                  "cumplimentan — esas no parpadean."),
    },
    {
        "id": "exclusiones", "rol": "ans", "rol_label": "Encargado ANS",
        "titulo": "Exclusiones + su razón", "columnas": "AL·AO·AT + AM·AP·AU",
        "ayuda": ("Dos pasos, y los dos parpadean. (1) DECIDIR si aplica exclusión en las filas que "
                  "quedaron en NO CUMPLE o anuladas: AL excluye del 3.2 A, AO del 3.2 B (anulados) y "
                  "AT del 3.3 — marcar «SI» las saca del indicador (pasan a N/A). "
                  "(2) DECIR POR QUÉ en la observación de al lado (AM, AP, AU): sea SI o NO, la "
                  "decisión necesita razón — una exclusión sin justificación no se sostiene ante ENEL. "
                  "El 3.4 (AA/AB) NO es tuyo: lo llevan las programadoras."),
    },
]
# Quién ve TODAS las tareas (no solo las suyas) vive en config.py → `ans_ve_todo(rol, email)`,
# junto a `puede_ans`: una sola fuente de verdad para backend y frontend.


TAREA_CAMPOS = {   # qué celdas cubre cada tarea (deriva del MISMO _pend_campos que el parpadeo,
    # El 3.4 va COMPLETO a las programadoras: las fechas Y su exclusión + razón.
    "3.4":         ("fecha_debio_validar", "fecha_envio_validar",     # así la campanita y las
                    "excluible_tiempos", "obs_tiempos"),             # celdas nunca se contradicen
    "sigla":       ("nombre_responsable", "sigla_elaborador"),   # G · H (lo que el STM no trajo)
    "3.2C":        ("nodos_planeados", "nodos_ejecutados"),
    "cumplimentador": ("nombre_cumplimentador",),                # AQ (quién cumplimentó — 3.3)
    "exclusiones": ("excluible", "obs", "excluible_anulado", "obs_anulado", "excluible2", "obs3"),
}


def _pendiente(reg: AnsRegistro, tid: str) -> bool:
    """¿A esta fila le falta lo que le toca a esa tarea/rol? Se lee de _pend_campos (única
    fuente): lo que parpadea en el grid es exactamente lo que cuenta la campanita."""
    campos = _pend_campos(reg)
    return any(c in campos for c in TAREA_CAMPOS.get(tid, ()))


# ── ¿A QUIÉN LE PREGUNTO? — sugerencia por celda (solo dev, por ahora) ───────────
# La guía de Paola define DOS caminos según por qué falló la orden, y el sistema los puede
# separar solo mirando el ESTADO: si está ANULADA la canceló el Centro Operativo de ENEL
# (el correo lo consigue el profesional de apoyo de la zona); si está CERRADA pero incumplió
# tiempos, la causa está en CAMPO (la sabe el líder que estuvo allí, o su supervisor).
# SEMILLA del profesional de apoyo por zona (la guía de Paola). Lo que mande de verdad es el
# DIRECTORIO (tab Líderes → rol «apoyo» + zona): así se arregla una renuncia o un traslado
# editando datos, sin tocar código. Ver `_apoyo_map()`.
APOYO_ZONA = {
    "GUAVIO": "Jhon Quiroga",
    "ALMEIDAS": "Michael Guasca",
    "UBATE": None,          # hueco de la guía → se llena desde el directorio
}
# Del log de exclusiones de campo (2.856 filas, 2023-2027): ~63% son CLIMA.
PISTA_CAMPO = ("En el histórico de exclusiones de campo, ~63% son CLIMA («stop work por lluvias»); "
               "el resto: terreno rocoso, vía cerrada, trancón. Empieza preguntando por el clima de ese día.")


# ── DIRECTORIO: se llena a mano, un contacto a la vez ────────────────────────────
# No importamos la agenda ni raspamos WhatsApp: cuando el sistema dice "pregúntale a X" y X no
# tiene número, tú lo agregas ahí mismo. Queda guardado para siempre y con certeza, porque lo
# guardó una persona. El nombre normalizado es la llave: el mismo líder vuelve a aparecer en
# otras órdenes y el sistema ya sabe a qué número escribirle.
# (aquí vivía una SEGUNDA _norm_nombre que pisaba a la de arriba sin quitar el «(CÓDIGO)» —
#  causó el bug de Línea Viva de jul-2026; la única definición válida está junto a los helpers)


def _norm_tel(s: Optional[str]) -> Optional[str]:
    """A E.164 sin '+' (lo que pide wa.me). Colombia: móvil = 10 dígitos que empiezan por 3."""
    d = re.sub(r"\D", "", s or "")
    d = d.lstrip("0")
    if len(d) == 10 and d.startswith("3"):
        d = "57" + d                       # móvil colombiano sin indicativo
    return d if 10 <= len(d) <= 15 else None


def _nombre_corto(nombre: str, apellidos_primero: bool) -> str:
    """Para saludar. Ojo: las dos columnas traen el nombre en ORDEN DISTINTO — el líder (col E) va
    «APELLIDO APELLIDO NOMBRE NOMBRE (CO123…)» y el supervisor (col G) va «NOMBRE NOMBRE APELLIDO».
    Sin distinguirlos, al supervisor se le saluda por el apellido."""
    limpio = re.sub(r"\(.*?\)", " ", nombre or "")          # fuera el código «(CO79171313)»
    t = _norm_nombre(limpio).split()
    if not t:
        return ""
    if apellidos_primero and len(t) >= 3:
        return t[2].title()
    return t[0].title()


# Roles que son ingenieros → se les trata de «Ing.». Los profesionales de apoyo lo son.
_ES_ING = ("apoyo",)


def _trato(quien: str, rol_dir: str) -> str:
    """El nombre para dirigirse a la persona: «Ing. Juan» / «Juan», con el apellido o el nombre
    según de qué columna venga (líder/programadora = apellidos primero)."""
    corto = _nombre_corto(quien, apellidos_primero=(rol_dir in _APELLIDOS_PRIMERO))
    if not corto:
        return ""
    return f"Ing. {corto}" if rol_dir in _ES_ING else corto


def _mensaje_wa(reg: AnsRegistro, campo: str, quien: str, rol_dir: str = "otro",
                remitido_por: Optional[str] = None, remitido_rol: str = "otro",
                saludar: bool = True) -> Optional[str]:
    """El mensaje ya redactado, con los datos de ESTA orden. Tono cordial y de usted (por favor,
    gracias, «Ing.» a los profesionales de apoyo). `saludar=False` quita el «Buen día» cuando ya
    se le escribió a esa persona hoy — sería raro saludarla dos veces el mismo día. Si `remitido_por`
    viene, aclara quién lo remitió."""
    trato = _trato(quien, rol_dir)
    if saludar:
        saludo = f"Buen día, {trato}." if trato else "Buen día."
        if remitido_por:
            rem = _trato(remitido_por, remitido_rol) or remitido_por
            saludo += f" {rem} me indicó que usted me puede colaborar, por favor, con el siguiente caso del ANS."
        else:
            saludo += " Le escribo para solicitar su apoyo, por favor, con un tema del ANS."
    else:
        # Ya se le escribió hoy: sin «Buen día», como quien sigue la conversación.
        saludo = (f"{trato}, también tengo este otro caso, por favor:"
                  if trato else "También tengo este otro caso, por favor:")
    cuando = (reg.fecha_inicio or "").strip()
    ficha = f"Orden {reg.protocolo}" + (f" · {reg.zona}" if reg.zona else "") + (f" · {cuando}" if cuando else "")
    desc = (reg.descripcion or "").strip()
    if len(desc) > 90:
        desc = desc[:90].rstrip() + "…"
    # La evidencia depende de a quién se le pide: quien estuvo en campo la graba (VIDEO); el
    # profesional de apoyo la consigue de ENEL (CORREO). En una redirección aún no sabemos su rol.
    if rol_dir in ("apoyo", "programadora"):
        pide = "el correo que lo respalde"
    elif rol_dir in ("lider", "supervisor"):
        pide = "el video del frente de trabajo"
    else:
        pide = "el video del frente de trabajo o el correo que lo respalde"

    if campo in ("excluible", "obs"):                       # 3.2 A — tiempos, causa de campo
        return (f"{saludo}\n\n{ficha}\n{desc}\n\n"
                "Esta orden presentó incumplimiento en los tiempos pactados. Por favor, ¿me podría "
                "indicar qué ocurrió ese día? (condiciones climáticas, vía cerrada, terreno, un "
                "tercero, o si fue un tema interno).\n\n"
                f"Si cuenta con {pide}, le agradezco me lo pueda compartir. Quedo muy atento a su "
                "amable respuesta. Muchas gracias.")
    if campo in ("excluible_anulado", "obs_anulado"):       # 3.2 B — la anuló ENEL
        return (f"{saludo}\n\n{ficha}\n{desc}\n\n"
                f"Esta orden figura como ANULADA ({reg.estado_actual or 'anulada'}). Por favor, ¿me "
                "podría facilitar el correo del Centro Operativo donde indiquen el motivo de la "
                "cancelación?\n\n"
                "Quedo atento a su amable respuesta. Muchas gracias.")
    if campo in ("excluible_tiempos", "obs_tiempos"):       # 3.4 — envío tardío (causa interna)
        return (f"{saludo}\n\n{ficha}\n\n"
                "La programación de esta orden se envió tarde a validación (indicador 3.4). Por favor, "
                "¿recuerda si hubo algún motivo (cambio por parte de ENEL, indisponibilidad del "
                "sistema, reprogramación)? De ser así, le agradezco el correo o pantallazo. Quedo "
                "atento a su amable respuesta. Muchas gracias.")
    return None


async def _tel_de(db: AsyncSession, nombre: str) -> Optional[Contacto]:
    n = _norm_nombre(nombre)
    if not n:
        return None
    r = await db.execute(select(Contacto).where(Contacto.nombre_norm == n, Contacto.activo.is_(True)))
    return r.scalar_one_or_none()


async def _ya_escrito_hoy(db: AsyncSession, quien: str) -> bool:
    """¿Ya se le mandó HOY al menos un mensaje a esta persona (en cualquier orden)? Sirve para no
    volver a saludarla si en el mismo día le escribimos por 2 exclusiones distintas. El join con
    AnsConsulta hace que el filtro de entorno aplique solo."""
    n = _norm_nombre(quien)
    if not n:
        return False
    hoy = datetime.now()
    ini = datetime(hoy.year, hoy.month, hoy.day)
    r = await db.execute(
        select(AnsConsultaMsg.persona)
        .join(AnsConsulta, AnsConsulta.id == AnsConsultaMsg.consulta_id)
        .where(AnsConsultaMsg.direccion == "enviado", AnsConsultaMsg.creado_en >= ini))
    return any(_norm_nombre(p) == n for (p,) in r.all())


# ── PASO 1: BUSCAR EN EL GRUPO (antes de molestar a nadie) ──────────────────────
# La guía de Paola manda mirar primero el grupo de WhatsApp: la cuadrilla sube el video cuando
# para el trabajo. Y el sistema puede decir EXACTAMENTE cuándo mirar, porque sabe la hora que se
# planeó y la hora real de la maniobra: el video está en medio de esas dos.
MARGEN_BUSQUEDA_MIN = 30


def _busqueda_grupo(reg: AnsRegistro) -> Optional[dict]:
    """La ventana donde estaría el video: del rato antes de la hora planeada al rato después de la
    maniobra real. Solo aplica al 3.2 A (causa de campo); las otras exclusiones son por correo."""
    c = computar(reg)
    tramos = []
    for lado, etiqueta, fecha, hora, real, cumple, desf in (
        ("apertura", "la apertura (inicio del trabajo)", reg.fecha_inicio, reg.hora_inicio,
         reg.maniobra_inicio, c.get("cumple_apertura"), c.get("desfase_apertura")),
        ("cierre", "el cierre (fin del trabajo)", reg.fecha_fin, reg.hora_fin,
         reg.maniobra_fin, c.get("cumple_cierre"), c.get("desfase_cierre")),
    ):
        if cumple != "NO CUMPLE":
            continue
        plan = _planeada(fecha, hora)
        rdt = _parse_dt(real)
        if not plan or not rdt:
            continue
        desde = plan - timedelta(minutes=MARGEN_BUSQUEDA_MIN)
        hasta = rdt + timedelta(minutes=MARGEN_BUSQUEDA_MIN)
        horas = int(desf // 60) if desf else 0
        mins = int(desf % 60) if desf else 0
        retraso = (f"{horas} h {mins} min" if horas else f"{mins} min")
        # La franja puede cruzar la medianoche (un cierre planeado a las 20:15 que se hizo a la
        # 01:28). Si el final cae otro día hay que decirlo, o se buscaría en el día equivocado.
        cruza = hasta.date() != desde.date()
        tramos.append({
            "lado": lado, "etiqueta": etiqueta,
            "dia": desde.strftime("%d/%m/%Y"),
            "dia_hasta": hasta.strftime("%d/%m/%Y") if cruza else None,
            "desde": desde.strftime("%H:%M"), "hasta": hasta.strftime("%H:%M"),
            "planeado": plan.strftime("%H:%M"), "real": rdt.strftime("%H:%M"),
            "retraso": retraso,
            "por_que": (f"Se planeó {etiqueta} a las {plan.strftime('%H:%M')} y la maniobra quedó "
                        f"registrada a las {rdt.strftime('%H:%M')}"
                        + (f" del {hasta.strftime('%d/%m')}" if cruza else "")
                        + f" ({retraso} tarde). Lo que pasó, pasó entre esas dos horas: ahí es donde "
                        "la cuadrilla habría subido el video."),
        })
    if not tramos:
        return None
    # Datos del caso a la mano (pedido de Sergio): EN QUÉ GRUPO buscar (la zona) y de quién
    # sería el video (líder RI + supervisor), sin tener que ir a mirar las celdas de la fila.
    z = _zona_reg(reg)
    lider = re.sub(r"\s+", " ", str(reg.ri_jt or "")).strip()
    return {
        "tramos": tramos,
        "caso": {
            "zona": z,
            "grupo": f"grupo de WhatsApp de {z}",
            "lider": lider or None,
            "supervisor": (reg.nombre_responsable or "").strip() or None,
            "pdl": (reg.protocolo or "").split("/")[0].strip() or None,
        },
        "grupos": [f"Grupo de WhatsApp de la zona {z}", "Grupo «ANS / Cuadrillas»"],
        "que_buscar": ("Videos o fotos del frente de trabajo, y mensajes tipo «stop work», «no se puede "
                       "subir», «vía cerrada». Suelen ser 2 videos: uno al parar y otro al retomar. "
                       "Rara vez el soporte es un CORREO o un PDF (directriz de ENEL, vía cerrada "
                       "de la alcaldía): si es el caso, adjúntalo aquí mismo (.msg/.eml/PDF)."),
    }


async def _apoyo_map(db: AsyncSession) -> dict:
    """{ZONA: nombre del profesional de apoyo}. Semilla del código + lo que haya en el DIRECTORIO
    (contactos con rol «apoyo» y zona), y el directorio manda. Igual que `_zona_map`: agregar o
    cambiar al apoyo de una zona es editar el tab Líderes, no redesplegar."""
    m = {z: n for z, n in APOYO_ZONA.items() if n}
    r = await db.execute(select(Contacto).where(
        Contacto.activo.is_(True), Contacto.rol == "apoyo", Contacto.zona.isnot(None)))
    for c in r.scalars().all():
        z = (c.zona or "").strip().upper()
        if z:
            m[z] = c.nombre
    return m


async def _zona_apoyo_de(db: AsyncSession, user: Usuario) -> Optional[str]:
    """De qué zona es profesional de apoyo esta cuenta, o None si no lo es. Manda el DIRECTORIO
    (se cruza el nombre de la cuenta con el del contacto rol «apoyo»), así un traslado o un
    reemplazo se arregla editando el tab Líderes y no aquí. Si el directorio aún no la tiene,
    cae a la zona de su propia cuenta para no dejarla sin sus casos."""
    if not _es_apoyo_ans(user.rol, user.email):
        return None
    mio = _norm_nombre(user.nombre or "")
    if mio:
        for z, nombre in (await _apoyo_map(db)).items():
            # El directorio suele traer el nombre completo («Geraldine Mendez») y la cuenta solo
            # el de pila: basta con que uno contenga al otro para reconocerla.
            n = _norm_nombre(nombre)
            if n and (n == mio or n.startswith(mio + " ") or mio.startswith(n + " ")):
                return z
    return user.zona.value if user.zona else None


def _cond_anulada():
    """Filtro SQL de «la anuló el Centro Operativo de ENEL». Mismos patrones que reconoce
    `_se_ejecuto_auto` sobre el estado del STM («Anulado por Centro Operativo», «An. por CO…»)."""
    return or_(AnsRegistro.estado_actual.ilike("%ANULAD%"),
               AnsRegistro.estado_actual.ilike("%AN. POR%"),
               AnsRegistro.estado_actual.ilike("AN.%"))


def _sugerencia_celda(reg: AnsRegistro, campo: str, apoyo_map: Optional[dict] = None) -> dict:
    """Quién sabe la respuesta para poder llenar ESTA celda, y por qué esa persona."""
    zona = (reg.zona or "").strip().upper()
    sup = (reg.nombre_responsable or "").strip()
    lider = (reg.ri_jt or "").strip()
    anulado = "ANULAD" in (reg.estado_actual or "").upper() or "AN. POR" in (reg.estado_actual or "").upper()
    apoyo = (apoyo_map if apoyo_map is not None else APOYO_ZONA).get(zona)
    contactos, ruta, pista = [], [], None

    if campo in ("excluible_anulado", "obs_anulado"):
        titulo = "3.2 B · ¿Por qué se anuló esta orden?"
        que = ("Decide si la anulación es EXCLUIBLE. Si ENEL la canceló por repriorizar (mayor impacto, "
               "emergencia) → SI. Si todavía no tienes el correo que lo pruebe → NO: cuenta en contra "
               "hasta que llegue la evidencia (así lo hacía Paola).")
        if apoyo:
            contactos.append({"quien": apoyo, "rol": f"Profesional de apoyo · {zona}",
                              "porque": "Es quien consigue el correo del Centro Operativo de ENEL con el motivo de la cancelación.",
                              "canal": "WhatsApp → te reenvía el correo"})
        elif zona:
            contactos.append({"quien": "— sin asignar —", "rol": f"Profesional de apoyo · {zona}",
                              "porque": "Esta zona no tiene profesional de apoyo en la guía. Pregúntale a Sabogal quién es.",
                              "canal": "—"})
        if sup:
            contactos.append({"quien": sup, "rol": "Supervisor de la orden (col G)",
                              "porque": "Tenía el trabajo a cargo cuando lo cancelaron: te da el contexto.",
                              "canal": "WhatsApp"})
        ruta = ["Pregunta por WhatsApp al profesional de apoyo de la zona",
                "Te reenvía el correo del Centro Operativo con el motivo",
                "Transcribe ese motivo en la observación (AP)"]
        evidencia = "SharePoint / ANS / Programados / <Mes> / exclusiones / 3.2B — carpeta con el N.º de orden, y dentro el correo"

    elif campo in ("excluible", "obs"):
        titulo = "3.2 A · ¿Por qué se incumplieron los tiempos?"
        que = ("Decide si la causa fue EXTERNA (lluvia, terreno, vía cerrada, un tercero → excluible SI) "
               "o INTERNA (nuestra → NO). Sin evidencia no se excluye.")
        if lider:
            contactos.append({"quien": lider, "rol": "Líder / RI designado (col E)",
                              "porque": "Estuvo en campo en esta orden: es quien sabe qué pasó ese día.",
                              "canal": "WhatsApp «ANS / Cuadrillas» → si no responde, llamar"})
        if sup:
            contactos.append({"quien": sup, "rol": "Supervisor (col G)",
                              "porque": "Responde por la cuadrilla de esta orden: confirma o corrige al líder.",
                              "canal": "WhatsApp → llamar"})
        ruta = ["1.º Revisa el log de exclusiones (Drive «ANS / Exclusión») — puede que ya esté registrado",
                "2.º Si no está: pregunta por el grupo de WhatsApp «ANS / Cuadrillas»",
                "3.º Si nadie responde: llama al líder o al supervisor"]
        pista = PISTA_CAMPO
        evidencia = ("SharePoint / ANS / Programados / <Mes> / exclusiones / 3.2A — el VIDEO del "
                     "frente de trabajo que grabó el líder (o, rara vez, el CORREO soporte)")

    elif campo in ("excluible_tiempos", "obs_tiempos"):
        titulo = "3.4 · ¿Se envió tarde la programación con excusa válida?"
        que = ("Aquí la causa es INTERNA (nuestra programación), no de campo. Solo se excluye si hubo un "
               "motivo real y demostrable para enviar tarde. Paola excluyó las 4 tardías de julio sin "
               "evidencia visible — no lo repitas: ante ENEL no se sostiene.")
        # El 3.4 NO es un dato tuyo: lo envía la programadora, y la columna H (sigla) dice CUÁL de
        # ellas elaboró ESTA orden. Ella tiene el correo del envío y sabe si hubo un motivo real.
        prog = (reg.sigla_elaborador or "").strip()
        if prog:
            contactos.append({"quien": prog, "rol": "Programadora que la elaboró (col H)",
                              "porque": "Es quien envió esta programación a validar: tiene el correo del envío "
                                        "y sabe si hubo un motivo real para el retraso.",
                              "canal": "WhatsApp «DILIGENCIAMIENTO ANS» → te reenvía el correo del envío"})
        else:
            contactos.append({"quien": "— sin sigla —", "rol": "Programadora (col H)",
                              "porque": "Esta fila no tiene sigla de elaborador, así que no se sabe qué "
                                        "programadora la envió. Hay que llenar primero la columna H.",
                              "canal": "—"})
        ruta = ["Pídele a la programadora el correo con el que envió la programación de esa semana",
                "Si se envió tarde por una causa real (caída del sistema, cambio de ENEL…), que te pase el soporte",
                "Sin soporte, no se excluye: cuenta en contra"]
        evidencia = "El correo del envío de la programación (lo tiene la programadora)"

    else:   # excluible2 / obs3 (3.3 UUCC) y cualquier otro
        titulo = "3.3 · Calidad de la información (UUCC)"
        que = "Decide si el incumplimiento de UUCC es excluible."
        contactos.append({"quien": "— no documentado —", "rol": "Sin dueño en la guía de Paola",
                          "porque": "La guía nunca dijo a quién se le pregunta esto. Es una duda abierta para Cristian.",
                          "canal": "—"})
        ruta = []
        evidencia = "—"

    # Paso 1 (solo 3.2 A): antes de preguntarle a nadie, buscar el video en el grupo.
    busqueda = _busqueda_grupo(reg) if campo in ("excluible", "obs") else None

    return {"titulo": titulo, "que_determinar": que, "contactos": contactos, "ruta": ruta,
            "pista": pista, "evidencia": evidencia, "busqueda": busqueda,
            "contexto": {"protocolo": reg.protocolo, "zona": zona or "—",
                         "estado": reg.estado_actual or "—", "anulado": anulado}}


@router.get("/sugerencia/{reg_id}")
async def sugerencia(reg_id: int, campo: str, db: AsyncSession = Depends(get_db),
                     current_user: Usuario = Depends(get_current_active_user)):
    """A quién preguntar para poder llenar esta celda, y por qué a esa persona. Por ahora solo
    la ve el desarrollador (es una ayuda en evaluación, no un procedimiento oficial)."""
    if getattr(current_user.rol, "value", str(current_user.rol)) != "desarrollador":
        raise HTTPException(status_code=403, detail="Sugerencias: solo el desarrollador por ahora")
    reg = await db.get(AnsRegistro, reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    sug = _sugerencia_celda(reg, campo, await _apoyo_map(db))   # apoyo: semilla + directorio

    # A cada contacto real (los "— sin asignar —" no) se le busca su teléfono en el directorio y
    # se le adjunta el mensaje ya redactado. Si no tiene número, el popup ofrece agregarlo.
    for c in sug["contactos"]:
        quien = c.get("quien") or ""
        if quien.startswith("—"):
            continue
        rol_dir = _rol_dir_de(c.get("rol"))       # una sola fuente de verdad (no duplicar aquí)
        ct = await _tel_de(db, quien)
        saludar = not await _ya_escrito_hoy(db, quien)
        c["contacto_id"] = ct.id if ct else None
        c["telefono"] = ct.telefono if ct else None
        c["mensaje"] = _mensaje_wa(reg, campo, quien, rol_dir, saludar=saludar)
        c["rol_dir"] = rol_dir
    return sug


# ── Directorio de contactos (CRUD) ───────────────────────────────────────────────
def _contacto_dict(c: Contacto) -> dict:
    return {"id": c.id, "nombre": c.nombre, "telefono": c.telefono, "cedula": c.cedula,
            "rol": c.rol, "zona": c.zona, "notas": c.notas}


@router.get("/contactos")
async def listar_contactos(buscar: Optional[str] = None, rol: Optional[str] = None,
                           db: AsyncSession = Depends(get_db)):
    q = select(Contacto).where(Contacto.activo.is_(True))
    if buscar:
        q = q.where(Contacto.nombre_norm.ilike(f"%{_norm_nombre(buscar)}%"))
    if rol:
        q = q.where(Contacto.rol.in_([r.strip() for r in rol.split(",")]))
    r = await db.execute(q.order_by(Contacto.nombre))
    return [_contacto_dict(c) for c in r.scalars().all()]


@router.post("/contactos")
async def guardar_contacto(datos: dict = Body(...), db: AsyncSession = Depends(get_db),
                           current_user: Usuario = Depends(get_current_active_user)):
    """Agregar o corregir a alguien en el directorio. Upsert por nombre normalizado. Sirve para
    dos cosas: teléfono (para WhatsApp) y/o zona+cédula (para el mapa responsable→zona). Al menos
    uno de teléfono/zona debe venir. IMPORTANTE: esto solo escribe el DIRECTORIO — NO toca ninguna
    fila del ANS ya cargada; el cambio aplica a los PRÓXIMOS imports."""
    nombre = (datos.get("nombre") or "").strip()
    if not nombre:
        raise HTTPException(status_code=400, detail="Falta el nombre")
    tel_in = (datos.get("telefono") or "").strip()
    tel = _norm_tel(tel_in) if tel_in else None
    if tel_in and not tel:
        raise HTTPException(status_code=400, detail="Número no válido. Escríbelo con indicativo o los 10 dígitos del móvil.")
    zona = (datos.get("zona") or "").strip().upper() or None
    if not tel and not zona:
        raise HTTPException(status_code=400, detail="Pon al menos el teléfono o la zona.")

    n = _norm_nombre(nombre)
    r = await db.execute(select(Contacto).where(Contacto.nombre_norm == n))
    c = r.scalar_one_or_none()
    if c:
        c.activo = True
        if tel:
            c.telefono = tel
        if zona:
            c.zona = zona
        c.rol = datos.get("rol") or c.rol
        if datos.get("cedula") is not None:
            c.cedula = (datos.get("cedula") or "").strip() or None
        if datos.get("notas"):
            c.notas = datos["notas"]
    else:
        c = Contacto(nombre=nombre, nombre_norm=n, telefono=tel or "", zona=zona,
                     rol=datos.get("rol"), cedula=(datos.get("cedula") or "").strip() or None,
                     notas=datos.get("notas"), creado_por=current_user.id)
        db.add(c)
    await db.commit()
    await db.refresh(c)
    return _contacto_dict(c)


@router.delete("/contactos/{cid}")
async def borrar_contacto(cid: int, db: AsyncSession = Depends(get_db)):
    c = await db.get(Contacto, cid)
    if not c:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")
    c.activo = False               # se desactiva, no se borra: el histórico queda
    await db.commit()
    return {"ok": True}


@router.get("/contactos/cobertura")
async def cobertura_contactos(periodo: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """A quién de los que aparecen en el ANS le falta número. Es el mapa de lo que te queda
    por completar: el directorio no se llena de golpe, se llena con el uso."""
    q = select(AnsRegistro.ri_jt, AnsRegistro.nombre_responsable, AnsRegistro.zona)
    if periodo:
        q = q.where(AnsRegistro.periodo == periodo)
    filas = (await db.execute(q)).all()

    gente: dict[str, dict] = {}
    for lider, sup, zona in filas:
        for nombre, rol in ((lider, "lider"), (sup, "supervisor")):
            n = _norm_nombre(nombre)
            if not n:
                continue
            g = gente.setdefault(n, {"nombre": (nombre or "").strip(), "rol": rol,
                                     "zona": zona or "", "ordenes": 0})
            g["ordenes"] += 1

    r = await db.execute(select(Contacto).where(Contacto.activo.is_(True)))
    tels = {c.nombre_norm: c.telefono for c in r.scalars().all()}

    out = []
    for n, g in gente.items():
        out.append({**g, "telefono": tels.get(n)})
    out.sort(key=lambda g: (g["telefono"] is not None, -g["ordenes"]))
    con = sum(1 for g in out if g["telefono"])
    return {"total": len(out), "con_telefono": con, "sin_telefono": len(out) - con, "gente": out}


# ── EVIDENCIA — los videos del grupo (el soporte de la exclusión) ────────────────
EVID_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "ans_evidencias"))
EVID_MAX_MB = 200          # un video de WhatsApp rara vez pasa de 60 MB


def _evid_dict(e: AnsEvidencia) -> dict:
    return {"id": e.id, "nombre": e.nombre, "mime": e.mime, "tamano": e.tamano,
            "nota": e.nota, "cuando": _bog(e.creado_en).isoformat() if e.creado_en else None}


@router.get("/evidencia")
async def evidencia_listar(reg_id: int, campo: str, db: AsyncSession = Depends(get_db)):
    dec = _CONSULTA_CFG.get(campo, (campo,))[0]
    r = await db.execute(select(AnsEvidencia).where(
        AnsEvidencia.reg_id == reg_id, AnsEvidencia.campo == dec).order_by(AnsEvidencia.id))
    return [_evid_dict(e) for e in r.scalars().all()]


@router.post("/evidencia")
async def evidencia_subir(reg_id: int = Form(...), campo: str = Form(...),
                          nota: Optional[str] = Form(None),
                          archivos: list[UploadFile] = File(...),
                          db: AsyncSession = Depends(get_db),
                          current_user: Usuario = Depends(get_current_active_user)):
    """Sube el/los video(s) que encontraste en el grupo. Suelen ser 2: uno al parar y otro al
    retomar. Quedan amarrados a la celda de exclusión y descargables como soporte."""
    if campo not in _CONSULTA_CFG:
        raise HTTPException(status_code=400, detail="Campo sin evidencia")
    reg = await _get_reg_o_404(db, reg_id)
    dec = _CONSULTA_CFG[campo][0]
    carpeta = os.path.join(EVID_DIR, current_entorno(), str(reg_id), dec)
    os.makedirs(carpeta, exist_ok=True)

    guardadas = []
    for up in archivos:
        datos = await up.read()
        if len(datos) > EVID_MAX_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"«{up.filename}» pasa de {EVID_MAX_MB} MB")
        seguro = re.sub(r"[^\w.\-]", "_", os.path.basename(up.filename or "archivo"))[:120]
        destino = os.path.join(carpeta, f"{int(datetime.now().timestamp())}_{seguro}")
        with open(destino, "wb") as f:
            f.write(datos)
        e = AnsEvidencia(reg_id=reg_id, campo=dec, nombre=up.filename or seguro, ruta=destino,
                         mime=up.content_type, tamano=len(datos), nota=nota,
                         subido_por=current_user.id)
        db.add(e)
        guardadas.append(e)

    # Deja constancia en la bitácora de la observación (si hay hilo abierto, o lo abre)
    c = await _hilo_de(db, reg_id, campo)
    if not c:
        c = AnsConsulta(reg_id=reg_id, campo=dec, estado="abierta", creado_por=current_user.id)
        db.add(c)
        await db.flush()
    n = len(guardadas)
    if dec == "excluible_anulado":     # 3.2 B: el soporte es el correo del Centro Operativo
        nota_obs = ("SE ANEXA CORREO DEL CENTRO OPERATIVO COMO SOPORTE DE LA ANULACIÓN. "
                    "PENDIENTE DETERMINAR LA EXCLUSIÓN.")
    else:                              # 3.2 A: video(s) del frente — y, rara vez, un correo/PDF soporte
        n_correo = sum(1 for g in guardadas if (g.nombre or "").lower().endswith((".msg", ".eml")))
        n_pdf = sum(1 for g in guardadas if (g.nombre or "").lower().endswith(".pdf"))
        n_video = n - n_correo - n_pdf
        partes = []
        if n_video:
            partes.append(f"{n_video} VIDEO{'S' if n_video > 1 else ''} DEL FRENTE DE TRABAJO "
                          f"(HALLADO{'S' if n_video > 1 else ''} EN EL GRUPO)")
        if n_correo:
            partes.append(f"{n_correo} CORREO{'S' if n_correo > 1 else ''}")
        if n_pdf:
            partes.append(f"{n_pdf} PDF")
        nota_obs = (f"SE ANEXA{'N' if n > 1 else ''} " + " Y ".join(partes) +
                    " COMO SOPORTE. PENDIENTE DETERMINAR LA EXCLUSIÓN.")
    _escribir_nota(reg, c, _CONSULTA_CFG[campo][1], nota_obs)
    await db.commit()
    for e in guardadas:
        await db.refresh(e)
    return {"ok": True, "archivos": [_evid_dict(e) for e in guardadas]}


@router.get("/evidencia/{eid}/descargar")
async def evidencia_descargar(eid: int, db: AsyncSession = Depends(get_db)):
    e = await db.get(AnsEvidencia, eid)
    if not e or not os.path.exists(e.ruta):
        raise HTTPException(status_code=404, detail="Evidencia no encontrada")
    return StreamingResponse(
        open(e.ruta, "rb"), media_type=e.mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{e.nombre}"'})


@router.delete("/evidencia/{eid}")
async def evidencia_borrar(eid: int, db: AsyncSession = Depends(get_db)):
    e = await db.get(AnsEvidencia, eid)
    if not e:
        raise HTTPException(status_code=404, detail="Evidencia no encontrada")
    try:
        os.remove(e.ruta)
    except OSError:
        pass                       # el registro se va igual; el archivo ya no está
    await db.delete(e)
    await db.commit()
    return {"ok": True}


# ── CONSULTA GUIADA CON IA — el hilo que resuelve una exclusión ──────────────────
# Envías el mensaje por WhatsApp, pegas la respuesta, y Claude la clasifica: si te remiten a
# otra persona arma el mensaje para esa persona (misma dinámica), si ya hay respuesta propone
# excluible + razón, y tú la apruebas para que se escriba en el ANS. El hilo entero queda como
# evidencia de cómo se llegó a la determinación. Solo dev por ahora (igual que la sugerencia).
#
# campo (cualquiera del par decisión/razón) → (decisión, razón, objetivo para la IA)
_CONSULTA_CFG = {
    "excluible":         ("excluible", "obs", "3.2 A", "determinar si el incumplimiento de tiempos es excluible y por qué"),
    "obs":               ("excluible", "obs", "3.2 A", "determinar si el incumplimiento de tiempos es excluible y por qué"),
    "excluible_anulado": ("excluible_anulado", "obs_anulado", "3.2 B", "determinar por qué se anuló la orden y si esa anulación es excluible"),
    "obs_anulado":       ("excluible_anulado", "obs_anulado", "3.2 B", "determinar por qué se anuló la orden y si esa anulación es excluible"),
    "excluible_tiempos": ("excluible_tiempos", "obs_tiempos", "3.4", "determinar si el envío tardío de la programación tuvo una causa válida y demostrable"),
    "obs_tiempos":       ("excluible_tiempos", "obs_tiempos", "3.4", "determinar si el envío tardío de la programación tuvo una causa válida y demostrable"),
}


# ── BITÁCORA VIVA — la observación se va escribiendo sola mientras se investiga ──
# Paola usaba la casilla de observación como un diario de la gestión, no solo para la razón final:
# «A LA ESPERA DEL CORREO DE EXCLUSIÓN POR PARTE DE AGOBARDO Y JOHN», «VÍCTOR MANIFIESTA QUE…».
# El chat ya sabe a quién se le preguntó y quién falta por responder, así que puede llevar esa
# bitácora solo. Mientras no haya determinación, la casilla de DECISIÓN se queda vacía (y sigue
# parpadeando): la nota informa, no decide.
def _nota_espera(quien: Optional[str]) -> str:
    q = (quien or "").strip()
    return f"SE CONSULTÓ A {q.upper()}. A LA ESPERA DE SU RESPUESTA." if q else \
           "CONSULTA EN CURSO. A LA ESPERA DE RESPUESTA."


def _nota_remite(de: Optional[str], a: Optional[str]) -> str:
    return (f"SE CONSULTÓ A {(de or '').strip().upper()}, QUIEN REMITE A {(a or '').strip().upper()}. "
            "A LA ESPERA DE SU RESPUESTA.")


def _escribir_nota(reg: AnsRegistro, c: AnsConsulta, razon_field: str, nota: Optional[str]) -> bool:
    """Actualiza la observación con la bitácora — pero NUNCA pisa lo que escribió una persona.
    Solo escribe si la casilla está vacía o si lo que hay es exactamente la nota que pusimos
    nosotros la vez pasada. Si alguien la editó a mano, se respeta y no se toca más."""
    if not nota:
        return False
    actual = (getattr(reg, razon_field, None) or "").strip()
    mia = (c.nota_auto or "").strip()
    if actual and actual != mia:
        return False                       # lo escribió una persona: intocable
    setattr(reg, razon_field, nota)
    c.nota_auto = nota
    return True


def _ctx_orden(reg: AnsRegistro, label: str) -> str:
    partes = [f"{reg.protocolo}"]
    if reg.zona:
        partes.append(reg.zona)
    if reg.fecha_inicio:
        partes.append(reg.fecha_inicio)
    if reg.estado_actual:
        partes.append(f"estado: {reg.estado_actual}")
    desc = (reg.descripcion or "").strip()
    if desc:
        partes.append(desc[:120])
    return f"[indicador {label}] " + " · ".join(partes)


def _ctx_orden_correo(reg: AnsRegistro) -> str:
    """Contexto TEMPORAL de la orden para cruzarlo con una directriz por fecha/hora del correo:
    lleva fecha Y horas planeadas de inicio/fin (que _ctx_orden normal no incluye)."""
    p = [f"Orden {reg.protocolo}"]
    if reg.zona:
        p.append(f"zona {reg.zona}")
    fi, hi = (reg.fecha_inicio or "").strip(), (reg.hora_inicio or "").strip()
    ff, hf = (reg.fecha_fin or "").strip(), (reg.hora_fin or "").strip()
    if fi:
        p.append(f"inicio planeado {fi} {hi}".strip())
    if ff:
        p.append(f"fin planeado {ff} {hf}".strip())
    if reg.estado_actual:
        p.append(f"estado {reg.estado_actual}")
    desc = (reg.descripcion or "").strip()          # trae el LCL
    if desc:
        p.append(desc[:180])
    return " · ".join(p)


def _conversa_txt(msgs) -> str:
    """Los turnos como texto plano para la IA."""
    out = []
    for m in msgs:
        if m.direccion == "enviado":
            out.append(f"enviado (a {m.persona or '—'}): {m.texto or ''}")
        elif m.direccion == "recibido":
            out.append(f"recibido ({m.persona or '—'}): {m.texto or ''}")
    return "\n".join(out)


def _rol_dir_de(rol_txt: str) -> str:
    t = (rol_txt or "").lower()
    return ("supervisor" if "supervisor" in t else "apoyo" if "apoyo" in t
            else "programadora" if "programadora" in t
            else "lider" if "líder" in t or "lider" in t else "otro")


# Roles cuyo nombre viene «APELLIDOS NOMBRES» (líderes col E y programadoras col H). Sin esto, a
# ROJAS CONTRERAS LUZ ANDREA se la saludaría «Buen día, Rojas».
_APELLIDOS_PRIMERO = ("lider", "programadora")


async def _destinatario(db: AsyncSession, reg: AnsRegistro, campo: str, quien: str,
                        rol: str, rol_dir: str, porque: str,
                        remitido_por: Optional[str] = None, remitido_rol: str = "otro") -> dict:
    """La persona con la que toca hablar AHORA, ya lista para el chat: su teléfono (si lo tenemos)
    y el borrador del mensaje. Si no tiene teléfono, el chat ofrece agregarlo."""
    ct = await _tel_de(db, quien)
    saludar = not await _ya_escrito_hoy(db, quien)      # no saludar 2 veces el mismo día
    return {"quien": quien, "rol": rol, "rol_dir": rol_dir, "porque": porque,
            "contacto_id": ct.id if ct else None, "telefono": ct.telefono if ct else None,
            "remitido_por": remitido_por, "saludado_hoy": not saludar,
            "mensaje": _mensaje_wa(reg, campo, quien, rol_dir, remitido_por, remitido_rol, saludar)}


async def _opciones(db: AsyncSession, reg: AnsRegistro, campo: str) -> list:
    """TODOS los candidatos a quien preguntarle (líder y supervisor, p.ej.), cada uno con SU
    borrador. El chat los muestra para que la persona ELIJA: el sistema sugiere, no impone."""
    sug = _sugerencia_celda(reg, campo, await _apoyo_map(db))   # apoyo: semilla + directorio
    out = []
    for c in sug["contactos"]:
        quien = c.get("quien") or ""
        if quien.startswith("—"):          # "sin asignar" / "no documentado": no se le puede escribir
            continue
        out.append(await _destinatario(db, reg, campo, quien, c.get("rol") or "",
                                       _rol_dir_de(c.get("rol")), c.get("porque") or ""))
    return out


async def _consulta_dict(db: AsyncSession, c: Optional[AnsConsulta], reg: AnsRegistro,
                         campo: str) -> dict:
    """El hilo completo + en qué paso va + con quién toca hablar (con borrador). El frontend solo
    pinta: toda la lógica de 'qué sigue' se resuelve aquí."""
    decision, razon_field, label, _obj = _CONSULTA_CFG[campo]
    msgs, dec = [], None
    if c:
        r = await db.execute(select(AnsConsultaMsg).where(AnsConsultaMsg.consulta_id == c.id)
                             .order_by(AnsConsultaMsg.id))
        msgs = r.scalars().all()
        for m in reversed(msgs):
            if m.direccion == "ia" and m.meta:
                try:
                    dec = json.loads(m.meta)
                except Exception:
                    dec = None
                break

    out = {
        "id": c.id if c else None, "reg_id": reg.id, "campo": decision, "campo_click": campo,
        "estado": c.estado if c else "nueva", "indicador": label,
        "orden": reg.protocolo, "zona": reg.zona, "fecha": reg.fecha_inicio,
        "decision_campo": decision, "razon_campo": razon_field,
        "propuesta": None, "destinatario": None, "opciones": [], "ya_enviado": False,
        "paso": "enviar", "falta": None, "ia_error": None,
        # La bitácora que el hilo lleva escrita en la casilla de observación, ahora mismo
        "nota": (getattr(reg, razon_field, None) or "") if c else "",
        "mensajes": [{"direccion": m.direccion, "persona": m.persona, "texto": m.texto,
                      "meta": json.loads(m.meta) if (m.direccion == "ia" and m.meta) else None,
                      "cuando": _bog(m.creado_en).isoformat() if m.creado_en else None} for m in msgs],
    }

    if c and c.estado == "resuelta":
        out["paso"] = "resuelta"
        return out

    if dec and dec.get("accion") == "resolver" and dec.get("excluible"):
        out["propuesta"] = {"excluible": dec.get("excluible"), "categoria": dec.get("categoria"),
                            "razon": dec.get("razon"), "resumen": dec.get("resumen")}
        out["paso"] = "decidir"
        return out

    if dec and dec.get("accion") == "seguir":
        out["falta"] = dec.get("razon")
    elif dec and dec.get("accion") == "manual":
        out["ia_error"] = dec.get("error", "La IA no pudo interpretar la respuesta; decide a mano.")

    opciones = await _opciones(db, reg, campo)

    # ¿Con quién hablamos ahora?
    if dec and dec.get("accion") == "redirigir" and dec.get("siguiente_persona"):
        # Nos remiten a alguien concreto: no hay nada que elegir.
        quien = dec["siguiente_persona"]
        ult = next((m for m in reversed(msgs) if m.direccion == "recibido"), None)
        remitio = ult.persona if ult else None
        rol_rem = "otro"
        if ult and ult.meta:                     # el rol de quien nos remitió, para saludarlo bien
            try:
                rol_rem = (json.loads(ult.meta) or {}).get("rol_dir") or "otro"
            except Exception:
                pass
        out["destinatario"] = await _destinatario(
            db, reg, campo, quien, "Le remiten con esta persona", "otro",
            f"{remitio or 'La persona anterior'} indicó que es quien sabe la respuesta.",
            remitio, rol_rem)
    elif c and c.persona_actual:
        # Ya elegiste a alguien: mantenlo (y conserva su rol si es uno de los sugeridos).
        n = _norm_nombre(c.persona_actual)
        elegido = next((o for o in opciones if _norm_nombre(o["quien"]) == n), None)
        out["destinatario"] = elegido or await _destinatario(
            db, reg, campo, c.persona_actual, "Persona a la que le estás preguntando", "otro", "")
    else:
        # Aún no has elegido: el chat muestra las opciones y tú decides (por defecto, la primera).
        out["destinatario"] = opciones[0] if opciones else None
        out["opciones"] = opciones

    if out["destinatario"]:
        n = _norm_nombre(out["destinatario"]["quien"])
        out["ya_enviado"] = any(m.direccion == "enviado" and _norm_nombre(m.persona) == n for m in msgs)
    out["paso"] = "esperar" if out["ya_enviado"] else "enviar"
    return out


async def _get_reg_o_404(db: AsyncSession, reg_id: int) -> AnsRegistro:
    reg = await db.get(AnsRegistro, reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    return reg


def _solo_dev(current_user: Usuario):
    if getattr(current_user.rol, "value", str(current_user.rol)) != "desarrollador":
        raise HTTPException(status_code=403, detail="Consulta guiada: solo el desarrollador por ahora")


async def _hilo_de(db: AsyncSession, reg_id: int, campo: str) -> Optional[AnsConsulta]:
    decision = _CONSULTA_CFG[campo][0]
    r = await db.execute(select(AnsConsulta).where(
        AnsConsulta.reg_id == reg_id, AnsConsulta.campo == decision,
        AnsConsulta.estado != "descartada").order_by(AnsConsulta.id.desc()))
    return r.scalars().first()


@router.get("/consulta")
async def consulta_get(reg_id: int, campo: str, db: AsyncSession = Depends(get_db),
                       current_user: Usuario = Depends(get_current_active_user)):
    """El chat de esta celda: los mensajes, en qué paso va y con quién toca hablar. Si aún no hay
    hilo, devuelve igual el primer destinatario con su borrador (para poder arrancar)."""
    _solo_dev(current_user)
    if campo not in _CONSULTA_CFG:
        raise HTTPException(status_code=400, detail="Campo sin consulta guiada")
    reg = await _get_reg_o_404(db, reg_id)
    c = await _hilo_de(db, reg_id, campo)
    return await _consulta_dict(db, c, reg, campo)


@router.post("/consulta/enviado")
async def consulta_enviado(datos: dict = Body(...), db: AsyncSession = Depends(get_db),
                           current_user: Usuario = Depends(get_current_active_user)):
    """Deja constancia del mensaje que REALMENTE enviaste (tal como quedó tras editarlo). Crea el
    hilo si es el primero. Body: {reg_id, campo, persona, texto}."""
    _solo_dev(current_user)
    reg_id, campo = datos.get("reg_id"), datos.get("campo")
    persona = (datos.get("persona") or "").strip() or None
    texto = (datos.get("texto") or "").strip()
    rol_dir = datos.get("rol_dir") or "otro"
    if not reg_id or campo not in _CONSULTA_CFG or not texto or not persona:
        raise HTTPException(status_code=400, detail="Faltan reg_id, campo, persona o el texto enviado")
    reg = await _get_reg_o_404(db, reg_id)
    c = await _hilo_de(db, reg_id, campo)
    if not c:
        c = AnsConsulta(reg_id=reg_id, campo=_CONSULTA_CFG[campo][0], estado="abierta",
                        persona_actual=persona, creado_por=current_user.id)
        db.add(c)
        await db.flush()
    c.persona_actual = persona                 # a quien elegiste: el hilo se queda con él
    db.add(AnsConsultaMsg(consulta_id=c.id, direccion="enviado", persona=persona, texto=texto[:4000],
                          meta=json.dumps({"rol_dir": rol_dir})))
    # La observación empieza a contar la historia desde el PRIMER mensaje. Después, la bitácora la
    # gobierna la IA: si aquí escribiéramos siempre, al mandarle el mensaje a la persona a la que
    # nos remitieron pisaríamos la nota buena («…QUIEN REMITE A DANIEL PARRA») con una pobre.
    if not (c.nota_auto or "").strip():
        _escribir_nota(reg, c, _CONSULTA_CFG[campo][1], _nota_espera(persona))
    await db.commit()
    await db.refresh(c)
    return await _consulta_dict(db, c, reg, campo)


@router.post("/consulta/analizar")
async def consulta_analizar(datos: dict = Body(...), db: AsyncSession = Depends(get_db),
                            current_user: Usuario = Depends(get_current_active_user)):
    """Registra la respuesta que llegó por WhatsApp y la clasifica con la IA.
    Body: {reg_id, campo, persona, texto}."""
    _solo_dev(current_user)
    reg_id = datos.get("reg_id")
    campo = datos.get("campo")
    persona = (datos.get("persona") or "").strip() or None
    texto = (datos.get("texto") or "").strip()
    if not reg_id or campo not in _CONSULTA_CFG:
        raise HTTPException(status_code=400, detail="Faltan reg_id o un campo válido")
    if not texto:
        raise HTTPException(status_code=400, detail="Pega la respuesta que te dieron")

    decision, razon_field, label, objetivo = _CONSULTA_CFG[campo]
    reg = await _get_reg_o_404(db, reg_id)

    c = await _hilo_de(db, reg_id, campo)
    if not c:                       # respuesta sin haber registrado envío: igual abrimos el hilo
        c = AnsConsulta(reg_id=reg_id, campo=decision, estado="abierta",
                        persona_actual=persona, creado_por=current_user.id)
        db.add(c)
        await db.flush()
    # Guardamos su rol: si nos remite a otro, hace falta para saludar bien a quien nos remitió
    # («PEÑA LOPEZ JAIME» es líder → se llama Jaime, no Peña).
    db.add(AnsConsultaMsg(consulta_id=c.id, direccion="recibido", persona=persona, texto=texto[:4000],
                          meta=json.dumps({"rol_dir": datos.get("rol_dir") or "otro"})))
    await db.flush()

    # Clasifica TODA la conversación acumulada
    rr = await db.execute(select(AnsConsultaMsg).where(AnsConsultaMsg.consulta_id == c.id)
                          .order_by(AnsConsultaMsg.id))
    convo = _conversa_txt(rr.scalars().all())
    dec = await _ia_clasificar(objetivo, _ctx_orden(reg, label), convo)

    db.add(AnsConsultaMsg(consulta_id=c.id, direccion="ia", persona=None,
                          texto=dec.get("resumen") or dec.get("error"),
                          meta=json.dumps(dec, ensure_ascii=False)))

    # Actualiza el estado del hilo + la bitácora de la observación. La IA la redacta al estilo de
    # Paola; si falla, una plantilla determinista deja la nota igual (nunca nos quedamos sin ella).
    nota = (dec.get("bitacora") or "").strip() or None
    if dec.get("accion") == "resolver" and dec.get("excluible"):
        c.excluible_sug = dec.get("excluible"); c.categoria = dec.get("categoria")
        c.razon_sug = dec.get("razon"); c.resumen = dec.get("resumen")
        nota = nota or dec.get("razon")          # aún NO se decide: eso lo aprueba una persona
    elif dec.get("accion") == "redirigir" and dec.get("siguiente_persona"):
        nota = nota or _nota_remite(persona, dec["siguiente_persona"])
        c.persona_actual = dec["siguiente_persona"]; c.resumen = dec.get("resumen")
    else:                                        # seguir / manual
        # Si la IA no redactó nota, conservamos la que ya había: nunca degradar una nota rica
        # («…MANIFIESTA QUE LLOVIÓ, A LA ESPERA DEL VIDEO») a un genérico «a la espera».
        nota = nota or c.nota_auto or _nota_espera(persona)
    _escribir_nota(reg, c, razon_field, nota)

    await db.commit()
    await db.refresh(c)
    return await _consulta_dict(db, c, reg, campo)


@router.post("/consulta/{cid}/aplicar")
async def consulta_aplicar(cid: int, datos: dict = Body(default={}), db: AsyncSession = Depends(get_db),
                           current_user: Usuario = Depends(get_current_active_user)):
    """Escribe en el ANS la determinación propuesta y cierra el hilo. Es el paso de aprobación: la
    IA propone, una persona confirma. `razon` (opcional) permite corregirla antes.
    3.2 A (campo=excluible/obs, venga de video o del chat de investigación): el autorrellenado
    SOLO escribe la OBSERVACIÓN — el SI/NO de excluible lo decide la persona a mano en el grid
    (pedido de Sergio, 2026-07-21: el juicio de exclusión de tiempos no se delega a la IA, aunque
    su redacción de la observación sí se aproveche). 3.2 B / 3.4 siguen escribiendo decisión+razón."""
    _solo_dev(current_user)
    c = await db.get(AnsConsulta, cid)
    if not c:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")
    if (datos or {}).get("razon"):                  # la razón editada en el chat manda
        c.razon_sug = str(datos["razon"]).strip()
    if not c.razon_sug:
        raise HTTPException(status_code=400, detail="Aún no hay una observación para aplicar")
    reg = await _get_reg_o_404(db, c.reg_id)
    decision_field, razon_field, _l, _o = _CONSULTA_CFG.get(c.campo, (c.campo, "obs", "", ""))
    solo_obs = decision_field == "excluible"   # 3.2 A: la decisión la toma la persona, no la IA
    setattr(reg, razon_field, c.razon_sug)
    extra = {}
    if not solo_obs:
        if not c.excluible_sug:
            raise HTTPException(status_code=400, detail="Aún no hay una determinación para aplicar")
        setattr(reg, decision_field, c.excluible_sug)
        # 3.2 B: al excluir la anulación, «Se ejecutó» (AN) va N/A; si no se excluye, va NO.
        # Es exactamente lo que hacía Paola: 40 filas N/A+SI, 3 filas NO+NO.
        if decision_field == "excluible_anulado":
            reg.se_ejecuto = "N/A" if c.excluible_sug.upper() == "SI" else "NO"
            extra = {"campo_extra": "se_ejecuto", "valor_extra": reg.se_ejecuto}
    # Al aplicar la exclusión del 3.2 B pueden dispararse las reglas de nodos (AV→0 si la
    # anulada excluida ya tenía nodos de María Inés) o invalidarse una marca previa.
    _log_reglas_nodos(db, reg, _eval_reglas_nodos(reg))
    c.nota_auto = c.razon_sug            # la razón final sustituye a la bitácora
    c.estado = "resuelta"
    await db.commit()
    out = {"ok": True, "reg_id": reg.id, "campo_razon": razon_field, "valor_razon": c.razon_sug, **extra}
    if not solo_obs:
        out["campo_decision"], out["valor_decision"] = decision_field, c.excluible_sug
    return out


@router.post("/correo/analizar")
async def correo_analizar(datos: dict = Body(...), db: AsyncSession = Depends(get_db),
                          current_user: Usuario = Depends(get_current_active_user)):
    """Lee un correo ya subido como evidencia (.msg/.eml o imagen) y lo CRUZA con la orden usando
    Claude (texto + imágenes). Propone si se relaciona y si sustenta la exclusión, con un
    razonamiento CONCRETO para que el humano lo verifique y apruebe. Guarda la propuesta en el
    hilo de la celda → se aprueba con /consulta/{id}/aplicar (humano↔máquina).
    Nació para el 3.2 B (anulaciones); también sirve en el 3.2 A (tiempos) para el caso raro del
    correo soporte — mismo flujo, pero con el prompt/categorías del 3.2 A, y al aplicar solo se
    escribe la OBSERVACIÓN (el SI/NO del 3.2 A siempre lo decide la persona)."""
    _solo_dev(current_user)
    reg_id, campo, evid_id = datos.get("reg_id"), datos.get("campo"), datos.get("evidencia_id")
    if campo not in ("excluible_anulado", "obs_anulado", "excluible", "obs"):
        raise HTTPException(status_code=400,
                            detail="El análisis de correo es para el 3.2 B (anulaciones) o el 3.2 A (tiempos)")
    reg = await _get_reg_o_404(db, reg_id)
    e = await db.get(AnsEvidencia, evid_id)
    if not e or not os.path.exists(e.ruta):
        raise HTTPException(status_code=404, detail="No encuentro el correo subido")

    ext = (e.nombre.rsplit(".", 1)[-1] or "").lower()
    pdfs = []
    if ext in ("msg", "eml"):              # .msg = Outlook clásico · .eml = Outlook nuevo/web (arrastre)
        c_msg = _parse_msg(e.ruta) if ext == "msg" else _parse_eml(e.ruta)
        urls = [_img_url(n, b) for n, b in c_msg["imagenes"]]
        correo = {k: c_msg[k] for k in ("asunto", "de", "fecha", "cuerpo")}
        n_imgs = len(urls)
    elif ext in ("png", "jpg", "jpeg", "gif", "bmp", "webp"):     # un pantallazo suelto
        with open(e.ruta, "rb") as f:
            urls = [_img_url(e.nombre, f.read())]
        correo = {"asunto": "(pantallazo)", "de": "", "fecha": "", "cuerpo": ""}
        n_imgs = 1
    elif ext == "pdf":                     # Claude lee el PDF nativo (texto + páginas como imagen)
        with open(e.ruta, "rb") as f:
            pdfs = [base64.b64encode(f.read()).decode()]
        correo = {"asunto": f"(PDF adjunto: {e.nombre})", "de": "", "fecha": "", "cuerpo": ""}
        urls, n_imgs = [], 0
    else:
        raise HTTPException(status_code=400, detail="Solo puedo analizar .msg, .eml, PDF o una imagen (pantallazo).")

    dec = _CONSULTA_CFG[campo]
    ctx = _ctx_orden_correo(reg)         # con fecha Y horas, para el cruce con directrices
    res = await _ia_correo(ctx, correo, urls, pdfs=pdfs,
                           indicador="3.2A" if campo in ("excluible", "obs") else "3.2B")
    if res.get("error"):
        return {"error": res["error"], "imagenes": n_imgs, "pdfs": len(pdfs)}

    relacionado = str(res.get("relacionado", "")).upper() == "SI"
    excluible = (res.get("excluible") or "").upper()
    excluible = excluible if excluible in ("SI", "NO") else "NO"      # la IA siempre decide
    razon = (res.get("razon") or "").strip() or None
    razonamiento = (res.get("razonamiento") or "").strip()

    # Guarda la propuesta en el hilo de la celda (crea el hilo si no hay). Ahora SIEMPRE hay una
    # decisión (SI/NO), así que siempre queda guardada y lista para autorrellenar; el razonamiento
    # se conserva en el hilo (no se pierde al cerrar el popup).
    c = await _hilo_de(db, reg_id, campo)
    if not c:
        c = AnsConsulta(reg_id=reg_id, campo=dec[0], estado="abierta", creado_por=current_user.id)
        db.add(c)
        await db.flush()
    db.add(AnsConsultaMsg(consulta_id=c.id, direccion="ia", persona="Correo (IA)",
                          texto=razonamiento, meta=json.dumps(res, ensure_ascii=False)))
    if razon:
        c.excluible_sug, c.razon_sug, c.categoria = excluible, razon, res.get("categoria")
    await db.commit()

    return {"consulta_id": c.id, "relacionado": "SI" if relacionado else "NO",
            "excluible": excluible, "categoria": res.get("categoria"), "razon": razon,
            "razonamiento": razonamiento, "imagenes": n_imgs, "pdfs": len(pdfs),
            "aprobable": bool(razon)}


@router.get("/correo/ultimo")
async def correo_ultimo(reg_id: int, campo: str, db: AsyncSession = Depends(get_db),
                        current_user: Usuario = Depends(get_current_active_user)):
    """El último análisis de correo guardado para esta celda, para que el popup lo recupere y NO se
    pierda el razonamiento al cerrarlo/reabrirlo."""
    _solo_dev(current_user)
    if campo not in ("excluible_anulado", "obs_anulado"):
        return {"existe": False}
    c = await _hilo_de(db, reg_id, campo)
    if not c:
        return {"existe": False}
    r = await db.execute(select(AnsConsultaMsg).where(
        AnsConsultaMsg.consulta_id == c.id, AnsConsultaMsg.persona == "Correo (IA)")
        .order_by(AnsConsultaMsg.id.desc()))
    m = r.scalars().first()
    if not m or not m.meta:
        return {"existe": False}
    try:
        res = json.loads(m.meta)
    except Exception:
        return {"existe": False}
    excl = (res.get("excluible") or "").upper()
    excl = excl if excl in ("SI", "NO") else "NO"
    return {"existe": True, "consulta_id": c.id, "aplicado": c.estado == "resuelta",
            "relacionado": "SI" if str(res.get("relacionado", "")).upper() == "SI" else "NO",
            "excluible": excl, "categoria": res.get("categoria"),
            "razon": (res.get("razon") or "").strip() or None,
            "razonamiento": (res.get("razonamiento") or "").strip(),
            "aprobable": bool((res.get("razon") or "").strip())}


@router.post("/video/analizar")
async def video_analizar(datos: dict = Body(...), db: AsyncSession = Depends(get_db),
                         current_user: Usuario = Depends(get_current_active_user)):
    """3.2 A: el protocolo pide primero el/los video(s) del frente de trabajo, y en ellos la
    cuadrilla EXPLICA de viva voz el motivo del incumplimiento. La IA no sirve para escuchar esa
    parte — la API de Claude no procesa video ni audio — así que sin
    esto la única nota que quedaba era el genérico "SE ANEXA N VIDEO(S)... PENDIENTE DETERMINAR LA
    EXCLUSIÓN": el video estaba subido pero nunca oído. Aquí se transcribe el audio LOCAL (ffmpeg +
    faster-whisper, ver `services.transcribe`) y esa transcripción es la que Claude cruza con la
    orden para proponer excluible + razón. Analiza TODOS los videos de la celda juntos (suelen ser
    2: uno al parar, otro al retomar) para reconstruir la explicación completa. En raras ocasiones
    el soporte del 3.2 A llega (además o en vez del video) como un CORREO (.msg/.eml) — p. ej. una
    directriz de ENEL o un aviso de vía cerrada: aquí también se parsea (texto + pantallazos) y se
    cruza JUNTO con las transcripciones en la misma llamada. Se aprueba con
    /consulta/{id}/aplicar (humano↔máquina), igual que el correo del 3.2 B."""
    _solo_dev(current_user)
    reg_id, campo = datos.get("reg_id"), datos.get("campo")
    forzar = bool(datos.get("forzar"))     # "Reinterpretar": ignora la transcripción cacheada y la
                                            # vuelve a intentar (por si un video quedó "" por un
                                            # fallo transitorio de ffmpeg/whisper, no por falta real de audio)
    if campo not in ("excluible", "obs"):
        raise HTTPException(status_code=400, detail="El análisis de video es para el 3.2 A (tiempos)")
    reg = await _get_reg_o_404(db, reg_id)
    dec = _CONSULTA_CFG[campo][0]
    todas = (await db.execute(select(AnsEvidencia).where(
        AnsEvidencia.reg_id == reg_id, AnsEvidencia.campo == dec).order_by(AnsEvidencia.id))).scalars().all()
    # Un .msg/.eml/.pdf no es video: si entrara a ffmpeg la transcripción fallaría siempre
    correos_ev = [e for e in todas if (e.nombre or "").lower().endswith((".msg", ".eml", ".pdf"))]
    videos = [v for v in todas if v not in correos_ev and not (v.mime or "").startswith("image/")]
    if not videos and not correos_ev:
        raise HTTPException(status_code=404, detail="No hay video, correo ni PDF subido para esta celda")

    transcripciones, cambios = [], False
    for v in videos:
        if forzar or v.transcripcion is None:   # cachea salvo que se pida reinterpretar
            v.transcripcion = (await asyncio.to_thread(transcribir_video, v.ruta)) or ""
            cambios = True
        transcripciones.append({"nombre": v.nombre, "texto": v.transcripcion})
    if cambios:
        await db.commit()

    # Correos/PDF soporte del 3.2 A (raros): texto + pantallazos, igual que en el 3.2 B; el PDF
    # va nativo (Claude lo lee directo). Uno ilegible no tumba el análisis de los demás soportes.
    correos_txt, urls, pdfs = [], [], []
    for e in correos_ev:
        if not os.path.exists(e.ruta):
            continue
        low = e.nombre.lower()
        try:
            if low.endswith(".pdf"):
                with open(e.ruta, "rb") as f:
                    pdfs.append(base64.b64encode(f.read()).decode())
                continue
            c_msg = _parse_msg(e.ruta) if low.endswith(".msg") else _parse_eml(e.ruta)
        except Exception:                       # noqa: BLE001 — soporte corrupto: sigue con el resto
            continue
        correos_txt.append({"nombre": e.nombre,
                            **{k: c_msg[k] for k in ("asunto", "de", "fecha", "cuerpo")}})
        urls += [_img_url(n, b) for n, b in c_msg["imagenes"]]

    if not any(t["texto"] for t in transcripciones) and not correos_txt and not pdfs:
        msg = ("No se detectó voz explicando el motivo en el/los video(s) — video mudo, "
               "ruido de campo sin hablar, o audio no entendible."
               if videos else "No pude leer el/los soporte(s) adjunto(s) — archivo dañado o vacío.")
        return {"error": msg, "videos": len(videos), "correos": len(correos_ev)}

    ctx = _ctx_orden_correo(reg)
    res = await _ia_video(ctx, transcripciones, correos=correos_txt, imagenes_data_urls=urls,
                          pdfs=pdfs)
    if res.get("error"):
        return {"error": res["error"], "videos": len(videos),
                "correos": len(correos_txt), "pdfs": len(pdfs)}

    excluible = (res.get("excluible") or "").upper()
    excluible = excluible if excluible in ("SI", "NO") else "NO"      # la IA siempre decide
    razon = (res.get("razon") or "").strip() or None
    razonamiento = (res.get("razonamiento") or "").strip()

    c = await _hilo_de(db, reg_id, campo)
    if not c:
        c = AnsConsulta(reg_id=reg_id, campo=dec, estado="abierta", creado_por=current_user.id)
        db.add(c)
        await db.flush()
    db.add(AnsConsultaMsg(consulta_id=c.id, direccion="ia", persona="Video (IA)",
                          texto=razonamiento, meta=json.dumps(res, ensure_ascii=False)))
    if razon:
        c.excluible_sug, c.razon_sug, c.categoria = excluible, razon, res.get("categoria")
    await db.commit()

    transcripcion_vista = " / ".join(t["texto"] for t in transcripciones if t["texto"])[:2000]
    return {"consulta_id": c.id, "excluible": excluible, "categoria": res.get("categoria"),
            "razon": razon, "razonamiento": razonamiento, "videos": len(videos),
            "correos": len(correos_txt), "imagenes": len(urls), "pdfs": len(pdfs),
            "transcripcion": transcripcion_vista, "aprobable": bool(razon)}


@router.get("/video/ultimo")
async def video_ultimo(reg_id: int, campo: str, db: AsyncSession = Depends(get_db),
                       current_user: Usuario = Depends(get_current_active_user)):
    """El último análisis guardado para esta celda del 3.2 A — del video o del correo soporte
    (ambos escriben en el mismo hilo) — para que no se pierda al cerrar/reabrir el popup
    (mismo patrón que /correo/ultimo)."""
    _solo_dev(current_user)
    if campo not in ("excluible", "obs"):
        return {"existe": False}
    c = await _hilo_de(db, reg_id, campo)
    if not c:
        return {"existe": False}
    r = await db.execute(select(AnsConsultaMsg).where(
        AnsConsultaMsg.consulta_id == c.id,
        AnsConsultaMsg.persona.in_(("Video (IA)", "Correo (IA)")))
        .order_by(AnsConsultaMsg.id.desc()))
    m = r.scalars().first()
    if not m or not m.meta:
        return {"existe": False}
    try:
        res = json.loads(m.meta)
    except Exception:
        return {"existe": False}
    excl = (res.get("excluible") or "").upper()
    excl = excl if excl in ("SI", "NO") else "NO"
    out = {"existe": True, "consulta_id": c.id, "aplicado": c.estado == "resuelta",
           "excluible": excl, "categoria": res.get("categoria"),
           "razon": (res.get("razon") or "").strip() or None,
           "razonamiento": (res.get("razonamiento") or "").strip(),
           "aprobable": bool((res.get("razon") or "").strip())}
    if res.get("relacionado"):     # solo el análisis de correo trae este campo
        out["relacionado"] = "SI" if str(res["relacionado"]).upper() == "SI" else "NO"
    return out


@router.get("/tareas")
async def tareas(periodo: str, db: AsyncSession = Depends(get_db),
                 current_user: Usuario = Depends(get_current_active_user)):
    """Campanita: qué le falta llenar a cada rol en este periodo. Los roles que ven todo
    (dev/ans/directivo) reciben TODAS las tareas; los demás solo la suya."""
    regs = (await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo))).scalars().all()
    rol = getattr(current_user.rol, "value", str(current_user.rol))
    ve_todo = _ans_ve_todo(current_user.rol, current_user.email)   # única fuente: config.py
    out = []
    for t in TAREAS_ANS:
        if not ve_todo and t["rol"] != rol:
            continue
        pend = sum(1 for r in regs if _pendiente(r, t["id"]))
        out.append({"id": t["id"], "titulo": t["titulo"], "rol_label": t["rol_label"],
                    "columnas": t["columnas"], "ayuda": t["ayuda"],
                    "pendientes": pend, "mio": t["rol"] == rol})
    return {"rol": rol, "ve_todo": ve_todo, "periodo": periodo, "registros": len(regs),
            "tareas": out, "total_pendientes": sum(t["pendientes"] for t in out)}


# ── FILTROS DE FLUJO (solo dev — Sergio) ─────────────────────────────────────────
# Un «filtro de flujo» sigue el ESTADO de un caso dentro del proceso de trabajo de Sergio,
# no el valor de una celda; se irán construyendo más con el tiempo (el término es de él y va
# a automatizar su trabajo al final). V1 «No excluibles»: casos con NO en alguno de sus
# procesos (3.2 A/B, 3.3). El circuito real (explicado por Santana): NO → Santana busca el
# correo y lo reenvía → se sube como evidencia → la IA lo analiza → Sergio aprueba o no →
# si el NO queda definitivo, terminará en un correo a los ingenieros de zona (futuro).
_FLUJO_PROCESOS = (("excluible", "3.2 A"), ("excluible_anulado", "3.2 B"), ("excluible2", "3.3"))


@router.get("/flujo/no-excluibles")
async def flujo_no_excluibles(periodo: str, db: AsyncSession = Depends(get_db),
                              current_user: Usuario = Depends(get_current_active_user)):
    """Clasifica cada caso NO-excluible del periodo en su etapa del flujo:
    sin_soporte (pedir el correo a Santana) → soporte_sin_analizar (subido, falta la IA) →
    propuesta_si (la IA propone revertir, falta aprobar) · no_definitivo (la IA confirmó el
    NO → candidato al futuro correo a ing. de zona). Un reg puede estar en varias etapas si
    tiene NO en más de un proceso."""
    _solo_dev(current_user)
    regs = (await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo))).scalars().all()
    nos = [(r, campo, ind) for r in regs for campo, ind in _FLUJO_PROCESOS
           if (getattr(r, campo, "") or "").strip().upper() == "NO"]
    ids = {r.id for r, _, _ in nos}
    ev, con = set(), {}
    if ids:
        ent = current_entorno()
        for reg_id, campo in (await db.execute(
                select(AnsEvidencia.reg_id, AnsEvidencia.campo)
                .where(AnsEvidencia.reg_id.in_(ids), AnsEvidencia.entorno == ent))).all():
            dec = _CONSULTA_CFG.get(campo)
            ev.add((reg_id, dec[0] if dec else campo))     # evidencia cuenta para su celda de decisión
        for c in (await db.execute(
                select(AnsConsulta).where(AnsConsulta.reg_id.in_(ids)))).scalars().all():
            con[(c.reg_id, c.campo)] = c

    casos = {}
    for r, campo, ind in nos:
        c = con.get((r.id, campo))
        sug = ((c.excluible_sug if c else "") or "").strip().upper()
        if sug == "SI":
            etapa = "propuesta_si"
        elif sug == "NO":
            etapa = "no_definitivo"
        elif (r.id, campo) in ev:
            etapa = "soporte_sin_analizar"
        else:
            etapa = "sin_soporte"
        d = casos.setdefault(r.id, {"reg_id": r.id, "protocolo": r.protocolo, "etapas": [], "procesos": []})
        d["procesos"].append({"campo": campo, "indicador": ind, "etapa": etapa})
        if etapa not in d["etapas"]:
            d["etapas"].append(etapa)

    conteos = {}
    for d in casos.values():
        for e in d["etapas"]:
            conteos[e] = conteos.get(e, 0) + 1
    return {"periodo": periodo, "total": len(casos), "conteos": conteos, "casos": list(casos.values())}


@router.get("")
async def listar(
    periodo: Optional[str] = None,
    tipo: Optional[str] = None,
    zona: Optional[str] = None,
    q: Optional[str] = None,
    pendiente: Optional[str] = None,      # id de tarea → solo las filas que le faltan a ese rol
    apoyo: bool = False,                  # «Mis casos» del profesional de apoyo (ver abajo)
    skip: int = Query(0, ge=0),
    limit: int = Query(3000, le=5000),
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_current_active_user),
):
    f = []
    # «Mis casos»: las órdenes que involucran al profesional de apoyo — las de SU zona que anuló
    # el Centro Operativo, que son de las que él consigue el correo de ENEL para el 3.2 B. Se
    # ignora en silencio si quien pregunta no es apoyo: el filtro no es un permiso.
    if apoyo:
        z_apoyo = await _zona_apoyo_de(db, user)
        if z_apoyo:
            f.append(AnsRegistro.zona == z_apoyo)
            f.append(_cond_anulada())
    if periodo:
        f.append(AnsRegistro.periodo == periodo)
    if tipo:
        f.append(AnsRegistro.tipo == tipo)
    if zona:
        f.append(AnsRegistro.zona == zona)
    if q:
        like = f"%{q}%"
        f.append(or_(AnsRegistro.protocolo.ilike(like), AnsRegistro.pdl.ilike(like),
                     AnsRegistro.nombre_responsable.ilike(like), AnsRegistro.descripcion.ilike(like)))
    base = select(AnsRegistro)
    if f:
        base = base.where(and_(*f))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    items = (await db.execute(base.order_by(AnsRegistro.tipo, AnsRegistro.id).offset(skip).limit(limit))).scalars().all()
    if pendiente:   # el filtro depende de campos calculados → se aplica en Python
        items = [b for b in items if _pendiente(b, pendiente)]
        total = len(items)
    return {"total": total, "items": [_to_dict(b) for b in items]}


@router.get("/mi-apoyo")
async def mi_apoyo(db: AsyncSession = Depends(get_db),
                   user: Usuario = Depends(get_current_active_user)):
    """¿Quien pregunta es profesional de apoyo, y de qué zona? Alimenta el filtro «Mis casos»
    del grid. Devuelve es_apoyo=False para todos los demás, que no ven ese filtro."""
    z = await _zona_apoyo_de(db, user)
    if not z:
        return {"es_apoyo": False, "zona": None, "nombre": None}
    return {"es_apoyo": True, "zona": z, "nombre": (await _apoyo_map(db)).get(z)}


@router.get("/justificacion/{reg_id}")
async def justificacion(reg_id: int, db: AsyncSession = Depends(get_db),
                        _: Usuario = Depends(get_current_active_user)):
    """Por qué cada celda tiene su valor: fuente STM (archivo+columna), regla automática o
    campo manual. Alimenta el popup de justificación del grid."""
    reg = (await db.execute(select(AnsRegistro).where(AnsRegistro.id == reg_id))).scalar_one_or_none()
    if not reg:
        raise HTTPException(status_code=404, detail="No encontrado")
    ftipo = "programados" if reg.tipo == "FT" else "st"
    f = (await db.execute(
        select(AnsFuente).where(AnsFuente.periodo == reg.periodo, AnsFuente.tipo == ftipo)
        .order_by(AnsFuente.id.desc())
    )).scalars().first()
    campos = _justificar(reg, f.nombre if f else None)

    # Nodos aprobados en el apartado de María Inés → procedencia real (mismo espíritu STM):
    # quién aprobó, contra qué archivo cruzó y por qué la propuesta decía lo que decía.
    caso = (await db.execute(select(AnsNodosCaso).where(AnsNodosCaso.registro_id == reg_id))).scalar_one_or_none()
    if caso and caso.estado in ("aprobado", "revisar") and caso.aprobado_por:
        fp = None
        if caso.fuente_id:
            fp = (await db.execute(select(AnsFuente).where(AnsFuente.id == caso.fuente_id))).scalar_one_or_none()
        origen = f" cruzando la Programación diaria «{fp.nombre}»" if fp else ""
        cuando = f" el {_bog(caso.aprobado_en).strftime('%d/%m/%Y')}" if caso.aprobado_en else ""
        base = f"Aprobado caso a caso por {caso.aprobado_por}{cuando} en el apartado Nodos{origen}."
        extra = f" Cruce: {caso.motivo}" if caso.motivo else ""
        for campo in ("nodos_planeados", "nodos_ejecutados"):
            campos[campo] = {"origen": "fuente", "texto": base + extra}
        if reg.obs_nodos:
            campos["obs_nodos"] = {"origen": "manual", "texto": f"Observación escrita por {caso.aprobado_por} al aprobar el caso en el apartado Nodos."}

    # Reglas automáticas de nodos: la celda pintada explica aquí su porqué (manda sobre lo demás)
    if reg.nodos_auto:
        try:
            m = json.loads(reg.nodos_auto)
        except (ValueError, TypeError):
            m = None
        if m and m.get("campo"):
            if m.get("estado") == "invalida":
                txt = ("⚠ CELDA ROJA — revisar: la condición que justificaba el ajuste automático ya no se "
                       f"cumple (p. ej. se quitó la exclusión o cambió el estado). El valor sigue siendo el "
                       f"que puso el sistema ({m.get('valor')}, antes {m.get('antes')}); confírmalo o corrígelo. "
                       f"Regla original: {m.get('detalle')}")
            else:
                txt = f"Celda amarilla — ajustada sola por el sistema. {m.get('detalle')}"
            campos[m["campo"]] = {"origen": "auto", "texto": txt}
    return {"reg_id": reg_id, "fuente": f.nombre if f else None, "campos": campos}


def _s(v):
    return None if v is None else str(v)


# Campos de fecha del ANS y su normalización a colombiano DD/MM/YYYY al guardar, para que no
# vuelva a colarse una fecha gringa (mes/día) como pasó con 8 Y de junio.
_CAMPOS_FECHA = {"fecha_inicio", "fecha_fin", "fecha_debio_validar", "fecha_envio_validar"}
_RE_FECHA = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _norm_fecha_col(v):
    """A DD/MM/YYYY. Deja N/A, vacío y lo que no sea una fecha d/d/AAAA intactos. Si el 2.º número
    es > 12 es un día → venía mes/día (gringo) → se intercambia; el resto se asume colombiano."""
    if not isinstance(v, str):
        return v
    m = _RE_FECHA.match(v.strip())
    if not m:
        return v
    a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
    dia, mes = (b, a) if (b > 12 and a <= 12) else (a, b)   # gringo mes/día → día/mes
    if dia > 31 or mes > 12:
        return v                                            # datos raros: no tocar
    return f"{dia:02d}/{mes:02d}/{y}"


@router.patch("/{reg_id}")
async def actualizar(reg_id: int, req: AnsRegistroUpdate, db: AsyncSession = Depends(get_db),
                     current_user: Usuario = Depends(get_current_active_user)):
    b = (await db.execute(select(AnsRegistro).where(AnsRegistro.id == reg_id))).scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="No encontrado")
    zona_aprendida = None
    datos_req = req.model_dump(exclude_unset=True)
    # Aplica y registra en el historial cada campo que realmente cambió
    for k, v in datos_req.items():
        if k in _CAMPOS_FECHA:
            v = _norm_fecha_col(v)                          # nunca guardar una fecha gringa
        if k == "zona" and isinstance(v, str):
            v = v.strip().upper()                           # GUAVIO / ALMEIDAS / UBATE / LV
        old = getattr(b, k)
        if _s(old) == _s(v):
            continue
        setattr(b, k, v)
        if k == "zona":
            zona_aprendida = v
        db.add(AnsCambio(
            registro_id=b.id, periodo=b.periodo, protocolo=b.protocolo, campo=k,
            valor_anterior=_s(old), valor_nuevo=_s(v), tipo="edicion",
            usuario_id=current_user.id, usuario_nombre=current_user.nombre,
        ))

    # Regla de Paola (la misma que aplica la consulta con IA): al decidir la exclusión del
    # 3.2 B en una orden ANULADA, «Se ejecutó» (AN) la acompaña — SI → N/A, NO → NO. Evita
    # filas inconsistentes (excluida con AN=NO) que enredaban el conteo de la tabla B.
    if "excluible_anulado" in datos_req and _es_anulado(b.estado_actual):
        ao = (b.excluible_anulado or "").strip().upper()
        an = "N/A" if ao == "SI" else ("NO" if ao == "NO" else None)
        if an and (b.se_ejecuto or "").strip().upper() != an:
            db.add(AnsCambio(registro_id=b.id, periodo=b.periodo, protocolo=b.protocolo,
                             campo="se_ejecuto", valor_anterior=_s(b.se_ejecuto), valor_nuevo=an,
                             tipo="edicion", usuario_id=None, usuario_nombre="Sistema (regla 3.2B)"))
            b.se_ejecuto = an

    # Reglas automáticas de nodos (3.2 C): se reevalúan en CADA edición — si cambió la
    # exclusión AO, el «se ejecutó» o los nodos, la máquina ajusta/valida su marca aquí mismo.
    _log_reglas_nodos(db, b, _eval_reglas_nodos(b))

    # Auto-aprende: si corriges la zona a mano y la fila tiene responsable (no ST/LV), guarda
    # «responsable → zona» en el directorio para que los PRÓXIMOS imports la resuelvan solos.
    aprendido = False
    tambien = 0
    resp = (b.nombre_responsable or "").strip()
    if zona_aprendida and zona_aprendida != "LV" and resp and b.tipo != "ST":
        n = _norm_nombre(resp)
        c = (await db.execute(select(Contacto).where(Contacto.nombre_norm == n))).scalar_one_or_none()
        if c:
            if (c.zona or "").strip().upper() != zona_aprendida:
                c.zona, c.activo = zona_aprendida, True
                aprendido = True
        else:
            db.add(Contacto(nombre=resp, nombre_norm=n, telefono="", rol="supervisor",
                            zona=zona_aprendida, creado_por=current_user.id,
                            notas="Zona aprendida al corregir el ANS"))
            aprendido = True

        # Y de una vez resuelve las OTRAS filas del mismo responsable que estén sin zona en este
        # entorno: corregir una arregla todas las de esa persona.
        hermanas = (await db.execute(select(AnsRegistro).where(
            AnsRegistro.entorno == current_entorno(), AnsRegistro.tipo == "FT",
            AnsRegistro.id != b.id,
            func.upper(func.trim(AnsRegistro.nombre_responsable)) == resp.upper(),
            func.coalesce(AnsRegistro.zona, "") == ""))).scalars().all()
        for h in hermanas:
            h.zona = zona_aprendida
            db.add(AnsCambio(registro_id=h.id, periodo=h.periodo, protocolo=h.protocolo, campo="zona",
                             valor_anterior="", valor_nuevo=zona_aprendida, tipo="edicion",
                             usuario_id=current_user.id, usuario_nombre=current_user.nombre))
            tambien += 1

    await db.commit()
    await db.refresh(b)
    d = _to_dict(b)
    if aprendido or tambien:
        d["_aprendido_zona"] = {"responsable": resp, "zona": zona_aprendida, "tambien": tambien}
    return d


@router.get("/responsables-sin-zona")
async def responsables_sin_zona(periodo: Optional[str] = None, db: AsyncSession = Depends(get_db),
                                _: Usuario = Depends(get_current_active_user)):
    """Responsables (de órdenes FT) que aún no tienen zona: hay que agregarlos al directorio o
    corregir el dato. Es el aviso de lo que le falta al mapa supervisor→zona."""
    q = select(AnsRegistro.nombre_responsable, func.count().label("n")).where(
        AnsRegistro.tipo == "FT",
        func.coalesce(func.trim(AnsRegistro.nombre_responsable), "") != "",
        func.coalesce(AnsRegistro.zona, "") == "")
    if periodo:
        q = q.where(AnsRegistro.periodo == periodo)
    q = q.group_by(AnsRegistro.nombre_responsable).order_by(func.count().desc())
    filas = (await db.execute(q)).all()
    return {"total_filas": sum(n for _, n in filas),
            "responsables": [{"nombre": r, "filas": n} for r, n in filas]}


@router.get("/cambios")
async def listar_cambios(
    periodo: Optional[str] = None, registro_id: Optional[int] = None,
    limit: int = Query(500, le=2000), db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_active_user),
):
    q = select(AnsCambio)
    if periodo:
        q = q.where(AnsCambio.periodo == periodo)
    if registro_id:
        q = q.where(AnsCambio.registro_id == registro_id)
    rows = (await db.execute(q.order_by(AnsCambio.id.desc()).limit(limit))).scalars().all()
    return [{
        "id": c.id, "registro_id": c.registro_id, "protocolo": c.protocolo, "campo": c.campo,
        "valor_anterior": c.valor_anterior, "valor_nuevo": c.valor_nuevo, "tipo": c.tipo,
        "usuario": c.usuario_nombre, "creado_en": _bog(c.creado_en).isoformat() if c.creado_en else None,
    } for c in rows]


# Campos manuales que son enteros (para revertir con el tipo correcto)
_CAMPOS_INT = {"nodos_planeados", "nodos_ejecutados"}


@router.post("/cambios/{cambio_id}/revertir")
async def revertir_cambio(cambio_id: int, db: AsyncSession = Depends(get_db),
                          current_user: Usuario = Depends(get_current_active_user)):
    """Devuelve un campo a su valor anterior (como deshacer en Excel) y lo registra."""
    c = (await db.execute(select(AnsCambio).where(AnsCambio.id == cambio_id))).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Cambio no encontrado")
    reg = (await db.execute(select(AnsRegistro).where(AnsRegistro.id == c.registro_id))).scalar_one_or_none()
    if not reg:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    actual = getattr(reg, c.campo)
    destino = c.valor_anterior
    if c.campo in _CAMPOS_INT:
        destino = int(destino) if destino not in (None, "") else None
    setattr(reg, c.campo, destino)
    db.add(AnsCambio(
        registro_id=reg.id, periodo=reg.periodo, protocolo=reg.protocolo, campo=c.campo,
        valor_anterior=_s(actual), valor_nuevo=_s(destino), tipo="reversion",
        usuario_id=current_user.id, usuario_nombre=current_user.nombre,
    ))
    await db.commit()
    await db.refresh(reg)
    return _to_dict(reg)


@router.delete("/periodo/{periodo}")
async def borrar_periodo(periodo: str, db: AsyncSession = Depends(get_db),
                         _: Usuario = Depends(get_current_active_user)):
    # Los DELETE de Core NO pasan por el auto-filtro de entorno → filtrar explícito.
    ent = current_entorno()
    await db.execute(delete(AnsRegistro).where(AnsRegistro.periodo == periodo, AnsRegistro.entorno == ent))
    await db.execute(delete(AnsPeriodo).where(AnsPeriodo.periodo == periodo, AnsPeriodo.entorno == ent))
    await db.commit()
    return {"ok": True, "periodo": periodo}


@router.delete("/reset")
async def reset_prueba(db: AsyncSession = Depends(get_db),
                       _: Usuario = Depends(get_current_active_user)):
    """Borra TODOS los datos del entorno de prueba (registros, periodos, fuentes,
    cambios, snapshots, impacto36). Solo opera en 'prueba' — nunca toca los reales."""
    ent = current_entorno()
    if ent != "prueba":
        raise HTTPException(status_code=403, detail="El reset total solo está disponible en el entorno de prueba")
    n = 0
    for M in _MODELOS_ENTORNO:
        r = await db.execute(delete(M).where(M.entorno == ent))
        n += r.rowcount or 0
    # También limpia los archivos de procedencia en disco del entorno de prueba
    carpeta = os.path.join(FUENTES_DIR, "prueba")
    if os.path.isdir(carpeta):
        shutil.rmtree(carpeta, ignore_errors=True)
    await db.commit()
    return {"ok": True, "entorno": ent, "filas_borradas": n}


@router.get("/periodos")
async def periodos(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    rows = (await db.execute(
        select(AnsRegistro.periodo, func.count()).group_by(AnsRegistro.periodo).order_by(AnsRegistro.periodo.desc())
    )).all()
    return [{"periodo": p, "registros": n} for p, n in rows]


# ── 3.6 Mayor Impacto ($) — el sistema es dueño de estas cifras por periodo ───────

@router.get("/impacto36")
async def get_impacto36(periodo: str, db: AsyncSession = Depends(get_db),
                        _: Usuario = Depends(get_current_active_user)):
    """Cifras $ de la hoja 3.6 guardadas para el periodo (vacío si no hay)."""
    imp = (await db.execute(select(AnsImpacto36).where(AnsImpacto36.periodo == periodo))).scalar_one_or_none()
    return {"periodo": periodo, "datos": json.loads(imp.datos) if imp and imp.datos else {},
            "actualizado_en": _bog(imp.actualizado_en).isoformat() if imp and imp.actualizado_en else None}


@router.put("/impacto36")
async def put_impacto36(periodo: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                        datos: dict = Body(...), db: AsyncSession = Depends(get_db),
                        _: Usuario = Depends(get_current_active_user)):
    """Guarda/actualiza las cifras $ de 3.6 del periodo. datos = {"29":{fila:{zona:val}}, "36":{...}}."""
    payload = json.dumps(datos, ensure_ascii=False, default=str)
    imp = (await db.execute(select(AnsImpacto36).where(AnsImpacto36.periodo == periodo))).scalar_one_or_none()
    if imp:
        imp.datos = payload
    else:
        db.add(AnsImpacto36(periodo=periodo, datos=payload))
    await db.commit()
    return {"periodo": periodo, "ok": True}


# ── Fuentes (procedencia de los datos STM — descargables) ────────────────────────

@router.get("/duplicados")
async def duplicados(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    """Códigos (protocolos) que aparecen en MÁS de un periodo (riesgo de doble conteo)."""
    rows = (await db.execute(
        select(AnsRegistro.protocolo, AnsRegistro.periodo, AnsRegistro.id)
        .order_by(AnsRegistro.protocolo, AnsRegistro.periodo)
    )).all()
    por_prot = defaultdict(list)
    for prot, per, rid in rows:
        por_prot[prot].append({"periodo": per, "id": rid})
    dups = [{"protocolo": p, "periodos": v} for p, v in por_prot.items()
            if len({x["periodo"] for x in v}) > 1]
    return {"total": len(dups), "items": dups}


@router.get("/fuentes")
async def listar_fuentes(periodo: Optional[str] = None, db: AsyncSession = Depends(get_db),
                         _: Usuario = Depends(get_current_active_user)):
    q = select(AnsFuente)
    if periodo:
        q = q.where(AnsFuente.periodo == periodo)
    # Lo último subido primero: la pestaña es la bitácora de subidas del mes
    rows = (await db.execute(q.order_by(AnsFuente.periodo.desc(), AnsFuente.creado_en.desc(),
                                        AnsFuente.id.desc()))).scalars().all()
    return [{"id": f.id, "periodo": f.periodo, "tipo": f.tipo, "nombre": f.nombre,
             "tamano": f.tamano, "registros": f.registros,
             "creado_en": _bog(f.creado_en).isoformat() if f.creado_en else None} for f in rows]


# Carpetas del ZIP de procedencia, en el orden del flujo de trabajo
_FUENTE_CARPETA = {
    "programados":   "01. CD10 Programados (FT)",
    "st":            "02. CD10 Linea Viva (ST)",
    "prog20":        "03. Programacion diaria 20min",
    "bolsas":        "04. Bolsas ENEL",
    "no_programado": "05. No programados",
}


@router.get("/fuentes/exportar")
async def fuentes_exportar(periodo: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                           db: AsyncSession = Depends(get_db),
                           _: Usuario = Depends(get_current_active_user)):
    """ZIP con TODOS los archivos fuente del periodo — el respaldo de procedencia completo,
    encarpetado por tipo y con la fecha de subida en el nombre de cada archivo (mismo
    espíritu que el export de evidencias). NOTA: debe declararse ANTES de /fuentes/{id}."""
    rows = (await db.execute(
        select(AnsFuente).where(AnsFuente.periodo == periodo)
        .order_by(AnsFuente.tipo, AnsFuente.creado_en, AnsFuente.id))).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"Sin archivos fuente para {periodo}")

    raiz = f"Procedencia_ANS_{_mes_es(periodo)}"
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            usados, faltan, lineas = set(), [], []
            for f in rows:
                carpeta = _FUENTE_CARPETA.get(f.tipo, f"06. Otros ({_safe_fs(f.tipo)})")
                fecha = _bog(f.creado_en).strftime("%Y-%m-%d %H%M") if f.creado_en else "sin-fecha"
                destino = f"{raiz}/{carpeta}/{fecha} - {_safe_fs(f.nombre)}"
                if destino in usados:      # mismo nombre a la misma hora: se distingue por id
                    destino = f"{raiz}/{carpeta}/{fecha} - id{f.id} - {_safe_fs(f.nombre)}"
                usados.add(destino)
                lineas.append(f"  [{f.tipo}] {fecha} · {f.nombre}"
                              + (f" · {f.registros} filas STM" if f.registros else ""))
                ruta_abs = os.path.join(FUENTES_DIR, f.ruta)
                if not os.path.exists(ruta_abs):
                    faltan.append(f.nombre)
                    continue
                z.write(ruta_abs, destino)
            z.writestr(f"{raiz}/LEEME.txt",
                       f"Procedencia ANS de {_mes_es(periodo).replace('_', ' ')}\n"
                       f"Archivos fuente ORIGINALES (tal cual se subieron al sistema) que originaron\n"
                       f"los datos del mes, encarpetados por tipo y con su fecha de subida en el nombre.\n\n"
                       f"Inventario ({len(rows)} archivos):\n" + "\n".join(lineas) + "\n"
                       + (("\nNO DISPONIBLES en disco (solo queda el registro): "
                           + ", ".join(faltan) + "\n") if faltan else ""))
        tmp.close()
    except Exception:
        tmp.close()
        os.path.exists(tmp.name) and os.remove(tmp.name)
        raise

    def _iter():
        with open(tmp.name, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                yield chunk
        os.path.exists(tmp.name) and os.remove(tmp.name)

    return StreamingResponse(_iter(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{raiz}.zip"'})


@router.post("/bolsas-snapshot")
async def subir_bolsa_snapshot(
    file: UploadFile = File(...),
    periodo: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_active_user),
):
    """Sube el BOLSAS ENEL del mes; parsea sus hojas 3.1/3.5 y guarda el snapshot
    del periodo (para que 3.1/3.5 cuadren por mes). También lo respalda como fuente."""
    contents = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo: {e}")
    hoy = _fecha_corte(file.filename, periodo)
    datos = parse_bolsa_snapshot(wb, hoy)
    snap = (await db.execute(select(AnsBolsaSnapshot).where(AnsBolsaSnapshot.periodo == periodo))).scalar_one_or_none()
    payload = json.dumps(datos, ensure_ascii=False)
    if snap:
        snap.datos = payload
        snap.archivo = file.filename
    else:
        db.add(AnsBolsaSnapshot(periodo=periodo, archivo=file.filename, datos=payload))
    await _guardar_fuente(db, periodo, "bolsas", file.filename, contents, 0)
    await db.commit()
    # conteo rápido para feedback
    tot31 = sum(d["GLOBAL"] for c in datos["3.1"].values() for d in c.values())
    return {"ok": True, "periodo": periodo, "archivo": file.filename, "corte": hoy.isoformat(),
            "bolsas_3.1_global": tot31}


@router.post("/fuente")
async def subir_fuente(
    file: UploadFile = File(...),
    periodo: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    tipo: str = Query("no_programado"),
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_active_user),
):
    """Sube un archivo de respaldo (p.ej. indicadores 'No programados') solo para
    procedencia/descarga, sin parsearlo a la hoja data."""
    contents = await file.read()
    await _guardar_fuente(db, periodo, tipo, file.filename, contents, 0)
    await db.commit()
    return {"ok": True, "periodo": periodo, "tipo": tipo, "nombre": file.filename}


@router.get("/fuentes/{fuente_id}")
async def descargar_fuente(fuente_id: int, db: AsyncSession = Depends(get_db),
                           _: Usuario = Depends(get_current_active_user)):
    f = (await db.execute(select(AnsFuente).where(AnsFuente.id == fuente_id))).scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="Fuente no encontrada")
    ruta_abs = os.path.join(FUENTES_DIR, f.ruta)
    if not os.path.exists(ruta_abs):
        raise HTTPException(status_code=404, detail="Archivo no disponible en disco")
    with open(ruta_abs, "rb") as fh:
        data = fh.read()
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{f.nombre}"'},
    )


# ── Aprobadores (catálogo) ───────────────────────────────────────────────────────

@router.get("/aprobadores")
async def listar_aprobadores(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    rows = (await db.execute(select(AnsAprobador).order_by(AnsAprobador.nombre))).scalars().all()
    if not rows:
        return {"items": [{"id": None, "nombre": n, "codigo": None, "activo": True} for n in APROBADORES_SEED],
                "fuente": "seed"}
    return {"items": [{"id": a.id, "nombre": a.nombre, "codigo": a.codigo, "activo": a.activo} for a in rows],
            "fuente": "db"}


@router.post("/aprobadores")
async def crear_aprobador(req: AnsAprobadorCreate, db: AsyncSession = Depends(get_db),
                          _: Usuario = Depends(get_current_active_user)):
    a = AnsAprobador(**req.model_dump())
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return {"id": a.id, "nombre": a.nombre, "codigo": a.codigo, "activo": a.activo}


@router.post("/aprobadores/seed")
async def sembrar_aprobadores(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    """Carga la semilla ENEL en la tabla si está vacía."""
    n = (await db.execute(select(func.count()).select_from(AnsAprobador))).scalar() or 0
    if n:
        return {"ok": True, "ya_existian": n}
    for nombre in APROBADORES_SEED:
        m = re.search(r"\((CO\w+)\)", nombre)
        db.add(AnsAprobador(nombre=nombre, codigo=m.group(1) if m else None, activo=True))
    await db.commit()
    return {"ok": True, "insertados": len(APROBADORES_SEED)}


@router.delete("/aprobadores/{ap_id}")
async def borrar_aprobador(ap_id: int, db: AsyncSession = Depends(get_db),
                           _: Usuario = Depends(get_current_active_user)):
    a = (await db.execute(select(AnsAprobador).where(AnsAprobador.id == ap_id))).scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="No encontrado")
    await db.delete(a)
    await db.commit()
    return {"ok": True}


# ── Indicadores ──────────────────────────────────────────────────────────────────

def _tabla_zonas():
    return {z: {"cumple": 0, "no_cumple": 0, "exclusiones": 0, "na": 0} for z in ZONAS_REPORTE}


def _zona_reg(reg) -> str:
    z = (getattr(reg, "zona", None) or "").upper()
    if reg.tipo == "ST" or z == "LV":
        return "LV"
    return z if z in ZONAS else "UBATE"  # sin zona → UBATE por defecto (editable)


async def _indicadores_programacion(db: AsyncSession, periodo: str) -> dict:
    """3.2 (A oportunidad ejec, B efectividad apertura, C alcance nodos), 3.3, 3.4 y Dinamica."""
    regs = (await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo))).scalars().all()

    A, B, C = _tabla_zonas(), _tabla_zonas(), _tabla_zonas()   # 3.2
    t34, t33 = _tabla_zonas(), _tabla_zonas()                  # oportunidad prog / calidad info
    dinamica = defaultdict(lambda: {"CUMPLE": 0, "N/A": 0, "NO CUMPLE": 0})  # (zona, supervisor)
    prog33 = defaultdict(lambda: {"CUMPLE": 0, "NO CUMPLE": 0, "N/A": 0})
    prog34 = defaultdict(lambda: {"CUMPLE": 0, "NO CUMPLE": 0, "N/A": 0})
    # Pivote «NO CUMPLE TIEMPOS» del Excel de Paola: zona → supervisor → líder (RI),
    # solo órdenes que fallaron los tiempos, separando excluibles (el filtro del pivot).
    no_cumple_piv = defaultdict(lambda: {"si": 0, "no": 0})    # (zona, supervisor, líder)
    # Dinámicas de incumplimiento por caso (pantallazo para correos): cada orden que NO
    # cumple A/B/C con su equipo (supervisor + líder RI), motivo legible y su exclusión.
    nc_casos = {"A": [], "B": [], "C": []}

    # Cada caso lee su propia observación (la que escribe el encargado de ANS en el grid):
    # A → AB (obs), B → AP (obs_anulado, la de la anulación), C → obs de nodos.
    _obs_de_excl = {"excluible": "obs", "excluible_anulado": "obs_anulado", "nods_excluible": "obs_nodos"}

    def _item_nc(r, z, excl_campo, motivo, corto):
        obs_campo = _obs_de_excl.get(excl_campo)
        observacion = re.sub(r"\s+", " ", str(getattr(r, obs_campo, "") or "")).strip() if obs_campo else ""
        return {
            "zona": z,
            "supervisor": r.nombre_responsable or "—",
            "lider": re.sub(r"\s+", " ", str(r.ri_jt or "")).strip() or "—",
            "excluible": "SI" if (getattr(r, excl_campo, "") or "").upper() == "SI" else "NO",
            "protocolo": r.protocolo, "pdl": (r.protocolo or "").split("/")[0].strip(),
            "fecha": r.fecha_inicio or "", "motivo": motivo, "motivo_corto": corto,
            "observacion": observacion,
        }

    no_ejecutado = 0

    for r in regs:
        c = computar(r)
        z = _zona_reg(r)
        excl = (r.excluible or "").upper() == "SI"
        # B: efectividad (3.2 B — ejecución): se mira «Se ejecutó» (AN). SI = cumple;
        # anulada con exclusión aprobada (AO=SI) = exclusión; NO = no cumple. OJO: antes
        # se clasificaba por la maniobra de apertura (eso es del 3.2 A) y las anuladas
        # —que no tienen maniobras— no caían en ningún balde: el «no cumple» daba 0.
        se_ej = (r.se_ejecuto or "").strip().upper()
        if se_ej == "NO":
            no_ejecutado += 1
        if (r.excluible_anulado or "").strip().upper() == "SI":
            B[z]["exclusiones"] += 1
        elif se_ej == "SI":
            B[z]["cumple"] += 1
        elif se_ej == "NO":
            B[z]["no_cumple"] += 1
        else:
            B[z]["na"] += 1
        # Dinámica B: las que NO se ejecutaron (también las excluidas — el filtro separa)
        if se_ej == "NO":
            nc_casos["B"].append(_item_nc(r, z, "excluible_anulado",
                f"No se ejecutó ({(r.estado_actual or 'anulada').strip()})",
                "no ejecutada"))
        # A: oportunidad en la ejecución (cumple tiempos)
        if c["cumple_tiempos"] == "CUMPLE":
            A[z]["cumple"] += 1
        elif excl:
            A[z]["exclusiones"] += 1
        elif c["cumple_tiempos"] == "NO CUMPLE":
            A[z]["no_cumple"] += 1
        else:
            A[z]["na"] += 1
        # Dinámica A: falla cruda de tiempos (apertura o cierre tarde), incluidas excluidas
        if not c["apoyo_pdl"] and "NO CUMPLE" in (c["cumple_apertura"], c["cumple_cierre"]):
            partes, cortas = [], []
            if c["cumple_apertura"] == "NO CUMPLE":
                partes.append(f"apertura +{c['desfase_apertura']} min")
                cortas.append(f"apertura +{c['desfase_apertura']}′")
            if c["cumple_cierre"] == "NO CUMPLE":
                partes.append(f"cierre +{c['desfase_cierre']} min")
                cortas.append(f"cierre +{c['desfase_cierre']}′")
            nc_casos["A"].append(_item_nc(r, z, "excluible",
                "Fuera de tiempos: " + " y ".join(partes), " · ".join(cortas)))
        # C: alcance de nodos
        C[z]["cumple"] += (r.nodos_ejecutados or 0)
        C[z]["no_cumple"] += max((r.nodos_planeados or 0) - (r.nodos_ejecutados or 0), 0)
        if (r.nods_excluible or "").upper() == "SI":
            C[z]["exclusiones"] += 1
        # Dinámica C: órdenes que dejaron nodos sin ejecutar
        _np, _ne = (r.nodos_planeados or 0), (r.nodos_ejecutados or 0)
        if _np > _ne:
            nc_casos["C"].append(_item_nc(r, z, "nods_excluible",
                f"Ejecutó {_ne} de {_np} nodos (faltaron {_np - _ne})",
                f"{_ne}/{_np} nodos (faltan {_np - _ne})"))
        # 3.4 oportunidad en la programación (exclusiones = AA «Excluible tiempos» en SI,
        # como la fila Exclusiones de la hoja 3.4 del Excel de Paola)
        t34[z][{"CUMPLE": "cumple", "NO CUMPLE": "no_cumple"}.get(c["cumple_tprog"], "na")] += 1
        if (r.excluible_tiempos or "").upper() == "SI":
            t34[z]["exclusiones"] += 1
        prog34[r.sigla_elaborador or "—"][c["cumple_tprog"] if c["cumple_tprog"] in ("CUMPLE", "NO CUMPLE") else "N/A"] += 1
        # 3.3 calidad en la información (UUCC); exclusiones = AS «Excluible2» en SI
        t33[z][{"CUMPLE": "cumple", "NO CUMPLE": "no_cumple"}.get(c["cumple_uucc"], "na")] += 1
        if (r.excluible2 or "").upper() == "SI":
            t33[z]["exclusiones"] += 1
        # Por ahora agrupa por SUPERVISOR (pedido 2026-07-24): la sigla del elaborador
        # traía nombres que no correspondían en el 3.3.
        prog33[r.nombre_responsable or "—"][c["cumple_uucc"] if c["cumple_uucc"] in ("CUMPLE", "NO CUMPLE") else "N/A"] += 1
        # Dinamica
        dinamica[(z, (r.nombre_responsable or "—"))][c["cumple_tiempos"] if c["cumple_tiempos"] in ("CUMPLE", "NO CUMPLE") else "N/A"] += 1
        # Pivote «NO CUMPLE TIEMPOS»: falla cruda de tiempos (apertura o cierre tarde),
        # incluidas las que luego se excluyen — como el pivot de Paola con Excluible=(Todas).
        if not c["apoyo_pdl"] and "NO CUMPLE" in (c["cumple_apertura"], c["cumple_cierre"]):
            lider = re.sub(r"\s+", " ", str(r.ri_jt or "")).strip() or "—"
            no_cumple_piv[(z, r.nombre_responsable or "—", lider)]["si" if excl else "no"] += 1

    def ratio(tabla):
        cum = sum(t["cumple"] for t in tabla.values())
        noc = sum(t["no_cumple"] for t in tabla.values())
        return cum / (cum + noc) if (cum + noc) else 0.0

    indA = ratio(A)
    # B sobre "se abrió": cumple / (cumple+no_cumple)
    indB = ratio(B)
    cnod = sum(t["cumple"] for t in C.values())
    pnod = cnod + sum(t["no_cumple"] for t in C.values())
    indC = cnod / pnod if pnod else 0.0
    ans_32 = 0.25 * indA + 0.25 * indB + 0.50 * indC

    return {
        "3.2": {"A": A, "B": B, "C": C, "indA": indA, "indB": indB, "indC": indC, "ANS": ans_32,
                "descuento": DESCUENTO_32},
        "3.3": {"por_zona": t33, "por_programador": prog33, "ANS": ratio(t33)},
        "3.4": {"por_zona": t34, "por_programador": prog34, "ANS": ratio(t34)},
        "Dinamica": [{"zona": k[0], "supervisor": k[1], **v} for k, v in sorted(dinamica.items())],
        "NO_CUMPLE_TIEMPOS": [{"zona": k[0], "supervisor": k[1], "lider": k[2], **v}
                              for k, v in sorted(no_cumple_piv.items())],
        # Órdenes que NO cumplen, por caso, con equipo y motivo (dinámicas para correos)
        "NO_CUMPLEN": {k: sorted(v, key=lambda x: (x["zona"], x["supervisor"], x["lider"], x["pdl"]))
                       for k, v in nc_casos.items()},
        "NO_EJECUTADO": no_ejecutado,
        "total_registros": len(regs),
    }


def _norm_zona(zona) -> Optional[str]:
    """Normaliza la zona de una bolsa a UBATE / ALMEIDAS / GUAVIO / LV (las bolsas
    traen variantes: 'UBATÉ', 'ALEMEIDAS', 'LINEA VIVA', null…)."""
    z = str(zona or "").strip().upper()
    for a, b in (("Á", "A"), ("É", "E"), ("Í", "I"), ("Ó", "O"), ("Ú", "U")):
        z = z.replace(a, b)
    z = z.replace("ALEMEIDAS", "ALMEIDAS")
    if not z:
        return None
    if "LINEA VIVA" in z or "LINE VIVA" in z or z in ("LV", "LV&TLC"):
        return "LV"
    if z.startswith("UBATE"):
        return "UBATE"
    if z.startswith("ALMEIDAS") or "CHOCONTA" in z:
        return "ALMEIDAS"
    if z.startswith("GUAVIO"):
        return "GUAVIO"
    return z if z in ZONAS_REPORTE else None


def _t31_vacio():
    return {c: {z: {"GLOBAL": 0, "PLANEADO": 0, "PENDIENTE BOLSA": 0, "ENTREGADO A ENEL": 0} for z in ZONAS}
            for c in CONCEPTOS_31}


def _t35_vacio():
    return {p: {z: {"PLANEADO": 0, "EJECUTADO": 0} for z in ZONAS_REPORTE} for p in PROCESOS_35}


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# Hoja de detalle del BOLSAS ENEL → tipo canónico (para contar casos por estado).
BOLSA_SHEETS = {
    "IPPE": "IPPE", "CUN100": "CUN100", "PQR": "PQR", "CASOS BOCATOMA": "BOCATOMA",
    "AMP": "AMP", "TLC": "TLC", "TUTELA": "TUTELA", "BP": "BP", "NNDD": "NNDD",
    "CUNBT": "CUNBT", "DRONE": "DRONE",
}
# Estados de los casos de bolsa → bucket de las tablas 3.1/3.5.
_EST_ENTREGADO = {"ENVIADO A ENEL", "EJECUTADO"}
_EST_PLANEADO = {"PROGRAMADO"}
_EST_PENDIENTE = {"PTE VISITA", "PTE PROGRAMAR", "PTE PROG", "COBRAR VISITA", "PENDIENTE", "PTE"}
_EST_EJECUTADO = {"EJECUTADO"}

# Lógica por FECHA (explicación de Paola, hilo #19): el bucket NO sale del texto del
# estado sino de comparar una fecha contra "hoy" (la fecha del corte del BOLSAS ENEL).
#   3.1 (envío del presupuesto): sin fecha=PENDIENTE, futura=PLANEADO, ≤hoy=ENTREGADO A ENEL.
#   3.5 (ejecución): ≤hoy=EJECUTADO (ya pasó → se ejecutó), futura=PLANEADO.
# Columna de fecha por hoja, según las que Paola pintó (AMARILLO=3.1, VERDE=3.5) en el
# BOLSAS ENEL de ejemplo. (col_3.1, col_3.5); None = esa hoja NO alimenta ese indicador.
BOLSA_FECHAS = {
    "IPPE":           ("FECHA ENVIO PRESUPUESTO", "FECHA DE PROG"),
    "CUN100":         ("FECHA ENVIO PRESUPUESTO", "FECHA DE PROG"),
    "PQR":            ("FECHA ENTREGA PPTO",       "FECHA EJECUCIÓN"),
    "CASOS BOCATOMA": ("FECHA ENVIADO A ENEL",     "FECHA DE PROGRAMACION"),
    "AMP":            ("FECHA PTTO",               "FECHA DE PROGRAMACION"),
    "TLC":            (None,                        "FECHA EJECUCION"),
    "TUTELA":         (None,                        "FECHA DE PROG"),
    "BP":             ("FECHA ENVIO ENEL",          "FECHA PRGRAMACIÓN"),
    "NNDD":           (None, None),
    "CUNBT":          (None, None),
    "DRONE":          (None, None),
}
# En 3.1 la 3ª zona es "GUAVIO-BOCA": los casos de CASOS BOCATOMA (que no traen ZONA)
# se suman a GUAVIO. (En 3.5 BOCATOMA va a LV, su comportamiento normal.)
BOLSA_ZONA_31_DEFAULT = {"CASOS BOCATOMA": "GUAVIO"}

# Mismos mapeos pero por TIPO de bolsa (Bolsa.tipo), para calcular 3.1/3.5 directo desde
# el módulo Bolsas ya cargado (no requiere re-subir el archivo).
BOLSA_FECHAS_TIPO = {BOLSA_SHEETS[sh]: cols for sh, cols in BOLSA_FECHAS.items() if sh in BOLSA_SHEETS}
BOLSA_ZONA_31_DEF_TIPO = {BOLSA_SHEETS[sh]: z for sh, z in BOLSA_ZONA_31_DEFAULT.items() if sh in BOLSA_SHEETS}

# DEPRECADO (2026-07-01): antes 3.1 decidía por ESTADO para PQR/BOCATOMA. Paola confirmó con sus
# IDs de junio que el bucket sale SOLO de la fecha (amarilla) vacía/llena, sin mirar el estado.
# Se conservan por si algún reporte legado los referencia; _bolsas_3x_modulo ya NO los usa.
REGLA_31 = {"IPPE": "fecha", "CUN100": "fecha", "BP": "fecha", "PQR": "fecha", "BOCATOMA": "fecha"}
ENTREGADO_ESTADOS = {"ENVIADO A ENEL", "EJECUTADO"}


def _fin_de_mes(periodo: str) -> date:
    import calendar
    y, mo = int(periodo[:4]), int(periodo[5:7])
    return date(y, mo, calendar.monthrange(y, mo)[1])


def _fecha_extra(extra: dict, col):
    """Fecha de una columna (por nombre, substring) dentro del datos_extra de la bolsa."""
    if not col:
        return None
    up = col.upper()
    for k, v in (extra or {}).items():
        if up in str(k).upper() and v not in (None, ""):
            d = _fecha_celda(v)
            if d:
                return d
            try:
                return date.fromisoformat(str(v)[:10])
            except Exception:
                return None
    return None


def _valor_extra(extra: dict, col):
    """Valor crudo (str recortado) de una columna del datos_extra, o None si no está / está vacío.
    Distingue la celda VACÍA de la que tiene texto/nota que no es fecha (ej. 'NA', 'PENDIENTE
    ENTREGA DE PTTO...') → sirve para el bucketing (vacío = pendiente) y para el reporte de fechas
    mal escritas (presente pero no parseable)."""
    if not col:
        return None
    up = col.upper()
    for k, v in (extra or {}).items():
        if up in str(k).upper() and v not in (None, "") and str(v).strip() != "":
            return str(v).strip()
    return None


def _tiene_valor_extra(extra: dict, col) -> bool:
    """True si la columna existe en datos_extra con algún valor NO vacío (sea o no una fecha)."""
    return _valor_extra(extra, col) is not None


async def _bolsas_3x_modulo(db: AsyncSession, periodo: Optional[str]) -> dict:
    """3.1 / 3.5 calculados desde el módulo Bolsas (Bolsa) por FECHA vs 'hoy'. 3.1: col por tipo
    del datos_extra; 3.5: idem (o fecha_prog del núcleo). No requiere re-subir el BOLSAS ENEL:
    usa los casos ya cargados.
    'hoy' = fin de mes del periodo, PERO nunca más adelante que el día real de hoy (bug 2026-07-22,
    Sergio): en el mes EN CURSO, usar directamente fin de mes como corte hacía que una fecha de
    mañana (ej. 2026-07-22 visto el 21) se contara como "ya ocurrió" y cayera en ENTREGADO A ENEL
    en vez de PLANEADO. Para un mes YA CERRADO (el real de hoy es posterior a su fin de mes) el
    min() no cambia nada — sigue siendo fin de mes, igual que la validación de junio."""
    hoy = min(_fin_de_mes(periodo), date.today()) if periodo else date.today()
    mes_ini = date(int(periodo[:4]), int(periodo[5:7]), 1) if periodo else hoy.replace(day=1)
    t31, t35 = _t31_vacio(), _t35_vacio()
    casos = []
    anomalias = []   # celdas de fecha (3.1/3.5) con un valor que NO es fecha válida → a corregir
    rows = (await db.execute(select(Bolsa))).scalars().all()
    for b in rows:
        # NO se excluye ANULADO: Paola cuenta según el campo de fecha (vacío/lleno) sin importar
        # el estado ni las notas de los supervisores (regla confirmada con sus IDs de junio 2026).
        tipo = (b.tipo or "").strip().upper()
        concepto = BOLSA_CONCEPTO_31.get(tipo)
        proceso = BOLSA_PROCESO_35.get(tipo)
        col31, col35 = BOLSA_FECHAS_TIPO.get(tipo, (None, None))
        extra = {}
        if b.datos_extra:
            try:
                extra = json.loads(b.datos_extra)
            except Exception:
                extra = {}
        z = _norm_zona(b.zona) if b.zona else None
        if z not in ZONAS:   # fallback: zona en datos_extra (ej. CUN100 usa "SUB ZONA 2")
            for k, v in extra.items():
                if "ZONA" in str(k).upper() and v:
                    zc = _norm_zona(v)
                    if zc in ZONAS:
                        z = zc
                        break
        z31 = z if z in ZONAS else BOLSA_ZONA_31_DEF_TIPO.get(tipo)   # 3.1: BOCATOMA → GUAVIO
        zz = z if z in ZONAS else (z31 or "LV")                       # 3.5: BOCATOMA→GUAVIO, resto→LV
        # 3.1: el bucket sale SOLO de la fecha de la columna que Paola pinta de AMARILLO
        # (FECHA ENTREGA PPTO / ENVIO PRESUPUESTO / ENVIADO A ENEL). El ESTADO y las notas de los
        # supervisores NO influyen — regla verificada contra los IDs de junio 2026 de Paola
        # (PQR ALM=60/GUA=86, BOCATOMA=27, CUN100 ALM=30/GUA=115, IPPE ALM=14/GUA=3):
        #   sin fecha            → PENDIENTE BOLSA
        #   fecha del mes ya pasó → ENTREGADO A ENEL
        #   fecha futura          → PLANEADO
        #   fecha de un mes previo → ya se reportó ese mes (no cuenta).
        f31 = _fecha_extra(extra, col31)
        if f31 is None:
            # celda vacía → PENDIENTE; celda con texto que no es fecha (nota/'NA') → dato errado,
            # no cuenta (Paola sólo cuenta los campos verdaderamente vacíos como pendientes).
            b31 = None if _tiene_valor_extra(extra, col31) else "PENDIENTE BOLSA"
        elif f31 < mes_ini:
            b31 = None
        elif f31 <= hoy:
            b31 = "ENTREGADO A ENEL"
        else:
            b31 = "PLANEADO"
        # 3.5 ejecución, también relativo al mes: ejecutado dentro del mes (≤hoy); fechas de
        # meses anteriores ya se reportaron → no cuentan; futuras = planeado.
        f35_extra = _fecha_extra(extra, col35)
        f35 = f35_extra or b.fecha_prog
        # Paola: en 3.5 SOLO se alimenta lo EJECUTADO (ejecutado dentro del mes). Lo demás vacío.
        b35 = "EJECUTADO" if (f35 is not None and mes_ini <= f35 <= hoy) else None
        # Reporte "datos por revisar": la celda de fecha tiene algo escrito pero NO es una fecha
        # válida (rango "11/04-03/06", typo "02/07/206", nota, "NA"...) → el sistema no la puede
        # ubicar. Queda en evidencia para que un encargado la corrija en la bolsa.
        for grp, col, parsed in (("3.1", col31, f31), ("3.5", col35, f35_extra)):
            raw = _valor_extra(extra, col)
            if col and raw is not None and parsed is None:
                anomalias.append({
                    "grupo": grp, "tipo": tipo,
                    "concepto": (concepto if grp == "3.1" else proceso) or tipo,
                    "zona": (z31 if grp == "3.1" else zz) or z or "?",
                    "columna": col, "valor": raw,
                    "id": b.caso or "", "municipio": b.municipio or "",
                    "supervisor": b.supervisor or "",
                })
        if concepto and z31 in ZONAS and col31 and b31:
            t31[concepto][z31]["GLOBAL"] += 1
            t31[concepto][z31][b31] += 1
        if proceso and b35:
            t35[proceso][zz][b35] += 1
        casos.append({
            "concepto": concepto, "proceso": proceso, "zona": z31 if (concepto and z31) else z,
            "zona35": zz, "estado": b.estado or "", "b31": b31 or "—", "b35": b35 or "—", "tipo": tipo,
            "id": b.caso, "municipio": b.municipio, "lcl": b.lcl, "pdl": b.pdl,
            "supervisor": b.supervisor, "fecha": str(f35 or ""), "fecha_envio": str(f31 or ""),
        })
    vistos, anom_uniq = set(), []
    for a in anomalias:   # colapsa duplicados idénticos (mismo caso/columna/valor)
        k = (a["grupo"], a["tipo"], a["id"], a["columna"], a["valor"])
        if k not in vistos:
            vistos.add(k)
            anom_uniq.append(a)
    anom_uniq.sort(key=lambda a: (a["grupo"], a["tipo"], a["zona"], str(a["id"])))
    return {"3.1": t31, "3.5": t35, "casos": casos, "corte": hoy.isoformat(), "anomalias": anom_uniq}
# ── 3.5 según la PROGRAMACIÓN (solicitud #22 de Paola, 2026-07-02) ──────────────
# La programación viva del sistema: las programadoras marcan estado_pdt=EJECUTADO y el
# tipo de bolsa va en `proceso`. Mapeo proceso → fila de la tabla 3.5 (PROCESOS_35);
# los procesos que no son bolsa (OBRAS, MANTENIMIENTO, CONEXIONES…) no alimentan 3.5.
PROG_PROCESO_35 = {
    "PQR": "PQR", "PQRS": "PQR", "AMP": "AMP", "IPPE": "IPPE",
    "CUN100": "CUND 100", "CUND100": "CUND 100", "CUND 100": "CUND 100",
    "TELECONTROL": "TLC", "TLC": "TLC", "TUTELA": "TUTELAS", "TUTELAS": "TUTELAS",
    "INVR": "INVR",
}


def _num_pdt_valido(v: Optional[str]) -> Optional[str]:
    """# PDT utilizable como identidad del trabajo: alfanumérico con dígitos (CDOPP2026R05550,
    DF2026...). Descarta vacíos y texto suelto ('EN GESTION') que no identifica nada."""
    s = (v or "").strip().upper()
    return s if len(s) >= 6 and any(ch.isdigit() for ch in s) else None


async def _prog_35(db: AsyncSession, periodo: str) -> dict:
    """Tabla 3.5 EJECUCIÓN contada desde la PROGRAMACIÓN (unidad SABOGAL): trabajos del mes
    con estado_pdt=EJECUTADO, por zona × tipo de bolsa (proceso). Cuenta TRABAJOS, no filas:
    un # PDT marcado EJECUTADO en varios días del mes cuenta una sola vez (sin # PDT válido,
    cada fila cuenta). Devuelve también las filas para el drill-down."""
    y, m = int(periodo[:4]), int(periodo[5:7])
    ini, fin = date(y, m, 1), _fin_de_mes(periodo)
    rows = (await db.execute(select(Programacion).where(
        Programacion.unidad == "SABOGAL",
        Programacion.fecha >= ini, Programacion.fecha <= fin,
    ))).scalars().all()
    tabla = {p: {z: {"EJECUTADO": 0} for z in ZONAS_REPORTE} for p in PROCESOS_35}
    filas, vistos = [], set()
    for r in rows:
        if (r.estado_pdt or "").strip().upper() != "EJECUTADO":
            continue
        proc = PROG_PROCESO_35.get((r.proceso or "").strip().upper())
        if not proc:
            continue
        z = "LV" if r.zona == ZonaEnum.LVTLC else (r.zona.value if r.zona else None)
        if z not in ZONAS_REPORTE:
            continue
        pdt = _num_pdt_valido(r.num_pdt)
        clave = (proc, pdt) if pdt else ("fila", r.id)
        dup = clave in vistos
        if not dup:
            vistos.add(clave)
            tabla[proc][z]["EJECUTADO"] += 1
        filas.append({
            "fecha": str(r.fecha), "zona": z, "proceso": proc, "num_pdt": r.num_pdt or "(sin # PDT)",
            "lider": r.lider, "municipio": r.municipio or "", "supervisor": r.supervisor or "",
            "duplicado": "sí (ya contado)" if dup else "",
        })
    return {"tabla": tabla, "filas": filas}


# ── 3.5 EJECUTADO · cruce BOLSA vs PROGRAMACIÓN por ANTECEDENTE (prueba, 2026-07-24) ──
# El enlace real bolsa↔programación es el ANTECEDENTE: el `caso` de la bolsa coincide con el
# `proyecto_antecedente` de la programación (p.ej. CNXIPPCRUR-1500532052-07-0JN12D). Se cruza
# por el número de antecedente (código Enel de 7+ dígitos, o una orden WR-####), NO por el
# texto completo: así una bolsa cuenta como "programada" aunque en la programación aparezca
# bajo otro subproceso (visto: un IPPE que estaba programado como CONEXION) o con otro sufijo.
SUBPROC_PROC_35 = {
    "IPPC": "IPPE", "IPPE": "IPPE",
    "PQR": "PQR", "PQRS": "PQR",
    "AMP": "AMP",
    "CUN100": "CUND 100", "CUND100": "CUND 100", "CUND 100": "CUND 100", "CUN 100": "CUND 100",
    "TELECONTROL": "TLC", "TLC": "TLC",
    "TUTELA": "TUTELAS", "TUTELAS": "TUTELAS",
}
# Procesos con bolsa cuyo `caso` trae un antecedente cruzable (los que muestra el cruce).
PROCESOS_CRUCE_35 = ["IPPE", "PQR", "AMP", "CUND 100", "TLC", "TUTELAS"]


# Cada proceso enlaza por una llave DISTINTA (verificado con junio real): IPPE por número de
# antecedente Enel; PQR por el número del caso; AMP no trae caso ni LCL, solo el PDL (R#####);
# CUND 100 trae un NOMBRE en el caso → cruza por LCL; TLC trae un número corto → cruza por LCL;
# TUTELAS por el id de tutela (AAAA-NNNNN). Un solo extractor multi-llave las cubre todas: saca
# todos los identificadores posibles de cada registro (con prefijo por tipo para no confundir un
# LCL con un antecedente) y cruza si comparten alguno.
def _llaves_cruce(caso=None, lcl=None, pdl=None, texto=None) -> set:
    """Llaves para cruzar un registro de bolsa con uno de programación. Namespaced por tipo:
    LCL: (localización, en ambos lados), NUM: (código Enel 7+ díg), WR: (orden WR-####),
    PDL: (orden de trabajo R#####), TUT: (id de tutela AAAA-NNNNN)."""
    K = set()
    blob = f"{caso or ''} {texto or ''}".upper()
    L = re.sub(r"\D", "", str(lcl or ""))
    if len(L) >= 6:
        K.add(f"LCL:{L}")
    for m in re.findall(r"WR[-\s]?(\d{3,})", blob):
        K.add(f"WR:{m}")
    for m in re.findall(r"\d{7,}", re.sub(r"WR[-\s]?\d{3,}", " ", blob)):
        K.add(f"NUM:{m}")
    for m in re.findall(r"R\d{4,6}", str(pdl or "").upper()):   # PDL de la bolsa / num_pdt de la prog
        K.add(f"PDL:{m}")
    for m in re.findall(r"20\d{2}-\d{4,5}", blob):
        K.add(f"TUT:{m}")
    return K


async def _cruce_35(db: AsyncSession, periodo: str) -> dict:
    """3.5 ejecutado: cruza cada caso de BOLSA con la PROGRAMACIÓN del mes por su identificador
    (la llave la elige el extractor según el proceso). Por proceso × zona (de la bolsa): cuántas
    bolsas hay, cuántas están en la programación y cuántas no. Guarda el detalle para el drill."""
    y, m = int(periodo[:4]), int(periodo[5:7])
    ini, fin = date(y, m, 1), _fin_de_mes(periodo)
    prog_rows = (await db.execute(select(Programacion).where(
        Programacion.unidad == "SABOGAL",
        Programacion.fecha >= ini, Programacion.fecha <= fin,
    ))).scalars().all()
    prog_index = defaultdict(list)                 # llave → [{zona, subproceso}]
    for r in prog_rows:
        z = "LV" if r.zona == ZonaEnum.LVTLC else (r.zona.value if r.zona else None)
        sub = (r.subproceso or "").strip()
        for k in _llaves_cruce(caso=r.proyecto_antecedente, lcl=r.lcl, pdl=r.num_pdt):
            prog_index[k].append({"zona": z, "subproceso": sub or "(sin subproceso)"})

    casos = (await _bolsas_3x_modulo(db, periodo)).get("casos", [])
    tabla = {p: {z: {"bolsa": 0, "programada": 0, "sin_programar": 0, "otro_subproc": 0, "otra_zona": 0}
                 for z in ZONAS_REPORTE} for p in PROCESOS_CRUCE_35}
    detalle = {p: {z: [] for z in ZONAS_REPORTE} for p in PROCESOS_CRUCE_35}
    for c in casos:
        proc = c.get("proceso")
        if proc not in tabla or c.get("b35") != "EJECUTADO":
            continue
        z = c.get("zona35")
        if z not in ZONAS_REPORTE:
            continue
        keys = _llaves_cruce(caso=c.get("id"), lcl=c.get("lcl"), pdl=c.get("pdl"))
        hits = []
        for k in keys:
            hits.extend(prog_index.get(k, []))
        cell = tabla[proc][z]
        cell["bolsa"] += 1
        if hits:
            cell["programada"] += 1
            subs = sorted({h["subproceso"] for h in hits})
            zonas_prog = sorted({h["zona"] for h in hits if h["zona"]})
            if all(SUBPROC_PROC_35.get(s.upper()) != proc for s in subs):
                cell["otro_subproc"] += 1
            if zonas_prog and z not in zonas_prog:
                cell["otra_zona"] += 1
            estado = "programada"
        else:
            subs, zonas_prog = [], []
            cell["sin_programar"] += 1
            estado = "sin_programar"
        detalle[proc][z].append({
            "caso": c.get("id") or (f"PDL {c.get('pdl')}" if c.get("pdl") else "(sin identificador)"),
            "llave": ", ".join(sorted(k.split(":", 1)[1] for k in keys)) or "—",
            "estado": estado, "prog_subprocesos": subs, "prog_zonas": zonas_prog,
            "municipio": c.get("municipio") or "",
        })
    return {"tabla": tabla, "detalle": detalle, "procesos": PROCESOS_CRUCE_35, "zonas": ZONAS_REPORTE}


# Fallback si una hoja no está en el mapa o cambian los nombres: candidatas por orden.
FECHA_31_CANDIDATAS = ["FECHA ENVIO PRESUPUESTO", "FECHA ENTREGA PPTO", "FECHA PTTO",
                       "FECHA ENVIADO A ENEL", "FECHA ENVIO ENEL"]
FECHA_35_CANDIDATAS = ["FECHA DE PROG", "FECHA DE PROGRAMACION", "FECHA PRGRAMACIÓN",
                       "FECHA EJECUCION", "FECHA EJECUCIÓN"]


def _col_por_nombre(headers: dict, nombre):
    """Columna por nombre exacto (mayúsculas) y, si no, por substring."""
    if not nombre:
        return None
    up = nombre.upper()
    if up in headers:
        return headers[up]
    for h, c in headers.items():
        if up in h or h in up:
            return c
    return None


def _col_fecha(headers: dict, candidatas: list):
    """Primera columna cuyo encabezado contiene alguna de las candidatas (en orden)."""
    for cand in candidatas:
        for h, c in headers.items():
            if cand in h:
                return c
    return None


def _fecha_celda(v):
    """Valor de celda → date (o None si vacío/no parseable)."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    d = _parse_dt(v)
    return d.date() if d else None


def _fecha_corte(nombre: str, periodo: str) -> date:
    """'Hoy' del corte: fecha en el nombre del BOLSAS ENEL (DD-MM-YYYY); si no, fin de mes."""
    import calendar
    m = re.search(r"(\d{1,2})[-_/](\d{1,2})[-_/](20\d{2})", str(nombre or ""))
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d)
        except ValueError:
            pass
    y, mo = int(periodo[:4]), int(periodo[5:7])
    return date(y, mo, calendar.monthrange(y, mo)[1])


def parse_bolsa_snapshot(wb, hoy: date) -> dict:
    """Arma 3.1/3.5 de las hojas de detalle del BOLSAS ENEL clasificando por FECHA vs
    'hoy' (el corte), según la explicación de Paola (hilo #19):
      3.1 (envío del presupuesto): sin fecha=PENDIENTE BOLSA, futura=PLANEADO, ≤hoy=ENTREGADO A ENEL.
      3.5 (ejecución): ≤hoy=EJECUTADO (ya pasó → se ejecutó), futura=PLANEADO.
    Excluye ANULADO. Guarda la lista de casos (para el drill-down)."""
    t31, t35 = _t31_vacio(), _t35_vacio()
    casos = []
    for sheet, tipo in BOLSA_SHEETS.items():
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        hr = None
        headers = {}
        for r in range(1, 7):
            row = [ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)]
            if sum(1 for v in row if isinstance(v, str) and v.strip()) >= 4:
                hr = r
                headers = {str(v).strip().upper(): c + 1 for c, v in enumerate(row) if isinstance(v, str) and v.strip()}
                break
        if not hr:
            continue
        ecol = (headers.get("ESTADO") or headers.get("ESTADO DOMINION") or headers.get("ESTADO REVISADO")
                or next((c for h, c in headers.items()
                         if h.startswith("ESTADO") and "MARE" not in h and "ID" not in h), None))
        zcol = (headers.get("ZONA") or next((c for h, c in headers.items() if "ZONA" in h), None))
        # Columnas de fecha: mapeo autoritativo (lo que Paola pintó) y, si falta, candidatas.
        n31, n35 = BOLSA_FECHAS.get(sheet, (None, None))
        f31col = _col_por_nombre(headers, n31) if sheet in BOLSA_FECHAS else _col_fecha(headers, FECHA_31_CANDIDATAS)
        f35col = _col_por_nombre(headers, n35) if sheet in BOLSA_FECHAS else _col_fecha(headers, FECHA_35_CANDIDATAS)
        if sheet in BOLSA_FECHAS:   # respeta "esta hoja no alimenta ese indicador"
            if n31 is None: f31col = None
            if n35 is None: f35col = None

        def col(*keys):
            for k in keys:
                if k in headers:
                    return headers[k]
            return None

        idcol = col("CASO", "QR", "PQRS", "NOMBRE", "RADICADO", "TICKET NUEVO", "NUM. CASO", "ID", "ACTIVO", "CD", "WR")
        mcol = col("MUNICIPIO")
        lcol = col("LCL")
        pcol = col("PDL")
        scol = col("SUPERVISOR")
        concepto = BOLSA_CONCEPTO_31.get(tipo)
        proceso = BOLSA_PROCESO_35.get(tipo)
        for r in range(hr + 1, ws.max_row + 1):
            est = str(ws.cell(row=r, column=ecol).value or "").strip().upper() if ecol else ""
            if ecol:
                if not est or "ANULAD" in est:
                    continue
            else:  # sin columna de estado: salta filas sin id (vacías)
                if not (idcol and ws.cell(row=r, column=idcol).value):
                    continue
            z = _norm_zona(ws.cell(row=r, column=zcol).value) if zcol else None
            zz = z if z in ZONAS else "LV"
            # 3.1 por fecha de envío del presupuesto
            f31 = _fecha_celda(ws.cell(row=r, column=f31col).value) if f31col else None
            b31 = "PENDIENTE BOLSA" if f31 is None else ("PLANEADO" if f31 > hoy else "ENTREGADO A ENEL")
            # 3.5 por fecha de programación
            f35 = _fecha_celda(ws.cell(row=r, column=f35col).value) if f35col else None
            b35 = None if f35 is None else ("PLANEADO" if f35 > hoy else "EJECUTADO")
            z31 = z if z in ZONAS else BOLSA_ZONA_31_DEFAULT.get(sheet)
            if concepto and z31 in ZONAS and f31col:  # solo si la hoja alimenta 3.1 (col amarilla)
                t31[concepto][z31]["GLOBAL"] += 1
                t31[concepto][z31][b31] += 1
            if proceso and b35:                       # b35=None si no hay col 3.5 (verde) o sin fecha
                t35[proceso][zz][b35] += 1

            def g(c):
                return _cell(ws.cell(row=r, column=c).value) if c else ""
            casos.append({
                "concepto": concepto, "proceso": proceso, "zona": z, "zona35": zz,
                "estado": est, "b31": b31, "b35": b35 or "—", "tipo": tipo,
                "id": g(idcol), "municipio": g(mcol), "lcl": g(lcol),
                "pdl": g(pcol), "supervisor": g(scol),
                "fecha": g(f35col), "fecha_envio": g(f31col),
            })
    return {"3.1": t31, "3.5": t35, "casos": casos, "corte": hoy.isoformat()}


async def _indicadores_bolsas(db: AsyncSession, periodo: Optional[str] = None) -> dict:
    """3.1 / 3.5 calculados directamente del módulo Bolsas ya cargado (por fecha vs fin de
    mes). No requiere re-subir el BOLSAS ENEL."""
    d = await _bolsas_3x_modulo(db, periodo)
    return {"3.1": d["3.1"], "3.5": d["3.5"]}


async def _bolsas_carga(db: AsyncSession) -> dict:
    """Última carga del módulo Bolsas (pedido de Paola en el 3.1): cuándo entraron los
    datos y qué archivo Excel del cargue masivo los respalda (si ya se guardó).
    `al_dia` = la última carga es de HOY (día de Bogotá) — es el candado del correo de estado
    (pedido de Sergio, 2026-07-30: sin bolsas del día no hay correo, para que no se olvide
    actualizarlas). creado_en lo pone el servidor de BD en UTC → se corrige −5 h para
    comparar el DÍA (una carga de las 8 pm de Bogotá ya cae en el día siguiente en UTC)."""
    ultima = (await db.execute(select(func.max(Bolsa.creado_en)))).scalar_one_or_none()
    hoy_bog = (datetime.now(_tz.utc) - timedelta(hours=5)).date()
    al_dia = bool(ultima) and (ultima - timedelta(hours=5)).date() == hoy_bog
    f = (await db.execute(
        select(AnsFuente).where(AnsFuente.tipo == "bolsas")
        .order_by(AnsFuente.creado_en.desc(), AnsFuente.id.desc()).limit(1))).scalar_one_or_none()
    return {
        "datos": _bog(ultima).isoformat() if ultima else None,   # mostrada en hora de Bogotá
        "al_dia": al_dia,
        "archivo": ({"id": f.id, "nombre": f.nombre,
                     "creado_en": _bog(f.creado_en).isoformat() if f.creado_en else None} if f else None),
    }


async def _alimentacion(db: AsyncSession, periodo: str) -> dict:
    """«Alimentación de los indicadores» (pedido de Sergio, 2026-07-31): de qué datos viven los
    indicadores y quién los tiene al día. (1) Última subida de STM del periodo (fecha y hora) y
    última carga de bolsas; (2) semáforo por rol, con los MISMOS pendientes de la campanita:
    · verde = al día · rojo = pendientes propios (está frenando el indicador) · amarillo =
    tiene pendientes PERO su insumo aguas arriba también está pendiente («no es su culpa
    todavía»). La cadena real es la de la campanita: PROGRAMADORAS + MARÍA INÉS alimentan al
    ENCARGADO ANS — él no puede cerrar exclusiones si a ellas les falta lo suyo."""
    fs = (await db.execute(select(AnsFuente).where(
        AnsFuente.periodo == periodo, AnsFuente.tipo.in_(("programados", "st")))
        .order_by(AnsFuente.creado_en.desc(), AnsFuente.id.desc()))).scalars().all()
    stm = None
    if fs:
        stm = {"fecha": _bog(fs[0].creado_en).isoformat() if fs[0].creado_en else None,
               "nombre": fs[0].nombre, "cortes": len(fs)}

    regs = (await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo))).scalars().all()
    pend_rol = {}
    for t in TAREAS_ANS:
        pend_rol[t["rol"]] = pend_rol.get(t["rol"], 0) + sum(1 for r in regs if _pendiente(r, t["id"]))
    cadena = [("programador", "Programadoras", []),
              ("ingeniero_zona", "María Inés / Ingeniería", []),
              ("cumplimentador", "Cumplimentadores", []),
              ("ans", "Encargado ANS", ["programador", "ingeniero_zona"])]
    roles = []
    for rol, label, arriba in cadena:
        p = pend_rol.get(rol, 0)
        bloqueado = any(pend_rol.get(a, 0) > 0 for a in arriba)
        estado = "verde" if p == 0 else ("amarillo" if bloqueado else "rojo")
        motivo = ("Al día." if p == 0 else
                  f"{p} celdas pendientes, pero su insumo aún no está completo: depende de lo que "
                  "falta aguas arriba (no es responsabilidad suya todavía)." if bloqueado else
                  f"{p} celdas pendientes — es lo que está frenando el indicador.")
        roles.append({"rol": rol, "label": label, "pendientes": p, "estado": estado, "motivo": motivo})
    return {"stm": stm, "bolsas": await _bolsas_carga(db), "roles": roles}


@router.get("/indicadores")
async def indicadores(periodo: str, db: AsyncSession = Depends(get_db),
                      _: Usuario = Depends(get_current_active_user)):
    prog = await _indicadores_programacion(db, periodo)
    bol = await _indicadores_bolsas(db, periodo)
    p35 = await _prog_35(db, periodo)
    alim = await _alimentacion(db, periodo)
    return {"periodo": periodo, **prog, **bol, "3.5_prog": p35["tabla"],
            "bolsas_carga": alim["bolsas"], "alimentacion": alim}


# ── Correo de estado (solo dev) ───────────────────────────────────────────────
# Genera el correo «ESTADO ANS PROGRAMADOS 3.1 y 3.2 <MES> CORTE <fecha>» con las
# tablas YA incrustadas en el cuerpo (HTML apto para Outlook, sin pantallazos).
# El server NUNCA envía nada ni conoce destinatarios: esto solo se DESCARGA como
# archivo; el Cazador (en el PC) arma el borrador en Outlook y Sergio lo envía a mano.

# OJO: _MESES_ES ya existe más abajo (Title-case, para el nombre del export); aquí se
# reutiliza con .upper() — no redefinirla, que la última definición del módulo gana.
_e = _html.escape


def _fecha_corta_correo(f) -> str:
    s = str(f or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(3)}/{m.group(2)}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}"
    return s[:10]


def _tabla_zonas_correo(titulo: str, valor, tabla: dict, modo: str = "ordenes") -> str:
    """La tablita «cumplen / no cumplen / exclusiones» por zona de la pestaña Indicadores
    (TablaZonas), como HTML inline para el correo. Va AL LADO de su dinámica."""
    td = "padding:4px 10px;border:1px solid #cbd5e1;font-size:10pt;text-align:center"
    th = "padding:4px 10px;border:1px solid #cbd5e1;background:#f3f4f6;color:#4b5563;font-size:10pt"
    g = lambda z, k: (tabla or {}).get(z, {}).get(k, 0) or 0
    if modo == "nodos":
        filas = [("Nodos ejecutados", lambda z: g(z, "cumple"), "#15803d", False),
                 ("Nodos programados", lambda z: g(z, "cumple") + g(z, "no_cumple"), "#374151", True),
                 ("Exclusiones", lambda z: g(z, "exclusiones"), "#b45309", False)]
    else:
        filas = [("Cumplen", lambda z: g(z, "cumple"), "#15803d", False),
                 ("No cumplen", lambda z: g(z, "no_cumple"), "#dc2626", False),
                 ("Exclusiones", lambda z: g(z, "exclusiones"), "#b45309", False),
                 ("Total órdenes", lambda z: g(z, "cumple") + g(z, "no_cumple") + g(z, "exclusiones"), "#1f2937", True)]
    tit = f"<p style='margin:0 0 3px'><b>{_e(titulo)}</b>"
    if valor is not None:
        tit += f" — <span style='color:#1d4ed8'><b>{(valor or 0) * 100:.1f}%</b></span>"
    tit += "</p>"
    out = [f"<tr><th style='{th}'></th>"
           + "".join(f"<th style='{th}'>{z}</th>" for z in ZONAS_REPORTE)
           + f"<th style='{th};background:#e5e7eb'>TOTAL</th></tr>"]
    for label, fn, color, es_total in filas:
        fondo = ";background:#f9fafb" if es_total else ""
        peso = "bold" if es_total else "normal"
        celdas = "".join(f"<td style='{td};color:{color};font-weight:{peso}{fondo}'>{fn(z)}</td>"
                         for z in ZONAS_REPORTE)
        total = sum(fn(z) for z in ZONAS_REPORTE)
        out.append(f"<tr><td style='{td};text-align:left;font-weight:bold{fondo}'>{_e(label)}</td>{celdas}"
                   f"<td style='{td};color:{color};font-weight:bold;background:#f9fafb'>{total}</td></tr>")
    return (tit + "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse'>"
            + "".join(out) + "</table>")


def _tabla_matriz_bolsas_correo(tabla: dict) -> str:
    """La matriz de bolsas del 3.1 (concepto × zona con PLANEADO/PENDIENTE/ENTREGADO),
    como en la tarjeta 3.1 de la plataforma."""
    td = "padding:4px 9px;border:1px solid #cbd5e1;font-size:9pt;text-align:center"
    th = "padding:4px 9px;border:1px solid #cbd5e1;background:#f3f4f6;color:#4b5563;font-size:9pt"
    subcols = [("PLANEADO", "PLANEADO"), ("PENDIENTE BOLSA", "PENDIENTE"), ("ENTREGADO A ENEL", "ENTREGADO")]
    head1 = (f"<tr><th style='{th}' rowspan='2'>Concepto</th>"
             + "".join(f"<th style='{th};text-align:center' colspan='3'>{z}</th>" for z in ZONAS) + "</tr>")
    head2 = "<tr>" + "".join(f"<th style='{th};font-weight:normal'>{lbl}</th>"
                             for _ in ZONAS for _k, lbl in subcols) + "</tr>"
    filas = []
    for concepto, zonas in (tabla or {}).items():
        celdas = ""
        for z in ZONAS:
            for k, _lbl in subcols:
                v = (zonas.get(z) or {}).get(k, 0) or 0
                estilo = "color:#1d4ed8;font-weight:bold" if v else "color:#cbd5e1"
                celdas += f"<td style='{td};{estilo}'>{v}</td>"
        filas.append(f"<tr><td style='{td};text-align:left;font-weight:bold'>{_e(str(concepto))}</td>{celdas}</tr>")
    return ("<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;margin:4px 0'>"
            + head1 + head2 + "".join(filas) + "</table>")


def _tabla_descuento_correo(valor, filas: list) -> str:
    """El cuadro META (Cumplimiento/Descuento) que va AL FRENTE del resultado."""
    td = "padding:4px 12px;border:1px solid #cbd5e1;font-size:10pt"
    th = "padding:4px 12px;border:1px solid #0f172a;background:#1e293b;color:#fff;font-size:10pt;text-align:left"
    tit = f"<p style='margin:0 0 3px'><b>META</b> — resultado <b style='color:#1d4ed8'>{(valor or 0) * 100:.1f}%</b></p>"
    cuerpo = "".join(f"<tr><td style='{td}'>{_e(f[0])}</td><td style='{td}'>{_e(f[1])}</td></tr>"
                     for f in filas or [])
    return (tit + "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse'>"
            f"<tr><th style='{th}'>Cumplimiento</th><th style='{th}'>Descuento</th></tr>{cuerpo}</table>")


def _lado_a_lado(izq: str, der: str, gap: int = 28) -> str:
    """Dos bloques al frente el uno del otro (tabla contenedora; flex no existe en Outlook)."""
    return ("<table cellspacing='0' cellpadding='0' style='margin:8px 0'><tr>"
            f"<td valign='top'>{izq}</td><td valign='top' style='padding-left:{gap}px'>{der}</td></tr></table>")


def _tabla_dinamica_correo(caso: str, criterio: str, items: list, mes_label: str = "") -> str:
    """La misma dinámica de la pestaña Indicadores (equipos que NO cumplen, filtro
    Excluible=NO) pero como tabla HTML con estilos inline, digerible por Outlook."""
    td = "padding:4px 8px;border:1px solid #cbd5e1;font-size:10pt"
    lst = [it for it in items or [] if it.get("excluible") == "NO"]
    tit = (f"<p style='margin:0 0 3px'><b>Dinámica {caso} — equipos que NO cumplen"
           f"{' · ' + _e(mes_label) if mes_label else ''}</b><br>"
           f"<span style='color:#64748b;font-size:9pt'>Se mide: {_e(criterio)}; solo órdenes sin exclusión aprobada.</span></p>")
    if not lst:
        return tit + "<p style='margin:2px 0;color:#15803d;font-size:10pt'>Ningún equipo está incumpliendo. ✅</p>"
    arbol = {}
    for it in lst:
        z = arbol.setdefault(it["zona"], {})
        z.setdefault((it["supervisor"], it["lider"]), []).append(it)
    filas = []
    for zona in sorted(arbol):
        equipos = arbol[zona]
        n = sum(len(o) for o in equipos.values())
        filas.append(
            f"<tr><td colspan='4' style='{td};background:#dbeafe;color:#1e3a8a;font-weight:bold'>"
            f"ZONA {_e(zona)} <span style='font-weight:normal;color:#1d4ed8'>— {n} {'órdenes' if n != 1 else 'orden'} "
            f"en {len(equipos)} equipo{'s' if len(equipos) != 1 else ''}</span></td></tr>")
        for (sup, lid), ords in equipos.items():
            for j, o in enumerate(ords):
                celdas = ""
                if j == 0:
                    celdas = (f"<td rowspan='{len(ords)}' style='{td}'><b>{_e(sup)}</b><br>"
                              f"<span style='color:#64748b;font-size:9pt'>Líder: {_e(lid)}</span></td>"
                              f"<td rowspan='{len(ords)}' style='{td};text-align:center;color:#b91c1c'><b>{len(ords)}</b></td>")
                chip = f"<b>{_e(o.get('pdl') or '')}</b> · {_e(_fecha_corta_correo(o.get('fecha')))} · {_e(o.get('motivo_corto') or o.get('motivo') or '')}"
                obs = (o.get("observacion") or "").strip()
                obs_html = _e(obs) if obs else "<span style='color:#94a3b8'>—</span>"
                filas.append(f"<tr>{celdas}<td style='{td};background:#fef2f2;color:#991b1b;white-space:nowrap'>{chip}</td>"
                             f"<td style='{td}'>{obs_html}</td></tr>")
    filas.append(f"<tr><td style='{td};background:#f1f5f9'><b>Total general</b></td>"
                 f"<td style='{td};background:#f1f5f9;text-align:center;color:#b91c1c'><b>{len(lst)}</b></td>"
                 f"<td colspan='2' style='{td};background:#f1f5f9;color:#64748b'>"
                 f"{sum(len(z) for z in arbol.values())} equipo(s) señalado(s)</td></tr>")
    th = "padding:5px 8px;border:1px solid #475569;background:#334155;color:#fff;font-size:10pt;text-align:left"
    return (tit + "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;margin:4px 0'>"
            f"<tr><th style='{th}'>Equipo (supervisor / líder)</th><th style='{th};text-align:center'>Órdenes</th>"
            f"<th style='{th}'>Detalle del incumplimiento</th><th style='{th}'>Observación</th></tr>"
            + "".join(filas) + "</table>")


@router.get("/correo-estado")
async def correo_estado(periodo: str, db: AsyncSession = Depends(get_db),
                        current_user: Usuario = Depends(get_current_active_user)):
    if getattr(current_user.rol, "value", str(current_user.rol)) != "desarrollador":
        raise HTTPException(status_code=403, detail="Correo de estado: solo el desarrollador por ahora")
    # Última carga de bolsas (pedido de Sergio, 2026-07-30): la misma que muestra la tarjeta
    # 3.1 (_bolsas_carga = max(creado_en) del módulo Bolsas), tal cual, p. ej. «27/07/2026, 16:13»
    carga_b = await _bolsas_carga(db)
    try:
        ult_bolsas = datetime.fromisoformat(carga_b["datos"]).strftime("%d/%m/%Y, %H:%M") \
            if carga_b.get("datos") else "— (sin bolsas cargadas)"
    except ValueError:
        ult_bolsas = carga_b.get("datos") or "— (sin bolsas cargadas)"
    # CANDADO (pedido de Sergio, 2026-07-30): sin bolsas actualizadas HOY no se genera el
    # correo — obliga a refrescarlas primero (a veces se olvida y el correo saldría viejo).
    if not carga_b.get("al_dia"):
        raise HTTPException(status_code=409, detail=(
            f"Las bolsas no se han actualizado HOY (última carga: {ult_bolsas}). "
            "Actualiza el módulo Bolsas con el corte del día y vuelve a generar el correo."))
    prog = await _indicadores_programacion(db, periodo)
    r31 = await _resultado_31(db, periodo)
    bol31 = (await _bolsas_3x_modulo(db, periodo))["3.1"]          # matriz de bolsas del 3.1
    hoy = (datetime.now(_tz.utc) - timedelta(hours=5)).date()      # fecha de Bogotá
    corte = hoy.strftime("%d/%m/%Y")
    mes = _MESES_ES[int(periodo[5:7])].upper()
    asunto = f"ESTADO ANS PROGRAMADOS 3.1 y 3.2 {mes} CORTE {corte}"

    pct = lambda v: f"{(v or 0) * 100:.1f}%"
    td = "padding:4px 10px;border:1px solid #cbd5e1;font-size:10pt;text-align:center"
    th = "padding:5px 10px;border:1px solid #475569;background:#334155;color:#fff;font-size:10pt"

    # Tabla resultado 3.1 (la SENDA 35): concepto / total / cumple / no cumple / %
    f31 = "".join(
        f"<tr><td style='{td};text-align:left'>{_e(f['concepto'])}</td><td style='{td}'>{f['total']}</td>"
        f"<td style='{td};color:#15803d'>{f['cumple']}</td><td style='{td};color:#b91c1c'>{f['no_cumple']}</td>"
        f"<td style='{td}'>{pct(f['cumplimiento'])}</td></tr>"
        for f in r31["filas"])
    t31 = (f"<table cellspacing='0' cellpadding='0' style='border-collapse:collapse;margin:4px 0'>"
           f"<tr><th style='{th};text-align:left'>Concepto</th><th style='{th}'>Cantidad</th>"
           f"<th style='{th}'>Cumple</th><th style='{th}'>No cumple</th><th style='{th}'>Cumplimiento</th></tr>"
           f"{f31}"
           f"<tr><td style='{td};text-align:left;background:#f1f5f9'><b>Total</b></td>"
           f"<td style='{td};background:#f1f5f9'><b>{r31['total']}</b></td>"
           f"<td style='{td};background:#f1f5f9;color:#15803d'><b>{r31['cumple']}</b></td>"
           f"<td style='{td};background:#f1f5f9;color:#b91c1c'><b>{r31['no_cumple']}</b></td>"
           f"<td style='{td};background:#f1f5f9'><b>{pct(r31['cumplimiento'])}</b></td></tr></table>")

    i32 = prog["3.2"]
    v31, v32 = r31["cumplimiento"], i32["ANS"]
    frase31 = (f"se pierde por <span style='color:red'>{pct(v31)}</span>" if v31 < 0.93
               else f"va en <span style='color:#15803d'>{pct(v31)}</span>")
    frase32 = (f"se pierde al <span style='color:red'>{pct(v32)}</span>" if v32 < 1
               else f"va en <span style='color:#15803d'>{pct(v32)}</span>")

    criterios = {
        "A": "apertura o cierre fuera del tiempo permitido (más de 10 min de desfase)",
        "B": "no se ejecutó — anuladas por el Centro Operativo",
        "C": "nodos planeados que quedaron sin ejecutar",
    }
    titulos = {"A": "A — Oportunidad en la ejecución", "B": "B — Efectividad en la apertura",
               "C": "C — Alcance (nodos)"}
    vals = {"A": i32["indA"], "B": i32["indB"], "C": i32["indC"]}
    mes_label = f"{_MESES_ES[int(periodo[5:7])]} {periodo[:4]}"
    # Cada caso va como en la plataforma: la tablita por zonas y, AL LADO, su dinámica
    # (lado a lado con una tabla contenedora — flex no existe en Outlook). Entre casos,
    # la misma línea separadora gris de la tarjeta 3.2.
    sep = "<div style='border-top:1px solid #e5e7eb;margin:14px 0 0'></div>"
    dinamicas = sep.join(
        _lado_a_lado(_tabla_zonas_correo(titulos[c], vals[c], i32[c], modo='nodos' if c == 'C' else 'ordenes'),
                     _tabla_dinamica_correo(c, criterios[c], prog['NO_CUMPLEN'].get(c), mes_label))
        for c in ("A", "B", "C"))

    # Tarjeta con el marco de colores de la plataforma: borde + franja izquierda gruesa
    # del tono del indicador (3.1 azul, 3.2 verde) y el título en su color.
    def _card(borde, franja, color_titulo, titulo, contenido):
        return ("<table cellspacing='0' cellpadding='0' style='margin:14px 0;width:100%'><tr>"
                f"<td style='border:1px solid {borde};border-left:4px solid {franja};"
                f"padding:10px 14px;background:#ffffff'>"
                f"<p style='font-size:13pt;margin:0 0 8px;color:{color_titulo}'><b>{titulo}</b></p>"
                f"{contenido}</td></tr></table>")

    # 3.1 COMPLETO como la tarjeta de la plataforma: matriz de bolsas + resultado SENDA
    # con el cuadro META al frente.
    card31 = _card(
        "#93c5fd", "#2563eb", "#1e40af",
        f"&bull; Indicador 3.1 — {frase31}",
        _tabla_matriz_bolsas_correo(bol31)
        + "<p style='margin:10px 0 3px;font-size:10pt;color:#4b5563'><b>Resultado del indicador (visitas "
          "entregadas) — medición contra la SENDA (35)</b></p>"
        + _lado_a_lado(t31, _tabla_descuento_correo(v31, r31.get("descuento") or DESCUENTO_31)))

    # 3.2: A, B y C en cuadros grandes + el cuadro GRANDE del ANS + tablita META al
    # frente, y luego los casos.
    def _mini_box(letra, nombre, v):
        return ("<td style='border:1px solid #cbd5e1;background:#ffffff;padding:8px 16px'>"
                f"<p style='margin:0;font-size:8pt;color:#64748b'><b>{letra} — {nombre}</b></p>"
                f"<p style='margin:2px 0 0;font-size:16pt;color:#1d4ed8'><b>{pct(v)}</b></p></td>")

    gap = "<td style='width:10px'></td>"
    ans_box = ("<table cellspacing='0' cellpadding='0'><tr>"
               + _mini_box("A", "Oportunidad", i32["indA"]) + gap
               + _mini_box("B", "Efectividad", i32["indB"]) + gap
               + _mini_box("C", "Alcance", i32["indC"]) + gap +
               "<td style='border:1px solid #86efac;background:#f0fdf4;padding:8px 20px'>"
               "<p style='margin:0;font-size:9pt;color:#166534'><b>Cumplimiento ANS 3.2</b></p>"
               f"<p style='margin:2px 0;font-size:22pt;color:#15803d'><b>{pct(i32['ANS'])}</b></p>"
               f"<p style='margin:0;font-size:9pt;color:#64748b'>= 0.25·A + 0.25·B + 0.50·C</p>"
               "</td></tr></table>")
    card32 = _card(
        "#86efac", "#16a34a", "#166534",
        f"&bull; Indicador 3.2 — {frase32}",
        _lado_a_lado(ans_box, _tabla_descuento_correo(i32["ANS"], DESCUENTO_32))
        + "<p style='margin:8px 0 4px;font-size:10pt;color:#334155'>En los cuadros se visualiza la zona, "
          "el supervisor y el líder responsable de cada incumplimiento, con la observación registrada "
          "en la plataforma.</p>" + dinamicas)

    # ── «Alimentación de los indicadores» — DE PRIMERAS en el correo (pedido de Sergio):
    #    última subida de STM, última carga de bolsas y el semáforo por rol ──
    alim = await _alimentacion(db, periodo)
    if alim["stm"] and alim["stm"].get("fecha"):
        stm_txt = datetime.fromisoformat(alim["stm"]["fecha"]).strftime("%d/%m/%Y, %H:%M")
        stm_txt += f" ({alim['stm']['nombre']}" + (
            f" · {alim['stm']['cortes']} cortes del mes)" if alim["stm"]["cortes"] > 1 else ")")
    else:
        stm_txt = "— (sin STM subidos este mes)"
    _COL_ROL = {"verde": ("#dcfce7", "#166534"), "amarillo": ("#fef9c3", "#854d0e"),
                "rojo": ("#fee2e2", "#991b1b")}
    chips_roles = "".join(
        f"<span style='display:inline-block;margin:2px 8px 2px 0;padding:3px 12px;border-radius:9999px;"
        f"background:{_COL_ROL[r['estado']][0]};color:{_COL_ROL[r['estado']][1]};font-size:10pt;font-weight:600'>"
        + (f"{r['label']} — al día" if r["estado"] == "verde" else
           f"{r['label']} — {r['pendientes']} pendientes (esperando insumo)" if r["estado"] == "amarillo" else
           f"{r['label']} — {r['pendientes']} pendientes")
        + "</span>"
        for r in alim["roles"])
    card_alim = (
        "<div style='border:1px solid #cbd5e1;border-radius:8px;padding:10px 14px;margin:0 0 14px'>"
        "<p style='margin:0 0 6px;font-weight:700'>Alimentación de los indicadores</p>"
        f"<p style='margin:0 0 2px;font-size:10pt;color:#334155'>Última actualización de STM: <b>{stm_txt}</b></p>"
        f"<p style='margin:0 0 8px;font-size:10pt;color:#334155'>Última carga de bolsas: <b>{ult_bolsas}</b></p>"
        f"<p style='margin:0 0 6px'>{chips_roles}</p>"
        "<p style='margin:0;font-size:9pt;color:#64748b'>Verde: el rol está al día. Amarillo: tiene celdas "
        "pendientes pero su insumo aguas arriba aún no está completo (no es responsabilidad suya todavía). "
        "Rojo: pendientes propios que están frenando los indicadores.</p>"
        "</div>")

    cuerpo = (
        "<div style='font-family:Calibri,Arial,sans-serif;font-size:11pt;color:#1f2937'>"
        "<p>Buenas tardes,</p>"
        "<p>Se comparte el estado de los indicadores, el cual pueden consultar en la plataforma "
        f"SG07 (Sistema de Gestión Operativa Zona 7). Corte al {corte}.</p>"
        + card_alim + card31 + card32 +
        "</div>")

    return {"version": 1, "tipo": "estado_ans", "generado": datetime.now(_tz.utc).isoformat(),
            "periodo": periodo, "corte": corte, "asunto": asunto, "html": cuerpo,
            "ultima_carga_bolsas": ult_bolsas, "alimentacion": alim}


@router.get("/anomalias")
async def anomalias(periodo: Optional[str] = None, db: AsyncSession = Depends(get_db),
                    _: Usuario = Depends(get_current_active_user)):
    """Reporte 'datos por revisar': celdas de fecha (3.1/3.5) del módulo Bolsas con un valor que NO
    es una fecha válida (rangos, typos de año, notas, 'NA'...). El sistema no las puede ubicar y por
    eso no las cuenta — aquí quedan en evidencia para que un encargado las corrija en la bolsa.
    Es independiente del periodo (escanea todos los casos cargados)."""
    d = await _bolsas_3x_modulo(db, periodo)
    an = d.get("anomalias", [])
    return {"anomalias": an, "total": len(an)}


# ══════════════════════════════════════════════════════════════════════════════
#  NUEVO (cambio_final): tablas de 3.1 (resultado + naturaleza) y 3.5 (general +
#  por zona) alimentadas por la "Programación diaria ENEL" (la de los 20'). ADITIVO:
#  no toca la extracción ni los indicadores existentes; vive en /indicadores-plus.
# ══════════════════════════════════════════════════════════════════════════════
PROC35_ORDEN = ["PQR", "AMP", "IPPE", "CUND 100", "TLC", "TUTELAS"]   # se cuentan por LCL
# Umbrales de descuento SLA (la "naturaleza" del indicador; del doc de Paola):
DESCUENTO_31 = [["C ≥ 93%", "0%"], ["89% ≤ C < 93%", "−1%"], ["C < 89%", "−2%"]]
# Umbrales de descuento del 3.2 (cuadro «Cumplimiento/Descuento» del Excel de Paola)
DESCUENTO_32 = [["%C ≥ 95%", "0%"], ["90% ≤ %C < 95%", "−1,0%"], ["%C < 90%", "−2,0%"]]
DESCUENTO_35 = [["C ≥ 95%", "0%"], ["90% ≤ C < 95%", "−1%"], ["C < 90%", "−2%"]]

# El TOTAL del indicador 3.1 (fila "Total"/celda C9 de la hoja 3.1 del export) NO es la suma de
# los casos expuestos en el módulo Bolsas (eso daba 320) — Santana (vía Sergio, 2026-07-22) confirmó
# que es un tema ESTRUCTURAL del cálculo del indicador: el universo sobre el que se mide el
# cumplimiento de 3.1 es fijo en 35, no varía con cuántos casos haya cargados. Solo afecta la fila
# Total (agregada); cada concepto (IPPE/CUN100/…) sigue mostrando su propio conteo real.
TOTAL_31_FIJO = 35

# Fuente de cada dato (para el popup de justificación): módulo (link app, nueva pestaña) o archivo
# (se descarga por /ans/fuentes/{id}). Así siempre queda claro DE DÓNDE sale cada número.
FUENTE_BOLSAS = {"tipo": "modulo", "label": "Módulo Bolsas", "ruta": "/bolsas"}
FUENTE_PROG = {"tipo": "modulo", "label": "Módulo Programación", "ruta": "/programacion"}


async def _fuente_prog20(db) -> dict:
    f = (await db.execute(select(AnsFuente).where(AnsFuente.tipo == "prog20")
                          .order_by(AnsFuente.id.desc()))).scalars().first()
    if f:
        return {"tipo": "archivo", "label": f.nombre, "fuente_id": f.id}
    return {"tipo": "archivo", "label": "Programación de los 20’ (aún no cargada)", "fuente_id": None}


async def _fuente_stm(db, periodo) -> dict:
    fs = (await db.execute(select(AnsFuente).where(
        AnsFuente.periodo == periodo, AnsFuente.tipo.in_(("programados", "st")))
        .order_by(AnsFuente.id.desc()))).scalars().all()
    if fs:
        # Los STM son varios cortes del mes (se acumulan). Enlazamos el último como muestra y
        # avisamos cuántos hay (todos están en la pestaña Procedencia).
        extra = f" (+{len(fs) - 1} cortes más en Procedencia)" if len(fs) > 1 else ""
        return {"tipo": "archivo", "label": fs[0].nombre, "fuente_id": fs[0].id, "nota_fuente": extra}
    return {"tipo": "modulo", "label": "Procedencia STM (pestaña)", "ruta": "/ans"}


def _znorm(x):
    """Provincia/zona (con acentos y mayúsc. variadas) → UBATE/ALMEIDAS/GUAVIO/LV o None."""
    p = str(getattr(x, "value", x) or "").strip().upper()
    if p.startswith("UBAT"):
        return "UBATE"
    if p.startswith("ALMEIDAS") or p.startswith("ALEMEIDAS") or "CHOCONTA" in p:
        return "ALMEIDAS"
    if p.startswith("GUAVIO"):
        return "GUAVIO"
    if p.startswith("LV") or "LINEA VIVA" in p or "LÍNEA VIVA" in p:
        return "LV"
    return None


# ── Traductor oficial PROCESO ↔ SUBPROCESO (archivo «PROCESOS Y SUBPROCESO», jul-2026) ──
# Dice de qué PROCESO canónico es cada subproceso. Con él (más las equivalencias de nombre tipo
# IPPE≈IPPC) se auto-siembra el mapeo subproceso→concepto 3.5; sigue siendo EDITABLE en
# «Mapeo 20'» — el traductor solo pone la semilla.
_TRADUCTOR_SUB_PROC = {
    "AMP": "MANTENIMIENTO", "COMETAS": "MANTENIMIENTO", "DOBLETEO PASES": "MANTENIMIENTO",
    "EQ INDISPONIBLES": "MANTENIMIENTO", "INSPECCIONES": "MANTENIMIENTO", "OTROS": "MANTENIMIENTO",
    "OBRAS SIMPLES": "OBRAS", "POSTE MADERA": "OBRAS",
    "BOLETIN PAGO": "PQR", "CREG038": "PQR", "PEAJES": "PQR", "PQR": "PQR", "SSPD": "PQR",
    "TUTELA": "PQR",
    "INSTALACION": "TELECONTROL", "PNC": "TELECONTROL", "PREVENTIVO": "TELECONTROL",
    "REC INDISPONIBLE": "TELECONTROL",
    "CUN100": "CONEXIONES", "IPPC": "CONEXIONES", "NNDD": "CONEXIONES",
    "FAER": "CONVENIOS", "SMEG": "CONVENIOS",
    "EQ COMBINADO": "COMERCIAL", "TOP MOROSOS": "COMERCIAL",
}


def _norm35(s) -> str:
    """MAYÚSCULAS sin tildes y con espacios colapsados — para comparar encabezados/subprocesos."""
    t = unicodedata.normalize("NFD", str(s or ""))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip().upper()


def _guess_proceso(sub, proc):
    """Subproceso de la programación 20' → concepto 3.5 (semilla EDITABLE del mapeo).
    Primero equivalencias de nombre (IPPE≈IPPC, CUN100≈CUND 100≈«Cundinamarca al 100%», AMP*,
    TUTELA*); luego el proceso canónico del TRADUCTOR: PQR→PQR, TELECONTROL→TLC y OBRAS→OBRAS
    (la fila de inversión: no cuenta por LCL, va en plata — pauta ENEL ago-2026). El resto
    (MANTENIMIENTO, CONEXIONES, CONVENIOS, COMERCIAL) no puntúa en 3.5 → None."""
    s, p = _norm35(sub), _norm35(proc)
    if not s:
        return None
    if "TUTELA" in s:
        return "TUTELAS"
    if "IPPE" in s or "IPPC" in s:
        return "IPPE"
    if s.startswith("AMP"):
        return "AMP"
    if s.startswith("CUND") or ("CUN" in s and "100" in s):
        return "CUND 100"
    canon = _TRADUCTOR_SUB_PROC.get(s) or (p if p in set(_TRADUCTOR_SUB_PROC.values()) else None)
    if canon == "PQR" or "PQR" in s:
        return "PQR"
    if canon == "TELECONTROL" or "TELECONTROL" in p or "EQUIPOS" in s:
        return "TLC"
    if canon == "OBRAS" or s == "OBRAS":   # el archivo también trae «OBRAS» como subproceso
        return "OBRAS"
    return None


# Encabezados del formato REAL de los 20' («Programación Unificada», hoja P. UNIFICADA — el
# archivo de referencia anterior era otro y por eso el 3.5 quedó montado sobre columnas que no
# existían). Se resuelven POR NOMBRE, no por posición: si mueven una columna, se sigue encontrando.
_PROG20_REQUERIDAS = {
    "zona": ("ZONA",),
    "fecha": ("FECHA",),
    "proceso": ("PROCESO",),
    "subproceso": ("SUBPROCESO",),
    "lcl": ("LCL",),
}
_PROG20_OPCIONALES = {
    "lider": ("LIDER",),
    "tipo_pdt": ("TIPO PDT",),
    "pdt": ("# PDT", "#PDT", "N PDT", "PDT"),
    "estado": ("ESTADO",),
    "municipio": ("MUNICIPIO",),
    "alcance": ("ALCANCE",),
    "valor_mo": ("VALOR MO", "MO PROYECTADA"),
    "valor_mat": ("VALOR MATERIALES",),
    "nodos_prog": ("NODOS PMS",),
    "cant_nodos": ("CANT NODOS",),
    "avance": ("AVANCE 20:30", "AVANCE"),
}
# Rellenos que la programación trae en celdas aún sin dato — no son una LCL real
_LCL_PLACEHOLDER = {"EN GESTION", "EN PROGRESO", "PENDIENTE", "N/A", "NA", "-", "SIN LCL"}


def _prog20_localizar_hoja(wb):
    """Encuentra la hoja y la fila de encabezados del formato P. UNIFICADA. Devuelve
    (ws, fila_encabezados, {campo: índice}) o ValueError con un mensaje que sí ayuda."""
    candidatas = sorted(wb.sheetnames, key=lambda n: 0 if "UNIFICADA" in _norm35(n) else 1)
    for nombre in candidatas:
        ws = wb[nombre]
        if not hasattr(ws, "iter_rows"):      # hoja de gráfico (Chartsheet): no tiene celdas
            continue
        for nfila, row in enumerate(ws.iter_rows(min_row=1, max_row=5, values_only=True), start=1):
            headers = {}
            for i, v in enumerate(row or ()):
                h = _norm35(v)
                if h and h not in headers:
                    headers[h] = i
            cols = {}
            for campo, nombres in _PROG20_REQUERIDAS.items():
                idx = next((headers[n] for n in nombres if n in headers), None)
                if idx is None and campo == "fecha":     # tolera «FECHA EJECUCION» y similares
                    idx = next((ix for h, ix in headers.items() if h.startswith("FECHA")), None)
                if idx is None:
                    break
                cols[campo] = idx
            else:
                for campo, nombres in _PROG20_OPCIONALES.items():
                    idx = next((headers[n] for n in nombres if n in headers), None)
                    if idx is not None:
                        cols[campo] = idx
                return ws, nfila, cols
    raise ValueError(
        "El archivo no parece la Programación Unificada de los 20': no encontré ninguna hoja con "
        "las columnas ZONA, FECHA, PROCESO, SUBPROCESO y LCL (hojas del archivo: "
        + ", ".join(f"«{n}»" for n in wb.sheetnames) + "). Debe ser el archivo con la hoja "
        "«P. UNIFICADA» (ZONA en A, SUBPROCESO en H). El formato viejo «Programación diaria "
        "ENEL_V1» ya no se acepta. No se tocó nada de lo cargado.")


def _parse_prog20(wb):
    """«Programación Unificada» (la de los 20') → (filas, reporte de lectura). Formato real
    (jul-2026): hoja «P. UNIFICADA» con ZONA(A), MES, DÍA, FECHA(D), LÍDER, TIPO GRUPO,
    PROCESO(G), SUBPROCESO(H), …, LCL(K), TIPO PDT, # PDT, ESTADO(N), …, MUNICIPIO, ALCANCE.
    Filas inválidas se DESCARTAN contadas en el reporte (no se insertan): zona irreconocible o
    sin fecha de ejecución → fuera (el PLANEADO se corta por mes justamente con esa fecha).
    Este archivo no trae VALOR MO/MATERIALES ni NODOS (eso era del formato viejo): quedan en
    0/None; si algún día los agregan, los encabezados opcionales los recogen solos.
    Corte tras 300 filas vacías seguidas: estos archivos declaran ~1M de filas por formato
    residual y sin el corte la subida se arrastra minutos."""
    ws, nfila, cols = _prog20_localizar_hoja(wb)

    def num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    filas, vacias = [], 0
    rep = {"hoja": ws.title, "leidas": 0, "sin_fecha": 0, "zona_desconocida": 0,
           "zonas_raras": Counter(), "sin_subproceso": 0}
    max_col = max(cols.values()) + 1
    for row in ws.iter_rows(min_row=nfila + 1, max_col=max_col, values_only=True):
        g = lambda campo: (row[cols[campo]] if campo in cols and len(row) > cols[campo] else None)
        zona_raw, sub, lcl = g("zona"), g("subproceso"), g("lcl")
        if zona_raw is None and sub is None and lcl is None and g("fecha") is None:
            vacias += 1
            if vacias > 300:
                break
            continue
        vacias = 0
        rep["leidas"] += 1
        z = _znorm(zona_raw)
        if not z:
            rep["zona_desconocida"] += 1
            rep["zonas_raras"][str(zona_raw)[:40]] += 1
            continue
        d = _fecha_celda(g("fecha"))
        if not d:
            rep["sin_fecha"] += 1
            continue
        sub_t = _txt(sub)
        if not sub_t:
            rep["sin_subproceso"] += 1       # la fila se queda (es real), solo no puntúa en 3.5
        lcl_t = _txt(lcl)
        if lcl_t and _norm35(lcl_t) in _LCL_PLACEHOLDER:
            lcl_t = None
        filas.append({
            "mes": f"{d.year:04d}-{d.month:02d}", "fecha_ejec": d.isoformat(),
            "lcl": (lcl_t or "")[:60] or None,
            "proceso": (_txt(g("proceso")) or "")[:120] or None,
            "subproceso": (sub_t or "")[:120] or None,
            "zona": z, "estado": (_txt(g("estado")) or "")[:60] or None,
            "valor_mo": num(g("valor_mo")), "valor_mat": num(g("valor_mat")),
            "nodos_prog": _txt(g("nodos_prog")), "cant_nodos": (_txt(g("cant_nodos")) or "")[:120] or None,
            "tipo_pdt": (_txt(g("tipo_pdt")) or "")[:40] or None, "pdt": (_txt(g("pdt")) or "")[:200] or None,
            "alcance": _txt(g("alcance")), "municipio": (_txt(g("municipio")) or "")[:120] or None,
            "jefe_cuadrilla": (_txt(g("lider")) or "")[:200] or None,
            "avance": (_txt(g("avance")) or "")[:200] or None,
        })
    rep["zonas_raras"] = dict(rep["zonas_raras"].most_common(5))
    return filas, rep


async def _subproc_map_dict(db) -> dict:
    rows = (await db.execute(select(AnsSubprocMap))).scalars().all()
    return {(r.subproceso or "").strip().upper(): r.proceso for r in rows}


async def _invr_ejecutado(db, periodo) -> dict:
    """INVR ejecutado = MO real + material de la Programación Arasaka del mes, por zona.
    (Confirmar con Sabogal que mo_real/valor_material son las columnas correctas.)"""
    y, mo = int(periodo[:4]), int(periodo[5:7])
    ini, fin = date(y, mo, 1), _fin_de_mes(periodo)
    rows = (await db.execute(
        select(Programacion).where(Programacion.fecha >= ini, Programacion.fecha <= fin)
    )).scalars().all()
    out = {z: 0.0 for z in ZONAS_REPORTE}
    for p in rows:
        z = _znorm(p.zona)
        if z in out:
            out[z] += (p.mo_real or 0) + (p.valor_material or 0)
    return out


async def _prog20_indicadores(db, periodo) -> dict:
    """3.5 desde la programación de los 20', con las FÓRMULAS del Excel oficial de Paola
    (hoja «3.5» de Calculo_ANS_*.xlsx, verificado fórmula a fórmula el 2026-07-30):
    · Cronograma = Enviaron/Deben (hoy Enviaron==Deben, pendiente Paola).
    · Efectividad = Ejecutaron/DEBEN topada al 100% — el denominador es Deben, NO Enviaron
      (Excel F4 = IF(D4/B4>1;100%;D4/B4); antes dividíamos por Enviaron y no cuadraba).
    · Deben==0 → 100% en todo (así lo fijó Paola a mano: CUN100 de julio, E7=F7=1).
    · El Total ANS incluye la fila OBRAS (la inversión, antes rotulada INV) en MILLONES
      ENTEROS de pesos — en el Excel INVR entra a B16/C16/D16 como un sumando más
      (p. ej. 324.179.673 → 324). Renombrada a OBRAS por pauta de ENEL (ago-2026): esa fila
      es la plata de las obras (subprocesos OBRAS SIMPLES y POSTE MADERA del mapeo).
    · Efectividad del TOTAL con tope POR CONCEPTO (revisión de Santana, 2026-07-31): se suma
      min(Ejecutaron, Deben) de cada concepto — el exceso de uno (PQR 26/22, TLC 27/0) NO
      compensa el faltante de otro (AMP 1/3). El Excel suma crudos (F16=SUM(D)/SUM(B)) y con
      datos así daría 100% aunque haya conceptos al 33% — Santana confirmó que no puede ser.
      Cuando ningún concepto se pasa (como en los datos de Paola), da IDÉNTICO al Excel.
    · Cumplimiento = 0.4·Cronograma + 0.6·Efectividad (celda B19 del Excel).
    PLANEADO = LCL únicos por zona × proceso (mapeo editable); EJECUTADO = bolsas Arasaka;
    OBRAS = plata del INVR: PLANEADO (solo las filas de la 20' relacionadas con OBRAS en el
    mapeo) vs EJECUTADO (Arasaka), en millones enteros.
    `por_zona` = el resumen H19:O27 del Excel: mismos 0.4/0.6 con los totales de cada zona."""
    mapa = await _subproc_map_dict(db)
    rows = (await db.execute(select(AnsProg20).where(AnsProg20.mes == periodo))).scalars().all()
    lcls = {p: {z: set() for z in ZONAS_REPORTE} for p in PROC35_ORDEN}
    invr_plan = {z: 0.0 for z in ZONAS_REPORTE}
    sin_mapa = set()      # subprocesos del mes SIN proceso 3.5 asignado (antes se perdían callados)
    for r in rows:
        z = r.zona if r.zona in ZONAS_REPORTE else None
        if not z:
            continue
        su = (r.subproceso or "").strip().upper()
        pm = mapa.get(su)
        if pm == "OBRAS":
            # La plata de la fila OBRAS (antes INV): SOLO las filas relacionadas con OBRAS
            # en el mapeo (OBRAS SIMPLES, POSTE MADERA) — pauta de ENEL, ago-2026.
            invr_plan[z] += (r.valor_mo or 0) + (r.valor_mat or 0)
        if pm in PROC35_ORDEN and r.lcl:
            lcls[pm][z].add(r.lcl)
        elif su and pm is None:
            sin_mapa.add(su)
    planeado = {p: {z: len(lcls[p][z]) for z in ZONAS_REPORTE} for p in PROC35_ORDEN}
    d35 = (await _bolsas_3x_modulo(db, periodo))["3.5"]
    ejec = {p: {z: d35.get(p, {}).get(z, {}).get("EJECUTADO", 0) for z in ZONAS_REPORTE}
            for p in PROC35_ORDEN}

    # INV en MILLONES ENTEROS (324.179.673 → 324): así entra al total como en el Excel
    inv_ejec_pesos = await _invr_ejecutado(db, periodo)
    MILL = 1_000_000
    inv_plan_m = {z: int(round(invr_plan[z] / MILL)) for z in ZONAS_REPORTE}
    inv_ejec_m = {z: int(round(inv_ejec_pesos[z] / MILL)) for z in ZONAS_REPORTE}

    def cumpl(deben, enviaron, ejecutaron):
        # Fórmulas del Excel (ver docstring): Deben==0 → 100%; Efectividad = Ejec/DEBEN tope 100%
        if not deben:
            return 1.0, 1.0, 1.0
        crono = min(enviaron / deben, 1.0)
        efec = min(ejecutaron / deben, 1.0)
        return crono, efec, 0.4 * crono + 0.6 * efec

    general = []
    td = te = tj = tj_cap = 0
    for p in PROC35_ORDEN:
        deben = sum(planeado[p].values())
        enviaron = deben                      # Deben == Enviaron por ahora (pendiente Paola)
        ejecutaron = sum(ejec[p].values())
        crono, efec, cp = cumpl(deben, enviaron, ejecutaron)
        general.append({"concepto": p, "deben": deben, "enviaron": enviaron,
                        "ejecutaron": ejecutaron, "cronograma": crono,
                        "efectividad": efec, "cumplimiento": cp})
        td += deben; te += enviaron; tj += ejecutaron
        tj_cap += min(ejecutaron, deben)      # tope por concepto (revisión Santana)
    # Fila OBRAS (inversión en millones, antes INV): un concepto más, sumando al total ANS
    inv_deben, inv_ej = sum(inv_plan_m.values()), sum(inv_ejec_m.values())
    crono, efec, cp = cumpl(inv_deben, inv_deben, inv_ej)
    general.append({"concepto": "OBRAS", "deben": inv_deben, "enviaron": inv_deben,
                    "ejecutaron": inv_ej, "cronograma": crono, "efectividad": efec,
                    "cumplimiento": cp})
    td += inv_deben; te += inv_deben; tj += inv_ej
    tj_cap += min(inv_ej, inv_deben)
    # Total ANS: cronograma igual que el Excel; efectividad con la suma TOPADA por concepto
    ct = 1.0 if td == 0 else min(te / td, 1.0)
    ef = 1.0 if td == 0 else min(tj_cap / td, 1.0)
    cpt = 0.4 * ct + 0.6 * ef

    # Resumen por zona (bloque H19:O27 del Excel): totales de la zona (procesos + INV) con
    # los mismos 0.4/0.6 y el mismo tope por concepto — la «tablita formulada» de Por zona
    por_zona = {}
    for z in ZONAS_REPORTE:
        dz = sum(planeado[p][z] for p in PROC35_ORDEN) + inv_plan_m[z]
        jz = sum(ejec[p][z] for p in PROC35_ORDEN) + inv_ejec_m[z]
        jz_cap = (sum(min(ejec[p][z], planeado[p][z]) for p in PROC35_ORDEN)
                  + min(inv_ejec_m[z], inv_plan_m[z]))
        cz = 1.0
        ez = 1.0 if dz == 0 else min(jz_cap / dz, 1.0)
        por_zona[z] = {"deben": dz, "ejecutaron": jz, "ejecutaron_cap": jz_cap,
                       "cronograma": cz, "efectividad": ez,
                       "cumplimiento": 0.4 * cz + 0.6 * ez}

    return {
        "planeado": planeado, "ejecutado": ejec, "general": general,
        "ans": {"deben": td, "enviaron": te, "ejecutaron": tj, "ejecutaron_cap": tj_cap,
                "cronograma": ct, "efectividad": ef, "cumplimiento": cpt},
        "invr": {"planeado": invr_plan, "ejecutado": inv_ejec_pesos},
        "inv_mill": {"planeado": inv_plan_m, "ejecutado": inv_ejec_m},
        "por_zona": por_zona,
        "procesos": PROC35_ORDEN, "zonas": ZONAS_REPORTE, "descuento": DESCUENTO_35,
        "sin_mapa": sorted(sin_mapa),
    }


async def _resultado_31(db, periodo) -> dict:
    """3.1 resultado: por concepto Cantidad total / Cumple (=ENTREGADO A ENEL) / No cumple /
    Cumplimiento (=Cumple/Total). Total POR CONCEPTO = PENDIENTE + ENTREGADO + PLANEADO (real).
    Total AGREGADO (fila "Total") = TOTAL_31_FIJO (35, estructural — ver comentario arriba), NO la
    suma de los conceptos: el cumplimiento del indicador se mide sobre ese universo fijo."""
    t31 = (await _bolsas_3x_modulo(db, periodo))["3.1"]
    filas, C = [], 0
    for concepto, zonas in t31.items():
        pend = sum(z.get("PENDIENTE BOLSA", 0) for z in zonas.values())
        entr = sum(z.get("ENTREGADO A ENEL", 0) for z in zonas.values())
        plan = sum(z.get("PLANEADO", 0) for z in zonas.values())
        total = pend + entr + plan
        filas.append({"concepto": concepto, "total": total, "cumple": entr,
                      "no_cumple": total - entr, "cumplimiento": (entr / total) if total else 0.0})
        C += entr
    T = TOTAL_31_FIJO
    return {"filas": filas, "total": T, "cumple": C, "no_cumple": T - C,
            "cumplimiento": (C / T) if T else 0.0, "descuento": DESCUENTO_31}


@router.post("/prog20")
async def subir_prog20(file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                       current_user: Usuario = Depends(get_current_active_user)):
    """Sube la «Programación Unificada» (la de los 20'). REEMPLAZA todo lo cargado del entorno
    y auto-siembra el mapeo de subprocesos nuevos con el TRADUCTOR (editable después).
    Blindada (2026-07-30, después de que un archivo con otra estructura entró sin queja, borró
    lo bueno y sembró números como subprocesos): la estructura se valida por ENCABEZADOS, y si
    no queda NI UNA fila válida se rechaza SIN tocar lo ya cargado. Además depura del mapeo la
    basura sin proceso que dejó algún archivo erróneo anterior."""
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="El archivo debe ser .xlsx")
    contents = await file.read()
    if len(contents) > 40 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El archivo pasa de 40 MB — no parece la programación de los 20'")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True, read_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el Excel: {e}")
    try:
        filas, rep = _parse_prog20(wb)
    except ValueError as e:                    # estructura ajena — mensaje ya explicativo
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:                     # noqa: BLE001 — cualquier otra cosa: fuera, sin tocar BD
        raise HTTPException(status_code=400, detail=f"No se pudo interpretar el archivo ({type(e).__name__}: {e})")
    finally:
        wb.close()

    if not filas:
        raise HTTPException(status_code=400, detail=(
            f"Leí la hoja «{rep['hoja']}» pero NINGUNA fila quedó válida (descartadas: "
            f"{rep['zona_desconocida']} por zona irreconocible, {rep['sin_fecha']} sin fecha de ejecución"
            + (f"; valores raros en ZONA: {', '.join(rep['zonas_raras'])}" if rep["zonas_raras"] else "")
            + "). No se tocó nada de lo cargado."))

    ent = current_entorno()
    await db.execute(delete(AnsProg20).where(AnsProg20.entorno == ent))
    for f in filas:
        db.add(AnsProg20(**f))

    # Siembra del mapeo (traductor + equivalencias). Números sueltos no son subprocesos: no se
    # siembran (blindaje anti-basura, aprendido del archivo erróneo).
    existentes = await _subproc_map_dict(db)
    subs_archivo, nuevos = set(), {}
    for f in filas:
        s = (f["subproceso"] or "").strip()
        if not s:
            continue
        su = s.upper()
        subs_archivo.add(su)
        if su in existentes or su in nuevos or re.fullmatch(r"[\d.,\s\-]+", s):
            continue
        nuevos[su] = _guess_proceso(s, f["proceso"])
        db.add(AnsSubprocMap(subproceso=s, proceso=nuevos[su]))
    mapa_final = {**existentes, **nuevos}
    sin_mapa = sorted(su for su in subs_archivo if mapa_final.get(su) is None)

    # Depura la basura que sembró el archivo erróneo (números como subprocesos — venían de la
    # hoja RESUMEN). OJO: un mapeo NULL puede ser una decisión A MANO («no cuenta», Paola) y el
    # modelo no distingue el origen — por eso SOLO cae lo inequívocamente basura: lo numérico.
    for m in (await db.execute(select(AnsSubprocMap))).scalars().all():
        if m.proceso is None and re.fullmatch(r"[\d.,\s\-]+", (m.subproceso or "").strip() or "x"):
            await db.delete(m)

    # Procedencia: guarda el archivo original para poder VERIFICAR el cruce (descargable).
    file_hash = hashlib.sha256(contents).hexdigest()
    await _guardar_fuente(db, date.today().strftime("%Y-%m"), "prog20", file.filename,
                          contents, len(filas), file_hash)
    await db.commit()
    meses = sorted({f["mes"] for f in filas if f["mes"]})
    return {"filas": len(filas), "meses": meses, "subprocesos_nuevos": len(nuevos),
            "archivo": file.filename, "hoja": rep["hoja"],
            "zonas": dict(Counter(f["zona"] for f in filas)),
            "descartadas": {"zona_desconocida": rep["zona_desconocida"], "sin_fecha": rep["sin_fecha"]},
            "zonas_raras": rep["zonas_raras"], "sin_subproceso": rep["sin_subproceso"],
            "sin_mapa_35": sin_mapa}


@router.get("/prog20/estado")
async def estado_prog20(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    filas = (await db.execute(select(func.count()).select_from(AnsProg20))).scalar() or 0
    meses = sorted({m for (m,) in (await db.execute(select(AnsProg20.mes).distinct())).all() if m})
    return {"filas": filas, "meses": meses}


@router.get("/prog20/archivos")
async def prog20_archivos(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    """Historial de 'Cronograma 20' subidos' (para verificar el cruce). El más reciente = actual;
    se descargan por GET /ans/fuentes/{id}. La DATA de 3.5 sale del último subido. Orden por
    creado_en (=última subida, se refresca en el upsert) para que «EN USO» no mienta al
    re-subir un nombre ya usado."""
    rows = (await db.execute(
        select(AnsFuente).where(AnsFuente.tipo == "prog20")
        .order_by(AnsFuente.creado_en.desc(), AnsFuente.id.desc())
    )).scalars().all()
    return [{"id": r.id, "nombre": r.nombre, "subido": r.periodo, "filas": r.registros,
             "tamano": r.tamano, "hash": (r.hash or "")[:12], "actual": i == 0}
            for i, r in enumerate(rows)]


# ══════════════════════════════════════════════════════════════════════════════
#  APARTADO NODOS (María Inés · 3.2 C) — cruce PDL ↔ su «Programación diaria ENEL»
#  y bandeja de aprobación CASO A CASO. La máquina cruza y PROPONE con evidencia
#  (las filas de su propio archivo); NUNCA escribe sola: al aprobar ella, se llenan
#  AV/AW/AY del registro con historial (AnsCambio) y procedencia (archivo prog20).
#  Su archivo es el MISMO de los 20' → una sola subida alimenta 3.5 y esta bandeja.
#  Gate: config.puede_nodos (dev + mariaines + cristian) — aparte de puede_ans.
# ══════════════════════════════════════════════════════════════════════════════

def _gate_nodos(user: Usuario):
    if not _puede_nodos(user.rol, user.email):
        raise HTTPException(status_code=403, detail="Sin acceso al apartado de Nodos")


def _cods_de(texto) -> set:
    """Códigos de orden dentro de un texto: la celda PDT trae a veces VARIOS pegados
    («CDOPP…+DF…;TK DF…») y con espacios invisibles. Token alfanumérico largo con
    suficientes dígitos = código (deja fuera N36, ORA, TK, ZONA…)."""
    if not texto:
        return set()
    s = unicodedata.normalize("NFKD", str(texto)).upper()
    return {t for t in re.split(r"[^A-Z0-9]+", s)
            if len(t) >= 8 and sum(ch.isdigit() for ch in t) >= 6}


def _n_lista(s) -> Optional[int]:
    """Cuenta nodos de una lista tipo «N36-N38-N42, N35-N37» → 5; None si no parece lista."""
    if not s:
        return None
    return len(re.findall(r"N\s*\d+", str(s).upper())) or None


def _n_cant(s) -> Optional[int]:
    """«9» / «9.0» → 9; None si no es una cantidad razonable (N/A, fechas, listas…)."""
    if s is None:
        return None
    try:
        f = float(str(s).strip().replace(",", "."))
    except ValueError:
        return None
    n = int(round(f))
    return n if 0 <= n <= 500 and abs(f - n) < 0.01 else None


def _nodos_de_fila(r: AnsProg20):
    """(cantidad, lista_para_mostrar, cómo_se_leyó). Tolera los líos reales del archivo:
    F y G intercambiadas, cantidad como lista, N/A."""
    cant = _n_cant(r.cant_nodos)
    if cant is not None:
        return cant, r.nodos_prog, "col G"
    cant_f = _n_cant(r.nodos_prog)
    if cant_f is not None:
        if _n_lista(r.cant_nodos):
            return cant_f, r.cant_nodos, "F y G venían intercambiadas"
        return cant_f, None, "la cantidad venía en la columna F"
    n = _n_lista(r.cant_nodos)
    if n:
        return n, r.cant_nodos, "contando los nodos escritos en la columna G"
    n = _n_lista(r.nodos_prog)
    if n:
        return n, r.nodos_prog, "contando los nodos escritos en la columna F"
    return None, r.nodos_prog, "sin cantidad legible (N/A)"


def _avance_num(s) -> Optional[float]:
    """Avance 20:30 → fracción 0..1: «1»→1.0, «0.8»→0.8, «50% cierre parcial»→0.5; None si texto."""
    if s is None:
        return None
    try:
        f = float(str(s).strip().replace("%", "").replace(",", "."))
    except ValueError:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", str(s))
        if not m:
            return None
        f = float(m.group(1).replace(",", "."))
    return f / 100.0 if f > 1 else f


async def _sync_casos_nodos(db: AsyncSession, periodo: str):
    """Upsert de la bandeja: un caso por fila ANS del periodo, con el cruce recalculado
    contra el ÚLTIMO archivo prog20 cargado. No pisa lo decidido: un caso aprobado cuyo
    dato cambió al re-subir el archivo pasa a 'revisar' (nunca se sobreescribe solo)."""
    regs = (await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo))).scalars().all()
    if not regs:
        return
    prows = (await db.execute(select(AnsProg20).where(func.coalesce(AnsProg20.pdt, "") != ""))).scalars().all()
    idx = defaultdict(list)
    for r in prows:
        for c in _cods_de(r.pdt):
            idx[c].append(r)
    fuente = (await db.execute(select(AnsFuente).where(AnsFuente.tipo == "prog20")
                               .order_by(AnsFuente.id.desc()))).scalars().first()
    casos = {c.registro_id: c for c in (await db.execute(
        select(AnsNodosCaso).where(AnsNodosCaso.periodo == periodo))).scalars().all()}

    for reg in regs:
        # Barrido de las reglas de nodos: cada apertura de la bandeja revalida las marcas
        # (aplica pendientes retroactivas y pone en rojo las que perdieron su condición).
        _log_reglas_nodos(db, reg, _eval_reglas_nodos(reg))
        cods = _cods_de(reg.pdl) | _cods_de(reg.protocolo)
        vistos, matches = set(), []
        for c in sorted(cods):
            for r in idx.get(c, ()):
                if r.id not in vistos:
                    vistos.add(r.id)
                    matches.append(r)
        matches.sort(key=lambda r: (r.fecha_ejec or ""))

        ev, cants, avances, lios = [], [], [], set()
        multi_cod = False
        for r in matches:
            n, lista, como = _nodos_de_fila(r)
            solo_este = len(_cods_de(r.pdt)) == 1
            multi_cod = multi_cod or not solo_este
            if n is not None:
                cants.append(n)
                if como != "col G":
                    lios.add(como)
            a = _avance_num(r.avance)
            if a is not None:
                avances.append(a)
            ev.append({"fecha": r.fecha_ejec, "pdt": r.pdt, "tipo_pdt": r.tipo_pdt,
                       "nodos": lista or r.nodos_prog, "cant": r.cant_nodos, "cant_num": n,
                       "como": como, "avance": r.avance, "municipio": r.municipio,
                       "jefe": r.jefe_cuadrilla, "alcance": (r.alcance or "")[:200] or None,
                       "zona": r.zona, "exacto": solo_este})

        sug_plan = max(cants) if cants else None
        ejecutado = any(a >= 0.999 for a in avances)
        sug_ejec = sug_plan if (sug_plan is not None and ejecutado) else None

        partes = []
        if not matches:
            confianza = "sin_cruce"
            partes.append("El PDL no aparece en la columna PDT de tu archivo — llénalo a mano como siempre.")
        else:
            fechas = [e["fecha"][:10] for e in ev if e["fecha"]]
            partes.append(f"Cruzó con {len(matches)} fila(s) de tu archivo"
                          + (f" ({', '.join(fechas[:6])})" if fechas else "") + ".")
            if multi_cod:
                partes.append("Ojo: el código venía acompañado de otros en la misma celda PDT.")
            if len(set(cants)) > 1:
                partes.append(f"Cantidades distintas entre fechas ({', '.join(map(str, sorted(set(cants))))}): "
                              "se propone la MAYOR — ajústala tú a la realidad.")
            for lio in sorted(lios):
                partes.append(f"La cantidad se leyó {lio}.")
            if sug_plan is None:
                confianza = "revisar"
                partes.append("Tu archivo trae la fila pero sin cantidad legible (N/A): escribe tú los nodos.")
            else:
                if ejecutado:
                    partes.append("El avance de las 20:30 llegó a 1 → se propone ejecutados = planeados.")
                else:
                    partes.append("El avance no llegó a 1 (o no se pudo leer): los ejecutados quedan para ti.")
                limpio = len(matches) == 1 and not multi_cod and not lios and len(set(cants)) == 1
                confianza = "alta" if limpio else "revisar"

        h = hashlib.sha256(json.dumps({"ev": ev, "p": sug_plan, "e": sug_ejec},
                                      sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        caso = casos.get(reg.id)
        if caso is None:
            caso = AnsNodosCaso(periodo=periodo, registro_id=reg.id, estado="pendiente")
            db.add(caso)
            casos[reg.id] = caso
        if caso.estado == "aprobado" and caso.hash_evidencia and caso.hash_evidencia != h:
            caso.estado = "revisar"    # el archivo cambió lo que respaldaba la aprobación
        # Si AV y AW ya están llenos (p. ej. diligenciados en la grilla), el caso no debe
        # estorbar en la bandeja de pendientes: cuenta como resuelto.
        if caso.estado == "pendiente" and reg.nodos_planeados is not None and reg.nodos_ejecutados is not None:
            caso.estado, caso.aprobado_por = "aprobado", caso.aprobado_por or "(grilla)"
        caso.confianza, caso.motivo = confianza, " ".join(partes)
        caso.sugerido_planeados, caso.sugerido_ejecutados = sug_plan, sug_ejec
        caso.evidencia, caso.hash_evidencia = json.dumps(ev, ensure_ascii=False, default=str), h
        caso.fuente_id = fuente.id if fuente else None
    await db.commit()


def _caso_dict(c: AnsNodosCaso, reg: AnsRegistro) -> dict:
    return {
        "id": c.id, "registro_id": c.registro_id, "estado": c.estado, "confianza": c.confianza,
        "motivo": c.motivo, "sugerido_planeados": c.sugerido_planeados,
        "sugerido_ejecutados": c.sugerido_ejecutados,
        "evidencia": json.loads(c.evidencia) if c.evidencia else [],
        "aprobado_por": c.aprobado_por,
        "aprobado_en": _bog(c.aprobado_en).isoformat() if c.aprobado_en else None,
        "reg": {"id": reg.id, "pdl": reg.pdl, "protocolo": reg.protocolo, "tipo": reg.tipo,
                "zona": reg.zona, "fecha_inicio": reg.fecha_inicio, "descripcion": reg.descripcion,
                "estado_actual": reg.estado_actual, "nombre_responsable": reg.nombre_responsable,
                "nodos_planeados": reg.nodos_planeados, "nodos_ejecutados": reg.nodos_ejecutados,
                "nods_excluible": reg.nods_excluible, "obs_nodos": reg.obs_nodos,
                "nodos_auto": json.loads(reg.nodos_auto) if reg.nodos_auto else None},
    }


@router.get("/nodos/casos")
async def nodos_casos(periodo: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                      db: AsyncSession = Depends(get_db),
                      current_user: Usuario = Depends(get_current_active_user)):
    """La bandeja del periodo: sincroniza el cruce (barato, cientos de filas) y devuelve
    todos los casos con su evidencia + el archivo del cruce."""
    _gate_nodos(current_user)
    await _sync_casos_nodos(db, periodo)
    regs = {r.id: r for r in (await db.execute(
        select(AnsRegistro).where(AnsRegistro.periodo == periodo))).scalars().all()}
    casos = (await db.execute(select(AnsNodosCaso).where(AnsNodosCaso.periodo == periodo))).scalars().all()
    fuente = (await db.execute(select(AnsFuente).where(AnsFuente.tipo == "prog20")
                               .order_by(AnsFuente.id.desc()))).scalars().first()
    out = [_caso_dict(c, regs[c.registro_id]) for c in casos if c.registro_id in regs]
    resumen = defaultdict(int)
    for c in out:
        resumen[c["estado"]] += 1
    return {"casos": out,
            "resumen": dict(resumen),
            "fuente": {"nombre": fuente.nombre, "filas": fuente.registros,
                       "subido": _bog(fuente.creado_en).isoformat() if fuente.creado_en else None,
                       "id": fuente.id} if fuente else None}


@router.post("/nodos/casos/{caso_id}/aprobar")
async def nodos_aprobar(caso_id: int, datos: dict = Body(...),
                        db: AsyncSession = Depends(get_db),
                        current_user: Usuario = Depends(get_current_active_user)):
    """Aprueba UN caso: escribe AV/AW (+ observación AY y exclusión AX si vienen) en el
    registro ANS con historial AnsCambio — la única vía por la que este apartado escribe."""
    _gate_nodos(current_user)
    caso = (await db.execute(select(AnsNodosCaso).where(AnsNodosCaso.id == caso_id))).scalar_one_or_none()
    if not caso:
        raise HTTPException(status_code=404, detail="Caso no encontrado")
    reg = (await db.execute(select(AnsRegistro).where(AnsRegistro.id == caso.registro_id))).scalar_one_or_none()
    if not reg:
        raise HTTPException(status_code=404, detail="Registro ANS no encontrado")

    campos = {}
    for k in ("nodos_planeados", "nodos_ejecutados"):
        if k in datos:
            v = datos[k]
            if v in (None, ""):
                campos[k] = None
            else:
                try:
                    campos[k] = int(v)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"{k} debe ser un número entero")
    for k in ("obs_nodos", "nods_excluible"):
        if k in datos:
            campos[k] = _txt(datos[k])
    if campos.get("nodos_planeados") is None and campos.get("nodos_ejecutados") is None:
        raise HTTPException(status_code=400, detail="Escribe al menos los nodos planeados o los ejecutados")

    for k, v in campos.items():
        old = getattr(reg, k)
        if _s(old) == _s(v):
            continue
        setattr(reg, k, v)
        db.add(AnsCambio(registro_id=reg.id, periodo=reg.periodo, protocolo=reg.protocolo,
                         campo=k, valor_anterior=_s(old), valor_nuevo=_s(v), tipo="edicion",
                         usuario_id=current_user.id, usuario_nombre=current_user.nombre))
    # María Inés llena a su disposición; las reglas de nodos ajustan encima si aplican
    # (anulada excluida → AV=0; cerrada sin exclusión con AW<AV → AW=AV) y pintan la celda.
    _log_reglas_nodos(db, reg, _eval_reglas_nodos(reg))
    caso.estado = "aprobado"
    caso.aprobado_por = current_user.nombre
    # Naive-UTC como func.now() de la BD: _bog le resta las 5h al mostrar. Con now() a secas
    # se restaría doble en cualquier host que no esté en UTC.
    caso.aprobado_en = datetime.now(_tz.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(caso)
    await db.refresh(reg)
    return _caso_dict(caso, reg)


@router.post("/nodos/casos/{caso_id}/estado")
async def nodos_estado_caso(caso_id: int, datos: dict = Body(...),
                            db: AsyncSession = Depends(get_db),
                            current_user: Usuario = Depends(get_current_active_user)):
    """Omitir un caso (o reabrirlo a pendiente) sin escribir nada en el registro."""
    _gate_nodos(current_user)
    estado = str(datos.get("estado") or "")
    if estado not in ("omitido", "pendiente"):
        raise HTTPException(status_code=400, detail="Estado inválido (omitido | pendiente)")
    caso = (await db.execute(select(AnsNodosCaso).where(AnsNodosCaso.id == caso_id))).scalar_one_or_none()
    if not caso:
        raise HTTPException(status_code=404, detail="Caso no encontrado")
    caso.estado = estado
    if estado == "pendiente":
        caso.aprobado_por, caso.aprobado_en = None, None
    await db.commit()
    reg = (await db.execute(select(AnsRegistro).where(AnsRegistro.id == caso.registro_id))).scalar_one()
    return _caso_dict(caso, reg)


@router.get("/subproc-map")
async def get_subproc_map(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    rows = (await db.execute(select(AnsSubprocMap).order_by(AnsSubprocMap.subproceso))).scalars().all()
    # OBRAS es elegible en el mapeo pero NO está en PROC35_ORDEN: no cuenta por LCL — es la
    # fila de inversión (plata). Relaciona OBRAS SIMPLES y POSTE MADERA (pauta ENEL ago-2026).
    return {"procesos": PROC35_ORDEN + ["OBRAS"],
            "items": [{"subproceso": r.subproceso, "proceso": r.proceso} for r in rows]}


@router.put("/subproc-map")
async def put_subproc_map(payload: list = Body(...), db: AsyncSession = Depends(get_db),
                          _: Usuario = Depends(get_current_active_user)):
    idx = {r.subproceso.strip().upper(): r
           for r in (await db.execute(select(AnsSubprocMap))).scalars().all()}
    for item in (payload or []):
        s = (item.get("subproceso") or "").strip()
        if not s:
            continue
        proc = item.get("proceso") or None
        ex = idx.get(s.upper())
        if ex:
            ex.proceso = proc
        else:
            db.add(AnsSubprocMap(subproceso=s, proceso=proc))
    await db.commit()
    return {"ok": True}


@router.get("/indicadores-plus")
async def indicadores_plus(periodo: str, db: AsyncSession = Depends(get_db),
                           _: Usuario = Depends(get_current_active_user)):
    """Tablas NUEVAS de 3.1 (resultado + naturaleza) y 3.5 (general + por zona con PLANEADO de la
    programación de los 20'). Separado de /indicadores para no tocar lo existente."""
    meses = sorted({m for (m,) in (await db.execute(select(AnsProg20.mes).distinct())).all() if m})
    return {"periodo": periodo,
            "3.1": await _resultado_31(db, periodo),
            "3.5": await _prog20_indicadores(db, periodo),
            "3.5_cruce": await _cruce_35(db, periodo),
            "prog20_meses": meses}


@router.get("/cruce35/detalle")
async def cruce35_detalle(periodo: str, proceso: str, zona: str, estado: Optional[str] = None,
                          db: AsyncSession = Depends(get_db),
                          _: Usuario = Depends(get_current_active_user)):
    """Detalle del cruce 3.5 bolsa vs programación: las bolsas de ese proceso/zona con su
    antecedente y si están o no en la programación del mes (y bajo qué subproceso/zona)."""
    d = (await _cruce_35(db, periodo)).get("detalle", {})
    crudas = d.get(proceso, {}).get(zona, [])
    if estado in ("programada", "sin_programar"):
        crudas = [f for f in crudas if f["estado"] == estado]
    filas = []
    for f in crudas:
        if f["estado"] == "programada":
            donde = ", ".join(f["prog_subprocesos"]) or "programación"
            zp = ", ".join(f["prog_zonas"])
            en_prog = f"Sí — en «{donde}»" + (f" (zona {zp})" if zp and zona not in f["prog_zonas"] else "")
        else:
            en_prog = "No aparece en la programación del mes"
        filas.append({"Caso (bolsa)": f["caso"], "Llave de cruce": f["llave"],
                      "Municipio": f["municipio"], "¿En la programación?": en_prog})
    return {
        "titulo": f"3.5 bolsa vs programación · {proceso} · {zona}",
        "explicacion": ("Cada caso de bolsa de este proceso y zona, y si su identificador aparece en la "
                        "programación del mes. Cruza «Sí» aunque en la programación esté bajo otro "
                        "subproceso: el enlace es por el identificador (LCL, número de antecedente, orden "
                        "de trabajo o id de tutela, según el proceso), no por el nombre del subproceso."),
        "columnas": ["Caso (bolsa)", "Llave de cruce", "Municipio", "¿En la programación?"],
        "filas": filas, "total": len(filas),
        "fuente": {"tipo": "modulo", "label": "Módulo Bolsas", "ruta": "/bolsas"},
    }


@router.get("/prog20/detalle")
async def prog20_detalle(periodo: str, proceso: Optional[str] = None, zona: Optional[str] = None,
                         modo: str = "lcl", subproceso: Optional[str] = None,
                         db: AsyncSession = Depends(get_db),
                         _: Usuario = Depends(get_current_active_user)):
    """Justifica una celda de las tablas NUEVAS de 3.5 (clickeable → verificable). modo:
    'lcl' = PLANEADO (LCL únicos de la 20'); 'invr' = INVR planeado (filas MO+MAT de la 20');
    'invr_ejec' = INVR ejecutado (trabajos de la programación Arasaka con MO real + material);
    'sub' = las filas del archivo con UN subproceso (el aviso «sin proceso 3.5» es clicable →
    aquí se ven exactamente qué casos son antes de decidir si se mapea o se deja sin contar)."""
    if modo == "invr_ejec":
        y, mo = int(periodo[:4]), int(periodo[5:7])
        ini, fin = date(y, mo, 1), _fin_de_mes(periodo)
        rows = (await db.execute(select(Programacion).where(
            Programacion.fecha >= ini, Programacion.fecha <= fin))).scalars().all()
        filas = []
        for p in rows:
            z = _znorm(p.zona)
            if zona and z != zona:
                continue
            mo_r, mat = p.mo_real or 0, p.valor_material or 0
            if not mo_r and not mat:
                continue
            filas.append({"fecha": str(p.fecha), "proceso": p.proceso or "", "lider": p.lider or "",
                          "lcl": p.lcl or "", "MO real ($)": round(mo_r), "Materiales ($)": round(mat)})
        tot = sum(f["MO real ($)"] + f["Materiales ($)"] for f in filas)
        return {"titulo": f"OBRAS — inversión ejecutada · {zona or 'todas'}",
                "explicacion": ("El sistema suma, de la programación de Arasaka, la mano de obra (MO) real "
                                "más los materiales de todos los trabajos ejecutados dentro del mes en esta "
                                "zona. Cada fila de abajo aporta su MO real + materiales al total."),
                "fuente": FUENTE_PROG,
                "nota": f"{len(filas)} trabajos · el total de la celda es la suma de MO real + Materiales = ${tot:,.0f}.",
                "columnas": ["fecha", "proceso", "lider", "lcl", "MO real ($)", "Materiales ($)"],
                "filas": filas, "total": len(filas)}

    mapa = await _subproc_map_dict(db)
    rows = (await db.execute(select(AnsProg20).where(AnsProg20.mes == periodo))).scalars().all()
    if modo == "sub":
        su = (subproceso or "").strip().upper()
        filas, lcls_unicas = [], set()
        for r in rows:
            if (r.subproceso or "").strip().upper() != su:
                continue
            if r.lcl:
                lcls_unicas.add(r.lcl)
            filas.append({"fecha_ejec": r.fecha_ejec or "", "zona": r.zona or "", "lcl": r.lcl or "—",
                          "proceso (archivo)": r.proceso or "", "municipio": r.municipio or "",
                          "lider": r.jefe_cuadrilla or "", "estado": r.estado or ""})
        filas.sort(key=lambda f: (f["zona"], f["fecha_ejec"], f["lcl"]))
        pm = mapa.get(su)
        if pm == "NO_CUENTA":                # decisión explícita: fuera del 3.5 y sin aviso
            expl = (f"Estas son las filas del archivo de los 20’ con el subproceso «{subproceso}». "
                    "Está marcado a mano como «NO cuenta en el 3.5» — decisión explícita, por eso "
                    "no aparece en el aviso de huérfanos. Se corrige en «Mapeo subprocesos».")
        elif pm == "OBRAS":
            expl = (f"Estas son las filas del archivo de los 20’ con el subproceso «{subproceso}». "
                    "Está relacionado con OBRAS: no cuenta por LCL — su plata (VALOR MO + VALOR "
                    "MATERIALES) alimenta la fila OBRAS (inversión) del 3.5.")
        elif pm:
            expl = (f"Estas son las filas del archivo de los 20’ con el subproceso «{subproceso}». "
                    f"Hoy está relacionado con el proceso {pm}: sus LCL únicas cuentan en ese PLANEADO.")
        else:
            expl = (f"Estas son las filas del archivo de los 20’ con el subproceso «{subproceso}». "
                    "Hoy NO cuenta en el PLANEADO del 3.5 (sin relación asignada). Si debe contar, "
                    "relaciónalo con un proceso; si no debe contar, márcalo como «No cuenta».")
        return {"titulo": f"Subproceso «{subproceso}» · {len(filas)} filas del mes",
                "explicacion": expl,
                "fuente": await _fuente_prog20(db),
                "nota": (f"{len(lcls_unicas)} LCL únicas en estas filas — si se mapea a un proceso, "
                         f"aportarían eso al PLANEADO (cada LCL cuenta una sola vez)."),
                "columnas": ["fecha_ejec", "zona", "lcl", "proceso (archivo)", "municipio", "lider", "estado"],
                "filas": filas, "total": len(filas)}
    if modo == "invr":
        filas = []
        for r in rows:
            if zona and r.zona != zona:
                continue
            if mapa.get((r.subproceso or "").strip().upper()) != "OBRAS":
                continue          # la fila OBRAS suma SOLO lo relacionado con OBRAS en el mapeo
            if not (r.valor_mo or r.valor_mat):
                continue
            filas.append({"lcl": r.lcl or "", "subproceso": r.subproceso or "",
                          "MO ($)": round(r.valor_mo or 0), "Materiales ($)": round(r.valor_mat or 0)})
        tot = sum(f["MO ($)"] + f["Materiales ($)"] for f in filas)
        return {"titulo": f"OBRAS — inversión planeada (20') · {zona or 'todas'}",
                "explicacion": ("El sistema suma, de la programación de los 20’, la mano de obra (columna "
                                "VALOR MO) más los materiales (columna VALOR MATERIALES) de las filas de "
                                "esta zona relacionadas con OBRAS en el mapeo (OBRAS SIMPLES, POSTE "
                                "MADERA). Cada fila de abajo aporta su MO + materiales al total."),
                "fuente": await _fuente_prog20(db),
                "nota": f"{len(filas)} filas · el total de la celda es la suma de MO + Materiales = ${tot:,.0f}.",
                "columnas": ["lcl", "subproceso", "MO ($)", "Materiales ($)"],
                "filas": filas, "total": len(filas)}
    # modo 'lcl' → PLANEADO: LCL únicos (dedup), con cuántas veces apareció cada uno
    grupos = {}
    for r in rows:
        if zona and r.zona != zona:
            continue
        pm = mapa.get((r.subproceso or "").strip().upper())
        if pm not in PROC35_ORDEN or (proceso and pm != proceso) or not r.lcl:
            continue
        g = grupos.setdefault(r.lcl, {"lcl": r.lcl, "subproceso": r.subproceso or "", "zona": r.zona or "",
                                      "proceso": pm, "fecha_ejec": r.fecha_ejec or "", "estado": r.estado or "",
                                      "veces": 0})
        g["veces"] += 1
    filas = sorted(grupos.values(), key=lambda x: x["lcl"] or "")
    filas2 = []
    for gr in filas:
        v = gr["veces"]
        pq = (f"Subproceso «{gr['subproceso']}» → cuenta como {gr['proceso']}. "
              + (f"La LCL aparece {v} veces en el archivo, pero se cuenta 1 sola vez."
                 if v > 1 else "Aparece 1 vez en el archivo."))
        filas2.append({"lcl": gr["lcl"], "subproceso": gr["subproceso"],
                       "fecha_ejec": gr["fecha_ejec"], "por qué": pq})
    return {"titulo": f"PLANEADO 3.5 · {proceso or 'todos'} · {zona or 'todas'}",
            "explicacion": ("El sistema toma la programación de los 20’ (archivo de ENEL) y cuenta cuántas "
                            "LCL DISTINTAS hay para este proceso y zona en el mes. Cada LCL cuenta UNA vez "
                            "aunque aparezca en varias filas del archivo. El proceso (PQR/AMP/IPPE…) se "
                            "decide cruzando el «subproceso» de cada fila con el mapeo configurable."),
            "fuente": await _fuente_prog20(db),
            "columnas": ["lcl", "subproceso", "fecha_ejec", "por qué"],
            "filas": filas2, "total": len(filas2)}


# Campo computado de cada indicador de programación (para el drill-down)
_CAMPO_DETALLE = {
    "3.4": "cumple_tprog", "3.3": "cumple_uucc",
    "3.2A": "cumple_tiempos", "3.2B": "cumple_apertura", "DINAMICA": "cumple_tiempos",
}
# Campo de exclusión (y su observación) de cada indicador — para el bucket EXCLUSION
_EXCL_DETALLE = {
    "3.2A": ("excluible", "obs"), "3.2B": ("excluible_anulado", "obs_anulado"),
    "3.2C": ("nods_excluible", "obs_nodos"), "3.3": ("excluible2", "obs3"),
    "3.4": ("excluible_tiempos", "obs_tiempos"),
}


@router.get("/detalle")
async def detalle(
    periodo: str, grupo: str,
    concepto: Optional[str] = None, zona: Optional[str] = None,
    bucket: Optional[str] = None, supervisor: Optional[str] = None,
    lider: Optional[str] = None, excluible: Optional[str] = None,
    db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user),
):
    """Devuelve las filas que JUSTIFICAN una celda de indicador (drill-down).
    grupo: 3.1/3.5 (casos de bolsa), 3.5P (filas de programación), 3.2A/3.2B/3.2C/3.3/3.4/DINAMICA
    (registros STM) o NCT (pivote «No cumple tiempos» zona→supervisor→líder).
    bucket=EXCLUSION lista las órdenes marcadas excluibles del indicador."""
    g = grupo.upper()
    # ── 3.5 según programación: filas EJECUTADO del mes (solicitud #22) ──
    if g == "3.5P":
        filas = (await _prog_35(db, periodo))["filas"]
        filas = [f for f in filas
                 if (not concepto or f["proceso"] == concepto) and (not zona or f["zona"] == zona)]
        return {"titulo": " · ".join(x for x in [grupo, concepto, zona, "EJECUTADO"] if x),
                "explicacion": ("El sistema cuenta los trabajos de la programación de Arasaka (unidad "
                                "SABOGAL) del mes cuyo estado quedó marcado como ejecutado (columna # PDT). "
                                "Cada trabajo (# PDT) cuenta una sola vez aunque aparezca en varios días — "
                                "las repeticiones van marcadas en «duplicado»."),
                "fuente": FUENTE_PROG,
                "nota": "Depende de que la programadora ponga el estado # PDT; si queda vacío, no aparece aquí.",
                "columnas": ["fecha", "zona", "proceso", "num_pdt", "lider", "municipio", "supervisor", "duplicado"],
                "filas": filas, "total": len(filas)}
    # ── Bolsas (3.1 / 3.5): filtra los casos del módulo Bolsas ──
    if g in ("3.1", "3.5"):
        casos = (await _bolsas_3x_modulo(db, periodo)).get("casos", [])
        col_fecha = "fecha de envío" if g == "3.1" else "fecha de ejecución"
        bkey = "b31" if g == "3.1" else "b35"
        filas = []
        omitidos = 0
        for c in casos:
            if g == "3.1":
                if concepto and c.get("concepto") != concepto:
                    continue
                if zona and c.get("zona") != zona:
                    continue
            else:  # 3.5
                if concepto and c.get("proceso") != concepto:
                    continue
                if zona and c.get("zona35") != zona:
                    continue
            bk = c.get(bkey)
            if bucket:
                if bk != bucket:
                    continue
            elif bk in (None, "—"):
                omitidos += 1        # sin bucket (Total): los que NO cuentan este mes se omiten (y se avisa)
                continue
            fv = (c.get("fecha_envio") if g == "3.1" else c.get("fecha")) or "(sin fecha)"
            if g == "3.1":
                if bk == "PENDIENTE BOLSA":
                    pq = "La fecha de envío del presupuesto está vacía → PENDIENTE"
                elif bk == "ENTREGADO A ENEL":
                    pq = f"La fecha de envío ({fv}) ya ocurrió dentro del mes → ENTREGADO A ENEL"
                elif bk == "PLANEADO":
                    pq = f"La fecha de envío ({fv}) es futura → PLANEADO"
                else:
                    pq = ""
            else:
                pq = f"La fecha de ejecución ({fv}) cae dentro del mes medido → EJECUTADO"
            filas.append({"id": c.get("id"), "lcl": c.get("lcl"), "estado": c.get("estado"),
                          col_fecha: fv, "resultado": bk, "por qué": pq})
        explic = ("El sistema revisa cada caso del módulo Bolsas y lee su FECHA DE ENVÍO del presupuesto "
                  "a ENEL. Regla: sin fecha → PENDIENTE (aún no se envía); fecha ya ocurrida dentro del mes "
                  "→ ENTREGADO A ENEL; fecha futura → PLANEADO. Solo cuenta la fecha; el estado del caso no "
                  "cambia la clasificación." if g == "3.1" else
                  "El sistema revisa cada caso del módulo Bolsas y lee su FECHA DE EJECUCIÓN. Si esa fecha "
                  "cae dentro del mes medido, el caso cuenta como EJECUTADO. Fechas de otros meses o vacías "
                  "no cuentan aquí.")
        out = {"titulo": " · ".join(x for x in [grupo, concepto, zona, bucket] if x),
               "explicacion": explic, "fuente": FUENTE_BOLSAS,
               "columnas": ["id", "lcl", "estado", col_fecha, "resultado", "por qué"],
               "filas": filas, "total": len(filas)}
        if omitidos:
            out["nota"] = (f"No se listan {omitidos} caso(s) con fecha de un mes anterior (ya se reportaron "
                           f"en su mes) o con fecha inválida (ver «Datos por revisar»): no cuentan para {periodo}.")
        return out

    # ── Programación (3.2/3.3/3.4/Dinamica/NCT): filtra los registros STM ──
    regs = (await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo))).scalars().all()
    campo = _CAMPO_DETALLE.get(g)
    es_exclusion = bool(bucket) and bucket.strip().upper().startswith("EXCLUSION")
    filas = []
    for r in regs:
        z = _zona_reg(r)
        if zona and z != zona:
            continue
        if supervisor and (r.nombre_responsable or "—") != supervisor:
            continue
        c = computar(r)
        if g == "NCT":
            # Dinámicas de incumplimiento: caso A (tiempos), B (apertura) o C (nodos),
            # elegido con `concepto`. Incluye las excluidas (el filtro Excluible separa).
            caso = (concepto or "A").strip().upper()
            if caso == "B":
                if (r.se_ejecuto or "").strip().upper() != "NO":
                    continue
                excl_campo = "excluible_anulado"
                pq = f"No se ejecutó ({(r.estado_actual or 'anulada').strip()})"
            elif caso == "C":
                _np, _ne = (r.nodos_planeados or 0), (r.nodos_ejecutados or 0)
                if _np <= _ne:
                    continue
                excl_campo = "nods_excluible"
                pq = f"Ejecutó {_ne} de {_np} nodos planeados (faltaron {_np - _ne})"
            else:   # A — falla cruda de tiempos (apertura o cierre tarde)
                if c["apoyo_pdl"] or "NO CUMPLE" not in (c["cumple_apertura"], c["cumple_cierre"]):
                    continue
                excl_campo = "excluible"
                partes = []
                if c["cumple_apertura"] == "NO CUMPLE":
                    partes.append(f"apertura tarde ({c['desfase_apertura']} min de desfase)")
                if c["cumple_cierre"] == "NO CUMPLE":
                    partes.append(f"cierre tarde ({c['desfase_cierre']} min de desfase)")
                pq = " y ".join(partes).capitalize()
            lid = re.sub(r"\s+", " ", str(r.ri_jt or "")).strip() or "—"
            if lider and lid != lider:
                continue
            exc = "SI" if (getattr(r, excl_campo, "") or "").upper() == "SI" else "NO"
            if excluible and exc != excluible.strip().upper():
                continue
            filas.append({"protocolo": r.protocolo, "responsable": r.nombre_responsable or "—",
                          "líder (RI)": lid, "fecha": r.fecha_inicio or "—", "excluible": exc,
                          "resultado": "NO CUMPLE", "por qué": pq})
            continue
        if es_exclusion and g in _EXCL_DETALLE:
            # Celda «Exclusiones»: órdenes marcadas excluibles para ESTE indicador
            campo_exc, campo_obs = _EXCL_DETALLE[g]
            if (getattr(r, campo_exc, "") or "").upper() != "SI":
                continue
            razon = (getattr(r, campo_obs, "") or "").strip()
            filas.append({"protocolo": r.protocolo, "responsable": r.nombre_responsable or "—",
                          "resultado": "EXCLUSIÓN",
                          "por qué": f"Marcada excluible (SI). Razón: {razon}" if razon
                                     else "Marcada excluible (SI) — sin razón escrita aún"})
            continue
        if g == "3.2C":   # nodos: registros con nodos planeados/ejecutados
            if not (r.nodos_planeados or r.nodos_ejecutados):
                continue
            val = f"{r.nodos_ejecutados or 0}/{r.nodos_planeados or 0}"
        elif g == "3.2B":   # efectividad (ejecución): se mira «Se ejecutó» (AN)
            if (r.excluible_anulado or "").strip().upper() == "SI":
                continue                      # esas se ven en la celda Exclusiones
            val = {"SI": "CUMPLE", "NO": "NO CUMPLE"}.get((r.se_ejecuto or "").strip().upper(), "N/A")
            if bucket and val != bucket.strip().upper():
                continue
        else:
            val = c.get(campo) if campo else ""
            if bucket and str(val).strip().upper() != bucket.strip().upper():
                continue
        vu = str(val).strip().upper()
        if g == "3.4":
            w, y = r.fecha_debio_validar or "—", r.fecha_envio_validar or "—"
            if vu == "CUMPLE":
                pq = f"Se envió a validar el {y} y el plazo era {w} → se envió a tiempo (al menos 1 día antes) → CUMPLE"
            elif vu == "NO CUMPLE":
                pq = f"Se envió a validar el {y} y el plazo era {w} → se envió tarde → NO CUMPLE"
            else:
                pq = "Sin fecha de envío a validar, o la orden está excluida → N/A"
        elif g == "3.2C":
            pq = f"Ejecutó {r.nodos_ejecutados or 0} de {r.nodos_planeados or 0} nodos planeados"
        elif g == "3.2B":
            se_txt = (r.se_ejecuto or "—").strip()
            if vu == "CUMPLE":
                pq = f"«Se ejecutó» = {se_txt} → la orden se ejecutó → CUMPLE"
            elif vu == "NO CUMPLE":
                pq = (f"«Se ejecutó» = {se_txt}: {(r.estado_actual or 'anulada').strip()} y la exclusión "
                      f"del 3.2 B no está aprobada → NO CUMPLE")
            else:
                pq = "«Se ejecutó» sin diligenciar o en N/A → no entra al conteo"
        else:
            pq = f"El sistema calculó «{val}» para esta orden a partir de los datos del CD10 STM"
        filas.append({"protocolo": r.protocolo, "responsable": r.nombre_responsable or "—",
                      "resultado": val, "por qué": pq})
    explic_stm = {
        "3.2A": ("3.2 A (Oportunidad en la ejecución): por cada orden de trabajo, el sistema revisa si los "
                 "tiempos de ejecución se cumplieron o no, a partir de las fechas/horas del CD10 STM."),
        "3.2B": ("3.2 B (Efectividad en la apertura/ejecución): por cada orden, el sistema mira la columna "
                 "«Se ejecutó» (AN): SI cumple; NO (anulada sin exclusión aprobada) no cumple; las "
                 "anuladas con la exclusión AO en SI van a la celda Exclusiones."),
        "3.2C": ("3.2 C (Alcance): por cada orden, el sistema compara los nodos que se ejecutaron contra "
                 "los que estaban planeados."),
        "3.3": ("3.3 (Calidad de la información): por cada orden, el sistema revisa si la información de "
                "UUCC quedó completa y correcta."),
        "3.4": ("3.4 (Oportunidad en la programación): por cada orden, el sistema compara la fecha en que se "
                "DEBÍA enviar a validación con la fecha en que realmente se envió. CUMPLE si se envió al "
                "menos 1 día antes del plazo; si no, NO CUMPLE."),
        "DINAMICA": ("Dinámica: agrupa las órdenes por zona y supervisor y cuenta cuántas cumplen, cuántas "
                     "son N/A y cuántas no cumplen los tiempos."),
        "NCT": {
            "A": ("Dinámica A — órdenes cuya apertura o cierre se hizo tarde (más de 10 min de desfase "
                  "contra lo planeado), con su equipo (supervisor + líder RI). Incluye también las que "
                  "luego se marcaron excluibles — usa el filtro Excluible para separarlas."),
            "B": ("Dinámica B — órdenes que NO se ejecutaron (columna «Se ejecutó» en NO, normalmente "
                  "anuladas por el Centro Operativo), con su equipo (supervisor + líder RI). Incluye "
                  "las excluibles — usa el filtro Excluible para separarlas."),
            "C": ("Dinámica C — órdenes que dejaron nodos planeados SIN ejecutar, con su equipo "
                  "(supervisor + líder RI) y cuántos nodos faltaron. Incluye las excluibles."),
        }.get((concepto or "A").strip().upper()),
    }
    if es_exclusion and g in _EXCL_DETALLE:
        explic_stm[g] = ("Órdenes marcadas como EXCLUIBLES (SI) para este indicador: no descuentan el "
                         "cumplimiento, pero deben tener su razón/soporte para sostenerse ante ENEL.")
    cols = ["protocolo", "responsable", "líder (RI)", "fecha", "excluible", "resultado", "por qué"] if g == "NCT" \
        else ["protocolo", "responsable", "resultado", "por qué"]
    return {"titulo": " · ".join(x for x in [(f"Dinámica {(concepto or 'A').upper()}" if g == "NCT" else grupo),
                                             zona, supervisor, lider,
                                             (f"Excluible: {excluible}" if excluible else None), bucket] if x),
            "explicacion": explic_stm.get(g, "Órdenes de trabajo (CD10 STM) que sustentan este indicador."),
            "fuente": await _fuente_stm(db, periodo),
            "columnas": cols,
            "filas": filas, "total": len(filas)}


# ── Export: inyecta sobre la plantilla real (formulaciones y diseño intactos) ────

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "templates", "plantilla_ans.xlsx")

# Fórmulas de las columnas calculadas (fila 4 de la tabla DATA). Se traducen por fila.
FORMULAS_DATA = {
    "A": '=+IFERROR(MID(C4,1,FIND("/",C4)-1),C4)',
    "B": '=+IFERROR(IFERROR(IF(FIND("PP2026R",A4),"FT","ERR FT"),IF(FIND("PT",A4),"ST","ERR ST")),"PI")',
    "U": '=+IF(AND(B4="FT",R4="Programada Sin Preaviso"),TRUE,IF(AND(B4="ST",R4="ACC"),TRUE,FALSE))',
    "V": '=+IF(AND(B4="ST",NOT(ISBLANK(D4))),TRUE,FALSE)',
    # X/Z con guarda de vacío: si no hay fecha de validación (celda vacía) → N/A
    # (antes una celda vacía se evaluaba como 0 y daba "NO CUMPLE" falso).
    "X": '=IF(N(W4)=0,"N/A",W4-1)',
    "Z": '=+IF(OR(N(Y4)=0,X4="N/A",AA4="SI"),"N/A",IF(Y4<=X4,"CUMPLE","NO CUMPLE"))',
    # AC/AG/AE/AI endurecidas: aritmética de fecha-hora REAL (J/K/L/M/S/T se inyectan
    # como tipos datetime), no TEXT()*1440 → cálculo correcto e idéntico en Excel/LibreOffice.
    "AC": '=IFERROR(J4+K4,"N/A")',
    "AD": '=+IF(ISBLANK(S4),"N/A",S4)',
    "AE": '=IFERROR(IF(U4=TRUE,"N/A",ROUND((AD4-AC4)*1440,0)),"N/A")',
    "AF": '=IF(DATA[[#This Row],[Desfase en apertura (minutos)]]="N/A","N/A",IF(DATA[[#This Row],[Desfase en apertura (minutos)]]<=10,"CUMPLE","NO CUMPLE"))',
    "AG": '=IFERROR(L4+M4,"N/A")',
    "AH": '=+IF(ISBLANK(T4),"N/A",T4)',
    "AI": '=IFERROR(IF(U4=TRUE,"N/A",ROUND((AH4-AG4)*1440,0)),"N/A")',
    "AJ": '=IF(DATA[[#This Row],[Desfase en cierre (minutos)]]="N/A","N/A",IF(DATA[[#This Row],[Desfase en cierre (minutos)]]<=10,"CUMPLE","NO CUMPLE"))',
    "AK": '=+IF(V4,"N/A",IF(AND(AF4="CUMPLE",AJ4="CUMPLE"),"CUMPLE",IF(AND(AF4="N/A",AJ4="CUMPLE"),"CUMPLE",IF(AND(AF4="CUMPLE",AJ4="N/A"),"CUMPLE",IF(AND(AF4="N/A",AJ4="N/A"),"N/A",IF(DATA[[#This Row],[Excluible?]]="SI","N/A","NO CUMPLE"))))))',
    # Ago-2026: «Nombre cumplimentador» entra en AQ y corre una letra todo lo que sigue
    # (Cumple UUCC AR→AS con su obs en AR, y la diferencia de nodos AY→AZ).
    "AS": '=+IF(T4=TRUE,"N/A",IF(AND(Q4="no",ISBLANK(AR4)),"N/A",IF(DATA[[#This Row],[Excluible2]]="SI","CUMPLE",IF(ISBLANK(AR4),"CUMPLE","NO CUMPLE"))))',
    "AZ": '=DATA[[#This Row],[Cantidad Nodos planeados]]-DATA[[#This Row],[Cantidad Nodos ejecutados]]',
}

# Columna de la hoja 'data' → atributo del registro (fuente del CD10).
DATA_SRC = {
    "C": "protocolo", "D": "asociado_pdl", "E": "ri_jt", "F": "zona", "G": "nombre_responsable",
    "H": "sigla_elaborador", "I": "descripcion", "J": "fecha_inicio", "K": "hora_inicio",
    "L": "fecha_fin", "M": "hora_fin", "N": "unidad_resp", "O": "elaborado_por",
    "P": "estado_actual", "Q": "modif_consistencia", "R": "causal",
    "S": "maniobra_inicio", "T": "maniobra_fin",
}
# Columna de la hoja 'data' → campo manual (lo rellena la programadora).
DATA_MAN = {
    "W": "fecha_debio_validar", "Y": "fecha_envio_validar", "AA": "excluible_tiempos",
    "AB": "obs_tiempos", "AL": "excluible", "AM": "obs", "AN": "se_ejecuto",
    "AO": "excluible_anulado", "AP": "obs_anulado", "AQ": "nombre_cumplimentador",
    "AR": "obs_uucc", "AT": "excluible2",
    "AU": "obs3", "AV": "nodos_planeados", "AW": "nodos_ejecutados", "AX": "nods_excluible",
    "AY": "obs_nodos",
}


def _as_date(v):
    d = _parse_dt(v)
    return d.date() if d else (v or None)


def _as_time(v):
    if isinstance(v, time):
        return v
    m = re.match(r"(\d{1,2}):(\d{2})", str(v or "").strip())
    return time(int(m.group(1)), int(m.group(2))) if m else (v or None)


def _as_dt(v):
    return _parse_dt(v) or (v if v not in ("", None) else None)


# Columnas fuente que se inyectan como tipo real (para que la aritmética de fechas
# de AC/AG/AE/AI sea correcta) + su formato de presentación.
_COERCE_DATA = {"J": _as_date, "L": _as_date, "K": _as_time, "M": _as_time, "S": _as_dt, "T": _as_dt}
_FMT_DATA = {
    "J": "DD/MM/YYYY", "L": "DD/MM/YYYY", "K": "HH:MM", "M": "HH:MM",
    "S": "DD/MM/YYYY HH:MM", "T": "DD/MM/YYYY HH:MM",
    "AC": "DD/MM/YYYY HH:MM", "AD": "DD/MM/YYYY HH:MM",
    "AG": "DD/MM/YYYY HH:MM", "AH": "DD/MM/YYYY HH:MM",
    "W": "DD/MM/YYYY", "X": "DD/MM/YYYY", "Y": "DD/MM/YYYY",
}
# Columnas manuales de tipo fecha (se inyectan como date real para que X=W-1 y Z funcionen).
_MAN_FECHAS = {"W", "Y"}


def _inyectar_data(ws, regs):
    """Reescribe la tabla DATA: columnas fuente + manuales como valores (fecha/hora
    con tipo real) y las calculadas con las fórmulas originales (traducidas por fila)."""
    start = 4
    n = len(regs)
    for i, r in enumerate(regs):
        row = start + i
        for col, attr in DATA_SRC.items():
            val = getattr(r, attr, None)
            if col in _COERCE_DATA and val not in (None, ""):
                val = _COERCE_DATA[col](val)
            ws[f"{col}{row}"] = val
        for col, attr in DATA_MAN.items():
            val = getattr(r, attr, None)
            if col in _MAN_FECHAS and val not in (None, ""):
                val = _as_date(val)
            ws[f"{col}{row}"] = val
        for col, f in FORMULAS_DATA.items():
            ws[f"{col}{row}"] = f if row == 4 else Translator(f, origin=f"{col}4").translate_formula(f"{col}{row}")
        for col, fmt in _FMT_DATA.items():
            c = ws[f"{col}{row}"]
            if not isinstance(c, MergedCell):
                c.number_format = fmt
    ws.tables["DATA"].ref = f"A3:AZ{start + max(n, 1) - 1}"


def _recalcular_xlsx(data: bytes) -> bytes:
    """Recalcula el .xlsx con LibreOffice headless para embeber los valores (que
    openpyxl no calcula). Si LibreOffice no está disponible, devuelve el archivo tal
    cual (Excel igualmente recalcula al abrir por fullCalcOnLoad)."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return data
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "calc.xlsx")
        outdir = os.path.join(d, "out")
        os.makedirs(outdir, exist_ok=True)
        with open(src, "wb") as f:
            f.write(data)
        try:
            subprocess.run(
                [soffice, "--headless", "--calc", "--convert-to",
                 "xlsx:Calc MS Excel 2007 XML", "--outdir", outdir, src],
                check=True, timeout=120,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "HOME": d},
            )
            rec = os.path.join(outdir, "calc.xlsx")
            if os.path.exists(rec):
                with open(rec, "rb") as f:
                    return f.read()
        except Exception:
            pass
    return data


def _set(ws, addr, val):
    """Escribe en una celda saltando las combinadas (read-only en openpyxl)."""
    if not isinstance(ws[addr], MergedCell):
        ws[addr] = val


def _inyectar_31(ws, t31):
    """Conteos de bolsas en el bloque CONCEPTO×ZONA de 3.1 (filas 4-8)."""
    zcols = {"UBATE": ("J", "K", "L"), "ALMEIDAS": ("M", "N", "O"), "GUAVIO": ("P", "Q", "R")}
    for i, concepto in enumerate(CONCEPTOS_31):
        row = 4 + i
        for z in ZONAS:
            d = t31.get(concepto, {}).get(z, {})
            cp, cpe, ce = zcols[z]
            _set(ws, f"{cp}{row}", d.get("PLANEADO", 0))
            _set(ws, f"{cpe}{row}", d.get("PENDIENTE BOLSA", 0))
            _set(ws, f"{ce}{row}", d.get("ENTREGADO A ENEL", 0))


def _inyectar_35(ws, t35):
    """Planeado/ejecutado por proceso×zona en 3.5 (filas 8-14)."""
    zcols = {"UBATE": ("I", "J"), "ALMEIDAS": ("K", "L"), "GUAVIO": ("M", "N"), "LV": ("O", "P")}
    base = {"PQR": 8, "AMP": 9, "INVR": 10, "IPPE": 11, "CUND 100": 12, "TLC": 13, "TUTELAS": 14}
    for proceso, row in base.items():
        for z in ZONAS_REPORTE:
            d = t35.get(proceso, {}).get(z, {})
            cp, ce = zcols[z]
            _set(ws, f"{cp}{row}", d.get("PLANEADO", 0))
            _set(ws, f"{ce}{row}", d.get("EJECUTADO", 0))


def _inyectar_dinamica(ws, filas):
    """Pivote Cumple-tiempos por zona/supervisor como valores planos."""
    for j, h in enumerate(["ZONA", "SUPERVISOR", "CUMPLE", "N/A", "NO CUMPLE", "TOTAL"], 1):
        ws.cell(row=3, column=j, value=h).font = Font(bold=True)
    for k, f in enumerate(filas):
        tot = f["CUMPLE"] + f["N/A"] + f["NO CUMPLE"]
        for j, v in enumerate([f["zona"], f["supervisor"], f["CUMPLE"], f["N/A"], f["NO CUMPLE"], tot], 1):
            ws.cell(row=4 + k, column=j, value=v)


# Hoja 3.6: celdas de entrada $ (las que antes venían del archivo externo de materiales).
# bloque "29" = 2.9 Mayor Impacto (cols Ubate/Choconta/Guavio/LV); "36" = 3.6 Programado
# (cols Ubate/Almeidas/Guavio/LV). Los totales y el indicador son fórmulas internas.
IMPACTO36_FILAS = ["Cumplen", "No cumplen", "Exclusiones", "En flujo"]
IMPACTO36_LAYOUT = {
    "29": {"filabase": 3, "zonas": {"Ubate": "B", "Choconta": "C", "Guavio": "D", "LV": "E"}},
    "36": {"filabase": 12, "zonas": {"Ubate": "B", "Almeidas": "C", "Guavio": "D", "LV": "E"}},
}


def _inyectar_36(ws, datos):
    """Rellena las celdas de entrada $ de 3.6 con los valores que el sistema guarda
    por periodo (AnsImpacto36). Si no hay datos, deja las celdas en blanco (sin la
    referencia externa rota). Los totales/indicador recalculan con sus fórmulas."""
    datos = datos or {}
    for bloque, lay in IMPACTO36_LAYOUT.items():
        bd = datos.get(bloque, {})
        for i, fila in enumerate(IMPACTO36_FILAS):
            row = lay["filabase"] + i
            for zona, col in lay["zonas"].items():
                v = (bd.get(fila, {}) or {}).get(zona)
                _set(ws, f"{col}{row}", v if v not in ("", None) else None)


def _build_export(regs, prog, bol, periodo, impacto36=None):
    """Inyecta los datos del periodo sobre la plantilla real: conserva fórmulas,
    estilos y todas las hojas-reporte. 3.2/3.3/3.4 recalculan solas desde DATA al
    abrir en Excel (fullCalcOnLoad)."""
    wb = openpyxl.load_workbook(TEMPLATE_PATH)
    _inyectar_data(wb["data"], regs)
    if "3.1" in wb.sheetnames:
        _inyectar_31(wb["3.1"], bol["3.1"])
    if "3.5" in wb.sheetnames:
        _inyectar_35(wb["3.5"], bol["3.5"])
    if "Dinamica" in wb.sheetnames:
        _inyectar_dinamica(wb["Dinamica"], prog["Dinamica"])
    if "3.6" in wb.sheetnames:
        _inyectar_36(wb["3.6"], impacto36)
    wb.calculation = CalcProperties(fullCalcOnLoad=True)
    return wb


async def _ans_xlsx_bytes(db: AsyncSession, periodo: str) -> Optional[bytes]:
    """Los bytes del Calculo_ANS_<periodo>.xlsx (con valores ya recalculados). None si no hay datos.
    Se reusa para el export normal y para meter el Excel dentro del ZIP de evidencias."""
    regs = (await db.execute(
        select(AnsRegistro).where(AnsRegistro.periodo == periodo).order_by(AnsRegistro.tipo, AnsRegistro.id)
    )).scalars().all()
    if not regs:
        return None
    prog = await _indicadores_programacion(db, periodo)
    bol = await _indicadores_bolsas(db, periodo)
    imp = (await db.execute(select(AnsImpacto36).where(AnsImpacto36.periodo == periodo))).scalar_one_or_none()
    impacto36 = json.loads(imp.datos) if imp and imp.datos else None
    wb = _build_export(regs, prog, bol, periodo, impacto36)
    buf = io.BytesIO()
    wb.save(buf)
    return _recalcular_xlsx(buf.getvalue())   # embebe valores (LibreOffice) → no se ve vacío


@router.get("/export")
async def exportar(periodo: str, db: AsyncSession = Depends(get_db),
                   _: Usuario = Depends(get_current_active_user)):
    data = await _ans_xlsx_bytes(db, periodo)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Sin registros para el periodo {periodo}")
    fname = f"Calculo_ANS_{periodo}.xlsx"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── EVIDENCIAS: mismo concepto de carpetas de SharePoint, exportable por mes ──────
# Carpeta por indicador (como las tenía Paola), y dentro por N.º de orden.
_EVID_CARPETA = {
    "excluible":         "3.2 A - Tiempos",
    "excluible_anulado": "3.2 B - Anulaciones",
    "excluible_tiempos": "3.4 - Oportunidad",
    "excluible2":        "3.3 - UUCC",
}
_MESES_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
             "Septiembre", "Octubre", "Noviembre", "Diciembre"]


def _mes_es(periodo: str) -> str:
    try:
        y, m = periodo.split("-")
        return f"{_MESES_ES[int(m)]}_{y}"
    except Exception:
        return periodo


def _safe_fs(s: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", (s or "").strip()) or "sin_nombre"


async def _evidencias_del_mes(db: AsyncSession, periodo: str):
    """Evidencias cuyas órdenes son de ese periodo → [(AnsEvidencia, protocolo), …]."""
    q = (select(AnsEvidencia, AnsRegistro.protocolo)
         .join(AnsRegistro, AnsRegistro.id == AnsEvidencia.reg_id)
         .where(AnsRegistro.periodo == periodo)
         .order_by(AnsEvidencia.campo, AnsRegistro.protocolo, AnsEvidencia.id))
    return (await db.execute(q)).all()


@router.get("/evidencias")
async def evidencias_arbol(periodo: str, db: AsyncSession = Depends(get_db),
                           _: Usuario = Depends(get_current_active_user)):
    """El árbol de carpetas de evidencias del mes (para verlo antes de exportar)."""
    filas = await _evidencias_del_mes(db, periodo)
    grupos: dict = {}
    total = 0
    for e, proto in filas:
        total += 1
        g = grupos.setdefault(e.campo, {})
        g.setdefault(proto, []).append({"id": e.id, "nombre": e.nombre, "tamano": e.tamano,
                                        "mime": e.mime, "cuando": _bog(e.creado_en).isoformat() if e.creado_en else None})
    salida = [{"campo": campo, "label": _EVID_CARPETA.get(campo, campo),
               "total": sum(len(v) for v in ordenes.values()),
               "ordenes": [{"protocolo": p, "archivos": a} for p, a in ordenes.items()]}
              for campo, ordenes in grupos.items()]
    n_regs = (await db.execute(select(func.count()).select_from(AnsRegistro)
                               .where(AnsRegistro.periodo == periodo))).scalar()
    return {"periodo": periodo, "mes": _mes_es(periodo), "ans_disponible": bool(n_regs),
            "total_evidencias": total, "grupos": salida}


@router.get("/evidencias/exportar")
async def evidencias_exportar(periodo: str, db: AsyncSession = Depends(get_db),
                              _: Usuario = Depends(get_current_active_user)):
    """ZIP del mes con la estructura de carpetas de Paola: el Calculo_ANS_<Mes>.xlsx en la raíz, y
    `exclusiones/<indicador>/<N.º de orden>/<archivos>` con los videos (3.2 A) y correos (3.2 B)."""
    raiz = f"ANS_{_mes_es(periodo)}"
    xlsx = await _ans_xlsx_bytes(db, periodo)          # async: fuera del zip (sync)
    filas = await _evidencias_del_mes(db, periodo)
    if xlsx is None and not filas:
        raise HTTPException(status_code=404, detail=f"Sin datos ni evidencias para {periodo}")

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            if xlsx:
                z.writestr(f"{raiz}/Calculo_ANS_{periodo}.xlsx", xlsx)
            faltan = 0
            for e, proto in filas:
                if not os.path.exists(e.ruta):
                    faltan += 1
                    continue
                carpeta = _EVID_CARPETA.get(e.campo, e.campo)
                destino = f"{raiz}/exclusiones/{carpeta}/{_safe_fs(proto)}/{_safe_fs(e.nombre)}"
                z.write(e.ruta, destino)
            z.writestr(f"{raiz}/LEEME.txt",
                       f"Paquete ANS de {_mes_es(periodo).replace('_', ' ')}\n"
                       f"- Calculo_ANS_{periodo}.xlsx: el ANS del mes.\n"
                       f"- exclusiones/: soportes por indicador y N.º de orden (videos 3.2 A, correos 3.2 B).\n"
                       f"Total de soportes: {len(filas) - faltan}.\n")
        tmp.close()
    except Exception:
        tmp.close()
        os.path.exists(tmp.name) and os.remove(tmp.name)
        raise

    def _iter():
        with open(tmp.name, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                yield chunk
        os.path.exists(tmp.name) and os.remove(tmp.name)

    return StreamingResponse(_iter(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{raiz}.zip"'})
