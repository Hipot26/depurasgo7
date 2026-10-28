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
import json
import unicodedata
import shutil
import hashlib
import tempfile
import subprocess
from datetime import date, datetime, timedelta, time
from collections import defaultdict
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

from app.db.database import get_db
from app.api.deps import get_current_active_user
from app.models.models import (AnsRegistro, AnsAprobador, AnsPeriodo, AnsFuente, AnsBolsaSnapshot,
                                AnsCambio, AnsImpacto36, AnsProg20, AnsSubprocMap,
                                AnsConsulta, AnsConsultaMsg,
                                Bolsa, Contacto, Programacion, Usuario, ZonaEnum)
from app.schemas.schemas import AnsRegistroUpdate, AnsAprobadorCreate
from app.services.minimax import clasificar as _ia_clasificar
from app.db.ans_ctx import current_entorno, set_entorno

router = APIRouter(prefix="/ans", tags=["ans"])

# ── Aislamiento real/prueba ───────────────────────────────────────────────────────
# El router se monta dos veces (main.py): /api/ans (real) y /api/prueba/ans (prueba).
# Estos modelos llevan la columna `entorno`; un único listener filtra TODOS sus SELECT
# por el entorno del request, así el mismo código sirve a ambos sin mezclar datos.
_MODELOS_ENTORNO = (AnsRegistro, AnsPeriodo, AnsFuente, AnsCambio, AnsBolsaSnapshot, AnsImpacto36,
                    AnsProg20, AnsSubprocMap, AnsConsulta)


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
    """Nombre de aprobador sin sufijo (CODIGO), espacios colapsados, mayúsculas."""
    s = re.sub(r"\(.*?\)", "", str(s or ""))
    return re.sub(r"\s+", " ", s).strip().upper()


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


def _zona_de(tipo: str, nombre_responsable: Optional[str]) -> Optional[str]:
    if tipo == "ST":
        return "LV"
    if nombre_responsable:
        return SUPERVISOR_ZONA.get(_norm_nombre(nombre_responsable))
    return None


# ── ETL: parsear un CD10 a registros (aplicando el filtro) ───────────────────────

def _parse_cd10(wb, tipo: str, aprobadores: set) -> list[dict]:
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
        rec["zona"] = _zona_de(tipo, rec.get("nombre_responsable"))
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
    "excluible", "obs", "se_ejecuto", "excluible_anulado", "obs_anulado", "obs_uucc",
    "excluible2", "obs3", "nodos_planeados", "nodos_ejecutados", "nods_excluible", "obs_nodos",
]


def computar(reg: "AnsRegistro | dict") -> dict:
    """Columnas U..AY de la hoja 'data', calculadas en Python igual que las fórmulas."""
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

    # AR: cumple UUCC (3.3)
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
            esperado = _debio_validar_auto(g("fecha_inicio"))
            if v and esperado and str(v).strip() == esperado:
                J[campo] = {"origen": "auto",
                            "texto": (f"Calculada por el sistema: fecha de inicio ({g('fecha_inicio')}) "
                                      f"− {DIAS_HABILES_VALIDACION} días hábiles = {esperado}. Editable si "
                                      f"este caso va distinto.")}
            elif v:
                J[campo] = {"origen": "manual",
                            "texto": (f"Fecha puesta a mano. La regla del sistema daría {esperado or '—'} "
                                      f"(inicio − {DIAS_HABILES_VALIDACION} días hábiles), así que alguien la corrigió.")}
            else:
                J[campo] = {"origen": "manual", "texto": "Sin fecha de inicio válida → no se pudo calcular."}
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

    # Resto de manuales (exclusiones, observaciones, nodos)
    for campo in ("excluible_tiempos", "obs_tiempos", "excluible", "obs", "excluible_anulado",
                  "obs_anulado", "obs_uucc", "excluible2", "obs3", "nodos_planeados",
                  "nodos_ejecutados", "nods_excluible", "obs_nodos"):
        J.setdefault(campo, {"origen": "manual", "texto": "Campo manual que diligencia la programadora."})
    return J


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
    # María Inés / Ingeniería → 3.2 C (nodos)
    if reg.nodos_planeados is None and reg.nodos_ejecutados is None:
        out["nodos_planeados"] = "ingeniero_zona"
        out["nodos_ejecutados"] = "ingeniero_zona"
    # Encargado ANS → exclusiones. Son DOS momentos, y los dos parpadean:
    #   1) decidir si se excluye (SI/NO),
    #   2) DECIR POR QUÉ — una exclusión sin razón no se sostiene ante ENEL.
    # Por eso, en cuanto se decide (sea SI o NO), la observación queda pendiente.
    c = computar(reg)
    nc = lambda v: str(v or "").upper() == "NO CUMPLE"

    def _excl(toca_decidir, decision, campo_decision, razon, campo_razon):
        if toca_decidir and not txt(decision):
            out[campo_decision] = "ans"            # falta decidir si aplica exclusión
        elif txt(decision) and not txt(razon):
            out[campo_razon] = "ans"               # ya decidiste → falta la razón

    _excl(nc(c.get("cumple_tprog")),                                    # 3.4   AA → AB
          reg.excluible_tiempos, "excluible_tiempos", reg.obs_tiempos, "obs_tiempos")
    _excl(nc(c.get("cumple_tiempos")) or nc(c.get("cumple_apertura")),  # 3.2 A AL → AM
          reg.excluible, "excluible", reg.obs, "obs")
    _excl(str(reg.se_ejecuto or "").upper() == "NO",                    # 3.2 B AO → AP (anulados)
          reg.excluible_anulado, "excluible_anulado", reg.obs_anulado, "obs_anulado")
    _excl(nc(c.get("cumple_uucc")),                                     # 3.3   AS → AT
          reg.excluible2, "excluible2", reg.obs3, "obs3")
    return out


def _to_dict(b: AnsRegistro) -> dict:
    d = {c.name: getattr(b, c.name) for c in b.__table__.columns}
    d["datos_extra"] = json.loads(b.datos_extra) if b.datos_extra else {}
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
    tipo: str = Query(..., pattern="^(programados|st)$"),
    periodo: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    db: AsyncSession = Depends(get_db),
    current_user: Usuario = Depends(get_current_active_user),
):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="El archivo debe ser .xlsx")
    t = "FT" if tipo == "programados" else "ST"
    contents = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo: {e}")

    aprob = await _aprobadores_set(db)
    registros = _parse_cd10(wb, t, aprob)
    uni = current_user.unidad or "SABOGAL"

    # ── Clasificación automática por mes (un CD10 puede traer varios meses) ─────
    # Cada registro va al periodo de su FECHA FIN (o inicio); sin fecha parseable → periodo elegido.
    for rec in registros:
        rec["_periodo"] = _periodo_registro(rec, periodo)
    reparto: dict = {}
    for rec in registros:
        reparto[rec["_periodo"]] = reparto.get(rec["_periodo"], 0) + 1
    periodos_dest = sorted(reparto.keys()) or [periodo]

    # ── Controles de subida (procedencia) ──────────────────────────────────────
    avisos = []
    file_hash = hashlib.sha256(contents).hexdigest()
    # 1) ¿archivo idéntico ya subido? (mismo contenido, cualquier nombre/periodo)
    rep = (await db.execute(select(AnsFuente).where(AnsFuente.hash == file_hash))).scalars().first()
    if rep:
        avisos.append(f"Archivo idéntico ya subido como «{rep.nombre}» (periodo {rep.periodo}). Es un re-proceso, no se duplican datos.")
    # 2) ¿tipo correcto? FT→protocolos CDOPP/PP, ST→CDOPT/PT
    prefijo_ok = "PP2026R" if t == "FT" else "PT2026R"
    malos = sum(1 for r in registros if prefijo_ok not in (r.get("protocolo") or ""))
    if registros and malos > len(registros) / 2:
        otro = "Línea Viva (ST)" if t == "FT" else "Programados (FT)"
        avisos.append(f"⚠ El archivo no parece ser de tipo «{'Programados (FT)' if t=='FT' else 'Línea Viva (ST)'}»; ¿quizá es {otro}?")
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
            if _emerg_o_apoyo({"tipo": ex.tipo, "causal": ex.causal, "asociado_pdl": ex.asociado_pdl}):
                # Regla absoluta de Paola: emergencial/apoyo NUNCA lleva fecha → siempre N/A
                # (corrige casos atípicos que hubieran quedado con fecha por error).
                ex.fecha_debio_validar = "N/A"                                  # #10
                ex.fecha_envio_validar = "N/A"
            elif not (ex.fecha_debio_validar or "").strip():
                ex.fecha_debio_validar = _debio_validar_auto(ex.fecha_inicio)   # W = inicio − 9 hábiles
            ex.datos_extra = json.dumps(extra, ensure_ascii=False, default=str)
            actualizados += 1
        else:
            # Autocompletados al crear (editables; se preservan en re-importes):
            if t == "ST" and not rec.get("sigla_elaborador"):
                rec["sigla_elaborador"] = _sigla_lv_auto(rec.get("descripcion"))  # #8 ZONA VII→Andrea
            if not rec.get("se_ejecuto"):
                rec["se_ejecuto"] = _se_ejecuto_auto(rec.get("estado_actual"))    # #9 cerrado→SI/anulado→NO
            if _emerg_o_apoyo(rec):                                               # #10 emergencial/apoyo→N/A
                rec["fecha_debio_validar"] = "N/A"
                rec["fecha_envio_validar"] = "N/A"
            elif not (rec.get("fecha_debio_validar") or "").strip():
                rec["fecha_debio_validar"] = _debio_validar_auto(rec.get("fecha_inicio"))  # W = inicio − 9 hábiles
            db.add(AnsRegistro(
                unidad=uni, periodo=pr,
                datos_extra=json.dumps(extra, ensure_ascii=False, default=str), **rec,
            ))
            insertados += 1
    # Respaldo de procedencia: guarda el CD10 bajo cada periodo que recibió registros
    for pr in periodos_dest:
        await _guardar_fuente(db, pr, tipo, file.filename, contents, reparto.get(pr, 0), file_hash)
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
    else:
        db.add(AnsFuente(periodo=periodo, tipo=tipo, nombre=nombre, ruta=ruta_rel,
                         tamano=len(contents), registros=registros, hash=file_hash))


# ── Listado / edición ────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
#  CAMPANITA DE TAREAS: qué columna le toca llenar a cada rol (guía de Paola).
#  Cadena real: el SISTEMA vuelca el STM → aviso "DILIGENCIAMIENTO ANS" →
#  PROGRAMADORAS (3.4: W/Y + sigla FT) + MARÍA INÉS (3.2 C: nodos) →
#  ENCARGADO ANS aplica EXCLUSIONES (AA/AL/AS) sobre los NO CUMPLE.
# ══════════════════════════════════════════════════════════════════════════════
TAREAS_ANS = [
    {
        "id": "3.4", "rol": "programador", "rol_label": "Programadoras",
        "titulo": "Fecha en que se envió a validar (3.4)", "columnas": "Y",
        "ayuda": ("Solo falta la Y: la fecha en que REALMENTE enviaste la programación a validar (búscala en "
                  "el correo / SharePoint donde la mandaste). La W («debió validar») ya la calcula el sistema "
                  "= fecha de inicio − 9 días hábiles, y es editable si un caso va distinto. CUMPLE si "
                  "enviaste al menos 1 día antes del plazo. Las emergenciales y las de apoyo a PDL quedan en "
                  "N/A solas — esas NO se llenan."),
    },
    {
        "id": "sigla", "rol": "programador", "rol_label": "Programadoras",
        "titulo": "Sigla del elaborador (FT)", "columnas": "H",
        "ayuda": ("Quién elaboró/programó la orden. En las filas ST el sistema la pone sola (regla ZONA VII); "
                  "en las FT hay que confirmarla. Sirve para desglosar el 3.4 por programadora."),
    },
    {
        "id": "3.2C", "rol": "ingeniero_zona", "rol_label": "María Inés / Ingeniería",
        "titulo": "Nodos planeados / ejecutados (3.2 C)", "columnas": "AU · AV",
        "ayuda": ("Cuántos nodos estaban planeados y cuántos se ejecutaron en la orden. Con eso el sistema "
                  "calcula el 3.2 C (alcance)."),
    },
    {
        "id": "exclusiones", "rol": "ans", "rol_label": "Encargado ANS",
        "titulo": "Exclusiones + su razón", "columnas": "AA·AL·AO·AS + AB·AM·AP·AT",
        "ayuda": ("Dos pasos, y los dos parpadean. (1) DECIDIR si aplica exclusión en las filas que "
                  "quedaron en NO CUMPLE o anuladas: AA excluye del 3.4, AL del 3.2 A, AO del 3.2 B "
                  "(anulados) y AS del 3.3 — marcar «SI» las saca del indicador (pasan a N/A). "
                  "(2) DECIR POR QUÉ en la observación de al lado (AB, AM, AP, AT): sea SI o NO, la "
                  "decisión necesita razón — una exclusión sin justificación no se sostiene ante ENEL."),
    },
]
ROLES_VEN_TODO = ("desarrollador", "ans", "directivo")


TAREA_CAMPOS = {   # qué celdas cubre cada tarea (deriva del MISMO _pend_campos que el parpadeo,
    "3.4":         ("fecha_debio_validar", "fecha_envio_validar"),   # así la campanita y las
    "sigla":       ("sigla_elaborador",),                            # celdas nunca se contradicen
    "3.2C":        ("nodos_planeados", "nodos_ejecutados"),
    "exclusiones": ("excluible_tiempos", "obs_tiempos", "excluible", "obs",
                    "excluible_anulado", "obs_anulado", "excluible2", "obs3"),
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
APOYO_ZONA = {
    "GUAVIO": "Jhon Quiroga",
    "ALMEIDAS": "Michael Guasca",
    "UBATE": None,          # ⚠ hueco de la guía: nadie asignado
}
# Del log de exclusiones de campo (2.856 filas, 2023-2027): ~63% son CLIMA.
PISTA_CAMPO = ("En el histórico de exclusiones de campo, ~63% son CLIMA («stop work por lluvias»); "
               "el resto: terreno rocoso, vía cerrada, trancón. Empieza preguntando por el clima de ese día.")


# ── DIRECTORIO: se llena a mano, un contacto a la vez ────────────────────────────
# No importamos la agenda ni raspamos WhatsApp: cuando el sistema dice "pregúntale a X" y X no
# tiene número, tú lo agregas ahí mismo. Queda guardado para siempre y con certeza, porque lo
# guardó una persona. El nombre normalizado es la llave: el mismo líder vuelve a aparecer en
# otras órdenes y el sistema ya sabe a qué número escribirle.
def _norm_nombre(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", (s or "").strip().upper())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s)


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


def _mensaje_wa(reg: AnsRegistro, campo: str, quien: str, rol_dir: str = "otro",
                remitido_por: Optional[str] = None) -> Optional[str]:
    """El mensaje ya redactado, con los datos de ESTA orden. Tono profesional (usted). Editable
    antes de enviar. Si `remitido_por` viene, arranca aclarando quién lo remitió (para cuando una
    persona nos manda con otra: «X me indicó que usted…»)."""
    corto = _nombre_corto(quien, apellidos_primero=(rol_dir == "lider"))
    saludo = f"Buen día, {corto}. Cordial saludo." if corto else "Buen día. Cordial saludo."
    if remitido_por:
        rem = _nombre_corto(remitido_por, apellidos_primero=False) or remitido_por
        saludo += f" {rem} me indicó que usted me puede ayudar con el siguiente caso del ANS."
    else:
        saludo += " Le escribo por un tema del ANS."
    cuando = (reg.fecha_inicio or "").strip()
    ficha = f"Orden {reg.protocolo}" + (f" · {reg.zona}" if reg.zona else "") + (f" · {cuando}" if cuando else "")
    desc = (reg.descripcion or "").strip()
    if len(desc) > 90:
        desc = desc[:90].rstrip() + "…"

    if campo in ("excluible", "obs"):                       # 3.2 A — tiempos, causa de campo
        return (f"{saludo}\n\n{ficha}\n{desc}\n\n"
                "Esta orden presentó incumplimiento en los tiempos pactados. ¿Me podría indicar qué "
                "ocurrió ese día? (condiciones climáticas, vía cerrada, terreno, un tercero, o si fue "
                "un tema interno).\n\n"
                "Si cuenta con algún soporte o evidencia fotográfica, le agradezco me lo comparta: con "
                "soporte puedo gestionar la exclusión del indicador; de lo contrario debe quedar en "
                "contra. Quedo atento a su respuesta. Muchas gracias.")
    if campo in ("excluible_anulado", "obs_anulado"):       # 3.2 B — la anuló ENEL
        return (f"{saludo}\n\n{ficha}\n{desc}\n\n"
                f"Esta orden figura como ANULADA ({reg.estado_actual or 'anulada'}). ¿Me podría "
                "facilitar el correo del Centro Operativo donde indiquen el motivo de la cancelación?\n\n"
                "Sin ese soporte la orden queda contabilizada en contra. Quedo atento. Muchas gracias.")
    if campo in ("excluible_tiempos", "obs_tiempos"):       # 3.4 — envío tardío (causa interna)
        return (f"{saludo}\n\n{ficha}\n\n"
                "La programación de esta orden se envió tarde a validación (indicador 3.4). ¿Recuerda si "
                "hubo algún motivo (cambio por parte de ENEL, indisponibilidad del sistema, "
                "reprogramación)? De ser así, le agradezco el correo o pantallazo para poder "
                "justificarlo. Quedo atento. Muchas gracias.")
    return None


async def _tel_de(db: AsyncSession, nombre: str) -> Optional[Contacto]:
    n = _norm_nombre(nombre)
    if not n:
        return None
    r = await db.execute(select(Contacto).where(Contacto.nombre_norm == n, Contacto.activo.is_(True)))
    return r.scalar_one_or_none()


def _sugerencia_celda(reg: AnsRegistro, campo: str) -> dict:
    """Quién sabe la respuesta para poder llenar ESTA celda, y por qué esa persona."""
    zona = (reg.zona or "").strip().upper()
    sup = (reg.nombre_responsable or "").strip()
    lider = (reg.ri_jt or "").strip()
    anulado = "ANULAD" in (reg.estado_actual or "").upper() or "AN. POR" in (reg.estado_actual or "").upper()
    apoyo = APOYO_ZONA.get(zona)
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
        evidencia = "SharePoint / ANS / Programados / <Mes> / exclusiones / 3.2A"

    elif campo in ("excluible_tiempos", "obs_tiempos"):
        titulo = "3.4 · ¿Se envió tarde la programación con excusa válida?"
        que = ("Aquí la causa es INTERNA (nuestra programación), no de campo. Solo se excluye si hubo un "
               "motivo real y demostrable para enviar tarde. Paola excluyó las 4 tardías de julio sin "
               "evidencia visible — no lo repitas: ante ENEL no se sostiene.")
        contactos.append({"quien": "Tú / la programadora", "rol": "Quien envió la programación",
                          "porque": "El dato está en el correo o el SharePoint donde se envió la programación de esa semana.",
                          "canal": "Correo / SharePoint"})
        ruta = ["Busca el correo de envío de la programación de esa semana",
                "Si se envió tarde por una causa real (caída del sistema, cambio de ENEL…), documéntala"]
        evidencia = "El correo del envío"

    else:   # excluible2 / obs3 (3.3 UUCC) y cualquier otro
        titulo = "3.3 · Calidad de la información (UUCC)"
        que = "Decide si el incumplimiento de UUCC es excluible."
        contactos.append({"quien": "— no documentado —", "rol": "Sin dueño en la guía de Paola",
                          "porque": "La guía nunca dijo a quién se le pregunta esto. Es una duda abierta para Cristian.",
                          "canal": "—"})
        ruta = []
        evidencia = "—"

    return {"titulo": titulo, "que_determinar": que, "contactos": contactos, "ruta": ruta,
            "pista": pista, "evidencia": evidencia,
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
    sug = _sugerencia_celda(reg, campo)

    # A cada contacto real (los "— sin asignar —" no) se le busca su teléfono en el directorio y
    # se le adjunta el mensaje ya redactado. Si no tiene número, el popup ofrece agregarlo.
    for c in sug["contactos"]:
        quien = c.get("quien") or ""
        if quien.startswith("—"):
            continue
        rol_txt = (c.get("rol") or "").lower()
        rol_dir = ("supervisor" if "supervisor" in rol_txt
                   else "apoyo" if "apoyo" in rol_txt
                   else "lider" if "líder" in rol_txt or "lider" in rol_txt else "otro")
        ct = await _tel_de(db, quien)
        c["contacto_id"] = ct.id if ct else None
        c["telefono"] = ct.telefono if ct else None
        c["mensaje"] = _mensaje_wa(reg, campo, quien, rol_dir)
        c["rol_dir"] = rol_dir
    return sug


# ── Directorio de contactos (CRUD) ───────────────────────────────────────────────
@router.get("/contactos")
async def listar_contactos(buscar: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    q = select(Contacto).where(Contacto.activo.is_(True))
    if buscar:
        q = q.where(Contacto.nombre_norm.ilike(f"%{_norm_nombre(buscar)}%"))
    r = await db.execute(q.order_by(Contacto.nombre))
    return [{"id": c.id, "nombre": c.nombre, "telefono": c.telefono, "rol": c.rol,
             "zona": c.zona, "notas": c.notas} for c in r.scalars().all()]


@router.post("/contactos")
async def guardar_contacto(datos: dict = Body(...), db: AsyncSession = Depends(get_db),
                           current_user: Usuario = Depends(get_current_active_user)):
    """Agregar (o corregir) el número de alguien. Upsert por nombre normalizado: si ese líder ya
    estaba, se le actualiza el teléfono en vez de duplicarlo."""
    nombre = (datos.get("nombre") or "").strip()
    tel = _norm_tel(datos.get("telefono"))
    if not nombre:
        raise HTTPException(status_code=400, detail="Falta el nombre")
    if not tel:
        raise HTTPException(status_code=400, detail="Número no válido. Escríbelo con indicativo o los 10 dígitos del móvil.")

    n = _norm_nombre(nombre)
    r = await db.execute(select(Contacto).where(Contacto.nombre_norm == n))
    c = r.scalar_one_or_none()
    if c:
        c.telefono, c.activo = tel, True
        c.rol = datos.get("rol") or c.rol
        c.zona = datos.get("zona") or c.zona
        if datos.get("notas"):
            c.notas = datos["notas"]
    else:
        c = Contacto(nombre=nombre, nombre_norm=n, telefono=tel, rol=datos.get("rol"),
                     zona=datos.get("zona"), notas=datos.get("notas"), creado_por=current_user.id)
        db.add(c)
    await db.commit()
    await db.refresh(c)
    return {"id": c.id, "nombre": c.nombre, "telefono": c.telefono, "rol": c.rol, "zona": c.zona}


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


# ── CONSULTA GUIADA CON IA — el hilo que resuelve una exclusión ──────────────────
# Envías el mensaje por WhatsApp, pegas la respuesta, y MiniMax la clasifica: si te remiten a
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


def _conversa_txt(msgs) -> str:
    """Los turnos como texto plano para la IA."""
    out = []
    for m in msgs:
        if m.direccion == "enviado":
            out.append(f"enviado (a {m.persona or '—'}): {m.texto or ''}")
        elif m.direccion == "recibido":
            out.append(f"recibido ({m.persona or '—'}): {m.texto or ''}")
    return "\n".join(out)


async def _consulta_dict(db: AsyncSession, c: AnsConsulta, reg: AnsRegistro) -> dict:
    """Serializa el hilo + la última decisión de la IA + a quién sigue (con teléfono y mensaje)."""
    r = await db.execute(select(AnsConsultaMsg).where(AnsConsultaMsg.consulta_id == c.id)
                         .order_by(AnsConsultaMsg.id))
    msgs = r.scalars().all()
    dec = None
    for m in reversed(msgs):
        if m.direccion == "ia" and m.meta:
            try:
                dec = json.loads(m.meta)
            except Exception:
                dec = None
            break

    decision, razon_field, label, _obj = _CONSULTA_CFG.get(c.campo, ("excluible", "obs", "?", ""))
    out = {
        "id": c.id, "reg_id": c.reg_id, "campo": c.campo, "estado": c.estado,
        "indicador": label, "persona_actual": c.persona_actual,
        "decision_campo": decision, "razon_campo": razon_field,
        "propuesta": None, "siguiente": None,
        "mensajes": [{"direccion": m.direccion, "persona": m.persona, "texto": m.texto,
                      "meta": json.loads(m.meta) if (m.direccion == "ia" and m.meta) else None,
                      "cuando": m.creado_en.isoformat() if m.creado_en else None} for m in msgs],
    }
    if not dec:
        return out

    if dec.get("accion") == "resolver" and dec.get("excluible"):
        out["propuesta"] = {"excluible": dec.get("excluible"), "categoria": dec.get("categoria"),
                            "razon": dec.get("razon"), "resumen": dec.get("resumen")}
    elif dec.get("accion") == "redirigir" and dec.get("siguiente_persona"):
        quien = dec["siguiente_persona"]
        ct = await _tel_de(db, quien)
        remitio = c.persona_actual
        out["siguiente"] = {
            "quien": quien, "rol": "Le remiten con esta persona", "rol_dir": "otro",
            "porque": f"{remitio or 'La persona anterior'} indicó que es quien sabe la respuesta.",
            "contacto_id": ct.id if ct else None, "telefono": ct.telefono if ct else None,
            "mensaje": _mensaje_wa(reg, c.campo, quien, "otro", remitido_por=remitio),
            "resumen": dec.get("resumen"),
        }
    elif dec.get("accion") == "seguir":
        out["seguir"] = {"falta": dec.get("razon"), "resumen": dec.get("resumen")}
    elif dec.get("accion") == "manual":
        out["ia_error"] = dec.get("error", "La IA no pudo interpretar; decide a mano.")
    return out


async def _get_reg_o_404(db: AsyncSession, reg_id: int) -> AnsRegistro:
    reg = await db.get(AnsRegistro, reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    return reg


def _solo_dev(current_user: Usuario):
    if getattr(current_user.rol, "value", str(current_user.rol)) != "desarrollador":
        raise HTTPException(status_code=403, detail="Consulta guiada: solo el desarrollador por ahora")


@router.get("/consulta")
async def consulta_get(reg_id: int, campo: str, db: AsyncSession = Depends(get_db),
                       current_user: Usuario = Depends(get_current_active_user)):
    """El hilo abierto de esta celda, si existe (para rehidratar el popup)."""
    _solo_dev(current_user)
    if campo not in _CONSULTA_CFG:
        raise HTTPException(status_code=400, detail="Campo sin consulta guiada")
    decision = _CONSULTA_CFG[campo][0]
    r = await db.execute(select(AnsConsulta).where(
        AnsConsulta.reg_id == reg_id, AnsConsulta.campo == decision,
        AnsConsulta.estado != "descartada").order_by(AnsConsulta.id.desc()))
    c = r.scalars().first()
    if not c:
        return {"existe": False}
    reg = await _get_reg_o_404(db, reg_id)
    return {"existe": True, **(await _consulta_dict(db, c, reg))}


@router.post("/consulta/analizar")
async def consulta_analizar(datos: dict = Body(...), db: AsyncSession = Depends(get_db),
                            current_user: Usuario = Depends(get_current_active_user)):
    """Registra la respuesta que llegó por WhatsApp y la clasifica con la IA. Crea el hilo si no
    existía. Body: {reg_id, campo, persona, texto, enviado?}. `enviado` (opcional) = el mensaje que
    tú mandaste, para que quede en el hilo la primera vez."""
    _solo_dev(current_user)
    reg_id = datos.get("reg_id")
    campo = datos.get("campo")
    persona = (datos.get("persona") or "").strip() or None
    texto = (datos.get("texto") or "").strip()
    if not reg_id or campo not in _CONSULTA_CFG or not texto:
        raise HTTPException(status_code=400, detail="Faltan reg_id, campo válido o el texto de la respuesta")

    decision, razon_field, label, objetivo = _CONSULTA_CFG[campo]
    reg = await _get_reg_o_404(db, reg_id)

    # Hilo existente o nuevo
    r = await db.execute(select(AnsConsulta).where(
        AnsConsulta.reg_id == reg_id, AnsConsulta.campo == decision,
        AnsConsulta.estado != "descartada").order_by(AnsConsulta.id.desc()))
    c = r.scalars().first()
    if not c:
        c = AnsConsulta(reg_id=reg_id, campo=decision, estado="abierta",
                        persona_actual=persona, creado_por=current_user.id)
        db.add(c)
        await db.flush()
        if datos.get("enviado"):        # deja constancia del primer mensaje enviado
            db.add(AnsConsultaMsg(consulta_id=c.id, direccion="enviado",
                                  persona=persona, texto=str(datos["enviado"])[:4000]))
    db.add(AnsConsultaMsg(consulta_id=c.id, direccion="recibido", persona=persona, texto=texto[:4000]))
    await db.flush()

    # Clasifica TODA la conversación acumulada
    rr = await db.execute(select(AnsConsultaMsg).where(AnsConsultaMsg.consulta_id == c.id)
                          .order_by(AnsConsultaMsg.id))
    convo = _conversa_txt(rr.scalars().all())
    dec = await _ia_clasificar(objetivo, _ctx_orden(reg, label), convo)

    db.add(AnsConsultaMsg(consulta_id=c.id, direccion="ia", persona=None,
                          texto=dec.get("resumen") or dec.get("error"),
                          meta=json.dumps(dec, ensure_ascii=False)))
    # Actualiza el estado del hilo según la decisión
    if dec.get("accion") == "resolver" and dec.get("excluible"):
        c.excluible_sug = dec.get("excluible"); c.categoria = dec.get("categoria")
        c.razon_sug = dec.get("razon"); c.resumen = dec.get("resumen")
    elif dec.get("accion") == "redirigir" and dec.get("siguiente_persona"):
        c.persona_actual = dec["siguiente_persona"]; c.resumen = dec.get("resumen")
    await db.commit()
    await db.refresh(c)
    return await _consulta_dict(db, c, reg)


@router.post("/consulta/{cid}/aplicar")
async def consulta_aplicar(cid: int, db: AsyncSession = Depends(get_db),
                           current_user: Usuario = Depends(get_current_active_user)):
    """Escribe en el ANS la determinación propuesta (decisión + razón) y cierra el hilo. Es el paso
    de aprobación: la IA propone, una persona confirma."""
    _solo_dev(current_user)
    c = await db.get(AnsConsulta, cid)
    if not c:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")
    if not (c.excluible_sug and c.razon_sug):
        raise HTTPException(status_code=400, detail="Aún no hay una determinación para aplicar")
    reg = await _get_reg_o_404(db, c.reg_id)
    decision_field, razon_field, _l, _o = _CONSULTA_CFG.get(c.campo, (c.campo, "obs", "", ""))
    setattr(reg, decision_field, c.excluible_sug)
    setattr(reg, razon_field, c.razon_sug)
    c.estado = "resuelta"
    await db.commit()
    return {"ok": True, "reg_id": reg.id, "campo_decision": decision_field, "valor_decision": c.excluible_sug,
            "campo_razon": razon_field, "valor_razon": c.razon_sug}


@router.get("/tareas")
async def tareas(periodo: str, db: AsyncSession = Depends(get_db),
                 current_user: Usuario = Depends(get_current_active_user)):
    """Campanita: qué le falta llenar a cada rol en este periodo. Los roles que ven todo
    (dev/ans/directivo) reciben TODAS las tareas; los demás solo la suya."""
    regs = (await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo))).scalars().all()
    rol = getattr(current_user.rol, "value", str(current_user.rol))
    ve_todo = rol in ROLES_VEN_TODO
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


@router.get("")
async def listar(
    periodo: Optional[str] = None,
    tipo: Optional[str] = None,
    zona: Optional[str] = None,
    q: Optional[str] = None,
    pendiente: Optional[str] = None,      # id de tarea → solo las filas que le faltan a ese rol
    skip: int = Query(0, ge=0),
    limit: int = Query(3000, le=5000),
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_active_user),
):
    f = []
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
    return {"reg_id": reg_id, "fuente": f.nombre if f else None, "campos": _justificar(reg, f.nombre if f else None)}


def _s(v):
    return None if v is None else str(v)


@router.patch("/{reg_id}")
async def actualizar(reg_id: int, req: AnsRegistroUpdate, db: AsyncSession = Depends(get_db),
                     current_user: Usuario = Depends(get_current_active_user)):
    b = (await db.execute(select(AnsRegistro).where(AnsRegistro.id == reg_id))).scalar_one_or_none()
    if not b:
        raise HTTPException(status_code=404, detail="No encontrado")
    # Aplica y registra en el historial cada campo que realmente cambió
    for k, v in req.model_dump(exclude_unset=True).items():
        old = getattr(b, k)
        if _s(old) == _s(v):
            continue
        setattr(b, k, v)
        db.add(AnsCambio(
            registro_id=b.id, periodo=b.periodo, protocolo=b.protocolo, campo=k,
            valor_anterior=_s(old), valor_nuevo=_s(v), tipo="edicion",
            usuario_id=current_user.id, usuario_nombre=current_user.nombre,
        ))
    await db.commit()
    await db.refresh(b)
    return _to_dict(b)


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
        "usuario": c.usuario_nombre, "creado_en": c.creado_en.isoformat() if c.creado_en else None,
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
            "actualizado_en": imp.actualizado_en.isoformat() if imp and imp.actualizado_en else None}


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
    rows = (await db.execute(q.order_by(AnsFuente.periodo.desc(), AnsFuente.tipo, AnsFuente.nombre))).scalars().all()
    return [{"id": f.id, "periodo": f.periodo, "tipo": f.tipo, "nombre": f.nombre,
             "tamano": f.tamano, "registros": f.registros,
             "creado_en": f.creado_en.isoformat() if f.creado_en else None} for f in rows]


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
    no_ejecutado = 0

    for r in regs:
        c = computar(r)
        z = _zona_reg(r)
        excl = (r.excluible or "").upper() == "SI"
        # B: efectividad de apertura (se abrió / no se abrió / exclusiones)
        if (r.se_ejecuto or "").upper() == "NO":
            no_ejecutado += 1
        if c["cumple_apertura"] == "CUMPLE":
            B[z]["cumple"] += 1
        elif (r.excluible_anulado or "").upper() == "SI":
            B[z]["exclusiones"] += 1
        elif c["cumple_apertura"] == "NO CUMPLE":
            B[z]["no_cumple"] += 1
        # A: oportunidad en la ejecución (cumple tiempos)
        if c["cumple_tiempos"] == "CUMPLE":
            A[z]["cumple"] += 1
        elif excl:
            A[z]["exclusiones"] += 1
        elif c["cumple_tiempos"] == "NO CUMPLE":
            A[z]["no_cumple"] += 1
        else:
            A[z]["na"] += 1
        # C: alcance de nodos
        C[z]["cumple"] += (r.nodos_ejecutados or 0)
        C[z]["no_cumple"] += max((r.nodos_planeados or 0) - (r.nodos_ejecutados or 0), 0)
        if (r.nods_excluible or "").upper() == "SI":
            C[z]["exclusiones"] += 1
        # 3.4 oportunidad en la programación
        t34[z][{"CUMPLE": "cumple", "NO CUMPLE": "no_cumple"}.get(c["cumple_tprog"], "na")] += 1
        prog34[r.sigla_elaborador or "—"][c["cumple_tprog"] if c["cumple_tprog"] in ("CUMPLE", "NO CUMPLE") else "N/A"] += 1
        # 3.3 calidad en la información (UUCC)
        t33[z][{"CUMPLE": "cumple", "NO CUMPLE": "no_cumple"}.get(c["cumple_uucc"], "na")] += 1
        prog33[r.sigla_elaborador or "—"][c["cumple_uucc"] if c["cumple_uucc"] in ("CUMPLE", "NO CUMPLE") else "N/A"] += 1
        # Dinamica
        dinamica[(z, (r.nombre_responsable or "—"))][c["cumple_tiempos"] if c["cumple_tiempos"] in ("CUMPLE", "NO CUMPLE") else "N/A"] += 1

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
        "3.2": {"A": A, "B": B, "C": C, "indA": indA, "indB": indB, "indC": indC, "ANS": ans_32},
        "3.3": {"por_zona": t33, "por_programador": prog33, "ANS": ratio(t33)},
        "3.4": {"por_zona": t34, "por_programador": prog34, "ANS": ratio(t34)},
        "Dinamica": [{"zona": k[0], "supervisor": k[1], **v} for k, v in sorted(dinamica.items())],
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
    """3.1 / 3.5 calculados desde el módulo Bolsas (Bolsa) por FECHA vs 'hoy' (fin del mes
    medido). 3.1: col por tipo del datos_extra; 3.5: idem (o fecha_prog del núcleo). No
    requiere re-subir el BOLSAS ENEL: usa los casos ya cargados."""
    hoy = _fin_de_mes(periodo) if periodo else date.today()
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


@router.get("/indicadores")
async def indicadores(periodo: str, db: AsyncSession = Depends(get_db),
                      _: Usuario = Depends(get_current_active_user)):
    prog = await _indicadores_programacion(db, periodo)
    bol = await _indicadores_bolsas(db, periodo)
    p35 = await _prog_35(db, periodo)
    return {"periodo": periodo, **prog, **bol, "3.5_prog": p35["tabla"]}


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
DESCUENTO_35 = [["C ≥ 95%", "0%"], ["90% ≤ C < 95%", "−1%"], ["C < 90%", "−2%"]]

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


def _guess_proceso(sub, proc):
    """Adivina el proceso 3.5 de un subproceso de la programación 20' (semilla EDITABLE)."""
    s = (sub or "").upper()
    p = (proc or "").upper()
    if "PQR" in s or "PQR" in p:
        return "PQR"
    if s.startswith("AMP"):
        return "AMP"
    if "IPPE" in s:
        return "IPPE"
    if "CUNDINAMARCA AL 100" in s or "CUND" in s:
        return "CUND 100"
    if "TUTELA" in s or "TUTELA" in p:
        return "TUTELAS"
    if "TELECONTROL" in p or "EQUIPOS" in s:
        return "TLC"
    return None


def _parse_prog20(wb):
    """Programación diaria ENEL_V1 → filas. Cols (hoja Data): A=FECHA EJEC, B=LCL, C=PROCESO,
    D=SUBPROCESO, K=VALOR MO, L=VALOR MATERIALES, Q=PROVINCIA, V=ESTADO."""
    ws = wb["Data"] if "Data" in wb.sheetnames else wb[wb.sheetnames[0]]

    def num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    filas = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row is None:
            continue
        g = lambda i: row[i] if len(row) > i else None
        lcl, proc, sub, prov = g(1), g(2), g(3), g(16)
        if lcl is None and sub is None and proc is None:
            continue
        d = _fecha_celda(g(0))
        filas.append({
            "mes": f"{d.year:04d}-{d.month:02d}" if d else "",
            "lcl": str(lcl).strip() if lcl is not None else None,
            "proceso": _txt(proc), "subproceso": _txt(sub), "zona": _znorm(prov),
            "valor_mo": num(g(10)), "valor_mat": num(g(11)),
            "fecha_ejec": d.isoformat() if d else None, "estado": _txt(g(21)),
        })
    return filas


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
    """3.5 desde la programación de los 20': PLANEADO = LCL únicos por zona × proceso (mapeo
    editable de subproceso); EJECUTADO = del cálculo de bolsas Arasaka (ya nailado). Tabla
    general con Cumplimiento = 0.4·Cronograma + 0.6·Efectividad. + INVR (MO+MAT)."""
    mapa = await _subproc_map_dict(db)
    rows = (await db.execute(select(AnsProg20).where(AnsProg20.mes == periodo))).scalars().all()
    lcls = {p: {z: set() for z in ZONAS_REPORTE} for p in PROC35_ORDEN}
    invr_plan = {z: 0.0 for z in ZONAS_REPORTE}
    for r in rows:
        z = r.zona if r.zona in ZONAS_REPORTE else None
        if not z:
            continue
        invr_plan[z] += (r.valor_mo or 0) + (r.valor_mat or 0)
        pm = mapa.get((r.subproceso or "").strip().upper())
        if pm in PROC35_ORDEN and r.lcl:
            lcls[pm][z].add(r.lcl)
    planeado = {p: {z: len(lcls[p][z]) for z in ZONAS_REPORTE} for p in PROC35_ORDEN}
    d35 = (await _bolsas_3x_modulo(db, periodo))["3.5"]
    ejec = {p: {z: d35.get(p, {}).get(z, {}).get("EJECUTADO", 0) for z in ZONAS_REPORTE}
            for p in PROC35_ORDEN}

    def cumpl(deben, enviaron, ejecutaron):
        crono = 1.0 if deben == 0 else min(enviaron / deben, 1.0)
        efec = 0.0 if enviaron == 0 else min(ejecutaron / enviaron, 1.0)
        return crono, efec, 0.4 * crono + 0.6 * efec

    general = []
    td = te = tj = 0
    for p in PROC35_ORDEN:
        deben = sum(planeado[p].values())
        enviaron = deben                      # Deben == Enviaron por ahora (pendiente Paola)
        ejecutaron = sum(ejec[p].values())
        crono, efec, cp = cumpl(deben, enviaron, ejecutaron)
        general.append({"concepto": p, "deben": deben, "enviaron": enviaron,
                        "ejecutaron": ejecutaron, "cronograma": crono,
                        "efectividad": efec, "cumplimiento": cp})
        td += deben; te += enviaron; tj += ejecutaron
    ct, ef, cpt = cumpl(td, te, tj)
    return {
        "planeado": planeado, "ejecutado": ejec, "general": general,
        "ans": {"deben": td, "enviaron": te, "ejecutaron": tj,
                "cronograma": ct, "efectividad": ef, "cumplimiento": cpt},
        "invr": {"planeado": invr_plan, "ejecutado": await _invr_ejecutado(db, periodo)},
        "procesos": PROC35_ORDEN, "zonas": ZONAS_REPORTE, "descuento": DESCUENTO_35,
    }


async def _resultado_31(db, periodo) -> dict:
    """3.1 resultado: por concepto Cantidad total / Cumple (=ENTREGADO A ENEL) / No cumple /
    Cumplimiento (=Cumple/Total). Total = PENDIENTE + ENTREGADO + PLANEADO."""
    t31 = (await _bolsas_3x_modulo(db, periodo))["3.1"]
    filas, T, C = [], 0, 0
    for concepto, zonas in t31.items():
        pend = sum(z.get("PENDIENTE BOLSA", 0) for z in zonas.values())
        entr = sum(z.get("ENTREGADO A ENEL", 0) for z in zonas.values())
        plan = sum(z.get("PLANEADO", 0) for z in zonas.values())
        total = pend + entr + plan
        filas.append({"concepto": concepto, "total": total, "cumple": entr,
                      "no_cumple": total - entr, "cumplimiento": (entr / total) if total else 0.0})
        T += total; C += entr
    return {"filas": filas, "total": T, "cumple": C, "no_cumple": T - C,
            "cumplimiento": (C / T) if T else 0.0, "descuento": DESCUENTO_31}


@router.post("/prog20")
async def subir_prog20(file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                       current_user: Usuario = Depends(get_current_active_user)):
    """Sube la 'Programación diaria ENEL' (la de los 20'). REEMPLAZA todo lo cargado del entorno
    y auto-siembra el mapeo de subprocesos nuevos (editable después)."""
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="El archivo debe ser .xlsx")
    contents = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True, read_only=True)
        filas = _parse_prog20(wb)
        wb.close()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer: {e}")
    ent = current_entorno()
    await db.execute(delete(AnsProg20).where(AnsProg20.entorno == ent))
    for f in filas:
        db.add(AnsProg20(**f))
    existentes = await _subproc_map_dict(db)
    nuevos = set()
    for f in filas:
        s = (f["subproceso"] or "").strip()
        su = s.upper()
        if s and su not in existentes and su not in nuevos:
            nuevos.add(su)
            db.add(AnsSubprocMap(subproceso=s, proceso=_guess_proceso(s, f["proceso"])))
    # Procedencia: guarda el archivo original para poder VERIFICAR el cruce (descargable).
    file_hash = hashlib.sha256(contents).hexdigest()
    await _guardar_fuente(db, date.today().strftime("%Y-%m"), "prog20", file.filename,
                          contents, len(filas), file_hash)
    await db.commit()
    meses = sorted({f["mes"] for f in filas if f["mes"]})
    return {"filas": len(filas), "meses": meses, "subprocesos_nuevos": len(nuevos),
            "archivo": file.filename}


@router.get("/prog20/estado")
async def estado_prog20(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    filas = (await db.execute(select(func.count()).select_from(AnsProg20))).scalar() or 0
    meses = sorted({m for (m,) in (await db.execute(select(AnsProg20.mes).distinct())).all() if m})
    return {"filas": filas, "meses": meses}


@router.get("/prog20/archivos")
async def prog20_archivos(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    """Historial de 'Cronograma 20' subidos' (para verificar el cruce). El más reciente = actual;
    se descargan por GET /ans/fuentes/{id}. La DATA de 3.5 sale del último subido."""
    rows = (await db.execute(
        select(AnsFuente).where(AnsFuente.tipo == "prog20").order_by(AnsFuente.id.desc())
    )).scalars().all()
    return [{"id": r.id, "nombre": r.nombre, "subido": r.periodo, "filas": r.registros,
             "tamano": r.tamano, "hash": (r.hash or "")[:12], "actual": i == 0}
            for i, r in enumerate(rows)]


@router.get("/subproc-map")
async def get_subproc_map(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user)):
    rows = (await db.execute(select(AnsSubprocMap).order_by(AnsSubprocMap.subproceso))).scalars().all()
    return {"procesos": PROC35_ORDEN,
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
            "prog20_meses": meses}


@router.get("/prog20/detalle")
async def prog20_detalle(periodo: str, proceso: Optional[str] = None, zona: Optional[str] = None,
                         modo: str = "lcl", db: AsyncSession = Depends(get_db),
                         _: Usuario = Depends(get_current_active_user)):
    """Justifica una celda de las tablas NUEVAS de 3.5 (clickeable → verificable). modo:
    'lcl' = PLANEADO (LCL únicos de la 20'); 'invr' = INVR planeado (filas MO+MAT de la 20');
    'invr_ejec' = INVR ejecutado (trabajos de la programación Arasaka con MO real + material)."""
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
        return {"titulo": f"INVR ejecutado · {zona or 'todas'}",
                "explicacion": ("El sistema suma, de la programación de Arasaka, la mano de obra (MO) real "
                                "más los materiales de todos los trabajos ejecutados dentro del mes en esta "
                                "zona. Cada fila de abajo aporta su MO real + materiales al total."),
                "fuente": FUENTE_PROG,
                "nota": f"{len(filas)} trabajos · el total de la celda es la suma de MO real + Materiales = ${tot:,.0f}.",
                "columnas": ["fecha", "proceso", "lider", "lcl", "MO real ($)", "Materiales ($)"],
                "filas": filas, "total": len(filas)}

    mapa = await _subproc_map_dict(db)
    rows = (await db.execute(select(AnsProg20).where(AnsProg20.mes == periodo))).scalars().all()
    if modo == "invr":
        filas = []
        for r in rows:
            if zona and r.zona != zona:
                continue
            if not (r.valor_mo or r.valor_mat):
                continue
            filas.append({"lcl": r.lcl or "", "subproceso": r.subproceso or "",
                          "MO ($)": round(r.valor_mo or 0), "Materiales ($)": round(r.valor_mat or 0)})
        tot = sum(f["MO ($)"] + f["Materiales ($)"] for f in filas)
        return {"titulo": f"INVR planeado (20') · {zona or 'todas'}",
                "explicacion": ("El sistema suma, de la programación de los 20’, la mano de obra (columna "
                                "VALOR MO) más los materiales (columna VALOR MATERIALES) de todas las filas "
                                "de esta zona en el mes. Cada fila de abajo aporta su MO + materiales al total."),
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


@router.get("/detalle")
async def detalle(
    periodo: str, grupo: str,
    concepto: Optional[str] = None, zona: Optional[str] = None,
    bucket: Optional[str] = None, supervisor: Optional[str] = None,
    db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_active_user),
):
    """Devuelve las filas que JUSTIFICAN una celda de indicador (drill-down).
    grupo: 3.1/3.5 (casos de bolsa), 3.5P (filas de programación) o 3.2A/3.2B/3.2C/3.3/3.4/DINAMICA
    (registros STM)."""
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

    # ── Programación (3.2/3.3/3.4/Dinamica): filtra los registros STM ──
    regs = (await db.execute(select(AnsRegistro).where(AnsRegistro.periodo == periodo))).scalars().all()
    campo = _CAMPO_DETALLE.get(g)
    filas = []
    for r in regs:
        z = _zona_reg(r)
        if zona and z != zona:
            continue
        if supervisor and (r.nombre_responsable or "—") != supervisor:
            continue
        c = computar(r)
        if g == "3.2C":   # nodos: registros con nodos planeados/ejecutados
            if not (r.nodos_planeados or r.nodos_ejecutados):
                continue
            val = f"{r.nodos_ejecutados or 0}/{r.nodos_planeados or 0}"
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
        else:
            pq = f"El sistema calculó «{val}» para esta orden a partir de los datos del CD10 STM"
        filas.append({"protocolo": r.protocolo, "responsable": r.nombre_responsable or "—",
                      "resultado": val, "por qué": pq})
    explic_stm = {
        "3.2A": ("3.2 A (Oportunidad en la ejecución): por cada orden de trabajo, el sistema revisa si los "
                 "tiempos de ejecución se cumplieron o no, a partir de las fechas/horas del CD10 STM."),
        "3.2B": ("3.2 B (Efectividad en la apertura): por cada orden, el sistema revisa si la apertura se "
                 "hizo correctamente según las fechas/horas del CD10 STM."),
        "3.2C": ("3.2 C (Alcance): por cada orden, el sistema compara los nodos que se ejecutaron contra "
                 "los que estaban planeados."),
        "3.3": ("3.3 (Calidad de la información): por cada orden, el sistema revisa si la información de "
                "UUCC quedó completa y correcta."),
        "3.4": ("3.4 (Oportunidad en la programación): por cada orden, el sistema compara la fecha en que se "
                "DEBÍA enviar a validación con la fecha en que realmente se envió. CUMPLE si se envió al "
                "menos 1 día antes del plazo; si no, NO CUMPLE."),
        "DINAMICA": ("Dinámica: agrupa las órdenes por zona y supervisor y cuenta cuántas cumplen, cuántas "
                     "son N/A y cuántas no cumplen los tiempos."),
    }
    return {"titulo": " · ".join(x for x in [grupo, zona, supervisor, bucket] if x),
            "explicacion": explic_stm.get(g, "Órdenes de trabajo (CD10 STM) que sustentan este indicador."),
            "fuente": await _fuente_stm(db, periodo),
            "columnas": ["protocolo", "responsable", "resultado", "por qué"],
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
    "AR": '=+IF(T4=TRUE,"N/A",IF(AND(Q4="no",ISBLANK(AQ4)),"N/A",IF(DATA[[#This Row],[Excluible2]]="SI","CUMPLE",IF(ISBLANK(AQ4),"CUMPLE","NO CUMPLE"))))',
    "AY": '=DATA[[#This Row],[Cantidad Nodos planeados]]-DATA[[#This Row],[Cantidad Nodos ejecutados]]',
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
    "AO": "excluible_anulado", "AP": "obs_anulado", "AQ": "obs_uucc", "AS": "excluible2",
    "AT": "obs3", "AU": "nodos_planeados", "AV": "nodos_ejecutados", "AW": "nods_excluible",
    "AX": "obs_nodos",
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
    ws.tables["DATA"].ref = f"A3:AY{start + max(n, 1) - 1}"


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


@router.get("/export")
async def exportar(periodo: str, db: AsyncSession = Depends(get_db),
                   _: Usuario = Depends(get_current_active_user)):
    regs = (await db.execute(
        select(AnsRegistro).where(AnsRegistro.periodo == periodo).order_by(AnsRegistro.tipo, AnsRegistro.id)
    )).scalars().all()
    if not regs:
        raise HTTPException(status_code=404, detail=f"Sin registros para el periodo {periodo}")
    prog = await _indicadores_programacion(db, periodo)
    bol = await _indicadores_bolsas(db, periodo)
    imp = (await db.execute(select(AnsImpacto36).where(AnsImpacto36.periodo == periodo))).scalar_one_or_none()
    impacto36 = json.loads(imp.datos) if imp and imp.datos else None

    wb = _build_export(regs, prog, bol, periodo, impacto36)
    buf = io.BytesIO()
    wb.save(buf)
    data = _recalcular_xlsx(buf.getvalue())   # embebe valores (LibreOffice) → no se ve vacío
    fname = f"Calculo_ANS_{periodo}.xlsx"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
