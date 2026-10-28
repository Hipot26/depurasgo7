"""
Módulo de liquidación SGS — /liquidacion
=========================================
Sistema automatizado de liquidación de prestaciones (mano de obra).
Filosofía: margen de error = 0. Máxima prudencia. Replicar exactamente
el comportamiento humano del sistema.

Variables de entorno requeridas:
  LIQ_PASSWORD     — contraseña de acceso (texto plano en env var segura)
  LIQ_TOTP_SECRET  — secreto TOTP en base32 (compatible Google Authenticator)
  SECRET_KEY       — clave de firma de sesiones Flask

Para generar LIQ_TOTP_SECRET la primera vez:
  python -c "import pyotp; print(pyotp.random_base32())"
  → copiar el resultado como env var en Railway
  → agregar manualmente en Google Authenticator con el mismo secreto

Fases implementadas:
  Fase 1 — Importar plantilla + verificar contra SGS  ← ACTIVO
  Fase 2 — Motor de liquidación controlado             ← PRÓXIMO
"""

import io
import json
import logging
import os
import re
import sys
import threading
import uuid
import hmac
from datetime import datetime, timedelta
from functools import wraps

import unicodedata

import pyotp
import openpyxl
from flask import (Blueprint, render_template, request,
                   session, redirect, url_for)

# SGSClient desde el mismo directorio
sys.path.insert(0, os.path.dirname(__file__))
from sgs_client import SGSClient


# ── Blueprint ──────────────────────────────────────────────────────────────────
liq = Blueprint(
    "liquidacion", __name__,
    url_prefix="/liquidacion",
    template_folder="templates",
)


# ── Logger ─────────────────────────────────────────────────────────────────────
_log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(_log_dir, exist_ok=True)

log = logging.getLogger("liq")
if not log.handlers:
    _h = logging.FileHandler(
        os.path.join(_log_dir, "liquidacion.log"), encoding="utf-8"
    )
    _h.setFormatter(logging.Formatter(
        "%(asctime)s UTC | %(levelname)-8s | %(message)s"
    ))
    log.addHandler(_h)
log.setLevel(logging.INFO)


# ── Rate limiting ──────────────────────────────────────────────────────────────
_intentos: dict = {}   # ip -> {count, blocked_until}
MAX_INTENTOS    = 5
BLOQUEO_MINUTOS = 15


def _ip() -> str:
    raw = request.headers.get("X-Forwarded-For", request.remote_addr or "?")
    return raw.split(",")[0].strip()


def _esta_bloqueado() -> bool:
    e = _intentos.get(_ip())
    if not e:
        return False
    bu = e.get("blocked_until")
    if bu and datetime.utcnow() < bu:
        return True
    if bu and datetime.utcnow() >= bu:
        _intentos.pop(_ip(), None)
    return False


def _registrar_fallo():
    e = _intentos.setdefault(_ip(), {"count": 0, "blocked_until": None})
    e["count"] += 1
    if e["count"] >= MAX_INTENTOS:
        e["blocked_until"] = datetime.utcnow() + timedelta(minutes=BLOQUEO_MINUTOS)
        log.warning(f"IP BLOQUEADA {BLOQUEO_MINUTOS}min | {_ip()}")


def _limpiar_fallos():
    _intentos.pop(_ip(), None)


# ── Autenticación ──────────────────────────────────────────────────────────────
SESSION_TIMEOUT_H = 8


def _check_pwd(inp: str) -> bool:
    expected = os.environ.get("LIQ_PASSWORD", "")
    if not expected:
        return False
    try:
        return hmac.compare_digest(
            inp.encode("utf-8"), expected.encode("utf-8")
        )
    except Exception:
        return False


def _check_totp(code: str) -> bool:
    secret = os.environ.get("LIQ_TOTP_SECRET", "")
    if not secret:
        return False
    try:
        return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
    except Exception:
        return False


def _autenticado() -> bool:
    if not session.get("liq_auth"):
        return False
    ts_str = session.get("liq_ts")
    if not ts_str:
        return False
    try:
        delta = datetime.utcnow() - datetime.fromisoformat(ts_str)
        if delta.total_seconds() > SESSION_TIMEOUT_H * 3600:
            session.clear()
            return False
    except Exception:
        session.clear()
        return False
    return True


def require_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not _autenticado():
            return redirect(url_for("liquidacion.login"))
        return f(*args, **kwargs)
    return wrapped


# ── SGS client (reutilizable) ──────────────────────────────────────────────────
_sgs_cache: SGSClient = None


def _sgs() -> SGSClient:
    global _sgs_cache
    url = os.environ.get("SGS_URL", "")
    db  = os.environ.get("SGS_DB",  "")
    login    = os.environ.get("SGS_LOGIN",    "")
    password = os.environ.get("SGS_PASSWORD", "")

    if not all([url, login, password]):
        cfg_path = os.path.join(os.path.dirname(__file__), "config_sgs.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            url      = url      or cfg.get("url",      "")
            db       = db       or cfg.get("db",       "")
            login    = login    or cfg.get("login",    "")
            password = password or cfg.get("password", "")

    if not db:
        db = SGSClient.detectar_db(url) or ""

    # Reutilizar sesión activa
    if _sgs_cache and _sgs_cache.uid:
        try:
            _sgs_cache._call_kw(
                "res.users", "read", [[_sgs_cache.uid]], {"fields": ["id"]}
            )
            return _sgs_cache
        except Exception:
            pass

    c = SGSClient(url, db, login, password)
    c.autenticar()
    _sgs_cache = c
    return c


# ── Sesiones de liquidación (en memoria) ──────────────────────────────────────
_sesiones: dict = {}  # sid -> {state, rows, checks, ...}


# ── Parser de plantilla Excel ──────────────────────────────────────────────────
# Formatos soportados:
#   A) Plantilla SGS-Liq: columnas PRO | CODIGO | DESCRIPCION | CANTIDAD
#   B) SIP_MANO_OBRA:     columnas #   | NODO   | CODIGO      | DESCRIPCION | UNIDAD | CANTIDAD
#      (en este caso el usuario debe incluir una columna PRO o especificar el proyecto)

_HOJAS_PRIORITARIAS = [
    "LIQUIDACION", "LIQ", "PRESTACIONES", "ACTIVIDADES",
    "SIP_MANO_OBRA", "MANO_OBRA",
]

_MARCADORES_PRO    = {"PRO", "PROYECTO", "PROJECT", "ODM", "NODO_PRO", "NODO"}
_MARCADORES_COD    = {"CODIGO", "COD", "CODE", "PRESTACION", "ACTIVIDAD", "ACTIVITY"}
_MARCADORES_QTY    = {"CANTIDAD", "CANT", "QTY", "AMOUNT", "UNIDADES", "TOTAL"}
_MARCADORES_LIDER  = {"LIDER", "RESPONSABLE", "PARTNER", "TECNICO"}
_EXCLUIR_SI_CONTIENE = {"DESC", "NOMBRE", "OBSERV", "NOTA"}


def _parse_currency(val) -> float | None:
    """
    Convierte valores monetarios del Excel a float, manejando ambos formatos:
      US         → '$3,335,423.84'   (miles=coma, decimal=punto)
      Colombiano → '$3.335.423,84'   (miles=punto, decimal=coma)
    También acepta float/int directamente.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("$", "").replace(" ", "").replace("\xa0", "")
    if not s:
        return None
    has_dot   = "." in s
    has_comma = "," in s
    if has_dot and has_comma:
        if s.rfind(".") > s.rfind(","):   # último separador = punto → US format
            s = s.replace(",", "")
        else:                              # último separador = coma → formato colombiano
            s = s.replace(".", "").replace(",", ".")
    elif has_comma:
        # sola coma: si hay exactamente 1 y ≤2 decimales → decimal; si no → miles
        partes = s.split(",")
        if len(partes) == 2 and len(partes[1]) <= 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_dot and s.count(".") > 1:
        # múltiples puntos = separador de miles
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _extraer_total_mo(wb: openpyxl.Workbook) -> float | None:
    """
    Lee el total de mano de obra del Excel.
    Busca en SIP_MANO_OBRA la fila con label 'TOTAL MANO DE OBRA'
    y devuelve el primer valor numérico que encuentre en esa fila.
    """
    for hoja in ("SIP_MANO_OBRA", "MANO_OBRA"):
        if hoja not in wb.sheetnames:
            continue
        ws = wb[hoja]
        for r in range(1, ws.max_row + 1):
            label = _sin_tildes(str(ws.cell(r, 1).value or "").upper().strip())
            if "TOTAL" in label and ("MANO" in label or "OBRA" in label):
                for c in range(2, 15):
                    val = ws.cell(r, c).value
                    parsed = _parse_currency(val)
                    if parsed and parsed > 0:
                        return parsed
        break
    return None


def _sin_tildes(s: str) -> str:
    """Normaliza una cadena eliminando acentos para comparación robusta.
    'Código' → 'CODIGO', 'Cantidad' → 'CANTIDAD'
    """
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _col_match(v: str, marcadores: set, excluir: set = None) -> bool:
    """Compara sin acentos para soportar cabeceras como 'Código', 'Número', etc."""
    v_norm  = _sin_tildes(v)
    excluir = excluir or set()
    if any(e in v_norm for e in excluir):
        return False
    return any(m in v_norm for m in marcadores)


def _parsear_plantilla(wb: openpyxl.Workbook) -> tuple:
    """
    Extrae filas {pro, codigo, cantidad, lider} del Excel.
    Devuelve (rows: list, errores: list).
    """
    rows, errs = [], []

    # Seleccionar hoja
    ws = None
    for nombre in _HOJAS_PRIORITARIAS:
        if nombre in wb.sheetnames:
            ws = wb[nombre]
            break
    if ws is None:
        ws = wb.active

    # Detectar fila de cabecera (primeras 20 filas)
    # IMPORTANTE: la detección es POR FILA — evita que labels de metadatos
    # (ej. "OdM / Grafo:") contaminen la asignación de columnas.
    col = {"pro": None, "cod": None, "qty": None, "lider": None}
    header_row = None

    for r in range(1, 21):
        vals = [
            str(ws.cell(r, c).value or "").upper().strip()
            for c in range(1, 25)
        ]
        row_col = {"pro": None, "cod": None, "qty": None, "lider": None}
        for i, v in enumerate(vals):
            if not row_col["pro"]   and _col_match(v, _MARCADORES_PRO):
                row_col["pro"]   = i + 1
            if not row_col["cod"]   and _col_match(v, _MARCADORES_COD, _EXCLUIR_SI_CONTIENE):
                row_col["cod"]   = i + 1
            if not row_col["qty"]   and _col_match(v, _MARCADORES_QTY, _EXCLUIR_SI_CONTIENE):
                row_col["qty"]   = i + 1
            if not row_col["lider"] and _col_match(v, _MARCADORES_LIDER):
                row_col["lider"] = i + 1

        # La fila de cabecera válida debe tener al menos COD + QTY en la misma fila
        if row_col["cod"] and row_col["qty"]:
            col        = row_col
            header_row = r
            break

    if not header_row:
        errs.append(
            "No se detectaron columnas requeridas (CODIGO + CANTIDAD). "
            "Verifica que la plantilla tenga cabeceras en las primeras 20 filas."
        )
        return rows, errs

    # Extraer identificador DF/LCL del bloque de metadatos de SIP_MANO_OBRA.
    # Se busca en este orden de prioridad:
    #   1. "Ticket / PDL:" → contiene el DF (p.ej. DF202664312541)
    #   2. "OdM / Grafo:"  → a veces '0' (se ignora si es cero)
    #   3. "LCL:"          → identificador alternativo
    # Se ignoran valores que sean '0', vacíos o puramente numéricos sin prefijo.
    odm_meta = ""
    if ws.title in ("SIP_MANO_OBRA", "MANO_OBRA"):
        _PRIO_LABELS = [
            ("TICKET", "PDL"),   # prioridad 1 — campo más fiable
            ("ODM", "GRAFO"),    # prioridad 2 — suele ser '0' en algunos formatos
            ("LCL",),            # prioridad 3 — identificador alternativo
        ]
        for grupo in _PRIO_LABELS:
            for r_m in range(1, min(header_row + 1, 20)):
                label = _sin_tildes(str(ws.cell(r_m, 1).value or "").upper().strip())
                if any(lbl in label for lbl in grupo):
                    val = str(ws.cell(r_m, 2).value or "").strip()
                    # Saltar valores vacíos, '0', o puramente numéricos (sin prefijo DF/LCL)
                    if val and val != "0" and not val.isdigit():
                        odm_meta = val
                        break
            if odm_meta:
                break

        if odm_meta:
            errs.append(f"Modo SIP_MANO_OBRA detectado — DF/LCL: {odm_meta}")
        elif not col["pro"]:
            errs.append(
                "No se encontro columna PRO ni identificador DF/LCL en metadatos "
                "(Ticket/PDL, OdM o LCL). Verifica la plantilla."
            )

    # Leer datos fila a fila
    vistos = set()  # (pro, codigo) para detección de duplicados

    for r in range(header_row + 1, ws.max_row + 1):
        pro     = str(ws.cell(r, col["pro"]).value  or "").strip().upper() if col["pro"]   else ""
        cod     = str(ws.cell(r, col["cod"]).value  or "").strip().upper()
        lid     = str(ws.cell(r, col["lider"]).value or "").strip()        if col["lider"] else ""
        qty_raw = ws.cell(r, col["qty"]).value

        # Fila vacía o fila de totales (texto en columna de cantidad)
        if not cod:
            continue
        if qty_raw is None:
            continue

        # Cantidad
        try:
            qty = float(qty_raw)
        except (TypeError, ValueError):
            continue  # fila de totales con texto monetario — ignorar silenciosamente

        if qty == 0:
            continue

        # Duplicados dentro de la plantilla
        clave = (pro or odm_meta, cod)
        if clave in vistos:
            errs.append(f"Fila {r}: duplicado ({pro or odm_meta}, {cod}) — ignorado")
            continue
        vistos.add(clave)

        rows.append({
            "fila":     r,
            "pro":      pro,       # PRO-XXXXX, ID, nodo-num o vacío
            "codigo":   cod,
            "cantidad": qty,
            "lider":    lid,
            "_odm":     odm_meta,  # ODM del proyecto padre (para resolución en SGS)
        })

    return rows, errs


# ── Parser de materiales (CRUCE_SERGIO) ───────────────────────────────────────

def _parsear_materiales(wb: openpyxl.Workbook) -> tuple:
    """
    Extrae materiales de la hoja CRUCE_SERGIO del balance integrado.
    Columnas: # | Nodo | Código | Descripción | SIP Cant. | SGS Neto | Estado
    Devuelve (rows, errs).
    """
    rows, errs = [], []
    hoja = "CRUCE_SERGIO"
    if hoja not in wb.sheetnames:
        errs.append("Hoja 'CRUCE_SERGIO' no encontrada (Liquidación Sergio).")
        return rows, errs

    ws = wb[hoja]
    col = {"nodo": None, "cod": None, "desc": None, "sip": None, "sgs": None, "estado": None}
    header_row = None

    for r in range(1, min(ws.max_row + 1, 15)):
        vals = [_sin_tildes(str(ws.cell(r, c).value or "").upper().strip())
                for c in range(1, 10)]
        for i, v in enumerate(vals):
            if v == "NODO"                       and col["nodo"]   is None: col["nodo"]   = i + 1
            if ("CODIGO" in v or v == "COD")     and col["cod"]    is None: col["cod"]    = i + 1
            if "DESCRIPCION" in v                and col["desc"]   is None: col["desc"]   = i + 1
            if "SIP" in v and "CANT" in v        and col["sip"]    is None: col["sip"]    = i + 1
            if "SGS" in v and "NETO" in v        and col["sgs"]    is None: col["sgs"]    = i + 1
            if v == "ESTADO"                     and col["estado"] is None: col["estado"] = i + 1
        if col["cod"] and col["sgs"]:
            header_row = r
            break

    if not header_row:
        errs.append("Cabecera no detectada en CRUCE_SERGIO (busca 'Código' y 'SGS Neto').")
        return rows, errs

    current_nodo = "1"
    for r in range(header_row + 1, ws.max_row + 1):
        cell1 = str(ws.cell(r, 1).value or "").strip()
        cell1_n = _sin_tildes(cell1.upper())

        if "NODO:" in cell1_n:
            m = re.search(r"\d+", cell1)
            if m:
                current_nodo = m.group()
            continue
        if any(k in cell1_n for k in ("RESUMEN", "VERDE", "ROJO", "AMBAR", "GRIS")):
            continue

        cod = str(ws.cell(r, col["cod"]).value or "").strip()
        if not cod or not any(ch.isdigit() for ch in cod):
            continue

        nodo_v  = str(ws.cell(r, col["nodo"]).value or "").strip() if col["nodo"] else current_nodo
        desc    = str(ws.cell(r, col["desc"]).value or "").strip() if col["desc"] else ""
        estado  = str(ws.cell(r, col["estado"]).value or "").strip().upper() if col["estado"] else ""

        sip_raw = ws.cell(r, col["sip"]).value if col["sip"] else None
        sip_cant = None
        if sip_raw is not None and str(sip_raw).strip() not in ("", "—", "\u2014", "?"):
            try:
                sip_cant = float(sip_raw)
            except (TypeError, ValueError):
                sip_cant = None

        sgs_raw = ws.cell(r, col["sgs"]).value if col["sgs"] else None
        try:
            sgs_neto = float(sgs_raw) if sgs_raw is not None else 0.0
        except (TypeError, ValueError):
            sgs_neto = 0.0

        rows.append({
            "fila":          r,
            "nodo":          nodo_v or current_nodo,
            "codigo":        cod,
            "descripcion":   desc,
            "sip_cant":      sip_cant,       # cantidad SIP (None = sin movimiento SIP)
            "sgs_neto_excel": sgs_neto,      # neto SGS según Excel (despacho - devolución)
            "estado_excel":  estado,          # OK / SIN MOV / DESCUADRE / SOLO_SGS
        })

    return rows, errs


def _verificar_mat_fila(mat: dict, sgs_mat: dict | None) -> dict:
    """
    Compara un material del Excel (CRUCE_SERGIO) con su registro en project.material de SGS.
    El neto SGS real = despachado - devuelto.
    También detecta avance ya registrado para prevenir doble liquidación.

    Estados posibles:
      ok           — cantidades cuadran, advance=0 → lista para liquidar
      ya_liquidado — cantidades cuadran Y advance >= despachado → 100% ya liquidado
      avance_parcial — cantidades cuadran Y 0 < advance < despachado → el motor liquidará el delta
      sin_mov      — neto SGS = 0, advance = 0, no hay movimiento
      sin_mov_ok   — Excel dice 0 y no existe registro en SGS → coherente
      descuadre    — cantidades no cuadran → bloquea aprobación
      no_encontrado — código no existe en project.material de SGS
    """
    mc = {
        "mat":            mat,
        "sgs_mat":        sgs_mat,
        "ok":             False,
        "estado":         "no_encontrado",
        "sgs_despachado": None,
        "sgs_devuelto":   None,
        "sgs_neto":       None,
        "sgs_advance":    None,
        "sgs_balance":    None,
        "diferencia":     None,
    }

    excel_neto = mat["sgs_neto_excel"]

    if sgs_mat is None:
        if excel_neto == 0:
            mc["ok"]     = True
            mc["estado"] = "sin_mov_ok"   # Excel dice 0 y no existe en SGS → coherente
        else:
            mc["estado"] = "no_encontrado"
        return mc

    desp    = float(sgs_mat.get("despachado") or 0)
    devto   = float(sgs_mat.get("devuelto")   or 0)
    advance = float(sgs_mat.get("advance")    or 0)
    balance = float(sgs_mat.get("balance")    or 0)
    neto    = desp - devto
    diff    = abs(neto - excel_neto)

    mc["sgs_despachado"] = desp
    mc["sgs_devuelto"]   = devto
    mc["sgs_neto"]       = neto
    mc["sgs_advance"]    = advance
    mc["sgs_balance"]    = balance
    mc["diferencia"]     = diff

    if diff != 0:
        mc["ok"]     = False
        mc["estado"] = "descuadre"
        return mc

    # Cantidades cuadran — evaluar avance
    mc["ok"] = True
    if neto == 0 and advance == 0:
        mc["estado"] = "sin_mov"
    elif advance >= desp and desp > 0:
        mc["estado"] = "ya_liquidado"    # 100% consumido — el motor lo omitirá
    elif advance > 0:
        mc["estado"] = "avance_parcial"  # parcialmente consumido — motor liquida el delta
    else:
        mc["estado"] = "ok"              # pendiente completo

    return mc


# ── Verificación contra SGS ────────────────────────────────────────────────────

def _nodo_match(nombre: str, nodo_idx: int) -> bool:
    """
    Coincidencia EXACTA del número de nodo en el nombre del proyecto.
    Evita que 'NODO 1' coincida con 'NODO 10', 'NODO 11', 'NODO 19', etc.
    El patrón exige que tras el número NO venga otro dígito.
    Ejemplo: nodo_idx=1 → coincide con 'NODO 1', 'NODO 1 //', 'NODO 1_'
                          pero NO con 'NODO 10', 'NODO 19', 'NODO 100'
    """
    return bool(re.search(rf"NODO\s*{nodo_idx}(?!\d)", nombre.upper()))


def _verificar_fila(cliente: SGSClient, row: dict) -> dict:
    """
    Ejecuta todas las validaciones de una fila contra SGS.
    Retorna dict con {ok, errores, advertencias, datos...}
    """
    ck: dict = {
        "row":         row.copy(),
        "ok":          True,
        "errores":     [],
        "advertencias":[],
    }

    # ── 1. Resolución DF → nombre SGS → Nodo → PRO ────────────────────────────
    pro_val  = row["pro"]
    odm_meta = row.get("_odm", "")
    proy     = None
    _CAMPOS_PROY = [
        "id", "code", "name", "nodo",
        "contract_id", "formula_id", "parent_id",
        "responsible_id", "order_number",
    ]

    # Determinar el identificador DF a usar para la búsqueda
    df_id = odm_meta if odm_meta else (pro_val if not pro_val.isdigit() else "")

    try:
        if pro_val.startswith("PRO-"):
            # Código estándar PRO-XXXXXX — búsqueda directa
            proy = cliente.buscar_por_codigo(pro_val)

        elif df_id:
            # Flujo principal: DF/LCL → order_number → nodo → PRO
            # En SGS el campo order_number almacena exactamente el identificador
            # DF (p.ej. "DF202664312541") y el campo nodo almacena el índice
            # numérico del nodo ("1", "2", "3", …).
            nodo_idx = int(pro_val) if pro_val.isdigit() else 1

            # ── Estrategia 1: order_number exacto + nodo exacto (más precisa) ──
            candidatos_df = cliente.search_read(
                "project.dominion",
                [["order_number", "=", df_id]],
                _CAMPOS_PROY,
                limit=60,
            )
            log.info(
                f"order_number='{df_id}': {len(candidatos_df)} proyectos | "
                + str([(p.get("code",""), p.get("nodo","")) for p in candidatos_df[:5]])
            )

            candidatos = candidatos_df  # para guardar df_nodos más abajo

            if candidatos_df:
                if len(candidatos_df) == 1:
                    # Solo un nodo para este DF → usar directamente
                    proy = candidatos_df[0]
                    if str(candidatos_df[0].get("nodo","")) != str(nodo_idx):
                        ck["advertencias"].append(
                            f"El DF '{df_id}' tiene un único nodo (nodo="
                            f"{candidatos_df[0].get('nodo','?')!r}) pero la "
                            f"plantilla indica nodo {nodo_idx}. "
                            "Verificar que corresponde al proyecto correcto."
                        )
                else:
                    # Varios nodos — buscar el que coincida con nodo_idx
                    # El campo nodo puede ser '1', 'N1', '01'; normalizamos
                    # extrayendo el primer número del valor.
                    def _nodo_num(val) -> int:
                        if not val:
                            return -1
                        m = re.search(r"\d+", str(val))
                        return int(m.group()) if m else -1

                    match_nodo = [
                        p for p in candidatos_df
                        if _nodo_num(p.get("nodo")) == nodo_idx
                    ]
                    log.info(
                        f"nodo={nodo_idx} match: {len(match_nodo)} | "
                        + str([p.get("code","") for p in match_nodo])
                    )

                    if len(match_nodo) == 1:
                        proy = match_nodo[0]
                    elif len(match_nodo) > 1:
                        ck["advertencias"].append(
                            f"{len(match_nodo)} proyectos con nodo={nodo_idx} "
                            f"bajo DF '{df_id}'. Se usa el primero."
                        )
                        proy = match_nodo[0]
                    else:
                        # Sin nodo exacto — error descriptivo
                        nodos_disp = [
                            f"{p.get('code','?')}(nodo={p.get('nodo','?')})"
                            for p in candidatos_df
                        ]
                        ck["ok"] = False
                        ck["errores"].append(
                            f"No se encontró nodo {nodo_idx} para DF '{df_id}'. "
                            f"Nodos en SGS: {', '.join(nodos_disp)}"
                        )
                        ck["df_nodos"] = [
                            {"id": p["id"], "code": p.get("code",""), "name": p.get("name","")}
                            for p in candidatos_df
                        ]
                        return ck

            # ── Estrategia 2: fallback — nombre empieza con el DF ────────────
            if not candidatos_df:
                candidatos = cliente.search_read(
                    "project.dominion",
                    [["name", "=like", df_id + "%"]],
                    _CAMPOS_PROY,
                    limit=60,
                )
                log.info(
                    f"name =like '{df_id}%': {len(candidatos)} | "
                    + str([p.get("name","")[:40] for p in candidatos[:3]])
                )
                if not candidatos:
                    candidatos = cliente.search_read(
                        "project.dominion",
                        [["name", "ilike", df_id]],
                        _CAMPOS_PROY,
                        limit=60,
                    )
                    log.info(
                        f"name ilike '{df_id}': {len(candidatos)} | "
                        + str([p.get("name","")[:40] for p in candidatos[:3]])
                    )

                if candidatos:
                    match_exacto = [
                        p for p in candidatos
                        if _nodo_match(p.get("name") or "", nodo_idx)
                    ]
                    if len(match_exacto) == 1:
                        proy = match_exacto[0]
                    elif len(match_exacto) > 1:
                        ck["advertencias"].append(
                            f"{len(match_exacto)} proyectos con NODO {nodo_idx} "
                            "en el nombre. Se usa el primero."
                        )
                        proy = match_exacto[0]
                    elif len(candidatos) == 1:
                        proy = candidatos[0]
                    else:
                        ck["ok"] = False
                        ck["errores"].append(
                            f"No se encontró NODO {nodo_idx} exacto para DF '{df_id}'. "
                            f"{len(candidatos)} candidatos en SGS."
                        )
                        ck["df_nodos"] = [
                            {"id": p["id"], "code": p.get("code",""), "name": p.get("name","")}
                            for p in candidatos[:15]
                        ]
                        return ck

            # Guardar todos los nodos del DF para la tabla de verificación
            ck["df_nodos"] = [
                {"id": p["id"], "code": p.get("code",""), "name": p.get("name","")}
                for p in candidatos
            ]

        elif pro_val.isdigit():
            # ID numérico directo de SGS
            proy = cliente.leer_proyecto(int(pro_val))

    except Exception as ex:
        ck["ok"] = False
        ck["errores"].append(f"Error resolviendo proyecto: {ex}")
        return ck

    if not proy:
        ck["ok"] = False
        ck["errores"].append(
            f"Proyecto no encontrado en SGS para DF='{df_id}'"
        )
        return ck

    # ── Datos de identificación del check ────────────────────────────────────
    ck["pid"]        = proy["id"]
    ck["pro_code"]   = proy.get("code", "")
    ck["pnombre"]    = proy.get("name", "")
    ck["nodo_sgs"]   = proy.get("nodo", "") or ""
    ck["df_detectado"] = df_id
    ck["cid"]        = (proy.get("contract_id")  or [None])[0]
    ck["contrato"]   = (proy.get("contract_id")  or [None, ""])[1]
    ck["formula"]    = (proy.get("formula_id")   or [None, ""])[1]
    ck["fid"]        = (proy.get("formula_id")   or [None])[0]
    ck["supervisor"] = (proy.get("responsible_id") or [None, ""])[1]

    # ── 2. Actividad existe en catálogo ───────────────────────────────────────
    try:
        cat = cliente.search_read(
            "activities",
            [["code", "=", row["codigo"]]],
            ["id", "name", "code", "customer_contracts_id"],
            limit=5,
        )
    except Exception as ex:
        ck["ok"] = False
        ck["errores"].append(f"Error buscando actividad en catálogo: {ex}")
        return ck

    if not cat:
        ck["ok"] = False
        ck["errores"].append(f"Código '{row['codigo']}' no existe en catálogo de actividades")
        return ck

    ck["cat_id"]  = cat[0]["id"]
    ck["cat_nom"] = cat[0]["name"]
    act_ctr_id    = (cat[0].get("customer_contracts_id") or [None])[0]
    act_ctr_nom   = (cat[0].get("customer_contracts_id") or [None, ""])[1]

    # ── 3. Contrato compatible ─────────────────────────────────────────────────
    proy_ctr_id = (proy.get("contract_id") or [None])[0]
    if proy_ctr_id and act_ctr_id and proy_ctr_id != act_ctr_id:
        ck["ok"] = False
        ck["errores"].append(
            f"Contrato incompatible: proyecto={ck['contrato']}, "
            f"actividad={act_ctr_nom}"
        )

    # ── 4. Líder del proyecto ─────────────────────────────────────────────────
    try:
        equipo = cliente.search_read(
            "project.team",
            [["project_id", "=", proy["id"]]],
            ["id", "partner_id"],
            limit=10,
        )
    except Exception:
        equipo = []

    if not equipo:
        ck["ok"] = False
        ck["errores"].append("Proyecto sin líder asignado en el equipo")
    else:
        ck["partner_id"]     = equipo[0]["partner_id"][0]
        ck["partner_nombre"] = equipo[0]["partner_id"][1]
        if len(equipo) > 1:
            nombres = ", ".join(e["partner_id"][1] for e in equipo)
            ck["advertencias"].append(
                f"Proyecto con {len(equipo)} líderes ({nombres}). "
                "Se usará el primero."
            )

    # ── 5. ¿Ya existe esta actividad en el proyecto? (sobre-liquidación) ──────
    try:
        existentes = cliente.search_read(
            "project.activities",
            [
                ["project_id",    "=", proy["id"]],
                ["activity_code", "=", row["codigo"]],
            ],
            ["id", "amount", "advance", "balance"],
            limit=1,
        )
    except Exception:
        existentes = []

    if existentes:
        ea = existentes[0]
        ck["act_existente"] = {
            "id":      ea["id"],
            "amount":  ea["amount"],
            "advance": ea["advance"],
            "balance": ea["balance"],
        }

        # ── Detección de doble liquidación ────────────────────────────────────
        if ea["amount"] > 0 and ea["advance"] >= ea["amount"]:
            # Ya liquidada al 100% — bloquear con error explícito
            ck["ok"] = False
            ck["ya_liquidada"] = True
            ck["errores"].append(
                f"DOBLE LIQUIDACION DETECTADA: esta actividad ya fue liquidada "
                f"al 100% (proyectado={ea['amount']}, avance={ea['advance']}). "
                "Omite esta fila o revisa el origen del archivo."
            )
        elif ea["advance"] > 0:
            nuevo_advance = ea["advance"] + row["cantidad"]
            if nuevo_advance > ea["amount"]:
                ck["ok"] = False
                ck["errores"].append(
                    f"Sobre-liquidación detectada: proyectado={ea['amount']}, "
                    f"avance actual={ea['advance']}, solicitud={row['cantidad']}, "
                    f"resultado={nuevo_advance:.2f} > {ea['amount']:.2f}"
                )
            elif nuevo_advance == ea["amount"]:
                ck["advertencias"].append(
                    "Esta liquidación dejará la actividad al 100%."
                )
            else:
                ck["advertencias"].append(
                    f"Actividad con avance previo ({ea['advance']} de {ea['amount']}). "
                    "Verifica que no sea una liquidación duplicada."
                )
        else:
            nuevo_advance = ea["advance"] + row["cantidad"]
            if nuevo_advance > ea["amount"]:
                ck["ok"] = False
                ck["errores"].append(
                    f"Sobre-liquidación detectada: proyectado={ea['amount']}, "
                    f"avance actual={ea['advance']}, solicitud={row['cantidad']}, "
                    f"resultado={nuevo_advance:.2f} > {ea['amount']:.2f}"
                )
            elif nuevo_advance == ea["amount"]:
                ck["advertencias"].append(
                    "Esta liquidación dejará la actividad al 100%."
                )
    return ck


# ── Motor de liquidación (Fase 2) ─────────────────────────────────────────────

def _ejecutar_liquidacion(sid: str):
    """
    Ejecuta la liquidación de todas las filas verificadas.
    Corre en hilo de fondo; actualiza _sesiones[sid]['progreso'] en tiempo real.

    Por cada fila:
      1. Busca o crea el registro project.activities en el proyecto
      2. Crea una línea project.activities.details con state='done'

    Política: margen de error = 0 → el motor se detiene ante el primer error.
    """
    s = _sesiones.get(sid)
    if not s:
        return

    prog = s["progreso"]
    prog["estado"] = "ejecutando"
    hoy = datetime.utcnow().strftime("%Y-%m-%d")

    def _step(fila, cod, estado, msg):
        entry = {
            "fila":   fila,
            "cod":    cod,
            "estado": estado,   # info | ok | error | skip
            "msg":    msg,
            "ts":     datetime.utcnow().strftime("%H:%M:%S"),
        }
        prog["log"].append(entry)
        log.info(f"[{sid[:8]}] {estado.upper()} fila={fila} {cod}: {msg}")

    # Conectar a SGS
    try:
        cliente = _sgs()
    except Exception as ex:
        prog["estado"]       = "error"
        prog["error_global"] = f"Error conectando a SGS: {ex}"
        log.error(f"[{sid[:8]}] SGS connection failed: {ex}")
        return

    det_ids = []   # IDs de los project.activities.details creados en esta ejecución

    for row, ck in zip(s["rows"], s["checks"]):
        fila = row["fila"]
        cod  = row["codigo"]
        qty  = row["cantidad"]

        if not ck.get("ok"):
            _step(fila, cod, "skip", "Fila con errores de verificacion — omitida")
            continue

        pid          = ck["pid"]
        cat_id       = ck.get("cat_id")
        cat_nom      = ck.get("cat_nom", cod)
        fid          = ck.get("fid")
        cid          = ck.get("cid")
        partner_id   = ck.get("partner_id")
        act_existente = ck.get("act_existente")

        try:
            # ── 1. Obtener o crear project.activities ────────────────────────
            if act_existente:
                act_id = act_existente["id"]
                _step(fila, cod, "info",
                      f"Usando actividad existente #{act_id} "
                      f"(proyectado={act_existente['amount']}, "
                      f"avance={act_existente['advance']})")
            else:
                act_vals = {
                    "project_id":    pid,
                    "activity_code": cod,
                    "amount":        qty,
                    "activities_id": cat_id,   # campo obligatorio
                }
                if fid:
                    act_vals["formula"] = fid
                act_id = cliente.create("project.activities", act_vals)
                _step(fila, cod, "info", f"Actividad creada en proyecto → #{act_id}")

            # ── 2. Crear línea de detalle (liquidación real) ──────────────────
            det_vals = {
                "project_activities_id": act_id,
                "amount":                qty,
                "partner_id":            partner_id,
                "state":                 "done",
                "date":                  hoy,
                "production_type":       "mix",
            }
            if cat_id:
                det_vals["activities_id"] = cat_id
            if cat_nom:
                det_vals["activities_name"] = cat_nom
            if cid:
                det_vals["contract_id"] = cid

            det_id = cliente.create("project.activities.details", det_vals)
            det_ids.append(det_id)
            _step(fila, cod, "ok",
                  f"Liquidado — detail #{det_id} | "
                  f"qty={qty} | lider={ck.get('partner_nombre','?')}")
            prog["ok"] += 1

        except Exception as ex:
            _step(fila, cod, "error", str(ex))
            prog["err"]          += 1
            prog["estado"]        = "error"
            prog["error_global"]  = f"Error en fila {fila} ({cod}): {ex}"
            log.error(f"[{sid[:8]}] ABORT en fila={fila} {cod}: {ex}")
            return  # margen de error = 0 → parar

    # ── Verificación final de totales monetarios ──────────────────────────────
    prog["estado"] = "verificando"
    _step(0, "—", "info", "Verificando totales monetarios contra SGS...")

    verificacion = {
        "excel_total": s.get("excel_total_mo"),
        "sgs_total":   None,
        "diferencia":  None,
        "ok":          None,
        "det_ids":     det_ids,
    }

    try:
        if det_ids:
            dets_final = cliente.search_read(
                "project.activities.details",
                [["id", "in", det_ids]],
                ["id", "advanced_price", "amount"],
                limit=len(det_ids) + 5,
            )
            sgs_total = sum(
                (d.get("advanced_price") or 0)
                for d in dets_final
                if (d.get("amount") or 0) > 0
            )
            verificacion["sgs_total"] = round(sgs_total, 2)

            excel_total = s.get("excel_total_mo")
            if excel_total is not None:
                diferencia = abs(sgs_total - excel_total)
                verificacion["diferencia"] = round(diferencia, 2)
                verificacion["ok"]         = diferencia < 1.0  # tolerancia $1

                if verificacion["ok"]:
                    _step(0, "—", "ok",
                          f"Verificacion CORRECTA | "
                          f"Excel={excel_total:,.2f} | SGS={sgs_total:,.2f} | "
                          f"Diferencia={diferencia:.2f}")
                else:
                    _step(0, "—", "error",
                          f"ALERTA: diferencia de {diferencia:,.2f} | "
                          f"Excel={excel_total:,.2f} | SGS={sgs_total:,.2f}")
                    log.warning(
                        f"[{sid[:8]}] Diferencia monetaria: "
                        f"Excel={excel_total} SGS={sgs_total} diff={diferencia}"
                    )
            else:
                _step(0, "—", "info",
                      f"Total SGS liquidado: {sgs_total:,.2f} "
                      "(no hay total Excel para comparar)")
        else:
            _step(0, "—", "info", "Sin detalles creados — verificacion omitida")

    except Exception as ex:
        _step(0, "—", "error", f"Error en verificacion de totales: {ex}")
        log.error(f"[{sid[:8]}] Error verificacion totales: {ex}")
        verificacion["error"] = str(ex)

    prog["verificacion"] = verificacion

    # ── Cierre ────────────────────────────────────────────────────────────────
    prog["estado"] = "completado"
    s["state"]     = "liquidado"
    s["liquidado_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    log.info(
        f"[{sid[:8]}] Liquidacion COMPLETADA | "
        f"{prog['ok']} OK | {prog['err']} ERR | archivo={s['filename']}"
    )


# ── Motor de materiales (Fase 3) ──────────────────────────────────────────────

def _obtener_tray_info(cliente: SGSClient, pid: int) -> dict:
    """
    Construye un índice product_code → {partner_id, partner_nombre, lot_ids, tracking}
    leyendo todas las bandejas de despacho aprobadas del proyecto.
    """
    from sgs_client import parsear_producto as _pp
    try:
        bandejas = cliente.search_read(
            "project.tray",
            [["project_id", "=", pid], ["type", "=", "deliver"],
             ["state", "=", "approved"]],
            ["id", "name", "partner_id", "project_tray_material_ids"],
            limit=50,
        )
    except Exception:
        bandejas = []

    info: dict = {}  # product_code -> dict
    for ban in bandejas:
        mat_ids = ban.get("project_tray_material_ids") or []
        if not mat_ids:
            continue
        try:
            tms = cliente.search_read(
                "project.tray.material",
                [["id", "in", mat_ids]],
                ["id", "product_id", "partner_id",
                 "lot_ids", "product_tracking", "deliver_quantity"],
                limit=300,
            )
        except Exception:
            continue
        for tm in tms:
            _, alt_code, _ = _pp(tm.get("product_id"))
            if not alt_code:
                continue
            pid_field = tm.get("partner_id")
            info.setdefault(alt_code, {
                "partner_id":     pid_field[0] if pid_field else None,
                "partner_nombre": pid_field[1] if pid_field else "?",
                "lot_ids":        tm.get("lot_ids") or [],
                "tracking":       tm.get("product_tracking", "none"),
            })
    return info


def _ejecutar_liquidacion_materiales(sid: str):
    """
    Motor de liquidación de materiales — corre en hilo de fondo.

    Política idempotente:
      • Si advance >= despachado → ya liquidado, ignorar.
      • Si 0 < advance < despachado → parcialmente liquidado, liquidar solo el delta.
      • Si advance == 0 → liquidar despachado completo.

    Por cada material:
      1. Crea project.material.details (trazabilidad por líder).
      2. Escribe advance y balance en project.material.
    """
    s = _sesiones.get(sid)
    if not s:
        return

    prog = s["progreso_mat"]
    prog["estado"] = "ejecutando"
    hoy = datetime.utcnow().strftime("%Y-%m-%d")

    def _step(fila, cod, estado, msg):
        entry = {
            "fila":   fila,
            "cod":    cod,
            "estado": estado,
            "msg":    msg,
            "ts":     datetime.utcnow().strftime("%H:%M:%S"),
        }
        prog["log"].append(entry)
        log.info(f"[MAT:{sid[:8]}] {estado.upper()} fila={fila} {cod}: {msg}")

    # ── Conectar a SGS ────────────────────────────────────────────────────────
    try:
        cliente = _sgs()
    except Exception as ex:
        prog["estado"]       = "error"
        prog["error_global"] = f"Error conectando a SGS: {ex}"
        return

    # ── Obtener pid desde checks ───────────────────────────────────────────────
    pid = next((ck.get("pid") for ck in s.get("checks", []) if ck.get("pid")), None)
    if not pid:
        prog["estado"]       = "error"
        prog["error_global"] = "Proyecto SGS no resuelto — verifica la Mano de Obra primero."
        return

    # ── Info de bandejas (líder por material) ─────────────────────────────────
    _step(0, "—", "info", f"Leyendo bandejas de despacho del proyecto id={pid}...")
    tray_info = _obtener_tray_info(cliente, pid)
    _step(0, "—", "info",
          f"{len(tray_info)} materiales encontrados en bandejas: {list(tray_info.keys())}")

    # ── Leer estado actual de project.material (advance fresco de SGS) ────────
    try:
        pm_list = cliente.search_read(
            "project.material",
            [["project_id", "=", pid]],
            ["id", "product_code", "despachado", "devuelto",
             "advance", "balance", "is_pending"],
            limit=500,
        )
    except Exception as ex:
        prog["estado"]       = "error"
        prog["error_global"] = f"Error leyendo materiales de SGS: {ex}"
        return

    pm_by_code = {m["product_code"]: m for m in pm_list}
    _step(0, "—", "info",
          f"project.material leídos: {len(pm_by_code)} registros en PRO-{pid}")

    # ── Leer detalles existentes para idempotencia real (no usar advance) ─────
    try:
        all_details = cliente.search_read(
            "project.material.details",
            [["project_material_id.project_id", "=", pid]],
            ["project_material_id", "amount"],
            limit=1000,
        )
        details_sum_by_pm: dict = {}
        for d in all_details:
            pm_ref = d.get("project_material_id")
            if pm_ref:
                pm_ref_id = pm_ref[0] if isinstance(pm_ref, list) else pm_ref
                details_sum_by_pm[pm_ref_id] = (
                    details_sum_by_pm.get(pm_ref_id, 0.0) + float(d.get("amount") or 0)
                )
        _step(0, "—", "info",
              f"Detalles existentes: {len(all_details)} registros, "
              f"{len(details_sum_by_pm)} materiales con advance real")
    except Exception as ex:
        _step(0, "—", "info",
              f"No se pudo leer detalles (fallback a campo advance): {ex}")
        details_sum_by_pm = {}

    # ── Procesar cada material ────────────────────────────────────────────────
    mat_checks = s.get("mat_checks", [])
    aprobados  = s.get("mat_indices_aprobados", None)  # None = todos (retrocompat.)
    prog["total"] = len(mat_checks)

    # Guarda pm_ids ya procesados en este run para prevenir doble liquidación
    # cuando el mismo código aparece en varios nodos del Excel pero comparte
    # el mismo project.material en SGS (mismo pid, mismo código).
    pm_ids_procesados: set = set()

    for idx, mc in enumerate(mat_checks):
        mat  = mc["mat"]
        cod  = mat["codigo"]
        fila = mat["fila"]

        # Solo procesar los índices que el usuario seleccionó con check
        if aprobados is not None and idx not in aprobados:
            _step(fila, cod, "skip", "No seleccionado por el usuario — omitido")
            prog["skip"] = prog.get("skip", 0) + 1
            continue

        # Material ignorado en la verificación (descuadre/no encontrado)
        # Solo liquidamos los que tienen ok=True en la verificación
        if not mc.get("ok"):
            estado_v = mc.get("estado", "?")
            _step(fila, cod, "skip",
                  f"Verificación previa: {estado_v} — omitido por seguridad")
            prog["skip"] = prog.get("skip", 0) + 1
            continue

        # Registro en project.material
        pm = pm_by_code.get(cod)
        if pm is None:
            _step(fila, cod, "skip",
                  "No encontrado en project.material de SGS — omitido")
            prog["skip"] = prog.get("skip", 0) + 1
            continue

        pm_id        = pm["id"]
        despachado   = float(pm.get("despachado") or 0)
        # Usar suma real de detalles para idempotencia; advance field es inestable
        advance_act  = details_sum_by_pm.get(pm_id, float(pm.get("advance") or 0))
        pendiente    = round(despachado - advance_act, 4)

        # ── Protección doble liquidación: mismo pm_id en este run ────────────
        if pm_id in pm_ids_procesados:
            _step(fila, cod, "skip",
                  f"pm#{pm_id} ya procesado en este run (mismo código en otro nodo) — ignorado")
            prog["skip"] = prog.get("skip", 0) + 1
            continue

        # ── Control idempotente ───────────────────────────────────────────────
        if pendiente <= 0:
            _step(fila, cod, "skip",
                  f"Ya liquidado ({advance_act:.2f}/{despachado:.2f}) — ignorado")
            prog["ya_liq"] = prog.get("ya_liq", 0) + 1
            continue

        if advance_act > 0:
            _step(fila, cod, "info",
                  f"Liquidacion parcial detectada: advance={advance_act:.2f}, "
                  f"despachado={despachado:.2f} — liquidando delta={pendiente:.2f}")
            prog["parcial"] = prog.get("parcial", 0) + 1

        # ── Líder del despacho ────────────────────────────────────────────────
        ti = tray_info.get(cod, {})
        partner_id = ti.get("partner_id")
        if not partner_id:
            _step(fila, cod, "error",
                  "Líder no encontrado en bandejas de despacho — omitido")
            prog["err"] += 1
            continue

        # ── Lot_id para materiales serializados ───────────────────────────────
        lot_id = None
        if ti.get("tracking") == "serial":
            lot_ids = ti.get("lot_ids", [])
            if len(lot_ids) == 1:
                lot_id = lot_ids[0]
                _step(fila, cod, "info",
                      f"Serial detectado: lot_id={lot_id}")
            else:
                _step(fila, cod, "info",
                      f"Tracking=serial pero {len(lot_ids)} lots en bandeja "
                      f"— se liquida sin número de serie")

        # ── Crear project.material.details ────────────────────────────────────
        # Replicar exactamente el proceso humano: crear el detalle y dejar que
        # Odoo actualice advance/balance/stock automáticamente via ORM.
        # NO escribir advance/balance explícitamente — el human no lo hace
        # y hacerlo genera conflictos con el cómputo automático de Odoo.
        try:
            det_vals = {
                "project_material_id": pm_id,
                "partner_id":          partner_id,
                "amount":              pendiente,
                "date":                hoy,
                "state":               "done",
            }
            if lot_id:
                det_vals["lot_id"] = lot_id

            det_id = cliente.create("project.material.details", det_vals)
        except Exception as ex:
            _step(fila, cod, "error", f"Error creando detalle: {ex}")
            prog["err"] += 1
            continue

        pm_ids_procesados.add(pm_id)
        _step(fila, cod, "ok",
              f"Liquidado {pendiente:.2f} uds | "
              f"lider={ti.get('partner_nombre','?')} | "
              f"detail=#{det_id}")
        prog["ok"] += 1

    # ── Resumen final ─────────────────────────────────────────────────────────
    prog["estado"] = "completado"
    s["mat_liquidado_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    s["state"] = "mat_liquidado"
    log.info(
        f"[MAT:{sid[:8]}] COMPLETADO | "
        f"ok={prog['ok']} | ya_liq={prog.get('ya_liq',0)} | "
        f"parcial={prog.get('parcial',0)} | err={prog['err']} | "
        f"archivo={s['filename']}"
    )


# ── Rutas ──────────────────────────────────────────────────────────────────────

@liq.route("/")
@require_auth
def dashboard():
    # Leer últimas N líneas del log para mostrarlas en el dashboard
    log_lines = _leer_log_reciente(20)
    return render_template("liq_dashboard.html", log_lines=log_lines)


@liq.route("/login", methods=["GET", "POST"])
def login():
    if _autenticado():
        return redirect(url_for("liquidacion.dashboard"))

    error = None
    paso  = "password"

    if request.method == "POST":
        paso = request.form.get("paso", "password")

        if _esta_bloqueado():
            log.warning(f"Intento bloqueado | {_ip()}")
            error = (
                f"Acceso bloqueado por {BLOQUEO_MINUTOS} minutos "
                f"({MAX_INTENTOS} intentos fallidos). Intenta más tarde."
            )
            paso = "password"

        elif paso == "password":
            if _check_pwd(request.form.get("password", "")):
                session["liq_pwd_ok"] = True
                log.info(f"Paso 1 OK (contrasena) | {_ip()}")
                return render_template("liq_login.html", paso="totp")
            else:
                _registrar_fallo()
                log.warning(f"Contrasena incorrecta | {_ip()}")
                error = "Contraseña incorrecta."
                paso  = "password"

        elif paso == "totp":
            if not session.get("liq_pwd_ok"):
                return redirect(url_for("liquidacion.login"))
            if _check_totp(request.form.get("totp", "")):
                _limpiar_fallos()
                session.pop("liq_pwd_ok", None)
                session["liq_auth"] = True
                session["liq_ts"]   = datetime.utcnow().isoformat()
                log.info(f"Login completo (2FA) | {_ip()}")
                return redirect(url_for("liquidacion.dashboard"))
            else:
                _registrar_fallo()
                log.warning(f"TOTP incorrecto | {_ip()}")
                error = "Código de verificación incorrecto."
                paso  = "totp"

    return render_template("liq_login.html", paso=paso, error=error)


@liq.route("/logout")
def logout():
    ip = _ip()
    session.clear()
    log.info(f"Logout | {ip}")
    return redirect(url_for("liquidacion.login"))


@liq.route("/upload", methods=["POST"])
@require_auth
def upload():
    f = request.files.get("archivo")
    if not f or not f.filename:
        return render_template("liq_dashboard.html",
                               error="No se seleccionó ningún archivo.",
                               log_lines=_leer_log_reciente(20))

    fname = f.filename.strip()
    if not fname.lower().endswith(".xlsx"):
        return render_template("liq_dashboard.html",
                               error="Solo se aceptan archivos .xlsx",
                               log_lines=_leer_log_reciente(20))

    try:
        raw  = f.read()
        wb   = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
        rows, errs = _parsear_plantilla(wb)
    except Exception as e:
        log.error(f"Error parseando {fname}: {e}")
        return render_template("liq_dashboard.html",
                               error=f"Error leyendo el archivo: {e}",
                               log_lines=_leer_log_reciente(20))

    excel_total_mo = _extraer_total_mo(wb)
    mat_rows, mat_errs = _parsear_materiales(wb)

    sid = str(uuid.uuid4())
    _sesiones[sid] = {
        "created_at":    datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "filename":      fname,
        "state":         "uploaded",
        "rows":          rows,
        "parse_errs":    errs,
        "checks":        [],
        "all_ok":        False,
        "excel_total_mo": excel_total_mo,
        "mat_rows":      mat_rows,     # materiales de CRUCE_SERGIO
        "mat_errs":      mat_errs,     # errores del parser de materiales
        "mat_checks":    [],
        "mat_all_ok":    False,
        "mat_ok":        False,        # aprobado manualmente por usuario
    }
    log.info(f"Plantilla cargada | {fname} | {len(rows)} filas | sid={sid[:8]}")
    return redirect(url_for("liquidacion.preview", sid=sid))


@liq.route("/preview/<sid>")
@require_auth
def preview(sid):
    s = _sesiones.get(sid)
    if not s:
        return redirect(url_for("liquidacion.dashboard"))
    return render_template("liq_preview.html", sid=sid, s=s)


@liq.route("/verificar/<sid>", methods=["POST"])
@require_auth
def verificar(sid):
    s = _sesiones.get(sid)
    if not s:
        return redirect(url_for("liquidacion.dashboard"))

    try:
        cliente = _sgs()
    except Exception as e:
        log.error(f"SGS connection error: {e}")
        return render_template("liq_preview.html", sid=sid, s=s,
                               sgs_error=f"Error conectando a SGS: {e}")

    checks = []
    for row in s["rows"]:
        ck = _verificar_fila(cliente, row)
        checks.append(ck)

    s["checks"] = checks
    s["all_ok"] = all(c["ok"] for c in checks)
    s["state"]  = "verificado"

    # Detectar riesgo de doble liquidación: ≥50% de actividades ya con avance > 0
    con_avance = sum(
        1 for c in checks
        if c.get("act_existente") and c["act_existente"].get("advance", 0) > 0
    )
    s["doble_liq_riesgo"] = (len(checks) > 0 and con_avance / len(checks) >= 0.5)
    s["doble_liq_n"] = con_avance

    # Detectar si TODAS las filas MO están bloqueadas por doble liquidación al 100%
    # → habilita el flujo de "solo materiales"
    todas_ya_liq = (
        len(checks) > 0
        and all(c.get("ya_liquidada") for c in checks)
    )
    s["mo_ya_liquidada"] = todas_ya_liq

    n_ok  = sum(1 for c in checks if c["ok"])
    n_err = len(checks) - n_ok
    log.info(
        f"Verificacion | sid={sid[:8]} | {n_ok}/{len(checks)} OK | "
        f"{n_err} errores"
    )

    return render_template("liq_verify.html", sid=sid, s=s)


@liq.route("/materiales/<sid>", methods=["GET", "POST"])
@require_auth
def materiales(sid):
    s = _sesiones.get(sid)
    if not s:
        return redirect(url_for("liquidacion.dashboard"))

    if request.method == "POST":
        # Confirmación manual — solo los materiales que el usuario marcó
        confirmados = request.form.getlist("mat_confirmado")
        if not confirmados:
            return render_template("liq_materiales.html", sid=sid, s=s,
                                   confirm_error="Selecciona al menos 1 material para liquidar.")
        s["mat_indices_aprobados"] = set(int(i) for i in confirmados if i.isdigit())
        s["mat_ok"]                = True
        s["mat_confirmado_at"]     = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        log.info(f"Materiales APROBADOS | sid={sid[:8]} | {len(confirmados)} seleccionados")
        return redirect(url_for("liquidacion.verificar_get", sid=sid))

    # GET — ejecutar verificación de materiales contra SGS
    if not s.get("mat_rows"):
        return render_template("liq_materiales.html", sid=sid, s=s,
                               sgs_error="No se encontró la hoja CRUCE_SERGIO en el archivo.")

    try:
        cliente = _sgs()
    except Exception as ex:
        return render_template("liq_materiales.html", sid=sid, s=s,
                               sgs_error=f"Error conectando a SGS: {ex}")

    # Obtener PID desde los checks de MO ya realizados
    pid = next((ck["pid"] for ck in s.get("checks", []) if ck.get("pid")), None)
    if not pid:
        return render_template("liq_materiales.html", sid=sid, s=s,
                               sgs_error="Proyecto SGS no resuelto — verifica la Mano de Obra primero.")

    # Guardar nombre del proyecto para mostrar en la plantilla
    ck_pid = next((ck for ck in s.get("checks", []) if ck.get("pid") == pid), None)
    if ck_pid:
        s["mat_pro_code"]   = ck_pid.get("pro_code", "")
        s["mat_pro_nombre"] = ck_pid.get("pnombre",  "")

    # Construir mapa nodo_numero → PRO code desde los checks de MO
    # nodo_sgs en checks es "NODO1", "NODO2" → extraer dígito → "1", "2"
    nodo_pro_map: dict = {}
    for ck in s.get("checks", []):
        nodo_sgs = ck.get("nodo_sgs", "")
        pro_code = ck.get("pro_code", "")
        m_nodo = re.search(r"\d+", nodo_sgs)
        if m_nodo and pro_code:
            nodo_pro_map[m_nodo.group()] = pro_code
    s["mat_nodo_pro_map"] = nodo_pro_map

    # Obtener materiales de SGS para el proyecto (incluyendo advance para detectar ya-liquidados)
    try:
        sgs_mats = cliente.search_read(
            "project.material",
            [["project_id", "=", pid]],
            ["id", "product_code", "name", "despachado", "devuelto",
             "advance", "balance", "amount"],
            limit=300,
        )
    except Exception as ex:
        return render_template("liq_materiales.html", sid=sid, s=s,
                               sgs_error=f"Error consultando materiales en SGS: {ex}")

    sgs_by_code = {m["product_code"]: m for m in sgs_mats}

    mat_checks = []
    for mat in s["mat_rows"]:
        mc = _verificar_mat_fila(mat, sgs_by_code.get(mat["codigo"]))
        mat_checks.append(mc)

    s["mat_checks"]  = mat_checks
    s["mat_all_ok"]  = all(mc["ok"] for mc in mat_checks)

    n_ok      = sum(1 for mc in mat_checks if mc["ok"])
    n_ya_liq  = sum(1 for mc in mat_checks if mc["estado"] == "ya_liquidado")
    n_parcial = sum(1 for mc in mat_checks if mc["estado"] == "avance_parcial")
    n_err     = len(mat_checks) - n_ok
    log.info(
        f"Verificacion MAT | sid={sid[:8]} | {n_ok}/{len(mat_checks)} OK | "
        f"{n_ya_liq} ya-liq | {n_parcial} parcial | {n_err} descuadres"
    )

    return render_template("liq_materiales.html", sid=sid, s=s)


@liq.route("/verificar-get/<sid>")
@require_auth
def verificar_get(sid):
    """Muestra la página de verificación (GET) con los resultados ya calculados."""
    s = _sesiones.get(sid)
    if not s:
        return redirect(url_for("liquidacion.dashboard"))
    return render_template("liq_verify.html", sid=sid, s=s)


@liq.route("/confirmar/<sid>", methods=["POST"])
@require_auth
def confirmar(sid):
    s = _sesiones.get(sid)
    if not s:
        return redirect(url_for("liquidacion.dashboard"))

    if not s.get("all_ok"):
        return render_template("liq_verify.html", sid=sid, s=s,
                               confirm_error="Hay filas con errores. No es posible confirmar.")

    # Si hay materiales, deben estar aprobados
    if s.get("mat_rows") and not s.get("mat_ok"):
        return render_template("liq_verify.html", sid=sid, s=s,
                               confirm_error=(
                                   "Debes verificar y aprobar los materiales antes de confirmar. "
                                   "Usa el botón 'Verificar Materiales'."
                               ))

    confirmados = request.form.getlist("confirmado")
    n_filas     = len(s["rows"])

    if len(confirmados) != n_filas:
        return render_template("liq_verify.html", sid=sid, s=s,
                               confirm_error=(
                                   f"Debes aprobar las {n_filas} filas antes de enviar. "
                                   f"Solo {len(confirmados)} marcadas."
                               ))

    s["state"]         = "ejecutando"
    s["confirmado_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    s["progreso"] = {
        "estado":       "iniciando",
        "total":        n_filas,
        "ok":           0,
        "err":          0,
        "log":          [],
        "error_global": None,
    }

    log.info(f"Liquidacion INICIADA | sid={sid[:8]} | {n_filas} filas | archivo={s['filename']}")

    # Lanzar motor en hilo de fondo
    t = threading.Thread(target=_ejecutar_liquidacion, args=(sid,), daemon=True)
    t.start()

    return redirect(url_for("liquidacion.progreso", sid=sid))


@liq.route("/confirmar-materiales/<sid>", methods=["POST"])
@require_auth
def confirmar_materiales(sid):
    """
    Lanza el motor de liquidación de materiales en segundo plano.
    Requiere que mat_ok=True (materiales ya verificados y aprobados).
    """
    s = _sesiones.get(sid)
    if not s:
        return redirect(url_for("liquidacion.dashboard"))

    if not s.get("mat_ok"):
        return render_template("liq_verify.html", sid=sid, s=s,
                               confirm_error=(
                                   "Debes verificar y aprobar los materiales "
                                   "antes de confirmar."
                               ))

    n_mat = len(s.get("mat_checks", []))

    s["progreso_mat"] = {
        "estado":       "iniciando",
        "total":        n_mat,
        "ok":           0,
        "ya_liq":       0,
        "parcial":      0,
        "skip":         0,
        "err":          0,
        "log":          [],
        "error_global": None,
    }
    s["mat_confirmado_motor_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    s["state"] = "mat_ejecutando"

    log.info(
        f"Motor MAT INICIADO | sid={sid[:8]} | "
        f"{n_mat} materiales | archivo={s['filename']}"
    )

    t = threading.Thread(
        target=_ejecutar_liquidacion_materiales, args=(sid,), daemon=True
    )
    t.start()

    return redirect(url_for("liquidacion.progreso_mat", sid=sid))


@liq.route("/progreso-mat/<sid>")
@require_auth
def progreso_mat(sid):
    s = _sesiones.get(sid)
    if not s:
        return redirect(url_for("liquidacion.dashboard"))
    return render_template("liq_progreso_mat.html", sid=sid, s=s)


@liq.route("/progreso-mat-api/<sid>")
@require_auth
def progreso_mat_api(sid):
    from flask import jsonify
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"error": "session not found"}), 404
    prog = s.get("progreso_mat", {})
    return jsonify({
        "estado":       prog.get("estado", "desconocido"),
        "total":        prog.get("total", 0),
        "ok":           prog.get("ok", 0),
        "ya_liq":       prog.get("ya_liq", 0),
        "parcial":      prog.get("parcial", 0),
        "skip":         prog.get("skip", 0),
        "err":          prog.get("err", 0),
        "log":          prog.get("log", []),
        "error_global": prog.get("error_global"),
        "liquidado_at": s.get("mat_liquidado_at"),
    })


@liq.route("/progreso/<sid>")
@require_auth
def progreso(sid):
    s = _sesiones.get(sid)
    if not s:
        return redirect(url_for("liquidacion.dashboard"))
    return render_template("liq_progreso.html", sid=sid, s=s)


@liq.route("/progreso-api/<sid>")
@require_auth
def progreso_api(sid):
    from flask import jsonify
    s = _sesiones.get(sid)
    if not s:
        return jsonify({"error": "session not found"}), 404
    prog = s.get("progreso", {})
    return jsonify({
        "estado":       prog.get("estado", "desconocido"),
        "total":        prog.get("total", 0),
        "ok":           prog.get("ok", 0),
        "err":          prog.get("err", 0),
        "log":          prog.get("log", []),
        "error_global": prog.get("error_global"),
        "liquidado_at": s.get("liquidado_at"),
    })


# ── Utilidades internas ────────────────────────────────────────────────────────

def _leer_log_reciente(n: int) -> list:
    """Lee las últimas n líneas del log de actividad."""
    path = os.path.join(_log_dir, "liquidacion.log")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lineas = f.readlines()
        return [l.rstrip() for l in lineas[-n:]][::-1]
    except Exception:
        return []
